"""教学观察者：基于「公开信息 + 玩家0手牌」口径的牌局数据展示。

教学体系模块一（阶段三）。信息口径（架构决策②）：
- 公开信息：本局已出牌（game.played_cards）、各家剩牌数、当前出牌展示、
  名次、级牌/队伍等局面字段；
- 玩家0手牌：人类玩家自己的手牌；
- 对手手牌只做统计推断（未见牌分布），绝不读取、绝不显示。

与 AI 体系互不引用：本模块只依赖 cards/rules/game，不 import ai/registry。

PlayTracker 为已出牌数据库（listener 挂载，比赛起点 attach），
配合 PlayTracker.predict_rows 预留的剩牌推测接口构成面板数据层。
配额口径：点数 2..A 各 8 张，王各 2 张；
每花色 13 点 × 2 副 = 26 张；逢人配 = 红桃级牌（2 张，含于级牌配额）。
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from cards import BIG_JOKER, SMALL_JOKER, Card, RANK_LABELS, Suit

_SUIT_ORDER = (Suit.SPADE, Suit.HEART, Suit.CLUB, Suit.DIAMOND)
_SUIT_QUOTA = 26          # 每花色配额（13 点 × 2 副）


class Observer:
    """以玩家 ``pid``（默认人类 0）视角观察牌局，产出教学数据。"""

    def __init__(self, game, pid: int = 0) -> None:
        self.game = game
        self.pid = pid
        self.level = game.level
        self._counts()      # 预计算核心统计

    # ------------------------------------------------------------ 基础统计
    def _counts(self) -> None:
        """统计我手/已出，得到按点数与按花色的未见分布。"""
        mine = self.game.hands.get(self.pid, [])
        played = self.game.played_cards
        self.mine_by_rank: Dict[int, int] = {}
        self.mine_by_suit: Dict[Suit, int] = {}
        self.mine_cards: List[Card] = list(mine)
        for c in mine:
            self.mine_by_rank[c.rank] = self.mine_by_rank.get(c.rank, 0) + 1
            if not c.is_joker:
                self.mine_by_suit[c.suit] = self.mine_by_suit.get(c.suit, 0) + 1
        seen: Dict[int, int] = dict(self.mine_by_rank)
        for c in played:
            seen[c.rank] = seen.get(c.rank, 0) + 1
        totals = {**{r: 8 for r in range(2, 15)}, SMALL_JOKER: 2, BIG_JOKER: 2}
        self.unseen: Dict[int, int] = {r: totals[r] - seen.get(r, 0)
                                       for r in totals}
        # 花色：已出非王牌按花色归并；未见 = 配额 - 我手 - 已出
        played_by_suit: Dict[Suit, int] = {}
        for c in played:
            if not c.is_joker:
                played_by_suit[c.suit] = played_by_suit.get(c.suit, 0) + 1
        self.unseen_by_suit: Dict[Suit, int] = {
            s: _SUIT_QUOTA - self.mine_by_suit.get(s, 0) - played_by_suit.get(s, 0)
            for s in _SUIT_ORDER}

    # ------------------------------------------------------------ 查询接口
    def core_rows(self) -> List[Tuple[str, int, int, int]]:
        """核心牌行：(名称, 我持, 已出, 未见)。逢人配含于级牌。"""
        played = self.game.played_cards
        lv = self.level

        def stat(pred) -> Tuple[int, int]:
            return (sum(1 for c in self.mine_cards if pred(c)),
                    sum(1 for c in played if pred(c)))

        rows: List[Tuple[str, int, int, int]] = []
        for name, pred, quota in (
            ("大王", lambda c: c.rank == BIG_JOKER, 2),
            ("小王", lambda c: c.rank == SMALL_JOKER, 2),
            ("级牌" + RANK_LABELS[lv],
             lambda c: c.rank == lv and not c.is_wild(lv), 6),
            ("逢人配", lambda c: c.is_wild(lv), 2),
        ):
            m, o = stat(pred)
            rows.append((name, m, o, quota - m - o))
        return rows

    def my_voids(self) -> List[Suit]:
        """我手缺门花色（按黑红梅方顺序）。"""
        return [s for s in _SUIT_ORDER if self.mine_by_suit.get(s, 0) == 0]

    def core_residue(self) -> List[int]:
        """教学面板模块1 七列剩余（未见）张数：
        王(大小王合计) / 级(非红桃级牌) / 配(逢人配) / A / K / 10 / 5。"""
        rows = self.core_rows()
        return [self.unseen.get(SMALL_JOKER, 0) + self.unseen.get(BIG_JOKER, 0),
                rows[2][3], rows[3][3],
                self.unseen[14], self.unseen[13], self.unseen[10], self.unseen[5]]

    def bomb_candidates(self) -> List[int]:
        """未见张数 ≥4 的点数（可能仍在场上成炸），升序。"""
        return sorted(r for r, n in self.unseen.items() if n >= 4 and r < 15)


# ---------------------------------------------------------------- 已出牌数据库
# 牌型列（模块2 第3行标题顺序）：炸=炸弹+同花顺+天王炸（炸弹族）
COMBO_COLS = ["炸", "钢", "连", "顺", "32", "三", "对", "单"]
_COL_OF_TYPE = {
    "BOMB": 0, "STRAIGHT_FLUSH": 0, "ROCKET": 0,
    "TRIPLE_CHAIN": 1, "PAIR_CHAIN": 2, "STRAIGHT": 3,
    "TRIPLE_PAIR": 4, "TRIPLE": 5, "PAIR": 6, "SINGLE": 7,
}


class PlayTracker:
    """玩家已出牌数据库（教学体系，listener 挂载，仅公开信息）。

    - 已出：``play`` 事件 → played[pid][列] += 1（按 combo.type 归列）；
    - ``round_start`` 清空本局数据；attach 校验比赛起点（第1局且无已出牌）。
    """

    def __init__(self) -> None:
        self.reset()
        self._fn = None

    def reset(self) -> None:
        self.played: Dict[int, List[int]] = {p: [0] * len(COMBO_COLS)
                                             for p in range(4)}

    def attach(self, game) -> bool:
        """挂载到比赛起点；非起点（已有出牌/非第1局/已挂载）返回 False。"""
        if self._fn is not None or game.round_number != 1 or game.played_cards:
            return False
        self._fn = self._on_event
        game.listeners.append(self._fn)
        return True

    def detach(self, game) -> None:
        if self._fn is not None and self._fn in game.listeners:
            game.listeners.remove(self._fn)
        self._fn = None

    def _on_event(self, game, event: str, data: dict) -> None:
        if event == "round_start":
            self.reset()
        elif event == "play":
            col = _COL_OF_TYPE.get(data["combo"].type.name)
            if col is not None:
                self.played[data["pid"]][col] += 1

    def predict_rows(self):
        """模块3「剩牌推测」接口（预留，暂不实现）。

        预期返回 [{pid, cells: List[...]}] 形式的推测行数据或 None。
        TODO(阶段四/五)：基于 PlayTracker 已出统计 + 未见分布做
        各家剩牌构成推测（如对家可能成炸点位、大概率缺门等）。
        """
        return None
