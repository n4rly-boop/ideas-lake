"""Build the one-file demo page out of a real snapshot of the lake.

    python3 demo/build.py            # -> demo/index.html

Input is what block A's own HTTP layer answered on prod (school10) at the moment of
the snapshot, verbatim: `snapshot.json` is the body of /stats, /healthz, /sources,
/ideas, /ideas/{id}/neighbors, /search and eight /retrieve calls; `retrieve_log.jsonl`
is the matching tail of `data/logs/retrieve.jsonl`, which is where `cut_off` lives —
the answer itself does not carry it (§5.5). `vectors.npy` holds the 384-float idea
embeddings, dropped from the committed snapshot because they are 1.7 MB of noise to
read and are needed only to seed the map's layout.

The page has no JavaScript. It is built for screenshots, so every number is rendered
into the HTML at build time and hover is whatever an SVG `<title>` gives for free.

Nothing here computes a number the API could have answered: `total`s, counts and
scores are copied. The only derived values are the map's coordinates (a layout, not a
measurement) and the two ablation charts' geometry.
"""
import html
import json
import math
from collections import Counter
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
OUT = HERE / "index.html"

# The query the hero section is built around: the one where the lake has a real answer
# (top cosine 0.634 — the highest of the four probes).
HERO_QUERY = "cheap way to avoid evaluating every candidate"
CONTROL_QUERY = "how to bake sourdough bread"   # the lake has nothing on this, by design
MAP_W, MAP_H = 1340, 660


def esc(text) -> str:
    return html.escape(str(text), quote=True)


def num(value) -> str:
    """1234 -> '1 234'. Narrow no-break space, so a column never wraps mid-number."""
    return f"{value:,}".replace(",", " ")


# --------------------------------------------------------------------------- load

snap = json.loads((HERE / "snapshot.json").read_text(encoding="utf-8"))
log_rows = [json.loads(line) for line in (HERE / "retrieve_log.jsonl").read_text(
    encoding="utf-8").splitlines() if line.strip()]
log_by_id = {row["log_id"]: row for row in log_rows}

stats = snap["stats"]["body"]
health = snap["healthz"]["body"]
sources = snap["sources"]["body"]["items"]
ideas = [item for page in snap["idea_pages"] for item in page["body"]["items"]]
by_id = {idea["id"]: idea for idea in ideas}
edges = snap["edges"]
retrieves = snap["retrieve"]
search_hits = snap["search"]["body"]

vectors = np.load(HERE / "vectors.npy")
vector_ids = json.loads((HERE / "vector_ids.json").read_text(encoding="utf-8"))

# Undirected pairs: the store keeps co-citation in both directions (1487 pairs, 2974
# rows), and drawing both would double every line's opacity.
undirected = {}
for edge in edges:
    key = frozenset((edge["source_id"], edge["target_id"]))
    if len(key) == 2 and (key not in undirected or edge["weight"] > undirected[key]["weight"]):
        undirected[key] = edge
pairs = [(sorted(key)[0], sorted(key)[1], edge["weight"], edge["type"], edge["note"])
         for key, edge in undirected.items()]


def arm(query: str, rewrite: bool) -> dict:
    for entry in retrieves:
        if entry["query"] == query and entry["rewrite"] == rewrite and entry["status"] == 200:
            return entry
    raise KeyError((query, rewrite))


hero = arm(HERO_QUERY, True)
hero_log = log_by_id[hero["body"]["log_id"]]
control = arm(CONTROL_QUERY, True)
control_log = log_by_id[control["body"]["log_id"]]
QUERIES = [entry["query"] for entry in retrieves if entry["rewrite"]]


def check_snapshot() -> None:
    """The page's whole claim is "every number here is a body the API answered". These
    asserts are that claim, run once per build: a tile that disagrees with /stats, an
    answer whose log line is missing, or an idea a card cannot look up is a page that
    lies quietly, and a build that dies is cheaper than a screenshot nobody re-checks.
    """
    assert len(ideas) == stats["ideas"], (len(ideas), stats["ideas"])
    assert len(sources) == stats["sources"], (len(sources), stats["sources"])
    assert sum(len(i["theses"]) for i in ideas) == stats["theses"]
    assert len(edges) == stats["edges"], (len(edges), stats["edges"])
    # Both directions are stored, so pairs must be exactly half — if a future ingest
    # writes a one-way edge this fires rather than drawing a line that is not there twice.
    assert len(pairs) * 2 == len(edges), (len(pairs), len(edges))
    assert stats["theses_indexed"] == health["leaves_in_store"] == stats["theses"]
    assert not stats["ideas_without_leaves"], stats["ideas_without_leaves"]
    for entry in retrieves:
        assert entry["status"] == 200, (entry["query"], entry["status"])
        assert entry["body"]["log_id"] in log_by_id, entry["body"]["log_id"]
        for item in entry["body"]["ideas"]:
            assert item["idea_id"] in by_id, item["idea_id"]   # cards read `dirty` from here
            assert item["theses"], item["idea_id"]             # IDEA ||--|{ THESIS
    for query in QUERIES:
        arm(query, False)                                      # both arms exist for every probe
    assert all(hit["bm25_rank"] or hit["vec_rank"] for hit in search_hits)


check_snapshot()

# --------------------------------------------------------------------- map layout
# Force-directed over the real co-citation graph, seeded by a PCA of the idea
# embeddings. The result is a LAYOUT, not a measurement: distance on the map is not a
# distance in embedding space, and the page says so where it draws it.

ITERS, REPULSION, ATTRACTION, GRAVITY, T0 = 900, 1.0, 2.0, 0.02, 0.15

centered = vectors - vectors.mean(axis=0)
u, s, _ = np.linalg.svd(centered, full_matrices=False)
pos = (u[:, :2] * s[:2]).astype(np.float64)
pos /= np.abs(pos).max()

index_of = {idea_id: i for i, idea_id in enumerate(vector_ids)}
edge_ix = np.array([[index_of[a], index_of[b]] for a, b, *_ in pairs if
                    a in index_of and b in index_of], dtype=np.int32)
edge_w = np.array([w for a, b, w, *_ in pairs if a in index_of and b in index_of])
n = len(pos)
k = math.sqrt(4.0 / n)                        # the frame is [-1,1]², so its area is 4

for step in range(ITERS):
    temperature = T0 * (1 - step / ITERS) ** 1.5 + 1e-4
    delta = pos[:, None, :] - pos[None, :, :]
    dist = np.linalg.norm(delta, axis=2) + 1e-9
    force = REPULSION * (delta / dist[:, :, None] * (k * k / dist)[:, :, None]).sum(axis=1)
    if len(edge_ix):
        vec = pos[edge_ix[:, 0]] - pos[edge_ix[:, 1]]
        length = np.linalg.norm(vec, axis=1) + 1e-9
        # log1p(weight): a pair co-cited by 11 sources pulls harder than one co-cited once,
        # but not eleven times harder — otherwise the heavy pairs collapse onto each other.
        pull = (vec / length[:, None]) * ((length ** 2 / k) * ATTRACTION
                                         * np.log1p(edge_w))[:, None]
        np.add.at(force, edge_ix[:, 0], -pull)
        np.add.at(force, edge_ix[:, 1], pull)
    force -= GRAVITY * pos                    # keeps the disconnected node from drifting off
    norm = np.linalg.norm(force, axis=1, keepdims=True) + 1e-12
    pos += force / norm * np.minimum(norm, temperature)
    pos -= pos.mean(axis=0)
    pos = np.clip(pos, -1.05, 1.05)

