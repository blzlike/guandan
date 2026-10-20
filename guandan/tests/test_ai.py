"""AI V1.1 增强模块测试：记牌器、残局搜索、enhanced 决策与音效映射。"""
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import (CardMemory, Budget, FEATURE_FEED, HIGH_FEATURES, Weights,
                auto_step, choose_action, choose_action_detail, simulate_match)
from cards import BIG_JOKER, SMALL_JOKER
from game import GuandanGame
from rules import HandType
from sound import event_for_combo
from strategy import feed_helps

from helper import club, diamond, heart, spade


# ---------------------------------------------------------------- 记牌器
def test_card_memory_unseen_counts():
    g = GuandanGame(rng=random.Random(7))
    mem = CardMemory(g, 0)
    total = {r: 8 for r in range(2, 15)}
    total[SMALL_JOKER] = 2
    total[BIG_JOKER] = 2
    # 未见 = 108 张配额 - 我手牌 - 已出（发牌后无已出牌）
    for r, n in total.items():
        mine = sum(1 for c in g.hands[0] if c.rank == r)
        assert mem.unseen[r] == n - mine
    assert sum(mem.unseen.values()) == 108 - 27


def test_card_memory_tracks_played_cards():
    g = GuandanGame(rng=random.Random(11))
    # 直接操纵：构造第一轮自由出牌（选普通点数牌，避开王的 2 张配额）
    g.phase = "playing"
    g.turn = 0
    card = next(c for c in g.hands[0] if c.rank <= 14)
    g.play(0, [card])
    mem = CardMemory(g, 0)
    assert mem.unseen[card.rank] == 8 - sum(
        1 for c in g.hands[0] if c.rank == card.rank) - 1


def test_card_memory_beat_possible():
    class FakeCombo:
        def __init__(self, type_, rank, bomb=False):
            self.type = type_
            self.rank = rank
            self._bomb = bomb

        def is_bomb_family(self):
            return self._bomb

    g = GuandanGame(rng=random.Random(3))
    mem = CardMemory(g, 0)
    # 大王单张：未见无更大 → 压不住（除非另一张大王未出）
    assert not mem.beat_possible(FakeCombo(HandType.SINGLE, 17)) or \
        mem.unseen[BIG_JOKER] > 0
    # 构造：把两张大王放进我的手牌 → 未见大王为 0 → 大王必压不住
    for c in list(g.hands[0]):
        if c.rank != BIG_JOKER:
            g.hands[0].remove(c)
            break
    g.hands[0] += [spade(BIG_JOKER)]
    seen_big = sum(1 for c in g.hands[0] if c.rank == BIG_JOKER)
    mem2 = CardMemory(g, 0)
    if seen_big == 2:
        assert not mem2.beat_possible(FakeCombo(HandType.SINGLE, 17))
    # 天王炸永远无解
    assert not mem2.beat_possible(FakeCombo(HandType.ROCKET, 0, bomb=True))


# ---------------------------------------------------------------- 残局搜索
def test_endgame_search_finishes_when_possible():
    g = GuandanGame(rng=random.Random(5))
    # 精简手牌为 4 张同点炸弹 → 主动出牌时一次出完锁定
    g.phase = "playing"
    g.hands[0] = [spade(9), heart(9), club(9), diamond(9)]
    g.last_play = None
    g.turn = 0
    kind, cards = choose_action(g, 0, enhanced=True)
    assert kind == "play" and len(cards) == 4


def test_enhanced_ai_full_match_smoke():
    """enhanced=True 全自动模拟整场比赛：合法、无死循环、能正常终局。"""
    for seed in (1, 2, 3):
        g = GuandanGame(rng=random.Random(seed))
        stats = simulate_match(g, enhanced=True, max_actions=40000)
        assert stats["winner_team"] in (0, 1)
        assert stats["rounds"] >= 1


def test_enhanced_matches_basic_when_disabled():
    """enhanced=False 保持 V1.0 行为：与旧接口结果一致（同种子）。"""
    g1 = GuandanGame(rng=random.Random(42))
    a1 = auto_step(g1)
    g2 = GuandanGame(rng=random.Random(42))
    a2 = auto_step(g2, enhanced=False)
    assert a1 == a2


