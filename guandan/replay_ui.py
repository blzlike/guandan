"""复盘界面（阶段四 + 2026-10-14 优化）：存档清单 + 四列回放。

清单：按时间从旧到新排列；每行 = 存档名 + 删除/打开/载入三按钮。
存档命名（方案 A）：``#NNN 日期 我方打X 我方胜/负 我名次+对家名次``（空格分隔；
未完赛时不显示胜负与名次字段）。
载入 = 用存档 seed 重建第 1 局发牌态交给牌桌（比赛起点，复盘/打分可录）。

回放页四列（手牌 2/5 | 出牌 1/5 | 理由 1/5 | 按钮 1/5）× 四行（我/下家/
对家/上家），浅色分隔线。手牌区左缘显示头像+姓名+剩牌数（样式同正常牌桌
上家，头像贴屏幕边 0.2 倍头像宽，手牌底部对齐紧贴行下沿、左贴头像）；出牌
区每人 3 槽位显示本轮最近 3 次行牌，编号为轮内 play/pass 顺序号（每轮从 1
起，过牌显示「不出」；下一轮/上一轮按下即切轮清空重新编号）；理由区逐行对齐
（AI 动作理由由正常牌局 Recorder 的 reason 字段提供，旧档留空）。
按钮三组：下一步/上一步、下一轮/上一轮（下一轮 0.5s/步自动播放至本轮收牌）、
重新开始/载入局势/返回列表。载入局势 = 当前回放状态克隆交给牌桌；进贡阶段
由 AI 自动完成进贡/还贡后开局（已确认口径）；中途局面非比赛起点，续局不
录制复盘（开新局恢复）。

逻辑层 ReplaySession（含轮次/顺序号推导）在 replay.py，本文件只做 UI 与
存取；ui 组件（FlatButton/CardWidget/_label 等）一律在使用处延迟 import，
避免与 ui.py 循环依赖（ui.py 运行时挂载本模块）。
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from typing import Any, Dict, List, Optional

from kivy.clock import Clock
from kivy.core.window import Window
from kivy.uix.screenmanager import Screen

from cards import RANK_LABELS, build_deck, sort_key
from replay import ReplaySession, load_json

# 可写目录（与 ui.py settings.json 同套 frozen 规则：exe 旁 / 源码目录旁）
if getattr(sys, "frozen", False):
    _HERE = os.path.dirname(sys.executable)
else:
    _HERE = os.path.dirname(os.path.abspath(__file__))
REPLAYS_DIR = os.path.join(_HERE, "replays")

RANK_NAMES = ("头游", "二游", "三游", "末游")
_CARD_BY_EID = {c.eid: c for c in build_deck()}   # eid 确定性分配，展示重建用


# ---------------------------------------------------------------- 命名与存取
def _parse_no(basename: str) -> Optional[int]:
    """从 ``#NNN、…`` 文件名解析编号；旧格式返回 None。"""
    if basename.startswith("#") and len(basename) >= 4 and basename[1:4].isdigit():
        return int(basename[1:4])
    return None


def next_replay_no() -> int:
    """新存档编号 = replays/ 现有 #NNN 最大值 + 1。"""
    mx = 0
    if os.path.isdir(REPLAYS_DIR):
        for fn in os.listdir(REPLAYS_DIR):
            no = _parse_no(fn)
            if no is not None:
                mx = max(mx, no)
    return mx + 1


def replay_basename(record: Dict[str, Any], no: int,
                    date: Optional[str] = None) -> str:
    """按方案 A 生成存档名（空格分隔）：#NNN 日期 我方打X [胜负] [名次]。

    未完赛时不显示胜负与名次字段（2026-10-14 用户调整）。
    """
    res = record.get("result") or {}
    lv = res.get("levels") or record.get("levels") or {}
    try:
        play_x = RANK_LABELS[int(lv.get("0", lv.get(0, 2)))]
    except Exception:
        play_x = "?"
    if not date:
        date = f"{datetime.datetime.now():%Y%m%d_%H%M%S}"
    parts = [f"#{no:03d}", date, f"我方打{play_x}"]
    w = res.get("match_winner")
    if w is not None:
        parts.append("我方胜" if w == 0 else "我方负")
        rounds = res.get("rounds") or []
        if rounds:
            fo = rounds[-1].get("finish_order") or []
            parts.append("".join(
                RANK_NAMES[fo.index(p)] if p in fo else "?" for p in (0, 2)))
    return " ".join(parts)


