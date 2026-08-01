"""Agent lake — lake/api/routes.py:246, lake/research/agent.py:192

агентский RAG: ретрив → «хватило?» → веб-поиск → развилка. Отчёт уходит
вызывающему сразу (`routes.py:261`), а найденные статьи тем же вызовом
встают в очередь ингеста (`research/agent.py:330-365`) — и находятся
следующим вопросом.

Ролик идёт по коду, а не по `12-decisions-meetings.md:118` («из веба в
эволюцию — только через озеро»): встреча 4 требовала ждать переваривания,
код отвечает сразу и кладёт в озеро параллельно.
"""

from manim import *

from theme import *

LAKE_C = LEFT * 4.4 + DOWN * 0.4
LAKE_R = 1.95
INNER = [UP * 0.85, RIGHT * 0.95 + DOWN * 0.3, LEFT * 0.75 + DOWN * 0.85]
INNER_EDGES = [(0, 1), (0, 2)]
AGENT_C = RIGHT * 0.3 + DOWN * 0.4
WEB_C = RIGHT * 0.3 + UP * 2.7
DOCS = [RIGHT * 2.8 + UP * 2.5, RIGHT * 4.5 + UP * 2.5]
SLOTS = [RIGHT * 3.6 + UP * y for y in (1.35, 0.35, -0.65, -1.65)]


