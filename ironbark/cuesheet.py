"""CUE sheets (no UI): where each track starts inside one or more audio files.

A `.cue` file is a small text file written by CD rippers (EAC, dBpoweramp, XLD…). It names the
audio file(s) and says where each track starts, in CD time. FLAC files can also carry the same
information inside them, as a CUESHEET block. Both are read here into one `CueSheet`.

The text format, as published (GNU ccd2cue manual, "CUE sheet format", and the CueSharp
specification at wyday.com, both read 04/10/2026):
- `FILE "name" WAVE` — the audio file the lines below refer to (types BINARY, MOTOROLA, AIFF,
  WAVE, MP3; rippers also write `FLAC`, which we accept).
- `TRACK nn AUDIO` — a track, number 1–99 (data types AUDIO, CDG, MODE1/2048, MODE1/2352,
  MODE2/2336, MODE2/2352, CDI/2336, CDI/2352).
- `INDEX nn mm:ss:ff` — index points 0–99, as an offset from the start of the current FILE:
  minutes, seconds (0–59) and frames, **75 frames per second** (a CD "sector"; 588 samples at
  44.1 kHz). INDEX 01 is where the track starts; INDEX 00, if present, starts the gap before
  it (the "pregap", taken from the file).
- `FLAGS …` — the track's subcode flags: PRE (pre-emphasis), DCP (digital copy permitted),
  4CH (four-channel audio), SCMS (serial copy management system). After TRACK, before INDEX.
- `PREGAP` / `POSTGAP mm:ss:ff` — gaps of generated silence (not in the file).
- `TITLE`, `PERFORMER`, `SONGWRITER` (CD-Text; for the disc before the first TRACK, else for
  the track), `ISRC` (per track), `CATALOG` (the disc's 13-digit barcode), `CDTEXTFILE`, and
  `REM` comments (rippers put GENRE, DATE, DISCID, COMMENT, REPLAYGAIN_… there).
- Strings containing spaces must be in double quotes.

Real-world cue sheets bend these rules, so the parser is forgiving: unknown commands and
malformed lines are skipped with a note in `CueSheet.warnings`, never an error. One common
bend is worth knowing: when a disc is ripped to separate files with the gaps kept at the end
of the previous track (EAC's "noncompliant" cue sheet), a track's INDEX 00 sits under the
previous FILE and its INDEX 01 under its own FILE. So every index remembers which file it is
in, and a track belongs to the file holding its INDEX 01.

The FLAC CUESHEET block (RFC 9639 §8.7): offsets in samples — each track's offset from the
start of the audio, each index's offset from its track's. The lead-out is track 170 on a CD
(255 otherwise) and marks the end of the last track. Per-track flags: audio / non-audio and
pre-emphasis. Read by Ironbark's own FLAC reader (tagio/flac.py: `FLACFile(path).cuesheet`).

Where a track ends (for playback): at the next track's INDEX 01 in the same file, so
the gap before a track stays at the end of the one before it — nothing is skipped or played
twice — or at the end of the file (FLAC: the lead-out) for the last track in a file.
"""

from __future__ import annotations

import locale
import re
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from .tagio import flac_cuesheet

FRAMES_PER_SECOND = 75
CD_RATE = 44100
SAMPLES_PER_FRAME_CD = CD_RATE // FRAMES_PER_SECOND   # 588
LEAD_OUT_CD = 170
LEAD_OUT_OTHER = 255
KNOWN_FLAGS = {"PRE", "DCP", "4CH", "SCMS"}

_TIME = re.compile(r"^(\d+):(\d{1,2}):(\d{1,2})$")
_TOKEN = re.compile(r'"([^"]*)"?|(\S+)')


def parse_time(text: str) -> int:
    """"mm:ss:ff" -> CD frames (1/75 s). Minutes may run past 99 (long files); seconds must be
    0–59 and frames 0–74. Raises ValueError otherwise."""
    match = _TIME.match(text.strip())
    if not match:
        raise ValueError(f"not a mm:ss:ff time: {text!r}")
    minutes, seconds, frames = (int(g) for g in match.groups())
    if seconds > 59 or frames >= FRAMES_PER_SECOND:
        raise ValueError(f"time out of range: {text!r}")
    return (minutes * 60 + seconds) * FRAMES_PER_SECOND + frames


