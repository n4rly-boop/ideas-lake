"""Combine two existing Idea nodes of the lake into a new one, judged and drafted by
the school's LLMs (spec 10 §3.1 conventions), against the real Neo4j the lake was
loaded into (`knowledge/07-roles-and-contracts.md:70-81`).

Not a `graph_client` backend and not a phase of ingest: this reads and writes the real
Neo4j directly, the way `neo4j_load.py` already does and for the same reason —
`graph_client` fronts `stub_store`, which never held the ideas this operates on
(`neo4j_load.py:7-10`). Weight and edge mechanics between ideas are block B's side
(`06-proposal-design.md` §6, `knowledge/07-roles-and-contracts.md:81`: "рёбер
Idea—Idea в озере нет вообще"); this module and `idea_edges.py` are exploratory tools
against B's instance, not the live serving path, and stay out of `graph_client.py` on
purpose so they cannot be mistaken for it.

Pipeline per pair:

  1. two random Idea nodes (or a caller-supplied pair).
  2. merge_classify (35B, schema-forced boolean): can these two combine into one
     coherent idea? A schema-forced `can_combine` replaces free-text ДА/НЕТ entirely —
     there is no "chatty small model" failure mode to parse around, the grammar only
     ever returns a JSON boolean (`lake/llm.py` p.1, p.5-p.7).
  3. if yes, merge_generate (9B, schema-forced): reuses `GENERALIZE_SCHEMA` /
     `IdeaFields` as-is — the shape of "idea content" (text, applicability_conditions,
     limitations, failure_modes) does not change because the input is two ideas
     instead of one thesis draft.
  4. embed the new idea's text with the project's own encoder (`lake/embed.py`), not a
     third-party one: a vector from a different model would sit in the same graph
     column but not the same space, and every cosine search touching it would be
     silently wrong instead of loudly absent.
  5. write the new Idea node (`trust_score=0.0`, no leaves of its own — the same state
     `graph_client.ideas_without_leaves` already anticipates) and `DERIVED_FROM` edges
     to the two source ideas.

Every pair, accepted or not, is logged to `data/logs/idea_merger.jsonl` (append, same
shape as `retrieve/api.py`'s own log) for manual audit of the classifier. A classifier
or generator failure raises `llm.LLMError` and is not swallowed into "rejected": the
two are different outcomes and a run that cannot tell them apart cannot be audited.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

from . import llm, trace
from .models import GENERALIZE_SCHEMA, LOGS_DIR, Idea, IdeaFields, new_idea_id

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

DERIVED_FROM = "DERIVED_FROM"


# ============================================================
# Neo4j — direct driver, same convention as neo4j_load.py
# ============================================================

def open_driver():
    from neo4j import GraphDatabase

    missing = [name for name in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD")
               if not os.environ.get(name)]
    if missing:
        raise SystemExit(f"not in the environment: {', '.join(missing)}")
    return GraphDatabase.driver(os.environ["NEO4J_URI"],
                                auth=(os.environ["NEO4J_USERNAME"],
                                      os.environ["NEO4J_PASSWORD"]))


def database_name() -> str:
    return os.environ.get("NEO4J_DATABASE", "neo4j")


def random_idea_pairs(session, num_pairs: int, min_trust: float | None = None) -> list[tuple[dict, dict]]:
    """`num_pairs` pairs of distinct Idea nodes, chosen at random.

    Pairing is sequential over a `rand()`-ordered read, the same shape the original
    prototype used: correct for "some arbitrary sample of pairs to try", not for
    "every idea gets an equal chance of appearing" (an odd result count drops the
    last row unpaired).
    """
    query = """
    MATCH (i:Idea)
    WHERE i.text IS NOT NULL
      AND ($min_trust IS NULL OR i.trust_score >= $min_trust)
    RETURN i.id AS id, i.text AS text,
           i.effect_claimed AS effect_claimed, i.effect_observed AS effect_observed
    ORDER BY rand()
    LIMIT $limit
    """
    records = [dict(r) for r in
              session.run(query, limit=num_pairs * 2, min_trust=min_trust)]
    return [(records[i], records[i + 1]) for i in range(0, len(records) - 1, 2)]


def _props(idea: Idea) -> dict:
    """Idea -> Neo4j property map. `differentiation` is the model's only optional
    field (`models.py`); absent stays absent rather than landing as `null`, the same
    rule `neo4j_load._row` follows for the ingested side of the same graph."""
    return {k: v for k, v in idea.model_dump().items() if v is not None}


def write_merged_idea(session, idea: Idea, source_idea_ids: list[str]) -> None:
    session.run("CREATE (i:Idea $props)", props=_props(idea))
    for old_id in source_idea_ids:
        session.run(
            f"MATCH (new:Idea {{id: $new_id}}), (old:Idea {{id: $old_id}}) "
            f"MERGE (new)-[:{DERIVED_FROM}]->(old)",
            new_id=idea.id, old_id=old_id)


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
        trust_score=0.0,
        rederived_at_leaf_count=0,
        created_at=now,
        updated_at=now,
    )


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

def run(session, num_pairs: int, persist: bool, min_trust: float | None = None,
       log_path=MERGE_LOG) -> list[Idea | None]:
    pairs = random_idea_pairs(session, num_pairs, min_trust=min_trust)
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
                write_merged_idea(session, result, [idea_a["id"], idea_b["id"]])
                print(f"   written to Neo4j (database={database_name()!r})")
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
        description="Combine random Idea pairs of the lake via the school LLMs.")
    parser.add_argument("--num-pairs", type=int, default=5,
                        help="how many random pairs to try (default 5)")
    parser.add_argument("--persist", action="store_true",
                        help="write accepted merges to Neo4j (default: print only)")
    parser.add_argument("--min-trust", type=float, default=None,
                        help="only consider ideas with trust_score >= this value")
    parser.add_argument("--self-check", action="store_true",
                        help="offline check, no network and no Neo4j connection")
    args = parser.parse_args(argv)

    if args.self_check:
        demo()
        return 0

    llm.assert_grammar_works(llm.QWEN_9B)     # canary per model used, every run
    llm.assert_grammar_works(llm.QWEN_35B)

    driver = open_driver()
    try:
        with driver.session(database=database_name()) as session:
            results = run(session, args.num_pairs, args.persist, min_trust=args.min_trust)
    finally:
        driver.close()

    successful = [r for r in results if r is not None]
    print(f"{len(successful)}/{len(results)} pairs combined; log: {MERGE_LOG}")
    return 0


# ============================================================
# Self-check — no network, no Neo4j (matches neo4j_load.py --self-check)
# ============================================================

class _FakeSession:
    """Records `(query, params)` instead of talking to Neo4j."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def run(self, query, **params):
        self.calls.append((query, params))


