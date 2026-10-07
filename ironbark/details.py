"""The audiophile bits: the output-path line and the live Now Playing window.

(0.22.0) The window has the main window's finish: Settings' section headings, flat pill buttons,
one label column lined up across every section, green / amber values as the main window colours
them (a ✓ line green, ✗ / ⚠ amber), live numbers in fixed-width digits (theme.numbers), and it
scrolls (wheel, slim scrollbar) when it's taller than the screen instead of running off it."""

from __future__ import annotations

import logging
import math
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from . import controls, loopback, placement, ripcheck, theme, toolwin
from .analysis import (CLIP_RUN, LOSSY_BELOW_HZ, UPSAMPLED_BELOW_HZ, AlbumResult, Analysis, album_measure,
                       analyse)
from .engine import Health, OutputInfo, PlaybackEvent, Player, State
from .proof import Proof, compare_paths, describe_path
from .flacframes import FrameIndex, build_index
from .formats import FormatFlags
from .mp3frames import build_index as build_mp3_index
from .library import LOSSY_CODECS, Track, format_rate
from .meters import album_files

log = logging.getLogger(__name__)

MIXER = "Windows" if sys.platform == "win32" else "system"

METER_FLOOR_DB = -60.0
METER_FALL_DB = 2.0  # how far the bars fall back per refresh, so they're readable
REFRESH_MS = 100
WINDOWS_CHECK_SECONDS = 3.0
TONE_STYLE = {"good": "GoodValue.TLabel", "warn": "WarnValue.TLabel", "plain": "TLabel", "muted": "Muted.TLabel"}
# (0.22.0) The same tones for a live number (fixed-width digits: theme.numbers).
NUMBER_STYLE = {"good": "GoodNumber.TLabel", "warn": "WarnNumber.TLabel", "plain": "Number.TLabel"}
NUMBER_ROWS = ("position", "bitrate", "peak", "volume", "buffer")   # Live rows that are mostly numbers
VALUE_WRAP = 360        # px at 100%: where a value's text wraps
LABEL_MIN = 96          # px at 100%: the label column is never narrower (it grows to its longest label)
FIT_MARGIN = 48         # px at 100%: kept free below the window when it's as tall as the screen allows
NETWORK_HINT = ("Turn on Settings → Network → Allow network access first. Only the disc's IDs and "
                "track layout are sent.")


def output_line(output: OutputInfo | None, gain: float, float_source: bool = False,
                lossy: bool = False, trim: float = 1.0) -> tuple[str, str]:
    """(text, tone) for the line under the format. tone is "good", "warn" or "plain".
    float_source: the file holds floating-point samples (some WAVs), which the device
    can only take after conversion to whole numbers — so never bit-perfect.
    lossy: an MP3 — delivery can be exact, but only of what the decoder rebuilt."""
    if output is None:
        return "", "plain"
    guard = (f" · clipping guard {20 * math.log10(trim):.1f} dB on this loud recording (it would clip "
             f"the converter)") if trim < 1.0 else ""
    device = output.device
    if output.exclusive:
        base = f"→ {device} · {format_rate(output.rate)} exclusive"
        if gain < 1.0:
            return f"{base} · volume below 100%, so not bit-perfect", "warn"
        if float_source:
            return f"{base} · floating-point file converted for the device, so not bit-perfect", "warn"
        if lossy:
            return f"{base} · decoded MP3 sent untouched, but the source is lossy{guard}", "plain"
        if guard:
            # (0.24.0) The clipping guard wins over Bit-perfect (crackle-free first), and says so.
            return f"{base}{guard}, so this track isn't bit-perfect (the guard comes first)", "good"
        if output.hardware_volume is False:
            return f"{base} · bit-perfect ✓ · no device volume: use the player or headphone volume", "warn"
        if output.hardware_volume:
            return f"{base} · bit-perfect ✓ · volume by device ✓", "good"
        return f"{base} · bit-perfect ✓", "good"
    if output.resampled:
        return (f"→ {device} · resampled to {format_rate(output.mixer_rate)} by the {MIXER} mixer ⚠{guard}",
                "warn")
    return f"→ {device} · {format_rate(output.rate)} · shared ({MIXER} mixer){guard}", "plain"


def fmt_size(size: int) -> str:
    return f"{size / 1_048_576:.1f} MB" if size else "—"


def fmt_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def fmt_clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def to_db(level: float) -> float:
    return 20 * math.log10(level) if level > 0 else float("-inf")


def tone_of(text: str) -> str:
    """(0.22.0) A Check / File line's colour, as the main window colours its status: a line that
    starts ✓ is good news (green), ✗ or ⚠ something to look at (amber), anything else plain."""
    if text.startswith("✓"):
        return "good"
    if text.startswith(("✗", "⚠")):
        return "warn"
    return "plain"


def describe_proof(proof: Proof | None, output: OutputInfo | None, gain: float, trim: float,
                   lossy: bool = False, float_source: bool = False) -> tuple[str, str]:
    """(text, tone) for what the player has sent, as checked against the file."""
    if proof is None or proof.sent <= 0:
        return "Waiting for music…", "plain"
    if proof.mismatch_at is not None:
        return (f"✗ At {fmt_clock(proof.mismatch_at)} the player sent different audio from the file. "
                "Please report this.", "warn")
    behind = proof.sent - proof.checked
    checked = (f"{fmt_clock(proof.checked)} checked" if behind < 2
               else f"checking… {fmt_clock(proof.checked)} of {fmt_clock(proof.sent)}")
    if not proof.untouched:
        why = ("the volume (below 100%)" if gain < 1.0
               else "the clipping guard (it comes before Bit-perfect: this track would clip the converter)"
               if trim < 1.0 else "the volume or clipping guard")
        where = f" from {fmt_clock(proof.changed_from)}" if proof.changed_from else ""
        return (f"Sent exactly as intended ({checked}), but changed by {why}{where}, so not bit-perfect",
                "warn")
    if output is not None and output.exclusive:
        if lossy:
            return f"✓ Sent exactly as decoded ({checked}), but an MP3 is lossy to begin with", "plain"
        if float_source:
            return f"Sent exactly as decoded ({checked}); a floating-point file can't be bit-perfect", "plain"
        return f"✓ Bit-perfect, proven: every sample sent is identical to the file ({checked})", "good"
    return (f"✓ The player sent every sample unchanged ({checked}). In shared mode {MIXER} mixes it next: "
            f"use “Check {MIXER}” below to see what that does", "plain")


