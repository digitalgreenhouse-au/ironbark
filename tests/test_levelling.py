"""Volume levelling: the gain from each tag form (Vorbis, ID3 TXXX, MP4 free-form, Opus R128), the
measured fallback (EBU Tech 3341 signals), the peak limit, the clipping guard winning when it's
smaller, the wording, and — the one that matters most — Off leaves playback exactly as it was:
every sample sent identical to the file."""
import gc
import math
import queue
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
import sounddevice as sd

from ironbark import engine, levelling, tagio
from ironbark.details import describe_proof, output_line
from ironbark.engine import BLOCK_FRAMES, OutputInfo, Player
from ironbark.levelling import GainTags, Levelling, level_for, mode_for, parse_gain, parse_peak, parse_r128
from ironbark.library import Track
from ironbark.proof import Proof, SentRecord, apply_gain
from ironbark.tagio import id3, mp4
from mp4_maker import write_m4a
from tagging import write_id3, write_vorbis

RNG = np.random.default_rng(18)
TIME_INFO = SimpleNamespace(outputBufferDacTime=0.0, currentTime=0.0)
OK = SimpleNamespace(output_underflow=False)


def db(x):
    return 10 ** (x / 20)


# -- reading the tags ----------------------------------------------------------------------------
@pytest.mark.parametrize("text,want", [
    ("-6.20 dB", -6.2), ("+1.50 dB", 1.5), ("-6.2", -6.2), ("−3,40 dB", -3.4), (" -0.5DB ", -0.5),
    ("3 dB", 3.0), (".5 dB", 0.5),
])
def test_gain_spellings(text, want):
    assert parse_gain(text) == pytest.approx(want)


@pytest.mark.parametrize("text", ["", "dB", "loud", "-6.2 dB extra", "1e3 dB", "-99 dB", "nan"])
def test_gains_that_arent(text):
    assert parse_gain(text) is None


def test_peaks_and_opus_r128():
    assert parse_peak("0.988312") == pytest.approx(0.988312) and parse_peak("1,2") == pytest.approx(1.2)
    assert parse_peak("0") is None and parse_peak("-1") is None and parse_peak("x") is None
    assert parse_peak("inf") is None and parse_peak("99") is None
    # RFC 7845: a Q7.8 integer in dB — the RFC's own examples
    assert parse_r128("-573") == pytest.approx(-573 / 256) and parse_r128("111") == pytest.approx(111 / 256)
    assert parse_r128("40000") is None and parse_r128("-1.5") is None and parse_r128("") is None


def test_tags_from_values_any_case_and_cue_tracks():
    values = {"replaygain_track_gain": ["-7.10 dB"], "REPLAYGAIN_ALBUM_GAIN": ["-8.00 dB"],
              "Replaygain_Track_Peak": ["0.95"], "REPLAYGAIN_ALBUM_PEAK": ["0.99"]}
    tags = levelling.tags_from_values(values)
    assert (tags.track_gain, tags.album_gain, tags.track_peak, tags.album_peak) == (-7.1, -8.0, 0.95, 0.99)
    assert tags.source == "ReplayGain tags"
    one_of_a_cue = levelling.tags_from_values(values, whole_file=False)   # the image's track figures: not this track's
    assert one_of_a_cue.track_gain is None and one_of_a_cue.track_peak is None and one_of_a_cue.album_gain == -8.0
    assert levelling.tags_from_values({"TITLE": ["x"]}) is None


def test_opus_r128_tags_come_first_and_move_to_minus_18_lufs():
    values = {"R128_TRACK_GAIN": ["-573"], "R128_ALBUM_GAIN": ["111"], "REPLAYGAIN_TRACK_GAIN": ["-1 dB"]}
    tags = levelling.tags_from_values(values, opus=True)
    assert tags.source == "Opus R128 tags"
    assert tags.track_gain == pytest.approx(-573 / 256 + 5) and tags.album_gain == pytest.approx(111 / 256 + 5)
    # An Opus file with only ReplayGain tags (as some taggers write) is read as any other.
    assert levelling.tags_from_values({"REPLAYGAIN_TRACK_GAIN": ["-1 dB"]}, opus=True).track_gain == -1.0


