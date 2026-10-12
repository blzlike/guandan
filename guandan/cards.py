"""扑克牌与实体牌编号定义。

- 两副标准扑克，共 108 张（含 4 张王：大王 x2、小王 x2）。
- 每张实体牌拥有全局唯一编号 ``eid``（0..107），作为"真实手牌唯一数据源"的标识。
- 点数 ``rank`` 采用统一数值编码：2..10 原值，J=11, Q=12, K=13, A=14, 小王=15, 大王=16。
"""
from __future__ import annotations

from enum import IntEnum

SMALL_JOKER = 15
BIG_JOKER = 16


class Suit(IntEnum):
    """花色。JOKER 用于大小王。"""

    SPADE = 0     # 黑桃 ♠
    HEART = 1     # 红桃 ♥
    CLUB = 2      # 梅花 ♣
    DIAMOND = 3   # 方块 ♦
    JOKER = 4     # 王

    @property
    def symbol(self) -> str:
        return {
            Suit.SPADE: "\u2660",
            Suit.HEART: "\u2665",
            Suit.CLUB: "\u2663",
            Suit.DIAMOND: "\u2666",
            Suit.JOKER: "\u2605",
        }[self]

    @property
    def cn_name(self) -> str:
        return {
            Suit.SPADE: "黑桃",
            Suit.HEART: "红桃",
            Suit.CLUB: "梅花",
            Suit.DIAMOND: "方块",
            Suit.JOKER: "王",
        }[self]

    @property
    def is_red(self) -> bool:
        return self in (Suit.HEART, Suit.DIAMOND)


RANK_LABELS = {
    2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7", 8: "8", 9: "9", 10: "10",
    11: "J", 12: "Q", 13: "K", 14: "A",
    SMALL_JOKER: "小王", BIG_JOKER: "大王",
}


class Card:
    """一张实体扑克牌。相等性以唯一实体编号 ``eid`` 判定。"""

    __slots__ = ("suit", "rank", "eid")

    def __init__(self, suit: Suit, rank: int, eid: int) -> None:
        self.suit = suit
        self.rank = rank
        self.eid = eid

    @property
    def is_joker(self) -> bool:
        return self.suit is Suit.JOKER

    def is_wild(self, level_rank: int) -> bool:
        """逢人配：红桃级牌可替代除大小王外的任意点数。"""
        return self.suit is Suit.HEART and self.rank == level_rank

    def label(self) -> str:
        """牌面文字，如 ``♥7`` 或 ``大王``。"""
        if self.is_joker:
            return RANK_LABELS[self.rank]
        return f"{self.suit.symbol}{RANK_LABELS[self.rank]}"

    def __hash__(self) -> int:
        return self.eid

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Card) and other.eid == self.eid

    def __repr__(self) -> str:
        return f"Card({self.label()}, eid={self.eid})"


def build_deck() -> list[Card]:
    """构造两副标准扑克共 108 张实体牌，eid 依次为 0..107。"""
    cards: list[Card] = []
    eid = 0
    for _ in range(2):  # 两副牌
        for suit in (Suit.SPADE, Suit.HEART, Suit.CLUB, Suit.DIAMOND):
            for rank in range(2, 15):  # 2..A
                cards.append(Card(suit, rank, eid))
                eid += 1
        cards.append(Card(Suit.JOKER, SMALL_JOKER, eid))
        eid += 1
        cards.append(Card(Suit.JOKER, BIG_JOKER, eid))
        eid += 1
    return cards


def point_value(rank: int, level_rank: int) -> int:
    """单牌比较点值：大王 > 小王 > 级牌 > A > K > ... > 2。

    序列类牌型（顺子/连对/钢板/同花顺）中的级牌按其原始点数参与，不使用此映射。
    """
    if rank == BIG_JOKER:
        return 17
    if rank == SMALL_JOKER:
        return 16
    if rank == level_rank:
        return 15
    return rank


def sort_key(card: Card, level_rank: int) -> tuple:
    """手牌排序键：按比较点值降序，同点值按花色、eid 稳定排序。"""
    return (-point_value(card.rank, level_rank), int(card.suit), card.eid)
