"""Whole-file measures on awkward files: cut-off MP3s, the end of a file, low sample rates,
surround layouts, damaged album tracks, repeating music in Compare, files cut short in Compare,
the ABX test's memory, the 8x true peak's speed-up, and the live loudness meter's threads."""
import queue
import shutil
import threading
import tracemalloc

import numpy as np
import pytest
import soundfile as sf

from ironbark import abx, analysis, audiofile, clipping, compare, loudness, meters
from ironbark.analysis import (TAPS_PER_PHASE, TruePeak, oversampling_phases, album_measure, analyse,
                               true_peak, wall_verdict)
from ironbark.details import describe_analysis
from ironbark.mp3frames import _frame, _skip_id3v2

RATE = 44100


def noise(seconds, rate=RATE, amp=0.25, channels=2, seed=1):
    rng = np.random.default_rng(seed)
    return (amp * rng.standard_normal((int(seconds * rate), channels))).clip(-0.99, 0.99)


def music(seconds, rate=RATE, channels=2, seed=3):
    """Band-limited noise with a swell: never periodic, so it lines up in one place only."""
    n = int(seconds * rate)
    x = np.random.default_rng(seed).standard_normal((n, channels))
    spectrum = np.fft.rfft(x, axis=0)
    spectrum[int(15000 * n / rate):] = 0
    x = np.fft.irfft(spectrum, n=n, axis=0) * (0.6 + 0.4 * np.sin(np.linspace(0, 6, n)))[:, None]
    return np.round(0.25 * x / np.max(np.abs(x)) * 32767) / 32768


def flac(path, data, rate=RATE, subtype="PCM_16"):
    sf.write(path, data, rate, format="FLAC", subtype=subtype)
    return path


def mp3(path, seconds=20.0):
    """A CBR MP3 from libsndfile's encoder (LAME), which states its length in an Info header."""
    sf.write(path, noise(seconds, amp=0.2).astype("float32"), RATE, format="MP3",
             bitrate_mode="CONSTANT", compression_level=0.3)
    return path


def cut_in_half(path):
    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) // 2])     # an incomplete download
    return path


def without_length_header(path):
    """The same MP3 without its Info frame, so its length can only be estimated from its size."""
    data = path.read_bytes()
    pos = _skip_id3v2(data)
    while _frame(data, pos, len(data)) is None:
        pos += 1
    length = _frame(data, pos, len(data))[2]
    assert b"Info" in data[pos: pos + 64] or b"Xing" in data[pos: pos + 64]
    path.write_bytes(data[:pos] + data[pos + length:])
    return path


# -- MP3s cut off part-way --------------------------------------------------------------------
def test_a_cut_off_mp3_is_measured_only_as_far_as_it_decodes_and_called_damaged(tmp_path):
    path = cut_in_half(mp3(tmp_path / "a.mp3"))
    with sf.SoundFile(path) as f:
        stated = f.frames
        read = sum(len(block) for block in audiofile.blocks(f, RATE * 3, dtype="float32", always_2d=True))
    assert stated == 20 * RATE                     # the header still says 20 s…
    assert 8 * RATE < read < 12 * RATE             # …but only about half decodes, read once
    result = analyse(path, 16, 0)
    assert result.duration == pytest.approx(20.0)
    assert result.damaged_at == pytest.approx(read / RATE)
    assert describe_analysis(result)[0][1].startswith("✗ Damaged: the audio stops decoding at 0:")
    assert clipping.scan(path).damaged_at == pytest.approx(read / RATE)
    assert len(loudness.file_powers(path)) == pytest.approx(read / RATE * 100, abs=2)   # 10 ms steps
    assert len(meters.vu_levels(path)) <= read / (0.3 * RATE) + 1                    # 300 ms slices
    album = album_measure([path])
    assert (album.tracks, album.partial, album.skipped) == (1, 1, 0)


