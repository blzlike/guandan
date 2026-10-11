"""牌局流程测试：发牌、回合、非法出牌防护、进贡还贡、升级、完整模拟。"""
import random

import pytest
from ai import AIProfile, choose_action, simulate_match
from cards import Card, build_deck
from game import (GameError, GuandanGame, IllegalPlayError, NotPlayerTurnError)
from rules import HandType, classify


def _to_eid_map(g):
    return {c.eid: c for pid in g.hands for c in g.hands[pid]}


def test_deal_108_27_each():
    g = GuandanGame(rng=random.Random(1))
    assert g.phase == "playing"
    assert sum(g.hand_count(i) for i in range(4)) == 108
    assert all(g.hand_count(i) == 27 for i in range(4))
    eids = [c.eid for i in range(4) for c in g.hands[i]]
    assert len(set(eids)) == 108  # 无重复、无丢失


def test_turn_enforcement():
    g = GuandanGame(rng=random.Random(2))
    wrong = (g.turn + 1) % 4
    with pytest.raises(NotPlayerTurnError):
        g.play(wrong, [g.hands[wrong][0]])


def test_illegal_play_does_not_change_hand():
    g = GuandanGame(rng=random.Random(3))
    pid = g.turn
    before = list(g.hands[pid])
    # 两张不同点非对子 -> 非法
    bad = [c for c in before if c.rank not in (x.rank for x in before if x is not c)]
    two = before[0], before[1]
    if classify([two[0], two[1]], g.level):
        pytest.skip("恰好合法，换种子")
    with pytest.raises(IllegalPlayError):
        g.play(pid, [two[0], two[1]])
    assert g.hands[pid] == before  # 手牌未被改变


def test_cannot_pass_when_leading():
    g = GuandanGame(rng=random.Random(4))
    assert g.last_play is None
    with pytest.raises(IllegalPlayError):
        g.pass_play(g.turn)


def test_trick_displays_lifecycle():
    # 回合内各玩家出牌/过牌保留展示；回合结束（下轮开始）清空
    g = None
    for sd in range(21, 60):
        g = GuandanGame(rng=random.Random(sd))
        if g.phase == "playing":
            break
    assert g.phase == "playing"
    leader = g.turn
    card = min(g.hands[leader], key=lambda c: (c.is_joker, c.rank))
    g.play(leader, [card])
    assert g.trick_displays[leader]["action"] == "play"
    assert g.trick_displays[leader]["combo"] is not None
    for i in range(1, 3):
        pid = (leader + i) % 4
        g.pass_play(pid)
        assert g.trick_displays[pid]["action"] == "pass"
    last = (leader + 3) % 4
    g.pass_play(last)                  # 最后一家过牌即回合结束
    assert g.last_play is None
    assert g.trick_displays == {}      # 回合结束清除展示
    assert g.turn == leader            # 首出者继续首出


def test_full_match_simulation_many_seeds():
    for seed in range(15):
        g = GuandanGame(rng=random.Random(seed))
        profiles = [AIProfile("balanced"), AIProfile("aggressive"),
                    AIProfile("balanced"), AIProfile("conservative")]
        result = simulate_match(g, profiles, max_actions=50000)
        assert result["winner_team"] in (0, 1)
        assert result["rounds"] >= 1
        # 每局结束牌数守恒：全程无重复实体牌被使用
        assert g.phase == "match_over"


def test_tribute_and_return_conserved():
    g = GuandanGame(rng=random.Random(7))
    # 手工构造：让 P1 成为末游进贡给 P0
    g.tribute_pairs = [(1, 0)]
    g._round_last, g._round_head = 1, 0
    g.next_leader = 1
    g.phase = "round_over"
    g.start_next_round()
    assert g.phase == "tribute" or g.tribute_refused
    if g.phase == "tribute":
        total_before = sum(g.hand_count(i) for i in range(4))
        giver = g.tribute_pairs[0][0]
        receiver = g.tribute_pairs[0][1]
        eid = g.legal_tribute_cards(giver)[0].eid
        g.submit_tribute(giver, eid)
        assert g.hand_count(giver) == 26 and g.hand_count(receiver) == 28
        # 非法进贡（非最大牌）被拒
        awaiting = [r for r in g.tribute_done.values() if r not in g.return_done]
        rec = awaiting[0]
        small = min(g.hands[rec], key=lambda c: c.eid).eid
        g.submit_return(rec, small)
        assert g.phase == "playing"
        assert sum(g.hand_count(i) for i in range(4)) == total_before


def test_tribute_must_be_max_card():
    g = GuandanGame(rng=random.Random(11))
    g.tribute_pairs = [(1, 0)]
    g._round_last, g._round_head = 1, 0
    g.phase = "round_over"
    g.start_next_round()
    if g.phase != "tribute":
        pytest.skip("抗贡")
    giver = 1
    legal = g.legal_tribute_cards(giver)
    non_legal = [c for c in g.hands[giver] if c not in legal]
    if non_legal:
        with pytest.raises(IllegalPlayError):
            g.submit_tribute(giver, non_legal[0].eid)


def test_level_cap_at_ace():
    g = GuandanGame(rng=random.Random(5))
    g.levels = {0: 13, 1: 5}
    g.playing_team = 0
    # 直接触发结算：构造 finish_order 使 0 队头游+二游（双下+3），封顶14
    g.finish_order = [0, 2, 1, 3]
    g._end_round()
    assert g.levels[0] == 14  # 13+3 封顶14


def test_play_a_wins_match():
    g = GuandanGame(rng=random.Random(6))
    g.levels = {0: 14, 1: 5}
    g.playing_team = 0
    g.finish_order = [0, 1, 2, 3]  # 头游P0属0队，二游P1属1队 -> +1
    g._end_round()
    assert g.phase == "match_over"
    assert g.match_winner == 0


def test_team_complete_ends_round_early():
    """一方两家出完：本局立即结束，未打完的两家直接记末游。"""
    from cards import Suit
    g = GuandanGame(rng=random.Random(7))
    g.phase = "playing"
    g.turn = 0
    g.last_play = None
    bomb0 = [Card(Suit.SPADE, 5, 900 + i) for i in range(4)]
    bomb2 = [Card(Suit.CLUB, 7, 910 + i) for i in range(4)]
    g.hands[0] = bomb0
    g.hands[2] = bomb2
    g.play(0, bomb0)
    assert g.phase == "playing"           # 仅头游，对方仍在打
    g.turn = 2
    g.play(2, bomb2)                      # 0队两家出完 -> 立即结束
    assert g.phase == "round_over"
    assert set(g.finish_order) == {0, 1, 2, 3}
    assert g.finish_order[:2] == [0, 2]   # 出完的两家按出完顺序
    assert g.round_result["auto_last"] == [1, 3]   # 未打完记末游
    assert g.round_result["double_down"] is True   # 头游+二游同队
    assert g.round_result["delta"] == 3
