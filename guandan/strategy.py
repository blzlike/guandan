"""团队策略与动态角色判定（阶段四，任务书五.4）。

从 ai._feed_teammate / advisor 送牌与协同规则提炼的**纯函数**团队判断：
- feed_helps(game, pid)               送牌条件谓词（与 ai._feed_teammate
                                      前置条件一致，供两处复用）
- judge_role(game, pid)               动态角色：主攻/助攻/防守/救援/终局，
                                      每步实时判定，不做整局标签

只读公开信息 + 自己手牌；pass 连击等信号由本局公开动作史重算，
无跨步可变状态。本模块属 AI 侧：observer/advisor 严禁 import
（tests 源码检查固化）。
"""
from __future__ import annotations

from typing import List

ROLES = ("主攻", "助攻", "防守", "救援", "终局")


def feed_helps(game, pid: int) -> bool:
    """送牌帮对家跑牌的条件：对家报牌 1-2 张，且无对手报单截胡风险。"""
    mate = game.partner(pid)
    if mate in game.finish_order or game.hand_count(mate) > 2:
        return False
    return not any(o != mate and game.hand_count(o) == 1
                   for o in game.active_players())


def _pass_streak(game, target: int) -> int:
    """target 最近连续过牌次数（由本局公开动作史重算，无跨步状态）。"""
    streak = 0
    for pid, kind, _combo, _lead in reversed(getattr(game, "round_history", ()) or ()):
        if pid != target:
            continue
        if kind != "pass":
            break
        streak += 1
    return streak


def judge_role(game, pid: int) -> dict:
    """动态角色判定 → {role, reason, evidence}（B.1 team_task 结构）。"""
    hand = game.hand_count(pid)
    mate = game.partner(pid)
    mate_active = mate not in game.finish_order
    opps = [o for o in game.active_players()
            if game.team_of(o) != game.team_of(pid)]
    if pid in game.finish_order:
        return {"role": "终局", "reason": "已出完，等待本局结算", "evidence": []}
    if hand <= 4:
        return {"role": "终局", "reason": f"仅剩{hand}张，规划连续出完",
                "evidence": [f"我手{hand}张"]}
    rep = [(o, game.hand_count(o)) for o in opps if game.hand_count(o) <= 2]
    if rep:
        o, n = min(rep, key=lambda t: (t[1], t[0]))
        return {"role": "防守", "reason": f"对手P{o}剩{n}张，优先压制不让其跑完",
                "evidence": [f"对手P{o}剩{n}张"]}
    if mate_active and game.hand_count(mate) <= 2:
        n = game.hand_count(mate)
        return {"role": "助攻", "reason": f"对家剩{n}张，送牌助其跑完",
                "evidence": [f"对家剩{n}张"]}
    streak = _pass_streak(game, mate)
    if mate_active and streak >= 2 and hand >= 6:
        return {"role": "救援", "reason": "对家连续过牌，接手牌权组织进攻",
                "evidence": [f"对家连续过牌{streak}次", f"我手{hand}张"]}
    ev: List[str] = [f"我手{hand}张"]
    if hand <= 8:
        return {"role": "主攻", "reason": f"仅剩{hand}张，主动推进", "evidence": ev}
    return {"role": "主攻", "reason": "正常行牌，保持牌权与节奏", "evidence": ev}
