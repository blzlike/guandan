"""夯档 PIMC 内核（阶段五，方案B讨论稿）。

Perfect Information Monte Carlo：关键时刻把「对手/队友未知手牌」按
未见牌分布随机采样补齐为完全信息局面，对每个候选动作 clone 推演到
本局结束，按游次奖励求均值选优。非关键回合直通 classic_high，
现有两档 AI 行为零影响。

决策流水线（choose）：
  1. 非关键时刻（自己 >8 张且无对手 ≤6 张）→ classic_high（逐字节一致）；
  2. 关键时刻能一次出完 → 直接锁定；
  3. 关键时刻 → 候选剪枝（成本 top-K + 全部炸弹族 + 过牌）
     → N 组采样手牌（同一组样本评估所有候选，降低比较方差）
     → 每候选 N 次 rollout → 均值取优，平局偏向过牌/低成本。

rollout 推演策略两档（A/B 验证用）：
  - "heuristic"：四家均用 classic_low 启发式（默认，模拟质量高）；
  - "random"：四家随机合法着（快但方差大）。
  经 opts["rollout"] 传入；环境变量 GUANDAN_PIMC_ROLLOUT 改默认值
  （bench 多进程 spawn 下随环境传播，无需改 bench 代码）。

采样口径：未见实体牌 = 108 全副 − 自己手牌 − 本局已出牌（eid 精确集），
按其余三家 hand_count 约束随机均分（均匀随机划分，天然满足张数守恒）。
rng 由公开局面状态哈希派生：同局面决策可复现（测试/bench A/B 依赖）。
"""
from __future__ import annotations

import hashlib
import os
import random
from typing import Dict, List, Optional, Tuple

import ai
from cards import Card, build_deck
from game import GuandanGame
from rules import Combo, generate_legal_moves, move_cost

# ---------------------------------------------------------------- 参数
N_SAMPLES = 6            # 每决策采样组数上限（同组样本评估全部候选）
TOP_K = 4                # 候选剪枝：按 move_cost 取前 K（炸弹族另加）
MAX_ROLLOUT = 140        # 单次 rollout 动作数上限（防意外死循环/长尾）
ROLLOUT_BUDGET = 28      # 每决策 rollout 总预算：n=min(N_SAMPLES, 预算//候选数)
CRITICAL_SELF = ai.ENDGAME_HAND   # 自己剩牌 ≤8 视为关键时刻
CRITICAL_OPP = 6         # 任一对手剩牌 ≤6 视为关键时刻

_DECK: List[Card] = sorted(build_deck(), key=lambda c: c.eid)

_ROLLOUT_POLICIES = ("heuristic", "random")


def _default_rollout() -> str:
    p = os.environ.get("GUANDAN_PIMC_ROLLOUT", "heuristic")
    return p if p in _ROLLOUT_POLICIES else "heuristic"


# ---------------------------------------------------------------- 采样
def unseen_cards(game: GuandanGame, pid: int) -> List[Card]:
    """未见实体牌（含其余三家手牌）：108 − 自己手牌 − 本局已出。"""
    known = {c.eid for c in game.hands[pid]}
    known.update(c.eid for c in game.played_cards)
    return [c for c in _DECK if c.eid not in known]


def sample_partition(unseen: List[Card], counts: Dict[int, int],
                     rng: random.Random) -> Dict[int, List[Card]]:
    """把未见牌随机均分给各座位（counts 之和须等于 unseen 张数）。"""
    pool = list(unseen)
    rng.shuffle(pool)
    hands: Dict[int, List[Card]] = {}
    i = 0
    for o in sorted(counts):
        hands[o] = pool[i:i + counts[o]]
        i += counts[o]
    return hands


def _state_rng(game: GuandanGame, pid: int, salt: int = 0) -> random.Random:
    """由公开状态派生确定性 rng（跨进程可复现，不用 hash() 防盐化）。"""
    prev = game.last_play
    key = (game.round_number, game.level, pid, salt,
           tuple(sorted(c.eid for c in game.played_cards)),
           tuple(sorted(c.eid for c in prev[1].cards)) if prev else ())
    digest = hashlib.md5(repr(key).encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "little"))


# ---------------------------------------------------------------- 判定
def is_critical(game: GuandanGame, pid: int) -> bool:
    """关键时刻：自己剩牌少，或任一对手即将出完。"""
    if game.hand_count(pid) <= CRITICAL_SELF:
        return True
    return any(game.hand_count(o) <= CRITICAL_OPP
               for o in game.active_players()
               if o != pid and game.team_of(o) != game.team_of(pid))


def build_candidates(game: GuandanGame, pid: int) -> List[Tuple[str, Optional[Combo]]]:
    """候选动作：成本 top-K + 全部炸弹族；跟牌时含过牌。"""
    prev = game.last_play[1] if game.last_play else None
    level = game.level
    moves = generate_legal_moves(game.hands[pid], prev, level)
    cands: List[Tuple[str, Optional[Combo]]] = []
    seen: set = set()
    for m in moves[:TOP_K]:
        cands.append(("play", m))
        seen.add(m)
    for m in moves[TOP_K:]:
        if m.is_bomb_family() and m not in seen:
            cands.append(("play", m))
            seen.add(m)
    if game.last_play is not None:      # 跟牌时过牌也是候选
        cands.append(("pass", None))
    return cands


