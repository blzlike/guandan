"""理牌/选牌/同花顺候选轮换测试，重点验证真实手牌数量不被展示操作改变。"""
import random

from arrange import HandManager
from cards import Suit
from game import GuandanGame
from rules import HandType


def _mgr(seed=1):
    g = GuandanGame(rng=random.Random(seed))
    return g, HandManager(g, pid=0)


def test_invariant_after_operations():
    g, m = _mgr(2)
    n0 = len(m.real_hand)
    eids = m.normal_eids()
    m.select(eids[:5]); m.add_group()
    m.select(eids[5:7]); m.add_group()
    m.toggle_select(eids[7]); m.toggle_select(eids[8])
    m.cycle_sf(Suit.SPADE)
    m.sort_normal()
    m.column_layout()
    m.restore_group(0)
    m.clear_selection()
    assert len(m.real_hand) == n0
    assert m.invariant_ok()
    m.clear_groups()
    assert len(m.real_hand) == n0
    assert m.invariant_ok()


def test_groups_do_not_duplicate_or_delete():
    g, m = _mgr(3)
    n0 = len(m.real_hand)
    eids = m.normal_eids()
    gi = m.add_group(eids[:6])
    grouped = set(m.groups[gi])
    normal = set(m.normal_eids())
    assert grouped.isdisjoint(normal)          # 分组与正常区不重叠
    assert grouped | normal == {c.eid for c in m.real_hand}
    assert len(m.real_hand) == n0
    m.restore_group(gi)
    assert set(m.normal_eids()) == {c.eid for c in m.real_hand}


def test_move_selected_to_group_and_clear():
    g, m = _mgr(4)
    eids = m.normal_eids()
    gi = m.add_group(eids[:2])
    m.select(eids[2:4])
    m.move_selected_to_group(gi)
    assert len(m.groups[gi]) == 4
    m.clear_groups()
    assert m.groups == []
    assert len(m.normal_eids()) == 27


def test_sf_cycle_rotation():
    # 构造一手含某花色多个同花顺候选的手牌
    g = GuandanGame(rng=random.Random(5))
    m = HandManager(g, 0)
    # 直接改写真实手牌为测试牌（黑桃5-9、黑桃6-10 两个候选）
    from cards import Card, build_deck
    deck = build_deck()
    def pick(suit, rank):
        return next(c for c in deck if c.suit == suit and c.rank == rank and c.eid not in used)
    used = set()
    hand = []
    for r in [5, 6, 7, 8, 9, 10]:
        c = pick(Suit.SPADE, r); used.add(c.eid); hand.append(c)
    # 补足 27 张无关牌（避开黑桃 4..J 以免引入额外同花顺候选）
    for c in deck:
        if len(hand) >= 27:
            break
        if c.eid in used:
            continue
        if c.suit == Suit.SPADE and 4 <= c.rank <= 11:
            continue
        hand.append(c); used.add(c.eid)
    g.hands[0] = hand
    m.sync()
    assert m.has_sf(Suit.SPADE)
    c1 = m.cycle_sf(Suit.SPADE)
    c2 = m.cycle_sf(Suit.SPADE)
    assert c1 is not None and c2 is not None
    assert c1.eids != c2.eids               # 切换到不同候选
    assert m.selected == set(c2.eids)        # 仅保留当前候选为待出牌
    c3 = m.cycle_sf(Suit.SPADE)              # 循环回到首个
    assert set(c3.cards) is not None
    assert len(m.real_hand) == 27


def test_auto_arrange_keeps_count():
    g, m = _mgr(6)
    n0 = len(m.real_hand)
    m.auto_arrange()
    assert len(m.real_hand) == n0
    assert m.invariant_ok()
    total = sum(len(cg) for cg in m.groups) + len(m.normal_eids())
    assert total == n0


def test_empty_suit_icon_state():
    g, m = _mgr(7)
    # 无候选花色 cycle 返回 None
    suits_with = {s for s in Suit if m.has_sf(s)}
    for s in (Suit.SPADE, Suit.HEART, Suit.CLUB, Suit.DIAMOND):
        if s not in suits_with:
            assert m.cycle_sf(s) is None


