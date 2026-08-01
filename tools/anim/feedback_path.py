"""Feedback path — knowledge/06-proposal-design.md:456

прогон → логи → подкрепление набора рёбер → пере-вывод → trust
"""

from manim import *

from theme import *

FEED_POS = LEFT * 6.4
RUN_POS = LEFT * 4.2
LAKE = [
    RIGHT * 2.2 + UP * 1.5,
    RIGHT * 4.6 + UP * 0.5,
    RIGHT * 2.5 + DOWN * 1.2,
    RIGHT * 4.8 + DOWN * 2.3,
]
LAKE_EDGES = [(0, 1), (0, 2), (1, 3), (2, 3)]
FED = [0, 1, 2]  # что ушло в прогон; четвёртая идея не подавалась
HOT = [0, 1]  # рёбра между поданными — их подкрепляем
NEW_EDGE = (1, 2)  # со-встретились в одном прогоне, ребра между ними не было
LEAVES = [3, 1, 2, 2]  # сколько тезисов уже лежит в каждой идее
TRUST = [(0, 0.86, IDEA), (1, 0.61, IDEA), (2, 0.22, BAD)]


class FeedbackPath(PipelineScene):
    def construct(self):
        # --- озеро уже есть: рисуем сразу, анимировать нечего ------------------
        nodes = VGroup(*[idea(0.62).move_to(p) for p in LAKE])
        edges = VGroup()
        for a, b in LAKE_EDGES:
            e = Line(nodes[a].get_center(), nodes[b].get_center()).set_stroke(
                FAINT, width=2
            )
            edges.add(e)
        # Явный порядок слоёв: ребро проходит под кругом, лист — над.
        # Без этого утолщённое ребро наезжает на узел.
        edges.set_z_index(-2)
        nodes.set_z_index(0)

        # тезисы, уже собранные в идеях — по-разному в разных
        leaves = VGroup()
        for n, k in zip(nodes, LEAVES):
            g = VGroup(*[thesis(0.19) for _ in range(k)])
            g.arrange(RIGHT, buff=0.07).move_to(n.get_center() + DOWN * 0.2)
            leaves.add(g)
        leaves.set_z_index(1)

        self.add(edges, nodes, leaves)  # граф уже есть до начала ролика
        self.step("озеро", IDEA)
        self.wait(0.8)

        # --- прогон: подача, вращение, логи — одним куском ----------------------
        self.step("прогон", IDEA)
        fed = VGroup(*[idea(0.42) for _ in range(3)])
        fed.arrange(DOWN, buff=0.5).move_to(FEED_POS)

        box = (
            RoundedRectangle(width=2.9, height=2.8, corner_radius=0.1)
            .set_fill(BG, opacity=1)
            .set_stroke(INK, width=1.6)
            .move_to(RUN_POS)
        )
        name = label("GigaEvo", 22, INK, MEDIUM).next_to(box, DOWN, buff=0.3)
        # колесо со спицами: крутится — значит, идёт прогон
        rotor = VGroup(
            Circle(radius=0.66).set_fill(opacity=0).set_stroke(IDEA, width=2.2),
            *[
                Line(ORIGIN, RIGHT * 0.66)
                .rotate(i * TAU / 6, about_point=ORIGIN)
                .set_stroke(IDEA, width=2)
                for i in range(6)
            ],
        ).move_to(box.get_center())

        self.play(
            AnimationGroup(*[Create(c) for c in fed]),  # все три сразу
            Create(box),
            FadeIn(name),
            run_time=1.1,
        )
        self.play(Create(rotor), run_time=0.7)
        self.play(
            LaggedStart(
                *[
                    c.animate.scale(0.25).move_to(box.get_center()).set_opacity(0)
                    for c in fed
                ],
                lag_ratio=0.25,
            ),
            Rotate(rotor, TAU, about_point=box.get_center(), rate_func=linear),
            run_time=1.7,
        )

        # логи выходят справа от коробки: два удачных прогона и один провальный
        logs = VGroup(runlog(), runlog(), runlog(0.62, BAD))
        drops = [UP * 1.6, ORIGIN, DOWN * 1.6]
        for l in logs:
            l.move_to(box.get_right()).scale(0.4)
        self.play(
            LaggedStart(
                *[
                    Succession(
                        Create(l),
                        l.animate.scale(2.5).move_to(box.get_right() + RIGHT * 1.1 + d),
                    )
                    for l, d in zip(logs, drops)
                ],
                lag_ratio=0.4,
            ),
            Rotate(rotor, TAU, about_point=box.get_center(), rate_func=linear),
            run_time=2.3,
        )
        self.wait(0.5)

        # --- логи ложатся на идеи как есть, треугольниками ----------------------
        # Прогон кончился — колесо и коробка уходят, иначе шумят до титров.
        self.step("логи в озеро", LOG)
        logs.set_z_index(1)  # над кругом, рядом с уже лежащими тезисами
        self.play(
            LaggedStart(
                *[
                    l.animate.scale(0.48).move_to(nodes[i].get_center() + UP * 0.26)
                    for i, l in zip(FED, logs)
                ],
                lag_ratio=0.3,
            ),
            FadeOut(box, rotor, name),
            run_time=1.9,
        )
        self.wait(0.6)

        # --- рёбра: подкрепляем только между поданными идеями ---------------------
        # У четвёртой идеи логов нет — её рёбра не трогаем.
        self.step("рёбра", EDGE)
        a, b = NEW_EDGE
        born = Line(nodes[a].get_center(), nodes[b].get_center()).set_stroke(
            EDGE, width=4
        )
        born.set_z_index(-2)
        self.play(
            LaggedStart(
                *[edges[i].animate.set_stroke(EDGE, width=5.5) for i in HOT],
                lag_ratio=0.3,
            ),
            run_time=1.3,
        )
        # со-встречаемость в одном прогоне — новое ребро там, где его не было
        self.play(Create(born), run_time=1.0)
        self.wait(0.6)

        # --- пере-вывод затронутых идей -------------------------------------------
        self.step("переоценка", IDEA)
        halos = VGroup(
            *[
                DashedVMobject(
                    Circle(radius=0.95).move_to(nodes[i].get_center()), num_dashes=20
                )
                .set_fill(opacity=0)
                .set_stroke(IDEA, width=2.4)
                for i in FED
            ]
        )
        self.play(
            LaggedStart(*[Create(h) for h in halos], lag_ratio=0.2), run_time=1.1
        )
        self.play(
            *[
                Rotate(h, TAU / 3, about_point=h.get_center(), rate_func=linear)
                for h in halos
            ],
            run_time=1.6,
        )
        self.play(FadeOut(halos), run_time=0.5)

        # --- trust пересчитан ------------------------------------------------------
        self.step("trust", IDEA)
        for i, frac, color in TRUST:
            ring = tick_ring(nodes[i].get_center(), 0.72, frac, color)
            side = LEFT if i != 1 else UP
            val = label(f"{frac:.2f}", 17, color, MEDIUM).next_to(
                nodes[i], side, buff=0.45
            )
            self.play(
                LaggedStart(*[Create(t) for t in ring], lag_ratio=0.04),
                FadeIn(val),
                run_time=1.0,
            )
        self.wait(1.4)
