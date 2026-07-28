"""The read endpoint (spec 10 §5.4, §5.5).

`http.server.ThreadingHTTPServer` from the stdlib, zero dependencies (`09:290`).

The boundary this file exists to hold: **a broken store is not an empty answer.**
`ideas: []` with a live graph means "the lake has nothing on this query" and is
data for the A/B; an exception out of the store means "the lake is broken" and is
not data at all. Mixing them contaminates the main metric of the project, so an
exception answers 503 `{error, log_id}` and an empty ranking answers 200 (§5.4).

Every request leaves exactly one line in `data/logs/retrieve.jsonl`, the 503 ones
included: that log is not cut under any circumstances (§5.5, `08:381`).
"""
import argparse
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import trace
from ..models import LOGS_DIR
from . import rewrite

K_DEFAULT = 5                   # §8
MAX_BODY_BYTES = 64 * 1024      # a query is a sentence; anything larger is not a query

RETRIEVE_LOG = LOGS_DIR / "retrieve.jsonl"
_log_lock = threading.Lock()    # ThreadingHTTPServer: unsynchronized appends interleave

# Handed to C before the real path exists (§9, `08:348`) — same shape as §5.4,
# frozen plausible data. Touches neither the graph nor an LLM, and by design it
# writes no retrieval-log line: mock rows in the metrics log are contamination.
MOCK_RESPONSE = {
    "ideas": [
        {
            "idea_id": "idea_mock00000a",
            "text": "Filter candidates with a cheap proxy before paying for the full "
                    "evaluation, and spend the full budget only on survivors.",
            "applicability_conditions": "The full evaluation dominates the loop cost and a "
                                        "proxy correlating with it above ~0.6 exists.",
            "limitations": "The proxy has to be recalibrated when the population drifts; on "
                           "cheap fitness functions the cascade is pure overhead.",
            "failure_modes": [
                "The proxy discards a whole promising region and the run converges early.",
                "Recalibration is skipped and the correlation quietly decays over generations.",
            ],
            "effect_claimed": "3-10x fewer full evaluations per generation",
            "effect_observed": "4.2x on the reported benchmark suite",
            "trust_score": 0.62,
            "score": 0.81,
            "via": "thesis",
            "theses": [
                {
                    "text": "A two-stage cascade evaluates every candidate with a distilled "
                            "surrogate and forwards the top 15% to the real objective.",
                    "url": "https://arxiv.org/abs/2402.01000",
                    "title": "Cascaded Evaluation for Population-Based Search",
                    "effect": "4.2x fewer full evaluations at equal final fitness",
                    "locator": "§4.2, Table 3",
                },
                {
                    "text": "The surrogate is retrained every 10 generations on the pairs the "
                            "full objective has already scored.",
                    "url": "https://arxiv.org/abs/2402.01000",
                    "title": "Cascaded Evaluation for Population-Based Search",
                    "effect": "keeps rank correlation above 0.7 to generation 200",
                    "locator": "§4.4",
                },
            ],
        },
        {
            "idea_id": "idea_mock00000b",
            "text": "Keep the population split into islands that evolve independently and "
                    "exchange a few migrants on a fixed schedule.",
            "applicability_conditions": "Evaluation parallelizes across workers and the search "
                                        "space has several distinct basins worth holding.",
            "limitations": "Measured on populations of 100-1000; below that the islands are "
                           "too small to keep their own basin.",
            "failure_modes": [
                "Migration is too frequent and the islands collapse into one population.",
                "Migration is too rare and most islands waste the whole budget on a dead basin.",
            ],
            "effect_claimed": "diversity held over 5x more generations",
            "effect_observed": "",
            "trust_score": 0.41,
            "score": 0.57,
            "via": "edge",
            "theses": [
                {
                    "text": "Eight islands of 64 individuals migrate their two best every 25 "
                            "generations in a ring topology.",
                    "url": "https://arxiv.org/abs/2311.02000",
                    "title": "Island Models Revisited",
                    "effect": "final best improved by 11% over a panmictic run of equal cost",
                    "locator": "§3.1",
                },
            ],
        },
        {
            "idea_id": "idea_mock00000c",
            "text": "Store every evaluated candidate with its score in an external memory and "
                    "retrieve neighbours of the current parent before mutating.",
            "applicability_conditions": "Evaluations are expensive enough that a lookup is "
                                        "cheaper than a re-evaluation.",
            "limitations": "Memory grows with the run; without eviction the retrieval itself "
                           "becomes the bottleneck after ~10^5 entries.",
            "failure_modes": [
                "Duplicate detection is exact-match only and near-duplicates are re-evaluated.",
            ],
            "effect_claimed": "15% of evaluations avoided as duplicates",
            "effect_observed": "",
            "trust_score": 0.28,
            "score": 0.33,
            "via": "padding",
            "theses": [
                {
                    "text": "The archive is queried by embedding similarity before each "
                            "mutation and returns the five nearest scored candidates.",
                    "url": "https://example.org/docs/evo-memory",
                    "title": "Evolutionary Memory: Implementation Notes",
                    "effect": "15% duplicate evaluations avoided",
                    "locator": "section 'Archive lookup'",
                },
            ],
        },
    ],
    "log_id": "log_mock000001",
    "cost": {"tokens_in": 0, "tokens_out": 0, "wall_ms": 0},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _write_log(record: dict) -> None:
    """One line per request, §5.5. Never skipped, not even on 503."""
    line = json.dumps(record, ensure_ascii=False)
    with _log_lock:
        RETRIEVE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with RETRIEVE_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def parse_request(body) -> dict:
    """Validate the §5.4 request. Raises ValueError (-> 400) with a readable text."""
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    query = body.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query is required and must be a non-empty string")
    k = body.get("k", K_DEFAULT)
    if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
        raise ValueError(f"k must be a positive integer, got {k!r}")
    budget = body.get("budget")
    if budget is not None and (not isinstance(budget, int) or isinstance(budget, bool)
                               or budget <= 0):
        raise ValueError(f"budget must be a positive integer (max_tokens ceiling for the "
                         f"rewrite step), got {budget!r}")
    do_rewrite = body.get("rewrite", True)
    if not isinstance(do_rewrite, bool):
        raise ValueError(f"rewrite must be a boolean, got {do_rewrite!r}")
    run_id = body.get("run_id")
    if run_id is not None and not isinstance(run_id, str):
        raise ValueError(f"run_id must be a string, got {run_id!r}")
    if body.get("allow_web"):
        # The field is accepted (§5.4: turning stage III on must not break the
        # integration with C) but the stage does not exist, and answering as if
        # the web had been searched would be a lie about where the ideas came from.
        raise ValueError("allow_web=true is not supported: the web stage (III) is out of "
                         "the MVP; resend with allow_web=false")
    return {"query": query, "k": k, "budget": budget, "do_rewrite": do_rewrite, "run_id": run_id}


def retrieve(query: str, k: int = K_DEFAULT, *, budget: int | None = None,
             do_rewrite: bool = True, run_id: str | None = None) -> tuple[int, dict]:
    """One /retrieve call: (http status, body). Always leaves one log line (§5.5)."""
    log_id = "log_" + uuid.uuid4().hex[:12]
    # ponytail: trace.py keeps run/log id per process, so concurrent requests can
    # cross-tag each other's trace rows. Fine at demo load; needs a ContextVar in
    # trace.py if /retrieve ever gets real concurrency.
    trace.set_run_id(run_id or trace.current_run_id(), log_id=log_id)
    started = time.perf_counter()
    before = trace.totals()
    # One dict shared by the response and the log line, filled in the `finally`:
    # the two must never disagree about what the request cost.
    cost = {"tokens_in": 0, "tokens_out": 0, "wall_ms": 0.0}
    record = {"log_id": log_id, "ts": _now(), "query_raw": query, "query_rewritten": query,
              "rewrite_failed": False, "k": k, "returned": [], "cut_off": [], "cost": cost}
    try:
        if do_rewrite:
            record["query_rewritten"], record["rewrite_failed"] = rewrite.rewrite(query, budget)
        try:
            # Imported here, not at module level: `rank` pulls in the embedding
            # model (§3.2), and --mock must reach neither it nor the graph. Inside
            # the guard, so an unimportable ranker is a logged 503, not a dropped
            # connection.
            from . import rank
            ideas, payload = rank.rank(record["query_rewritten"], k=k)
            record["returned"], record["cut_off"] = payload["returned"], payload["cut_off"]
        except Exception as exc:
            # §5.4 — the store raised, or ranking could not produce an answer at
            # all. That is "the lake is broken", not "the lake is empty": 503, and
            # the reason lands in the log next to `returned: []`.
            record["error"] = f"{type(exc).__name__}: {exc}"
            return 503, {"error": record["error"], "log_id": log_id}
        # An empty `ideas` here is a live graph with nothing to say — 200, and it
        # is data for the A/B (§5.4).
        return 200, {"ideas": ideas, "log_id": log_id, "cost": cost}
    finally:
        after = trace.totals()
        cost["tokens_in"] = after["tokens_in"] - before["tokens_in"]
        cost["tokens_out"] = after["tokens_out"] - before["tokens_out"]
        cost["wall_ms"] = round((time.perf_counter() - started) * 1000, 1)
        _write_log(record)


class _Handler(BaseHTTPRequestHandler):
    server_version = "IdeasLake/0.1"

    def do_POST(self) -> None:
        if self.path.split("?")[0] != "/retrieve":
            self._send(404, {"error": f"unknown path {self.path!r}, the endpoint is POST /retrieve"})
            return
        try:
            body = self._read_json()
            req = parse_request(body)
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
            return
        if getattr(self.server, "mock", False):
            self._send(200, MOCK_RESPONSE)
            return
        status, payload = retrieve(req["query"], req["k"], budget=req["budget"],
                                   do_rewrite=req["do_rewrite"], run_id=req["run_id"])
        self._send(status, payload)

    def _read_json(self):
        length = self.headers.get("Content-Length")
        if length is None or not length.strip().isdigit():
            raise ValueError("Content-Length header is required")
        size = int(length)
        if size > MAX_BODY_BYTES:
            raise ValueError(f"body of {size} bytes exceeds the {MAX_BODY_BYTES} byte limit")
        raw = self.rfile.read(size)
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"body is not valid JSON: {exc}") from exc

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(port: int = 8077, mock: bool = False) -> None:
    """Blocking. `mock=True` answers MOCK_RESPONSE and touches nothing else."""
    server = ThreadingHTTPServer(("", port), _Handler)
    server.mock = mock
    if not mock:
        # §8: the budget is p95 <= 5 s, and the first embedding call loads the
        # model (seconds, once per process). Pay it here so no request does.
        # No try/except: a server that cannot embed cannot answer, and finding
        # that out at start beats finding it out per request as a 503.
        from .. import embed
        embed.embed_query("warm up")
    print(f"/retrieve on port {port}{' (MOCK)' if mock else ''}, log -> {RETRIEVE_LOG}",
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python3 -m lake.retrieve.api")
    parser.add_argument("--port", type=int, default=8077)
    parser.add_argument("--mock", action="store_true",
                        help="serve the frozen MOCK_RESPONSE, touch neither graph nor LLM")
    parser.add_argument("--selfcheck", action="store_true",
                        help="offline check: no network, no graph, nothing left running")
    args = parser.parse_args()

    if not args.selfcheck:
        serve(args.port, args.mock)
        raise SystemExit(0)

    import socket
    import sqlite3
    import sys
    import tempfile
    import types
    import urllib.error
    import urllib.request
    from pathlib import Path

    from .. import embed

    warmed: list[str] = []
    embed.embed_query = lambda text: warmed.append(text)   # the model must not load here
    RETRIEVE_LOG = Path(tempfile.mkdtemp(prefix="retrieve-selfcheck-")) / "retrieve.jsonl"

    # `rank` is written by another agent; the check owns a stub of it either way.
    stub = types.ModuleType("lake.retrieve.rank")
    sys.modules["lake.retrieve.rank"] = stub
    sys.modules["lake.retrieve"].rank = stub
    rewrite.rewrite = lambda query, budget=None: (query + " :: mechanisms", False)

    IDEAS = [{"idea_id": "idea_a", "text": "t", "applicability_conditions": "a",
              "limitations": "l", "failure_modes": [], "effect_claimed": "c",
              "effect_observed": "o", "trust_score": 0.5, "score": 0.9, "via": "thesis",
              "theses": [{"text": "x", "url": "u", "title": "T", "effect": "e",
                          "locator": "§1"}]}]
    PAYLOAD = {"returned": [{"idea_id": "idea_a", "score": 0.9, "raw_score": 0.031,
                             "rank": 1, "via": "thesis"}],
               "cut_off": [{"idea_id": "idea_z", "score": 0.2, "raw_score": 0.011, "rank": 6}]}

    def start(port: int, mock: bool) -> None:
        threading.Thread(target=serve, args=(port, mock), daemon=True).start()
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        raise AssertionError(f"server on port {port} never came up")

    def free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def post(port: int, payload=None, raw: bytes | None = None):
        data = raw if raw is not None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(f"http://127.0.0.1:{port}/retrieve", data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def log_lines() -> list[dict]:
        if not RETRIEVE_LOG.exists():
            return []
        return [json.loads(ln) for ln in RETRIEVE_LOG.read_text(encoding="utf-8").splitlines()]

    # (a) --mock: the shape of §5.4, verbatim, without touching anything.
    mock_port = free_port()
    start(mock_port, True)
    assert warmed == [], "mock mode must not warm up the embedding model"
    status, body = post(mock_port, {"query": "speed up evaluation"})
    assert status == 200, status
    assert set(body) == {"ideas", "log_id", "cost"}, sorted(body)
    assert set(body["cost"]) == {"tokens_in", "tokens_out", "wall_ms"}, body["cost"]
    assert len(body["ideas"]) >= 2, len(body["ideas"])
    for idea in body["ideas"]:
        assert set(idea) == {"idea_id", "text", "applicability_conditions", "limitations",
                             "failure_modes", "effect_claimed", "effect_observed",
                             "trust_score", "score", "via", "theses"}, sorted(idea)
        assert idea["theses"], idea["idea_id"]
        for leaf in idea["theses"]:
            assert set(leaf) == {"text", "url", "title", "effect", "locator"}, sorted(leaf)
    assert log_lines() == [], "the mock must not write into the metrics log"

    # (b) real path, rank and rewrite stubbed: 200 + a full §5.5 line.
    port = free_port()
    start(port, False)
    assert warmed == ["warm up"], f"the server must warm the embedder once, got {warmed}"
    stub.rank = lambda query, k=5: (IDEAS, PAYLOAD)
    status, body = post(port, {"query": "speed up evaluation", "k": 3, "run_id": "run-selfcheck"})
    assert status == 200 and body["ideas"] == IDEAS, (status, body)
    line = log_lines()[-1]
    assert set(line) == {"log_id", "ts", "query_raw", "query_rewritten", "rewrite_failed",
                         "k", "returned", "cut_off", "cost"}, sorted(line)
    assert line["log_id"] == body["log_id"] and line["k"] == 3
    assert line["query_rewritten"] == "speed up evaluation :: mechanisms"
    assert line["rewrite_failed"] is False
    assert line["returned"][0]["raw_score"] == 0.031 and line["returned"][0]["via"] == "thesis"
    assert line["cut_off"][0]["raw_score"] == 0.011
    # The response and the log must never disagree about the cost of a request.
    assert line["cost"] == body["cost"], (line["cost"], body["cost"])
    assert set(line["cost"]) == {"tokens_in", "tokens_out", "wall_ms"}, line["cost"]

    rewrite.rewrite = lambda query, budget=None: (query, True)      # degraded rewrite
    post(port, {"query": "island model migration", "budget": 64})
    assert log_lines()[-1]["rewrite_failed"] is True, "the degradation must reach the log"
    rewrite.rewrite = lambda query, budget=None: (query + " :: mechanisms", False)

    # (c) the store raised -> 503, and the log line still exists.
    def dead_store(query, k=5):
        raise sqlite3.OperationalError("database is locked")

    stub.rank = dead_store
    status, body = post(port, {"query": "speed up evaluation"})
    assert status == 503, status
    assert set(body) == {"error", "log_id"} and "OperationalError" in body["error"], body
    line = log_lines()[-1]
    assert line["log_id"] == body["log_id"] and line["returned"] == [] and line["cut_off"] == []
    assert line["error"] == body["error"], line

    # (d) the boundary this endpoint exists for: empty ranking is 200, not 503.
    stub.rank = lambda query, k=5: ([], {"returned": [], "cut_off": []})
    status, body = post(port, {"query": "нет такого в озере"})
    assert status == 200 and body["ideas"] == [], (status, body)
    assert log_lines()[-1]["log_id"] == body["log_id"]

    # (e) bad requests: 400 with a text, never 500 and never a silent default.
    stub.rank = lambda query, k=5: (IDEAS, PAYLOAD)
    before_lines = len(log_lines())
    for payload, raw, expect in [
        (None, b"{not json", "not valid JSON"),
        ({"k": 5}, None, "query is required"),
        ({"query": "q", "k": 0}, None, "k must be a positive integer"),
        ({"query": "q", "k": -3}, None, "k must be a positive integer"),
        ({"query": "   "}, None, "query is required"),
        ({"query": "q", "allow_web": True}, None, "allow_web"),
        ({"query": "q", "budget": 0}, None, "budget must be"),
    ]:
        status, body = post(port, payload, raw)
        assert status == 400, (payload, raw, status, body)
        assert expect in body["error"], body
    assert len(log_lines()) == before_lines, "a rejected request is not a retrieval"

    status, body = post(port, {"query": "q", "allow_web": False})
    assert status == 200, "allow_web=false is accepted, the field only has to exist"

    print(f"ok: mock shape, 200 with ideas, 503 on a dead store, 200 on an empty lake, "
          f"400 on bad input; {len(log_lines())} log lines in {RETRIEVE_LOG}")