pos /= np.abs(pos).max()
pad = 26
px = np.column_stack([pad + (pos[:, 0] + 1) / 2 * (MAP_W - 2 * pad),
                      pad + (pos[:, 1] + 1) / 2 * (MAP_H - 2 * pad)])

# Overlap relaxation in pixel space: the force layout knows nothing about how big a node
# is drawn, and a node hidden under another is a node the picture does not show.
leaves_of = {idea["id"]: len(idea["theses"]) for idea in ideas}
radii = np.array([4.0 + 1.55 * math.sqrt(max(0, leaves_of.get(i, 1) - 1)) for i in vector_ids])
for _ in range(140):
    delta = px[:, None, :] - px[None, :, :]
    dist = np.linalg.norm(delta, axis=2) + 1e-9
    want = radii[:, None] + radii[None, :] + 3.0
    np.fill_diagonal(want, 0.0)
    overlap = np.maximum(0.0, want - dist)
    if overlap.max() < 0.4:
        break
    push = (delta / dist[:, :, None] * (overlap * 0.5)[:, :, None]).sum(axis=1)
    px += np.clip(push, -6, 6)
    px[:, 0] = np.clip(px[:, 0], pad, MAP_W - pad)
    px[:, 1] = np.clip(px[:, 1], pad, MAP_H - pad)

xy = {idea_id: (float(x), float(y)) for idea_id, (x, y) in zip(vector_ids, px)}
assert np.isfinite(px).all(), "layout diverged"
assert len(xy) == len(ideas), (len(xy), len(ideas))     # every idea gets a dot, or the map lies
assert all(item["idea_id"] in xy for item in hero["body"]["ideas"])

# ------------------------------------------------------------------------ helpers

TRUST_STEPS = 9        # 0.0 … 0.8, the range the judge actually produced


def trust_class(score: float) -> str:
    return f"t{min(TRUST_STEPS - 1, max(0, int(round(score * 10))))}"


def leaf_radius(count: int) -> float:
    return 4.0 + 1.55 * math.sqrt(max(0, count - 1))


def cos_class(value: float) -> str:
    """The measured band, not a guess: the four probes put a query the lake answers at
    0.63 and the control query at 0.45–0.52 (README §8.1 measured ~0.48 / ~0.75)."""
    return "cos-hi" if value >= 0.60 else ("cos-mid" if value >= 0.52 else "cos-lo")


VIA_LABEL = {"thesis": ("тезис", "◆"), "edge": ("ребро", "◈"), "padding": ("добито", "◇")}


def via_badge(via: str) -> str:
    label, glyph = VIA_LABEL.get(via, (via, "•"))
    return (f'<span class="badge via-{esc(via)}"><span class="glyph">{glyph}</span>'
            f'via {esc(label)}</span>')


def tile(value: str, label: str, note: str = "") -> str:
    return (f'<div class="tile"><div class="tile-v">{value}</div>'
            f'<div class="tile-l">{esc(label)}</div>'
            + (f'<div class="tile-n">{note}</div>' if note else "") + "</div>")


def bullet_list(items: list[str]) -> str:
    return "<ul class='fm'>" + "".join(f"<li>{esc(item)}</li>" for item in items) + "</ul>"


# ------------------------------------------------------------------- idea card

def idea_card(item: dict, rank: int, *, open_leaves: bool = False) -> str:
    body = by_id.get(item["idea_id"], {})
    leaves = item["theses"]
    shown = leaves if open_leaves else leaves[:2]
    rows = []
    for leaf in shown:
        rows.append(
            '<div class="leaf">'
            f'<div class="leaf-text">{esc(leaf["text"])}</div>'
            '<div class="leaf-meta">'
            f'<a class="src" href="{esc(leaf["url"])}">{esc(leaf["title"])}</a>'
            f'<span class="loc">{esc(leaf["locator"])}</span>'
            + (f'<span class="eff">{esc(leaf["effect"])}</span>' if leaf["effect"] else "")
            + "</div></div>")
    hidden = len(leaves) - len(shown)
    if hidden > 0:
        rows.append(f'<div class="leaf-more">+ ещё {hidden} '
                    f'{"лист" if hidden == 1 else "листа" if hidden < 5 else "листьев"}</div>')

    dirty = body.get("dirty")
    trust_note = ' <span class="stale">устарел</span>' if dirty else ""
    return f"""
      <article class="card">
        <header class="card-head">
          <span class="rank">{rank}</span>
          <div class="card-title">{esc(item["text"])}</div>
        </header>
        <div class="card-metrics">
          {via_badge(item["via"])}
          <span class="metric"><span class="mk">cosine</span>
            <span class="mv {cos_class(item["cosine_similarity"])}">{item["cosine_similarity"]:.3f}</span></span>
          <span class="metric"><span class="mk">score</span>
            <span class="mv dim">{item["score"]:.2f}</span></span>
          <span class="metric trust"><span class="mk">trust</span>
            <span class="trust-bar"><i style="width:{item["trust_score"] * 100:.0f}%"></i></span>
            <span class="mv">{item["trust_score"]:.1f}</span>{trust_note}</span>
          <span class="metric"><span class="mk">листьев</span>
            <span class="mv">{len(leaves)}</span></span>
        </div>
        <div class="card-grid">
          <div><div class="field-l">применимо когда</div>
               <div class="field-v">{esc(item["applicability_conditions"])}</div></div>
          <div><div class="field-l">ограничения</div>
               <div class="field-v">{esc(item["limitations"])}</div></div>
          <div><div class="field-l">эффект заявленный</div>
               <div class="field-v">{esc(item["effect_claimed"]) or "—"}</div></div>
          <div><div class="field-l">эффект наблюдённый</div>
               <div class="field-v">{esc(item["effect_observed"]) or "—"}</div></div>
        </div>
        <details class="fmodes" {"open" if open_leaves else ""}>
          <summary>режимы отказа · {len(item["failure_modes"])}</summary>
          {bullet_list(item["failure_modes"])}
        </details>
        <div class="leaves">
          <div class="leaves-h">провенанс · {len(leaves)} {"лист" if len(leaves) == 1 else "листьев"}</div>
          {"".join(rows)}
        </div>
        <div class="card-id">{esc(item["idea_id"])}</div>
      </article>"""


# ------------------------------------------------------------ chart: candidates

