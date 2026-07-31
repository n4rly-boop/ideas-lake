"""Evolution logs (GigaEvo) -> staging rows of exactly the phase-1 format (`13` §2).

    payload_from_csv  CSV -> the same body POST /run takes
    from_payload       payload -> staging.jsonl-shaped rows (this is the converter)
    from_csv            CSV -> payload -> from_payload, one call, no second code path
    drain_run           phase 2 for a batch: reuses `run.phase2`, own staging file
    leak_terms          per-source forbidden terms for the leakage check (§2.2.1)

One `Source` per MUTANT, not per run (§2.1): a run is 100+ programs with 100+ different
fitnesses, and the contract's "one Source, one outcome" only holds at mutant grain.
`-1000.0` is a validation-timeout marker baked into the same column as real fitness
(§1.3) and is never treated as a number here — not in `run_success`, not in `effect`,
not in a delta. "No measurement" has three distinct, separately-counted causes (dead,
no-fitness, root) plus a fourth axis entirely — an unparseable `metadata_mutation_output`
never reaches a `run_success` decision at all, it just yields no thesis (`rows_unparsed`).

CLI:
    python3 -m lake.ingest.runlog <evolution_full.csv> [--limit N] [--min-abs-delta X] [--dry-run]
"""
import argparse
import csv
import io
import json
import keyword
import re
import tokenize
from datetime import datetime, timezone
from pathlib import Path

from .. import graph_client, llm, trace
from ..models import RUN_DIR, DraftThesis, Source, source_id as make_source_id, text_hash

# The marker a failed validation run leaves in `metric_fitness` (README.md:37-52,
# timeout 2400s) — the same column real fitness lives in, so it can never be read as
# a number: not as `own_fitness`, not as a parent's fitness, not in `effect`.
DEAD_FITNESS = -1000.0

# A mutant's structured output needs at least these two to build anything (§1.1):
# `archetype` for the source title, `changes` for the theses. `justification`,
# `insights_used` and `code` are read with `.get(..., default)` — their absence
# degrades run_meta/leak_terms, it does not make the row unparseable.
_MO_REQUIRED = ("archetype", "changes")

