"""掼蛋房间会话层（好友联机阶段二）。

在 net.py 传输层之上实现牌桌房间：RoomHost（主机=房间权威端）、RoomClient
（客人）、RoomBrowser（UDP 房间发现）与 local_ip 工具。协议见
《20261018 开发大纲（好友联机对战）》第三节。

线程模型（延续 net.py 铁律）：
- UDP 广播（主机）与监听（浏览器）各在独立后台线程，只操作自身套接字与
  内部锁，绝不触碰 Kivy；
- TCP 会话状态（座位表、started 标记）只在 pump() 调用线程中变更——UI 侧
  每帧调用 pump() 泵取 net 层事件并顺序处理，天然串行，座位表无需加锁；
- 对 UI 暴露统一事件队列 events（(kind, peer, payload) 元组），由房间页
  在主线程泵取分发。

座位模型：seat 0-3 即牌局 pid，(0,2) 一队、(1,3) 二队（pid%2 同队）。
空位即 AI 补位（is_ai=True，tier 为 AI 档位）；人类入座顶替 AI，人类离开
（主动退出/掉线/被踢）座位自动还原为 AI。welcome/start 中 pid 为**会话
临时编号**（主机恒为 0，客人 = net 层 peer_id 自增），与座位号（牌局 pid）
是两个概念，客户端靠 seats[].pid == 自己的临时编号定位自己的座位。

对外事件（payload 见各类处理处）：
- RoomHost：joined / rejoined / left / seats / started / ai_takeover /
  error / closed
- RoomClient：joined / seats / rejected / sit_rejected / started / resync /
  act / act_rejected / ai_takeover / end / disconnected / error

动作流（阶段三）：主机为唯一动作源——本端引擎（人类提交/AI 代打/客人
动作代应用）经事件镜像统一打 seq 广播；客户端只上行自己动作、按广播
seq 顺序套用本地镜像，断档自动 resync_req 全量重演追平。对局中掉线的
座位保留并等待重连（_takeover_wait 秒超时转 AI，广播 ai_takeover），
重连以昵称精确匹配归位（welcome + resync）。
"""

from __future__ import annotations

import queue
import random
import socket
import threading
import time

from net import (
    DEFAULT_PORT,
    DISCOVER_PORT,
    PROTOCOL_VER,
    FrameDecoder,
    NetClient,
    NetServer,
    encode_frame,
)
from replay import apply_action

DISCOVER_INTERVAL = 1.0     # 主机 UDP 房间广播周期（秒）
ROOM_STALE = 3.5            # 发现条目超时未刷新判过期（秒），约 3 个广播周期
JOIN_TIMEOUT = 5.0          # 客户端发出 join 后等待 welcome 的时限（秒）
MAX_NAME = 12               # 玩家/房间名长度上限（字符）
SEAT_KEYS = ("seat", "name", "is_ai", "tier", "pid")
TIER_LABELS = {"novice": "初级", "intermediate": "中级", "advanced": "高级"}
NET_TAKEOVER_WAIT = 60.0    # 掉线座位等待重连时长（秒），超时转 AI 接管
ACT_KINDS = ("play", "pass", "tribute", "return")   # 动作流白名单

_BROADCAST_TARGETS = ("255.255.255.255", "127.0.0.1")   # 局域网 + 本机回环


def _clamp_name(name, fallback: str) -> str:
    """清洗昵称：去空白、限长、空则回退默认名。"""
    text = str(name or "").strip()[:MAX_NAME]
    return text or fallback