def test_commit_play_syncs_states():
    g, m = _mgr(8)
    # 选一张单牌出牌
    pid = g.turn if g.turn == 0 else 0
    # 强制轮到 0：直接调用 manager + game
    from rules import can_play
    card = m.real_hand[0]
    combo = can_play([card], None, g.level)
    assert combo is not None
    m.select([card.eid])
    n_before = len(m.real_hand)
    g.hands[0].remove(card)          # 模拟 game.play 已移除
    m.commit_play(combo)
    assert len(m.real_hand) == n_before - 1
    assert card.eid not in m.selected
    assert m.invariant_ok()


def test_arrange_selected_bomb_invalid_and_single():
    # 理牌只处理选中的牌：炸弹成组、不能成一首牌不整理、单张也可成组
    from cards import build_deck
    g = GuandanGame(rng=random.Random(11))
    m = HandManager(g, 0)
    deck = build_deck()
    used = set()

    def pick(pred):
        for c in deck:
            if c.eid not in used and pred(c):
                used.add(c.eid)
                return c
        raise AssertionError("no card")

    bomb = [pick(lambda c: c.rank == 9 and not c.is_joker) for _ in range(4)]
    trio = [pick(lambda c: c.rank == r and c.suit == Suit.CLUB) for r in (5, 6, 7)]
    g.hands[0] = bomb + trio
    m.sync()
    # 未选牌：无效果
    assert m.arrange_selected() == ("empty", "")
    # 炸弹 → ok/bomb
    m.select([c.eid for c in bomb])
    assert m.arrange_selected() == ("ok", "bomb")
    bombs, combos = m.arranged_layout()
    assert len(bombs) == 1 and not combos
    assert set(bombs[0][1]) == {c.eid for c in bomb}
    # 三张杂牌不能构成一首牌：不整理
    m.select([c.eid for c in trio])
    assert m.arrange_selected()[0] == "invalid"
    assert m.groups and len(m.groups) == 1       # 未新增分组
    # 单张可成组 → ok/combo（放右侧）
    m.select([trio[0].eid])
    assert m.arrange_selected() == ("ok", "combo")
    bombs, combos = m.arranged_layout()
    assert len(combos) == 1
    assert m.invariant_ok()
    assert len(m.real_hand) == 7


def test_restore_group_back_to_origin():
    g = GuandanGame(rng=random.Random(12))
    m = HandManager(g, 0)
    eids = m.normal_eids()
    target = eids[3:6]
    gi = m.add_group(target)
    assert gi >= 0
    assert set(target).isdisjoint(set(m.normal_eids()))
    m.restore_group(gi)
    assert m.normal_eids()[3:6] == target      # 恢复到原来位置
    assert m.invariant_ok()


def test_restore_selected_group_only():
    g = GuandanGame(rng=random.Random(13))
    m = HandManager(g, 0)
    eids = m.normal_eids()
    t1, t2 = eids[2:4], eids[6:9]
    m.add_group(t1)
    g2 = m.add_group(t2)
    m.select(t2)
    bombs, combos = m.arranged_layout()
    idxs = [gi for gi, ee in bombs + combos if any(e in m.selected for e in ee)]
    assert idxs == [g2]
    m.restore_groups(idxs)
    assert set(t2) <= set(m.normal_eids())
    assert set(t1).isdisjoint(set(m.normal_eids()))
    assert m.invariant_ok()


def test_arrange_prefers_straight_flush_left():
    # 选中的 5 张同花色连牌既能作顺子也能作同花顺，应按炸弹族（同花顺）放左侧
    from cards import build_deck
    g = GuandanGame(rng=random.Random(21))
    m = HandManager(g, 0)
    deck = build_deck()
    used = set()
    hand = []
    for r in (5, 6, 7, 8, 9):
        c = next(c for c in deck
                 if c.suit == Suit.SPADE and c.rank == r and c.eid not in used)
        used.add(c.eid)
        hand.append(c)
    for c in deck:
        if len(hand) >= 27:
            break
        if c.eid in used:
            continue
        if c.suit == Suit.SPADE and 4 <= c.rank <= 11:
            continue
        hand.append(c)
        used.add(c.eid)
    g.hands[0] = hand
    m.sync()
    m.select([c.eid for c in hand[:5]])
    status, kind = m.arrange_selected()
    assert status == "ok" and kind == "bomb"
    bombs, combos = m.arranged_layout()
    assert len(bombs) == 1 and not combos
    assert set(bombs[0][1]) == {c.eid for c in hand[:5]}
    assert m.invariant_ok()
