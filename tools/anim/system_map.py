"""System map, полоса 850×280: вся система одним кадром.

Схема не собирается на глазах, а стоит готовой с первого кадра: под неё
рассказывают про систему целиком, и зритель должен видеть её всю, а не
ждать, пока дорисуется. Движение показывает только путь — фишка едет по
стрелке, узел на её конце подсвечивается.

Порядок обхода — от эволюции: прогон даёт логи → агент эволюции формирует
вопросы → агент озера идёт в озеро и в веб → ответ возвращается в прогон.
Статьи в озеро льются отдельным каналом, поэтому показаны последними.

Пропорция 3:1: вертикали почти нет, поэтому имена узлов стоят сверху,
отношения — снизу, а возврат логов идёт дугой под полосой.
"""

from manim import *

from theme import *

# Кадр задаётся здесь, а не флагом -r: присваивание идёт после разбора
# командной строки, поэтому переживает -qh. Вчетверо крупнее цели: 850
# получается ужатием, и на меньшей кратности контур в полосе рвётся.
config.pixel_width = 3400
config.pixel_height = 1120
config.frame_height = 4.68
config.frame_width = 14.22

# Контур толще базового: на 850 px по ширине штрих в 2.2 даёт около
# полутора пикселей и читается серой размазнёй.
WIDE_STROKE = 3.0

PAPER_C = LEFT * 6.6
LAKE_C = LEFT * 4.75
LAKE_R = 1.15
INNER = [UP * 0.48, RIGHT * 0.6 + DOWN * 0.12, LEFT * 0.48 + DOWN * 0.44]
INNER_EDGES = [(0, 1), (0, 2)]
AGENT_C = LEFT * 1.55
WEB_C = LEFT * 3.05 + UP * 1.5
EVO_C = RIGHT * 1.55
GIGA_C = RIGHT * 4.6


