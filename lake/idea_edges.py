"""Write the Idea—Idea edges of the lake, against the Neo4j instance block B serves
(`knowledge/07-roles-and-contracts.md:70-81`). Two passes, one CLI:

- **co-citation** (default): two ideas that both leaf into the same Source
  (`(:Source)-[:YIELDS]->(:Thesis)<-[:HAS_LEAF]-(:Idea)`) are co-cited by it; every
  source with >= `min_theses` distinct co-cited ideas contributes one weight increment
  per unordered idea pair.
- **`--derived-from`**: a hypothesis (`Idea.origin="synthesized"`, `13` §5) to the two
  ideas it was synthesized out of. `idea_merger.py` does not write this edge — it goes
  through `graph_client`, which has no method for one, because A writes no Idea—Idea
  edges at all (`13` §3.1). The parentage reaches the graph on the synthetic leaf's
  `locator` (`synthesis/<id>+<id>`, `13` §6) and this pass turns it into an edge.

**Both passes are Neo4j-only, and that is a real gap, not a formality.** This file
speaks Cypher against a driver of its own; `idea_merger.py` writes through
`graph_client`, which under the default `LAKE_STORE=stub` puts the hypothesis in
SQLite. Run against a stub-backed lake there is nothing here to read and nothing
written: `stub_store`'s `edge` table — the one `stub_store.neighbors` reads
(`stub_store.py:41-42`) — stays empty, and a hypothesis keeps its parentage only on the
leaf's `locator`. Edges on the stub backend would need a writer that does not exist.

Still not a `graph_client` backend, but the reason changed on 2026-07-31 and the old
one is worth not repeating: it used to be "`graph_client` fronts `stub_store`, which
never held these ideas" — no longer true, `LAKE_STORE=neo4j` puts the real Bolt store
behind it (`graph_client.py:44-92`). The reason now is the contract: **A never writes
edges** (`13` §3.1, `07:16`, `06` §6) and `graph_client` therefore exposes no method
that could; `neighbors()` is read-only and that is the whole of A's side. This script
is block B's, run by hand against B's instance, and it stays out of `graph_client.py`
so it cannot be mistaken for part of the serving path.

**The relationship type is `:RELATED` and is not configurable.** Both readers of the
graph match exactly `(:Idea)-[:RELATED]->(:Idea)` — `neighbors()` (`neo4j_store.py:761`,
which feeds `via="edge"` top-up in `retrieve/rank.py:149`) and `counts()`
(`neo4j_store.py:545`, which is `/stats`'s `edges`). The first version of this file
wrote `RELATED_VIA_SOURCE` with a `--rel-type` flag: those edges land in the database,
`MATCH` finds them by hand, and every serving path reads straight past them — a full
graph that reports `edges: 0` and ranks as if it were empty. The kind of edge lives in
the `type` **property** instead, which is what both readers already return in their
rows, and the flag is gone rather than defaulted: a knob whose every non-default value
produces invisible writes is not a knob.

**Both directions are written for co-citation.** The stored edge is directed in both
backends (`stub_store.neighbors` filters `WHERE source_id IN (...)`,
`neo4j_store.neighbors` matches `-[r]->`), so a single edge is only ever traversable
from one end; co-citation is symmetric, and one row would make the traversal depend on
which of the two idea ids sorts first. `--derived-from` writes one direction on
purpose — child -> parent — because there the direction *is* the meaning.

Repeated runs accumulate the co-citation weight rather than overwrite it, so a source
seen again (a re-post through `upsert_source`, or a second load) keeps compounding it —
deduplicating that is `--dry-run`'s job, not this script's: run it once per source per
intended increment. `--derived-from` is idempotent: it sets the weight rather than
adding to it, since the parentage of a hypothesis cannot happen twice.

TODO (left as found, not this pass's job): `compute_weight_increment` is
`min(count_a, count_b)`, chosen as a placeholder. It rewards two ideas for how many
leaves EACH has under the source, not for how much the source specifically ties them
together, so an idea with many unrelated leaves under one source inflates every pair it
is in.
"""
import argparse
import os

DEFAULT_MIN_THESES = 2

