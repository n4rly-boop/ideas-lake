"""Read path — knowledge/06-proposal-design.md:454

запрос → индекс тезисов → подъём к идеям → dedup → ранжирование
→ при нехватке обход рёбер → выдача идея + листья + trust
"""

from manim import *

from theme import *

QUERY_POS = LEFT * 5.8
GRID_POS = LEFT * 2.8
LIFT_X = -0.4  # колонка поднятых тезисов
IDEA_X = 2.3
CARD_POS = RIGHT * 5.4

HITS = [3, 6, 9, 12, 17]  # какие ячейки индекса попали в выдачу
SCORES = ["0.91", "0.84", "0.72", "0.66", "0.51"]
OWNER = [0, 0, 1, 2, 2]  # тезисы 0-1 и 3-4 ведут к одной идее — это dedup
RANKS = ["0.88", "0.71", "0.55"]
NEIGHBOUR_RANK = "0.41"

SLOTS = [UP * 1.71, UP * 0.57, DOWN * 0.57, DOWN * 1.71]  # 4 места в выдаче


class ReadPath(PipelineScene):
    def construct(self):
        # --- запрос от эволюции ----------------------------------------------
        self.step("запрос", THESIS)
        # Пустой прямоугольник читался как «какая-то карточка»; подписываем.
        chip = (
            RoundedRectangle(width=2.1, height=1.1, corner_radius=0.1)
            .set_fill(BG, opacity=1)
            .set_stroke(INK, width=1.6)
            .move_to(QUERY_POS)
        )
        qtext = label("query", 24, INK, MEDIUM).move_to(chip)
        self.play(Create(chip), run_time=0.8)
        self.play(FadeIn(qtext), run_time=0.5)
        self.wait(0.4)

        # --- индекс тезисов: BM25 + cosine -------------------------------------
        self.step("индекс тезисов", THESIS)
        cells = VGroup(*[thesis(0.44, FAINT) for _ in range(20)])
        cells.arrange_in_grid(rows=4, cols=5, buff=0.26).move_to(GRID_POS)
        self.play(
            LaggedStart(*[FadeIn(c, scale=0.8) for c in cells], lag_ratio=0.04),
            run_time=1.2,
        )

        scan = Line(cells.get_top(), cells.get_bottom()).set_stroke(THESIS, width=2.4)
        scan.next_to(cells, LEFT, buff=0.15)
        self.play(FadeIn(scan), run_time=0.2)
        self.play(scan.animate.next_to(cells, RIGHT, buff=0.15), run_time=1.3)
        self.play(FadeOut(scan), run_time=0.2)

        scores = VGroup(
            *[
                label(s, 14, THESIS).next_to(cells[i], UP, buff=0.08)
                for i, s in zip(HITS, SCORES)
            ]
        )
        self.play(
            LaggedStart(
                *[
                    AnimationGroup(
                        cells[i].animate.set_stroke(THESIS, width=STROKE), FadeIn(s)
                    )
                    for i, s in zip(HITS, scores)
                ],
                lag_ratio=0.22,
            ),
            run_time=1.5,
        )
        self.wait(0.4)

        # --- подъём: попавшие тезисы выходят из индекса отдельной колонкой ------
        # Так стрелки к идеям не проходят поверх остальных ячеек.
        self.step("тезис → идея", IDEA)
        column = [RIGHT * LIFT_X + UP * (1.4 - 0.7 * i) for i in range(5)]
        rest = VGroup(*[c for i, c in enumerate(cells) if i not in HITS])
        self.play(
            LaggedStart(
                *[cells[h].animate.move_to(spot) for h, spot in zip(HITS, column)],
                lag_ratio=0.12,
            ),
            FadeOut(scores),
            rest.animate.set_stroke(opacity=0.3),
            run_time=1.6,
        )
        self.wait(0.3)

        ideas = VGroup(*[idea(0.42).move_to(s + RIGHT * IDEA_X) for s in SLOTS])
        born = set()
        beats = []
        arrows = []
        for h, o in zip(HITS, OWNER):
            arrow = Arrow(
                cells[h].get_right(),
                ideas[o].get_left(),
                buff=0.16,
                stroke_width=2,
                max_tip_length_to_length_ratio=0.06,
            ).set_color(FAINT)
            arrows.append(arrow)
            parts = [GrowArrow(arrow)]
            if o not in born:
                born.add(o)
                parts.append(Create(ideas[o]))
            beats.append(AnimationGroup(*parts, lag_ratio=0.3))
        self.play(LaggedStart(*beats, lag_ratio=0.45), run_time=2.6)
        self.wait(0.4)

        # dedup: пять тезисов дали три идеи — в первую и третью пришло по два.
        # Подсвечиваем стрелки, а не обводку круга: обводка занята trust,
        # и утолщение круга здесь читалось бы как «эта идея релевантнее».
        dup = VGroup(*[arrows[i] for i in (0, 1, 3, 4)])
        self.play(dup.animate.set_stroke(THESIS, width=3.4), run_time=0.8)
        self.wait(0.5)
        self.play(dup.animate.set_stroke(FAINT, width=2), run_time=0.6)

        # --- ранжирование --------------------------------------------------------
        self.step("ранжирование", IDEA)
        vals = VGroup(
            *[
                label(r, 17, INK).next_to(ideas[i], RIGHT, buff=0.52)
                for i, r in enumerate(RANKS)
            ]
        )
        self.play(LaggedStart(*[FadeIn(v) for v in vals], lag_ratio=0.25), run_time=1.3)
        self.wait(0.4)

        # --- идей мало → обход рёбер; найденный сосед остаётся в выдаче ------------
        self.step("обход рёбер", EDGE)
        far = idea(0.42).move_to(ideas[1].get_center() + RIGHT * 2.4 + DOWN * 0.9)
        link = Line(ideas[1].get_center(), far.get_center()).set_stroke(EDGE, width=2)
        link.set_z_index(-2)
        self.play(Create(link), run_time=0.9)
        self.play(Create(far), run_time=0.7)
        self.wait(0.3)

        # Сосед встаёт в конец ранжирования, ребро тянется за ним. Дуга, а не
        # прямая: прямая от второй идеи к четвёртой прошла бы сквозь третью.
        slot = SLOTS[3] + RIGHT * IDEA_X
        docked = ArcBetweenPoints(
            ideas[1].get_center(), slot, angle=-PI / 1.5
        ).set_stroke(EDGE, width=4)
        docked.set_z_index(-2)
        self.play(
            far.animate.move_to(slot),
            Transform(link, docked),
            run_time=1.2,
        )
        self.play(
            FadeIn(label(NEIGHBOUR_RANK, 17, INK).next_to(far, RIGHT, buff=0.52)),
            run_time=0.5,
        )
        self.wait(0.4)

        # --- выдача: верхняя идея + её листья + trust -------------------------------
        self.step("выдача", IDEA)
        card = (
            RoundedRectangle(width=2.9, height=3.5, corner_radius=0.1)
            .set_fill(BG, opacity=1)
            .set_stroke(INK, width=1.6)
            .move_to(CARD_POS)
        )
        head = idea(0.4)
        # У идеи листьев больше, чем нашлось в индексе: выдаём все.
        leaves = VGroup(*[thesis(0.34) for _ in range(6)])
        leaves.arrange_in_grid(rows=2, cols=3, buff=0.22)
        VGroup(head, leaves).arrange(DOWN, buff=0.6).move_to(card).shift(UP * 0.15)
        ring = tick_ring(head.get_center(), 0.52, 0.88, IDEA, length=0.12)
        val = label("trust  0.88", 17, IDEA, MEDIUM).next_to(leaves, DOWN, buff=0.34)

        self.play(Create(card), run_time=0.9)
        self.play(ReplacementTransform(ideas[0].copy(), head), run_time=0.9)
        self.play(LaggedStart(*[Create(l) for l in leaves], lag_ratio=0.2), run_time=1.2)
        self.play(
            LaggedStart(*[Create(t) for t in ring], lag_ratio=0.04),
            FadeIn(val),
            run_time=1.3,
        )
        self.wait(1.6)
