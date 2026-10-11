"""牌局状态与流程控制。

职责：发牌、回合流转、合法出牌校验（统一委托 rules 引擎）、进贡还贡、
接风、升级逻辑、胜负判定。本模块不包含任何 UI 或 AI 策略逻辑。

座位与队伍：0=人类玩家（下方），1=右家AI，2=对家AI（上方），3=左家AI。
队伍：0队={0,2}，1队={1,3}，按 0→1→2→3 轮转。

规则要点（V1.0 通用标准，待确认项见 README/变更摘要）：
- 升级：双下+3、头游+三游+2、头游+末游+1，封顶 A(14)。
- 一方两家全部出完即本局结束，未打完的两家直接记末游。
- 打 A 的队伍拿到头游即获胜（若需"打A必须双下"为变体规则，待确认）。
- 进贡：发牌后，末游向头游进手中最大牌（红桃级牌/逢人配除外，若全为逢人配则进最大）；
  双下时三游另向二游进贡。进贡方合计持有两张大王可抗贡。
- 还贡：受贡方任意还一张。
- 下一局首出：进贡方（末游）先出；抗贡则头游先出。
- 接风：最后出牌者出完且其余各家均过牌，则由其对家接风首出。
"""
from __future__ import annotations

import random
from typing import Dict, List, Optional, Tuple

from cards import BIG_JOKER, Card, build_deck, point_value
from rules import Combo, RuleConfig, can_play


class GameError(Exception):
    pass


class IllegalPlayError(GameError):
    pass


class NotPlayerTurnError(GameError):
    pass


