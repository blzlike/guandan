"""夯级AI（value 档）测试：步骤① 分解器与牌力因子（控制力底表 v2：
顶级组 1.0 + 点值×0.001 小补偿 + 顶级出完提升继承）+ 步骤② 手数估计/
三因子/终局乘数/choose 与 registry 注册 + 步骤③ 三级记录/校准检测
+ 步骤④ 设置页接入/快照切列。"""
import json
import random
import re
from pathlib import Path

import pytest

from helper import big_joker, club, diamond, heart, small_joker, spade

from cards import BIG_JOKER, SMALL_JOKER, build_deck
from game import GuandanGame
import ai
from rules import HandType, classify
import registry
import value_ai as va

LEVEL = 10  # 本级打 10，红桃10 为逢人配

_BY_EID = {c.eid: c for c in build_deck()}


def E(suit: int, rank: int, deck: int = 0) -> int:
    """实体牌 eid：deck*54 + suit*13 + (rank-2)；王见 build_deck 布局。"""
    return deck * 54 + suit * 13 + (rank - 2)


def _make_state(seed: int, p0_eids=None, counts=(12, 12, 12)):
    """构造 playing 局面：级牌=LEVEL；P0 手牌按 eid 指定（None=保留随机
    27 张），1/2/3 各 counts[i] 张，其余实体牌记已出（unseen 守恒成立）。"""
    g = GuandanGame(random.Random(seed))
    g.levels = {0: LEVEL, 1: LEVEL}
    p0 = list(g.hands[0]) if p0_eids is None \
        else [_BY_EID[e] for e in p0_eids]
    pool = [c for c in _BY_EID.values() if c.eid not in {c.eid for c in p0}]
    rng = random.Random(seed + 7)
    rng.shuffle(pool)
    n1, n2, n3 = counts
    g.hands = {0: p0, 1: pool[:n1], 2: pool[n1:n1 + n2],
               3: pool[n1 + n2:n1 + n2 + n3]}
    g.played_cards = pool[n1 + n2 + n3:]
    g.finish_order = []
    g.last_play = None
    g.phase = "playing"
    g.turn = 0
    return g


def _all_jokers(g):
    pool = list(g.played_cards)
    for h in g.hands.values():
        pool.extend(h)
    return [c for c in pool if c.is_joker]


def types_of(cards, level=LEVEL):
    return [g.type for g in va.decompose(cards, level)]


def fire_groups(cards, level=LEVEL):
    return [g for g in va.decompose(cards, level) if g.is_fire]


def eid_set(groups):
    out = set()
    for g in groups:
        out.update(c.eid for c in g.cards)
    return out


# ---------------------------------------------------------------- 分解器
def test_bomb_isolated():
    """4 张同点独立成炸弹组，其余按单张分解。"""
    hand = [spade(5), heart(5), club(5), diamond(5),
            spade(3), spade(7), spade(9)]
    gs = va.decompose(hand, LEVEL)
    fires = fire_groups(hand)
    assert len(fires) == 1
    assert fires[0].type is HandType.BOMB
    assert fires[0].size == 4
    assert {c.rank for c in fires[0].cards} == {5}
    assert len(gs) == 4  # 炸弹 + 3 单张
    assert all(g.type is HandType.SINGLE for g in gs[1:])


def test_rocket_isolated():
    """4 张王成天王炸，王不参与其他分组。"""
    hand = [big_joker(), small_joker(), big_joker(), small_joker(),
            spade(3), spade(4)]
    gs = va.decompose(hand, LEVEL)
    assert gs[0].type is HandType.ROCKET
    assert gs[0].size == 4
    rest = gs[1:]
    assert all(not any(c.is_joker for c in g.cards) for g in rest)


def test_wild_bomb():
    """3 张同点 + 逢人配（红桃级牌）配成 4 头炸（配火优先）。"""
    hand = [spade(10), club(10), diamond(10), heart(10), spade(3)]
    gs = va.decompose(hand, LEVEL)
    fires = fire_groups(hand)
    assert len(fires) == 1 and fires[0].type is HandType.BOMB
    assert fires[0].size == 4
    assert any(c.is_wild(LEVEL) for c in fires[0].cards)
    assert [g.type for g in gs[1:]] == [HandType.SINGLE]


def test_straight_flush_picked():
    """同花顺优先独立成组，每花色取顶牌最大者。"""
    hand = [spade(6), spade(7), spade(8), spade(9), spade(10),
            heart(3), heart(4)]
    gs = va.decompose(hand, LEVEL)
    fires = fire_groups(hand)
    assert len(fires) == 1 and fires[0].type is HandType.STRAIGHT_FLUSH
    assert fires[0].rank == 10
    assert len(gs) == 3  # 同花顺 + 2 单张


def test_wild_in_straight_when_no_bomb():
    """纯牌力最大口径：逢人配参与杂花顺子后组控制低，级牌单张计权反超
    → 5 单张（v2：级牌 0.015 + 6/7/8/9 各×0.001）。"""
    hand = [heart(10), spade(6), heart(7), spade(8), diamond(9)]
    gs = va.decompose(hand, LEVEL)
    assert len(gs) == 5
    assert all(g.type is HandType.SINGLE for g in gs)
    assert any(g.cards[0].is_wild(LEVEL) for g in gs)  # 逢人配按自然点独立
    d = va.power_of(hand, LEVEL)
    assert d.control == pytest.approx(0.045)   # 级牌 0.015 + 6/7/8/9 拆出


def test_natural_straight_flush_before_wild():
    """自然同花顺优先于 wild 凑顺：♠6-10 + 逢人配 → ♠6-10 同花顺 +
    逢人配单张（wild 不再凑 J 高顺拆散自然同花顺、低张散落）。"""
    hand = [spade(6), spade(7), spade(8), spade(9), spade(10), heart(10)]
    gs = va.decompose(hand, LEVEL)
    fires = fire_groups(hand)
    assert len(fires) == 1 and fires[0].type is HandType.STRAIGHT_FLUSH
    assert {c.rank for c in fires[0].cards} == {6, 7, 8, 9, 10}
    assert not any(c.is_wild(LEVEL) for c in fires[0].cards)
    assert len(gs) == 2  # 同花顺 + 逢人配单张（级牌自然点）
    assert gs[1].type is HandType.SINGLE


