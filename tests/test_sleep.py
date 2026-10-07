"""Stop after this song / this album, and the sleep timer: the engine stops cleanly at a song's end
(no gapless join into the next), the fade goes through the proof's gain path, and the timer (on a
clock moved by hand) fades in shared mode but stops at a song boundary in bit-perfect mode."""
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import sounddevice as sd
import soundfile as sf

from ironbark import engine, sleep
from ironbark.engine import BLOCK_FRAMES, FADE_FLOOR_DB, OutputInfo, Player, State
from ironbark.library import Track

RNG = np.random.default_rng(23)
TIME_INFO = SimpleNamespace(outputBufferDacTime=0.0, currentTime=0.0)
OK = SimpleNamespace(output_underflow=False)


class FakeStream:
    def __init__(self, **kwargs):
        self.callback = kwargs["callback"]
        self.latency, self.device = 0.1, 0

    def start(self):
        pass

    stop = abort = close = start


@pytest.fixture
def fake_card(monkeypatch):
    monkeypatch.setattr(engine.sd, "OutputStream", FakeStream)
    monkeypatch.setattr(engine.devices, "find_device", lambda name: None)
    monkeypatch.setattr(engine.devices, "mixer_rate", lambda index: None)


def wav(folder: Path, name, frames):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    sf.write(path, (RNG.standard_normal((frames, 2)) * 4000).astype(np.int16), 44100, subtype="PCM_16")
    return path, sf.read(path, dtype="float32", always_2d=True)[0]


def pull(session, limit=300):
    out = []
    for _ in range(limit):
        deadline = time.monotonic() + 2
        while session._queue.qsize() < 2 and not session._draining and time.monotonic() < deadline:
            time.sleep(0.002)
        block = np.zeros((BLOCK_FRAMES, 2), np.float32)
        try:
            session._callback(block, BLOCK_FRAMES, TIME_INFO, OK)
        except sd.CallbackStop:
            return np.concatenate(out)
        if out or block.any():
            out.append(block)
    raise AssertionError("never reached the end")


def wait_lined_up(session, path):
    deadline = time.monotonic() + 5
    while session._reading.path != path and time.monotonic() < deadline:
        time.sleep(0.005)
    assert session._reading.path == path


def start(player, paths):
    player.set_tracks([Track(p, p.stem) for p in paths])
    player.set_volume(1.0)
    with player._lock:
        player._start(0)
    return player._session


# -- stop after this song -----------------------------------------------------------------------------
def test_stop_after_this_song_ends_the_gapless_chain_there(tmp_path, fake_card):
    # B is longer than the read-ahead buffer, so the decoder lines it up but never reaches C.
    (a, da), (b, _), (c, _) = (wav(tmp_path, n, f) for n, f in (("a.wav", 9000), ("b.wav", 200000), ("c.wav", 5000)))
    player = Player()
    session = start(player, [a, b, c])
    wait_lined_up(session, b)                       # the decoder has already gone on into B…
    player.set_stop_after("song")                   # …so it's dropped again
    got = pull(session)
    assert np.array_equal(got[: len(da)], da) and not got[len(da):].any()   # A, then nothing
    assert session._src.path == a                   # no gapless join happened
    with player._lock:
        player._ended(session)                      # (what the player's own thread does at the end)
    status = player.status()
    assert status.state is State.STOPPED and status.index == 1 and player.stop_after is None
    assert status.message == "Stopped after “a”, as asked"
    # Play carries on from the next song (here, as a new start).
    with player._lock:
        player.play()
    assert player._session is not None and player._session._src.path == b
    player.stop()


def test_without_it_the_same_songs_join_gaplessly(tmp_path, fake_card):
    (a, da), (b, db_) = wav(tmp_path, "a.wav", 9000), wav(tmp_path, "b.wav", 7000)
    player = Player()
    session = start(player, [a, b])
    player.set_stop_after("song")
    player.set_stop_after(None)                     # changed my mind: the chain is lined up again
    got = pull(session)
    assert np.array_equal(got[: len(da) + len(db_)], np.concatenate([da, db_]))
    player.stop()


