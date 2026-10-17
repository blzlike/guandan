"""牌局记录与重演（阶段一：Recorder + rebuild）。

Recorder 挂在 ``game.listeners`` 上记录全部动作（进贡/还贡/出牌/过牌），
record 为纯 JSON 结构（version/seed/levels/actions/result）。``rebuild``
用同 seed 重建 GuandanGame 并逐步套用动作，重演出整场比赛（build_deck 的
eid 0..107 确定性分配 + 同 seed 洗牌 ⇒ eid 与原局一一对应）。

用途：阶段一回归测试（重演 log 与原局逐行一致）；阶段四复盘界面直接
复用本模块做逐动作步进与逐局跳转。

限制：record 须从比赛开始（match 起点）挂载录制；seed 未知（None）时
可记录但不可 rebuild。
"""
from __future__ import annotations

import json
import random
from typing import Any, Dict, List, Optional

from game import GuandanGame

RECORD_VERSION = 1


class Recorder:
    """事件监听器：记录一整场比赛的动作流与结果。"""

    def __init__(self, seed: Optional[int] = None) -> None:
        self.seed = seed
        self.levels: Dict[int, int] = {0: 2, 1: 2}
        self.actions: List[Dict[str, Any]] = []
        self.rounds: List[Dict[str, Any]] = []   # 每局 round_end 结果
        self.match_winner: Optional[int] = None
        self.final_levels: Optional[Dict[int, int]] = None

    # ---------------------------------------------------------------- 挂载
    def attach(self, game: GuandanGame) -> None:
        if game.round_number != 1 or game.played_cards:
            raise ValueError("Recorder 须在比赛起点挂载（新 GuandanGame 之后、"
                             "任何动作之前）")
        self.levels = dict(game.levels)
        game.listeners.append(self._on_event)

    def detach(self, game: GuandanGame) -> None:
        """从 game.listeners 移除（「再来一局」换新 Recorder 前调用）。"""
        try:
            game.listeners.remove(self._on_event)
        except ValueError:
            pass

    def _on_event(self, game: GuandanGame, event: str, data: dict) -> None:
        rnd = game.round_number
        if event == "round_start":
            self.actions.append({"kind": "round_start", "round": rnd})
        elif event == "play":
            self.actions.append({"kind": "play", "round": rnd,
                                 "pid": data["pid"], "eids": list(data["eids"])})
        elif event == "pass":
            self.actions.append({"kind": "pass", "round": rnd,
                                 "pid": data["pid"]})
        elif event == "tribute":
            self.actions.append({"kind": "tribute", "round": rnd,
                                 "pid": data["pid"], "eid": data["eid"]})
        elif event == "return":
            self.actions.append({"kind": "return", "round": rnd,
                                 "pid": data["pid"], "eid": data["eid"]})
        elif event == "round_end":
            self.rounds.append(dict(data["result"]))
        elif event == "match_end":
            self.match_winner = data["winner"]
            self.final_levels = dict(data["levels"])

    # ---------------------------------------------------------------- 导出
    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": RECORD_VERSION,
            "seed": self.seed,
            # JSON 键只能为字符串：levels 统一 str(pid) 存储，rebuild 转回 int
            "levels": {str(k): v for k, v in self.levels.items()},
            "actions": [dict(a) for a in self.actions],
            "result": {
                "rounds": [dict(r) for r in self.rounds],
                "match_winner": self.match_winner,
                "levels": {str(k): v for k, v in (self.final_levels or {}).items()}
                          if self.final_levels is not None else None,
            },
        }

    def save_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False)


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        record = json.load(fh)
    if record.get("version") != RECORD_VERSION:
        raise ValueError(f"不支持的记录版本: {record.get('version')}")
    return record


