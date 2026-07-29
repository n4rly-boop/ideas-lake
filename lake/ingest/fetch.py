"""One `sources.yaml` row -> (Source, sections), spec 10 §4.2.

Path order: `arxiv.org/html/{id}` -> `ar5iv.labs.arxiv.org/html/{id}` -> PDF via
PyMuPDF (46 of 84 sources have no HTML, `09:308`). Sections come from
`09-raw/fetch_arxiv_sections.py` with its two regexes replaced: see `_blocks`,
ar5iv writes the same LaTeXML markup with the attributes in the other order, and
the class-first pattern matched nothing there — the whole no-HTML half of the
corpus would have fallen through to a PDF path that raises.

`url` and `version` come from the arXiv API, never from `sources.yaml` — the
corpus file has neither (§0 p.9), and `Source.id = sha1(url + version)` (§4.8).

The abstract is returned as the FIRST section, `kind="abstract"`, so that
`(Source, list[Section])` carries everything `parse_section(section, abstract,
limitations)` needs (§4.3); `find_abstract` / `find_limitations` read them back.
Callers that parse sections must skip `kind == "abstract"`: it is reference
material for every call, not a section to extract theses from.

Nothing here returns an empty result: a source that quietly yields no sections
drops out of the corpus without an error anywhere.
"""
import hashlib
import http.client
import importlib.util
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET   # no DTD/entity expansion in ElementTree; defusedxml is not installed
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser

from ..models import RAW_DIR, Section, Source, source_id

ARXIV_PAUSE_S = 3.0      # §8: ToU is 1 req / 3 s, 429 is real when pulling the corpus (09:50)
HTTP_TIMEOUT = 60.0      # a hung socket raises nothing on its own (§0.1.15)
ATTEMPTS = 3             # §4.2: at most 3 tries on 429/5xx, then raise
CHUNK_TOKENS = 1500      # §4.2 / 08:122, PDF fallback when there are no headings
CHUNK_OVERLAP = 100

NS = {"a": "http://www.w3.org/2005/Atom"}
# New-style arXiv ids only: `2406.04824`, optionally `v2`. Old-style ones
# (`hep-th/9901001`) are refused at the door BY DESIGN — `fetch_metadata` keeps only
# the last path segment of the API's id (`_VERSION` below, `9901001v3`), and all three
# paths of `fetch_sections` answer 404 for that, so accepting them would buy a job that
# dies after three network round trips with a message about the missing PDF library.
# Narrow on purpose in the other direction too: the id becomes a cache key and a
# staging file name, and `\d{4}\.\d{4,5}(v\d+)?` is safe as both with nothing to strip.
_ARXIV_ID = r"\d{4}\.\d{4,5}(?:v\d+)?"
_ARXIV_URL = re.compile(
    rf"^(?:https?://)?(?:www\.|export\.)?arxiv\.org/(?:abs|pdf|html)/({_ARXIV_ID})"
    r"(?:\.pdf)?/?$", re.I)
_OLD_STYLE = re.compile(r"^(?:https?://)?(?:www\.|export\.)?arxiv\.org/(?:abs|pdf|html)/"
                        r"[a-z-]+(?:\.[a-z-]+)?/\d{7}", re.I)
# Identify the client: arXiv's ToU asks for it, and readthedocs answers 403 to the
# default urllib UA — a 403 on three of the six doc rows and nothing to show for it.
USER_AGENT = "ideas-lake/0.1 (AIRI Summer 2026 corpus fetch; +https://arxiv.org/help/api)"
_HEADING = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.+?)[ \t]*$", re.M)
_VERSION = re.compile(r"v(\d+)$")


class FetchError(RuntimeError):
    """This row cannot be turned into a Source. The caller skips it, loudly."""


# ------------------------------------------------------------------ http + pause

_pause_lock = threading.Lock()
_last_request = 0.0      # monotonic stamp of the last outbound request


def _pause() -> None:
    """Hold 1 request / 3 s (§8).

    Under a lock because phase 1 runs 8 threads (§8): an unsynchronized stamp
    lets all eight fire at once, which is exactly how 429 was caught. Cache hits
    never reach this function, so a re-run of the corpus pays no pause at all.
    """
    global _last_request
    with _pause_lock:
        wait = ARXIV_PAUSE_S - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()


