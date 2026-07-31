"""One run, one file: the 19 assertions of spec 10 §6 (`10-implementation-spec.md:638-664`),
the vault export of §11.6, which the spec asks for in the same shape, and the shaping
of the Neo4j load (C1, `07-roles-and-contracts.md:72`).

    python3 -m lake.selfcheck             # 6.1 talks to both school servers
    python3 -m lake.selfcheck --offline   # 34 of 35, no LLM network, no key in the env

Both forms need a live, empty, LOCAL Neo4j (D11: the only backend, not a `stub`
default some checks used to reach past on purpose) — checked once, loudly, before
either form runs a single check (`_require_neo4j_up`). `--offline` is only ever
about the school's LLM servers, never about the graph.

Only `assert`, no framework. Every check gets its own temporary directory and the
writers are pointed at it, and wipes the shared live graph clean going in and
coming out (`_open`/`_cleanup`): the real `data/index.db`, `data/staging.jsonl`,
`data/pending_link.jsonl`, `data/traces/` and `data/logs/` are never opened for
writing — they hold the results of real runs. The one thing read from `data/` is
the parse cache, and only as the §6.2 fixture.

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
import csv
import functools
import importlib.util
import io
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
import threading
import traceback
import types
import uuid
from pathlib import Path

import numpy as np

from . import (graph_client, idea_merger, index, llm, neo4j_load, neo4j_store, queue,
               trace, vault, writer_lock)
from .api import jobs, workers
from .ingest import generalize, link, parse, rederive, run, runlog, split, trust
from .models import (CACHE_DIR, EMBED_DIM, GENERALIZE_SCHEMA, PARSE_SCHEMA,
                     SCHEMA_BINDINGS, DraftThesis, Idea, IdeaFields, Section, Source,
                     Thesis, model_field_names, new_idea_id, new_thesis_id,
                     schema_properties, source_id as make_source_id, text_hash)
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


# D11's Cypher-side counterpart: `neo4j_store` has no `UPDATE` keyword to grep for —
# Cypher's only property-mutation syntax is `SET`, on a node bound by `MATCH`, `MERGE`
# or `CREATE` alike. Unlike the SQL side there is no legal exception left at all:
# `neo4j_store.split_idea` re-homes a leaf by moving the `HAS_LEAF` edge, never by
# writing `idea_id` as a Thesis property (module docstring) — so every hit this finds
# is a violation, not "check the one permitted column".
_CYPHER_THESIS_ALIAS = re.compile(r"\(\s*(\w+)\s*:\s*Thesis\b")
_CYPHER_SET = re.compile(r"\bSET\s+(\w+)\b")


def _cypher_query_strings(source: str) -> list[str]:
    """Every run of Python string literals Python itself would implicitly
    concatenate into one value — the unit a single `tx.run("...")`/`session.run(f"...")`
    call passes as its Cypher text. Scoping to THIS, not the whole file, is the
    difference between a real violation and noise: single-letter aliases like `n` or
    `t` are reused across dozens of unrelated statements (`CREATE CONSTRAINT ... FOR
    (n:Thesis)`, `MERGE (n:Source {id: $id}) SET n = $row`, ...) for completely
    different labels, and a whole-file alias scan flags every one of them the moment
    ANY statement anywhere binds that same letter to `:Thesis`. `tokenize`, not a
    second regex heuristic: string-literal adjacency is a real Python grammar rule,
    not a pattern worth re-approximating.
    """
    import io
    import tokenize

    chunks: list[str] = []
    run: list[str] = []

    def flush():
        if run:
            chunks.append("".join(run))
            run.clear()

    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.STRING:
            body = tok.string
            i = 0
            while i < len(body) and body[i] not in "'\"":
                i += 1                                    # skip f/r/b/u prefix letters
            quote = body[i:i + 3] if body[i:i + 3] in ('"""', "'''") else body[i]
            run.append(body[i + len(quote):-len(quote)])
        elif tok.type not in (tokenize.NL, tokenize.COMMENT, tokenize.INDENT,
                              tokenize.DEDENT, tokenize.ENCODING):
            flush()
    flush()
    return chunks


def _cypher_thesis_sets(source: str) -> list[str]:
    """Every `SET <alias>` where `<alias>` is bound, in the SAME query string, to
    `:Thesis`. A node's initial `CREATE (t:Thesis $row)` never contains the token
    `SET` at all, so this cannot flag creation — only a later mutation."""
    hits = []
    for chunk in _cypher_query_strings(source):
        aliases = set(_CYPHER_THESIS_ALIAS.findall(chunk))
        if not aliases:
            continue
        for m in _CYPHER_SET.finditer(chunk):
            if m.group(1) in aliases:
                hits.append(chunk[max(0, m.start() - 40):m.start() + 60])
    return hits


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


def _wipe_graph() -> None:
    """`MATCH (n) DETACH DELETE n`, guarded exactly like `neo4j_load.py --wipe`
    (`neo4j_store._require_local_target`, reused rather than a second copy):
    it checks the URI the DRIVER actually connected with,
    never `os.environ` re-read at call time (`13` MAJOR 4) — this can never
    land on a host that is not localhost/127.0.0.1/the compose service name,
    no matter what `NEO4J_URI` says by the time some check runs. This is the
    isolation `stub_store._db_path` swapping used to give for free with a fresh
    SQLite file per check (D11): there is one shared live graph now, and
    "isolated" means "wiped clean by us, who already own everything in it" —
    `main()`'s `_require_neo4j_up` is what confirms that ownership once, before
    any check runs at all.
    """
    neo4j_store._get_driver()  # so `_uri` below reflects what the driver really used
    neo4j_store._require_local_target(neo4j_store._uri)
    with neo4j_store._session() as session:
        session.run("MATCH (n) DETACH DELETE n").consume()


def _require_neo4j_up() -> None:
    """The whole suite's precondition after D11 (`13` §4.1): Neo4j is the only
    backend now, so every check needs a live one, not just the two that used to
    reach past a `stub` default on purpose. Checked ONCE, before any check
    runs: a single clear abort here beats the same `ServiceUnavailable`
    traceback surfacing under 25 near-identical `FAIL 6.N` lines, and this must
    never print so much as one `ok` line before it — "no graph" quietly
    reading as partial success is exactly the shape CLAUDE.md's fail-open ban
    exists to catch.

    Unreachable and reachable-but-not-empty are two different refusals and
    must not read the same way (the reasoning `neo4j_store`'s own self-check
    gives for its BLOCKER 1): the checks below wipe the graph freely BETWEEN
    themselves once they own it, but this gate is the one place that decides
    whether they get to own it at all — a database that already has something
    in it might be a stale fixture, or might not be this suite's to erase, and
    only a human reading this message can tell which.
    """
    uri = os.environ.get("NEO4J_URI")
    try:
        with neo4j_store._session() as session:
            existing = session.run("MATCH (n) RETURN count(n) AS c").single()["c"]
    except graph_client.STORE_ERRORS as exc:
        raise SystemExit(
            f"ABORT: Neo4j is required for the whole suite now (D11) and is not "
            f"reachable at NEO4J_URI={uri!r} ({type(exc).__name__}: {exc}). Bring "
            "one up (`docker compose up -d neo4j`, or a bare `docker run -d --rm "
            "-p 7687:7687 -e NEO4J_AUTH=none neo4j:5-community`) and rerun.") from exc
    if existing:
        raise SystemExit(
            f"ABORT: NEO4J_URI={uri!r} is reachable but NOT EMPTY ({existing} "
            "node(s), MATCH (n)) — this suite wipes the graph between its own "
            "checks once it owns it, but refuses to make that call about a "
            "database it did not confirm empty first. Wipe it by hand "
            "(`MATCH (n) DETACH DELETE n`) if it really is scratch, or point "
            "NEO4J_URI at an empty instance, and rerun.")


def _open(tmp: Path) -> Path:
    """Wipe the graph and hand back the index path. Returns tmp/index.db.

    D11: one live Neo4j, shared by every check in this file — there is no
    per-check file to swap in for isolation the way `stub_store._db_path` gave
    for free. `_wipe_graph` is the replacement: every check starts clean
    because it just cleaned the graph itself, not because it got a private
    file nobody else could see.
    """
    _wipe_graph()
    return tmp / "index.db"


def _cleanup() -> None:
    """Close every cached index handle between checks, and leave the graph
    wiped: a check that fails partway must not poison the next one's fixtures
    with rows it never got to clean up itself."""
    for key in list(index._CONNS):
        index._CONNS.pop(key).close()
    index._MATS.clear()
    _wipe_graph()


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


def _seed_idea(source_id: str, text: str, trust: float) -> str:
    """One Idea with one real leaf under `source_id` — for checks that mock
    `rank.search`'s hits directly (bypassing FTS/embedding entirely, `check_32`'s own
    pattern) and only need `graph_client.get_ideas` (via `rank._bodies`) to resolve a
    real body for each `idea_id` the mock hands back."""
    idea_id = new_idea_id()
    idea = Idea(id=idea_id, text=text, applicability_conditions="ac", limitations="lim",
               failure_modes=[], effect_claimed="+1 pp", effect_observed="", vector=_vec(text))
    leaf = Thesis(id=new_thesis_id(), source_id=source_id, idea_id=idea_id, text=text,
                 context="ctx", effect="+1 pp", locator="Table 1", text_hash=text_hash(text),
                 vector=_vec(text + " leaf"), created_at="2026-07-28T10:00:00Z")
    graph_client.create_idea_with_theses(idea, source_id, [leaf])
    if trust:
        graph_client.set_trust(idea_id, trust)
    return idea_id


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


def _arbiter(answers: list, *, trust_score: str = "5"):
    """Scripted `llm.complete`: link answers in order, one canned re-derivation, and
    the trust judge, which phase 2 calls once per dirty idea at the end (`13` §3.3).

    `trust_score` is a string because the judge's schema constrains the answer to an
    enum of strings — see `models.TRUST_SCHEMA`. Pass an Exception to make the judge
    refuse and check the fail-closed path.
    """
    ops: list[str] = []

    def fake(prompt, *, system, schema, op, max_tokens, timeout,
             model=llm.QWEN_9B, temperature=0.0):
        ops.append(op)
        if op == "rederive":
            assert "LEAVES (" in prompt, prompt
            return dict(REDERIVED)
        if op == "trust":
            assert model is llm.QWEN_35B, "the judge runs on 35B (`13` §3.3)"
            assert "leaves_shown:" in prompt and "leaves_total:" in prompt, prompt
            if isinstance(trust_score, Exception):
                raise trust_score
            return {"reason": "fixture", "score": trust_score}
        assert op == "link", op
        assert model is llm.QWEN_35B, "the arbiter must run on 35B (§8)"
        assert (max_tokens, timeout, temperature) == (300, 60.0, 0.0), (max_tokens, timeout)
        assert "CANDIDATE IDEAS" in prompt, prompt
        reply = answers.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {"link_to": reply(prompt) if callable(reply) else reply}

    return fake, ops


def _phase2(tmp: Path, rows: list[dict], answers: list, *, limit: int | None = None,
            trust_score: str = "5"):
    """The real `run.phase2` over `rows`, with every path inside `tmp`.

    `index.index_theses` and `link.link_batch` are bound to the temp index and the
    temp `pending_link.jsonl`; phase 2 calls both without a db argument, which in
    production is the point and here would open `data/`.
    """
    idx = tmp / "index.db"
    staging = tmp / "staging.jsonl"
    staging.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                       encoding="utf-8")
    fake, ops = _arbiter(list(answers), trust_score=trust_score)
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


def _cocite_edges(idea_ids: list[str]) -> dict[frozenset, list[dict]]:
    """`RELATED` edges among `idea_ids`, grouped by unordered pair.

    The shape both `_corpus` and check_06 need to confirm `write_cocitation_edges`
    wrote BOTH directions (mutation M6: the reverse call silently repeats the
    forward one) and, on a replay, did not move the weight (M7: the
    already-recorded-`source_id` guard is dropped and every reload adds another
    increment). One `graph_client.neighbors` call, not two — hop 1 already
    matches both directions once both endpoints are in the seed list.
    """
    by_pair: dict[frozenset, list[dict]] = {}
    for edge in graph_client.neighbors(idea_ids, hops=1):
        by_pair.setdefault(frozenset((edge["source_id"], edge["target_id"])), []).append(edge)
    return by_pair


def _corpus(tmp: Path) -> tuple[Path, dict]:
    """Two sources through the real phase 2: 5 leaves, 4 ideas, temp store + index."""
    idx = _open(tmp)
    report, ops, _ = _phase2(tmp, CORPUS, CORPUS_ANSWERS)
    assert report["theses"] == 5 and report["ideas"] == 4, report
    # Four link calls, then one judge call per idea the pass made dirty (`13` §3.3).
    # Spelled out rather than filtered: a phase 2 that stopped judging would otherwise
    # keep this fixture green, and the whole trust feature would go missing quietly.
    assert ops == ["link"] * 4 + ["trust"] * 4, ops
    assert report["trust_scored"] == 4 and report["trust_failed"] == 0, report
    assert report["ideas_without_leaves"] == 0 and report["pending_link"] == 0, report
    assert index.count(db=idx) == 5, index.count(db=idx)
    # D12 (review 2026-07-31, mutation agent): `write_cocitation_edges` runs inside
    # `run.phase2` for every check that calls `_corpus` (6.5, 6.6, 6.7, 6.10, 6.12,
    # 6.13, 6.17, 6.19...), but until now nothing here asserted anything about its
    # OUTPUT — the report field was printed and never read, exactly the "checked
    # but not read" shape that lets a broken write stay green (this file's own
    # docstring on 6.34 names the general failure). After BLOCKER 2's fix the gate
    # is the source's own distinct-idea count, and this fixture clears it without
    # any change: s1 alone already touches 2 ideas (freeze, island), s2 touches 2
    # (mixed precision, cheap proxy) — one pair per source, checked on the GRAPH
    # itself below, not only on the self-reported count (M6/M7 would keep this
    # report field printing correctly while the graph under it went wrong).
    assert report["cocitation_pairs"] == 2, report
    assert report["cocitation_missing"] == [], report
    leaves = {leaf["text"]: leaf["idea_id"] for leaf in graph_client.all_theses()}
    freeze_idea = leaves[CORPUS[0]["thesis"]["text"]]
    island_idea = leaves[CORPUS[2]["thesis"]["text"]]
    mixed_idea = leaves[CORPUS[3]["thesis"]["text"]]
    proxy_idea = leaves[CORPUS[4]["thesis"]["text"]]
    by_pair = _cocite_edges([freeze_idea, island_idea, mixed_idea, proxy_idea])
    assert set(by_pair) == {frozenset((freeze_idea, island_idea)),
                            frozenset((mixed_idea, proxy_idea))}, by_pair
    for pair, rows in by_pair.items():
        assert len(rows) == 2, f"co-citation must write both directions (M6): {rows}"
        assert {r["source_id"] for r in rows} == set(pair), \
            f"both ideas of a pair must each appear once as the edge's source (M6): {rows}"
        assert all(r["weight"] == 1.0 for r in rows), rows   # min(leaf counts) == 1 both pairs
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
        status = rank.demo()
    finally:
        rank.search = real_search
    # `demo()` returns 1 and prints SKIPPED/REFUSED instead of raising when it never
    # touched the graph (D14's quota, sections 9-11, is only ever exercised here) —
    # a bare call reads that as "ok" the same as a real pass. Assert, do not just call.
    assert status == 0, f"rank.demo() did not run (status={status}); see captured output"


