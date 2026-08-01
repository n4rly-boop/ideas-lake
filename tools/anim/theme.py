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


TEXT_BASE = 96  # кегль, на котором рисуем всегда


def label(text, size=20, color=INK, weight=NORMAL):
    """Текст нужного размера, отрисованный крупно и уменьшенный.

    Напрямую мелкий кегль брать нельзя: manim ставит глифы по пиксельной
    сетке, и на 19–24 pt внутри слова появляются разрывы («p arse p aper»).
    На 96 pt округление незаметно, а scale() уже не трогает расстановку.
    """
    def one(s):
        return Text(s, font=FONT, font_size=TEXT_BASE, color=color, weight=weight)

    if "\n" in text:
        # Text выравнивает строки по левому краю; собираем сами, чтобы
        # вторая строка встала по центру первой.
        lines = VGroup(*[one(s) for s in text.split("\n")])
        return lines.arrange(DOWN, buff=0.22).scale(size / TEXT_BASE)
    return one(text).scale(size / TEXT_BASE)


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
    """Светлый фон и короткие подписи у самих фигур.

    Названия шагов из угла кадра убраны: подпись стоит там, куда смотрят.
    Развёрнутый текст живёт на слайде, не в ролике.
    """

    def setup(self):
        self.camera.background_color = BG
        self._notes = VGroup()

    def note(
        self, text, target, direction=UP, buff=0.3, size=19, color=DIM, shift=None
    ):
        """Подпись у фигуры. Висит, пока её не снимет drop_notes().

        shift — сдвиг от края кадра: подпись шире фигуры и у самого борта
        обрезается.
        """
        cap = label(text, size, color).next_to(target, direction, buff=buff)
        if shift is not None:
            cap.shift(shift)
        self.play(FadeIn(cap), run_time=0.35)
        self._notes.add(cap)
        return cap

    def drop_notes(self, *keep, run_time=0.3):
        """Снять подписи, кроме перечисленных."""
        gone = [c for c in self._notes if c not in keep]
        if gone:
            self.play(*[FadeOut(c) for c in gone], run_time=run_time)
            self._notes.remove(*gone)
