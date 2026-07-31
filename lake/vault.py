"""Vault export (spec 11): the lake as a folder of markdown notes Obsidian draws.

Reads the graph through `graph_client` only — format B stays in that module
(§3.4) — and writes files with `pathlib`. Zero dependencies, zero rendering code:
the graph view, the search and the backlinks are Obsidian's (§11.1). Vectors are
not exported, 384 floats in markdown mean nothing (§11.5).

    python3 -m lake.vault [--dest PATH]
    python3 -m lake.vault --self-check     # §11.6, temp fixture, no network, no data/

Nothing here writes to the lake. The export is a whole-folder rebuild rather than
an update: an incremental one would keep the notes of nodes that no longer exist,
and the graph would show a lake that is gone (§11.3.4).

Refusals are `lake.ops` exceptions, so the future `POST /vault/export` gets its
status for free (`app.py:OPS_STATUS`): `Conflict` for what the operator can fix —
a foreign `--dest`, an id that is not a file name, an empty lake — and `Broken`
for a store that contradicts itself or a file count that does not add up.

Three known limits, left as they are on purpose:

* `escape`/`_line` coerce `None` to `""`. Unreachable: the store refuses NULL in
  these columns (`stub_store._NULLABLE_IDEA_FIELDS`).
* `ID_RE` admits neither a dot nor a separator, so even when the slug is empty and
  the file name is the bare id, there is no `..`, no `/` and no dotfile to reach.
* an empty `data/lake.db` is created by whoever opens the store first; that is
  `graph_client`'s behaviour, shared by every module, and not vault's to fix.
  What vault does about it is refuse to report a successful export of zero nodes.
"""
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import graph_client, ops
from .models import DATA

PAGE = 500          # rows per store read, and the IN(...) width of `get_ideas`

SUBDIRS = ("ideas", "theses", "sources")
OWNED = set(SUBDIRS) | {"README.md"}

# Written into `dest` and kept there (dotfiles survive `_clear`). A directory that
# holds notes without it was not written by this export, whatever its files are
# called — "ideas/ + README.md" is a plausible shape for somebody's own vault.
MARKER = ".lake-vault"
# Everything is built here first and moved in at the end, so a failure halfway
# leaves the previous export intact instead of a smaller lake with no README. The
# name is unique per run (`tempfile.mkdtemp`): under a fixed one, two exports into
# the same `dest` destroy each other — the second wipes the first's half-built
# notes and the first's swap then fails — and one killed run wedges every later
# one on `FileExistsError` forever.
BUILD_PREFIX = ".export."
BUILD_SUFFIX = ".tmp"

NAME_MAX = 40       # slug length before the id; a file name stays readable in a graph

# Ids are `th_`/`idea_` + hex and sha1[:16] for sources, so the id needs no encoding
# scheme to become part of a file name — but it is CHECKED, not assumed (§11.3.3):
# a '/' or a '..' would write outside `dest`, and ':' '?' '*' '"' '<' '>' '|' are
# names Windows and iCloud refuse without saying why.
ID_RE = re.compile(r"[A-Za-z0-9_-]+")

# Used by the self-check to walk what was written. Deliberately loose: `[[(.+?)]]`
# and not "no brackets inside", because a target that DOES hold a bracket is the
# ghost node §11.3.2 is about, and a regex that cannot see it cannot catch the
# regression. `\[\[` (escaped, see `escape`) does not match — the backslash between
# the brackets breaks the pair, which is the property the check relies on.
WIKILINK_RE = re.compile(r"\[\[(.+?)\]\]")

# C0 minus \t and \n, DEL, and C1. `\x7f` and `\x9f` make the YAML reader throw and
# the whole frontmatter of the note is then unparsed — the note drops out of every
# filter and search while looking fine in the editor; `\x85` turns into a line break
# mid-value. json.dumps passes all three through unescaped.
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


# ------------------------------------------------------------------- formatting

def escape(text: str) -> str:
    """`[` and `]` in source text -> literal brackets (§11.3.2).

    Theses are quotes from papers and brackets occur in them. Left alone they
    become links to notes that do not exist, and the graph grows ghost nodes that
    are not in the lake — a wrong picture, drawn confidently.

    Every bracket, not every pair: escaping `[[` alone is not idempotent, and
    `[[[a]]]` came back out as `\\[\\[[a\\]\\]]`, whose characters 3-4 are a fresh
    unescaped `[[`. One backslash per bracket makes two adjacent unescaped brackets
    impossible to produce. Noisier in the raw file, identical once rendered — and
    the file is generated.
    """
    return (text or "").replace("[", r"\[").replace("]", r"\]")


def _line(text: str) -> str:
    """One-line form for a heading, a table cell or a list item.

    Collapsing the whitespace is not cosmetic: a `\\n` inside a list item ends the
    item, and a body value that starts a line with `---` or `#` puts a horizontal
    rule or somebody else's H1 into the note.
    """
    return escape(" ".join((text or "").split())).replace("|", r"\|")


def _clean(value):
    """Escape wikilinks and drop control characters inside a frontmatter value.

    At any depth: Obsidian reads links out of frontmatter too, so a `[[` left in
    `run_meta` would produce the same ghost node as one left in the body.
    """
    if isinstance(value, str):
        return escape(CONTROL_RE.sub("", value))
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _scalar(value) -> str:
    """One YAML scalar. Strings and containers go through `json.dumps`: JSON is a
    subset of YAML 1.2, and quoting is what keeps a ':' or a '#' inside an effect
    string from re-parsing the frontmatter into something else."""
    if isinstance(value, bool):     # before int: bool IS an int
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(_clean(value if isinstance(value, (dict, list)) else str(value)),
                      ensure_ascii=False)


