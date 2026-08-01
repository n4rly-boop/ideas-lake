"""Offline check of the HTTP layer: no network, no model load, nothing left running.

Covers what this layer alone can get wrong — the §5.4 field set, 400-not-422, the
503-vs-empty boundary, `total` on a page, the 404s that exist so an empty list
cannot double as a missing row, the single ingest slot, and the §6.19 repair.
The ranking behind it is `lake.selfcheck` §6.4/§6.5.

Everything writes to a temporary directory. The real `data/` is fingerprinted
before and after: a self-check that quietly edits the lake it is checking has
happened here once already, and the guard is cheaper than finding out later.
"""
import csv
import functools
import json
import os
import re
import shutil
import subprocess
import sqlite3
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from .. import (graph_client, index, neo4j_store, ops, queue, trace, vault as vault_mod,
                writer_lock)
from .. import models as models_mod
from ..ingest import run as run_mod
from ..models import (DATA, EMBED_DIM, Idea, Source, Thesis, new_idea_id, new_thesis_id,
                      source_id as make_source_id, text_hash)
from ..retrieve import api as retrieve_api, rank as rank_mod, search as search_mod
from . import app as app_mod, jobs, schemas, workers
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


def _fetch_dir() -> list[str] | None:
    """What is in the real `data/fetch/` right now — `None` if it does not exist."""
    fetch_dir = DATA / "fetch"
    return sorted(p.name for p in fetch_dir.iterdir()) if fetch_dir.exists() else None


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


def main() -> int:
    """Exit code, not `None`: `SKIPPED` (no Neo4j) and `REFUSED` (not empty) must
    both leave a nonzero code behind them, or `if __name__ == "__main__": main()`
    reads the process's own $? as 0 regardless — a check that never ran a single
    assertion looking exactly like one that ran and passed, in CI or by hand.
    D11 made this reachable for the first time: before it, the isolated fixture
    store meant nothing could ever skip or refuse in the first place."""
    before = _fingerprint()
    tmp = Path(tempfile.mkdtemp(prefix="lake-api-selfcheck-"))
    idx = tmp / "index.db"

    # --- bind every default path to the temp directory -----------------------
    saved = {}

    def bind(module, name, value):
        saved[(module, name)] = getattr(module, name)
        setattr(module, name, value)

    # `dial` belongs on this list for the same reason as the rest: unbound, `GET /dial`
    # in this check answered out of the REAL `data/index.db` — 3 040 live leaves under a
    # fixture query — and the only reason it showed up was an assert that compared the
    # two counts.
    for name in ("count", "fts_count", "search_theses", "dial", "index_theses", "index_rows",
                 "has", "reset", "reconcile"):
        bind(index, name, functools.partial(getattr(index, name), db=idx))
    # `search.search` takes db=INDEX_DB as a DEFAULT ARGUMENT, bound at def time, and
    # `rank` imported the function by name. So patching `index` alone leaves the read
    # path reading the real `data/index.db` — which is how this check first ran, with
    # 22 live ideas answering a fixture query.
    bind(rank_mod, "search", functools.partial(search_mod.search, db=idx))
    bind(ops, "STAGING", tmp / "staging.jsonl")
    bind(ops, "STAGING_CURSOR", tmp / "staging.cursor")
    bind(ops, "PENDING_LINK", tmp / "pending_link.jsonl")
    # `queue.py`'s own db, not part of format B (queue.py:1-30) — bound the
    # same way, or `_fingerprint()` catches this check writing to the real
    # `data/jobs.db` the moment any test touches `/fetch` or the durable queue.
    bind(queue, "DB", tmp / "jobs.db")
    # `run.phase2` takes the writer lock now (§4.5). Phase 2 is stubbed out in this
    # check, so nothing should reach it — bound anyway, because "should" is what
    # `_fingerprint` exists to distrust, and taking the real lock would also refuse a
    # live API process its writer.
    bind(writer_lock, "LOCK_PATH", tmp / "writer.lock")
    bind(retrieve_api, "RETRIEVE_LOG", tmp / "retrieve.jsonl")
    # `export(dest=DATA / "vault")` binds the real folder as a DEFAULT ARGUMENT at def
    # time — the same trap as `search.search` above, and here the blast radius is a
    # directory this check would rewrite while an operator has it open in Obsidian.
    bind(vault_mod, "export", functools.partial(vault_mod.export, dest=tmp / "vault"))
    # Every graph call is @trace'd, and trace appends to TRACES_DIR/<run_id>.jsonl.
    bind(trace, "TRACES_DIR", tmp / "traces")
    # D11 removed the isolated store this check used to swap in (a fresh SQLite
    # file). Neo4j has no equivalent disposable target, so the fixture below is
    # written into whatever `NEO4J_URI` names for real — guarded by the same two
    # checks `neo4j_store`'s own self-check uses: the host must be local/scratch
    # (`neo4j_store._require_local_target`) and the graph must be confirmed empty
    # (`MATCH (n)`, not just the labels this file reads) before anything is written.
    # Everything is wiped again in the `finally`, safe exactly because the
    # emptiness was already confirmed.
    # BLOCKER (review 2026-07-31): checked `os.environ.get("NEO4J_URI")` here and
    # unconditionally `DETACH DELETE`d below — one variable checked, a different
    # one (whatever the driver connected with, possibly set before an env change)
    # wiped. `_get_driver()` first, then the URI it actually snapshotted
    # (`neo4j_store._uri`), same as `lake.selfcheck._wipe_graph`.
    try:
        neo4j_store._get_driver()  # so `_uri` below reflects what the driver really used
        neo4j_store._require_local_target(neo4j_store._uri)
        with neo4j_store._session() as _s:
            _existing = _s.execute_read(
                lambda tx: tx.run("MATCH (n) RETURN count(n) AS c").single()["c"])
    except graph_client.STORE_ERRORS as exc:
        print(f"SKIPPED: no Neo4j reachable at {os.environ.get('NEO4J_URI')} "
              f"({type(exc).__name__}: {exc}). Bring one up with "
              "`docker compose up -d neo4j` and rerun.")
        return 1
    if _existing:
        print(f"REFUSED: the graph is not empty ({_existing} node(s) total) — this "
              "self-check only ever runs against an empty scratch instance, never one "
              "that might hold data it did not create. Point NEO4J_URI at an empty "
              "instance and rerun.")
        return 1

    # `RUN_DIR` is the converter's own addition to `lake.models` (`13` build note 2),
    # landing in a parallel change. Bound the normal way if it is already there; set
    # for the duration of this run and removed again — not left as `None`, which
    # `bind`'s blanket restore would do — if it is not, so this check proves the
    # `/run` contract offline without waiting on that file to exist.
    run_dir_existed = hasattr(models_mod, "RUN_DIR")
    if run_dir_existed:
        bind(models_mod, "RUN_DIR", tmp / "run")
    else:
        models_mod.RUN_DIR = tmp / "run"

    # `lake.ingest.runlog` is the other agent's file (§2.5 contract) and does not
    # exist yet either. Faked the same way `lake.embed` is below: a module object
    # registered in `sys.modules` AND set as an attribute of its parent package,
    # because `from ..ingest import runlog` resolves through both.
    #
    # `payload_from_csv` is captured from the REAL module first, before the fake
    # replaces it in `sys.modules`: it is pure CSV parsing (no LLM, no embed), and
    # MAJOR 3's round-trip check needs the actual converter's output, not a stub's.
    from ..ingest import runlog as real_runlog_module
    real_payload_from_csv = real_runlog_module.payload_from_csv
    fake_runlog = types.ModuleType("lake.ingest.runlog")
    fake_runlog.from_payload = lambda payload, staging_path=None, **kw: _unexpected(
        "runlog.from_payload")
    fake_runlog.drain_run = lambda staging_path, staged=None: _unexpected("runlog.drain_run")
    real_runlog_mod = getattr(sys.modules["lake.ingest"], "runlog", None)
    sys.modules["lake.ingest.runlog"] = fake_runlog
    setattr(sys.modules["lake.ingest"], "runlog", fake_runlog)

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
    real_ingest_one = run_mod.ingest_one
    real_stage_one, real_drain_one = run_mod.stage_one, run_mod.drain_one
    run_mod.phase1 = lambda entries, workers=8: _unexpected("phase1")
    # `staging_path` first and positional: `ingest_one` calls `phase2(staging_path)`,
    # and a stub shaped `(limit=None)` would take a Path as the limit the day the real
    # `ingest_one` is let into this check.
    run_mod.phase2 = lambda staging_path=None, limit=None: _unexpected("phase2")
    # /fetch runs BOTH phases in one job, so it is the one route that would fetch from
    # arXiv and write `data/fetch/` if it slipped past the stubs.
    run_mod.ingest_one = lambda entry, staging_path: _unexpected("ingest_one")
    # `/fetch` now runs through these two instead of `ingest_one` — `api/workers.py`'s
    # `fetch_step`/`write_step`, which `_run` drives by hand once the fakes it wants
    # are installed. Stubbed here too so no path through the durable queue can reach
    # a real fetch before that point.
    run_mod.stage_one = lambda entry, staging_path: _unexpected("stage_one")
    run_mod.drain_one = lambda staging_path, staged=None: _unexpected("drain_one")

    leaked: list[str] = []
    try:
        _run(tmp, idx, real_payload_from_csv)
    finally:
        for (module, name), value in saved.items():
            setattr(module, name, value)
        run_mod.phase1, run_mod.phase2 = real_phase1, real_phase2
        run_mod.ingest_one = real_ingest_one
        run_mod.stage_one, run_mod.drain_one = real_stage_one, real_drain_one
        if not run_dir_existed:
            del models_mod.RUN_DIR
        if real_runlog_mod is None:
            sys.modules.pop("lake.ingest.runlog", None)
            delattr(sys.modules["lake.ingest"], "runlog")
        else:
            sys.modules["lake.ingest.runlog"] = real_runlog_mod
            setattr(sys.modules["lake.ingest"], "runlog", real_runlog_mod)
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
        # The graph was confirmed empty above, so wiping it outright on the way out
        # cannot touch anything this run did not itself write.
        with neo4j_store._session() as _s:
            _s.execute_write(lambda tx: tx.run("MATCH (n) DETACH DELETE n").consume())
        jobs._reset_for_tests()
        queue.close()          # drop the cached handle on the temp db before it is rmtree'd
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
    return 0


