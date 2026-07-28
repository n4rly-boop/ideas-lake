"""Read path, step 2: theses -> ideas -> the top-k answer and its log (spec 10 §5.3).

    top-50 theses
     -> thesis_id -> idea_id
     -> dedup by idea_id, an idea keeps the MAXIMUM score of its theses
     -> raw_score is kept as is and never touched again
     -> min-max over the FULL candidate list (before the cut to k)
     -> score = norm_score + 0.15 * trust_norm
     -> fewer than k ideas: neighbors(found, hops=1), via="edge"
     -> still fewer: top up from the cut-off tail, via="padding"

Recall-first (§5.5): there is no refusal, we pad up to k — but every element says
how it got here in `via`, and everything cut off lands in `cut_off` on the same
scale as what was returned. Fewer candidates in the lake than k is not an error:
the answer is simply shorter, and an empty lake is an empty list (data, §5.4),
not an exception. Storage failures are not caught here — api.py turns them into 503.
"""
from .. import graph_client
from .search import search


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


def _item(body: dict, score: float, via: str) -> dict:
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
        "via": via,
        "theses": [{"text": leaf["text"], "url": leaf["source_url"],
                    "title": leaf["source_title"], "effect": leaf["effect"],
                    "locator": leaf["locator"]} for leaf in body["theses"]],
    }


def rank(query: str, k: int = 5, *, query_vec=None) -> tuple[list[dict], dict]:
    """(ideas, log_payload). `log_payload` is §5.5 minus the fields rank cannot know
    — log_id, ts, query_raw/query_rewritten, rewrite_failed and cost are api.py's."""
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
    # and leave the 0.15 weight nothing to be calibrated against.
    scale = graph_client.trust_scale() if raw else 1.0

    lo = min(raw.values(), default=0.0)
    span = max(raw.values(), default=0.0) - lo
    scored: list[tuple[float, float, str, dict]] = []   # (score, raw_score, via, body)
    for idea_id, raw_score in raw.items():
        # min-max over the FULL candidate list, not over the returned k (§0.1.17):
        # normalizing per page gives the top element 1.0 whatever its absolute
        # quality, and makes cut_off incomparable between queries.
        # One candidate (or an exact tie) leaves nothing to spread: everyone gets
        # 1.0 and trust decides the order. `raw_score` is what carries the absolute
        # level to the log in that case.
        norm = 1.0 if span == 0 else (raw_score - lo) / span
        body = bodies[idea_id]
        scored.append((norm + 0.15 * (body["trust_score"] / scale),  # §8: weight 0.15
                       raw_score, "thesis", body))
    scored.sort(key=lambda c: -c[0])

    found = scored[:k]
    out = list(found)
    if out and len(out) < k:
        # `edge` is empty in the MVP, so neighbors returns [] and ranking degrades
        # to flat top-k — planned degradation (§3.4, `08:377`). An edge idea matched
        # no thesis: raw_score 0.0 and no normalized part, which keeps it on the
        # same scale as everything else instead of inventing a relevance for it.
        targets = [e["target_id"] for e in graph_client.neighbors([b["id"] for *_, b in found])
                   if e["target_id"] not in raw]
        targets = list(dict.fromkeys(targets))[:k - len(out)]
        edge_bodies = _bodies(targets)
        out += [(0.15 * (edge_bodies[t]["trust_score"] / scale), 0.0, "edge", edge_bodies[t])
                for t in targets]

    padded: list[tuple[float, float, str, dict]] = []
    if len(out) < k:
        # Unreachable while nothing thresholds the candidate list: the tail is empty
        # exactly when fewer than k candidates exist. Kept because the day a
        # relevance floor appears, recall-first must pad from below it (§5.5) and
        # the log has to say so.
        padded = [(s, r, "padding", b) for s, r, _, b in scored[len(found):][:k - len(out)]]
        out += padded

    ideas = [_item(body, score, via) for score, _, via, body in out]
    payload = {
        "k": k,
        "returned": [{"idea_id": body["id"], "score": score, "raw_score": raw_score,
                      "rank": i, "via": via}
                     for i, (score, raw_score, via, body) in enumerate(out, 1)],
        # Everything cut off, on the same scale as what was returned — this is the
        # "what would we lose at threshold X" curve (§5.5, `08:270`).
        "cut_off": [{"idea_id": body["id"], "score": score, "raw_score": raw_score,
                     "rank": len(out) + i}
                    for i, (score, raw_score, _, body) in
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
            idea = Idea(id=new_idea_id(), text=f"idea {name}", applicability_conditions="ac",
                        limitations="lim", failure_modes=[f"{name} fails"],
                        effect_claimed="+3 pp", effect_observed="",
                        vector=[0.1] * EMBED_DIM)
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
                                 "trust_score", "score", "via", "theses"}, sorted(item)

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

        # 4. normalization spans the FULL candidate list, cut-off tail included:
        # the global minimum gets norm 0 and the global maximum norm 1, so their
        # scores are exactly the trust term and 1.0 + the trust term.
        scale = graph_client.trust_scale()
        trust = {i["id"]: i["trust_score"] for i in
                 graph_client.get_ideas([e["idea_id"] for e in entries])}
        lowest = min(entries, key=lambda e: e["raw_score"])
        highest = max(entries, key=lambda e: e["raw_score"])
        assert abs(lowest["score"] - 0.15 * trust[lowest["idea_id"]] / scale) < 1e-12, lowest
        assert abs(highest["score"] - (1.0 + 0.15 * trust[highest["idea_id"]] / scale)) < 1e-12
        assert lowest in log["cut_off"], "the global minimum was returned, not cut off"
        assert abs(ideas[0]["score"] - 1.0) > 1e-6, "top score is 1.0: trust was not added"
        assert abs(scale - math.log(3)) < 1e-12, scale   # bulk: 2 distinct sources

        # 5. the cut-off tail is logged and continues the same ranking.
        assert log["cut_off"] and [c["rank"] for c in log["cut_off"]] == [4], log["cut_off"]
        assert log["k"] == k and set(log) == {"k", "returned", "cut_off"}

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
              f" trust={item['trust_score']:.3f} leaves={len(item['theses'])}")
    for entry in log["cut_off"]:
        print(f"  {entry['rank']}. cut_off      "
              f"          score={entry['score']:.4f} raw={entry['raw_score']:.5f}")


if __name__ == "__main__":
    demo()
