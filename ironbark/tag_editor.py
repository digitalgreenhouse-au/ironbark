"""Tag editing (0.16.0) — behind Settings → Experimental → "Tag editing", off by default. No UI.

This is the first part of the player that writes to your music files, so every save goes the
long way round, one file at a time:

1. The file's old tags are saved to the tag history first (`TagHistory` beside the settings,
   one small JSON file per save; pictures stored once each in its `blobs` folder; the newest
   HISTORY_KEEP saves kept), so every edit can be undone (`undo_jobs`). A file whose earlier
   save is still waiting to be swapped in isn't saved again until that one is done.
2. The file is copied (`.~name.dgtmp` beside it) and the new tags are written into the copy
   (Ironbark's own tag writer, tagio/), then read back. Links are left alone (a symbolic link
   would become a plain file; hard-linked names would split apart).
3. The copy's sound is proved untouched: both files are decoded through `audiofile.open` (the
   player's one decode path) and compared sample for sample; for FLAC the stored MD5 fingerprint
   must be unchanged and must match the decoded sound.
4. Only then is the copy swapped in with `os.replace` — all at once, so a crash leaves the old file
   or the new one, never half of one. Any difference → stop: the original is kept, the copy deleted.

A file the player has open (playing, or lined up next) can't be replaced on Windows: it waits,
already checked, until the player moves on (`TagSave.retry`) — never forced.

Formats (all through tagio, 0.27.0 — mutagen before): FLAC (Vorbis comments + picture blocks),
MP3 / WAV / AIFF (ID3v2; the file's own version is kept, new tags are v2.3 — the version Windows
reads), Apple Lossless .m4a (MP4 atoms), Ogg Vorbis / Opus (Vorbis comments; the cover in
METADATA_BLOCK_PICTURE).
Read-only, with a plain reason (`why_read_only`): CUE-sheet tracks (their titles live in the
.cue text file), files the player can't decode (AAC .m4a, FLAC inside Ogg — the sound couldn't
be proved untouched), and missing files.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import logging
import os
import re
import shutil
import stat as stat_module
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf
from PIL import Image

from . import audiofile, platforms, settings, tagio
from .analysis import pcm_bytes
from .library import mp4_genres
from .tagio import TagError
from .tagio import id3 as id3tags
from .tagio import mp4 as mp4tags
from .tagio.flac import FLACFile
from .tagio.vorbis import Picture

log = logging.getLogger(__name__)

VARIOUS = "(various)"       # shown for a field that differs between the songs being edited
FIELDS = (("title", "Title"), ("artist", "Artist"), ("album_artist", "Album artist"), ("album", "Album"),
          ("track", "Track"), ("disc", "Disc"), ("year", "Year"), ("genre", "Genre"),
          ("composer", "Composer"), ("comment", "Comment"))
FIELD_NAMES = tuple(name for name, _label in FIELDS)
LABELS = dict(FIELDS)
LISTS = {"genre", "composer"}  # may hold several values: shown joined with "; ", as the library shows them
KINDS = {"FLAC": "flac", "WAV": "wav", "AIFF": "aiff", "MP3": "mp3", "ALAC": "mp4", "Vorbis": "ogg", "Opus": "opus"}
ID3_KINDS = {"mp3", "wav", "aiff"}
VORBIS_KINDS = {"flac", "ogg", "opus"}
TEMP_PREFIX, TEMP_SUFFIX = ".~", ".dgtmp"  # not a music extension, so no scan ever lists a copy
BLOCK = 1 << 16                 # frames per read when comparing the sound
HISTORY_FOLDER = "TagHistory"
HISTORY_FORMAT = 1
HISTORY_KEEP = 500              # saves kept in the tag history (older ones can no longer be undone)
BLOB_FOLDER = "blobs"           # big values (pictures) in the history, stored once each by their SHA-256
BLOB_MIN = 4096                 # a value at least this long (base64 / text characters) goes to the blob store
BLOB_GRACE_SECONDS = 3600       # an unreferenced blob this new may belong to a save still being written
FRONT_COVER = 3                 # picture type "front cover" (FLAC, ID3)
_INTEGER_SUBTYPES = {"PCM_S8", "PCM_U8", "PCM_16", "PCM_24", "PCM_32", "ALAC_16", "ALAC_20", "ALAC_24", "ALAC_32"}

# Vorbis comment keys per field: the first is written; all are cleared on a write, so an old
# spelling ("album artist", "year", "description") can't linger and show through.
_VORBIS = {"title": ("title",), "artist": ("artist",), "album_artist": ("albumartist", "album artist", "album_artist"),
           "album": ("album",), "track": ("tracknumber",), "disc": ("discnumber",), "year": ("date", "year"),
           "genre": ("genre",), "composer": ("composer",), "comment": ("comment", "description")}
_ID3 = {"title": "TIT2", "artist": "TPE1", "album_artist": "TPE2", "album": "TALB", "track": "TRCK",
        "disc": "TPOS", "year": "TDRC", "genre": "TCON", "composer": "TCOM"}
# What a damaged or unexpected file can raise while its tags are read or written.
_TAG_ERRORS = (TagError, OSError, ValueError, KeyError, IndexError, struct.error)
_MP4 = {"title": "\xa9nam", "artist": "\xa9ART", "album_artist": "aART", "album": "\xa9alb", "year": "\xa9day",
        "genre": "\xa9gen", "composer": "\xa9wrt", "comment": "\xa9cmt"}
# What libsndfile reads from a WAV's RIFF INFO list (AIFF: its NAME / AUTH chunks). The library
# shows these when the ID3 tag lacks the field (library._wav_tags), so clearing such a field
# writes an empty ID3 frame — otherwise the old INFO value would show straight back.
_RIFF = {"title": "title", "artist": "artist", "album": "album", "track": "tracknumber", "genre": "genre",
         "year": "date"}

_NUMBER = re.compile(r"^\d{1,4}(/\d{1,4})?$")
_YEAR = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")


class SaveError(Exception):
    """Why a file wasn't saved, in plain words (the original is untouched)."""


class Busy(SaveError):
    """The file is held open (by the player, or another program): it waits, never forced."""


# -- which files can be edited -----------------------------------------------------------------
def kind_of(track) -> str | None:
    """How a track's tags are stored: flac / mp3 / wav / aiff / mp4 / ogg / opus (by what the
    file really contains — Track.codec — not its name)."""
    return KINDS.get(track.codec)


def why_read_only(track) -> str:
    """'' when the song's tags can be edited here; else the reason, in plain words."""
    if track.missing or not Path(audiofile.real_path(track.path)).is_file():
        return "the file can't be found"
    if audiofile.cue_of(track.path) is not None:
        return "a CUE sheet track — its title and artist live in the .cue file (edit that in a text editor)"
    if track.unsupported:
        return "the player can't decode it, so it couldn't prove the sound is untouched"
    if kind_of(track) is None:
        return f"{track.codec} files can't be edited here"
    return ""


