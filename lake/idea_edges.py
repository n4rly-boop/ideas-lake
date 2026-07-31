"""Backfill CLI for the Idea—Idea edges of the lake (`(:Idea)-[:RELATED {type}]->(:Idea)`).

**D12: A writes these edges itself, in the pipeline, not here.** Co-citation is
written per source right after that source's ideas are committed
(`ingest/run.py`, phase 2), `derived_from` is written at synthesis
(`idea_merger.write_hypothesis`). This file is what is left over: a one-off
recompute for data that reached the graph BEFORE D12 shipped and therefore never
went through either of those two write points.

- **co-citation** (default): re-runs `graph_client.write_cocitation_edges` over
  every Source currently in the lake. Idempotent by construction (the edge's
  `evidence` records which source ids have already contributed, `neo4j_store`'s
  docstring) — running this against a lake phase 2 already covered is a no-op,
  not a doubled weight, which is what makes "just in case, backfill again" safe.
- **`--derived-from`**: every `Idea.origin="synthesized"` node, parentage read off
  its synthetic leaf's `locator` (`synthesis/<id>+<id>`, `13` §6) the same way
  `idea_merger` reads it before the fresh write path existed, then
  `graph_client.write_derived_from_edges`.

**Goes through `graph_client`, not a driver of its own.** The original version of
this file opened `neo4j.GraphDatabase` directly and justified it by "A never
writes edges, so this cannot go through `graph_client`, which has no method for
one" (`13` §3.1). D12 gave `graph_client` exactly that method, and it is the one
the pipeline itself calls — a second Cypher implementation here would drift from
whatever the pipeline's ends up doing.
"""
import argparse
import os

DEFAULT_MIN_IDEAS = 2


def backfill_cocitation(min_ideas: int = DEFAULT_MIN_IDEAS,
                        dry_run: bool = False) -> list[dict]:
    """One `graph_client.write_cocitation_edges` call per Source in the lake.
    Returns every outcome, across every source, in source order."""
    from . import graph_client

    outcomes = []
    offset = 0
    page = 50
    while True:
        sources = graph_client.list_sources(limit=page, offset=offset)
        if not sources:
            break
        for src in sources:
            for outcome in graph_client.write_cocitation_edges(
                    src["id"], min_ideas=min_ideas, dry_run=dry_run):
                outcomes.append({"source_id": src["id"], **outcome})
        offset += page
    return outcomes


def parse_parents(locator: str) -> list[str]:
    """`synthesis/idea_x+idea_y` -> ['idea_x', 'idea_y'] (`13` §6, the leaf's locator is
    the hypothesis's only record of where it came from). An empty or malformed locator
    returns [] and the caller skips it — a hypothesis whose parentage cannot be read is
    reported, not guessed at.

    The prefix guard is load-bearing and easy to lose: without it `locator[10:]` still
    slices any string, so `pdf/page/12+34` would come back as the two "parents"
    `['ge/12', '34']` and this pass would try to write two edges to ids that do not
    exist. The demo pins that with a locator long enough for the slice to survive — a
    short one like `pdf/page/3` truncates to `''` and passes with the guard removed
    (review).
    """
    if not locator.startswith("synthesis/"):
        return []
    return [part for part in locator[len("synthesis/"):].split("+") if part]


