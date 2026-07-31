"""The only module that knows format B (spec 10 §3.4). Changing the storage format
is an edit of this file and nothing else (`08:60`).

Neo4j (`neo4j_store`, Bolt) is the only backend (D11). `NEO4J_URI` is required —
its absence is a `RuntimeError` on import, not a quiet fallback.

Every graph call is traced: the call is initiated by A even though the graph is B's,
and D needs the whole picture (§3.3, `08:293`).

Storage failures propagate as exceptions — `retrieve/api.py` turns them into 503,
because `ideas: []` means "the lake has nothing" and is data, while a broken graph
is not (§5.4). **There is no fallback**: an unreachable Neo4j is a `STORE_ERRORS`
exception and a 503, never a quiet switch to something else — that would make a
broken graph look like an empty lake, the exact fail-open §5.4 exists to rule out.

There is no `update_thesis` and there will not be one: thesis immutability (§1.2)
is held by the absence of the method (§3.4), checked by selfcheck §6.9.
"""
import os
import sqlite3

from neo4j.exceptions import Neo4jError, ServiceUnavailable

from .models import Idea, Source, Thesis
from .trace import trace

# What "the store is down" is, as a set of exception types. The HTTP layer turns
# these into 503 and everything else into 500. `sqlite3.Error` stays even though
# the graph backend is Neo4j-only now (D11): `index.py`'s FTS index is still
# SQLite regardless of which graph backend is chosen, and its own corruption
# check raises `sqlite3.DatabaseError` (`index.py:186-203`) expecting THIS tuple
# to turn it into a 503 at the API boundary (`api/app.py:227`) — dropping it here
# would silently downgrade every FTS-corruption response from 503 to 500.
STORE_ERRORS: tuple[type[BaseException], ...] = (sqlite3.Error, ServiceUnavailable, Neo4jError)


def _select_backend():
    """`NEO4J_URI` required, checked once at import so a missing configuration
    keeps the process from starting at all rather than surfacing mid-request as
    a graph-route 503 (D11)."""
    if not os.environ.get("NEO4J_URI"):
        raise RuntimeError("NEO4J_URI is required — Neo4j is the only backend (D11)")
    from . import neo4j_store
    return neo4j_store


_backend = _select_backend()


def backend_name() -> str:
    """For the startup log and `/healthz` (`13` §4.1). The API layer reads this;
    it does not decide it."""
    return "neo4j"


@trace(component="graph", op="write_source")
def write_source(src: Source) -> str:
    return _backend.write_source(src)


@trace(component="graph", op="write_theses")
def write_theses(source_id: str, theses: list[Thesis]) -> list[str]:
    return _backend.write_theses(source_id, theses)


@trace(component="graph", op="create_idea")
def create_idea(idea: Idea) -> str:
    return _backend.create_idea(idea)


@trace(component="graph", op="create_idea_with_theses")
def create_idea_with_theses(idea: Idea | None, source_id: str, theses: list[Thesis]) -> list[str]:
    """One transaction (§3.4). `idea=None` — the idea exists, only append leaves."""
    return _backend.create_idea_with_theses(idea, source_id, theses)


@trace(component="graph", op="update_idea")
def update_idea(idea_id: str, fields: dict) -> None:
    return _backend.update_idea(idea_id, fields)


@trace(component="graph", op="dirty_ideas")
def dirty_ideas(limit: int | None = None) -> list[str]:
    return _backend.dirty_ideas(limit)


@trace(component="graph", op="set_trust")
def set_trust(idea_id: str, score: float) -> None:
    """The judge's score, and the only lowering of `dirty` (`13` §3.2-3.3).

    Deliberately not `update_idea(id, {...})` from the caller: those two columns move
    together or the lake ends up clean with a score from before the leaves it now has.
    """
    return _backend.set_trust(idea_id, score)


@trace(component="graph", op="split_idea")
def split_idea(parent_id: str, parent_fields: dict, children: list) -> None:
    """Move part of `parent_id`'s leaves onto new ideas, one transaction (§3.4).

    `children` is [(Idea, [thesis_id, ...])]. This is the only call that writes a
    thesis row after phase 2 and it writes exactly one column, `idea_id`; thesis
    immutability (§1.2) is about what the source said, and none of that is touched.
    Still not `update_thesis`, and there is still no `update_thesis` (§6.9).
    """
    return _backend.split_idea(parent_id, parent_fields, children)


@trace(component="graph", op="get_ideas")
def get_ideas(ids: list[str]) -> list[dict]:
    """Ideas with leaves already joined to source.type/url/title (§3.4)."""
    return _backend.get_ideas(ids)


@trace(component="graph", op="get_leaves")
def get_leaves(idea_id: str) -> list[dict]:
    return _backend.get_leaves(idea_id)


@trace(component="graph", op="leaf_count")
def leaf_count(idea_id: str) -> int:
    return _backend.leaf_count(idea_id)


@trace(component="graph", op="get_source")
def get_source(source_id: str) -> dict | None:
    return _backend.get_source(source_id)


@trace(component="graph", op="list_sources")
def list_sources(limit: int = 50, offset: int = 0) -> list[dict]:
    return _backend.list_sources(limit, offset)


@trace(component="graph", op="list_idea_ids")
def list_idea_ids(limit: int = 50, offset: int = 0) -> list[str]:
    """Ids for one page; `get_ideas` turns them into bodies with leaves."""
    return _backend.list_idea_ids(limit, offset)


