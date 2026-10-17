"""room.py 房间会话层测试（好友联机阶段二）。

全部走 127.0.0.1 回环、短超时；等待一律 deadline 轮询（延续 test_net 纪律，
禁止裸 sleep 等结果）。裸 socket 用例在收帧间隙持续泵取 host（否则房间层
状态机不推进、回执无从产生）。房间发现用临时端口 RoomBrowser + 单播注入
验证聚合/过期逻辑，不依赖真实 UDP 广播；RoomHost 统一 advertise=False。

覆盖大纲阶段二验收点：加入 / 换座冲突 / 退出 / 双加入 / 版本不符，
另含房间满员、掉线清座、主机换座、开局广播、解散通知、发现列表、本机 IP。
"""

import contextlib
import json
import queue
import socket
import struct
import time

from net import PROTOCOL_VER, encode_frame
from room import RoomBrowser, RoomClient, RoomHost, local_ip

SEAT_KEYS = {"seat", "name", "is_ai", "tier", "pid"}


# ---------- 泵取辅助 ----------

def pump_collect(rooms, until, timeout=5.0):
    """持续泵取所有会话直到 until(events) 为真或超时。

    返回累计事件列表 [(room, ev)]；ev 为 (kind, peer, payload)。
    until 为 None 时仅做一轮泵取+清队（用于收尾断言）。
    """
    deadline = time.monotonic() + timeout
    got = []
    while True:
        for r in rooms:
            r.pump()
        for r in rooms:
            while True:
                try:
                    got.append((r, r.events.get_nowait()))
                except queue.Empty:
                    break
        if until is not None and until(got):
            return got
        if until is None or time.monotonic() > deadline:
            assert until is None, f"泵取超时，已收集 {len(got)} 个事件：{got}"
            return got
        time.sleep(0.01)


def has_event(events, kind, room=None):
    return any(e[1][0] == kind and (room is None or e[0] is room)
               for e in events)


@contextlib.contextmanager
def make_host(name="房主", **kw):
    kw.setdefault("advertise", False)
    kw.setdefault("bind_ip", "127.0.0.1")   # 回环监听，避免触发防火墙弹窗
    host = RoomHost(name, port=0, **kw)
    try:
        yield host
    finally:
        host.close()


def join_client(name, host, timeout=5.0):
    """创建客户端并完成 join→welcome，返回客户端（已入自动座位）。"""
    cli = RoomClient(name)
    cli.join("127.0.0.1", host.port)
    pump_collect([host, cli],
                 lambda evs: has_event(evs, "joined", room=cli), timeout)
    return cli


@contextlib.contextmanager
def joined(name, host):
    cli = join_client(name, host)
    try:
        yield cli
    finally:
        cli.close()


def _recv_frame(sock):
    """收一帧（sock 已设短超时）；对端关闭返回 None，无数据抛 socket.timeout。"""
    head = b""
    while len(head) < 4:
        chunk = sock.recv(4 - len(head))
        if not chunk:
            return None
        head += chunk
    (n,) = struct.unpack(">I", head)
    body = b""
    while len(body) < n:
        chunk = sock.recv(n - len(body))
        if not chunk:
            return None
        body += chunk
    return json.loads(body.decode("utf-8"))


def raw_join(host, msg, timeout=3.0):
    """裸 socket 发 join，泵取 host 的同时收回执；返回 (回执列表, 是否收线)。

    泵取与收帧交替进行——房间状态机只在 pump() 里推进，必须泵着等回执。
    """
    sock = socket.create_connection(("127.0.0.1", host.port), timeout=3)
    sock.settimeout(0.05)
    try:
        sock.sendall(encode_frame(msg))
        deadline = time.monotonic() + timeout
        msgs, closed = [], False
        while time.monotonic() < deadline:
            host.pump()
            try:
                got = _recv_frame(sock)
            except socket.timeout:
                continue
            except OSError:
                closed = True
                break
            if got is None:
                closed = True
                break
            msgs.append(got)
            if msgs[-1].get("t") == "reject":
                # 拒绝路径主机随后必收线，再泵一轮确认
                end = time.monotonic() + 1.0
                while time.monotonic() < end and not closed:
                    host.pump()
                    try:
                        if _recv_frame(sock) is None:
                            closed = True
                    except socket.timeout:
                        continue
                    except OSError:
                        closed = True
                break
        return msgs, closed
    finally:
        sock.close()


