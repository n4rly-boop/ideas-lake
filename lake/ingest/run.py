"""Corpus orchestration: two phases with `data/staging.jsonl` between them (§4.7).

    phase 1  parallel, 8 threads, writes NOTHING to the graph
             fetch -> parse -> generalize -> vector -> staging.jsonl
             (acceptance by eye happens here, before the graph is ever opened)

    phase 2  sequential, cursor next to the staging file, restartable from any line
             per source: write_source -> link (overlay) -> create_idea_with_theses
                         -> index_theses (same batch, right here) -> rederive

CLI:
    python3 -m lake.ingest.run phase1 [--limit N] [--sources path]
    python3 -m lake.ingest.run phase2 [--limit N]
    python3 -m lake.ingest.run selfcheck      # offline, temp db + index, no network

`--limit` is what makes an end-to-end run on 2-3 papers possible instead of 84.
"""
import argparse
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import graph_client, index, llm, trace
from ..models import (PENDING_LINK, STAGING, STAGING_CURSOR, DraftThesis, IdeaFields,
                      Source, text_hash)

# Phase 1 runs in 8 threads and every one of them appends to the same file.
_staging_lock = threading.Lock()


# ------------------------------------------------------------------------ phase 1

def phase1(entries: list[dict], workers: int = 8) -> int:
    """fetch -> parse -> generalize -> vector -> `staging.jsonl`. Returns lines written.

    Nothing here touches the graph (§4.7). A source that dies takes only itself down:
    it is named and counted in the report, which is the opposite of fail-open — the
    loss has a name and a number instead of a silently shorter corpus.

    The corpus file is the only one this phase writes: a single url goes through
    `ingest_one`, which drives `_one_source` over a staging file of its own.
    """
    llm.assert_grammar_works(llm.QWEN_9B)       # canary per model used, every run (§6.1)
    from . import fetch, generalize, parse      # local: phase 2 needs none of these

    staging_path = Path(STAGING)                # module global: `selfcheck` rebinds it
    # Derived from the file, not the `STAGING_CURSOR` constant, and for the same
    # reason: `selfcheck` rebinds `STAGING` to a temp path and the constant would
    # keep pointing at the real `data/staging.cursor` — a check that deletes the
    # operator's cursor while proving it wrote nowhere near the real data.
    cursor_path = _cursor_path(staging_path)
    written = leaked = dropped = 0
    failed: list[tuple[str, str]] = []
    staging_path.parent.mkdir(parents=True, exist_ok=True)
    # Phase 1 rewrites lines of the sources it touches, so every line number after
    # them moves and a cursor left over from an earlier phase 2 points at the wrong
    # row. Dropping it costs nothing: on the replay, link step [0] skips everything
    # already stored without a single LLM call (§4.5, §4.8).
    if cursor_path.exists():
        cursor_path.unlink()
        print(f"phase1: dropped {cursor_path.name}, phase 2 will replay from the top")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one_source, entry, fetch, parse, generalize, staging_path): entry
                   for entry in entries}
        for future, entry in futures.items():
            name = entry.get("arxiv_id") or entry.get("url") or entry.get("title") or "?"
            try:
                lines, leaks, cut = future.result()
            except Exception as exc:            # named loss, not a silent shorter corpus
                failed.append((str(name), f"{type(exc).__name__}: {exc}"))
                continue
            written += lines
            leaked += leaks
            dropped += cut

    print(f"phase1: {len(entries) - len(failed)}/{len(entries)} sources ok, "
          f"{written} staging lines, leakage {leaked}/{written} "
          f"({_share(leaked, written):.2f}), {dropped} cut by the 30/document ceiling, "
          f"graph untouched")
    for name, why in failed:
        print(f"  failed: {name}: {why}")
    return written


def _one_source(entry: dict, fetch, parse, generalize, staging_path) -> tuple[int, int, int]:
    """One source end to end into staging lines. Returns (lines, leaks, dropped)."""
    from .. import embed

    src, sections = fetch.fetch_source(entry)
    # The abstract rides in the section list as reference material for every call
    # (fetch.py), and the bibliography holds no technique — parsing either one
    # spends a call per source to mine theses out of a title list.
    body = [s for s in sections if s.kind not in ("abstract", "bibliography")]
    drafts, report = parse.parse_document(body, fetch.find_abstract(sections),
                                          fetch.find_limitations(sections))
    # `parse_document` counts per section and then cuts to 30 (§4.3 p.6), so the
    # counts expanded in section order line up one-to-one with the theses it kept.
    section_ids = [sid for sid, n in report["per_section"].items() for _ in range(n)]
    section_ids = section_ids[:len(drafts)]
    if len(section_ids) != len(drafts):
        raise ValueError(f"{src.id}: parse_document report does not match its theses")

    ideas = [generalize.generalize(draft) for draft in drafts]
    # One embedding call per source. The vector is over `thesis.text`, the
    # pre-generalization text: candidates are gathered thesis-to-thesis (§4.5).
    vectors = embed.embed_docs([draft.text for draft in drafts])
    source = src.model_dump()
    lines, leaks = [], 0
    for section_id, draft, fields, vector in zip(section_ids, drafts, ideas, vectors):
        leaks += bool(generalize.leakage(draft, fields))
        lines.append(json.dumps({
            "source": source,
            "section_id": section_id,
            "thesis": {"text": draft.text, "context": draft.context, "effect": draft.effect,
                       "locator": draft.locator, "text_hash": text_hash(draft.text)},
            "draft": {"draft_text": draft.draft_text,
                      "draft_applicability": draft.draft_applicability,
                      "draft_limitations": draft.draft_limitations},
            "idea_fields": fields.model_dump(),
            "vector": [float(x) for x in vector],
        }, ensure_ascii=False))

    # One append per source, under the lock: phase 2 groups by source and carries a
    # linear cursor, and per-line appends from 8 threads would scatter a source
    # across the file.
    with _staging_lock:
        _drop_source(src.id, staging_path)
        with staging_path.open("a", encoding="utf-8") as fh:
            fh.write("".join(line + "\n" for line in lines))
    return len(lines), leaks, report["dropped"]


