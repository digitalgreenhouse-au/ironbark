"""Playback when things go wrong, and bit-perfect mode with every kind of file: checked through the
stand-in sound card (as test_gapless.py) — no sound device is ever opened.

- Float and lossy files in bit-perfect mode: decoded as floats and converted once, the same way for
  playback, its proof and the Check (float WAVs used to go out as silence, Vorbis/Opus overs as
  full-scale spikes of the opposite sign).
- The decoder thread owns its files: closing a session never closes one under a read.
- Damaged, truncated, empty and multichannel files, a lost device, a missing device in bit-perfect
  mode: the player says what happened and never sits at "playing" in silence.
- The track lined up to follow is lined up again when the list, Up next or repeat changes.
"""
import struct
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import mp4_maker
import numpy as np
import pytest
import sounddevice as sd
import soundfile as sf
from test_gapless import FakeStream, pull, wav  # noqa: F401 (the stand-in sound card)

from ironbark import analysis, audiofile, devices, engine, m4a, mp3frames, statustext
from ironbark import session as session_mod  # (tests name their own `session`)
from ironbark.engine import BLOCK_FRAMES, Player, State, _Session
from ironbark.library import Track
from ironbark.proof import SentRecord, apply_gain
from ironbark.tools import audition

RATE = 44100
RNG = np.random.default_rng(11)
TIME_INFO = SimpleNamespace(outputBufferDacTime=0.0, currentTime=0.0)
OK = SimpleNamespace(output_underflow=False)
INT32_MAX = 2**31 - 1


@pytest.fixture
def fake_card(monkeypatch):
    monkeypatch.setattr(engine.sd, "OutputStream", FakeStream)
    monkeypatch.setattr(engine.devices, "find_device", lambda name, rescan_ok=True: None)
    monkeypatch.setattr(engine.devices, "mixer_rate", lambda index: None)
    monkeypatch.setattr(engine.devices, "max_output_channels", lambda index: None, raising=False)


@pytest.fixture
def exclusive_card(monkeypatch, fake_card):
    """A stand-in for a device that takes bit-perfect (exclusive) mode — the system default, with a
    volume control of its own."""
    monkeypatch.setattr(engine.devices, "find_device", lambda name, rescan_ok=True: 0)
    monkeypatch.setattr(engine.devices, "exclusive_supported", lambda: True)
    monkeypatch.setattr(engine.devices, "default_output_name", lambda: "Test DAC")
    monkeypatch.setattr(engine.devices, "has_hardware_volume", lambda name: True)
    monkeypatch.setattr(engine.sd, "RawOutputStream", FakeStream)   # (packed 24-bit: only if asked)


def sine(seconds, amplitude, rate=RATE, channels=2, freq=997.0):
    t = np.arange(int(seconds * rate)) / rate
    x = amplitude * np.sin(2 * np.pi * freq * t)
    fade = rate // 50   # (a sudden full-scale start rings past the tone's height)
    x[:fade] *= np.linspace(0.0, 1.0, fade)
    return np.repeat(x[:, None], channels, axis=1)


def write_chunked(path, data, rate, **kwargs):
    """libsndfile's Vorbis encoder can overflow its stack given a long stretch in one call."""
    with sf.SoundFile(path, "w", rate, data.shape[1], **kwargs) as f:
        for k in range(0, len(data), 4096):
            f.write(data[k:k + 4096])
    return path


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


def pump(session, dtype="float32", channels=2, limit=600):
    """Ask for blocks the way the sound card does until the chain ends — then PortAudio's
    finished-callback runs, as it would. Returns the music sent (from its first sound)."""
    out = []
    for _ in range(limit):
        deadline = time.monotonic() + 2
        while ((session._seek_request is not None or (session._queue.qsize() < 2 and not session._draining))
               and time.monotonic() < deadline):
            time.sleep(0.002)
        block = np.zeros((BLOCK_FRAMES, channels), dtype)
        try:
            session._callback(block, BLOCK_FRAMES, TIME_INFO, OK)
        except sd.CallbackStop:
            session._finished()
            return np.concatenate(out) if out else np.zeros((0, channels), dtype)
        if out or block.any():
            out.append(block)
    raise AssertionError("never reached the end")


def noise_flac(path, seconds):
    data = (RNG.standard_normal((int(RATE * seconds), 2)) * 3000).astype(np.int16)
    sf.write(path, data, RATE, subtype="PCM_16")
    return path


# -- float and lossy files in bit-perfect mode (one int32 conversion for everyone) -------------------
FLOAT_FILES = [("float.wav", dict(subtype="FLOAT")), ("double.wav", dict(subtype="DOUBLE")),
               ("float.aiff", dict(format="AIFF", subtype="FLOAT"))]


