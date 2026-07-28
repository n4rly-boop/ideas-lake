"""Offline check of the HTTP layer: no network, no model load, nothing left running.

Covers what this layer alone can get wrong — the §5.4 field set, 400-not-422, the
503-vs-empty boundary, `total` on a page, the 404s that exist so an empty list
cannot double as a missing row, the single ingest slot, and the §6.19 repair.
The ranking behind it is `lake.selfcheck` §6.4/§6.5.

Everything writes to a temporary directory. The real `data/` is fingerprinted
before and after: a self-check that quietly edits the lake it is checking has
happened here once already, and the guard is cheaper than finding out later.
"""
import functools
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from .. import graph_client, index, ops, stub_store, trace
from ..ingest import run as run_mod
from ..models import (DATA, EMBED_DIM, Idea, Source, Thesis, new_idea_id, new_thesis_id,
                      source_id as make_source_id, text_hash)
from ..retrieve import api as retrieve_api, rank as rank_mod, search as search_mod
from . import jobs, schemas
from .app import create_app
from .schemas import MAX_K, MAX_PAGE, MAX_QUERY_CHARS, IdeaOut, ThesisOut

def _fingerprint() -> dict:
    """(size, mtime) of EVERY file under the real `data/`, walked, not listed.

    A list of names was the earlier version and it missed what it did not name:
    `trace` appends to `data/traces/<run_id>.jsonl` on every graph call, so each
    run left ~10 KB in the directory block D reads for cost, under a banner
    saying the real data was untouched. Walking the tree also covers `raw/`,
    `cache/` and anything a route learns to write later.
    """
    if not DATA.exists():
        return {}
    return {str(p.relative_to(DATA)): f"{p.stat().st_size}:{p.stat().st_mtime_ns}"
            for p in sorted(DATA.rglob("*")) if p.is_file()}


def _vec(text: str) -> np.ndarray:
    """Deterministic unit vector per text — the fake encoder."""
    rng = np.random.default_rng(int(text_hash(text)[:8], 16))
    vec = rng.standard_normal(EMBED_DIM).astype(np.float32)
    return vec / np.linalg.norm(vec)


def _fixture(tmp: Path) -> dict:
    """1 source, 2 ideas, 3 leaves — enough for paging, filters and a search hit."""
    url = "https://arxiv.org/abs/2405.00001"
    sid = make_source_id(url, "v1")
    graph_client.write_source(Source(id=sid, url=url, title="A Paper", type="paper",
                                     version="v1", retrieved_at="2026-07-28T10:00:00Z"))
    made = {"source_id": sid, "ideas": [], "theses": []}
    plan = [("freeze the encoder and train the head only",
             ["the frozen encoder keeps the semantics", "freezing halves the trainable params"]),
            ("evaluate candidates with a cheap proxy first",
             ["a distilled surrogate scores every candidate before the real objective"])]
    for idea_text, leaf_texts in plan:
        idea = Idea(id=new_idea_id(), text=idea_text, applicability_conditions="ac",
                    limitations="lim", failure_modes=["fm"], effect_claimed="+3 pp",
                    effect_observed="", vector=_vec(idea_text).tolist(),
                    created_at="2026-07-28T10:00:00Z", updated_at="2026-07-28T10:00:00Z")
        leaves = [Thesis(id=new_thesis_id(), source_id=sid, idea_id=idea.id, text=text,
                         context="cifar-10", effect="+3.1 pp", locator="Table 4",
                         text_hash=text_hash(text), vector=_vec(text).tolist(),
                         created_at="2026-07-28T10:00:00Z") for text in leaf_texts]
        graph_client.create_idea_with_theses(idea, sid, leaves)
        index.index_theses(leaves)
        made["ideas"].append(idea.id)
        made["theses"] += [t.id for t in leaves]
    return made


