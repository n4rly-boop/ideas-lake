"""Wire and internal records for the Ideas Lake research agent."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _http_url(value: str) -> str:
    value = value.strip()
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("source URL must be an absolute HTTP(S) URL")
    return value


class ResearchSource(BaseModel):
    """A bounded, independently fetched source record in a report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=500)
    url: str
    excerpt: str = Field(min_length=1, max_length=8_000)
    origin: Literal["web"] = "web"

    _url = field_validator("url")(_http_url)


class ResearchIngest(BaseModel):
    """One `/fetch`-shaped queue attempt for a source found during research.

    Only arXiv sources reach here (`/fetch`'s own door, §2.1 of the spec): `url` is
    the original hit url as found, never the Docling-normalized PDF url — it is the
    address research actually read, reported back unchanged. It does NOT predict the
    graph node: the fetch worker reads only `args["arxiv_id"]` (`api/workers.py:176`)
    and `Source.id` hashes the arXiv API's own versioned abs url
    (`ingest/fetch.py:423`), which this url need not equal. Use `arxiv_id` to correlate
    with the graph, not `url`. `status` mirrors the
    queue row's status on success (`queued`/`running`/`staged`/…) and one of
    `queue_full` / `conflict` / `error` on a refusal — never silently absent, so a
    caller can tell "not sent, not arXiv" (missing from this list) from "sent, and
    it failed" (present, with a status that says how).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str
    arxiv_id: str = Field(min_length=1, max_length=64)
    job_id: str | None = None
    status: str = Field(min_length=1, max_length=32)

    _url = field_validator("url")(_http_url)


class ResearchRequest(BaseModel):
    """Natural-language mission accepted by the Lake research service."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str = Field(min_length=1, max_length=4_000)
    context: str = Field(default="", max_length=12_000)
    known_ideas: list[str] = Field(default_factory=list, max_length=100)
    directions: list[str] = Field(default_factory=list, max_length=8)
    max_queries: int = Field(default=3, ge=1, le=5)
    max_sources: int = Field(default=8, ge=1, le=12)
    rag_k: int = Field(default=5, ge=1, le=10)
    run_id: str | None = Field(default=None, max_length=200)

    @field_validator("query", "context")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("known_ideas", "directions")
    @classmethod
    def _clean_lines(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            item = " ".join(str(value).split())[:800]
            if item and item not in cleaned:
                cleaned.append(item)
        return cleaned

    @model_validator(mode="after")
    def _non_blank_query(self) -> "ResearchRequest":
        if not self.query:
            raise ValueError("query must not be blank")
        return self


class ResearchCost(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    wall_ms: float = Field(ge=0)


class ResearchResponse(BaseModel):
    """Language report plus provenance and explicit degradation diagnostics."""

    model_config = ConfigDict(extra="forbid")

    report: str = Field(min_length=1, max_length=50_000)
    queries: list[str] = Field(max_length=5)
    sources: list[ResearchSource] = Field(max_length=12)
    # default_factory, not a bare default: every existing call that built a
    # ResearchResponse before ingest existed omits this field, and a mutable-looking
    # default without the factory would be one shared list across every response.
    ingested: list[ResearchIngest] = Field(default_factory=list, max_length=12)
    rag_status: Literal["ok", "empty", "degraded", "disabled"]
    rag_log_id: str | None = None
    rag_ideas: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list, max_length=12)
    cost: ResearchCost