def _frontmatter(fields: dict) -> str:
    """`None` fields are omitted, not written as null: `run_success` belongs to a
    run and a `run_success: null` on every paper would be a filter that lies."""
    body = "\n".join(f"{k}: {_scalar(v)}" for k, v in fields.items() if v is not None)
    return f"---\n{body}\n---\n"


def _url(url: str) -> str:
    """A link destination for `[…](<…>)`. Only `<` and `>` are encoded: they close
    the angle brackets early and leave half a url, and the rest — spaces included —
    is what the `<…>` form exists to carry."""
    return (url or "").replace("<", "%3C").replace(">", "%3E")


def _name(text: str, node_id: str) -> str:
    """File name of one node: `<slug>-<id>`, or the bare id if nothing slugifies.

    Obsidian labels a graph node with the file name and reads no `title` and no H1,
    so `idea-idea_953dd49774a8` draws a graph nobody can read. The id stays in the
    name because a slug is not unique — and it is checked here, before anything is
    deleted or written (§11.3.3).
    """
    if not ID_RE.fullmatch(node_id or ""):
        raise ops.Conflict(f"id {node_id!r} is not a safe file name (§11.3.3)")
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    if len(slug) > NAME_MAX:
        slug = slug[:NAME_MAX].rsplit("-", 1)[0].strip("-")
    return f"{slug}-{node_id}" if slug else node_id


def _names(nodes: list[tuple[str, str]]) -> dict[str, str]:
    """`{node_id: file name}` for the whole lake, resolved before anything is written.

    A link is a file NAME, and `_thesis_note` knows the id of its idea but not the
    idea's text, so the names cannot be built where they are used. Two ids landing
    on one name is refused here by name rather than noticed later as a note that
    silently overwrote another — Obsidian resolves `[[…]]` across folders, so the
    collision matters between types too.
    """
    names: dict[str, str] = {}
    taken: dict[str, str] = {}
    for text, node_id in nodes:
        name = _name(text, node_id)
        owner = taken.setdefault(name, node_id)
        if owner != node_id:
            raise ops.Broken(f"ids {owner!r} and {node_id!r} both want the file {name!r}")
        names[node_id] = name
    return names


def _leaf_item(leaf: dict, names: dict[str, str]) -> str:
    return (f"- [[{names[leaf['id']]}]] — {_line(leaf['effect'])}"
            f" — {_line(leaf['locator'])}")


# ----------------------------------------------------------------------- notes

def _idea_note(idea: dict, names: dict[str, str]) -> str:
    leaves = idea["theses"]
    out = [_frontmatter({
        "type": "idea",
        "id": idea["id"],
        # Only on a broken idea, so `orphan: true` is a filter that finds all of
        # them and nothing else.
        "orphan": True if not leaves else None,
        "leaves": len(leaves),
        "sources": len({leaf["source_id"] for leaf in leaves}),
        "trust_score": round(idea["trust_score"], 4),
        "rederived_at_leaf_count": idea["rederived_at_leaf_count"],
    })]
    out += [f"# {_line(idea['text'])}", ""]
    if not leaves:
        # Not a refusal: `run.py:257` prints INVARIANT BROKEN and carries on, and
        # a lake that cannot be looked at exactly when it is broken is worse than
        # a lake with a note that says so.
        out += ["**Идея без листьев** — нарушен `IDEA ||--|{ THESIS` (`06:85`): "
                "провенанса у неё нет.", ""]
    out += [f"**Условия применимости.** {_line(idea['applicability_conditions'])}",
            f"**Ограничения.** {_line(idea['limitations'])}"]
    if idea["failure_modes"]:
        out.append("**Режимы отказа.**")
        out += [f"- {_line(mode)}" for mode in idea["failure_modes"]]
    # Two rows, never merged into one (`06:200`): the claimed effect is what the
    # source says, the observed one is what a run showed, and a single cell would
    # quietly present the first as the second.
    out += ["", "| | |", "|---|---|",
            f"| Заявленный эффект | {_line(idea['effect_claimed'])} |",
            f"| Наблюдаемый эффект | {_line(idea['effect_observed'])} |",
            "", f"## Листья ({len(leaves)})"]
    out += [_leaf_item(leaf, names) for leaf in leaves]
    return "\n".join(out) + "\n"


def _thesis_note(leaf: dict, names: dict[str, str]) -> str:
    out = [_frontmatter({"type": "thesis", "id": leaf["id"], "idea": leaf["idea_id"],
                         "effect": leaf["effect"], "locator": leaf["locator"]})]
    out += ["> " + escape(line) for line in (leaf["text"] or "").splitlines() or [""]]
    out += ["",
            f"**Контекст.** {_line(leaf['context'])}",
            f"**Идея:** [[{names[leaf['idea_id']]}]]",
            # <> around the url: a ')' in a link destination ends it early, and a
            # half-eaten url is a provenance link that goes nowhere.
            f"**Источник:** [[{names[leaf['source_id']]}]]"
            f" · [ссылка](<{_url(leaf['source_url'])}>)"]
    return "\n".join(out) + "\n"


def _source_note(source: dict, leaves: list[dict], names: dict[str, str]) -> str:
    out = [_frontmatter({"type": "source", "id": source["id"], "kind": source["type"],
                         "version": source["version"], "retrieved_at": source["retrieved_at"],
                         "run_success": source["run_success"], "run_meta": source["run_meta"]})]
    out += [f"# {_line(source['title'])}", "",
            f"[{_line(source['url'])}](<{_url(source['url'])}>)", "",
            f"## Тезисы ({len(leaves)})"]
    out += [_leaf_item(leaf, names) for leaf in leaves]
    return "\n".join(out) + "\n"


