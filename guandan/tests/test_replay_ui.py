"""阶段四复盘回归：ReplaySession 步进/回退/跳局与 rebuild 前缀一致 + 保存读回。"""
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai
from ai import AIProfile, HIGH_FEATURES, choose_tribute, choose_return
from game import GuandanGame
from replay import Recorder, ReplaySession, rebuild


PROFILES = [None, AIProfile("aggressive"), AIProfile("balanced"),
            AIProfile("conservative")]


def _drive(game) -> None:
    """AI 整场自动打完（classic_high 增强，与阶段一驱动一致）。"""
    while game.phase != "match_over":
        if game.phase == "round_over":
            game.start_next_round()
            continue
        if game.phase == "tribute":
            pending = [(g, r) for g, r in game.tribute_pairs
                       if g not in game.tribute_done]
            if pending:
                giver = pending[0][0]
                game.submit_tribute(giver, choose_tribute(game, giver))
            else:
                receiver = next(r for r in game.tribute_done.values()
                                if r not in game.return_done)
                game.submit_return(receiver, choose_return(game, receiver))
            continue
        pid = game.turn
        kind, cards = ai.choose_action(game, pid, PROFILES[pid],
                                       enhanced=True, features=HIGH_FEATURES)
        if kind == "play":
            game.play(pid, cards)
        else:
            game.pass_play(pid)


def _make_record(seed: int):
    rec = Recorder(seed=seed)
    g = GuandanGame(rng=random.Random(seed))
    rec.attach(g)
    _drive(g)
    return rec.to_dict(), g


def _snapshot(g: GuandanGame) -> tuple:
    """公开可比较状态快照（含 log 全文，逐字节等价判定）。"""
    return (tuple(g.log), g.phase, g.round_number, g.turn,
            g.match_winner, tuple(g.finish_order),
            tuple(sorted(g.levels.items())),
            tuple(len(g.hands[p]) for p in range(4)),
            len(g.played_cards), tuple(sorted(g.trick_displays)),
            tuple(sorted(g.tribute_done.items())),
            tuple(sorted(g.return_done.items())))


# ---------------------------------------------------------------- 步进
def test_session_forward_all_matches_rebuild():
    record, g = _make_record(99)
    s = ReplaySession(record)
    assert s.total() == len(record["actions"])
    s.goto_end()
    assert s.index == s.total()
    assert _snapshot(s.game) == _snapshot(g)


