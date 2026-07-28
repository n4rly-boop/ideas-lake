"""Every route of block A, grouped by what it touches.

    graph     /sources /ideas /theses          reads and the two legal writes
    search    /search                          the raw index, no ideas, no LLM
    retrieve  /retrieve                        the read path of §5.4
    ingest    /ingest/*                        the write path, as background jobs
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
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .. import graph_client, index
from ..models import PENDING_LINK, STAGING, STAGING_CURSOR, Source, source_id as make_source_id
from ..retrieve import api as retrieve_api
from . import jobs
from .schemas import (MAX_K, MAX_PAGE, EdgeOut, ErrorResponse, Health, IdeaOut, IdeaPatch,
                      JobOut, Page, PendingLinkOut, Phase1Request, Phase2Request,
                      ReindexResult, RetrieveRequest, RetrieveResponse, SearchHit, SourceIn,
                      SourceOut, StagingOut, Stats, ThesisOut)

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
               "design (§4.5); this is a refusal, not a queue."}}
# Declared per route and never on the router: 400 and 503 come from app-wide
# handlers, but a route with nothing to validate cannot produce a 400, and
# documenting one there is the same defect `_drop_422` exists to prevent —
# C writes an error branch that never runs. The self-check enforces the
# equivalence in both directions: input <=> 400 documented.
_GRAPH_ERRORS = {**_BAD, **_STORE_DOWN}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _page(total: int, limit: int, offset: int, items: list) -> dict:
    return {"total": total, "limit": limit, "offset": offset, "items": items}


def _idea(body: dict, include_vector: bool) -> dict:
    """Store row -> wire shape. The 384 floats travel only when asked for."""
    out = dict(body)
    if not include_vector:
        out.pop("vector", None)
    return out


def _lines(path: Path) -> list[str]:
    if not Path(path).exists():
        return []
    return [ln for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln.strip()]


def _cursor(lines: int | None = None) -> int:
    """Phase 2's watermark. Absent file means nothing ingested yet, which is 0 —
    the one case where a default is the truth and not a guess.

    Anything else is refused with the reason. These two endpoints are what an
    operator opens when the ingest is already in a bad state, so a bare 500 is
    the least useful answer they could give; and a cursor past the end of the
    file is corruption, not "everything ingested" — clamping it to zero pending
    lines would report a finished ingest for a file the cursor no longer fits.
    """
    path = Path(STAGING_CURSOR)
    if not path.exists():
        return 0
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return 0
    if not raw.isdigit():
        raise HTTPException(503, f"{path.name} holds {raw!r}, not a line number")
    cursor = int(raw)
    if lines is not None and cursor > lines:
        raise HTTPException(503, f"{path.name} is at {cursor}, past the {lines} lines of "
                                 f"{Path(STAGING).name} — the two disagree; drop the cursor "
                                 f"to replay (phase 2 skips what is already stored)")
    return cursor


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
    """The id is derived from (url, version), so re-posting the same run replaces the
    row instead of duplicating it — that is what makes this safe to call after every
    evolution run (§1.1).

    What a re-post may change is the run outcome (`run_success`, `run_meta`,
    `retrieved_at`) and nothing else. `title` and `type` are read back through the
    JOIN as `source_title` / `source_type` of every leaf of that source, and
    `source_type` is what the linker uses to keep `effect_claimed` apart from
    `effect_observed` (§4.6). Letting a re-post move them would rewrite the
    provenance of theses that are supposed to be frozen (§1.2) — through a route
    that never touches the thesis table.
    """
    existing = graph_client.get_source(make_source_id(body.url, body.version))
    if existing is not None:
        changed = [name for name in ("title", "type")
                   if getattr(body, name) != existing[name]]
        if changed:
            raise HTTPException(409, f"source {existing['id']} already exists with a different "
                                     f"{' and '.join(changed)}; those are provenance of its "
                                     f"leaves. Re-post only run_success/run_meta.")
    src = Source(id=make_source_id(body.url, body.version), url=body.url, title=body.title,
                 type=body.type, version=body.version,
                 retrieved_at=body.retrieved_at or _now(),
                 run_success=body.run_success, run_meta=body.run_meta)
    graph_client.write_source(src)
    return graph_client.get_source(src.id)


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
    """`text` drags the vector with it: the idea vector is derived from the text
    (§1.3), and writing one without the other drifts the idea's neighbourhood away
    from what the idea now says — the same rule `rederive` follows (§4.6)."""
    fields = body.model_dump(exclude_unset=True)
    if "text" in fields:
        from .. import embed          # local: loading sentence-transformers costs seconds
        fields["vector"] = embed.embed_docs([fields["text"]])[0].tolist()
    fields["updated_at"] = _now()
    try:
        graph_client.update_idea(idea_id, fields)
    except KeyError:
        raise HTTPException(404, f"idea {idea_id} not found")
    return _idea(graph_client.get_ideas([idea_id])[0], False)


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


# -------------------------------------------------------------------------- ingest

ingest = APIRouter(prefix="/ingest", tags=["ingest"])


def _start(kind: str, fn, args: dict) -> dict:
    try:
        return jobs.start(kind, fn, args)
    except jobs.Busy as busy:
        raise HTTPException(409, str(busy))


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


@ingest.get("/jobs", response_model=list[JobOut], summary="Задания этого процесса, новые сверху")
def list_jobs():
    return jobs.listing()


@ingest.get("/jobs/{job_id}", response_model=JobOut, responses=_NOT_FOUND)
def get_job(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"job {job_id} not found (jobs live in this process only)")
    return job


@ingest.get("/staging", response_model=StagingOut, responses={503: {
    "model": ErrorResponse,
    "description": "staging.jsonl or staging.cursor is unreadable or the two disagree; "
                   "the body names the line or the value."}},
    summary="Что лежит между фазами — точка приёмки глазами")
def staging_state():
    # ponytail: parses the whole file to group by source. Fine at 84 sources x 30
    # lines; if staging ever grows past that, keep a sidecar index instead.
    lines = _lines(STAGING)
    cursor = _cursor(len(lines))
    per_source: dict[str, dict] = {}
    total = 0
    for lineno, line in enumerate(lines, 1):
        try:
            src = json.loads(line)["source"]
            key, title = src["id"], src["title"]
        except (ValueError, KeyError, TypeError) as exc:
            # A phase 1 killed mid-write leaves a truncated last line. Name the line
            # instead of dying with a traceback: this endpoint is the one an operator
            # opens precisely because something went wrong.
            raise HTTPException(503, f"{Path(STAGING).name}:{lineno} is not a staging line "
                                     f"({type(exc).__name__}: {exc})")
        entry = per_source.setdefault(key, {"id": key, "title": title,
                                            "lines": 0, "ingested": 0})
        entry["lines"] += 1
        entry["ingested"] += lineno <= cursor
        total += 1
    return {"lines": total, "cursor": cursor, "pending_lines": max(0, total - cursor),
            "sources": list(per_source.values())}


@ingest.get("/pending-link", response_model=list[PendingLinkOut], responses=_BAD,
            summary="Очередь отказов арбитра линковки (§4.5) — свежие снизу")
def pending_link(limit: int = Query(50, gt=0, le=MAX_PAGE)):
    """This queue existing at all is the fail-closed behaviour: an arbiter that failed
    writes here instead of guessing `add` or `new`. Non-empty means theses were parsed
    and never attached — work waiting, not work lost. The full lines (staging row and
    all candidates) stay in `data/pending_link.jsonl`."""
    out = []
    for line in _lines(PENDING_LINK)[-limit:]:
        rec = json.loads(line)
        row = rec.get("staging_line") or {}
        out.append({"ts": rec.get("ts", ""), "run_id": rec.get("run_id"),
                    "error": rec.get("error", ""),
                    "thesis_text": (row.get("thesis") or {}).get("text", ""),
                    "source_id": (row.get("source") or {}).get("id", ""),
                    "candidates": len(rec.get("candidates") or [])})
    return out


# ----------------------------------------------------------------------------- ops

ops = APIRouter(tags=["ops"])


@ops.get("/healthz", response_model=Health,
         summary="Живость плюс единственный инвариант, который гниёт молча")
def healthz(request: Request):
    if request.app.state.mock:
        return {"status": "ok", "mock": True, "detail": "mock mode, no store touched"}
    try:
        leaves = graph_client.counts()["theses"]
        indexed = index.count()
    except Exception as exc:
        # `degraded` with the reason, not a 500: a health check that dies tells the
        # caller less than one that says what is wrong.
        return {"status": "degraded", "mock": False, "detail": f"{type(exc).__name__}: {exc}"}
    ok = indexed == leaves
    return {"status": "ok" if ok else "degraded", "mock": False, "theses_indexed": indexed,
            "leaves_in_store": leaves, "in_sync": ok,
            "detail": None if ok else "index and store disagree — POST /admin/reindex (§6.19)"}


@ops.get("/stats", response_model=Stats, responses=_STORE_DOWN,
         summary="Числа отчёта §4.7 по всему озеру")
def stats():
    counts = graph_client.counts()
    indexed = index.count()
    running = jobs.running()
    return {**counts, "theses_indexed": indexed,
            "in_sync": indexed == counts["theses"],
            "ideas_without_leaves": graph_client.ideas_without_leaves(),
            "trust_scale": graph_client.trust_scale(),
            "staging_lines": len(_lines(STAGING)),
            "staging_cursor": _cursor(),
            "pending_link": len(_lines(PENDING_LINK)),
            "job_running": running["id"] if running else None}


@ops.post("/admin/reindex", response_model=ReindexResult,
          responses={**_BUSY, **_STORE_DOWN},
          summary="Пересобрать индекс тезисов из хранилища (§6.19)")
def reindex():
    """The repair path of §6.19, and the only supported answer to a `degraded` health
    check: the store carries `idea_id`, which phase 2 assigns and `staging.jsonl`
    therefore never holds.

    It takes the ingest slot for the duration — a rebuild racing a phase 2 would index
    a moving target — and every vector is validated before the old index is dropped,
    so a refusal leaves the suspect index in place instead of emptying it.
    """
    try:
        with jobs.exclusive("reindex") as job:
            before = index.count()
            rows = graph_client.all_theses()
            index.reconcile(rows)
            after = index.count()
            job["report"] = {"indexed_before": before, "leaves_in_store": len(rows),
                             "indexed_after": after}
            return {"indexed_before": before, "leaves_in_store": len(rows),
                    "indexed_after": after, "in_sync": after == len(rows)}
    except jobs.Busy as busy:
        raise HTTPException(409, str(busy))


ROUTERS = (retrieve, graph, search, ingest, ops)
