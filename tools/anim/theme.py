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
    # Скругление от размера: фиксированные 0.07 на стороне 0.17 съедают
    # угол целиком, и тезис в идее читается кружком, а не квадратом.
    return (
        Square(side_length=size)
        .set_fill(BG, opacity=1)
        .set_stroke(color, width=STROKE)
        .round_corners(min(0.07, size * 0.09))
    )


def idea(radius=0.80, color=IDEA):
    return Circle(radius=radius).set_fill(BG, opacity=1).set_stroke(color, width=STROKE)


def runlog(size=0.62, color=LOG):
    t = Triangle().set_fill(BG, opacity=1).set_stroke(color, width=STROKE)
    t.set_height(size)
    return t


def box(width=2.6, height=1.5, color=INK):
    """Внешняя система или шаг-«чёрный ящик»."""
    return (
        RoundedRectangle(width=width, height=height, corner_radius=0.1)
        .set_fill(BG, opacity=1)
        .set_stroke(color, width=1.6)
    )


def rotor(radius=0.66, color=IDEA, spokes=6):
    """Колесо со спицами: крутится — значит, идёт прогон."""
    return VGroup(
        Circle(radius=radius).set_fill(opacity=0).set_stroke(color, width=2.2),
        *[
            Line(ORIGIN, RIGHT * radius)
            .rotate(i * TAU / spokes, about_point=ORIGIN)
            .set_stroke(color, width=2)
            for i in range(spokes)
        ],
    )


def globe(radius=0.55, color=INK):
    """Веб: круг с меридианом и параллелями."""
    return VGroup(
        Circle(radius=radius).set_fill(BG, opacity=1).set_stroke(color, width=1.8),
        Ellipse(width=radius, height=radius * 2)
        .set_fill(opacity=0)
        .set_stroke(color, width=1.4),
        *[
            Line(LEFT * w, RIGHT * w).shift(UP * dy).set_stroke(color, width=1.4)
            for w, dy in (
                (radius, 0.0),
                (radius * 0.76, radius * 0.55),
                (radius * 0.76, -radius * 0.55),
            )
        ],
    ).move_to(ORIGIN)


def robot(size=1.4, color=INK):
    """Агент.

    Робота в наборе иконок AIRI нет (шаблон, стр. 47–48), поэтому он собран
    из тех же примитивов, что и человечки оттуда: один вес контура,
    скруглённые углы, точки-акценты, без заливки внутри.
    """
    head = RoundedRectangle(width=0.62, height=0.5, corner_radius=0.12)
    body = RoundedRectangle(width=0.8, height=0.62, corner_radius=0.12).next_to(
        head, DOWN, buff=0.12
    )
    frame = VGroup(
        head,
        body,
        Line(head.get_bottom(), body.get_top()),  # шея
        Line(head.get_top(), head.get_top() + UP * 0.18),  # антенна
        Line(body.get_left(), body.get_left() + LEFT * 0.24),
        Line(body.get_right(), body.get_right() + RIGHT * 0.24),
        *[
            Line(
                body.get_bottom() + RIGHT * s * 0.19,
                body.get_bottom() + RIGHT * s * 0.19 + DOWN * 0.24,
            )
            for s in (1, -1)
        ],
    )
    head.set_fill(BG, opacity=1)
    body.set_fill(BG, opacity=1)
    frame.set_stroke(color, width=2.2)
    dots = VGroup(
        *[
            Dot(head.get_center() + RIGHT * s * 0.15, radius=0.05, color=color)
            for s in (1, -1)
        ],
        Dot(head.get_top() + UP * 0.23, radius=0.055, color=color),
    )
    return VGroup(frame, dots).set_height(size)


def check(size=0.55, color=IDEA):
    """Вердикт арбитра: да."""
    v = VMobject().set_points_as_corners(
        [LEFT * 0.5 + UP * 0.1, DOWN * 0.45, RIGHT * 0.6 + UP * 0.6]
    )
    return v.set_stroke(color, width=6).set_fill(opacity=0).set_width(size)


def cross(size=0.55, color=BAD):
    """Вердикт арбитра: нет."""
    return Cross(stroke_color=color, stroke_width=6).set_width(size)


def span(a, b, gap=0.68):
    """Ребро от края до края, а не от центра к центру.

    Утолщённое ребро от центров наезжает на контур узла: белая заливка
    круга его не прячет, порядок слоёв тут не спасает.
    """
    d = normalize(b.get_center() - a.get_center())
    return Line(a.get_center() + d * gap, b.get_center() - d * gap)


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
