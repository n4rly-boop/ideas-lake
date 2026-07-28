"""Query rewrite, one 9B call inside /retrieve (spec 10 §5.1).

Retrieval has to run on the terms of a solution, not of a task (`08:218`): the
call turns the raw query into a draft guess at the mechanisms (MemoRAG pattern,
`08:220`) and the search runs on that guess.

This is the only place on the read path where an LLM failure is not fatal. A
timeout or an `LLMError` is a *named* degradation, not a silent default: the raw
query goes on and `failed=True` reaches the retrieval log as `rewrite_failed`,
without which the "does rewriting help" ablation would be computed over a
contaminated sample (§5.1). Rewriting is an improvement, not a condition of
correctness, and §9 puts it first in the cutting order.
"""
from .. import llm
from ..models import REWRITE_SCHEMA, text_hash

MAX_TOKENS = 200        # §8
TIMEOUT_S = 20.0        # §8 — the ceiling of the degradation, /retrieve itself has 5 s p95

# Process-local, no eviction: a run asks tens of queries (§9), and the ablation
# reruns the same set. The cost of a call is logged by `llm.complete` as its own
# trace row (op="rewrite"), so D sees rewriting as overhead; a hit costs nothing.
_cache: dict[tuple[str, int], str] = {}


def rewrite(query: str, budget: int | None = None) -> tuple[str, bool]:
    """(query to search with, failed). `budget` is a ceiling on max_tokens (§5.4)."""
    if not query.strip():
        raise ValueError("rewrite got an empty query")
    if budget is not None and budget <= 0:
        raise ValueError(f"budget must be a positive token count, got {budget}")
    max_tokens = MAX_TOKENS if budget is None else min(MAX_TOKENS, budget)

    key = (text_hash(query), max_tokens)
    cached = _cache.get(key)
    if cached is not None:
        return cached, False

    try:
        out = llm.complete(query, system=llm.load_prompt("rewrite"), schema=REWRITE_SCHEMA,
                           op="rewrite", max_tokens=max_tokens, timeout=TIMEOUT_S,
                           model=llm.QWEN_9B, temperature=0.0)
    except (llm.LLMError, TimeoutError):
        # Named degradation, see the module docstring. The failed call is already
        # in the trace (llm.complete logs error rows too, §3.1), and the caller
        # writes `rewrite_failed: true` into the retrieval log.
        return query, True

    rewritten = out["query"].strip()
    if not rewritten:
        # An empty rewrite searches for nothing and looks like a normal answer;
        # it degrades exactly like a timeout, so it is reported exactly like one.
        return query, True
    # Only successes are cached: a timeout is transient, and caching one would
    # fake `rewrite_failed` for every later call with the same query.
    _cache[key] = rewritten
    return rewritten, False


if __name__ == "__main__":
    calls: list[dict] = []

    def fake_complete(prompt: str, **kw):
        calls.append({"prompt": prompt, **kw})
        return {"query": "cascaded evaluation, cheap proxy pre-filter, surrogate fitness"}

    llm.complete = fake_complete            # offline: no server is touched
    q1 = "how do I speed up the evaluation step of my evolutionary loop"
    assert rewrite(q1) == ("cascaded evaluation, cheap proxy pre-filter, surrogate fitness", False)
    assert calls[-1]["max_tokens"] == 200 and calls[-1]["timeout"] == 20.0
    assert calls[-1]["temperature"] == 0.0 and calls[-1]["model"] == llm.QWEN_9B
    assert calls[-1]["system"].strip(), "prompt file empty"      # real prompts/rewrite/system.txt

    assert rewrite(q1)[1] is False
    assert len(calls) == 1, "second call with the same query must hit the cache"
    assert rewrite(q1, budget=1000)[0] == _cache[(text_hash(q1), 200)]
    assert len(calls) == 1, "budget above the §8 ceiling must not change the cache key"

    q2 = "my agent keeps forgetting what it did earlier"
    assert rewrite(q2, budget=64)[1] is False
    assert calls[-1]["max_tokens"] == 64, calls[-1]["max_tokens"]

    def boom(prompt: str, **kw):
        raise llm.LLMError("rewrite: 2 attempts failed: TimeoutError")

    llm.complete = boom
    q3 = "island model migration schedule"
    assert rewrite(q3) == (q3, True), "a dead 9B must degrade to the raw query, not raise"
    llm.complete = fake_complete
    assert rewrite(q3)[1] is False, "a failure must not be cached"

    llm.complete = lambda prompt, **kw: {"query": "   "}
    q4 = "what beats random search on discrete program spaces"
    assert rewrite(q4) == (q4, True), "an empty rewrite is a failure, not a query"

    for bad in (lambda: rewrite("   "), lambda: rewrite("q", budget=0)):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("bad input must raise, not degrade silently")

    print(f"ok: rewrite degrades on LLMError, caches only successes, "
          f"{len(calls)} calls for 6 rewrites")
