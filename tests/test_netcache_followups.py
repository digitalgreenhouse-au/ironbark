"""The network playback cache's loose ends, tied: a song playback has left isn't kept downloading
at the front of the queue (nor anything once stopped); a copy that fails its check twice says the
song is skipped; a copy whose bytes go missing here is fetched again and the song carries on from
the same spot, not after 30 s of "Waiting for the network"; the audio callback can never wipe out
a pause for the network the decoder has just begun (no false dropout); pauses on a song are still
counted after the device was let go and taken back; an MP3's "cut short" note comes once its copy
is complete mid-song; and "one more" song stays within the budget for songs not heard yet.
No real network: FakeShare / FakeCopy / FakePreparer, fake clocks where time matters."""
from __future__ import annotations

import importlib
import socket
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from ironbark import audiofile, engine
from ironbark.details import describe_proof, source_rows
from ironbark.statustext import COPY_FAILED, COPY_SKIPPED, copy_failed_text, network_hover
from ironbark.engine import NetworkView, Player, WaitInfo, _Stall
from ironbark.library import Track
from ironbark.netcache.fetch import Changed
from ironbark.netcache.store import FAILED, QUEUED, READY, Item
from ironbark.proof import Proof

from netfakes import FakeCopy, FakePlayer, FakePreparer, FakeShare, make_manager, unc, wait_until
from test_light_mini import make_window  # noqa: F401 — (a fixture)
from test_stereo import FakePlayer as MeterPlayer, root, tick  # noqa: F401 — (root is a fixture)
from test_netcache_engine import (RATE, Card, fake_card, music, preparing_player, pump, quick_watch,  # noqa: F401
                                  session_on, silent_frames, stall_and_cut)

manager_mod = importlib.import_module("ironbark.netcache.manager")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to use the network")
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture
def managers():
    made = []
    yield made
    for m in made:
        m.shutdown()


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def idle_manager(tmp_path, player=None, share=None, **options):
    """A manager with no threads running (the test drives its plan), its list already loaded."""
    clock = options.pop("clock", Clock())
    m = make_manager(tmp_path / "NetCache", share or FakeShare(), player, clock=clock, start=False, **options)
    m._loaded.set()
    return m, clock


def key(m, path) -> str:
    return m._describe(path)[1]


# -- prepare()'s roles last only while the plan has those songs ------------------------------------------
def test_a_song_playback_has_left_isnt_kept_downloading_at_the_front(tmp_path):
    a, b = unc("a.flac"), unc("b.flac")
    player = FakePlayer([a, b])
    m, clock = idle_manager(tmp_path, player, prefs={"net_predownload": "off"})
    m.prepare(a, role="play")
    m.prepare(b, role="next")                                  # (the decoder lines it up near a's end)
    m._replan(clock.now)
    assert (m.items[key(m, a)].priority, m.items[key(m, b)].priority) == (manager_mod.CURRENT, manager_mod.NEXT)
    player.order = [Path("C:/Music/local.flac")]               # playback moved on to a song on this computer
    m._replan(clock.now)
    assert m.items[key(m, a)].priority is None and m.items[key(m, b)].priority is None
    assert not m._explicit and m._current_key is None
    player.order = [a, b]
    m.prepare(a, role="play")
    m._replan(clock.now)
    assert m.items[key(m, a)].priority == manager_mod.CURRENT
    player.order = []                                          # stopped: nothing is planned
    m._replan(clock.now)
    assert m.items[key(m, a)].priority is None and not m._explicit


def test_a_role_asked_for_while_the_plan_is_read_is_kept_until_the_next_look(tmp_path):
    c = unc("c.flac")
    player = FakePlayer([])
    m, clock = idle_manager(tmp_path, player)
    real = player.upcoming

    def upcoming(n):                                           # the player starts c just as the plan is read
        m.prepare(c, role="play")
        return real(n)

    player.upcoming = upcoming
    m._replan(clock.now)
    assert m.items[key(m, c)].priority == manager_mod.CURRENT
    player.upcoming = real
    m._replan(clock.now)                                       # (looked at again: not in the plan, let go)
    assert m.items[key(m, c)].priority is None


def test_with_no_plan_to_read_prepares_roles_stand(tmp_path):
    m, clock = idle_manager(tmp_path, player=None)
    m.prepare(unc("a.flac"), role="play")
    m._replan(clock.now)
    assert m.items[key(m, unc("a.flac"))].priority == manager_mod.CURRENT


