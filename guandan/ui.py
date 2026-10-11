"""掼蛋 Kivy 横屏界面与交互逻辑。

严格分层：本模块只负责展示与交互，所有牌型判断/合法性校验统一调用 rules 引擎，
所有流程状态来自 game，展示分组/选牌/候选来自 arrange。UI 不实现任何规则校验。

布局区域（参考主流掼蛋手游横屏布局）：
- 背景：程序目录下「背景图.jpg」拉伸铺满整窗（缺失时退回木纹底色）。
- 顶部：左上信息区；右上「开新局 / 退出 / 设置」等宽按钮，上沿与对家头像齐平。
- 四名玩家：卡通头像（对家头像上沿与右上按钮上端齐平，名字/剩牌数在头像左右
  两侧贴图标、间隔半个字；上家/下家与出牌区水平中线对齐；我方在手牌区左侧；
  超过报牌阈值显示「不够报牌」）；每人一个出牌分区（全部位于中央出牌矩形内，
  互不重叠）。
- 中央出牌区：从对家到手牌区、上家到下家之间的矩形；每轮打出的牌缩小横排
  （相互挨着）显示在各家分区，展示牌高度为手牌牌高的 80%；轮到某家出牌时清除
  该家上一轮展示；进贡/还贡共用按钮居中显示；抗贡时在对应玩家分区显示「抗贡！」；
  行动徽章为各家分区中央的圆形「出牌」。
- 局终：出牌区显示头游/二游/三游/末游（一方两家出完时其余两家均记末游）+
  「X方胜，下局打X」结算 + 下一局/退出。
- 底部：手牌区（无底色；炸弹/同花顺组在左、普通手牌按点数分列居中、
  非炸弹牌组在右；列内纵向重叠仅露出每张牌角标，同点列从下到上按黑桃/红桃/
  梅花/方块排列；牌面尺寸按「6 张一列恰不出出牌区下沿」锁定，不随列长变化，
  7 炸可向上突破，整体过宽时列间水平重叠）+ 功能条（手牌一行切换 /
  同花顺识别框（黑边框无填充、实心/空心花色，图案高为栏高 80%） / 整理手牌 /
  恢复手牌，四个控件间隔一个字）。

牌面：白底圆角；级牌浅绿填充、文字花色为常规红黑配色；中央花色图案宽约牌面
3/4；左上角标（点数+花色）文字高度为牌高 36%；王牌中央画小猫（大王红色、小王
黑色），左上角标「大/小」。

交互：点击选牌（单张选中，再点取消）；按住滑动连续选牌/取消；点击空白处
取消全部选中；选中牌保留颜色高亮（无弹出动画）。按钮为圆角矩形、字体微软雅黑、
字号为按钮高度 60%，按下有下沉动画。全部按窗口尺寸自适应。
"""
from __future__ import annotations

import json
import os
import random
import time
from typing import Callable, Dict, List, Optional, Set, Tuple

from kivy.animation import Animation
from kivy.app import App
from kivy.clock import Clock
from kivy.core.text import LabelBase
from kivy.core.window import Window
from kivy.graphics import Color, Ellipse, Line, Rectangle, RoundedRectangle, Triangle
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.checkbox import CheckBox
from kivy.uix.image import Image as KImage
from kivy.uix.label import Label
from kivy.uix.screenmanager import NoTransition, Screen, ScreenManager
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput
from kivy.uix.widget import Widget
from kivy.utils import platform as _platform

from ai import AIProfile, choose_action
from arrange import HandManager
from cards import BIG_JOKER, Card, Suit, RANK_LABELS
from game import GameError, GuandanGame, IllegalPlayError, NotPlayerTurnError
from rules import RuleConfig, can_play

# 扑克花色符号（Windows 下 msyh 缺 ♠♥♣♦ 字形，用 Segoe UI Symbol 渲染；
# Android 下统一使用随包内嵌的 Noto Sans CJK，其自带花色字形）
SUIT_SYM = {Suit.SPADE: "♠", Suit.HEART: "♥", Suit.CLUB: "♣", Suit.DIAMOND: "♦"}
_HERE = os.path.dirname(os.path.abspath(__file__))
_NOTO = os.path.join(_HERE, "fonts", "NotoSansCJKsc-Regular.otf")
if _platform == "android" and os.path.exists(_NOTO):
    _CJK_FONT = _NOTO
    _SYM_FONT = _NOTO
else:
    _CJK_FONT = None
    for _f in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyh.ttf",
               r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\simsun.ttc"):
        if os.path.exists(_f):
            _CJK_FONT = _f
            break
    _SYM_FONT = r"C:\Windows\Fonts\seguisym.ttf"
if os.path.exists(_SYM_FONT):
    LabelBase.register("suitfont", _SYM_FONT)


def _sym(sym: str) -> str:
    """花色符号 markup：用 suitfont 字体渲染，缺字体时退回原字符。"""
    return f"[font=suitfont]{sym}[/font]" if os.path.exists(_SYM_FONT) else sym

# 角色名（0=人类玩家下方，1=下家右侧，2=对家上方，3=上家左侧）
PLAYER_NAMES = {0: "我", 1: "下家", 2: "对家", 3: "上家"}

# ---------------------------------------------------------------- 资源
_ASSETS = os.path.join(_HERE, "assets")
# 头像：0=我(缅因猫) 1=下家(无毛猫) 2=对家(田园猫) 3=上家(波斯猫)
AVATAR_PATHS = {pid: os.path.join(_HERE, f"{name}.png") for pid, name in
                ((0, "缅因猫"), (1, "无毛猫"), (2, "田园猫"), (3, "波斯猫"))}

WOOD = (0.44, 0.29, 0.16, 1)          # 木纹棕
WOOD_DARK = (0.30, 0.19, 0.10, 1)
GOLD = (0.85, 0.68, 0.25, 1)
ORANGE = (0.90, 0.45, 0.10, 1)
BLUE = (0.15, 0.45, 0.80, 1)
GREEN = (0.20, 0.60, 0.30, 1)
DARKGREY = (0.30, 0.30, 0.30, 1)
PANEL = (0.16, 0.34, 0.22, 1)         # 牌桌绿
WHITE = (0.97, 0.97, 0.94, 1)
LEVEL_FACE = (0.78, 0.93, 0.78, 1)    # 级牌浅绿牌面
SEL_FACE = (1.0, 0.90, 0.45, 1)       # 选中牌面
BG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "背景图.jpg")

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
DEFAULT_SETTINGS = {
    "report_threshold": 10,
    "low_card_alert": True,
    "hand_mode": "column",
    "ai_delay": 0.35,
    "level_start": 2,
}


def load_settings() -> dict:
    data = dict(DEFAULT_SETTINGS)
    try:
        if os.path.exists(SETTINGS_PATH):
            with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            for k in DEFAULT_SETTINGS:
                if k in loaded:
                    data[k] = loaded[k]
    except Exception:
        pass
    if not isinstance(data["report_threshold"], int) or data["report_threshold"] < 1:
        data["report_threshold"] = 10
    if data["hand_mode"] not in ("row", "column"):
        data["hand_mode"] = "column"
    return data


