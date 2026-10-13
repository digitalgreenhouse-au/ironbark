"""The player with the network playback cache, sample for sample through a stand-in sound card:
songs that wait to start ("Preparing…"), a short hiccup ridden out unheard, a pause for the
network that stops at a quiet sample and carries on from exactly there, the device let go and
taken back, seeks past what has downloaded, gapless joins into a song still downloading, a source
that goes away mid-song, the proof against the cached copy, and the 0.31.0 features (stop after,
sleep timer, levelling, lost devices) meeting all of that. No real network: the copies are
FakeCopy (every byte on disk, readers see what the test lets them) and the cache is FakePreparer,
except where the real download manager runs against a FakeShare."""
from __future__ import annotations

import builtins
import io
import os
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import sounddevice as sd
import soundfile as sf
from netfakes import FakeCopy, FakePreparer, FakeShare, make_manager, manager_mod, wait_until

from ironbark import audiofile, engine, session
from ironbark.engine import BLOCK_FRAMES, Player, State, _Session
from ironbark.library import Track
from ironbark.netcache import policy
from ironbark.netcache.store import VERIFYING

RATE = 44100
TIME_INFO = SimpleNamespace(outputBufferDacTime=0.0, currentTime=0.0)
OK = SimpleNamespace(output_underflow=False)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to use the network")
    monkeypatch.setattr(socket.socket, "connect", refuse)


class FakeStream:
    made: list = []

    def __init__(self, **kwargs):
        self.callback = kwargs.get("callback")
        self.latency, self.device = 0.1, 0
        self.closed = False
        FakeStream.made.append(self)

    def start(self):
        pass

    stop = abort = start

    def close(self, ignore_errors=False):
        self.closed = True


@pytest.fixture
def fake_card(monkeypatch):
    FakeStream.made = []
    monkeypatch.setattr(engine.sd, "OutputStream", FakeStream)
    monkeypatch.setattr(engine.devices, "find_device", lambda name: None)
    monkeypatch.setattr(engine.devices, "mixer_rate", lambda index: None)
    return FakeStream.made


@pytest.fixture
def bit_perfect(monkeypatch, fake_card):
    """Exclusive mode on the stand-in card: int32 samples, nothing scaled at full volume."""
    def open_stream(self, index, want_exclusive):
        return FakeStream(callback=self._callback), bool(want_exclusive)
    monkeypatch.setattr(engine._Session, "_open_stream", open_stream)


def music(tmp_path: Path, name: str, seconds: float, seed: int = 1):
    """A WAV whose every frame has sound in it (so silence can only come from a pause), its bytes,
    and what it decodes to (float32 and int32)."""
    rng = np.random.default_rng(seed)
    n = int(RATE * seconds)
    tmp_path.mkdir(parents=True, exist_ok=True)
    x = (rng.integers(100, 20000, (n, 2)) * rng.choice([-1, 1], (n, 2))).astype(np.int16)
    path = tmp_path / name
    sf.write(path, x, RATE, subtype="PCM_16")
    with audiofile.open(path) as f:
        floats = audiofile.read(f, n, "float32")
    with audiofile.open(path) as f:
        ints = audiofile.read(f, n, "int32")
    return path, path.read_bytes(), floats, ints


class Card:
    """Asks a session for blocks the way the sound card does."""

    def __init__(self, session: _Session):
        self.s = session
        self.out: list[np.ndarray] = []
        self.ended = False

    def request(self) -> np.ndarray:
        block = np.zeros((BLOCK_FRAMES, self.s.channels), self.s.dtype)
        try:
            self.s._callback(block, BLOCK_FRAMES, TIME_INFO, OK)
        except sd.CallbackStop:
            if not self.ended:
                self.ended = True
                self.s._finished()   # (as PortAudio does once the stream has stopped)
        self.out.append(block)
        return block

    def ahead(self, keep: int, timeout: float = 3.0) -> None:
        """Wait (a while) for the decoder to have `keep` blocks queued, unless it can't."""
        s, end = self.s, time.monotonic() + timeout
        while time.monotonic() < end:
            if (s._seek_request is None and s._queue.qsize() >= keep) or s._draining or s.waiting is not None:
                return
            time.sleep(0.002)

    def play_while(self, condition, keep: int = 2, limit: int = 2000, pace: float = 0.0) -> None:
        for _ in range(limit):
            self.ahead(keep)
            if not condition() or self.ended:
                return
            self.request()
            if pace:
                time.sleep(pace)   # (about as often as a real card asks, so the decoder can look in time)
        raise AssertionError("played too long")

    def play_to_end(self, seconds: float = 20.0) -> None:
        s, give_up = self.s, time.monotonic() + seconds
        while time.monotonic() < give_up:
            if self.ended:
                return
            end = time.monotonic() + 3
            while time.monotonic() < end and not (s._draining or s._queue.qsize() >= 2):
                if s.waiting is not None and (s.waiting.silent_since is not None or s._queue.empty()):
                    time.sleep(0.005)   # (paused for the network: the card still asks, and gets silence)
                    break
                time.sleep(0.002)
            self.request()
        raise AssertionError("never reached the end")

    @property
    def audio(self) -> np.ndarray:
        return np.concatenate(self.out) if self.out else np.zeros((0, 2))


def silent_frames(audio: np.ndarray) -> np.ndarray:
    return np.flatnonzero(~audio.any(axis=1))


def session_on(copy_or_path, preparer, **kwargs) -> _Session:
    local = copy_or_path.local() if isinstance(copy_or_path, FakeCopy) else copy_or_path
    s = _Session(local, lambda path: 1.0, lambda s: None, preparer=preparer, **kwargs)
    s.start()
    return s


def stall_and_cut(card: Card, copy: FakeCopy):
    """Play until the decoder catches up with the download (music still queued), then on until
    the pause is heard. Returns (frame where the reads gave up, the cut)."""
    s = card.s
    card.play_while(lambda: s.waiting is None, keep=12)
    assert s.waiting is not None and not s.waiting.committed
    edge = s.waiting.frame
    card.play_while(lambda: not s.waiting.committed, keep=0, pace=0.02)
    card.play_while(lambda: s.waiting.silent_since is None, keep=0)
    assert s.waiting.committed
    return edge, s.waiting.frame


