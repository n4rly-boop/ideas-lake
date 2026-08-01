"""Offline checks for the reusable Ideas Lake research agent."""

from __future__ import annotations

import asyncio

import httpx

from .. import queue
from .agent import (
    GRAMMAR_MAX_LENGTH, PLAN_SCHEMA, SYNTHESIS_SCHEMA, DeepResearchAgent,
    ResearchError, _safe_queries, build_research_prompt,
)
from .models import ResearchRequest, ResearchSource
from .web import ResearchSearchError, SelfHostedResearchClient, WebHit


# An arXiv url whose version digits push `arxiv_id` past `ResearchIngest`'s
# max_length=64. `_ARXIV_ID` (`ingest/fetch.py:54`) accepts it; the model does not.
_OVERLONG_URL = "https://arxiv.org/abs/2406.04824v" + "9" * 60


class FakeSearch:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.queries: list[str] = []

    async def search(self, query: str, *, max_results: int):
        self.queries.append(query)
        if self.fail:
            raise ResearchSearchError("offline search failure")
        return [WebHit("Primary paper", "https://arxiv.org/abs/2401.00001", "paper snippet", 1.0)]

    async def extract(self, hits, *, max_chars: int):
        if self.fail:
            raise ResearchSearchError("offline extraction failure")
        return [ResearchSource(
            source_id="web_primary", title="Primary paper",
            url="https://arxiv.org/abs/2401.00001",
            excerpt="The paper compares two independent mechanisms under a bounded budget.",
        )]


class EmptySearch:
    """A web client that reaches the network cleanly and finds nothing.

    Distinct from `FakeSearch(fail=True)`: that one raises, so `_discover`/`_extract`
    log a `*_failed:` warning and `rag_status` never enters the picture. This one
    returns `[]` with no exception at all — the guard in 1.4 has to 503 on this
    combination (empty web AND non-"ok" RAG) purely from `rag_status`, because
    nothing else in the round is shouting "something broke".
    """

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(self, query: str, *, max_results: int):
        self.queries.append(query)
        return []

    async def extract(self, hits, *, max_chars: int):
        return []


class NonArxivSearch:
    """A web client whose only hit is outside `/fetch`'s contract (§2.1)."""

    async def search(self, query: str, *, max_results: int):
        return [WebHit("Blog post", "https://example.com/post", "snippet", 1.0)]

    async def extract(self, hits, *, max_chars: int):
        return [ResearchSource(
            source_id="web_blog", title="Blog post", url="https://example.com/post",
            excerpt="A blog post mentioning mechanisms, not a paper worth ingesting.",
        )]


class HitNoExtractSearch:
    """SearXNG up, Docling down: a real hit, then extraction yields nothing.

    Distinct from `EmptySearch` (zero hits, no exception) and `FakeSearch(fail=True)`
    (search itself raises): here `search` succeeds with a hit, and `extract` either
    returns `[]` cleanly or raises. Both are the shape that shipped a blocker — a
    hit without a source is not evidence, and the round must not read as a 200 about
    nothing just because a url was found.
    """

    def __init__(self, *, extract_raises: bool = False) -> None:
        self.extract_raises = extract_raises

    async def search(self, query: str, *, max_results: int):
        return [WebHit("Primary paper", "https://arxiv.org/abs/2401.00001", "paper snippet", 1.0)]

    async def extract(self, hits, *, max_chars: int):
        if self.extract_raises:
            raise ResearchSearchError("offline extraction failure")
        return []


class RecordingModel:
    """Wraps `_model` and records every `op` it is called with, in order.

    Needed because a graph-only round asserting `queries == []` only proves the
    *result* stayed empty — it does not prove `_plan` (and its `research_plan` model
    call) was never invoked at all. A call site bug that ran `_plan` and then
    discarded the result would still pass that weaker check.
    """

    def __init__(self) -> None:
        self.ops: list[str] = []
        self.prompts: dict[str, str] = {}

    def __call__(self, prompt: str, *, op: str, **kwargs):
        self.ops.append(op)
        self.prompts[op] = prompt
        return _model(prompt, op=op, **kwargs)


def _model(prompt: str, *, op: str, **kwargs):
    if op == "research_plan":
        return {"queries": ["primary research alternative mechanism", "failure modes implementation"]}
    if op == "research_synthesis":
        return {
            "summary": "The source compares two mechanisms and reports their operating context.",
            "directions": ["Compare the mechanisms under the stated budget."],
            "gaps": ["The source does not establish transfer to the current task."],
            "source_ids": ["web_primary"],
        }
    raise AssertionError(op)


