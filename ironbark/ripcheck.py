"""Rip verification (no UI): is this CD rip identical to other people's rips of the same disc?

Two public databases hold checksums of CD rips submitted by ripping programs:
- **AccurateRip** (accuraterip.com): a checksum per track, per pressing, with a "confidence"
  = how many people's rips agreed.
- **CUETools DB** (CTDB, db.cuetools.net): a checksum of the whole disc plus one per track,
  again with a confidence.

A rip matching them is bit-for-bit what many other drives read from the same disc, so it's
almost certainly an error-free copy. To look a disc up we need its **track layout** (TOC,
"table of contents": where each track starts on the CD, in sectors of 1/75 s = 588 samples), and
then the checksums of the files. Only the disc's IDs / TOC are sent; the checksums are compared
here, on this computer.

Everything below was read from the open source of CUETools (github.com/gchudov/cuetools.net,
files AccurateRip.cs, CDRepair.cs, CUEToolsDB.cs, DBEntry.cs, CDImage.cs; read 04/10/2026) and
cross-checked against whipper's accuraterip-checksum.c, not written from memory:

**AccurateRip checksums** (per track, over 16-bit stereo; a sample word is the 32-bit number with
the left sample in the low 16 bits and the right in the high 16, as stored on the CD):
- v1 = Σ (word × position) mod 2³², position = 1, 2, 3… within the track.
- v2 = Σ low 32 bits + Σ high 32 bits of the 64-bit products, mod 2³².
- The first track skips positions below 5 × 588 = 2940, the last track positions above
  N − 2940 (drives can't all read the very start and end of a disc).
- A track matches a database entry if v1 *or* v2 equals it (AccurateRip.cs `Confidence`).

**AccurateRip disc IDs** (CalculateAccurateRipId): starts in sectors (track 1 usually 0), audio
tracks only; leadout = end of the whole disc.
- id1 = Σ starts + leadout; id2 = Σ max(start, 1) × n (n = 1, 2, …) + max(leadout, 1) × (count+1).
- CDDB ID (all tracks, data too) = (Σ digit sums of (start/75 + 2)) mod 255 << 24 |
  (leadout/75 − start₁/75) << 8 | track count.
- The file: https://www.accuraterip.com/accuraterip/{id1 last hex digit}/{2nd last}/{3rd last}/
  dBAR-{tracks:03}-{id1:08x}-{id2:08x}-{cddb:08x}.bin — chunks of: count (1 byte), id1, id2,
  cddb (4 bytes each, little-endian), then per track confidence (1 byte), CRC, frame-450 CRC
  (4 bytes each). One chunk per pressing on file. 404 = disc not in the database.

**CUETools DB** (CUEToolsDB.cs, CDRepair.cs, AccurateRip.cs `CTDBCRC`):
- Lookup: http://db.cuetools.net/lookup2.php?version=3&ctdb=1&fuzzy=1&metadata=none&toc=…, the
  TOC as the start sector of every track (data tracks prefixed "-") then the leadout, joined
  with ":". (CUETools itself uses plain http for this address, so this one request is
  unencrypted: anyone on the network path can see which disc is looked up. The window says so.
  The certificate of the https address db.cuetools.net doesn't carry that name, so it can't
  simply be switched over.)
- Answer: XML `<ctdb><entry id crc32 confidence toc trackcrcs …/></ctdb>`; total = the sum of
  every entry's confidence.
- Checksums are the ordinary CRC-32 (zlib's) of the 16-bit little-endian interleaved audio,
  counted from track 1's start (a hidden track before it isn't included). The disc CRC skips
  the first 5880 samples (10 sectors) and the last 5880 + (N mod 5880), N = samples from
  track 1's start to the end of the audio. (The CTDB wiki says "5880 samples at each end"; the
  source has the extra N mod 5880 at the end — it follows the error-correction "stride" — and
  the source is what the database computes, so that's what's used.)
- Per-track CRCs: each track whole, except the first track skips its first 5880 samples and
  the last track its last 5880 + (N mod 5880) — the same skips as the disc CRC. (Read from
  `CDRepairEncode.TrackCRC` → `CTDBCRC`.)
- A track counts as matched for an entry when the entry's whole-disc CRC matches, or its own
  track CRC does (CUEToolsDB.cs `GetConfidence`). The CTDB wiki: "a CTDB confidence of 2/X or
  higher indicates that the rip in the database is probably undamaged".

**Enhanced CDs** (music plus a data track at the end): the data session starts 11 400 sectors
(152 × 75) after the audio ends (CDImage.cs subtracts 152 × 75 from the last audio track's
length when a data track follows). The data track's *length* is on the disc only, never in
the audio files — so it has to come from the ripper's log or a CDTOC tag. Without one the
disc can't be looked up, and we say so.

Limits, said plainly in the window too: checksums are compared at the rip's own offset only
(as EAC reports "accurately ripped"); a rip of a different pressing, or one made with a wrong
drive offset, can show "no match" although CUETools (which searches every offset) would
match it. CTDB's "differs in N samples" (its repair data) isn't used.
"""

from __future__ import annotations

import base64
import hashlib
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf

from . import APP_NAME, __version__, audiofile, cuesheet

SECTOR = 588                 # samples per CD sector (1/75 s at 44.1 kHz)
AR_SKIP = 5 * SECTOR         # AccurateRip: 2940 samples skipped at the disc's two ends
CTDB_SKIP = 10 * SECTOR      # CUETools DB: 5880 (+ N mod 5880 at the end)
DATA_GAP = 152 * 75          # 11 400 sectors between the audio and an enhanced CD's data track
LEAD_IN = 150                # the 2 s every CD starts with; CDTOC tags and CDDB count from it

AR_URL = ("https://www.accuraterip.com/accuraterip/{a:x}/{b:x}/{c:x}/"
          "dBAR-{n:03d}-{id1:08x}-{id2:08x}-{cddb:08x}.bin")
CTDB_URL = "http://db.cuetools.net/lookup2.php?version=3&ctdb=1&fuzzy=1&metadata=none&toc={toc}"
TIMEOUT = 20                 # seconds the server may stay silent (connecting, or between data)
DEADLINE = 30                # seconds for a whole answer, however slowly it trickles in
MAX_BYTES = 4 << 20          # an answer bigger than this isn't what we asked for
CHUNK = 64 << 10             # bytes read at a time, so a slow answer can be given up on
MAX_CONFIDENCE = 1_000_000   # a CTDB confidence above this (or below 0) isn't a real count of rips
BLOCK = 1 << 18              # samples read at a time
CD_MAX_TRACKS = 99           # the most tracks a CD can hold (the Red Book CD standard)
CD_MAX_SECTORS = 80 * 60 * 75   # 80 minutes: past the Red Book's longest disc (79:59.74)
LOG_MAX = 4 << 20            # a .log bigger than this isn't a rip log (those are a few KB) …
LOG_READ = 1 << 20           # … and only its first megabyte is read

_urlopen = urllib.request.urlopen   # tests replace this; nothing else in the player goes online


class NetworkOff(RuntimeError):
    """Raised if anything tries to go online while Settings → Allow network access is off."""


def network_allowed(prefs: dict | None) -> bool:
    """The one switch: Settings → Network → Allow network access (off unless turned on)."""
    return bool(prefs and prefs.get("allow_network"))