@pytest.mark.parametrize("name, kind", FLOAT_FILES + [("hot.ogg", dict(format="OGG", subtype="VORBIS"))])
def test_float_and_lossy_files_play_at_their_level_in_bit_perfect_mode(tmp_path, exclusive_card, name, kind):
    path = write_chunked(tmp_path / name, sine(0.6, 1.3 if name == "hot.ogg" else 0.5), RATE, **kind)
    s = _Session(path, lambda p: 1.0, lambda s: None, want_exclusive=True)
    assert s.exclusive and s.dtype == "int32"
    s.start()
    got = pump(s, "int32")
    with audiofile.open(path) as f:
        want = audiofile.to_int32(f.read(dtype="float64", always_2d=True))
    assert np.array_equal(got[:len(want)], want)            # what the file holds, at its own level
    if name != "hot.ogg":
        assert abs(int(got.max()) - 2**30) < 2**30 // 100     # 0.5 of full scale, not silence
    else:
        assert not (got[:len(want)][want > 2**30] < 0).any()  # overs held at full scale, never wrapped
    record = s._src.sent
    with audiofile.open(path) as reference:                   # the proof decodes the very same way
        record.check(reference)
    assert record.summary().complete and record.summary().mismatch_at is None
    s.close()


@pytest.mark.parametrize("subtype, rate", [("VORBIS", 44100), ("OPUS", 48000)])
def test_lossy_overs_are_held_at_full_scale_not_wrapped(tmp_path, subtype, rate):
    path = write_chunked(tmp_path / f"hot_{subtype}.ogg", sine(1.0, 1.5, rate), rate, format="OGG", subtype=subtype)
    with audiofile.open(path) as f:
        decoded = f.read(dtype="float64", always_2d=True)
    with audiofile.open(path) as f:
        sent = np.concatenate(list(audiofile.blocks(f, BLOCK_FRAMES, "int32", True)))
    over = decoded > 1.0
    assert over.any()                                   # (the encoder kept the overs)
    assert (sent[over] == INT32_MAX).all()
    assert not (sent[decoded > 0.5] < 0).any()          # nothing wrapped round to negative full scale
    assert np.array_equal(sent, audiofile.to_int32(decoded))


@pytest.mark.parametrize("name, kind", FLOAT_FILES)
def test_the_check_measures_float_files(tmp_path, name, kind):
    quiet = write_chunked(tmp_path / f"quiet_{name}", sine(2.0, 10 ** (-6 / 20)), RATE, **kind)
    result = analysis.analyse(quiet, 32, 0)
    assert result.peak_dbfs == pytest.approx(-6.0, abs=0.05)   # not silence
    assert result.lufs is not None and result.dr is not None and result.float_samples
    loud = write_chunked(tmp_path / f"loud_{name}", sine(2.0, 10 ** (3 / 20)), RATE, **kind)
    result = analysis.analyse(loud, 32, 0)
    assert result.peak_dbfs == pytest.approx(3.0, abs=0.05)    # overs above full scale, measured as they are
    assert result.clip_runs > 0 and result.true_peak > 1.4


# -- the decoder owns its files ------------------------------------------------------------------
class HeldFile:
    """A file whose reads wait at a gate (a sleeping drive, or a long Vorbis seek)."""

    def __init__(self, inner):
        self.inner, self.closed = inner, False
        self.reading, self.gate = threading.Event(), threading.Event()

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def read(self, frames=-1, dtype="float64", always_2d=False):
        self.reading.set()
        self.gate.wait(5)
        assert not self.closed, "closed under a read"
        return self.inner.read(frames, dtype=dtype, always_2d=always_2d)

    def close(self):
        self.closed = True
        self.inner.close()


def test_closing_never_closes_a_file_under_a_read(tmp_path, fake_card):
    path, _ = wav(tmp_path, "a.wav", 40000)
    s = _Session(path, lambda p: 1.0, lambda s: None)
    held = s._src.file = HeldFile(s._src.file)
    s.start()
    assert held.reading.wait(2)
    started = time.monotonic()
    s.close()
    assert time.monotonic() - started < 1.0            # nothing waits long on the window's thread
    assert not held.closed                             # the read is still going: its file stays open
    held.gate.set()
    s._decoder.join(2)
    assert not s._decoder.is_alive() and held.closed   # closed by the decoder itself, once it let go


def test_a_file_opened_ahead_as_the_session_closes_is_closed_too(tmp_path, fake_card, monkeypatch):
    (a, _), (b, _) = wav(tmp_path, "a.wav", 5000), wav(tmp_path, "b.wav", 5000)
    opened = []

    class Recorded(engine._Source):
        def __init__(self, *args):
            super().__init__(*args)
            opened.append(self)

    monkeypatch.setattr(session_mod, "_Source", Recorded)
    asked, go_on = threading.Event(), threading.Event()

    def follow(index):   # slow to answer: the session is closed meanwhile
        asked.set()
        go_on.wait(5)
        return 1, b

    s = _Session(a, lambda p: 1.0, lambda s: None, index=0, follow=follow)
    s.start()
    assert asked.wait(5)
    s.close()
    go_on.set()
    s._decoder.join(2)
    assert not s._decoder.is_alive()
    assert len(opened) == 2 and all(source.file.closed for source in opened)


