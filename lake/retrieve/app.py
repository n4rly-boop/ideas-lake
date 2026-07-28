"""HTTP layer for the read path: FastAPI over `api.retrieve` (spec 10 §5.4, §5.5).

    uvicorn lake.retrieve.app:app --port 8077
    python3 -m lake.retrieve.app --port 8077 [--mock]

Spec §5.4 specified `http.server` for zero dependencies (`09:290`). The trade the
project took instead: typed request/response models and an OpenAPI schema at
`/docs`, which is what C integrates against. `fastapi`, `uvicorn` and `pydantic`
were already installed; nothing new was added.

Three things this layer must not soften, all of them §5.4/§5.5:

1. **503 is not `ideas: []`.** A store that raised means the lake is broken and
   the answer is not data; an empty ranking from a live graph IS data for the
   A/B. `api.retrieve` decides which happened — this file only forwards it.
2. **Every request leaves exactly one log line**, 503 included. The log lives in
   `api.retrieve`, so no HTTP path can skip it. The mock is the one deliberate
   exception: frozen rows in the metrics log are contamination.
3. **Validation answers 400**, not FastAPI's default 422 — C was integrated
   against 400 before this layer existed and the body stays `{"error": ...}`.

The endpoint is a plain `def`, not `async def`: ranking is blocking work (sqlite,
numpy, one LLM call for the rewrite), so Starlette runs it in its threadpool and
the event loop stays free. `contextvars` are copied into that threadpool, which
is what keeps `trace.request` per-request (see trace.py).
"""
import argparse
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import api

MAX_QUERY_CHARS = 4000      # a query is a sentence; anything larger is not a query
MAX_K = 50                  # k is a page size, not a dump switch


# ------------------------------------------------------------------- the contract

class RetrieveRequest(BaseModel):
    """§5.4 request. `extra="forbid"`: a misspelled field must not be ignored."""
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, max_length=MAX_QUERY_CHARS,
                       description="Free-text query, phrased as a problem or as a solution.")
    k: int = Field(api.K_DEFAULT, gt=0, le=MAX_K,
                   description="How many ideas to return. Recall-first: the answer is padded "
                               "to k rather than cut by a relevance threshold (§5.5).")
    run_id: str | None = Field(None, description="Caller's run id, echoed into the trace.")
    budget: int | None = Field(None, gt=0,
                               description="max_tokens ceiling for the rewrite step (§5.1).")
    rewrite: bool = Field(True, description="Rewrite the query 'in terms of a solution' first. "
                                            "Off is the ablation arm.")
    allow_web: bool = Field(False, description="Stage III. Accepted so that turning it on later "
                                               "does not break the integration; not implemented.")

    @field_validator("query")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value

    @field_validator("allow_web")
    @classmethod
    def _web_stage_absent(cls, value: bool) -> bool:
        # Answering as if the web had been searched would be a lie about where the
        # ideas came from, and provenance is the one thing §9 never cuts.
        if value:
            raise ValueError("allow_web=true is not supported: the web stage (III) is out of "
                             "the MVP; resend with allow_web=false")
        return value


class ThesisOut(BaseModel):
    """A leaf, with the provenance the answer is required to carry (ТЗ criterion 4)."""
    text: str
    url: str
    title: str
    effect: str
    locator: str


class IdeaOut(BaseModel):
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
    theses: list[ThesisOut]


class Cost(BaseModel):
    tokens_in: int
    tokens_out: int
    wall_ms: float


class RetrieveResponse(BaseModel):
    ideas: list[IdeaOut]
    log_id: str
    cost: Cost


class ErrorResponse(BaseModel):
    error: str
    log_id: str | None = None


class Health(BaseModel):
    status: str
    mock: bool
    theses_indexed: int | None = None
    leaves_in_store: int | None = None
    in_sync: bool | None = None
    detail: str | None = None


# ----------------------------------------------------------------------- the app

@asynccontextmanager
async def lifespan(app: FastAPI):
    # §8: the budget is p95 <= 5 s and the first embedding call loads the model
    # (seconds, once per process). Pay it before the port accepts anything, so no
    # request does. No try/except: a server that cannot embed cannot answer, and
    # failing at startup beats failing per request as a 503.
    if not app.state.mock:
        from .. import embed
        embed.embed_query("warm up")
    yield


