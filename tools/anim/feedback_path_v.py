"""Feedback path, вертикальный кадр 800×850 — то же, что в feedback_path.py.

Озеро занимает верх кадра, прогон — низ: идеи уходят вниз в GigaEvo, логи
поднимаются обратно. В вертикали это честнее горизонтальной схемы: круг
виден как круг.
"""

from manim import *

from theme import *

portrait()

LAKE = [
    LEFT * 1.7 + UP * 3.0,
    RIGHT * 1.6 + UP * 2.3,
    LEFT * 1.9 + UP * 1.0,
    RIGHT * 1.3 + UP * 0.3,
]
LAKE_EDGES = [(0, 1), (0, 2), (1, 3), (2, 3)]
FED = [0, 1, 2]  # что ушло в прогон; четвёртая идея не подавалась
HOT = [0, 1]  # рёбра между поданными — их подкрепляем
NEW_EDGE = (1, 2)  # со-встретились в одном прогоне, ребра между ними не было
LEAVES = [3, 1, 2, 2]  # сколько тезисов уже лежит в каждой идее
HALO_R = 0.78  # радиус пунктира пере-вывода; схлопывается до радиуса идеи
TRUST = [(0, 0.86, IDEA), (1, 0.61, IDEA), (2, 0.22, BAD)]
RUN_POS = DOWN * 2.45


class FeedbackPathV(PipelineScene):
    def construct(self):
        # --- озеро уже есть -----------------------------------------------------
        nodes = VGroup(*[idea(0.52).move_to(p) for p in LAKE])
        edges = VGroup()
        for a, b in LAKE_EDGES:
            edges.add(span(nodes[a], nodes[b], 0.58).set_stroke(FAINT, width=2))
        edges.set_z_index(-2)
        nodes.set_z_index(0)

        leaves = VGroup()
        for n, k in zip(nodes, LEAVES):
            g = VGroup(*[thesis(0.16) for _ in range(k)])
            g.arrange(RIGHT, buff=0.06).move_to(n.get_center() + DOWN * 0.17)
            leaves.add(g)
        leaves.set_z_index(1)

        self.add(edges, nodes, leaves)
        lake = self.note("lake", nodes, UP, buff=0.3, color=IDEA, size=21)
        self.wait(0.6)

        # --- прогон: подача, вращение, логи ---------------------------------------
        run = box(2.5, 2.1).move_to(RUN_POS)
        name = label("GigaEvo", 21, INK, MEDIUM).next_to(run, DOWN, buff=0.24)
        wheel = rotor(0.58).move_to(RUN_POS)

        fed = VGroup(*[nodes[i].copy() for i in FED])
        self.add(fed)
        # Подписи по бокам коробки: сверху над ней летят сначала идеи,
        # потом логи, и подпись оказывается прямо на их пути.
        fd = self.note(
            "ideas from\nretrieve", run, RIGHT, buff=0.28, color=IDEA, size=18
        )
        self.play(Create(run), FadeIn(name), run_time=0.9)
        self.play(Create(wheel), run_time=0.6)
        self.play(
            LaggedStart(
                *[c.animate.scale(0.3).move_to(RUN_POS).set_opacity(0) for c in fed],
                lag_ratio=0.25,
            ),
            Rotate(wheel, TAU, about_point=RUN_POS, rate_func=linear),
            run_time=1.6,
        )
        self.remove(fed)

        # логи выходят из коробки и поднимаются к идеям
        logs = VGroup(runlog(0.44), runlog(0.44), runlog(0.44, BAD))
        for l in logs:
            l.move_to(run.get_top()).scale(0.4).set_opacity(0)
        drops = [LEFT * 1.1, ORIGIN, RIGHT * 1.1]
        self.play(FadeOut(fd), run_time=0.2)
        self._notes.remove(fd)
        self.note("logs from\nevolution", run, LEFT, buff=0.28, color=LOG, size=18)
        self.play(
            LaggedStart(
                *[
                    l.animate.scale(2.5)
                    .set_opacity(1)
                    .move_to(run.get_top() + UP * 0.75 + d)
                    for l, d in zip(logs, drops)
                ],
                lag_ratio=0.35,
            ),
            Rotate(wheel, TAU, about_point=RUN_POS, rate_func=linear),
            run_time=1.8,
        )
        self.wait(0.4)
        self.drop_notes(lake)

        # --- логи ложатся на идеи как есть, треугольниками -------------------------
        # Прогон кончился — коробка уходит, иначе шумит до титров.
        logs.set_z_index(1)
        self.play(
            LaggedStart(
                *[
                    l.animate.scale(0.62).move_to(nodes[i].get_center() + UP * 0.22)
                    for i, l in zip(FED, logs)
                ],
                lag_ratio=0.3,
            ),
            FadeOut(run, wheel, name),
            run_time=1.8,
        )
        self.wait(0.5)

        # --- рёбра: подкрепляем только между поданными идеями -----------------------
        a, b = NEW_EDGE
        newborn = span(nodes[a], nodes[b], 0.58).set_stroke(EDGE, width=4)
        newborn.set_z_index(-2)
        self.note("edges reinforced", nodes, DOWN, buff=1.0, color=DIM)
        self.play(
            LaggedStart(
                *[edges[i].animate.set_stroke(EDGE, width=5.5) for i in HOT],
                lag_ratio=0.3,
            ),
            run_time=1.2,
        )
        self.wait(0.4)
        self.drop_notes(lake)

        # Снизу: слева от нового ребра стоит узел, на который подпись садится.
        self.note("new edge", newborn, DOWN, buff=0.5, color=DIM)
        self.play(Create(newborn), run_time=0.9)
        self.wait(0.4)
        self.drop_notes(lake)

        # --- пере-вывод затронутых идей ----------------------------------------------
        halos = VGroup(
            *[
                DashedVMobject(
                    Circle(radius=HALO_R).move_to(nodes[i].get_center()),
                    num_dashes=18,
                )
                .set_fill(opacity=0)
                .set_stroke(IDEA, width=2.4)
                for i in FED
            ]
        )
        self.note("ideas re-derived", halos, DOWN, buff=0.9, color=IDEA)
        self.play(
            LaggedStart(*[Create(h) for h in halos], lag_ratio=0.2), run_time=1.0
        )
        self.play(
            *[
                Rotate(h, TAU / 3, about_point=h.get_center(), rate_func=linear)
                for h in halos
            ],
            run_time=1.3,
        )
        # Пунктир стягивается до контура идеи и садится на него.
        self.play(
            *[h.animate.scale(0.52 / HALO_R) for h in halos],
            *[nodes[i].animate.set_stroke(width=STROKE * 2) for i in FED],
            run_time=0.9,
        )
        self.play(
            FadeOut(halos),
            *[nodes[i].animate.set_stroke(width=STROKE) for i in FED],
            run_time=0.4,
        )
        self.drop_notes(lake)

        # --- trust пересчитан ----------------------------------------------------------
        self.note("trust recomputed", nodes, DOWN, buff=1.0, color=IDEA)
        for i, frac, color in TRUST:
            ring = tick_ring(nodes[i].get_center(), 0.62, frac, color, length=0.13)
            side = LEFT if i != 1 else RIGHT
            val = label(f"{frac:.2f}", 17, color, MEDIUM).next_to(
                nodes[i], side, buff=0.42
            )
            self.play(
                LaggedStart(*[Create(t) for t in ring], lag_ratio=0.04),
                FadeIn(val),
                run_time=0.9,
            )
        self.wait(1.2)
