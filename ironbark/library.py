"""Find the audio files in a folder and put them in album order.

FLAC and WAV are the formats the player is built for; AIFF (Apple's uncompressed format) and
Apple Lossless (ALAC, in .m4a) are lossless too. MP3, Ogg Vorbis and Ogg Opus are listed and
played, clearly labelled as lossy, with whatever detail the file carries. AAC .m4a files and FLAC
inside Ogg are recognised but can't be played (see audiofile.py): they're listed with a warning.

CUE sheets: a disc ripped to one long file plus a `.cue` (or a FLAC with its own CUESHEET block)
is listed as its separate tracks (`audiofile.CuePath`). A folder that also holds separate track
files uses those instead, so nothing is listed twice.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re
import struct
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import soundfile as sf

from . import audiofile, cuesheet, m4a
from .audiofile import CuePath, CueRange
from .musicfiles import (  # noqa: F401
    AUDIO_EXTENSIONS,
    DISC_FOLDER,
    SKIP_FOLDERS,
    album_folder,
    album_folder_text,
    is_music_name,
    natural_key,
    skip_folder,
    sort_name,
)
from .tagio import TagError
from .tagio import genres as genre_list
from .tagio.flac import FLACFile
from .tagio.mp4 import MP4File
from .tagio.mpeg import MP3File
from .tagio.ogg import OggOpusFile, OggVorbisFile
from .tagio.riff import AiffFile, WaveFile

log = logging.getLogger(__name__)

_UNREADABLE = (TagError, OSError, IndexError, struct.error)  # a damaged file: listed with what's known

# The codec a file's name suggests (for the Library's quality column, before anything is read)
_CODEC_BY_EXT = {".flac": "FLAC", ".wav": "WAV", ".mp3": "MP3", ".aif": "AIFF", ".aiff": "AIFF", ".aifc": "AIFF",
                 ".m4a": "ALAC", ".ogg": "Vorbis", ".oga": "Vorbis", ".opus": "Opus"}
# The container a name promises, and what the contents really are (libsndfile reads the
# contents, not the name; MP4 and Ogg FLAC are recognised by audiofile.sniff). A file is flagged
# as misnamed when the two differ — an .ogg holding Opus is still an Ogg file, so it isn't.
_FAMILY_BY_EXT = {".flac": "FLAC", ".wav": "WAV", ".mp3": "MP3", ".aif": "AIFF", ".aiff": "AIFF", ".aifc": "AIFF",
                  ".m4a": "MP4", ".ogg": "Ogg", ".oga": "Ogg", ".opus": "Ogg"}
_FAMILY_BY_FORMAT = {"FLAC": "FLAC", "WAV": "WAV", "WAVEX": "WAV", "RF64": "WAV", "MP3": "MP3", "MPEG": "MP3",
                     "AIFF": "AIFF", "OGG": "Ogg"}
LOSSY_CODECS = {"MP3", "Vorbis", "Opus", "AAC"}

# libsndfile sample formats -> (bits, is floating point)
_SUBTYPE_BITS = {"PCM_U8": (8, False), "PCM_S8": (8, False), "PCM_16": (16, False),
                 "PCM_24": (24, False), "PCM_32": (32, False), "FLOAT": (32, True), "DOUBLE": (64, True),
                 "ALAC_16": (16, False), "ALAC_20": (20, False), "ALAC_24": (24, False), "ALAC_32": (32, False)}

# "01 ", "01 - ", "01. ", "07.-", "3) " at the start of a filename.
_LEADING_TRACK_NO = re.compile(r"^\s*\d{1,3}\s*[.\-_)]+\s*|^\s*\d{1,3}\s+")


@dataclass
class Track:
    path: Path
    title: str
    artist: str = ""
    album: str = ""
    track_no: int | None = None
    disc_no: int | None = None
    duration: float = 0.0  # seconds
    sample_rate: int = 0
    bits: int = 0
    channels: int = 0
    bitrate: int = 0       # average, bits per second
    encoder: str = ""      # FLAC "vendor string", e.g. "reference libFLAC 1.3.2"
    md5: int = 0           # MD5 of the decoded audio stored by the encoder (0 = none)
    size: int = 0          # bytes
    codec: str = "FLAC"    # FLAC WAV AIFF ALAC MP3 Vorbis Opus (AAC, "Ogg FLAC": recognised, unplayable)
    float_samples: bool = False  # WAV can hold 32/64-bit floating-point audio
    bitrate_mode: str = ""       # MP3: "CBR", "VBR" or "ABR"
    channel_mode: str = ""       # MP3: "joint stereo", "stereo", "dual channel", "mono"
    details: tuple[tuple[str, str], ...] = ()  # extra format-specific facts for Now Playing
    misnamed: str = ""           # e.g. ".flac" when the file is really something else
    missing: bool = False        # listed in a playlist, but the file isn't there (moved, drive unplugged)
    unsupported: str = ""        # why the player can't decode it (AAC, FLAC in Ogg); "" = it plays
    album_artist: str = ""       # the album's own artist tag ("" = none; see `browse.album_artist`)
    genre: str = ""              # every genre the file lists, joined with "; "
    composer: str = ""           # every composer, joined with "; "
    date: str = ""               # the date / year tag as written ("2004", "2004-05-03")
    compilation: bool = False    # tagged as a compilation (various artists)
    embedded_cue: object = dataclasses.field(default=None, repr=False, compare=False)  # FLAC CUESHEET block
    # The file couldn't be read just now (locked by another program, a network drive dropping
    # out), so only its name is known: worth reading again later, unlike a damaged file.
    unreadable: bool = dataclasses.field(default=False, repr=False, compare=False)

    @property
    def is_flac(self) -> bool:
        return self.codec == "FLAC"

    @property
    def lossy(self) -> bool:
        return self.codec in LOSSY_CODECS

    @property
    def cue(self) -> CueRange | None:
        """Where this track sits in its file, when it's one track of a CUE sheet."""
        return audiofile.cue_of(self.path)

    @property
    def year(self) -> int | None:
        """The year from the date tag ("2004-05-03" -> 2004); None if there isn't one."""
        return year_of(self.date)

    @property
    def display_artist_album(self) -> str:
        return " — ".join(part for part in (self.artist, self.album) if part)

    @property
    def format_line(self) -> str:
        """e.g. "FLAC · 24-bit / 96 kHz · stereo · 2,860 kbps" or
        "MP3 · 320 kbps CBR · 44.1 kHz · joint stereo · lossy"."""
        warning = [f"⚠ really {self.codec}, named {self.misnamed}"] if self.misnamed else []
        if self.unsupported:
            warning.append("⚠ can't be played")
        if not self.sample_rate:
            return " · ".join([self.codec] + warning)
        if self.lossy:
            rate = f"{round(self.bitrate / 1000):,} kbps {self.bitrate_mode}".strip() if self.bitrate else ""
            parts = [self.codec, rate, format_rate(self.sample_rate),
                     self.channel_mode or channel_name(self.channels), "lossy"]
            return " · ".join([part for part in parts if part] + warning)
        depth = f"{self.bits}-bit float" if self.float_samples else f"{self.bits}-bit"
        parts = [self.codec, f"{depth} / {format_rate(self.sample_rate)}", channel_name(self.channels)]
        if self.bitrate:
            parts.append(f"{round(self.bitrate / 1000):,} kbps")
        return " · ".join(parts + warning)