def test_a_stopped_player_plans_nothing_and_tells_the_cache(tmp_path, fake_card):
    p, prep, copy = preparing_player(tmp_path)
    assert p.upcoming(2) == []                                 # nothing played yet
    p.play(0)
    assert [path for _, path in p.upcoming(1)][0] == copy.source and p.active()
    prep.calls.clear()
    p.stop()
    assert ("plan",) in prep.calls and p.upcoming(2) == [] and not p.active() and p.session_paths() == []


def test_paused_with_the_device_let_go_still_counts_as_playing_for_the_cache(tmp_path, fake_card):
    path, _, _, _ = music(tmp_path, "a.wav", 2.0)
    p = Player(preparer=FakePreparer())
    p.set_tracks([Track(path, "A", duration=2.0)])
    p.play(0)
    assert p.suspend()                                         # (the ABX test takes the device)
    assert p._session is None and p.active()
    assert [q for _, q in p.upcoming(1)] == [path] and p.session_paths() == [path]
    p.stop()


# -- a copy that fails its check twice: the song is skipped, and says so --------------------------------
def test_a_copy_that_failed_its_check_twice_says_the_song_is_skipped():
    item = Item(None, key="k", kind="share", source="\\\\nas\\s\\a.flac", host="nas", ext="flac", size=10)
    item.failed_gens.add(1)
    item.gen = 2
    assert item.copy_state(1) == "failed"                      # a fresh copy downloads
    item.state, item.permanent = FAILED, True
    assert item.copy_state(1) == "rejected"                    # failed again: skipped for now
    assert copy_failed_text("failed") == COPY_FAILED and COPY_FAILED.endswith("Downloading it again.")
    assert copy_failed_text("rejected") == COPY_SKIPPED and COPY_SKIPPED.endswith("The song is skipped for now.")
    assert copy_failed_text("verified") == ""
    assert network_hover(NetworkView("playing", True, copy="rejected", cached=1.0)) == COPY_SKIPPED
    track = Track(Path("//192.168.80.173/Shared/Song.flac"), "Song", size=28_300_000)
    rows = dict(source_rows(track, NetworkView("playing", True, copy="rejected", cached=1.0)))
    assert rows["Downloaded"] == COPY_SKIPPED
    proof = Proof(sent=192.0, checked=192.0)
    proof.copy = "rejected"
    text, tone = describe_proof(proof, None, 1.0, 1.0)
    assert text.startswith(COPY_SKIPPED) and tone == "warn" and "Downloading it again" not in text


def test_the_window_says_the_song_is_skipped_too(make_window):
    from ironbark.engine import OutputInfo, State, Status
    window, root, player, errors = make_window()
    track = player.track
    proof = Proof(sent=60.0, checked=60.0)
    proof.copy = "rejected"
    player.status = lambda: Status(State.PLAYING, 0, track, 30.0, track.duration,
                                   output=OutputInfo("Speakers", 44100, 48000, False), proof=proof)
    window._refresh_view()
    assert window._status_detail.endswith(COPY_SKIPPED) and "Downloading it again" not in window._status_detail
    assert errors == []


# -- a copy whose bytes went missing from this computer ---------------------------------------------------
def test_a_reader_whose_copy_was_deleted_behind_the_caches_back_stops_waiting_at_once(tmp_path):
    _, data, _, _ = music(tmp_path, "a.wav", 2.0)
    copy = FakeCopy(tmp_path / "cache", "a.wav", data)
    copy.arrive()
    told = []
    copy.item.on_lost = lambda item, gen: told.append(gen)
    with audiofile.open(copy.local()) as f:
        assert len(audiofile.read(f, 4096, "int32")) == 4096
    copy.lose_file()                                           # deleted behind the cache's back
    started = time.monotonic()
    with pytest.raises(audiofile.Waiting) as waited:
        audiofile.open(copy.local(), patience=5.0)
    assert waited.value.reason == "failed" and time.monotonic() - started < 1.0   # (not the 5 s patience)
    assert told == [copy.item.gen] and copy.item.lost_gens == {copy.item.gen}
    assert not copy.item.has(copy.item.gen, 10) and copy.item.copy_state(copy.item.gen) == "downloading"


def test_a_reader_giving_up_on_bytes_still_to_come_never_marks_the_copy_lost(tmp_path):
    _, data, _, _ = music(tmp_path, "a.wav", 2.0)
    copy = FakeCopy(tmp_path / "cache", "a.wav", data)
    copy.arrive(seconds=0.5)
    f = audiofile.open(copy.local())
    audiofile.set_patience(f, 0.05)
    with pytest.raises(audiofile.Waiting) as waited:
        audiofile.read(f, RATE, "int32")                        # past what has come: it gives up waiting
    f.close()
    assert waited.value.reason == "patience" and not copy.item.lost_gens