@check(5, "every /retrieve leaves a log line with score, raw_score, cosine_similarity, "
          "cut_off, via, rewrite_failed")
def check_05(tmp: Path) -> None:
    idx, _ = _corpus(tmp)
    query_vec = np.asarray(_vec(CORPUS[0]["thesis"]["text"]), dtype=np.float32)
    log_path = tmp / "logs" / "retrieve.jsonl"

    def lines() -> list[dict]:
        return [json.loads(ln) for ln in log_path.read_text(encoding="utf-8").splitlines()]

    with contextlib.ExitStack() as stack:
        stack.enter_context(_swap(api, "RETRIEVE_LOG", log_path))
        # `index.count` takes `db=INDEX_DB` as a DEFAULT ARGUMENT, bound at def
        # time (the same trap `search.search` has, api/selfcheck.py:109-112, and
        # check_19 already patches around) — left unpatched, `retrieve.api.retrieve`'s
        # own §6.19 divergence guard reads the real `data/index.db` (empty in a
        # fresh image) against this check's temp store (5 leaves) and answers 503
        # instead of 200, and on a host whose real index.db is non-empty it silently
        # reads the operator's real index on every run instead of failing loud.
        stack.enter_context(_swap(index, "count", functools.partial(index.count, db=idx)))
        # The whole read path is real except the two edges that would need a server:
        # the query embedding and the rewrite call. `main()` already wraps the whole
        # suite in `_fake_embed()`, which is what keeps `rank.rank`'s own embed call
        # (`cosine_similarity`, 2026-07-31 finding) from loading a real encoder here
        # — nesting a second one around just this check corrupts `sys.modules` for
        # whichever check runs next (its `finally` pops the module the OUTER one
        # still expects to find there) and is not needed on top of the outer one.
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
                             "k", "returned", "cut_off", "cost", "trust_quota",
                             "untrusted_returned", "untrusted_over_quota"}, sorted(line)
        assert line["log_id"] == body["log_id"] and line["k"] == 2
        # D14: k=2 -> quota floor(0.2*2)=0. `_corpus`'s phase 2 already judged every
        # idea (`report["trust_scored"] == 4`, trust_score 0.5 each) before this
        # call, so the two returned here are trusted and the quota never engages.
        assert line["trust_quota"] == 0, line
        assert line["untrusted_returned"] == 0 and line["untrusted_over_quota"] == 0, line
        assert line["query_rewritten"].endswith("frozen encoder")
        assert line["rewrite_failed"] is False
        assert [r["rank"] for r in line["returned"]] == [1, 2], line["returned"]
        for entry in line["returned"]:
            assert set(entry) == {"idea_id", "score", "raw_score", "cosine_similarity",
                                  "rank", "via"}, entry
            assert entry["via"] in ("thesis", "edge", "padding"), entry
            assert entry["raw_score"] > 0.0, entry
            assert -1.0 <= entry["cosine_similarity"] <= 1.0, entry
        assert line["cut_off"], "4 ideas and k=2 must leave a cut-off tail"
        for entry in line["cut_off"]:
            assert set(entry) == {"idea_id", "score", "raw_score", "cosine_similarity",
                                  "rank"}, entry
        # score is normalized per query, raw_score is not: the threshold curve of
        # §5.5 is built on the second one, so they must not be the same number.
        assert any(abs(e["score"] - e["raw_score"]) > 1e-9
                   for e in line["returned"] + line["cut_off"]), line
        # cosine_similarity must not silently be a copy of `score` either — the whole
        # point (review finding) is that it does NOT renormalize per request.
        assert any(abs(e["score"] - e["cosine_similarity"]) > 1e-9
                   for e in line["returned"] + line["cut_off"]), line
        assert line["cost"] == body["cost"], (line["cost"], body["cost"])

        with _swap(rewrite, "rewrite", lambda query, budget=None: (query, True)):
            api.retrieve("island model migration", k=2)
        assert lines()[-1]["rewrite_failed"] is True, "a degraded rewrite must reach the log"
        assert len(lines()) == 2, lines()


@check(6, "idempotency: the same source through phase 2 twice -> zero new theses")
def check_06(tmp: Path) -> None:
    idx, first = _corpus(tmp)
    leaves = {leaf["text"]: leaf["idea_id"] for leaf in graph_client.all_theses()}
    pair = frozenset((leaves[CORPUS[0]["thesis"]["text"]], leaves[CORPUS[2]["thesis"]["text"]]))
    before = _cocite_edges(list(pair))[pair]
    (tmp / "staging.cursor").write_text("0\n", encoding="utf-8")   # replay from the top
    second, ops, _ = _phase2(tmp, CORPUS, [])                      # no answer may be needed
    assert ops == [], f"a replayed corpus cost {len(ops)} LLM calls"
    assert second["theses_written"] == 0, second
    assert second["theses_skipped"] == 5, second
    assert second["theses"] == first["theses"] == 5, (first, second)
    assert second["ideas"] == first["ideas"] == 4, (first, second)
    assert index.count(db=idx) == 5, index.count(db=idx)
    # D12/M7 (review 2026-07-31): the same source recomputes the same co-citation
    # pair on replay and must not move the weight — `write_cocitation_edges` is
    # idempotent per source because `source_id` is already in `evidence`. Checked
    # on the GRAPH before/after, not on `second["cocitation_pairs"]` alone: that
    # field recomputes to the same number (2) whether or not the write underneath
    # re-accumulated, so a broken idempotency guard would leave it unchanged.
    assert second["cocitation_pairs"] == 2, second
    after = _cocite_edges(list(pair))[pair]
    assert {r["weight"] for r in before} == {r["weight"] for r in after} == {1.0}, (before, after)
    assert {tuple(r["evidence"]) for r in before} == {tuple(r["evidence"]) for r in after}, \
        (before, after)


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
    # D11: `stub_store` is gone, `neo4j_store` is the only module that actually talks
    # to the store now. Parsing `graph_client` alone here would have gone green for
    # the wrong reason the moment the second file disappeared — this must name the
    # module that replaced it, not just drop the second entry from the tuple.
    for module in (graph_client, neo4j_store):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        defined = {node.name for node in ast.walk(tree)
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        defined |= {node.id for node in ast.walk(tree)
                    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
        assert "update_thesis" not in defined, f"{module.__name__} grew an update_thesis"
        assert not hasattr(module, "update_thesis"), module.__name__
    # A direct UPDATE (SQL) or SET (Cypher) would bypass the missing method. On SQLite
    # exactly one used to be legal — `stub_store.split_idea` re-homing a leaf's
    # `idea_id` column. That exception does not exist on Neo4j at all: `split_idea`
    # moves the `HAS_LEAF` edge instead of writing a Thesis property (`neo4j_store`
    # module docstring), so a Thesis node has literally zero legal writes after its
    # own creation. §1.2 immutability is about what the source said (text, context,
    # effect, locator, text_hash, source_id); with `idea_id` gone as a Thesis property
    # too, there is nothing left that a repair could legitimately touch.
    found = 0
    for path in sorted((REPO / "lake").rglob("*.py")):
        if path.name == "selfcheck.py" and path.parent.name == "lake":
            continue
        text = path.read_text(encoding="utf-8")
        for columns in _thesis_update_columns(text):
            found += 1
            raise AssertionError(f"{path}: direct SQL UPDATE on thesis assigns "
                                 f"{sorted(columns) or '(unparsed)'} — no backend has a "
                                 "legal exception left (D11)")
        cypher_hits = _cypher_thesis_sets(text)
        assert not cypher_hits, f"{path}: Cypher SET on a :Thesis-bound variable — {cypher_hits}"
    assert found == 0, f"the SQL-side scan found {found} UPDATE(s) on thesis; want 0 (D11)"
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
    real_insert = neo4j_store._insert_theses

    def boom(tx, source_id, theses):
        # Inside the transaction, after the idea row: exactly the window that would
        # leave IDEA ||--|{ THESIS broken if the two were not one transaction (§3.4).
        # A plain RuntimeError is enough — what is under test is that ANY exception
        # from inside `session.execute_write`'s callback rolls back everything the
        # driver already sent it, same guarantee `sqlite3.OperationalError` proved
        # on the SQLite side, not the exception's type.
        if any(t.text.startswith("mixed precision") for t in theses):
            raise RuntimeError("simulated write failure")
        return real_insert(tx, source_id, theses)

    with _swap(neo4j_store, "_insert_theses", boom):
        try:
            _phase2(tmp, CORPUS, CORPUS_ANSWERS)
        except RuntimeError:
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
        """Everything in this process's memory is dropped; the driver reconnects on
        the next call, and whatever it reads comes back from Neo4j itself, not from
        a cached Python object — the same proof `stub_store._conn.close()` gave by
        dropping SQLite's open handle."""
        neo4j_store.close()

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
        # `13` §3.2: writing a leaf raises `dirty`, and re-derivation deliberately does
        # NOT lower it — only the judge does, together with the score. The flag being
        # still up here is the whole retry mechanism: rewriting what the idea says and
        # deciding what it is worth are separate steps that fail separately.
        assert after["dirty"], "re-derivation must leave the flag for the judge"
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

    # 2026-07-31 finding: `/healthz` already saw this exact drift (`in_sync` false)
    # but nothing checked what `/retrieve` itself answers while it lasts — a wiped
    # index makes `search_theses` return `[]` legitimately (an empty index IS a []
    # answer, self-check §6.12/§6.13), so a store that still holds 5 leaves must not
    # be allowed to look like an empty lake through the read path (§5.4).
    with contextlib.ExitStack() as stack:
        # `index.count` takes `db=INDEX_DB` as a DEFAULT ARGUMENT, bound at def
        # time (the same trap `search.search` has, api/selfcheck.py:109-112) — left
        # unpatched, `retrieve.api.retrieve`'s own guard would read the real
        # `data/index.db` instead of this check's temp one.
        stack.enter_context(_swap(index, "count", functools.partial(index.count, db=idx)))
        stack.enter_context(_swap(rank, "search",
                                  lambda q, qv, top_k=50, _db=idx: index.search_theses(
                                      q, top_k, query_vec=qv, db=_db)))
        stack.enter_context(_swap(rewrite, "rewrite",
                                  lambda query, budget=None: (query, False)))
        stack.enter_context(_swap(api, "RETRIEVE_LOG", tmp / "retrieve.jsonl"))
        status, body = api.retrieve("encoder", k=2, run_id="selfcheck-wiped-index")
        assert status == 503, (status, body)
        assert "diverged" in body["error"] or "empty" in body["error"], body

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
    # `demo()` returns 1 and prints SKIPPED/REFUSED instead of raising when it never
    # touched the graph — a bare call reads that as "ok" the same as a real pass.
    status = vault.demo()
    assert status == 0, f"vault.demo() did not run (status={status}); see captured output"


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
    # `demo()` returns 1 and prints SKIPPED/REFUSED instead of raising when it never
    # touched the graph — a bare call reads that as "ok" the same as a real pass.
    status = split.demo()
    assert status == 0, f"split.demo() did not run (status={status}); see captured output"


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


@check(24, "durable queue + writer: enqueue -> fetch_step -> staged -> write_step -> ok "
           "with both phases in the report; exactly one phase-2 writer at a time, the "
           "busy one loses no attempt; MAX_ATTEMPTS gives up on a transient failure and "
           "one attempt on a permanent one; phase 2 refuses to run beside another process")
def check_24(tmp: Path) -> str:
    # The real workers.fetch_step/write_step, not copies. Only what they reach for is
    # redirected: the queue's own db, the per-job staging directory, phase 1 (network
    # and the 9B/35B models), and — inside phase 2 — the store, the index and the
    # arbiter, the same way `_phase2` binds them for `run.phase2` above.
    idx = _open(tmp)
    fake, _ = _arbiter([])            # one leaf, no candidates: zero arbiter calls

    def fixture_stage_one(entry: dict, staging_path) -> dict:
        """Phase 1, faked: one staging line, no fetch, no parser, no LLM call."""
        staging_path = Path(staging_path)
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [_row("s1", "freezing the encoder before finetuning keeps 3.1 pp of "
                          "accuracy", FREEZE)]
        staging_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                                        for r in rows), encoding="utf-8")
        return {"staging_lines": len(rows), "leakage": 0, "theses_dropped": 0,
                "source": entry["arxiv_id"]}

    jobs._reset_for_tests()
    try:
        with contextlib.ExitStack() as stack:
            stack.callback(queue.close)
            stack.enter_context(_swap(queue, "DB", tmp / "jobs.db"))
            stack.enter_context(_swap(workers, "FETCH_DIR", tmp / "fetch"))
            stack.enter_context(_swap(run, "stage_one", fixture_stage_one))
            stack.enter_context(_swap(llm, "complete", fake))
            stack.enter_context(_swap(llm, "assert_grammar_works", lambda model: None))
            stack.enter_context(_swap(index, "index_theses",
                                      functools.partial(index.index_theses, db=idx)))
            stack.enter_context(_swap(index, "has", functools.partial(index.has, db=idx)))
            stack.enter_context(_swap(index, "index_rows",
                                      functools.partial(index.index_rows, db=idx)))
            stack.enter_context(_swap(index, "stale_links",
                                      functools.partial(index.stale_links, db=idx)))
            stack.enter_context(_swap(index, "reconcile",
                                      functools.partial(index.reconcile, db=idx)))
            stack.enter_context(_swap(link, "link_batch",
                                      functools.partial(link.link_batch, index_db=idx,
                                                        pending_path=tmp / "pending_link.jsonl")))

            # --- 1. the full path: queued -> staged -> ok, with a real report -------
            job = queue.enqueue("fetch", {"arxiv_id": "s1"})
            assert job["status"] == "queued" and job["attempts"] == 0, job
            assert workers.fetch_step() is True, "one queued job must be taken"
            staged = queue.get(job["id"])
            assert staged["status"] == "staged", staged
            assert staged["report"]["staging_lines"] == 1, staged["report"]
            assert workers.write_step() is True, "one staged job must be taken"
            done = queue.get(job["id"])
            assert done["status"] == "ok", done
            assert done["report"], "an ok job must carry a non-empty report"
            assert done["report"]["theses_written"] == 1, done["report"]
            # One claim, not two: `stage()` resets the counter, because the two phases
            # are two pieces of work and an article that needed a second fetch would
            # otherwise enter the writer's queue with one life left.
            assert done["attempts"] == 1, done
            # What phase 1 measured survives into the final report, and the cost is both
            # halves summed. Dropping either one reports the linking as the whole article.
            assert done["report"]["leakage"] == 0 and done["report"]["theses_dropped"] == 0, \
                done["report"]
            assert set(done["report"]["cost"]) == {"tokens_in", "tokens_out", "wall_ms"}, \
                done["report"]["cost"]
            assert index.count(db=idx) == 1, index.count(db=idx)
            assert len(graph_client.all_theses()) == 1, graph_client.all_theses()

            # --- 2. exactly one writer: the slot is held, the second write_step must
            #        not enter phase 2, and the job it holds must not lose an attempt.
            job2 = queue.enqueue("fetch", {"arxiv_id": "s5"})
            assert workers.fetch_step() is True
            before = queue.get(job2["id"])
            assert before["status"] == "staged", before
            with jobs.exclusive("fetch", {"who": "selfcheck"}):
                took = workers.write_step()
            assert took is False, "a busy phase-2 slot is not work done"
            after = queue.get(job2["id"])
            assert after["status"] == "staged", \
                "a busy writer must put the job back to staged, not leave it running"
            assert after["attempts"] == before["attempts"] == 0, \
                "release() must not spend the attempt of a job that was never run"
            assert after["stage"] == "phase1", \
                "a released job must not keep the stage it was claimed for"
            assert index.count(db=idx) == 1, "the busy writer must not have touched the graph"

            # --- 3. a TRANSIENT phase-2 failure is retried, MAX_ATTEMPTS gives up ----
            def boom_drain(staging_path, staged=None):
                raise RuntimeError("boom: phase 2 exploded")

            for expected in range(1, queue.MAX_ATTEMPTS):
                with _swap(run, "drain_one", boom_drain):
                    assert workers.write_step() is True
                once = queue.get(job2["id"])
                assert once["status"] == "staged", \
                    "a failed phase 2 must return the job to staged, not lose it"
                assert "boom" in (once["error"] or ""), once
                assert once["attempts"] == expected, once    # retried, not given up

            with _swap(run, "drain_one", boom_drain):
                assert workers.write_step() is True
            failed = queue.get(job2["id"])
            assert failed["status"] == "failed", failed
            assert failed["attempts"] == queue.MAX_ATTEMPTS, failed
            assert "giving up" in failed["error"], failed["error"]
            # A failed job is not queued work any more: claim() must not hand it out.
            assert queue.claim("staged", "phase2") is None, \
                "a failed job is still claimable — it disappeared without a trace"

            # --- 3a. the terminal write itself fails (a full disk, a read-only mount).
            #         The graph is already written at that point, so a job left `running`
            #         says "in progress" forever and comes back from the next restart as
            #         `failed` — for work that succeeded.
            job_fin = queue.enqueue("fetch", {"arxiv_id": "s5b"})
            assert workers.fetch_step() is True
            calls: list[int] = []

            def finish_once(*args, **kwargs):
                calls.append(1)
                if len(calls) == 1:
                    raise sqlite3.OperationalError("attempt to write a readonly database")
                return queue_finish(*args, **kwargs)

            queue_finish = queue.finish
            with _swap(queue, "finish", finish_once):
                assert workers.write_step() is True
            after_fin = queue.get(job_fin["id"])
            assert after_fin["status"] != "running", \
                "a failed finish() left the job running: the article is in the lake and " \
                "the row says work in progress until the process restarts"
            # Terminal, and the message carries both facts. Not put back to `staged`:
            # the staging file is gone, so a replay would burn three attempts on a
            # missing file and blame that instead.
            assert after_fin["status"] == "failed", after_fin
            assert "the graph has this article" in after_fin["error"], after_fin
            assert "readonly" in after_fin["error"], after_fin
            assert len(calls) == 2, calls        # the failed write, then the honest one
            # Phase 2 really did finish: `drain_one` deletes the staging file only after
            # the ingest went through, and that deletion is why the row must not be put
            # back as retryable work.
            assert not (tmp / "fetch" / "s5b.jsonl").exists(), \
                "phase 2 did not complete, so this proves nothing about a failed finish()"

            # --- 3b. a PERMANENT failure fails once. Three fetch/parse rounds to
            #         relearn "this article has no HTML" cost the pool three rounds and
            #         bury the reason under "attempt 3 of 3".
            from .ingest.fetch import FetchError

            def dead_source(entry, staging_path):
                raise FetchError(f"{entry['arxiv_id']}: no sections anywhere")

            job3 = queue.enqueue("fetch", {"arxiv_id": "s6"})
            with _swap(run, "stage_one", dead_source):
                assert workers.fetch_step() is True
            dead = queue.get(job3["id"])
            assert dead["status"] == "failed", dead
            assert dead["attempts"] == 1, f"a permanent error burned {dead['attempts']} attempts"
            assert "permanent" in dead["error"] and "no sections" in dead["error"], dead
            # --- 3d. the arbiter refuses every thesis, and the RETRY must not read as ok
            # Attempt 1 raises: every thesis is in `pending_link` and the graph has
            # nothing. But it also advanced the cursor over the whole group, so attempt 2
            # processes no group at all and every counter is zero — which a guard reading
            # this run's counters called success: staging file deleted, job `ok`, article
            # nowhere. The store is what decides now.
            def refusing(source_id, rows):
                return [{"thesis": None, "idea": None, "skipped": True,
                         "reason": "pending_link: LLMError: server said no"} for _ in rows]

            def stage_s3(entry, staging_path) -> dict:
                staging_path = Path(staging_path)
                staging_path.parent.mkdir(parents=True, exist_ok=True)
                row = _row("s3", "a replication run reproduces the 3.1 pp gain", FREEZE)
                staging_path.write_text(json.dumps(row, ensure_ascii=False) + "\n",
                                        encoding="utf-8")
                return {"staging_lines": 1, "leakage": 0, "theses_dropped": 0,
                        "source": entry["arxiv_id"]}

            job_ref = queue.enqueue("fetch", {"arxiv_id": "s3ref"})
            with _swap(link, "link_batch", refusing), _swap(run, "stage_one", stage_s3):
                assert workers.fetch_step() is True
                # Attempt 1 is caught by this run's counters ("refused all 1 of 1");
                # attempt 2 has none — the cursor is spent, so it processes no group and
                # every counter is zero — and only the store can still say no.
                for attempt, expected in ((1, "refused all 1 of 1"),
                                          (2, "no leaf for this source")):
                    assert workers.write_step() is True
                    row = queue.get(job_ref["id"])
                    assert row["status"] != "ok", (
                        f"attempt {attempt} answered ok for an article whose every thesis "
                        f"the arbiter refused: {row}")
                    assert expected in (row["error"] or ""), (attempt, row)
            assert graph_client.count_theses(source_id=_sid("s3")) == 0, \
                "a refused thesis reached the graph"
            assert (tmp / "fetch" / "s3ref.jsonl").exists(), \
                "the staging file of a refused article was deleted — that work is the " \
                "only record of it and re-posting the url is what replays it"

    finally:
        jobs._reset_for_tests()

    # --- 3c. zero work must not read as success, and cost is both halves -------------
    empty = tmp / "fetch" / "empty.jsonl"
    empty.parent.mkdir(parents=True, exist_ok=True)
    empty.write_text("", encoding="utf-8")
    try:
        run.drain_one(empty)
    except RuntimeError as exc:
        assert "no staging lines" in str(exc), exc
    else:
        raise AssertionError("an empty staging file went through phase 2 and reported ok")
    # Summed, not overwritten: the writer's counter starts at zero, so the phase-1 half
    # (fetch, parse, generalize, embed) is the whole cost of an article minus the linking.
    assert workers._merge_cost({"tokens_in": 3, "tokens_out": 4, "wall_ms": 1.5},
                               {"tokens_in": 2, "tokens_out": 0, "wall_ms": 0.5}) == \
        {"tokens_in": 5, "tokens_out": 4, "wall_ms": 2.0}, "phase 1 cost was dropped"

    # --- 4. the writer lock is on the phase-2 PATH, and another process is refused ---
    # In-process re-entry cannot prove this: `flock` is per open file description, so a
    # second `open()` here is refused exactly like a neighbour would be — and the writer
    # thread itself nests `phase2` inside a lock this process already holds. So: assert
    # the lock is held when phase 2 runs, then let a real second process hold it.
    import subprocess

    seen_depth: list[int] = []
    with _swap(writer_lock, "LOCK_PATH", tmp / "writer.lock"), \
            _swap(run, "_phase2", lambda staging_path, limit: seen_depth.append(
                writer_lock.depth()) or {"theses_written": 0}):
        run.phase2(tmp / "nothing.jsonl")
        assert seen_depth == [1], f"phase 2 ran outside the writer lock: {seen_depth}"
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl, sys\n"
             "fh = open(sys.argv[1], 'a+')\n"
             "fcntl.flock(fh.fileno(), fcntl.LOCK_EX)\n"
             "print('held', flush=True)\n"
             "sys.stdin.readline()\n", str(tmp / "writer.lock")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            assert holder.stdout.readline().strip() == "held", "the holder never locked"
            try:
                run.phase2(tmp / "nothing.jsonl")
            except writer_lock.SecondWriter:
                pass
            else:
                raise AssertionError("phase 2 ran while another PROCESS held the writer "
                                     "lock — §4.5 says one writer per lake")
            assert seen_depth == [1], "the refused run reached the body anyway"
        finally:
            holder.stdin.close()
            holder.wait(timeout=30)

    return ("enqueue -> fetch_step -> staged -> write_step -> ok with both phases in the "
            "report and cost; a busy writer keeps the attempt and drops the stage; "
            f"{queue.MAX_ATTEMPTS} transient failures give up, one permanent failure is "
            "final; phase 2 runs under the writer lock and refuses a second process")


