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

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import inference
import strategy
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
                   memory: Optional[CardMemory],
                   threshold: Optional[int] = None) -> List[Combo]:
    """残局浅层搜索（enhanced 专用）。

    - 能一次出完：直接锁定该动作；
    - 主动出牌且处于关键时刻（对手即将出完，或己方剩牌≤4）：
      锁定「未见牌大概率压不住」的组合中张数最多者，抢回牌权连续甩牌。
      平时残局（5-8 张）不做锁定，保持基础评分的甩牌节奏。
    ``threshold``：触发阈值（手牌剩余张数），None=经典默认 ``ENDGAME_HAND``，
    0=禁用搜索（阶段六档位预算参数化）。
    """
    hc = game.hand_count(pid)
    eff = ENDGAME_HAND if threshold is None else threshold
    if memory is None or hc > eff or eff <= 0:
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
    # 送牌前置条件提炼为 strategy.feed_helps（阶段四，语义逐位一致）
    if not strategy.feed_helps(game, pid):
        return None
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


# ---------------------------------------------------------------- 权重
@dataclass
class Weights:
    """评分权重表（任务书附录 B.5：可配置，供 bench A/B 校准）。

    阶段四新增两项系数，默认 0 → 经典档评分逐字节不变（test_arch 守护）；
    阶段六随新 AI 档位启用并经 bench 校准后调整默认值。
    """
    risk: float = 0.0             # 未知牌被压风险系数（inference 估计 ×系数 → risk 分项）
    team_feed_lead: float = 0.0   # 领出低单帮对家跑牌的协同系数（→ gain_team 分项）


@dataclass(frozen=True)
class Budget:
    """档位预算（阶段六）：搜索深度与计算预算按档位参数化。

    各字段缺省（None/0）= 经典默认值（``ENDGAME_HAND`` /
    ``MAX_CANDIDATES`` / ``Weights()``），经典档不传 budget → 行为逐字节
    不变（test_arch/test_ai 守护）。registry 阶段六新档位经 budget 传入：
    - novice:       endgame_hand=0（残局搜索关闭）、候选上限 4；
    - intermediate: 经典残局阈值、候选上限 6；
    - advanced:     经典阈值 + gain_team 协同分项启用（team_feed_lead=1.0；
      risk 经 bench A/B 证伪关闭——25/6/2 → 35.4%/46.3%/50.2% 单调劣化，
      见 registry 注释与任务书附录 D 阶段六）。
    """
    endgame_hand: Optional[int] = None   # 残局搜索触发阈值（手牌数）；0=禁用
    max_candidates: int = 0              # 快照候选上限；0=MAX_CANDIDATES
    weights: Optional[Weights] = None    # 评分权重（None=Weights() 全 0）


# ---------------------------------------------------------------- 评分
# 统一候选动作评分分项（任务书附录 B.5）：每个收益维度只归入一个 part，
# 防止重复奖励；total = 各分项贡献之和（min 越小越优，负值=利好该动作）。
# risk / gain_team 阶段四实装：经 Weights 系数启用（默认 0，经典行为不变），
# 数据源为 inference 无状态概率推断（被压风险估计 / 领出送牌协同）。
SCORE_PARTS = ("gain_self", "gain_team", "block", "tempo", "cost", "risk",
               "endgame")


def _beat_risk_estimate(game, pid: int, move: Combo, ctx: dict) -> float:
    """候选动作被对手压住的估计概率 [0,1]（risk 分项数据源，阶段四启发式）。

    - 单张：对手合计牌池含更高点牌的精确概率（inference.pool_contains_prob）；
    - 对子：对手池内出现更高成对点的并集近似（各点事件按容斥上界求和）；
    - 炸弹族：未见更大炸弹/王可压 → 二值 0.9/0.0；
    - 其余复合牌型：保守常数 0.5。
    仅当 Weights.risk > 0 时进入评分，默认关闭不影响经典行为。
    """
    mem = ctx["memory"]
    if mem is None:
        return 0.0
    unseen = mem.unseen
    total = sum(unseen.values())
    opp_pool = sum(game.hand_count(o) for o in game.active_players()
                   if game.team_of(o) != game.team_of(pid))
    if opp_pool <= 0 or total <= 0:
        return 0.0
    t = move.type
    if t is HandType.SINGLE:
        return inference.pool_contains_prob(mem.singles_higher(move.rank),
                                            opp_pool, total)
    if t is HandType.PAIR:
        p = 0.0
        for r, n in unseen.items():
            if n >= 2 and point_value(r, game.level) > move.rank:
                p += inference.hypergeom_at_least(n, opp_pool, total, 2)
        return min(1.0, p)
    if move.is_bomb_family():
        return 0.9 if mem.beat_possible(move) else 0.0
    return 0.5