# -- a short hiccup ------------------------------------------------------------------------------
def test_a_short_hiccup_is_ridden_out_from_the_buffer_unheard(tmp_path, fake_card):
    _, data, ref, _ = music(tmp_path, "a.wav", 4.0)
    prep = FakePreparer()
    copy = prep.add(FakeCopy(tmp_path / "cache", "a.wav", data))
    copy.arrive(seconds=3.0)
    s = session_on(copy, prep)
    card = Card(s)
    card.play_while(lambda: s.waiting is None, keep=12)
    assert not s.waiting.committed and s._queued_seconds() > engine.STALL_COMMIT_S
    copy.arrive()                                   # the rest comes in while music is still queued
    assert wait_until(lambda: s.waiting is None, 1)     # (within a block or two at the card's real pace)
    card.ahead(20)
    card.play_to_end()
    got = card.audio
    assert np.array_equal(got[: len(ref)], ref) and not got[len(ref):].any()
    assert s.dropouts == 0 and s.health.near_misses == 0
    assert not [e for e in s._events if e.kind in ("network pause", "dropout", "near miss")]
    s.close()


# -- a committed pause, in shared and bit-perfect modes --------------------------------------------
@pytest.mark.parametrize("mode", ["shared", "bit-perfect"])
def test_a_pause_for_the_network_stops_at_a_quiet_sample_and_carries_on_exactly(tmp_path, fake_card, mode,
                                                                                 monkeypatch):
    if mode == "bit-perfect":
        monkeypatch.setattr(engine._Session, "_open_stream",
                            lambda self, index, ex: (FakeStream(callback=self._callback), True))
    _, data, floats, ints = music(tmp_path, "a.wav", 4.0)
    ref = ints if mode == "bit-perfect" else floats
    prep = FakePreparer()
    copy = prep.add(FakeCopy(tmp_path / "cache", "a.wav", data))
    copy.arrive(seconds=2.5)
    s = session_on(copy, prep, want_exclusive=mode == "bit-perfect")
    assert s.dtype == ("int32" if mode == "bit-perfect" else "float32")
    card = Card(s)
    edge, cut = stall_and_cut(card, copy)
    assert edge - BLOCK_FRAMES < cut <= edge                  # within the last block queued
    window = ref[cut - int(RATE * policy.QUIET_WINDOW_S): cut].astype(np.float64)
    assert np.abs(ref[cut - 1].astype(np.float64)).max() == np.abs(window).max(axis=1).min()   # the quietest
    for _ in range(20):
        card.request()                                        # the card keeps asking: exact zeros
    assert s.position == pytest.approx(cut / RATE) and s.levels == (0.0, 0.0)
    assert s.waiting_for(time.perf_counter()) > 0 and not s.stalled(time.perf_counter())
    sent_before = s._src.sent.summary().sent
    copy.arrive()
    card.play_to_end()
    got = card.audio
    assert np.array_equal(got[:cut], ref[:cut])
    zeros = silent_frames(got[cut:])
    gap = int(zeros[np.flatnonzero(np.diff(zeros) > 1)[0]] + 1) if (np.diff(zeros) > 1).any() else len(zeros)
    assert gap >= 20 * BLOCK_FRAMES and not got[cut: cut + gap].any()
    assert np.array_equal(got[cut + gap: cut + gap + len(ref) - cut], ref[cut:])   # carried on at the cut
    assert not got[cut + gap + len(ref) - cut:].any()
    assert sent_before == pytest.approx(cut / RATE)           # the silence was never fingerprinted
    assert s.dropouts == 0 and s.health.near_misses == 0
    assert [e.kind for e in s._events] == ["network pause"] and s._events[0].silent > 0
    with audiofile.open(copy.local()) as reference:
        s._src.sent.check(reference)
    proof = s._src.sent.summary()
    assert proof.complete and proof.untouched and proof.sent == pytest.approx(len(ref) / RATE)
    s.close()


# -- the music is back before the pause is reached ---------------------------------------------------
def test_recovering_before_the_marker_is_reached_is_seamless(tmp_path, fake_card):
    _, data, ref, _ = music(tmp_path, "a.wav", 4.0)
    prep = FakePreparer()
    copy = prep.add(FakeCopy(tmp_path / "cache", "a.wav", data))
    copy.arrive(seconds=2.5)
    s = session_on(copy, prep)
    card = Card(s)
    card.play_while(lambda: s.waiting is None, keep=12)
    for _ in range(200):                                       # down to the commit, at about a real card's pace
        if s.waiting.committed:
            break
        card.request()
        time.sleep(0.04)
    assert s.waiting.committed and s.waiting.silent_since is None
    serial = s.waiting.serial
    copy.arrive()
    assert wait_until(lambda: s._resumed_serial >= serial, 3)
    card.play_to_end()
    got = card.audio
    assert np.array_equal(got[: len(ref)], ref) and not got[len(ref):].any()
    assert not [e for e in s._events if e.kind == "network pause"] and s.waiting is None
    s.close()


# -- seeks -----------------------------------------------------------------------------------------
def test_a_seek_past_the_download_fetches_then_plays_exactly_from_there(tmp_path, fake_card):
    _, data, ref, _ = music(tmp_path, "a.wav", 4.0)
    prep = FakePreparer()
    copy = prep.add(FakeCopy(tmp_path / "cache", "a.wav", data))
    copy.arrive(seconds=1.5)
    s = session_on(copy, prep)
    card = Card(s)
    card.ahead(12)
    s.seek(3.0)
    assert wait_until(lambda: s.network_wait is not None, 3)
    assert s.network_wait.seek and s.network_wait.frame == 3 * RATE
    asked = []
    real = prep.resumable
    prep.resumable = lambda local, frame, stalls, quick=False: asked.append(stalls) or real(local, frame, stalls, quick)
    assert wait_until(lambda: asked, 3)
    copy.arrive()
    card.play_to_end()
    got = card.audio
    start = int(np.flatnonzero(got.any(axis=1))[0])
    assert np.array_equal(got[start: start + len(ref) - 3 * RATE], ref[3 * RATE:])
    assert set(asked) == {0}            # a seek ahead isn't a pause for the network: no longer wait next time
    s.close()


