import numpy as np
import soundfile as sf

from ironbark.proof import SentRecord, apply_gain, compare_paths, describe_path

RNG = np.random.default_rng(7)
FREQS = [(220.0, 0.3), (1234.5, 0.2), (5000.0, 0.1), (11025.3, 0.05)]


def music(rate, seconds, delay=0.0, phases=(0.0, 1.0)):
    """A stand-in for music that can be generated exactly at any rate, so 'what Windows
    resampled' can be produced perfectly and compared with our own resampling."""
    t = np.arange(int(rate * seconds)) / rate - delay
    channels = [sum(a * np.sin(2 * np.pi * f * t + p * (i + 1)) for i, (f, a) in enumerate(FREQS))
                for p in phases]
    return np.stack(channels, axis=1)


def embed(x, before, after):
    return np.concatenate([np.zeros((before, x.shape[1])), x, np.zeros((after, x.shape[1]))])


def test_identical_audio_is_untouched():
    sent = music(48000, 1.0).astype(np.float32)
    check = compare_paths(sent, 48000, embed(sent, 1234, 500), 48000)
    assert check.verdict == "untouched" and check.identical
    assert abs(check.delay_ms - 1000 * 1234 / 48000) < 0.01


def test_a_level_change_is_measured():
    sent = music(48000, 1.0)
    check = compare_paths(sent, 48000, embed(sent * 10 ** (-3 / 20), 800, 800), 48000)
    assert check.verdict == "changed" and abs(check.level_db + 3) < 0.01


def test_clean_resampling_with_a_fractional_delay_is_only_resampling():
    sent = music(44100, 1.5)
    got = embed(music(48000, 1.5, delay=0.37 / 48000), 3000, 3000)  # Windows' ideal resampler
    check = compare_paths(sent, 44100, got, 48000)
    assert check.resampled == (44100, 48000)
    assert check.verdict == "resampled_only", check
    assert abs(check.level_db) < 0.05


def test_left_right_mixing_is_flagged():
    sent = music(48000, 1.0)
    got = sent.copy()
    got[:, 0] += 0.2 * sent[:, 1]
    check = compare_paths(sent, 48000, embed(got, 100, 100), 48000)
    assert check.crosstalk_db > -20
    assert any(label == "Left/right" for label, _, _ in describe_path(check))


def test_other_sounds_show_as_a_difference():
    sent = music(48000, 1.0)
    got = sent + 0.05 * RNG.standard_normal(sent.shape)
    check = compare_paths(sent, 48000, embed(got, 100, 100), 48000)
    assert check.verdict == "changed" and -40 < check.difference_db < -10


def test_flipped_polarity():
    sent = music(48000, 1.0)
    check = compare_paths(sent, 48000, embed(-sent, 50, 50), 48000)
    assert check.polarity_flipped


def test_silence_is_too_quiet_to_check():
    assert compare_paths(np.zeros((48000, 2)), 48000, np.zeros((50000, 2)), 48000).verdict == "too_quiet"


# -- the player's own output ----------------------------------------------------
def _flac(tmp_path, frames=20000):
    path = tmp_path / "t.flac"
    data = (RNG.standard_normal((frames, 2)) * 3000).astype(np.int16)
    sf.write(path, data, 44100, subtype="PCM_16")
    return path


def _send(record, path, start, gains, dtype="int32", block=4096, corrupt_block=None):
    with sf.SoundFile(str(path)) as f:
        f.seek(start)
        for k, gain in enumerate(gains):
            data = f.read(block, dtype=dtype, always_2d=True)
            if not len(data):
                break
            sent = apply_gain(data, gain)
            if k == corrupt_block:
                sent = sent.copy()
                sent[0, 0] ^= 1  # one bit of one sample
            record.add(1 if start == 0 else 2, start + k * block, sent, gain)  # each block's own first frame


def test_untouched_playback_is_proven_bit_perfect(tmp_path):
    path = _flac(tmp_path)
    record = SentRecord(path, 44100, "int32", 4096)
    _send(record, path, 0, [1.0] * 10)
    with sf.SoundFile(str(path)) as ref:
        record.check(ref)
    proof = record.summary()
    assert proof.complete and proof.untouched and proof.mismatch_at is None
    assert abs(proof.sent - 20000 / 44100) < 1e-9


def test_a_single_flipped_bit_is_caught_and_located(tmp_path):
    path = _flac(tmp_path)
    record = SentRecord(path, 44100, "int32", 4096)
    _send(record, path, 0, [1.0] * 10, corrupt_block=2)
    with sf.SoundFile(str(path)) as ref:
        record.check(ref)
    assert record.summary().mismatch_at == 2 * 4096 / 44100


def test_volume_is_reproduced_exactly_but_reported_as_a_change(tmp_path):
    path = _flac(tmp_path)
    for dtype in ("int32", "float32"):
        record = SentRecord(path, 44100, dtype, 4096)
        _send(record, path, 0, [1.0, 0.5, 0.5, 1.0, 1.0], dtype=dtype)
        with sf.SoundFile(str(path)) as ref:
            record.check(ref)
        proof = record.summary()
        assert proof.mismatch_at is None and proof.complete
        assert not proof.untouched and proof.min_gain == 0.5 and proof.changed_from == 4096 / 44100


def test_after_a_seek_the_new_stretch_is_checked_from_where_it_starts(tmp_path):
    path = _flac(tmp_path)
    record = SentRecord(path, 44100, "int32", 4096)
    _send(record, path, 0, [1.0] * 2)
    _send(record, path, 7000, [1.0] * 2)
    with sf.SoundFile(str(path)) as ref:
        record.check(ref)
    assert len(record.segments) == 2 and record.summary().complete


def test_blocks_of_any_length_are_checked_where_they_start(tmp_path):
    # Gapless playback joins tracks mid-block, so the pieces sent aren't all one block long.
    path = _flac(tmp_path)
    record = SentRecord(path, 44100, "int32", 4096)
    with sf.SoundFile(str(path)) as f:
        start = 0
        for size in (1000, 4096, 3096, 4096, 50):
            data = f.read(size, dtype="int32", always_2d=True)
            record.add(1, start, apply_gain(data, 1.0), 1.0)
            start += size
    with sf.SoundFile(str(path)) as ref:
        record.check(ref)
    proof = record.summary()
    assert proof.complete and proof.mismatch_at is None and abs(proof.sent - 12338 / 44100) < 1e-9
