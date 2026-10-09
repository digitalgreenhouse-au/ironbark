"""Music files and folders by their names alone — no file is opened here, so anything may import
this: the audio extensions, the folders a music search skips, "Disc 1"-style folders and the album
folder they belong to, and the natural and "The"-less sort keys.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

AUDIO_EXTENSIONS = {".flac", ".wav", ".mp3", ".aif", ".aiff", ".aifc", ".m4a", ".ogg", ".oga", ".opus"}
# Folders that never hold anyone's music: the Recycle Bin and Windows' own system folder on a
# drive, and the hidden folders macOS and NAS boxes keep beside the music (Finder data, thumbnails).
SKIP_FOLDERS = {"$recycle.bin", "system volume information", ".trashes", ".appledouble", "@eadir", "#recycle"}
# A "CD1" / "Disc 2" folder inside an album folder.
DISC_FOLDER = re.compile(r"^\s*(cd|dis[ck])\s*\d+\b", re.IGNORECASE)
_LEADING_THE = re.compile(r"^\s*the\s+", re.IGNORECASE)


def is_music_name(name: str) -> bool:
    """A music file, by its name: an audio extension, and not one of the "._" files macOS leaves
    beside every file on drives and network shares it has used (they hold Finder data, not audio,
    under the same extension)."""
    return not name.startswith("._") and Path(name).suffix.lower() in AUDIO_EXTENSIONS


def skip_folder(name: str) -> bool:
    """A folder no music search should go into (see SKIP_FOLDERS)."""
    return name.lower() in SKIP_FOLDERS


def natural_key(text: str) -> list:
    """Sort key that orders "2" before "10". Only runs of decimal digits count as numbers (every
    odd-numbered piece of the split): "²" or "①" are digits to str.isdigit, but not to int()."""
    return [int(chunk) if i % 2 else chunk for i, chunk in enumerate(re.split(r"(\d+)", text.lower()))]


def sort_name(text: str) -> list:
    """Sort key for names: "The Beatles" sorts under B, and "2" comes before "10"."""
    return natural_key(_LEADING_THE.sub("", text, count=1).strip() or text)


def album_folder(track_path: Path) -> Path:
    """The album a track belongs to: its folder, or the parent of a "CD1"/"Disc 2" folder."""
    folder = track_path.parent
    return folder.parent if DISC_FOLDER.match(folder.name) else folder


def album_folder_text(folder: str) -> str:
    """album_folder for a file's folder given as text: the folder, or the parent of a "CD1" /
    "Disc 2" folder (no Path objects made: for working through a whole library)."""
    parent, name = os.path.split(folder)
    if not name:   # a drive or the top of a share: no parent to go to
        return folder
    return parent if DISC_FOLDER.match(name) else folder
