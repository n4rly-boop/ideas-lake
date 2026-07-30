"""Data models (spec 10 §1) and the literal JSON schemas handed to llama.cpp (§3.1).

The records are pydantic models: a row coming back from the store, a line of
`staging.jsonl` or a batch handed to the graph is validated at the boundary
instead of being trusted. `extra="forbid"` on every one of them — a field that
drifted out of the store schema must fail loudly, not arrive as a silent None.

**The LLM schemas below stay literal dicts and are NOT generated from these
models.** `pydantic.model_json_schema()` emits `$ref`, llama.cpp resolves those
only after PR #21699 and then hits MAX_REPETITION_THRESHOLD, after which the
grammar silently fails to build (09:67) — the server answers 200 with prose and
nothing in the response says the schema was ignored. The two representations are
held together by an assert instead (§6.11, `SCHEMA_BINDINGS`).
"""
import hashlib
import re
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

DATA = Path(__file__).resolve().parent / "data"
PROMPTS = Path(__file__).resolve().parent / "prompts"

STAGING = DATA / "staging.jsonl"
STAGING_CURSOR = DATA / "staging.cursor"
PENDING_LINK = DATA / "pending_link.jsonl"
INDEX_DB = DATA / "index.db"
LAKE_DB = DATA / "lake.db"
FETCH_DIR = DATA / "fetch"      # one staging file per single-url ingest, see run.ingest_one
JOBS_DB = DATA / "jobs.db"      # the durable job queue, see queue.py — not format B
RAW_DIR = DATA / "raw"
CACHE_DIR = DATA / "cache"
TRACES_DIR = DATA / "traces"
LOGS_DIR = DATA / "logs"

EMBED_DIM = 384


# --------------------------------------------------------------------------- ids

def normalize(text: str) -> str:
    """Case- and whitespace-insensitive form used for `text_hash` (§4.8)."""
    return re.sub(r"\s+", " ", text).strip().lower()


def text_hash(text: str) -> str:
    return hashlib.md5(normalize(text).encode("utf-8")).hexdigest()


def source_id(url: str, version: str) -> str:
    return hashlib.sha1((url + version).encode("utf-8")).hexdigest()[:16]


def new_thesis_id() -> str:
    return "th_" + uuid.uuid4().hex[:12]


def new_idea_id() -> str:
    # uuid7 lands in 3.14, we are on 3.12 (09:293).
    return "idea_" + uuid.uuid4().hex[:12]


# ----------------------------------------------------------------------- records

class Record(BaseModel):
    """Shared config: unknown fields are an error, not something to ignore."""
    model_config = ConfigDict(extra="forbid")


class Source(Record):
    id: str
    url: str
    title: str
    type: str            # paper | doc | run
    version: str
    retrieved_at: str
    run_success: bool | None = None
    run_meta: dict | None = None


class Thesis(Record):
    id: str
    source_id: str
    idea_id: str
    text: str
    context: str
    effect: str
    locator: str
    text_hash: str
    vector: list[float] = Field(..., min_length=EMBED_DIM, max_length=EMBED_DIM)
    created_at: str


class Idea(Record):
    id: str
    text: str
    applicability_conditions: str
    limitations: str
    failure_modes: list[str]
    effect_claimed: str
    effect_observed: str
    vector: list[float] = Field(..., min_length=EMBED_DIM, max_length=EMBED_DIM)
    differentiation: str | None = None
    trust_score: float = 0.0          # written by B
    dirty: bool = False               # written by B
    rederived_at_leaf_count: int = 0  # written by A, trigger of §4.6
    created_at: str = ""
    updated_at: str = ""


class Section(Record):
    id: str
    kind: str            # section | bibliography | appendix | chunk
    title: str
    text: str


class DraftThesis(Record):
    """Output of 1c (§4.3). `draft_*` fields are derived, not stated by the source."""
    text: str
    context: str
    effect: str
    locator: str
    draft_text: str
    draft_applicability: str
    draft_limitations: str


class IdeaFields(Record):
    """Output of 1d (§4.4)."""
    text: str
    applicability_conditions: str
    limitations: str
    failure_modes: list[str] = Field(default_factory=list)


