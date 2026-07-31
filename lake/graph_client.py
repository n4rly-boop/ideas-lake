"""The only module that knows format B (spec 10 §3.4). Changing the storage format
is an edit of this file and nothing else (`08:60`).

Two backends exist — `stub_store` (SQLite) and `neo4j_store` (Bolt) — with the
identical function set. `LAKE_STORE=stub|neo4j` (default `stub`) picks between them
once, at import, and callers never see which one answered (`13` §4.1).

Every graph call is traced: the call is initiated by A even though the graph is B's,
and D needs the whole picture (§3.3, `08:293`).

Storage failures propagate as exceptions — `retrieve/api.py` turns them into 503,
because `ideas: []` means "the lake has nothing" and is data, while a broken graph
is not (§5.4). **There is no fallback**: an unreachable Neo4j is a `STORE_ERRORS`
exception and a 503, never a quiet switch to SQLite — that would make a broken
graph look like an empty lake, the exact fail-open §5.4 exists to rule out
(`13` §4.1).

There is no `update_thesis` and there will not be one: thesis immutability (§1.2)
is held by the absence of the method (§3.4), checked by selfcheck §6.9.
"""
import os
import sqlite3

from neo4j.exceptions import Neo4jError, ServiceUnavailable

from . import stub_store
from .models import Idea, Source, Thesis
from .trace import trace

# What "the store is down" is, as a set of exception types. The HTTP layer turns
# these into 503 and everything else into 500. `ServiceUnavailable`/`Neo4jError`
# have to be here unconditionally — not only when `LAKE_STORE=neo4j` — because the
# backend is chosen once at import and a tuple assembled conditionally on that
# choice would silently downgrade every graph route from 503 to 500 on the exact
# day the backend flips (this is the failure §4.2 of `13` names by address:
# "not add neo4j.exceptions... here" was the trap). Swapping storage format still
# means editing this tuple, here, next to the calls it describes — it just no
# longer means swapping *which* exceptions.
STORE_ERRORS: tuple[type[BaseException], ...] = (sqlite3.Error, ServiceUnavailable, Neo4jError)

_STUB, _NEO4J = "stub", "neo4j"


def _select_backend():
    """Backend choice and both directions of the refusal `13` §4.1 asks for, run
    once at import so a broken configuration keeps the process from starting at
    all rather than surfacing mid-request as a graph-route 503.

    Both directions matter equally: `LAKE_STORE=neo4j` with no `NEO4J_URI` is the
    obvious one, but `NEO4J_URI` set while `LAKE_STORE` stays at its `stub`
    default is the one that is easy to ship by accident — a deployment that
    carries `NEO4J_URI` for an unrelated reason (`neo4j_load`'s one-way push, say)
    would otherwise write to SQLite next to a configured graph URI nobody is
    using, and look healthy doing it.

    That second direction has to tell apart two states that look identical if you
    only read the resolved value (BLOCKER 3): `LAKE_STORE` never mentioned at all
    (an operator forgot it, or copied `.env.local` from before this variable
    existed) versus `LAKE_STORE=stub` written out by hand, on purpose, next to a
    `NEO4J_URI` that exists only for `neo4j_load`'s one-way push. The former is
    the exact crash-loop this refusal exists to catch; the latter is a legitimate,
    documented deployment (`.env.local.example`, `docker-compose.yml`) that this
    refusal must not also break. `os.environ.get("LAKE_STORE")` returning `None`
    is how "never mentioned" is told apart from "said `stub`" — collapsing that
    into a single default (as the previous version did) makes the two
    indistinguishable again.
    """
    store_raw = os.environ.get("LAKE_STORE")
    explicit = store_raw is not None
    store = store_raw if explicit else _STUB
    if store not in (_STUB, _NEO4J):
        raise RuntimeError(f"LAKE_STORE must be {_STUB!r} or {_NEO4J!r}, got {store!r}")
    uri = os.environ.get("NEO4J_URI")
    if store == _NEO4J and not uri:
        raise RuntimeError("LAKE_STORE=neo4j requires NEO4J_URI in the environment (13 §4.1)")
    if store == _STUB and uri and not explicit:
        raise RuntimeError(
            f"NEO4J_URI is set ({uri!r}) but LAKE_STORE was never set (defaults to "
            f"{_STUB!r}) — refusing to start rather than silently writing to SQLite "
            "next to a configured graph URI nobody is using (13 §4.1, BLOCKER 3). "
            "NEO4J_URI here is likely there only for `neo4j_load`'s one-way push, not "
            f"for this server; if SQLite really is what you want, set LAKE_STORE={_STUB!r} "
            "explicitly to say so — 'never mentioned' and 'said stub' are different "
            "states and this refusal only fires on the first one.")
    if store == _NEO4J:
        from . import neo4j_store
        return neo4j_store, _NEO4J
    return stub_store, _STUB


