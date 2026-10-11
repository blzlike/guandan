from cards import BIG_JOKER, SMALL_JOKER, Card, Suit, build_deck, point_value, sort_key


def test_deck_108_unique():
    deck = build_deck()
    assert len(deck) == 108
    assert len({c.eid for c in deck}) == 108
    assert len({(c.suit, c.rank) for c in deck if not c.is_joker}) == 52
    assert sum(1 for c in deck if c.rank == BIG_JOKER) == 2
    assert sum(1 for c in deck if c.rank == SMALL_JOKER) == 2


def test_wild_definition():
    deck = build_deck()
    wilds = [c for c in deck if c.is_wild(10)]
    assert len(wilds) == 2
    assert all(c.suit is Suit.HEART and c.rank == 10 for c in wilds)
    assert not Card(Suit.SPADE, 10, 999).is_wild(10)


def test_point_value_order():
    level = 7
    assert point_value(BIG_JOKER, level) > point_value(SMALL_JOKER, level)
    assert point_value(SMALL_JOKER, level) > point_value(level, level)
    assert point_value(level, level) > point_value(14, level)  # 级牌 > A
    assert point_value(14, level) > point_value(13, level) > point_value(2, level)


def test_card_equality_by_eid():
    a = Card(Suit.HEART, 7, 1)
    b = Card(Suit.HEART, 7, 2)
    assert a != b
    assert a == Card(Suit.HEART, 7, 1)
