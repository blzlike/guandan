"""规则引擎测试：牌型识别、比较、逢人配、同花顺候选、合法出牌生成。"""
import random

import pytest
from helper import big_joker, club, diamond, heart, small_joker, spade

from cards import Suit
from rules import HandType, beats, can_play, classify, display_order, \
    find_all_straight_flush_candidates, \
    find_straight_flushes, generate_legal_moves

LEVEL = 10  # 本级打 10，红桃10 为逢人配


def types_of(cards, level=LEVEL):
    return {c.type for c in classify(cards, level)}


def one(cards, level=LEVEL):
    res = classify(cards, level)
    assert len(res) >= 1, f"应能识别: {cards}"
    return res[0]


def as_type(cards, t: HandType, level=LEVEL):
    res = [c for c in classify(cards, level) if c.type is t]
    assert res, f"应能识别为 {t}: {cards}"
    return res[0]


# ---------------------------------------------------------------- 基本牌型
def test_single_pair_triple():
    assert one([spade(7)]).type is HandType.SINGLE
    assert one([spade(7), club(7)]).type is HandType.PAIR
    assert one([spade(7), club(7), diamond(7)]).type is HandType.TRIPLE
    assert classify([spade(7), club(8)], LEVEL) == []


def test_level_single_beats_ace():
    lvl = one([spade(LEVEL)])
    ace = one([spade(14)])
    assert lvl.rank == 15 and ace.rank == 14
    assert beats(lvl, ace)


def test_triple_pair_and_straight():
    assert one([club(3), diamond(3), spade(3), heart(4), club(4)]).type is HandType.TRIPLE_PAIR
    st = one([spade(4), club(5), diamond(6), heart(7), club(8)])
    assert st.type is HandType.STRAIGHT and st.rank == 8
    # A 可作 1 或 14
    a_low = one([heart(14), club(2), diamond(3), spade(4), club(5)])
    assert a_low.type is HandType.STRAIGHT and a_low.rank == 5
    a_high = one([spade(10), club(11), diamond(12), heart(13), club(14)])
    assert a_high.type is HandType.STRAIGHT and a_high.rank == 14
    assert beats(a_high, a_low)


# ---------------------------------------------------------------- 出牌展示顺序
def test_display_order_straight_ascending_ace_low():
    out = display_order([club(8), spade(4), diamond(6), heart(7), club(5)], LEVEL)
    assert [c.rank for c in out] == [4, 5, 6, 7, 8]
    a_low = display_order([heart(14), club(2), diamond(3), spade(4), club(5)],
                          LEVEL)
    assert [c.rank for c in a_low] == [14, 2, 3, 4, 5]   # A 记最小位


def test_display_order_wild_at_substituted_position():
    # 打10：5,6,7,8+红桃10 可作 4..8 或 5..9
    w = heart(LEVEL)
    cards = [spade(5), club(6), diamond(7), heart(8), w]
    out = display_order(cards, LEVEL)          # 推定取最大解释 5..9
    assert out[-1] is w
    four_eight = [c for c in classify(cards, LEVEL)
                  if c.type is HandType.STRAIGHT and c.rank == 8]
    assert four_eight, "5678+配应有 4..8 解释"
    out2 = display_order(cards, LEVEL, four_eight[0])
    assert out2[0] is w and [c.rank for c in out2[1:]] == [5, 6, 7, 8]


def test_display_order_pair_chain_ace_low():
    # A A 2 2 3 + 配顶 3：A 记最小位，配排在 3 之后（同位按花色）
    out = display_order([heart(LEVEL), spade(3), spade(14), diamond(14),
                         spade(2), diamond(2)], LEVEL)
    assert [c.rank for c in out] == [14, 14, 2, 2, 3, 10]
    assert [int(c.suit) for c in out[:2]] == [0, 3]   # 黑桃/方块


def test_display_order_triple_pair_and_suit_order():
    out = display_order([diamond(3), club(3), spade(3), heart(4), club(4)],
                        LEVEL)
    assert [c.rank for c in out] == [3, 3, 3, 4, 4]   # 三张在前对子在后
    assert [int(c.suit) for c in out[:3]] == [0, 2, 3]    # 黑/梅/方
    assert [int(c.suit) for c in out[3:]] == [1, 2]        # 红/梅
    bomb = display_order([diamond(7), heart(7), club(7), spade(7)], LEVEL)
    assert [int(c.suit) for c in bomb] == [0, 1, 2, 3]     # 黑红梅方
    rocket = display_order([big_joker(), small_joker(), big_joker(),
                            small_joker()], LEVEL)
    assert [c.rank for c in rocket] == [15, 15, 16, 16]    # 小小大大


