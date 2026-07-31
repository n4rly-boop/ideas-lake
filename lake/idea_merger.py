"""Combine two existing Idea nodes of the lake into a new one — a hypothesis, judged
and drafted by the school's LLMs (spec 10 §3.1 conventions), written the way `13` §5-§6
says a hypothesis is written.

**Goes through `graph_client`, not through a driver of its own.** The original version
opened `neo4j.GraphDatabase` directly and justified it by "`graph_client` fronts
`stub_store`, which never held the ideas this operates on". That stopped being true on
2026-07-31: `graph_client` picks between `stub_store` and `neo4j_store` by `LAKE_STORE`
(`graph_client.py:44-92`, `13` §4.1), and the Bolt backend is the same lake this reads.
A second write path around it would miss everything the backend does on the way in — the
`seq` ordering counter (`neo4j_store.py:131`, without which `list_idea_ids`/`get_leaves`
come back in a shuffled order), the uniqueness constraints, `dirty` raised inside the
same transaction as the leaves (`13` §3.2), and the trace block D reads. This module now
runs on whichever backend `LAKE_STORE` names, stub or Bolt, and writes nothing the
serving path cannot read.

A hypothesis is not a bare Idea node. `13` §5-§6:

- `origin="synthesized"` on the idea itself. Without it a leafless idea with
  `trust_score=0` is indistinguishable from a write that died between `create_idea` and
  `write_theses`, which is exactly what selfcheck 6.30 (`selfcheck.py:2097-2127`) reads
  it as.
- one **synthetic leaf**, or the hypothesis cannot be found at all: `/retrieve` searches
  theses, not ideas (§6 p.1). `Source(type="synthesis", url="lake://synthesis/<idea_id>")`
  + a `Thesis` whose `locator` names the parents is the whole provenance mechanism —
  "нет адреса — нет утверждения", and the address is the parents.
- the leaf goes into the thesis index in the same step as the write (`run.py:279`),
  otherwise the index drifts from the graph silently.

Pipeline per pair:

  1. two random Idea nodes from the lake. Hypotheses are **not** eligible as parents —
     see `sample_idea_pairs`.
  2. merge_classify (35B, schema-forced boolean): can these two combine into one
     coherent idea? A schema-forced `can_combine` replaces free-text ДА/НЕТ entirely —
     there is no "chatty small model" failure mode to parse around, the grammar only
     ever returns a JSON boolean (`lake/llm.py` p.1, p.5-p.7).
  3. if yes, merge_generate (9B, schema-forced): reuses `GENERALIZE_SCHEMA` /
     `IdeaFields` as-is — the shape of "idea content" (text, applicability_conditions,
     limitations, failure_modes) does not change because the input is two ideas
     instead of one thesis draft.
  4. embed the new idea's text and its synthetic leaf's text with the project's own
     encoder (`lake/embed.py`), not a third-party one: a vector from a different model
     would sit in the same graph column but not the same space, and every cosine search
     touching it would be silently wrong instead of loudly absent.
  5. `write_source` → `create_idea_with_theses` → `index_theses`, the same three calls
     in the same order as the ingest loop (`run.py:246-280`).

The `DERIVED_FROM` edge to the two parents is **not** written here. A writes no
Idea—Idea edges at all (`13` §3.1) and `graph_client` has no method for one; the edge is
block B's side and lives in `idea_edges.py --derived-from`, which reads the parentage
back off the leaf's `locator`. Nothing is lost by the split — the parentage reaches the
graph either way — and this module keeps one write path instead of two.

Every pair, accepted or not, is logged to `data/logs/idea_merger.jsonl` (append, same
shape as `retrieve/api.py`'s own log) for manual audit of the classifier. A classifier
or generator failure raises `llm.LLMError` and is not swallowed into "rejected": the
two are different outcomes and a run that cannot tell them apart cannot be audited.
"""
import argparse
import json
import random
import sys
from datetime import datetime, timezone

from . import graph_client, index, llm, trace
from .models import (GENERALIZE_SCHEMA, LOGS_DIR, Idea, IdeaFields, Source, Thesis,
                     new_idea_id, new_thesis_id, source_id as make_source_id, text_hash)