def main() -> None:
    before = _fingerprint()
    tmp = Path(tempfile.mkdtemp(prefix="lake-api-selfcheck-"))
    idx = tmp / "index.db"

    # --- bind every default path to the temp directory -----------------------
    saved = {}

    def bind(module, name, value):
        saved[(module, name)] = getattr(module, name)
        setattr(module, name, value)

    for name in ("count", "search_theses", "index_theses", "index_rows", "has", "reset",
                 "reconcile"):
        bind(index, name, functools.partial(getattr(index, name), db=idx))
    # `search.search` takes db=INDEX_DB as a DEFAULT ARGUMENT, bound at def time, and
    # `rank` imported the function by name. So patching `index` alone leaves the read
    # path reading the real `data/index.db` — which is how this check first ran, with
    # 22 live ideas answering a fixture query.
    bind(rank_mod, "search", functools.partial(search_mod.search, db=idx))
    bind(ops, "STAGING", tmp / "staging.jsonl")
    bind(ops, "STAGING_CURSOR", tmp / "staging.cursor")
    bind(ops, "PENDING_LINK", tmp / "pending_link.jsonl")
    bind(retrieve_api, "RETRIEVE_LOG", tmp / "retrieve.jsonl")
    # Every graph call is @trace'd, and trace appends to TRACES_DIR/<run_id>.jsonl.
    bind(trace, "TRACES_DIR", tmp / "traces")
    real_db, stub_store._db_path, stub_store._conn = stub_store._db_path, tmp / "lake.db", None

    fake_embed = types.ModuleType("lake.embed")
    fake_embed.embed_docs = lambda texts: np.stack([_vec(t) for t in texts])
    fake_embed.embed_query = _vec
    real_embed_mod = sys.modules.get("lake.embed")
    sys.modules["lake.embed"] = fake_embed
    setattr(sys.modules["lake"], "embed", fake_embed)
    # Swapped HERE, before anything serves: inside `_run` they were installed after
    # the app was already up, so any /ingest call before that line would have run the
    # real phase 1 — network, `data/raw/`, `data/cache/` and the real staging file,
    # which `run.py` holds as its own module-level constants and this file cannot
    # rebind from outside.
    real_phase1, real_phase2 = run_mod.phase1, run_mod.phase2
    run_mod.phase1 = lambda entries, workers=8: _unexpected("phase1")
    run_mod.phase2 = lambda limit=None: _unexpected("phase2")

    leaked: list[str] = []
    try:
        _run(tmp, idx)
    finally:
        for (module, name), value in saved.items():
            setattr(module, name, value)
        run_mod.phase1, run_mod.phase2 = real_phase1, real_phase2
        if real_embed_mod is None:
            sys.modules.pop("lake.embed", None)
            delattr(sys.modules["lake"], "embed")
        else:
            sys.modules["lake.embed"] = real_embed_mod
            setattr(sys.modules["lake"], "embed", real_embed_mod)
        con = index._CONNS.pop(str(idx), None)
        if con is not None:
            con.close()
        index._MATS.pop(str(idx), None)
        if stub_store._conn is not None:
            stub_store._conn.close()
        stub_store._db_path, stub_store._conn = real_db, None
        jobs._reset_for_tests()
        # Compared in the `finally`, not after it. Outside, a failing assertion
        # skipped the comparison entirely — so the run that leaked and the run that
        # was being debugged were usually the same run, and it never said so.
        after = _fingerprint()
        leaked = sorted(set(before) ^ set(after)) + \
            sorted(k for k in before.keys() & after.keys() if before[k] != after[k])
        if leaked:
            print(f"LEAK: the self-check wrote to real data/: {leaked}")
        shutil.rmtree(tmp, ignore_errors=True)

    # Raised only out here: with an assertion already in flight, the print above is
    # the report and re-raising would bury the failure the operator came for.
    assert not leaked, f"the self-check wrote to real data/: {leaked}"
    print("api self-check OK — real data/ untouched")


def _unexpected(what: str):
    raise AssertionError(f"the real ingest {what} was reached: this check must never "
                         f"fetch, call an LLM or write the real staging file")


