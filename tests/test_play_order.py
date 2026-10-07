from pathlib import Path

from ironbark.engine import Player
from ironbark.library import Track


def player_with(n):
    p = Player()
    p.set_tracks([Track(Path(f"{i}.flac"), str(i)) for i in range(n)])
    return p


def test_straight_order_stops_at_the_end():
    p = player_with(3)
    assert [p._step(0, 1), p._step(1, 1), p._step(2, 1)] == [1, 2, None]
    assert p._step(0, -1) is None


def test_repeat_all_wraps_both_ways():
    p = player_with(3)
    p.set_repeat("all")
    assert p._step(2, 1) == 0
    assert p._step(0, -1) == 2


def test_shuffle_plays_every_track_once_starting_from_the_current_one():
    p = player_with(20)
    p.index = 7
    p.shuffle = True
    p._rebuild_order(first=7)
    order, i = [7], 7
    while (i := p._step(i, 1)) is not None:
        order.append(i)
    assert sorted(order) == list(range(20)) and order[0] == 7
    assert order != list(range(7, 20)) + list(range(7))  # actually shuffled (1 in 20! odds of a false fail)


def test_shuffle_off_restores_list_order():
    p = player_with(5)
    p.set_shuffle(True)
    p.set_shuffle(False)
    assert list(p._order) == [0, 1, 2, 3, 4]


def test_bad_repeat_mode_is_ignored():
    p = player_with(2)
    p.set_repeat("sideways")
    assert p.repeat == "off"
