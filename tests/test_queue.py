"""Up next (0.15.0): "Play next" / "Add to Up next" songs play before the list carries on, in order;
a queued song joins on gaplessly (checked sample for sample through the stand-in sound card, as
test_gapless.py), and the list carries on after the song the queue interrupted."""
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from ironbark import engine
from ironbark.engine import Player, Queued, State
from ironbark.library import Track

from test_gapless import FakeStream, pull, wav  # noqa: F401 (the stand-in sound card)


@pytest.fixture
def fake_card(monkeypatch):
    monkeypatch.setattr(engine.sd, "OutputStream", FakeStream)
    monkeypatch.setattr(engine.devices, "find_device", lambda name: None)
    monkeypatch.setattr(engine.devices, "mixer_rate", lambda index: None)


def songs(*names):
    return [Track(Path(f"{n}.flac"), n) for n in names]


FAKE_SESSION = SimpleNamespace(retire=lambda source: None)


def join(p, previous, index):
    """What the engine does when the next song's first sample goes out."""
    path = index.track.path if isinstance(index, Queued) else p.tracks[index].path
    p._joined(FAKE_SESSION, previous, SimpleNamespace(index=index, path=path))


def test_up_next_plays_before_the_list_carries_on():
    p = Player()
    a, b, c = songs("a", "b", "c")
    q1, q2 = songs("q1", "q2")
    p.set_tracks([a, b, c])
    p.index = 0
    p.add_to_queue([q1])
    p.add_to_queue([q2])
    following, path = p._follow(0)
    assert isinstance(following, Queued) and path == q1.path and p.up_next() == [q1, q2]  # (lined up: still listed)
    join(p, SimpleNamespace(), following)
    assert p.up_next() == [q2] and p.queued_now is following and p.index == 0  # heard: it leaves the list
    status = p.status()
    assert status.track is q1 and status.index == -1        # not one of the list's songs
    second, path = p._follow(following)
    assert path == q2.path
    join(p, SimpleNamespace(), second)
    assert p.up_next() == [] and p._follow(second) == (1, b.path)   # then the list: after a, b
    join(p, SimpleNamespace(), 1)
    assert p.queued_now is None and p.index == 1 and p.status().index == 1


def test_play_next_goes_in_front_in_the_order_given():
    p = Player()
    p.set_tracks(songs("a", "b"))
    x, y, z = songs("x", "y", "z")
    p.add_to_queue([z])
    assert p.play_next([x, y]) == 2
    assert p.up_next() == [x, y, z]
    p.remove_from_queue(1)
    assert p.up_next() == [x, z]
    p.clear_queue()
    assert p.up_next() == []


def test_missing_and_unplayable_songs_are_not_queued():
    p = Player()
    ok, gone, aac = songs("ok", "gone", "aac")
    gone.missing, aac.unsupported = True, "AAC"
    assert p.add_to_queue([ok, gone, aac]) == 1 and p.up_next() == [ok]


def test_a_short_queued_song_read_to_its_end_before_it_is_heard():
    """The decoder can finish a queued song before its first sample is heard (it's still listed):
    what follows it is the next queued song, not the same one again."""
    p = Player()
    p.set_tracks(songs("a", "b"))
    q1, q2 = songs("q1", "q2")
    p.add_to_queue([q1, q2])
    first, _ = p._follow(0)
    assert p._follow(first)[1] == q2.path


def test_repeat_one_still_lets_up_next_go_first():
    p = Player()
    a, b = songs("a", "b")
    p.set_tracks([a, b])
    p.index = 0
    p.set_repeat("one")
    q, = songs("q")
    p.add_to_queue([q])
    queued, _ = p._follow(0)
    join(p, SimpleNamespace(), queued)
    assert p._follow(queued) == (0, a.path)  # then the song being repeated, again


def test_the_list_changing_while_a_queued_song_plays():
    p = Player()
    a, b, c = songs("a", "b", "c")
    p.set_tracks([a, b, c])
    p.index = 1
    q, = songs("q")
    p.add_to_queue([q])
    queued, _ = p._follow(1)
    join(p, SimpleNamespace(), queued)
    p._session = SimpleNamespace(paused=False, close=lambda: None, relineup=lambda: None)   # (as if playing)
    p.set_tracks([c, b, a], keep_playing=True)                       # e.g. a playlist reordered
    assert p.queued_now is queued and p.index == 1                   # b moved; it's still where the list carries on
    assert p._follow(queued) == (2, a.path)
    p._session = None