def test_max_power_wild_plan_over_greedy():
    """2026-10-21 用户报告：旧配火贪心把逢人配焊给点值最高三张级（KKK$），
    挤掉更大组合。修复后逢人配分配枚举取全局最大牌力：27 张 → 炸弹22222/
    4444/888$ + 同花顺9XJQK + 单3/对66/对XX/单Q/对KK/单A（1.3976 > 旧贪心
    1.0246，同手数 6）。"""
    hand = [spade(2), spade(2), club(2), diamond(2), diamond(2), heart(2),
            club(3),
            spade(4), heart(4), club(4), diamond(4),
            spade(6), heart(6),
            spade(8), club(8), diamond(8),
            heart(9),
            spade(10), heart(10), club(10),
            heart(11), heart(12), club(12),
            spade(13), heart(13), diamond(13), spade(14)]
    gs = va.decompose(hand, 2)  # 级牌=2，红桃2 逢人配
    descs = [g.desc(2) for g in gs]
    assert "炸弹888$" in descs and "同花顺9XJQK" in descs
    assert "炸弹KKK$" not in descs
    assert "三二88866" not in descs and "三二XXXQQ" not in descs
    d = va.power_of(hand, 2)
    assert d.control == pytest.approx(9.783, abs=1e-3)
    assert d.value == pytest.approx(9.783 / 7, abs=1e-3)
    assert d.non_bomb_groups == 6


def test_wild_enlarges_natural_bomb():
    """升头：4 张同点 + 逢人配 → 5 头炸（2.610/2=1.305 胜过 4头+级牌单张
    2.225/3≈0.742）。"""
    hand = [spade(7), heart(7), club(7), diamond(7), heart(10), spade(3)]
    gs = va.decompose(hand, LEVEL)
    fires = fire_groups(hand)
    assert len(fires) == 1 and fires[0].type is HandType.BOMB
    assert fires[0].size == 5
    assert any(c.is_wild(LEVEL) for c in fires[0].cards)
    assert [g.type for g in gs] == [HandType.BOMB, HandType.SINGLE]
    d = va.power_of(hand, LEVEL)
    assert d.value == pytest.approx(2.610 / 2, abs=1e-3)


def test_wild_to_straight_flush_over_match_bomb():
    """反例：配火非最优——三张10 + 红桃JQKA + 逢人配 → 逢人配补同花顺
    10JQKA（2.814+级牌三张1.0，1.272）胜过配火 4头级牌炸（2.5+散张，
    0.426）→ 不成炸弹。"""
    hand = [spade(10), club(10), diamond(10),
            heart(11), heart(12), heart(13), heart(14), heart(10), spade(3)]
    gs = va.decompose(hand, LEVEL)
    fires = fire_groups(hand)
    assert len(fires) == 1 and fires[0].type is HandType.STRAIGHT_FLUSH
    assert any(c.is_wild(LEVEL) for c in fires[0].cards)
    assert fires[0].rank == 14
    assert all(g.type is not HandType.BOMB for g in gs)


def test_two_wilds_split_match_bombs():
    """双逢人配注入式分配：三张10+三张K+双逢人配 → 各配一头（炸弹10$+
    炸弹K$，4.916/2=2.458）胜过集中同级（5头级牌炸 1.239 等）。"""
    hand = [spade(10), club(10), diamond(10),
            spade(13), club(13), diamond(13),
            heart(10), heart(10), spade(3)]
    gs = va.decompose(hand, LEVEL)
    fires = fire_groups(hand)
    assert len(fires) == 2
    assert sorted(f.cards[0].rank for f in fires) == [10, 13]
    assert all(f.size == 4 for f in fires)
    assert [g.type for g in gs] == [HandType.BOMB, HandType.BOMB,
                                    HandType.SINGLE]
    d = va.power_of(hand, LEVEL)
    assert d.value == pytest.approx(4.916 / 2, abs=1e-3)


def test_seq_order_enumeration():
    """连对/顺子顺序枚举取手数少者：两份5-8+单9 → 连对 556677 + 对88 +
    单9（3 组；实测口径，顺子方案会散成更多手）。"""
    hand = [spade(5), heart(5), club(6), diamond(6), club(7), diamond(7),
            club(8), diamond(8), heart(9)]
    gs = va.decompose(hand, LEVEL)
    assert len(gs) == 3
    types = [g.type for g in gs]
    assert types.count(HandType.PAIR_CHAIN) == 1
    assert types.count(HandType.PAIR) == 1
    assert types.count(HandType.SINGLE) == 1


def test_min_hands_steel_chain():
    """333444555666 → 连对445566 + 三张333 + 单4/5/6（v2 牌力值口径：
    连对方案 0.024/6=0.004 优于双钢板 0.010/3≈0.0033）。"""
    hand = [spade(3), club(3), diamond(3), spade(4), club(4), diamond(4),
            spade(5), club(5), diamond(5), spade(6), club(6), diamond(6)]
    gs = va.decompose(hand, LEVEL)
    kinds = [g.type for g in gs]
    assert kinds.count(HandType.PAIR_CHAIN) == 1
    assert kinds.count(HandType.TRIPLE) == 1
    assert kinds.count(HandType.SINGLE) == 3
    assert len(gs) == 5


def test_min_hands_pair_chain_and_triple_pair():
    """33-88 六对 + 999 + 1010（杂花防同花顺）→ 顺子45678×2 + 三带二
    (999+33) + 对级牌（v2 牌力值口径：0.040/5=0.008 优于双连对方案
    0.022/4=0.0055）。"""
    hand = [spade(3), club(3), heart(4), spade(4), club(5), heart(5),
            spade(6), heart(6), club(7), spade(7), heart(8), diamond(8),
            spade(9), club(9), diamond(9), spade(10), club(10)]
    gs = va.decompose(hand, LEVEL)
    kinds = [g.type for g in gs]
    assert kinds.count(HandType.STRAIGHT) == 2
    assert kinds.count(HandType.TRIPLE_PAIR) == 1
    assert kinds.count(HandType.PAIR) == 1
    assert len(gs) == 4
    pair = next(g for g in gs if g.type is HandType.PAIR)
    assert pair.cards[0].rank == LEVEL      # 对级牌


def test_a_low_straight_and_joker_excluded():
    """A 可作 1（A2345 杂花），王不进顺子。"""
    hand = [spade(14), heart(2), spade(3), diamond(4), club(5),
            small_joker()]
    gs = va.decompose(hand, LEVEL)
    assert gs[0].type is HandType.STRAIGHT
    assert gs[0].rank == 5  # A2345 顶牌记 5
    assert gs[1].type is HandType.SINGLE
    assert gs[1].cards[0].rank == SMALL_JOKER