# Schema-forced boolean: flat, no $ref, additionalProperties False (models.py's own
# rule for every schema handed to llama.cpp). Kept local rather than added to
# `models.py`, which is block A's ingest-schema file (`SCHEMA_BINDINGS`) and does not
# need a fourth, unrelated schema to track.
CAN_COMBINE_SCHEMA = {
    "type": "object",
    "properties": {"can_combine": {"type": "boolean"}},
    "required": ["can_combine"],
    "additionalProperties": False,
}

MERGE_LOG = LOGS_DIR / "idea_merger.jsonl"

# How many idea ids the sampler pulls before choosing pairs out of them.
# ponytail: whole-pool read, fine while the lake is thousands of ideas; if it outgrows
# that, sample on the store side instead (`ORDER BY rand() LIMIT` over Bolt).
POOL_LIMIT = 10_000

SYNTHESIS_TYPE = "synthesis"        # the fourth Source.type (`13` §6, models.py:76)
SYNTHESIS_URL = "lake://synthesis/{idea_id}"
UNVERIFIED = "не проверено"         # Thesis.effect of a hypothesis — it has no measurement


# ============================================================
# Reading the lake — through graph_client, on whichever backend it selected
# ============================================================

def sample_idea_pairs(num_pairs: int, min_trust: float | None = None) -> list[tuple[dict, dict]]:
    """`num_pairs` pairs of distinct ideas, chosen at random out of the lake.

    Pairing is sequential over a shuffled pool, the same shape the original prototype's
    `rand()`-ordered read had: correct for "some arbitrary sample of pairs to try", not
    for "every idea gets an equal chance of appearing" (an odd pool drops the last one
    unpaired).

    **A hypothesis is not eligible as a parent.** `origin == "synthesized"` ideas are
    filtered out: they carry `trust_score = 0` and a leaf that says `effect="не
    проверено"`, and synthesizing out of them compounds unverified content with nothing
    in the chain ever grounding it — a second run would merge hypotheses of hypotheses
    and every one of them would still read as evidence-free. Parents come from papers
    and runs.
    """
    ids = graph_client.list_idea_ids(limit=POOL_LIMIT)
    pool = [body for body in graph_client.get_ideas(ids)
            if body["origin"] == "extracted"
            and (min_trust is None or body["trust_score"] >= min_trust)]
    random.shuffle(pool)
    picked = pool[:num_pairs * 2]
    return [(picked[i], picked[i + 1]) for i in range(0, len(picked) - 1, 2)]


# ============================================================
# LLM steps
# ============================================================

def _describe(idea: dict) -> str:
    effect = idea.get("effect_claimed") or idea.get("effect_observed") or "(no effect recorded)"
    return f"statement: {idea['text']}\neffect: {effect}"


def ask_can_combine(idea_a: dict, idea_b: dict) -> bool:
    prompt = f"IDEA A\n{_describe(idea_a)}\n\nIDEA B\n{_describe(idea_b)}\n"
    answer = llm.complete(prompt, system=llm.load_prompt("merge_classify"),
                          schema=CAN_COMBINE_SCHEMA, op="merge_classify", max_tokens=32,
                          timeout=30, model=llm.QWEN_35B, temperature=0.0)
    return bool(answer["can_combine"])


def generate_merged_idea(idea_a: dict, idea_b: dict) -> IdeaFields:
    prompt = f"IDEA A\n{_describe(idea_a)}\n\nIDEA B\n{_describe(idea_b)}\n"
    obj = llm.complete(prompt, system=llm.load_prompt("merge_generate"),
                       schema=GENERALIZE_SCHEMA, op="merge_generate", max_tokens=800,
                       timeout=60, model=llm.QWEN_9B, temperature=0.0)
    return IdeaFields(**obj)


def try_combine_ideas(idea_a: dict, idea_b: dict) -> Idea | None:
    """One pair, full cycle. `None` means the classifier said no. An `llm.LLMError`
    from either step propagates instead of being folded into `None`: a failed
    generation and a genuine "these do not combine" are different outcomes, and a
    log that cannot tell them apart cannot be audited (module docstring)."""
    if not ask_can_combine(idea_a, idea_b):
        return None

    from . import embed          # local: keeps sentence-transformers off import (ops.py:133)

    fields = generate_merged_idea(idea_a, idea_b)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return Idea(
        id=new_idea_id(),
        text=fields.text,
        applicability_conditions=fields.applicability_conditions,
        limitations=fields.limitations,
        failure_modes=fields.failure_modes,
        effect_claimed="",     # an aggregate over leaves this idea does not have (§1.3)
        effect_observed="",
        vector=embed.embed_docs([fields.text])[0].tolist(),
        origin="synthesized",  # `13` §5 — without it this write reads as a crashed one
        trust_score=0.0,
        rederived_at_leaf_count=0,
        created_at=now,
        updated_at=now,
    )