def noise(frames=20000, scale=4000):
    return (RNG.standard_normal((frames, 2)) * scale).astype(np.int16)


def test_each_tag_form_is_read_from_a_real_file(tmp_path):
    flac = tmp_path / "a.flac"
    sf.write(flac, noise(), 44100, subtype="PCM_16")
    write_vorbis(flac, REPLAYGAIN_TRACK_GAIN="-6.20 dB", REPLAYGAIN_TRACK_PEAK="0.911")
    assert levelling.read_tags(flac) == GainTags(-6.2, None, 0.911, None)

    mp3 = tmp_path / "a.mp3"
    sf.write(mp3, noise() / 32768, 44100, format="MP3")
    write_id3(mp3, [id3.UserTextFrame("replaygain_album_gain", ["-4.00 dB"]),
                    id3.UserTextFrame("replaygain_track_gain", ["-3.50 dB"])])
    tags = levelling.read_tags(mp3)
    assert (tags.track_gain, tags.album_gain) == (-3.5, -4.0)

    wav = tmp_path / "a.wav"
    sf.write(wav, noise(), 44100, subtype="PCM_16")
    write_id3(wav, [id3.UserTextFrame("REPLAYGAIN_TRACK_GAIN", ["+2.00 dB"])], version=3)
    assert levelling.read_tags(wav).track_gain == 2.0

    m4a = write_m4a(tmp_path / "a.m4a", noise(), bits=16)
    audio = mp4.MP4File(m4a)
    audio["----:com.apple.iTunes:replaygain_track_gain"] = [mp4.FreeForm(b"-9.87 dB")]
    audio["----:com.apple.iTunes:replaygain_track_peak"] = [mp4.FreeForm(b"1.012")]
    audio.save()
    tags = levelling.read_tags(m4a)
    assert (tags.track_gain, tags.track_peak) == (-9.87, 1.012)

    opus = tmp_path / "a.opus"
    sf.write(opus, noise() / 32768, 48000, format="OGG", subtype="OPUS")
    write_vorbis(opus, R128_TRACK_GAIN="-1280")
    tags = levelling.read_tags(opus)
    assert tags.source == "Opus R128 tags" and tags.track_gain == pytest.approx(-5.0 + 5.0)

    plain = tmp_path / "b.flac"
    sf.write(plain, noise(), 44100, subtype="PCM_16")
    assert levelling.read_tags(plain) is None


# -- measuring, when there are no tags --------------------------------------------------------------
def tone(seconds, dbfs, rate=48000, freq=1000.0):
    t = np.arange(int(rate * seconds)) / rate
    x = db(dbfs) * np.sin(2 * np.pi * freq * t)
    return np.repeat(x[:, None], 2, axis=1)


@pytest.mark.parametrize("level", [-23.0, -33.0])   # EBU Tech 3341, cases 1 and 2
def test_the_measured_fallback_reads_the_ebu_test_tones(tmp_path, level):
    path = tmp_path / "tone.wav"
    sf.write(path, tone(20, level), 48000, subtype="FLOAT")
    peak, lufs = levelling.measure(path)
    assert lufs == pytest.approx(level, abs=0.1)
    assert peak == pytest.approx(db(level), rel=0.01)
    found = level_for("track", None, lufs, None, peak)
    assert found.source == "measured" and found.mode == "track"
    assert found.db == pytest.approx(-18.0 - level, abs=0.1)    # brought to ReplayGain's -18 LUFS
    assert not found.limited                                      # (the peak leaves room)
    assert levelling.measure(path, peak=False)[0] is None
    stop = threading.Event()
    stop.set()
    assert levelling.measure(path, cancel=stop) is None


