"""掼蛋联机传输层（好友联机阶段一）。

纯标准库实现（socket / threading / queue / json / struct / time），
零第三方依赖，buildozer 安卓打包安全。

设计要点（见《20261018 开发大纲（好友联机对战）》4.1 节）：
- 帧格式：4 字节大端长度前缀 + UTF-8 JSON 载荷（ensure_ascii=False）；
- 线程桥铁律：socket 线程绝不触碰 Kivy，所有入站事件推入 queue.Queue，
  UI 侧在主线程泵取后分发；
- 消息为 dict 透传，本层不解析业务字段；唯一例外是心跳 ping/pong：
  由本层自动应答并从事件流中屏蔽，业务层无感知；
- send 失败一律返回 False，不向调用方抛异常；
- 每条连接最终恰好产生一次 disconnected 事件（payload 为原因字符串）。

事件格式：元组 (kind, peer, payload)
- kind ∈ {"connected", "message", "disconnected", "error"}
- peer：服务端为自增整数 peer_id（从 1 起）；客户端恒为 None（对端唯一）
- payload：
  connected    -> 对端地址 (ip, port)
  message      -> 解码后的消息（dict）
  disconnected -> 原因：closed(本地主动关闭) / reset(对端断开) /
                  timeout(心跳超时) / protocol(帧损坏) / error(未预期异常)
  error        -> 错误描述字符串
"""

import itertools
import json
import queue
import socket
import struct
import threading
import time

PROTOCOL_VER = 1                    # 协议版本（join/welcome 版本校验用，阶段二启用）
DEFAULT_PORT = 17666                # TCP 默认端口
DISCOVER_PORT = 17667               # UDP 房间发现端口（阶段二使用，先定常量）
HEARTBEAT_INTERVAL = 5.0            # 心跳发送间隔（秒）
HEARTBEAT_TIMEOUT = 15.0            # 无任何收包判掉线的时限（秒）
MAX_FRAME = 4 * 1024 * 1024         # 单帧载荷上限 4MB

_HEAD = struct.Struct(">I")         # 4 字节大端长度头


def encode_frame(msg) -> bytes:
    """将消息编码为一帧：4 字节大端长度 + UTF-8 JSON。

    载荷超 MAX_FRAME 抛 ValueError，不可 JSON 序列化抛 TypeError。
    """
    data = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_FRAME:
        raise ValueError(f"帧载荷超限：{len(data)} > {MAX_FRAME}")
    return _HEAD.pack(len(data)) + data


class FrameDecoder:
    """流式帧解码器：处理粘包/半包，不依赖 socket，便于单测。"""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data: bytes) -> list:
        """喂入任意长度字节，返回本次完整解出的消息列表。

        长度头超 MAX_FRAME 视为协议损坏：清空缓冲并抛 ValueError
        （连接已无法恢复同步，调用方应断开）。
        """
        self._buf.extend(data)
        msgs = []
        while True:
            if len(self._buf) < _HEAD.size:
                break
            (n,) = _HEAD.unpack_from(self._buf, 0)
            if n > MAX_FRAME:
                self._buf.clear()
                raise ValueError(f"帧长度超限：{n} > {MAX_FRAME}")
            if len(self._buf) < _HEAD.size + n:
                break
            payload = bytes(self._buf[_HEAD.size:_HEAD.size + n])
            del self._buf[:_HEAD.size + n]
            msgs.append(json.loads(payload.decode("utf-8")))
        return msgs


def _join_thread(t):
    """安全 join 后台线程：跳过当前线程，限时等待避免卡死。"""
    if t is not None and t is not threading.current_thread() and t.is_alive():
        t.join(timeout=2.0)


class _Conn:
    """单条 TCP 连接的收发/心跳管理，服务端与客户端共用。"""

    def __init__(self, sock, addr, on_event, on_closed, interval, timeout):
        self.sock = sock
        self.addr = addr
        self.id = None                 # 服务端分配自增 peer_id；客户端为 None
        self._on_event = on_event      # (kind, conn, payload)
        self._on_closed = on_closed    # (conn,)
        self.interval = interval
        self.timeout = timeout
        self.decoder = FrameDecoder()
        self._send_lock = threading.Lock()   # 多线程并发 sendall 防帧交错
        self.alive = True
        self.last_recv = time.monotonic()
        self._closed_once = threading.Event()  # disconnected 事件仅发一次
        self.threads = []

    def start(self):
        t1 = threading.Thread(target=self._recv_loop, name="guandan-recv", daemon=True)
        t2 = threading.Thread(target=self._heartbeat_loop, name="guandan-heartbeat", daemon=True)
        self.threads = [t1, t2]
        t1.start()
        t2.start()

    def send(self, msg) -> bool:
        """发送一条消息；连接已断/编码失败/套接字异常一律返回 False。"""
        if not self.alive:
            return False
        try:
            frame = encode_frame(msg)
        except (TypeError, ValueError):
            return False
        try:
            with self._send_lock:
                self.sock.sendall(frame)
            return True
        except OSError:
            self.shutdown("reset")
            return False

    def shutdown(self, reason: str):
        """幂等关闭：shutdown 套接字、广播 disconnected 事件（仅一次）。"""
        if self._closed_once.is_set():
            return
        self._closed_once.set()
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)   # 唤醒对端与本端阻塞的 recv
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self._on_event("disconnected", self, reason)
        self._on_closed(self)

    def _recv_loop(self):
        reason = "reset"
        try:
            while self.alive:
                try:
                    data = self.sock.recv(65536)
                except OSError:
                    break            # 本端 shutdown/close 后正常退出
                if not data:
                    break            # 对端关闭连接
                self.last_recv = time.monotonic()
                try:
                    msgs = self.decoder.feed(data)
                except ValueError as exc:
                    self._on_event("error", self, f"帧解码失败：{exc}")
                    self.shutdown("protocol")
                    return
                for msg in msgs:
                    if isinstance(msg, dict) and msg.get("t") == "ping":
                        self.send({"t": "pong"})     # 心跳应答，业务层无感知
                        continue
                    if isinstance(msg, dict) and msg.get("t") == "pong":
                        continue                      # 仅用于刷新 last_recv
                    self._on_event("message", self, msg)
        finally:
            self.shutdown(reason)

    def _heartbeat_loop(self):
        step = min(0.2, max(0.005, self.interval / 10.0))
        next_ping = time.monotonic() + self.interval
        while self.alive:
            time.sleep(step)
            if not self.alive:
                break
            now = time.monotonic()
            if now - self.last_recv > self.timeout:
                self.shutdown("timeout")
                break
            if now >= next_ping:
                next_ping = now + self.interval
                self.send({"t": "ping"})