# The relationship label both readers match on. Not a parameter — see the docstring.
REL_LABEL = "RELATED"
CO_CITED = "related_via_source"     # goes into the edge's `type` property
DERIVED_FROM = "derived_from"

_FIND_PAIRS = """
MATCH (s:Source)-[:YIELDS]->(t:Thesis)<-[:HAS_LEAF]-(i:Idea)
WITH s, i, count(DISTINCT t) AS thesis_count
WHERE thesis_count >= $min_theses
WITH s, collect({idea_id: i.id, count: thesis_count}) AS ideas
WHERE size(ideas) >= 2
UNWIND ideas AS a
UNWIND ideas AS b
WITH s, a, b
WHERE a.idea_id < b.idea_id
RETURN s.id AS source_id,
       a.idea_id AS idea_a_id, a.count AS count_a,
       b.idea_id AS idea_b_id, b.count AS count_b
"""

# The parentage of every hypothesis, read off the synthetic leaf `13` §6 puts it on.
# `origin` is checked as well as the locator prefix: either one alone would also match
# a hand-made node, and the two together are what `idea_merger.write_hypothesis` writes.
_FIND_DERIVED = """
MATCH (i:Idea)-[:HAS_LEAF]->(t:Thesis)
WHERE i.origin = 'synthesized' AND t.locator STARTS WITH 'synthesis/'
RETURN i.id AS child_id, t.locator AS locator
"""

# One statement for both passes. `MERGE` on the typed edge, then `SET` — the type has
# to be part of the MERGE pattern, or a second pass with a different `type` would match
# the first pass's edge and overwrite its kind.
_UPSERT = f"""
MATCH (a:Idea {{id: $idea_a_id}})
MATCH (b:Idea {{id: $idea_b_id}})
MERGE (a)-[r:{REL_LABEL} {{type: $type}}]->(b)
ON CREATE SET r.weight = 0
SET r.weight = CASE WHEN $accumulate THEN coalesce(r.weight, 0) + $increment
                    ELSE $increment END,
    r.note = $note,
    r.evidence = $evidence,
    r.updated_at = datetime()
RETURN r.weight AS new_weight
"""


def compute_weight_increment(count_a: int, count_b: int) -> float:
    """Weight added to one Idea-Idea edge for one shared Source. See the TODO above."""
    return min(count_a, count_b)


def parse_parents(locator: str) -> list[str]:
    """`synthesis/idea_x+idea_y` -> ['idea_x', 'idea_y'] (`13` §6, the leaf's locator is
    the hypothesis's only record of where it came from). An empty or malformed locator
    returns [] and the caller skips it — a hypothesis whose parentage cannot be read is
    reported, not guessed at.

    The prefix guard is load-bearing and easy to lose: without it `locator[10:]` still
    slices any string, so `pdf/page/12+34` would come back as the two "parents"
    `['ge/12', '34']` and this pass would MATCH two ids that do not exist. The demo
    pins that with a locator long enough for the slice to survive — a short one like
    `pdf/page/3` truncates to `''` and passes with the guard removed (review).
    """
    if not locator.startswith("synthesis/"):
        return []
    return [part for part in locator[len("synthesis/"):].split("+") if part]


def open_driver():
    from neo4j import GraphDatabase

    missing = [name for name in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD")
               if not os.environ.get(name)]
    if missing:
        raise SystemExit(f"not in the environment: {', '.join(missing)}")
    return GraphDatabase.driver(os.environ["NEO4J_URI"],
                                auth=(os.environ["NEO4J_USERNAME"],
                                      os.environ["NEO4J_PASSWORD"]))


def database_name() -> str:
    return os.environ.get("NEO4J_DATABASE", "neo4j")


def find_pairs(session, min_theses: int) -> list[dict]:
    return [dict(r) for r in session.run(_FIND_PAIRS, min_theses=min_theses)]


def find_derived(session) -> list[dict]:
    return [dict(r) for r in session.run(_FIND_DERIVED)]