# ---------------------------------------------------------------- 领出协同评分
def _feed_state(p0_cards, p2_n=2, p1_n=5, p3_n=5, seed=1):
    """构造 feed_helps 领出局面：P0 手牌=p0_cards，对家 P2 剩 p2_n 张，
    P1/P3 各 5 张（无对手报单）；其余实体牌全部记已出。"""
    g = GuandanGame(rng=random.Random(seed))
    g.levels = {0: 10, 1: 10}
    pool = [c for h in g.hands.values() for c in h]
    rng = random.Random(seed + 7)
    rng.shuffle(pool)
    g.hands = {0: list(p0_cards), 1: pool[:p1_n],
               2: pool[p1_n:p1_n + p2_n],
               3: pool[p1_n + p2_n:p1_n + p2_n + p3_n]}
    g.played_cards = pool[p1_n + p2_n + p3_n:]
    g.finish_order = []
    g.last_play = None
    g.phase = "playing"
    g.turn = 0
    return g


def test_gain_team_synergy_scoring_active():
    """协同修复回归（2026-10-20）：advanced 口径（无 feed 硬覆盖 +
    team_feed_lead=1.0）下领出低单 gain_team=-30；经典权重（全 0）本块
    短路不加；本步能出完时守卫生效，不惩罚出完。修复前该分项被
    _feed_teammate 硬覆盖完全遮蔽、从未生效。"""
    p0 = [spade(3), heart(14), heart(13), club(8), club(7),
          diamond(9), spade(5), heart(6), club(4)]
    g = _feed_state(p0)
    assert feed_helps(g, 0)
    _, _, d_adv = choose_action_detail(
        g, 0, enhanced=True, features=HIGH_FEATURES - {FEATURE_FEED},
        budget=Budget(endgame_hand=8, max_candidates=8,
                      weights=Weights(team_feed_lead=1.0)))
    low = next(x for x in d_adv["candidates"]
               if x["kind"] == "play" and x["type"] == "单张")
    assert low["score_parts"]["gain_team"] == -30.0
    _, _, d_cls = choose_action_detail(
        g, 0, enhanced=True, features=HIGH_FEATURES - {FEATURE_FEED},
        budget=Budget(weights=Weights()))
    low2 = next(x for x in d_cls["candidates"]
                if x["kind"] == "play" and x["type"] == "单张")
    assert low2["score_parts"]["gain_team"] == 0.0
    # can_finish 守卫：仅 1 张低单（本步出完）→ 不加 -30
    g1 = _feed_state([spade(3)])
    _, _, d_fin = choose_action_detail(
        g1, 0, enhanced=True, features=HIGH_FEATURES - {FEATURE_FEED},
        budget=Budget(endgame_hand=8, max_candidates=8,
                      weights=Weights(team_feed_lead=1.0)))
    sel = next(x for x in d_fin["candidates"] if x["selected"])
    assert sel["score_parts"]["gain_team"] == 0.0


# ---------------------------------------------------------------- 音效映射
def test_event_for_combo_mapping():
    class C:
        def __init__(self, t):
            self.type = t
            self.rank = 5

        def is_bomb_family(self):
            return True

    assert event_for_combo(C(HandType.SINGLE)) == "play_single"
    assert event_for_combo(C(HandType.PAIR)) == "play_pair"
    assert event_for_combo(C(HandType.BOMB)) == "play_bomb"
    assert event_for_combo(C(HandType.STRAIGHT_FLUSH)) == "play_sf"
    assert event_for_combo(C(HandType.ROCKET)) == "play_rocket"
    assert event_for_combo(C(HandType.STRAIGHT)) == "play_multi"
    assert event_for_combo(C(HandType.TRIPLE_PAIR)) == "play_multi"


def test_sound_manager_silent_without_assets():
    """无音频素材时 SoundManager 全链路静默不抛错。"""
    from sound import SoundManager
    settings = {"sound": True, "bgm": "off"}
    sm = SoundManager(lambda: settings)
    for ev in ("deal", "pass", "play_bomb", "round_win"):
        sm.play(ev)               # 缺素材 → 静默
    sm.update_bgm()
    settings["bgm"] = "track1"
    sm.update_bgm()               # 缺 BGM 文件 → 静默
    sm.stop_all()
    settings["sound"] = False
    sm.play("deal")               # 开关关闭 → 直接跳过