def test_goto_index_prefix_matches_rebuild_stop_after():
    """前缀重演一致性：goto_index(k) 状态 == rebuild(stop_after=k)。"""
    record, _ = _make_record(7)
    n = len(record["actions"])
    for k in (0, 1, 7, n // 2, n - 1, n):
        s = ReplaySession(record)
        s.goto_index(k)
        assert _snapshot(s.game) == _snapshot(rebuild(record, stop_after=k)), k


def test_back_then_forward_consistent():
    record, g = _make_record(2024)
    s = ReplaySession(record)
    for _ in range(10):
        s.forward()
    mid = _snapshot(s.game)
    s.back()
    s.back()
    s.back()
    assert _snapshot(s.game) != mid
    s.goto_end()
    assert _snapshot(s.game) == _snapshot(g)


def test_goto_round_state_and_continuation():
    record, g = _make_record(42)
    rounds = sorted({a.get("round", 1) for a in record["actions"]})
    assert len(rounds) >= 1
    for r in rounds:
        s = ReplaySession(record)
        s.goto_round(r)
        assert s.game.round_number == r
        first = next(i for i, a in enumerate(record["actions"])
                     if a.get("round", 1) == r)
        expected = first + (1 if record["actions"][first]["kind"] == "round_start"
                            else 0)
        assert s.index == expected, r
        s.goto_end()
        assert _snapshot(s.game) == _snapshot(g)


def test_goto_round_out_of_range_clamps():
    record, _ = _make_record(5)
    s = ReplaySession(record)
    max_r = max(a.get("round", 1) for a in record["actions"])
    s.goto_round(max_r + 10)
    assert s.game.round_number == max_r
    s.goto_round(-3)
    assert s.game.round_number == 1


# ---------------------------------------------------------------- 存取
def test_save_and_list_roundtrip(tmp_path, monkeypatch):
    import replay_ui
    monkeypatch.setattr(replay_ui, "REPLAYS_DIR", str(tmp_path))
    record, g = _make_record(5)
    p1 = replay_ui.save_replay(record)
    assert p1.startswith(str(tmp_path))
    p2 = replay_ui.save_replay(record, prev=p1)   # 同场重复保存覆盖
    assert p2 == p1
    items = replay_ui.list_replays()
    assert len(items) == 1
    assert items[0]["name"].startswith("#001 ")
    s = ReplaySession(replay_ui.load_json(items[0]["path"]))
    s.goto_end()
    assert _snapshot(s.game) == _snapshot(g)


def test_list_replays_skips_corrupt(tmp_path, monkeypatch):
    import replay_ui
    monkeypatch.setattr(replay_ui, "REPLAYS_DIR", str(tmp_path))
    record, _ = _make_record(3)
    replay_ui.save_replay(record)
    (tmp_path / "match_broken.json").write_text("{bad json", encoding="utf-8")
    (tmp_path / "readme.txt").write_text("ignore", encoding="utf-8")
    items = replay_ui.list_replays()
    assert len(items) == 1


def test_session_rejects_seedless_record():
    try:
        ReplaySession({"seed": None, "actions": []})
        assert False, "无 seed 应拒绝"
    except ValueError:
        pass


# ---------------------------------------------------------------- 命名（方案 A）
def test_replay_basename_fields():
    import replay_ui
    record, g = _make_record(11)
    name = replay_ui.replay_basename(record, 3)
    segs = name.split(" ")
    assert len(segs) == 5
    assert segs[0] == "#003"
    assert segs[2] == "我方打" + _rank_of(record, 0)
    assert segs[3] == ("我方胜" if record["result"]["match_winner"] == 0
                       else "我方负")
    fo = record["result"]["rounds"][-1]["finish_order"]
    ranks = ("头游", "二游", "三游", "末游")
    assert segs[4] == ranks[fo.index(0)] + ranks[fo.index(2)]
    # 未完赛：不显示胜负与名次字段
    part = {**record, "result": {**record["result"], "match_winner": None}}
    assert len(replay_ui.replay_basename(part, 3).split(" ")) == 3


def _rank_of(record, team):
    from cards import RANK_LABELS
    lv = record["result"]["levels"] or record["levels"]
    return RANK_LABELS[int(lv[str(team)])]


def test_save_replay_seq_no_and_rename(tmp_path, monkeypatch):
    import replay_ui
    monkeypatch.setattr(replay_ui, "REPLAYS_DIR", str(tmp_path))
    rec, _ = _make_record(5)
    p1 = replay_ui.save_replay(rec)
    assert "#001 " in p1
    p2 = replay_ui.save_replay(rec)           # 新一场 → #002
    assert "#002 " in p2 and p2 != p1
    # 同场重存：编号/日期沿用；赛果变化则改名并删旧
    rec2 = {**rec, "result": {**rec["result"], "match_winner": 1}}
    p3 = replay_ui.save_replay(rec2, prev=p2)
    assert p3 != p2 and not os.path.exists(p2)
    assert "#002 " in p3 and "我方负" in p3
    assert len(replay_ui.list_replays()) == 2


def test_list_replays_old_to_new(tmp_path, monkeypatch):
    import os as _os
    import replay_ui
    monkeypatch.setattr(replay_ui, "REPLAYS_DIR", str(tmp_path))
    rec, _ = _make_record(5)
    pa = replay_ui.save_replay(rec)
    pb = replay_ui.save_replay(rec)
    now = _t_time()
    _os.utime(pa, (now - 100, now - 100))      # 强制 pa 更早
    _os.utime(pb, (now, now))
    items = replay_ui.list_replays()
    assert [i["path"] for i in items] == [pa, pb]


def _t_time():
    import time
    return time.time()


# ---------------------------------------------------------------- 轮次/顺序号
def test_trick_meta_consistency():
    record, _ = _make_record(7)
    s = ReplaySession(record)
    assert len(s.meta) == len(s.actions)
    prev_seq = 0
    prev_trick = 0
    cur_round = None
    for i, a in enumerate(s.actions):
        m = s.meta[i]
        if a.get("round", 1) != cur_round:     # 跨局：轮次归零
            cur_round = a.get("round", 1)
            prev_trick = 0
        if a["kind"] in ("round_start", "tribute", "return"):
            assert m["seq"] is None and m["trick"] is None
            continue
        assert m["trick"] >= prev_trick        # 轮次只增
        assert m["trick"] == prev_trick or a["kind"] == "play"  # 只有出牌开新轮
        if m["trick"] != prev_trick:           # 新轮：编号从 1 重计
            assert m["seq"] == 1
        else:
            assert m["seq"] == prev_seq + 1    # 轮内逐次 +1（过牌也计数）
        prev_seq, prev_trick = m["seq"], m["trick"]


def test_next_prev_trick_roundtrip():
    record, _ = _make_record(7)
    s = ReplaySession(record)
    t = s.next_trick_target()
    assert t is not None and t > 0
    s.goto_index(t)
    # 已到第 1 轮收牌点：上一轮回本轮起点（首局 0）
    assert s.prev_trick_target() == 0
    s.goto_index(0)
    t2 = s.next_trick_target()
    assert t2 == t
    # 播放中途：下一轮目标 = 第 2 轮结束点（> 第 1 轮结束点）
    s.goto_index(t)
    t3 = s.next_trick_target()
    assert t3 is not None and t3 > t


# ---------------------------------------------------------------- 理由字段
def test_reason_field_roundtrip(tmp_path, monkeypatch):
    import replay_ui
    monkeypatch.setattr(replay_ui, "REPLAYS_DIR", str(tmp_path))
    record, _ = _make_record(5)
    plays = [a for a in record["actions"] if a["kind"] == "play"]
    plays[0]["reason"] = "压住对家，夺回牌权"
    p = replay_ui.save_replay(record)
    back = replay_ui.load_json(p)
    got = [a for a in back["actions"] if a.get("reason")]
    assert got and got[0]["reason"] == "压住对家，夺回牌权"


def test_explain_choice_returns_text():
    import ai
    g = GuandanGame(rng=random.Random(9))
    kind, cards = ai.choose_action(g, 1, AIProfile("balanced"),
                                   enhanced=True, features=HIGH_FEATURES)
    txt = ai.explain_choice(g, 1, kind, cards)
    assert isinstance(txt, str) and txt
    g.play(0, ai.choose_action(g, 0, None)[1])   # 进入跟牌场景
    pid = g.turn
    kind2, cards2 = ai.choose_action(g, pid, None)
    assert isinstance(ai.explain_choice(g, pid, kind2, cards2), str)
