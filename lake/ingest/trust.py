"""`trust_score`: one number per idea, produced by a judge, not by a formula (`13` §3.3).

Until 2026-07-31 the number was `log(1 + distinct sources)` computed on every read.
Meeting 5 replaced it with a few-shot judgement over the idea and its leaves, because
what the number has to carry — "is this worth showing" — is not a function of how many
papers happened to mention it. The formula is gone from the codebase entirely; there is
no second expression of it left to drift.

Fail-closed, and here the failure mode is specific: 0.0 is a legal ANSWER ("we judged
and there is little to trust", or "there are no leaves, so there is nothing to trust").
It is never a failure marker. A judge that refused writes nothing at all — the previous
score stands, the idea stays `dirty`, and the next pass judges it again. That retry is
the whole reason `dirty` is lowered here and nowhere else.

Cost: one 35B call per idea per pass over the dirty set. That is why it sits in phase 2
and not in `/retrieve` — `O(N)` against `O(500 * K)` (`12-decisions-meetings.md:86`).
"""
from .. import graph_client, llm
from ..models import TRUST_SCHEMA
from ..trace import trace

# Same ceiling as `split.MAX_LEAVES`, and for a related reason. One idea already
# reached 92 leaves on the first corpus run (`lake/README.md:498`); "idea + all its
# leaves" would run out of context and answer `finish_reason != "stop"`, i.e. the judge
# would fail exactly on the ideas that matter most. The slice is deterministic and both
# numbers are reported: "judged on 16 of 40" is a different statement from "judged on
# 40" and must not look the same.
MAX_LEAVES = 16
MAX_TOKENS = 300
TIMEOUT_S = 90

SCORES = tuple(TRUST_SCHEMA["properties"]["score"]["enum"])


def leaf_order(leaves: list[dict]) -> list[dict]:
    """Which leaves the judge sees when there are more than `MAX_LEAVES`.

    Run leaves that carry an outcome come first — they are the only evidence measured
    inside our own loop, and rule 2 of the prompt leans on them. The rest follow by
    `created_at`, then by `id` so the order is total: a slice that depends on dict
    iteration would make the same idea score differently on two identical passes.
    """
    def key(leaf: dict) -> tuple:
        measured = leaf.get("source_type") == "run" and leaf.get("run_success") is not None
        return (0 if measured else 1, leaf.get("created_at") or "", leaf.get("id") or "")

    return sorted(leaves, key=key)


def _render(idea: dict, shown: list[dict], total: int) -> str:
    lines = [
        "IDEA",
        f"text: {idea['text']}",
        f"applicability_conditions: {idea.get('applicability_conditions') or '(none stated)'}",
        f"limitations: {idea.get('limitations') or '(none stated)'}",
        "failure_modes: " + ("; ".join(idea.get("failure_modes") or []) or "(none stated)"),
        f"effect_claimed: {idea.get('effect_claimed') or '(none stated)'}",
        f"effect_observed: {idea.get('effect_observed') or '(none measured)'}",
        "",
        f"leaves_shown: {len(shown)}",
        f"leaves_total: {total}",
        "",
        f"LEAVES ({len(shown)})",
    ]
    for n, leaf in enumerate(shown, 1):
        success = leaf.get("run_success")
        lines += [
            f"[{n}] source_type: {leaf.get('source_type') or '(unknown)'}",
            f"    source_title: {leaf.get('source_title') or '(untitled)'}",
        ]
        if leaf.get("source_type") == "run":
            lines.append("    run_success: "
                         + ("null (no measurement exists)" if success is None
                            else ("true" if success else "false")))
        lines += [
            f"    text: {leaf.get('text') or ''}",
            f"    context: {leaf.get('context') or '(none stated)'}",
            f"    effect: {leaf.get('effect') or '(no number stated)'}",
            "",
        ]
    return "\n".join(lines)


@trace(component="ingest", op="trust")
def judge(idea: dict, leaves: list[dict]) -> dict:
    """{"score": 0.0..1.0, "reason", "leaves_shown", "leaves_total"}. Raises on refusal.

    Writes nothing. The caller decides what a refusal costs — which, everywhere in this
    package, is: nothing is stored and the idea stays dirty.
    """
    ordered = leaf_order(leaves)
    shown = ordered[:MAX_LEAVES]
    out = llm.complete(_render(idea, shown, len(leaves)),
                       system=llm.load_prompt("trust"), schema=TRUST_SCHEMA, op="trust",
                       max_tokens=MAX_TOKENS, timeout=TIMEOUT_S, model=llm.QWEN_35B,
                       temperature=0.0)
    raw = out["score"]
    # The grammar holds the enum; this holds the day the schema is edited and the
    # grammar is not, or a server answers without applying it at all. An out-of-range
    # score must be a refusal, never a stored number: `13` §9 p.4.
    if raw not in SCORES:
        raise llm.LLMError(f"trust: score {raw!r} is outside {SCORES}")
    return {"score": int(raw) / 10.0, "reason": out["reason"],
            "leaves_shown": len(shown), "leaves_total": len(leaves)}


