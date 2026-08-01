"""System map, полоса 850×280 — тот же круг, что в system_map.py, но в строку.

Не масштабированный 16:9: в пропорции 3:1 вертикали почти нет, поэтому
раскладка своя — источник, озеро, оба агента и прогон стоят в ряд, а логи
возвращаются дугой под ними.
"""

from manim import *

from theme import *

# Кадр задаётся здесь, а не флагом -r: присваивание идёт после разбора
# командной строки, поэтому переживает -qh.
config.pixel_width = 1700  # вдвое крупнее цели: 850 получается ужатием
config.pixel_height = 560
config.frame_height = 4.68
config.frame_width = 14.22

PAPER_C = LEFT * 6.75
LAKE_C = LEFT * 4.85
LAKE_R = 1.2
INNER = [UP * 0.5, RIGHT * 0.62 + DOWN * 0.12, LEFT * 0.5 + DOWN * 0.45]
INNER_EDGES = [(0, 1), (0, 2)]
AGENT_C = LEFT * 1.6
WEB_C = LEFT * 3.3 + UP * 1.7
EVO_C = RIGHT * 1.5
GIGA_C = RIGHT * 4.55
LANE = 0.0  # всё стоит на одной оси: полоса низкая, этажей нет


class SystemMapWide(PipelineScene):
    def construct(self):
        # --- озеро ---------------------------------------------------------
        rim = dot_ring(LAKE_C, LAKE_R, 40, IDEA, 0.03)
        nodes = VGroup(*[idea(0.24).move_to(LAKE_C + p) for p in INNER])
        edges = VGroup(
            *[
                span(nodes[a], nodes[b], 0.28).set_stroke(FAINT, width=1.8)
                for a, b in INNER_EDGES
            ]
        )
        leaves = VGroup(*[thesis(0.13).move_to(n.get_center()) for n in nodes[:2]])
        edges.set_z_index(-2)
        leaves.set_z_index(1)
        self.note("idea lake", rim, UP, buff=0.16, color=IDEA, size=18)
        self.play(
            LaggedStart(*[FadeIn(d, scale=0.6) for d in rim], lag_ratio=0.02),
            run_time=0.9,
        )
        self.play(
            LaggedStart(*[Create(n) for n in nodes], lag_ratio=0.12),
            LaggedStart(*[Create(e) for e in edges], lag_ratio=0.12),
            FadeIn(leaves),
            run_time=0.9,
        )

        # --- статьи входят в озеро -------------------------------------------
        paper = doc(0.5, 0.68, 4).move_to(PAPER_C)
        feed = Arrow(
            paper.get_right(), LAKE_C + LEFT * LAKE_R, buff=0.12, stroke_width=2
        ).set_color(DIM)
        self.note("papers", paper, UP, buff=0.16, color=INK, size=17, shift=RIGHT * 0.3)
        self.play(FadeIn(paper), GrowArrow(feed), run_time=0.7)

        # Имена узлов — сверху, отношения — снизу: нижняя треть полосы
        # оставлена дуге возврата, иначе она идёт по подписям.
        # --- агент озера, веб на нём -------------------------------------------
        agent = robot(1.2).move_to(AGENT_C)
        talk = DoubleArrow(
            LAKE_C + RIGHT * LAKE_R,
            agent.get_left(),
            buff=0.14,
            stroke_width=2.2,
            tip_length=0.16,
        ).set_color(IDEA)
        self.note("lake agent", agent, UP, buff=0.18, color=INK, size=18)
        self.note("retrieve", talk, DOWN, buff=0.14, color=IDEA, size=17)
        self.play(Create(agent), GrowFromCenter(talk), run_time=0.9)

        web = globe(0.33).move_to(WEB_C)
        surf = DoubleArrow(
            web.get_center(),
            agent.get_corner(UL),
            buff=0.12,
            stroke_width=2.2,
            tip_length=0.16,
        ).set_color(DIM)
        self.note("web", web, RIGHT, buff=0.2, color=INK, size=17)
        self.play(Create(web), GrowFromCenter(surf), run_time=0.7)

        # --- агент эволюции и прогон -------------------------------------------
        evo = robot(1.2).move_to(EVO_C)
        pass_ = DoubleArrow(
            agent.get_right(),
            evo.get_left(),
            buff=0.14,
            stroke_width=2.2,
            tip_length=0.16,
        ).set_color(IDEA)
        self.note("evolution agent", evo, UP, buff=0.18, color=INK, size=18)
        self.note("questions", pass_, DOWN, buff=0.14, color=DIM, size=17)
        self.play(Create(evo), GrowFromCenter(pass_), run_time=0.9)

        giga = box(1.9, 1.5).move_to(GIGA_C)
        wheel = rotor(0.42).move_to(GIGA_C)
        into = Arrow(
            evo.get_right(), giga.get_left(), buff=0.14, stroke_width=2.2
        ).set_color(IDEA)
        self.note("GigaEvo", giga, UP, buff=0.18, color=INK, size=18)
        self.play(Create(giga), Create(wheel), GrowArrow(into), run_time=0.9)
        self.play(
            Rotate(wheel, TAU, about_point=GIGA_C, rate_func=linear), run_time=1.0
        )

        # --- логи возвращаются в озеро дугой под полосой -------------------------
        # От нижнего левого угла, а не от середины низа: из середины дуга
        # уходит вправо и пересекает борт коробки.
        home = ArcBetweenPoints(
            giga.get_corner(DL) + DOWN * 0.08,
            LAKE_C + rotate_vector(RIGHT * LAKE_R, -PI / 3),
            # Знак угла: при положительном дуга выгибается вверх и идёт
            # по ногам роботов, снизу же места достаточно.
            angle=-PI / 10,
        ).set_stroke(LOG, width=2.2)
        home.add_tip(tip_length=0.18, tip_width=0.15)
        self.note("run logs", home, DOWN, buff=0.14, color=LOG, size=17)
        self.play(Create(home), run_time=0.9)
        logs = VGroup(*[runlog(0.22).move_to(home.get_start()) for _ in range(3)])
        self.play(
            LaggedStart(*[MoveAlongPath(l, home) for l in logs], lag_ratio=0.25),
            run_time=1.6,
        )
        self.play(FadeOut(logs), run_time=0.3)

        # --- один оборот подсветкой ----------------------------------------------
        loop = [feed, talk, surf, pass_, into, home]
        self.play(
            LaggedStart(
                *[Indicate(a, color=INK, scale_factor=1.0) for a in loop],
                lag_ratio=0.4,
            ),
            run_time=2.2,
        )
        self.wait(1.0)