def save_settings(data: dict) -> None:
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------- 基础控件
class FlatButton(Widget):
    """圆角矩形按钮：圆角 + 底部厚度（3D）+ 顶部高光，按下有下沉动画。"""

    def __init__(self, text: str, color, on_press: Callable,
                 enabled=True, text_color=(1.0, 1.0, 1.0), **kw):
        kw.setdefault("size_hint", (None, None))
        super().__init__(**kw)
        self.text = text
        self.on_press = on_press
        self.enabled = enabled
        self.base_color = color
        self.text_color = tuple(text_color[:3])
        self._pressed = False
        self._press_y: Optional[float] = None
        init_c = color if enabled else (0.45, 0.45, 0.45, 1)
        with self.canvas.before:
            self._sh_c = Color(0, 0, 0, 0.28)
            self._sh = RoundedRectangle(pos=self.pos, size=self.size, radius=[10])
            self._bd_c = Color(*self._shade(init_c, 0.60))
            self._bd = RoundedRectangle(pos=self.pos, size=self.size, radius=[10])
            self._top_c = Color(*init_c)
            self._top = RoundedRectangle(pos=self.pos, size=self.size, radius=[10])
        self._label = Label(text=text, font_name=_CJK_FONT, bold=True,
                            font_size=16, color=self.text_color + (1 if enabled else 0.4,),
                            markup=True, halign="center", valign="middle")
        self._label.bind(size=self._label.setter("text_size"))
        self.add_widget(self._label)
        self.bind(pos=self._place, size=self._place)
        self._place()

    @staticmethod
    def _shade(c, k):
        return (c[0] * k, c[1] * k, c[2] * k, 1)

    def _place(self, *a):
        x, y = self.pos
        w, h = self.size
        rad = [max(6.0, h * 0.18)]            # 圆角矩形
        th = max(2.0, h * 0.12)            # 底部厚度
        self._sh.pos = (x, y - th * 0.7)
        self._sh.size = (w, h)
        self._sh.radius = rad
        self._bd.pos = (x, y)
        self._bd.size = (w, h)
        self._bd.radius = rad
        self._top.pos = (x, y + th)
        self._top.size = (w, h - th)
        self._top.radius = rad
        self._label.pos = (x, y + th)
        self._label.size = (w, h - th)
        # 字号为按钮高度 60%（长文本按宽度收敛，防止溢出）
        est = sum(1.0 if ord(ch) > 0x2E80 else 0.55 for ch in self.text) or 1.0
        self._label.font_size = max(9, int(min(h * 0.60, w * 0.94 / est)))

    def set_enabled(self, val: bool):
        self.enabled = val
        self._top_c.rgba = self.base_color if val else (0.45, 0.45, 0.45, 1)
        self._bd_c.rgba = self._shade(self.base_color if val else (0.45, 0.45, 0.45, 1), 0.60)
        self._label.color = self.text_color + (1 if val else 0.45,)

    def _sink(self, down: bool):
        h = self.height
        th = max(2.0, h * 0.12)
        target = self._press_y if not down else self._press_y - th
        Animation.cancel_all(self, "y")
        Animation(y=target, duration=0.06 if down else 0.08,
                  t="out_quad" if not down else "in_quad").start(self)

    def on_touch_down(self, touch):
        if self.enabled and self.collide_point(*touch.pos):
            self._pressed = True
            self._press_y = self.y
            self._sink(True)
            return True
        return False

    def on_touch_up(self, touch):
        if not self._pressed:
            return False
        self._pressed = False
        self._sink(False)
        if self.collide_point(*touch.pos) and self.on_press:
            self.on_press()
        return True


class CircleBadge(Widget):
    """圆形行动徽章（出牌/进贡/还贡），放在行动者出牌分区中央。"""

    def __init__(self, text: str, **kw):
        kw.setdefault("size_hint", (None, None))
        super().__init__(**kw)
        with self.canvas.before:
            self._sh_c = Color(0, 0, 0, 0.25)
            self._sh = Ellipse(pos=self.pos, size=self.size)
            self._bg_c = Color(0.94, 0.80, 0.32, 1)
            self._bg = Ellipse(pos=self.pos, size=self.size)
            self._ring_c = Color(0.62, 0.45, 0.10, 1)
            self._ring = Line(width=1.2)
        self._label = Label(text=f"[b]{text}[/b]", font_name=_CJK_FONT, markup=True,
                            font_size=14, color=(0.70, 0.15, 0.08, 1),
                            halign="center", valign="middle")
        self._label.bind(size=self._label.setter("text_size"))
        self.add_widget(self._label)
        self.bind(pos=self._place, size=self._place)
        self._place()

    def _place(self, *a):
        x, y = self.pos
        w, h = self.size
        self._sh.pos = (x, y - 2)
        self._sh.size = (w, h)
        self._bg.pos = (x, y)
        self._bg.size = (w, h)
        self._ring.ellipse = (x + 1, y + 1, w - 2, h - 2)
        self._label.pos = (x, y)
        self._label.size = (w, h)
        self._label.font_size = max(12, int(h * 0.30))


class SuitIcon(Widget):
    """同花顺栏花色图标：无底色无边框，仅花色符号（实心附候选数），可点击。"""

    def __init__(self, text: str, color, on_press: Optional[Callable] = None, **kw):
        kw.setdefault("size_hint", (None, None))
        super().__init__(**kw)
        self.on_press = on_press
        self._label = Label(text=text, font_name=_CJK_FONT, markup=True, bold=True,
                            color=color, halign="center", valign="middle")
        self._label.bind(size=self._label.setter("text_size"))
        self.add_widget(self._label)
        self.bind(pos=self._place, size=self._place)
        self._place()

    def _place(self, *a):
        x, y = self.pos
        w, h = self.size
        self._label.pos = (x, y)
        self._label.size = (w, h)
        # 图案高约为栏高 80%（花色字形约占字号的 75%）
        plain = "".join(ch for ch in self._label.text
                        if ch not in "[]" and not ch.isalpha())
        est = sum(1.0 if ord(ch) > 0x2E80 else 0.55 for ch in plain) or 1.0
        self._label.font_size = max(12, int(min(h * 1.05, w * 0.94 / est)))

    def on_touch_down(self, touch):
        if self.on_press and self.collide_point(*touch.pos):
            self.on_press()
            return True
        return False