def local_ip() -> str:
    """取本机局域网 IP（供主机页大字展示、好友手动直连兜底）。

    UDP connect 不会真的发包，仅让内核按路由表选源地址；离线时退回
    主机名解析，再退回 127.0.0.1。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        sock.close()


def _seats_view(seats) -> list:
    """座位表快照（纯 JSON 标量，可直接入网/入 UI）。"""
    return [{k: s[k] for k in SEAT_KEYS} for s in seats]


class RoomHost:
    """房间主机：NetServer + UDP 广播 + 房间状态机（权威端）。

    - 构造即监听 TCP（端口被占用抛 OSError，由 UI 捕获提示）；
    - advertise=False 可关闭 UDP 广播（测试用）；
    - pump() 必须由 UI 线程周期调用：驱动 net 事件 → 房间状态迁移 →
      广播座位表 → 向 self.events 推送 UI 事件。
    """

    def __init__(self, host_name, *, room_name=None, tier="advanced",
                 port=DEFAULT_PORT, bind_ip="0.0.0.0", advertise=True,
                 discover_port=DISCOVER_PORT):
        self.events = queue.Queue()
        self.host_name = _clamp_name(host_name, "房主")
        self.room_name = _clamp_name(room_name, f"{self.host_name}的房间")
        self.tier = tier                      # AI 补位档位（座位还原时使用）
        self.is_host = True
        self._started = False
        self._closed = False
        self._seed_base = random.randrange(2 ** 31)   # 预留：多局 seed 派生基值
        # 座位表：0 号默认主机本人，其余 AI 补位
        self._seats = [{"seat": i, "name": "AI", "is_ai": True,
                        "tier": tier, "pid": None} for i in range(4)]
        self._seats[0].update(name=self.host_name, is_ai=False,
                              tier=None, pid=0)
        self._sock = NetServer(bind_ip, port)
        # -- 阶段三：牌局动作流（主机=权威动作源）--
        self.game = None              # 当前 GuandanGame（set_game 挂载）
        self._mirror_on = False       # 引擎事件镜像监听是否已挂
        self.actions = []             # 已接受动作（replay 格式，seq=下标+1）
        self.match_levels = None      # 本场初始级数（resync 重建用）
        self._end_record = None       # 阶段四：最近一场赛末分发的复盘记录
        self._down = {}               # seat -> 等待重连截止时刻（monotonic）
        self._taken = set()           # 已被 AI 接管的座位号
        self._takeover_wait = NET_TAKEOVER_WAIT
        self._adv_ev = threading.Event() if advertise else None
        self._adv_thread = None
        if advertise:
            self._adv_ev.set()
            self._adv_thread = threading.Thread(
                target=self._advertise_loop, args=(discover_port,),
                name="guandan-advertise", daemon=True)
            self._adv_thread.start()

    # -- 属性 ------------------------------------------------------------

    @property
    def port(self) -> int:
        return self._sock.port

    @property
    def started(self) -> bool:
        return self._started

    @property
    def seed_base(self) -> int:
        return self._seed_base

    @property
    def players_count(self) -> int:
        return sum(1 for s in self._seats if not s["is_ai"])

    def seats_snapshot(self) -> list:
        return _seats_view(self._seats)

    def seat_of(self, pid) -> int | None:
        """会话编号 pid 当前所在的座位号；未入座返回 None。"""
        for s in self._seats:
            if s["pid"] == pid:
                return s["seat"]
        return None

    @property
    def my_seat(self) -> int:
        """主机本人（会话 pid=0）的座位号。"""
        return self.seat_of(0) or 0

    def ai_pids(self) -> set:
        """AI 代打的座位集合：补位 AI + 已接管/等待重连超时的座位。"""
        return {s["seat"] for s in self._seats if s["is_ai"]} | set(self._taken)

    # -- 牌局动作流（阶段三）-------------------------------------------------

    def set_game(self, game) -> None:
        """挂载权威引擎并开始镜像其动作事件（UI 建局后调用）；重复挂载先卸。"""
        if self.game is not None and self._mirror_on:
            try:
                self.game.listeners.remove(self._on_game_event)
            except ValueError:
                pass
        self.game = game
        self._mirror_on = game is not None
        if game is not None:
            game.listeners.append(self._on_game_event)

    def _on_game_event(self, game, event: str, data: dict) -> None:
        """引擎事件镜像：本端（人类提交/AI 代打/客人动作代应用）产生的每个
        动作统一在此捕获、打 seq、广播。seq 仅发给已接受的动作。"""
        if event not in ACT_KINDS:
            return
        act = {"t": "act", "kind": event, "pid": data["pid"],
               "round": game.round_number}
        if "eids" in data:
            act["eids"] = list(data["eids"])
        if "eid" in data:
            act["eid"] = data["eid"]
        self._push_act(act)

    def _push_act(self, act: dict) -> None:
        """打 seq、记入动作流并广播（调用方须保证动作已在权威引擎生效）。"""
        act["seq"] = len(self.actions) + 1
        self.actions.append({k: v for k, v in act.items() if k != "t"})
        self._sock.broadcast(act)

    def next_match(self, levels, seed=None):
        """本场打完后再开一场：广播新 start（新 seed），全员重建引擎。"""
        if not self._started or self._closed:
            return None
        if seed is None:
            seed = random.randrange(2 ** 31)
        self._cur_seed = seed
        self.actions = []
        self.match_levels = list(levels)
        self._sock.broadcast({"t": "start", "seed": seed,
                              "seats": self.seats_snapshot(),
                              "levels": list(levels)})
        self.events.put(("started", None, {"seed": seed, "levels": list(levels)}))
        return seed

    def _send_resync(self, peer) -> None:
        """向单个客户端全量重发动作流（断档/重连归位）。"""
        self._sock.send(peer, {"t": "resync", "seed": self._resync_seed,
                               "levels": list(self.match_levels or [2, 2]),
                               "seats": self.seats_snapshot(),
                               "actions": [dict(a) for a in self.actions]})

    def broadcast_end(self, record: dict) -> None:
        """赛末分发完整复盘记录（end 消息，阶段四）。

        主机 UI 在 playing → match_over 跃迁时调用：把权威 Recorder 记录
        （含 AI 座位出牌理由/决策快照键）广播给全部客人；赛末后重连归位的
        客人由 _on_join 补发同一条记录。
        """
        self._end_record = record
        self._sock.broadcast({"t": "end", "record": record})

    @property
    def _resync_seed(self):
        return getattr(self, "_cur_seed", None)

    def _on_act(self, peer, payload) -> None:
        """客人动作代应用：校验后套用到权威引擎（镜像监听负责广播）。

        校验：座位归属（防伪）、动作类型白名单、回合号必须等于当前局
        （apply_action 的 round 跳变是重演专用，联机中收到越回合动作说明
        客户端状态已漂移，一律拒绝并回执）。引擎规则校验（非你回合/牌型
        不成立等）由 play/pass_play/submit_* 异常承担，失败回 act_ack。
        """
        if self.game is None:
            self._sock.send(peer, {"t": "act_ack", "ok": False,
                                   "err": "牌局未就绪"})
            return
        seat = self.seat_of(peer)
        kind = payload.get("kind")
        pid = payload.get("pid")
        if seat is None or pid != seat or kind not in ACT_KINDS:
            self._sock.send(peer, {"t": "act_ack", "ok": False,
                                   "err": "动作与座位不符"})
            return
        if payload.get("round") != self.game.round_number:
            self._sock.send(peer, {"t": "act_ack", "ok": False,
                                   "err": "动作已过期，正在重新同步"})
            return
        act = {"kind": kind, "pid": pid, "round": payload.get("round")}
        if "eids" in payload:
            act["eids"] = list(payload["eids"])
        if "eid" in payload:
            act["eid"] = payload["eid"]
        # 过牌若终结一轮，_advance 会就地清空展示——预先捕获含本次「不出」
        # 的完整展示随事件上交 UI（与本地 _on_pass/_ai_step 同款快照）
        pre = None
        if kind == "pass":
            pre = dict(self.game.trick_displays)
            pre[pid] = {"action": "pass", "combo": None}
        try:
            apply_action(self.game, act)
        except Exception as e:            # GameError 族：非法动作回执
            self._sock.send(peer, {"t": "act_ack", "ok": False, "err": str(e)})
            return
        self.events.put(("acted", peer, {"kind": kind, "pid": pid, "pre": pre}))

    # -- 主机操作 ----------------------------------------------------------

    def host_sit(self, seat: int) -> bool:
        """主机本人选座/换座（仅空位）；成功后广播座位表。"""
        if self._started or self._closed or not self._valid_seat(seat):
            return False
        cur = self.seat_of(0)
        if seat == cur:
            return True
        if not self._seats[seat]["is_ai"]:
            return False
        if cur is not None:
            self._free_seat(cur)
        self._occupy(seat, 0, self.host_name)
        self._broadcast_seats()
        return True

    def try_sit(self, seat: int) -> bool:
        """与 RoomClient.try_sit 同名接口：主机本人选座/换座（仅空位）。"""
        return self.host_sit(seat)

    def start_game(self, levels=None, seed=None):
        """广播 start（seed + 座位表 + 级数），进入对局态；重复启动返回 None。

        广播后停止房间发现（对局中不再接受新加入，重连走阶段三 resync）。
        """
        if self._started or self._closed:
            return None
        if seed is None:
            seed = random.randrange(2 ** 31)
        if levels is None:
            levels = [2, 2]
        self._started = True
        self._cur_seed = seed
        self.actions = []
        self._down = {}
        self._taken = set()
        self.match_levels = list(levels)
        self._stop_advertise()
        self._sock.broadcast({"t": "start", "seed": seed,
                              "seats": self.seats_snapshot(),
                              "levels": list(levels)})
        self.events.put(("started", None, {"seed": seed, "levels": list(levels)}))
        return seed

    def close(self):
        """解散房间：广播 room_closed、停广播、关监听；幂等。"""
        if self._closed:
            return
        self._closed = True
        self._stop_advertise()
        self._sock.broadcast({"t": "room_closed"})
        self._sock.close()
        self.events.put(("closed", None, None))

    # -- 事件泵 ------------------------------------------------------------

    def pump(self):
        """UI 线程周期调用：泵取 net 层事件并驱动房间状态机。"""
        if self._closed:
            return
        self._check_takeover()
        while True:
            try:
                kind, peer, payload = self._sock.events.get_nowait()
            except queue.Empty:
                break
            if kind == "connected":
                continue                     # 等其 join 再处理
            if kind == "message":
                self._on_message(peer, payload)
            elif kind == "disconnected":
                self._on_drop(peer, payload)
            elif kind == "error":
                self.events.put(("error", None, str(payload)))

    def _check_takeover(self):
        """掉线座位等待重连超时 → 广播 ai_takeover 并转 AI 代打。"""
        now = time.monotonic()
        for seat, deadline in list(self._down.items()):
            if now < deadline:
                continue
            del self._down[seat]
            self._taken.add(seat)
            self._sock.broadcast({"t": "ai_takeover", "pid": seat})
            self.events.put(("ai_takeover", None,
                             {"seat": seat, "name": self._seats[seat]["name"]}))

    def _on_message(self, peer, payload):
        if not isinstance(payload, dict):
            return
        t = payload.get("t")
        if t == "join":
            self._on_join(peer, payload)
        elif t == "sit":
            self._on_sit(peer, payload)
        elif t == "leave":
            self._on_leave(peer)
        elif t == "act":
            self._on_act(peer, payload)
        elif t == "resync_req":
            self._send_resync(peer)

    # -- 协议处理 ----------------------------------------------------------

    def _on_join(self, peer, payload):
        if payload.get("ver") != PROTOCOL_VER:
            self._reject(peer, "游戏版本不一致，请与主机使用相同版本")
            return
        name = _clamp_name(payload.get("name"), f"玩家{peer}")
        if self._started:
            # 对局中断线重连：昵称精确匹配掉线/被接管座位则归位并发 resync；
            # 否则按新加入拒绝（不支持对局中途换人）
            seat = next((s["seat"] for s in self._seats
                         if s["name"] == name
                         and (s["seat"] in self._down
                              or s["seat"] in self._taken)), None)
            if seat is None:
                self._reject(peer, "对局已开始，无法加入")
                return
            self._down.pop(seat, None)
            self._taken.discard(seat)
            self._occupy(seat, peer, name)
            self._sock.send(peer, {"t": "welcome", "pid": peer,
                                   "name": self.room_name,
                                   "seats": self.seats_snapshot(),
                                   "host_seed_base": self._seed_base})
            self._send_resync(peer)
            if self._end_record is not None and self.game is not None \
                    and self.game.phase == "match_over":
                # 赛末重连：补发本场复盘记录（常规广播时该客人不在线）
                self._sock.send(peer, {"t": "end", "record": self._end_record})
            self._broadcast_seats()
            self.events.put(("rejoined", peer, {"name": name, "seat": seat}))
            return
        if self.seat_of(peer) is not None:
            self._reject(peer, "请勿重复加入")
            return
        seat = next((s["seat"] for s in self._seats if s["is_ai"]), None)
        if seat is None:
            self._reject(peer, "房间已满")
            return
        name = _clamp_name(payload.get("name"), f"玩家{peer}")
        self._occupy(seat, peer, name)
        self._sock.send(peer, {"t": "welcome", "pid": peer,
                               "name": self.room_name,
                               "seats": self.seats_snapshot(),
                               "host_seed_base": self._seed_base})
        self._broadcast_seats()
        self.events.put(("joined", peer, {"name": name, "seat": seat}))

    def _on_sit(self, peer, payload):
        if self._started:
            self._reject(peer, "对局已开始", close=False)
            return
        seat = payload.get("seat")
        if not isinstance(seat, int) or not 0 <= seat <= 3:
            self._reject(peer, "无效座位", close=False)
            return
        cur = self.seat_of(peer)
        if cur is None:
            self._reject(peer, "请先加入房间", close=False)
            return
        if seat == cur:
            return
        if not self._seats[seat]["is_ai"]:
            self._reject(peer, "该座位已被占用", close=False)
            return
        name = self._seats[cur]["name"]
        self._free_seat(cur)
        self._occupy(seat, peer, name)
        self._broadcast_seats()

    def _on_leave(self, peer):
        seat = self.seat_of(peer)
        if seat is not None:
            name = self._seats[seat]["name"]
            if self._started:
                # 对局中主动退出：不等重连，立即 AI 接管该座位
                self._taken.add(seat)
                self._sock.broadcast({"t": "ai_takeover", "pid": seat})
            else:
                self._free_seat(seat)
                self._broadcast_seats()
            self.events.put(("left", peer, {"name": name, "reason": "离开"}))
        self._sock.close_peer(peer)          # 客人已表态离开，主动收线

    def _on_drop(self, peer, reason):
        seat = self.seat_of(peer)
        if seat is None:
            return
        name = self._seats[seat]["name"]
        if self._started:
            # 对局中掉线：保留座位等待重连（_takeover_wait 秒后 AI 接管）
            self._down[seat] = time.monotonic() + self._takeover_wait
            self.events.put(("left", peer, {"name": name, "reason": "掉线"}))
            return
        self._free_seat(seat)
        self._broadcast_seats()
        self.events.put(("left", peer, {"name": name, "reason": "掉线"}))

    # -- 座位内部操作 --------------------------------------------------------

    def _valid_seat(self, seat) -> bool:
        return isinstance(seat, int) and 0 <= seat <= 3

    def _occupy(self, seat, pid, name):
        s = self._seats[seat]
        s.update(name=name, is_ai=False, tier=None, pid=pid)

    def _free_seat(self, seat):
        s = self._seats[seat]
        s.update(name="AI", is_ai=True, tier=self.tier, pid=None)

    def _broadcast_seats(self):
        snap = self.seats_snapshot()
        self._sock.broadcast({"t": "seats", "seats": snap})
        self.events.put(("seats", None, snap))

    def _reject(self, peer, reason: str, close: bool = True):
        """发送拒绝回执；join 级拒绝随即收线，sit 级拒绝保持连接可重试。"""
        self._sock.send(peer, {"t": "reject", "reason": reason})
        if close:
            self._sock.close_peer(peer)

    # -- UDP 房间广播 --------------------------------------------------------

    def _advertise_loop(self, discover_port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            while self._adv_ev.is_set() and not self._closed:
                frame = encode_frame({
                    "t": "room", "name": self.room_name,
                    "host_name": self.host_name,
                    "players": self.players_count,
                    "ver": PROTOCOL_VER, "port": self.port,
                })
                for target in _BROADCAST_TARGETS:
                    try:
                        sock.sendto(frame, (target, discover_port))
                    except OSError:
                        pass             # 无网络接口时不中断广播循环
                self._adv_ev.wait(DISCOVER_INTERVAL)
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _stop_advertise(self):
        if self._adv_ev is not None:
            self._adv_ev.clear()
        thread, self._adv_thread = self._adv_thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)


class RoomClient:
    """房间客人：NetClient + join 状态机。

    join() 在后台线程完成连接与 join 发送（避免 UI 线程被 connect 阻塞），
    之后的状态迁移全部在 pump()（UI 线程）中进行。
    """

    def __init__(self, player_name):
        self.events = queue.Queue()
        self.player_name = _clamp_name(player_name, "玩家")
        self.is_host = False
        self._cli = None
        self._my_pid = None
        self._welcomed = False
        self._started = False
        self._leaving = False
        self._notified = False             # disconnected 事件仅推一次
        self._closed = False
        self._deadline = None              # join 后等 welcome 的时限
        self.room_name = ""
        self.seed_base = None
        self._seats = None                 # 最近一次座位表（welcome/seats）
        self.host_addr = None              # (ip, port)
        # -- 阶段三：动作流 --
        self._applied = 0                  # 已套用到本地镜像的动作数
        self._resync_pending = False       # 已发 resync_req 待应答（防重）
        self.resync = None                 # 最近一次 resync 载荷（UI 消费）

    # -- 属性 ------------------------------------------------------------

    @property
    def welcomed(self) -> bool:
        return self._welcomed

    @property
    def started(self) -> bool:
        return self._started

    @property
    def active(self) -> bool:
        """会话仍挂着一个可用连接（未 close 且未主动离开）。"""
        return not self._closed and not self._leaving and self._cli is not None

    @property
    def my_pid(self):
        return self._my_pid

    @property
    def my_seat(self) -> int | None:
        """自己在座位表中的座位号；未知返回 None。"""
        if self._my_pid is None or not self._seats:
            return None
        for s in self._seats:
            if s.get("pid") == self._my_pid:
                return s["seat"]
        return None

    @property
    def applied_seq(self) -> int:
        """本地镜像已套用的动作数（seq 连续性依据）。"""
        return self._applied

    # -- 动作流（阶段三）-----------------------------------------------------

    def send_act(self, kind: str, *, eids=None, eid=None, round=None) -> bool:
        """上行本座位动作（pid=自己座位号）；是否接受以广播/act_ack 为准。"""
        if self._cli is None or not self._started:
            return False
        seat = self.my_seat
        if seat is None or kind not in ACT_KINDS:
            return False
        msg = {"t": "act", "kind": kind, "pid": seat,
               "round": round if round is not None else 0}
        if eids is not None:
            msg["eids"] = [int(e) for e in eids]
        if eid is not None:
            msg["eid"] = int(eid)
        return self._cli.send(msg)

    def request_resync(self) -> bool:
        """请求全量动作流重发（seq 断档/套用失败自愈）。"""
        if self._cli is None or not self._started or self._resync_pending:
            return False
        self._resync_pending = True
        return self._cli.send({"t": "resync_req", "have_seq": self._applied})

    def mark_applied(self) -> None:
        """UI 套用一条 act 后调用（seq 推进；漏调会触发 resync 自愈）。"""
        self._applied += 1

    def ingest_act(self, act: dict) -> bool:
        """UI 套用前过闸：True=可套用；False=跳过（重复帧或已触发 resync）。

        - seq <= applied：resync 重演已覆盖的重复帧，跳过（防止二次套用
          抛 GameError 引发 resync 风暴）；
        - seq > applied+1：真断档（该帧永远不会再来的漂移场景），自动
          request_resync 全量重演；网络线程的假断档（泵滞后）已在
          _on_message 层放行入队，不会走到这里。
        成功套用后 UI 仍需调用 mark_applied()。
        """
        seq = act.get("seq")
        if not isinstance(seq, int):
            return True
        if seq <= self._applied:
            return False
        if seq > self._applied + 1:
            self.request_resync()
            return False
        return True

    def seats_snapshot(self) -> list:
        return _seats_view(self._seats) if self._seats else []

    # -- 操作 ---------------------------------------------------------------

    def join(self, host_ip, port=DEFAULT_PORT) -> bool:
        """连接主机并请求加入（后台线程）；重复调用忽略。"""
        if self._cli is not None or self._closed or self._leaving:
            return False
        self.host_addr = (host_ip, port)
        threading.Thread(target=self._connect, args=(host_ip, port),
                         name="guandan-join", daemon=True).start()
        return True

    def try_sit(self, seat: int) -> bool:
        """选座/换座请求；是否成功以 seats 广播为准，被拒收 sit_rejected。"""
        if self._cli is None or not self._welcomed or self._started:
            return False
        return self._cli.send({"t": "sit", "seat": int(seat)})

    def leave(self):
        """退出房间：尽力通知主机后收线。"""
        self._leaving = True
        if self._cli is not None:
            self._cli.send({"t": "leave"})
        self.close()

    def close(self):
        """关闭会话；幂等。"""
        self._closed = True
        cli, self._cli = self._cli, None
        if cli is not None:
            cli.close()

    # -- 事件泵 ------------------------------------------------------------

    def pump(self):
        """UI 线程周期调用：welcome 超时看门狗 + 泵取 net 层事件。"""
        if self._closed:
            return
        if (self._deadline is not None and not self._welcomed
                and time.monotonic() > self._deadline):
            self._deadline = None
            self._notify("主机无响应，请确认房间未解散或版本一致")
            self.close()
            return
        cli = self._cli
        if cli is None:
            return
        while True:
            try:
                kind, _peer, payload = cli.events.get_nowait()
            except queue.Empty:
                break
            if kind == "connected":
                self._deadline = time.monotonic() + JOIN_TIMEOUT
            elif kind == "message":
                self._on_message(payload)
            elif kind == "disconnected":
                if not self._leaving:
                    self._notify(_DROP_REASONS.get(payload, "与主机断开连接"))
                self.close()
            elif kind == "error":
                self.events.put(("error", None, str(payload)))

    def _on_message(self, payload):
        if not isinstance(payload, dict):
            return
        t = payload.get("t")
        if t == "welcome":
            self._my_pid = payload.get("pid")
            self.room_name = str(payload.get("name") or "")
            self.seed_base = payload.get("host_seed_base")
            self._seats = payload.get("seats") or []
            self._welcomed = True
            self._deadline = None
            self.events.put(("joined", self._my_pid,
                             {"seats": self.seats_snapshot(),
                              "room_name": self.room_name}))
        elif t == "seats":
            self._seats = payload.get("seats") or []
            self.events.put(("seats", None, self.seats_snapshot()))
        elif t == "reject":
            reason = str(payload.get("reason") or "被主机拒绝")
            if self._welcomed:
                self.events.put(("sit_rejected", None, reason))
            else:
                self._notify(reason)
                self.close()
        elif t == "start":
            self._started = True
            self._seats = payload.get("seats") or self._seats
            self._applied = 0               # 新一场：动作流从零计
            self._resync_pending = False
            self.events.put(("started", None,
                             {"seed": payload.get("seed"),
                              "seats": self.seats_snapshot(),
                              "levels": payload.get("levels")}))
        elif t == "act":
            seq = payload.get("seq")
            if isinstance(seq, int) and seq > self._applied:
                # 超前帧（seq > applied+1）也直接入队：TCP 保序下这只说明
                # UI 还没套用前几帧（泵滞后/动作连发），按序套用即可追平；
                # 在此误判断档会触发假 resync，导致客人端重建清空整理手牌
                self.events.put(("act", None, payload))
            # seq <= applied：重复帧（resync 重演已覆盖），静默忽略
        elif t == "resync":
            self.resync = payload
            self._applied = len(payload.get("actions") or [])
            self._resync_pending = False
            self._started = True
            if payload.get("seats"):
                self._seats = payload["seats"]
            self.events.put(("resync", None, payload))
        elif t == "act_ack":
            if not payload.get("ok"):
                self.events.put(("act_rejected", None,
                                 str(payload.get("err") or "动作被拒绝")))
        elif t == "ai_takeover":
            self.events.put(("ai_takeover", None, payload))
        elif t == "end":
            # 阶段四：主机分发的赛末复盘记录（record 为完整 replay JSON）
            self.events.put(("end", None, payload.get("record")))
        elif t == "room_closed":
            if not self._leaving:
                self._notify("主机已解散房间")
            self.close()
        # 其余类型忽略

    def _connect(self, host_ip, port):
        """后台线程：建立连接并发送 join（失败推 error 事件）。"""
        try:
            cli = NetClient(host_ip, port)
        except OSError:
            self.events.put(("error", None, "无法连接主机，请检查 IP 与网络"))
            return
        if self._closed or self._leaving:  # 连接期间用户已取消
            cli.close()
            return
        self._cli = cli                    # 先挂队列再发 join，事件序有保障
        cli.send({"t": "join", "name": self.player_name, "ver": PROTOCOL_VER})

    def _notify(self, text: str):
        """推送一次性终态事件（rejected/disconnected 共用防重）。"""
        if self._notified:
            return
        self._notified = True
        self.events.put(("disconnected", None, text))


_DROP_REASONS = {
    "closed": "已退出房间",
    "reset": "与主机断开连接",
    "timeout": "连接超时",
    "protocol": "协议错误",
    "error": "连接异常",
}


class RoomBrowser:
    """UDP 房间发现（客户端侧）：监听 room 广播帧并按主机 IP 聚合。

    snapshot() 返回存活条目（stale 秒内有广播）列表，按发现先后排序；
    条目字段：ip / port / name / host_name / players / ver。
    """

    def __init__(self, port=DISCOVER_PORT, *, stale=ROOM_STALE):
        self._stale = stale
        self._rooms = {}                   # ip -> {info..., "seen": monotonic}
        self._first_seen = {}              # ip -> 首次发现序（排序用）
        self._order = 0
        self._lock = threading.Lock()
        self._alive = True
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("", port))
        self._sock.settimeout(0.5)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._recv_loop,
                                        name="guandan-discover", daemon=True)
        self._thread.start()

    def snapshot(self) -> list:
        """当前存活房间列表（剔除过期条目，按发现先后排序）。"""
        now = time.monotonic()
        with self._lock:
            dead = [ip for ip, r in self._rooms.items()
                    if now - r["seen"] > self._stale]
            for ip in dead:
                self._rooms.pop(ip, None)
                self._first_seen.pop(ip, None)
            entries = sorted(self._rooms.values(),
                             key=lambda r: self._first_seen.get(r["ip"], 0))
            return [{k: r[k] for k in
                     ("ip", "port", "name", "host_name", "players", "ver")}
                    for r in entries]

    def close(self):
        """停止监听；幂等。"""
        self._alive = False
        try:
            self._sock.close()
        except OSError:
            pass
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)

    def _recv_loop(self):
        decoder = FrameDecoder()
        while self._alive:
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break                      # 套接字已关闭
            try:
                msgs = decoder.feed(data)
            except ValueError:
                continue
            for msg in msgs:
                if isinstance(msg, dict) and msg.get("t") == "room":
                    self._record(addr[0], msg)

    def _record(self, ip, msg):
        info = {"ip": ip,
                "port": int(msg.get("port") or DEFAULT_PORT),
                "name": str(msg.get("name") or "")[:MAX_NAME],
                "host_name": str(msg.get("host_name") or "")[:MAX_NAME],
                "players": int(msg.get("players") or 0),
                "ver": msg.get("ver"),
                "seen": time.monotonic()}
        with self._lock:
            if ip not in self._first_seen:
                self._first_seen[ip] = self._order
                self._order += 1
            self._rooms[ip] = info
