import math
from pathlib import Path

import numpy as np
import soundfile as sf

from ironbark.analysis import true_peak
from ironbark.details import output_line
from ironbark.engine import OutputInfo, Player
from ironbark.library import Track


def test_true_peak_finds_overs_between_samples(tmp_path):
    # A tone at a quarter of the sample rate, phase-shifted 45 degrees: every sample lands at
    # 0.707 of the wave's real height. Height 1.3 -> samples peak at 0.92 (no sample clips),
    # but the wave a DAC rebuilds reaches 1.3: over full scale.
    n = np.arange(44100 * 2)
    wave = 1.3 * np.sin(np.pi / 2 * n + np.pi / 4)
    path = tmp_path / "overs.wav"
    sf.write(path, np.column_stack([wave, wave]).astype("float32"), 44100, subtype="FLOAT")
    assert np.abs(wave).max() < 0.93
    assert abs(true_peak(path) - 1.3) < 0.03


def test_trim_applies_only_to_its_own_track():
    p = Player()
    a, b = Track(Path("a.flac"), "a"), Track(Path("b.flac"), "b")
    p.set_tracks([a, b])
    p.set_volume(1.0)
    p.set_trim(a.path, 0.89)
    p.index = 0
    assert math.isclose(p._gain(), 0.89)
    p.index = 1
    assert p._gain() == 1.0


def test_output_line_names_the_guard_and_stays_green():
    out = OutputInfo("DAC", 44100, 48000, exclusive=True, hardware_volume=True)
    text, tone = output_line(out, 1.0, trim=10 ** (-1.1 / 20))
    assert "clipping guard -1.1 dB" in text and tone == "good"
