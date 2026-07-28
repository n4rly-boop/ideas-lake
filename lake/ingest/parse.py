"""Step 1c (spec 10 §4.3): one section -> at most 6 draft theses, one LLM call.

The abstract and the limitations block ride along in every call as reference material:
`draft_limitations` is the named quality risk (08:391), and a thesis pulled out of
Method knows nothing about the price of the technique without them. The price of that
decision is named in §4.3: the abstract is sent ~6 times per paper.
"""
import hashlib
import json
import uuid
from pathlib import Path

from .. import llm
from ..models import CACHE_DIR, PARSE_SCHEMA, DraftThesis, Section
from ..trace import trace

MAX_PER_DOCUMENT = 30      # §8. The per-section ceiling of 6 is held by the grammar.


@trace(component="ingest", op="parse")
def parse_section(section: Section, abstract: str, limitations: str, *,
                  cache_dir: Path = CACHE_DIR) -> list[DraftThesis]:
    """One LLM call for one section.

    An empty list is a legal answer, not a failure: related work and acknowledgements
    state no technique. LLMError is deliberately not caught — phase 1 (run.py) owns
    the decision of what a dead section costs.
    """
    system = llm.load_prompt("parse")
    path = _cache_path(cache_dir, section.text, system)
    if path.exists():
        # A corrupt cache file raises here instead of quietly turning into a refetch.
        obj = json.loads(path.read_text(encoding="utf-8"))
    else:
        obj = llm.complete(_user_message(section, abstract, limitations),
                           system=system, schema=PARSE_SCHEMA, op="parse",
                           max_tokens=2500, timeout=120, model=llm.QWEN_9B,
                           temperature=0.0)
        _cache_write(path, obj)
    return [DraftThesis(**item) for item in obj["theses"]]


def parse_document(sections: list[Section], abstract: str, limitations: str, *,
                   cache_dir: Path = CACHE_DIR) -> tuple[list[DraftThesis], dict]:
    """All sections of one document, cut to 30 theses (§4.3 p.6 — this ceiling is code).

    Returns (theses, report). `report["per_section"]` carries the count for every
    section including the zeros, `report["dropped"]` the number the ceiling cut off:
    both go into the run report, neither is lost silently.
    """
    theses: list[DraftThesis] = []
    per_section: dict[str, int] = {}
    skipped: list[str] = []
    for section in sections:
        if len(theses) >= MAX_PER_DOCUMENT:
            # Stop calling once the ceiling is reached instead of parsing the whole
            # document and slicing at the end: the discarded sections cost a full
            # prompt each, ~2500 max_tokens, on every source that runs long.
            skipped.append(section.id)
            continue
        extracted = parse_section(section, abstract, limitations, cache_dir=cache_dir)
        per_section[section.id] = len(extracted)
        theses.extend(extracted)
    kept = theses[:MAX_PER_DOCUMENT]
    return kept, {"per_section": per_section, "dropped": len(theses) - len(kept),
                  "sections_not_parsed": skipped}


def _user_message(section: Section, abstract: str, limitations: str) -> str:
    """Labelled blocks, in the order the prompt file announces them.

    The paper title is not in the signature (contract C_A), so the section speaks for
    itself; the two reference blocks are labelled as such, and their absence is said
    out loud rather than left as an empty block for the model to fill in.
    """
    return (f"SECTION {section.id} — {section.title}\n"
            f"{section.text}\n\n"
            f"ABSTRACT (reference only)\n{abstract.strip() or '(not available)'}\n\n"
            f"LIMITATIONS BLOCK (reference only)\n"
            f"{limitations.strip() or '(this paper states none)'}\n")


def _cache_path(cache_dir: Path, section_text: str, system: str) -> Path:
    """§4.8: keyed by (section_hash, prompt_hash).

    Editing the parser prompt invalidates extraction without refetching the papers;
    a cache hit costs zero LLM calls.
    """
    section_hash = hashlib.md5(section_text.encode("utf-8")).hexdigest()
    prompt_hash = hashlib.md5(system.encode("utf-8")).hexdigest()
    return cache_dir / f"parse_{section_hash}_{prompt_hash}.json"


def _cache_write(path: Path, obj: dict) -> None:
    # Written through a unique temp name: phase 1 runs 8 threads, and a half-written
    # cache file would be read back as a hard JSON error on every later run.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


if __name__ == "__main__":
    import tempfile

    from .. import trace as trace_mod
    from ..models import TRACES_DIR

    trace_mod.set_run_id("selfcheck-parse")
    item = {"text": "A cheap-model prefilter drops 70% of candidates before full "
                    "evaluation.",
            "context": "ImageNet with ResNet-50", "effect": "-70% compute",
            "locator": "Methodology, 3.2",
            "draft_text": "score candidates with a cheap proxy first",
            "draft_applicability": "an expensive evaluator exists",
            "draft_limitations": "the proxy must correlate with the true score"}
    calls = {"n": 0}

    def fake_complete(prompt, **kw):
        calls["n"] += 1
        assert "ABSTRACT (reference only)" in prompt, prompt
        assert "LIMITATIONS BLOCK (reference only)" in prompt, prompt
        assert (kw["max_tokens"], kw["timeout"], kw["temperature"]) == (2500, 120, 0.0), kw
        assert kw["op"] == "parse" and kw["schema"] is PARSE_SCHEMA and kw["model"] == llm.QWEN_9B
        return {"theses": [] if "no technique here" in prompt else [item] * 6}

    llm.complete = fake_complete
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = Path(tmpdir)
        method = Section(id="S3", kind="section", title="Method", text="cascade text")

        first = parse_section(method, "abstract text", "limitations text", cache_dir=cache)
        assert calls["n"] == 1
        assert len(first) == 6 and isinstance(first[0], DraftThesis)
        assert first[0].effect == "-70% compute" and first[0].locator == "Methodology, 3.2"

        again = parse_section(method, "abstract text", "limitations text", cache_dir=cache)
        assert calls["n"] == 1, "cache hit must cost zero LLM calls"
        assert [t.text for t in again] == [t.text for t in first]

        # A different prompt text must miss the cache even for the same section (§4.8).
        moved = _cache_path(cache, method.text, llm.load_prompt("parse") + " edited")
        assert not moved.exists() and moved != _cache_path(cache, method.text,
                                                           llm.load_prompt("parse"))

        empty = Section(id="S7", kind="section", title="Related Work",
                        text="no technique here, only citations")
        assert parse_section(empty, "abstract text", "", cache_dir=cache) == []

        sections = [Section(id=f"S{i}", kind="section", title=f"Part {i}",
                            text=f"body {i}") for i in range(6)] + [empty]
        theses, report = parse_document(sections, "abstract text", "limitations text",
                                        cache_dir=cache)
        assert len(theses) == MAX_PER_DOCUMENT, len(theses)
        assert report["dropped"] == 36 - MAX_PER_DOCUMENT, report
        assert report["per_section"]["S7"] == 0, report      # zero is a count, not an error
        assert sum(report["per_section"].values()) == len(theses) + report["dropped"]

    (TRACES_DIR / f"{trace_mod.current_run_id()}.jsonl").unlink(missing_ok=True)
    print(f"ok: parse -> DraftThesis, cache hit costs 0 calls ({calls['n']} total), "
          f"document ceiling {MAX_PER_DOCUMENT}, dropped reported")