# -- working out the gain ---------------------------------------------------------------------------
def test_track_album_and_fallbacks():
    tags = GainTags(-6.0, -8.0)
    assert level_for("track", tags, None, None, None) == Levelling(db(-6.0), "track", "ReplayGain tags")
    assert level_for("album", tags, None, None, None) == Levelling(db(-8.0), "album", "ReplayGain tags")
    # No album gain in the tags: the album's own measurement, else the track's gain for now.
    no_album = GainTags(-6.0, None)
    assert level_for("album", no_album, None, -10.0, None) == Levelling(db(-8.0), "album", "measured")
    assert level_for("album", no_album, None, None, None).mode == "track"
    assert level_for("track", None, None, None, None) is None        # not measured yet
    assert level_for("off", tags, -10.0, -10.0, 0.5) is None
    assert level_for("track", tags, None, None, None, preamp_db=2.0).db == pytest.approx(-4.0)


def test_never_turned_up_past_the_peak():
    quiet = GainTags(+9.0, None)
    # No peak known: never above unity.
    assert level_for("track", quiet, None, None, None) == Levelling(1.0, "track", "ReplayGain tags", limited=True)
    # A true peak of -6 dBTP leaves 5.9 dB: the gain stops so the peak lands at -0.1 dBTP.
    held = level_for("track", quiet, None, None, db(-6.0))
    assert held.limited and held.db == pytest.approx(5.9)
    # The tags' own peak counts too (the higher of the two).
    tagged = GainTags(+9.0, None, track_peak=db(-3.0))
    assert level_for("track", tagged, None, None, db(-6.0)).db == pytest.approx(2.9)
    # An album's gain is held by the album's peak (so the album keeps its balance).
    album = GainTags(+9.0, +4.0, track_peak=db(-12.0), album_peak=db(-3.0))
    assert level_for("album", album, None, None, db(-12.0)).db == pytest.approx(2.9)
    # A loud master whose true peak is over full scale: turned down below the ceiling too.
    loud = level_for("track", GainTags(-1.0, None), None, None, db(+2.0))
    assert loud.db == pytest.approx(-2.1) and loud.limited


def test_smart_mode():
    assert mode_for("smart", "album", shuffle=False, queued=False) == "album"
    assert mode_for("smart", "folder", shuffle=False, queued=False) == "album"
    assert mode_for("smart", "album", shuffle=True, queued=False) == "track"
    assert mode_for("smart", "album", shuffle=False, queued=True) == "track"   # a song from Up next
    assert mode_for("smart", "playlist", shuffle=False, queued=False) == "track"
    assert mode_for("track", "album", False, False) == "track" and mode_for("album", "playlist", True, True) == "album"
    assert mode_for("off", "album", False, False) == "off" and mode_for("odd", None, False, False) == "off"


# -- in the player: the guard and levelling together ---------------------------------------------------
def test_the_smaller_of_guard_and_levelling_wins():
    p = Player()
    a, b = Track(Path("a.flac"), "a"), Track(Path("b.flac"), "b")
    p.set_tracks([a, b])
    p.set_volume(1.0)
    p.set_trim(a.path, db(-1.0))
    p.set_levelling(a.path, Levelling(db(-4.0), "album", "ReplayGain tags"))
    assert p.trim_for(a.path) == pytest.approx(db(-4.0))          # levelling turns it down further
    p.set_levelling(a.path, Levelling(db(+2.0), "track", "measured"))
    assert p.trim_for(a.path) == pytest.approx(db(-1.0))          # the guard holds a boost back
    p.set_levelling(b.path, Levelling(db(+3.0), "track", "measured"))
    assert p._gain(b.path) == pytest.approx(db(+3.0))             # set ahead; the guard leaves it alone
    status = p.status()
    assert status.guard == pytest.approx(db(-1.0)) and status.levelling.gain == pytest.approx(db(2.0))
    p.set_levelling(None)                                          # off: every song as before
    assert p.trim_for(a.path) == pytest.approx(db(-1.0)) and p._gain(b.path) == 1.0
    assert p.status().levelling is None


def test_apply_gain_above_one_turns_up_and_clips_at_full_scale():
    block = np.array([[1000, -2000], [2**30, -(2**31)]], dtype=np.int32)
    assert apply_gain(block, 1.0) is not block and np.array_equal(apply_gain(block, 1.0), block)
    up = apply_gain(block, 2.0)
    assert up[0].tolist() == [2000, -4000] and up[1].tolist() == [2**31 - 1, -(2**31)]
    floats = np.array([[0.25, -0.75]], dtype=np.float32)
    assert apply_gain(floats, 2.0).tolist() == [[0.5, -1.0]]
    assert apply_gain(floats, 0.5).tolist() == [[0.125, -0.375]]