# Function/class names out of the mutant's own source (§9 p.8): these are the
# concrete identifiers a generalized thesis must not leak, and they are stored in
# `run_meta` (not the raw code) so `leak_terms` can be recomputed from a staging row
# alone, long after the payload that carried `code` is gone.
#
# The real `code` field (§9 p.8) is a `def entrypoint() -> dict: return {...}` whose
# dict VALUES are the prompt's English sentences and whose KEYS are quoted scaffold
# names ("system_prompt", "stage_action", "reasoning_questions", ...) repeated
# verbatim across nearly every mutant of an archetype (§9 p.9, "these programs are
# prompt scaffolds") — neither belongs in a per-mutant leak list, and both live
# inside STRING literals, not as bare identifiers. `_code_names` below tokenizes
# with the stdlib `tokenize` module rather than scanning with a bare identifier
# regex, because `tokenize` is what actually tells a NAME token (`entrypoint`,
# `helper_fn`) apart from a STRING token (`"system_prompt"`, the sentence inside
# it) — a regex over the raw text cannot make that distinction at all, string
# quoting is not a lexical feature `\b[A-Za-z_]\w*\b` sees.
_DEF_KEYWORDS = frozenset(("def", "class"))
# Regex fallback for `code` the tokenizer cannot read (a mutation diff is not
# guaranteed to be complete, standalone Python) — weaker than the tokenizer (it
# cannot tell a string literal from bare code) but still narrower than the
# pre-this-round regex: no `\s*` before the `(` (real code never puts a space
# between a call and its paren; "the final answer (an integer)" does), and a
# literal backslash-escape ("\n" as two characters, not a newline — a prompt's
# own un-decoded text) is neutralized first so it cannot fuse with the next word
# ("sequence \nReasoning" -> "nReasoning", read as one fake camelCase token).
_DEF_NAME = re.compile(r"^[ \t]*(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)
_CALL_NAME = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\(")
_IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_ESCAPE_SEQ = re.compile(r"\\[ntr]")
# Python syntax plus the handful of builtins a mutant calls constantly without
# leaking anything (`print(...)`, `len(...)`) — kept out of `_code_names` so a
# call-site scan does not fire on the shape of ordinary code, only its names.
_CODE_STOPWORDS = frozenset(keyword.kwlist) | {
    "print", "len", "str", "int", "float", "bool", "list", "dict", "set", "tuple",
    "sorted", "range", "sum", "min", "max", "open", "type", "super", "self", "cls",
    "isinstance", "getattr", "setattr", "hasattr", "enumerate", "zip", "map",
    "filter", "format", "input", "round", "abs",
}


def _is_leak_shaped(name: str) -> bool:
    """snake_case, a digit anywhere, or true mixed case (an uppercase letter past
    position 0 AND a lowercase letter somewhere) — ordinary English, including
    ALL-CAPS emphasis ("MUST", "ONLY", "ANSWER" are ordinary language, every
    letter of it uppercase, MAJOR second round), has none of the three."""
    return ("_" in name or any(c.isdigit() for c in name) or
            (any(c.isupper() for c in name[1:]) and any(c.islower() for c in name)))


# ------------------------------------------------------------------------- helpers

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_fitness(raw) -> float | None:
    """`''` (a `state=running` row that never finished, §1.3) -> None. Never raises
    on a genuinely numeric string; a non-numeric one is a CSV that lied about its own
    schema and is allowed to raise — there is nothing sane to substitute."""
    if raw is None:
        return None
    raw = str(raw).strip()
    return float(raw) if raw else None


def _parse_int(raw) -> int | None:
    if raw is None:
        return None
    raw = str(raw).strip()
    return int(raw) if raw else None


def _coerce_fitness(value) -> float | None:
    """`from_payload` accepts both an already-parsed batch (`payload_from_csv`, where
    `fitness` is already `float | None`) and a raw POST body (where it may still be a
    JSON number or `null`, i.e. Python `int | float | None`). A bare string only shows
    up if a caller hand-built a payload from CSV text without going through
    `payload_from_csv` — `_parse_fitness` covers that case too."""
    if value is None or isinstance(value, (int, float)):
        return float(value) if value is not None else None
    return _parse_fitness(value)


def _derive_run_id(path) -> str:
    """`.../runs/aime_seed1/results/evolution_full.csv` -> `aime_seed1`: the run id is
    the directory that holds `results/`, which is how the three ground-truth files are
    laid out. Anything not nested that way falls back to the file stem — a caller who
    cares passes `run_id=` explicitly rather than leaning on file layout."""
    path = Path(path)
    if path.parent.name == "results" and len(path.parents) > 1:
        return path.parents[1].name
    return path.stem


def _derive_seed(run_id: str) -> str:
    match = re.search(r"seed(\d+)$", run_id)
    return match.group(1) if match else run_id


def _default_task_id(run_id: str) -> str:
    """No `task` column exists in the CSV (§1.1) and POST /run's `task_id` is optional
    (§2.5) — the run id is the only place the files themselves name the benchmark, as
    "<task>_seed<N>", so stripping the seed suffix is what is actually knowable here."""
    stripped = re.sub(r"_seed\d+$", "", run_id)
    return stripped or run_id


def _code_names(code: str) -> list[str]:
    """Identifiers the mutant's code actually CONTAINS, not only the ones it
    DEFINES (MINOR 5, §9 p.9): a name it only calls is exactly as concrete a
    leak as one it introduces, and the old `def`/`class`-only regex let it
    straight through — the mutation this misses is a call-site-only name.

    Tokenized (MAJOR, second round), not regex-scanned: `code` is real Python
    (`def entrypoint() -> dict: return {...}`) whose dict VALUES are the
    prompt's English sentences and whose KEYS are quoted, repeated-everywhere
    scaffold names — both live inside STRING tokens, and a bare identifier
    regex cannot tell a STRING from a NAME at all, which is exactly how it read
    "answer (an integer)" and "You MUST return ONLY the ANSWER" as code. Only
    `tokenize.NAME` tokens are ever considered here; a call site is "the next
    token is `(`" and a def/class name is "the previous token is `def`/`class`"
    — both exact, because the tokenizer already resolved every string boundary.

    A NAME that is none of those two is still added if it already LOOKS like
    code rather than English prose: snake_case, a digit anywhere, or true mixed
    case (an uppercase letter past position 0 AND a lowercase letter somewhere
    — `_is_leak_shaped`). A plain lowercase word or an ALL-CAPS one ("the",
    "ANSWER") is ordinary language, shouted or not, not a leaked identifier —
    the line is drawn there, not at length alone, because a generalized
    thesis's own prose is full of length->=3 English words. `_CODE_STOPWORDS`
    additionally drops Python syntax and the handful of builtins a mutant calls
    constantly (`print`, `len`, ...).

    Falls back to a (weaker, string-blind) regex scan when `code` is not
    complete, standalone Python the tokenizer can read — a mutation diff is not
    guaranteed to be one — rather than reading an unparseable mutant as
    carrying no names at all, which would be the fail-open version of this.
    """
    if not code:
        return []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(code).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return _code_names_fallback(code)
    names = set()
    for i, tok in enumerate(toks):
        if tok.type != tokenize.NAME or keyword.iskeyword(tok.string):
            continue
        name = tok.string
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        # `tokenize` drops whitespace between tokens from the stream, so "the next
        # token is `(`" alone is not adjacency — "answer (an integer)" tokenizes to
        # the exact same NAME/OP('(') pair "helper_fn(" does. `tok.end == nxt.start`
        # (same row, no gap) is the actual no-space-before-the-paren check a real
        # call site passes and a parenthetical aside in prose does not.
        is_call = (nxt is not None and nxt.type == tokenize.OP and nxt.string == "("
                  and tok.end == nxt.start)
        prev = toks[i - 1] if i > 0 else None
        is_def = (prev is not None and prev.type == tokenize.NAME
                 and prev.string in _DEF_KEYWORDS)
        if is_call or is_def or _is_leak_shaped(name):
            names.add(name)
    return sorted({n for n in names if len(n) >= 3 and n not in _CODE_STOPWORDS})


def _code_names_fallback(code: str) -> list[str]:
    """`_code_names` when `code` does not tokenize as Python at all. Cannot tell a
    string literal from bare code (no lexer ran), so it is strictly weaker than the
    tokenized path — kept only so an unparseable mutant still contributes SOME
    per-mutant terms instead of silently contributing none.
    """
    code = _ESCAPE_SEQ.sub(" ", code)
    names = set(_DEF_NAME.findall(code)) | set(_CALL_NAME.findall(code))
    for name in _IDENT.findall(code):
        if _is_leak_shaped(name):
            names.add(name)
    return sorted({n for n in names if len(n) >= 3 and n not in _CODE_STOPWORDS})


def _mutation_output(mutant: dict) -> dict | None:
    """A pre-parsed `mutation_output` wins over the raw string (§2.5: both accepted,
    the raw string parses HERE, never at the sender). None on anything that is not a
    well-formed object carrying at least `archetype` and `changes` — the caller counts
    that in `rows_unparsed` (§1.3, §9 p.9) and moves on; it never raises and never
    stops the batch."""
    obj = mutant.get("mutation_output")
    if obj is None:
        raw = mutant.get("mutation_output_raw")
        if not raw or not str(raw).strip():
            return None
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
    return obj if isinstance(obj, dict) and all(k in obj for k in _MO_REQUIRED) else None


def leak_terms(source: dict) -> tuple[str, ...]:
    """Forbidden terms `generalize.leakage` must additionally check for a `run` source
    (§2.2.1 p.9): the program id, the run id, the task, the mutation model, and the
    function/class names of the mutant's own code. Recomputed from `source["run_meta"]`
    alone — `run.py:_report` (run.py:432-433) redoes the leakage count from staging
    rows long after the conversion's payload is gone, so nothing lives only in memory.
    Terms shorter than 3 characters are dropped: a one- or two-letter term matches
    everywhere and would make the check fire on every row.
    """
    meta = source.get("run_meta") or {}
    terms = {str(meta.get("program_id") or ""), str(meta.get("run") or ""),
             str(meta.get("task") or ""), str(meta.get("mutation_model") or "")}
    terms.update(str(t) for t in (meta.get("mutant_code_names") or ()))
    return tuple(sorted(t for t in terms if len(t) >= 3))


# --------------------------------------------------------------------------- ingest

def payload_from_csv(path, run_id: str | None = None) -> dict:
    """One `evolution_full.csv` -> the same body `POST /run` takes (§2.5).

    `mutation_output_raw` carries the CSV cell verbatim, unparsed — parsing happens
    once, inside `from_payload`, so the CLI and the HTTP push share one code path
    instead of the CLI silently being "the real" converter and the handler a copy.
    `parent_fitness` is left `None` for every mutant: `from_payload` always joins it
    from the batch when absent (§1.3), so a payload built here and one posted by a
    caller who never computed it behave identically.
    """
    path = Path(path)
    run_id = run_id or _derive_run_id(path)
    task_id = _default_task_id(run_id)
    mutants = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            mutants.append({
                "program_id": row["program_id"],
                "parent_ids": json.loads(row.get("parent_ids") or "[]"),
                "state": row.get("state", ""),
                "generation": _parse_int(row.get("generation")),
                "iteration": _parse_int(row.get("iteration")),
                "fitness": _parse_fitness(row.get("metric_fitness")),
                "parent_fitness": None,
                "mutation_model": row.get("metadata_mutation_model", ""),
                "mutation_output_raw": row.get("metadata_mutation_output", ""),
            })
    return {"run_id": run_id, "task_id": task_id, "mutants": mutants}


def from_payload(payload: dict, staging_path=None, *, limit: int | None = None,
                  min_abs_delta: float = 0.0) -> dict:
    """The converter (§2.5, C4 push form). One `Source` per mutant with a computable
    delta (§2.1), one `Thesis` per `changes[i]` (§2.2), through the same two steps a
    paper's draft goes through — `generalize.generalize` then one batched `embed_docs`
    call (§2.2.1) — so the staging row is the same six-key shape phase 2 already knows
    how to drain.

    Returns a report dict, not a count (the spec's own `-> int` cannot carry four
    different drop reasons plus two truncations as separate numbers, §9 p.3, §12) —
    every one of `dropped_dead`, `dropped_no_fitness`, `dropped_root`,
    `dropped_parent_unmeasured`, `dropped_min_delta` and `rows_unparsed` is its own key,
    on purpose: summed into one "skipped", they hide which failure mode is which.
    """
    from . import generalize as generalize_mod
    from .. import embed  # local: keeps sentence-transformers off module import

    run_id = payload.get("run_id")
    if not run_id:
        raise ValueError("runlog: payload has no run_id")
    mutants = payload.get("mutants")
    if not mutants:
        raise ValueError("runlog: payload has no mutants")
    task_id = payload.get("task_id") or _default_task_id(run_id)
    seed = _derive_seed(run_id)

    llm.assert_grammar_works(llm.QWEN_9B)       # generalize runs once per changes[i]

    # Own fitness of every mutant in the batch, keyed by program_id — the join a
    # parent's fitness needs happens inside this same file/batch, never across runs
    # (§1.3). Built once, up front: a mutant can be referenced as a parent before its
    # own row is visited, csv order is not topological.
    fitness_map = {m.get("program_id"): _coerce_fitness(m.get("fitness")) for m in mutants}

    mutants_total = rows_unparsed = 0
    dropped_dead = dropped_no_fitness = dropped_root = 0
    dropped_parent_unmeasured = mutants_no_changes = 0
    candidates: list[dict] = []

    for mutant in mutants:
        mutants_total += 1
        own_fitness = fitness_map.get(mutant.get("program_id"))
        if own_fitness is None:
            dropped_no_fitness += 1          # §1.3: 12 rows, mostly state=running
            continue
        if own_fitness == DEAD_FITNESS:
            dropped_dead += 1                # §1.3: 111 rows, validation timeout
            continue
        parent_ids = mutant.get("parent_ids") or []
        if not parent_ids:
            dropped_root += 1                # §1.3: 3 rows, nothing to compare against
            continue

        given_pf = mutant.get("parent_fitness")
        if given_pf is not None:
            given_pf = float(given_pf)
            parent_fitness = given_pf if given_pf != DEAD_FITNESS else None
        else:
            # Several parents (crossover) -> the MAXIMUM parent fitness: "got better"
            # only means something against the best of what it was made from. This
            # branch is unverified by the three ground-truth files — lineage_num_parents
            # is 1 everywhere in them (§1.3, open question §13.3).
            valid = [f for f in (fitness_map.get(pid) for pid in parent_ids)
                     if f is not None and f != DEAD_FITNESS]
            parent_fitness = max(valid) if valid else None
        if parent_fitness is None:
            dropped_parent_unmeasured += 1
            continue

        mo = _mutation_output(mutant)
        if mo is None:
            rows_unparsed += 1               # §1.3: empty or broken JSON, named, counted
            continue
        changes = mo.get("changes") or []
        if not changes:
            mutants_no_changes += 1          # §9 p.9: parses fine, nothing to say
            continue

        candidates.append({"mutant": mutant, "mo": mo, "changes": changes,
                           "own_fitness": own_fitness, "parent_fitness": parent_fitness,
                           "delta": own_fitness - parent_fitness,
                           "parent_ids": parent_ids})

    kept, dropped_min_delta = [], 0
    for cand in candidates:
        if abs(cand["delta"]) >= min_abs_delta:
            kept.append(cand)
        else:
            dropped_min_delta += 1
    # Descending |delta|: informative mutants first, an interrupted load still leaves
    # a meaningful slice (§2.4).
    kept.sort(key=lambda c: abs(c["delta"]), reverse=True)
    selected = kept if limit is None else kept[:limit]

    drafts: list[DraftThesis] = []
    row_meta: list[dict] = []
    for cand in selected:
        mutant, mo, delta = cand["mutant"], cand["mo"], cand["delta"]
        program_id = mutant["program_id"]
        archetype = mo.get("archetype", "")
        mutation_model = mutant.get("mutation_model", "")
        url = f"gigaevo://{run_id}/{program_id}"
        # No fitness in the title (§2.1 revision): `write_source` is INSERT OR REPLACE
        # and POST /sources answers 409 on a changed title, so a re-fetch of the same
        # mutant must not change what the title says.
        title = f"{archetype} · {run_id}/{program_id}"
        run_success = delta > 0
        run_meta = {
            "run": run_id, "seed": seed, "program_id": program_id,
            "parent_ids": cand["parent_ids"], "generation": mutant.get("generation"),
            "iteration": mutant.get("iteration"), "state": mutant.get("state", ""),
            "fitness": cand["own_fitness"], "fitness_delta": delta,
            "parent_fitness": cand["parent_fitness"], "archetype": archetype,
            "justification": mo.get("justification", ""),
            "insights_used": mo.get("insights_used", []),
            "mutation_model": mutation_model, "task": task_id,
            "mutant_code_names": _code_names(mo.get("code", "")),
        }
        source = Source(id=make_source_id(url, program_id), url=url, title=title,
                        type="run", version=program_id, retrieved_at=_now_iso(),
                        run_success=run_success, run_meta=run_meta)
        source_dict = source.model_dump()
        context = f"{archetype} · {task_id} · {mutation_model}"
        effect = f"fitness {cand['parent_fitness']} -> {cand['own_fitness']} ({delta:+.2f})"
        terms = leak_terms(source_dict)
        for i, change in enumerate(cand["changes"]):
            text = change.get("description", "")
            locator = f"{run_id}:{program_id}#changes[{i}]"
            # `explanation` ("why this helped ... could transfer to future mutations",
            # mutation.py:46-51) is the closest thing to an applicability hint the log
            # carries; there is no limitations signal in this schema at all, so that
            # draft field is left for the model to fill rather than guessed at.
            draft = DraftThesis(text=text, context=context, effect=effect, locator=locator,
                                draft_text=text, draft_applicability=change.get("explanation", ""),
                                draft_limitations="")
            drafts.append(draft)
            row_meta.append({"source": source_dict, "section_id": f"changes[{i}]",
                             "extra_terms": terms})

    # generalize is one call per changes[i] (§2.2.1); embed is ONE call for the whole
    # batch, over the pre-generalization thesis text — same split as run.py's
    # `_one_source` (run.py:104-107).
    ideas = [generalize_mod.generalize(draft, prompt="run") for draft in drafts]
    vectors = embed.embed_docs([draft.text for draft in drafts])

    lines = []
    leaks = 0
    for meta, draft, fields, vector in zip(row_meta, drafts, ideas, vectors):
        # BLOCKER 1: counted, not discarded. The run-specific term list
        # (§2.2.1, `leak_terms`) only means anything if the count it produces
        # reaches a report — a call whose result nobody reads is the same as
        # not calling it at all, and `run.py:_report`'s phase-2 recount used
        # to redo this with no `extra_terms`, i.e. the paper-shaped check
        # (§9 p.9), which means nothing on a log.
        if generalize_mod.leakage(draft, fields, extra_terms=meta["extra_terms"]):
            leaks += 1
        lines.append({
            "source": meta["source"], "section_id": meta["section_id"],
            "thesis": {"text": draft.text, "context": draft.context, "effect": draft.effect,
                       "locator": draft.locator, "text_hash": text_hash(draft.text)},
            "draft": {"draft_text": draft.draft_text,
                      "draft_applicability": draft.draft_applicability,
                      "draft_limitations": draft.draft_limitations},
            "idea_fields": fields.model_dump(),
            "vector": [float(x) for x in vector],
        })

    if not lines:
        # MAJOR 3 (§2.5, §9 p.3): "задание failed с причиной, а не ok с нулями" —
        # the sibling path (`run.py:stage_one`) already raises rather than
        # write an empty staging file, and this converter must too. Two
        # distinct empty-batch shapes get two distinct messages: an operator
        # who sees "everything died before measurement" and one who sees
        # "everything measured but said nothing" need to fix different things.
        passed_gate = (mutants_total - dropped_dead - dropped_no_fitness
                       - dropped_root - dropped_parent_unmeasured)
        if passed_gate <= 0:
            raise ValueError(
                f"runlog {run_id}: every one of {mutants_total} mutant(s) was dropped "
                "before measurement (dead validation, no fitness, root, or an "
                "unmeasured parent) — none had a computable delta to read changes[] "
                "from, nothing to ingest")
        if not candidates:
            raise ValueError(
                f"runlog {run_id}: {passed_gate} mutant(s) had a computable delta but "
                f"none carried a changes[] — {rows_unparsed} unparsed, "
                f"{mutants_no_changes} parsed with an empty changes[], nothing to ingest")
        raise ValueError(
            f"runlog {run_id}: {len(candidates)} mutant(s) had changes[] to convert but "
            f"min_abs_delta={min_abs_delta} and limit={limit} left none selected "
            f"({dropped_min_delta} dropped by min_abs_delta), nothing to ingest")

    if staging_path is not None:
        staging_path = Path(staging_path)
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        with staging_path.open("w", encoding="utf-8") as fh:
            for row in lines:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    return {
        "run_sources": len(selected), "run_theses": len(lines), "staging_lines": len(lines),
        "leakage": leaks, "rows_unparsed": rows_unparsed, "mutants_total": mutants_total,
        "mutants_converted": len(selected), "mutants_no_changes": mutants_no_changes,
        "dropped_dead": dropped_dead, "dropped_no_fitness": dropped_no_fitness,
        "dropped_root": dropped_root, "dropped_parent_unmeasured": dropped_parent_unmeasured,
        "dropped_min_delta": dropped_min_delta,
        # -1 is the codebase's existing "no value" sentinel for a bounded int (LINK_SCHEMA's
        # -1 == "no duplicate", models.py:204) — `None` cannot ride in a report where every
        # key is a number (§9 p.3), and echoing `len(kept)` here would make `limit` read the
        # same whether the caller asked for one or asked for none, hiding the one case the
        # key exists to show.
        "limit": limit if limit is not None else -1,
        "min_abs_delta": min_abs_delta,
    }


def from_csv(path, *, limit: int | None = None, min_abs_delta: float = 0.0,
             staging_path=None, run_id: str | None = None) -> dict:
    """CSV -> staging rows of exactly the phase-1 format. Returns the report dict, not
    the row count the spec first sketched (`-> int`): §9 p.3 and §12 ask that every
    drop be its own number, which an int cannot carry.
    """
    payload = payload_from_csv(path, run_id=run_id)
    return from_payload(payload, staging_path, limit=limit, min_abs_delta=min_abs_delta)


def drain_run(staging_path, staged: dict | None = None) -> dict:
    """Phase 2 for one evolution-log batch. Modeled on `run.drain_one` (§2.10), with
    the store guard widened from the batch's first source to the whole batch: a run
    staging file holds MANY sources — one per mutant (§2.1) — and `drain_one`'s guard
    checks only `rows[0]`'s source id, which would raise on a batch whose FIRST mutant
    the arbiter fully refused even though later mutants in the same file landed leaves.
    Here: if not a single source id in the file has a leaf, raise; otherwise report.

    Both of `drain_one`'s guards are kept, in the same order, for the same reason —
    they catch two different lies and neither sees the other's (run.py:406-424) — and
    the actual work is `run.phase2`, not a reimplementation of it.

    MINOR 6 (§9 p.12): the aggregate `any()` guard alone would read "1 of 182 sources
    landed a leaf, 181 were silently refused" the same as a clean run — both raise
    nothing. The report carries `sources_total`/`sources_with_leaves` so that shape is
    a number an operator can see, on top of (not instead of) the guard.
    """
    from . import run as run_mod

    staging_path = Path(staging_path)
    staged = staged or {}
    name = staged.get("run_id") or staging_path.stem
    rows = run_mod._read_staging(staging_path)
    if not rows:
        raise RuntimeError(f"{name}: {staging_path} holds no staging lines, so there is "
                           "nothing for phase 2 to ingest; convert the log again")
    source_ids = {row["source"]["id"] for _, row in rows}
    lines = staged.get("staging_lines") or len(rows)
    report = run_mod.phase2(staging_path)

    if report["theses_written"] == 0 and report["theses_refused"]:
        raise RuntimeError(
            f"{name}: the linking arbiter refused all {report['theses_refused']} of "
            f"{lines} theses, every one of them is queued in pending_link and nothing "
            "reached the graph; re-run once the 35B server answers again")
    leafy = {sid for sid in source_ids if graph_client.count_theses(source_id=sid)}
    if not leafy:
        raise RuntimeError(
            f"{name}: phase 2 left nothing in the graph for any of the "
            f"{len(source_ids)} mutants in this batch — of {lines} staging lines, "
            f"{report['theses_written']} were written, {report['theses_refused']} "
            f"refused and {report['theses_skipped']} skipped, and the store holds no "
            "leaf for a single one of them. Anything refused is queued in pending_link; "
            "re-run once the 35B server answers again")

    staging_path.unlink(missing_ok=True)
    run_mod._cursor_path(staging_path).unlink(missing_ok=True)
    carry = {k: staged[k] for k in (
        "mutants_total", "rows_unparsed", "dropped_dead", "dropped_no_fitness",
        "dropped_root", "dropped_parent_unmeasured", "dropped_min_delta",
        "mutants_no_changes", "limit", "min_abs_delta") if k in staged}
    return {**report, "staging_lines": lines, "sources_total": len(source_ids),
            "sources_with_leaves": len(leafy), **carry}


# ------------------------------------------------------------------------------- cli

def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="python3 -m lake.ingest.runlog")
    parser.add_argument("csv_path")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--min-abs-delta", type=float, default=0.0)
    parser.add_argument("--dry-run", action="store_true",
                        help="report only, write no staging file")
    args = parser.parse_args(argv)

    path = Path(args.csv_path)
    run_id = _derive_run_id(path)
    staging_path = None if args.dry_run else RUN_DIR / f"{run_id}.jsonl"
    report = from_csv(path, limit=args.limit, min_abs_delta=args.min_abs_delta,
                      staging_path=staging_path, run_id=run_id)
    for key, value in report.items():
        print(f"{key}: {value}")


# ------------------------------------------------------------------------- self-check

if __name__ == "__main__":
    import sys
    import tempfile
    import types

    import numpy as np

    from ..models import EMBED_DIM

    # `generalize()` (called through `from_csv` below) is `@trace`d (ingest/generalize.py:39),
    # and `trace._write` mkdirs and appends to the module-global `trace.TRACES_DIR` — left
    # unbound, every `from_csv` call in this self-check (the synthetic CSV below and the
    # optional real ground-truth CSVs further down) wrote its trace rows into the real
    # `data/traces/<pid-random>.jsonl` (found: a plain self-check run left ~900 lines
    # behind). One process, one run id, so one bind for the whole script is enough — there
    # is no next check after this to leave dirty.
    trace.TRACES_DIR = Path(tempfile.mkdtemp(prefix="lake-runlog-selfcheck-"))

    real_complete, real_canary = llm.complete, llm.assert_grammar_works

    def fake_embed_docs(texts):
        out = np.zeros((len(texts), EMBED_DIM), dtype=np.float32)
        for i, text in enumerate(texts):
            vec = np.random.default_rng(int(text_hash(text)[:8], 16)).standard_normal(EMBED_DIM)
            out[i] = (vec / np.linalg.norm(vec)).astype(np.float32)
        return out

    fake_embed = types.ModuleType("lake.embed")
    fake_embed.embed_docs = fake_embed_docs
    sys.modules["lake.embed"] = fake_embed

    def fake_complete(prompt, *, system, schema, op, max_tokens, timeout, model=None,
                       temperature=0.0):
        assert op == "generalize", op
        assert "THESIS" in prompt, prompt
        lever = prompt.split("THESIS\n", 1)[1].splitlines()[0]
        return {"text": f"generalized: {lever}", "applicability_conditions": "ac",
                "limitations": "lim", "failure_modes": []}

    llm.complete = fake_complete
    llm.assert_grammar_works = lambda model: None

    def row(program_id, parent_ids, fitness, mutation_output, state="discarded",
            generation="1", iteration="1", mutation_model="qwen3.6-35b-a3b"):
        return {"program_id": program_id, "name": program_id, "code": "", "created_at": "",
                "atomic_counter": "0", "state": state, "is_complete": "True",
                "generation": generation, "iteration": iteration,
                "is_root": "True" if not parent_ids else "False",
                "parent_ids": json.dumps(parent_ids), "children_ids": "[]",
                "metric_fitness": fitness, "metric_avg_extraction_failures": "0",
                "metric_is_valid": "1", "lineage_num_parents": str(len(parent_ids)),
                "lineage_num_children": "0", "lineage_mutation": "", "lineage_generation": "1",
                "metadata_mutation_output": mutation_output, "metadata_mutation_model": mutation_model,
                "metadata_memory_used": "False", "metadata_mutation_context": "",
                "metadata_home_island": "", "metadata_current_island": "",
                "metadata_intra_memory_card": "", "metadata_intra_memory_signal": "",
                "metadata_source": "", "metadata_strategy_name": "", "metadata_file_path": ""}

    def mo(archetype="Precision Optimization", changes=None, code="def helper_fn(): pass"):
        return json.dumps({"archetype": archetype, "justification": "because",
                           "insights_used": ["[tag] some insight"],
                           "changes": changes if changes is not None else
                           [{"description": "cascade a cheap check first",
                             "explanation": "cuts wasted expensive calls"}],
                           "code": code})

    fieldnames = list(row("x", [], "0", mo()).keys())

    def _title_leaks_fitness(source: dict) -> bool:
        """MAJOR 4 (§2.1 revision): the hazard is the VALUE, not the word
        "fitness" — checked as both a bare `str()` and a signed 2-decimal
        form, since either is how a number would actually show up spliced
        into an f-string title."""
        meta = source["run_meta"]
        title = source["title"]
        return any(str(v) in title or f"{v:+.2f}" in title
                   for v in (meta["fitness"], meta["parent_fitness"], meta["fitness_delta"]))

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        csv_path = tmp / "evolution_full.csv"
        rows = [
            row("dead1", ["root1"], "-1000.0", mo()),                       # 1: dropped_dead
            row("nofit1", ["root1"], "", mo(), state="running"),            # 2: dropped_no_fitness
            row("root1", [], "0.4", mo(), state="done"),                    # 3: dropped_root
            row("broken1", ["root1"], "0.6", "{not json"),                  # 4: rows_unparsed
            row("nochg1", ["root1"], "0.5", mo(changes=[])),                # 5: mutants_no_changes
            row("normal1", ["root1"], "0.7", mo(changes=[
                {"description": "prefilter with a cheap proxy", "explanation": "saves calls"},
                {"description": "reorder validation steps", "explanation": "fails fast"}])),
        ]
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        staging_path = tmp / "run" / "s1.jsonl"
        report = from_csv(csv_path, staging_path=staging_path, run_id="unit_seed9")

        # -- counts and drop reasons, each in its own counter --------------------
        assert report["mutants_total"] == 6, report
        assert report["dropped_dead"] == 1, report
        assert report["dropped_no_fitness"] == 1, report
        assert report["dropped_root"] == 1, report
        assert report["rows_unparsed"] == 1, report
        assert report["mutants_no_changes"] == 1, report
        assert report["mutants_converted"] == 1, report
        assert report["run_sources"] == 1, report
        assert report["run_theses"] == 2, "root1 -> 0.7, delta +0.30, 2 changes"
        assert report["staging_lines"] == 2, report
        assert report["dropped_parent_unmeasured"] == 0, report
        assert report["dropped_min_delta"] == 0, report
        assert report["limit"] == -1, "no limit passed -> sentinel, not None"
        assert report["min_abs_delta"] == 0.0, report
        assert report["leakage"] == 0, report      # neither of the two clean leaves leaks

        # -- effect carries no number where there is no measurement, and the
        #    normal row's effect DOES carry the fitness pair --------------------
        lines = [json.loads(ln) for ln in staging_path.read_text(encoding="utf-8").splitlines()]
        assert len(lines) == 2, lines
        for r in lines:
            assert set(r) == {"source", "section_id", "thesis", "draft", "idea_fields",
                              "vector"}, sorted(r)
            assert set(r["source"]) == {"id", "url", "title", "type", "version",
                                        "retrieved_at", "run_success", "run_meta"}
            assert set(r["thesis"]) == {"text", "context", "effect", "locator", "text_hash"}
            assert set(r["draft"]) == {"draft_text", "draft_applicability", "draft_limitations"}
            assert set(r["idea_fields"]) == {"text", "applicability_conditions",
                                             "limitations", "failure_modes"}
            assert len(r["vector"]) == EMBED_DIM, len(r["vector"])
            assert r["thesis"]["text_hash"] == text_hash(r["thesis"]["text"])
            assert "0.7" in r["thesis"]["effect"] and "0.4" in r["thesis"]["effect"]
            # MAJOR 4: the hazard (§2.1 revision) is the fitness VALUE riding in the
            # title, not the literal word "fitness" — a title built as an f-string
            # with the number spliced in carries no such word and would sail past
            # `"fitness" not in title`. `_title_leaks_fitness` checks every number
            # `run_meta` actually holds, and both this real check below and the
            # "proof" right after it share the one function, so weakening either
            # back to a literal-word check cannot hide from the other.
            assert not _title_leaks_fitness(r["source"]), r["source"]
            assert r["source"]["run_success"] is True, r["source"]  # delta +0.30 > 0

        # -- prove the check above is not vacuous: a title that DOES carry the
        #    fitness value must make `_title_leaks_fitness` say so (this is what
        #    the old literal-"fitness" assertion missed) -----------------------
        leaky_source = dict(lines[0]["source"])
        leaky_meta = leaky_source["run_meta"]
        leaky_source["title"] = f"{leaky_source['title']} · {leaky_meta['fitness']:+.2f}"
        assert "fitness" not in leaky_source["title"], \
            "the old (wrong) check would call this title clean"
        assert _title_leaks_fitness(leaky_source), \
            "a title carrying the fitness value must be caught"

        # -- locator is reversible to the CSV row (program_id + changes index) ---
        for i, r in enumerate(lines):
            run_part, rest = r["thesis"]["locator"].split(":", 1)
            program_id, idx = rest.split("#changes[")
            assert run_part == "unit_seed9" and program_id == "normal1", r
            assert idx == f"{i}]", r

        # -- lines of one mutant are contiguous (trivially true here: one mutant) -
        assert {r["source"]["id"] for r in lines} == {lines[0]["source"]["id"]}

        # -- a broken/unparsed row does not kill the run; mutants_total counts it -
        assert report["mutants_total"] == 6 and report["rows_unparsed"] == 1

        # -- leak_terms recomputes from the staging row's run_meta alone ---------
        terms = leak_terms(lines[0]["source"])
        assert "helper_fn" in terms, terms         # `def helper_fn` in the mutant's code
        assert "normal1" in terms, terms           # program_id
        assert "unit_seed9" in terms, terms        # run id

        # -- those terms are actually usable by generalize.leakage -------------
        from .generalize import leakage as real_leakage
        from ..models import IdeaFields
        leaky = IdeaFields(text="the fix calls helper_fn during validation",
                           applicability_conditions="", limitations="", failure_modes=[])
        clean = IdeaFields(text="a cheap check runs before the expensive one",
                           applicability_conditions="", limitations="", failure_modes=[])
        probe = DraftThesis(text="t", context="c", effect="e", locator="l",
                            draft_text="d", draft_applicability="a", draft_limitations="l")
        assert real_leakage(probe, leaky, extra_terms=terms), "extra_terms must catch it"
        assert real_leakage(probe, leaky) == [], "the plain paper check must miss a snake_case name"
        assert real_leakage(probe, clean, extra_terms=terms) == []

        # -- MINOR 5: `_code_names` catches a name the mutant only CALLS, not just
        #    one it defines, and does not fire on ordinary code shape or prose ----
        assert "process_batch" in _code_names("result = process_batch(x, y)"), \
            "a call site with no matching `def` in this diff must still be caught"
        assert "MultiModelRouter" in _code_names("router: MultiModelRouter = build()"), \
            "a PascalCase reference with no call and no def must still be caught"
        assert "print" not in _code_names("print('done')"), \
            "a stopword builtin call must not fire constantly"
        assert _code_names("while (score > 3): return") == [], \
            "a keyword followed by '(' must not read as a call-site name"
        assert _code_names("for the cheap check to run") == [], \
            "ordinary English prose must not read as leaked code identifiers"

        # -- BLOCKER 1: the converter's own leakage count is not thrown away —
        #    a generalized text that echoes an extra_term is counted as a leak,
        #    and the count is the one the report actually carries -------------
        leaky_payload = {"run_id": "leak_seed1", "task_id": "t", "mutants": [
            {"program_id": "leaky1", "parent_ids": ["r1"], "state": "done",
             "generation": 1, "iteration": 1, "fitness": 0.9, "parent_fitness": 0.5,
             "mutation_model": "m",
             "mutation_output_raw": mo(changes=[
                 {"description": "cache helper_fn output", "explanation": "e"}])},
        ]}
        leaky_report = from_payload(leaky_payload, staging_path=None)
        assert leaky_report["leakage"] == 1, leaky_report

        # -- ordering by descending |delta|, and limit/min_abs_delta both report -
        multi_rows = [
            row("root2", [], "0.4", mo(), state="done"),
            row("big2", ["root2"], "0.9", mo(changes=[{"description": "d1", "explanation": "e1"}])),
            row("small2", ["root2"], "0.45", mo(changes=[{"description": "d2", "explanation": "e2"}])),
        ]
        csv2 = tmp / "evolution_full2.csv"
        with csv2.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(multi_rows)
        payload2 = payload_from_csv(csv2, run_id="order_test")
        report2 = from_payload(payload2, staging_path=None, min_abs_delta=0.1)
        assert report2["mutants_converted"] == 1, report2   # small2's delta 0.05 < 0.1
        assert report2["dropped_min_delta"] == 1, report2
        assert report2["min_abs_delta"] == 0.1, report2

        report3 = from_payload(payload2, staging_path=None)
        assert report3["mutants_converted"] == 2, report3
        assert report3["limit"] == -1, report3
        report4 = from_payload(payload2, staging_path=None, limit=1)
        assert report4["mutants_converted"] == 1, report4
        assert report4["limit"] == 1, report4

        # -- from_csv actually FORWARDS limit/min_abs_delta to from_payload, through
        #    the OTHER entry point than report2/3/4 above (§2.5 module==HTTP parity:
        #    the CLI's own two flags must not be dead keywords that from_csv accepts
        #    and never passes on). Reproduced without the fix: deleting the
        #    `limit=limit, min_abs_delta=min_abs_delta` forwarding in `from_csv`
        #    (falling back to `from_payload`'s defaults) leaves every number below
        #    unchanged from the unfiltered case and both asserts fail. -------------
        report5 = from_csv(csv2, staging_path=None, run_id="order_test", min_abs_delta=0.1)
        assert report5["mutants_converted"] == 1, \
            "from_csv did not forward min_abs_delta to from_payload"
        assert report5["dropped_min_delta"] == 1, report5
        assert report5["min_abs_delta"] == 0.1, report5
        report6 = from_csv(csv2, staging_path=None, run_id="order_test", limit=1)
        assert report6["mutants_converted"] == 1, \
            "from_csv did not forward limit to from_payload"
        assert report6["limit"] == 1, report6
        print("ok: from_csv forwards limit and min_abs_delta to from_payload (both "
              "the CLI's own flags, `runlog.main`, and the module-level entry point "
              "a future non-HTTP caller would use)")

        # -- MAJOR 3: an empty batch fails with a reason, not `ok` with zeros,
        #    and the two empty-batch shapes get two distinguishable messages --
        payload_measure = {"run_id": "empty_measure", "task_id": "t", "mutants": [
            {"program_id": "d1", "parent_ids": ["r1"], "state": "discarded",
             "generation": 1, "iteration": 1, "fitness": DEAD_FITNESS,
             "parent_fitness": None, "mutation_model": "m", "mutation_output_raw": mo()},
        ]}
        try:
            from_payload(payload_measure, staging_path=None)
        except ValueError as exc:
            assert "dropped before measurement" in str(exc), exc
        else:
            raise AssertionError("a batch with nothing past measurement must raise")

        payload_no_changes = {"run_id": "empty_changes", "task_id": "t", "mutants": [
            {"program_id": "n1", "parent_ids": ["r1"], "state": "done",
             "generation": 1, "iteration": 1, "fitness": 0.7, "parent_fitness": 0.4,
             "mutation_model": "m", "mutation_output_raw": mo(changes=[])},
        ]}
        try:
            from_payload(payload_no_changes, staging_path=None)
        except ValueError as exc:
            assert "carried a changes" in str(exc), exc
            assert "dropped before measurement" not in str(exc), \
                "the two empty-batch causes must not share one message"
        else:
            raise AssertionError("a batch that parsed but carried no changes[] must raise")
        never_written = tmp / "run" / "never-written.jsonl"
        assert not never_written.exists(), "an empty batch must write no staging file"
        print("ok: an empty batch raises with a reason naming which way it came up empty")

        # -- BLOCKER 2: drain_run's whole-batch store guard, its asymmetry vs
        #    `drain_one`, the refusal guard, and staging surviving a failure --
        from . import run as run_mod

        real_count_theses = graph_client.count_theses
        real_phase2 = run_mod.phase2
        leaf_counts: dict[str, int] = {}

        def fake_count_theses(idea_id=None, source_id=None):
            return leaf_counts.get(source_id, 0)

        def make_fake_phase2(written, refused, skipped=0):
            def fake_phase2(staging_path, limit=None):
                return {"theses_written": written, "theses_refused": refused,
                        "theses_skipped": skipped}
            return fake_phase2

        def batch_file(path, source_ids):
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as fh:
                for sid in source_ids:
                    fh.write(json.dumps({"source": {"id": sid}, "section_id": "x",
                                         "thesis": {}, "draft": {}, "idea_fields": {},
                                         "vector": []}) + "\n")

        graph_client.count_theses = fake_count_theses
        try:
            # (c) the refusal guard fires on nothing-written/something-refused,
            # even while the store looks fine — it runs BEFORE the store check.
            leaf_counts = {"run-src-a": 5, "run-src-b": 5}
            run_mod.phase2 = make_fake_phase2(written=0, refused=3)
            p = tmp / "run" / "refused.jsonl"
            batch_file(p, ["run-src-a", "run-src-b"])
            cur = run_mod._cursor_path(p)
            cur.write_text("0\n", encoding="utf-8")
            try:
                drain_run(p, {"run_id": "refused"})
            except RuntimeError as exc:
                assert "refused all 3" in str(exc), exc
            else:
                raise AssertionError("an all-refused batch must raise, not report ok")
            # (d) a failure leaves the staging file and its cursor on disk
            assert p.exists() and cur.exists(), \
                "a failed drain_run must not delete its staging file or cursor"

            # (a) the whole-batch store guard: no source in the batch landed a
            # leaf, even though the counters alone (written=2) read clean —
            # `drain_one`'s rows[0]-only guard would not see this at all.
            leaf_counts = {}
            run_mod.phase2 = make_fake_phase2(written=2, refused=0)
            p2 = tmp / "run" / "nostore.jsonl"
            batch_file(p2, ["run-src-a", "run-src-b"])
            cur2 = run_mod._cursor_path(p2)
            cur2.write_text("0\n", encoding="utf-8")
            try:
                drain_run(p2, {"run_id": "nostore"})
            except RuntimeError as exc:
                assert "nothing in the graph for any" in str(exc), exc
            else:
                raise AssertionError("a batch with no leaf anywhere must raise")
            assert p2.exists() and cur2.exists(), \
                "a failed drain_run must not delete its staging file or cursor"

            # (b) the asymmetry drain_run exists for: the FIRST source in the
            # file has no leaf, a LATER one does — must NOT raise. Reusing
            # `drain_one`'s rows[0]-only check would raise on exactly this batch.
            leaf_counts = {"run-src-a": 0, "run-src-b": 1}
            run_mod.phase2 = make_fake_phase2(written=1, refused=0)
            p3 = tmp / "run" / "partial.jsonl"
            batch_file(p3, ["run-src-a", "run-src-b"])
            cur3 = run_mod._cursor_path(p3)
            cur3.write_text("0\n", encoding="utf-8")
            report_p3 = drain_run(p3, {"run_id": "partial"})
            # MINOR 6: the shape (1 of 2 sources landed a leaf) is a number, not
            # only a pass/fail — a 181-of-182 batch must not read as plain "ok".
            assert report_p3["sources_total"] == 2, report_p3
            assert report_p3["sources_with_leaves"] == 1, report_p3
            # success removes both the staging file and its cursor
            assert not p3.exists() and not cur3.exists(), \
                "a successful drain_run must remove its staging file and cursor"
        finally:
            graph_client.count_theses = real_count_theses
            run_mod.phase2 = real_phase2

        print("ok: drain_run — whole-batch store guard, its asymmetry vs "
              "drain_one, the refusal guard, staging survives a failure but "
              "not a success, per-source shape visible in numbers")

    llm.complete, llm.assert_grammar_works = real_complete, real_canary
    del sys.modules["lake.embed"]

    # -- optional: the three real ground-truth CSVs, if present on disk ----------
    real_dirs = sorted(Path("/Users/work/Code/gigaevo-core/outputs/aime_runs_2026-07-30/runs")
                       .glob("aime_seed*")) if Path(
        "/Users/work/Code/gigaevo-core/outputs/aime_runs_2026-07-30/runs").exists() else []
    if real_dirs:
        llm.complete, llm.assert_grammar_works = fake_complete, lambda model: None
        fake_embed = types.ModuleType("lake.embed")
        fake_embed.embed_docs = fake_embed_docs
        sys.modules["lake.embed"] = fake_embed
        totals = {"mutants_total": 0, "dropped_dead": 0, "dropped_no_fitness": 0,
                  "dropped_root": 0, "mutants_converted": 0, "run_theses": 0,
                  "rows_unparsed": 0}
        for seed_dir in real_dirs:
            csv_path = seed_dir / "results" / "evolution_full.csv"
            rep = from_csv(csv_path, staging_path=None)
            for key in totals:
                totals[key] += rep[key]
        llm.complete, llm.assert_grammar_works = real_complete, real_canary
        del sys.modules["lake.embed"]
        print(f"real data: {totals}")
        assert totals["mutants_total"] == 308, totals
        assert totals["dropped_dead"] == 111, totals
        assert totals["dropped_no_fitness"] == 12, totals
        assert totals["dropped_root"] == 3, totals
        assert totals["mutants_converted"] == 182, totals
        assert totals["run_theses"] == 647, totals
        # The 3 unparsed-JSON rows in these files ARE the 3 root rows (root has no
        # mutation to describe) — `dropped_root` intercepts them first, so this
        # converter's `rows_unparsed` is 0 here, not 3; the "3" from `13` §1.2 is the
        # same three physical rows counted on a different axis (parent_ids, not JSON).
        assert totals["rows_unparsed"] == 0, totals
        print("ok: reproduced 308/111/12/3/182/647 on the real ground-truth CSVs")

        # The CLI's own wiring, driven through `main` rather than around it. Every
        # other check calls `from_csv`/`from_payload` directly, so `main` forwarding
        # neither flag sat on no asserted path at all: `--limit 3` could have been a
        # no-op and every suite would still have been green. `--dry-run` keeps this
        # off the disk; the numbers come off stdout, which is the operator's only
        # view of a CLI run and therefore the thing worth pinning.
        import contextlib
        import io

        def cli(*flags) -> dict:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                main([str(real_dirs[0] / "results" / "evolution_full.csv"),
                      "--dry-run", *flags])
            printed = {}
            for line in out.getvalue().splitlines():
                key, _, value = line.partition(": ")
                printed[key] = value
            return printed

        llm.complete, llm.assert_grammar_works = fake_complete, (lambda model: None)
        sys.modules["lake.embed"] = fake_embed
        try:
            plain, limited = cli(), cli("--limit", "3")
            coarse = cli("--min-abs-delta", "0.2")
        finally:
            llm.complete, llm.assert_grammar_works = real_complete, real_canary
            del sys.modules["lake.embed"]

        assert plain["limit"] == "-1", plain            # the "not given" sentinel
        assert limited["limit"] == "3", limited
        assert int(limited["mutants_converted"]) == 3, limited
        assert int(limited["mutants_converted"]) < int(plain["mutants_converted"])
        assert coarse["min_abs_delta"] == "0.2", coarse
        assert int(coarse["dropped_min_delta"]) > 0, coarse
        assert int(coarse["mutants_converted"]) < int(plain["mutants_converted"]), coarse
        print("ok: the CLI forwards --limit and --min-abs-delta, and says so in its report")
    else:
        print("skip: real ground-truth CSVs not found on this machine")

    print("ok: runlog converter — drop reasons, staging shape, locator reversibility, "
          "leak_terms, ordering, limit/min_abs_delta all pass")
