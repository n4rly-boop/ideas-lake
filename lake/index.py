"""Thesis index: FTS5 (BM25) + numpy cosine, fused with RRF (spec 10 §3.5, §5.2).

Own file `data/index.db`, not a table in `lake.db`: the stub store is documented
as throwaway, the index is not (§3.5). Code is `09-raw/hybrid_recipe.py` with the
schema and the id mapping bolted on.
"""
import json
import re
import sqlite3
import threading
from pathlib import Path

import numpy as np

from .models import EMBED_DIM, INDEX_DB, Thesis

K_RRF = 60  # §8; Cormack et al. 2009 default (09-raw/hybrid_recipe.py:8)

# `thesis_fts` is a PLAIN fts5 table, not external-content: SQLite does not keep
# the external-content form in sync by itself, so the index would stay empty,
# MATCH would return nothing and the hybrid would silently degrade to cosine
# alone (§0.1.11). Rows are inserted explicitly, same rowid, same transaction.
DDL = (
    "CREATE TABLE IF NOT EXISTS idx_thesis(rowid INTEGER PRIMARY KEY,"
    " thesis_id TEXT UNIQUE, idea_id TEXT, text TEXT, vec BLOB)",
    "CREATE VIRTUAL TABLE IF NOT EXISTS thesis_fts"
    " USING fts5(text, tokenize='porter unicode61')",
)

# ponytail: one global lock over every connection and cache. /retrieve is
# threaded but read-only at ~2000 rows; per-db locks only if writes ever contend.
_LOCK = threading.RLock()
_CONNS: dict[str, sqlite3.Connection] = {}
_MATS: dict[str, tuple[int, list[int], np.ndarray]] = {}   # db -> (data_version, rowids, matrix)


def _con(db) -> sqlite3.Connection:
    """Connection cached per db path, opened on demand."""
    key = str(db)
    con = _CONNS.get(key)
    if con is None:
        Path(key).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: /retrieve serves from several threads, the
        # module lock above is what actually serializes access.
        con = sqlite3.connect(key, check_same_thread=False)
        with con:
            for stmt in DDL:
                con.execute(stmt)
        _CONNS[key] = con
    return con


def _matrix(db, con: sqlite3.Connection) -> tuple[list[int], np.ndarray]:
    """(rowids, (n, EMBED_DIM) matrix) kept in memory, ~3 MB at 2000 theses (§3.5).

    Keyed on `PRAGMA data_version`, which SQLite bumps when ANOTHER connection
    commits. Invalidating only on our own writes was silent staleness across
    processes: a long-running /retrieve server kept the matrix it built at
    startup while phase 2 wrote new theses, so the cosine arm ranked over a
    frozen corpus and only the BM25 arm ever saw the new rows — no error, no
    log line, just quietly worse recall.
    """
    key = str(db)
    version = con.execute("PRAGMA data_version").fetchone()[0]
    cached = _MATS.get(key)
    if cached is not None and cached[0] != version:
        cached = None
    if cached is None:
        rows = con.execute("SELECT rowid, vec FROM idx_thesis ORDER BY rowid").fetchall()
        if rows:
            mat = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32)
            mat = mat.reshape(len(rows), EMBED_DIM)
        else:
            mat = np.zeros((0, EMBED_DIM), dtype=np.float32)
        cached = (version, [r[0] for r in rows], mat)
        _MATS[key] = cached
    return cached[1], cached[2]


# ------------------------------------------------------------------ search arms

def fts_escape(query: str) -> str:
    """Free text -> safe FTS5 MATCH expression. Two silent problems (§5.2):

    1. FTS5 grammar: ':', '-', '"', '*', 'OR', 'NEAR' in a raw string either raise
       `fts5: syntax error` or quietly mean something else.
    2. Space-separated terms are implicitly ANDed. A rewritten 10-word query then
       matches nothing and the hybrid degrades to the cosine arm without a trace.

    Quoting each token fixes (1), joining with OR fixes (2); bm25() still ranks.
    """
    return " OR ".join('"%s"' % t for t in re.findall(r"\w+", query))


def bm25_search(con: sqlite3.Connection, query: str, top_k: int) -> list[int]:
    """Rowids best-first. bm25() is more negative = better match."""
    match = fts_escape(query)
    if not match:  # punctuation-only query: no BM25 arm, cosine answers alone (§5.2)
        return []
    rows = con.execute(
        "SELECT rowid FROM thesis_fts WHERE thesis_fts MATCH ? ORDER BY bm25(thesis_fts) LIMIT ?",
        (match, top_k),
    ).fetchall()
    return [r[0] for r in rows]