class CardWidget(Widget):
    """扑克牌：白底圆角（级牌浅绿）、左上角标（点数+花色）、中央大花色图案。

    王牌中央画小猫（大王红/小王黑），左上角标竖排「JOKER」。
    列内纵向重叠时仅露出每张牌顶部条带（角标可见），最底部牌整张可见。
    """

    def __init__(self, card: Card, level: int, on_tap: Optional[Callable] = None,
                 selected=False, show_face=True, **kw):
        kw.setdefault("size_hint", (None, None))
        super().__init__(**kw)
        self.card = card
        self.level = level
        self.on_tap = on_tap
        self.selected = selected
        self.show_face = show_face
        with self.canvas.before:
            self._edge_c = Color(0.30, 0.30, 0.33, 1)
            self._edge_r = RoundedRectangle(pos=self.pos, size=self.size, radius=[6])
            self._face_c = Color(1, 1, 1, 1)
            self._face_r = RoundedRectangle(pos=self.pos, size=self.size, radius=[6])
        self._cat = None
        if card.is_joker:
            with self.canvas:
                self._cat_c = Color(0.80, 0.10, 0.10, 1)
                self._cat_head = Ellipse()
                self._cat_ear_l = Triangle()
                self._cat_ear_r = Triangle()
                self._cat_wisk = Line(width=1.2)
                self._cat_w = Color(1, 1, 1, 1)
                self._cat_eye_l = Ellipse()
                self._cat_eye_r = Ellipse()
                self._cat_mouth = Line(width=1.0)
            self._cat = True
        self._corner = Label(text="", font_name=_CJK_FONT, bold=True,
                             font_size=13, markup=True, halign="left", valign="top")
        self._corner.bind(size=self._corner.setter("text_size"))
        self._pip = Label(text="", font_name=_CJK_FONT, bold=True,
                          font_size=20, markup=True, halign="center", valign="middle")
        self._pip.bind(size=self._pip.setter("text_size"))
        self.add_widget(self._pip)
        self.add_widget(self._corner)
        self.bind(pos=self._redraw, size=self._redraw)
        self._redraw()

    def _redraw(self, *a):
        x, y = self.pos
        w, h = self.size
        rad = [max(3.0, min(w, h) * 0.14)]
        self._edge_r.pos = (x, y)
        self._edge_r.size = (w, h)
        self._edge_r.radius = rad
        self._face_r.pos = (x + 1.2, y + 1.2)
        self._face_r.size = (w - 2.4, h - 2.4)
        self._face_r.radius = rad
        if not self.show_face:
            self._face_c.rgba = (0.20, 0.35, 0.60, 1)
            self._set_text(self._corner, "")
            self._set_text(self._pip, "")
            self._hide_cat()
            return
        c = self.card
        level_card = (not c.is_joker) and c.rank == self.level
        if self.selected:
            self._face_c.rgba = SEL_FACE
            self._edge_c.rgba = (0.85, 0.62, 0.10, 1)
        else:
            self._face_c.rgba = LEVEL_FACE if level_card else (1, 1, 1, 1)
            self._edge_c.rgba = (0.30, 0.30, 0.33, 1)
        if c.is_joker:
            big = c.rank == BIG_JOKER
            col = (0.80, 0.10, 0.10, 1) if big else (0.12, 0.12, 0.14, 1)
            # 竖排 JOKER 角标（大王红 / 小王黑，靠中央小猫颜色区分）
            fs = max(9, int(h * 0.11))
            band = int(fs * 1.30 * 5)
            self._corner.color = col
            self._corner.font_size = fs
            self._corner.size = (int(w * 0.6), band)
            # 上移 fs*0.33：字体纹理在首行字形上方有行距留白，补偿后文字顶边才与牌顶齐平
            self._corner.pos = (int(x + w * 0.08), int(y + h - band - h * 0.02 + fs * 0.33))
            self._set_text(self._corner, "J\nO\nK\nE\nR")
            self._set_text(self._pip, "")
            self._draw_cat(col, SEL_FACE if self.selected else (1, 1, 1, 1))
        else:
            col = self._suit_color(c.suit)
            self._corner.color = col
            self._pip.color = col
            wild = "*" if c.is_wild(self.level) else ""
            plain = f"{RANK_LABELS[c.rank]}{SUIT_SYM[c.suit]}{wild}"
            est = sum(1.0 if ord(ch2) > 0x2E80 else 0.55 for ch2 in plain)
            fs = max(9, min(int(h * 0.36), int(w * 0.98 / max(est, 1.0))))
            band = int(fs * 1.30)
            # 角标顶边与牌顶齐平（上移 fs*0.33 补偿纹理首行上方留白）；
            # 文本区加宽防「10♠」换行裁剪
            self._corner.font_size = fs
            self._corner.size = (int(w * 1.8), band)
            self._corner.pos = (int(x + w * 0.08), int(y + h - band + fs * 0.33))
            # 中央大花色：字形宽≈牌宽（文本区超出牌面并手动居中，防裁剪）
            self._pip.font_size = max(14, int(w * 1.15))
            self._pip.size = (int(w * 2.3), int(h * 0.72))
            self._pip.pos = (int(x - w * 0.65), int(y + h * 0.005))
            self._set_text(self._corner,
                           f"[b]{RANK_LABELS[c.rank]}[/b]{_sym(SUIT_SYM[c.suit])}{wild}")
            self._set_text(self._pip, _sym(SUIT_SYM[c.suit]))

    @staticmethod
    def _suit_color(suit):
        """花色配色：方块紫、梅花蓝、红桃红、黑桃黑。"""
        if suit == Suit.DIAMOND:
            return (0.52, 0.15, 0.72, 1)
        if suit == Suit.CLUB:
            return (0.10, 0.28, 0.75, 1)
        if suit == Suit.HEART:
            return (0.80, 0.10, 0.10, 1)
        return (0.12, 0.12, 0.14, 1)

    def _draw_cat(self, col, face_col):
        """王牌中央小猫：胡须展开宽约牌面 3/4。"""
        if self._cat is None:
            return
        x, y = self.pos
        w, h = self.size
        cx, cy = x + w * 0.56, y + h * 0.30   # 略偏右下，给左侧竖排 JOKER 让位
        hd = 0.44 * w                       # 头径
        self._cat_c.rgba = col
        self._cat_head.pos = (cx - hd / 2, cy - hd / 2)
        self._cat_head.size = (hd, hd)
        self._cat_ear_l.points = (cx - hd * 0.42, cy + hd * 0.28,
                                  cx - hd * 0.54, cy + hd * 0.78,
                                  cx - hd * 0.06, cy + hd * 0.50)
        self._cat_ear_r.points = (cx + hd * 0.42, cy + hd * 0.28,
                                  cx + hd * 0.54, cy + hd * 0.78,
                                  cx + hd * 0.06, cy + hd * 0.50)
        self._cat_wisk.points = [
            cx - hd * 0.50, cy - hd * 0.02, cx - hd * 0.85, cy - hd * 0.14,
            cx - hd * 0.50, cy - hd * 0.16, cx - hd * 0.85, cy - hd * 0.24,
            cx + hd * 0.50, cy - hd * 0.02, cx + hd * 0.85, cy - hd * 0.14,
            cx + hd * 0.50, cy - hd * 0.16, cx + hd * 0.85, cy - hd * 0.24]
        er = hd * 0.10
        self._cat_w.rgba = face_col
        self._cat_eye_l.pos = (cx - hd * 0.20 - er, cy + hd * 0.08 - er)
        self._cat_eye_l.size = (er * 2, er * 2)
        self._cat_eye_r.pos = (cx + hd * 0.20 - er, cy + hd * 0.08 - er)
        self._cat_eye_r.size = (er * 2, er * 2)
        mw = hd * 0.12
        self._cat_mouth.points = [cx - mw, cy - hd * 0.14, cx, cy - hd * 0.22,
                                  cx + mw, cy - hd * 0.14]

    def _hide_cat(self):
        if self._cat is None:
            return
        self._cat_head.size = (0, 0)
        self._cat_eye_l.size = (0, 0)
        self._cat_eye_r.size = (0, 0)
        self._cat_ear_l.points = ()
        self._cat_ear_r.points = ()
        self._cat_wisk.points = []
        self._cat_mouth.points = []

    @staticmethod
    def _set_text(lb: Label, txt: str):
        if lb.text != txt:      # 动画期间避免每帧重建纹理
            lb.text = txt

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos) and self.on_tap and self.show_face:
            self.on_tap(self.card.eid)
            return True
        return False


class Panel(Widget):
    def __init__(self, color=PANEL, border=GOLD, radius=10, **kw):
        kw.setdefault("size_hint", (None, None))
        super().__init__(**kw)
        self._radius = radius
        with self.canvas.before:
            self._bg_c = Color(*color)
            self._bg = RoundedRectangle(pos=self.pos, size=self.size, radius=[radius])
            self._bd_c = Color(*border)
            self._bd = Line(width=1.2)
        self.bind(pos=self._place, size=self._place)
        self._place()

    def _place(self, *a):
        self._bg.pos = self.pos
        self._bg.size = self.size
        x, y = self.pos
        w, h = self.size
        rad = min(self._radius, w / 2, h / 2)
        self._bd.rounded_rectangle = (x, y, w, h, rad)


def _label(text, size=16, color=(1, 1, 1, 1), bold=False, widget_size=None, **kw):
    kw.setdefault("halign", "center")
    kw.setdefault("valign", "middle")
    lb = Label(text=text, font_name=_CJK_FONT, font_size=size, bold=bold,
               color=color, markup=True, **kw)
    if widget_size is not None:
        lb.size_hint = (None, None)
        lb.size = widget_size
    lb.bind(size=lb.setter("text_size"))
    return lb


