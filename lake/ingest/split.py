"""Split an idea that stopped being a mechanism and became a category (issue #2).

§4.6 re-derives an idea over all of its leaves, and nothing bounded how many leaves
that could be. On the 10-source run one idea held 92 leaves from 9 sources — 34% of
the lake — and its text had widened into the research area the corpus is about. That
is self-reinforcing rather than a one-off model error: a wider text is retrieved by
more theses, more leaves make the next re-derivation wider still, and 92 leaves are 92
draws at the top of a 30-hit candidate window (see `link._first_per_idea`, which prices
that draw advantage out). The arbiter prompt now refuses a candidate that names a family
rather than a lever, but neither of those repairs an idea that is ALREADY a category.
This module does.

The cut is spherical 2-means over the leaf vectors, applied recursively until every
part is at or under the ceiling. It is unsupervised on purpose: the thing being
recovered is which leaves talk about the same mechanism, and the leaf vectors are the
only evidence of that which does not cost an LLM call per pair.

Order matters and is the whole safety argument:

  1. cluster            — no writes, no calls
  2. re-derive EVERY part (§4.6) — LLM, still no writes
  3. one transaction    — parent updated, children inserted, leaves re-homed
  4. re-index           — the index carries `idea_id` too

A failure anywhere in 1-2 leaves the lake exactly as it was. Deriving after the move
instead would, on a failure, leave children carrying a verbatim copy of the over-broad
parent text — the defect this module exists to remove, now multiplied.
"""
import numpy as np

from .. import graph_client, index
from ..models import Idea, new_idea_id
from ..trace import trace
from . import rederive

# The ceiling, and it is a calibration knob, not a law. The 10-source run: max 92,
# second 12, median 2, and README §8.2 already called 14 leaves suspicious. 16 sits
# above every idea that looked healthy and far below the one that did not. A genuine
# mechanism replicated by 16+ papers exists and will be split once too often — that
# costs a duplicate the arbiter can re-merge, which is the cheaper of the two errors.
MAX_LEAVES = 16

# ponytail: the refinement loop is the one part of this module the self-check does not
# pin. On any fixture whose themes are separable enough to assert a expected cut, the
# seed pair alone already finds it, so `_KMEANS_ITERS = 0` stays green — proving the
# loop helps needs a labelled fixture harder than the code it is testing. What IS pinned:
# the cut is deterministic, never empty on either side, terminates, and recovers known
# themes. Raise this only with a case that shows the extra passes changing the answer.
_KMEANS_ITERS = 20      # converges in 3-4 at these sizes; the bound is a runaway guard

# A child is a NEW idea and its only evidence is its own leaves, so it is derived from
# an empty current idea rather than from a copy of the parent. Seeding it with the
# parent's text would hand the model back the very sentence that was too broad, and
# `prompts/rederive` tells it to keep the idea's identity — the two together are how
# every child comes back saying the same over-broad thing again.
_EMPTY_SEED = {"text": "", "applicability_conditions": "", "limitations": "",
               "failure_modes": [], "effect_claimed": "", "effect_observed": ""}


def leaf_counts() -> dict[str, int]:
    """Leaves per idea over the whole store. The distribution issue #2 is about —
    `phase2` reports its maximum, so a lake collapsing into one node is a number in
    the report and not something only a vault read by eye would show."""
    counts: dict[str, int] = {}
    for row in graph_client.all_theses():
        counts[row["idea_id"]] = counts.get(row["idea_id"], 0) + 1
    return counts


def due(max_leaves: int = MAX_LEAVES) -> list[str]:
    """Ids of the ideas that are over the ceiling, smallest id first (deterministic)."""
    return sorted(i for i, n in leaf_counts().items() if n > max_leaves)