def _part_item(m, name: str, data: bytes) -> Item:
    item = Item(m.store, key=f"smb:{name}", kind="share", source=f"\\\\nas\\s\\{name}", host="nas", ext="wav",
                size=len(data) * 2, mtime_ns=5)
    m.store.make_folders()
    m.store.part_path(item, item.gen).write_bytes(data)
    item.visible = item.durable = len(data)
    item.state = manager_mod.PARTIAL
    m._wire(item)
    m.items[item.key] = item
    m._paths[item.key] = unc(name)
    return item


def test_the_cache_gives_a_lost_version_up_and_fetches_the_song_again(tmp_path):
    m, clock = idle_manager(tmp_path)
    item = _part_item(m, "a.wav", b"x" * 5000)
    gen = item.gen
    played = audiofile.LocalTrack(unc("a.wav"), item, gen)
    played.acquire()
    m.store.part_path(item, gen).unlink()
    item.lose(gen)                                             # (as the reader that found it gone does)
    m._handle(m._inbox.get_nowait())
    assert item.gen == gen + 1 and item.state == QUEUED and item.visible == 0
    assert item.size == 10_000 and item.mtime_ns == 5          # (the same file on the drive: only fetched again)
    outlook = m.outlook(played, 0.0)
    assert isinstance(outlook, manager_mod.NotReady) and outlook.phase == "lost"
    assert not m.resumable(played, 0, 0) and not m.resumable(played, 0, 0, quick=True)
    played.release()


def test_a_false_alarm_is_simply_cleared(tmp_path):
    m, clock = idle_manager(tmp_path)
    item = _part_item(m, "b.wav", b"y" * 5000)
    gen = item.gen
    item.lose(gen)                                             # (its file is all there after all)
    m._handle(m._inbox.get_nowait())
    assert item.gen == gen and not item.lost_gens and item.has(gen, 5000)


def test_the_writer_never_fills_a_file_cut_short_behind_its_back(tmp_path):
    m, clock = idle_manager(tmp_path)
    item = _part_item(m, "c.wav", b"z" * 5000)
    writer = manager_mod._Writer(m.store, item, item.gen, clock)
    assert writer.write(None, 5000, b"z" * 1000)
    with open(m.store.part_path(item), "r+b") as f:
        f.truncate(100)                                        # cut short while it was open
    with pytest.raises(Changed):
        writer.write(None, 6000, b"z" * 1000)
    writer.close()
    assert m.store.part_path(item).stat().st_size == 100       # (no hole of zeros made)
    assert item.gen in item.lost_gens                          # (and its readers are told)


def test_a_copy_the_download_finds_gone_is_fetched_again_not_cut_short_as_changed(tmp_path):
    m, clock = idle_manager(tmp_path)
    item = _part_item(m, "d.wav", b"w" * 5000)
    gen = item.gen
    played = audiofile.LocalTrack(unc("d.wav"), item, gen)
    played.acquire()                                           # (a song paused for the network mid-way)
    m.store.part_path(item, gen).unlink()                      # deleted while its download was idle
    with pytest.raises(Changed):
        manager_mod._Writer(m.store, item, gen, clock)          # the download carries on first, and finds it gone
    with m._lock:
        m._new_generation(item, None)                          # (as the I/O thread does then)
    outlook = m.outlook(played, 0.0)
    assert isinstance(outlook, manager_mod.NotReady) and outlook.phase == "lost"   # not "the file … changed"
    assert not m.resumable(played, 0, 0)
    m._handle(m._inbox.get_nowait())
    assert gen in item.lost_gens and item.gen == gen + 1
    played.release()