# ---------------------------------------------------------------- 牌桌界面
class TableScreen(Screen):
    # 中央出牌区：从对家到手牌区、上家到下家之间的矩形（窗口比例）；
    # 下沿上移半张手牌高度（0.445→0.519），上/下家头像与其新水平中线 0.697 对齐
    PLAY_RECT = (0.115, 0.519, 0.885, 0.875)
    # 各玩家头像/名字/剩牌数（对家：头像上沿与右上按钮上端(0.988)齐平，名字/剩牌数
    # 在头像左右两侧贴图标、间隔半个字，与头像横向轴线居中；其余：名字在头像上方、
    # 剩牌数在头像下方，均与头像水平居中；窗口比例）
    P_LAYOUT = {
        2: dict(avatar=(0.468, 0.896, 0.532, 0.988), name=(0.320, 0.920, 0.460, 0.965),
                count=(0.540, 0.920, 0.680, 0.965)),
        3: dict(avatar=(0.020, 0.651, 0.080, 0.743), name=(0.000, 0.751, 0.100, 0.795),
                count=(0.000, 0.598, 0.100, 0.643)),
        1: dict(avatar=(0.920, 0.651, 0.980, 0.743), name=(0.900, 0.751, 1.000, 0.795),
                count=(0.900, 0.598, 1.000, 0.643)),
        0: dict(avatar=(0.020, 0.225, 0.080, 0.317), name=(0.000, 0.325, 0.100, 0.369),
                count=(0.000, 0.173, 0.100, 0.219)),
    }

    def __init__(self, app, **kw):
        super().__init__(**kw)
        self.app = app
        self.game = app.game
        self.mgr = app.mgr
        self._alerted = set()
        self._last_round = -1
        self._toast = None
        self._ai_scheduled = False
        self._mode = "play"  # play | tribute | return
        self._prev_phase = self.game.phase
        self._tribute_hold_until = 0.0   # 贡牌展示保留期截止时刻
        self._hand_widgets: Dict[int, CardWidget] = {}
        self._drag_target: Optional[bool] = None
        self._drag_touched: Set[int] = set()
        self._btn_play: Optional[FlatButton] = None
        self._btn_pass: Optional[FlatButton] = None
        self.refresh()
        Clock.schedule_interval(self._pump, 0.12)

    # ---- 布局工具
    def _rect(self, fx1, fy1, fx2, fy2):
        W, H = Window.width, Window.height
        return int(fx1 * W), int(fy1 * H), int((fx2 - fx1) * W), int((fy2 - fy1) * H)

    def _add_panel(self, region, color=PANEL, border=GOLD, radius=10):
        x, y, w, h = region
        p = Panel(pos=(x, y), size=(w, h), color=color, border=border, radius=radius)
        self.add_widget(p)
        return p

    def _add_label(self, region, text, **kw):
        x, y, w, h = region
        lb = _label(text, **kw)
        lb.size_hint = (None, None)
        lb.pos = (x, y)
        lb.size = (w, h)
        self.add_widget(lb)
        return lb

    # ---- 中央出牌区四家分区（互不重叠）
    def _play_zones(self) -> Dict[int, Tuple[float, float, float, float]]:
        rx, ry, rw, rh = self._rect(*self.PLAY_RECT)
        return {
            2: (rx + rw * 0.30, ry + rh * 0.64, rw * 0.40, rh * 0.36),   # 对家：上中
            0: (rx + rw * 0.30, ry, rw * 0.40, rh * 0.36),               # 我：下中
            3: (rx, ry + rh * 0.32, rw * 0.24, rh * 0.36),               # 上家：左中
            1: (rx + rw * 0.76, ry + rh * 0.32, rw * 0.24, rh * 0.36),   # 下家：右中
        }

    # ---- 主刷新
    def refresh(self):
        g = self.game
        # 进入牌局时若还有贡牌/抗贡展示：保留 2s 后再正式开始行牌
        if g.phase == "playing" and self._prev_phase != "playing" and \
                (g.tribute_displays or g.tribute_refused_by):
            self._tribute_hold_until = time.monotonic() + 2.0
        self._prev_phase = g.phase
        self.clear_widgets()
        self._drag_target = None
        self._drag_touched = set()
        self._update_mode()
        W, H = Window.width, Window.height
        self._bg = Panel(pos=(0, 0), size=(W, H), color=WOOD, border=WOOD, radius=0)
        self.add_widget(self._bg)
        if os.path.exists(BG_PATH):
            self.add_widget(KImage(source=BG_PATH, pos=(0, 0), size=(W, H),
                                   size_hint=(None, None), allow_stretch=True,
                                   keep_ratio=False))

        self._build_hand()
        self._build_players()
        self._build_play_areas()
        self._build_info()
        self._build_sf_strip()
        self._build_actions()
        self._build_topbar()
        self._update_alerts()

    # ---- 左上信息区：「我方打X」「对方打X」两个带底色文本框，攻方高亮
    def _build_info(self):
        g = self.game
        fs = max(16, int(0.024 * Window.width))   # 字号为原信息文字 2 倍
        for i, (txt, team) in enumerate((("我方打" + RANK_LABELS[g.levels[0]], 0),
                                         ("对方打" + RANK_LABELS[g.levels[1]], 1))):
            fx1 = 0.005 + i * 0.150
            x, y, w, h = self._rect(fx1, 0.905, fx1 + 0.142, 0.995)
            active = g.playing_team == team
            self.add_widget(Panel(pos=(x, y), size=(w, h),
                                  color=(0.99, 0.88, 0.55, 1) if active else PANEL,
                                  radius=8,
                                  border=GOLD if active else (0.45, 0.45, 0.45, 1)))
            self._add_label((x, y, w, h), f"[b]{txt}[/b]", size=fs,
                            color=(0.30, 0.16, 0.02, 1) if active else WHITE, bold=True)

    # ---- 右上角：开新局 / 退出 / 设置（等宽，上沿与对家头像齐平）
    def _build_topbar(self):
        H = Window.height
        h = int(0.063 * H)
        w = int(2.7 * h)                    # 容纳「开新局」三字（字号=高度80%）
        gap = int(0.012 * H)
        right = int(Window.width * 0.995)
        y = int(0.925 * H)
        GRN = (0.80, 0.93, 0.80, 1)          # 浅绿底
        GRN_TXT = (0.08, 0.26, 0.10, 1)      # 浅绿底上的深色文字
        btns = [
            ("开新局", GRN, self._on_new_match),
            ("退出", GRN, lambda: App.get_running_app().stop()),
            ("设置", GRN, lambda: self.app.goto("settings")),
        ]
        for i, (text, color, cb) in enumerate(btns):
            x = int(right - (3 - i) * w - (2 - i) * gap)
            self.add_widget(FlatButton(text, color, cb, text_color=GRN_TXT,
                                       pos=(x, y), size=(w, h)))

    # ---- 四名玩家：头像 + 名字（上方）+ 剩牌数（下方）
    def _turn_badges(self) -> Dict[int, str]:
        g = self.game
        out: Dict[int, str] = {}
        if g.phase == "playing":
            out[g.turn] = "出牌"
        elif g.phase == "tribute":
            for pid, _ in g.tribute_pairs:
                if pid not in g.tribute_done:
                    out[pid] = "进贡"
            for r in g.tribute_done.values():
                if r not in g.return_done:
                    out[r] = "还贡"
        return out

    def _build_players(self):
        g = self.game
        badges = self._turn_badges()
        thr = self.app.settings["report_threshold"]
        fs = 32
        for pid, lay in self.P_LAYOUT.items():
            ax, ay, aw, ah = self._rect(*lay["avatar"])
            self.add_widget(Panel(pos=(ax, ay), size=(aw, ah),
                                  color=(0, 0, 0, 0), radius=8,
                                  border=(0, 0, 0, 0)))
            path = AVATAR_PATHS.get(pid, "")
            if os.path.exists(path):
                # 透明底头像图铺满图标框；keep_ratio 保持源图宽高比，防止压扁
                img = KImage(source=path, allow_stretch=True, keep_ratio=True,
                             size_hint=(None, None),
                             pos=(ax, ay), size=(aw, ah))
                self.add_widget(img)
            else:
                self.add_widget(_label(PLAYER_NAMES[pid][:1], size=int(ah * 0.5),
                                       pos=(ax, ay), widget_size=(aw, ah)))
            # 名字（对家在头像左侧贴图标；其余在头像上方，16号）
            nkw = {"halign": "right"} if pid == 2 else {}
            self._add_label(self._rect(*lay["name"]), PLAYER_NAMES[pid],
                            size=fs, color=WHITE, bold=True, **nkw)
            # 剩牌数（对家在头像右侧贴图标；其余在头像下方，16号；
            # 超过报牌阈值显示「不够报牌」）
            n = g.hand_count(pid)
            if n <= thr:
                ctxt, ccol = f"剩 {n} 张", (1.0, 0.85, 0.40, 1)
            else:
                ctxt, ccol = "不够报牌", (0.78, 0.78, 0.74, 1)
            ckw = {"halign": "left"} if pid == 2 else {}
            self._add_label(self._rect(*lay["count"]), ctxt, size=fs, color=ccol,
                            bold=True, **ckw)

    # ---- 中央出牌区：各家出牌展示 / 贡牌展示 / 抗贡 / 结算 / 行动徽章
    def _build_play_areas(self):
        g = self.game
        zones = self._play_zones()
        W, H = Window.width, Window.height
        px1, py1, pw, ph = self._rect(*self.PLAY_RECT)
        self._add_panel((px1, py1, pw, ph),
                        color=(0, 0, 0, 0), border=(0, 0, 0, 0), radius=12)

        if g.phase in ("round_over", "match_over"):
            self._build_settlement(zones)
            return

        # 实时位次：玩家一出完牌即在其分区靠边界处显示名次（字号 40）
        if g.phase == "playing" and g.finish_order:
            ranks = ["头游", "二游", "三游", "末游"]
            for i, pid in enumerate(g.finish_order[:4]):
                zx, zy, zw, zh = zones[pid]
                lw, lh = int(2.6 * 40), int(1.4 * 40)
                if pid == 2:      # 贴出牌区上边界
                    lx, ly = zx + (zw - lw) / 2, zy + zh - lh
                elif pid == 0:    # 贴出牌区下边界
                    lx, ly = zx + (zw - lw) / 2, zy
                elif pid == 3:    # 贴出牌区左边界
                    lx, ly = zx, zy + (zh - lh) / 2
                else:             # 贴出牌区右边界
                    lx, ly = zx + zw - lw, zy + (zh - lh) / 2
                self._add_label((int(lx), int(ly), lw, lh), f"[b]{ranks[i]}[/b]",
                                size=40, color=(1.0, 0.85, 0.35, 1), bold=True)

        badges = self._turn_badges()
        if g.phase == "playing" and time.monotonic() < self._tribute_hold_until:
            badges = {}   # 贡牌/抗贡展示期：不出徽章，展示保持原大小
        zone_shown: Dict[int, bool] = {}
        for pid, zone in zones.items():
            zx, zy, zw, zh = zone
            up = pid in badges          # 分区还有行动徽章时，内容上移错开
            shown = False
            if g.phase == "playing" and pid in g.trick_displays and pid not in badges:
                # 轮到该家出牌时不画其旧展示（下方清除），否则旧牌会被缩小上移
                d = g.trick_displays[pid]
                if d["action"] == "play":
                    self._draw_mini(d["combo"].cards, zone, up=up)
                else:
                    self._zone_label(zone, "不  出", up=up)
                shown = True
            elif pid in g.tribute_displays and g.phase in ("tribute", "playing"):
                # 进贡/还贡牌展示保留到本局首次出牌（由 game 清除）
                self._draw_mini([g.tribute_displays[pid]["card"]], zone, up=up)
                shown = True
            elif g.phase == "playing" and pid in badges:
                shown = False   # 轮到该家出牌：清除其上一轮出牌/不出展示，以免干扰
            if not shown and g.tribute_refused and pid in g.tribute_refused_by \
                    and pid not in g.finish_order:
                self._zone_label(zone, "抗贡！", color=(1, 0.35, 0.30, 1), up=up)
                shown = True
            zone_shown[pid] = shown

        # 中央：进贡/还贡共用按钮
        if g.phase == "tribute":
            # 按「我」的任务显示按钮文字：我要进贡→进贡；我要还贡→还贡
            if self._mode == "tribute":
                text = "进贡"
            elif self._mode == "return":
                text = "还贡"
            else:
                pending_givers = [pid for pid, _ in g.tribute_pairs
                                  if pid not in g.tribute_done]
                text = "进贡" if pending_givers else "还贡"
            mx = px1 + pw / 2
            my = py1 + ph / 2
            bw, bh = int(0.110 * W), int(0.062 * H)
            self.add_widget(FlatButton(text, (0.80, 0.55, 0.15, 1), self._on_play,
                                       enabled=self._mode in ("tribute", "return"),
                                       pos=(int(mx - bw / 2), int(my - bh / 2)),
                                       size=(bw, bh)))

        # 行动徽章：圆形「出牌/进贡/还贡」置于行动者分区中央（有内容时缩小放底部）
        for pid, text in badges.items():
            zx, zy, zw, zh = zones[pid]
            if zone_shown.get(pid):
                d = min(0.040 * W, zh * 0.40)
                self.add_widget(CircleBadge(text, pos=(int(zx + zw / 2 - d / 2), int(zy + 2)),
                                            size=(d, d)))
            else:
                d = min(0.050 * W, zh * 0.92)
                self.add_widget(CircleBadge(text, pos=(zx + zw / 2 - d / 2, zy + zh / 2 - d / 2),
                                            size=(d, d)))

    def _build_settlement(self, zones):
        """局终结算：头游~末游 + 「X方胜，下局打X」 + 下一局/退出按钮。"""
        g = self.game
        W, H = Window.width, Window.height
        px1, py1, pw, ph = self._rect(*self.PLAY_RECT)
        mx = px1 + pw / 2
        my = py1 + ph / 2
        ranks = ["头游", "二游", "三游", "末游"]
        auto = set(g.round_result.get("auto_last", [])) if g.round_result else set()
        fo = g.round_result["finish_order"] if g.round_result else list(g.finish_order)
        for i, pid in enumerate(fo[:4]):
            # 未打完被记末游的玩家直接标「末游」，其余按标准名次
            self._zone_label(zones[pid], "末游" if pid in auto else ranks[i],
                             size=40, color=(1.0, 0.85, 0.35, 1))
        if g.phase == "match_over":
            big = "我方获胜！" if g.match_winner == 0 else "对方获胜！"
            self._center_text(mx, my + 0.014 * H, f"[b]{big}[/b]", 26, (1.0, 0.90, 0.40, 1))
            btn1 = ("再来一局", GREEN, self._on_new_match)
        else:
            wt = g.round_result["winner_team"] if g.round_result else 0
            who = "我方胜" if wt == 0 else "对方胜"
            sub = f"下局：我方打{RANK_LABELS[g.levels[0]]} · 对方打{RANK_LABELS[g.levels[1]]}"
            self._center_text(mx, my + 0.014 * H, f"[b]{who}[/b]　{sub}", 20,
                              (1.0, 0.90, 0.40, 1))
            btn1 = ("下一局", GREEN, self._on_next_round)
        bw, bh = int(0.105 * W), int(0.060 * H)
        by = int(my - 0.075 * H)
        self.add_widget(FlatButton(btn1[0], btn1[1], btn1[2],
                                   pos=(int(mx - bw - 6), by), size=(bw, bh)))
        self.add_widget(FlatButton("退出", (0.55, 0.18, 0.16, 1),
                                   lambda: App.get_running_app().stop(),
                                   pos=(int(mx + 6), by), size=(bw, bh)))

    def _center_text(self, mx, y, text, size, color):
        W, H = Window.width, Window.height
        self.add_widget(_label(text, size=size, color=color,
                               pos=(int(mx - 0.19 * W), int(y - 0.026 * H)),
                               widget_size=(int(0.38 * W), int(0.052 * H))))

    def _zone_label(self, zone, text, size=18, color=(0.95, 0.90, 0.70, 1), up=False):
        zx, zy, zw, zh = zone
        y = zy + zh * 0.50 if up else zy
        h = zh * 0.46 if up else zh
        self.add_widget(_label(f"[b]{text}[/b]", size=size, color=color,
                               pos=(int(zx), int(y)), widget_size=(int(zw), int(h))))

    def _draw_mini(self, cards, zone, up=False):
        """出牌展示：一组牌横排、相互挨着；高度为手牌牌高的 80%；
        up=True 时贴分区顶部（给徽章让位）。牌过多放不下时不缩小牌面，
        改为水平重叠（每张仍露出左上角标）。"""
        zx, zy, zw, zh = zone
        n = len(cards)
        if n == 0:
            return
        hand_h = (0.517 - 0.066) * Window.height
        hand_ch = max(60.0, (hand_h - 6) / 2.5)
        ch = 0.8 * hand_ch
        ch = min(ch, zh * (0.58 if up else 0.92))
        cw = ch * 0.72
        step = cw
        if n * cw > zw - 4:
            step = max(cw * 0.28, (zw - 4 - cw) / (n - 1))
        total = cw + (n - 1) * step
        sx = zx + (zw - total) / 2
        sy = zy + zh - ch - 2 if up else zy + (zh - ch) / 2
        for i, c in enumerate(cards):
            self.add_widget(CardWidget(c, self.game.level, on_tap=None, show_face=True,
                                       pos=(sx + i * step, sy), size=(cw, ch)))

    # ---- 底部手牌区（无底色）：炸弹/同花顺左、普通牌分列居中、组合牌右
    def _build_hand(self):
        g = self.game
        self._hand_widgets = {}
        x, y, w, h = self._rect(0.080, 0.066, 0.875, 0.517)
        mode = self.app.settings["hand_mode"]
        bombs, combos = self.mgr.arranged_layout()
        normal = self.mgr.normal_eids()
        nb, ncc = len(bombs), len(combos)
        # 牌面尺寸锁定：按「6 张一列恰到出牌区下沿」计算一次
        # （ch + 5×0.30ch = 2.5ch ≤ 区高），不随实际列长调整；更长列可向上突破
        ch = max(60.0, (h - 6) / 2.5)
        cw = ch * 0.72
        gap = 8
        pad = 5
        stack_step = cw + 10
        if mode == "row":
            left_w = nb * stack_step
            right_w = ncc * stack_step
            n = len(normal)
            avail_norm = max(cw, w - left_w - right_w - 10)
            step = min(cw * 0.55, (avail_norm - cw) / (n - 1)) if n > 1 else 0
            step = max(0.0, step)
            normal_total = cw + step * (n - 1) if n else 0
        else:
            cols = self.mgr.column_layout()
            nc = max(1, len(cols))
            step = 0.30 * ch
            left_w = nb * stack_step
            right_w = ncc * stack_step
            # 牌宽锁定不压缩；整体过宽时列间水平重叠（左上角标仍露出）
            avail = w - 6
            norm_avail = max(cw, avail - left_w - right_w - 20)
            col_step = min(cw + gap, (norm_avail - cw) / (nc - 1)) if nc > 1 else cw + gap
            col_step = max(cw * 0.30, col_step)
            normal_total = cw + col_step * (nc - 1)
            total = left_w + normal_total + right_w
            if nb + ncc and total > avail:
                stack_step = max(cw * 0.30,
                                 min(stack_step, (avail - normal_total - 20) / (nb + ncc)))
                left_w = nb * stack_step
                right_w = ncc * stack_step
        total = left_w + normal_total + right_w - (10 if right_w else 0)
        start = x + max(0, (w - total) / 2)
        # 左侧炸弹/同花顺组（同尺寸竖排，紧贴普通牌左侧，底部与普通牌对齐）
        sx = start
        for gi, eids in bombs:
            self._draw_stack(eids, sx, y + pad, cw, ch)
            sx += stack_step
        # 中间普通手牌
        nx0 = start + left_w
        if mode == "row":
            base_y = y + pad
            for i, eid in enumerate(normal):
                c = self.mgr.card_by_eid(eid)
                self._add_hand_card(c, nx0 + i * step, base_y, cw, ch)
        else:
            for ci, col in enumerate(cols):
                colx = nx0 + ci * col_step
                # 列内纵向重叠：底牌最后添加（在最上层，整张可见）
                for k in reversed(range(len(col))):
                    c = self.mgr.card_by_eid(col[k])
                    self._add_hand_card(c, colx, y + pad + k * step, cw, ch)
        # 右侧非炸弹牌组（同尺寸竖排，紧贴普通牌右侧，底部与普通牌对齐）
        sx = nx0 + normal_total + 10
        for gi, eids in combos:
            self._draw_stack(eids, sx, y + pad, cw, ch)
            sx += stack_step

    def _add_hand_card(self, card: Card, cx, cy, cw, ch):
        if card is None:
            return
        wid = CardWidget(card, self.game.level, on_tap=None,
                         selected=(card.eid in self.mgr.selected),
                         pos=(cx, cy), size=(cw, ch))
        self._hand_widgets[card.eid] = wid
        self.add_widget(wid)

    def _draw_stack(self, eids: List[int], cx, cy_bottom, cw, ch):
        """竖排牌组：底部与普通手牌对齐，固定重叠率露出每张牌角标；
        同点牌组从下到上按黑桃/红桃/梅花/方块排列；
        底牌最后添加（整张可见）；超出区高时向上突破（如 7 炸）。"""
        n = len(eids)
        if n == 0:
            return
        step = 0.30 * ch if n > 1 else 0
        base_y = cy_bottom
        col = list(eids)
        if len({self.mgr.card_by_eid(e).rank for e in col}) == 1:
            col.sort(key=lambda e: (int(self.mgr.card_by_eid(e).suit), e))
        for k in reversed(range(n)):
            c = self.mgr.card_by_eid(col[k])
            self._add_hand_card(c, cx, base_y + k * step, cw, ch)

    # ---- 底部功能条：手牌一行 / 同花顺框 / 整理 / 恢复（挨近，间隔一个字）
    def _build_sf_strip(self):
        W, H = Window.width, Window.height
        hs = int(0.058 * H)                     # 控件统一高度
        y = int(0.012 * H)
        gap = int(0.8 * hs)                     # 间隔一个字（字号≈高度80%）

        def text_w(nchars: float) -> int:
            return int(nchars * 0.8 * hs * 1.02 + 0.5 * hs)

        w_btn = text_w(4)
        YEL = (0.99, 0.94, 0.70, 1)          # 浅黄底
        YEL_TXT = (0.30, 0.20, 0.04, 1)      # 浅黄底上的深色文字
        # 同花顺栏：黑边框 + 浅黄填充（图标后添加，绘制在填充之上）
        box_w = int(0.240 * W)
        box_h = hs
        slot = box_w / 4.0
        # 总宽居中：手牌一行 | 同花顺栏 | 整理手牌 | 恢复手牌
        total = w_btn + gap + box_w + gap + w_btn + gap + w_btn
        x0 = int((W - total) / 2)
        # 手牌一行切换
        mode = self.app.settings["hand_mode"]
        self.add_widget(FlatButton("手牌一行" if mode == "column" else "手牌竖排",
                                   YEL, self._on_toggle_mode, text_color=YEL_TXT,
                                   pos=(x0, y), size=(w_btn, hs)))
        bx = x0 + w_btn + gap
        self._add_panel((bx, y, box_w, box_h), color=YEL,
                        border=(0.05, 0.05, 0.05, 0.9), radius=6)
        order = [Suit.SPADE, Suit.HEART, Suit.CLUB, Suit.DIAMOND]
        for i, s in enumerate(order):
            has = self.mgr.has_sf(s)
            sym = SUIT_SYM[s]
            col = (0.85, 0.12, 0.12, 1) if s.is_red else (0.10, 0.10, 0.12, 1)
            if has:
                txt, ccol = f"{_sym(sym)}{self.mgr.sf_count(s)}", col
                cb = (lambda s=s: self._on_sf(s))
            else:
                txt, ccol, cb = _sym(sym), (0.55, 0.55, 0.58, 1), None
            self.add_widget(SuitIcon(txt, ccol, cb,
                                     pos=(int(bx + i * slot), y),
                                     size=(int(slot), box_h)))
        # 整理手牌 / 恢复手牌（同浅黄底）
        ax = bx + box_w + gap
        self.add_widget(FlatButton("整理手牌", YEL, self._on_arrange, text_color=YEL_TXT,
                                   pos=(ax, y), size=(w_btn, hs)))
        rx = ax + w_btn + gap
        self.add_widget(FlatButton("恢复手牌", YEL, self._on_restore, text_color=YEL_TXT,
                                   pos=(rx, y), size=(w_btn, hs)))

    # ---- 右侧：出牌 / 不出 两个大按钮（右缘与下家头像右边(0.980)齐平，宽为原 3/4）
    def _build_actions(self):
        g = self.game
        x1, y1, w1, h1 = self._rect(0.875, 0.280, 0.980, 0.380)
        y1 = int(y1 + h1 * 0.5)
        self._btn_play = FlatButton("出牌", BLUE, self._on_play,
                                    enabled=self._confirm_enabled(),
                                    pos=(x1, y1), size=(w1, h1))
        self.add_widget(self._btn_play)
        x2, y2, w2, h2 = self._rect(0.875, 0.155, 0.980, 0.255)
        my_turn = g.phase == "playing" and g.turn == 0
        self._btn_pass = FlatButton(
            "不出", ORANGE, self._on_pass,
            enabled=(my_turn and g.last_play is not None and g.last_play[0] != 0),
            pos=(x2, y2), size=(w2, h2))
        self.add_widget(self._btn_pass)

    def _sync_action_buttons(self):
        if self._btn_play:
            self._btn_play.set_enabled(self._confirm_enabled())
        if self._btn_pass:
            g = self.game
            my_turn = g.phase == "playing" and g.turn == 0
            self._btn_pass.set_enabled(my_turn and g.last_play is not None
                                       and g.last_play[0] != 0)

    # ---- 选牌交互：点击（竖排整列）+ 滑动连续选牌
    def _hand_card_at(self, pos):
        hit = None
        for wid in self._hand_widgets.values():   # 后绘制者在上层
            if wid.show_face and wid.collide_point(*pos):
                hit = wid
        return hit

    def _apply_card_touch(self, eid: int) -> bool:
        """处理一次点击：单张选中，再点取消。返回拖动目标状态。"""
        new_state = eid not in self.mgr.selected
        self.mgr.set_selected(eid, new_state)
        self._update_sel_visuals()
        return new_state

    def _update_sel_visuals(self):
        for eid, wid in self._hand_widgets.items():
            desired = eid in self.mgr.selected
            if wid.selected != desired:
                wid.selected = desired
                wid._redraw()
        self._sync_action_buttons()

    def on_touch_down(self, touch):
        wid = self._hand_card_at(touch.pos)
        if wid is not None and touch.button in ("left", None):
            self._drag_touched = {wid.card.eid}
            self._drag_target = self._apply_card_touch(wid.card.eid)
            return True
        consumed = super().on_touch_down(touch)
        if not consumed and touch.button in ("left", None):
            # 点击空白处：取消全部手牌选中
            if self.mgr.selected:
                self.mgr.clear_selection()
                self._update_sel_visuals()
            return True
        return consumed

    def on_touch_move(self, touch):
        if self._drag_target is not None:
            wid = self._hand_card_at(touch.pos)
            if wid is not None and wid.card.eid not in self._drag_touched:
                eid = wid.card.eid
                self._drag_touched.add(eid)
                self.mgr.set_selected(eid, self._drag_target)
                self._update_sel_visuals()
            return True
        return super().on_touch_move(touch)

    def on_touch_up(self, touch):
        self._drag_target = None
        return super().on_touch_up(touch)

    # ---- 交互回调
    def _on_arrange(self):
        """整理手牌：仅处理选中的牌。炸弹族（含同花顺）放左侧（按大小），非炸弹放右侧。"""
        status, _kind = self.mgr.arrange_selected()
        if status == "empty":
            self._toast_msg("请先选中要整理的牌")
            return
        if status == "invalid":
            self._toast_msg("选中的牌不能构成一首牌，未整理")
            return
        self.refresh()

    def _on_restore(self):
        """恢复手牌：未选牌=全部恢复；选中某组牌=只恢复该组。"""
        sel = self.mgr.selected
        bombs, combos = self.mgr.arranged_layout()
        idxs = [gi for gi, eids in bombs + combos if any(e in sel for e in eids)]
        self.mgr.restore_groups(idxs or None)
        self.mgr.clear_selection()
        self.refresh()

    def _on_new_match(self):
        try:
            self.game.start_match()
        except GameError as e:
            self._toast_msg(str(e))
            return
        self.mgr.clear_groups()
        self.mgr.clear_selection()
        self.mgr.sync()
        self._last_round = -1
        self._alerted = set()
        self.refresh()

    def _on_next_round(self):
        try:
            self.game.start_next_round()
        except GameError as e:
            self._toast_msg(str(e))
            return
        self.mgr.sync()
        self._last_round = -1
        self._alerted = set()
        self.refresh()

    def _on_toggle_mode(self):
        self.app.settings["hand_mode"] = "column" if self.app.settings["hand_mode"] == "row" else "row"
        save_settings(self.app.settings)
        self.refresh()

    def _on_sf(self, suit):
        combo = self.mgr.cycle_sf(suit)
        if combo is None:
            self._toast_msg("该花色无同花顺候选")
            return
        self.refresh()
        self._update_sel_visuals()

    def _on_play(self):
        g = self.game
        cards = self.mgr.selected_cards()
        if self._mode == "tribute":
            if len(cards) != 1:
                self._toast_msg("请选中恰好一张要进贡的最大牌")
                return
            try:
                g.submit_tribute(0, cards[0].eid)
            except GameError as e:
                self._toast_msg(str(e))
            self.refresh()
            return
        if self._mode == "return":
            if len(cards) != 1:
                self._toast_msg("请选中恰好一张要还贡的牌")
                return
            try:
                g.submit_return(0, cards[0].eid)
            except GameError as e:
                self._toast_msg(str(e))
            self.refresh()
            return
        if not cards:
            self._toast_msg("请先选择要出的牌")
            return
        try:
            combo = g.play(0, cards)
            self.mgr.commit_play(combo)
        except (IllegalPlayError, NotPlayerTurnError, GameError) as e:
            self._toast_msg(f"非法出牌：{e}")
        self.refresh()

    def _on_pass(self):
        g = self.game
        try:
            g.pass_play(0)
        except GameError as e:
            self._toast_msg(str(e))
        self.refresh()

    # ---- 模式/提示/驱动
    def _confirm_enabled(self) -> bool:
        """右侧「出牌」按钮：进贡/还贡阶段一律置灰不高亮。"""
        g = self.game
        if g.phase != "playing" or g.turn != 0:
            return False
        if time.monotonic() < self._tribute_hold_until:
            return False   # 贡牌展示期内暂不进入牌局
        cards = self.mgr.selected_cards()
        prev = g.last_play[1] if g.last_play else None
        return can_play(cards, prev, g.level) is not None

    def _update_mode(self):
        g = self.game
        self._mode = "play"
        if g.phase == "tribute":
            pending_givers = [pid for pid, _ in g.tribute_pairs if pid not in g.tribute_done]
            awaiting_receivers = [r for r in g.tribute_done.values() if r not in g.return_done]
            if 0 in pending_givers:
                self._mode = "tribute"
            elif 0 in awaiting_receivers:
                self._mode = "return"

    def _update_alerts(self):
        g = self.game
        self._update_mode()
        if g.round_number != self._last_round:
            self._last_round = g.round_number
            self._alerted = set()
        if self.app.settings["low_card_alert"] and g.phase == "playing":
            for p in range(4):
                n = g.hand_count(p)
                if n <= 6 and p not in self._alerted and p not in g.finish_order:
                    self._alerted.add(p)
                    who = "你" if p == 0 else PLAYER_NAMES[p]
                    self._toast_msg(f"提示：{who} 只剩 {n} 张牌！")
                    break

    def _pump(self, dt):
        g = self.game
        if g.phase in ("match_over", "round_over"):
            return          # 等待结算画面上的「下一局/再来一局」按钮
        if g.phase == "tribute":
            # 先推进所有 AI 的进贡/还贡任务（双下时两家进贡都要展示），
            # 仅在轮到「我」的任务时才等待人类
            if self.app.ai_tribute_step():
                self.mgr.sync()
                self.refresh()
            return
        if time.monotonic() < self._tribute_hold_until:
            return          # 贡牌展示期内不进牌局
        if self._tribute_hold_until:
            self._tribute_hold_until = 0.0
            g.begin_round()   # 牌局正式开始：清除贡牌/抗贡展示
            self.refresh()
        if g.turn != 0:
            if not self._ai_scheduled:
                self._ai_scheduled = True
                Clock.schedule_once(self._ai_step, self.app.settings["ai_delay"])

    def _ai_step(self, dt):
        self._ai_scheduled = False
        g = self.game
        if g.phase == "playing" and g.turn != 0:
            pid = g.turn
            kind, cards = choose_action(g, pid, self.app.profiles[pid])
            if kind == "play":
                g.play(pid, cards)
            else:
                g.pass_play(pid)
            self.mgr.sync()
            self.refresh()

    def _toast_msg(self, text):
        W, H = Window.width, Window.height
        if self._toast:
            self.remove_widget(self._toast)
        t = _label(text, size=18, color=(1, 1, 0.5, 1), bold=True)
        t.size_hint = (None, None)
        t.pos = (int(W * 0.25), int(H * 0.42))
        t.size = (int(W * 0.5), 40)
        self.add_widget(t)
        self._toast = t
        Clock.schedule_once(self._kill_toast, 2.2)

    def _kill_toast(self, dt):
        if self._toast:
            self.remove_widget(self._toast)
            self._toast = None