def upsert_edge(tx, idea_a_id: str, idea_b_id: str, increment: float, *,
                type_: str, note: str, evidence: str, accumulate: bool) -> float | None:
    """One directed edge, inside a caller-supplied transaction. `None` means one of the
    two MATCHes found nothing — the edge was NOT written. Every caller has to tell that
    apart from a weight, because `None` is also what a dry run records."""
    rec = tx.run(_UPSERT, idea_a_id=idea_a_id, idea_b_id=idea_b_id,
                 increment=increment, type=type_, note=note, evidence=evidence,
                 accumulate=accumulate).single()
    return rec["new_weight"] if rec else None


def upsert_both(session, idea_a_id: str, idea_b_id: str, increment: float, *,
                type_: str, note: str, evidence: str) -> tuple[float | None, float | None]:
    """Both directions of one symmetric edge, in ONE managed transaction.

    Two autocommit `session.run` calls would not do: if the second raises, the first is
    already committed and the graph keeps a one-sided edge — and since `neighbors()`
    traverses `-[r]->` directionally, the traversal then depends on which end you enter
    from, which is exactly what writing both directions exists to prevent. Worse, the
    obvious repair (re-run) adds the increment to the surviving direction a second time
    and the two weights diverge for good. `execute_write` makes the pair atomic and
    gives it the driver's retry (review, finding 8).
    """
    def txn(tx):
        forward = upsert_edge(tx, idea_a_id, idea_b_id, increment, type_=type_,
                              note=note, evidence=evidence, accumulate=True)
        reverse = upsert_edge(tx, idea_b_id, idea_a_id, increment, type_=type_,
                              note=note, evidence=evidence, accumulate=True)
        return forward, reverse

    return session.execute_write(txn)


def process(session, min_theses: int = DEFAULT_MIN_THESES, dry_run: bool = False) -> list[dict]:
    """Co-citation pass over the whole graph. Returns what happened to each pair, so a
    caller (or the self-check) can assert on it instead of only reading stdout."""
    pairs = find_pairs(session, min_theses)
    print(f"found {len(pairs)} pair(s)" + (" [dry-run]" if dry_run else ""))

    outcomes = []
    for rec in pairs:
        increment = compute_weight_increment(rec["count_a"], rec["count_b"])
        if dry_run:
            print(f"[source={rec['source_id']}] {rec['idea_a_id']} (n={rec['count_a']}) <-> "
                  f"{rec['idea_b_id']} (n={rec['count_b']}): would add +{increment}")
            outcomes.append({**rec, "increment": increment, "new_weight": None})
            continue

        # Both directions, one transaction: the stored edge is directed and co-citation
        # is not (module docstring, `upsert_both`).
        forward, reverse = upsert_both(session, rec["idea_a_id"], rec["idea_b_id"],
                                       increment, type_=CO_CITED,
                                       note=f"co-cited by {rec['source_id']}",
                                       evidence=rec["source_id"])
        # `None` from either direction means a MATCH found no node — nothing was
        # written. Printing it raw would read as `weight = None` and land in the
        # outcome as the SAME value a dry-run row carries, so "wrote nothing" and
        # "was asked not to write" would be one state (review, finding 7).
        missing = forward is None or reverse is None
        if missing:
            print(f"[source={rec['source_id']}] {rec['idea_a_id']} <-> {rec['idea_b_id']}: "
                  f"MISSING (idea not in the graph: "
                  f"{'a' if forward is None else ''}{'b' if reverse is None else ''})")
        else:
            print(f"[source={rec['source_id']}] {rec['idea_a_id']} <-> {rec['idea_b_id']}: "
                  f"+{increment} -> weight = {forward}")
        outcomes.append({**rec, "increment": increment, "new_weight": forward,
                         "reverse_weight": reverse, "missing": missing})
    return outcomes


