"""System map — knowledge/12-decisions-meetings.md:118-124, 06-proposal-design.md:452-456

Круг целиком, от эволюции: GigaEvo → логи → агент эволюции формулирует
вопрос → агент озера ходит в озеро и в веб → ответ идеями обратно в прогон.
Статьи входят в озеро, веб — через агента озера.
"""

from manim import *

from theme import *

LAKE_C = LEFT * 4.4 + DOWN * 0.4
LAKE_R = 1.85
INNER = [
    UP * 0.85,
    RIGHT * 1.0 + UP * 0.05,
    LEFT * 0.8 + DOWN * 0.55,
    RIGHT * 0.2 + DOWN * 1.05,
]
INNER_EDGES = [(0, 1), (0, 2), (2, 3), (1, 3)]
PAPER_C = LEFT * 6.3 + UP * 2.6
WEB_C = LEFT * 1.9 + UP * 3.05
AGENT_C = RIGHT * 0.2 + UP * 1.4
EVO_C = RIGHT * 4.3 + UP * 1.4
GIGA_C = RIGHT * 4.2 + DOWN * 2.1
DOWN_X = 3.6  # полоса «идеи в прогон»
UP_X = 4.8  # полоса «логи из прогона»
ASK_Y = 1.75  # вопрос идёт вправо-налево над полосой ответа
ANS_Y = 1.05


