"""Playlists as plain .m3u8 files: one song path per line, readable by almost any player.

They live in a "Playlists" folder next to the settings, but any .m3u / .m3u8 file can be
opened. Paths are written in full so a playlist works wherever it's copied; relative paths
in playlists from elsewhere are resolved against the playlist's own folder.

A CUE sheet track (one track of a long file) is written as its file's path with a
"#DG-CUE:<track number>" line before it. Other players skip the comment and play the whole
file; this player reads it back as that one track.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from . import audiofile, settings
from .library import Track, cue_track_path

EXTENSIONS = {".m3u8", ".m3u"}
CUE_MARK = "#DG-CUE:"
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def folder() -> Path:
    return settings.settings_path().parent / "Playlists"


def saved() -> list[Path]:
    """Every playlist in the playlists folder, by name."""
    try:
        return sorted((p for p in folder().iterdir() if p.suffix.lower() in EXTENSIONS),
                      key=lambda p: p.stem.lower())
    except OSError:
        return []


def safe_name(name: str) -> str:
    """A playlist name that's also a valid file name on Windows, macOS and Linux."""
    cleaned = _BAD_CHARS.sub("", name).strip().strip(".")
    return cleaned[:120] or "Playlist"


def path_for(name: str) -> Path:
    return folder() / f"{safe_name(name)}.m3u8"


def unused_name(base: str = "Playlist") -> str:
    taken = {p.stem.lower() for p in saved()}
    if base.lower() not in taken:
        return base
    n = 2
    while f"{base} {n}".lower() in taken:
        n += 1
    return f"{base} {n}"


def read(path: Path) -> list[Path]:
    """The song paths in a playlist, in order (missing files included — the caller decides)."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")  # older .m3u files from Windows players
    songs = []
    cue_number = None
    for line in text.splitlines():
        line = line.strip()
        if line.upper().startswith(CUE_MARK):
            try:
                cue_number = int(line[len(CUE_MARK):])
            except ValueError:
                cue_number = None
            continue
        if not line or line.startswith("#"):
            continue
        song = Path(line)
        song = song if song.is_absolute() else (path.parent / song)
        if cue_number is not None and song.is_file():
            song = cue_track_path(song, cue_number)
        cue_number = None
        songs.append(song)
    return songs


def write(path: Path, tracks: list[Track]) -> None:
    lines = ["#EXTM3U"]
    for track in tracks:
        label = f"{track.artist} - {track.title}" if track.artist else track.title
        lines.append(f"#EXTINF:{int(round(track.duration))},{label}")
        cue = audiofile.cue_of(track.path)
        if cue is not None:
            lines.append(f"{CUE_MARK}{cue.number}")
        lines.append(str(track.path))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(temporary, path)  # never leave a half-written playlist behind


def rename(path: Path, new_name: str) -> Path:
    target = path.with_name(f"{safe_name(new_name)}{path.suffix}")
    if target != path:
        if target.exists():
            raise FileExistsError(f"There's already a playlist called “{target.stem}”")
        os.replace(path, target)
    return target
