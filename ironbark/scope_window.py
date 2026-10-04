"""The Stereo scope window (key O): where the sound sits between the speakers.

- The scope (vectorscope / goniometer): every sample heard as a glowing dot that fades in a
  moment. Two views: Lissajous (the classic: mono is a vertical line, left-only and right-only
  lean 45°, opposite channels lie flat) and Polar (the same folded into a half circle, with
  the 45° lines marking left and right; outside them = out of phase).
- Correlation bar (-1…+1, last 600 ms), balance bar (which side is louder), and the DC offset
  of each channel since the track began.

The maths is in stereo.py; this file only draws. Fed by the file's own samples (before the
volume), so the picture is the same at any volume.
"""

from __future__ import annotations

import math
import time
import tkinter as tk
from tkinter import ttk

import numpy as np
from PIL import Image, ImageTk

from . import controls, placement, stereo, theme

REFRESH_MS = 33          # ~30 frames a second
FADE = 0.7               # how much of the glow is left after each frame (gone in ~1/3 s)
GLOW = 1.2               # how bright one dot is (brightness = 1 - e^(-GLOW x dots))
BALANCE_SPAN = 12.0      # dB at each end of the balance bar
VIEWS = (("lissajous", "Lissajous"), ("polar", "Polar"))


def _rgb(colour: str) -> np.ndarray:
    return np.array(theme.rgb(colour), dtype=np.float32)


def describe_correlation(reading: stereo.ScopeReading | None) -> str:
    if reading is None:
        return "Correlation: — (nothing playing)"
    now = "—" if reading.correlation is None else f"{reading.correlation:+.2f}"
    track = "—" if reading.track_correlation is None else f"{reading.track_correlation:+.2f}"
    return f"Correlation {now} (last 0.6 s) · this track so far {track}"


def describe_balance(reading: stereo.ScopeReading | None) -> str:
    if reading is None:
        return "Balance: —"
    if reading.channels == 1:
        return "Balance: a mono file — the same sound in both speakers"
    if reading.one_sided:
        return f"Balance: sound in the {reading.one_sided} channel only"
    if reading.balance_db is None:
        return "Balance: — (silence)"
    if abs(reading.balance_db) < 0.05:
        return "Balance: centred (left and right equally loud)"
    side = "left" if reading.balance_db > 0 else "right"
    return f"Balance: {side} louder by {abs(reading.balance_db):.1f} dB"


def describe_dc(reading: stereo.ScopeReading | None) -> tuple[str, bool]:
    """(text, worth a warning?) — each channel's DC offset this track so far."""
    if reading is None:
        return "DC offset: —", False
    levels = [stereo.dc_db(v) for v in reading.dc_offset[: max(1, min(2, reading.channels))]]
    names = ("L", "R") if reading.channels > 1 else ("",)
    parts = [f"{n} {'none' if v < -200 else f'{v:.0f} dBFS'}".strip() for n, v in zip(names, levels)]
    worst = max(levels)
    warn = worst >= stereo.DC_WARN_DBFS
    note = " ⚠ a constant push away from zero" if warn else ""
    return f"DC offset this track: {' · '.join(parts)}{note}", warn