def test_two_same_jokers_pair():
    """同种两张王可成对；异种王各为单张。"""
    hand = [small_joker(), small_joker(), big_joker(), spade(3)]
    gs = va.decompose(hand, LEVEL)
    kinds = [(g.type, g.cards[0].rank) for g in gs]
    assert (HandType.PAIR, SMALL_JOKER) in kinds
    assert (HandType.SINGLE, BIG_JOKER) in kinds


def test_determinism_and_partition():
    """同输入两次分解结果一致；各组无重复且并集=原手牌。"""
    hand = [spade(5), heart(5), club(5), diamond(5), heart(10),
            spade(6), spade(7), spade(8), spade(9), big_joker(),
            spade(3), club(3)]
    g1 = va.decompose(hand, LEVEL)
    g2 = va.decompose(hand, LEVEL)
    assert [g.eids for g in g1] == [g.eids for g in g2]
    eids = eid_set(g1)
    assert len(eids) == len(hand)
    assert eids == {c.eid for c in hand}


def test_empty_hand():
    assert va.decompose([], LEVEL) == []
    d = va.power_of([], LEVEL)
    assert d.control == 0.0 and d.value == 0.0


def test_group_point_str_format():
    """点数串：10→X、逢人配→$、组内按展示序升序（三带二三张在前）；
    天王炸固定名（2026-10-20 用户定口径）。序列类在 v2 牌力值口径下
    分解会拆散，直接构造组验证串。"""
    def _one(cards):
        gs = va.decompose(cards, LEVEL)
        assert len(gs) == 1
        return gs[0]

    assert _one([spade(7), heart(7), club(7),
                 diamond(7)]).point_str(LEVEL) == "7777"
    assert _one([spade(8), heart(8), club(8), spade(2),
                 heart(2)]).point_str(LEVEL) == "88822"
    assert _one([spade(10)]).point_str(LEVEL) == "X"
    assert _one([small_joker()]).point_str(LEVEL) == "小王"
    assert _one([big_joker(), small_joker(), big_joker(),
                 small_joker()]).point_str(LEVEL) == "天王炸"
    # 序列类直接构造（v2 下 45678/444555/334455 分解均按牌力值拆散）
    assert va.Group(HandType.STRAIGHT, 8,
                    (spade(4), heart(5), club(6), diamond(7),
                     spade(8))).point_str(LEVEL) == "45678"
    assert va.Group(HandType.TRIPLE_CHAIN, 5,
                    (spade(4), heart(4), club(4), spade(5), heart(5),
                     club(5))).point_str(LEVEL) == "444555"
    assert va.Group(HandType.PAIR_CHAIN, 5,
                    (spade(3), heart(3), spade(4), heart(4), spade(5),
                     heart(5))).point_str(LEVEL) == "334455"
    # 顺子含 10/逢人配：直接构造组验串
    s10 = va.Group(HandType.STRAIGHT, 10,
                   (spade(6), heart(7), spade(8), diamond(9), spade(10)))
    assert s10.point_str(LEVEL) == "6789X"
    sw = va.Group(HandType.STRAIGHT, 10,
                  (spade(6), heart(7), spade(8), diamond(9), heart(10)))
    assert sw.point_str(LEVEL) == "6789$"


def test_group_desc_format():
    """desc=类型短名+点数串（快照三表统一行格式，2026-10-20 用户定）：
    炸弹7777 / 三二55544 / 顺子45678 / 单张大王；天王炸固定名。序列类
    在 v2 牌力值口径下分解会拆散，直接构造组验证。"""
    def _one(cards):
        gs = va.decompose(cards, LEVEL)
        assert len(gs) == 1
        return gs[0]

    assert _one([spade(7), heart(7), club(7),
                 diamond(7)]).desc(LEVEL) == "炸弹7777"
    assert _one([spade(5), heart(5), club(5),
                 spade(4), heart(4)]).desc(LEVEL) == "三二55544"
    assert va.Group(HandType.STRAIGHT, 8,
                    (spade(4), heart(5), club(6), diamond(7),
                     spade(8))).desc(LEVEL) == "顺子45678"
    assert va.Group(HandType.PAIR_CHAIN, 5,
                    (spade(3), heart(3), spade(4), heart(4), spade(5),
                     heart(5))).desc(LEVEL) == "连对334455"
    assert va.Group(HandType.TRIPLE_CHAIN, 4,
                    (spade(3), heart(3), club(3), spade(4), heart(4),
                     club(4))).desc(LEVEL) == "钢板333444"
    assert _one([spade(9), heart(9), club(9)]).desc(LEVEL) == "三张999"
    assert _one([spade(3), heart(3)]).desc(LEVEL) == "对子33"
    assert _one([big_joker()]).desc(LEVEL) == "单张大王"
    assert _one([small_joker()]).desc(LEVEL) == "单张小王"
    assert _one([big_joker(), small_joker(), big_joker(),
                 small_joker()]).desc(LEVEL) == "天王炸"
    # 同花顺：短名+点数串（10→X）
    hand = [spade(6), spade(7), spade(8), spade(9), spade(10), heart(3)]
    fires = [g for g in va.decompose(hand, LEVEL) if g.is_fire]
    assert fires[0].desc(LEVEL) == "同花顺6789X"
    # 候选动作 desc（_combo_desc）：逢人配按实际替代位显示 $
    combo = classify([heart(10), spade(10), club(10),
                      spade(2), heart(2)], LEVEL)[0]
    d = va._combo_desc(combo, LEVEL)
    assert d.startswith("三二") and "$" in d and d.endswith("22")


# ---------------------------------------------------------------- 牌力
def test_power_rank_step_order():
    """同型四头炸点数级差：档基差 0.4 + 顶点逐点×CTRL_STEP（v2 每点都
    计小补偿，配同点配牌消去干扰）。"""
    quad_a = [spade(14), heart(14), club(14), diamond(14), spade(5)]
    quad_3 = [spade(3), heart(3), club(3), diamond(3), spade(5)]
    pa = va.power_of(quad_a, LEVEL)
    p3 = va.power_of(quad_3, LEVEL)
    assert pa.control > p3.control
    assert pa.control - p3.control == pytest.approx(0.4 + va.CTRL_STEP * 11)


