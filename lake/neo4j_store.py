"""Neo4j (Bolt) backend behind `graph_client` (spec `13-run-ingest-and-graph-spec.md` §4).

The only backend (D11) — `stub_store.py` (SQLite) is gone, and `graph_client` no
longer picks between anything; it always imports this module.

Storage shape, and why it differs from the SQLite columns the old backend used:

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
  creating a Thesis with a dangling reference — SQLite's `thesis.idea_id` had no
  foreign key at all and would have accepted it. This is the one place behaviour
  intentionally tightens rather than reproduces the old backend's leniency: a bare
  "not found" is a bug worth surfacing, not an invariant worth keeping.

Listing order is an explicit `seq` property (`_next_seq`), not `id(n)`: Neo4j's
internal id is reused after a delete, so `ORDER BY id(n)` shuffles the moment any
node with a lower id is removed and a new one created — reproduced deterministically
below (create a 4-leaf idea, delete it, create a 5-leaf idea, `get_leaves` comes
back in a different order than it was written in). SQLite's `rowid` gave the old
backend stable insertion order for free (the thesis/idea tables never deleted
rows); `seq` is this module's equivalent, one shared counter node because
ordering is always queried within a single label at a time.

`neo4j` is imported lazily (inside `_get_driver`), so this module imports cleanly
even when the `neo4j` package or a reachable server is absent — `graph_client`
still refuses to start without `NEO4J_URI` (D11), but that refusal never depends
on this import succeeding first.
"""
import json
import os
import threading
from typing import get_args

from .models import Idea, Source, Thesis

# --------------------------------------------------------------------- timeouts
# 2026-07-31 finding (live prod, stopped Neo4j container): POST /retrieve answered
# the right 503 — but only after 120+ seconds. Root cause, read off the driver
# source (`neo4j` 6.2, `_sync/work/session.py::_run_transaction`, the code every
# `execute_read`/`execute_write` call below goes through): a managed transaction
# retries a `ServiceUnavailable`, but only checks the `max_transaction_retry_time`
# budget AFTER a full attempt has already completed — the retry timer starts the
# instant the FIRST attempt fails, so `elapsed since timer start` is ~0 right then
# and never exceeds a positive budget, which means at least a SECOND full attempt
# always happens no matter how small that budget is. With the driver's own
# defaults (`connection_acquisition_timeout`=60s, `max_transaction_retry_time`=30s)
# one failed attempt alone can burn 60s, two of them are 120s, and the 30s retry
# budget only gets checked (and only stops a THIRD attempt) after that — exactly
# the "several attempts to resolve the name and connect" this finding describes.
#
# `/retrieve` is the hot path block C polls in a 12h loop (`08:293`) — a slow 503
# there is worse than a fast one the caller can retry itself next cycle, so reads
# get a short acquisition window and NO retry budget (`0.0`: per the math above,
# any positive number still forces a second full attempt, so `0.0` is the only
# value that actually caps a read at one attempt). Writes (phase 2 batch insert,
# the judge's `set_trust`, `split_idea`) keep the driver's own patience instead —
# aborting a batch on one transient blip is expensive to redo; a slow `/retrieve`
# reply is not.
CONNECTION_TIMEOUT = 3.0  # seconds, TCP handshake only — shared by both profiles
# below (it is a Pool-level setting, fixed at driver construction, `neo4j` has no
# per-session override). A live Neo4j on the same docker network answers in
# single-digit milliseconds, so 3s is generous for a live host and tiny next to
# "several minutes" for a dead one.
READ_CONNECTION_ACQUISITION_TIMEOUT = 5.0  # seconds; kept above CONNECTION_TIMEOUT
# per the driver's own guidance (acquisition wraps the connect attempt plus the
# Bolt handshake, so it must leave room for both).
READ_MAX_TRANSACTION_RETRY_TIME = 0.0  # no retry — see the timer-math above.
WRITE_CONNECTION_ACQUISITION_TIMEOUT = 60.0  # the driver's own default, named here
# so it sits next to the read profile instead of being an invisible library default.
WRITE_MAX_TRANSACTION_RETRY_TIME = 30.0  # the driver's own default, same reason.

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
                # `connection_timeout` is Pool-level (this call only) — session-level
                # `connection_acquisition_timeout`/`max_transaction_retry_time` are set
                # to the READ (fast-fail) profile here as the driver-wide default, since
                # this same driver also opens the constraint-check session right below,
                # which does not go through `_session()`'s write override. Writes ask
                # for the patient profile explicitly, per call, in `_session(write=True)`.
                _driver = GraphDatabase.driver(
                    uri, auth=auth, connection_timeout=CONNECTION_TIMEOUT,
                    connection_acquisition_timeout=READ_CONNECTION_ACQUISITION_TIMEOUT,
                    max_transaction_retry_time=READ_MAX_TRANSACTION_RETRY_TIME)
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


def _session(*, write: bool = False):
    """`write=True` (batch inserts, `set_trust`, `split_idea`, the two edge writers)
    gets the patient profile — worth the wait, an aborted batch is expensive to
    redo. Every other caller is on `/retrieve`'s read path or close to it and gets
    the driver's fast-fail default (module timeouts section above)."""
    if write:
        return _get_driver().session(
            database=_database,
            connection_acquisition_timeout=WRITE_CONNECTION_ACQUISITION_TIMEOUT,
            max_transaction_retry_time=WRITE_MAX_TRANSACTION_RETRY_TIME)
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
    with _session(write=True) as session:
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

    with _session(write=True) as session:
        return session.execute_write(txn)


def create_idea(idea: Idea) -> str:
    row = _idea_row(idea)
    with _session(write=True) as session:
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

    with _session(write=True) as session:
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
    with _session(write=True) as session:
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

    with _session(write=True) as session:
        session.execute_write(txn)


