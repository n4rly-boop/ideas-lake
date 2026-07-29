"""One run, one file: the 19 assertions of spec 10 §6 (`10-implementation-spec.md:638-664`),
the vault export of §11.6, which the spec asks for in the same shape, and the shaping
of the Neo4j load (C1, `07-roles-and-contracts.md:72`).

    python3 -m lake.selfcheck             # 6.1 talks to both school servers
    python3 -m lake.selfcheck --offline   # 21 of 22, no network, no key in the env

Only `assert`, no framework. Every check gets its own temporary directory and the
writers are pointed at it: the real `data/lake.db`, `data/index.db`,
`data/staging.jsonl`, `data/pending_link.jsonl`, `data/traces/` and `data/logs/`
are never opened for writing — they hold the results of real runs. The one thing
read from `data/` is the parse cache, and only as the §6.2 fixture.

Checks that already exist inside a module are CALLED, never copied: `index.demo`
(§6.12, §6.13), `search.demo`, `rank.demo` (§6.4), `hybrid_recipe.demo` (§6.3),
`vault.demo` (§11.6), `neo4j_load.demo` (C1).
The rest drive the real code — `run.phase2`, `link.link_batch`,
`rederive.maybe_rederive`, `api.retrieve` — with the LLM scripted and the encoder
replaced by seeded vectors (`python3 -m lake.embed` is what proves the encoder).
"""
import argparse
import ast
import contextlib
import functools
import importlib.util
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import traceback
import types
import uuid
from pathlib import Path

import numpy as np

from . import graph_client, index, llm, neo4j_load, stub_store, trace, vault
from .ingest import link, parse, rederive, run, split
from .models import (CACHE_DIR, EMBED_DIM, GENERALIZE_SCHEMA, PARSE_SCHEMA,
                     SCHEMA_BINDINGS, DraftThesis, Idea, Section, Source, Thesis,
                     model_field_names, new_idea_id, new_thesis_id, schema_properties,
                     source_id as make_source_id, text_hash)
from .retrieve import api, rank, rewrite, search

REPO = Path(__file__).resolve().parents[1]
CHECKS: list[tuple[int, str, object]] = []

# §6.9: every statement that updates the thesis table, and EVERY column it assigns.
#
# The table name is reached through anything that is not a statement separator, so
# `UPDATE OR REPLACE thesis`, `UPDATE main.thesis` and `UPDATE thesis AS t` all count.
# The SET list is captured whole and split into columns below — reading only the first
# assignment let `SET idea_id=?, text=?` through green, which is exactly how a guard
# narrowed to admit one statement stops holding the invariant it was narrowed for.
# `ON CONFLICT ... DO UPDATE SET` is a thesis rewrite with no `UPDATE thesis` in it, so
# it is matched separately.
# Deliberately greedy: prose that puts "update" within 40 characters of a bare "thesis"
# fails this check. It fails CLOSED, which is the right direction for a guard, and the
# phrase was already forbidden outright before this was narrowed.
_THESIS_UPDATE = re.compile(r"update\b[^;]{0,40}?\bthesis\b(?P<rest>[^;]{0,300})", re.IGNORECASE)
_UPSERT_UPDATE = re.compile(r"on\s+conflict\b[^;]{0,80}?do\s+update\s+set\b(?P<rest>[^;]{0,300})",
                            re.IGNORECASE)
_SET_COLUMN = re.compile(r"(\w+)\s*=")


def _thesis_update_columns(source: str) -> list[set[str]]:
    """One entry per statement that updates the thesis table: the columns it assigns.

    An `UPDATE` whose SET list this cannot parse yields an EMPTY set, which no caller
    accepts — an unreadable variant fails the check rather than slipping past it.
    """
    out = []
    for pattern in (_THESIS_UPDATE, _UPSERT_UPDATE):
        for match in pattern.finditer(source):
            if pattern is _UPSERT_UPDATE and "thesis" not in source[:match.start()][-200:].lower():
                continue        # an upsert on some other table
            head = re.split(r"\bwhere\b", match.group("rest"), maxsplit=1, flags=re.IGNORECASE)[0]
            out.append(set(_SET_COLUMN.findall(head)))
    return out


def check(number: int, what: str):
    """Register one §6 point. The text is what the ok-line prints."""
    def register(fn):
        CHECKS.append((number, what, fn))
        return fn
    return register


# --------------------------------------------------------------------- plumbing

@contextlib.contextmanager
def _swap(obj, name: str, value):
    """Temporarily replace `obj.name`. Restored even when the check fails."""
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield old
    finally:
        setattr(obj, name, old)


def _vec(text: str) -> list[float]:
    """Deterministic unit vector seeded by the text — the fake encoder of every check."""
    rng = np.random.default_rng(int(text_hash(text)[:8], 16))
    vec = rng.standard_normal(EMBED_DIM).astype(np.float32)
    return (vec / np.linalg.norm(vec)).tolist()


@contextlib.contextmanager
def _fake_embed():
    """`lake.embed` without sentence-transformers: seeded vectors, no model load.

    Installed where the lazy imports of `link._new_idea` and `rederive` look it up.
    """
    module = types.ModuleType(f"{__package__}.embed")
    module.embed_docs = lambda texts: (
        np.asarray([_vec(t) for t in texts], dtype=np.float32) if texts
        else np.zeros((0, EMBED_DIM), dtype=np.float32))
    module.embed_query = lambda text: np.asarray(_vec(text), dtype=np.float32)
    package = sys.modules[__package__]
    had = getattr(package, "embed", None)
    sys.modules[module.__name__] = module
    package.embed = module
    try:
        yield module
    finally:
        sys.modules.pop(module.__name__, None)
        if had is None:
            delattr(package, "embed")
        else:
            package.embed = had


def _open(tmp: Path) -> Path:
    """Point the store at `tmp` and hand back the index path. Returns tmp/index.db."""
    if stub_store._conn is not None:
        stub_store._conn.close()
    stub_store._conn = None
    stub_store._db_path = tmp / "lake.db"
    return tmp / "index.db"