def format_time(frames: int) -> str:
    """CD frames -> "mm:ss:ff"."""
    seconds, ff = divmod(int(frames), FRAMES_PER_SECOND)
    return f"{seconds // 60:02d}:{seconds % 60:02d}:{ff:02d}"


def frames_to_samples(frames: int, rate: int) -> int:
    """CD frames -> samples at `rate`. Exact for every common rate (44.1, 48, 88.2, 96, 176.4,
    192 kHz are all whole multiples of 75); otherwise rounded down."""
    return int(frames) * int(rate) // FRAMES_PER_SECOND


@dataclass
class CueTrack:
    number: int
    datatype: str = "AUDIO"
    file: str = ""        # the FILE holding this track's start (INDEX 01), as written in the sheet
    file_type: str = ""
    title: str = ""
    performer: str = ""
    songwriter: str = ""
    isrc: str = ""
    flags: set[str] = field(default_factory=set)
    pregap: int = 0        # PREGAP: frames of generated silence before the track (not in the file)
    postgap: int = 0       # POSTGAP: frames of generated silence after it
    indexes: dict[int, int] = field(default_factory=dict)       # index number -> frames into its file
    index_files: dict[int, str] = field(default_factory=dict)   # index number -> the FILE it's in
    samples: dict[int, int] | None = None   # FLAC CUESHEET: index number -> exact sample position
    rem: dict[str, str] = field(default_factory=dict)

    @property
    def pre_emphasis(self) -> bool:
        return "PRE" in self.flags

    @property
    def audio(self) -> bool:
        return self.datatype.upper() == "AUDIO"

    @property
    def start(self) -> int | None:
        """Where the track starts (INDEX 01), in frames into its file; else its first index."""
        if 1 in self.indexes:
            return self.indexes[1]
        return self.indexes[min(self.indexes)] if self.indexes else None

    def index_sample(self, number: int, rate: int) -> int | None:
        """An index point as a sample position in its file (exact from a FLAC CUESHEET)."""
        if self.samples is not None and number in self.samples:
            return self.samples[number]
        if number not in self.indexes:
            return None
        return frames_to_samples(self.indexes[number], rate)

    def start_sample(self, rate: int) -> int | None:
        if 1 in self.indexes or (self.samples and 1 in self.samples):
            return self.index_sample(1, rate)
        first = self.start
        return None if first is None else frames_to_samples(first, rate)


@dataclass
class CueSheet:
    title: str = ""
    performer: str = ""
    songwriter: str = ""
    catalog: str = ""
    cdtextfile: str = ""
    rem: dict[str, str] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)     # FILE names, in order
    tracks: list[CueTrack] = field(default_factory=list)
    source: Path | None = None       # the .cue file, or the FLAC file holding the block
    embedded: bool = False           # read from a FLAC CUESHEET block
    lead_out: int | None = None      # FLAC: sample where the last track ends
    warnings: list[str] = field(default_factory=list)

    def track(self, number: int) -> CueTrack | None:
        return next((t for t in self.tracks if t.number == number), None)

    def tracks_in(self, filename: str | Path) -> list[CueTrack]:
        """The tracks that start in this audio file (matched by file name, ignoring case and
        any folder written in the sheet)."""
        wanted = _base(str(filename))
        return [t for t in self.tracks if _base(t.file) == wanted]

    def end_sample(self, track: CueTrack, rate: int) -> int | None:
        """Where `track` ends in its file: the next track's INDEX 01 in the same file (so the gap
        before it stays with this one), else the lead-out (FLAC), else None = the end of the file."""
        after = self.tracks[self.tracks.index(track) + 1:]
        for nxt in after:
            if _base(nxt.file) != _base(track.file):
                break
            start = nxt.start_sample(rate)
            if start is not None:
                return start
        return self.lead_out if self.embedded else None


def _base(name: str) -> str:
    """A file name without any folder (either slash), lower-cased: how the sheet's FILE lines
    are matched to real files."""
    return PureWindowsPath(name.strip()).name.lower()