_ssl_ctx: ssl.SSLContext | None = None


def _context() -> ssl.SSLContext:
    """TLS with verification on. This machine's Python points at an OpenSSL store
    that does not exist (`create_default_context()` loads 0 CA certs), so fall back
    to the certifi bundle that already ships with the installed requests stack.
    Turning verification off would be the fail-open version of the same fix."""
    global _ssl_ctx
    if _ssl_ctx is None:
        ctx = ssl.create_default_context()
        if not ctx.get_ca_certs():
            import certifi                   # already installed, not a new dependency
            ctx.load_verify_locations(certifi.where())
        _ssl_ctx = ctx
    return _ssl_ctx


def _get(url: str) -> bytes:
    """GET with the pause and up to `ATTEMPTS` tries on 429/5xx and network errors."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last: Exception | None = None
    for _ in range(ATTEMPTS):
        _pause()
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT, context=_context()) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code != 429 and exc.code < 500:
                raise                       # 404 drives the path fallback; retrying repeats it
            last = exc
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException) as exc:
            last = exc
    raise FetchError(f"{url}: {ATTEMPTS} attempts failed: {type(last).__name__}: {last}")


# ----------------------------------------------------------------- text from html

class _Text(HTMLParser):
    """Tag stripper (09-raw/fetch_arxiv_sections.py:10-23) plus what the doc path needs:
    <script>/<style> bodies are handle_data to HTMLParser, and with `headings=True`
    <h1>-<h6> become markdown so `_split` can cut on them."""

    _SKIP = {"script", "style"}

    def __init__(self, headings: bool = False):
        super().__init__()
        self.headings = headings
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skipping += 1
        elif self.headings and re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1
        elif self.headings and re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skipping:
            self.parts.append(data)

    def text(self) -> str:
        joined = "".join(self.parts)
        if not self.headings:
            return unescape(re.sub(r"\s+", " ", joined)).strip()
        # line structure survives, everything else collapses: _split needs the newlines
        return re.sub(r"\n{3,}", "\n\n", re.sub(r"[^\S\n]+", " ", unescape(joined))).strip()


def strip_tags(html: str, headings: bool = False) -> str:
    parser = _Text(headings)
    parser.feed(html)
    return parser.text()


def _split(text: str) -> list[Section]:
    """Markdown headings; none -> ~1500-token chunks with 100 overlap (§4.2).

    ponytail: whitespace tokens stand in for model tokens (~0.75 words/token, so
    chunks run short, never long). Swap in a tokenizer only if parse starts
    truncating.
    """
    marks = list(_HEADING.finditer(text))
    if marks:
        out = []
        for i, mark in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            body = text[mark.end():end].strip()
            if body:
                out.append(Section(id=f"S{len(out) + 1}", kind="section",
                                   title=mark.group(2).strip(), text=body))
        return out

    tokens = text.split()
    out = []
    for start in range(0, len(tokens), CHUNK_TOKENS - CHUNK_OVERLAP):
        out.append(Section(id=f"C{len(out) + 1}", kind="chunk",
                           title=f"chunk {len(out) + 1}",
                           text=" ".join(tokens[start:start + CHUNK_TOKENS])))
        if start + CHUNK_TOKENS >= len(tokens):
            break                            # the window already covers the tail
    return out


# ----------------------------------------------------------------------- arxiv

def fetch_metadata(arxiv_id: str) -> dict:
    """arXiv API -> versioned abs url, version, title, abstract (09-raw:26-37)."""
    root = ET.fromstring(_get(f"https://export.arxiv.org/api/query?id_list={arxiv_id}"))
    entry = root.find("a:entry", NS)
    if entry is None:
        raise FetchError(f"{arxiv_id}: arXiv API returned no entry")

    def field(name: str) -> str:
        node = entry.find(f"a:{name}", NS)
        if node is None or not (node.text or "").strip():
            raise FetchError(f"{arxiv_id}: arXiv API entry has no <{name}>")
        return node.text.strip()

    url = field("id")                        # versioned abs url, verbatim: source_id hashes it
    versioned_id = url.rsplit("/", 1)[-1]
    match = _VERSION.search(versioned_id)
    if not match:
        raise FetchError(f"{arxiv_id}: arXiv id {url!r} carries no version")
    return {"arxiv_id": arxiv_id,
            "versioned_id": versioned_id,
            "url": url,
            "version": "v" + match.group(1),     # §1.1: arXiv version is "v2"
            "title": re.sub(r"\s+", " ", field("title")),
            "updated": field("updated"),
            "summary": re.sub(r"\s+", " ", field("summary"))}


def arxiv_id_from_url(url: str) -> str:
    """`https://arxiv.org/abs/2406.04824v2` -> `2406.04824v2`. Raises on anything else.

    `/abs/`, `/pdf/` and `/html/` are the same article, so all three are accepted, and
    the version stays on the id: it is the version the arXiv API is asked for, and
    `Source.id = sha1(url + version)` (§4.8) has to follow the paper the link points at.
    Lower-cased, so that `...v2` and `...V2` are one cache key and one staging file
    rather than two fetches of one article.

    Refusing here is the point: this is the door in front of the fetch and minutes of
    LLM spend. An unknown url would otherwise be found out by `fetch_metadata` as an
    empty API answer, one job later, and an old-style id not even then — it dies three
    round trips deep with a message about a missing PDF library (see `_ARXIV_ID`).
    """
    url = url.strip()
    match = _ARXIV_URL.match(url)
    if not match:
        old = _OLD_STYLE.match(url)
        raise FetchError(
            f"{url!r} is not an arXiv article url this can fetch: expected "
            "https://arxiv.org/abs/2406.04824 (also /pdf/ or /html/, with or without a "
            "version)" + (". Old-style ids (hep-th/9901001) are not supported: the "
                          "fetch paths answer 404 for them" if old else ""))
    return match.group(1).lower()


def _blocks(html: str, tag: str, marker: re.Pattern):
    """(attrs, body) for every <tag> whose opening tag matches `marker`, the body cut
    at the *matching* close tag.

    Replaces the two regexes of 09-raw:50-57, both of which fail silently on the
    second fetch path: ar5iv writes `id` before `class` (so a class-first pattern
    matches nothing on 44 of 84 sources), and `(?=<section|\\Z)` ends a section at
    its first nested subsection instead of at its own close.
    """
    tags = list(re.finditer(rf"</?{tag}\b[^>]*>", html, re.I))
    for i, opening in enumerate(tags):
        if opening.group(0).startswith("</") or not marker.search(opening.group(0)):
            continue
        depth = 0
        for closing in tags[i:]:
            depth += -1 if closing.group(0).startswith("</") else 1
            if depth == 0:
                yield opening.group(0), html[opening.end():closing.start()]
                break


_ABSTRACT_CLASS = re.compile(r'class="[^"]*\bltx_abstract\b', re.I)
_SECTION_CLASS = re.compile(r'class="[^"]*\bltx_(section|bibliography|appendix)\b', re.I)
_ID_ATTR = re.compile(r'id="([^"]*)"', re.I)


def _parse_ltx(html: str) -> dict:
    """LaTeXML markup, identical on arxiv.org/html and ar5iv once attribute order
    stops mattering. Subsections carry class ltx_subsection and are skipped by the
    class filter, so they stay inside their parent's text."""
    out = {"abstract": "", "sections": []}
    for _, body in _blocks(html, "div", _ABSTRACT_CLASS):
        out["abstract"] = strip_tags(re.sub(r"<h6[^>]*>.*?</h6>", "", body, flags=re.S))
        break
    for attrs, body in _blocks(html, "section", _SECTION_CLASS):
        kind = _SECTION_CLASS.search(attrs).group(1)
        ident = _ID_ATTR.search(attrs)
        sec_id = ident.group(1) if ident else f"S{len(out['sections']) + 1}"
        head = re.search(r"<h[1-6][^>]*>(.*?)</h[1-6]>", body, re.S)
        out["sections"].append(Section(id=sec_id, kind=kind,
                                       title=strip_tags(head.group(1)) if head else sec_id,
                                       text=strip_tags(body)))
    return out