def test_a_long_vorbis_seek_gives_way(tmp_path):
    path = write_chunked(tmp_path / "long.ogg", sine(4.0, 0.3), RATE, format="OGG", subtype="VORBIS")
    with audiofile.open(path) as f:
        asked = []
        audiofile.set_cancel(f, lambda: bool(asked) or asked.append(1))   # gives way after the first chunk
        assert f.seek(3 * RATE) == audiofile.SEEK_CHUNK == f.tell()
        audiofile.set_cancel(f, lambda: False)
        assert f.seek(3 * RATE) == 3 * RATE                              # left alone: lands exactly
        block = f.read(1000, dtype="float32", always_2d=True)
    with audiofile.open(path) as f:
        whole = f.read(dtype="float32", always_2d=True)
    assert np.array_equal(block, whole[3 * RATE: 3 * RATE + 1000])


# -- damaged and odd files ----------------------------------------------------------------------
def test_a_seek_past_the_end_of_a_truncated_flac_moves_on(tmp_path, fake_card):
    good = noise_flac(tmp_path / "good.flac", 10)
    cut = tmp_path / "cut.flac"
    cut.write_bytes(good.read_bytes()[: int(good.stat().st_size * 0.4)])   # STREAMINFO still says 10 s
    other, _ = wav(tmp_path, "other.wav", 40000)
    p = Player()
    p.set_tracks([Track(cut, "cut"), Track(other, "other")])
    p.play(0, start_at=8.0)
    pump(p._session)                                  # the file ends where it can't be read
    assert wait_for(lambda: p.status().index == 1 and p.status().state is State.PLAYING)
    assert any(e.kind == "damaged" and e.track == "cut.flac" for e in p.status().events)
    p.close()


def test_a_damaged_stretch_is_skipped_and_reported(tmp_path, fake_card):
    path = noise_flac(tmp_path / "damaged.flac", 10)
    raw = bytearray(path.read_bytes())
    middle = len(raw) // 2
    raw[middle: middle + 4096] = RNG.integers(0, 256, 4096, dtype=np.uint8).tobytes()
    path.write_bytes(bytes(raw))
    events = []
    s = _Session(path, lambda p: 1.0, lambda s: None, events=events)
    s.start()
    got = pump(s)
    damage = [e for e in events if e.kind == "damaged"]
    assert damage and "skipped" in damage[0].cause and 3 < damage[0].position < 7
    assert len(got) > 0.8 * 10 * RATE                 # the rest of the song still played
    assert "damaged" in s.message
    assert s._src.sent.summary().mismatch_at is None
    s.close()


def damaged_m4a(tmp_path, name, what):
    path = mp4_maker.write_m4a(tmp_path / name, (RNG.standard_normal((20000, 2)) * 3000).astype(np.int16),
                               moov_first=True)
    data = bytearray(path.read_bytes())
    if what == "frame length":
        cookie = data.find(b"alac", data.find(b"alac") + 4)  # the sample entry, then its setup block
        data[cookie + 8: cookie + 12] = b"\xff\xff\xff\xff"
    else:                                                    # the packet count
        at = data.find(b"stsz")
        data[at + 12: at + 16] = struct.pack(">I", 0x7FFFFFFF)
    path.write_bytes(bytes(data))
    return path


@pytest.mark.parametrize("what", ["frame length", "packet count"])
def test_a_damaged_m4a_is_unsupported_not_a_crash(tmp_path, what):
    path = damaged_m4a(tmp_path, "bad.m4a", what)
    with pytest.raises(m4a.NotAlac):
        m4a.read_index(path)
    with pytest.raises(audiofile.Unsupported):
        audiofile.open(path)


def test_an_album_carries_on_past_a_damaged_m4a(tmp_path, fake_card):
    (a, _), (c, _) = wav(tmp_path, "a.wav", 8000), wav(tmp_path, "c.wav", 40000)
    bad = damaged_m4a(tmp_path, "bad.m4a", "frame length")
    p = Player()
    p.set_tracks([Track(a, "a"), Track(bad, "bad"), Track(c, "c")])
    p.play(0)
    pump(p._session)                                  # a, then the chain ends: the next can't be read
    assert wait_for(lambda: p.status().index == 2 and p.status().state is State.PLAYING)
    assert "Skipped bad.m4a" in p.status().message
    p.close()


def test_a_multichannel_file_on_a_stereo_output_is_skipped(tmp_path, fake_card, monkeypatch):
    monkeypatch.setattr(engine.devices, "max_output_channels", lambda index: 2)
    six = tmp_path / "six.wav"
    sf.write(six, np.zeros((4000, 6), np.int16), RATE, subtype="PCM_16")
    two, _ = wav(tmp_path, "two.wav", 40000)
    p = Player()
    p.set_tracks([Track(six, "six"), Track(two, "two")])
    p.play(0)
    status = p.status()
    assert status.index == 1 and status.state is State.PLAYING
    assert "Skipped six.wav" in status.message and "6 channels" in status.message
    p.close()