def set_trust(idea_id: str, score: float) -> None:
    if not 0.0 <= score <= 1.0:
        raise ValueError(f"trust_score out of [0, 1]: {score!r}")
    with _session(write=True) as session:
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
    `edges` reads the real `(:Idea)-[:RELATED]->(:Idea)` relationships — since D12
    A writes co-citation and `derived_from` edges itself, and B may also write to
    the same instance — claiming 0 unconditionally would be a number that
    disagrees with what the page shows, not an honest reading of an empty table
    (MAJOR 8)."""
    with _session() as session:
        def txn(tx):
            sources = tx.run("MATCH (s:Source) RETURN count(s) AS c").single()["c"]
            ideas = tx.run("MATCH (n:Idea) RETURN count(n) AS c").single()["c"]
            theses = tx.run(f"{_LEAF_TRAVERSAL} RETURN count(DISTINCT t) AS c").single()["c"]
            edges = tx.run("MATCH (:Idea)-[r:RELATED]->(:Idea) RETURN count(r) AS c").single()["c"]
            return {"sources": sources, "ideas": ideas, "theses": theses, "edges": edges}
        return session.execute_read(txn)


def count_edges(min_weight: float | None = None) -> int:
    """`(:Idea)-[:RELATED]->(:Idea)` rows, same filter as `all_edges`. Its own COUNT,
    never `len(all_edges(...))`: a page total computed from the page is a number that
    agrees with itself and with nothing else."""
    where = " WHERE r.weight >= $min_weight" if min_weight is not None else ""
    params = {} if min_weight is None else {"min_weight": min_weight}
    with _session() as session:
        return session.execute_read(lambda tx: tx.run(
            "MATCH (:Idea)-[r:RELATED]->(:Idea)" + where + " RETURN count(r) AS c",
            **params).single()["c"])


def all_edges(limit: int = 200, offset: int = 0, min_weight: float | None = None) -> list[dict]:
    """Every Idea-Idea edge, paged — the bulk read `neighbors` cannot be: drawing the
    lake through it costs one request per idea (859 of them at the time of writing),
    which over a tunnel is minutes and on stage is a demo that does not start.

    Row shape is `neighbors`'s, `hop` included and always 1: these are the edges
    themselves, not a traversal, and a null hop would make `EdgeOut` a different model
    depending on which route filled it."""
    where = " WHERE r.weight >= $min_weight" if min_weight is not None else ""
    params = {"limit": limit, "offset": offset}
    if min_weight is not None:
        params["min_weight"] = min_weight
    query = ("MATCH (a:Idea)-[r:RELATED]->(b:Idea)" + where +
             " RETURN a.id AS source_id, b.id AS target_id, r.type AS type, r.note AS note,"
             " r.weight AS weight, r.evidence AS evidence"
             " ORDER BY a.id, b.id SKIP $offset LIMIT $limit")
    with _session() as session:
        rows = session.execute_read(lambda tx: list(tx.run(query, **params)))
    out = []
    for row in rows:
        edge = dict(row)
        # Same pre-D12 legacy shape `neighbors` normalizes: a bare string `evidence`
        # fails `EdgeOut` validation and 500s the route.
        ev = edge.get("evidence")
        edge["evidence"] = None if ev is None else (ev if isinstance(ev, list) else [ev])
        edge["hop"] = 1
        out.append(edge)
    return out


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
#
# BLOCKER (third round, prod deploy 2026-07-3x): this allowlist alone stopped being
# enough the moment prod itself started using `NEO4J_URI=bolt://neo4j:7687` — the
# same compose SERVICE NAME a developer's throwaway local instance also uses
# (`docker-compose.yml`). Before that, "stub was the real backend and NEO4J_URI
# pointed at Aura or nothing" made hostname == "not Aura" == "safe"; now the prod
# lake (829 nodes, 27 sources, `07`) sits behind the exact hostname this allowlist
# was written to let through. A wipe against `bolt://neo4j:7687` can no longer tell
# "my scratch graph" from "the production lake" by hostname alone — it needs a
# SECOND, independent signal, not a longer or shorter host list. `_WIPE_ALLOWED_HOSTS`
# stays as the first gate (still refuses Aura/anything remote, unconditionally); the
# second gate is `_require_wipe_confirmed`, below.
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


def _require_wipe_confirmed(uri: str) -> None:
    """The wipe's second gate (BLOCKER, third round) — independent of hostname on
    purpose, because hostname stopped being able to tell dev from prod apart the day
    prod's own `NEO4J_URI` became `bolt://neo4j:7687` (the comment above
    `_WIPE_ALLOWED_HOSTS` has the full story). No *documented* command may wipe data
    on its own: `LAKE_CONFIRM_WIPE` must be set, by hand, at the moment of the call,
    to the EXACT URI being wiped. A doc or a script can tell an operator to run
    `--wipe`; it cannot tell them what today's `NEO4J_URI` happens to be, so a fixed
    string baked into a runbook (`LAKE_CONFIRM_WIPE=yes`, say) would defeat this the
    same way a hostname allowlist already does — the check is that the two values
    the operator typed and the process is about to erase are the SAME string, not
    that some confirmation variable merely exists.
    """
    confirm = os.environ.get("LAKE_CONFIRM_WIPE")
    if confirm != uri:
        raise RuntimeError(
            f"refusing a wipe against {uri!r}: LAKE_CONFIRM_WIPE is "
            f"{confirm!r}, not an exact match. Set LAKE_CONFIRM_WIPE={uri!r} "
            "by hand, in this shell, for this one call — never in .env.local or "
            "any file a deploy could carry to a host you did not mean to wipe "
            "(13 §4.4 p1, BLOCKER third round). This is deliberately independent "
            "of _require_local_target: a compose service literally named `neo4j` "
            "is prod now, and a hostname allowlist alone can no longer refuse that.")


def trust_scale() -> float:
    """Fixed 1.0 — the judge already answers normalized (`13` §3.3); see
    `stub_store.trust_scale` for the long version of why there is no second formula."""
    return 1.0


def neighbors(ids: list[str], hops: int = 1, min_weight: float | None = None) -> list[dict]:
    """Breadth-first over `(:Idea)-[:RELATED]->(:Idea)`, `hop` stamped on every row —
    same shape as the old `stub_store.neighbors`'s `edge` rows (`source_id`,
    `target_id`, `type`, `note`, `weight`, `evidence`, `hop`). Since D12 A writes
    this relationship itself (`write_cocitation_edges`, `write_derived_from_edges`,
    below) — the old `13` §3.1 "A never writes edges" no longer holds, and B may
    also hold edges over the same instance. Reading `[]` unconditionally would be a
    claim about a database nobody queried (MAJOR 8) — same class of bug as
    `edges: 0` hardcoded in `counts()`, fixed the same way: read the graph."""
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
                # BLOCKER (review 2026-07-31): `_COCITE_UPSERT` normalizes a legacy
                # scalar `evidence` to a list on WRITE, but a pre-D12 edge nobody has
                # re-touched since is still a bare string on READ — `EdgeOut.evidence`
                # is `list[str] | None`, so returning it raw 500s the response.
                ev = edge.get("evidence")
                edge["evidence"] = None if ev is None else (ev if isinstance(ev, list) else [ev])
                edge["hop"] = hop
                out.append(edge)
                if edge["target_id"] not in seen:
                    seen.add(edge["target_id"])
                    frontier.append(edge["target_id"])
    return out


# --------------------------------------------------------------- Idea-Idea edges
#
# D12: A writes `(:Idea)-[:RELATED {type}]->(:Idea)` itself, in the pipeline —
# co-citation in phase 2 (`ingest/run.py`), `derived_from` at synthesis
# (`idea_merger.write_hypothesis`). `13` §3.1 ("A never writes edges") is stale;
# the relationship label and the `type` property convention are unchanged (moved
# here from the standalone `idea_edges.py`, which is now a read-only backfill CLI
# over this same code, not a second implementation with its own driver).

CO_CITED = "related_via_source"     # goes into the edge's `type` property
DERIVED_FROM = "derived_from"

# BLOCKER 2 (`13`, review 2026-07-31): the gate is the number of DISTINCT IDEAS a
# source touches, not how many theses any one of them has under it. The old
# `WHERE thesis_count >= $min_theses` filtered per idea BEFORE pairing — a normal
# source gives each idea exactly one leaf, so on the real corpus no idea ever
# cleared a 2-thesis bar and `_COCITE_PAIRS` returned empty rows forever (D12/D14
# stayed dead code). Co-citation means "two ideas share a source", full stop; an
# idea needs exactly one leaf under the source to be a candidate, and the only
# threshold left is how many such candidates the source must have to form ANY
# pair at all — `$min_ideas`, checked once on the whole collected list, not per
# idea.
_COCITE_PAIRS = """
MATCH (s:Source {id: $source_id})-[:YIELDS]->(t:Thesis)<-[:HAS_LEAF]-(i:Idea)
WITH i, count(DISTINCT t) AS thesis_count
WITH collect({idea_id: i.id, count: thesis_count}) AS ideas
WHERE size(ideas) >= $min_ideas
UNWIND ideas AS a
UNWIND ideas AS b
WITH a, b
WHERE a.idea_id < b.idea_id
RETURN a.idea_id AS idea_a_id, a.count AS count_a, b.idea_id AS idea_b_id, b.count AS count_b
"""

# Idempotency (D12, "re-loading a source must not inflate co-citation weight"):
# `evidence` on a co-citation edge is not the last contributing source, it is the
# LIST of source ids that have ever contributed to it. `weight`/`evidence` only
# move if `$source_id` is not already in that list — a re-ingest of the same
# source recomputes the same pairs and increments, finds its own id already
# recorded, and both SETs become no-ops. A second, DIFFERENT source that touches
# the same idea pair still accumulates on top, which a plain "delete this
# source's edges first" scheme could not do without losing the other source's
# contribution (there is only one `weight`, not one per source).
#
# BLOCKER 4 (`13`, review 2026-07-31): `ON CREATE SET r.evidence = []` only fires
# for a brand-new edge — a `RELATED{type: related_via_source}` edge already
# sitting in the graph from before D12 (B's own write, or an earlier version of
# this same query) carried a SCALAR string, one source id, not a list, and
# `$source_id IN r.evidence` against a String is a Cypher type error that kills
# the whole write. The `WITH ... CASE WHEN r.evidence IS :: LIST<STRING> THEN
# r.evidence ELSE [r.evidence] END AS evidence` line is what makes the rest of
# the query see a list either way — a fresh edge's `[]` passes the predicate
# unchanged, a legacy scalar gets wrapped into a one-element list once and then
# behaves exactly like it had always been one. Verified live: an edge seeded with
# `evidence: 'legacy_source'` upserts to `['legacy_source', $source_id]` without
# raising (neo4j_store self-check, BLOCKER 4 section below).
_COCITE_UPSERT = """
MATCH (a:Idea {id: $idea_a_id})
MATCH (b:Idea {id: $idea_b_id})
MERGE (a)-[r:RELATED {type: $type}]->(b)
ON CREATE SET r.weight = 0, r.evidence = []
WITH r, CASE WHEN r.evidence IS :: LIST<STRING> THEN r.evidence ELSE [r.evidence] END AS evidence
SET r.weight = CASE WHEN $source_id IN evidence THEN r.weight ELSE r.weight + $increment END,
    r.evidence = CASE WHEN $source_id IN evidence THEN evidence ELSE evidence + $source_id END,
    r.note = $note,
    r.updated_at = datetime()