def fetch_sections(versioned_id: str) -> dict:
    """The three paths of §4.2, in order. Raises when all three fail."""
    reasons = []
    for name, url in (("arxiv_html", f"https://arxiv.org/html/{versioned_id}"),
                      ("ar5iv", f"https://ar5iv.labs.arxiv.org/html/{versioned_id}")):
        try:
            html = _get(url).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            reasons.append(f"{name}: HTTP {exc.code}")
            continue
        parsed = _parse_ltx(html)
        if parsed["sections"]:
            parsed["path"] = name
            return parsed
        reasons.append(f"{name}: no ltx_section markup")

    try:
        sections = _pdf_sections(versioned_id)
    except FetchError as exc:
        raise FetchError(f"{versioned_id}: no sections ({'; '.join(reasons)}; pdf: {exc})") from exc
    if not sections:
        raise FetchError(f"{versioned_id}: PDF yielded no text ({'; '.join(reasons)})")
    return {"abstract": "", "sections": sections, "path": "pdf"}


def _pdf_sections(versioned_id: str) -> list[Section]:
    """PDF path. PyMuPDF is AGPL-3.0 (§4.2) and is not installed here, so it is
    imported at the point of use: the 38 HTML sources must work without it."""
    if importlib.util.find_spec("fitz") is None:
        raise FetchError("PDF path needs PyMuPDF (AGPL-3.0), not installed")
    import fitz

    with fitz.open(stream=_get(f"https://arxiv.org/pdf/{versioned_id}"), filetype="pdf") as doc:
        return _split("\n".join(page.get_text() for page in doc))