def test_level_card_in_straight_uses_natural_rank():
    # 打10：8,9,10,J,Q 顺子顶牌为 Q(12)，10 按原始点数参与
    st = one([spade(8), club(9), diamond(LEVEL), heart(11), club(12)])
    assert st.type is HandType.STRAIGHT and st.rank == 12
    # 该顺子不能压 A2345? 顶牌12 > 5，可以压
    a_low = one([heart(14), club(2), diamond(3), spade(4), club(5)])
    assert beats(st, a_low)


def test_pair_chain_and_triple_chain():
    pc = one([spade(4), club(4), diamond(5), heart(5), spade(6), club(6)])
    assert pc.type is HandType.PAIR_CHAIN and pc.rank == 6
    tc = one([spade(4), club(4), diamond(4), heart(5), spade(5), club(5)])
    assert tc.type is HandType.TRIPLE_CHAIN and tc.rank == 5


def test_bombs_and_rocket():
    for n in (4, 5, 6, 7, 8):
        pool = [spade(9), club(9), diamond(9), heart(9), spade(9), club(9), diamond(9), heart(9)]
        b = one(pool[:n])
        assert b.type is HandType.BOMB and b.size == n
    rocket = one([big_joker(), big_joker(), small_joker(), small_joker()])
    assert rocket.type is HandType.ROCKET


def test_bomb_hierarchy():
    b4 = one([spade(5), club(5), diamond(5), heart(5)])
    b5 = one([spade(4), club(4), diamond(4), heart(4), spade(4)])
    sf = as_type([spade(5), spade(6), spade(7), spade(8), spade(9)], HandType.STRAIGHT_FLUSH)
    b6 = one([spade(3), club(3), diamond(3), heart(3), spade(3), club(3)])
    b7 = one([spade(3), club(3), diamond(3), heart(3), spade(3), club(3), diamond(3)])
    b8 = one([spade(2), club(2), diamond(2), heart(2), spade(2), club(2), diamond(2), heart(2)])
    rk = one([big_joker(), big_joker(), small_joker(), small_joker()])
    chain = [b4, b5, sf, b6, b7, b8, rk]
    for i in range(len(chain) - 1):
        assert beats(chain[i + 1], chain[i]), f"{i}->{i+1}"
        assert not beats(chain[i], chain[i + 1])
    # 普通牌型压不过炸弹
    single = one([big_joker()])
    assert beats(b4, single) and not beats(single, b4)


def test_sf_beats_b5_but_loses_b6_by_tier():
    sf = as_type([spade(5), spade(6), spade(7), spade(8), spade(9)], HandType.STRAIGHT_FLUSH)
    b5 = one([spade(14), club(14), diamond(14), heart(14), spade(14)])
    assert beats(sf, b5) and not beats(b5, sf)


# ---------------------------------------------------------------- 逢人配
def test_wild_pair_triple_bomb():
    wild = heart(LEVEL)
    p = one([wild, spade(7)])
    assert p.type is HandType.PAIR and p.rank == 7 and p.subs == ((wild.eid, 7),)
    t = one([wild, spade(7), club(7)])
    assert t.type is HandType.TRIPLE and t.subs == ((wild.eid, 7),)
    b = one([wild, heart(LEVEL), spade(7), club(7)])
    assert b.type is HandType.BOMB and b.size == 4 and len(b.subs) == 2
    # 两张配 + 一对7 = 4张炸7
    assert all(s[1] == 7 for s in b.subs)


def test_wild_cannot_substitute_joker():
    assert classify([heart(LEVEL), big_joker()], LEVEL) == []


def test_wild_in_straight_records_substitution():
    wild = heart(LEVEL)
    cards = [wild, spade(5), club(6), diamond(7), heart(8)]
    straights = [c for c in classify(cards, LEVEL) if c.type is HandType.STRAIGHT]
    assert len(straights) == 2  # 4-8 或 5-9
    by_top = {c.rank: c for c in straights}
    assert set(by_top) == {8, 9}
    assert (wild.eid, 4) in by_top[8].subs  # 顶牌8 => 配牌替代4
    assert (wild.eid, 9) in by_top[9].subs  # 顶牌9 => 配牌替代9


