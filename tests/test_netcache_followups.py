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
from ironbark.details import (COPY_FAILED, COPY_SKIPPED, copy_failed_text, describe_proof, network_hover,
                              source_rows)
from ironbark.engine import NetworkView, Player, WaitInfo, _Stall
from ironbark.library import Track
from ironbark.netcache.fetch import Changed
from ironbark.netcache.store import FAILED, QUEUED, READY, Item
from ironbark.proof import Proof

from netfakes import FakeCopy, FakePlayer, FakePreparer, FakeShare, make_manager, unc, wait_until
from test_light_mini import make_window  # noqa: F401 — (a fixture)
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
    monkeypatch.setattr(manager_mod, "GIB", 1_000_000)          # "1 GB" is 1 MB here: half of it for songs ahead
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