class SystemMap(PipelineScene):
    def construct(self):
        # --- прогон: с него всё начинается -------------------------------------
        giga = box(2.6, 2.0).move_to(GIGA_C)
        wheel = rotor(0.6).move_to(GIGA_C)
        self.note("GigaEvo", giga, DOWN, buff=0.26, color=INK, size=21)
        self.play(Create(giga), Create(wheel), run_time=1.0)
        self.play(
            Rotate(wheel, TAU, about_point=GIGA_C, rate_func=linear), run_time=1.2
        )

        # --- логи поднимаются к агенту эволюции ----------------------------------
        evo = robot(1.5).move_to(EVO_C)
        up = Arrow(
            RIGHT * UP_X + DOWN * 1.05,
            RIGHT * UP_X + UP * 0.6,
            buff=0.05,
            stroke_width=2.4,
        ).set_color(LOG)
        self.note("run logs", up, RIGHT, buff=0.2, color=LOG)
        self.play(GrowArrow(up), run_time=0.7)
        logs = VGroup(*[runlog(0.3).move_to(up.get_start()) for _ in range(3)])
        self.play(
            LaggedStart(
                *[l.animate.move_to(up.get_end()).set_opacity(0) for l in logs],
                lag_ratio=0.25,
            ),
            run_time=1.4,
        )
        self.remove(logs)
        self.note("evolution agent", evo, UP, buff=0.26, color=INK, size=21)
        self.play(Create(evo), run_time=1.0)
        self.wait(0.3)

        # --- вопрос агенту озера -------------------------------------------------
        agent = robot(1.5).move_to(AGENT_C)
        ask = Arrow(
            RIGHT * 3.7 + UP * ASK_Y,
            RIGHT * 0.85 + UP * ASK_Y,
            buff=0.05,
            stroke_width=2.4,
        ).set_color(DIM)
        self.note("questions", ask, UP, buff=0.2, color=DIM)
        self.play(GrowArrow(ask), run_time=0.8)
        self.note("lake agent", agent, UP, buff=0.26, color=INK, size=21)
        self.play(Create(agent), run_time=1.0)
        self.wait(0.3)

        # --- озеро и статьи ------------------------------------------------------
        rim = dot_ring(LAKE_C, LAKE_R, 44, IDEA, 0.035)
        nodes = VGroup(*[idea(0.32).move_to(LAKE_C + p) for p in INNER])
        edges = VGroup(
            *[
                span(nodes[a], nodes[b], 0.36).set_stroke(FAINT, width=2)
                for a, b in INNER_EDGES
            ]
        )
        leaves = VGroup(*[thesis(0.16).move_to(n.get_center()) for n in nodes[:3]])
        edges.set_z_index(-2)
        leaves.set_z_index(1)
        self.note("idea lake", rim, DOWN, buff=0.26, color=IDEA, size=22)
        self.play(
            LaggedStart(*[FadeIn(d, scale=0.6) for d in rim], lag_ratio=0.02),
            run_time=1.0,
        )
        self.play(
            LaggedStart(*[Create(n) for n in nodes], lag_ratio=0.12),
            LaggedStart(*[Create(e) for e in edges], lag_ratio=0.12),
            FadeIn(leaves),
            run_time=1.1,
        )

        paper = doc(0.85, 1.15, 5).move_to(PAPER_C)
        feed = Arrow(
            paper.get_bottom(),
            LAKE_C + normalize(PAPER_C - LAKE_C) * LAKE_R,
            buff=0.16,
            stroke_width=2.2,
        ).set_color(DIM)
        self.note("papers", paper, RIGHT, buff=0.22, color=INK)
        self.play(FadeIn(paper), GrowArrow(feed), run_time=0.9)

        # Разговор с озером двусторонний: запрос вниз, идеи наверх.
        talk = DoubleArrow(
            LAKE_C + rotate_vector(RIGHT * LAKE_R, PI / 4.4),
            agent.get_left(),
            buff=0.16,
            stroke_width=2.4,
            tip_length=0.2,
        ).set_color(IDEA)
        self.note("retrieve", talk, DOWN, buff=0.2, color=IDEA)
        self.play(GrowFromCenter(talk), run_time=0.9)
        self.wait(0.3)

        # --- веб висит на агенте озера, а не на озере -----------------------------
        web = globe(0.45).move_to(WEB_C)
        surf = DoubleArrow(
            web.get_right(),
            agent.get_corner(UL) + UP * 0.1,
            buff=0.14,
            stroke_width=2.4,
            tip_length=0.2,
        ).set_color(DIM)
        self.note("web search", web, LEFT, buff=0.24, color=INK)
        self.play(Create(web), GrowFromCenter(surf), run_time=1.0)
        self.wait(0.4)

        # --- ответ идеями обратно в прогон -----------------------------------------
        back = Arrow(
            RIGHT * 0.85 + UP * ANS_Y,
            RIGHT * 3.7 + UP * ANS_Y,
            buff=0.05,
            stroke_width=2.4,
        ).set_color(IDEA)
        self.note("ideas", back, DOWN, buff=0.2, color=IDEA)
        self.play(GrowArrow(back), run_time=0.8)

        cargo = VGroup(*[idea(0.19).move_to(back.get_start()) for _ in range(2)])
        self.play(
            LaggedStart(
                *[c.animate.move_to(EVO_C) for c in cargo], lag_ratio=0.3
            ),
            run_time=1.1,
        )
        down = Arrow(
            RIGHT * DOWN_X + UP * 0.6,
            RIGHT * DOWN_X + DOWN * 1.05,
            buff=0.05,
            stroke_width=2.4,
        ).set_color(IDEA)
        self.play(GrowArrow(down), run_time=0.6)
        self.play(
            cargo.animate.move_to(GIGA_C).set_opacity(0),
            Rotate(wheel, TAU, about_point=GIGA_C, rate_func=linear),
            run_time=1.3,
        )
        self.remove(cargo)

        # --- круг замкнулся ---------------------------------------------------------
        # Indicate, а не set_stroke: он сам возвращает цвет, иначе рёбра
        # остались бы чернильными и потеряли смысл.
        loop = [up, ask, talk, surf, back, down]
        self.play(
            LaggedStart(
                *[Indicate(a, color=INK, scale_factor=1.0) for a in loop],
                lag_ratio=0.45,
            ),
            run_time=2.6,
        )

        # --- и последнее: логи прогона ложатся в озеро ----------------------------
        # Круг замыкается не на агенте, а на источнике: то, что нашла эволюция,
        # становится тезисами и остаётся в озере.
        home = ArcBetweenPoints(
            giga.get_left() + LEFT * 0.12,
            LAKE_C + rotate_vector(RIGHT * LAKE_R, -PI / 4),
            angle=PI / 7,
        ).set_stroke(LOG, width=2.4)
        home.add_tip(tip_length=0.22, tip_width=0.18)
        self.note("logs into the lake", home, DOWN, buff=0.24, color=LOG)
        self.play(Create(home), run_time=0.9)
        back = VGroup(*[runlog(0.3).move_to(home.get_start()) for _ in range(3)])
        self.play(
            LaggedStart(*[MoveAlongPath(l, home) for l in back], lag_ratio=0.25),
            run_time=1.8,
        )
        self.play(FadeOut(back), run_time=0.4)
        self.wait(1.2)