def _tokens(line: str) -> list[str]:
    """Split a line into words, keeping "quoted strings" (with their spaces and any
    backslashes) whole. An unclosed quote runs to the end of the line."""
    return [m.group(1) if m.group(1) is not None else m.group(2) for m in _TOKEN.finditer(line)]


def _text(raw: str, keyword: str) -> str:
    """The value of a `KEYWORD value` line: the quoted string if there is one, else the rest of
    the line as written (some sheets leave spaces unquoted)."""
    rest = raw.strip()[len(keyword):].strip()
    if rest.startswith('"'):
        end = rest.find('"', 1)
        return rest[1:end] if end > 0 else rest[1:]
    return rest


def parse(text: str, source: Path | None = None) -> CueSheet:
    """Parse the text of a .cue file. Never raises on bad lines: they're noted in `warnings`."""
    sheet = CueSheet(source=source)
    current_file, current_type = "", ""
    track: CueTrack | None = None
    text = text.lstrip("﻿")   # a byte-order mark left on the text
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        words = _tokens(line)
        command = words[0].upper()
        where = f"line {number}"
        try:
            if command == "REM":
                if len(words) >= 2:
                    key = words[1].upper()
                    value = _text(line[len(words[0]):].strip(), words[1])
                    (track.rem if track else sheet.rem)[key] = value
            elif command == "FILE":
                rest = line[len(words[0]):].strip()
                if rest.startswith('"'):
                    end = rest.find('"', 1)
                    name = rest[1:end] if end > 0 else rest[1:]
                    after = rest[end + 1:].split() if end > 0 else []
                    current_type = after[0].upper() if after else ""
                else:  # unquoted: the last word is the type if there are two or more
                    parts = rest.split()
                    name = " ".join(parts[:-1]) if len(parts) >= 2 else rest
                    current_type = parts[-1].upper() if len(parts) >= 2 else ""
                current_file = name
                sheet.files.append(name)
            elif command == "TRACK":
                track_no = int(words[1])
                if not 1 <= track_no <= 99:
                    sheet.warnings.append(f"{where}: track number {track_no} outside 1–99")
                if sheet.tracks and track_no != sheet.tracks[-1].number + 1:
                    sheet.warnings.append(f"{where}: track {track_no} doesn't follow track "
                                          f"{sheet.tracks[-1].number}")
                if not current_file:
                    sheet.warnings.append(f"{where}: TRACK before any FILE")
                track = CueTrack(number=track_no, datatype=words[2].upper() if len(words) > 2 else "AUDIO",
                                 file=current_file, file_type=current_type)
                sheet.tracks.append(track)
            elif command in ("TITLE", "PERFORMER", "SONGWRITER"):
                setattr(track if track else sheet, command.lower(), _text(line, words[0]))
            elif command == "CATALOG":
                sheet.catalog = _text(line, words[0])
            elif command == "CDTEXTFILE":
                sheet.cdtextfile = _text(line, words[0])
            elif command == "ISRC":
                if track is None:
                    raise ValueError("ISRC before any TRACK")
                track.isrc = _text(line, words[0])
            elif command == "FLAGS":
                if track is None:
                    raise ValueError("FLAGS before any TRACK")
                flags = {w.upper() for w in words[1:]}
                unknown = flags - KNOWN_FLAGS
                if unknown:
                    sheet.warnings.append(f"{where}: unknown flag(s) {', '.join(sorted(unknown))}")
                track.flags |= flags
            elif command == "INDEX":
                if track is None:
                    raise ValueError("INDEX before any TRACK")
                index_no = int(words[1])
                if not 0 <= index_no <= 99:
                    raise ValueError(f"index number {index_no} outside 0–99")
                track.indexes[index_no] = parse_time(words[2])
                track.index_files[index_no] = current_file
                if index_no == 1 and current_file != track.file:
                    # The gap was under the previous FILE; the track itself starts here.
                    track.file, track.file_type = current_file, current_type
            elif command in ("PREGAP", "POSTGAP"):
                if track is None:
                    raise ValueError(f"{command} before any TRACK")
                setattr(track, command.lower(), parse_time(words[1]))
            else:
                sheet.warnings.append(f"{where}: unknown command {words[0]!r} skipped")
        except (ValueError, IndexError) as error:
            sheet.warnings.append(f"{where}: {error or 'incomplete line'} — skipped")
    for t in sheet.tracks:
        if 1 not in t.indexes:
            sheet.warnings.append(f"track {t.number} has no INDEX 01")
    return sheet