@check(25, "the threads are really started and really loop: start() takes the writer "
           "lock BEFORE it recovers, a queued article goes to ok with nobody driving "
           "the steps, alive() answers for the threads that exist, and stop() keeps the "
           "lock while the writer is still inside phase 2")
def check_25(tmp: Path) -> str:
    # Check 24 drives `fetch_step`/`write_step` by hand, which leaves the whole
    # lifecycle — `start`, `stop`, `_loop`, `alive` — unexercised: `if False:` around
    # the thread creation kept every check in this suite green. Here nothing is driven;
    # the threads have to do it. Phase 1 and phase 2 are both faked, so this check is
    # about the machinery and never opens the graph.
    import subprocess
    import time

    done: list[str] = []
    gate = threading.Event()
    gate.set()

    def fake_stage_one(entry: dict, staging_path) -> dict:
        return {"staging_lines": 1, "leakage": 0, "theses_dropped": 0,
                "source": entry["arxiv_id"]}

    def fake_drain_one(staging_path, staged=None) -> dict:
        assert gate.wait(30), "the writer was never let out of phase 2"
        done.append(str(staging_path))
        return {"sources_processed": 1, "theses_written": 1, "staging_lines": 1}

    def wait_for(what, deadline: float = 20.0):
        """Poll instead of sleeping a fixed time: a wrong guess is either a flaky check
        or a slow one, and both are worse than a condition."""
        until = time.monotonic() + deadline
        while time.monotonic() < until:
            value = what()
            if value:
                return value
            time.sleep(0.05)
        return None

    jobs._reset_for_tests()
    try:
        with contextlib.ExitStack() as stack:
            stack.callback(queue.close)
            stack.callback(lambda: workers.stop(timeout=10))
            stack.enter_context(_swap(queue, "DB", tmp / "jobs.db"))
            stack.enter_context(_swap(workers, "FETCH_DIR", tmp / "fetch"))
            stack.enter_context(_swap(workers, "POLL_S", 0.05))
            stack.enter_context(_swap(writer_lock, "LOCK_PATH", tmp / "writer.lock"))
            stack.enter_context(_swap(run, "stage_one", fake_stage_one))
            stack.enter_context(_swap(run, "drain_one", fake_drain_one))

            # --- 1. the order inside start(): the lock, and only then the queue -------
            # `recover()` declares every `running` row dead. A second process doing that
            # before it asks for the lock requeues the jobs the FIRST one is running —
            # the same article ingested twice, and the duplicate is refused a lock it
            # should have taken before touching anything.
            mid_flight = queue.enqueue("fetch", {"arxiv_id": "s7"})
            assert queue.claim("queued", "phase1")["id"] == mid_flight["id"]
            holder = subprocess.Popen(
                [sys.executable, "-c",
                 "import fcntl, sys\n"
                 "fh = open(sys.argv[1], 'a+')\n"
                 "fcntl.flock(fh.fileno(), fcntl.LOCK_EX)\n"
                 "print('held', flush=True)\n"
                 "sys.stdin.readline()\n", str(tmp / "writer.lock")],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                assert holder.stdout.readline().strip() == "held", "the holder never locked"
                try:
                    workers.start(fetch_workers=1)
                except workers.SecondWriter:
                    pass
                else:
                    raise AssertionError("start() came up beside another writer")
                assert queue.get(mid_flight["id"])["status"] == "running", \
                    "recover() ran before the lock was taken: a refused process requeued " \
                    "work that the process holding the lock is running right now"
                assert workers.alive() == {}, "the refused start left threads behind"
            finally:
                holder.stdin.close()
                holder.wait(timeout=30)

            # --- 2. now it starts, and recovery happens because the lock is ours ------
            started = workers.start(fetch_workers=1)
            assert sorted(started["threads"]) == ["fetch0", "writer"], started
            assert started["recovered"]["queued"] == 1, started
            assert workers.alive() == {"writer": True, "fetch0": True}, workers.alive()

            # --- 3. the loops do the work: nothing below drives a step ---------------
            queue.enqueue("fetch", {"arxiv_id": "s8"})
            counts = wait_for(lambda: queue.counts()["ok"] == 2 and queue.counts())
            assert counts, f"the threads never drained the queue: {queue.counts()}"
            assert len(done) == 2, done
            assert {Path(p).name for p in done} == {"s7.jsonl", "s8.jsonl"}, done
            assert Path(done[0]).parent == tmp / "fetch", done

            # --- 4. stop() while the writer is inside phase 2 ------------------------
            gate.clear()
            queue.enqueue("fetch", {"arxiv_id": "s9"})
            # By STAGE, not by `running`: `counts()` counts a status, and the few
            # microseconds s9 spends claimed by the fetch worker are `running` too. Waiting
            # on that let `stop()` fire before the writer was inside phase 2, and the check
            # then failed on the unmutated code about half the time — a check that is red
            # for its own reasons cannot say anything about the guard it is aimed at.
            assert wait_for(lambda: len(done) == 2 and any(
                row["status"] == "running" and row["stage"] == "phase2"
                for row in queue.listing())), \
                f"the writer never entered phase 2: {queue.listing()[:3]}"
            workers.stop(timeout=0.2)
            assert workers.alive().get("writer") is True, \
                "stop() dropped a writer that is still linking from alive(): /healthz " \
                "then reports a stall for a live ingest"
            assert writer_lock.depth() == 1, \
                "stop() released the writer lock while the writer was still in phase 2 " \
                "— the next process would come up as a second writer (§4.5)"
            gate.set()
            assert wait_for(lambda: not any(workers.alive().values())), \
                f"the writer never finished after the gate opened: {workers.alive()}"
            workers.stop(timeout=10)
            assert writer_lock.depth() == 0, "the lock outlived the writer"
            assert queue.get(queue.listing()[0]["id"])["status"] == "ok", queue.listing()[0]
    finally:
        jobs._reset_for_tests()

    return ("a refused start() moves no row and starts no thread; the started pool takes "
            "2 articles to ok on its own; alive() answers for both threads; stop() keeps "
            "the lock and the writer visible until phase 2 ends")


# --------------------------------------------------------------- runlog fixtures (`13`)

# Only the columns `runlog.payload_from_csv` actually reads (`runlog.py:274-285`) —
# the real GigaEvo CSV carries ~30, the converter needs 8 of them.
_RUNLOG_FIELDS = ["program_id", "parent_ids", "state", "generation", "iteration",
                  "metric_fitness", "metadata_mutation_model", "metadata_mutation_output"]


def _runlog_row(program_id: str, parent_ids: list, fitness: str, mutation_output: str,
                state: str = "discarded") -> dict:
    return {"program_id": program_id, "parent_ids": json.dumps(parent_ids), "state": state,
            "generation": "1", "iteration": "1", "metric_fitness": fitness,
            "metadata_mutation_model": "qwen3.6-35b-a3b",
            "metadata_mutation_output": mutation_output}


def _runlog_mo(archetype: str = "Precision Optimization", changes: list | None = None,
              code: str = "def helper_fn(): pass") -> str:
    """`metadata_mutation_output`, structured (`13` §1.1) — the JSON string as it sits
    in the CSV cell, not yet parsed."""
    return json.dumps({"archetype": archetype, "justification": "because",
                       "insights_used": ["[tag] some insight"],
                       "changes": changes if changes is not None else
                       [{"description": "cascade a cheap check first",
                         "explanation": "cuts wasted expensive calls"}],
                       "code": code})


def _fake_runlog_generalize(prompt, *, system, schema, op, max_tokens, timeout,
                            model=None, temperature=0.0):
    """`llm.complete` scripted for `generalize_mod.generalize` alone (op="generalize")
    — checks 26/27 never reach the link arbiter or the trust judge through this fake,
    an empty index and a single new idea cost neither (see each check's own assert)."""
    assert op == "generalize", op
    lever = prompt.split("THESIS\n", 1)[1].splitlines()[0]
    return {"text": f"generalized: {lever}", "applicability_conditions": "ac",
            "limitations": "lim", "failure_modes": []}


@check(26, "runlog converter: a 6-row CSV — three drop reasons stay three separate "
           "counters, -1000 never reaches effect, staging shape is complete, locator "
           "reverses to its CSV row, the title carries no fitness, and a batch with no "
           "usable changes[] raises through the real converter, not a stub")
def check_26(tmp: Path) -> str:
    """§10 point 26. One ordinary mutant converts; the other five (dead validation,
    empty fitness, root without a parent, unparseable mutation_output, empty
    changes[]) contribute nothing — and WHY each contributed nothing is a separate
    number, not one summed "skipped" (`13` §9 p.3, p.12)."""
    rows_csv = [
        _runlog_row("dead1", ["root1"], "-1000.0", _runlog_mo()),
        _runlog_row("nofit1", ["root1"], "", _runlog_mo(), state="running"),
        _runlog_row("root1", [], "0.4", _runlog_mo(), state="done"),
        _runlog_row("broken1", ["root1"], "0.6", "{not json"),
        _runlog_row("nochg1", ["root1"], "0.5", _runlog_mo(changes=[])),
        _runlog_row("normal1", ["root1"], "0.7", _runlog_mo(changes=[
            {"description": "prefilter with a cheap proxy", "explanation": "saves calls"},
            {"description": "reorder validation steps", "explanation": "fails fast"}])),
    ]
    csv_path = tmp / "evolution_full.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_RUNLOG_FIELDS)
        writer.writeheader()
        writer.writerows(rows_csv)

    staging_path = tmp / "run" / "unit_seed9.jsonl"
    with contextlib.ExitStack() as stack:
        stack.enter_context(_swap(llm, "complete", _fake_runlog_generalize))
        stack.enter_context(_swap(llm, "assert_grammar_works", lambda model: None))
        report = runlog.from_csv(csv_path, staging_path=staging_path, run_id="unit_seed9")

    # -- three drop reasons, three counters — folding them into one would still read
    # green on a batch that lost track of which failure mode is which -------------
    assert report["dropped_dead"] == 1, report
    assert report["dropped_no_fitness"] == 1, report
    assert report["dropped_root"] == 1, report
    assert report["rows_unparsed"] == 1, \
        "the broken metadata_mutation_output row must be counted, not silently dropped"
    assert report["mutants_no_changes"] == 1, report
    assert report["mutants_converted"] == 1 and report["run_theses"] == 2, report

    lines = [json.loads(ln) for ln in staging_path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2, lines
    # -- an unparseable/dropped row never reaches a `run_success` decision at all
    # (`runlog.py:12-15`): none of the five problem mutants produces a Source, so the
    # closest observable form of "run_success is None" for them is total absence
    # from staging — checked here as their program_ids never appearing in a locator.
    locators = {ln["thesis"]["locator"] for ln in lines}
    for bad_id in ("dead1", "nofit1", "root1", "broken1", "nochg1"):
        assert all(bad_id not in loc for loc in locators), (bad_id, locators)

    for i, ln in enumerate(lines):
        assert set(ln) == {"source", "section_id", "thesis", "draft", "idea_fields",
                           "vector"}, sorted(ln)
        assert set(ln["draft"]) == {"draft_text", "draft_applicability", "draft_limitations"}
        assert set(ln["idea_fields"]) == {"text", "applicability_conditions", "limitations",
                                          "failure_modes"}
        assert len(ln["vector"]) == EMBED_DIM, len(ln["vector"])
        # -- -1000 (the validation-timeout marker, §1.3) never reaches `effect`: the
        # only path there is through a delta, and every dropped row above is filtered
        # before a delta is ever computed.
        assert "-1000" not in ln["thesis"]["effect"], ln["thesis"]["effect"]
        # -- locator is reversible to its CSV row (run id, program id, changes index)
        run_part, rest = ln["thesis"]["locator"].split(":", 1)
        program_id, idx_part = rest.split("#changes[")
        assert run_part == "unit_seed9" and program_id == "normal1", ln
        assert idx_part == f"{i}]", ln
        # -- title carries no fitness number (§2.1 revision: `write_source` is INSERT
        # OR REPLACE, and a title that varies with fitness would rewrite the source
        # row on every re-fetch of the same mutant, `stub_store.py:79-86`)
        meta, title = ln["source"]["run_meta"], ln["source"]["title"]
        assert not any(str(v) in title for v in
                       (meta["fitness"], meta["parent_fitness"], meta["fitness_delta"])), title

    # -- second fixture: the 6-row batch above gives dropped_dead/no_fitness/root
    # the SAME count (1/1/1), so a permutation of those three counters is invisible
    # to every assert above — it just relabels three identical 1s. This one uses
    # three DIFFERENT counts (2/3/1) so a permutation changes the numbers, not only
    # their names. It also carries four converting mutants of both delta signs and
    # four distinct |delta| magnitudes, driven through a default call, a non-default
    # `min_abs_delta` call and a non-default `limit` call — the one drive above never
    # exercises any of the three (kept[:limit], the min_abs_delta filter, the
    # |delta|-descending sort) because it has exactly one convertible candidate, for
    # which every one of those three steps is a no-op.
    sel_mo = lambda desc: _runlog_mo(changes=[{"description": desc, "explanation": "e"}])
    sel_payload = {"run_id": "unit_sel", "task_id": "unit", "mutants": [
        {"program_id": "base", "parent_ids": [], "state": "done", "generation": 1,
         "iteration": 1, "fitness": 0.50, "parent_fitness": None, "mutation_model": "m",
         "mutation_output_raw": _runlog_mo()},
        # insertion order deliberately NOT the descending-|delta| order below, so a
        # sort that silently turned into a no-op (candidate/CSV order) is observable.
        {"program_id": "m_small", "parent_ids": ["base"], "state": "done", "generation": 1,
         "iteration": 1, "fitness": 0.53, "parent_fitness": None, "mutation_model": "m",
         "mutation_output_raw": sel_mo("d_small")},                       # delta +0.03
        {"program_id": "m_neg", "parent_ids": ["base"], "state": "done", "generation": 1,
         "iteration": 1, "fitness": 0.30, "parent_fitness": None, "mutation_model": "m",
         "mutation_output_raw": sel_mo("d_neg")},                         # delta -0.20
        {"program_id": "m_mid", "parent_ids": ["base"], "state": "done", "generation": 1,
         "iteration": 1, "fitness": 0.75, "parent_fitness": None, "mutation_model": "m",
         "mutation_output_raw": sel_mo("d_mid")},                         # delta +0.25
        {"program_id": "m_big", "parent_ids": ["base"], "state": "done", "generation": 1,
         "iteration": 1, "fitness": 0.95, "parent_fitness": None, "mutation_model": "m",
         "mutation_output_raw": sel_mo("d_big")},                         # delta +0.45
        {"program_id": "dead_a", "parent_ids": ["base"], "state": "discarded",
         "generation": 1, "iteration": 1, "fitness": -1000.0, "parent_fitness": None,
         "mutation_model": "m", "mutation_output_raw": _runlog_mo()},
        {"program_id": "dead_b", "parent_ids": ["base"], "state": "discarded",
         "generation": 1, "iteration": 1, "fitness": -1000.0, "parent_fitness": None,
         "mutation_model": "m", "mutation_output_raw": _runlog_mo()},
        {"program_id": "nofit_a", "parent_ids": ["base"], "state": "running",
         "generation": 1, "iteration": 1, "fitness": None, "parent_fitness": None,
         "mutation_model": "m", "mutation_output_raw": _runlog_mo()},
        {"program_id": "nofit_b", "parent_ids": ["base"], "state": "running",
         "generation": 1, "iteration": 1, "fitness": None, "parent_fitness": None,
         "mutation_model": "m", "mutation_output_raw": _runlog_mo()},
        {"program_id": "nofit_c", "parent_ids": ["base"], "state": "running",
         "generation": 1, "iteration": 1, "fitness": None, "parent_fitness": None,
         "mutation_model": "m", "mutation_output_raw": _runlog_mo()},
    ]}

    def _sel_run(**kwargs):
        with contextlib.ExitStack() as stack:
            stack.enter_context(_swap(llm, "complete", _fake_runlog_generalize))
            stack.enter_context(_swap(llm, "assert_grammar_works", lambda model: None))
            return runlog.from_payload(sel_payload, **kwargs)

    staging_default = tmp / "run" / "unit_sel_default.jsonl"
    report_default = _sel_run(staging_path=staging_default)
    # -- three DISTINCT drop counts: dead<-root/no_fitness<-dead/root<-no_fitness
    # (any permutation of the triple below) changes at least one of these numbers,
    # unlike a 1/1/1 fixture where every permutation reads identical.
    assert report_default["dropped_dead"] == 2, report_default
    assert report_default["dropped_no_fitness"] == 3, report_default
    assert report_default["dropped_root"] == 1, report_default
    assert len({report_default["dropped_dead"], report_default["dropped_no_fitness"],
                report_default["dropped_root"]}) == 3, \
        "the fixture itself must give the three drop counters distinct values, or a " \
        "permutation of the report's keys is invisible"
    assert report_default["mutants_converted"] == 4, report_default
    assert report_default["limit"] == -1, "no limit passed -> sentinel, not None/absent"

    default_lines = [json.loads(ln) for ln in
                      staging_default.read_text(encoding="utf-8").splitlines()]
    # -- descending |delta|, not CSV/candidate insertion order (m_small, m_neg, m_mid,
    # m_big) — this is what actually proves `kept.sort(...)` still runs, not merely
    # that the right COUNT of mutants converted.
    assert [ln["source"]["run_meta"]["program_id"] for ln in default_lines] == \
        ["m_big", "m_mid", "m_neg", "m_small"], \
        "selected must be sorted by descending |delta|, not left in candidate order"
    # -- run_success per mutant, checked against a KNOWN delta sign for each one:
    # `delta > 0` inverted to `delta < 0` flips m_big/m_mid/m_small (all positive,
    # would read False) and m_neg (negative, would read True) — every one of the
    # four disagrees with its inverted value, so the inversion cannot hide behind
    # any single mutant's sign.
    run_success_by_pid = {ln["source"]["run_meta"]["program_id"]: ln["source"]["run_success"]
                          for ln in default_lines}
    assert run_success_by_pid == {"m_big": True, "m_mid": True, "m_neg": False,
                                  "m_small": True}, run_success_by_pid

    staging_min = tmp / "run" / "unit_sel_min.jsonl"
    report_min = _sel_run(staging_path=staging_min, min_abs_delta=0.1)
    assert report_min["min_abs_delta"] == 0.1, report_min
    assert report_min["dropped_min_delta"] == 1, report_min
    assert report_min["mutants_converted"] == 3, report_min
    min_pids = {json.loads(ln)["source"]["run_meta"]["program_id"]
               for ln in staging_min.read_text(encoding="utf-8").splitlines()}
    assert min_pids == {"m_big", "m_mid", "m_neg"}, min_pids
    assert "m_small" not in min_pids, \
        "|delta|=0.03 < min_abs_delta=0.1 must be filtered out of staging, not just counted"

    staging_limit = tmp / "run" / "unit_sel_limit.jsonl"
    report_limit = _sel_run(staging_path=staging_limit, limit=2)
    assert report_limit["limit"] == 2, report_limit
    assert report_limit["mutants_converted"] == 2, report_limit
    limit_pids = {json.loads(ln)["source"]["run_meta"]["program_id"]
                 for ln in staging_limit.read_text(encoding="utf-8").splitlines()}
    assert limit_pids == {"m_big", "m_mid"}, (
        "limit=2 after a descending-|delta| sort must keep the two BIGGEST movers "
        f"(m_big, m_mid), not the first two in candidate/CSV order: {limit_pids}")

    # -- MAJOR 3 (`13` §2.5, §9 p.3): a batch with no usable changes[] anywhere must
    # raise, not report `ok` with zero theses. Driven through the REAL
    # `runlog.from_payload` — the API suite's terminal-failure check (`api/selfcheck.py`,
    # "dying_from_payload") stubs this exact function to raise a canned ValueError, which
    # proves the queue/worker wiring around a raise but nothing about whether the
    # converter itself still raises. This is the other half: no stub, the actual
    # drop-reason accounting deciding whether `lines` ends up empty. Two distinct
    # empty-batch shapes get two distinct messages (`runlog.py:456-479`), so deleting
    # either raise — or collapsing both into one message — must turn this red.
    payload_measure_empty = {"run_id": "empty_measure_sc", "task_id": "t", "mutants": [
        {"program_id": "d1", "parent_ids": ["r1"], "state": "discarded",
         "generation": 1, "iteration": 1, "fitness": runlog.DEAD_FITNESS,
         "parent_fitness": None, "mutation_model": "m",
         "mutation_output_raw": _runlog_mo()},
    ]}
    try:
        with contextlib.ExitStack() as stack:
            stack.enter_context(_swap(llm, "complete", _fake_runlog_generalize))
            stack.enter_context(_swap(llm, "assert_grammar_works", lambda model: None))
            runlog.from_payload(payload_measure_empty, staging_path=None)
    except ValueError as exc:
        assert "dropped before measurement" in str(exc), exc
    else:
        raise AssertionError(
            "a batch with nothing past measurement must raise, not report ok with zeros")

    payload_no_changes = {"run_id": "empty_changes_sc", "task_id": "t", "mutants": [
        {"program_id": "n1", "parent_ids": ["r1"], "state": "done",
         "generation": 1, "iteration": 1, "fitness": 0.7, "parent_fitness": 0.4,
         "mutation_model": "m", "mutation_output_raw": _runlog_mo(changes=[])},
    ]}
    try:
        with contextlib.ExitStack() as stack:
            stack.enter_context(_swap(llm, "complete", _fake_runlog_generalize))
            stack.enter_context(_swap(llm, "assert_grammar_works", lambda model: None))
            runlog.from_payload(payload_no_changes, staging_path=None)
    except ValueError as exc:
        assert "carried a changes" in str(exc), exc
        assert "dropped before measurement" not in str(exc), \
            "the two empty-batch causes must not share one message"
    else:
        raise AssertionError(
            "a batch that parsed but carried no changes[] must raise, not report ok")

    return ("6 rows -> 1 converted, 2 theses; dropped_dead/no_fitness/root/"
            "rows_unparsed/no_changes each their own counter; -1000 absent from "
            "effect; locator/title/staging shape all hold; second fixture (2/3/1 "
            "drop counts, 4 converting mutants, both delta signs, non-default limit "
            "and min_abs_delta) proves the selection half and run_success sign; a "
            "batch with no usable changes[] anywhere raises through the REAL "
            "converter, with the two empty-batch causes kept distinguishable")


@check("26a", "run-log leakage: extra_terms (program_id / code name / router class) "
              "is caught for a `run` source, clean through the paper-shaped check "
              "without them, precise about English prose, and wired for real in "
              "`_one_source`")
def check_26a(tmp: Path) -> str:
    """§10 point 26a. `runlog.leak_terms` (`13` §2.2.1) is the third rule
    `generalize.leakage` gets for a log — this proves it actually catches something,
    not merely that it exists: the mutation is handing the identical (draft, out)
    pair to the plain paper check and requiring CLEAN, which is what shows
    `extra_terms` is doing the work rather than decorating an already-strict check.

    Two more mutations a review found by reverting fixes and watching nothing here
    go red are closed in the same check, same theme (leak-term precision, `13` §9
    p.9):

    * `_code_names` reverted to its earlier prose-eating regex leaves every OTHER
      check green — the term count only balloons on the three real ground-truth
      CSVs, which nothing in this suite reads. The property that actually matters
      (`_is_leak_shaped`, `runlog.py:84-90`) is asserted directly: ordinary
      English, ALL-CAPS emphasis included, is not a leaked identifier.
    * `_one_source` (phase 1, `run.py:125`) passes `extra_terms` for a `run`
      source, but `run.py`'s own fixture for that line has `run_meta=None` — both
      arms of the ternary return `()` for it, so the wiring could be deleted and
      that fixture would not notice. Driven here with a `run_meta` that actually
      carries a program_id and a code name.
    """
    code = "class MultiModelRouter:\n    def route_call(self): pass\n"
    source = {"run_meta": {
        "program_id": "eab12c3d", "run": "aime_seed1", "task": "aime",
        "mutation_model": "qwen3.6-35b-a3b",
        "mutant_code_names": runlog._code_names(code)}}
    terms = runlog.leak_terms(source)
    assert {"eab12c3d", "MultiModelRouter", "route_call"} <= set(terms), terms

    probe = DraftThesis(text="t", context="a benchmark run", effect="e", locator="l",
                        draft_text="d", draft_applicability="a", draft_limitations="l")
    leaky = IdeaFields(
        text="the fix routes through MultiModelRouter.route_call for program eab12c3d",
        applicability_conditions="", limitations="", failure_modes=[])

    caught = generalize.leakage(probe, leaky, extra_terms=terms)
    assert any("eab12c3d" in v for v in caught), caught
    assert any("MultiModelRouter" in v for v in caught), caught
    assert any("route_call" in v for v in caught), caught

    # The mutation that matters: the identical pair through the paper-shaped check
    # (no extra_terms) must come back CLEAN — if it also caught this, extra_terms
    # would be redundant and a dropped `extra_terms=` argument would go unnoticed.
    paper_only = generalize.leakage(probe, leaky)
    assert paper_only == [], (
        "the paper check alone caught a run-shaped leak too, so extra_terms proves "
        f"nothing here: {paper_only}")

    # -- coverage gap 1: `_code_names` precision, not a term count -----------------
    # reverting to the earlier def/class-only-or-length>=3 regex would let every
    # one of these back in; the current tokenizer + `_is_leak_shaped` keeps them out.
    assert runlog._code_names("You MUST return ONLY the ANSWER") == [], \
        "ALL-CAPS emphasis is prose, not a leaked identifier"
    assert runlog._code_names("for the cheap check to run") == [], \
        "ordinary lowercase English must not read as leaked code identifiers"
    assert "process_batch" in runlog._code_names("result = process_batch(x, y)"), \
        "a call site with no matching def in this diff must still be caught"

    # -- coverage gap 2: `_one_source` (phase 1) with a REAL run_meta, not the
    # vacuous `run_meta=None` fixture `run.py`'s own self-check drives it with.
    # `_one_source` takes fetch/parse/generalize as plain arguments (`run.py:93`),
    # so no module faking is needed, only namespaces.
    run_src = Source(id=make_source_id("gigaevo://run9/prog9", "prog9"),
                     url="gigaevo://run9/prog9", title="t", type="run", version="prog9",
                     retrieved_at="2026-07-28T10:00:00Z", run_success=True,
                     run_meta={"run": "run9", "program_id": "prog9", "task": "aime",
                               "mutation_model": "m", "mutant_code_names": ["helper_fn"]})
    paper_src = Source(id=make_source_id("https://arxiv.org/abs/prog9", "v1"), url="u",
                       title="t2", type="paper", version="v1",
                       retrieved_at="2026-07-28T10:00:00Z", run_success=None, run_meta=None)
    sections = [Section(id="abstract", kind="abstract", title="Abstract", text="abs"),
               Section(id="S1", kind="section", title="Body", text="body")]
    draft = DraftThesis(text="uses helper_fn under prog9", context="c", effect="e",
                        locator="S1", draft_text="d", draft_applicability="a",
                        draft_limitations="l")
    fake_parse = types.SimpleNamespace(
        parse_document=lambda body, abstract, limitations:
            ([draft], {"per_section": {"S1": 1}, "dropped": 0}))
    seen_extra_terms: list[tuple] = []

    def recording_leakage(draft, out, extra_terms=()):
        seen_extra_terms.append(tuple(extra_terms))
        return []

    fake_generalize = types.SimpleNamespace(
        generalize=lambda d: IdeaFields(text="x", applicability_conditions="",
                                        limitations="", failure_modes=[]),
        leakage=recording_leakage)
    staging_path = tmp / "one_source.jsonl"

    for src in (run_src, paper_src):
        fake_fetch = types.SimpleNamespace(
            fetch_source=lambda entry, s=src: (s, sections),
            find_abstract=lambda secs: "abs", find_limitations=lambda secs: "")
        run._one_source({"arxiv_id": src.id}, fake_fetch, fake_parse, fake_generalize,
                        staging_path)

    assert seen_extra_terms[0] != (), \
        ("a `run` source with a real run_meta must produce non-empty extra_terms — "
         "this is exactly the case run.py's own fixture (run_meta=None) cannot see")
    assert "prog9" in seen_extra_terms[0] and "helper_fn" in seen_extra_terms[0], \
        seen_extra_terms[0]
    assert seen_extra_terms[1] == (), \
        "a paper source must pass no extra_terms at all — leak_terms is a run-only rule"

    return ("3 run-specific leaks caught with extra_terms, 0 caught by the paper "
            "check alone; _code_names stays precise on prose; _one_source wires "
            "extra_terms for a run source with a real run_meta, not just an empty one")


@check(27, "idempotency: re-ingesting the same mutant twice does not move the leaf "
           "count, keeps one source, and the second pass reports `skipped`")
def check_27(tmp: Path) -> str:
    """§10 point 27. `link.py`'s step [0] (`link.py:69-70`, `UNIQUE(source_id,
    text_hash)`) is what §2.3 calls idempotency "for free" — this check exercises it
    through the real `run.phase2`, not `link_batch` alone, so a regression in how
    `_phase2` wires step [0] would show up here even if `link.py`'s own self-check
    stayed green.
    """
    idx = _open(tmp)
    staging_src = tmp / "runlog.jsonl"
    payload = {"run_id": "idem_seed1", "task_id": "aime", "mutants": [
        {"program_id": "m1", "parent_ids": ["r1"], "state": "done",
         "generation": 1, "iteration": 1, "fitness": 0.7, "parent_fitness": 0.4,
         "mutation_model": "m",
         "mutation_output_raw": _runlog_mo(changes=[
             {"description": "cache the router lookup before revalidating",
              "explanation": "cuts wasted calls"}])},
    ]}
    with contextlib.ExitStack() as stack:
        stack.enter_context(_swap(llm, "complete", _fake_runlog_generalize))
        stack.enter_context(_swap(llm, "assert_grammar_works", lambda model: None))
        report0 = runlog.from_payload(payload, staging_path=staging_src)
    assert report0["run_theses"] == 1, report0
    rows = [json.loads(ln) for ln in staging_src.read_text(encoding="utf-8").splitlines()]
    source_id = rows[0]["source"]["id"]

    # -- first load: one leaf, one source, the judge scores the new idea once ------
    report1, ops1, _ = _phase2(tmp, rows, [])
    assert ops1 == ["trust"], \
        f"an empty index offers no candidates, so no link call at all: {ops1}"
    assert report1["theses_written"] == 1 and report1["theses_skipped"] == 0, report1
    assert graph_client.count_theses(source_id=source_id) == 1
    assert graph_client.counts()["sources"] == 1

    # -- second load of the SAME mutant, as a fresh ingestion would present it: own
    # staging content, no leftover cursor (`drain_run` deletes both on success, `13`
    # §2.3) — `link.link_batch`'s step [0] finds the text_hash already stored under
    # this source and skips before any LLM call, on the real `run.phase2` path.
    (tmp / "staging.cursor").unlink(missing_ok=True)
    report2, ops2, _ = _phase2(tmp, rows, [])
    assert ops2 == [], f"a replayed mutant must cost no LLM call at all: {ops2}"
    assert report2["theses_written"] == 0, report2
    assert report2["theses_skipped"] == 1, (
        "a replay must report `theses_skipped`, not a clean `ok` that reads "
        f"identical to a run that never touched this mutant at all: {report2}")
    assert graph_client.count_theses(source_id=source_id) == 1, \
        "a replayed mutant must not add a second leaf"
    assert graph_client.counts()["sources"] == 1, \
        "write_source is INSERT OR REPLACE (§2.1 revision): a replay must not add a " \
        "second source row"
    return "1 leaf and 1 source after both loads; the replay reports theses_skipped=1"


def _blank_idea(text: str, **override) -> Idea:
    """An `Idea` with every required field filled and nothing about it under test —
    checks 28-30 build several of these and only the overridden fields matter."""
    base = dict(id=new_idea_id(), text=text, applicability_conditions="a", limitations="l",
               failure_modes=[], effect_claimed="", effect_observed="", vector=_vec(text))
    return Idea(**{**base, **override})


@check(28, "trust: the judge on a mock — \"7\" stores 0.7 and lowers dirty; an "
           "out-of-enum answer and a raised call both leave the score and the flag "
           "untouched and count one trust_failed each; a leafless idea scores 0.0 with "
           "no call at all; trust_scale() is fixed at 1.0; a failed judgement is judged "
           "again on the next pass; a raw JSON number where the enum wants a string is "
           "refused by judge() itself, never by the store's range guard two layers down; "
           "an idea over MAX_LEAVES is capped in leaves_shown AND in the prompt the judge "
           "actually receives; run_pass() reaches a named idea even if it is clean, and "
           "with idea_ids omitted judges exactly TRUST_PER_PASS of whatever is dirty, "
           "reporting trust_due/trust_deferred the same way phase 2's own end-of-pass "
           "step does (13 finding, review 2026-07-31)")
def check_28(tmp: Path) -> None:
    _open(tmp)
    sid = _write_source("s1")

    def fixed_score(raw):
        """`raw` is either the enum string the judge answers with, or an Exception the
        call raises instead — same shape as `_arbiter`'s scripted `llm.complete`."""
        def fake(prompt, *, system, schema, op, max_tokens, timeout,
                 model=llm.QWEN_9B, temperature=0.0):
            assert op == "trust" and model is llm.QWEN_35B, (op, model)
            if isinstance(raw, Exception):
                raise raw
            return {"reason": "fixture", "score": raw}
        return fake

    # -- 1. "7" -> 0.7 in the store, and the judge is what lowers dirty (`13` §3.2-3.3).
    good = _blank_idea("freeze the encoder")
    graph_client.create_idea_with_theses(good, sid, [_thesis("s1", good.id, "leaf a")])
    assert graph_client.get_ideas([good.id])[0]["dirty"], "a fresh leaf must raise dirty"
    with _swap(llm, "complete", fixed_score("7")):
        report = trust.sweep([good.id])
    assert report["trust_scored"] == 1 and report["trust_failed"] == 0, report
    after = graph_client.get_ideas([good.id])[0]
    assert after["trust_score"] == 0.7, after["trust_score"]
    assert not after["dirty"], "the judge ran and must have lowered the flag"

    # -- 2. an answer outside the enum: the score and the flag are UNCHANGED, one
    # trust_failed — never 0.0, which is a legal ANSWER and would look like a verdict.
    bad_enum = _blank_idea("mixed precision saves memory")
    graph_client.create_idea_with_theses(bad_enum, sid, [_thesis("s1", bad_enum.id, "leaf b")])
    with _swap(llm, "complete", fixed_score("11")):
        report = trust.sweep([bad_enum.id])
    assert report["trust_scored"] == 0 and report["trust_failed"] == 1, report
    after = graph_client.get_ideas([bad_enum.id])[0]
    assert after["trust_score"] == 0.0, "an out-of-enum answer must not move the score"
    assert after["dirty"], "a refused judgement must leave the idea dirty for the retry"

    # -- 3. a raised call: the same two invariants, its own trust_failed.
    raised = _blank_idea("island model migration")
    graph_client.create_idea_with_theses(raised, sid, [_thesis("s1", raised.id, "leaf c")])
    with _swap(llm, "complete", fixed_score(llm.LLMError("connection reset by peer"))):
        report = trust.sweep([raised.id])
    assert report["trust_scored"] == 0 and report["trust_failed"] == 1, report
    after = graph_client.get_ideas([raised.id])[0]
    assert after["trust_score"] == 0.0, after["trust_score"]
    assert after["dirty"], "a raised call must leave the idea dirty for the retry"

    # -- 4. a leafless idea is 0.0 BY DEFINITION (`12-decisions-meetings.md:70-72`) and
    # costs no call — proven by making any call an AssertionError, not just by counting.
    def no_call(*a, **kw):
        raise AssertionError("a leafless idea must not call the judge at all")

    hollow = _blank_idea("a hypothesis with nothing under it")
    graph_client.create_idea(hollow)
    with _swap(llm, "complete", no_call):
        got = trust.refresh(hollow.id)
    assert got == {"idea_id": hollow.id, "score": 0.0, "reason": "no leaves, nothing to judge",
                   "leaves_shown": 0, "leaves_total": 0, "called": False}, got
    assert graph_client.get_ideas([hollow.id])[0]["trust_score"] == 0.0

    # -- 5. the scale is fixed: the judge already answers normalized, there is no
    # second expression of it left in the store to drift (`13` §3.3).
    assert graph_client.trust_scale() == 1.0

    # -- 6. the retry the design turns on: a SECOND pass over the idea the judge
    # refused on judges it again, because `dirty` was never lowered for it. Nothing
    # else in this suite drives a second sweep over the same idea — without this, a
    # judge that failed once and was silently never retried would still read green.
    with _swap(llm, "complete", fixed_score("6")):
        retried = trust.sweep([bad_enum.id])
    assert retried["trust_scored"] == 1 and retried["trust_failed"] == 0, retried
    after = graph_client.get_ideas([bad_enum.id])[0]
    assert after["trust_score"] == 0.6, after["trust_score"]
    assert not after["dirty"], "the retried pass must have judged the idea and cleared it"

    # -- 7. the enum guard catches a TYPE drift that the store's range guard would not.
    # "11" (step 2) becomes int("11")/10 = 1.1 if the guard were gone — out of [0, 1],
    # so `set_trust`'s own range check would still refuse it and `sweep` would report
    # the identical trust_failed=1 either way. That leaves the enum guard unproven: the
    # outcome is pinned, never the reason. A raw JSON number 7 (the string-enum grammar
    # skipped, a bare number answered instead) has no such second net: int(7)/10 = 0.7
    # is INSIDE [0, 1] and would sail straight through `set_trust` and be stored as a
    # normal, silently wrong score. Calling `trust.judge` directly — under sweep's own
    # exception, not through it — is what lets this assert WHERE the refusal comes from,
    # not merely that one happened.
    drifted = _blank_idea("mixed precision, a second time")
    graph_client.create_idea_with_theses(drifted, sid, [_thesis("s1", drifted.id, "leaf e")])
    drifted_idea = graph_client.get_ideas([drifted.id])[0]
    with _swap(llm, "complete", fixed_score(7)):
        try:
            trust.judge(drifted_idea, drifted_idea["theses"])
        except llm.LLMError as exc:
            msg = str(exc)
            # The judge's own message names the raw value and the enum it is not in —
            # the store's ValueError instead reads "trust_score out of [0, 1]" and never
            # fires here at all, since 0.7 is a value it would happily accept.
            assert "7" in msg and "outside" in msg, msg
            assert "trust_score out of" not in msg, \
                "this is the store's range guard talking, not the judge's enum guard"
        else:
            raise AssertionError(
                "a raw JSON number 7 (not the string \"7\") was accepted: the enum guard "
                "is gone and only the store's range check stood between this and a "
                "silently wrong 0.7 — which 0.7 does not trip")
    # And the call never even reached the store: no score was written for this idea.
    assert graph_client.get_ideas([drifted.id])[0]["trust_score"] == 0.0, \
        "a judge refusal must not leave a score behind"
    assert graph_client.get_ideas([drifted.id])[0]["dirty"], \
        "a judge refusal must leave the idea dirty for the retry"

    # -- 8. MAX_LEAVES caps what the judge is shown, not just what the report claims.
    # One idea already reached 92 leaves on the first corpus run (`lake/README.md:498`);
    # the cap exists so the judge never has to read all of them. `leaves_shown` and
    # `leaves_total` are supposed to make a truncation visible — this must also prove
    # the truncation actually happened to the PROMPT, not just to the two numbers that
    # describe it: a `shown = ordered[:MAX_LEAVES]` turned into `shown = ordered` would
    # still let a report say "leaves_shown: 16" if only the numbers were checked here.
    overfull = _blank_idea("an idea with too many leaves")
    over_leaves = [_thesis("s1", overfull.id, f"overfull leaf {n}")
                   for n in range(trust.MAX_LEAVES + 4)]
    graph_client.create_idea_with_theses(overfull, sid, over_leaves)
    seen_prompt = {}

    def capturing(prompt, **kw):
        seen_prompt["prompt"] = prompt
        return {"reason": "fixture", "score": "5"}

    with _swap(llm, "complete", capturing):
        got = trust.refresh(overfull.id)
    assert got["leaves_shown"] == trust.MAX_LEAVES, got
    assert got["leaves_total"] == trust.MAX_LEAVES + 4, got
    assert seen_prompt["prompt"].count("source_type:") == trust.MAX_LEAVES, \
        "the judge's own prompt carried more leaves than the cap allows"

    # -- 9. `run_pass` — the operator-triggered entry point (13 finding, review
    # 2026-07-31). (a) it must reach an idea BY ID regardless of its `dirty` flag:
    # the whole point, since the corpus that motivated this predates the judge and
    # was never marked dirty by anything (`dirty_ideas()` would never see it).
    reachable = _blank_idea("never touched since before the judge existed")
    graph_client.create_idea_with_theses(reachable, sid, [_thesis("s1", reachable.id, "leaf f")])
    graph_client.set_trust(reachable.id, 0.2)          # judged once already, now clean
    assert not graph_client.get_ideas([reachable.id])[0]["dirty"], "must start clean"
    with _swap(llm, "complete", fixed_score("9")):
        named_report = trust.run_pass([reachable.id])
    assert (named_report["trust_scored"], named_report["trust_due"],
            named_report["trust_deferred"]) == (1, 1, 0), named_report
    assert graph_client.get_ideas([reachable.id])[0]["trust_score"] == 0.9, \
        "run_pass must judge a NAMED idea even though it was clean"

    # (b) `idea_ids` omitted: the SAME worklist the ordinary sweep reads
    # (`dirty_ideas()`), the same `TRUST_PER_PASS` ceiling, the same trust_due/
    # trust_deferred pair phase 2's own end-of-pass step reports (`13` §9 p.3) — a
    # truncated on-demand pass must not read as a finished one either.
    extra_dirty = _blank_idea("one more dirty idea, for the ceiling")
    graph_client.create_idea_with_theses(extra_dirty, sid, [_thesis("s1", extra_dirty.id, "leaf h")])
    before_dirty = set(graph_client.dirty_ideas())
    # `raised` (step 3) and `drifted` (step 7) are still dirty from earlier in this
    # check — a refused judgement never lowers the flag — so together with this new
    # idea there are already >= 3 dirty ideas without building any more fixtures.
    assert len(before_dirty) >= 3, "need >= 3 dirty ideas to prove the ceiling truncates"
    with _swap(run, "TRUST_PER_PASS", 2), _swap(llm, "complete", fixed_score("4")):
        capped_report = trust.run_pass()
    assert capped_report["trust_due"] == len(before_dirty), capped_report
    assert capped_report["trust_scored"] == 2, \
        "the ceiling must cap how many are judged, not just how many are reported"
    assert capped_report["trust_deferred"] == len(before_dirty) - 2, capped_report
    after_dirty = set(graph_client.dirty_ideas())
    assert len(before_dirty) - len(after_dirty) == 2, \
        ("exactly TRUST_PER_PASS ideas must actually be judged and cleared, the rest "
         f"left dirty for the next pass: before={before_dirty}, after={after_dirty}")


@check(29, "dirty: writing a leaf raises the flag inside the SAME transaction as the "
           "leaf — a failure staged after the leaf write does not let the flag survive "
           "the rollback; `set_trust` is the only place that lowers it; `_rederive_due` "
           "requires BOTH the flag and the leaf-count threshold, neither one alone")
def check_29(tmp: Path) -> None:
    _open(tmp)
    sid = _write_source("s1")

    # -- 1. the transaction, not the flag. An idea already sits in the store, clean.
    # The SAME call that appends a leaf to it also raises `dirty`
    # (`create_idea_with_theses`, `neo4j_store.py:308-328`, through the real
    # `_mark_dirty`) — made here to blow up right after it has actually run, forcing
    # a rollback of everything the transaction touched. A `dirty` that survives this
    # was never really written inside the leaf's transaction. `_mark_dirty`, not
    # `_update_idea`: `create_idea_with_theses` calls the former directly (the
    # latter is `update_idea`/`split_idea`/`set_trust`'s function, exercised in
    # part 2 below through the real API instead of a swap).
    clean = _blank_idea("already in the store")
    graph_client.create_idea(clean)
    assert not graph_client.get_ideas([clean.id])[0]["dirty"], "a fresh idea starts clean"

    real_mark_dirty = neo4j_store._mark_dirty

    def boom(tx, idea_id, value):
        real_mark_dirty(tx, idea_id, value)       # the flag really is raised...
        raise RuntimeError("simulated write failure")  # ...then the write fails

    leaf = _thesis("s1", clean.id, "a fresh leaf")
    with _swap(neo4j_store, "_mark_dirty", boom):
        try:
            graph_client.create_idea_with_theses(None, sid, [leaf])
        except RuntimeError:
            pass
        else:
            raise AssertionError("the stubbed write failure did not reach the caller")

    after = graph_client.get_ideas([clean.id])[0]
    assert not after["dirty"], "dirty survived a transaction that was rolled back"
    assert after["theses"] == [], "the leaf survived the same rollback"

    # -- 2. `set_trust` is the only lowering (`13` §3.2), spelled out once more right
    # next to the transaction proof above rather than assumed from check 28.
    graph_client.create_idea_with_theses(None, sid, [_thesis("s1", clean.id, "leaf two")])
    assert graph_client.get_ideas([clean.id])[0]["dirty"], "the real write must raise dirty"
    graph_client.set_trust(clean.id, 0.4)
    lowered = graph_client.get_ideas([clean.id])[0]
    assert not lowered["dirty"] and lowered["trust_score"] == 0.4, lowered

    # -- 3. `_rederive_due` needs BOTH conditions, never either alone.
    over_but_clean = _blank_idea("clean but over the threshold")
    graph_client.create_idea_with_theses(
        over_but_clean, sid, [_thesis("s1", over_but_clean.id, f"leaf {n}") for n in range(3)])
    # New ideas start dirty (§3.2) — flipped clean here to fake "the leaves moved but
    # the flag says no", the state `_rederive_due` must refuse regardless of the count.
    graph_client.update_idea(over_but_clean.id, {"dirty": False})

    dirty_but_under = _blank_idea("dirty but under the threshold")
    graph_client.create_idea_with_theses(
        dirty_but_under, sid, [_thesis("s1", dirty_but_under.id, "only leaf")])

    both = _blank_idea("dirty and over the threshold")
    graph_client.create_idea_with_theses(
        both, sid, [_thesis("s1", both.id, f"both leaf {n}") for n in range(3)])

    due = run._rederive_due()
    assert over_but_clean.id not in due, "3 new leaves with a clean flag must not be due"
    assert dirty_but_under.id not in due, "1 new leaf while dirty must not be due either"
    assert both.id in due, ("dirty AND over the threshold must be due — otherwise this "
                            "check cannot tell AND from a guard that always answers no")

    # -- 4. `write_theses` is the OTHER leaf-writing path — append-only, no idea to
    # create (`create_idea_with_theses` above is the transactional pair that does).
    # Until 2026-07-31 it did not raise `dirty` at all: the module docstring already
    # claimed the flag is raised "in the same transaction as the leaves" for every
    # write, and this path silently contradicted it (`13` finding, review of the
    # same date). An idea with no leaves yet is correctly clean; the very next call
    # gives it its first leaf and must flip the flag, same as the transactional path.
    appended = _blank_idea("only ever grows through write_theses")
    graph_client.create_idea(appended)
    assert not graph_client.get_ideas([appended.id])[0]["dirty"], \
        "an idea with no leaves yet starts clean"
    graph_client.write_theses(sid, [_thesis("s1", appended.id, "the only leaf")])
    assert graph_client.get_ideas([appended.id])[0]["dirty"], \
        "write_theses must raise dirty exactly like create_idea_with_theses"


@check(30, "idea without leaves: a hypothesis (origin=\"synthesized\", trust_score 0.0) "
           "passes; an extracted idea with no leaves is a broken write; a planted "
           "interrupted transaction proves a \"we no longer check\" replacement would "
           "go red")
def check_30(tmp: Path) -> None:
    _open(tmp)

    # A hypothesis: legal since `13` §5, and it must never be mistaken for the write
    # that broke — the two states are opposite, not two names for the same thing.
    hypothesis = _blank_idea("a synthesized hypothesis", origin="synthesized", trust_score=0.0)
    graph_client.create_idea(hypothesis)

    # The planted interrupted transaction: an idea row with no thesis row at all,
    # default origin "extracted" — exactly the old crash between `create_idea` and
    # `write_theses` that the bare `ideas_without_leaves() == []` invariant used to
    # catch (§5). A leafless idea that is NOT a hypothesis is still that same defect.
    broken = _blank_idea("a broken extracted idea")
    graph_client.create_idea(broken)
    assert broken.origin == "extracted", "the default must stay extracted"

    orphans = set(graph_client.ideas_without_leaves())
    assert {hypothesis.id, broken.id} <= orphans, orphans

    report, ops, _ = _phase2(tmp, [], [])
    assert ops == [], "an idle store with nothing dirty must cost no LLM call"
    # The hypothesis is counted as a hypothesis, never as broken.
    assert report["hypotheses"] == 1, report
    # The extracted orphan IS the invariant violation. A "we no longer check" that
    # quietly drops this bucket to 0, or that folds it into `hypotheses`, must go red
    # here — this is the case that replacement is supposed to be caught by.
    assert report["ideas_without_leaves"] == 1, report


def _rederive_would_fire(idea_id: str, threshold: int = 3) -> bool:
    body = graph_client.get_ideas([idea_id])[0]
    return len(body["theses"]) - body["rederived_at_leaf_count"] >= threshold


# ---------------------------------------------------------- neo4j fixtures (`13` §4)

def _leaf_groups() -> list[frozenset[str]]:
    """Leaves partitioned by idea, keyed by TEXT rather than id: ids are fresh
    random uuids per run and would never match a re-run's even when the grouping
    — which arbiter decisions were made — is identical (`13` §10 point 31)."""
    by_idea: dict[str, set[str]] = {}
    for leaf in graph_client.all_theses():
        by_idea.setdefault(leaf["idea_id"], set()).add(leaf["text"])
    return sorted((frozenset(v) for v in by_idea.values()), key=lambda s: sorted(s))


def _raw_delete(ids: list[str]) -> None:
    """Storage-level delete of `ids` out of source/idea/thesis — nothing is ever
    deleted through the public API (`13`). Used only to free lower internal ids
    on Neo4j and reproduce the ordering bug the `neo4j_store` module docstring
    describes."""
    with neo4j_store._session() as session:
        session.run("MATCH (n) WHERE n.id IN $ids DETACH DELETE n", ids=ids).consume()


def _ordering_scenario() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """A throwaway source+idea+leaves, torn down at the storage level, then the
    real survivors — on Neo4j the teardown frees lower internal ids, the exact
    trigger `ORDER BY id(...)` reshuffles on and `seq`/`rowid` must not (review,
    `13` §10 point 31: 6 of the 7 readers that promise insertion order were never
    driven through this scenario, only `get_leaves` was). Returns (what every
    order-sensitive reader actually answered, what it must have answered), keyed
    by function name, so the caller can diff reader by reader.
    """
    gone_sid = _write_source("s3")
    gone_idea = _blank_idea("gone", dirty=True)
    gone_leaves = [_thesis("s3", gone_idea.id, f"gone leaf {n}") for n in range(4)]
    graph_client.create_idea_with_theses(gone_idea, gone_sid, gone_leaves)
    _raw_delete([gone_sid, gone_idea.id] + [leaf.id for leaf in gone_leaves])

    kept_sid_a, kept_sid_b = _write_source("s1"), _write_source("s2")
    kept_idea_a = _blank_idea("kept a", dirty=True)
    # No leaves of its own on purpose — this idea exists only to give
    # list_idea_ids/dirty_ideas two survivors to order. A leafless idea is only
    # legal as a hypothesis (`13` §5, checked separately by 6.30), hence origin.
    kept_idea_b = _blank_idea("kept b", dirty=True, origin="synthesized", trust_score=0.0)
    kept_leaves = [_thesis("s1", kept_idea_a.id, f"kept leaf {n}") for n in range(5)]
    graph_client.create_idea_with_theses(kept_idea_a, kept_sid_a, kept_leaves)
    graph_client.create_idea(kept_idea_b)

    want_leaves = [leaf.id for leaf in kept_leaves]
    want_ideas = [kept_idea_a.id, kept_idea_b.id]
    want_sources = [kept_sid_a, kept_sid_b]
    got = {
        "get_ideas": [t["id"] for t in graph_client.get_ideas([kept_idea_a.id])[0]["theses"]],
        "get_leaves": [t["id"] for t in graph_client.get_leaves(kept_idea_a.id)],
        "list_theses": [t["id"] for t in
                        graph_client.list_theses(idea_id=kept_idea_a.id, limit=100)],
        "all_theses": [t["id"] for t in graph_client.all_theses()
                       if t["idea_id"] == kept_idea_a.id],
        "list_idea_ids": [i for i in graph_client.list_idea_ids(limit=1000) if i in want_ideas],
        "dirty_ideas": [i for i in graph_client.dirty_ideas() if i in want_ideas],
        "list_sources": [s["id"] for s in graph_client.list_sources(limit=1000)
                         if s["id"] in want_sources],
    }
    want = {"get_ideas": want_leaves, "get_leaves": want_leaves, "list_theses": want_leaves,
            "all_theses": want_leaves, "list_idea_ids": want_ideas, "dirty_ideas": want_ideas,
            "list_sources": want_sources}
    return got, want


_NEO4J_ORDERED_READERS = ("get_ideas", "get_leaves", "list_theses", "all_theses",
                          "list_idea_ids", "dirty_ideas", "list_sources")


def _assert_neo4j_orders_by_seq() -> None:
    """Static guard, offline and deterministic on purpose. The live scenario below
    needs Neo4j to actually REUSE a freed internal id inside one session to prove
    anything about `ORDER BY id(...)` — and on this driver/image that turned out
    NOT to happen live: probed directly (create 4, delete, create 5, all in one
    session), the new nodes kept getting fresh, never-before-used ids every time;
    reuse only showed up after a container restart. A live check built on that
    same-session assumption would never go red for this mutation in this
    environment — exactly the false confidence CLAUDE.md's review step exists to
    catch. So the six readers the review found uncovered (`13` §10: "revert to
    `ORDER BY id(...)` and everything passes") are pinned by reading the source:
    none of them may order by Neo4j's internal id(), all of them must order by
    the stored `seq` (module docstring, neo4j_store.py).
    """
    source = Path(neo4j_store.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    by_name = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    for name in _NEO4J_ORDERED_READERS:
        assert name in by_name, f"neo4j_store.{name} is gone"
        node = by_name[name]
        # Every string literal INSIDE the function except its own docstring — the
        # actual Cypher text sent to the driver. Comments never reach the AST at
        # all, so scanning raw source text here (as `check_09` does for
        # `update_thesis`) would also match this very module's docstring and
        # in-code comments about the bug, which talk about `ORDER BY id(` on
        # purpose while explaining why it is banned.
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            body = body[1:]
        literals = " ".join(
            child.value for child in ast.walk(ast.Module(body=body, type_ignores=[]))
            if isinstance(child, ast.Constant) and isinstance(child.value, str))
        assert "ORDER BY id(" not in literals, \
            f"neo4j_store.{name} orders rows by Neo4j's internal id() — reused after a " \
            "delete, the exact regression this ban exists for (13 §10)"
        assert "ORDER BY" in literals and ".seq" in literals, \
            f"neo4j_store.{name} no longer orders by the stored seq (13 §10)"


@check(31, "insertion order + replay: write/read/replay through phase 2, plus a "
           "delete-then-recreate that frees lower Neo4j internal ids, still gives "
           "back the right counts, idea groupings, ideas_without_leaves, a replay "
           "that says skipped, and every reader that promises insertion order "
           "(get_ideas/get_leaves/list_theses/all_theses/list_idea_ids/"
           "dirty_ideas/list_sources) in insertion order — pinned statically too "
           "(id(...) is not reliably reused live in this environment); the "
           "`:_Seq` counter refuses a duplicate")
def check_31(tmp: Path) -> str:
    _assert_neo4j_orders_by_seq()   # offline, static — runs before anything live

    from neo4j.exceptions import ConstraintError

    # The `:_Seq` counter is the one identity node the whole ordering guarantee
    # rests on (module docstring, neo4j_store.py) and had no uniqueness
    # constraint until this review: a second one landed with only a
    # UserWarning, not an error. Proven fixed here, before it matters to
    # anything else this check writes — `_open` below wipes any leftover
    # `:_Seq` first, so this creates the very first one on purpose.
    _open(tmp)
    with neo4j_store._session() as session:
        session.run("CREATE (:_Seq {id: 'global'})").consume()
    try:
        with neo4j_store._session() as session:
            session.run("CREATE (:_Seq {id: 'global'})").consume()
    except ConstraintError:
        pass
    else:
        raise AssertionError("a second :_Seq counter node was accepted — nothing "
                             "left stands behind the ordering guarantee (13 §10)")
    with neo4j_store._session() as session:
        session.run("MATCH (n:_Seq) DETACH DELETE n").consume()

    report, ops, _ = _phase2(tmp, CORPUS, CORPUS_ANSWERS)
    (tmp / "staging.cursor").write_text("0\n", encoding="utf-8")   # replay from the top
    replay, replay_ops, _ = _phase2(tmp, CORPUS, [])
    order_got, order_want = _ordering_scenario()
    # `ideas_without_leaves()` is raw (`13` §5): it also names legal hypotheses
    # (`_ordering_scenario`'s own `kept_idea_b`, origin="synthesized"), so the
    # invariant is "never extracted" — that origin is the broken-write case
    # §5/6.30 exist to catch, not "the list is always empty".
    orphan_origins = sorted(idea["origin"] for idea in
                            graph_client.get_ideas(graph_client.ideas_without_leaves()))

    assert report["theses"] == 5 and report["ideas"] == 4, report
    assert ops == ["link"] * 4 + ["trust"] * 4, ops
    assert "extracted" not in orphan_origins, \
        ("a broken write left an extracted idea without leaves", orphan_origins)
    assert replay["theses_written"] == 0, replay
    assert replay["theses_skipped"] == 5, replay
    assert replay_ops == [], replay_ops
    for reader, ids in order_got.items():
        assert ids == order_want[reader], \
            (reader, "insertion order lost", ids, order_want[reader])

    return ("5 theses / 4 ideas / no extracted orphan / replay=skipped / 7 order-"
            "sensitive readers hold insertion order / the :_Seq constraint refuses "
            "a duplicate")


@check("31a", "lake/neo4j_store.py's own `__main__` self-check suite (single-"
              "transaction rollback, leaf_key idempotency, the orphan-Thesis "
              "traversal behind counts()/count_theses()/all_theses()/"
              "list_theses(), get_ideas/get_leaves/set_trust, the wipe guard) is "
              "RUN here, as a subprocess, not merely sitting next to this suite "
              "unreached (review, `13` §10): a thin slice re-derived next to it "
              "let a split-transaction create_idea_with_theses, a silently "
              "MERGEd duplicate leaf_key, and counts() reading the raw label "
              "instead of the served traversal all survive mutation")
def check_31a(tmp: Path) -> str:
    import subprocess
    # No gate and no env override needed here any more (D11: one shared live
    # graph for the whole suite, the same NEO4J_URI this process already
    # validated at the top of `main`) — the runner's own `_cleanup`, which runs
    # after every check including this one, is what guarantees the graph is
    # empty going in.
    result = subprocess.run(
        [sys.executable, "-B", "-m", "lake.neo4j_store"],
        cwd=REPO, capture_output=True, text=True, timeout=180)
    # Pin the module's own final print, not only the exit code: a run killed or
    # crashed before ever reaching a live assertion (import error, hang, timeout)
    # must not read as green just because something downstream defaulted to 0.
    assert "neo4j_store self-check OK" in result.stdout, (
        f"lake/neo4j_store.py's own self-check did not reach its final line "
        f"(exit {result.returncode}):\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}")
    assert result.returncode == 0, (
        f"lake/neo4j_store.py's own self-check printed its ok line but still exited "
        f"{result.returncode}:\n{result.stdout}\n{result.stderr}")
    return ("lake/neo4j_store.py's own self-check ran to completion, as a subprocess, "
            "against the shared live Neo4j — the rollback/idempotency/traversal suite "
            "next to the module is no longer merely present, it is reached")


@check(32, "a killed Neo4j: every sampled graph route answers 503 (never a quiet "
           "200), /retrieve answers 503 too — not `ideas: []` — and writes no "
           "success line to the retrieve log; the D11 startup refusal (missing "
           "or empty NEO4J_URI) still refuses, for both shapes of missing")
def check_32(tmp: Path) -> str:
    from fastapi.testclient import TestClient

    from .api.app import create_app

    # --- part 1: the D11 startup refusal — no fallback, ever (`13` §4.1) -------
    # `stub_store` is gone and so is `LAKE_STORE`: Neo4j is the only backend, and
    # `_select_backend` now refuses on a single condition, checked both ways it
    # can go missing — the env key absent entirely, and present but empty (a
    # blank `.env.local` line, say) — `not os.environ.get(...)` treats both the
    # same, and this proves the check does too, not just the more common one.
    old_uri = os.environ.get("NEO4J_URI")
    try:
        for missing_uri in (None, ""):
            if missing_uri is None:
                os.environ.pop("NEO4J_URI", None)
            else:
                os.environ["NEO4J_URI"] = missing_uri
            try:
                graph_client._select_backend()
            except RuntimeError as exc:
                assert "NEO4J_URI" in str(exc), exc
            else:
                raise AssertionError(f"NEO4J_URI={missing_uri!r} must refuse (D11)")

        os.environ["NEO4J_URI"] = "bolt://neo4j:7687"
        assert graph_client._select_backend() is neo4j_store, \
            "a non-empty NEO4J_URI must select neo4j_store, the only backend left"
    finally:
        if old_uri is None:
            os.environ.pop("NEO4J_URI", None)
        else:
            os.environ["NEO4J_URI"] = old_uri

    # --- part 2: a killed Neo4j, through the real app -------------------------
    # No backend to swap any more (D11: `graph_client._backend` is always
    # `neo4j_store`) — only the URI needs to point somewhere nothing answers.
    neo4j_store.close()
    old_uri = os.environ.get("NEO4J_URI")
    os.environ["NEO4J_URI"] = "bolt://127.0.0.1:1"   # nothing listens on port 1: fails fast

    def _restore() -> None:
        neo4j_store.close()
        if old_uri is None:
            os.environ.pop("NEO4J_URI", None)
        else:
            os.environ["NEO4J_URI"] = old_uri

    with contextlib.ExitStack() as stack:
        stack.callback(_restore)

        with TestClient(create_app(mock=False, warmup=False, api_key=False,
                                   workers=False)) as client:
            for path in ("/sources", "/ideas", "/theses", "/stats"):
                resp = client.get(path)
                assert resp.status_code == 503, (path, resp.status_code, resp.text)
                body = resp.json()
                assert set(body) == {"error"}, (path, body)

            log_path = tmp / "retrieve.jsonl"
            # `index.count` takes `db=INDEX_DB` as a DEFAULT ARGUMENT, bound at def
            # time (same trap as check_05/check_19) — the §6.19 divergence guard
            # runs it FIRST, before `graph_client.counts()` gets a chance to raise
            # on the killed Neo4j below, so an unpatched call here reaches the real
            # `data/index.db` even though this check never builds a fixture index.
            with _swap(index, "count", functools.partial(index.count, db=tmp / "index.db")), \
                    _swap(api, "RETRIEVE_LOG", log_path), \
                    _swap(rank, "search", lambda q, qv, top_k=50:
                          [{"idea_id": "idea_dead0000001", "score": 0.5}]):
                resp = client.post("/retrieve", json={
                    "query": "keep accuracy while finetuning", "rewrite": False})
            assert resp.status_code == 503, (resp.status_code, resp.text)
            body = resp.json()
            assert set(body) == {"error", "log_id"}, body
            assert "ideas" not in body, "a broken graph answered ideas: [] instead of 503"

            lines = [json.loads(ln) for ln in log_path.read_text(encoding="utf-8").splitlines()]
            assert len(lines) == 1, lines
            line = lines[0]
            assert "error" in line, "the retrieve log has no error for a broken store"
            assert line["returned"] == [] and line["cut_off"] == [], line
            assert "ideas" not in line, \
                "the retrieve log recorded a success shape for a broken store"
            # Review finding 2026-07-31: 0 is ALSO the legal value of a request whose
            # D14 quota never had to reject anyone — initializing the three quota
            # fields to 0 made a 503 indistinguishable from a healthy, unviolated
            # quota on all three. `None` is the only value that cannot be misread
            # as "ranking ran and the quota held".
            assert (line["trust_quota"], line["untrusted_returned"],
                    line["untrusted_over_quota"]) == (None, None, None), line

    return ("the D11 startup refusal holds for a missing and an empty NEO4J_URI alike, "
            "and a non-empty one selects neo4j_store; every sampled graph route and "
            "/retrieve answer 503 on a killed Neo4j, with no fake success in the "
            "retrieve log")


# check 33 ("migration: lake.db -> Neo4j") is retired with `neo4j_store.migrate()`
# itself (D11: "миграция... делается операционно, старым образом... в коде путь
# чтения из SQLite не сохраняем") — there is no `stub_store` left for it to read
# from. The 384-float-vector regression (07:78) that check proved fixed was a
# property of `migrate()`'s own Thesis-building, not of anything `neo4j_store`'s
# normal write path does; that write path (`create_idea_with_theses`/
# `write_theses`) is exercised by nearly every other check in this file with real
# vectors from `_vec()`, so the regression class stays covered even with the
# migration-specific proof gone.


# ------------------------------------------------------------------- the runner

def _fingerprint_real_data() -> dict[str, str]:
    """sha1 of every real artefact the suite must not touch (missing files count too)."""
    import hashlib
    from .models import DATA
    out = {}
    # `jobs.db` and `writer.lock` are in the list for the same reason as the rest: a
    # check that reached the module default instead of its bound temp path would enqueue
    # fixture jobs into the operator's real queue, or take the lock the running API
    # holds — and neither shows up as a failure anywhere else.
    # `lake.db` (the old stub_store SQLite file) is gone with D11 — the graph this
    # suite must not touch is the real Neo4j the process is pointed at, guarded
    # separately by `_require_neo4j_up`'s empty-database refusal, not a file here.
    for path in (DATA / "index.db", DATA / "staging.jsonl",
                 DATA / "staging.cursor", DATA / "pending_link.jsonl",
                 DATA / "jobs.db", DATA / "writer.lock",
                 DATA / "logs" / "retrieve.jsonl"):
        out[path.name] = (hashlib.sha1(path.read_bytes()).hexdigest()
                          if path.exists() else "absent")
    return out


@check(34, "the Idea-synthesis pair (`lake/idea_merger.py`, `lake/idea_edges.py`) has "
           "its own self-check RUN here, as a subprocess: hypothesis shape (`13` §6 "
           "field by field), origin/trust, the write order through graph_client "
           "including the two `derived_from` edges (D12), the indexed synthetic "
           "leaf, `persisted` in the audit log, and `idea_edges.py`'s own "
           "`parse_parents`. The `:RELATED` label / accumulate-MERGE-MATCH shape of "
           "the upsert itself moved into `neo4j_store.py` with the edge-writing "
           "code (D12) and is checked there, offline, by 6.31a's subprocess instead "
           "— `idea_edges.py` no longer has a Cypher of its own to pin")
def check_34(tmp: Path) -> str:
    """Both modules used to live entirely behind their own `--self-check`, reached by
    neither this suite nor CI (`.github/workflows/deploy.yml` runs `lake.ingest.run
    selfcheck`, `lake.api.selfcheck` and `lake.selfcheck --offline`, and nothing else)
    — against CLAUDE.md's «нетривиальная логика оставляет проверку в `selfcheck.py`».
    A check that exists but is never invoked is the same as no check.

    Subprocess, not `demo()` in-process, for the reason 6.31a already gives: `demo()`
    monkey-patches `llm.complete` and `embed.embed_docs`, and although both are now
    restored in a `finally`, a fake LLM leaking into the rest of THIS suite would make
    every later check pass for free. Process isolation makes that structurally
    impossible rather than merely unlikely.
    """
    import subprocess

    notes = []
    for module, final_line in (("lake.idea_merger", "idea_merger self-check OK"),
                               ("lake.idea_edges", "idea_edges self-check OK")):
        result = subprocess.run([sys.executable, "-B", "-m", module, "--self-check"],
                                cwd=REPO, capture_output=True, text=True, timeout=180)
        # The module's own final line, not only the exit code: a run that died before
        # reaching a live assertion (import error, hang) must not read as green
        # because something downstream defaulted to 0.
        assert final_line in result.stdout, (
            f"{module}'s self-check did not reach its final line (exit "
            f"{result.returncode}):\n--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}")
        assert result.returncode == 0, (
            f"{module}'s self-check printed its ok line but exited "
            f"{result.returncode}:\n{result.stdout}\n{result.stderr}")
        notes.append(f"{module}: {result.stdout.count('ok (')} checks")
    return "; ".join(notes) + " — both ran to completion as subprocesses"


# The four checks below close mutation-testing gaps found against D14's quota (`13`
# review 2026-07-31): each one is written to FAIL on the specific mutation named in
# its `@check` text, not just to exercise the code path the mutation sits in.

@check(35, "graph_client.neighbors() on an unreachable Neo4j RAISES — it must never "
           "swallow the error into an empty list (mutation M13: that turns a broken "
           "graph into a falsely 'no related ideas' answer instead of a 503, exactly "
           "the fail-open CLAUDE.md bans)")
def check_35(tmp: Path) -> None:
    neo4j_store.close()
    old_uri = os.environ.get("NEO4J_URI")
    os.environ["NEO4J_URI"] = "bolt://127.0.0.1:1"   # nothing listens on port 1: fails fast
    try:
        try:
            graph_client.neighbors(["idea_doesnotexist0"])
        except graph_client.STORE_ERRORS:
            pass
        else:
            raise AssertionError(
                "neighbors() returned instead of raising on an unreachable Neo4j — a "
                "broken graph must surface as a 503 through rank.rank()'s edge step, "
                "not a quiet empty page")
    finally:
        neo4j_store.close()
        if old_uri is None:
            os.environ.pop("NEO4J_URI", None)
        else:
            os.environ["NEO4J_URI"] = old_uri


@check(36, "the D14 quota (`floor(0.2*k)` ideas with trust_score==0) is enforced on "
           "the ACTUAL /retrieve answer, not just the log field (mutation M2: "
           "`untrusted_used < quota` replaced by `True` removes the cap outright "
           "while `trust_quota` in the log keeps printing floor(0.2*k) as if nothing "
           "changed — only the served ideas would show the difference)")
def check_36(tmp: Path) -> None:
    _open(tmp)
    src = _sid("s1")
    graph_client.write_source(Source(id=src, url=SOURCES["s1"][0], title=SOURCES["s1"][1],
                                     type=SOURCES["s1"][2], version="v1",
                                     retrieved_at="2026-07-28T10:00:00Z"))
    k = 10
    quota = math.floor(0.2 * k)  # 2
    # 6 untrusted ideas outscore all 10 trusted ones outright — without the cap every
    # one of them fits inside k on score alone, nothing pushes them down.
    untrusted_ids = [_seed_idea(src, f"untrusted match {i}", 0.0) for i in range(6)]
    trusted_ids = [_seed_idea(src, f"trusted match {i}", 0.8) for i in range(10)]
    hits = ([{"idea_id": i, "score": 0.9 - 0.001 * n} for n, i in enumerate(untrusted_ids)]
           + [{"idea_id": i, "score": 0.5 - 0.001 * n} for n, i in enumerate(trusted_ids)])
    with _swap(rank, "search", lambda q, qv, top_k=50, _h=hits: _h):
        ideas, log = rank.rank("anything", k=k,
                               query_vec=np.asarray(_vec("anything"), dtype=np.float32))
    assert len(ideas) == k, ideas
    n_untrusted = sum(1 for i in ideas if i["trust_score"] == 0)
    # This is the line the mutation breaks: without the cap all 6 top-scored
    # untrusted ideas would be selected, not `quota` (2) of them.
    assert n_untrusted <= quota, (n_untrusted, quota, [i["idea_id"] for i in ideas])
    assert log["trust_quota"] == quota, log


@check(37, "the untrusted top-up beyond quota (`via='padding'`) is picked in the SAME "
           "deterministic best-first order `scored` already put candidates in, not "
           "reversed (mutation M4: `deferred_untrusted[:k-len(out)]` reversed hands "
           "back the worst-matching untrusted ideas first)")
def check_37(tmp: Path) -> None:
    _open(tmp)
    src = _sid("s1")
    graph_client.write_source(Source(id=src, url=SOURCES["s1"][0], title=SOURCES["s1"][1],
                                     type=SOURCES["s1"][2], version="v1",
                                     retrieved_at="2026-07-28T10:00:00Z"))
    trusted_id = _seed_idea(src, "trusted anchor", 0.8)
    # quota = floor(0.2*5) = 1: the primary scan takes untrusted #0 into the cap and
    # defers #1, #2, #3 in best-score-first order — exactly what padding must preserve.
    untrusted_ids = [_seed_idea(src, f"untrusted padding candidate {i}", 0.0)
                     for i in range(4)]
    hits = ([{"idea_id": trusted_id, "score": 0.99}]
           + [{"idea_id": i, "score": 0.9 - 0.1 * n} for n, i in enumerate(untrusted_ids)])
    k = 5
    with _swap(rank, "search", lambda q, qv, top_k=50, _h=hits: _h):
        ideas, log = rank.rank("anything", k=k,
                               query_vec=np.asarray(_vec("anything"), dtype=np.float32))
    assert len(ideas) == k, ideas
    padded = [i for i in ideas if i["via"] == "padding"]
    # untrusted_ids[0] was consumed by the quota (via="thesis"); padding must take the
    # rest in the SAME best-first order `scored` put them in: [1], [2], [3].
    assert [i["idea_id"] for i in padded] == untrusted_ids[1:], (padded, untrusted_ids)
    assert log["untrusted_returned"] == 4 and log["trust_quota"] == 1, log


@check(38, "idea_merger's grammar canary (`main()`, before every real run) checks the "
           "SAME model merge_classify itself runs on (D10: both moved to 9B) — a "
           "canary pinned to a different model proves nothing about the step it "
           "guards (mutation M15)")
def check_38(tmp: Path) -> None:
    class _Stop(Exception):
        pass

    step_models: list = []

    def fake_step_complete(prompt, *, system, schema, op, max_tokens, timeout, model,
                           temperature):
        step_models.append(model)
        return {"can_combine": False}

    canary_models: list = []

    def fake_canary(model):
        canary_models.append(model)

    with _swap(llm, "complete", fake_step_complete), \
            _swap(llm, "assert_grammar_works", fake_canary):
        # merge_classify itself, to learn what model it ACTUALLY runs on — not a
        # constant copied from `idea_merger.py`, which would just check the literal
        # against itself and stay green under the mutation.
        idea_merger.ask_can_combine({"text": "a", "effect_claimed": "e"},
                                    {"text": "b", "effect_claimed": "e"})
        assert step_models, "ask_can_combine made no llm.complete call"
        step_model = step_models[0]

        # `main()`'s canary line runs unconditionally, before `run()` — stub `run()`
        # to abort right after, so this needs neither a real graph nor `--persist`.
        with _swap(idea_merger, "run", lambda *a, **k: (_ for _ in ()).throw(_Stop())):
            try:
                idea_merger.main([])
            except _Stop:
                pass
    assert canary_models, "idea_merger.main() must run the grammar canary before run()"
    assert canary_models[0] == step_model, (
        f"canary checked model={canary_models[0]!r} but merge_classify actually runs "
        f"on {step_model!r} — a passing canary said nothing about the real step")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m lake.selfcheck",
        description="The 19 assertions of spec 10 §6 plus §11.6, the Neo4j load and the "
                    "leaf-ceiling split, "
                    "one run, only assert.")
    parser.add_argument("--offline", action="store_true",
                        help="skip 6.1, the only check that opens a socket to the school's "
                             "LLM servers; the other checks still need NO_PROXY, no key — "
                             "but DO need a live, empty, LOCAL Neo4j (D11: it is the only "
                             "backend now, not just what points 31/31a used to reach past "
                             "a `stub` default for)")
    args = parser.parse_args(argv)

    _require_neo4j_up()  # D11: the whole suite's precondition now, checked once, up front —
                          # never silently green because the graph it needs is absent (13 §10)

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
        # The writer lock goes to the temp dir for the whole suite and not per check:
        # `run.phase2` takes it now (§4.5), so ANY check that reaches phase 2 would
        # otherwise create `data/writer.lock` — and be refused by a live API that holds it.
        with _swap(trace, "TRACES_DIR", Path(root) / "traces"), \
                _swap(writer_lock, "LOCK_PATH", Path(root) / "writer.lock"), _fake_embed():
            for number, what, fn in CHECKS:
                if number == 1 and args.offline:
                    skipped.append(number)
                    print(f"skip 6.{number}  {what} [--offline]")
                    continue
                # `number` is an int for every point but 26a (§10): `:02d}` rejects a
                # str outright, so the padding only applies where it still can.
                label = f"{number:02d}" if isinstance(number, int) else str(number)
                tmp = Path(root) / f"check{label}"
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
        summary += f", {len(skipped)} skipped ({', '.join('6.%s' % n for n in skipped)})"
    if failed:
        summary += f", {len(failed)} FAILED ({', '.join('6.%s' % n for n in failed)})"
    print(summary)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
