"""无状态概率推断纯函数（阶段四，任务书五.2 / 附录 C.2）。

AI 侧（decision/ai）与培训侧（observer）各自以**自己合法的输入**调用本模块：
- 输入只含「自己手牌 + 本局已出 + 各家剩牌数 + 本局公开动作史」等公开口径数据；
- 全部函数无状态、确定性，不依赖任何 AI 私有对象（B.4.5 守恒约束：
  observer/advisor 严禁 import ai/registry/decision/plan_b/strategy，故本模块
  也不得反向 import 它们——tests 源码检查固化）。

能力：
- unseen_counts / unseen_by_suit_rank  未见分布（verify_unseen 做 108 守恒校验）
- hypergeom_at_least / pool_contains_prob  超几何精确概率
- bomb_prob_any                        炸弹概率（多手并集，生成函数精确 DP）
- window_pattern_prob                  连续窗口牌型概率（顺子/连对/钢板，精确 DP）
- all_present_prob                     指定点集合每点≥1 的精确概率（容斥）
- straight_prob / straight_flush_prob  顺子 / 同花顺（A2345 与跨花色并集为
                                       独立近似，evidence 注明近似口径）
- bomb_prediction / pattern_prediction 快照与面板的预测条目（prob+置信+证据）
- history_events / behaviour_facts     本局动作史归一化 + 行为信号
                                       （首次领出/主动压牌/炸弹使用 → 已知事实；
                                        「不接牌」≠ 无牌：低单过牌仅作弱提示
                                        进 insufficient）
- build_predictions                   B.1 predictions 结构组装

近似口径（预测忽略逢人配替代，按面值统计；evidence/模块注释声明）。
"""
from __future__ import annotations

from math import comb
from typing import Dict, List, Optional, Sequence, Tuple

from cards import BIG_JOKER, RANK_LABELS, SMALL_JOKER

# 108 全副配额：点数 2..A 各 8 张、双王各 2 张（与 cards/ai.CardMemory 同口径）
TOTAL_QUOTA = {**{r: 8 for r in range(2, 15)}, SMALL_JOKER: 2, BIG_JOKER: 2}
_PLAIN_RANKS = tuple(range(2, 15))     # 王不成普通炸弹，炸弹预测只看普通点数
_CONF_HIGH, _CONF_MED = 0.6, 0.3
_PATTERN_MAX_HAND = 10                 # 对手剩牌超过该值，牌型构成无区分度


# ---------------------------------------------------------------- 基础工具

def _c(a: int, b: int) -> int:
    """安全组合数：越界（b<0 或 b>a）按 0 处理。"""
    return comb(a, b) if 0 <= b <= a else 0


def rank_label(rank: int) -> str:
    return RANK_LABELS.get(rank, str(rank))


def confidence_of(prob: float) -> str:
    """概率 → 置信等级（high / medium / low）。"""
    if prob >= _CONF_HIGH:
        return "high"
    if prob >= _CONF_MED:
        return "medium"
    return "low"


# --------------------------------------------------------------- 未见分布

def unseen_counts(mine: Sequence, played: Sequence) -> Dict[int, int]:
    """未见分布：108 配额 − 我手 − 已出（mine/played 为 Card 可迭代）。"""
    seen: Dict[int, int] = {}
    for card in list(mine) + list(played):
        seen[card.rank] = seen.get(card.rank, 0) + 1
    return {r: n - seen.get(r, 0) for r, n in TOTAL_QUOTA.items()}


def unseen_by_suit_rank(mine: Sequence, played: Sequence) -> Dict[int, Dict[int, int]]:
    """花色×点数未见分布（同花顺预测用；不含王）。"""
    seen: Dict[Tuple[int, int], int] = {}
    for card in list(mine) + list(played):
        if not card.is_joker:
            k = (int(card.suit), card.rank)
            seen[k] = seen.get(k, 0) + 1
    out: Dict[int, Dict[int, int]] = {}
    for suit in (0, 1, 2, 3):
        out[suit] = {r: 2 - seen.get((suit, r), 0) for r in _PLAIN_RANKS}
    return out


def verify_unseen(unseen: Dict[int, int], mine_count: int, played_count: int) -> bool:
    """B.4.5 守恒校验：Σunseen == 108 − 自己 − 已出，且各点数非负。"""
    if any(n < 0 for n in unseen.values()):
        return False
    return sum(unseen.values()) == 108 - mine_count - played_count


