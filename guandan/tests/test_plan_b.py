"""阶段五：夯档 PIMC（plan_b.py）测试。

采样/推演依赖真实 108 副牌（eid 0..107），本文件不使用 helper.c()
（其 eid 从 1001 起，会破坏 unseen 守恒）。
"""
import random

import ai
import plan_b
import registry
from cards import build_deck
from game import GuandanGame
from rules import classify

_BY_EID = {c.eid: c for c in build_deck()}
# 牌堆布局：每副 4 花色 ×13 点（2..A），两副 + 4 王；rank r 的花色 s eid = s*13+(r-2)（+52 为第二副）


def _make_state(seed=0, p0_eids=()):
    """构造 playing 阶段局面：P0 手牌按 eid 指定，其余牌均分 1/2/3。"""
    g = GuandanGame(random.Random(seed))
    p0 = [_BY_EID[e] for e in p0_eids]
    rest = [c for c in _BY_EID.values() if c.eid not in set(p0_eids)]
    rng = random.Random(seed + 99)
    rng.shuffle(rest)
    n = len(rest)
    g.hands = {0: p0, 1: rest[:n // 3], 2: rest[n // 3:2 * (n // 3)],
               3: rest[2 * (n // 3):]}
    g.played_cards = []
    g.finish_order = []
    g.last_play = None
    g.phase = "playing"
    g.turn = 0
    return g


# ---------------------------------------------------------------- 采样守恒
def test_unseen_cards_conserves():
    g = GuandanGame(random.Random(11))
    for _ in range(30):
        if g.phase != "playing":
            break
        ai.auto_step(g)
    unseen = plan_b.unseen_cards(g, 0)
    others = sum(g.hand_count(o) for o in (1, 2, 3))
    assert len(unseen) == others
    mine = {c.eid for c in g.hands[0]} | {c.eid for c in g.played_cards}
    assert not mine & {c.eid for c in unseen}


def test_sample_partition_valid():
    g = GuandanGame(random.Random(12))
    unseen = plan_b.unseen_cards(g, 0)
    counts = {o: g.hand_count(o) for o in (1, 2, 3)}
    hands = plan_b.sample_partition(unseen, counts, random.Random(3))
    assert all(len(hands[o]) == counts[o] for o in counts)
    all_eids = [c.eid for o in (1, 2, 3) for c in hands[o]]
    assert sorted(all_eids) == sorted(c.eid for c in unseen)  # 无重复无遗漏


# ---------------------------------------------------------------- 回退一致性
def test_noncritical_equals_classic_high():
    """非关键回合 pimc 与 classic_high 逐字节一致（回归保证）。"""
    for seed in (21, 22, 23):
        g = GuandanGame(random.Random(seed))
        checked = 0
        while g.phase != "match_over" and checked < 40:
            if g.phase == "round_over":
                g.start_next_round()
            elif g.phase == "playing":
                pid = g.turn
                if not plan_b.is_critical(g, pid):
                    a1 = plan_b.choose(g, pid, ai.AIProfile())
                    a2 = ai.choose_action(g, pid, ai.AIProfile(),
                                          enhanced=True,
                                          features=ai.HIGH_FEATURES)
                    assert a1[0] == a2[0]
                    assert [c.eid for c in (a1[1] or [])] == \
                           [c.eid for c in (a2[1] or [])]
                    checked += 1
                act, cards = ai.choose_action(g, pid, ai.AIProfile(),
                                              enhanced=True,
                                              features=ai.HIGH_FEATURES)
                if act == "play":
                    g.play(pid, cards)
                else:
                    g.pass_play(pid)
            else:
                ai.auto_step(g)
        assert checked > 0


# ---------------------------------------------------------------- 锁定与判定
def test_finish_lock():
    """关键回合能一次出完 → 直接锁定该动作（♠2+♥2 一对级牌）。"""
    g = _make_state(p0_eids=[0, 13])
    act, cards = plan_b.choose(g, 0, ai.AIProfile())
    assert act == "play" and len(cards) == 2


def test_is_critical():
    g = _make_state(p0_eids=[0, 13])
    assert plan_b.is_critical(g, 0)          # 自己 2 张 ≤8
    # P0 14 张、对手 23/20/20 → 非关键
    g.hands[0] = g.hands[0] + g.hands[1][:12]
    g.hands[1] = g.hands[1][12:]
    g.hands[2] = g.hands[2][:20]
    g.hands[3] = g.hands[3][:20]
    assert not plan_b.is_critical(g, 0)
    # 对手 P1 剩 5 张 → 关键（对手 ≤6；P2 是队友不算）
    g.hands[1] = g.hands[1][:5]
    assert plan_b.is_critical(g, 0)


def test_reward_of():
    r = {"finish_order": [0, 2, 1, 3], "double_down": True}
    assert plan_b.reward_of(r, 0) == 6 and plan_b.reward_of(r, 1) == -2
    r2 = {"finish_order": [0, 1, 2, 3], "double_down": False}
    assert plan_b.reward_of(r2, 2) == -1 and plan_b.reward_of(r2, 3) == -3
    assert plan_b.reward_of(None, 0) == 0


# ---------------------------------------------------------------- 候选剪枝
def test_candidates_include_bombs_and_pass():
    # P0：9 炸弹（4×9）+ 一对 10；对手领出 A → 只能炸弹或过牌
    g = _make_state(p0_eids=[7, 20, 33, 46, 8, 21])
    ace = classify([_BY_EID[12]], g.level)[0]   # ♠A
    g.last_play = (1, ace)
    cands = plan_b.build_candidates(g, 0)
    assert any(k == "pass" for k, _ in cands)
    bombs = [m for _, m in cands if m is not None and m.is_bomb_family()]
    assert bombs, "9 炸弹应进候选"


# ---------------------------------------------------------------- 确定性/推演
def test_choose_deterministic():
    # 三张 2 + ♠3：无一次出完路径（三带二需 5 张），走 PIMC 推演路径
    g = _make_state(p0_eids=[0, 13, 26, 1])
    a1 = plan_b.choose(g, 0, ai.AIProfile(), n_samples=2, rollout="random")
    a2 = plan_b.choose(g, 0, ai.AIProfile(), n_samples=2, rollout="random")
    assert a1[0] == a2[0]
    assert [c.eid for c in (a1[1] or [])] == [c.eid for c in (a2[1] or [])]


def test_rollout_reaches_round_end():
    g = _make_state(p0_eids=[0, 13])
    unseen = plan_b.unseen_cards(g, 0)
    counts = {o: g.hand_count(o) for o in (1, 2, 3)}
    sampled = plan_b.sample_partition(unseen, counts, random.Random(5))
    # 各家缩到 8 张，避免整局推演过长（rollout 不校验 108 守恒）
    for o in (1, 2, 3):
        sampled[o] = sampled[o][:8]
        g.hands[o] = g.hands[o][:8]
    m = classify(g.hands[0], g.level)[0]
    res = plan_b.rollout(g, 0, ("play", m), sampled,
                         random.Random(6), "random")
    assert res is not None and len(res["finish_order"]) == 4


# ---------------------------------------------------------------- 集成
def test_registry_pimc_round_smoke(monkeypatch):
    """pimc vs classic_low 打完一整局（小参数快速档，冒烟不校验胜率）。"""
    monkeypatch.setattr(plan_b, "N_SAMPLES", 2)
    monkeypatch.setattr(plan_b, "TOP_K", 2)
    monkeypatch.setattr(plan_b, "MAX_ROLLOUT", 60)
    monkeypatch.setenv("GUANDAN_PIMC_ROLLOUT", "random")
    g = GuandanGame(random.Random(77))
    for _ in range(2000):
        if g.phase in ("round_over", "match_over"):
            break
        if g.phase == "tribute":
            ai.auto_step(g)
            continue
        pid = g.turn
        sid = "pimc" if pid % 2 == 0 else "classic_low"
        act, cards = registry.choose(g, pid, ai.AIProfile(), sid)
        if act == "play":
            g.play(pid, cards)
        else:
            g.pass_play(pid)
    assert g.phase in ("round_over", "match_over")
    assert len(g.round_result["finish_order"]) == 4