def test_power_formula_bomb_and_weak_singles():
    """牌力 = 控制力 ÷ (非炸弹手数+1)，每组恒 1 手（v2 弱组不再加权）。"""
    hand = [spade(5), heart(5), club(5), diamond(5),
            spade(3), spade(7), spade(9)]
    d = va.power_of(hand, LEVEL)
    # 控制力 = 四炸档基 2.0 + 顶点 5×0.001 + 单张 3/7/9 各×0.001
    assert d.control == pytest.approx(2.024)
    assert d.non_bomb_groups == 3 and d.weak_groups == 3
    assert d.non_bomb_hands == 3.0
    assert d.value == d.control / (d.non_bomb_hands + 1.0)


def test_power_big_card_resources():
    """v2 底表：大王顶级 1.0，小王/级牌/A 按点值×0.001 递增；级牌不弱。"""
    hand = [big_joker(), small_joker(), heart(10), spade(14),
            spade(2), spade(3)]
    d = va.power_of(hand, LEVEL)
    assert d.control_detail["单大王"] == pytest.approx(va.TOP_CTRL)
    assert d.control_detail["单小王"] == pytest.approx(0.016)
    assert d.control_detail["单10"] == pytest.approx(0.015)   # 级牌
    assert d.control_detail["单A"] == pytest.approx(0.014)
    assert d.control_detail["单2"] == pytest.approx(0.002)
    assert d.control_detail["单3"] == pytest.approx(0.003)
    assert d.non_bomb_groups == 6          # 全为单张组
    assert d.weak_groups == 3              # A/2/3 弱（<级牌）；王与级牌强


def test_power_sequence_group_no_card_weight():
    """纯牌力最大口径：K/A 资源拆单张计权反超顺子（v2 顺子控制仅
    顶点×0.001）——10JQKA 拆 5 单张（本级打 7 避级牌）。"""
    hand = [spade(10), heart(11), spade(12), diamond(13), club(14)]
    d = va.power_of(hand, 7)
    assert [g.type for g in d.groups] == [HandType.SINGLE] * 5
    assert d.control == pytest.approx(0.060)   # 10~A 各点×0.001
    assert d.non_bomb_groups == 5 and d.weak_groups == 5
    assert d.value == pytest.approx(0.060 / 6.0)


def test_low_cards_split_over_straight():
    """v2 牌力值口径：低张顺子方案（0.006/2=0.003）劣于 5 单张拆分
    （0.020/6≈0.0033）→ 23456 拆 5 单张（2026-10-20 第二次修订后行为）。"""
    hand = [spade(2), heart(3), spade(4), diamond(5), club(6)]
    gs = va.decompose(hand, LEVEL)
    assert len(gs) == 5
    assert all(g.type is HandType.SINGLE for g in gs)


def test_power_triple_pair_big_cards():
    """KKK+AA 成三带二（贪心恒并）；v2 整组一值：三张顶点×0.001。"""
    hand = [spade(14), club(14), spade(13), club(13), diamond(13)]
    gs = va.decompose(hand, LEVEL)
    assert len(gs) == 1 and gs[0].type is HandType.TRIPLE_PAIR
    d = va.power_of(hand, LEVEL)
    assert d.control_detail["三带二"] == pytest.approx(0.013)
    assert d.non_bomb_groups == 1


def test_v2_top_groups():
    """v2 顶级组 1.0：单/对大王、级牌三张/三带二；级牌四炸 2.5。"""
    assert va.power_of([big_joker()], LEVEL).control == pytest.approx(1.0)
    assert va.power_of([big_joker(), big_joker()],
                       LEVEL).control == pytest.approx(1.0)
    assert va.power_of([spade(10), club(10), diamond(10)],
                       LEVEL).control == pytest.approx(1.0)
    assert va.power_of([spade(10), club(10), diamond(10),
                        spade(3), club(3)],
                       LEVEL).control == pytest.approx(1.0)
    assert va.power_of([spade(10), club(10), diamond(10), heart(10)],
                       LEVEL).control == pytest.approx(va.BOMB_LEVEL_CTRL[4])


def test_v2_promotion_inherits_top():
    """提升规则：更高牌类全部出完 → 组继承顶级 1.0；任一更高类有剩 →
    底表值。played_counts 按原始牌面计数（级牌=面值 10、小王 raw15、
    大王 raw16）。"""
    # 单A：级牌+双王全出完 → 1.0；级牌剩 1 张 → 0.014；静态 → 0.014
    assert va.power_of([spade(14)], LEVEL,
                       {LEVEL: 8, SMALL_JOKER: 2, BIG_JOKER: 2}).control \
        == pytest.approx(1.0)
    assert va.power_of([spade(14)], LEVEL,
                       {LEVEL: 7}).control == pytest.approx(0.014)
    assert va.power_of([spade(14)], LEVEL).control == pytest.approx(0.014)
    # 单小王：大王出完 → 1.0
    assert va.power_of([small_joker()], LEVEL,
                       {BIG_JOKER: 2}).control == pytest.approx(1.0)
    # 单级牌：双王出完 → 1.0
    assert va.power_of([heart(10)], LEVEL,
                       {SMALL_JOKER: 2, BIG_JOKER: 2}).control \
        == pytest.approx(1.0)
    # 对J：Q/K/A/级牌/双王全出完 → 1.0
    assert va.power_of([spade(11), heart(11)], LEVEL,
                       {12: 8, 13: 8, 14: 8, LEVEL: 8,
                        SMALL_JOKER: 2, BIG_JOKER: 2}).control \
        == pytest.approx(1.0)


def test_v2_context_promotion_wiring():
    """context 接线：game.played_counts 提供时快照组清单含提升控制值
    （双王出完 → P0 级牌单张 ctrl=1.0，cur_power=1.0/2）；混合手牌
    每组恒 1 手。"""
    g = _make_state(40, (E(0, 10),))            # P0 只持一张级牌
    g.played_cards = [c for c in _BY_EID.values() if c.is_joker]
    ctx = va.context_of(g, 0)
    assert ctx["cur_groups"][0]["ctrl"] == pytest.approx(1.0)
    assert ctx["cur_power"] == pytest.approx(0.5)
    # 每组恒 1 手：炸弹+单3+对4 → 非火 2 组=2.0 手
    hand = [spade(5), heart(5), club(5), diamond(5), spade(3),
            heart(4), heart(4)]
    d = va.power_of(hand, LEVEL)
    assert len(d.groups) == 3 and d.non_bomb_groups == 2
    assert d.non_bomb_hands == 2.0