def test_repeat_one_on_a_file_with_no_music_stops(tmp_path, fake_card, monkeypatch):
    empty = tmp_path / "empty.wav"
    sf.write(empty, np.zeros((0, 2), np.int16), RATE, subtype="PCM_16")
    opened = []

    class Counted(engine._Source):
        def __init__(self, *args):
            super().__init__(*args)
            opened.append(self)

    monkeypatch.setattr(session_mod, "_Source", Counted)
    p = Player()
    p.set_tracks([Track(empty, "empty")])
    p.set_repeat("one")
    p.play(0)
    session = p._session
    time.sleep(0.3)                                    # (the decoder used to open the file again and again)
    assert len(opened) == 1 and "no music" in session.handoff
    pump(session)
    assert wait_for(lambda: p.status().state is State.STOPPED)
    assert "has no music" in p.status().message and len(opened) == 1
    p.close()


def test_mp3_with_no_length_header_is_reported(tmp_path, fake_card):
    """libsndfile estimates such a file's length from its first frame and won't decode past that:
    a VBR one that starts loud and goes quiet stops early. Made here by cutting the Xing frame out
    of a VBR file libsndfile wrote."""
    full = tmp_path / "vbr.mp3"
    signal = np.concatenate([RNG.standard_normal((RATE * 2, 2)) * 0.3, np.zeros((RATE * 10, 2))])
    write_chunked(full, signal, RATE, format="MP3", subtype="MPEG_LAYER_III", bitrate_mode="VARIABLE",
                  compression_level=0.0)
    data = full.read_bytes()
    first = mp3frames._frame(data, 0, len(data))
    assert first is not None and b"Xing" in data[:64]
    bare = tmp_path / "bare.mp3"
    bare.write_bytes(data[first[2]:])
    with audiofile.open(full) as f:
        assert audiofile.mp3_shortfall(f, full) is None
    with audiofile.open(bare) as f:
        short = audiofile.mp3_shortfall(f, bare)
    assert short is not None and short[1] > 2 * short[0]
    events = []
    s = _Session(bare, lambda p: 1.0, lambda s: None, events=events)
    s.start()
    assert wait_for(lambda: "no length header" in s.message)
    assert any(e.kind == "cut short" for e in events)
    s.close()


# -- the device ---------------------------------------------------------------------------------
def test_a_lost_device_leaves_the_player_paused_at_the_spot(tmp_path, fake_card):
    path, _ = wav(tmp_path, "t.wav", RATE * 10)
    p = Player()
    p.set_tracks([Track(path, "t", duration=10.0)])
    p.play(0, start_at=4.0)
    session = p._session
    session._finished()                               # the stream stopped by itself: the device went
    assert wait_for(lambda: p.status().state is State.PAUSED)
    status = p.status()
    assert p._session is None and "stopped playing" in status.message
    assert status.position == pytest.approx(4.0, abs=0.2)
    p.play()                                          # carries on from the same spot
    assert p._session is not None and p.status().position == pytest.approx(4.0, abs=0.2)
    p.close()


def test_a_device_that_stops_asking_for_audio_is_noticed(tmp_path, fake_card, monkeypatch):
    monkeypatch.setattr(session_mod, "STALL_SECONDS", 0.3)
    path, _ = wav(tmp_path, "t.wav", RATE * 10)
    p = Player()
    p.set_tracks([Track(path, "t", duration=10.0)])
    p.play(0)                                         # the stand-in card never asks for anything
    assert wait_for(lambda: p.status().state is State.PAUSED, timeout=4)
    assert "stopped playing" in p.status().message
    p.close()


def test_a_device_that_wont_start_again_stays_paused(tmp_path, fake_card, monkeypatch):
    class Stubborn(FakeStream):
        refuse = False

        def start(self):
            if Stubborn.refuse:
                raise sd.PortAudioError("Device unavailable")

    monkeypatch.setattr(engine.sd, "OutputStream", Stubborn)
    path, _ = wav(tmp_path, "t.wav", RATE * 10)
    p = Player()
    p.set_tracks([Track(path, "t", duration=10.0)])
    p.play(0)
    p.pause()
    Stubborn.refuse = True
    p.play()
    status = p.status()
    assert status.state is State.PAUSED and "wouldn't start again" in status.message
    p.close()


def test_bit_perfect_never_moves_to_another_device(tmp_path, fake_card, monkeypatch):
    made = []

    class Counted(FakeStream):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            made.append(kwargs)

    monkeypatch.setattr(engine.sd, "OutputStream", Counted)
    monkeypatch.setattr(engine.devices, "find_device", lambda name, rescan_ok=True: None if name else 5)
    monkeypatch.setattr(engine.devices, "exclusive_supported", lambda: True)
    path, _ = wav(tmp_path, "t.wav", RATE * 10)
    p = Player()
    p.set_tracks([Track(path, "t", duration=10.0)])
    p.set_exclusive(True)
    p.set_output_device("DAC")
    p.play(0, start_at=2.0)
    status = p.status()
    assert not made and p._session is None                      # nothing opened anywhere
    assert status.state is State.PAUSED and status.position == pytest.approx(2.0)
    assert "DAC isn't connected" in status.message and "Bit-perfect" in status.message
    p.set_exclusive(False)                                      # shared mode: Windows' volume still works
    p.play()
    assert made and made[-1]["device"] == 5 and made[-1]["dtype"] == "float32"
    assert "playing on the system default" in p.status().message
    p.close()