def test_a_song_whose_copy_goes_missing_carries_on_from_a_fresh_copy_at_once(tmp_path, fake_card, quick_watch,
                                                                              monkeypatch):
    monkeypatch.setattr(engine, "HOLD_RELEASE_S", 30.0)
    _, data, ref, _ = music(tmp_path, "a.wav", 4.0)
    prep = FakePreparer()
    copy = prep.add(FakeCopy(tmp_path / "cache", "a.wav", data))
    copy.arrive(seconds=2.0)
    p = Player(preparer=prep)
    p.set_volume(1.0)
    p.set_tracks([Track(copy.source, "A", duration=4.0)])
    p.play(0)
    s = p._session
    card = Card(s)
    card.play_while(lambda: s.waiting is None, keep=12)          # the decoder reached the end of what came
    copy.lose_file()                                             # … and the copy is deleted behind the cache's back
    copy.arrive()                                                # (its bytes are still recorded as here)
    started = time.monotonic()
    card.play_while(lambda: p._session is s, keep=0, pace=0.01)
    assert time.monotonic() - started < 10                       # never 30 s of "Waiting for the network"
    assert copy.item.lost_gens and fake_card[0].closed
    position = p._preparing.start_at
    status = p.status()
    assert status.network.phase == "preparing" and status.network.auto and "fresh copy" in status.transition
    copy.renew()                                                 # the cache starts a fresh version …
    copy.arrive()
    prep.tick()
    assert wait_until(lambda: p._session is not None, 3)        # … and it carries on by itself from the same spot
    second = Card(p._session)
    second.play_to_end()
    first = card.audio
    first = np.delete(first, silent_frames(first), axis=0)
    got = second.audio
    got = np.delete(got, silent_frames(got), axis=0)
    assert int(round(position * RATE)) == len(first)
    assert np.array_equal(np.concatenate([first, got]), ref)     # nothing skipped, nothing played twice
    p.stop()


# -- the callback never wipes out a wait the decoder has just begun ------------------------------------
def test_a_new_wait_begun_as_the_callback_passes_the_last_pauses_marker_survives(tmp_path, fake_card):
    _, data, ref, _ = music(tmp_path, "a.wav", 4.0)
    prep = FakePreparer()
    copy = prep.add(FakeCopy(tmp_path / "cache", "a.wav", data))
    copy.arrive()
    s = session_on(copy, prep)
    card = Card(s)
    card.ahead(4)
    later = WaitInfo(time.perf_counter(), 99_999, "patience")   # the decoder's next wait (not committed)
    raced = []

    class Racing(WaitInfo):
        """The pause the marker belongs to. The decoder replaces it with its next wait just as the
        callback looks at it (an earlier version of the callback then cleared the new one)."""

        def __getattribute__(self, name):
            if name == "serial" and sys._getframe(1).f_code.co_name == "_past_stall" and not raced:
                raced.append(True)
                s.waiting = later
            return super().__getattribute__(name)

    with s._queue.mutex:
        s._serial += 1
        old = Racing(time.perf_counter(), 0, "patience", serial=s._serial, committed=True)
        s._resumed_serial = s._serial                            # (the decoder has carried on from its cut)
        s._queue.queue.appendleft((s._generation, s._src, 0, _Stall(s._serial)))
    s.waiting = old
    s._stop.set()                                                # (the decoder stops here: the test plays its part)
    s._wake()
    time.sleep(0.1)
    card.request()                                               # the callback plays on past the marker
    if not raced:                                                # (it didn't look: the decoder's new wait lands now)
        s.waiting = later
    assert s.waiting is later
    s._drain()
    for _ in range(3):
        card.request()                                           # the queue has run dry while waiting
    assert s.dropouts == 0 and not [e for e in s._events if e.kind == "dropout"]
    s.close()


# -- pauses on a song still counted after the device was let go -----------------------------------------
def test_pauses_carry_over_when_the_device_is_let_go_and_taken_back(tmp_path, fake_card, quick_watch):
    _, data, ref, _ = music(tmp_path, "a.wav", 6.0)
    prep = FakePreparer()
    copy = prep.add(FakeCopy(tmp_path / "cache", "a.wav", data))
    copy.arrive(seconds=2.0)
    p = Player(preparer=prep)
    p.set_tracks([Track(copy.source, "A", duration=6.0)])
    p.play(0)
    s = p._session
    card = Card(s)
    stall_and_cut(card, copy)
    assert prep.calls.count(("stall", copy.source)) == 1        # the cache hears of the pause once
    pump(card, lambda: p._session is not s)                      # silent long enough: the device is let go
    copy.arrive(seconds=4.5)
    prep.tick()
    assert wait_until(lambda: p._session is not None, 3)
    resumed = p._session
    assert len(resumed._src.stalls) == 1                         # carried over to the song's new session
    prep.calls.clear()
    stall_and_cut(Card(resumed), copy)                           # a second pause
    assert wait_until(lambda: any(c[0] == "resumable" and not c[3] for c in prep.calls), 3)
    asked = {c[2] for c in prep.calls if c[0] == "resumable" and not c[3]}
    assert asked == {2}                                          # need × 2² before it carries on
    assert prep.calls.count(("stall", copy.source)) == 1
    p.stop()