def _readme(counts: dict, files: int, orphans: int) -> str:
    built = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = [
        "# Озеро идей — выгрузка", "",
        f"Собрано `python3 -m lake.vault`, {built}.", "",
        "| | |", "|---|---|",
        f"| Идеи | {counts['ideas']} |",
        f"| Тезисы | {counts['theses']} |",
        f"| Источники | {counts['sources']} |",
        f"| Файлов | {files} |", "",
    ]
    if orphans:
        out += [f"**Идей без листьев: {orphans}.** Нарушен `IDEA ||--|{{ THESIS` "
                "(`06:85`), заметки помечены `orphan: true`.", ""]
    out += [
        "Папка пересоздаётся целиком при каждом экспорте, правки в ней не",
        "возвращаются в озеро: озеро — единственная точка правды (§11.7).", "",
        "Граф-вью: каждая идея — сгусток из своих листьев, источники — узлы,",
        "к которым сходится всё.", "",
    ]
    return "\n".join(out)


# ----------------------------------------------------------------------- export

def _all(fetch, expected: int) -> list:
    """Every row of a paged store read. `fetch(limit, offset)`.

    `expected` is the store's own count and the loop's upper bound. Without one, a
    page that comes back without advancing hangs the export forever — and a hang
    reads as a slow lake, not as a store that is answering nonsense.
    """
    out: list = []
    while True:
        page = fetch(PAGE, len(out))
        if not page:
            return out
        out += page
        if len(out) > expected:
            raise ops.Broken(f"a paged read returned {len(out)} rows where the store "
                             f"counts {expected} — the store contradicts itself, or "
                             f"a write landed in the lake while this export read it")


def _read(counts: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """(sources, ideas, theses) — the whole lake, through `graph_client`."""
    sources = _all(graph_client.list_sources, counts["sources"])
    theses = _all(lambda limit, offset: graph_client.list_theses(None, None, limit, offset),
                  counts["theses"])
    idea_ids = _all(graph_client.list_idea_ids, counts["ideas"])
    ideas: list[dict] = []
    for start in range(0, len(idea_ids), PAGE):
        # `get_ideas` joins the leaves, so the idea note needs no second read.
        ideas += graph_client.get_ideas(idea_ids[start:start + PAGE])
    return sources, ideas, theses


def _check_dest(dest: Path, building: str | None = None) -> None:
    """Refuse a directory this export does not own, before touching anything.

    A `--dest` typo must not delete somebody's notes. Two questions, both needed:
    strange names mean it is not ours, and the absence of `MARKER` in a directory
    that already holds notes means the same thing even when every name matches —
    `ideas/`, `theses/`, `sources/` and a README are an ordinary vault layout.

    `building` is this run's own build directory, the one name that is not foreign.
    """
    if dest.exists() and not dest.is_dir():
        raise ops.Conflict(f"{dest} is a file, not a vault directory")
    if not dest.is_dir():
        return
    # Somebody else's build directory: an export running right now, whose notes our
    # `_clear` would delete, or the remains of one that was killed. Neither is ours
    # to walk over, and the operator cannot tell the two apart from here either.
    others = sorted(p.name for p in dest.glob(f"{BUILD_PREFIX}*{BUILD_SUFFIX}")
                    if p.name != building)
    if others:
        raise ops.Conflict(
            f"{dest} holds {others[0]}, the build directory of another export — "
            "wait for that export to finish, or delete the directory if none is running")
    # Dotfiles are not ours to judge: `.obsidian/` holds the graph settings and the
    # pane layout, and wiping those on every export resets the view just tuned.
    here = [p.name for p in dest.iterdir() if not p.name.startswith(".")]
    stray = sorted(name for name in here if name not in OWNED)
    if stray:
        raise ops.Conflict(
            f"{dest} holds files this export does not own ({stray[:5]}) — "
            "refusing to delete it, point --dest at a fresh directory")
    if here and not (dest / MARKER).exists():
        raise ops.Conflict(
            f"{dest} holds notes but no {MARKER}, so this export did not write them — "
            f"point --dest elsewhere, or create {MARKER} to adopt the directory")


def _clear(dest: Path, building: str | None = None) -> None:
    """Drop what a previous export wrote, keep everything else, refuse strangers."""
    _check_dest(dest, building)
    for name in OWNED:
        path = dest / name
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


def export(dest: Path = DATA / "vault") -> dict:
    """Write the whole lake to `dest` as markdown. Returns the numbers it wrote.

    Read-only towards the lake, and loud about anything that would produce a
    partial graph: a leaf pointing at an idea or a source that is not there, and a
    file count that does not match `/stats` (§11.3.5). A half-exported vault does
    not look broken in Obsidian — it looks like a smaller lake.

    Ideas with no leaves are counted and marked, not refused: `orphans` in the
    result, `orphan: true` in the note (see `_idea_note`).
    """
    dest = Path(dest)
    counts = graph_client.counts()
    expected = counts["ideas"] + counts["theses"] + counts["sources"]
    if not expected:
        raise ops.Conflict("the lake holds no nodes — an empty vault reports a "
                           "successful export of nothing (is data/lake.db the right one?)")
    sources, ideas, theses = _read(counts)

    if (len(sources), len(ideas), len(theses)) != \
            (counts["sources"], counts["ideas"], counts["theses"]):
        raise ops.Broken(
            f"read {len(sources)} sources / {len(ideas)} ideas / {len(theses)} theses, "
            f"store reports {counts} — the paged reads did not see the whole lake")

    idea_ids = {idea["id"] for idea in ideas}
    source_ids = {source["id"] for source in sources}
    for leaf in theses:
        if leaf["idea_id"] not in idea_ids:
            raise ops.Broken(f"thesis {leaf['id']} points at missing idea {leaf['idea_id']!r}")
        if leaf["source_id"] not in source_ids:
            raise ops.Broken(f"thesis {leaf['id']} points at missing source {leaf['source_id']!r}")
    # Both directions come from the same store but by different queries; if they
    # ever disagree, one side of every link would be missing and the graph would
    # show stars without back-edges (§11.3.1).
    listed = {leaf["id"] for leaf in theses}
    linked = {leaf["id"] for idea in ideas for leaf in idea["theses"]}
    if listed != linked:
        raise ops.Broken(f"leaves of ideas and listed theses differ: {sorted(listed ^ linked)[:5]}")

    # Every id validated and every name resolved while the previous export is still
    # on disk: an id refused halfway through the writing would refuse it after the
    # deletion, which is how a typo turns into an empty vault.
    names = _names([(idea["text"], idea["id"]) for idea in ideas]
                   + [(leaf["text"], leaf["id"]) for leaf in theses]
                   + [(source["title"], source["id"]) for source in sources])

    by_source: dict[str, list[dict]] = {}
    for leaf in theses:
        by_source.setdefault(leaf["source_id"], []).append(leaf)
    orphans = sum(1 for idea in ideas if not idea["theses"])

    _check_dest(dest)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / MARKER).write_text(
        "Выгрузка `python3 -m lake.vault`. Каталог пересоздаётся целиком (§11.3.4);\n"
        "без этого файла экспорт откажется удалять содержимое.\n", encoding="utf-8")
    staging = Path(tempfile.mkdtemp(prefix=BUILD_PREFIX, suffix=BUILD_SUFFIX, dir=dest))
    try:
        for sub in SUBDIRS:
            (staging / sub).mkdir()

        for idea in ideas:
            (staging / "ideas" / f"{names[idea['id']]}.md").write_text(
                _idea_note(idea, names), encoding="utf-8")
        for leaf in theses:
            (staging / "theses" / f"{names[leaf['id']]}.md").write_text(
                _thesis_note(leaf, names), encoding="utf-8")
        for source in sources:
            (staging / "sources" / f"{names[source['id']]}.md").write_text(
                _source_note(source, by_source.get(source["id"], []), names), encoding="utf-8")

        # Counted on disk, not in memory: two nodes colliding on one file name lose a
        # note without raising anything, and that is the shape of a silently smaller
        # graph the check exists to catch (§11.3.5).
        notes = sum(1 for sub in SUBDIRS for _ in (staging / sub).glob("*.md"))
        if notes != expected:
            raise ops.Broken(f"{notes} notes written, store reports {expected} nodes")

        files = notes + 1
        (staging / "README.md").write_text(_readme(counts, files, orphans), encoding="utf-8")
        # ponytail: the window between the delete and the moves is the one thing left
        # non-atomic. Closing it means replacing `dest` itself, which cannot be done
        # while `.obsidian/` has to survive in place.
        _clear(dest, staging.name)
        for name in (*SUBDIRS, "README.md"):
            try:
                os.replace(staging / name, dest / name)
            except OSError as exc:
                # Named, not raw: an `OSError` out of here is not a `lake.ops` refusal
                # and reaches the caller as a bare 500 with no idea what to do next.
                raise ops.Broken(
                    f"moving {name!r} into {dest} failed ({exc}) — the vault is now "
                    "part old and part new; the lake itself was not touched, re-run "
                    "the export to repair the folder") from exc
    finally:
        # Never a reason to fail: by the time this runs the four moves are already on
        # disk, and a build directory left behind refuses every later export (§11.3.4).
        shutil.rmtree(staging, ignore_errors=True)
    return {"ideas": len(ideas), "theses": len(theses), "sources": len(sources),
            "orphans": orphans, "files": files, "dest": str(dest)}