def apply_action(game: GuandanGame, act: Dict[str, Any],
                 on_action: Optional[callable] = None) -> None:
    """套用单个动作（round 跳变时自动 start_next_round）。

    ``on_action(act, game)`` 仅对非 round_start 动作、在套用前回调
    （此刻 game 即该座位的决策时刻）。rebuild 与 ReplaySession 共用。
    """
    kind, rnd = act["kind"], act.get("round", 1)
    if kind == "round_start":
        if game.round_number < rnd:
            game.start_next_round()
        return
    if game.round_number < rnd:
        game.start_next_round()
    if on_action is not None:
        on_action(act, game)

    def card_of(pid: int, eid: int):
        for c in game.hands[pid]:
            if c.eid == eid:
                return c
        raise ValueError(f"P{pid} 手中无 eid={eid}")

    if kind == "play":
        game.play(act["pid"], [card_of(act["pid"], e) for e in act["eids"]])
    elif kind == "pass":
        game.pass_play(act["pid"])
    elif kind == "tribute":
        game.submit_tribute(act["pid"], act["eid"])
    elif kind == "return":
        game.submit_return(act["pid"], act["eid"])
    else:
        raise ValueError(f"未知动作类型: {kind}")


def apply_net_act(game: GuandanGame, act: Dict[str, Any]) -> None:
    """联机动作套用（客户端镜像/主机代打统一入口，好友联机阶段三）。

    与 apply_action 的差异：套用行牌类动作后清除贡牌/抗贡开局展示——
    主机端由 UI 展示保留期后调 begin_round，客户端没有对应时机，改为在
    收到本回合首个行牌动作时就地清除（begin_round 只清展示、不动逻辑）。
    清除须在 apply_action **之后**：round 跳变时 _deal_round 会重设本局
    展示，先清会被跳局覆盖，导致与主机端失步（抗贡横幅残留）。
    """
    apply_action(game, act)
    if act["kind"] in ("play", "pass"):
        game.begin_round()


def rebuild(record: Dict[str, Any],
            attach_recorder: Optional[Recorder] = None,
            on_action: Optional[callable] = None,
            stop_after: Optional[int] = None) -> GuandanGame:
    """按 record 重演比赛，返回套用动作后的 GuandanGame。

    默认重演整场（phase=match_over）；``stop_after`` 传入时最多套用
    前 N 条动作（阶段四复盘的前缀重演：回退/跳局用）。其余同上。
    """
    if record.get("seed") is None:
        raise ValueError("record 缺少 seed，无法重演")
    game = GuandanGame(rng=random.Random(record["seed"]))
    saved_levels = record.get("levels") or {0: 2, 1: 2}
    game.levels = {int(k): v for k, v in saved_levels.items()}
    if attach_recorder is not None:
        attach_recorder.attach(game)

    for i, act in enumerate(record["actions"]):
        if stop_after is not None and i >= stop_after:
            break
        apply_action(game, act, on_action)
    return game


