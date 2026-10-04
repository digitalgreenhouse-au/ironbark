"""Find an album's cover: art embedded in the file first, then an image in the folder.

Folder images come under all sorts of names (cover.jpg, Folder.jpg, Front.jpg,
"Madvillainy.jpg", "00-artist-album-proof.jpg"), and folders often hold other
pictures too (back covers, disc scans, inlays, spectrograms), so they're scored:
well-known cover names first, obvious non-covers never.
"""

from __future__ import annotations

import io
import re
import struct
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageOps

from . import tagio
from .library import album_folder

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
FRONT_COVER = 3  # picture type for "front cover" in FLAC and ID3
_NOT_A_COVER = re.compile(r"back|inlay|inside|spectr|booklet|tray|matrix", re.IGNORECASE)
# Disc scans ("CD (1-2).jpg") — but scene-style names use "-cd-" for the release type
# ("00-artist-album-cd-flac-2019.jpg"), so these only push an image down the list.
_MAYBE_DISC = re.compile(r"\bcd\b|\bdis[ck]\b|label", re.IGNORECASE)
_NAME_RANK = {"cover": 0, "folder": 1, "front": 2}  # by file name without extension


def embedded_art(path: Path) -> bytes | None:
    """The front-cover picture stored inside the file (or its first picture). The file is read
    by what it contains, not its name (tagio.kind_of). Ogg Vorbis / Opus covers are
    METADATA_BLOCK_PICTURE comments (Xiph wiki, "VorbisComment"); MP4 covers carry no picture
    type, so the first counts as the front."""
    try:
        audio = tagio.open_any(path)
        pictures = tagio.pictures(audio) if audio is not None else []
    except (tagio.TagError, OSError, ValueError, IndexError, struct.error):
        return None
    if not pictures:
        return None
    front = [data for kind, data in pictures if kind == FRONT_COVER]
    return front[0] if front else pictures[0][1]


def folder_art(folder: Path) -> Path | None:
    """The image in an album folder most likely to be its front cover."""
    try:
        images = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS]
    except OSError:
        return None
    candidates = [p for p in images if not _NOT_A_COVER.search(p.stem)]
    if not candidates:
        return None

    def rank(p: Path):
        stem = p.stem.lower()
        if stem in _NAME_RANK:
            return (0, _NAME_RANK[stem])
        if "front" in stem or "cover" in stem:
            return (1, 0)
        if stem.startswith("albumart"):
            return (3, 0 if "large" in stem else 1)  # Windows Media Player's copies; the small one is tiny
        if _MAYBE_DISC.search(stem):
            return (4, -p.stat().st_size)
        return (2, -p.stat().st_size)  # anything else: the biggest picture is usually the cover
    return min(candidates, key=rank)


@lru_cache(maxsize=48)
def _thumbnail(key: tuple, size: int) -> Image.Image | None:
    source = key[1]
    try:
        image = Image.open(io.BytesIO(source) if isinstance(source, bytes) else source)
        image = ImageOps.exif_transpose(image).convert("RGB")
        return ImageOps.fit(image, (size, size), Image.Resampling.LANCZOS)  # square, centred
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def first_audio_file(folder: Path) -> Path | None:
    """The first audio file in a folder (or its CD1/Disc 1 subfolder), without a full scan."""
    from .library import AUDIO_EXTENSIONS, natural_key

    try:
        entries = sorted(folder.iterdir(), key=lambda p: natural_key(p.name))
    except OSError:
        return None
    files = [p for p in entries if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS]
    if files:
        return files[0]
    for sub in (p for p in entries if p.is_dir()):
        found = first_audio_file(sub)
        if found:
            return found
    return None


def cover_for(track_path: Path, size: int) -> Image.Image | None:
    """A size×size cover for the track's album, or None. Safe to call off the UI thread."""
    data = embedded_art(track_path)
    if data:
        return _thumbnail((str(album_folder(track_path)), data), size)
    image = folder_art(album_folder(track_path))
    if image is None and album_folder(track_path) != track_path.parent:
        image = folder_art(track_path.parent)  # a "CD1" folder with its own picture
    return _thumbnail(("file", image), size) if image else None