def describe_health(health: Health | None, dropouts: int) -> tuple[str, str]:
    """(text, tone): how close this track came to a dropout."""
    if health is None or health.buffer_low is None:
        return "Measuring…", "plain"
    parts = [f"buffer never below {health.buffer_low:.1f} s"]
    if health.driver_low is not None:
        parts.append(f"the driver always had ≥ {health.driver_low * 1000:.0f} ms queued")
    if health.longest_wait:
        parts.append(f"longest wait between its requests {health.longest_wait * 1000:.0f} ms "
                     f"(normal {health.usual_wait * 1000:.0f})")
    parts.append(f"slowest file read {health.slowest_read * 1000:.0f} ms")
    detail = " · ".join(parts)
    if dropouts:
        return f"⚠ {dropouts} dropout{'' if dropouts == 1 else 's'} this track · {detail}", "warn"
    if health.near_misses:
        return (f"⚠ {health.near_misses} near miss{'' if health.near_misses == 1 else 'es'} (nothing heard yet) · "
                f"{detail}", "warn")
    return f"✓ Safe · {detail}", "good"


def describe_events(events: list[PlaybackEvent], limit: int = 3) -> tuple[str, str]:
    if not events:
        return "✓ No dropouts or near misses since the player started", "good"
    lines = []
    for e in events[-limit:]:
        kind = f"{e.kind} ×{e.count}" if e.count > 1 else e.kind
        silent = f", silent {e.silent:.1f} s in total" if e.silent >= 0.05 else ""
        lines.append(f"{time.strftime('%H:%M:%S', time.localtime(e.when))} · {kind} in {e.track} at "
                     f"{fmt_clock(e.position)}: {e.cause}{silent}")
    more = f"\n(+{len(events) - limit} earlier)" if len(events) > limit else ""
    return "\n".join(reversed(lines)) + more, "warn"


def describe_transition(transition: str) -> tuple[str, str]:
    """(text, tone): how the track playing began — straight on from the last one, or not."""
    if transition == "gapless":
        return "✓ Gapless: carried straight on from the previous track, sample for sample", "good"
    if transition in ("", "started"):
        return "Started fresh (chosen, skipped to, or the first track)", "plain"
    return f"A short gap before this track: {transition}", "plain"


def describe_loudness(lufs: float | None, lra: float | None) -> str:
    if lufs is None:
        return "silent"
    spread = f" · range {lra:.1f} LU" if lra is not None else " · too short for a range"
    return f"{lufs:.1f} LUFS integrated{spread}"


def describe_analysis(result: Analysis, codec: str = "FLAC", cue: bool = False) -> list[tuple[str, str]]:
    """`cue`: the track is one track of a CUE image (its file's MD5, if any, covers every track)."""
    if result.damaged_at is not None:
        check = (f"✗ Damaged: the audio stops decoding at {fmt_clock(result.damaged_at)} of "
                 f"{fmt_clock(result.duration)}, so the rest can't be played. Re-rip or re-download it "
                 "(the figures below cover only the part that decodes)")
    elif result.md5_ok is None and cue and codec == "FLAC":
        check = ("Not per track: this is one track of a CUE sheet, and the file's MD5 covers the whole "
                 "file (all its tracks)")
    elif result.md5_ok is None and codec in ("WAV", "AIFF"):
        check = f"Not applicable: {codec} is uncompressed and stores no fingerprint to check against"
    elif result.md5_ok is None and codec == "ALAC":
        check = "Not applicable: Apple Lossless stores no fingerprint of the audio to check against"
    elif result.md5_ok is None and codec in LOSSY_CODECS:
        check = f"Not applicable: {codec} is lossy, so there's no original audio to verify against"
    elif result.md5_ok is None:
        check = "No MD5 stored in this file, so it can't be verified"
    elif result.md5_ok:
        check = "✓ Verified: decodes exactly as it was encoded"
    else:
        check = "✗ MD5 mismatch: the audio is damaged or was altered after encoding"
    dr = f"DR{result.dr}" if result.dr is not None else "—"
    peak = f"{result.peak_dbfs:.2f} dBFS" if result.peak_dbfs != float("-inf") else "silent"
    if result.peak_at is not None:
        peak += f" at {fmt_clock(result.peak_at)}"
    rows = [("Lossless check", check)]
    bits = describe_bits(result)
    if bits:
        rows.append(("Bit depth", bits))
    sound = describe_range(result)
    if sound:
        rows.append(("Frequencies", sound))
    if result.flags is not None:
        rows += describe_flags(result.flags, result.rate)
    rows += [("Dynamic range", dr), ("Peak level", peak)]
    if result.true_peak is not None:
        rows.append(("True peak", describe_true_peak(result)))
    rows += [("Clipping", describe_clipping(result)),
             ("Loudness", describe_loudness(result.lufs, result.lra))]
    rows += describe_stereo(result)
    return rows


def describe_bits(a: Analysis) -> str:
    """The "padded?" clue: how many of the file's bits ever carry sound."""
    if a.float_samples:
        return "Floating-point samples: no fixed bit depth to check"
    if a.bits is None:
        return ""
    if a.bits_used is None:
        return "silent"
    if a.bits_used >= a.bits:
        return f"✓ All {a.bits} bits used"
    if a.bits == 24 and a.bits_used <= 16:
        return (f"⚠ Only {a.bits_used} of its {a.bits} bits are ever used: a 16-bit recording "
                "stored as 24-bit, so no more detail than a CD")
    return f"⚠ Only {a.bits_used} of its {a.bits} bits are ever used (the lowest bits are always zero)"


def describe_range(a: Analysis) -> str:
    """The "lossy copy? upsampled?" clue: where the sound stops, and how."""
    if a.cutoff_hz is None:
        return ""
    khz = f"{a.cutoff_hz / 1000:.1f} kHz"
    if a.lossy:
        return f"Sound reaches {khz} (an MP3 trims the top end by design)"
    if a.verdict == "lossy":
        return (f"⚠ Sound stops sharply at {khz}: suggests it was made from a lossy file "
                "(MP3, AAC…) — lossless from here on, but no better than that copy")
    if a.verdict == "upsampled":
        return (f"⚠ Sound stops sharply at {khz}: suggests it was upsampled from CD quality "
                f"(44.1/48 kHz), so the {format_rate(a.rate)} adds no real detail")
    if a.cutoff_hz < LOSSY_BELOW_HZ:
        return f"Little above {khz}, fading out gradually: the recording itself, not a sign of a lossy copy"
    if a.rate >= 88200 and a.cutoff_hz >= UPSAMPLED_BELOW_HZ:
        return f"✓ Sound reaches {khz}: real high-resolution content"
    return f"✓ Sound reaches {khz}: no sign of a lossy source"


