"""好友联机大厅与房间界面（好友联机阶段二）。

两个 Screen，风格与控件复用 ui.py（延迟 import 防循环，on_enter 整页重建，
与设置页/主菜单同一模式）：

- LobbyScreen（net_lobby）：昵称输入、「创建房间」（本机为主机）、附近房间
  发现列表（RoomBrowser UDP 聚合，0.5s 刷新，点击即加入）、手动输入主机 IP
  直连兜底、热点组网指引文案。
- RoomScreen（net_room）：牌桌座位图（0=下 1=右 2=上 3=左，与牌桌方位一致；
  0/2 一队、1/3 二队），点击空位入座/换座；主机可「开始游戏」（AI 补位，AI
  档位取设置页「AI 牌力」）与「解散房间」，客人「退出房间」；主机大字显示
  本机 IP 供直连兜底。

会话事件泵：RoomScreen 每帧（0.15s）调用 room 层 pump()（仅 UI 线程触碰
状态）并分发事件；座位表/连接状态变化经指纹比对触发整页重建。

Kivy 坐标惯例与 ui.py 一致：Panel/FlatButton/SeatCard 均为普通 Widget 子类，
子控件（Label）使用绝对屏幕坐标定位（普通 Widget 不做子坐标平移）。
"""

from __future__ import annotations

import json
import os
import queue

from kivy.clock import Clock
from kivy.core.window import Window
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.image import Image as KImage
from kivy.uix.scrollview import ScrollView
from kivy.uix.screenmanager import Screen
from kivy.uix.textinput import TextInput

from net import PROTOCOL_VER
from room import TIER_LABELS, RoomBrowser, RoomClient, RoomHost, local_ip
from ui import Panel   # 模块级：SeatCard 的基类在类定义时即需解析；
                       # ui.py 对本模块是函数内延迟导入，故无循环导入

TICK_LOBBY = 0.5       # 房间发现列表刷新周期（秒）
TICK_ROOM = 0.15       # 房间会话泵周期（秒）
RED = (0.55, 0.18, 0.16, 1)      # 与主菜单退出按钮同色（ui 无模块级 RED）


def _team_of(seat: int) -> str:
    """队名：0/2 一队、1/3 二队（pid%2 同队）。"""
    return "一队" if seat % 2 == 0 else "二队"


def _team_color(seat: int):
    """座位描边色：一队金、二队蓝。"""
    from ui import BLUE, GOLD
    return GOLD if seat % 2 == 0 else BLUE


def _bg_widgets(self, W, H):
    """两页共用的背景：深木底色 + 背景图（缺失时退回底色）。"""
    from ui import BG_PATH, WOOD_DARK, Panel
    self.add_widget(Panel(pos=(0, 0), size=(W, H), color=WOOD_DARK,
                          border=WOOD_DARK, radius=0))
    if os.path.exists(BG_PATH):
        self.add_widget(KImage(source=BG_PATH, pos=(0, 0), size=(W, H),
                               size_hint=(None, None), allow_stretch=True,
                               keep_ratio=False))


