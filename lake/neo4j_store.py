"""Neo4j (Bolt) backend behind `graph_client` (spec `13-run-ingest-and-graph-spec.md` §4).

Same public surface as `stub_store`, same signatures, same returned key sets — this
file is a drop-in swap, not a new API. `graph_client` picks between the two by
`LAKE_STORE` (see there); nothing here knows about that variable.

Storage shape, and why it differs from the SQLite columns:

- Labels/edges are `neo4j_load.py`'s, reused rather than reinvented: `Source`,
  `Thesis`, `Idea`, `(:Source)-[:YIELDS]->(:Thesis)`, `(:Idea)-[:HAS_LEAF]->(:Thesis)`.
- `source_id` and `idea_id` are NOT stored as Thesis properties. The edges above are
  the only place that association lives — a property duplicating an edge is a second
  definition of the same fact (exactly the `dirty`-had-two-definitions mistake `13`
  §3.2 undid for the idea side), and `split_idea` would need to keep both in sync on
  every move. Read paths derive `source_id`/`idea_id` from the traversal instead.
- `vector` on `Thesis` and `Idea` is a native Neo4j float-array property — no blob
  encoding needed, unlike SQLite's `array('f', ...).tobytes()`.
- `run_meta` (Source) is the one nested value in the whole schema; Neo4j properties
  are scalars or arrays of scalars, so it goes in as JSON text, same choice
  `neo4j_load.py` already made.
- `failure_modes` is a list of strings and stays a Neo4j string-array property.
- Idempotency: SQLite holds `UNIQUE(source_id, text_hash)`. Community Neo4j has no
  composite uniqueness constraint, so the write computes `leaf_key =
  f"{source_id}|{text_hash}"` and CREATEs the Thesis node with it under a
  single-property uniqueness constraint. A repeat raises `ConstraintError` (a
  `Neo4jError`, already in `graph_client.STORE_ERRORS`) — never a silent skip,
  exactly like `sqlite3.IntegrityError` on the SQLite side.
- A leaf write MATCHes both its Source and its Idea before CREATEing the Thesis node
  and MERGEs both edges in the same statement; if either MATCH finds nothing the
  statement returns no row and the write raises `ValueError` instead of silently
  creating a Thesis with a dangling reference — SQLite's `thesis.idea_id` has no
  foreign key at all (map §"DDL", stub_store.py:17) and would accept it. This is
  the one place behaviour intentionally tightens rather than mirrors: a bare "not
  found" is a bug worth surfacing, not an invariant worth reproducing.

Listing order is an explicit `seq` property (`_next_seq`), not `id(n)`: Neo4j's
internal id is reused after a delete, so `ORDER BY id(n)` shuffles the moment any
node with a lower id is removed and a new one created — reproduced deterministically
(create a 4-leaf idea, delete it, create a 5-leaf idea, `get_leaves` comes back in a
different order than `stub_store`'s). `stub_store` gets stable insertion order for
free from SQLite's `rowid` (the thesis/idea tables never delete rows); `seq` is this
module's equivalent, one shared counter node because ordering is always queried
within a single label at a time.

`neo4j` is imported lazily (inside `_get_driver`), so this module imports cleanly
with the package present-but-unreachable and stays inert when `LAKE_STORE=stub`.
"""
import json
import os
import threading
from typing import get_args

from .models import Idea, Source, Thesis

# --------------------------------------------------------------------- connection

_lock = threading.Lock()
_driver = None
_database: str | None = None
_schema_ready = False
# The URI the driver was actually built with — captured once, at connection time.
# The wipe guard (`_require_local_target`) validates THIS, never `os.environ` read
# again later: `NEO4J_URI` can change after the driver connects (a shell export in
# another terminal, a test fixture), and a guard that re-reads the environment at
# guard time checks a string that may no longer name where the open connection
# actually points (`13` MAJOR 4 / BLOCKER 2, second round).
_uri: str | None = None

# Reused from `neo4j_load.py` (constraints already chosen there, `13` §4.2) plus the
# two this file adds: leaf idempotency, and the `_Seq` counter (below) — without a
# constraint on it, a second `:_Seq {id: 'global'}` node can be seeded by hand and
# `_next_seq`'s MERGE then matches both, silently: the Neo4j driver's `.single()`
# on a two-row result issues a UserWarning and returns one of them rather than
# raising, so the whole ordering guarantee (`seq`, module docstring) would corrupt
# without a single error anywhere (review, `13` §10). A uniqueness constraint is
# the DB refusing the duplicate outright, which is cheaper and stronger than
# detecting the damage after the fact.
CONSTRAINTS = (
    "CREATE CONSTRAINT source_id      IF NOT EXISTS FOR (n:Source) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT thesis_id      IF NOT EXISTS FOR (n:Thesis) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT thesis_leaf_key IF NOT EXISTS FOR (n:Thesis) REQUIRE n.leaf_key IS UNIQUE",
    "CREATE CONSTRAINT idea_id        IF NOT EXISTS FOR (n:Idea)   REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT seq_id         IF NOT EXISTS FOR (n:_Seq)   REQUIRE n.id IS UNIQUE",
)


def _get_driver():
    """Open (once) the driver and make sure the constraints exist. Call under `_lock`
    is not required here — `GraphDatabase.driver` and constraint creation are cheap
    and idempotent, and double-creation costs a redundant round trip, not a bug."""
    global _driver, _database, _schema_ready, _uri
    if _driver is None:
        from neo4j import GraphDatabase  # lazy: see module docstring

        uri = os.environ["NEO4J_URI"]  # graph_client already refused to start without it
        user = os.environ.get("NEO4J_USERNAME")
        password = os.environ.get("NEO4J_PASSWORD")
        auth = (user, password) if user else None
        with _lock:
            if _driver is None:
                _driver = GraphDatabase.driver(uri, auth=auth)
                _database = os.environ.get("NEO4J_DATABASE", "neo4j")
                _uri = uri  # snapshot at connection time, see the module-level comment
    if not _schema_ready:
        with _lock:
            if not _schema_ready:
                with _driver.session(database=_database) as session:
                    for statement in CONSTRAINTS:
                        session.run(statement)
                _schema_ready = True
    return _driver


def _session():
    return _get_driver().session(database=_database)


def close() -> None:
    """For tests: drop the driver so the next call reconnects (e.g. after `--wipe`)."""
    global _driver, _schema_ready, _uri
    with _lock:
        if _driver is not None:
            _driver.close()
        _driver = None
        _schema_ready = False
        _uri = None


# --------------------------------------------------------------------- encode/decode

def _next_seq(tx) -> int:
    """Atomic monotonic counter, used as the `seq` property that stands in for
    SQLite's `rowid` insertion order (module docstring). One node, one property,
    incremented inside the caller's own write transaction so a rolled-back write
    never burns a number permanently (the `MERGE` locks the counter node for the
    life of the transaction, same as any other write in it)."""
    row = tx.run(
        "MERGE (c:_Seq {id: 'global'}) "
        "ON CREATE SET c.next = 1 "
        "ON MATCH SET c.next = c.next + 1 "
        "RETURN c.next AS seq").single()
    return row["seq"]


def _floats(name: str, values) -> list[float]:
    """`array('f', values)` on the SQLite side raises `TypeError` on a non-numeric
    entry; Neo4j would happily store a list of strings as a STRING array and never
    complain. This is the equivalent guard, so a corrupted vector still fails the
    write (and rolls back the whole transaction) instead of landing as the wrong
    Neo4j type."""
    try:
        return [float(v) for v in values]
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{name}: not a list of floats ({exc})") from exc


def _source_row(src: Source) -> dict:
    row = {"id": src.id, "url": src.url, "title": src.title, "type": src.type,
           "version": src.version, "retrieved_at": src.retrieved_at}
    if src.run_success is not None:
        row["run_success"] = src.run_success
    if src.run_meta is not None:
        row["run_meta"] = json.dumps(src.run_meta, ensure_ascii=False)
    return row