@trace(component="ingest", op="split")
def split_idea(idea_id: str, max_leaves: int = MAX_LEAVES) -> dict:
    """Split one over-full idea. Returns {"idea_id", "leaves", "parts": [(id, n)]}.

    The parent keeps its id and the LARGEST part: edges and their accumulated weights
    hang off the id (`08:200`), so the id has to stay where most of the evidence went.
    Every part, parent included, is re-derived over exactly the leaves it ends up with,
    so no part is left describing leaves it no longer has.
    """
    leaves = graph_client.get_leaves(idea_id)
    if len(leaves) <= max_leaves:
        raise ValueError(f"split_idea: {idea_id} has {len(leaves)} leaves, "
                         f"ceiling is {max_leaves}")

    # `get_leaves` deliberately does not carry vectors (§3.5); `all_theses` does.
    vectors = {row["id"]: row["vector"] for row in graph_client.all_theses()
               if row["idea_id"] == idea_id}
    missing = [leaf["id"] for leaf in leaves if leaf["id"] not in vectors]
    if missing:
        # Fail-closed: clustering a subset would silently split on partial evidence.
        raise ValueError(f"split_idea: {len(missing)} leaves of {idea_id} have no "
                         f"vector, first is {missing[0]}")

    mat = np.asarray([vectors[leaf["id"]] for leaf in leaves], dtype=np.float32)
    parts = _clusters(mat, np.arange(len(leaves)), max_leaves)
    # No "fewer than 2 parts" guard: `_clusters` recurses whenever it is over the
    # ceiling and `_cut` never returns an empty side, so 2 is the floor by construction.
    # A guard no input can reach is not a guard — it is a line that reads as one, and
    # `neo4j_store.split_idea` refuses an empty child list anyway.
    #
    # Largest first; ties by first leaf, so the same store always splits the same way.
    parts.sort(key=lambda part: (-len(part), int(part[0])))

    # --- steps 1-2 done, nothing written yet ---------------------------------------
    idea = graph_client.get_ideas([idea_id])
    if not idea:
        raise KeyError(f"split_idea: idea {idea_id} is not in the graph")
    keep, rest = parts[0], parts[1:]
    parent_fields = rederive.derive(idea[0], [leaves[i] for i in keep])

    children: list[tuple[Idea, list[str]]] = []
    for part in rest:
        part_leaves = [leaves[i] for i in part]
        fields = rederive.derive(_EMPTY_SEED, part_leaves)
        if "vector" not in fields:
            # `derive` only embeds when `text` moved, and it moved off "" by
            # construction. No vector here means the model echoed the empty seed.
            raise ValueError(f"split_idea: a child of {idea_id} came back with no text")
        children.append((Idea(id=new_idea_id(), **fields),
                         [leaf["id"] for leaf in part_leaves]))

    # --- steps 3-4: the writes -----------------------------------------------------
    graph_client.split_idea(idea_id, parent_fields, children)
    # The index carries `idea_id` per leaf and half of it is now stale. Rebuilt whole
    # rather than patched: `index.index_theses` REFUSES a thesis whose indexed idea_id
    # changed (that guard is what catches drift), and a stale index here is not a worse
    # ranking, it is the arbiter being offered a candidate whose leaves have moved.
    # ponytail: O(corpus) per split; splits are rare. Narrow it if that stops being true.
    #
    # These two lines are NOT one transaction and cannot be: the store and the index are
    # separate files (§3.5). So the failure says which of the two happened — a caller
    # told only "the split failed" would report an idea that is in fact already split,
    # and the drift that IS the damage would go unmentioned. `run._reconcile_index`
    # looks for it by value on the next pass and rebuilds.
    try:
        index.reconcile(graph_client.all_theses())
    except Exception as exc:
        raise RuntimeError(
            f"split of {idea_id} is COMMITTED in the store, the index rebuild after it "
            f"failed ({type(exc).__name__}: {exc}): the leaves moved and the index still "
            "points at the idea they left. Repaired by the next phase2 pass, or now by "
            "POST /admin/reindex") from exc
    return {"idea_id": idea_id, "leaves": len(leaves),
            "parts": [(idea_id, len(keep))] + [(c.id, len(ids)) for c, ids in children]}


# ------------------------------------------------------------------- the clustering

