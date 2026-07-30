"""One ingest run at a time, in a background thread (spec 10 §4.7).

An ingest is minutes of LLM calls, so it cannot answer inside a request. It also
cannot run twice at once: phase 2 is sequential *by design* — two parallel runs
open two ideas for one mechanism (§4.5) — and phase 1 rewrites the staging lines
of the sources it touches and drops the cursor. So this module holds exactly one
slot, and a second start is a 409, never a queue.

Jobs live in this process. A shutdown mid-run loses the record, not the work: the
cursor makes phase 2 resume where it stopped, and phase 1 replays a source from
its own beginning. The thread is a daemon for that reason — a 25-minute ingest
must not hold the port open on Ctrl-C.
"""
import contextlib
import threading
import uuid
from datetime import datetime, timezone

from .. import trace

_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_current: str | None = None
MAX_KEPT = 50           # ring of finished jobs; the reports themselves are small


class Busy(RuntimeError):
    """A job is already running. Carries it, so the caller can say which."""

    def __init__(self, job: dict):
        super().__init__(f"job {job['id']} ({job['kind']}) is already running")
        self.job = job


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _claim(kind: str, args: dict | None, job_id: str | None = None) -> dict:
    """Take the single slot or raise `Busy`. The check and the take are one step —
    two callers a microsecond apart must not both believe the lake is free.

    `job_id` lets a caller that already has a record — the phase-2 writer, whose job
    lives in `queue.py` — reuse its id instead of minting a second one. Two ids for
    one piece of work would show up twice in `/ingest/jobs` and make `job_running`
    name an id the caller polling `/fetch` has never seen.
    """
    global _current
    with _lock:
        if _current is not None:
            raise Busy(_jobs[_current])
        job = {"id": job_id or f"job_{uuid.uuid4().hex[:12]}", "kind": kind,
               "status": "running",
               "created_at": _now(), "finished_at": None, "args": args or {},
               "report": None, "error": None}
        _jobs[job["id"]] = job
        _current = job["id"]
        _evict()
    return job


def _release(job: dict) -> None:
    global _current
    job["finished_at"] = _now()
    with _lock:
        if _current == job["id"]:
            _current = None


def start(kind: str, fn, args: dict | None = None) -> dict:
    """Claim the slot and run `fn()` in the background. Raises `Busy` if taken."""
    job = _claim(kind, args)
    try:
        threading.Thread(target=_run, args=(job, fn), name=f"lake-{kind}", daemon=True).start()
    except BaseException as exc:
        # The slot is already taken at this point. A thread that never started
        # would hold it until the process restarts, and the job would read
        # "running" forever for work that was never invoked — every later ingest
        # and every repair refused with a 409 naming a job that does not exist.
        job["status"] = "failed"
        job["error"] = f"{type(exc).__name__}: {exc or '(no message)'}"
        _release(job)
        raise
    return job


@contextlib.contextmanager
def exclusive(kind: str, args: dict | None = None, job_id: str | None = None):
    """The same slot, for work short enough to answer inside the request (a reindex).

    Claiming it is what keeps a repair and an ingest off the store at the same time;
    checking `running()` and then working would leave the window open between the two.
    """
    job = _claim(kind, args, job_id)
    try:
        yield job
        job["status"] = "ok"
    except BaseException as exc:
        job["status"] = "failed"
        job["error"] = f"{type(exc).__name__}: {exc or '(no message)'}"
        raise
    finally:
        _release(job)


def _run(job: dict, fn) -> None:
    try:
        # The job's own trace ids and its own token counter, same mechanism as one
        # /retrieve request: the process-global totals keep accumulating for §4.7.
        with trace.request(job["id"]) as own:
            report = fn()
            report = dict(report) if isinstance(report, dict) else {"result": report}
            job["report"] = {**report, "cost": dict(own)}
        job["status"] = "ok"
    except BaseException as exc:
        # BaseException, not Exception: nothing above this frame can catch anything
        # here, so a KeyboardInterrupt or a SystemExit that escaped would leave the
        # job reading "running" forever — a status that lies is worse than a failure.
        job["status"] = "failed"
        job["error"] = f"{type(exc).__name__}: {exc or '(no message)'}"
    finally:
        _release(job)


def _evict() -> None:
    """Drop the oldest finished jobs past MAX_KEPT. Caller holds `_lock`."""
    finished = [j for j in _jobs.values() if j["status"] != "running"]
    for job in finished[:max(0, len(finished) - MAX_KEPT)]:
        _jobs.pop(job["id"], None)


def get(job_id: str) -> dict | None:
    with _lock:
        return _jobs.get(job_id)


def listing() -> list[dict]:
    """Newest first — by insertion order, not by `created_at`.

    `created_at` has second resolution, and two jobs in the same second sorted
    into whatever order the comparison happened to produce. Dicts keep insertion
    order, which is exactly the order jobs were claimed in.
    """
    with _lock:
        return list(reversed(_jobs.values()))


def running() -> dict | None:
    with _lock:
        return _jobs.get(_current) if _current else None


def _reset_for_tests() -> None:
    global _current
    with _lock:
        _jobs.clear()
        _current = None
