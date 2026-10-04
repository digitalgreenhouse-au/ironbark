"""Test helper: tag test files with Ironbark's own writer (ironbark.tagio) — what the tests used
mutagen for before 0.27.0. The writer itself is checked against FFmpeg's ffprobe in
test_tagio.py, so fixtures made here are readable by other software too."""
from __future__ import annotations

from pathlib import Path

from ironbark import tagio
from ironbark.tagio import id3


def text(frame_id: str, *values: str, encoding: int | None = None) -> id3.TextFrame:
    """An ID3 text frame: text("TIT2", "Song"), text("TCON", "Rock", "Pop")."""
    return id3.TextFrame(frame_id, list(values), encoding)


def write_id3(path: Path, frames, version: int = 4) -> Path:
    """Add these ID3 frames to an MP3 / WAV / AIFF file (made by libsndfile) and save as v2.`version`."""
    audio = tagio.open_file(path, tagio.kind_of(path))
    tags = audio.add_tags()
    for frame in frames:
        tags.add(frame)
    audio.save(version)
    return Path(path)


def write_vorbis(path: Path, **comments) -> Path:
    """Set Vorbis comments in a FLAC / Ogg Vorbis / Ogg Opus file: write_vorbis(p, title="x",
    genre=["Rock", "Pop"]). Keyword names are the comment names (use ** for ones with spaces)."""
    audio = tagio.open_file(path, tagio.kind_of(path))
    for name, value in comments.items():
        audio[name] = value
    audio.save()
    return Path(path)
