"""One-way load of the lake into block B's Neo4j (contract `07:72`).

    python3 -m lake.neo4j_load --dry-run     # build and validate, no connection
    python3 -m lake.neo4j_load               # write
    python3 -m lake.neo4j_load --wipe        # delete every node first, then write

Not a `graph_client` backend. The integration form — HTTP service or client
library — is B's open decision (`07:72`), and this does not pretend to make it:
it reads through `graph_client` like every other consumer and speaks Cypher only
on the way out. When B lands the real adapter, this file goes away.

The vocabulary is ours: `Source`, `Thesis`, `Idea` carry the fields of
`lake.models`, name for name. B's four test nodes use a different one
(`concept`, `target_score`, `is_success`, `kind`); those are not written and not
removed. Edges follow the ERD (`06:83-85`): `(:Idea)-[:HAS_LEAF]->(:Thesis)` —
the name already in the database — and `(:Source)-[:YIELDS]->(:Thesis)`.

Three things this is careful about:

1. **Constraints first.** B's instance has none, and `MERGE` without a unique
   index is a full label scan per row and a race between two loaders. Creating
   them is idempotent and makes the load O(1) per node.
2. **No nested maps.** Neo4j properties are scalars or arrays of scalars, so
   `run_meta` goes in as JSON text; a dict would be rejected mid-batch, after
   earlier batches already landed. `failure_modes` is a list of strings and
   stays a list.
3. **Absent is not empty.** A field that is `None` in the lake is left out of
   the row rather than written as `null`: `run_success: null` on every paper
   would be a filter that lies, the same rule the vault export follows (§11.2).
"""
import argparse
import json
import os

from . import graph_client
from .models import Idea, Source, Thesis

PAGE = 500
BATCH = 200

# Which model fields carry a nested value. Everything else is a scalar or a list
# of scalars and goes in as it is.
JSON_FIELDS = {"run_meta"}

CONSTRAINTS = (
    "CREATE CONSTRAINT source_id IF NOT EXISTS FOR (n:Source) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT thesis_id IF NOT EXISTS FOR (n:Thesis) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT idea_id   IF NOT EXISTS FOR (n:Idea)   REQUIRE n.id IS UNIQUE",
)

# `SET n += $row` and not `SET n = $row`: a reload must not silently drop a
# property B added on her side. Removing one is her call, not this script's.
UPSERT = "UNWIND $rows AS row MERGE (n:{label} {{id: row.id}}) SET n += row"

EDGES = (
    ("UNWIND $rows AS row MATCH (i:Idea {id: row.idea_id}), (t:Thesis {id: row.id}) "
     "MERGE (i)-[:HAS_LEAF]->(t)", "HAS_LEAF"),
    ("UNWIND $rows AS row MATCH (s:Source {id: row.source_id}), (t:Thesis {id: row.id}) "
     "MERGE (s)-[:YIELDS]->(t)", "YIELDS"),
)


def _row(model, node: dict) -> dict:
    """One node -> one Neo4j property map, by the model's field list.

    A field the model marks required must arrive. Dropping it is not the
    "absent is not empty" rule of the module docstring: that rule is about a
    value the lake genuinely does not have (`run_success` on a paper), and the
    model spells those `| None = None`. A required field missing means the
    reader did not carry it — `list_theses` is a serving projection and has no
    `vector` (`stub_store:277-283`) — and the node lands with a hole, silently,
    because Neo4j has no schema to object. That is how 60 theses reached the
    database without vectors on 2026-07-29.
    """
    out = {}
    for name, field in model.model_fields.items():
        value = node.get(name)
        if value is None:
            if field.is_required():
                raise ValueError(f"{model.__name__} {node.get('id', '?')}: required field "
                                 f"{name!r} did not arrive from the reader — a node with a "
                                 f"hole, not a node with an absent value")
            continue        # optional and absent — see the module docstring
        out[name] = json.dumps(value, ensure_ascii=False) if name in JSON_FIELDS else value
    return out


def _all(fetch) -> list:
    out: list = []
    while True:
        page = fetch(PAGE, len(out))
        if not page:
            return out
        out += page