def format_rate(hz: int) -> str:
    """44100 -> "44.1 kHz", 96000 -> "96 kHz"."""
    khz = hz / 1000
    return f"{khz:g} kHz" if khz == round(khz, 1) else f"{khz:.2f} kHz"


def channel_name(channels: int) -> str:
    return {1: "mono", 2: "stereo"}.get(channels, f"{channels} ch")


def year_of(date: str) -> int | None:
    """The year in a date tag: "2004", "2004-05-03", "03/05/2004" -> 2004; None if there isn't one."""
    match = re.search(r"(?<!\d)(1[0-9]{3}|2[0-9]{3})(?!\d)", date or "")
    return int(match.group(1)) if match else None


def _joined(values) -> str:
    """Several values of one tag (two genres, two composers) as one "a; b" string, repeats dropped."""
    out: list[str] = []
    for value in values or ():
        for part in str(value).split("\x00"):  # (ID3v2.4 separates several values with a null)
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return "; ".join(out)


def parse_number(value: str | None) -> int | None:
    """Turn tag values like "3", "03" or "3/12" into 3."""
    if not value:
        return None
    match = re.match(r"\s*(\d+)", value)
    return int(match.group(1)) if match else None


def title_from_filename(path: Path) -> str:
    if path.stem.strip().isdecimal():
        return f"Track {int(path.stem)}"  # rips named 01.flac, 02.flac…
    stem = _LEADING_TRACK_NO.sub("", path.stem, count=1).strip()
    return stem or path.stem


def describe_encoder(text: str) -> str:
    """Make encoder tags readable: "Lavf62.12.100" is FFmpeg's internal library name."""
    text = text.strip()
    if text.startswith(("Lavf", "Lavc")):
        return f"FFmpeg ({text})"
    return text


def _first(tags, key: str) -> str:
    if tags is None:
        return ""
    values = tags.get(key)
    return values[0].strip() if values else ""


def _vorbis_extras(tags) -> dict:
    """Album artist, genre, composer, date and the compilation flag from Vorbis comments (FLAC and
    Ogg use the same keys; names vary a little between taggers, so the common spellings are tried)."""
    if tags is None:
        return {}

    def every(key):
        try:
            return tags.get(key) or []
        except (KeyError, ValueError):
            return []

    album_artist = next((_first(tags, k) for k in ("albumartist", "album artist", "album_artist")
                         if _first(tags, k)), "")
    date = next((_first(tags, k) for k in ("date", "year", "originaldate") if _first(tags, k)), "")
    return dict(album_artist=album_artist, genre=_joined(every("genre")), composer=_joined(every("composer")),
                date=date, compilation=_first(tags, "compilation").lower() in ("1", "yes", "true"))