def describe_flags(flags: FormatFlags, rate: int = 0) -> list[tuple[str, str]]:
    """Rows for what the file carries: HDCD, MQA, pre-emphasis (0.9.0). Found only — the player
    decodes none of them, so what's sent is still exactly the file."""
    rows = []
    hdcd = flags.hdcd
    if hdcd is not None and hdcd.detected:
        if hdcd.effectual:
            used, would = [], []
            if hdcd.peak_extend != "never":
                used.append(f"peak extend {'always on' if hdcd.peak_extend == 'permanent' else 'used at times'}")
                would.append("expand the loudest peaks")
            if hdcd.max_gain_db:
                used.append(f"gain adjust down to {hdcd.max_gain_db:.1f} dB")
                would.append(f"turn parts down by up to {-hdcd.max_gain_db:.1f} dB")
            text = (f"⚠ HDCD-encoded ({' · '.join(used)}): an HDCD decoder would {' and '.join(would)}. "
                    "Not decoded here — you hear the CD as pressed")
        else:
            text = ("HDCD codes present, but asking for no change (no peak extend, no gain adjust): "
                    "sounds the same with or without a decoder")
        if hdcd.packet_type == "A+B":
            text += " · both packet types mixed (usually an encoding slip)"
        rows.append(("HDCD", text))
    mqa = flags.mqa
    if mqa is not None and mqa.detected:
        details = []
        if mqa.original_rate:
            details.append(f"original {format_rate(mqa.original_rate)}")
        if mqa.studio:
            details.append("MQA Studio")
        if mqa.encoder:
            details.append(mqa.encoder)
        stored = f" {format_rate(rate)}" if rate else ""
        rows.append(("MQA", f"⚠ MQA-encoded{' (' + ' · '.join(details) + ')' if details else ''}: not decoded "
                            f"here — played as the plain{stored} audio it's stored as, MQA's hidden data "
                            "included in the lowest bits"))
    elif mqa is not None and mqa.encoder:
        rows.append(("MQA", f"MQAENCODER tag ({mqa.encoder}), but no MQA signal found in the audio: "
                            "probably already decoded or converted"))
    pre = flags.pre_emphasis
    if pre.flagged:
        rows.append(("Pre-emphasis", f"⚠ Flagged as pre-emphasised (in {'; '.join(pre.sources)}): the treble was "
                                     "boosted at mastering, to be cut again on playback (de-emphasis). "
                                     "This player doesn't de-emphasise, so it sounds brighter than intended"))
    if not rows:
        rows.append(("Format flags", "✓ None: no HDCD, MQA or pre-emphasis"))
    return rows


def describe_true_peak(a: Analysis) -> str:
    db = a.true_peak_db
    over = " (over full scale between samples: a converter may clip)" if db > 0 else ""
    plr = f" · PLR {a.plr:.1f} dB (peak to loudness: bigger = more dynamic)" if a.plr is not None else ""
    text = f"{db:+.2f} dBTP{over}{plr}"
    return f"⚠ {text}" if db > 0 else text


def describe_clipping(a: Analysis) -> str:
    if not a.clip_runs:
        return f"✓ None ({CLIP_RUN}+ samples in a row stuck at full scale)"
    runs = f"{a.clip_runs} run{'' if a.clip_runs == 1 else 's'}"
    return (f"⚠ {runs} of {CLIP_RUN}+ samples stuck at full scale ({a.clipped_samples} samples): "
            "clipped when it was mastered")


def describe_stereo(a: Analysis) -> list[tuple[str, str]]:
    rows = []
    if a.identical_channels:
        rows.append(("Stereo", "Left and right are identical: a mono recording stored as stereo"))
    elif a.correlation is not None:
        side = "left" if a.balance_db > 0 else "right"
        balance = ("balanced" if abs(a.balance_db) < 0.5
                   else f"{side} louder by {abs(a.balance_db):.1f} dB")
        text = f"correlation {a.correlation:+.2f} (+1 = same in both, 0 = unrelated) · {balance}"
        if a.correlation < 0:
            text = "⚠ " + text + " · the channels mostly oppose: one may be inverted (out of phase)"
        rows.append(("Stereo", text))
    if a.dc_offset:
        worst = max(abs(d) for d in a.dc_offset)
        if worst < 10 ** (-60 / 20):
            rows.append(("DC offset", "✓ None to speak of (below -60 dBFS)"))
        else:
            names = ["L", "R"] if len(a.dc_offset) == 2 else [f"ch{k + 1}" for k in range(len(a.dc_offset))]
            levels = " · ".join(f"{n} {to_db(abs(d)):.0f} dBFS" for n, d in zip(names, a.dc_offset))
            warn = "⚠ " if worst >= 10 ** (-40 / 20) else ""
            rows.append(("DC offset", f"{warn}{levels} (a constant push away from zero)"))
    return rows


def is_cd_audio(track: Track) -> bool:
    """16-bit, 44.1 kHz stereo FLAC or WAV: what a CD rip is, and all rip verification can check."""
    return (track.codec in ("FLAC", "WAV") and track.sample_rate == 44100 and track.bits == 16
            and track.channels == 2 and not track.float_samples)


def describe_album(album: AlbumResult) -> str:
    count = f"{album.tracks} track{'' if album.tracks == 1 else 's'}"
    dr = ""
    if album.dr is not None:
        spread = (f" (tracks DR{album.dr_low}–{album.dr_high})" if album.dr_low != album.dr_high else "")
        dr = f"DR{album.dr}{spread} · "
    return f"{dr}{describe_loudness(album.lufs, album.lra)} · {count} as one programme"


