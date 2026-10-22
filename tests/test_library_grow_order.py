"""While the Library tab fills in (a first look, a Rebuild) it hands the player a bigger list every few
seconds. The player keeps its place: with a song from Up next playing, the Library carries on after
the song it interrupted (not from its first song); with shuffle on, what's lined up next and what
Previous goes back to stay as they were (not a new shuffle every few seconds)."""

from __future__ import annotations

import random
import threading
from pathlib import Path

from test_library_tab import _flac, _wait
from test_light_mini import _pump, make_window  # noqa: F401  (make_window is a fixture)

from ironbark import ui
from ironbark.engine import Player, Queued
from ironbark.library import Track


def _songs(names):
    return [Track(Path(f"{name}.flac"), name) for name in names]


def _around(p: Player, before: int, after: int) -> list[str]:
    return [p.tracks[i].title for i in p.order_around(p.index, before, after)]


def test_shuffle_keeps_its_order_round_the_song_when_the_list_only_grows():
    random.seed(7)
    old = _songs(f"s{n:03d}" for n in range(60))
    p = Player()
    p.set_tracks(old)
    p.set_shuffle(True)
    for _ in range(5):                       # five songs into the shuffle
        p.index = p._step(p.index, 1)
    before = _around(p, 5, 12)
    grown = old[:20] + _songs(f"new{n}" for n in range(30)) + old[20:]   # (new albums land in the middle)
    for _ in range(3):                       # the Library tab taking in more, three times
        p.set_tracks(list(grown), keep_playing=True)
        assert p.tracks[p.index].title == before[5]
        assert _around(p, 5, 12) == before   # next up and Previous unchanged
    assert sorted(p.tracks[i].title for i in p._order) == sorted(t.title for t in grown)   # every song once


def test_without_shuffle_the_order_is_the_lists_own():
    p = Player()
    p.set_tracks(_songs("abc"))
    p.index = 1
    p.set_tracks(_songs("xabcy"), keep_playing=True)
    assert p.index == 2 and list(p._order) == [0, 1, 2, 3, 4]


def _first_look_of_blue(window, tmp_path, monkeypatch, songs_each: int = 1):
    monkeypatch.setattr(ui, "GROW_EVERY_S", 0.3)
    music = tmp_path / "Music"
    blue = [_flac(music / "Blue" / f"{n:02d} River {n}.flac", title=f"River {n}", artist="Joni", album="Blue",
                  tracknumber=str(n)) for n in range(1, songs_each + 1)]
    for name in ("Court", "Hejira", "Red"):
        for n in range(1, songs_each + 1):
            _flac(music / name / f"{n:02d} {name} {n}.flac", title=f"{name} {n}", artist="Someone", album=name,
                  tracknumber=str(n))
    index = window._index()
    index.indexer.check(index.roots, folders=[blue[0].parent])   # (part of a first look: only Blue read yet)
    main = window._main_tab(make=True)
    window.select_tab(main)
    root = window.root
    tab = window.tabs[main]
    assert _wait(root, lambda: tab.tracks is not None and len(tab.tracks) == songs_each, 10) and tab.growing
    return index, main, tab


def _rest_of_the_look(window, index, tab, total):
    threading.Thread(target=index.refresh, daemon=True).start()
    done = _wait(window.root, lambda: tab.tracks is not None and len(tab.tracks) == total and not tab.growing, 30)
    assert done, (len(tab.tracks or []), tab.growing, index.built, [t.title for t in tab.tracks or []], window._notice)


def test_with_a_queued_song_playing_the_library_carries_on_after_the_song_it_interrupted(make_window, tmp_path,
                                                                                         monkeypatch):
    window, root, player, errors = make_window()
    index, main, tab = _first_look_of_blue(window, tmp_path, monkeypatch)
    player.set_tracks(tab.tracks)                 # River plays from the Library tab…
    player.index = 0
    window.playing_tab = main
    queued = Track(tmp_path / "Elsewhere" / "Queued.flac", "Queued")
    player.queued_now, player.track = Queued(queued), queued   # …and a song from Up next interrupts it
    window.select_tab(0)                          # (another tab on show: the Library tab's rows aren't read)
    _rest_of_the_look(window, index, tab, 5)     # (and the window's own Album/01 Song)
    assert player.tracks is tab.tracks
    assert player.index >= 0 and player.tracks[player.index].title == "River 1"   # (was -1: Library from the top)
    assert errors == []


def test_shuffling_the_library_tab_keeps_its_order_while_it_fills_in(make_window, tmp_path, monkeypatch):
    random.seed(11)
    window, root, player, errors = make_window()
    index, main, tab = _first_look_of_blue(window, tmp_path, monkeypatch, songs_each=8)
    player.set_tracks(tab.tracks)
    player.set_shuffle(True)
    player.index = player._order[0]
    window.playing_tab = main
    player.track = tab.tracks[player.index]
    for _ in range(2):
        player.index = player._step(player.index, 1)
    player.track = tab.tracks[player.index]
    window._hold_playing()
    before = _around(player, 2, 5)
    _rest_of_the_look(window, index, tab, 33)
    assert player.tracks is tab.tracks and len(player._order) == 33
    assert _around(player, 2, 5) == before        # the same song playing, next up and Previous unchanged
    assert errors == []
