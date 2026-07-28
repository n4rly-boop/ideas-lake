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
    """One node -> one Neo4j property map, by the model's field list."""
    out = {}
    for name in model.model_fields:
        value = node.get(name)
        if value is None:            # absent, not null — see the module docstring
            continue
        out[name] = json.dumps(value, ensure_ascii=False) if name in JSON_FIELDS else value
    if "id" not in out:
        raise ValueError(f"{model.__name__} without an id: {sorted(node)}")
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

    missing = [name for name in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD")
               if not os.environ.get(name)]
    if missing:
        raise SystemExit(f"not in the environment: {', '.join(missing)}")

    driver = GraphDatabase.driver(os.environ["NEO4J_URI"],
                                  auth=(os.environ["NEO4J_USERNAME"],
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
    idea = _row(Idea, {"id": "i1", "text": "t", "failure_modes": ["a", "b"],
                       "vector": [0.1] * 3, "differentiation": None})
    assert idea["failure_modes"] == ["a", "b"], "a list of scalars stays a list"
    assert "differentiation" not in idea and idea["vector"] == [0.1] * 3, idea
    assert set(idea) <= set(Idea.model_fields), "a property the model does not have"
    try:
        _row(Thesis, {"text": "no id"})
    except ValueError:
        pass
    else:
        raise AssertionError("a node without an id would MERGE onto every id-less node")
    print("ok: nested -> JSON, None dropped, False kept, id required")
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