def backfill_derived_from(dry_run: bool = False) -> list[dict]:
    """Every `origin="synthesized"` idea currently in the lake, parentage read off
    its synthetic leaf's `locator`. Skips (and reports) an idea whose parentage
    cannot be parsed, same as `idea_merger.write_hypothesis` does for a fresh one."""
    from . import graph_client

    outcomes = []
    offset = 0
    page = 50
    while True:
        ids = graph_client.list_idea_ids(limit=page, offset=offset)
        if not ids:
            break
        for idea in graph_client.get_ideas(ids):
            if idea["origin"] != "synthesized":
                continue
            # The SYNTHETIC leaf explicitly, not `theses[0]` (`13` review 2026-07-31):
            # `theses[0]` is "whichever leaf landed first, by `seq`" — true for every
            # hypothesis `write_hypothesis` has ever produced (exactly one leaf), but
            # not a guarantee this loop can lean on. The deleted pre-D12 Cypher picked
            # the leaf explicitly (`origin='synthesized'` on the idea, already checked
            # above, AND `locator STARTS WITH 'synthesis/'` on the leaf) — restored
            # here so a hypothesis that ever gained an out-of-order leaf still finds
            # its real parentage instead of silently reading someone else's locator.
            synthetic = next((t for t in idea["theses"]
                             if t["locator"].startswith("synthesis/")), None)
            locator = synthetic["locator"] if synthetic else ""
            parents = parse_parents(locator)
            if not parents:
                print(f"[skip] {idea['id']}: unparseable locator {locator!r}")
                outcomes.append({"idea_id": idea["id"], "parents": [], "missing": True})
                continue
            if len(parents) != 2:
                print(f"[partial] {idea['id']}: {len(parents)} parent(s) in "
                      f"{locator!r}, expected 2")
            for edge in graph_client.write_derived_from_edges(idea["id"], parents,
                                                               dry_run=dry_run):
                print(f"{idea['id']} -> {edge['idea_b_id']}: " +
                      (f"derived_from, weight = {edge['weight']}" +
                       (" [dry-run]" if dry_run else "")
                       if not edge["missing"] else "MISSING (idea not in the graph)"))
                outcomes.append({"idea_id": idea["id"], **edge})
        offset += page
    return outcomes


# ============================================================
# CLI
# ============================================================

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m lake.idea_edges",
        description="Backfill (:Idea)-[:RELATED]->(:Idea) edges for data that reached the "
                    "graph before D12 (the pipeline writes these itself now).")
    parser.add_argument("--min-ideas", type=int, default=DEFAULT_MIN_IDEAS,
                        help=f"minimum distinct ideas a source must touch before any "
                             f"co-citation pair is formed (default {DEFAULT_MIN_IDEAS}; "
                             f"NOT a per-idea thesis count, BLOCKER 2 review 2026-07-31)")
    parser.add_argument("--derived-from", action="store_true",
                        help="instead of co-citation: hypothesis -> its parents, read "
                             "off the synthetic leaf's locator (13 §6)")
    parser.add_argument("--dry-run", action="store_true",
                        help="find and print, write nothing")
    parser.add_argument("--self-check", action="store_true",
                        help="check against a live, empty, local Neo4j (paging logic "
                             "included, review 2026-07-31) plus the offline parse_parents "
                             "formula; 1 on SKIPPED/REFUSED, matching every other module")
    args = parser.parse_args(argv)

    if args.self_check:
        return demo()

    if not os.environ.get("NEO4J_URI"):
        raise SystemExit("NEO4J_URI is required (D11) — import lake.graph_client to check")

    if args.derived_from:
        outcomes = backfill_derived_from(dry_run=args.dry_run)
    else:
        outcomes = backfill_cocitation(min_ideas=args.min_ideas, dry_run=args.dry_run)
        print(f"found {len(outcomes)} co-citation pair(s)" + (" [dry-run]" if args.dry_run else ""))
        for o in outcomes:
            tag = "MISSING" if o["missing"] else f"weight = {o['weight']}"
            print(f"[source={o['source_id']}] {o['idea_a_id']} <-> {o['idea_b_id']}: {tag}")

    # A run where every write found no node used to exit 0 exactly like a clean one:
    # "found nothing" stays 0 — an empty result is a legitimate answer about a small
    # lake — but "tried to write and the node was absent" does not.
    missing = [o for o in outcomes if o.get("missing")]
    if missing:
        print(f"{len(missing)} write(s) matched no idea — nothing was written for them")
        return 1
    return 0


# ============================================================
# Self-check — parse_parents offline, backfill_cocitation/backfill_derived_from
# against a live local scratch Neo4j
# ============================================================