def read_track(path: Path) -> Track:
    """Reads by what the file really contains; flags it if that doesn't match its name. A CUE
    track's path (audiofile.CuePath) gives that track; one its sheet no longer has (the .cue
    moved or changed) is listed as missing, keeping its path, rather than as the whole file."""
    cue = audiofile.cue_of(path)
    if cue is not None:
        whole = read_track(audiofile.real_path(path))
        found = [t for t in split_cue_images([whole]) if t.cue is not None and t.cue.number == cue.number]
        if found:
            return found[0]
        return dataclasses.replace(whole, path=path, title=f"Track {cue.number}", duration=0.0, md5=0,
                                   missing=True, embedded_cue=None)
    by_name = _FAMILY_BY_EXT.get(path.suffix.lower(), "")
    if by_name == "FLAC":
        # The usual case in one open (on a network drive each open costs several round trips): a
        # .flac that really is FLAC — FLACFile checks the "fLaC" mark itself. Anything else
        # (damaged or misnamed) goes the long way below, which looks at the contents first.
        try:
            return _flac_track(path, FLACFile(path))
        except OSError as error:   # locked, or a network drive dropping out: as _read_flac would say
            return Track(path=path, title=title_from_filename(path), unreadable=_passing(error))
        except _UNREADABLE:
            pass
    head = _contents(path)
    actual, ogg_flac = head if head is not None else ("", False)
    if head is not None and not actual:  # the first bytes don't say: ask libsndfile
        try:
            actual = _FAMILY_BY_FORMAT.get(sf.info(str(path)).format, "")
        except (sf.LibsndfileError, OSError, RuntimeError):
            log.debug("libsndfile couldn't say what %s holds", path, exc_info=True)
            actual = ""
    family = actual or by_name
    if family == "Ogg":
        track = _read_ogg(path, ogg_flac)
    else:
        readers = {"WAV": _read_wav, "MP3": _read_mp3, "AIFF": _read_aiff, "MP4": _read_m4a}
        track = readers.get(family, _read_flac)(path)
    if actual and by_name and actual != by_name:
        track.misnamed = path.suffix.lower()
    return track


def _contents(path: Path) -> tuple[str, bool] | None:
    """What a file holds, from its first bytes (one open; on a network drive each open costs
    several round trips): the container family ("FLAC", "MP3", "WAV", "AIFF", "MP4", "Ogg", or
    "" when the bytes don't say), and whether it's FLAC inside Ogg. None if it can't be opened."""
    try:
        with open(path, "rb") as f:
            head = f.read(64)
            if head.startswith(b"ID3") and len(head) >= 10:
                # An ID3v2 tag comes first (MP3s, and some FLACs): what follows it decides.
                # Its size is "syncsafe" (7 bits a byte), plus a 10-byte footer when flagged.
                size = 10 + sum((b & 0x7F) << shift for b, shift in zip(head[6:10], (21, 14, 7, 0)))
                f.seek(size + (10 if head[5] & 0x10 else 0))
                after = f.read(4)
                if after.startswith(b"fLaC"):
                    return "FLAC", False
                return ("MP3" if _mpeg_frame(after) else ""), False
    except OSError:
        log.debug("couldn't read the start of %s", path, exc_info=True)
        return None
    if head.startswith(b"fLaC"):
        return "FLAC", False
    if m4a.is_mp4(head):
        return "MP4", False
    if head.startswith(b"OggS"):
        return "Ogg", head[28:33] == b"\x7fFLAC"
    if head[:4] in (b"RIFF", b"RF64") and head[8:12] == b"WAVE":
        return "WAV", False
    if head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
        return "AIFF", False
    return ("MP3" if _mpeg_frame(head) else ""), False


def _mpeg_frame(data: bytes) -> bool:
    """An MPEG audio frame header: 11 sync bits, then a version and a layer that aren't the
    reserved values (which also rules out AAC's ADTS header, layer 0)."""
    return (len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0
            and data[1] & 0x18 != 0x08 and data[1] & 0x06 != 0)


def _passing(error: BaseException) -> bool:
    """Whether a failed read is worth trying again later: the system couldn't read the file just
    now (locked, a network drive dropping out), rather than the file being damaged."""
    if isinstance(error, sf.LibsndfileError):
        return error.code == 2  # libsndfile's "system error": the read itself failed
    return isinstance(error, OSError)


def _read_flac(path: Path) -> Track:
    try:
        audio = FLACFile(path)
    except _UNREADABLE as error:
        return Track(path=path, title=title_from_filename(path), unreadable=_passing(error))
    return _flac_track(path, audio)


def _flac_track(path: Path, audio: FLACFile) -> Track:
    tags = audio.tags
    info = audio.info
    return Track(
        **_vorbis_extras(tags),
        embedded_cue=audio.cuesheet,
        path=path,
        title=_first(tags, "title") or title_from_filename(path),
        artist=_first(tags, "artist") or _first(tags, "albumartist"),
        album=_first(tags, "album"),
        track_no=parse_number(_first(tags, "tracknumber")),
        disc_no=parse_number(_first(tags, "discnumber")),
        duration=float(info.length or 0.0),
        sample_rate=info.sample_rate,
        bits=info.bits_per_sample,
        channels=info.channels,
        bitrate=info.bitrate or 0,
        encoder=describe_encoder(tags.vendor if tags is not None else ""),
        md5=info.md5_signature,
        size=audio.size,   # (measured on the file FLACFile had open: no second look at it)
    )


_ID3_FIELDS = {"TIT2": "title", "TPE1": "artist", "TPE2": "albumartist", "TALB": "album",
               "TRCK": "tracknumber", "TPOS": "discnumber", "TDRC": "date", "TCMP": "compilation"}
# libsndfile's names for RIFF INFO (WAV): ICRD is "date", IGNR "genre"
_INFO_FIELDS = ("title", "artist", "album", "tracknumber", "genre", "date")