def cosine_search(mat: np.ndarray, query_vec: np.ndarray, top_k: int) -> list[int]:
    """Row positions in `mat` (L2-normalized rows), best-first."""
    if len(mat) == 0 or top_k <= 0:
        return []
    scores = mat @ query_vec
    top_k = min(top_k, len(scores))
    idx = np.argpartition(-scores, top_k - 1)[:top_k]
    return idx[np.argsort(-scores[idx])].tolist()


def rrf_fuse(ranked_lists: list[list[int]], k: int = K_RRF) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion over ranked lists of rowids. Best-first."""
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, rowid in enumerate(ranked, start=1):
            scores[rowid] = scores.get(rowid, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: -kv[1])


# ------------------------------------------------------------------- public api

def _check_vector(tag: str, vector) -> np.ndarray:
    """Shape and L2 norm, or raise. Cosine assumes normalized rows (§3.2), and a
    vector that is neither is a silently worse ranking, not an error anyone sees."""
    vec = np.asarray(vector, dtype=np.float32)
    if vec.shape != (EMBED_DIM,):
        raise ValueError(f"{tag}: vector shape {vec.shape}, expected ({EMBED_DIM},)")
    norm = float(np.linalg.norm(vec))
    if abs(norm - 1.0) > 1e-3:
        raise ValueError(f"{tag}: vector not L2-normalized, norm={norm:.4f}")
    return vec


def _insert(con: sqlite3.Connection, rows: list[tuple[str, str, str, list]]) -> int:
    """rows: (thesis_id, idea_id, text, vector). Returns the number actually written."""
    written = 0
    with con:  # one transaction: idx_thesis and thesis_fts must never diverge
        for thesis_id, idea_id, text, vector in rows:
            vec = _check_vector(thesis_id, vector)
            old = con.execute(
                "SELECT idea_id, text FROM idx_thesis WHERE thesis_id = ?", (thesis_id,)
            ).fetchone()
            if old is not None:
                if tuple(old) != (idea_id, text):
                    raise ValueError(
                        f"{thesis_id} is already indexed with a different idea_id/text: "
                        f"{tuple(old)!r} != {(idea_id, text)!r}"
                    )
                continue  # same batch replayed, §4.8 idempotency
            cur = con.execute(
                "INSERT INTO idx_thesis(thesis_id, idea_id, text, vec) VALUES (?, ?, ?, ?)",
                (thesis_id, idea_id, text, vec.tobytes()),
            )
            con.execute(
                "INSERT INTO thesis_fts(rowid, text) VALUES (?, ?)", (cur.lastrowid, text)
            )
            written += 1
    return written


def index_theses(theses: list[Thesis], db=INDEX_DB) -> None:
    """Called from phase 2 next to `write_theses` (§4.7), same batch."""
    with _LOCK:
        con = _con(db)
        _insert(con, [(t.id, t.idea_id, t.text, t.vector) for t in theses])
        _MATS.pop(str(db), None)  # invalidate the in-memory matrix


def search_theses(query: str, k: int, query_vec=None, db=INDEX_DB) -> list[dict]:
    """Hybrid BM25 + cosine, fused with RRF k=60 (§5.2).

    `query_vec` skips the embedding model — used by the self-check and by callers
    that already embedded the query.
    """
    if query_vec is None:
        from .embed import embed_query  # local: keeps sentence-transformers off import
        query_vec = embed_query(query)
    qv = np.asarray(query_vec, dtype=np.float32)
    with _LOCK:
        con = _con(db)
        rowids, mat = _matrix(db, con)
        bm25_ids = bm25_search(con, query, k)
        vec_ids = [rowids[i] for i in cosine_search(mat, qv, k)]
        bm25_pos = {rid: i for i, rid in enumerate(bm25_ids, 1)}
        vec_pos = {rid: i for i, rid in enumerate(vec_ids, 1)}
        out = []
        for rid, score in rrf_fuse([bm25_ids, vec_ids])[:k]:
            thesis_id, idea_id = con.execute(
                "SELECT thesis_id, idea_id FROM idx_thesis WHERE rowid = ?", (rid,)
            ).fetchone()
            out.append({
                "thesis_id": thesis_id,
                "idea_id": idea_id,
                "score": score,
                "bm25_rank": bm25_pos.get(rid),
                "vec_rank": vec_pos.get(rid),
            })
        return out


def stale_links(rows: list[dict], db=INDEX_DB) -> list[str]:
    """Thesis ids this index maps to a different idea than `rows` does.

    `has()` answers "is it indexed" and nothing else, so a leaf that MOVED between
    ideas is present and wrong and a presence check calls the index healthy. Moving a
    leaf is exactly what `ingest.split` does, and it commits the store and rebuilds the
    index as two steps — if the second one does not happen, every count-based check
    still passes (the row count did not change) while the arbiter keeps being offered a
    candidate whose leaves have left it. This is the only thing that sees that.

    `rows` is `graph_client.all_theses()`. Ids the index does not hold are `has()`'s
    business, not this one's.
    """
    with _LOCK:
        indexed = dict(_con(db).execute("SELECT thesis_id, idea_id FROM idx_thesis").fetchall())
    return [r["id"] for r in rows
            if r["id"] in indexed and indexed[r["id"]] != r["idea_id"]]


def has(thesis_id: str, db=INDEX_DB) -> bool:
    with _LOCK:
        return _con(db).execute(
            "SELECT 1 FROM idx_thesis WHERE thesis_id = ?", (thesis_id,)).fetchone() is not None


def reset(db=INDEX_DB) -> None:
    """Drop and recreate. The reconciliation path of §6.19 is `reset()` followed by
    `index_rows(graph_client.all_theses())`: the store is what carries `idea_id`,
    which phase 2 assigns and `staging.jsonl` therefore never holds."""
    with _LOCK:
        con = _con(db)
        with con:
            con.execute("DROP TABLE IF EXISTS idx_thesis")
            con.execute("DROP TABLE IF EXISTS thesis_fts")
            for stmt in DDL:
                con.execute(stmt)
        _MATS.pop(str(db), None)


def index_rows(rows: list[dict], db=INDEX_DB) -> int:
    """rows: {id, idea_id, text, vector} — the shape `graph_client.all_theses()` returns."""
    with _LOCK:
        con = _con(db)
        written = _insert(con, [(r["id"], r["idea_id"], r["text"], r["vector"]) for r in rows])
        _MATS.pop(str(db), None)
    return written


def _rebuild(con: sqlite3.Connection, rows: list[tuple[str, str, str, list]]) -> int:
    """Drop, recreate and refill in ONE transaction. Returns the rows written.

    Validating first and dropping second is not enough on its own: the drop used to
    commit before the inserts began, so anything that failed afterwards — a
    duplicate id, a full disk, a killed process — left the index EMPTY and rolled
    back only the inserts. An empty index is the worst possible failure here,
    because it does not raise: `/search` answers 200 with `[]` and ranking reads it
    as "the lake has nothing on this query" (§5.4). SQLite makes DDL transactional,
    so one transaction turns that into "the repair failed, the old index is still
    there" — which is what the caller was promised.
    """
    with con:
        con.execute("DROP TABLE IF EXISTS idx_thesis")
        con.execute("DROP TABLE IF EXISTS thesis_fts")
        for stmt in DDL:
            con.execute(stmt)
        for thesis_id, idea_id, text, vector in rows:
            vec = _check_vector(thesis_id, vector)
            cur = con.execute(
                "INSERT INTO idx_thesis(thesis_id, idea_id, text, vec) VALUES (?, ?, ?, ?)",
                (thesis_id, idea_id, text, vec.tobytes()),
            )
            con.execute("INSERT INTO thesis_fts(rowid, text) VALUES (?, ?)",
                        (cur.lastrowid, text))
    return len(rows)


def reconcile(rows: list[dict], db=INDEX_DB) -> int:
    """§6.19 repair: rebuild the index from the store, all or nothing.

    `rows` is `graph_client.all_theses()` — the store is what carries `idea_id`,
    which phase 2 assigns and `staging.jsonl` therefore never holds.

    Vectors are checked before the drop AND the whole thing is one transaction: the
    caller reaches for this exactly when the index is already suspect, so a repair
    that fails must leave what it was asked to fix intact rather than empty.
    """
    with _LOCK:
        con = _con(db)
        for row in rows:                        # loud refusal before anything moves
            _check_vector(row["id"], row["vector"])
        written = _rebuild(con, [(r["id"], r["idea_id"], r["text"], r["vector"]) for r in rows])
        _MATS.pop(str(db), None)
        return written


def rebuild_from(path: str, db=INDEX_DB) -> int:
    """Replay staging.jsonl from scratch (§3.5). Line format §4.7:
    {"source": {...}, "thesis": {...Thesis without vector...}, "draft": {...}, "vector": [...]}.
    """
    with _LOCK:
        # Parse and validate BEFORE dropping anything: a rebuild that refuses must
        # leave the index it was asked to repair intact. Emptying it first turns a
        # loud refusal into a destructive one — the caller reaches for this exactly
        # when the index is already suspect (§6.19).
        rows: list[tuple[str, str, str, list]] = []
        seen: set[str] = set()
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                rec = json.loads(line)
                thesis = rec["thesis"]
                if not thesis.get("idea_id"):
                    # Linking happens in phase 2; a staging line replayed without
                    # idea_id would index theses that point at no idea, and the
                    # read path would drop them silently.
                    raise ValueError(
                        f"{path}:{lineno}: thesis {thesis.get('id')!r} has no idea_id — "
                        "phase 2 must write the linked idea_id back into staging"
                    )
                _check_vector(f"{path}:{lineno}", rec["vector"])
                if thesis["id"] in seen:
                    raise ValueError(f"{path}:{lineno}: thesis {thesis['id']!r} appears twice")
                seen.add(thesis["id"])
                rows.append((thesis["id"], thesis["idea_id"], thesis["text"], rec["vector"]))

        con = _con(db)
        with con:
            con.execute("DROP TABLE IF EXISTS idx_thesis")
            con.execute("DROP TABLE IF EXISTS thesis_fts")
            for stmt in DDL:
                con.execute(stmt)
        _MATS.pop(str(db), None)
        written = _insert(con, rows)
        _MATS.pop(str(db), None)
        return written


def count(db=INDEX_DB) -> int:
    with _LOCK:
        return _con(db).execute("SELECT count(*) FROM idx_thesis").fetchone()[0]


# ------------------------------------------------------------------- self-check

def demo() -> None:
    """ponytail: single-run self-check (§6.12, §6.13), not a test suite."""
    import tempfile
    from .models import new_idea_id, new_thesis_id, text_hash

    texts = [
        "the quick brown fox jumps over the lazy dog",
        "a completely unrelated sentence about cats and mice",
        "quick foxes are quick and brown and fast animals",
        "graph databases store nodes and relationships",
        "vector embeddings capture semantic similarity of text",
    ]
    rng = np.random.default_rng(0)
    vecs = rng.standard_normal((len(texts), EMBED_DIM)).astype(np.float32)
    vecs[2] = vecs[0] + 0.01 * rng.standard_normal(EMBED_DIM)  # doc 2 ~ doc 0
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)

    idea_ids = [new_idea_id() for _ in range(3)]
    theses = [
        Thesis(id=new_thesis_id(), source_id="src0", idea_id=idea_ids[i % 3], text=t,
               context="ctx", effect="eff", locator="§1", text_hash=text_hash(t),
               vector=vecs[i].tolist(), created_at="2026-07-28T00:00:00Z")
        for i, t in enumerate(texts)
    ]

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "index.db"
        query_vec = vecs[0].copy()  # pretend the query embeds close to doc 0

        assert search_theses("anything", 5, query_vec=query_vec, db=db) == [], \
            "empty index must return [], not raise"

        index_theses(theses, db=db)
        index_theses(theses, db=db)  # §4.8: replaying the same batch is a no-op
        assert count(db=db) == 5, count(db=db)
        con = _con(db)
        assert con.execute("SELECT count(*) FROM thesis_fts").fetchone()[0] == 5, \
            "FTS rows must not duplicate on replay"

        # §6.12 BM25 alive: a token known to be in the fixture matches.
        assert bm25_search(con, "graph", 5), "empty FTS index -> hybrid silently = cosine"

        hits = search_theses("quick brown", 5, query_vec=query_vec, db=db)
        assert hits[0]["thesis_id"] in (theses[0].id, theses[2].id), hits
        by_id = {t.id: t.idea_id for t in theses}
        assert all(h["idea_id"] == by_id[h["thesis_id"]] for h in hits), hits
        assert all(h["bm25_rank"] or h["vec_rank"] for h in hits), hits
        assert all(hits[i]["score"] >= hits[i + 1]["score"] for i in range(len(hits) - 1)), \
            "RRF output must be sorted descending"

        # §6.13 a rewritten query is raw a syntax error, and space-joined an implicit AND.
        dirty = 'cheap proxy: pre-filter candidates -- "x" OR NEAR(y)'
        assert search_theses(dirty, 5, query_vec=query_vec, db=db), "escaped query must return hits"
        assert bm25_search(con, "::: --- ???", 5) == [], "punctuation-only -> [], not raise"
        punct = search_theses("::: --- ???", 5, query_vec=query_vec, db=db)
        assert punct and all(h["bm25_rank"] is None for h in punct), \
            "punctuation-only: cosine answers alone, no BM25 ranks"

        # rebuild_from: staging replay, §4.7 line shape.
        staging = Path(tmp) / "staging.jsonl"
        with open(staging, "w", encoding="utf-8") as fh:
            for t in theses:
                fh.write(json.dumps({
                    "source": {"id": t.source_id},
                    "thesis": {"id": t.id, "idea_id": t.idea_id, "text": t.text},
                    "draft": {"draft_text": t.text},
                    "vector": t.vector,
                }) + "\n")
        assert rebuild_from(str(staging), db=db) == 5
        assert count(db=db) == 5
        assert search_theses("graph databases", 5, query_vec=query_vec, db=db), \
            "index must be searchable right after a rebuild"

        _CONNS.pop(str(db)).close()

    print("index self-check OK:", hits[0])


if __name__ == "__main__":
    demo()