def _idea_row(idea: Idea) -> dict:
    row = {"id": idea.id, "text": idea.text,
           "applicability_conditions": idea.applicability_conditions,
           "limitations": idea.limitations, "failure_modes": list(idea.failure_modes),
           "effect_claimed": idea.effect_claimed, "effect_observed": idea.effect_observed,
           "vector": _floats("vector", idea.vector), "origin": idea.origin,
           "trust_score": float(idea.trust_score), "dirty": bool(idea.dirty),
           "rederived_at_leaf_count": int(idea.rederived_at_leaf_count),
           "created_at": idea.created_at, "updated_at": idea.updated_at}
    if idea.differentiation is not None:
        row["differentiation"] = idea.differentiation
    return row


def _thesis_row(th: Thesis, source_id: str) -> dict:
    return {"id": th.id, "text": th.text, "context": th.context, "effect": th.effect,
            "locator": th.locator, "text_hash": th.text_hash,
            "vector": _floats("vector", th.vector), "created_at": th.created_at,
            "leaf_key": f"{source_id}|{th.text_hash}"}


def _require(model, d: dict, exclude: frozenset[str] = frozenset()) -> dict:
    """Every required field of `model` must be a key in `d` (§4.2 p."`_row`" /
    `13` §4.2 point 6): Neo4j has no schema to reject a node with a hole, so this is
    the check that plays that role on the way out. `exclude` is for fields this store
    derives from an edge rather than a property (Thesis.source_id, Thesis.idea_id)."""
    for name, field in model.model_fields.items():
        if name in exclude:
            continue
        if name not in d:
            if field.is_required():
                raise ValueError(f"{model.__name__} {d.get('id', '?')}: missing required "
                                 f"field {name!r} — a Neo4j node with a hole (13 §4.2)")
            d[name] = None
    return d


def _idea_out(node) -> dict:
    d = _require(Idea, dict(node))
    d.pop("seq", None)  # storage-only ordering key, not an Idea field (extra="forbid")
    d["failure_modes"] = list(d["failure_modes"])
    d["vector"] = list(d["vector"])
    d["dirty"] = bool(d["dirty"])
    return d


def _source_out(d: dict) -> dict:
    d = _require(Source, dict(d))
    d.pop("seq", None)  # storage-only ordering key, not a Source field (extra="forbid")
    d["run_meta"] = None if d["run_meta"] is None else json.loads(d["run_meta"])
    return d


def _leaf_out(t: dict, s: dict, idea_id: str | None) -> dict:
    """Thesis + its Source, shaped like `stub_store._leaf_out` (map doc §"Row
    decoders"): `vector` popped (index.py's business), `source_*`/`run_*` from the
    Source node, `idea_id` from the traversal, not a stored property."""
    if idea_id is None:
        raise ValueError(f"Thesis {t.get('id', '?')}: no owning Idea via HAS_LEAF — "
                         "a leaf with a hole (13 §4.2)")
    d = _require(Thesis, dict(t), exclude=frozenset({"source_id", "idea_id"}))
    d.pop("vector", None)
    d.pop("leaf_key", None)
    d.pop("seq", None)  # storage-only ordering key, not a Thesis field (extra="forbid")
    d["idea_id"] = idea_id
    src = _source_out(s)
    d["source_id"] = src["id"]
    d["source_type"] = src["type"]
    d["source_url"] = src["url"]
    d["source_title"] = src["title"]
    d["run_success"] = src["run_success"]
    d["run_meta"] = src["run_meta"]
    return d


# ------------------------------------------------------------------------- writes

def write_source(src: Source) -> str:
    row = _source_row(src)
    with _session() as session:
        def txn(tx):
            # `seq` assigned once, at first creation, and kept across re-fetches of
            # the same (url, version) — a MERGE that reassigned it on every upsert
            # would reorder `list_sources` on the mere act of re-pulling a paper.
            existing = tx.run("MATCH (n:Source {id: $id}) RETURN n.seq AS seq",
                              id=src.id).single()
            row["seq"] = existing["seq"] if existing is not None else _next_seq(tx)
            tx.run("MERGE (n:Source {id: $id}) SET n = $row", id=src.id, row=row).consume()
        session.execute_write(txn)
    return src.id


def _create_thesis(tx, th: Thesis, source_id: str) -> str:
    if th.source_id and th.source_id != source_id:
        raise ValueError(f"thesis {th.id} carries source_id {th.source_id}, batch is {source_id}")
    row = _thesis_row(th, source_id)
    row["seq"] = _next_seq(tx)
    result = list(tx.run(
        "MATCH (s:Source {id: $source_id}) "
        "MATCH (i:Idea {id: $idea_id}) "
        "CREATE (t:Thesis $row) "
        "MERGE (s)-[:YIELDS]->(t) "
        "MERGE (i)-[:HAS_LEAF]->(t) "
        "RETURN t.id AS id",
        source_id=source_id, idea_id=th.idea_id, row=row))
    if not result:
        raise ValueError(f"thesis {th.id}: source {source_id!r} or idea {th.idea_id!r} "
                         "does not exist")
    return result[0]["id"]


def _insert_theses(tx, source_id: str, theses: list[Thesis]) -> list[str]:
    return [_create_thesis(tx, th, source_id) for th in theses]


def write_theses(source_id: str, theses: list[Thesis]) -> list[str]:
    """Append-only counterpart of `create_idea_with_theses` (same split as the
    stub backend): leaves onto ideas that already exist. `dirty` is raised here too,
    inside the same `execute_write`, on every idea touched — until 2026-07-31 this
    path did not, leaving an idea clean with a stale score after a leaf landed on it
    through here (`13` finding, review of the same date)."""
    def txn(tx):
        ids = _insert_theses(tx, source_id, theses)
        for idea_id in sorted({th.idea_id for th in theses}):
            _mark_dirty(tx, idea_id, True)
        return ids

    with _session() as session:
        return session.execute_write(txn)


def create_idea(idea: Idea) -> str:
    row = _idea_row(idea)
    with _session() as session:
        def txn(tx):
            row["seq"] = _next_seq(tx)
            tx.run("CREATE (n:Idea $row)", row=row).consume()
        session.execute_write(txn)
    return idea.id


def create_idea_with_theses(idea: Idea | None, source_id: str, theses: list[Thesis]) -> list[str]:
    """Idea and its leaves in one `execute_write` transaction, same guarantee as
    `stub_store.create_idea_with_theses` (`13` §4.2): any exception here — including
    `_create_thesis`'s ValueError, `_floats`'s TypeError, or a `ConstraintError` from
    a duplicate `leaf_key` — aborts the whole driver transaction and nothing commits.
    `dirty` is raised on every touched idea inside the same transaction (`13` §3.2).
    """
    def txn(tx):
        if idea is not None:
            row = _idea_row(idea)
            row["dirty"] = True
            row["seq"] = _next_seq(tx)
            tx.run("CREATE (n:Idea $row)", row=row).consume()
        ids = _insert_theses(tx, source_id, theses)
        touched = sorted({th.idea_id for th in theses if idea is None or th.idea_id != idea.id})
        for idea_id in touched:
            _mark_dirty(tx, idea_id, True)
        return ids

    with _session() as session:
        return session.execute_write(txn)


def _mark_dirty(tx, idea_id: str, value: bool) -> None:
    result = list(tx.run("MATCH (n:Idea {id: $id}) SET n.dirty = $value RETURN n.id AS id",
                         id=idea_id, value=value))
    if not result:
        raise KeyError(f"idea {idea_id} not found")


_IDEA_FIELDS = set(Idea.model_fields) - {"id"}
_NULLABLE_IDEA_FIELDS = {name for name, field in Idea.model_fields.items()
                         if type(None) in get_args(field.annotation)}