# -- the disc's layout ---------------------------------------------------------------
@dataclass
class Toc:
    """A CD's table of contents. Sectors, counted from the start of the disc's audio area (track
    1 normally starts at 0; a hidden track or long first gap puts it later)."""
    starts: list[int]            # every track, audio and data, in disc order
    audio: list[bool]            # per track: audio (True) or data
    leadout: int                 # end of the whole disc (after the last track, data included)
    source: str = ""             # where it came from, for the window
    certain: bool = True         # False when rebuilt from the files' lengths
    notes: list[str] = field(default_factory=list)

    @property
    def audio_indexes(self) -> list[int]:
        return [i for i, a in enumerate(self.audio) if a]

    @property
    def audio_count(self) -> int:
        return len(self.audio_indexes)

    @property
    def audio_starts(self) -> list[int]:
        return [self.starts[i] for i in self.audio_indexes]

    @property
    def has_data(self) -> bool:
        return not all(self.audio)

    @property
    def audio_end(self) -> int:
        """The sector after the last audio track: before an enhanced CD's data track, the gap
        between the sessions isn't audio."""
        last = self.audio_indexes[-1]
        if last + 1 < len(self.starts):
            return self.starts[last + 1] - DATA_GAP
        return self.leadout

    @property
    def audio_lengths(self) -> list[int]:
        starts = self.audio_starts
        return [b - a for a, b in zip(starts, starts[1:] + [self.audio_end])]

    def cddb_id(self) -> int:
        digits = sum(_digit_sum(s // 75 + 2) for s in self.starts)
        return (((digits % 255) << 24) + ((self.leadout // 75 - self.starts[0] // 75) << 8)
                + len(self.starts)) & 0xFFFFFFFF

    def accuraterip_ids(self) -> tuple[int, int, int]:
        id1 = id2 = 0
        for n, start in enumerate(self.audio_starts, start=1):
            id1 += start
            id2 += max(start, 1) * n
        id1 += self.leadout
        id2 += max(self.leadout, 1) * (self.audio_count + 1)
        return id1 & 0xFFFFFFFF, id2 & 0xFFFFFFFF, self.cddb_id()

    def accuraterip_id(self) -> str:
        """As rippers write it, e.g. "009-0011a0b8-00e1c3f0-8d0cf009"."""
        id1, id2, cddb = self.accuraterip_ids()
        return f"{self.audio_count:03d}-{id1:08x}-{id2:08x}-{cddb:08x}"

    def accuraterip_url(self) -> str:
        id1, id2, cddb = self.accuraterip_ids()
        return AR_URL.format(a=id1 & 0xF, b=id1 >> 4 & 0xF, c=id1 >> 8 & 0xF, n=self.audio_count,
                             id1=id1, id2=id2, cddb=cddb)

    def ctdb_toc(self) -> str:
        return ":".join(("" if a else "-") + str(s) for s, a in zip(self.starts, self.audio)) + f":{self.leadout}"

    def ctdb_url(self) -> str:
        return CTDB_URL.format(toc=urllib.parse.quote(self.ctdb_toc(), safe=":-"))

    def ctdb_tocid(self) -> str:
        """CUETools' TOC ID (CDImage.cs `TOCID`), as EAC's CTDB plug-in logs it: SHA-1 of the
        audio tracks' starts and end relative to the first, in hex, padded to 100 tracks."""
        first = self.audio_starts[0]
        text = "".join(f"{s - first:08X}" for s in self.audio_starts[1:])
        text += f"{self.audio_end - first:08X}" + "0" * (max(0, 100 - self.audio_count) * 8)
        digest = base64.b64encode(hashlib.sha1(text.encode("ascii")).digest()).decode("ascii")
        return digest.replace("+", ".").replace("/", "_").replace("=", "-")


def _digit_sum(n: int) -> int:
    return sum(int(c) for c in str(n))


def toc_from_cdtoc(text: str, source: str = "the CDTOC tag") -> Toc | None:
    """A CDTOC tag (what EAC / foobar2000 / CUETools write): hex numbers joined by "+": the audio
    track count, each track's start + 150, the leadout + 150; data tracks *before* the audio are
    listed at the end with an "X" (CDImage.cs `FromTag`)."""
    try:
        ids = [p.strip() for p in text.strip().split("+")]
        audio_tracks = int(ids[0], 16)
        count = len(ids) - 2
        if count < 1 or audio_tracks < 1 or audio_tracks > count:
            return None
        if count > audio_tracks and ids[-1][:1].upper() == "X":
            leading = [int(x[1:], 16) - LEAD_IN for x in ids[2 + audio_tracks:2 + count]]
            rest = [int(x, 16) - LEAD_IN for x in ids[1:2 + audio_tracks]]
            starts, leadout = leading + rest[:-1], rest[-1]
            audio = [False] * len(leading) + [True] * audio_tracks
        else:
            values = [int(x, 16) - LEAD_IN for x in ids[1:2 + count]]
            starts, leadout = values[:-1], values[-1]
            audio = [i < audio_tracks for i in range(count)]
    except (ValueError, IndexError):
        return None
    if starts != sorted(starts) or leadout <= starts[-1] or min(starts) < 0:
        return None
    return Toc(starts, audio, leadout, source=source)


_LOG_ROW = re.compile(r"^\s*(\d+)\s*\|\s*[\d:.]+\s*\|\s*[\d:.]+\s*\|\s*(\d+)\s*\|\s*(\d+)\s*$", re.MULTILINE)


def tocs_from_log(text: str, source: str = "the rip log") -> list[Toc]:
    """The "TOC of the extracted CD" tables in an EAC or XLD log (Track | Start | Length | Start
    sector | End sector — the same columns in every language EAC writes). A track starting
    exactly 11 400 sectors after the one before ends is an enhanced CD's data track."""
    tocs, rows = [], []

    def finish():
        if rows:
            starts = [r[1] for r in rows]
            audio = [True] * len(rows)
            for k in range(1, len(rows)):
                if rows[k][1] - (rows[k - 1][2] + 1) == DATA_GAP:
                    audio[k:] = [False] * (len(rows) - k)
                    break
            if starts == sorted(starts) and any(audio):
                tocs.append(Toc(starts, audio, rows[-1][2] + 1, source=source))
        rows.clear()

    for match in _LOG_ROW.finditer(text):
        number, start, end = (int(g) for g in match.groups())
        if rows and number != rows[-1][0] + 1:
            finish()
        rows.append((number, start, end))
    finish()
    return tocs


def toc_from_lengths(lengths: list[int]) -> Toc | None:
    """Rebuilt from the files alone: track 1 at sector 0, each next track where the last one
    ended. Right for most rips (gaps kept at the end of the track before), but blind to a hidden
    first-track gap or a data track — so it's marked less certain."""
    if not lengths or any(n % SECTOR or n <= 0 for n in lengths):
        return None
    starts, at = [], 0
    for n in lengths:
        starts.append(at)
        at += n // SECTOR
    return Toc(starts, [True] * len(starts), at, source="rebuilt from the track lengths", certain=False)


# -- the files, laid out on the disc -------------------------------------------------
@dataclass
class Rip:
    toc: Toc
    files: list[Path]       # the audio, in disc order, read one after another
    origin: int             # the disc sector where the first file starts (e.g. 33 when a
                            # hidden 33-sector gap before track 1 isn't in the files)
    notes: list[str] = field(default_factory=list)

    def track_bounds(self) -> list[tuple[int, int]]:
        """Each audio track as [start, end) sample positions in the files read end to end."""
        starts = [(s - self.origin) * SECTOR for s in self.toc.audio_starts]
        ends = starts[1:] + [(self.toc.audio_end - self.origin) * SECTOR]
        return list(zip(starts, ends))


def fit(toc: Toc, files: list[Path], lengths: list[int]) -> Rip | str:
    """Lay these files over this TOC, or say why they don't fit. The files read end to end must
    finish exactly where the disc's audio ends, and start no later than track 1."""
    if not toc.audio_count:
        return f"{toc.source} has no audio tracks"
    total = sum(lengths)
    if total % SECTOR:
        return "the files don't add up to whole CD sectors"
    origin = toc.audio_end - total // SECTOR
    if origin < 0 or origin > toc.audio_starts[0]:
        diff = origin if origin > 0 else -origin
        more = "shorter" if origin > 0 else "longer"
        return (f"the files are {diff:,} sectors ({diff / 75:.2f} s) {more} than the disc in "
                f"{toc.source}")
    rip = Rip(toc, list(files), origin)
    if len(files) == toc.audio_count and len(files) > 1:
        cuts, at = [], 0
        for n in lengths[:-1]:
            at += n
            cuts.append(at)
        if cuts != [(s - origin) * SECTOR for s in toc.audio_starts[1:]]:
            rip.notes.append("the files are split at different points from the disc's tracks; "
                             "the check uses the disc's own track boundaries")
    return rip


def rip_from_cue(sheet: cuesheet.CueSheet, files: list[Path], lengths: list[int],
                 source: str) -> Rip | str:
    """A .cue sheet's layout over these files. Handles one file per track (EAC's usual, gaps at
    the end of the track before), a single-file image, a hidden gap before track 1 that's in the
    file (INDEX 00 at 0) or generated (PREGAP), and sheets whose FILE names are stale."""
    tracks = sheet.tracks
    if not tracks:
        return f"{source} lists no tracks"
    if any(not t.audio for t in tracks):
        return (f"{source} has a data track (an enhanced CD): its length isn't in a .cue sheet "
                "— the rip's log or a CDTOC tag is needed")
    if any(t.postgap for t in tracks) or any(t.pregap for t in tracks[1:]):
        return f"{source} adds silence between tracks (PREGAP/POSTGAP) that isn't in the files"
    if any(1 not in t.indexes for t in tracks):
        return f"{source} has a track without a start (INDEX 01)"
    names: list[str] = []
    for t in tracks:
        for name in [t.index_files.get(n, t.file) for n in sorted(t.indexes)]:
            if name not in names:
                names.append(name)
    by_name = {f.name.lower(): (f, n) for f, n in zip(files, lengths)}
    notes = []
    mapped = [by_name.get(cuesheet._base(n)) for n in names]
    if all(mapped):
        pass
    elif len(names) == len(files):
        mapped = list(zip(files, lengths))
        notes.append(f"{source}'s file names don't match these files; matched in order")
    elif len(names) == 1 and len(files) == len(tracks):
        return _image_cue_over_tracks(sheet, files, lengths, source)
    else:
        return f"{source} names {len(names)} file(s), but there are {len(files)} here"
    pregap = tracks[0].pregap
    if pregap and tracks[0].indexes[1] != 0:
        return f"{source} puts generated silence inside a file"
    file_start, at = {}, pregap * SECTOR
    for name, (_, n) in zip(names, mapped):
        file_start[name] = at
        at += n
    if at % SECTOR:
        return "the files don't add up to whole CD sectors"
    starts = []
    for t in tracks:
        sample = file_start[t.index_files.get(1, t.file)] + (t.index_sample(1, cuesheet.CD_RATE) or 0)
        if sample % SECTOR:
            return f"{source}: track {t.number} doesn't start on a CD sector"
        starts.append(sample // SECTOR)
    toc = Toc(starts, [True] * len(starts), at // SECTOR, source=source)
    result = fit(toc, [f for f, _ in mapped], [n for _, n in mapped])
    if isinstance(result, Rip):
        result.notes[:0] = notes
    return result


def _image_cue_over_tracks(sheet, files, lengths, source) -> Rip | str:
    """A .cue for a one-file image of the disc, kept beside files split per track: the track
    starts come from the sheet; the files must be cut at those starts (gaps at the end of the
    track before)."""
    origin = sheet.tracks[0].pregap
    starts = [origin + sheet.tracks[k].indexes[1] for k in range(len(sheet.tracks))]
    for k in range(len(files) - 1):
        if lengths[k] != (starts[k + 1] - starts[k]) * SECTOR:
            return f"{source} is for a one-file image, and these files aren't cut at its track starts"
    if lengths[-1] % SECTOR:
        return "the files don't add up to whole CD sectors"
    toc = Toc(starts, [True] * len(starts), starts[-1] + lengths[-1] // SECTOR, source=source)
    result = fit(toc, files, lengths)
    if isinstance(result, Rip):
        result.notes.append(f"{source} is for a one-file image; its track starts match these files")
    return result


# -- finding the rip for a track -----------------------------------------------------
class NotARip(Exception):
    """These files can't be checked (not CD audio, can't be read, no layout fits). The message
    says why, in plain words."""


def disc_files(track: Path) -> list[Path]:
    """The disc a track belongs to: the audio files in its own folder (a "CD2" folder is its own
    disc), in play order. When the folder holds more than one disc, only the track's own (see
    `_discs`)."""
    return _disc_and_others(track)[0]


# "1-01 Title.flac", "2_03.flac", "CD2-05 …": a disc number before the track number.
_DISC_PREFIX = re.compile(r"^\s*(?:(?:cd|disc|disk)\s*)?(\d{1,2})\s*[-_.]\s*\d{1,3}(?!\d)", re.IGNORECASE)


def _discs(tracks: list) -> list[list]:
    """The folder's tracks (library Tracks, in play order) split into discs: by DISCNUMBER tag
    when the files carry more than one disc number; otherwise by a disc number at the start of
    the file names, when every file has one and they aren't all the same. One group when
    neither (a single disc)."""
    def from_name(track):
        found = _DISC_PREFIX.match(audiofile.real_path(track.path).name)
        return int(found.group(1)) if found else None

    tagged = [t.disc_no for t in tracks]
    named = [from_name(t) for t in tracks]
    if len(set(tagged) - {None}) > 1:
        keys = tagged
    elif None not in named and len(set(named)) > 1:
        keys = named
    else:
        return [tracks]
    groups: dict = {}
    for key, t in zip(keys, tracks):
        groups.setdefault(key, []).append(t)
    return list(groups.values())


def _disc_and_others(track: Path) -> tuple[list[Path], list[Path]]:
    """(the audio files of the track's disc, the folder's other audio files), real files in
    play order (a CUE image once)."""
    from .library import _in_play_order, is_music_name
    real_track = audiofile.real_path(track)
    folder = real_track.parent
    paths = [p for p in folder.iterdir() if is_music_name(p.name) and p.is_file()]
    ours: list[Path] = []
    others: list[Path] = []
    for group in _discs(_in_play_order(folder, paths)):
        files: list[Path] = []
        for t in group:   # a CUE image is listed as its tracks: check the file
            real = audiofile.real_path(t.path)
            if real not in files:
                files.append(real)
        if real_track in files and not ours:
            ours = files
        else:
            others += [f for f in files if f not in others]
    return ours, [f for f in others if f not in ours]


def _side_files(folder: Path, suffix: str) -> list[Path]:
    """.cue / .log files in the folder and its subfolders one level down (rippers' leftovers often
    sit in an "Archive" or "Scans" folder)."""
    found = []
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return []
    for entry in entries:
        if entry.is_file() and entry.suffix.lower() == suffix:
            found.append(entry)
    for entry in entries:
        if entry.is_dir():
            try:
                found += sorted(p for p in entry.iterdir() if p.is_file() and p.suffix.lower() == suffix)
            except OSError:
                pass
    return found


def _where(path: Path, folder: Path) -> str:
    try:
        return str(path.relative_to(folder))
    except ValueError:
        return path.name


def find_rip(track: Path) -> Rip:
    """Work out the disc's layout for the files around `track`. Tries, in order: a CUESHEET block
    inside the file (one-file rips), a CDTOC tag, the ripper's log (EAC / XLD), a .cue sheet,
    and last the files' own lengths. The first one that fits the files wins; the ones that
    didn't are listed in the notes. Raises NotARip when nothing can be checked. One track of a CUE
    image checks the whole image (the disc). In a folder holding several discs only the track's
    own disc is used, and files of another kind beside the rip (an MP3 copy, a hi-res bonus
    track) are left out and named in the notes."""
    track = audiofile.real_path(track)
    folder = track.parent
    try:
        files, other_discs = _disc_and_others(track)
    except OSError as error:
        raise NotARip(f"couldn't read the folder ({error})") from error
    files = files if track in files else [track]
    info = _read_info(track)
    problem = _not_cd_audio(track, info)
    if problem:                # the chosen track itself must be CD audio …
        raise NotARip(problem)
    lengths, skipped = [], []
    for path in files:         # … but the folder may also hold copies in other formats
        if path == track:
            lengths.append(info.frames)
            continue
        other = _read_info(path) if path.suffix.lower() == track.suffix.lower() else None
        if other is None or _not_cd_audio(path, other):
            skipped.append(path)
        else:
            lengths.append(other.frames)
    files = [f for f in files if f not in skipped]
    if len(files) > CD_MAX_TRACKS:
        raise NotARip(f"{len(files)} files belong together here, more than the {CD_MAX_TRACKS} tracks a CD "
                      "can hold; if the folder holds several discs, tag each file's disc number "
                      "(DISCNUMBER) or put each disc in its own folder")
    notes = []
    if other_discs:
        notes.append(f"the folder holds more than one disc; only this track's disc was checked "
                     f"({len(files)} of {len(files) + len(other_discs) + len(skipped)} files)")
    if skipped:
        names = ", ".join(p.name for p in skipped[:3])
        names += f" and {len(skipped) - 3} more" if len(skipped) > 3 else ""
        notes.append(f"left out, not the same kind of CD-audio file as this track: {names}")

    tried: list[str] = []

    def accept(result: Rip | str) -> Rip | None:
        if isinstance(result, Rip):
            result.notes = notes + [f"tried first: {t}" for t in tried] + result.notes
            return result
        tried.append(result)
        return None

    # 1. A CUESHEET block in a one-file rip.
    if len(files) == 1 and track.suffix.lower() == ".flac":
        sheet = cuesheet.from_flac(track)
        if sheet is not None and sheet.tracks:
            if any(not t.audio for t in sheet.tracks):
                tried.append("the file's cue sheet has a data track, whose length isn't recorded there")
            elif sheet.lead_out and all(t.samples and 1 in t.samples and t.samples[1] % SECTOR == 0
                                        for t in sheet.tracks) and sheet.lead_out % SECTOR == 0:
                toc = Toc([t.samples[1] // SECTOR for t in sheet.tracks], [True] * len(sheet.tracks),
                          sheet.lead_out // SECTOR, source="the cue sheet inside the file")
                rip = accept(fit(toc, files, lengths))
                if rip:
                    return rip
            else:
                tried.append("the file's cue sheet isn't on CD sectors")
    # 2. A CDTOC tag (any of the files; they all carry the same one).
    for path in files[:1] + ([track] if track != files[0] else []):
        text = _cdtoc_tag(path)
        if text:
            toc = toc_from_cdtoc(text)
            if toc is None:
                tried.append("the CDTOC tag couldn't be read")
            else:
                rip = accept(fit(toc, files, lengths))
                if rip:
                    return rip
            break
    # 3. The ripper's log.
    for log in _side_files(folder, ".log"):
        try:
            if log.stat().st_size > LOG_MAX:   # not a rip log: those are a few kilobytes
                continue
            with log.open("rb") as file:
                text = cuesheet.decode(file.read(LOG_READ))
        except OSError:
            continue
        for toc in tocs_from_log(text, source=f"the rip log ({_where(log, folder)})"):
            if toc.audio_count == len(files) or len(files) == 1:
                rip = accept(fit(toc, files, lengths))
                if rip:
                    return rip
    # 4. A .cue sheet (not one written for another disc's files in the same folder).
    def stems(paths):
        return {Path(p.name.lower()).stem for p in paths}

    elsewhere = stems(other_discs + skipped) - stems(files)
    for cue in _side_files(folder, ".cue"):
        try:
            sheet = cuesheet.read(cue)
        except OSError:
            continue
        source = f"the cue sheet ({_where(cue, folder)})"
        named = {Path(cuesheet._base(name)).stem for name in sheet.files}
        if named & elsewhere:
            tried.append(f"{source} is for other files in the folder")
            continue
        rip = accept(rip_from_cue(sheet, files, lengths, source))
        if rip:
            return rip
    # 5. The files' own lengths.
    toc = toc_from_lengths(lengths)
    if toc is None:
        reasons = "; ".join(tried)
        raise NotARip("no track layout found, and the files aren't cut on CD sectors, so it can't be "
                      "rebuilt" + (f" ({reasons})" if reasons else ""))
    if toc.leadout > CD_MAX_SECTORS:
        raise NotARip(f"no track layout found, and the files last {toc.leadout / 75 / 60:.0f} minutes, more "
                      "than a CD can hold (80 minutes), so they can't be one disc; if the folder holds "
                      "several discs, tag each file's disc number (DISCNUMBER) or put each disc in its "
                      "own folder")
    rip = accept(fit(toc, files, lengths))
    if rip is None:
        raise NotARip("; ".join(tried))
    return rip


def _read_info(path: Path):
    try:
        return audiofile.info(path)
    except (sf.LibsndfileError, OSError, RuntimeError) as error:
        raise NotARip(f"couldn't read {path.name}") from error


def _not_cd_audio(path: Path, info) -> str:
    """Why this file isn't an exact copy of CD audio, or "" when it is."""
    if path.suffix.lower() == ".mp3" or info.format == "MP3":
        return "MP3 files can't be checked: they aren't an exact copy of a CD"
    if info.samplerate != cuesheet.CD_RATE or info.channels != 2 or info.subtype != "PCM_16":
        bits = {"PCM_24": 24, "PCM_32": 32, "PCM_16": 16, "FLOAT": 32}.get(info.subtype, 0)
        return (f"{path.name} isn't CD audio ({bits or '?'}-bit, {info.samplerate / 1000:g} kHz, "
                f"{info.channels} channel{'s' if info.channels != 1 else ''}); rip checks are for "
                "CD rips (16-bit, 44.1 kHz, stereo)")
    return ""


def _cdtoc_tag(path: Path) -> str:
    if path.suffix.lower() != ".flac":
        return ""
    from .tagio import TagError
    from .tagio.flac import FLACFile
    try:
        tags = FLACFile(path).tags
    except (TagError, OSError):
        return ""
    if not tags:
        return ""
    for key, value in tags:
        if key.upper() == "CDTOC" and value:
            return str(value)
    return ""


# -- the checksums -------------------------------------------------------------------
@dataclass
class Checksums:
    ar_v1: list[int]
    ar_v2: list[int]
    ctdb_tracks: list[int]
    ctdb_disc: int
    # Plain CRC-32s of the 16-bit audio, for the rip log (only when asked: checksums(log_crcs=True)).
    # "nz" = every 16-bit sample that is 0 left out (XLD's "skip zero"; EAC's null samples off).
    crc_files: list[int] = field(default_factory=list)       # each file whole
    crc_files_nz: list[int] = field(default_factory=list)
    crc_tracks: list[int] = field(default_factory=list)      # each track of the disc layout, whole
    crc_tracks_nz: list[int] = field(default_factory=list)
    crc_disc: int = 0                                        # every file, end to end
    crc_disc_nz: int = 0


def ar_sums(words: np.ndarray, positions: np.ndarray) -> tuple[int, int]:
    """Σ low and Σ high 32 bits of word × position (both as unsigned 64-bit; no product can
    overflow: both are under 2³²)."""
    products = words.astype(np.uint64) * positions.astype(np.uint64)
    low = int(np.sum(products & np.uint64(0xFFFFFFFF), dtype=np.uint64))
    high = int(np.sum(products >> np.uint64(32), dtype=np.uint64))
    return low, high


def _crc_pair(block: np.ndarray, plain: int, nonzero: int) -> tuple[int, int]:
    """Carry on a plain CRC-32 and a zero-skipping one over a block of 16-bit samples."""
    data = np.ascontiguousarray(block, dtype="<i2")
    flat = data.reshape(-1)
    return zlib.crc32(data.tobytes(), plain), zlib.crc32(flat[flat != 0].tobytes(), nonzero)


def checksums(rip: Rip, cancel: threading.Event | None = None, progress=lambda p: None,
              log_crcs: bool = False) -> Checksums | None:
    """Read the files once, end to end, and work out every checksum (with `log_crcs`, the plain
    CRC-32s a rip log holds too). None if cancelled."""
    bounds = rip.track_bounds()
    count = len(bounds)
    disc_lo, disc_hi = bounds[0][0], bounds[-1][1]
    tail = CTDB_SKIP + (disc_hi - disc_lo) % CTDB_SKIP
    ar_lo = [AR_SKIP if k == 0 else 1 for k in range(count)]                 # 1-based positions
    ar_hi = [hi - lo - (AR_SKIP if k == count - 1 else 0) for k, (lo, hi) in enumerate(bounds)]
    ct_lo = [lo + (CTDB_SKIP if k == 0 else 0) for k, (lo, hi) in enumerate(bounds)]
    ct_hi = [hi - (tail if k == count - 1 else 0) for k, (lo, hi) in enumerate(bounds)]
    low = [0] * count
    high = [0] * count
    crcs = [0] * count
    disc = 0
    at = 0
    plain = {"files": [], "files_nz": [], "tracks": [0] * count, "tracks_nz": [0] * count, "disc": [0, 0]}
    for path in rip.files:
        file_crc = [0, 0]
        with audiofile.open(path) as f:
            while True:
                if cancel is not None and cancel.is_set():
                    return None
                block = f.read(BLOCK, dtype="int16", always_2d=True)
                n = len(block)
                if not n:
                    break
                end = at + n
                if log_crcs:
                    file_crc = list(_crc_pair(block, *file_crc))
                    plain["disc"] = list(_crc_pair(block, *plain["disc"]))
                    for k, (lo, hi) in enumerate(bounds):
                        first, last = max(at, lo), min(end, hi)
                        if last > first:
                            plain["tracks"][k], plain["tracks_nz"][k] = _crc_pair(
                                block[first - at:last - at], plain["tracks"][k], plain["tracks_nz"][k])
                words = None
                for k, (lo, hi) in enumerate(bounds):
                    if hi <= at or lo >= end:
                        continue
                    # AccurateRip: positions within the track, 1-based, inside the skips
                    first = max(at, lo + ar_lo[k] - 1)
                    last = min(end, lo + ar_hi[k])
                    if last > first:
                        if words is None:
                            u = block.view(np.uint16).astype(np.uint32)
                            words = u[:, 0] | (u[:, 1] << np.uint32(16))
                        positions = np.arange(first - lo + 1, last - lo + 1, dtype=np.uint64)
                        l_, h_ = ar_sums(words[first - at:last - at], positions)
                        low[k] += l_
                        high[k] += h_
                    # CTDB track CRC
                    first, last = max(at, ct_lo[k]), min(end, ct_hi[k])
                    if last > first:
                        crcs[k] = zlib.crc32(np.ascontiguousarray(block[first - at:last - at], dtype="<i2").tobytes(),
                                             crcs[k])
                first, last = max(at, disc_lo + CTDB_SKIP), min(end, disc_hi - tail)
                if last > first:
                    disc = zlib.crc32(np.ascontiguousarray(block[first - at:last - at], dtype="<i2").tobytes(), disc)
                at = end
                progress(min(1.0, at / max(1, disc_hi)))
        plain["files"].append(file_crc[0])
        plain["files_nz"].append(file_crc[1])
    if at < disc_hi:
        raise NotARip("the files ended early (one may be damaged)")
    v1 = [x & 0xFFFFFFFF for x in low]
    v2 = [(lo_ + hi_) & 0xFFFFFFFF for lo_, hi_ in zip(low, high)]
    sums = Checksums(v1, v2, crcs, disc)
    if log_crcs:
        sums.crc_files, sums.crc_files_nz = plain["files"], plain["files_nz"]
        sums.crc_tracks, sums.crc_tracks_nz = plain["tracks"], plain["tracks_nz"]
        sums.crc_disc, sums.crc_disc_nz = plain["disc"]
    return sums


# -- the databases -------------------------------------------------------------------
class Cancelled(Exception):
    """The check was cancelled (its window closed) while waiting for an answer."""


def fetch(url: str, prefs: dict | None, cancel: threading.Event | None = None,
          https_only: bool = False) -> bytes | None:
    """GET one address. None = not found (404). Refuses unless network access is allowed —
    checked here, at the one place this program goes online, not just in the window. Gives up
    after DEADLINE seconds (TimeoutError) and stops when `cancel` is set (Cancelled), checked
    between pieces of the answer. `https_only`: an answer that a redirect brought from a plain
    http:// address is refused (URLError)."""
    if not network_allowed(prefs):
        raise NetworkOff("network access is off (Settings → Network)")
    if cancel is not None and cancel.is_set():
        raise Cancelled()
    request = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME.replace(' ', '-')}/{__version__}"})
    deadline = time.monotonic() + DEADLINE
    try:
        with _urlopen(request, timeout=TIMEOUT) as response:
            answered_from = str(getattr(response, "url", None) or url)   # (where any redirects ended)
            if https_only and not answered_from.lower().startswith("https://"):
                raise urllib.error.URLError(f"the answer came from a plain http address: {answered_from}")
            read = getattr(response, "read1", response.read)   # read1: whatever has arrived
            data = bytearray()
            while len(data) < MAX_BYTES:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                if time.monotonic() > deadline:
                    raise TimeoutError(f"no complete answer in {DEADLINE} s")
                piece = read(min(CHUNK, MAX_BYTES - len(data)))
                if not piece:
                    break
                data += piece
            return bytes(data)
    except urllib.error.HTTPError as error:
        error.close()   # its connection, now rather than whenever it's garbage-collected
        if error.code == 404:
            return None
        raise


@dataclass
class ArPressing:
    id1: int
    id2: int
    cddb: int
    tracks: list[tuple[int, int, int]]   # per track: (confidence, CRC, frame-450 CRC)


def parse_accuraterip(data: bytes) -> list[ArPressing]:
    """The .bin file: one chunk per pressing (AccurateRip.cs `ContactAccurateRip`)."""
    pressings, at = [], 0
    while at + 13 <= len(data):
        count = data[at]
        id1, id2, cddb = (int.from_bytes(data[at + 1 + 4 * i:at + 5 + 4 * i], "little") for i in range(3))
        at += 13
        tracks = []
        for _ in range(count):
            if at + 9 > len(data):
                raise ValueError("AccurateRip's answer was cut short")
            tracks.append((data[at], int.from_bytes(data[at + 1:at + 5], "little"),
                           int.from_bytes(data[at + 5:at + 9], "little")))
            at += 9
        pressings.append(ArPressing(id1, id2, cddb, tracks))
    if at != len(data):
        raise ValueError("AccurateRip's answer was cut short")
    return pressings


@dataclass
class CtdbEntry:
    id: str
    crc32: int
    confidence: int
    toc: str
    trackcrcs: list[int] | None


def parse_ctdb(data: bytes) -> list[CtdbEntry]:
    """lookup2.php's XML: `<ctdb><entry …/></ctdb>` (namespace ignored)."""
    root = ET.fromstring(data)
    entries = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "entry" or not element.get("toc"):
            continue
        try:
            crcs = element.get("trackcrcs")
            confidence = int(element.get("confidence", "0"))
            if not 0 <= confidence <= MAX_CONFIDENCE:   # not a count of rips: a broken or forged answer
                continue
            entries.append(CtdbEntry(
                id=element.get("id", ""), crc32=int(element.get("crc32", "0"), 16),
                confidence=confidence, toc=element.get("toc", ""),
                trackcrcs=[int(c, 16) for c in crcs.split()] if crcs else None))
        except ValueError:
            continue
    return entries


@dataclass
class TrackCheck:
    number: int
    ar_v1: int = 0              # submissions agreeing by the v1 checksum
    ar_v2: int = 0              # … and by the v2 checksum
    ar_total: int = 0           # every confidence on file for this track, all chunks
    ctdb_confidence: int = 0

    # AccurateRip keeps v1 and v2 results in separate chunks of its file, so the same rips can
    # be counted under both (Brothers in Arms, 04/10/2026: one chunk of v2 checksums at 200, one
    # of v1 at 200). Adding them would double-count; like EAC, show the larger.
    @property
    def ar_confidence(self) -> int:
        return max(self.ar_v1, self.ar_v2)

    @property
    def ar_version(self) -> int:
        return 2 if self.ar_v2 >= self.ar_v1 and self.ar_v2 else 1 if self.ar_v1 else 0


@dataclass
class Verification:
    rip: Rip | None = None
    sums: Checksums | None = None
    problem: str = ""                       # set when the disc couldn't be checked at all
    ar_status: str = "not asked"            # "found", "not found", "error", "not asked"
    ar_error: str = ""
    ar_pressings: int = 0
    ctdb_status: str = "not asked"
    ctdb_error: str = ""
    ctdb_total: int = 0
    ctdb_disc: int = 0                      # confidence of entries whose whole-disc CRC matches
    tracks: list[TrackCheck] = field(default_factory=list)


def compare_accuraterip(sums: Checksums, pressings: list[ArPressing], tracks: list[TrackCheck]) -> None:
    for k, track in enumerate(tracks):
        for pressing in pressings:
            if k >= len(pressing.tracks):
                continue
            confidence, crc, _ = pressing.tracks[k]
            track.ar_total += confidence
            if crc and crc == sums.ar_v2[k]:
                track.ar_v2 += confidence
            elif crc and crc == sums.ar_v1[k]:
                track.ar_v1 += confidence


def compare_ctdb(sums: Checksums, entries: list[CtdbEntry], toc: Toc, tracks: list[TrackCheck]) -> tuple[int, int]:
    """(total confidence, confidence of entries matching the whole disc). Fills per-track
    confidence: an entry counts for a track if its whole-disc CRC matches, or its own CRC for
    that track does."""
    ours = toc.ctdb_toc()
    total = sum(e.confidence for e in entries)
    disc = 0
    for entry in entries:
        whole = entry.crc32 == sums.ctdb_disc and entry.toc == ours
        if whole:
            disc += entry.confidence
        for k, track in enumerate(tracks):
            if whole or (entry.trackcrcs and len(entry.trackcrcs) == len(tracks)
                         and entry.trackcrcs[k] == sums.ctdb_tracks[k]):
                track.ctdb_confidence += entry.confidence
    return total, disc


def _reason(error: Exception) -> str:
    if isinstance(error, urllib.error.HTTPError):
        return f"the server answered {error.code}"
    if isinstance(error, ssl.SSLCertVerificationError) or isinstance(
            getattr(error, "reason", None), ssl.SSLCertVerificationError):
        return ("couldn't confirm the server is the real one: its certificate couldn't be checked on "
                "this computer (Windows may be missing the root certificate it needs, or something on "
                "the network is intercepting secure connections)")
    if isinstance(error, urllib.error.URLError):
        return f"couldn't connect ({error.reason})"
    if isinstance(error, TimeoutError):
        return "no answer in time"
    if isinstance(error, (ValueError, ET.ParseError)):
        return f"unexpected answer ({error})"
    return str(error) or type(error).__name__


def _pressings_for(toc: Toc, data: bytes) -> list[ArPressing]:
    """The AccurateRip answer's chunks for this disc. Every chunk of a real answer repeats the
    disc's IDs and track count; an answer with none of those (a proxy's or portal's page that
    happened to parse) is refused rather than read as "no track matches"."""
    wanted = toc.accuraterip_ids()
    pressings = [p for p in parse_accuraterip(data)
                 if (p.id1, p.id2, p.cddb) == wanted and len(p.tracks) == toc.audio_count]
    if not pressings:
        raise ValueError("the answer isn't about this disc")
    return pressings


def look_up(rip: Rip, sums: Checksums, prefs: dict | None,
            cancel: threading.Event | None = None) -> Verification:
    """Ask both databases (two requests: the AccurateRip .bin and one CTDB lookup) and compare.
    Raises Cancelled if `cancel` is set on the way."""
    result = Verification(rip=rip, sums=sums,
                          tracks=[TrackCheck(number=k + 1) for k in range(rip.toc.audio_count)])
    try:
        data = fetch(rip.toc.accuraterip_url(), prefs, cancel)
        if data is None:
            result.ar_status = "not found"
        else:
            pressings = _pressings_for(rip.toc, data)
            result.ar_status, result.ar_pressings = "found", len(pressings)
            compare_accuraterip(sums, pressings, result.tracks)
    except (NetworkOff, Cancelled):
        raise
    except Exception as error:  # noqa: BLE001 — any failure is reported, never a crash
        result.ar_status, result.ar_error = "error", _reason(error)
    try:
        data = fetch(rip.toc.ctdb_url(), prefs, cancel)
        entries = parse_ctdb(data) if data else []
        if not entries:
            result.ctdb_status = "not found"
        else:
            result.ctdb_status = "found"
            result.ctdb_total, result.ctdb_disc = compare_ctdb(sums, entries, rip.toc, result.tracks)
    except (NetworkOff, Cancelled):
        raise
    except Exception as error:  # noqa: BLE001
        result.ctdb_status, result.ctdb_error = "error", _reason(error)
    return result


def verify(track: Path, prefs: dict | None, cancel: threading.Event | None = None,
           progress=lambda p: None) -> Verification | None:
    """The whole check for the disc `track` is on. None if cancelled. Never raises for a disc
    that can't be checked: `problem` says why."""
    if not network_allowed(prefs):
        return Verification(problem="network access is off (Settings → Network → Allow network access)")
    try:
        rip = find_rip(track)
        sums = checksums(rip, cancel, progress)
    except NotARip as error:
        return Verification(problem=str(error))
    except (sf.LibsndfileError, OSError, RuntimeError) as error:
        return Verification(problem=f"couldn't read the files ({error})")
    if sums is None:
        return None
    try:
        return look_up(rip, sums, prefs, cancel)
    except Cancelled:
        return None


# -- wording ---------------------------------------------------------------------------
def _list(numbers: list[int]) -> str:
    return ", ".join(str(n) for n in numbers)


def describe(v: Verification) -> list[tuple[str, str, str]]:
    """(label, text, tone) rows for the Check section; tone "good" / "warn" / "plain" / "muted"."""
    if v.problem:
        return [("Rip check", f"Can't check this disc: {v.problem}", "warn")]
    rows: list[tuple[str, str, str]] = []
    toc = v.rip.toc
    layout = f"{toc.audio_count} tracks, " + (toc.source if not toc.certain else f"from {toc.source}")
    if toc.has_data:
        layout += " (an enhanced CD: music plus a data track)"
    if not toc.certain:
        rows.append(("Layout", f"{layout} — less certain: a hidden gap before track 1 or a data track "
                     "would change the disc's ID, and then it may not be found", "warn"))
    else:
        rows.append(("Layout", layout, "plain"))
    for note in v.rip.notes:
        rows.append(("", f"Note: {note}", "plain"))
    tracks = v.tracks
    count = len(tracks)
    # AccurateRip
    if v.ar_status == "found":
        matched = [t for t in tracks if t.ar_confidence]
        missed = [t.number for t in tracks if not t.ar_confidence]
        version = "v2" if any(t.ar_version == 2 for t in matched) else "v1"
        if not missed:
            low = min(t.ar_confidence for t in matched)
            rows.append(("AccurateRip", f"✓ Accurately ripped: all {count} tracks match (confidence "
                         f"{low}{'+' if any(t.ar_confidence > low for t in matched) else ''}, {version} checksum)",
                         "good"))
        elif matched:
            rows.append(("AccurateRip", f"⚠ {len(matched)} of {count} tracks match; no match for track"
                         f"{'s' if len(missed) > 1 else ''} {_list(missed)}", "warn"))
        else:
            rows.append(("AccurateRip", f"✗ No track matches the {v.ar_pressings} entr"
                         f"{'ies' if v.ar_pressings != 1 else 'y'} on file: a different pressing, a drive "
                         "offset that wasn't corrected, or a damaged rip", "warn"))
    elif v.ar_status == "not found":
        rows.append(("AccurateRip", "Not in the AccurateRip database (nothing to compare with)", "plain"))
    elif v.ar_status == "error":
        rows.append(("AccurateRip", f"Couldn't check: {v.ar_error}", "warn"))
    # CUETools DB
    if v.ctdb_status == "found":
        missed = [t.number for t in tracks if not t.ctdb_confidence]
        if v.ctdb_disc:
            sure = v.ctdb_disc >= 2
            rows.append(("CUETools DB", f"{'✓' if sure else '⚠'} Accurately ripped: the whole disc matches "
                         f"(confidence {v.ctdb_disc} of {v.ctdb_total})"
                         + ("" if sure else " — one other rip agrees; 2 or more is the database's "
                            "\"probably undamaged\""), "good" if sure else "warn"))
        elif not missed:
            low = min(t.ctdb_confidence for t in tracks)
            rows.append(("CUETools DB", f"✓ Every track matches (confidence {low}+ of {v.ctdb_total}), "
                         "though not as one whole disc", "good"))
        elif len(missed) < count:
            rows.append(("CUETools DB", f"⚠ {count - len(missed)} of {count} tracks match "
                         f"(of {v.ctdb_total}); no match for track{'s' if len(missed) > 1 else ''} "
                         f"{_list(missed)}", "warn"))
        else:
            rows.append(("CUETools DB", f"✗ No match among {v.ctdb_total} rips on file: a different "
                         "pressing, an uncorrected drive offset, or a damaged rip", "warn"))
    elif v.ctdb_status == "not found":
        rows.append(("CUETools DB", "Not in the CUETools database (nothing to compare with)", "plain"))
    elif v.ctdb_status == "error":
        rows.append(("CUETools DB", f"Couldn't check: {v.ctdb_error}", "warn"))
    if not toc.certain and "not found" in (v.ar_status, v.ctdb_status):
        rows.append(("", "Note: the layout was rebuilt from the files' lengths. If it isn't the disc's "
                     "real one (a hidden gap before track 1, a data track, or files from more than one "
                     "disc), the disc is looked up under the wrong ID and isn't found", "plain"))
    # Per track, only when something didn't match everywhere it could
    found_ar, found_ct = v.ar_status == "found", v.ctdb_status == "found"
    if (found_ar and any(not t.ar_confidence for t in tracks)) or (found_ct and any(not t.ctdb_confidence for t in tracks)):
        parts = []
        for t in tracks:
            bits = []
            if found_ar:
                bits.append(f"AR {t.ar_confidence}" if t.ar_confidence else "AR ✗")
            if found_ct:
                bits.append(f"CTDB {t.ctdb_confidence}" if t.ctdb_confidence else "CTDB ✗")
            parts.append(f"{t.number}: {' '.join(bits)}")
        rows.append(("Per track", " · ".join(parts), "plain"))
    rows.append(("Disc ID", f"AccurateRip {toc.accuraterip_id()} · CTDB {toc.ctdb_tocid()}", "plain"))
    rows.append(("", "Compared at this rip's own offset; only the disc's IDs and track layout were sent "
                 "(to the CUETools DB unencrypted, as CUETools itself sends them)", "muted"))
    return rows


# -- the rip log's own checksums: an offline check --------------------------------------
# What EAC and XLD write per track, and what each figure is, measured 07/10/2026 against the
# logs and files of real rips (EAC 0.99 pb4/pb5, 1.1, 1.6, 1.8; XLD 20250302): every value below
# was recomputed from the files and matched.
#   EAC "Test CRC" / "Copy CRC" (localised: the line holding "CRC" and the 8-digit value, test
#     first): CRC-32 of the track's 16-bit little-endian interleaved samples, as written to its
#     file (with "Null samples used in CRC calculations: Yes"). A range (one-piece image) rip has
#     one Test / Copy CRC for the whole range: the files end to end.
#   EAC "Accurately ripped (confidence N)  [XXXXXXXX]  (AR v2)" and "Cannot be verified as
#     accurate … [XXXXXXXX], AccurateRip returned [YYYYYYYY]  (AR v2)": the first bracketed value
#     is the rip's own AccurateRip checksum, of the version named; EAC 0.99 names none, and its
#     value is the v1 checksum.
#   XLD "CRC32 hash (test run)" / "CRC32 hash": as EAC's Test / Copy CRC; "CRC32 hash (skip
#     zero)": the same with every 16-bit sample that is 0 left out (not whole stereo frames);
#     "AccurateRip v1 signature" / "AccurateRip v2 signature": the AccurateRip checksums.
# EAC with "Null samples used in CRC calculations: No" (none of the logs measured had it) is
# taken to leave out the same samples as XLD's skip zero.

_HEX = re.compile(r"\b([0-9A-Fa-f]{8})\s*$")
_BRACKET = re.compile(r"\[([0-9A-Fa-f]{8})\]")
_TRACK_HEAD = re.compile(r"^(\S+)\s+(\d{1,2})\s*$")      # "Track  1", "Track 01" (a word in any language)
_XLD_FIELD = re.compile(r"^\s+(.+?)\s*:\s*([0-9A-Fa-f]{8})\s*$")
_NULL_SAMPLES = re.compile(r"Null samples used in CRC calculations\s*:\s*(\w+)", re.IGNORECASE)


@dataclass
class LogTrack:
    number: int
    copy_crc: int | None = None
    test_crc: int | None = None
    skip_zero_crc: int | None = None    # XLD's "CRC32 hash (skip zero)"
    ar_v1: int | None = None
    ar_v2: int | None = None


@dataclass
class RipLog:
    ripper: str                              # "EAC" or "XLD"
    tracks: dict[int, LogTrack] = field(default_factory=dict)
    null_samples: bool | None = None         # EAC's "Null samples used in CRC calculations" (None: not said)
    range_copy_crc: int | None = None        # EAC range rip: one CRC for the whole disc
    range_test_crc: int | None = None
    tocs: list[Toc] = field(default_factory=list)

    @property
    def has_crcs(self) -> bool:
        return self.range_copy_crc is not None or any(
            t.copy_crc is not None or t.skip_zero_crc is not None or t.ar_v1 is not None or t.ar_v2 is not None
            for t in self.tracks.values())


def _ripper(text: str) -> str:
    head = text.lstrip("﻿ \r\n")[:200]
    if head.startswith("Exact Audio Copy") or "EAC extraction logfile" in head:
        return "EAC"
    if head.startswith("X Lossless Decoder"):
        return "XLD"
    return ""


def parse_log(text: str, source: str = "the rip log") -> RipLog | None:
    """The checksums an EAC or XLD log holds (see above), or None when it isn't one of their logs.
    EAC writes its logs in the user's language: tracks are found by layout (a word and a number
    at the start of a line), CRC lines by "CRC" and their 8-digit value, AccurateRip lines by the
    bracketed value."""
    ripper = _ripper(text)
    if not ripper:
        return None
    log = RipLog(ripper, tocs=tocs_from_log(text, source))
    current: LogTrack | None = None
    crc_lines: dict[int | None, list[tuple[str, int]]] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        setting = _NULL_SAMPLES.search(line) if ripper == "EAC" else None
        if setting:   # (among the settings at the top, written from the start of the line)
            log.null_samples = {"yes": True, "no": False}.get(setting.group(1).lower())
            continue
        if not line[0].isspace():   # a heading: a track's section, or something else
            found = _TRACK_HEAD.match(line)
            current = log.tracks.setdefault(int(found.group(2)), LogTrack(int(found.group(2)))) if found else None
            continue
        if ripper == "EAC":
            if "\\" in line or "/" in line:
                continue   # a file name (which could hold anything), never a checksum line
            bracket = _BRACKET.search(line)
            if bracket and current is not None:
                value = int(bracket.group(1), 16)
                if "(AR v2)" in line:
                    current.ar_v2 = value
                else:
                    current.ar_v1 = value   # "(AR v1)", or EAC 0.99's (which names no version)
                continue
            value = _HEX.search(line)
            if value and "CRC" in line:
                lowered = line.lower()
                kind = "test" if "test" in lowered else "copy" if "copy" in lowered else ""
                crc_lines.setdefault(current.number if current else None, []).append((kind, int(value.group(1), 16)))
        elif current is not None:
            found = _XLD_FIELD.match(line)
            if not found:
                continue
            name, value = found.group(1).lower(), int(found.group(2), 16)
            if name == "crc32 hash":
                current.copy_crc = value
            elif name == "crc32 hash (test run)":
                current.test_crc = value
            elif name == "crc32 hash (skip zero)":
                current.skip_zero_crc = value
            elif name == "accuraterip v1 signature":
                current.ar_v1 = value
            elif name == "accuraterip v2 signature":
                current.ar_v2 = value
    for number, found in crc_lines.items():   # EAC: named in English; else test first, then copy
        test = next((v for kind, v in found if kind == "test"), None)
        copy = next((v for kind, v in found if kind == "copy"), None)
        if test is None and copy is None:
            test, copy = (found[0][1], found[-1][1]) if len(found) > 1 else (None, found[-1][1])
        if number is None:
            log.range_test_crc, log.range_copy_crc = test, copy
        else:
            log.tracks[number].test_crc, log.tracks[number].copy_crc = test, copy
    return log


@dataclass
class LogTrackCheck:
    number: int
    matched: list[str] = field(default_factory=list)    # the log's figures that agree ("Copy CRC", …)
    differed: list[str] = field(default_factory=list)   # …and the ones that don't

    @property
    def verdict(self) -> str:
        return "differs" if self.differed else "matches" if self.matched else "absent"


@dataclass
class LogCheck:
    problem: str = ""                         # set when there was nothing to check against
    rip: Rip | None = None
    log: RipLog | None = None
    log_name: str = ""
    tracks: list[LogTrackCheck] = field(default_factory=list)
    whole: str = ""                           # a range rip's one CRC: "matches" / "differs"
    notes: list[str] = field(default_factory=list)


def _same_layout(a: Toc, b: Toc) -> bool:
    return a.audio_starts == b.audio_starts and a.audio_end == b.audio_end


def _logs_for(rip: Rip, folder: Path) -> tuple[RipLog, str] | str:
    """The rip log beside the disc that belongs to it (its track layout is the disc's), with its
    name; or why there's none to check against."""
    found: list[tuple[RipLog, str]] = []
    for path in _side_files(folder, ".log"):
        try:
            if path.stat().st_size > LOG_MAX:
                continue
            with path.open("rb") as file:
                text = cuesheet.decode(file.read(LOG_READ))
        except OSError:
            continue
        log = parse_log(text, source=f"the rip log ({_where(path, folder)})")
        if log is not None and log.has_crcs:
            found.append((log, _where(path, folder)))
    if not found:
        return "there's no EAC or XLD log with checksums beside this album"
    for log, name in found:
        if any(_same_layout(toc, rip.toc) for toc in log.tocs):
            return log, name
    count = rip.toc.audio_count
    untabled = [(log, name) for log, name in found if not log.tocs and max(log.tracks, default=0) == count]
    if len(untabled) == 1:
        return untabled[0]
    return "the rip log here is for a different track layout (another disc, or other files)"


def compare_log(sums: Checksums, rip: Rip, log: RipLog) -> tuple[list[LogTrackCheck], str, list[str]]:
    """Each track's audio against what the log says was ripped: (per track, the whole disc's
    verdict for a range rip ("" without one), notes)."""
    count = rip.toc.audio_count
    per_file = len(rip.files) == count   # one file per track: each file is what the ripper wrote
    plain = sums.crc_files if per_file else sums.crc_tracks
    nonzero = sums.crc_files_nz if per_file else sums.crc_tracks_nz
    notes: list[str] = []
    checks = []
    for k in range(count):
        check = LogTrackCheck(k + 1)
        entry = log.tracks.get(k + 1)
        if entry is not None:
            figures = []
            if entry.copy_crc is not None:
                if log.ripper == "XLD":
                    figures.append(("CRC32", entry.copy_crc == plain[k]))
                else:   # EAC: silent samples counted or not, as the log says (both when it doesn't)
                    ours = {True: (plain[k],), False: (nonzero[k],)}.get(log.null_samples, (plain[k], nonzero[k]))
                    figures.append(("Copy CRC", entry.copy_crc in ours))
            if entry.skip_zero_crc is not None:
                figures.append(("CRC32 skip zero", entry.skip_zero_crc == nonzero[k]))
            if entry.ar_v2 is not None:
                figures.append(("AccurateRip v2", entry.ar_v2 == sums.ar_v2[k]))
            if entry.ar_v1 is not None:
                figures.append(("AccurateRip v1", entry.ar_v1 == sums.ar_v1[k]))
            for name, agrees in figures:
                (check.matched if agrees else check.differed).append(name)
            if entry.test_crc is not None and entry.copy_crc is not None and entry.test_crc != entry.copy_crc:
                notes.append(f"the log's test and copy CRCs of track {k + 1} differ: the ripper itself read "
                             "that track two ways")
        checks.append(check)
    whole = ""
    if log.range_copy_crc is not None:
        ours = {True: (sums.crc_disc,), False: (sums.crc_disc_nz,)}.get(
            log.null_samples, (sums.crc_disc, sums.crc_disc_nz))
        whole = "matches" if log.range_copy_crc in ours else "differs"
        if log.range_test_crc is not None and log.range_test_crc != log.range_copy_crc:
            notes.append("the log's test and copy CRCs differ: the ripper itself read the disc two ways")
    if log.ripper == "EAC" and log.null_samples is None and any(t.matched or t.differed for t in checks):
        notes.append("the log doesn't say (in English) whether silent samples count in its CRCs: both "
                     "ways were tried")
    return checks, whole, notes


def check_log(track: Path, cancel: threading.Event | None = None, progress=lambda p: None) -> LogCheck | None:
    """The offline check: this disc's audio against the CRCs its rip log holds. Needs no network.
    None if cancelled; never raises for a disc that can't be checked (`problem` says why)."""
    try:
        rip = find_rip(track)
        chosen = _logs_for(rip, audiofile.real_path(track).parent)
        if isinstance(chosen, str):
            return LogCheck(problem=chosen, rip=rip)
        log, name = chosen
        sums = checksums(rip, cancel, progress, log_crcs=True)
    except NotARip as error:
        return LogCheck(problem=str(error))
    except (sf.LibsndfileError, OSError, RuntimeError) as error:
        return LogCheck(problem=f"couldn't read the files ({error})")
    if sums is None:
        return None
    tracks, whole, notes = compare_log(sums, rip, log)
    return LogCheck(rip=rip, log=log, log_name=name, tracks=tracks, whole=whole, notes=notes)


def describe_log(c: LogCheck) -> list[tuple[str, str, str]]:
    """(label, text, tone) rows for the Check section: a summary, then each track."""
    if c.problem:
        return [("Rip log", f"Can't check against the rip log: {c.problem}", "warn")]
    rows: list[tuple[str, str, str]] = []
    log_words = f"the {c.log.ripper} log ({c.log_name})"
    if c.whole == "matches":
        rows.append(("Rip log", f"✓ The whole disc matches the CRC in {log_words}", "good"))
    elif c.whole == "differs":
        rows.append(("Rip log", f"✗ The disc differs from the CRC in {log_words}: these files aren't what "
                     "was ripped (changed since, or another rip)", "warn"))
    count = len(c.tracks)
    differs = [t.number for t in c.tracks if t.verdict == "differs"]
    absent = [t.number for t in c.tracks if t.verdict == "absent"]
    if len(absent) < count:
        if differs:
            which = f"Track {differs[0]} differs" if len(differs) == 1 else f"Tracks {_list(differs)} differ"
            rows.append(("Rip log", f"✗ {which} from what was ripped ({log_words})", "warn"))
        elif absent:
            rows.append(("Rip log", f"✓ {count - len(absent)} of {count} tracks match {log_words}; "
                         f"not in the log: {_list(absent)}", "good"))
        else:
            rows.append(("Rip log", f"✓ All {count} tracks match {log_words}", "good"))
        for t in c.tracks:
            if t.verdict == "matches":
                rows.append((f"Track {t.number}", f"matches the rip log ✓ ({', '.join(t.matched)})", "good"))
            elif t.verdict == "differs":
                agrees = f"; agrees: {', '.join(t.matched)}" if t.matched else ""
                rows.append((f"Track {t.number}", f"differs from the rip log ✗ ({', '.join(t.differed)}{agrees})",
                             "warn"))
            else:
                rows.append((f"Track {t.number}", "not in the log", "muted"))
    elif not c.whole:
        rows.append(("Rip log", f"{log_words} holds no checksums for these tracks", "plain"))
    for note in c.notes:
        rows.append(("", f"Note: {note}", "plain"))
    rows.append(("", "Checked on this computer against the checksums the ripper wrote; nothing was sent "
                 "anywhere", "muted"))
    return rows