def wait_snapshot(browser, pred, timeout=3.0):
    """轮询发现快照直到 pred(列表) 为真。"""
    deadline = time.monotonic() + timeout
    last = []
    while time.monotonic() < deadline:
        last = browser.snapshot()
        if pred(last):
            return last
        time.sleep(0.05)
    raise AssertionError(f"发现快照等待超时：{last}")


# ---------- 加入与座位表 ----------

def test_join_welcome_and_seat_schema():
    with make_host() as host:
        cli = RoomClient("小明")
        try:
            cli.join("127.0.0.1", host.port)
            # 一次泵取同时捕获客户端 joined 与主机侧 joined 事件
            evs = pump_collect([host, cli],
                               lambda e: has_event(e, "joined", room=cli))
            assert cli.welcomed and cli.my_pid is not None
            assert cli.my_seat == 1            # 自动坐第一个空位（0 是主机）
            assert cli.room_name == "房主的房间"
            seats = cli.seats_snapshot()
            assert [s["seat"] for s in seats] == [0, 1, 2, 3]
            for s in seats:
                assert set(s) == SEAT_KEYS     # 座位条目 schema 钉死
            assert seats[0] == {"seat": 0, "name": "房主", "is_ai": False,
                                "tier": None, "pid": 0}
            assert seats[1]["name"] == "小明"
            assert seats[1]["is_ai"] is False
            assert seats[1]["tier"] is None
            assert seats[1]["pid"] == cli.my_pid
            assert all(s["is_ai"] and s["tier"] == "advanced"
                       for s in seats[2:])     # 空位 AI 补位带档位
            # 主机侧 joined 事件（peer=客户端临时编号）
            hev = next(e for e in evs
                       if e[0] is host and e[1][0] == "joined")
            assert hev[1][1] == cli.my_pid
            assert hev[1][2] == {"name": "小明", "seat": 1}
        finally:
            cli.close()


def test_multiple_clients_auto_seat_in_order():
    with make_host() as host:
        with joined("甲", host) as c1, joined("乙", host) as c2, \
                joined("丙", host) as c3:
            assert (c1.my_seat, c2.my_seat, c3.my_seat) == (1, 2, 3)
            assert host.players_count == 4
            for s in host.seats_snapshot():
                assert not s["is_ai"]


# ---------- 拒绝路径（版本/满员/双加入/开局后） ----------

def test_join_version_mismatch_rejected():
    with make_host() as host:
        msgs, closed = raw_join(host, {"t": "join", "name": "旧版",
                                       "ver": PROTOCOL_VER + 1})
        assert [m["t"] for m in msgs] == ["reject"]
        assert "版本" in msgs[0]["reason"]
        assert closed is True                   # 拒绝后主机收线
        pump_collect([host], None)
        assert not any(e[1][0] == "joined" for e in pump_collect([host], None))
        assert all(s["is_ai"] for s in host.seats_snapshot()[1:])


def test_join_room_full_rejected():
    with make_host() as host:
        with joined("甲", host), joined("乙", host), joined("丙", host):
            msgs, closed = raw_join(host, {"t": "join", "name": "丁",
                                           "ver": PROTOCOL_VER})
            assert [m["t"] for m in msgs] == ["reject"]
            assert "房间已满" in msgs[0]["reason"]
            assert closed is True


def test_double_join_rejected_and_kicked():
    with make_host() as host:
        with joined("甲", host) as cli:
            # 已入座的会话再发 join → 主机拒绝并收线，座位随后因断开释放
            assert cli._cli.send({"t": "join", "name": "甲",
                                  "ver": PROTOCOL_VER}) is True
            pump_collect([host, cli],
                         lambda evs: has_event(evs, "disconnected"))
            pump_collect([host],
                         lambda e: host.seats_snapshot()[1]["is_ai"])


