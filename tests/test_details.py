from pathlib import Path

from ironbark.engine import OutputInfo
from ironbark.library import Track, format_rate
from ironbark.statustext import output_line


def test_format_rate():
    assert format_rate(44100) == "44.1 kHz"
    assert format_rate(96000) == "96 kHz"
    assert format_rate(88200) == "88.2 kHz"
    assert format_rate(22050) == "22.05 kHz"


def test_format_line():
    t = Track(Path("x.flac"), "x", sample_rate=96000, bits=24, channels=2, bitrate=2_860_123)
    assert t.format_line == "FLAC · 24-bit / 96 kHz · stereo · 2,860 kbps"
    assert Track(Path("x.flac"), "x").format_line == "FLAC"


def test_output_line_flags_resampling():
    text, tone = output_line(OutputInfo("Headphones", 96000, 48000, exclusive=False), 1.0)
    assert "resampled to 48 kHz" in text and tone == "warn"


def test_output_line_shared_same_rate_is_not_called_bit_perfect():
    text, tone = output_line(OutputInfo("Headphones", 48000, 48000, exclusive=False), 1.0)
    assert "bit-perfect" not in text and tone == "plain"


def test_output_line_exclusive():
    out = OutputInfo("Headphones", 96000, 48000, exclusive=True)
    assert output_line(out, 1.0) == ("→ Headphones · 96 kHz exclusive · bit-perfect ✓", "good")
    text, tone = output_line(out, 0.5)
    assert "not bit-perfect" in text and tone == "warn"


def test_output_line_when_stopped():
    assert output_line(None, 1.0) == ("", "plain")


def test_output_line_with_device_volume():
    out = OutputInfo("Headphones", 44100, 48000, exclusive=True, hardware_volume=True)
    assert output_line(out, 1.0) == ("→ Headphones · 44.1 kHz exclusive · bit-perfect ✓ · volume by device ✓", "good")


def test_output_line_without_device_volume_warns():
    out = OutputInfo("Headphones", 44100, 48000, exclusive=True, hardware_volume=False)
    text, tone = output_line(out, 1.0)
    assert "bit-perfect ✓" in text and "no device volume" in text and tone == "warn"