def _id3_tags(id3) -> dict[str, str]:
    """An ID3v2.3 tag is read as v2.4 (tagio/id3.py), so the year is always in TDRC (TYER, TDAT
    and TIME are joined into it)."""
    tags: dict[str, str] = {}
    if id3 is not None:
        for frame, key in _ID3_FIELDS.items():
            text = id3.text(frame)
            if text:
                tags[key] = str(text[0]).strip()
        if "TCON" in id3:  # .genres() turns ID3v1 numbers like "(17)" into names
            tags["genre"] = _joined(id3.genres())
        if "TCOM" in id3:
            tags["composer"] = _joined(id3.text("TCOM"))
    return tags


def _tag_extras(tags: dict[str, str]) -> dict:
    """The browse fields (genre, composer, date, album artist) from a plain tag dict (ID3 / RIFF
    INFO / MP4, already turned into words)."""
    return dict(album_artist=tags.get("albumartist", ""), genre=tags.get("genre", ""),
                composer=tags.get("composer", ""), date=tags.get("date", ""),
                compilation=tags.get("compilation", "").lower() in ("1", "yes", "true"))


def _wav_tags(path: Path, id3_file, text: dict[str, str]) -> tuple[dict[str, str], bool]:
    """WAV keeps tags in an ID3 block and/or a RIFF "INFO" list; AIFF in an "ID3 " chunk
    (tagio/riff.py) and/or NAME / AUTH text chunks. `text` is what libsndfile read from the
    INFO list or text chunks; the ID3 block (`id3_file`: WaveFile or AiffFile) wins. Also says
    whether the ID3 block couldn't be read just now (see _passing)."""
    try:
        tags, passing = _id3_tags(id3_file(path).tags), False
    except _UNREADABLE as error:
        tags, passing = {}, _passing(error)
    for key, value in text.items():
        if value and key not in tags:
            tags[key] = value
    return tags, passing


def _read_aiff(path: Path) -> Track:
    return _read_wav(path, codec="AIFF")


def _read_wav(path: Path, codec: str = "WAV") -> Track:
    """WAV or AIFF: the format and libsndfile's text tags from one open, then the ID3 tags."""
    keys, id3_file = (_INFO_FIELDS, WaveFile) if codec == "WAV" else (("title", "artist"), AiffFile)
    try:
        with sf.SoundFile(str(path)) as f:
            info = SimpleNamespace(samplerate=f.samplerate, frames=f.frames, channels=f.channels,
                                   subtype=f.subtype, subtype_info=f.subtype_info)
            text = {key: (getattr(f, key, "") or "").strip() for key in keys}
    except (sf.LibsndfileError, OSError, RuntimeError) as error:
        return Track(path=path, title=title_from_filename(path), codec=codec, unreadable=_passing(error))
    bits, is_float = _SUBTYPE_BITS.get(info.subtype, (0, False))
    tags, passing = _wav_tags(path, id3_file, text)
    details = ()
    if info.subtype not in _SUBTYPE_BITS:  # e.g. AIFF-C holding µ-law or another packed format
        details = (("Encoding", f"{info.subtype_info} — not plain PCM"),)
    return Track(
        **_tag_extras(tags),
        path=path,
        title=tags.get("title") or title_from_filename(path),
        artist=tags.get("artist") or tags.get("albumartist", ""),
        album=tags.get("album", ""),
        track_no=parse_number(tags.get("tracknumber")),
        disc_no=parse_number(tags.get("discnumber")),
        duration=info.frames / info.samplerate if info.samplerate else 0.0,
        sample_rate=info.samplerate,
        bits=bits,
        channels=info.channels,
        bitrate=info.samplerate * bits * info.channels,  # uncompressed: always the same
        size=path.stat().st_size,
        codec=codec,
        float_samples=is_float,
        details=details,
        unreadable=passing,
    )


_MP4_FIELDS = {"\xa9nam": "title", "\xa9ART": "artist", "aART": "albumartist", "\xa9alb": "album",
               "\xa9day": "date"}


def mp4_genres(tags) -> list[str]:
    """An MP4's genres: the "©gen" text, else the numbered "gnre" item (ID3v1 number + 1)."""
    if tags is None:
        return []
    text = tags.get("\xa9gen") or []
    if text:
        return [str(v) for v in text]
    return [genre_list.name(n - 1) for n in tags.get("gnre") or [] if isinstance(n, int) and genre_list.name(n - 1)]