def _drop_source(source_id: str, staging_path) -> None:
    """Remove any earlier lines of this source. Caller holds `_staging_lock`.

    §4.7 expects "fix the parse prompt, re-run phase 1" to be routine. A plain
    append kept both generations in the file, and phase 2 would then write the
    new theses as extra leaves next to the old ones — same source, different
    wording, so `(source_id, text_hash)` does not stop it. Re-running a source
    replaces it.
    """
    if not staging_path.exists():
        return
    kept = [ln for ln in staging_path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and json.loads(ln)["source"]["id"] != source_id]
    tmp = staging_path.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(ln + "\n" for ln in kept), encoding="utf-8")
    tmp.replace(staging_path)


# ------------------------------------------------------------------------ phase 2

def phase2(staging_path=STAGING, limit: int | None = None) -> dict:
    """Sequential ingest of `staging.jsonl` into the graph and the index (§4.7).

    Sequential globally: inside a source because thesis #2 may link to the idea
    thesis #1 just created, between sources because two parallel sources would
    create two ideas under one mechanism (§4.5).

    `limit` — first N sources of the remaining staging, for end-to-end runs on 2-3
    papers. Restart is free: the cursor is written after every source and stage [0]
    of the link cascade skips already-written theses without a single LLM call.
    """
    llm.assert_grammar_works(llm.QWEN_9B)       # rederive
    llm.assert_grammar_works(llm.QWEN_35B)      # link arbiter
    from . import generalize, link, rederive, split

    # Before a single arbiter call: an index that is empty while the store is not
    # makes step [1] return zero candidates for every thesis, and zero candidates
    # reads as "no duplicate" — so a stale index would quietly re-create every idea
    # in the lake, with no LLM call and no pending_link line to show for it.
    repaired, undrifted = _reconcile_index()
    if repaired:
        print(f"phase2: index was missing {repaired} leaf/leaves, re-indexed before linking")
    if undrifted:
        print(f"phase2: {undrifted} leaf/leaves were indexed under the idea they were "
              "split away from, index rebuilt before linking")

    cursor_path = _cursor_path(staging_path)
    rows = _read_staging(staging_path)
    cursor = _read_cursor(cursor_path)
    done = set(range(1, cursor + 1))
    groups = _group_by_source(rows, done)

    processed: list[dict] = []
    rederived = skipped = refused = written = 0
    rederive_failed: list[dict] = []
    splits: list[dict] = []
    split_failed: list[dict] = []

    def split_sweep() -> None:
        """Split every idea over the leaf ceiling (issue #2).

        Called per source AND once after the loop. Per source because an over-broad idea
        keeps absorbing the next source's theses, and because leaving it whole makes the
        §4.6 sweep below pay for a re-derivation over the whole over-broad set that the
        split then throws away — so this runs BEFORE that sweep, not after.

        After the loop because `groups` can be empty: a phase2 whose staging is already
        consumed, or `limit=0`, processes no group at all, and an idea that crossed the
        ceiling on the previous run would otherwise never be looked at again. The split
        is what repairs the existing 92-leaf node, and "run phase2 again" has to actually
        mean it.

        Same granularity as the §4.6 sweep: one idea failing is named and counted, and it
        is retried, because nothing about the idea changed.
        """
        for idea_id in split.due():
            try:
                splits.append(split.split_idea(idea_id))
            except Exception as exc:
                split_failed.append({"idea_id": idea_id,
                                     "error": f"{type(exc).__name__}: {exc}"})

    for group in groups[:limit]:
        src = Source(**group[0][1]["source"])
        graph_client.write_source(src)
        decisions = link.link_batch(src.id, [row for _, row in group])

        by_idea: dict[str, list] = {}
        new_ideas: dict[str, object] = {}
        for decision in decisions:
            if decision["skipped"]:
                # Two different things wear one flag, and they are opposites: a
                # duplicate means the leaf is ALREADY in the lake, an arbiter refusal
                # means it is nowhere and waiting in `pending_link` (§4.5). Counted
                # together, a run where the arbiter refused everything reports the
                # same numbers as a clean replay — see `ingest_one`, which refuses to
                # call that `ok`. `link.py:79,93` is where the two prefixes are set.
                if decision["reason"].startswith("pending_link:"):
                    refused += 1
                else:
                    skipped += 1
                continue
            thesis = decision["thesis"]
            by_idea.setdefault(thesis.idea_id, []).append(thesis)
            if decision["idea"] is not None:
                new_ideas[decision["idea"].id] = decision["idea"]

        for idea_id, theses in by_idea.items():
            # One transaction per idea (§3.4): a failure between the idea and its
            # leaves would leave an idea with zero leaves.
            graph_client.create_idea_with_theses(new_ideas.get(idea_id), src.id, theses)
            # Indexed in the SAME per-idea step, not after the loop (§3.5, §4.7).
            # Batching it after the loop drifts the index permanently and silently:
            # a failure on idea #2 leaves idea #1's leaves committed but unindexed,
            # and the restart skips them at link stage [0] as already stored, so
            # they never get indexed again. Only the §6.19 assert would ever see it.
            index.index_theses(theses)
            written += len(theses)

        _reconcile_index()

        split_sweep()

        # Every idea that is over the trigger, not only the ones this batch touched
        # (§4.6). An idea whose third leaf landed in a source whose loop then died
        # is never in `by_idea` again — the restart skips its theses at link step
        # [0] — so a batch-scoped sweep would leave it un-re-derived forever.
        for idea_id in _rederive_due():
            try:
                if rederive.maybe_rederive(idea_id):
                    rederived += 1
            except Exception as exc:
                # Named and counted, and the trigger field did not move, so the next
                # leaf under this idea retries it. Killing an 84-source run over one
                # re-derivation would cost more than it saves (§4.5 granularity).
                rederive_failed.append({"idea_id": idea_id,
                                        "error": f"{type(exc).__name__}: {exc}"})

        processed += [row for _, row in group]
        done.update(lineno for lineno, _ in group)
        cursor = _advance(cursor, done)
        _write_cursor(cursor_path, cursor)

    split_sweep()       # see the docstring: `groups` can be empty and the node stays

    report = _report(processed, generalize, cursor)
    report.update({"sources_processed": len(groups[:limit]), "theses_written": written,
                   "theses_skipped": skipped, "theses_refused": refused,
                   "rederived": rederived, "rederive_failed": rederive_failed,
                   "splits": splits, "split_failed": split_failed,
                   # Read off the STORE, not off `split_failed`. The failure list counts
                   # attempts — one idea failing under ten sources is ten entries, and an
                   # idea nobody attempted is zero — so it answers "did a call raise",
                   # never "is the lake still collapsing into one node" (issue #2).
                   "ideas_over_ceiling": len(split.due()),
                   "max_leaves_per_idea": max(split.leaf_counts().values(), default=0)})
    _print_report(report)
    return report


