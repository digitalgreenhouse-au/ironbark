"""Playlists as plain .m3u8 files: one song path per line, readable by almost any player.

They live in a "Playlists" folder next to the settings, but any .m3u / .m3u8 file can be
opened. Paths are written in full so a playlist works wherever it's copied; relative paths
in playlists from elsewhere are resolved against the playlist's own folder.

Playlists are written as UTF-8. One another program saved in an older encoding (a Windows code
page) is read in that (`fallback_encoding` says which), and the player asks before saving over it.

A CUE sheet track (one track of a long file) is written as its file's path with a
"#IRONBARK-CUE:<track number>" line before it. Other players skip the comment and play the whole
file; this player reads it back as that one track. While the track can't be found (the drive is
unplugged, the .cue has been moved) it keeps its number, so saving the playlist meanwhile
doesn't turn it into the whole file.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from . import audiofile, cuesheet, settings
from .audiofile import CuePath, CueRange
from .library import Track, cue_track_path

EXTENSIONS = {".m3u8", ".m3u"}
CUE_MARK = "#IRONBARK-CUE:"
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


def fallback_encoding(path: Path) -> str:
    """The older encoding a playlist had to be read in — "" when it's UTF-8 or UTF-16 (or can't be
    read just now) — named for people: "Windows-1252", "Windows code page 932"."""
    try:
        _text, encoding = cuesheet.decode_with(Path(path).read_bytes())
    except OSError:
        return ""
    name = encoding.lower().replace("_", "-")
    if name.startswith("utf"):
        return ""
    if name.startswith("cp") and name[2:].isdigit():
        return "Windows-1252" if name == "cp1252" else f"Windows code page {name[2:]}"
    return encoding


def read(path: Path) -> list[Path]:
    """The song paths in a playlist, in order (missing files included — the caller decides).
    A CUE track that can't be found just now is a CuePath whose range starts at -1 (unknown)."""
    text = cuesheet.decode(path.read_bytes())  # UTF-8, UTF-16, or older players' Windows code page
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
        if cue_number is not None:
            found = cue_track_path(song, cue_number) if song.is_file() else song
            song = found if audiofile.cue_of(found) else CuePath(song, cue=CueRange("", cue_number, -1, None))
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
    # (A file name that isn't valid Unicode is written as Windows holds it; `read` takes it back.)
    settings.write_file(path, "\n".join(lines) + "\n", errors="surrogatepass")


def rename(path: Path, new_name: str) -> Path:
    target = path.with_name(f"{safe_name(new_name)}{path.suffix}")
    if target.name != path.name:  # (compared as written: "rock" -> "Rock" is a rename too)
        if target.exists() and not target.samefile(path):
            raise FileExistsError(f"There's already a playlist called “{target.stem}”")
        os.replace(path, target)
    return target