_backend, _BACKEND_NAME = _select_backend()


def backend_name() -> str:
    """Which backend answered the choice above — for the startup log and
    `/healthz` (`13` §4.1). The API layer reads this; it does not decide it."""
    return _BACKEND_NAME


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
    """Every leaf + vector. Feeds index reconciliation (§6.19) — see `stub_store`."""
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
    """[] while `edge` is empty — ranking degrades to flat top-k (§3.4, `08:377`)."""
    return _backend.neighbors(ids, hops, min_weight)


# ---------------------------------------------------------------------- self-check
# Entirely offline: every backend-selection combination is exercised in a fresh
# subprocess (the choice is a module-level singleton fixed at import, `13` §4.1),
# so nothing here needs a reachable Neo4j — the one exception (MAJOR 7) points at
# a closed local port, which fails immediately, not a real server.

if __name__ == "__main__":
    import subprocess
    import sys
    from pathlib import Path

    # MAJOR 4: shrinking STORE_ERRORS back to `(sqlite3.Error,)` must go red here.
    assert sqlite3.Error in STORE_ERRORS, STORE_ERRORS
    assert ServiceUnavailable in STORE_ERRORS, STORE_ERRORS
    assert Neo4jError in STORE_ERRORS, STORE_ERRORS
    print("ok: STORE_ERRORS carries sqlite3.Error, ServiceUnavailable and Neo4jError")

    root = str(Path(__file__).resolve().parents[1])

    def _run(code: str, env_overrides: dict) -> subprocess.CompletedProcess:
        env = {**os.environ, "PYTHONPATH": root}
        for key in ("LAKE_STORE", "NEO4J_URI"):
            env.pop(key, None)
        env.update(env_overrides)
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              timeout=30, cwd=root, env=env)

    # BLOCKER 3, direction 1: LAKE_STORE forgotten next to a configured NEO4J_URI
    # (the loader's variable, left set) must still refuse to start.
    forgotten = _run("import lake.graph_client", {"NEO4J_URI": "bolt://neo4j:7687"})
    assert forgotten.returncode != 0 and "BLOCKER 3" in forgotten.stderr, \
        (forgotten.returncode, forgotten.stderr)
    print("ok: forgotten LAKE_STORE next to a configured NEO4J_URI still refuses to start")

    # BLOCKER 3, direction 2: LAKE_STORE=stub said EXPLICITLY, same NEO4J_URI —
    # a legitimate deployment (`.env.local.example`) and it must start.
    explicit_stub = _run("import lake.graph_client; print('OK')",
                         {"LAKE_STORE": "stub", "NEO4J_URI": "bolt://neo4j:7687"})
    assert explicit_stub.returncode == 0 and "OK" in explicit_stub.stdout, \
        (explicit_stub.returncode, explicit_stub.stderr)
    print("ok: explicit LAKE_STORE=stub next to NEO4J_URI (for the loader) still starts")

    # Original direction, kept: LAKE_STORE=neo4j with no NEO4J_URI still refuses.
    no_uri = _run("import lake.graph_client", {"LAKE_STORE": "neo4j"})
    assert no_uri.returncode != 0, no_uri.stdout
    print("ok: LAKE_STORE=neo4j with no NEO4J_URI still refuses to start")

    # Neither set: the boring case, nothing to refuse, stub default.
    neither = _run("import lake.graph_client; print('OK')", {})
    assert neither.returncode == 0 and "OK" in neither.stdout, (neither.returncode, neither.stderr)
    print("ok: neither LAKE_STORE nor NEO4J_URI set — starts on the stub default")

    # MAJOR 7: neo4j backend selected, server unreachable — the call must RAISE,
    # never quietly answer from stub_store. Port 1 on the loopback: nothing binds
    # there, so the driver fails fast instead of timing out.
    unreachable = _run(
        "import lake.graph_client as gc\n"
        "try:\n"
        "    gc.counts()\n"
        "except gc.STORE_ERRORS as exc:\n"
        "    print('RAISED', type(exc).__name__)\n"
        "else:\n"
        "    print('ANSWERED')\n",
        {"LAKE_STORE": "neo4j", "NEO4J_URI": "bolt://127.0.0.1:1"})
    assert unreachable.stdout.strip().startswith("RAISED"), \
        (unreachable.stdout, unreachable.stderr)
    print("ok: neo4j backend + unreachable server raises, never falls back to SQLite (MAJOR 7)")

    print("graph_client self-check OK")
