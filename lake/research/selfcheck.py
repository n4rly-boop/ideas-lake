"""Offline checks for the reusable Ideas Lake research agent."""

from __future__ import annotations

import asyncio

import httpx

from .agent import DeepResearchAgent, ResearchError, _safe_queries
from .models import ResearchRequest, ResearchSource
from .web import ResearchSearchError, SelfHostedResearchClient, WebHit


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


def main() -> None:
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

    print("research selfcheck OK — bounded planning, independent web lookup, degradation, and 503")


if __name__ == "__main__":
    main()