def _read_m4a(path: Path) -> Track:
    """An .m4a / MP4: Apple Lossless plays (audiofile.py); AAC is listed but can't be played.
    What the file holds comes from the player's own MP4 reader (m4a.py, the one playback uses);
    the tags and figures from tagio/mp4.py."""
    passing = False
    try:
        m4a.read_index(path)
        codec_name = "alac"
    except m4a.NotAlac as error:
        codec_name = error.codec or "unknown"
    except OSError as error:
        codec_name, passing = "unknown", _passing(error)
    try:
        audio = MP4File(path)
        info, tags = audio.info, audio.tags
    except _UNREADABLE as error:
        info, tags = SimpleNamespace(length=0.0, sample_rate=0, bits_per_sample=0, channels=0, bitrate=0,
                                     codec_description=""), None
        passing = passing or _passing(error)
    genres = mp4_genres(tags)
    tags = tags if tags is not None else {}
    text = {key: str(tags[atom][0]).strip() for atom, key in _MP4_FIELDS.items() if tags.get(atom)}
    text["genre"] = _joined(genres)
    text["composer"] = _joined(tags.get("\xa9wrt"))
    text["compilation"] = "1" if any(tags.get("cpil") or []) else ""
    track_no = tags["trkn"][0][0] if tags.get("trkn") else None
    disc_no = tags["disk"][0][0] if tags.get("disk") else None
    encoder = describe_encoder(str(tags["\xa9too"][0])) if tags.get("\xa9too") else ""
    track = Track(
        **_tag_extras(text),
        path=path,
        title=text.get("title") or title_from_filename(path),
        artist=text.get("artist") or text.get("albumartist", ""),
        album=text.get("album", ""),
        track_no=track_no or None,
        disc_no=disc_no or None,
        duration=float(info.length or 0.0),
        sample_rate=info.sample_rate,
        bits=info.bits_per_sample if codec_name == "alac" else 0,
        channels=info.channels,
        bitrate=info.bitrate or 0,
        encoder=encoder,
        size=path.stat().st_size,
        unreadable=passing,
    )
    if codec_name == "alac":
        track.codec = "ALAC"
        track.details = (("Codec", "Apple Lossless (ALAC): lossless, decodes to exactly the original samples"),
                         ("Encoder", encoder or "not recorded"))
    elif codec_name.startswith("mp4a"):
        track.codec, track.bitrate_mode = "AAC", ""
        track.unsupported = "AAC (.m4a) can't be played: the player has no AAC decoder"
        track.details = (("Codec", f"{info.codec_description or 'AAC'}: lossy, detail was discarded when it was "
                                   "encoded"),
                         ("Warning", "can't be played — the player has no AAC decoder (Apple Lossless .m4a "
                                     "files do play)"))
    else:
        track.codec = (info.codec_description or codec_name.upper() or "MP4").strip()
        track.unsupported = f"{track.codec} in an MP4 file can't be played"
        track.details = (("Warning", "can't be played — only Apple Lossless .m4a files are supported"),)
    return track


def opus_input_rate(path: Path) -> int:
    """The sample rate an Opus file was made from (its OpusHead "input sample rate"); 0 = not
    recorded. RFC 7845 §5.1: this is *not* the rate to play at — Opus always decodes at 48 kHz."""
    try:
        with open(path, "rb") as f:
            head = f.read(512)
    except OSError:
        return 0
    at = head.find(b"OpusHead")
    if at < 0 or len(head) < at + 16:
        return 0
    return int.from_bytes(head[at + 12: at + 16], "little")


def _read_ogg(path: Path, ogg_flac: bool | None = None) -> Track:
    """Ogg Vorbis and Ogg Opus (both lossy; libsndfile decodes them). Tags are Vorbis comments,
    the same keys as FLAC. FLAC inside Ogg is recognised but can't be played (`ogg_flac`: already
    known from the file's first bytes; None = look)."""
    if ogg_flac is None:
        ogg_flac = audiofile.sniff(path) == "oggflac"
    if ogg_flac:
        return Track(path=path, title=title_from_filename(path), codec="Ogg FLAC", size=path.stat().st_size,
                     unsupported="FLAC inside an Ogg file can't be played (a normal .flac copy will)",
                     details=(("Warning", "can't be played — FLAC inside an Ogg file isn't supported by the "
                                          "player's decoder; a normal .flac copy of it will play"),))
    try:
        info = sf.info(str(path))
    except (sf.LibsndfileError, OSError, RuntimeError) as error:
        return Track(path=path, title=title_from_filename(path), codec="Vorbis", unreadable=_passing(error))
    opus = info.subtype == "OPUS"
    passing = False
    try:
        audio = (OggOpusFile if opus else OggVorbisFile)(path)
        tags, bitrate = audio.tags, int(audio.bitrate or 0)
    except _UNREADABLE as error:
        tags, bitrate, passing = None, 0, _passing(error)
    duration = info.frames / info.samplerate if info.samplerate else 0.0
    if not bitrate and duration:
        bitrate = int(path.stat().st_size * 8 / duration)
    encoder = describe_encoder(_first(tags, "encoder") or (getattr(tags, "vendor", "") or ""))
    if opus:
        details = [("Codec", "Opus (in an Ogg file): lossy, detail was discarded when it was encoded"),
                   ("Plays at", "48 kHz — Opus always decodes at 48 kHz, whatever rate it was made from")]
        original = opus_input_rate(path)
        if original and original != 48000:
            details.append(("Made from", f"{format_rate(original)} audio (before encoding)"))
    else:
        details = [("Codec", "Vorbis (in an Ogg file): lossy, detail was discarded when it was encoded")]
    details.append(("Encoder", encoder or "not recorded"))
    return Track(
        **_vorbis_extras(tags),
        path=path,
        title=_first(tags, "title") or title_from_filename(path),
        artist=_first(tags, "artist") or _first(tags, "albumartist"),
        album=_first(tags, "album"),
        track_no=parse_number(_first(tags, "tracknumber")),
        disc_no=parse_number(_first(tags, "discnumber")),
        duration=duration,
        sample_rate=info.samplerate,
        channels=info.channels,
        bitrate=bitrate,
        encoder=encoder,
        size=path.stat().st_size,
        codec="Opus" if opus else "Vorbis",
        details=tuple(details),
        unreadable=passing,
    )