RETURN r.weight AS new_weight
"""

# `derived_from` is one direction (child -> parent, the direction IS the meaning)
# and the weight is SET, never accumulated: a hypothesis is derived from its
# parents exactly once, and a repeated call (idempotent by construction, `13` §6
# п.2 dedups the synthesis itself before this is ever reached) must not compound.
_DERIVED_UPSERT = """
MATCH (a:Idea {id: $idea_a_id})
MATCH (b:Idea {id: $idea_b_id})
MERGE (a)-[r:RELATED {type: $type}]->(b)
SET r.weight = $weight, r.note = $note, r.evidence = $evidence, r.updated_at = datetime()
RETURN r.weight AS new_weight
"""

DEFAULT_MIN_IDEAS = 2


def compute_cocitation_increment(count_a: int, count_b: int) -> float:
    """Weight added to one Idea-Idea co-citation edge for one shared Source.

    TODO (left as found, moved here with the rest of the co-citation pass, not
    this pass's job to fix): `min(count_a, count_b)` is a placeholder. It rewards
    two ideas for how many leaves EACH has under the source, not for how much the
    source specifically ties them together, so an idea with many unrelated leaves
    under one source inflates every pair it is in.
    """
    return min(count_a, count_b)


def _upsert_related(tx, idea_a_id: str, idea_b_id: str, *, type_: str, note: str,
                    query: str, **params) -> float | None:
    """One directed `RELATED` edge, inside a caller-supplied transaction. `None`
    means one of the two MATCHes found nothing — the edge was NOT written."""
    rec = tx.run(query, idea_a_id=idea_a_id, idea_b_id=idea_b_id, type=type_, note=note,
                **params).single()
    return rec["new_weight"] if rec else None


class _MissingEndpoint(Exception):
    """Raised inside `_write_cocite_pair`'s transaction the instant either
    direction's MATCH finds no node. Never caught by anything but that function
    itself — its only job is making `session.execute_write` roll the transaction
    back instead of committing the half that already ran (BLOCKER 3 below)."""


def _write_cocite_pair(session, idea_a_id: str, idea_b_id: str, *, source_id: str,
                       increment: float, note: str) -> float | None:
    """Both directions of one co-citation pair, in ONE transaction: forward and
    reverse either both land or neither does.

    BLOCKER 3 (`13`, review 2026-07-31): the two directions used to be two
    `_upsert_related` calls inside a `session.execute_write(txn)` whose `txn`
    returned normally either way — a MATCH finding nothing is an empty result,
    not a Cypher exception, so a reverse endpoint missing (a concurrent
    split/delete between the forward `tx.run` and the reverse one; Neo4j's
    default isolation re-reads from the store on every statement, so this is a
    real race, not a theoretical one) let the forward SET commit anyway: a
    one-sided edge, plus a poisoned `evidence` list the retry can no longer
    repair (`source_id` is already recorded, so the "missing" reverse never
    accumulates again). Raising `_MissingEndpoint` from inside `txn`, instead of
    returning `None` and letting the caller notice after the fact, is what makes
    `execute_write` discard the whole transaction — verified live: pairing a
    real idea against a bogus id, the forward edge does NOT survive (neo4j_store
    self-check, BLOCKER 3 section below).

    Returns the forward edge's new weight, or `None` if either MATCH found no
    node (nothing was written, on either side).
    """
    def txn(tx):
        forward = _upsert_related(tx, idea_a_id, idea_b_id, type_=CO_CITED, note=note,
                                  query=_COCITE_UPSERT, increment=increment, source_id=source_id)
        if forward is None:
            raise _MissingEndpoint
        reverse = _upsert_related(tx, idea_b_id, idea_a_id, type_=CO_CITED, note=note,
                                  query=_COCITE_UPSERT, increment=increment, source_id=source_id)
        if reverse is None:
            raise _MissingEndpoint
        return forward

    try:
        return session.execute_write(txn)
    except _MissingEndpoint:
        return None


def write_cocitation_edges(source_id: str, min_ideas: int = DEFAULT_MIN_IDEAS,
                           dry_run: bool = False) -> list[dict]:
    """Co-citation pass over ONE source's own leaves: every pair out of the ideas
    that have a leaf under `source_id` gets a `RELATED` edge, both directions
    (co-citation is symmetric; the stored edge is directed, module docstring),
    one weight increment — as long as the source touches at least `min_ideas`
    distinct ideas in the first place (BLOCKER 2: the gate is source-level idea
    count, never a per-idea thesis count — a normal source gives each idea
    exactly one leaf). Called from ingest phase 2 right after that source's
    ideas are committed (`ingest/run.py`), and from `idea_edges.py`'s backfill
    CLI once per already-loaded source.

    `dry_run=True` finds and returns the same pairs/increments, writes nothing —
    the backfill CLI's `--dry-run`; `weight` in each outcome is then the increment
    that WOULD apply, not a value read back from the graph.

    Returns one {"idea_a_id", "idea_b_id", "weight", "missing"} per pair found.
    `missing=True` (either direction's MATCH found no node) should not happen
    here — both ideas of a pair were just read off this same source — but is
    reported rather than silently dropped, since a concurrent split/delete makes
    it possible; `_write_cocite_pair` guarantees that case leaves neither
    direction written (BLOCKER 3).
    """
    with _session(write=True) as session:
        pairs = session.execute_read(lambda tx: [
            dict(r) for r in tx.run(_COCITE_PAIRS, source_id=source_id, min_ideas=min_ideas)])

        outcomes = []
        for rec in pairs:
            if dry_run:
                outcomes.append({"idea_a_id": rec["idea_a_id"], "idea_b_id": rec["idea_b_id"],
                                 "weight": compute_cocitation_increment(rec["count_a"],
                                                                        rec["count_b"]),
                                 "missing": False})
                continue
            increment = compute_cocitation_increment(rec["count_a"], rec["count_b"])
            note = f"co-cited by {source_id}"
            weight = _write_cocite_pair(session, rec["idea_a_id"], rec["idea_b_id"],
                                        source_id=source_id, increment=increment, note=note)
            outcomes.append({"idea_a_id": rec["idea_a_id"], "idea_b_id": rec["idea_b_id"],
                             "weight": weight, "missing": weight is None})
        return outcomes


def write_derived_from_edges(child_id: str, parent_ids: list[str],
                             dry_run: bool = False) -> list[dict]:
    """One `derived_from` edge per parent, child -> parent, weight fixed at 1.0 and
    SET rather than accumulated. Called from `idea_merger.write_hypothesis` right
    after the hypothesis and its synthetic leaf are committed, and from
    `idea_edges.py`'s backfill CLI for hypotheses synthesized before D12 (parentage
    read off the synthetic leaf's `locator`, `13` §6).

    `dry_run=True` writes nothing; every outcome reports `weight=1.0, missing=False`
    without a MATCH ever running — the backfill CLI's `--dry-run` cannot promise more
    than "this parent id looks parseable" without touching the graph, and this pass
    has no read-only existence check cheap enough to be worth a third query shape.

    Returns one {"idea_a_id", "idea_b_id", "weight", "missing"} per parent.
    `missing=True` means the parent id does not exist in the graph — reported,
    never invented.
    """
    if dry_run:
        return [{"idea_a_id": child_id, "idea_b_id": p, "weight": 1.0, "missing": False}
                for p in parent_ids]
    # list[str], one element: co-citation's `evidence` is a LIST of contributing source
    # ids (`_COCITE_UPSERT`) and `EdgeOut.evidence` (api/schemas.py) has to read either
    # edge's property as the same type — a scalar string here made a real co-citation
    # edge and a real derived_from edge answer the same field with two different Python
    # types, and `GET /ideas/{id}/neighbors` 500s the moment a co-citation edge is in the
    # response (review, 2026-07-31). The locator convention itself (`13` §6) is unchanged.
    evidence = ["synthesis/" + "+".join(parent_ids)]
    outcomes = []
    with _session(write=True) as session:
        for parent_id in parent_ids:
            weight = session.execute_write(lambda tx, p=parent_id: _upsert_related(
                tx, child_id, p, type_=DERIVED_FROM, note="синтез", query=_DERIVED_UPSERT,
                weight=1.0, evidence=evidence))
            outcomes.append({"idea_a_id": child_id, "idea_b_id": parent_id,
                             "weight": weight, "missing": weight is None})
    return outcomes


# ---------------------------------------------------------------------- self-check

if __name__ == "__main__":
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

    # BLOCKER (third round): the second, hostname-independent gate. Prod's own
    # NEO4J_URI is `bolt://neo4j:7687` — a URI `_require_local_target` alone must
    # allow (previous check, right above) — so `_require_wipe_confirmed` is what has
    # to catch it. Saved/restored so this offline block never leaks state into a
    # later part of this same process (a real live-graph wipe further below reads
    # this variable too).
    _saved_confirm = os.environ.get("LAKE_CONFIRM_WIPE")
    try:
        os.environ.pop("LAKE_CONFIRM_WIPE", None)
        try:
            _require_wipe_confirmed("bolt://neo4j:7687")
        except RuntimeError as exc:
            assert "LAKE_CONFIRM_WIPE" in str(exc), exc
        else:
            raise AssertionError("a wipe must refuse an unconfirmed URI even when "
                                 "_require_local_target alone would allow it — this "
                                 "is prod's own NEO4J_URI now")
        os.environ["LAKE_CONFIRM_WIPE"] = "bolt://neo4j:7687"
        _require_wipe_confirmed("bolt://neo4j:7687")   # exact match — must not raise
        try:
            _require_wipe_confirmed("bolt://localhost:7687")   # a DIFFERENT URI
        except RuntimeError:
            pass
        else:
            raise AssertionError("LAKE_CONFIRM_WIPE must match the URI being wiped "
                                 "exactly, not merely be set to something")
    finally:
        if _saved_confirm is None:
            os.environ.pop("LAKE_CONFIRM_WIPE", None)
        else:
            os.environ["LAKE_CONFIRM_WIPE"] = _saved_confirm
    print("ok: the wipe's second gate (LAKE_CONFIRM_WIPE) refuses unset/mismatched, "
          "requires an exact URI match, independent of the hostname allowlist (offline, "
          "BLOCKER third round)")

    # D12, offline: the query TEXT of the two edge upserts, pinned literally — this is
    # the check that used to live in `idea_edges.py`'s own `demo()` against a fake
    # session (RELATED_VIA_SOURCE, a first draft of this file, wrote edges nobody
    # read: `neighbors()`/`counts()` match `type` as a PROPERTY, not a second label —
    # module docstring). Moved here rather than dropped when `idea_edges.py` stopped
    # having its own Cypher to pin (review, check 34's coverage).
    assert compute_cocitation_increment(3, 5) == 3
    assert compute_cocitation_increment(2, 2) == 2
    assert compute_cocitation_increment(0, 4) == 0
    assert CO_CITED == "related_via_source" and DERIVED_FROM == "derived_from"
    # The literal `RELATED` spelled out, not `REL_LABEL` interpolated into itself:
    # setting the label to something else would move both sides together and stay
    # green (review, verified by mutation on the original file).
    assert "-[r:RELATED {type: $type}]->" in _COCITE_UPSERT, _COCITE_UPSERT
    assert "-[r:RELATED {type: $type}]->" in _DERIVED_UPSERT, _DERIVED_UPSERT
    assert _COCITE_UPSERT.count("MATCH (") == 2, "both endpoints MATCH; MERGE would invent them"
    assert _DERIVED_UPSERT.count("MATCH (") == 2, "both endpoints MATCH; MERGE would invent them"
    assert "MERGE (a)-[r:RELATED" in _COCITE_UPSERT, "MERGE, not CREATE: re-runs must not duplicate"
    assert "MERGE (a)-[r:RELATED" in _DERIVED_UPSERT, "MERGE, not CREATE: re-runs must not duplicate"
    # Co-citation accumulates ONLY for a source id not already recorded (D12
    # idempotency) — both halves of that CASE have to survive, or a re-ingest either
    # never accumulates (weight stuck at 0) or accumulates every time (the bug this
    # exists to prevent).
    assert "r.weight + $increment" in _COCITE_UPSERT and "$source_id IN evidence" in \
        _COCITE_UPSERT, _COCITE_UPSERT
    # BLOCKER 4: the normalized-to-list `evidence` variable, not the raw property,
    # is what the CASE above tests — a mutation that reverted `$source_id IN
    # evidence` back to `$source_id IN r.evidence` would still contain the string
    # "r.weight + $increment" and pass the assert above, so the type-predicate
    # normalization line is pinned separately.
    assert "r.evidence IS :: LIST<STRING>" in _COCITE_UPSERT, _COCITE_UPSERT
    assert "ELSE [r.evidence] END AS evidence" in _COCITE_UPSERT, _COCITE_UPSERT
    # derived_from SETS the weight outright — no `+`, or a second synthesis of the
    # same pair would compound it instead of staying at 1.0.
    assert "r.weight = $weight" in _DERIVED_UPSERT and "+" not in _DERIVED_UPSERT.split(
        "SET")[1].split(",")[0], _DERIVED_UPSERT
    print("ok: write_cocitation_edges/write_derived_from_edges write "
          "(:Idea)-[:RELATED {type: ...}]->(:Idea), the shape neighbors()/counts() read, "
          "MERGE not CREATE, co-citation accumulates once per source, derived_from is SET "
          "(offline)")

    # D11: this self-check may run standalone (bare `python3 -m lake.neo4j_store`,
    # localhost default for local dev) or as a subprocess of `lake.selfcheck` (6.31a),
    # which already validated whatever NEO4J_URI/USERNAME/PASSWORD/DATABASE it was
    # given — inherited here rather than clobbered with a hardcoded localhost, so this
    # exercises the SAME graph the rest of that suite runs against. Matters in CI,
    # where the compose service name is `neo4j`, not `localhost`.
    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user, password = os.environ.get("NEO4J_USERNAME"), os.environ.get("NEO4J_PASSWORD")
    try:
        from neo4j import GraphDatabase
        _probe = GraphDatabase.driver(uri, auth=(user, password) if user else None)
        _probe.verify_connectivity()
        _probe.close()
    except Exception as exc:  # noqa: BLE001 — this is the loud-skip path, not a real handler
        print(f"SKIPPED: no Neo4j reachable at {uri} ({type(exc).__name__}: {exc}). "
              "Bring one up with `docker compose up -d neo4j` and rerun.")
        # BLOCKER (review 2026-07-31): SKIPPED must exit non-zero, same as every
        # other module's demo()/main() (`link.py`, `split.py`, `vault.py`, ...) —
        # this was the one remaining entry point that greened without checking
        # anything, on an unreachable Neo4j alone.
        raise SystemExit(1)

    os.environ["NEO4J_URI"] = uri
    os.environ.setdefault("NEO4J_DATABASE", "neo4j")

    # MAJOR (second round): the wipe guard must validate the URI the DRIVER
    # actually connected with, not `os.environ` re-read at guard time. Reproduced
    # by connecting, then mutating the environment variable to a different host: a
    # guard that re-reads the environment would now check a string the open
    # connection never used.
    _get_driver()
    assert _uri == uri, _uri
    os.environ["NEO4J_URI"] = "neo4j+s://deadbeef00.databases.neo4j.io"
    _require_local_target(_uri)  # must NOT raise: this is what neo4j_load.py --wipe consults
    try:
        _require_local_target(os.environ["NEO4J_URI"])
    except RuntimeError:
        pass
    else:
        raise AssertionError("os.environ['NEO4J_URI'] no longer names the real "
                             "target — the exact divergence this guard exists for")
    os.environ["NEO4J_URI"] = uri  # restore for the rest of the run
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
        print(f"REFUSED: {uri} is not empty ({existing} node(s) total, "
              "MATCH (n) — not just :Source/:Idea/:Thesis reachable through the leaf "
              "traversal) — this self-check only ever runs against an empty scratch "
              "database, never one that might hold data it did not create (13 BLOCKER 1). "
              "Point it at an empty instance and rerun.")
        raise SystemExit(1)
    created_ids: list[str] = []
    print("ok: target database confirmed empty by MATCH (n), not just known labels, "
          "safe to write fixtures into")

    # MAJOR (second round): a failed run must not poison the next one — every
    # fixture section below cleans up in a `finally`, so an assertion failure
    # midway still leaves the database empty for the next invocation. The
    # failure itself still propagates and this run still reports it.
    try:
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
            assert create_idea_with_theses(idea, sid, [t1, t2]) == [t1.id, t2.id]
            print("ok: source + idea + 2 leaves written in one transaction")

            got = get_ideas([idea.id])
            assert len(got) == 1
            one = got[0]
            # Ground truth, not a second implementation's opinion: comparing against
            # what the fixture itself set catches a bug both sides of a parity
            # comparison could otherwise share (D11 — there is no second backend
            # left to compare against at all).
            expect = {"id": idea.id, "text": "freeze the encoder", "applicability_conditions": "ac",
                     "limitations": "lim", "failure_modes": ["weak encoder -> semantics lost"],
                     "effect_claimed": "+3 pp", "effect_observed": "", "differentiation": None,
                     "origin": "extracted", "trust_score": 0.0, "dirty": True,
                     "rederived_at_leaf_count": 0}
            for key, want in expect.items():
                assert one[key] == want, (key, one[key], want)
            assert len(one["vector"]) == 384
            assert max(abs(a - b) for a, b in zip(one["vector"], [0.2] * 384)) < 1e-6
            assert len(one["theses"]) == 2
            leaf_keys = {"id", "source_id", "idea_id", "text", "context", "effect", "locator",
                        "text_hash", "created_at", "source_type", "source_url", "source_title",
                        "run_success", "run_meta"}
            for leaf, written in zip(one["theses"], (t1, t2)):
                assert set(leaf) == leaf_keys, set(leaf)
                assert leaf["id"] == written.id and leaf["text"] == written.text
                assert (leaf["context"], leaf["effect"], leaf["locator"]) == \
                       ("ctx", "+3.1 pp", "Table 4")
                assert leaf["source_id"] == sid and leaf["idea_id"] == idea.id
                assert (leaf["source_type"], leaf["source_url"], leaf["source_title"]) == \
                       ("paper", src.url, src.title)
                assert leaf["run_success"] is True and leaf["run_meta"] == {"fitness_delta": 0.1}
                assert "vector" not in leaf
            # MAJOR (second round): leaf order must match insertion order, not merely
            # have the same length — `ORDER BY id(t)` used to shuffle the instant an
            # internal id got reused; see the dedicated ordering test further below.
            assert [t["id"] for t in one["theses"]] == [t1.id, t2.id], \
                "leaf order diverges from insertion order"
            print("ok: get_ideas matches the fixture, key-for-key and in insertion order")

            assert get_leaves(idea.id) == one["theses"]
            assert leaf_count(idea.id) == 2
            print("ok: get_leaves / leaf_count")

            set_trust(idea.id, 0.7)
            after = get_ideas([idea.id])[0]
            assert after["trust_score"] == 0.7
            assert after["dirty"] is False, "set_trust is the one place dirty is lowered"
            assert trust_scale() == 1.0
            try:
                set_trust(idea.id, 1.4)
            except ValueError:
                pass
            else:
                raise AssertionError("a score outside [0, 1] must be refused")
            print("ok: set_trust, out-of-range refused")

            assert counts() == {"sources": 1, "ideas": 1, "theses": 2, "edges": 0}
            assert count_theses() == 2
            assert ideas_without_leaves() == []
            print("ok: counts / count_theses / ideas_without_leaves")

            # MINOR (second round): write_theses with an idea_id that does not exist
            # must raise, not silently write a dangling leaf no JOIN-based count can
            # reach.
            ghost = make_thesis("orphaned by design")
            ghost.idea_id = "idea_does_not_exist"
            try:
                write_theses(sid, [ghost])
            except ValueError:
                pass
            else:
                raise AssertionError("write_theses accepted a nonexistent idea_id")
            assert leaf_count(idea.id) == 2, "the refused write must not have landed partially"
            print("ok: write_theses refuses a nonexistent idea_id (MINOR)")

            # `13` finding, review 2026-07-31: `write_theses` used to append a leaf
            # WITHOUT raising `dirty` — `create_idea_with_theses` raised it, this
            # append-only sibling silently did not, contradicting the module
            # docstring's own claim that the flag is raised "in the same
            # transaction as the leaves" for every write. `idea` is clean here
            # (`set_trust` above lowered it), so a leaf landing through this path
            # is exactly the reproduction; the same invariant on the OLD SQLite
            # backend lived in `lake/selfcheck.py` check 29 (an isolated store,
            # D11 — no second backend here to prove parity against any more).
            # Proven here, then removed again immediately — leaving the extra
            # leaf behind would move every hardcoded leaf/thesis count below
            # (MAJOR 6 and the rest of this fixture block) off by one.
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

            # BLOCKER 2 (`13`, review 2026-07-31), live: two ideas, ONE leaf EACH, under
            # their own source — exactly what a real corpus produces (an idea rarely
            # gets a second leaf from the SAME source). The old per-idea `min_theses`
            # gate meant this never co-cited on live data at all; the fix gates on the
            # source's distinct-idea count instead (`_COCITE_PAIRS` above).
            cocite_sid = make_source_id("https://arxiv.org/abs/2405.00003", "v1")
            created_ids.append(cocite_sid)
            write_source(Source(id=cocite_sid, url="https://arxiv.org/abs/2405.00003",
                                title="Co-cite Paper", type="paper", version="v1",
                                retrieved_at="2026-07-31T00:00:00Z"))
            cocite_a = Idea(id=new_idea_id(), text="cocite idea a", applicability_conditions="ac",
                            limitations="lim", failure_modes=[], effect_claimed="",
                            effect_observed="", vector=[0.55] * 384)
            cocite_b = Idea(id=new_idea_id(), text="cocite idea b", applicability_conditions="ac",
                            limitations="lim", failure_modes=[], effect_claimed="",
                            effect_observed="", vector=[0.56] * 384)
            created_ids += [cocite_a.id, cocite_b.id]

            def cocite_thesis(text: str, idea_id: str) -> Thesis:
                # Not `make_thesis` (line ~1051): that closure hardcodes `source_id=sid`,
                # the FIRST fixture source — a leaf whose `source_id` disagrees with the
                # batch it is inserted under makes `_create_thesis` raise ValueError.
                return Thesis(id=new_thesis_id(), source_id=cocite_sid, idea_id=idea_id,
                             text=text, context="ctx", effect="+1 pp", locator="p1",
                             text_hash=text_hash(text), vector=[0.1] * 384,
                             created_at="2026-07-31T00:00:00Z")

            cocite_leaf_a = cocite_thesis("cocite leaf a", cocite_a.id)
            cocite_leaf_b = cocite_thesis("cocite leaf b", cocite_b.id)
            created_ids += [cocite_leaf_a.id, cocite_leaf_b.id]
            create_idea_with_theses(cocite_a, cocite_sid, [cocite_leaf_a])
            create_idea_with_theses(cocite_b, cocite_sid, [cocite_leaf_b])
            assert counts()["edges"] == 0, "no cocitation edge before write_cocitation_edges runs"

            # BLOCKER 4, same fixture, live: a pre-existing edge with SCALAR `evidence`
            # (how B, or this module before D12, would have written one) must not make
            # the upsert raise a Cypher type error the moment a new source tries to
            # extend it — it normalizes to a list and keeps going. Both directions are
            # seeded, symmetrically, so the two directions below stay comparable —
            # co-citation always writes both (module docstring), so a pre-D12 edge
            # would have had both rows too, not just one.
            with _session() as _s:
                _s.run(
                    "MATCH (a:Idea {id: $a}), (b:Idea {id: $b}) "
                    "CREATE (a)-[:RELATED {type: $t, weight: 1.0, evidence: 'legacy_source', "
                    "note: 'pre-D12'}]->(b) "
                    "CREATE (b)-[:RELATED {type: $t, weight: 1.0, evidence: 'legacy_source', "
                    "note: 'pre-D12'}]->(a)",
                    a=cocite_a.id, b=cocite_b.id, t=CO_CITED).consume()

            cocite_outcomes = write_cocitation_edges(cocite_sid)
            assert len(cocite_outcomes) == 1 and cocite_outcomes[0]["missing"] is False, \
                cocite_outcomes
            assert {cocite_outcomes[0]["idea_a_id"], cocite_outcomes[0]["idea_b_id"]} == \
                {cocite_a.id, cocite_b.id}, cocite_outcomes
            # both directions written: the pre-seeded forward edge upgraded in place,
            # the reverse edge created fresh — 2 rows total, one pair.
            assert counts()["edges"] == 2, counts()
            cocite_fwd = neighbors([cocite_a.id])
            assert len(cocite_fwd) == 1 and cocite_fwd[0]["target_id"] == cocite_b.id, cocite_fwd
            assert cocite_fwd[0]["weight"] == 2.0, cocite_fwd     # 1.0 seeded + min(1,1) increment
            assert set(cocite_fwd[0]["evidence"]) == {"legacy_source", cocite_sid}, cocite_fwd
            assert isinstance(cocite_fwd[0]["evidence"], list), \
                "a legacy scalar must come back normalized to a list, not left as a string"
            cocite_back = neighbors([cocite_b.id])
            assert len(cocite_back) == 1 and cocite_back[0]["target_id"] == cocite_a.id, \
                cocite_back
            assert cocite_back[0]["weight"] == 2.0, cocite_back
            print("ok: two ideas with ONE leaf each under one source still co-cite, and a "
                  "pre-D12 scalar `evidence` upgrades to a list instead of raising a Cypher "
                  "type error (BLOCKER 2 + BLOCKER 4)")

            # BLOCKER 3, live: forward and reverse commit together or not at all.
            #
            # A bogus id for the reverse leg does NOT reproduce the race: with two
            # REAL, already-existing nodes, `MATCH (a:Idea {id: idea_a_id})` and
            # `MATCH (a:Idea {id: idea_b_id})` either both find their node or both
            # don't, regardless of which id plays "a" and which plays "b" — a single
            # bogus id fails BOTH legs identically, which was atomic even in the
            # buggy version this fix replaces (nothing to roll back: neither leg's
            # MATCH ever got past the WHERE to reach the MERGE/SET at all). That is
            # why the mutation this exact scenario was meant to catch — the original
            # BLOCKER 3 fix, with its `raise _MissingEndpoint` reverted back to a
            # plain `return forward` — passed THIS assertion clean (verified by
            # mutation while writing this check): both sides failing together says
            # nothing about whether the transaction is atomic.
            #
            # The real race is forward's MATCH succeeding, something committing for
            # it, and THEN reverse's failing — only a concurrent delete between the
            # two `tx.run()`s produces that, and this is single-threaded. Proven
            # instead by patching `_upsert_related` so the FIRST call runs the REAL
            # query against the two real nodes (genuinely updating the pre-existing
            # edge's weight from the BLOCKER 2/4 proof above) and the SECOND call
            # returns `None` without touching the graph — if `execute_write` truly
            # discards the whole transaction on the raise, that REAL update must not
            # survive either.
            edges_before_race = counts()["edges"]
            weight_before_race = neighbors([cocite_a.id])[0]["weight"]
            real_upsert_related = globals()["_upsert_related"]
            calls: list[int] = []

            def _flaky_upsert(tx, *args, **kwargs):
                calls.append(1)
                if len(calls) == 1:
                    return real_upsert_related(tx, *args, **kwargs)  # a REAL write
                return None                                          # a vanished endpoint

            globals()["_upsert_related"] = _flaky_upsert
            try:
                with _session() as _s:
                    result = _write_cocite_pair(_s, cocite_a.id, cocite_b.id,
                                                source_id="race_test", increment=1.0,
                                                note="race")
            finally:
                globals()["_upsert_related"] = real_upsert_related
            assert len(calls) == 2, "both legs must have been attempted"
            assert result is None, "the pair must report missing when either leg failed"
            assert counts()["edges"] == edges_before_race, \
                "the forward half's REAL write must not survive a reverse that failed after it"
            assert neighbors([cocite_a.id])[0]["weight"] == weight_before_race, \
                "the forward leg's weight bump must have rolled back too, not just its count"
            print("ok: co-citation writes both directions atomically — a REAL forward write "
                  "does not survive a reverse leg that fails right after it (BLOCKER 3)")

            # Undo: this proof's own source/ideas/leaves/edges are not part of the
            # fixture every assertion below counts against (MAJOR 6, MAJOR 8, MINOR 9
            # all hardcode counts that assume only `idea`'s 2 leaves exist) — dropped
            # here immediately, the same "prove then restore" shape as the `dirty`
            # proofs above, rather than left for the outer `finally` to find at the end.
            with _session() as _s:
                _s.run("MATCH (n) WHERE n.id IN $ids DETACH DELETE n",
                      ids=[cocite_sid, cocite_a.id, cocite_b.id, cocite_leaf_a.id,
                           cocite_leaf_b.id]).consume()
            assert counts()["edges"] == 0, "the cocitation proof must not leave edges behind"
            assert leaf_count(idea.id) == 2, "the cocitation proof must not touch idea's own leaves"

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

            assert neighbors([idea.id]) == []
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
