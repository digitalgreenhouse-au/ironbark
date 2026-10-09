"""Performance of big lists at 200,000 songs (marked `perf`: runs only with --perf, e.g.
`uv run pytest -q -p no:cacheprovider --perf -m perf -s tests/test_big_lists_perf.py`). A synthetic
library of 200,000 songs in a library database (tests/synthlib.py), then:

- "Open all albums in a tab" as a LibraryTrackList: how long it takes and what it holds (ids, not
  Tracks), against the old list of every Track;
- the track list (PlaylistView on VirtualTree) showing it: opening, a step of scrolling, a search
  keystroke (the database's search off Tk's thread, then Tk's share), and the same for a plain list of
  200,000 Tracks (searched on the list's own thread);
- the engine playing it: Next through the list with the window round the playing song held — the
  engine never waiting for the database.

Timings are printed with -s. Run before each release, and on the packaged build too."""
from __future__ import annotations

import gc
import statistics
import time
import tracemalloc
from pathlib import Path

import pytest
import synthlib

from ironbark import library_tracks, playlist_view, searchfold, theme
from ironbark.engine import Player
from ironbark.library_db import LibraryDB

pytestmark = pytest.mark.perf

SIZE = 200_000
ROOT = Path("C:/Music")
KEYSTROKES = ["lo", "lov", "love", "love n", "love ni", "ight", "beet", "1997", "zzzz"]


def _rss_mb() -> float:
    """This process's resident memory (Windows; 0 elsewhere)."""
    try:
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        kernel32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return 0.0
        return counters.WorkingSetSize / 2 ** 20
    except (AttributeError, OSError):
        return 0.0


@pytest.fixture(scope="module")
def big(tmp_path_factory):
    folder = tmp_path_factory.mktemp("bigperf")
    started = time.perf_counter()
    files = synthlib.make_library(SIZE, ROOT, seed=200)
    with LibraryDB.open(folder / "library.db", [ROOT]) as db:
        synthlib.load(db, ROOT, files)
        print(f"\nsynthetic library of {db.counts()['tracks']:,} songs ({db.counts()['albums']:,} albums) "
              f"stored in {time.perf_counter() - started:.0f} s")
    del files
    gc.collect()
    return folder / "library.db"


@pytest.fixture
def root():
    tk = pytest.importorskip("tkinter")
    try:
        r = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"no display: {error}")
    r.geometry("700x900+20+20")
    theme.apply(r)
    r.update()
    yield r
    r.destroy()
    del r
    gc.collect()


def _wait(root, until, seconds: float = 60.0) -> bool:
    deadline = time.monotonic() + seconds
    while not until() and time.monotonic() < deadline:
        root.update()
        time.sleep(0.005)
    return until()


def test_a_whole_library_tab_opens_in_a_second_and_holds_ids_not_tracks(big):
    with LibraryDB.open(big) as db:
        albums = db.albums()
        before = _rss_mb()
        started = time.perf_counter()
        tracks = library_tracks.build(db, albums)
        took = time.perf_counter() - started
        del tracks
        gc.collect()
        tracemalloc.start()
        tracks = library_tracks.build(db, albums)   # (again, to count what it holds: tracemalloc slows it)
        held = tracemalloc.get_traced_memory()[0]
        tracemalloc.stop()
        print(f"200k tab: built in {took * 1000:.0f} ms, holding {held / 2 ** 20:.1f} MB "
              f"(RSS {before:.0f} MB, then {_rss_mb():.0f} MB), {len(tracks):,} songs in {len(tracks.groups):,} albums")

        assert len(tracks) >= SIZE * 0.95
        assert took < 3.0
        assert held < 40 * 2 ** 20      # ids and album runs only (the old list of Tracks: ~0.6 GB)


