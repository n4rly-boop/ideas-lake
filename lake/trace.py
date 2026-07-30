"""Trace log, contract C5 (spec 10 §3.3).

One JSONL line per LLM call and per graph call, appended under a module lock:
/retrieve runs in a ThreadingHTTPServer and unsynchronized appends interleave.
`finish_reason`, `max_tokens`, `build_info` are the three extra fields — half of
the llama.cpp bugs are tied to the build number (probe-results.md:52).
"""
import contextlib
import contextvars
import functools
import json
import threading
import time
import uuid
from datetime import datetime, timezone

from .models import TRACES_DIR

_lock = threading.Lock()
_run_id = uuid.uuid4().hex[:12]
_extra: dict[str, str] = {}          # task_id / log_id, optional in C5 (07:114)
_totals = {"tokens_in": 0, "tokens_out": 0, "wall_ms": 0.0}

# Per-request identity and cost. /retrieve runs under ThreadingHTTPServer, and
# process-global counters made every concurrent request report the sum of all
# overlapping ones — four parallel calls each claiming 4x their own tokens, in
# the response body AND in the log line. Cost is the project's own metric (D),
# so a wrong number here is worse than a missing one. A ContextVar is per-thread
# and per-task without any caller doing bookkeeping.
_ctx_ids: contextvars.ContextVar[dict] = contextvars.ContextVar("lake_trace_ids")
_ctx_totals: contextvars.ContextVar[dict] = contextvars.ContextVar("lake_trace_totals")


def set_run_id(run_id: str, *, task_id: str | None = None, log_id: str | None = None) -> None:
    """Overwrite the process run id (default: generated once at import)."""
    global _run_id
    _run_id = run_id
    if task_id is not None:
        _extra["task_id"] = task_id
    if log_id is not None:
        _extra["log_id"] = log_id


@contextlib.contextmanager
def request(run_id: str | None = None, *, log_id: str | None = None):
    """Scope one /retrieve request: its own ids and its own cost counter.

    Yields the counter dict; it holds exactly the calls made inside this block,
    on this thread. Process-wide `totals()` keeps accumulating in parallel — the
    ingest report wants the whole run, a request wants only itself.
    """
    ids = {k: v for k, v in (("run_id", run_id), ("log_id", log_id)) if v is not None}
    own = {"tokens_in": 0, "tokens_out": 0, "wall_ms": 0.0}
    id_token = _ctx_ids.set(ids)
    tot_token = _ctx_totals.set(own)
    try:
        yield own
    finally:
        _ctx_ids.reset(id_token)
        _ctx_totals.reset(tot_token)


def current_run_id() -> str:
    return _ctx_ids.get({}).get("run_id") or _run_id


def totals() -> dict:
    """Accumulated cost of this process — the ingest run report (§4.7).

    NOT the `cost` of one /retrieve: use `request()` for that, or concurrent
    requests report each other's tokens.
    """
    with _lock:
        return dict(_totals)


def _write(record: dict) -> None:
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
              "run_id": _run_id, **_extra, **_ctx_ids.get({}), **record}
    line = json.dumps(record, ensure_ascii=False)
    own = _ctx_totals.get(None)
    with _lock:
        TRACES_DIR.mkdir(parents=True, exist_ok=True)
        with (TRACES_DIR / f"{record['run_id']}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        for field in ("tokens_in", "tokens_out", "wall_ms"):
            _totals[field] += record[field]
            if own is not None:
                own[field] += record[field]


def trace(component: str, op: str):
    """@trace(component="ingest", op="parse") — wall time of one step.

    Token counts stay 0 here: the LLM call inside logs its own row via log_llm.
    A failing step is still logged (with `error`) and the exception re-raised —
    an unlogged failure would read as a step that never cost anything.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            started = time.perf_counter()
            error = None
            try:
                return fn(*args, **kwargs)
            except BaseException as exc:
                error = type(exc).__name__
                raise
            finally:
                record = {"component": component, "op": op, "model": None,
                          "tokens_in": 0, "tokens_out": 0,
                          "wall_ms": round((time.perf_counter() - started) * 1000, 1)}
                if error is not None:
                    record["error"] = error
                _write(record)
        return wrapper
    return decorator


def log_llm(op: str, model: str, usage: dict, wall_ms: float,
            finish_reason: str, max_tokens: int, build_info: str) -> None:
    """One LLM call. `usage` is the server's, reliable with stream=False (09:293).

    Keys are indexed, not `.get`-ed: a missing usage block must fail loudly, a
    silent 0 would understate Δcost, which is a project metric (07:120).
    """
    _write({"component": "llm", "op": op, "model": model,
            "tokens_in": usage["prompt_tokens"], "tokens_out": usage["completion_tokens"],
            "wall_ms": round(wall_ms, 1), "finish_reason": finish_reason,
            "max_tokens": max_tokens, "build_info": build_info})


if __name__ == "__main__":
    import pathlib
    import tempfile

    set_run_id("selfcheck-" + uuid.uuid4().hex[:6], log_id="log-1")
    # Into a temp dir, not into data/traces/. Unlinking the file afterwards was not
    # enough: `_write` mkdirs the directory, and an otherwise-absent `data/` that exists
    # while holding no file is what makes `vault.demo`'s leak guard refuse to run
    # ("the leak guard measured nothing"). README §1 tells the reader to run every
    # module's `__main__`, so this one has to leave nothing behind either.
    _tmp = tempfile.TemporaryDirectory(prefix="lake-trace-selfcheck-")
    TRACES_DIR = pathlib.Path(_tmp.name)
    path = TRACES_DIR / f"{current_run_id()}.jsonl"

    @trace(component="ingest", op="parse")
    def step(n):
        if n < 0:
            raise ValueError("boom")
        return n * 2

    assert step(21) == 42
    try:
        step(-1)
    except ValueError:
        pass
    else:
        raise AssertionError("exception swallowed by the decorator")

    log_llm("parse", "qwen3.5-9b", {"prompt_tokens": 100, "completion_tokens": 20},
            wall_ms=1234.5, finish_reason="stop", max_tokens=2500, build_info="b1234-abc")

    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 3, rows
    ok, failed, llm = rows
    for row in rows:
        assert set(row) >= {"ts", "component", "op", "model", "tokens_in", "tokens_out",
                            "wall_ms", "run_id", "log_id"}, row
        assert row["run_id"] == current_run_id() and row["log_id"] == "log-1"
    assert (ok["component"], ok["op"]) == ("ingest", "parse") and "error" not in ok
    assert failed["error"] == "ValueError"
    assert llm["tokens_in"] == 100 and llm["finish_reason"] == "stop"
    assert llm["max_tokens"] == 2500 and llm["build_info"] == "b1234-abc"
    assert totals() == {"tokens_in": 100, "tokens_out": 20,
                        "wall_ms": ok["wall_ms"] + failed["wall_ms"] + 1234.5}, totals()
    path.unlink()
    print(f"ok: 3 rows, totals={totals()}")