def process_derived(session, dry_run: bool = False) -> list[dict]:
    """`--derived-from` pass: hypothesis -> each parent it was synthesized out of.

    One direction only (child -> parent): there the direction is the meaning. Weight is
    set, not accumulated — a hypothesis is derived from its parents exactly once, and a
    second run of this pass over the same graph must not inflate it.
    """
    rows = find_derived(session)
    outcomes = []
    for rec in rows:
        parents = parse_parents(rec["locator"])
        if not parents:
            # Not silently skipped: a hypothesis whose parentage cannot be read is the
            # one thing this pass exists to record, and dropping it quietly would leave
            # a synthesized idea with no edge and no complaint.
            print(f"[skip] {rec['child_id']}: unparseable locator {rec['locator']!r}")
            outcomes.append({**rec, "parents": [], "new_weight": None, "missing": True})
            continue
        if len(parents) != 2:
            # `13` §6 puts exactly two parents on the locator. One is not "a smaller
            # merge", it is a parentage that lost a half somewhere — the edges below
            # are still true, so they are written, but a truncated provenance that
            # printed like a whole one would be indistinguishable from a correct run.
            print(f"[partial] {rec['child_id']}: {len(parents)} parent(s) in "
                  f"{rec['locator']!r}, expected 2")
        for parent_id in parents:
            if dry_run:
                print(f"{rec['child_id']} -> {parent_id}: would set derived_from [dry-run]")
                outcomes.append({**rec, "parent_id": parent_id, "new_weight": None,
                                 "missing": False})
                continue
            weight = session.execute_write(
                lambda tx, p=parent_id: upsert_edge(tx, rec["child_id"], p, 1.0,
                                                    type_=DERIVED_FROM, note="синтез",
                                                    evidence=rec["locator"],
                                                    accumulate=False))
            # None means one of the two MATCHes found nothing — a parent that has since
            # been deleted or split away. Named, not swallowed.
            print(f"{rec['child_id']} -> {parent_id}: " +
                  (f"derived_from, weight = {weight}" if weight is not None
                   else "MISSING (idea not in the graph)"))
            outcomes.append({**rec, "parent_id": parent_id, "new_weight": weight,
                             "missing": weight is None})
    print(f"{len(rows)} hypothesis/-es scanned" + (" [dry-run]" if dry_run else ""))
    return outcomes


# ============================================================
# CLI
# ============================================================

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m lake.idea_edges",
        description="Write (:Idea)-[:RELATED]->(:Idea) edges: co-citation by shared "
                    "source, or the parentage of synthesized hypotheses.")
    parser.add_argument("--min-theses", type=int, default=DEFAULT_MIN_THESES,
                        help=f"minimum co-cited theses per idea under one source "
                             f"(default {DEFAULT_MIN_THESES})")
    parser.add_argument("--derived-from", action="store_true",
                        help="instead of co-citation: hypothesis -> its parents, read "
                             "off the synthetic leaf's locator (13 §6)")
    parser.add_argument("--dry-run", action="store_true",
                        help="find and print, write nothing")
    parser.add_argument("--self-check", action="store_true",
                        help="offline check of the formulas and query shapes, no Neo4j")
    args = parser.parse_args(argv)

    if args.self_check:
        demo()
        return 0

    driver = open_driver()
    try:
        with driver.session(database=database_name()) as session:
            if args.derived_from:
                outcomes = process_derived(session, dry_run=args.dry_run)
            else:
                outcomes = process(session, min_theses=args.min_theses, dry_run=args.dry_run)
    finally:
        driver.close()

    # A run where every write found no node used to exit 0 exactly like a clean one:
    # the wrong `NEO4J_DATABASE` (`database_name()` defaults to "neo4j" and verifies
    # nothing) reads as an empty graph, and an operator wiring this into a script gets
    # green from a database that was never touched (review, finding 9). "Found
    # nothing" stays 0 — an empty co-citation result is a legitimate answer about a
    # small lake — but "tried to write and the node was absent" does not.
    missing = [o for o in outcomes if o.get("missing")]
    if missing:
        print(f"{len(missing)} write(s) matched no idea — nothing was written for them")
        return 1
    return 0


# ============================================================
# Self-check — the pure formula and the query shapes, no Neo4j
# ============================================================

class _FakeRecord(dict):
    def single(self):
        return self


class _NoRecord:
    """What the driver returns when a MATCH found nothing: a result whose `.single()`
    is None. Without this the missing-node branch of `upsert_edge` — and every
    `MISSING` print built on it — never runs in the self-check at all (review)."""

    def single(self):
        return None


