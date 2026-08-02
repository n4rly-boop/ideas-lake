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
4. **Every WRITE route, and every route that spends the school's GPUs, needs
   `Authorization: Bearer $LAKE_API_KEY`.** There is no other authentication anywhere
   in block A, so the server refuses to START without a key rather than serving an
   open one. A fixed, named set of READ-ONLY routes — the graph, the index, `/dial`,
   `/healthz`, `/stats` — needs none: nothing in them writes or costs a token, so a
   key would gate nothing but the reading itself (§ below `OPEN_ENDPOINTS`). The
   ingest machine room (`/ingest/jobs`, `/ingest/staging`, `/ingest/pending-link`) is
   GET too but stays behind the key on purpose — it is operational detail, not lake
   data. `--no-auth` exists for a loopback-only port and says so in the log. See
   `_require_key`, `OPEN_PATHS` and `OPEN_ENDPOINTS`.

Routes are plain `def`, not `async def`: everything under them is blocking work
(sqlite, numpy, LLM calls), so Starlette runs them in its threadpool and the
event loop stays free. `contextvars` are copied into that threadpool, which is
what keeps `trace.request` per-request (see trace.py).
"""
import argparse
import hmac
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse
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

# The static, non-RESTful paths served without a key, and the list is deliberately short:
# the OpenAPI document is the integration contract, it holds no lake data, and C reads it
# before it has anything to authenticate with. `/healthz` and the rest of the lake's own
# reads are open too, but by ROUTE, not by raw path — that boundary is `OPEN_ENDPOINTS`,
# below. Everything not covered by either set — including paths that do not exist — still
# needs the key.
#
# `/ui` is on the list for exactly the same reason `/docs` is, and on no weaker one: it
# is a static asset that holds no lake data and reads nothing. A browser cannot put a
# header on a top-level navigation, so a guarded page would be a page nobody can open;
# the alternative — the key in the query string — would put a secret in every log and
# every history entry. The DATA the page shows is still every one of these routes,
# fetched with the key the operator types into it. Kept out of the OpenAPI document
# (`include_in_schema=False`) so the contract keeps describing the API and nothing else,
# and so `_drop_422` does not stamp it with a 401 it cannot answer.
UI_PATH = "/ui"
UI_FILE = Path(__file__).resolve().parent / "console.html"
OPEN_PATHS = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc", UI_PATH})

# The read-only lake data a caller may reach without a key, as (method, route template)
# pairs — a template alone would open every verb the router answers on that path (a
# future PATCH landing behind today's GET), so openness is decided on the PAIR, never
# on the path string. Every template here is copied from the `@router.get(...)`
# declaration in `routes.py`, and `_open_route_matchers` below refuses to start if one
# of them was mistyped and matches no registered route — a hand-rolled prefix check
# (`path.startswith("/ideas/")`) would instead have opened `PATCH /ideas/{id}` and
# whatever gets added under `/ideas/` tomorrow.
#
# Left OUT on purpose, though every one is GET: `/ingest/jobs(/{id})`, `/ingest/staging`,
# `/ingest/pending-link` are the ingest machine room (queue/staging state), not lake
# data, and `/fetch`, `/run`, `/ingest/phase1|2`, `/retrieve`, `/research`,
# `/admin/reindex`, `/admin/trust`, `/vault/export`, `POST /sources`, `PATCH
# /ideas/{id}` all write the graph or spend the school's GPUs.
OPEN_ENDPOINTS: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/healthz"), ("GET", "/stats"),
    ("GET", "/sources"), ("GET", "/sources/{source_id}"),
    ("GET", "/ideas"), ("GET", "/ideas/{idea_id}"),
    ("GET", "/ideas/{idea_id}/theses"), ("GET", "/ideas/{idea_id}/neighbors"),
    ("GET", "/edges"),
    ("GET", "/theses"), ("GET", "/theses/{thesis_id}"),
    ("GET", "/search"), ("GET", "/dial"),
})


def _open_route_matchers(app: FastAPI) -> tuple[re.Pattern[str], ...]:
    """Turn `OPEN_ENDPOINTS` into the compiled matcher each route ALREADY dispatches
    on, instead of re-deriving one by hand.

    `route.path_format`/`route.path_regex` are what Starlette compiled from the exact
    `@router.get("/ideas/{idea_id}")` string (`compile_path`, anchored both ends), so a
    match here is a match on the router's own shape — `/ideas/x/y`, which no route
    answers, cannot slip through, and neither can a path that merely starts with
    `/ideas/`. Every entry is also handed `HEAD`: FastAPI's `APIRoute` (unlike
    Starlette's own `Route`, which is what serves `/docs`) does not add it for a bare
    `.get()`, and requirement 4 is that HEAD on an open route behaves like GET.
    """
    matchers: list[re.Pattern[str]] = []
    found: set[str] = set()
    for route in app.routes:
        path_format = getattr(route, "path_format", None)
        if path_format is None or "GET" not in getattr(route, "methods", ()):
            continue
        if ("GET", path_format) in OPEN_ENDPOINTS:
            route.methods.add("HEAD")
            matchers.append(route.path_regex)
            found.add(path_format)
    missing = {template for _, template in OPEN_ENDPOINTS} - found
    # A typo here would silently CLOSE a route that should be open (fail closed, not
    # open) — but it is still a bug worth crashing on rather than discovering by curl.
    assert not missing, f"OPEN_ENDPOINTS names no registered route: {sorted(missing)}"
    return tuple(matchers)

DESCRIPTION = """\
Долговременная память между прогонами эволюции (проект 28, блок A).

