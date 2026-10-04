"""Ironbark's own tag reader and writer (0.27.0). No UI.

Until 0.26.0 the player read and wrote tags with mutagen, which is GPL-2.0-or-later — the one
GPL library in the app. Ironbark is MIT-licensed (since 0.27.0),
so mutagen had to go. This package replaces it with Ironbark's own code, written clean-room
from the published format specifications only (cited in each module; no other tag library's
source code was read):

- flac.py   FLAC metadata blocks: STREAMINFO, VORBIS_COMMENT, PICTURE, CUESHEET, PADDING (RFC 9639)
- vorbis.py Vorbis comments and FLAC picture blocks (Xiph.Org; RFC 9639 §8.6, §8.8)
- ogg.py    Ogg pages + CRC (RFC 3533), Ogg Vorbis (Vorbis I spec), Ogg Opus (RFC 7845)
- id3.py    ID3v1, ID3v2.2 / 2.3 / 2.4 (id3.org); genres.py the numbered ID3v1 genres
- mpeg.py   MP3: MPEG frame headers, Xing / Info / LAME / VBRI headers, plus the ID3 tag
- riff.py   the ID3 chunk in WAV (RIFF) and AIFF / AIFF-C files
- mp4.py    MP4 / M4A: ISO/IEC 14496-12 boxes + Apple's iTunes-style ilst tags

Each format has a file class with `tags`, `info` (where the player needs audio facts) and
`save()`. `open_file(path, kind)` opens one by kind; `kind_of(path)` works the kind out from the
file's contents (not its name).
"""

from __future__ import annotations

import base64
import binascii
from pathlib import Path

from . import id3
from ._files import TagError
from .flac import FLACFile, id3_prefix_size
from .mp4 import Cover, FreeForm, MP4File
from .mpeg import MP3File
from .ogg import OggOpusFile, OggVorbisFile
from .ogg import sniff as _sniff_ogg
from .riff import AiffFile, WaveFile
from .vorbis import Picture, VComment

__all__ = ["TagError", "FLACFile", "MP3File", "WaveFile", "AiffFile", "MP4File", "OggVorbisFile",
           "OggOpusFile", "Picture", "VComment", "Cover", "FreeForm", "open_file", "kind_of", "open_any",
           "pictures", "text_values", "FRONT_COVER", "KINDS"]

FRONT_COVER = 3   # picture type "front cover" (FLAC, ID3)
KINDS = {"flac": FLACFile, "mp3": MP3File, "wav": WaveFile, "aiff": AiffFile, "mp4": MP4File,
         "ogg": OggVorbisFile, "opus": OggOpusFile}
ID3_KINDS = {"mp3", "wav", "aiff"}
VORBIS_KINDS = {"flac", "ogg", "opus"}


def open_file(path, kind: str):
    """The file opened as `kind` (flac / mp3 / wav / aiff / mp4 / ogg / opus). Raises TagError
    (damaged, or not that kind) or OSError."""
    return KINDS[kind](path)


def kind_of(path) -> str | None:
    """The tag format the file's contents say it is (its name doesn't matter); None if unknown
    or unreadable."""
    try:
        with open(path, "rb") as f:
            head = f.read(64)
            skip = id3_prefix_size(head)
            if skip:
                f.seek(skip)
                after = f.read(4)
                return "flac" if after == b"fLaC" else "mp3"
    except OSError:
        return None
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"OggS":
        return {"vorbis": "ogg", "opus": "opus"}.get(_sniff_ogg(head))
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
        return "aiff"
    if head[4:8] == b"ftyp":
        return "mp4"
    if len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0:
        return "mp3"
    return None


def open_any(path):
    """The file opened by its contents, or None if it isn't a known kind / won't open."""
    kind = kind_of(path)
    if kind is None:
        return None
    try:
        return open_file(path, kind)
    except (TagError, OSError):
        return None


def vorbis_pictures(tags: VComment | None) -> list[Picture]:
    """Ogg Vorbis / Opus covers: METADATA_BLOCK_PICTURE comments, each a FLAC picture block in
    base64 (Xiph wiki, "VorbisComment"). Unreadable ones are skipped."""
    found = []
    for value in tags.get("metadata_block_picture") if tags is not None else []:
        try:
            found.append(Picture.parse(base64.b64decode(value, validate=False)))
        except (binascii.Error, ValueError, TagError):
            continue
    return found


def pictures(audio) -> list[tuple[int, bytes]]:
    """(picture type, image bytes) for every picture in an opened file. MP4 covers carry no
    picture type: they count as front covers."""
    if isinstance(audio, FLACFile):
        return [(p.type, p.data) for p in audio.pictures]
    if isinstance(audio, (OggVorbisFile, OggOpusFile)):
        return [(p.type, p.data) for p in vorbis_pictures(audio.tags)]
    if isinstance(audio, MP4File):
        return [(FRONT_COVER, bytes(c)) for c in ((audio.tags.get("covr") if audio.tags else None) or [])]
    tags = getattr(audio, "tags", None)
    if isinstance(tags, id3.ID3Tag):
        return [(f.type, f.data) for f in tags.getall("APIC") if isinstance(f, id3.PictureFrame)]
    return []


def text_values(audio) -> dict[str, list[str]]:
    """Every text tag as {UPPER NAME: [values]}: Vorbis comments by name; ID3 text frames by
    frame ID and TXXX by description; MP4 text items by atom name and free-form ("----")
    items by their name."""
    out: dict[str, list[str]] = {}
    tags = getattr(audio, "tags", None)
    if tags is None:
        return out
    if isinstance(tags, VComment):
        for name, value in tags:
            out.setdefault(name.upper(), []).append(value)
    elif isinstance(tags, id3.ID3Tag):
        for frame in tags.frames:
            if isinstance(frame, id3.UserTextFrame):
                out.setdefault(frame.desc.upper(), []).extend(frame.text)
            elif isinstance(frame, id3.TextFrame):
                out.setdefault(frame.id, []).extend(frame.text)
    else:
        for item in tags.items:
            name = item.key.split(":", 2)[2] if item.key.startswith("----:") else item.key
            for value in item.values():
                if isinstance(value, FreeForm):
                    value = bytes(value).decode("utf-8", "replace") if value.dataformat == 1 else None
                if isinstance(value, str):
                    out.setdefault(name.upper(), []).append(value)
    return out


def flac_cuesheet(path):
    """(the FLAC file's CUESHEET block or None, its sample rate); (None, 0) if it won't open."""
    try:
        audio = FLACFile(Path(path))
    except (TagError, OSError):
        return None, 0
    return audio.cuesheet, audio.info.sample_rate
