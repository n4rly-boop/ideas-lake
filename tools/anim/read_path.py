"""Read path, вертикальный кадр 800×850: запрос → выдача на чтении.

Поток развёрнут сверху вниз: запрос, индекс тезисов, поднятые тезисы,
идеи-родители, выдача. Сосед, пришедший по ребру, встаёт под строкой идей,
а не рядом: четвёртый круг с числом в ширину кадра не влезает.
"""

from manim import *

from theme import *

portrait()

QUERY_POS = UP * 3.05
GRID_POS = UP * 1.4
LIFT_Y = 0.1
# Три идеи строкой, сосед по ребру — под ними: в строку из четырёх
# круги с подписями в 8 единиц ширины не встают.
# Сосед смещён вправо, а не строго под центральной идеей: вертикальное
# ребро проходило ровно по её числу.
SLOTS = [
    LEFT * 2.0 + DOWN * 1.55,
    DOWN * 1.55,
    RIGHT * 2.0 + DOWN * 1.55,
    RIGHT * 1.35 + DOWN * 3.15,
]

HITS = [2, 5, 9, 13]  # какие ячейки индекса попали в выдачу
SCORES = ["0.91", "0.84", "0.72", "0.66"]
OWNER = [0, 0, 1, 2]  # первые два тезиса ведут к одной идее — это dedup
RANKS = ["0.88", "0.71", "0.55"]
NEIGHBOUR_RANK = "0.41"