# ============================================================
# The synthetic leaf (`13` §6) — what makes a hypothesis findable at all
# ============================================================

def synthesis_records(idea: Idea, parent_ids: list[str]) -> tuple[Source, Thesis]:
    """`Source(type="synthesis")` + the one leaf that carries the hypothesis into the
    thesis index, shaped as `13` §6 spells it out.

    `version` is the idea's own `created_at`, so `source_id` (a hash of url + version)
    is unique per hypothesis instead of colliding across re-runs, and the synthetic
    source's identity stays reproducible from the idea alone.
    """
    from . import embed          # local, same reason as above

    parents = "+".join(parent_ids)
    url = SYNTHESIS_URL.format(idea_id=idea.id)
    version = idea.created_at
    sid = make_source_id(url, version)
    src = Source(id=sid, url=url, title=f"синтез: {parents}", type=SYNTHESIS_TYPE,
                 version=version, retrieved_at=idea.created_at)

    # "текст гипотезы + спекулятивная применимость" (§6). The applicability belongs in
    # the indexed text on purpose: it is the half a query about *when* a technique
    # applies matches on, and idea bodies are not indexed — only leaves are.
    text = f"{idea.text}\n\nПрименимость: {idea.applicability_conditions}"
    thesis = Thesis(id=new_thesis_id(), source_id=sid, idea_id=idea.id, text=text,
                    context=f"синтез из {parents}", effect=UNVERIFIED,
                    locator=f"synthesis/{parents}", text_hash=text_hash(text),
                    vector=embed.embed_docs([text])[0].tolist(),
                    created_at=idea.created_at)
    return src, thesis


def write_hypothesis(idea: Idea, parent_ids: list[str]) -> Thesis:
    """Source, then idea+leaf in one transaction, then the index — the same three calls
    in the same order as the ingest loop (`run.py:246-280`). Indexing after the commit,
    never before: an indexed thesis no graph write backs is a search hit into a hole,
    and `_reconcile_index` only ever repairs the other direction."""
    src, thesis = synthesis_records(idea, parent_ids)
    graph_client.write_source(src)
    graph_client.create_idea_with_theses(idea, src.id, [thesis])
    index.index_theses([thesis])
    return thesis


# ============================================================
# Audit log (data/logs/idea_merger.jsonl, gitignored runtime data)
# ============================================================

def log_pair(idea_a: dict, idea_b: dict, result: Idea | None, *, log_path=MERGE_LOG) -> None:
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "run_id": trace.current_run_id(),
              "idea_a_id": idea_a["id"], "idea_b_id": idea_b["id"],
              "decision": "combine" if result is not None else "reject",
              "new_idea": result.model_dump() if result is not None else None}
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ============================================================
# Run loop
# ============================================================

def run(num_pairs: int, persist: bool, min_trust: float | None = None,
        log_path=MERGE_LOG) -> list[Idea | None]:
    pairs = sample_idea_pairs(num_pairs, min_trust=min_trust)
    if not pairs:
        print("no Idea pairs found in the graph")
        return []

    results: list[Idea | None] = []
    for idea_a, idea_b in pairs:
        result = try_combine_ideas(idea_a, idea_b)
        log_pair(idea_a, idea_b, result, log_path=log_path)
        if result is not None:
            print(f"combined {idea_a['id']} + {idea_b['id']} -> {result.id}: {result.text}")
            if persist:
                thesis = write_hypothesis(result, [idea_a["id"], idea_b["id"]])
                print(f"   written via {graph_client.backend_name()}, "
                      f"synthetic leaf {thesis.id} indexed")
        else:
            print(f"rejected {idea_a['id']} + {idea_b['id']}")
        results.append(result)
    return results


