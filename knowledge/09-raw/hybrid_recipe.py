"""Minimal hybrid search: SQLite FTS5 (BM25) + numpy cosine + RRF.
Scale target: ~2000 short docs, 384-768d vectors. No extra deps beyond numpy.
"""
import re
import sqlite3
import numpy as np

K_RRF = 60  # Cormack et al. 2009 default, robust across benchmarks


def fts_escape(query: str) -> str:
    """Turn a free-text query into a safe FTS5 MATCH expression.

    Two separate problems, both silent:
    1. FTS5 has its own grammar. ':', '-', '"', '*', 'OR', 'NEAR' in a raw string
       either raise `fts5: syntax error` or quietly mean something else.
    2. Space-separated terms are implicitly ANDed. A 10-word rewritten query then
       matches only documents containing all 10 words -- i.e. nothing, and the
       hybrid silently degrades to the cosine arm alone.

    Quoting each token fixes (1); joining with OR fixes (2). bm25() still ranks,
    so recall goes up without the top of the list getting worse.
    """
    return " OR ".join('"%s"' % t for t in re.findall(r"\w+", query))


def build_db(docs: list[str]) -> sqlite3.Connection:
    """docs[i] is the thesis text for doc id i (0-based)."""
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE VIRTUAL TABLE fts USING fts5(content, tokenize='porter unicode61')"
    )
    con.executemany(
        "INSERT INTO fts(rowid, content) VALUES (?, ?)",
        [(i, d) for i, d in enumerate(docs)],
    )
    return con


def bm25_search(con: sqlite3.Connection, query: str, top_k: int) -> list[int]:
    """Returns doc ids ranked best-first. bm25() is more negative = better match."""
    match = fts_escape(query)
    if not match:  # query was all punctuation: no BM25 arm, cosine still answers
        return []
    rows = con.execute(
        "SELECT rowid FROM fts WHERE fts MATCH ? ORDER BY bm25(fts) LIMIT ?",
        (match, top_k),
    ).fetchall()
    return [r[0] for r in rows]


def cosine_search(mat: np.ndarray, query_vec: np.ndarray, top_k: int) -> list[int]:
    """mat: (n_docs, dim) L2-normalized rows. query_vec: (dim,) normalized."""
    scores = mat @ query_vec
    top_k = min(top_k, len(scores))
    idx = np.argpartition(-scores, top_k - 1)[:top_k]
    return idx[np.argsort(-scores[idx])].tolist()


def rrf_fuse(
    ranked_lists: list[list[int]], k: int = K_RRF
) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion over N ranked lists of doc ids (best-first).
    Returns (doc_id, fused_score) sorted best-first."""
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: -kv[1])


def hybrid_search(
    con: sqlite3.Connection,
    mat: np.ndarray,
    query: str,
    query_vec: np.ndarray,
    top_k: int = 50,
) -> list[tuple[int, float]]:
    bm25_ids = bm25_search(con, query, top_k)
    vec_ids = cosine_search(mat, query_vec, top_k)
    return rrf_fuse([bm25_ids, vec_ids])[:top_k]


def demo() -> None:
    """ponytail: single-run self-check, not a test suite."""
    docs = [
        "the quick brown fox jumps over the lazy dog",
        "a completely unrelated sentence about cats and mice",
        "quick foxes are quick and brown and fast animals",
        "graph databases store nodes and relationships",
        "vector embeddings capture semantic similarity of text",
    ]
    con = build_db(docs)

    rng = np.random.default_rng(0)
    mat = rng.standard_normal((len(docs), 8)).astype(np.float32)
    mat[2] = mat[0] + 0.01 * rng.standard_normal(8)  # doc 2 semantically ~ doc 0
    mat /= np.linalg.norm(mat, axis=1, keepdims=True)
    query_vec = mat[0].copy()  # pretend query embeds close to doc 0

    fused = hybrid_search(con, mat, "quick brown", query_vec, top_k=5)
    fused_ids = [doc_id for doc_id, _ in fused]

    assert fused_ids[0] in (0, 2), f"expected doc 0 or 2 on top, got {fused_ids}"
    assert set(fused_ids) <= set(range(len(docs)))
    assert all(fused[i][1] >= fused[i + 1][1] for i in range(len(fused) - 1)), \
        "fused scores must be sorted descending"

    bm25_only = bm25_search(con, "quick brown", 5)
    assert 0 in bm25_only and 2 in bm25_only, "BM25 should surface both fox docs"

    # A rewritten query from the LLM looks like this, and raw it is a syntax error.
    dirty = 'cheap proxy: pre-filter candidates -- "quick brown" OR NEAR(fox)'
    assert bm25_search(con, dirty, 5), "escaped query must run and match doc 0/2"
    assert bm25_search(con, "::: --- ???", 5) == [], "punctuation-only -> empty, not raise"

    print("demo OK:", fused)


if __name__ == "__main__":
    demo()