def test_queued_song_joins_gaplessly_then_the_list_carries_on(tmp_path, fake_card):
    (a, da), (b, db), (q, dq) = wav(tmp_path, "a.wav", 10000), wav(tmp_path, "b.wav", 6000), wav(tmp_path, "q.wav", 7000)
    p = Player()
    p.set_volume(1.0)
    ta, tb, tq = Track(a, "a"), Track(b, "b"), Track(q, "q")
    p.set_tracks([ta, tb])
    p.add_to_queue([tq])
    p.play(0)
    session = p._session
    got = pull(session)
    want = np.concatenate([da, dq, db])   # a, then the queued song, then the list carries on with b
    assert np.array_equal(got[: len(want)], want)       # every sample, in order, nothing in between
    assert not got[len(want):].any()
    deadline = time.monotonic() + 5
    while (p._session is not None or p.status().index != 0) and time.monotonic() < deadline:
        time.sleep(0.01)  # the player's own thread hears "advance" twice, then "end"
    assert p.up_next() == [] and p.queued_now is None
    p.close()


def test_queued_song_proof_and_status_while_it_plays(tmp_path, fake_card):
    (a, _), (b, _), (q, _) = wav(tmp_path, "a.wav", 9000), wav(tmp_path, "b.wav", 3000), wav(tmp_path, "q.wav", 30000)
    p = Player()
    p.set_volume(1.0)
    tq = Track(q, "q")
    p.set_tracks([Track(a, "a"), Track(b, "b")])
    p.play_next([tq])
    p.play(0)
    session = p._session
    out = []
    deadline = time.monotonic() + 5
    while p.queued_now is None and time.monotonic() < deadline:  # play until the queued song is heard
        while session._queue.qsize() < 2 and not session._draining and time.monotonic() < deadline:
            time.sleep(0.002)
        block = np.zeros((engine.BLOCK_FRAMES, 2), np.float32)
        session._callback(block, engine.BLOCK_FRAMES, SimpleNamespace(outputBufferDacTime=0.0, currentTime=0.0),
                          SimpleNamespace(output_underflow=False))
        out.append(block)
        time.sleep(0.02)
    status = p.status()
    assert status.state is State.PLAYING and status.track is tq and status.index == -1 and p.index == 0
    assert p.transition == "gapless" and session._src.path == q
    with sf.SoundFile(str(q)) as ref:   # its own bit-perfect proof, from its first sample
        session._src.sent.check(ref)
    assert session._src.sent.summary().mismatch_at is None
    assert p.following_path() == b                     # the clipping guard looks ahead to b
    p.next()                                           # next: the list carries on
    assert p.queued_now is None and p.index == 1
    p.close()


def test_next_and_previous_with_up_next(tmp_path, fake_card):
    (a, _), (b, _), (q, _) = wav(tmp_path, "a.wav", 40000), wav(tmp_path, "b.wav", 40000), wav(tmp_path, "q.wav", 40000)
    p = Player()
    tq = Track(q, "q")
    p.set_tracks([Track(a, "a"), Track(b, "b")])
    p.play(0)
    p.add_to_queue([tq])
    p.next()                                   # next goes to Up next first
    assert p.queued_now is not None and p.status().track is tq and p.up_next() == []
    p.previous()                               # from a queued song: back to the list's song it interrupted
    assert p.queued_now is None and p.index == 0
    p.play_next([tq])
    picked = p.queue[0]
    p.play_queued(picked)                      # picked from the Up next list: plays now
    assert p.status().track is tq and p.up_next() == []
    p.play_queued(picked)                      # (picked again from a list not yet redrawn: it has played)
    assert p.status().track is tq and p.queued_now is picked
    p.set_exclusive(False)
    p.set_output_device("elsewhere")           # reopening the device keeps the queued song
    assert p.status().track is tq
    p.stop()
    assert p.status().state is State.STOPPED and p.queued_now is None
    p.close()


def test_play_with_only_up_next(tmp_path, fake_card):
    (q, _), = [wav(tmp_path, "q.wav", 5000)]
    p = Player()
    p.add_to_queue([Track(q, "q")])
    p.play()
    assert p.status().track.path == q and p.up_next() == []
    p.close()
