"""阶段三教学一期回归：observer 统计守恒 / 缺门 / 炸弹预测 / advisor 建议合法性。"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from advisor import advise
from cards import RANK_LABELS, Suit
from game import GuandanGame
from observer import COMBO_COLS, Observer, PlayTracker
from rules import can_play, generate_legal_moves


# ---------------------------------------------------------------- observer
def test_observer_conservation():
    """未见分布守恒：点数未见合计 = 108 - 我手 - 已出；花色同理。"""
    for seed in (1, 7, 2026):
        g = GuandanGame(random.Random(seed))
        # 打几手牌制造已出牌
        obs = Observer(g, 0)
        for r in range(2, 17):
            assert obs.unseen[r] >= 0
        total_mine = len(g.hands[0])
        total_unseen = sum(obs.unseen.values())
        assert total_unseen == 108 - total_mine - len(g.played_cards)
        for s in (Suit.SPADE, Suit.HEART, Suit.CLUB, Suit.DIAMOND):
            assert obs.unseen_by_suit[s] == 26 - sum(
                1 for c in g.hands[0] if c.suit is s) - sum(
                1 for c in g.played_cards if c.suit is s)


def test_observer_core_and_bombs():
    """核心牌行配额恒定（王2/级牌6/逢人配2），三项之和守恒；炸弹候选⊆未见≥4。"""
    g = GuandanGame(random.Random(42))
    obs = Observer(g, 0)
    rows = {name: (m, o, u) for name, m, o, u in obs.core_rows()}
    for name, quota in (("大王", 2), ("小王", 2),
                        ("级牌" + RANK_LABELS[g.level], 6), ("逢人配", 2)):
        m, o, u = rows[name]
        assert m + o + u == quota, name
    lv = g.level
    for r in obs.bomb_candidates():
        assert obs.unseen[r] >= 4 and r < 15
    # 级牌与逢人配不相交（逢人配=红桃级牌）
    assert rows["逢人配"][0] + rows["级牌" + RANK_LABELS[lv]][0] \
        == sum(1 for c in g.hands[0] if c.rank == lv)


def test_observer_voids():
    """手牌缺门：构造缺黑桃的手牌后 my_voids 含黑桃。"""
    g = GuandanGame(random.Random(3))
    hand = [c for c in g.hands[0] if c.suit is not Suit.SPADE]
    g.hands[0] = hand
    obs = Observer(g, 0)
    assert Suit.SPADE in obs.my_voids()
    assert obs.mine_by_suit.get(Suit.SPADE, 0) == 0


def test_core_residue():
    """模块1 七列剩余：王=大小王未见合计、级/配与 core_rows 一致、A/K/10/5=unseen。"""
    g = GuandanGame(random.Random(5))
    obs = Observer(g, 0)
    vals = obs.core_residue()
    assert len(vals) == 7
    assert vals[0] == obs.unseen[15] + obs.unseen[16]
    rows = obs.core_rows()
    assert vals[1] == rows[2][3] and vals[2] == rows[3][3]
    assert vals[3] == obs.unseen[14] and vals[4] == obs.unseen[13]
    assert vals[5] == obs.unseen[10] and vals[6] == obs.unseen[5]
    # 打出一张 A 后 A 列减少
    ace = next(c for c in g.hands[0] if c.rank == 14)
    g.played_cards.append(ace)
    obs2 = Observer(g, 0)
    assert obs2.core_residue()[3] == vals[3] - 1


# ---------------------------------------------------------------- advisor
def _advise_playable(g, pid, adv):
    """建议为出牌时必须合法（can_play 通过 + 牌在手）。"""
    combo = adv["combo"]
    assert all(c in g.hands[pid] for c in combo.cards)
    prev = g.last_play[1] if g.last_play else None
    assert can_play(combo.cards, prev, g.level) is not None, adv["text"]


def test_advisor_always_legal_or_pass():
    """随机 60 局×每手：轮到 0 时建议要么合法出牌要么 pass，永不出错。"""
    plays = passes = 0
    for seed in range(1, 61):
        g = GuandanGame(random.Random(seed))
        guard = 0
        while g.phase == "playing" and g.turn != 0 and guard < 400:
            _ai_step(g, g.turn)
            guard += 1
        while g.phase == "playing" and guard < 1200:
            adv = advise(g, 0)
            assert adv is not None and adv["text"]
            if adv["action"] == "play":
                _advise_playable(g, 0, adv)
                g.play(0, list(adv["combo"].cards))
                plays += 1
            else:
                assert g.last_play is not None    # pass 仅在跟牌时建议
                g.pass_play(0)
                passes += 1
            guard += 1
            while g.phase == "playing" and g.turn != 0 and guard < 1200:
                _ai_step(g, g.turn)
                guard += 1
        if g.phase == "round_over":
            g.start_next_round()
    assert plays > 30 and passes > 0


def _ai_step(g, pid):
    """轻量驱动：用最小成本合法动作推进 AI（不引 ai.py，保持教学隔离）。"""
    prev = g.last_play[1] if g.last_play else None
    moves = generate_legal_moves(g.hands[pid], prev, g.level)
    if not moves:
        g.pass_play(pid)
    else:
        m = min(moves, key=lambda m: (1000 if m.is_bomb_family() else 0,
                                      m.size, m.rank))
        g.play(pid, list(m.cards))


def test_advisor_finish_first():
    """能一次出完时必建议全出。"""
    from cards import build_deck
    g = GuandanGame(random.Random(11))
    g.phase = "playing"
    g.turn = 0
    g.last_play = None
    deck = build_deck()
    # 手牌：3333 444 → 可一次出完？不合法；改用同点炸弹+对：5张炸弹剩2单不行。
    # 用 5 张炸弹一次出完
    bomb5 = [c for c in deck if c.rank == 13][:5]
    g.hands[0] = bomb5
    adv = advise(g, 0)
    assert adv["action"] == "play" and len(adv["combo"].cards) == 5
    assert "出完" in adv["text"]


def test_advisor_pass_on_partner_lead():
    """队友牌权：有牌可压仍建议 pass（保队友持续出牌）。"""
    from cards import build_deck
    g = GuandanGame(random.Random(12))
    g.phase = "playing"
    deck = build_deck()
    g.hands[0] = [c for c in deck if c.rank == 14][:2] + \
                 [c for c in deck if c.rank == 3][:1]   # 对A+单3，可压单9
    lead = [c for c in deck if c.rank == 9][:1]
    g.last_play = (2, can_play(lead, None, g.level))       # 对家(队友)领出单9
    g.turn = 0
    moves = generate_legal_moves(g.hands[0], g.last_play[1], g.level)
    assert moves                                          # 确实有牌可压
    adv = advise(g, 0)
    assert adv["action"] == "pass" and "队友" in adv["text"]


def test_advisor_top_when_opponent_reporting():
    """对手报牌（剩≤2）→ 必顶：出最大非炸；无非炸则最小炸。"""
    from cards import build_deck
    g = GuandanGame(random.Random(13))
    g.phase = "playing"
    deck = build_deck()
    g.hands[0] = [c for c in deck if c.rank == 3][:2] + \
                 [c for c in deck if c.rank == 14][:1]
    g.hands[1] = [c for c in deck if c.rank == 7][:1]      # 对手报牌（剩1张）
    g.hands[3] = []
    g.finish_order = []
    lead = [c for c in deck if c.rank == 6][:1]
    g.last_play = (1, can_play(lead, None, g.level))       # 右家(对手)领单6
    g.turn = 0
    adv = advise(g, 0)
    assert adv["action"] == "play" and adv["combo"].rank > can_play(lead, None, g.level).rank
    assert "报牌" in adv["text"]


def test_advisor_no_advice_when_not_turn():
    g = GuandanGame(random.Random(15))
    g.phase = "playing"
    g.turn = 1
    assert advise(g, 0) is None
    g.phase = "tribute"
    g.turn = 0
    assert advise(g, 0) is None


# ---------------------------------------------------------------- PlayTracker
def test_tracker_played_and_reset():
    """play 事件按牌型归列（炸弹族→炸列）；round_start 清空。"""
    from cards import build_deck
    g = GuandanGame(random.Random(21))
    tr = PlayTracker()
    assert tr.attach(g)
    g.phase = "playing"
    g.turn = 0
    g.last_play = None
    deck = build_deck()
    bomb = [c for c in deck if c.rank == 6][:4]
    filler = next(c for c in deck if c.rank == 3 and c.suit is Suit.CLUB)
    g.hands[0] = bomb + [filler]                            # 留垫牌防出完
    g.play(0, bomb)                                         # 4张炸
    assert tr.played[0][0] == 1 and sum(tr.played[0]) == 1
    # 同花顺也归「炸」列
    sf = [next(c for c in deck if c.suit is Suit.SPADE and c.rank == r)
          for r in (3, 4, 5, 6, 7)]
    g.hands[0] = sf + [next(c for c in deck if c.rank == 4 and c.suit is Suit.CLUB)]
    g.turn = 0
    g.last_play = None
    g.play(0, sf)
    # classify 多解释时取首个（3..7 同花连续 → 顺子解释优先于同花顺）
    assert tr.played[0][COMBO_COLS.index("顺")] == 1
    # 同花顺/天王炸归「炸」列（映射直验）
    from observer import _COL_OF_TYPE
    assert _COL_OF_TYPE["STRAIGHT_FLUSH"] == 0 and _COL_OF_TYPE["ROCKET"] == 0
    g._notify("round_start", round_number=2)
    assert sum(sum(v) for v in tr.played.values()) == 0


def test_tracker_attach_guard():
    """多监听共存可挂；非比赛起点（已有出牌 / 非第1局）拒绝挂载。"""
    from cards import build_deck
    g = GuandanGame(random.Random(23))
    tr = PlayTracker()
    assert tr.attach(g)
    assert PlayTracker().attach(g)               # 多 listener 共存
    g.phase = "playing"
    deck = build_deck()
    g.hands[0] = [c for c in deck if c.rank == 8][:2] + \
                 [c for c in deck if c.rank == 3][:1]   # 留垫牌防出完
    g.last_play = None
    g.turn = 0
    g.play(0, [c for c in deck if c.rank == 8][:2])
    assert not PlayTracker().attach(g)           # 已有出牌
    tr.detach(g)
    assert tr._fn is None
    g._deal_round(leader=0)                      # round_number=2
    assert not PlayTracker().attach(g)           # 非第1局


def test_tracker_predict_placeholder():
    assert PlayTracker().predict_rows() is None
