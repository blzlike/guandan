"""阶段一架构打底回归：registry 等价性 / clone 保真 / Recorder 重演 / 座位解析。"""
import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai
from ai import AIProfile, HIGH_FEATURES, choose_tribute, choose_return
from game import GuandanGame
from registry import choose as strategy_choose, ids, resolve_seat
from replay import Recorder, load_json, rebuild


PROFILES = [None, AIProfile("aggressive"), AIProfile("balanced"),
            AIProfile("conservative")]


def _drive(game, seat_resolver, use_registry: bool) -> None:
    """驱动整场比赛至结束；seat_resolver(pid) -> (enh, feat) 或 sid。"""
    while game.phase != "match_over":
        if game.phase == "round_over":
            game.start_next_round()
            continue
        if game.phase == "tribute":
            pending = [(g, r) for g, r in game.tribute_pairs
                       if g not in game.tribute_done]
            if pending:
                giver, _ = pending[0]
                game.submit_tribute(giver, choose_tribute(game, giver))
            else:
                receiver = next(r for r in game.tribute_done.values()
                                if r not in game.return_done)
                game.submit_return(receiver, choose_return(game, receiver))
            continue
        pid = game.turn
        if use_registry:
            kind, cards = strategy_choose(game, pid, PROFILES[pid],
                                          seat_resolver)
        else:
            enh, feat = seat_resolver(pid)
            kind, cards = ai.choose_action(game, pid, PROFILES[pid],
                                           enhanced=enh, features=feat)
        if kind == "play":
            game.play(pid, cards)
        else:
            game.pass_play(pid)


# ---------------------------------------------------------------- 等价性
def test_registry_equivalence_high_and_low():
    """registry classic_high/classic_low 与直接调用 choose_action 全局对局等价。"""
    for strength, sid in (("high", "classic_high"), ("low", "classic_low")):
        settings = {"ai_strength": strength}
        for seed in (1, 7, 2024):
            g_direct = GuandanGame(rng=random.Random(seed))
            g_reg = GuandanGame(rng=random.Random(seed))
            _drive(g_direct, lambda pid: (
                strength == "high",
                HIGH_FEATURES if strength == "high" else None), use_registry=False)
            _drive(g_reg, sid, use_registry=True)
            assert g_direct.log == g_reg.log
            assert g_direct.match_winner == g_reg.match_winner


def test_registry_registered_ids():
    assert ids() == ["classic_low", "classic_high", "pimc"]


# ---------------------------------------------------------------- clone
def _midpoint_game(seed: int) -> GuandanGame:
    """跑到中途（第 2 局出过几张牌）返回。"""
    g = GuandanGame(rng=random.Random(seed))
    while len(g.played_cards) < 20 or g.round_number < 2:
        if g.phase == "match_over":
            break
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
        kind, cards = ai.choose_action(g, pid, PROFILES[pid],
                                       enhanced=True, features=HIGH_FEATURES)
        if kind == "play":
            g.play(pid, cards)
        else:
            g.pass_play(pid)
    return g


def test_clone_fidelity():
    """克隆点之后两局面同策略续战：整场 log 与结果完全一致。"""
    g1 = _midpoint_game(42)
    g2 = g1.clone()
    assert g2 is not g1 and g2.rng is not g1.rng
    assert g2.listeners == []
    # 克隆不共享可变容器
    g1.hands[0].append(g1.hands[0][0])
    assert len(g1.hands[0]) != len(g2.hands[0])
    g1.hands[0].pop()
    sid = "classic_high"
    _drive(g1, sid, use_registry=True)
    _drive(g2, sid, use_registry=True)
    assert g1.log == g2.log
    assert g1.match_winner == g2.match_winner


# ---------------------------------------------------------------- 录制重演
def test_recorder_rebuild_replay(tmp_path):
    """录制整场 → JSON 落盘 → rebuild 重演：log 与结果逐行一致。"""
    rec = Recorder(seed=99)
    game = GuandanGame(rng=random.Random(99))
    rec.attach(game)
    _drive(game, "classic_high", use_registry=True)
    record = rec.to_dict()
    path = tmp_path / "record.json"
    rec.save_json(str(path))
    loaded = load_json(str(path))
    assert loaded == record
    assert record["result"]["match_winner"] == game.match_winner

    replayed = rebuild(record)
    assert replayed.log == game.log
    assert replayed.match_winner == game.match_winner
    assert replayed.levels == game.levels
    assert replayed.finish_order == game.finish_order


def test_recorder_rejects_midmatch_attach():
    g = GuandanGame(rng=random.Random(5))
    g.play(0, [g.hands[0][0]])
    rec = Recorder(seed=5)
    try:
        rec.attach(g)
        assert False, "应拒绝中途挂载"
    except ValueError:
        pass


# ---------------------------------------------------------------- 座位解析
def test_resolve_seat_compat():
    # 旧配置：无 seats 键 → 全局默认
    assert resolve_seat({"ai_strength": "high"}, 1) == "classic_high"
    assert resolve_seat({"ai_strength": "low"}, 3) == "classic_low"
    # seats 覆盖优先，未覆盖座位回退全局
    s = {"ai_strength": "high", "seats": {"1": "classic_low", "2": None}}
    assert resolve_seat(s, 1) == "classic_low"
    assert resolve_seat(s, 2) == "classic_high"   # None → 全局
    assert resolve_seat(s, 3) == "classic_high"   # 缺失 → 全局
    # 未知 sid / 非 dict seats → 回退全局
    s2 = {"ai_strength": "low", "seats": {"1": "dmc"}}
    assert resolve_seat(s2, 1) == "classic_low"
    assert resolve_seat({"ai_strength": "high", "seats": "bad"}, 1) == "classic_high"
    # 全局「夯」与座位覆盖 pimc（阶段五）
    assert resolve_seat({"ai_strength": "pimc"}, 1) == "pimc"
    assert resolve_seat({"ai_strength": "high", "seats": {"1": "pimc"}}, 1) == "pimc"


def test_settings_json_seats_roundtrip(tmp_path):
    """seats 写入/读回 JSON：None 存为 null，load 不丢键。"""
    data = {"ai_strength": "high",
            "seats": {"1": None, "2": "classic_low", "3": None}}
    p = tmp_path / "s.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    loaded = json.loads(p.read_text(encoding="utf-8"))
    assert loaded["seats"]["1"] is None
    assert loaded["seats"]["2"] == "classic_low"
    assert resolve_seat(loaded, 2) == "classic_low"
