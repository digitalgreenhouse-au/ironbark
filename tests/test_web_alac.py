"""The web player's Apple Lossless decoder (web/js/alac.js), against real ALAC files.

The files are encoded by libsndfile (Apple's own ALAC encoder, via mp4_maker) and, when it's
installed, by FFmpeg's, with its settings varied so the decoder meets each kind of frame:
uncompressed, every predictor order, short frames. Node.js decodes each one to a WAV with
alac.js, and the WAV must hold exactly the samples that went in. Skipped without Node.js.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from mp4_maker import write_aac_m4a, write_m4a

ALAC_JS = Path(__file__).resolve().parent.parent / "web" / "js" / "alac.js"
RNG = np.random.default_rng(14)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js isn't installed")

# Decodes each [m4a, wav] pair; prints one result per pair ("ok", or the error's name and message)
DECODE = """
const fs = require("fs");
const A = require(process.argv[1]);
(async () => {
  const out = [];
  for (const [m4a, wav] of JSON.parse(process.argv[2])) {
    try {
      const blob = await A.decodeToWav(await fs.openAsBlob(m4a));
      fs.writeFileSync(wav, new Uint8Array(await blob.arrayBuffer()));
      out.push("ok");
    } catch (e) {
      out.push(e.name + ": " + e.message);
    }
  }
  console.log(JSON.stringify(out));
})();
"""


def decode(*paths: Path) -> list[str]:
    pairs = [[str(p), str(p.with_suffix(".wav"))] for p in paths]
    run = subprocess.run(["node", "-e", DECODE, str(ALAC_JS), json.dumps(pairs)], capture_output=True, text=True,
                         encoding="utf-8", timeout=120)
    assert run.returncode == 0, run.stderr[-2000:]
    return json.loads(run.stdout)


def music(frames: int, channels: int, bits: int) -> np.ndarray:
    """Left-justified int32 samples: tones, noise, a stretch of silence (runs of zeros), a burst
    of loud noise (large residuals, uncompressed frames) and full scale both ways."""
    t = np.arange(frames)[:, None]
    x = (0.3 * np.sin(2 * np.pi * 220 * t / 44100 + np.arange(channels))
         + 0.1 * np.sin(2 * np.pi * 7000 * t / 44100) + RNG.normal(0, 0.01, (frames, channels)))
    if frames > 8000:
        x[1000:3000] = 0
        x[5000:5200] = RNG.uniform(-1, 1, (200, channels))
        x[6000], x[6001] = 1.0, -1.0
    full = 2 ** (bits - 1)
    return (np.clip(np.round(x * full), -full, full - 1).astype(np.int64) << (32 - bits)).astype(np.int32)


def decoded(path: Path) -> np.ndarray:
    data, _ = sf.read(path.with_suffix(".wav"), dtype="int32", always_2d=True)
    return data


@pytest.mark.parametrize("bits", [16, 24])
@pytest.mark.parametrize("channels", [1, 2])
def test_apples_encoder_decodes_exactly(tmp_path, bits, channels):
    cases = {
        "long.m4a": dict(frames=44100 * 2 + 321, per_chunk=5, junk=17),   # several chunks, other bytes between
        "first.m4a": dict(frames=30000, moov_first=True),                 # the index before the audio
        "short.m4a": dict(frames=777),                                     # one partial packet
    }
    want = {}
    for name, opts in cases.items():
        frames = opts.pop("frames")
        want[name] = music(frames, channels, bits)
        write_m4a(tmp_path / name, want[name], 44100, bits, **opts)
    assert decode(*(tmp_path / name for name in cases)) == ["ok"] * len(cases)
    for name, samples in want.items():
        info = sf.info(tmp_path / name.replace(".m4a", ".wav"))
        assert (info.samplerate, info.channels, info.subtype) == (44100, channels, f"PCM_{bits}")
        assert np.array_equal(decoded(tmp_path / name), samples), name


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg not installed (an extra, independent encoder)")
@pytest.mark.parametrize("bits", [16, 24])
def test_ffmpegs_encoder_decodes_exactly(tmp_path, bits):
    samples = music(44100 * 2 + 99, 2, bits)
    source = tmp_path / "src.wav"
    sf.write(source, samples, 48000, subtype=f"PCM_{bits}")
    settings = {
        "default": [],
        "uncompressed": ["-compression_level", "0"],
        "fast": ["-compression_level", "1"],
        "orders": ["-min_prediction_order", "1", "-max_prediction_order", "30"],
        "order3": ["-min_prediction_order", "3", "-max_prediction_order", "3"],
        "frames": ["-frame_size", "300"],
        "mono": ["-ac", "1"],
    }
    paths = []
    for name, extra in settings.items():
        path = tmp_path / f"{name}.m4a"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(source), "-c:a", "alac",
                        "-sample_fmt", "s16p" if bits == 16 else "s32p", *extra, str(path)], check=True)
        paths.append(path)
    assert decode(*paths) == ["ok"] * len(paths)
    for path in paths:
        if path.stem == "mono":   # (FFmpeg's own downmix: compared with FFmpeg's decode of it)
            ref = tmp_path / "mono-ref.wav"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-c:a", f"pcm_s{bits}le", str(ref)], check=True)
            want, _ = sf.read(ref, dtype="int32", always_2d=True)
        else:
            want = samples
        assert sf.info(path.with_suffix(".wav")).samplerate == 48000
        assert np.array_equal(decoded(path), want), path.stem


def test_aac_and_damaged_files_are_refused_not_decoded(tmp_path):
    aac = write_aac_m4a(tmp_path / "aac.m4a")
    good = write_m4a(tmp_path / "good.m4a", music(44100, 2, 16), 44100, 16)
    cut = tmp_path / "cut.m4a"
    cut.write_bytes(good.read_bytes()[: good.stat().st_size // 2])   # (the index is at the end: gone)
    noisy = tmp_path / "noisy.m4a"
    data = bytearray(good.read_bytes())
    data[200:4000] = RNG.integers(0, 256, 3800, dtype=np.uint8).tobytes()   # the first packets overwritten
    noisy.write_bytes(bytes(data))
    huge = tmp_path / "huge.m4a"
    data = bytearray(good.read_bytes())
    at = data.find(b"stsz")
    data[at + 12: at + 16] = (10 ** 6).to_bytes(4, "big")   # claims a million packets
    huge.write_bytes(bytes(data))
    results = decode(aac, cut, noisy, huge)
    assert results[0] == "NotAlac: this .m4a holds AAC, not Apple Lossless"
    assert results[1].startswith("NotAlac: no index")
    assert results[2] == "ok" or results[2].startswith("NotAlac: "), results[2]   # (garbage in: never a crash or a hang)
    assert results[3].startswith("NotAlac: it's damaged"), results[3]