_MP3_MODES = {0: "stereo", 1: "joint stereo", 2: "dual channel", 3: "mono"}


def _read_mp3(path: Path) -> Track:
    try:
        audio = MP3File(path)
    except _UNREADABLE as error:
        return Track(path=path, title=title_from_filename(path), codec="MP3", unreadable=_passing(error))
    info = audio.info
    tags = _id3_tags(audio.tags)
    software = str(audio.tags.text("TSSE")[0]).strip() if audio.tags and audio.tags.text("TSSE") else ""
    encoder = describe_encoder(" ".join(x for x in (info.encoder_info, info.encoder_settings) if x) or software)
    rate_mode = info.bitrate_mode   # "CBR", "VBR", "ABR" or "" (not known)

    version = {1: "MPEG-1", 2: "MPEG-2", 2.5: "MPEG-2.5"}.get(info.version, f"MPEG-{info.version}")
    layer = {1: "I", 2: "II", 3: "III"}.get(info.layer, str(info.layer))
    details = [
        ("Codec", f"{version} Layer {layer} (MP3): lossy, detail was discarded when it was encoded"),
        ("Bitrate mode", {"CBR": "constant (CBR)", "VBR": "variable (VBR)",
                          "ABR": "average (ABR)"}.get(rate_mode, "unknown")),
        ("Channel mode", _MP3_MODES.get(info.mode, "unknown")),
        ("Encoder", encoder or "not recorded"),
        ("Error check (CRC)", "yes" if info.protected else "no"),
    ]
    if info.track_gain is not None:
        peak = f", peak {info.track_peak:.3f}" if info.track_peak else ""
        details.append(("ReplayGain", f"track {info.track_gain:+.1f} dB{peak}"))
    if info.sketchy:
        details.append(("Warning", "the file's frame structure looks damaged or unusual"))

    return Track(
        **_tag_extras(tags),
        path=path,
        title=tags.get("title") or title_from_filename(path),
        artist=tags.get("artist") or tags.get("albumartist", ""),
        album=tags.get("album", ""),
        track_no=parse_number(tags.get("tracknumber")),
        disc_no=parse_number(tags.get("discnumber")),
        duration=float(info.length or 0.0),
        sample_rate=info.sample_rate,
        channels=info.channels,
        bitrate=info.bitrate or 0,
        encoder=encoder,
        size=path.stat().st_size,
        codec="MP3",
        bitrate_mode=rate_mode,
        channel_mode=_MP3_MODES.get(info.mode, ""),
        details=tuple(details),
    )


def scan_folder(folder: str | Path) -> list[Track]:
    """Every audio file under `folder` (including Disc 1/Disc 2 style subfolders), in play order.

    Order: subfolder, album tag (so a flat folder of several albums doesn't
    interleave), disc number, track number, then filename. Files without a
    track-number tag fall back to natural filename order.
    """
    folder = Path(folder)
    paths = []
    for here, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if not skip_folder(d)]
        paths += [Path(here, name) for name in names if is_music_name(name)]
    return read_in_play_order(folder, paths)


def scan_album(folder: str | Path) -> list[Track]:
    """Just this album: its own audio files plus "CD1"/"Disc 2" subfolders — not other albums
    that happen to sit in folders underneath it."""
    folder = Path(folder)
    paths = []
    for entry in folder.iterdir():
        if entry.is_file() and is_music_name(entry.name):
            paths.append(entry)
        elif entry.is_dir() and DISC_FOLDER.match(entry.name):
            paths += [p for p in entry.iterdir() if p.is_file() and is_music_name(p.name)]
    return read_in_play_order(folder, paths)


def read_in_play_order(folder: Path, paths: list[Path]) -> list[Track]:
    """The tracks of these files (read now), in play order (a CUE image once, as its tracks)."""
    return play_order(folder, split_cue_images([read_track(p) for p in paths]))


def play_order(folder: Path, tracks: list[Track]) -> list[Track]:
    """Tracks under `folder` in play order (see scan_folder)."""
    top = os.fspath(folder)
    top = top if top.endswith(os.sep) else top + os.sep

    def subfolder_of(track: Track) -> str:
        # The track's folder below `folder`, as Path.relative_to(...).as_posix() would give it, worked
        # out on the text (making Path objects was most of the time a big library took to sort).
        here = os.path.dirname(os.fspath(track.path))
        if here == top or here + os.sep == top:
            return "."
        if here.startswith(top):
            return here[len(top):].replace(os.sep, "/")
        return track.path.parent.relative_to(folder).as_posix()   # (other spellings of the same folder)

    def sort_key(track: Track):
        subfolder = subfolder_of(track)
        return (
            natural_key(subfolder),
            natural_key(track.album),
            track.disc_no or 0,
            track.track_no if track.track_no is not None else 10_000,
            natural_key(track.path.name),
        )

    return sorted(tracks, key=sort_key)


# -- CUE sheets: one long file, listed as its tracks ----------------------------
def _sheets_in(folder: Path) -> list[cuesheet.CueSheet]:
    sheets = []
    for cue in cuesheet.cue_files(folder):
        try:
            sheets.append(cuesheet.read(cue))
        except OSError:
            log.info("couldn't read the CUE sheet %s", cue, exc_info=True)
            continue
    return sheets