def decode(data: bytes) -> str:
    """A text file's bytes as text (decode_with, without saying how)."""
    return decode_with(data)[0]


def decode_with(data: bytes) -> tuple[str, str]:
    """A text file's bytes as text, and the encoding that read them: a .cue sheet, or a playlist
    (playlists.py). Tried in turn:
    - a byte-order mark says which (UTF-8, or UTF-16 as Windows Notepad saves "Unicode");
    - UTF-16 without one: plain text never holds zero bytes, UTF-16 has one beside every letter;
    - UTF-8 (a file name that isn't valid Unicode, as playlists.write saves it, comes back as it was);
    - this PC's Windows code page — what older Windows software wrote: Shift-JIS on a Japanese PC,
      Windows-1252 on a Western one;
    - Windows-1252, where the few bytes it doesn't define become replacement marks."""
    if data.startswith(b"\xef\xbb\xbf"):
        attempts = [("utf-8", data[3:])]
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        attempts = [("utf-16", data)]
    elif b"\x00" in data:
        sample = data[:4096]
        odd_zeros = sample[1::2].count(0) >= sample[0::2].count(0)   # "C\0:\0" is little-endian
        attempts = [("utf-16-le" if odd_zeros else "utf-16-be", data)]
    else:
        attempts = []
    attempts += [("utf-8", data), (locale.getencoding(), data)]
    for encoding, body in attempts:
        try:
            return body.decode(encoding, errors="surrogatepass" if encoding == "utf-8" else "strict"), encoding
        except (UnicodeDecodeError, LookupError):
            pass
    return data.decode("cp1252", errors="replace"), "cp1252"


def read(path: str | Path) -> CueSheet:
    path = Path(path)
    return parse(decode(path.read_bytes()), source=path)


def from_flac(path: str | Path) -> CueSheet | None:
    """The CUESHEET block inside a FLAC file, or None if it has none (or won't open)."""
    path = Path(path)
    block, rate = flac_cuesheet(path)
    return from_flac_block(block, rate, path)


def from_flac_block(block, rate: int, path: str | Path) -> CueSheet | None:
    """A CueSheet from a FLAC CUESHEET block (tagio.flac.CueSheet: `FLACFile(path).cuesheet`),
    when the file has already been opened (library.py reads it with the file's tags)."""
    if block is None:
        return None
    path = Path(path)
    rate = rate or CD_RATE
    catalog = block.media_catalog_number
    sheet = CueSheet(catalog=catalog.decode("ascii", "replace") if isinstance(catalog, bytes) else catalog,
                     files=[path.name], source=path, embedded=True)
    for entry in block.tracks:
        if entry.track_number in (LEAD_OUT_CD, LEAD_OUT_OTHER) and not entry.indexes:
            sheet.lead_out = entry.start_offset
            continue
        isrc = entry.isrc.decode("ascii", "replace") if isinstance(entry.isrc, bytes) else entry.isrc
        samples = {index.index_number: entry.start_offset + index.index_offset for index in entry.indexes}
        sheet.tracks.append(CueTrack(
            number=entry.track_number, datatype="AUDIO" if entry.type == 0 else "DATA",
            file=path.name, file_type="FLAC", isrc=isrc,
            flags={"PRE"} if entry.pre_emphasis else set(),
            indexes={n: s * FRAMES_PER_SECOND // rate for n, s in samples.items()},
            index_files={n: path.name for n in samples}, samples=samples))
    return sheet


def cue_files(folder: str | Path) -> list[Path]:
    """The .cue files in a folder (not its subfolders), in name order. [] if it can't be read.
    (Not macOS's "._" companions, which share the name but hold Finder data.)"""
    try:
        return sorted(p for p in Path(folder).iterdir()
                      if p.suffix.lower() == ".cue" and not p.name.startswith("._") and p.is_file())
    except OSError:
        return []