def _open(path, kind: str):
    return tagio.open_file(os.fspath(path), kind)


# -- reading -------------------------------------------------------------------------------------
def _joined(values) -> str:
    out: list[str] = []
    for value in values or ():
        for part in str(value).split("\x00"):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return "; ".join(out)


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(";") if part.strip()]


def _riff_text(path) -> dict[str, str]:
    """The RIFF INFO / AIFF text values libsndfile reads (see _RIFF)."""
    found = {}
    try:
        with sf.SoundFile(os.fspath(path)) as f:
            for name, attribute in _RIFF.items():
                value = (getattr(f, attribute, "") or "").strip()
                if value:
                    found[name] = value
    except (sf.LibsndfileError, OSError, RuntimeError):
        pass
    return found


def _fields_from(audio, kind: str, path) -> dict[str, str]:
    fields = dict.fromkeys(FIELD_NAMES, "")
    if kind in VORBIS_KINDS:
        tags = audio.tags
        for name, keys in _VORBIS.items():
            for key in keys:
                values = (tags.get(key) or []) if tags is not None else []
                if any(v.strip() for v in values):
                    fields[name] = _joined(values) if name in LISTS else values[0].strip()
                    break
    elif kind in ID3_KINDS:
        tags = audio.tags
        present = set()
        if tags is not None:
            for name, frame_id in _ID3.items():
                if frame_id in tags:
                    present.add(name)
                    text = tags.text(frame_id)
                    if name == "genre":
                        fields[name] = _joined(tags.genres())
                    elif name in LISTS:
                        fields[name] = _joined(text)
                    else:
                        fields[name] = str(text[0]).strip() if text else ""
            comments = _plain_comments(tags)
            if comments and comments[0].text:
                fields["comment"] = str(comments[0].text[0]).strip()
        if kind in ("wav", "aiff"):  # as the library reads them: ID3 first, then the file's own text
            for name, value in _riff_text(path).items():
                if name not in present and (kind == "wav" or name in ("title", "artist")):
                    fields[name] = value
    elif kind == "mp4":
        genres = mp4_genres(audio.tags)   # (the numbered "gnre" item too)
        tags = audio.tags or {}
        for name, atom in _MP4.items():
            values = genres if name == "genre" else tags.get(atom) or []
            if values:
                fields[name] = _joined(values) if name in LISTS else str(values[0]).strip()
        for name, atom in (("track", "trkn"), ("disc", "disk")):
            pairs = tags.get(atom) or []
            if pairs and pairs[0][0]:
                number, total = pairs[0][0], pairs[0][1] if len(pairs[0]) > 1 else 0
                fields[name] = f"{number}/{total}" if total else str(number)
    return fields


def read_fields(path, kind: str) -> dict[str, str]:
    """The ten editable fields as the file holds them ('' = not set)."""
    return _fields_from(_open(path, kind), kind, path)


def _plain_comments(tags) -> list:
    """The ID3 comments with no description (the one the editor shows and writes)."""
    return [c for c in tags.getall("COMM") if isinstance(c, id3tags.CommentFrame) and c.desc == ""]


def _pictures(audio, kind: str) -> list[tuple[int, bytes]]:
    return tagio.pictures(audio)


def read_cover(path, kind: str) -> bytes | None:
    """The embedded cover the player shows (the front cover, else the first picture), or None."""
    pictures = _pictures(_open(path, kind), kind)
    front = [data for kind_, data in pictures if kind_ == FRONT_COVER]
    return front[0] if front else (pictures[0][1] if pictures else None)


# -- what's being changed ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Cover:
    """A new embedded cover (data = the picture file's bytes, kept as they are), or data=None to
    remove the cover."""
    data: bytes | None
    mime: str = ""
    width: int = 0
    height: int = 0
    name: str = ""


def load_cover(path) -> Cover:
    """A picture file chosen as the new cover: JPEG or PNG only (what every format and player takes)."""
    path = Path(path)
    try:
        data = path.read_bytes()
        with Image.open(io.BytesIO(data)) as image:
            kind, size = image.format, image.size
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise SaveError(f"“{path.name}” couldn't be read as a picture ({error})") from error
    if kind not in ("JPEG", "PNG"):
        raise SaveError(f"“{path.name}” is a {kind} picture — choose a JPEG or PNG")
    return Cover(data, "image/jpeg" if kind == "JPEG" else "image/png", size[0], size[1], path.name)


REMOVE_COVER = Cover(None)


@dataclass
class Edit:
    fields: dict[str, str] = field(default_factory=dict)  # field -> new value ('' = remove it)
    cover: Cover | None = None                             # None = leave the cover alone

    def __bool__(self) -> bool:
        return bool(self.fields) or self.cover is not None

    def describe(self) -> str:
        """e.g. "Album, Year and the cover" — what this edit changes."""
        parts = [LABELS[name] for name in FIELD_NAMES if name in self.fields]
        if self.cover is not None:
            parts.append("the cover" if self.cover.data is not None else "removing the cover")
        if len(parts) <= 1:
            return "".join(parts)
        return ", ".join(parts[:-1]) + " and " + parts[-1]


def common(values: list[dict[str, str]]) -> dict[str, str]:
    """Each field's value when every song has the same one, else VARIOUS."""
    shown = {}
    for name in FIELD_NAMES:
        seen = {v.get(name, "") for v in values}
        shown[name] = seen.pop() if len(seen) == 1 else VARIOUS
    return shown


def changes(shown: dict[str, str], typed: dict[str, str]) -> dict[str, str]:
    """The fields typed over. A field still showing VARIOUS is left alone in every file; anything
    else typed there (even nothing) is written to all of them."""
    out = {}
    for name in FIELD_NAMES:
        old = shown.get(name, "")
        new = typed.get(name, old)
        if old == VARIOUS and new == VARIOUS:
            continue
        new = new.strip()
        if new != old.strip() or old == VARIOUS:
            out[name] = new
    return out


def validate(fields: dict[str, str], kinds=()) -> list[str]:
    """Plain-words problems with the values typed (none = fine to save). `kinds`: the files'
    kinds (kind_of), for what only some formats can hold."""
    problems = []
    for name in ("track", "disc"):
        value = fields.get(name, "")
        if value and not _NUMBER.match(value):
            problems.append(f"{LABELS[name]}: a number, like 3 or 3/12")
    year = fields.get("year", "")
    if year and not _YEAR.match(year):
        problems.append("Year: a year like 2004, or a date like 2004-05-03")
    elif len(year) == 7 and ID3_KINDS & set(kinds):   # ID3v2.3 stores a year, or a day and month with it
        problems.append("Year: MP3, WAV and AIFF tags hold a year or a whole date (2004, or 2004-05-03), "
                        "not a year and month")
    for name, value in fields.items():
        if "\x00" in value:
            problems.append(f"{LABELS[name]}: contains a hidden character that can't be saved")
    return problems


