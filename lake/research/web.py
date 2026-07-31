"""Self-hosted web discovery and evidence extraction for deep research.

SearXNG discovers sources; Crawl4AI reads ordinary pages; Docling reads PDFs.
The services are intentionally injected/configured at the boundary so tests can
use a fake client and the Lake remains independent of GigaEvo's runtime.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .models import ResearchSource


class ResearchSearchError(RuntimeError):
    """A discovery or evidence-service failure."""


class WebHit:
    """Small internal discovery record; snippets never become proof silently."""

    __slots__ = ("title", "url", "snippet", "score")

    def __init__(self, title: str, url: str, snippet: str, score: float) -> None:
        self.title, self.url, self.snippet, self.score = title, url, snippet, score


def is_public_source_url(value: str) -> bool:
    """Reject credentials, local addresses, and unusual ports before fetching."""

    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").casefold().rstrip(".")
        port = parts.port
    except ValueError:
        return False
    if (
        parts.scheme not in {"http", "https"}
        or not host
        or parts.username is not None
        or parts.password is not None
        or (port is not None and port not in {80, 443})
        or host in {"localhost", "metadata.google.internal"}
        or host.endswith((".localhost", ".local", ".internal"))
    ):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def _service_url(value: str) -> str:
    parts = urlsplit(value.strip())
    host = (parts.hostname or "").casefold()
    if parts.scheme not in {"http", "https"} or not host or parts.query or parts.fragment:
        raise ValueError("research service URL must be an absolute HTTP(S) URL")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _canonical(value: str) -> str:
    parts = urlsplit(value)
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(),
                       parts.path.rstrip("/") or "/", parts.query, ""))


def _source_id(url: str) -> str:
    return "web_" + hashlib.sha1(_canonical(url).encode("utf-8")).hexdigest()[:16]


def _paper_url(value: str) -> str:
    parts = urlsplit(value)
    host = (parts.hostname or "").casefold()
    if host in {"arxiv.org", "www.arxiv.org"} and parts.path.startswith("/abs/"):
        paper_id = parts.path.removeprefix("/abs/").strip("/")
        if paper_id:
            return f"https://arxiv.org/pdf/{paper_id}.pdf"
    return value


def _is_paper(value: str) -> bool:
    parts = urlsplit(value)
    host = (parts.hostname or "").casefold()
    return PurePosixPath(parts.path.casefold()).suffix == ".pdf" or "/pdf/" in parts.path.casefold() or host in {"arxiv.org", "www.arxiv.org"}


def _crawl_markdown(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    results = payload.get("results")
    if not isinstance(results, list):
        nested = payload.get("result")
        results = nested.get("results") if isinstance(nested, dict) else None
    if not isinstance(results, list):
        return ""
    for result in results:
        if not isinstance(result, dict) or result.get("success") is False:
            continue
        markdown = result.get("markdown")
        if isinstance(markdown, str) and markdown.strip():
            return markdown
        if isinstance(markdown, dict):
            for key in ("fit_markdown", "raw_markdown", "markdown_with_citations"):
                value = markdown.get(key)
                if isinstance(value, str) and value.strip():
                    return value
    return ""


def _docling_markdown(payload: Any) -> str:
    document = payload.get("document") if isinstance(payload, dict) else None
    if not isinstance(document, dict):
        return ""
    for key in ("md_content", "text_content"):
        value = document.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


class SelfHostedResearchClient:
    """Bounded SearXNG + Crawl4AI + Docling client."""

    def __init__(
        self,
        *,
        searxng_url: str = "http://127.0.0.1:8080",
        crawl4ai_url: str = "http://127.0.0.1:11235",
        docling_url: str = "http://127.0.0.1:5001",
        timeout_s: float = 30.0,
        max_fetch_concurrency: int = 4,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._searxng = _service_url(searxng_url)
        self._crawl4ai = _service_url(crawl4ai_url)
        self._docling_url = _service_url(docling_url)
        self._timeout = max(1.0, float(timeout_s))
        self._concurrency = max(1, min(int(max_fetch_concurrency), 8))
        self._transport = transport

    async def search(self, query: str, *, max_results: int) -> list[WebHit]:
        limit = max(1, min(int(max_results), 10))
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                response = await client.get(
                    f"{self._searxng}/search",
                    params={"q": query[:400], "format": "json"},
                    headers={"Accept": "application/json"},
                )
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            raise ResearchSearchError(f"SearXNG discovery failed: {type(exc).__name__}") from exc
        raw = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            raise ResearchSearchError("SearXNG response has no results list")
        hits: list[WebHit] = []
        seen: set[str] = set()
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            url, title = str(item.get("url") or "").strip(), str(item.get("title") or "").strip()
            if not title or not is_public_source_url(url) or _canonical(url) in seen:
                continue
            seen.add(_canonical(url))
            score = item.get("score")
            hits.append(WebHit(title, url, str(item.get("content") or "")[:2_000],
                               float(score) if isinstance(score, (int, float)) else 1.0 / (index + 1)))
            if len(hits) >= limit:
                break
        return hits

    async def extract(self, hits: list[WebHit], *, max_chars: int) -> list[ResearchSource]:
        if not hits:
            return []
        limit = max(500, min(int(max_chars), 12_000))
        semaphore = asyncio.Semaphore(self._concurrency)
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            async def one(hit: WebHit) -> ResearchSource | None:
                async with semaphore:
                    content = ""
                    if _is_paper(hit.url):
                        content = await self._docling_extract(client, _paper_url(hit.url))
                    if not content:
                        content = await self._crawl(client, hit.url)
                    if not content.strip():
                        return None
                    return ResearchSource(source_id=_source_id(hit.url), title=hit.title,
                                          url=hit.url, excerpt=content[:limit].strip())
            outputs = await asyncio.gather(*(one(hit) for hit in hits), return_exceptions=True)
        sources: list[ResearchSource] = []
        seen: set[str] = set()
        for hit, output in zip(hits, outputs, strict=True):
            if isinstance(output, ResearchSource) and _canonical(output.url) not in seen:
                sources.append(output)
                seen.add(_canonical(output.url))
        return sources

    async def _crawl(self, client: httpx.AsyncClient, url: str) -> str:
        try:
            response = await client.post(f"{self._crawl4ai}/crawl", json={"urls": [url]})
            response.raise_for_status()
            return _crawl_markdown(response.json())
        except Exception:
            return ""

    async def _docling_extract(self, client: httpx.AsyncClient, url: str) -> str:
        try:
            response = await client.post(
                f"{self._docling_url}/v1/convert/source",
                json={"sources": [{"kind": "http", "url": url}],
                      "options": {"to_formats": ["md"], "do_ocr": False,
                                  "abort_on_error": False}},
            )
            response.raise_for_status()
            return _docling_markdown(response.json())
        except Exception:
            return ""