def test_join_after_start_rejected():
    with make_host() as host:
        with joined("甲", host) as cli:
            assert host.start_game() is not None
            msgs, _closed = raw_join(host, {"t": "join", "name": "迟到",
                                            "ver": PROTOCOL_VER})
            assert [m["t"] for m in msgs] == ["reject"]
            assert "对局已开始" in msgs[0]["reason"]


# ---------- 选座 / 换座 ----------

def test_sit_move_and_conflict():
    with make_host() as host:
        with joined("甲", host) as c1, joined("乙", host) as c2:
            assert c1.try_sit(3) is True
            pump_collect([host, c1], lambda e: c1.my_seat == 3)
            assert c1.my_seat == 3
            seats = c1.seats_snapshot()
            assert seats[1]["is_ai"] and seats[3]["name"] == "甲"
            # 换座冲突：乙坐甲刚占的 3 号 → 被拒，原地不动
            assert c2.try_sit(3) is True            # 发送成功
            pump_collect([host, c2],
                         lambda e: has_event(e, "sit_rejected"))
            assert c2.my_seat == 2
            assert c2.seats_snapshot()[3]["name"] == "甲"
            # 原地重坐当前座位是无操作
            assert c2.try_sit(2) is True
            assert c2.my_seat == 2


def test_sit_invalid_seat_rejected():
    with make_host() as host:
        with joined("甲", host) as cli:
            assert cli.try_sit(9) is True           # 发送成功
            evs = pump_collect([host, cli],
                               lambda e: has_event(e, "sit_rejected"))
            ev = next(e for e in evs if e[1][0] == "sit_rejected")
            assert "无效座位" in ev[1][2]
            assert cli.my_seat == 1


def test_host_move_seat():
    with make_host() as host:
        assert host.try_sit(2) is True
        seats = host.seats_snapshot()
        assert seats[2]["name"] == "房主" and not seats[2]["is_ai"]
        assert seats[0]["is_ai"] and seats[0]["tier"] == "advanced"
        with joined("甲", host) as cli:
            assert cli.my_seat == 0                 # 客人自动补 0 号空位


def test_sit_after_start_blocked():
    with make_host() as host:
        with joined("甲", host) as cli:
            host.start_game()
            pump_collect([host, cli],
                         lambda e: has_event(e, "started", room=cli))
            assert cli.started is True
            assert cli.try_sit(2) is False          # 客户端本地即拒


# ---------- 退出 / 掉线清座 ----------

def test_leave_frees_seat_and_reusable():
    with make_host() as host:
        with joined("甲", host) as cli:
            pid = cli.my_pid
            cli.leave()
            evs = pump_collect([host], lambda e: has_event(e, "left"))
            ev = next(e for e in evs if e[1][0] == "left")
            assert ev[1][1] == pid
            assert ev[1][2] == {"name": "甲", "reason": "离开"}
            assert host.seats_snapshot()[1]["is_ai"]
        with joined("乙", host) as c2:              # 释放后座位可复用
            assert c2.my_seat == 1


def test_disconnect_frees_seat():
    with make_host() as host:
        with joined("甲", host) as cli:
            cli.close()                             # 不发 leave 直接断开
            evs = pump_collect([host], lambda e: has_event(e, "left"))
            ev = next(e for e in evs if e[1][0] == "left")
            assert ev[1][2] == {"name": "甲", "reason": "掉线"}
            assert host.seats_snapshot()[1]["is_ai"]


# ---------- 开局与解散 ----------

