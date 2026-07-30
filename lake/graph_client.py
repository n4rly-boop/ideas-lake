"""The only module that knows format B (spec 10 §3.4). Changing the storage format
is an edit of this file and nothing else (`08:60`).

Today every call goes to `stub_store` (SQLite). When Neo4j is up, the bodies here
switch over; callers do not change. There is no Neo4j branch yet — it gets written
when the database exists, not before.

Every graph call is traced: the call is initiated by A even though the graph is B's,
and D needs the whole picture (§3.3, `08:293`).

Storage failures propagate as exceptions — `retrieve/api.py` turns them into 503,
because `ideas: []` means "the lake has nothing" and is data, while a broken graph
is not (§5.4).

There is no `update_thesis` and there will not be one: thesis immutability (§1.2)
is held by the absence of the method (§3.4), checked by selfcheck §6.9.
"""
import sqlite3

from . import stub_store
from .models import Idea, Source, Thesis
from .trace import trace

# What "the store is down" is, as an exception type. The HTTP layer turns these
# into 503 and everything else into 500, and it must not name `sqlite3` itself to
# do it: §3.4 keeps format B in this file, and the day the backend becomes Bolt
# an API layer keyed on `sqlite3.Error` would quietly downgrade every store
# failure from 503 to 500 — the distinction §5.4 exists to protect. Swapping the
# backend means editing this tuple, here, next to the calls it describes.
STORE_ERRORS: tuple[type[BaseException], ...] = (sqlite3.Error,)


@trace(component="graph", op="write_source")
def write_source(src: Source) -> str:
    return stub_store.write_source(src)


@trace(component="graph", op="write_theses")
def write_theses(source_id: str, theses: list[Thesis]) -> list[str]:
    return stub_store.write_theses(source_id, theses)


@trace(component="graph", op="create_idea")
def create_idea(idea: Idea) -> str:
    return stub_store.create_idea(idea)


@trace(component="graph", op="create_idea_with_theses")
def create_idea_with_theses(idea: Idea | None, source_id: str, theses: list[Thesis]) -> list[str]:
    """One transaction (§3.4). `idea=None` — the idea exists, only append leaves."""
    return stub_store.create_idea_with_theses(idea, source_id, theses)


@trace(component="graph", op="update_idea")
def update_idea(idea_id: str, fields: dict) -> None:
    return stub_store.update_idea(idea_id, fields)


@trace(component="graph", op="split_idea")
def split_idea(parent_id: str, parent_fields: dict, children: list) -> None:
    """Move part of `parent_id`'s leaves onto new ideas, one transaction (§3.4).

    `children` is [(Idea, [thesis_id, ...])]. This is the only call that writes a
    thesis row after phase 2 and it writes exactly one column, `idea_id`; thesis
    immutability (§1.2) is about what the source said, and none of that is touched.
    Still not `update_thesis`, and there is still no `update_thesis` (§6.9).
    """
    return stub_store.split_idea(parent_id, parent_fields, children)


@trace(component="graph", op="get_ideas")
def get_ideas(ids: list[str]) -> list[dict]:
    """Ideas with leaves already joined to source.type/url/title (§3.4)."""
    return stub_store.get_ideas(ids)


@trace(component="graph", op="get_leaves")
def get_leaves(idea_id: str) -> list[dict]:
    return stub_store.get_leaves(idea_id)


@trace(component="graph", op="leaf_count")
def leaf_count(idea_id: str) -> int:
    return stub_store.leaf_count(idea_id)


@trace(component="graph", op="get_source")
def get_source(source_id: str) -> dict | None:
    return stub_store.get_source(source_id)


@trace(component="graph", op="list_sources")
def list_sources(limit: int = 50, offset: int = 0) -> list[dict]:
    return stub_store.list_sources(limit, offset)


@trace(component="graph", op="list_idea_ids")
def list_idea_ids(limit: int = 50, offset: int = 0) -> list[str]:
    """Ids for one page; `get_ideas` turns them into bodies with leaves."""
    return stub_store.list_idea_ids(limit, offset)


@trace(component="graph", op="list_theses")
def list_theses(idea_id: str | None = None, source_id: str | None = None,
                limit: int = 50, offset: int = 0) -> list[dict]:
    return stub_store.list_theses(idea_id, source_id, limit, offset)


@trace(component="graph", op="count_theses")
def count_theses(idea_id: str | None = None, source_id: str | None = None) -> int:
    return stub_store.count_theses(idea_id, source_id)


@trace(component="graph", op="get_thesis")
def get_thesis(thesis_id: str) -> dict | None:
    return stub_store.get_thesis(thesis_id)


@trace(component="graph", op="counts")
def counts() -> dict:
    """Rows per table — what `/stats` reports (§4.7 numbers, served over HTTP)."""
    return stub_store.counts()


@trace(component="graph", op="all_theses")
def all_theses() -> list[dict]:
    """Every leaf + vector. Feeds index reconciliation (§6.19) — see `stub_store`."""
    return stub_store.all_theses()


@trace(component="graph", op="ideas_without_leaves")
def ideas_without_leaves() -> list[str]:
    return stub_store.ideas_without_leaves()


@trace(component="graph", op="trust_scale")
def trust_scale() -> float:
    """Fixed scale for `trust_norm` in ranking (§5.3), declared by the storage side."""
    return stub_store.trust_scale()


@trace(component="graph", op="neighbors")
def neighbors(ids: list[str], hops: int = 1, min_weight: float | None = None) -> list[dict]:
    """[] while `edge` is empty — ranking degrades to flat top-k (§3.4, `08:377`)."""
    return stub_store.neighbors(ids, hops, min_weight)