def _cleanup() -> None:
    """Close every cached handle between checks: each check owns its own files."""
    for key in list(index._CONNS):
        index._CONNS.pop(key).close()
    index._MATS.clear()
    if stub_store._conn is not None:
        stub_store._conn.close()
        stub_store._conn = None


# ------------------------------------------------------------------- fixtures

SOURCES = {
    "s1": ("https://arxiv.org/abs/2405.00001", "Freezing Encoders", "paper"),
    "s2": ("https://arxiv.org/abs/2405.00002", "Cheap Proxies", "paper"),
    "s3": ("https://arxiv.org/abs/2405.00003", "A Replication", "paper"),
}


def _sid(tag: str) -> str:
    return make_source_id(SOURCES[tag][0], "v1")


def _row(tag: str, text: str, idea_text: str, effect: str = "+3.1 pp") -> dict:
    """One `staging.jsonl` line, shape fixed by §4.7 / the contract."""
    url, title, kind = SOURCES[tag]
    return {
        "source": {"id": _sid(tag), "url": url, "title": title, "type": kind,
                   "version": "v1", "retrieved_at": "2026-07-28T10:00:00Z",
                   "run_success": None, "run_meta": None},
        "section_id": "S3.2",
        "thesis": {"text": text, "context": "CIFAR-10, ResNet-18", "effect": effect,
                   "locator": "Table 4", "text_hash": text_hash(text)},
        "draft": {"draft_text": idea_text, "draft_applicability": "a",
                  "draft_limitations": "l"},
        "idea_fields": {"text": idea_text, "applicability_conditions": "a frozen encoder",
                        "limitations": "needs a pretrained encoder",
                        "failure_modes": ["encoder too weak -> semantics lost"]},
        "vector": _vec(text),
    }


FREEZE = "freeze the pretrained encoder before finetuning"
CORPUS = [
    _row("s1", "freezing the encoder before finetuning keeps 3.1 pp of accuracy", FREEZE),
    _row("s1", "keeping the encoder frozen during finetuning preserves accuracy", FREEZE),
    _row("s1", "island model with periodic migration keeps population diversity",
         "run isolated subpopulations and migrate between them"),
    _row("s2", "mixed precision training halves memory at equal accuracy",
         "train in mixed precision"),
    _row("s2", "a cheap proxy pre-filter drops most candidates before full evaluation",
         "filter candidates with a cheap proxy first"),
]
# Consumed in order by the scripted arbiter. The first thesis of the corpus has no
# candidates at all, so it costs no call: 4 answers for 5 theses (§4.5 step [1]).
CORPUS_ANSWERS = [0, -1, -1, -1]

REDERIVED = {"text": "REDERIVED: freeze the pretrained encoder",
             "applicability_conditions": "ac2", "limitations": "lim2",
             "failure_modes": ["fm2"], "effect_claimed": "+3.1 pp on paper leaves",
             "effect_observed": "+1.0 pp on run leaves"}


def _pick(marker: str):
    """Arbiter that links to whichever candidate carries `marker`.

    Candidate order depends on BM25 and on the seeded vectors; the intent under
    test does not, so the answer is read off the numbered prompt (§4.5).
    """
    def choose(prompt: str) -> int:
        for line in prompt.splitlines():
            if line[:1].isdigit() and marker in line:
                return int(line.split(".", 1)[0])
        raise AssertionError(f"candidate {marker!r} was never offered:\n{prompt}")
    return choose


def _arbiter(answers: list):
    """Scripted `llm.complete`: link answers in order, one canned re-derivation."""
    ops: list[str] = []

    def fake(prompt, *, system, schema, op, max_tokens, timeout,
             model=llm.QWEN_9B, temperature=0.0):
        ops.append(op)
        if op == "rederive":
            assert "LEAVES (" in prompt, prompt
            return dict(REDERIVED)
        assert op == "link", op
        assert model is llm.QWEN_35B, "the arbiter must run on 35B (§8)"
        assert (max_tokens, timeout, temperature) == (300, 60.0, 0.0), (max_tokens, timeout)
        assert "CANDIDATE IDEAS" in prompt, prompt
        reply = answers.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {"link_to": reply(prompt) if callable(reply) else reply}

    return fake, ops


def _phase2(tmp: Path, rows: list[dict], answers: list, *, limit: int | None = None):
    """The real `run.phase2` over `rows`, with every path inside `tmp`.

    `index.index_theses` and `link.link_batch` are bound to the temp index and the
    temp `pending_link.jsonl`; phase 2 calls both without a db argument, which in
    production is the point and here would open `data/`.
    """
    idx = tmp / "index.db"
    staging = tmp / "staging.jsonl"
    staging.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                       encoding="utf-8")
    fake, ops = _arbiter(list(answers))
    with contextlib.ExitStack() as stack:
        stack.enter_context(_swap(llm, "complete", fake))
        stack.enter_context(_swap(llm, "assert_grammar_works", lambda model: None))
        stack.enter_context(_swap(index, "index_theses",
                                  functools.partial(index.index_theses, db=idx)))
        # `run._reconcile_index` reaches for these two, also without a db argument.
        # Leaving them unbound wrote fixture rows into the real data/index.db and
        # only surfaced later, as a 503 from the divergence guard in rank.
        stack.enter_context(_swap(index, "has", functools.partial(index.has, db=idx)))
        stack.enter_context(_swap(index, "index_rows",
                                  functools.partial(index.index_rows, db=idx)))
        # `_reconcile_index`'s drift repair and `split.split_idea` reach for these two
        # the same way. `reconcile` unbound does not add rows to the real index, it
        # REBUILDS it from this fixture store.
        stack.enter_context(_swap(index, "stale_links",
                                  functools.partial(index.stale_links, db=idx)))
        stack.enter_context(_swap(index, "reconcile",
                                  functools.partial(index.reconcile, db=idx)))
        stack.enter_context(_swap(link, "link_batch",
                                  functools.partial(link.link_batch, index_db=idx,
                                                    pending_path=tmp / "pending_link.jsonl")))
        report = run.phase2(staging, limit=limit)
    return report, ops, staging