def test_a_seek_or_stop_during_a_wait_is_taken_at_once(tmp_path, fake_card):
    _, data, ref, _ = music(tmp_path, "a.wav", 4.0)
    prep = FakePreparer()
    copy = prep.add(FakeCopy(tmp_path / "cache", "a.wav", data))
    copy.arrive(seconds=1.0)
    s = session_on(copy, prep)
    card = Card(s)
    card.ahead(2)
    s.seek(3.0)                                                # nothing there: waiting
    assert wait_until(lambda: s.network_wait is not None, 3)
    time.sleep(0.1)
    started = time.perf_counter()
    s.seek(0.5)
    assert wait_until(lambda: s._seek_request is None, 1, step=0.0005)
    assert time.perf_counter() - started < 0.03
    card.ahead(4)
    assert card.request().any()                                # playing again from 0.5 s
    s.seek(3.5)
    assert wait_until(lambda: s.network_wait is not None, 3)
    started = time.perf_counter()
    s.close()
    assert time.perf_counter() - started <= engine.CLOSE_WAIT + 0.05
    assert not s._decoder.is_alive() and s._src._closed          # the session's files are let go...
    assert wait_until(lambda: not copy.item.leases, 3)            # (... and the proof's, once it stops waiting)


# -- gapless joins into a song still downloading ----------------------------------------------------
def two_songs(tmp_path):
    _, a_data, a_ref, _ = music(tmp_path, "a.wav", 2.0, seed=1)
    _, b_data, b_ref, _ = music(tmp_path, "b.wav", 2.0, seed=2)
    prep = FakePreparer()
    a = prep.add(FakeCopy(tmp_path / "cache", "a.wav", a_data))
    b = prep.add(FakeCopy(tmp_path / "cache", "b.wav", b_data))
    a.arrive()
    return prep, a, b, a_ref, b_ref


def test_a_startable_next_song_joins_sample_exactly(tmp_path, fake_card):
    prep, a, b, a_ref, b_ref = two_songs(tmp_path)
    b.arrive(seconds=1.5)
    advances = []
    s = session_on(a, prep, index=0, follow={0: (1, b.source)}.get,
                   on_advance=lambda s, previous, new: advances.append(new.path))
    card = Card(s)
    card.play_while(lambda: s._reading.path != b.source, keep=2)
    b.arrive()
    card.play_to_end()
    want = np.concatenate([a_ref, b_ref])
    assert np.array_equal(card.audio[: len(want)], want) and advances == [b.source]
    assert ("prepare", b.source, "next", 0.0, False) in prep.calls
    s.close()


def test_a_next_song_ready_within_the_join_window_still_joins_gaplessly(tmp_path, fake_card):
    prep, a, b, a_ref, b_ref = two_songs(tmp_path)
    s = session_on(a, prep, index=0, follow={0: (1, b.source)}.get)
    card = Card(s)
    card.play_while(lambda: s._awaiting is None, keep=12)     # A read to its end; B not startable
    assert s._awaiting == (1, b.source) and s._queued_seconds() > engine.JOIN_GIVE_UP_S
    b.arrive()
    assert wait_until(lambda: s._reading.path == b.source, 3)   # (the card asks again only every 93 ms)
    card.play_to_end()
    want = np.concatenate([a_ref, b_ref])
    assert np.array_equal(card.audio[: len(want)], want)
    assert any(c[:3] == ("prepare", b.source, "join") for c in prep.calls)
    s.close()


def test_a_next_song_not_ready_in_time_lets_this_one_end_naturally_then_starts_by_itself(tmp_path, fake_card):
    prep, a, b, a_ref, b_ref = two_songs(tmp_path)
    p = Player(preparer=prep)
    p.set_volume(1.0)
    p.set_tracks([Track(a.source, "A", duration=2.0), Track(b.source, "B", duration=2.0)])
    p.play(0)
    card = Card(p._session)
    card.play_to_end()
    assert np.array_equal(card.audio[: len(a_ref)], a_ref) and not card.audio[len(a_ref):].any()
    assert wait_until(lambda: p._preparing is not None, 3)
    status = p.status()
    assert status.state is State.PAUSED and status.index == 1 and status.network.auto
    assert "still downloading" in status.transition
    b.arrive()
    prep.tick()
    assert wait_until(lambda: p._session is not None, 3)
    assert p.status().state is State.PLAYING and "still downloading" in p.transition
    second = Card(p._session)
    second.play_to_end()
    assert np.array_equal(second.audio[: len(b_ref)], b_ref)
    p.stop()


def test_up_next_changed_during_the_join_window_lines_up_again(tmp_path, fake_card):
    prep, a, b, a_ref, _ = two_songs(tmp_path)
    c_path, _, c_ref, _ = music(tmp_path, "c.wav", 1.0, seed=3)   # a local song
    p = Player(preparer=prep)
    p.set_volume(1.0)
    p.set_tracks([Track(a.source, "A", duration=2.0), Track(b.source, "B", duration=2.0)])
    p.play(0)
    s = p._session
    card = Card(s)
    card.play_while(lambda: s._awaiting is None, keep=12)
    p.play_next([Track(c_path, "C", duration=1.0)])             # Up next now plays C before B
    assert wait_until(lambda: s._reading.path == c_path, 3)
    card.play_to_end()
    got = card.audio
    want = np.concatenate([a_ref, c_ref])
    assert np.array_equal(got[: len(want)], want)
    p.stop()