def demo() -> None:
    import tempfile
    from pathlib import Path

    import numpy as np

    idea_a = {"id": "idea_aaaaaaaaaaaa", "text": "cache repeated query results",
             "effect_claimed": "faster responses"}
    idea_b = {"id": "idea_bbbbbbbbbbbb", "text": "pool database connections",
             "effect_claimed": "lower load"}

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

    # (b) classifier says yes -> Idea, trust_score 0, 384-dim vector, DERIVED_FROM ready.
    calls.clear()
    _ANSWERS = [True]
    result = try_combine_ideas(idea_a, idea_b)
    assert result is not None and result.trust_score == 0.0
    assert result.id.startswith("idea_") and len(result.vector) == 384
    assert result.effect_claimed == "" and result.effect_observed == ""
    assert [c["op"] for c in calls] == ["merge_classify", "merge_generate"], calls
    print("ok (b): can_combine=true -> Idea with trust_score=0.0, 384-dim vector")

    # (c) _props drops the one optional field only when it is absent, keeps everything else.
    props = _props(result)
    assert "differentiation" not in props, props
    assert props["id"] == result.id and props["vector"] == result.vector

    # (d) write_merged_idea: one CREATE, one MERGE per source idea, DERIVED_FROM named.
    session = _FakeSession()
    write_merged_idea(session, result, [idea_a["id"], idea_b["id"]])
    assert len(session.calls) == 3, session.calls
    create_q, create_p = session.calls[0]
    assert "CREATE (i:Idea $props)" in create_q and create_p["props"]["id"] == result.id
    for (query, params), old_id in zip(session.calls[1:], [idea_a["id"], idea_b["id"]]):
        assert f"MERGE (new)-[:{DERIVED_FROM}]->(old)" in query
        assert params == {"new_id": result.id, "old_id": old_id}
    print("ok (d): write_merged_idea -> 1 CREATE + 2 DERIVED_FROM MERGE, ids threaded through")

    # (e) log_pair: reject and combine both produce one JSONL line, decision named.
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "idea_merger.jsonl"
        log_pair(idea_a, idea_b, None, log_path=log_path)
        log_pair(idea_a, idea_b, result, log_path=log_path)
        lines = [json.loads(ln) for ln in log_path.read_text(encoding="utf-8").splitlines()]
        assert len(lines) == 2, lines
        assert lines[0]["decision"] == "reject" and lines[0]["new_idea"] is None
        assert lines[1]["decision"] == "combine" and lines[1]["new_idea"]["id"] == result.id
    print("ok (e): log_pair -> one JSONL line per pair, reject and combine both carried")

    print("idea_merger self-check OK")


if __name__ == "__main__":
    sys.exit(main())