def score_move_detail(game, pid: int, move: Combo, ctx: dict,
                      profile: AIProfile) -> Tuple[float, Dict[str, float]]:
    """``_score_move`` 的分项版：返回 ``(total, score_parts)``。

    total 与旧 ``_score_move`` 逐位一致（同一表达式、同一求和序列）；
    score_parts 按语义把各项归入 B.5 七分项（各分项之和与 total 仅差
    浮点归组误差）。分项归集：
    - gain_self  走牌/甩牌收益：甩点数、一次出完、主动多甩长组合；
    - gain_team  队友协同：过牌让路（既有）+ 领出送牌（阶段四，Weights 启用）；
    - block      压制敌方：能压则压、urgent_top 报牌压制、炸阻对手跑完；
    - tempo      牌权与节奏：mem_lead 稳拿牌权（过牌让权记 tempo 代价）；
    - cost       拆牌/炸弹/控制牌消耗：用炸保留、拆炸、拆同花顺、废逢人配；
    - risk       未知牌被压风险（阶段四 inference 估计，Weights.risk 启用）；
    - endgame    对最终结果影响：天王炸终结价值。
    """
    parts: Dict[str, float] = {k: 0.0 for k in SCORE_PARTS}
    score = 0.0

    def add(part: str, v: float) -> None:
        # 与旧实现 score += v 的求和序逐位一致（a-b 与 a+(-b) 在 IEEE 下等价）
        nonlocal score
        score = score + v
        parts[part] = parts[part] + v

    level = game.level
    bomb_ranks = ctx["bomb_ranks"]
    sf_eids = ctx["sf_eids"]
    move_cards = move.cards

    # 基础：出牌点数越低越好（甩小牌）
    add("gain_self",
        sum(point_value(c.rank, level) for c in move_cards if not c.is_wild(level)))

    # 一次性出完：极大奖励
    if len(move_cards) == game.hand_count(pid):
        add("gain_self", -500)

    # 主动出牌时，优先多甩牌（长组合）
    if ctx["leading"]:
        add("gain_self", -(len(move_cards) * 12 * profile.dump))

    # 炸弹族：保留倾向
    if move.is_bomb_family():
        add("cost", 260 * profile.bomb_use)
        if ctx["opponent_threat"]:
            add("block", -200)  # 对手即将出完，值得动用炸弹
        if move.type is HandType.ROCKET:
            add("endgame", -40)  # 天王炸最果断，可优先用于终结

    # 拆炸弹惩罚：非炸弹却消耗炸弹点数的牌
    if not move.is_bomb_family():
        for c in move_cards:
            if not c.is_joker and c.rank in bomb_ranks:
                add("cost", 120)

    # 拆同花顺惩罚
    if move.type is not HandType.STRAIGHT_FLUSH:
        for c in move_cards:
            if c.eid in sf_eids:
                add("cost", 100)

    # 浪费逢人配惩罚（用于普通单/对/三）
    if move.type in (HandType.SINGLE, HandType.PAIR, HandType.TRIPLE):
        for c in move_cards:
            if c.is_wild(level):
                add("cost", 60)

    # 跟对手牌：能压则压，倾向小代价
    if not ctx["leading"] and ctx["beat_opponent"]:
        add("block", -30)
        owner = ctx["owner"]
        # V1.2 urgent_top：出牌者为报牌对手（剩≤2 张），不压则其可能借牌权跑完
        if "urgent_top" in ctx["features"] and owner is not None \
                and game.hand_count(owner) <= 2:
            add("block", -80)
        # V1.2 mem_lead：压完大概率无人能反压（含队友，其反压即转回本队牌权）
        # → 稳拿牌权值得奖励。仅限非炸弹、不含级牌/王/逢人配的跟牌，
        #   避免大王/级牌提前挥霍（见文首历史教训）。
        mem = ctx["memory"]
        if "mem_lead" in ctx["features"] and mem is not None \
                and not move.is_bomb_family():
            plain_pv = [point_value(c.rank, level) for c in move_cards
                        if not c.is_wild(level)]
            if plain_pv and max(plain_pv) < 15 and not mem.beat_possible(move):
                add("tempo", -40)

    # 阶段四 inference 实装：risk / gain_team 团队送牌（默认系数 0 →
    # 本块整体短路，经典档评分逐字节不变）。阶段六起仅 advanced 启用
    # team_feed_lead=1.0（其特性集不含 feed 硬覆盖，评分式协同才可达——
    # 修复 2026-10-20 发现的遮蔽问题：原 advanced 同时带 feed 硬覆盖与
    # 本评分，触发窗口被 _feed_teammate 提前 return 完全遮蔽，从未生效）。
    # can_finish 守卫与 _feed_teammate 同口径：本步能出完时不送牌，防
    # 一手出完的低单被 -30 惩罚（ctx 缺该键=手工构造 ctx 的旧调用者，
    # 行为同前）。
    weights = ctx.get("weights")
    if weights is not None and (weights.risk or weights.team_feed_lead):
        if weights.team_feed_lead and ctx["leading"] \
                and not ctx.get("can_finish") \
                and strategy.feed_helps(game, pid):
            # 领出低单（不含级牌/王/逢人配）给对家报牌者留压牌机会
            plain_pv = [point_value(c.rank, level) for c in move_cards
                        if not c.is_wild(level)]
            if move.type is HandType.SINGLE and plain_pv \
                    and max(plain_pv) < 15:
                add("gain_team", -30.0 * weights.team_feed_lead)
        if weights.risk and ctx["memory"] is not None:
            add("risk", weights.risk * _beat_risk_estimate(game, pid, move, ctx))

    return score, parts