def _update_idea(tx, idea_id: str, fields: dict) -> None:
    if not fields:
        raise ValueError("update_idea called with no fields")
    unknown = set(fields) - _IDEA_FIELDS
    if unknown:
        raise ValueError(f"unknown Idea fields: {sorted(unknown)}")
    nulls = sorted(k for k, v in fields.items() if v is None and k not in _NULLABLE_IDEA_FIELDS)
    if nulls:
        raise ValueError(f"NULL is not a value for Idea fields: {nulls}")
    if "vector" in fields:
        fields = {**fields, "vector": _floats("vector", fields["vector"])}
    # `+=` (not `=`): a value of None deletes that property (Neo4j SET-map semantics),
    # which is exactly how `differentiation: None` should read back — absent, not null.
    result = list(tx.run("MATCH (n:Idea {id: $id}) SET n += $fields RETURN n.id AS id",
                         id=idea_id, fields=fields))
    if not result:
        raise KeyError(f"idea {idea_id} not found")


def update_idea(idea_id: str, fields: dict) -> None:
    with _session() as session:
        session.execute_write(lambda tx: _update_idea(tx, idea_id, fields))


def split_idea(parent_id: str, parent_fields: dict,
               children: list[tuple[Idea, list[str]]]) -> None:
    """Same three refusals as `stub_store.split_idea`, same one transaction. The
    "leaf" a child takes is re-homed by moving the `HAS_LEAF` edge, not by writing a
    Thesis property — there is no `idea_id` property to write (module docstring), so
    this never touches a Thesis node at all, which is a stronger form of the
    "no `update_thesis`" invariant (`13` §4.2, selfcheck §6.9) than the SQLite side
    manages: not "one column may change", but "zero properties may change"."""
    if not children:
        raise ValueError("split_idea called with no children")
    moving: list[str] = []
    for idea, thesis_ids in children:
        if not thesis_ids:
            raise ValueError(f"split_idea: child {idea.id} would have no leaves")
        moving += thesis_ids
    if len(set(moving)) != len(moving):
        raise ValueError("split_idea: the same leaf was given to two children")

    def txn(tx):
        # Same traversal as counts/all_theses/ideas_without_leaves (`13` §4.2 point 5):
        # a leaf is a thesis whose source exists.
        owned = {r["id"] for r in tx.run(
            "MATCH (:Idea {id: $parent_id})-[:HAS_LEAF]->(t:Thesis)<-[:YIELDS]-(:Source) "
            "RETURN t.id AS id", parent_id=parent_id)}
        stolen = sorted(set(moving) - owned)
        if stolen:
            raise ValueError(f"split_idea: leaves {stolen} are not leaves of {parent_id}")
        if not owned - set(moving):
            raise ValueError(f"split_idea: would move every leaf off {parent_id}, "
                             "leaving an idea with none")
        # `dirty` is forced True here, on the parent AND on every child, regardless of
        # what `parent_fields` carries (`rederive.derive` never touches it). Both leaf
        # sets just changed and neither has been judged over its new composition —
        # leaving either clean is the same "invisible to the sweep" defect
        # `write_theses` had (`13` §3.2 finding), through a second door.
        _update_idea(tx, parent_id, {**parent_fields, "dirty": True})
        for idea, thesis_ids in children:
            child_row = _idea_row(idea)
            child_row["dirty"] = True
            child_row["seq"] = _next_seq(tx)
            tx.run("CREATE (n:Idea $row)", row=child_row).consume()
            tx.run(
                "MATCH (old:Idea)-[r:HAS_LEAF]->(t:Thesis) WHERE t.id IN $ids "
                "MATCH (new:Idea {id: $new_id}) "
                "DELETE r "
                "MERGE (new)-[:HAS_LEAF]->(t)",
                ids=thesis_ids, new_id=idea.id).consume()

    with _session() as session:
        session.execute_write(txn)


def set_trust(idea_id: str, score: float) -> None:
    if not 0.0 <= score <= 1.0:
        raise ValueError(f"trust_score out of [0, 1]: {score!r}")
    with _session() as session:
        session.execute_write(lambda tx: _update_idea(
            tx, idea_id, {"trust_score": float(score), "dirty": False}))


# -------------------------------------------------------------------------- reads

def get_ideas(ids: list[str]) -> list[dict]:
    if not ids:
        return []
    with _session() as session:
        def txn(tx):
            idea_rows = list(tx.run("UNWIND $ids AS id MATCH (n:Idea {id: id}) RETURN n", ids=ids))
            leaf_rows = list(tx.run(
                "UNWIND $ids AS idea_id "
                "MATCH (i:Idea {id: idea_id})-[:HAS_LEAF]->(t:Thesis)<-[:YIELDS]-(s:Source) "
                "RETURN idea_id, t, s ORDER BY t.seq", ids=ids))
            return idea_rows, leaf_rows
        idea_rows, leaf_rows = session.execute_read(txn)
    leaves: dict[str, list[dict]] = {}
    for r in leaf_rows:
        leaves.setdefault(r["idea_id"], []).append(_leaf_out(dict(r["t"]), dict(r["s"]), r["idea_id"]))
    by_id = {}
    for r in idea_rows:
        d = _idea_out(r["n"])
        d["theses"] = leaves.get(d["id"], [])
        by_id[d["id"]] = d
    return [by_id[i] for i in ids if i in by_id]


def get_leaves(idea_id: str) -> list[dict]:
    with _session() as session:
        rows = session.execute_read(lambda tx: list(tx.run(
            "MATCH (i:Idea {id: $id})-[:HAS_LEAF]->(t:Thesis)<-[:YIELDS]-(s:Source) "
            "RETURN t, s ORDER BY t.seq", id=idea_id)))
    return [_leaf_out(dict(r["t"]), dict(r["s"]), idea_id) for r in rows]


def leaf_count(idea_id: str) -> int:
    """The one query that does NOT require a source, exactly like
    `stub_store.leaf_count` (map doc §"Reads"): counts `HAS_LEAF` edges alone."""
    with _session() as session:
        row = session.execute_read(lambda tx: tx.run(
            "MATCH (:Idea {id: $id})-[:HAS_LEAF]->(t:Thesis) RETURN count(t) AS c",
            id=idea_id).single())
    return row["c"]


def get_source(source_id: str) -> dict | None:
    with _session() as session:
        rows = session.execute_read(lambda tx: list(tx.run(
            "MATCH (s:Source {id: $id}) RETURN s", id=source_id)))
    return None if not rows else _source_out(dict(rows[0]["s"]))


def list_sources(limit: int = 50, offset: int = 0) -> list[dict]:
    with _session() as session:
        rows = session.execute_read(lambda tx: list(tx.run(
            "MATCH (s:Source) RETURN s ORDER BY s.retrieved_at, s.seq SKIP $offset LIMIT $limit",
            offset=offset, limit=limit)))
    return [_source_out(dict(r["s"])) for r in rows]


def list_idea_ids(limit: int = 50, offset: int = 0) -> list[str]:
    with _session() as session:
        rows = session.execute_read(lambda tx: list(tx.run(
            "MATCH (n:Idea) RETURN n.id AS id ORDER BY n.seq SKIP $offset LIMIT $limit",
            offset=offset, limit=limit)))
    return [r["id"] for r in rows]


def _thesis_filter(idea_id: str | None, source_id: str | None) -> tuple[str, dict]:
    where, params = [], {}
    if idea_id is not None:
        where.append("i.id = $idea_id")
        params["idea_id"] = idea_id
    if source_id is not None:
        where.append("s.id = $source_id")
        params["source_id"] = source_id
    return (" WHERE " + " AND ".join(where) if where else ""), params


_LEAF_TRAVERSAL = "MATCH (s:Source)-[:YIELDS]->(t:Thesis)<-[:HAS_LEAF]-(i:Idea)"