# -- an MP3's "cut short" note once its copy is complete --------------------------------------------------
def test_an_mp3_cut_short_note_comes_when_its_copy_completes_mid_song(tmp_path, fake_card):
    from ironbark import mp3frames
    from test_fix_playback import write_chunked
    rng = np.random.default_rng(3)
    full = tmp_path / "vbr.mp3"
    signal = np.concatenate([rng.standard_normal((RATE * 6, 2)) * 0.3, np.zeros((RATE * 30, 2))])
    write_chunked(full, signal, RATE, format="MP3", subtype="MPEG_LAYER_III", bitrate_mode="VARIABLE",
                  compression_level=0.0)
    data = full.read_bytes()
    first = mp3frames._frame(data, 0, len(data))
    bare = data[first[2]:]                                       # no length header: libsndfile stops early
    prep = FakePreparer()
    copy = prep.add(FakeCopy(tmp_path / "cache", "bare.mp3", bare, header=0))
    copy.arrive()                                                # every byte here, but not complete and checked yet
    events = []
    s = session_on(copy, prep, events=events)
    card = Card(s)
    card.ahead(engine.QUEUE_BLOCKS - 1)
    time.sleep(0.2)
    assert "no length header" not in s.message                   # (can't be counted on a copy still downloading)
    assert s._reading.length_due
    copy.ready()                                                 # downloaded and checked, mid-song
    for _ in range(4):
        card.request()
    assert wait_until(lambda: "no length header" in s.message, 3)
    assert any(e.kind == "cut short" for e in events) and not s._reading.length_due
    s.close()


# -- "one more" stays within the budget -------------------------------------------------------------------
def test_one_more_song_is_fetched_only_within_the_budget(tmp_path, managers, monkeypatch):
    monkeypatch.setattr(manager_mod, "GB", 1_000_000)          # "1 GB" is 1 MB here: half of it for songs ahead
    monkeypatch.setattr(manager_mod, "SCHEDULER_TICK_S", 0.05)
    share = FakeShare(rate=8_000_000)
    paths = [share.add(f"{k}.bin", bytes(300_000)) for k in range(4)]
    m = make_manager(tmp_path / "NetCache", share, FakePlayer(paths), prefs={"net_predownload": "next",
                                                                            "net_cache_gb": 1})
    managers.append(m)
    assert wait_until(lambda: all(m.items.get(key(m, path)) is not None and m.items[key(m, path)].state == READY
                                  for path in paths[:2]), 20)
    time.sleep(0.5)
    read = {name for (name, op, _, _) in share.log if op == "read"}
    assert read == {"0.bin", "1.bin"}                            # (the next song and one more: 600 KB > 500 KB)


# -- VU Auto on a network album: measured again once its songs have downloaded --------------------------
def test_vu_auto_measures_a_network_album_once_its_songs_have_downloaded(root, tmp_path, monkeypatch):
    from ironbark import meter_window
    album = tmp_path / "album"
    album.mkdir()
    songs = [album / "01.flac", album / "02.flac"]
    for song in songs:
        song.write_bytes(b"")                                    # never read: the measuring is a stand-in
    here: set = set()
    monkeypatch.setattr(audiofile, "local_copy", lambda path: path if path in here else None)
    monkeypatch.setattr(meter_window, "COPIES_LOOK_S", 0.02)
    tries = []

    def alignment(track, on_first, cancel):
        tries.append(len(here))
        if songs[0] in here:
            on_first(-17.0)
        if set(songs) - here:
            raise audiofile.NotCached("this network song hasn't been downloaded yet", None, "not-cached")
        return -15.0

    monkeypatch.setattr(meter_window, "album_alignment", alignment)
    win = meter_window.MeterWindow(root, MeterPlayer(), {"vu_align": meter_window.AUTO})
    win.win.update()
    win.engine.current_path = songs[0]

    def says(words):
        def shown():
            tick(win)
            return words in win.align_text.cget("text")
        return wait_until(shown, 5, 0.03)

    assert says("Auto: measured once this album's songs have downloaded")   # not "couldn't measure"
    here.add(songs[0])                                           # the song playing has downloaded
    assert says("0 VU = -17 dBFS · Auto: from this song, until the rest of the album has downloaded")
    here.add(songs[1])                                           # … and the rest of the album
    assert says("0 VU = -15 dBFS · Auto, measured for this album (album)")
    assert tries == [0, 1, 2]                                    # once at first, once a figure, once the album
    win.close()


