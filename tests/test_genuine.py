"""0.6.0: the "is it genuine?" clues, clipping, true peak / PLR, stereo figures and album DR."""

import numpy as np
import pytest
import soundfile as sf

from ironbark.analysis import ClipCounter, album_measure, analyse, bits_in_use, find_cutoff
from ironbark.tools.details import (
    describe_album,
    describe_analysis,
    describe_bits,
    describe_clipping,
    describe_range,
    describe_stereo,
)

RATE = 44100


def noise(seconds, rate=RATE, amp=0.25, channels=2, seed=1):
    rng = np.random.default_rng(seed)
    return (amp * rng.standard_normal((int(seconds * rate), channels))).clip(-0.99, 0.99)


def flac(path, data, rate=RATE, subtype="PCM_16"):
    sf.write(path, data, rate, format="FLAC", subtype=subtype)
    return path


def lowpass(data, rate, cutoff):
    """A brick wall: every frequency above `cutoff` removed (what a lossy encoder or a CD-rate
    source leaves behind)."""
    spectrum = np.fft.rfft(data, axis=0)
    spectrum[np.fft.rfftfreq(len(data), 1 / rate) > cutoff] = 0
    return np.fft.irfft(spectrum, n=len(data), axis=0)


def resample(data, rate_in, rate_out):
    """Ideal (FFT) resampling, as a mastering tool would upsample a CD file."""
    n_out = int(len(data) * rate_out / rate_in)
    spectrum = np.fft.rfft(data, axis=0)
    padded = np.zeros((n_out // 2 + 1, data.shape[1]), dtype=complex)
    padded[: len(spectrum)] = spectrum
    return np.fft.irfft(padded, n=n_out, axis=0) * (n_out / len(data))


# -- bits ------------------------------------------------------------------------
def test_bits_in_use():
    assert bits_in_use(0, 24) is None
    assert bits_in_use(1 << 8, 24) == 24          # lowest bit of a 24-bit sample (left-justified)
    assert bits_in_use(1 << 16, 24) == 16         # never below bit 16: 16-bit padded
    assert bits_in_use(-(1 << 16), 24) == 16      # negative samples too
    assert bits_in_use(0xFFFFFFFF, 16) == 16


def test_genuine_24_bit_uses_all_its_bits(tmp_path):
    result = analyse(flac(tmp_path / "a.flac", noise(4), subtype="PCM_24"), 24, 0)
    assert result.bits == 24 and result.bits_used == 24
    assert describe_bits(result).startswith("✓ All 24 bits")


def test_16_bit_padded_to_24_is_found(tmp_path):
    cd = np.round(noise(4) * 32767) / 32768          # exact 16-bit values…
    result = analyse(flac(tmp_path / "a.flac", cd, subtype="PCM_24"), 24, 0)  # …stored as 24-bit
    assert result.bits_used == 16 and result.verdict == "padded"
    assert "16-bit recording stored as 24-bit" in describe_bits(result)


# -- where the sound stops -------------------------------------------------------------
def test_full_range_cd_is_not_flagged(tmp_path):
    result = analyse(flac(tmp_path / "a.flac", noise(6)), 16, 0)
    assert result.cutoff_hz > 21000 and result.verdict == ""
    assert describe_range(result).startswith("✓ Sound reaches")


def test_brick_wall_at_16k_suggests_a_lossy_source(tmp_path):
    result = analyse(flac(tmp_path / "a.flac", lowpass(noise(6), RATE, 16000)), 16, 0)
    assert result.cutoff_hz == pytest.approx(16000, abs=300) and result.cutoff_sharp
    assert result.verdict == "lossy"
    assert "lossy file" in describe_range(result)


def test_real_mp3_made_into_flac_is_flagged(tmp_path):
    mp3 = tmp_path / "a.mp3"
    sf.write(mp3, noise(6).astype("float32"), RATE, format="MP3", bitrate_mode="CONSTANT",
             compression_level=0.9)  # a low bitrate: the encoder cuts the top end hard
    decoded, rate = sf.read(mp3)
    result = analyse(flac(tmp_path / "a.flac", decoded, rate), 16, 0)
    assert result.verdict == "lossy", (result.cutoff_hz, result.cutoff_sharp)


def test_gentle_roll_off_is_the_recording_not_a_lossy_copy(tmp_path):
    data = noise(6)
    spectrum = np.fft.rfft(data, axis=0)
    f = np.fft.rfftfreq(len(data), 1 / RATE)[:, None]
    spectrum /= (1 + (f / 3000) ** 2)                 # 12 dB/octave from 3 kHz: a mellow, dull tape
    result = analyse(flac(tmp_path / "a.flac", np.fft.irfft(spectrum, n=len(data), axis=0), subtype="PCM_24"), 24, 0)
    assert result.verdict == ""
    assert not describe_range(result).startswith("⚠")


def test_cd_upsampled_to_96k_is_flagged(tmp_path):
    data = resample(noise(5), RATE, 96000) * 0.8
    result = analyse(flac(tmp_path / "a.flac", data, 96000, subtype="PCM_24"), 24, 0)
    assert result.cutoff_hz == pytest.approx(22050, abs=400) and result.verdict == "upsampled"
    assert "upsampled from CD quality" in describe_range(result)


def test_real_hi_res_is_not_flagged(tmp_path):
    result = analyse(flac(tmp_path / "a.flac", noise(4, rate=96000), 96000, subtype="PCM_24"), 24, 0)
    assert result.cutoff_hz > 40000 and result.verdict == ""
    assert "real high-resolution" in describe_range(result)


def test_find_cutoff_on_silence():
    assert find_cutoff(np.zeros(4097), RATE) == (None, False)


# -- clipping, peaks ---------------------------------------------------------------------
def test_clip_counter_runs_across_blocks():
    c = ClipCounter(3)
    c.add(np.array([0, 1, 1], bool))                  # a run carried into the next block…
    c.add(np.array([1, 0, 1, 1, 0, 1, 1, 1, 1], bool))  # …(3 long), then 2 (too short), then 4
    c.add(np.array([1, 1, 1], bool))                  # the 4 keeps going: 7
    c.finish()
    assert (c.runs, c.samples) == (2, 10)


def test_clipped_runs_and_peak_position(tmp_path):
    data = noise(4, amp=0.1)
    data[RATE * 2: RATE * 2 + 5] = 1.0                # five samples stuck at full scale, at 0:02
    data[RATE * 3, 0] = 1.0                           # a lone full-scale sample: not a clip run
    result = analyse(flac(tmp_path / "a.flac", data), 16, 0)
    assert result.clip_runs == 2 and result.clipped_samples == 10  # both channels
    assert result.peak_at == pytest.approx(2.0, abs=0.01)
    assert describe_clipping(result).startswith("⚠ 2 runs")


def test_true_peak_and_plr(tmp_path):
    # A full-scale sine at a quarter of the rate, phased so the samples miss the crests:
    # the wave peaks 3 dB above the samples.
    t = np.arange(RATE * 4)
    wave = np.sin(2 * np.pi * t / 4 + np.pi / 4) * 10 ** (-6 / 20) / np.sin(np.pi / 4)
    data = np.column_stack([wave, wave]) * np.sin(np.pi / 4)
    result = analyse(flac(tmp_path / "a.flac", data, subtype="PCM_24"), 24, 0)
    assert result.peak_dbfs == pytest.approx(-9.03, abs=0.05)
    assert result.true_peak_db == pytest.approx(-6.0, abs=0.1)
    assert result.plr == pytest.approx(result.true_peak_db - result.lufs)


# -- stereo -----------------------------------------------------------------------------
def test_mono_stored_as_stereo(tmp_path):
    mono = noise(3, channels=1)
    result = analyse(flac(tmp_path / "a.flac", np.column_stack([mono, mono])), 16, 0)
    assert result.identical_channels and result.correlation == pytest.approx(1.0)
    assert "mono recording stored as stereo" in describe_stereo(result)[0][1]


def test_inverted_channel_is_flagged(tmp_path):
    mono = noise(3, channels=1)
    result = analyse(flac(tmp_path / "a.flac", np.column_stack([mono, -mono])), 16, 0)
    assert result.correlation == pytest.approx(-1.0, abs=0.001)
    assert "inverted" in describe_stereo(result)[0][1]


def test_unrelated_channels_and_balance(tmp_path):
    data = noise(3) * np.array([1.0, 0.5])              # right 6 dB quieter
    result = analyse(flac(tmp_path / "a.flac", data), 16, 0)
    assert abs(result.correlation) < 0.05
    assert result.balance_db == pytest.approx(6.02, abs=0.1)
    assert "left louder by 6.0 dB" in describe_stereo(result)[0][1]


def test_dc_offset(tmp_path):
    clean = analyse(flac(tmp_path / "a.flac", noise(3, amp=0.1)), 16, 0)
    assert dict(describe_stereo(clean))["DC offset"].startswith("✓ None")
    shifted = analyse(flac(tmp_path / "b.flac", noise(3, amp=0.1) + 0.02), 16, 0)
    assert shifted.dc_offset[0] == pytest.approx(0.02, abs=0.002)
    assert dict(describe_stereo(shifted))["DC offset"].startswith("⚠ L -34 dBFS")


# -- the whole check, and the album ---------------------------------------------------------
def test_check_rows_in_order(tmp_path):
    result = analyse(flac(tmp_path / "a.flac", noise(4), subtype="PCM_24"), 24, 0)
    labels = [label for label, _ in describe_analysis(result)]
    assert labels == ["Lossless check", "Bit depth", "Frequencies", "Format flags", "Dynamic range", "Peak level",
                      "True peak", "Clipping", "Loudness", "Stereo", "DC offset"]


def test_album_dr_is_the_average_of_the_tracks(tmp_path):
    def track(name, dr_target_amp):
        # A loud steady tone with two short bursts: DR set by the tone level under the bursts.
        t = np.arange(RATE * 30) / RATE
        x = dr_target_amp * np.sin(2 * np.pi * 1000 * t)
        x[RATE * 5: RATE * 5 + 100] = 0.9
        x[RATE * 20: RATE * 20 + 100] = 0.9
        return flac(tmp_path / name, np.column_stack([x, x]), subtype="PCM_24")

    files = [track("1.flac", 0.9 * 10 ** (-6 / 20)), track("2.flac", 0.9 * 10 ** (-12 / 20))]
    single = [analyse(f, 24, 0).dr for f in files]
    album = album_measure(files)
    assert single == [6, 12]
    assert (album.dr, album.dr_low, album.dr_high, album.tracks) == (9, 6, 12, 2)
    from ironbark.loudness import album_loudness
    assert (album.lufs, album.lra) == pytest.approx(album_loudness(files))
    assert describe_album(album).startswith("DR9 (tracks DR6–12)")


def test_silence_is_not_called_mono(tmp_path):
    result = analyse(flac(tmp_path / "a.flac", np.zeros((RATE * 2, 2))), 16, 0)
    assert not result.identical_channels
    assert not any("mono" in text for _, text in describe_stereo(result))


def test_a_file_that_stops_decoding_part_way_is_reported_as_damaged(tmp_path):
    path = flac(tmp_path / "a.flac", noise(20))
    raw = bytearray(path.read_bytes())
    middle = len(raw) // 2
    raw[middle:middle + 20000] = bytes(20000)       # a stretch of the file wiped out
    path.write_bytes(bytes(raw))
    result = analyse(path, 16, 0)                    # no exception: the damage is the result
    assert result.damaged_at is not None and 0 < result.damaged_at < 20
    assert result.md5_ok is False
    assert describe_analysis(result)[0][1].startswith("✗ Damaged: the audio stops decoding at 0:")
