"""出牌建议 v1（教学体系模块二）。

为人类玩家（pid=0）提供「下一手怎么出」的规则化建议，附简短理由。
架构约束（2026-10-13 定稿）：
- 教学与 AI 体系互不引用：本模块只依赖 rules/cards，绝不 import ai/registry，
  建议逻辑独立实现、可各自演进，不影响 AI 决策；
- 只读公开信息 + 玩家手牌，不做任何概率推断显示对手手牌。

v1 建议规则（纯启发式，后续版本可引入 PIMC/模型）：
1. 能一次出完 → 直接出（最高优先级）；
2. 跟队友牌 → 过牌（让队友继续拿牌权）；
3. 跟对手牌：
   - 对手报牌（剩 ≤2）→ 必顶：出最大非炸跟牌；无则出最小炸弹；
   - 普通 → 最小成本非炸跟牌；无牌可压且对手临近出完（≤6）→ 最小炸弹抢权；
   - 否则过牌；
4. 领出：
   - 队友报牌 → 送小于级牌的最小单张帮其跑牌；
   - 对手报牌 → 优先出组合牌（避免送低单张让其跑完）；
   - 默认 → 最小成本非炸领出。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from rules import Combo, HandType, generate_legal_moves, move_cost

_FEED_PV_MAX = 15          # 送牌上限：级牌 pv=15（王 16/17 自然排除）
_OPP_REPORT = 2            # 对手报牌阈值（剩 N 张）
_OPP_THREAT = 6            # 对手临近出完阈值（值得用炸抢权）


def advise(game, pid: int = 0) -> Optional[Dict]:
    """返回 {"action": "play"/"pass", "combo": Combo|None, "text": 理由}。

    非该玩家回合 / 非行牌阶段 / 已出完 → 返回 None（无建议）。
    """
    g = game
    if g.phase != "playing" or g.turn != pid:
        return None
    if pid in g.finish_order or not g.hands[pid]:
        return None
    hand = g.hands[pid]
    prev = g.last_play[1] if g.last_play else None
    if prev is not None and g.last_play[0] == pid:
        prev = None                       # 牌权回到自己（理论少见，防御）
    moves = generate_legal_moves(hand, prev, g.level)
    if prev is not None and not moves:
        return {"action": "pass", "combo": None, "text": "无牌可压，过牌"}
    # 1. 一次出完
    for m in moves:
        if len(m.cards) == len(hand):
            return {"action": "play", "combo": m,
                    "text": f"可一次出完！出 {m.cards[0].label()} 等"}
    if prev is None:
        return _advise_lead(moves, g, pid)
    return _advise_follow(moves, g, pid)


def _partners_and_opps(g, pid):
    partner = g.partner(pid)
    if partner in g.finish_order:
        partner = None
    opps = [p for p in g.active_players()
            if g.team_of(p) != g.team_of(pid)]
    opp_min = min((g.hand_count(o) for o in opps), default=99)
    return partner, opp_min


def _advise_lead(moves: List[Combo], g, pid: int) -> Dict:
    partner, opp_min = _partners_and_opps(g, pid)
    non_bomb = [m for m in moves if not m.is_bomb_family()]
    if not non_bomb:
        m = min(moves, key=lambda m: (m.tier(), m.rank))
        return {"action": "play", "combo": m, "text": f"仅剩炸弹：出 {m.cards[0].label()} 等"}
    pool = sorted(non_bomb, key=lambda m: (move_cost(m, g.level), m.size))
    # 队友报牌：送小于级牌的最小单张（对手剩 1 张时不送，防截胡跑完）
    if partner is not None and g.hand_count(partner) <= _OPP_REPORT and opp_min > 1:
        singles = [m for m in pool if m.type is HandType.SINGLE and m.rank < _FEED_PV_MAX]
        if singles:
            m = singles[0]
            return {"action": "play", "combo": m,
                    "text": f"队友报牌：送 {m.cards[0].label()} 帮其跑牌"}
    # 对手报牌：避免送低单张
    if opp_min <= _OPP_REPORT:
        nonsingle = [m for m in pool if m.type is not HandType.SINGLE]
        if nonsingle:
            m = nonsingle[0]
            return {"action": "play", "combo": m,
                    "text": f"对手报牌：出 {m.type.value} 防其跑完"}
        m = pool[0]
        return {"action": "play", "combo": m,
                "text": f"对手报牌：出 {m.cards[0].label()}"}
    m = pool[0]
    return {"action": "play", "combo": m,
            "text": f"领出：{m.type.value} {m.cards[0].label()}"}


def _advise_follow(moves: List[Combo], g, pid: int) -> Dict:
    owner = g.last_play[0]
    partner, opp_min = _partners_and_opps(g, pid)
    # 队友牌权 → 过牌（让队友继续出）
    if partner is not None and owner == partner:
        return {"action": "pass", "combo": None, "text": "队友牌权，建议过牌"}
    if g.team_of(owner) == g.team_of(pid):
        return {"action": "pass", "combo": None, "text": "队友牌权，建议过牌"}
    non_bomb = [m for m in moves if not m.is_bomb_family()]
    # 对手报牌 → 必顶
    if opp_min <= _OPP_REPORT:
        if non_bomb:
            m = max(non_bomb, key=lambda m: m.rank)
            return {"action": "play", "combo": m,
                    "text": f"对手报牌：顶 {m.type.value} {m.cards[0].label()}"}
        m = min(moves, key=lambda m: (m.tier(), m.rank))
        return {"action": "play", "combo": m,
                "text": f"对手报牌：炸弹打断 {m.cards[0].label()} 等"}
    if non_bomb:
        m = min(non_bomb, key=lambda m: (move_cost(m, g.level), m.size))
        return {"action": "play", "combo": m,
                "text": f"跟牌：{m.type.value} {m.cards[0].label()}"}
    # 只剩炸弹可压：对手临近出完才建议用炸
    if opp_min <= _OPP_THREAT:
        m = min(moves, key=lambda m: (m.tier(), m.rank))
        return {"action": "play", "combo": m,
                "text": f"对手临近出完：炸弹抢权 {m.cards[0].label()} 等"}
    return {"action": "pass", "combo": None, "text": "牌不必要，建议过牌"}