# ------------------------------------------------------------------- one source

def ingest_one(entry: dict, staging_path) -> dict:
    """One source from `entry` all the way into the graph. Both phases, own staging.

    This is /fetch: a caller hands over one url and expects the article to be in the
    lake afterwards, so the acceptance file the corpus run stops at (§4.7) is not the
    product here — the graph is.

    It is deliberately NOT `phase1(...)` followed by `phase2()` on the corpus staging,
    for two reasons, both of which would show up as a job that says `ok`:

    * phase 1 drops the shared cursor, so the phase 2 after it replays the whole file
      — one url would drag every other source waiting for acceptance into the graph
      with it, and `limit=1` would ingest the corpus's first source instead of this one.
    * `phase1` names a dead source in its report and returns a count. For a batch of 84
      that is the right granularity; for one url it is a success over an empty lake.
      `_one_source` raises, and the job carries the reason (`jobs._run`).

    Returns the phase 2 report plus what this source cost in phase 1.
    """
    # BOTH canaries before the fetch (§6.1). The arbiter's is inside `phase2`, which
    # for a corpus run is early enough — the staging file is in front of the operator
    # either way. Here the same order would spend the whole fetch, parse, generalize
    # and embed of an article before finding out that the 35B server is down.
    llm.assert_grammar_works(llm.QWEN_9B)
    llm.assert_grammar_works(llm.QWEN_35B)
    from . import fetch, generalize, parse

    name = entry.get("arxiv_id") or entry.get("url")
    staging_path = Path(staging_path)
    staging_path.parent.mkdir(parents=True, exist_ok=True)
    # The whole file, not just this source's lines. Re-fetching a url is routine and
    # `_one_source` would only drop the lines of the id the arXiv API resolves to NOW;
    # a group left by an earlier version of the same article would survive, and phase 2
    # takes the file whole — one url would report `sources_processed: 2`. The cursor
    # goes with it: it counts lines that are about to be rewritten.
    staging_path.unlink(missing_ok=True)
    _cursor_path(staging_path).unlink(missing_ok=True)
    lines, leaks, dropped = _one_source(entry, fetch, parse, generalize, staging_path)
    if not lines:
        # An article the parser found no technique in. Fail-closed: a 0-line staging
        # file makes phase 2 a no-op, and the job would report `ok` with every number
        # at zero — indistinguishable from an article that was already in the lake.
        raise ValueError(f"{name}: parse extracted no theses, nothing to ingest")
    report = phase2(staging_path)
    if report["theses_written"] == 0 and report["theses_refused"]:
        # The other way this ends with an unchanged lake, and the one that looks most
        # like success: the arbiter refused every thesis, so each is in `pending_link`
        # and none is in the graph (§4.5). The counters alone cannot say so — a clean
        # replay of an article already in the lake reports `theses_written: 0` too.
        raise RuntimeError(
            f"{name}: the linking arbiter refused all {report['theses_refused']} of "
            f"{lines} theses, every one of them is queued in pending_link and nothing "
            "reached the graph; re-post the url once the 35B server answers again")
    # Ingested, so the staging file has nothing left to say — the graph is the record
    # now. Kept only on failure, and that is what makes the directory readable: what
    # sits in `data/fetch/` is exactly the articles that did NOT make it, and no ops
    # view lists them (`/ingest/staging` reads the corpus file alone).
    staging_path.unlink(missing_ok=True)
    _cursor_path(staging_path).unlink(missing_ok=True)
    return {**report, "staging_lines": lines, "leakage": leaks, "theses_dropped": dropped}


