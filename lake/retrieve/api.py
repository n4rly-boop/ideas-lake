"""The read path core (spec 10 §5.4, §5.5) — transport-agnostic.

`retrieve()` decides the status and writes the log line; `app.py` is the HTTP
layer over it (FastAPI). Splitting them keeps the boundary below testable
without a socket, and the self-check drives this function directly.

Spec §5.4 named `http.server` for zero dependencies (`09:290`); the project
moved to FastAPI for typed request/response models and a generated OpenAPI
schema for C. Nothing in this file knows about either.

The boundary this file exists to hold: **a broken store is not an empty answer.**
`ideas: []` with a live graph means "the lake has nothing on this query" and is
data for the A/B; an exception out of the store means "the lake is broken" and is
not data at all. Mixing them contaminates the main metric of the project, so an
exception answers 503 `{error, log_id}` and an empty ranking answers 200 (§5.4).

Every request leaves exactly one line in `data/logs/retrieve.jsonl`, the 503 ones
included: that log is not cut under any circumstances (§5.5, `08:381`).
"""
import json
import threading
import time
import uuid
from datetime import datetime, timezone

from .. import trace
from ..models import LOGS_DIR
from . import rewrite

K_DEFAULT = 5                   # §8

RETRIEVE_LOG = LOGS_DIR / "retrieve.jsonl"
_log_lock = threading.Lock()    # requests are served from a threadpool: appends interleave

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
            "cosine_similarity": 0.71,
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
            "cosine_similarity": 0.55,
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
            "cosine_similarity": 0.46,
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


def retrieve(query: str, k: int = K_DEFAULT, *, budget: int | None = None,
             do_rewrite: bool = True, run_id: str | None = None) -> tuple[int, dict]:
    """One /retrieve call: (http status, body). Always leaves one log line (§5.5)."""
    log_id = "log_" + uuid.uuid4().hex[:12]
    started = time.perf_counter()
    # One dict shared by the response and the log line, filled in the `finally`:
    # the two must never disagree about what the request cost.
    cost = {"tokens_in": 0, "tokens_out": 0, "wall_ms": 0.0}
    record = {"log_id": log_id, "ts": _now(), "query_raw": query, "query_rewritten": query,
              "rewrite_failed": False, "k": k, "returned": [], "cut_off": [], "cost": cost}
    # Per-request ids and per-request token counter (trace.request). Diffing the
    # process-global totals made concurrent requests claim each other's tokens,
    # and the global run/log id cross-tagged trace rows even sequentially.
    with trace.request(run_id or trace.current_run_id(), log_id=log_id) as own:
        try:
            if do_rewrite:
                try:
                    record["query_rewritten"], record["rewrite_failed"] = \
                        rewrite.rewrite(query, budget)
                except Exception as exc:
                    # §5.1 — rewriting is an improvement, never a condition of
                    # correctness. `rewrite` catches LLMError/TimeoutError itself;
                    # anything else used to escape `retrieve` entirely and drop the
                    # connection with no status and no log line. Degrade to the raw
                    # query and say so, so the ablation is not counted on dirty data.
                    record["rewrite_failed"] = True
                    record["rewrite_error"] = f"{type(exc).__name__}: {exc}"
            try:
                # Imported here, not at module level: `rank` pulls in the embedding
                # model (§3.2), and --mock must reach neither it nor the graph. Inside
                # the guard, so an unimportable ranker is a logged 503, not a dropped
                # connection.
                from . import rank
                from .. import graph_client, index
                # 2026-07-31 finding: `index.search_theses` on an EMPTY index answers
                # `[]` on purpose (§6.12's own self-check asserts exactly that) — the
                # right behaviour for a genuinely empty lake. But an index wiped by a
                # bad rebuild or a file mutated outside this process is also an empty
                # `idx_thesis`, and nothing before this point told the two apart: the
                # store still holding theses is a broken index answering as an empty
                # one, not the "the lake has nothing on this query" that a 200 means
                # (§5.4). A few theses of lag while phase 2 is mid-write is NOT this —
                # only the all-or-nothing wipe is refused here; `/admin/reindex` (§6.19)
                # is the repair either way.
                if index.count() == 0 and graph_client.counts()["theses"] > 0:
                    raise RuntimeError(
                        "index is empty but the store holds theses — index and store "
                        "diverged (§6.19); refusing to answer as an empty lake")
                ideas, payload = rank.rank(record["query_rewritten"], k=k)
                record["returned"], record["cut_off"] = payload["returned"], payload["cut_off"]
                # The body is validated HERE, inside the guard, and not left to the
                # HTTP layer. FastAPI validates a response AFTER this function has
                # returned 200 and its `finally` has already written `returned: [...]`
                # to the metrics log — so a body it could not serialize left the
                # caller with a 500 while the log recorded a successful answer with
                # two ideas in it. The A/B is measured off that log (§5.5).
                # Imported lazily: `api.schemas` IS the §5.4 contract, but this
                # module must stay importable without the HTTP layer.
                from ..api.schemas import RetrieveResponse
                body = {"ideas": ideas, "log_id": log_id, "cost": cost}
                RetrieveResponse.model_validate(body)
                return 200, body
            except Exception as exc:
                # §5.4 — the store raised, or ranking could not produce an answer at
                # all. That is "the lake is broken", not "the lake is empty": 503, and
                # the reason lands in the log next to `returned: []`.
                # An empty `ideas` is NOT this branch: a live graph with nothing to say
                # returns 200 above, and that is data for the A/B (§5.4).
                record["error"] = f"{type(exc).__name__}: {exc}"
                record["returned"], record["cut_off"] = [], []
                return 503, {"error": record["error"], "log_id": log_id}
        finally:
            cost["tokens_in"] = own["tokens_in"]
            cost["tokens_out"] = own["tokens_out"]
            cost["wall_ms"] = round((time.perf_counter() - started) * 1000, 1)
            _write_log(record)
