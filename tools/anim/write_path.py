"""Write path — knowledge/06-proposal-design.md:452

источник → 1c парсер → 1d обобщение → 2a digest → 2b-1 линковка/создание
→ 2b-3 рёбра → 2b-4 trust → Neo4j
"""

from manim import *

from theme import *

DOC_POS = LEFT * 5.3
GRID_POS = LEFT * 1.85
PACK_A = GRID_POS + UP * 1.55
PACK_B = GRID_POS + DOWN * 1.65
IDEA_A = RIGHT * 2.5 + UP * 1.5
IDEA_B = RIGHT * 2.5 + DOWN * 1.6


def rosette(center, radius=0.78):
    """Три позиции по кругу — так тезисы собираются в батч и в идею."""
    return [center + rotate_vector(UP * radius, i * TAU / 3) for i in range(3)]


class WritePath(PipelineScene):
    def construct(self):
        # --- 1c parser: документ разбирается на тезисы ----------------------
        self.step("1c  парсер", THESIS)
        page = doc().move_to(DOC_POS)
        self.play(Create(page[0]), run_time=0.9)
        self.play(
            LaggedStart(*[Create(r) for r in page[1]], lag_ratio=0.12), run_time=1.1
        )
        self.wait(0.6)

        theses = VGroup(*[thesis() for _ in range(6)])
        for t in theses:
            t.move_to(page.get_center()).scale(0.2).set_opacity(0)
        grid = VGroup(*[thesis() for _ in range(6)])
        grid.arrange_in_grid(rows=3, cols=2, buff=0.9).move_to(GRID_POS)

        self.play(
            LaggedStart(
                *[
                    t.animate.scale(5).set_opacity(1).move_to(g.get_center())
                    for t, g in zip(theses, grid)
                ],
                lag_ratio=0.22,
            ),
            run_time=2.2,
        )
        self.wait(0.6)

        # --- 1d generalisation: обобщённый текст поверх исходного -----------
        self.step("1d  обобщение", THESIS)
        cores = VGroup(
            *[
                Square(side_length=0.32)
                .move_to(t)
                .set_stroke(FAINT, width=1.8)
                .set_fill(BG, opacity=0)
                for t in theses
            ]
        )
        self.play(
            LaggedStart(*[Create(c) for c in cores], lag_ratio=0.15), run_time=1.7
        )
        self.wait(0.7)

        # --- 2a digest: батч тезисов одного источника ------------------------
        self.step("2a  digest", THESIS)
        pack_a = VGroup(theses[0], theses[2], theses[4])
        pack_b = VGroup(theses[1], theses[3], theses[5])
        core_a = VGroup(cores[0], cores[2], cores[4])
        core_b = VGroup(cores[1], cores[3], cores[5])

        moves = []
        for pack, core, spots in (
            (pack_a, core_a, rosette(PACK_A)),
            (pack_b, core_b, rosette(PACK_B)),
        ):
            for t, c, spot in zip(pack, core, spots):
                moves.append(VGroup(t, c).animate.move_to(spot))
        self.play(LaggedStart(*moves, lag_ratio=0.14), run_time=2.0)

        rings = VGroup(
            dot_ring(PACK_A, 1.5, 22, THESIS), dot_ring(PACK_B, 1.5, 22, THESIS)
        )
        self.play(
            LaggedStart(
                *[FadeIn(d, scale=0.85) for ring in rings for d in ring], lag_ratio=0.03
            ),
            run_time=1.4,
        )
        self.wait(0.5)

        # --- 2b-1 linking: найдена идея или создаётся новая -------------------
        # Оба батча линкуются в одном проходе, а не по очереди: решение
        # принимается для каждого независимо. A садится на найденную идею,
        # B подходящей не находит — под него заводится новая.
        self.step("2b-1  линковка", IDEA)
        # z_index ниже тезисов: у идеи белая заливка, и без этого пришедшие
        # позже тезисы прячутся под кругом.
        known = idea().move_to(IDEA_A).set_z_index(-1)
        fresh = idea().move_to(IDEA_B).set_z_index(-1)
        self.play(FadeOut(rings), Create(known), Create(fresh), run_time=1.0)
        self.wait(0.5)

        spots_a = rosette(known.get_center(), 0.36)
        spots_b = rosette(fresh.get_center(), 0.36)
        self.play(
            LaggedStart(
                *[
                    VGroup(t, c).animate.scale(0.45).move_to(s)
                    for t, c, s in zip(pack_a, core_a, spots_a)
                ],
                lag_ratio=0.25,
            ),
            LaggedStart(
                *[
                    VGroup(t, c).animate.scale(0.45).move_to(s)
                    for t, c, s in zip(pack_b, core_b, spots_b)
                ],
                lag_ratio=0.25,
            ),
            run_time=2.4,
        )
        self.play(known.animate.scale(1.14), run_time=0.6)
        self.wait(0.5)

        # --- 2b-3 edges ------------------------------------------------------
        self.step("2b-3  рёбра", EDGE)
        edge = Line(known.get_bottom(), fresh.get_top()).set_stroke(FAINT, width=2)
        edge.set_z_index(-2)
        self.play(Create(edge), run_time=1.0)
        self.play(edge.animate.set_stroke(EDGE, width=6), run_time=0.8)
        self.wait(0.6)

        # --- 2b-4 trust: заполненность кольца = trust_score --------------------
        self.step("2b-4  trust", IDEA)
        ring_a = tick_ring(known.get_center(), known.width / 2 + 0.1, 0.78, IDEA)
        ring_b = tick_ring(fresh.get_center(), fresh.width / 2 + 0.1, 0.34, BAD)
        val_a = label("0.78", 19, IDEA, MEDIUM).next_to(known, RIGHT, buff=0.5)
        val_b = label("0.34", 19, BAD, MEDIUM).next_to(fresh, RIGHT, buff=0.5)
        self.play(
            LaggedStart(*[Create(t) for t in ring_a], lag_ratio=0.04),
            FadeIn(val_a),
            run_time=1.4,
        )
        self.play(
            LaggedStart(*[Create(t) for t in ring_b], lag_ratio=0.04),
            FadeIn(val_b),
            run_time=1.1,
        )
        self.wait(0.8)

        # --- запись в граф (блок B) --------------------------------------------
        # Граф не исчезает в хранилище, а оказывается внутри его рамки:
        # так видно, что записано именно то, что собрали.
        self.step("запись в граф", THESIS)
        graph = VGroup(known, fresh, edge, ring_a, ring_b, theses, cores)
        self.play(
            FadeOut(page), FadeOut(val_a), FadeOut(val_b),
            graph.animate.scale(0.62).move_to(DOWN * 0.25),
            run_time=1.4,
        )
        box = SurroundingRectangle(graph, buff=0.7, corner_radius=0.1).set_stroke(
            THESIS, width=STROKE
        )
        name = label("Neo4j", 22, THESIS, MEDIUM).next_to(box, UP, buff=0.28)
        self.play(Create(box), run_time=1.0)
        self.play(FadeIn(name), run_time=0.5)
        self.wait(1.4)