class _FakeSession:
    """Scripted read results, recorded calls for `upsert_edge`.

    Dispatch is on the QUERY OBJECT, not on a substring of it: keying on text made the
    fake's routing depend on the very strings the checks were supposed to pin, so a
    mutation to `_FIND_DERIVED` broke the routing and looked "caught" for the wrong
    reason (review). Identity dispatch cannot do that.
    """

    def __init__(self, pairs: list[dict] | None = None, derived: list[dict] | None = None,
                 missing: bool = False):
        self._pairs = pairs or []
        self._derived = derived or []
        self._missing = missing
        self.upserts: list[dict] = []
        self.queries: list[str] = []
        self.transactions = 0

    def execute_write(self, fn):
        self.transactions += 1
        return fn(self)

    def run(self, query, **params):
        self.queries.append(query)
        if query is _FIND_PAIRS:
            return [dict(p) for p in self._pairs]
        if query is _FIND_DERIVED:
            return [dict(d) for d in self._derived]
        assert query is _UPSERT, f"unexpected query: {query!r}"
        self.upserts.append(dict(params))
        return _NoRecord() if self._missing else _FakeRecord({"new_weight": params["increment"]})


def demo() -> None:
    # (a) the formula itself: min() of the two leaf counts under the shared source.
    assert compute_weight_increment(3, 5) == 3
    assert compute_weight_increment(2, 2) == 2
    assert compute_weight_increment(0, 4) == 0
    print("ok (a): compute_weight_increment(a, b) == min(a, b)")

    # (b) the label both readers match on is in the write statement, and the kind of
    # edge is a property rather than a second label. This is the check that would have
    # caught `RELATED_VIA_SOURCE`: edges nobody reads (`neo4j_store.py:545,761`).
    #
    # The literal `RELATED` is spelled out on purpose. The first version asserted
    # `f"-[r:{REL_LABEL} ..." in _UPSERT`, and since `_UPSERT` is itself built from
    # `REL_LABEL`, both sides moved together: setting `REL_LABEL = "LINKSTO"` left the
    # self-check green while every serving read went blind (review, verified by
    # mutation). A guard interpolated from the thing it guards is not a guard.
    assert REL_LABEL == "RELATED", REL_LABEL
    assert "-[r:RELATED {type: $type}]->" in _UPSERT, _UPSERT
    assert "RELATED_VIA_SOURCE" not in _UPSERT
    # The parts of the statement no fake can execute, pinned as text — the fake returns
    # canned rows, so `MERGE`->`CREATE` (a duplicate edge every run), `MATCH`->`MERGE`
    # (inventing the missing idea instead of reporting it) and the loss of `coalesce`
    # (accumulation silently becoming assignment) were all invisible before (review).
    assert "MERGE (a)-[r:RELATED" in _UPSERT, "MERGE, not CREATE: re-runs must not duplicate"
    assert _UPSERT.count("MATCH (") == 2, "both endpoints MATCH; MERGE would invent them"
    assert "coalesce(r.weight, 0) + $increment" in _UPSERT, "accumulation must accumulate"
    print("ok (b): writes (:Idea)-[:RELATED {type: ...}]->(:Idea), the shape neighbors() reads")

    pairs = [{"source_id": "s1", "idea_a_id": "idea_a", "count_a": 3,
              "idea_b_id": "idea_b", "count_b": 2}]

    # (c) dry-run finds pairs and writes nothing.
    session = _FakeSession(pairs)
    outcomes = process(session, dry_run=True)
    assert len(outcomes) == 1 and outcomes[0]["new_weight"] is None
    assert session.upserts == [], "dry-run must not touch the graph"
    print("ok (c): --dry-run finds pairs, upserts nothing")

    # (d) a real pass computes the increment and writes BOTH directions, accumulating,
    # inside ONE transaction — a second commit for the reverse direction would leave a
    # one-sided edge that no re-run can repair without double-counting the survivor.
    session = _FakeSession(pairs)
    outcomes = process(session, dry_run=False)
    assert outcomes[0]["increment"] == 2, outcomes
    assert len(session.upserts) == 2, session.upserts
    assert session.transactions == 1, "both directions must share one transaction"
    forward, back = session.upserts
    assert (forward["idea_a_id"], forward["idea_b_id"]) == ("idea_a", "idea_b")
    assert (back["idea_a_id"], back["idea_b_id"]) == ("idea_b", "idea_a")
    for call in session.upserts:
        assert call["type"] == CO_CITED and call["increment"] == 2
        assert call["accumulate"] is True, "co-citation weight accumulates across runs"
        assert call["evidence"] == "s1"
    assert outcomes[0]["missing"] is False, outcomes
    print("ok (d): both directions, one transaction, type=related_via_source, accumulating")

    # (d2) a MATCH that finds nothing is NOT a weight: `None` must not read as success
    # and must not be the same recorded value a dry run leaves behind.
    session = _FakeSession(pairs, missing=True)
    outcomes = process(session, dry_run=False)
    assert outcomes[0]["missing"] is True, outcomes
    assert outcomes[0]["new_weight"] is None and outcomes[0]["reverse_weight"] is None
    print("ok (d2): a missing idea is reported as MISSING, not as a written weight")

    # (e) locator parsing, including every way it can be unusable. The non-synthesis
    # case uses a locator LONG enough that dropping the prefix guard changes the
    # answer: `pdf/page/3` slices to `''` either way and pins nothing (review).
    assert parse_parents("synthesis/idea_x+idea_y") == ["idea_x", "idea_y"]
    assert parse_parents("synthesis/idea_x") == ["idea_x"]
    assert parse_parents("pdf/page/12+34") == [], "a non-synthesis locator has no parents"
    assert parse_parents("synthesis/") == [], "an empty parent list is not a parent"
    print("ok (e): parse_parents reads synthesis/<id>+<id>, refuses anything else")

    # (f) the derived-from pass: one edge per parent, child -> parent, weight SET.
    derived = [{"child_id": "idea_new", "locator": "synthesis/idea_a+idea_b"}]
    session = _FakeSession(derived=derived)
    outcomes = process_derived(session)
    assert len(session.upserts) == 2, session.upserts
    assert [(c["idea_a_id"], c["idea_b_id"]) for c in session.upserts] == \
        [("idea_new", "idea_a"), ("idea_new", "idea_b")], session.upserts
    for call in session.upserts:
        assert call["type"] == DERIVED_FROM
        assert call["accumulate"] is False, "parentage is set once, never accumulated"
        assert call["evidence"] == "synthesis/idea_a+idea_b"
    print("ok (f): --derived-from writes child -> parent per parent, weight set not added")

    # (g) an unreadable locator is reported and written nowhere, not guessed at.
    session = _FakeSession(derived=[{"child_id": "idea_bad", "locator": "synthesis/"}])
    outcomes = process_derived(session)
    assert session.upserts == [], session.upserts
    assert outcomes[0]["parents"] == [] and outcomes[0]["new_weight"] is None, outcomes
    assert outcomes[0]["missing"] is True, "an unwritten parentage is not a success"
    print("ok (g): unparseable locator -> reported, no edge invented")

    # (h) a parentage that lost a half is still written — the edge it names is true —
    # but it is not allowed to print like a whole one (`13` §6 puts two parents there).
    session = _FakeSession(derived=[{"child_id": "idea_half", "locator": "synthesis/idea_a"}])
    outcomes = process_derived(session)
    assert len(session.upserts) == 1, session.upserts
    assert all(o["missing"] is False for o in outcomes), outcomes
    print("ok (h): a one-parent locator is written and flagged [partial], not passed off as whole")

    # (i) a derived-from write against an absent parent is MISSING, and `main` turns
    # that into a non-zero exit instead of the green an empty database used to give.
    session = _FakeSession(derived=[{"child_id": "idea_x", "locator": "synthesis/idea_a+idea_b"}],
                           missing=True)
    outcomes = process_derived(session)
    assert all(o["missing"] for o in outcomes), outcomes
    assert [o for o in outcomes if o.get("missing")], "main keys its exit code on this"
    print("ok (i): absent parent -> MISSING on every row, which is what main exits 1 on")

    print("idea_edges self-check OK")


if __name__ == "__main__":
    import sys
    sys.exit(main())