def test_a_wait_in_a_next_song_dropped_from_the_plan_ends_with_it(tmp_path, fake_card):
    """The next song's read-ahead was waiting for the network when Up next changed: that wait goes
    with the song, and the song lined up instead waits (and carries on) from its own spot."""
    _, a_data, a_ref, _ = music(tmp_path, "a.wav", 2.0, seed=1)
    _, b_data, b_ref, _ = music(tmp_path, "b.wav", 4.0, seed=2)
    _, c_data, c_ref, _ = music(tmp_path, "c.wav", 10.0, seed=3)
    prep = FakePreparer()
    a, b, c = (prep.add(FakeCopy(tmp_path / "cache", f"{name}.wav", data))
               for name, data in (("a", a_data), ("b", b_data), ("c", c_data)))
    a.arrive()
    b.arrive(seconds=1.2)                                       # B may start; its reads give up at 1.2 s
    c.arrive(seconds=7.0)                                       # C too; its reads give up at 7.0 s
    p = Player(preparer=prep)
    p.set_volume(1.0)
    p.set_tracks([Track(a.source, "A", duration=2.0), Track(b.source, "B", duration=4.0)])
    p.play(0)
    s = p._session
    card = Card(s)
    card.play_while(lambda: s.waiting is None, keep=12, pace=0.09)   # A heard; B's read-ahead waits
    assert s._src.path == a.source and s._reading.path == b.source and not s.waiting.committed
    p.play_next([Track(c.source, "C", duration=10.0)])         # Up next: C after A now, not B
    assert wait_until(lambda: s._reading.path == c.source, 3)
    assert s.waiting is None and s.network_wait is None          # B's wait went with B
    _, cut = stall_and_cut(card, c)                             # C's own pause, at its own spot
    assert s.waiting.source.path == c.source and 6 * RATE < cut <= 7 * RATE
    got = card.audio
    want = np.concatenate([a_ref, c_ref[:cut]])
    assert np.array_equal(got[: len(want)], want) and not got[len(want):].any()   # nothing played twice
    c.arrive()
    b.arrive()                                                  # (after C the list carries on: B)
    card.play_to_end()
    got = card.audio
    assert np.array_equal(got[got.any(axis=1)], np.concatenate([a_ref, c_ref, b_ref]))
    p.stop()


def test_after_a_pause_near_its_end_the_rest_of_a_song_plays_while_the_next_still_downloads(tmp_path,
                                                                                             fake_card):
    """A pause for the network heard in a song's last two seconds; the rest of it comes, but the next
    song isn't ready: the rest plays (it can't fill the buffer, so it doesn't wait for that), then the
    next song starts by itself once it's ready."""
    _, a_data, a_ref, _ = music(tmp_path, "a.wav", 4.0, seed=1)
    _, b_data, _, _ = music(tmp_path, "b.wav", 4.0, seed=2)
    prep = FakePreparer()
    a = prep.add(FakeCopy(tmp_path / "cache", "a.wav", a_data))
    b = prep.add(FakeCopy(tmp_path / "cache", "b.wav", b_data))
    a.arrive(seconds=2.6)                                       # A pauses about 1.4 s before its end
    p = Player(preparer=prep)
    p.set_volume(1.0)
    p.set_tracks([Track(a.source, "A", duration=4.0), Track(b.source, "B", duration=4.0)])
    p.play(0)
    s = p._session
    card = Card(s)
    _, cut = stall_and_cut(card, a)
    assert len(a_ref) - cut < (s._queue_blocks - 1) * BLOCK_FRAMES   # (the rest can't fill the buffer)
    a.arrive()                                                  # the rest of A is here; nothing of B
    assert wait_until(lambda: s._awaiting is not None, 3)       # A read to its end, waiting for B
    card.play_to_end(5.0)
    got = card.audio
    assert np.array_equal(got[got.any(axis=1)], a_ref)          # all of A, once, in order
    assert wait_until(lambda: p._preparing is not None, 3) and p.status().index == 1
    p.stop()


# -- the source goes away mid-song ---------------------------------------------------------------------
@pytest.mark.parametrize("why", ["the file on the drive changed while it was playing (it will play in full next time)",
                                 "it's no longer on \\\\fakehost\\share"])
def test_a_source_gone_mid_song_is_cut_short_and_playback_moves_on(tmp_path, fake_card, why):
    _, data, ref, _ = music(tmp_path, "a.wav", 4.0)
    local_path, _, local_ref, _ = music(tmp_path, "next.wav", 1.0, seed=5)
    prep = FakePreparer()
    copy = prep.add(FakeCopy(tmp_path / "cache", "a.wav", data))
    copy.arrive(seconds=2.0)
    p = Player(preparer=prep)
    p.set_tracks([Track(copy.source, "A", duration=4.0), Track(local_path, "Next", duration=1.0)])
    p.play(0)
    s = p._session
    card = Card(s)
    stall_and_cut(card, copy)
    prep.gone[copy.source] = why
    copy.fail("changed")
    card.play_to_end()
    assert [e.kind for e in s._events if e.kind == "cut short"] == ["cut short"]
    assert s.note.startswith("Cut short at ") and s.note.endswith(f": {why}")   # (said as it is, plainly)
    assert wait_until(lambda: p._session is not None and p._session is not s, 3)
    assert p.status().index == 1                                # moved on to the next song
    p.stop()


# -- a long wait: the device let go, then taken back --------------------------------------------------
@pytest.fixture
def quick_watch(monkeypatch):
    monkeypatch.setattr(engine, "WATCH_SECONDS", 0.02)
    monkeypatch.setattr(engine, "HOLD_RELEASE_S", 0.3)


def pump(card: Card, condition, seconds: float = 5.0) -> None:
    """The card keeps asking (silence while paused for the network) until `condition()`."""
    end = time.monotonic() + seconds
    while time.monotonic() < end and not condition():
        card.request()
        time.sleep(0.005)
    assert condition()


