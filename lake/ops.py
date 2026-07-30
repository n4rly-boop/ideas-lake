"""Composed operations of block A: the work that is more than one store call.

All of this used to live in `lake/api/routes.py`, which made HTTP the only way to
reach it — and two of these are guards, not conveniences:

* a re-post of a source must not move its `title`/`type`, because those are read
  back as `source_title`/`source_type` of every leaf and are therefore provenance
  of theses that are frozen (§1.2);
* a patched idea `text` must drag its vector with it (§1.3), or the idea's
  neighbourhood drifts away from what the idea now says.

A guard that only a route enforces is a guard every importer of `graph_client`
walks straight past, silently and with a 200-shaped result — the fail-open shape
this project bans. So the composition lives here, importable, with no FastAPI in
it; the routes are wrappers that map the exceptions below onto statuses.

Not here on purpose: the pass-through reads (`list_sources`, `get_idea`,
`list_theses`, `search`, `retrieve`). They already have module equivalents in
`graph_client`, `index` and `retrieve.api`, and a second name for one call is not
an operation.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from . import graph_client, index
from .api import jobs          # threading and dicts only — no HTTP in it either
from .models import PENDING_LINK, STAGING, STAGING_CURSOR, Source, source_id as make_source_id


class OpsError(Exception):
    """A refusal the caller can act on — as opposed to a store that fell over,
    which stays an exception of the store's own type (`graph_client.STORE_ERRORS`)."""


class NotFound(OpsError):
    """No such row."""


class Conflict(OpsError):
    """The request contradicts what is already stored, or the single slot is taken."""


