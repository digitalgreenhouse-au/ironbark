"""A playlist may hold a song twice. Changing it while the second copy plays (a song added, another
taken out, one moved) keeps the player on that copy: what comes next is what follows it, not what
follows the first copy."""

from __future__ import annotations

import random
from pathlib import Path

from ironbark.engine import Player
from ironbark.library import Track


def _songs(names):
    return [Track(Path(f"{name}.flac"), name) for name in names]


def _next(p: Player) -> str:
    return p.tracks[p._step(p.index, 1)].title


def test_a_song_added_keeps_the_copy_playing():
    a, b, c, d = _songs("ABCD")
    p = Player()
    p.set_tracks([a, b, a, c])
    p.index = 2                              # the second A
    p.set_tracks([a, b, a, c, d], keep_playing=True)
    assert p.index == 2
    assert _next(p) == "C"


def test_a_song_before_it_taken_out_keeps_the_copy_playing():
    a, b, c = _songs("ABC")
    p = Player()
    p.set_tracks([a, b, a, c])
    p.index = 2
    p.set_tracks([a, a, c], keep_playing=True)   # B out
    assert p.index == 1
    assert _next(p) == "C"


def test_the_first_copy_playing_stays_the_first():
    a, b, c = _songs("ABC")
    p = Player()
    p.set_tracks([a, b, a, c])
    p.index = 0
    p.set_tracks([a, b, a, c, c], keep_playing=True)
    assert p.index == 0
    assert _next(p) == "B"


def test_a_list_of_new_track_objects_still_finds_the_song_by_path():
    a, b, c = _songs("ABC")
    p = Player()
    p.set_tracks([a, b, c])
    p.index = 1
    p.set_tracks(_songs("xABC"), keep_playing=True)   # read again: equal songs, other objects
    assert p.index == 2
    assert _next(p) == "C"


def test_shuffle_keeps_its_order_round_the_second_copy():
    random.seed(3)
    songs = _songs(f"s{n:02d}" for n in range(30))
    twice = songs[:10] + [songs[0]] + songs[10:]   # s00 at 0 and at 10
    p = Player()
    p.set_tracks(twice)
    p.set_shuffle(True)
    p.index = 10
    p._rebuild_order(first=10)
    ahead = [p.tracks[i].title for i in p.order_around(p.index, 0, 6)]
    p.set_tracks(twice + _songs(["extra"]), keep_playing=True)
    assert p.index == 10
    assert [p.tracks[i].title for i in p.order_around(p.index, 0, 6)] == ahead