def create_app(mock: bool = False) -> FastAPI:
    app = FastAPI(
        title="Ideas Lake — retrieve",
        version="0.1.0",
        summary="Block A read path: a query in, depersonalized ideas with provenance out.",
        lifespan=lifespan,
    )
    app.state.mock = mock

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """422 -> 400, one readable line. C integrated against 400 (§5.4)."""
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"][1:]) or "body"
        return JSONResponse(status_code=400, content={"error": f"{where}: {first['msg']}"})

    @app.post("/retrieve", response_model=RetrieveResponse, responses={
        400: {"model": ErrorResponse, "description": "Malformed request."},
        503: {"model": ErrorResponse,
              "description": "The store is unreachable or raised. NOT the same as an empty "
                             "answer: `ideas: []` with 200 means the lake has nothing on this "
                             "query and is data for the A/B (§5.4)."},
    })
    def retrieve_endpoint(req: RetrieveRequest):
        if app.state.mock:
            # Frozen shape for C, before the real path exists (§9, `08:348`).
            # Writes no log line by design: mock rows are metric contamination.
            return api.MOCK_RESPONSE
        status, payload = api.retrieve(req.query, req.k, budget=req.budget,
                                       do_rewrite=req.rewrite, run_id=req.run_id)
        if status != 200:
            # Raised as a response, not an exception: `api.retrieve` has already
            # written the log line and built the {error, log_id} body.
            return JSONResponse(status_code=status, content=payload)
        return payload

    @app.get("/healthz", response_model=Health)
    def healthz() -> Health:
        """Liveness plus the one invariant that rots silently: index vs store (§6.19)."""
        if app.state.mock:
            return Health(status="ok", mock=True, detail="mock mode, no store touched")
        try:
            from .. import graph_client, index
            leaves = len(graph_client.all_theses())
            indexed = index.count()
        except Exception as exc:
            return Health(status="degraded", mock=False, detail=f"{type(exc).__name__}: {exc}")
        return Health(status="ok" if indexed == leaves else "degraded", mock=False,
                      theses_indexed=indexed, leaves_in_store=leaves, in_sync=indexed == leaves,
                      detail=None if indexed == leaves else
                      "index and store disagree — reconcile per §6.19")

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
        schema = get_openapi(title=app.title, version=app.version,
                             summary=app.summary, routes=app.routes)
        for path in schema.get("paths", {}).values():
            for operation in path.values():
                operation.get("responses", {}).pop("422", None)
        schema.get("components", {}).get("schemas", {}).pop("HTTPValidationError", None)
        schema.get("components", {}).get("schemas", {}).pop("ValidationError", None)
        app.openapi_schema = schema
        return schema

    app.openapi = openapi


app = create_app()


def selfcheck() -> None:
    """Offline: no network, no graph, no LLM, nothing left running.

    Covers what the HTTP layer alone can get wrong — the §5.4 field set, the
    400-not-422 rule, and the 503-vs-empty boundary. The ranking behind it is
    `lake.selfcheck` §6.4/§6.5.
    """
    import json
    import tempfile
    from pathlib import Path

    from fastapi.testclient import TestClient

    from . import rank

    api.RETRIEVE_LOG = Path(tempfile.mkdtemp(prefix="app-selfcheck-")) / "retrieve.jsonl"

    with TestClient(create_app(mock=True)) as client:
        body = client.post("/retrieve", json={"query": "diversity", "k": 2}).json()
        assert sorted(body) == ["cost", "ideas", "log_id"], sorted(body)
        assert sorted(body["ideas"][0]) == sorted(list(IdeaOut.model_fields)), body["ideas"][0]
        assert sorted(body["ideas"][0]["theses"][0]) == sorted(list(ThesisOut.model_fields))
        assert client.get("/healthz").json()["mock"] is True
        # Validation answers 400, never FastAPI's default 422 (§5.4, C integrated on 400).
        for payload in ({"k": 3}, {"query": "  "}, {"query": "a", "k": 0}, {"query": "a", "k": -1},
                        {"query": "a", "budget": 0}, {"query": "a", "allow_web": True},
                        {"query": "a", "unknown_field": 1}):
            answer = client.post("/retrieve", json=payload)
            assert answer.status_code == 400, (payload, answer.status_code, answer.text)
            assert "error" in answer.json(), answer.text
        assert client.post("/retrieve", content=b"{oops").status_code == 400
        assert not api.RETRIEVE_LOG.exists(), "the mock must not write to the metrics log"

    app_real = create_app()
    app_real.state.mock = False
    with TestClient(app_real) as client:
        broken = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("graph is down"))
        real_rank, rank.rank = rank.rank, broken
        try:
            answer = client.post("/retrieve", json={"query": "anything", "k": 2})
            assert answer.status_code == 503, answer.status_code
            assert set(answer.json()) == {"error", "log_id"}, answer.json()
            # A live store with nothing to say is 200 and DATA, not 503 (§5.4).
            rank.rank = lambda *a, **k: ([], {"returned": [], "cut_off": []})
            empty = client.post("/retrieve", json={"query": "nothing here", "k": 2})
            assert empty.status_code == 200 and empty.json()["ideas"] == [], empty.json()
        finally:
            rank.rank = real_rank

    lines = [json.loads(line) for line in
             api.RETRIEVE_LOG.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2, lines                 # the 503 is logged too (§5.5)
    assert "error" in lines[0] and lines[1]["returned"] == []
    print("ok: §5.4 shape, 400 not 422, 503 != empty, log written on both")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="python3 -m lake.retrieve.app")
    parser.add_argument("--port", type=int, default=8077)
    parser.add_argument("--host", default="0.0.0.0",
                        help="C calls the endpoint from another machine (default: all "
                             "interfaces); 127.0.0.1 to keep it local")
    parser.add_argument("--mock", action="store_true",
                        help="serve the frozen MOCK_RESPONSE, touch neither graph nor LLM")
    parser.add_argument("--selfcheck", action="store_true",
                        help="offline check of the HTTP layer, then exit")
    args = parser.parse_args(argv)

    if args.selfcheck:
        selfcheck()
        return

    import uvicorn
    uvicorn.run(create_app(mock=args.mock), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