def _report(processed: list[dict], generalize, cursor: int) -> dict:
    """The numbers §4.7 asks for, over the whole store, not only this run."""
    leaves = graph_client.all_theses()
    orphans = graph_client.ideas_without_leaves()
    ideas = graph_client.get_ideas(sorted({leaf["idea_id"] for leaf in leaves} | set(orphans)))
    multi = sum(1 for idea in ideas if len({t["source_id"] for t in idea["theses"]}) >= 2)
    sources = {t["source_id"] for idea in ideas for t in idea["theses"]}
    leaked = sum(1 for row in processed
                 if generalize.leakage(_draft_of(row), IdeaFields(**row["idea_fields"])))
    return {"sources": len(sources), "theses": len(leaves), "ideas": len(ideas),
            "ideas_multi_source": _share(multi, len(ideas)),
            "pending_link": _count_lines(PENDING_LINK),
            "leakage_share": _share(leaked, len(processed)),
            "ideas_without_leaves": len(orphans),
            "cursor": cursor, **trace.totals()}


def _print_report(report: dict) -> None:
    print("phase2 report:")
    for key, value in report.items():
        print(f"  {key}: {value}")
    if report["ideas_without_leaves"]:
        # IDEA ||--|{ THESIS (`06:85`). Loud, not a number nobody reads.
        print(f"  INVARIANT BROKEN: {report['ideas_without_leaves']} ideas have no leaves")
    # Two independent facts, each from its own source of truth. A split can fail with
    # nothing left over the ceiling (the store commit went through and only the index
    # rebuild raised), and an idea can be over the ceiling with nothing in `split_failed`
    # (a run that processed no source never attempted it). Reporting one as the other is
    # how a lake collapsing into one node reads as healthy — which is issue #2.
    if report.get("split_failed"):
        print(f"  split attempts that failed: {len(report['split_failed'])}")
        for failure in report["split_failed"]:
            print(f"    {failure['idea_id']}: {failure['error']}")
    if report.get("ideas_over_ceiling"):
        from .split import MAX_LEAVES
        print(f"  STILL OVER THE CEILING: {report['ideas_over_ceiling']} idea(s) above "
              f"{MAX_LEAVES} leaves, max is {report['max_leaves_per_idea']} (issue #2)")


def _draft_of(row: dict) -> DraftThesis:
    """Staging line -> DraftThesis, for the leakage recount (§4.4). No LLM call."""
    thesis = {k: v for k, v in row["thesis"].items() if k != "text_hash"}
    return DraftThesis(**thesis, **row["draft"])


# ------------------------------------------------------------------- staging i/o

def _read_staging(path) -> list[tuple[int, dict]]:
    """(physical line number, parsed line). The number is what the cursor counts."""
    out = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if line.strip():
                out.append((lineno, json.loads(line)))
    return out


def _group_by_source(rows: list[tuple[int, dict]], done: set[int]) -> list[list]:
    """Unprocessed lines grouped by source, in order of first appearance."""
    groups: dict[str, list] = {}
    for lineno, row in rows:
        if lineno not in done:
            groups.setdefault(row["source"]["id"], []).append((lineno, row))
    return list(groups.values())


def _reconcile_index() -> tuple[int, int]:
    """Repair the index against the store. Returns (re-indexed, un-drifted).

    `index_theses` runs after `create_idea_with_theses` has already committed, so
    an index write that fails leaves leaves in the graph that the index will never
    see: the restart skips them at link step [0] as already stored. One pass per
    source closes that window, and it is the §6.19 reconciliation path
    (`index.index_rows` over `graph_client.all_theses()`), not a second mechanism.

    The presence pass is not enough since `ingest.split` exists. A split MOVES a leaf
    between ideas and commits that before rebuilding the index; if the rebuild does not
    happen, every leaf is still indexed — so `has()` repairs nothing — and every
    count-based check still agrees, while the arbiter is offered the pre-split parent
    for leaves that left it. That is the issue #2 loop reopening inside the module built
    to close it, so the drift is looked for by value and repaired here too.
    """
    rows = graph_client.all_theses()
    missing = [row for row in rows if not index.has(row["id"])]
    if missing:
        index.index_rows(missing)
    stale = index.stale_links(rows)
    if stale:
        index.reconcile(rows)
    return len(missing), len(stale)


def _rederive_due(threshold: int = 3) -> list[str]:
    """Ideas with `len(leaves) - rederived_at_leaf_count >= threshold` (§4.6)."""
    counts: dict[str, int] = {}
    for row in graph_client.all_theses():
        counts[row["idea_id"]] = counts.get(row["idea_id"], 0) + 1
    due = []
    for idea in graph_client.get_ideas(sorted(counts)):
        if counts[idea["id"]] - idea["rederived_at_leaf_count"] >= threshold:
            due.append(idea["id"])
    return due


def _cursor_path(staging_path) -> Path:
    """`staging.jsonl` -> `staging.cursor` (models.STAGING_CURSOR), same directory."""
    return Path(staging_path).with_suffix(".cursor")