# -- writing -----------------------------------------------------------------------------------
def _id3_version(audio) -> int:
    """Keep the file's own ID3 version (2.4 stays 2.4); a new tag (or 2.2) is written as 2.3."""
    tags = audio.tags
    return 4 if tags is not None and tags.version >= (2, 4, 0) else 3


def _ensure_tags(audio):
    if audio.tags is None:
        audio.add_tags()
    return audio.tags


def _write_field(audio, kind: str, name: str, value: str, riff: dict[str, str], version: int):
    if kind in VORBIS_KINDS:
        tags = _ensure_tags(audio)
        for key in _VORBIS[name]:
            tags.delete(key)
        if value:
            tags.set(_VORBIS[name][0], _split(value) if name in LISTS else [value])
    elif kind in ID3_KINDS:
        tags = _ensure_tags(audio)
        if name == "comment":
            for frame in _plain_comments(tags):
                tags.remove(frame)
            if value:
                tags.add(id3tags.CommentFrame("", [value], "eng"))
            return
        frame_id = _ID3[name]
        tags.delall(frame_id)
        if name == "genre":
            text = _id3_genre_text(value, version)
        elif name in LISTS and version == 4:
            text = _split(value)  # ID3v2.4 keeps several values (null-separated)
        else:
            text = [value]        # v2.3 has no standard way to list several: one "a; b" text
        if value:
            tags.add(id3tags.TextFrame(frame_id, text))
        elif name in riff and (kind == "wav" or name in ("title", "artist")):
            # Hides the RIFF INFO value (see _RIFF). A single space, as before 0.27.0 (mutagen
            # dropped empty frames); the library and this editor both strip it back to nothing.
            tags.add(id3tags.TextFrame(frame_id, [" "]))
    elif kind == "mp4":
        tags = _ensure_tags(audio)
        if name in ("track", "disc"):
            atom = "trkn" if name == "track" else "disk"
            if value:
                number, _, total = value.partition("/")
                tags[atom] = [(int(number), int(total or 0))]
            else:
                tags.pop(atom, None)
            return
        atom = _MP4[name]
        if name == "genre":
            tags.pop("gnre", None)  # the old numbered genre would otherwise show through
        if value:
            tags[atom] = [value]
        else:
            tags.pop(atom, None)


def _id3_genre_text(value: str, version: int) -> list[str]:
    """TCON's text as written: v2.4 one value per genre, v2.3 one "a; b" text. In v2.3 a genre
    starting with "(" would be read as a numbered reference ("(17)" = Rock), so it's written
    with the literal-"(" escape "((". Numbers ("17") are kept as typed; every ID3 reader shows
    them as the numbered genre's name."""
    if version == 4:
        return _split(value)
    return ["(" + value if value.startswith("(") else value]


def _picture_block(cover: Cover) -> Picture:
    return Picture(type=FRONT_COVER, mime=cover.mime, desc="", width=cover.width, height=cover.height, depth=24,
                   data=cover.data)


def _keep(pictures: list, type_of: Callable, removing: bool) -> list:
    """The pictures that stay. Replacing the cover: every picture but the one shown as the cover
    — the front covers, or with no front cover the first readable picture (`type_of` None =
    unreadable) — so booklet scans stored as "other" survive. Removing it: nothing — the player
    shows any picture left in the file as the cover (artwork.embedded_art), so a back cover left
    behind would simply take its place. (Undo brings them all back.)"""
    if removing:
        return []
    if any(type_of(p) == FRONT_COVER for p in pictures):
        return [p for p in pictures if type_of(p) != FRONT_COVER]
    shown = next((i for i, p in enumerate(pictures) if type_of(p) is not None), None)
    return [p for i, p in enumerate(pictures) if i != shown]


def _write_cover(audio, kind: str, cover: Cover):
    if kind == "flac":
        kept = _keep(list(audio.pictures), lambda p: p.type, cover.data is None)
        audio.pictures = kept + ([_picture_block(cover)] if cover.data is not None else [])
    elif kind in ("ogg", "opus"):
        tags = _ensure_tags(audio)
        blocks = []
        for value in tags.get("metadata_block_picture") or []:
            try:
                blocks.append((Picture.parse(base64.b64decode(value)).type, value))
            except (binascii.Error, ValueError):
                blocks.append((None, value))  # not readable: left as it was
        kept = [b[1] for b in _keep(blocks, lambda b: b[0], cover.data is None)]
        if cover.data is not None:
            kept.append(base64.b64encode(_picture_block(cover).render()).decode("ascii"))
        tags.delete("metadata_block_picture")
        if kept:
            tags.set("metadata_block_picture", kept)
    elif kind in ID3_KINDS:
        tags = _ensure_tags(audio)
        frames = [f for f in tags.getall("APIC") if isinstance(f, id3tags.PictureFrame)]
        unreadable = [f for f in tags.getall("APIC") if not isinstance(f, id3tags.PictureFrame)]
        kept = _keep(frames, lambda f: f.type, cover.data is None)
        tags.delall("APIC")
        for frame in unreadable + kept:   # (an unreadable picture never shows as the cover: left as it was)
            tags.add(frame)
        if cover.data is not None:
            taken = {f.desc for f in kept}
            desc = "" if "" not in taken else "Front cover"  # two APIC frames can't share a description
            tags.add(id3tags.PictureFrame(cover.mime, FRONT_COVER, desc, cover.data))
    elif kind == "mp4":
        tags = _ensure_tags(audio)
        # MP4 covers carry no picture type: the first is the cover. Replace it, keep the rest;
        # removing takes them all (any left would simply show as the cover).
        rest = list(tags.get("covr") or [])[1:]
        if cover.data is None:
            tags.pop("covr", None)
        else:
            form = mp4tags.JPEG if cover.mime == "image/jpeg" else mp4tags.PNG
            tags["covr"] = [mp4tags.Cover(cover.data, form)] + rest


def apply_edit(audio, kind: str, edit: Edit) -> None:
    """Write an Edit into an opened (tagio) file object; `save` writes it out."""
    riff = _riff_text(audio.path) if kind in ("wav", "aiff") else {}
    version = _id3_version(audio) if kind in ID3_KINDS else 0
    for name, value in edit.fields.items():
        _write_field(audio, kind, name, value, riff, version)
    if edit.cover is not None:
        _write_cover(audio, kind, edit.cover)


def save(audio, kind: str, version: int | None = None) -> None:
    """Write a tagio file object back to its file. ID3: the file's own version (`_id3_version`,
    worked out before any change; v2.3 gets the date as TYER / TDAT — tagio/id3.py); an ID3 tag
    left empty is removed rather than saved empty."""
    if kind in ID3_KINDS:
        if audio.tags is not None and len(audio.tags) == 0:
            audio.delete()
            return
        audio.save(version or _id3_version(audio))
    else:
        audio.save()


