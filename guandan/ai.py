"""AI 出牌策略（V1.0 基础规则策略，不含机器学习）。

对外统一接口：``choose_action(game, player_id)``。

策略要点：
1. 所有候选出牌均由规则引擎 ``generate_legal_moves`` 生成，保证合法；
   最终仍由 ``game.play`` 内的统一引擎再次校验，禁止输出未校验结果。
2. 评分选择最优动作：尽量保留炸弹/同花顺等高价值组合；
   综合考虑队友出牌、当前牌权、对手剩余牌数、能否一次出完。
3. 提供三个基础人格（aggressive / balanced / conservative），供三个 AI 座位差异化使用。

扩展接口（V1.0 预留，暂不启用）：
- ``CardMemory``：记牌器，跟踪已出牌以推断剩余分布。
- ``probability_refine`` / ``endgame_search``：概率推断与残局搜索钩子。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from cards import Card, point_value
from rules import (Combo, HandType, find_straight_flushes, generate_legal_moves,
                   split_wilds)


# ---------------------------------------------------------------- 扩展占位
class CardMemory:
    """记牌器占位：记录每个实体牌是否已离开牌局。V1.0 不接入决策。"""

    def __init__(self) -> None:
        self.played: set = set()

    def record(self, combo: Combo) -> None:
        self.played.update(c.eid for c in combo.cards)


def probability_refine(moves, game, pid):  # 扩展钩子
    return moves


def endgame_search(moves, game, pid):  # 扩展钩子
    return moves


# ---------------------------------------------------------------- 工具
def _bomb_ranks(hand: List[Card], level: int) -> set:
    wilds, plain = split_wilds(hand, level)
    cnt = {}
    for c in plain:
        if c.is_joker:
            continue
        cnt[c.rank] = cnt.get(c.rank, 0) + 1
    ranks = {r for r, n in cnt.items() if n >= 4}
    if wilds:
        ranks |= {r for r, n in cnt.items() if n + len(wilds) >= 4 and n >= 3}
    return ranks


def _sf_eids(hand: List[Card], level: int) -> set:
    """保护价值最高的同花顺（每花色取顶牌最大者）所用实体牌。"""
    combos = find_straight_flushes(hand, level)
    if not combos:
        return set()
    best_by_suit = {}
    for c in combos:
        cur = best_by_suit.get(c.suit)
        if cur is None or c.rank > cur.rank:
            best_by_suit[c.suit] = c
    eids = set()
    for c in best_by_suit.values():
        eids.update(x.eid for x in c.cards)
    return eids


# ---------------------------------------------------------------- 人格
PERSONALITIES = ("aggressive", "balanced", "conservative")


class AIProfile:
    def __init__(self, name: str = "balanced") -> None:
        self.name = name
        # bomb_use: 愿意用炸弹抢牌权的阈值系数；dump: 甩牌激进程度
        self.bomb_use = {"aggressive": 0.7, "balanced": 1.0, "conservative": 1.4}[name]
        self.dump = {"aggressive": 1.2, "balanced": 1.0, "conservative": 0.8}[name]


# ---------------------------------------------------------------- 评分
def _score_move(game, pid: int, move: Combo, ctx: dict, profile: AIProfile) -> float:
    score = 0.0
    level = game.level
    bomb_ranks = ctx["bomb_ranks"]
    sf_eids = ctx["sf_eids"]
    move_cards = move.cards

    # 基础：出牌点数越低越好（甩小牌）
    score += sum(point_value(c.rank, level) for c in move_cards if not c.is_wild(level))

    # 一次性出完：极大奖励
    if len(move_cards) == game.hand_count(pid):
        score -= 500

    # 主动出牌时，优先多甩牌（长组合）
    if ctx["leading"]:
        score -= len(move_cards) * 12 * profile.dump

    # 炸弹族：保留倾向
    if move.is_bomb_family():
        score += 260 * profile.bomb_use
        if ctx["opponent_threat"]:
            score -= 200  # 对手即将出完，值得动用炸弹
        if move.type is HandType.ROCKET:
            score -= 40  # 天王炸最果断，可优先用于终结

    # 拆炸弹惩罚：非炸弹却消耗炸弹点数的牌
    if not move.is_bomb_family():
        for c in move_cards:
            if not c.is_joker and c.rank in bomb_ranks:
                score += 120

    # 拆同花顺惩罚
    if move.type is not HandType.STRAIGHT_FLUSH:
        for c in move_cards:
            if c.eid in sf_eids:
                score += 100

    # 浪费逢人配惩罚（用于普通单/对/三）
    if move.type in (HandType.SINGLE, HandType.PAIR, HandType.TRIPLE):
        for c in move_cards:
            if c.is_wild(level):
                score += 60

    # 跟对手牌：能压则压，倾向小代价
    if not ctx["leading"] and ctx["beat_opponent"]:
        score -= 30

    return score


def _pass_score(game, pid: int, ctx: dict, profile: AIProfile) -> float:
    if ctx["leading"]:
        return float("inf")  # 主动出牌不可过
    # 队友当前持牌权：默认让队友过，除非我们能借此出完
    if ctx["beat_teammate"]:
        return -50 * profile.dump  # 很低，倾向过牌
    # 跟对手：中等，倾向用小牌压制
    return 55


# ---------------------------------------------------------------- 决策
def choose_action(game, pid: int,
                  profile: Optional[AIProfile] = None) -> Tuple[str, Optional[List[Card]]]:
    """返回 ('play', cards) 或 ('pass', None)。"""
    profile = profile or AIProfile("balanced")
    level = game.level
    prev = game.last_play[1] if game.last_play else None
    owner = game.last_play[0] if game.last_play else None

    ctx = {
        "leading": prev is None,
        "beat_teammate": owner is not None and game.team_of(owner) == game.team_of(pid),
        "beat_opponent": owner is not None and game.team_of(owner) != game.team_of(pid),
        "opponent_threat": any(
            game.hand_count(o) <= 6
            for o in game.active_players()
            if o != pid and game.team_of(o) != game.team_of(pid)
        ),
        "bomb_ranks": _bomb_ranks(game.hands[pid], level),
        "sf_eids": _sf_eids(game.hands[pid], level),
    }

    moves = generate_legal_moves(game.hands[pid], prev, level)
    moves = probability_refine(moves, game, pid)
    moves = endgame_search(moves, game, pid)

    best_action = ("pass", None)
    best_score = _pass_score(game, pid, ctx, profile)
    for m in moves:
        s = _score_move(game, pid, m, ctx, profile)
        if s < best_score:
            best_score = s
            best_action = ("play", list(m.cards))
    return best_action


def choose_tribute(game, pid: int) -> int:
    """进贡选牌：在合法进贡候选（最大牌）中，选对炸弹/同花顺破坏最小的一张。"""
    legal = game.legal_tribute_cards(pid)
    level = game.level
    bomb_ranks = _bomb_ranks(game.hands[pid], level)
    sf_eids = _sf_eids(game.hands[pid], level)

    def damage(card: Card) -> tuple:
        return (1 if card.rank in bomb_ranks else 0,
                1 if card.eid in sf_eids else 0,
                int(card.suit), card.eid)

    return min(legal, key=damage).eid


def choose_return(game, pid: int) -> int:
    """还贡选牌：优先还点数最小、非逢人配、非炸弹/同花顺核心的孤立牌。"""
    level = game.level
    bomb_ranks = _bomb_ranks(game.hands[pid], level)
    sf_eids = _sf_eids(game.hands[pid], level)

    def key(card: Card) -> tuple:
        return (point_value(card.rank, level),
                1 if card.is_wild(level) else 0,
                1 if card.rank in bomb_ranks else 0,
                1 if card.eid in sf_eids else 0,
                card.eid)

    return min(game.hands[pid], key=key).eid


# ---------------------------------------------------------------- 自动模拟
def auto_step(game, profiles: Optional[List[AIProfile]] = None) -> bool:
    """驱动 AI 完成当前待办动作（进贡/还贡/出牌/过牌/开局）。

    返回 True 表示仍有动作可执行，False 表示比赛结束。
    """
    if game.phase == "match_over":
        return False
    if game.phase == "round_over":
        game.start_next_round()
        return True
    if game.phase == "tribute":
        pending = [(g, r) for g, r in game.tribute_pairs if g not in game.tribute_done]
        if pending:
            giver, _ = pending[0]
            game.submit_tribute(giver, choose_tribute(game, giver))
        else:
            awaiting = [r for r in game.tribute_done.values() if r not in game.return_done]
            receiver = awaiting[0]
            game.submit_return(receiver, choose_return(game, receiver))
        return True
    pid = game.turn
    prof = profiles[pid] if profiles else AIProfile()
    kind, cards = choose_action(game, pid, prof)
    if kind == "play":
        game.play(pid, cards)
    else:
        game.pass_play(pid)
    return True


def simulate_match(game, profiles: Optional[List[AIProfile]] = None,
                   max_actions: int = 20000) -> dict:
    """全自动打完一场比赛，返回统计。用于测试与冒烟。"""
    actions = 0
    while game.phase != "match_over":
        auto_step(game, profiles)
        actions += 1
        if actions >= max_actions:
            raise RuntimeError("模拟超过最大动作数，疑似死循环")
    return {
        "winner_team": game.match_winner,
        "rounds": game.round_number,
        "levels": dict(game.levels),
        "actions": actions,
    }