def test_a_long_wait_lets_the_device_go_and_carries_on_by_itself(tmp_path, fake_card, quick_watch):
    _, data, ref, _ = music(tmp_path, "a.wav", 4.0)
    prep = FakePreparer()
    copy = prep.add(FakeCopy(tmp_path / "cache", "a.wav", data))
    copy.arrive(seconds=2.5)
    p = Player(preparer=prep)
    p.set_volume(0.5)
    p.set_tracks([Track(copy.source, "A", duration=4.0)])
    p.play(0)
    s = p._session
    card = Card(s)
    _, cut = stall_and_cut(card, copy)
    status = p.status()
    assert status.state is State.PAUSED and status.network.phase == "waiting" and status.network.auto
    assert status.position == pytest.approx(cut / RATE)
    pump(card, lambda: p._session is not s)                     # 30 s (here 0.3 s) of silence
    assert fake_card[0].closed and p._session is None           # the device is let go
    status = p.status()
    assert status.state is State.PAUSED and status.network.phase == "released" and status.network.auto
    assert status.position == pytest.approx(cut / RATE)
    copy.arrive()
    prep.tick()
    assert wait_until(lambda: p._session is not None, 3)
    resumed = p._session
    assert p.volume == 0.5 and p.output_device is None and len(fake_card) == 2
    assert "network" in p.transition
    second = Card(resumed)
    second.play_to_end()
    got = second.audio
    start = int(np.flatnonzero(got.any(axis=1))[0])
    assert start == 0 or not got[:start].any()
    gain = 0.5 ** 2
    assert np.allclose(got[start: start + len(ref) - cut], ref[cut:] * np.float32(gain), atol=0)
    p.stop()


def test_after_five_minutes_it_waits_for_play(tmp_path, fake_card, quick_watch):
    _, data, ref, _ = music(tmp_path, "a.wav", 4.0)
    prep = FakePreparer()
    copy = prep.add(FakeCopy(tmp_path / "cache", "a.wav", data))
    copy.arrive(seconds=2.5)
    clock = [1000.0]
    p = Player(preparer=prep)
    p._clock = lambda: clock[0]
    p.set_tracks([Track(copy.source, "A", duration=4.0)])
    p.play(0)
    s = p._session
    card = Card(s)
    stall_and_cut(card, copy)
    pump(card, lambda: p._session is not s)
    clock[0] += engine.AUTO_RESUME_S + 1                         # five minutes go by
    assert wait_until(lambda: not p._preparing.auto, 3)
    assert "press play" in p.status().message
    copy.arrive()
    prep.tick()
    time.sleep(0.2)
    assert p._session is None and p.status().message == "The network is back · press play to carry on"
    p.play()
    assert p._session is not None and p.status().state is State.PLAYING
    p.stop()


def test_a_device_lost_during_a_pause_for_the_network_is_still_noticed(tmp_path, fake_card, quick_watch,
                                                                         monkeypatch):
    monkeypatch.setattr(engine, "HOLD_RELEASE_S", 30.0)
    _, data, ref, _ = music(tmp_path, "a.wav", 4.0)
    prep = FakePreparer()
    copy = prep.add(FakeCopy(tmp_path / "cache", "a.wav", data))
    copy.arrive(seconds=2.5)
    p = Player(preparer=prep)
    p.set_tracks([Track(copy.source, "A", duration=4.0)])
    p.play(0)
    card = Card(p._session)
    stall_and_cut(card, copy)
    monkeypatch.setattr(session, "STALL_SECONDS", 0.15)
    card.request()                                               # the last request the card makes
    assert wait_until(lambda: p._session is None, 3)           # the card stopped asking: the device has gone
    assert "stopped playing" in p.status().message and p._preparing is None
    copy.arrive()
    prep.tick()
    time.sleep(0.2)
    assert p._session is None                                    # a lost device always waits for play


# -- preparing ---------------------------------------------------------------------------------------------
def preparing_player(tmp_path, seconds=4.0, need=1.0):
    _, data, ref, _ = music(tmp_path, "a.wav", seconds)
    local_path, _, _, _ = music(tmp_path, "b.wav", 1.0, seed=4)
    prep = FakePreparer(need=need)
    copy = prep.add(FakeCopy(tmp_path / "cache", "a.wav", data))
    p = Player(preparer=prep)
    p.set_tracks([Track(copy.source, "A", duration=seconds), Track(local_path, "B", duration=1.0)])
    return p, prep, copy


def test_preparing_shows_paused_with_the_intent_to_play_and_opens_no_device(tmp_path, fake_card):
    p, prep, copy = preparing_player(tmp_path)
    started = time.perf_counter()
    p.play(0)
    assert time.perf_counter() - started < 0.05
    status = p.status()
    assert status.state is State.PAUSED and status.index == 0 and status.track.title == "A"
    assert status.network.phase == "preparing" and status.network.auto and status.network.percent == 0
    assert not fake_card and p._session is None                  # no stream, not skipped
    copy.arrive(seconds=0.5)
    assert p.status().network.percent == 50
    copy.arrive(seconds=1.5)
    prep.tick()
    assert wait_until(lambda: p._session is not None, 3)
    assert p.status().state is State.PLAYING and len(fake_card) == 1 and p.status().network.phase == "playing"
    p.stop()


def test_pause_stop_next_and_play_while_preparing(tmp_path, fake_card):
    p, prep, copy = preparing_player(tmp_path, need=10.0)
    p.play(0)
    p.toggle_pause()                                             # starting by itself counts as playing
    assert not p._preparing.auto and not p.status().network.auto
    copy.arrive(seconds=3.5)
    p.play()                                                     # forced: 3 s is here
    assert p._session is not None and p.status().state is State.PLAYING
    p.stop()
    p, prep, copy = preparing_player(tmp_path / "2")
    p.play(0)
    p.stop()
    assert p._preparing is None and not prep.waiters and p.status().state is State.STOPPED
    p.play(0)
    p.next()                                                     # the next song is local: it plays at once
    assert p._session is not None and p.status().index == 1 and not prep.waiters
    p.stop()


def test_pause_then_ready_waits_for_play(tmp_path, fake_card):
    p, prep, copy = preparing_player(tmp_path)
    p.play(0)
    p.pause()
    copy.arrive()
    prep.tick()
    time.sleep(0.2)
    assert p._session is None and p.status().message == "“A” is ready: press play"
    p.play()
    assert p._session is not None
    p.stop()


def test_suspend_disarms_the_automatic_start(tmp_path, fake_card):
    p, prep, copy = preparing_player(tmp_path)
    p.play(0)
    assert p.suspend() is True and not p._preparing.auto         # (the ABX test plays its own sound)
    copy.arrive()
    prep.tick()
    time.sleep(0.2)
    assert p._session is None