* **retrieve** — запрос → идеи с провенансом. Recall-first: отказа по низкому
  скору нет, выдача дозаполняется до `k`, но каждый элемент говорит `via`.
* **research** — bounded natural-language mission → Lake priors plus optional
  independently fetched web evidence → language report. It never creates local
  ideas or fitness evidence.
* **graph** — чтение хранилища постранично; из записей только upsert источника
  (сюда блок C пишет исход прогона) и правка полей идеи.
* **ingest** — write path фоновыми заданиями, по одному за раз.
* **ops** — живость, числа §4.7 и пересборка индекса.

Тезисы неизменяемы (§1.2) и создаются только фазой 2, поэтому ручки на запись
тезиса нет и не будет. Удаления нет ни у чего.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Refused at startup, not per request: every route of this app writes to the graph
    # or spends the school's GPUs, and a server that came up without a key would be an
    # open one. The check lives here rather than in `create_app` so that importing the
    # module — which every self-check does — never needs a secret.
    if app.state.api_key is not False and not app.state.api_key:
        raise RuntimeError(
            "LAKE_API_KEY is empty: this API has no other authentication and every "
            "route either writes to the lake or spends LLM budget. Set the variable, "
            "or start with --no-auth if the port is bound to the loopback only "
            "(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')")
    if app.state.api_key is False:
        print("lake.api: WARNING — started with --no-auth, every route is open to "
              "anyone who can reach the port")
    # §8: the budget is p95 <= 5 s and the first embedding call loads the model
    # (seconds, once per process). Pay it before the port accepts anything, so no
    # request does. No try/except: a server that cannot embed cannot answer, and
    # failing at startup beats failing per request as a 503.
    if app.state.warmup and not app.state.mock:
        from .. import embed
        embed.embed_query("warm up")
    # The ingest threads: a pool for phase 1 and exactly one writer for phase 2
    # (`workers.py`). Started here and not at import, because importing this module is
    # what every self-check does and a thread pool that ingests on import would be a
    # trap. `--mock` starts none: a mock app answers frozen rows and must not open the
    # graph at all.
    if app.state.workers and not app.state.mock:
        from . import workers
        started = workers.start()
        print(f"lake.api: ingest threads {started['threads']}, recovered from the "
              f"last process: {started['recovered']}")
    try:
        yield
    finally:
        if app.state.workers and not app.state.mock:
            from . import workers
            workers.stop()