def test_power_fire_hierarchy():
    """火牌基准单调：4头炸 < 5头炸 < 同花顺 < 天王炸；档基/顶点补偿生效。"""
    b4 = [spade(5), heart(5), club(5), diamond(5)]
    b5 = [spade(5), heart(5), club(5), diamond(5), club(5)]
    sf = [spade(6), spade(7), spade(8), spade(9), spade(10)]
    rocket = [big_joker(), small_joker(), big_joker(), small_joker()]
    c4 = va.power_of(b4, LEVEL).control
    c5 = va.power_of(b5, LEVEL).control
    csf = va.power_of(sf, LEVEL).control
    crocket = va.power_of(rocket, LEVEL).control
    assert c4 < c5 < csf < crocket
    assert c4 == pytest.approx(va.BOMB_TIERS[0][0] + va.CTRL_STEP * 5)
    assert c5 == pytest.approx(va.BOMB_BASE[5] + va.CTRL_STEP * 5)
    assert csf == pytest.approx(va.SF_BASE_CTRL + va.CTRL_STEP * 10)
    assert crocket == pytest.approx(va.ROCKET_CTRL)


# ==================================================================== 步骤②
# ---------------------------------------------------------------- 手数估计
def test_estimate_hands_three_phases():
    """开局(>20)→None；中局=剩张÷2.2；残局=分解手数（每组恒 1 手）。"""
    g = _make_state(1)                           # P0 全 27 张 → 开局
    assert va.estimate_hands(g, 0) is None
    g2 = _make_state(2, tuple(range(15)))        # 15 张 → 中局
    assert va.estimate_hands(g2, 0) == pytest.approx(15 / va.MID_DIVISOR)
    # 残局：炸弹rank3 + 单4 + 单5 → 3 组（无历史修正=×1）
    g3 = _make_state(3, (E(0, 3), E(1, 3), E(2, 3), E(3, 3), E(0, 4), E(0, 5)))
    assert g3.hand_count(0) == 6
    assert va.estimate_hands(g3, 0) == pytest.approx(3.0)


def test_estimate_endgame_recent_correction():
    """残局近期净接牌下调（×0.8）、净过牌上调（×1.2）。"""
    g = _make_state(4, counts=(12, 12, 2))
    g.hands[3] = [_BY_EID[E(0, 10)], _BY_EID[E(1, 10)]]   # 对10（强组，基手数1）
    assert va.estimate_hands(g, 3) == pytest.approx(1.0)
    g.round_history = [(3, "play", None, True), (3, "play", None, False),
                       (3, "play", None, False)]
    assert va.estimate_hands(g, 3) == pytest.approx(0.8)
    g.round_history = [(3, "pass", None, False)] * 3
    assert va.estimate_hands(g, 3) == pytest.approx(1.2)


# ---------------------------------------------------------------- 接牌率
def test_accept_rate_calculation():
    """接牌率=非领出机会中实际出牌比例；无历史=0.5 中性。"""
    g = _make_state(5)
    assert va.accept_rate(g, 2) == pytest.approx(0.5)
    # P2 三次机会：接、过、接 → 2/3 > 0.6
    g.round_history = [
        (1, "play", None, True),    # P1 领出
        (2, "play", None, False),   # P2 接（机会1）
        (0, "pass", None, False),
        (3, "pass", None, False),
        (2, "play", None, True),    # P2 领出（非机会）
        (3, "play", None, False),   # P3 接
        (0, "pass", None, False),
        (1, "pass", None, False),
        (3, "play", None, True),    # P3 领出
        (0, "pass", None, False),
        (1, "pass", None, False),
        (2, "pass", None, False),   # P2 过（机会2）
        (3, "play", None, True),    # P3 领出
        (2, "play", None, False),   # P2 接（机会3）
        (0, "pass", None, False),
        (1, "pass", None, False),
    ]
    assert va.accept_rate(g, 2) == pytest.approx(2 / 3)


# ---------------------------------------------------------------- 三因子
def test_pressure_factor_rocket_vs_single():
    """压制：单张可压 → >1；天王炸不可压 → =1.0；开局对手不参与。"""
    g = _make_state(6, (E(0, 3), E(1, 3), E(2, 3), E(3, 3), E(0, 4), E(0, 5)))
    c4 = classify([_BY_EID[E(0, 4)]], LEVEL)[0]
    assert va.pressure_factor(g, 0, c4) > 1.0
    rocket = classify(_all_jokers(g), LEVEL)[0]
    assert rocket.type is HandType.ROCKET
    assert va.pressure_factor(g, 0, rocket) == pytest.approx(1.0)
    # 开局：对手 27 张（估计 None）不参与 → 单张压制=1.0
    g2 = _make_state(7, (E(0, 4),), counts=(27, 27, 26))
    assert va.pressure_factor(g2, 0, c4) == pytest.approx(1.0)


def test_partner_gain_requires_feedinable():
    """对家增益：无历史（0.5 中性）→ =1；可送+能接 → >1。"""
    g = _make_state(8, (E(0, 4), E(0, 5)))
    c4 = classify([_BY_EID[E(0, 4)]], LEVEL)[0]
    assert va.partner_gain(g, 0, c4) == pytest.approx(1.0)
    g.round_history = [(1, "play", None, True), (2, "play", None, False),
                       (0, "pass", None, False), (3, "pass", None, False)]
    assert va.accept_rate(g, 2) > va.ACCEPT_RATE
    assert va.partner_gain(g, 0, c4) > 1.0


# ---------------------------------------------------------------- 终局乘数
def test_endgame_multiplier_finish():
    """整手恰为一组牌且一次出完 → ×5.0。"""
    g = _make_state(9, (E(0, 3), E(1, 4), E(2, 5), E(3, 6), E(0, 7)))
    combo = next(c for c in classify(g.hands[0], LEVEL)
                 if c.type is HandType.STRAIGHT)
    assert va.endgame_multiplier(g, 0, combo) == pytest.approx(va.ENDGAME_FINISH)


def test_endgame_multiplier_block():
    """对手剩 1 手（单K）：本牌其压不住 → ×3；压得住 → ×1。"""
    g = _make_state(10, (E(0, 14), E(0, 4)), counts=(1, 12, 12))
    g.hands[1] = [_BY_EID[E(0, 13)]]                       # 单K（剩 1 手）
    ca = classify([_BY_EID[E(0, 14)]], LEVEL)[0]           # 单A（单K压不住）
    c4 = classify([_BY_EID[E(0, 4)]], LEVEL)[0]            # 单4（单K压得住）
    assert va.endgame_multiplier(g, 0, ca) == pytest.approx(va.ENDGAME_BLOCK)
    assert va.endgame_multiplier(g, 0, c4) == 1.0


