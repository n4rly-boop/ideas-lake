"""SQLite backend behind `graph_client` (spec 10 §3.4).

TEMPORARY: this file dies the day the graph moves to Neo4j. Nothing but
`graph_client` may import it — format B knowledge stays in one module (§3.4).

Storage notes:
- `failure_modes` and `run_meta` are JSON text in the table and parsed dicts/lists
  on the way out.
- vectors are float32 blobs (`array('f')`); thesis vectors are *not* returned by
  read paths, the thesis index lives in `index.py` (§3.5).
- `trust_score` is a stub computed at read time, never stored: log of the number of
  distinct sources under the idea (`06:226`, candidate of the first version). B
  replaces the value; the formula is smeared nowhere else.
"""
import json
import math
import sqlite3
import threading
from array import array
from pathlib import Path

from .models import LAKE_DB, Idea, Source, Thesis

# Exactly §3.4, plus IF NOT EXISTS so reopening an existing file is not an error.
# In `edge`, source_id/target_id are idea ids (edges are B's), not Source.id.
DDL = """
CREATE TABLE IF NOT EXISTS source(id TEXT PRIMARY KEY, url, title, type, version,
                    retrieved_at, run_success INT, run_meta TEXT);
CREATE TABLE IF NOT EXISTS idea(id TEXT PRIMARY KEY, text, applicability_conditions, limitations,
                  failure_modes TEXT, differentiation, effect_claimed, effect_observed,
                  trust_score REAL, dirty INT, rederived_at_leaf_count INT DEFAULT 0,
                  created_at, updated_at, vec BLOB);
CREATE TABLE IF NOT EXISTS thesis(rowid INTEGER PRIMARY KEY, id TEXT UNIQUE, source_id, idea_id,
                    text, context, effect, locator, text_hash, created_at, vec BLOB,
                    UNIQUE(source_id, text_hash));
CREATE TABLE IF NOT EXISTS edge(source_id, target_id, type, note, weight REAL, evidence TEXT,
                  PRIMARY KEY(source_id, target_id, type));
"""

# ponytail: one global lock over one shared connection. /retrieve is threaded
# (ThreadingHTTPServer, §5.4) and SQLite serialises writers anyway; if reads ever
# contend, give each thread its own connection instead.
_lock = threading.RLock()
_db_path: Path = LAKE_DB
_conn: sqlite3.Connection | None = None


