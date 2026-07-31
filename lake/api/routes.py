"""Every route of block A, grouped by what it touches.

    graph     /sources /ideas /theses          reads and the two legal writes
    search    /search                          the raw index, no ideas, no LLM
    retrieve  /retrieve                        the read path of §5.4
    ingest    /fetch /ingest/*                 the write path, as background jobs
    ops       /healthz /stats /admin/reindex   is the lake alive and consistent

Two rules run through all of them, both §5.4 in origin and both about not lying:

1. **Empty is not broken.** `[]` from a live store is data; a store that raised is
   503. Where an empty list would be indistinguishable from a missing row — the
   leaves of an idea, its neighbours — the route answers 404 instead.
2. **A number is either right or absent.** `total` on a page comes from a COUNT
   with the same filter and the same JOIN as the page itself, never from
   `len(items)`.

No route writes a Thesis and none deletes anything. Theses are immutable (§1.2)
and are created only by phase 2, which assigns `idea_id` through the arbiter; a
hand-written leaf would skip linking and land in the store attached to nothing.

Anything composed — a guard, a recomputation, a number built out of several store
calls — lives in `lake.ops` and not here, so that importing the modules reaches
the same behaviour as calling the port. The routes below shape the wire and
nothing else; `lake.ops` exceptions become statuses in ONE place, `app.py`.
"""
from pathlib import Path
import asyncio

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .. import graph_client, index, ops, vault
from ..retrieve import api as retrieve_api
from ..research import ResearchError, build_default_agent
from . import jobs
from ..models import FETCH_DIR
from .schemas import (MAX_K, MAX_PAGE, EdgeOut, ErrorResponse, FetchRequest, Health, IdeaOut,
                      IdeaPatch, JobOut, Page, PendingLinkOut, Phase1Request, Phase2Request,
                      ReindexResult, ResearchRequest, ResearchResponse, RetrieveRequest,
                      RetrieveResponse, SearchHit, SourceIn, SourceOut, StagingOut, Stats,
                      ThesisOut, VaultExportResult)

SOURCES_YAML = Path(__file__).resolve().parents[1] / "sources.yaml"

_STORE_DOWN = {503: {"model": ErrorResponse, "description":
                     "The store is unreachable or raised — the lake is broken, which is "
                     "not the same as the lake being empty (§5.4)."}}
_BAD = {400: {"model": ErrorResponse, "description":
              "Malformed request: a missing or out-of-range parameter, or an unknown "
              "field (`extra=forbid`). Never 422 — C integrated against 400."}}
_NOT_FOUND = {404: {"model": ErrorResponse, "description": "No such row."}}
_BUSY = {409: {"model": ErrorResponse, "description":
               "Another ingest or repair holds the single slot. Ingest is sequential by "
               "design (§4.5); this is a refusal, not a queue. `/fetch` is the exception "
               "— it has a queue and answers 202."}}
_QUEUE_FULL = {429: {"model": ErrorResponse, "description":
                     "The ingest backlog is at `LAKE_QUEUE_MAX`. Carries `Retry-After`. "
                     "Accepting past the ceiling would hand out a `queued` status that "
                     "nothing reaches for hours."}}
_RESEARCH_ERRORS = {**_BAD, 503: {"model": ErrorResponse,
                                   "description": "Neither a usable Lake prior nor an "
                                                  "independently fetched web report was "
                                                  "available."}}
# The queue is a file (`data/jobs.db`), so these three routes can fail the way the store
# can: a read-only mount, a full disk, a locked database. Documented per route rather
# than inherited — the handler over `graph_client.STORE_ERRORS` (`sqlite3.Error`) already
# answers 503 for them, and an undocumented status is a branch C never wrote.
_QUEUE_DOWN = {503: {"model": ErrorResponse, "description":
                     "`data/jobs.db` is unreachable or raised. The job was NOT accepted "
                     "(or, on a read, cannot be listed) — this is not an empty queue."}}
# One status, two refusals, so one entry: the slot is taken, or the destination is not
# ours to rewrite. Splitting them would need two 409 keys, which OpenAPI has no room for.
_VAULT_REFUSED = {409: {"model": ErrorResponse, "description":
                        "A refusal the caller can act on, and the message says which: the "
                        "single slot is held by another ingest or repair (§4.5); the "
                        "destination holds notes this export did not write, so it will not "
                        "be cleared (§11.3.4); the destination is a file; the lake holds no "
                        "nodes; or a node id is not a safe file name (§11.3.3)."}}
