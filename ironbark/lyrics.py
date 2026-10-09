"""Lyrics from the song's own file and the files beside it — never from the internet. No UI.

Where they're looked for, in order:
1. A same-name .lrc beside the song ("Song.flac" → "Song.lrc"): synced lyrics. The LRC format
   (an open text format; checked 07/10/2026): "[mm:ss.xx]words" lines, xx hundredths (two or
   three digits, or none, are read); a line may carry several time tags for words sung more than
   once ("[00:21.10][00:45.10]words"); "[offset:+/-ms]" moves every line, "+" making lyrics
   appear sooner; other "[name:value]" lines are details about the file (title, artist…).
   Enhanced LRC's word stamps ("<mm:ss.xx>") are taken out of the words.
2. The file's tags: ID3 SYLT (synchronised: each piece of text with its time stamp, in
   milliseconds or MPEG frames), then ID3 USLT, Vorbis LYRICS / UNSYNCEDLYRICS, MP4 ©lyr (plain
   text; a tag holding LRC-style lines is read as synced).
3. A same-name .txt beside the song: plain text.
A track of a CUE image has no file of its own: nothing beside it or in the image's tags is about
that one track, so it's only said that nothing was found.

Read on a worker (a slow drive mustn't stall the window); the window asks `line_at` as the song
plays.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import audiofile, cuesheet, tagio
from .tagio import id3

MAX_BYTES = 1 << 20          # a lyrics file bigger than this isn't one
MPEG_FRAME_SAMPLES = 1152    # an MPEG-1 Layer III frame (SYLT stamps counted in frames)
_STAMP = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_WORD_STAMP = re.compile(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>")
_FIELD = re.compile(r"^\s*\[([A-Za-z#][A-Za-z ]*):(.*)\]\s*$")
TEXT_TAGS = ("LYRICS", "UNSYNCEDLYRICS", "©LYR")   # tagio.text_values names (Vorbis, MP4)


@dataclass
class Line:
    time: float | None    # seconds into the song (None: plain lyrics)
    text: str


@dataclass
class Lyrics:
    lines: list[Line]
    synced: bool
    source: str           # where they came from, in words ("Song.lrc beside the song")
    _times: list[float] = field(default_factory=list, repr=False)

    def __post_init__(self):
        if self.synced:
            self.lines.sort(key=lambda line: line.time)
            self._times = [line.time for line in self.lines]

    def line_at(self, seconds: float) -> int:
        """The line being sung at `seconds` (the last one whose time has come); −1 before the
        first, and always −1 for plain lyrics."""
        if not self.synced:
            return -1
        return bisect.bisect_right(self._times, seconds + 1e-9) - 1


def _stamp_seconds(minutes: str, seconds: str, fraction: str | None) -> float:
    value = int(minutes) * 60 + int(seconds)
    if fraction:
        value += int(fraction) / 10 ** len(fraction)
    return value


def parse_lrc(text: str, source: str = "") -> Lyrics | None:
    """Synced lyrics from LRC text; None when it has no time-stamped line. Lines without a time
    stamp (and bad ones) are left out; ID lines such as [ar:…] are skipped."""
    offset = 0.0
    lines: list[Line] = []
    for raw in text.splitlines():
        stamps = []
        rest = raw.strip()
        while True:
            found = _STAMP.match(rest)
            if not found:
                break
            stamps.append(_stamp_seconds(*found.groups()))
            rest = rest[found.end():].lstrip()
        if stamps:
            words = _WORD_STAMP.sub("", rest).strip()
            words = re.sub(r"\s{2,}", " ", words)
            lines.extend(Line(time, words) for time in stamps)
            continue
        field_line = _FIELD.match(raw)
        if field_line and field_line.group(1).strip().lower() == "offset":
            try:
                offset = int(field_line.group(2).strip()) / 1000
            except ValueError:
                pass
    if not lines:
        return None
    for line in lines:   # "+" = sooner: every line comes that much earlier
        line.time = max(0.0, line.time - offset)
    return Lyrics(lines, True, source)


def plain(text: str, source: str = "") -> Lyrics | None:
    """Plain lyrics: one line per line of text (blank ones kept, as verse breaks)."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n").split("\n")
    if not any(line.strip() for line in lines):
        return None
    return Lyrics([Line(None, line.rstrip()) for line in lines], False, source)