def test_the_automatic_start_lapses_after_five_minutes(tmp_path, fake_card, quick_watch):
    p, prep, copy = preparing_player(tmp_path)
    clock = [50.0]
    p._clock = lambda: clock[0]
    p.play(0)
    clock[0] += engine.AUTO_RESUME_S + 1
    assert wait_until(lambda: not p._preparing.auto, 3)
    assert p.status().message == "Still downloading after 5 minutes · press play to start it"
    copy.arrive()
    prep.tick()
    time.sleep(0.2)
    assert p._session is None


def test_a_start_that_finds_the_song_not_ready_after_all_keeps_its_five_minutes(tmp_path, fake_card):
    p, prep, copy = preparing_player(tmp_path)
    clock = [1000.0]
    p._clock = lambda: clock[0]
    p.play(0)
    first = p._preparing
    assert first.deadline == 1000.0 + engine.AUTO_RESUME_S
    clock[0] += 200                                              # 200 s of "Preparing…"
    copy.arrive(seconds=1.5)                                     # the cache says it may start …
    prep.prepare = lambda path, **kwargs: prep._not_ready(copy, 0.0)   # … but its outlook drops just then
    assert prep.tick() == 1
    assert wait_until(lambda: p._preparing is not first, 3)
    assert p._preparing is not None and p._preparing.deadline == first.deadline   # never a fresh 5 minutes
    p.stop()


def test_a_seek_while_preparing_moves_where_it_starts(tmp_path, fake_card):
    p, prep, copy = preparing_player(tmp_path)
    p.play(0)
    p.seek(2.0)
    assert p.status().position == 2.0 and ("hint", copy.source, 2.0) in prep.calls
    copy.arrive(seconds=2.5)                                    # enough from 0, not from 2.0
    assert prep.tick() == 0
    copy.arrive(seconds=3.0)
    prep.tick()
    assert wait_until(lambda: p._session is not None, 3)
    assert p._session.position == pytest.approx(2.0, abs=0.01)
    p.stop()


def test_a_lost_post_is_caught_by_the_watch(tmp_path, fake_card, quick_watch):
    p, prep, copy = preparing_player(tmp_path)
    p.play(0)
    prep.waiters.clear()                                         # the cache's word never comes
    copy.arrive()
    assert wait_until(lambda: p._session is not None, 3)
    p.stop()


# -- the proof -----------------------------------------------------------------------------------------------
def test_the_proof_reads_the_same_bytes_and_never_counts_a_wait(tmp_path):
    from ironbark.proof import SentRecord
    _, data, _, ints = music(tmp_path, "a.wav", 2.0)
    copy = FakeCopy(tmp_path / "cache", "a.wav", data)
    copy.arrive(seconds=1.0)
    record = SentRecord(copy.local(), RATE, "int32", BLOCK_FRAMES)
    assert record.path == copy.source and record.summary().copy == "downloading"
    for start in range(0, 2 * RATE - BLOCK_FRAMES, BLOCK_FRAMES):
        record.add(0, start, ints[start: start + BLOCK_FRAMES], 1.0)   # (as if all of it had played)
    reference = audiofile.open(copy.local(), patience=0.05)
    audiofile.set_patience(reference, 0.05)
    assert record.check(reference) is False                      # waited at 1 s: nothing counted
    reference.close()
    assert record.unchecked_frames == 0 and 0 < record.summary().checked < record.summary().sent
    copy.arrive()
    copy.item.state = VERIFYING
    assert record.summary().copy == "checking"
    with audiofile.open(copy.local()) as reference:
        assert record.check(reference)
    copy.ready()
    proof = record.summary()
    assert proof.complete and proof.mismatch_at is None and proof.copy == "verified"


def test_the_proofs_own_thread_waits_for_the_bytes_and_holds_the_copy(tmp_path):
    from ironbark.proof import SentRecord
    _, data, _, ints = music(tmp_path, "a.wav", 2.0)
    copy = FakeCopy(tmp_path / "cache", "a.wav", data)
    copy.arrive(seconds=1.0)
    record = SentRecord(copy.local(), RATE, "int32", BLOCK_FRAMES)
    for start in range(0, 2 * RATE - BLOCK_FRAMES, BLOCK_FRAMES):
        record.add(0, start, ints[start: start + BLOCK_FRAMES], 1.0)
    record.start_checking(interval=0.02)
    assert wait_until(lambda: copy.item.leased(copy.item.gen), 2)
    time.sleep(0.3)
    assert record.unchecked_frames == 0 and not record.summary().complete
    copy.arrive()
    assert wait_until(lambda: record.summary().complete, 10)
    record.stop()
    assert wait_until(lambda: not copy.item.leases, 3)


# -- what plays next, as the cache sees it ---------------------------------------------------------------------
def test_upcoming_follows_up_next_shuffle_repeat_and_stop_after():
    p = Player()
    tracks = [Track(Path(f"{name}.flac"), name) for name in "abcde"]
    p.set_tracks(tracks)
    p.index = 1
    assert p.upcoming(3) == []                                # stopped: nothing plays, nothing to fetch
    p._suspended_at = 0.0                                     # (paused, the device let go: it plays on from here)
    assert [path.stem for _, path in p.upcoming(3)] == ["b", "c", "d", "e"]
    assert [path.stem for _, path in p.upcoming(10)] == ["b", "c", "d", "e"]   # (the end of the list)
    q = Track(Path("q.flac"), "q")
    p.play_next([q])
    order = p.upcoming(3)
    assert [path.stem for _, path in order] == ["b", "q", "c", "d"] and isinstance(order[1][0], engine.Queued)
    p.clear_queue()
    p.set_repeat("all")
    p.index = 3
    assert [path.stem for _, path in p.upcoming(10)] == ["d", "e", "a", "b", "c"]   # round once, no repeats
    p.set_repeat("one")
    assert [path.stem for _, path in p.upcoming(3)] == ["d"]
    p.set_repeat("off")
    p.set_stop_after("song")
    assert [path.stem for _, path in p.upcoming(3)] == ["d"]
    p.set_stop_after(None)
    p.set_shuffle(True)
    p.index = 0
    shuffled = [path.stem for _, path in p.upcoming(10)]
    assert sorted(shuffled) == sorted("abcde") or len(shuffled) == len(set(shuffled))