def demo() -> int:
    """`parse_parents` needs no server and runs first, unconditionally. Then, against
    a live local scratch Neo4j: `backfill_cocitation`/`backfill_derived_from`
    THEMSELVES, not just the `graph_client.write_*_edges` calls underneath them.

    Review, 2026-07-31: the print this replaced claimed the paging in this file
    ("exercised live by `ingest.run.selfcheck` (D12 assertions) and `idea_merger.demo`
    (`write_derived_from_edges`)") was covered elsewhere. False — neither of those two
    ever calls `backfill_cocitation`/`backfill_derived_from`; they exercise
    `graph_client.write_*_edges` directly. `lake/selfcheck.py`'s check 34 only asserts
    this module reached its own final line, so a claim printed here that nothing
    actually verifies was the green light producing itself (CLAUDE.md: a check that
    does not fail on broken code is worse than no check). Made true here instead of
    deleted, since the paging loop (offset stepping, page boundary) is exactly the
    kind of non-trivial logic CLAUDE.md wants a check for.

    Returns 1 on SKIPPED/REFUSED, 0 once every assertion below actually ran — the
    same contract as every other module self-check since D12 (`lake/vault.py:demo`
    is the reference shape this mirrors: local-only target, confirmed-empty graph,
    fixture wiped in a `finally`).
    """
    assert parse_parents("synthesis/idea_x+idea_y") == ["idea_x", "idea_y"]
    assert parse_parents("synthesis/idea_x") == ["idea_x"]
    assert parse_parents("pdf/page/12+34") == [], "a non-synthesis locator has no parents"
    assert parse_parents("synthesis/") == [], "an empty parent list is not a parent"
    print("ok: parse_parents reads synthesis/<id>+<id>, refuses anything else")

    from . import graph_client, neo4j_store
    from .models import Idea, Source, Thesis, new_idea_id, new_thesis_id
    from .models import source_id as make_source_id, text_hash

    # BLOCKER (review 2026-07-31): checked `os.environ.get("NEO4J_URI")` here and
    # unconditionally `DETACH DELETE`d below — one variable checked, a different
    # one (whatever the driver connected with, possibly set before an env change)
    # wiped. `_get_driver()` first, then the URI it actually snapshotted
    # (`neo4j_store._uri`), same as `lake.selfcheck._wipe_graph`.
    try:
        neo4j_store._get_driver()  # so `_uri` below reflects what the driver really used
        neo4j_store._require_local_target(neo4j_store._uri)
        with neo4j_store._session() as session:
            existing = session.execute_read(
                lambda tx: tx.run("MATCH (n) RETURN count(n) AS c").single()["c"])
    except graph_client.STORE_ERRORS as exc:
        print(f"SKIPPED: no Neo4j reachable at {os.environ.get('NEO4J_URI')} "
              f"({type(exc).__name__}: {exc}). Bring one up with "
              "`docker compose up -d neo4j` and rerun.")
        return 1
    if existing:
        print(f"REFUSED: the graph is not empty ({existing} node(s) total) — this "
              "self-check only ever runs against an empty scratch instance, never one "
              "that might hold data it did not create. Point NEO4J_URI at an empty "
              "instance and rerun.")
        return 1

    try:
        sid = make_source_id("https://arxiv.org/abs/idea-edges-demo", "v1")
        graph_client.write_source(Source(id=sid, url="https://arxiv.org/abs/idea-edges-demo",
                                         title="idea_edges demo", type="paper", version="v1",
                                         retrieved_at="2026-07-31T00:00:00Z"))
        idea_a = Idea(id=new_idea_id(), text="idea_edges demo a", applicability_conditions="ac",
                     limitations="lim", failure_modes=[], effect_claimed="",
                     effect_observed="", vector=[0.1] * 384)
        idea_b = Idea(id=new_idea_id(), text="idea_edges demo b", applicability_conditions="ac",
                     limitations="lim", failure_modes=[], effect_claimed="",
                     effect_observed="", vector=[0.2] * 384)

        def leaf(text: str, idea_id: str) -> Thesis:
            return Thesis(id=new_thesis_id(), source_id=sid, idea_id=idea_id, text=text,
                         context="c", effect="e", locator="l", text_hash=text_hash(text),
                         vector=[0.1] * 384, created_at="2026-07-31T00:00:00Z")

        leaf_a, leaf_b = leaf("demo leaf a", idea_a.id), leaf("demo leaf b", idea_b.id)
        graph_client.create_idea_with_theses(idea_a, sid, [leaf_a])
        graph_client.create_idea_with_theses(idea_b, sid, [leaf_b])

        # One source, ONE leaf per idea — exactly the shape BLOCKER 2 fixed: the old
        # per-idea threshold found nothing here; the fixed gate (source touches >= 2
        # distinct ideas) finds the pair.
        outcomes = backfill_cocitation()
        assert len(outcomes) == 1 and outcomes[0]["missing"] is False, outcomes
        assert graph_client.counts()["edges"] == 2, graph_client.counts()
        print("ok: backfill_cocitation pages over list_sources and finds the pair "
              "(1 leaf per idea, BLOCKER 2 semantics)")

        # Item 6 (review 2026-07-31): the synthetic leaf must be found by its
        # LOCATOR PREFIX, not by leaf position. A hypothesis whose synthesis leaf
        # landed SECOND (an out-of-order append `write_theses` allows and
        # `create_idea_with_theses` does not prevent) used to read `theses[0]` —
        # the decoy leaf below — and report the parentage unparseable. Two real
        # parent ideas, so `backfill_derived_from`'s edges can actually MATCH.
        parent_a = Idea(id=new_idea_id(), text="parent a", applicability_conditions="ac",
                       limitations="lim", failure_modes=[], effect_claimed="",
                       effect_observed="", vector=[0.3] * 384)
        parent_b = Idea(id=new_idea_id(), text="parent b", applicability_conditions="ac",
                       limitations="lim", failure_modes=[], effect_claimed="",
                       effect_observed="", vector=[0.4] * 384)
        graph_client.create_idea(parent_a)
        graph_client.create_idea(parent_b)
        hypo = Idea(id=new_idea_id(), text="a hypothesis", applicability_conditions="ac",
                   limitations="lim", failure_modes=[], effect_claimed="", effect_observed="",
                   vector=[0.5] * 384, origin="synthesized", trust_score=0.0)
        decoy = leaf("decoy: an ordinary leaf, not the synthesis one", hypo.id)
        decoy.locator = "not-synthesis/decoy"        # deliberately NOT the synthesis prefix
        real = leaf("real: the synthesis leaf", hypo.id)
        real.locator = f"synthesis/{parent_a.id}+{parent_b.id}"
        graph_client.create_idea_with_theses(hypo, sid, [decoy])   # decoy lands FIRST (lower seq)
        graph_client.write_theses(sid, [real])                     # synthesis leaf appended SECOND

        derived_outcomes = backfill_derived_from()
        assert len(derived_outcomes) == 2, derived_outcomes
        assert {o["idea_b_id"] for o in derived_outcomes} == {parent_a.id, parent_b.id}, \
            derived_outcomes
        assert all(o["missing"] is False for o in derived_outcomes), derived_outcomes
        print("ok: backfill_derived_from pages over list_idea_ids and finds the SYNTHESIS "
              "leaf by its locator prefix even when it is not theses[0] (item 6)")
    finally:
        # Unconditional full wipe, not id-scoped (`lake/vault.py:demo`'s own choice,
        # same reasoning): the emptiness gate above already confirmed nothing but this
        # fixture is in the graph, so `MATCH (n) DETACH DELETE n` cannot touch data it
        # did not write — and it is what also clears `_Seq` (`neo4j_store.py:132`'s
        # global ordering counter, bumped as a side effect of every `create_idea_with_
        # theses` call here), which an id-scoped delete would leave behind as a single
        # node the NEXT empty-graph gate would then see and REFUSE against.
        with neo4j_store._session() as session:
            session.execute_write(lambda tx: tx.run("MATCH (n) DETACH DELETE n").consume())

    print("idea_edges self-check OK — paging logic exercised live, not just claimed")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