def refresh(idea_id: str) -> dict:
    """Judge one idea and store the result. Raises whatever the judge raised.

    An idea with no leaves is 0.0 BY DEFINITION and costs no call: a hypothesis carries
    no evidence yet (`12-decisions-meetings.md:70-72`), and 0.0 is precisely what "no
    evidence" means on this scale. That is a decision, not a failure, so the flag comes
    down with it.
    """
    ideas = graph_client.get_ideas([idea_id])
    if not ideas:
        raise KeyError(f"trust: idea {idea_id} is not in the graph")
    idea = ideas[0]
    leaves = idea["theses"]
    if not leaves:
        graph_client.set_trust(idea_id, 0.0)
        return {"idea_id": idea_id, "score": 0.0, "reason": "no leaves, nothing to judge",
                "leaves_shown": 0, "leaves_total": 0, "called": False}

    verdict = judge(idea, leaves)
    graph_client.set_trust(idea_id, verdict["score"])
    return {"idea_id": idea_id, **verdict, "called": True}


def sweep(idea_ids: list[str]) -> dict:
    """Judge every named idea, counting refusals separately. Never raises.

    `trust_failed` is its own number in the report on purpose: folded into one
    "skipped" it would read the same as "already clean", and those are opposite
    outcomes (`lake/README.md:191-195` fixed the same confusion once already).
    """
    scored, failed, capped = [], [], 0
    for idea_id in idea_ids:
        try:
            got = refresh(idea_id)
        except Exception as exc:                      # noqa: BLE001 — reported, not raised
            failed.append({"idea_id": idea_id, "error": f"{type(exc).__name__}: {exc}"})
            continue
        capped += got["leaves_shown"] < got["leaves_total"]
        scored.append(got)
    return {"trust_scored": len(scored), "trust_failed": len(failed),
            "trust_leaves_capped": capped, "trust_errors": failed,
            "trust_mean": round(sum(s["score"] for s in scored) / len(scored), 3)
            if scored else 0.0}


def run_pass(idea_ids: list[str] | None = None) -> dict:
    """One judging pass, on demand — an operator-triggered door onto `sweep()`,
    reusing it whole rather than a second definition of how a pass runs.

    Finding (review, 2026-07-31): the corpus that predates the judge (ideas ingested
    before `13`, all `trust_score=0.0, dirty=0`) can never appear in `dirty_ideas()` —
    no leaf write ever touched them, so nothing ever raised their flag — and the
    phase-2 sweep only ever looks at that list. `0.0` is a legal score ("judged,
    little to trust"), so those ideas are stuck indistinguishable from "judged and
    found worthless", forever, with no way to ask for an actual judgement.

    Two ways to close that were weighed. (a) Mark the named ideas dirty and leave the
    actual judging to the next phase-2 run: fewer new paths, and `dirty_ideas()` stays
    the one place a pass reads its worklist from. Rejected here because it does not
    answer "give the operator a way to ask for a (re)judgement" — an operator asking
    right now would still have to also start an ingest, and for the ideas this exists
    for there may be no ingest pending to start; the corpus is already fully linked,
    nothing about its leaves is going to change. (b) Call `sweep()` directly, right
    here: `idea_ids` names exactly which ideas to judge regardless of their `dirty`
    flag, bypassing `dirty_ideas()` rather than adding a second way to decide it — the
    thing `13` §3.3 warns a separate entry point risks being ("a second definition of
    when judging happens"). Chosen, because "how a pass runs" stays the ONE thing
    `sweep()` defines; this only adds a second *trigger* for it — the exact split the
    warning is about avoiding.

    `idea_ids=None` means "whatever is already dirty, run now instead of waiting for
    the next phase 2" — the same worklist `_phase2` reads, just not deferred. Either
    way this is honest about scale exactly like the phase-2 step it mirrors: capped at
    `TRUST_PER_PASS` (`ingest/run.py`, one 35B call per idea), with `trust_due` and
    `trust_deferred` reported the same way (`13` §9 p.3) — a truncated on-demand pass
    must not read as a finished one any more than a truncated phase-2 one does.
    """
    from .run import TRUST_PER_PASS  # local: `run` pulls in llm/writer_lock at import
    # time, and importing it here (not at module scope) avoids paying that just to
    # define this function; by the time this runs, `run` is already loaded whenever
    # phase 2 is the caller, and importing it fresh costs nothing extra for the HTTP
    # entry point either.
    due = idea_ids if idea_ids is not None else graph_client.dirty_ideas()
    report = sweep(due[:TRUST_PER_PASS])
    report["trust_due"] = len(due)
    report["trust_deferred"] = max(0, len(due) - TRUST_PER_PASS)
    return report