def _corpus(tmp: Path) -> tuple[Path, dict]:
    """Two sources through the real phase 2: 5 leaves, 4 ideas, temp store + index."""
    idx = _open(tmp)
    report, ops, _ = _phase2(tmp, CORPUS, CORPUS_ANSWERS)
    assert report["theses"] == 5 and report["ideas"] == 4, report
    assert ops == ["link"] * 4, ops
    assert report["ideas_without_leaves"] == 0 and report["pending_link"] == 0, report
    assert index.count(db=idx) == 5, index.count(db=idx)
    return idx, report


def _thesis(source: str, idea_id: str, text: str) -> Thesis:
    return Thesis(id=new_thesis_id(), source_id=_sid(source), idea_id=idea_id, text=text,
                  context="ctx", effect="+1 pp", locator="§1", text_hash=text_hash(text),
                  vector=_vec(text), created_at="2026-07-28T10:00:00Z")


def _write_source(tag: str) -> str:
    url, title, kind = SOURCES[tag]
    sid = _sid(tag)
    graph_client.write_source(Source(id=sid, url=url, title=title, type=kind, version="v1",
                                     retrieved_at="2026-07-28T10:00:00Z"))
    return sid


def _validate(value, schema: dict, path: str) -> None:
    """Assert `value` against one of the literal flat schemas of `models.py`.

    Not a general JSON-Schema implementation: exactly the keywords those schemas
    use — type, required, additionalProperties, maxItems, maxLength, const.
    """
    if "const" in schema:
        assert value == schema["const"], f"{path}: {value!r} != const {schema['const']!r}"
    kind = schema.get("type")
    if kind == "object":
        assert isinstance(value, dict), f"{path}: expected object, got {type(value).__name__}"
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            assert name in value, f"{path}: required property {name!r} is missing"
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            assert not extra, f"{path}: undeclared properties {extra}"
        for name, sub in properties.items():
            if name in value:
                _validate(value[name], sub, f"{path}.{name}")
    elif kind == "array":
        assert isinstance(value, list), f"{path}: expected array, got {type(value).__name__}"
        cap = schema.get("maxItems")
        assert cap is None or len(value) <= cap, f"{path}: {len(value)} items > maxItems {cap}"
        for i, item in enumerate(value):
            _validate(item, schema["items"], f"{path}[{i}]")
    elif kind == "string":
        assert isinstance(value, str), f"{path}: expected string, got {type(value).__name__}"
        cap = schema.get("maxLength")
        assert cap is None or len(value) <= cap, f"{path}: {len(value)} chars > maxLength {cap}"
    elif kind == "integer":
        assert isinstance(value, int) and not isinstance(value, bool), \
            f"{path}: expected integer, got {value!r}"


# ---------------------------------------------------------------- the 19 points

@check(1, "canary passes on 9B and on 35B: the grammar really forces the schema")
def check_01(tmp: Path) -> None:
    for model in (llm.QWEN_9B, llm.QWEN_35B):
        llm.assert_grammar_works(model)          # raises LLMError on a silent ignore


