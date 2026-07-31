"""Read path, step 2: theses -> ideas -> the top-k answer and its log (spec 10 §5.3).

    top-50 theses
     -> thesis_id -> idea_id
     -> dedup by idea_id, an idea keeps the MAXIMUM score of its theses
     -> raw_score is kept as is and never touched again
     -> min-max over the FULL candidate list (before the cut to k)
     -> score = norm_score + TRUST_WEIGHT * trust_norm   (weight 0.0 today, see below)
     -> fewer than k ideas: neighbors(found, hops=1), via="edge"
     -> still fewer: top up from the cut-off tail, via="padding"

Recall-first (§5.5): there is no refusal, we pad up to k — but every element says
how it got here in `via`, and everything cut off lands in `cut_off` on the same
scale as what was returned. Fewer candidates in the lake than k is not an error:
the answer is simply shorter, and an empty lake is an empty list (data, §5.4),
not an exception. Storage failures are not caught here — api.py turns them into 503.

`cosine_similarity` (review finding, 2026-07-31): `score` is min-max over the
candidate list (§0.1.17), so its top element is 1.0 BY CONSTRUCTION whatever the
query — the sourdough query and a genuinely relevant one both come back with a
1.0, and the caller who has to decide "enough, or go to the web" (`13` §7) cannot
tell them apart from `score` or from `raw_score` either (README §8.1: RRF's raw
score barely moves with relevance, 0.0305 vs 0.0323 measured live). Every idea
already carries its own embedding, derived from its text (§1.3) the same way a
thesis leaf's vector is; the query embedding is computed once, here, and dotted
against it — an absolute cosine similarity that does not renormalize per request
and so IS comparable across calls. Measured live on the same two queries: 0.482
top (sourdough, nothing in the lake) vs 0.752 top (a query the lake has an answer
to) — the two ends of the scale the RRF number could not show. Necessary but not
free of its own ceiling: cosine similarity of a general-purpose sentence encoder
has a nonzero floor even between unrelated text (anisotropy), so "0" is not the
absence baseline — it is measured, not assumed, and the self-check pins the gap,
not an absolute threshold.
"""
import numpy as np

from .. import graph_client
from .search import search

# Weight of trust in the final score. 0.15 until 2026-07-31, then 0.0 by decision:
# the number is now produced by an LLM judge (`13` §3.3) and is being calibrated,
# so retrieval must not move under it yet — an ablation that changes two things at
# once measures neither. `trust_score` still travels in the answer (contract C3),
# it just buys no position. This is the whole integration: one constant, and the
# day the judge is trusted it goes back to 0.15 and is measured against the old
# ordering. Keeping the term (instead of deleting it) is deliberate — a weight of
# zero is visible in the log and in the code, a deleted branch is not.
TRUST_WEIGHT = 0.0


def _bodies(ids: list[str]) -> dict[str, dict]:
    """Idea bodies by id, leaves already joined to their source (§3.4) — no manual join.

    An id with no idea row means the thesis index and the store have diverged.
    Dropping it would turn a stale index into a quietly shorter answer, which is
    exactly the fail-open this project bans, so it raises: §6.19 reconciliation is
    `index.reset()` + `index.index_rows(graph_client.all_theses())`.
    """
    if not ids:
        return {}
    found = {idea["id"]: idea for idea in graph_client.get_ideas(ids)}
    missing = [i for i in ids if i not in found]
    if missing:
        raise ValueError(f"ideas referenced but absent from the store: {missing} — "
                         "index and graph diverged, reconcile per §6.19")
    return found


def _cosine(body: dict, query_vec) -> float:
    """Query · idea vector — both L2-normalized (`embed.py`), so this is exactly
    cosine similarity, and unlike `score`/`raw_score` it is not renormalized
    against whatever else this call happened to retrieve (module docstring)."""
    return float(np.dot(np.asarray(body["vector"], dtype=np.float32), query_vec))


def _item(body: dict, score: float, via: str, cosine_similarity: float) -> dict:
    """One element of the /retrieve answer, shape §5.4."""
    return {
        "idea_id": body["id"],
        "text": body["text"],
        "applicability_conditions": body["applicability_conditions"],
        "limitations": body["limitations"],
        "failure_modes": body["failure_modes"],
        "effect_claimed": body["effect_claimed"],
        "effect_observed": body["effect_observed"],
        "trust_score": body["trust_score"],
        "score": score,
        "cosine_similarity": cosine_similarity,
        "via": via,
        "theses": [{"text": leaf["text"], "url": leaf["source_url"],
                    "title": leaf["source_title"], "effect": leaf["effect"],
                    "locator": leaf["locator"]} for leaf in body["theses"]],
    }