# ------------------------------------------------------------------- self-check

def _diff(before: dict, after: dict) -> list[str]:
    """Keys added, removed or changed. Its own function so the self-check can prove
    the leak guard still detects anything at all — an `assert not leaked` over a
    comparison that silently stopped comparing is the quietest green there is."""
    return sorted(set(before) ^ set(after)) + \
        sorted(k for k in before.keys() & after.keys() if before[k] != after[k])


def _demo_body(tmp: Path) -> None:
    from .models import EMBED_DIM, Idea, Source, Thesis, new_idea_id, new_thesis_id
    from .models import source_id as make_source_id, text_hash

    global PAGE

    # Two sources on purpose: with one, "every source claims every thesis of the
    # lake" is a mutation that resolves every link and passes §11.6 while the
    # provenance lies. The dangerous characters sit in the fields where they break
    # something: '|' in a table cell, '\n' in a list item, ')' and '>' in a link
    # destination, '[[[1]]]' in body text, DEL in a frontmatter value, and '[[' in
    # the title of a source — a clean title makes an unescaped H1 indistinguishable.
    url_a = "https://arxiv.org/abs/2405.00001?f=(4)&t=<x>"
    sid_a = make_source_id(url_a, "v1")
    title_a = "Attention Is All You Need [[v2]]"
    graph_client.write_source(Source(id=sid_a, url=url_a, title=title_a, type="paper",
                                     version="v1", retrieved_at="2026-07-28T10:00:00Z"))
    url_b = "https://runs.local/run-17"
    sid_b = make_source_id(url_b, "v1")
    graph_client.write_source(Source(id=sid_b, url=url_b, title="evo run 17", type="run",
                                     version="v1", retrieved_at="2026-07-28T11:00:00Z",
                                     run_success=False,
                                     run_meta={"fitness_delta": 0.1, "note": "[[x]]"}))

    def make_leaf(idea_id: str, source: str, text: str, effect: str = "+3.1 pp",
                  locator: str = "Section 3.3") -> Thesis:
        return Thesis(id=new_thesis_id(), source_id=source, idea_id=idea_id, text=text,
                      context="cifar-10, resnet-18\n\n---\n# heading", effect=effect,
                      locator=locator, text_hash=text_hash(text), vector=[0.2] * EMBED_DIM,
                      created_at="2026-07-28T10:00:00Z")

    def make_idea(text: str, effect_claimed: str) -> Idea:
        return Idea(id=new_idea_id(), text=text,
                    applicability_conditions="a frozen encoder\n\n---\n# heading",
                    limitations="lim", failure_modes=["weak encoder -> semantics lost"],
                    effect_claimed=effect_claimed, effect_observed="", vector=[0.1] * EMBED_DIM,
                    created_at="2026-07-28T10:00:00Z", updated_at="2026-07-28T10:00:00Z")

    first = make_idea("freeze the encoder and train the head only", "+3 | pp")
    marked = make_leaf(first.id, sid_a, "the arbiter reads [[[1]]] before linking",
                       effect="+3.1\x7f pp: [[Table 4]]", locator="Section 3.3\nстрока два")
    plain = make_leaf(first.id, sid_a, "freezing halves the trainable params")
    graph_client.create_idea_with_theses(first, sid_a, [marked, plain])
    second = make_idea("evaluate candidates with a cheap proxy first", "3x cheaper")
    proxy = make_leaf(second.id, sid_a, "a distilled surrogate scores every candidate")
    graph_client.create_idea_with_theses(second, sid_a, [proxy])
    from_run = make_leaf(second.id, sid_b, "the run reproduced the surrogate gain")
    graph_client.create_idea_with_theses(None, sid_b, [from_run])
    # An idea with no leaves breaks `IDEA ||--|{ THESIS` and still has to export:
    # a broken lake is exactly when somebody opens the graph (F4).
    orphan = make_idea("an idea whose leaves are gone", "")
    graph_client.create_idea(orphan)

    vault = tmp / "vault"
    result = export(vault)
    assert result == {"ideas": 3, "theses": 4, "sources": 2, "orphans": 1, "files": 10,
                      "dest": str(vault)}, result

    # §11.3.5: files on disk == nodes in the store, README aside.
    notes = sorted(p for sub in SUBDIRS for p in (vault / sub).glob("*.md"))
    assert len(notes) == 9, notes
    readme = (vault / "README.md").read_text(encoding="utf-8")
    # Every row against the returned numbers, not one row: README is the only place
    # an operator compares the export with /stats, so a row that lies there is a
    # smaller lake nobody has any way of noticing.
    for row, key in (("Идеи", "ideas"), ("Тезисы", "theses"),
                     ("Источники", "sources"), ("Файлов", "files")):
        assert f"| {row} | {result[key]} |" in readme, (row, readme)
    assert "**Идей без листьев: 1.**" in readme, readme
    print("ok: 3 идеи + 4 тезиса + 2 источника -> 9 заметок + README")

    # The name is the label Obsidian draws, and the only one it draws.
    names = {path.stem for path in notes}
    assert f"freeze-the-encoder-and-train-the-head-{first.id}" in names, sorted(names)
    assert f"attention-is-all-you-need-v2-{sid_a}" in names, sorted(names)
    assert _name("КИРИЛЛИЦА", "idea_x") == "idea_x", "an unslugifiable text must not lose the id"
    assert _name("x" * 60, "idea_x").startswith("x" * 40 + "-idea_x")
    print("ok: имя заметки читаемое, id на месте")

    # §11.6: every [[…]] resolves to a file. An unescaped bracket in a thesis would
    # show up here as a link to a note nobody wrote — provided the regex can see a
    # target with a bracket in it, which is exactly the shape such a link has.
    assert WIKILINK_RE.findall("[[a]b]] [[c]]") == ["a]b", "c"], "the walk is blind to ghosts"
    links = 0
    for path in notes:
        for target in WIKILINK_RE.findall(path.read_text(encoding="utf-8")):
            assert target in names, f"{path.name}: [[{target}]] points at no file"
            links += 1
    assert links == 16, links
    ghost = (vault / "theses" / f"the-arbiter-reads-1-before-linking-{marked.id}.md")
    text = ghost.read_text(encoding="utf-8")
    assert r"\[\[\[1\]\]\]" in text, text
    assert r"\\[\\[Table 4\\]\\]" in text, "frontmatter effect is not escaped either"
    assert "\x7f" not in text, "a control character reached the frontmatter"
    print(f"ok: {links} wikilinks резолвятся, скобки внутри тезиса экранированы")

    # One snapshot, whole file, all three note kinds: it is the only assertion that
    # sees a missing `type:`, a truncated trust_score, an unrendered section, a
    # `retrieved_at` that stopped being written or a leaf counter that does not
    # match the list under it.
    expected_idea = f"""\
---
type: "idea"
id: "{first.id}"
leaves: 2
sources: 1
trust_score: 0.0
rederived_at_leaf_count: 0
---

# freeze the encoder and train the head only

**Условия применимости.** a frozen encoder --- # heading
**Ограничения.** lim
**Режимы отказа.**
- weak encoder -> semantics lost

| | |
|---|---|
| Заявленный эффект | +3 \\| pp |
| Наблюдаемый эффект |  |

## Листья (2)
- [[the-arbiter-reads-1-before-linking-{marked.id}]] — +3.1\x7f pp: \\[\\[Table 4\\]\\] — Section 3.3 строка два
- [[freezing-halves-the-trainable-params-{plain.id}]] — +3.1 pp — Section 3.3
"""
    expected_thesis = f"""\
---
type: "thesis"
id: "{marked.id}"
idea: "{first.id}"
effect: "+3.1 pp: \\\\[\\\\[Table 4\\\\]\\\\]"
locator: "Section 3.3\\nстрока два"
---

> the arbiter reads \\[\\[\\[1\\]\\]\\] before linking

**Контекст.** cifar-10, resnet-18 --- # heading
**Идея:** [[freeze-the-encoder-and-train-the-head-{first.id}]]
**Источник:** [[attention-is-all-you-need-v2-{sid_a}]] · [ссылка](<https://arxiv.org/abs/2405.00001?f=(4)&t=%3Cx%3E>)
"""
    expected_source = f"""\
---
type: "source"
id: "{sid_a}"
kind: "paper"
version: "v1"
retrieved_at: "2026-07-28T10:00:00Z"
---

# Attention Is All You Need \\[\\[v2\\]\\]

[https://arxiv.org/abs/2405.00001?f=(4)&t=<x>](<https://arxiv.org/abs/2405.00001?f=(4)&t=%3Cx%3E>)

## Тезисы (3)
- [[the-arbiter-reads-1-before-linking-{marked.id}]] — +3.1\x7f pp: \\[\\[Table 4\\]\\] — Section 3.3 строка два
- [[freezing-halves-the-trainable-params-{plain.id}]] — +3.1 pp — Section 3.3
- [[a-distilled-surrogate-scores-every-{proxy.id}]] — +3.1 pp — Section 3.3
"""
    got_idea = (vault / "ideas" /
                f"freeze-the-encoder-and-train-the-head-{first.id}.md").read_text(encoding="utf-8")
    paper_note = (vault / "sources" /
                  f"attention-is-all-you-need-v2-{sid_a}.md").read_text(encoding="utf-8")
    assert got_idea == expected_idea, got_idea
    assert text == expected_thesis, text
    assert paper_note == expected_source, paper_note
    print("ok: заметки идеи, тезиса и источника совпали с эталоном целиком")

    # F4: the broken idea is exported, and says so in both places an operator looks —
    # the frontmatter filter and the note itself.
    lonely = (vault / "ideas" / f"{_name(orphan.text, orphan.id)}.md").read_text(encoding="utf-8")
    assert "orphan: true" in lonely and "**Идея без листьев**" in lonely, lonely
    assert "orphan" not in got_idea, "an idea with leaves must not be flagged"
    assert "## Листья (0)" in lonely, lonely

    # Provenance: a source lists its own leaves and nobody else's. "Every source
    # claims every thesis of the lake" resolves every link and satisfies §11.6 —
    # only a second source makes the difference visible.
    every_leaf = (marked, plain, proxy, from_run)
    for sid, title, own in ((sid_a, title_a, (marked, plain, proxy)),
                            (sid_b, "evo run 17", (from_run,))):
        note = (vault / "sources" / f"{_name(title, sid)}.md").read_text(encoding="utf-8")
        assert f"## Тезисы ({len(own)})" in note, note
        for leaf in every_leaf:
            listed_here = f"[[{_name(leaf.text, leaf.id)}]]" in note
            assert listed_here == (leaf in own), f"source {sid} and thesis {leaf.id}"
    # A run carries both run fields, a paper carries neither — and a wikilink inside
    # `run_meta` reaches the graph as a ghost node just like one in the body.
    run_note = (vault / "sources" / f"{_name('evo run 17', sid_b)}.md").read_text(encoding="utf-8")
    assert 'kind: "run"' in run_note and "run_success: false" in run_note, run_note
    assert '"fitness_delta": 0.1' in run_note and "[[x]]" not in run_note, run_note
    print("ok: источник перечисляет свои тезисы и только свои, поля прогона на месте")

    # §11.3.4: a re-export rebuilds, so the note of a node that is gone goes too.
    stale = vault / "ideas" / "idea-idea_deadbeef1234.md"
    stale.write_text("a node that no longer exists\n", encoding="utf-8")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "graph.json").write_text("{}", encoding="utf-8")
    again = export(vault)
    assert not stale.exists(), "stale note survived a re-export"
    assert again == result, (again, result)
    assert (vault / ".obsidian" / "graph.json").exists(), "export wiped the Obsidian settings"
    # Adopted directory, own marker — and one file that is not ours in it. Still a
    # refusal: `_clear` deletes by name, so a stranger's note is not proof of what
    # the next name will be.
    intruder = vault / "somebody-elses.md"
    intruder.write_text("mine\n", encoding="utf-8")
    try:
        export(vault)
    except ops.Conflict as exc:
        assert "does not own" in str(exc), exc
    else:
        raise AssertionError("export ran over a file it did not write")
    intruder.unlink()
    # A build directory that is not this run's: a parallel export whose notes `_clear`
    # would delete, or the remains of a killed one. Refused by name, so the operator
    # is not left guessing which of the two it is.
    leftover = vault / f"{BUILD_PREFIX}killed{BUILD_SUFFIX}"
    leftover.mkdir()
    try:
        export(vault)
    except ops.Conflict as exc:
        assert leftover.name in str(exc), exc
    else:
        raise AssertionError("export built next to another export's build directory")
    leftover.rmdir()
    print("ok: re-export drops stale notes, keeps .obsidian/, отступает перед чужим файлом")

    # The multi-page path with a fixture that actually has more than one page: at
    # PAGE = 500 every paging bug in this module is unreachable and every mutation
    # of it is green.
    paged = tmp / "vault-paged"
    real_page, PAGE = PAGE, 2
    try:
        assert export(paged) == {**result, "dest": str(paged)}, "paged read lost rows"
    finally:
        PAGE = real_page
    assert (paged / "ideas" / f"freeze-the-encoder-and-train-the-head-{first.id}.md"
            ).read_text(encoding="utf-8") == expected_idea
    print("ok: PAGE=2 — постраничное чтение даёт ту же выгрузку")

    _demo_refusals(tmp, vault, first, orphan)
    # A refused export must not poison the directory it refused in: no marker where
    # nothing was written, no build directory of its own left to block the next run.
    broken = tmp / "vault-broken"
    assert export(broken) == {**result, "dest": str(broken)}, "a refusal poisoned --dest"
    assert export(vault) == result, "the refusals damaged the live vault"
    print("ok: после отказов экспорт в тот же каталог проходит")


