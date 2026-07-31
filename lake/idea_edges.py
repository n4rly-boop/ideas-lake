"""Weight the edges between Idea nodes that share a Source, against the real Neo4j the
lake was loaded into (`knowledge/07-roles-and-contracts.md:70-81`).

Not a `graph_client` backend, same reason as `idea_merger.py` and `neo4j_load.py`:
`graph_client` fronts `stub_store`, which never held the ideas this operates on.
Filling `Idea—Idea` edges is block B's side (`06-proposal-design.md` §6) and, as of
`knowledge/07-roles-and-contracts.md:81`, there were none at all in the loaded graph —
this is what puts the first ones there.

Two ideas that both leaf into the same Source (`(:Source)-[:YIELDS]->(:Thesis)<-[:HAS_LEAF]-(:Idea)`)
are co-cited by that source; every source with >= `min_theses` distinct co-cited ideas
contributes one weight increment per unordered idea pair. Repeated runs accumulate the
weight rather than overwrite it, so a source seen again (a re-post through `upsert_source`,
or a second load) keeps compounding it — deduplicating that is `--dry-run`'s job, not
this script's: run it once per source per intended increment.

TODO (left as found, not this pass's job): `compute_weight_increment` is
`min(count_a, count_b)`, chosen as a placeholder. It rewards two ideas for how many
leaves EACH has under the source, not for how much the source specifically ties them
together, so an idea with many unrelated leaves under one source inflates every pair it
is in.
"""
import argparse
import os

DEFAULT_MIN_THESES = 2
DEFAULT_REL_TYPE = "RELATED_VIA_SOURCE"

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


def compute_weight_increment(count_a: int, count_b: int) -> float:
    """Weight added to one Idea-Idea edge for one shared Source. See the TODO above."""
    return min(count_a, count_b)


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


def upsert_edge(session, idea_a_id: str, idea_b_id: str, increment: float,
                rel_type: str) -> float:
    query = f"""
    MATCH (a:Idea {{id: $idea_a_id}})
    MATCH (b:Idea {{id: $idea_b_id}})
    MERGE (a)-[r:{rel_type}]-(b)
    ON CREATE SET r.weight = 0
    SET r.weight = coalesce(r.weight, 0) + $increment,
        r.updated_at = datetime()
    RETURN r.weight AS new_weight
    """
    rec = session.run(query, idea_a_id=idea_a_id, idea_b_id=idea_b_id,
                      increment=increment).single()
    return rec["new_weight"] if rec else None


def process(session, min_theses: int = DEFAULT_MIN_THESES, rel_type: str = DEFAULT_REL_TYPE,
           dry_run: bool = False) -> list[dict]:
    """One pass over the whole graph. Returns what happened to each pair, so a caller
    (or the self-check) can assert on it instead of only reading stdout."""
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

        new_weight = upsert_edge(session, rec["idea_a_id"], rec["idea_b_id"], increment, rel_type)
        print(f"[source={rec['source_id']}] {rec['idea_a_id']} <-> {rec['idea_b_id']}: "
             f"+{increment} -> weight = {new_weight}")
        outcomes.append({**rec, "increment": increment, "new_weight": new_weight})
    return outcomes


# ============================================================
# CLI
# ============================================================

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m lake.idea_edges",
        description="Weight RELATED_VIA_SOURCE edges between Idea nodes sharing a Source.")
    parser.add_argument("--min-theses", type=int, default=DEFAULT_MIN_THESES,
                        help=f"minimum co-cited theses per idea under one source "
                             f"(default {DEFAULT_MIN_THESES})")
    parser.add_argument("--rel-type", default=DEFAULT_REL_TYPE,
                        help=f"edge type to write (default {DEFAULT_REL_TYPE!r})")
    parser.add_argument("--dry-run", action="store_true",
                        help="find and print pairs, write nothing")
    parser.add_argument("--self-check", action="store_true",
                        help="offline check of the weight formula, no Neo4j connection")
    args = parser.parse_args(argv)

    if args.self_check:
        demo()
        return 0

    driver = open_driver()
    try:
        with driver.session(database=database_name()) as session:
            process(session, min_theses=args.min_theses, rel_type=args.rel_type,
                    dry_run=args.dry_run)
    finally:
        driver.close()
    return 0


# ============================================================
# Self-check — the pure formula and the query shapes, no Neo4j
# ============================================================

class _FakeRecord(dict):
    def single(self):
        return self


class _FakeSession:
    """Scripted co-citation results for `find_pairs`, recorded calls for `upsert_edge`."""

    def __init__(self, pairs: list[dict]):
        self._pairs = pairs
        self.upserts: list[tuple] = []

    def run(self, query, **params):
        if "MATCH (s:Source)" in query:
            return [dict(p) for p in self._pairs]
        self.upserts.append((params["idea_a_id"], params["idea_b_id"], params["increment"]))
        return _FakeRecord({"new_weight": params["increment"]})


def demo() -> None:
    # (a) the formula itself: min() of the two leaf counts under the shared source.
    assert compute_weight_increment(3, 5) == 3
    assert compute_weight_increment(2, 2) == 2
    assert compute_weight_increment(0, 4) == 0
    print("ok (a): compute_weight_increment(a, b) == min(a, b)")

    pairs = [{"source_id": "s1", "idea_a_id": "idea_a", "count_a": 3,
             "idea_b_id": "idea_b", "count_b": 2}]

    # (b) dry-run finds pairs and writes nothing.
    session = _FakeSession(pairs)
    outcomes = process(session, dry_run=True)
    assert len(outcomes) == 1 and outcomes[0]["new_weight"] is None
    assert session.upserts == [], "dry-run must not touch the graph"
    print("ok (b): --dry-run finds pairs, upserts nothing")

    # (c) a real pass computes the increment and calls upsert_edge with it.
    session = _FakeSession(pairs)
    outcomes = process(session, dry_run=False)
    assert outcomes[0]["increment"] == 2, outcomes
    assert session.upserts == [("idea_a", "idea_b", 2)], session.upserts
    print("ok (c): a real pass upserts (idea_a, idea_b, increment)")

    print("idea_edges self-check OK")


if __name__ == "__main__":
    import sys
    sys.exit(main())