def from_text(text: str, source: str = "") -> Lyrics | None:
    """A tag's or a file's text: LRC lines are synced, anything else plain."""
    return parse_lrc(text, source) or plain(text, source)


def from_sylt(frame: id3.SyncedLyricsFrame, samplerate: int, source: str = "") -> Lyrics | None:
    """Synced lyrics from an ID3 SYLT frame. Entries are often syllables: a new line begins with
    one whose text starts with a newline (then the line's time is its first syllable's). When no
    entry carries a newline, each entry is a line of its own."""
    if not frame.entries:
        return None
    if frame.stamp_format == 1:   # MPEG frames
        seconds = lambda stamp: stamp * MPEG_FRAME_SAMPLES / max(1, samplerate)
    else:                         # milliseconds
        seconds = lambda stamp: stamp / 1000
    joined = any("\n" in text or "\r" in text for text, _ in frame.entries)
    lines: list[Line] = []
    break_next = True   # (also: a piece ending in a newline puts the next one on a new line)
    for text, stamp in frame.entries:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not joined or break_next or text.startswith("\n"):
            lines.append(Line(seconds(stamp), text.strip("\n")))
        else:
            lines[-1].text += text.rstrip("\n")
        break_next = text.endswith("\n")
    return Lyrics(lines, True, source)


def _read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_BYTES:
            return None
        return cuesheet.decode(path.read_bytes())
    except OSError:
        return None


def embedded(path) -> Lyrics | None:
    """Lyrics in the file's own tags (synced preferred), or None."""
    audio = tagio.open_any(audiofile.real_path(path))
    if audio is None:
        return None
    tags = getattr(audio, "tags", None)
    if isinstance(tags, id3.ID3Tag):
        frames = [f for f in tags.getall("SYLT") if isinstance(f, id3.SyncedLyricsFrame)]
        frames.sort(key=lambda f: f.content_type != 1)   # lyrics before other synced text
        rate = _sample_rate(audio)
        for frame in frames:
            found = from_sylt(frame, rate, "the file's tags (ID3 SYLT)")
            if found is not None:
                return found
        for frame in tags.getall("USLT"):
            text = "\n".join(getattr(frame, "text", []))
            found = from_text(text, "the file's tags (ID3 USLT)")
            if found is not None:
                return found
        return None
    values = tagio.text_values(audio)
    for name in TEXT_TAGS:
        for text in values.get(name, []):
            found = from_text(text, f"the file's tags ({name.replace('©LYR', 'MP4 ©lyr')})")
            if found is not None:
                return found
    return None


def _sample_rate(audio) -> int:
    info = getattr(audio, "info", None)
    return int(getattr(info, "sample_rate", 0) or 44100)


def beside(path) -> tuple[Path, Path]:
    """The .lrc and .txt files that would hold this song's lyrics."""
    real = Path(audiofile.real_path(path))
    return real.with_suffix(".lrc"), real.with_suffix(".txt")


def where_looked(path) -> str:
    """Where `find` looks, in words (for "No lyrics found")."""
    if audiofile.cue_of(path) is not None:
        return ("This is one track of a CUE image: lyrics are read from a song's own file, its tags "
                "and the .lrc / .txt beside it, and this track has no file of its own.")
    lrc, txt = beside(path)
    return (f"Looked in {lrc.name} and {txt.name} beside it, and in its tags (ID3 SYLT / USLT, "
            "Vorbis LYRICS / UNSYNCEDLYRICS, MP4 ©lyr).")


def find(path) -> Lyrics | None:
    """The song's lyrics, from where the module notes say, or None."""
    if audiofile.cue_of(path) is not None:
        return None
    lrc, txt = beside(path)
    text = _read_text(lrc) if lrc.is_file() else None
    if text is not None:
        found = from_text(text, f"{lrc.name} beside the song")
        if found is not None:
            return found
    found = embedded(path)
    if found is not None:
        return found
    text = _read_text(txt) if txt.is_file() else None
    return plain(text, f"{txt.name} beside the song") if text is not None else None