class ScopeWindow:
    def __init__(self, parent: tk.Misc, player, prefs: dict, on_close=lambda: None):
        self.player, self.prefs, self.on_close = player, prefs, on_close
        self.win = tk.Toplevel(parent, bg=theme.BG)
        self.win.title("Stereo scope")
        self.win.geometry(f"{theme.px(420)}x{theme.px(640)}")
        self.win.minsize(theme.px(300), theme.px(420))
        placement.restore(self.win, prefs.get("scope_geometry", ""))
        theme.dark_title_bar(self.win)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<Escape>", lambda e: self.close())

        bar = ttk.Frame(self.win, padding=theme.pad(10, 8, 10, 4))
        bar.pack(fill="x")
        view = prefs.get("scope_view", "lissajous")
        self.view = tk.StringVar(value=view if view in dict(VIEWS) else "lissajous")
        # (0.22.0) A row of words (controls.ChoiceRow), as the other windows' choices.
        self.view_choice = controls.ChoiceRow(bar, VIEWS, self.view, command=self._view_changed)
        self.view_choice.pack(side="left")
        self.note = ttk.Label(self.win, style="Muted.TLabel", padding=theme.pad(10, 0, 10, 8),
                              text="The file's own samples (before the volume), as you hear them. Distance from "
                                   "the centre = level: edge 0 dBFS, inner ring -24, centre -48. Correlation, last "
                                   "600 ms: +1 the same in both channels, 0 unrelated, -1 opposite (cancels on a "
                                   "mono speaker).")
        self.note.pack(side="bottom", fill="x")
        self.note.bind("<Configure>", lambda e: self.note.configure(wraplength=max(200, e.width - 24)))
        self.texts = {}
        for key in ("dc", "balance", "correlation"):
            # (0.22.0) Live readings: fixed-width digits and a true minus sign, so they hold still.
            label = tk.Label(self.win, text="", bg=theme.BG, fg=theme.TEXT, font=theme.numbers(theme.SIZE_BODY),
                             anchor="w", justify="left", padx=theme.px(theme.GAP_LARGE))
            label.pack(side="bottom", fill="x")
            self.texts[key] = label
        self.canvas = tk.Canvas(self.win, bg=theme.BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=theme.px(10), pady=theme.pad(4, 6))
        self.canvas.bind("<Configure>", lambda e: self._layout_changed())

        self.feed = player.monitor()
        self.engine = stereo.ScopeEngine(self.feed)
        self.engine.start()
        self._glow: np.ndarray | None = None
        self._photo = None
        self._geometry = None      # (x, y, width, height, centre x, centre y, radius) of the picture
        self._last = time.perf_counter()
        self._bg, self._dot = _rgb(theme.BG), _rgb(theme.GOOD)
        theme.on_change(self._palette_changed)
        self.win.bind("<Configure>", self._moved, add="+")
        self._job = self.win.after(REFRESH_MS, self._tick)

    def _palette_changed(self, _palette):
        """The glow picture's colours are kept between frames: take the new ones."""
        self._bg, self._dot = _rgb(theme.BG), _rgb(theme.GOOD)

    # -- layout ---------------------------------------------------------------------
    def _bars_height(self) -> int:
        return theme.px(64)

    def _layout_changed(self):
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        room = max(40, h - self._bars_height())
        pad = theme.px(16)   # room for the L / R labels
        if self.view.get() == "polar":
            radius = max(10, min((w - 2 * pad) / 2, room - 2 * pad))
            width, height = int(2 * radius + 2), int(radius + 2)
        else:
            radius = max(10, min(w - 2 * pad, room - 2 * pad) / 2)
            width = height = int(2 * radius + 2)
        # The picture and the bars below it, together in the middle of the space.
        x, y = (w - width) // 2, max(pad, (h - height - self._bars_height()) // 2)
        cy = height - 1 if self.view.get() == "polar" else height / 2
        self._geometry = (x, y, width, height, width / 2, cy, radius)
        self._glow = np.zeros((height, width), dtype=np.float32)
        self._draw()

    def _view_changed(self):
        self.prefs["scope_view"] = self.view.get()
        self._layout_changed()

    # -- the picture ------------------------------------------------------------------
    def _add_points(self, x: np.ndarray):
        """Light up the dots for these sample pairs (frames x 2, full scale = 1.0)."""
        if self._glow is None or not len(x):
            return
        _, _, width, height, cx, cy, radius = self._geometry
        across, up = stereo.log_radius(*stereo.side_mid(x.astype(np.float64)))
        if self.view.get() == "polar":
            across, up = stereo.fold_up(across, up)
        cols = np.clip(np.round(cx + across * radius).astype(int), 0, width - 1)
        rows = np.clip(np.round(cy - up * radius).astype(int), 0, height - 1)
        counts = np.bincount(rows * width + cols, minlength=width * height)
        self._glow += counts.reshape(height, width).astype(np.float32)

    def _image(self):
        brightness = 1.0 - np.exp(-GLOW * self._glow)
        rgb = self._bg + brightness[..., None] * (self._dot - self._bg)
        return ImageTk.PhotoImage(Image.fromarray(rgb.astype(np.uint8)), master=self.win)

    def _tick(self):
        if not self.win.winfo_exists():
            return
        now = time.perf_counter()
        if self._glow is not None:
            self._glow *= FADE
            self._add_points(self.engine.samples_between(self._last, now))
        self._last = now
        reading = self.engine.reading_at(now)
        self._draw(reading)
        self._job = self.win.after(REFRESH_MS, self._tick)

    # -- drawing ------------------------------------------------------------------------
    def _draw(self, reading: stereo.ScopeReading | None = None):
        c = self.canvas
        c.delete("all")
        if self._geometry is None or self._glow is None:
            return
        x0, y0, width, height, cx, cy, radius = self._geometry
        self._photo = self._image()
        c.create_image(x0, y0, image=self._photo, anchor="nw")
        self._draw_guides(x0 + cx, y0 + cy, radius)
        self._draw_bars(reading, y0 + height + theme.px(14))
        self._draw_texts(reading)

    def _draw_guides(self, cx: float, cy: float, r: float):
        c = self.canvas
        small = theme.font(theme.SIZE_SMALL)
        guide = theme.BORDER
        polar = self.view.get() == "polar"
        if polar:
            c.create_arc(cx - r, cy - r, cx + r, cy + r, start=0, extent=180, style="arc", outline=guide)
            half = [(cx + r / 2 * math.cos(a), cy - r / 2 * math.sin(a)) for a in np.linspace(0, math.pi, 61)]
            c.create_line(half, fill=guide, dash=(2, 3))   # a line, not an arc: a dashed arc didn't show in testing
            c.create_line(cx - r, cy, cx + r, cy, fill=guide)
        else:
            c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=guide)
            c.create_oval(cx - r / 2, cy - r / 2, cx + r / 2, cy + r / 2, outline=guide, dash=(2, 3))
            c.create_line(cx - r, cy, cx + r, cy, fill=guide)
        c.create_line(cx, cy - r, cx, cy if polar else cy + r, fill=guide)
        d = r * math.sqrt(0.5)
        c.create_line(cx - d, cy - d, cx if polar else cx + d, cy if polar else cy + d, fill=guide)   # left only
        c.create_line(cx + d, cy - d, cx if polar else cx - d, cy if polar else cy + d, fill=guide)   # right only
        pad = 8 * theme.scale()
        c.create_text(cx - d - pad, cy - d - pad, text="L", fill=theme.MUTED, font=theme.font(theme.SIZE_BODY, "bold"))
        c.create_text(cx + d + pad, cy - d - pad, text="R", fill=theme.MUTED, font=theme.font(theme.SIZE_BODY, "bold"))
        c.create_text(cx + 4, cy - r - 2, text="mono", anchor="sw", fill=theme.MUTED, font=small)
        c.create_text(cx - r, cy + (-2 if polar else 2), text="out of phase", anchor="sw" if polar else "nw",
                      fill=theme.MUTED, font=small)
        c.create_text(cx + r / 2 + 2, cy + (-2 if polar else 0), text="-24", anchor="sw" if polar else "w",
                      fill=theme.MUTED, font=small)

    def _bar(self, y: int, label: str, fraction_from: float, fraction_to: float, colour: str, ticks):
        c = self.canvas
        w = c.winfo_width()
        left, right = theme.px(78), w - theme.px(8)
        bar_h = theme.px(12)
        c.create_text(0, y + bar_h / 2, text=label, anchor="w", fill=theme.MUTED, font=theme.font(theme.SIZE_SMALL))
        c.create_rectangle(left, y, right, y + bar_h, fill=theme.SURFACE, width=0)
        if fraction_to != fraction_from:
            a, b = sorted((fraction_from, fraction_to))
            c.create_rectangle(left + a * (right - left), y, left + b * (right - left), y + bar_h, fill=colour,
                               width=0)
        for f, text in ticks:
            x = left + f * (right - left)
            anchor = "nw" if f <= 0 else "ne" if f >= 1 else "n"   # the end labels stay inside the window
            c.create_line(x, y + bar_h, x, y + bar_h + 3, fill=theme.MUTED)
            c.create_text(x, y + bar_h + 3, text=text, anchor=anchor, fill=theme.MUTED, font=theme.font(theme.SIZE_TINY))

    def _draw_bars(self, reading: stereo.ScopeReading | None, y: int):
        value = reading.correlation if reading is not None else None
        if value is None:
            frac, colour = 0.5, theme.GOOD
        else:
            frac = (value + 1) / 2
            colour = theme.GOOD if value >= 0 else theme.WARN   # negative: the channels work against each other
        self._bar(y, "Correlation", 0.5, frac, colour, [(0, "-1"), (0.25, "-0.5"), (0.5, "0"), (0.75, "+0.5"),
                                                        (1, "+1")])
        balance = None
        if reading is not None:
            balance = reading.balance_db
            if reading.one_sided:
                balance = BALANCE_SPAN if reading.one_sided == "left" else -BALANCE_SPAN
        frac = 0.5 if balance is None else 0.5 - max(-1.0, min(1.0, balance / BALANCE_SPAN)) / 2
        colour = theme.METER_LEVEL
        self._bar(y + theme.px(30), "Balance", 0.5, frac, colour,
                  [(0, f"L +{BALANCE_SPAN:g} dB"), (0.5, "centre"), (1, f"R +{BALANCE_SPAN:g} dB")])

    def _draw_texts(self, reading: stereo.ScopeReading | None):
        dc, warn = describe_dc(reading)
        values = {"correlation": (describe_correlation(reading), theme.TEXT),
                  "balance": (describe_balance(reading), theme.TEXT),
                  "dc": (dc, theme.WARN if warn else theme.TEXT)}
        for key, (text, colour) in values.items():
            text = theme.figures(text)
            label = self.texts[key]
            if label.cget("text") != text or label.cget("fg") != colour:
                label.configure(text=text, fg=colour)

    # -- window ----------------------------------------------------------------------------
    def _moved(self, event):
        if event.widget is self.win:
            geometry = placement.current(self.win)
            if geometry:
                self.prefs["scope_geometry"] = geometry

    def lift(self):
        self.win.deiconify()
        self.win.lift()

    def close(self):
        if self._job:
            self.win.after_cancel(self._job)
        self.engine.stop()
        self.player.unmonitor(self.feed)
        self.win.destroy()
        self.on_close()