@trace(component="graph", op="list_theses")
def list_theses(idea_id: str | None = None, source_id: str | None = None,
                limit: int = 50, offset: int = 0) -> list[dict]:
    return _backend.list_theses(idea_id, source_id, limit, offset)


@trace(component="graph", op="count_theses")
def count_theses(idea_id: str | None = None, source_id: str | None = None) -> int:
    return _backend.count_theses(idea_id, source_id)


@trace(component="graph", op="get_thesis")
def get_thesis(thesis_id: str) -> dict | None:
    return _backend.get_thesis(thesis_id)


@trace(component="graph", op="counts")
def counts() -> dict:
    """Rows per table — what `/stats` reports (§4.7 numbers, served over HTTP)."""
    return _backend.counts()


@trace(component="graph", op="all_theses")
def all_theses() -> list[dict]:
    """Every leaf + vector. Feeds index reconciliation (§6.19) — see `neo4j_store`."""
    return _backend.all_theses()


@trace(component="graph", op="ideas_without_leaves")
def ideas_without_leaves() -> list[str]:
    return _backend.ideas_without_leaves()


@trace(component="graph", op="trust_scale")
def trust_scale() -> float:
    """Fixed scale for `trust_norm` in ranking (§5.3), declared by the storage side."""
    return _backend.trust_scale()


@trace(component="graph", op="neighbors")
def neighbors(ids: list[str], hops: int = 1, min_weight: float | None = None) -> list[dict]:
    """Real co-citation and derived_from edges (D12, 2026-07-31). Used for edge-based
    recall in ranking: `via="edge"` when thesis search alone does not fill k slots."""
    return _backend.neighbors(ids, hops, min_weight)


@trace(component="graph", op="write_cocitation_edges")
def write_cocitation_edges(source_id: str, min_ideas: int = 2,
                          dry_run: bool = False) -> list[dict]:
    """Co-citation edges for one source's own leaves, both directions, weight
    idempotent per source (D12 — A writes Idea-Idea edges in the pipeline now,
    `13` §3.1 is stale). `min_ideas`: how many DISTINCT ideas the source must
    touch before any pair is formed — not a per-idea thesis count (BLOCKER 2,
    `13` review 2026-07-31; see `neo4j_store.write_cocitation_edges`)."""
    return _backend.write_cocitation_edges(source_id, min_ideas, dry_run)


@trace(component="graph", op="write_derived_from_edges")
def write_derived_from_edges(child_id: str, parent_ids: list[str],
                             dry_run: bool = False) -> list[dict]:
    """One `derived_from` edge per parent, child -> parent, weight fixed (D12).
    See `neo4j_store.write_derived_from_edges`."""
    return _backend.write_derived_from_edges(child_id, parent_ids, dry_run)


# ---------------------------------------------------------------------- self-check
# Entirely offline: the refusal is exercised in a fresh subprocess (the choice is
# a module-level singleton fixed at import, `13` §4.1), so nothing here needs a
# reachable Neo4j — the one exception (unreachable server) points at a closed
# local port, which fails immediately, not a real server.

if __name__ == "__main__":
    import subprocess
    import sys
    from pathlib import Path

    assert sqlite3.Error in STORE_ERRORS, STORE_ERRORS
    assert ServiceUnavailable in STORE_ERRORS, STORE_ERRORS
    assert Neo4jError in STORE_ERRORS, STORE_ERRORS
    print("ok: STORE_ERRORS carries sqlite3.Error (index.py's FTS store), "
          "ServiceUnavailable and Neo4jError")

    root = str(Path(__file__).resolve().parents[1])

    def _run(code: str, env_overrides: dict) -> subprocess.CompletedProcess:
        env = {**os.environ, "PYTHONPATH": root}
        env.pop("NEO4J_URI", None)
        env.update(env_overrides)
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              timeout=30, cwd=root, env=env)

    # D11: no NEO4J_URI at all must refuse to start, not fall back to anything.
    no_uri = _run("import lake.graph_client", {})
    assert no_uri.returncode != 0 and "NEO4J_URI" in no_uri.stderr, \
        (no_uri.returncode, no_uri.stderr)
    print("ok: missing NEO4J_URI refuses to start")

    # NEO4J_URI set — import must succeed (backend selection alone doesn't connect).
    with_uri = _run("import lake.graph_client; print('OK')",
                    {"NEO4J_URI": "bolt://neo4j:7687"})
    assert with_uri.returncode == 0 and "OK" in with_uri.stdout, \
        (with_uri.returncode, with_uri.stderr)
    print("ok: NEO4J_URI set — starts")

    # Server unreachable — the call must RAISE, never quietly answer from
    # somewhere else. Port 1 on the loopback: nothing binds there, so the driver
    # fails fast instead of timing out.
    unreachable = _run(
        "import lake.graph_client as gc\n"
        "try:\n"
        "    gc.counts()\n"
        "except gc.STORE_ERRORS as exc:\n"
        "    print('RAISED', type(exc).__name__)\n"
        "else:\n"
        "    print('ANSWERED')\n",
        {"NEO4J_URI": "bolt://127.0.0.1:1"})
    assert unreachable.stdout.strip().startswith("RAISED"), \
        (unreachable.stdout, unreachable.stderr)
    print("ok: unreachable server raises, never falls back silently")

    print("graph_client self-check OK")