def _clusters(mat: np.ndarray, rows: np.ndarray, max_leaves: int) -> list[np.ndarray]:
    """Recursively bisect `rows` until every part is at or under `max_leaves`.

    Terminates because `_bisect` never returns an empty side, so both halves are
    strictly smaller than what went in.
    """
    if len(rows) <= max_leaves:
        return [rows]
    mask = _bisect(mat[rows])
    return (_clusters(mat, rows[mask], max_leaves)
            + _clusters(mat, rows[~mask], max_leaves))


def _bisect(mat: np.ndarray) -> np.ndarray:
    """Spherical 2-means over unit rows. Boolean mask, both sides non-empty.

    Seeded with the two least similar leaves rather than at random: the split has to
    be reproducible, because the same store re-split differently on a replay would
    make `rederived_at_leaf_count` and the leaf sets disagree across a restart.
    """
    if len(mat) < 2:
        raise ValueError("_bisect needs at least 2 rows")
    sim = mat @ mat.T
    a, b = divmod(int(np.argmin(sim)), len(mat))
    ca, cb = mat[a], mat[b]
    mask = _cut(sim[a] - sim[b])
    for _ in range(_KMEANS_ITERS):
        ca, cb = _centroid(mat[mask]), _centroid(mat[~mask])
        moved = _cut(mat @ ca - mat @ cb)
        if np.array_equal(moved, mask):
            break
        mask = moved
    return mask


