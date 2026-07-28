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
import dataclasses
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
    """
    llm.assert_grammar_works(llm.QWEN_9B)       # canary per model used, every run (§6.1)
    from . import fetch, generalize, parse      # local: phase 2 needs none of these

    written = leaked = dropped = 0
    failed: list[tuple[str, str]] = []
    STAGING.parent.mkdir(parents=True, exist_ok=True)
    # Phase 1 rewrites lines of the sources it touches, so every line number after
    # them moves and a cursor left over from an earlier phase 2 points at the wrong
    # row. Dropping it costs nothing: on the replay, link step [0] skips everything
    # already stored without a single LLM call (§4.5, §4.8).
    if STAGING_CURSOR.exists():
        STAGING_CURSOR.unlink()
        print(f"phase1: dropped {STAGING_CURSOR.name}, phase 2 will replay from the top")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one_source, entry, fetch, parse, generalize): entry
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


def _one_source(entry: dict, fetch, parse, generalize) -> tuple[int, int, int]:
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
    source = dataclasses.asdict(src)
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
            "idea_fields": dataclasses.asdict(fields),
            "vector": [float(x) for x in vector],
        }, ensure_ascii=False))

    # One append per source, under the lock: phase 2 groups by source and carries a
    # linear cursor, and per-line appends from 8 threads would scatter a source
    # across the file.
    with _staging_lock:
        _drop_source(src.id)
        with STAGING.open("a", encoding="utf-8") as fh:
            fh.write("".join(line + "\n" for line in lines))
    return len(lines), leaks, report["dropped"]


def _drop_source(source_id: str) -> None:
    """Remove any earlier lines of this source. Caller holds `_staging_lock`.

    §4.7 expects "fix the parse prompt, re-run phase 1" to be routine. A plain
    append kept both generations in the file, and phase 2 would then write the
    new theses as extra leaves next to the old ones — same source, different
    wording, so `(source_id, text_hash)` does not stop it. Re-running a source
    replaces it.
    """
    if not STAGING.exists():
        return
    kept = [ln for ln in STAGING.read_text(encoding="utf-8").splitlines()
            if ln.strip() and json.loads(ln)["source"]["id"] != source_id]
    tmp = STAGING.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(ln + "\n" for ln in kept), encoding="utf-8")
    tmp.replace(STAGING)


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
    from . import generalize, link, rederive

    # Before a single arbiter call: an index that is empty while the store is not
    # makes step [1] return zero candidates for every thesis, and zero candidates
    # reads as "no duplicate" — so a stale index would quietly re-create every idea
    # in the lake, with no LLM call and no pending_link line to show for it.
    repaired = _reconcile_index()
    if repaired:
        print(f"phase2: index was missing {repaired} leaf/leaves, re-indexed before linking")

    cursor_path = _cursor_path(staging_path)
    rows = _read_staging(staging_path)
    cursor = _read_cursor(cursor_path)
    done = set(range(1, cursor + 1))
    groups = _group_by_source(rows, done)

    processed: list[dict] = []
    rederived = skipped = written = 0
    rederive_failed: list[dict] = []

    for group in groups[:limit]:
        src = Source(**group[0][1]["source"])
        graph_client.write_source(src)
        decisions = link.link_batch(src.id, [row for _, row in group])

        by_idea: dict[str, list] = {}
        new_ideas: dict[str, object] = {}
        for decision in decisions:
            if decision["skipped"]:             # duplicate, or arbiter failure -> pending_link
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

    report = _report(processed, generalize, cursor)
    report.update({"sources_processed": len(groups[:limit]), "theses_written": written,
                   "theses_skipped": skipped, "rederived": rederived,
                   "rederive_failed": rederive_failed})
    _print_report(report)
    return report


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


def _reconcile_index() -> int:
    """Re-index whatever the store has and the index does not. Returns the count.

    `index_theses` runs after `create_idea_with_theses` has already committed, so
    an index write that fails leaves leaves in the graph that the index will never
    see: the restart skips them at link step [0] as already stored. One pass per
    source closes that window, and it is the §6.19 reconciliation path
    (`index.index_rows` over `graph_client.all_theses()`), not a second mechanism.
    """
    missing = [row for row in graph_client.all_theses() if not index.has(row["id"])]
    if missing:
        index.index_rows(missing)
    return len(missing)


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
    from ..models import (EMBED_DIM, Idea, Section, Thesis, TRACES_DIR, new_idea_id,
                          new_thesis_id, source_id as make_source_id)

    global STAGING
    real_staging = STAGING
    real_index_theses, real_complete, real_canary = (index.index_theses, llm.complete,
                                                     llm.assert_grammar_works)
    real_has, real_index_rows = index.has, index.index_rows
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

    levers = {"s1-S1": ["0", "1"], "s1-S2": ["0", "1"], "s2-S1": ["0", "0"]}
    fixtures = {}
    for tag, kind, ids in (("s1", "paper", ["s1-S1", "s1-S2"]), ("s2", "run", ["s2-S1"])):
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
            index.index_theses = functools.partial(real_index_theses, db=idx)
            # `_reconcile_index` uses these two, also without a db argument; unbound
            # they wrote fixture rows straight into the real data/index.db.
            index.has = functools.partial(real_has, db=idx)
            index.index_rows = functools.partial(real_index_rows, db=idx)

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
        (TRACES_DIR / f"{trace.current_run_id()}.jsonl").unlink(missing_ok=True)

    print("run self-check OK")


if __name__ == "__main__":
    main()