def test_audition_in_bit_perfect_mode_never_moves_to_another_device(monkeypatch):
    made = []
    monkeypatch.setattr(audition.devices, "find_device", lambda name, rescan_ok=True: None if name else 5)
    monkeypatch.setattr(audition.devices, "exclusive_supported", lambda: True)
    switcher = audition.Switcher([np.zeros((8000, 2), np.float32)], RATE)
    a = audition.Audition(switcher, device="DAC", exclusive=True, stream_factory=lambda **kw: made.append(kw))
    with pytest.raises(devices.DeviceMissing):
        a.play(0)
    assert not made and not a.playing


@pytest.fixture
def changing_default(monkeypatch):
    """Windows' default output, which the test changes: "Speakers" (with a volume control of its own)
    until a "USB DAC" (with none) is plugged in and made the default."""
    default = {"name": "Speakers"}
    places, volumes = {"Speakers": 1, "USB DAC": 2}, {"Speakers": True, "USB DAC": False}
    monkeypatch.setattr(devices, "find_device", lambda name, rescan_ok=True: places.get(name or default["name"]))
    monkeypatch.setattr(devices, "default_output_name", lambda: default["name"])
    monkeypatch.setattr(devices, "has_hardware_volume", lambda name: volumes[name or default["name"]])
    monkeypatch.setattr(devices, "exclusive_supported", lambda: True)
    monkeypatch.setattr(devices, "max_output_channels", lambda index: 2)
    return default


def test_bit_perfect_on_the_system_default_never_follows_it_to_an_unchecked_device(tmp_path, fake_card,
                                                                                    changing_default, monkeypatch):
    made = []

    class Counted(FakeStream):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            made.append(kwargs)

    monkeypatch.setattr(engine.sd, "OutputStream", Counted)
    (one, _), (two, _) = wav(tmp_path, "1.wav", RATE * 5), wav(tmp_path, "2.wav", RATE * 5)
    p = Player()
    p.set_tracks([Track(one, "1", duration=5.0), Track(two, "2", duration=5.0)])
    assert p.refresh_hardware_volume() is True                  # the window's check, as bit-perfect goes on
    p.set_exclusive(True)
    p.set_volume(1.0)                                           # (100%: as bit-perfect is meant to be heard)
    p.play(0)
    assert made[-1]["device"] == 1 and made[-1]["dtype"] == "int32"
    changing_default["name"] = "USB DAC"                        # plugged in: Windows makes it the default
    p.next()                                                    # a new stream (any song that can't join on)
    status = p.status()
    assert len(made) == 1 and p._session is None                # nothing opened on the new default
    assert status.state is State.PAUSED and status.index == 1 and status.position == 0.0
    assert "USB DAC" in status.message and "check its volume" in status.message
    assert p.volume_check_due and p.hardware_volume is None     # (the ear checks treat it as having none)
    p.play()
    assert len(made) == 1 and p.status().state is State.PAUSED    # not until it has been checked
    assert p.refresh_hardware_volume() is False and not p.volume_check_due   # (the window does this at once)
    p.play()
    assert made[-1]["device"] == 2 and p.status().state is State.PLAYING
    p.close()


def test_bit_perfect_on_the_system_default_needs_its_volume_checked_first(tmp_path, fake_card, changing_default):
    path, _ = wav(tmp_path, "t.wav", RATE * 5)
    p = Player()
    p.set_tracks([Track(path, "t", duration=5.0)])
    p.set_exclusive(True)                                       # (never checked)
    p.play(0)
    assert p._session is None and p.volume_check_due
    p.refresh_hardware_volume()
    p.play()
    assert p._session is not None
    p.close()


def test_audition_in_bit_perfect_mode_on_the_system_default_asks_the_player_first(changing_default):
    made = []
    p = Player()
    p.refresh_hardware_volume()
    p.set_exclusive(True)
    switcher = audition.Switcher([np.zeros((8000, 2), np.float32)], RATE)
    a = audition.Audition(switcher, device=None, exclusive=True, default_check=p.check_default,
                          stream_factory=lambda **kw: made.append(kw) or FakeStream(**kw))
    a.play(0)
    assert made[-1]["device"] == 1 and a.exclusive
    a.stop()
    changing_default["name"] = "USB DAC"
    with pytest.raises(devices.DefaultChanged):
        a.play(0)
    assert len(made) == 1 and not a.playing and p.volume_check_due
    p.close()


def test_open_streams_are_known_so_a_rescan_waits(tmp_path, fake_card):
    path, _ = wav(tmp_path, "t.wav", RATE * 5)
    p = Player()
    p.set_tracks([Track(path, "t", duration=5.0)])
    p.play(0)
    stream = p._session._stream
    assert stream in devices._streams and devices.streams_open()
    p.stop()
    assert stream not in devices._streams
    p.close()