# Declared per route and never on the router: 400 and 503 come from app-wide
# handlers, but a route with nothing to validate cannot produce a 400, and
# documenting one there is the same defect `_drop_422` exists to prevent —
# C writes an error branch that never runs. The self-check enforces the
# equivalence in both directions: input <=> 400 documented.
_GRAPH_ERRORS = {**_BAD, **_STORE_DOWN}


def _page(total: int, limit: int, offset: int, items: list) -> dict:
    return {"total": total, "limit": limit, "offset": offset, "items": items}


def _idea(body: dict, include_vector: bool) -> dict:
    """Store row -> wire shape. The 384 floats travel only when asked for."""
    out = dict(body)
    if not include_vector:
        out.pop("vector", None)
    return out


# --------------------------------------------------------------------------- graph

graph = APIRouter(tags=["graph"])


@graph.get("/sources", response_model=Page[SourceOut], responses=_GRAPH_ERRORS, summary="Источники, постранично")
def list_sources(limit: int = Query(50, gt=0, le=MAX_PAGE), offset: int = Query(0, ge=0)):
    return _page(graph_client.counts()["sources"], limit, offset,
                 graph_client.list_sources(limit, offset))


@graph.get("/sources/{source_id}", response_model=SourceOut,
           responses={**_NOT_FOUND, **_STORE_DOWN})
def get_source(source_id: str):
    src = graph_client.get_source(source_id)
    if src is None:
        raise HTTPException(404, f"source {source_id} not found")
    return src


@graph.post("/sources", response_model=SourceOut, responses={**_GRAPH_ERRORS, 409: {
    "model": ErrorResponse,
    "description": "The id exists with a different title or type. Those two are "
                   "provenance of leaves already written; they are not re-postable."}},
    summary="Создать или заменить источник (сюда блок C пишет исход прогона)")
def upsert_source(body: SourceIn):
    """The id-from-(url, version) rule and the 409 that keeps `title`/`type` frozen
    both live in `ops.upsert_source` — a guard only this route enforced would be a
    guard every importer of `graph_client.write_source` walks past."""
    return ops.upsert_source(**body.model_dump())


@graph.get("/ideas", response_model=Page[IdeaOut], responses=_GRAPH_ERRORS, summary="Идеи с листьями, постранично")
def list_ideas(limit: int = Query(50, gt=0, le=MAX_PAGE), offset: int = Query(0, ge=0),
               include_vector: bool = Query(False, description="Отдать вектор идеи (384 float).")):
    ids = graph_client.list_idea_ids(limit, offset)
    return _page(graph_client.counts()["ideas"], limit, offset,
                 [_idea(body, include_vector) for body in graph_client.get_ideas(ids)])


@graph.get("/ideas/{idea_id}", response_model=IdeaOut,
           responses={**_GRAPH_ERRORS, **_NOT_FOUND})
def get_idea(idea_id: str, include_vector: bool = False):
    found = graph_client.get_ideas([idea_id])
    if not found:
        raise HTTPException(404, f"idea {idea_id} not found")
    return _idea(found[0], include_vector)


@graph.patch("/ideas/{idea_id}", response_model=IdeaOut,
             responses={**_GRAPH_ERRORS, **_NOT_FOUND},
             summary="Изменить поля идеи (id и листья не трогаются)")
def patch_idea(idea_id: str, body: IdeaPatch):
    """`IdeaPatch` decides what may be written at all; `ops.patch_idea` is what makes
    `text` drag the vector with it (§1.3), for this route and for every importer."""
    return _idea(ops.patch_idea(idea_id, body.model_dump(exclude_unset=True)), False)


@graph.get("/ideas/{idea_id}/theses", response_model=list[ThesisOut],
           responses={**_NOT_FOUND, **_STORE_DOWN})
def idea_theses(idea_id: str):
    """404 rather than `[]` for an unknown idea: an idea with zero leaves violates
    `IDEA ||--|{ THESIS` (`06:85`), so an empty list here has to mean the invariant
    broke — it must not double as "no such idea"."""
    if not graph_client.get_ideas([idea_id]):
        raise HTTPException(404, f"idea {idea_id} not found")
    return graph_client.get_leaves(idea_id)


@graph.get("/ideas/{idea_id}/neighbors", response_model=list[EdgeOut],
           responses={**_GRAPH_ERRORS, **_NOT_FOUND},
           summary="Рёбра из идеи (пусто в MVP: рёбра — блок B)")