def _cut(delta: np.ndarray) -> np.ndarray:
    """`delta >= 0`, except that an all-or-nothing cut is replaced by a median one.

    Identical or near-identical leaves land on one side of every centroid pair, and an
    empty side would make `_clusters` recurse on the same rows forever.
    """
    mask = delta >= 0
    if mask.all() or not mask.any():
        mask = np.zeros(len(delta), dtype=bool)
        mask[np.argsort(-delta, kind="stable")[:len(delta) // 2]] = True
    return mask


def _centroid(rows: np.ndarray) -> np.ndarray:
    """Mean of unit rows, re-normalized. A zero mean (rows cancelling exactly) keeps
    the first row instead of dividing by zero and poisoning every later comparison."""
    mean = rows.mean(axis=0)
    norm = float(np.linalg.norm(mean))
    return rows[0] if norm < 1e-8 else mean / norm


# -------------------------------------------------------------------- self-check

def demo() -> int:
    """ponytail: single-run self-check, not a test suite. Offline: the encoder is a
    seeded fake and the re-derivation is scripted off the leaves in the prompt.

    Returns 1 on SKIPPED/REFUSED, 0 once every assertion below actually ran — a
    caller that only checks "did it raise" must not read a skipped demo as a
    pass (`lake/api/selfcheck.py:main` docstring has the full story).

    The fixture is deliberately asymmetric and holds TWO ideas. A symmetric one-idea
    fixture passed while "the parent keeps the largest part" was inverted, while the
    recursion in `_clusters` was deleted, and while the split clobbered every OTHER
    idea's leaves — three properties that only an uneven cut and a bystander idea can
    see. Sizes 14/12/8 also mean no single bisect can land both sides under the ceiling,
    so the recursive path is on the only road through.
    """
    import functools
    import os
    import sys
    import tempfile
    import types
    from pathlib import Path

    from .. import llm, neo4j_store, trace as trace_mod
    from ..models import (EMBED_DIM, REDERIVE_SCHEMA, Source, Thesis, new_thesis_id,
                          source_id as make_source_id, text_hash)

    THEMES = {1: 14, 2: 12, 3: 8, 4: 14}      # 4 is the bystander idea, under the ceiling
    assert max(THEMES[t] for t in (1, 2, 3)) < sum(THEMES[t] for t in (1, 2, 3)) - MAX_LEAVES, \
        "the fixture must force at least one recursive bisect"
    assert THEMES[4] <= MAX_LEAVES < THEMES[4] + 3, \
        "the bystander pins MAX_LEAVES from BELOW: it must sit just under the ceiling"

    def vec(text: str) -> list[float]:
        rng = np.random.default_rng(int(text_hash(text)[:8], 16))
        v = rng.standard_normal(EMBED_DIM).astype(np.float32)
        return (v / np.linalg.norm(v)).tolist()

    def themed(theme: int, n: int) -> list[float]:
        """Tight cluster per theme. The clustering reads vectors, so the fixture builds
        structure into them instead of borrowing the encoder's opinion of the text."""
        rng = np.random.default_rng(1000 * theme + n)
        base = np.random.default_rng(theme).standard_normal(EMBED_DIM).astype(np.float32)
        v = base + 0.25 * rng.standard_normal(EMBED_DIM).astype(np.float32)
        return (v / np.linalg.norm(v)).tolist()

    # `rederive.derive` imports `lake.embed` lazily; installed where that lookup lands,
    # so the check never loads sentence-transformers (`python3 -m lake.embed` is what
    # proves the real encoder).
    fake_embed = types.ModuleType("lake.embed")
    fake_embed.embed_docs = lambda texts: np.asarray([vec(t) for t in texts],
                                                     dtype=np.float32)
    package = sys.modules["lake"]
    had_embed = getattr(package, "embed", None)
    sys.modules["lake.embed"] = fake_embed
    package.embed = fake_embed

    seeds: list[str] = []        # the `text:` of the CURRENT idea handed to each derive
    fail_on: set[int] = set()    # 1-based derive calls that must raise
    blank_on: set[int] = set()   # 1-based derive calls that come back with no text

    def fake_complete(prompt, *, system, schema, op, max_tokens, timeout,
                      model=None, temperature=0.0):
        assert op == "rederive" and schema is REDERIVE_SCHEMA, (op, schema)
        assert system.startswith("You re-derive"), system[:40]
        seeds.append(next(ln[len("text: "):] for ln in prompt.splitlines()
                          if ln.startswith("text: ")))
        if len(seeds) in fail_on:
            raise llm.LLMError("server said no")
        # Answer off the leaves, so a part handed the wrong ones is visible.
        themes = sorted({ln.split()[-1] for ln in prompt.splitlines()
                         if ln.startswith("statement: leaf ")})
        return {"text": "" if len(seeds) in blank_on else "mechanism of " + "+".join(themes),
                "applicability_conditions": "ac", "limitations": "lim",
                "failure_modes": ["fm"], "effect_claimed": "+1 pp", "effect_observed": ""}

    old_complete, old_run_id = llm.complete, trace_mod.current_run_id()
    old_traces = trace_mod.TRACES_DIR
    old_reconcile, old_all_theses = index.reconcile, graph_client.all_theses
    llm.complete = fake_complete
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
        llm.complete = old_complete
        return 1
    if _existing:
        print(f"REFUSED: the graph is not empty ({_existing} node(s) total) — this "
              "self-check only ever runs against an empty scratch instance. Point "
              "NEO4J_URI at an empty instance and rerun.")
        llm.complete = old_complete
        return 1
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "index.db"
            # Every graph call is @trace'd into TRACES_DIR/<run_id>.jsonl, inside the real
            # data/ this check must not touch (the same move `vault.demo` makes).
            trace_mod.TRACES_DIR = Path(tmp) / "traces"
            trace_mod.set_run_id("selfcheck-split")
            # `split_idea` calls `index.reconcile` with no db argument — in production
            # that is the point, here it would rebuild the real data/index.db. Bound the
            # way selfcheck binds the other index calls, not by patching INDEX_DB: the
            # `db=INDEX_DB` defaults were captured when the module was imported.
            index.reconcile = functools.partial(old_reconcile, db=db)

            sid = make_source_id("https://arxiv.org/abs/2405.00001", "v1")
            graph_client.write_source(Source(id=sid, url="https://arxiv.org/abs/2405.00001",
                                             title="A Paper", type="paper", version="v1",
                                             retrieved_at="2026-07-28T10:00:00Z"))

            def make_idea(text: str, themes: list[int]) -> tuple[Idea, list[Thesis]]:
                idea = Idea(id=new_idea_id(), text=text, applicability_conditions="ac",
                            limitations="lim", failure_modes=["fm"],
                            effect_claimed="lots of numbers", effect_observed="",
                            vector=vec(text), rederived_at_leaf_count=0)
                leaves = [Thesis(id=new_thesis_id(), source_id=sid, idea_id=idea.id,
                                 text=f"leaf {theme}.{n} of theme {theme}", context="ctx",
                                 effect="+1 pp", locator="Table 1",
                                 text_hash=text_hash(f"leaf {theme}.{n} of theme {theme}"),
                                 vector=themed(theme, n),
                                 created_at="2026-07-28T10:00:00Z")
                          for theme in themes for n in range(THEMES[theme])]
                graph_client.create_idea_with_theses(idea, sid, leaves)
                index.index_theses(leaves, db=db)
                return idea, leaves

            # Theme order is 3,1,2 on purpose: the smallest part now owns leaf 0, so
            # "parent keeps the largest" and "parent keeps the first" disagree, and the
            # size rule (`08:200`: the id stays where the evidence went) is actually pinned.
            parent, big = make_idea("the whole research area", [3, 1, 2])
            bystander, small = make_idea("one honest mechanism", [4])
            assert (len(big), len(small)) == (34, 14), (len(big), len(small))

            # The live repro this closes: a judge cleared the idea BEFORE the split, same
            # as `13`'s "judged to 0.5/clean and splitting 2 of 3 leaves" (review of
            # 2026-07-31). Asserting dirty afterwards is only meaningful starting from
            # clean — `create_idea_with_theses` already leaves a fresh idea dirty, so a
            # split that did nothing to the flag would still read True by accident.
            graph_client.set_trust(parent.id, 0.5)
            graph_client.set_trust(bystander.id, 0.9)
            assert not graph_client.get_ideas([parent.id])[0]["dirty"], \
                "set_trust must leave the idea clean before the split proof below"

            def snapshot() -> dict:
                """Every thesis row in the store, every column."""
                return {row["id"]: graph_client.get_thesis(row["id"])
                        for row in old_all_theses()}

            before = snapshot()

            # --- the sweep reads the whole store, not one idea -------------------------
            assert leaf_counts() == {parent.id: 34, bystander.id: 14}, leaf_counts()
            assert due() == [parent.id], due()
            assert due(max_leaves=40) == [], "nothing is over a ceiling of 40"
            assert due(max_leaves=7) == sorted([parent.id, bystander.id]), \
                "due() must return EVERY idea over the ceiling, smallest id first"
            print("ok: due()/leaf_counts() read the whole store, sorted, complete")

            # --- the cut, as pure functions --------------------------------------------
            mat = np.asarray([themed(t, n) for t in (1, 2, 3) for n in range(THEMES[t])],
                             dtype=np.float32)
            mask_a, mask_b = _bisect(mat), _bisect(mat)
            assert np.array_equal(mask_a, mask_b), "_bisect is not deterministic"
            assert mask_a.any() and not mask_a.all(), "_bisect returned an empty side"
            # `_cut` on a delta that is entirely non-negative: the plain `>= 0` rule would
            # put everything on one side, `_clusters` would recurse on the same rows and
            # never terminate. Identical leaves are what produce that delta in the wild.
            degenerate = _cut(np.asarray([3.0, 2.0, 1.0, 0.5], dtype=np.float32))
            assert degenerate.any() and not degenerate.all(), degenerate
            assert list(degenerate) == [True, True, False, False], degenerate
            # All-negative delta, the mirror case: the larger delta is the one that stays.
            assert list(_cut(np.asarray([-3.0, -2.0], dtype=np.float32))) == [False, True]
            unit = np.asarray([[1.0, 0.0], [-1.0, 0.0]], dtype=np.float32)
            assert np.array_equal(_centroid(unit), unit[0]), "zero mean divided by zero"
            assert abs(np.linalg.norm(_centroid(mat[:5])) - 1.0) < 1e-5, "centroid not unit"
            print("ok: _bisect deterministic and never empty, _cut and _centroid degenerate")

            # --- fail-closed: nothing is written before every part is derived ----------
            graph_client.all_theses = lambda: [r for r in old_all_theses()
                                               if r["id"] != big[0].id]
            try:
                split_idea(parent.id)
            except ValueError as exc:
                assert "have no vector" in str(exc), exc
            else:
                raise AssertionError("a leaf missing from the vector source did not stop it")
            graph_client.all_theses = old_all_theses

            for label, control, value, says in (
                    ("an LLM failure", fail_on, 3, "server said no"),
                    ("a child with no text", blank_on, 2, "came back with no text")):
                control.add(value)
                seeds.clear()
                try:
                    split_idea(parent.id)
                except (llm.LLMError, ValueError) as exc:
                    # The message, not just the type: pydantic refuses a child with no
                    # vector too, so `except ValueError` alone stays green with the
                    # guard deleted and reports the wrong reason.
                    assert says in str(exc), f"{label}: refused with the wrong reason: {exc}"
                else:
                    raise AssertionError(f"{label} did not stop the split")
                control.discard(value)
                assert snapshot() == before, \
                    f"{label} happened mid-split and the store kept the writes"
                assert due() == [parent.id], "the idea stopped being due after a failed split"
            print("ok: derive fails -> zero writes, the store is byte-identical, still due")

            # --- the real split ---------------------------------------------------------
            seeds.clear()
            report = split_idea(parent.id)

            assert report["idea_id"] == parent.id and report["leaves"] == 34, report
            sizes = sorted((n for _, n in report["parts"]), reverse=True)
            assert sizes == [14, 12, 8], f"{sizes}: the cut did not recover 14/12/8"
            assert len(report["parts"]) == 3, "one bisect cannot land 34 under 16 — " \
                                              "the recursion in _clusters did not run"
            assert report["parts"][0] == (parent.id, 14), \
                f"{report['parts'][0]}: the parent must keep its id AND the largest part"
            print("ok: 34 leaves -> 14/12/8 by recursive bisect, parent keeps the largest")

            # Each part re-derived over exactly its own leaves, counter reset to match.
            for pid, n in report["parts"]:
                body = graph_client.get_ideas([pid])[0]
                assert body["rederived_at_leaf_count"] == n == len(body["theses"]), body
                themes = {t["text"].split()[-1] for t in body["theses"]}
                assert len(themes) == 1, f"part {pid} mixes themes {themes}"
                assert body["text"] == f"mechanism of {themes.pop()}", body["text"]
                assert np.allclose(body["vector"], vec(body["text"]), atol=1e-6), \
                    f"{pid}: text changed and the vector did not follow"
                # `13` review 2026-07-31: every leaf-set that just changed must come out
                # dirty — the parent lost 20 of its 34 leaves, each child owns leaves
                # NOTHING has ever judged, and a clean part is one the sweep never visits.
                assert body["dirty"] is True, \
                    f"{pid}: split left a changed leaf set clean, invisible to the sweep"
            assert graph_client.get_ideas([bystander.id])[0]["dirty"] is False, \
                "the untouched bystander must not have been marked dirty as a side effect"
            assert len(seeds) == 3, f"{len(seeds)} derive calls for 3 parts"
            assert seeds[0] == "the whole research area", seeds[0]
            assert seeds[1:] == ["", ""], \
                f"{seeds[1:]}: a child was seeded with the parent, not with _EMPTY_SEED"
            print("ok: parent derived from itself, every child from an empty seed")

            # §1.2: the split wrote `idea_id`, on the parent's leaves, and nothing else.
            after = snapshot()
            assert set(after) == set(before), "the split added or dropped a thesis row"
            moved = set()
            for tid, was in before.items():
                changed = {k for k in was if was[k] != after[tid][k]}
                assert changed <= {"idea_id"}, f"{tid}: split rewrote {sorted(changed)}"
                if changed:
                    moved.add(tid)
            assert moved and moved < {t.id for t in big}, moved
            for leaf in small:      # the bystander idea is not collateral
                assert after[leaf.id] == before[leaf.id], f"{leaf.id} of another idea changed"
            assert {t["id"] for t in graph_client.get_leaves(bystander.id)} == \
                {t.id for t in small}, "the split moved a leaf off another idea"
            assert graph_client.get_ideas([bystander.id])[0]["text"] == "one honest mechanism"
            print("ok: idea_id and no other column, and only on this idea's leaves (§1.2)")

            # The index moved with the store: a candidate must not point at a stale idea.
            indexed = {r["id"]: r["idea_id"] for r in graph_client.all_theses()}
            con = index._con(db)
            for tid, idea_id in con.execute("SELECT thesis_id, idea_id FROM idx_thesis"):
                assert indexed[tid] == idea_id, \
                    f"{tid}: index says {idea_id}, store says {indexed[tid]}"
            assert index.count(db=db) == 48, index.count(db=db)
            assert index.stale_links(graph_client.all_theses(), db=db) == []
            assert due() == [] and max(leaf_counts().values()) <= MAX_LEAVES, leaf_counts()
            print("ok: the index followed the split, 48 leaves, nothing over the ceiling")

            # --- the store guards --------------------------------------------------------
            child = report["parts"][1][0]
            child_leaves = [t["id"] for t in graph_client.get_leaves(child)]
            spare = Idea(id=new_idea_id(), text="x", applicability_conditions="a",
                         limitations="l", failure_modes=[], effect_claimed="",
                         effect_observed="", vector=vec("x"))
            # Each case is pinned to ITS OWN message. `except ValueError: pass` would be
            # satisfied by a guard replaced with an unrelated one, or by the UNIQUE
            # constraint firing on the inserted child before the guard is ever reached.
            for bad, why, says in (
                ((child, {}, []), "no children", "called with no children"),
                ((child, {}, [(spare, [])]), "a child with no leaves", "would have no leaves"),
                ((child, {}, [(spare, child_leaves)]), "moving every leaf off",
                 "would move every leaf off"),
                ((child, {}, [(spare, child_leaves[:1] * 2)]), "the same leaf twice",
                 "given to two children"),
                ((child, {}, [(spare, ["th_nope"])]), "a leaf that does not exist",
                 "are not leaves of"),
                # A leaf of a DIFFERENT real idea, not merely a missing id.
                ((child, {}, [(spare, [small[0].id])]), "a leaf of the bystander idea",
                 "are not leaves of"),
            ):
                try:
                    graph_client.split_idea(*bad)
                except ValueError as exc:
                    assert says in str(exc), f"{why}: refused with the wrong reason: {exc}"
                else:
                    raise AssertionError(f"split_idea accepted {why}")
            assert snapshot() == after, "a refused split still wrote something"
            assert graph_client.get_ideas([spare.id]) == [], "a refused split inserted the idea"
            print("ok: empty child, stolen leaf, duplicate leaf and a full move all refuse")
    finally:
        # Restored even when an assert fires: this runs as check 22 of a suite, and a
        # module global left pointing at a deleted temp directory would fail the NEXT
        # check instead — a green run that lies about which line broke.
        llm.complete = old_complete
        index.reconcile = old_reconcile
        graph_client.all_theses = old_all_theses
        trace_mod.TRACES_DIR = old_traces
        trace_mod.set_run_id(old_run_id)
        sys.modules.pop("lake.embed", None)
        if had_embed is None:
            del package.embed
        else:
            package.embed = had_embed
        for key in list(index._CONNS):          # the temp index.db handle, closed not leaked
            index._CONNS.pop(key).close()
        index._MATS.clear()
        # The graph was confirmed empty above, so wiping it outright on the way out
        # cannot touch anything this run did not itself write.
        with neo4j_store._session() as _s:
            _s.execute_write(lambda tx: tx.run("MATCH (n) DETACH DELETE n").consume())

    # The cleanup above is itself checked: check 22 is registered LAST, so a leaked
    # global has no later check to fail and would sit here unnoticed forever.
    assert llm.complete is old_complete and index.reconcile is old_reconcile
    assert graph_client.all_theses is old_all_theses
    assert trace_mod.TRACES_DIR == old_traces and trace_mod.current_run_id() == old_run_id
    assert "lake.embed" not in sys.modules and getattr(package, "embed", None) is had_embed
    print("split self-check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(demo())