# ---------------------------------------------------------------- 设置页（含规则说明）
class SettingsScreen(Screen):
    def __init__(self, app, **kw):
        super().__init__(**kw)
        self.app = app

    def on_enter(self):
        self.clear_widgets()
        W, H = Window.width, Window.height
        self.add_widget(Panel(pos=(0, 0), size=(W, H), color=WOOD_DARK, border=WOOD_DARK, radius=0))
        if os.path.exists(BG_PATH):
            self.add_widget(KImage(source=BG_PATH, pos=(0, 0), size=(W, H),
                                   size_hint=(None, None), allow_stretch=True,
                                   keep_ratio=False))
        scroll = ScrollView(size_hint=(None, None),
                            pos=(W * 0.25, H * 0.05), size=(W * 0.5, H * 0.9))
        box = BoxLayout(orientation="vertical", size_hint=(None, None),
                        spacing=12, padding=14)
        box.width = scroll.width
        box.bind(minimum_height=box.setter("height"))
        scroll.add_widget(box)
        self.add_widget(scroll)
        s = self.app.settings

        box.add_widget(_label("规则与设置", size=26, bold=True, color=GOLD,
                              size_hint=(1, None), height=44))

        row1 = BoxLayout(orientation="horizontal", size_hint=(1, None), height=40, spacing=10)
        row1.add_widget(_label("剩余牌数报牌阈值：", size=18, halign="left",
                               size_hint=(0.6, 1)))
        self.th_input = TextInput(text=str(s["report_threshold"]), font_size=20,
                                  font_name=_CJK_FONT,
                                  input_filter="digits", size_hint=(None, 1), width=80,
                                  foreground_color=(1, 1, 1, 1), background_color=(0.2, 0.2, 0.2, 1))
        row1.add_widget(self.th_input)
        row1.add_widget(_label("张（默认10）", size=16, halign="left", size_hint=(0.2, 1)))
        box.add_widget(row1)

        row2 = BoxLayout(orientation="horizontal", size_hint=(1, None), height=40, spacing=10)
        row2.add_widget(_label("剩余≤6张主动提示：", size=18, halign="left",
                               size_hint=(0.6, 1)))
        self.alert_cb = CheckBox(active=bool(s["low_card_alert"]), size_hint=(None, 1), width=40)
        row2.add_widget(self.alert_cb)
        row2.add_widget(_label("开启", size=16, halign="left"))
        box.add_widget(row2)

        row3 = BoxLayout(orientation="horizontal", size_hint=(1, None), height=44, spacing=10)
        row3.add_widget(_label("默认手牌展示模式：", size=18, halign="left",
                               size_hint=(0.6, 1)))
        self.mode_row_btn = FlatButton("一行", DARKGREY, lambda: self._set_mode("row"),
                                       size=(90, 36),
                                       enabled=(s["hand_mode"] == "row"))
        self.mode_col_btn = FlatButton("竖排", DARKGREY, lambda: self._set_mode("column"),
                                       size=(90, 36),
                                       enabled=(s["hand_mode"] == "column"))
        row3.add_widget(self.mode_row_btn)
        row3.add_widget(self.mode_col_btn)
        box.add_widget(row3)

        box.add_widget(_label(
            "操作说明：点击选牌（单张选中，再点取消）；按住滑动连续选牌/取消；"
            "点击空白处取消全部选中。\n"
            "整理手牌：仅整理选中的牌，炸弹与同花顺放左侧、其它牌型放右侧，"
            "不能构成一首牌则不整理。\n恢复手牌：未选中时恢复全部已整理的牌；"
            "选中某组牌时只恢复该组。\n剩牌数：超过报牌阈值时显示「不够报牌」，"
            "达到阈值后显示具体张数。",
            size=14, color=(0.85, 0.9, 0.8, 1), halign="left", valign="top",
            size_hint=(1, None), height=110))

        box.add_widget(_label(
            "[b]掼蛋 V1.0 规则说明[/b]\n"
            "· 两副牌共108张，4人各27张，0/2为一家，1/3为一家。\n"
            "· 牌型：单张/对子/三同张/三带二/顺子(5)/连对(3对)/钢板(2组三连)/炸弹(4-8)/同花顺/天王炸。\n"
            "· 逢人配：红桃级牌可替代除大小王外任意点数，允许参与同花顺并记录替代点数。\n"
            "· 压制：同型比大小；炸弹族 4炸<5炸<同花顺<6炸<7炸<8炸<天王炸。\n"
            "· 升级：双下+3、头游+三游+2、头游+末游+1，封顶A；打A且头游即获胜。\n"
            "· 一方两家全部出完即本局结束，未打完的两家直接记末游。\n"
            "· 进贡：末游进最大牌(逢人配除外)给头游，双下三游另进二游；持双大王可抗贡。\n"
            "· 还贡：受贡方任意还一张。接风：出完者无人压则其对家首出。\n"
            f"[color=ffd27f]集中规则参数[/color]：顺子张数={RuleConfig.STRAIGHT_LENGTH}，"
            f"连对={RuleConfig.PAIR_CHAIN_PAIRS}对，钢板={RuleConfig.TRIPLE_CHAIN_SETS}组，"
            f"炸弹={RuleConfig.BOMB_MIN_SIZE}-{RuleConfig.BOMB_MAX_SIZE}张，"
            f"逢人配花色={RuleConfig.WILD_SUIT.cn_name}。",
            size=14, color=(0.95, 0.95, 0.9, 1), halign="left", valign="top",
            size_hint=(1, None), height=300))

        save = FlatButton("保存设置", GREEN, self._on_save,
                          size_hint=(1, None), height=50)
        box.add_widget(save)
        back = FlatButton("返回牌桌", BLUE, lambda: self.app.goto("table"),
                          size_hint=(1, None), height=50)
        box.add_widget(back)

    def _set_mode(self, m):
        self.app.settings["hand_mode"] = m
        self.mode_row_btn.set_enabled(m == "row")
        self.mode_col_btn.set_enabled(m == "column")

    def _on_save(self):
        try:
            v = int(self.th_input.text)
            self.app.settings["report_threshold"] = max(1, min(27, v))
        except Exception:
            pass
        self.app.settings["low_card_alert"] = bool(self.alert_cb.active)
        save_settings(self.app.settings)
        self.app.table.refresh()
        self.app.goto("table")


