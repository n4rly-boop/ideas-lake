"""Idea re-derivation over all of its leaves (spec 10 §4.6).

The trigger is a FIELD on the idea, not a counter in run memory (§0.1.19): the corpus
is ingested in several passes and leaves from C4 runs arrive separately and later, so
an in-memory counter would be lost between passes and fire in the wrong place.

`id` is preserved — edges and their accumulated weights do not break (`08:200`).
`dirty` and `trust_score` stay untouched: they are B's and mean something else.
`differentiation` needs neighbouring ideas, i.e. edges, and is a separate pass.
"""
from .. import graph_client, llm
from ..models import REDERIVE_SCHEMA
from ..trace import trace

REDERIVE_EVERY = 3      # §8: every 3 new leaves (`09:181`)
# §8 has no row for rederive; six fields with 700+500+500+5*300+400+400 chars of
# ceiling need more than generalize's 800, and 90 s is the same ~x3 slack over the
# measured 7.0 s per ~400 output tokens that the other timeouts carry (§3.1 p.4).
MAX_TOKENS = 1200
TIMEOUT_S = 90

# Exactly the six fields of §4.6, nothing else is rewritten.
FIELDS = ("text", "applicability_conditions", "limitations", "failure_modes",
          "effect_claimed", "effect_observed")


@trace(component="ingest", op="rederive")
def maybe_rederive(idea_id: str) -> bool:
    """Re-derive the idea if 3 leaves have arrived since the last time. True if it ran.

    An LLM failure propagates: the idea stays as it was and `rederived_at_leaf_count`
    does not move, so the next leaf retries it. Returning False on a failure would be
    indistinguishable from "not due yet" — the fail-open this project forbids.
    """
    ideas = graph_client.get_ideas([idea_id])
    if not ideas:
        raise KeyError(f"rederive: idea {idea_id} is not in the graph")
    idea = ideas[0]
    # All leaves, already joined to source.type by the store (§4.6).
    leaves = graph_client.get_leaves(idea_id)
    if len(leaves) - idea["rederived_at_leaf_count"] < REDERIVE_EVERY:
        return False

    out = llm.complete(_render(idea, leaves), system=llm.load_prompt("rederive"),
                       schema=REDERIVE_SCHEMA, op="rederive",
                       max_tokens=MAX_TOKENS, timeout=TIMEOUT_S, model=llm.QWEN_9B)
    fields = {name: out[name] for name in FIELDS}
    if fields["text"] != idea["text"]:
        # The idea vector comes from `text` (§1.3); leaving the old one would drift
        # idea-to-idea neighbourhood away from what the idea now says.
        from .. import embed      # local: loading sentence-transformers costs seconds
        fields["vector"] = embed.embed_docs([fields["text"]])[0].tolist()
    fields["rederived_at_leaf_count"] = len(leaves)
    graph_client.update_idea(idea_id, fields)
    return True


def _render(idea: dict, leaves: list[dict]) -> str:
    """Current idea + ALL leaves with their context, effect and source type (§4.6).

    The source type is what keeps `effect_claimed` and `effect_observed` apart: the
    model cannot tell a claimed number from a measured one without it.
    """
    out = ["IDEA (current)",
           f"text: {idea['text']}",
           f"applicability_conditions: {idea['applicability_conditions']}",
           f"limitations: {idea['limitations']}",
           "failure_modes: " + ("; ".join(idea["failure_modes"]) or "(none)"),
           f"effect_claimed: {idea['effect_claimed'] or '(none)'}",
           f"effect_observed: {idea['effect_observed'] or '(none)'}",
           "",
           f"LEAVES ({len(leaves)})"]
    for n, leaf in enumerate(leaves, 1):
        out += [f"[{n}] source_type: {leaf['source_type']}",
                f"statement: {leaf['text']}",
                f"context: {leaf['context']}",
                f"effect: {leaf['effect']}",
                ""]
    return "\n".join(out)