def _run(tmp: Path, idx: Path) -> None:
    # ---------------------------------------------------------------- the mock
    with TestClient(create_app(mock=True, warmup=False)) as client:
        body = client.post("/retrieve", json={"query": "diversity", "k": 2}).json()
        assert sorted(body) == ["cost", "ideas", "log_id"], sorted(body)
        # Asserted against the CONSTANT, not the response: `response_model` reshapes
        # the body to exactly the model's fields before any assertion sees it, so
        # checking the response's key set proves only that pydantic filters extras.
        # The mock is what C builds against, and it has to be a real §5.4 answer.
        mock = retrieve_api.MOCK_RESPONSE
        assert sorted(mock) == ["cost", "ideas", "log_id"], sorted(mock)
        for idea in mock["ideas"]:
            assert sorted(idea) == sorted(schemas.RetrieveIdea.model_fields), sorted(idea)
            assert idea["via"] in ("thesis", "edge", "padding"), idea["via"]
            assert idea["theses"], "an idea with no leaves is not a legal answer"
            for leaf in idea["theses"]:
                assert sorted(leaf) == sorted(schemas.RetrieveThesis.model_fields)
                # Provenance is ТЗ criterion 4: blank url/title pass every key check
                # and make the answer unusable.
                assert leaf["url"].startswith("http") and leaf["title"] and leaf["locator"], leaf
        assert client.get("/healthz").json()["mock"] is True
        for payload in ({"k": 3}, {"query": "  "}, {"query": "a", "k": 0}, {"query": "a", "k": -1},
                        {"query": "a", "budget": 0}, {"query": "a", "allow_web": True},
                        {"query": "a", "unknown_field": 1},
                        {"query": "a", "k": MAX_K + 1},              # the page-size ceiling
                        {"query": "x" * (MAX_QUERY_CHARS + 1)}):     # the query ceiling
            answer = client.post("/retrieve", json=payload)
            assert answer.status_code == 400, (payload, answer.status_code, answer.text)
            assert "error" in answer.json(), answer.text
        assert client.post("/retrieve", content=b"{oops").status_code == 400
        assert not retrieve_api.RETRIEVE_LOG.exists(), "the mock must not write to the metrics log"

        # An unknown path and a wrong verb are raised by the ROUTER, as Starlette's
        # own exception class — they answered {"detail": ...} until the handler moved
        # to the parent class, and that is the one body C cannot parse.
        for answer in (client.get("/nope"), client.post("/healthz"),
                       client.delete("/ideas/whatever")):
            assert answer.status_code in (404, 405), answer.status_code
            assert set(answer.json()) == {"error"}, answer.json()

        schema = client.get("/openapi.json").json()
        operations = [(path, method, op) for path, item in schema["paths"].items()
                      for method, op in item.items() if isinstance(op, dict)]
        assert not any("422" in op.get("responses", {}) for _, _, op in operations), \
            "the schema documents a 422 this app never returns"
        # ...and the inverse, which is the same defect mirrored: a status the server
        # DOES return, missing from the document, sends C into an unhandled branch.
        for path, method, op in operations:
            documented = set(op.get("responses", {}))
            # A path parameter is a string and cannot fail validation; a query
            # parameter or a body can. So: validatable input <=> a documented 400.
            takes_input = bool(op.get("requestBody")) or \
                any(p.get("in") == "query" for p in op.get("parameters", []))
            assert takes_input == ("400" in documented), (path, method, sorted(documented))
            if path.startswith(("/sources", "/ideas", "/theses", "/search")):
                assert "503" in documented, (path, method, sorted(documented))
    print("ok: mock — §5.4 answer with real provenance, 400 not 422, {\"error\"} on 404/405, "
          "schema documents what it returns")

    # ------------------------------------------------------------- the real app
    made = _fixture(tmp)
    idea_a, idea_b = made["ideas"]
    client = TestClient(create_app(mock=False, warmup=False))
    with client:
        stats = client.get("/stats").json()
        assert (stats["sources"], stats["ideas"], stats["theses"]) == (1, 2, 3), stats
        assert stats["in_sync"] and stats["ideas_without_leaves"] == [], stats
        assert stats["edges"] == 0 and stats["job_running"] is None, stats
        assert client.get("/healthz").json()["status"] == "ok"

        # --- sources ------------------------------------------------------
        page = client.get("/sources").json()
        assert page["total"] == 1 and len(page["items"]) == 1, page
        assert page["items"][0]["id"] == made["source_id"]
        assert client.get(f"/sources/{made['source_id']}").status_code == 200
        missing = client.get("/sources/nope")
        assert missing.status_code == 404 and set(missing.json()) == {"error"}, missing.text

        # A run reported twice is one row, not two: the id is (url, version).
        run_body = {"url": "https://runs.local/run-17", "title": "evo run 17", "type": "run",
                    "version": "v1", "run_success": True, "run_meta": {"fitness_delta": 0.1}}
        first = client.post("/sources", json=run_body).json()
        again = client.post("/sources", json=run_body).json()
        assert first["id"] == again["id"] and first["run_meta"] == {"fitness_delta": 0.1}
        assert client.get("/sources").json()["total"] == 2
        assert client.post("/sources", json={**run_body, "type": "movie"}).status_code == 400
        assert client.post("/sources", json={**run_body, "oops": 1}).status_code == 400
        # `title` and `type` are read back as `source_title`/`source_type` of every leaf
        # of that source, and `source_type` is what keeps claimed apart from observed
        # (§4.6). Moving them re-writes the provenance of a frozen thesis (§1.2) through
        # a route that never touches the thesis table.
        for stolen in ({"type": "paper"}, {"title": "RETITLED"}):
            answer = client.post("/sources", json={**run_body, **stolen})
            assert answer.status_code == 409, (stolen, answer.status_code, answer.text)
        # The outcome of the run itself is exactly what a re-post is for.
        rerun = client.post("/sources", json={**run_body, "run_success": False,
                                              "run_meta": {"fitness_delta": -0.2}}).json()
        assert rerun["run_success"] is False and rerun["run_meta"]["fitness_delta"] == -0.2
        # Paging must actually move, on this listing too.
        assert client.get("/sources", params={"limit": 1}).json()["items"][0]["id"] != \
            client.get("/sources", params={"limit": 1, "offset": 1}).json()["items"][0]["id"]
        assert client.get("/sources", params={"limit": MAX_PAGE + 1}).status_code == 400
        assert client.get("/sources", params={"limit": 0}).status_code == 400

        # --- ideas --------------------------------------------------------
        page = client.get("/ideas", params={"limit": 1}).json()
        assert page["total"] == 2 and len(page["items"]) == 1, page
        assert page["items"][0]["vector"] is None, "the vector left without being asked for"
        assert sorted(page["items"][0]) == sorted(IdeaOut.model_fields)
        second = client.get("/ideas", params={"limit": 1, "offset": 1}).json()
        assert second["items"][0]["id"] != page["items"][0]["id"], "offset did not move"
        with_vec = client.get(f"/ideas/{idea_a}", params={"include_vector": True}).json()
        assert len(with_vec["vector"]) == EMBED_DIM
        assert len(with_vec["theses"]) == 2 and with_vec["theses"][0]["source_url"]
        assert client.get("/ideas/nope").status_code == 404

        # An unknown idea must not answer [] — that reads as "an idea with no leaves",
        # which is a broken invariant, not a normal answer (`06:85`).
        assert client.get(f"/ideas/{idea_a}/theses").status_code == 200
        assert len(client.get(f"/ideas/{idea_a}/theses").json()) == 2
        assert client.get("/ideas/nope/theses").status_code == 404
        assert client.get(f"/ideas/{idea_a}/neighbors").json() == []      # edges are B's
        assert client.get("/ideas/nope/neighbors").status_code == 404

        # --- patch --------------------------------------------------------
        patched = client.patch(f"/ideas/{idea_b}", json={"text": "score cheaply, then pay"}).json()
        assert patched["text"] == "score cheaply, then pay"
        assert patched["updated_at"] != "2026-07-28T10:00:00Z", patched
        stored = client.get(f"/ideas/{idea_b}", params={"include_vector": True}).json()
        assert np.allclose(stored["vector"], _vec("score cheaply, then pay"), atol=1e-6), \
            "text changed and the vector did not follow it"
        for bad in ({}, {"vector": [0.0] * EMBED_DIM}, {"id": "x"}, {"created_at": "now"},
                    # trust_score is derived from the leaves on every read, so storing
                    # it would answer 200 and never be read back — a no-op that reports
                    # success.
                    {"trust_score": 0.99},
                    # An explicit null is not "leave it alone": it used to reach the
                    # store, write NULL into a non-nullable column, and make the row
                    # unreadable — taking /ideas and /retrieve down for the whole lake
                    # with one well-formed request.
                    {"failure_modes": None}, {"limitations": None}, {"text": None},
                    {"rederived_at_leaf_count": None}):
            answer = client.patch(f"/ideas/{idea_b}", json=bad)
            assert answer.status_code == 400, (bad, answer.status_code, answer.text)
        assert client.patch(f"/ideas/{idea_b}", json={"differentiation": None}).status_code == 200
        assert client.get(f"/ideas/{idea_b}").json()["failure_modes"] == ["fm"], "the store took a NULL"
        # The same guard one layer down, so it covers every future caller and not
        # just this route (§3.4: the store owns what its columns accept).
        try:
            graph_client.update_idea(idea_b, {"limitations": None})
        except ValueError as exc:
            assert "NULL" in str(exc), exc
        else:
            raise AssertionError("stub_store.update_idea wrote NULL into a non-nullable field")
        assert client.patch("/ideas/nope", json={"text": "x"}).status_code == 404

        # --- the same two guards without HTTP ------------------------------
        # Both used to exist only inside a route, so `import graph_client` walked
        # past them and nothing said so. They live in `lake.ops` now; called as
        # functions, they must refuse and re-embed exactly as the routes do.
        for stolen in ({"type": "paper"}, {"title": "RETITLED"}):
            try:
                ops.upsert_source(**{**run_body, **stolen})
            except ops.Conflict as exc:
                assert list(stolen)[0] in str(exc), (stolen, exc)
            else:
                raise AssertionError(f"ops.upsert_source moved {list(stolen)[0]} of a source "
                                     f"whose leaves carry it as provenance")
        assert client.get(f"/sources/{first['id']}").json()["title"] == run_body["title"], \
            "the refused upsert wrote anyway"
        moved = "pay the cheap score first"
        assert ops.patch_idea(idea_b, {"text": moved})["text"] == moved
        assert np.allclose(client.get(f"/ideas/{idea_b}", params={"include_vector": True})
                           .json()["vector"], _vec(moved), atol=1e-6), \
            "ops.patch_idea wrote the text and left the vector behind"
        try:
            ops.patch_idea("nope", {"text": "x"})
        except ops.NotFound:
            pass
        else:
            raise AssertionError("ops.patch_idea invented an idea that does not exist")

        # --- theses -------------------------------------------------------
        page = client.get("/theses").json()
        assert page["total"] == 3 and len(page["items"]) == 3
        assert sorted(page["items"][0]) == sorted(ThesisOut.model_fields)
        filtered = client.get("/theses", params={"idea_id": idea_a}).json()
        assert filtered["total"] == 2 == len(filtered["items"]), filtered
        assert client.get("/theses", params={"idea_id": "nope"}).json()["total"] == 0
        by_source = client.get("/theses", params={"source_id": made["source_id"]}).json()
        assert by_source["total"] == 3 == len(by_source["items"]), by_source
        assert client.get("/theses", params={"source_id": "nope"}).json()["total"] == 0
        assert client.get("/theses", params={"idea_id": idea_a,
                                             "source_id": "nope"}).json()["total"] == 0
        assert client.get(f"/theses/{made['theses'][0]}").status_code == 200
        assert client.get("/theses/nope").status_code == 404

        # --- search and the read path -------------------------------------
        hits = client.get("/search", params={"q": "freeze the encoder", "k": 3}).json()
        assert hits and hits[0]["thesis_id"] in made["theses"], hits
        assert any(h["bm25_rank"] for h in hits), "FTS arm dead: the hybrid is cosine alone"
        assert len(client.get("/search", params={"q": "encoder", "k": 1}).json()) == 1, \
            "k is a ceiling, not a suggestion"
        assert client.get("/search", params={"q": "encoder", "k": 0}).status_code == 400
        assert client.get("/search", params={"q": ""}).status_code == 400
        answer = client.post("/retrieve", json={"query": "freeze the encoder", "k": 2,
                                                "rewrite": False})
        assert answer.status_code == 200, answer.text
        assert [i["via"] for i in answer.json()["ideas"]] == ["thesis", "thesis"], answer.json()
        assert len(json.loads(retrieve_api.RETRIEVE_LOG.read_text(encoding="utf-8")
                              .splitlines()[-1])["returned"]) == 2

        # --- 503: a broken store is not an empty answer --------------------
        real_rank = rank_mod.rank
        rank_mod.rank = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("graph is down"))
        try:
            dead = client.post("/retrieve", json={"query": "anything", "rewrite": False})
            assert dead.status_code == 503 and set(dead.json()) == {"error", "log_id"}
            rank_mod.rank = lambda *a, **k: ([], {"returned": [], "cut_off": []})
            empty = client.post("/retrieve", json={"query": "nothing", "rewrite": False})
            assert empty.status_code == 200 and empty.json()["ideas"] == [], empty.json()
        finally:
            rank_mod.rank = real_rank
        lines = [json.loads(ln) for ln in
                 retrieve_api.RETRIEVE_LOG.read_text(encoding="utf-8").splitlines()]
        assert len(lines) == 3 and "error" in lines[1], "the 503 left no log line"

        real_counts = graph_client.counts
        graph_client.counts = lambda: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked"))
        try:
            down = client.get("/stats")
            assert down.status_code == 503, down.status_code
            assert "database is locked" in down.json()["error"], down.text
            # A health check that dies tells the caller less than one that says what
            # is wrong: this path must report `degraded`, not 500 and not `ok`.
            sick = client.get("/healthz").json()
            assert sick["status"] == "degraded" and "database is locked" in sick["detail"], sick
        finally:
            graph_client.counts = real_counts

        # Anything that is neither a store error nor an HTTPException: still
        # `{"error": ...}`, never Starlette's text/plain "Internal Server Error".
        loud = TestClient(client.app, raise_server_exceptions=False)
        graph_client.counts = lambda: (_ for _ in ()).throw(TypeError("something else"))
        try:
            boom = loud.get("/stats")
            assert boom.status_code == 500, boom.status_code
            assert boom.json() == {"error": "TypeError: something else"}, boom.text
        finally:
            graph_client.counts = real_counts
        print("ok: graph — pages with a true total, 404 where [] would lie, patch re-embeds, "
              "503 != empty, 500 still says what broke; both guards hold when lake.ops is "
              "imported, not served")

        # --- §6.19 repair --------------------------------------------------
        index.reset()
        assert client.get("/healthz").json() == {"status": "degraded", "mock": False,
                                                 "theses_indexed": 0, "leaves_in_store": 3,
                                                 "in_sync": False,
                                                 "detail": "index and store disagree — "
                                                           "POST /admin/reindex (§6.19)"}
        assert client.get("/stats").json()["in_sync"] is False, "in_sync never says false"
        repaired = client.post("/admin/reindex").json()
        assert repaired == {"indexed_before": 0, "leaves_in_store": 3, "indexed_after": 3,
                            "in_sync": True}, repaired
        assert client.get("/healthz").json()["status"] == "ok"
        assert client.get("/search", params={"q": "freeze the encoder"}).json(), \
            "the index is not searchable after the repair"

        # A repair that REFUSES must leave the suspect index in place. Dropping first
        # and filling second used to empty it — and an empty index does not raise:
        # /search answers 200 [] and ranking reads that as "the lake has nothing".
        real_all = graph_client.all_theses
        rows = real_all()
        rows[1] = {**rows[1], "vector": [0.0] * EMBED_DIM}      # not L2-normalized
        graph_client.all_theses = lambda: rows
        try:
            refused = loud.post("/admin/reindex")
            assert refused.status_code == 500 and "not L2-normalized" in refused.json()["error"]
        finally:
            graph_client.all_theses = real_all
        assert index.count() == 3, f"the refused repair emptied the index: {index.count()}"
        assert client.get("/search", params={"q": "freeze the encoder"}).json(), \
            "the index stopped answering after a refused repair"
        assert client.get("/healthz").json()["status"] == "ok"
        failed_repair = client.get("/ingest/jobs").json()[0]      # newest first
        assert failed_repair["kind"] == "reindex" and failed_repair["status"] == "failed", \
            failed_repair
        assert "not L2-normalized" in failed_repair["error"], failed_repair
        assert jobs.running() is None, "the failed repair kept the slot"

        # --- the single ingest slot ----------------------------------------
        gate = threading.Event()

        def blocking(limit=None):
            assert gate.wait(10), "the job was never released"
            return {"sources_processed": 0}

        run_mod.phase2 = blocking
        started = client.post("/ingest/phase2", json={"limit": 1})
        assert started.status_code == 202, started.text
        job_id = started.json()["id"]
        assert started.json()["status"] == "running" and started.json()["args"] == {"limit": 1}
        for busy in (client.post("/ingest/phase2"),
                     client.post("/ingest/phase1", json={"sources": [{"arxiv_id": "x", "type": "paper"}]}),
                     client.post("/admin/reindex")):
            assert busy.status_code == 409, (busy.url, busy.status_code)
            assert job_id in busy.json()["error"], busy.text
        assert client.get("/stats").json()["job_running"] == job_id
        gate.set()
        job = _await(client, job_id)
        assert job["status"] == "ok" and job["error"] is None, job
        assert job["report"]["sources_processed"] == 0 and "cost" in job["report"], job

        # A job that dies says so. "running" forever, or "ok" with no report, would
        # both read as success from the outside.
        run_mod.phase1 = lambda entries, workers=8: 1 / 0
        crashed = client.post("/ingest/phase1", json={"sources": [{"arxiv_id": "x", "type": "paper"}]})
        assert crashed.status_code == 202
        job = _await(client, crashed.json()["id"])
        assert job["status"] == "failed" and job["error"].startswith("ZeroDivisionError"), job
        assert job["report"] is None and job["finished_at"], job
        assert client.post("/ingest/phase1", json={"sources": []}).status_code == 400
        assert client.get("/ingest/jobs/nope").status_code == 404
        listing = client.get("/ingest/jobs").json()
        assert len(listing) == 4, listing        # 2 reindex (ok, failed), phase2, phase1
        # Newest first, checked by identity: `created_at` has second resolution and
        # the two reindex jobs share a timestamp, so a sort on it proves nothing.
        assert [j["kind"] for j in listing] == ["phase1", "phase2", "reindex", "reindex"], listing
        assert listing[0]["status"] == "failed" and listing[-1]["status"] == "ok", listing
        assert client.get("/stats").json()["job_running"] is None

        # This route starts minutes of LLM spend: an entry nothing can fetch is a 400
        # at the door, not a job that discovers it later.
        for bad in ({"sources": [{"title": "no id and no url", "type": "paper"}]},
                    {"sources": [{"arxiv_id": "x"}]},                   # no type
                    {"sources": [{"arxiv_id": "x", "type": "movie"}]},
                    {"limit": 0}, {"workers": 0}, {"oops": 1}):
            answer = client.post("/ingest/phase1", json=bad)
            assert answer.status_code == 400, (bad, answer.status_code, answer.text)
        assert client.post("/ingest/phase2", json={"oops": 1}).status_code == 400
        # A `skip:` row is a legal entry — fetch.py refuses it by name (§4.2).
        run_mod.phase1 = lambda entries, workers=8: len(entries)
        skipped = client.post("/ingest/phase1", json={"sources": [
            {"arxiv_id": None, "url": "https://doi.org/x", "type": "paper", "skip": "no arXiv id",
             "group": "evo_search", "year": 2024}]})
        assert skipped.status_code == 202, skipped.text
        assert _await(client, skipped.json()["id"])["report"]["staging_lines"] == 1

        # --- what sits between the phases ----------------------------------
        rows = [{"source": {"id": "s1", "title": "One"}}, {"source": {"id": "s1", "title": "One"}},
                {"source": {"id": "s2", "title": "Two"}}]
        ops.STAGING.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        ops.STAGING_CURSOR.write_text("2\n", encoding="utf-8")
        staging = client.get("/ingest/staging").json()
        assert staging["lines"] == 3 and staging["cursor"] == 2 and staging["pending_lines"] == 1
        assert staging["sources"] == [{"id": "s1", "title": "One", "lines": 2, "ingested": 2},
                                      {"id": "s2", "title": "Two", "lines": 1, "ingested": 0}]
        assert client.get("/stats").json()["staging_lines"] == 3

        # The three states this endpoint used to answer 200 or 500 for, all of them
        # corruption an operator opens it precisely to see.
        ops.STAGING_CURSOR.write_text("99\n", encoding="utf-8")
        past_end = client.get("/ingest/staging")
        assert past_end.status_code == 503 and "past the 3 lines" in past_end.json()["error"], \
            past_end.text
        ops.STAGING_CURSOR.write_text("garbage\n", encoding="utf-8")
        for answer in (client.get("/ingest/staging"), client.get("/stats")):
            assert answer.status_code == 503 and "garbage" in answer.json()["error"], answer.text
        ops.STAGING_CURSOR.write_text("2\n", encoding="utf-8")
        ops.STAGING.write_text(ops.STAGING.read_text(encoding="utf-8") + '{"trunc',
                                  encoding="utf-8")
        torn = client.get("/ingest/staging")
        assert torn.status_code == 503 and "staging.jsonl:4" in torn.json()["error"], torn.text

        entries = [{"ts": f"2026-07-28T10:0{n}:00+00:00", "run_id": f"r{n}",
                    "error": f"LLMError: no answer {n}",
                    "candidates": [{"idea_id": "a"}, {"idea_id": "b"}][:n],
                    "staging_line": {"source": {"id": "s1"},
                                     "thesis": {"text": f"statement {n}"}}} for n in (1, 2)]
        ops.PENDING_LINK.write_text("".join(json.dumps(e) + "\n" for e in entries),
                                       encoding="utf-8")
        pending = client.get("/ingest/pending-link").json()
        assert pending[-1] == {"ts": "2026-07-28T10:02:00+00:00", "run_id": "r2",
                               "error": "LLMError: no answer 2", "thesis_text": "statement 2",
                               "source_id": "s1", "candidates": 2}, pending
        assert len(pending) == 2, pending
        newest = client.get("/ingest/pending-link", params={"limit": 1}).json()
        assert len(newest) == 1 and newest[0]["run_id"] == "r2", newest
        assert client.get("/stats").json()["pending_link"] == 2
        print("ok: ingest — one slot, 409 on the second, a dead job says failed, "
              "a corrupt cursor or staging line is named, not swallowed")

        # --- one definition of a leaf, everywhere --------------------------
        # A thesis whose source row is gone is invisible to /theses, /ideas and
        # /retrieve. It must be invisible to the counts as well, or /stats and
        # /healthz report `in_sync: false` forever over rows nobody can reach.
        with stub_store._lock:
            con = stub_store._c()
            with con:
                con.execute("DELETE FROM source WHERE id=?", (made["source_id"],))
        orphaned = client.get("/stats").json()
        assert orphaned["theses"] == 0 and orphaned["sources"] == 1, orphaned
        assert sorted(orphaned["ideas_without_leaves"]) == sorted(made["ideas"]), orphaned
        assert client.get("/theses").json()["total"] == 0
        assert client.get(f"/ideas/{idea_a}").json()["theses"] == []
        assert client.post("/admin/reindex").json() == {
            "indexed_before": 3, "leaves_in_store": 0, "indexed_after": 0, "in_sync": True}
        print("ok: a leaf is a thesis with a source — the pages, the counts and the "
              "invariant check agree on it")


def _await(client, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/ingest/jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} never finished")


if __name__ == "__main__":
    main()