def test_the_track_list_opens_scrolls_and_searches_a_200k_tab(big, root):
    with LibraryDB.open(big) as db:
        tracks = library_tracks.build(db, db.albums())
        view = playlist_view.PlaylistView(root, on_play=lambda i: None)
        root.update()
        started = time.perf_counter()
        view.set_tracks(tracks)
        root.update()
        opened = time.perf_counter() - started
        assert _wait(root, lambda: not tracks.waiting())
        page = view.list.page()
        steps = []
        for top in range(0, 40 * page, page):            # a page at a time, rows read as they show
            started = time.perf_counter()
            view.list.scroll_to(top)
            steps.append(time.perf_counter() - started)
            _wait(root, lambda: not tracks.waiting(), 5)
        jumps = []
        for fraction in (0.1, 0.5, 0.9, 0.3, 0.7):          # the scrollbar dragged far
            started = time.perf_counter()
            view.list._scrolled("moveto", str(fraction))
            jumps.append(time.perf_counter() - started)
        keystrokes, waits = [], []
        real_take = view._take_search
        listed = {}

        def take(*answer):
            started = time.perf_counter()
            real_take(*answer)
            listed["at"], listed["took"] = time.perf_counter(), time.perf_counter() - started

        view._take_search = take
        for typed in KEYSTROKES:
            view.query.set(typed)
            listed.clear()
            started = time.perf_counter()
            view._search_now()
            asked = time.perf_counter() - started
            assert _wait(root, lambda: "at" in listed)
            waits.append(listed["at"] - started)          # typed (after the pause) → listed
            keystrokes.append(asked + listed["took"])      # Tk's share: asking, then listing what came

        print(f"200k tab in the track list: open {opened * 1000:.0f} ms; scroll step median "
              f"{statistics.median(steps) * 1000:.2f} ms (worst {max(steps) * 1000:.1f}); scrollbar jump worst "
              f"{max(jumps) * 1000:.1f} ms; search keystroke on Tk's thread worst {max(keystrokes) * 1000:.1f} ms, "
              f"answer from the database median {statistics.median(waits) * 1000:.0f} ms "
              f"(worst {max(waits) * 1000:.0f}); rows held {len(view.tree.get_children())}; RSS {_rss_mb():.0f} MB")
        assert opened < 1.0
        assert statistics.median(steps) < 0.010 and max(jumps) < 0.050
        assert max(keystrokes) < 0.100           # Tk never waits for the search itself
        assert statistics.median(waits) < 0.5
        assert len(view.tree.get_children()) <= page
        view.tree.destroy()
        view._worker.join(5)


def test_a_plain_200k_list_is_searched_off_tk(root):
    tracks = [synthlib_track(i) for i in range(SIZE)]
    view = playlist_view.PlaylistView(root, on_play=lambda i: None)
    started = time.perf_counter()
    view.set_tracks(tracks)
    root.update()
    opened = time.perf_counter() - started
    keystrokes, waits = [], []
    for typed in KEYSTROKES:
        view.query.set(typed)
        started = time.perf_counter()
        view._search_now()
        keystrokes.append(time.perf_counter() - started)
        started = time.perf_counter()
        assert _wait(root, lambda: view._asked["search"] is None)
        waits.append(time.perf_counter() - started)
    print(f"plain 200k list: open {opened * 1000:.0f} ms; keystroke on Tk's thread worst "
          f"{max(keystrokes) * 1000:.1f} ms; search on the list's thread median "
          f"{statistics.median(waits) * 1000:.0f} ms (the first folds every song: {waits[0] * 1000:.0f} ms)")
    assert opened < 2.0 and max(keystrokes) < 0.050
    view.tree.destroy()
    view._worker.join(5)


def synthlib_track(i: int):
    from ironbark.library import Track

    album = i // 11
    return Track(Path(f"C:/Music/Artist {album % 997}/Album {album}/{i % 11:02d}.flac"),
                 f"{searchfold.fold(synthlib.WORDS[i % len(synthlib.WORDS)])} song {i}",
                 artist=f"Artist {album % 997}", album=f"Album {album}", track_no=i % 11 + 1, duration=200.0)


def test_next_through_a_200k_tab_never_waits_for_the_database(big):
    with LibraryDB.open(big) as db:
        tracks = library_tracks.build(db, db.albums())
        player = Player()
        player.set_tracks(tracks)
        index = len(tracks) // 2
        tracks.ready(index, player.order_around(index))
        player.index = index
        _settle(tracks)
        before = tracks.misses
        steps = []
        for shuffle in (False, True):
            if shuffle:
                player.shuffle = True
                player._rebuild_order(first=player.index)
                tracks.hold(player.order_around(player.index))
                _settle(tracks)
            for _ in range(100):
                started = time.perf_counter()
                following = player._next_after(player.index)
                track = player._track_at(following)
                steps.append(time.perf_counter() - started)
                assert track is not None and player._find(tracks, track.path) == following
                player.index = following
                tracks.hold(player.order_around(player.index))   # (the window's poll, every 250 ms)
                _settle(tracks)
        print(f"Next through 200k (in order, then shuffled): median {statistics.median(steps) * 1000:.2f} ms, "
              f"worst {max(steps) * 1000:.2f} ms, reads made for the engine: {tracks.misses - before}")
        assert tracks.misses == before
        assert statistics.median(steps) < 0.005


def _settle(tracks, seconds: float = 10.0):
    deadline = time.monotonic() + seconds
    while tracks.waiting() and time.monotonic() < deadline:
        time.sleep(0.002)