def idea_neighbors(idea_id: str, hops: int = Query(1, ge=1, le=3),
                   min_weight: float | None = None):
    if not graph_client.get_ideas([idea_id]):
        raise HTTPException(404, f"idea {idea_id} not found")
    return graph_client.neighbors([idea_id], hops, min_weight)


@graph.get("/theses", response_model=Page[ThesisOut], responses=_GRAPH_ERRORS, summary="Тезисы-листья, постранично")
def list_theses(idea_id: str | None = None, source_id: str | None = None,
                limit: int = Query(50, gt=0, le=MAX_PAGE), offset: int = Query(0, ge=0)):
    return _page(graph_client.count_theses(idea_id, source_id), limit, offset,
                 graph_client.list_theses(idea_id, source_id, limit, offset))


@graph.get("/theses/{thesis_id}", response_model=ThesisOut,
           responses={**_NOT_FOUND, **_STORE_DOWN})
def get_thesis(thesis_id: str):
    leaf = graph_client.get_thesis(thesis_id)
    if leaf is None:
        raise HTTPException(404, f"thesis {thesis_id} not found")
    return leaf


# -------------------------------------------------------------------------- search

search = APIRouter(tags=["search"])


@search.get("/search", response_model=list[SearchHit], responses=_GRAPH_ERRORS,
            summary="Сырой гибридный поиск по тезисам: BM25 + косинус, RRF")
def search_index(q: str = Query(..., min_length=1), k: int = Query(10, gt=0, le=MAX_K)):
    """The index arm of the read path on its own (§5.2) — no rewrite, no idea bodies,
    no ranking. `bm25_rank: null` on every hit means the FTS arm returned nothing and
    the hybrid is running on cosine alone, which is the failure this view exists to
    make visible."""
    return index.search_theses(q, k)


# ------------------------------------------------------------------------ retrieve

retrieve = APIRouter(tags=["retrieve"])


@retrieve.post("/retrieve", response_model=RetrieveResponse,
               responses={**_BAD, **_STORE_DOWN},
               summary="Запрос эволюции → идеи с провенансом (§5.4)")
def retrieve_endpoint(req: RetrieveRequest, request: Request):
    if request.app.state.mock:
        # Frozen shape for C, before the real path exists (§9, `08:348`). Writes no
        # log line by design: mock rows in the metrics log are contamination.
        return retrieve_api.MOCK_RESPONSE
    status, payload = retrieve_api.retrieve(req.query, req.k, budget=req.budget,
                                            do_rewrite=req.rewrite, run_id=req.run_id)
    if status != 200:
        # Returned, not raised: `retrieve` has already written the log line and built
        # the {error, log_id} body, and the handler must not build a second one.
        return JSONResponse(status_code=status, content=payload)
    return payload


# ---------------------------------------------------------------- deep research

research = APIRouter(tags=["research"])


@research.post("/research", response_model=ResearchResponse,
               responses=_RESEARCH_ERRORS, summary="RAG + web → language research report")
def research_endpoint(body: ResearchRequest, request: Request):
    """Run one bounded research round owned by Ideas Lake.

    GigaEvo calls this from its background research worker.  It is deliberately
    not part of `/retrieve` and is never invoked by a memory selector or an
    evolution hook.  The agent is cached per process; tests and embedders may
    inject `app.state.research_agent` before making a request.
    """
    if request.app.state.mock:
        raise HTTPException(503, "mock mode: /research is disabled")
    agent = getattr(request.app.state, "research_agent", None)
    if agent is None:
        agent = build_default_agent()
        request.app.state.research_agent = agent
    try:
        return asyncio.run(agent.research(body))
    except ResearchError as exc:
        raise HTTPException(503, str(exc)) from exc


# -------------------------------------------------------------------------- ingest

ingest = APIRouter(prefix="/ingest", tags=["ingest"])


def _start(kind: str, fn, args: dict) -> dict:
    try:
        return jobs.start(kind, fn, args)
    except jobs.Busy as busy:
        raise HTTPException(409, str(busy))


# `/fetch`, not `/ingest/fetch`: its own router, because the `ingest` one carries a
# prefix. Same tag — it is the same write path, with both phases in one call.
fetch_router = APIRouter(tags=["ingest"])


