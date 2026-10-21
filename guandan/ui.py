"""掼蛋 Kivy 横屏界面与交互逻辑。

严格分层：本模块只负责展示与交互，所有牌型判断/合法性校验统一调用 rules 引擎，
所有流程状态来自 game，展示分组/选牌/候选来自 arrange。UI 不实现任何规则校验。

布局区域（参考主流掼蛋手游横屏布局）：
- 背景：程序目录下「background.jpg」拉伸铺满整窗（缺失时退回木纹底色）。
- 顶部：左上信息区；右上「开新局 / 主界面 / 复盘」等宽按钮（整组右缘贴屏幕
  右边，水平中线与对家头像中线对齐）。
- 四名玩家：卡通头像（对家头像上沿与右上按钮区齐平，名字/剩牌数在头像左右
  两侧贴图标、间隔半个字；上家/下家与出牌区水平中线对齐；我方在手牌区左侧；
  超过报牌阈值显示「未报数」）；每人一个出牌分区（全部位于中央出牌矩形内，
  互不重叠）。
- 中央出牌区：从对家到手牌区、上家到下家之间的矩形；每轮打出的牌横排显示在
  各家分区（牌序按 rules.display_order 统一规范：序列类点数升序、配牌占
  顶替位，三带二三张在前，同点黑红梅方，与复盘回放一致），展示牌高度为
  手牌牌高的 3/4（各家统一、不压缩牌宽）；上家展示从
  分区左缘开始、下家贴分区右缘，牌多时缩小间距堆叠（不遮挡头像）；轮到某家
  出牌时清除该家上一轮展示；一轮结束（各家均不出）后整体保留 1.5s 再清场，
  保证「不出」文字可见；进贡/还贡共用按钮居中显示；抗贡时在对应玩家分区显示
  「抗贡！」；行动徽章为各家分区中央的圆形「出牌」，轮到我方时「不出/出牌」
  两按钮对称排布在我方分区行动徽章两侧（水平中线对齐，高度与徽章相同、
  间距为一个徽章宽度），与徽章同现同隐；贡牌阶段我方任务只显示一个
  「进贡/还贡」按钮（置于我方分区中央，即原徽章中点），不显示徽章与按钮。
- 局终：出牌区显示头游/二游/三游/末游（一方两家出完时其余两家均记末游）+
  「X方胜，下局打X」结算 + 下一局/主界面。
- 底部：手牌区（右缘至右侧竖列按钮左侧；无底色；炸弹/同花顺组在左、
  普通手牌按点数分列居中、非炸弹牌组在右；列内纵向重叠仅露出每张牌角标，
  同点列从下到上按黑桃/红桃/梅花/方块排列；牌面尺寸按「6 张一列恰好到
  出牌区下沿」锁定，不随列长变化，7 炸可向上突破，整体过宽时列间水平重叠）。
- 右侧竖列（原出牌按钮位置）：整理/恢复手牌合并按钮、「教学」按钮（宽均为
  原出牌按钮 2/3、右缘齐平，高度=出牌按钮高度，间距=高度之半；教学按钮在
  原「恢复手牌」位置）与同花顺识别框（距教学按钮下沿两个按钮高度、高与宽
  相等的正方形框，两列矩形格、每列两个花色，黑边框、实心/空心花色，框内仅
  显示花色符号、字形高=框整体高度的一半）自上而下排列；合并按钮随选中状态
  切换文字与激活态（2026-10-19）：未选中=无整理组时「整理手牌」（灰）/
  有整理组时「恢复手牌」（全部恢复）；选中已整理组=「恢复手牌」（仅恢复
  所选组）；选中构成合法牌型=「整理手牌」；其余选中=「整理手牌」（灰）。
  程序图标为根目录 logo.png。

牌面：白底圆角；级牌浅绿填充、文字花色为常规红黑配色；中央花色图案宽约牌面
3/4；左上角标（点数+花色）文字高度为牌高 36%；王牌中央画小猫（大王红色、小王
黑色），左上角标竖排「JOKER」（逐字母居中）。

交互：点击选牌（单张选中，再点取消）；双击某牌快速选中其所属牌组（已整理组
或同点数整列）；按住滑动连续选牌/取消；点击空白处取消全部选中；选中牌保留
颜色高亮（无弹出动画）。按钮为圆角矩形、字体微软雅黑、
字号为按钮高度 60%，按下有下沉动画。全部按窗口尺寸自适应。

设置页：仅可调整项——报牌阈值、低牌提示、音效开关、背景乐选择（关/曲目一/
曲目二）、AI 牌力（初级/中级/高级/夯级单选）；控件尺寸随窗口高度比例缩放。
入口在主菜单（牌桌不再有设置按钮），保存/返回均回主菜单。

主菜单：启动初始屏——单机游戏 / 好友组局 / 设置 / 复盘 / 退出游戏；单机
牌局进行中时「单机游戏」变「返回游戏」且「好友组局」置灰（联机与单机同
时仅进行一个）；好友组局进入联机大厅（room_ui），有进行中的联机会话时变
「返回房间」。
"""
from __future__ import annotations

import json
import os
import random
import sys
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

from ai import AIProfile, explain_choice
from registry import choose_detail as strategy_choose_detail, resolve_seat
from arrange import HandManager
from cards import BIG_JOKER, Card, Suit, RANK_LABELS
from game import GameError, GuandanGame, IllegalPlayError, NotPlayerTurnError
from replay import Recorder, apply_net_act
import calib
import decision
import observer
import scorer
from rules import can_play, classify, display_order
from sound import SoundManager, event_for_combo

# 扑克花色符号（Windows 下 msyh 缺 ♠♥♣♦ 字形，用 Segoe UI Symbol 渲染；
# Android 下统一使用随包内嵌的 Noto Sans CJK，其自带花色字形）
SUIT_SYM = {Suit.SPADE: "♠", Suit.HEART: "♥", Suit.CLUB: "♣", Suit.DIAMOND: "♦"}
if getattr(sys, "frozen", False):
    # PyInstaller 打包：只读资源在解包目录 _MEIPASS；可写文件（settings.json）放 exe 旁
    _HERE = os.path.dirname(sys.executable)
    _ASSET_DIR = getattr(sys, "_MEIPASS", _HERE)
else:
    _HERE = os.path.dirname(os.path.abspath(__file__))
    _ASSET_DIR = _HERE
_NOTO = os.path.join(_ASSET_DIR, "fonts", "NotoSansCJKsc-Regular.otf")
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
# 决策快照面板仿宋字体（Windows simfang；缺失时回退当前 CJK 字体）
_FS_FONT = r"C:\Windows\Fonts\simfang.ttf"
if not os.path.exists(_FS_FONT):
    _FS_FONT = _CJK_FONT
if os.path.exists(_SYM_FONT):
    LabelBase.register("suitfont", _SYM_FONT)


def _sym(sym: str) -> str:
    """花色符号 markup：用 suitfont 字体渲染，缺字体时退回原字符。"""
    return f"[font=suitfont]{sym}[/font]" if os.path.exists(_SYM_FONT) else sym

# 界面文字统一字号：出牌区/结算/进贡画面及头像旁文字均以右上「开新局」按钮
# 的文字大小为基准，默认 1 倍；调整此系数可整体放大/缩小
UI_TEXT_SCALE = 1.0

# 角色名（0=人类玩家下方，1=下家右侧，2=对家上方，3=上家左侧）
PLAYER_NAMES = {0: "我", 1: "下家", 2: "对家", 3: "上家"}

# ---------------------------------------------------------------- 资源
_ASSETS = os.path.join(_HERE, "assets")
# 头像：0=我(缅因猫) 1=下家(无毛猫) 2=对家(田园猫) 3=上家(波斯猫)
# 文件名必须为 ASCII：buildozer 打包 APK 时中文文件名不会进入资产包
AVATAR_PATHS = {pid: os.path.join(_ASSET_DIR, f"avatar{pid}.png") for pid in range(4)}

WOOD = (0.44, 0.29, 0.16, 1)          # 木纹棕
WOOD_DARK = (0.30, 0.19, 0.10, 1)
GOLD = (0.85, 0.68, 0.25, 1)
ORANGE = (0.90, 0.45, 0.10, 1)
BLUE = (0.15, 0.45, 0.80, 1)
GREEN = (0.20, 0.60, 0.30, 1)
PANEL = (0.16, 0.34, 0.22, 1)         # 牌桌绿
WHITE = (0.97, 0.97, 0.94, 1)
LEVEL_FACE = (0.78, 0.93, 0.78, 1)    # 级牌浅绿牌面
SEL_FACE = (1.0, 0.90, 0.45, 1)       # 选中牌面
BG_PATH = os.path.join(_ASSET_DIR, "background.jpg")
LOGO_PATH = os.path.join(_ASSET_DIR, "logo.png")