class AgentLake(PipelineScene):
    def construct(self):
        # --- озеро уже есть ---------------------------------------------------
        rim = dot_ring(LAKE_C, LAKE_R, 44, IDEA, 0.035)
        nodes = VGroup(*[idea(0.34).move_to(LAKE_C + p) for p in INNER])
        edges = VGroup(
            *[
                span(nodes[a], nodes[b], 0.38).set_stroke(FAINT, width=2)
                for a, b in INNER_EDGES
            ]
        )
        leaves = VGroup(*[thesis(0.17).move_to(n.get_center()) for n in nodes[:2]])
        edges.set_z_index(-2)
        leaves.set_z_index(1)
        self.add(rim, edges, nodes, leaves)
        lake = self.note("lake", rim, UP, buff=0.26, color=IDEA, size=21)
        self.wait(0.5)

        agent = box(2.4, 1.4).move_to(AGENT_C)
        # Коробка — главный герой ролика: её имя висит до конца.
        who = self.note("lake agent", agent, DOWN, buff=0.28, color=INK, size=21)
        self.play(Create(agent), run_time=0.8)
        self.wait(0.3)
        self.drop_notes(lake, who)

        # --- первый ретрив: пришло мало ---------------------------------------
        ask = Arrow(
            agent.get_left(), rim.get_right(), buff=0.2, stroke_width=2.4
        ).set_color(DIM)
        # Подпись поднята выше стрелки: она шире промежутка и на малом
        # отступе заезжает на коробку агента.
        self.note("retrieve", ask, UP, buff=0.55, color=DIM)
        self.play(GrowArrow(ask), run_time=0.7)
        got = self.lift(nodes[:2], SLOTS[:2])
        self.wait(0.4)
        self.drop_notes(lake, who)

        few = cross(0.7).next_to(got, RIGHT, buff=0.5)
        self.note("not enough", few, UP, buff=0.26, color=BAD, shift=RIGHT * 0.35)
        self.play(FadeIn(few), run_time=0.5)
        self.wait(0.5)
        self.drop_notes(lake, who)

        # --- веб: агент ищет сам, но кладёт найденное в озеро -------------------
        web = globe(0.6).move_to(WEB_C)
        out = Arrow(
            agent.get_top(), web.get_bottom(), buff=0.18, stroke_width=2.4
        ).set_color(DIM)
        self.note("search the web", web, LEFT, buff=0.32, color=INK)
        self.play(FadeOut(few), GrowArrow(out), Create(web), run_time=1.1)

        pages = VGroup(*[doc(0.8, 1.1, 5).move_to(p) for p in DOCS])
        self.note("links found", pages, UP, buff=0.26, color=INK)
        self.play(
            LaggedStart(*[FadeIn(p, shift=RIGHT * 0.3) for p in pages], lag_ratio=0.3),
            run_time=1.0,
        )
        self.wait(0.3)
        self.drop_notes(lake, who)

        # Развилка: ответ уходит в эволюцию сразу (`/research` синтезирует отчёт
        # и возвращает его вызывающему, `api/routes.py:246-261`), а найденное
        # тем же вызовом ставится в очередь ингеста (`research/agent.py:330-365`).
        # Ждать озеро незачем: ингест идёт минутами, ответ нужен сейчас.
        now = Arrow(
            pages.get_right(), RIGHT * 6.9 + UP * 2.5, buff=0.2, stroke_width=2.6
        ).set_color(IDEA)
        self.note(
            "to evolution\nright away",
            now,
            DOWN,
            buff=0.24,
            color=IDEA,
            size=17,
            shift=RIGHT * 0.45,
        )
        shot = pages.copy()
        self.add(shot)
        self.play(GrowArrow(now), run_time=0.6)
        self.play(
            shot.animate.move_to(RIGHT * 7.6 + UP * 2.5).set_opacity(0), run_time=0.9
        )
        self.remove(shot)
        self.drop_notes(lake, who)

        # --- и та же находка ложится в озеро — на будущее ------------------------
        # Подпись сверху: слева стоит глобус, снизу страницы улетают в озеро.
        self.note("and into the lake", pages, UP, buff=0.28, color=THESIS)
        self.play(
            pages.animate.scale(0.5).move_to(LAKE_C + UP * 0.1).set_opacity(0),
            FadeOut(out, web, now),
            run_time=1.3,
        )
        self.remove(pages)
        self.drop_notes(lake, who)

        # тезисы страницы собираются в новую идею — write path в одну строку.
        # Место свободное: над центром уже стоит узел, туда класть нельзя.
        spot = LAKE_C + LEFT * 1.0 + UP * 0.35
        squares = VGroup(*[thesis(0.17) for _ in range(3)])
        squares.arrange(RIGHT, buff=0.1).move_to(spot)
        fresh = idea(0.34).move_to(spot).set_z_index(-1)
        self.note("ingest, minutes", rim, DOWN, buff=0.26, color=THESIS)
        self.play(FadeIn(squares, scale=0.6), run_time=0.7)
        self.play(
            squares.animate.scale(0.75).move_to(spot),
            Create(fresh),
            run_time=1.0,
        )
        self.wait(0.4)
        self.drop_notes(lake, who)

        # --- следующий вопрос: найденное уже лежит в озере ------------------------
        # В две строки: в один ряд подпись шире промежутка между озером и
        # агентом и садится либо на пунктир, либо на коробку.
        self.note("next\nquestion", ask, UP, buff=0.35, color=DIM)
        self.play(Flash(ask.get_center(), color=DIM, line_length=0.12), run_time=0.5)
        more = self.lift([fresh, nodes[2]], SLOTS[2:])
        self.wait(0.4)
        self.drop_notes(lake, who)

        full = VGroup(got, more)
        ok = check(0.7).next_to(full, RIGHT, buff=0.5)
        self.note("reused, enough", ok, UP, buff=0.26, color=IDEA)
        self.play(FadeIn(ok), run_time=0.5)
        self.wait(0.5)
        self.drop_notes(lake, who)

        away = Arrow(
            full.get_right() + RIGHT * 0.3,
            RIGHT * 6.8,
            buff=0.1,
            stroke_width=2.6,
        ).set_color(IDEA)
        self.note("to evolution", away, UP, buff=0.2, color=IDEA)
        self.play(FadeOut(ok), GrowArrow(away), run_time=0.8)
        self.wait(1.2)

    def lift(self, src, slots):
        """Идеи из озера встают колонкой справа от агента."""
        got = VGroup(*[s.copy() for s in src])
        self.add(got)
        self.play(
            LaggedStart(
                *[
                    c.animate.scale(0.42 / 0.34).move_to(p)
                    for c, p in zip(got, slots)
                ],
                lag_ratio=0.3,
            ),
            run_time=1.3,
        )
        return got