def test_endgame_multiplier_bomb():
    """对手 ≤2 手且动作为炸弹 → ×2；非炸弹动作 → ×1。"""
    g = _make_state(11, (E(0, 3), E(1, 3), E(2, 3), E(3, 3), E(0, 4)),
                    counts=(3, 12, 12))
    g.hands[1] = [_BY_EID[E(0, 10)], _BY_EID[E(1, 10)], _BY_EID[E(0, 5)]]
    bomb = classify([_BY_EID[E(0, 3)], _BY_EID[E(1, 3)],
                     _BY_EID[E(2, 3)], _BY_EID[E(3, 3)]], LEVEL)[0]
    c4 = classify([_BY_EID[E(0, 4)]], LEVEL)[0]
    assert va.endgame_multiplier(g, 0, bomb) == pytest.approx(va.ENDGAME_BOMB)
    assert va.endgame_multiplier(g, 0, c4) == 1.0


def test_endgame_multiplier_feed():
    """送对家冲刺：对家残局 1 组 + 可送 + 能接 → ×2.5；无历史 → ×1。"""
    g = _make_state(12, (E(0, 4), E(0, 5)), counts=(12, 2, 12))
    g.hands[2] = [_BY_EID[E(0, 10)], _BY_EID[E(1, 10)]]   # 对家=对10
    c4 = classify([_BY_EID[E(0, 4)]], LEVEL)[0]
    assert va.endgame_multiplier(g, 0, c4) == 1.0          # 无历史不可送
    g.round_history = [(1, "play", None, True), (2, "play", None, False),
                       (0, "pass", None, False), (3, "pass", None, False)]
    assert va.endgame_multiplier(g, 0, c4) == pytest.approx(va.ENDGAME_FEED)


# ---------------------------------------------------------------- 决策入口
def test_choose_prefers_finish_and_detail():
    """能一手出完时选整手；detail 结构合法（唯一 selected、值降序）。"""
    g = _make_state(13, (E(0, 3), E(1, 4), E(2, 5), E(3, 6), E(0, 7)))
    kind, cards, detail = va.choose_detail(g, 0)
    assert kind == "play"
    assert sorted(c.eid for c in cards) == sorted(c.eid for c in g.hands[0])
    cands = detail["candidates"]
    assert detail["n_candidates"] == len(cands)
    assert sum(1 for e in cands if e["selected"]) == 1
    assert cands[0]["selected"] and cands[0]["type"] == HandType.STRAIGHT.value
    vals = [e["value_total"] for e in cands]
    assert vals == sorted(vals, reverse=True)
    assert all({"power", "pressure", "partner", "endgame"} ==
               set(e["score_parts"]) for e in cands)
    assert detail["cost"] == {"search_depth": 0, "n_rollouts": 0}


def test_choose_dedupe_same_type():
    """同型去重保最优：两花色同顶同花顺只保留成本序首个，键无重复。"""
    from rules import generate_legal_moves
    g = _make_state(14, tuple(E(s, r) for s in (0, 1) for r in range(3, 8)))
    moves = generate_legal_moves(g.hands[0], None, LEVEL)
    raw_sf = [m for m in moves if m.type is HandType.STRAIGHT_FLUSH]
    assert len(raw_sf) >= 2                      # 两花色同花顺同顶
    cands = va.build_candidates(g, 0)
    kept_sf = [m for k, m in cands
               if k == "play" and m.type is HandType.STRAIGHT_FLUSH]
    assert len(kept_sf) == 1                     # 同型只保留成本序首个
    assert kept_sf[0].eids == raw_sf[0].eids
    keys = [(m.type, m.rank) for k, m in cands if k == "play"]
    assert len(keys) == len(set(keys))
    assert len(keys) <= va.MAX_CANDIDATES


def test_choose_follow_pass_keeps_pair():
    """跟牌：零散大单接牌带走自身控制值，不如过牌保留整手牌力
    （v2 牌力因子自动体现接牌成本：单K+单3+单5 跟单Q → pass 胜）。"""
    g = _make_state(15, (E(0, 13), E(0, 3), E(0, 5)))
    g.last_play = (3, classify([_BY_EID[E(0, 12)]], LEVEL)[0])   # 对手领单Q
    g.turn = 0
    kind, cards, detail = va.choose_detail(g, 0)
    assert kind == "pass" and cards is None
    kinds = {e["kind"] for e in detail["candidates"]}
    assert "pass" in kinds
    sel = next(e for e in detail["candidates"] if e["selected"])
    assert sel["kind"] == "pass"
    # 过牌牌力=当前整手（单K+单3+单5），高于拆出单K的出牌后牌力
    pw_now = va.power_of(g.hands[0], LEVEL).value
    assert sel["score_parts"]["power"] == pytest.approx(pw_now)
    # 出牌后清单：过牌=出牌前同清单
    ctx = detail["context"]
    assert ctx["finished"] is False
    assert ctx["after_groups"] == ctx["cur_groups"]


def test_after_context_partial_play():
    """出牌后清单：部分出牌=剩余手牌按最大牌力分解（领出对3 保对K 牌力，
    after_groups=[KK]）。"""
    g = _make_state(22, (E(0, 13), E(1, 13), E(0, 3), E(1, 3)))
    _kind, _cards, d = va.choose_detail(g, 0)
    sel = next(e for e in d["candidates"] if e["selected"])
    assert sel["kind"] == "play"
    assert sorted(_BY_EID[e].rank for e in sel["eids"]) == [3, 3]
    ctx = d["context"]
    assert ctx["finished"] is False
    assert [c["cards"] for c in ctx["after_groups"]] == ["KK"]


def test_registry_value_tier_and_determinism():
    """registry 注册 value 档；步骤④起进设置页（resolve_seat 直返 value）；
    同局面重复决策逐字节一致。"""
    spec = registry.get("value")
    assert spec is not None and spec.name == "夯级"
    assert "value" in registry.ids()
    g = _make_state(16, (E(0, 4), E(0, 5)))
    k1, c1 = registry.choose(g, 0, None, "value")
    k2, c2 = va.choose(g, 0)
    assert k1 == k2 == "play"
    assert [x.eid for x in c1] == [x.eid for x in c2]
    _k, _c, d = registry.choose_detail(g, 0, None, "value")
    assert d["n_candidates"] == len(d["candidates"])
    assert sum(1 for e in d["candidates"] if e["selected"]) == 1
    assert registry.resolve_seat({"ai_strength": "value"}, 0) == "value"
    k4, c4 = va.choose(g, 0)
    assert (k4, [x.eid for x in c4]) == (k1, [x.eid for x in c1])