def test_the_proof_counts_a_boost_as_a_change():
    record = SentRecord("x.wav", 44100, "float32", 4)
    record.add(0, 0, np.zeros((4, 2), np.float32), 1.0)
    assert record.summary().untouched
    record.add(0, 4, np.zeros((4, 2), np.float32), 1.5)
    summary = record.summary()
    assert not summary.untouched and summary.max_gain == 1.5 and summary.changed_from == pytest.approx(4 / 44100)


# -- Off = exactly today's playback: every sample sent is the file's -------------------------------
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


def pull(session, limit=200):
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


def wav(tmp_path, name, frames):
    path = tmp_path / name
    sf.write(path, noise(frames), 44100, subtype="PCM_16")
    return path, sf.read(path, dtype="float32", always_2d=True)[0]


def play_through(player, paths):
    player.set_tracks([Track(p, p.stem) for p in paths])
    player.set_volume(1.0)
    with player._lock:
        player._start(0)
    session = player._session
    got = pull(session)
    proof = session.sent.summary()
    player.stop()
    return got, proof


def test_off_sends_every_sample_exactly_as_the_file(tmp_path, fake_card):
    (a, da), (b, db_) = wav(tmp_path, "a.wav", 9000), wav(tmp_path, "b.wav", 7000)
    want = np.concatenate([da, db_])
    untouched, _ = play_through(Player(), [a, b])
    assert np.array_equal(untouched[: len(want)], want)
    # Switched on, then off again: exactly the same as never switched on.
    player = Player()
    player.set_levelling(a, Levelling(db(-6.0), "track", "measured"))
    player.set_levelling(b, Levelling(db(-6.0), "track", "measured"))
    player.set_levelling(None)
    again, proof = play_through(player, [a, b])
    assert np.array_equal(again, untouched) and proof.untouched
    # On: the music is sent at the levelling gain (through the same path the proof checks).
    player = Player()
    player.set_levelling(a, Levelling(db(-6.0), "track", "measured"))
    player.set_levelling(b, Levelling(db(-6.0), "track", "measured"))
    levelled, proof = play_through(player, [a, b])
    gain = np.float32(db(-6.0))
    assert np.array_equal(levelled[: len(want)], want * gain)
    assert not proof.untouched and proof.min_gain == pytest.approx(db(-6.0))


# -- wording -------------------------------------------------------------------------------------------
EXCLUSIVE = OutputInfo("DAC", 44100, None, exclusive=True, hardware_volume=True, device_bits=32)
SHARED = OutputInfo("Speakers", 44100, 44100, exclusive=False)


def test_output_line_says_levelling_and_is_amber():
    level = Levelling(db(-4.3), "album", "ReplayGain tags")
    text, tone = output_line(EXCLUSIVE, 1.0, trim=db(-4.3), bits=16, level=level)
    assert tone == "warn" and "Volume levelling −4.3 dB (album), so not bit-perfect" in text
    text, tone = output_line(SHARED, 1.0, trim=db(-4.3), level=level)
    assert tone == "warn" and "· shared (" in text and text.endswith(" mixer) · Volume levelling −4.3 dB (album)")
    held = output_line(EXCLUSIVE, 1.0, trim=db(-1.0), level=Levelling(db(2.0), "track", "measured"))[0]
    assert "Volume levelling −1.0 dB (track, held down by the clipping guard)" in held
    boost = output_line(EXCLUSIVE, 1.0, trim=db(3.0), level=Levelling(db(3.0), "track", "measured"))[0]
    assert "Volume levelling +3.0 dB (track)" in boost
    # Off: exactly the old wording.
    assert output_line(EXCLUSIVE, 1.0, bits=16) == ("→ DAC · 44.1 kHz exclusive · bit-perfect ✓ · volume by device ✓",
                                                     "good")