# -- Now Playing's live bitrate once the copy completes ---------------------------------------------------
def test_now_playings_frame_index_is_built_once_the_copy_completes_mid_song(monkeypatch):
    import threading
    from types import SimpleNamespace
    from ironbark import details
    song, other = Path("//nas/s/song.flac"), Path("//nas/s/other.flac")
    complete: set = set()
    asked = []
    monkeypatch.setattr(audiofile, "local_copy",
                        lambda path: asked.append(path) or (path if path in complete else None))
    monkeypatch.setattr(details, "INDEX_LOOK_S", 0.02)
    w = SimpleNamespace(_index_cache={}, _track_path=song, _index=None, INDEX_CACHE_SIZE=20,
                        _closed=threading.Event())
    details.NowPlayingWindow._load_index(w, song, lambda source: ("index of", source))
    assert wait_until(lambda: len(asked) >= 3, 3) and w._index is None and song not in w._index_cache
    complete.add(song)                                           # the copy is complete now, mid-song
    assert wait_until(lambda: w._index == ("index of", song), 3)
    assert w._index_cache[song] == ("index of", song)
    asked.clear()
    details.NowPlayingWindow._load_index(w, other, lambda source: ("index of", source))
    time.sleep(0.2)                                              # (another song is on show: not looked for)
    assert asked.count(other) == 1 and other not in w._index_cache
    w._closed.set()


# -- the track list's Cached column --------------------------------------------------------------------------
def test_the_track_list_marks_network_songs_when_the_tab_has_some(make_window):
    from ironbark.netcache import Progress
    from test_netcache_ui import NetStub
    window, root, player, errors = make_window(geometry="420x1100+120+120")
    local = player.track
    assert window.playlist.cache_column is False
    window._show_cache_column()
    assert window.playlist.cache_column is False                 # no cache running: no column
    songs = [Track(Path(f"//192.168.80.173/Shared/{name}.flac"), name.title(), duration=200.0)
             for name in ("ready", "half", "waiting", "away", "unknown")]
    stub = NetStub({"ready.flac": Progress("ready", 1.0, None, "verified"),
                    "half.flac": Progress("downloading", 0.72, 3e6, "downloading", host="192.168.80.173"),
                    "waiting.flac": Progress("queued", 0.0, None, "downloading", host="192.168.80.173"),
                    "away.flac": Progress("partial", 0.1, None, "downloading", host="192.168.80.173",
                                          host_state="down")})
    stub.any_remote = lambda paths: any(str(p).startswith("\\\\") for p in paths)
    window._netcache = stub
    window._show_current_tab()                                   # a tab of songs on this computer
    assert window.playlist.cache_column is False
    assert "cache" not in [str(c) for c in window.playlist.tree.cget("displaycolumns")]
    window.tabs[0].tracks = [local] + songs
    window._show_current_tab()
    root.update()
    tree = window.playlist.tree
    assert len(window.playlist.visible_tracks()) == 6
    assert [str(c) for c in tree.cget("displaycolumns")][-2:] == ["cache", "time"]
    assert tree.heading("cache", "text") == "Cached"
    assert [tree.set(str(i), "cache") for i in range(6)] == ["", "●", "◐ 72%", "○", "!", ""]
    stub.stages["half.flac"] = Progress("downloading", 0.9, 3e6, "downloading", host="192.168.80.173")
    window._paint_track_marks()                                  # (as about every second's poll does)
    assert tree.set("2", "cache") == "◐ 90%"
    window.playlist.query.set("half")                            # searching keeps the marks
    window.playlist._search_now()
    assert tree.set("2", "cache") == "◐ 90%"
    window.tabs[0].tracks = [local]
    window._show_current_tab()
    assert window.playlist.cache_column is False
    assert errors == []


def test_only_the_rows_on_screen_are_marked(make_window):
    window, root, player, errors = make_window()
    asked = []

    class Stub:
        notices: list = []
        narrowed = False

        def any_remote(self, paths):
            return True

        def progress(self, path):
            asked.append(path)
            return None

    songs = [Track(Path(f"//nas/s/{k:03}.flac"), f"Song {k}", duration=100.0) for k in range(400)]
    window._netcache = Stub()
    window.tabs[0].tracks = songs
    window._show_current_tab()
    root.update()
    asked.clear()
    window._paint_track_marks()
    on_screen = window.playlist.visible_tracks()
    assert 0 < len(on_screen) < 100 and len(asked) == len(on_screen)
    assert errors == []