def list_theses(idea_id: str | None = None, source_id: str | None = None,
                limit: int = 50, offset: int = 0) -> list[dict]:
    where, params = _thesis_filter(idea_id, source_id)
    query = (f"{_LEAF_TRAVERSAL}{where} RETURN t, s, i.id AS idea_id "
             "ORDER BY t.seq SKIP $offset LIMIT $limit")
    with _session() as session:
        rows = session.execute_read(lambda tx: list(tx.run(
            query, offset=offset, limit=limit, **params)))
    return [_leaf_out(dict(r["t"]), dict(r["s"]), r["idea_id"]) for r in rows]


def count_theses(idea_id: str | None = None, source_id: str | None = None) -> int:
    where, params = _thesis_filter(idea_id, source_id)
    query = f"{_LEAF_TRAVERSAL}{where} RETURN count(DISTINCT t) AS c"
    with _session() as session:
        row = session.execute_read(lambda tx: tx.run(query, **params).single())
    return row["c"]


def get_thesis(thesis_id: str) -> dict | None:
    with _session() as session:
        rows = session.execute_read(lambda tx: list(tx.run(
            f"{_LEAF_TRAVERSAL} WHERE t.id = $id RETURN t, s, i.id AS idea_id", id=thesis_id)))
    return None if not rows else _leaf_out(dict(rows[0]["t"]), dict(rows[0]["s"]), rows[0]["idea_id"])


def counts() -> dict:
    """{sources, ideas, theses, edges} — same JOIN as every serving path (`13` §4.2
    point 5), so `/healthz`'s `in_sync` cannot lie over rows nobody can reach.
    `edges` reads the real `(:Idea)-[:RELATED]->(:Idea)` relationships block B
    writes (`13` §3.1, §4.4 p1) — A never creates one, but claiming 0 while B has
    built a thousand of them is a number that disagrees with what the page shows,
    not an honest reading of an empty table (MAJOR 8)."""
    with _session() as session:
        def txn(tx):
            sources = tx.run("MATCH (s:Source) RETURN count(s) AS c").single()["c"]
            ideas = tx.run("MATCH (n:Idea) RETURN count(n) AS c").single()["c"]
            theses = tx.run(f"{_LEAF_TRAVERSAL} RETURN count(DISTINCT t) AS c").single()["c"]
            edges = tx.run("MATCH (:Idea)-[r:RELATED]->(:Idea) RETURN count(r) AS c").single()["c"]
            return {"sources": sources, "ideas": ideas, "theses": theses, "edges": edges}
        return session.execute_read(txn)


def all_theses() -> list[dict]:
    """Every leaf + vector — the index reconciliation source (§6.19), same JOIN as
    every other "leaf" query here. A row missing `text`/`vector` (a Neo4j node with
    a hole, same class of bug `_require` catches on the other read paths) raises
    `ValueError` here too, instead of letting `list(None)` surface as a bare
    `TypeError` (MINOR 9) — the columns actually selected are a subset of Thesis's
    fields, so `_require` itself does not apply to this row shape."""
    query = f"{_LEAF_TRAVERSAL} RETURN t.id AS id, i.id AS idea_id, t.text AS text, " \
            "t.vector AS vector ORDER BY t.seq"
    with _session() as session:
        rows = session.execute_read(lambda tx: list(tx.run(query)))
    out = []
    for r in rows:
        if r["text"] is None or r["vector"] is None:
            raise ValueError(f"Thesis {r['id']}: missing required field ('text' or "
                             "'vector' is null) — a Neo4j node with a hole (13 §4.2)")
        out.append({"id": r["id"], "idea_id": r["idea_id"], "text": r["text"],
                    "vector": list(r["vector"])})
    return out


def ideas_without_leaves() -> list[str]:
    """Must always be empty (unless the idea is a legal hypothesis, `13` §5, checked
    one layer up). "Leaf" here means the same as everywhere else: a thesis whose
    source exists."""
    query = ("MATCH (n:Idea) WHERE NOT (n)-[:HAS_LEAF]->(:Thesis)<-[:YIELDS]-(:Source) "
             "RETURN n.id AS id")
    with _session() as session:
        rows = session.execute_read(lambda tx: list(tx.run(query)))
    return [r["id"] for r in rows]


def dirty_ideas(limit: int | None = None) -> list[str]:
    """Oldest first (`13` §3.2), `n.seq` standing in for `rowid` — see the module
    docstring."""
    query = "MATCH (n:Idea {dirty: true}) RETURN n.id AS id ORDER BY n.seq"
    if limit is not None:
        query += " LIMIT $limit"
    with _session() as session:
        rows = session.execute_read(lambda tx: list(tx.run(query, limit=limit)))
    return [r["id"] for r in rows]


# Hosts a wipe is allowed to erase. `localhost`/`127.0.0.1` for a developer running
# Neo4j directly, `neo4j` for the compose service name (`13` §4, `docker-compose.yml`).
# Anything else — `37b54210...databases.neo4j.io` (Aura, block B's, `07:79`) included
# — is refused, unconditionally: BLOCKER 2 is exactly a wipe that ran against
# "whatever NEO4J_URI names" with no check.
#
# Shared with `neo4j_load.py`'s `push(wipe=True)` — the SAME function, not a second
# copy: that script's whole purpose is writing into a remote host (Aura, `07:72`),
# so a "local only" answer there means `--wipe` never gets to run against it at all.
# That is deliberate (BLOCKER 2's second round: `push(wipe=True)` had zero guard and
# actually erased a seeded Aura database) — an unattended `--wipe` against a shared
# remote host nobody who ran the command necessarily owns is not a feature worth
# keeping; a maintainer who really needs to reset their own scratch instance can do
# it by hand in the Aura console, not through an automated flag.
_WIPE_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "neo4j"})


def _require_local_target(uri: str) -> None:
    from urllib.parse import urlparse

    host = urlparse(uri).hostname
    if host not in _WIPE_ALLOWED_HOSTS:
        raise RuntimeError(
            f"refusing a wipe against {uri!r}: host {host!r} is not one of "
            f"{sorted(_WIPE_ALLOWED_HOSTS)}. A wipe may only ever run against the lake's "
            "own local instance — never a URI that could be Aura or anyone else's "
            "database (13 §4.4 p1, BLOCKER 2). If this really is the local instance "
            "under a different hostname, add it to _WIPE_ALLOWED_HOSTS by hand; do not "
            "route around this check.")


def _paged(fetch, page: int = 500) -> list:
    out: list = []
    while True:
        batch = fetch(page, len(out))
        if not batch:
            return out
        out += batch


