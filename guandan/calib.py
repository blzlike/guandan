"""夯级AI 校准检测（value 档步骤③）：局终对照校准常量，偏离 ±25% 标记复盘。

依据 2026-10-19 夯级方案定稿：校准常量（头游 9.6 手/二游 10.6/三游 11.1，
火牌=四头 5.6/五头 0.9/同花顺 1.6）逐局对照；出炸频率/手数偏离超过 ±25%
经 review.py 标记复盘（v1 范围=出炸频率+手数；配火率 85%/还贡成火率 50%
留待后续步骤，天王炸无校准常量仅统计不对照）。

引用边界：本模块属复盘/校核侧——允许 import decision/review（快照定位
与标记落档）；value_ai/ai 等 AI 侧严禁反向 import 本模块与 review
（review 为人工输入层，tests 固化单向边界）。

CalibrationChecker 与 DecisionLogger 同型（listener，比赛起点挂载，
ui._attach_recorder 接线）：round_end 时统计本局实测值，仅当存在
sid=value 的决策快照才落 review 记录（挂靠本局最近一条夯级快照
decision_id；无快照无从复盘定位，静默跳过）。任何异常不外抛，不
影响牌局。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import review
from decision import DecisionStore
from review import ReviewStore
from rules import HandType

DEV_LIMIT = 0.25                       # 偏离阈值：|实测−基准|/基准
HAND_BASE = (9.6, 10.6, 11.1)          # 头游/二游/三游 平均用手数
FIRE_BASE = {"四头炸": 5.6, "五头炸": 0.9, "同花顺": 1.6}   # 每局期望次数
_CATEGORY = {"hands": "局势", "fire": "炸弹"}


def observed_stats(game) -> Dict[str, Any]:
    """局终实测：各家用手数 + 火牌次数（口径：round_history 逐动作）。

    手数=该玩家本局 play 动作数；火牌按 四头炸（4 头，含配火）/
    五头炸（≥5 头）/同花顺/天王炸 分档计数。
    """
    hands = {p: 0 for p in range(4)}
    fire = {k: 0 for k in FIRE_BASE}
    fire["天王炸"] = 0
    for pid, kind, combo, _lead in game.round_history:
        if kind != "play":
            continue
        hands[pid] += 1
        if combo is None or not combo.is_bomb_family():
            continue
        if combo.type is HandType.ROCKET:
            fire["天王炸"] += 1
        elif combo.type is HandType.STRAIGHT_FLUSH:
            fire["同花顺"] += 1
        elif combo.size >= 5:
            fire["五头炸"] += 1
        else:
            fire["四头炸"] += 1
    auto_last = set(((game.round_result or {}).get("auto_last")) or [])
    return {"hands": hands, "fire": fire, "auto_last": auto_last,
            "finish_order": list(game.finish_order)}


def find_deviations(stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    """实测 vs 常量对照：|偏离| > DEV_LIMIT 的项列表（纯函数，可测）。

    手数按名次位 0/1/2 对照 HAND_BASE（auto_last 记末游者手数不完整，
    不对照；末游位无常量）；火牌按 FIRE_BASE 逐项对照。条目键：
    kind(hands|fire)/label/pid/obs/base/delta。
    """
    out: List[Dict[str, Any]] = []
    auto = stats["auto_last"]
    for i, pid in enumerate(stats["finish_order"][:3]):
        if pid in auto:
            continue
        base = HAND_BASE[i]
        obs = stats["hands"].get(pid, 0)
        delta = (obs - base) / base
        if abs(delta) > DEV_LIMIT:
            out.append({"kind": "hands", "label": f"{'头二三四'[i]}游手数",
                        "pid": pid, "obs": obs, "base": base,
                        "delta": delta})
    for label, base in FIRE_BASE.items():
        obs = stats["fire"].get(label, 0)
        delta = (obs - base) / base
        if abs(delta) > DEV_LIMIT:
            out.append({"kind": "fire", "label": label, "pid": None,
                        "obs": obs, "base": base, "delta": delta})
    return out


class CalibrationChecker:
    """局终校准检测器：round_end 对照常量，偏离经 review.py 标记复盘。"""

    def __init__(self, seed: int, store: Optional[DecisionStore] = None,
                 reviews: Optional[ReviewStore] = None) -> None:
        self.seed = seed
        self.store = store or DecisionStore(seed)
        self.reviews = reviews or review.ReviewStore()
        self._fn = None

    def attach(self, game) -> bool:
        """挂载到比赛起点（与 DecisionLogger 同判据）；非起点返回 False。"""
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
        if event != "round_end":
            return
        try:
            self.check(game)
        except Exception:               # 校准失败不影响牌局
            pass

    # ------------------------------------------------------------- 检测
    def anchor_id(self, game) -> Optional[str]:
        """本局最近一条 sid=value 快照的 decision_id（无则 None）。"""
        for snap in reversed(self.store.load()):
            if snap.get("round") == game.round_number \
                    and snap.get("sid") == "value":
                return snap.get("decision_id")
        return None

    def check(self, game) -> List[Dict[str, Any]]:
        """局终对照；偏离逐项落 review 记录（挂靠最近夯级快照）。"""
        anchor = self.anchor_id(game)
        if not anchor:
            return []               # 无夯级快照：无从复盘定位，不标记
        devs = find_deviations(observed_stats(game))
        for dev in devs:
            self.mark(anchor, dev)
        return devs

    def mark(self, decision_id: str, dev: Dict[str, Any]) -> dict:
        """单条偏离 → review 记录（人工校核队列，disposition=open）。"""
        return self.reviews.append(
            decision_id=decision_id,
            category=_CATEGORY[dev["kind"]],
            verdict="improvable",
            note=f"{dev['label']} {dev['obs']}（基准 {dev['base']}，"
                 f"偏离 {dev['delta']:+.0%}）",
            conditions="夯级校准检测（局终对照 ±25%，自动标记待人工确认）")
