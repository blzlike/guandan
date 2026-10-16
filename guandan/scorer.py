"""对局打分器（阶段二 v1）：专家对比法。

对每个 play/pass 动作，在「动作前」的重演状态下用注册策略（默认
classic_high，即「经典·高」）生成参考动作，与实际动作对比：

- 一致            → 1.0 分；
- 不一致          → 0.3 分；以下关键错误 → 0 分：
    ① 该出未出：专家可出而实际过牌；
    ② 乱用炸弹：实际出炸弹/同花顺/天王炸而专家未出同级别牌；
    ③ 报牌失误：报牌局面（自己或任一对手剩牌 ≤ REPORT_RED）下不一致。

座位得分 = 100 × 平均动作得分，四座位全打（含人类）。口径为该座位
可见信息：专家策略只读自己手牌与公开已出牌（ai.py 全程无对手手牌
访问），人类与 AI 同标准。进贡/还贡动作不评分（v1）。

入口 ``score_record(record)`` 基于 replay.rebuild 重演（记录须含
seed，即 Recorder 从比赛起点录制）。rebuild 的 on_action 在动作
套用前回调，game.turn == act["pid"]，正是决策时刻。

UI：TableScreen 在「playing → 结算」跃迁时对当前 record 打分并缓存，
结算页名次标签旁展示（决策④：四座位全打，结算页展示）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import ai
import registry
import rules
from rules import HandType
from replay import rebuild

REPORT_RED = 2        # 报牌局面阈值：自己或任一对手剩牌 ≤ 此值
GAIN_MATCH = 1.0      # 与专家一致
GAIN_MINOR = 0.3      # 普通不一致
GAIN_KEY = 0.0        # 关键错误
BOMB_TYPES = frozenset({HandType.BOMB, HandType.STRAIGHT_FLUSH, HandType.ROCKET})


def _bomb_like(cards: list, level_rank: int) -> bool:
    """cards 是否炸弹族（炸弹/同花顺/天王炸）。"""
    return any(c.type in BOMB_TYPES for c in rules.classify(list(cards), level_rank))


def _under_pressure(g, pid: int) -> bool:
    """报牌局面：自己或任一对手剩牌 ≤ REPORT_RED。"""
    if g.hand_count(pid) <= REPORT_RED:
        return True
    return any(g.hand_count(o) <= REPORT_RED for o in range(4) if o % 2 != pid % 2)


def _judge(act_kind: str, act_eids, exp_kind: str, exp_eids,
           actual_cards: list, exp_cards, pressure: bool,
           level_rank: int) -> tuple:
    """单动作判定，返回 (gain, note)。actual_cards/exp_cards 为动作前手牌
    中查到的 Card 列表（pass 时为空）。"""
    if act_eids == exp_eids:
        return GAIN_MATCH, ""
    if act_kind == "pass" and exp_kind == "play":
        return GAIN_KEY, "该出未出"
    if actual_cards and _bomb_like(actual_cards, level_rank) \
            and not (exp_cards and _bomb_like(exp_cards, level_rank)):
        return GAIN_KEY, "乱用炸弹"
    if pressure:
        return GAIN_KEY, "报牌失误"
    return GAIN_MINOR, "与专家不一致"


def score_record(record: Dict[str, Any], expert_sid: str = "classic_high",
                 max_actions: int = 50000) -> Dict[str, Any]:
    """重演 record 并逐动作打分。

    返回 {"expert": sid, "scores": {pid: 0-100}, "actions": {pid: n},
    "events": [{round,pid,kind,gain,note,eids}, ...]}。
    """
    spec = registry.get(expert_sid)
    if spec is None:
        raise KeyError(f"未注册专家策略: {expert_sid}")
    gains: Dict[int, List[tuple]] = {p: [] for p in range(4)}
    events: List[Dict[str, Any]] = []

    def on_action(act: Dict[str, Any], g) -> None:
        kind = act["kind"]
        if kind not in ("play", "pass"):
            return                      # 进贡/还贡 v1 不评分
        pid = act["pid"]
        level_rank = g.level
        # 专家参考动作：动作前状态（该座位可见信息口径）
        exp_kind, exp_cards = spec.choose(g, pid, ai.AIProfile())
        exp_eids = sorted(c.eid for c in exp_cards) if exp_cards else None
        act_eids = sorted(act["eids"]) if kind == "play" else None
        actual = [c for c in g.hands[pid] if c.eid in set(act["eids"])] \
            if kind == "play" else []
        gain, note = _judge(kind, act_eids, exp_kind, exp_eids,
                            actual, exp_cards, _under_pressure(g, pid),
                            level_rank)
        gains[pid].append(gain)
        events.append({"round": act.get("round"), "pid": pid, "kind": kind,
                       "gain": gain, "note": note,
                       "eids": list(act["eids"]) if kind == "play" else None})

    g = rebuild(record, on_action=on_action)
    n = sum(len(v) for v in gains.values())
    if n > max_actions:
        raise ValueError(f"动作数超限: {n}")
    scores = {p: round(100.0 * sum(v) / len(v), 1) if v else 100.0
              for p, v in gains.items()}
    return {"expert": expert_sid, "scores": scores,
            "actions": {p: len(v) for p, v in gains.items()}, "events": events}