def save_replay(record: Dict[str, Any], prev: Optional[str] = None) -> str:
    """保存 record 到 replays/，返回文件路径。

    ``prev`` 为同一场比赛此前的保存路径：编号与日期沿用、赛果字段重算
    （名字变了则写新删旧，同一场只留最新快照）。
    """
    os.makedirs(REPLAYS_DIR, exist_ok=True)
    if prev and os.path.exists(prev):
        segs = os.path.basename(prev)[:-5].split()
        no = _parse_no(os.path.basename(prev)) or next_replay_no()
        date = segs[1] if len(segs) >= 2 else None
        path = os.path.join(REPLAYS_DIR,
                            replay_basename(record, no, date) + ".json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False)
        if os.path.abspath(path) != os.path.abspath(prev):
            try:
                os.remove(prev)
            except OSError:
                pass
        return path
    path = os.path.join(REPLAYS_DIR,
                        replay_basename(record, next_replay_no()) + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False)
    return path


def list_replays() -> List[Dict[str, Any]]:
    """扫描 replays/ 下全部记录，按修改时间从旧到新返回摘要列表。

    单项：{path, name}；损坏文件静默跳过。
    """
    if not os.path.isdir(REPLAYS_DIR):
        return []
    fns = [f for f in os.listdir(REPLAYS_DIR) if f.endswith(".json")]
    fns.sort(key=lambda f: os.path.getmtime(os.path.join(REPLAYS_DIR, f)))
    out = []
    for fn in fns:
        path = os.path.join(REPLAYS_DIR, fn)
        try:
            load_json(path)
        except Exception:
            continue
        out.append({"path": path, "name": fn[:-5]})
    return out


class ReplayScreen(Screen):
    """复盘页：session 为 None 时显示列表，否则显示逐动作回放。"""

    def __init__(self, app, **kw):
        super().__init__(**kw)
        self.app = app
        self.session: Optional[ReplaySession] = None
        self._msg = ""                   # 一次性提示（载入失败等）
        self._auto_target: Optional[int] = None   # 「下一轮」自动播放目标
        self._auto_ev = None
        self.view_trick: Optional[int] = None    # 出牌区显示轮次；None=自动
        Window.bind(on_resize=self._on_win_resize)

    # ------------------------------------------------------------ 导航
    def on_enter(self):
        self._rebuild()

    def _on_win_resize(self, *a):
        if self.manager is not None and self.manager.current == self.name:
            self._rebuild()

    def _rebuild(self):
        self.clear_widgets()
        if self.session is None:
            self._build_list()
        else:
            self._build_replay()
        # 列表↔回放为纯组件树变化，无 _pump 帧源；Kivy 仅在 window canvas
        # 脏时出帧（base.py EventLoop.idle），必须显式标脏否则黑屏
        Window.canvas.ask_update()

    def _stop_auto(self):
        if self._auto_ev is not None:
            Clock.unschedule(self._auto_ev)
        self._auto_ev = None
        self._auto_target = None

    def _back_to_list(self):
        self._stop_auto()
        self.session = None
        self.view_trick = None
        self._msg = ""
        self._rebuild()

    def _open(self, path: str):
        self._stop_auto()
        self.view_trick = None
        try:
            self.session = ReplaySession(load_json(path))
            self._msg = ""
        except Exception as e:
            self.session = None
            self._msg = f"无法打开：{e}"
        self._rebuild()

    def _delete(self, path: str):
        try:
            os.remove(path)
        except OSError as e:
            self._msg = f"删除失败：{e}"
        self._rebuild()

    def _load_match(self, path: str):
        """清单「载入」：用存档 seed 重建第 1 局发牌态，交给牌桌正常对局。"""
        try:
            import random
            from game import GuandanGame
            rec = load_json(path)
            seed = rec.get("seed")
            if seed is None:
                raise ValueError("存档缺少 seed，无法载入")
            game = GuandanGame(rng=random.Random(seed))
            lv = rec.get("levels") or {}
            if lv:
                game.levels = {int(k): v for k, v in lv.items()}
            self.session = None
            self.app.table.load_game(game, seed=seed)
            self.app.goto("table")
        except Exception as e:
            self._msg = f"载入失败：{e}"
            self._rebuild()

    def _load_state(self):
        """回放页「载入局势」：当前状态克隆交给牌桌；进贡阶段 AI 自动完成。"""
        try:
            from ai import choose_return, choose_tribute
            self._stop_auto()
            seed = self.session.record.get("seed")
            game = self.session.game.clone()
            while game.phase == "tribute":
                pending = [(g, r) for g, r in game.tribute_pairs
                           if g not in game.tribute_done]
                if pending:
                    giver = pending[0][0]
                    game.submit_tribute(giver, choose_tribute(game, giver))
                else:
                    receiver = next(r for r in game.tribute_done.values()
                                    if r not in game.return_done)
                    game.submit_return(receiver, choose_return(game, receiver))
            self.session = None
            self.app.table.load_game(game, seed=seed)
            self.app.goto("table")
        except Exception as e:
            self._msg = f"载入失败：{e}"
            self._rebuild()

    # ------------------------------------------------------------ 列表页
    def _build_list(self):
        from ui import BLUE, FlatButton, _label   # 延迟 import 防循环
        RED = (0.62, 0.20, 0.18, 1)
        GREEN = (0.20, 0.60, 0.30, 1)
        W, H = Window.width, Window.height
        bh = int(0.058 * H)
        fs = max(14, int(0.028 * H))
        self.add_widget(FlatButton("返回牌桌", BLUE,
                                   lambda: self.app.goto("table"),
                                   pos=(int(0.010 * W), int(H - bh - 0.010 * H)),
                                   size=(int(0.100 * W), bh)))
        self.add_widget(_label("[b]复盘列表[/b]（旧→新）", size=int(fs * 1.25),
                               pos=(int(0.36 * W), int(H - bh - 0.010 * H)),
                               widget_size=(int(0.28 * W), int(0.05 * H))))
        if self._msg:
            self.add_widget(_label(self._msg, size=fs,
                                   color=(1.0, 0.55, 0.45, 1),
                                   pos=(int(0.10 * W), int(H - bh - 0.060 * H)),
                                   widget_size=(int(0.80 * W), int(0.04 * H))))
        items = list_replays()
        if not items:
            self.add_widget(_label(
                "暂无已保存牌局：牌局结算页点「保存复盘」后出现在这里",
                size=fs, pos=(int(0.15 * W), int(0.52 * H)),
                widget_size=(int(0.70 * W), int(0.06 * H))))
            return
        from kivy.uix.boxlayout import BoxLayout
        from kivy.uix.gridlayout import GridLayout
        from kivy.uix.scrollview import ScrollView
        sv = ScrollView(pos=(int(0.05 * W), int(0.06 * H)),
                        size=(int(0.90 * W), int(0.83 * H)),
                        size_hint=(None, None),      # Screen 是 RelativeLayout，会应用默认 (1,1) hint 覆盖 size
                        do_scroll_x=False)
        grid = GridLayout(cols=1, spacing=int(0.012 * H),
                          padding=int(0.010 * W), size_hint=(1, None))
        rh = int(0.062 * H)
        for it in items:
            row = BoxLayout(orientation="horizontal", spacing=int(0.006 * W),
                            size_hint=(1, None), size=(0, rh))
            lb = _label(it["name"], size=max(12, int(0.022 * H)),
                        halign="left", widget_size=(int(0.58 * W), rh))
            lb.size_hint = (0.61, 1)     # BoxLayout 内按宽度比例占位
            row.add_widget(lb)
            row.add_widget(FlatButton("删除", RED,
                                      (lambda p=it["path"]: self._delete(p)),
                                      size_hint=(0.13, 1)))
            row.add_widget(FlatButton("打开", BLUE,
                                      (lambda p=it["path"]: self._open(p)),
                                      size_hint=(0.13, 1)))
            row.add_widget(FlatButton("载入", GREEN,
                                      (lambda p=it["path"]: self._load_match(p)),
                                      size_hint=(0.13, 1)))
            grid.add_widget(row)
        grid.bind(minimum_height=grid.setter("height"))
        sv.add_widget(grid)
        self.add_widget(sv)

    # ------------------------------------------------------------ 回放页
    def _build_replay(self):
        from ui import BLUE, FlatButton, _label
        GREEN = (0.20, 0.60, 0.30, 1)
        W, H = Window.width, Window.height
        s, g = self.session, self.session.game
        fs = max(12, int(0.024 * H))
        # 背景 + 浅色分隔线（四列三行内缝）
        from ui import Panel, WOOD
        self.add_widget(Panel(pos=(0, 0), size=(W, H), color=WOOD,
                              border=WOOD, radius=0))
        LINE = (0.93, 0.90, 0.82, 0.35)
        for fx in (0.4, 0.6, 0.8):
            self.add_widget(Panel(pos=(int(fx * W), 0), size=(1, H),
                                  color=LINE, border=LINE, radius=0))
        for fy in (0.25, 0.5, 0.75):
            self.add_widget(Panel(pos=(0, int(fy * H)), size=(W, 1),
                                  color=LINE, border=LINE, radius=0))
        # 四行：我 / 下家 / 对家 / 上家（自上而下）
        row_h = int(H / 4)
        for k, pid in enumerate((0, 1, 2, 3)):
            top = H - k * row_h          # 本行顶边（Kivy y 自下而上）
            self._row_hand(g, pid, top, row_h, fs)
            self._row_moves(s, g, pid, top, row_h, fs)
            self._row_reasons(s, pid, top, row_h, fs)
        # 按钮列
        x0 = int(0.8 * W) + int(0.012 * W)
        bw = int(0.2 * W) - 2 * int(0.012 * W)
        bh = int(0.052 * H)
        y = H - int(0.015 * H) - int(0.04 * H)
        self.add_widget(_label(
            f"[b]第{g.round_number}局 打{RANK_LABELS[g.level]}　"
            f"{s.index}/{s.total()}[/b]",
            size=fs, pos=(x0, y), widget_size=(bw, int(0.04 * H))))
        y -= int(0.02 * H)
        groups = [("下一步", GREEN, lambda: self._nav(self._step_forward)),
                  ("上一步", BLUE, lambda: self._nav(self._step_back)),
                  ("下一轮", GREEN, self._play_trick),
                  ("上一轮", BLUE, self._prev_trick),
                  ("重新开始", BLUE, lambda: self._nav(self._go_start)),
                  ("载入局势", GREEN, self._load_state),
                  ("返回列表", BLUE, self._back_to_list)]
        # 三组拉开：首钮下沿从信息栏起，末钮（返回列表）下沿落在上家行中部
        y_top = y - int(0.02 * H) - bh
        y_bot = int(0.125 * H)
        unit = max(int(0.006 * H), (y_top - y_bot - 6 * bh) / 8.0)
        yy = y_top
        for i, (t, c, cb) in enumerate(groups):
            self.add_widget(FlatButton(t, c, cb, pos=(x0, yy), size=(bw, bh)))
            yy -= bh + (2 * unit if i in (1, 3) else unit)

    # ---- 手牌列：左缘头像+姓名+剩牌数（样式同正常牌桌上家）+ 横排分组手牌
    def _row_hand(self, g, pid, top, row_h, fs):
        import os as _os
        from ui import KImage, PLAYER_NAMES, _label
        from ui import AVATAR_PATHS, WHITE
        W = Window.width
        side = int(row_h * 0.34)
        gap = max(2, int(0.2 * side))          # 头像/手牌贴边间隔=0.2 倍头像宽
        cx = gap + side // 2
        info_w = max(side, int(0.07 * W))
        y = top - int(row_h * 0.08)
        self.add_widget(_label(PLAYER_NAMES[pid], size=fs, color=WHITE,
                               bold=True, pos=(cx - info_w // 2, y - fs),
                               widget_size=(info_w, fs + 4)))
        y -= fs + int(row_h * 0.05)
        path = AVATAR_PATHS.get(pid, "")
        if _os.path.exists(path):
            self.add_widget(KImage(source=path, allow_stretch=True,
                                   keep_ratio=True, size_hint=(None, None),
                                   pos=(cx - side // 2, y - side),
                                   size=(side, side)))
        else:
            self.add_widget(_label(PLAYER_NAMES[pid][:1], size=int(side * 0.6),
                                   pos=(cx - side // 2, y - side),
                                   widget_size=(side, side)))
        y -= side + int(row_h * 0.04)
        self.add_widget(_label(str(g.hand_count(pid)), size=fs,
                               color=(1.0, 0.85, 0.40, 1), bold=True,
                               pos=(cx - info_w // 2, y - fs),
                               widget_size=(info_w, fs + 4)))
        ch = int(row_h * 0.42)
        # 同正常模式「我的手牌」：底部对齐紧贴行下沿，自左向右牌力减小
        self._draw_hand_h(g, pid, cx + side // 2 + gap,
                          int(0.4 * W) - 4, top - row_h, ch, up=True)

    def _hand_groups(self, g, pid):
        """手牌按牌力降序分组（同 rank 一组；组内 sort_key 稳定序）。"""
        cards = sorted(g.hands[pid], key=lambda c: sort_key(c, g.level))
        groups = []
        for c in cards:
            if groups and groups[-1][0].rank == c.rank:
                groups[-1].append(c)
            else:
                groups.append([c])
        return groups

    def _draw_hand_h(self, g, pid, x_left, x_right, y_base, ch, up):
        """横排手牌（同正常模式「我的手牌」分组方式）：从左到右牌力减小，
        同点数叠放；总宽超区压缩列距（下限 0.30cw）；组内偏移 0.24ch，
        k=0 最后绘制整张可见。up=False 顶对齐向下叠（复盘行内用）。"""
        from ui import CardWidget
        groups = self._hand_groups(g, pid)
        if not groups:
            return
        cw = int(ch * 0.72)
        nc = len(groups)
        span = x_right - x_left
        if span <= cw:
            return
        col_step = min(cw + 8, (span - cw) / (nc - 1)) if nc > 1 else cw + 8
        col_step = max(cw * 0.30, col_step)
        total = (nc - 1) * col_step + cw
        x0 = x_left + max(0, (span - total) / 2)
        step = 0.24 * ch
        for gi, col in enumerate(groups):
            gx = x0 + gi * col_step
            for k in reversed(range(len(col))):
                y = (y_base + k * step) if up else (y_base - ch - k * step)
                self.add_widget(CardWidget(col[k], g.level,
                                           pos=(gx, y), size=(cw, ch)))

    # ---- 出牌列：本轮最近 3 次行牌（编号=轮内顺序号；过牌显示「不出」）
    def _display_trick(self) -> Optional[int]:
        """当前出牌区应显示的轮次。

        view_trick 由「下一轮/上一轮」显式设定（按下即切到新轮→清空重
        新从 1 编号）；手动步进等场景为 None，自动取最后已套用行牌之轮。
        """
        if self.view_trick is not None:
            return self.view_trick
        return self.session._trick_of_applied(self.session.index)

    def _recent_moves(self, pid: int) -> List[tuple]:
        """返回 [(动作 index, 动作 dict), ...]（时间序，最多 3 条）。"""
        s = self.session
        rnd = s.game.round_number
        disp = self._display_trick()
        if disp is None:
            return []
        out = []
        for i in range(min(s.index, len(s.actions))):
            a = s.actions[i]
            if a.get("round", 1) != rnd or s.meta[i]["trick"] != disp:
                continue
            if a["pid"] == pid:
                out.append((i, a))
        return out[-3:]

    def _slot_geo(self, top, row_h):
        """3 槽位几何：[(出牌列左缘, 槽顶 y, 槽高), ...] 自上而下（旧→新）。"""
        W = Window.width
        slot_h = int(row_h / 3)
        x1 = int(0.4 * W) + 2
        return [(x1, top - k * slot_h, slot_h) for k in range(3)]

    def _row_moves(self, s, g, pid, top, row_h, fs):
        from ui import CardWidget, _label
        W = Window.width
        moves = self._recent_moves(pid)
        geo = self._slot_geo(top, row_h)
        for k in range(3):
            x1, s_top, slot_h = geo[k]
            if k >= len(moves):
                continue
            i, a = moves[k]
            seq = s.meta[i]["seq"]
            self.add_widget(_label(str(seq), size=fs,
                                   color=(1.0, 0.92, 0.55, 1), bold=True,
                                   pos=(x1, s_top - slot_h),
                                   widget_size=(int(0.030 * W), slot_h)))
            cx = x1 + int(0.032 * W)
            if a["kind"] == "pass":
                self.add_widget(_label("不出", size=fs,
                                       color=(0.75, 0.75, 0.75, 1),
                                       pos=(cx, s_top - slot_h),
                                       widget_size=(int(0.05 * W), slot_h)))
                continue
            cards = [_CARD_BY_EID[e] for e in a["eids"] if e in _CARD_BY_EID]
            ch = int(slot_h * 0.72)
            cw = int(ch * 0.72)
            n = len(cards)
            room = int(0.6 * W) - 4 - cx
            step = cw + 2
            if n > 1 and cw + (n - 1) * step > room:
                step = max(cw * 0.30, (room - cw) / (n - 1))
            y = s_top - slot_h + int((slot_h - ch) / 2)
            for j, c in enumerate(cards):
                self.add_widget(CardWidget(c, g.level,
                                           pos=(cx + j * step, y),
                                           size=(cw, ch)))

    # ---- 理由列：与出牌槽位逐行对齐（仅 AI 动作有；旧档/我方留空）
    def _row_reasons(self, s, pid, top, row_h, fs):
        from ui import _label
        W = Window.width
        moves = self._recent_moves(pid)
        geo = self._slot_geo(top, row_h)
        x = int(0.6 * W) + int(0.012 * W)
        bw = int(0.2 * W) - 2 * int(0.012 * W)
        for k in range(3):
            if k >= len(moves):
                continue
            reason = moves[k][1].get("reason") or ""
            if not reason:
                continue
            _, s_top, slot_h = geo[k]
            self.add_widget(_label(reason, size=max(11, int(fs * 0.85)),
                                   color=(0.95, 0.90, 0.70, 1), halign="left",
                                   valign="middle",
                                   pos=(x, s_top - slot_h),
                                   widget_size=(bw, slot_h)))

    # ------------------------------------------------------------ 步进
    def _go_start(self):
        self.session.goto_index(0)

    def _step_forward(self):
        self.session.forward()

    def _step_back(self):
        self.session.back()

    def _play_trick(self):
        """下一轮：出牌区切到新轮（清空、编号从 1 重显），0.5s/步播到收牌点。"""
        self._stop_auto()
        self._auto_target = self.session.next_trick_target()
        if self._auto_target is None or self._auto_target <= self.session.index:
            self._auto_target = None
            return
        self.view_trick = self.session.next_play_trick(self.session.index)
        self._rebuild()
        self._auto_ev = Clock.schedule_interval(self._auto_tick, 0.5)

    def _auto_tick(self, dt):
        s = self.session
        if s is None or self._auto_target is None or s.index >= self._auto_target:
            self._stop_auto()
            return
        s.forward()
        self._rebuild()

    def _prev_trick(self):
        """上一轮：跳回上一轮起点，出牌区清空、编号从 1 重新显示该轮。"""
        self._stop_auto()
        t = self.session.prev_trick_target()
        if t is not None:
            self.session.goto_index(t)
            self.view_trick = self.session.next_play_trick(t)
        self._rebuild()

    def _nav(self, fn):
        self._stop_auto()
        self.view_trick = None
        fn()
        self._rebuild()

