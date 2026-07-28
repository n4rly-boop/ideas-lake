"""Request/response models of the HTTP layer (spec 10 §5.4 for /retrieve, §3.4/§4.7
for the rest).

These are the wire contract, deliberately separate from `lake.models`: those are
the records the store holds, these are what a caller may send and will receive.
`vector` is the clearest case — every record carries one, no response should.

`extra="forbid"` on every request model: a misspelled field must be a 400, not a
silently ignored default. Response models list their fields explicitly so that a
column added to the store does not leak out of the API unannounced.
"""
from typing import ClassVar, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..models import EMBED_DIM

MAX_QUERY_CHARS = 4000      # a query is a sentence; anything larger is not a query
MAX_K = 50                  # k is a page size, not a dump switch
MAX_PAGE = 200              # ceiling on limit= for every listing

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """One page plus the total, so a caller can tell "the end" from "the ceiling"."""
    total: int
    limit: int
    offset: int
    items: list[T]


class ErrorResponse(BaseModel):
    error: str
    log_id: str | None = None


# ------------------------------------------------------------------------ graph

class SourceOut(BaseModel):
    id: str
    url: str
    title: str
    type: str
    version: str
    retrieved_at: str
    run_success: bool | None = None
    run_meta: dict | None = None


class SourceIn(BaseModel):
    """Upsert of a Source. This is how block C reports a run back (§1.1, `stub_store`
    comment on `write_source`): the same (url, version) yields the same id, so the
    row is replaced and never duplicated."""
    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    type: Literal["paper", "doc", "run"]
    version: str = Field("v1", min_length=1)
    retrieved_at: str | None = Field(None, description="ISO-8601 UTC; now() if omitted.")
    run_success: bool | None = Field(None, description="type=run: did the run succeed.")
    run_meta: dict | None = Field(None, description="type=run: fitness_delta and friends.")


class ThesisOut(BaseModel):
    """A leaf as the store holds it, joined to its source (§3.4). `vector` is not
    here on purpose: leaf vectors are the index's business (§3.5)."""
    id: str
    source_id: str
    idea_id: str
    text: str
    context: str
    effect: str
    locator: str
    text_hash: str
    created_at: str
    source_type: str
    source_url: str
    source_title: str
    run_success: bool | None = None
    run_meta: dict | None = None


class IdeaOut(BaseModel):
    id: str
    text: str
    applicability_conditions: str
    limitations: str
    failure_modes: list[str]
    differentiation: str | None = None
    effect_claimed: str
    effect_observed: str
    trust_score: float = Field(..., description="Derived from the leaves by the stub store, "
                                                "not stored — see IdeaPatch.")
    dirty: bool
    rederived_at_leaf_count: int
    created_at: str = Field(..., description="ISO-8601, or \"\": timestamps on Idea are "
                                             "block B's columns and the ingest leaves them "
                                             "empty (`link.py`). Do not parse blindly.")
    updated_at: str = Field(..., description="ISO-8601, or \"\" — see created_at. A PATCH "
                                             "sets this one and never invents a created_at.")
    theses: list[ThesisOut]
    vector: list[float] | None = Field(
        None, description=f"{EMBED_DIM} floats, only with include_vector=true.")