# --------------------------------------------------------------- 精确概率

def hypergeom_at_least(u: int, n: int, total: int, k: int) -> float:
    """从 total 张中抽 n 张，其中指定的 u 张「目标牌」抽到 ≥k 张的精确概率。"""
    if k > min(u, n) or n <= 0 or total <= 0:
        return 0.0
    top = min(u, n)
    good = sum(_c(u, j) * _c(total - u, n - j) for j in range(k, top + 1))
    denom = _c(total, n)
    return good / denom if denom else 0.0


def pool_contains_prob(u: int, pool: int, total: int) -> float:
    """total 张中标记 u 张，随机取 pool 张（某方合计牌池）含 ≥1 张标记的精确概率。"""
    if u <= 0 or pool <= 0 or total <= 0:
        return 0.0
    denom = _c(total, pool)
    if not denom:
        return 0.0
    return 1.0 - _c(total - u, pool) / denom


def bomb_prob_any(u: int, sizes: Sequence[int], capped: Sequence[bool],
                  total: int) -> float:
    """u 张目标牌随机分入各家（手牌数 sizes），至少一家「受监察」家持 ≥4 的精确概率。

    生成函数精确计算（多元超几何）：capped=True 的家多项式只允许 0..3 张
    （对手：成炸即威胁），False 不设限（队友：持炸无害，不参与并集）。
    P(无受监察家成炸) = [x^u] Π poly_i / C(total, u)。
    """
    if u < 4 or not any(capped) or total <= 0:
        return 0.0
    acc = [1]
    for n_i, cap in zip(sizes, capped):
        hi = min(n_i, 3 if cap else n_i, u)
        row = [_c(n_i, j) for j in range(hi + 1)]
        new = [0] * (u + 1)
        for i, a in enumerate(acc):
            if not a:
                continue
            for j, b in enumerate(row):
                if b and i + j <= u:
                    new[i + j] += a * b
        acc = new
    denom = _c(total, u)
    if not denom:
        return 0.0
    none_bomb = acc[u] if u < len(acc) else 0
    return max(0.0, min(1.0, 1.0 - none_bomb / denom))


def window_pattern_prob(counts: Dict[int, int], n: int, total: int,
                        window: int, threshold: int) -> float:
    """手牌（从 total 张未见中抽 n 张，各点 counts 张可抽，王不在 counts 内）
    含「连续 window 个点、每点 ≥threshold 张」的精确概率（不含 A2345 环窗）。

    精确 DP：状态 (已用张数, 最近 window-1 个点的达标掩码, 是否已命中)，
    每点选 k 张贡献 C(u_r, k) 种取法；王占名额但不参与窗口，卷积修正。
    """
    if n <= 0 or total <= 0 or window <= 0 or threshold <= 0:
        return 0.0
    # 防御：只取普通点数（调用方可能传入含王键 15/16 的完整 unseen 表，
    # 王占名额但不参与窗口，须先剥离再推算 jokers 名额）
    plain = {r: counts.get(r, 0) for r in _PLAIN_RANKS}
    jokers = total - sum(plain.values())
    if jokers < 0:
        return 0.0
    full = (1 << (window - 1)) - 1
    states: Dict[Tuple[int, int, bool], int] = {(0, 0, False): 1}
    for r in _PLAIN_RANKS:
        u_r = plain[r]
        nxt: Dict[Tuple[int, int, bool], int] = {}
        for (used, mask, hit), ways in states.items():
            if ways == 0 or used > n:
                continue
            for k in range(0, min(u_r, n - used) + 1):
                met = 1 if k >= threshold else 0
                new_mask = ((mask << 1) | met) & full
                new_hit = hit or (met and window == 1) or \
                    (met and window > 1 and mask == full)
                key = (used + k, new_mask, new_hit)
                nxt[key] = nxt.get(key, 0) + ways * _c(u_r, k)
        states = nxt
    total_good = 0
    total_all = 0
    for (used, _m, hit), w in states.items():
        fill = _c(jokers, n - used)
        if not fill:
            continue
        total_all += w * fill
        if hit:
            total_good += w * fill
    denom = _c(total, n)
    if not denom:
        return 0.0
    return max(0.0, min(1.0, total_good / denom))