def test_wild_as_level_card_natural_in_straight():
    wild = heart(LEVEL)  # 红桃10
    cards = [wild, spade(6), club(7), diamond(8), heart(9)]
    res = [c for c in classify(cards, LEVEL) if c.type is HandType.STRAIGHT]
    st = [c for c in res if c.rank == LEVEL]
    assert st and st[0].subs == ()  # 配牌按自然点数10参与，不记替代


def test_two_wilds_pair_each_other():
    res = classify([heart(LEVEL), heart(LEVEL)], LEVEL)
    assert any(c.type is HandType.PAIR and c.rank == 15 for c in res)


# ---------------------------------------------------------------- 同花顺候选
def test_straight_flush_candidates_all_and_unique():
    cards = [spade(5), spade(6), spade(7), spade(8), spade(9), spade(9), club(2)]
    combos = find_straight_flushes(cards, LEVEL)
    # 两张黑桃9 牌形完全等价：只算一个候选组合（不按实体牌区分）
    assert len(combos) == 1
    c = combos[0]
    eids = [x.eid for x in c.cards]
    assert len(set(eids)) == 5  # 实体牌不重复使用
    assert c.type is HandType.STRAIGHT_FLUSH and c.suit is Suit.SPADE


def test_straight_flush_with_wild():
    wild = heart(LEVEL)
    cards = [wild, spade(5), spade(6), spade(7), spade(8)]
    combos = [c for c in find_straight_flushes(cards, LEVEL) if c.suit is Suit.SPADE]
    assert combos
    tops = sorted({c.rank for c in combos})
    assert 9 in tops  # 配牌替代黑桃9
    fill = [c for c in combos if c.rank == 9][0]
    assert (wild.eid, 9) in fill.subs


def test_heart_sf_wild_natural():
    wild = heart(LEVEL)
    cards = [wild, heart(6), heart(7), heart(8), heart(9)]
    combos = [c for c in find_straight_flushes(cards, LEVEL) if c.suit is Suit.HEART]
    assert combos
    top = max(c.rank for c in combos)
    assert top == 10
    c10 = [c for c in combos if c.rank == 10][0]
    assert c10.subs == ()


def test_grouped_candidates_by_suit():
    hand = [spade(5), spade(6), spade(7), spade(8), spade(9),
            diamond(2), diamond(3), diamond(4), diamond(5), diamond(6),
            club(13)]
    grouped = find_all_straight_flush_candidates(hand, LEVEL)
    assert set(grouped) == {Suit.SPADE, Suit.DIAMOND}
    assert grouped[Suit.SPADE][0].rank == 9


# ---------------------------------------------------------------- 合法性校验
def test_can_play_follow_rules():
    prev = one([spade(9), club(9)])  # 对9
    assert can_play([spade(11), club(11)], prev, LEVEL) is not None
    assert can_play([spade(8), club(8)], prev, LEVEL) is None
    assert can_play([spade(9)], prev, LEVEL) is None
    bomb = [spade(3), club(3), diamond(3), heart(3)]
    assert can_play(bomb, prev, LEVEL) is not None
    # 非法组合不改判
    assert can_play([spade(3), club(4)], prev, LEVEL) is None


def test_can_play_leading():
    assert can_play([spade(7)], None, LEVEL) is not None
    assert can_play([spade(7), club(8)], None, LEVEL) is None


def test_generated_moves_all_pass_engine():
    rng = random.Random(42)
    from cards import build_deck
    deck = build_deck()
    for _ in range(30):
        rng.shuffle(deck)
        hand = deck[:12]
        for prev in [None, one([spade(9), club(9)]), one([big_joker()])]:
            for m in generate_legal_moves(hand, prev, LEVEL):
                assert can_play(m.cards, prev, LEVEL) is not None, m.describe()


def test_rocket_only_beats_rocket():
    rk = one([big_joker(), big_joker(), small_joker(), small_joker()])
    b8 = one([spade(2), club(2), diamond(2), heart(2), spade(2), club(2), diamond(2), heart(2)])
    assert beats(rk, b8)
    assert not beats(b8, rk)
