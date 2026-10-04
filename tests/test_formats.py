"""0.9.0: format flags — HDCD packets, the MQA signal and pre-emphasis, found and reported only."""

import shutil
import subprocess

import numpy as np
import pytest
import soundfile as sf
from ironbark.tagio.flac import CueSheet, CueSheetTrack, FLACFile
from ironbark.tagio.flac import CueSheetIndex as CueSheetTrackIndex

from ironbark import formats
from ironbark.analysis import analyse
from ironbark.details import describe_analysis, describe_flags
from ironbark.formats import (HdcdScanner, MqaScanner, find_mqa, hdcd_gain_db, mqa_original_rate,
                                pre_emphasis, scan)

RATE = 44100


def noise16(seconds, rate=RATE, channels=2, seed=1):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal((int(seconds * rate), channels)) * 3000).clip(-32000, 32000).astype(np.int16)


def bits_of(value: int, width: int) -> list[int]:
    """`value` as `width` bits in time order (first = highest), as an encoder sends them."""
    return [(value >> (width - 1 - k)) & 1 for k in range(width)]


def hdcd_packet_a(raw: int) -> list[int]:
    return bits_of((0x7E0FA005 << 8) | raw, 40)


def hdcd_packet_b(code: int) -> list[int]:
    return bits_of((0x7E0FA006 << 16) | (code << 8) | (~code & 0xFF), 48)


def scramble(d: np.ndarray) -> np.ndarray:
    """The bits to store so that descrambling (s[n] ^ s[n-5] ^ s[n-23]) gives back `d`."""
    s = np.zeros(len(d), dtype=np.uint8)
    for n in range(len(d)):
        s[n] = d[n] ^ (s[n - 5] if n >= 5 else 0) ^ (s[n - 23] if n >= 23 else 0)
    return s