# ============================================================
# CLI
# ============================================================

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m lake.idea_merger",
        description="Combine random Idea pairs of the lake into hypotheses via the school LLMs.")
    parser.add_argument("--num-pairs", type=int, default=5,
                        help="how many random pairs to try (default 5)")
    parser.add_argument("--persist", action="store_true",
                        help="write accepted hypotheses to the lake (default: print only)")
    parser.add_argument("--min-trust", type=float, default=None,
                        help="only consider parents with trust_score >= this value")
    parser.add_argument("--self-check", action="store_true",
                        help="offline check, no network and no store connection")
    args = parser.parse_args(argv)

    if args.self_check:
        demo()
        return 0

    llm.assert_grammar_works(llm.QWEN_9B)     # canary per model used, every run
    llm.assert_grammar_works(llm.QWEN_35B)

    results = run(args.num_pairs, args.persist, min_trust=args.min_trust)
    successful = [r for r in results if r is not None]
    print(f"{len(successful)}/{len(results)} pairs combined; log: {MERGE_LOG}")
    return 0


# ============================================================
# Self-check — no network, no store (matches neo4j_load.py --self-check)
# ============================================================

class _FakeGraph:
    """Stands in for both `graph_client` and `index`: answers reads from a fixture and
    records the write calls in the order they were made."""

    def __init__(self, bodies: list[dict] | None = None):
        self.bodies = bodies or []
        self.sources: list[Source] = []
        self.writes: list[tuple] = []
        self.indexed: list[Thesis] = []
        self.order: list[str] = []

    def list_idea_ids(self, limit=50, offset=0):
        return [b["id"] for b in self.bodies][offset:offset + limit]

    def get_ideas(self, ids):
        by_id = {b["id"]: b for b in self.bodies}
        return [by_id[i] for i in ids if i in by_id]

    def write_source(self, src):
        self.order.append("write_source")
        self.sources.append(src)
        return src.id

    def create_idea_with_theses(self, idea, source_id, theses):
        self.order.append("create_idea_with_theses")
        self.writes.append((idea, source_id, theses))
        return [t.id for t in theses]

    def index_theses(self, theses):
        self.order.append("index_theses")
        self.indexed.extend(theses)

    def backend_name(self):
        return "fake"


