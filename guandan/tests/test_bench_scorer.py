"""阶段二回归：bench 双通道等价性 / ladder 聚合+Elo / scorer 专家对比打分。"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai
import bench
import pytest
import scorer
from ai import AIProfile, HIGH_FEATURES, choose_tribute, choose_return
from game import GuandanGame
from replay import Recorder, rebuild


def _drive_auto(game, cfg_a, cfg_b) -> None:
    """旧 bench_ai.py 的驱动逻辑（ai.auto_step，token 配置）用于等价回归。"""
    enhanced = {p: cfg_a[1][0] if p % 2 == 0 else cfg_b[1][0] for p in range(4)}
    features = {p: cfg_a[1][1] if p % 2 == 0 else cfg_b[1][1] for p in range(4)}
    while game.phase != "match_over":
        ai.auto_step(game, enhanced=enhanced, features=features)


# ---------------------------------------------------------------- bench
def test_parse_cfg_dual_channel():
    assert bench.parse_cfg("classic_high") == ("sid", "classic_high")
    assert bench.parse_cfg("classic_low") == ("sid", "classic_low")
    assert bench.parse_cfg("basic") == ("token", (False, frozenset()))
    assert bench.parse_cfg("enh+feed") == ("token", (True, frozenset({"feed"})))
    assert bench.label_of(("token", (True, frozenset({"feed", "mem_lead"})))) \
        == "enh+feed+mem_lead"
    with pytest.raises(SystemExit):
        bench.parse_cfg("wuzui")
    with pytest.raises(SystemExit):
        bench.parse_cfg("enh+nope")


def test_bench_play_match_matches_legacy_auto_step():
    """bench._step（token 通道）与旧 ai.auto_step 驱动逐字节等价。"""
    cfg_a, cfg_b = ("token", (True, HIGH_FEATURES)), ("token", (False, frozenset()))
    for seed in (11, 42):
        by_pid = {p: cfg_a if p % 2 == 0 else cfg_b for p in range(4)}
        g_new = GuandanGame(rng=random.Random(seed))
        while g_new.phase != "match_over":
            bench._step(g_new, by_pid)
        g_old = GuandanGame(rng=random.Random(seed))
        _drive_auto(g_old, cfg_a, cfg_b)
        assert g_new.match_winner == g_old.match_winner
        assert len(g_new.log) == len(g_old.log)


def test_bench_sid_token_equivalence():
    """classic_high sid 与 enh+HIGH_FEATURES token 同 seed 同结果（双通道一致）。"""
    for seed in (11, 42):
        g_sid = GuandanGame(rng=random.Random(seed))
        by_pid = {p: ("sid", "classic_high") for p in range(4)}
        while g_sid.phase != "match_over":
            bench._step(g_sid, by_pid)
        g_tok = GuandanGame(rng=random.Random(seed))
        by_pid = {p: ("token", (True, HIGH_FEATURES)) for p in range(4)}
        while g_tok.phase != "match_over":
            bench._step(g_tok, by_pid)
        assert g_sid.match_winner == g_tok.match_winner
        assert len(g_sid.log) == len(g_tok.log)


# ---------------------------------------------------------------- ladder
def test_ladder_roundtrip_and_elo(tmp_path):
    path = str(tmp_path / "ladder.json")
    d = bench.load_ladder(path)
    assert d["version"] == 1 and not d["elo"]
    e0 = bench.ELO_BASE
    bench.record_result(d, "classic_low", "classic_high", 100, 812, 430, 60, 1000)
    bench.record_result(d, "classic_low", "classic_high", 100, 790, 405, 55, 1100)
    bench.save_ladder(d, path)
    d2 = bench.load_ladder(path)
    pair = d2["pairs"]["classic_low|classic_high"]
    assert pair["games"] == 200 and pair["rounds"] == 1602
    assert pair["a_round_wins"] == 835 and pair["a_match_wins"] == 115
    assert len(d2["history"]) == 2
    # Elo：胜多者涨分（60+55/200 场），双方净分守恒
    lo, hi = d2["elo"]["classic_low"], d2["elo"]["classic_high"]
    assert lo > e0 > hi
    assert abs((lo - e0) + (hi - e0)) < 1e-6


# ---------------------------------------------------------------- scorer
def _record_match(seed: int, profiles=None):
    rec = Recorder(seed=seed)
    g = GuandanGame(rng=random.Random(seed))
    rec.attach(g)
    while g.phase != "match_over":
        if g.phase == "round_over":
            g.start_next_round()
            continue
        if g.phase == "tribute":
            pending = [(x, r) for x, r in g.tribute_pairs if x not in g.tribute_done]
            if pending:
                giver, _ = pending[0]
                g.submit_tribute(giver, choose_tribute(g, giver))
            else:
                receiver = next(r for r in g.tribute_done.values()
                                if r not in g.return_done)
                g.submit_return(receiver, choose_return(g, receiver))
            continue
        pid = g.turn
        prof = profiles[pid] if profiles else AIProfile()
        kind, cards = ai.choose_action(g, pid, prof,
                                       enhanced=True, features=HIGH_FEATURES)
        if kind == "play":
            g.play(pid, cards)
        else:
            g.pass_play(pid)
    return rec


def test_scorer_scores_expert_consistent_match():
    """全员打 expert 同策略（balanced）→ 四座位高分；事件数=play+pass 数。"""
    rec = _record_match(99)
    res = scorer.score_record(rec.to_dict())
    assert set(res["scores"]) == {0, 1, 2, 3}
    for p, s in res["scores"].items():
        assert 0.0 <= s <= 100.0
        assert s >= 90.0, f"P{p} 全专家同策略应接近满分: {s}"
    n_pp = sum(1 for a in rec.actions if a["kind"] in ("play", "pass"))
    assert sum(res["actions"].values()) == n_pp
    assert len(res["events"]) == n_pp


def test_scorer_rebuild_on_action_timing():
    """on_action 在动作套用前触发：turn == 动作 pid，手牌含待出牌。"""
    rec = _record_match(7)
    seen = []

    def on_action(act, g):
        if act["kind"] in ("play", "pass"):
            seen.append((act["pid"], g.turn, g.round_number))
    rebuild(rec.to_dict(), on_action=on_action)
    plays = [a for a in rec.actions if a["kind"] in ("play", "pass")]
    assert len(seen) == len(plays)
    for (pid, turn, rnd), act in zip(seen, plays):
        assert pid == act["pid"] == turn       # 决策时刻：轮到本家
        assert rnd == act["round"]


def test_scorer_judge_rules():
    """_judge 判定分支：一致 / 普通不一致 / 该出未出 / 乱用炸弹 / 报牌失误。"""
    from types import SimpleNamespace
    from cards import build_deck
    level = 2
    eids = [0, 1]
    assert scorer._judge("play", eids, "play", eids, [], [], False, level) \
        == (1.0, "")
    assert scorer._judge("play", [9, 9], "play", eids, [], [], False, level) \
        == (0.3, "与专家不一致")
    # 该出未出：专家可出而实际过牌
    assert scorer._judge("pass", None, "play", eids, [], [], False, level) \
        == (0.0, "该出未出")
    # 乱用炸弹：实际出炸弹而专家未出炸弹族
    deck = build_deck()
    bomb = [c for c in deck if c.rank == 7][:4]        # 4 张同点（非级牌）
    other = [c for c in deck if c.rank not in (7, 15, 16)][:2]
    assert scorer._judge("play", [c.eid for c in bomb], "play", eids,
                         bomb, other, False, level) == (0.0, "乱用炸弹")
    # 专家也出炸弹 → 不算乱用（降级为普通不一致）
    assert scorer._judge("play", [c.eid for c in bomb], "play", eids,
                         bomb, bomb, False, level) == (0.3, "与专家不一致")
    # 报牌失误：报牌局面下普通不一致
    g = SimpleNamespace(hand_count=lambda p: 2 if p == 0 else 20)
    assert scorer._under_pressure(g, 0)
    assert not scorer._under_pressure(
        SimpleNamespace(hand_count=lambda p: 20), 0)
    assert scorer._judge("play", [9], "pass", None, other, [], True, level) \
        == (0.0, "报牌失误")