# -- snapshots: a file's whole tag state, for the history and undo ------------------------------
# The JSON shapes: Vorbis kinds {"comments": [[name, value]…], "pictures": [base64 FLAC picture
# block…] (FLAC only)}; ID3 kinds {"id3": base64 of the whole tag (no padding) or null,
# "version": 3 / 4}; MP4 {"ilst": base64 of the ilst items, byte for byte}. History written before
# 0.27.0 (by mutagen) has the same Vorbis and ID3 shapes, and MP4 as {"atoms": {key: typed
# values}} — still restored, and compared by meaning (`same_snapshot`), not byte for byte.
def _b64(data: bytes) -> str:
    return base64.b64encode(bytes(data)).decode("ascii")


def _mp4_from(item):
    """A value from a pre-0.27.0 MP4 snapshot ({"text"}, {"pair"}, {"cover"}…)."""
    if "cover" in item:
        return mp4tags.Cover(base64.b64decode(item["cover"]), item.get("format", mp4tags.JPEG))
    if "freeform" in item:
        return mp4tags.FreeForm(base64.b64decode(item["freeform"]), item.get("format", mp4tags.TEXT_UTF8))
    if "bool" in item:
        return bool(item["bool"])
    if "pair" in item:
        return tuple(item["pair"])
    if "bytes" in item:
        return base64.b64decode(item["bytes"])
    if "int" in item:
        return int(item["int"])
    return item["text"]


def _mp4_items(snap: dict) -> list:
    if snap.get("ilst"):
        return mp4tags.parse_items(base64.b64decode(snap["ilst"]))
    tags = mp4tags.MP4Tags()
    for key, value in (snap.get("atoms") or {}).items():
        tags[key] = [_mp4_from(v) for v in value] if isinstance(value, list) else [_mp4_from(value)]
    return tags.items


def snapshot_of(audio, kind: str) -> dict:
    """Everything in the file's tags (not just the ten fields), as plain JSON values."""
    if kind in VORBIS_KINDS:
        tags = audio.tags
        snap: dict = {"comments": [[k, v] for k, v in tags] if tags is not None else []}
        if kind == "flac":
            snap["pictures"] = [_b64(p.render()) for p in audio.pictures]
        return snap
    if kind in ID3_KINDS:
        version = _id3_version(audio)
        tags = audio.tags
        data = _b64(tags.render(version)) if tags is not None and len(tags) else None
        return {"id3": data, "version": version}
    tags = audio.tags
    return {"ilst": _b64(tags.render()) if tags is not None else ""}


def snapshot(path, kind: str) -> dict:
    return snapshot_of(_open(path, kind), kind)


def _frame_meaning(frame) -> tuple:
    if isinstance(frame, id3tags.UserTextFrame):
        return ("TXXX", frame.desc, tuple(frame.text))
    if isinstance(frame, id3tags.TextFrame):
        return (frame.id, tuple(frame.text))
    if isinstance(frame, id3tags.CommentFrame):
        return (frame.id, frame.desc, frame.lang, tuple(frame.text))
    if isinstance(frame, id3tags.PictureFrame):
        return ("APIC", frame.mime, frame.type, frame.desc, hashlib.sha256(frame.data).hexdigest())
    return (frame.id, hashlib.sha256(frame.raw or b"").hexdigest())


def _meaning(snap: dict | None, kind: str):
    """What a snapshot says, independent of how its bytes were laid out (frame order, text
    encodings, which program rendered it)."""
    if snap is None:
        return None
    if kind in VORBIS_KINDS:
        return ([tuple(c) for c in snap.get("comments", [])], list(snap.get("pictures", [])))
    if kind in ID3_KINDS:
        if not snap.get("id3"):
            return []
        tag = id3tags.ID3Tag.parse(base64.b64decode(snap["id3"]))
        return sorted((_frame_meaning(f) for f in tag.frames), key=repr)
    return sorted((i.key, [(d.type, d.payload) for d in i.data]) for i in _mp4_items(snap))


def same_snapshot(a: dict | None, b: dict | None, kind: str) -> bool:
    """Do two snapshots hold the same tags? (Byte-for-byte equal ones always do.)"""
    if a == b:
        return True
    try:
        return _meaning(a, kind) == _meaning(b, kind)
    except _TAG_ERRORS:
        return False


def snapshot_digest(snap: dict | None, kind: str) -> str:
    """A short fingerprint of what a snapshot says (by meaning, as same_snapshot compares). The
    history keeps this for the tags a save left behind: all it's needed for is telling whether
    the file was edited again since."""
    return hashlib.sha256(repr(_meaning(snap, kind)).encode("utf-8", "backslashreplace")).hexdigest()


def restore(audio, kind: str, snap: dict) -> int | None:
    """Put a snapshot's tags back into an opened file object (replacing what's there). Returns
    the ID3 version to save with (None for other formats)."""
    if kind in VORBIS_KINDS:
        tags = _ensure_tags(audio)
        tags.items = [(k, v) for k, v in snap.get("comments", [])]
        if kind == "flac":
            audio.pictures = [Picture.parse(base64.b64decode(block)) for block in snap.get("pictures", [])]
        return None
    if kind in ID3_KINDS:
        tags = _ensure_tags(audio)
        tags.frames = id3tags.ID3Tag.parse(base64.b64decode(snap["id3"])).frames if snap.get("id3") else []
        return int(snap.get("version") or 3)
    tags = _ensure_tags(audio)
    tags.items = _mp4_items(snap)
    return None


# -- proving the sound is untouched ----------------------------------------------------------------
def _clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def same_audio(original, copy, kind: str, cancel: threading.Event | None = None) -> str | None:
    """None when the copy decodes to exactly the original's samples (and, for FLAC, carries the
    same MD5 fingerprint, which matches the sound); else what differs, in plain words. Both files
    are read where they are, even on a network drive: never a copy the network playback cache keeps
    (it may be older than the file about to be replaced, and the temporary copy mustn't be cached)."""
    flac_md5 = None
    if kind == "flac":
        stored_old, stored_new = FLACFile(os.fspath(original)).info, FLACFile(os.fspath(copy)).info
        if stored_old.md5_signature != stored_new.md5_signature:
            return "FLAC's stored fingerprint (MD5) changed"
        if stored_new.md5_signature:
            flac_md5 = (hashlib.md5(), stored_new.bits_per_sample, stored_new.md5_signature)
    with audiofile.open(original, direct=True) as a, audiofile.open(copy, direct=True) as b:
        if (a.samplerate, a.channels, a.frames) != (b.samplerate, b.channels, b.frames):
            return "the sound's length or format came out different"
        dtype = "int32" if a.subtype in _INTEGER_SUBTYPES else "float64"
        done = 0
        while True:
            if cancel is not None and cancel.is_set():
                return "stopped"
            x = a.read(BLOCK, dtype=dtype, always_2d=True)
            y = b.read(BLOCK, dtype=dtype, always_2d=True)
            if x.shape != y.shape:
                return f"the sound came out a different length (at {_clock(done / a.samplerate)})"
            if not np.array_equal(x, y):
                where = done + int(np.argmax(np.any(x != y, axis=1)))
                return f"the sound came out different at {_clock(where / a.samplerate)}"
            if not len(x):
                break
            if flac_md5 is not None:
                flac_md5[0].update(pcm_bytes(y, flac_md5[1]))
            done += len(x)
    if flac_md5 is not None and int(flac_md5[0].hexdigest(), 16) != flac_md5[2]:
        return ("the sound doesn't match the file's own fingerprint (MD5) — the file may already be damaged, "
                "so it's left alone")
    return None