# ----------------------------------------------------------------------- schemas
# Every schema: flat, no $ref, additionalProperties False, every property required
# (llama.cpp grammar treats optional properties as a branch point, and a missing
# field is indistinguishable from a refusal downstream).
# maxLength is a runaway guard, not formatting: hitting it exactly is treated as
# truncation and raises LLMError (§3.1 p.7), so ceilings carry slack.

CANARY_SCHEMA = {
    "type": "object",
    "properties": {"canary": {"const": "llamacpp"}},
    "required": ["canary"],
    "additionalProperties": False,
}

PARSE_SCHEMA = {
    "type": "object",
    "properties": {
        "theses": {
            "type": "array",
            "maxItems": 6,
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "maxLength": 700},
                    "context": {"type": "string", "maxLength": 400},
                    "effect": {"type": "string", "maxLength": 200},
                    "locator": {"type": "string", "maxLength": 120},
                    "draft_text": {"type": "string", "maxLength": 700},
                    "draft_applicability": {"type": "string", "maxLength": 400},
                    "draft_limitations": {"type": "string", "maxLength": 400},
                },
                "required": ["text", "context", "effect", "locator",
                             "draft_text", "draft_applicability", "draft_limitations"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["theses"],
    "additionalProperties": False,
}

GENERALIZE_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "maxLength": 700},
        "applicability_conditions": {"type": "string", "maxLength": 500},
        "limitations": {"type": "string", "maxLength": 500},
        "failure_modes": {
            "type": "array",
            "maxItems": 4,
            "items": {"type": "string", "maxLength": 300},
        },
    },
    "required": ["text", "applicability_conditions", "limitations", "failure_modes"],
    "additionalProperties": False,
}

# Arbiter answers with a candidate index or the -1 "no duplicate" sentinel (09:151).
LINK_SCHEMA = {
    "type": "object",
    "properties": {"link_to": {"type": "integer", "minimum": -1}},
    "required": ["link_to"],
    "additionalProperties": False,
}

# effect_claimed / effect_observed are two schema properties so that "never merged"
# (06:200) is held by the grammar, not by prompt discipline (§4.6).
REDERIVE_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "maxLength": 700},
        "applicability_conditions": {"type": "string", "maxLength": 500},
        "limitations": {"type": "string", "maxLength": 500},
        "failure_modes": {
            "type": "array",
            "maxItems": 5,
            "items": {"type": "string", "maxLength": 300},
        },
        # 900, not 400: these two aggregate over every leaf of the idea, so the
        # ceiling has to grow with leaf count. At 400 the first real run truncated
        # two ideas mid-word and the §3.1 p.7 guard rejected both — the guard was
        # right, the ceiling was too tight to carry the slack §3.1 asks for.
        "effect_claimed": {"type": "string", "maxLength": 900},
        "effect_observed": {"type": "string", "maxLength": 900},
    },
    "required": ["text", "applicability_conditions", "limitations", "failure_modes",
                 "effect_claimed", "effect_observed"],
    "additionalProperties": False,
}

REWRITE_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string", "maxLength": 300}},
    "required": ["query"],
    "additionalProperties": False,
}

# (schema, model, path to the object whose properties must match its fields).
# Checked by selfcheck §6.11: schema property names ⊆ model fields. This is what
# keeps the literal schemas above and the models here from drifting apart, since
# one is not generated from the other.
SCHEMA_BINDINGS = [
    (PARSE_SCHEMA, DraftThesis, ["theses", "items"]),
    (GENERALIZE_SCHEMA, IdeaFields, []),
    (REDERIVE_SCHEMA, Idea, []),
]


def schema_properties(schema: dict, path: list[str]) -> set[str]:
    """Walk `path` ('theses' -> 'items') down to the object carrying properties."""
    node = schema
    for step in path:
        node = node["properties"][step] if step in node.get("properties", {}) else node[step]
    return set(node["properties"])


def model_field_names(cls) -> set[str]:
    return set(cls.model_fields)
