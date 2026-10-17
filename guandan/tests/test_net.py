"""net.py 传输层测试（好友联机阶段一）。

全部走 127.0.0.1 回环、短超时，不依赖外网；
等待一律用 deadline 轮询（queue.get 限时 / 周期轮询），禁止裸 sleep 等结果。
"""

import contextlib
import json
import queue
import socket
import struct
import threading
import time

import pytest

from net import (
    DEFAULT_PORT,
    DISCOVER_PORT,
    HEARTBEAT_INTERVAL,
    HEARTBEAT_TIMEOUT,
    MAX_FRAME,
    PROTOCOL_VER,
    FrameDecoder,
    NetClient,
    NetServer,
    encode_frame,
)


# ---------- 辅助 ----------

def wait_event(q, kind, timeout=5.0):
    """阻塞等待指定 kind 的事件，返回 (事件, 途中扫过的全部事件列表)。"""
    deadline = time.monotonic() + timeout
    seen = []
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            ev = q.get(timeout=min(0.1, max(0.001, remaining)))
        except queue.Empty:
            continue
        seen.append(ev)
        if ev[0] == kind:
            return ev, seen
    raise AssertionError(f"等待事件 {kind} 超时，已见事件：{seen}")


def collect_messages(q, n, timeout=5.0):
    """收集 n 条 message 载荷（跳过其它事件），不足则超时报错。"""
    deadline = time.monotonic() + timeout
    msgs = []
    while len(msgs) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"收集消息超时：{len(msgs)}/{n}")
        try:
            kind, _peer, payload = q.get(timeout=min(0.1, remaining))
        except queue.Empty:
            continue
        if kind == "message":
            msgs.append(payload)
    return msgs


@contextlib.contextmanager
def running_server(**kw):
    kw.setdefault("bind_ip", "127.0.0.1")
    kw.setdefault("port", 0)
    srv = NetServer(**kw)
    try:
        yield srv
    finally:
        srv.close()


@contextlib.contextmanager
def running_client(host, port, **kw):
    cli = NetClient(host, port, **kw)
    try:
        yield cli
    finally:
        cli.close()


# ---------- 协议常量 ----------

def test_protocol_constants():
    """钉住协议常量，防止无意变更破坏兼容。"""
    assert PROTOCOL_VER == 1
    assert DEFAULT_PORT == 17666
    assert DISCOVER_PORT == 17667
    assert HEARTBEAT_INTERVAL == 5.0
    assert HEARTBEAT_TIMEOUT == 15.0
    assert MAX_FRAME == 4 * 1024 * 1024


# ---------- 帧编解码 ----------

def test_frame_roundtrip():
    decoder = FrameDecoder()
    cases = [
        {"t": "act", "kind": "play", "note": "黑桃♠红桃♥梅花♣方块♦"},
        {},
        {"t": "seats", "seats": [{"seat": i, "name": f"玩家{i}"} for i in range(4)]},
        {"t": "big", "pad": "甲" * 200_000},   # 大 payload（UTF-8 下约 600KB）
    ]
    for msg in cases:
        assert decoder.feed(encode_frame(msg)) == [msg]


def test_encode_oversize_rejected():
    with pytest.raises(ValueError):
        encode_frame({"t": "x", "pad": "A" * (MAX_FRAME + 1)})


def test_decoder_sticky():
    d = FrameDecoder()
    f1 = encode_frame({"n": 1})
    f2 = encode_frame({"n": 2, "t": "act"})
    assert d.feed(f1 + f2) == [{"n": 1}, {"n": 2, "t": "act"}]   # 粘包一次全出
    assert d.feed(b"") == []


def test_decoder_partial():
    d = FrameDecoder()
    frame = encode_frame({"t": "act", "kind": "pass", "n": 7})
    got = []
    for i in range(0, len(frame), 3):      # 半包：3 字节一段逐段喂入
        got.extend(d.feed(frame[i:i + 3]))
    assert got == [{"t": "act", "kind": "pass", "n": 7}]


def test_decoder_oversize_length():
    d = FrameDecoder()
    with pytest.raises(ValueError):
        d.feed(struct.pack(">I", MAX_FRAME + 1))


def test_decoder_bad_json():
    d = FrameDecoder()
    payload = b"\xff\xfe not json"
    with pytest.raises(ValueError):        # JSONDecodeError/UnicodeDecodeError 均为 ValueError 子类
        d.feed(struct.pack(">I", len(payload)) + payload)


# ---------- 集成：回环收发 ----------

def test_server_client_roundtrip():
    with running_server() as srv:
        assert srv.port > 0
        with running_client("127.0.0.1", srv.port) as cli:
            ev, _ = wait_event(srv.events, "connected")
            assert ev[1] == 1                    # 首个 peer_id 从 1 起
            assert ev[2][0] == "127.0.0.1"       # payload 为对端地址
            cev, _ = wait_event(cli.events, "connected")
            assert cev[1] is None                # 客户端事件 peer 恒为 None
            assert cli.send({"t": "join", "name": "甲"}) is True
            ev, _ = wait_event(srv.events, "message")
            assert ev[1] == 1
            assert ev[2] == {"t": "join", "name": "甲"}
            assert srv.send(1, {"t": "welcome", "pid": 1}) is True
            ev, _ = wait_event(cli.events, "message")
            assert ev[2] == {"t": "welcome", "pid": 1}


def test_client_disconnect_notifies_server():
    with running_server() as srv:
        with running_client("127.0.0.1", srv.port):
            wait_event(srv.events, "connected")
        # with 块退出即 client.close()
        ev, _ = wait_event(srv.events, "disconnected")
        assert ev[1] == 1
        assert ev[2] == "reset"
        assert srv.send(1, {"t": "x"}) is False   # 死连接发送返回 False