def test_a_whole_librarys_paths_are_classified_by_drive_letter():
    from ironbark.netcache import sources
    from netfakes import FakeClassifier
    looked = []

    class Letters(FakeClassifier):
        def _type_of(self, letter):
            looked.append(letter)
            return sources.DRIVE_REMOTE if letter == "Z:" else 3

    c = Letters()
    assert not c.any_remote(Path(f"C:/Music/{k}.flac") for k in range(5000))
    assert looked == ["C:"]                                      # once, not 5,000 times
    assert c.any_remote([Path("C:/Music/a.flac"), Path("Z:/Music/b.flac")])
    assert c.any_remote([Path("//nas/share/c.flac")]) and not c.any_remote(["\\\\.\\C:\\x.flac"])


def test_whether_a_folder_is_inside_onedrive_is_worked_out_once_per_folder():
    from netfakes import FakeClassifier
    roots, files = [], []

    class OneDrive(FakeClassifier):
        def _under_roots(self, text):
            roots.append(text)
            return super()._under_roots(text)

        def _is_cloud(self, text):
            files.append(text)
            return text.endswith("online-only.flac")

    c = OneDrive()
    c._roots = ["c:\\users\\x\\onedrive\\"]
    songs = [Path(f"C:/Music/Album {k % 5}/{k}.flac") for k in range(5000)]
    assert not c.any_remote(songs)
    assert len(roots) == 5 and files == []                       # (no file outside OneDrive looked at)
    inside = [Path(f"C:/Users/X/OneDrive/Music/{k}.flac") for k in range(3)]
    assert not c.any_remote(songs + inside) and len(files) == 3
    assert c.any_remote(songs + inside + [Path("C:/Users/X/OneDrive/Music/online-only.flac")])
    assert not c.any_remote([Path("C:/Users/X/OneDriveOld/a.flac")])   # (only inside the folder itself)


# -- a copy gone missing: fetched again once the music queued has played out to its quiet cut ----------------
def _lose_mid_song(tmp_path, prep):
    """A player on a song whose copy has 2 s of its 4 here, read up to there; then its .part is
    deleted behind the cache's back (its bytes still recorded as here)."""
    _, data, ref, _ = music(tmp_path, "a.wav", 4.0)
    copy = prep.add(FakeCopy(tmp_path / "cache", "a.wav", data))
    copy.arrive(seconds=2.0)
    p = Player(preparer=prep)
    p.set_volume(1.0)
    p.set_tracks([Track(copy.source, "A", duration=4.0)])
    p.play(0)
    s = p._session
    card = Card(s)
    card.play_while(lambda: s.waiting is None, keep=12)
    copy.lose_file()
    copy.arrive()
    # Played on until the pause is committed, with music still queued before its marker.
    card.play_while(lambda: s._queued_seconds() > engine.STALL_COMMIT_S and not s.waiting.committed, keep=0,
                    pace=0.02)
    assert wait_until(lambda: s.waiting.committed, 3)
    return p, s, card, copy, ref


def test_a_lost_copy_lets_the_music_queued_play_out_to_its_quiet_cut_first(tmp_path, fake_card):
    prep = FakePreparer()
    p, s, card, copy, ref = _lose_mid_song(tmp_path, prep)
    cut = s.waiting.frame
    assert s._queue.qsize() > 1                                  # music still queued before the marker
    time.sleep(0.5)                                              # (the card isn't asking just now)
    assert p._session is s and not fake_card[0].closed           # never stopped part-way through the music
    card.play_while(lambda: p._session is s, keep=0, pace=0.01)
    assert fake_card[0].closed and p._preparing is not None
    position = p._preparing.start_at
    assert int(round(position * RATE)) == cut                    # let go at the quiet cut, and carries on there
    played = card.audio
    played = np.delete(played, silent_frames(played), axis=0)
    assert len(played) == cut and np.array_equal(played, ref[:cut])
    assert ("hint", copy.source, position) in prep.calls        # (the fresh copy is fetched from there)
    # Not the network: no pause counted against the server, nor making the next wait longer.
    assert ("stall", copy.source) not in prep.calls and s._src.stalls == []
    kinds = [event.kind for event in p.events]
    assert "fetched again" in kinds and "network pause" not in kinds and "dropout" not in kinds
    copy.renew()
    copy.arrive()
    prep.tick()
    assert wait_until(lambda: p._session is not None, 3)
    assert p._session._src.stalls == []
    second = Card(p._session)
    second.play_to_end()
    got = second.audio
    got = np.delete(got, silent_frames(got), axis=0)
    assert np.array_equal(np.concatenate([played, got]), ref)   # nothing skipped, nothing played twice
    p.stop()