def _c() -> sqlite3.Connection:
    """Open (once) the connection. Call under `_lock`."""
    global _conn
    if _conn is None:
        _db_path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(_db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(DDL)
    return _conn


# ------------------------------------------------------------------ encode/decode

def _column(name: str) -> str:
    return "vec" if name == "vector" else name


def _encode(name: str, value):
    """Dataclass field value -> column value."""
    if name in ("failure_modes", "run_meta"):
        return None if value is None else json.dumps(value)
    if name == "vector":
        return array("f", value).tobytes()
    if name in ("dirty", "run_success"):
        return None if value is None else int(value)
    return value


def _insert(conn: sqlite3.Connection, table: str, obj, replace: bool = False, **override) -> None:
    row = {_column(name): _encode(name, override.get(name, getattr(obj, name)))
           for name in type(obj).model_fields}
    verb = "INSERT OR REPLACE" if replace else "INSERT"
    cols = ",".join(row)
    conn.execute(f"{verb} INTO {table} ({cols}) VALUES ({','.join(':' + c for c in row)})", row)


def _placeholders(values) -> str:
    return ",".join("?" * len(values))


def _idea_out(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["failure_modes"] = json.loads(d["failure_modes"])
    d["dirty"] = bool(d["dirty"])
    d["vector"] = array("f", d.pop("vec")).tolist()
    return d


def _leaf_out(row: sqlite3.Row) -> dict:
    d = dict(row)
    d.pop("rowid", None)
    d.pop("vec", None)          # thesis vectors are the search index's business (§3.5)
    d["run_success"] = None if d["run_success"] is None else bool(d["run_success"])
    d["run_meta"] = None if d["run_meta"] is None else json.loads(d["run_meta"])
    return d


def _stub_trust(leaves: list[dict]) -> float:
    """log of the number of distinct sources under the idea (§3.4, `06:226`).
    1 + n keeps a single-source idea defined and above zero."""
    return math.log(1 + len({leaf["source_id"] for leaf in leaves}))


# ------------------------------------------------------------------------- writes

def _insert_theses(conn: sqlite3.Connection, source_id: str, theses: list[Thesis]) -> list[str]:
    for th in theses:
        if th.source_id and th.source_id != source_id:
            raise ValueError(f"thesis {th.id} carries source_id {th.source_id}, batch is {source_id}")
        _insert(conn, "thesis", th, source_id=source_id)
    return [th.id for th in theses]


def write_source(src: Source) -> str:
    # Re-fetching the same (url, version) yields the same id, so the row is replaced,
    # not duplicated. run_success/run_meta arrive later from C4 through the same call.
    with _lock, _c() as conn:
        _insert(conn, "source", src, replace=True)
    return src.id


def write_theses(source_id: str, theses: list[Thesis]) -> list[str]:
    with _lock, _c() as conn:
        return _insert_theses(conn, source_id, theses)


def create_idea(idea: Idea) -> str:
    with _lock, _c() as conn:
        _insert(conn, "idea", idea)
    return idea.id


def create_idea_with_theses(idea: Idea | None, source_id: str, theses: list[Thesis]) -> list[str]:
    """Idea and its leaves in ONE transaction (§3.4): a failure between the two would
    leave an idea with zero leaves, against `IDEA ||--|{ THESIS` (`06:85`).
    `idea=None` means the idea already exists, only append leaves.
    `with conn` is BEGIN ... COMMIT, and ROLLBACK on any exception."""
    with _lock, _c() as conn:
        if idea is not None:
            _insert(conn, "idea", idea)
        return _insert_theses(conn, source_id, theses)


# No `update_thesis`, here or anywhere: thesis immutability (§1.2) is held by the
# absence of the method (§3.4). Do not add one.


_IDEA_FIELDS = set(Idea.model_fields) - {"id"}


def update_idea(idea_id: str, fields: dict) -> None:
    if not fields:
        raise ValueError("update_idea called with no fields")
    unknown = set(fields) - _IDEA_FIELDS
    if unknown:                     # a typo must not become a silent no-op
        raise ValueError(f"unknown Idea fields: {sorted(unknown)}")
    cols = [_column(k) for k in fields]
    values = [_encode(k, v) for k, v in fields.items()]
    sql = f"UPDATE idea SET {', '.join(c + '=?' for c in cols)} WHERE id=?"
    with _lock, _c() as conn:
        cur = conn.execute(sql, values + [idea_id])
    if cur.rowcount != 1:
        raise KeyError(f"idea {idea_id} not found")


# -------------------------------------------------------------------------- reads

_LEAF_SQL = """SELECT t.*, s.type AS source_type, s.url AS source_url, s.title AS source_title,
                      s.run_success AS run_success, s.run_meta AS run_meta
               FROM thesis t JOIN source s ON s.id = t.source_id
               WHERE t.idea_id IN ({q}) ORDER BY t.rowid"""


def _leaves(conn: sqlite3.Connection, idea_ids: list[str]) -> dict[str, list[dict]]:
    rows = conn.execute(_LEAF_SQL.format(q=_placeholders(idea_ids)), idea_ids).fetchall()
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(row["idea_id"], []).append(_leaf_out(row))
    return out


def get_ideas(ids: list[str]) -> list[dict]:
    """Ideas with their leaves already joined to source.type/url/title (§3.4), in the
    order asked. Ids with no row are simply absent from the result."""
    if not ids:
        return []
    with _lock:
        conn = _c()
        rows = conn.execute(f"SELECT * FROM idea WHERE id IN ({_placeholders(ids)})", ids).fetchall()
        leaves = _leaves(conn, ids)
    by_id = {}
    for row in rows:
        d = _idea_out(row)
        d["theses"] = leaves.get(d["id"], [])
        d["trust_score"] = _stub_trust(d["theses"])
        by_id[d["id"]] = d
    return [by_id[i] for i in ids if i in by_id]


def get_leaves(idea_id: str) -> list[dict]:
    """Leaves of one idea, same join as `get_ideas` (rederive needs it, §4.6)."""
    with _lock:
        return _leaves(_c(), [idea_id]).get(idea_id, [])


def leaf_count(idea_id: str) -> int:
    with _lock:
        return _c().execute("SELECT COUNT(*) FROM thesis WHERE idea_id=?", (idea_id,)).fetchone()[0]


def all_theses() -> list[dict]:
    """Every leaf with its vector — the reconciliation source for the index (§6.19).
    Replaying `staging.jsonl` cannot do this: `idea_id` is assigned in phase 2, so a
    staging line does not carry one. The store does, and it holds the vectors too."""
    with _lock:
        rows = _c().execute(
            "SELECT id, idea_id, text, vec FROM thesis ORDER BY rowid"
        ).fetchall()
    return [{"id": r["id"], "idea_id": r["idea_id"], "text": r["text"],
             "vector": array("f", r["vec"]).tolist()} for r in rows]


def ideas_without_leaves() -> list[str]:
    """Must always be empty (§4.7 report, selfcheck §6.17): `IDEA ||--|{ THESIS`."""
    with _lock:
        rows = _c().execute(
            "SELECT i.id FROM idea i LEFT JOIN thesis t ON t.idea_id = i.id"
            " WHERE t.id IS NULL"
        ).fetchall()
    return [r["id"] for r in rows]


def trust_scale() -> float:
    """Max trust over the whole graph — the FIXED scale `rank.py` normalizes by (§5.3).
    Normalizing by the returned page instead would make the 0.15 weight relative and
    leave nothing to calibrate it on."""
    with _lock:
        rows = _c().execute(
            "SELECT idea_id, COUNT(DISTINCT source_id) AS n FROM thesis GROUP BY idea_id"
        ).fetchall()
    return max((math.log(1 + r["n"]) for r in rows), default=1.0) or 1.0


def neighbors(ids: list[str], hops: int = 1, min_weight: float | None = None) -> list[dict]:
    """Edges out of `ids`, breadth-first, `hop` on every row. `edge` is empty in the
    MVP (edges are B's), so this returns [] and ranking degrades to flat top-k —
    planned degradation, not an error (§3.4, `08:377`)."""
    if hops < 1:
        raise ValueError("hops must be >= 1")
    out: list[dict] = []
    frontier = list(dict.fromkeys(ids))
    seen = set(frontier)
    with _lock:
        conn = _c()
        for hop in range(1, hops + 1):
            if not frontier:
                break
            sql = f"SELECT * FROM edge WHERE source_id IN ({_placeholders(frontier)})"
            params: list = list(frontier)
            if min_weight is not None:
                sql += " AND weight >= ?"
                params.append(min_weight)
            rows = conn.execute(sql, params).fetchall()
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
    import tempfile

    from .models import new_idea_id, new_thesis_id, source_id as make_source_id, text_hash

    with tempfile.TemporaryDirectory() as tmp:
        _db_path = Path(tmp) / "lake.db"

        sid = make_source_id("https://arxiv.org/abs/2405.00001", "v1")
        write_source(Source(id=sid, url="https://arxiv.org/abs/2405.00001",
                            title="A Paper", type="paper", version="v1",
                            retrieved_at="2026-07-28T10:00:00Z",
                            run_success=True, run_meta={"fitness_delta": 0.1}))

        def make_thesis(text: str) -> Thesis:
            return Thesis(id=new_thesis_id(), source_id=sid, idea_id="", text=text,
                          context="ctx", effect="+3.1 pp", locator="Table 4",
                          text_hash=text_hash(text), vector=[0.1] * 384,
                          created_at="2026-07-28T10:00:00Z")

        idea = Idea(id=new_idea_id(), text="freeze the encoder", applicability_conditions="ac",
                    limitations="lim", failure_modes=["weak encoder -> semantics lost"],
                    effect_claimed="+3 pp", effect_observed="", vector=[0.2] * 384)
        t1, t2 = make_thesis("first leaf"), make_thesis("second leaf")
        t1.idea_id = t2.idea_id = idea.id
        assert create_idea_with_theses(idea, sid, [t1, t2]) == [t1.id, t2.id]

        got = get_ideas([idea.id])
        assert len(got) == 1, got
        one = got[0]
        assert one["failure_modes"] == ["weak encoder -> semantics lost"]
        assert len(one["vector"]) == 384 and abs(one["vector"][0] - 0.2) < 1e-6
        assert len(one["theses"]) == 2
        for leaf in one["theses"]:
            assert leaf["source_url"] == "https://arxiv.org/abs/2405.00001"
            assert leaf["source_title"] == "A Paper"
            assert leaf["source_type"] == "paper"
            assert leaf["run_success"] is True
            assert leaf["run_meta"] == {"fitness_delta": 0.1}
            assert "vec" not in leaf
        assert abs(one["trust_score"] - math.log(2)) < 1e-9, one["trust_score"]
        assert get_leaves(idea.id) == one["theses"]
        assert leaf_count(idea.id) == 2
        assert neighbors([idea.id]) == []
        assert neighbors([idea.id], hops=2, min_weight=0.5) == []
        print("ok: source + idea + 2 leaves written in one transaction, leaves joined to source")

        update_idea(idea.id, {"text": "freeze the pretrained encoder", "failure_modes": [],
                              "rederived_at_leaf_count": 2})
        after = get_ideas([idea.id])[0]
        assert after["text"] == "freeze the pretrained encoder"
        assert after["failure_modes"] == [] and after["rederived_at_leaf_count"] == 2
        for bad, exc in (({"concept": "x"}, ValueError), ({}, ValueError)):
            try:
                update_idea(idea.id, bad)
            except exc:
                pass
            else:
                raise AssertionError(f"update_idea({bad}) did not raise {exc.__name__}")
        try:
            update_idea("idea_nope", {"text": "x"})
        except KeyError:
            pass
        else:
            raise AssertionError("update_idea on a missing idea did not raise")
        print("ok: update_idea writes; unknown field, empty fields and missing idea all raise")

        dup = make_thesis("FIRST   Leaf")      # same normalize() -> same text_hash as t1
        dup.idea_id = idea.id
        assert dup.text_hash == t1.text_hash
        try:
            create_idea_with_theses(None, sid, [dup])
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("duplicate (source_id, text_hash) was swallowed")
        assert leaf_count(idea.id) == 2
        print("ok: duplicate (source_id, text_hash) raises IntegrityError, not a silent skip")

        idea2 = Idea(id=new_idea_id(), text="second idea", applicability_conditions="ac",
                     limitations="lim", failure_modes=[], effect_claimed="", effect_observed="",
                     vector=[0.3] * 384)
        good = make_thesis("third leaf")
        broken = make_thesis("fourth leaf")
        broken.vector = ["not a float"] * 384   # blows up mid-transaction, after the idea insert
        good.idea_id = broken.idea_id = idea2.id
        try:
            create_idea_with_theses(idea2, sid, [good, broken])
        except TypeError:
            pass
        else:
            raise AssertionError("broken vector did not raise")
        assert get_ideas([idea2.id]) == [], "idea survived a rolled back transaction"
        assert leaf_count(idea2.id) == 0
        with _lock:
            assert _c().execute("SELECT COUNT(*) FROM thesis").fetchone()[0] == 2
        print("ok: failure inside the transaction leaves zero ideas and zero leaves")

        idea3 = Idea(id=new_idea_id(), text="third idea", applicability_conditions="ac",
                     limitations="lim", failure_modes=[], effect_claimed="", effect_observed="",
                     vector=[0.4] * 384)
        t5 = make_thesis("fifth leaf")
        t5.idea_id = idea3.id
        assert create_idea(idea3) == idea3.id
        assert write_theses(sid, [t5]) == [t5.id]
        assert leaf_count(idea3.id) == 1
        try:
            write_theses("other_source", [t5])
        except ValueError:
            pass
        else:
            raise AssertionError("thesis written under a source_id it does not belong to")
        print("ok: create_idea + write_theses; a thesis from another source is refused")

    # §6.9: the name must be absent from the module, not merely unused. Parsed, not
    # grepped, so the comment explaining its absence does not trip the check.
    import ast

    tree = ast.parse((Path(__file__).resolve().parent / "graph_client.py").read_text(encoding="utf-8"))
    defined = {n.name for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    defined |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    assert "update_thesis" not in defined, \
        "graph_client grew an update_thesis: thesis immutability (§1.2, §3.4) is gone"
    print("ok: graph_client defines no update_thesis")