# ---------------------------------------------------------------- App
class GuandanApp(App):
    title = "掼蛋 Guandan"

    def __init__(self, seed=None, **kw):
        super().__init__(**kw)
        self.settings = load_settings()
        rng = random.Random(seed)
        self.game = GuandanGame(rng=rng)
        self.mgr = HandManager(self.game, pid=0)
        self.profiles = [None, AIProfile("aggressive"), AIProfile("balanced"),
                         AIProfile("conservative")]
        self.sm = ScreenManager(transition=NoTransition())
        self.table = TableScreen(self, name="table")
        self.settings_screen = SettingsScreen(self, name="settings")
        self.sm.add_widget(self.table)
        self.sm.add_widget(self.settings_screen)

    def build(self):
        Window.min_size = (900, 560)
        Window.show_touches = False      # 关闭触摸白圈指示器
        Window.bind(on_resize=self._on_resize)
        return self.sm

    def _on_resize(self, *a):
        if self.sm.current == "table":
            Clock.schedule_once(lambda dt: self.table.refresh(), 0)

    def goto(self, name):
        self.sm.current = name

    def ai_tribute_step(self) -> bool:
        """推进一个 AI 进贡/还贡动作；仅剩人类任务（或全部完成）时返回 False。

        顺序：先完成全部进贡（含两家进贡都要展示），再依次还贡；
        人类的任务由界面按钮触发，本方法只处理 AI 的任务。
        """
        g = self.game
        from ai import choose_tribute, choose_return
        pending = [(gg, rr) for gg, rr in g.tribute_pairs if gg not in g.tribute_done]
        if pending:
            ai_giver = next((gg for gg, _ in pending if gg != 0), None)
            if ai_giver is None:
                return False   # 只剩人类进贡，等按钮
            g.submit_tribute(ai_giver, choose_tribute(g, ai_giver))
            return True
        awaiting = [r for r in g.tribute_done.values() if r not in g.return_done]
        ai_rec = next((r for r in awaiting if r != 0), None)
        if ai_rec is None:
            return False       # 只剩人类还贡（或已全部完成）
        g.submit_return(ai_rec, choose_return(g, ai_rec))
        return True

    def on_stop(self):
        save_settings(self.settings)


def run(seed=None):
    GuandanApp(seed=seed).run()


if __name__ == "__main__":
    run()