# -- one file: copy, change, check, swap -----------------------------------------------------------
@dataclass
class Prepared:
    """A checked copy, ready to swap in."""
    path: Path
    temp: Path
    stat: tuple[int, int]   # the original's size and modified time when it was copied
    after: str              # the digest of the copy's tag snapshot (`snapshot_digest`)
    copy: tuple[int, int]   # the checked copy's own size and modified time


def temp_path(path: Path) -> Path:
    return path.with_name(f"{TEMP_PREFIX}{path.name}{TEMP_SUFFIX}")


def _read_only(st) -> bool:
    attributes = getattr(st, "st_file_attributes", 0)
    return bool(attributes & getattr(stat_module, "FILE_ATTRIBUTE_READONLY", 1)) if attributes else \
        not st.st_mode & stat_module.S_IWUSR


def _created_ns(st) -> int | None:
    return getattr(st, "st_birthtime_ns", None) or (st.st_ctime_ns if platforms.IS_WINDOWS else None)


def _keep_created(st, temp: Path) -> None:
    """Give the copy the original's creation time (Windows), so a library index rebuilt from
    scratch still dates the album by when it arrived, not by the tag edit. Best effort."""
    created = _created_ns(st)
    if not platforms.IS_WINDOWS or not created:
        return
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # private: its types stay ours
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel32.SetFileTime.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), wintypes.LPVOID,
                                     wintypes.LPVOID)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.CreateFileW(str(temp), 0x100, 7, None, 3, 0x02000000, None)  # FILE_WRITE_ATTRIBUTES
    if handle in (None, wintypes.HANDLE(-1).value):
        return
    try:
        ticks = created // 100 + 116444736000000000  # 100 ns steps since 1601
        when = wintypes.FILETIME(ticks & 0xFFFFFFFF, ticks >> 32)
        kernel32.SetFileTime(handle, ctypes.byref(when), None, None)
    finally:
        kernel32.CloseHandle(handle)


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass  # left behind: hidden, not music, and replaced by the next try


def prepare(path, kind: str, change: Callable, check: Callable | None = None,
            cancel: threading.Event | None = None) -> Prepared:
    """Copy the file, `change(audio)` the copy's tags, read them back (`check(copy) -> problem`),
    prove the sound untouched. Raises SaveError (the copy is deleted; the original never touched).
    Links are refused: swapping a new file in would replace a symbolic link with a plain file,
    and give a hard-linked file's other names the old tags."""
    path = Path(path)
    if path.is_symlink():
        raise SaveError("the file is a link to another file — edit the original instead")
    try:
        st = path.stat()
    except OSError as error:
        raise SaveError("the file can't be found") from error
    if _read_only(st):
        raise SaveError("the file is read-only")
    if st.st_nlink > 1:
        raise SaveError("the file has other names (hard links), which a save would split apart — left alone")
    temp = temp_path(path)
    try:
        shutil.copy2(path, temp)
        audio = _open(temp, kind)
        version = _id3_version(audio) if kind in ID3_KINDS else None
        wanted = change(audio)
        save(audio, kind, wanted or version)
        problem = check(temp) if check is not None else None
        if problem:
            raise SaveError(problem)
        problem = same_audio(path, temp, kind, cancel)
        if problem:
            raise SaveError(problem)
        after = snapshot_digest(snapshot(temp, kind), kind)
        _keep_created(st, temp)
        copy = temp.stat()
    except SaveError:
        _remove(temp)
        raise
    except Exception as error:  # noqa: BLE001 — whatever went wrong, the copy goes; the original was never touched
        _remove(temp)
        raise SaveError(f"couldn't write the tags ({error})") from error
    return Prepared(path, temp, (st.st_size, st.st_mtime_ns), after, (copy.st_size, copy.st_mtime_ns))


def commit(prepared: Prepared) -> None:
    """Swap the checked copy in. Busy when the file is held open (try again later); SaveError
    when the file changed meanwhile (the copy is dropped: it was made from the old file)."""
    try:
        st = prepared.path.stat()
    except OSError as error:
        discard(prepared)
        raise SaveError("the file has gone (moved or deleted) — not saved") from error
    if (st.st_size, st.st_mtime_ns) != prepared.stat:
        discard(prepared)
        raise SaveError("the file changed while it was waiting — not saved")
    try:
        copy = prepared.temp.stat()
    except OSError as error:
        raise SaveError("the checked copy has gone — not saved") from error
    if (copy.st_size, copy.st_mtime_ns) != prepared.copy:
        discard(prepared)
        raise SaveError("the checked copy changed while it was waiting — not saved")
    try:
        os.replace(prepared.temp, prepared.path)
    except PermissionError as error:
        raise Busy("the file is in use") from error
    except OSError as error:
        discard(prepared)
        raise SaveError(f"couldn't replace the file ({error})") from error


def discard(prepared: Prepared) -> None:
    _remove(prepared.temp)


# -- jobs ---------------------------------------------------------------------------------------
@dataclass
class Job:
    path: Path
    kind: str
    change: Callable          # change(audio) -> ID3 version to save with, or None
    check: Callable | None    # check(copy) -> problem or None


def _field_check(kind: str, edit: Edit) -> Callable:
    def check(temp) -> str | None:
        audio = _open(temp, kind)
        got = _fields_from(audio, kind, temp)
        for name, value in edit.fields.items():
            want = "; ".join(_split(value)) if name in LISTS else value
            have = "; ".join(_split(got[name])) if name in LISTS else got[name]
            if name == "track" and kind == "mp4" and value:  # stored as numbers: "03" reads back "3"
                number, _, total = value.partition("/")
                want = f"{int(number)}/{int(total)}" if total and int(total) else str(int(number))
            if name == "genre" and kind in ID3_KINDS and value:  # as every ID3 reader shows it: "17" → Rock
                written = _id3_genre_text(value, _id3_version(audio))
                want = "; ".join(_split(_joined(id3tags.genres(written))))
            if have != want:
                return f"{LABELS[name]} didn't read back as written (“{have}”)"
        if edit.cover is not None:
            cover = read_cover(temp, kind)
            if cover != edit.cover.data:
                return "the cover didn't read back as written"
        return None
    return check