class SystemMap(PipelineScene):
    def construct(self):
        # --- вся схема разом ------------------------------------------------
        paper = doc(0.55, 0.75, 4).move_to(PAPER_C)

        rim = dot_ring(LAKE_C, LAKE_R, 40, IDEA, 0.035)
        nodes = VGroup(*[idea(0.24).move_to(LAKE_C + p) for p in INNER])
        edges = VGroup(
            *[
                span(nodes[a], nodes[b], 0.28).set_stroke(FAINT, width=2.4)
                for a, b in INNER_EDGES
            ]
        )
        leaves = VGroup(*[thesis(0.13).move_to(n.get_center()) for n in nodes[:2]])
        edges.set_z_index(-2)
        leaves.set_z_index(1)

        agent = robot(1.25).move_to(AGENT_C)
        evo = robot(1.25).move_to(EVO_C)
        web = globe(0.32).move_to(WEB_C)
        giga = box(1.9, 1.5).move_to(GIGA_C)
        wheel = rotor(0.42).move_to(GIGA_C)

        shapes = VGroup(paper, nodes, leaves, agent, evo, web, giga, wheel)
        shapes.set_stroke(width=WIDE_STROKE)

        # Все стрелки серые: цвет здесь означает «идёт прямо сейчас», а не
        # тип связи — тип держат подписи и сами фишки. Стрелка красится
        # только на время проезда, см. send().
        feed = self.link(paper.get_right(), LAKE_C + LEFT * LAKE_R)
        talk = self.link(LAKE_C + RIGHT * LAKE_R, agent.get_left(), both=True)
        # От края глобуса, а не от его центра: из центра стрелка идёт поверх
        # меридианов, и веб читается перечёркнутым.
        # В плечо, а не в угол рамки робота: в угол стрелка приходит полого
        # и проезжает по его имени.
        surf = self.link(
            WEB_C + rotate_vector(RIGHT * 0.32, -PI / 4),
            agent.get_left() + UP * 0.22,
            both=True,
        )
        pass_ = self.link(agent.get_right(), evo.get_left(), both=True)
        into = self.link(evo.get_right(), giga.get_left(), both=True)
        # От нижнего левого угла, а не от середины низа: из середины дуга
        # уходит вправо и пересекает борт коробки. Знак угла отрицательный —
        # при положительном дуга выгибается вверх и идёт по ногам роботов.
        home = ArcBetweenPoints(
            giga.get_corner(DL) + DOWN * 0.08,
            LAKE_C + rotate_vector(RIGHT * LAKE_R, -PI / 3),
            angle=-PI / 10,
        ).set_stroke(DIM, width=2.6)
        home.add_tip(tip_length=0.2, tip_width=0.16)

        # Имена узлов — сверху, отношения — снизу: нижняя треть полосы
        # оставлена дуге возврата, иначе она идёт по подписям.
        names = VGroup(
            self.tag("papers", paper, UP, 0.16, INK, 19),
            self.tag("idea lake", rim, UP, 0.14, IDEA, 21),
            self.tag("lake agent", agent, UP, 0.16, INK, 21),
            self.tag("evolution agent", evo, UP, 0.16, INK, 21),
            self.tag("GigaEvo", giga, UP, 0.16, INK, 21),
        )
        rels = VGroup(
            # Под страницей, а не под стрелкой: стрелка от страницы до озера
            # короче своей подписи, и подпись ложится на саму страницу.
            self.tag("ingest", paper, DOWN, 0.16, DIM, 18),
            self.tag("retrieve", talk, DOWN, 0.12, IDEA, 18),
            self.tag("questions", pass_, DOWN, 0.12, DIM, 18),
            self.tag("ideas · logs", into, DOWN, 0.12, DIM, 18),
            self.tag("run logs", home, DOWN, 0.12, LOG, 18),
            # Сбоку от глобуса и вместо имени узла: над ним борт кадра, под
            # ним стрелка, а два ярлыка на одну мелкую иконку — уже каша.
            self.tag("web search", web, RIGHT, 0.16, DIM, 18),
        )

        self.add(edges, shapes, rim, feed, talk, surf, pass_, into, home, names, rels)
        self.wait(0.7)

        # --- один обход, от прогона ------------------------------------------
        self.play(
            Rotate(wheel, TAU, about_point=GIGA_C, rate_func=linear),
            self.pulse(giga),
            run_time=1.2,
        )
        self.send([runlog(0.2) for _ in range(3)], into, LOG, back=True)
        self.play(self.pulse(evo), run_time=0.6)

        self.send([label("?", 26, INK) for _ in range(2)], pass_, INK, back=True)
        self.play(self.pulse(agent), run_time=0.6)

        # Озеро и веб — одним движением: агент спрашивает оба сразу.
        self.send([idea(0.16) for _ in range(2)], talk, IDEA, back=True, extra=surf)
        self.send([idea(0.16), thesis(0.2)], talk, IDEA)

        self.send([idea(0.18) for _ in range(2)], pass_, IDEA)
        self.send([idea(0.18) for _ in range(2)], into, IDEA)
        self.play(
            Rotate(wheel, TAU, about_point=GIGA_C, rate_func=linear),
            self.pulse(giga),
            run_time=1.0,
        )

        self.send([runlog(0.2) for _ in range(3)], home, LOG, run_time=1.5)

        # Круг замкнулся — озеро крутится: пунктир идёт по кольцу, и на
        # последних секундах видно, что система работает дальше.
        self.play(
            Rotate(rim, TAU / 4, about_point=LAKE_C, rate_func=linear), run_time=2.4
        )
        self.wait(0.8)

    def pulse(self, m, factor=1.07):
        """Внимание к узлу — размером, а не цветом.

        Indicate красит фигуру в свой цвет и возвращает обратно; на схеме,
        где всё стоит неподвижно, это читается чёрной вспышкой.
        """
        return m.animate(rate_func=there_and_back).scale(factor)

    def link(self, a, b, both=False):
        """Стрелка схемы. Один вес и один размер наконечника на всю полосу."""
        kind = DoubleArrow if both else Arrow
        return kind(
            a,
            b,
            buff=0.14,
            stroke_width=2.6,
            tip_length=0.18,
            max_tip_length_to_length_ratio=1.0,
        ).set_color(DIM)

    def tag(self, text, target, direction, buff, color, size):
        """Подпись схемы. Все висят с первого кадра до последнего."""
        return label(text, size, color).next_to(target, direction, buff=buff)

    def thicken(self, path, color, width=4.6):
        """Стрелка на время проезда толстеет и берёт цвет того, что по ней едет.

        set_color, а не set_stroke: наконечник — заливка, и от одного
        контурного цвета он остался бы серым.
        """
        return path.animate(rate_func=there_and_back).set_color(color).set_stroke(
            width=width
        )

    def send(self, tokens, path, color, back=False, run_time=1.1, extra=None):
        """Фишки едут по стрелке; сама стрелка на это время подсвечивается.

        back — против нарисованного направления: двусторонняя стрелка одна,
        а ездят по ней в обе стороны.
        """
        rate = (lambda t: 1 - t) if back else linear
        group = VGroup(*tokens)
        paths = [path] * len(group)
        if extra is not None:
            side = [t.copy() for t in tokens]
            group.add(*side)
            paths += [extra] * len(side)
        # Только контурным фишкам: у текста ширина штриха ноль, и общий
        # set_stroke обвёл бы «?» жирным контуром.
        for t in group:
            if t.get_stroke_width():
                t.set_stroke(width=WIDE_STROKE)
        for t, p in zip(group, paths):
            t.move_to(p.get_end() if back else p.get_start())
        self.add(group)
        self.play(
            LaggedStart(
                *[MoveAlongPath(t, p, rate_func=rate) for t, p in zip(group, paths)],
                lag_ratio=0.3,
            ),
            self.thicken(path, color),
            *([self.thicken(extra, color)] if extra else []),
            run_time=run_time,
        )
        self.play(FadeOut(group), run_time=0.25)
