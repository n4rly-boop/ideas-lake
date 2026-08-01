"""Palette, fonts and mobject factories in the AIRI Summer 2026 template style.

Light background, Inter, thin outlines, no fills and no glow. Recurring motifs
from the template: outlined circles, dotted rings, fine radial tick rings.

Shapes are fixed across all three scenes: square = thesis, circle = idea,
triangle = run log.
"""

import contextlib
from pathlib import Path

from manim import *

# Inter лежит рядом, а не в системе. register_font — контекстный менеджер;
# держим его открытым на всё время процесса, иначе шрифт пропадёт до рендера.
_FONTS = contextlib.ExitStack()
for _f in ("Inter-Regular.ttf", "Inter-Medium.ttf", "Inter-SemiBold.ttf"):
    _FONTS.enter_context(register_font(str(Path(__file__).parent / "fonts" / _f)))

FONT = "Inter"

BG = "#FFFFFF"
INK = "#1A1A1A"
DIM = "#8A8A8A"

THESIS = "#4F52E0"  # квадрат — тезис
IDEA = "#12B394"  # круг — идея
LOG = "#F5B335"  # треугольник — лог прогона
BAD = "#E0655F"  # отрицательное
EDGE = "#C79A7A"
FAINT = "#D6D2CC"

STROKE = 2.2


def label(text, size=20, color=INK, weight=NORMAL):
    return Text(text, font=FONT, font_size=size, color=color, weight=weight)


def thesis(size=0.78, color=THESIS):
    return (
        Square(side_length=size)
        .set_fill(BG, opacity=1)
        .set_stroke(color, width=STROKE)
        .round_corners(0.07)
    )


def idea(radius=0.80, color=IDEA):
    return Circle(radius=radius).set_fill(BG, opacity=1).set_stroke(color, width=STROKE)


def runlog(size=0.62, color=LOG):
    t = Triangle().set_fill(BG, opacity=1).set_stroke(color, width=STROKE)
    t.set_height(size)
    return t


def dot_ring(center, radius, n=16, color=IDEA, dot_radius=0.045):
    """Кольцо из точек — мотив шаблона; здесь: связка тезисов в батче."""
    return VGroup(
        *[
            Dot(
                center + rotate_vector(RIGHT * radius, i * TAU / n),
                radius=dot_radius,
                color=color,
            )
            for i in range(n)
        ]
    )


def tick_ring(center, radius, frac=1.0, color=IDEA, total=44, length=0.16):
    """Кольцо радиальных штрихов; заполнено на frac — так показан trust_score."""
    n = max(1, round(total * frac))
    ticks = VGroup()
    for i in range(n):
        a = PI / 2 - i * TAU / total
        d = rotate_vector(RIGHT, a)
        ticks.add(
            Line(center + d * radius, center + d * (radius + length)).set_stroke(
                color, width=1.8
            )
        )
    return ticks


def doc(width=2.4, height=3.3, lines=8):
    """Источник: контур страницы с текстовыми строками."""
    page = (
        RoundedRectangle(width=width, height=height, corner_radius=0.06)
        .set_fill(BG, opacity=1)
        .set_stroke(INK, width=1.6)
    )
    rows = VGroup()
    for i in range(lines):
        w = width * (0.66 if i % 3 == 2 else 0.78)
        rows.add(Line(LEFT * w / 2, RIGHT * w / 2).set_stroke(FAINT, width=2))
    rows.arrange(DOWN, buff=height / (lines + 3)).move_to(page)
    return VGroup(page, rows)


class PipelineScene(Scene):
    """Светлый фон и один короткий бейдж шага слева сверху.

    Больше текста в кадре нет: пояснения живут на слайде, не в ролике.
    """

    BADGE_ANCHOR = UP * 3.45 + LEFT * 6.4

    def setup(self):
        self.camera.background_color = BG
        self._badge = None
        self._rule = None

    def step(self, text, accent=IDEA):
        """Сменить бейдж шага.

        Слово меняется на месте, без сдвигов — текст не «едет». Гасим и
        зажигаем последовательно и быстро: одновременно две разные строки
        в одной точке дают призрак, а долгая пауза — заметный разрыв.
        Линейка под словом не гаснет вовсе, только перекрашивается.
        """
        cap = label(text, 26, INK, MEDIUM).move_to(
            self.BADGE_ANCHOR, aligned_edge=LEFT
        )
        if self._badge is None:
            self._rule = Line(LEFT * 0.55, RIGHT * 0.55).set_stroke(accent, width=3)
            self._rule.move_to(self.BADGE_ANCHOR + DOWN * 0.42, aligned_edge=LEFT)
            self.play(FadeIn(cap), FadeIn(self._rule), run_time=0.4)
        else:
            self.play(
                FadeOut(self._badge),
                self._rule.animate.set_stroke(accent),
                run_time=0.2,
            )
            self.play(FadeIn(cap), run_time=0.25)
        self._badge = cap