# -- 0.31.0 features meeting the cache ----------------------------------------------------------------------------
def test_a_sleep_timer_fade_finishing_during_a_pause_for_the_network_ends_it(tmp_path, fake_card, quick_watch,
                                                                                 monkeypatch):
    monkeypatch.setattr(engine, "HOLD_RELEASE_S", 30.0)
    _, data, ref, _ = music(tmp_path, "a.wav", 4.0)
    prep = FakePreparer()
    copy = prep.add(FakeCopy(tmp_path / "cache", "a.wav", data))
    copy.arrive(seconds=2.5)
    p = Player(preparer=prep)
    p.set_tracks([Track(copy.source, "A", duration=4.0)])
    p.play(0)
    s = p._session
    card = Card(s)
    _, cut = stall_and_cut(card, copy)
    assert p.fade_out(0.2) and p.status().fading                 # its clock runs through the silence
    pump(card, lambda: p._session is None)
    status = p.status()
    assert status.message.startswith("Sleep timer: faded out") and status.position == pytest.approx(cut / RATE)
    copy.arrive()
    prep.tick()
    time.sleep(0.2)
    assert p._session is None and p._preparing is None           # it never starts again by itself


def test_a_sleep_timer_while_a_song_waits_to_start_stops_it_starting(tmp_path, fake_card, quick_watch):
    p, prep, copy = preparing_player(tmp_path)
    p.play(0)
    assert p.fade_out(0.1) and p.status().fading
    assert wait_until(lambda: not p._preparing.auto, 3)
    assert p.status().message.startswith("Sleep timer: faded out")
    copy.arrive()
    prep.tick()
    time.sleep(0.2)
    assert p._session is None


def test_stop_after_this_song_fetches_and_starts_nothing_past_it(tmp_path, fake_card):
    a_path, _, a_ref, _ = music(tmp_path, "a.wav", 1.0)
    _, b_data, _, _ = music(tmp_path, "b.wav", 1.0, seed=2)
    prep = FakePreparer()
    b = prep.add(FakeCopy(tmp_path / "cache", "b.wav", b_data))
    p = Player(preparer=prep)
    p.set_tracks([Track(a_path, "A", duration=1.0), Track(b.source, "B", duration=1.0)])
    p.set_stop_after("song")   # (before it starts: the decoder lines up the next song within milliseconds)
    p.play(0)
    assert [path for _, path in p.upcoming(2)] == [a_path]
    card = Card(p._session)
    card.play_to_end()
    assert wait_until(lambda: p._session is None, 3)
    time.sleep(0.1)
    assert p._preparing is None and "Stopped after" in p.status().message
    assert not [c for c in prep.calls if c[0] == "prepare" and c[1] == b.source]


def test_levelling_and_the_guard_set_while_a_song_waits_apply_from_its_first_sample(tmp_path, fake_card):
    p, prep, copy = preparing_player(tmp_path, seconds=2.0)
    p.set_volume(1.0)
    p.play(0)
    p.set_levelling(copy.source, SimpleNamespace(gain=0.5))    # measured while it waited
    p.set_trim(copy.source, 0.8)
    copy.arrive()
    prep.tick()
    assert wait_until(lambda: p._session is not None, 3)
    s = p._session
    first = s._src
    card = Card(s)
    card.play_to_end()
    proof = first.sent.summary()
    assert first.path == copy.source and proof.sent == pytest.approx(2.0)
    assert proof.min_gain == pytest.approx(0.5) and proof.max_gain == 1.0   # (never turned up)
    assert proof.changed_from == 0.0
    p.stop()


# -- no network on the window's thread, under the player's lock, in the decoder ----------------------
def flac_song(tmp_path, seconds=3.0):
    t = np.arange(int(RATE * seconds)) / RATE
    x = 0.3 * np.sin(2 * np.pi * 330 * t)[:, None] * np.ones((1, 2))
    path = tmp_path / "src.flac"
    sf.write(path, np.rint(x * 32767).astype(np.int16), RATE, subtype="PCM_16")
    with audiofile.open(path) as f:
        ref = audiofile.read(f, len(t), "float32")
    return path.read_bytes(), ref


def test_a_network_song_is_only_ever_read_by_the_caches_own_threads(tmp_path, fake_card, monkeypatch):
    data, ref = flac_song(tmp_path)
    share = FakeShare(rate=2_000_000)
    path = share.add("song.flac", data)
    p = Player()
    p.set_volume(1.0)
    m = make_manager(tmp_path / "NetCache", share, None)
    m.player = p
    p.set_preparer(m)
    seen = []

    def watch(name, real):
        def spy(target, *args, **kwargs):
            text = os.fspath(target) if isinstance(target, (str, os.PathLike)) else ""
            if text.startswith("\\\\fakehost"):
                seen.append((name, threading.current_thread().name))
                raise OSError("a network path was opened outside the cache's I/O threads")
            return real(target, *args, **kwargs)
        return spy

    for module, name in ((builtins, "open"), (io, "open"), (os, "stat"), (os, "open")):
        monkeypatch.setattr(module, name, watch(name, getattr(module, name)))
    real_init = sf.SoundFile.__init__
    monkeypatch.setattr(sf.SoundFile, "__init__", lambda self, file, *a, **k: (
        watch("SoundFile", lambda *x, **y: None)(file) if isinstance(file, (str, os.PathLike)) else None,
        real_init(self, file, *a, **k))[1])
    try:
        p.set_tracks([Track(path, "Song", duration=3.0)])
        p.play(0)
        assert wait_until(lambda: p._session is not None, 20)
        card = Card(p._session)
        card.play_to_end()
        got = card.audio
        assert np.array_equal(got[: len(ref)], ref)
        assert p._session is None or p._session._src.local.remote
        assert seen == []
    finally:
        p.stop()
        m.shutdown()


