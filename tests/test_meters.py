"""The meters against the figures in their standards (IEC 60268-17 VU, IEC 60268-10 PPM, AES17 RMS)."""
import math
import queue

import numpy as np
import pytest

from ironbark.meters import MeterEngine

RATE = 48000


def sine(freq, seconds, dbfs=0.0, phase=0.0):
    t = np.arange(int(RATE * seconds)) / RATE
    x = 10 ** (dbfs / 20) * np.sin(2 * np.pi * freq * t + phase)
    return np.stack([x, x], axis=1).astype(np.float32)


def run(signal, **kwargs):
    """Feed `signal` in player-sized blocks; return the engine and its timeline."""
    engine = MeterEngine(queue.Queue(), **kwargs)
    block = 4096
    for start in range(0, len(signal), block):
        engine.process(signal[start:start + block], RATE, start / RATE)
    return engine, list(engine._timeline)


def series(timeline, index, ch=0):
    return np.array([entry[index][ch] for entry in timeline])


def db(x):
    return 20 * np.log10(np.maximum(x, 1e-12))


def test_vu_reads_zero_for_a_sine_at_the_alignment_level():
    engine, timeline = run(sine(1000, 2.0, -18))
    assert abs(db(series(timeline, 1)[-1] / engine.vu_reference())) < 0.1
    reading = engine.reading_at(1.9)
    assert abs(reading.vu[0]) < 0.1


def test_vu_needle_timing_is_iec_60268_17():
    engine, timeline = run(sine(1000, 2.0, -18))
    vu = series(timeline, 1) / engine.vu_reference()  # linear, 1.0 = 0 VU
    t99 = np.argmax(vu >= 0.99) / 1000
    assert 0.27 <= t99 <= 0.33            # 99% in 300 ms (+-10%)
    assert 1.01 <= vu.max() <= 1.015      # overshoot 1-1.5%


def test_vu_alignment_can_be_minus_20():
    engine, _ = run(sine(1000, 2.0, -20), vu_align_dbfs=-20)
    assert abs(engine.reading_at(1.9).vu[0]) < 0.1


def burst_reading(ms, kind):
    silence = np.zeros((RATE // 10, 2), np.float32)
    signal = np.concatenate([silence, sine(5000, ms / 1000), silence * 5])
    _, timeline = run(signal, ppm_type=kind)
    return db(series(timeline, 2).max())


@pytest.mark.parametrize("kind,ms,expected,tolerance", [
    ("I", 5, -2, 0.3),    # Type I integration time: a 5 ms burst reads -2 dB
    ("I", 10, -1, 0.5),
    ("I", 3, -4, 0.75),
    ("II", 10, -2, 0.3),  # Type II integration time: a 10 ms burst reads -2 dB
    ("II", 5, -4, 0.5),
    ("II", 100, 0, 0.3),
    ("II", 1.5, -9, 2.0),  # the shortest bursts: within 2 dB of the table (see meters.py)
])
def test_ppm_burst_table_iec_60268_10(kind, ms, expected, tolerance):
    assert abs(burst_reading(ms, kind) - expected) <= tolerance


@pytest.mark.parametrize("kind,seconds,drop", [("I", 1.7, 20), ("II", 2.8, 24)])
def test_ppm_return_time(kind, seconds, drop):
    signal = np.concatenate([sine(5000, 0.5), np.zeros((int(RATE * 3.5), 2), np.float32)])
    _, timeline = run(signal, ppm_type=kind)
    ppm = db(series(timeline, 2))
    end = 500  # ms: the tone stops here and the meter starts falling
    assert abs((ppm[end] - ppm[end + int(seconds * 1000)]) - drop) < 0.5


def test_rms_is_aes17_full_scale_sine_reads_zero():
    engine, _ = run(sine(997, 1.0, 0.0))
    assert abs(engine.reading_at(0.9).rms[0]) < 0.05
    engine, _ = run(sine(997, 1.0, -18.0))
    assert abs(engine.reading_at(0.9).rms[0] + 18) < 0.05


def test_true_peak_sees_the_peak_between_samples():
    # A sine at a quarter of the sample rate, sampled 45 degrees off its peaks: every sample is
    # at 0.707, but the waveform the DAC rebuilds reaches 1.0.
    engine, _ = run(sine(RATE / 4, 1.0, 0.0, phase=math.pi / 4))
    reading = engine.reading_at(0.9)
    assert abs(reading.sample_peak[0] + 3.01) < 0.05
    assert abs(reading.true_peak[0]) < 0.2
    assert abs(reading.max_true_peak) < 0.2


def test_readings_follow_the_moment_heard():
    engine, _ = run(np.concatenate([sine(1000, 1.0, -18), np.zeros((RATE, 2), np.float32)]))
    assert engine.reading_at(0.95).vu[0] > -1        # still in the tone
    assert engine.reading_at(1.95).vu[0] < -20       # a second into the silence
    assert engine.reading_at(10.0) is None           # long after: nothing playing


def test_live_loudness_reads_the_ebu_reference_tone():
    # EBU Tech 3341 case 1 through the live meter, in player-sized blocks: M, S and I all -23.0 LUFS.
    engine, _ = run(sine(1000, 20, -23.0))
    reading = engine.reading_at(19.9)
    for value in (reading.momentary, reading.short_term, reading.integrated, reading.max_short_term):
        assert abs(value + 23.0) <= 0.1
    assert abs(reading.loudness_range) < 0.1 and abs(reading.measured - 20.0) < 0.1


def test_live_loudness_starts_again_on_reset():
    engine = MeterEngine(queue.Queue())
    loud, quiet = sine(1000, 10, -20.0), sine(1000, 10, -30.0)
    for start in range(0, len(loud), 4096):
        engine.process(loud[start:start + 4096], RATE, start / RATE)
    engine.reset_loudness()
    for start in range(0, len(quiet), 4096):
        engine.process(quiet[start:start + 4096], RATE, 10 + start / RATE)
    reading = engine.reading_at(19.9)
    assert abs(reading.integrated + 30.0) <= 0.1 and abs(reading.max_momentary + 30.0) <= 0.1