def _demo_refusals(tmp: Path, vault: Path, first, orphan) -> None:
    """Every guard broken on purpose, one at a time, each by the message and the
    exception type it owes, and each checked against what it left on disk.

    A guard nobody tries to break is a guard nobody knows is there. The message is
    asserted because these guards overlap — without it, deleting one of them still
    leaves the export raising, from the next one down, and the mutation reads as
    caught. The type is asserted because it is the HTTP status (`app.py:OPS_STATUS`):
    409 tells the caller to fix the request, 503 that the service is down.
    """
    real = {name: getattr(graph_client, name)
            for name in ("counts", "list_sources", "list_theses", "list_idea_ids", "get_ideas")}
    broken = tmp / "vault-broken"

    def tree(root: Path) -> dict:
        return {str(p.relative_to(root)): p.read_bytes()
                for p in sorted(root.rglob("*")) if p.is_file()}

    intact = tree(vault)

    def refuses(what: str, says: str, kind: type, leftover=None, **patched) -> None:
        shutil.rmtree(broken, ignore_errors=True)
        for name, fake in patched.items():
            setattr(graph_client, name, fake)
        try:
            # Twice: into a `--dest` that does not exist yet, and against the live
            # export. What each one leaves behind is the half of §11.3.4 the other
            # cannot see — a directory adopted for nothing, and notes destroyed.
            for target in (broken, vault):
                try:
                    export(target)
                except (ops.Conflict, ops.Broken) as exc:
                    assert says in str(exc), f"{what}: refused with {exc!r}, expected {says!r}"
                    assert type(exc) is kind, \
                        f"{what}: refused {type(exc).__name__}, expected {kind.__name__}"
                else:
                    raise AssertionError(f"export() accepted a store that {what}")
        finally:
            for name in patched:
                setattr(graph_client, name, real[name])
        left = sorted(p.name for p in broken.rglob("*")) if broken.exists() else None
        assert left == leftover, f"{what}: a refused export left {left}, expected {leftover}"
        assert tree(vault) == intact, f"{what}: a refused export damaged the previous one"

    # Zero nodes is the shape of "the wrong data/lake.db was opened": an empty vault
    # that reports a successful export of the whole lake is the fail-open of §11.
    refuses("holds no nodes at all", "holds no nodes", ops.Conflict,
            counts=lambda: {"sources": 0, "ideas": 0, "theses": 0, "edges": 0})
    # A page that does not advance: without the ceiling in `_all` this never returns,
    # and a hanging export reads as a slow lake. Once per reader — the ceiling is
    # passed in three separate calls, and two of them had nothing standing on them.
    refuses("never advanced its thesis offset", "contradicts itself", ops.Broken,
            list_theses=lambda idea_id, source_id, limit, offset:
                real["list_theses"](idea_id, source_id, limit, 0))
    refuses("never advanced its source offset", "contradicts itself", ops.Broken,
            list_sources=lambda limit, offset: real["list_sources"](limit, 0))
    refuses("never advanced its idea offset", "contradicts itself", ops.Broken,
            list_idea_ids=lambda limit, offset: real["list_idea_ids"](limit, 0))
    # A short read that nothing downstream can see: the dropped idea has no leaves,
    # so only the count says so.
    refuses("dropped a row from a page", "did not see the whole lake", ops.Broken,
            list_idea_ids=lambda limit, offset:
                [i for i in real["list_idea_ids"](limit, offset) if i != orphan.id])
    refuses("pointed a leaf at no idea", "points at missing idea", ops.Broken,
            list_theses=lambda idea_id, source_id, limit, offset:
                [{**row, "idea_id": "idea_ghost"} if n == 0 and not offset else row
                 for n, row in enumerate(real["list_theses"](idea_id, source_id, limit, offset))])
    refuses("hid a leaf of an idea", "leaves of ideas and listed theses differ", ops.Broken,
            get_ideas=lambda ids: [{**idea, "theses": idea["theses"][1:]} if n == 0 else idea
                                   for n, idea in enumerate(real["get_ideas"](ids))])
    # Two different ids, one file name: caught by the name map, before any deletion —
    # hence `leftover=None`, the refusal happens before `dest` is even created.
    collision = _name(first.text, first.id)
    refuses("gave two ids one file name", "both want the file", ops.Broken,
            get_ideas=lambda ids: [{**idea, "id": collision, "text": "кириллица"}
                                   if idea["id"] == orphan.id else idea
                                   for idea in real["get_ideas"](ids)])
    # One id twice: the map cannot see it (one key), the note is overwritten in
    # silence, and only the count of files on disk is short. The one refusal that
    # happens with notes already written, so the previous export standing untouched
    # afterwards is what `.export.*.tmp` is for — and the marker is all it may leave.
    refuses("returned one id twice", "notes written", ops.Broken, leftover=[MARKER],
            get_ideas=lambda ids: [{**idea, "id": first.id, "text": first.text}
                                   if idea["id"] == orphan.id else idea
                                   for idea in real["get_ideas"](ids)])
    print("ok: короткое чтение, битая ссылка, спрятанный лист и коллизия имён — отказ")

    # A --dest typo must not delete notes this export did not write.
    foreign = tmp / "my-notes"
    (foreign / "sub").mkdir(parents=True)
    (foreign / "diary.md").write_text("mine\n", encoding="utf-8")
    # ...and neither must a directory that merely has the shape of a vault: the
    # names match, so only the marker separates it from ours.
    lookalike = tmp / "someones-vault"
    (lookalike / "ideas").mkdir(parents=True)
    (lookalike / "README.md").write_text("my own notes\n", encoding="utf-8")
    # A stranger can be a directory and nothing else — scanning only files here
    # would clear somebody's `notebooks/` without ever naming it.
    subdirs_only = tmp / "notebooks-only"
    (subdirs_only / "notebooks").mkdir(parents=True)
    # ...and `--dest` can be no directory at all: a refusal the caller can act on
    # (409), not the `FileExistsError` mkdir would raise from under it (500).
    a_file = tmp / "not-a-directory"
    a_file.write_text("mine\n", encoding="utf-8")
    for target in (foreign, lookalike, subdirs_only, a_file):
        try:
            export(target)
        except ops.Conflict:
            pass
        else:
            raise AssertionError(f"export cleared {target.name}, which it did not write")
        assert not (target / MARKER).exists(), "a refused export left a marker behind"
    assert (foreign / "diary.md").exists() and (lookalike / "README.md").exists()
    assert (subdirs_only / "notebooks").is_dir(), "the foreign directory was cleared"
    assert a_file.read_text(encoding="utf-8") == "mine\n", "the file at --dest was rewritten"
    # `.`, `..` and `.hidden` too: they reach the file system as a dotfile that
    # `glob("*.md")` does not count and `_clear` does not delete, and the export
    # then dies on "notes written != nodes" with no hint of why.
    for bad in ("../etc/passwd", "a/b", 'q?"', "", "..", ".", ".hidden"):
        try:
            _name("text", bad)
        except ops.Conflict:
            continue
        raise AssertionError(f"id {bad!r} accepted as a file name")
    print("ok: чужой каталог и небезопасные id — отказ, без удаления")


