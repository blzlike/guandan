"""复盘界面（阶段四 + 2026-10-14 优化）：存档清单 + 四列回放。

清单：按时间从旧到新排列；每行 = 存档名 + 删除/打开/载入三按钮。
存档命名（方案 A）：``#NNN 日期 我方打X 我方胜/负 我名次+对家名次``（空格分隔；
未完赛时不显示胜负与名次字段）。
载入 = 用存档 seed 重建第 1 局发牌态交给牌桌（比赛起点，复盘/打分可录）。

回放页四列（手牌 2/5 | 出牌 1/5 | 理由 3/10 | 按钮 1/10）× 四行（我/下家/
对家/上家），浅色分隔线；本页按钮统一宽:高=3:2。手牌区左缘显示头像+姓名+
剩牌数（样式同正常牌桌上家，头像贴屏幕边 0.2 倍头像宽，手牌底部对齐紧贴行
下沿、左贴头像），手牌按未整理手牌形式放置（同点数一列叠放，列自左向右
点值降序，5 张到顶反算牌高）；出牌区每人 2 槽位显示本轮
最近 2 次行牌（牌高 90% 槽高；牌序按 rules.display_order 统一规范：序列类
点数升序、配牌占顶替位，三带二三张在前，同点黑红梅方，与正常牌桌一致），
编号为轮内 play/pass 顺序号（每轮从 1 起，
过牌显示「不出」；下一轮/上一轮按下即切轮清空重新编号）；理由区左右紧贴
分隔线（各留 0.5 字宽）自动换行，写满槽位后以省略号结尾，每槽位右端「对
比」按钮（宽=槽位高、3:2）打开候选分项面板（AI 动作理由由正常牌局
Recorder 的 reason 字段提供，旧档留空）。
按钮三组：下一步/上一步、下一轮/上一轮（下一轮 0.5s/步自动播放至本轮收牌）、
重新开始/载入局势/返回列表（高度 0.052H→0.078H，右缘与各钮垂直中心不变）。
载入局势 = 当前回放状态克隆交给牌桌；进贡阶段
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

from cards import RANK_LABELS, build_deck, point_value, sort_key
from decision import DecisionStore, format_snapshot_summary
from replay import ReplaySession, load_json
from review import CATEGORIES, VERDICTS, VERDICT_LABELS, ReviewStore
from rules import Combo, HandType, display_order

# 可写目录（与 ui.py settings.json 同套 frozen 规则：exe 旁 / 源码目录旁）
if getattr(sys, "frozen", False):
    _HERE = os.path.dirname(sys.executable)
else:
    _HERE = os.path.dirname(os.path.abspath(__file__))
REPLAYS_DIR = os.path.join(_HERE, "replays")

RANK_NAMES = ("头游", "二游", "三游", "末游")
_CARD_BY_EID = {c.eid: c for c in build_deck()}   # eid 确定性分配，展示重建用


def _one_line(text: str) -> str:
    """多行文本 → 单行（行间以「｜」分隔；理由区按槽宽自动换行展示）。"""
    return "｜".join(ln.strip() for ln in text.splitlines() if ln.strip())


def _hand_columns(cards, level: int):
    """未整理手牌形式分列（同正常牌桌 column_layout 口径）：同点数一列
    叠放（列内自下而上黑桃/红桃/梅花/方块），列自左向右按比较点值降序
    （大王→小王→级牌→A→…→2）。"""
    cols: Dict[int, list] = {}
    for c in cards:
        cols.setdefault(c.rank, []).append(c)
    ordered = sorted(cols.keys(), key=lambda r: -point_value(r, level))
    return [sorted(cols[r], key=lambda c: sort_key(c, level)) for r in ordered]


def _action_combo(a: Dict[str, Any]):
    """由存档 play 动作重建出牌解释（新档含 ctype/crank/csubs 字段，
    供 display_order 精确放置红心配）；旧档无该字段返回 None，
    display_order 改按 classify 推定。"""
    ct = a.get("ctype")
    if not ct:
        return None
    try:
        t = HandType(ct)
    except ValueError:
        return None
    cards = tuple(_CARD_BY_EID[e] for e in a["eids"] if e in _CARD_BY_EID)
    subs = tuple((int(e), int(r)) for e, r in (a.get("csubs") or []))
    return Combo(t, int(a.get("crank", 0)), cards, subs)


def _btn_font(text: str, w: float, h: float) -> int:
    """FlatButton 文字字号（与 ui.FlatButton._place 公式一致），供普通文字
    对齐按钮字号：清单页=「打开」、回放/快照页=「下一步」（2026-10-19 规格）。"""
    est = sum(1.0 if ord(ch) > 0x2E80 else 0.55 for ch in text) or 1.0
    return max(9, int(min(h * 0.60, w * 0.94 / est)))


def _fit_reason_text(text: str, tw: int, slot_h: int, size: int) -> str:
    """理由文字按渲染宽度模拟换行，截断到槽位高度能容纳的行数，
    写满后末行以「...」收尾（Kivy 无多行省略号，此处逐字测量预截断；
    与 ui 同字体同字号，保证与真实 wrap 宽度一致）。"""
    from kivy.core.text import Label as CoreLabel
    from ui import _CJK_FONT
    core = CoreLabel(font_name=_CJK_FONT, font_size=size)
    line_h = (core.get_extents("国")[1] or size + 2) + 2
    nl_max = max(1, int((slot_h - 2) // line_h))
    tw = max(size, tw - 2)                 # 2px 余量防测量偏差导致再折行
    n, i, lines, truncated = len(text), 0, [], False
    while i < n:
        line, lw = "", 0
        while i < n:
            w = core.get_extents(text[i])[0]
            if line and lw + w > tw:
                break
            line += text[i]
            lw += w
            i += 1
        lines.append(line)
        if len(lines) >= nl_max and i < n:
            truncated = True
            break
    if truncated:
        tail = lines[-1]
        while tail and core.get_extents(tail + "...")[0] > tw:
            tail = tail[:-1]
        lines[-1] = tail + "..."
    return "\n".join(lines)


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
        self._snap_map: Dict[str, dict] = {}     # decision_id → 决策快照
        self._snap_view: Optional[dict] = None   # 候选对比面板当前快照
        self._review_open: Optional[dict] = None  # 校核表单当前快照（阶段五）
        self._review_msg = ""                    # 校核表单一次性提示
        self._review_draft = {"category": CATEGORIES[4], "verdict": "improvable",
                              "to_regression": False}
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
            if self._snap_view is not None:
                self._build_snapshot_panel(self._snap_view)
                if self._review_open is not None:
                    self._build_review_form(self._review_open)
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
        self._snap_map = {}
        self._snap_view = None
        self._review_open = None
        self._review_msg = ""
        self._msg = ""
        self._rebuild()

    def _load_snaps(self, record: Dict[str, Any]) -> Dict[str, dict]:
        """加载该存档 seed 对应的决策快照（decision_id → 快照）。

        decisions/<seed>.jsonl 缺失（旧档/中途载入续局）时返回空表，
        理由区回退显示 reason（历史记录兼容）。
        """
        seed = record.get("seed")
        if seed is None:
            return {}
        try:
            return {d["decision_id"]: d
                    for d in DecisionStore(int(seed)).load()
                    if d.get("decision_id")}
        except Exception:
            return {}

    def _open(self, path: str):
        self._stop_auto()
        self.view_trick = None
        self._snap_view = None
        self._review_open = None
        self._review_msg = ""
        try:
            rec = load_json(path)
            self.session = ReplaySession(rec)
            self._snap_map = self._load_snaps(rec)
            self._msg = ""
        except Exception as e:
            self.session = None
            self._snap_map = {}
            self._msg = f"无法打开：{e}"
        self._rebuild()

    # ---- 决策快照候选对比面板（阶段三）：理由区「对比」按钮打开
    def _open_snapshot(self, snap: dict):
        self._stop_auto()
        self._snap_view = snap
        self._rebuild()

    def _close_snapshot(self):
        self._snap_view = None
        self._rebuild()

    def _build_snapshot_panel(self, snap: dict):
        """候选对比覆盖面板（2026-10-20 用户定调试三区布局）：

        - 标题行（居中）+ value 档局面参数行；删除座位/最终两行信息；
        - 左区（贴左边缘）三表并排：控制构成（副题「基础方案」，各手
          控制值+总控制）、手牌清单（出牌前/出牌后，行=类型短名+点数串
          如 炸弹7777/三二55544，10→X、逢人配→$；旧档回退点数串/资源
          标签；均按最大牌力组合，出牌后数据源 context.after_groups，
          旧档显示无明细），出牌前表页脚「非炸手数」、出牌后表页脚
          「非炸手数/出牌后牌力」，三表下方汇总行「当前牌力 → 出牌后」；
        - 右列候选分项表（value=4 因子+胜利值 6 列，列宽按内容自适应、
          列距 1 字符、右贴快照框右沿；经典档 9 列固定比例不变）；
        - 整页字号 =「下一步」字号的 2/3；底部贴底四因子公式块，字号 =
          当前字号的 1/2；校核/关闭钮随字号。
        """
        from decision import format_candidates_table
        from ui import BLUE, FlatButton, Panel, _label
        W, H = Window.width, Window.height
        DIM = (0.10, 0.11, 0.13, 0.55)
        self.add_widget(Panel(pos=(0, 0), size=(W, H), color=DIM,
                              border=DIM, radius=0))
        pw, ph = int(0.90 * W), int(0.88 * H)
        px, py = (W - pw) // 2, (H - ph) // 2
        self.add_widget(Panel(pos=(px, py), size=(pw, ph),
                              color=(0.16, 0.17, 0.15, 0.98)))
        fs0 = _btn_font("下一步", int(0.078 * H) * 1.5, int(0.078 * H))
        fs = max(11, int(fs0 * 2 / 3))           # 整页字号 = 原字号 2/3
        lh = fs + 8                              # 文字行高（含行距）
        y = py + ph - lh - 10
        self.add_widget(_label(
            f"[b]决策快照[/b]  {snap.get('decision_id', '')}",
            size=fs, pos=(px, y),
            widget_size=(pw, lh), halign="center"))
        y -= lh + 4
        is_value = (snap.get("sid") or "") == "value"
        # 局面参数行（value 档）：压制/增益因子的局面级输入；旧档无
        # context 跳过。
        if is_value:
            ctx0 = snap.get("context") or {}
            opp_s = "/".join(f"{e:.1f}" for e in (ctx0.get("opp_ests")
                                                  or ())) or "—"
            mate_s = f"{ctx0['mate_est']:.1f}" \
                if ctx0.get("mate_est") else "—"
            acc = ctx0.get("mate_accept")
            acc_s = "" if acc is None else ("·可送" if acc else "·不可送")
            self.add_widget(_label(
                f"参数：对手手数(估) {opp_s} · 对家手数(估) {mate_s}{acc_s}",
                size=fs, color=(0.85, 0.85, 0.80, 1),
                pos=(px + int(0.03 * pw), y),
                widget_size=(int(0.9 * pw), lh), halign="left"))
            y -= lh
        # 右列分项表按 sid 切列：value 档 6 列=候选|牌力 压制 增益 终局|
        # 胜利值（基本参数在左区三表，列宽按内容自适应）；经典档 9 列
        # 固定比例不变。表头下附备注行说明各列含义（字号自适应防溢出）。
        rows = format_candidates_table(snap)
        if is_value:
            NOTE = ("▶=实际选择", "出牌后\n牌力", "压制\n对手", "队友\n配合",
                    "终结\n乘数", "越高\n越优")
        else:
            col_fx = (0.03, 0.30, 0.42, 0.50, 0.58, 0.66, 0.74, 0.82, 0.90)
            col_w = [int(0.26 * pw), int(0.11 * pw)] + [int(0.075 * pw)] * 7
            NOTE = ("▶=实际选择", "越低\n越优", "出牌\n收益", "队友\n配合",
                    "压制\n对手", "节奏\n得失", "牌力\n消耗", "被压\n风险",
                    "终结\n影响")
            tail = "总分越低越优；分项为对总分的贡献，负值=利好该动作"

        def _tw(txt, size) -> int:
            """文本宽估算（与 _btn_font 同口径：CJK 全宽、ASCII 0.55）。"""
            return sum(size if ord(ch) > 0x2E80 else int(size * 0.55)
                       for ch in str(txt))

        _rw_cache = {}

        def _rw(txt, size) -> int:
            """真实渲染宽（CoreLabel 单行实测）：desc 行含宽字母（X/J/Q/K/
            小王/大王），_tw 估算偏小会截断，改用实测（2026-10-20）。
            加粗行按上限预留余量。"""
            key = (str(txt), size)
            if key not in _rw_cache:
                from kivy.core.text import Label as CoreLabel
                from ui import _CJK_FONT
                core = CoreLabel(font_name=_CJK_FONT, font_size=size)
                _rw_cache[key] = int(core.get_extents(str(txt))[0] or 0)
            return _rw_cache[key] + int(size * 0.18) + 2

        if is_value:
            # 列宽按内容自适应（表头/数据实测宽、备注按 nfs），列距=1 个
            # 字符宽度，整表右贴快照框右沿（2026-10-20 用户定）。
            raw = []
            for ci in range(len(rows[0])):
                w = _rw(rows[0][ci], fs)
                for r in rows[1:]:
                    w = max(w, _rw(r[ci], fs))
                raw.append(w)
            nfs = min(fs, int(min(raw) * 0.45))
            ws = [max(raw[ci], max(_rw(ln, nfs)
                                   for ln in NOTE[ci].split("\n")))
                  for ci in range(len(raw))]
            gap_c = max(6, int(0.55 * fs))       # 列距=1 个字符宽度
            xr = px + pw - int(0.012 * pw)       # 右贴快照框右沿
            xs = [0] * len(ws)
            for ci in range(len(ws) - 1, -1, -1):
                xs[ci] = xr - ws[ci]
                xr = xs[ci] - gap_c
        else:
            nfs = min(fs, int(min(col_w) * 0.45))
            ws = col_w
            xs = [px + int(fx * pw) for fx in col_fx]
        nh = int(nfs * 2.3) + 4                  # 备注行高（容两行）
        rh = fs + 10
        bh = max(int(0.052 * H), int(fs * 1.7))  # 底部按钮随字号增大
        fbase = py + int(0.012 * H) + bh + 8     # 底注锚点（贴底）
        if is_value:                             # 四因子公式：字号=当前 1/2
            FL = ("胜利值 = 牌力 × 压制 × 增益 × 终局",
                  "牌力 = 出牌后控制 ÷ (出牌后手数 + 1)，每组恒 1 手",
                  "压制 = 1 + Σ 被压概率 ÷ 对手估计手数^1.5",
                  "增益 = 1 + 对家减量 ÷ (对家估计手数 + 1)",
                  "终局 = 出完×5 · 阻断×3 · 用炸×2 · 送冲×2.5 连乘")
            nff = max(9, fs // 2)
            flh = nff + 4                        # 公式行高（单行）
            bottom = fbase + len(FL) * flh + 6
        else:
            bottom = py + int(0.012 * H) + bh + lh + 6
        if is_value:                             # 左区三表并排（贴左边缘）
            ctx = snap.get("context") or {}
            groups = ctx.get("cur_groups") or []
            afters = ctx.get("after_groups")
            gt = 6                               # 表间距（列宽自适应压紧）
            ga_x = 6                             # 控制构成表 x
            HEAD_C = (0.75, 0.78, 0.72, 1)
            BODY_C = (0.95, 0.95, 0.90, 1)
            SUB_C = (0.65, 0.68, 0.62, 1)

            def _col(x_off, w_lab, head, sub, trows, foot):
                """单列小表：主/副两行表头 + (标签, 值) 行 + 页脚行；
                行数超出可视高度截断；返回下一空行 y。"""
                ty = y
                self.add_widget(_label(
                    f"[b]{head}[/b]", size=fs, color=HEAD_C,
                    pos=(px + x_off, ty - rh),
                    widget_size=(w_lab + c2, rh), halign="left"))
                self.add_widget(_label(
                    sub, size=nfs, color=SUB_C,
                    pos=(px + x_off, ty - 2 * rh),
                    widget_size=(w_lab + c2, rh), halign="left"))
                ty -= 2 * rh
                for lab, val in trows:
                    if ty - rh < bottom:
                        break
                    self.add_widget(_label(
                        lab, size=fs, color=BODY_C,
                        pos=(px + x_off, ty - rh),
                        widget_size=(w_lab, rh), halign="left"))
                    self.add_widget(_label(
                        val, size=fs, color=BODY_C,
                        pos=(px + x_off + w_lab, ty - rh),
                        widget_size=(c2, rh), halign="left"))
                    ty -= rh
                for lab, val in foot:
                    self.add_widget(_label(
                        lab, size=fs, color=HEAD_C,
                        pos=(px + x_off, ty - rh),
                        widget_size=(w_lab, rh), halign="left"))
                    self.add_widget(_label(
                        val, size=fs, color=HEAD_C,
                        pos=(px + x_off + w_lab, ty - rh),
                        widget_size=(c2, rh), halign="left"))
                    ty -= rh
                return ty

            def _hand_rows(glist):
                """手牌清单行：类型短名+点数串（desc，如 炸弹7777；旧档
                回退点数串/资源标签+配/弱/×n 标记）+ 手数值（火牌=—，
                其余恒 1，2026-10-20 用户定弱组不再加权）。"""
                out = []
                for g in glist:
                    if g.get("desc"):
                        lab = str(g["desc"])
                    elif "cards" in g:
                        lab = str(g["cards"])
                    else:
                        lab = str(g.get("label", "?"))
                        if g.get("wild_used"):
                            lab += "·配"
                        if g.get("weak"):
                            lab += "·弱"
                        if g.get("n", 1) > 1 and "x" not in lab:
                            lab += f"×{g['n']}"
                    val = "—" if g.get("fire") else "1"
                    out.append((lab, val))
                return out

            rows_a = [(str(g.get("desc") or g.get("label", "?")),
                       f"{g['ctrl']:.2f}")
                      for g in groups if g.get("ctrl", 0.0) > 0.0]
            foot_a = [("总控制", f"{ctx.get('cur_control', 0) or 0:.2f}")]
            rows_b = _hand_rows(groups)
            foot_b = [("非炸手数", f"{ctx.get('cur_hands', 0) or 0:.1f}")]
            if afters is None:                   # 旧档无出牌后明细
                rows_c, foot_c = [("（无明细）", "")], []
            else:
                rows_c = _hand_rows(afters)
                if ctx.get("finished"):
                    foot_c = [("已出完", "—"), ("非炸手数", "—"),
                              ("出牌后牌力", "—")]
                else:
                    foot_c = [("非炸手数",
                               f"{ctx.get('after_hands', 0) or 0:.1f}"),
                              ("出牌后牌力",
                               f"{ctx.get('after_power', 0) or 0:.2f}")]
            # 列宽按内容自适应（行格式=类型短名+点数串，实测宽防截断；
            # 左区总宽不越候选表最左列，越界时标签列按比例压紧）
            def _wlab(texts) -> int:
                return max((_rw(t, fs) for t in texts), default=0)
            wa = _wlab([r[0] for r in rows_a] + ["总控制"])
            wb = _wlab([r[0] for r in rows_b] + ["非炸手数"])
            wc = _wlab([r[0] for r in rows_c] + [f[0] for f in foot_c])
            vals = [r[1] for r in rows_a + rows_b + rows_c] \
                + [v for _, v in foot_a + foot_b + foot_c]
            c2 = max(30, max((_rw(v, fs) for v in vals), default=0))
            budget = xs[0] - px - ga_x - 2 * gt - 6   # 左区可用宽（留缝）
            if wa + wb + wc + 3 * c2 > budget:
                k = max(0.0, budget - 3 * c2) / (wa + wb + wc)
                wa, wb, wc = (max(36, int(w * k)) for w in (wa, wb, wc))
            gb_x = ga_x + wa + c2 + gt            # 手牌清单（出牌前）x
            gc_x = gb_x + wb + c2 + gt            # 手牌清单（出牌后）x
            ys = [_col(ga_x, wa, "控制构成", "基础方案", rows_a, foot_a)]
            ys.append(_col(gb_x, wb, "手牌清单", "（出牌前）",
                           rows_b, foot_b))
            ys.append(_col(gc_x, wc, "手牌清单", "（出牌后）",
                           rows_c, foot_c))
            if afters is None:
                tail_s = "无出牌后明细"
            elif ctx.get("finished"):
                tail_s = "一手出完"
            else:
                tail_s = f"{ctx.get('after_power') or 0:.2f}"
            sy = min(ys) - rh
            if sy >= bottom:                     # 三表下方汇总行
                self.add_widget(_label(
                    f"[b]当前牌力[/b] {ctx.get('cur_power', 0) or 0:.2f}"
                    f" → 出牌后 {tail_s}", size=fs, color=HEAD_C,
                    pos=(px + ga_x, sy),
                    widget_size=(gc_x - ga_x + wc + c2, rh),
                    halign="left"))
        for ri, row in enumerate(rows):
            if y - (rh + (nh if ri == 0 else 0)) < bottom:
                break
            sel = row[0].startswith("▶")
            color = (1.0, 0.90, 0.45, 1) if sel else \
                ((0.75, 0.78, 0.72, 1) if ri == 0 else (0.95, 0.95, 0.90, 1))
            for ci, cell in enumerate(row):
                self.add_widget(_label(
                    cell, size=fs, color=color,
                    bold=sel or ri == 0,
                    pos=(xs[ci], y - rh),
                    widget_size=(ws[ci], rh), halign="left"))
            if ri == 0:                          # 表头下方备注：各列含义
                for ci, txt in enumerate(NOTE):
                    self.add_widget(_label(
                        txt, size=nfs, color=(0.65, 0.68, 0.62, 1),
                        pos=(xs[ci], y - rh - nh),
                        widget_size=(ws[ci], nh), halign="left"))
                y -= rh + nh                     # 表头行占位=表头+备注
                continue
            y -= rh
        if is_value:                             # 底注：四因子公式（贴底）
            for i, txt in enumerate(FL):
                self.add_widget(_label(
                    txt, size=nff, color=(0.65, 0.68, 0.62, 1),
                    pos=(px + int(0.03 * pw),
                         fbase + (len(FL) - 1 - i) * flh),
                    widget_size=(int(0.94 * pw), flh), halign="left"))
        else:
            self.add_widget(_label(
                tail,
                size=fs, color=(0.65, 0.68, 0.62, 1),
                pos=(px + int(0.03 * pw), fbase),
                widget_size=(int(0.94 * pw), lh), halign="left"))
        # 左下「校核」（阶段五人工校核入口）+ 右下「关闭」
        self.add_widget(FlatButton("校核", (0.72, 0.45, 0.12, 1),
                                   (lambda sn=snap: self._open_review(sn)),
                                   pos=(px + int(0.03 * pw),
                                        py + int(0.012 * H)),
                                   size=(int(bh * 1.5), bh), font_size=fs))
        self.add_widget(FlatButton("关闭", BLUE, self._close_snapshot,
                                   pos=(px + pw - int(0.03 * pw)
                                        - int(bh * 1.5),
                                        py + int(0.012 * H)),
                                   size=(int(bh * 1.5), bh), font_size=fs))

    # ---- 人工校核标注（阶段五）：类别/结论/建议/适用条件/是否入回归集
    def _open_review(self, snap: dict):
        self._stop_auto()
        self._review_open = snap
        self._review_msg = ""
        self._review_draft = {"category": CATEGORIES[4],
                              "verdict": VERDICTS[1],
                              "to_regression": False}
        self._rebuild()

    def _close_review(self):
        self._review_open = None
        self._review_msg = ""
        self._rebuild()

    def _save_review(self):
        snap = self._review_open or {}
        try:
            rec = ReviewStore().append(
                decision_id=str(snap.get("decision_id", "")),
                category=self._review_draft["category"],
                verdict=self._review_draft["verdict"],
                note=self._rv_note.text.strip(),
                conditions=self._rv_cond.text.strip(),
                to_regression=self._review_draft["to_regression"])
            self._review_msg = f"已保存 {rec['review_id']}（待处理）"
            if rec["to_regression"]:
                self._review_msg += "，待落回归用例后 --sync 回写测试状态"
        except Exception as e:
            self._review_msg = f"保存失败：{e}"
        self._rebuild()

    def _build_review_form(self, snap: dict):
        from kivy.uix.textinput import TextInput
        from ui import BLUE, FlatButton, Panel, _CJK_FONT, _label

        class _Toggle(FlatButton):
            """选中态高亮的开关按钮（不重建组件树，保持输入框文字）。"""

            def __init__(self, text, on_press, selected=False, **kw):
                super().__init__(text, (0.24, 0.30, 0.24, 1), on_press, **kw)
                self.set_selected(selected)

            def set_selected(self, val: bool):
                c = (1.0, 0.88, 0.40, 1) if val else self.base_color
                self._top_c.rgba = c
                self._bd_c.rgba = FlatButton._shade(c, 0.60)
                self._label.color = ((0.15, 0.13, 0.05, 1) if val
                                     else self.text_color + (1,))

        GREEN = (0.20, 0.60, 0.30, 1)
        W, H = Window.width, Window.height
        DIM = (0.10, 0.11, 0.13, 0.55)
        self.add_widget(Panel(pos=(0, 0), size=(W, H), color=DIM,
                              border=DIM, radius=0))
        pw, ph = int(0.58 * W), int(0.86 * H)
        px, py = (W - pw) // 2, (H - ph) // 2
        self.add_widget(Panel(pos=(px, py), size=(pw, ph),
                              color=(0.16, 0.17, 0.15, 0.98)))
        fs = max(11, int(0.022 * H))
        mx = px + int(0.03 * pw)
        cw = int(0.94 * pw)
        did = str(snap.get("decision_id", ""))
        try:
            n_have = len(ReviewStore().by_decision(did))
        except Exception:
            n_have = 0
        y = py + ph - int(0.040 * H)
        self.add_widget(_label("[b]校核标注[/b]", size=int(fs * 1.2),
                               pos=(mx, y), widget_size=(cw, int(0.035 * H)),
                               halign="left"))
        y -= int(0.030 * H)
        self.add_widget(_label(
            f"{did}　已有 {n_have} 条校核", size=int(fs * 0.85),
            color=(0.80, 0.80, 0.74, 1), pos=(mx, y),
            widget_size=(cw, int(0.026 * H)), halign="left"))
        y -= int(0.034 * H)
        draft = self._review_draft

        def section(text):
            nonlocal y
            self.add_widget(_label(text, size=int(fs * 0.92),
                                   color=(0.72, 0.76, 0.68, 1),
                                   pos=(mx, y), widget_size=(cw, int(0.024 * H)),
                                   halign="left"))
            y -= int(0.030 * H)

        def toggles(items, n_row, h):
            """n_row 行开关组；回调在创建后按组绑定（避免前向引用）。"""
            nonlocal y
            gap = int(0.006 * W)
            per = (len(items) + n_row - 1) // n_row
            bw = (cw - (per - 1) * gap) // per
            btns: List[tuple] = []
            for i, it in enumerate(items):
                r, c = divmod(i, per)
                bx = mx + c * (bw + gap)
                by = y - h - r * (h + int(0.008 * H))
                btn = _Toggle(it, (lambda: None), pos=(bx, by),
                              size=(bw, h))
                btns.append((btn, it))
                self.add_widget(btn)
            y -= n_row * h + (n_row - 1) * int(0.008 * H) + int(0.014 * H)
            return btns

        section("类别（记牌/推理/局势/团队/候选评价/炸弹/拆牌/规则/信息不足）")
        cat_btns = toggles(list(CATEGORIES), 2, int(0.042 * H))

        def pick_cat(it):
            draft["category"] = it
            for b, it2 in cat_btns:
                b.set_selected(it2 == it)

        for b, it in cat_btns:
            b.on_press = (lambda t=it: pick_cat(t))
        section("结论")
        ver_btns = toggles([VERDICT_LABELS[v] for v in VERDICTS], 1,
                           int(0.042 * H))

        def pick_verdict(lab):
            draft["verdict"] = next(v for v, l in VERDICT_LABELS.items()
                                    if l == lab)
            for b, l2 in ver_btns:
                b.set_selected(l2 == lab)

        for b, lab in ver_btns:
            b.set_selected(lab == VERDICT_LABELS[draft["verdict"]])
            b.on_press = (lambda t=lab: pick_verdict(t))
        section("建议（此处应如何处理、依据是什么）")
        nh = int(0.115 * H)
        self._rv_note = TextInput(text="", multiline=True, font_name=_CJK_FONT,
                                  font_size=fs, size_hint=(None, None),
                                  pos=(mx, y - nh), size=(cw, nh),
                                  foreground_color=(1, 1, 1, 1),
                                  background_color=(0.10, 0.11, 0.10, 1))
        self.add_widget(self._rv_note)
        y -= nh + int(0.016 * H)
        section("适用条件（该建议在什么局面下成立）")
        ch = int(0.042 * H)
        self._rv_cond = TextInput(text="", multiline=False,
                                  font_name=_CJK_FONT, font_size=fs,
                                  size_hint=(None, None), pos=(mx, y - ch),
                                  size=(cw, ch),
                                  foreground_color=(1, 1, 1, 1),
                                  background_color=(0.10, 0.11, 0.10, 1))
        self.add_widget(self._rv_cond)
        y -= ch + int(0.018 * H)
        # 纳入回归集开关
        def pick_reg():
            draft["to_regression"] = not draft["to_regression"]
            reg_btn.set_selected(draft["to_regression"])
        reg_btn = _Toggle("纳入回归集", (lambda: pick_reg()),
                          selected=draft["to_regression"],
                          pos=(mx, y - int(0.042 * H)),
                          size=(int(0.24 * pw), int(0.042 * H)))
        self.add_widget(reg_btn)
        self.add_widget(_label("勾选后须落 fixed_cases 用例并 --sync 通过，"
                               "才可置「已采纳」", size=int(fs * 0.80),
                               color=(0.65, 0.68, 0.62, 1),
                               pos=(mx + int(0.26 * pw), y - int(0.036 * H)),
                               widget_size=(int(0.66 * pw), int(0.030 * H)),
                               halign="left"))
        y -= int(0.042 * H) + int(0.020 * H)
        # 底部：保存 / 返回 / 状态提示
        bh = int(0.048 * H)
        bw2 = int(bh * 1.5)
        self.add_widget(FlatButton("保存", GREEN, self._save_review,
                                   pos=(px + pw - int(0.03 * pw)
                                        - 2 * bw2 - int(0.010 * W),
                                        py + int(0.012 * H)),
                                   size=(bw2, bh)))
        self.add_widget(FlatButton("返回", BLUE, self._close_review,
                                   pos=(px + pw - int(0.03 * pw) - bw2,
                                        py + int(0.012 * H)),
                                   size=(bw2, bh)))
        if self._review_msg:
            self.add_widget(_label(self._review_msg, size=int(fs * 0.88),
                                   color=(0.55, 0.95, 0.55, 1),
                                   pos=(mx, py + int(0.020 * H)),
                                   widget_size=(int(0.55 * pw), int(0.034 * H)),
                                   halign="left"))

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
        self.add_widget(FlatButton("返回主界面", BLUE,
                                   lambda: self.app.goto("menu"),
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
        # 列表文字字号 = 行内「打开」按钮文字字号（2026-10-19 用户规格）；
        # 按钮实际宽 = BoxLayout 按行内 4 子件（名称+3 钮）size_hint 分配
        inner = int(0.90 * W) - 2 * int(0.010 * W)
        frow = _btn_font("打开", int(0.13 * (inner - 3 * int(0.006 * W))), rh)
        for it in items:
            row = BoxLayout(orientation="horizontal", spacing=int(0.006 * W),
                            size_hint=(1, None), size=(0, rh))
            lb = _label(it["name"], size=frow,
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
        # 回放页各区文字（头像旁/出牌区/理由区/对比按钮）统一=「下一步」字号
        # （2026-10-19 用户规格）；fs 仅保留给右上局数指示
        fnav = _btn_font("下一步", int(0.078 * H) * 1.5, int(0.078 * H))
        # 背景 + 浅色分隔线（四列三行内缝）
        from ui import Panel, WOOD
        self.add_widget(Panel(pos=(0, 0), size=(W, H), color=WOOD,
                              border=WOOD, radius=0))
        LINE = (0.93, 0.90, 0.82, 0.35)
        for fx in (0.4, 0.6, 0.9):
            self.add_widget(Panel(pos=(int(fx * W), 0), size=(1, H),
                                  color=LINE, border=LINE, radius=0))
        for fy in (0.25, 0.5, 0.75):
            self.add_widget(Panel(pos=(0, int(fy * H)), size=(W, 1),
                                  color=LINE, border=LINE, radius=0))
        # 四行：我 / 下家 / 对家 / 上家（自上而下）
        row_h = int(H / 4)
        for k, pid in enumerate((0, 1, 2, 3)):
            top = H - k * row_h          # 本行顶边（Kivy y 自下而上）
            self._row_hand(g, pid, top, row_h, fnav)
            self._row_moves(s, g, pid, top, row_h, fnav)
            self._row_reasons(s, pid, top, row_h, fnav)
        # 按钮列（理由区扩至 0.3W 后按钮区缩减为 0.9W–1.0W）：全部按钮
        # 宽:高=3:2；高度 0.052H→0.078H（+50%），右缘与各钮垂直中心不变
        bh = int(0.078 * H)
        bw = int(bh * 1.5)
        x0 = int(0.8 * W) + int(0.2 * W) - int(0.012 * W) - bw
        y = H - int(0.015 * H) - int(0.04 * H)
        self.add_widget(_label(
            f"[b]第{g.round_number}局 打{RANK_LABELS[g.level]}\n"
            f"{s.index}/{s.total()}[/b]",
            size=max(10, int(fs * 0.72)), pos=(x0, y),
            widget_size=(bw, int(0.04 * H))))
        y -= int(0.02 * H)
        groups = [("下一步", GREEN, lambda: self._nav(self._step_forward)),
                  ("上一步", BLUE, lambda: self._nav(self._step_back)),
                  ("下一轮", GREEN, self._play_trick),
                  ("上一轮", BLUE, self._prev_trick),
                  ("重新开始", BLUE, lambda: self._nav(self._go_start)),
                  ("载入局势", GREEN, self._load_state),
                  ("返回列表", BLUE, self._back_to_list)]
        # 三组拉开：以旧几何（0.052H）求各钮垂直中心，新钮按 +50% 高度
        # 居中展开 → 每钮右缘与垂直中心均保持原位
        bh_old = int(0.052 * H)
        y_top_old = y - int(0.02 * H) - bh_old
        y_bot = int(0.125 * H)
        unit = max(int(0.006 * H),
                   (y_top_old - y_bot - 6 * bh_old) / 8.0)
        yy = y_top_old
        for i, (t, c, cb) in enumerate(groups):
            cy = yy + bh_old / 2
            self.add_widget(FlatButton(t, c, cb, pos=(x0, cy - bh / 2),
                                       size=(bw, bh)))
            yy -= bh_old + (2 * unit if i in (1, 3) else unit)

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
        # 5 张到顶反算：组内叠放 0.24ch，列高 = ch + 4×0.24ch = 1.96ch ≈ row_h
        # （超 5 张的组允许向上突破，同正常牌桌 7 炸约定）
        ch = int(row_h / 1.96)
        # 同正常模式「我的手牌」：底部对齐紧贴行下沿，自左向右牌力减小
        self._draw_hand_h(g, pid, cx + side // 2 + gap,
                          int(0.4 * W) - 4, top - row_h, ch, up=True)

    def _draw_hand_h(self, g, pid, x_left, x_right, y_base, ch, up):
        """横排手牌（未整理手牌形式）：从左到右牌力减小，同点数叠放；
        总宽超区压缩列距（下限 0.30cw）；组内偏移 0.24ch，
        k=0 最后绘制整张可见。up=False 顶对齐向下叠（复盘行内用）。"""
        from ui import CardWidget
        groups = _hand_columns(g.hands[pid], g.level)
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

    # ---- 出牌列：本轮最近 2 次行牌（编号=轮内顺序号；过牌显示「不出」）
    def _display_trick(self) -> Optional[int]:
        """当前出牌区应显示的轮次。

        view_trick 由「下一轮/上一轮」显式设定（按下即切到新轮→清空重
        新从 1 编号）；手动步进等场景为 None，自动取最后已套用行牌之轮。
        """
        if self.view_trick is not None:
            return self.view_trick
        return self.session._trick_of_applied(self.session.index)

    def _recent_moves(self, pid: int) -> List[tuple]:
        """返回 [(动作 index, 动作 dict), ...]（时间序，最多 2 条）。"""
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
        return out[-2:]

    def _slot_geo(self, top, row_h):
        """2 槽位几何：[(出牌列左缘, 槽顶 y, 槽高), ...] 自上而下（旧→新）。"""
        W = Window.width
        slot_h = int(row_h / 2)
        x1 = int(0.4 * W) + 2
        return [(x1, top - k * slot_h, slot_h) for k in range(2)]

    def _row_moves(self, s, g, pid, top, row_h, fs):
        from ui import CardWidget, _label
        W = Window.width
        moves = self._recent_moves(pid)
        geo = self._slot_geo(top, row_h)
        for k in range(2):
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
            cards = display_order(cards, g.level, _action_combo(a))
            ch = int(slot_h * 0.9)   # 牌高尽量占满槽位高（90%）
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

    # ---- 理由列：与出牌槽位逐行对齐（文字左右紧贴理由区分隔线、各留
    #      0.5 字宽；限宽自动换行，写满槽位后以省略号结尾）；槽位右端
    #      「对比」按钮（宽=槽位高、3:2，文字=「下一步」字号）打开候选
    #      分项面板；无 did 的旧档回退显示 reason，历史记录兼容
    def _row_reasons(self, s, pid, top, row_h, fs):
        from ui import FlatButton, _label
        W = Window.width
        moves = self._recent_moves(pid)
        geo = self._slot_geo(top, row_h)
        for k in range(2):
            if k >= len(moves):
                continue
            a = moves[k][1]
            snap = self._snap_map.get(a.get("did")) if a.get("did") else None
            _, s_top, slot_h = geo[k]
            btn_w = slot_h              # 对比按钮宽=槽位高，3:2（本页统一比例）
            btn_h = int(slot_h / 1.5)
            size = fs                   # 文字统一=「下一步」字号（2026-10-19）
            if snap is not None:
                text = _one_line(format_snapshot_summary(snap))
            else:
                text = _one_line(a.get("reason") or "")
                if not text:
                    continue
            # 文字区：左右各留 0.5 字宽紧贴 0.6W/0.9W 分隔线；有对比按钮时
            # 文字右界让开按钮（再留 0.5 字宽）
            gap = size // 2
            x = int(0.6 * W) + gap
            right = int(0.9 * W) - gap
            has_btn = snap is not None and bool(snap.get("candidates"))
            if has_btn:
                right -= btn_w + gap
            tw = right - x
            lb = _label(_fit_reason_text(text, tw, slot_h, size),
                        size=size, color=(0.95, 0.90, 0.70, 1),
                        halign="left", valign="middle",
                        pos=(x, s_top - slot_h),
                        widget_size=(tw, slot_h))
            lb.text_size = (tw, slot_h)    # 限宽换行（预截断保证不溢出槽位）
            self.add_widget(lb)
            if has_btn:
                self.add_widget(FlatButton(
                    "对比", (0.30, 0.38, 0.30, 1),
                    (lambda sn=snap: self._open_snapshot(sn)),
                    pos=(right + gap, s_top - slot_h
                         + (slot_h - btn_h) // 2),
                    size=(btn_w, btn_h), font_size=size))

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