def create_app(mock: bool = False, warmup: bool = True, api_key=None,
               workers: bool = True) -> FastAPI:
    """`api_key`: `None` reads `LAKE_API_KEY` from the environment (the normal path),
    a string is the key itself, and `False` turns the check off — which is a choice
    somebody has to type, on the command line as `--no-auth` or here in a check.

    `workers=False` builds the same app without the ingest threads: the HTTP contract
    can then be checked without an article ever being fetched for real."""
    app = FastAPI(
        title="Ideas Lake — block A",
        version="0.2.0",
        summary="Ingestion + retrieve: источник → тезис → идея, и обратно по запросу.",
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.mock = mock
    app.state.warmup = warmup
    app.state.workers = workers
    # Lazy construction keeps imports and mock/self-checks free of network calls.
    app.state.research_agent = None
    # Read here, not per request: a key that changes under a running server would make
    # "it works on my machine" depend on when the request landed.
    app.state.api_key = os.environ.get("LAKE_API_KEY", "") if api_key is None else api_key
    for router in ROUTERS:
        app.include_router(router)
    # Computed once here, not per request: matching HTTP verbs is cheap, but a route's
    # `path_regex` only exists once every router above is included, and building it on
    # every request would rebuild the same dozen patterns for every read.
    app.state.open_route_matchers = _open_route_matchers(app)

    @app.get(UI_PATH, include_in_schema=False)
    def console():
        """The operator console: one static file that then talks to the routes above.

        Read from disk per request rather than baked into the module: it is a template
        file like the prompts are (`lake/prompts/`), and editing it while the server
        runs is the whole point of having it be one file. 503 rather than 500 if it is
        missing — an image built without it is a broken deployment, not a bad request.
        """
        if not UI_FILE.is_file():
            return JSONResponse(status_code=503, content={
                "error": f"console.html is missing next to app.py ({UI_FILE}); the API "
                         "itself is unaffected — use /docs"})
        return FileResponse(UI_FILE, media_type="text/html; charset=utf-8",
                            headers={"Cache-Control": "no-store"})

    @app.middleware("http")
    async def _require_key(request: Request, call_next):
        """`Authorization: Bearer <LAKE_API_KEY>` on everything but `OPEN_PATHS`.

        Middleware, not a dependency per route: a dependency is something a new route
        can be written without, and this app's routes ingest papers and rewrite ideas.
        It also runs BEFORE routing, so an unknown path answers 401 rather than 404 —
        the key is needed even to learn which paths exist.
        """
        expected = request.app.state.api_key
        # GET/HEAD only, not "this path is open": every entry in `OPEN_PATHS` is a
        # document or a static asset, and matching on the path alone let any verb
        # through to the router — which then answered 405 and told a stranger the path
        # exists. Found by the self-check the moment `/ui` was added.
        is_read = request.method in ("GET", "HEAD")
        is_open = is_read and (
            request.url.path in OPEN_PATHS
            # `OPEN_ENDPOINTS` pairs are checked against the compiled route regex, not
            # the path string, so a path parameter only opens the SHAPE a route
            # actually answers (see `_open_route_matchers`).
            or any(matcher.match(request.url.path)
                  for matcher in request.app.state.open_route_matchers))
        # Checked BEFORE the "no key configured" branch below, on purpose: a read that
        # needs no key does not become gated by a misconfigured one. In practice this
        # never fires in production — `lifespan` refuses to start without a key unless
        # `--no-auth` was typed — but it is what a server built directly (tests,
        # self-check) without running `lifespan` sees, and "the key is missing" must
        # not read as "the lake is unreachable" for a route that was never behind it.
        if expected is False or is_open:
            return await call_next(request)
        if not expected:
            # Unreachable once `lifespan` has run, and 503 rather than "let it through"
            # if it ever is: a server with no key configured is broken, not open.
            return JSONResponse(status_code=503,
                                content={"error": "server started without LAKE_API_KEY"})
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        # compare_digest, not `==`: the comparison is over a secret and against
        # whatever the caller sent. Both sides encoded, so a non-ASCII header cannot
        # raise TypeError out of the middleware and become a 500.
        if scheme.lower() != "bearer" or not hmac.compare_digest(
                token.encode("utf-8"), expected.encode("utf-8")):
            return JSONResponse(status_code=401, content={"error": "missing or wrong API key"},
                                headers={"WWW-Authenticate": "Bearer"})
        return await call_next(request)

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
    """Fix the schema to what the server actually answers: no 422, a 401 on every
    operation that can actually produce one, and none on the ones in `OPEN_ENDPOINTS`
    that cannot.

    The OpenAPI document is what C integrates against. A documented status the server
    cannot produce sends the other side writing a branch that never runs; an
    undocumented one it DOES produce — 401 on every call until the header is right —
    sends it into a branch it never wrote. Since `OPEN_ENDPOINTS` opened a fixed set of
    GET operations to no key at all, documenting 401 on THOSE would be the first kind
    of lie: `security: []` on exactly those operations is what tells an OpenAPI-aware
    client it may skip the header there and nowhere else.
    """
    def openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, summary=app.summary,
                             description=app.description, routes=app.routes)
        for path, operations in schema.get("paths", {}).items():
            for method, operation in operations.items():
                if not isinstance(operation, dict):
                    continue
                operation.get("responses", {}).pop("422", None)
                if app.state.api_key is False:
                    continue
                if (method.upper(), path) in OPEN_ENDPOINTS:
                    operation["security"] = []
                else:
                    operation.setdefault("responses", {})["401"] = {
                        "description": "Нет заголовка `Authorization: Bearer "
                                       "<LAKE_API_KEY>` или ключ не тот.",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/ErrorResponse"}}}}
        for name in ("HTTPValidationError", "ValidationError"):
            schema.get("components", {}).get("schemas", {}).pop(name, None)
        if app.state.api_key is not False:
            schema.setdefault("components", {}).setdefault("securitySchemes", {})[
                "bearerAuth"] = {"type": "http", "scheme": "bearer"}
            schema["security"] = [{"bearerAuth": []}]
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
    parser.add_argument("--no-auth", action="store_true",
                        help="serve without the LAKE_API_KEY check. Only for a port bound "
                             "to 127.0.0.1: every route writes to the lake or spends LLM "
                             "budget, and there is no other authentication")
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
    uvicorn.run(create_app(mock=args.mock, api_key=False if args.no_auth else None),
                host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