def demo() -> None:
    """ponytail: single-run self-check (§11.6), fixture in a temp dir, no network.

    The store and the trace log are pointed at that directory, and the real
    `data/` is fingerprinted before and after — same guard, same reason as
    `lake/api/selfcheck.py`: a check that edits the lake it is checking has
    happened in this repo once already.

    Pointing the store there means writing `stub_store._db_path` and `._conn`
    directly, around `graph_client`. Both neighbouring self-checks do the same;
    the module's "reads through graph_client only" is about the working path, not
    about the check that has to put a lake somewhere disposable.
    """
    from . import stub_store, trace

    def fingerprint() -> dict:
        if not DATA.exists():
            return {}
        return {str(p.relative_to(DATA)): f"{p.stat().st_size}:{p.stat().st_mtime_ns}"
                for p in sorted(DATA.rglob("*")) if p.is_file()}

    assert _diff({"a": "1"}, {"a": "2"}) == ["a"], "the leak guard sees no change"
    assert _diff({"a": "1"}, {}) == ["a"] and _diff({}, {"a": "1"}) == ["a"]
    assert _diff({"a": "1"}, {"a": "1"}) == []

    before = fingerprint()
    tmp = Path(tempfile.mkdtemp(prefix="lake-vault-selfcheck-"))
    if stub_store._conn is not None:      # a live handle on the real lake, closed first
        stub_store._conn.close()
    real_db, stub_store._db_path, stub_store._conn = stub_store._db_path, tmp / "lake.db", None
    # Every graph call is @trace'd and trace appends to TRACES_DIR/<run_id>.jsonl,
    # which is inside the real data/ this check promises not to touch.
    real_traces, trace.TRACES_DIR = trace.TRACES_DIR, tmp / "traces"

    leaked: list[str] = []
    try:
        # The guards of the guard, and BEFORE the fixture: a store still pointed at
        # the real lake, or a fingerprint that fingerprints nothing, would make
        # `not leaked` true for the wrong reason — and noticing that afterwards
        # means noticing it with the fixture already written into data/lake.db.
        assert stub_store._db_path.is_relative_to(tmp), stub_store._db_path
        # "There are files under data/ and the fingerprint saw none" — that is a
        # fingerprint pointed at the wrong path. An EMPTY data/ measuring `{}` is not
        # the same thing and must pass: the image declares `VOLUME /app/lake/data`, so
        # in a fresh container the directory exists and is empty, and the earlier
        # `not DATA.exists() or before` failed there — on every clean checkout too.
        assert not any(DATA.rglob("*")) or before, "the leak guard measured nothing"
        _demo_body(tmp)
    finally:
        if stub_store._conn is not None:
            stub_store._conn.close()
        stub_store._db_path, stub_store._conn = real_db, None
        trace.TRACES_DIR = real_traces
        # Compared inside the `finally`: after a failed assertion the comparison
        # would never run, and the leaking run and the debugged run are the same one.
        leaked = _diff(before, fingerprint())
        if leaked:
            print(f"LEAK: the self-check wrote to real data/: {leaked}")
        shutil.rmtree(tmp, ignore_errors=True)

    assert not leaked, f"the self-check wrote to real data/: {leaked}"
    print("vault self-check OK — real data/ untouched")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        prog="python3 -m lake.vault",
        description="Выгрузить озеро в Obsidian-vault (спека 11).")
    parser.add_argument("--dest", type=Path, default=DATA / "vault",
                        help="куда писать, по умолчанию data/vault")
    parser.add_argument("--self-check", action="store_true",
                        help="проверка §11.6 на фикстуре во временном каталоге")
    args = parser.parse_args()

    if args.self_check:
        demo()
    else:
        done = export(args.dest)
        print(f"vault: {done['ideas']} идей + {done['theses']} тезисов + "
              f"{done['sources']} источников -> {done['files']} файлов в {done['dest']}")
        if done["orphans"]:
            print(f"  INVARIANT BROKEN: {done['orphans']} идей без листьев "
                  "(`06:85`), в заметках `orphan: true`")