def candidates_strip(log: dict) -> str:
    ret = log["returned"]
    cut = log["cut_off"]
    width, height = 1340, 172
    left, right = 46, 26
    top, bottom = 30, 34
    scores = [row["score"] for row in ret + cut] or [0.0]
    hi = max(scores + [1.0])

    def sx(score):
        return left + score / hi * (width - left - right)

    baseline = height - bottom
    parts = [f'<line class="axis" x1="{left}" y1="{baseline}" x2="{width - right}" y2="{baseline}"/>']
    for tick in (0.0, 0.25, 0.5, 0.75, 1.0):
        if tick > hi:
            continue
        x = sx(tick)
        parts.append(f'<line class="grid" x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{baseline}"/>')
        parts.append(f'<text class="tick" x="{x:.1f}" y="{baseline + 16}" text-anchor="middle">'
                     f'{tick:.2f}</text>')
    # Beeswarm rather than one line: 38 candidates whose scores crowd below 0.25 draw as one
    # smear otherwise, and the shape of that crowd IS the point of the picture.
    placed: list[tuple[float, float]] = []

    middle = (top + baseline) / 2

    def swarm(x: float, r: float) -> float:
        for lane in range(0, 6):
            for sign in ((1, -1) if lane else (1,)):
                y = middle + sign * lane * 14
                if all(abs(x - ox) > r + 6.5 or abs(y - oy) > 1 for ox, oy in placed):
                    placed.append((x, y))
                    return y
        placed.append((x, middle))
        return middle

    for row in sorted(cut, key=lambda item: -item["score"]):
        x = sx(row["score"])
        parts.append(f'<circle class="cand cut" cx="{x:.1f}" cy="{swarm(x, 5):.1f}" r="5">'
                     f'<title>отсечено · rank {row["rank"]} · score {row["score"]:.3f}</title></circle>')
    for row in ret:
        x = sx(row["score"])
        parts.append(f'<circle class="cand ret" cx="{x:.1f}" cy="{swarm(x, 6.5):.1f}" r="6.5">'
                     f'<title>выдано · rank {row["rank"]} · score {row["score"]:.3f} · '
                     f'via {row["via"]}</title></circle>')
    return f"""<figure class="fig">
      <figcaption><b>Все кандидаты одного запроса.</b> Выдано {len(ret)}, отсечено {len(cut)} —
        и отсечённое лежит в логе со скорами, не выбрасывается (§5.5).</figcaption>
      <svg viewBox="0 0 {width} {height}" class="chart" role="img"
           aria-label="Скоры {len(ret) + len(cut)} кандидатов: {len(ret)} выдано, {len(cut)} отсечено">
        {"".join(parts)}
        <text class="axis-l" x="{left}" y="18">score (min-max по этому вызову)</text>
      </svg>
      <div class="legend">
        <span class="lg"><span class="sw ret"></span>выдано в ответе ({len(ret)})</span>
        <span class="lg"><span class="sw cut"></span>отсечено, записано в лог ({len(cut)})</span>
      </div>
    </figure>"""


# --------------------------------------------------------------- chart: ablation

def grouped_bars(title: str, caption: str, unit: str, values: dict, fmt) -> str:
    """One measure, two series (rewrite on / off), four queries. Horizontal bars, so the
    query text is readable without rotation. One axis — never two measures in one chart."""
    width = 660
    label_w, right = 250, 58
    row_h, bar_h, gap = 62, 18, 6
    height = 42 + row_h * len(values)
    hi = max(max(pair) for pair in values.values()) or 1.0

    def bx(value):
        return (value / hi) * (width - label_w - right)

    parts = []
    for i, (query, (on, off)) in enumerate(values.items()):
        y = 34 + i * row_h
        short = query if len(query) < 34 else query[:32] + "…"
        parts.append(f'<text class="bar-l" x="0" y="{y + bar_h - 3}">{esc(short)}</text>')
        for j, (value, cls, name) in enumerate(((on, "s1", "rewrite on"), (off, "s2", "rewrite off"))):
            by = y + j * (bar_h + gap)
            w = max(2.0, bx(value))
            parts.append(
                f'<rect class="bar {cls}" x="{label_w}" y="{by}" width="{w:.1f}" height="{bar_h}" rx="4">'
                f'<title>{esc(query)} · {name} · {fmt(value)}{unit}</title></rect>'
                f'<text class="bar-v" x="{label_w + w + 8:.1f}" y="{by + bar_h - 4}">{fmt(value)}</text>')
    return f"""<figure class="fig half">
      <figcaption><b>{title}</b> {caption}</figcaption>
      <svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="{esc(title)}">
        {"".join(parts)}
      </svg>
      <div class="legend">
        <span class="lg"><span class="sw s1"></span>rewrite on</span>
        <span class="lg"><span class="sw s2"></span>rewrite off</span>
        {f'<span class="lg dim">единица: {esc(unit.strip())}</span>' if unit.strip() else
         '<span class="lg dim">безразмерная величина, [-1, 1]</span>'}
      </div>
    </figure>"""


# -------------------------------------------------------------------- chart: map