def test_start_broadcast_and_once():
    with make_host() as host:
        with joined("甲", host) as cli:
            seed = host.start_game(levels=[3, 4], seed=12345)
            assert seed == 12345
            evs = pump_collect([host, cli],
                               lambda e: has_event(e, "started", room=cli))
            sev = next(e for e in evs if e[0] is cli and e[1][0] == "started")
            payload = sev[1][2]
            assert payload["seed"] == 12345
            assert payload["levels"] == [3, 4]
            assert payload["seats"] == host.seats_snapshot()
            assert host.started is True
            assert host.start_game() is None        # 二次启动无效
            assert not has_event(pump_collect([host], None), "started")


def test_host_close_notifies_clients():
    with make_host() as host:
        cli = join_client("甲", host)
        host.close()
        evs = pump_collect([cli],
                           lambda e: has_event(e, "disconnected"))
        ev = next(e for e in evs if e[1][0] == "disconnected")
        assert "解散" in ev[1][2]
        assert cli.active is False


def test_host_close_idempotent_and_event():
    with make_host() as host:
        host.close()
        host.close()                                # 重复关闭不抛异常
        evs = pump_collect([host], lambda e: has_event(e, "closed"))
        assert sum(1 for e in evs if e[1][0] == "closed") == 1


# ---------- 客户端异常路径 ----------

def test_connect_failure_reports_error():
    # 找一个确定无人监听的端口：绑定后立即释放
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()
    cli = RoomClient("甲")
    try:
        assert cli.join("127.0.0.1", dead_port) is True
        evs = pump_collect([cli], lambda e: has_event(e, "error"))
        ev = next(e for e in evs if e[1][0] == "error")
        assert "无法连接" in ev[1][2]
    finally:
        cli.close()


# ---------- 房间发现 ----------

def test_browser_aggregates_and_updates():
    browser = RoomBrowser(port=0)
    try:
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            frame = encode_frame({"t": "room", "name": "测试房",
                                  "host_name": "测试主机", "players": 2,
                                  "ver": PROTOCOL_VER, "port": 12345})
            sender.sendto(frame, ("127.0.0.1", browser.port))
            rooms = wait_snapshot(browser, lambda r: len(r) == 1)
            assert rooms[0]["ip"] == "127.0.0.1"
            assert rooms[0]["port"] == 12345
            assert rooms[0]["name"] == "测试房"
            assert rooms[0]["host_name"] == "测试主机"
            assert rooms[0]["players"] == 2
            assert rooms[0]["ver"] == PROTOCOL_VER
            # 同 IP 再广播：按 IP 去重并更新
            sender.sendto(encode_frame({"t": "room", "name": "测试房",
                                        "host_name": "测试主机", "players": 3,
                                        "ver": PROTOCOL_VER, "port": 12345}),
                          ("127.0.0.1", browser.port))
            wait_snapshot(browser,
                          lambda r: len(r) == 1 and r[0]["players"] == 3)
        finally:
            sender.close()
    finally:
        browser.close()


def test_browser_prunes_stale_entries():
    browser = RoomBrowser(port=0, stale=0.15)
    try:
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sender.sendto(encode_frame({"t": "room", "name": "房",
                                        "host_name": "主", "players": 1,
                                        "ver": PROTOCOL_VER, "port": 1}),
                          ("127.0.0.1", browser.port))
            wait_snapshot(browser, lambda r: len(r) == 1)
        finally:
            sender.close()
        # 超过 stale 后过期剔除（deadline 轮询，非裸 sleep 等结果）
        assert wait_snapshot(browser, lambda r: not r, timeout=3.0) == []
    finally:
        browser.close()


def test_browser_ignores_garbage():
    browser = RoomBrowser(port=0)
    try:
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sender.sendto(b"not a frame", ("127.0.0.1", browser.port))
            sender.sendto(encode_frame({"t": "other"}),
                          ("127.0.0.1", browser.port))
            time.sleep(0.1)     # 给接收线程处理垃圾帧的时间（结果恒为空，非等待）
            assert browser.snapshot() == []
        finally:
            sender.close()
    finally:
        browser.close()


# ---------- 工具 ----------

def test_local_ip_valid_ipv4():
    ip = local_ip()
    socket.inet_aton(ip)        # 非法格式会抛异常
    assert ip.count(".") == 3