class IdeaPatch(BaseModel):
    """Fields of an idea that may be written over HTTP.

    `vector` is absent by design: it is derived from `text` (§1.3), and letting a
    caller set the two independently is exactly how the idea neighbourhood drifts
    away from what the idea says. Patch `text` and the server re-embeds it.
    `id` and `created_at` are absent because they are identity, not content.

    `trust_score` is absent too, and that one is not an oversight: the stub store
    recomputes it from the leaves on every read (`stub_store._stub_trust`), so a
    write would land in the column, answer 200, and never be read again — a
    success that is a no-op. It comes back the day the value is stored rather
    than derived.
    """
    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    applicability_conditions: str | None = None
    limitations: str | None = None
    failure_modes: list[str] | None = None
    differentiation: str | None = None
    effect_claimed: str | None = None
    effect_observed: str | None = None
    dirty: bool | None = Field(None, description="Written by block B.")
    rederived_at_leaf_count: int | None = Field(None, ge=0)

    # `None` is the "not sent" marker for every field above, so only these may be
    # SET to null. Everything else is a non-nullable column: writing NULL there
    # makes the row unserializable, and since the write commits before anything
    # reads it back, one such request would take `/ideas` and `/retrieve` down
    # for the whole lake until somebody fixed it with SQL.
    NULLABLE: ClassVar[tuple[str, ...]] = ("differentiation",)

    @model_validator(mode="after")
    def _check(self):
        if not self.model_fields_set:
            raise ValueError("patch is empty: name at least one field to change")
        nulled = sorted(name for name in self.model_fields_set
                        if name not in self.NULLABLE and getattr(self, name) is None)
        if nulled:
            raise ValueError(f"{', '.join(nulled)}: null is not a value — omit the field "
                             "to leave it unchanged")
        return self


class EdgeOut(BaseModel):
    """`edge` is block B's and empty in the MVP, so these listings answer [] —
    planned degradation, not an error (§3.4, `08:377`)."""
    source_id: str
    target_id: str
    type: str
    note: str | None = None
    weight: float | None = None
    evidence: str | None = None
    hop: int


# ------------------------------------------------------------------ index/search

class SearchHit(BaseModel):
    """One raw index hit (§5.2): BM25 + cosine fused with RRF, no ideas, no LLM.
    `bm25_rank`/`vec_rank` are None when that arm did not return the row — which is
    what tells a dead FTS index apart from a merely worse one."""
    thesis_id: str
    idea_id: str
    score: float
    bm25_rank: int | None = None
    vec_rank: int | None = None


class ReindexResult(BaseModel):
    """§6.19 reconciliation: `index.reset()` + `index_rows(all_theses())`."""
    indexed_before: int
    leaves_in_store: int
    indexed_after: int
    in_sync: bool


# ---------------------------------------------------------------------- retrieve

class RetrieveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, max_length=MAX_QUERY_CHARS,
                       description="Free-text query, phrased as a problem or as a solution.")
    k: int = Field(5, gt=0, le=MAX_K,
                   description="How many ideas to return. Recall-first: the answer is padded "
                               "to k rather than cut by a relevance threshold (§5.5).")
    run_id: str | None = Field(None, description="Caller's run id, echoed into the trace.")
    budget: int | None = Field(None, gt=0,
                               description="max_tokens ceiling for the rewrite step (§5.1).")
    rewrite: bool = Field(True, description="Rewrite the query 'in terms of a solution' first. "
                                            "Off is the ablation arm.")
    allow_web: bool = Field(False, description="Stage III. Accepted so that turning it on later "
                                               "does not break the integration; not implemented.")

    @model_validator(mode="after")
    def _check(self):
        if not self.query.strip():
            raise ValueError("query must not be blank")
        if self.allow_web:
            # Answering as if the web had been searched would be a lie about where
            # the ideas came from, and provenance is the one thing §9 never cuts.
            raise ValueError("allow_web=true is not supported: the web stage (III) is out of "
                             "the MVP; resend with allow_web=false")
        return self


class RetrieveThesis(BaseModel):
    """A leaf inside the /retrieve answer — the provenance the answer must carry
    (ТЗ criterion 4). Narrower than `ThesisOut` on purpose: this shape is §5.4."""
    text: str
    url: str
    title: str
    effect: str
    locator: str


class RetrieveIdea(BaseModel):
    idea_id: str
    text: str
    applicability_conditions: str
    limitations: str
    failure_modes: list[str]
    effect_claimed: str
    effect_observed: str
    trust_score: float
    score: float
    via: str = Field(..., description="thesis | edge | padding — how this idea reached the "
                                      "answer. Without it, 'found' and 'padded' are "
                                      "indistinguishable in the metrics (§5.5).")
    theses: list[RetrieveThesis]


class Cost(BaseModel):
    tokens_in: int
    tokens_out: int
    wall_ms: float


