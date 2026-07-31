"""2b-1: link a thesis to an existing idea, or open a new one (spec 10 §4.5).

The one irreversible step on the write path: a wrong link merges two techniques
into one idea, a wrong `new` piles up duplicates. Both are silent.

Cascade, exactly four steps (§4.5):

  [0] text_hash already seen under THIS source_id -> skip, zero LLM calls.
      In the store: idempotency (§4.8). In the batch overlay: the abstract repeated
      in Method gives two equal hashes inside one source, and UNIQUE(source_id,
      text_hash) (§1.2) would drop the whole batch — the article, not the thesis.
  [1] candidates: index.search_theses(k=30) UNION the batch overlay, deduped by
      idea_id -> top-10 distinct ideas. Candidates are gathered thesis<->thesis,
      never against Idea.vector (§4.5).
  [2] arbiter (35B, schema-forced): a candidate index or the -1 sentinel.
  [3] link | new; the decision and its vector enter the overlay immediately.

There is NO cosine threshold in this file (§0.5, §0.6). Cosine only gathers
candidates; "no duplicate here" is said by the arbiter with -1.

Fail-closed: an arbiter failure is NOT "new idea" (that is gigaevo-core's default
and it quietly accumulates duplicates, `08:198`). The row goes to
`data/pending_link.jsonl` whole and the thesis is not written. An answer outside
[-1, len(candidates)-1] is a failure too, not "take the first one". A failure on
one thesis does not block the other five of the same article (§4.5).

This module decides and writes nothing to the graph: `run.py` writes the returned
Thesis/Idea in one transaction (§3.4, §4.7).
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .. import graph_client, index, llm, trace
from ..models import (INDEX_DB, LINK_SCHEMA, PENDING_LINK, Idea, Thesis,
                      new_idea_id, new_thesis_id, text_hash)

# ponytail: linking is sequential for correctness — inside a source because thesis
# #2 may link to the idea thesis #1 just created, between sources because two
# parallel sources would open two ideas for one mechanism (§4.5). Batch the arbiter
# call the way mem0 does (main.py:920-932) if the ~25 minutes ever start to hurt.


def link_batch(source_id: str, rows: list[dict], *,
               index_db=INDEX_DB, pending_path=PENDING_LINK) -> list[dict]:
    """Decide one source's staging rows, in order. Returns one dict per row:

        {"thesis": Thesis | None, "idea": Idea | None, "skipped": bool, "reason": str}

    `idea` is filled only when the decision is "new" — that is the only case where
    `row["idea_fields"]` is used at all (§4.5). `thesis is None` means nothing is to
    be written for this row: either a duplicate hash (skipped) or an arbiter failure
    already recorded in `pending_path`.

    The overlay of decisions taken but not yet written lives here, in this call.
    A restart replays whole sources (the cursor moves per source, §4.7), so the part
    of the overlay that must survive a restart is step [0]'s hash set — and that one
    is rebuilt from the store by `_stored_hashes` below. The vectors do not need
    rebuilding: theses already written are already in the index, so step [1] sees
    them through the index arm.
    """
    for i, row in enumerate(rows):
        if row["source"]["id"] != source_id:
            raise ValueError(f"row {i} carries source {row['source']['id']!r}, "
                             f"batch is {source_id!r}")

    seen = {h: "duplicate: already stored under this source"
            for h in _stored_hashes(source_id, {r["thesis"]["text_hash"] for r in rows})}
    overlay: list[dict] = []          # {text_hash, vector, idea_id, thesis_id} in memory
    created: dict[str, Idea] = {}     # ideas opened in this batch, not yet in the store
    out: list[dict] = []

    for row in rows:
        th = row["thesis"]
        hashed = th["text_hash"]
        if hashed in seen:            # [0] — no candidates, no LLM call
            out.append({"thesis": None, "idea": None, "skipped": True, "reason": seen[hashed]})
            continue

        candidates: list[dict] = []
        try:
            if hashed != text_hash(th["text"]):
                raise ValueError(f"staging text_hash {hashed} does not match its own text")
            candidates = fts_candidates(th["text"], row["vector"], overlay, db=index_db)   # [1]
            idea_id, idea = _decide(row, candidates, created)                              # [2][3]
        except Exception as exc:
            # Fail-closed: the row is queued whole and NOT written. Granularity is per
            # thesis on purpose — the other five of this article still go through.
            _write_pending(pending_path, row, candidates, exc)
            out.append({"thesis": None, "idea": None, "skipped": True,
                        "reason": f"pending_link: {type(exc).__name__}: {exc}"})
            continue

        thesis = Thesis(id=new_thesis_id(), source_id=source_id, idea_id=idea_id,
                        text=th["text"], context=th["context"], effect=th["effect"],
                        locator=th["locator"], text_hash=hashed,
                        vector=list(row["vector"]), created_at=_now())
        # Overlay updated immediately (§4.5): the next thesis of this source must be
        # able to see this idea, which has no leaf in the index until run.py writes.
        overlay.append({"text_hash": hashed, "vector": row["vector"],
                        "idea_id": idea_id, "thesis_id": thesis.id})
        seen[hashed] = "duplicate: same text_hash earlier in this batch"
        if idea is not None:
            created[idea.id] = idea
        out.append({"thesis": thesis, "idea": idea, "skipped": False,
                    "reason": "new" if idea is not None else f"linked to {idea_id}"})
    return out


def fts_candidates(text: str, vector, overlay, k: int = 30, db=INDEX_DB) -> list[dict]:
    """Step [1]: index hits UNION overlay hits -> at most 10 distinct ideas.

    Returns [{"idea_id", "thesis_id", "score"}], best first — the same shape the
    `pending_link` record carries.

    Both arms are turned into ranked lists of ideas and fused with the RRF of the
    read path (`index.rrf_fuse`, k=60). That is the only way to put an RRF score and
    a raw cosine on one scale; taking a max over the two as they come would let the
    overlay's cosine (~0.9) outrank every stored candidate (~0.02) and a 30-thesis
    article would stop linking to the lake after its first few theses.

    Inside one arm, several leaves of one idea collapse to one entry (§4.5) — and the
    rank of that entry is normalized by how many of the arm's hits the idea took, see
    `_first_per_idea`: leaf count must buy a place in the draw, not a place at the top
    of it.
    """
    store = index.search_theses(text, k, query_vec=vector, db=db)
    ranked_store = _first_per_idea((h["idea_id"], h["thesis_id"]) for h in store)
    ranked_overlay = _first_per_idea(_overlay_hits(vector, overlay, k))

    best = {**dict(ranked_overlay), **dict(ranked_store)}   # thesis to show per idea
    fused = index.rrf_fuse([[i for i, _ in ranked_store], [i for i, _ in ranked_overlay]])
    return [{"idea_id": i, "thesis_id": best[i], "score": s} for i, s in fused[:10]]  # §8


# ------------------------------------------------------------------- the cascade

def _decide(row: dict, candidates: list[dict], created: dict[str, Idea]) -> tuple[str, Idea | None]:
    """Steps [2] and [3]. Returns (idea_id, Idea | None); the Idea is set only on "new"."""
    if not candidates:
        # Nothing to link to, so -1 is the only answer the arbiter could give: the
        # prompt states the list is the whole set of options. Not a threshold and not
        # a fallback — an empty lake would otherwise cost one call per thesis.
        link_to = -1
    else:
        answer = llm.complete(_prompt(row, _describe(candidates, created)),
                              system=llm.load_prompt("link"), schema=LINK_SCHEMA, op="link",
                              max_tokens=300, timeout=60.0,          # §8
                              model=llm.QWEN_35B, temperature=0.0)   # 35B: arbiter (§8)
        link_to = answer["link_to"]
        if isinstance(link_to, bool) or not isinstance(link_to, int):
            raise llm.LLMError(f"link: arbiter answered {link_to!r}, not an integer")
        if not -1 <= link_to < len(candidates):
            # Fail-closed: out of range is a failure, never "take the first one" (§4.5).
            raise llm.LLMError(f"link: arbiter answered {link_to}, outside "
                               f"[-1, {len(candidates) - 1}]")
    if link_to >= 0:
        return candidates[link_to]["idea_id"], None
    return _new_idea(row)


def _new_idea(row: dict) -> tuple[str, Idea]:
    """`row["idea_fields"]` is used here and nowhere else: it is the generalize()
    output, valid only when the arbiter said there is no duplicate (§4.5)."""
    from ..embed import embed_docs        # local: keeps sentence-transformers off import

    fields = row["idea_fields"]
    effect, source_type = row["thesis"]["effect"], row["source"]["type"]
    idea = Idea(
        id=new_idea_id(),                 # generated here: the next thesis of this batch
        text=fields["text"],              # may link to it before any store round-trip (§1.4)
        applicability_conditions=fields["applicability_conditions"],
        limitations=fields["limitations"],
        failure_modes=list(fields["failure_modes"]),
        # §1.3: claimed aggregates leaves whose source is a paper, observed those from
        # runs. With one leaf the aggregate is that leaf's effect. Never merged (06:200).
        effect_claimed=effect if source_type == "paper" else "",
        effect_observed=effect if source_type == "run" else "",
        vector=embed_docs([fields["text"]])[0].tolist(),   # idea vector is from idea text
        rederived_at_leaf_count=0,        # §4.6 trigger starts here, it is a field not a counter
    )
    # created_at/updated_at stay empty: timestamps on Idea are B's column (§1.4).
    return idea.id, idea


def _describe(candidates: list[dict], created: dict[str, Idea]) -> list[tuple[str, str]]:
    """(text, applicability_conditions) per candidate, in candidate order.

    Ideas opened earlier in this batch are not in the store yet, so they are read
    from `created`. An unresolvable candidate raises instead of being dropped:
    dropping would shift every index below it and the arbiter's answer would point
    at a different idea than the one it read.
    """
    missing = [c["idea_id"] for c in candidates if c["idea_id"] not in created]
    stored = {d["id"]: d for d in graph_client.get_ideas(missing)} if missing else {}
    out = []
    for cand in candidates:
        idea = created.get(cand["idea_id"])
        if idea is not None:
            out.append((idea.text, idea.applicability_conditions))
        elif cand["idea_id"] in stored:
            row = stored[cand["idea_id"]]
            out.append((row["text"], row["applicability_conditions"]))
        else:
            raise LookupError(f"candidate idea {cand['idea_id']} is in neither the batch "
                              "nor the store")
    return out


def _prompt(row: dict, described: list[tuple[str, str]]) -> str:
    """Body for `prompts/link/system.txt`: the thesis, then integer-numbered candidates
    (anti-hallucination numbering, mem0 `main.py:918` / graphiti, `09:150`)."""
    th, fields = row["thesis"], row["idea_fields"]
    lines = ["NEW THESIS",
             f"statement: {th['text']}",
             f"context: {th['context']}",
             f"effect: {th['effect'] or '(no number stated)'}",
             f"generalized draft: {fields['text']}",
             "",
             "CANDIDATE IDEAS"]
    for i, (text, applicability) in enumerate(described):
        lines.append(f"{i}. statement: {text}")
        lines.append(f"   applicability: {applicability}")
    return "\n".join(lines)


# ----------------------------------------------------------------------- helpers

def _first_per_idea(pairs) -> list[tuple[str, str]]:
    """(idea_id, thesis_id) best-first -> one entry per idea, ranked by best rank TIMES
    the number of hits the idea took in this arm.

    The plain "keep the best rank" version is what let one idea eat a third of the lake
    (issue #2): an idea with 92 leaves gets 92 draws inside a 30-hit window and an idea
    with one leaf gets one, so the big idea reached rank 1 on almost every thesis and the
    arbiter saw it almost every time. Best-of-n is not a similarity, it is a headcount.

    The correction is the rank the volume alone would have bought: an idea holding `n` of
    the window's slots lands at rank ~window/n by draw count, so multiplying its best rank
    by `n` prices that back out. `n` is counted in the returned window, not over the store,
    which is what makes it self-correcting — a big idea that is genuinely close still takes
    many slots and pays for them, and one that took a single slot on this query pays
    nothing. Ties keep their original order (the sort is stable).
    """
    best: dict[str, str] = {}
    rank: dict[str, int] = {}
    hits: dict[str, int] = {}
    for position, (idea_id, thesis_id) in enumerate(pairs, start=1):
        hits[idea_id] = hits.get(idea_id, 0) + 1
        if idea_id not in best:
            best[idea_id], rank[idea_id] = thesis_id, position
    return sorted(best.items(), key=lambda kv: rank[kv[0]] * hits[kv[0]])


def _overlay_hits(vector, overlay: list[dict], k: int) -> list[tuple[str, str]]:
    """Brute-force cosine over the overlay (hundreds of rows at most, §4.5)."""
    if not overlay:
        return []
    mat = np.asarray([e["vector"] for e in overlay], dtype=np.float32)
    order = index.cosine_search(mat, np.asarray(vector, dtype=np.float32), k)
    return [(overlay[i]["idea_id"], overlay[i]["thesis_id"]) for i in order]


def _stored_hashes(source_id: str, wanted: set[str]) -> set[str]:
    """Which of `wanted` this source already has as leaves — step [0] against the store.

    ponytail: `graph_client` has no `theses_of_source()`, and `all_theses()` carries
    neither `source_id` nor `text_hash`, so this scans it once and then confirms
    source membership through `get_leaves()`. Uniqueness is on the PAIR (§0.8): the
    same wording from another source must become a new leaf, so a global hash match
    is not enough. Collapses to one query the day B exposes the source-scoped read.
    """
    if not wanted:
        return set()
    ideas = {t["idea_id"] for t in graph_client.all_theses() if text_hash(t["text"]) in wanted}
    found = set()
    for idea_id in ideas:
        for leaf in graph_client.get_leaves(idea_id):
            if leaf["source_id"] == source_id and leaf["text_hash"] in wanted:
                found.add(leaf["text_hash"])
    return found


def _write_pending(path, row: dict, candidates: list[dict], exc: Exception) -> None:
    """One self-contained line (§4.5): a replay reruns it without re-parsing the paper."""
    record = {"ts": _now(), "run_id": trace.current_run_id(), "staging_line": row,
              "candidates": candidates, "error": f"{type(exc).__name__}: {exc}"}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# -------------------------------------------------------------------- self-check

if __name__ == "__main__":
    import os
    import sys
    import tempfile
    import types

    # The idea vector is the only embedding this step needs. Stubbed before the lazy
    # import in `_new_idea` binds, so the check stays offline and never loads torch;
    # `embed.py` proves the real encoder itself.
    _fake_embed = types.ModuleType("lake.embed")

    def _embed_docs(texts):
        vecs = np.stack([np.random.default_rng(int(text_hash(t)[:8], 16))
                         .standard_normal(384).astype(np.float32) for t in texts])
        return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)

    _fake_embed.embed_docs = _embed_docs
    sys.modules["lake.embed"] = _fake_embed

    from .. import neo4j_store
    from ..models import Source, source_id as make_source_id

    trace.set_run_id("selfcheck-link")

    answers: list = []          # scripted arbiter replies, consumed in order
    prompts: list[str] = []

    def fake_complete(prompt, *, system, schema, op, max_tokens, timeout,
                      model=llm.QWEN_9B, temperature=0.0):
        assert (schema, op) == (LINK_SCHEMA, "link"), (schema, op)
        assert (max_tokens, timeout) == (300, 60.0), (max_tokens, timeout)
        assert model is llm.QWEN_35B and temperature == 0.0, (model, temperature)
        assert "CANDIDATE IDEAS" in prompt and system.startswith("You are the arbiter")
        prompts.append(prompt)
        reply = answers.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {"link_to": reply(prompt) if callable(reply) else reply}

    llm.complete = fake_complete

    def pick(marker: str):
        """Arbiter that links to the candidate carrying `marker` — the candidate order
        depends on BM25 and on the fake vectors, the intent under test does not."""
        def choose(prompt: str) -> int:
            for line in prompt.splitlines():
                if line[:1].isdigit() and marker in line:
                    return int(line.split(".", 1)[0])
            raise AssertionError(f"candidate {marker!r} was never offered:\n{prompt}")
        return choose

    SID = make_source_id("https://arxiv.org/abs/2405.00001", "v1")
    SID2 = make_source_id("https://arxiv.org/abs/2405.00002", "v1")

    def row(text: str, idea_text: str, source: str = SID) -> dict:
        vec = _embed_docs([text])[0]
        return {"source": {"id": source, "url": "u", "title": "t", "type": "paper",
                           "version": "v1", "retrieved_at": _now(),
                           "run_success": None, "run_meta": None},
                "section_id": "S3.2",
                "thesis": {"text": text, "context": "CIFAR-10, ResNet-18", "effect": "+3.1 pp",
                           "locator": "Table 4", "text_hash": text_hash(text)},
                "draft": {"draft_text": idea_text, "draft_applicability": "a",
                          "draft_limitations": "l"},
                "idea_fields": {"text": idea_text, "applicability_conditions": "frozen encoder",
                                "limitations": "needs a pretrained encoder",
                                "failure_modes": ["encoder too weak -> semantics lost"]},
                "vector": vec.tolist()}

    # (g) issue #2: leaf count buys a place in the draw, not the top of it. `big` owns
    # four of the five hits and the best rank; `small` owns one hit at rank 2. Before
    # the normalization `big` came first on every thesis in the corpus and the arbiter
    # linked to it, which is how one idea reached 92 leaves and 34% of the lake.
    window = [("big", "tb1"), ("small", "ts1"), ("big", "tb2"), ("big", "tb3"), ("big", "tb4")]
    assert _first_per_idea(window) == [("small", "ts1"), ("big", "tb1")], _first_per_idea(window)
    # Same window without the volume: order follows rank, and the shown thesis is the
    # idea's best one, not its last.
    assert _first_per_idea(window[:2]) == [("big", "tb1"), ("small", "ts1")]
    # Equal adjusted rank keeps the better raw rank in front (the sort is stable).
    assert _first_per_idea([("a", "t1"), ("b", "t2"), ("a", "t3")]) == [("a", "t1"), ("b", "t2")]
    print("ok (g) issue #2: 4 hits of one idea do not outrank a single closer hit")

    # D11 removed the isolated store this check used to swap in (a fresh SQLite
    # file). Neo4j has no equivalent disposable target, so the fixture below is
    # written into whatever `NEO4J_URI` names for real — guarded the same way
    # `vault.demo`/`lake.api.selfcheck` are: the host must be local/scratch
    # (`neo4j_store._require_local_target`) and the graph confirmed empty first.
    # BLOCKER (review 2026-07-31): checked `os.environ.get("NEO4J_URI")` here and
    # unconditionally `DETACH DELETE`d below — one variable checked, a different
    # one (whatever the driver connected with, possibly set before an env change)
    # wiped. `_get_driver()` first, then the URI it actually snapshotted
    # (`neo4j_store._uri`), same as `lake.selfcheck._wipe_graph`.
    try:
        neo4j_store._get_driver()  # so `_uri` below reflects what the driver really used
        neo4j_store._require_local_target(neo4j_store._uri)
        with neo4j_store._session() as _s:
            _existing = _s.execute_read(
                lambda tx: tx.run("MATCH (n) RETURN count(n) AS c").single()["c"])
    except graph_client.STORE_ERRORS as exc:
        print(f"SKIPPED: no Neo4j reachable at {os.environ.get('NEO4J_URI')} "
              f"({type(exc).__name__}: {exc}). Bring one up with "
              "`docker compose up -d neo4j` and rerun.")
        raise SystemExit(1)
    if _existing:
        print(f"REFUSED: the graph is not empty ({_existing} node(s) total) — this "
              "self-check only ever runs against an empty scratch instance. Point "
              "NEO4J_URI at an empty instance and rerun.")
        raise SystemExit(1)

    with tempfile.TemporaryDirectory() as tmp:
        # Every graph call is @trace'd into TRACES_DIR/<run_id>.jsonl. Deleting that
        # file afterwards was not enough: it still CREATED data/traces/, and an
        # otherwise-absent data/ that exists but holds no file is what makes
        # `vault.demo`'s leak guard refuse to run ("the leak guard measured nothing").
        trace.TRACES_DIR = Path(tmp) / "traces"
        DB = Path(tmp) / "index.db"
        PENDING = Path(tmp) / "pending_link.jsonl"

        def link(sid, rows):
            return link_batch(sid, rows, index_db=DB, pending_path=PENDING)

        for sid, url in ((SID, "https://arxiv.org/abs/2405.00001"),
                         (SID2, "https://arxiv.org/abs/2405.00002")):
            graph_client.write_source(Source(id=sid, url=url, title="A Paper", type="paper",
                                             version="v1", retrieved_at=_now()))

        # (a) §6.15 batch overlay: two near-identical theses of one section -> ONE idea.
        rows_a = [row("freezing the encoder before finetuning keeps 3.1 pp of accuracy",
                      "freeze the pretrained encoder before finetuning"),
                  row("keeping the encoder frozen during finetuning preserves accuracy",
                      "freeze the pretrained encoder before finetuning")]
        answers[:] = [0]                       # the only candidate is the overlay entry
        res_a = link(SID, rows_a)
        assert len(prompts) == 1, "thesis 1 had no candidates: it must not cost an LLM call"
        assert res_a[0]["idea"] is not None and res_a[0]["reason"] == "new"
        assert res_a[1]["idea"] is None, "second thesis opened a second idea: overlay is dead"
        assert res_a[0]["thesis"].idea_id == res_a[1]["thesis"].idea_id
        assert "0. statement: freeze the pretrained encoder" in prompts[0]
        assert res_a[0]["idea"].rederived_at_leaf_count == 0
        assert res_a[0]["idea"].effect_claimed == "+3.1 pp" and not res_a[0]["idea"].effect_observed
        assert len(res_a[0]["idea"].vector) == 384
        print("ok (a) §6.15: two near-identical theses -> one idea, 1 arbiter call")

        # (b) §6.16 in-batch dedup: two equal text_hash in one source -> one leaf,
        # and the write of the batch does not blow up on UNIQUE(source_id, text_hash).
        dup = "mixed precision training halves memory at equal accuracy"
        rows_b = [row(dup, "train in mixed precision", SID2),
                  row("MIXED   Precision training halves memory at equal accuracy",
                      "train in mixed precision", SID2),
                  row("island model with periodic migration keeps population diversity",
                      "run isolated subpopulations and migrate between them", SID2)]
        assert rows_b[0]["thesis"]["text_hash"] == rows_b[1]["thesis"]["text_hash"]
        answers[:] = [-1]                      # thesis 3 is a different mechanism
        res_b = link(SID2, rows_b)
        assert res_b[1]["skipped"] and "earlier in this batch" in res_b[1]["reason"], res_b[1]
        assert res_b[0]["thesis"] is not None and res_b[2]["idea"] is not None
        written = [r for r in res_b if r["thesis"] is not None]
        assert len(written) == 2, written
        for r in written:                      # the real write, one transaction per idea (§3.4)
            graph_client.create_idea_with_theses(r["idea"], SID2, [r["thesis"]])
            index.index_theses([r["thesis"]], db=DB)
        assert index.count(db=DB) == 2
        print("ok (b) §6.16: equal text_hash -> one leaf, write_theses did not raise")

        # (c) §6.8 fail-closed: the arbiter blows up on thesis 2 of three.
        rows_c = [row("adaptive mutation rates raise search efficiency by 12%",
                      "adapt the mutation rate to observed progress", SID2),
                  row("adaptive mutation scheduling improves search efficiency",
                      "adapt the mutation rate to observed progress", SID2),
                  row("curriculum ordering of tasks speeds up convergence",
                      "order tasks from easy to hard", SID2)]
        answers[:] = [-1, llm.LLMError("connection reset by peer"), -1]
        res_c = link(SID2, rows_c)
        assert res_c[1]["thesis"] is None and res_c[1]["skipped"], res_c[1]
        assert res_c[1]["reason"].startswith("pending_link: LLMError"), res_c[1]["reason"]
        assert res_c[0]["thesis"] is not None and res_c[2]["thesis"] is not None, \
            "one failed thesis blocked the rest of the article"
        queued = [json.loads(ln) for ln in PENDING.read_text(encoding="utf-8").splitlines()]
        assert len(queued) == 1, queued
        assert queued[0]["staging_line"] == rows_c[1] and queued[0]["run_id"]
        assert queued[0]["candidates"] and set(queued[0]["candidates"][0]) == {
            "idea_id", "thesis_id", "score"}
        assert "connection reset" in queued[0]["error"]
        print("ok (c) §6.8: arbiter failure -> 0 writes, 1 pending_link line, batch survived")

        # (d) sentinel, link and out-of-range.
        rows_d = [row("tournament selection with size 4 beats roulette wheel",
                      "select parents by small tournaments", SID2),
                  row("selection by small tournaments outperforms fitness-proportional",
                      "select parents by small tournaments", SID2),
                  row("elitism preserves the best individual across generations",
                      "always carry the best individual over", SID2)]
        answers[:] = [-1, pick("select parents by small tournaments"), 7]
        res_d = link(SID2, rows_d)
        assert res_d[0]["idea"] is not None, "-1 must open a new idea"
        assert res_d[1]["idea"] is None and res_d[1]["thesis"].idea_id == res_d[0]["idea"].id
        assert res_d[2]["thesis"] is None and "outside" in res_d[2]["reason"], res_d[2]
        queued = [json.loads(ln) for ln in PENDING.read_text(encoding="utf-8").splitlines()]
        assert len(queued) == 2 and "outside [-1," in queued[1]["error"], queued[1]["error"]
        print("ok (d): -1 -> new, index -> link, out of range -> pending_link")

        # (e) §4.8 idempotency after a restart: the source is replayed whole, the hash
        # set is rebuilt from the store, zero LLM calls, zero writes.
        for r in res_a:
            graph_client.create_idea_with_theses(r["idea"], SID, [r["thesis"]])
            index.index_theses([r["thesis"]], db=DB)
        calls_before = len(prompts)
        answers[:] = []
        res_e = link(SID, rows_a)
        assert all(r["skipped"] and r["thesis"] is None for r in res_e), res_e
        assert all("already stored" in r["reason"] for r in res_e), res_e
        assert len(prompts) == calls_before, "a replayed source must cost no LLM call"
        print("ok (e) §4.8: replayed source -> 0 theses, 0 arbiter calls")

        # (f) §6.7 the pair, not the hash: the same wording from another source is a
        # NEW leaf, and the stored idea is offered as a candidate.
        answers[:] = [pick("freeze the pretrained encoder")] * 2
        res_f = link(SID2, [row(t["thesis"]["text"], t["idea_fields"]["text"], SID2)
                            for t in rows_a])
        assert all(not r["skipped"] for r in res_f), res_f
        assert {r["thesis"].idea_id for r in res_f} == {res_a[0]["thesis"].idea_id}, \
            "the candidate from the store was not offered or not linked"
        print("ok (f) §6.7: same text, other source -> new leaves on the stored idea")

        index._CONNS.pop(str(DB)).close()

    # The graph was confirmed empty above, so wiping it outright on the way out
    # cannot touch anything this run did not itself write.
    with neo4j_store._session() as _s:
        _s.execute_write(lambda tx: tx.run("MATCH (n) DETACH DELETE n").consume())
    print("link self-check OK")