# ==================================================================== 步骤③
# ---------------------------------------------------------------- 三级记录
def test_candidates_record_keys():
    """候选记录键位：score_total 别名 + power 牌力明细（JSON 安全）。"""
    g = _make_state(20, (E(0, 3), E(1, 4), E(2, 5), E(3, 6), E(0, 7)))
    _k, _c, detail = va.choose_detail(g, 0)
    sel = next(e for e in detail["candidates"] if e["selected"])
    assert sel["score_total"] == sel["value_total"]
    assert set(sel["power"]) == {"control", "control_detail",
                                 "non_bomb_hands", "non_bomb_groups",
                                 "weak_groups", "value"}
    # 整手出完：power=整手牌力（v2 分解=5 单张 0.025/6，非 FLOOR 下限）
    assert sel["power"]["non_bomb_groups"] == 5
    assert sel["power"]["value"] == pytest.approx(0.025 / 6.0, abs=1e-6)
    assert sel["score_parts"]["power"] == pytest.approx(0.025 / 6.0, abs=1e-6)
    # 出牌后清单：整手出完 → 空清单 + finished（面板第三列表数据源）
    ctx = detail["context"]
    assert ctx["finished"] is True
    assert ctx["after_groups"] == []
    assert ctx["after_power"] == 0.0 and ctx["after_control"] == 0.0
    json.dumps(detail["candidates"])       # 可序列化（capture 落盘前提）


def test_value_snapshot_capture(tmp_path):
    """三级记录：value 决策快照落 DecisionStore（final.score_total=所选
    胜利值；sid=value 特性集为空）。"""
    from decision import DecisionLogger, DecisionStore
    g = _make_state(21, (E(0, 3), E(1, 4), E(2, 5), E(3, 6), E(0, 7)))
    logger = DecisionLogger(seed=2101, directory=str(tmp_path))
    kind, cards, detail = va.choose_detail(g, 0)
    did = logger.capture(g, pid=0, sid="value", kind=kind, cards=cards,
                         reason="测试理由", action_index=3,
                         candidates=detail["candidates"],
                         n_candidates=detail["n_candidates"],
                         context=detail["context"])
    assert did
    snap = DecisionStore(2101, str(tmp_path)).find(did)
    assert snap["sid"] == "value" and snap["features"] == []
    sel = next(c for c in snap["candidates"] if c["selected"])
    assert snap["final"]["score_total"] == sel["value_total"]
    assert snap["final"]["reason"] == "测试理由"
    assert snap["final"]["eids"] == sorted(c.eid for c in cards)
    assert set(sel["power"]) >= {"control", "value"}
    # cur_power 快照候选侧 round6 与 context 侧 round4（同源近似容差）
    assert abs(sel["cur_power"] - snap["context"]["cur_power"]) < 1e-4


def test_value_ai_import_boundary():
    """value_ai（AI 侧）不得 import review/calib（校核层单向依赖）。"""
    src = Path(va.__file__).read_text(encoding="utf-8")
    assert re.search(r"^\s*(?:import|from)\s+(review|calib)\b", src,
                     re.M) is None


# ==================================================================== 步骤④
# ---------------------------------------------------------------- 快照切列
def test_candidates_table_value_columns():
    """format_candidates_table 按 sid 切列：value 档 6 列（四因子+胜利值，
    基本参数走面板左列两表 context.cur_groups），经典档 9 列七分项不变。"""
    from decision import format_candidates_table

    def _sel_entry(cands):
        return next(c for c in cands if c["selected"])

    g = _make_state(30, (E(0, 3), E(1, 4), E(2, 5), E(3, 6), E(0, 7)))
    _k, _c, detail = va.choose_detail(g, 0)
    snap_v = {"sid": "value", "candidates": detail["candidates"]}
    rows_v = format_candidates_table(snap_v)
    assert rows_v[0] == ["候选", "牌力", "压制", "增益", "终局", "胜利值"]
    assert len(rows_v) == 1 + len(detail["candidates"])
    sel = _sel_entry(detail["candidates"])
    row_sel = next(r for r in rows_v[1:] if r[0].startswith("▶"))
    assert row_sel[1] == f"{sel['score_parts']['power']:.2f}"
    assert row_sel[2] == f"{sel['score_parts']['pressure']:.2f}"
    assert row_sel[3] == f"{sel['score_parts']['partner']:.2f}"
    assert row_sel[4] == f"{sel['score_parts']['endgame']:.2f}"
    assert row_sel[5] == f"{sel['score_total']:.2f}"
    # 候选首列=desc（类型短名+点数串，与手牌清单行同格式）
    assert row_sel[0] == "▶" + sel["desc"]
    assert all(isinstance(c.get("desc"), str) and c["desc"]
               for c in detail["candidates"])
    # 旧档候选无 desc：回退旧格式（类型+关键点，多张 ×n）
    snap_o = {"sid": "value", "candidates": [
        {"kind": "play", "type": "炸弹", "rank": 5, "eids": [1, 2, 3, 4],
         "selected": False, "score_total": 1.0,
         "score_parts": {k: 0.0 for k in ai.SCORE_PARTS}}]}
    assert format_candidates_table(snap_o)[1][0] == "炸弹5×4"
    # context 局面参数：当前牌力/总控制/非炸手数 + 分解组清单（左列两表
    # 数据源）+ 对手/对家估计手数/可送
    ctx = detail["context"]
    assert isinstance(ctx["cur_power"], float)
    assert isinstance(ctx["cur_control"], float)
    assert isinstance(ctx["cur_hands"], float)
    assert ctx["cur_groups"] and all(
        set(c) >= {"label", "cards", "desc", "n", "ctrl", "fire", "weak"}
        for c in ctx["cur_groups"])
    assert {c["desc"] for c in ctx["cur_groups"]} == {
        "单张3", "单张4", "单张5", "单张6", "单张7"}
    # 出牌后清单键（实际所选动作的剩余手牌；本例整手出完=空清单+finished）
    assert ctx["finished"] is True and ctx["after_groups"] == []
    assert all(isinstance(e, float) and e > 0 for e in ctx["opp_ests"])
    assert ctx["mate_est"] is None or isinstance(ctx["mate_est"], float)
    assert ctx["mate_accept"] is None or isinstance(ctx["mate_accept"], bool)
    # 控制构成合计与总控制一致（面板左表数值口径）
    assert abs(sum(c["ctrl"] for c in ctx["cur_groups"])
               - ctx["cur_control"]) < 1e-6
    # 经典档（缺省 sid 亦同）：9 列七分项口径不变
    snap_c = {"sid": "classic_high",
              "candidates": [{"kind": "pass", "selected": True,
                              "score_total": -1.5,
                              "score_parts": {k: 0.0
                                              for k in ai.SCORE_PARTS}}]}
    rows_c = format_candidates_table(snap_c)
    assert rows_c[0] == ["候选", "总分", "走牌", "协同", "压制", "牌权",
                         "成本", "风险", "终局"]
    assert rows_c[1] == ["▶过牌", "-1.5"] + ["0.0"] * 7
    rows_d = format_candidates_table({"candidates": []})
    assert rows_d[0] == ["候选", "总分", "走牌", "协同", "压制", "牌权",
                         "成本", "风险", "终局"]