class LobbyScreen(Screen):
    """大厅页：创建房间 / 发现列表 / 手动直连 / 组网指引。"""

    def __init__(self, app, **kw):
        super().__init__(**kw)
        self.app = app
        self._browser = None
        self._browser_failed = False
        self._rooms_sig = None
        self._toast_text = ""
        self._toast_event = None
        self._name_text = ""
        self._ip_text = ""
        self._rooms_box = None      # 房间列表容器（_tick 局部刷新，不整页重建）

    # -- 生命周期 ----------------------------------------------------------

    def on_enter(self):
        self._name_text = str(self.app.settings.get("net_name") or "")
        self._ip_text = ""
        self._rebuild()
        Clock.schedule_interval(self._tick, TICK_LOBBY)

    def on_leave(self):
        Clock.unschedule(self._tick)
        self.close_browser()

    def close_browser(self):
        """停止房间发现（离开大厅/应用退出时调用）。"""
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        self._rooms_sig = None

    # -- 布局 ---------------------------------------------------------------

    def _rebuild(self):
        self.clear_widgets()
        self._rooms_box = None
        from ui import BLUE, GOLD, GREEN, FlatButton, Panel, _CJK_FONT, _label
        W, H = Window.width, Window.height
        fs = max(14, int(0.030 * H))
        _bg_widgets(self, W, H)
        self.add_widget(_label("好友组局", size=int(fs * 1.5), bold=True,
                               color=GOLD, pos=(int(W * 0.30), int(H * 0.86)),
                               widget_size=(int(W * 0.40), int(H * 0.10))))
        ih = max(30, int(0.055 * H))            # 输入框高
        bw, bh = int(0.20 * W), max(40, int(0.076 * H))

        # 左列：昵称 + 创建房间
        self.add_widget(_label("我的昵称", size=fs, color=GOLD, bold=True,
                               pos=(int(W * 0.05), int(H * 0.755)),
                               widget_size=(int(0.24 * W), int(0.045 * H))))
        self._name_input = TextInput(text=self._name_text, multiline=False,
                                     font_name=_CJK_FONT, font_size=fs,
                                     size_hint=(None, None),
                                     pos=(int(W * 0.05), int(H * 0.665)),
                                     size=(int(0.24 * W), ih),
                                     foreground_color=(1, 1, 1, 1),
                                     background_color=(0.16, 0.16, 0.16, 1))
        self._name_input.bind(text=lambda _i, v: setattr(self, "_name_text", v))
        self.add_widget(self._name_input)
        self.add_widget(FlatButton("创建房间", GREEN, self._on_create,
                                   pos=(int(W * 0.05), int(H * 0.535)),
                                   size=(bw, bh)))

        # 左列：手动直连兜底（热点网段多样/AP 隔离时 UDP 发现可能失效）
        self.add_widget(_label("主机 IP 直连", size=fs, color=GOLD, bold=True,
                               pos=(int(W * 0.05), int(H * 0.465)),
                               widget_size=(int(0.24 * W), int(0.045 * H))))
        self._ip_input = TextInput(text=self._ip_text, multiline=False,
                                   font_name=_CJK_FONT, font_size=fs,
                                   size_hint=(None, None),
                                   pos=(int(W * 0.05), int(H * 0.375)),
                                   size=(int(0.24 * W), ih),
                                   foreground_color=(1, 1, 1, 1),
                                   background_color=(0.16, 0.16, 0.16, 1),
                                   hint_text="如 192.168.1.100")
        self._ip_input.bind(text=lambda _i, v: setattr(self, "_ip_text", v))
        self.add_widget(self._ip_input)
        self.add_widget(FlatButton("直连加入", BLUE, self._on_direct_join,
                                   pos=(int(W * 0.05), int(H * 0.245)),
                                   size=(bw, bh)))
        self.add_widget(_label(f"本机 IP：{local_ip()}", size=int(fs * 0.7),
                               color=(0.78, 0.78, 0.78, 1),
                               pos=(int(W * 0.05), int(H * 0.185)),
                               widget_size=(int(0.30 * W), int(0.045 * H))))

        # 右列：附近房间发现列表
        px, pw = int(W * 0.40), int(W * 0.55)
        self.add_widget(Panel(pos=(px, int(H * 0.16)), size=(pw, int(H * 0.66)),
                              border=GOLD))
        self.add_widget(_label("附近的房间（同一 WiFi / 热点）", size=fs,
                               color=GOLD, bold=True,
                               pos=(px + int(0.01 * W), int(H * 0.745)),
                               widget_size=(pw - int(0.02 * W), int(0.05 * H))))
        scroll = ScrollView(size_hint=(None, None),
                            pos=(px + int(0.015 * W), int(H * 0.175)),
                            size=(pw - int(0.03 * W), int(H * 0.555)))
        box = BoxLayout(orientation="vertical", size_hint=(None, None),
                        spacing=int(0.012 * H), padding=int(0.008 * W))
        box.width = scroll.width
        box.bind(minimum_height=box.setter("height"))
        scroll.add_widget(box)
        self._rooms_box = box
        self.add_widget(scroll)

        # 底部：组网指引 + 返回
        self.add_widget(_label(
            "组网指引：没有 WiFi？任一台手机开个人热点（无需流量），"
            "其他人连上同一热点即可 ｜ Windows 主机首次监听如弹防火墙提示请选「允许」",
            size=int(fs * 0.72), color=(0.80, 0.72, 0.50, 1),
            pos=(int(W * 0.03), int(H * 0.015)),
            widget_size=(int(W * 0.72), int(0.05 * H))))
        self.add_widget(FlatButton("返回主界面", BLUE,
                                   lambda: self.app.goto("menu"),
                                   pos=(int(W * 0.80), int(H * 0.055)),
                                   size=(int(0.16 * W), max(36, int(0.065 * H)))))
        if self._toast_text:
            self.add_widget(_label(self._toast_text, size=fs,
                                   color=(1.0, 0.85, 0.35, 1),
                                   pos=(int(W * 0.28), int(H * 0.115)),
                                   widget_size=(int(W * 0.44), int(0.05 * H))))
        self._refresh_rooms()

    # -- 房间发现 ------------------------------------------------------------

    def _tick(self, dt):
        if self._browser is None and not self._browser_failed:
            try:
                self._browser = RoomBrowser()
            except OSError:
                self._browser_failed = True   # 只提示一次，避免反复整页重建
                self._toast("房间发现不可用（端口被占用），可手动输入 IP 直连")
                return
        if self._browser is None:
            return
        rooms = [r for r in self._browser.snapshot()
                 if r["ver"] == PROTOCOL_VER]   # 仅展示协议版本一致的房间
        sig = [(r["ip"], r["port"], r["name"], r["players"]) for r in rooms]
        if sig != self._rooms_sig:
            self._rooms_sig = sig
            self._refresh_rooms(rooms)

    def _refresh_rooms(self, rooms=None):
        """重绘房间列表（局部重建，不打断输入框焦点）。"""
        from ui import BLUE, FlatButton, _label
        if self._rooms_box is None:
            return
        W, H = Window.width, Window.height
        rh = max(34, int(0.066 * H))
        self._rooms_box.clear_widgets()
        if rooms is None:
            rooms = [r for r in (self._browser.snapshot()
                                 if self._browser else [])
                     if r["ver"] == PROTOCOL_VER]
        if not rooms:
            self._rooms_box.add_widget(_label(
                "未发现房间\n请让主机先「创建房间」，或手动输入主机 IP",
                size=int(0.026 * H), color=(0.75, 0.75, 0.75, 1),
                size_hint=(1, None), height=rh * 2))
            return
        for r in rooms:
            text = (f"{r['name']}  {r['host_name']}  "
                    f"{r['players']}/4 人  [{r['ip']}]")
            self._rooms_box.add_widget(FlatButton(
                text, BLUE, (lambda ip=r["ip"], port=r["port"]:
                             self._on_join(ip, port)),
                size_hint=(None, None),
                size=(self._rooms_box.width - int(0.016 * W), rh)))

    # -- 操作 ----------------------------------------------------------------

    def _nick(self) -> str:
        from ui import save_settings
        name = (self._name_text or "").strip()[:12]
        self.app.settings["net_name"] = name
        save_settings(self.app.settings)
        return name

    def _on_create(self):
        try:
            host = RoomHost(self._nick() or "房主",
                            tier=self.app.settings.get("ai_strength", "advanced"))
        except OSError:
            self._toast("创建失败：端口 17666 被占用，请先关闭其他房间")
            return
        self._stop_toast()
        self.app.net_session = host
        self.app.goto("net_room")

    def _on_join(self, ip, port):
        ip = (ip or "").strip()
        if not ip:
            self._toast("请先选择房间或输入主机 IP")
            return
        self._start_join(ip, port)

    def _on_direct_join(self):
        ip = (self._ip_text or "").strip()
        if not ip:
            self._toast("请输入主机 IP")
            return
        from net import DEFAULT_PORT
        self._start_join(ip, DEFAULT_PORT)

    def _start_join(self, ip, port):
        self._stop_toast()
        cli = RoomClient(self._nick() or "玩家")
        self.app.net_session = cli
        cli.join(ip, port)
        self.app.goto("net_room")

    # -- 提示条 ---------------------------------------------------------------

    def _toast(self, text):
        self._toast_text = text
        self._rebuild()
        if self._toast_event is not None:
            self._toast_event.cancel()
        self._toast_event = Clock.schedule_once(
            lambda dt: self._hide_toast(), 2.4)

    def _stop_toast(self):
        if self._toast_event is not None:
            self._toast_event.cancel()
            self._toast_event = None
        self._toast_text = ""

    def _hide_toast(self):
        self._toast_event = None
        if self._toast_text and self.manager is not None \
                and self.manager.current == "net_lobby":
            self._toast_text = ""
            self._rebuild()


