"""The block A backend: one FastAPI app over the graph, the index and the ingest.

    uvicorn lake.api.app:app --port 8077
    python3 -m lake.api.app --port 8077 [--mock] [--selfcheck]

Spec §5.4 named `http.server` for zero dependencies (`09:290`) and only for
/retrieve. What C, B and D actually need is the whole block reachable the same
way: read the graph, page over it, trigger an ingest, see the arbiter's refusal
queue, repair the index. `fastapi`, `uvicorn` and `pydantic` were already
installed; nothing new was added.

Three rules this layer must not soften:

1. **503 is not `[]`.** A store that raised means the lake is broken; an empty
   answer from a live store is data for the A/B (§5.4). `retrieve.api` decides
   which happened for /retrieve, and for the rest the handler registered over
   `graph_client.STORE_ERRORS` does — a store error is never a 200 with a short
   list. Which exception classes those are is the store's knowledge, not this
   layer's (§3.4).

The composed operations behind the routes live in `lake.ops`, which knows no
HTTP: the guards they carry — a source's provenance is not re-postable, an
idea's text drags its vector — have to hold for a caller that imports the
module, not only for one that sends a request. The routes here are the thin
half: they map `ops` refusals to statuses and nothing else.
2. **Every /retrieve leaves exactly one log line**, 503 included. The log lives
   in `retrieve.api`, so no HTTP path can skip it. The mock is the one deliberate
   exception: frozen rows in the metrics log are contamination.
3. **Validation answers 400**, not FastAPI's default 422 — C was integrated
   against 400 and the body stays `{"error": ...}` on every status.

Routes are plain `def`, not `async def`: everything under them is blocking work
(sqlite, numpy, LLM calls), so Starlette runs them in its threadpool and the
event loop stays free. `contextvars` are copied into that threadpool, which is
what keeps `trace.request` per-request (see trace.py).
"""
import argparse
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import graph_client, ops
from . import jobs
from .routes import ROUTERS

# `lake.ops` refusals -> statuses, declared once. The composed operations live in a
# module with no HTTP in it (see `ops.py`), so the mapping has to live somewhere;
# repeating it per route is how one route ends up answering 500 for a refusal the
# caller could have acted on. A bare `OpsError` is not mapped on purpose: an
# unclassified refusal is a bug here, and 500 says so.
OPS_STATUS: tuple[tuple[type[ops.OpsError], int], ...] = (
    (ops.NotFound, 404), (ops.Conflict, 409), (ops.Broken, 503))