def test_stop_after_this_album_lets_its_songs_join_then_stops(tmp_path, fake_card):
    (a1, d1), (a2, d2) = wav(tmp_path / "Album A", "1.wav", 6000), wav(tmp_path / "Album A", "2.wav", 6000)
    (b1, _) = wav(tmp_path / "Album B", "1.wav", 6000)
    player = Player()
    session = start(player, [a1, a2, b1])
    player.set_stop_after("album")
    got = pull(session)
    want = np.concatenate([d1, d2])
    assert np.array_equal(got[: len(want)], want) and not got[len(want):].any()
    assert session._src.path == a2                  # A1 → A2 joined on; Album B never started
    deadline = time.monotonic() + 5                 # (the player's own thread notes the join)
    while player.index != 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    with player._lock:
        player._ended(session)                      # …and then the end
    assert player.status().message == "Stopped at the end of the album, as asked" and player.index == 2
    player.stop()


def test_stop_after_a_song_from_up_next_resumes_with_the_queue(tmp_path, fake_card):
    (a, _), (b, _), (q, _) = (wav(tmp_path, n, 5000) for n in ("a.wav", "b.wav", "q.wav"))
    player = Player()
    session = start(player, [a, b])
    player.add_to_queue([Track(q, "q")])
    player.set_stop_after("song")
    pull(session)
    with player._lock:
        player._ended(session)
    assert player._session is None and player.up_next()[0].path == q
    with player._lock:
        player.play()                               # Up next's song comes first, as it would have
    assert player._session._src.path == q and player.queued_now is not None
    player.stop()


def test_stop_after_resumes_with_the_list_if_that_song_left_up_next(tmp_path, fake_card):
    (a, _), (b, _), (q, _) = (wav(tmp_path, n, 5000) for n in ("a.wav", "b.wav", "q.wav"))
    player = Player()
    session = start(player, [a, b])
    player.add_to_queue([Track(q, "q")])
    player.set_stop_after("song")
    pull(session)
    with player._lock:
        player._ended(session)
    player.remove_from_queue(0)                     # taken out of Up next while stopped
    with player._lock:
        player.play()
    assert player._session._src.path == b           # the list carries on (not "a" over again)
    player.stop()


def test_stopped_after_a_song_play_carries_on_with_the_next_even_after_songs_are_added(tmp_path, fake_card):
    (a, _), (b, _), (c, _), (d, _) = (wav(tmp_path, n, 5000) for n in ("a.wav", "b.wav", "c.wav", "d.wav"))
    player = Player()
    session = start(player, [a, b, c])
    player.set_stop_after("song")
    pull(session)
    with player._lock:
        player._ended(session)
    assert player._session is None and player.index == 1
    player.set_tracks([Track(p, p.stem) for p in (d, a, b, c)], keep_playing=True)   # an album added to the tab
    assert player.status().index == 2               # still b, where it now sits
    with player._lock:
        player.play()
    assert player._session._src.path == b           # b next, as it was (not the list's first song)
    player.stop()


def test_a_spot_paused_with_the_device_let_go_survives_songs_added_to_the_list(tmp_path, fake_card):
    """The sleep timer's end, a lost device, ABX or Test signals leave the player paused with no stream
    open (suspend): songs added to the tab playing meanwhile keep that spot for play."""
    paths = [wav(tmp_path, f"{n}.wav", 44100 * 3)[0] for n in range(5)]
    tracks = [Track(path, path.stem, duration=3.0) for path in paths]
    player = Player()
    player.set_tracks(tracks[:3])
    player.play(2, start_at=1.5)
    assert player.suspend() and player._session is None
    player.set_tracks(tracks[:4], keep_playing=True)             # an album added to the tab playing
    status = player.status()
    assert status.state is State.PAUSED and status.index == 2 and status.position == pytest.approx(1.5)
    player.play()                                                # carries on from there
    status = player.status()
    assert status.state is State.PLAYING and status.track is tracks[2] and status.position == pytest.approx(1.5)
    # A song from Up next, paused the same way, carries on too (the list after it from the same place).
    player.add_to_queue([tracks[4]])
    player.next()
    assert player.queued_now is not None and player.suspend()
    player.set_tracks(tracks[:4] + [tracks[0]], keep_playing=True)
    status = player.status()
    assert status.state is State.PAUSED and status.track is tracks[4] and player.index == 2
    # The song itself taken out of the list: nothing left to carry on with (stopped, as when playing).
    player.next()
    player.suspend()
    player.set_tracks(tracks[:3], keep_playing=True)
    assert player.status().state is State.STOPPED
    player.stop()
    player.stop()


