"""AI 出牌策略（V1.0 基础规则策略 + V1.1 残局增强）。

对外统一接口：``choose_action(game, player_id)``。

策略要点：
1. 所有候选出牌均由规则引擎 ``generate_legal_moves`` 生成，保证合法；
   最终仍由 ``game.play`` 内的统一引擎再次校验，禁止输出未校验结果。
2. 评分选择最优动作：尽量保留炸弹/同花顺等高价值组合；
   综合考虑队友出牌、当前牌权、对手剩余牌数、能否一次出完。
3. 提供三个基础人格（aggressive / balanced / conservative），供三个 AI 座位差异化使用。

V1.1 增强模块（``enhanced=True`` 时启用，对应设置页「AI 牌力：高」）：
- ``CardMemory``：记牌器，按当前玩家视角统计未见牌（其余三家手牌）分布。
- ``endgame_search``：残局浅层搜索——能一次出完直接锁定；对手报牌或
  己方剩牌≤4 时，主动出牌锁定「未见牌大概率压不住」的组合抢回牌权。

V1.2 战术特性（方案 A，``features`` 开关逐项经 bench_ai.py A/B 验证）：
- ``mem_lead``：跟对手牌时用记牌器判断「压完大概率无人能反压」→ 额外奖励
  抢牌权；仅限非炸弹、不含级牌/王/逢人配的跟牌，避免大牌提前挥霍。
- ``urgent_top``：出牌者为报牌对手（剩≤2 张）时加大跟牌权重。
- ``feed``：持牌权且队友剩 1-2 张 → 意图层硬覆盖，送最小单张帮其跑牌
  （若任一对手仅剩 1 张则放弃，防其借小单张跑完）。

经 120 局 A/B 实测证伪的启发式（勿再引入）：
- 平时对「未见压不住」的安全牌减分：导致大王/级牌提前挥霍，胜率 28%→53% 基线；
- 防反炸加分（对手可能有炸弹则更保守用炸）：未见 4+ 同点在开局是常态，
  信号无效，且对手报牌时本应果断用炸打断；
- critical 场景顶牌 -55：胜率 43% vs 53% 基线，无净收益。
浅层启发式在单局胜率上已接近该评分框架上限，进一步提升需神经网络/
自博弈方向（行为克隆或 RL），见项目记忆。

扩展接口（预留，暂不启用）：
- ``probability_refine``：概率推断钩子。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from cards import Card, point_value
from rules import (Combo, HandType, can_play, find_straight_flushes,
                   generate_legal_moves, split_wilds)

ENDGAME_HAND = 8          # 残局搜索触发阈值（手牌剩余张数）

# V1.2 特性开关（bench_ai.py 逐项 A/B 验证；HIGH_FEATURES 回填验证通过的默认集）
FEATURE_MEM_LEAD = "mem_lead"      # 跟牌用记牌器评估「压完稳拿牌权」
FEATURE_URGENT_TOP = "urgent_top"  # 对手报牌时加大跟牌权重
FEATURE_FEED = "feed"              # 队友报牌送牌（意图层硬覆盖）
ALL_FEATURES = frozenset({FEATURE_MEM_LEAD, FEATURE_URGENT_TOP, FEATURE_FEED})
# 2026-10-13 A/B 结论（每组 2000 场 ≈2.2 万局，Wilson ±0.65%，bench_ai.py）：
#   feed / mem_lead 单开中性（49.99% / 49.91%）；urgent_top 单开 +4.9%（54.87%）；
#   三特性组合 55.34%，换边对照 55.39% 一致（无首出偏差）；组合 vs basic 54.08%。
#   「高」档默认全开：组合实测最高，且送牌/抢权行为改善对局观感，无实测代价。
HIGH_FEATURES: Optional[frozenset] = ALL_FEATURES


# ---------------------------------------------------------------- 记牌器
_TOTAL_COUNTS: Dict[int, int] = {**{r: 8 for r in range(2, 15)}, 15: 2, 16: 2}


class CardMemory:
    """记牌器：以 ``pid`` 视角统计未见牌（其余三家手牌）的分布。

    数据源：全部 108 张的固定配额 - 自己手牌 - 本局已出牌（game.played_cards）。
    进贡/还贡是玩家间转移，不影响配额统计。
    """

    def __init__(self, game, pid: int) -> None:
        self.pid = pid
        self.level = game.level
        seen: Dict[int, int] = {}
        for c in game.hands[pid]:
            seen[c.rank] = seen.get(c.rank, 0) + 1
        for c in game.played_cards:
            seen[c.rank] = seen.get(c.rank, 0) + 1
        # rank -> 未见张数
        self.unseen: Dict[int, int] = {r: n - seen.get(r, 0)
                                       for r, n in _TOTAL_COUNTS.items()}
        # 单张比较点值 -> 未见张数（级牌折算 15、小王 16、大王 17）
        self.pv_unseen: Dict[int, int] = {}
        for r, n in self.unseen.items():
            if n > 0:
                pv = point_value(r, self.level)
                self.pv_unseen[pv] = self.pv_unseen.get(pv, 0) + n

    def singles_higher(self, pv: int) -> int:
        """未见单牌中点值大于 ``pv`` 的总张数。"""
        return sum(n for v, n in self.pv_unseen.items() if v > pv)

    def pair_possible_above(self, pv: int) -> bool:
        """未见牌中是否存在点值大于 ``pv`` 的成对可能（同点剩 2+）。"""
        return any(n >= 2 and point_value(r, self.level) > pv
                   for r, n in self.unseen.items())

    def bomb_ranks_unseen(self) -> set:
        """未见张数 ≥4 的普通点数（对手可能仍握有炸弹）。"""
        return {r for r, n in self.unseen.items() if n >= 4 and r < 15}

    def jokers_unseen(self) -> int:
        return self.unseen.get(15, 0) + self.unseen.get(16, 0)

    def beat_possible(self, combo: Combo) -> bool:
        """近似判断：未见牌中是否可能存在压过该组合的牌。

        单张/对子精确判定；炸弹族按「剩炸弹点数或剩王」近似；
        复合牌型（顺子/连对/钢板/三带二等）构成路径多，保守返回 True。
        注意统计含队友未见牌：队友能压只是转回本队牌权，无害。
        """
        t = combo.type
        if t is HandType.SINGLE:
            return self.singles_higher(combo.rank) > 0
        if t is HandType.PAIR:
            return self.pair_possible_above(combo.rank)
        if t is HandType.ROCKET:
            return False          # 天王炸无解
        if combo.is_bomb_family():
            return bool(self.bomb_ranks_unseen()) or self.jokers_unseen() > 0
        return True


def probability_refine(moves, game, pid):  # 扩展钩子
    return moves


def endgame_search(moves: List[Combo], game, pid: int,
                   memory: Optional[CardMemory]) -> List[Combo]:
    """残局浅层搜索（enhanced 专用）。

    - 能一次出完：直接锁定该动作；
    - 主动出牌且处于关键时刻（对手即将出完，或己方剩牌≤4）：
      锁定「未见牌大概率压不住」的组合中张数最多者，抢回牌权连续甩牌。
      平时残局（5-8 张）不做锁定，保持基础评分的甩牌节奏。
    """
    hc = game.hand_count(pid)
    if memory is None or hc > ENDGAME_HAND:
        return moves
    done = [m for m in moves if len(m.cards) == hc]
    if done:
        return [done[0]]
    if game.last_play is None:      # 主动出牌
        threat = any(game.hand_count(o) <= 6 for o in game.active_players()
                     if o != pid and game.team_of(o) != game.team_of(pid))
        if threat or hc <= 4:
            safe = [m for m in moves if not memory.beat_possible(m)]
            if safe:
                return [max(safe, key=lambda m: (len(m.cards), -m.rank))]
    return moves


def _feed_teammate(moves: List[Combo], game, pid: int,
                   ctx: dict) -> Optional[Combo]:
    """V1.2 送牌意图层（feed）：持牌权且队友剩 1-2 张时，送最小单张帮其跑牌。

    与 ``endgame_search`` 同为硬覆盖模式：直接改动作选择，不做加减分。
    送小单张让队友白拿牌权：剩 2 张时赢下这轮即获首出、下轮出最后一张；
    只送点值低于级牌的最小单张（避开王/级牌/逢人配/炸弹与同花顺核心）。
    座位与队友之间隔着对手，送牌可能被截胡——这是配合的基本赌注，但若任一
    对手仅剩 1 张（其最后单牌大概率压住并直接跑完）则放弃送牌。
    """
    if "feed" not in ctx["features"] or not ctx["leading"]:
        return None
    hc = game.hand_count(pid)
    if any(len(m.cards) == hc for m in moves):
        return None                       # 本步能出完，优先获胜
    mate = game.partner(pid)
    if mate in game.finish_order or game.hand_count(mate) > 2:
        return None
    if any(o != mate and game.hand_count(o) == 1 for o in game.active_players()):
        return None                       # 有对手报单，小单张会送对手跑掉
    level = game.level
    bomb_ranks = ctx["bomb_ranks"]
    sf_eids = ctx["sf_eids"]
    singles = [m for m in moves if m.type is HandType.SINGLE]
    if not singles:
        return None                       # 残局锁定等场景下可能无单张可选

    def key(m: Combo) -> tuple:
        c = m.cards[0]
        return (point_value(c.rank, level),
                1 if c.is_wild(level) else 0,
                1 if c.rank in bomb_ranks else 0,
                1 if c.eid in sf_eids else 0,
                c.eid)

    best = min(singles, key=key)
    if point_value(best.cards[0].rank, level) >= 15:
        return None                       # 最小单张已是大牌，不送
    return best


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
        owner = ctx["owner"]
        # V1.2 urgent_top：出牌者为报牌对手（剩≤2 张），不压则其可能借牌权跑完
        if "urgent_top" in ctx["features"] and owner is not None \
                and game.hand_count(owner) <= 2:
            score -= 80
        # V1.2 mem_lead：压完大概率无人能反压（含队友，其反压即转回本队牌权）
        # → 稳拿牌权值得奖励。仅限非炸弹、不含级牌/王/逢人配的跟牌，
        #   避免大王/级牌提前挥霍（见文首历史教训）。
        mem = ctx["memory"]
        if "mem_lead" in ctx["features"] and mem is not None \
                and not move.is_bomb_family():
            plain_pv = [point_value(c.rank, level) for c in move_cards
                        if not c.is_wild(level)]
            if plain_pv and max(plain_pv) < 15 and not mem.beat_possible(move):
                score -= 40

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
                  profile: Optional[AIProfile] = None,
                  enhanced: bool = False,
                  features: Optional[frozenset] = None
                  ) -> Tuple[str, Optional[List[Card]]]:
    """返回 ('play', cards) 或 ('pass', None)。

    ``enhanced=True`` 时启用记牌器与残局搜索（对应设置页「AI 牌力：高」）；
    默认 False 保持 V1.0 基础策略行为不变。
    ``features`` 为 V1.2 特性开关集合（mem_lead/urgent_top/feed，见模块头），
    None=全关（行为与 V1.1 完全一致）；bench_ai.py 按队切换做 A/B。
    """
    profile = profile or AIProfile("balanced")
    level = game.level
    prev = game.last_play[1] if game.last_play else None
    owner = game.last_play[0] if game.last_play else None
    feats = features if features is not None else frozenset()

    mem = CardMemory(game, pid) if (enhanced or "mem_lead" in feats) else None
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
        "memory": mem,
        "owner": owner,
        "features": feats,
    }

    moves = generate_legal_moves(game.hands[pid], prev, level)
    moves = probability_refine(moves, game, pid)
    # 残局锁定仅随 enhanced 启用，避免 mem_lead 单开时混入残局行为干扰 A/B 归因
    moves = endgame_search(moves, game, pid, mem if enhanced else None)

    feed = _feed_teammate(moves, game, pid, ctx)
    if feed is not None:
        return ("play", list(feed.cards))

    best_action = ("pass", None)
    best_score = _pass_score(game, pid, ctx, profile)
    for m in moves:
        s = _score_move(game, pid, m, ctx, profile)
        if s < best_score:
            best_score = s
            best_action = ("play", list(m.cards))
    return best_action


def explain_choice(game, pid: int, kind: str,
                   cards: Optional[List[Card]]) -> str:
    """为 AI 已选动作生成一句简短中文理由（复盘「理由区」数据源）。

    在动作套用**前**调用（此刻 game 即决策时刻状态）。按决策上下文
    （主动/跟牌、持权方阵营、能否一次出完、是否炸弹族）给出口径一致
    的解释，不重算评分；异常时回退空串，不影响牌局。
    """
    try:
        level = game.level
        prev = game.last_play[1] if game.last_play else None
        owner = game.last_play[0] if game.last_play else None
        mate = owner is not None and game.team_of(owner) == game.team_of(pid)
        if kind == "pass":
            if owner is not None and mate:
                return "队友持权，让路不压"
            if prev is not None and prev.is_bomb_family():
                return "对手炸弹，不硬耗"
            return "无合适牌型，保留实力"
        combo = can_play(list(cards), prev, level)
        if combo is None:
            return ""
        if len(combo.cards) == game.hand_count(pid):
            return "一把出完，抢游数"
        if combo.is_bomb_family():
            return "动用炸弹，夺回牌权" if prev is not None else "炸弹开路"
        if prev is None:
            hc = game.hand_count(pid)
            if hc <= ENDGAME_HAND and game.hand_count(game.partner(pid)) <= 6:
                return "趁残局抢节奏甩长牌"
            return "主动出牌，先甩小整理手牌"
        if mate:
            return "接住队友，续攻施压"
        threat = any(game.hand_count(o) <= 6 for o in game.active_players()
                     if o != pid and game.team_of(o) != game.team_of(pid))
        if threat:
            return "对手牌少，压住抢权"
        return f"压住{combo.type.value}，夺回牌权"
    except Exception:
        return ""


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
def auto_step(game, profiles: Optional[List[AIProfile]] = None,
              enhanced=False, features=None) -> bool:
    """驱动 AI 完成当前待办动作（进贡/还贡/出牌/过牌/开局）。

    ``enhanced``/``features`` 可为标量（所有座位相同）或按 pid 的 dict
    （bench_ai.py A/B 对抗时 0/2 队与 1/3 队各用一套配置）。
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
    enh = enhanced[pid] if isinstance(enhanced, dict) else enhanced
    feat = features[pid] if isinstance(features, dict) else features
    kind, cards = choose_action(game, pid, prof, enhanced=enh, features=feat)
    if kind == "play":
        game.play(pid, cards)
    else:
        game.pass_play(pid)
    return True


def simulate_match(game, profiles: Optional[List[AIProfile]] = None,
                   max_actions: int = 20000,
                   enhanced=False, features=None) -> dict:
    """全自动打完一场比赛，返回统计。用于测试与冒烟。"""
    actions = 0
    while game.phase != "match_over":
        auto_step(game, profiles, enhanced=enhanced, features=features)
        actions += 1
        if actions >= max_actions:
            raise RuntimeError("模拟超过最大动作数，疑似死循环")
    return {
        "winner_team": game.match_winner,
        "rounds": game.round_number,
        "levels": dict(game.levels),
        "actions": actions,
    }