class ReplaySession:
    """逐动作回放会话（阶段四复盘界面的逻辑层）。

    index = 已套用 record["actions"] 的条数；index=0 即开局发牌完毕、
    尚无动作。前进为增量单步；回退/跳转按前缀重演（与 rebuild 同逻辑，
    单场毫秒级）。
    """

    def __init__(self, record: Dict[str, Any]) -> None:
        if record.get("seed") is None:
            raise ValueError("record 缺少 seed，无法回放")
        self.record = record
        self.actions: List[Dict[str, Any]] = record["actions"]
        self.game = self._fresh()
        self.index = 0
        self.meta = self._analyze()   # 每动作的轮内顺序号/轮次（复盘界面用）

    # ------------------------------------------------ 轮次/顺序号推导
    def _analyze(self) -> List[Dict[str, Optional[int]]]:
        """逐动作模拟，推导 play/pass 的轮内顺序号与轮次号。

        seq   = 本轮内第几次行牌（出牌+过牌都计数，每轮从 1 起）；
        trick = 本局轮序号（主动出牌开新一轮，收牌后下一手即 +1）；
        round_start/tribute/return 两项均为 None。
        """
        game = self._fresh()
        meta: List[Dict[str, Optional[int]]] = []
        seq = trick = 0
        for act in self.actions:
            kind = act["kind"]
            if kind == "round_start":
                trick = 0
                meta.append({"seq": None, "trick": None})
            elif kind in ("tribute", "return"):
                meta.append({"seq": None, "trick": None})
            else:
                if kind == "play" and game.last_play is None:
                    trick += 1
                    seq = 0
                seq += 1
                meta.append({"seq": seq, "trick": trick})
            apply_action(game, act)
        return meta

    def next_play_trick(self, n: int) -> Optional[int]:
        """第 n 条起（含）第一个未套用 play/pass 动作的轮次号；无则 None。"""
        for j in range(n, len(self.actions)):
            t = self.meta[j]["trick"]
            if t is not None:
                return t
        return None

    def _trick_of_applied(self, n: int) -> Optional[int]:
        """已套用前 n 条动作中、本局最后一次行牌的轮次号；无则 None。"""
        for i in range(n - 1, -1, -1):
            if self.actions[i]["kind"] == "round_start":
                return None
            t = self.meta[i]["trick"]
            if t is not None:
                return t
        return None

    def next_trick_target(self) -> Optional[int]:
        """「下一轮」目标 index：下一未套用行牌所在轮的结束点。"""
        i = self.index
        j = i
        while j < len(self.actions) and self.meta[j]["trick"] is None:
            j += 1
        if j >= len(self.actions):
            return None
        t = self.meta[j]["trick"]
        k = j
        while k < len(self.actions):
            if self.actions[k]["kind"] == "round_start":
                break
            mt = self.meta[k]["trick"]
            if mt is not None and mt != t:
                break
            k += 1
        return k

    def prev_trick_target(self) -> Optional[int]:
        """「上一轮」目标 index：本轮起点；已在起点则上一轮起点（首轮回本局开局）。"""
        i = self.index
        cur = self._trick_of_applied(i)
        if cur is None:
            return None
        s = next(j for j in range(i) if self.meta[j]["trick"] == cur)
        if i > s:
            return s
        if cur > 1:
            return next(j for j in range(i) if self.meta[j]["trick"] == cur - 1)
        for j in range(s - 1, -1, -1):
            if self.actions[j]["kind"] == "round_start":
                return j + 1
        return 0

    def _fresh(self) -> GuandanGame:
        game = GuandanGame(rng=random.Random(self.record["seed"]))
        saved = self.record.get("levels") or {0: 2, 1: 2}
        game.levels = {int(k): v for k, v in saved.items()}
        return game

    def total(self) -> int:
        return len(self.actions)

    def forward(self) -> Optional[Dict[str, Any]]:
        """套用下一条动作并返回它；已到末尾返回 None。"""
        if self.index >= len(self.actions):
            return None
        act = self.actions[self.index]
        apply_action(self.game, act)
        self.index += 1
        return act

    def goto_index(self, n: int) -> None:
        """重演到「已套用前 n 条」的状态（向前增量、向后整段重建前缀）。"""
        n = max(0, min(int(n), len(self.actions)))
        if n == self.index:
            return
        if n > self.index:
            while self.index < n:
                self.forward()
            return
        game = self._fresh()
        for i in range(n):
            apply_action(game, self.actions[i])
        self.game, self.index = game, n

    def back(self) -> None:
        if self.index > 0:
            self.goto_index(self.index - 1)

    def goto_end(self) -> None:
        while self.forward() is not None:
            pass

    def goto_round(self, rnd: int) -> None:
        """跳到第 rnd 局开始（发牌完毕、本局 0 动作）；越界取最近有效局。"""
        rounds = sorted({a.get("round", 1) for a in self.actions}) or [1]
        rnd = max(rounds[0], min(rnd, rounds[-1]))
        idx = next((i for i, a in enumerate(self.actions)
                    if a.get("round", 1) == rnd), None)
        if idx is None:
            return
        if self.actions[idx]["kind"] == "round_start":
            self.goto_index(idx + 1)   # 套用 round_start（发牌），停在 0 动作
        else:
            self.goto_index(idx)       # 首局无 round_start 录入：0 即发牌态