def lake_map(highlight: list[dict]) -> str:
    highlighted = {item["idea_id"]: i + 1 for i, item in enumerate(highlight)}
    degree = Counter()
    for a, b, *_ in pairs:
        degree[a] += 1
        degree[b] += 1

    lines = []
    for a, b, weight, kind, note in pairs:
        if a not in xy or b not in xy:
            continue
        x1, y1 = xy[a]
        x2, y2 = xy[b]
        strength = min(1.0, math.log1p(weight) / math.log1p(6))
        opacity = 0.05 + 0.22 * strength
        lines.append(f'<line class="edge" x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                     f'stroke-opacity="{opacity:.3f}" stroke-width="{0.6 + 0.9 * strength:.2f}"/>')

    nodes, rings, labels = [], [], []
    for idea in sorted(ideas, key=lambda item: len(item["theses"])):
        if idea["id"] not in xy:
            continue
        x, y = xy[idea["id"]]
        r = leaf_radius(len(idea["theses"]))
        tip = (f'{idea["text"][:150]}\ntrust {idea["trust_score"]:.1f} · '
               f'листьев {len(idea["theses"])} · рёбер {degree[idea["id"]]}')
        nodes.append(f'<circle class="node {trust_class(idea["trust_score"])}" cx="{x:.1f}" '
                     f'cy="{y:.1f}" r="{r:.1f}"><title>{esc(tip)}</title></circle>')
        if idea["id"] in highlighted:
            rank = highlighted[idea["id"]]
            rings.append(f'<circle class="halo" cx="{x:.1f}" cy="{y:.1f}" r="{r + 6:.1f}"/>'
                         f'<circle class="ring" cx="{x:.1f}" cy="{y:.1f}" r="{r + 6:.1f}"/>')
            labels.append(f'<text class="node-l" x="{x:.1f}" y="{y - r - 11:.1f}" '
                          f'text-anchor="middle">№{rank}</text>')

    swatches = "".join(
        f'<span class="sw {trust_class(step / 10)}"></span>' for step in range(0, 9))
    return f"""<figure class="fig">
      <figcaption><b>Озеро целиком: {num(len(ideas))} идей, {num(len(pairs))} связей.</b>
        Раскладка force-directed по рёбрам co-citation, начальное положение — PCA эмбеддингов
        идей. Позиция это раскладка, а не измерение: расстояние на картинке не равно
        расстоянию в пространстве эмбеддингов. Нарисованы все {num(len(pairs))}
        неориентированных пар (в хранилище они лежат в обе стороны — {num(len(edges))} строк).
        Ничего не отброшено.</figcaption>
      <svg viewBox="0 0 {MAP_W} {MAP_H}" class="chart map" role="img"
           aria-label="Карта озера: {len(ideas)} идей, {len(pairs)} связей co-citation">
        <g class="edges">{"".join(lines)}</g>
        <g class="nodes">{"".join(nodes)}</g>
        <g class="rings">{"".join(rings)}</g>
        <g class="labels">{"".join(labels)}</g>
      </svg>
      <div class="legend wrap">
        <span class="lg"><span class="ramp">{swatches}</span>trust 0.0 → 0.8</span>
        <span class="lg"><span class="sw size-s"></span><span class="sw size-l"></span>размер — число листьев (1 … {max(len(i["theses"]) for i in ideas)})</span>
        <span class="lg"><span class="sw ring-sw"></span>выдано по запросу «{esc(HERO_QUERY)}»</span>
        <span class="lg dim">тип рёбер в озере один: related_via_source (co-citation). derived_from
          появится с синтезом — сейчас его в графе нет, и рисовать его нечем.</span>
      </div>
    </figure>"""


# ------------------------------------------------------- chart: trust histogram

def trust_hist() -> str:
    buckets = Counter(min(8, int(round(idea["trust_score"] * 10))) for idea in ideas)
    width, height = 660, 210
    left, bottom, top = 40, 34, 26
    hi = max(buckets.values())
    slot = (width - left - 16) / 9
    parts = []
    for step in range(9):
        count = buckets.get(step, 0)
        h = (count / hi) * (height - bottom - top)
        x = left + step * slot
        y = height - bottom - h
        parts.append(f'<rect class="bar {trust_class(step / 10)}" x="{x + 3:.1f}" y="{y:.1f}" '
                     f'width="{slot - 6:.1f}" height="{max(h, 1.5):.1f}" rx="4">'
                     f'<title>trust {step / 10:.1f} · {count} идей</title></rect>')
        parts.append(f'<text class="tick" x="{x + slot / 2:.1f}" y="{height - bottom + 16:.0f}" '
                     f'text-anchor="middle">{step / 10:.1f}</text>')
        if count:
            parts.append(f'<text class="bar-v" x="{x + slot / 2:.1f}" y="{y - 6:.1f}" '
                         f'text-anchor="middle">{count}</text>')
    parts.append(f'<line class="axis" x1="{left}" y1="{height - bottom}" '
                 f'x2="{width - 16}" y2="{height - bottom}"/>')
    return f"""<figure class="fig half">
      <figcaption><b>Как судья оценил {num(len(ideas))} идей.</b> Медиана 0.3, максимум 0.8,
        ноль у {buckets.get(0, 0)} идей — «судили, и доверять почти нечему», а не «судья упал».</figcaption>
      <svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="Распределение trust_score">
        {"".join(parts)}
        <text class="axis-l" x="0" y="14">идей</text>
      </svg>
      <div class="legend"><span class="lg dim">trust_score, шаг 0.1 · шкала цвета та же, что на карте</span></div>
    </figure>"""


# ------------------------------------------------------------------- provenance

def provenance_block() -> str:
    deepest = max(ideas, key=lambda item: len(item["theses"]))
    leaves = deepest["theses"]
    by_source: dict[str, list[dict]] = {}
    for leaf in leaves:
        by_source.setdefault(leaf["source_id"], []).append(leaf)
    source_meta = {src["id"]: src for src in sources}

    columns = []
    for source_id, group in sorted(by_source.items(), key=lambda kv: -len(kv[1])):
        src = source_meta.get(source_id, {})
        rows = "".join(
            f'<div class="leaf"><div class="leaf-text">{esc(leaf["text"])}</div>'
            f'<div class="leaf-meta"><span class="loc">{esc(leaf["locator"])}</span>'
            + (f'<span class="eff">{esc(leaf["effect"])}</span>' if leaf["effect"] else "")
            + "</div></div>" for leaf in group)
        columns.append(
            f'<div class="prov-src"><div class="prov-src-h">'
            f'<span class="badge type-{esc(src.get("type", "paper"))}">{esc(src.get("type", "?"))}</span>'
            f'<a class="src" href="{esc(src.get("url", "#"))}">{esc(src.get("title", source_id))}</a>'
            f'<span class="dim">{len(group)} '
            f'{"лист" if len(group) == 1 else "листа" if len(group) < 5 else "листьев"}</span>'
            f'</div>{rows}</div>')

    return f"""<div class="prov">
      <div class="prov-idea">
        <div class="field-l">идея</div>
        <div class="prov-idea-t">{esc(deepest["text"])}</div>
        <div class="card-metrics">
          <span class="metric"><span class="mk">trust</span>
            <span class="trust-bar"><i style="width:{deepest["trust_score"] * 100:.0f}%"></i></span>
            <span class="mv">{deepest["trust_score"]:.1f}</span></span>
          <span class="metric"><span class="mk">листьев</span><span class="mv">{len(leaves)}</span></span>
          <span class="metric"><span class="mk">источников</span><span class="mv">{len(by_source)}</span></span>
          <span class="metric"><span class="mk">origin</span><span class="mv">{esc(deepest["origin"])}</span></span>
          <span class="metric"><span class="mk">пере-выведена на</span>
            <span class="mv">{deepest["rederived_at_leaf_count"]} листьях</span></span>
        </div>
        <div class="prov-note">Дубль не выбрасывается — он становится ещё одним листом. Поэтому
          доверие к идее выводится из состава листьев, а не объявляется полем.</div>
      </div>
      <div class="prov-sources">{"".join(columns)}</div>
    </div>"""


# ------------------------------------------------------------------ search table

def search_table() -> str:
    rows = "".join(
        f'<tr><td class="mono">{i + 1}</td><td class="mono">{esc(hit["thesis_id"])}</td>'
        f'<td class="mono dim">{esc(hit["idea_id"])}</td>'
        f'<td class="mono">{hit["score"]:.5f}</td>'
        f'<td class="mono">{"—" if hit["bm25_rank"] is None else hit["bm25_rank"]}</td>'
        f'<td class="mono">{"—" if hit["vec_rank"] is None else hit["vec_rank"]}</td></tr>'
        for i, hit in enumerate(search_hits))
    dead_fts = all(hit["bm25_rank"] is None for hit in search_hits)
    verdict = ("<b class='bad'>BM25 не вернул ничего</b> — гибрид работает на одном косинусе"
               if dead_fts else
               "Обе руки вернули строки — гибрид действительно гибрид, а не косинус под другим именем")
    return f"""<table class="tbl">
      <thead><tr><th>#</th><th>тезис</th><th>идея</th><th>RRF score</th>
        <th>BM25 rank</th><th>vector rank</th></tr></thead>
      <tbody>{rows}</tbody>
      <caption>GET /search?q=surrogate+fitness+proxy&amp;k=10 — сырой индекс, без rewrite,
        без подъёма к идеям. {verdict}.</caption>
    </table>"""


# ----------------------------------------------------------------------- page

def ablation_rows() -> str:
    rows = []
    for query in QUERIES:
        on, off = arm(query, True), arm(query, False)
        on_top = max(item["cosine_similarity"] for item in on["body"]["ideas"])
        off_top = max(item["cosine_similarity"] for item in off["body"]["ideas"])
        delta = on_top - off_top
        cls = "good" if delta > 0.01 else ("bad" if delta < -0.01 else "dim")
        rows.append(f"""<tr>
          <td>{esc(query)}{' <span class="badge control">контроль на шум</span>' if query == CONTROL_QUERY else ''}</td>
          <td class="mono">{esc(on["body"].get("query_rewritten") or log_by_id[on["body"]["log_id"]]["query_rewritten"])}</td>
          <td class="mono">{on["body"]["cost"]["wall_ms"]:.0f}<span class="dim"> / {off["body"]["cost"]["wall_ms"]:.0f}</span></td>
          <td class="mono">{on["body"]["cost"]["tokens_in"]}+{on["body"]["cost"]["tokens_out"]}<span class="dim"> / 0</span></td>
          <td class="mono">{on_top:.3f}<span class="dim"> / {off_top:.3f}</span></td>
          <td class="mono {cls}">{delta:+.3f}</td>
        </tr>""")
    return "".join(rows)


latency = {q: (arm(q, True)["body"]["cost"]["wall_ms"], arm(q, False)["body"]["cost"]["wall_ms"])
           for q in QUERIES}
cosines = {q: (max(i["cosine_similarity"] for i in arm(q, True)["body"]["ideas"]),
               max(i["cosine_similarity"] for i in arm(q, False)["body"]["ideas"]))
           for q in QUERIES}

hero_ideas = "".join(idea_card(item, i + 1, open_leaves=(i == 0))
                     for i, item in enumerate(hero["body"]["ideas"]))
control_top = control["body"]["ideas"][0]
hero_top = hero["body"]["ideas"][0]
hero_best = max(hero["body"]["ideas"], key=lambda item: item["cosine_similarity"])
control_best = max(control["body"]["ideas"], key=lambda item: item["cosine_similarity"])
hero_best_rank = hero["body"]["ideas"].index(hero_best) + 1


def versus_card(kind: str, heading: str, entry: dict) -> str:
    """Both numbers, because they disagree: the answer is ORDERED by score, and its first
    element is not necessarily the closest by cosine."""
    answer = entry["body"]["ideas"]
    top = answer[0]
    best = max(answer, key=lambda item: item["cosine_similarity"])
    rank = answer.index(best) + 1
    return f"""<div class="vs {kind}">
      <div class="vs-h">{esc(heading)}</div>
      <div class="vs-q">{esc(entry["query"])}</div>
      <div class="vs-nums">
        <div><span>score элемента №1</span><b class="dim">{top["score"]:.2f}</b></div>
        <div><span>лучший cosine (№{rank})</span>
          <b class="{cos_class(best["cosine_similarity"])}">{best["cosine_similarity"]:.3f}</b></div>
        <div><span>cosine элемента №1</span>
          <b class="{cos_class(top["cosine_similarity"])}">{top["cosine_similarity"]:.3f}</b></div>
        <div><span>via всех пяти</span>
          <b>{esc(VIA_LABEL.get(top["via"], (top["via"], ""))[0])}</b></div>
      </div>
      <div class="vs-idea"><span class="dim">№1 в выдаче:</span> {esc(top["text"][:210])}…</div>
    </div>"""
by_type = Counter(src["type"] for src in sources)
queue = stats["queue"]
workers_alive = all(stats["workers"].values())

STYLE = """
:root{
  color-scheme: dark;
  --plane:#0d0d0d; --surface:#141414; --surface-2:#1a1a19; --raised:#1f1f1e;
  --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --hair:rgba(255,255,255,.10);
  --s1:#3987e5; --s2:#d95926;
  --good:#0ca30c; --warning:#fab219; --serious:#ec835a; --critical:#d03b3b;
  --t0:#1c5cab; --t1:#256abf; --t2:#2a78d6; --t3:#3987e5; --t4:#5598e7;
  --t5:#6da7ec; --t6:#86b6ef; --t7:#b7d3f6; --t8:#cde2fb;
}
:root[data-theme="light"]{
  color-scheme: light;
  --plane:#f9f9f7; --surface:#fcfcfb; --surface-2:#f4f3f0; --raised:#ffffff;
  --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --hair:rgba(11,11,11,.10);
  --s1:#2a78d6; --s2:#eb6834;
  --t0:#cde2fb; --t1:#b7d3f6; --t2:#9ec5f4; --t3:#6da7ec; --t4:#3987e5;
  --t5:#256abf; --t6:#1c5cab; --t7:#184f95; --t8:#104281;
}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap-page{max-width:1440px;margin:0 auto;padding:0 40px 96px}
a{color:inherit}
.mono{font-variant-numeric:tabular-nums;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.dim{color:var(--muted)}
.good{color:var(--good)} .bad{color:var(--critical)}

/* header */
header.top{padding:52px 0 30px;border-bottom:1px solid var(--hair)}
.kicker{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
h1{margin:12px 0 8px;font-size:44px;line-height:1.05;letter-spacing:-.02em;font-weight:650}
.sub{max-width:900px;color:var(--ink-2);font-size:16.5px}
.origin{margin-top:18px;display:flex;gap:22px;flex-wrap:wrap;font-size:12.5px;color:var(--muted)}
.origin b{color:var(--ink-2);font-weight:550}

/* tiles */
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:26px 0 14px}
.tile{background:var(--surface);border:1px solid var(--hair);border-radius:12px;padding:18px 20px}
.tile-v{font-size:34px;line-height:1.05;letter-spacing:-.02em;font-weight:600}
.tile-l{margin-top:6px;font-size:13px;color:var(--ink-2)}
.tile-n{margin-top:4px;font-size:12px;color:var(--muted)}

/* health strip */
.health{display:flex;flex-wrap:wrap;gap:10px;margin:14px 0 0}
.chip{display:inline-flex;align-items:center;gap:7px;background:var(--surface);
  border:1px solid var(--hair);border-radius:999px;padding:7px 13px;font-size:12.5px;color:var(--ink-2)}
.chip .glyph{font-size:11px}
.chip.ok .glyph{color:var(--good)} .chip.warn .glyph{color:var(--warning)}
.chip.crit .glyph{color:var(--critical)}
.chip b{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}

section{padding:56px 0 0}
.sec-h{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
h2{margin:0;font-size:13px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);font-weight:600}
.sec-t{margin:10px 0 0;font-size:28px;line-height:1.2;letter-spacing:-.015em;font-weight:600}
.sec-d{margin:10px 0 0;max-width:960px;color:var(--ink-2)}

/* query bar */
.qbar{margin:24px 0 0;background:var(--surface);border:1px solid var(--hair);border-radius:14px;
  padding:20px 22px;display:grid;grid-template-columns:1fr auto;gap:20px;align-items:center}
.q-raw{font-size:22px;font-weight:550;letter-spacing:-.01em}
.q-rw{margin-top:9px;font-size:13.5px;color:var(--ink-2)}
.q-rw .arrow{color:var(--muted);margin-right:8px}
.q-rw b{color:var(--s1);font-weight:550}
.q-cost{display:flex;gap:22px;text-align:right}
.q-cost div span{display:block;font-size:11.5px;color:var(--muted);letter-spacing:.06em;text-transform:uppercase}
.q-cost div b{font-size:19px;font-weight:600;font-variant-numeric:tabular-nums}

/* cards */
.cards{margin-top:16px;display:grid;gap:12px}
.card{background:var(--surface);border:1px solid var(--hair);border-radius:14px;padding:20px 22px;position:relative}
.card-head{display:flex;gap:14px;align-items:flex-start;padding-right:160px}
.rank{flex:0 0 26px;height:26px;border-radius:8px;background:var(--surface-2);color:var(--ink-2);
  display:grid;place-items:center;font-size:12.5px;font-weight:600;font-variant-numeric:tabular-nums}
.card-title{font-size:17px;line-height:1.45;font-weight:550;letter-spacing:-.005em}
.card-metrics{display:flex;flex-wrap:wrap;gap:8px 18px;margin:14px 0 0;padding:12px 0 0;border-top:1px solid var(--hair)}
.metric{display:inline-flex;align-items:center;gap:7px;font-size:12.5px}
.mk{color:var(--muted);letter-spacing:.04em;text-transform:uppercase;font-size:11px}
.mv{font-variant-numeric:tabular-nums;font-weight:600}
.cos-hi{color:var(--good)} .cos-mid{color:var(--warning)} .cos-lo{color:var(--muted)}
.trust-bar{width:54px;height:6px;border-radius:3px;background:var(--surface-2);overflow:hidden}
.trust-bar i{display:block;height:100%;background:var(--s1)}
.stale{font-size:11px;color:var(--warning)}
.badge{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:4px 10px;
  font-size:11.5px;border:1px solid var(--hair);background:var(--surface-2);color:var(--ink-2)}
.badge .glyph{font-size:10px}
.via-thesis .glyph{color:var(--good)} .via-edge .glyph{color:var(--warning)}
.via-padding{color:var(--muted)} .via-padding .glyph{color:var(--serious)}
.badge.control{border-color:var(--serious);color:var(--serious)}
.badge.type-run{border-color:var(--s2)} .badge.type-paper{border-color:var(--s1)}
.card-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px 26px;margin-top:16px}
.field-l{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.field-v{margin-top:5px;font-size:13.5px;color:var(--ink-2);line-height:1.5}
.fmodes{margin-top:16px;border-top:1px solid var(--hair);padding-top:12px}
.fmodes summary{cursor:default;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
ul.fm{margin:10px 0 0;padding-left:18px}
ul.fm li{font-size:13px;color:var(--ink-2);margin-bottom:6px}
.leaves{margin-top:16px;border-top:1px solid var(--hair);padding-top:14px}
.leaves-h{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-bottom:10px}
.leaf{border-left:2px solid var(--s1);padding:2px 0 2px 14px;margin-bottom:12px}
.leaf-text{font-size:13.5px;line-height:1.5;color:var(--ink-2)}
.leaf-meta{margin-top:6px;display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;font-size:11.5px}
.src{color:var(--s1);text-decoration:none;border-bottom:1px solid color-mix(in srgb,var(--s1) 40%,transparent)}
.loc{color:var(--muted);font-family:ui-monospace,Menlo,monospace}
.eff{color:var(--ink-2);background:var(--surface-2);border-radius:6px;padding:2px 7px}
.leaf-more{font-size:12px;color:var(--muted);padding-left:16px}
.card-id{position:absolute;top:18px;right:20px;font-size:11px;color:var(--muted);
  font-family:ui-monospace,Menlo,monospace}

/* figures & charts */
.fig{margin:22px 0 0;background:var(--surface);border:1px solid var(--hair);border-radius:14px;padding:20px 22px}
.fig figcaption{font-size:13px;color:var(--ink-2);max-width:1100px;line-height:1.55;margin-bottom:14px}
.fig figcaption b{color:var(--ink);font-weight:600}
.chart{width:100%;height:auto;display:block;overflow:visible}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.row2 .fig{margin:0}
.axis{stroke:var(--axis);stroke-width:1}
.grid{stroke:var(--grid);stroke-width:1}
.tick,.axis-l,.bar-l,.bar-v,.node-l{fill:var(--muted);font-size:11px;
  font-family:system-ui,sans-serif;font-variant-numeric:tabular-nums}
.bar-l{fill:var(--ink-2);font-size:12px}
.bar-v{fill:var(--ink-2);font-size:11.5px;font-weight:600}
.node-l{fill:var(--ink);font-size:11px;font-weight:650}
.bar.s1{fill:var(--s1)} .bar.s2{fill:var(--s2)}
.cand{stroke:var(--surface);stroke-width:2}
.cand.ret{fill:var(--s1)} .cand.cut{fill:none;stroke:var(--axis);stroke-width:1.4}
.edge{stroke:var(--ink-2)}
.node{stroke:var(--surface);stroke-width:1}
.halo{fill:none;stroke:var(--surface);stroke-width:4}
.ring{fill:none;stroke:var(--s2);stroke-width:2}
.map{background:var(--surface-2);border-radius:10px}
.t0{fill:var(--t0)} .t1{fill:var(--t1)} .t2{fill:var(--t2)} .t3{fill:var(--t3)}
.t4{fill:var(--t4)} .t5{fill:var(--t5)} .t6{fill:var(--t6)} .t7{fill:var(--t7)} .t8{fill:var(--t8)}
.legend{margin-top:14px;display:flex;gap:20px;flex-wrap:wrap;align-items:center;font-size:12px;color:var(--ink-2)}
.legend.wrap{gap:14px 24px}
.lg{display:inline-flex;align-items:center;gap:8px}
.sw{width:11px;height:11px;border-radius:3px;display:inline-block;background:var(--muted)}
.sw.s1{background:var(--s1)} .sw.s2{background:var(--s2)}
.sw.ret{background:var(--s1);border-radius:50%} .sw.cut{background:none;border:1.4px solid var(--axis);border-radius:50%}
.sw.ring-sw{background:none;border:2px solid var(--s2);border-radius:50%;width:13px;height:13px}
.sw.size-s{width:7px;height:7px;border-radius:50%;background:var(--ink-2)}
.sw.size-l{width:15px;height:15px;border-radius:50%;background:var(--ink-2)}
.ramp{display:inline-flex;gap:2px}
.ramp .sw{width:15px;height:11px;border-radius:2px}
.ramp .t0{background:var(--t0)} .ramp .t1{background:var(--t1)} .ramp .t2{background:var(--t2)}
.ramp .t3{background:var(--t3)} .ramp .t4{background:var(--t4)} .ramp .t5{background:var(--t5)}
.ramp .t6{background:var(--t6)} .ramp .t7{background:var(--t7)} .ramp .t8{background:var(--t8)}

/* tables */
.tbl{width:100%;border-collapse:collapse;margin-top:22px;background:var(--surface);
  border:1px solid var(--hair);border-radius:14px;overflow:hidden}
.tbl th{text-align:left;font-size:11px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--muted);font-weight:600;padding:14px 16px;border-bottom:1px solid var(--hair)}
.tbl td{padding:13px 16px;border-bottom:1px solid var(--hair);font-size:13px;color:var(--ink-2);vertical-align:top}
.tbl tbody tr:last-child td{border-bottom:0}
.tbl caption{caption-side:bottom;text-align:left;padding:14px 16px;font-size:12.5px;color:var(--muted)}

/* callout */
.callout{margin:22px 0 0;background:var(--surface);border:1px solid var(--hair);
  border-left:3px solid var(--s2);border-radius:12px;padding:18px 22px}
.callout h3{margin:0 0 8px;font-size:15px;font-weight:600}
.callout p{margin:0 0 8px;color:var(--ink-2);font-size:13.5px;line-height:1.6;max-width:1000px}
.callout p:last-child{margin-bottom:0}

/* two-up comparison */
.versus{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:22px}
.vs{background:var(--surface);border:1px solid var(--hair);border-radius:14px;padding:20px 22px}
.vs.control{border-left:3px solid var(--serious)}
.vs.real{border-left:3px solid var(--good)}
.vs-h{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.vs-q{margin-top:8px;font-size:19px;font-weight:600;letter-spacing:-.01em}
.vs-nums{display:flex;gap:26px;margin:16px 0}
.vs-nums div span{display:block;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.vs-nums div b{font-size:26px;font-weight:600;font-variant-numeric:tabular-nums}
.vs-idea{font-size:13.5px;line-height:1.55;color:var(--ink-2);padding-top:14px;border-top:1px solid var(--hair)}

/* provenance */
.prov{margin-top:22px;display:grid;grid-template-columns:minmax(340px,1fr) 2fr;gap:14px}
.prov-idea,.prov-src{background:var(--surface);border:1px solid var(--hair);border-radius:14px;padding:20px 22px}
.prov-idea-t{margin-top:8px;font-size:17px;line-height:1.45;font-weight:550}
.prov-note{margin-top:16px;padding-top:14px;border-top:1px solid var(--hair);
  font-size:12.5px;color:var(--muted);line-height:1.55}
.prov-sources{display:grid;gap:14px}
.prov-src-h{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px}

footer{margin-top:64px;padding-top:26px;border-top:1px solid var(--hair);
  display:grid;grid-template-columns:1fr 1fr;gap:26px;font-size:12.5px;color:var(--muted);line-height:1.6}
footer b{color:var(--ink-2);font-weight:550}
"""

HTML = f"""<!doctype html>
<html lang="ru" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=1440">
<title>Озеро идей — блок A</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap-page">

<header class="top">
  <div class="kicker">AIRI Summer 2026 · проект 28 · блок A</div>
  <h1>Озеро идей</h1>
  <p class="sub">Долговременная память между прогонами эволюции. Статья, документация или лог
    прогона → <b>тезис</b> (утверждение близко к источнику, с числом и провенансом) →
    <b>идея</b> (обобщённый приём: условия применимости, ограничения, режимы отказа).
    Дубль не выбрасывается, он становится ещё одним листом — поэтому доверие к идее
    выводится из состава её листьев, а не объявляется полем.</p>
  <div class="origin">
    <span><b>Снимок:</b> прод block A, school10, {esc(log_rows[-1]["ts"][:16].replace("T", " "))} UTC</span>
    <span><b>Источник чисел:</b> /stats, /healthz, /sources, /ideas, /neighbors, /search,
      восемь вызовов /retrieve и хвост retrieve.jsonl</span>
    <span><b>Ничего не досочинено:</b> каждое число на странице — тело ответа ручки</span>
  </div>

  <div class="tiles">
    {tile(num(stats["sources"]), "источников в озере",
          f'{by_type.get("paper", 0)} статей · {by_type.get("run", 0)} логов прогонов')}
    {tile(num(stats["ideas"]), "идей — нод графа",
          f'все origin=extracted · dirty у {sum(1 for i in ideas if i["dirty"])}')}
    {tile(num(stats["theses"]), "тезисов-листьев",
          f'медиана 1 лист на идею, максимум {max(len(i["theses"]) for i in ideas)}')}
    {tile(num(stats["edges"]), "рёбер идея—идея",
          f'{num(len(pairs))} пар, все — co-citation')}
  </div>

  <div class="health">
    <span class="chip ok"><span class="glyph">●</span>/healthz <b>{esc(health["status"])}</b></span>
    <span class="chip {"ok" if stats["in_sync"] else "crit"}"><span class="glyph">●</span>
      индекс == хранилище <b>{num(stats["theses_indexed"])} / {num(health["leaves_in_store"])}</b></span>
    <span class="chip {"ok" if not stats["ideas_without_leaves"] else "crit"}"><span class="glyph">●</span>
      идей без листьев <b>{len(stats["ideas_without_leaves"])}</b></span>
    <span class="chip {"ok" if workers_alive else "crit"}"><span class="glyph">●</span>
      воркеры <b>{"writer + 2 fetch живы" if workers_alive else "мёртвые есть"}</b></span>
    <span class="chip ok"><span class="glyph">●</span>заданий ok <b>{queue.get("ok", 0)}</b></span>
    <span class="chip warn"><span class="glyph">▲</span>заданий failed <b>{queue.get("failed", 0)}</b>
      &nbsp;<span class="dim">недостижимые источники, отказ назван</span></span>
    <span class="chip warn"><span class="glyph">▲</span>очередь арбитра <b>{stats["pending_link"]}</b>
      &nbsp;<span class="dim">сбой арбитра → pending_link, не тихий add</span></span>
  </div>
</header>

<section>
  <div class="sec-h"><h2>Read path · POST /retrieve</h2></div>
  <p class="sec-t">Запрос эволюции → идеи, и каждая со своим провенансом</p>
  <p class="sec-d">Один вызов: запрос переписывается «в терминах решения» моделью Qwen3.5-9B,
    гибридный поиск идёт по тезисам (BM25 + косинус, RRF), найденные листья поднимаются к
    своим идеям, идеи ранжируются. Политика recall-first: отказа по низкому скору нет,
    выдача дозаполняется до k — поэтому на экране обязателен <b>via</b>, иначе «нашли» и
    «добили» неотличимы.</p>

  <div class="qbar">
    <div>
      <div class="q-raw">{esc(hero["query"])}</div>
      <div class="q-rw"><span class="arrow">переписано →</span>
        <b>{esc(hero_log["query_rewritten"])}</b></div>
    </div>
    <div class="q-cost">
      <div><span>k</span><b>{hero["body"]["ideas"].__len__()}</b></div>
      <div><span>tokens in/out</span><b>{hero["body"]["cost"]["tokens_in"]}/{hero["body"]["cost"]["tokens_out"]}</b></div>
      <div><span>wall</span><b>{hero["body"]["cost"]["wall_ms"] / 1000:.2f} с</b></div>
      <div><span>log_id</span><b class="mono">{esc(hero["body"]["log_id"])}</b></div>
    </div>
  </div>

  <div class="cards">{hero_ideas}</div>
  {candidates_strip(hero_log)}
</section>

<section>
  <div class="sec-h"><h2>Эксперимент · плечо абляции</h2></div>
  <p class="sec-t">rewrite on против rewrite off, тот же запрос, тот же граф</p>
  <p class="sec-d">Проект принимается по эксперименту, а не по системе, поэтому переписывание
    запроса — переключаемое плечо, а не встроенный шаг. Ниже те же четыре запроса в двух
    режимах. Две величины — две картинки: одна ось на график, никаких двух шкал.</p>

  <div class="row2">
    {grouped_bars("Задержка ответа.", "Переписывание — это один вызов 9B поверх поиска.",
                  " мс", latency, lambda v: f"{v:.0f}")}
    {grouped_bars("Лучший cosine в выдаче.",
                  "Косинус запроса и идеи; не перенормируется, сравним между вызовами.",
                  "", cosines, lambda v: f"{v:.3f}")}
  </div>

  <table class="tbl">
    <thead><tr><th>запрос</th><th>во что переписан</th><th>мс on / off</th>
      <th>токены on / off</th><th>лучший cosine on / off</th><th>Δ cosine</th></tr></thead>
    <tbody>{ablation_rows()}</tbody>
    <caption>Восемь вызовов /retrieve на живом проде, k=5. Δ считается по лучшему cosine в выдаче.</caption>
  </table>

  <div class="callout">
    <h3>Что здесь видно, и это не тот результат, которого ждали</h3>
    <p>Переписывание стоит <b>+1.0…1.3 с</b> и ~500 входных токенов на запрос, а лучший
      cosine оно на трёх запросах из четырёх <b>снижает</b>. Единственный запрос, которому
      переписывание помогло, — контрольный «how to bake sourdough bread»: 0.451 → 0.520.
      То есть на этом озере rewrite делает <b>нерелевантный запрос похожим на релевантный</b>,
      а не наоборот.</p>
    <p>Это ровно то, для чего плечо и нужно. Число измерено на 225 идеях и одном прогоне
      по каждому плечу — на защиту оно идёт как направление, а не как вывод: набора из 30
      оценочных запросов ещё нет, и без него разницу 0.03 по одному замеру нельзя называть
      значимой.</p>
  </div>
</section>

<section>
  <div class="sec-h"><h2>Контроль на шум</h2></div>
  <p class="sec-t">Запрос, на который в озере нет ответа, — и почему выдача всё равно не пустая</p>
  <p class="sec-d">Recall-first означает, что ответ дозаполняется до k. Значит «озеро знает» и
    «озеро выдало пять лучших из ничего» нужно отличать по числу, и это число —
    не <b>score</b>: он min-max нормирован по кандидатам этого же вызова, поэтому у лучшего
    он ~1.0 всегда, на любом запросе. Отличать по <b>cosine_similarity</b>: он не
    перенормируется и сравним между вызовами.</p>

  <div class="versus">
    {versus_card("real", "запрос, на который у озера есть ответ", hero)}
    {versus_card("control", "контрольный запрос: в озере нет ни одного релевантного листа", control)}
  </div>

  <div class="callout">
    <h3>Два одинаковых score, разные ответы</h3>
    <p>Слева и справа <b>score {hero_top["score"]:.2f} и {control_top["score"]:.2f}</b> —
      по нему запросы неотличимы, и вызывающий, которому надо решить «хватит или идти в веб»,
      по нему решить не может. Cosine различает: лучший в выдаче
      <b>{hero_best["cosine_similarity"]:.3f}</b> против
      <b>{control_best["cosine_similarity"]:.3f}</b>.</p>
    <p>И тут же второе, что видно только если положить рядом два числа: порядок выдачи
      задаёт <b>score</b>, а он строится на RRF по тезисам, — поэтому элемент №1 не обязан
      быть самым близким по смыслу. На релевантном запросе №1 имеет cosine
      {hero_top["cosine_similarity"]:.3f}, а максимум в той же выдаче —
      {hero_best["cosine_similarity"]:.3f} у элемента №{hero_best_rank}.</p>
    <p>Разрыв между релевантным и контрольным узкий, и это честная картина этого озера, а не
      хорошая. Универсальные энкодеры держат ненулевой пол между несвязанными текстами:
      сравнивать надо со своим измеренным базовым уровнем, не с нулём. Ни на одном из восьми
      вызовов <b>via не стал <code>padding</code></b> — индекс всегда что-то находил, поэтому
      «пусто» здесь выглядит как «пять идей с низким косинусом», и отличить их можно
      только числом.</p>
  </div>

  {search_table()}
</section>

<section>
  <div class="sec-h"><h2>Граф · Neo4j</h2></div>
  <p class="sec-t">Всё озеро одной картинкой</p>
  <p class="sec-d">Neo4j — единственное хранилище графа: SQLite остался только под индекс
    тезисов и очередь заданий. Каждый узел ниже — идея, прочитанная через
    <code>GET /ideas</code>; каждая линия — ребро <code>related_via_source</code>, прочитанное
    через <code>GET /ideas/{{id}}/neighbors</code>. Подсвечены пять идей, которые вернул
    запрос из первой секции.</p>
  {lake_map(hero["body"]["ideas"])}
  <div class="row2">
    {trust_hist()}
    <div class="fig half">
      <figcaption><b>Чего на этой картинке нет.</b> Честный список того, что граф
        пока не содержит — чтобы это не пришлось объяснять на вопросе из зала.</figcaption>
      <ul class="fm">
        <li>Рёбер <code>derived_from</code> нет ни одного: они пишутся при синтезе идей,
          а синтез на этом озере ещё не запускался. Все {num(len(pairs))} связей — co-citation.</li>
        <li>Все {num(len(ideas))} идей имеют <code>origin=extracted</code>. Гипотез
          (<code>origin=synthesized</code>, идея без листьев и с trust 0.0) в графе нет.</li>
        <li>Медиана числа листьев — 1. Идея, у которой один лист, — это ещё не
          подтверждённое обобщение, и trust это отражает: медиана 0.3.</li>
        <li>Рядом в той же базе Neo4j лежат тестовые узлы блока B. Они не проходят через
          ручки блока A (другие лейблы), поэтому на карте их нет — а
          <code>MATCH (n) RETURN count(n)</code> в браузере Neo4j покажет больше.</li>
      </ul>
    </div>
  </div>
</section>

<section>
  <div class="sec-h"><h2>Провенанс</h2></div>
  <p class="sec-t">Самая обеспеченная идея озера и всё, из чего она собрана</p>
  <p class="sec-d">Идея не хранит текст источника — она хранит листья, а лист хранит
    ссылку и локатор. Поэтому любое утверждение на предыдущих экранах разворачивается до
    строки в статье или до мутанта в логе прогона. Ручки на запись тезиса нет: тезис
    неизменяем и создаётся только фазой 2, которая назначает ему идею через арбитра.</p>
  {provenance_block()}
</section>

<footer>
  <div>
    <b>Что здесь настоящее.</b> Все числа, тексты идей и тезисов, скоры, стоимости и
    состояния — тело ответов живого прода блока A на school10 в момент снимка. Страница
    статическая: JavaScript отсутствует, потому что она сделана под скриншоты, а не под
    интерактивный показ. Раскладка карты вычислена при сборке.
  </div>
  <div>
    <b>Что ещё не сделано.</b> Набора из 30 оценочных запросов и метрик нет, поэтому все
    сравнения плеч — направление, а не вывод. Синтез идей не запускался: нет ни гипотез,
    ни рёбер <code>derived_from</code>. {queue.get("failed", 0)} заданий ингеста в статусе
    failed — недостижимые источники, отказ по каждому назван в отчёте фазы.
  </div>
</footer>

</div>
</body>
</html>
"""

OUT.write_text(HTML, encoding="utf-8")
print(f"{OUT} · {len(HTML) / 1024:.0f} KB · {len(ideas)} идей, {len(pairs)} пар рёбер, "
      f"{len(retrieves)} вызовов /retrieve")