def _score_move(game, pid: int, move: Combo, ctx: dict, profile: AIProfile) -> float:
    """旧签名兼容包装：返回总分（分项见 ``score_move_detail``）。"""
    return score_move_detail(game, pid, move, ctx, profile)[0]


def _pass_score_detail(game, pid: int, ctx: dict,
                       profile: AIProfile) -> Tuple[float, Dict[str, float]]:
    """过牌候选评分（分项版）：让权/让路的代价归入对应分项。"""
    parts: Dict[str, float] = {k: 0.0 for k in SCORE_PARTS}
    if ctx["leading"]:
        return float("inf"), parts      # 主动出牌不可过
    if ctx["beat_teammate"]:
        v = -50 * profile.dump          # 队友持权，让路=协同收益
        parts["gain_team"] = v
        return v, parts
    parts["tempo"] = 55.0               # 跟对手不压：让出本轮牌权/节奏
    return 55.0, parts


def _pass_score(game, pid: int, ctx: dict, profile: AIProfile) -> float:
    """旧签名兼容包装：返回过牌总分。"""
    return _pass_score_detail(game, pid, ctx, profile)[0]


# ---------------------------------------------------------------- 决策
MAX_CANDIDATES = 8     # 快照存储的主要候选上限（按总分升序裁剪，必含最终动作）


def _candidate_entry(kind: str, move: Optional[Combo], total: float,
                     parts: Dict[str, float], selected: bool) -> dict:
    """候选动作 → 快照条目（B.1 candidates 键位）。"""
    if kind == "pass":
        return {"kind": "pass", "type": None, "rank": None, "eids": [],
                "score_total": total, "score_parts": dict(parts),
                "selected": selected}
    return {"kind": "play", "type": move.type.value, "rank": move.rank,
            "eids": sorted(c.eid for c in move.cards),
            "score_total": total, "score_parts": dict(parts),
            "selected": selected}