class NetServer:
    """TCP 服务端（主机即服务器，联机对局权威端）。

    - 后台 accept 线程接受连接并分配自增 peer_id（从 1 起）；
    - 每条连接独立收包线程与心跳线程；
    - 入站事件统一进入 self.events 队列，由 UI 主线程泵取。
    """

    def __init__(self, bind_ip="0.0.0.0", port=DEFAULT_PORT, *,
                 heartbeat_interval=HEARTBEAT_INTERVAL,
                 heartbeat_timeout=HEARTBEAT_TIMEOUT):
        self.events = queue.Queue()
        self._conns = {}
        self._lock = threading.Lock()
        self._peer_ids = itertools.count(1)
        self._alive = True
        self._hb_interval = heartbeat_interval
        self._hb_timeout = heartbeat_timeout
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((bind_ip, port))
        self._sock.listen(4)
        self._sock.settimeout(0.5)     # accept 轮询，保证 close 能及时退出
        self.port = self._sock.getsockname()[1]
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="guandan-accept", daemon=True)
        self._accept_thread.start()

    @property
    def alive(self) -> bool:
        return self._alive

    # -- 内部 ----------------------------------------------------------

    def _dispatch(self, kind, conn, payload):
        self.events.put((kind, conn.id, payload))

    def _on_conn_closed(self, conn):
        with self._lock:
            self._conns.pop(conn.id, None)

    def _accept_loop(self):
        while self._alive:
            try:
                sock, addr = self._sock.accept()
            except socket.timeout:
                continue
            except ConnectionError:
                continue                # 个别握手失败，继续监听
            except OSError:
                break                   # 监听套接字已关闭
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:
                if not self._alive:
                    try:
                        sock.close()
                    except OSError:
                        pass
                    return
                conn = _Conn(sock, addr, self._dispatch, self._on_conn_closed,
                             self._hb_interval, self._hb_timeout)
                conn.id = next(self._peer_ids)
                self._conns[conn.id] = conn
            self._dispatch("connected", conn, addr)   # 先于收包线程，保证事件顺序
            conn.start()

    # -- 对外接口 -------------------------------------------------------

    def send(self, peer_id: int, msg) -> bool:
        """向指定 peer 发送消息；peer 不存在或发送失败返回 False。"""
        with self._lock:
            conn = self._conns.get(peer_id)
        if conn is None:
            return False
        return conn.send(msg)

    def broadcast(self, msg) -> int:
        """向当前所有存活连接广播；返回成功送达的连接数。"""
        with self._lock:
            conns = list(self._conns.values())
        sent = 0
        for conn in conns:
            if conn.send(msg):
                sent += 1
        return sent

    def close_peer(self, peer_id: int) -> bool:
        """主动断开指定 peer（如加入被拒）；peer 不存在返回 False。

        断开后对端与本端各收到一次 disconnected 事件（原因 closed）。
        """
        with self._lock:
            conn = self._conns.get(peer_id)
        if conn is None:
            return False
        conn.shutdown("closed")
        return True

    def close(self):
        """关闭服务端与全部连接；幂等。"""
        self._alive = False
        with self._lock:
            conns = list(self._conns.values())
            self._conns.clear()
        for conn in conns:
            conn.shutdown("closed")
        try:
            self._sock.close()
        except OSError:
            pass
        _join_thread(self._accept_thread)
        for conn in conns:
            for t in conn.threads:
                _join_thread(t)


class NetClient:
    """TCP 客户端：构造时即连接主机，事件 peer 恒为 None。"""

    def __init__(self, host, port=DEFAULT_PORT, *,
                 heartbeat_interval=HEARTBEAT_INTERVAL,
                 heartbeat_timeout=HEARTBEAT_TIMEOUT):
        self.events = queue.Queue()
        self._sock = socket.create_connection((host, port), timeout=5.0)
        self._sock.settimeout(None)    # connect 用限时，收发恢复阻塞模式
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._conn = _Conn(self._sock, (host, port), self._dispatch,
                           lambda conn: None, heartbeat_interval, heartbeat_timeout)
        self.events.put(("connected", None, (host, port)))   # 先于收包线程
        self._conn.start()

    @property
    def alive(self) -> bool:
        return self._conn.alive

    def send(self, msg) -> bool:
        """发送消息给主机；失败返回 False。"""
        return self._conn.send(msg)

    def close(self):
        """关闭连接；幂等。"""
        self._conn.shutdown("closed")
        for t in self._conn.threads:
            _join_thread(t)

    def _dispatch(self, kind, conn, payload):
        self.events.put((kind, None, payload))
