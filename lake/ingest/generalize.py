"""Step 1d (spec 10 §4.4): one draft thesis -> the implementation-free idea fields.

A separate step and not a pass inside 1c, because generalization is irreversible: the
pre-generalized text has to survive on the leaf and this layer has to stay replaceable
(§4.4). The leakage check lives here, is computed for every thesis, and travels into
the run report — a leak is a quality metric (target <=10%, §4.4), not a reason to stop
a run, so it never raises.
"""
import re

from .. import llm
from ..models import GENERALIZE_SCHEMA, DraftThesis, IdeaFields
from ..trace import trace

# A number keeps its grouping and decimals ("3.4", "1,000"), so "70" cannot match
# inside "1970": both sides are tokenized the same way and then intersected.
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")

# A name token: letters first, digits and inner hyphens allowed — GSM8K, GPT-4,
# ResNet-50, MAP-Elites, ImageNet.
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-'][A-Za-z0-9]+)*")

# Capitalization alone would flag every sentence opener and every capitalized common
# noun; the sentence-start filter below removes the first, this list the second.
_STOPWORDS = frozenset("""
a an the and or but of in on at for with to from by as is are was were be been it its
this that these those we our they their he she his her you your i not no than then
when where which who what while during over under between into out up down after
before all both each few more most other some such only own same so too very can will
just should now here there
paper section sections table tables figure figures appendix abstract
model models method methods approach technique dataset datasets benchmark benchmarks
task tasks baseline baselines result results experiment experiments evaluation
training trained test testing validation accuracy loss data set sets
gpu gpus cpu cpus tpu tpus
""".split())


@trace(component="ingest", op="generalize")
def generalize(draft: DraftThesis, *, prompt: str = "system") -> IdeaFields:
    """One LLM call. LLMError is not caught: run.py decides what a failed thesis costs.

    `prompt` names the variant inside `prompts/generalize/` — `run` for a mutation log,
    whose concrete embodiment is code, not a dataset (`13` §2.2.1).
    """
    obj = llm.complete(_user_message(draft), system=llm.load_prompt("generalize", prompt),
                       schema=GENERALIZE_SCHEMA, op="generalize", max_tokens=800,
                       timeout=60, model=llm.QWEN_9B, temperature=0.0)
    return IdeaFields(**obj)


def leakage(draft: DraftThesis, out: IdeaFields, extra_terms=()) -> list[str]:
    """§4.4 — automatic check that the concrete embodiment did not survive.

    Two rules: a number from `draft.effect` in `out.text`, and a dataset / model /
    benchmark name from `draft.context` in `out.text`. Returns one string per
    violation; an empty list means clean. Never raises and never blocks a run.

    `extra_terms` is the third rule, and it exists because the first two are shaped for
    a paper. In an evolution log the concrete thing is a program id, a function name out
    of the mutant's source, a task name — none of which `_names` recognises: they are
    snake_case, so they carry neither a capital nor a digit and the heuristic drops them.
    A green check written for papers says nothing about a log (`13` §9 p.9), so the
    caller that knows the log passes the terms it knows. Terms shorter than 3 characters
    are ignored: a one-letter name matches everywhere and would make the check useless
    by firing constantly.
    """
    violations = []
    leaked_numbers = set(_NUMBER.findall(draft.effect)) & set(_NUMBER.findall(out.text))
    for number in sorted(leaked_numbers):
        violations.append(f"number {number!r} from effect leaked into text")
    for lowered, original in sorted(_names(draft.context).items()):
        if _mentions(out.text, lowered):
            violations.append(f"name {original!r} from context leaked into text")
    for term in sorted({t for t in extra_terms if len(t) >= 3}):
        if _mentions(out.text, term):
            violations.append(f"term {term!r} from the source leaked into text")
    return violations


def _names(context: str) -> dict[str, str]:
    """Proper names out of `context`, as {lowercase: as written}.

    A digit inside the token (GSM8K, ResNet-50) or a capital past the first character
    (ImageNet, PyTorch, MAP-Elites, BLEU) is a name on its own, wherever it stands —
    a context string usually opens with the dataset name, so no position filter may
    touch these. A plainly capitalized token (Cityscapes, Adam) is a name only away
    from a sentence start, where the capital would just be grammar.

    ponytail: a capitalized common word opening a context ("Evaluated on ...") is a
    missed name; a word list is the upgrade if the measured leak rate needs it.
    """
    names: dict[str, str] = {}
    for match in _WORD.finditer(context):
        token = match.group()
        if len(token) < 2 or token.lower() in _STOPWORDS:
            continue
        if any(char.isdigit() for char in token) or any(c.isupper() for c in token[1:]):
            names.setdefault(token.lower(), token)
        elif token[0].isupper() and not _opens_sentence(context, match.start()):
            names.setdefault(token.lower(), token)
    return names