@check(2, "parser output on a cached section fixture validates against PARSE_SCHEMA")
def check_02(tmp: Path) -> str:
    # Read-only: this is the only place that looks at data/, and only as a fixture.
    cached = []
    for path in sorted(CACHE_DIR.glob("parse_*.json")):
        obj = json.loads(path.read_text(encoding="utf-8"))
        _validate(obj, PARSE_SCHEMA, path.name)
        # The real detector, not a copy: a string exactly at its ceiling is a cut
        # word that finish_reason="stop" hides (§3.1 p.7).
        llm._reject_truncated(obj, PARSE_SCHEMA, path.name)
        cached.append((path.name, obj))
    fixture = next((obj for _, obj in cached if obj["theses"]), None)
    origin = "data/cache" if fixture else "literal (no cached section with theses)"
    if fixture is None:
        fixture = {"theses": [{"text": "A cheap-model prefilter drops 70% of candidates.",
                               "context": "ImageNet with ResNet-50", "effect": "-70% compute",
                               "locator": "Methodology, 3.2",
                               "draft_text": "score candidates with a cheap proxy first",
                               "draft_applicability": "an expensive evaluator exists",
                               "draft_limitations": "the proxy must correlate"}]}
        _validate(fixture, PARSE_SCHEMA, "literal")

    # The real parse path over the fixture: a cache hit must build DraftThesis
    # objects and cost zero LLM calls.
    def no_llm(*args, **kwargs):
        raise AssertionError("a cached section must cost no LLM call")

    section = Section(id="S3.2", kind="section", title="Method", text="cascade text")
    cache = tmp / "cache"
    path = parse._cache_path(cache, section.text, llm.load_prompt("parse"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    with _swap(llm, "complete", no_llm):
        drafts = parse.parse_section(section, "abstract", "limits", cache_dir=cache)
    assert drafts and all(isinstance(d, DraftThesis) for d in drafts), drafts
    assert schema_properties(PARSE_SCHEMA, ["theses", "items"]) <= model_field_names(DraftThesis)
    return (f"{len(cached)} cached section(s) validated, "
            f"{sum(len(o['theses']) for _, o in cached)} theses; fixture from {origin}")


@check(3, "RRF fusion is sorted best-first and loses no document (hybrid_recipe.demo)")
def check_03(tmp: Path) -> str:
    recipe = REPO / "knowledge" / "09-raw" / "hybrid_recipe.py"
    note = ""
    if recipe.exists():                          # the reference recipe, run verbatim
        spec = importlib.util.spec_from_file_location("hybrid_recipe", recipe)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.demo()
    else:
        note = "knowledge/09-raw/hybrid_recipe.py absent (local-only), production RRF only"
    # The production copy, which is what actually runs: index.rrf_fuse (§3.5).
    left, right = [11, 22, 33, 44], [44, 55, 11]
    fused = index.rrf_fuse([left, right])
    ids = [doc for doc, _ in fused]
    assert set(ids) == set(left) | set(right), f"RRF dropped a document: {ids}"
    assert len(ids) == len(set(ids)), ids
    assert all(fused[i][1] >= fused[i + 1][1] for i in range(len(fused) - 1)), fused
    assert ids[0] == 11, fused                   # rank 1 in one list, rank 3 in the other
    assert abs(fused[0][1] - (1 / 61 + 1 / 63)) < 1e-12, fused
    return note


@check(4, "ranking returns exactly k and every item carries `via` (rank.demo)")
def check_04(tmp: Path) -> None:
    real_search = rank.search                    # demo() repoints it at its own temp index
    try:
        rank.demo()
    finally:
        rank.search = real_search


@check(5, "every /retrieve leaves a log line with score, raw_score, cut_off, via, "
          "rewrite_failed")
def check_05(tmp: Path) -> None:
    idx, _ = _corpus(tmp)
    query_vec = np.asarray(_vec(CORPUS[0]["thesis"]["text"]), dtype=np.float32)
    log_path = tmp / "logs" / "retrieve.jsonl"

    def lines() -> list[dict]:
        return [json.loads(ln) for ln in log_path.read_text(encoding="utf-8").splitlines()]

    with contextlib.ExitStack() as stack:
        stack.enter_context(_swap(api, "RETRIEVE_LOG", log_path))
        # The whole read path is real except the two edges that would need a server:
        # the query embedding and the rewrite call.
        stack.enter_context(_swap(rank, "search", lambda q, qv, top_k=50: index.search_theses(
            q, top_k, query_vec=query_vec, db=idx)))
        stack.enter_context(_swap(rewrite, "rewrite",
                                  lambda query, budget=None: (query + " frozen encoder", False)))
        status, body = api.retrieve("how do I keep accuracy when finetuning", k=2,
                                    run_id="selfcheck-retrieve")
        assert status == 200, (status, body)
        assert set(body) == {"ideas", "log_id", "cost"}, sorted(body)
        assert len(body["ideas"]) == 2, body["ideas"]

        line = lines()[-1]
        assert set(line) == {"log_id", "ts", "query_raw", "query_rewritten", "rewrite_failed",
                             "k", "returned", "cut_off", "cost"}, sorted(line)
        assert line["log_id"] == body["log_id"] and line["k"] == 2
        assert line["query_rewritten"].endswith("frozen encoder")
        assert line["rewrite_failed"] is False
        assert [r["rank"] for r in line["returned"]] == [1, 2], line["returned"]
        for entry in line["returned"]:
            assert set(entry) == {"idea_id", "score", "raw_score", "rank", "via"}, entry
            assert entry["via"] in ("thesis", "edge", "padding"), entry
            assert entry["raw_score"] > 0.0, entry
        assert line["cut_off"], "4 ideas and k=2 must leave a cut-off tail"
        for entry in line["cut_off"]:
            assert set(entry) == {"idea_id", "score", "raw_score", "rank"}, entry
        # score is normalized per query, raw_score is not: the threshold curve of
        # §5.5 is built on the second one, so they must not be the same number.
        assert any(abs(e["score"] - e["raw_score"]) > 1e-9
                   for e in line["returned"] + line["cut_off"]), line
        assert line["cost"] == body["cost"], (line["cost"], body["cost"])

        with _swap(rewrite, "rewrite", lambda query, budget=None: (query, True)):
            api.retrieve("island model migration", k=2)
        assert lines()[-1]["rewrite_failed"] is True, "a degraded rewrite must reach the log"
        assert len(lines()) == 2, lines()


@check(6, "idempotency: the same source through phase 2 twice -> zero new theses")
def check_06(tmp: Path) -> None:
    idx, first = _corpus(tmp)
    (tmp / "staging.cursor").write_text("0\n", encoding="utf-8")   # replay from the top
    second, ops, _ = _phase2(tmp, CORPUS, [])                      # no answer may be needed
    assert ops == [], f"a replayed corpus cost {len(ops)} LLM calls"
    assert second["theses_written"] == 0, second
    assert second["theses_skipped"] == 5, second
    assert second["theses"] == first["theses"] == 5, (first, second)
    assert second["ideas"] == first["ideas"] == 4, (first, second)
    assert index.count(db=idx) == 5, index.count(db=idx)


@check(7, "not idempotent between sources: the same wording from another source -> "
          "a new leaf")
def check_07(tmp: Path) -> None:
    idx, _ = _corpus(tmp)
    repeated = CORPUS[0]["thesis"]["text"]
    rows = CORPUS + [_row("s3", repeated, FREEZE)]
    third, ops, _ = _phase2(tmp, rows, [_pick(FREEZE)])
    assert third["theses"] == 6, third                 # the leaf was added
    assert third["ideas"] == 4, third                  # under the existing idea
    assert third["sources"] == 3, third
    leaves = [t for t in graph_client.all_theses() if text_hash(t["text"]) == text_hash(repeated)]
    assert len(leaves) == 2, leaves
    assert len({t["idea_id"] for t in leaves}) == 1, "the two sources split into two ideas"
    assert ops[0] == "link", ops
    assert "rederive" in ops, "the third leaf must trigger the re-derivation (§4.6)"
    assert index.count(db=idx) == 6, index.count(db=idx)


@check(8, "fail-closed: a broken arbiter writes nothing and queues one pending_link line")
def check_08(tmp: Path) -> None:
    idx = _open(tmp)
    sid = _write_source("s2")
    pending = tmp / "pending_link.jsonl"
    rows = [_row("s2", "adaptive mutation rates raise search efficiency by 12%",
                 "adapt the mutation rate to observed progress"),
            _row("s2", "adaptive mutation scheduling improves search efficiency",
                 "adapt the mutation rate to observed progress"),
            _row("s2", "curriculum ordering of tasks speeds up convergence",
                 "order tasks from easy to hard")]
    fake, ops = _arbiter([llm.LLMError("connection reset by peer"), -1])
    with _swap(llm, "complete", fake):
        decisions = link.link_batch(sid, rows, index_db=idx, pending_path=pending)

    assert decisions[1]["thesis"] is None and decisions[1]["skipped"], decisions[1]
    assert decisions[1]["idea"] is None, decisions[1]
    assert decisions[1]["reason"].startswith("pending_link: LLMError"), decisions[1]["reason"]
    assert decisions[0]["thesis"] is not None and decisions[2]["thesis"] is not None, \
        "one failed thesis blocked the rest of the article"
    assert graph_client.all_theses() == [], "link_batch wrote to the graph"

    queued = [json.loads(ln) for ln in pending.read_text(encoding="utf-8").splitlines()]
    assert len(queued) == 1, queued
    assert set(queued[0]) == {"ts", "run_id", "staging_line", "candidates", "error"}, queued[0]
    assert queued[0]["staging_line"] == rows[1], "the queued line is not self-contained"
    assert queued[0]["candidates"], queued[0]
    assert set(queued[0]["candidates"][0]) == {"idea_id", "thesis_id", "score"}, queued[0]
    assert "connection reset" in queued[0]["error"], queued[0]["error"]
    assert ops == ["link", "link"], ops       # thesis 1 had no candidates: no call


@check(9, "immutability: no method anywhere changes Thesis.text, in either module")
def check_09(tmp: Path) -> None:
    for module in (graph_client, stub_store):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        defined = {node.name for node in ast.walk(tree)
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        defined |= {node.id for node in ast.walk(tree)
                    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
        assert "update_thesis" not in defined, f"{module.__name__} grew an update_thesis"
        assert not hasattr(module, "update_thesis"), module.__name__
    # A direct UPDATE would bypass the missing method. Exactly ONE is allowed in all of
    # block A — `stub_store.split_idea` re-homing a leaf — and it may write exactly one
    # column. §1.2 immutability is about what the source said (text, context, effect,
    # locator, text_hash, source_id); `idea_id` is the arbiter's decision from §4.5 and
    # has to be repairable, or a mislinked leaf can only be fixed by deleting it.
    # `UPDATE thesis SET text=?` anywhere, including in that one function, still fails.
    found = 0
    for path in sorted((REPO / "lake").rglob("*.py")):
        if path.name == "selfcheck.py" and path.parent.name == "lake":
            continue
        for columns in _thesis_update_columns(path.read_text(encoding="utf-8")):
            found += 1
            assert path == REPO / "lake" / "stub_store.py", f"{path}: direct UPDATE on thesis"
            assert columns == {"idea_id"}, \
                f"{path}: UPDATE thesis assigns {sorted(columns) or '(unparsed)'} — " \
                "only idea_id may move"
    assert found == 1, f"the one allowed UPDATE on thesis is now {found}"
    # And the one update the store does expose refuses to touch anything but an idea.
    _open(tmp)
    sid = _write_source("s1")
    idea = Idea(id=new_idea_id(), text="freeze", applicability_conditions="a",
                limitations="l", failure_modes=[], effect_claimed="", effect_observed="",
                vector=_vec("freeze"))
    leaf = _thesis("s1", idea.id, "the encoder stays frozen")
    graph_client.create_idea_with_theses(idea, sid, [leaf])
    for bad in (leaf.id, "th_nope"):
        try:
            graph_client.update_idea(bad, {"text": "rewritten"})
        except KeyError:
            pass
        else:
            raise AssertionError(f"update_idea({bad!r}) reached a row it must not touch")
    assert graph_client.get_leaves(idea.id)[0]["text"] == "the encoder stays frozen"


@check(10, "cardinality: every thesis carries exactly one existing idea_id")
def check_10(tmp: Path) -> None:
    _corpus(tmp)
    leaves = graph_client.all_theses()
    assert len(leaves) == 5, leaves
    ids = [leaf["id"] for leaf in leaves]
    assert len(set(ids)) == len(ids), "a thesis id appears twice"
    assert all(leaf["idea_id"] for leaf in leaves), leaves
    idea_ids = sorted({leaf["idea_id"] for leaf in leaves})
    assert {idea["id"] for idea in graph_client.get_ideas(idea_ids)} == set(idea_ids), \
        "a leaf points at an idea that is not in the store"
    # Summing the leaves per idea has to give the total back: a leaf counted under
    # two ideas would show up here as an excess.
    assert sum(graph_client.leaf_count(i) for i in idea_ids) == len(leaves)


@check(11, "schema property names are a subset of the model fields (drift guard)")
def check_11(tmp: Path) -> None:
    assert SCHEMA_BINDINGS, "no schema is bound to a model"
    for schema, cls, path in SCHEMA_BINDINGS:
        properties = schema_properties(schema, path)
        fields = model_field_names(cls)
        assert properties, (cls.__name__, path)
        assert properties <= fields, \
            f"{cls.__name__}: schema fills {sorted(properties - fields)}, which has nowhere to go"


@check(12, "BM25 arm is alive: MATCH on a token known to be in the store is not empty")
def check_12(tmp: Path) -> None:
    index.demo()                                  # §6.12 assertion inside index.py
    idx, _ = _corpus(tmp)
    con = index._con(idx)
    assert con.execute("SELECT count(*) FROM thesis_fts").fetchone()[0] == 5, \
        "the FTS table is empty: the hybrid silently degraded to cosine"
    for token in ("encoder", "migration", "precision"):
        assert index.bm25_search(con, token, 5), f"MATCH {token!r} returned nothing"


@check(13, "a dirty rewritten query does not crash search and still returns hits")
def check_13(tmp: Path) -> None:
    search.demo()
    idx, _ = _corpus(tmp)
    query_vec = np.asarray(_vec("island"), dtype=np.float32)
    dirty = 'cheap proxy: pre-filter candidates -- "encoder" OR NEAR(migration)'
    hits = search.search(dirty, query_vec, top_k=50, db=idx)
    assert hits, "the escaped query returned nothing"
    # The point of the OR-join: with the implicit AND of FTS5 no document contains
    # every token and the BM25 arm goes silently to zero (§5.2).
    assert any(hit["bm25_rank"] for hit in hits), "BM25 arm empty: implicit AND is back"
    con = index._con(idx)
    assert index.bm25_search(con, "::: --- ???", 5) == [], "punctuation-only must give []"
    punct = search.search("::: --- ???", query_vec, top_k=50, db=idx)
    assert punct and all(hit["bm25_rank"] is None for hit in punct), \
        "punctuation-only: the cosine arm answers alone"


@check(14, "a string exactly at its maxLength raises LLMError (the silent truncation)")
def check_14(tmp: Path) -> None:
    model = ("http://selfcheck.invalid", "LAKE_SELFCHECK_KEY")
    body = {"text": "t", "applicability_conditions": "a", "limitations": "l",
            "failure_modes": ["f"]}
    cap = GENERALIZE_SCHEMA["properties"]["text"]["maxLength"]

    def answer(payload: dict):
        # finish_reason "stop" on purpose: this is the failure the §3.1 p.5 check
        # cannot see — the grammar closed the string legally (probe-results.md:47).
        return {"choices": [{"finish_reason": "stop",
                             "message": {"content": json.dumps(payload)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20}}

    def call(payload):
        with _swap(llm, "_post", lambda base_url, key, body_, timeout: answer(payload)):
            return llm.complete("p", system="s", schema=GENERALIZE_SCHEMA, op="generalize",
                                max_tokens=800, timeout=60, model=model)

    os.environ[model[1]] = "not-a-real-key-no-socket-is-opened"
    try:
        assert call({**body, "text": "x" * (cap - 1)})["text"] == "x" * (cap - 1)
        for truncated in ({**body, "text": "x" * cap},
                          {**body, "failure_modes": ["ok", "f" * 300]}):
            try:
                call(truncated)
            except llm.LLMError as exc:
                assert "truncated at maxLength" in str(exc), exc
            else:
                raise AssertionError("a string at its ceiling was accepted as complete")
    finally:
        os.environ.pop(model[1], None)


@check(15, "batch overlay: two near-identical theses of one source -> one idea")
def check_15(tmp: Path) -> None:
    idx = _open(tmp)
    sid = _write_source("s1")
    rows = CORPUS[:2]
    fake, ops = _arbiter([0])                    # the only candidate is the overlay entry
    with _swap(llm, "complete", fake):
        decisions = link.link_batch(sid, rows, index_db=idx,
                                    pending_path=tmp / "pending_link.jsonl")
    assert ops == ["link"], "thesis 1 has no candidates and must cost no call"
    assert decisions[0]["idea"] is not None and decisions[0]["reason"] == "new"
    assert decisions[1]["idea"] is None, "thesis 2 opened a second idea: the overlay is dead"
    assert decisions[0]["thesis"].idea_id == decisions[1]["thesis"].idea_id
    assert len(decisions[0]["idea"].vector) == EMBED_DIM


@check(16, "in-batch dedup: two equal text_hash in one source -> one leaf, no exception")
def check_16(tmp: Path) -> None:
    idx = _open(tmp)
    sid = _write_source("s2")
    duplicate = "mixed precision training halves memory at equal accuracy"
    rows = [_row("s2", duplicate, "train in mixed precision"),
            _row("s2", "MIXED   Precision training halves memory at EQUAL accuracy",
                 "train in mixed precision"),
            _row("s2", "curriculum ordering of tasks speeds up convergence",
                 "order tasks from easy to hard")]
    assert rows[0]["thesis"]["text_hash"] == rows[1]["thesis"]["text_hash"]
    fake, ops = _arbiter([-1])
    with _swap(llm, "complete", fake):
        decisions = link.link_batch(sid, rows, index_db=idx,
                                    pending_path=tmp / "pending_link.jsonl")
    assert decisions[1]["skipped"] and "earlier in this batch" in decisions[1]["reason"]
    written = [d for d in decisions if d["thesis"] is not None]
    assert len(written) == 2, written
    # The write itself must survive UNIQUE(source_id, text_hash): one idea through
    # the transactional call, the other through create_idea + write_theses.
    first, second = written
    graph_client.create_idea_with_theses(first["idea"], sid, [first["thesis"]])
    graph_client.create_idea(second["idea"])
    assert graph_client.write_theses(sid, [second["thesis"]]) == [second["thesis"].id]
    assert graph_client.leaf_count(first["thesis"].idea_id) == 1
    assert len(graph_client.all_theses()) == 2


@check(17, "phase 2 with a stubbed write failure leaves zero ideas without leaves")
def check_17(tmp: Path) -> None:
    idx = _open(tmp)
    real_insert = stub_store._insert_theses

    def boom(conn, source_id, theses):
        # Inside the transaction, after the idea row: exactly the window that would
        # leave IDEA ||--|{ THESIS broken if the two were not one transaction (§3.4).
        if any(t.text.startswith("mixed precision") for t in theses):
            raise sqlite3.OperationalError("disk I/O error")
        return real_insert(conn, source_id, theses)

    with _swap(stub_store, "_insert_theses", boom):
        try:
            _phase2(tmp, CORPUS, CORPUS_ANSWERS)
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError("the stubbed write failure did not reach the caller")

    assert graph_client.ideas_without_leaves() == [], "an idea survived with zero leaves"
    leaves = graph_client.all_theses()
    assert len(leaves) == 3, leaves                        # source 1 only
    assert len({leaf["idea_id"] for leaf in leaves}) == 2, leaves
    assert (tmp / "staging.cursor").read_text(encoding="utf-8").strip() == "3", \
        "the cursor moved past the source that failed"
    assert index.count(db=idx) == 3, index.count(db=idx)


@check(18, "the re-derivation trigger survives a restart: it is a field, not a counter")
def check_18(tmp: Path) -> None:
    _open(tmp)
    sid = _write_source("s1")
    idea = Idea(id=new_idea_id(), text="freeze the pretrained encoder",
                applicability_conditions="ac", limitations="lim", failure_modes=["fm"],
                effect_claimed="+3 pp", effect_observed="",
                vector=_vec("freeze the pretrained encoder"))
    graph_client.create_idea_with_theses(idea, sid, [_thesis("s1", idea.id, "leaf one"),
                                                    _thesis("s1", idea.id, "leaf two")])
    fake, ops = _arbiter([])

    def restart() -> None:
        """Everything in memory is dropped; the store file is all that is left."""
        stub_store._conn.close()
        stub_store._conn = None

    restart()
    assert graph_client.get_ideas([idea.id])[0]["rederived_at_leaf_count"] == 0
    with _swap(llm, "complete", fake):
        assert rederive.maybe_rederive(idea.id) is False, "2 leaves must not fire the trigger"
        graph_client.create_idea_with_theses(None, sid, [_thesis("s1", idea.id, "leaf three")])
        restart()
        assert rederive.maybe_rederive(idea.id) is True, "the 3rd leaf after a restart"
        after = graph_client.get_ideas([idea.id])[0]
        assert after["id"] == idea.id, "the re-derivation changed the idea id"
        assert after["rederived_at_leaf_count"] == 3, after
        assert after["text"] == REDERIVED["text"] and after["limitations"] == "lim2"
        assert after["effect_claimed"] != after["effect_observed"], after
        assert not after["dirty"], "dirty is B's field"
        assert np.allclose(after["vector"], _vec(REDERIVED["text"]), atol=1e-6), \
            "the text changed and the vector did not follow"
        restart()
        assert rederive.maybe_rederive(idea.id) is False, \
            "the trigger fired twice on the same three leaves"
    assert ops == ["rederive"], ops


@check(19, "index and graph agree: index.count() == leaves in the store, and the "
           "reconciliation path is reset() + index_rows(all_theses())")
def check_19(tmp: Path) -> str:
    idx, report = _corpus(tmp)
    leaves = graph_client.all_theses()
    assert index.count(db=idx) == len(leaves) == report["theses"] == 5, index.count(db=idx)

    # rebuild_from(staging) is NOT the reconciliation path: `idea_id` is assigned in
    # phase 2, so no staging line carries one and the module refuses the file. The
    # refusal must leave the index it was asked to repair intact — a rebuild is
    # reached for when the index is already suspect, so a destructive refusal would
    # turn a diagnosis into damage.
    try:
        index.rebuild_from(str(tmp / "staging.jsonl"), db=idx)
    except ValueError as exc:
        assert "no idea_id" in str(exc), exc
    else:
        raise AssertionError("rebuild_from(staging) accepted lines with no idea_id")
    assert index.count(db=idx) == len(leaves), "a refused rebuild damaged the index"

    # Manufacture the drift honestly, then take the path §6.19 actually takes:
    # reset() + index_rows(graph_client.all_theses()).
    index.reset(db=idx)
    assert index.count(db=idx) == 0 != len(leaves), "a wiped index went unnoticed"
    assert index.index_rows(graph_client.all_theses(), db=idx) == 5
    assert index.count(db=idx) == len(graph_client.all_theses()) == 5
    # Searchable again, not merely counted.
    assert index.search_theses("encoder", 5, db=idx,
                               query_vec=np.asarray(_vec("encoder"), dtype=np.float32))
    return "rebuild_from(staging) refuses without damaging the index; reset() + index_rows() repairs"


@check(20, "vault export: every [[link]] resolves, a source lists only its own leaves, "
           "a store that contradicts itself is refused (vault.demo, §11.6)")
def check_20(tmp: Path) -> None:
    # Owns its temp store and its own data/ fingerprint, like the demos above.
    vault.demo()


@check(21, "Neo4j load: a required model field the reader did not carry is refused, "
           "not written as a hole (neo4j_load.demo, C1 `07`)")
def check_21(tmp: Path) -> None:
    # Wired in on 2026-07-29. `neo4j_load` had this check from the start but only
    # behind its own `--self-check`, so the suite never ran it and 60 theses reached
    # the database with no vector: `list_theses` does not carry one and `_row`
    # dropped it. A check nobody runs is the false confidence, not the absent one.
    neo4j_load.demo()


@check(22, "an idea over the leaf ceiling is split by its leaf vectors, every part is "
           "re-derived over its own leaves, and the split writes idea_id and nothing "
           "else on a thesis (split.demo, issue #2)")
def check_22(tmp: Path) -> None:
    split.demo()


@check(23, "phase 2 runs the split sweep and reports the ceiling off the STORE: an idea "
           "over it is split even when staging is empty, and the alarm is not read off "
           "the list of failed attempts (issue #2)")
def check_23(tmp: Path) -> str:
    idx = _open(tmp)
    sid = _write_source("s1")
    idea = Idea(id=new_idea_id(), text="the whole research area",
                applicability_conditions="ac", limitations="lim", failure_modes=["fm"],
                effect_claimed="lots of numbers", effect_observed="",
                vector=_vec("the whole research area"))
    leaves = [_thesis("s1", idea.id, f"leaf {n} about theme {n % 3}") for n in range(20)]
    graph_client.create_idea_with_theses(idea, sid, leaves)
    index.index_theses(leaves, db=idx)
    assert split.due() == [idea.id], "the fixture is not over the ceiling"

    # Empty staging on purpose. `split.due()` used to be called only inside the
    # per-source loop, so a phase 2 with nothing left to ingest — the normal state
    # after a finished run — processed no group, swept nothing, and reported a lake
    # holding a 92-leaf node as healthy: `split_failed: []` reads as "no problem"
    # when it actually means "never looked". This is the run that has to repair it.
    report, ops, _ = _phase2(tmp, [], [])
    assert report["sources_processed"] == 0 and report["theses_written"] == 0, report
    assert "link" not in ops, ops

    assert len(report["splits"]) == 1, report["splits"]
    assert report["split_failed"] == [], report["split_failed"]
    parts = report["splits"][0]["parts"]
    assert len(parts) >= 2 and sum(n for _, n in parts) == 20, parts
    assert parts[0][0] == idea.id, "the parent did not keep its id"

    # The two report numbers that describe the defect, both read off the store.
    assert report["ideas_over_ceiling"] == 0, report["ideas_over_ceiling"]
    assert report["max_leaves_per_idea"] == max(n for _, n in parts) <= split.MAX_LEAVES, \
        report["max_leaves_per_idea"]
    assert split.due() == [] and report["ideas_without_leaves"] == 0, report

    # The store moved and the index went with it — the drift is looked for by value.
    assert index.stale_links(graph_client.all_theses(), db=idx) == [], "index left stale"
    assert index.count(db=idx) == 20, index.count(db=idx)

    # --- the sweep inside the loop, and why it runs BEFORE §4.6 ----------------------
    # A second over-ceiling idea, this one due for re-derivation too (counter at 0).
    # With one source in staging the per-source sweep splits it first and resets every
    # part's counter to its own size, so §4.6 has nothing left to do. Without that
    # sweep the split only happens after the loop, and §4.6 fires first — paying for a
    # re-derivation over the whole over-broad set that the split then throws away.
    # `rederived == 0` is what says the two ran in the right order.
    second = Idea(id=new_idea_id(), text="another whole research area",
                  applicability_conditions="ac", limitations="lim", failure_modes=["fm"],
                  effect_claimed="numbers", effect_observed="",
                  vector=_vec("another whole research area"))
    more = [_thesis("s1", second.id, f"second leaf {n} on topic {n % 3}") for n in range(20)]
    graph_client.create_idea_with_theses(second, sid, more)
    index.index_theses(more, db=idx)
    assert second.id in split.due() and _rederive_would_fire(second.id)

    report2, ops2, _ = _phase2(tmp, [_row("s2", "a wholly unrelated mixed precision trick",
                                          "train in mixed precision")], [-1])
    assert report2["sources_processed"] == 1, report2
    assert any(s["idea_id"] == second.id for s in report2["splits"]), report2["splits"]
    assert report2["rederived"] == 0, \
        "§4.6 ran before the split and re-derived the over-broad set it was about to lose"
    assert report2["ideas_over_ceiling"] == 0 and report2["split_failed"] == [], report2

    # --- a split that FAILS: the two numbers must disagree, and honestly -------------
    third = Idea(id=new_idea_id(), text="a third research area", applicability_conditions="ac",
                 limitations="lim", failure_modes=["fm"], effect_claimed="", effect_observed="",
                 vector=_vec("a third research area"))
    graph_client.create_idea_with_theses(
        third, sid, [_thesis("s1", third.id, f"third leaf {n}") for n in range(20)])
    boom = lambda idea_id, *a, **kw: (_ for _ in ()).throw(RuntimeError("clustering died"))
    with _swap(split, "split_idea", boom):
        # One source in staging, so the sweep runs twice: once in the loop and once
        # after it. ONE idea is over the ceiling and the failure list has TWO entries —
        # the two numbers have to disagree here, or `ideas_over_ceiling` could be read
        # off `split_failed` and nobody would notice.
        # Three rows because the cursor from the run above already covers line 1: two
        # survive it and form one group, which is all this needs.
        report3, _, _ = _phase2(tmp, [_row("s3", f"a third unrelated trick, number {n}",
                                           "cache the intermediate results")
                                      for n in range(3)], [-1] * 3)
    assert {f["idea_id"] for f in report3["split_failed"]} == {third.id}, report3["split_failed"]
    assert len(report3["split_failed"]) == 2, \
        "the sweep must run per source AND after the loop, so this counts attempts"
    assert report3["ideas_over_ceiling"] == 1, report3["ideas_over_ceiling"]
    assert report3["max_leaves_per_idea"] == 20, report3["max_leaves_per_idea"]
    assert "clustering died" in report3["split_failed"][0]["error"]
    return (f"empty staging still split 20 leaves into {len(parts)} parts; the in-loop "
            "sweep runs before §4.6; a failed split still reports the true ceiling")


def _rederive_would_fire(idea_id: str, threshold: int = 3) -> bool:
    body = graph_client.get_ideas([idea_id])[0]
    return len(body["theses"]) - body["rederived_at_leaf_count"] >= threshold


# ------------------------------------------------------------------- the runner

def _fingerprint_real_data() -> dict[str, str]:
    """sha1 of every real artefact the suite must not touch (missing files count too)."""
    import hashlib
    from .models import DATA
    out = {}
    for path in (DATA / "lake.db", DATA / "index.db", DATA / "staging.jsonl",
                 DATA / "staging.cursor", DATA / "pending_link.jsonl",
                 DATA / "logs" / "retrieve.jsonl"):
        out[path.name] = (hashlib.sha1(path.read_bytes()).hexdigest()
                          if path.exists() else "absent")
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m lake.selfcheck",
        description="The 19 assertions of spec 10 §6 plus §11.6, the Neo4j load and the "
                    "leaf-ceiling split, "
                    "one run, only assert.")
    parser.add_argument("--offline", action="store_true",
                        help="skip 6.1, the only check that opens a socket; the other "
                             "21 need neither the network nor a key")
    args = parser.parse_args(argv)

    trace.set_run_id("selfcheck-" + uuid.uuid4().hex[:6])
    failed: list[int] = []
    skipped: list[int] = []
    # The suite claims in its own docstring that it never writes to data/. Claiming
    # is not checking: a helper reaching for a module default instead of the bound
    # temp path wrote fixture rows into the real index and stayed invisible until a
    # live query answered 503. Fingerprint before, compare after.
    guarded = _fingerprint_real_data()
    with tempfile.TemporaryDirectory(prefix="lake-selfcheck-") as root:
        # Traces of the check itself do not belong in data/traces with the real runs.
        with _swap(trace, "TRACES_DIR", Path(root) / "traces"), _fake_embed():
            for number, what, fn in CHECKS:
                if number == 1 and args.offline:
                    skipped.append(number)
                    print(f"skip 6.{number}  {what} [--offline]")
                    continue
                tmp = Path(root) / f"check{number:02d}"
                tmp.mkdir()
                # The reused demos and phase 2 print their own reports; they are
                # shown only when something failed and the output is evidence.
                captured = io.StringIO()
                try:
                    with contextlib.redirect_stdout(captured):
                        note = fn(tmp)
                except Exception:
                    failed.append(number)
                    print(f"FAIL 6.{number}  {what}")
                    traceback.print_exc(file=sys.stdout)
                    sys.stdout.write(captured.getvalue())
                else:
                    print(f"ok  6.{number}  {what}" + (f"\n      {note}" if note else ""))
                finally:
                    _cleanup()

    touched = [name for name, digest in _fingerprint_real_data().items()
               if guarded.get(name) != digest]
    if touched:
        failed.append(0)
        print(f"FAIL 6.0  the suite wrote to real data/: {', '.join(sorted(touched))}")

    total = len(CHECKS)
    summary = f"{total - len(failed) - len(skipped)}/{total} ok"
    if skipped:
        summary += f", {len(skipped)} skipped ({', '.join('6.%d' % n for n in skipped)})"
    if failed:
        summary += f", {len(failed)} FAILED ({', '.join('6.%d' % n for n in failed)})"
    print(summary)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