def all_present_prob(us: Sequence[int], n: int, total: int) -> float:
    """n 张手牌中，指定各点（各 us 张未见）每点至少 1 张的精确概率（容斥）。"""
    if n <= 0 or total <= 0 or any(u <= 0 for u in us):
        return 0.0
    denom = _c(total, n)
    if not denom:
        return 0.0
    miss = 0.0
    m = len(us)
    for bits in range(1, 1 << m):
        sub = 0
        cnt = 0
        for i in range(m):
            if bits >> i & 1:
                sub += us[i]
                cnt += 1
        term = _c(total - sub, n) / denom
        miss += term if cnt % 2 else -term
    return max(0.0, min(1.0, 1.0 - miss))


def straight_prob(counts: Dict[int, int], n: int, total: int) -> float:
    """顺子概率：普通连续窗口精确 DP + A2345 环窗（两者独立近似合并）。"""
    p_contig = window_pattern_prob(counts, n, total, 5, 1)
    p_wrap = all_present_prob([counts.get(r, 0) for r in (14, 2, 3, 4, 5)],
                              n, total)
    return 1.0 - (1.0 - p_contig) * (1.0 - p_wrap)


def straight_flush_prob(srank: Dict[int, Dict[int, int]], n: int,
                        total: int) -> float:
    """同花顺概率：按花色条件化精确 + 跨花色并集独立近似。

    P ≈ 1 − Π_s (1 − P_s)；P_s = Σ_m P(手牌含 m 张花色 s) × P(m 张内成顺)。
    """
    if n <= 0 or total <= 0:
        return 0.0
    surv = 1.0
    for _suit, counts in sorted(srank.items()):
        u_s = sum(counts.values())
        if u_s < 5:
            continue
        p_s = 0.0
        for m in range(5, min(u_s, n) + 1):
            pm = _c(u_s, m) * _c(total - u_s, n - m) / _c(total, n)
            if pm <= 0:
                continue
            p_run = window_pattern_prob(counts, m, u_s, 5, 1)
            p_wrap = all_present_prob(
                [counts.get(r, 0) for r in (14, 2, 3, 4, 5)], m, u_s)
            p_run = 1.0 - (1.0 - p_run) * (1.0 - p_wrap)
            p_s += pm * p_run
        surv *= (1.0 - max(0.0, min(1.0, p_s)))
    return 1.0 - surv


# ----------------------------------------------------------- 预测条目构建

def hidden_view(game, pid: int) -> Tuple[Dict[int, int], List[int], List[int],
                                         List[bool], int]:
    """公开口径分解：未见分布 / 隐藏各家手牌数 / 对手标记 / 未见总数。"""
    unseen = unseen_counts(game.hands[pid], game.played_cards)
    hidden = [o for o in game.active_players() if o != pid]
    sizes = [game.hand_count(o) for o in hidden]
    capped = [game.team_of(o) != game.team_of(pid) for o in hidden]
    return unseen, hidden, sizes, capped, sum(unseen.values())


def bomb_prediction(game, pid: int, top: int = 4,
                    min_prob: float = 0.05) -> List[dict]:
    """炸弹预测条目（按概率降序）：{rank, prob, confidence, evidence}。"""
    unseen, _hidden, sizes, capped, total = hidden_view(game, pid)
    n_opp = sum(s for s, c in zip(sizes, capped) if c)
    out: List[dict] = []
    for r in _PLAIN_RANKS:
        u = unseen.get(r, 0)
        if u < 4 or n_opp <= 0:
            continue
        p = bomb_prob_any(u, sizes, capped, total)
        if p < min_prob:
            continue
        out.append({"rank": r, "prob": round(p, 4),
                    "confidence": confidence_of(p),
                    "evidence": f"未见{u}张，对手合计剩{n_opp}张"})
    out.sort(key=lambda d: (-d["prob"], d["rank"]))
    return out[:top]


def pattern_prediction(game, pid: int) -> List[dict]:
    """对手可能牌型预测（剩牌 ≤ _PATTERN_MAX_HAND 才有区分度）。

    返回 [{pid, pattern, prob, confidence}]，每家最多 2 条、prob ≥ 0.10。
    """
    unseen, hidden, sizes, capped, total = hidden_view(game, pid)
    if total <= 0:
        return []
    srank = unseen_by_suit_rank(game.hands[pid], game.played_cards)
    out: List[dict] = []
    for o, size, cap in zip(hidden, sizes, capped):
        if not cap or size <= 0 or size > _PATTERN_MAX_HAND:
            continue
        entries: List[Tuple[float, str]] = [
            (straight_prob(unseen, size, total), "顺子"),
            (window_pattern_prob(unseen, size, total, 3, 2), "连对"),
            (window_pattern_prob(unseen, size, total, 2, 3), "钢板"),
        ]
        if size <= 8:
            entries.append((straight_flush_prob(srank, size, total), "同花顺"))
        entries = [(p, name) for p, name in entries if p >= 0.10]
        entries.sort(key=lambda t: (-t[0], t[1]))
        for p, name in entries[:2]:
            out.append({"pid": o, "pattern": name, "prob": round(p, 4),
                        "confidence": confidence_of(p)})
    return out