class ReadPath(PipelineScene):
    def construct(self):
        # --- запрос от эволюции ------------------------------------------------
        chip = box(2.0, 0.9).move_to(QUERY_POS)
        qtext = label("query", 24, INK, MEDIUM).move_to(chip)
        self.note("what evolution needs?", chip, UP, buff=0.26, color=INK, size=18)
        self.play(Create(chip), run_time=0.7)
        self.play(FadeIn(qtext), run_time=0.4)
        self.wait(0.3)

        # --- индекс тезисов -----------------------------------------------------
        # Сетка мельче исходной: на 0.4 с отступом 0.24 она достаёт до
        # чипа запроса, а поднять чип некуда — он и так у борта.
        cells = VGroup(*[thesis(0.36, FAINT) for _ in range(16)])
        cells.arrange_in_grid(rows=4, cols=4, buff=0.2).move_to(GRID_POS)
        idx = self.note("theses index", cells, LEFT, buff=0.3, color=THESIS, size=19)
        self.play(
            LaggedStart(*[FadeIn(c, scale=0.8) for c in cells], lag_ratio=0.04),
            run_time=1.1,
        )

        self.note("bm25+cosine", cells, RIGHT, buff=0.3, color=DIM, size=19)
        scan = Line(cells.get_left(), cells.get_right()).set_stroke(THESIS, width=2.4)
        scan.next_to(cells, UP, buff=0.12)
        self.play(FadeIn(scan), run_time=0.2)
        self.play(scan.animate.next_to(cells, DOWN, buff=0.12), run_time=1.2)
        self.play(FadeOut(scan), run_time=0.2)

        scores = VGroup(
            *[
                label(s, 14, THESIS).next_to(cells[i], UP, buff=0.06)
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
            run_time=1.3,
        )
        self.wait(0.4)
        self.drop_notes(idx)

        # --- найденные тезисы выходят из индекса строкой --------------------------
        # Слева направо: обратный порядок разводил бы стрелки крест-накрест.
        row = [LEFT * (1.65 - 1.1 * i) + UP * LIFT_Y for i in range(4)]
        rest = VGroup(*[c for i, c in enumerate(cells) if i not in HITS])
        self.play(
            LaggedStart(
                *[cells[h].animate.move_to(spot) for h, spot in zip(HITS, row)],
                lag_ratio=0.12,
            ),
            FadeOut(scores),
            rest.animate.set_stroke(opacity=0.25),
            run_time=1.4,
        )
        self.wait(0.3)
        self.drop_notes()

        # --- родительские идеи ---------------------------------------------------
        ideas = VGroup(*[idea(0.44).move_to(s) for s in SLOTS])
        born = set()
        beats = []
        arrows = []
        for h, o in zip(HITS, OWNER):
            # Наконечник фиксированный: при ratio он считается от длины, и
            # у диагональной стрелки оказывается вдвое крупнее соседних.
            arrow = Arrow(
                cells[h].get_bottom(),
                ideas[o].get_top(),
                buff=0.14,
                stroke_width=2,
                tip_length=0.16,
                max_tip_length_to_length_ratio=1.0,
                max_stroke_width_to_length_ratio=99,
            ).set_color(FAINT)
            arrows.append(arrow)
            parts = [GrowArrow(arrow)]
            if o not in born:
                born.add(o)
                parts.append(Create(ideas[o]))
            beats.append(AnimationGroup(*parts, lag_ratio=0.3))
        # В две строки: в одну подпись у левого борта обрезается.
        self.note("parent\nideas", ideas[0], LEFT, buff=0.3, color=IDEA, size=19)
        self.play(LaggedStart(*beats, lag_ratio=0.45), run_time=2.3)
        self.wait(0.3)
        self.drop_notes()

        # dedup: два тезиса привели в одну идею. Подсвечиваем стрелки, а не
        # обводку круга: обводка занята trust.
        dup = VGroup(arrows[0], arrows[1])
        dd = self.note("dedup by idea", dup, RIGHT, buff=0.24, color=THESIS, size=19)
        self.play(dup.animate.set_stroke(THESIS, width=3.4), run_time=0.7)
        self.wait(0.4)
        self.play(dup.animate.set_stroke(FAINT, width=2), FadeOut(dd), run_time=0.5)
        self._notes.remove(dd)

        # --- ранжирование ---------------------------------------------------------
        vals = VGroup(
            *[
                label(r, 17, INK).next_to(ideas[i], DOWN, buff=0.22)
                for i, r in enumerate(RANKS)
            ]
        )
        self.note("score", vals[0], LEFT, buff=0.24, color=DIM, size=17)
        self.play(LaggedStart(*[FadeIn(v) for v in vals], lag_ratio=0.25), run_time=1.1)
        self.wait(0.3)
        self.drop_notes()

        # --- идей мало → обход рёбер ------------------------------------------------
        far = ideas[3]
        link = span(ideas[1], far, 0.48).set_stroke(EDGE, width=3)
        link.set_z_index(-2)
        self.note("too few: add neighbours", far, DOWN, buff=0.28, color=EDGE, size=18)
        self.play(Create(link), run_time=0.8)
        self.play(Create(far), run_time=0.6)
        nval = label(NEIGHBOUR_RANK, 17, INK).next_to(far, RIGHT, buff=0.28)
        self.play(FadeIn(nval), run_time=0.5)
        self.wait(0.4)

        # --- выдача: верхняя идея, её листья и trust -----------------------------------
        self.drop_notes()
        card = box(3.0, 3.6).move_to(DOWN * 0.2)
        head = idea(0.42)
        # У идеи листьев больше, чем нашлось в индексе: выдаём все.
        leaves = VGroup(*[thesis(0.36) for _ in range(6)])
        leaves.arrange_in_grid(rows=2, cols=3, buff=0.24)
        VGroup(head, leaves).arrange(DOWN, buff=0.6).move_to(card).shift(UP * 0.15)
        ring = tick_ring(head.get_center(), 0.54, 0.88, IDEA, length=0.12)
        val = label("trust  0.88", 17, IDEA, MEDIUM).next_to(leaves, DOWN, buff=0.34)

        self.note("result", card, UP, buff=0.28, color=INK, size=20)
        self.play(
            FadeOut(cells, ideas, VGroup(*arrows), link, vals, nval),
            Create(card),
            run_time=1.1,
        )
        self.play(Create(head), run_time=0.7)
        self.play(LaggedStart(*[Create(l) for l in leaves], lag_ratio=0.2), run_time=1.1)
        self.play(
            LaggedStart(*[Create(t) for t in ring], lag_ratio=0.04),
            FadeIn(val),
            run_time=1.2,
        )
        # Хвост длиннее прочих пауз: на финальном кадре гифка стоит, пока
        # о нём говорят, иначе цикл начинается заново посреди фразы.
        self.wait(2.8)