# ------------------------------------------------------------------- entry point

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fetch_arxiv(arxiv_id: str) -> dict:
    meta = fetch_metadata(arxiv_id)
    got = fetch_sections(meta["versioned_id"])
    return {"title": meta["title"],
            "url": meta["url"],
            "version": meta["version"],
            "retrieved_at": _now(),
            # the PDF path has no ltx_abstract; the API summary is the same abstract
            "abstract": got["abstract"] or meta["summary"],
            "path": got["path"],
            "sections": [s.model_dump() for s in got["sections"]]}


def _doc_body(html: str) -> str:
    """Cut to <article>, else <main>: site chrome (nav menu, search box, footer)
    otherwise turns into sections indistinguishable from content — the GitHub
    README page gives 90 sections, 44 of them menu items."""
    for tag in ("article", "main"):          # article first: mkdocs nests it inside main
        found = re.search(rf"<{tag}\b[^>]*>(.*)</{tag}>", html, re.S | re.I)
        if found:
            return found.group(1)
    return html


def _fetch_doc(url: str, entry: dict) -> dict:
    try:
        html = _get(url).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:    # wrapped: the caller skips FetchError rows
        raise FetchError(f"{url}: HTTP {exc.code}") from exc
    text = strip_tags(_doc_body(html), headings=True)
    now = _now()
    return {"title": entry.get("title") or url,
            "url": url,
            "version": now[:10],             # §1.1: a doc has no version, the fetch date is it
            "retrieved_at": now,
            # a doc has no abstract; its lead-in is what parse gets as reference (§4.3)
            "abstract": text[:1200],
            "path": "doc",
            "sections": [s.model_dump() for s in _split(text)]}


def fetch_source(entry: dict) -> tuple[Source, list[Section]]:
    """One row of `lake/sources.yaml` -> (Source, [abstract section, *sections]).

    Raw text is cached in `data/raw/{arxiv_id}.json` with its metadata: without
    it a repeat run burns 4 extra minutes on pauses alone (§4.2). `retrieved_at`
    is stored with the cache — it is the moment of the fetch, so a cached row
    keeps reporting the same instant, and `Source.id` never moves.
    """
    if entry.get("skip"):
        raise FetchError(f"{entry.get('title', '?')}: sources.yaml marks it skip: {entry['skip']}")
    stype = entry.get("type")
    if stype not in ("paper", "doc", "run"):
        raise FetchError(f"{entry.get('title', '?')}: Source.type is {stype!r}, not paper|doc|run")

    arxiv_id, url = entry.get("arxiv_id"), entry.get("url")
    if not arxiv_id and not url:
        raise FetchError(f"{entry.get('title', '?')}: neither arxiv_id nor url, nothing to fetch")

    key = arxiv_id or "doc_" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    path = RAW_DIR / f"{key}.json"
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
    else:
        raw = _fetch_arxiv(arxiv_id) if arxiv_id else _fetch_doc(url, entry)
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        # Temp file then replace, like the parse cache: a crash or a kill halfway
        # through the write would otherwise leave truncated JSON at the cache key,
        # and every later run would read it back as a hard parse error.
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    sections = [Section(**s) for s in raw["sections"] if s["text"].strip()]
    if not sections:
        raise FetchError(f"{key}: no sections after {raw['path']} path")
    if not raw["abstract"].strip():
        raise FetchError(f"{key}: no abstract after {raw['path']} path")

    source = Source(id=source_id(raw["url"], raw["version"]), url=raw["url"],
                    title=raw["title"], type=stype, version=raw["version"],
                    retrieved_at=raw["retrieved_at"])
    return source, [Section(id="abstract", kind="abstract", title="Abstract",
                            text=raw["abstract"]), *sections]