def _max_lengths(node) -> list[int]:
    """Every `maxLength` anywhere in a schema, however deeply nested."""
    found: list[int] = []
    if isinstance(node, dict):
        if isinstance(node.get("maxLength"), int):
            found.append(node["maxLength"])
        for value in node.values():
            found.extend(_max_lengths(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_max_lengths(value))
    return found


def main() -> None:
    # Offline guard for an online failure that cost a whole prod round every time:
    # llama.cpp expands a bounded-length string into explicit grammar repetitions and
    # answers `400 failed to parse grammar` past a certain size. `SYNTHESIS_SCHEMA`
    # shipped with `summary: maxLength 7000`, so every prod round answered 200 with the
    # fallback report and `synthesis_failed:LLMError` — a schema this server cannot
    # compile is not a style question. Nothing here talks to a model, so this is the
    # only place the ceiling can be enforced without a live server.
    for name, schema in (("PLAN_SCHEMA", PLAN_SCHEMA), ("SYNTHESIS_SCHEMA", SYNTHESIS_SCHEMA)):
        for limit in _max_lengths(schema):
            assert limit <= GRAMMAR_MAX_LENGTH, (
                f"{name}: maxLength={limit} exceeds the {GRAMMAR_MAX_LENGTH} llama.cpp "
                "will compile; the server answers 400 and the step silently degrades"
            )

    try:
        ResearchRequest(query=" ")
    except ValueError:
        pass
    else:
        raise AssertionError("blank research queries must be rejected")

    def web_transport(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/search":
            return httpx.Response(200, json={"results": [{
                "title": "Primary paper", "url": "https://arxiv.org/abs/2401.00001",
                "content": "snippet", "score": 1.0,
            }]})
        if request.method == "POST" and request.url.path == "/v1/convert/source":
            return httpx.Response(200, json={"document": {"md_content": "full paper text"}})
        if request.method == "POST" and request.url.path == "/crawl":
            return httpx.Response(200, json={"results": [{"success": True, "markdown": "page"}]})
        return httpx.Response(404)

    web = SelfHostedResearchClient(
        transport=httpx.MockTransport(web_transport), timeout_s=2,
    )
    hits = asyncio.run(web.search("test", max_results=2))
    sources = asyncio.run(web.extract(hits, max_chars=1000))
    assert len(hits) == len(sources) == 1 and sources[0].excerpt == "full paper text"

    request = ResearchRequest(
        query="maintain diversity in evolutionary search",
        context="Prompt optimization with a bounded evaluation budget.",
        known_ideas=["periodic migration between islands"],
        directions=["alternative mechanisms", "failure modes"],
        run_id="selfcheck-research",
    )
    queries = _safe_queries(
        [
            "periodic migration between islands",  # known inventory: rejected
            "primary research alternative mechanism",
            "primary research alternative mechanism",  # duplicate: rejected
            "api_key=do-not-search-this",  # secret: rejected
        ],
        request,
    )
    assert queries == ["primary research alternative mechanism"], queries

    search = FakeSearch()
    agent = DeepResearchAgent(
        search_client=search,
        model_complete=_model,
        rag_retrieve=lambda *args, **kwargs: (200, {
            "log_id": "rag-1",
            "ideas": [{"text": "known prior", "applicability_conditions": "bounded budget"}],
        }),
    )
    response = asyncio.run(agent.research(request))
    assert response.rag_status == "ok" and response.rag_log_id == "rag-1"
    assert response.rag_ideas == 1 and response.sources[0].url.startswith("https://")
    assert "source-grounded" not in response.report.lower()  # report is concrete, not a refusal
    assert "https://arxiv.org/abs/2401.00001" in response.report
    assert search.queries, "the agent must independently query the web even with RAG priors"

    degraded = DeepResearchAgent(
        search_client=FakeSearch(), model_complete=_model,
        rag_retrieve=lambda *args, **kwargs: (503, {"log_id": "rag-down"}),
    )
    degraded_response = asyncio.run(degraded.research(request))
    assert degraded_response.rag_status == "degraded"
    assert any(item.startswith("rag_failed:") for item in degraded_response.warnings)

    broken = DeepResearchAgent(
        search_client=FakeSearch(fail=True), model_complete=_model,
        rag_retrieve=lambda *args, **kwargs: (503, {"log_id": "rag-down"}),
    )
    try:
        asyncio.run(broken.research(request))
    except ResearchError:
        pass
    else:
        raise AssertionError("broken RAG plus broken web must not look like an empty success")

    # ------------------------------------------------------------- graph-only mode

    # 1. graph-only, live RAG: `search_client=None` must never touch the web at
    # all — no queries, no hits, no sources — and still produce a real 200 report
    # from Lake priors alone, with the mode itself visible in `warnings` rather
    # than left for a caller to infer from an empty `sources` list.
    graph_only_model = RecordingModel()
    graph_only = DeepResearchAgent(
        search_client=None, model_complete=graph_only_model,
        rag_retrieve=lambda *args, **kwargs: (200, {
            "log_id": "rag-graph-only",
            "ideas": [{"text": "prior idea", "applicability_conditions": "bounded budget"}],
        }),
    )
    graph_only_response = asyncio.run(graph_only.research(request))
    assert graph_only_response.sources == [] and graph_only_response.ingested == []
    assert "web_disabled:graph_only_mode" in graph_only_response.warnings
    assert graph_only_response.report.strip()
    # graph-only must not plan queries at all — not just "the response's `queries`
    # ended up empty" (that could also mean planning ran and its result got
    # dropped), but proven from the recording wrapper: `research_plan` never
    # appears among the ops the round actually called the model with.
    assert graph_only_response.queries == []
    assert "research_plan" not in graph_only_model.ops, graph_only_model.ops

    # The prompt the round actually sent, not the one a direct `build_research_prompt`
    # call would produce: the call site inside `research()` is free to pass the wrong
    # `web_enabled` and stay green if nothing captures what it really sent. Recorded
    # from the same graph-only round above.
    graph_only_synthesis_prompt = graph_only_model.prompts["research_synthesis"]
    assert "supplied web sources" not in graph_only_synthesis_prompt
    assert "LAKE_RESEARCH_WEB=0" in graph_only_synthesis_prompt
    assert "leave source_ids empty" in graph_only_synthesis_prompt
    assert "IDs of sources actually used" not in graph_only_synthesis_prompt

    # 2. graph-only, dead RAG: with the web off, RAG is the only channel left: if
    # it also fails there is zero evidence of any kind, and 1.4's guard has to 503
    # rather than let a graph-only deploy answer 200 from nothing.
    graph_only_dead_rag = DeepResearchAgent(
        search_client=None, model_complete=_model,
        rag_retrieve=lambda *args, **kwargs: (503, {"log_id": "rag-graph-only-down"}),
    )
    try:
        asyncio.run(graph_only_dead_rag.research(request))
    except ResearchError:
        pass
    else:
        raise AssertionError("graph-only mode with a dead RAG must not look like an empty success")

    # 3. web enabled, RAG answers cleanly but has nothing, and the web returns zero
    # hits with no exception at all. Before 1.4 this was `rag_status == "empty"`
    # with no failed channel to blame, so the old `web_failed`-only guard let it
    # through as a confident 200 about nothing; `!= "ok"` catches "empty" too.
    empty_everything = DeepResearchAgent(
        search_client=EmptySearch(), model_complete=_model,
        rag_retrieve=lambda *args, **kwargs: (200, {"log_id": "rag-empty", "ideas": []}),
    )
    try:
        asyncio.run(empty_everything.research(request))
    except ResearchError:
        pass
    else:
        raise AssertionError("empty web plus empty RAG must not look like an empty success")

    # 3b. the one that hid a shipped blocker: SearXNG up, Docling down. `search`
    # cleanly returns a hit; `extract` yields nothing (or raises). A hit is a url,
    # not evidence — only `sources` reaches the synthesis prompt — so this must
    # 503 exactly like "no hits at all" does, not ship a confident 200 built from
    # urls nothing could read. Both extract failure modes (empty return, exception)
    # are checked, paired with an empty graph so no channel has evidence.
    hit_no_extract_empty = DeepResearchAgent(
        search_client=HitNoExtractSearch(extract_raises=False), model_complete=_model,
        rag_retrieve=lambda *args, **kwargs: (200, {"log_id": "rag-hit-no-extract", "ideas": []}),
    )
    try:
        asyncio.run(hit_no_extract_empty.research(request))
    except ResearchError:
        pass
    else:
        raise AssertionError("a hit with nothing extracted must not look like an empty success")

    hit_no_extract_raises = DeepResearchAgent(
        search_client=HitNoExtractSearch(extract_raises=True), model_complete=_model,
        rag_retrieve=lambda *args, **kwargs: (200, {"log_id": "rag-hit-extract-raises", "ideas": []}),
    )
    try:
        asyncio.run(hit_no_extract_raises.research(request))
    except ResearchError:
        pass
    else:
        raise AssertionError("a hit whose extraction raises must not look like an empty success")

    # ------------------------------------------------------------------- ingest

    # 4. an extracted arXiv source is queued through the injected `ingest`
    # callable, and the queue row's own `id`/`status` flow straight into the
    # response rather than being reinvented here.
    ingest_calls: list[tuple[str, str]] = []

    def _ingest_accepts(url: str, arxiv_id: str) -> dict:
        ingest_calls.append((url, arxiv_id))
        return {"id": "job_abc123", "status": "running"}

    ingest_ok = DeepResearchAgent(
        search_client=FakeSearch(), model_complete=_model,
        rag_retrieve=lambda *args, **kwargs: (200, {"log_id": "rag-ingest-ok", "ideas": []}),
        ingest=_ingest_accepts,
    )
    ingest_ok_response = asyncio.run(ingest_ok.research(request))
    assert ingest_calls == [("https://arxiv.org/abs/2401.00001", "2401.00001")]
    assert len(ingest_ok_response.ingested) == 1
    ingested_item = ingest_ok_response.ingested[0]
    assert ingested_item.arxiv_id == "2401.00001"
    assert ingested_item.job_id == "job_abc123"
    assert ingested_item.status == "running"
    # The original hit url, not a Docling-normalized one: `Source.id = sha1(url +
    # version)` on the graph side has to match what a caller re-posting this exact
    # url to `/fetch` would compute (models.py docstring on `ResearchIngest`).
    assert ingested_item.url == "https://arxiv.org/abs/2401.00001"

    # 5. a non-arXiv source is outside `/fetch`'s contract entirely: it must never
    # reach the injected `ingest` callable, and must not appear in `ingested`
    # under any status — the gap between `len(sources)` and `len(ingested)` is
    # how a caller tells "not sent, not arXiv" from "sent, and it failed".
    non_arxiv_calls: list[tuple[str, str]] = []

    def _ingest_must_not_run(url: str, arxiv_id: str) -> dict:
        non_arxiv_calls.append((url, arxiv_id))
        return {"id": "job_should_not_exist", "status": "queued"}

    non_arxiv = DeepResearchAgent(
        search_client=NonArxivSearch(), model_complete=_model,
        rag_retrieve=lambda *args, **kwargs: (200, {
            "log_id": "rag-non-arxiv",
            "ideas": [{"text": "prior idea", "applicability_conditions": "bounded budget"}],
        }),
        ingest=_ingest_must_not_run,
    )
    non_arxiv_response = asyncio.run(non_arxiv.research(request))
    assert non_arxiv_response.ingested == []
    assert not non_arxiv_calls, "a non-arXiv source must never reach the ingest callable"

    # 6. the queue at its ceiling must not abort the round: `queue.Full` (the real
    # class production catches, not a look-alike) is caught, the refusal is
    # recorded in both `ingested[].status` and `warnings`, and the report still
    # ships. A fake exception class here would leave this green even if the
    # production `except queue.Full:` stopped matching what the real queue raises.
    def _ingest_queue_full(url: str, arxiv_id: str) -> dict:
        raise queue.Full("2 jobs already queued or running (ceiling 2)")

    queue_full = DeepResearchAgent(
        search_client=FakeSearch(), model_complete=_model,
        rag_retrieve=lambda *args, **kwargs: (200, {
            "log_id": "rag-queue-full",
            "ideas": [{"text": "prior idea", "applicability_conditions": "bounded budget"}],
        }),
        ingest=_ingest_queue_full,
    )
    queue_full_response = asyncio.run(queue_full.research(request))  # must not raise
    assert len(queue_full_response.ingested) == 1
    queue_full_item = queue_full_response.ingested[0]
    assert queue_full_item.status == "queue_full" and queue_full_item.job_id is None
    # Exact, not `startswith` alone: the warning is now COUNTED
    # (`ingest_queue_full:<count>`), and with one refused source the count must read
    # 1 — `startswith("ingest_queue_full:")` alone would still pass on a wrong count
    # (e.g. `:0`) or on a format that dropped the count entirely.
    assert "ingest_queue_full:1" in queue_full_response.warnings

    # 6b. `queue.DedupConflict` (a live job already owns this dedup key with a
    # different payload) is the queue's OTHER refusal, and had no coverage at all:
    # a caller might catch `queue.Full` only and let `DedupConflict` propagate as an
    # unhandled 500 instead of a status the caller can read.
    def _ingest_dedup_conflict(url: str, arxiv_id: str) -> dict:
        raise queue.DedupConflict("live job owns 2401.00001 with a different payload_hash")

    dedup_conflict = DeepResearchAgent(
        search_client=FakeSearch(), model_complete=_model,
        rag_retrieve=lambda *args, **kwargs: (200, {
            "log_id": "rag-dedup-conflict",
            "ideas": [{"text": "prior idea", "applicability_conditions": "bounded budget"}],
        }),
        ingest=_ingest_dedup_conflict,
    )
    dedup_conflict_response = asyncio.run(dedup_conflict.research(request))  # must not raise
    assert len(dedup_conflict_response.ingested) == 1
    dedup_conflict_item = dedup_conflict_response.ingested[0]
    assert dedup_conflict_item.status == "conflict" and dedup_conflict_item.job_id is None
    assert any(w.startswith("ingest_conflict:") for w in dedup_conflict_response.warnings)

    # 6c. a generic ingest failure (sqlite gone bad, disk full, anything neither
    # `queue.Full` nor `queue.DedupConflict`) is the catch-all `except Exception`
    # branch, also uncovered: it must still degrade to a status, not an unhandled
    # exception that takes the whole round down with it.
    def _ingest_raises_runtime_error(url: str, arxiv_id: str) -> dict:
        raise RuntimeError("sqlite disk I/O error")

    ingest_error = DeepResearchAgent(
        search_client=FakeSearch(), model_complete=_model,
        rag_retrieve=lambda *args, **kwargs: (200, {
            "log_id": "rag-ingest-error",
            "ideas": [{"text": "prior idea", "applicability_conditions": "bounded budget"}],
        }),
        ingest=_ingest_raises_runtime_error,
    )
    ingest_error_response = asyncio.run(ingest_error.research(request))  # must not raise
    assert len(ingest_error_response.ingested) == 1
    ingest_error_item = ingest_error_response.ingested[0]
    assert ingest_error_item.status == "error" and ingest_error_item.job_id is None
    assert "ingest_failed:RuntimeError:1" in ingest_error_response.warnings

    # 6c. an arXiv id longer than `ResearchIngest.arxiv_id` allows must be refused
    # BEFORE the record is built. `_ARXIV_ID` leaves the version digits unbounded
    # (`ingest/fetch.py:54`), so this url parses cleanly and then blows up inside the
    # model — a ValidationError raised while constructing the response, which no
    # `except` around `enqueue` sees and which the route can only answer as an
    # undocumented 500. The round must survive it as a counted refusal instead.
    class _OverlongArxivSearch:
        async def search(self, query: str, *, max_results: int):
            return [WebHit("Malformed link", _OVERLONG_URL, "snippet", 1.0)]

        async def extract(self, hits, *, max_chars: int):
            return [ResearchSource(
                source_id="web_overlong", title="Malformed link", url=_OVERLONG_URL,
                excerpt="Parses as an arXiv id, but the version digits are unbounded.",
            )]

    overlong_calls: list[str] = []
    overlong = DeepResearchAgent(
        search_client=_OverlongArxivSearch(), model_complete=_model,
        rag_retrieve=lambda *args, **kwargs: (200, {
            "log_id": "rag-overlong",
            "ideas": [{"text": "prior idea", "applicability_conditions": "bounded budget"}],
        }),
        ingest=lambda url, arxiv_id: overlong_calls.append(arxiv_id) or {"id": "x", "status": "queued"},
    )
    overlong_response = asyncio.run(overlong.research(request))  # must not raise
    assert overlong_calls == [], "an id past max_length must never reach the queue"
    assert overlong_response.ingested == []
    assert "ingest_bad_arxiv_id:1" in overlong_response.warnings

    # 7. graph-only mode must not leave the planning/synthesis prompt asking for
    # something the round cannot deliver: an instruction to ground every claim in
    # "the supplied web sources" when none were ever fetched is exactly what makes
    # a 9B model invent a citation to comply (§1.5).
    no_web_prompt = build_research_prompt(request, "", web_enabled=False)
    assert "supplied web sources" not in no_web_prompt
    assert "LAKE_RESEARCH_WEB=0" in no_web_prompt

    print("research selfcheck OK — bounded planning, independent web lookup, degradation, "
          "graph-only mode, ingest handoff, and 503")


if __name__ == "__main__":
    main()