def demo() -> None:
    import tempfile
    from pathlib import Path

    import numpy as np

    def _body(idea_id, text, **over):
        body = {"id": idea_id, "text": text, "applicability_conditions": "ac",
                "limitations": "lim", "failure_modes": [], "effect_claimed": "e",
                "effect_observed": "", "vector": [0.0] * 384, "differentiation": None,
                "origin": "extracted", "trust_score": 5.0, "dirty": False,
                "rederived_at_leaf_count": 0, "created_at": "2026-07-31T00:00:00+00:00",
                "updated_at": "2026-07-31T00:00:00+00:00", "theses": []}
        body.update(over)
        return body

    idea_a = _body("idea_aaaaaaaaaaaa", "cache repeated query results")
    idea_b = _body("idea_bbbbbbbbbbbb", "pool database connections")

    calls: list[dict] = []

    def fake_complete(prompt, *, system, schema, op, max_tokens, timeout, model, temperature):
        calls.append({"op": op, "schema": schema, "model": model,
                      "max_tokens": max_tokens, "timeout": timeout,
                      "temperature": temperature})
        assert "IDEA A" in prompt and "IDEA B" in prompt, prompt
        assert temperature == 0.0
        if op == "merge_classify":
            assert schema is CAN_COMBINE_SCHEMA and model is llm.QWEN_35B
            return {"can_combine": _ANSWERS.pop(0)}
        assert op == "merge_generate" and schema is GENERALIZE_SCHEMA and model is llm.QWEN_9B
        return {"text": "cache-aside reads paired with pooled connections to the backing "
                        "store, so a miss reuses an already-open connection instead of "
                        "opening a new one",
                "applicability_conditions": "reads dominate and a shared backing store exists",
                "limitations": "no benefit when every request is a write",
                "failure_modes": ["a stale cache entry outlives a pooled connection's data"]}

    def fake_embed_docs(texts):
        vec = np.zeros(384, dtype=np.float32)
        vec[0] = 1.0
        return np.stack([vec for _ in texts])

    llm.complete = fake_complete
    from . import embed
    embed.embed_docs = fake_embed_docs

    # (a) classifier says no -> None, generator never called.
    global _ANSWERS
    _ANSWERS = [False]
    assert try_combine_ideas(idea_a, idea_b) is None
    assert [c["op"] for c in calls] == ["merge_classify"], calls
    print("ok (a): can_combine=false -> None, no generator call")

    # (b) classifier says yes -> Idea, origin="synthesized", trust 0, 384-dim vector.
    # `origin` is the entire difference between a hypothesis and a crashed write
    # (`13` §5, selfcheck 6.30) — asserted here, not assumed.
    calls.clear()
    _ANSWERS = [True]
    result = try_combine_ideas(idea_a, idea_b)
    assert result is not None and result.trust_score == 0.0
    assert result.origin == "synthesized", result.origin
    assert result.id.startswith("idea_") and len(result.vector) == 384
    assert result.effect_claimed == "" and result.effect_observed == ""
    assert [c["op"] for c in calls] == ["merge_classify", "merge_generate"], calls
    print("ok (b): can_combine=true -> Idea(origin='synthesized', trust_score=0.0), 384-dim")

    # (c) the synthetic leaf: §6's shape, field by field, parents in the locator.
    src, thesis = synthesis_records(result, [idea_a["id"], idea_b["id"]])
    parents = f"{idea_a['id']}+{idea_b['id']}"
    assert src.type == "synthesis", src.type
    assert src.url == f"lake://synthesis/{result.id}", src.url
    assert src.id == make_source_id(src.url, result.created_at), src.id
    assert parents in src.title, src.title
    assert thesis.idea_id == result.id and thesis.source_id == src.id
    assert thesis.locator == f"synthesis/{parents}", thesis.locator
    assert thesis.context == f"синтез из {parents}", thesis.context
    assert thesis.effect == UNVERIFIED, thesis.effect
    assert result.text in thesis.text and result.applicability_conditions in thesis.text
    assert thesis.text_hash == text_hash(thesis.text) and len(thesis.vector) == 384
    print("ok (c): synthetic leaf = Source(type='synthesis') + Thesis(locator=parents)")

    # (d) the write goes through graph_client, in the ingest order, and the leaf is
    # indexed — a hypothesis nobody indexed is one /retrieve cannot find (§6 п.1).
    real_graph, real_index = graph_client, index
    fake = _FakeGraph()
    globals()["graph_client"], globals()["index"] = fake, fake
    try:
        leaf = write_hypothesis(result, [idea_a["id"], idea_b["id"]])
    finally:
        globals()["graph_client"], globals()["index"] = real_graph, real_index
    assert fake.order == ["write_source", "create_idea_with_theses", "index_theses"], fake.order
    written_idea, written_sid, written_theses = fake.writes[0]
    assert written_idea is result and written_sid == fake.sources[0].id
    assert [t.id for t in written_theses] == [leaf.id] == [t.id for t in fake.indexed]
    print("ok (d): write_source -> create_idea_with_theses -> index_theses, one leaf")

    # (e) sampling refuses hypotheses as parents: compounding unverified content leaves
    # nothing in the chain that ever grounds it (`sample_idea_pairs`).
    hypo = _body("idea_cccccccccccc", "a hypothesis", origin="synthesized", trust_score=0.0)
    globals()["graph_client"] = _FakeGraph([idea_a, idea_b, hypo])
    try:
        pairs = sample_idea_pairs(5)
        assert len(pairs) == 1, pairs
        assert {p["id"] for p in pairs[0]} == {idea_a["id"], idea_b["id"]}, pairs
        assert sample_idea_pairs(5, min_trust=9.0) == []      # min_trust, same read
    finally:
        globals()["graph_client"] = real_graph
    print("ok (e): origin='synthesized' is never a parent; min_trust filters")

    # (f) log_pair: reject and combine both produce one JSONL line, decision named.
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "idea_merger.jsonl"
        log_pair(idea_a, idea_b, None, log_path=log_path)
        log_pair(idea_a, idea_b, result, log_path=log_path)
        lines = [json.loads(ln) for ln in log_path.read_text(encoding="utf-8").splitlines()]
        assert len(lines) == 2, lines
        assert lines[0]["decision"] == "reject" and lines[0]["new_idea"] is None
        assert lines[1]["decision"] == "combine" and lines[1]["new_idea"]["id"] == result.id
        assert lines[1]["new_idea"]["origin"] == "synthesized", lines[1]
    print("ok (f): log_pair -> one JSONL line per pair, reject and combine both carried")

    print("idea_merger self-check OK")


if __name__ == "__main__":
    sys.exit(main())