def test_end_of_this_album_stops_even_when_the_list_goes_round(tmp_path, fake_card):
    # One album with repeat all (or repeat one) never reaches another album: going round is its end.
    (a1, _), (a2, _) = wav(tmp_path / "Album", "1.wav", 5000), wav(tmp_path / "Album", "2.wav", 5000)
    player = Player()
    player.set_tracks([Track(a1, "1"), Track(a2, "2")])
    player.set_stop_after("album")
    for repeat in ("all", "one"):
        player.set_repeat(repeat)
        assert player._stops_after(1) and player._follow(1) is None
    player.set_repeat("all")
    assert not player._stops_after(0) and player._follow(0)[1] == a2   # (the album's own songs still join)
    session = start(player, [a1, a2])
    player.set_stop_after("album")
    pull(session)
    deadline = time.monotonic() + 5
    while player.index != 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    with player._lock:
        player._ended(session)
    assert player._session is None and player.status().message == "Stopped at the end of the album, as asked"
    assert player.index == 0                        # ready to play the album again
    player.stop()


def test_a_fade_carries_on_into_a_song_that_doesnt_join_gaplessly(tmp_path, fake_card):
    # A sample-rate change means a fresh start, not a gapless join: the fade must not end there,
    # or the sleep timer would leave the next song playing at full level.
    (a, _) = wav(tmp_path, "a.wav", 9000)
    b = tmp_path / "b.wav"
    sf.write(b, (RNG.standard_normal((9000, 2)) * 4000).astype(np.int16), 48000, subtype="PCM_16")
    now = [100.0]
    player = Player()
    player._clock = lambda: now[0]
    session = start(player, [a, b])
    assert player.fade_out(10.0)
    now[0] += 5.0
    pull(session)
    with player._lock:
        player._ended(session)                      # B starts afresh (the device reopens at 48 kHz)
    assert player._session is not None and player._session._src.path == b
    status = player.status()
    assert status.fading and player._gain(b) == pytest.approx(10 ** (FADE_FLOOR_DB * 0.5 / 20))
    timer = sleep.SleepTimer(player, 15, clock=lambda: now[0])
    timer.state = sleep.FADING
    timer.tick(status)
    assert timer.active                             # still fading: the timer hasn't let go
    now[0] += 5.0
    with player._lock:
        player._faded()
    assert player.status().state is State.PAUSED
    player.stop()


def test_stop_by_hand_forgets_it():
    player = Player()
    player.set_stop_after("song")
    assert player.status().stop_after == "song"
    player.stop()
    assert player.stop_after is None
    player.set_stop_after("nonsense")
    assert player.stop_after is None


# -- the fade -------------------------------------------------------------------------------------------
def test_the_fade_falls_evenly_in_db_then_pauses_where_it_was(tmp_path, fake_card):
    (a, da) = wav(tmp_path, "a.wav", 44100 * 3)
    now = [100.0]
    player = Player()
    player._clock = lambda: now[0]
    session = start(player, [a])
    assert player.fade_out(10.0)
    assert player._gain(a) == 1.0                    # (at its start)
    now[0] += 5.0
    assert player._gain(a) == pytest.approx(10 ** (FADE_FLOOR_DB * 0.5 / 20))
    assert player.status().fading
    block = np.zeros((BLOCK_FRAMES, 2), np.float32)
    deadline = time.monotonic() + 2
    while session._queue.qsize() < 2 and time.monotonic() < deadline:
        time.sleep(0.002)
    session._callback(block, BLOCK_FRAMES, TIME_INFO, OK)
    assert np.allclose(block, da[:BLOCK_FRAMES] * np.float32(10 ** (FADE_FLOOR_DB * 0.5 / 20)))
    proof = session.sent.summary()
    assert not proof.untouched                      # the proof records the fade as a change of level
    now[0] += 5.0
    assert player._gain(a) == 0.0
    with player._lock:
        player._faded()                             # (the player's own thread, once the fade is over)
    status = player.status()
    assert status.state is State.PAUSED and not status.fading
    assert player._suspended_at == pytest.approx(BLOCK_FRAMES / 44100)   # play carries on from there
    assert "Sleep timer: faded out" in status.message
    player.stop()


def test_pausing_by_hand_ends_the_fade(tmp_path, fake_card):
    (a, _) = wav(tmp_path, "a.wav", 44100)
    player = Player()
    start(player, [a])
    assert player.fade_out(10.0) and player._fade is not None
    player.pause()
    assert player._fade is None and player._gain(a) == 1.0
    assert not player.fade_out(10.0)                # paused: nothing to fade
    player.stop()


