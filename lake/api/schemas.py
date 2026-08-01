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

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from ..models import EMBED_DIM
from ..research.models import ResearchRequest, ResearchResponse

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
    """Upsert of a Source. This is how block C reports a run back (§1.1, `neo4j_store`
    comment on `write_source`): the same (url, version) yields the same id, so the
    row is replaced and never duplicated."""
    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    # Four values, not three (`13` §6). `SourceOut.type` is a bare `str`, so `/sources`
    # and `/retrieve` already hand `"synthesis"` back; leaving it out here made the
    # write side unable to round-trip what the read side returns, and `/openapi.json`
    # still advertised three as the domain — the contract change §6 calls "правка
    # контракта, а не деталь", announced together with `Idea.origin`.
    type: Literal["paper", "doc", "run", "synthesis"]
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
    origin: str = Field("extracted",
                        description="`extracted` — derived from theses of papers or runs; "
                                    "`synthesized` — a hypothesis built out of other ideas "
                                    "and carrying no evidence of its own. An idea with no "
                                    "leaves is legal only in the second case.")
    trust_score: float = Field(..., description="0..1, written by the judge over the idea "
                                                "and its leaves. 0.0 means either "
                                                "\"judged, and there is little to trust\" "
                                                "or \"nothing to judge, no leaves\" — never "
                                                "\"the judge failed\", that leaves the "
                                                "previous value and the idea dirty.")
    dirty: bool = Field(..., description="Leaves changed since the idea was last judged.")
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

    `trust_score` is absent too, and that one is not an oversight either. Since
    `13` §3.3 the value IS stored — but it is written by the judge together with
    `dirty`, in one update (`graph_client.set_trust`), because an idea that is
    clean with a score from before its current leaves is the exact lie the flag
    exists to prevent. A hand-written score over HTTP would break that pair.

    `origin` is absent for the same class of reason: it says where the idea came
    from, which is a fact about its creation, not a field to be edited afterwards.
    """
    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    applicability_conditions: str | None = None
    limitations: str | None = None
    failure_modes: list[str] | None = None
    differentiation: str | None = None
    effect_claimed: str | None = None
    effect_observed: str | None = None
    # `dirty` is gone from the patch on purpose (`13` §3.2). It is raised with the
    # leaves, in their transaction, and lowered only by the judge in the same update
    # that stores the score. A hand-written `dirty: false` would leave an idea clean
    # with a stale score — precisely the state the flag exists to make impossible.
    # Asking for a re-judge is `dirty` going UP, and that happens by writing leaves.
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
    """**[D12, 2026-07-31]** `edge` is now block A's: `write_cocitation_edges` and
    `write_derived_from_edges` write to Neo4j in the ingest/synthesis pipeline itself.
    These listings answer the real edges, not [] (§3.4, `07:C1`)."""
    source_id: str
    target_id: str
    type: str
    note: str | None = None
    weight: float | None = None
    # list[str], not str: co-citation writes the LIST of contributing source ids
    # (`neo4j_store._COCITE_UPSERT`), derived_from now writes a one-element list for
    # the same reason (`neo4j_store.write_derived_from_edges`) — one key, one shape,
    # or `GET /ideas/{id}/neighbors` 500s on FastAPI response validation the moment a
    # real co-citation edge is in the answer (review, 2026-07-31).
    evidence: list[str] | None = None
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


class DialPoint(BaseModel):
    """One indexed thesis around the hypothesis. `cosine` is the real similarity —
    the radius on the dial. `angle` is projected and carries no distance."""
    thesis_id: str
    idea_id: str
    cosine: float
    angle: float = Field(..., description="Радианы, из PCA остатка. Декорация, не расстояние.")


class DialHit(SearchHit):
    """A hit with what the index already holds: the leaf text and its cosine. Both come
    from `idx_thesis`, so the list under the dial reads without touching the graph."""
    text: str
    cosine: float


class DialCosine(BaseModel):
    """Percentiles of the same cosine over the whole index — the rings. Measured per
    call because "far" is a property of this corpus, not a constant."""
    median: float
    p90: float
    p99: float
    max: float


class DialResponse(BaseModel):
    """§5.2 with no ranking and no LLM: an arbitrary phrase against every leaf."""
    query: str
    total: int = Field(..., description="Точек = строк в индексе; расхождение с /healthz видно сразу.")
    points: list[DialPoint]
    hits: list[DialHit] = Field(..., description="То же, что вернул бы GET /search, плюс текст листа: правда, с которой картинка обязана сходиться.")
    cosine: DialCosine
    angle_variance: float = Field(
        ..., description="Доля дисперсии остатка в плоскости угла. На этом озере ~0.1: "
                         "угол почти ничего не несёт, и страница обязана это сказать.")


class ReindexResult(BaseModel):
    """§6.19 reconciliation: `index.reset()` + `index_rows(all_theses())`."""
    indexed_before: int
    leaves_in_store: int
    indexed_after: int
    in_sync: bool


class VaultExportResult(BaseModel):
    """Spec 11: the lake as a folder of markdown notes. `files` counts the README,
    `ideas + theses + sources` does not — the invariant is `.md` minus README == /stats."""
    ideas: int
    theses: int
    sources: int
    orphans: int = Field(..., description="Ideas exported with no leaf — `INVARIANT BROKEN` "
                                          "(`06:85`), marked in the note, not silently dropped.")
    files: int
    dest: str


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
    score: float = Field(..., description="Min-max normalized over THIS call's candidate "
                                          "list (§5.3). Always 1.0 for the best of whatever "
                                          "was found, on a query the lake has nothing on "
                                          "exactly as much as on one it answers well — use "
                                          "it to ORDER this answer's ideas, not to judge "
                                          "whether the answer is any good.")
    cosine_similarity: float = Field(
        ..., description="Review finding, 2026-07-31: cosine similarity between the query "
                         "embedding and this idea's own embedding (`text` -> vector, §1.3), "
                         "in [-1, 1]. NOT renormalized per request, so — unlike `score` and "
                         "`raw_score` — it is comparable across different /retrieve calls: "
                         "this is the signal for 'is this actually relevant, or the best of "
                         "a bad set'. Measured live: ~0.48 for a query the lake has nothing "
                         "on, ~0.75 for one it has a real answer to (`lake/README.md` §8.1). "
                         "Not a probability and not zero-centered on 'unrelated' — general-"
                         "purpose sentence encoders keep a nonzero floor between unrelated "
                         "text, so a caller judging relevance should compare this number "
                         "against its own measured baseline, not against 0.")
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
    # Three values here on purpose, unlike `SourceIn` above: this is a row of things to
    # FETCH, and a synthesis is generated by the lake, never fetched into it (`13` §6).
    # A `type: synthesis` entry in `sources.yaml` would name a file nobody can retrieve.
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


class MutantIn(BaseModel):
    """One mutant of an evolution run (`13` §2.1/§2.5). `program_id` becomes
    `Source.version`; `parent_ids` and `parent_fitness` let the converter compute
    a fitness delta by joining within the batch when `parent_fitness` is omitted.

    Either `mutation_output` (already parsed) or `mutation_output_raw` (the JSON
    string exactly as it sits in the CSV's `metadata_mutation_output` column) must
    be present — the converter parses the raw form, not this route, so a mutant
    with neither is 400 at the door rather than a job that discovers it empty-handed.

    `generation`, `iteration` and `mutation_model` are exactly the three keys
    `runlog.payload_from_csv` emits for every mutant alongside the ones below
    (`10` §2.5 module == HTTP parity). Missing them made the CLI's own payload a
    400 over HTTP, and a caller who stripped them to get past that would have fed
    `from_payload` an empty `mutation_model` and a null `generation` — a run
    ingested through HTTP writing different `Source` rows than the same log through
    the CLI, with no error saying so.
    """
    model_config = ConfigDict(extra="forbid")

    program_id: str = Field(..., min_length=1)
    parent_ids: list[str] = Field(default_factory=list)
    state: str = Field(..., min_length=1)
    generation: int | None = None
    iteration: int | None = None
    fitness: float | None = None
    parent_fitness: float | None = None
    mutation_model: str = ""
    mutation_output: dict | None = None
    mutation_output_raw: str | None = Field(
        None, description="`metadata_mutation_output` as it sits in the CSV — the "
                          "converter parses it, this route does not.")

    @model_validator(mode="after")
    def _check(self):
        if self.mutation_output is None and self.mutation_output_raw is None:
            raise ValueError("mutant needs mutation_output or mutation_output_raw")
        return self


class RunRequest(BaseModel):
    """One evolution run, batched (`13` §2.5): the unit of outcome is the mutant
    (§2.1), but they arrive as a batch, and a job per mutant would put 182 rows
    against a queue ceiling of 100. One job = one batch."""
    model_config = ConfigDict(extra="forbid")

    # A run id is a short slug, not a path: it becomes a filename under `RUN_DIR`
    # (`workers.payload_for`), and `/../../pwned` in a JSON body used to reach the
    # filesystem two directories above it, with `mkdir(parents=True)` happily
    # building the way there. Anchored, so partial matches (`re.search` semantics)
    # cannot slip a "/" in past the end; charset excludes it and ".." outright, a
    # leading dash, and a null byte, and the length ceiling stops an absurd one.
    # `workers.payload_for` repeats the check against the resolved path — defense
    # in depth for any future non-HTTP caller of the same helper (`10 §BLOCKER 1`).
    run_id: str = Field(..., pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$",
                        description="Дедуп-ключ задания, как `arxiv_id` у /fetch. "
                                    "Короткий слаг: [A-Za-z0-9][A-Za-z0-9_.-]{0,63}, "
                                    "не путь.")
    task_id: str | None = None
    mutants: list[MutantIn] = Field(..., min_length=1)
    # §2.4: order is descending |delta|, so a truncated load still keeps the most
    # informative mutants; `limit`/`min_abs_delta` are the two filters that decide
    # WHICH slice survives an interrupted or deliberately partial load. Absent from
    # the wire before this round — `RunRequest` had no field for either, so a
    # caller could never ask for "only the top 50 by |delta|" over HTTP, only the
    # CLI (`runlog.main`) could. Both ride in the SAME body `payload_for` writes to
    # disk, so `_stage_run` reads them back off `payload["limit"]`/
    # `payload["min_abs_delta"]` with no second field to keep in sync.
    limit: int | None = Field(
        None, gt=0, description="Оставить только первые N мутантов по убыванию |delta| "
                                "после отсева и min_abs_delta (§2.4). Отсутствует — "
                                "конвертируются все прошедшие отсев.")
    min_abs_delta: float = Field(
        0.0, ge=0.0, description="Мутант с |fitness_delta| меньше порога в конверсию "
                                 "не попадает (§2.4); отсев считается отдельным числом "
                                 "в отчёте (`dropped_min_delta`), не молчит.")


class FetchRequest(BaseModel):
    """One arXiv url, both phases, straight into the graph (§4.7 collapsed to one call).

    The url is validated against `fetch.arxiv_id_from_url` here, at the door: the route
    starts minutes of fetch and LLM spend, and a link to something that is not an arXiv
    article must be a 400 now rather than a failed job later. `type` is not a field —
    an arXiv article is a `paper`; a `run` is reported through POST /sources by block C.
    """
    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., min_length=1, examples=["https://arxiv.org/abs/2406.04824"],
                     description="Ссылка на статью arXiv: /abs/, /pdf/ или /html/, с "
                                 "версией или без. Версия из ссылки уважается — "
                                 "Source.id = sha1(url + version).")

    # Parsed once, in the validator, and read back through `arxiv_id`. A property that
    # re-parsed would put a second, unvalidated call to `arxiv_id_from_url` inside the
    # route, where its `FetchError` is a 500 — the one status this request shape can
    # never legitimately produce.
    _arxiv_id: str = PrivateAttr(default="")

    @property
    def arxiv_id(self) -> str:
        """The id `_check` already proved is there."""
        return self._arxiv_id

    @model_validator(mode="after")
    def _check(self):
        from ..ingest.fetch import FetchError, arxiv_id_from_url
        try:
            self._arxiv_id = arxiv_id_from_url(self.url)
        except FetchError as exc:
            raise ValueError(str(exc)) from exc
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


class TrustRequest(BaseModel):
    """An on-demand judging pass (`13` §3.3, finding of the 2026-07-31 review):
    `trust.run_pass`, queued like phase1/phase2 rather than blocking like
    `/admin/reindex` — a pass is dozens of 35B calls, the same cost profile phase 2's
    own end-of-pass step already has.

    Exists because an idea ingested before the judge existed has `dirty=0` and can
    never appear in the ordinary sweep's worklist (`dirty_ideas()`) on its own —
    nothing ever raises its flag. `idea_ids` names it directly, bypassing that list
    rather than adding a second way to decide it.
    """
    model_config = ConfigDict(extra="forbid")

    idea_ids: list[str] | None = Field(
        None, description="Ideas to (re)judge regardless of their dirty flag. Omit to "
                          "judge whatever is already dirty instead — the same worklist "
                          "phase 2's own end-of-pass step reads, just run now rather "
                          "than deferred to the next ingest.")

    @model_validator(mode="after")
    def _check(self):
        if self.idea_ids is not None and not self.idea_ids:
            raise ValueError("idea_ids is empty: name at least one id, or omit the "
                             "field entirely to judge whatever is already dirty")
        return self


class JobOut(BaseModel):
    """One ingest run.

    `/fetch` jobs live in `data/jobs.db` and survive a restart (`queue.py`); the
    operator-triggered ones (`phase1`, `phase2`, `reindex`, `vault-export`) still live
    in the process that serves them and disappear with it. The work is restartable
    either way — the staging cursor is what carries it (§4.7).
    """
    id: str
    # Every string ever passed to `jobs.exclusive`/`jobs.start`/`queue.enqueue` must be
    # listed here. A missing one does not fail where it is used: the job is claimed and
    # served, and `/ingest/jobs` dies later on response validation — a 500 on the only
    # operator view of an ingest that is in fact healthy, for every record until the
    # slot log evicts it.
    kind: Literal["fetch", "run", "phase1", "phase2", "reindex", "vault-export", "trust"]
    # `queued` and `staged` are `/fetch`'s, and they are separate statuses rather than
    # one "running" with a note: queued means no worker has taken it, staged means
    # phase 1 is done and the article is parsed but the single writer has not linked it
    # yet (§4.5). "running" for an hour while nothing runs is the status that lies.
    status: Literal["queued", "running", "staged", "ok", "failed"]
    created_at: str
    finished_at: str | None = None
    args: dict = {}
    stage: str | None = Field(None, description="phase1 | phase2 — which half a queued "
                                                "job is in. Absent for the in-process "
                                                "kinds.")
    attempts: int = Field(0, description="Claims so far IN THE CURRENT PHASE — the "
                                         "counter resets when phase 1 hands the article "
                                         "over, so each half gets its own lives. A job "
                                         "that died mid-run comes back; past "
                                         "queue.MAX_ATTEMPTS it stays failed. A "
                                         "permanent failure (no HTML anywhere, nothing "
                                         "parsed) is final after one.")
    report: dict | None = Field(None, description="The §4.7 report, on status=ok. On "
                                                  "status=staged, what phase 1 measured.")
    error: str | None = Field(None, description="Type and message, on status=failed. "
                                                "Never empty when status=failed. On a "
                                                "requeued job, why the last attempt died.")


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
    queue: dict = Field({}, description="Jobs per status in `data/jobs.db`: queued, "
                                        "running, staged, ok, failed.")
    workers: dict = Field({}, description="Which ingest threads are alive. A dead "
                                          "writer with jobs in `staged` is the one "
                                          "failure that is otherwise invisible.")