class GuandanGame:
    HAND_SIZE = RuleConfig.HAND_SIZE
    PLAYERS = RuleConfig.PLAYERS

    def __init__(self, rng: Optional[random.Random] = None) -> None:
        self.rng = rng or random.Random()
        self.levels: Dict[int, int] = {0: 2, 1: 2}
        self.playing_team = 0
        self.round_number = 0
        self.phase = "setup"  # setup | playing | tribute | round_over | match_over
        self.hands: Dict[int, List[Card]] = {i: [] for i in range(4)}
        self.turn = 0
        self.last_play: Optional[Tuple[int, Combo]] = None
        self.trick_displays: Dict[int, dict] = {}  # pid -> {"action": "play"/"pass", "combo": Combo}
        self.passed_since: set = set()
        self.finish_order: List[int] = []
        self.tribute_pairs: List[Tuple[int, int]] = []  # (giver, receiver)
        self.tribute_done: Dict[int, int] = {}  # giver -> receiver
        self.return_done: Dict[int, int] = {}  # receiver -> giver
        self.tribute_refused = False
        self.tribute_displays: Dict[int, dict] = {}  # pid -> {"kind": "tribute"/"return", "card": Card}
        self.tribute_refused_by: List[int] = []  # 抗贡成功方（持双大王者）
        self.round_result: Optional[dict] = None
        self.match_winner: Optional[int] = None
        self.next_leader = 0
        self.log: List[str] = []
        self.start_match()

    # ------------------------------------------------------------------ 基础
    @staticmethod
    def team_of(pid: int) -> int:
        return pid % 2

    @staticmethod
    def partner(pid: int) -> int:
        return (pid + 2) % 4

    @property
    def level(self) -> int:
        return self.levels[self.playing_team]

    def hand_count(self, pid: int) -> int:
        return len(self.hands[pid])

    def active_players(self) -> List[int]:
        return [p for p in range(4) if p not in self.finish_order]

    # ------------------------------------------------------------------ 发牌
    def start_match(self) -> None:
        self.levels = {0: 2, 1: 2}
        self.playing_team = 0
        self.round_number = 0
        self.match_winner = None
        self.tribute_pairs = []
        self.next_leader = 0
        self._deal_round(leader=0)

    def _deal_round(self, leader: int) -> None:
        deck = build_deck()
        assert len(deck) == 108
        self.rng.shuffle(deck)
        self.hands = {i: deck[i * self.HAND_SIZE:(i + 1) * self.HAND_SIZE]
                      for i in range(4)}
        assert all(len(h) == self.HAND_SIZE for h in self.hands.values())
        self.round_number += 1
        self.finish_order = []
        self.last_play = None
        self.trick_displays = {}
        self.passed_since = set()
        self.tribute_done = {}
        self.return_done = {}
        self.tribute_refused = False
        self.tribute_displays = {}
        self.tribute_refused_by = []
        self.round_result = None
        if self.tribute_pairs:
            big_count = sum(
                1 for g, _ in self.tribute_pairs
                for c in self.hands[g] if c.rank == BIG_JOKER
            )
            if big_count >= 2:
                self.tribute_refused = True
                self.tribute_refused_by = [g for g, _ in self.tribute_pairs]
                self.tribute_pairs = []
                self.next_leader = getattr(self, "_round_head", leader)
                self.phase = "playing"
                self.turn = self.next_leader
                self.log.append("抗贡成功，头游先出")
            else:
                self.next_leader = getattr(self, "_round_last", leader)
                self.phase = "tribute"
                self.turn = self.tribute_pairs[0][0]
        else:
            self.phase = "playing"
            self.turn = leader
        self.log.append(f"=== 第{self.round_number}局 打{self._rank_text(self.level)} 首出P{self.turn} ===")

    def _rank_text(self, rank: int) -> str:
        from cards import RANK_LABELS
        return RANK_LABELS[rank]

    # ------------------------------------------------------------------ 进贡还贡
    def legal_tribute_cards(self, giver: int) -> List[Card]:
        """进贡候选：手中最大牌（逢人配除外；若全为逢人配则取最大）。"""
        hand = self.hands[giver]
        level = self.level
        non_wild = [c for c in hand if not c.is_wild(level)]
        pool = non_wild if non_wild else hand
        best = max(point_value(c.rank, level) for c in pool)
        return [c for c in pool if point_value(c.rank, level) == best]

    def submit_tribute(self, giver: int, eid: int) -> Card:
        if self.phase != "tribute":
            raise GameError("当前不在进贡阶段")
        pending = [(g, r) for g, r in self.tribute_pairs if g not in self.tribute_done]
        match = next(((g, r) for g, r in pending if g == giver), None)
        if match is None:
            raise GameError(f"P{giver} 无待进贡任务")
        legal = self.legal_tribute_cards(giver)
        card = next((c for c in legal if c.eid == eid), None)
        if card is None:
            raise IllegalPlayError("进贡牌必须为手中最大牌（逢人配除外）")
        receiver = match[1]
        self.hands[giver].remove(card)
        self.hands[receiver].append(card)
        self.tribute_done[giver] = receiver
        self.tribute_displays[giver] = {"kind": "tribute", "card": card}
        self.log.append(f"P{giver} 进贡 {card.label()} 给 P{receiver}")
        self._maybe_finish_tribute_phase()
        return card

    def submit_return(self, receiver: int, eid: int) -> Card:
        if self.phase != "tribute":
            raise GameError("当前不在还贡阶段")
        # receiver 必须是"已收到进贡且尚未还贡"的一方
        giver = self.tribute_done.get(receiver)
        if giver is None and receiver not in self.tribute_done.values():
            raise GameError(f"P{receiver} 无待还贡任务")
        if receiver in self.return_done:
            raise GameError(f"P{receiver} 已还贡")
        if giver is None:
            giver = next(g for g, r in self.tribute_done.items() if r == receiver)
        card = next((c for c in self.hands[receiver] if c.eid == eid), None)
        if card is None:
            raise IllegalPlayError("还贡牌不在手中")
        self.hands[receiver].remove(card)
        self.hands[giver].append(card)
        self.return_done[receiver] = giver
        self.tribute_displays[receiver] = {"kind": "return", "card": card}
        self.log.append(f"P{receiver} 还贡 {card.label()} 给 P{giver}")
        self._maybe_finish_tribute_phase()
        return card

    def _maybe_finish_tribute_phase(self) -> None:
        if len(self.tribute_done) == len(self.tribute_pairs) and \
                len(self.return_done) == len(self.tribute_pairs):
            self.phase = "playing"
            self.turn = self.next_leader
            # 进贡/还贡展示保留到牌局正式开始（begin_round）时再清除

    def begin_round(self) -> None:
        """牌局正式开始：清除进贡/还贡/抗贡的开局展示。"""
        self.tribute_displays = {}
        self.tribute_refused_by = []

    # ------------------------------------------------------------------ 出牌
    def play(self, pid: int, cards: List[Card]) -> Combo:
        if self.phase != "playing":
            raise IllegalPlayError("当前阶段不能出牌")
        if pid != self.turn:
            raise NotPlayerTurnError(f"未到 P{pid} 的回合")
        if not cards:
            raise IllegalPlayError("出牌不能为空")
        eids = [c.eid for c in cards]
        if len(set(eids)) != len(eids):
            raise IllegalPlayError("存在重复实体牌")
        by_eid = {c.eid: c for c in self.hands[pid]}
        real = [by_eid.get(e) for e in eids]
        if any(c is None for c in real):
            raise IllegalPlayError("出牌包含非当前手牌的实体牌")
        prev = self.last_play[1] if self.last_play else None
        combo = can_play(real, prev, self.level)
        if combo is None:
            raise IllegalPlayError("非法出牌：牌型不成立或压不过当前牌")
        if self.last_play is None:
            self.tribute_displays = {}   # 牌局开始：清除进贡/还贡展示
        for c in real:
            self.hands[pid].remove(c)
        self.last_play = (pid, combo)
        self.trick_displays[pid] = {"action": "play", "combo": combo}
        self.passed_since = set()
        self.log.append(f"P{pid} 出 {combo.describe()}")
        if not self.hands[pid]:
            self.finish_order.append(pid)
            self.log.append(f"P{pid} 成为第{len(self.finish_order)}游")
            # 一方两家全部出完：本局结束，未打完的直接记末游（无需继续打）
            if sum(1 for p in self.finish_order
                   if self.team_of(p) == self.team_of(pid)) == 2:
                rest = self.active_players()
                self.finish_order.extend(rest)
                if rest:
                    self.log.append(
                        f"{self.team_of(pid)}队两家出完，"
                        + "、".join(f"P{p}" for p in rest) + "记末游")
                self._end_round(auto_last=rest)
                return combo
        if len(self.finish_order) == 3:
            self._end_round()
        else:
            self._advance()
        return combo

    def pass_play(self, pid: int) -> None:
        if self.phase != "playing":
            raise IllegalPlayError("当前阶段不能过牌")
        if pid != self.turn:
            raise NotPlayerTurnError(f"未到 P{pid} 的回合")
        if self.last_play is None or self.last_play[0] == pid:
            raise IllegalPlayError("主动出牌时不能过牌")
        self.passed_since.add(pid)
        self.trick_displays[pid] = {"action": "pass", "combo": None}
        self.log.append(f"P{pid} 过牌")
        self._advance()

    def _next_active(self, from_pid: int) -> int:
        for i in range(1, 5):
            p = (from_pid + i) % 4
            if p not in self.finish_order:
                return p
        return from_pid

    def _advance(self) -> None:
        owner = self.last_play[0] if self.last_play else None
        active = self.active_players()
        if owner is not None:
            others = [p for p in active if p != owner]
            if others and all(p in self.passed_since for p in others):
                # 本轮牌权结算：回合结束，清除各玩家出牌展示（下一轮出牌开始）
                self.passed_since = set()
                self.last_play = None
                self.trick_displays = {}
                if owner in active:
                    self.turn = owner
                else:
                    pt = self.partner(owner)
                    self.turn = pt if pt in active else self._next_active(owner)
                    if pt not in active:
                        self.log.append(f"P{owner} 出完无人接，对家已出完，顺延首出")
                    else:
                        self.log.append(f"接风：P{pt} 首出")
                return
        self.turn = self._next_active(self.turn)

    # ------------------------------------------------------------------ 结算
    def _end_round(self, auto_last: Optional[List[int]] = None) -> None:
        fo = list(self.finish_order)
        if len(fo) < 4:
            tail = next(p for p in range(4) if p not in fo)
            fo.append(tail)
            auto_last = list(auto_last or []) + [tail]
        head, second, third, last = fo
        wt = self.team_of(head)
        if self.team_of(second) == wt:
            delta = 3
        elif self.team_of(third) == wt:
            delta = 2
        else:
            delta = 1
        self.round_result = {
            "finish_order": fo,
            "winner_team": wt,
            "delta": delta,
            "double_down": self.team_of(second) == wt,
            "auto_last": list(auto_last or []),  # 未打完被记为末游的玩家
        }
        self.log.append(
            f"本局结束 名次P{head}>P{second}>P{third}>P{last}，{wt}队升{delta}级")
        if self.playing_team == wt and self.levels[wt] == 14:
            self.phase = "match_over"
            self.match_winner = wt
            self.log.append(f"{wt}队打过A，比赛结束")
            return
        self.levels[wt] = min(14, self.levels[wt] + delta)
        self.playing_team = wt
        # 进贡计划（发新牌后执行）；保存头游/末游用于抗贡判定首出者
        self._round_head = head
        self._round_last = last
        if self.team_of(second) == wt:  # 双下：末游→头游，三游→二游
            self.tribute_pairs = [(last, head), (third, second)]
        else:
            self.tribute_pairs = [(last, head)]
        self.phase = "round_over"

    def start_next_round(self) -> None:
        if self.phase == "match_over":
            raise GameError("比赛已结束")
        if self.phase != "round_over":
            raise GameError("本局尚未结束")
        self._deal_round(leader=self.next_leader)
