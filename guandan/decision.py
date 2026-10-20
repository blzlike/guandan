"""AI 决策快照（阶段二，任务书附录 B.1）。

每次 AI 出牌/过牌决策生成一条 DecisionSnapshot，定位键
``(seed, round, action_index)`` 与 Recorder.actions 一一对应：
``load_json(record) → ReplaySession.goto_index(action_index)`` 即可
精确还原该决策的**前**状态（动作套用前 game 即决策时刻）。

- 快照存储：``decisions/<seed>.jsonl``（一比赛一文件，逐决策一行追加）；
- record.actions 仅追加可选键 ``"did": "<decision_id>"``（向后兼容，
  同 reason 先例），复盘 UI 据 did 加载快照展示；
- 中途「载入局势」续局无快照属预期（与 Recorder 同口径，见任务书 A.5）。

引用边界（任务书 B.4）：本模块属 AI 侧，仅可被 AI 侧与复盘/牌桌 UI 引用；
observer/advisor 严禁 import 本模块（tests/test_decision.py 源码检查固化）。

DecisionLogger 随 Recorder 在比赛起点挂载：作为 listener 逐事件维护
(trick, seq) 计数（口径与 ReplaySession._analyze 严格一致），决策时刻由
ui._ai_step 调用 ``capture`` 采集快照并落盘。
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import ai
import inference
import strategy
from ai import AIProfile
from cards import BIG_JOKER, RANK_LABELS, SMALL_JOKER

DATA_VERSION = 1
STRATEGY_VERSION = "v1.3.0"     # ai.py V1.2 特性集 + 阶段六分级档（Budget）

# sid → 生效特性开关（快照记录用；pimc 非关键时刻沿用经典·高；
# 阶段六分级档：novice 无特性 / intermediate 仅 urgent_top / advanced 全特性）
_SID_FEATURES = {
    "classic_high": ai.HIGH_FEATURES,
    "pimc": ai.HIGH_FEATURES,
    "novice": frozenset(),
    "intermediate": frozenset({ai.FEATURE_URGENT_TOP}),
    "advanced": ai.HIGH_FEATURES,
    "value": frozenset(),    # 夯级档：胜利值公式独立，无 V1.2 特性
}

# 可写目录（与 replay_ui 同套 frozen 规则：exe 旁 / 源码目录）
if getattr(sys, "frozen", False):
    _HERE = os.path.dirname(sys.executable)
else:
    _HERE = os.path.dirname(os.path.abspath(__file__))
DECISIONS_DIR = os.path.join(_HERE, "decisions")


def features_of(sid: str) -> List[str]:
    """sid 当前生效的 V1.2 特性开关（排序后列表）。"""
    return sorted(_SID_FEATURES.get(sid) or ())


def make_decision_id(seed: int, round_number: int, action_index: int) -> str:
    return f"seed={seed}|r={round_number}|a={action_index}"


# ---------------------------------------------------------------- 序列化
def _public_dict(game) -> Dict[str, Any]:
    """该 AI 合法掌握的公开信息（B.1 public）。"""
    lp = None
    if game.last_play is not None:
        owner, combo = game.last_play
        lp = {"owner": owner, "type": combo.type.value, "rank": combo.rank,
              "size": len(combo.cards), "eids": sorted(c.eid for c in combo.cards)}
    return {
        "played_eids": [c.eid for c in game.played_cards],
        "hand_counts": {str(p): game.hand_count(p) for p in range(4)},
        "last_play": lp,
        "finish_order": list(game.finish_order),
        "levels": {str(k): v for k, v in game.levels.items()},
        "playing_team": game.playing_team,
    }


def _memory_dict(game, pid: int) -> Dict[str, Any]:
    """该 AI 自己算的记牌结果（CardMemory 序列化，B.1 memory）。"""
    mem = ai.CardMemory(game, pid)
    level = game.level
    mine_wild = sum(1 for c in game.hands[pid] if c.is_wild(level))
    played_wild = sum(1 for c in game.played_cards if c.is_wild(level))
    wild_unseen = 2 - mine_wild - played_wild      # 逢人配全副 2 张
    return {
        "unseen": {str(r): n for r, n in sorted(mem.unseen.items()) if n > 0},
        "core": {"big_joker": mem.unseen.get(BIG_JOKER, 0),
                 "small_joker": mem.unseen.get(SMALL_JOKER, 0),
                 "level": mem.unseen.get(level, 0) - wild_unseen,
                 "wild": wild_unseen},
    }


# ---------------------------------------------------------------- 快照结构
@dataclass
class DecisionSnapshot:
    """AI 决策快照（B.1 schema；predictions/team_task/candidates 阶段三四填充）。"""
    decision_id: str
    seed: int
    round: int
    action_index: int
    trick: int
    seq: int
    pid: int
    sid: str
    profile: str
    features: List[str]
    strategy_version: str
    phase: str
    turn_pid: int
    hand: List[int]
    public: Dict[str, Any]
    memory: Dict[str, Any]
    predictions: Dict[str, Any] = field(
        default_factory=lambda: {"bombs": [], "patterns": [],
                                 "known_facts": [], "insufficient": []})
    team_task: Optional[Dict[str, Any]] = None
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    final: Dict[str, Any] = field(default_factory=dict)
    cost: Dict[str, Any] = field(default_factory=dict)
    context: Optional[Dict[str, Any]] = None   # 局面级参数（value 档 4 因子输入）

    def to_dict(self) -> Dict[str, Any]:
        return {
            "data_version": DATA_VERSION,
            "decision_id": self.decision_id,
            "seed": self.seed, "round": self.round,
            "action_index": self.action_index,
            "trick": self.trick, "seq": self.seq,
            "pid": self.pid, "sid": self.sid, "profile": self.profile,
            "features": list(self.features),
            "strategy_version": self.strategy_version,
            "phase": self.phase, "turn_pid": self.turn_pid,
            "hand": list(self.hand),
            "public": self.public, "memory": self.memory,
            "predictions": self.predictions, "team_task": self.team_task,
            "candidates": self.candidates,
            "final": self.final, "cost": self.cost,
            "context": self.context,
        }


def build_decision_snapshot(seed: int, game, pid: int, sid: str, *,
                            profile: Optional[AIProfile] = None,
                            features: Optional[List[str]] = None,
                            kind: str = "pass",
                            cards: Optional[list] = None,
                            reason: str = "",
                            action_index: int = 0,
                            trick: int = 0, seq: int = 0,
                            cost_ms: float = 0.0,
                            candidates: Optional[List[Dict[str, Any]]] = None,
                            n_candidates: int = 0,
                            search_depth: int = 0,
                            n_rollouts: int = 0,
                            team_task: Optional[Dict[str, Any]] = None,
                            context: Optional[Dict[str, Any]] = None
                            ) -> DecisionSnapshot:
    """在决策时刻（动作套用前）构建快照。game 须处于该 pid 行牌前状态。

    ``candidates`` 为 ai.choose_action_detail 产出的候选明细（B.1 键位，
    含分项 score_parts 与 selected 标记）；final.score_total 取所选候选
    总分（无候选明细时为 None，pimc/旧路径兼容）；
    ``n_candidates`` 为实际参与评分的候选总数（缺省回退 len(candidates)）；
    ``search_depth``/``n_rollouts`` 为档位计算预算（阶段六：经 detail.cost
    传入，残局搜索触发阈值 / PIMC 推演数，经典路径缺省 0）。
    ``predictions`` 由 inference 无状态纯函数现算（阶段四：炸弹/牌型预测
    + 行为信号事实，可解释、符合信息约束）；``team_task`` 缺省由
    strategy.judge_role 实时判定动态角色（主攻/助攻/防守/救援/终局）。
    """
    cands = list(candidates) if candidates else []
    selected_total = next((c["score_total"] for c in cands if c.get("selected")),
                          None)
    try:
        predictions = inference.build_predictions(game, pid)
    except Exception:                       # 推断异常不阻断决策与落盘
        predictions = {"bombs": [], "patterns": [],
                       "known_facts": [], "insufficient": []}
    if team_task is None:
        try:
            team_task = strategy.judge_role(game, pid)
        except Exception:                   # 角色判定异常不影响出牌
            team_task = None
    return DecisionSnapshot(
        decision_id=make_decision_id(seed, game.round_number, action_index),
        seed=seed, round=game.round_number, action_index=action_index,
        trick=trick, seq=seq, pid=pid, sid=sid,
        profile=getattr(profile, "name", "balanced"),
        features=features if features is not None else features_of(sid),
        strategy_version=STRATEGY_VERSION,
        phase=game.phase, turn_pid=game.turn,
        hand=sorted(c.eid for c in game.hands[pid]),
        public=_public_dict(game), memory=_memory_dict(game, pid),
        predictions=predictions, team_task=team_task,
        final={"kind": kind,
               "eids": sorted(c.eid for c in cards) if kind == "play" and cards else None,
               "reason": reason, "score_total": selected_total},
        cost={"ms": round(cost_ms, 2),
              "n_candidates": n_candidates if n_candidates else len(cands),
              "search_depth": search_depth, "n_rollouts": n_rollouts},
        candidates=cands,
        context=context,
    )


def format_snapshot_summary(snap: Dict[str, Any]) -> str:
    """快照 → 复盘理由区紧凑摘要（多行文本）。"""
    fin = snap.get("final") or {}
    mem = snap.get("memory") or {}
    core = mem.get("core") or {}
    unseen = mem.get("unseen") or {}
    parts = []
    jokers = int(core.get("big_joker") or 0) + int(core.get("small_joker") or 0)
    if jokers:
        parts.append(f"王{jokers}")
    if core.get("level"):
        parts.append(f"级{core['level']}")
    if core.get("wild"):
        parts.append(f"配{core['wild']}")
    for r in (14, 13, 12):
        n = unseen.get(str(r)) or 0
        if n:
            parts.append(f"{RANK_LABELS[r]}{n}")
    lines = [fin.get("reason") or "（无理由）"]
    margin = _best_alternative_margin(snap)
    if margin is not None:
        lines.append(f"候选{len(snap.get('candidates') or [])}个"
                     f" · 较次优 +{margin:.1f}")
    # 阶段四：动态角色 + 推测摘要（炸弹/牌型预测，概率降序首条）
    tt = snap.get("team_task") or {}
    pred = snap.get("predictions") or {}
    bits = []
    if tt.get("role"):
        bits.append(str(tt["role"]))
    bombs = pred.get("bombs") or []
    if bombs:
        b = bombs[0]
        bits.append(f"炸{RANK_LABELS.get(b.get('rank'), b.get('rank'))}"
                    f" {b.get('prob', 0) * 100:.0f}%")
    pats = pred.get("patterns") or []
    if pats:
        p = pats[0]
        bits.append(f"{p.get('pattern', '?')}{p.get('prob', 0) * 100:.0f}%")
    if len(bits) > 1 or (bits and not tt.get("role")):
        lines.append("推测 " + "，".join(bits))
    elif bits:
        lines.append("角色 " + bits[0])
    if parts:
        lines.append("未见 " + " ".join(parts))
    tail = f"手牌{len(snap.get('hand') or [])}张"
    ms = (snap.get("cost") or {}).get("ms")
    if ms is not None:
        tail += f" · {ms}ms"
    lines.append(tail)
    return "\n".join(lines)


def _best_alternative_margin(snap: Dict[str, Any]) -> Optional[float]:
    """最终动作相对最佳备选的总分优势（越大=取舍依据越充分）。

    total 越小越优：margin = 次优 score_total − 所选 score_total；
    候选缺失或无备选时返回 None。
    """
    cands = snap.get("candidates") or []
    if len(cands) < 2:
        return None
    sel = next((c for c in cands if c.get("selected")), None)
    if sel is None:
        return None
    others = [c["score_total"] for c in cands if c is not sel]
    if not others:
        return None
    return min(others) - sel["score_total"]


def format_candidates_table(snap: Dict[str, Any]) -> List[List[str]]:
    """快照候选对比 → 文本表格（复盘「候选对比」面板数据源，纯函数）。

    返回行列表（首行为表头），按快照 sid 切列：

    - 经典档（默认）：候选 | 总分 | 七分项（B.5 中文短标 走牌/协同/压制/
      牌权/成本/风险/终局）。总分越低越优，各分项为对总分的贡献
      （负值=利好）；risk 阶段四实装，经典档系数 0（Weights 启用后为
      inference 被压风险估计）。
    - value 档（夯级）：候选 | 四因子（牌力/压制/增益/终局）| 胜利值
      （胜利值越高越优，四因子连乘；牌力=出牌后牌力=控制÷(非炸手数+1)）。
      基本参数（当前牌力/控制构成/手数清单）在快照面板左列两表
      （数据源 snap.context.cur_groups，2026-10-20 用户定调试布局）。
    """
    if (snap.get("sid") or "") == "value":
        return _format_value_table(snap)
    head = ["候选", "总分", "走牌", "协同", "压制", "牌权", "成本", "风险",
            "终局"]
    rows = [head]
    for c in snap.get("candidates") or []:
        if c.get("kind") == "pass":
            desc = "过牌"
        else:
            rank = c.get("rank")
            label = {17: "大王", 16: "小王", 15: "级牌"}.get(rank) \
                or RANK_LABELS.get(rank, "?")
            desc = f"{c.get('type') or '?'}{label}"
            if len(c.get("eids") or []) > 1:
                desc += f"×{len(c['eids'])}"
        if c.get("selected"):
            desc = "▶" + desc
        p = c.get("score_parts") or {}
        row = [desc, f"{c.get('score_total', 0):.1f}"]
        row += [f"{p.get(k, 0) or 0:.1f}" for k in ai.SCORE_PARTS]
        rows.append(row)
    return rows


_VALUE_HEAD = ["候选", "牌力", "压制", "增益", "终局", "胜利值"]


def _format_value_table(snap: Dict[str, Any]) -> List[List[str]]:
    """value 档候选行：四因子 + 胜利值（当前牌力/控制构成/手数清单在
    面板左列两表，不经本表）。候选首列优先用采集时生成的 desc
    （类型短名+点数串，如 炸弹7777 / 三二55544），旧档快照无 desc 时
    回退旧格式（类型+关键点，多张带 ×n 后缀）。"""
    rows = [list(_VALUE_HEAD)]
    for c in snap.get("candidates") or []:
        if c.get("kind") == "pass":
            desc = "过牌"
        elif c.get("desc"):
            desc = str(c["desc"])
        else:
            rank = c.get("rank")
            label = {17: "大王", 16: "小王", 15: "级牌"}.get(rank) \
                or RANK_LABELS.get(rank, "?")
            desc = f"{c.get('type') or '?'}{label}"
            if len(c.get("eids") or []) > 1:
                desc += f"×{len(c['eids'])}"
        if c.get("selected"):
            desc = "▶" + desc
        p = c.get("score_parts") or {}
        rows.append([desc,
                     f"{p.get('power', 0) or 0:.2f}",
                     f"{p.get('pressure', 0) or 0:.2f}",
                     f"{p.get('partner', 0) or 0:.2f}",
                     f"{p.get('endgame', 0) or 0:.2f}",
                     f"{c.get('score_total', 0) or 0:.2f}"])
    return rows


# ---------------------------------------------------------------- 存储
class DecisionStore:
    """决策快照 JSONL 存储：``<directory>/<seed>.jsonl`` 追加/读取/定位。"""

    def __init__(self, seed: int, directory: Optional[str] = None) -> None:
        self.seed = seed
        self.directory = directory or DECISIONS_DIR

    @property
    def path(self) -> str:
        return os.path.join(self.directory, f"{self.seed}.jsonl")

    def append(self, snap: Dict[str, Any]) -> None:
        os.makedirs(self.directory, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(snap, ensure_ascii=False) + "\n")

    def load(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.path):
            return []
        out: List[Dict[str, Any]] = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def find(self, decision_id: str) -> Optional[Dict[str, Any]]:
        for d in self.load():
            if d.get("decision_id") == decision_id:
                return d
        return None


class DecisionLogger:
    """比赛级决策记录器：比赛起点挂载（与 Recorder 同口径）。

    listener 逐事件维护 (trick, seq)，口径与 ReplaySession._analyze 严格
    一致（round 1 无 round_start 录入，构造初值即其 round_start 后状态）；
    ``capture`` 由 ui._ai_step 在动作套用前调用，采集并落盘快照，任何
    存储异常不外抛（快照失败不影响牌局）。
    """

    def __init__(self, seed: int, directory: Optional[str] = None) -> None:
        self.seed = seed
        self.store = DecisionStore(seed, directory)
        self.trick = 0
        self.seq = 0
        self._trick_open = False      # 本轮已出现领出 play（等价 last_play 非空）
        self._fn = None

    def attach(self, game) -> bool:
        """挂载到比赛起点；非起点（已有出牌/非第1局/已挂载）返回 False。"""
        if self._fn is not None or game.round_number != 1 or game.played_cards:
            return False
        self._fn = self._on_event
        game.listeners.append(self._fn)
        return True

    def detach(self, game) -> None:
        if self._fn is not None and self._fn in game.listeners:
            game.listeners.remove(self._fn)
        self._fn = None

    def _on_event(self, game, event: str, data: dict) -> None:
        if event == "round_start":
            self.trick = 0
            self.seq = 0
            self._trick_open = False
        elif event == "play":
            if self._trick_open:
                self.seq += 1
            else:                     # 领出开新一轮
                self.trick += 1
                self.seq = 1
                self._trick_open = True
        elif event == "pass":
            self.seq += 1
            # 收牌判定须与 game._advance 一致：事件回调发生在 _advance 之前，
            # 此刻 last_play 尚未清空，不能以 last_play is None 判断收牌
            owner = game.last_play[0] if game.last_play else None
            others = [p for p in game.active_players() if p != owner] \
                if owner is not None else []
            if others and all(p in game.passed_since for p in others):
                self._trick_open = False   # 各家均过，本轮收牌

    def next_meta(self, game) -> Tuple[int, int]:
        """即将发生的动作的 (trick, seq)（ReplaySession.meta 口径）。"""
        if game.last_play is None:    # 领出：开新一轮
            return self.trick + 1, 1
        return self.trick, self.seq + 1

    def capture(self, game, *, pid: int, sid: str,
                profile: Optional[AIProfile] = None,
                kind: str, cards: Optional[list] = None,
                reason: str = "", action_index: int = 0,
                cost_ms: float = 0.0,
                candidates: Optional[List[Dict[str, Any]]] = None,
                n_candidates: int = 0,
                search_depth: int = 0, n_rollouts: int = 0,
                context: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """采集决策快照并落盘，返回 decision_id；失败返回 None。"""
        try:
            trick, seq = self.next_meta(game)
            snap = build_decision_snapshot(
                self.seed, game, pid, sid, profile=profile,
                kind=kind, cards=cards, reason=reason,
                action_index=action_index, trick=trick, seq=seq,
                cost_ms=cost_ms, candidates=candidates,
                n_candidates=n_candidates,
                search_depth=search_depth, n_rollouts=n_rollouts,
                context=context)
            self.store.append(snap.to_dict())
            return snap.decision_id
        except Exception:
            return None
