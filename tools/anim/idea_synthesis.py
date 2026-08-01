"""Idea synthesis — knowledge/12-decisions-meetings.md:94, 13-run-ingest-and-graph-spec.md:596

случайная пара идей → merge_classify «объединимо?» → merge_generate →
гипотеза с синтетическим листом, рёбра derived_from к родителям,
лист в тот же индекс тезисов
"""

from manim import *

from theme import *

LAKE = [
    LEFT * 5.5 + UP * 2.1,
    LEFT * 3.5 + UP * 1.1,
    LEFT * 5.9 + DOWN * 0.3,
    LEFT * 3.8 + DOWN * 1.5,
    LEFT * 5.7 + DOWN * 2.6,
]
LAKE_EDGES = [(0, 1), (0, 2), (2, 4), (1, 3)]
REJECTED = (2, 4)  # арбитр говорит «нет» — пара просто выбрасывается
ACCEPTED = (0, 3)
GATE_POS = LEFT * 0.6 + UP * 0.9
HYP_POS = RIGHT * 3.7 + UP * 0.9
INDEX_POS = RIGHT * 2.0 + DOWN * 2.7


class IdeaSynthesis(PipelineScene):
    def construct(self):
        # --- озеро уже есть ---------------------------------------------------
        nodes = VGroup(*[idea(0.5).move_to(p) for p in LAKE])
        edges = VGroup(
            *[
                span(nodes[a], nodes[b], 0.54).set_stroke(FAINT, width=2)
                for a, b in LAKE_EDGES
            ]
        )
        edges.set_z_index(-2)
        self.add(edges, nodes)
        lake = self.note("lake", nodes, UP, buff=0.3, color=IDEA, size=21)
        self.wait(0.5)

        gate = box(2.5, 1.7).move_to(GATE_POS)
        gc = self.note("combinable?", gate, DOWN, buff=0.28, color=INK, size=21)
        self.play(Create(gate), run_time=0.6)

        # --- пара 1: арбитр отказывает ----------------------------------------
        # Отбор случайный, поэтому мимо — обычный исход, а не сбой.
        pair = self.pick(nodes, REJECTED, "random pair")
        bad = cross(0.75).move_to(gate)
        self.note("no", gate, UP, buff=0.28, color=BAD, size=21)
        self.play(FadeIn(bad), run_time=0.5)
        self.wait(0.4)
        self.play(
            FadeOut(bad, pair),
            *[nodes[i].animate.set_stroke(IDEA, width=STROKE) for i in REJECTED],
            run_time=0.6,
        )
        self.drop_notes(lake, gc)

        # --- пара 2: арбитр соглашается ---------------------------------------
        # Подпись справа от пары: снизу под ней стоит пятый узел.
        pair = self.pick(nodes, ACCEPTED, "next pair", RIGHT)
        ok = check(0.75).move_to(gate)
        self.note("yes", gate, UP, buff=0.28, color=IDEA, size=21)
        self.play(FadeIn(ok), run_time=0.5)
        self.wait(0.4)
        self.drop_notes(lake, gc)

        # --- гипотеза: идея без доказательств, поэтому пунктиром ---------------
        hyp = (
            DashedVMobject(Circle(radius=0.62).move_to(HYP_POS), num_dashes=24)
            .set_fill(opacity=0)
            .set_stroke(IDEA, width=STROKE)
        )
        self.note("hypothesis", hyp, UP, buff=0.3, color=IDEA)
        self.play(
            pair.animate.move_to(HYP_POS).set_opacity(0),
            FadeOut(ok),
            Create(hyp),
            run_time=1.2,
        )
        self.remove(pair, ok)
        # Родители возвращают свой цвет: выбор кончился, они обычные идеи.
        # Коробка своё отработала: держать её до титров — шум в кадре.
        self.play(
            FadeOut(gate, gc),
            *[nodes[i].animate.set_stroke(IDEA, width=STROKE) for i in ACCEPTED],
            run_time=0.5,
        )
        self._notes.remove(gc)

        # Лист синтетический, но настоящий: без него гипотезу нечем найти.
        leaf = thesis(0.3).move_to(hyp.get_center()).set_z_index(1)
        self.note("synthetic thesis", hyp, DOWN, buff=0.3, color=THESIS)
        self.play(Create(leaf), run_time=0.7)
        self.wait(0.5)
        self.drop_notes(lake)

        # Доказательств нет — доверие нулевое, и это стоит числом рядом.
        tr = label("trust  0.00", 17, DIM, MEDIUM).next_to(hyp, DOWN, buff=0.75)
        self.play(FadeIn(tr), run_time=0.5)

        # --- derived_from: обратный адрес к родителям --------------------------
        back = VGroup(
            *[
                DashedLine(
                    hyp.get_center(), nodes[i].get_center(), buff=0.66, dash_length=0.13
                ).set_stroke(EDGE, width=2.4)
                for i in ACCEPTED
            ]
        )
        back.set_z_index(-2)
        self.note("derived_from", back, UP, buff=0.22, color=EDGE)
        self.play(
            LaggedStart(*[Create(b) for b in back], lag_ratio=0.35), run_time=1.3
        )
        self.wait(0.5)
        self.drop_notes(lake)

        # --- лист уходит в тот же индекс тезисов --------------------------------
        # Второго канала поиска нет: гипотеза находится тем же гибридом.
        cells = VGroup(*[thesis(0.34, FAINT) for _ in range(6)])
        cells.arrange(RIGHT, buff=0.22).move_to(INDEX_POS)
        self.note("same index", cells, DOWN, buff=0.28, color=THESIS)
        self.play(
            LaggedStart(*[FadeIn(c, scale=0.8) for c in cells], lag_ratio=0.08),
            run_time=0.7,
        )
        flyer = leaf.copy()
        self.add(flyer)
        self.play(
            flyer.animate.move_to(cells[3]).scale(34 / 30),
            cells[3].animate.set_stroke(THESIS, width=STROKE),
            run_time=0.9,
        )
        self.remove(flyer)
        self.wait(0.6)
        self.drop_notes(lake)

        # --- гипотеза едет в прогон вперемешку с идеями --------------------------
        # Отдельного канала нет: агент берёт её так же, как проверенные идеи.
        self.play(FadeOut(cells), run_time=0.5)
        gone = Arrow(
            hyp.get_right(), RIGHT * 6.9 + UP * 0.9, buff=0.15, stroke_width=2.4
        ).set_color(DIM)
        self.note("mixed into the run", gone, UP, buff=0.22, color=DIM)
        self.play(GrowArrow(gone), run_time=0.6)
        riders = VGroup(
            idea(0.26),
            idea(0.26),
            DashedVMobject(
                Circle(radius=0.26).set_stroke(IDEA, width=STROKE), num_dashes=14
            ),
        )
        for r in riders:
            r.move_to(hyp.get_center())
        self.add(riders)
        self.play(
            LaggedStart(
                *[
                    r.animate.move_to(gone.get_end()).set_opacity(0) for r in riders
                ],
                lag_ratio=0.25,
            ),
            run_time=1.0,
        )
        self.remove(riders)
        self.drop_notes(lake)

        # --- лог прогона садится в гипотезу — доказательство появилось -------------
        run = runlog(0.3).move_to(gone.get_end()).set_z_index(1)
        self.note("run log", gone, DOWN, buff=0.22, color=LOG)
        self.play(
            FadeOut(gone),
            run.animate.move_to(hyp.get_center() + UP * 0.22),
            leaf.animate.move_to(hyp.get_center() + DOWN * 0.2),
            run_time=1.2,
        )
        self.wait(0.4)
        self.drop_notes(lake)

        # --- пунктир становится сплошным: гипотеза дозрела до идеи ------------------
        solid = (
            Circle(radius=0.62)
            .move_to(hyp.get_center())
            .set_fill(opacity=0)
            .set_stroke(IDEA, width=STROKE)
        )
        ring = tick_ring(hyp.get_center(), 0.78, 0.55, IDEA)
        grown = label("trust  0.55", 17, IDEA, MEDIUM).move_to(tr)
        self.note("now an idea", solid, UP, buff=0.4, color=IDEA)
        self.play(
            ReplacementTransform(hyp, solid),
            LaggedStart(*[Create(t) for t in ring], lag_ratio=0.04),
            FadeTransform(tr, grown),
            run_time=1.6,
        )
        self.wait(1.1)

    def pick(self, nodes, ids, caption, direction=DOWN):
        """Пара уходит копиями в арбитра, оригиналы остаются в озере."""
        picked = VGroup(*[nodes[i] for i in ids])
        cap = self.note(caption, picked, direction, buff=0.32, color=DIM)
        self.play(
            *[n.animate.set_stroke(INK, width=STROKE * 1.4) for n in picked],
            run_time=0.5,
        )
        copies = VGroup(*[n.copy() for n in picked])
        self.add(copies)
        gate = GATE_POS
        self.play(
            *[
                c.animate.scale(0.52).move_to(gate + side * 0.45)
                for c, side in zip(copies, (LEFT, RIGHT))
            ],
            run_time=1.0,
        )
        return copies