@fetch_router.post("/fetch", response_model=JobOut, status_code=202,
                   responses={**_BAD, **_QUEUE_FULL, 503: {
                       "model": ErrorResponse,
                       "description": "Either `data/jobs.db` raised — the job was NOT "
                                      "accepted, which is not the same as an empty queue "
                                      "— or the server runs in `--mock`, where this route "
                                      "would be the one thing that fetches and writes."}},
                   summary="Одна статья с arXiv по ссылке: fetch → тезисы → идеи в графе")
def fetch_article(body: FetchRequest, request: Request):
    """Both phases for one url, as a queued job (202 + `JobOut`, poll /ingest/jobs).

    Never 409. The job goes into `data/jobs.db` (`queue.py`), a fetch worker runs
    phase 1 on it, and the single writer links it into the graph when its turn comes —
    so a caller may post ten urls in a row without waiting or retrying. What the single
    slot used to buy is still bought, by the writer being one thread holding
    `jobs.exclusive` (§4.5, `api/workers.py`).

    The article gets its own staging file under `data/fetch/` rather than a line in the
    corpus staging — see `run.ingest_one` for why sharing it would ingest other people's
    sources. Re-posting the same url is idempotent twice over: a url already queued or
    running returns THAT job rather than opening a second one, and a url already in the
    lake is skipped leaf by leaf at link step [0] (§4.8).

    429 when the backlog is at `LAKE_QUEUE_MAX`: a status of `queued` that no worker
    will reach for hours is a polite way of dropping the request.
    """
    from .. import queue
    from . import workers
    if request.app.state.mock:
        # `--mock` starts no workers (`app.lifespan`) and promises "no store touched"
        # on /healthz. Accepting here would write a row into the real `data/jobs.db`
        # that nothing in this process will ever claim: a 202 for work silently
        # dropped, which is exactly the shape the mock is supposed to be free of.
        raise HTTPException(503, "mock mode: /fetch is the one route that fetches an "
                                 "article and writes the graph, and a mock server "
                                 "touches neither — start without --mock")
    # No sanitizing of `arxiv_id`, because there is nothing to sanitize:
    # `fetch._ARXIV_ID` admits digits, one dot and an optional `vN`, and nothing else
    # reaches this line. Stripping separators here would read as a guard and hide that
    # the door is one.
    try:
        return queue.enqueue("fetch", {"url": body.url, "arxiv_id": body.arxiv_id},
                             dedup_key=body.arxiv_id, ceiling=workers.QUEUE_MAX)
    except queue.Full as full:
        raise HTTPException(429, str(full), headers={"Retry-After": "60"})


@ingest.post("/phase1", response_model=JobOut, status_code=202, responses={**_BUSY, 400:
             {"model": ErrorResponse, "description": "Nothing to ingest."}},
             summary="Фаза 1: fetch → parse → generalize → staging.jsonl (граф не трогается)")
def start_phase1(body: Phase1Request | None = None):
    """Returns immediately with a job. Phase 1 costs one LLM call per section, so it
    runs for minutes — and it writes nothing to the graph, which is what makes it the
    safe half to trigger over HTTP (§4.7)."""
    body = body or Phase1Request()
    if body.sources is None:
        import yaml                             # only this route reads sources.yaml
        # Not re-validated through SourceEntry: a bad row in the generated corpus
        # must cost that one source, named and counted in the phase 1 report (§4.7),
        # not refuse the whole 84-source ingest at the door.
        entries = yaml.safe_load(SOURCES_YAML.read_text(encoding="utf-8"))
    else:
        entries = [entry.model_dump(exclude_none=True) for entry in body.sources]
    entries = entries[:body.limit]
    if not entries:
        raise HTTPException(400, "no source entries to ingest")

    from ..ingest import run
    return _start("phase1",
                  lambda: {"staging_lines": run.phase1(entries, workers=body.workers)},
                  {"sources": len(entries), "workers": body.workers,
                   "from_yaml": body.sources is None})


@ingest.post("/phase2", response_model=JobOut, status_code=202,
             responses={**_BAD, **_BUSY},
             summary="Фаза 2: staging → граф + индекс, последовательно, с курсором")
def start_phase2(body: Phase2Request | None = None):
    body = body or Phase2Request()
    from ..ingest import run
    return _start("phase2", lambda: run.phase2(limit=body.limit), {"limit": body.limit})


@ingest.get("/jobs", response_model=list[JobOut], responses={**_BAD, **_QUEUE_DOWN},
            summary="Задания: очередь /fetch с диска плюс ручные из этого процесса")