def test_an_intact_mp3_is_not_called_damaged_with_or_without_a_length_header(tmp_path):
    whole = mp3(tmp_path / "a.mp3")
    assert analyse(whole, 16, 0).damaged_at is None
    headerless = without_length_header(mp3(tmp_path / "b.mp3"))
    assert analyse(headerless, 16, 0).damaged_at is None
    # Without a header the stated length is only an estimate, so even a cut-off copy can't be told.
    assert analyse(cut_in_half(headerless), 16, 0).damaged_at is None
    assert clipping.scan(whole).damaged_at is None


def test_reading_stopped_early_on_purpose_is_not_damage(tmp_path):
    path = flac(tmp_path / "a.flac", noise(12))
    damaged: list = []
    with sf.SoundFile(path) as f:
        for _ in audiofile.read_blocks(f, damaged, blocksize=RATE, dtype="int32", always_2d=True):
            break
    assert damaged == []


# -- the 8x true peak ------------------------------------------------------------------------
class EveryPoint:
    """The 8x true peak working out every point of every phase: what TruePeak must equal."""

    def __init__(self, channels):
        self.phases = oversampling_phases(8)
        self.previous = np.zeros((TAPS_PER_PHASE - 1, channels))
        self.peak = 0.0

    def add(self, block):
        x = np.concatenate([self.previous, block])
        for phase in self.phases:
            for ch in range(x.shape[1]):
                y = np.convolve(x[:, ch], phase, mode="valid")
                if len(y):
                    self.peak = max(self.peak, float(np.abs(y).max()))
        self.previous = x[-(TAPS_PER_PHASE - 1):]

    def finish(self):
        self.add(np.zeros((TAPS_PER_PHASE, self.previous.shape[1])))


def signals():
    rng = np.random.default_rng(7)
    loud = np.clip(1.6 * rng.standard_normal((RATE * 3, 2)), -1, 1)           # a squashed master
    dynamic = 0.01 * rng.standard_normal((RATE * 4, 2))                        # quiet, with bursts
    dynamic[RATE: RATE + 300] += 0.8 * np.sin(np.arange(300) * 2.9)[:, None]
    dynamic[3 * RATE + 17: 3 * RATE + 40, 1] = 0.97
    edges = np.zeros((10000, 1))
    edges[2047:2049] = 1.0                                                     # across a stretch boundary
    edges[-2:] = -1.0                                                          # and right at the end
    surround = 0.3 * rng.standard_normal((RATE, 6))
    surround[5000, 4] = 1.0
    return {"loud": loud, "dynamic": dynamic, "edges": edges, "surround": surround}


@pytest.mark.parametrize("name", ["loud", "dynamic", "edges", "surround"])
@pytest.mark.parametrize("block", [1, 47, 2048, 5000, RATE * 5])
@pytest.mark.parametrize("dtype", ["float32", "float64"])
def test_true_peak_equals_working_out_every_point(name, block, dtype):
    x = signals()[name].astype(dtype)
    if block == 1:
        x = x[:3000]
    fast, slow = TruePeak(x.shape[1]), EveryPoint(x.shape[1])
    for start in range(0, len(x), block):
        fast.add(x[start: start + block])
        slow.add(x[start: start + block])
    fast.finish()
    slow.finish()
    assert fast.peak == slow.peak                   # exactly: the skip never changes the answer


def test_true_peak_skips_stretches_that_cannot_beat_the_peak(monkeypatch):
    x = signals()["dynamic"]
    worked_out = []
    convolve = np.convolve

    def counting(*args, **kwargs):
        y = convolve(*args, **kwargs)
        worked_out.append(len(y))
        return y

    monkeypatch.setattr(np, "convolve", counting)
    meter = TruePeak(2)
    meter.add(x)
    every_point = len(x) * 2 * 8                    # frames x channels x phases
    assert sum(worked_out) < 0.2 * every_point      # the quiet stretches never filtered


def test_true_peak_sees_the_very_end_of_a_file(tmp_path):
    x = 0.05 * np.random.default_rng(2).standard_normal((RATE * 3, 2))
    x[-3:] = [[0.0, 0.0], [0.9999, 0.9999], [0.9999, 0.9999]]     # two full-scale samples to finish
    path = flac(tmp_path / "a.flac", x, subtype="PCM_24")
    expected = float(np.abs(clipping.oversample(x[-200:])).max())  # silence after, as a DAC sees it
    assert expected > 1.2
    assert true_peak(path) == pytest.approx(expected, rel=1e-6)
    assert analyse(path, 24, 0).true_peak == pytest.approx(expected, rel=1e-6)