def _cue_tracks_for(sheet: cuesheet.CueSheet, path: Path, folder_audio: list[str]) -> list[cuesheet.CueTrack]:
    """The sheet's audio tracks that start in `path`. FILE lines are matched by name (ignoring
    case and any folder). A sheet whose FILE names match nothing in the folder (renamed, or
    converted from .wav to .flac after ripping) still fits by the name without its extension,
    or when it names one file and the folder holds one audio file. Data tracks are left out."""
    found = sheet.tracks_in(path.name)
    if not found and not any(cuesheet._base(f) in folder_audio for f in sheet.files):
        stem = path.stem.lower()
        same = [f for f in sheet.files if Path(cuesheet._base(f)).stem == stem]
        if same:
            found = sheet.tracks_in(same[0])
        elif len({cuesheet._base(f) for f in sheet.files}) == 1 and len(folder_audio) == 1:
            found = sheet.tracks_in(sheet.files[0])
    return [t for t in found if t.audio and t.start is not None]


def cue_layout(path: Path, frames: int, rate: int, sheets: list[cuesheet.CueSheet], folder_audio: list[str],
               embedded: cuesheet.CueSheet | None = None):
    """(sheet, its audio tracks in this file) when `path` is a CUE image — a file holding two or
    more tracks of a .cue beside it, or of its own CUESHEET block — else None. The tracks must
    start in order and inside the file (a sheet for a different file never fits)."""
    candidates = [(s, _cue_tracks_for(s, path, folder_audio)) for s in sheets]
    if embedded is not None:
        candidates.append((embedded, [t for t in embedded.tracks if t.audio]))
    for sheet, tracks in candidates:
        if len(tracks) < 2:
            continue
        starts = [t.start_sample(rate) for t in tracks]
        if all(b > a for a, b in zip(starts, starts[1:])) and 0 <= starts[0] and starts[-1] < frames:
            return sheet, tracks
    return None


def _cue_range(sheet: cuesheet.CueSheet, track: cuesheet.CueTrack, rate: int, frames: int) -> CueRange:
    end = sheet.end_sample(track, rate)
    return CueRange(str(sheet.source or ""), track.number, track.start_sample(rate),
                    None if end is None or end >= frames else end)


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def _cue_split(track: Track, sheet: cuesheet.CueSheet, cue_tracks: list[cuesheet.CueTrack]) -> list[Track]:
    rate = track.sample_rate
    frames = round(track.duration * rate)
    where = "the file's own cue sheet" if sheet.embedded else (sheet.source.name if sheet.source else "a cue sheet")
    disc = track.disc_no or parse_number(sheet.rem.get("DISCNUMBER"))
    out = []
    for ct in cue_tracks:
        cue = _cue_range(sheet, ct, rate, frames)
        end = frames if cue.end is None else cue.end
        about = (f"track {ct.number} of {where}: {_clock(cue.start / rate)}–{_clock(end / rate)} "
                 f"of {track.path.name}")
        out.append(dataclasses.replace(
            track, path=CuePath(track.path, cue=cue), title=ct.title or f"Track {ct.number}",
            artist=ct.performer or sheet.performer or track.artist, album=sheet.title or track.album,
            album_artist=sheet.performer or track.album_artist,
            composer=ct.songwriter or sheet.songwriter or track.composer,
            genre=sheet.rem.get("GENRE", "").strip('"') or track.genre,
            date=sheet.rem.get("DATE", "").strip('"') or track.date,
            track_no=ct.number, disc_no=disc, duration=(end - cue.start) / rate if rate else 0.0,
            md5=0,  # the file's MD5 covers the whole file, not one track of it
            embedded_cue=None, details=track.details + (("CUE sheet", about),)))
    return out


def split_cue_images(tracks: list[Track]) -> list[Track]:
    """Each CUE image (see cue_layout) replaced by its tracks, in place. Only when *every* music
    file in its folder is an image: a folder that also has separate track files (a rip kept both
    ways, or a .cue that describes the separate files) lists its files as they are, so nothing
    appears twice. A file that couldn't be read as audio at all (no sample rate) is left as it is
    and doesn't count."""
    by_folder: dict[Path, list[Track]] = {}
    for track in tracks:
        if track.cue is None and not track.unsupported and track.sample_rate:
            by_folder.setdefault(track.path.parent, []).append(track)
    layouts: dict[int, tuple] = {}
    for folder, group in by_folder.items():
        has_cue = any(t.embedded_cue is not None for t in group) or bool(cuesheet.cue_files(folder))
        if not has_cue:
            continue
        sheets = _sheets_in(folder)
        names = [t.path.name.lower() for t in group]
        found = {}
        for track in group:
            embedded = (cuesheet.from_flac_block(track.embedded_cue, track.sample_rate, track.path)
                        if track.embedded_cue is not None else None)
            layout = cue_layout(track.path, round(track.duration * track.sample_rate), track.sample_rate,
                                sheets, names, embedded)
            if layout is None:
                break  # a separate track file: the folder stays as it is
            found[id(track)] = layout
        else:
            layouts.update(found)
    if not layouts:
        return tracks
    out: list[Track] = []
    for track in tracks:
        layout = layouts.get(id(track))
        out += _cue_split(track, *layout) if layout else [track]
    return out


