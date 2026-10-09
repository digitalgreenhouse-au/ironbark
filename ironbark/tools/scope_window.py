"""The Stereo scope window (key O): where the sound sits between the speakers.

- The scope (vectorscope / goniometer): every sample heard as a glowing dot that fades in a
  moment. Two views: Lissajous (the classic: mono is a vertical line, left-only and right-only
  lean 45°, opposite channels lie flat) and Polar (the same folded into a half circle, with
  the 45° lines marking left and right; outside them = out of phase).
- Correlation bar (-1…+1, last 600 ms), balance bar (which side is louder), and the DC offset
  of each channel since the track began.

The maths is in stereo.py; this file only draws. Fed by the file's own samples (before the
volume), so the picture is the same at any volume. A frame is drawn only when something changed
(sound heard, glow still fading, a new reading) and while the window is on screen.
"""

from __future__ import annotations

import math
import time
import tkinter as tk
from tkinter import ttk

import numpy as np
from PIL import Image, ImageTk

from .. import controls, placement, theme, toolwin
from . import stereo

REFRESH_MS = 33          # ~30 frames a second
FADE = 0.7               # how much of the glow is left after each frame (gone in ~1/3 s)
GLOW = 1.2               # how bright one dot is (brightness = 1 - e^(-GLOW x dots))
GLOW_FLOOR = 1e-3        # glow below this is under one colour step: cleared to exactly 0 (fading
                         # on would leave tiny "subnormal" numbers that slow every frame many times over)
BALANCE_SPAN = 12.0      # dB at each end of the balance bar
VIEWS = (("lissajous", "Lissajous"), ("polar", "Polar"))
_UNSET = object()        # no frame drawn yet: the next one is drawn whatever the reading


def _glow_colours(background: str, dot: str) -> list[int]:
    """The picture's 256 colours, background to fully lit, as Pillow's flat palette list."""
    bg, lit = np.array(theme.rgb(background), float), np.array(theme.rgb(dot), float)
    steps = np.linspace(0.0, 1.0, 256)[:, None]
    return (bg + steps * (lit - bg)).astype(np.uint8).ravel().tolist()


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


class ScopeWindow(toolwin.ToolWindow):
    TITLE = "Stereo scope"
    SIZE = (420, 640)
    MIN_SIZE = (300, 420)
    GEOMETRY_KEY = "scope_geometry"
    REFRESH_MS = REFRESH_MS

    def __init__(self, parent: tk.Misc, player, prefs: dict, on_close=lambda: None):
        self.player = player
        super().__init__(parent, prefs, on_close)

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
        self._dark = True          # the glow is all zero (nothing to fade)
        self._photo = None         # the picture, drawn into in place each frame
        self._shown = _UNSET       # the reading the bars and lines show
        self._geometry = None      # (x, y, width, height, centre x, centre y, radius) of the picture
        self._last = time.perf_counter()
        self._colours = _glow_colours(theme.BG, theme.GOOD)
        theme.on_change(self._palette_changed)
        self.start_ticking()

    def _palette_changed(self, _palette):
        """The glow picture's colours are kept between frames: take the new ones (and draw the
        guides again in theirs)."""
        self._colours = _glow_colours(theme.BG, theme.GOOD)
        self._layout_changed()

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
        self._dark = True
        # The picture and the guides stay on the canvas; each frame only repaints the picture
        # (and the bars, when the reading changed).
        self._photo = ImageTk.PhotoImage("RGB", (width, height), master=self.win)
        self.canvas.delete("all")
        self.canvas.create_image(x, y, image=self._photo, anchor="nw")
        self._draw_guides(x + width / 2, y + cy, radius)
        self._shown = _UNSET
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

    def _image(self) -> Image.Image:
        """The glow as a picture: brightness 1 - e^(-GLOW x glow), one of 256 palette steps."""
        lit = -np.expm1(self._glow * -GLOW)
        image = Image.fromarray((lit * 255).astype(np.uint8))
        image.putpalette(self._colours)
        return image

    def _step(self):
        now = time.perf_counter()
        since, self._last = self._last, now
        if self._glow is None or not toolwin.on_screen(self.win):
            self._shown = _UNSET   # minimised: nothing drawn (the engine reads on, on its own thread)
            return
        points = self.engine.samples_between(since, now)
        reading = self.engine.reading_at(now)
        if self._dark and not len(points) and reading == self._shown:
            return                 # nothing heard, nothing fading, the same reading: the frame shown is right
        self._glow *= FADE
        self._glow[self._glow < GLOW_FLOOR] = 0.0
        self._add_points(points)
        self._dark = self._glow.max() == 0
        self._draw(reading)

    # -- drawing ------------------------------------------------------------------------
    def _draw(self, reading: stereo.ScopeReading | None = None):
        if self._geometry is None or self._photo is None:
            return
        x0, y0, width, height, cx, cy, radius = self._geometry
        self._photo.paste(self._image())
        if reading != self._shown:
            self._shown = reading
            self.canvas.delete("bars")
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
        c.create_text(0, y + bar_h / 2, text=label, anchor="w", fill=theme.MUTED, font=theme.font(theme.SIZE_SMALL),
                      tags="bars")
        c.create_rectangle(left, y, right, y + bar_h, fill=theme.SURFACE, width=0, tags="bars")
        if fraction_to != fraction_from:
            a, b = sorted((fraction_from, fraction_to))
            c.create_rectangle(left + a * (right - left), y, left + b * (right - left), y + bar_h, fill=colour,
                               width=0, tags="bars")
        for f, text in ticks:
            x = left + f * (right - left)
            anchor = "nw" if f <= 0 else "ne" if f >= 1 else "n"   # the end labels stay inside the window
            c.create_line(x, y + bar_h, x, y + bar_h + 3, fill=theme.MUTED, tags="bars")
            c.create_text(x, y + bar_h + 3, text=text, anchor=anchor, fill=theme.MUTED,
                          font=theme.font(theme.SIZE_TINY), tags="bars")

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
    def _closing(self):
        self.engine.stop()
        self.player.unmonitor(self.feed)
        self._photo = None   # its picture goes with the window, on this thread
