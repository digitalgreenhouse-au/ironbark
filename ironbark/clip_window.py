"""The Clipping window (key C): see — and, as an experiment, hear — inter-sample overs.

- Live: the moment just heard where the music came closest to (or past) full scale. Dots are the
  file's samples; the line is the wave a converter rebuilds from them (8 points per sample, the
  same filter as the true-peak measures), red where it passes full scale (the dashed lines). An
  over stays on show for a second, so it can be seen. Zoom: 50 ms, 5 ms or 30 samples across;
  Freeze holds the picture. Readouts over the last second heard: highest sample, true peak, overs.
- Whole song: every over in the track along a strip (taller = further over), read in the
  background with the same filter; the worst marked. Click a mark to choose it and hear it from a
  second before. Summary: "37 overs, worst +1.9 dB at 2:14".
- Hear it (Settings → Experimental → Hear clipping, off by default): loops a few seconds around
  the chosen over and switches, at the same sample, between A = an ordinary converter (rebuilt,
  chopped at full scale, brought back down) and B = clean (the same loop turned down just below
  its true peak), loudness-matched; or just the difference (what the chop takes away, boosted).
  Uses audition.py like the ABX test: the player pauses and lets go of the device while you
  listen, and plays on the player's output at its volume. Not bit-perfect, and it says so. Another
  window that plays its own sound (ABX, Test signals) stops this one first, and the other way round.

The maths is in clipping.py; this file only shows it.
"""

from __future__ import annotations

import collections
import logging
import math
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

import numpy as np
from PIL import Image, ImageDraw, ImageTk

from . import audition, clipping, controls, experiments, placement, theme, toolwin
from .engine import State

log = logging.getLogger(__name__)

EXPERIMENT = "clip_audition"
REFRESH_MS = 100
HOLD_S = 1.0             # an over stays on show this long before a smaller moment replaces it
RECENT_S = 3.0           # sound kept for drawing (heard and about to be heard)
READOUT_S = 1.0          # the readouts cover the last second heard
PRE_ROLL_S = 1.0         # clicking a mark plays from this long before the over
ZOOMS = (("wide", "50 ms"), ("mid", "5 ms"), ("close", "30 samples"))
ZOOM_SAMPLES = {"close": 30}
ZOOM_SECONDS = {"wide": 0.050, "mid": 0.005}
DOT_SPACING = 5          # px at 100%: draw the samples as dots only when they're at least this far apart
STRIP_HEIGHT = 64        # px at 100%: the whole-song strip
MARK_REACH = 6           # px at 100%: a click this close to a mark chooses it
SLOTS = {"A": 0, "B": 1, "D": 2}
CONTEXT = 48             # samples kept either side of the view, for the filter
TOP_SAMPLE = 1.0 - 2.0 ** -15   # a sample this high is at full scale (16-bit's top value or above)


def fmt_ms(seconds: float) -> str:
    """2:14.381"""
    seconds = max(0.0, seconds)
    return f"{int(seconds // 60)}:{seconds % 60:06.3f}"


def fmt_db(value: float, unit: str) -> str:
    if not math.isfinite(value):
        return f"– {unit}"
    return theme.figures(f"{value:+.2f} {unit}")