class Broken(OpsError):
    """State on disk that cannot be read as what it claims to be: a corrupt cursor,
    a cursor past the end of the file, a torn staging line. Named and refused, never
    smoothed over — these are exactly what an operator opens these views to see."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _lines(path: Path) -> list[str]:
    if not Path(path).exists():
        return []
    return [ln for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln.strip()]


def _cursor(lines: int | None = None) -> int:
    """Phase 2's watermark. Absent file means nothing ingested yet, which is 0 —
    the one case where a default is the truth and not a guess.

    Anything else is refused with the reason. These two views are what an operator
    opens when the ingest is already in a bad state, so a bare traceback is the
    least useful answer they could give; and a cursor past the end of the file is
    corruption, not "everything ingested" — clamping it to zero pending lines would
    report a finished ingest for a file the cursor no longer fits.
    """
    path = Path(STAGING_CURSOR)
    if not path.exists():
        return 0
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return 0
    if not raw.isdigit():
        raise Broken(f"{path.name} holds {raw!r}, not a line number")
    cursor = int(raw)
    if lines is not None and cursor > lines:
        raise Broken(f"{path.name} is at {cursor}, past the {lines} lines of "
                     f"{Path(STAGING).name} — the two disagree; drop the cursor "
                     f"to replay (phase 2 skips what is already stored)")
    return cursor


# --------------------------------------------------------------------------- graph

def upsert_source(url: str, title: str, type: str, version: str = "v1",
                  retrieved_at: str | None = None, run_success: bool | None = None,
                  run_meta: dict | None = None) -> dict:
    """Create or replace a source; this is how block C reports a run back.

    The id is derived from (url, version), so re-posting the same run replaces the
    row instead of duplicating it — that is what makes this safe to call after every
    evolution run (§1.1).

    What a re-post may change is the run outcome (`run_success`, `run_meta`,
    `retrieved_at`) and nothing else. `title` and `type` are read back through the
    JOIN as `source_title` / `source_type` of every leaf of that source, and
    `source_type` is what the linker uses to keep `effect_claimed` apart from
    `effect_observed` (§4.6). Letting a re-post move them would rewrite the
    provenance of theses that are supposed to be frozen (§1.2) — without ever
    touching the thesis table. Raises `Conflict`.
    """
    src = Source(id=make_source_id(url, version), url=url, title=title, type=type,
                 version=version, retrieved_at=retrieved_at or _now(),
                 run_success=run_success, run_meta=run_meta)
    existing = graph_client.get_source(src.id)
    if existing is not None:
        changed = [name for name in ("title", "type")
                   if getattr(src, name) != existing[name]]
        if changed:
            raise Conflict(f"source {existing['id']} already exists with a different "
                           f"{' and '.join(changed)}; those are provenance of its "
                           f"leaves. Re-post only run_success/run_meta.")
    graph_client.write_source(src)
    return graph_client.get_source(src.id)


def patch_idea(idea_id: str, fields: dict) -> dict:
    """Write `fields` onto an idea and return the stored row (vector included).

    `text` drags the vector with it: the idea vector is derived from the text
    (§1.3), and writing one without the other drifts the idea's neighbourhood away
    from what the idea now says — the same rule `rederive` follows (§4.6). A caller
    that sets both loses its own vector, deliberately: the text is the source.

    Raises `NotFound`. What may be patched at all is the HTTP layer's business
    (`schemas.IdeaPatch`); what the columns accept is the store's (`stub_store`).
    """
    fields = dict(fields)
    if "text" in fields:
        from . import embed          # local: loading sentence-transformers costs seconds
        fields["vector"] = embed.embed_docs([fields["text"]])[0].tolist()
    fields["updated_at"] = _now()
    try:
        graph_client.update_idea(idea_id, fields)
    except KeyError:
        raise NotFound(f"idea {idea_id} not found")
    return graph_client.get_ideas([idea_id])[0]


# ----------------------------------------------------------------------------- ops

def reindex() -> dict:
    """The repair path of §6.19, and the only supported answer to a `degraded` health
    check: the store carries `idea_id`, which phase 2 assigns and `staging.jsonl`
    therefore never holds.

    It takes the ingest slot for the duration — a rebuild racing a phase 2 would index
    a moving target — and every vector is validated before the old index is dropped,
    so a refusal leaves the suspect index in place instead of emptying it. Raises
    `Conflict` when the slot is taken.
    """
    try:
        with jobs.exclusive("reindex") as job:
            before = index.count()
            rows = graph_client.all_theses()
            index.reconcile(rows)
            after = index.count()
            job["report"] = {"indexed_before": before, "leaves_in_store": len(rows),
                             "indexed_after": after}
            return {"indexed_before": before, "leaves_in_store": len(rows),
                    "indexed_after": after, "in_sync": after == len(rows)}
    except jobs.Busy as busy:
        raise Conflict(str(busy))


def health() -> dict:
    """Liveness plus the one invariant that rots silently. Never raises: a health
    check that dies tells the caller less than one that says what is wrong."""
    from . import queue
    from .api import workers
    try:
        leaves = graph_client.counts()["theses"]
        indexed = index.count()
        pending = queue.counts()
    except Exception as exc:
        return {"status": "degraded", "detail": f"{type(exc).__name__}: {exc}"}
    threads = workers.alive()
    # A dead worker is invisible from every other angle: the queue keeps accepting, every
    # job keeps its status, and nothing moves. Both halves can die on their own — a dead
    # writer strands `staged` articles that are already parsed, a dead fetch pool strands
    # `queued` ones that were never touched — and each is silent in the other's numbers.
    # Only work actually waiting makes it degraded: a healthz that fails on an idle lake
    # with no threads (the CLI, a self-check, `--mock`) would cry wolf.
    stalled = []
    if pending["staged"] and not threads.get("writer", False):
        stalled.append(f"{pending['staged']} article(s) parsed and waiting while the "
                       "phase-2 writer thread is not alive — nothing will reach the "
                       "graph until the process restarts")
    if pending["queued"] and not any(alive for name, alive in threads.items()
                                     if name.startswith("fetch")):
        stalled.append(f"{pending['queued']} article(s) queued while no fetch worker is "
                       "alive — nothing will be parsed until the process restarts")
    ok = indexed == leaves and not stalled
    detail = None
    if stalled:
        detail = "; ".join(stalled)
    elif indexed != leaves:
        detail = "index and store disagree — POST /admin/reindex (§6.19)"
    return {"status": "ok" if ok else "degraded", "theses_indexed": indexed,
            "leaves_in_store": leaves, "in_sync": indexed == leaves, "detail": detail}


def stats() -> dict:
    """The §4.7 numbers over the whole lake. Raises `Broken` on a corrupt cursor:
    a number that cannot be computed is absent, never guessed."""
    from . import queue
    from .api import workers
    counts = graph_client.counts()
    indexed = index.count()
    running = jobs.running()
    return {**counts, "queue": queue.counts(), "workers": workers.alive(),
            "theses_indexed": indexed,
            "in_sync": indexed == counts["theses"],
            "ideas_without_leaves": graph_client.ideas_without_leaves(),
            "trust_scale": graph_client.trust_scale(),
            "staging_lines": len(_lines(STAGING)),
            "staging_cursor": _cursor(),
            "pending_link": len(_lines(PENDING_LINK)),
            "job_running": running["id"] if running else None}


# -------------------------------------------------------------------------- ingest

def staging_state() -> dict:
    """What sits between the phases, grouped by source. Raises `Broken`."""
    # ponytail: parses the whole file to group by source. Fine at 84 sources x 30
    # lines; if staging ever grows past that, keep a sidecar index instead.
    lines = _lines(STAGING)
    cursor = _cursor(len(lines))
    per_source: dict[str, dict] = {}
    total = 0
    for lineno, line in enumerate(lines, 1):
        try:
            src = json.loads(line)["source"]
            key, title = src["id"], src["title"]
        except (ValueError, KeyError, TypeError) as exc:
            # A phase 1 killed mid-write leaves a truncated last line. Name the line
            # instead of dying with a traceback: this view is the one an operator
            # opens precisely because something went wrong.
            raise Broken(f"{Path(STAGING).name}:{lineno} is not a staging line "
                         f"({type(exc).__name__}: {exc})")
        entry = per_source.setdefault(key, {"id": key, "title": title,
                                            "lines": 0, "ingested": 0})
        entry["lines"] += 1
        entry["ingested"] += lineno <= cursor
        total += 1
    return {"lines": total, "cursor": cursor, "pending_lines": max(0, total - cursor),
            "sources": list(per_source.values())}


def pending_link(limit: int = 50) -> list[dict]:
    """The arbiter's refusal queue (§4.5), freshest last.

    This queue existing at all is the fail-closed behaviour: an arbiter that failed
    writes here instead of guessing `add` or `new`. Non-empty means theses were parsed
    and never attached — work waiting, not work lost. The full lines (staging row and
    all candidates) stay in `data/pending_link.jsonl`.
    """
    out = []
    for line in _lines(PENDING_LINK)[-limit:]:
        rec = json.loads(line)
        row = rec.get("staging_line") or {}
        out.append({"ts": rec.get("ts", ""), "run_id": rec.get("run_id"),
                    "error": rec.get("error", ""),
                    "thesis_text": (row.get("thesis") or {}).get("text", ""),
                    "source_id": (row.get("source") or {}).get("id", ""),
                    "candidates": len(rec.get("candidates") or [])})
    return out