def test_a_lost_copy_while_paused_by_hand_waits_for_play(tmp_path, fake_card):
    prep = FakePreparer()
    p, s, card, copy, ref = _lose_mid_song(tmp_path, prep)
    cut = s.waiting.frame
    p.pause()                                                    # (paused before the pause was heard)
    assert wait_until(lambda: p._session is not s, 3)
    position = p._preparing.start_at
    assert position * RATE < cut                                 # (where it was paused: before the cut)
    assert p._preparing.auto is False and p.status().state.name == "PAUSED"
    assert [event.kind for event in p.events].count("fetched again") == 1   # (in the playback report)
    assert ("stall", copy.source) not in prep.calls
    copy.renew()
    copy.arrive()
    prep.tick()
    time.sleep(0.2)
    assert p._session is None and "press play" in p.status().message
    p.play()
    assert p._session is not None and p.status().position == pytest.approx(position)
    p.stop()


# -- VU Auto's wait copes with an album folder it can't list for a moment -----------------------------------
def test_vu_autos_wait_lists_the_album_once_and_again_only_if_it_couldnt(monkeypatch, tmp_path):
    import threading
    from ironbark import meter_window
    track = tmp_path / "01.flac"
    listed, here = [], []

    def album_files(t):
        listed.append(t)
        if len(listed) < 3:
            raise OSError("the specified network name is no longer available")
        return tmp_path, [track, tmp_path / "02.flac"]

    monkeypatch.setattr(meter_window, "album_files", album_files)
    monkeypatch.setattr(meter_window, "COPIES_LOOK_S", 0.01)
    monkeypatch.setattr(audiofile, "local_copy", lambda path: path if len(here) > 5 else here.append(path))
    assert meter_window._copies_here(track, threading.Event(), every=True)   # (not given up: it waited on)
    assert len(listed) == 3                                      # listed again only while it couldn't be
    cancel = threading.Event()
    cancel.set()
    assert not meter_window._copies_here(track, cancel, every=True)


def test_a_copy_deleted_while_the_network_was_down_carries_on_through_the_real_cache(tmp_path, fake_card,
                                                                                     monkeypatch):
    """End to end through the real cache (its scheduler, I/O thread, writer and reader): the network
    goes and the song pauses at a quiet sample; its partial copy is deleted meanwhile; the network
    comes back — the song carries on from that spot on a fresh copy: nothing skipped or played twice,
    never cut short as if the file on the drive had changed."""
    monkeypatch.setattr(manager_mod, "PROBE_BACKOFF_MIN_S", 0.2)
    monkeypatch.setattr(manager_mod, "SCHEDULER_TICK_S", 0.05)
    _, data, ref, _ = music(tmp_path, "a.wav", 6.0)
    share = FakeShare(rate=200_000)                              # (a little faster than the music)
    path = share.add("a.wav", data)
    p = Player()
    p.set_volume(1.0)
    m = make_manager(tmp_path / "NetCache", share, None, prefs={"net_mode": "fast", "net_buffer": 1})
    m.player = p
    p.set_preparer(m)
    try:
        p.set_tracks([Track(path, "A", duration=6.0)])
        p.play(0)
        assert wait_until(lambda: p._session is not None, 20)
        s = p._session
        item = m.items[key(m, path)]
        card = Card(s)
        card.play_while(lambda: item.visible < len(data) // 3, keep=4, pace=0.005)
        share.transient["a.wav"] = 10 ** 6                       # the network goes
        pump(card, lambda: s.waiting is not None and s.waiting.silent_since is not None, 20)
        cut = s.waiting.frame
        part = m.store.part_path(item, item.gen)
        assert wait_until(lambda: m.hosts[item.host].state == manager_mod.DOWN, 5)

        def delete():
            try:
                part.unlink()                                    # deleted behind the cache's back
                return True
            except PermissionError:   # (the download looking in just then holds it: tried again)
                return False

        assert wait_until(delete, 5)
        share.rate = 20_000                                      # (slow for now: it shows "Preparing…" a while)
        share.transient["a.wav"] = 0                             # the network is back
        pump(card, lambda: p._session is not s, 20)
        assert "Cut short" not in (s.note or "") and p._preparing is not None
        assert int(round(p._preparing.start_at * RATE)) == cut   # let go at the cut, and carries on there
        share.rate = 400_000
        assert wait_until(lambda: p._session is not None, 20)    # … by itself, once enough of a fresh copy is here
        second = Card(p._session)
        second.play_to_end()
        first = np.delete(card.audio, silent_frames(card.audio), axis=0)
        got = np.delete(second.audio, silent_frames(second.audio), axis=0)
        assert len(first) == cut
        assert np.array_equal(np.concatenate([first, got]), ref)   # nothing skipped, nothing played twice
    finally:
        p.stop()
        m.shutdown()