def list_jobs(limit: int = Query(50, gt=0, le=MAX_PAGE)):
    """Two registers, one list, newest first.

    `/fetch` jobs come from `data/jobs.db` and outlive the process; the manual kinds
    (`phase1`, `phase2`, `reindex`, `vault-export`) come from `jobs.py` and do not.
    Merged rather than kept apart because a caller asking "what is the lake doing"
    means both, and a queued fetch behind a manual phase 2 is exactly the pair that
    explains a wait.

    `limit` bounds each register and therefore the answer: the durable side is a file
    that keeps `queue.KEEP_FINISHED` rows, and slicing it inside `queue.listing()` while
    the route promised "all jobs" is how a caller reads a truncated list as the whole
    state of the lake.
    """
    from .. import queue
    durable = queue.listing(limit)
    # By id, durable first: while the writer holds a fetch job it also holds the
    # in-process slot under the SAME id (`workers.write_step`), and listing both
    # copies would show one article twice, once with a status that is a snapshot of
    # the slot rather than of the work.
    seen = {job["id"] for job in durable}
    both = durable + [job for job in jobs.listing() if job["id"] not in seen]
    return sorted(both, key=lambda job: job["created_at"], reverse=True)[:limit]


@ingest.get("/jobs/{job_id}", response_model=JobOut,
            responses={**_NOT_FOUND, **_QUEUE_DOWN})
def get_job(job_id: str):
    from .. import queue
    # The durable register first: after a restart it is the only one that still has
    # the row, and the in-process dict is empty rather than wrong.
    job = queue.get(job_id) or jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"job {job_id} not found (a /fetch job survives a "
                                 "restart; phase1/phase2/reindex/vault-export do not, "
                                 "and older ones fall off the ring)")
    return job


@ingest.get("/staging", response_model=StagingOut, responses={503: {
    "model": ErrorResponse,
    "description": "staging.jsonl or staging.cursor is unreadable or the two disagree; "
                   "the body names the line or the value."}},
    summary="Что лежит между фазами — точка приёмки глазами")
def staging_state():
    return ops.staging_state()


@ingest.get("/pending-link", response_model=list[PendingLinkOut], responses=_BAD,
            summary="Очередь отказов арбитра линковки (§4.5) — свежие снизу")
def pending_link(limit: int = Query(50, gt=0, le=MAX_PAGE)):
    return ops.pending_link(limit)


# ----------------------------------------------------------------------------- ops
# `ops_router`, not `ops`: the module of the same name is what these three call.

ops_router = APIRouter(tags=["ops"])


@ops_router.get("/healthz", response_model=Health,
                summary="Живость плюс единственный инвариант, который гниёт молча")
def healthz(request: Request):
    if request.app.state.mock:
        return {"status": "ok", "mock": True, "detail": "mock mode, no store touched"}
    # `mock` is app state, so it stays here; `ops.health()` never raises — it answers
    # `degraded` with the reason, because a health check that dies tells the caller
    # less than one that says what is wrong.
    return {**ops.health(), "mock": False}


@ops_router.get("/stats", response_model=Stats, responses=_STORE_DOWN,
                summary="Числа отчёта §4.7 по всему озеру")
def stats():
    return ops.stats()


@ops_router.post("/admin/reindex", response_model=ReindexResult,
                 responses={**_BUSY, **_STORE_DOWN},
                 summary="Пересобрать индекс тезисов из хранилища (§6.19)")
def reindex():
    return ops.reindex()


@ops_router.post("/vault/export", response_model=VaultExportResult,
                 responses={**_VAULT_REFUSED, **_STORE_DOWN},
                 summary="Выгрузить озеро в Obsidian-vault (спека 11)")
def vault_export():
    """Deliberately takes no `dest`: over HTTP that is a write-anywhere primitive, and
    the server listens on 0.0.0.0 by default. `--dest` stays on the CLI, where the
    caller already owns the filesystem. The operation is the same either way (§11.4).

    The job slot, not because the export is slow, but because an export racing phase 2
    reads a torn lake: the ideas of a batch without its theses. The read-vs-`counts()`
    guard would catch it as a 503, which is a true answer to the wrong question.
    """
    with jobs.exclusive("vault-export") as job:
        job["report"] = vault.export()
        return job["report"]


ROUTERS = (retrieve, research, graph, search, fetch_router, ingest, ops_router)