def build() -> dict:
    """Read the whole lake and shape it for Cypher. No connection, no writes."""
    sources = _all(graph_client.list_sources)
    theses = _all(lambda limit, offset: graph_client.list_theses(None, None, limit, offset))
    # `list_theses` is what `/theses` serves and it carries no vector — 384 floats
    # per leaf on every page would be a listing nobody wants. `all_theses` is the
    # reader that holds them (`stub_store:319-332`), the same one the index
    # reconciles against. A leaf missing from it stays `None` and `_row` refuses.
    vectors = {leaf["id"]: leaf["vector"] for leaf in graph_client.all_theses()}
    for leaf in theses:
        leaf["vector"] = vectors.get(leaf["id"])
    ideas = []
    idea_ids = _all(graph_client.list_idea_ids)
    for start in range(0, len(idea_ids), PAGE):
        ideas += graph_client.get_ideas(idea_ids[start:start + PAGE])

    counts = graph_client.counts()
    if (len(sources), len(ideas), len(theses)) != \
            (counts["sources"], counts["ideas"], counts["theses"]):
        raise ValueError(f"read {len(sources)}/{len(ideas)}/{len(theses)}, store counts "
                         f"{counts} — a partial load looks like a smaller lake, not a failure")
    # Every leaf must find both ends, or its MERGE matches nothing and the edge is
    # skipped without a word — the shape of a graph that is quietly missing
    # provenance (`06:83-85`).
    idea_ids_set, source_ids = {i["id"] for i in ideas}, {s["id"] for s in sources}
    for leaf in theses:
        if leaf["idea_id"] not in idea_ids_set or leaf["source_id"] not in source_ids:
            raise ValueError(f"thesis {leaf['id']} points at a missing idea or source")

    return {"Source": [_row(Source, s) for s in sources],
            "Thesis": [_row(Thesis, t) for t in theses],
            "Idea": [_row(Idea, i) for i in ideas],
            "edges": [{"id": t["id"], "idea_id": t["idea_id"], "source_id": t["source_id"]}
                      for t in theses]}


def _chunks(rows: list, size: int = BATCH):
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


