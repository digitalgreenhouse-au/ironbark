"""Volume levelling (no UI): play every track, or every album, at about the same loudness.

Off by default: any change of level means the device no longer gets the file's own samples, so a
levelled track is never bit-perfect, and the window says so (an amber square, "Volume levelling
−4.3 dB (album)" in the Output and Proof lines).

Where the gain comes from, in order:

1. ReplayGain tags written by a tagger: REPLAYGAIN_TRACK_GAIN / REPLAYGAIN_ALBUM_GAIN ("-6.20 dB")
   and REPLAYGAIN_TRACK_PEAK / REPLAYGAIN_ALBUM_PEAK (linear, 1.0 = full scale). Vorbis comments
   (FLAC, Ogg) by name, ID3 as TXXX frames of the same names, MP4 as iTunes free-form items
   (----:com.apple.iTunes:replaygain_track_gain); names in any case. ReplayGain 2.0's reference is
   −18 LUFS (the rsgain README, which implements the ReplayGain 2.0 specification; checked
   07/10/2026): the gain brings the track to −18 LUFS.
2. Opus: R128_TRACK_GAIN / R128_ALBUM_GAIN first (RFC 7845 §5.2.1, checked 07/10/2026): a Q7.8
   fixed-point number in dB (an integer, −573 = −2.24 dB) that normalises to EBU R 128's target,
   −23 LUFS (EBU R 128, tech.ebu.ch), applied "in addition to" the header's output gain. The
   decoder (libsndfile) already applies that output gain: measured 07/10/2026, a −6 dB output gain
   written into an OpusHead came out exactly −6.00 dB quieter. So only the R128 gain goes on top,
   plus 5 dB to move it from −23 to ReplayGain's −18 LUFS. (Some taggers write REPLAYGAIN_* tags
   into Opus files instead, with the output gain at 0; those are read as in 1.)
3. Otherwise Ironbark measures the track itself: its integrated loudness to ITU-R BS.1770-5 /
   EBU R 128 (loudness.py), worked out on a worker ahead of play like the clipping guard's true
   peak. An album's own figure is all its tracks measured as one programme (loudness.album_loudness).

The pre-amp (Settings, −6…+6 dB) is added to whichever gain is used. A gain above 0 dB (a quiet
track turned up) is allowed only as far as the track's peak allows: the true peak (measured at 8x)
or the peak in its tags, whichever is higher, stays at or below −0.1 dBTP. With no peak known, a
track is never turned up. The clipping guard still applies: a track it turns down gets the smaller
of the two gains (engine.Player.trim_for).

A track of a CUE image takes only the album gain from the image's tags (its track gain would be
the whole file's); its own loudness is measured from its stretch of the file.
"""

from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass

from . import audiofile, tagio
from .analysis import TruePeak
from .loudness import Loudness, integrated

REFERENCE_LUFS = -18.0        # ReplayGain 2.0's reference loudness
R128_REFERENCE_LUFS = -23.0   # EBU R 128's target: what Opus R128_* gains normalise to
PEAK_CEILING_DBTP = -0.1      # a turned-up track's peak stays at or below this
PREAMP_CHOICES = tuple(float(db) for db in range(-6, 7))   # Settings → Pre-amp, dB
MODES = (("off", "Off"), ("track", "Track"), ("album", "Album"), ("smart", "Smart"))
ALBUM_TABS = ("album", "folder", "library")   # tabs whose songs come album by album, in order
ALBUM_MOST_FILES = 200        # a folder of more songs than this isn't one album (a folder of singles):
                              # it isn't measured as one, and its songs get their track gains
MAX_GAIN_DB = 64.0            # a tag value beyond this either way isn't a real gain
MAX_PEAK = 16.0               # nor a peak above this (24 dB over full scale)

_NUMBER = re.compile(r"^\s*([+-]?)\s*(\d+(?:[.,]\d*)?|[.,]\d+)\s*(?:db)?\s*$", re.IGNORECASE)