def edit_jobs(tracks, edit: Edit) -> list[Job]:
    """One job per editable song (read-only ones are left out — see why_read_only)."""
    jobs, seen = [], set()
    for track in tracks:
        if why_read_only(track):
            continue
        path = Path(audiofile.real_path(track.path))
        if path in seen:
            continue
        seen.add(path)
        kind = kind_of(track)
        jobs.append(Job(path, kind, lambda audio, k=kind: apply_edit(audio, k, edit), _field_check(kind, edit)))
    return jobs


def undo_jobs(entry: dict) -> tuple[list[Job], list[Path], list[Path]]:
    """Put back the tags a saved edit replaced. Returns (jobs, files edited again since — undoing
    takes those later changes back too, so ask first —, files that have gone).
    Reads every file, so it's for a worker thread. The old tags are taken out of the history
    (pictures from its blob store) only when each file's turn comes."""
    jobs, changed, gone = [], [], []
    for item in entry.get("files", []):
        if not item.get("saved"):
            continue
        path, kind, before = Path(item["path"]), item["kind"], item["before"]
        if not path.is_file():
            gone.append(path)
            continue
        try:
            now = snapshot(path, kind)
            if "after_digest" in item:
                same = snapshot_digest(now, kind) == item["after_digest"]
            else:   # (history written before the digests: the whole snapshot)
                same = same_snapshot(now, item.get("after"), kind)
            if not same:
                changed.append(path)
        except _TAG_ERRORS:
            changed.append(path)

        def check(temp, kind=kind, before=before):
            if same_snapshot(snapshot(temp, kind), _unpack(before), kind):
                return None
            return "the old tags didn't read back exactly"

        jobs.append(Job(path, kind, lambda audio, k=kind, b=before: restore(audio, k, _unpack(b)), check))
    return jobs, changed, gone


