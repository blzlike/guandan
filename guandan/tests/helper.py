"""测试辅助：按 (suit, rank) 构造实体牌，eid 自动分配且互不冲突。"""
from cards import BIG_JOKER, SMALL_JOKER, Card, Suit

_counter = [1000]


def c(suit: Suit, rank: int) -> Card:
    _counter[0] += 1
    return Card(suit, rank, _counter[0])


def spade(r): return c(Suit.SPADE, r)
def heart(r): return c(Suit.HEART, r)
def club(r): return c(Suit.CLUB, r)
def diamond(r): return c(Suit.DIAMOND, r)
def big_joker(): return c(Suit.JOKER, BIG_JOKER)
def small_joker(): return c(Suit.JOKER, SMALL_JOKER)