@pytest.mark.skipif(not devices.IS_WINDOWS, reason="Windows Core Audio")
def test_the_device_list_asks_only_for_connected_outputs_names(monkeypatch):
    asked = []

    class Value:
        def __init__(self, value):
            self.value = value

        def GetValue(self):
            return self.value

        def clear(self):
            pass

    class Endpoint:
        def __init__(self, name):
            self.name = name

        def OpenPropertyStore(self, access):
            endpoint = self

            class Store:
                def GetValue(self, key):
                    asked.append(key.pid)
                    return Value(endpoint.name)
            return Store()

    class Enumerator:
        def EnumAudioEndpoints(self, flow, state):
            asked.append((flow, state))
            endpoints = [Endpoint("DAC"), Endpoint("Speakers")]
            return SimpleNamespace(GetCount=lambda: len(endpoints), Item=lambda k: endpoints[k])

    monkeypatch.setattr(devices, "_enumerator", Enumerator)
    assert devices.output_names() == ["DAC", "Speakers"]
    assert asked == [(0, 1), 14, 14]   # active output endpoints (not every endpoint), and only their names


def test_a_device_taking_fewer_bits_is_reported(tmp_path, exclusive_card, monkeypatch):
    """PortAudio converts to the nearest format the device takes in exclusive mode (unless told
    not to), so a 24-bit track on a 16-bit-only device isn't bit-perfect, however exact the player is."""
    monkeypatch.setattr(engine.devices, "exclusive_bits", lambda stream: 16)
    deep = tmp_path / "deep.wav"
    sf.write(deep, (RNG.standard_normal((20000, 2)) * 0.1), RATE, subtype="PCM_24")
    shallow, _ = wav(tmp_path, "shallow.wav", 20000)
    p = Player()
    p.refresh_hardware_volume()                       # (the window's check, as bit-perfect goes on)
    p.set_exclusive(True)
    p.set_tracks([Track(deep, "deep"), Track(shallow, "shallow")])
    p.play(0)
    status = p.status()
    assert status.output.device_bits == 16
    assert "16-bit samples" in status.message and "24-bit" in status.message and "not bit-perfect" in status.message
    assert p._session.stream_format == "int32"       # packed 24-bit was tried, and got no more bits
    p.play(1)
    assert p.status().message == ""                   # a 16-bit track loses nothing
    p.close()


class Uac1Card(FakeStream):
    """A stand-in for a USB Audio Class 1 device such as the CMedia CM6542: in exclusive mode it
    refuses 32-bit samples (PortAudio falls back to 16-bit) but takes packed 24-bit ones."""
    takes = {"int32": 16, "int24": 24}
    refuse = ()
    opened: list = []

    def __init__(self, **kwargs):
        if kwargs["dtype"] in self.refuse:
            raise sd.PortAudioError("Invalid sample format")
        super().__init__(**kwargs)
        self.dtype, self.closed = kwargs["dtype"], False
        Uac1Card.opened.append(self)

    def close(self):
        self.closed = True


@pytest.fixture
def uac1_card(monkeypatch, exclusive_card):
    Uac1Card.opened, Uac1Card.refuse = [], ()
    monkeypatch.setattr(engine.sd, "OutputStream", Uac1Card)
    monkeypatch.setattr(engine.sd, "RawOutputStream", Uac1Card)
    monkeypatch.setattr(engine.devices, "exclusive_bits", lambda stream: Uac1Card.takes[stream.dtype])
    return Uac1Card


def pump_packed(session, limit=600):
    """`pump` for a packed 24-bit stream: raw 3-byte buffers through the stream's own callback,
    turned back into left-justified int32 samples."""
    out = []
    for _ in range(limit):
        deadline = time.monotonic() + 2
        while ((session._seek_request is not None or (session._queue.qsize() < 2 and not session._draining))
               and time.monotonic() < deadline):
            time.sleep(0.002)
        raw = bytearray(BLOCK_FRAMES * session.channels * 3)
        try:
            session._stream.callback(raw, BLOCK_FRAMES, TIME_INFO, OK)
            stopped = False
        except sd.CallbackStop:
            stopped = True
        wide = np.zeros((BLOCK_FRAMES * session.channels, 4), np.uint8)
        wide[:, 1:] = np.frombuffer(bytes(raw), np.uint8).reshape(-1, 3)
        block = wide.view("<i4").reshape(BLOCK_FRAMES, session.channels)
        if out or block.any():
            out.append(block)
        if stopped:
            session._finished()
            return np.concatenate(out)
    raise AssertionError("never reached the end")


def test_pack_int24_sends_each_samples_top_three_bytes():
    samples = np.array([[0x12345600, -256], [2**31 - 256, -2**31]], np.int32)
    raw = bytearray(12)
    session_mod.pack_int24(samples, raw)
    assert bytes(raw) == bytes([0x56, 0x34, 0x12, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x7F, 0x00, 0x00, 0x80])