def test_proof_line_names_levelling_and_the_fade():
    proof = Proof(sent=10, checked=10, min_gain=db(-4.3))
    text, tone = describe_proof(proof, EXCLUSIVE, 1.0, db(-4.3), level=Levelling(db(-4.3), "album", "measured"))
    assert tone == "warn" and "changed by volume levelling −4.3 dB (album)" in text
    text = describe_proof(Proof(sent=10, checked=10, min_gain=0.5), SHARED, 1.0, 1.0, fading=True)[0]
    assert "the sleep timer's fade-out" in text
    boosted = Proof(sent=10, checked=10, max_gain=1.4)
    assert "not bit-perfect" in describe_proof(boosted, EXCLUSIVE, 1.0, 1.4,
                                                level=Levelling(1.4, "track", "measured"))[0]


# -- the window: Settings, the worker, the lock's tooltip -----------------------------------------------
from test_light_mini import _pump, make_window  # noqa: E402,F401  (make_window is a fixture)


def test_the_window_levels_the_song_playing_and_the_next(make_window):
    window, root, player, errors = make_window(levelling="track")
    path = player.track.path
    assert window.levelling_box.get() == "Track" and str(window.preamp_box.cget("state")) == "readonly"
    window._gain_tags[path] = GainTags(-6.0, -7.0)
    window._peaks[path] = db(-1.0)
    window._level_wanted = {path}                         # (as if the worker had been asked)
    status = player.status()
    window._apply_levelling(player.track, status)
    assert player.levelling_for(path) == Levelling(db(-6.0), "track", "ReplayGain tags")
    assert "Volume levelling is on (Track)" in window._lock_tip()
    # Playing: its level isn't changed by a later measurement — only by a new choice in Settings.
    window._gain_tags[path] = GainTags(-9.0, -7.0)
    window._apply_levelling(player.track, status)
    assert player.levelling_for(path).db == pytest.approx(-6.0)
    window.levelling_box.current(2)                       # Album
    window._levelling_chosen()
    window._apply_levelling(player.track, status)
    assert player.levelling_for(path) == Levelling(db(-7.0), "album", "ReplayGain tags")
    window.preamp_box.current(levelling.PREAMP_CHOICES.index(3.0))
    window._preamp_chosen()
    window._apply_levelling(player.track, status)
    assert player.levelling_for(path).db == pytest.approx(-4.0) and window.prefs["levelling_preamp"] == 3.0
    window.levelling_box.current(0)                       # Off: taken away at once
    window._levelling_chosen()
    window._apply_levelling(player.track, status)
    assert player.levelling_for(path) is None and str(window.preamp_box.cget("state")) == "disabled"
    assert not errors


def test_the_worker_reads_tags_and_measures_only_what_it_must(monkeypatch, tmp_path):
    from ironbark.ui import PlayerWindow
    measured = []

    def measure(path, peak=True, loudness=True, cancel=None):
        measured.append((path.name, peak, loudness))
        return 0.5, -12.0

    monkeypatch.setattr(levelling, "measure", measure)
    monkeypatch.setattr(levelling, "read_tags", lambda path: GainTags(-3.0) if path.name == "tagged.flac" else None)
    w = SimpleNamespace(_peaks={}, _gain_tags={}, _lufs={}, _peak_jobs=queue.Queue(), _peak_wanted=set(),
                        _level_wanted={Path("tagged.flac"), Path("bare.flac")})
    w._measure_for_levelling = PlayerWindow._measure_for_levelling.__get__(w)
    for name in ("skipped.flac", "tagged.flac", "bare.flac"):
        w._peak_jobs.put(("level", Path(name)))
    threading.Thread(target=PlayerWindow._measure_peaks, args=(w,), daemon=True).start()
    deadline = time.monotonic() + 5
    while Path("bare.flac") not in w._lufs and time.monotonic() < deadline:
        time.sleep(0.01)
    # Tagged: only its true peak; bare: loudness and peak in one read; not wanted: nothing.
    assert measured == [("tagged.flac", True, False), ("bare.flac", True, True)]
    assert w._lufs == {Path("bare.flac"): -12.0} and w._peaks[Path("tagged.flac")] == 0.5
