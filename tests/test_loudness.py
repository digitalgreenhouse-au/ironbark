"""Loudness against the published figures: ITU-R BS.1770-5's filter coefficients, and the
EBU's minimum-requirements test signals (Tech 3341 Table 1 for momentary, short-term and
integrated; Tech 3342 Table 1 for loudness range), synthesised here as the tables describe.
Cases 7-8 (Tech 3341) and 5-6 (Tech 3342) are recorded programme from the EBU's download set
and aren't included."""
import math

import numpy as np
import pytest

from ironbark import loudness
from ironbark.loudness import HIGHPASS_48K, SHELF_48K, Loudness, integrated, loudness_range

RATE = 48000


def tone(seconds, dbfs, rate=RATE, channels=2, freq=1000.0):
    t = np.arange(int(round(rate * seconds))) / rate
    x = 10 ** (dbfs / 20) * np.sin(2 * np.pi * freq * t) if dbfs is not None else np.zeros_like(t)
    return np.repeat(x[:, None], channels, axis=1)


def tones(*parts, rate=RATE):
    return np.concatenate([tone(s, d, rate) for s, d in parts])


def measure(signal, rate=RATE, block=4096):
    meter = Loudness(rate, signal.shape[1])
    for start in range(0, len(signal), block):  # in player-sized pieces, as the live meter gets it
        meter.add(signal[start:start + block])
    return meter


def series(meter, steps):
    return meter.trailing(steps, meter.count)


def test_filters_at_48k_are_the_published_ones():
    shelf, highpass = loudness.k_weighting(48000)
    for got, want in ((shelf, SHELF_48K), (highpass, HIGHPASS_48K)):
        for g, w in zip(got[0] + got[1], want[0] + want[1]):
            assert g == pytest.approx(w, abs=1e-12)


@pytest.mark.parametrize("rate", [44100, 48000, 96000, 192000])
def test_k_weighting_shape_holds_at_every_rate(rate):
    """+4 dB shelf above ~2 kHz, the 997 Hz gain that -0.691 cancels, and the low cut."""
    def gain_db(f):
        z = np.exp(-2j * np.pi * f / rate)
        h = 1.0
        for b, a in loudness.k_weighting(rate):
            h *= (b[0] + b[1] * z + b[2] * z * z) / (a[0] + a[1] * z + a[2] * z * z)
        return 20 * math.log10(abs(h))
    assert gain_db(997) == pytest.approx(0.691, abs=0.01)
    assert gain_db(10000) == pytest.approx(4.0, abs=0.1)
    assert gain_db(20) < -10


# -- EBU Tech 3341, Table 1 -------------------------------------------------------------
@pytest.mark.parametrize("rate", [48000, 44100])
@pytest.mark.parametrize("level", [-23.0, -33.0])  # cases 1 and 2
def test_cases_1_and_2_steady_tone(level, rate):
    meter = measure(tone(20, level, rate), rate)
    assert integrated(meter.powers) == pytest.approx(level, abs=0.1)
    assert np.nanmax(np.abs(series(meter, 40)[100:] - level)) <= 0.1   # momentary
    assert np.nanmax(np.abs(series(meter, 300)[400:] - level)) <= 0.1  # short-term


@pytest.mark.parametrize("parts", [
    [(10, -36), (60, -23), (10, -36)],                         # case 3: the relative gate
    [(10, -72), (10, -36), (60, -23), (10, -36), (10, -72)],   # case 4: both gates
    [(20, -26), (20.1, -20), (20, -26)],                       # case 5
])
def test_cases_3_to_5_integrated(parts):
    assert integrated(measure(tones(*parts)).powers) == pytest.approx(-23.0, abs=0.1)


def test_case_6_five_channels():
    levels = [-28.0, -28.0, -24.0, -30.0, -30.0]  # L R C Ls Rs
    signal = np.concatenate([tone(20, d, channels=1) for d in levels], axis=1)
    assert integrated(measure(signal).powers) == pytest.approx(-23.0, abs=0.1)


def test_case_9_short_term_is_constant_after_3_s():
    signal = tones(*[(1.34, -20), (1.66, -30)] * 5)
    s = series(measure(signal), 300)
    assert np.nanmax(np.abs(s[300:] - -23.0)) <= 0.1


def test_case_10_max_short_term_each_file():
    for i in range(20):
        signal = tones((i * 0.15, None), (3, -23), (1, None))
        assert np.nanmax(series(measure(signal), 300)) == pytest.approx(-23.0, abs=0.1)


def test_case_11_max_short_term_live():
    signal = tones(*[part for i in range(20) for part in ((i * 0.15, None), (3, -38 + i), (3 - i * 0.15, None))])
    s = series(measure(signal), 300)
    for i in range(20):  # each 6 s segment: windows ending from its tone up to the next tone's start
        assert np.nanmax(s[i * 600 + 299: (i + 1) * 600 + (i + 1) * 15]) == pytest.approx(-38 + i, abs=0.1)


def test_case_12_momentary_is_constant_after_1_s():
    signal = tones(*[(0.18, -20), (0.22, -30)] * 25)
    m = series(measure(signal), 40)
    assert np.nanmax(np.abs(m[100:] - -23.0)) <= 0.1


def test_case_13_max_momentary_each_file():
    for i in range(20):
        signal = tones((i * 0.02, None), (0.4, -23), (1, None))
        assert np.nanmax(series(measure(signal), 40)) == pytest.approx(-23.0, abs=0.1)


def test_case_14_max_momentary_live():
    signal = tones(*[part for i in range(20) for part in ((i * 0.02, None), (0.4, -38 + i), (0.4 - i * 0.02, None))])
    m = series(measure(signal), 40)
    for i in range(20):
        assert np.nanmax(m[i * 80 + 39: (i + 1) * 80 + 2 * (i + 1)]) == pytest.approx(-38 + i, abs=0.1)


# -- EBU Tech 3342, Table 1 -------------------------------------------------------------
@pytest.mark.parametrize("levels, lra", [
    ([-20, -30], 10), ([-20, -15], 5), ([-40, -20], 20),  # cases 1-3
    ([-50, -35, -20, -35, -50], 15),                      # case 4
])
def test_loudness_range_cases(levels, lra):
    signal = tones(*[(20, d) for d in levels])
    assert loudness_range(measure(signal).powers) == pytest.approx(lra, abs=1.0)


def test_reset_starts_a_new_measurement_but_keeps_the_windows():
    meter = measure(tone(10, -30))
    meter.reset()
    for start in range(0, RATE * 10, 4096):
        meter.add(tone(10, -20)[start:start + 4096])
    assert integrated(meter.measured()) == pytest.approx(-20.0, abs=0.1)
    assert meter.seconds() == pytest.approx(10.0, abs=0.02)


def test_silence_has_no_integrated_loudness():
    assert integrated(measure(tone(5, None)).powers) is None
    assert loudness_range(measure(tone(5, None)).powers) is None