# ---------------------------------------------------------------- 校准检测
def _fire_combo(kind: str):
    """合成火牌组合（仅作 round_history 统计口径用）。"""
    if kind == "four":
        return classify([spade(3), heart(3), club(3), diamond(3)], LEVEL)[0]
    if kind == "five":
        return classify([spade(3), heart(3), club(3), diamond(3),
                         _BY_EID[E(0, 3, 1)]], LEVEL)[0]
    if kind == "sf":
        return next(c for c in
                    classify([spade(6), spade(7), spade(8), spade(9),
                              spade(10)], LEVEL)
                    if c.type is HandType.STRAIGHT_FLUSH)
    return classify([big_joker(), small_joker(), big_joker(), small_joker()],
                    LEVEL)[0]


def test_observed_stats_counting():
    """实测统计：手数逐 play 计数（过牌不计）；火牌四头/五头/同花顺/
    天王炸分档；finish_order/auto_last 随局终状态。"""
    import calib
    g = _make_state(30)
    g.round_history = [
        (0, "play", None, True), (0, "pass", None, False),
        (1, "play", _fire_combo("four"), False),
        (2, "play", _fire_combo("five"), False),
        (3, "play", _fire_combo("sf"), False),
        (3, "play", _fire_combo("rocket"), False),
    ]
    st = calib.observed_stats(g)
    assert st["hands"] == {0: 1, 1: 1, 2: 1, 3: 2}
    assert st["fire"] == {"四头炸": 1, "五头炸": 1, "同花顺": 1, "天王炸": 1}
    assert st["finish_order"] == list(g.finish_order)


def test_find_deviations_band_and_auto_last():
    """±25% 带内不标记；auto_last 手数不对照；末游位无常量。"""
    import calib
    g = _make_state(31)
    g.finish_order = [0, 2, 1, 3]
    g.round_result = {"auto_last": [1]}       # 三游 P1 被记末游：不对照
    hist = [(0, "play", None, False) for _ in range(11)]   # 头游 +14.6%
    hist += [(2, "play", None, False) for _ in range(11)]  # 二游 +3.8%
    hist += [(1, "play", None, False) for _ in range(5)]
    hist += [(3, "play", _fire_combo("four"), False) for _ in range(6)]
    hist.append((3, "play", _fire_combo("five"), False))    # +11%
    hist += [(3, "play", _fire_combo("sf"), False) for _ in range(2)]
    g.round_history = hist
    assert calib.find_deviations(calib.observed_stats(g)) == []


def test_calibration_marks_reviews(tmp_path):
    """偏离 >±25% → 逐项落 review（挂靠最近夯级快照）；无夯级快照的
    局不标记。"""
    import calib
    from decision import DecisionLogger
    logger = DecisionLogger(seed=32, directory=str(tmp_path))
    logger.store.append({"decision_id": "seed=32|r=1|a=9", "round": 1,
                         "sid": "value"})
    reviews = calib.ReviewStore(str(tmp_path / "reviews.jsonl"))
    checker = calib.CalibrationChecker(seed=32, store=logger.store,
                                       reviews=reviews)
    g = _make_state(32)
    g.finish_order = [0, 2, 1, 3]
    g.round_result = {"auto_last": [3]}
    hist = [(0, "play", None, False) for _ in range(14)]   # 头游 +46%
    hist += [(2, "play", None, False) for _ in range(12)]  # 二游 +13% 不标
    hist += [(1, "play", None, False) for _ in range(11)]  # 三游 -1% 不标
    g.round_history = hist                                 # 火牌全 0 → 全标
    devs = checker.check(g)
    labels = [(d["kind"], d["label"]) for d in devs]
    assert ("hands", "头游手数") in labels
    assert {("fire", "四头炸"), ("fire", "五头炸"),
            ("fire", "同花顺")} <= set(labels)
    recs = reviews.load()
    assert len(recs) == len(devs)
    assert all(r["decision_id"] == "seed=32|r=1|a=9" for r in recs)
    hands_rec = next(r for r in recs if r["category"] == "局势")
    assert hands_rec["verdict"] == "improvable"
    assert "头游手数" in hands_rec["note"]
    assert hands_rec["disposition"] == "open"
    # 换局（round 2 无夯级快照）→ 不标记不落档
    g2 = _make_state(33)
    g2.round_number = 2
    g2.finish_order = [0, 2, 1, 3]
    g2.round_result = {"auto_last": [3]}
    g2.round_history = [(0, "play", None, False) for _ in range(14)]
    assert checker.check(g2) == []
    assert len(reviews.load()) == len(recs)


def test_calibration_attach_guard():
    """非比赛起点（已有出牌）attach 返回 False；round_end 之外不动作。"""
    import calib
    g = _make_state(34, (E(0, 3),))            # played_cards 非空 = 非起点
    ck = calib.CalibrationChecker(seed=1)
    assert ck.attach(g) is False
    g2 = _make_state(35)
    g2.played_cards = []                       # 构造态清空 → 视为比赛起点
    ck2 = calib.CalibrationChecker(seed=2)
    assert ck2.attach(g2) is True
    ck2._on_event(g2, "play", {})              # 非 round_end 静默
    ck2.detach(g2)
    assert calib.CalibrationChecker(seed=3)._fn is None