def test_nothing_waits_on_a_server_that_doesnt_answer(tmp_path, fake_card):
    data, _ = flac_song(tmp_path)
    share = FakeShare()
    path = share.add("song.flac", data)
    other = share.add("other.flac", data)
    share.open.clear()                                           # every call to the server hangs
    p = Player()
    m = make_manager(tmp_path / "NetCache", share, None)
    m.player = p
    p.set_preparer(m)
    try:
        p.set_tracks([Track(path, "Song", duration=3.0), Track(other, "Other", duration=3.0)])
        assert wait_until(lambda: share.waiting > 0 or True, 1)
        for action in (lambda: p.play(0), p.status, lambda: p.seek(1.0), p.status, p.next, p.status,
                       p.toggle_pause, p.status, p.stop):
            started = time.perf_counter()
            action()
            assert time.perf_counter() - started < 0.05
        audiofile.set_resolver(m.resolve, info=m.known_info, partial_folder=m.store.partial)
        started = time.perf_counter()
        with pytest.raises(audiofile.NotCached):
            audiofile.open(path)                                 # the window's own thread never waits
        assert time.perf_counter() - started < 0.01
    finally:
        audiofile.set_resolver(None)
        share.open.set()
        p.stop()
        m.shutdown()


def test_a_second_pause_soon_after_the_first_never_repeats_any_music(tmp_path, fake_card):
    """The decoder carries on after a pause and runs out again before the card has played past the
    first pause's marker: the second wait is its own, from its own spot, and nothing is heard twice."""
    _, data, ref, _ = music(tmp_path, "a.wav", 5.0)
    prep = FakePreparer()
    copy = prep.add(FakeCopy(tmp_path / "cache", "a.wav", data))
    copy.arrive(seconds=2.5)
    s = session_on(copy, prep)
    card = Card(s)
    _, cut = stall_and_cut(card, copy)
    first = s.waiting.serial
    copy.arrive(seconds=cut / RATE + 2.0)    # twice the 1 s asked for (one recent pause), less than a buffer
    assert wait_until(lambda: s._resumed_serial >= first, 3)
    assert wait_until(lambda: s.waiting is not None and s.waiting.serial != first, 5)
    assert s.waiting.frame > cut                                 # its own spot, past the first cut
    for _ in range(10):
        assert not card.request().any()                          # still the first pause: the buffer isn't full
    assert s.network_wait is not None and s.position == pytest.approx(cut / RATE)
    copy.arrive()
    card.play_to_end()
    got = card.audio
    assert np.array_equal(got[got.any(axis=1)], ref)             # every sample once, in order
    s.close()


def test_a_sleep_timer_running_out_while_a_song_waits_for_the_network_still_ends_the_music(tmp_path, fake_card,
                                                                                            quick_watch, monkeypatch):
    from ironbark import sleep
    monkeypatch.setattr(sleep, "FADE_SECONDS", 0.2)
    p, prep, copy = preparing_player(tmp_path)
    p.play(0)
    clock = [0.0]
    timer = sleep.SleepTimer(p, 1, clock=lambda: clock[0])
    clock[0] = 61.0                                              # time's up while it waits
    timer.tick(p.status())
    assert timer.state == sleep.FADING and p.status().fading
    assert wait_until(lambda: not p._preparing.auto, 5)        # the fade runs out: it won't start
    copy.arrive()
    prep.tick()
    time.sleep(0.2)
    assert p._session is None


def test_a_sleep_timer_running_out_in_bit_perfect_while_a_song_waits_never_fades_it(tmp_path, fake_card,
                                                                                   bit_perfect, monkeypatch):
    """No stream is open yet, but the song will start in bit-perfect: the timer stops at its end
    (the bit-perfect rule), rather than fading the music it sends to the device."""
    from ironbark import sleep
    monkeypatch.setattr(engine.devices, "find_device", lambda name, rescan_ok=True: 0)
    monkeypatch.setattr(engine.devices, "default_output_name", lambda: "Test DAC")
    monkeypatch.setattr(engine.devices, "has_hardware_volume", lambda name: True)
    p, prep, copy = preparing_player(tmp_path)
    p.refresh_hardware_volume()                                  # (the window's check, as bit-perfect goes on)
    p.set_exclusive(True)
    p.play(0)
    clock = [0.0]
    timer = sleep.SleepTimer(p, 1, clock=lambda: clock[0])
    clock[0] = 61.0                                              # time's up while it waits
    timer.tick(p.status())
    assert timer.state == sleep.AT_BOUNDARY and p.stop_after == "song" and not p.status().fading
    copy.arrive()
    prep.tick()
    assert wait_until(lambda: p._session is not None, 3)
    status = p.status()
    assert status.output.exclusive and not status.fading and p._fade is None and status.stop_after == "song"
    p.stop()


@pytest.mark.skipif(os.name != "nt", reason="Windows network paths")
def test_a_song_found_gone_while_it_waits_to_start_is_skipped_saying_why(tmp_path, fake_card, quick_watch):
    p, prep, copy = preparing_player(tmp_path)
    p.play(0)
    assert p.status().network.phase == "preparing"
    prep.gone[copy.source] = "it's no longer on \\\\fakehost\\share"   # (the cache's look at the drive found that)
    assert wait_until(lambda: p.status().index == 1, 3)                  # never minutes of "Preparing…"
    status = p.status()
    assert p._preparing is None and p._session is not None and status.state is State.PLAYING
    assert status.message == "Skipped a.wav: it's no longer on \\\\fakehost\\share"
    p.stop()


def test_a_song_with_no_room_found_while_it_waits_to_start_stops_there_saying_why(tmp_path, fake_card,
                                                                                    quick_watch):
    p, prep, copy = preparing_player(tmp_path)
    p.play(0)
    no_room = manager_mod.Unavailable("Not enough free space on C: for the music cache", False)
    prep.outlook = lambda x, at=0.0: no_room
    prep.prepare = lambda path, **kwargs: no_room
    assert wait_until(lambda: p._preparing is None, 3)
    status = p.status()
    assert status.index == 0 and status.state is State.PAUSED and p._session is None
    assert status.message.startswith("Not enough free space")
