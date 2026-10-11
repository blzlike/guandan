"""掼蛋规则引擎：牌型识别、大小比较、合法性校验、合法出牌生成、同花顺候选枚举。

所有牌型判断与出牌合法性验证统一由本模块完成，其他模块不得自行实现校验逻辑。

规则参数集中在 :class:`RuleConfig`，不得散落在业务代码中。

牌型体系（V1.0 通用标准规则）：
- 单张 / 对子 / 三同张 / 三带二
- 顺子(5张) / 连对(3对) / 钢板(2组三连)
- 炸弹(4~8张同点) / 同花顺(5张同花色顺子) / 天王炸(4张王)
压制关系：普通牌型只能被同型更大牌或炸弹族压制；
炸弹族内部：4张炸 < 5张炸 < 同花顺 < 6张炸 < 7张炸 < 8张炸 < 天王炸。
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

from cards import BIG_JOKER, SMALL_JOKER, Card, Suit, point_value


class RuleConfig:
    """集中管理的规则参数。"""

    WILD_SUIT: Suit = Suit.HEART          # 逢人配花色（红桃级牌）
    ALLOW_WILD_IN_STRAIGHT_FLUSH: bool = True  # 逢人配允许参与同花顺
    STRAIGHT_LENGTH: int = 5              # 顺子/同花顺张数
    PAIR_CHAIN_PAIRS: int = 3             # 连对组数
    TRIPLE_CHAIN_SETS: int = 2            # 钢板组数
    BOMB_MIN_SIZE: int = 4                # 炸弹最小张数
    BOMB_MAX_SIZE: int = 8                # 炸弹最大张数
    PLAYERS: int = 4
    HAND_SIZE: int = 27                   # 每人起手张数


class HandType(Enum):
    SINGLE = "单张"
    PAIR = "对子"
    TRIPLE = "三同张"
    TRIPLE_PAIR = "三带二"
    STRAIGHT = "顺子"
    PAIR_CHAIN = "连对"
    TRIPLE_CHAIN = "钢板"
    BOMB = "炸弹"
    STRAIGHT_FLUSH = "同花顺"
    ROCKET = "天王炸"


# 炸弹族层级：-1 表示普通牌型
_SF_TIER = 2
_ROCKET_TIER = 6
_BOMB_TIER = {4: 0, 5: 1, 6: 3, 7: 4, 8: 5}


@dataclass(frozen=True)
class Combo:
    """一个待出牌组合。

    - ``rank``：比较用点数。普通牌型/炸弹为 point_value 编码；
      顺子/连对/钢板/同花顺为顶牌原始点数（A2345 顶牌记 5，10JQKA 记 14）。
    - ``subs``：逢人配实际替代记录，元组 (实体牌eid, 替代原始点数)。
    - ``suit``：仅同花顺使用。
    """

    type: HandType
    rank: int
    cards: Tuple[Card, ...]
    subs: Tuple[Tuple[int, int], ...] = ()
    suit: Optional[Suit] = None

    @property
    def size(self) -> int:
        return len(self.cards)

    @property
    def eids(self) -> Tuple[int, ...]:
        return tuple(sorted(c.eid for c in self.cards))

    def tier(self) -> int:
        if self.type is HandType.ROCKET:
            return _ROCKET_TIER
        if self.type is HandType.STRAIGHT_FLUSH:
            return _SF_TIER
        if self.type is HandType.BOMB:
            return _BOMB_TIER.get(self.size, -1)
        return -1

    def is_bomb_family(self) -> bool:
        return self.tier() >= 0

    def describe(self) -> str:
        parts = [c.label() for c in sorted(self.cards, key=lambda c: (-point_value(c.rank, 2), c.eid))]
        txt = " ".join(parts) + f"（{self.type.value}）"
        if self.subs:
            sub_desc = "、".join(f"eid{eid}替代{_rank_name(r)}" for eid, r in self.subs)
            txt += f"[逢人配: {sub_desc}]"
        return txt


def _rank_name(rank: int) -> str:
    from cards import RANK_LABELS
    return RANK_LABELS.get(rank, str(rank))


def split_wilds(cards: Sequence[Card], level_rank: int) -> Tuple[List[Card], List[Card]]:
    """分离逢人配与普通牌。"""
    wilds: List[Card] = []
    plain: List[Card] = []
    for c in cards:
        if c.is_wild(level_rank):
            wilds.append(c)
        else:
            plain.append(c)
    return wilds, plain


def _fill_subs(wilds: Sequence[Card], r: int, level_rank: int) -> Tuple[Tuple[int, int], ...]:
    """记录 wilds 填补点数 r 的替代关系；r 即级牌本身时为自然参与，不记替代。"""
    if r == level_rank:
        return ()
    return tuple((wc.eid, r) for wc in wilds)


def _dedup(combos: List[Combo]) -> List[Combo]:
    seen = set()
    out = []
    for m in combos:
        key = (m.type, m.rank, m.size, m.eids, m.suit)
        if key not in seen:
            seen.add(key)
            out.append(m)
    return out


# ---------------------------------------------------------------------------
# 牌型识别
# ---------------------------------------------------------------------------

def classify(cards: Sequence[Card], level_rank: int) -> List[Combo]:
    """识别一组牌的全部合法牌型解释（含逢人配替代方案）。

    返回空列表表示非法组合。同一组牌可能存在多种解释（如 5,6,7,8+配 可作顺子 4..8 或 5..9）。
    """
    cards = tuple(cards)
    n = len(cards)
    if n == 0:
        return []
    wilds, plain = split_wilds(cards, level_rank)
    w = len(wilds)
    results: List[Combo] = []

    joker_cards = [c for c in plain if c.is_joker]
    non_joker = [c for c in plain if not c.is_joker]
    cnt: Dict[int, List[Card]] = {}
    for c in sorted(non_joker, key=lambda c: c.eid):
        cnt.setdefault(c.rank, []).append(c)

    # 天王炸：4 张王
    if n == 4 and len(joker_cards) == 4 and w == 0:
        ranks = sorted(c.rank for c in joker_cards)
        if ranks == [SMALL_JOKER, SMALL_JOKER, BIG_JOKER, BIG_JOKER]:
            return [Combo(HandType.ROCKET, 100, cards)]
        return []

    if joker_cards:
        # 王只能参与单张或对子（同种两张王），不能进顺子/炸弹/三带二等
        if n == 1:
            return [Combo(HandType.SINGLE, point_value(cards[0].rank, level_rank), cards)]
        if n == 2 and w == 0 and len(joker_cards) == 2 and joker_cards[0].rank == joker_cards[1].rank:
            return [Combo(HandType.PAIR, point_value(joker_cards[0].rank, level_rank), cards)]
        return []

    if n == 1:
        return [Combo(HandType.SINGLE, point_value(cards[0].rank, level_rank), cards)]

    if n == 2:
        if w == 0 and len(cnt) == 1:
            r = next(iter(cnt))
            results.append(Combo(HandType.PAIR, point_value(r, level_rank), cards))
        elif w == 1 and len(plain) == 1:
            r = plain[0].rank
            subs = () if r == level_rank else ((wilds[0].eid, r),)
            results.append(Combo(HandType.PAIR, point_value(r, level_rank), cards, subs))
        elif w == 2:
            results.append(Combo(HandType.PAIR, point_value(level_rank, level_rank), cards))
        return results

    if n == 3:
        # 三同张（王不可）
        if len(cnt) == 1:
            r = next(iter(cnt))
            if len(plain) + w == 3:
                subs = tuple((wc.eid, r) for wc in wilds) if r != level_rank else ()
                results.append(Combo(HandType.TRIPLE, point_value(r, level_rank), cards, subs))
        return results

    if n == 4:
        # 炸弹（4张同点，王不可）
        if len(cnt) == 1:
            r = next(iter(cnt))
            if len(plain) + w == 4:
                results.append(Combo(HandType.BOMB, point_value(r, level_rank), cards,
                                     _fill_subs(wilds, r, level_rank)))
        return results

    if n == 5:
        # 炸弹（5张）
        if len(cnt) == 1 and len(plain) + w == 5:
            r = next(iter(cnt))
            results.append(Combo(HandType.BOMB, point_value(r, level_rank), cards,
                                 _fill_subs(wilds, r, level_rank)))
        # 三带二
        results.extend(_classify_triple_pair(non_joker, cnt, wilds, w, level_rank))
        # 顺子
        results.extend(_classify_straight(cnt, wilds, w, level_rank))
        # 同花顺
        results.extend(_classify_straight_flush(cards, level_rank))
        return _dedup(results)

    if n == 6:
        if len(cnt) == 1 and len(plain) + w == 6:
            r = next(iter(cnt))
            results.append(Combo(HandType.BOMB, point_value(r, level_rank), cards,
                                 _fill_subs(wilds, r, level_rank)))
        results.extend(_classify_pair_chain(cnt, wilds, w, level_rank))
        results.extend(_classify_triple_chain(cnt, wilds, w, level_rank))
        return _dedup(results)

    if n in (7, 8):
        if len(cnt) == 1 and len(plain) + w == n:
            r = next(iter(cnt))
            results.append(Combo(HandType.BOMB, point_value(r, level_rank), cards,
                                 _fill_subs(wilds, r, level_rank)))
        return results

    return []


def _classify_triple_pair(non_joker, cnt, wilds, w, level_rank) -> List[Combo]:
    out = []
    for t, cs in cnt.items():
        used_natural = min(len(cs), 3)
        need = 3 - used_natural
        if need > w:
            continue
        triple_cards = cs[:used_natural] + wilds[:need]
        rest = [c for c in non_joker if c not in triple_cards]
        rest_wilds = wilds[need:]
        if len(rest) + len(rest_wilds) != 2:
            continue
        rc: Dict[int, List[Card]] = {}
        for c in rest:
            rc.setdefault(c.rank, []).append(c)
        pair = None
        subs = [(wc.eid, t) for wc in wilds[:need]]
        if len(rest) == 2 and len(rest_wilds) == 0:
            if len(rc) == 1:
                r = next(iter(rc))
                if r != t:
                    pair = (rest, r, [])
        elif len(rest) == 1 and len(rest_wilds) == 1:
            r = rest[0].rank
            pair = (rest + rest_wilds, r, [(rest_wilds[0].eid, r)])
        elif len(rest) == 0 and len(rest_wilds) == 2:
            pair = (rest_wilds, level_rank, [])
        if pair:
            cards, pr, psubs = pair
            all_subs = tuple(subs + psubs)
            out.append(Combo(HandType.TRIPLE_PAIR, point_value(t, level_rank),
                             tuple(triple_cards) + tuple(cards), all_subs))
    return out


def _classify_straight(cnt, wilds, w, level_rank) -> List[Combo]:
    out = []
    L = RuleConfig.STRAIGHT_LENGTH
    for start in range(1, 14 - L + 2):  # 1..10（A 可作 1 或 14）
        window = list(range(start, start + L))
        picks: List[Card] = []
        missing: List[int] = []
        ok = True
        for r in window:
            nat = 14 if r == 1 else r
            cs = cnt.get(nat, [])
            if len(cs) > 1:
                ok = False
                break
            if len(cs) == 1:
                picks.append(cs[0])
            else:
                missing.append(r)
        if not ok:
            continue
        if len(missing) > w:
            continue
        total_natural = sum(len(v) for v in cnt.values())
        if len(picks) != total_natural:
            continue  # 有牌落在窗口外
        subs = tuple((wilds[i].eid, m) for i, m in enumerate(missing) if m != level_rank)
        cards = tuple(picks) + tuple(wilds[:len(missing)])
        out.append(Combo(HandType.STRAIGHT, window[-1], cards, subs))
    return out


def _classify_pair_chain(cnt, wilds, w, level_rank) -> List[Combo]:
    out = []
    K = RuleConfig.PAIR_CHAIN_PAIRS
    for start in range(1, 14 - K + 2):  # 1..12
        window = list(range(start, start + K))
        picks: List[Card] = []
        need_wild = 0
        ok = True
        for r in window:
            nat = 14 if r == 1 else r
            cs = cnt.get(nat, [])
            if len(cs) > 2:
                ok = False
                break
            picks.extend(cs)
            need_wild += 2 - len(cs)
        if not ok or need_wild > w:
            continue
        if sum(len(v) for v in cnt.values()) != len(picks):
            continue
        # 精确记录每个缺口的点数
        subs = []
        wi = 0
        picks2 = []
        for r in window:
            nat = 14 if r == 1 else r
            cs = cnt.get(nat, [])
            picks2.extend(cs)
            for _ in range(2 - len(cs)):
                picks2.append(wilds[wi])
                if nat != level_rank:
                    subs.append((wilds[wi].eid, nat))
                wi += 1
        out.append(Combo(HandType.PAIR_CHAIN, window[-1], tuple(picks2), tuple(subs)))
    return out


def _classify_triple_chain(cnt, wilds, w, level_rank) -> List[Combo]:
    out = []
    K = RuleConfig.TRIPLE_CHAIN_SETS
    for start in range(1, 14 - K + 2):  # 1..13
        window = list(range(start, start + K))
        total = 0
        ok = True
        for r in window:
            nat = 14 if r == 1 else r
            c = len(cnt.get(nat, []))
            if c > 3:
                ok = False
                break
            total += c
        if not ok or total != sum(len(v) for v in cnt.values()):
            continue
        need = 3 * K - total
        if need > w:
            continue
        subs = []
        picks = []
        wi = 0
        for r in window:
            nat = 14 if r == 1 else r
            cs = cnt.get(nat, [])
            picks.extend(cs)
            for _ in range(3 - len(cs)):
                picks.append(wilds[wi])
                if nat != level_rank:
                    subs.append((wilds[wi].eid, nat))
                wi += 1
        out.append(Combo(HandType.TRIPLE_CHAIN, window[-1], tuple(picks), tuple(subs)))
    return out


def _classify_straight_flush(cards: Tuple[Card, ...], level_rank) -> List[Combo]:
    """在 classify 中识别同花顺：复用枚举函数并筛选实体集合一致的候选。"""
    target = frozenset(c.eid for c in cards)
    out = []
    for combo in find_straight_flushes(cards, level_rank):
        if frozenset(c.eid for c in combo.cards) == target:
            out.append(combo)
    return out


# ---------------------------------------------------------------------------
# 同花顺候选枚举（含逢人配替代记录）
# ---------------------------------------------------------------------------

def find_straight_flushes(cards: Sequence[Card], level_rank: int) -> List[Combo]:
    """枚举给定牌集合中能组成的全部合法同花顺候选。

    保证同一实体牌在单个候选中仅使用一次；默认允许逢人配参与并记录替代点数。
    用于手牌候选维护与 classify 的同花顺解释。
    """
    wilds, plain = split_wilds(cards, level_rank)
    avail_wilds = list(wilds) if RuleConfig.ALLOW_WILD_IN_STRAIGHT_FLUSH else []
    pool: Dict[Suit, Dict[int, List[Card]]] = {}
    for c in plain:
        if c.is_joker:
            continue
        pool.setdefault(c.suit, {}).setdefault(c.rank, []).append(c)

    L = RuleConfig.STRAIGHT_LENGTH
    out: List[Combo] = []
    for suit, ranks in pool.items():
        for start in range(1, 14 - L + 2):
            window = list(range(start, start + L))
            choices: List[Tuple[int, List[Card]]] = []
            missing: List[int] = []
            ok = True
            for r in window:
                nat = 14 if r == 1 else r
                cands = list(ranks.get(nat, []))
                if suit is Suit.HEART and nat == level_rank:
                    cands = cands + avail_wilds  # 红桃级牌本身即逢人配，可自然参与
                if cands:
                    choices.append((r, cands))
                else:
                    missing.append(r)
            if not ok:
                continue
            if len(missing) > len(avail_wilds):
                continue
            # 组合数保护：每个位置候选 <= 2 张实体牌（红桃级牌位含配牌），5 位 <= 32
            for pick in itertools.product(*[c for _, c in choices]):
                used_eids = {c.eid for c in pick}
                if len(used_eids) != len(pick):
                    continue
                remaining_wilds = [wc for wc in avail_wilds if wc.eid not in used_eids]
                if len(remaining_wilds) < len(missing):
                    continue
                subs = []
                chosen = list(pick)
                for i, m in enumerate(missing):
                    wc = remaining_wilds[i]
                    chosen.append(wc)
                    if m != level_rank:
                        subs.append((wc.eid, m))
                combo = Combo(HandType.STRAIGHT_FLUSH, window[-1], tuple(chosen),
                              tuple(subs), suit)
                out.append(combo)
    return _dedup(out)


def find_all_straight_flush_candidates(hand: Sequence[Card], level_rank: int) -> Dict[Suit, List[Combo]]:
    """按花色分组返回手牌中的全部同花顺候选（供 UI 快捷选择区使用）。"""
    grouped: Dict[Suit, List[Combo]] = {}
    for combo in find_straight_flushes(hand, level_rank):
        if combo.suit is None:
            continue
        grouped.setdefault(combo.suit, []).append(combo)
    for s in grouped:
        grouped[s].sort(key=lambda c: (c.rank, c.eids))
    return grouped


# ---------------------------------------------------------------------------
# 大小比较与压制
# ---------------------------------------------------------------------------

def beats(a: Combo, b: Combo) -> bool:
    """a 是否能压过 b。"""
    if a.type is HandType.ROCKET:
        return True
    if b.type is HandType.ROCKET:
        return False
    ta, tb = a.tier(), b.tier()
    if ta >= 0 and tb >= 0:
        if ta != tb:
            return ta > tb
        if a.type is b.type:
            return a.rank > b.rank
        return False
    if ta >= 0:
        return True
    if tb >= 0:
        return False
    return a.type is b.type and a.size == b.size and a.rank > b.rank


def can_play(cards: Sequence[Card], prev: Optional[Combo], level_rank: int) -> Optional[Combo]:
    """统一合法性校验入口。

    返回一个可压制 ``prev`` 的牌型解释；``prev`` 为 None 表示主动出牌，
    返回任一合法解释即可。非法返回 None。
    """
    interpretations = classify(cards, level_rank)
    if not interpretations:
        return None
    if prev is None:
        return interpretations[0]
    for m in interpretations:
        if beats(m, prev):
            return m
    return None


# ---------------------------------------------------------------------------
# 合法出牌生成（AI 与提示功能共用）
# ---------------------------------------------------------------------------

def _hand_index(cards: Sequence[Card], level_rank: int):
    wilds, plain = split_wilds(cards, level_rank)
    non_joker = [c for c in plain if not c.is_joker]
    jokers = [c for c in plain if c.is_joker]
    cnt: Dict[int, List[Card]] = {}
    for c in sorted(non_joker, key=lambda c: c.eid):
        cnt.setdefault(c.rank, []).append(c)
    return wilds, plain, non_joker, jokers, cnt


def enumerate_all_moves(hand: Sequence[Card], level_rank: int) -> List[Combo]:
    """枚举手牌能组成的全部合法出牌组合（不含大小解释歧义，取代表性组合）。"""
    wilds, plain, non_joker, jokers, cnt = _hand_index(hand, level_rank)
    w = wilds
    moves: List[Combo] = []

    # 单张
    seen_pv = set()
    for c in sorted(hand, key=lambda c: point_value(c.rank, level_rank)):
        pv = point_value(c.rank, level_rank)
        if pv not in seen_pv:
            seen_pv.add(pv)
            moves.append(Combo(HandType.SINGLE, pv, (c,)))

    # 对子
    for r, cs in cnt.items():
        pv = point_value(r, level_rank)
        if len(cs) >= 2:
            moves.append(Combo(HandType.PAIR, pv, (cs[0], cs[1])))
        if len(cs) == 1 and len(w) >= 1:
            moves.append(Combo(HandType.PAIR, pv, (cs[0], w[0]),
                              _fill_subs(w[:1], r, level_rank)))
    if len(w) >= 2:
        moves.append(Combo(HandType.PAIR, point_value(level_rank, level_rank), (w[0], w[1])))

    # 三同张
    for r, cs in cnt.items():
        pv = point_value(r, level_rank)
        if len(cs) >= 3:
            moves.append(Combo(HandType.TRIPLE, pv, (cs[0], cs[1], cs[2])))
        elif len(cs) == 2 and len(w) >= 1:
            moves.append(Combo(HandType.TRIPLE, pv, (cs[0], cs[1], w[0]),
                              _fill_subs(w[:1], r, level_rank)))
        elif len(cs) == 1 and len(w) >= 2:
            moves.append(Combo(HandType.TRIPLE, pv, (cs[0], w[0], w[1]),
                              _fill_subs(w[:2], r, level_rank)))

    # 炸弹（同点 + 逢人配扩充）
    for r, cs in cnt.items():
        pv = point_value(r, level_rank)
        for k in range(RuleConfig.BOMB_MIN_SIZE, RuleConfig.BOMB_MAX_SIZE + 1):
            natural = min(len(cs), k)
            need = k - natural
            if natural >= 1 and need <= len(w) and natural + need == k:
                cards = tuple(cs[:natural]) + tuple(w[:need])
                moves.append(Combo(HandType.BOMB, pv, cards,
                                  _fill_subs(w[:need], r, level_rank)))

    # 天王炸
    if len(jokers) == 4:
        moves.append(Combo(HandType.ROCKET, 100, tuple(jokers)))

    # 顺子
    moves.extend(_gen_straights(cnt, w, level_rank))
    # 连对
    moves.extend(_gen_pair_chains(cnt, w, level_rank))
    # 钢板
    moves.extend(_gen_triple_chains(cnt, w, level_rank))
    # 三带二
    moves.extend(_gen_triple_pairs(cnt, w, level_rank))
    # 同花顺
    moves.extend(find_straight_flushes(hand, level_rank))

    return _dedup(moves)


def _gen_straights(cnt, w, level_rank) -> List[Combo]:
    out = []
    L = RuleConfig.STRAIGHT_LENGTH
    for start in range(1, 14 - L + 2):
        window = list(range(start, start + L))
        picks = []
        subs = []
        wi = 0
        ok = True
        for r in window:
            nat = 14 if r == 1 else r
            cs = cnt.get(nat, [])
            if cs:
                picks.append(cs[0])
            else:
                if wi >= len(w):
                    ok = False
                    break
                picks.append(w[wi])
                if nat != level_rank:
                    subs.append((w[wi].eid, nat))
                wi += 1
        if ok:
            out.append(Combo(HandType.STRAIGHT, window[-1], tuple(picks), tuple(subs)))
    return out


def _gen_pair_chains(cnt, w, level_rank) -> List[Combo]:
    out = []
    K = RuleConfig.PAIR_CHAIN_PAIRS
    for start in range(1, 14 - K + 2):
        window = list(range(start, start + K))
        picks = []
        subs = []
        wi = 0
        ok = True
        for r in window:
            nat = 14 if r == 1 else r
            cs = cnt.get(nat, [])[:2]
            picks.extend(cs)
            for _ in range(2 - len(cs)):
                if wi >= len(w):
                    ok = False
                    break
                picks.append(w[wi])
                if nat != level_rank:
                    subs.append((w[wi].eid, nat))
                wi += 1
            if not ok:
                break
        if ok:
            out.append(Combo(HandType.PAIR_CHAIN, window[-1], tuple(picks), tuple(subs)))
    return out


def _gen_triple_chains(cnt, w, level_rank) -> List[Combo]:
    out = []
    K = RuleConfig.TRIPLE_CHAIN_SETS
    for start in range(1, 14 - K + 2):
        window = list(range(start, start + K))
        picks = []
        subs = []
        wi = 0
        ok = True
        for r in window:
            nat = 14 if r == 1 else r
            cs = cnt.get(nat, [])[:3]
            picks.extend(cs)
            for _ in range(3 - len(cs)):
                if wi >= len(w):
                    ok = False
                    break
                picks.append(w[wi])
                if nat != level_rank:
                    subs.append((w[wi].eid, nat))
                wi += 1
            if not ok:
                break
        if ok:
            out.append(Combo(HandType.TRIPLE_CHAIN, window[-1], tuple(picks), tuple(subs)))
    return out


def _gen_triple_pairs(cnt, w, level_rank) -> List[Combo]:
    out = []
    triples = []
    for r, cs in cnt.items():
        if len(cs) >= 3:
            triples.append((r, tuple(cs[:3]), 0))
        elif len(cs) == 2 and len(w) >= 1:
            triples.append((r, tuple(cs) + (w[0],), 1))
        elif len(cs) == 1 and len(w) >= 2:
            triples.append((r, tuple(cs) + (w[0], w[1]), 2))
    pairs = []
    for r, cs in cnt.items():
        if len(cs) >= 2:
            pairs.append((r, tuple(cs[:2]), 0))
        elif len(cs) == 1:
            pairs.append((r, (cs[0],), 1))  # 需 1 张配
    for tr, tcards, tneed in triples:
        for pr, pcards, pneed in pairs:
            if pr == tr:
                continue
            if tneed + pneed > len(w):
                continue
            pair_wilds = tuple(w[tneed: tneed + pneed])
            used = tcards + pcards + pair_wilds
            eids = [c.eid for c in used]
            if len(set(eids)) != len(eids):
                continue
            subs = _fill_subs(w[:tneed], tr, level_rank) + _fill_subs(pair_wilds, pr, level_rank)
            out.append(Combo(HandType.TRIPLE_PAIR, point_value(tr, level_rank),
                             used, subs))
    return out


def move_cost(m: Combo, level_rank: int) -> int:
    """启发式成本：越小越"廉价"，用于提示排序与 AI 选牌。"""
    if m.tier() >= 0:
        return 1000 + m.tier() * 100 + m.rank
    cost = 0
    for c in m.cards:
        if c.is_wild(level_rank):
            continue
        cost += point_value(c.rank, level_rank)
    return cost - len(m.cards) * 2


def generate_legal_moves(hand: Sequence[Card], prev: Optional[Combo],
                         level_rank: int) -> List[Combo]:
    """生成全部合法应对（供 AI/提示使用），按成本升序。"""
    all_moves = enumerate_all_moves(hand, level_rank)
    if prev is None:
        legal = all_moves
    else:
        legal = [m for m in all_moves if beats(m, prev)]
    legal.sort(key=lambda m: (move_cost(m, level_rank), m.size))
    return legal