def migrate(wipe: bool = False) -> dict:
    """Move the live SQLite lake into this Neo4j and verify BY NUMBERS afterwards
    (`13` §4.4 point 3) — the step that silently dropped 60 theses' vectors on
    2026-07-29 (`07:78`) was a migration with no check on the far side.

    Deliberately reads the SQLite side through `stub_store` directly rather than
    through `graph_client` (the way `neo4j_load.build()` does): `graph_client`'s own
    two-directional refusal (`13` §4.1, see `_select_backend`) means a process
    cannot import it with `NEO4J_URI` set while `LAKE_STORE` stays at its `stub`
    default — exactly the combination this migration needs, reading the old store
    and writing the new one in the same run. That refusal is about the API SERVER
    never starting half-configured; a one-shot admin migration is not that server,
    and going one layer under `graph_client` for the read side is what keeps the
    refusal meaningful instead of routed around.

    Writes go through this module's OWN `write_source`/`create_idea`/`write_theses`
    — not `neo4j_load.push()`'s separate Cypher — so a migrated node is shaped
    identically to one this backend would have written itself: `leaf_key` set from
    the same formula, the same MATCH-before-CREATE ownership check, the same
    `ConstraintError` on a row already present. Reusing `neo4j_load.push()` here
    would need a second write path patched to add `leaf_key`, which is a second
    definition of an idempotency key the module docstring already argues against.

    `wipe` stays a plain default-`False` argument rather than being removed
    outright (BLOCKER 2 asked the question): a one-shot admin migration that
    cannot be re-run against a scratch instance is worse ergonomics for no safety
    gain, once the real danger — wiping a URI nobody checked — has its own guard
    below (`_require_local_target`). What changes is that `wipe=True` no longer
    trusts `NEO4J_URI` at face value.
    """
    from . import stub_store

    if wipe:
        # The guard must run before ANYTHING touches the target (review, `13` §10):
        # `_get_driver()` does not just open a connection, it also WRITES the
        # schema constraints into whatever it connects to — so checking `_uri`
        # only after calling it (the previous order) let a non-local NEO4J_URI get
        # constraints written into a remote database before the wipe was ever
        # refused. Fixed by checking the URI this call is ABOUT to use, before
        # calling it at all: if a driver already exists in this process, that is
        # the already-connected `_uri` (`os.environ` can have drifted since that
        # connection was made, `13` MAJOR 4 / BLOCKER 2 — the reason the second
        # check below still reads `_uri` and not `os.environ` again); if no driver
        # exists yet, `os.environ["NEO4J_URI"]` IS what `_get_driver()` is about to
        # read, so there is no drift possible before a connection exists at all.
        _require_local_target(_uri if _driver is not None else os.environ.get("NEO4J_URI", ""))
        _get_driver()
        _require_local_target(_uri)  # defense in depth: what the driver actually used
        with _session() as session:
            session.execute_write(lambda tx: tx.run("MATCH (n) DETACH DELETE n").consume())

    before = stub_store.counts()
    sources = _paged(stub_store.list_sources)
    idea_ids = _paged(stub_store.list_idea_ids)
    ideas = []
    for start in range(0, len(idea_ids), 500):
        ideas += stub_store.get_ideas(idea_ids[start:start + 500])
    vectors = {leaf["id"]: leaf["vector"] for leaf in stub_store.all_theses()}
    if (len(sources), len(ideas)) != (before["sources"], before["ideas"]):
        raise ValueError(f"read {len(sources)} sources / {len(ideas)} ideas, "
                         f"stub_store.counts() says {before} — a partial read looks like "
                         "a smaller lake, not a failure")

    for row in sources:
        write_source(Source(**row))
    for row in ideas:
        create_idea(Idea(**{k: v for k, v in row.items() if k != "theses"}))
    theses_written = 0
    for row in ideas:
        for leaf in row["theses"]:
            thesis = Thesis(id=leaf["id"], source_id=leaf["source_id"], idea_id=leaf["idea_id"],
                            text=leaf["text"], context=leaf["context"], effect=leaf["effect"],
                            locator=leaf["locator"], text_hash=leaf["text_hash"],
                            vector=vectors[leaf["id"]], created_at=leaf["created_at"])
            write_theses(leaf["source_id"], [thesis])
            theses_written += 1

    after = counts()
    if (after["sources"], after["ideas"], after["theses"]) != \
            (before["sources"], before["ideas"], before["theses"]):
        raise ValueError(f"migration count mismatch: sqlite had {before}, neo4j landed "
                         f"{after} — a partial load, not a failure")
    with _session() as session:
        holes = session.execute_read(lambda tx: {
            "ideas": tx.run(
                "MATCH (n:Idea) WHERE size(n.vector) <> 384 RETURN count(n) AS c").single()["c"],
            "theses": tx.run(
                "MATCH (t:Thesis) WHERE size(t.vector) <> 384 RETURN count(t) AS c").single()["c"],
        })
    if holes["ideas"] or holes["theses"]:
        raise ValueError(f"{holes} node(s) landed without a 384-float vector — "
                         "the 2026-07-29 regression, again (07:78)")
    # Two sides, kept apart on purpose (MAJOR 11): `**after` here used to overwrite
    # `sources`/`ideas`/`theses` with Neo4j's own self-report, so the return value
    # was that self-report echoed twice rather than the comparison "verify by
    # numbers" (§4.4 p3) promises — the read-from-SQLite counts never actually
    # appeared in the result at all.
    return {"read_from_sqlite": {"sources": len(sources), "ideas": len(ideas),
                                 "theses": theses_written},
            "counted_in_neo4j": after}


def trust_scale() -> float:
    """Fixed 1.0 — the judge already answers normalized (`13` §3.3); see
    `stub_store.trust_scale` for the long version of why there is no second formula."""
    return 1.0


def neighbors(ids: list[str], hops: int = 1, min_weight: float | None = None) -> list[dict]:
    """Breadth-first over `(:Idea)-[:RELATED]->(:Idea)`, `hop` stamped on every row —
    same shape as `stub_store.neighbors`'s `edge` rows (`source_id`, `target_id`,
    `type`, `note`, `weight`, `evidence`, `hop`). A never writes this relationship
    (`13` §3.1); it is block B's, and it may already hold real edges over the same
    instance (§4.4 p1). Reading `[]` unconditionally would be a claim about a
    database nobody queried (MAJOR 8) — same class of bug as `edges: 0` hardcoded
    in `counts()`, fixed the same way: read the graph."""
    if hops < 1:
        raise ValueError("hops must be >= 1")
    if not ids:
        return []
    out: list[dict] = []
    frontier = list(dict.fromkeys(ids))
    seen = set(frontier)
    with _session() as session:
        for hop in range(1, hops + 1):
            if not frontier:
                break
            where = " AND r.weight >= $min_weight" if min_weight is not None else ""
            query = ("MATCH (a:Idea)-[r:RELATED]->(b:Idea) WHERE a.id IN $frontier" + where +
                    " RETURN a.id AS source_id, b.id AS target_id, r.type AS type, "
                    "r.note AS note, r.weight AS weight, r.evidence AS evidence")
            params = {"frontier": frontier}
            if min_weight is not None:
                params["min_weight"] = min_weight
            rows = session.execute_read(lambda tx, q=query, p=params: list(tx.run(q, **p)))
            frontier = []
            for row in rows:
                edge = dict(row)
                edge["hop"] = hop
                out.append(edge)
                if edge["target_id"] not in seen:
                    seen.add(edge["target_id"])
                    frontier.append(edge["target_id"])
    return out


# ---------------------------------------------------------------------- self-check