def rank(query: str, k: int = 5, *, query_vec=None) -> tuple[list[dict], dict]:
    """(ideas, log_payload). `log_payload` is §5.5 minus the fields rank cannot know
    — log_id, ts, query_raw/query_rewritten, rewrite_failed and cost are api.py's."""
    if query_vec is None:
        # Lazy, matching `index.search_theses`'s own guard: `--mock` and the offline
        # self-checks must never load sentence-transformers. Embedding here (once)
        # instead of leaving it to `search` below means the vector is on hand for
        # `cosine_similarity` too — one encoder call, not two.
        from .. import embed
        query_vec = embed.embed_query(query)
    hits = search(query, query_vec, top_k=50)  # §8: 50 theses

    raw: dict[str, float] = {}
    for hit in hits:
        # MAX, not sum: summing lets an idea with 20 leaves beat a better idea with
        # two, on volume alone (§5.3, `08:243`).
        best = raw.get(hit["idea_id"])
        if best is None or hit["score"] > best:
            raw[hit["idea_id"]] = hit["score"]

    bodies = _bodies(list(raw))
    # Fixed scale declared by the storage side, never the returned page (§5.3): a
    # per-page trust_norm would hand 1.0 to whatever the query happened to return
    # and leave the weight nothing to be calibrated against. Since `13` §3.3 the
    # judge answers already normalized and the scale is a constant 1.0.
    scale = graph_client.trust_scale() if raw else 1.0

    lo = min(raw.values(), default=0.0)
    span = max(raw.values(), default=0.0) - lo
    # (score, raw_score, cosine_similarity, via, body)
    scored: list[tuple[float, float, float, str, dict]] = []
    for idea_id, raw_score in raw.items():
        # min-max over the FULL candidate list, not over the returned k (§0.1.17):
        # normalizing per page gives the top element 1.0 whatever its absolute
        # quality, and makes cut_off incomparable between queries.
        # One candidate (or an exact tie) leaves nothing to spread: everyone gets
        # 1.0 and trust decides the order. `raw_score` is what carries the absolute
        # level to the log in that case.
        norm = 1.0 if span == 0 else (raw_score - lo) / span
        body = bodies[idea_id]
        scored.append((norm + TRUST_WEIGHT * (body["trust_score"] / scale),
                       raw_score, _cosine(body, query_vec), "thesis", body))
    scored.sort(key=lambda c: -c[0])

    found = scored[:k]
    out = list(found)
    if out and len(out) < k:
        # `edge` is empty in the MVP, so neighbors returns [] and ranking degrades
        # to flat top-k — planned degradation (§3.4, `08:377`). An edge idea matched
        # no thesis: raw_score 0.0 and no normalized part, which keeps it on the
        # same scale as everything else instead of inventing a relevance for it.
        # `cosine_similarity` is unaffected either way — it reads the idea's own
        # vector, not the thesis hit that would otherwise carry it.
        targets = [e["target_id"] for e in graph_client.neighbors([b["id"] for *_, b in found])
                   if e["target_id"] not in raw]
        targets = list(dict.fromkeys(targets))[:k - len(out)]
        edge_bodies = _bodies(targets)
        out += [(TRUST_WEIGHT * (edge_bodies[t]["trust_score"] / scale), 0.0,
                 _cosine(edge_bodies[t], query_vec), "edge", edge_bodies[t]) for t in targets]

    padded: list[tuple[float, float, float, str, dict]] = []
    if len(out) < k:
        # Unreachable while nothing thresholds the candidate list: the tail is empty
        # exactly when fewer than k candidates exist. Kept because the day a
        # relevance floor appears, recall-first must pad from below it (§5.5) and
        # the log has to say so.
        padded = [(s, r, cos, "padding", b) for s, r, cos, _, b in scored[len(found):][:k - len(out)]]
        out += padded

    ideas = [_item(body, score, via, cos) for score, _, cos, via, body in out]
    payload = {
        "k": k,
        "returned": [{"idea_id": body["id"], "score": score, "raw_score": raw_score,
                      "cosine_similarity": cos, "rank": i, "via": via}
                     for i, (score, raw_score, cos, via, body) in enumerate(out, 1)],
        # Everything cut off, on the same scale as what was returned — this is the
        # "what would we lose at threshold X" curve (§5.5, `08:270`).
        "cut_off": [{"idea_id": body["id"], "score": score, "raw_score": raw_score,
                     "cosine_similarity": cos, "rank": len(out) + i}
                    for i, (score, raw_score, cos, _, body) in
                    enumerate(scored[len(found) + len(padded):], 1)],
    }
    return ideas, payload


