"""arXiv id -> sections (stdlib only: urllib, re, html.parser, xml.etree)."""
import re, time, urllib.request
from html.parser import HTMLParser
from html import unescape
import xml.etree.ElementTree as ET

NS = {"a": "http://www.w3.org/2005/Atom"}


class _Strip(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
    def handle_data(self, d):
        self.parts.append(d)
    def text(self):
        return unescape(re.sub(r"\s+", " ", "".join(self.parts))).strip()


def strip_tags(html: str) -> str:
    p = _Strip()
    p.feed(html)
    return p.text()


def fetch_metadata(arxiv_id: str) -> dict:
    url = f"https://export.arxiv.org/api/query?id_list={arxiv_id}"
    with urllib.request.urlopen(url) as r:
        root = ET.fromstring(r.read())
    e = root.find("a:entry", NS)
    return {
        "arxiv_id": arxiv_id,
        "title": e.find("a:title", NS).text.strip().replace("\n", " "),
        "published": e.find("a:published", NS).text,   # first version date
        "updated": e.find("a:updated", NS).text,        # this version's date
        "url_or_doi": e.find("a:id", NS).text,           # abs URL, versioned
    }


def fetch_sections(arxiv_id: str) -> dict:
    time.sleep(3)  # ловушка: >=1 req/3s per arXiv API ToU, апплай и к arxiv.org/html
    html_url = f"https://arxiv.org/html/{arxiv_id}"
    try:
        with urllib.request.urlopen(html_url) as r:
            html = r.read().decode("utf-8")
    except urllib.error.HTTPError as ex:
        raise RuntimeError(f"no HTML for {arxiv_id} (status {ex.code}); fall back to PDF") from ex

    out = {"abstract": "", "sections": []}
    m = re.search(r'<div class="ltx_abstract">(.*?)</div>', html, re.S)
    if m:
        out["abstract"] = strip_tags(re.sub(r"<h6[^>]*>.*?</h6>", "", m.group(1), flags=re.S))

    for sec_m in re.finditer(
        r'<section class="ltx_(section|bibliography|appendix)"[^>]*id="([^"]*)">(.*?)</section>\s*(?=<section|\Z)',
        html, re.S,
    ):
        kind, sec_id, body = sec_m.groups()
        h = re.search(r"<h[1-6][^>]*>(.*?)</h[1-6]>", body, re.S)
        title = strip_tags(h.group(1)) if h else sec_id
        out["sections"].append({"id": sec_id, "kind": kind, "title": title, "text": strip_tags(body)})
    return out


if __name__ == "__main__":
    aid = "2406.04824v2"
    meta = fetch_metadata(aid)
    data = fetch_sections(aid)
    assert meta["title"], "metadata fetch failed"
    assert data["abstract"], "abstract extraction failed"
    assert len(data["sections"]) >= 3, "section split failed"
    print("META:", meta)
    print("ABSTRACT (first 150 chars):", data["abstract"][:150])
    for s in data["sections"]:
        print(f"- [{s['kind']}] {s['title']!r}: {len(s['text'])} chars")