def with_hdcd(audio: np.ndarray, packet: list[int], every: float = 0.5, rate=RATE,
              channels=(0, 1)) -> np.ndarray:
    """16-bit audio whose lowest bits carry `packet` every `every` seconds in the given channels."""
    out = audio.copy()
    for ch in channels:
        d = np.zeros(len(out), dtype=np.uint8)
        for start in range(rate // 10, len(out) - len(packet), int(every * rate)):
            d[start:start + len(packet)] = packet
        out[:, ch] = (out[:, ch] & ~1) | scramble(d).astype(np.int16)
    return out


def flac16(path, data, rate=RATE):
    sf.write(path, data, rate, format="FLAC", subtype="PCM_16")
    return path


# -- HDCD ------------------------------------------------------------------------------
def test_hdcd_gain_codes():
    assert hdcd_gain_db(0) == 0.0
    assert hdcd_gain_db(1) == -0.5
    assert hdcd_gain_db(4) == -2.0
    assert hdcd_gain_db(15) == -7.5


def test_hdcd_packet_a_found_with_peak_extend_and_gain(tmp_path):
    # raw byte 0x12: peak extend (bit 4) + gain bits 010 -> control 0x12 + 2 = 0x14 -> gain 4 = -2 dB
    path = flac16(tmp_path / "hdcd.flac", with_hdcd(noise16(4), hdcd_packet_a(0x12)))
    result = scan(path).hdcd
    assert result.detected and result.effectual
    assert result.packet_type == "A"
    assert result.peak_extend == "permanent"
    assert result.max_gain_db == -2.0
    assert result.packets >= 14            # 7 or 8 per channel over 4 s
    assert result.errors == 0


def test_hdcd_packet_b_found(tmp_path):
    path = flac16(tmp_path / "hdcd.flac", with_hdcd(noise16(3), hdcd_packet_b(0x20)))  # transient filter only
    result = scan(path).hdcd
    assert result.detected and result.packet_type == "B"
    assert result.transient_filter and result.peak_extend == "never" and result.max_gain_db == 0.0
    assert not result.effectual             # HDCD, but nothing a decoder would change


def test_hdcd_b_with_a_bad_check_is_not_a_packet(tmp_path):
    bad = bits_of((0x7E0FA006 << 16) | (0x10 << 8) | 0x00, 48)  # second byte isn't the inverse
    result = scan(flac16(tmp_path / "x.flac", with_hdcd(noise16(3), bad))).hdcd
    assert not result.detected and result.packets == 0 and result.errors > 0


def test_hdcd_needs_every_channel(tmp_path):
    one = with_hdcd(noise16(3), hdcd_packet_a(0x10), channels=(0,))
    result = scan(flac16(tmp_path / "x.flac", one)).hdcd
    assert result.packets > 0 and not result.detected


def test_hdcd_packets_cross_block_edges():
    """Fed in tiny blocks, so packets straddle block boundaries: still all found."""
    audio = with_hdcd(noise16(2), hdcd_packet_a(0x10), every=0.25)
    block = (audio.astype(np.int32) << 16)
    whole, pieces = HdcdScanner(2, RATE), HdcdScanner(2, RATE)
    whole.add(block)
    for start in range(0, len(block), 37):
        pieces.add(block[start:start + 37])
    assert whole.result() == pieces.result()
    assert whole.result().packets == 2 * 8   # at 0.1, 0.35 … 1.85 s


def test_plain_audio_has_no_hdcd(tmp_path):
    result = scan(flac16(tmp_path / "x.flac", noise16(5))).hdcd
    assert not result.detected and result.packets == 0


def test_hdcd_only_checked_in_16_bit(tmp_path):
    path = tmp_path / "x.flac"
    sf.write(path, noise16(1).astype(np.float64) / 32768, RATE, format="FLAC", subtype="PCM_24")
    assert scan(path).hdcd is None


def test_intermittent_peak_extend():
    scanner = HdcdScanner(1, RATE)
    d = np.zeros(RATE, dtype=np.uint8)
    d[1000:1040] = hdcd_packet_a(0x10)     # peak extend on
    d[20000:20040] = hdcd_packet_a(0x00)   # off
    samples = (scramble(d).astype(np.int32) << 16)[:, None]
    scanner.add(samples)
    assert scanner.result().peak_extend == "intermittent"
    assert scanner.result().detected       # mono: one channel is every channel


# -- MQA -------------------------------------------------------------------------------
def mqa_stereo(code: int, provenance: int, bits=24, bit=16, at_frame=5000, frames=4 * 48000, seed=2):
    """Stereo int32 (left-justified) whose left XOR right carries the MQA sync word ending at
    `at_frame`, then the rate code (frames +3..+7) and provenance (frames +29..+33)."""
    rng = np.random.default_rng(seed)
    left = (rng.integers(-2**23, 2**23, frames) << 8).astype(np.int64)
    if bits == 16:
        left = (left >> 16) << 16
    stream = rng.integers(0, 2, frames)
    stream[at_frame - 35: at_frame + 1] = bits_of(formats.MQA_SYNC, 36)
    stream[at_frame + 3: at_frame + 8] = bits_of(code, 5)
    stream[at_frame + 29: at_frame + 34] = bits_of(provenance, 5)
    right = left ^ (stream << bit)
    return np.stack([left, right], axis=1).astype(np.int32)


def test_mqa_rate_codes():
    assert mqa_original_rate(0b01001) == 96000      # 48 kHz family, ×2
    assert mqa_original_rate(0b01100) == 352800     # 44.1 kHz family, ×8
    assert mqa_original_rate(0b00000) == 44100
    assert mqa_original_rate(0b01110) == 44100 * 128 * 2   # above ×16 counts double (DSD)


@pytest.mark.parametrize("bits", [16, 24])
def test_mqa_sync_found_with_original_rate_and_studio(tmp_path, bits):
    data = mqa_stereo(code=0b01001, provenance=16, bits=bits)
    path = tmp_path / "mqa.flac"
    sf.write(path, data, 48000, format="FLAC", subtype=f"PCM_{bits}")
    result = scan(path).mqa
    assert result.detected and result.original_rate == 96000 and result.studio and result.bit == 16


def test_mqa_found_one_bit_higher_and_not_studio():
    data = mqa_stereo(code=0b01100, provenance=3, bit=17)
    scanner = MqaScanner(48000, 2, 24)
    for start in range(0, len(data), 1000):
        scanner.add(data[start:start + 1000])
    result = scanner.result()
    assert result.detected and result.bit == 17 and result.original_rate == 352800 and not result.studio


def test_mqa_sync_after_3_seconds_does_not_count():
    data = mqa_stereo(code=1, provenance=0, at_frame=int(3.5 * 48000))
    xor = (data[:, 0] ^ data[:, 1]).view(np.uint32)
    assert find_mqa(xor) is not None
    assert find_mqa(xor, limit=3 * 48000) is None


def test_plain_audio_is_not_mqa(tmp_path):
    rng = np.random.default_rng(5)
    path = tmp_path / "x.flac"
    sf.write(path, rng.uniform(-0.5, 0.5, (48000 * 4, 2)), 48000, format="FLAC", subtype="PCM_24")
    assert not scan(path).mqa.detected


def test_mqa_not_checked_in_mono(tmp_path):
    path = tmp_path / "x.flac"
    sf.write(path, np.zeros((RATE, 1)), RATE, format="FLAC", subtype="PCM_16")
    assert scan(path).mqa is None


def test_mqa_encoder_tag_is_read(tmp_path):
    path = flac16(tmp_path / "x.flac", noise16(1))
    tags = FLACFile(str(path))
    tags["MQAENCODER"] = "MQAEncode v1.1"
    tags.save()
    result = scan(path).mqa
    assert result.encoder == "MQAEncode v1.1" and not result.detected
    assert "no MQA signal" in describe_flags(scan(path))[0][1]


# -- pre-emphasis ------------------------------------------------------------------------
def test_pre_emphasis_from_a_folder_cue(tmp_path):
    flac16(tmp_path / "01 One.flac", noise16(0.2))
    flac16(tmp_path / "02 Two.flac", noise16(0.2))
    (tmp_path / "album.cue").write_text(
        'FILE "01 One.flac" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n'
        'FILE "02 Two.flac" WAVE\n  TRACK 02 AUDIO\n    FLAGS DCP PRE\n    INDEX 01 00:00:00\n',
        encoding="utf-8")
    assert not pre_emphasis(tmp_path / "01 One.flac").flagged
    found = pre_emphasis(tmp_path / "02 Two.flac")
    assert found.flagged and found.sources == ["album.cue: FLAGS PRE on track 2"]


def test_pre_emphasis_cue_with_stale_names_falls_back_to_track_number(tmp_path):
    path = flac16(tmp_path / "renamed.flac", noise16(0.2))
    tags = FLACFile(str(path))
    tags["TRACKNUMBER"] = "2/10"
    tags.save()
    (tmp_path / "disc.cue").write_text(
        'FILE "Track01.wav" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n'
        'FILE "Track02.wav" WAVE\n  TRACK 02 AUDIO\n    FLAGS PRE\n    INDEX 01 00:00:00\n', encoding="utf-8")
    assert pre_emphasis(path).flagged


def test_pre_emphasis_from_the_flac_cuesheet_block(tmp_path):
    path = flac16(tmp_path / "image.flac", noise16(1))
    block = CueSheet()
    block.media_catalog_number, block.lead_in_samples, block.compact_disc = "", 88200, True
    for number, offset, pre in ((1, 0, False), (2, 588 * 30, True)):
        track = CueSheetTrack(number, offset, pre_emphasis=pre)
        track.indexes.append(CueSheetTrackIndex(1, 0))
        block.tracks.append(track)
    block.tracks.append(CueSheetTrack(170, RATE))
    tags = FLACFile(str(path))
    tags.cuesheet = block
    tags.save()
    found = pre_emphasis(path)
    assert found.flagged and found.sources == ["the file's own cue sheet, track 2"]


@pytest.mark.parametrize("name,value,flagged", [("PRE_EMPHASIS", "1", True), ("PRE-EMPHASIS", "yes", True),
                                                ("PRE_EMPHASIS", "on", True), ("PRE_EMPHASIS", "0", False),
                                                ("EMPHASIS", "no", False)])
def test_pre_emphasis_tags(tmp_path, name, value, flagged):
    path = flac16(tmp_path / "x.flac", noise16(0.2))
    tags = FLACFile(str(path))
    tags[name] = value
    tags.save()
    assert pre_emphasis(path).flagged is flagged


# -- in the Check -----------------------------------------------------------------------
def test_analysis_carries_the_flags_and_check_reports_them(tmp_path):
    path = flac16(tmp_path / "hdcd.flac", with_hdcd(noise16(3), hdcd_packet_a(0x10)))
    tags = FLACFile(str(path))
    tags["PRE_EMPHASIS"] = "1"
    tags.save()
    result = analyse(path, 16, 0)
    assert result.flags.hdcd.detected and result.flags.pre_emphasis.flagged
    rows = dict(describe_analysis(result))
    assert rows["HDCD"].startswith("⚠ HDCD-encoded (peak extend always on)")
    assert "would expand the loudest peaks." in rows["HDCD"] and "turn parts down" not in rows["HDCD"]
    assert rows["Pre-emphasis"].startswith("⚠") and "PRE_EMPHASIS tag" in rows["Pre-emphasis"]


def test_check_says_none_when_nothing_is_flagged(tmp_path):
    result = analyse(flac16(tmp_path / "x.flac", noise16(2)), 16, 0)
    rows = dict(describe_analysis(result))
    assert rows["Format flags"].startswith("✓ None")
    assert "HDCD" in rows["Format flags"] and "pre-emphasis" in rows["Format flags"]


def test_flags_never_change_the_audio_check(tmp_path):
    """The flags only read: the MD5 check and the samples are the same as before."""
    audio = with_hdcd(noise16(2), hdcd_packet_a(0x10))
    path = flac16(tmp_path / "x.flac", audio)
    stored = FLACFile(str(path)).info.md5_signature
    result = analyse(path, 16, stored)
    assert result.md5_ok in (True, None)
    assert np.array_equal(sf.read(path, dtype="int16")[0], audio)


# -- an independent check -------------------------------------------------------------------
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg not installed")
@pytest.mark.parametrize("packet", [hdcd_packet_a(0x12), hdcd_packet_b(0x20), hdcd_packet_b(0x16)])
def test_hdcd_agrees_with_ffmpegs_decoder(tmp_path, packet):
    """FFmpeg's own hdcd filter, run on the same crafted files, reports the same."""
    path = flac16(tmp_path / "x.flac", with_hdcd(noise16(4), packet))
    log = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af",
                          "hdcd=disable_autoconvert=0", "-f", "null", "-"], capture_output=True, text=True).stderr
    line = [part for part in log.splitlines() if "HDCD detected" in part][-1]
    ours = scan(path).hdcd
    assert "HDCD detected: yes" in line and ours.detected
    assert f"max_gain_adj: {ours.max_gain_db:.1f} dB" in line
    assert {"never": "never enabled", "permanent": "enabled permanently"}[ours.peak_extend] in line
    assert ("transient_filter: detected" in line) == ours.transient_filter


def test_check_wording_for_gain_adjust_only():
    """Like a real album's tags: gain adjust, no peak extend."""
    flags = formats.FormatFlags(hdcd=formats.HdcdResult(detected=True, packets=10, packet_type="B",
                                                        max_gain_db=-4.0))
    text = dict(describe_flags(flags))["HDCD"]
    assert text.startswith("⚠ HDCD-encoded (gain adjust down to -4.0 dB): an HDCD decoder would turn parts "
                           "down by up to 4.0 dB.")
    assert "peak" not in text


@pytest.mark.parametrize("width", [5, 32, 36])
def test_rolling_bit_windows_match_the_plain_sum(width):
    bits = np.random.default_rng(3).integers(0, 2, 500).astype(np.uint8)
    fast = formats._windows(bits, width)
    for n in (0, 3, width - 1, width, 250, 499):
        assert int(fast[n]) == sum(int(bits[n - k]) << k for k in range(width) if n - k >= 0)