# ------------------------------------------------------------------- self-check

def demo() -> None:
    """ponytail: single-run self-check (§6.4), offline — no network, no embedding model."""
    import math
    import tempfile
    from pathlib import Path

    import numpy as np

    from .. import index, stub_store
    from ..models import (EMBED_DIM, Idea, Source, Thesis, new_idea_id, new_thesis_id,
                          source_id as make_source_id, text_hash)

    # 8 theses on 4 ideas. `bulk` carries 4 of them on purpose: its theses are all
    # weaker than `sharp`'s two, so summing per idea would put it first and taking
    # the maximum must not.
    plan = [
        ("sharp", ["freeze the encoder and train the head only",
                   "the frozen encoder keeps the semantics"], 0.05),
        ("mid",   ["encoder capacity limits the head"], 0.55),
        ("bulk",  ["encoder notes one", "encoder notes two",
                   "encoder notes three", "encoder notes four"], 0.75),
        ("far",   ["graph databases store nodes and relationships"], 3.0),
    ]

    rng = np.random.default_rng(11)
    query_vec = rng.standard_normal(EMBED_DIM).astype(np.float32)
    query_vec /= np.linalg.norm(query_vec)

    with tempfile.TemporaryDirectory() as tmp:
        stub_store._db_path = Path(tmp) / "lake.db"
        idx_db = Path(tmp) / "index.db"
        empty_db = Path(tmp) / "empty.db"
        # rank() calls the module-level `search`; point it at the temp index so the
        # check needs neither data/ nor the embedding model.
        globals()["search"] = lambda q, qv, top_k=50, _db=idx_db: index.search_theses(
            q, top_k, query_vec=qv, db=_db)

        sources = []
        for i, url in enumerate(("https://arxiv.org/abs/2405.00001",
                                 "https://arxiv.org/abs/2405.00002")):
            sid = make_source_id(url, "v1")
            sources.append(sid)
            graph_client.write_source(Source(id=sid, url=url, title=f"Paper {i}", type="paper",
                                             version="v1", retrieved_at="2026-07-28T10:00:00Z"))

        ideas_by_name, all_theses = {}, []
        for name, texts, noise in plan:
            # The idea's own vector, same construction as its theses' (§1.3: derived
            # from text, and here standing in for "text close to the query at the
            # same noise level `plan` assigns the idea"). A constant placeholder
            # vector — every idea sharing one — would make `cosine_similarity`
            # identical for all four ideas and the checks below could not tell a
            # real computation from a stub returning any fixed number.
            idea_vec = query_vec + noise * rng.standard_normal(EMBED_DIM).astype(np.float32)
            idea_vec /= np.linalg.norm(idea_vec)
            idea = Idea(id=new_idea_id(), text=f"idea {name}", applicability_conditions="ac",
                        limitations="lim", failure_modes=[f"{name} fails"],
                        effect_claimed="+3 pp", effect_observed="",
                        vector=idea_vec.tolist())
            ideas_by_name[name] = idea
            leaves = []
            for text in texts:
                vec = query_vec + noise * rng.standard_normal(EMBED_DIM).astype(np.float32)
                vec /= np.linalg.norm(vec)
                leaves.append(Thesis(id=new_thesis_id(), source_id="", idea_id=idea.id,
                                     text=text, context="ctx", effect="+3.1 pp",
                                     locator="Table 4", text_hash=text_hash(text),
                                     vector=vec.tolist(), created_at="2026-07-28T10:00:00Z"))
            # `bulk` gets its last two leaves from a second source: distinct sources
            # are what the stub trust_score counts, so trust actually varies here.
            split = len(leaves) - 2 if name == "bulk" else len(leaves)
            graph_client.create_idea_with_theses(idea, sources[0], leaves[:split])
            if split < len(leaves):
                graph_client.create_idea_with_theses(None, sources[1], leaves[split:])
            all_theses += leaves

        index.index_theses(all_theses, db=idx_db)
        assert index.count(db=idx_db) == 8

        k = 3
        ideas, log = rank("freeze the encoder", k=k, query_vec=query_vec)

        # 1. exactly k, and every element says how it got here.
        assert len(ideas) == k, ideas
        assert [i["via"] for i in ideas] == ["thesis"] * k, ideas
        assert len(log["returned"]) == k and [r["rank"] for r in log["returned"]] == [1, 2, 3]
        for item in ideas:
            assert item["theses"] and all(t["url"].startswith("https://arxiv.org")
                                          and t["title"] and t["locator"] for t in item["theses"])
            assert set(item) == {"idea_id", "text", "applicability_conditions", "limitations",
                                 "failure_modes", "effect_claimed", "effect_observed",
                                 "trust_score", "score", "cosine_similarity", "via",
                                 "theses"}, sorted(item)
            assert -1.0 <= item["cosine_similarity"] <= 1.0, item["cosine_similarity"]

        # 2. dedup takes the maximum: 5 weak leaves must not outrank 2 strong ones.
        order = [i["idea_id"] for i in ideas]
        assert order[0] == ideas_by_name["sharp"].id, [i["text"] for i in ideas]
        summed = {}
        for hit in search("freeze the encoder", query_vec, 50):
            summed[hit["idea_id"]] = summed.get(hit["idea_id"], 0.0) + hit["score"]
        assert max(summed, key=summed.get) == ideas_by_name["bulk"].id, summed
        assert order.index(ideas_by_name["bulk"].id) > 0, "sum-like ranking: bulk won on volume"

        # 3. raw_score is the fused score, unnormalized, and differs from score.
        entries = log["returned"] + log["cut_off"]
        assert len(entries) == 4, log
        assert all(0.0 < e["raw_score"] < 0.1 for e in entries), entries   # RRF scale
        assert all(abs(e["score"] - e["raw_score"]) > 1e-6 for e in entries), entries

        # 3b. `ideas` (the actual /retrieve body) and `log["returned"]` (§5.5) must
        # report the SAME `cosine_similarity` for the same idea, and it must not be
        # `_item()` quietly relaying `score` — the served response is what a caller
        # reads, and a defect confined to `_item()` alone would pass every check
        # above, which only reads the log.
        by_id = {e["idea_id"]: e for e in log["returned"]}
        for item in ideas:
            logged = by_id[item["idea_id"]]
            assert abs(item["cosine_similarity"] - logged["cosine_similarity"]) < 1e-12, \
                (item, logged)
            assert abs(item["cosine_similarity"] - item["score"]) > 1e-9, \
                "cosine_similarity must not be a copy of score"

        # 4. normalization spans the FULL candidate list, cut-off tail included: the
        # global minimum gets norm 0 and the global maximum norm 1. With TRUST_WEIGHT
        # at 0.0 (`13` §3.3: the judge is not wired into ranking yet) those are the
        # whole score, so the two numbers are exactly 0.0 and 1.0.
        scale = graph_client.trust_scale()
        assert scale == 1.0, scale        # the judge answers normalized, `13` §3.3
        trust = {i["id"]: i["trust_score"] for i in
                 graph_client.get_ideas([e["idea_id"] for e in entries])}
        lowest = min(entries, key=lambda e: e["raw_score"])
        highest = max(entries, key=lambda e: e["raw_score"])
        assert abs(lowest["score"] - TRUST_WEIGHT * trust[lowest["idea_id"]] / scale) < 1e-12
        assert abs(highest["score"]
                   - (1.0 + TRUST_WEIGHT * trust[highest["idea_id"]] / scale)) < 1e-12
        assert lowest in log["cut_off"], "the global minimum was returned, not cut off"

        # 4b. the trust term is WIRED, it is only weighted zero. A deleted term and a
        # zero weight look identical from the outside, and the day the weight goes
        # back up the difference is the whole feature — so drive it once, here.
        graph_client.set_trust(ideas_by_name["sharp"].id, 0.9)
        saved_weight = globals()["TRUST_WEIGHT"]
        globals()["TRUST_WEIGHT"] = 0.5
        try:
            moved, _ = rank("freeze the encoder", k=k, query_vec=query_vec)
        finally:
            globals()["TRUST_WEIGHT"] = saved_weight
        sharp = next(i for i in moved if i["idea_id"] == ideas_by_name["sharp"].id)
        assert abs(sharp["score"] - (1.0 + 0.5 * 0.9)) < 1e-12, sharp["score"]
        flat, _ = rank("freeze the encoder", k=k, query_vec=query_vec)
        again = next(i for i in flat if i["idea_id"] == ideas_by_name["sharp"].id)
        assert abs(again["score"] - 1.0) < 1e-12, \
            "a stored trust_score must not move the score while TRUST_WEIGHT is 0"

        # 5. the cut-off tail is logged and continues the same ranking.
        assert log["cut_off"] and [c["rank"] for c in log["cut_off"]] == [4], log["cut_off"]
        assert log["k"] == k and set(log) == {"k", "returned", "cut_off"}

        # 5b. `cosine_similarity` is the fix for the 2026-07-31 finding: `score` is
        # 1.0 for the best of ANY candidate list by construction (§0.1.17), so it
        # cannot tell a real hit from the best of a bad set — this field must, and
        # the check that would go red if it stopped is: querying in a DIFFERENT
        # idea's terms has to move the number, not just the ranking.
        def _cos(entries: list[dict], name: str) -> float:
            return next(e["cosine_similarity"] for e in entries
                       if e["idea_id"] == ideas_by_name[name].id)

        entries = log["returned"] + log["cut_off"]
        sharp_cos, far_cos = _cos(entries, "sharp"), _cos(entries, "far")
        # "freeze the encoder" was built close to `sharp` and far from `far` (noise
        # 0.05 vs 3.0) — the gap here is the fixture analogue of the live measurement
        # (README §8.1 review note): 0.482 for a query the lake has nothing on vs
        # 0.752 for one it does.
        assert sharp_cos > far_cos + 0.3, (sharp_cos, far_cos)

        far_query_vec = np.asarray(ideas_by_name["far"].vector, dtype=np.float32)
        flipped, flipped_log = rank("graph databases store nodes and relationships",
                                    k=k, query_vec=far_query_vec)
        flipped_entries = flipped_log["returned"] + flipped_log["cut_off"]
        sharp_cos2, far_cos2 = _cos(flipped_entries, "sharp"), _cos(flipped_entries, "far")
        # The query flipped to `far`'s own terms: a `cosine_similarity` that is a
        # real per-request computation must flip with it. A field that silently
        # copied `score`, or one wired to a stale/constant vector, would leave
        # `sharp` on top here too — this is what "stops distinguishing" looks like.
        assert far_cos2 > sharp_cos2 + 0.3, (far_cos2, sharp_cos2)
        assert far_cos2 > far_cos, "cosine_similarity did not move with the query at all"

        # 6. fewer candidates than k is a shorter answer, not an error; and an empty
        # lake is an empty list, not an exception.
        few, few_log = rank("freeze the encoder", k=10, query_vec=query_vec)
        assert len(few) == 4 and few_log["cut_off"] == [], few_log
        globals()["search"] = lambda q, qv, top_k=50, _db=empty_db: index.search_theses(
            q, top_k, query_vec=qv, db=_db)
        assert rank("anything", k=k, query_vec=query_vec) == (
            [], {"k": k, "returned": [], "cut_off": []})

        # 7. a single candidate: min == max, and the span must not divide by zero.
        solo_db = Path(tmp) / "solo.db"
        solo = [t for t in all_theses if t.idea_id == ideas_by_name["mid"].id]
        index.index_theses(solo, db=solo_db)
        globals()["search"] = lambda q, qv, top_k=50, _db=solo_db: index.search_theses(
            q, top_k, query_vec=qv, db=_db)
        one, one_log = rank("encoder", k=k, query_vec=query_vec)
        assert len(one) == 1 and one_log["cut_off"] == []
        assert abs(one[0]["score"] - (1.0 + 0.15 * trust[one[0]["idea_id"]] / scale)) < 1e-12

        # 8. an indexed thesis whose idea is not in the store means index and graph
        # diverged (§6.19). It must raise, not shorten the answer silently.
        ghost = solo[0]
        index.index_rows([{"id": new_thesis_id(), "idea_id": "idea_ghost",
                           "text": ghost.text + " ghost", "vector": ghost.vector}], db=solo_db)
        try:
            rank("encoder", k=k, query_vec=query_vec)
        except ValueError as exc:
            assert "idea_ghost" in str(exc), exc
        else:
            raise AssertionError("a candidate missing from the store was dropped silently")

        for db in (idx_db, empty_db, solo_db):
            index._CONNS.pop(str(db)).close()
        stub_store._conn.close()
        stub_store._conn = None

    print("rank self-check OK")
    for item, entry in zip(ideas, log["returned"]):
        print(f"  {entry['rank']}. {item['text']:<12} via={item['via']:<8}"
              f" score={entry['score']:.4f} raw={entry['raw_score']:.5f}"
              f" cos={entry['cosine_similarity']:.4f}"
              f" trust={item['trust_score']:.3f} leaves={len(item['theses'])}")
    for entry in log["cut_off"]:
        print(f"  {entry['rank']}. cut_off      "
              f"          score={entry['score']:.4f} raw={entry['raw_score']:.5f}"
              f" cos={entry['cosine_similarity']:.4f}")


if __name__ == "__main__":
    demo()
