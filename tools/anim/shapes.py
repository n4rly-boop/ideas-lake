"""Словарь фигур отдельными картинками: круг — идея, квадрат — тезис,
треугольник — лог прогона. Цвета и толщина контура те же, что в роликах.

Рендер: ./shapes.sh (или вручную `.venv/bin/manim -s -t shapes.py Idea`).
"""

from manim import *

from theme import *

config.pixel_width = 512
config.pixel_height = 512
config.frame_height = 2.0
config.frame_width = 2.0


class _Shape(Scene):
    """Одна фигура во весь кадр.

    Заливка снята: в роликах она белая, чтобы прятать под узлом рёбра, а
    отдельной картинке на слайде нужен прозрачный центр.
    """

    make = None

    def construct(self):
        m = self.make().set_fill(opacity=0).set_height(1.5).move_to(ORIGIN)
        # Штрих ставится после set_height: масштаб его не трогает, и без
        # этого контур на 512 px выходит волосяным.
        m.set_stroke(width=9)
        self.add(m)


class Idea(_Shape):
    make = staticmethod(idea)


class Thesis(_Shape):
    make = staticmethod(thesis)


class RunLog(_Shape):
    make = staticmethod(runlog)
