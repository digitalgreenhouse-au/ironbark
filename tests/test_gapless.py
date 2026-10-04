"""Gapless playback, checked sample for sample through a stand-in sound card: what the callback
hands over must be exactly track A followed by track B, with nothing missing, repeated or
inserted between them."""
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import sounddevice as sd
import soundfile as sf

from ironbark import engine
from ironbark.engine import BLOCK_FRAMES, Player, _Session
from ironbark.library import Track

RNG = np.random.default_rng(7)
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


def wav(tmp_path, name, frames, rate=44100):
    data = (RNG.standard_normal((frames, 2)) * 4000).astype(np.int16)
    path = tmp_path / name
    sf.write(path, data, rate, subtype="PCM_16")
    return path, sf.read(path, dtype="float32", always_2d=True)[0]


def pull(session, limit=200):
    """Ask for blocks the way the sound card does, until the session stops."""
    out = []
    for _ in range(limit):
        deadline = time.monotonic() + 2
        while ((session._seek_request is not None or (session._queue.qsize() < 2 and not session._draining))
               and time.monotonic() < deadline):
            time.sleep(0.002)  # let the decoder keep ahead, as it does in real playback
        block = np.zeros((BLOCK_FRAMES, 2), np.float32)
        try:
            session._callback(block, BLOCK_FRAMES, TIME_INFO, OK)
        except sd.CallbackStop:
            return np.concatenate(out)
        if out or block.any():  # (silence while the buffer refills after a seek isn't the music)
            out.append(block)
    raise AssertionError("never reached the end")


def session_for(paths, advances):
    follow = {i: (i + 1, paths[i + 1]) for i in range(len(paths) - 1)}.get
    s = _Session(paths[0], lambda path: 1.0, lambda s: None, index=0, follow=follow,
                 on_advance=lambda s, previous, new: advances.append((previous.path, new.path)))
    s.start()
    return s


def test_two_tracks_join_with_nothing_in_between(tmp_path, fake_card):
    (a, da), (b, db) = wav(tmp_path, "a.wav", 10000), wav(tmp_path, "b.wav", 7000)  # neither a whole block
    advances = []
    s = session_for([a, b], advances)
    got = pull(s)
    want = np.concatenate([da, db])
    assert np.array_equal(got[: len(want)], want)  # every sample, in order, no gap
    assert not got[len(want):].any()                # then silence to the end of that block
    assert advances == [(a, b)]
    proof = s._src.sent  # B's own proof: its first sample came out first
    with sf.SoundFile(str(b)) as ref:
        proof.check(ref)
    assert proof.summary().complete and s._src.path == b
    s.close()


def test_a_sample_rate_change_ends_the_chain_and_says_why(tmp_path, fake_card):
    (a, da), (b, _) = wav(tmp_path, "a.wav", 10000), wav(tmp_path, "b.wav", 7000, rate=96000)
    advances = []
    s = session_for([a, b], advances)
    got = pull(s)
    assert np.array_equal(got[: len(da)], da) and advances == []
    assert "sample rate changes" in s.handoff and "96" in s.handoff
    s.close()


def test_seeking_back_while_the_next_track_is_read_ahead(tmp_path, fake_card):
    (a, da), (b, db) = wav(tmp_path, "a.wav", 30000), wav(tmp_path, "b.wav", 9000)
    advances = []
    s = session_for([a, b], advances)
    deadline = time.monotonic() + 5
    while s._reading.path != b and time.monotonic() < deadline:  # the decoder has gone on into B
        time.sleep(0.005)
    assert s._reading.path == b
    s.seek(20000 / 44100)
    got = pull(s)
    want = np.concatenate([da[20000:], db])
    assert np.array_equal(got[: len(want)], want) and advances == [(a, b)]
    s.close()


def test_the_player_moves_on_when_a_track_joins(tmp_path):
    p = Player()
    tracks = [Track(Path(f"{i}.flac"), str(i)) for i in range(3)]
    p.set_tracks(tracks)
    assert p._follow(0) == (1, tracks[1].path) and p._follow(2) is None
    p.set_repeat("one")
    assert p._follow(1) == (1, tracks[1].path)  # repeat one: the same song again, gaplessly
    fake = SimpleNamespace(retire=lambda source: None)
    p._joined(fake, SimpleNamespace(), SimpleNamespace(index=1, path=tracks[1].path))
    assert p.index == 1 and p.transition == "gapless"
    p.set_tracks(list(reversed(tracks)), keep_playing=False)
    moved = SimpleNamespace(index=1, path=tracks[0].path)  # the list changed while it was lined up
    p._joined(fake, SimpleNamespace(), moved)
    assert p.index == 2 and moved.index == 2


def test_clipping_guard_can_be_set_for_the_next_track_too():
    p = Player()
    a, b = Track(Path("a.flac"), "a"), Track(Path("b.flac"), "b")
    p.set_tracks([a, b])
    p.set_volume(1.0)
    p.set_trim(a.path, 0.9)
    p.set_trim(b.path, 0.8)
    assert p._gain(a.path) == pytest.approx(0.9) and p._gain(b.path) == pytest.approx(0.8)
    p.set_trim(None, 1.0)
    assert p._gain(b.path) == 1.0