if __name__ == "__main__":
    from . import stub_store
    from .models import new_idea_id, new_thesis_id, source_id as make_source_id, text_hash

    # --- offline: pure-Python guards, no server needed, must run even when SKIPPED
    # below — these are exactly the two functions mutation testing named (MAJOR 5,
    # BLOCKER 2) and a check that only runs with a live Neo4j is a check that never
    # runs in plain CI. -----------------------------------------------------------
    try:
        _require(Idea, {"id": "hole"})
    except ValueError as exc:
        assert "hole" in str(exc) and "missing required field" in str(exc), exc
    else:
        raise AssertionError("_require must raise on a node with a hole")
    print("ok: _require raises ValueError on a node missing a required field (offline)")

    try:
        _require_local_target("neo4j+s://deadbeef00.databases.neo4j.io")
    except RuntimeError as exc:
        assert "deadbeef00" in str(exc)
    else:
        raise AssertionError("a wipe must refuse a non-local NEO4J_URI (Aura, say)")
    _require_local_target("bolt://localhost:7687")   # must not raise
    _require_local_target("bolt://neo4j:7687")        # compose service name, must not raise
    print("ok: the wipe guard refuses a non-local URI, allows localhost/the compose "
          "service name (offline)")

    try:
        from neo4j import GraphDatabase
        _probe = GraphDatabase.driver("bolt://localhost:7687", auth=None)
        _probe.verify_connectivity()
        _probe.close()
    except Exception as exc:  # noqa: BLE001 — this is the loud-skip path, not a real handler
        print(f"SKIPPED: no Neo4j reachable at bolt://localhost:7687 ({type(exc).__name__}: {exc}). "
              "Bring one up with `docker compose up -d neo4j` and rerun.")
        raise SystemExit(0)

    os.environ["NEO4J_URI"] = "bolt://localhost:7687"
    os.environ.pop("NEO4J_USERNAME", None)
    os.environ.pop("NEO4J_PASSWORD", None)
    os.environ["NEO4J_DATABASE"] = "neo4j"

    # MAJOR (second round): the wipe guard must validate the URI the DRIVER
    # actually connected with, not `os.environ` re-read at guard time. Reproduced
    # by connecting, then mutating the environment variable to a different host: a
    # guard that re-reads the environment would now check a string the open
    # connection never used.
    _get_driver()
    assert _uri == "bolt://localhost:7687", _uri
    os.environ["NEO4J_URI"] = "neo4j+s://deadbeef00.databases.neo4j.io"
    _require_local_target(_uri)  # must NOT raise: this is what migrate()/push() consult
    try:
        _require_local_target(os.environ["NEO4J_URI"])
    except RuntimeError:
        pass
    else:
        raise AssertionError("os.environ['NEO4J_URI'] no longer names the real "
                             "target — the exact divergence this guard exists for")
    os.environ["NEO4J_URI"] = "bolt://localhost:7687"  # restore for the rest of the run
    print("ok: the wipe guard validates the driver's actual connected URI, ignores "
          "a later change to the environment")

    # BLOCKER 1: this self-check must never be able to destroy a lake it does not
    # own. No blanket `MATCH (n) DETACH DELETE n` — it refuses outright against a
    # non-empty database instead of wiping indiscriminately.
    #
    # MAJOR (second round): "empty" means the DATABASE is empty — `MATCH (n)` —
    # not "empty of the labels this module knows about". `counts()` alone would
    # miss a stray `(:Concept)` or a `(:Thesis)` sitting outside the leaf
    # traversal (exactly block B's node shape, `07:79`) and call a database
    # "confirmed empty" while writing fixtures into it.
    with _session() as _s:
        existing = _s.execute_read(
            lambda tx: tx.run("MATCH (n) RETURN count(n) AS c").single()["c"])
    if existing != 0:
        print(f"REFUSED: bolt://localhost:7687 is not empty ({existing} node(s) total, "
              "MATCH (n) — not just :Source/:Idea/:Thesis reachable through the leaf "
              "traversal) — this self-check only ever runs against an empty scratch "
              "database, never one that might hold data it did not create (13 BLOCKER 1). "
              "Point it at an empty instance and rerun.")
        raise SystemExit(1)
    created_ids: list[str] = []
    print("ok: target database confirmed empty by MATCH (n), not just known labels, "
          "safe to write fixtures into")

    import tempfile
    from pathlib import Path

    # MAJOR (second round): a failed run must not poison the next one — every
    # fixture section below cleans up in a `finally`, so an assertion failure
    # midway still leaves the database empty for the next invocation. The
    # failure itself still propagates and this run still reports it.
    try:
        # MAJOR 11: migrate()'s return must show BOTH sides of "verify by numbers"
        # separately — it used to be `**after` silently overwriting the SQLite-read
        # counts with Neo4j's own self-report, so the result was that self-report
        # echoed twice rather than a comparison of two sides.
        with tempfile.TemporaryDirectory() as mig_tmp:
            stub_store._db_path = Path(mig_tmp) / "lake.db"
            stub_store._conn = None
            m_sid = make_source_id("https://arxiv.org/abs/2405.00099", "v1")
            stub_store.write_source(Source(id=m_sid, url="https://arxiv.org/abs/2405.00099",
                                           title="Migrate Me", type="paper", version="v1",
                                           retrieved_at="2026-07-30T00:00:00Z"))
            m_idea = Idea(id=new_idea_id(), text="migrated idea", applicability_conditions="ac",
                         limitations="lim", failure_modes=[], effect_claimed="", effect_observed="",
                         vector=[0.5] * 384)
            m_thesis = Thesis(id=new_thesis_id(), source_id=m_sid, idea_id=m_idea.id,
                              text="migrated leaf", context="ctx", effect="eff", locator="loc",
                              text_hash=text_hash("migrated leaf"), vector=[0.5] * 384,
                              created_at="2026-07-30T00:00:00Z")
            stub_store.create_idea_with_theses(m_idea, m_sid, [m_thesis])
            try:
                report = migrate()
                assert set(report) == {"read_from_sqlite", "counted_in_neo4j"}, report
                assert report["read_from_sqlite"] == {"sources": 1, "ideas": 1, "theses": 1}, report
                assert report["counted_in_neo4j"]["sources"] == 1
                assert report["counted_in_neo4j"]["ideas"] == 1
                assert report["counted_in_neo4j"]["theses"] == 1
            finally:
                # Cleaned up immediately, not deferred to the final sweep: the
                # fixture block right below compares neo4j_store against a FRESH
                # stub_store and would otherwise see these migrated rows as an
                # unexplained surplus — and it has to happen even if one of the
                # asserts above fails, or this run poisons the next one (MAJOR).
                with _session() as _s:
                    _s.run("MATCH (n) WHERE n.id IN $ids DETACH DELETE n",
                          ids=[m_sid, m_idea.id, m_thesis.id]).consume()
            assert counts() == {"sources": 0, "ideas": 0, "theses": 0, "edges": 0}
        print("ok: migrate() reports read_from_sqlite/counted_in_neo4j as two distinct "
              "sides, not one merged into the other (MAJOR 11)")

        with tempfile.TemporaryDirectory() as tmp:
            stub_store._db_path = Path(tmp) / "lake.db"
            stub_store._conn = None
            try:
                sid = make_source_id("https://arxiv.org/abs/2405.00001", "v1")
                created_ids.append(sid)
                src = Source(id=sid, url="https://arxiv.org/abs/2405.00001", title="A Paper",
                            type="paper", version="v1", retrieved_at="2026-07-28T10:00:00Z",
                            run_success=True, run_meta={"fitness_delta": 0.1})

                def make_thesis(text: str) -> Thesis:
                    return Thesis(id=new_thesis_id(), source_id=sid, idea_id="", text=text,
                                  context="ctx", effect="+3.1 pp", locator="Table 4",
                                  text_hash=text_hash(text), vector=[0.1] * 384,
                                  created_at="2026-07-28T10:00:00Z")

                idea = Idea(id=new_idea_id(), text="freeze the encoder", applicability_conditions="ac",
                            limitations="lim", failure_modes=["weak encoder -> semantics lost"],
                            effect_claimed="+3 pp", effect_observed="", vector=[0.2] * 384)
                created_ids.append(idea.id)
                t1, t2 = make_thesis("first leaf"), make_thesis("second leaf")
                created_ids += [t1.id, t2.id]
                t1.idea_id = t2.idea_id = idea.id

                write_source(src)
                stub_store.write_source(src)
                assert create_idea_with_theses(idea, sid, [t1, t2]) == [t1.id, t2.id]
                assert stub_store.create_idea_with_theses(idea, sid, [t1, t2]) == [t1.id, t2.id]
                print("ok: source + idea + 2 leaves written in one transaction, both backends")

                got_neo, got_stub = get_ideas([idea.id]), stub_store.get_ideas([idea.id])
                assert len(got_neo) == 1 and len(got_stub) == 1
                one_neo, one_stub = got_neo[0], got_stub[0]
                # Byte-for-byte parity except the pieces that are legitimately backend-shaped:
                # vectors are float32-vs-float64 across the two stores.
                for key in ("id", "text", "applicability_conditions", "limitations", "failure_modes",
                           "effect_claimed", "effect_observed", "differentiation", "origin",
                           "trust_score", "dirty", "rederived_at_leaf_count"):
                    assert one_neo[key] == one_stub[key], (key, one_neo[key], one_stub[key])
                assert len(one_neo["vector"]) == 384
                assert max(abs(a - b) for a, b in zip(one_neo["vector"], one_stub["vector"])) < 1e-6
                assert len(one_neo["theses"]) == 2 == len(one_stub["theses"])
                leaf_keys = {"id", "source_id", "idea_id", "text", "context", "effect", "locator",
                            "text_hash", "created_at", "source_type", "source_url", "source_title",
                            "run_success", "run_meta"}
                for leaf_n, leaf_s in zip(one_neo["theses"], one_stub["theses"]):
                    assert set(leaf_n) == leaf_keys, set(leaf_n)
                    for key in leaf_keys:
                        assert leaf_n[key] == leaf_s[key], (key, leaf_n[key], leaf_s[key])
                    assert "vector" not in leaf_n
                assert one_neo["dirty"] is True, "create_idea_with_theses must raise dirty"
                # MAJOR (second round): leaf order must match stub_store's exactly, not
                # merely have the same length — `ORDER BY id(t)` used to shuffle the
                # instant an internal id got reused; see the dedicated ordering test
                # further below for the exact repro.
                assert [t["id"] for t in one_neo["theses"]] == [t["id"] for t in one_stub["theses"]], \
                    "leaf order diverges from stub_store"
                print("ok: get_ideas parity, key-for-key and in the same order, against "
                      "stub_store for the same input")

                assert get_leaves(idea.id) == one_neo["theses"]
                assert leaf_count(idea.id) == 2 == stub_store.leaf_count(idea.id)
                print("ok: get_leaves / leaf_count parity")

                set_trust(idea.id, 0.7)
                stub_store.set_trust(idea.id, 0.7)
                after_neo, after_stub = get_ideas([idea.id])[0], stub_store.get_ideas([idea.id])[0]
                assert after_neo["trust_score"] == 0.7 == after_stub["trust_score"]
                assert after_neo["dirty"] is False, "set_trust is the one place dirty is lowered"
                assert trust_scale() == 1.0 == stub_store.trust_scale()
                try:
                    set_trust(idea.id, 1.4)
                except ValueError:
                    pass
                else:
                    raise AssertionError("a score outside [0, 1] must be refused")
                print("ok: set_trust parity, out-of-range refused")

                for c_neo, c_stub in ((counts(), stub_store.counts()),):
                    assert c_neo == c_stub, (c_neo, c_stub)
                assert count_theses() == stub_store.count_theses() == 2
                assert ideas_without_leaves() == stub_store.ideas_without_leaves() == []
                print("ok: counts / count_theses / ideas_without_leaves agree with stub_store")

                # MINOR (second round): write_theses with an idea_id that does not
                # exist must raise on BOTH backends, not raise on neo4j and silently
                # write a dangling leaf on stub (a row no JOIN-based count can reach).
                ghost = make_thesis("orphaned by design")
                ghost.idea_id = "idea_does_not_exist"
                try:
                    write_theses(sid, [ghost])
                except ValueError:
                    pass
                else:
                    raise AssertionError("neo4j write_theses accepted a nonexistent idea_id")
                try:
                    stub_store.write_theses(sid, [ghost])
                except ValueError:
                    pass
                else:
                    raise AssertionError("stub_store.write_theses accepted a nonexistent idea_id")
                assert leaf_count(idea.id) == 2 == stub_store.leaf_count(idea.id), \
                    "the refused write must not have landed partially"
                print("ok: write_theses refuses a nonexistent idea_id on both backends (MINOR)")

                # `13` finding, review 2026-07-31: `write_theses` used to append a leaf
                # WITHOUT raising `dirty` — `create_idea_with_theses` raised it, this
                # append-only sibling silently did not, contradicting the module
                # docstring's own claim that the flag is raised "in the same
                # transaction as the leaves" for every write. `idea` is clean here
                # (`set_trust` above lowered it), so a leaf landing through this path
                # is exactly the reproduction. stub_store's own parity proof lives in
                # `lake/selfcheck.py` check 29 (an isolated store); proven against
                # NEO4J here specifically, then removed again immediately — leaving
                # the extra leaf behind would move every hardcoded leaf/thesis count
                # below (MAJOR 6 and the rest of this fixture block) off by one.
                assert not get_ideas([idea.id])[0]["dirty"], "idea must be clean before this proof"
                appended = make_thesis("write_theses must raise dirty too")
                appended.idea_id = idea.id
                assert write_theses(sid, [appended]) == [appended.id]
                assert get_ideas([idea.id])[0]["dirty"] is True, \
                    "neo4j write_theses did not raise dirty"
                with _session() as _s:
                    _s.run("MATCH (n:Thesis {id: $id}) DETACH DELETE n", id=appended.id).consume()
                set_trust(idea.id, 0.7)      # restore the exact pre-proof state
                assert leaf_count(idea.id) == 2, "the proof's own leaf must not survive it"
                print("ok: write_theses raises dirty, same as create_idea_with_theses "
                      "(13 finding 2026-07-31)")

                # Same defect, a second door (`13` review 2026-07-31): `split_idea`
                # re-homed leaves onto a brand-new child with the Idea model's
                # defaults (dirty=False) and left the parent's flag untouched, though
                # both leaf sets just changed. `idea` is clean here (`set_trust` above
                # restored it to 0.7/clean) — exactly the live repro's starting state
                # ("judged to 0.5/clean, then split").
                assert not get_ideas([idea.id])[0]["dirty"], "idea must be clean before this proof"
                child = Idea(id=new_idea_id(), text="split child", applicability_conditions="ac",
                            limitations="lim", failure_modes=[], effect_claimed="",
                            effect_observed="", vector=[0.5] * 384)
                created_ids.append(child.id)
                split_idea(idea.id, {}, [(child, [t2.id])])
                assert get_ideas([idea.id])[0]["dirty"] is True, \
                    "neo4j split_idea did not raise dirty on the parent"
                assert get_ideas([child.id])[0]["dirty"] is True, \
                    "neo4j split_idea did not raise dirty on the child"
                assert leaf_count(idea.id) == 1 and leaf_count(child.id) == 1
                # Undo: move the leaf back and drop the child, restoring the exact
                # pre-proof state (leaf_count(idea.id) == 2) for every fixture below.
                with _session() as _s:
                    _s.run(
                        "MATCH (:Idea)-[r:HAS_LEAF]->(t:Thesis {id: $tid}) "
                        "MATCH (old:Idea {id: $old_id}) "
                        "DELETE r MERGE (old)-[:HAS_LEAF]->(t)",
                        tid=t2.id, old_id=idea.id).consume()
                    _s.run("MATCH (n:Idea {id: $id}) DETACH DELETE n", id=child.id).consume()
                set_trust(idea.id, 0.7)      # restore the exact pre-proof state
                assert leaf_count(idea.id) == 2, "the proof's own move must not survive it"
                print("ok: split_idea raises dirty on the parent AND the child, closing "
                      "the same door write_theses had (13 review 2026-07-31)")

                # MAJOR 6: counts()/count_theses()/all_theses()/list_theses() must all apply
                # the same source-join (`_LEAF_TRAVERSAL`), not a raw `MATCH (t:Thesis)`. A
                # Thesis with no Source/Idea edges at all sits outside that traversal; if any
                # of the four dropped the join, it alone would start disagreeing with the
                # other three.
                orphan_id = new_thesis_id()
                created_ids.append(orphan_id)
                with _session() as _s:
                    _s.run(
                        "CREATE (t:Thesis {id: $id, leaf_key: $key, text: 'orphan', "
                        "context: 'x', effect: 'x', locator: 'x', text_hash: 'x', "
                        "vector: [0.0], created_at: 'now'})",
                        id=orphan_id, key=f"orphan|{orphan_id}").consume()
                assert counts()["theses"] == 2, "an unlinked Thesis must not be counted"
                assert count_theses() == 2
                assert len(all_theses()) == 2
                assert len(list_theses(limit=100)) == 2
                print("ok: counts/count_theses/all_theses/list_theses all agree with a Thesis "
                      "outside the traversal present (MAJOR 6)")

                dup = make_thesis("FIRST   Leaf")   # same normalize() -> same text_hash as t1
                dup.idea_id = idea.id
                try:
                    create_idea_with_theses(None, sid, [dup])
                except Exception as exc:
                    from neo4j.exceptions import ConstraintError
                    assert isinstance(exc, ConstraintError), f"wrong exception type: {type(exc)}"
                else:
                    raise AssertionError("duplicate (source_id, text_hash) was swallowed")
                assert leaf_count(idea.id) == 2, "a rejected duplicate must not have partially written"
                print("ok: re-writing the same leaf raises (ConstraintError on leaf_key)")

                idea2 = Idea(id=new_idea_id(), text="second idea", applicability_conditions="ac",
                            limitations="lim", failure_modes=[], effect_claimed="", effect_observed="",
                            vector=[0.3] * 384)
                created_ids.append(idea2.id)
                good = make_thesis("third leaf")
                broken = make_thesis("fourth leaf")
                created_ids += [good.id, broken.id]
                broken.vector = ["not a float"] * 384
                good.idea_id = broken.idea_id = idea2.id
                try:
                    create_idea_with_theses(idea2, sid, [good, broken])
                except TypeError:
                    pass
                else:
                    raise AssertionError("broken vector did not raise")
                assert get_ideas([idea2.id]) == [], "idea survived a rolled-back transaction"
                assert leaf_count(idea2.id) == 0
                with _session() as _s:
                    n = _s.run(f"{_LEAF_TRAVERSAL} RETURN count(DISTINCT t) AS c").single()["c"]
                assert n == 2, "the good leaf of the failed batch must not have been committed either"
                print("ok: a mid-transaction failure leaves neither the idea nor its leaves behind")

                neighbors_neo = neighbors([idea.id])
                assert neighbors_neo == [] == stub_store.neighbors([idea.id])
                try:
                    neighbors([idea.id], hops=0)
                except ValueError:
                    pass
                else:
                    raise AssertionError("hops=0 must raise")
                print("ok: neighbors() == [] when block B has not written any RELATED edge yet")

                # MAJOR 8: with a real edge in the graph (as block B would write, `13`
                # §4.4 p1), counts()/neighbors() must read it, not keep answering as if the
                # graph were still empty — verified live by the reviewer against exactly
                # this relationship shape.
                idea3 = Idea(id=new_idea_id(), text="edge target", applicability_conditions="ac",
                            limitations="lim", failure_modes=[], effect_claimed="", effect_observed="",
                            vector=[0.4] * 384)
                created_ids.append(idea3.id)
                create_idea(idea3)
                with _session() as _s:
                    _s.run(
                        "MATCH (a:Idea {id: $a}), (b:Idea {id: $b}) "
                        "CREATE (a)-[:RELATED {type: 'supports', note: 'n', weight: 0.8, "
                        "evidence: 'e'}]->(b)", a=idea.id, b=idea3.id).consume()
                assert counts()["edges"] == 1, "a real RELATED edge must be counted, not hardcoded 0"
                hop1 = neighbors([idea.id])
                assert len(hop1) == 1, hop1
                assert hop1[0]["target_id"] == idea3.id and hop1[0]["weight"] == 0.8 and hop1[0]["hop"] == 1
                assert neighbors([idea.id], min_weight=0.9) == [], "min_weight must filter the edge out"
                print("ok: counts()/neighbors() read a real RELATED edge instead of asserting 0/[] (MAJOR 8)")

                # MINOR 9: a leaf inside the traversal (proper Source + Idea edges) but
                # missing `vector` must raise ValueError from all_theses(), the same class
                # of error every other read path raises on a node with a hole — not a bare
                # TypeError from `list(None)`.
                hole_id = new_thesis_id()
                created_ids.append(hole_id)
                with _session() as _s:
                    _s.run(
                        "MATCH (s:Source {id: $sid}), (i:Idea {id: $iid}) "
                        "CREATE (t:Thesis {id: $tid, leaf_key: $key, text: 'has a hole', "
                        "context: 'x', effect: 'x', locator: 'x', text_hash: 'x', "
                        "created_at: 'now'}) "
                        "MERGE (s)-[:YIELDS]->(t) MERGE (i)-[:HAS_LEAF]->(t)",
                        sid=sid, iid=idea.id, tid=hole_id, key=f"{sid}|hole-{hole_id}").consume()
                try:
                    all_theses()
                except ValueError as exc:
                    assert hole_id in str(exc), exc
                else:
                    raise AssertionError("all_theses() must raise on a leaf missing vector, "
                                         "not a bare TypeError")
                print("ok: all_theses() raises ValueError, not a bare TypeError, on a leaf "
                      "with a hole (MINOR 9)")
            finally:
                # MAJOR (second round): unconditional, so an assertion failure above
                # never leaves fixtures behind to poison the next run's emptiness gate.
                with _session() as _s:
                    _s.run("MATCH (n) WHERE n.id IN $ids DETACH DELETE n", ids=created_ids).consume()
            final = counts()
            assert final == {"sources": 0, "ideas": 0, "theses": 0, "edges": 0}, final
            print("ok: self-check removed exactly the nodes it created, target database "
                  "empty again")

        # MAJOR (second round): `ORDER BY id(t)` is Neo4j's internal id, reused
        # after a delete — reproduced deterministically: create a 4-leaf idea,
        # delete it, create a 5-leaf idea, and `get_leaves` used to come back
        # shuffled. `seq` (an explicit, stored counter) does not have this problem.
        order_ids: list[str] = []
        try:
            order_sid = make_source_id("https://arxiv.org/abs/2405.00002", "v1")
            order_ids.append(order_sid)
            write_source(Source(id=order_sid, url="https://arxiv.org/abs/2405.00002",
                                title="Order Paper", type="paper", version="v1",
                                retrieved_at="2026-07-29T00:00:00Z"))

            def order_thesis(text: str, idea_id: str) -> Thesis:
                return Thesis(id=new_thesis_id(), source_id=order_sid, idea_id=idea_id, text=text,
                              context="c", effect="e", locator="l", text_hash=text_hash(text),
                              vector=[0.6] * 384, created_at="2026-07-29T00:00:00Z")

            gone_idea = Idea(id=new_idea_id(), text="gone", applicability_conditions="ac",
                             limitations="lim", failure_modes=[], effect_claimed="", effect_observed="",
                             vector=[0.6] * 384)
            gone_leaves = [order_thesis(f"four-leaf {i}", gone_idea.id) for i in range(4)]
            create_idea_with_theses(gone_idea, order_sid, gone_leaves)
            with _session() as _s:
                _s.run("MATCH (n) WHERE n.id IN $ids DETACH DELETE n",
                      ids=[gone_idea.id] + [t.id for t in gone_leaves]).consume()

            kept_idea = Idea(id=new_idea_id(), text="kept", applicability_conditions="ac",
                             limitations="lim", failure_modes=[], effect_claimed="", effect_observed="",
                             vector=[0.7] * 384)
            order_ids.append(kept_idea.id)
            kept_leaves = [order_thesis(f"five-leaf {i}", kept_idea.id) for i in range(5)]
            order_ids += [t.id for t in kept_leaves]
            create_idea_with_theses(kept_idea, order_sid, kept_leaves)

            got = get_leaves(kept_idea.id)
            assert [t["id"] for t in got] == [t.id for t in kept_leaves], (
                f"leaf order does not match insertion order after a delete freed lower "
                f"internal ids: got {[t['id'] for t in got]}, wrote "
                f"{[t.id for t in kept_leaves]}")
        finally:
            with _session() as _s:
                _s.run("MATCH (n) WHERE n.id IN $ids DETACH DELETE n", ids=order_ids).consume()
        print("ok: leaf order survives a delete that freed lower internal ids "
              "(seq, not id(t))")
    finally:
        # `_Seq` is this module's own infrastructure, not a named fixture: reset it
        # so the next invocation starts the counter at 1 and the emptiness gate
        # above stays exact (`MATCH (n)`, not "empty of known labels").
        with _session() as _s:
            _s.run("MATCH (n:_Seq) DETACH DELETE n").consume()
        close()

    print("neo4j_store self-check OK")