def test_server_close_notifies_client():
    with running_server() as srv:
        with running_client("127.0.0.1", srv.port) as cli:
            wait_event(cli.events, "connected")
            srv.close()                           # 主机退出
            ev, _ = wait_event(cli.events, "disconnected")
            assert ev[1] is None
            assert ev[2] == "reset"
            assert cli.send({"t": "x"}) is False
            assert srv.broadcast({"t": "x"}) == 0


def test_heartbeat_timeout_disconnects_silent_peer():
    with running_server(heartbeat_interval=0.05, heartbeat_timeout=0.25) as srv:
        dead = socket.create_connection(("127.0.0.1", srv.port), timeout=2)
        try:
            ev, _ = wait_event(srv.events, "connected")
            pid = ev[1]
            dead.settimeout(1.0)
            head = dead.recv(4)                   # 能收到服务端心跳 ping 帧
            (n,) = struct.unpack(">I", head)
            body = b""
            while len(body) < n:
                body += dead.recv(n - len(body))
            assert json.loads(body.decode("utf-8"))["t"] == "ping"
            ev, _ = wait_event(srv.events, "disconnected", timeout=3)
            assert ev[1] == pid
            assert ev[2] == "timeout"             # 静默超时判死
            assert srv.send(pid, {"t": "x"}) is False
        finally:
            dead.close()


def test_ping_pong_not_leaked_and_connection_stays():
    """正常应答心跳的连接不判死；ping/pong 不泄漏进业务事件流。"""
    with running_server(heartbeat_interval=0.05, heartbeat_timeout=0.4) as srv:
        with running_client("127.0.0.1", srv.port,
                            heartbeat_interval=0.05, heartbeat_timeout=0.4) as cli:
            seen = []
            end = time.monotonic() + 0.6          # 远超 timeout 窗口
            while time.monotonic() < end:
                for q in (srv.events, cli.events):
                    try:
                        seen.append(q.get_nowait())
                    except queue.Empty:
                        pass
                time.sleep(0.05)
            assert not any(k == "disconnected" for k, _p, _pl in seen)
            assert not any(k == "message" and isinstance(pl, dict)
                           and pl.get("t") in ("ping", "pong")
                           for k, _p, pl in seen)
            assert cli.send({"t": "hello"}) is True
            ev, _ = wait_event(srv.events, "message")
            assert ev[2] == {"t": "hello"}


def test_concurrent_send_thread_safe():
    with running_server() as srv:
        with running_client("127.0.0.1", srv.port) as cli:
            wait_event(srv.events, "connected")
            n_threads, per_thread = 4, 100

            def worker(tid):
                for i in range(per_thread):
                    assert cli.send({"t": "load", "tid": tid, "i": i})

            threads = [threading.Thread(target=worker, args=(t,))
                       for t in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            msgs = collect_messages(srv.events, n_threads * per_thread)
            assert len(msgs) == n_threads * per_thread
            for tid in range(n_threads):          # 每线程 100 条一条不少（帧不交错损坏）
                assert sum(1 for m in msgs if m["tid"] == tid) == per_thread


def test_broadcast_reaches_all_clients():
    with running_server() as srv:
        with running_client("127.0.0.1", srv.port) as c1, \
                running_client("127.0.0.1", srv.port) as c2:
            wait_event(srv.events, "connected")
            wait_event(srv.events, "connected")
            assert srv.broadcast({"t": "seats"}) == 2
            for c in (c1, c2):
                ev, _ = wait_event(c.events, "message")
                assert ev[2] == {"t": "seats"}


def test_send_unknown_peer_returns_false():
    with running_server() as srv:
        assert srv.send(999, {"t": "x"}) is False
        with running_client("127.0.0.1", srv.port):
            wait_event(srv.events, "connected")
            assert srv.send(999, {"t": "x"}) is False
            assert srv.send(1, {"t": "x"}) is True


def test_close_peer_drops_only_target():
    """close_peer 只断指定连接（房间层拒绝加入用），其余连接不受影响。"""
    with running_server() as srv:
        with running_client("127.0.0.1", srv.port) as c1, \
                running_client("127.0.0.1", srv.port) as c2:
            wait_event(srv.events, "connected")
            wait_event(srv.events, "connected")
            assert srv.close_peer(1) is True
            ev, _ = wait_event(c1.events, "disconnected")
            assert ev[2] == "reset"     # 被服务端收线的一侧报告对端断开
            assert srv.close_peer(999) is False
            assert c2.send({"t": "still here"}) is True
            ev, _ = wait_event(srv.events, "message")
            assert ev[1] == 2
            assert ev[2] == {"t": "still here"}


def test_close_idempotent():
    srv = NetServer("127.0.0.1", 0)
    cli = NetClient("127.0.0.1", srv.port)
    wait_event(cli.events, "connected")
    for _ in range(2):                            # 重复 close 不抛异常
        cli.close()
        srv.close()
    assert cli.alive is False
    assert srv.alive is False
    assert srv.broadcast({"t": "x"}) == 0
    assert cli.send({"t": "x"}) is False


def test_connected_event_precedes_message():
    with running_server() as srv:
        with running_client("127.0.0.1", srv.port) as cli:
            cli.send({"t": "hi"})
            _ev, seen = wait_event(srv.events, "message")
            kinds = [k for k, _p, _pl in seen]
            assert "connected" in kinds
            assert kinds.index("connected") < kinds.index("message")