if __name__ == "__main__":
    import json

    def leaf(**kw):
        base = {"id": "th_1", "source_type": "paper", "source_title": "A Paper",
                "text": "t", "context": "c", "effect": "+3 pp", "created_at": "2026-01-01",
                "run_success": None}
        return {**base, **kw}

    idea = {"id": "idea_1", "text": "freeze the encoder", "applicability_conditions": "ac",
            "limitations": "lim", "failure_modes": ["weak encoder"], "effect_claimed": "+3 pp",
            "effect_observed": ""}

    # 1. the slice is deterministic and puts measured run evidence first.
    leaves = [leaf(id="th_b", created_at="2026-01-02"),
              leaf(id="th_a", created_at="2026-01-02"),
              leaf(id="th_run", source_type="run", run_success=False, created_at="2026-01-09"),
              leaf(id="th_null", source_type="run", run_success=None, created_at="2026-01-01")]
    order = [l["id"] for l in leaf_order(leaves)]
    assert order == ["th_run", "th_null", "th_a", "th_b"], order
    assert order == [l["id"] for l in leaf_order(list(reversed(leaves)))], "order is not total"

    # 2. the call: model, op, schema, temperature, and the ceiling on leaves shown.
    seen = {}

    def fake_complete(prompt, **kw):
        seen.update(kw, prompt=prompt)
        return {"reason": "one source, no run evidence", "score": "5"}

    llm.complete, real_complete = fake_complete, llm.complete
    llm.load_prompt, real_prompt = (lambda step: f"<{step}>"), llm.load_prompt
    try:
        many = [leaf(id=f"th_{n:03d}", created_at=f"2026-02-{n:02d}") for n in range(1, 26)]
        verdict = judge(idea, many)
        assert verdict["score"] == 0.5, verdict
        assert verdict["leaves_shown"] == MAX_LEAVES and verdict["leaves_total"] == 25, verdict
        assert seen["model"] == llm.QWEN_35B and seen["op"] == "trust", seen
        assert seen["schema"] is TRUST_SCHEMA and seen["temperature"] == 0.0
        assert seen["timeout"] == TIMEOUT_S and seen["max_tokens"] == MAX_TOKENS
        assert seen["system"] == "<trust>", seen["system"]
        assert "leaves_shown: 16" in seen["prompt"] and "leaves_total: 25" in seen["prompt"]
        assert seen["prompt"].count("source_type:") == MAX_LEAVES, "more leaves than the cap"

        # 3. a score outside the enum is a refusal, not a stored number.
        for bad in ("12", "-1", "", "7.5", 7):
            llm.complete = lambda *a, _b=bad, **k: {"reason": "r", "score": _b}
            try:
                judge(idea, [leaf()])
            except llm.LLMError:
                pass
            else:
                raise AssertionError(f"score {bad!r} was accepted")

        # 4. every enum value maps onto [0, 1] and 10 is the top.
        for raw in SCORES:
            llm.complete = lambda *a, _r=raw, **k: {"reason": "r", "score": _r}
            assert judge(idea, [leaf()])["score"] == int(raw) / 10.0
        assert judge(idea, [leaf()])["score"] == 1.0

        # 5. run leaves are rendered with their outcome; a paper leaf has no such line.
        llm.complete = fake_complete
        judge(idea, [leaf(id="r", source_type="run", run_success=None)])
        assert "run_success: null (no measurement exists)" in seen["prompt"]
        judge(idea, [leaf(id="r", source_type="run", run_success=True)])
        assert "run_success: true" in seen["prompt"]
        judge(idea, [leaf()])
        assert "run_success" not in seen["prompt"], "a paper leaf must not claim an outcome"
    finally:
        llm.complete, llm.load_prompt = real_complete, real_prompt

    # 6. the real prompt file exists, is non-empty and states the scale it promises.
    text = llm.load_prompt("trust")
    for anchor in ("leaves_shown", "leaves_total", "run_success", "reason", "score"):
        assert anchor in text, anchor
    assert all(f'"{n}"' in text or f" {n}" in text for n in ("0", "10")), "the scale is unstated"

    print("ok: " + json.dumps({"cap": MAX_LEAVES, "scores": len(SCORES),
                               "order": order}, ensure_ascii=False))