# -- "Is it genuine?" below CD rate ----------------------------------------------------------
def test_the_converters_own_filter_below_cd_rate_is_not_called_lossy():
    assert wall_verdict(15059, True, 32000) == ""          # a 32 kHz recorder's own filter
    assert wall_verdict(10387, True, 22050) == ""
    assert wall_verdict(11000, True, 32000) == "lossy"     # far below it: still a lossy clue
    assert wall_verdict(8000, True, 22050) == "lossy"
    assert wall_verdict(19500, True, 44100) == "lossy"     # CD rate and up: unchanged
    assert wall_verdict(16000, True, 48000) == "lossy"


@pytest.mark.parametrize("rate", [22050, 32000])
def test_lossless_low_rate_recordings_pass_the_check(tmp_path, rate):
    x = noise(10, rate=rate, amp=0.1)
    spectrum = np.fft.rfft(x, axis=0)
    spectrum[np.fft.rfftfreq(len(x), 1 / rate) > 0.45 * rate] = 0      # the converter's anti-alias wall
    x = np.fft.irfft(spectrum, n=len(x), axis=0)
    result = analyse(flac(tmp_path / "a.flac", x, rate=rate), 16, 0)
    assert result.cutoff_sharp and result.cutoff_hz > 0.43 * rate
    assert result.verdict == ""


# -- surround channel weights (BS.1770-5) -----------------------------------------------------
def tone_in(channel, channels, seconds=6.0, rate=48000):
    t = np.arange(int(seconds * rate)) / rate
    x = np.zeros((len(t), channels))
    x[:, channel] = 0.1 * np.sin(2 * np.pi * 997 * t)
    meter = loudness.Loudness(rate, channels)
    meter.add(x)
    return loudness.integrated(meter.powers)


@pytest.mark.parametrize("channels", [6, 7, 8])
def test_the_lfe_is_left_out_of_every_surround_layout(channels):
    assert tone_in(3, channels) is None


@pytest.mark.parametrize("channels, front, surrounds, others", [
    (4, 0, [2, 3], []),          # quad: back pair on the surround speakers
    (5, 0, [3, 4], [2]),
    (6, 0, [4, 5], [2]),
    (7, 0, [5, 6], [2, 4]),      # 6.1: back centre counts as a front
    (8, 0, [6, 7], [2, 4, 5]),   # 7.1: back pair at 135 degrees 1.0, side pair at 90 degrees 1.41
])
def test_surround_channels_are_weighted_by_where_they_sit(channels, front, surrounds, others):
    base = tone_in(front, channels)
    for ch in surrounds:
        assert tone_in(ch, channels) - base == pytest.approx(10 * np.log10(1.41), abs=0.005)
    for ch in others:
        assert tone_in(ch, channels) == pytest.approx(base, abs=0.005)


