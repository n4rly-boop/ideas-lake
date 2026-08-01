"""Agent lake, вертикальный кадр 800×850: агентский поиск поверх озера.

Озеро сверху, агент под ним, веб сбоку, выдача внизу строкой.

Ролик идёт по коду, а не по `12-decisions-meetings.md:118` («из веба в
эволюцию — только через озеро»): встреча 4 требовала ждать переваривания,
код отвечает сразу и кладёт найденное в озеро параллельно —
`lake/api/routes.py:246,261`, `lake/research/agent.py:330-365`.
"""

from manim import *

from theme import *

portrait()

LAKE_C = UP * 2.1
LAKE_R = 1.55
INNER = [UP * 0.65, RIGHT * 0.75 + DOWN * 0.25, LEFT * 0.6 + DOWN * 0.65]
INNER_EDGES = [(0, 1), (0, 2)]
AGENT_C = DOWN * 1.0
WEB_C = LEFT * 2.6 + DOWN * 1.0
DOCS = [RIGHT * 2.55 + DOWN * 0.55, RIGHT * 2.55 + DOWN * 1.8]
SLOTS = [RIGHT * (1.75 - 1.2 * i) + DOWN * 3.5 for i in range(4)]


class AgentLake(PipelineScene):
    def construct(self):
        # --- озеро уже есть -------------------------------------------------------
        rim = dot_ring(LAKE_C, LAKE_R, 40, IDEA, 0.032)
        nodes = VGroup(*[idea(0.3).move_to(LAKE_C + p) for p in INNER])
        edges = VGroup(
            *[
                span(nodes[a], nodes[b], 0.34).set_stroke(FAINT, width=2)
                for a, b in INNER_EDGES
            ]
        )
        leaves = VGroup(*[thesis(0.15).move_to(n.get_center()) for n in nodes[:2]])
        edges.set_z_index(-2)
        leaves.set_z_index(1)
        self.add(rim, edges, nodes, leaves)
        lake = self.note("lake", rim, UP, buff=0.2, color=IDEA, size=21)
        self.wait(0.5)

        agent = robot(1.4).move_to(AGENT_C)
        # Агент — главный герой ролика: его имя висит до конца.
        # Имя под роботом: справа встают найденные страницы.
        who = self.note("lake agent", agent, DOWN, buff=0.26, color=INK, size=20)
        self.play(Create(agent), run_time=0.8)
        self.wait(0.3)
        self.drop_notes(lake, who)

        # --- первый ретрив: пришло мало --------------------------------------------
        ask = Arrow(
            agent.get_top(), rim.get_bottom(), buff=0.12, stroke_width=2.4
        ).set_color(DIM)
        self.note("retrieve", ask, LEFT, buff=0.24, color=DIM)
        self.play(GrowArrow(ask), run_time=0.6)
        got = self.lift(nodes[:2], SLOTS[:2])
        self.wait(0.3)
        self.drop_notes(lake, who)

        few = cross(0.6).next_to(got, LEFT, buff=0.4)
        self.note("not enough", few, UP, buff=0.24, color=BAD, size=18)
        self.play(FadeIn(few), run_time=0.5)
        self.wait(0.4)
        self.drop_notes(lake, who)

        # --- веб --------------------------------------------------------------------
        web = globe(0.5).move_to(WEB_C)
        out = Arrow(
            agent.get_left(), web.get_right(), buff=0.14, stroke_width=2.4
        ).set_color(DIM)
        self.note("search the web", web, UP, buff=0.24, color=INK, size=18)
        self.play(FadeOut(few), GrowArrow(out), Create(web), run_time=1.0)

        pages = VGroup(*[doc(0.85, 1.15, 5).move_to(p) for p in DOCS])
        self.note("links found", pages, UP, buff=0.24, color=INK, size=18)
        self.play(
            LaggedStart(*[FadeIn(p, shift=DOWN * 0.25) for p in pages], lag_ratio=0.3),
            run_time=0.9,
        )
        self.wait(0.3)
        self.drop_notes(lake, who)

        # Развилка: отчёт уходит вызывающему сразу, а найденное тем же вызовом
        # встаёт в очередь ингеста. Ждать озеро незачем — ингест идёт минутами.
        # Стрелка вниз-вправо, подпись — над страницами: справа от них до
        # борта меньше единицы, там подпись не стоит.
        now = Arrow(
            pages.get_bottom(), RIGHT * 3.4 + DOWN * 3.1, buff=0.18, stroke_width=2.6
        ).set_color(IDEA)
        self.note(
            "to evolution\nright away", pages, UP, buff=0.26, color=IDEA, size=17
        )
        shot = pages.copy()
        self.add(shot)
        self.play(GrowArrow(now), run_time=0.5)
        self.play(
            shot.animate.scale(0.5).move_to(RIGHT * 3.9 + DOWN * 3.5).set_opacity(0),
            run_time=0.8,
        )
        self.remove(shot)
        self.drop_notes(lake, who)

        # --- и та же находка ложится в озеро — на будущее ------------------------------
        # Снизу, а не слева: слева от страниц стоит агент.
        self.note("and into the lake", pages, DOWN, buff=0.26, color=THESIS, size=18)
        self.play(
            pages.animate.scale(0.5).move_to(LAKE_C).set_opacity(0),
            FadeOut(out, web, now),
            run_time=1.2,
        )
        self.remove(pages)
        self.drop_notes(lake, who)

        # тезисы страницы собираются в новую идею — write path в одну строку
        spot = LAKE_C + RIGHT * 0.7 + UP * 0.5
        squares = VGroup(*[thesis(0.15) for _ in range(3)])
        squares.arrange(RIGHT, buff=0.08).move_to(spot)
        fresh = idea(0.3).move_to(spot).set_z_index(-1)
        self.note("ingest, minutes", rim, LEFT, buff=0.24, color=THESIS, size=18)
        self.play(FadeIn(squares, scale=0.6), run_time=0.6)
        self.play(
            squares.animate.scale(0.75).move_to(spot), Create(fresh), run_time=0.9
        )
        self.wait(0.3)
        self.drop_notes(lake, who)

        # --- следующий вопрос: найденное уже лежит в озере -------------------------------
        self.note("next question", ask, LEFT, buff=0.24, color=DIM, size=18)
        self.play(Flash(ask.get_center(), color=DIM, line_length=0.12), run_time=0.5)
        more = self.lift([fresh, nodes[2]], SLOTS[2:])
        self.wait(0.3)
        self.drop_notes(lake, who)

        full = VGroup(got, more)
        ok = check(0.6).next_to(full, LEFT, buff=0.4)
        self.note("reused,\nenough", ok, UP, buff=0.24, color=IDEA, size=18)
        self.play(FadeIn(ok), run_time=0.5)
        self.wait(0.5)
        self.drop_notes(lake, who)

        away = Arrow(
            full.get_bottom() + DOWN * 0.12, DOWN * 4.1, buff=0.05, stroke_width=2.6
        ).set_color(IDEA)
        self.note("to evolution", away, LEFT, buff=0.26, color=IDEA, size=18)
        # Галочка уходит раньше стрелки: вместе они спорят за одно место.
        self.play(FadeOut(ok), run_time=0.3)
        self.play(GrowArrow(away), run_time=0.6)
        self.wait(1.1)

    def lift(self, src, slots):
        """Идеи из озера встают строкой под агентом."""
        got = VGroup(*[s.copy() for s in src])
        self.add(got)
        self.play(
            LaggedStart(
                *[
                    c.animate.scale(0.38 / 0.3).move_to(p)
                    for c, p in zip(got, slots)
                ],
                lag_ratio=0.3,
            ),
            run_time=1.2,
        )
        return got
