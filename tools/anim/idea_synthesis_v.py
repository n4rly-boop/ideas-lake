"""Idea synthesis, вертикальный кадр 800×850 — то же, что в idea_synthesis.py.

Сверху вниз: озеро, арбитр «объединимо?», гипотеза, индекс тезисов,
прогон и превращение гипотезы в идею.
"""

from manim import *

from theme import *

portrait()

LAKE = [
    LEFT * 2.4 + UP * 3.4,
    RIGHT * 0.1 + UP * 3.0,
    RIGHT * 2.5 + UP * 3.5,
    LEFT * 1.1 + UP * 2.1,
    RIGHT * 1.9 + UP * 1.9,
]
LAKE_EDGES = [(0, 1), (1, 2), (0, 3), (1, 4)]
REJECTED = (2, 4)  # арбитр говорит «нет» — пара просто выбрасывается
ACCEPTED = (0, 3)
GATE_POS = DOWN * 0.15
HYP_POS = DOWN * 2.5
INDEX_POS = DOWN * 3.9


class IdeaSynthesisV(PipelineScene):
    def construct(self):
        # --- озеро уже есть ------------------------------------------------------
        nodes = VGroup(*[idea(0.42).move_to(p) for p in LAKE])
        edges = VGroup(
            *[
                span(nodes[a], nodes[b], 0.46).set_stroke(FAINT, width=2)
                for a, b in LAKE_EDGES
            ]
        )
        edges.set_z_index(-2)
        self.add(edges, nodes)
        lake = self.note("lake", nodes, LEFT, buff=0.3, color=IDEA, size=20)
        self.wait(0.5)

        gate = box(2.4, 1.5).move_to(GATE_POS)
        gc = self.note("combinable?", gate, LEFT, buff=0.3, color=INK, size=20)
        self.play(Create(gate), run_time=0.6)

        # --- пара 1: арбитр отказывает --------------------------------------------
        # Отбор случайный, поэтому мимо — обычный исход, а не сбой.
        pair = self.pick(nodes, REJECTED, "random pair")
        bad = cross(0.7).move_to(gate)
        self.note("no", gate, RIGHT, buff=0.3, color=BAD, size=20)
        self.play(FadeIn(bad), run_time=0.5)
        self.wait(0.4)
        self.play(
            FadeOut(bad, pair),
            *[nodes[i].animate.set_stroke(IDEA, width=STROKE) for i in REJECTED],
            run_time=0.6,
        )
        self.drop_notes(lake, gc)

        # --- пара 2: арбитр соглашается --------------------------------------------
        pair = self.pick(nodes, ACCEPTED, "next pair")
        ok = check(0.7).move_to(gate)
        self.note("yes", gate, RIGHT, buff=0.3, color=IDEA, size=20)
        self.play(FadeIn(ok), run_time=0.5)
        self.wait(0.4)
        self.drop_notes(lake, gc)

        # --- гипотеза: идея без доказательств, поэтому пунктиром --------------------
        hyp = (
            DashedVMobject(Circle(radius=0.58).move_to(HYP_POS), num_dashes=22)
            .set_fill(opacity=0)
            .set_stroke(IDEA, width=STROKE)
        )
        self.note("hypothesis", hyp, LEFT, buff=0.3, color=IDEA)
        self.play(
            pair.animate.move_to(HYP_POS).set_opacity(0),
            FadeOut(ok),
            Create(hyp),
            run_time=1.1,
        )
        self.remove(pair, ok)
        # Родители возвращают свой цвет, коробка уходит: своё она отработала.
        self.play(
            FadeOut(gate, gc),
            *[nodes[i].animate.set_stroke(IDEA, width=STROKE) for i in ACCEPTED],
            run_time=0.5,
        )
        self._notes.remove(gc)

        # Лист синтетический, но настоящий: без него гипотезу нечем найти.
        leaf = thesis(0.28).move_to(hyp.get_center()).set_z_index(1)
        self.note("synthetic thesis", hyp, RIGHT, buff=0.3, color=THESIS)
        self.play(Create(leaf), run_time=0.6)
        self.wait(0.4)
        self.drop_notes(lake)

        # Доказательств нет — доверие нулевое, и это стоит числом рядом.
        tr = label("trust  0.00", 17, DIM, MEDIUM).next_to(hyp, RIGHT, buff=0.4)
        self.play(FadeIn(tr), run_time=0.4)

        # --- derived_from: обратный адрес к родителям --------------------------------
        back = VGroup(
            *[
                DashedLine(
                    hyp.get_center(), nodes[i].get_center(), buff=0.6, dash_length=0.12
                ).set_stroke(EDGE, width=2.4)
                for i in ACCEPTED
            ]
        )
        back.set_z_index(-2)
        self.note("derived_from", back, LEFT, buff=0.24, color=EDGE)
        self.play(
            LaggedStart(*[Create(b) for b in back], lag_ratio=0.35), run_time=1.2
        )
        self.wait(0.4)
        self.drop_notes(lake)

        # --- лист уходит в тот же индекс тезисов ---------------------------------------
        # Второго канала поиска нет: гипотеза находится тем же гибридом.
        cells = VGroup(*[thesis(0.3, FAINT) for _ in range(6)])
        cells.arrange(RIGHT, buff=0.2).move_to(INDEX_POS)
        self.note("same index", cells, LEFT, buff=0.3, color=THESIS, size=18)
        self.play(
            LaggedStart(*[FadeIn(c, scale=0.8) for c in cells], lag_ratio=0.08),
            run_time=0.7,
        )
        flyer = leaf.copy()
        self.add(flyer)
        self.play(
            flyer.animate.move_to(cells[3]).scale(30 / 28),
            cells[3].animate.set_stroke(THESIS, width=STROKE),
            run_time=0.9,
        )
        self.remove(flyer)
        self.wait(0.5)
        self.drop_notes(lake)

        # --- гипотеза едет в прогон вперемешку с идеями ----------------------------------
        # Отдельного канала нет: агент берёт её так же, как проверенные идеи.
        self.play(FadeOut(cells), run_time=0.4)
        gone = Arrow(
            hyp.get_bottom(), DOWN * 4.1, buff=0.14, stroke_width=2.4
        ).set_color(DIM)
        self.note("mixed into the run", gone, RIGHT, buff=0.24, color=DIM, size=18)
        self.play(GrowArrow(gone), run_time=0.5)
        riders = VGroup(
            idea(0.24),
            idea(0.24),
            DashedVMobject(
                Circle(radius=0.24).set_stroke(IDEA, width=STROKE), num_dashes=12
            ),
        )
        for r in riders:
            r.move_to(hyp.get_center())
        self.add(riders)
        self.play(
            LaggedStart(
                *[r.animate.move_to(gone.get_end()).set_opacity(0) for r in riders],
                lag_ratio=0.25,
            ),
            run_time=1.0,
        )
        self.remove(riders)
        self.drop_notes(lake)

        # --- лог прогона садится в гипотезу — доказательство появилось ---------------------
        run = runlog(0.26).move_to(gone.get_end()).set_z_index(1)
        self.note("run log", gone, LEFT, buff=0.24, color=LOG, size=18)
        self.play(
            FadeOut(gone),
            run.animate.move_to(hyp.get_center() + UP * 0.2),
            leaf.animate.move_to(hyp.get_center() + DOWN * 0.18),
            run_time=1.1,
        )
        self.wait(0.4)
        self.drop_notes(lake)

        # --- пунктир становится сплошным: гипотеза дозрела до идеи -------------------------
        solid = (
            Circle(radius=0.58)
            .move_to(hyp.get_center())
            .set_fill(opacity=0)
            .set_stroke(IDEA, width=STROKE)
        )
        ring = tick_ring(hyp.get_center(), 0.72, 0.55, IDEA, length=0.14)
        grown = label("trust  0.55", 17, IDEA, MEDIUM).move_to(tr)
        self.note("now an idea", solid, LEFT, buff=0.5, color=IDEA)
        self.play(
            ReplacementTransform(hyp, solid),
            LaggedStart(*[Create(t) for t in ring], lag_ratio=0.04),
            FadeTransform(tr, grown),
            run_time=1.5,
        )
        self.wait(1.1)

    def pick(self, nodes, ids, caption, direction=DOWN):
        """Пара уходит копиями в арбитра, оригиналы остаются в озере."""
        picked = VGroup(*[nodes[i] for i in ids])
        self.note(caption, picked, direction, buff=0.3, color=DIM, size=18)
        self.play(
            *[n.animate.set_stroke(INK, width=STROKE * 1.4) for n in picked],
            run_time=0.5,
        )
        copies = VGroup(*[n.copy() for n in picked])
        self.add(copies)
        self.play(
            *[
                c.animate.scale(0.55).move_to(GATE_POS + side * 0.42)
                for c, side in zip(copies, (LEFT, RIGHT))
            ],
            run_time=0.9,
        )
        return copies
