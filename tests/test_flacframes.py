import numpy as np
import soundfile as sf
from ironbark.tagio.flac import FLACFile

from ironbark.flacframes import build_index


def write(path, seconds, rate=44100, subtype="PCM_16", quiet_then_loud=False):
    n = int(seconds * rate)
    rng = np.random.default_rng(1)
    noise = rng.uniform(-1, 1, size=(n, 2))
    if quiet_then_loud:
        noise[: n // 2] *= 0.001  # near-silence compresses far better than loud noise
    sf.write(path, (noise * 0.8).astype("float32"), rate, format="FLAC", subtype=subtype)
    return FLACFile(path).info


def test_index_covers_every_sample(tmp_path):
    info = write(tmp_path / "a.flac", 5, rate=96000, subtype="PCM_24")
    idx = build_index(tmp_path / "a.flac")
    assert idx.sample_rate == 96000
    assert idx.samples[0] == 0 and idx.samples[-1] == info.total_samples
    assert np.all(np.diff(idx.samples) > 0)   # no bogus frames out of order
    steps = set(np.diff(idx.samples[:-1]).tolist())
    assert len(steps) == 1                    # fixed block size: every gap identical


def test_average_matches_file_and_live_bitrate_follows_the_music(tmp_path):
    info = write(tmp_path / "a.flac", 6, quiet_then_loud=True)
    idx = build_index(tmp_path / "a.flac")
    whole = idx.bitrate_at(3.0, window=6.0)
    assert abs(whole - info.bitrate) / info.bitrate < 0.05
    assert idx.bitrate_at(1.0) < idx.bitrate_at(5.0) / 2   # quiet half is much cheaper