def cue_track_count(folder: Path, names: list[str]) -> int:
    """How many tracks a folder's music files hold, counting a CUE image (by the .cue beside it)
    as its tracks — the same rule as split_cue_images, from the sheets alone (nothing decoded;
    images known only by their own CUESHEET block count as one until opened)."""
    sheets = _sheets_in(folder)
    lower = [n.lower() for n in names]
    counts = []
    for name in names:
        best = max((len(_cue_tracks_for(s, folder / name, lower)) for s in sheets), default=0)
        if best < 2:
            return len(names)
        counts.append(best)
    return sum(counts)


def cue_track_path(path: Path, number: int) -> Path:
    """The path of track `number` of the CUE image `path` (the plain path if it isn't one)."""
    for track in split_cue_images([read_track(Path(path))]):
        if track.cue is not None and track.cue.number == number:
            return track.path
    return Path(path)


def expand_cue_paths(paths: list[Path]) -> list[Path]:
    """Paths with any CUE images replaced by their tracks' paths (reads each file's tags)."""
    return [t.path for t in split_cue_images([read_track(Path(p)) for p in paths])]


def album_files(track: Path) -> tuple[Path, list[Path]]:
    """(album folder, its audio files) for a track — the folder plus "Disc 1"-style subfolders,
    with `track` itself first."""
    folder = album_folder(track)
    files = []
    for entry in sorted(folder.iterdir()):
        if is_music_name(entry.name) and entry.is_file():
            files.append(entry)
        elif entry.is_dir() and DISC_FOLDER.match(entry.name):
            files += sorted(p for p in entry.iterdir() if is_music_name(p.name) and p.is_file())
    if audiofile.cue_of(track) is not None:  # one track of a CUE image: the album is its tracks
        files = expand_cue_paths(files)
    files = [track] + [f for f in files if f != track]
    return folder, files


# -- library (a folder of album folders) ---------------------------------------
@dataclass
class Album:
    path: Path
    name: str
    track_count: int
    sample_rate: int = 0
    bits: int = 0
    codecs: tuple[str, ...] = ()  # e.g. ("FLAC",) or ("MP3", "WAV")
    # Filled in from the saved library index (library_index.py); find_albums leaves them empty.
    artist: str = ""              # the album artist ("Various Artists" for a compilation)
    year: int | None = None
    genres: tuple[str, ...] = ()
    composers: tuple[str, ...] = ()
    added: float = 0.0            # when its newest file arrived (seconds since 1970), for Recently added
    title: str = ""               # the album tag, when the tracks agree on one

    @property
    def quality(self) -> str:
        """e.g. "24/96", "16/44.1", "MP3", or "mixed" when formats differ."""
        if len(self.codecs) > 1:
            return "mixed"
        if len(self.codecs) == 1 and self.codecs[0] in LOSSY_CODECS:
            return self.codecs[0]
        if not self.sample_rate:
            return ""
        return f"{self.bits}/{self.sample_rate / 1000:g}"


def find_albums(root: str | Path) -> list[Album]:
    """Every album under `root`: a folder that holds audio files. "CD1"/"Disc 2" style
    subfolders count towards their parent album rather than appearing on their own."""
    root = Path(root)
    counts: dict[Path, int] = {}
    first_file: dict[Path, Path] = {}
    codecs: dict[Path, set[str]] = {}
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not skip_folder(d)]
        flacs = sorted(f for f in files if is_music_name(f))
        if not flacs:
            continue
        count = len(flacs)
        if any(f.lower().endswith(".cue") for f in files):
            count = cue_track_count(Path(folder), flacs)  # a CUE image counts as its tracks
        album = Path(folder)
        if DISC_FOLDER.match(album.name) and album != root:
            album = album.parent
        counts[album] = counts.get(album, 0) + count
        first_file.setdefault(album, Path(folder) / flacs[0])
        codecs.setdefault(album, set()).update(_CODEC_BY_EXT[Path(f).suffix.lower()] for f in flacs)

    albums = []
    for path, count in counts.items():
        album = Album(path=path, name=path.name, track_count=count, codecs=tuple(sorted(codecs[path])))
        try:
            info = audiofile.info(first_file[path], header=True)   # (a header: never a whole song fetched for it)
            album.sample_rate, album.bits = info.samplerate, _SUBTYPE_BITS.get(info.subtype, (0, False))[0]
        except (sf.LibsndfileError, OSError, RuntimeError):
            log.debug("couldn't read the format of %s", first_file[path], exc_info=True)
        albums.append(album)

    # Same folder name in two places (e.g. a backup copy): say where each one lives.
    names = Counter(a.name for a in albums)
    for album in albums:
        if names[album.name] > 1 and album.path != root:
            album.name = f"{album.name}  ({album.path.parent.name})"
    return sorted(albums, key=lambda a: natural_key(a.name))


def group_by_album(tracks: list[Track]) -> list[tuple[Path, int, int]]:
    """Consecutive runs of tracks from the same album: (album folder, first index, end index)."""
    groups: list[tuple[Path, int, int]] = []
    for i, track in enumerate(tracks):
        folder = album_folder(track.path)
        if groups and groups[-1][0] == folder:
            groups[-1] = (folder, groups[-1][1], i + 1)
        else:
            groups.append((folder, i, i + 1))
    return groups