# -- album figures with a damaged track -------------------------------------------------------
def test_album_counts_a_damaged_track_up_to_where_it_stops_and_says_what_it_left_out(tmp_path):
    good = flac(tmp_path / "1.flac", noise(20, amp=0.1))
    damaged = flac(tmp_path / "2.flac", noise(20, amp=0.3, seed=2))
    raw = bytearray(damaged.read_bytes())
    raw[len(raw) // 2: len(raw) // 2 + 20000] = bytes(20000)
    damaged.write_bytes(bytes(raw))
    unreadable = tmp_path / "3.flac"
    unreadable.write_bytes(b"not audio at all" * 100)
    stops = analyse(damaged, 16, 0).damaged_at
    assert stops is not None and 0 < stops < 20

    album = album_measure([good, damaged, unreadable])
    assert (album.tracks, album.partial, album.skipped) == (2, 1, 1)
    alone = album_measure([good])
    assert album.lufs > alone.lufs + 1                 # the louder damaged track's first part counts


# -- Compare ---------------------------------------------------------------------------------
def write16(path, x, rate=RATE):
    sf.write(path, x, rate, format="FLAC", subtype="PCM_16")
    return path


def test_identical_copies_of_repeating_music_are_identical(tmp_path):
    loop = music(2.0)
    silence = np.zeros((3 * RATE, 2))
    x = np.concatenate([silence, np.tile(loop, (15, 1)), silence])
    a = write16(tmp_path / "a.flac", x)
    b = tmp_path / "b.flac"
    shutil.copy(a, b)
    result = compare.compare_files(a, b)
    assert (result.verdict, result.offset) == ("identical", 0)

    t = np.arange(30 * RATE) / RATE
    fade = np.clip(np.minimum(t, t[-1] - t), 0, 1)                   # 1 s fades
    tone = np.round(0.5 * np.sin(2 * np.pi * 1000 * t) * fade * 32767) / 32768
    c = write16(tmp_path / "c.flac", np.column_stack([tone, tone]))
    d = tmp_path / "d.flac"
    shutil.copy(c, d)
    result = compare.compare_files(c, d)
    assert (result.verdict, result.offset) == ("identical", 0)


def test_find_lag_takes_the_nearest_of_equally_good_matches():
    period = np.random.default_rng(4).standard_normal(500)
    region = np.tile(period, 20)
    probe = region[3000:5000]
    assert compare.find_lag(probe, region, prefer=2400)[0] == 2500
    assert compare.find_lag(probe, region, prefer=3100)[0] == 3000
    shifted, match = compare.find_lag(probe, region, prefer=0)
    assert shifted == 0 and match > 0.99


def test_a_copy_cut_short_is_not_called_the_same(tmp_path):
    x = music(30)
    a = write16(tmp_path / "a.flac", x)
    part = compare.compare_files(a, write16(tmp_path / "part.flac", x[: 12 * RATE]))
    assert part.verdict == "partial" and part.missing_from_b and not part.extra_in_b
    rows = compare.describe(part)
    assert rows[2][2] == "warn" and "B is missing some of A's audio" in rows[2][1]
    assert ("Lined up", "Both start at the same sample", "plain") in rows

    scrap = compare.compare_files(a, write16(tmp_path / "scrap.flac", x[: RATE // 2]))
    assert scrap.verdict == "partial" and not scrap.offset_found and not scrap.silent
    assert any(label == "Lined up" and "couldn't be found in B" in text for label, text, _ in compare.describe(scrap))

    empty = tmp_path / "empty.wav"
    sf.write(empty, np.zeros((0, 2)), RATE, subtype="PCM_16")
    nothing = compare.compare_files(a, empty)
    assert nothing.verdict == "nothing_compared" and nothing.compared == 0
    assert compare.describe(nothing)[2] == ("Result", "⚠ Nothing to compare: B has no audio alongside A's", "warn")


def test_extra_silence_is_fine_but_extra_music_is_not(tmp_path):
    x = music(20)
    a = write16(tmp_path / "a.flac", x)
    quiet_end = compare.compare_files(a, write16(tmp_path / "b.flac", np.concatenate([x, np.zeros((2 * RATE, 2))])))
    assert quiet_end.verdict == "same_shifted" and quiet_end.extra_end == 2 * RATE
    assert "only the lengths differ" in compare.describe(quiet_end)[2][1]
    more = compare.compare_files(a, write16(tmp_path / "c.flac", np.concatenate([x, music(2, seed=9)])))
    assert more.verdict == "partial" and more.extra_in_b and not more.missing_from_b
    assert "B has audio A hasn't" in compare.describe(more)[2][1]


# -- ABX -------------------------------------------------------------------------------------
def test_abx_prepare_of_a_long_hi_res_excerpt_stays_modest(tmp_path):
    rate = 192000
    x = (0.2 * np.random.default_rng(1).standard_normal((rate * 40, 2))).astype(np.float32)
    a = flac(tmp_path / "a.flac", x, rate=rate, subtype="PCM_24")
    b = flac(tmp_path / "b.flac", np.concatenate([np.zeros((5000, 2), np.float32), x]), rate=rate,
             subtype="PCM_24")
    del x
    tracemalloc.start()
    try:
        pair = abx.prepare(a, b, 5.0, 30.0)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert pair.lined_up and round(pair.offset_s * rate) == 5000
    assert peak < 250e6                         # the excerpts themselves are 92 MB; it was over 600


@pytest.mark.parametrize("shift", [-60000, -1, 0, 777, 80000])
def test_abx_lines_up_anywhere_within_the_margin(tmp_path, shift):
    x = music(16).astype(np.float32)
    a = tmp_path / "a.wav"
    b = tmp_path / "b.wav"
    sf.write(a, x, RATE, subtype="FLOAT")
    sf.write(b, x[-shift:] if shift < 0 else np.concatenate([np.zeros((shift, 2), np.float32), x]), RATE,
             subtype="FLOAT")
    pair = abx.prepare(a, b, start_s=5.0, length_s=6.0)
    assert pair.lined_up and round(pair.offset_s * RATE) == shift
    assert np.array_equal(pair.a, pair.b)


def test_abx_lines_up_repeating_music_where_it_is(tmp_path):
    loop = music(1.0).astype(np.float32)
    x = np.tile(loop, (12, 1))
    a = tmp_path / "a.wav"
    sf.write(a, x, RATE, subtype="FLOAT")
    pair = abx.prepare(a, a, start_s=3.0, length_s=5.0)
    assert pair.lined_up and pair.offset_s == 0.0


# -- the live loudness meter -----------------------------------------------------------------
def test_programme_gives_exactly_what_the_whole_file_functions_give():
    rng = np.random.default_rng(5)
    powers = np.concatenate([10 ** rng.uniform(-9, -0.5, 4000), np.zeros(500), 10 ** rng.uniform(-3, -1, 3000)])
    programme = loudness.Programme()
    fed = 0
    for size in rng.integers(1, 700, 100):
        programme.add(powers[fed: fed + size])
        fed = min(len(powers), fed + size)
        assert programme.integrated() == loudness.integrated(powers[:fed])
        assert programme.loudness_range() == loudness.loudness_range(powers[:fed])
        if fed == len(powers):
            break


def feed(engine, x, rate=48000, at=0.0):
    block = rate // 10            # 100 ms: ten whole loudness steps each time
    for start in range(0, len(x), block):
        engine.process(x[start: start + block], rate, at + start / rate)


def test_live_integrated_loudness_is_worked_out_on_the_meters_thread(monkeypatch):
    rate = 48000
    rng = np.random.default_rng(6)
    x = (0.1 * rng.standard_normal((rate * 40, 2)) * np.linspace(0.2, 1.0, rate * 40)[:, None]).astype(np.float32)
    engine = meters.MeterEngine(queue.Queue())
    feed(engine, x)
    meter = engine.loudness
    want = (loudness.integrated(meter.measured()), loudness.loudness_range(meter.measured()))

    def not_here(*_args):
        raise AssertionError("worked out while reading")

    for name in ("integrated", "loudness_range"):
        monkeypatch.setattr(meters, name, not_here, raising=False)
    monkeypatch.setattr(loudness, "_gated", not_here, raising=False)
    monkeypatch.setattr(loudness, "_spread", not_here, raising=False)
    reading = engine.reading_at(39.95)
    assert (reading.integrated, reading.loudness_range) == want
    monkeypatch.undo()

    engine.reset_loudness()
    feed(engine, x[: rate * 10], at=40.0)
    reading = engine.reading_at(49.95)
    assert reading.integrated == loudness.integrated(engine.loudness.measured())
    assert reading.measured == pytest.approx(10.0)


def test_resetting_the_highest_true_peak_waits_for_the_meters_thread():
    engine = meters.MeterEngine(queue.Queue())
    engine.max_true_peak = 0.9
    with engine._lock:                       # the meters thread part-way through updating it
        reset = threading.Thread(target=engine.reset_max)
        reset.start()
        reset.join(0.2)
        assert reset.is_alive() and engine.max_true_peak == 0.9
    reset.join(5)
    assert engine.max_true_peak == 0.0