# ------------------------------------------------------------- 行为信号

def history_events(game) -> List[dict]:
    """game.round_history → 归一化事件列表（无状态重算，不引入跨决策缓存）。"""
    out: List[dict] = []
    for pid, kind, combo, lead in getattr(game, "round_history", ()) or ():
        if kind == "play":
            out.append({"pid": pid, "kind": "play", "lead": lead,
                        "type": combo.type.value, "rank": combo.rank,
                        "size": len(combo.cards),
                        "bomb": combo.is_bomb_family()})
        else:
            out.append({"pid": pid, "kind": "pass"})
    return out


def _role_label(p: int, pid: int) -> str:
    if p == (pid + 2) % 4:
        return "对家"
    if p % 2 != pid % 2:
        return f"对手P{p}"
    return f"队友P{p}"


def behaviour_facts(events: List[dict],
                    pid: int) -> Tuple[List[str], List[str]]:
    """行为信号 → (known_facts, insufficient)。

    任务书五.2：首次领出牌型、主动压牌、炸弹使用为已知事实；
    「不接牌」不得认定为无牌——低单（≤9）过牌仅作弱提示进 insufficient。
    """
    facts: List[str] = []
    weak: List[str] = []
    seen_weak: set = set()
    first_lead: Dict[int, str] = {}
    bombs_used: Dict[int, List[str]] = {}
    beats: List[Tuple[int, str]] = []
    last_play_ev: Optional[dict] = None
    standing: Optional[dict] = None
    for ev in events:
        if ev["kind"] != "play":
            # 低单过牌弱信号（每人只记一次）
            if standing is not None and standing["pid"] != ev["pid"] \
                    and standing["type"] == "单张" and standing["rank"] <= 9 \
                    and ev["pid"] != pid and ev["pid"] not in seen_weak:
                seen_weak.add(ev["pid"])
                weak.append(f"P{ev['pid']} 在低单{rank_label(standing['rank'])}"
                            f"上过牌（不接牌≠无牌，弱信号）")
            continue
        p = ev["pid"]
        if ev["lead"]:
            first_lead.setdefault(p, ev["type"])
        elif last_play_ev is not None and last_play_ev["type"] == ev["type"] \
                and last_play_ev["pid"] % 2 != p % 2:
            beats.append((p, ev["type"]))
        if ev["bomb"] and p != pid:
            bombs_used.setdefault(p, []).append(rank_label(ev["rank"]))
        last_play_ev = ev
        standing = ev
    for p, t in first_lead.items():
        if p != pid:
            facts.append(f"{_role_label(p, pid)}首次领出{t}")
    for p, ranks in bombs_used.items():
        facts.append(f"{_role_label(p, pid)}已用{'、'.join(ranks)}炸弹")
    for p, t in beats:
        facts.append(f"{_role_label(p, pid)}曾主动压牌（{t}）")
    led = {p for p, _t in first_lead.items()}
    passed = {ev["pid"] for ev in events if ev["kind"] == "pass"}
    for p in sorted(passed - led - {pid}):
        weak.append(f"P{p} 本局尚未领出，牌型信息不足")
    return facts, weak


def build_predictions(game, pid: int) -> dict:
    """组装 B.1 predictions 结构：bombs / patterns / known_facts / insufficient。"""
    bombs = bomb_prediction(game, pid)
    patterns = pattern_prediction(game, pid)
    facts, insufficient = behaviour_facts(history_events(game), pid)
    _unseen, hidden, sizes, capped, _total = hidden_view(game, pid)
    if not patterns and any(size > _PATTERN_MAX_HAND
                            for size, cap in zip(sizes, capped) if cap):
        insufficient.append("对手剩牌较多，牌型构成暂无区分度")
    return {"bombs": bombs, "patterns": patterns,
            "known_facts": facts[:4], "insufficient": insufficient[:4]}
