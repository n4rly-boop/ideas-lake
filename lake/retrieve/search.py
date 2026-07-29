"""Read path, step 1: top-50 theses for a query (spec 10 §5.2).

A thin layer over `index.search_theses`, which already owns the whole hybrid —
FTS5/BM25 + numpy cosine, RRF k=60, and the `fts_escape` fix (§0.1.18). Nothing
of that is re-implemented here.

`fuse="minmax"` is the second arm of the RRF-vs-min-max ablation (§5.2, planned
for Aug 1): normalize each arm, weights 0.5/0.5, mechanics of gigaevo-memory
`hybrid_strategy.py:12,107,134`. It lives here and not in `index.py` because the
ablation is A's alone and the index is not to be edited for it.
"""
from ..index import search_theses
from ..models import INDEX_DB


def _arm(rank: int | None, n: int) -> float:
    """One arm's rank -> [0,1], best rank = 1.0. Absent from the arm = the floor."""
    if rank is None or n <= 0:
        return 0.0
    return 1.0 if n <= 1 else (n - rank) / (n - 1)


def _minmax(hits: list[dict]) -> list[dict]:
    """Weighted min-max fusion of the two arms, 0.5/0.5 (§5.2).

    ponytail: min-max runs over the arm RANKS, not over the raw BM25/cosine
    values — `index.search_theses` returns `bm25_rank`/`vec_rank` and the fused
    score, the arm scores never leave it. Two ceilings follow, both named rather
    than hidden:
      1. inside an arm only the order survives, the distance between neighbours
         is flattened, and the worst-ranked document scores the same as one
         missing from that arm;
      2. the input is already RRF's top-k, so a document RRF dropped cannot come
         back — the two arms of the ablation do not see literally the same pool.
    Upgrade path: `search_theses` returning the raw arm scores and the untruncated
    union. That is an edit of §3.5's module, so it waits until the ablation needs it.
    """
    n_bm25 = max((h["bm25_rank"] for h in hits if h["bm25_rank"]), default=0)
    n_vec = max((h["vec_rank"] for h in hits if h["vec_rank"]), default=0)
    fused = [{**h, "score": 0.5 * _arm(h["bm25_rank"], n_bm25)
                          + 0.5 * _arm(h["vec_rank"], n_vec)} for h in hits]
    fused.sort(key=lambda h: -h["score"])
    return fused


def search(query: str, query_vec, top_k: int = 50, *,
           fuse: str = "rrf", db=INDEX_DB) -> list[dict]:
    """Top-50 theses (§8), best first: {thesis_id, idea_id, score, bm25_rank, vec_rank}.

    `query_vec=None` lets `index.search_theses` embed the query itself; passing a
    vector keeps the embedding model out of the call (self-checks, and callers
    that already embedded).
    """
    if fuse not in ("rrf", "minmax"):
        raise ValueError(f"unknown fusion {fuse!r}, expected 'rrf' or 'minmax'")
    hits = search_theses(query, top_k, query_vec=query_vec, db=db)
    return hits if fuse == "rrf" else _minmax(hits)


# ------------------------------------------------------------------- self-check

def demo() -> None:
    """ponytail: single-run self-check, offline — no network, no embedding model."""
    import tempfile
    from pathlib import Path

    import numpy as np

    from .. import index
    from ..models import EMBED_DIM, Thesis, new_idea_id, new_thesis_id, text_hash

    texts = [
        "freeze the encoder and train the head",       # both arms, top
        "the encoder is frozen during evolution",      # both arms
        "a note about encoder capacity",               # both arms, weaker
        "unrelated text about graph databases",        # vector arm only
    ]
    rng = np.random.default_rng(7)
    vecs = rng.standard_normal((len(texts), EMBED_DIM)).astype(np.float32)
    query_vec = vecs[0].copy()
    vecs[1] = vecs[0] + 0.30 * rng.standard_normal(EMBED_DIM).astype(np.float32)
    vecs[2] = vecs[0] + 0.60 * rng.standard_normal(EMBED_DIM).astype(np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    query_vec /= np.linalg.norm(query_vec)

    idea_ids = [new_idea_id(), new_idea_id()]
    theses = [
        Thesis(id=new_thesis_id(), source_id="src0", idea_id=idea_ids[i % 2], text=t,
               context="ctx", effect="+1 pp", locator="§1", text_hash=text_hash(t),
               vector=vecs[i].tolist(), created_at="2026-07-28T00:00:00Z")
        for i, t in enumerate(texts)
    ]

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "index.db"
        assert search("encoder", query_vec, db=db) == [], "empty index -> [], not a raise"

        index.index_theses(theses, db=db)
        rrf = search("freeze the encoder", query_vec, top_k=10, db=db)
        mm = search("freeze the encoder", query_vec, top_k=10, fuse="minmax", db=db)

        assert len(rrf) == len(mm) == 4, (rrf, mm)
        assert {h["thesis_id"] for h in rrf} == {h["thesis_id"] for h in mm}
        for out in (rrf, mm):
            assert all(out[i]["score"] >= out[i + 1]["score"] for i in range(len(out) - 1)), out
            assert all(set(h) == {"thesis_id", "idea_id", "score", "bm25_rank", "vec_rank"}
                       for h in out), out
        assert all(0.0 <= h["score"] <= 1.0 for h in mm), mm
        assert rrf[0]["thesis_id"] == mm[0]["thesis_id"] == theses[0].id, (rrf[0], mm[0])

        # The document only the cosine arm found must sit below the ones both arms
        # agreed on — that is the whole point of weighting the two arms.
        only_vec = [h for h in mm if h["bm25_rank"] is None]
        assert only_vec and mm.index(only_vec[0]) == len(mm) - 1, mm

        assert search("freeze the encoder", query_vec, top_k=2, db=db).__len__() == 2

        try:
            search("x", query_vec, fuse="wilson", db=db)
        except ValueError:
            pass
        else:
            raise AssertionError("unknown fusion silently fell back to RRF")

        index._CONNS.pop(str(db)).close()

    print("search self-check OK")
    print("  rrf   :", [(h["bm25_rank"], h["vec_rank"], round(h["score"], 5)) for h in rrf])
    print("  minmax:", [(h["bm25_rank"], h["vec_rank"], round(h["score"], 5)) for h in mm])


if __name__ == "__main__":
    demo()