class RoomScreen(Screen):
    """房间页：座位图选座/换座、开始游戏（主机）、解散/退出、状态提示。"""

    def __init__(self, app, **kw):
        super().__init__(**kw)
        self.app = app
        self._toast_text = ""
        self._toast_event = None
        self._sig = None
        self._back_event = None     # 客人被拒/掉线后自动回大厅的定时器

    # -- 生命周期 ----------------------------------------------------------

    def on_enter(self):
        self._sig = None
        self._rebuild()
        Clock.schedule_interval(self._tick, TICK_ROOM)

    def on_leave(self):
        Clock.unschedule(self._tick)
        if self._back_event is not None:
            self._back_event.cancel()
            self._back_event = None

    # -- 会话泵 --------------------------------------------------------------

    def _tick(self, dt):
        s = self.app.net_session
        if s is None:
            return
        s.pump()
        while True:
            try:
                ev = s.events.get_nowait()
            except queue.Empty:
                break
            self._handle(ev)
        sig = self._fingerprint(s)
        if sig != self._sig:
            self._sig = sig
            self._rebuild()

    @staticmethod
    def _fingerprint(s):
        """座位表/连接状态指纹：变化才整页重建。"""
        seats = json.dumps(s.seats_snapshot(), ensure_ascii=False,
                           sort_keys=True)
        if s.is_host:
            return (seats, s.started)
        return (seats, s.welcomed, s.started, s.room_name,
                s.my_seat is not None)

    def _handle(self, ev):
        kind = ev[0]
        s = self.app.net_session
        if kind == "joined" and s is not None and s.is_host:
            self._toast(f"{ev[2]['name']} 加入了房间")
        elif kind == "rejoined":
            self._toast(f"{ev[2]['name']} 重连归位（座位 {ev[2]['seat']}）")
        elif kind == "left":
            self._toast(f"{ev[2]['name']} {ev[2]['reason']}")
        elif kind == "started":
            # 开局/重开一场：建局并进入牌桌（主机与客人同路径）
            self._toast("对局已开始")
            self.app.net_on_started(ev[2])
        elif kind == "resync":
            # 断线重连归位：按全量动作流重建镜像后进牌桌
            self._toast("已重新同步牌局")
            self.app.net_on_started(ev[2])
        elif kind == "act":
            # 牌桌页之外的广播动作：就地套用到镜像，回到牌桌即为最新局面
            self.app.net_apply_act(ev[2])
        elif kind == "acted":
            self.app.table._net_after_remote(ev[2])
        elif kind == "act_rejected":
            self._toast(f"动作被拒绝：{ev[2]}")
        elif kind == "ai_takeover":
            self._toast(f"{ev[2].get('name', '玩家')} 掉线超时，已由 AI 接管")
        elif kind == "end":
            # 阶段四：主机分发的赛末复盘记录（客人暂在房间页也照常入库）
            self.app.table.net_accept_end(ev[2])
            self._toast("本场复盘已保存，可在复盘页查看")
        elif kind == "sit_rejected":
            self._toast(f"换座失败：{ev[2]}")
        elif kind == "error":
            self._toast(str(ev[2]))
        elif kind in ("rejected", "disconnected"):
            # 客人：加入被拒 / 与主机断开 → 提示后自动回大厅
            if s is not None:
                s.close()
                self.app.net_session = None
            self._toast(str(ev[2]))
            if self._back_event is None:
                self._back_event = Clock.schedule_once(
                    lambda dt: self._back_to_lobby(), 1.6)
        # seats/joined（客人）/closed 事件由指纹比对或按钮路径覆盖，无需处理

    def _back_to_lobby(self):
        self._back_event = None
        if self.manager is not None and self.manager.current == "net_room" \
                and self.app.net_session is None:
            self.app.goto("net_lobby")

    # -- 操作 ------------------------------------------------------------------

    def _on_seat_tap(self, seat: int):
        s = self.app.net_session
        if s is None or s.started:
            return
        if not s.try_sit(seat):
            self._toast("暂时无法入座" if s.is_host else "尚未加入房间")

    def _on_start(self):
        s = self.app.net_session
        if s is None or not s.is_host:
            return
        lv = self.app.settings.get("level_start", 2)
        if s.start_game(levels=[lv, lv]) is None:
            self._toast("对局已开始")

    def _on_back_table(self):
        """对局中从房间页返回牌桌（ started 后占位改为可点击返回）。"""
        self.app.goto("table")

    def _on_dismiss(self):
        """主机解散房间。"""
        self._toast_text = ""
        self.app.close_net()
        self.app.goto("net_lobby")

    def _on_exit(self):
        """客人退出房间。"""
        s = self.app.net_session
        self._toast_text = ""
        if s is not None:
            s.leave()
        self.app.net_session = None
        self.app.goto("net_lobby")

    # -- 布局 ------------------------------------------------------------------

    def _rebuild(self):
        self.clear_widgets()
        from ui import GOLD, GREEN, PANEL, FlatButton, Panel, _label
        W, H = Window.width, Window.height
        fs = max(14, int(0.030 * H))
        _bg_widgets(self, W, H)
        s = self.app.net_session
        if s is None:                      # 会话已结束（回大厅前的过渡态）
            self.add_widget(_label("会话已结束", size=int(fs * 2), color=GOLD,
                                   pos=(int(W * 0.3), int(H * 0.45)),
                                   widget_size=(int(W * 0.4), int(H * 0.1))))
            return
        self.add_widget(_label(s.room_name or "牌桌房间", size=int(fs * 1.4),
                               bold=True, color=GOLD,
                               pos=(int(W * 0.30), int(H * 0.875)),
                               widget_size=(int(W * 0.40), int(H * 0.09))))
        if s.is_host:
            self.add_widget(_label(
                f"本机 IP：{local_ip()}（好友也可在大厅手动输入直连）",
                size=int(fs * 0.8), color=(0.82, 0.82, 0.82, 1),
                pos=(int(W * 0.25), int(H * 0.80)),
                widget_size=(int(W * 0.50), int(H * 0.055))))

        # 中央牌桌 + 四个座位卡（0=下 1=右 2=上 3=左，与牌桌方位一致）
        tw, th = int(W * 0.23), int(H * 0.22)
        tx, ty = int(W * 0.5 - tw / 2), int(H * 0.42)
        self.add_widget(Panel(pos=(tx, ty), size=(tw, th),
                              color=PANEL, border=GOLD, radius=16))
        self.add_widget(_label("掼蛋", size=int(fs * 1.2), bold=True,
                               color=(0.92, 0.88, 0.70, 1),
                               pos=(tx, ty + th // 2 - int(0.03 * H)),
                               widget_size=(tw, int(0.06 * H))))
        cw, ch = int(W * 0.17), int(H * 0.095)
        centers = {
            0: (int(W * 0.5), int(H * 0.375)),
            1: (int(W * 0.715), int(H * 0.53)),
            2: (int(W * 0.5), int(H * 0.685)),
            3: (int(W * 0.285), int(H * 0.53)),
        }
        my_seat = 0 if s.is_host else s.my_seat
        started = s.started
        can_sit = not started and (s.is_host or s.welcomed)
        for seat, (cx, cy) in centers.items():
            info = self._seat_info(s, seat, my_seat)
            card = SeatCard(
                on_tap=(lambda st=seat: self._on_seat_tap(st))
                if can_sit and info["empty"] else None,
                pos=(cx - cw // 2, cy - ch // 2), size=(cw, ch),
                color=(0.13, 0.28, 0.18, 1),
                border=(1, 1, 1, 1) if seat == my_seat else _team_color(seat))
            card.add_widget(_label(info["name"], size=int(ch * 0.36),
                                   bold=True,
                                   color=(1, 1, 1, 1) if not info["empty"]
                                   else (0.80, 0.80, 0.80, 1),
                                   pos=(cx - cw // 2 + 2, cy + int(ch * 0.06)),
                                   widget_size=(cw - 4, int(ch * 0.48))))
            card.add_widget(_label(info["sub"], size=int(ch * 0.28),
                                   color=(0.85, 0.75, 0.45, 1),
                                   pos=(cx - cw // 2 + 2,
                                        cy - ch // 2 + int(ch * 0.08)),
                                   widget_size=(cw - 4, int(ch * 0.34))))
            self.add_widget(card)
        self.add_widget(_label(
            "点击空位入座或换座 ｜ 0/2 一队 · 1/3 二队 ｜ 空位由 AI 补齐",
            size=int(fs * 0.72), color=(0.78, 0.78, 0.78, 1),
            pos=(int(W * 0.20), int(H * 0.295)),
            widget_size=(int(W * 0.60), int(H * 0.04))))

        # 按钮排（主机：开始+解散；客人：退出）
        bh = max(42, int(0.075 * H))
        bw = int(0.19 * W)
        if s.is_host:
            self.add_widget(FlatButton("开始游戏", GREEN, self._on_start,
                                       pos=(int(W * 0.5 - bw - 0.015 * W),
                                            int(H * 0.165)),
                                       size=(bw, bh), enabled=not started))
            self.add_widget(FlatButton("解散房间", RED, self._on_dismiss,
                                       pos=(int(W * 0.5 + 0.015 * W),
                                            int(H * 0.165)),
                                       size=(bw, bh)))
        else:
            self.add_widget(FlatButton("退出房间", RED, self._on_exit,
                                       pos=(int(W * 0.5 - bw // 2),
                                            int(H * 0.165)),
                                       size=(bw, bh)))

        if started:
            self.add_widget(Panel(pos=(int(W * 0.335), int(H * 0.475)),
                                  size=(int(W * 0.33), int(H * 0.11)),
                                  color=(0.10, 0.22, 0.14, 0.94), border=GOLD,
                                  radius=12))
            self.add_widget(_label("对局进行中", size=int(fs * 1.1),
                                   bold=True, color=GOLD,
                                   pos=(int(W * 0.335), int(H * 0.475)),
                                   widget_size=(int(W * 0.33), int(H * 0.055))))
            self.add_widget(FlatButton("回到牌桌", GREEN, self._on_back_table,
                                       pos=(int(W * 0.335 + W * 0.08),
                                            int(H * 0.475 + H * 0.055)),
                                       size=(int(W * 0.17), int(H * 0.05))))
        if self._toast_text:
            self.add_widget(_label(self._toast_text, size=fs,
                                   color=(1.0, 0.85, 0.35, 1),
                                   pos=(int(W * 0.25), int(H * 0.055)),
                                   widget_size=(int(W * 0.50), int(H * 0.05))))

    @staticmethod
    def _seat_info(s, seat: int, my_seat) -> dict:
        """座位卡文案：name / sub / empty。"""
        info = next((x for x in s.seats_snapshot() if x["seat"] == seat), None)
        if info is None:
            return {"name": "空位", "sub": "点击入座", "empty": True}
        if info["is_ai"]:
            tier = TIER_LABELS.get(info.get("tier") or "", "中级")
            return {"name": "AI 补位", "sub": f"{tier}｜点击入座", "empty": True}
        name = info.get("name") or "玩家"
        if seat == my_seat:
            role = "房主" if s.is_host else "我"
            return {"name": name, "sub": f"{role} · {_team_of(seat)}",
                    "empty": False}
        return {"name": name, "sub": f"已入座 · {_team_of(seat)}",
                "empty": False}

    # -- 提示条 ------------------------------------------------------------

    def _toast(self, text):
        self._toast_text = text
        self._rebuild()
        if self._toast_event is not None:
            self._toast_event.cancel()
        self._toast_event = Clock.schedule_once(
            lambda dt: self._hide_toast(), 2.4)

    def _hide_toast(self):
        self._toast_event = None
        if self._toast_text and self.manager is not None \
                and self.manager.current == "net_room":
            self._toast_text = ""
            self._sig = None               # 置空指纹，下帧由 _tick 触发重建


class SeatCard(Panel):
    """座位卡：可点击的 Panel（空位可点入座，其余仅展示）。"""

    def __init__(self, on_tap=None, **kw):
        kw.setdefault("size_hint", (None, None))
        super().__init__(**kw)
        self.on_tap = on_tap

    def on_touch_down(self, touch):
        if self.on_tap is not None and self.collide_point(*touch.pos):
            self.on_tap()
            return True
        return False