def _opens_sentence(text: str, position: int) -> bool:
    before = text[:position].rstrip()
    return not before or before[-1] in ".!?:;\n"


def _mentions(text: str, term: str) -> bool:
    # Case-insensitive, but not a substring match: "MAP" must not fire on "mapping".
    return re.search(rf"(?<![\w-]){re.escape(term)}(?![\w-])", text, re.IGNORECASE) is not None


def _user_message(draft: DraftThesis) -> str:
    return (f"THESIS\n{draft.text}\n\n"
            f"CONTEXT\n{draft.context.strip() or '(none stated)'}\n\n"
            f"EFFECT\n{draft.effect.strip() or '(no number stated)'}\n\n"
            f"PARSER DRAFTS (a starting point, correct them)\n"
            f"draft_text: {draft.draft_text}\n"
            f"draft_applicability: {draft.draft_applicability}\n"
            f"draft_limitations: {draft.draft_limitations}\n")


if __name__ == "__main__":
    from .. import trace as trace_mod
    from ..models import TRACES_DIR

    trace_mod.set_run_id("selfcheck-generalize")
    draft = DraftThesis(
        text="A cheap-model prefilter drops 70% of candidates before full evaluation "
             "on ImageNet with ResNet-50.",
        context="ImageNet classification with ResNet-50 on 8 GPUs, MAP-Elites archive",
        effect="-70% compute", locator="Methodology, 3.2",
        draft_text="score candidates with a cheap proxy first",
        draft_applicability="an expensive evaluator exists",
        draft_limitations="the proxy must correlate with the true score")

    answer = {"text": "cascaded evaluation: a cheap proxy scores candidates first and "
                      "only the survivors reach the expensive evaluator",
              "applicability_conditions": "the expensive evaluator dominates the budget",
              "limitations": "requires a proxy that ranks consistently",
              "failure_modes": ["a proxy uncorrelated with the true score discards winners"]}

    def fake_complete(prompt, **kw):
        assert "THESIS" in prompt and "PARSER DRAFTS" in prompt, prompt
        assert (kw["max_tokens"], kw["timeout"], kw["temperature"]) == (800, 60, 0.0), kw
        assert kw["op"] == "generalize" and kw["schema"] is GENERALIZE_SCHEMA
        assert kw["model"] == llm.QWEN_9B
        return answer

    llm.complete = fake_complete
    clean = generalize(draft)
    assert isinstance(clean, IdeaFields) and clean.failure_modes == answer["failure_modes"]
    assert leakage(draft, clean) == [], leakage(draft, clean)

    leaky = IdeaFields(text="a cheap proxy drops 70% of the ImageNet candidates before "
                            "the ResNet-50 evaluator runs",
                       applicability_conditions="", limitations="", failure_modes=[])
    found = leakage(draft, leaky)
    assert any("'70'" in v and "number" in v for v in found), found
    assert any("ImageNet" in v for v in found), found
    assert any("ResNet-50" in v for v in found), found
    assert len(found) == 3, found

    # "1970" must not count as the number 70, and "mapping" must not count as MAP-Elites.
    near_miss = IdeaFields(text="an idea from 1970 about mapping candidate archives",
                           applicability_conditions="", limitations="", failure_modes=[])
    assert leakage(draft, near_miss) == [], leakage(draft, near_miss)

    # A sentence opener is grammar, a mid-sentence capital is a name.
    opener = DraftThesis(text="t", context="Evaluated on the MAP-Elites archive.",
                         effect="", locator="", draft_text="", draft_applicability="",
                         draft_limitations="")
    mixed = IdeaFields(text="evaluated variants are kept in an archive",
                       applicability_conditions="", limitations="", failure_modes=[])
    assert leakage(opener, mixed) == [], leakage(opener, mixed)
    assert leakage(opener, IdeaFields(text="kept in a MAP-Elites archive",
                                      applicability_conditions="", limitations="",
                                      failure_modes=[]))

    (TRACES_DIR / f"{trace_mod.current_run_id()}.jsonl").unlink(missing_ok=True)
    print("ok: generalize -> IdeaFields, leakage catches number + dataset + model, "
          "clean case empty, 1970/mapping not false positives")