def test_a_device_that_takes_only_packed_24_bit_gets_24_bit_tracks_whole(tmp_path, uac1_card):
    """A CM6542-based USB device: asked for int32 it got 16-bit samples, so 24-bit tracks lost their
    lowest 8 bits; asked for packed 24-bit it takes all 24."""
    deep = tmp_path / "deep.flac"
    sf.write(deep, RNG.standard_normal((30000, 2)) * 0.1, RATE, subtype="PCM_24")
    s = _Session(deep, lambda p: 1.0, lambda s: None, want_exclusive=True)
    assert s.exclusive and s.stream_format == "int24" and s.dtype == "int32"
    assert s.device_bits == 24 and s.message == ""
    assert isinstance(s._stream, Uac1Card) and s._stream.dtype == "int24" and not s._stream.closed
    assert [x.closed for x in uac1_card.opened] == [True, False]    # int32 was tried first, and let go
    s.start()
    got = pump_packed(s)
    with audiofile.open(deep) as f:
        want = audiofile.read(f, -1, "int32")
    assert np.array_equal(got[:len(want)], want)                    # every bit of every sample
    record = s._src.sent
    with audiofile.open(deep) as reference:
        record.check(reference)
    assert record.summary().complete and record.summary().mismatch_at is None and record.summary().untouched
    s.close()


def test_packed_24_bit_through_the_player_says_bit_perfect(tmp_path, uac1_card):
    deep = tmp_path / "deep.wav"
    sf.write(deep, RNG.standard_normal((20000, 2)) * 0.1, RATE, subtype="PCM_24")
    shallow, _ = wav(tmp_path, "shallow.wav", 20000)
    p = Player()
    p.refresh_hardware_volume()                       # (the window's check, as bit-perfect goes on)
    p.set_exclusive(True)
    p.set_tracks([Track(deep, "deep"), Track(shallow, "shallow")])
    for index, bits in ((0, 24), (1, 16)):                         # a 16-bit track is whole in 24 bits too
        p.play(index)
        status = p.status()
        assert status.output.device_bits == 24 and status.message == ""
        text, tone = statustext.output_line(status.output, 1.0, bits=bits, show_bits=True)
        assert "44.1 kHz 24-bit exclusive" in text and "bit-perfect ✓" in text and tone == "good"
    p.close()


def test_a_32_bit_track_on_a_packed_24_bit_device_is_flagged(tmp_path, uac1_card):
    deepest = tmp_path / "deepest.wav"
    sf.write(deepest, RNG.standard_normal((20000, 2)) * 0.1, RATE, subtype="PCM_32")
    s = _Session(deepest, lambda p: 1.0, lambda s: None, want_exclusive=True)
    assert s.stream_format == "int24" and "24-bit samples" in s.message and "32-bit" in s.message
    s.close()


@pytest.mark.parametrize("refuse, takes", [(("int24",), {"int32": 16, "int24": 24}),
                                           ((), {"int32": 16, "int24": 16})])
def test_without_packed_24_bit_the_stream_stays_int32(tmp_path, uac1_card, refuse, takes, monkeypatch):
    """Packed 24-bit refused, or no better: int32 is opened again (PortAudio's 16-bit), and a 24-bit
    track still says it isn't bit-perfect."""
    monkeypatch.setattr(uac1_card, "refuse", refuse)
    monkeypatch.setattr(uac1_card, "takes", takes)
    deep = tmp_path / "deep.wav"
    sf.write(deep, RNG.standard_normal((20000, 2)) * 0.1, RATE, subtype="PCM_24")
    s = _Session(deep, lambda p: 1.0, lambda s: None, want_exclusive=True)
    assert s.exclusive and s.stream_format == "int32" and s._stream.dtype == "int32" and not s._stream.closed
    assert s.device_bits == 16 and "16-bit samples" in s.message
    assert sum(not x.closed for x in uac1_card.opened) == 1          # only the stream in use is still open
    s.close()


def test_a_device_taking_int32_whole_is_never_asked_for_packed_24_bit(tmp_path, uac1_card, monkeypatch):
    monkeypatch.setattr(uac1_card, "takes", {"int32": 32, "int24": 24})
    path, _ = wav(tmp_path, "t.wav", 20000)
    s = _Session(path, lambda p: 1.0, lambda s: None, want_exclusive=True)
    assert s.stream_format == "int32" and [x.dtype for x in uac1_card.opened] == ["int32"]
    s.close()