def _read_cursor(path: Path) -> int:
    if not path.exists():
        return 0
    return int(path.read_text(encoding="utf-8").strip() or 0)


def _write_cursor(path: Path, cursor: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{cursor}\n", encoding="utf-8")


def _advance(cursor: int, done: set[int]) -> int:
    """Watermark: the last line with nothing unprocessed before it. Sources whose
    lines are not contiguous (a hand-edited staging file) hold it back instead of
    letting the restart skip over them."""
    while cursor + 1 in done:
        cursor += 1
    return cursor


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open(encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip())


def _share(part: int, whole: int) -> float:
    return round(part / whole, 3) if whole else 0.0


# ------------------------------------------------------------------------- cli

def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="python3 -m lake.ingest.run")
    sub = parser.add_subparsers(dest="phase", required=True)
    one = sub.add_parser("phase1", help="fetch/parse/generalize -> staging.jsonl, no graph")
    one.add_argument("--limit", type=int, help="first N sources only")
    one.add_argument("--sources", default=str(Path(__file__).resolve().parents[1] / "sources.yaml"))
    two = sub.add_parser("phase2", help="staging.jsonl -> graph + index, sequential")
    two.add_argument("--limit", type=int, help="first N sources of the remaining staging")
    sub.add_parser("selfcheck", help="offline end-to-end on fixtures, temp db and index")
    args = parser.parse_args(argv)

    if args.phase == "selfcheck":
        selfcheck()
    elif args.phase == "phase1":
        import yaml                             # only the CLI reads sources.yaml
        entries = yaml.safe_load(Path(args.sources).read_text(encoding="utf-8"))
        phase1(entries[:args.limit])
    else:
        phase2(limit=args.limit)


# ------------------------------------------------------------------- self-check