# ---------------------------------------------------------------- 推演
def _rollout_step(g2: GuandanGame, rng: random.Random, policy: str) -> None:
    cur = g2.turn
    prev = g2.last_play[1] if g2.last_play else None
    if policy == "random":
        moves = generate_legal_moves(g2.hands[cur], prev, g2.level)
        if prev is not None and not moves:
            g2.pass_play(cur)
        else:
            m = rng.choice(moves)
            g2.play(cur, list(m.cards))
    else:
        act, cards = ai.choose_action(g2, cur, ai.AIProfile(),
                                      enhanced=False, features=None)
        if act == "play":
            g2.play(cur, cards)
        else:
            g2.pass_play(cur)


def rollout(game: GuandanGame, pid: int, action: Tuple[str, Optional[Combo]],
            sampled: Dict[int, List[Card]], rng: random.Random,
            policy: str) -> Optional[dict]:
    """clone 注入采样手牌 → 执行候选动作 → 推演到本局结束，返回 round_result。"""
    g2 = game.clone(rng=random.Random(rng.randrange(1 << 30)))
    for o, cards in sampled.items():
        g2.hands[o] = list(cards)
    act, m = action
    if act == "play":
        g2.play(pid, list(m.cards))
    else:
        g2.pass_play(pid)
    steps = 0
    while g2.phase == "playing" and steps < MAX_ROLLOUT:
        _rollout_step(g2, rng, policy)
        steps += 1
    return g2.round_result


_REWARD = {0: 3, 1: 1, 2: -1, 3: -3}


def reward_of(result: Optional[dict], pid: int) -> int:
    """游次奖励：头游+3 二游+1 三游-1 末游-3，双下翻倍。"""
    if not result:
        return 0
    fo = result["finish_order"]
    r = _REWARD.get(fo.index(pid), 0) if pid in fo else 0
    if result.get("double_down"):
        r *= 2
    return r


# ---------------------------------------------------------------- 评估
def evaluate(game: GuandanGame, pid: int,
             cands: List[Tuple[str, Optional[Combo]]],
             n_samples: int, rng: random.Random,
             policy: str) -> List[float]:
    """同组样本评估所有候选，返回各候选平均奖励。"""
    others = {o: game.hand_count(o) for o in range(4) if o != pid}
    totals = [0.0] * len(cands)
    for _ in range(n_samples):
        sampled = sample_partition(unseen_cards(game, pid), others, rng)
        for i, act in enumerate(cands):
            res = rollout(game, pid, act, sampled, rng, policy)
            totals[i] += reward_of(res, pid)
    return [t / n_samples for t in totals]


def _tie_key(game: GuandanGame, pid: int, act: Tuple[str, Optional[Combo]]):
    """平局序：过牌优先 → 成本低 → eid 稳定序。"""
    kind, m = act
    if kind == "pass":
        return (0, 0, ())
    cost = move_cost(m, game.level)
    return (1, cost, tuple(sorted(c.eid for c in m.cards)))


# ---------------------------------------------------------------- 入口
def choose(game: GuandanGame, pid: int,
           profile: Optional[ai.AIProfile] = None,
           **opts) -> Tuple[str, Optional[List[Card]]]:
    """夯档决策入口（registry.choose 签名）。opts: rollout/n_samples。"""
    profile = profile or ai.AIProfile()
    hc = game.hand_count(pid)
    prev = game.last_play[1] if game.last_play else None
    level = game.level

    # 1. 非关键时刻直通 classic_high（保证与经典·高逐字节一致）
    if not is_critical(game, pid):
        return ai.choose_action(game, pid, profile,
                                enhanced=True, features=ai.HIGH_FEATURES)

    # 2. 关键时刻能一次出完 → 直接锁定
    moves = generate_legal_moves(game.hands[pid], prev, level)
    done = [m for m in moves if len(m.cards) == hc]
    if done:
        return ("play", list(done[0].cards))

    cands = build_candidates(game, pid)
    if len(cands) < 2:
        return ai.choose_action(game, pid, profile,
                                enhanced=True, features=ai.HIGH_FEATURES)

    # 3. PIMC 采样推演
    policy = opts.get("rollout") or _default_rollout()
    n = int(opts.get("n_samples", N_SAMPLES))
    n = max(2, min(n, ROLLOUT_BUDGET // len(cands)))   # 自适应预算
    rng = _state_rng(game, pid)
    means = evaluate(game, pid, cands, n, rng, policy)
    top = max(means)
    finalists = [i for i in range(len(cands)) if means[i] == top]
    # 4. 收益标准统一（阶段四，任务书五.6）：游次奖励并列时以经典启发式
    #    评分决胜（启发式口径贯通 PIMC），启发式未覆盖并列者再按 _tie_key。
    if len(finalists) > 1:
        hk, hcards = ai.choose_action(game, pid, profile,
                                      enhanced=True,
                                      features=ai.HIGH_FEATURES)
        hkey = None if hk == "pass" else \
            tuple(sorted(c.eid for c in hcards or ()))
        pick = None
        for i in finalists:
            kind_i, m_i = cands[i]
            k_i = None if kind_i == "pass" else \
                tuple(sorted(c.eid for c in m_i.cards))
            if k_i == hkey:
                pick = i
                break
        if pick is None:
            pick = min(finalists, key=lambda i: _tie_key(game, pid, cands[i]))
    else:
        pick = finalists[0]
    kind, m = cands[pick]
    if kind == "pass":
        return ("pass", None)
    return ("play", list(m.cards))