DESCRIPTION = """\
Долговременная память между прогонами эволюции (проект 28, блок A).

* **retrieve** — запрос → идеи с провенансом. Recall-first: отказа по низкому
  скору нет, выдача дозаполняется до `k`, но каждый элемент говорит `via`.
* **graph** — чтение хранилища постранично; из записей только upsert источника
  (сюда блок C пишет исход прогона) и правка полей идеи.
* **ingest** — write path фоновыми заданиями, по одному за раз.
* **ops** — живость, числа §4.7 и пересборка индекса.

Тезисы неизменяемы (§1.2) и создаются только фазой 2, поэтому ручки на запись
тезиса нет и не будет. Удаления нет ни у чего.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    # §8: the budget is p95 <= 5 s and the first embedding call loads the model
    # (seconds, once per process). Pay it before the port accepts anything, so no
    # request does. No try/except: a server that cannot embed cannot answer, and
    # failing at startup beats failing per request as a 503.
    if app.state.warmup and not app.state.mock:
        from .. import embed
        embed.embed_query("warm up")
    yield


def create_app(mock: bool = False, warmup: bool = True) -> FastAPI:
    app = FastAPI(
        title="Ideas Lake — block A",
        version="0.2.0",
        summary="Ingestion + retrieve: источник → тезис → идея, и обратно по запросу.",
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.mock = mock
    app.state.warmup = warmup
    for router in ROUTERS:
        app.include_router(router)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """422 -> 400, one readable line. C integrated against 400 (§5.4)."""
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"][1:]) or "body"
        return JSONResponse(status_code=400, content={"error": f"{where}: {first['msg']}"})

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """One error shape on every status: `{"error": ...}`, never `{"detail": ...}`.

        Registered on Starlette's class, not FastAPI's subclass: an unknown path and
        a wrong verb are raised by the ROUTER as the parent class, and a handler on
        the subclass alone leaves those two answering `{"detail": ...}` — the one
        body C cannot parse, handed to it exactly when it got the URL wrong.
        """
        return JSONResponse(status_code=exc.status_code, content={"error": str(exc.detail)},
                            headers=getattr(exc, "headers", None))

    async def _ops_error(request: Request, exc: Exception) -> JSONResponse:
        """A domain refusal from `lake.ops`, on the status `OPS_STATUS` gives it.

        Registered on the base class: Starlette walks the MRO, so every subclass is
        covered and a new one cannot silently start answering 200.
        """
        status = next((code for cls, code in OPS_STATUS if isinstance(exc, cls)), 500)
        return JSONResponse(status_code=status, content={"error": str(exc)})

    app.add_exception_handler(ops.OpsError, _ops_error)

    async def _busy(request: Request, exc: Exception) -> JSONResponse:
        """The single slot is taken: 409, the same refusal `ops.reindex` converts by hand.

        Registered here because `jobs.Busy` is a bare `RuntimeError` and `jobs` cannot
        subclass `ops.Conflict` — `ops` imports `jobs`, not the other way round. Two
        routes already remembered to convert it and a third did not, answering 500 while
        its own OpenAPI promised 409; a handler cannot be forgotten.
        """
        return JSONResponse(status_code=409, content={"error": str(exc)})

    app.add_exception_handler(jobs.Busy, _busy)

    async def _store_down(request: Request, exc: Exception) -> JSONResponse:
        """The store raised: 503, and it says so. A 500 with an empty body would let
        "the lake is broken" pass for "the request was bad" (§5.4)."""
        return JSONResponse(status_code=503,
                            content={"error": f"store unavailable: {type(exc).__name__}: {exc}"})

    # Which exception classes mean "the store is down" is the store's knowledge,
    # not this layer's (§3.4) — see `graph_client.STORE_ERRORS`.
    for store_error in graph_client.STORE_ERRORS:
        app.add_exception_handler(store_error, _store_down)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        """Anything else: 500, but still `{"error": ...}` with the type and message.

        Without this, Starlette answers `text/plain` "Internal Server Error" — a body
        C cannot parse and a message nobody can act on. Response-model validation
        failures land here too: they are raised AFTER the route returned, so no
        route-level try can see them.
        """
        return JSONResponse(status_code=500,
                            content={"error": f"{type(exc).__name__}: {exc or '(no message)'}"})

    _drop_422(app)
    return app


def _drop_422(app: FastAPI) -> None:
    """Remove FastAPI's automatic 422 from the schema: this app never returns one.

    The OpenAPI document is what C integrates against, and a documented status the
    server cannot produce sends the other side writing a branch that never runs.
    """
    def openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, summary=app.summary,
                             description=app.description, routes=app.routes)
        for path in schema.get("paths", {}).values():
            for operation in path.values():
                operation.get("responses", {}).pop("422", None)
        for name in ("HTTPValidationError", "ValidationError"):
            schema.get("components", {}).get("schemas", {}).pop(name, None)
        app.openapi_schema = schema
        return schema

    app.openapi = openapi


app = create_app()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="python3 -m lake.api.app")
    parser.add_argument("--port", type=int, default=8077)
    parser.add_argument("--host", default="0.0.0.0",
                        help="C calls the endpoint from another machine (default: all "
                             "interfaces); 127.0.0.1 to keep it local")
    parser.add_argument("--mock", action="store_true",
                        help="/retrieve serves the frozen MOCK_RESPONSE and touches neither "
                             "graph nor LLM; the other routes are unaffected")
    parser.add_argument("--selfcheck", action="store_true",
                        help="offline check of the HTTP layer, then exit")
    args = parser.parse_args(argv)

    if args.selfcheck:
        from .selfcheck import main as selfcheck_main
        selfcheck_main()
        return

    import uvicorn
    uvicorn.run(create_app(mock=args.mock), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