# -- the buffer and the proof -------------------------------------------------------------------
def test_the_buffer_holds_seconds_of_music_at_any_rate(tmp_path, fake_card):
    for rate, blocks in ((44100, engine.QUEUE_BLOCKS), (192000, None)):
        path = tmp_path / f"r{rate}.wav"
        sf.write(path, np.zeros((rate // 10, 2), np.int16), rate, subtype="PCM_16")
        s = _Session(path, lambda p: 1.0, lambda s: None)
        seconds = s._queue_blocks * BLOCK_FRAMES / rate
        assert seconds >= engine.BUFFER_SECONDS and s._queue.maxsize == s._queue_blocks
        if blocks is not None:
            assert s._queue_blocks == blocks
        s.close()


def test_the_proof_summary_costs_the_same_however_long_the_track(tmp_path):
    path = noise_flac(tmp_path / "t.flac", 1)
    record = SentRecord(path, RATE, "int32", BLOCK_FRAMES)
    with sf.SoundFile(str(path)) as f:
        for k, gain in enumerate([1.0, 0.5, 1.0, 1.0]):
            record.add(1, k * BLOCK_FRAMES, apply_gain(f.read(BLOCK_FRAMES, dtype="int32", always_2d=True), gain), gain)
    with sf.SoundFile(str(path)) as reference:
        record.check(reference)

    class NoWalking(list):
        def __iter__(self):
            raise AssertionError("summary() walked every block")

    record.segments = NoWalking(record.segments)
    proof = record.summary()
    assert proof.sent == pytest.approx(4 * BLOCK_FRAMES / RATE) and proof.complete
    assert proof.min_gain == 0.5 and proof.changed_from == pytest.approx(BLOCK_FRAMES / RATE)


# -- what's lined up next -----------------------------------------------------------------------
def test_a_list_change_after_the_next_track_was_lined_up(tmp_path, fake_card):
    (a, da), (b, _), (c, dc), (d, dd) = (wav(tmp_path, f"{n}.wav", 30000) for n in "abcd")
    ta, tb, tc, td = (Track(x, x.stem) for x in (a, b, c, d))
    p = Player()
    p.set_volume(1.0)
    p.set_tracks([ta, tb, tc, td])
    p.play(0)
    s = p._session
    assert wait_for(lambda: s._src.next_source is not None)   # b is lined up (some of it queued)
    lined_up = s._src.next_source
    p.set_tracks([ta, tc, td], keep_playing=True)             # b taken out of the list
    block = np.zeros((BLOCK_FRAMES, 2), np.float32)
    s._callback(block, BLOCK_FRAMES, TIME_INFO, OK)           # (room in the buffer: the decoder looks again)
    assert wait_for(lambda: lined_up.dropped)
    got = np.concatenate([block, pump(s)])
    want = np.concatenate([da, dc, dd])                       # a, then c — b never heard, c not skipped
    assert np.array_equal(got[:len(want)], want)
    assert lined_up.file.closed                               # and its file let go of straight away
    p.close()


def test_play_next_in_the_last_seconds_still_joins_on(tmp_path, fake_card):
    (a, da), (q, dq) = wav(tmp_path, "a.wav", 20000), wav(tmp_path, "q.wav", 9000)
    p = Player()
    p.set_volume(1.0)
    p.set_tracks([Track(a, "a")])
    p.play(0)
    s = p._session
    assert wait_for(lambda: s._draining)                       # the end of the album is already queued
    p.play_next([Track(q, "q")])
    assert wait_for(lambda: s._src.next_source is not None)   # lined up after all
    got = pump(s)
    want = np.concatenate([da, dq])
    assert np.array_equal(got[:len(want)], want)               # straight on into it, gaplessly
    p.close()


def test_a_song_taken_out_of_the_list_while_lined_up_carries_on_from_where_the_list_was():
    p = Player()
    a, b, c, d = (Track(Path(f"{n}.flac"), n) for n in "abcd")
    p.set_tracks([a, b, c, d])
    p.index = 0
    p.set_tracks([a, c, d])
    gone = SimpleNamespace(index=1, path=b.path)               # b was lined up, and is heard anyway
    p._joined(SimpleNamespace(retire=lambda source: None), SimpleNamespace(), gone)
    assert p.index == 0 and gone.index == 0
    assert p._follow(gone.index) == (1, c.path)                # c next, not skipped


def test_up_next_changes_wait_for_the_player(tmp_path):
    p = Player()
    done = threading.Event()
    with p._lock:   # (the player's own thread holds it while a queued song starts)
        threading.Thread(target=lambda: (p.add_to_queue([Track(Path("q.flac"), "q")]), done.set())).start()
        assert not done.wait(0.2)
    assert done.wait(2) and len(p.up_next()) == 1


def test_the_player_thread_survives_an_error(monkeypatch):
    p = Player()
    closed = []
    fake = SimpleNamespace(close=lambda: closed.append(1), relineup=lambda: None, delivered=True, handoff="",
                           paused=False, stalled=lambda now: False)
    p._session = fake
    p._ended = lambda session: (_ for _ in ()).throw(ValueError("boom"))
    p._ended_events.put(("end", fake))
    assert wait_for(lambda: "went wrong" in p._message)
    del p._ended                                               # (back to the real one)
    p._ended_events.put(("end", fake))
    assert wait_for(lambda: p._message == "End of album") and closed


# -- a position handed back as seconds starts on its own frame -------------------------------------
def test_a_position_handed_back_as_seconds_starts_on_that_very_frame(tmp_path, fake_card):
    """A resume (after the network, a lost device, another tab) hands the session the position it
    left at, frames / rate as seconds. For about one position in twelve that float times the rate
    comes back a hair under the whole frame, and cutting it down started one frame early: the
    frame before the resume was heard twice."""
    path, ref = wav(tmp_path, "t.wav", RATE)
    early = [n for n in range(RATE // 4, RATE // 2) if int(n / RATE * RATE) != n][:3]
    assert len(early) == 3
    for n in early:
        s = _Session(path, lambda p: 1.0, lambda s: None, start_at=n / RATE)
        assert s.frames_played == n
        s.start()
        got = pump(s)
        s.close()
        assert np.array_equal(got[: RATE - n], ref[n:])