# -- the tag history -------------------------------------------------------------------------------
def history_folder() -> Path:
    """%APPDATA%\\Ironbark\\TagHistory (follows the settings, so tests stay in a temp folder)."""
    return settings.settings_path().parent / HISTORY_FOLDER


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def history() -> list[dict]:
    """Every saved edit, newest first (each with its file as "file")."""
    entries = []
    for path in sorted(history_folder().glob("TagEdit_*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("format") == HISTORY_FORMAT:
            data["file"] = path
            entries.append(data)
    return entries


def mark_undone(entry: dict, by: str) -> None:
    path = Path(entry["file"])
    data = {k: v for k, v in entry.items() if k != "file"}
    data["undone_by"] = by
    _write_json(path, data)
    entry["undone_by"] = by


# Big values (pictures, mostly) are kept once each in TagHistory\blobs, named by their SHA-256,
# so an album's shared cover is stored once however many files and saves hold it, and each
# history entry stays a few kilobytes. In an entry, a big base64 value becomes
# {"history_parts": [base64 or {"history_blob": name}…]} (an ID3 tag cut into its header and
# frames, an MP4 ilst into its items, so the picture is a piece of its own) and a big Vorbis
# comment value {"history_text": name}. Small values stay as they are; entries written before
# the blob store simply have none of these.
def _blob_folder() -> Path:
    return history_folder() / BLOB_FOLDER


def _put_blob(data: bytes) -> str:
    name = hashlib.sha256(data).hexdigest()
    path = _blob_folder() / name
    if path.exists():
        os.utime(path)   # in use again: old unreferenced blobs are only swept after BLOB_GRACE_SECONDS
        return name
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{name}.{threading.get_ident()}.tmp")   # (two saves may store one picture at once)
    temp.write_bytes(data)
    os.replace(temp, path)
    return name


def _get_blob(name: str) -> bytes:
    if not re.fullmatch(r"[0-9a-f]{64}", str(name)):
        raise SaveError("the tag history entry is damaged")
    try:
        data = (_blob_folder() / name).read_bytes()
    except OSError as error:
        raise SaveError("the old tags' pictures are missing from the tag history") from error
    if hashlib.sha256(data).hexdigest() != name:
        raise SaveError("the old tags' pictures in the tag history are damaged")
    return data


def _id3_pieces(data: bytes) -> list[bytes]:
    """A rendered ID3 tag (no unsynchronisation, no padding: `snapshot_of`) cut into its header
    and frames; whole if it isn't laid out like that."""
    pieces, at = [data[:id3tags.HEADER]], id3tags.HEADER
    while at + 10 <= len(data):
        size = data[at + 4: at + 8]
        at_end = at + 10 + (id3tags.synchsafe(size) if data[3] == 4 else int.from_bytes(size, "big"))
        pieces.append(data[at: at_end])
        at = at_end
    return pieces if at == len(data) else [data]


def _mp4_pieces(data: bytes) -> list[bytes]:
    """MP4 ilst items (boxes: a 32-bit size first); whole if they don't add up."""
    pieces, at = [], 0
    while at + 8 <= len(data):
        size = int.from_bytes(data[at: at + 4], "big")
        if size < 8:
            return [data]
        pieces.append(data[at: at + size])
        at += size
    return pieces if at == len(data) else [data]


def _pack_base64(value, split: Callable | None = None):
    if not isinstance(value, str) or len(value) < BLOB_MIN:
        return value
    data = base64.b64decode(value)
    pieces = split(data) if split is not None else [data]
    return {"history_parts": [{"history_blob": _put_blob(p)} if len(p) >= BLOB_MIN else _b64(p) for p in pieces]}


def _pack(snap: dict | None, kind: str) -> dict | None:
    """A snapshot as the history stores it (big values in the blob store). OSError if a blob
    can't be written."""
    if snap is None:
        return None
    if kind in VORBIS_KINDS:
        out = dict(snap)
        out["comments"] = [[k, {"history_text": _put_blob(v.encode("utf-8"))} if len(v) >= BLOB_MIN else v]
                           for k, v in snap.get("comments", [])]
        if "pictures" in snap:
            out["pictures"] = [_pack_base64(p) for p in snap["pictures"]]
        return out
    if kind in ID3_KINDS:
        return dict(snap, id3=_pack_base64(snap.get("id3"), _id3_pieces))
    return dict(snap, ilst=_pack_base64(snap.get("ilst"), _mp4_pieces))


def _unpack(value):
    """A stored snapshot (or any part of one) with its blob references read back in. SaveError
    if a blob is missing or damaged."""
    if isinstance(value, list):
        return [_unpack(v) for v in value]
    if isinstance(value, dict):
        if set(value) == {"history_parts"}:
            return _b64(b"".join(_get_blob(p["history_blob"]) if isinstance(p, dict) else base64.b64decode(p)
                                 for p in value["history_parts"]))
        if set(value) == {"history_text"}:
            return _get_blob(value["history_text"]).decode("utf-8")
        return {k: _unpack(v) for k, v in value.items()}
    return value


def _prune_history() -> None:
    """Keep the newest HISTORY_KEEP entries (and any with files still waiting), then sweep the
    blobs no entry refers to any more. Best effort."""
    folder = history_folder()
    entries = sorted(folder.glob("TagEdit_*.json"), reverse=True)
    for path in entries[HISTORY_KEEP:]:
        try:
            if not re.search(r'"state": "waiting"', path.read_text(encoding="utf-8")):
                path.unlink()
        except OSError:
            pass
    _sweep_blobs(BLOB_GRACE_SECONDS)


def _sweep_blobs(grace: float) -> None:
    """Delete the blobs no entry refers to any more (only those older than `grace` seconds: a newer
    one may belong to a save whose entry isn't written yet). Best effort."""
    used = set()
    for path in history_folder().glob("TagEdit_*.json"):
        try:
            used.update(re.findall(r'"history_(?:blob|text)": "([0-9a-f]{64})"', path.read_text(encoding="utf-8")))
        except OSError:
            return   # can't tell what's in use: sweep nothing
    now = time.time()
    for blob in _blob_folder().glob("*") if _blob_folder().is_dir() else []:
        try:
            if blob.name not in used and now - blob.stat().st_mtime >= grace:
                blob.unlink()
        except OSError:
            pass


def unfinished(entry: dict) -> bool:
    """A save the player hasn't finished with: files still waiting for the player to let go of them,
    or a save still under way. Its entry is still needed (to finish it, or to undo it)."""
    return any(isinstance(item, dict) and item.get("state") in UNFINISHED for item in entry.get("files", []))


def clear_history() -> int:
    """Delete the tag history — every saved edit except the unfinished ones (`unfinished`), and the
    pictures only they held — so those edits can't be undone any more. The music files aren't
    touched. Returns how many saves went. Reads and deletes files: for a worker thread."""
    removed = 0
    for path in history_folder().glob("TagEdit_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        except ValueError:
            data = None   # (damaged: it can't be undone anyway)
        if isinstance(data, dict) and unfinished(data):
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    with _history_lock:   # (a save starting now waits for the sweep, so its new pictures are never swept)
        _sweep_blobs(BLOB_GRACE_SECONDS if _saves_running else 0)
    return removed


UNFINISHED = ("pending", "waiting", "saving")   # a file's state in a save the player never finished
_history_lock = threading.Lock()
_saves_running = 0   # TagSave.run calls under way (their pictures may be in the blob store before their entry)
STOPPED_REASON = "the player stopped before it was saved — not saved"


def sweep_leftover_copies() -> int:
    """At start-up: a save the player never finished (a crash, a power cut) can leave its checked
    copies (`.~name.dgtmp`) beside the music. They're deleted — only in the folders of files whose
    save the tag history shows unfinished, and never one a save in hand is using. Files that were
    still waiting their turn are then marked not saved (a file caught in the middle of its swap stays
    marked saved, so Undo can still put it back). Returns how many copies were deleted. Best effort:
    touches the music folders, so run it off the window's thread."""
    removed = 0
    for entry in history():
        files = [item for item in entry.get("files", []) if isinstance(item, dict)]
        unfinished = [item for item in files if item.get("state") in UNFINISHED and isinstance(item.get("path"), str)]
        if not unfinished:
            continue
        for folder in {Path(item["path"]).parent for item in unfinished}:
            try:
                copies = [p for p in folder.glob(f"{TEMP_PREFIX}*{TEMP_SUFFIX}") if p.is_file()]
            except OSError:
                continue   # (a drive that isn't connected: tried again next start)
            for copy in copies:
                original = copy.with_name(copy.name[len(TEMP_PREFIX):-len(TEMP_SUFFIX)])
                with _claims_lock:
                    in_hand = _claim_key(original) in _claims
                if in_hand:
                    continue
                try:
                    copy.unlink()
                except OSError:
                    log.info("tag history: couldn't delete the leftover copy %s", copy, exc_info=True)
                    continue
                removed += 1
                log.info("tag history: deleted the leftover copy %s", copy)
        changed = False
        for item in unfinished:
            if item["state"] != "saving":
                item.update(state="failed", saved=False, reason=STOPPED_REASON)
                changed = True
        if changed:
            try:
                _write_json(Path(entry["file"]), {k: v for k, v in entry.items() if k != "file"})
            except OSError:
                log.info("tag history: couldn't update %s", entry["file"], exc_info=True)
    return removed


# -- a whole save -------------------------------------------------------------------------------------
# Which save holds each file: a copy in hand, or checked and waiting to be swapped in. A second
# save of the same file meanwhile is refused: its copy would be made from the old file, so
# swapping it in after the first would quietly take the first save's changes back out.
_claims: dict[str, str] = {}
_claims_lock = threading.Lock()


def _claim_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _claim(path: Path, owner: str) -> bool:
    with _claims_lock:
        return _claims.setdefault(_claim_key(path), owner) == owner


def _release(path: Path, owner: str) -> None:
    with _claims_lock:
        if _claims.get(_claim_key(path)) == owner:
            del _claims[_claim_key(path)]


@dataclass
class FileResult:
    path: Path
    state: str = "pending"    # saved / waiting / failed
    reason: str = ""


class TagSave:
    """Save a set of jobs (one edit across songs, or an undo): history first, then each file
    copied → changed → checked → swapped. Files that are busy wait (`retry`)."""

    def __init__(self, jobs: list[Job], summary: str, undo_of: dict | None = None):
        self.jobs = jobs
        self.summary = summary
        self.undo_of = undo_of
        self.results = [FileResult(job.path) for job in jobs]
        self._prepared: dict[Path, Prepared] = {}
        # One reading of the clock for the seconds and the microseconds: read twice, a second turning
        # over in between would name a later save before an earlier one (history() sorts by the name).
        now = time.time_ns()
        local = time.localtime(now // 1_000_000_000)
        stamp = time.strftime("%Y%m%d_%H%M%S", local) + f"_{now // 1000 % 1000000:06d}"
        self.id = stamp
        self.history_file = history_folder() / f"TagEdit_{stamp}.json"
        self.entry = {"format": HISTORY_FORMAT, "id": stamp, "when": time.strftime("%d/%m/%Y %H:%M:%S", local),
                      "summary": summary, "undo_of": undo_of.get("id") if undo_of else None, "files": []}
        self.progress = (0, len(jobs))
        self.done = False

    # -- the history entry --
    def _write_history(self):
        _write_json(self.history_file, self.entry)

    def _try_write(self):
        try:
            self._write_history()
        except OSError:
            pass   # (every swap was written down before it happened)

    def _note(self, index: int, result: FileResult, after: str | None = None):
        item = self.entry["files"][index]
        item["saved"] = result.state == "saved"
        item["state"] = result.state
        item["reason"] = result.reason
        if after is not None:
            item["after_digest"] = after

    def run(self, busy: Callable[[Path], bool] = lambda path: False, cancel: threading.Event | None = None,
            progress: Callable[[int, int, Path], None] | None = None) -> list[FileResult]:
        global _saves_running
        with _history_lock:
            _saves_running += 1
        try:
            self._run(busy, cancel, progress)
        except Exception as error:  # noqa: BLE001 — a slip must never leave copies or claims behind
            self.give_up(f"stopped by an unexpected error ({error})")
        finally:
            with _history_lock:
                _saves_running -= 1
        self.progress = (len(self.jobs), len(self.jobs))
        self.done = True
        return self.results

    def _run(self, busy, cancel, progress):
        # 1. The old tags, all of them, kept in the history before anything is written.
        for job, result in zip(self.jobs, self.results):
            before = None
            try:
                before = _pack(snapshot(job.path, job.kind), job.kind)
            except OSError as error:
                result.state, result.reason = "failed", f"couldn't read its tags or keep them in the history ({error})"
            except Exception as error:  # noqa: BLE001 — a file that can't be read is reported, not a crash
                result.state, result.reason = "failed", f"couldn't read its tags ({error})"
            self.entry["files"].append({"path": str(job.path), "kind": job.kind, "before": before, "after_digest": None,
                                        "saved": False, "state": result.state, "reason": result.reason})
        if any(item["before"] is not None for item in self.entry["files"]):
            try:
                self._write_history()
            except OSError as error:
                for result in self.results:
                    if result.state != "failed":
                        result.state, result.reason = "failed", (f"couldn't write the tag history ({error}), so "
                                                                 "nothing was changed")
                return
        # 2. Each file in turn.
        for i, (job, result) in enumerate(zip(self.jobs, self.results)):
            self.progress = (i, len(self.jobs))
            if progress is not None:
                progress(i, len(self.jobs), job.path)
            if result.state == "failed":
                continue
            if cancel is not None and cancel.is_set():
                result.state, result.reason = "failed", "stopped before it was saved"
                self._note(i, result)
                continue
            if not _claim(job.path, self.id):
                result.state, result.reason = "failed", ("an earlier tag save of this file is still under way "
                                                         "or waiting for the player — not saved; try again "
                                                         "once it's done")
                self._note(i, result)
                continue
            try:
                prepared = prepare(job.path, job.kind, job.change, job.check, cancel)
            except SaveError as error:
                _release(job.path, self.id)
                result.state, result.reason = "failed", str(error)
                self._note(i, result)
                continue
            self._prepared[job.path] = prepared
            self._swap(i, job.path, busy)
        self._try_write()
        self._finish_undo()
        try:
            _prune_history()
        except OSError:
            pass

    def _drop(self, index: int, path: Path, reason: str):
        """A file that won't be saved: its copy deleted, its claim let go, the reason noted."""
        prepared = self._prepared.pop(path, None)
        if prepared is not None:
            discard(prepared)
        _release(path, self.id)
        result = self.results[index]
        result.state, result.reason = "failed", reason
        self._note(index, result)

    def _swap(self, index: int, path: Path, busy: Callable[[Path], bool]) -> bool:
        result, prepared = self.results[index], self._prepared[path]
        if busy(path):
            result.state, result.reason = "waiting", "playing — saved once the player moves on"
            self._note(index, result)
            return False
        # Written down as saved before the swap: if the write after it fails, Undo still finds it.
        self.entry["files"][index].update(saved=True, state="saving", after_digest=prepared.after)
        try:
            self._write_history()
        except OSError as error:
            self._drop(index, path, f"couldn't update the tag history ({error}), so it wasn't saved")
            self._try_write()
            return False
        try:
            commit(prepared)
        except Busy:
            result.state, result.reason = "waiting", "in use — saved once it's free"
            self._note(index, result)
            self._try_write()
            return False
        except SaveError as error:
            self._drop(index, path, str(error))
            self._try_write()
            return False
        del self._prepared[path]
        _release(path, self.id)
        result.state, result.reason = "saved", ""
        self._note(index, result, prepared.after)
        self._try_write()
        return True

    def give_up(self, reason: str):
        """After an unexpected error: every file not saved or waiting is reported as not saved,
        with its copy and claim let go. One caught mid-swap stays marked saved in the history
        (it may have been swapped in), so Undo can still put it back."""
        for i, result in enumerate(self.results):
            if result.state in ("saved", "waiting"):
                continue
            prepared = self._prepared.pop(result.path, None)
            if prepared is not None:
                discard(prepared)
            _release(result.path, self.id)
            if result.state != "failed":
                result.state, result.reason = "failed", reason
            if i < len(self.entry["files"]) and self.entry["files"][i].get("state") != "saving":
                self._note(i, result)
        self._try_write()

    def _finish_undo(self):
        if self.undo_of is not None and any(r.state == "saved" for r in self.results) and self.undo_of.get("file"):
            try:
                mark_undone(self.undo_of, self.id)
            except OSError:
                pass

    @property
    def waiting(self) -> list[Path]:
        return [r.path for r in self.results if r.state == "waiting"]

    @property
    def saved(self) -> list[Path]:
        return [r.path for r in self.results if r.state == "saved"]

    @property
    def failed(self) -> list[FileResult]:
        return [r for r in self.results if r.state == "failed"]

    def retry(self, busy: Callable[[Path], bool] = lambda path: False) -> list[Path]:
        """Try the waiting files again (on the UI thread: only a quick swap — they're already
        checked). Returns the files saved now."""
        saved = []
        for i, result in enumerate(self.results):
            if result.state == "waiting" and self._swap(i, result.path, busy):
                saved.append(result.path)
        return saved

    def abandon(self) -> None:
        """Give up on the waiting files (the player is closing and they're still in use)."""
        for i, result in enumerate(self.results):
            if result.state == "waiting":
                self._drop(i, result.path, "still in use when the player closed — not saved")
        self._try_write()

    def describe(self) -> str:
        """One line for the status line: "Saved new tags in 12 files" (+ waiting / not saved)."""
        saved, waiting, failed = len(self.saved), len(self.waiting), len(self.failed)
        parts = []
        if saved:
            parts.append(f"Saved new tags in {saved} file{'' if saved == 1 else 's'}")
        if waiting:
            parts.append(f"{waiting} wait{'s' if waiting == 1 else ''} until the player moves on")
        if failed:
            parts.append(f"{failed} not saved")
        return " · ".join(parts) or "Nothing saved"