class NowPlayingWindow:
    """Live stats for whatever is playing; follows the player from track to track."""

    _analysis_cache: dict[Path, Analysis] = {}
    _index_cache: dict[Path, FrameIndex | None] = {}
    _album_cache: dict[Path, AlbumResult] = {}  # album folder -> its DR and loudness
    _rip_cache: dict[Path, ripcheck.Verification] = {}  # disc folder -> its rip check
    INDEX_CACHE_SIZE = 20

    def __init__(self, parent: tk.Misc, player: Player, geometry: str = "",
                 on_moved=lambda geometry: None, on_meters=None, on_pcm=None, on_spectrogram=None,
                 on_scope=None, on_compare=None, on_abx=None, on_testsignals=None, prefs: dict | None = None,
                 on_clipping=None):
        self.player = player
        self.prefs = prefs if prefs is not None else {}  # read live: Settings → Allow network access
        self.on_moved = on_moved
        self.win = tk.Toplevel(parent, bg=theme.BG)
        self.win.title("Now playing")
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<Escape>", lambda e: self.close())
        theme.dark_title_bar(self.win)

        self._track_path: Path | None = None
        self._index: FrameIndex | None = None
        self._shown_db: list[float] = []
        self._analysis: Analysis | None = None
        self._analysis_state: dict | None = None  # the check running: {"path", "progress", "done", "error"}
        self._cancel = threading.Event()
        self._album_cancel = threading.Event()  # only the window closing stops an album measurement
        self._album_state: dict | None = None
        self._rip_cancel = threading.Event()   # only the window closing stops a rip check
        self._rip_state: dict | None = None
        self._job = None
        self._keep_going = toolwin.KeepGoing(self.win)

        # (0.22.0) Everything sits in a panel that scrolls when the window is shorter than it: the
        # window grows with its content (as before) but never past the bottom of the screen.
        self.area = controls.ScrollArea(self.win, bg="BG")
        self.area.pack(fill="both", expand=True)
        outer = ttk.Frame(self.area.inner, padding=theme.px(theme.GAP_WIDE))
        outer.pack(fill="both", expand=True)
        self.title_label = ttk.Label(outer, text="Nothing playing", style="Title.TLabel", anchor="w")
        self.title_label.pack(fill="x")
        self.sub_label = ttk.Label(outer, text="", style="Sub.TLabel", anchor="w")
        self.sub_label.pack(fill="x")
        # The other windows, as flat pill buttons in rows (the window is narrow): looking at the
        # sound, then testing it. (0.24.0) Clipping sits beside Meters / PCM stream; Stereo moves down
        # a row so each row still fits the narrow window.
        self.tool_buttons: dict[str, controls.FlatButton] = {}
        for first, tools in ((True, [("Meters (M)", on_meters), ("PCM stream (B)", on_pcm),
                                     ("Clipping (C)", on_clipping), ("Spectrogram (F)", on_spectrogram)]),
                             (False, [("Stereo (O)", on_scope), ("Compare two files (D)", on_compare),
                                      ("ABX blind test (X)", on_abx)]),
                             (False, [("Test signals (T)", on_testsignals)])):
            tools = [(text, command) for text, command in tools if command]
            if not tools:
                continue
            row_frame = ttk.Frame(outer)
            row_frame.pack(fill="x", pady=theme.pad(theme.GAP_MEDIUM if first else theme.GAP, 0))
            for index, (text, command) in enumerate(tools):
                button = controls.FlatButton(row_frame, text, command)
                button.pack(side="left", padx=theme.pad(0 if index == 0 else theme.GAP, 0))
                self.tool_buttons[text] = button

        live = self._section(outer, "Live", first=True)
        self.values: dict[str, ttk.Label] = {}
        rows = [("position", "Position"), ("start", "Start"), ("bitrate", "Bitrate"), ("levels", "Levels (source)"),
                ("peak", "Track peak"), ("volume", "Volume"), ("output", "Output"), ("buffer", "Buffer")]
        for row, (key, label) in enumerate(rows):
            self._label(live, row, label)
            if key == "levels":
                self.meter = tk.Canvas(live, width=theme.px(330), height=theme.px(30),
                                       bg=theme.BG, highlightthickness=0)
                self.meter.grid(row=row, column=1, sticky="w", pady=theme.px(2))
                continue
            value = ttk.Label(live, text="—", anchor="w", justify="left", wraplength=theme.px(VALUE_WRAP),
                              style=self._style(key))
            value.grid(row=row, column=1, sticky="w", pady=theme.px(1))
            self.values[key] = value

        proof = self._section(outer, "Proof")
        for row, (key, label) in enumerate([("sent", "Sent"), ("safety", "Safety"), ("events", "Events")]):
            self._label(proof, row, label)
            value = ttk.Label(proof, text="—", anchor="w", justify="left", wraplength=theme.px(VALUE_WRAP))
            value.grid(row=row, column=1, sticky="w", pady=theme.px(1))
            self.values[key] = value
        self._label(proof, 3, MIXER, theme.pad(theme.GAP, 1))
        self.windows_frame = ttk.Frame(proof)
        self.windows_frame.grid(row=3, column=1, sticky="w", pady=theme.pad(theme.GAP, 1))
        self.windows_button = controls.FlatButton(self.windows_frame, f"Check what {MIXER} does to the sound",
                                                  self._start_windows_check)
        self.windows_button.grid(row=0, column=0, columnspan=2, sticky="w")
        self.windows_note = ttk.Label(self.windows_frame, text="", style="Muted.TLabel",
                                      wraplength=theme.px(VALUE_WRAP), justify="left")
        self.windows_note.grid(row=1, column=0, columnspan=2, sticky="w", pady=theme.pad(theme.GAP_SMALL, 0))
        self.windows_note.grid_remove()   # (shown only while it has something to say: _say)
        self._windows_result: dict | None = None
        self._windows_rows: list = []

        self.file_frame = self._section(outer, "File")
        self.check_frame = self._section(outer, "Check")
        self._fit_job = None
        self._fitted: tuple[int, int] | None = None   # the size _fit last gave the window
        self._settled = False                         # …and the window has reached it since
        self.user_sized = False                       # the window was resized by hand: leave its size be
        placement.restore(self.win, geometry, keep_size=False)  # its size follows the content (_fit)
        self.win.bind("<Configure>", self._moved)
        self._align()
        self._tick()

    @classmethod
    def cached_analysis(cls, path) -> Analysis | None:
        """The check's result for a file, if it has been run this session (for the Spectrogram's
        cut-off line and the bitscope's whole-file line)."""
        return cls._analysis_cache.get(path if isinstance(path, Path) else Path(path)) if path else None

    def _moved(self, event):
        if event.widget is self.win:
            if self._fitted is not None:   # a size that isn't _fit's, once it has been reached: a hand resize
                if (event.width, event.height) == self._fitted:
                    self._settled = True
                elif self._settled:
                    self.user_sized = True
            geometry = placement.current(self.win)
            if geometry:
                self.on_moved(geometry)

    # -- layout (0.22.0) -----------------------------------------------------------
    def _section(self, parent, title: str, first: bool = False) -> ttk.Frame:
        """A section: Settings' heading style, then a two-column grid (labels, values)."""
        controls.section(parent, title, first)
        frame = ttk.Frame(parent)
        frame.pack(fill="x")
        return frame

    def _label(self, frame, row: int, text: str, pady=None):
        ttk.Label(frame, text=text, style="Muted.TLabel").grid(
            row=row, column=0, sticky="nw", pady=theme.px(1) if pady is None else pady)

    def _row(self, frame, row, label, value, style=None):
        """A label and its value; the value's colour from its first character (tone_of) unless given."""
        self._label(frame, row, label)
        ttk.Label(frame, text=value, style=style or TONE_STYLE[tone_of(value)], wraplength=theme.px(VALUE_WRAP),
                  justify="left").grid(row=row, column=1, sticky="w", pady=theme.px(1))

    @staticmethod
    def _style(key: str, tone: str = "plain") -> str:
        """A Live value's style: a number row keeps its fixed-width digits whatever its colour."""
        return (NUMBER_STYLE if key in NUMBER_ROWS else TONE_STYLE).get(tone, "TLabel")

    @staticmethod
    def _say(label: ttk.Label, text: str, style: str | None = None):
        """Set a note's text; an empty note takes no room (it used to leave a blank line)."""
        label.configure(text=text, **({"style": style} if style else {}))
        if text:
            label.grid()
        else:
            label.grid_remove()

    def _align(self):
        """One label column for the whole window: every section's labels as wide as the longest
        label anywhere, so all the values start at the same place. Then fit the window."""
        frames = [w for w in (self.values["position"].master, self.values["sent"].master, self.file_frame,
                              self.check_frame)]
        widest = max([theme.px(LABEL_MIN)] + [child.winfo_reqwidth() for frame in frames
                                              for child in frame.grid_slaves(column=0)
                                              if isinstance(child, ttk.Label)])
        for frame in frames:
            frame.columnconfigure(0, minsize=widest + theme.px(theme.GAP_WIDE))
        self._refit()

    def _refit(self):
        if self._fit_job is None:
            self._fit_job = self.win.after_idle(self._fit)

    def _fit(self):
        """As big as the content (as the window always was), but no taller than the screen: past
        that the content scrolls. Left alone once the window has been resized by hand."""
        self._fit_job = None
        if not self.win.winfo_exists():
            return
        self.area.bind_wheel()   # (new rows need the wheel too)
        if self.user_sized:
            return
        self.win.update_idletasks()
        width, height = self.area.inner.winfo_reqwidth(), self.area.inner.winfo_reqheight()
        room = placement.work_height(self.win) - theme.px(FIT_MARGIN)
        if height > room:
            width += max(4, theme.px(theme.SCROLLBAR_WIDTH))   # the slim scrollbar shows
            height = room
        if (width, height) != self._fitted:
            self._fitted, self._settled = (width, height), False
            self.win.geometry(f"{width}x{height}")

    def lift(self):
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    # -- per track -----------------------------------------------------------
    def _on_track_change(self, track: Track):
        self._track_path = track.path
        self._cancel.set()
        self._cancel = threading.Event()
        self._analysis_state = None   # (the last track's check is cancelled; its watch stops)
        self._analysis = self._analysis_cache.get(track.path)
        self.title_label.configure(text=track.title)
        self.sub_label.configure(text=track.display_artist_album or track.path.parent.name)
        self._shown_db = []

        for child in self.file_frame.winfo_children():
            child.destroy()
        rows = [
            ("File", track.path.name),
            ("Folder", str(track.path.parent)),
            ("Format", track.format_line),
            ("Length", fmt_duration(track.duration)),
            ("File size", fmt_size(track.size)),
        ]
        if track.is_flac:
            md5 = f"{track.md5:032x}" if track.md5 else "none"
            if track.cue is not None:
                md5 = "the file's own (covers the whole file, not one track)"
            rows += [("Encoder", track.encoder or "—"), ("Stored MD5", md5)]
        elif track.codec in ("WAV", "AIFF"):
            rows.append(("Encoding", "uncompressed floating-point PCM" if track.float_samples
                         else "uncompressed PCM"))
        rows += list(track.details)
        row = 0
        if track.misnamed:
            self._row(self.file_frame, row, "Heads up",
                      f"This file is really {track.codec}, but its name ends in {track.misnamed}",
                      "WarnValue.TLabel")
            row += 1
        for label, value in rows:
            self._row(self.file_frame, row, label, value,
                      "WarnValue.TLabel" if label in ("Codec", "Warning") and track.lossy else "TLabel")
            row += 1
        self._show_check()   # (and lines the columns up again: _align)
        if track.is_flac and track.cue is None:
            self._load_index(track.path, build_index)
        elif track.codec == "MP3" and track.bitrate_mode != "CBR" and track.cue is None:
            self._load_index(track.path, build_mp3_index)
        else:
            self._index = None  # constant bitrate, or no frame index for this format: average only

    def _load_index(self, path: Path, builder):
        if path in self._index_cache:
            self._index = self._index_cache[path]
            return
        self._index = None

        def work():
            try:
                index = builder(path)
            except Exception:  # noqa: BLE001 — no live bitrate is fine; the average still shows
                index = None
            cache = self._index_cache
            cache[path] = index
            while len(cache) > self.INDEX_CACHE_SIZE:
                cache.pop(next(iter(cache)))
            if path == self._track_path:
                self._index = index

        threading.Thread(target=work, name="frame-index", daemon=True).start()

    def _show_check(self):
        for child in self.check_frame.winfo_children():
            child.destroy()
        if self._analysis is not None:
            playing = self.player.status().track
            rows = describe_analysis(self._analysis, playing.codec if playing else "FLAC",
                                     cue=playing is not None and playing.cue is not None)
            for row, (label, value) in enumerate(rows):
                # ✓ green, ✗ / ⚠ amber (tone_of); negative figures with the minus sign, as the live ones
                self._row(self.check_frame, row, label, theme.figures(value))
            self._show_album(len(rows))
            self._show_rip(len(rows) + 1)
            self._align()
            return
        track = self.player.status().track
        label = ("Verify & examine (genuine?, dynamics, loudness, stereo)" if track is None or track.is_flac
                 else "Examine (genuine?, dynamics, loudness, stereo)")
        running = self._analysis_state   # (rebuilt while the check runs: still greyed out, still counting)
        self.check_button = controls.FlatButton(self.check_frame, label, self._start_analysis, kind="primary",
                                                state="disabled" if running else "normal")
        self.check_button.grid(row=0, column=0, columnspan=2, sticky="w")
        self.check_progress = ttk.Label(self.check_frame, text="", style="Muted.TLabel")
        self.check_progress.grid(row=1, column=0, columnspan=2, sticky="w", pady=theme.pad(theme.GAP_SMALL, 0))
        self._say(self.check_progress, f"Reading the whole file… {running['progress']:.0%}" if running else "")
        self._show_album(2)
        self._show_rip(3)
        self._align()

    # -- the whole album's loudness ---------------------------------------------
    def _album(self) -> tuple[Path, list[Path]] | None:
        track = self.player.status().track
        if track is None:
            return None
        try:
            return album_files(track.path)
        except OSError:
            return None

    def _show_album(self, row: int):
        album = self._album()
        if album is None:
            return
        folder, files = album
        found = self._album_cache.get(folder)
        if found is not None:
            self._row(self.check_frame, row, "Album", describe_album(found))
            return
        self._label(self.check_frame, row, "Album", theme.pad(theme.GAP, 1))
        holder = ttk.Frame(self.check_frame)
        holder.grid(row=row, column=1, sticky="w", pady=theme.pad(theme.GAP, 1))
        running = self._album_state is not None and self._album_state["folder"] == folder
        self.album_button = controls.FlatButton(holder, f"Measure the whole album ({len(files)} tracks): DR & loudness",
                                                lambda: self._start_album(folder, files),
                                                state="disabled" if running else "normal")
        self.album_button.grid(row=0, column=0, sticky="w")
        self.album_progress = ttk.Label(holder, text="", style="Muted.TLabel")
        self.album_progress.grid(row=1, column=0, sticky="w", pady=theme.pad(theme.GAP_SMALL, 0))
        self.album_progress.grid_remove()   # (a running measurement's watch fills it: _say)

    def _start_album(self, folder: Path, files: list[Path]):
        state = {"folder": folder, "progress": 0.0, "done": False}
        self._album_state = state
        self.album_button.configure(state="disabled")
        cancel = self._album_cancel

        def work():
            try:
                result = album_measure(files, cancel, progress=lambda p: state.__setitem__("progress", p))
            except (OSError, RuntimeError):
                result = None
            except Exception:  # noqa: BLE001 — logged; the button comes back (never greyed out for good)
                log.exception("measuring the album %s", folder)
                result = None
            if result is not None:
                self._album_cache[folder] = result
            state["done"] = True

        threading.Thread(target=work, name="album-loudness", daemon=True).start()
        self._watch_album(state)

    def _watch_album(self, state: dict):
        if not self.win.winfo_exists() or state is not self._album_state:
            return
        if state["done"]:
            self._album_state = None
            self._show_check()
            return
        album = self._album()
        if album is not None and album[0] == state["folder"]:
            try:
                self._say(self.album_progress, f"Reading every track… {state['progress']:.0%}")
            except tk.TclError:
                pass  # the row is being rebuilt
        self.win.after(200, self._watch_album, state)

    # -- is the rip identical to other people's? (AccurateRip, CUETools DB) ------------
    def network_changed(self):
        """Settings → Allow network access was switched: redraw the Check (the button's state)."""
        if self.win.winfo_exists():
            self._show_check()

    def _show_rip(self, row: int):
        track = self.player.status().track
        if track is None or not is_cd_audio(track):
            return  # nothing to check: rip verification is for CD audio
        folder = track.path.parent
        found = self._rip_cache.get(folder)
        if found is not None:
            for label, text, tone in ripcheck.describe(found):
                self._row(self.check_frame, row, label, text, TONE_STYLE[tone])
                row += 1
            return
        self._label(self.check_frame, row, "Rip", theme.pad(theme.GAP, 1))
        holder = ttk.Frame(self.check_frame)
        holder.grid(row=row, column=1, sticky="w", pady=theme.pad(theme.GAP, 1))
        allowed = ripcheck.network_allowed(self.prefs)
        running = self._rip_state is not None and self._rip_state["folder"] == folder
        self.rip_button = controls.FlatButton(holder, "Verify rip online (AccurateRip, CUETools DB)",
                                              lambda: self._start_rip(track.path, folder),
                                              state="normal" if allowed and not running else "disabled")
        self.rip_button.grid(row=0, column=0, sticky="w")
        self.rip_progress = ttk.Label(holder, text="", style="Muted.TLabel",
                                      wraplength=theme.px(VALUE_WRAP - 20), justify="left")
        self.rip_progress.grid(row=1, column=0, sticky="w", pady=theme.pad(theme.GAP_SMALL, 0))
        self._say(self.rip_progress, "" if allowed else NETWORK_HINT)   # (a running check's watch updates it)

    def _start_rip(self, path: Path, folder: Path):
        if not ripcheck.network_allowed(self.prefs):
            return
        state = {"folder": folder, "progress": 0.0, "done": False}
        self._rip_state = state
        self.rip_button.configure(state="disabled")
        cancel = self._rip_cancel
        prefs = self.prefs

        def work():
            try:
                result = ripcheck.verify(path, prefs, cancel,
                                         progress=lambda p: state.__setitem__("progress", p))
            except ripcheck.NetworkOff:
                result = None  # switched off part-way: nothing kept, the button comes back
            except Exception as error:  # noqa: BLE001 — a failed check is reported, never a crash
                result = ripcheck.Verification(problem=f"something went wrong ({error})")
            if result is not None:
                self._rip_cache[folder] = result
            state["done"] = True

        threading.Thread(target=work, name="rip-check", daemon=True).start()
        self._watch_rip(state)

    def _watch_rip(self, state: dict):
        if not self.win.winfo_exists() or state is not self._rip_state:
            return
        if state["done"]:
            self._rip_state = None
            self._show_check()
            return
        track = self.player.status().track
        if track is not None and track.path.parent == state["folder"]:
            text = (f"Reading every track… {state['progress']:.0%}" if state["progress"] < 1
                    else "Asking AccurateRip and CUETools DB…")
            try:
                self._say(self.rip_progress, text)
            except tk.TclError:
                pass  # the row is being rebuilt
        self.win.after(200, self._watch_rip, state)

    def _start_analysis(self):
        track = self.player.status().track
        if track is None or self._analysis_state is not None:
            return
        path = track.path
        state = {"path": path, "progress": 0.0, "done": False, "error": ""}
        self._analysis_state = state
        self.check_button.configure(state="disabled")
        cancel = self._cancel

        def work():
            try:
                result = analyse(path, track.bits, track.md5,
                                 progress=lambda p: state.__setitem__("progress", p), cancel=cancel)
            except (OSError, RuntimeError):  # won't even open: damaged from the start
                result = Analysis(md5_ok=False, dr=None, peak_dbfs=float("-inf"), damaged_at=0.0,
                                  duration=track.duration)
            except Exception as exc:  # noqa: BLE001 — not the file's fault: said, and the button comes back
                log.exception("checking %s", path)
                result, state["error"] = None, f"{type(exc).__name__}: {exc}"
            if result is not None:
                self._analysis_cache[path] = result
            state["done"] = True

        threading.Thread(target=work, name="analysis", daemon=True).start()
        self._watch_analysis(state)

    def _watch_analysis(self, state: dict):
        if not self.win.winfo_exists() or state is not self._analysis_state:
            return   # closed, or another track since (its check was cancelled)
        if not state["done"]:
            try:
                self._say(self.check_progress, f"Reading the whole file… {state['progress']:.0%}")
            except tk.TclError:
                pass  # the row is being rebuilt
            self.win.after(150, self._watch_analysis, state)
            return
        self._analysis_state = None
        self._analysis = self._analysis_cache.get(state["path"])
        self._show_check()   # the result, or the button again (cancelled, or failed: said below it)
        if self._analysis is None and state["error"]:
            self._say(self.check_progress, f"The check failed: {state['error']}")

    # -- live ----------------------------------------------------------------
    def _tick(self):
        if not self.win.winfo_exists():
            return
        with self._keep_going:
            self._step()
        self._job = self.win.after(REFRESH_MS, self._tick)

    def _step(self):
        status = self.player.status()
        track = status.track
        if track is not None and track.path != self._track_path:
            self._on_track_change(track)
        if not toolwin.on_screen(self.win):
            return   # minimised: the live figures aren't drawn

        if status.state is State.STOPPED or track is None:
            for key, value in self.values.items():
                value.configure(text="—", style=self._style(key))
            if track is not None:
                self.values["position"].configure(text=f"stopped · {fmt_clock(status.duration)}")
                if track.bitrate:
                    self.values["bitrate"].configure(text=f"{round(track.bitrate / 1000):,} kbps average")
            self._draw_meter(())
        else:
            self._update_live(status, track)

    def _update_live(self, status, track: Track):
        pos, dur = status.position, status.duration
        paused = " · paused" if status.state is State.PAUSED else ""
        self.values["position"].configure(
            text=f"{fmt_clock(pos)} / {fmt_clock(dur)}  ({theme.MINUS}{fmt_clock(dur - pos)}){paused}")

        if track.codec in ("WAV", "AIFF") or (track.codec == "MP3" and track.bitrate_mode == "CBR"):
            kind = "uncompressed, constant" if track.codec in ("WAV", "AIFF") else "constant bitrate"
            self.values["bitrate"].configure(
                text=f"{round(track.bitrate / 1000):,} kbps ({kind})" if track.bitrate else "—")
        elif not (track.is_flac or track.codec == "MP3") or track.cue is not None:
            # ALAC / Vorbis / Opus (no frame index yet), or one track of a CUE image: average only
            self.values["bitrate"].configure(
                text=f"{round(track.bitrate / 1000):,} kbps average" if track.bitrate else "—")
        else:
            avg = f"{round(track.bitrate / 1000):,} kbps average" if track.bitrate else ""
            live = self._index.bitrate_at(pos) if self._index is not None else None
            # (0.22.0) Four digit-wide places, so 883 → 1024 doesn't push the average along.
            now = (f"{theme.pad_figures(f'{live / 1000:.0f}', 4, after=True)} kbps now" if live is not None
                   else "measuring…")
            self.values["bitrate"].configure(text=f"{now} · {avg}" if avg else now)

        self._draw_meter(status.levels)

        if status.track_peak > 0:
            full = status.track_peak >= 0.9999
            self.values["peak"].configure(
                text="0.0 dBFS — full scale (often clipped in mastering)" if full
                else theme.figures(f"{to_db(status.track_peak):.1f} dBFS"),
                style=self._style("peak", "warn" if full else "plain"))

        gain = status.gain
        if gain <= 0:
            volume = "muted"
        elif gain >= 1.0:
            volume = f"{self.player.volume:.0%} · 0.0 dB (untouched)"
        else:
            volume = f"{self.player.volume:.0%} · {to_db(gain):+.1f} dB"
        self.values["volume"].configure(text=theme.figures(volume))

        text, tone = output_line(status.output, status.gain, track.float_samples, track.lossy, status.trim)
        self.values["output"].configure(text=text.removeprefix("→ "), style=self._style("output", tone))

        drops = status.dropouts
        full = theme.pad_figures(f"{status.buffer:.0%}", 4, after=True)
        self.values["buffer"].configure(
            text=f"{full} full · {drops} dropout{'' if drops == 1 else 's'}",
            style=self._style("buffer", "warn" if drops else "plain"))

        text, tone = describe_transition(status.transition)
        self.values["start"].configure(text=text, style=self._style("start", tone))
        for key, (text, tone) in (
                ("sent", describe_proof(status.proof, status.output, status.gain, status.trim, track.lossy,
                                        track.float_samples)),
                ("safety", describe_health(status.health, status.dropouts)),
                ("events", describe_events(status.events))):
            self.values[key].configure(text=text, style=self._style(key, tone))
        self._paint_windows_button()

    # -- what Windows does ---------------------------------------------------
    def _start_windows_check(self):
        """Record what the player sends and what Windows plays, at the same time, then compare.
        All of it on a worker thread: the window only polls for the answer."""
        for widget in self._windows_rows:
            widget.destroy()
        self._windows_rows = []
        self.windows_button.configure(state="disabled")
        self._say(self.windows_note, f"Listening for {WINDOWS_CHECK_SECONDS:g} seconds…", "Muted.TLabel")
        result: dict = {}
        self._windows_result = result
        player = self.player

        def work():
            try:
                import comtypes

                comtypes.CoInitialize()
                status = player.status()
                device = status.output.device if status.output else None
                recording: dict = {}
                flowing = threading.Event()

                def listen():
                    try:
                        comtypes.CoInitialize()
                        recording["take"] = loopback.record(device, WINDOWS_CHECK_SECONDS + 3.0, flowing)
                    except Exception as exc:  # noqa: BLE001
                        recording["error"] = exc

                listener = threading.Thread(target=listen, name="windows-listen", daemon=True)
                listener.start()
                # The player's own recording starts only once Windows' is receiving sound, so
                # what the player sends is sure to be inside what Windows recorded.
                flowing.wait(3.0)
                time.sleep(0.2)
                tapped = player.tap(WINDOWS_CHECK_SECONDS)
                if tapped is None:
                    result["note"] = "Start the music (not paused) in shared mode, then try again"
                    return
                tap, rate, device = tapped
                tap["done"].wait(WINDOWS_CHECK_SECONDS + 3)
                listener.join(WINDOWS_CHECK_SECONDS + 5)
                take = recording.get("take")
                result["settings"] = loopback.settings(device)
                if take is None or not len(take.audio):
                    reason = recording.get("error") or "Windows sent nothing to record"
                    result["note"] = f"Couldn't record what {MIXER} played ({reason})"
                    return
                result["check"] = compare_paths(tap["audio"][: tap["filled"]], rate, take.audio, take.rate)
                result["gaps"] = take.gaps
            except Exception as exc:  # noqa: BLE001 — report, don't crash the window
                result["note"] = f"The check failed: {exc}"
            finally:
                result["done"] = True

        threading.Thread(target=work, name="windows-check", daemon=True).start()
        self.win.after(200, self._watch_windows_check, result)

    def _watch_windows_check(self, result: dict):
        if not self.win.winfo_exists() or result is not self._windows_result:
            return
        if not result.get("done"):
            self.win.after(200, self._watch_windows_check, result)
            return
        self._paint_windows_button()
        self._say(self.windows_note, result.get("note", ""), "Muted.TLabel")
        lines = describe_path(result["check"]) if "check" in result else []
        found = result.get("settings")
        if found is not None:
            facts = []
            if found.mixer_rate:
                facts.append(f"mixer {found.mixer_rate / 1000:g} kHz / {found.mixer_bits}-bit")
            if found.system_volume is not None:
                facts.append(f"device volume slider {found.system_volume:.0%}" + (" (muted)" if found.muted else ""))
            if found.app_volume is not None:
                facts.append(f"this player's slider in the Volume mixer {found.app_volume:.0%}")
            if facts:
                lines.append(("Settings", " · ".join(facts) + ". (The device slider may not show in the "
                              "recording above.)", "plain"))
        if result.get("gaps"):
            lines.append(("Note", f"{MIXER}' recording had {result['gaps']} break(s); try again if the "
                                  "result looks odd", "plain"))
        for row, (label, text, tone) in enumerate(lines, start=2):
            name = ttk.Label(self.windows_frame, text=label, style="Muted.TLabel")
            name.grid(row=row, column=0, sticky="nw", padx=theme.pad(0, theme.GAP_MEDIUM), pady=theme.px(1))
            value = ttk.Label(self.windows_frame, text=text, wraplength=theme.px(VALUE_WRAP - 70), justify="left",
                              style=TONE_STYLE.get(tone, "TLabel"))
            value.grid(row=row, column=1, sticky="w", pady=theme.px(1))
            self._windows_rows += [name, value]
        self._refit()

    def _paint_windows_button(self):
        """The check only makes sense in shared mode: bit-perfect goes around Windows' mixer."""
        if self._windows_result is not None and not self._windows_result.get("done"):
            return  # a check is running
        status = self.player.status()
        exclusive = status.output is not None and status.output.exclusive
        self.windows_button.configure(state="disabled" if exclusive or not loopback.AVAILABLE else "normal")
        if exclusive and not self._windows_rows:
            if not self.windows_note.cget("text").startswith("Not needed"):
                self._say(self.windows_note, f"Not needed: in bit-perfect mode the player goes around the "
                                             f"{MIXER} mixer, straight to the device", "Muted.TLabel")
                self._refit()
        elif not exclusive and self.windows_note.cget("text").startswith("Not needed"):
            self._say(self.windows_note, "")
            self._refit()

    def _draw_meter(self, levels: tuple[float, ...]):
        c = self.meter
        c.delete("all")
        width, height = int(c.cget("width")), int(c.cget("height"))
        channels = len(levels) or 2
        names = ["L", "R"] if channels == 2 else [str(i + 1) for i in range(channels)]
        gap = max(2, theme.px(3))
        bar_h = max(4, (height - gap * (channels - 1)) // channels)
        label_w, text_w = theme.px(16), theme.px(70)
        bar_w = width - label_w - text_w

        if len(self._shown_db) != channels:
            self._shown_db = [METER_FLOOR_DB] * channels
        for ch in range(channels):
            db = to_db(levels[ch]) if levels else float("-inf")
            shown = max(db, self._shown_db[ch] - METER_FALL_DB, METER_FLOOR_DB)
            self._shown_db[ch] = shown
            y = ch * (bar_h + gap)
            c.create_text(0, y + bar_h / 2, text=names[ch], anchor="w", fill=theme.MUTED,
                          font=theme.font(theme.SIZE_BODY))
            # (0.22.0) The empty part in BORDER, like the seek bar's track (SURFACE all but vanished in Light).
            c.create_rectangle(label_w, y, label_w + bar_w, y + bar_h, fill=theme.BORDER, width=0)
            fill = (shown - METER_FLOOR_DB) / -METER_FLOOR_DB
            colour = theme.DANGER if shown > -0.1 else theme.WARN if shown > -6 else theme.ACCENT
            if fill > 0:
                c.create_rectangle(label_w, y, label_w + bar_w * fill, y + bar_h, fill=colour, width=0)
            reading = "—" if db == float("-inf") else theme.figures(f"{db:.1f} dB")
            c.create_text(width, y + bar_h / 2, text=reading, anchor="e", fill=theme.TEXT,
                          font=theme.numbers(theme.SIZE_BODY))

    def close(self):
        self._cancel.set()
        self._album_cancel.set()
        self._rip_cancel.set()
        if self._job:
            self.win.after_cancel(self._job)
        self.win.destroy()