def _unexpected(what: str):
    raise AssertionError(f"the real ingest {what} was reached: this check must never "
                         f"fetch, call an LLM or write the real staging file")


def _run(tmp: Path, idx: Path, real_payload_from_csv) -> None:
    # ---------------------------------------------------------------- the mock
    # `api_key=False` here and below: these two blocks check the shape of the API, and
    # the key gets its own block at the end, against a server that has one.
    with TestClient(create_app(mock=True, warmup=False, api_key=False, workers=False)) as client:
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
        # The mock says "no store touched" and starts no workers. `/fetch` is the one
        # route that would make both claims false: accepted, it writes a row into the
        # real `data/jobs.db` that nothing in this process will ever claim — a 202 for
        # work silently dropped.
        mock_fetch = client.post("/fetch", json={"url": "https://arxiv.org/abs/2406.04824"})
        assert mock_fetch.status_code == 503, mock_fetch.text
        assert "mock" in mock_fetch.json()["error"], mock_fetch.text
        assert queue.counts()["queued"] == 0, "the mock /fetch enqueued a job"
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
    # D14: `_fixture`'s ideas would otherwise sit at the judge's 0.0 default
    # (unjudged, not "judged and found untrustworthy") — this check is about the
    # HTTP layer, not the quota (that is `rank.demo`'s job), so both are marked
    # judged here to keep `/retrieve`'s `via` unaffected by trust_score == 0.
    for idea_id in made["ideas"]:
        graph_client.set_trust(idea_id, 0.8)
    client = TestClient(create_app(mock=False, warmup=False, api_key=False, workers=False))
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
        assert client.get(f"/ideas/{idea_a}/neighbors").json() == []      # none written yet
        assert client.get("/ideas/nope/neighbors").status_code == 404

        # D12 review, 2026-07-31: a REAL co-citation edge through this same route used
        # to 500 on FastAPI response validation — `EdgeOut.evidence` was `str | None`
        # while `write_cocitation_edges` writes the LIST of contributing source ids
        # (`neo4j_store._COCITE_UPSERT`). `_fixture`'s `idea_a`/`idea_b` already share
        # one source (2 leaves / 1 leaf, BLOCKER 2's own shape — not 2 leaves each),
        # so writing the edge here and hitting `/neighbors` for real is what proves the
        # schema fix, not just a unit check on the model in isolation.
        cocite_outcomes = graph_client.write_cocitation_edges(made["source_id"])
        assert len(cocite_outcomes) == 1 and cocite_outcomes[0]["missing"] is False, \
            cocite_outcomes
        answer = client.get(f"/ideas/{idea_a}/neighbors")
        assert answer.status_code == 200, answer.text     # used to be 500 (review)
        edges = answer.json()
        assert len(edges) == 1 and edges[0]["target_id"] == idea_b, edges
        assert edges[0]["type"] == "related_via_source", edges
        assert isinstance(edges[0]["evidence"], list) and edges[0]["evidence"] == \
            [made["source_id"]], edges
        assert client.get("/stats").json()["edges"] == 2, "one pair, both directions"
        print("ok: GET /ideas/{id}/neighbors serializes a real co-citation edge instead "
              "of 500ing on response validation (D12 review, 2026-07-31)")

        # --- patch --------------------------------------------------------
        patched = client.patch(f"/ideas/{idea_b}", json={"text": "score cheaply, then pay"}).json()
        assert patched["text"] == "score cheaply, then pay"
        assert patched["updated_at"] != "2026-07-28T10:00:00Z", patched
        stored = client.get(f"/ideas/{idea_b}", params={"include_vector": True}).json()
        assert np.allclose(stored["vector"], _vec("score cheaply, then pay"), atol=1e-6), \
            "text changed and the vector did not follow it"
        for bad in ({}, {"vector": [0.0] * EMBED_DIM}, {"id": "x"}, {"created_at": "now"},
                    # `trust_score` and `dirty` move together, in one update by the
                    # judge (`13` §3.2-3.3). Writing either one alone over HTTP leaves
                    # the pair inconsistent — an idea clean with a stale score, or a
                    # score nothing measured. `origin` is a fact of creation, not a
                    # field to be edited later.
                    {"trust_score": 0.99}, {"dirty": False}, {"origin": "synthesized"},
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
            raise AssertionError("neo4j_store.update_idea wrote NULL into a non-nullable field")
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
        # --- /dial: the same numbers as /search, and nothing written ------
        # The claim this route is sold on — a visitor's phrase costs nothing and pollutes
        # nothing — is exactly the kind that is true until someone adds a log line.
        log_before = (retrieve_api.RETRIEVE_LOG.read_text(encoding="utf-8")
                      if retrieve_api.RETRIEVE_LOG.exists() else "")
        placed = client.get("/dial", params={"q": "freeze the encoder", "k": 3})
        assert placed.status_code == 200, placed.text
        body = placed.json()
        assert body["total"] == len(body["points"]) == index.count(), body["total"]
        assert [h["thesis_id"] for h in body["hits"]] == [h["thesis_id"] for h in hits], body
        assert all(h["text"] for h in body["hits"]), "a hit with no text needs the graph to read"
        assert client.get("/dial", params={"q": ""}).status_code == 400
        assert client.get("/dial", params={"q": "x", "k": 0}).status_code == 400
        assert (retrieve_api.RETRIEVE_LOG.read_text(encoding="utf-8")
                if retrieve_api.RETRIEVE_LOG.exists() else "") == log_before, \
            "/dial wrote into the A/B measurement log"

        answer = client.post("/retrieve", json={"query": "freeze the encoder", "k": 2,
                                                "rewrite": False})
        assert answer.status_code == 200, answer.text
        assert [i["via"] for i in answer.json()["ideas"]] == ["thesis", "thesis"], answer.json()
        retrieve_line = json.loads(retrieve_api.RETRIEVE_LOG.read_text(encoding="utf-8")
                                   .splitlines()[-1])
        assert len(retrieve_line["returned"]) == 2
        # D14: both candidates are judged (trusted) above, so k=2's quota (0) never
        # engages — the log must say so, not just stay silent about it.
        assert retrieve_line["trust_quota"] == 0, retrieve_line
        assert (retrieve_line["untrusted_returned"], retrieve_line["untrusted_over_quota"]) \
               == (0, 0), retrieve_line

        # --- 503: a broken store is not an empty answer --------------------
        real_rank = rank_mod.rank
        rank_mod.rank = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("graph is down"))
        try:
            dead = client.post("/retrieve", json={"query": "anything", "rewrite": False})
            assert dead.status_code == 503 and set(dead.json()) == {"error", "log_id"}
            rank_mod.rank = lambda *a, **k: ([], {"returned": [], "cut_off": [],
                                                  "trust_quota": 0, "untrusted_returned": 0,
                                                  "untrusted_over_quota": 0})
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

        # --- 2026-07-31 finding: `thesis_fts` dropped/emptied on its own ----
        # `idx_thesis` still agrees with the store (`indexed == leaves` stays true),
        # which is exactly why `/healthz`'s original check missed this: the hybrid
        # had silently degraded to cosine alone (every hit's bm25_rank stuck at
        # null) and nothing on the health surface said so.
        con = index._con(idx)
        con.execute("DELETE FROM thesis_fts")
        con.commit()
        sick_fts = client.get("/healthz").json()
        assert sick_fts["status"] == "degraded" and sick_fts["in_sync"] is True, sick_fts
        assert "thesis_fts" in sick_fts["detail"], sick_fts
        assert client.get("/stats").json()["in_sync"] is False, \
            "stats must not call the lake in_sync while thesis_fts is empty"
        # And the read path itself: a broken index must not answer as an empty or
        # degraded one — /search and /retrieve both raise through the store-error
        # path (`sqlite3.DatabaseError`), not silently answer on cosine alone.
        broken_search = client.get("/search", params={"q": "freeze the encoder"})
        assert broken_search.status_code == 503, broken_search.text
        assert "thesis_fts" in broken_search.json()["error"], broken_search.text
        broken_retrieve = client.post("/retrieve", json={"query": "freeze the encoder",
                                                          "rewrite": False})
        assert broken_retrieve.status_code == 503, broken_retrieve.text
        repaired_fts = client.post("/admin/reindex").json()
        assert repaired_fts == {"indexed_before": 3, "leaves_in_store": 3,
                                "indexed_after": 3, "in_sync": True}, repaired_fts
        assert client.get("/healthz").json()["status"] == "ok"
        healed = client.get("/search", params={"q": "freeze the encoder"}).json()
        assert healed and any(h["bm25_rank"] for h in healed), \
            "the repair must refill thesis_fts too, not just idx_thesis"

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

        def blocking(staging_path=None, limit=None):
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
        # /fetch no longer shares this slot at all (§4.5's own exception,
        # `routes.fetch_article`): it has a queue behind it, so it answers 202 while
        # the slot above is busy, and the article waits its turn for a fetch worker.
        queued_while_busy = client.post("/fetch", json={"url": "https://arxiv.org/abs/2406.04824"})
        assert queued_while_busy.status_code == 202, queued_while_busy.text
        assert queued_while_busy.json()["status"] == "queued", queued_while_busy.json()
        # Removed rather than left to drain: the queue is durable and FIFO, and a row
        # left here would be the OLDEST queued job by the time `fetch_step` is driven
        # by hand in the /fetch section below, jumping ahead of that section's own job.
        with queue._LOCK:
            queue._con().execute("DELETE FROM job WHERE id = ?",
                                 (queued_while_busy.json()["id"],))
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
        # 3 reindex (ok, ok, failed — the extra ok is the thesis_fts-only repair
        # above), phase2, phase1.
        assert len(listing) == 5, listing
        # Newest first, checked by identity: `created_at` has second resolution and
        # the reindex jobs share a timestamp, so a sort on it proves nothing.
        assert [j["kind"] for j in listing] == \
            ["phase1", "phase2", "reindex", "reindex", "reindex"], listing
        assert listing[0]["status"] == "failed" and listing[-1]["status"] == "ok", listing
        assert client.get("/stats").json()["job_running"] is None

        # --- /admin/trust: operator-triggered judging pass (13 §3.3 review note) ---
        # Queued like phase1/phase2 (dozens of 35B calls, same cost profile as phase
        # 2's own end-of-pass step), unlike the synchronous /admin/reindex above.
        # `trust.run_pass` is stubbed the same way `run_mod.phase1/phase2` are: this
        # check must never call the school's LLM.
        from ..ingest import trust as trust_mod
        real_run_pass = trust_mod.run_pass
        seen_trust: dict = {}

        def fake_run_pass(idea_ids=None):
            seen_trust["idea_ids"] = idea_ids
            return {"trust_scored": 1, "trust_failed": 0, "trust_leaves_capped": 0,
                    "trust_errors": [], "trust_mean": 0.7, "trust_due": 1, "trust_deferred": 0}

        trust_mod.run_pass = fake_run_pass
        try:
            named = client.post("/admin/trust", json={"idea_ids": [idea_a]})
            assert named.status_code == 202, named.text
            assert named.json()["kind"] == "trust" and named.json()["args"] == {
                "idea_ids": [idea_a]}, named.text
            named_job = _await(client, named.json()["id"])
            assert named_job["status"] == "ok" and named_job["report"]["trust_scored"] == 1, \
                named_job
            assert seen_trust["idea_ids"] == [idea_a], \
                "a named idea must reach run_pass exactly as posted"

            # Body omitted entirely: `idea_ids` must reach `run_pass` as None (whatever
            # is already dirty), never as an empty list — the two mean different things.
            omitted = client.post("/admin/trust")
            assert omitted.status_code == 202, omitted.text
            _await(client, omitted.json()["id"])
            assert seen_trust["idea_ids"] is None, seen_trust

            # Empty idea_ids is refused at the door, same reasoning as IdeaPatch's
            # empty patch: "name at least one, or omit the field" — not silently
            # treated as either "all" or "none".
            assert client.post("/admin/trust", json={"idea_ids": []}).status_code == 400
            assert client.post("/admin/trust", json={"oops": 1}).status_code == 400
        finally:
            trust_mod.run_pass = real_run_pass
        print("ok: /admin/trust — named ideas or (idea_ids omitted) whatever is "
              "already dirty, queued like phase1/phase2 rather than blocking like "
              "/admin/reindex, empty idea_ids and an unknown field both 400")

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

        # --- /fetch: one url, both phases ----------------------------------
        fetch_dir_before = _fetch_dir()
        # The same door as /ingest/phase1 and for the same reason: this route starts a
        # fetch and minutes of LLM spend, so a link that is not an arXiv article is a
        # 400 now, not a job that discovers it later.
        for bad in ({"url": "https://openreview.net/forum?id=x"}, {"url": "not a url"},
                    {"url": "https://arxiv.org/list/cs.LG/2406"}, {"url": ""},
                    {"url": "https://arxiv.org.evil.com/abs/2406.04824"},
                    # arXiv's own listing links carry these, and the anchored regex is
                    # the only thing between them and a cache key: 400, not a 500 from
                    # somewhere inside the fetch.
                    {"url": "https://arxiv.org/abs/2406.04824?context=cs.LG"},
                    {"url": "https://arxiv.org/abs/2406.04824#S3"},
                    # Old-style ids look like arXiv links and cannot be fetched at all.
                    {"url": "https://arxiv.org/abs/hep-th/9901001"},
                    {"arxiv_id": "2406.04824"},          # only `url` is the contract
                    {"url": "https://arxiv.org/abs/2406.04824", "type": "paper"}):
            answer = client.post("/fetch", json=bad)
            assert answer.status_code == 400, (bad, answer.status_code, answer.text)
            assert set(answer.json()) == {"error"}, answer.text
        assert "Old-style" in client.post(
            "/fetch", json={"url": "https://arxiv.org/abs/hep-th/9901001"}).json()["error"]
        assert jobs.running() is None, "a refused /fetch took the slot"

        # This app was built with workers=False (task item 4): nothing claims a queued
        # job on its own, so both halves are driven by hand — `fetch_step`/`write_step`
        # are exactly what the real fetch pool and the writer thread call in a loop
        # (`api/workers.py`), one claim each.
        seen: dict = {}

        def fake_stage_one(entry, staging_path):
            seen.update(entry=entry, stage_staging=Path(staging_path))
            return {"staging_lines": 2, "leakage": 0, "theses_dropped": 0,
                    "source": entry["arxiv_id"]}

        def fake_drain_one(staging_path, staged=None):
            seen.update(drain_staging=Path(staging_path), staged=staged)
            return {"sources_processed": 1, "theses_written": 2, "staging_lines": 2}

        run_mod.stage_one = fake_stage_one
        run_mod.drain_one = fake_drain_one
        fetched = client.post("/fetch", json={"url": "https://arxiv.org/pdf/2406.04824v2.pdf"})
        assert fetched.status_code == 202, fetched.text
        assert fetched.json()["kind"] == "fetch" and fetched.json()["status"] == "queued", \
            fetched.text
        assert fetched.json()["args"] == {"url": "https://arxiv.org/pdf/2406.04824v2.pdf",
                                          "arxiv_id": "2406.04824v2"}, fetched.text
        job_id = fetched.json()["id"]

        assert workers.fetch_step() is True, "phase 1 step found nothing queued to claim"
        staged = client.get(f"/ingest/jobs/{job_id}").json()
        assert staged["status"] == "staged" and staged["stage"] == "phase1", staged
        assert workers.write_step() is True, "phase 2 step found nothing staged to claim"
        job = client.get(f"/ingest/jobs/{job_id}").json()
        assert job["status"] == "ok" and job["report"]["theses_written"] == 2, job
        # Phase 1's own report reaches phase 2 across the queue row: `leakage` and
        # `theses_dropped` are measured by the half that parsed the article and cannot be
        # recomputed by the half that links it, so a writer that dropped them would
        # report the linking as the whole article (and `cost` as ~1/50 of it).
        assert seen["staged"]["staging_lines"] == 2 and seen["staged"]["leakage"] == 0, seen
        assert seen["staged"]["source"] == "2406.04824v2", seen
        assert job["attempts"] == 1, job        # `stage()` gives phase 2 its own lives
        # The version in the link is the version fetched: `Source.id = sha1(url + version)`,
        # so dropping it would file v2's theses under v1's source (§4.8).
        assert seen["entry"] == {"arxiv_id": "2406.04824v2", "type": "paper"}, seen
        # Its own staging file, not the corpus one — sharing it would replay every
        # source still waiting for acceptance (§4.7, `run.stage_one`/`run.drain_one`).
        assert seen["stage_staging"] == DATA / "fetch" / "2406.04824v2.jsonl", seen
        assert seen["stage_staging"] != ops.STAGING and seen["stage_staging"].parent != DATA, seen
        assert seen["drain_staging"] == seen["stage_staging"], seen
        run_mod.stage_one = lambda entry, staging_path: _unexpected("stage_one")
        run_mod.drain_one = lambda staging_path, staged=None: _unexpected("drain_one")
        # Not asserted as "data/fetch is empty": a real /fetch that failed leaves its
        # article there on purpose, and this check would then fail over somebody else's
        # run. Compared as a delta instead — the same shape as `_fingerprint`.
        assert _fetch_dir() == fetch_dir_before, "the stubbed /fetch touched data/fetch"
        print("ok: /fetch — a non-arXiv link is 400 at the door, the version survives, "
              "the article gets its own staging file, fetch_step/write_step drain the "
              "queue exactly as the real fetch pool and writer would")

        # --- /run: a batch of mutants, one job per run (13 §2.5) ------------
        def _mutant(program_id: str, **over) -> dict:
            base = {"program_id": program_id, "parent_ids": [], "state": "done",
                    "fitness": 0.5, "mutation_output": {
                        "archetype": "swap_layer", "justification": "j",
                        "insights_used": [],
                        "changes": [{"description": "d", "explanation": "e"}]}}
            base.update(over)
            return base

        # `limit`/`min_abs_delta` are RunRequest's own fields (§2.4) — set to
        # non-default values here so every assertion below that reads them back
        # (JobOut.args, the payload on disk, the kwargs the converter actually
        # received) proves the seam and not just that a default survived.
        run_body = {"run_id": "run-selfcheck-1", "task_id": "aime-seed1",
                   "limit": 5, "min_abs_delta": 0.2,
                   "mutants": [_mutant("p1"),
                              _mutant("p2", parent_ids=["p1"], fitness=None,
                                      mutation_output=None,
                                      mutation_output_raw='{"archetype": "y"}')]}
        run_dir_before = (sorted(p.name for p in models_mod.RUN_DIR.iterdir())
                          if models_mod.RUN_DIR.exists() else [])

        # 400 before the queue and before any spend: no run_id, empty mutants, an
        # unknown field, a mutant with no program_id, a mutant with neither form of
        # its mutation output.
        for bad in ({**run_body, "mutants": []},
                    {k: v for k, v in run_body.items() if k != "run_id"},
                    {**run_body, "oops": 1},
                    {**run_body, "mutants": [{**_mutant("p3"), "program_id": ""}]},
                    {**run_body, "mutants": [
                        {k: v for k, v in _mutant("p4").items()
                         if k != "mutation_output"}]},
                    # Same numeric-input door as every other field in this file
                    # (schemas.py): `limit` is a count, `min_abs_delta` is a
                    # non-negative threshold, and 0/negative reach a 400 before
                    # the queue, not a job that discovers it later.
                    {**run_body, "limit": 0}, {**run_body, "min_abs_delta": -0.5}):
            before_counts = queue.counts()
            answer = client.post("/run", json=bad)
            assert answer.status_code == 400, (bad, answer.status_code, answer.text)
            assert set(answer.json()) == {"error"}, answer.text
            assert queue.counts() == before_counts, f"a refused /run enqueued a job: {bad}"
        after_bad = (sorted(p.name for p in models_mod.RUN_DIR.iterdir())
                    if models_mod.RUN_DIR.exists() else [])
        assert after_bad == run_dir_before, "a refused /run wrote a payload file"

        # --- BLOCKER 1: run_id reaches a filesystem path — validated at the door, and
        # again where the path is built. Reproduced without the fix: `run_id:
        # "../../pwned"` answered 202 and created a file two directories above
        # RUN_DIR, `mkdir(parents=True)` building the way there. A slash, "..", a
        # null byte, a leading dash, and an over-long id — each 400, nothing written.
        for bad_id in ("../../pwned", "a/b", "..", "\x00evil", "-x", "x" * 100):
            before_dir = (sorted(p.name for p in models_mod.RUN_DIR.iterdir())
                         if models_mod.RUN_DIR.exists() else [])
            answer = client.post("/run", json={**run_body, "run_id": bad_id})
            assert answer.status_code == 400, (bad_id, answer.status_code, answer.text)
            assert set(answer.json()) == {"error"}, answer.text
            after_dir = (sorted(p.name for p in models_mod.RUN_DIR.iterdir())
                        if models_mod.RUN_DIR.exists() else [])
            assert after_dir == before_dir, f"a rejected run_id {bad_id!r} wrote a file"
        # Defense in depth, at the second door: `workers.payload_for` refuses a
        # traversal on its own, for a future non-HTTP caller with no request model
        # standing in front of it (the schema pattern is bypassable by any caller
        # that builds a job without going through `RunRequest`).
        try:
            workers.payload_for("../../pwned")
        except ValueError as exc:
            assert "outside RUN_DIR" in str(exc), exc
        else:
            raise AssertionError("workers.payload_for let a path traversal through")

        # `_RUN_ID_RE` is a SECOND, independent guard inside `payload_for` (the
        # resolved-path check above is the first) — and every shape the route-level
        # loop just posted is ALSO rejected by `RunRequest.run_id`'s own pattern
        # before it ever reaches this helper, so that loop cannot prove `_RUN_ID_RE`
        # does anything. These five stay INSIDE `RUN_DIR` once resolved — `"a/b"` is
        # a subdirectory, `".."` becomes the literal filename `"...json"`, `""`
        # becomes `".json"`, `".hidden"` and the 100-char id are ordinary dotfiles —
        # so the resolved-path check alone waves all of them through; only the slug
        # regex objects. Reproduced without the fix: deleting the `_RUN_ID_RE.match`
        # branch (keeping the path check) left this whole file green.
        for bad_slug in ("a/b", "..", "", ".hidden", "x" * 100):
            try:
                workers.payload_for(bad_slug)
            except ValueError as exc:
                assert "valid slug" in str(exc), (bad_slug, exc)
            else:
                raise AssertionError(f"workers.payload_for accepted {bad_slug!r} "
                                     "with no _RUN_ID_RE check to stop it")
        valid_path = workers.payload_for("run-slug-check-1")
        assert valid_path == (models_mod.RUN_DIR / "run-slug-check-1.json").resolve(), valid_path
        assert valid_path.parent.is_dir(), "payload_for must mkdir its own parent"

        # --- MAJOR 3: module == HTTP parity — the CLI's own payload must not 400 ---
        # `real_payload_from_csv` is the actual converter (`lake.ingest.runlog`,
        # captured in `main()` before the module was faked): the exact body
        # `python3 -m lake.ingest.runlog` would push over HTTP, byte for byte,
        # must clear `RunRequest`/`MutantIn` at the door. Reproduced without the
        # fix: `generation`, `iteration` and `mutation_model` — the three keys
        # `payload_from_csv` emits for every mutant — made this a 400.
        csv_path = tmp / "round_trip.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=[
                "program_id", "parent_ids", "state", "generation", "iteration",
                "metric_fitness", "metadata_mutation_model", "metadata_mutation_output"])
            writer.writeheader()
            writer.writerow({
                "program_id": "csv-p1", "parent_ids": "[]", "state": "done",
                "generation": "3", "iteration": "7", "metric_fitness": "0.5",
                "metadata_mutation_model": "qwen3.6-35b-a3b",
                "metadata_mutation_output": json.dumps({
                    "archetype": "swap_layer",
                    "changes": [{"description": "d", "explanation": "e"}]})})
        csv_payload = real_payload_from_csv(csv_path, run_id="run-selfcheck-csv")
        assert {"generation", "iteration", "mutation_model"} <= set(
            csv_payload["mutants"][0]), csv_payload
        round_trip = client.post("/run", json=csv_payload)
        assert round_trip.status_code == 202, round_trip.text
        with queue._LOCK:
            queue._con().execute("DELETE FROM job WHERE id = ?",
                                 (round_trip.json()["id"],))

        # `mutation_output_raw` accepted alongside the parsed form; the batch rides in
        # a file, not `args`; dedup by `run_id`, checked BEFORE draining so the first
        # job is still live. `lake.ingest.runlog` is stubbed the same way
        # `run.stage_one`/`drain_one` are above — offline, no real converter.
        run_seen: dict = {}

        def fake_from_payload(payload, staging_path=None, **kw):
            run_seen.update(payload=payload, staging_path=Path(staging_path), kwargs=kw)
            return {"staging_lines": len(payload["mutants"]), "rows_unparsed": 0}

        def fake_drain_run(staging_path, staged=None):
            run_seen.update(drain_staging_path=Path(staging_path), drain_staged=staged)
            return {"run_sources": 1, "run_theses": 2,
                    "staging_lines": (staged or {}).get("staging_lines", 0)}

        from ..ingest import runlog
        runlog.from_payload = fake_from_payload
        runlog.drain_run = fake_drain_run

        submitted = client.post("/run", json=run_body)
        assert submitted.status_code == 202, submitted.text
        assert submitted.json()["kind"] == "run" and submitted.json()["status"] == "queued", \
            submitted.text
        run_job_id = submitted.json()["id"]
        args = submitted.json()["args"]
        # `limit`/`min_abs_delta` must be visible in JobOut.args too, not only
        # inside the payload file: a caller polling /ingest/jobs while the job is
        # still queued or running has no other way to see what was asked for.
        assert sorted(args) == ["limit", "min_abs_delta", "payload", "payload_hash",
                                "run_id", "task_id"], args
        assert (args["run_id"], args["task_id"], args["limit"], args["min_abs_delta"]) == \
            ("run-selfcheck-1", "aime-seed1", 5, 0.2), args
        payload_path = Path(args["payload"])
        assert payload_path.exists(), "the batch body must be on disk before the job is queued"
        on_disk = json.loads(payload_path.read_text(encoding="utf-8"))
        assert on_disk["mutants"][1]["mutation_output_raw"] == '{"archetype": "y"}', on_disk
        assert (on_disk["limit"], on_disk["min_abs_delta"]) == (5, 0.2), on_disk

        again = client.post("/run", json=run_body)
        assert again.status_code == 202 and again.json()["id"] == run_job_id, \
            "the same run_id twice must not open a second job"

        # MINOR 4: the SAME run_id with a DIFFERENT body must not be served silently.
        # Reproduced without the fix: a second POST with a live run_id both got 202
        # AND overwrote the first job's payload file — the accepted batch and the
        # converted batch differed with no error anywhere.
        conflicting = client.post("/run", json={**run_body, "task_id": "a-different-task"})
        assert conflicting.status_code == 409, conflicting.text
        assert "error" in conflicting.json(), conflicting.text
        assert json.loads(payload_path.read_text(encoding="utf-8"))["task_id"] == \
            "aime-seed1", "a refused conflicting /run overwrote the live job's payload"

        assert workers.fetch_step() is True, "phase 1 found nothing queued to claim"
        staged_run = client.get(f"/ingest/jobs/{run_job_id}").json()
        assert staged_run["status"] == "staged" and staged_run["stage"] == "phase1", staged_run
        assert not payload_path.exists(), \
            "a successfully staged run must not keep its payload file"
        assert run_seen["payload"]["mutants"][0]["program_id"] == "p1", run_seen
        # DEFECT 1: `_stage_run` must read `limit`/`min_abs_delta` back off the
        # payload it just parsed and forward BOTH to `runlog.from_payload` — the
        # whole reason §2.4's ordering-by-|delta| means anything on an
        # interrupted load. Reproduced without the fix: `_stage_run` calling
        # `runlog.from_payload(payload, staging_for(job))` with neither keyword
        # left `run_seen["kwargs"] == {}` here, and every /run job over HTTP
        # converted everything regardless of what `RunRequest` asked for.
        assert run_seen["kwargs"] == {"limit": 5, "min_abs_delta": 0.2}, run_seen

        assert workers.write_step() is True, "phase 2 found nothing staged to claim"
        run_job = client.get(f"/ingest/jobs/{run_job_id}").json()
        assert run_job["status"] == "ok" and run_job["report"]["run_theses"] == 2, run_job
        assert run_seen["drain_staged"]["staging_lines"] == 2, run_seen
        assert run_seen["drain_staging_path"] == run_seen["staging_path"], run_seen

        # --- MAJOR 2 probe: a transient queue.stage() failure must not lose the ---
        # payload `_stage_run` already read — the retry needs it back. Reproduced
        # without the fix: `stage()` raising after `_stage_run` had already unlinked
        # the file burned all three attempts on a FileNotFoundError, never actually
        # retrying the parse it already knew how to do.
        probe_submit = client.post("/run", json={**run_body, "run_id": "run-selfcheck-probe"})
        assert probe_submit.status_code == 202, probe_submit.text
        probe_job_id = probe_submit.json()["id"]
        probe_payload_path = Path(probe_submit.json()["args"]["payload"])
        assert probe_payload_path.exists()

        real_stage = queue.stage

        def _boom_stage(*a, **kw):
            raise sqlite3.OperationalError("database is locked")

        queue.stage = _boom_stage
        try:
            assert workers.fetch_step() is True, "phase 1 found nothing to claim for the probe"
        finally:
            queue.stage = real_stage
        after_boom = client.get(f"/ingest/jobs/{probe_job_id}").json()
        assert after_boom["status"] == "queued", \
            f"a transient stage() failure must retry, not fail for good: {after_boom}"
        assert probe_payload_path.exists(), \
            "MAJOR 2: the payload was gone before queue.stage() committed, so the retry has nothing to read"

        assert workers.fetch_step() is True, "the retry found nothing queued to claim"
        probe_staged = client.get(f"/ingest/jobs/{probe_job_id}").json()
        assert probe_staged["status"] == "staged", probe_staged
        assert not probe_payload_path.exists(), \
            "a successfully staged run must not keep its payload file"
        assert workers.write_step() is True, "phase 2 found nothing staged for the probe"
        probe_final = client.get(f"/ingest/jobs/{probe_job_id}").json()
        assert probe_final["status"] == "ok", probe_final

        # --- terminal failure: `_fail`'s cleanup branch, and "no changes[] anywhere"
        # ends the JOB `failed`, not `ok` with zeros --------------------------------
        # MAJOR 2 (above) proved the TRANSIENT half: a retryable error keeps the
        # payload. This is the other ending `_fail` has — a PERMANENT error
        # (`_permanent`: FetchError, ValueError, KeyError) fails the job for good on
        # the FIRST attempt, and only THAT branch drops the payload file; nothing
        # above reaches it, because every failure exercised so far was transient.
        # Reproduced without the fix: deleting `_fail`'s `if final_status ==
        # "failed": cleanup(job)` branch, or making `_permanent` always return
        # `False` (nothing ever fails for good, everything retries), both left this
        # entire file green. `runlog.from_payload` raising `ValueError` is also
        # §9's fourth way to end up with an unchanged lake — "a batch in which no
        # mutant has a single changes[] ends failed with a reason, not ok with
        # zeros" (`13` §2.5) — proved here through the real queue/worker wiring, not
        # just `runlog.from_payload`'s own raise (`lake/ingest/runlog.py`'s
        # `__main__`, MAJOR 3 there).
        dead_submit = client.post(
            "/run", json={**run_body, "run_id": "run-selfcheck-nochanges"})
        assert dead_submit.status_code == 202, dead_submit.text
        dead_job_id = dead_submit.json()["id"]
        dead_payload_path = Path(dead_submit.json()["args"]["payload"])
        assert dead_payload_path.exists()

        def dying_from_payload(payload, staging_path=None, **kw):
            raise ValueError(f"runlog {payload['run_id']}: 1 mutant(s) had a "
                             "computable delta but none carried a changes[]")

        runlog.from_payload = dying_from_payload
        try:
            assert workers.fetch_step() is True, "nothing queued to claim for the dead batch"
        finally:
            runlog.from_payload = lambda payload, staging_path=None, **kw: _unexpected(
                "runlog.from_payload")
        dead_job = client.get(f"/ingest/jobs/{dead_job_id}").json()
        assert dead_job["status"] == "failed", \
            f"a batch with no changes[] anywhere must fail, not retry or report ok: {dead_job}"
        assert dead_job["report"] is None, dead_job
        assert "changes" in dead_job["error"], dead_job
        assert not dead_payload_path.exists(), \
            "a terminally failed run job must not keep its payload file (_fail's cleanup)"

        runlog.from_payload = lambda payload, staging_path=None, **kw: _unexpected(
            "runlog.from_payload")
        runlog.drain_run = lambda staging_path, staged=None: _unexpected("runlog.drain_run")

        # 429 at the ceiling, with Retry-After — same mechanism as /fetch, same queue.
        # `ceiling=0` is falsy and SKIPS the check inside `queue.enqueue` (`if
        # ceiling:`), so the ceiling needs a real queued row to count, not zero.
        filler = queue.enqueue("run", {"run_id": "run-selfcheck-filler", "task_id": None,
                                       "payload": "/does/not/matter"},
                               dedup_key="run-selfcheck-filler")
        real_queue_max = workers.QUEUE_MAX
        workers.QUEUE_MAX = queue.counts()["queued"]      # the filler, already at ceiling
        try:
            over = client.post("/run", json={**run_body, "run_id": "run-selfcheck-2"})
            assert over.status_code == 429, over.text
            assert over.headers.get("retry-after"), over.headers
            assert "error" in over.json(), over.text
            # MINOR 4: a 429 must leave no orphan — the payload is only written once
            # `queue.enqueue` has actually accepted the job (`on_accept=`).
            assert not (models_mod.RUN_DIR / "run-selfcheck-2.json").exists(), \
                "a 429'd /run left a payload file nobody will ever read"
        finally:
            workers.QUEUE_MAX = real_queue_max
            with queue._LOCK:
                queue._con().execute("DELETE FROM job WHERE id = ?", (filler["id"],))

        # --mock never accepts, and never enqueues, exactly like /fetch (checked
        # against the mock app earlier in this file — repeated against THIS run's
        # queue file to prove it is not a coincidence of the other app's temp db).
        with TestClient(create_app(mock=True, warmup=False, api_key=False,
                                   workers=False)) as mock_client:
            mock_run = mock_client.post("/run", json={**run_body,
                                                       "run_id": "run-selfcheck-mock"})
        assert mock_run.status_code == 503, mock_run.text
        assert "mock" in mock_run.json()["error"], mock_run.text
        assert not any(job["args"].get("run_id") == "run-selfcheck-mock"
                       for job in queue.listing(200)), "the mock /run enqueued a job"

        # A run job must not be claimed off a fetch-only path, and a fetch job must
        # not be claimed off a run-only one — even with BOTH sitting `queued` at the
        # same time. MINOR 5: `queue.claim` no longer takes a `kind=` filter (it was
        # a second, duplicated `UPDATE ... RETURNING` branch nothing real ever called
        # — both `fetch_step` and `write_step` claim any kind and dispatch on
        # `job["kind"]` themselves via `_STAGE`/`_DRAIN`, §2.5 build note 4). That
        # dispatch table is what is actually asserted: a fetch job and a run job,
        # queued together, each reach the RIGHT handler through `fetch_step`.
        another_fetch = client.post(
            "/fetch", json={"url": "https://arxiv.org/abs/2412.00001"}).json()
        another_run_submit = client.post(
            "/run", json={**run_body, "run_id": "run-selfcheck-dispatch"})
        assert another_run_submit.status_code == 202, another_run_submit.text
        another_run_id = another_run_submit.json()["id"]

        dispatch_seen: dict = {"fetch": False, "run": False}

        def fake_stage_one_dispatch(entry, staging_path):
            dispatch_seen["fetch"] = True
            return {"staging_lines": 1, "leakage": 0, "theses_dropped": 0,
                    "source": entry["arxiv_id"]}

        def fake_from_payload_dispatch(payload, staging_path=None, **kw):
            dispatch_seen["run"] = True
            return {"staging_lines": len(payload["mutants"]), "rows_unparsed": 0}

        run_mod.stage_one = fake_stage_one_dispatch
        runlog.from_payload = fake_from_payload_dispatch
        try:
            assert workers.fetch_step() is True
            assert workers.fetch_step() is True
        finally:
            run_mod.stage_one = lambda entry, staging_path: _unexpected("stage_one")
            runlog.from_payload = lambda payload, staging_path=None, **kw: _unexpected(
                "runlog.from_payload")
        assert dispatch_seen == {"fetch": True, "run": True}, \
            f"a fetch job or a run job reached the wrong handler: {dispatch_seen}"
        assert client.get(f"/ingest/jobs/{another_run_id}").json()["stage"] == "phase1"
        with queue._LOCK:
            queue._con().execute("DELETE FROM job WHERE id IN (?, ?)",
                                 (another_fetch["id"], another_run_id))
        print("ok: /run — a batch is one job, mutation_output_raw travels with the "
              "parsed form, the same run_id dedups while the job is still live, a "
              "different body under a live run_id is 409, 400 before the queue and "
              "before any spend, a transient stage() failure keeps the payload for "
              "the retry, 429 at the ceiling with Retry-After and no orphan payload, "
              "--mock enqueues nothing, the payload survives a pending job and is "
              "gone once staged, and fetch_step/write_step dispatch a fetch job and "
              "a run job queued together to their own handlers")

        # --- the durable queue: dedup, ceiling, restart, merged listing, health ---
        # (a) dedup: the same url twice must not open a second job.
        dedup_url = "https://arxiv.org/abs/2411.00001"
        one_fetch = client.post("/fetch", json={"url": dedup_url})
        again_fetch = client.post("/fetch", json={"url": dedup_url})
        assert one_fetch.status_code == again_fetch.status_code == 202
        assert one_fetch.json()["id"] == again_fetch.json()["id"], \
            "the same url twice must not open a second job"
        dedup_job_id = one_fetch.json()["id"]
        stats = client.get("/stats").json()
        assert stats["queue"]["queued"] >= 1, stats
        assert stats["workers"] == {}, "workers=False started no thread, so none is alive"

        # (b) 429 at the ceiling, with Retry-After — the polite way to drop a request:
        # a `queued` status nothing reaches for hours is the impolite one.
        real_queue_max = workers.QUEUE_MAX
        workers.QUEUE_MAX = queue.counts()["queued"]      # already at the ceiling
        try:
            over = client.post("/fetch", json={"url": "https://arxiv.org/abs/2411.00002"})
            assert over.status_code == 429, over.text
            assert over.headers.get("retry-after"), over.headers
            assert "error" in over.json(), over.text
        finally:
            workers.QUEUE_MAX = real_queue_max

        # (c) durability: a "restart" — a second create_app/TestClient over the same
        # jobs.db, with queue.close() between — must still answer the job's status.
        # This is the entire reason the queue is its own database and not the dict in
        # `api/jobs.py`: a caller polling a job id must not see 404 for work that is
        # still on disk (`queue.py:1-6`).
        queue.close()
        with TestClient(create_app(mock=False, warmup=False, api_key=False,
                                   workers=False)) as restarted:
            reread = restarted.get(f"/ingest/jobs/{dedup_job_id}")
            assert reread.status_code == 200, reread.text
            assert reread.json()["status"] == "queued", reread.text

        # (d) the merged listing carries both registers: the durable queue (the
        # dedup'd fetch above) and the in-process one (a fresh manual job).
        manual = client.post("/admin/reindex")
        assert manual.status_code == 200, manual.text
        merged = client.get("/ingest/jobs").json()
        assert dedup_job_id in {j["id"] for j in merged}, "the queued job is missing"
        assert "reindex" in {j["kind"] for j in merged}, "the in-process job is missing"
        # `limit` bounds the ANSWER, both registers together. Bounding only the durable
        # side inside `queue.listing()` is how a caller reads a truncated list as the
        # whole state of the lake.
        assert len(merged) > 1, merged
        assert len(client.get("/ingest/jobs", params={"limit": 1}).json()) == 1, merged
        assert client.get("/ingest/jobs", params={"limit": 0}).status_code == 400

        # One piece of work is one row. While the writer is in phase 2 the same id lives
        # in BOTH registers — `workers.write_step` claims the in-process slot with
        # `job_id=` the durable row's id — and without the de-duplication in
        # `routes.list_jobs` the operator sees one article twice, with two different
        # statuses, one of which is a snapshot of the slot rather than of the work.
        with jobs.exclusive("fetch", {"who": "the writer"}, job_id=dedup_job_id):
            twice = client.get("/ingest/jobs").json()
            same = [job for job in twice if job["id"] == dedup_job_id]
            assert len(same) == 1, f"one job listed {len(same)} times: {same}"
            # And the durable row wins: it is the state of the WORK, not of the slot.
            assert same[0]["kind"] == "fetch" and same[0]["status"] == "queued", same

        # `limit` must bound the answer without truncating a register at its own default:
        # `queue.listing()` defaults to 50 while the file keeps `KEEP_FINISHED` = 200, so
        # a caller asking for more than 50 used to get 50 durable rows and no sign of it.
        padding = [queue.enqueue("fetch", {"url": f"https://arxiv.org/abs/2500.{n:05d}"},
                                 dedup_key=f"pad{n}")["id"] for n in range(51)]
        try:
            wide = client.get("/ingest/jobs", params={"limit": MAX_PAGE}).json()
            durable_ids = {job["id"] for job in queue.listing(MAX_PAGE)}
            assert durable_ids <= {job["id"] for job in wide}, \
                f"the durable register was truncated: {len(durable_ids)} rows on disk, " \
                f"{len([j for j in wide if j['id'] in durable_ids])} in the answer"
        finally:
            with queue._LOCK:
                for job_id in padding:
                    queue._con().execute("DELETE FROM job WHERE id = ?", (job_id,))

        # (e) /healthz turns degraded when a job is staged (phase 1 done, article
        # waiting) and no writer thread is alive to drain it — the one failure of this
        # design that is otherwise invisible (`ops.health`). `workers.alive()` is empty
        # because every app in this check was built with workers=False: no writer
        # exists to be alive, which is exactly the state this check has to prove is
        # visible from the outside.
        claimed = queue.claim("queued", "phase1")
        assert claimed is not None, "nothing queued left to stage for the degraded check"
        queue.stage(claimed["id"], {"staging_lines": 2})
        sick = client.get("/healthz").json()
        assert sick["status"] == "degraded" and "writer" in sick["detail"], sick
        assert client.get("/stats").json()["queue"]["staged"] >= 1
        # The other half dies on its own and is just as invisible: a `queued` article
        # with no fetch worker alive is never parsed, and the writer being fine says
        # nothing about it.
        client.post("/fetch", json={"url": "https://arxiv.org/abs/2411.00003"})
        starved = client.get("/healthz").json()
        assert starved["status"] == "degraded", starved
        assert "fetch worker" in starved["detail"], starved
        print("ok: the durable queue — dedup by url, 429 at the ceiling with "
              "Retry-After, a job survives a simulated restart, /ingest/jobs merges "
              "both registers, a staged job with no writer thread turns /healthz "
              "degraded")

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

        # --- vault export (spec 11) ----------------------------------------
        stats = client.get("/stats").json()
        exported = client.post("/vault/export")
        assert exported.status_code == 200, exported.text
        body = exported.json()
        schemas.VaultExportResult.model_validate(body)
        assert (body["ideas"], body["theses"], body["sources"]) == \
            (stats["ideas"], stats["theses"], stats["sources"]), (body, stats)
        assert body["files"] == body["ideas"] + body["theses"] + body["sources"] + 1, body
        assert body["orphans"] == 0, body
        # `export(dest=DATA / "vault")` is a def-time default, rebound in `main`. If that
        # binding ever slips, this check rewrites the folder an operator has open in
        # Obsidian — the fingerprint would catch it afterwards, this catches it here.
        assert not Path(body["dest"]).is_relative_to(DATA), body
        with jobs.exclusive("vault-export"):
            busy = client.post("/vault/export")
        assert busy.status_code == 409, (busy.status_code, busy.text)
        assert "error" in busy.json(), busy.text
        # A `kind` the schema does not list is claimed and served without complaint and
        # kills the LISTING later, on response validation. Asserted after every kind this
        # check can produce has been through the slot, refusals included: a refused export
        # leaves a record too, so 409 is enough to poison the view.
        listed = client.get("/ingest/jobs")
        assert listed.status_code == 200, listed.text
        assert {job["kind"] for job in listed.json()} <= set(
            schemas.JobOut.model_fields["kind"].annotation.__args__), listed.text
        print("ok: vault — the export answers the same numbers as /stats, and a taken "
              "slot is 409, the status its own OpenAPI promises")

        # --- one definition of a leaf, everywhere --------------------------
        # A thesis whose source row is gone is invisible to /theses, /ideas and
        # /retrieve. It must be invisible to the counts as well, or /stats and
        # /healthz report `in_sync: false` forever over rows nobody can reach.
        with neo4j_store._session() as _s:
            _s.execute_write(lambda tx: tx.run(
                "MATCH (s:Source {id: $id}) DETACH DELETE s", id=made["source_id"]).consume())
        orphaned = client.get("/stats").json()
        assert orphaned["theses"] == 0 and orphaned["sources"] == 1, orphaned
        assert sorted(orphaned["ideas_without_leaves"]) == sorted(made["ideas"]), orphaned
        assert client.get("/theses").json()["total"] == 0
        assert client.get(f"/ideas/{idea_a}").json()["theses"] == []
        assert client.post("/admin/reindex").json() == {
            "indexed_before": 3, "leaves_in_store": 0, "indexed_after": 0, "in_sync": True}
        # The lake is broken now, and that is exactly when someone opens the graph to
        # look. The export marks the orphans instead of refusing (§11.3.7).
        broken = client.post("/vault/export").json()
        assert broken["theses"] == 0 and broken["orphans"] == len(made["ideas"]), broken
        print("ok: a leaf is a thesis with a source — the pages, the counts, the "
              "invariant check and the vault export agree on it")

    # ------------------------------------------------------------------- the key
    # The only thing between this API and anyone who can reach the port: every route
    # here writes to the graph or spends the school's GPUs, and there is no other
    # authentication in block A.
    key = "s3cret-" + "x" * 40
    with TestClient(create_app(mock=True, warmup=False, api_key=key, workers=False)) as guarded:
        # One route per kind, because "the middleware covers everything" is exactly the
        # claim that rots: a read, a write, the ops view, the ingest and a path that
        # does not exist. The last one matters — routing happens AFTER the middleware,
        # so an unknown path must not be able to say "no such route" to a stranger.
        for method, path, body in (("get", "/healthz", None), ("get", "/stats", None),
                                   ("get", "/sources", None), ("get", "/search?q=x", None),
                                   ("post", "/retrieve", {"query": "x"}),
                                   ("post", "/fetch", {"url": "https://arxiv.org/abs/2406.04824"}),
                                   ("post", "/ingest/phase2", {}),
                                   ("post", "/admin/reindex", None),
                                   ("post", "/vault/export", None),
                                   ("patch", "/ideas/whatever", {"text": "x"}),
                                   ("get", "/no/such/path", None)):
            call = getattr(guarded, method)
            answer = call(path, json=body) if body is not None else call(path)
            assert answer.status_code == 401, (path, answer.status_code, answer.text)
            assert set(answer.json()) == {"error"}, answer.text
            assert answer.headers.get("www-authenticate") == "Bearer", answer.headers
            # A wrong key and a malformed header are the same refusal as no header.
            for header in ({"Authorization": f"Bearer {key}x"}, {"Authorization": key},
                           {"Authorization": "Basic " + key}, {"Authorization": "Bearer "},
                           {"X-Lake-Key": key}):
                wrong = call(path, json=body, headers=header) if body is not None \
                    else call(path, headers=header)
                assert wrong.status_code == 401, (path, header, wrong.status_code)
        # ...and the same routes answer normally once the header is right, so that the
        # block above cannot be passing because the server is simply broken.
        good = {"Authorization": f"Bearer {key}"}
        assert guarded.get("/healthz", headers=good).json()["mock"] is True
        assert guarded.post("/retrieve", json={"query": "diversity", "k": 2},
                            headers=good).status_code == 200
        # Validation still runs after the key, and still answers 400, not 401.
        assert guarded.post("/retrieve", json={"k": 1}, headers=good).status_code == 400
        assert guarded.get("/no/such/path", headers=good).status_code == 404

        # The schema is the integration contract and holds no lake data: C reads it
        # before it has a key. Everything else stays shut.
        schema = guarded.get("/openapi.json")
        assert schema.status_code == 200, schema.status_code
        assert guarded.get("/docs").status_code == 200
        doc = schema.json()
        assert doc["security"] == [{"bearerAuth": []}], doc.get("security")
        assert doc["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
        # A 401 the document does not mention is a branch C never writes, and it is the
        # one it will hit first. Asserted over EVERY operation, not a sample.
        for path, item in doc["paths"].items():
            for method, operation in item.items():
                assert "401" in operation.get("responses", {}), (path, method)
        assert "ErrorResponse" in doc["components"]["schemas"], "the 401 body is a $ref"

    # `--no-auth` is a choice somebody types; an EMPTY key is a server that thinks it
    # is guarded and is not, so it must not start at all.
    try:
        with TestClient(create_app(mock=True, warmup=False, api_key="", workers=False)):
            raise AssertionError("a server with an empty LAKE_API_KEY started")
    except RuntimeError as exc:
        assert "LAKE_API_KEY is empty" in str(exc), exc
    # The env is where the real one comes from, and `create_app` must read it there.
    os.environ["LAKE_API_KEY"] = key
    try:
        with TestClient(create_app(mock=True, warmup=False, workers=False)) as from_env:
            assert from_env.get("/healthz").status_code == 401
            assert from_env.get("/healthz", headers={"Authorization": f"Bearer {key}"}
                                ).status_code == 200
    finally:
        os.environ.pop("LAKE_API_KEY", None)
    # And with no variable set at all the server refuses to come up — the case that
    # turns a forgotten line in `.env.local` into an open API on a public port.
    try:
        with TestClient(create_app(mock=True, warmup=False, workers=False)):
            raise AssertionError("a server started with no LAKE_API_KEY in the environment")
    except RuntimeError as exc:
        assert "LAKE_API_KEY is empty" in str(exc), exc
    print("ok: the key — 401 on every route and on unknown paths, wrong key and wrong "
          "scheme refused, 400 still 400, schema open and documents its 401, empty or "
          "missing key refuses to start")

    # ------------------------------------------------------------------ the console
    # `/ui` is the fifth open path, and it is open for the same reason `/docs` is: a
    # browser cannot put a header on a top-level navigation. So the asserts here are
    # about what that costs — the asset must hold no lake data, must not appear in the
    # contract, and must not have widened the door for anything else.
    with TestClient(create_app(mock=True, warmup=False, api_key=key, workers=False)) as guarded:
        page = guarded.get(app_mod.UI_PATH)
        assert page.status_code == 200, page.status_code
        assert page.headers["content-type"].startswith("text/html"), page.headers
        body = page.text
        assert "<title>" in body and "Bearer" in body, "the console must ask for the key itself"
        # No lake data baked into the asset: it reads everything over the same routes,
        # with the key the operator types. A page shipping ids, a key or a Bolt URL
        # would be a page that keeps answering after the key is revoked. Matched as
        # real ids (`idea_` + hex), not as the bare prefix — the page says `idea_…` in
        # its own input placeholders, and a check that trips on those is a check
        # somebody deletes.
        for leaked in (r"idea_[0-9a-f]{8}", r"th_[0-9a-f]{8}", r"LAKE_API_KEY\s*=\s*\S",
                       r"neo4j(\+s)?://", r"bolt://"):
            assert not re.search(leaked, body), leaked
        # Not in the contract: C integrates against /openapi.json, and an HTML page in
        # there would be an operation with no schema — plus `_drop_422` would stamp it
        # with a 401 it deliberately does not answer.
        assert app_mod.UI_PATH not in guarded.get("/openapi.json").json()["paths"]
        # The door is exactly one path wider, and the neighbours of that path stay shut:
        # a prefix match instead of an exact one would have opened all of them.
        for near in (app_mod.UI_PATH + "/", app_mod.UI_PATH + "/x", "/ui.html", "/uix"):
            assert guarded.get(near).status_code == 401, near
        assert guarded.post(app_mod.UI_PATH).status_code == 401, "only GET is open"
    assert app_mod.UI_FILE.is_file(), app_mod.UI_FILE

    # The page is one file of hand-written JS, and a missing bracket in it does not
    # fail loudly: the browser parses nothing, runs nothing, and shows a header over an
    # empty page — served with a 200 by a server that is perfectly healthy. That is the
    # exact shape of a silent failure this project refuses elsewhere, so it gets a
    # check. (Caught precisely this, four times, the first time it ran.)
    script = re.search(r"<script>(.*)</script>", app_mod.UI_FILE.read_text(encoding="utf-8"),
                       re.S)
    assert script, "console.html has no <script> block"
    node = shutil.which("node")
    if node:
        scratch = tmp / "console.js"
        scratch.write_text(script.group(1), encoding="utf-8")
        checked = subprocess.run([node, "--check", str(scratch)], capture_output=True, text=True)
        assert checked.returncode == 0, f"console.html: {checked.stderr.strip()[:400]}"
        print("ok: the console — its JavaScript parses (node --check)")
    else:
        # Named, not swallowed: a check that quietly did not run is worse than none.
        print("SKIPPED: node is not installed, so console.html's JavaScript was NOT "
              "parsed — a syntax error in it would ship as a blank page")
    print("ok: the console — /ui open and HTML, no lake data in it, absent from the "
          "contract, neighbouring paths and non-GET still 401")


def _await(client, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/ingest/jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} never finished")


if __name__ == "__main__":
    raise SystemExit(main())