class RetrieveResponse(BaseModel):
    ideas: list[RetrieveIdea]
    log_id: str
    cost: Cost


# ------------------------------------------------------------------------ ingest

class SourceEntry(BaseModel):
    """One `sources.yaml` row. `extra="allow"` — that file also carries corpus-only
    keys (`group`, `html`, `why`, `survey`, `fresh`, `year`) which never reach a
    Source node (`06:237`), and rejecting them would make the two formats diverge.

    What IS checked is whether the entry is fetchable at all, because this is the
    boundary in front of minutes of LLM spend: an entry with neither `arxiv_id` nor
    `url` costs a full job to discover.
    """
    model_config = ConfigDict(extra="allow")

    arxiv_id: str | None = None
    url: str | None = None
    title: str | None = None
    type: Literal["paper", "doc", "run"] | None = None
    skip: str | None = Field(None, description="Reason this row cannot be fetched; "
                                               "fetch.py refuses it explicitly.")

    @model_validator(mode="after")
    def _fetchable(self):
        if self.skip:
            return self
        if not (self.arxiv_id or self.url):
            raise ValueError("entry has neither arxiv_id nor url: nothing to fetch")
        if self.type is None:
            raise ValueError("entry has no type: paper | doc | run")
        return self


class Phase1Request(BaseModel):
    """§4.7 phase 1: fetch -> parse -> generalize -> staging.jsonl. The graph is not
    opened at all, so this is the safe half to trigger over HTTP."""
    model_config = ConfigDict(extra="forbid")

    sources: list[SourceEntry] | None = Field(
        None, description="sources.yaml entries (arxiv_id | url, title, type, ...). "
                          "Omit to use lake/sources.yaml.")
    limit: int | None = Field(None, gt=0, description="First N entries only.")
    workers: int = Field(8, gt=0, le=16, description="Phase 1 is parallel (§4.7).")


class Phase2Request(BaseModel):
    """§4.7 phase 2: staging -> graph + index, strictly sequential, cursor-restartable."""
    model_config = ConfigDict(extra="forbid")

    limit: int | None = Field(None, gt=0,
                              description="First N sources of the remaining staging.")


class JobOut(BaseModel):
    """One ingest run. Jobs live in this process only: a restart loses the history,
    the graph and `staging.cursor` are what actually carry the state (§4.7)."""
    id: str
    kind: Literal["phase1", "phase2", "reindex"]
    status: Literal["running", "ok", "failed"]
    created_at: str
    finished_at: str | None = None
    args: dict = {}
    report: dict | None = Field(None, description="The §4.7 report, on status=ok.")
    error: str | None = Field(None, description="Type and message, on status=failed. "
                                                "Never empty when status=failed.")


class StagingOut(BaseModel):
    """What is waiting between the two phases — the point where acceptance by eye
    happens (§4.7)."""
    lines: int
    cursor: int = Field(..., description="Watermark: lines already ingested by phase 2.")
    pending_lines: int
    sources: list[dict] = Field(..., description="Per source: id, title, lines, ingested.")


class PendingLinkOut(BaseModel):
    """One arbiter refusal (§4.5). The queue existing at all is the fail-closed
    behaviour: a failed arbiter writes here instead of guessing `add` or `new`."""
    ts: str
    run_id: str | None = None
    error: str
    thesis_text: str
    source_id: str
    candidates: int


# ---------------------------------------------------------------------- ops

class Health(BaseModel):
    status: Literal["ok", "degraded"]
    mock: bool
    theses_indexed: int | None = None
    leaves_in_store: int | None = None
    in_sync: bool | None = None
    detail: str | None = None


class Stats(BaseModel):
    sources: int
    ideas: int
    theses: int
    edges: int
    theses_indexed: int
    in_sync: bool
    ideas_without_leaves: list[str] = Field(
        ..., description="Must be empty: IDEA ||--|{ THESIS (`06:85`, §6.17).")
    trust_scale: float
    staging_lines: int
    staging_cursor: int
    pending_link: int
    job_running: str | None = None
