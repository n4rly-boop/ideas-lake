"""Write path, вертикальный кадр 800×850 — тот же путь, что в write_path.py.

Не обрезка 16:9: в вертикальный кадр колонка 16:9 не влезает, срезается
источник и запись в граф. Поэтому поток развёрнут сверху вниз — статья,
тезисы, батчи, идеи, граф, — а подписи ушли к бортам.
"""

from manim import *

from theme import *

portrait()

DOC_POS = UP * 1.1
GRID_POS = UP * 1.35
PACK_L = LEFT * 1.75 + UP * 1.95
PACK_R = RIGHT * 1.75 + UP * 1.95
IDEA_L = LEFT * 1.75 + DOWN * 0.6
IDEA_R = RIGHT * 1.75 + DOWN * 0.6


def rosette(center, radius=0.72):
    """Три позиции по кругу — так тезисы собираются в батч и в идею."""
    return [center + rotate_vector(UP * radius, i * TAU / 3) for i in range(3)]


class WritePathV(PipelineScene):
    def construct(self):
        # --- 1c parser --------------------------------------------------------
        page = doc(2.0, 2.7, 7).move_to(DOC_POS)
        self.note("parse paper", page, UP, buff=0.3, color=INK, size=21)
        self.play(Create(page[0]), run_time=0.8)
        self.play(
            LaggedStart(*[Create(r) for r in page[1]], lag_ratio=0.12), run_time=1.0
        )
        self.wait(0.3)

        theses = VGroup(*[thesis(0.7) for _ in range(6)])
        for t in theses:
            t.move_to(page.get_center()).scale(0.2).set_opacity(0)
        grid = VGroup(*[thesis(0.7) for _ in range(6)])
        grid.arrange_in_grid(rows=2, cols=3, buff=0.62).move_to(GRID_POS)

        # Страница уходит: в вертикальном кадре она держит ровно то место,
        # куда встают тезисы.
        self.drop_notes()
        self.note("extract theses", grid, UP, buff=0.42, color=THESIS, size=21)
        self.play(
            LaggedStart(
                *[
                    t.animate.scale(5).set_opacity(1).move_to(g.get_center())
                    for t, g in zip(theses, grid)
                ],
                lag_ratio=0.22,
            ),
            FadeOut(page),
            run_time=2.0,
        )
        self.wait(0.3)

        # --- 1d generalisation -------------------------------------------------
        cores = VGroup(
            *[
                Square(side_length=0.3)
                .move_to(t)
                .set_stroke(FAINT, width=1.8)
                .set_fill(BG, opacity=0)
                for t in theses
            ]
        )
        self.note("generalize", grid, DOWN, buff=0.34, color=DIM)
        self.play(
            LaggedStart(*[Create(c) for c in cores], lag_ratio=0.15), run_time=1.5
        )
        self.wait(0.3)
        self.drop_notes()

        # --- 2a digest ---------------------------------------------------------
        pack_a = VGroup(theses[0], theses[2], theses[4])
        pack_b = VGroup(theses[1], theses[3], theses[5])
        core_a = VGroup(cores[0], cores[2], cores[4])
        core_b = VGroup(cores[1], cores[3], cores[5])

        moves = []
        for pack, core, spots in (
            (pack_a, core_a, rosette(PACK_L)),
            (pack_b, core_b, rosette(PACK_R)),
        ):
            for t, c, spot in zip(pack, core, spots):
                moves.append(VGroup(t, c).animate.scale(0.72).move_to(spot))
        rings = VGroup(
            dot_ring(PACK_L, 1.35, 20, THESIS), dot_ring(PACK_R, 1.35, 20, THESIS)
        )
        self.note("group by similarity", rings, UP, buff=0.22, color=THESIS)
        self.play(LaggedStart(*moves, lag_ratio=0.14), run_time=1.8)
        self.play(
            LaggedStart(
                *[FadeIn(d, scale=0.85) for ring in rings for d in ring], lag_ratio=0.03
            ),
            run_time=1.2,
        )
        self.wait(0.3)

        # --- 2b-1 linking ------------------------------------------------------
        # z_index ниже тезисов: у идеи белая заливка, и без этого пришедшие
        # позже тезисы прячутся под кругом.
        known = idea(0.78).move_to(IDEA_L).set_z_index(-1)
        fresh = idea(0.78).move_to(IDEA_R).set_z_index(-1)
        self.drop_notes()
        # Подписи по бортам: сверху и снизу от кругов места нет — там батчи
        # и кольца доверия.
        self.note("existing\nidea", known, LEFT, buff=0.3, color=IDEA, size=18)
        self.note("new\nidea", fresh, RIGHT, buff=0.3, color=IDEA, size=18)
        self.play(FadeOut(rings), Create(known), Create(fresh), run_time=1.0)

        spots_a = rosette(known.get_center(), 0.34)
        spots_b = rosette(fresh.get_center(), 0.34)
        self.play(
            LaggedStart(
                *[
                    VGroup(t, c).animate.scale(0.62).move_to(s)
                    for t, c, s in zip(pack_a, core_a, spots_a)
                ],
                lag_ratio=0.25,
            ),
            LaggedStart(
                *[
                    VGroup(t, c).animate.scale(0.62).move_to(s)
                    for t, c, s in zip(pack_b, core_b, spots_b)
                ],
                lag_ratio=0.25,
            ),
            run_time=2.2,
        )
        self.play(known.animate.scale(1.12), run_time=0.5)
        self.wait(0.3)
        self.drop_notes()

        # --- 2b-3 edges --------------------------------------------------------
        edge = span(known, fresh, 0.9).set_stroke(FAINT, width=2)
        edge.set_z_index(-2)
        # Подпись над парой, а не над ребром: ребро горизонтальное, и
        # «над ним» приходится ровно между кругами.
        self.note(
            "form edge (same source)",
            VGroup(known, fresh),
            UP,
            buff=0.34,
            color=EDGE,
            size=18,
        )
        self.play(Create(edge), run_time=0.9)
        self.play(edge.animate.set_stroke(EDGE, width=6), run_time=0.7)
        self.wait(0.3)
        self.drop_notes()

        # --- 2b-4 trust --------------------------------------------------------
        ring_a = tick_ring(known.get_center(), known.width / 2 + 0.1, 0.78, IDEA)
        ring_b = tick_ring(fresh.get_center(), fresh.width / 2 + 0.1, 0.34, BAD)
        val_a = label("0.78", 19, IDEA, MEDIUM).next_to(known, DOWN, buff=0.5)
        val_b = label("0.34", 19, BAD, MEDIUM).next_to(fresh, DOWN, buff=0.5)
        self.note("compute trust", VGroup(ring_a, ring_b), DOWN, buff=0.7, color=IDEA)
        self.play(
            LaggedStart(*[Create(t) for t in ring_a], lag_ratio=0.04),
            FadeIn(val_a),
            run_time=1.2,
        )
        self.play(
            LaggedStart(*[Create(t) for t in ring_b], lag_ratio=0.04),
            FadeIn(val_b),
            run_time=1.0,
        )
        self.wait(0.4)

        # --- запись в граф ------------------------------------------------------
        self.drop_notes()
        graph = VGroup(known, fresh, edge, ring_a, ring_b, theses, cores, val_a, val_b)
        self.play(graph.animate.scale(0.78).move_to(DOWN * 0.15), run_time=1.2)
        frame = SurroundingRectangle(graph, buff=0.55, corner_radius=0.1).set_stroke(
            THESIS, width=STROKE
        )
        self.note("ingest into Neo4j", frame, UP, buff=0.3, color=THESIS, size=21)
        self.play(Create(frame), run_time=0.9)
        self.wait(1.1)