def choose_action_detail(game, pid: int,
                         profile: Optional[AIProfile] = None,
                         enhanced: bool = False,
                         features: Optional[frozenset] = None,
                         weights: Optional[Weights] = None,
                         budget: Optional[Budget] = None
                         ) -> Tuple[str, Optional[List[Card]], dict]:
    """``choose_action`` 的明细版：额外返回候选动作评分明细。

    返回 ``(kind, cards, detail)``，前两项与 ``choose_action`` 逐位一致
    （决策路径完全同构，test_arch 等价性回归守护）。detail：
    - ``candidates``：主要候选（含过牌，若有），按 score_total 升序排列
      （并列按评估序，过牌居先——与主循环「严格小于取胜」的并列语义
      一致），至多 ``MAX_CANDIDATES`` 项且必含最终动作；每项含分项评分
      score_parts 与 selected 标记；
    - ``n_candidates``：实际参与评分的候选总数（含合法过牌）；
    - ``cost``：搜索深度与计算预算（``search_depth``=残局搜索触发阈值，
      未启用 enhanced 时为 0；``n_rollouts``=PIMC 推演数，经典引擎恒 0）。
    送牌意图层触发时仅评估送牌动作本身（与 choose_action 提前返回一致，
    该动作为硬覆盖选择，分项仅供参考）。
    ``weights`` 为阶段四权重表；``budget`` 为阶段六档位预算（endgame_hand/
    max_candidates/weights），两者同给时 budget.weights 仅在 weights 为
    None 时生效；budget 缺省 → 全部经典默认，行为逐字节不变。
    """
    profile = profile or AIProfile("balanced")
    level = game.level
    prev = game.last_play[1] if game.last_play else None
    owner = game.last_play[0] if game.last_play else None
    feats = features if features is not None else frozenset()
    end_hand = ENDGAME_HAND if budget is None or budget.endgame_hand is None \
        else budget.endgame_hand
    eff_weights = weights if weights is not None else \
        (budget.weights if budget is not None else None)

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
        "weights": eff_weights if eff_weights is not None else Weights(),
    }

    moves = generate_legal_moves(game.hands[pid], prev, level)
    moves = probability_refine(moves, game, pid)
    # 残局锁定仅随 enhanced 启用，避免 mem_lead 单开时混入残局行为干扰 A/B 归因
    moves = endgame_search(moves, game, pid, mem if enhanced else None,
                           threshold=end_hand)
    # 本步能否一手出完（gain_team 评分守卫用，与 _feed_teammate 同口径）
    ctx["can_finish"] = any(len(m.cards) == game.hand_count(pid) for m in moves)

    feed = _feed_teammate(moves, game, pid, ctx)
    if feed is not None:
        total, parts = score_move_detail(game, pid, feed, ctx, profile)
        entry = _candidate_entry("play", feed, total, parts, True)
        return ("play", list(feed.cards),
                {"candidates": [entry], "n_candidates": 1,
                 "cost": {"search_depth": end_hand if enhanced else 0,
                          "n_rollouts": 0}})

    # 评估序：过牌为在位者（并列时取胜，与 choose_action 初值语义一致），
    # 其后按 generate_legal_moves 顺序逐个评分
    cands: List[tuple] = []          # (评估序, kind, move, total, parts)
    p_total, p_parts = _pass_score_detail(game, pid, ctx, profile)
    if p_total != float("inf"):      # 主动出牌不可过，过牌非候选
        cands.append((0, "pass", None, p_total, p_parts))
        best_score = p_total
    else:
        best_score = float("inf")
    best_seq = 0
    for i, m in enumerate(moves):
        s, parts = score_move_detail(game, pid, m, ctx, profile)
        cands.append((i + 1, "play", m, s, parts))
        if s < best_score:
            best_score = s
            best_seq = i + 1
    best_kind, best_cards = ("pass", None) if best_seq == 0 else \
        ("play", list(moves[best_seq - 1].cards))

    max_c = MAX_CANDIDATES if budget is None or budget.max_candidates <= 0 \
        else budget.max_candidates
    ranked = sorted(cands, key=lambda t: (t[3], t[0]))[:max_c]
    entries = [_candidate_entry(kind, m, total, parts, seq == best_seq)
               for seq, kind, m, total, parts in ranked]
    return (best_kind, best_cards,
            {"candidates": entries, "n_candidates": len(cands),
             "cost": {"search_depth": end_hand if enhanced else 0,
                      "n_rollouts": 0}})


def choose_action(game, pid: int,
                  profile: Optional[AIProfile] = None,
                  enhanced: bool = False,
                  features: Optional[frozenset] = None,
                  weights: Optional[Weights] = None,
                  budget: Optional[Budget] = None
                  ) -> Tuple[str, Optional[List[Card]]]:
    """返回 ('play', cards) 或 ('pass', None)。

    ``enhanced=True`` 时启用记牌器与残局搜索（对应设置页「AI 牌力：高」）；
    默认 False 保持 V1.0 基础策略行为不变。
    ``features`` 为 V1.2 特性开关集合（mem_lead/urgent_top/feed，见模块头），
    None=全关（行为与 V1.1 完全一致）；bench_ai.py 按队切换做 A/B。
    ``weights`` 为阶段四评分权重表（默认 None → 经典行为逐字节不变）。
    ``budget`` 为阶段六档位预算（endgame_hand/max_candidates/weights），
    None=经典默认。候选分项明细见 ``choose_action_detail``
    （决策路径同构，行为逐位一致）。
    """
    kind, cards, _ = choose_action_detail(game, pid, profile,
                                          enhanced=enhanced, features=features,
                                          weights=weights, budget=budget)
    return kind, cards


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