SETTINGS_PATH = os.path.join(_HERE, "settings.json")
DEFAULT_SETTINGS = {
    "report_threshold": 10,   # 10 张报数（文字显示）
    "low_card_alert": True,   # ≤6 张主动报（声音提醒）默认开启
    "ai_delay": 0.15,      # AI 出牌间隔（秒），较旧版 0.35 调短
    "level_start": 2,
    "sound": True,          # 音效开关（播放接口，暂无音频文件）
    "bgm": "track1",        # 背景乐默认播放曲目一：off | track1 | track2
    "ai_strength": "advanced",  # AI 牌力默认高级：novice | intermediate |
                               # advanced | value（classic_low/classic_high/pimc
                               # 保留在 registry 供 bench/测试调用，不进设置界面）
    "teaching": True,       # 教学面板：数据显示 + 出牌建议（右列「教学」按钮展开）
    "net_name": "",         # 好友联机昵称（大厅页使用，可留空自动起默认名）
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
    if data.get("ai_delay") == 0.35:      # 旧默认值迁移为调短后的新默认
        data["ai_delay"] = 0.15
    # 旧档位迁移（低/高/夯已退出设置界面）：低→初级，高/夯→高级
    _legacy = {"low": "novice", "high": "advanced", "pimc": "advanced"}
    if data["ai_strength"] in _legacy:
        data["ai_strength"] = _legacy[data["ai_strength"]]
    elif data["ai_strength"] not in ("novice", "intermediate", "advanced",
                                     "value"):
        data["ai_strength"] = "advanced"
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
                 enabled=True, text_color=(1.0, 1.0, 1.0), font_size=None,
                 font_name=None, **kw):
        kw.setdefault("size_hint", (None, None))
        super().__init__(**kw)
        self.text = text
        self.on_press = on_press
        self.enabled = enabled
        self.base_color = color
        self.text_color = tuple(text_color[:3])
        self._font_fs = int(font_size) if font_size else None   # 显式字号（不传=按尺寸自算）
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
        self._label = Label(text=text, font_name=font_name or _CJK_FONT,
                            bold=True,
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
        # 字号为按钮高度 60%（长文本按宽度收敛，防止溢出）；显式 font_size 优先
        est = sum(1.0 if ord(ch) > 0x2E80 else 0.55 for ch in self.text) or 1.0
        fs = self._font_fs if self._font_fs else h * 0.60
        self._label.font_size = max(9, int(min(fs, w * 0.94 / est)))

    def set_enabled(self, val: bool):
        self.enabled = val
        self._top_c.rgba = self.base_color if val else (0.45, 0.45, 0.45, 1)
        self._bd_c.rgba = self._shade(self.base_color if val else (0.45, 0.45, 0.45, 1), 0.60)
        self._label.color = self.text_color + (1 if val else 0.45,)

    def set_text(self, text: str):
        """运行时改按钮文字（重算字号防溢出）。"""
        self.text = text
        self._label.text = text
        self._place()

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
    """同花顺栏花色图标：无底色无边框，仅花色符号（无数字），可点击。"""

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
        # 花色字形高约占字号 75%：格高=框高一半，取字号=格高 0.9333 倍，
        # 使字形高=框整体高度的 35%（0.75×0.9333=0.7=0.35×2）；
        # 估算宽度时剔除 [font=...] 标记，只统计可见字形
        plain, skip = "", False
        for ch in self._label.text:
            if ch == "[":
                skip = True
            elif ch == "]":
                skip = False
            elif not skip:
                plain += ch
        est = sum(1.0 if ord(ch) > 0x2E80 else 0.55 for ch in plain) or 1.0
        self._label.font_size = max(12, int(min(h * 0.9333, w / est)))

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
            # 竖排 JOKER 角标（大王红 / 小王黑，靠中央小猫颜色区分）；
            # 每个字母在角标栏内水平居中，竖排整齐
            fs = max(9, int(h * 0.11))
            band = int(fs * 1.30 * 5)
            self._corner.halign = "center"
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
            self._corner.halign = "left"
            self._corner.color = col
            self._pip.color = col
            wild = "*" if c.is_wild(self.level) else ""
            plain = f"{RANK_LABELS[c.rank]}{SUIT_SYM[c.suit]}{wild}"
            est = sum(1.0 if ord(ch2) > 0x2E80 else 0.55 for ch2 in plain)
            fs = max(9, min(int(h * 0.36), int(w * 0.98 / max(est, 1.0))))
            band = int(fs * 1.30)
            # 「10」角标不压缩字形，整体左移 1/3 数字宽度（数字宽≈0.55 字号）
            shift = (0.55 * fs / 3.0) if c.rank == 10 else 0.0
            self._corner.font_size = fs
            self._corner.size = (int(w * 1.8), band)
            self._corner.pos = (int(x + w * 0.08 - shift),
                                int(y + h - band + fs * 0.33))
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


def _label(text, size=16, color=(1, 1, 1, 1), bold=False, widget_size=None,
           font_name=None, **kw):
    kw.setdefault("halign", "center")
    kw.setdefault("valign", "middle")
    lb = Label(text=text, font_name=font_name or _CJK_FONT, font_size=size,
               bold=bold, color=color, markup=True, **kw)
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
    # 各玩家头像/名字/剩牌数（头像尺寸=原 1.5 倍、中心不变；对家头像中心与右上
    # 按钮水平中线(0.942)对齐、上沿略越出屏幕（PNG 自带 10% 透明边距，内容不裁切），
    # 名字/剩牌数在头像左右两侧贴图标，与头像横向轴线居中；其余：名字在头像上方、
    # 剩牌数在头像下方（留半字间隔），均与头像水平居中；窗口比例）
    P_LAYOUT = {
        2: dict(avatar=(0.452, 0.873, 0.548, 1.011), name=(0.320, 0.9085, 0.460, 0.9765),
                count=(0.540, 0.9085, 0.680, 0.9765)),
        3: dict(avatar=(0.005, 0.628, 0.095, 0.766), name=(-0.010, 0.774, 0.110, 0.842),
                count=(-0.010, 0.552, 0.110, 0.620)),
        1: dict(avatar=(0.905, 0.628, 0.995, 0.766), name=(0.890, 0.774, 1.010, 0.842),
                count=(0.885, 0.552, 1.015, 0.620)),
        0: dict(avatar=(0.005, 0.202, 0.095, 0.340), name=(-0.010, 0.348, 0.110, 0.416),
                count=(-0.010, 0.126, 0.110, 0.194)),
    }

    def __init__(self, app, **kw):
        super().__init__(**kw)
        self.app = app
        self.game = app.game
        self.mgr = app.mgr
        self.me = 0                     # 本端人类座位号（联机由 net_setup 设置）
        self._alerted = set()
        self._last_round = -1
        self._toast = None
        self._ai_scheduled = False
        self._mode = "play"  # play | tribute | return
        self._prev_phase = self.game.phase
        self._tribute_hold_until = 0.0   # 贡牌展示保留期截止时刻
        self._recorder = None            # 牌局记录（结算打分数据源，见 scorer）
        self._decisions = None           # 决策快照记录器（decision.DecisionLogger）
        self._calib = None               # 夯级校准检测（calib.CalibrationChecker）
        self._replay_saved_path = None   # 本场复盘文件已保存路径（重复保存覆盖）
        self._scores = None              # 打分缓存 {pid: 0-100}（跃迁帧算一次）
        self._prev_trick: Dict[int, dict] = {}    # 上次刷新时的各家出牌展示
        self._trick_snapshot: Dict[int, dict] = {}  # 一轮结束时的展示快照
        self._trick_hold_until = 0.0     # 一轮结束后展示保留期截止时刻
        self._hand_widgets: Dict[int, CardWidget] = {}
        self._drag_target: Optional[bool] = None
        self._drag_touched: Set[int] = set()
        self._btn_play: Optional[FlatButton] = None
        self._btn_pass: Optional[FlatButton] = None
        self._teach_open = False         # 教学面板展开状态（折叠面板 slot）
        self._tracker = None             # 教学数据跟踪（已出牌，比赛起点挂载）
        self._attach_recorder()          # 启动首局也录制（否则首局无复盘/打分）
        self._attach_tracker()
        self.refresh()
        self._pump_event = None          # 主循环泵：进入牌桌才调度（on_enter）

    def on_enter(self):
        """进入牌桌才启动主循环泵：主菜单停留期间牌局冻结、不后台自动行牌。"""
        if self._pump_event is None:
            self._pump_event = Clock.schedule_interval(self._pump, 0.12)

    def on_leave(self):
        if self._pump_event is not None:
            self._pump_event.cancel()
            self._pump_event = None

    # ---- 联机座位映射（阶段三好友联机）：引擎 pid 全局唯一（座位=pid），
    #      各端 UI 以「我」所在座位为基准换算牌桌方位
    def _is_net(self) -> bool:
        return self.app.net_session is not None

    def _is_guest(self) -> bool:
        s = self.app.net_session
        return s is not None and not s.is_host

    def _pos(self, pid: int) -> int:
        """牌桌方位（0=我/下 1=下家/右 2=对家/上 3=上家/左）= (pid - 我) mod 4。"""
        return (pid - self.me) % 4

    def _pid(self, pos: int) -> int:
        """由牌桌方位反推引擎 pid。"""
        return (self.me + pos) % 4

    def _display_name(self, pid: int) -> str:
        """玩家显示名：联机用座位昵称（AI 座位显示 AI），单机用相对称谓。"""
        s = self.app.net_session
        if s is not None:
            info = next((x for x in s.seats_snapshot() if x["seat"] == pid), None)
            if info is not None:
                return "AI" if info["is_ai"] else (info.get("name") or "玩家")
            return f"P{pid}"
        return PLAYER_NAMES[self._pos(pid)]

    def _seat_sid(self, pid: int) -> str:
        """AI 档位：联机用该座位的补位档位，单机跟随全局设置。"""
        s = self.app.net_session
        if s is not None:
            info = next((x for x in s.seats_snapshot() if x["seat"] == pid), None)
            if info is not None and info.get("tier"):
                return info["tier"]
        return resolve_seat(self.app.settings, pid)

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

    # ---- 中央出牌区四家分区（互不重叠；高度容纳 3/4 手牌牌高的展示牌）
    def _play_zones(self) -> Dict[int, Tuple[float, float, float, float]]:
        """各家出牌分区，按引擎 pid 为键（方位经 (pid-我)%4 换算）。"""
        rx, ry, rw, rh = self._rect(*self.PLAY_RECT)
        rects = {
            2: (rx + rw * 0.30, ry + rh * 0.56, rw * 0.40, rh * 0.44),   # 对家：上中
            0: (rx + rw * 0.30, ry, rw * 0.40, rh * 0.44),               # 我：下中
            3: (rx, ry + rh * 0.28, rw * 0.24, rh * 0.44),               # 上家：左中
            1: (rx + rw * 0.76, ry + rh * 0.28, rw * 0.24, rh * 0.44),   # 下家：右中
        }
        return {self._pid(pos): r for pos, r in rects.items()}

    def _hand_ch(self) -> float:
        """手牌牌高：按「6 张一列（步距 0.3 牌高）恰好到手牌区下沿至出牌区
        下沿」锁定，不随列长/列数变化。"""
        hy = int(0.012 * Window.height)
        ry = self._rect(*self.PLAY_RECT)[1]
        return max(60.0, (ry - hy - 5) / 2.5)

    def _ref_bh(self) -> int:
        """出牌按钮参考高度 = 我方分区无内容时行动徽章直径
        （与 _build_actions 中 bh = 徽章 d 的公式一致）。"""
        W, H = Window.width, Window.height
        zh = (self.PLAY_RECT[3] - self.PLAY_RECT[1]) * H * 0.44
        return int(min(0.050 * W, zh * 0.92))

    # ---- 主刷新
    def refresh(self):
        g = self.game
        if g.phase != "playing":
            self._trick_hold_until = 0.0
            self._trick_snapshot = {}
        # 一轮牌权结算（展示被清空）：保留上一轮各家「不出」/出牌展示 1.5s，
        # 否则最后一家过牌的「不出」根本来不及显示
        if g.phase == "playing" and not g.trick_displays and self._prev_trick \
                and time.monotonic() >= self._trick_hold_until:
            self._trick_snapshot = self._prev_trick
            self._trick_hold_until = time.monotonic() + 1.5
        self._prev_trick = dict(g.trick_displays) if g.phase == "playing" else {}
        # 进入牌局时若还有贡牌/抗贡展示：保留 2s 后再正式开始行牌
        if g.phase == "playing" and self._prev_phase != "playing" and \
                (g.tribute_displays or g.tribute_refused_by):
            self._tribute_hold_until = time.monotonic() + 2.0
        # 局终结算音（仅在「playing → 结算」的跃迁时播一次）
        if g.phase in ("round_over", "match_over") and self._prev_phase == "playing":
            my_team = self.me % 2
            if g.phase == "match_over":
                self.app.sounds.play("match_win" if g.match_winner == my_team
                                     else "match_lose")
                # 联机（阶段四）：主机在赛末跃迁时分发权威复盘记录
                s = self.app.net_session
                if s is not None and s.is_host and self._recorder is not None:
                    s.broadcast_end(self._recorder.to_dict())
            else:
                wt = g.round_result["winner_team"] if g.round_result else 0
                self.app.sounds.play("round_win" if wt == my_team else "round_lose")
            # 结算打分（专家对比法重演）：下一帧算完缓存，结算页读缓存展示
            if self._recorder is not None and self._scores is None:
                Clock.schedule_once(lambda dt: self._compute_scores(), 0)
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
        self._badge_geo = {}    # 行动徽章几何（_build_play_areas 填充）
        self._build_play_areas()
        self._build_info()
        self._build_sf_strip()
        self._build_actions()
        self._build_topbar()
        self._build_teaching()
        self._update_alerts()

    # ---- 统一文字字号：以右上「开新局」按钮的文字为基准（FlatButton 公式）
    def _text_fs(self) -> int:
        H = Window.height
        bh = 0.063 * H                        # 开新局按钮高（_build_topbar）
        bw = 2.7 * bh                         # 开新局按钮宽
        fs = max(9, int(min(bh * 0.60, bw * 0.94 / 3)))   # 按钮文字（3 字）
        return max(9, int(fs * UI_TEXT_SCALE))

    # ---- 左上信息区：「我方打X」「对方打X」两个带底色文本框，攻方高亮
    def _build_info(self):
        g = self.game
        fs = self._text_fs()
        my_team = self.me % 2
        for i, (txt, team) in enumerate(
                (("我方打" + RANK_LABELS[g.levels[my_team]], my_team),
                 ("对方打" + RANK_LABELS[g.levels[1 - my_team]], 1 - my_team))):
            fx1 = 0.005 + i * 0.150
            x, y, w, h = self._rect(fx1, 0.905, fx1 + 0.142, 0.995)
            active = g.playing_team == team
            self.add_widget(Panel(pos=(x, y), size=(w, h),
                                  color=(0.99, 0.88, 0.55, 1) if active else PANEL,
                                  radius=8,
                                  border=GOLD if active else (0.45, 0.45, 0.45, 1)))
            self._add_label((x, y, w, h), f"[b]{txt}[/b]", size=fs,
                            color=(0.30, 0.16, 0.02, 1) if active else WHITE, bold=True)

    # ---- 右上角：开新局 / 主界面 / 复盘（等宽，水平中线与对家头像中线对齐）
    def _build_topbar(self):
        H = Window.height
        # 三键恢复原布局：等宽 + 等间距，整组右缘贴屏幕右边
        # （设置已迁移至主菜单，退出按钮改为返回主界面）
        h = int(0.063 * H)
        gap = int(0.012 * H)
        w3 = int(2.7 * h)                 # 三字按钮宽
        left = int(Window.width * 0.995) - 3 * w3 - 2 * gap
        right_edge = int(Window.width)
        btns = [
            ("开新局", self._on_new_match),
            ("主界面", lambda: self.app.goto("menu")),
            ("复盘", lambda: self.app.goto("replay")),
        ]
        n = len(btns)
        bw = (right_edge - left - (n - 1) * gap) // n
        left += (right_edge - left - (n - 1) * gap) % n   # 整除余数并入起点，右沿精确贴边
        av = self.P_LAYOUT[2]["avatar"]
        y = int((av[1] + av[3]) / 2 * H - h / 2)      # 与对家头像水平中线对齐
        GRN = (0.80, 0.93, 0.80, 1)          # 浅绿底
        GRN_TXT = (0.08, 0.26, 0.10, 1)      # 浅绿底上的深色文字
        for i, (text, cb) in enumerate(btns):
            x = left + i * (bw + gap)
            self.add_widget(FlatButton(text, GRN, cb, text_color=GRN_TXT,
                                       pos=(x, y), size=(bw, h)))

    # ---- 教学面板（折叠 slot，阶段三挂载点）：左缘「教学」按钮 + 展开时的
    #      左下三模块网格面板。位置（用户 2026-10-13 规格）：
    #      上=上家「未报数」框下沿再隔 0.2 倍「未报数」行距；左=屏幕左沿隔
    #      0.2 倍行距；下=屏幕下沿隔 0.2 倍行距；右=「对方打X」按钮竖直中线。
    #      手牌区左沿避让面板（见 _build_hand），settings["teaching"] 关闭时整组隐藏。
    TEACH_PANEL_RIGHT = 0.226    # 「对方打X」框中线（fx1=0.155+宽0.142/2，窗口比）

    @staticmethod
    def _teach_gap():
        """0.2 倍「未报数」行距（上家剩牌框高 0.068H，P_LAYOUT[3]["count"]）。"""
        return int(0.2 * 0.068 * Window.height)

    def _toggle_teaching(self):
        self._teach_open = not self._teach_open
        self.refresh()

    def _build_teaching(self):
        if not self.app.settings.get("teaching", True):
            return
        W, H = Window.width, Window.height
        gap = self._teach_gap()
        x1, y1 = gap, gap
        x2 = int(self.TEACH_PANEL_RIGHT * W)
        y2 = int(self.P_LAYOUT[3]["count"][1] * H) - gap
        if self._teach_open:
            self._draw_teach_panel(x1, y1, x2 - x1, y2 - y1, gap)
        # 切换按钮：右侧竖列原「恢复手牌」位置（几何与整理/恢复合并按钮一致，
        # 由 _build_sf_strip 提供，尺寸/文字参照其设计；2026-10-19 用户规格）
        geo = getattr(self, "_sf_geo", None)
        if geo is None:
            return
        bx, by, bw, bh = geo
        self.add_widget(FlatButton("收起" if self._teach_open else "教学",
                                   GREEN, self._toggle_teaching,
                                   text_color=(1, 1, 1, 1),
                                   pos=(bx, by), size=(bw, bh)))

    def _draw_teach_panel(self, x1, y1, pw, ph, gap):
        """9 行三模块网格：1-2 核心牌剩余 / 3-6 已出牌型 / 7-9 推测预留。

        控件用现有 Panel(底色格)+Label 组合即可表达等宽列，无需新控件。
        """
        self.add_widget(Panel(pos=(x1, y1), size=(pw, ph), radius=8))
        pad = max(4, gap // 2)
        bgap = max(5, gap // 2)
        lh = (ph - 2 * pad - 2 * bgap) // 9
        fs = max(10, int(lh * 0.50))            # 数值字号
        fs_col = max(8, int(lh * 0.30))         # 列头/行头字号（容纳「32」两字）
        cw1 = (pw - 2 * pad) / 7.0              # 模块1 七等宽列（上下对齐）
        cw2 = (pw - 2 * pad) / 10.0             # 模块2 十等分：行头2格+数据8列
        head_w = 2 * cw2
        HEAD_C = (0.85, 0.82, 0.66, 1)
        BD_C = (0.55, 0.52, 0.40, 0.45)         # 模块边框色（同模块3）

        def cell(cx, cy, cw, ch, text, fsize, bold=False, color=WHITE):
            lb = _label(text, size=fsize, color=color, bold=bold)
            lb.size_hint = (None, None)
            lb.pos = (int(cx), int(cy))
            lb.size = (int(cw), int(ch))
            self.add_widget(lb)

        top = y1 + ph - pad
        # 模块1（第1-2行）：边框 + 列头 / 剩余张数（0=出完，加粗金色突出）
        self.add_widget(Panel(pos=(x1 + pad, top - 2 * lh),
                              size=(int(pw - 2 * pad), 2 * lh),
                              color=(0, 0, 0, 0), border=BD_C, radius=6))
        for i, htxt in enumerate(("王", "级", "配", "A", "K", "10", "5")):
            cell(x1 + pad + i * cw1, top - lh, cw1, lh, htxt, fs_col, color=HEAD_C)
        for i, v in enumerate(observer.Observer(self.game, self.me).core_residue()):
            dead = (v == 0)
            cell(x1 + pad + i * cw1, top - 2 * lh, cw1, lh, str(v), fs,
                 bold=dead, color=GOLD if dead else WHITE)
        # 模块2（第3-6行）：边框 + 标题行（两个空白格+8列）/ 三家已出情况
        m2 = top - 2 * lh - bgap
        self.add_widget(Panel(pos=(x1 + pad, m2 - 4 * lh),
                              size=(int(pw - 2 * pad), 4 * lh),
                              color=(0, 0, 0, 0), border=BD_C, radius=6))
        cell(x1 + pad, m2 - lh, head_w, lh, "", fs_col)     # 两个空白格（行头位）
        for i, c in enumerate(observer.COMBO_COLS):
            cell(x1 + pad + head_w + i * cw2, m2 - lh, cw2, lh, c, fs_col,
                 color=HEAD_C)
        tr = self._tracker
        for r, pos in enumerate((2, 3, 1)):   # 对家 / 上家 / 下家
            pid = self._pid(pos)
            ry = m2 - (r + 2) * lh
            cell(x1 + pad, ry, head_w, lh, {2: "对家", 3: "上家", 1: "下家"}[pos], fs_col)
            prow = tr.played[pid] if tr else [0] * 8
            for i in range(8):
                cell(x1 + pad + head_w + i * cw2, ry, cw2, lh,
                     str(prow[i]) if prow[i] > 0 else "", fs)
        # 模块3（第7-9行）：剩牌推测（阶段四：PlayTracker.predict_rows +
        # inference 纯函数，玩家0视角；无追踪器时回退预留提示）
        self.add_widget(Panel(pos=(x1 + pad, y1 + pad),
                              size=(int(pw - 2 * pad), 3 * lh),
                              color=(0, 0, 0, 0), border=BD_C,
                              radius=6))
        rows = tr.predict_rows(self.game, self.me) if tr else None
        if rows:
            for i, row in enumerate(rows):
                ry = y1 + pad + (2 - i) * lh
                cell(x1 + pad, ry, head_w, lh, row["label"], fs_col)
                cell(x1 + pad + head_w, ry, pw - 2 * pad - head_w, lh,
                     row["text"], fs)
        else:
            ph_lb = _label("剩牌推测 · 接口预留", size=fs_col,
                           color=(0.72, 0.72, 0.64, 1))
            ph_lb.size_hint = (None, None)
            ph_lb.pos = (x1 + pad, y1 + pad + (3 * lh - lh) // 2)
            ph_lb.size = (int(pw - 2 * pad), lh)
            self.add_widget(ph_lb)

    # ---- 四名玩家：头像 + 名字（上方）+ 剩牌数（下方）
    def _turn_badges(self) -> Dict[int, str]:
        g = self.game
        out: Dict[int, str] = {}
        if g.phase == "playing":
            # 联机时轮到远端人类：徽章显示「等待」（本端不出牌）
            if g.turn != self.me and self._is_net() and not (
                    self.app.net_session.is_host
                    and g.turn in self.app.net_session.ai_pids()):
                out[g.turn] = "等待"
            else:
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
        fs = self._text_fs()   # 头像旁文字：统一基准字号
        for pos, lay in self.P_LAYOUT.items():
            pid = self._pid(pos)
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
                self.add_widget(_label(self._display_name(pid)[:1], size=int(ah * 0.5),
                                       pos=(ax, ay), widget_size=(aw, ah)))
            # 名字（对家在头像左侧贴图标；其余在头像上方，16号）
            nkw = {"halign": "right"} if pos == 2 else {}
            self._add_label(self._rect(*lay["name"]), self._display_name(pid),
                            size=fs, color=WHITE, bold=True, **nkw)
            # 剩牌数（对家在头像右侧贴图标；其余在头像下方，16号；
            # 超过报牌阈值显示「未报数」）
            n = g.hand_count(pid)
            if n <= thr:
                ctxt, ccol = f"剩 {n} 张", (1.0, 0.85, 0.40, 1)
            else:
                ctxt, ccol = "未报数", (0.78, 0.78, 0.74, 1)
            ckw = {"halign": "left"} if pos == 2 else {}
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

        # 实时位次：玩家一出完牌即在其分区靠边界处显示名次（统一基准字号）
        if g.phase == "playing" and g.finish_order:
            ranks = ["头游", "二游", "三游", "末游"]
            fs = self._text_fs()
            for i, pid in enumerate(g.finish_order[:4]):
                pos = self._pos(pid)
                zx, zy, zw, zh = zones[pid]
                lw, lh = int(2.6 * fs), int(1.4 * fs)
                if pos == 2:      # 贴出牌区上边界
                    lx, ly = zx + (zw - lw) / 2, zy + zh - lh
                elif pos == 0:    # 贴出牌区下边界
                    lx, ly = zx + (zw - lw) / 2, zy
                elif pos == 3:    # 贴出牌区左边界
                    lx, ly = zx, zy + (zh - lh) / 2
                else:             # 贴出牌区右边界
                    lx, ly = zx + zw - lw, zy + (zh - lh) / 2
                self._add_label((int(lx), int(ly), lw, lh), f"[b]{ranks[i]}[/b]",
                                size=fs, color=(1.0, 0.85, 0.35, 1), bold=True)

        badges = self._turn_badges()
        if g.phase == "playing" and time.monotonic() < self._tribute_hold_until:
            badges = {}   # 贡牌/抗贡展示期：不出徽章，展示保持原大小
        # 一轮结束后的保留期内仍显示上一轮各家「不出」/出牌展示
        holding = time.monotonic() < self._trick_hold_until
        displays = g.trick_displays or (self._trick_snapshot if holding else {})
        zone_shown: Dict[int, bool] = {}
        for pid, zone in zones.items():
            zx, zy, zw, zh = zone
            pos = self._pos(pid)
            up = pid in badges          # 分区还有行动徽章时，内容上移错开
            shown = False
            # 上家展示从分区左缘开始、下家贴分区右缘（不遮挡头像）；超出分区
            # 宽时堆叠（只缩间距、不压缩牌宽）
            align = "left" if pos == 3 else "right" if pos == 1 else "center"
            if g.phase == "playing" and pid in displays and pid not in badges:
                # 轮到该家出牌时不画其旧展示（下方清除），否则旧牌会被缩小上移
                d = displays[pid]
                if d["action"] == "play":
                    self._draw_mini(display_order(d["combo"].cards, g.level,
                                                  d["combo"]),
                                    zone, up=up, align=align)
                else:
                    self._zone_label(zone, "不  出", size=self._text_fs(), up=up)
                shown = True
            elif pid in g.tribute_displays and g.phase in ("tribute", "playing") \
                    and not (pos == 0 and g.phase == "tribute"):
                # 进贡/还贡牌展示保留到本局首次出牌（由 game 清除）；
                # 我方贡牌任务进行中改为显示按钮（下方），不再重复显示牌面
                self._draw_mini([g.tribute_displays[pid]["card"]], zone, up=up,
                                align=align)
                shown = True
            elif g.phase == "playing" and pid in badges:
                shown = False   # 轮到该家出牌：清除其上一轮出牌/不出展示，以免干扰
            if not shown and g.tribute_refused and pid in g.tribute_refused_by \
                    and pid not in g.finish_order:
                self._zone_label(zone, "抗贡！", size=self._text_fs(),
                                 color=(1, 0.35, 0.30, 1), up=up)
                shown = True
            zone_shown[pid] = shown

        # 贡牌阶段：仅在我方任务时显示「进贡/还贡」一个按钮，置于我方分区中央
        # （即原「还贡」行动徽章的中点位置）；不显示不出/出牌按钮与徽章
        if g.phase == "tribute" and self._mode in ("tribute", "return"):
            text = "进贡" if self._mode == "tribute" else "还贡"
            zx, zy, zw, zh = zones[0]
            mx, my = zx + zw / 2, zy + zh / 2
            bw, bh = int(0.110 * W), self._ref_bh()   # 高度=出牌按钮高度
            self.add_widget(FlatButton(text, (0.80, 0.55, 0.15, 1), self._on_play,
                                       pos=(int(mx - bw / 2), int(my - bh / 2)),
                                       size=(bw, bh)))

        # 行动徽章：圆形「出牌/进贡/还贡」置于行动者分区中央（有内容时缩小放底部）
        # 记录徽章整数几何，供 _build_actions 在其两侧对称排布不出/出牌按钮
        for pid, text in badges.items():
            if g.phase == "tribute" and self._pos(pid) == 0:
                continue    # 贡牌阶段我方仅显示按钮，不显示徽章
            zx, zy, zw, zh = zones[pid]
            if zone_shown.get(pid):
                d = int(min(0.040 * W, zh * 0.40))
                bxi, byi = int(zx + zw / 2 - d / 2), int(zy + 2)
            else:
                d = int(min(0.050 * W, zh * 0.92))
                bxi, byi = int(zx + zw / 2 - d / 2), int(zy + zh / 2 - d / 2)
            self._badge_geo[pid] = (bxi, byi, d)
            self.add_widget(CircleBadge(text, pos=(bxi, byi), size=(d, d)))

    def _save_replay(self):
        """保存整场比赛记录到 replays/（同一场重复保存覆盖同文件）。

        联机客人优先保存主机分发的权威记录（含 AI 出牌理由/决策快照键）。
        """
        if self._is_guest() and self.app.net_end_record is not None:
            try:
                from replay_ui import save_replay
                path = save_replay(self.app.net_end_record,
                                   prev=self.app.net_replay_path)
            except Exception as e:
                self._toast_msg(f"保存失败：{e}")
                return
            self.app.net_replay_path = path
            self._toast_msg("已保存：" +
                            os.path.splitext(os.path.basename(path))[0])
            return
        rec = self._recorder
        if rec is None:
            self._toast_msg("本局未录制复盘数据")
            return
        try:
            from replay_ui import save_replay
            path = save_replay(rec.to_dict(), prev=self._replay_saved_path)
        except Exception as e:
            self._toast_msg(f"保存失败：{e}")
            return
        self._replay_saved_path = path
        self._toast_msg("已保存：" +
                        os.path.splitext(os.path.basename(path))[0])

    def _compute_scores(self):
        """结算打分：重演当前 record（专家对比法），缓存后重画结算页。"""
        rec = self._recorder
        if rec is None:
            return
        try:
            self._scores = scorer.score_record(rec.to_dict())["scores"]
        except Exception:            # 打分失败不阻塞结算页
            self._scores = None
            return
        self.refresh()

    def _build_settlement(self, zones):
        """局终结算：头游~末游 + 「X方胜，下局打X」 + 下一局/主界面按钮。"""
        g = self.game
        W, H = Window.width, Window.height
        px1, py1, pw, ph = self._rect(*self.PLAY_RECT)
        mx = px1 + pw / 2
        my = py1 + ph / 2
        ranks = ["头游", "二游", "三游", "末游"]
        auto = set(g.round_result.get("auto_last", [])) if g.round_result else set()
        fo = g.round_result["finish_order"] if g.round_result else list(g.finish_order)
        sc = self._scores or {}
        fs = self._text_fs()
        for i, pid in enumerate(fo[:4]):
            # 未打完被记末游的玩家直接标「末游」，其余按标准名次；后缀打分（专家对比法）
            rank = "末游" if pid in auto else ranks[i]
            s = sc.get(pid)
            self._zone_label(zones[pid], f"{rank} {s:.0f}" if s is not None else rank,
                             size=fs, color=(1.0, 0.85, 0.35, 1))
        guest_wait = ("等待主机", (0.40, 0.42, 0.40, 1), lambda: None)
        if g.phase == "match_over":
            big = "我方获胜！" if g.match_winner == self.me % 2 else "对方获胜！"
            self._center_text(mx, my + 0.014 * H, f"[b]{big}[/b]", fs,
                              (1.0, 0.90, 0.40, 1))
            btn1 = guest_wait if self._is_guest() else \
                ("再来一局", GREEN, self._on_new_match)
        else:
            wt = g.round_result["winner_team"] if g.round_result else 0
            who = "我方胜" if wt == self.me % 2 else "对方胜"
            sub = (f"下局：我方打{RANK_LABELS[g.levels[self.me % 2]]}"
                   f" · 对方打{RANK_LABELS[g.levels[1 - self.me % 2]]}")
            self._center_text(mx, my + 0.014 * H, f"[b]{who}[/b]　{sub}", fs,
                              (1.0, 0.90, 0.40, 1))
            btn1 = guest_wait if self._is_guest() else \
                ("下一局", GREEN, self._on_next_round)
        bw, bh = int(0.105 * W), int(0.060 * H)
        by = int(my - 0.075 * H)
        btns = [btn1,
                ("保存复盘", BLUE, self._save_replay),
                ("主界面", (0.55, 0.18, 0.16, 1),
                 lambda: self.app.goto("menu"))]
        x0 = int(mx - (len(btns) * bw + (len(btns) - 1) * 6) / 2)
        for i, (t, c, cb) in enumerate(btns):
            self.add_widget(FlatButton(t, c, cb,
                                       pos=(x0 + i * (bw + 6), by), size=(bw, bh)))

    def _center_text(self, mx, y, text, size, color):
        W, H = Window.width, Window.height
        # 宽度放大到 0.60W：基准字号下「我方胜　下局：…」单行可容纳
        self.add_widget(_label(text, size=size, color=color,
                               pos=(int(mx - 0.30 * W), int(y - 0.026 * H)),
                               widget_size=(int(0.60 * W), int(0.052 * H))))

    def _zone_label(self, zone, text, size=18, color=(0.95, 0.90, 0.70, 1), up=False):
        zx, zy, zw, zh = zone
        y = zy + zh * 0.50 if up else zy
        h = zh * 0.46 if up else zh
        self.add_widget(_label(f"[b]{text}[/b]", size=size, color=color,
                               pos=(int(zx), int(y)), widget_size=(int(zw), int(h))))

    def _draw_mini(self, cards, zone, up=False, align="center"):
        """出牌展示：一组牌横排、高度为手牌牌高的 3/4（各家统一尺寸，牌宽
        恒定不压缩）；align=left/right 时贴分区左/右缘（上家/下家，不遮挡
        头像），超出分区宽时缩小间距堆叠（牌面宽度不变）；up=True 时贴分区
        顶部（给徽章让位）。"""
        zx, zy, zw, zh = zone
        n = len(cards)
        if n == 0:
            return
        ch = 0.75 * self._hand_ch()
        cw = ch * 0.72
        step = cw
        if n > 1 and cw + (n - 1) * step > zw:
            step = max(cw * 0.30, (zw - cw) / (n - 1))   # 堆叠：只缩间距不缩牌宽
        total = cw + (n - 1) * step
        if align == "left":
            sx = zx                                      # 上家：从分区左缘开始
        elif align == "right":
            sx = zx + zw - total                         # 下家：贴分区右缘
        else:
            sx = zx + (zw - total) / 2
        sy = zy + zh - ch - 2 if up else zy + (zh - ch) / 2
        for i, c in enumerate(cards):
            self.add_widget(CardWidget(c, self.game.level, on_tap=None, show_face=True,
                                       pos=(sx + i * step, sy), size=(cw, ch)))

    # ---- 观战模式：我已出完，对家手牌亮出于手牌区，AI 提速
    def _spectator(self) -> bool:
        g = self.game
        return (g.phase == "playing" and self.me in g.finish_order
                and bool(g.hands.get(self._pid(2))))

    # ---- 底部手牌区（无底色）：炸弹/同花顺左、普通牌分列居中、组合牌右
    def _build_hand(self):
        g = self.game
        self._hand_widgets = {}
        # 左缘：教学模式避让教学面板（0.226W + 0.2 倍「未报数」行距），
        # 非教学模式回旧位 0.080W；右缘距同花顺框左缘（0.910W）0.2 倍
        # 「未报数」行距；区宽变窄由列间重叠（col_step/stack_step 下限
        # 0.30cw）自适应，牌宽不压缩
        gap = self._teach_gap()
        W, H = Window.width, Window.height
        x = int(0.226 * W) + gap if self._teach_open else int(0.080 * W)
        x2 = int(0.910 * W) - gap
        y = int(0.012 * H)
        w = x2 - x
        if self._spectator():
            # 我已出完：对家手牌按我的手牌方式整理展示（明牌不可点）
            m = HandManager(g, pid=self._pid(2))
            m.auto_arrange()
            self._layout_hand(m, x, y, w)
            return
        self._layout_hand(self.mgr, x, y, w)

    def _layout_hand(self, mgr, x, y, w):
        """手牌布局：左炸弹/同花顺组、中普通牌按点数分列、右非炸弹组。"""
        bombs, combos = mgr.arranged_layout()
        nb, ncc = len(bombs), len(combos)
        # 牌面尺寸锁定：按「6 张一列恰好到出牌区下沿」计算一次
        # （ch + 5×0.30ch = 2.5ch = 区下沿至出牌区下沿），不随实际列长调整
        ch = self._hand_ch()
        cw = ch * 0.72
        gap = 8
        pad = 5
        stack_step = cw + 10
        cols = mgr.column_layout()
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
            self._draw_stack(eids, sx, y + pad, cw, ch, mgr)
            sx += stack_step
        # 中间普通手牌（按点数分列，列内纵向重叠）
        nx0 = start + left_w
        for ci, col in enumerate(cols):
            colx = nx0 + ci * col_step
            # 列内纵向重叠：底牌最后添加（在最上层，整张可见）
            for k in reversed(range(len(col))):
                c = mgr.card_by_eid(col[k])
                self._add_hand_card(c, colx, y + pad + k * step, cw, ch, mgr)
        # 右侧非炸弹牌组（同尺寸竖排，紧贴普通牌右侧，底部与普通牌对齐）
        sx = nx0 + normal_total + 10
        for gi, eids in combos:
            self._draw_stack(eids, sx, y + pad, cw, ch, mgr)
            sx += stack_step

    def _add_hand_card(self, card: Card, cx, cy, cw, ch, mgr=None):
        if card is None:
            return
        mgr = mgr or self.mgr
        wid = CardWidget(card, self.game.level, on_tap=None,
                         selected=(card.eid in mgr.selected),
                         pos=(cx, cy), size=(cw, ch))
        self._hand_widgets[card.eid] = wid
        self.add_widget(wid)

    def _draw_stack(self, eids: List[int], cx, cy_bottom, cw, ch, mgr=None):
        """竖排牌组：底部与普通手牌对齐，固定重叠率露出每张牌角标；
        同点牌组从下到上按黑桃/红桃/梅花/方块排列；
        底牌最后添加（整张可见）；超出区高时向上突破（如 7 炸）。"""
        mgr = mgr or self.mgr
        n = len(eids)
        if n == 0:
            return
        step = 0.30 * ch if n > 1 else 0
        base_y = cy_bottom
        col = list(eids)
        if len({mgr.card_by_eid(e).rank for e in col}) == 1:
            col.sort(key=lambda e: (int(mgr.card_by_eid(e).suit), e))
        for k in reversed(range(n)):
            c = mgr.card_by_eid(col[k])
            self._add_hand_card(c, cx, base_y + k * step, cw, ch, mgr)

    # ---- 右侧竖列（原出牌按钮位置）：整理/恢复合并按钮 / 教学按钮位 / 同花顺框
    # （按钮宽为原出牌按钮 2/3、右缘齐平；按钮高度=出牌按钮高度；
    #   合并按钮在原「整理手牌」位置、教学按钮占原「恢复手牌」位置，两按钮
    #   间距保持原值（半旧高度）；同花顺框位置不变）
    def _build_sf_strip(self):
        W, H = Window.width, Window.height
        hs = int(0.045 * H)                     # 旧控件高度（间距/同花顺框定位基准）
        nh = self._ref_bh()                     # 按钮高度 = 出牌按钮高度
        w = int(0.070 * W)                      # 按钮宽 = 原出牌按钮 0.105W 的 2/3
        gap = hs // 2                           # 间距 = 原值（半个旧按钮高度）
        YEL = (0.99, 0.94, 0.70, 1)             # 浅黄底
        YEL_TXT = (0.30, 0.20, 0.04, 1)         # 浅黄底上的深色文字
        GRN_SF = (0.80, 0.93, 0.80, 1)          # 同花顺框填充（与开新局按钮同色）
        # 原出牌按钮：_rect(0.875, 0.280, 0.980, 0.380)
        x = int(0.980 * W) - w                  # 右缘齐平
        y2 = int(0.380 * H) - 2 * hs - gap      # 原「恢复手牌」位（现教学按钮位）
        self._sf_geo = (x, y2, w, nh)           # 教学按钮几何（_build_teaching 读取）
        y = y2 + gap + nh                       # 合并按钮下沿 = 教学位上沿 + 原间距
        self._btn_sort = FlatButton("整理手牌", YEL, self._on_sort_btn,
                                    text_color=YEL_TXT, pos=(x, y), size=(w, nh))
        self.add_widget(self._btn_sort)
        self._sync_sort_btn()
        # 同花顺框：距教学按钮下沿 2 个旧按钮高度；正方形（高=宽），
        # 两列×每列两花色（列1=黑桃/红桃，列2=梅花/方块），
        # 黑边框 + 浅绿填充（与开新局按钮同色）（图标后添加，绘制在填充之上）；
        # 框内仅显示花色符号（不显示候选数字），字形高=框高的 35%
        y3 = y2 - 2 * hs - w
        self._add_panel((x, y3, w, w), color=GRN_SF,
                        border=(0.05, 0.05, 0.05, 0.9), radius=6)
        slot_w = w / 2.0
        slot_h = w / 2.0
        order = [Suit.SPADE, Suit.HEART, Suit.CLUB, Suit.DIAMOND]
        for i, s in enumerate(order):
            coli, rowi = i // 2, i % 2          # 列从左到右，行从上到下
            has = self.mgr.has_sf(s)
            sym = SUIT_SYM[s]
            col = (0.85, 0.12, 0.12, 1) if s.is_red else (0.10, 0.10, 0.12, 1)
            if has:
                txt, ccol = _sym(sym), col
                cb = (lambda s=s: self._on_sf(s))
            else:
                txt, ccol, cb = _sym(sym), (0.55, 0.55, 0.58, 1), None
            self.add_widget(SuitIcon(txt, ccol, cb,
                                     pos=(int(x + coli * slot_w),
                                          int(y3 + (1 - rowi) * slot_h)),
                                     size=(int(slot_w), int(slot_h))))

    # ---- 整理/恢复合并按钮：随选中状态切换文字与激活态（2026-10-19 用户规格：
    #      未选中=无整理组「整理手牌」灰/有整理组「恢复手牌」；选中已整理组=
    #      「恢复手牌」仅恢复所选组；选中合法牌型=「整理手牌」；其余=「整理手牌」灰）
    def _sort_btn_state(self) -> Tuple[str, bool]:
        mgr = self.mgr
        if mgr.selected:
            if mgr.groups and mgr.selected <= mgr._grouped_eids():
                return "恢复手牌", True
            return "整理手牌", bool(classify(mgr.selected_cards(), mgr.level))
        return ("恢复手牌", True) if mgr.groups else ("整理手牌", False)

    def _sync_sort_btn(self):
        btn = getattr(self, "_btn_sort", None)
        if btn is None:
            return
        text, enabled = self._sort_btn_state()
        if btn.text != text:
            btn.set_text(text)
        btn.set_enabled(enabled)

    def _on_sort_btn(self):
        if self._sort_btn_state()[0] == "恢复手牌":
            self._on_restore()
        else:
            self._on_arrange()

    # ---- 出牌 / 不出 按钮：对称排布在我方出牌区行动徽章两侧（水平中线对齐，
    # 不出在左、出牌在右），高度与徽章相同、距徽章两侧一个徽章宽度；
    # 显示时机与行动徽章一致（轮到我方出牌时才出现）
    def _build_actions(self):
        g = self.game
        self._btn_play = None
        self._btn_pass = None
        geo = self._badge_geo.get(self.me)
        if geo is None:             # 非我方回合：与徽章一同隐藏
            return
        bx, by, d = geo
        W, H = Window.width, Window.height
        bw = int(0.105 * W)
        bh = d                      # 按钮高度 = 出牌图标（徽章）高度
        gap = d                     # 按钮与徽章的间距 = 徽章宽度（两侧一致）
        y = by + d // 2 - bh // 2      # 与徽章水平中线对齐
        self._btn_pass = FlatButton(
            "不出", ORANGE, self._on_pass,
            enabled=(g.last_play is not None and g.last_play[0] != self.me),
            pos=(bx - gap - bw, y), size=(bw, bh))
        self.add_widget(self._btn_pass)
        self._btn_play = FlatButton("出牌", BLUE, self._on_play,
                                    enabled=self._confirm_enabled(),
                                    pos=(bx + d + gap, y), size=(bw, bh))
        self.add_widget(self._btn_play)

    def _sync_action_buttons(self):
        if self._btn_play:
            self._btn_play.set_enabled(self._confirm_enabled())
        if self._btn_pass:
            g = self.game
            self._btn_pass.set_enabled(g.last_play is not None
                                       and g.last_play[0] != self.me)

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
        self._sync_sort_btn()

    def on_touch_down(self, touch):
        wid = self._hand_card_at(touch.pos)
        if wid is not None and touch.button in ("left", None):
            if getattr(touch, "is_double_tap", False):
                # 快速出牌：双击某牌 → 选中其所属牌组（已整理组或同点数整列）
                self._select_group_of(wid.card.eid)
            else:
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

    def _select_group_of(self, eid: int):
        """快速选组：双击已整理组中的牌选中整组；否则选中同点数整列。"""
        bombs, combos = self.mgr.arranged_layout()
        for _, eids in bombs + combos:
            if eid in eids:
                self.mgr.select([e for e in eids if self.mgr.card_by_eid(e)])
                break
        else:
            for col in self.mgr.column_layout():
                if eid in col:
                    self.mgr.select(list(col))
                    break
        self._update_sel_visuals()

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
        s = self.app.net_session
        if s is not None:
            # 联机：由主机广播新 start（新 seed），全员（含本端）重建引擎；
            # 客人端不提供「再来一局」按钮
            if s.is_host and self.game.phase == "match_over":
                levels = [self.game.levels[0], self.game.levels[1]]
                s.next_match(levels)
            return
        # 单机：以新 seed 重建引擎。引擎构造即完成首局发牌（game.__init__ 自动
        # start_match）；若对已有引擎再次 start_match，会用 rng 续洗出第二副
        # 牌，复盘记录里的 seed 便无法重演出实际手牌（seed→发牌确定性被破坏）
        seed = random.randrange(2 ** 31)
        g = GuandanGame(rng=random.Random(seed))
        self.app.seed = seed
        self.app.game = g
        self.game = g
        self.mgr = HandManager(g, pid=0)
        self._attach_recorder()      # 新比赛起点：重挂 Recorder（打分/复盘数据源）
        self._attach_tracker()       # 新比赛起点：重挂教学数据跟踪
        self.mgr.sync()
        self._last_round = -1
        self._alerted = set()
        self._prev_phase = g.phase
        self._prev_trick = {}
        self._trick_snapshot = {}
        self._trick_hold_until = 0.0
        self._tribute_hold_until = 0.0
        self.refresh()

    def _attach_recorder(self):
        """在比赛起点挂 Recorder 与决策快照记录器；失败（非起点）静默放弃。"""
        if self._recorder is not None:
            self._recorder.detach(self.game)   # 防止「再来一局」双录
        self._recorder = None
        if self._decisions is not None:
            self._decisions.detach(self.game)
        self._decisions = None
        if self._calib is not None:
            self._calib.detach(self.game)
        self._calib = None
        self._replay_saved_path = None
        self._scores = None
        rec = Recorder(seed=self.app.seed)
        try:
            rec.attach(self.game)
        except ValueError:
            return
        self._recorder = rec
        logger = decision.DecisionLogger(seed=self.app.seed)
        if logger.attach(self.game):
            self._decisions = logger
            # 夯级校准检测（value 档步骤③）：随快照记录器在比赛起点挂载，
            # round_end 对照校准常量；仅当本局出现 sid=value 快照才落
            # review 标记（无夯级快照静默跳过，内部守卫异常不外抛）
            self._calib = calib.CalibrationChecker(seed=self.app.seed,
                                                   store=logger.store)
            self._calib.attach(self.game)

    def _attach_tracker(self):
        """在比赛起点挂教学数据跟踪（observer.PlayTracker）；非起点静默放弃。"""
        if self._tracker is not None:
            self._tracker.detach(self.game)
        self._tracker = None
        tr = observer.PlayTracker()
        if tr.attach(self.game):
            self._tracker = tr

    def _on_next_round(self):
        try:
            self.game.start_next_round()
        except GameError as e:
            self._toast_msg(str(e))
            return
        self.mgr.sync()
        self._last_round = -1
        self._alerted = set()
        self._scores = None          # 新一局重算累计分
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
        guest = self._is_guest()
        if self._mode == "tribute":
            if len(cards) != 1:
                self._toast_msg("请选中恰好一张要进贡的最大牌")
                return
            if guest:
                # 联机客人：动作上送主机，待广播回放后统一生效
                self.app.net_session.send_act("tribute", eid=cards[0].eid,
                                              round=g.round_number)
            else:
                try:
                    g.submit_tribute(self.me, cards[0].eid)
                    self.app.sounds.play("tribute")
                except GameError as e:
                    self._toast_msg(str(e))
            self.refresh()
            return
        if self._mode == "return":
            if len(cards) != 1:
                self._toast_msg("请选中恰好一张要还贡的牌")
                return
            if guest:
                self.app.net_session.send_act("return", eid=cards[0].eid,
                                              round=g.round_number)
            else:
                try:
                    g.submit_return(self.me, cards[0].eid)
                    self.app.sounds.play("return_tribute")
                except GameError as e:
                    self._toast_msg(str(e))
            self.refresh()
            return
        if not cards:
            self._toast_msg("请先选择要出的牌")
            return
        if guest:
            self.app.net_session.send_act("play",
                                          eids=[c.eid for c in cards],
                                          round=g.round_number)
            self.refresh()
            return
        try:
            combo = g.play(self.me, cards)
            self.mgr.commit_play(combo)
            self.app.sounds.play(event_for_combo(combo))
        except (IllegalPlayError, NotPlayerTurnError, GameError) as e:
            self._toast_msg(f"非法出牌：{e}")
        self.refresh()

    def _on_pass(self):
        g = self.game
        if self._is_guest():
            self.app.net_session.send_act("pass", round=g.round_number)
            self.refresh()
            return
        try:
            pre = dict(g.trick_displays)
            pre[self.me] = {"action": "pass", "combo": None}
            g.pass_play(self.me)
            self.app.sounds.play("pass")
            if not g.trick_displays:      # 一轮结束：_advance 已清空展示，
                self._trick_snapshot = pre  # 预先捕获含本次「不出」的完整展示
                self._trick_hold_until = time.monotonic() + 1.5
        except GameError as e:
            self._toast_msg(str(e))
        self.refresh()

    # ---- 模式/提示/驱动
    def _confirm_enabled(self) -> bool:
        """「出牌」按钮可点：仅我方回合且当前选牌能构成合法牌型。"""
        g = self.game
        if g.phase != "playing" or g.turn != self.me:
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
            if self.me in pending_givers:
                self._mode = "tribute"
            elif self.me in awaiting_receivers:
                self._mode = "return"

    def _update_alerts(self):
        g = self.game
        self._update_mode()
        if g.round_number != self._last_round:
            self._last_round = g.round_number
            self._alerted = set()
            self.app.sounds.play("deal")     # 新一局发牌
        if self.app.settings["low_card_alert"] and g.phase == "playing":
            for p in range(4):
                n = g.hand_count(p)
                if n <= self.app.settings["report_threshold"] and p not in self._alerted \
                        and p not in g.finish_order:
                    self._alerted.add(p)
                    who = "你" if p == self.me else self._display_name(p)
                    self._toast_msg(f"提示：{who} 只剩 {n} 张牌！")
                    self.app.sounds.play("alert")
                    break

    def _pump(self, dt):
        # 联机：先泵网络会话（动作套用/座位变更/掉线提示），再驱动本地节奏
        s = self.app.net_session
        if s is not None:
            s.pump()
            self._net_drain()
        g = self.game
        if g.phase in ("match_over", "round_over"):
            return          # 等待结算画面上的「下一局/再来一局」按钮
        if g.phase == "tribute":
            if s is not None and not s.is_host:
                return      # 客人：贡牌/还贡动作以主机广播为准，仅等待
            # 先推进 AI 座位的进贡/还贡任务（联机主机仅代打 AI 座位，
            # 远端人类座位等待其上行动作），轮到「我」的任务才等待人类
            if self.app.ai_tribute_step(
                    None if s is None else s.ai_pids()):
                self.mgr.sync()
                self.refresh()
            return
        if time.monotonic() < self._tribute_hold_until:
            return          # 贡牌展示期内不进牌局
        if self._tribute_hold_until:
            self._tribute_hold_until = 0.0
            g.begin_round()   # 牌局正式开始：清除贡牌/抗贡展示
            self.refresh()
        if self._trick_hold_until:
            if time.monotonic() < self._trick_hold_until:
                return          # 上一轮「不出」/出牌展示保留期内暂停行牌
            self._trick_hold_until = 0.0
            self._trick_snapshot = {}
            self.refresh()
        if s is not None and not s.is_host:
            return              # 客人：不调度 AI，动作全部由主机广播驱动
        if g.turn != self.me:
            if s is not None and g.turn not in s.ai_pids():
                return          # 联机：远端人类座位等待其动作广播
            if not self._ai_scheduled:
                self._ai_scheduled = True
                # 观战（我已出完）时 AI 提速到最快
                d = 0.05 if self._spectator() else self.app.settings["ai_delay"]
                Clock.schedule_once(self._ai_step, d)

    def _ai_step(self, dt):
        self._ai_scheduled = False
        g = self.game
        if g.phase == "playing" and g.turn != self.me:
            pid = g.turn
            sid = self._seat_sid(pid)
            t0 = time.monotonic()
            kind, cards, detail = strategy_choose_detail(
                g, pid, self.app.profiles[pid], sid)
            cost_ms = (time.monotonic() - t0) * 1000.0
            # 出牌理由（复盘「理由区」数据源）：决策时刻生成，挂到本条动作
            reason = explain_choice(g, pid, kind, cards) \
                if self._recorder is not None else ""
            # 决策快照（阶段二/三）：动作套用前采集候选分项明细，decision_id
            # 追加为本条动作的 "did" 键（向后兼容，同 reason 先例）
            did = None
            if self._recorder is not None and self._decisions is not None:
                _cost = (detail or {}).get("cost") or {}   # 阶段六档位预算
                did = self._decisions.capture(
                    g, pid=pid, sid=sid, profile=self.app.profiles[pid],
                    kind=kind, cards=cards, reason=reason,
                    action_index=len(self._recorder.actions), cost_ms=cost_ms,
                    candidates=detail["candidates"] if detail else None,
                    n_candidates=detail["n_candidates"] if detail else 0,
                    search_depth=_cost.get("search_depth", 0),
                    n_rollouts=_cost.get("n_rollouts", 0),
                    context=(detail or {}).get("context"))
            if kind == "play":
                combo = g.play(pid, cards)
                self.app.sounds.play(event_for_combo(combo))
            else:
                pre = dict(g.trick_displays)
                pre[pid] = {"action": "pass", "combo": None}
                g.pass_play(pid)
                self.app.sounds.play("pass")
                if not g.trick_displays:   # 一轮结束：最后过牌者（上家）的
                    self._trick_snapshot = pre   # 「不出」也要进入保留快照
                    self._trick_hold_until = time.monotonic() + \
                        (0.3 if self._spectator() else 1.5)
            if self._recorder is not None and self._recorder.actions:
                last = self._recorder.actions[-1]
                if last.get("pid") == pid:
                    if reason:
                        last["reason"] = reason
                    if did:
                        last["did"] = did
            self.mgr.sync()
            self.refresh()

    # ---- 联机（阶段三好友联机）：建局 / 远端动作套用 / 会话事件分发
    def net_setup(self, seed, levels, actions=None):
        """联机建局（start）或重建镜像（resync 归位）。

        各端以同 seed 构造完整 GuandanGame（锁步全量复制），本地「我」=
        自己的座位号；Recorder/教学跟踪在比赛起点挂载，resync 的动作重演
        亦入录（与直播套用同一路径，本地复盘记录完整）。主机在动作重演后
        挂载引擎事件镜像（权威端唯一动作出口）；动作重演先于镜像挂载，
        避免重演动作被二次广播。
        """
        s = self.app.net_session
        if s is None:
            return
        me = s.my_seat or 0
        old_mgr = self.mgr           # 旧整理状态：重建时尽量保留分组
        # 构造即完成首局发牌（game.__init__ 自动 start_match）：seed→发牌
        # 确定性成立，赛末分发的复盘记录可在任意一端 rebuild 重演
        g = GuandanGame(rng=random.Random(seed))
        g.levels = {0: int(levels[0]), 1: int(levels[1])}
        self.app.seed = seed
        self.app.game = g
        self.game = g
        self.me = me
        self.mgr = HandManager(g, pid=me)
        self._attach_recorder()
        self._attach_tracker()
        for act in (actions or []):
            try:
                apply_net_act(g, act)
            except GameError:
                break              # 动作流损坏：保留已重演前缀，等待再 resync
        if s.is_host:
            s.set_game(g)
        self.mgr.clear_groups()
        self.mgr.clear_selection()
        self.mgr.sync()
        # resync/重连重建：若整理分组所用牌均仍在当前手牌中，则保留分组
        #（eid 全局唯一且发牌确定性，同座位手牌 eid 稳定），避免重连时
        # 已整理的炸弹/组牌被打散回普通列
        if (isinstance(old_mgr, HandManager) and old_mgr.pid == me
                and old_mgr.groups):
            real = {c.eid for c in self.mgr.real_hand}
            if all(e in real for grp in old_mgr.groups for e in grp):
                self.mgr.groups = [list(grp) for grp in old_mgr.groups]
                self.mgr.group_meta = [dict(m) for m in old_mgr.group_meta]
        self._last_round = -1
        self._alerted = set()
        self._scores = None
        self._replay_saved_path = None
        self._prev_phase = g.phase
        self._prev_trick = {}
        self._trick_snapshot = {}
        self._trick_hold_until = 0.0
        self._tribute_hold_until = 0.0
        self._ai_scheduled = False
        self._update_mode()
        self.refresh()

    def _net_apply_remote(self, act):
        """套用一条远端广播动作（客人：全部动作）。

        统一以广播为准本地套用并刷新；套用失败（状态漂移/漏动作）时客人
        自动请求 resync 全量重演自愈。
        """
        g = self.game
        s = self.app.net_session
        guest = s is not None and not s.is_host
        if guest and not s.ingest_act(act):
            return                     # 重复帧（resync 已覆盖）或真断档已转 resync
        pre = None
        try:
            if act.get("kind") == "pass":
                pre = dict(g.trick_displays)
                pre[act["pid"]] = {"action": "pass", "combo": None}
            apply_net_act(g, act)
        except GameError:
            if guest:
                s.request_resync()
            return
        if guest:
            s.mark_applied()
        self._net_sounds(act.get("kind"), act.get("pid"), pre)
        self.mgr.sync()
        if self._on_table():
            self.refresh()

    def _net_after_remote(self, ev):
        """主机侧：客人动作已在房间层套用完毕，此处补 UI 反应（音效/刷新）。"""
        g = self.game
        self._net_sounds(ev.get("kind"), ev.get("pid"), ev.get("pre"))
        self.mgr.sync()
        if self._on_table():
            self.refresh()

    def _net_sounds(self, kind, pid, pre):
        """远端动作的音效与一轮结束的「不出」保留快照（房间页不发声）。"""
        if not self._on_table() or kind is None:
            return
        g = self.game
        if kind == "play":
            disp = g.trick_displays.get(pid)
            if disp is not None:
                self.app.sounds.play(event_for_combo(disp["combo"]))
        elif kind == "pass":
            self.app.sounds.play("pass")
            if not g.trick_displays and pre is not None:
                self._trick_snapshot = pre     # 一轮结束：含本次「不出」
                self._trick_hold_until = time.monotonic() + \
                    (0.3 if self._spectator() else 1.5)
        elif kind == "tribute":
            self.app.sounds.play("tribute")
        elif kind == "return":
            self.app.sounds.play("return_tribute")

    def _on_table(self) -> bool:
        return self.manager is not None and self.manager.current == "table"

    def net_accept_end(self, record):
        """收到主机分发的赛末复盘记录（阶段四）：存入 replays/ 供复盘页查看。

        客人本地 Recorder 缺少 AI 座位的出牌理由/决策快照键（仅主机在
        _ai_step 里事后补挂），故以主机分发的权威记录为保存数据源。
        """
        self.app.net_end_record = record
        try:
            from replay_ui import save_replay
            self.app.net_replay_path = save_replay(record)
        except Exception:
            pass                    # 落盘失败不阻塞结算：可稍后在结算页重试

    def _net_drain(self):
        """泵取房间会话事件（仅牌桌页）：动作套用、开局/重演、掉线提示。"""
        from queue import Empty
        s = self.app.net_session
        while True:
            try:
                kind, _peer, ev = s.events.get_nowait()
            except Empty:
                break
            if kind == "act":
                self.app.net_apply_act(ev)
            elif kind in ("started", "resync"):
                self.app.net_on_started(ev)
            elif kind == "acted":
                self._net_after_remote(ev)
            elif kind == "end":
                self.net_accept_end(ev)
                self._toast_msg("本场复盘已保存，可在复盘页查看")
            elif kind == "act_rejected":
                self._toast_msg(f"动作被拒绝：{ev}")
            elif kind == "ai_takeover":
                self._toast_msg(f"{ev.get('name')} 掉线超时，已由 AI 接管")
            elif kind == "left":
                self._toast_msg(f"{ev.get('name')} {ev.get('reason', '掉线')}，等待重连…")
            elif kind == "disconnected":
                self._toast_msg("与主机断开连接")
                self.app.close_net()
                Clock.schedule_once(lambda dt: self.app.goto("menu"), 1.5)
            elif kind == "error":
                self._toast_msg(f"网络错误：{ev}")

    # ---- 载入牌局（复盘「载入/载入局势」入口）：以给定局面接管正常对局
    def load_game(self, game, seed=None):
        """用外部重建的局面替换当前牌桌对局（seed=局面来源存档的 seed）。

        - 起点局面（清单载入）：Recorder 可正常挂载，复盘/打分可用；
        - 中途局面（载入局势）：非比赛起点，挂载静默失败 → 续局不录制
          （2026-10-14 已确认的简化口径），开新局后恢复录制。
        """
        if seed is not None:
            self.app.seed = seed
        if self._recorder is not None:
            self._recorder.detach(self.game)
        self._recorder = None
        if self._decisions is not None:
            self._decisions.detach(self.game)   # 旧局面上的快照监听一并移除
        self._decisions = None
        self._replay_saved_path = None
        self._scores = None
        self.app.game = game
        self.game = game
        self.app.solo_started = True   # 复盘载入续局：主菜单变「返回游戏」
        self.mgr = HandManager(game, pid=0)
        self._attach_recorder()      # 起点成功 / 中途静默失败
        self._attach_tracker()
        self.mgr.sync()
        self._last_round = -1
        self._alerted = set()
        self._prev_phase = game.phase
        self._prev_trick = {}
        self._trick_snapshot = {}
        self._trick_hold_until = 0.0
        self._tribute_hold_until = 0.0
        self._update_mode()
        self.refresh()

    def _toast_msg(self, text):
        W, H = Window.width, Window.height
        if self._toast:
            self.remove_widget(self._toast)
        t = _label(text, size=self._text_fs(), color=(1, 1, 0.5, 1), bold=True)
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


# ---------------------------------------------------------------- 设置页（仅可调整项）
class SettingsScreen(Screen):
    """设置页：报牌张数 / 6张提示 / 音效 / 背景乐 / AI 牌力 / 教学面板。

    AI 牌力为四档单选（初级/中级/高级/夯级），下方附各档算法特点简述；经典档
    classic_low/classic_high 与夯档 pimc 保留在 registry 供 bench/测试调用，
    不再进入设置界面。座位策略覆盖已废除，全部 AI 座位跟随全局档位。
    全部控件尺寸按窗口高度比例缩放，保证安卓高分屏与桌面窗口上字号同样清晰。
    """

    BGM_OPTIONS = [("关", "off"), ("曲目一", "track1"), ("曲目二", "track2")]

    def __init__(self, app, **kw):
        super().__init__(**kw)
        self.app = app

    def on_enter(self):
        self.clear_widgets()
        W, H = Window.width, Window.height
        fs = max(15, int(0.030 * H))          # 行文字字号（随窗口高度缩放）
        rh = max(34, int(0.062 * H))          # 行高
        cw = max(28, int(rh * 0.85))          # 复选框边长
        self.add_widget(Panel(pos=(0, 0), size=(W, H), color=WOOD_DARK,
                              border=WOOD_DARK, radius=0))
        if os.path.exists(BG_PATH):
            self.add_widget(KImage(source=BG_PATH, pos=(0, 0), size=(W, H),
                                   size_hint=(None, None), allow_stretch=True,
                                   keep_ratio=False))
        scroll = ScrollView(size_hint=(None, None),
                            pos=(int(W * 0.25), int(H * 0.04)),
                            size=(int(W * 0.5), int(H * 0.92)))
        box = BoxLayout(orientation="vertical", size_hint=(None, None),
                        spacing=int(0.018 * H), padding=int(0.012 * W))
        box.width = scroll.width
        box.bind(minimum_height=box.setter("height"))
        scroll.add_widget(box)
        self.add_widget(scroll)
        s = self.app.settings

        box.add_widget(_label("设置", size=int(fs * 1.4), bold=True, color=GOLD,
                              size_hint=(1, None), height=int(rh * 1.3)))

        def row():
            r = BoxLayout(orientation="horizontal", size_hint=(1, None),
                          height=rh, spacing=int(0.010 * W))
            box.add_widget(r)
            return r

        r1 = row()
        r1.add_widget(_label("报牌张数：", size=fs, halign="left",
                             size_hint=(0.30, 1)))
        self.th_input = TextInput(text=str(s["report_threshold"]), font_size=fs,
                                  font_name=_CJK_FONT, input_filter="digits",
                                  size_hint=(None, 1), width=int(0.08 * W),
                                  foreground_color=(1, 1, 1, 1),
                                  background_color=(0.2, 0.2, 0.2, 1))
        r1.add_widget(self.th_input)
        r1.add_widget(_label("6张提示", size=fs, halign="left",
                             size_hint=(0.30, 1)))
        self.alert_cb = CheckBox(active=bool(s["low_card_alert"]),
                                 size_hint=(None, 1), width=cw)
        r1.add_widget(self.alert_cb)

        r2 = row()
        r2.add_widget(_label("音效：", size=fs, halign="left", size_hint=(0.52, 1)))
        self.sound_cb = CheckBox(active=bool(s.get("sound", True)),
                                 size_hint=(None, 1), width=cw)
        r2.add_widget(self.sound_cb)
        r2.add_widget(_label("开启", size=fs, halign="left", size_hint=(0.3, 1)))

        r3 = row()
        r3.add_widget(_label("背景乐：", size=fs, halign="left", size_hint=(0.52, 1)))
        self.bgm_cbs = []
        for txt, val in self.BGM_OPTIONS:
            r3.add_widget(_label(txt, size=fs, halign="center",
                                 size_hint=(None, 1), width=int(0.080 * W)))
            cb = CheckBox(group="bgm", active=(s.get("bgm", "off") == val),
                          size_hint=(None, 1), width=cw)
            self.bgm_cbs.append((cb, val))
            r3.add_widget(cb)

        r4 = row()
        r4.add_widget(_label("AI 牌力：", size=fs, halign="left",
                             size_hint=(0.30, 1)))
        # 四档单选（单选按钮组）：经典 低/高/夯 已退出设置界面，底层策略
        # 保留在 registry 供 bench/测试调用；保存后下一手 AI 即生效。
        STRENGTH_OPTIONS = [("初级", "novice"), ("中级", "intermediate"),
                            ("高级", "advanced"), ("夯级", "value")]
        self.ai_choice = s["ai_strength"]      # load_settings 已迁移为合法档
        for txt, val in STRENGTH_OPTIONS:
            cb = CheckBox(group="ai_strength", active=(self.ai_choice == val),
                          size_hint=(None, 1), width=cw)
            cb.bind(active=lambda inst, act, v=val: self._set_ai(v)
                    if act else None)
            r4.add_widget(cb)
            r4.add_widget(_label(txt, size=fs, halign="left",
                                 size_hint=(0.14, 1)))

        r4b = row()
        r4b.add_widget(_label(
            "初级：基础规则引擎，轻量快速｜中级：记牌器+报牌压制｜"
            "高级：全特性+领出协同评分+深残局搜索｜夯级：胜利值公式"
            "（牌力×压制×对家增益×终局乘数）",
            size=int(fs * 0.62), halign="left", valign="middle",
            size_hint=(1, 1), color=(0.78, 0.78, 0.78, 1)))

        r5 = row()
        r5.add_widget(_label("教学面板：", size=fs, halign="left",
                             size_hint=(0.52, 1)))
        self.teach_cb = CheckBox(active=bool(s.get("teaching", True)),
                                 size_hint=(None, 1), width=cw)
        r5.add_widget(self.teach_cb)
        r5.add_widget(_label("开启（牌桌右列「教学」按钮）", size=fs,
                             halign="left", size_hint=(0.42, 1)))

        # 保存/返回同排：高度 1.5 倍行高、宽高比 3:2，整排居中
        bh = max(40, int(0.062 * H))
        bh2 = int(bh * 1.5)
        bw2 = int(bh2 * 1.5)
        btn_row = BoxLayout(orientation="horizontal", size_hint=(1, None),
                            height=bh2, spacing=int(0.02 * W),
                            padding=(max(0, (box.width - 2 * bw2
                                             - int(0.02 * W)) // 2), 0, 0, 0))
        box.add_widget(btn_row)
        btn_row.add_widget(FlatButton("保存设置", GREEN, self._on_save,
                                      size_hint=(None, 1), width=bw2))
        btn_row.add_widget(FlatButton("返回主界面", BLUE,
                                      lambda: self.app.goto("menu"),
                                      size_hint=(None, 1), width=bw2))

    def _set_ai(self, val):
        """单选按钮回调：记录当前 AI 牌力档（novice/intermediate/advanced/value）。"""
        self.ai_choice = val

    def _on_save(self):
        s = self.app.settings
        try:
            v = int(self.th_input.text)
            s["report_threshold"] = max(1, min(27, v))
        except Exception:
            pass
        s["low_card_alert"] = bool(self.alert_cb.active)
        s["sound"] = bool(self.sound_cb.active)
        for cb, val in self.bgm_cbs:
            if cb.active:
                s["bgm"] = val
        s["ai_strength"] = self.ai_choice     # 四档单选（novice/intermediate/advanced/value）
        s["teaching"] = bool(self.teach_cb.active)
        s.pop("seats", None)                  # 座位策略覆盖已废除，清除旧配置残留
        save_settings(s)
        self.app.sounds.update_bgm()     # 背景乐选择/开关即时生效
        self.app.table.refresh()
        self.app.goto("menu")


# ---------------------------------------------------------------- 主菜单
class MenuScreen(Screen):
    """主菜单：单机游戏 / 好友组局 / 设置 / 复盘 / 退出游戏。

    单机牌局进行中（开过局且未打完整场）时「单机游戏」变「返回游戏」、「好
    友组局」置灰；无进行中牌局时恢复可选。有联机会话时「好友组局」变「返回
    房间」。设置项已从牌桌迁移至此（牌桌右上角不再有设置按钮）。on_enter
    整页重建（与设置页同一模式），控件尺寸按窗口比例缩放。
    """

    RED = (0.55, 0.18, 0.16, 1)

    def __init__(self, app, **kw):
        super().__init__(**kw)
        self.app = app
        self._toast_text = ""
        self._toast_event = None

    def on_enter(self):
        self._rebuild()

    def _rebuild(self):
        self.clear_widgets()
        W, H = Window.width, Window.height
        self.add_widget(Panel(pos=(0, 0), size=(W, H), color=WOOD_DARK,
                              border=WOOD_DARK, radius=0))
        if os.path.exists(BG_PATH):
            self.add_widget(KImage(source=BG_PATH, pos=(0, 0), size=(W, H),
                                   size_hint=(None, None), allow_stretch=True,
                                   keep_ratio=False))
        self.add_widget(_label("顺溜掼蛋", size=int(0.085 * H), bold=True,
                               color=GOLD,
                               pos=(int(W * 0.20), int(H * 0.76)),
                               widget_size=(int(W * 0.60), int(H * 0.14))))
        solo = bool(self.app.solo_started
                    and self.app.game.phase != "match_over")
        net = self.app.net_session is not None
        bw = int(0.26 * W)
        bh = max(44, int(0.076 * H))
        gap = int(0.018 * H)
        x = int(W / 2 - bw / 2)
        y = int(H * 0.60)
        items = [
            # (文案, 颜色, 回调, 是否禁用)
            ("返回游戏" if solo else "单机游戏", GREEN,
             self.app.enter_solo, net),
            ("返回房间" if net else "好友组局", BLUE,
             self.app.enter_net, solo),
            ("设置", BLUE, lambda: self.app.goto("settings"), False),
            ("复盘", BLUE, lambda: self.app.goto("replay"), False),
            ("退出游戏", self.RED, self.app.stop, False),
        ]
        for text, color, cb, disabled in items:
            b = FlatButton(text, color, cb, pos=(x, y), size=(bw, bh))
            if disabled:
                b.disabled = True
                b.opacity = 0.5
            self.add_widget(b)
            y -= bh + gap
        if self._toast_text:
            self.add_widget(_label(self._toast_text,
                                   size=max(14, int(0.030 * H)),
                                   color=(1.0, 0.85, 0.35, 1),
                                   pos=(int(W * 0.20), int(H * 0.06)),
                                   widget_size=(int(W * 0.60), int(H * 0.05))))

    def _show_toast(self, text):
        """按钮下方提示条，2.2s 后自动消失（整页重建，与设置页同模式）。"""
        self._toast_text = text
        self._rebuild()
        if self._toast_event is not None:
            self._toast_event.cancel()
        self._toast_event = Clock.schedule_once(
            lambda dt: self._hide_toast(), 2.2)

    def _hide_toast(self):
        self._toast_event = None
        if self._toast_text and self.manager is not None \
                and self.manager.current == "menu":
            self._toast_text = ""
            self._rebuild()


# ---------------------------------------------------------------- App
class GuandanApp(App):
    title = "顺溜掼蛋"
    icon = LOGO_PATH

    def __init__(self, seed=None, **kw):
        super().__init__(**kw)
        self.settings = load_settings()
        if seed is None:                 # 显式化数值 seed：Recorder 重演打分依赖
            seed = random.randrange(2 ** 31)
        self.seed = seed
        self.game = GuandanGame(rng=random.Random(seed))
        self.mgr = HandManager(self.game, pid=0)
        self.sounds = SoundManager(lambda: self.settings)
        self.profiles = [None, AIProfile("aggressive"), AIProfile("balanced"),
                         AIProfile("conservative")]
        self.sm = ScreenManager(transition=NoTransition())
        self.solo_started = False   # 单机牌局是否已开局（主菜单「返回游戏」依据）
        self.net_session = None     # 联机会话（RoomHost/RoomClient，room.py）
        self.net_end_record = None  # 联机赛末主机分发的复盘记录（replay JSON）
        self.net_replay_path = None # 分发记录已落盘 replays/ 的路径（覆盖保存）
        self.menu_screen = MenuScreen(self, name="menu")
        self.table = TableScreen(self, name="table")
        self.settings_screen = SettingsScreen(self, name="settings")
        from replay_ui import ReplayScreen   # 延迟 import：replay_ui 反向延迟用 ui 组件
        self.replay_screen = ReplayScreen(self, name="replay")
        from room_ui import LobbyScreen, RoomScreen  # 延迟 import：防循环
        self.lobby_screen = LobbyScreen(self, name="net_lobby")
        self.room_screen = RoomScreen(self, name="net_room")
        self.sm.add_widget(self.menu_screen)   # 首个添加 = 启动初始屏
        self.sm.add_widget(self.table)
        self.sm.add_widget(self.settings_screen)
        self.sm.add_widget(self.replay_screen)
        self.sm.add_widget(self.lobby_screen)
        self.sm.add_widget(self.room_screen)

    def enter_solo(self):
        """主菜单「单机游戏/返回游戏」：无进行中牌局时开新局，否则续玩。"""
        if not self.solo_started or self.game.phase == "match_over":
            self.table._on_new_match()
        self.solo_started = True
        self.goto("table")

    def enter_net(self):
        """主菜单「好友组局/返回房间」：无会话进大厅，有会话回房间。"""
        if self.net_session is not None:
            self.goto("net_room")
        else:
            self.goto("net_lobby")

    def close_net(self):
        """关闭联机会话（解散/退出房间）；幂等。"""
        if self.net_session is not None:
            self.net_session.close()
            self.net_session = None
        self.net_end_record = None
        self.net_replay_path = None

    def net_on_started(self, ev):
        """收到 start/resync（联机开局或重开/重连归位）：建局并进入牌桌。"""
        if self.net_session is None:
            return
        self.net_end_record = None      # 新一场开始：上场分发记录作废
        self.net_replay_path = None
        self.table.net_setup(ev.get("seed"),
                             ev.get("levels") or [2, 2],
                             actions=ev.get("actions"))
        self.goto("table")

    def net_apply_act(self, act):
        """联机动作统一套用入口（房间页/牌桌页的会话泵共用）。"""
        self.table._net_apply_remote(act)

    def build(self):
        Window.min_size = (900, 560)
        Window.show_touches = False      # 关闭触摸白圈指示器
        Window.bind(on_resize=self._on_resize)
        return self.sm

    def on_start(self):
        self.sounds.ensure_loaded()
        self.sounds.update_bgm()
        # 安卓部分机型启动后黑屏、需触摸命中控件才显示。根因：Kivy 仅在
        # canvas 变脏时才绘制+交换一帧（kivy/base.py EventLoop.idle），
        # 而启动初期（含 presplash 移除触发 surface 重建阶段）绘制的帧都在
        # Android Surface 就绪之前、全部被丢弃；此后 canvas 不再变脏、不再
        # 产生新帧，直到触摸命中控件（空点不脏 canvas，故仍黑屏）。
        # 修复：启动后 10 秒内每 0.25s 强制标脏产生新帧，保证至少一帧落在
        # Surface 就绪之后；前 3 次同时重建牌桌纠正迟到的窗口尺寸布局。
        for t in (0.2, 0.6, 1.2):
            Clock.schedule_once(lambda dt: self._force_redraw(), t)
        Clock.schedule_interval(self._boot_dirty, 0.25)
        Clock.schedule_once(self._stop_boot_dirty, 10)

    def _boot_dirty(self, *a):
        Window.canvas.ask_update()

    def _stop_boot_dirty(self, *a):
        Clock.unschedule(self._boot_dirty)

    def _force_redraw(self):
        Window.canvas.ask_update()
        if self.sm.current == "table":
            self.table.refresh()

    def _on_resize(self, *a):
        if self.sm.current == "table":
            Clock.schedule_once(lambda dt: self.table.refresh(), 0)

    def goto(self, name):
        self.sm.current = name
        # Kivy 仅在 window canvas 脏时绘制+翻帧（base.py EventLoop.idle）；
        # 结算后 _pump 停摆，切屏的组件变化不保证产生新帧 → 黑屏。
        # 强制标脏数帧兜底（与启动/后台返回黑屏同一修复模式）。
        for t in (0.05, 0.3):
            Clock.schedule_once(lambda dt: Window.canvas.ask_update(), t)

    def ai_tribute_step(self, ai_pids=None) -> bool:
        """推进一个 AI 进贡/还贡动作；仅剩人类任务（或全部完成）时返回 False。

        顺序：先完成全部进贡（含两家进贡都要展示），再依次还贡；
        人类的任务由界面按钮触发，本方法只处理 AI 的任务。
        ``ai_pids``：联机主机传入 AI 代打座位集合（缺省 None=单机，除 0 号
        外全部代打——单机模式「我」恒为 0 号）。
        """
        g = self.game
        from ai import choose_tribute, choose_return
        if ai_pids is None:
            ai_pids = {1, 2, 3}
        pending = [(gg, rr) for gg, rr in g.tribute_pairs if gg not in g.tribute_done]
        if pending:
            ai_giver = next((gg for gg, _ in pending if gg in ai_pids), None)
            if ai_giver is None:
                return False   # 只剩人类进贡，等按钮
            g.submit_tribute(ai_giver, choose_tribute(g, ai_giver))
            self.sounds.play("tribute")
            return True
        awaiting = [r for r in g.tribute_done.values() if r not in g.return_done]
        ai_rec = next((r for r in awaiting if r in ai_pids), None)
        if ai_rec is None:
            return False       # 只剩人类还贡（或已全部完成）
        g.submit_return(ai_rec, choose_return(g, ai_rec))
        self.sounds.play("return_tribute")
        return True

    def on_pause(self):
        # Android 切后台：SDL_mixer 在后台会继续出声，停掉全部音频；
        # 返回 True 表示进程驻留内存
        self.sounds.stop_all()
        return True

    def on_resume(self):
        # 后台稍长时间后 Android 会销毁并重建 Surface，回前台黑屏与启动黑屏
        # 同因：Kivy 仅在 canvas 变脏时出帧（kivy/base.py EventLoop.idle），
        # Surface 重建阶段的帧全部被丢弃、此后不再产生新帧。复用启动期修复：
        # 短时标脏刷帧 + 重建牌桌纠正状态，并按设置恢复背景乐。
        for t in (0.2, 0.6, 1.2):
            Clock.schedule_once(lambda dt: self._force_redraw(), t)
        Clock.schedule_interval(self._boot_dirty, 0.25)
        Clock.schedule_once(self._stop_boot_dirty, 8)
        self.sounds.update_bgm()

    def on_stop(self):
        self.sounds.stop_all()
        self.close_net()
        self.lobby_screen.close_browser()   # 停 UDP 房间发现监听
        save_settings(self.settings)


def run(seed=None):
    GuandanApp(seed=seed).run()


if __name__ == "__main__":
    run()