def mode_label(mode: str) -> str:
    return dict(MODES).get(mode, "Off")


def preamp_label(db: float) -> str:
    return "0 dB" if not db else f"{db:+g} dB".replace("-", "−")


def parse_gain(text: str) -> float | None:
    """A ReplayGain gain ("-6.20 dB", "+1.5 dB", "−3,4 dB", "-6.2") in dB, or None."""
    if not isinstance(text, str):
        return None
    found = _NUMBER.match(text.replace("−", "-").replace(" ", " "))
    if not found:
        return None
    value = float(found.group(2).replace(",", "."))
    value = -value if found.group(1) == "-" else value
    return value if abs(value) <= MAX_GAIN_DB else None


def parse_peak(text: str) -> float | None:
    """A ReplayGain peak ("0.988312", linear, 1.0 = full scale), or None."""
    if not isinstance(text, str):
        return None
    try:
        value = float(text.strip().replace(",", "."))
    except ValueError:
        return None
    return value if 0.0 < value <= MAX_PEAK and math.isfinite(value) else None


def parse_r128(text: str) -> float | None:
    """An Opus R128_TRACK_GAIN / R128_ALBUM_GAIN (RFC 7845: a base-10 integer, Q7.8 dB) in dB."""
    if not isinstance(text, str) or not re.fullmatch(r"\s*[+-]?\d+\s*", text):
        return None
    value = int(text)
    return value / 256 if -32768 <= value <= 32767 else None


@dataclass(frozen=True)
class GainTags:
    """What a file's tags say (dB to bring it to −18 LUFS; peaks linear). None = not tagged."""
    track_gain: float | None = None
    album_gain: float | None = None
    track_peak: float | None = None
    album_peak: float | None = None
    source: str = "ReplayGain tags"


def _first(values: dict[str, list[str]], name: str) -> str | None:
    found = values.get(name)
    return found[0] if found else None


def tags_from_values(values: dict[str, list[str]], opus: bool = False, whole_file: bool = True) -> GainTags | None:
    """The gains in a file's text tags ({UPPER NAME: [values]}, as tagio.text_values gives them).
    `opus`: R128_* tags come first. `whole_file`: False for one track of a CUE image, whose
    track gain and peak would describe the whole file — only its album figures are used."""
    values = {name.upper(): v for name, v in values.items()}
    if opus:
        track = parse_r128(_first(values, "R128_TRACK_GAIN") or "")
        album = parse_r128(_first(values, "R128_ALBUM_GAIN") or "")
        shift = REFERENCE_LUFS - R128_REFERENCE_LUFS
        if track is not None or album is not None:
            return GainTags(track_gain=track + shift if track is not None and whole_file else None,
                            album_gain=album + shift if album is not None else None, source="Opus R128 tags")
    track = parse_gain(_first(values, "REPLAYGAIN_TRACK_GAIN") or "")
    album = parse_gain(_first(values, "REPLAYGAIN_ALBUM_GAIN") or "")
    if not whole_file:
        track = None
    if track is None and album is None:
        return None
    return GainTags(track_gain=track, album_gain=album,
                    track_peak=parse_peak(_first(values, "REPLAYGAIN_TRACK_PEAK") or "") if whole_file else None,
                    album_peak=parse_peak(_first(values, "REPLAYGAIN_ALBUM_PEAK") or ""))


def read_tags(path) -> GainTags | None:
    """The gains tagged in a music file (see the module notes), or None."""
    audio = tagio.open_any(audiofile.real_path(path))
    if audio is None:
        return None
    return tags_from_values(tagio.text_values(audio), opus=isinstance(audio, tagio.OggOpusFile),
                            whole_file=audiofile.cue_of(path) is None)