def selfcheck() -> None:
    """Offline end-to-end over two fixture sources: no network, no model load.

    fetch/parse/generalize/link, `lake.embed` and `llm.complete` are fakes; the store
    and the index go to a temporary directory. ponytail: one runnable check, not a
    suite — it fails if the staging format, the cursor, idempotency or the re-derive
    trigger break.
    """
    import functools
    import sys
    import tempfile
    import types
    import uuid

    import numpy as np

    from .. import stub_store
    from ..models import (EMBED_DIM, Idea, Section, Thesis, new_idea_id,
                          new_thesis_id, source_id as make_source_id)

    global STAGING
    real_staging = STAGING
    real_index_theses, real_complete, real_canary = (index.index_theses, llm.complete,
                                                     llm.assert_grammar_works)
    real_has, real_index_rows = index.has, index.index_rows
    real_stale_links, real_reconcile = index.stale_links, index.reconcile
    real_traces = trace.TRACES_DIR
    trace.set_run_id("selfcheck-" + uuid.uuid4().hex[:6])
    root = __package__.split(".")[0]

    installed: list[tuple] = []

    def install(full_name: str, **members):
        """Put a fake module where the lazy imports of phase 1/2 will find it."""
        module = types.ModuleType(full_name)
        module.__dict__.update(members)
        parent, _, leaf = full_name.rpartition(".")
        # Record what was there: tearing down with an unconditional delattr wiped
        # a real module that the caller had already imported.
        installed.append((full_name, sys.modules.get(full_name),
                          getattr(sys.modules[parent], leaf, None)))
        sys.modules[full_name] = module
        setattr(sys.modules[parent], leaf, module)
        return module

    # --- fakes -------------------------------------------------------------
    def embed_docs(texts):
        out = np.zeros((len(texts), EMBED_DIM), dtype=np.float32)
        for i, text in enumerate(texts):        # deterministic, seeded by the text
            vec = np.random.default_rng(int(text_hash(text)[:8], 16)).standard_normal(EMBED_DIM)
            out[i] = (vec / np.linalg.norm(vec)).astype(np.float32)
        return out

    install(f"{root}.embed", embed_docs=embed_docs)

    # s3 is never in the phase 1 corpus below: it is what /fetch ingests on its own.
    levers = {"s1-S1": ["0", "1"], "s1-S2": ["0", "1"], "s2-S1": ["0", "0"],
              "s3-S1": ["2", "2"]}
    fixtures = {}
    for tag, kind, ids in (("s1", "paper", ["s1-S1", "s1-S2"]), ("s2", "run", ["s2-S1"]),
                           ("s3", "paper", ["s3-S1"]), ("s4", "paper", ["s4-S1"])):
        url = f"https://arxiv.org/abs/{tag}"
        src = Source(id=make_source_id(url, "v1"), url=url, title=f"Paper {tag}", type=kind,
                     version="v1", retrieved_at="2026-07-28T10:00:00Z",
                     run_success=(kind == "run") or None, run_meta=None)
        sections = [Section(id="abstract", kind="abstract", title="Abstract", text="abs text")]
        sections += [Section(id=sid, kind="section", title=f"Method {sid}", text="body")
                     for sid in ids]
        sections.append(Section(id=f"{tag}-bib", kind="bibliography", title="References",
                                text="[1] ..."))
        fixtures[tag] = (src, sections)

    def parse_document(sections, abstract, limitations):
        assert abstract == "abs text", abstract      # reference really reaches the parser
        drafts, per_section = [], {}
        for section in sections:
            got = [DraftThesis(text=f"{section.id}#{n} lever {lever} statement",
                               context="cifar-10, resnet-18", effect="+3.1 pp",
                               locator=f"{section.id} Table {n}",
                               draft_text="d", draft_applicability="a", draft_limitations="l")
                   for n, lever in enumerate(levers.get(section.id, []))]
            per_section[section.id] = len(got)       # zeros included, as the real one does
            drafts += got
        return drafts, {"per_section": per_section, "dropped": 0}

    def generalize_(draft):
        lever = draft.text.split("lever ")[1][0]
        return IdeaFields(text=f"lever {lever}", applicability_conditions="ac",
                          limitations="lim", failure_modes=["fm"])

    def leakage(draft, out):
        return ["dataset name leaked"] if out.text.endswith("1") else []   # lever 1 leaks

    install(f"{__package__}.fetch",
            fetch_source=lambda entry: fixtures[entry["arxiv_id"]],
            find_abstract=lambda sections: next(s.text for s in sections
                                                if s.kind == "abstract"),
            find_limitations=lambda sections: "")
    install(f"{__package__}.parse", parse_document=parse_document)
    install(f"{__package__}.generalize", generalize=generalize_, leakage=leakage)

    ideas_by_text: dict[str, str] = {}

    def link_batch(source_id, rows):
        """Stage [0] + arbiter, faked: same text_hash in this source -> skip, same
        generalized text -> same idea (this is what the batch overlay buys, §4.5)."""
        out = []
        for row in rows:
            th = row["thesis"]
            with stub_store._lock:
                seen = stub_store._c().execute(
                    "SELECT 1 FROM thesis WHERE source_id=? AND text_hash=?",
                    (source_id, th["text_hash"])).fetchone()
            if seen:
                out.append({"thesis": None, "idea": None, "skipped": True,
                            "reason": "text_hash already under this source"})
                continue
            key = row["idea_fields"]["text"]
            idea = None
            if key not in ideas_by_text:
                idea = Idea(id=new_idea_id(), **row["idea_fields"], effect_claimed="",
                            effect_observed="", vector=row["vector"])
                ideas_by_text[key] = idea.id
            thesis = Thesis(id=new_thesis_id(), source_id=source_id,
                            idea_id=ideas_by_text[key], text=th["text"], context=th["context"],
                            effect=th["effect"], locator=th["locator"],
                            text_hash=th["text_hash"], vector=row["vector"],
                            created_at="2026-07-28T10:00:00Z")
            out.append({"thesis": thesis, "idea": idea, "skipped": False, "reason": ""})
        return out

    install(f"{__package__}.link", link_batch=link_batch)

    def complete(prompt, *, system, schema, op, max_tokens, timeout, model=None,
                 temperature=0.0):
        assert op == "rederive" and system.startswith("You re-derive"), op
        assert "LEAVES (4)" in prompt, prompt              # ALL leaves, not just the new ones
        assert "source_type: paper" in prompt and "source_type: run" in prompt, prompt
        return {"text": "REDERIVED lever 0", "applicability_conditions": "ac2",
                "limitations": "lim2", "failure_modes": ["fm2"],
                "effect_claimed": "+3.1 pp on paper leaves",
                "effect_observed": "+1.0 pp on run leaves"}

    llm.assert_grammar_works = lambda model: None
    llm.complete = complete

    from . import rederive

    try:
        with tempfile.TemporaryDirectory() as tmp:
            STAGING = Path(tmp) / "staging.jsonl"
            cursor_path = _cursor_path(STAGING)
            idx = Path(tmp) / "index.db"
            stub_store._db_path = Path(tmp) / "lake.db"
            # Every graph call is @trace'd into TRACES_DIR/<run_id>.jsonl. Deleting the
            # file afterwards was not enough: `_write` mkdirs the directory, and a data/
            # that exists while holding no file is what makes `vault.demo`'s leak guard
            # refuse to run. This check promises to touch no real path; a directory is
            # a real path.
            trace.TRACES_DIR = Path(tmp) / "traces"
            index.index_theses = functools.partial(real_index_theses, db=idx)
            # `_reconcile_index` uses these two, also without a db argument; unbound
            # they wrote fixture rows straight into the real data/index.db.
            index.has = functools.partial(real_has, db=idx)
            index.index_rows = functools.partial(real_index_rows, db=idx)
            # And these two, reached through `_reconcile_index`'s drift repair and
            # through `split.split_idea`. Unbound, `reconcile` REBUILDS the operator's
            # real data/index.db from this fixture store — the same hazard as above,
            # with a destructive rather than an additive ending.
            index.stale_links = functools.partial(real_stale_links, db=idx)
            index.reconcile = functools.partial(real_reconcile, db=idx)

            assert _cursor_path(real_staging) == STAGING_CURSOR, _cursor_path(real_staging)

            # --- phase 1 ---------------------------------------------------
            entries = [{"arxiv_id": "s1"}, {"arxiv_id": "s2"}, {"arxiv_id": "missing"}]
            assert phase1(entries, workers=8) == 6, "4 theses from s1 + 2 from s2"
            lines = [json.loads(ln) for ln in STAGING.read_text(encoding="utf-8").splitlines()]
            assert len(lines) == 6, len(lines)
            for row in lines:
                assert set(row) == {"source", "section_id", "thesis", "draft", "idea_fields",
                                    "vector"}, sorted(row)
                assert set(row["source"]) == {"id", "url", "title", "type", "version",
                                              "retrieved_at", "run_success", "run_meta"}
                assert set(row["thesis"]) == {"text", "context", "effect", "locator",
                                              "text_hash"}
                assert set(row["draft"]) == {"draft_text", "draft_applicability",
                                             "draft_limitations"}
                assert set(row["idea_fields"]) == {"text", "applicability_conditions",
                                                   "limitations", "failure_modes"}
                assert len(row["vector"]) == EMBED_DIM
                assert row["thesis"]["text_hash"] == text_hash(row["thesis"]["text"])
            ids = [row["source"]["id"] for row in lines]
            runs = [sid for i, sid in enumerate(ids) if i == 0 or ids[i - 1] != sid]
            assert len(runs) == len(set(runs)) == 2, \
                "lines of one source must stay contiguous, the cursor is linear"
            assert graph_client.all_theses() == [] and index.count(db=idx) == 0, \
                "phase 1 wrote to the graph or the index"
            assert not cursor_path.exists()
            print("ok: phase 1 — 6 staging lines in the contract shape, graph untouched, "
                  "1 dead source named")

            # 8 threads finish in whatever order, and the restart assertions below
            # need a known one: put source 1 first (stable sort keeps each block).
            order = {fixtures[tag][0].id: i for i, tag in enumerate(("s1", "s2"))}
            lines.sort(key=lambda row: order[row["source"]["id"]])
            STAGING.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n"
                                       for row in lines), encoding="utf-8")

            # --- phase 2, first source only --------------------------------
            first = phase2(STAGING, limit=1)
            assert first["sources_processed"] == 1 and first["theses"] == 4, first
            assert first["ideas"] == 2 and first["rederived"] == 0, first     # 2 leaves each
            assert first["leakage_share"] == 0.5, first   # 2 of source 1's 4 lines leak
            assert _read_cursor(cursor_path) == 4, _read_cursor(cursor_path)
            assert index.count(db=idx) == 4, index.count(db=idx)
            print("ok: phase 2 on 1 source — 4 leaves, 2 ideas, cursor 4, no re-derive yet")

            # --- restart: continues from the cursor -------------------------
            second = phase2(STAGING)
            assert second["sources_processed"] == 1, "restart re-processed source 1"
            assert second["theses"] == 6 and second["ideas"] == 2, second
            assert second["theses_skipped"] == 0 and second["theses_written"] == 2, second
            assert second["rederived"] == 1 and second["rederive_failed"] == [], second
            assert second["ideas_without_leaves"] == 0, second
            assert index.count(db=idx) == 6 == second["theses"], "index drifted from the graph"
            assert _read_cursor(cursor_path) == 6
            assert second["sources"] == 2 and second["ideas_multi_source"] == 0.5, second
            assert second["leakage_share"] == 0.0 and second["pending_link"] == 0, second
            #  ^ source 2 is all lever 0; the whole staging replayed below is 2/6
            assert second["wall_ms"] > 0, second
            print("ok: restart from the cursor — source 2 only, 6 leaves, index == graph")

            # --- the re-derive itself ---------------------------------------
            hot = ideas_by_text["lever 0"]
            cold = ideas_by_text["lever 1"]
            idea = graph_client.get_ideas([hot])[0]
            assert idea["id"] == hot, "re-derive must not change the idea id"
            assert idea["text"] == "REDERIVED lever 0" and idea["rederived_at_leaf_count"] == 4
            assert idea["effect_claimed"] == "+3.1 pp on paper leaves"
            assert idea["effect_observed"] == "+1.0 pp on run leaves"
            assert idea["failure_modes"] == ["fm2"] and idea["limitations"] == "lim2"
            assert not idea["dirty"] and idea["trust_score"] > 0, "dirty/trust_score are B's"
            assert np.allclose(idea["vector"], embed_docs(["REDERIVED lever 0"])[0], atol=1e-6), \
                "text changed and the vector did not follow it"
            assert rederive.maybe_rederive(cold) is False, "3 leaves needed, cold has 2"
            print("ok: re-derive — id kept, six fields + counter + vector written, "
                  "claimed/observed apart")

            # --- idempotency: same staging again -----------------------------
            _write_cursor(cursor_path, 0)
            again = phase2(STAGING)
            assert again["theses"] == 6 and again["ideas"] == 2, again
            assert again["theses_skipped"] == 6 and again["rederived"] == 0, again
            assert again["theses_written"] == 0, "a replay wrote new theses"
            assert again["leakage_share"] == 0.333, again
            assert index.count(db=idx) == 6
            print("ok: replay of the whole staging — 6 skipped, 0 new theses, 0 new ideas")

            # --- /fetch: one source, own staging, corpus untouched -----------
            corpus_before = STAGING.read_text(encoding="utf-8")
            corpus_cursor = _read_cursor(cursor_path)
            fetch_staging = Path(tmp) / "fetch" / "s3.jsonl"
            one = ingest_one({"arxiv_id": "s3", "type": "paper"}, fetch_staging)
            assert one["staging_lines"] == 2 and one["sources_processed"] == 1, one
            assert one["theses_written"] == 2 and one["theses_skipped"] == 0, one
            assert one["theses_refused"] == 0, one
            assert one["theses"] == 8 and one["ideas"] == 3, one     # 6 + 2, a new lever
            assert index.count(db=idx) == 8, index.count(db=idx)
            # Ingested, so nothing is left behind: what stays in the directory is the
            # articles that did NOT make it, and no ops view lists them.
            assert not fetch_staging.exists() and not _cursor_path(fetch_staging).exists()
            # The corpus staging is the acceptance point of §4.7 and /fetch is a
            # different job: sharing the file would replay it and drag every source
            # waiting for acceptance into the graph.
            assert STAGING.read_text(encoding="utf-8") == corpus_before, "corpus staging moved"
            assert _read_cursor(cursor_path) == corpus_cursor == 6, "the corpus cursor moved"
            print("ok: /fetch — 1 source, 2 leaves, own staging and cursor, corpus file "
                  "byte-identical")

            # The same url twice is the normal case (a re-fetch), and it must not
            # double the leaves: same Source.id, link step [0] skips what is stored.
            again_one = ingest_one({"arxiv_id": "s3", "type": "paper"}, fetch_staging)
            assert again_one["theses_written"] == 0 and again_one["theses_skipped"] == 2, again_one
            assert again_one["theses_refused"] == 0, again_one
            assert again_one["theses"] == 8 and again_one["ideas"] == 3, again_one
            assert index.count(db=idx) == 8

            # Three ways this ends with an unchanged lake, and all three must raise —
            # a job that returns a report says `ok`. `phase1` counts a dead source as a
            # named loss and lives on, which for a batch of 84 is right and for one url
            # is a success over an untouched graph.
            #
            # The third is the one that looks most like success: the arbiter refuses
            # every thesis, so each is queued in `pending_link` and none is written,
            # and the counters alone read exactly like the legal replay just above.
            refusing = types.SimpleNamespace(link_batch=lambda source_id, rows: [
                {"thesis": None, "idea": None, "skipped": True,
                 "reason": "pending_link: LLMError: server said no"} for _ in rows])
            fake_link = sys.modules[f"{__package__}.link"]
            refused_staging = Path(tmp) / "fetch" / "s3-refused.jsonl"
            for entry, exc_type, path, arbiter in (
                    ({"arxiv_id": "missing", "type": "paper"}, KeyError,
                     Path(tmp) / "fetch" / "missing.jsonl", fake_link),
                    ({"arxiv_id": "s4", "type": "paper"}, ValueError,
                     Path(tmp) / "fetch" / "s4.jsonl", fake_link),
                    ({"arxiv_id": "s3", "type": "paper"}, RuntimeError,
                     refused_staging, refusing)):
                setattr(sys.modules[__package__], "link", arbiter)   # phase2 imports by name
                try:
                    ingest_one(entry, path)
                except exc_type as exc:
                    assert exc_type is not RuntimeError or (
                        "pending_link" in str(exc) and "refused all 2" in str(exc)), exc
                else:
                    raise AssertionError(f"{entry} returned a report instead of raising")
                finally:
                    setattr(sys.modules[__package__], "link", fake_link)
            assert len(graph_client.all_theses()) == 8, "a failed /fetch wrote to the graph"
            # The refused article is still on disk with its lines: that IS the record of
            # work waiting, and re-posting the url replays it.
            assert len(refused_staging.read_text(encoding="utf-8").splitlines()) == 2
            print("ok: /fetch — a replay writes nothing; a dead url, an empty parse and "
                  "an arbiter that refused every thesis all raise instead of reporting ok")

            # --- fail-closed: the arbiter of re-derivation dies ---------------
            third = "third leaf for the cold idea"
            extra = Thesis(id=new_thesis_id(), source_id=lines[0]["source"]["id"],
                           idea_id=cold, text=third, context="ctx", effect="+1 pp",
                           locator="§9", text_hash=text_hash(third),
                           vector=lines[0]["vector"], created_at="2026-07-28T10:00:00Z")
            graph_client.create_idea_with_theses(None, extra.source_id, [extra])
            before = graph_client.get_ideas([cold])[0]

            def boom(*a, **kw):
                raise llm.LLMError("server said no")

            llm.complete = boom
            try:
                rederive.maybe_rederive(cold)
            except llm.LLMError:
                pass
            else:
                raise AssertionError("an LLM failure returned success from maybe_rederive")
            after = graph_client.get_ideas([cold])[0]
            assert after["text"] == before["text"], "idea changed on a failed re-derivation"
            assert after["rederived_at_leaf_count"] == 0, "the trigger moved on a failure"
            print("ok: failed re-derivation — idea untouched, counter not moved, raised")

            index._CONNS.pop(str(idx)).close()
            stub_store._conn.close()
            stub_store._conn = None
    finally:
        STAGING = real_staging
        index.index_theses, llm.complete = real_index_theses, real_complete
        index.has, index.index_rows = real_has, real_index_rows
        index.stale_links, index.reconcile = real_stale_links, real_reconcile
        llm.assert_grammar_works = real_canary
        for full_name, prev_mod, prev_attr in reversed(installed):
            parent, _, leaf = full_name.rpartition(".")   # a fake left behind is a trap
            if prev_mod is None:
                sys.modules.pop(full_name, None)
            else:
                sys.modules[full_name] = prev_mod
            if prev_attr is None:
                delattr(sys.modules[parent], leaf)
            else:
                setattr(sys.modules[parent], leaf, prev_attr)
        trace.TRACES_DIR = real_traces

    print("run self-check OK")


if __name__ == "__main__":
    main()