class ClipWindow:
    def __init__(self, parent: tk.Misc, player, prefs: dict, on_close=lambda: None, stream_factory=None):
        self.player, self.prefs, self.on_close = player, prefs, on_close
        self._stream_factory = stream_factory      # tests: a stand-in for the sound device
        self.win = tk.Toplevel(parent, bg=theme.BG)
        self.win.title("Clipping")
        self.win.geometry(f"{theme.px(900)}x{theme.px(720)}")
        self.win.minsize(theme.px(520), theme.px(440))
        placement.restore(self.win, prefs.get("clip_geometry", ""))
        theme.dark_title_bar(self.win)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<Escape>", lambda e: self.close())

        outer = ttk.Frame(self.win, padding=theme.pad(10, 8, 10, 10))
        outer.pack(fill="both", expand=True)
        self.outer = outer
        bar = ttk.Frame(outer)
        bar.pack(fill="x")
        zoom = prefs.get("clip_zoom", "wide")
        self.zoom = tk.StringVar(value=zoom if zoom in dict(ZOOMS) else "wide")
        self.zoom_choice = controls.ChoiceRow(bar, ZOOMS, self.zoom, command=self._zoom_changed)
        self.zoom_choice.pack(side="left")
        self.freeze_button = controls.FlatButton(bar, "Freeze", self._toggle_freeze, fit="Resume",
                                                 label="Hold the picture still")
        self.freeze_button.pack(side="right")
        self.readout = ttk.Label(outer, text="", style="Number.TLabel", anchor="w")
        self.readout.pack(fill="x", pady=theme.pad(theme.GAP, 0))
        self.info = ttk.Label(outer, text="", style="Muted.TLabel", justify="left")
        self.info.pack(fill="x")
        # The whole song and Hear it sit along the bottom; the live picture takes the room left
        # (so a short window shrinks the picture, never cuts off the text below it).
        lower = ttk.Frame(outer)
        lower.pack(side="bottom", fill="x")
        self.canvas = tk.Canvas(outer, bg=theme.BG, highlightthickness=0, height=theme.px(220))
        self.canvas.pack(fill="both", expand=True, pady=theme.pad(theme.GAP, 0))
        self.canvas.bind("<Configure>", lambda e: self._draw_live(force=True))

        controls.section(lower, "Whole song")
        self.summary = ttk.Label(lower, text="", justify="left")
        self.summary.pack(fill="x")
        self.strip = tk.Canvas(lower, bg=theme.BG, highlightthickness=0,
                               height=theme.px(STRIP_HEIGHT) + theme.px(16))
        self.strip.pack(fill="x", pady=theme.pad(theme.GAP_SMALL, 0))
        self.strip.bind("<Configure>", lambda e: self._draw_strip(force=True))
        self.strip.bind("<Button-1>", self._strip_click)
        self.chosen_label = ttk.Label(lower, text="", style="Muted.TLabel", justify="left")
        self.chosen_label.pack(fill="x")
        self.listen_holder = ttk.Frame(lower)     # the experiment's section goes in here when it's on
        self.listen_holder.pack(fill="x")
        outer.bind("<Configure>", self._rewrap)

        # Live data
        self.feed = player.monitor()
        self.ring: collections.deque = collections.deque()   # (frame, float samples) in order, this stream
        self._ring_frames = 0
        self._stream_key = None        # (path, rate, channels) of the sound in the ring
        self._next_frame = None        # where the next block should start (else: a seek)
        self._waiting: collections.deque = collections.deque()  # (block, float samples) not yet heard
        self.finder: clipping.OverFinder | None = None
        self._recent: collections.deque = collections.deque()  # (seconds, block true peak, block sample peak)
        self.heard_to = 0.0            # seconds into the track: the latest sound heard
        self.shown: dict | None = None  # the moment on show (see _choose_moment)
        self.frozen = False
        self._live_key = None
        self._live_photo = None

        # Whole song
        self.scan_path = None
        self.scan_result: clipping.OverScan | None = None
        self._scan_progress = 0.0
        self._scan_failed = False
        self._scan_cancel = threading.Event()
        self.chosen: clipping.Over | None = None
        self._strip_key = None
        self._strip_photo = None
        self._strip_plot = (0, 0, 1, 1)

        # Hear it (Experimental)
        self.listen_frame: ttk.Frame | None = None
        self.audition: audition.Audition | None = None
        self.versions: clipping.Versions | None = None
        self._versions_for = None      # (path, over time) the versions were made for
        self._loading: threading.Thread | None = None
        self._loaded = None            # ((path, over time), Versions or why not), from the thread
        self._last: str | None = None
        self._message = ""
        for key, letter in (("a", "A"), ("b", "B"), ("d", "D")):
            self.win.bind(key, lambda e, l=letter: self.hear(l))
        self.win.bind("<space>", lambda e: self.stop() if self.playing else self.hear(self._last or "A"))

        self._keep_going = toolwin.KeepGoing(self.win)
        theme.on_change(self._palette_changed)
        self.win.bind("<Configure>", self._moved, add="+")
        self._sync_listen()
        self._job = self.win.after(REFRESH_MS, self._tick)

    # -- live --------------------------------------------------------------------------------
    def _drain(self) -> bool:
        """Take in the feed; feed the over-finder everything now heard. True if anything was heard."""
        try:
            while True:
                block = self.feed.get_nowait()
                self._receive(block)
        except queue.Empty:
            pass
        now = time.perf_counter()
        heard = False
        best = None
        while self._waiting and self._waiting[0][0]["dac_time"] <= now:
            block, x = self._waiting.popleft()
            if self.finder is None:
                continue
            self.finder.add(x)
            heard = True
            end = (block["frame"] + len(x)) / block["rate"]
            self.heard_to = max(self.heard_to, end)
            self._recent.append((end, self.finder.last_peak, self.finder.last_sample))
            if best is None or self.finder.last_peak > best[0]:
                best = (self.finder.last_peak, self.finder.last_peak_at)
        if self.finder is not None:
            self.finder.settle()
            # Keep only the last few seconds' overs (the readout counts the last one).
            cutoff = self.heard_to - 5 * READOUT_S
            if self.finder.overs and self.finder.overs[0].at < cutoff:
                self.finder.overs = [o for o in self.finder.overs if o.at >= cutoff]
        while self._recent and self._recent[0][0] < self.heard_to - READOUT_S:
            self._recent.popleft()
        if best is not None and not self.frozen:
            self._choose_moment(*best)
        return heard

    def _receive(self, block):
        x = clipping_float(block["source"])
        key = (block.get("path"), block["rate"], x.shape[1])
        if key != self._stream_key or block["frame"] != self._next_frame:
            # A new track, a new format, or a seek: start again from this block.
            self._stream_key = key
            self.ring.clear()
            self._ring_frames = 0
            self._waiting.clear()
            self._recent.clear()
            self.finder = clipping.OverFinder(x.shape[1], block["rate"], start_frame=block["frame"])
            self.heard_to = block["frame"] / block["rate"]
        self._next_frame = block["frame"] + len(x)
        self.ring.append((block["frame"], x))
        self._ring_frames += len(x)
        self._waiting.append((block, x))
        keep = RECENT_S * block["rate"]
        while len(self.ring) > 1 and self._ring_frames - len(self.ring[0][1]) >= keep:
            self._ring_frames -= len(self.ring.popleft()[1])

    def _samples(self, start: int, end: int) -> tuple[int, np.ndarray] | None:
        """(first frame, samples) of what the ring holds in [start, end)."""
        if not self.ring:
            return None
        first = self.ring[0][0]
        x = np.concatenate([piece for _, piece in self.ring])
        lo, hi = max(start, first), min(end, first + len(x))
        if hi <= lo:
            return None
        return lo, x[lo - first: hi - first]

    def _choose_moment(self, peak: float, at: float):
        """Show the moment around `at` (seconds) — unless an over on show is still being held
        and this one is smaller."""
        shown = self.shown
        if shown is not None and shown["peak"] > 1.0 and time.monotonic() - shown["when"] < HOLD_S \
                and peak <= shown["peak"]:
            return
        rate = self._stream_key[1] if self._stream_key else 0
        if not rate:
            return
        centre = int(round(at * rate))
        half = int(ZOOM_SECONDS["wide"] * rate / 2) + CONTEXT
        got = self._samples(centre - half, centre + half)
        if got is None:
            return
        first, x = got
        self.shown = {"first": first, "samples": x, "rate": rate, "at": at, "peak": peak,
                      "when": time.monotonic(), "track": self._stream_key[0]}
        self._live_key = None

    def _readouts(self) -> str:
        if not self._recent:
            return "Highest sample –   ·   True peak –   ·   Overs –"
        tp = max(r[1] for r in self._recent)
        sp = max(r[2] for r in self._recent)
        overs = 0
        if self.finder is not None:
            overs = len(clipping.overs_between(self.finder.overs, self.heard_to - READOUT_S, self.heard_to + 1))
            if self.finder._open is not None:
                overs += 1
        return (f"Last second:   highest sample {fmt_db(clipping.db(sp), 'dBFS')}   ·   "
                f"true peak {fmt_db(clipping.db(tp), 'dBTP')}   ·   overs {overs}")

    def _zoom_changed(self):
        self.prefs["clip_zoom"] = self.zoom.get()
        self._draw_live(force=True)

    def _toggle_freeze(self):
        self.frozen = not self.frozen
        self.freeze_button.configure(text="Resume" if self.frozen else "Freeze")
        self._draw_live(force=True)

    def _view(self):
        """(first frame, samples, rebuilt wave, factor, view start, view end) of the moment on show,
        cut to the zoom (in frames, floats)."""
        shown = self.shown
        rate = shown["rate"]
        centre = shown["at"] * rate
        zoom = self.zoom.get()
        half = ZOOM_SAMPLES[zoom] / 2 if zoom in ZOOM_SAMPLES else ZOOM_SECONDS[zoom] * rate / 2
        lo, hi = centre - half, centre + half
        first, x = shown["first"], shown["samples"]
        a = max(0, int(math.floor(lo)) - CONTEXT - first)
        b = min(len(x), int(math.ceil(hi)) + CONTEXT + 1 - first)
        piece = x[a:b]
        up = clipping.oversample(piece)
        return first + a, piece, up, clipping.FACTOR, lo, hi

    def _draw_live(self, force: bool = False):
        c = self.canvas
        w, h = c.winfo_width(), c.winfo_height()
        if w < 20 or h < 20:
            return
        key = (id(self.shown), self.zoom.get(), w, h, theme.current.name)
        if not force and key == self._live_key:
            return
        self._live_key = key
        c.delete("all")
        if self.shown is None:
            c.create_rectangle(1, 1, w - 1, h - 1, outline=theme.BORDER)
            self.info.configure(text="Nothing heard yet. Start the music: the loudest moment just heard shows "
                                     "here, an over (the wave past full scale) in red.")
            return
        first, x, up, factor, lo, hi = self._view()
        channels = x.shape[1]
        left, right, top, bottom = theme.px(22), theme.px(8), theme.px(4), theme.px(18)
        pw, ph = max(10, w - left - right), max(10, h - top - bottom)
        lane = ph / channels
        times = first + np.arange(len(up)) / factor          # frames
        visible = (times >= lo) & (times <= hi)
        peak = float(np.abs(up[visible]).max()) if visible.any() else 1.0
        ymax = min(3.0, max(1.25, peak * 1.06))
        image = Image.new("RGB", (pw, int(ph)), theme.rgb(theme.BG))
        draw = ImageDraw.Draw(image)
        span = max(hi - lo, 1e-9)

        def px_x(frame):
            return (np.asarray(frame) - lo) / span * (pw - 1)

        dash = max(3, theme.px(4))
        for ch in range(channels):
            y0 = ch * lane
            mid = y0 + lane / 2

            def px_y(v, mid=mid):
                return mid - np.asarray(v) / ymax * (lane / 2 - 2)

            draw.line([(0, mid), (pw, mid)], fill=theme.rgb(theme.BORDER))
            for level in (1.0, -1.0):                       # full scale, dashed
                y = float(px_y(level))
                for x0 in range(0, pw, dash * 2):
                    draw.line([(x0, y), (min(pw, x0 + dash), y)], fill=theme.rgb(theme.MUTED))
            if ch:
                draw.line([(0, y0), (pw, y0)], fill=theme.rgb(theme.BORDER))
            wave = up[:, ch]
            xs, ys = px_x(times), px_y(wave)
            inside = (xs >= -2) & (xs <= pw + 2)
            pts = list(zip(xs[inside].tolist(), ys[inside].tolist()))
            if len(pts) > 1:
                draw.line(pts, fill=theme.rgb(theme.TEXT), width=max(1, theme.px(1.5)))
            over = inside & (np.abs(wave) > 1.0)
            if over.any():                                   # the parts past full scale, red
                idx = np.flatnonzero(over)
                runs = np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1)
                for run in runs:
                    if len(run) > 1:
                        draw.line(list(zip(xs[run].tolist(), ys[run].tolist())), fill=theme.rgb(theme.DANGER),
                                  width=max(2, theme.px(2.5)))
                    else:
                        r = theme.px(1.5)
                        draw.ellipse([xs[run[0]] - r, ys[run[0]] - r, xs[run[0]] + r, ys[run[0]] + r],
                                     fill=theme.rgb(theme.DANGER))
            spacing = (pw - 1) / span
            if spacing >= theme.px(DOT_SPACING):          # the samples themselves, as dots
                r = max(2, theme.px(2.5))
                frames = first + np.arange(len(x))
                sx, sy = px_x(frames), px_y(x[:, ch])
                for fx, fy, v in zip(sx.tolist(), sy.tolist(), x[:, ch].tolist()):
                    if -r <= fx <= pw + r:
                        colour = theme.DANGER if abs(v) >= TOP_SAMPLE else theme.ACCENT
                        draw.ellipse([fx - r, fy - r, fx + r, fy + r], fill=theme.rgb(colour))
        self._live_photo = ImageTk.PhotoImage(image, master=self.win)
        c.create_image(left, top, image=self._live_photo, anchor="nw")
        small = theme.font(theme.SIZE_SMALL)
        names = ["L", "R"] if channels == 2 else [str(k + 1) for k in range(channels)]
        for ch in range(channels):
            mid = top + ch * lane + lane / 2
            scale = (lane / 2 - 2) / ymax
            c.create_text(left + pw - theme.px(4), mid - scale - 1, text="full scale", anchor="se",
                          fill=theme.MUTED, font=small)
            c.create_text(theme.px(6), top + ch * lane + theme.px(2), text=names[ch], anchor="nw",
                          fill=theme.MUTED, font=small)
        rate = self.shown["rate"]
        across = (hi - lo) / rate
        label = f"{across * 1000:.1f} ms across" if self.zoom.get() == "close" else f"{across * 1000:.0f} ms across"
        c.create_text(left, h - 2, text=f"{fmt_ms(lo / rate)}", anchor="sw", fill=theme.MUTED, font=small)
        c.create_text(left + pw, h - 2, text=f"{fmt_ms(hi / rate)}", anchor="se", fill=theme.MUTED, font=small)
        c.create_text(left + pw / 2, h - 2, text=label, anchor="s", fill=theme.MUTED, font=small)
        over = self.shown["peak"] > 1.0
        state = (f"Frozen at {fmt_ms(self.shown['at'])} — Resume to follow the music. " if self.frozen
                 else "The loudest moment just heard (an over stays a second). ")
        what = (f"Its rebuilt wave reaches {fmt_db(clipping.db(self.shown['peak']), 'dB')} — "
                f"{'past' if over else 'within'} full scale. ")
        dots = "Dots: the file's samples. " if (pw - 1) / span >= theme.px(DOT_SPACING) else \
            "Zoom in to see the samples as dots. "
        self.info.configure(text=f"{state}{what}{dots}Line: the wave a converter rebuilds from them (8×), red "
                                 "where it passes full scale (dashed).")

    # -- whole song ----------------------------------------------------------------------------
    def _start_scan(self, path):
        self._scan_cancel.set()
        self._scan_cancel = cancel = threading.Event()
        self.scan_path, self.scan_result, self.chosen = path, None, None
        self._scan_progress, self._scan_failed = 0.0, False
        self._strip_key = None
        # Hear it: the last song's loop goes with it (one still being made is dropped when it's ready).
        self._stop_audition()
        self.audition = self.versions = self._versions_for = None
        self._message = ""
        if path is None:
            return

        def work():
            try:
                result = clipping.scan(path, cancel, progress=lambda p: setattr(self, "_scan_progress", p))
            except (OSError, RuntimeError, ValueError):
                result = None
                self._scan_failed = not cancel.is_set()
            except Exception:  # noqa: BLE001 — said in the window ("couldn't be read"), never "Reading…" for ever
                log.exception("reading the whole song for overs")
                result = None
                self._scan_failed = not cancel.is_set()
            if result is not None and not cancel.is_set():
                self.scan_result = result

        threading.Thread(target=work, name="clip-scan", daemon=True).start()

    def _strip_geometry(self):
        w = self.strip.winfo_width()
        left, right = theme.px(6), theme.px(6)
        return left, theme.px(2), max(10, w - left - right), theme.px(STRIP_HEIGHT)

    def _draw_strip(self, force: bool = False):
        c = self.strip
        result = self.scan_result
        if result is None:
            if self._scan_failed:
                text = "This file couldn't be read."
            elif self.scan_path is not None:
                text = f"Reading the whole song… {self._scan_progress:.0%}"
            else:
                text = "Nothing playing."
            if self._strip_key is not None or self.summary.cget("text") != text:   # (only when it changed)
                c.delete("all")
                self._strip_key = None
                self.summary.configure(text=text, style="Muted.TLabel")
                self.chosen_label.configure(text="")
            return
        x0, y0, width, height = self._strip_geometry()
        key = (id(result), id(self.chosen), width, height, theme.current.name)
        if force or key != self._strip_key:
            self._strip_key = key
            c.delete("all")
            image = Image.new("RGB", (width, height), theme.rgb(theme.SURFACE))
            draw = ImageDraw.Draw(image)
            worst = result.worst
            top_db = max(1.0, worst.db if worst else 1.0)
            duration = max(result.duration, 1e-9)
            for over in result.overs:
                x = int(over.at / duration * (width - 1))
                tall = max(theme.px(3), int(min(1.0, over.db / top_db) * (height - theme.px(10))))
                draw.line([(x, height - 1), (x, height - 1 - tall)], fill=theme.rgb(theme.DANGER),
                          width=max(1, theme.px(1)))
            for over, colour in ((worst, theme.DANGER), (self.chosen, theme.ACCENT)):
                if over is None:
                    continue
                x = int(over.at / duration * (width - 1))
                r = theme.px(4)
                draw.polygon([(x - r, 0), (x + r, 0), (x, r + 1)], fill=theme.rgb(colour))
                if over is self.chosen:
                    draw.line([(x, 0), (x, height - 1)], fill=theme.rgb(theme.ACCENT), width=max(1, theme.px(1)))
            self._strip_photo = ImageTk.PhotoImage(image, master=self.win)
            c.create_image(x0, y0, image=self._strip_photo, anchor="nw")
            small = theme.font(theme.SIZE_SMALL)
            step = next((s for s in (10, 15, 30, 60, 120, 300, 600) if duration / s <= 10), 1200)
            for t in range(0, int(duration) + 1, step):
                x = x0 + width * t / duration
                c.create_text(x, y0 + height + 1, text=clipping.fmt_clock(t), anchor="nw" if t == 0 else "n",
                              fill=theme.MUTED, font=small)
            tp, sp = clipping.db(result.true_peak), clipping.db(result.highest_sample)
            figures = ((f" · true peak {fmt_db(tp, 'dBTP')}" if result.overs else "")
                       + f" · highest sample {fmt_db(sp, 'dBFS')} (8× oversampling)")
            damaged = (f" ⚠ The file stops decoding at {clipping.fmt_clock(result.damaged_at)}."
                       if result.damaged_at is not None else "")
            self.summary.configure(text=theme.figures(clipping.summary(result)) + figures + damaged,
                                   style="WarnValue.TLabel" if result.overs else "GoodValue.TLabel")
            self._show_chosen()
        self._draw_position()

    def _show_chosen(self):
        over = self.chosen
        if over is None:
            text = "Click a mark to choose that over and hear it from a second before." if \
                self.scan_result and self.scan_result.overs else ""
        else:
            worst = " (the worst)" if self.scan_result and over is self.scan_result.worst else ""
            length = max(over.end - over.start, 0.0) * 1000
            text = (f"Chosen{worst}: {fmt_ms(over.at)}, {fmt_db(over.db, 'dB')} over full scale, for "
                    f"{length:.1f} ms. Click another mark to choose it.")
        self.chosen_label.configure(text=text)

    def _draw_position(self):
        c = self.strip
        c.delete("position")
        result = self.scan_result
        status = self.player.status()
        if result is None or status.track is None or status.track.path != self.scan_path:
            return
        x0, y0, width, height = self._strip_geometry()
        x = x0 + width * min(1.0, status.position / max(result.duration, 1e-9))
        c.create_line(x, y0, x, y0 + height, fill=theme.TEXT, tags="position")

    def over_near(self, x: float) -> clipping.Over | None:
        """The over whose mark is nearest x (canvas px), if one is within reach."""
        result = self.scan_result
        if result is None or not result.overs:
            return None
        x0, _, width, _ = self._strip_geometry()
        duration = max(result.duration, 1e-9)
        ats = np.array([o.at for o in result.overs])
        marks = x0 + ats / duration * (width - 1)
        k = int(np.argmin(np.abs(marks - x)))
        return result.overs[k] if abs(marks[k] - x) <= theme.px(MARK_REACH) else None

    def _strip_click(self, event):
        over = self.over_near(event.x)
        if over is not None:
            self.choose(over, seek=True)

    def choose(self, over: clipping.Over, seek: bool = False):
        """Make `over` the chosen one (the loop's centre); with `seek`, play from just before it."""
        self.chosen = over
        self._draw_strip(force=True)
        if seek:
            status = self.player.status()
            if status.track is not None and status.track.path == self.scan_path:
                self.player.seek(max(0.0, over.at - PRE_ROLL_S))
                if self.frozen:
                    self._toggle_freeze()
        self._prepare_versions()

    # -- hear it (Experimental) ----------------------------------------------------------------
    @property
    def listening_on(self) -> bool:
        return experiments.enabled(self.prefs, EXPERIMENT)

    @property
    def playing(self) -> bool:
        return self.audition is not None and self.audition.playing

    def _sync_listen(self):
        """Build the Hear it section when the experiment is on; take it away (and stop any sound)
        when it's off. Off = no listening controls and nothing ever played."""
        on = self.listening_on
        if on and self.listen_frame is None:
            frame = ttk.Frame(self.listen_holder)
            frame.pack(fill="x")
            controls.section(frame, "Hear it (Experimental)")
            row = ttk.Frame(frame)
            row.pack(fill="x")
            self.listen_buttons = {}
            for letter, text, tip in (("A", "A · ordinary DAC", "An ordinary converter: the wave chopped at full "
                                       "scale (key a)"),
                                      ("B", "B · clean", "The same loop turned down just below its peak: nothing "
                                       "chopped (key b)"),
                                      ("D", "Just the difference", "Only what the chop takes away, 20 dB louder "
                                       "(key d)")):
                button = controls.FlatButton(row, text, lambda l=letter: self.hear(l), label=tip,
                                             fit=f"▶ {text}")
                button.pack(side="left", padx=theme.pad(0, theme.GAP))
                self.listen_buttons[letter] = button
            controls.FlatButton(row, "■ Stop", self.stop, label="Stop (space)").pack(side="left")
            self.level_label = ttk.Label(frame, text="", justify="left")
            self.level_label.pack(fill="x", pady=theme.pad(theme.GAP, 0))
            self.hearing = ttk.Label(frame, text="", style="Muted.TLabel", justify="left")
            self.hearing.pack(fill="x")
            self.listen_note = ttk.Label(frame, style="Muted.TLabel", justify="left", text=(
                f"Loops {clipping.LOOP_S:.0f} s around the chosen over. A simulates a converter with no room above "
                "full scale: the wave rebuilt at 8×, chopped flat at full scale, brought back down. B is the "
                "same loop, turned down just below its true peak so nothing is chopped. Levels are matched, and "
                "switching dips the sound the same way every time. Plays on the player's output at its volume; "
                "the player pauses while you listen (press play there to carry on). Not bit-perfect."))
            self.listen_note.pack(fill="x", pady=theme.pad(theme.GAP_SMALL, 0))
            self.listen_frame = frame
            self._listen_shown = None
            self._rewrap()
            self._prepare_versions()
            self._refresh_listen()
        elif not on and self.listen_frame is not None:
            self._stop_audition()
            self.audition, self.versions, self._versions_for = None, None, None
            self.listen_frame.destroy()
            self.listen_frame = None

    def _prepare_versions(self):
        """Make A / B / difference for the chosen over (the worst, until another is chosen), on a thread."""
        if self.listen_frame is None or self.scan_result is None:
            return
        over = self.chosen or self.scan_result.worst
        if over is None:
            return
        want = (self.scan_path, over.at)
        if want == self._versions_for or self._loading is not None:
            return
        self._versions_for = want
        path = self.scan_path

        def work():
            try:
                result = clipping.listening_versions(path, over.at)
            except (OSError, RuntimeError, ValueError) as exc:
                result = f"Couldn't read the loop: {exc}"
            except Exception as exc:  # noqa: BLE001 — said in the window; another over can still be tried
                log.exception("making the clipping A/B loop")
                result = f"Couldn't make the loop: {exc}"
            self._loaded = (want, result)

        self._loading = threading.Thread(target=work, name="clip-versions", daemon=True)
        self._loading.start()

    def _take_versions(self):
        (want, loaded), self._loaded = self._loaded, None
        self._loading = None
        if want[0] != self.scan_path:     # made for the song before: not wanted any more
            self._prepare_versions()
            return
        if isinstance(loaded, str):
            self._message = loaded
            return
        was_playing = self.playing
        self._stop_audition()
        self.versions = loaded
        switcher = audition.Switcher([loaded.a, loaded.b, loaded.difference], loaded.rate,
                                     gains=[loaded.gain_a, loaded.gain_b, loaded.gain_difference],
                                     volume=self.player.volume ** 2)
        self.audition = audition.Audition(switcher, device=self.player.output_device,
                                          exclusive=self.player.exclusive, stream_factory=self._stream_factory)
        self._message = ("The loop moved to the chosen over: press A or B" if was_playing else "")
        # Another over was chosen while this one was being made: make that one now.
        self._prepare_versions()

    def hear(self, letter: str):
        if self.listen_frame is None or self.audition is None:
            return
        self._last, self._message = letter, ""
        self.audition.switcher.volume = self.player.volume ** 2
        if not self.playing:
            toolwin.take_sound(self)   # another window's sound stops first
            self.player.suspend()      # the player pauses and lets go of the device while you listen
            try:
                self.audition.play(SLOTS[letter])
            except Exception as exc:  # noqa: BLE001 — a device that won't open is a message, not a crash
                self._message = f"Can't open the sound device ({exc})"
        else:
            self.audition.play(SLOTS[letter])
        self._refresh_listen()

    def _stop_audition(self):
        if self.audition is not None:
            self.audition.stop()

    def stop(self):
        self._stop_audition()
        self._refresh_listen()

    def sound_taken(self):
        """Another window started its own sound: this one stops, and says why."""
        if self.playing:
            self._stop_audition()
            self._message = "Stopped: another window is playing"
            self._refresh_listen()

    def describe_levels(self) -> tuple[str, bool]:
        """(text, fine?) about A's and B's levels as heard."""
        v = self.versions
        if v is None:
            return "", True
        if v.level_a is None or v.level_b is None:
            return "Levels: couldn't be measured (silence?)", True
        down_b = -20 * math.log10(v.gain_b) if v.gain_b > 0 else 0.0
        figures = theme.figures(f"A {v.level_a:.2f}, B {v.level_b:.2f} {v.level_unit}")
        if abs(v.matched_db) > 0.1:
            return (f"⚠ A and B differ by {abs(v.matched_db):.2f} dB as heard ({figures}): a level "
                    "difference alone is audible"), False
        return (f"✓ Levels matched ({figures}). B is turned down {down_b:.2f} dB to sit just under full scale "
                f"(this loop's true peak {fmt_db(clipping.db(v.true_peak), 'dBTP')}); A was "
                f"{abs(v.level_before_db):.2f} dB {'louder' if v.level_before_db >= 0 else 'quieter'}, so it's "
                "turned down to match"), True

    def _refresh_listen(self):
        """Bring the Hear it section up to date (Tk is only touched when something changed: the
        loop is made on a thread that holds Python for long stretches, so every Tk call costs)."""
        if self.listen_frame is None:
            return
        ready = self.audition is not None
        buttons = tuple(f"▶ {text}" if self.playing and self._last == letter else text
                        for letter, text in (("A", "A · ordinary DAC"), ("B", "B · clean"),
                                             ("D", "Just the difference")))
        if self.versions is not None:
            text, fine = self.describe_levels()
            level = (text, "GoodValue.TLabel" if fine else "WarnValue.TLabel")
        elif self._loading is not None:
            level = ("Making the loop…", "Muted.TLabel")
        elif self.scan_result is not None and not self.scan_result.overs:
            level = ("No overs in this song, so nothing to hear.", "Muted.TLabel")
        else:
            level = ("Waiting for the whole-song reading…", "Muted.TLabel")
        if self.playing:
            where = self.audition.device_name
            mode = "bit-perfect mode" if self.audition.exclusive else "shared mode"
            what = {"A": "A, the ordinary DAC", "B": "B, clean", "D": "just the difference (20 dB louder)"}
            at = fmt_ms(self.versions.start) if self.versions else ""
            hearing = (f"Hearing {what[self._last or 'A']} · loop from {at} · {where} ({mode}) · "
                       f"volume {self.player.volume:.0%}")
        else:
            hearing = " · ".join(n for n in (self._message, self.audition.note if self.audition else "") if n)
        state = (ready, buttons, level, hearing)
        if state == getattr(self, "_listen_shown", None):
            return
        self._listen_shown = state
        for button, text in zip(self.listen_buttons.values(), buttons):
            button.state(["!disabled"] if ready else ["disabled"])
            button.configure(text=text)
        self.level_label.configure(text=level[0], style=level[1])
        self.hearing.configure(text=hearing)

    # -- the loop ------------------------------------------------------------------------------
    def _tick(self):
        self._job = None
        if not self.win.winfo_exists():
            return
        with self._keep_going:
            self._step()
        self._job = self.win.after(REFRESH_MS, self._tick)

    def _step(self):
        self._drain()
        status = self.player.status()
        path = status.track.path if status.track is not None else None
        if path != self.scan_path:
            self._start_scan(path)
        if toolwin.on_screen(self.win):   # minimised: the overs are still found, nothing is drawn
            self._draw_live()
            readout = self._readouts()
            if self.readout.cget("text") != readout:
                self.readout.configure(text=readout)
            self._draw_strip()
        self._sync_listen()
        self._prepare_versions()     # (does nothing unless the experiment is on and the loop needs making)
        if self._loaded is not None:
            self._take_versions()
        if self.playing:
            self.audition.switcher.volume = self.player.volume ** 2
            if status.state is State.PLAYING:   # play was pressed in the player
                self._stop_audition()
                self._message = "Stopped: the player started playing again"
        self._refresh_listen()

    # -- window --------------------------------------------------------------------------------
    def _palette_changed(self, _palette=None):
        self._live_key = self._strip_key = None

    def _rewrap(self, event=None):
        width = max(200, self.outer.winfo_width() - theme.px(24))
        labels = [self.info, self.summary, self.chosen_label]
        if self.listen_frame is not None:
            labels += [self.level_label, self.hearing, self.listen_note]
        for label in labels:
            label.configure(wraplength=width)

    def _moved(self, event):
        if event.widget is self.win:
            geometry = placement.current(self.win)
            if geometry:
                self.prefs["clip_geometry"] = geometry

    def lift(self):
        self.win.deiconify()
        self.win.lift()

    def close(self):
        self._scan_cancel.set()
        if self._job:
            self.win.after_cancel(self._job)
        self._stop_audition()
        toolwin.give_back_sound(self)
        self.player.unmonitor(self.feed)
        self._live_photo = self._strip_photo = None   # its pictures go with the window, on this thread
        self.win.destroy()
        self.on_close()


def clipping_float(source: np.ndarray) -> np.ndarray:
    """A feed block's file samples as float64 frames × channels (full scale = 1.0)."""
    x = source.astype(np.float64)
    if source.dtype == np.int32:
        x /= 2.0 ** 31
    return x[:, None] if x.ndim == 1 else x