# -- the sleep timer (a clock moved by hand) -------------------------------------------------------------
class TimerPlayer:
    def __init__(self):
        self.stop_after, self.fades, self.cancelled = None, [], 0

    def set_stop_after(self, mode):
        self.stop_after = mode

    def fade_out(self, seconds):
        self.fades.append(seconds)
        return True

    def cancel_fade(self):
        self.cancelled += 1


def status(exclusive=False, state=State.PLAYING, fading=False, stop_after=""):
    output = OutputInfo("DAC", 44100, None if exclusive else 48000, exclusive=exclusive)
    return SimpleNamespace(state=state, output=output, fading=fading, stop_after=stop_after)


def test_shared_mode_fades_when_the_time_is_up():
    now = [0.0]
    player = TimerPlayer()
    timer = sleep.SleepTimer(player, 30, clock=lambda: now[0])
    assert timer.describe() == "Sleep timer: 30 min left"
    now[0] = 29 * 60 + 1
    timer.tick(status())
    assert timer.describe() == "Sleep timer: under a minute left" and player.fades == []
    now[0] = 30 * 60
    timer.tick(status())
    assert player.fades == [sleep.FADE_SECONDS] and timer.state == sleep.FADING
    assert timer.describe() == "Sleep timer: fading out…"
    timer.tick(status(fading=True))
    assert timer.active
    timer.tick(status(fading=False))                # faded and paused
    assert not timer.active and timer.describe() == ""


def test_bit_perfect_mode_stops_at_the_next_song_boundary_instead():
    now = [0.0]
    player = TimerPlayer()
    timer = sleep.SleepTimer(player, 15, clock=lambda: now[0])
    now[0] = 15 * 60
    timer.tick(status(exclusive=True))
    assert player.fades == [] and player.stop_after == "song" and timer.state == sleep.AT_BOUNDARY
    assert "stops after this song (bit-perfect: no fade)" in timer.describe()
    timer.tick(status(exclusive=True, stop_after="song"))
    assert timer.active
    timer.tick(status(exclusive=True, stop_after=""))   # it stopped there
    assert not timer.active


def test_end_of_this_album_and_cancelling():
    player = TimerPlayer()
    timer = sleep.SleepTimer(player, sleep.ALBUM, clock=lambda: 0.0)
    assert player.stop_after == "album" and timer.remaining() is None
    assert timer.describe() == "Sleep timer: stopping at the end of this album"
    timer.cancel()
    assert player.stop_after is None and not timer.active
    now = [0.0]
    faded = sleep.SleepTimer(player, 45, clock=lambda: now[0])
    now[0] = 45 * 60.0
    faded.tick(status())
    faded.cancel()
    assert player.cancelled == 1


def test_nothing_playing_when_the_time_is_up_does_nothing():
    player = TimerPlayer()
    now = [0.0]
    timer = sleep.SleepTimer(player, 60, clock=lambda: now[0])
    now[0] = 3600.0
    timer.tick(status(state=State.PAUSED))
    assert not timer.active and player.fades == [] and player.stop_after is None


# -- the window's side: menus, the status line, the methods other keys can call --------------------------
from test_light_mini import _pump, make_window  # noqa: E402,F401  (make_window is a fixture)


def test_the_window_offers_and_shows_them(make_window):
    window, root, player, errors = make_window()
    assert window.stop_after_current() is True and player.stop_after == "song"
    status = SimpleNamespace(state=State.PLAYING, stop_after="song")
    assert window._sleep_line(status) == "Stops after this song"
    assert window.stop_after_current() is False and player.stop_after is None
    window.set_sleep_timer(45)
    assert window.sleep_timer is not None and window._sleep_line(status) == "Sleep timer: 45 min left"
    window.set_sleep_timer("album")
    assert player.stop_after == "album" and window.sleep_timer.target == "album"
    album = SimpleNamespace(state=State.PLAYING, stop_after="album")
    assert window._sleep_line(album) == "Sleep timer: stopping at the end of this album"
    assert window.stop_after_current() is True      # sooner than the album's end: the line says so
    assert window._sleep_line(status) == "Stops after this song"
    assert window.stop_after_current() is False and player.stop_after == "album"   # (the album's end again)
    window.set_sleep_timer(None)
    assert window.sleep_timer is None and player.stop_after is None
    assert not errors
