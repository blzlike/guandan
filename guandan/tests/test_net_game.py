"""好友联机阶段三：牌局动作流单测（room.py 镜像广播 / seq / resync / 接管）。

不依赖 Kivy：直接驱动 RoomHost（权威引擎 + 事件镜像）与 RoomClient
（动作上行 / 广播套用），覆盖大纲验收项——锁步一致性对拍、非法动作拒绝、
resync 前缀重演一致、掉线 AI 接管与重连归位。
"""
import queue
import random
import time

import pytest

import ai
from game import GuandanGame
from replay import apply_net_act
from room import RoomClient, RoomHost

HOST = "127.0.0.1"


# ---------------------------------------------------------------- 工具

def _pump_until(host, client, pred, timeout=5.0, drain=None):
    """交替泵两端直到 pred 成立（deadline 轮询，禁裸 sleep）。

    ``drain``：每轮泵取后回调（通常为 ``_drain_guest``），把客户端事件
    队列中的 act/resync/act_ack 等套用或收集进 state，供 pred 观测。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        host.pump()
        if client is not None:
            client.pump()
            if drain is not None:
                drain()
        if pred():
            return True
    return False


def _await_sync(host, client, gg, state, timeout=5.0):
    """泵两端直到客人镜像套用追平主机动作流（TCP 异步送达需等待）。"""
    return _pump_until(host, client,
                       lambda: client.applied_seq == len(host.actions),
                       timeout=timeout,
                       drain=lambda: _drain_guest(client, gg, state))


def _make_pair(name="客人"):
    host = RoomHost("主机", advertise=False, port=0)
    client = RoomClient(name)
    assert client.join(HOST, host.port)
    assert _pump_until(host, client, lambda: client.welcomed)
    return host, client


def _build_games(host, client, seed):
    """两端按 start 消息同 seed 建局（对齐 UI net_setup 的最小路径）。

    构造即完成首局发牌（game.__init__ 自动 start_match），与 net_setup
    一致；勿再显式 start_match（会 rng 续洗第二副牌，破坏 seed→发牌确定性）。
    """
    hg = GuandanGame(rng=random.Random(seed))
    host.set_game(hg)
    gg = GuandanGame(rng=random.Random(seed))
    return hg, gg


def _drain_guest(client, gg, state):
    """泵出客户端事件：act 套用镜像 / resync 重演 / 提示收集。"""
    while True:
        try:
            kind, _peer, ev = client.events.get_nowait()
        except queue.Empty:
            return
        if kind == "act":
            if not client.ingest_act(ev):
                continue              # 重复帧/真断档：与 UI 闸门同口径
            apply_net_act(gg, ev)
            client.mark_applied()
        elif kind == "resync":
            state["resync"] = ev
        elif kind == "act_rejected":
            state["rejected"].append(ev)
        elif kind == "ai_takeover":
            state["takeover"].append(ev)
        elif kind == "started":
            state["started"].append(ev)
        elif kind == "end":
            state.setdefault("ends", []).append(ev)   # 阶段四：赛末复盘分发


def _snap(g):
    """引擎公开状态逐字段快照（锁步对拍口径）。"""
    return {
        "round": g.round_number,
        "phase": g.phase,
        "turn": g.turn,
        "levels": dict(g.levels),
        "hands": {p: sorted(c.eid for c in g.hands[p]) for p in range(4)},
        "played": len(g.played_cards),
        "finish": list(g.finish_order),
        "winner": g.match_winner,
        "last": None if g.last_play is None else
                (g.last_play[0], g.last_play[1].type.name,
                 sorted(c.eid for c in g.last_play[1].cards)),
        "trick": {p: (d["action"], None if d.get("combo") is None else
                      sorted(c.eid for c in d["combo"].cards))
                  for p, d in g.trick_displays.items()},
        "tdisp": {p: c["card"].eid for p, c in g.tribute_displays.items()},
        "pairs": [list(x) for x in g.tribute_pairs],
        "tdone": dict(g.tribute_done),
        "rdone": dict(g.return_done),
        "refused": sorted(g.tribute_refused_by),
    }


def _host_ai_step(hg):
    """主机代打一个动作（AI 座位行牌；贡牌期全座位由主机驱动）。

    行牌前调 begin_round 清开局展示——镜像 UI 主机端「还贡展示保留期
    结束后 begin_round，再进入首个行牌动作」的时机，与客户端
    apply_net_act「首个行牌前就地清除」对齐。
    """
    g = hg
    if g.phase == "playing":
        g.begin_round()
        pid = g.turn
        kind, cards = ai.choose_action(g, pid, ai.AIProfile("balanced"))
        if kind == "play":
            g.play(pid, cards)
        else:
            g.pass_play(pid)
        return True
    if g.phase == "tribute":
        pending = [(a, b) for a, b in g.tribute_pairs if a not in g.tribute_done]
        if pending:
            gg0 = pending[0][0]
            g.submit_tribute(gg0, ai.choose_tribute(g, gg0))
            return True
        awaiting = [r for r in g.tribute_done.values() if r not in g.return_done]
        if awaiting:
            r0 = awaiting[0]
            g.submit_return(r0, ai.choose_return(g, r0))
            return True
    return False


def _play_full_match(host, client, hg, gg, state, max_steps=20000,
                     stop_pred=None):
    """主机 AI 打完全场，客人逐步镜像套用；每个动作步追平后断言锁步一致。

    跳局（start_next_round）不产生动作流，客户端靠下一动作的 round 跳变
    自动追平（apply_action），故跳局步只泵事件、不做状态对比。
    """
    steps = 0
    while hg.phase != "match_over" and steps < max_steps:
        if stop_pred is not None and stop_pred():
            break
        if not _host_ai_step(hg):
            assert hg.phase == "round_over", f"卡死在 {hg.phase}"
            hg.start_next_round()          # 主机驱动开下一局（客人按回合号追平）
            _drain_guest(client, gg, state)
            continue
        _await_sync(host, client, gg, state)
        steps += 1
        assert _snap(hg) == _snap(gg), f"第 {steps} 步后锁步失步"
    assert hg.phase == "match_over" or (stop_pred is not None and stop_pred())
    _drain_guest(client, gg, state)
    assert client.applied_seq == len(host.actions)
    assert _snap(hg) == _snap(gg)


# ---------------------------------------------------------------- 用例

def test_lockstep_full_match_consistency():
    """锁步一致性对拍：整场 AI 对打，两端引擎状态逐字段一致（多 seed）。"""
    for seed in (19, 20261018, 777):
        host, client = _make_pair()
        host.start_game(levels=[2, 2], seed=seed)
        assert _pump_until(host, client, lambda: client.started)
        hg, gg = _build_games(host, client, seed)
        state = {"rejected": [], "takeover": [], "started": [], "resync": None}
        _play_full_match(host, client, hg, gg, state)
        host.close()
        client.close()


def test_illegal_action_rejected():
    """非法动作拒绝：非我回合 / 伪冒座位 / 过期回合均回执且不动权威引擎。"""
    host, client = _make_pair()
    seed = 19
    host.start_game(levels=[2, 2], seed=seed)
    assert _pump_until(host, client, lambda: client.started)
    hg, gg = _build_games(host, client, seed)
    state = {"rejected": [], "takeover": [], "started": [], "resync": None}
    _host_ai_step(hg)                      # 让主机先走一步（turn 离开 0）
    _await_sync(host, client, gg, state)
    before_actions = len(host.actions)
    before_snap = _snap(hg)
    mine = [c.eid for c in hg.hands[1]]
    turn0 = hg.turn == 1
    if not turn0:                          # 造一个「非我回合」的 play
        client.send_act("play", eids=mine, round=hg.round_number)
    else:                                  # 轮到我时改用过期回合号
        client.send_act("play", eids=mine, round=hg.round_number + 5)
    assert _pump_until(host, client, lambda: bool(state["rejected"]),
                       drain=lambda: _drain_guest(client, gg, state))
    assert state["rejected"], "非法动作未收到 act_ack 回执"
    assert len(host.actions) == before_actions
    assert _snap(hg) == before_snap
    # 伪冒他人座位：pid 与座位不符
    client._cli.send({"t": "act", "kind": "play", "pid": 2,
                      "round": hg.round_number, "eids": mine})
    assert _pump_until(host, client, lambda: len(state["rejected"]) >= 2,
                       drain=lambda: _drain_guest(client, gg, state))
    assert len(host.actions) == before_actions
    host.close()
    client.close()


def test_resync_rebuild_consistency():
    """resync：手动请求与 seq 断档自动请求均全量重演追平且状态一致。"""
    host, client = _make_pair()
    seed = 11
    host.start_game(levels=[2, 2], seed=seed)
    assert _pump_until(host, client, lambda: client.started)
    hg, gg = _build_games(host, client, seed)
    state = {"rejected": [], "takeover": [], "started": [], "resync": None}
    for _ in range(30):                    # 打一段
        if hg.phase == "match_over":
            break
        if not _host_ai_step(hg):
            hg.start_next_round()
        _await_sync(host, client, gg, state)
    mid_snap = _snap(hg)
    # 1) 手动 resync_req → 全量动作流 → 前缀重演重建一致
    assert client.request_resync()
    assert _pump_until(host, client, lambda: state["resync"] is not None,
                       drain=lambda: _drain_guest(client, gg, state))
    payload = state["resync"]
    assert payload["seed"] == seed
    assert len(payload["actions"]) == len(host.actions)
    g2 = GuandanGame(rng=random.Random(payload["seed"]))
    g2.levels = {0: payload["levels"][0], 1: payload["levels"][1]}
    for act in payload["actions"]:
        apply_net_act(g2, act)
    assert _snap(g2) == mid_snap
    # 2) seq 断档（模拟漏收）：applied 回零后下一动作自动触发 resync
    state["resync"] = None
    client._applied = 0
    for _ in range(50):                    # 驱动到确实产生新动作
        if _host_ai_step(hg):
            break
        assert hg.phase == "round_over"
        hg.start_next_round()
    else:
        pytest.fail("未能产生新动作触发 seq 断档")
    assert _pump_until(host, client, lambda: state["resync"] is not None,
                       drain=lambda: _drain_guest(client, gg, state))
    g3 = GuandanGame(rng=random.Random(state["resync"]["seed"]))
    g3.levels = {0: state["resync"]["levels"][0], 1: state["resync"]["levels"][1]}
    for act in state["resync"]["actions"]:
        apply_net_act(g3, act)
    assert _snap(g3) == _snap(hg)
    host.close()
    client.close()


def test_disconnect_takeover_and_rejoin():
    """掉线：座位保留等待重连，超时 AI 接管广播；重连昵称匹配归位 resync。"""
    host, client = _make_pair()
    host._takeover_wait = 0.05             # 测试用极短等待
    seed = 5
    host.start_game(levels=[2, 2], seed=seed)
    assert _pump_until(host, client, lambda: client.started)
    hg, gg = _build_games(host, client, seed)
    state = {"rejected": [], "takeover": [], "started": [], "resync": None}
    for _ in range(5):
        _host_ai_step(hg)
        _drain_guest(client, gg, state)
    # 掉线：座位保留（不清座、不广播 seats 变化）
    seat = client.my_seat
    name = "客人"
    client.close()
    assert _pump_until(host, None, lambda: seat in host._down, timeout=5)
    assert host._seats[seat]["name"] == name
    assert host._seats[seat]["is_ai"] is False
    assert seat not in host.ai_pids()
    # 超时 → AI 接管：广播 ai_takeover + 座位进入代打集合
    assert _pump_until(host, None, lambda: seat in host._taken, timeout=5)
    assert seat in host.ai_pids()
    # 打几步：接管座位由主机正常代打，动作流继续单调
    n0 = len(host.actions)
    for _ in range(10):
        if hg.phase == "match_over":
            break
        if not _host_ai_step(hg):
            hg.start_next_round()
    assert len(host.actions) > n0
    # 重连归位：昵称精确匹配 → welcome + resync（含全部动作）→ 座位还原
    client2 = RoomClient(name)
    assert client2.join(HOST, host.port)
    assert _pump_until(host, client2, lambda: client2.my_seat == seat
                       and client2.applied_seq == len(host.actions))
    assert seat not in host.ai_pids()          # 归位后恢复人类
    g3 = GuandanGame(rng=random.Random(seed))
    for act in host.actions:
        apply_net_act(g3, act)
    assert _snap(g3) == _snap(hg)
    host.close()
    client2.close()


def test_act_burst_no_false_resync():
    """连发动作积压（UI 泵滞后）不得误触发 resync：按序入队套用即追平。

    回归：客人出牌回执与 AI 连发动作在同一泵取批次到达时，旧实现按
    applied+1 硬比对误判断档 → 假 resync → 客人端 net_setup 重建清空
    整理手牌（「整理后被自动恢复」根因）。
    """
    host, client = _make_pair()
    host.start_game(levels=[2, 2], seed=19)
    assert _pump_until(host, client, lambda: client.started)
    hg, gg = _build_games(host, client, 19)
    state = {"rejected": [], "takeover": [], "started": [], "resync": None}
    _host_ai_step(hg)
    _host_ai_step(hg)                     # 连发两帧（seq=1、seq=2）
    assert len(host.actions) >= 2
    # 泵到两帧都入队（期间不 drain——模拟 UI 泵滞后）
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        client.pump()
        queued = [k for k, _p, _e in client.events.queue]
        if queued.count("act") >= 2:
            break
        time.sleep(0.01)
    queued = [k for k, _p, _e in client.events.queue]
    assert queued.count("act") >= 2, f"两帧未入队: {queued}"
    assert not client._resync_pending, "积压帧被误判断档触发 resync"
    assert "resync" not in queued
    _drain_guest(client, gg, state)       # UI 按序套用
    assert state["resync"] is None
    assert client.applied_seq == len(host.actions)
    assert _snap(hg) == _snap(gg)
    host.close()
    client.close()


def test_true_gap_resync_via_ingest_gate():
    """真断档（已入队动作帧丢失）：套用闸门自动 resync 全量重演自愈。"""
    host, client = _make_pair()
    host.start_game(levels=[2, 2], seed=11)
    assert _pump_until(host, client, lambda: client.started)
    hg, gg = _build_games(host, client, 11)
    state = {"rejected": [], "takeover": [], "started": [], "resync": None}
    _host_ai_step(hg)
    _await_sync(host, client, gg, state)  # seq=1 已套用（applied=1）
    _host_ai_step(hg)                     # seq=2 广播，只泵不入队消费
    _pump_until(host, client, lambda: False, timeout=0.3)  # 等帧送达事件队列
    # 从事件队列丢弃一条 act（模拟真丢帧：帧已消费但未套用）
    dropped = False
    kept = []
    while True:
        try:
            item = client.events.get_nowait()
        except queue.Empty:
            break
        if not dropped and item[0] == "act":
            dropped = True
            continue
        kept.append(item)
    for item in kept:
        client.events.put(item)
    assert dropped, "未找到可丢弃的 act 事件"
    _host_ai_step(hg)                     # seq=3：套用闸门应发现断档
    assert _pump_until(host, client, lambda: state["resync"] is not None,
                       drain=lambda: _drain_guest(client, gg, state))
    payload = state["resync"]
    g3 = GuandanGame(rng=random.Random(payload["seed"]))
    g3.levels = {0: payload["levels"][0], 1: payload["levels"][1]}
    for act in payload["actions"]:
        apply_net_act(g3, act)
    assert _snap(g3) == _snap(hg)
    assert client.applied_seq == len(host.actions)
    host.close()
    client.close()


def test_next_match_broadcasts_new_start():
    """再来一局：主机广播新 start（新 seed），动作流清零、客人收到新开局。"""
    host, client = _make_pair()
    host.start_game(levels=[2, 2], seed=111)
    assert _pump_until(host, client, lambda: client.started)
    hg, gg = _build_games(host, client, 111)
    state = {"rejected": [], "takeover": [], "started": [], "resync": None}
    for _ in range(3):
        _host_ai_step(hg)
        _drain_guest(client, gg, state)
    n0 = len(host.actions)
    levels = [3, 4]
    state["started"].clear()               # 丢弃开局时的旧 started 事件
    host.next_match(levels, seed=222)
    assert _pump_until(host, client, lambda: len(state["started"]) == 1,
                       drain=lambda: _drain_guest(client, gg, state))
    ev = state["started"][0]
    assert ev["seed"] == 222
    assert ev["levels"] == levels
    assert host.actions == []              # 新一场动作流从零计
    hg2, gg2 = _build_games(host, client, 222)
    hg2.levels = {0: levels[0], 1: levels[1]}
    gg2.levels = {0: levels[0], 1: levels[1]}
    assert _snap(hg2) == _snap(gg2)
    host.close()
    client.close()


def test_tribute_flow_via_network():
    """进贡/还贡/接风全流程走网络动作流：两端状态一致且 seq 连续。"""
    # 多试几个 seed，找到确实发生进贡的一局（每 seed 独立 pair，start 一次性）
    for seed in (19, 11, 5, 88, 1234):
        host, client = _make_pair()
        host.start_game(levels=[2, 2], seed=seed)
        assert _pump_until(host, client, lambda: client.started)
        hg, gg = _build_games(host, client, seed)
        state = {"rejected": [], "takeover": [], "started": [], "resync": None}
        _play_full_match(
            host, client, hg, gg, state,
            stop_pred=lambda: hg.round_number >= 2 and hg.tribute_done
            and len(hg.return_done) == len(hg.tribute_done))
        host.close()
        client.close()
        if hg.round_number >= 2 and hg.tribute_done:
            return
    pytest.fail("多 seed 均未触发进贡场景")


def test_host_validates_acts_against_round():
    """客人上送未来回合号的动作被拒（防 apply_action 的重演式跳局被滥用）。"""
    host, client = _make_pair()
    seed = 42
    host.start_game(levels=[2, 2], seed=seed)
    assert _pump_until(host, client, lambda: client.started)
    hg, gg = _build_games(host, client, seed)
    state = {"rejected": [], "takeover": [], "started": [], "resync": None}
    mine = [c.eid for c in hg.hands[client.my_seat]]
    client.send_act("play", eids=mine, round=hg.round_number + 1)
    assert _pump_until(host, client, lambda: bool(state["rejected"]),
                       drain=lambda: _drain_guest(client, gg, state))
    assert hg.round_number == 1            # 未被强制跳局
    host.close()
    client.close()


def test_end_record_distribution():
    """赛末 end 分发（阶段四）：客人收到完整 record；记录可独立重演出终局；
    赛末后重连归位的客人补收同一条记录。"""
    from replay import Recorder, rebuild
    host, client = _make_pair()
    host._takeover_wait = 30.0             # 赛末重连窗口内不转 AI 接管
    seed = 19
    host.start_game(levels=[2, 2], seed=seed)
    assert _pump_until(host, client, lambda: client.started)
    hg, gg = _build_games(host, client, seed)
    rec = Recorder(seed=seed)
    rec.attach(hg)                         # 主机权威记录（比赛起点挂载）
    state = {"rejected": [], "takeover": [], "started": [], "resync": None}
    _play_full_match(host, client, hg, gg, state)
    assert hg.phase == "match_over"
    assert rec.match_winner == hg.match_winner
    # UI 赛末跃迁分发：广播 end → 客人事件队列收到完整 record
    host.broadcast_end(rec.to_dict())
    assert _pump_until(host, client, lambda: state.get("ends"),
                       drain=lambda: _drain_guest(client, gg, state))
    record = state["ends"][0]
    assert record["seed"] == seed
    assert record["result"]["match_winner"] == hg.match_winner
    # 分发记录自足：任意一端 rebuild 后终局结果与权威引擎一致（复盘可用）
    g2 = rebuild(record)
    assert g2.phase == "match_over"
    assert g2.match_winner == hg.match_winner
    assert list(g2.finish_order) == list(hg.finish_order)
    # 赛末重连：掉线座位重连归位后补收同一条 end 记录（常规广播时不在场）
    seat = client.my_seat
    client.close()
    assert _pump_until(host, None, lambda: seat in host._down, timeout=5)
    client2 = RoomClient("客人")
    assert client2.join(HOST, host.port)
    ends = []

    def drain2():
        while True:
            try:
                kind, _peer, ev = client2.events.get_nowait()
            except queue.Empty:
                return
            if kind == "end":
                ends.append(ev)

    assert _pump_until(host, client2,
                       lambda: client2.my_seat == seat and bool(ends),
                       drain=drain2)
    assert ends[0] == record
    host.close()
    client2.close()


def test_teaching_on_guest_mirror():
    """教学面板联机可用（阶段四）：客人镜像上 PlayTracker 已出牌统计与
    剩牌推测行正常产出，方位标签按本端座位换算。"""
    from observer import PlayTracker
    host, client = _make_pair()
    seed = 777
    host.start_game(levels=[2, 2], seed=seed)
    assert _pump_until(host, client, lambda: client.started)
    hg, gg = _build_games(host, client, seed)
    tr = PlayTracker()
    tr.attach(gg)                          # 客人在比赛起点挂教学跟踪
    state = {"rejected": [], "takeover": [], "started": [], "resync": None}
    _play_full_match(host, client, hg, gg, state)
    me = client.my_seat
    # 已出牌统计与镜像动作流一致（PlayTracker 按 round_start 重置，为
    # 「本局已出牌」口径：与最后一局的 play 动作数对齐）
    last_round = hg.round_number
    for pid in range(4):
        n_play = sum(1 for a in host.actions
                     if a["kind"] == "play" and a["pid"] == pid
                     and a["round"] == last_round)
        assert sum(tr.played[pid]) == n_play
    # 剩牌推测：3 行（对家/上家/下家），方位与 (对手座位-我) mod 4 换算一致
    rows = tr.predict_rows(gg, me)
    assert isinstance(rows, list) and len(rows) == 3
    pos_of = {"对家": 2, "上家": 3, "下家": 1}
    for row in rows:
        assert set(row) == {"pid", "label", "text"}
        assert (me + pos_of[row["label"]]) % 4 == row["pid"]
        assert row["text"]
    host.close()
    client.close()