def find_abstract(sections: list[Section]) -> str:
    """The abstract `parse_section` takes as reference (§4.3)."""
    for section in sections:
        if section.kind == "abstract":
            return section.text
    raise FetchError("no abstract section: these sections did not come from fetch_source")


def find_limitations(sections: list[Section]) -> str:
    """Limitations text for `parse_section` (§4.3); "" when the paper has none.

    "" is not a fail-open: plenty of papers genuinely have no such section, and
    `parse_section` takes `limitations` as reference text, not as a precondition.
    Matches "Limitations", "Discussion and Limitations", "Limitations and Future Work".
    """
    for section in sections:
        if "limitation" in section.title.lower():
            return section.text
    return ""


if __name__ == "__main__":
    # ---- offline: splitting, the PDF guard, lookups, refused rows
    heads = _split("# Intro\nalpha beta\n\n## Method\ngamma\n### Empty\n\n## Limits\ndelta")
    assert [s.title for s in heads] == ["Intro", "Method", "Limits"], heads
    assert heads[0].text == "alpha beta" and heads[0].kind == "section"

    words = " ".join(f"w{i}" for i in range(3500))
    chunks = _split(words)
    assert [s.kind for s in chunks] == ["chunk"] * 3, chunks
    assert len(chunks[0].text.split()) == CHUNK_TOKENS
    assert chunks[0].text.split()[-CHUNK_OVERLAP:] == chunks[1].text.split()[:CHUNK_OVERLAP]
    assert chunks[-1].text.split()[-1] == "w3499"

    assert _doc_body("<nav>menu</nav><main>x<article>body</article>y</main>") == "body"
    assert _doc_body("<nav>menu</nav><main>only main</main>") == "only main"
    assert _doc_body("<p>plain page</p>") == "<p>plain page</p>"
    assert strip_tags("<p>a<script>var x=1</script>b</p>") == "ab"
    assert strip_tags("<h2>T &amp; U</h2><p>body</p>", headings=True) == "## T & U\nbody"

    # both attribute orders (ar5iv writes id first, arxiv.org/html writes class first)
    # and a nested subsection that must not end its parent
    ltx = _parse_ltx(
        '<div class="ltx_page">\n<div class="ltx_abstract">\n<h6>Abstract</h6>\n<p>the abstract</p>\n</div>\n</div>\n'
        '<section id="S1" class="ltx_section" lang="en">\n<h2>One</h2>\na\n'
        '<section id="S1.SS1" class="ltx_subsection">\n<h3>Sub</h3>\nb\n</section>\nc\n</section>\n'
        '<section class="ltx_bibliography" id="bib">\n<h2>References</h2>\nr\n</section>')
    assert ltx["abstract"] == "the abstract", ltx["abstract"]
    assert [(s.id, s.kind, s.title) for s in ltx["sections"]] == \
           [("S1", "section", "One"), ("bib", "bibliography", "References")], ltx["sections"]
    assert ltx["sections"][0].text == "One a Sub b c", ltx["sections"][0].text

    if importlib.util.find_spec("fitz") is None:
        try:
            _pdf_sections("1234.5678v1")
        except FetchError as exc:
            assert "PDF path needs PyMuPDF (AGPL-3.0), not installed" in str(exc), exc
        else:
            raise AssertionError("missing PyMuPDF must raise, not fall through to the network")

    # Keyword arguments: `Section` is a pydantic model and takes no positional ones.
    fakes = [Section(id="abstract", kind="abstract", title="Abstract", text="A"),
             Section(id="S4", kind="section", title="Discussion and Limitations", text="L")]
    assert find_limitations(fakes) == "L" and find_abstract(fakes) == "A"
    assert find_limitations(fakes[:1]) == ""

    # /fetch hands whatever the caller pasted to this one regex, and the id it returns
    # becomes a cache key and a staging file name.
    for url, want in (("https://arxiv.org/abs/2406.04824", "2406.04824"),
                      ("http://arxiv.org/abs/2406.04824v2", "2406.04824v2"),
                      ("https://www.arxiv.org/pdf/2406.04824v2.pdf", "2406.04824v2"),
                      ("https://arxiv.org/html/2406.04824v1", "2406.04824v1"),
                      ("https://export.arxiv.org/abs/2406.04824/", "2406.04824"),
                      ("https://arxiv.org/abs/2406.04824V2", "2406.04824v2"),
                      ("  https://arxiv.org/abs/1706.03762  ", "1706.03762")):
        assert arxiv_id_from_url(url) == want, (url, arxiv_id_from_url(url))
    # An id this returns is a `data/raw` cache key and a `data/fetch` file name with no
    # sanitizing anywhere after it, so the refusals are load-bearing, not tidiness.
    for bad_url in ("https://arxiv.org/abs/", "https://arxiv.org/list/cs.LG/2406",
                    "https://example.com/abs/2406.04824", "https://arxiv.org.evil.com/abs/1",
                    "https://arxiv.org/abs/../../etc/passwd", "2406.04824", "",
                    "https://arxiv.org/abs/2406.04824?x=1", "https://arxiv.org/abs/x#s1",
                    "https://openreview.net/forum?id=x"):
        try:
            arxiv_id_from_url(bad_url)
        except FetchError as exc:
            assert "not an arXiv article url" in str(exc), exc
        else:
            raise AssertionError(f"{bad_url!r} must be refused, not fetched")
    # Old-style ids pass the regex of an arXiv link and nothing after it: the refusal
    # has to say so, or the operator retries a url that cannot work.
    for old in ("arxiv.org/abs/hep-th/9901001",
                "https://arxiv.org/abs/cond-mat.stat-mech/0012345v3"):
        try:
            arxiv_id_from_url(old)
        except FetchError as exc:
            assert "Old-style ids" in str(exc), exc
        else:
            raise AssertionError(f"{old!r} cannot be fetched and must be refused at the door")

    for bad, needle in (({"type": "paper", "skip": "no arXiv id"}, "marks it skip"),
                        ({"type": "paper"}, "neither arxiv_id nor url"),
                        ({"arxiv_id": "1", "type": "html"}, "not paper|doc|run")):
        try:
            fetch_source(bad)
        except FetchError as exc:
            assert needle in str(exc), exc
        else:
            raise AssertionError(f"row {bad} must raise")

    # ---- live: one article, the one 09-raw/fetch_arxiv_sections.py was verified on
    entry = {"arxiv_id": "2406.04824", "title": "(from sources.yaml)", "type": "paper"}
    cache = RAW_DIR / "2406.04824.json"
    cache.unlink(missing_ok=True)

    started = time.monotonic()
    src, secs = fetch_source(entry)
    online_s = time.monotonic() - started
    assert cache.exists(), "raw cache was not written"
    assert find_abstract(secs), "empty abstract"
    body = [s for s in secs if s.kind != "abstract"]
    assert len(body) >= 3, body

    def _no_network(url: str) -> bytes:
        raise AssertionError(f"cache miss: {url}")

    _get, real_get = _no_network, _get           # second call must touch neither net nor pause
    started = time.monotonic()
    src2, secs2 = fetch_source(entry)
    cached_s = time.monotonic() - started
    _get = real_get
    assert cached_s < 0.5, f"cached call took {cached_s:.2f}s — the pause is being paid"
    assert (src2.id, src2.url, src2.version, src2.retrieved_at) == \
           (src.id, src.url, src.version, src.retrieved_at), (src, src2)
    assert [s.id for s in secs2] == [s.id for s in secs]

    print(f"ok: {src.id} {src.version} {src.url}")
    print(f"    online {online_s:.1f}s, cached {cached_s:.3f}s, {len(body)} sections")
    print(f"    abstract[:90]: {find_abstract(secs)[:90]!r}")
    print(f"    limitations: {len(find_limitations(secs))} chars")
    for s in body[:12]:
        print(f"    - [{s.kind}] {s.id} {s.title!r}: {len(s.text)} chars")