def push(payload: dict, wipe: bool = False) -> dict:
    from neo4j import GraphDatabase

    from .neo4j_store import _require_local_target

    missing = [name for name in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD")
               if not os.environ.get(name)]
    if missing:
        raise SystemExit(f"not in the environment: {', '.join(missing)}")

    # Captured once and reused for both the guard and the driver below, so the two
    # can never diverge the way a re-read of `os.environ` could (`13` MAJOR 4).
    uri = os.environ["NEO4J_URI"]
    if wipe:
        # BLOCKER (second round): this was the one reachable place `--wipe` ran
        # with NO target guard at all — `neo4j_store.migrate(wipe=True)` has zero
        # callers, this script is what an operator actually runs, and
        # `.env.local.example` points `NEO4J_*` straight at Aura, block B's
        # database. Reusing `neo4j_store`'s guard (not a second copy of it) means
        # `--wipe` here can now only ever hit the lake's own local instance —
        # never Aura, never "whatever the environment happens to name" (07:79).
        _require_local_target(uri)

    driver = GraphDatabase.driver(uri, auth=(os.environ["NEO4J_USERNAME"],
                                             os.environ["NEO4J_PASSWORD"]))
    written = {}
    try:
        with driver.session(database=os.environ.get("NEO4J_DATABASE", "neo4j")) as session:
            if wipe:
                gone = session.run("MATCH (n) DETACH DELETE n RETURN count(n) AS c")
                written["wiped"] = gone.single()["c"]
            for statement in CONSTRAINTS:
                session.run(statement)
            for label in ("Source", "Thesis", "Idea"):
                for chunk in _chunks(payload[label]):
                    session.run(UPSERT.format(label=label), rows=chunk)
                written[label] = len(payload[label])
            for statement, name in EDGES:
                for chunk in _chunks(payload["edges"]):
                    session.run(statement, rows=chunk)
                written[name] = len(payload["edges"])
            # Counted in the database, not in memory: a MERGE that matched nothing
            # raises nothing, and the difference is the whole point of loading.
            written["nodes_in_db"] = session.run(
                "MATCH (n) RETURN count(n) AS c").single()["c"]
            written["edges_in_db"] = session.run(
                "MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
    finally:
        driver.close()
    return written


def demo() -> None:
    """ponytail: offline check of the shaping, the part that has no database in it."""
    src = {"id": "s1", "url": "http://x/1", "title": "T", "type": "run", "version": "v1",
           "retrieved_at": "2026-07-28T10:00:00Z", "run_success": False,
           "run_meta": {"fitness_delta": -0.2}}
    row = _row(Source, src)
    assert row["run_meta"] == '{"fitness_delta": -0.2}', row
    assert row["run_success"] is False, "False is a value; only None is absent"
    paper = _row(Source, {**src, "run_success": None, "run_meta": None})
    assert "run_success" not in paper and "run_meta" not in paper, paper
    full_idea = {"id": "i1", "text": "t", "applicability_conditions": "c",
                 "limitations": "l", "failure_modes": ["a", "b"], "effect_claimed": "e",
                 "effect_observed": "o", "vector": [0.1] * 3, "differentiation": None}
    idea = _row(Idea, full_idea)
    assert idea["failure_modes"] == ["a", "b"], "a list of scalars stays a list"
    assert "differentiation" not in idea and idea["vector"] == [0.1] * 3, idea
    assert set(idea) <= set(Idea.model_fields), "a property the model does not have"

    def refuses(model, node, missing):
        try:
            _row(model, node)
        except ValueError as exc:
            assert missing in str(exc), f"refused, but not for {missing}: {exc}"
        else:
            raise AssertionError(f"{model.__name__} without {missing} was accepted")

    refuses(Thesis, {"text": "no id"}, "id")   # would MERGE onto every id-less node
    # The 2026-07-29 regression: `list_theses` carries no vector, `_row` dropped it,
    # and 60 leaves landed in Neo4j without one. Optional-and-absent still passes.
    full_leaf = {"id": "t1", "source_id": "s1", "idea_id": "i1", "text": "x",
                 "context": "c", "effect": "e", "locator": "p.1", "text_hash": "h",
                 "vector": [0.2] * 3, "created_at": "2026-07-28T10:00:00Z"}
    assert _row(Thesis, full_leaf)["vector"] == [0.2] * 3
    refuses(Thesis, {**full_leaf, "vector": None}, "vector")
    refuses(Idea, {**full_idea, "vector": None}, "vector")
    print("ok: nested -> JSON, None dropped, False kept, every required field demanded")

    # BLOCKER (second round): push(wipe=True) used to have no target guard at
    # all — the reachable destructive path, since neo4j_store.migrate(wipe=True)
    # has zero callers. Verified without a live connection: the guard runs
    # before the driver is even built, so a fake Aura-shaped URI must refuse
    # before any network call is attempted (no seeded database at risk here).
    saved = {k: os.environ.get(k) for k in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD")}
    os.environ["NEO4J_URI"] = "neo4j+s://deadbeef00.databases.neo4j.io"
    os.environ["NEO4J_USERNAME"] = "x"
    os.environ["NEO4J_PASSWORD"] = "x"
    try:
        push({"Source": [], "Thesis": [], "Idea": [], "edges": []}, wipe=True)
    except RuntimeError as exc:
        assert "deadbeef00" in str(exc), exc
    else:
        raise AssertionError("push(wipe=True) against a non-local URI must refuse, not wipe")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("ok: push(wipe=True) refuses a non-local NEO4J_URI before connecting (BLOCKER)")
    print("neo4j_load self-check OK — nothing connected, nothing written")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python3 -m lake.neo4j_load",
                                     description="Залить озеро в Neo4j блока B (`07:72`).")
    parser.add_argument("--dry-run", action="store_true", help="собрать и проверить, не подключаясь")
    parser.add_argument("--wipe", action="store_true",
                        help="снести все узлы перед записью (тестовая БД)")
    parser.add_argument("--self-check", action="store_true", help="офлайн-проверка формовки")
    args = parser.parse_args()

    if args.self_check:
        demo()
    else:
        payload = build()
        print(f"собрано: {len(payload['Source'])} источников, {len(payload['Thesis'])} тезисов, "
              f"{len(payload['Idea'])} идей, {len(payload['edges'])} листьев (×2 ребра)")
        if args.dry_run:
            print("--dry-run: не подключался, ничего не записано")
        else:
            for key, value in push(payload, wipe=args.wipe).items():
                print(f"  {key}: {value}")