def measure(path, peak: bool = True, loudness: bool = True,
            cancel: threading.Event | None = None) -> tuple[float | None, float | None] | None:
    """(true peak, integrated LUFS) of a file in one read; either is None when not asked for (or,
    for loudness, when the file is silent). None if cancelled. (A song on a network drive: its
    complete copy, waited for until `cancel` is set: audiofile.NotCached.)"""
    with audiofile.open(path, cancel=cancel.is_set if cancel is not None else None) as f:
        meter = TruePeak(f.channels) if peak else None
        levels = Loudness(f.samplerate, f.channels) if loudness else None
        for block in audiofile.blocks(f, f.samplerate * 5, dtype="float32", always_2d=True):
            if cancel is not None and cancel.is_set():
                return None
            if meter is not None:
                meter.add(block)
            if levels is not None:
                levels.add(block)
    if meter is not None:
        meter.finish()
    return (meter.peak if meter is not None else None,
            integrated(levels.powers) if levels is not None else None)


@dataclass(frozen=True)
class Levelling:
    """The levelling gain worked out for one file."""
    gain: float           # linear; above 1.0 only when the peak leaves room
    mode: str             # "track" or "album": which gain it is
    source: str           # "ReplayGain tags", "Opus R128 tags" or "measured"
    limited: bool = False  # held down so the peak stays under the ceiling

    @property
    def db(self) -> float:
        return 20 * math.log10(self.gain) if self.gain > 0 else float("-inf")


def mode_for(setting: str, tab_kind: str | None, shuffle: bool, queued: bool) -> str:
    """Which gain a song gets: Track and Album as chosen; Smart = album gain while an album plays
    in order (a tab of whole albums, shuffle off, not a song from Up next), else track. "off"
    stays off."""
    if setting == "smart":
        return "album" if tab_kind in ALBUM_TABS and not shuffle and not queued else "track"
    return setting if setting in ("track", "album") else "off"


def level_for(mode: str, tags: GainTags | None, track_lufs: float | None, album_lufs: float | None,
              peak: float | None, preamp_db: float = 0.0) -> Levelling | None:
    """The gain for a file, or None while it isn't known yet (not measured). `mode` "track" or
    "album" (an album with no album figure yet uses its track's). `peak`: its measured true peak
    (linear), if known; the tags' peaks count too."""
    if mode not in ("track", "album"):
        return None
    gain_db, used, source = None, mode, ""
    if mode == "album":
        if tags is not None and tags.album_gain is not None:
            gain_db, source = tags.album_gain, tags.source
        elif album_lufs is not None:
            gain_db, source = REFERENCE_LUFS - album_lufs, "measured"
        else:
            used = "track"
    if gain_db is None:
        if tags is not None and tags.track_gain is not None:
            gain_db, source = tags.track_gain, tags.source
        elif track_lufs is not None:
            gain_db, source = REFERENCE_LUFS - track_lufs, "measured"
        else:
            return None
    return held(Levelling(10 ** ((gain_db + preamp_db) / 20), used, source), tags, peak)


def held(level: Levelling, tags: GainTags | None, peak: float | None) -> Levelling:
    """`level`, held down if need be so the peak lands at or below PEAK_CEILING_DBTP: the measured
    true peak (`peak`, linear) or the tags' peaks, whichever is higher. No peak known: never up."""
    peaks = [p for p in (peak, tags.track_peak if tags else None,
                         tags.album_peak if tags is not None and level.mode == "album" else None) if p]
    ceiling = 10 ** (PEAK_CEILING_DBTP / 20) / max(peaks) if peaks else 1.0
    if level.gain > ceiling:
        return Levelling(ceiling, level.mode, level.source, limited=True)
    return level


def describe(level) -> str:
    """"Volume levelling −4.3 dB (album)" — for the Output and Proof lines."""
    if level is None:
        return ""
    held = ", held below its peak" if level.limited else ""
    return f"Volume levelling {level.db:+.1f} dB ({level.mode}{held})".replace("-", "−")
