"""The Meters window: two VU needles, then PPM, RMS and true-peak bars per channel, then
momentary and short-term loudness, with the track's integrated loudness and loudness range.

Everything shown comes from meters.py — standard ballistics computed from every sample sent
to the device, timed to what's being heard. This file only draws.

(0.22.0) Flat pill Reset buttons, and every live reading in fixed-width digits with a true minus
sign (theme.numbers / theme.figures), so the numbers hold still while they change. The VU faces,
the scales and the maths are untouched.
"""

from __future__ import annotations

import logging
import math
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from PIL import Image, ImageDraw, ImageFont, ImageTk

from .. import audiofile, controls, theme, toolwin
from ..library import album_folder
from ..loudness import LRA_SETTLING
from .meters import PPM_TYPES, MeterEngine, Reading, album_alignment, album_alignment_loudness, album_files

log = logging.getLogger(__name__)

REFRESH_MS = 33          # ~30 frames a second
# Colours: theme.py's METER_* group (the VU face's card, ink, red and rim; the RMS and loudness bars).
VU_MARKS = [-20, -10, -7, -5, -3, -2, -1, 0, 1, 2, 3]
SWEEP = 90               # degrees the needle travels, end stop to end stop
BAR_FLOOR = -48.0        # dB at the left end of the bars
# What 0 VU stands for. The broadcast references suit dynamic recordings; modern masters are
# mastered so loud that they pin the needle there, so the louder alignments people use for them too.
# "Auto" measures each album and sets 0 VU so its loud passages sit there (see meters.py);
# "Auto (loudness)" sets 0 VU to a steady tone as loud as the whole album (its integrated LUFS).
AUTO = "Auto (per album)"
AUTO_LOUDNESS = "Auto (loudness)"
AUTOS = {AUTO: "vu_auto", AUTO_LOUDNESS: "vu_auto_loudness"}  # each with its own remembered figures
AUTO_FALLBACK = -14.0  # until the first measurement arrives
ALIGNMENTS = {AUTO: None, AUTO_LOUDNESS: None, "-20 dBFS (SMPTE RP155)": -20.0, "-18 dBFS (EBU R68)": -18.0, "-14 dBFS": -14.0,
              "-12 dBFS": -12.0, "-10 dBFS (loud masters)": -10.0, "-8 dBFS": -8.0}
AUTO_CACHE_SIZE = 1000  # albums remembered
COPIES_LOOK_S = 5.0     # a network album waiting for its songs to download: looked at this often
_UNSET = object()       # nothing drawn yet: the next reading is drawn whatever it is


def vu_fraction(vu_db: float) -> float:
    """A VU scale is linear in voltage, not decibels: 0 VU sits at 100/141 of the sweep."""
    if vu_db == float("-inf") or vu_db < -60:
        return 0.0
    return min(1.06, 10 ** (vu_db / 20) / 10 ** (3 / 20))


def describe_loudness(reading: Reading | None) -> str:
    """The track's loudness so far, in one line (two when the window is narrow)."""
    if reading is None or reading.measured <= 0:
        return "Loudness this track: —"

    def lufs(v):
        return "—" if v is None else f"{v:.1f}"

    lra = "—" if reading.loudness_range is None else f"{reading.loudness_range:.1f} LU"
    if reading.loudness_range is not None and reading.measured < LRA_SETTLING:
        lra += " (settling)"  # EBU Mode: the range isn't stable for the first minute
    return (f"Loudness this track: integrated {lufs(reading.integrated)} LUFS · range {lra}\n"
            f"highest momentary {lufs(reading.max_momentary)} · short-term {lufs(reading.max_short_term)} LUFS")


def describe_psr(reading: Reading | None) -> str:
    """PSR now: the last 3 s's highest true peak minus the short-term loudness."""
    if reading is None or reading.psr is None:
        return "PSR —"
    return f"PSR {reading.psr:.1f} dB"


def _copies_here(track: Path, cancel: threading.Event, every: bool) -> bool:
    """(On Auto's worker.) Wait until songs of `track`'s album that are on a network drive have
    complete copies on this computer (the network playback cache) — `every` song, or just `track`
    — looking every COPIES_LOOK_S. Nothing is fetched for it. False if cancelled first."""
    wanted = None
    while not cancel.is_set():
        if wanted is None:
            try:
                files = album_files(track)[1]
                wanted = files if every else files[:1]
            except OSError:
                pass   # (its folder can't be listed just now — the network, say: looked at again)
        if wanted is not None and all(audiofile.local_copy(path) is not None for path in wanted):
            return True
        cancel.wait(COPIES_LOOK_S)
    return False


class MeterWindow(toolwin.ToolWindow):
    TITLE = "Meters"
    SIZE = (640, 700)
    MIN_SIZE = (420, 360)
    GEOMETRY_KEY = "meters_geometry"
    REFRESH_MS = REFRESH_MS

    def __init__(self, parent: tk.Misc, player, prefs: dict, on_close=lambda: None):
        self.player = player
        super().__init__(parent, prefs, on_close)

        choices = ttk.Frame(self.win, padding=theme.pad(10, 8, 10, 0))
        choices.pack(fill="x")
        ttk.Label(choices, text="0 VU =", style="Muted.TLabel").pack(side="left")
        align = prefs.get("vu_align", AUTO)
        self.align_var = tk.StringVar(value=align if align in ALIGNMENTS else AUTO)
        box = ttk.Combobox(choices, textvariable=self.align_var, values=list(ALIGNMENTS), state="readonly",
                           width=22, takefocus=False)
        box.pack(side="left", padx=theme.pad(4, 14))
        box.bind("<<ComboboxSelected>>", lambda e: self._restart())
        ttk.Label(choices, text="PPM", style="Muted.TLabel").pack(side="left")
        names = {v["name"]: k for k, v in PPM_TYPES.items()}
        self._ppm_names = names
        current = prefs.get("ppm_type", "I")
        self.ppm_var = tk.StringVar(value=PPM_TYPES.get(current, PPM_TYPES["I"])["name"])
        box = ttk.Combobox(choices, textvariable=self.ppm_var, values=list(names), state="readonly", width=17,
                           takefocus=False)
        box.pack(side="left", padx=theme.pad(4, 0))
        box.bind("<<ComboboxSelected>>", lambda e: self._restart())
        # What 0 VU stands for right now — always shown, so Auto's choice is never a mystery.
        self.align_text = ttk.Label(self.win, style="Muted.TLabel", padding=theme.pad(10, 4, 10, 0))
        self.align_text.pack(fill="x")
        self.align_text.bind("<Configure>", lambda e: self.align_text.configure(wraplength=max(200, e.width - 24)))
        self._auto: dict | None = None  # the album being measured: {"folder", "value", "final", "cancel"}

        self.note = ttk.Label(self.win, style="Muted.TLabel", padding=theme.pad(10, 0, 10, 8),
                              text="Measured from every sample sent to the device (after the volume), in time with "
                                   "what you hear. VU: IEC 60268-17 · PPM: IEC 60268-10 · RMS: 300 ms, AES17 · "
                                   "true peak: 4× oversampled · loudness: EBU R128 / ITU-R BS.1770-5 (momentary "
                                   "400 ms, short-term 3 s, integrated gated, range EBU Tech 3342) · PSR: true peak (last 3 s) − "
                                   "short-term loudness.")
        self.note.pack(side="bottom", fill="x")
        self.note.bind("<Configure>", lambda e: self.note.configure(wraplength=max(200, e.width - 24)))
        # The highest true peak since the window opened (or since Reset), with its Reset button.
        peak_row = ttk.Frame(self.win, padding=theme.pad(10, 0, 10, 6))
        peak_row.pack(side="bottom", fill="x")
        controls.FlatButton(peak_row, "Reset", self._reset_max, label="Start the highest true peak again").pack(
            side="right")
        self.max_label = tk.Label(peak_row, text="", bg=theme.BG, fg=theme.TEXT, font=theme.numbers(theme.SIZE_BODY),
                                  anchor="e")
        self.max_label.pack(side="right", padx=theme.pad(0, 8))
        # PSR (peak to short-term loudness ratio) now, on the left of the same row.
        self.psr_label = tk.Label(peak_row, text="PSR —", bg=theme.BG, fg=theme.TEXT,
                                  font=theme.numbers(theme.SIZE_BODY), anchor="w")
        self.psr_label.pack(side="left")
        # The track's loudness so far (starts again at each new track), with its own Reset.
        loud_row = ttk.Frame(self.win, padding=theme.pad(10, 0, 10, 4))
        loud_row.pack(side="bottom", fill="x")
        controls.FlatButton(loud_row, "Reset", self._reset_loudness,
                            label="Measure this track's loudness again from now").pack(side="right")
        self.loud_label = tk.Label(loud_row, text="", bg=theme.BG, fg=theme.TEXT, font=theme.numbers(theme.SIZE_BODY),
                                   anchor="e", justify="right")
        self.loud_label.pack(side="right", padx=theme.pad(0, 8))
        self.canvas = tk.Canvas(self.win, bg=theme.BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=theme.px(10), pady=theme.px(8))
        self.canvas.bind("<Configure>", lambda e: self._draw_static())

        self.feed = None
        self.engine: MeterEngine | None = None
        self._face = None
        self._layout = None
        self._live_shown = _UNSET   # the reading the needles and bars show
        theme.on_change(self._palette_changed)
        self._restart()
        self.start_ticking()

    # -- engine --------------------------------------------------------------
    def _restart(self):
        """(Re)start measuring — also when the alignment or PPM type changes."""
        self.prefs["vu_align"] = self.align_var.get()
        self.prefs["ppm_type"] = self._ppm_names.get(self.ppm_var.get(), "I")
        if self.engine is not None:
            self.engine.stop()
            self.player.unmonitor(self.feed)
        self.feed = self.player.monitor()
        self.engine = MeterEngine(self.feed, ppm_type=self.prefs["ppm_type"], vu_align_dbfs=self._alignment())
        self.engine.start()
        self._paint_alignment()

    # -- what 0 VU stands for -------------------------------------------------
    def _alignment(self) -> float:
        fixed = ALIGNMENTS.get(self.align_var.get())
        if fixed is not None:
            return fixed
        if (self._auto is not None and self._auto["mode"] == self.align_var.get()
                and self._auto["value"] is not None):
            return self._auto["value"]
        return AUTO_FALLBACK

    def _auto_cache(self, mode: str) -> dict:
        key = AUTOS[mode]
        cache = self.prefs.get(key)
        if not isinstance(cache, dict):
            cache = self.prefs[key] = {}
        return cache

    def _follow_album(self):
        """Auto: when an album starts, use its remembered figure, or measure it in the background
        (the song playing first, as a stand-in, then the whole album)."""
        mode = self.align_var.get()
        if mode not in AUTOS or self.engine is None or self.engine.current_path is None:
            return
        track = Path(self.engine.current_path)
        folder = album_folder(track)   # (worked out from the path alone: this runs 30 times a second)
        if self._auto is not None and self._auto["folder"] == folder and self._auto["mode"] == mode:
            return
        if self._auto is not None:
            self._auto["cancel"].set()
        cached = self._auto_cache(mode).get(str(folder))
        lufs = None
        if isinstance(cached, list) and len(cached) == 2:  # Auto (loudness) keeps [0 VU, album LUFS]
            cached, lufs = cached
        if isinstance(cached, (int, float)) and not isinstance(cached, bool):
            self._auto = {"folder": folder, "mode": mode, "value": float(cached), "lufs": lufs, "final": True,
                          "failed": False, "cancel": threading.Event()}
            return
        state = {"folder": folder, "mode": mode, "value": None, "lufs": None, "final": False, "failed": False,
                 "cancel": threading.Event()}
        self._auto = state

        def provisional(value, lufs=None):
            if not state["final"]:
                state["value"], state["lufs"] = value, lufs

        def work():
            value = lufs = None
            tried_first = False
            while True:
                try:
                    if mode == AUTO_LOUDNESS:
                        result = album_alignment_loudness(track, on_first=provisional, cancel=state["cancel"])
                        if result is not None:
                            value, lufs = result
                    else:
                        value = album_alignment(track, on_first=provisional, cancel=state["cancel"])
                except audiofile.NotCached:
                    # A network album isn't all on this computer yet (its songs download as they're
                    # played): measured again once they are — first the song playing, for a figure
                    # meanwhile, then the whole album. Never from a part of it.
                    every, tried_first = tried_first or state["value"] is not None, True
                    state["waiting"] = True
                    if not _copies_here(track, state["cancel"], every):
                        return   # (another album, or the window closed)
                    state["waiting"] = False
                    continue
                except (OSError, RuntimeError):
                    value = None
                    state["failed"] = True
                except Exception:  # noqa: BLE001 — any failure is said in the window, never "measuring…" for ever
                    log.exception("measuring the album for Auto")
                    value = None
                    state["failed"] = True
                break
            if value is not None and not state["cancel"].is_set():
                state["value"], state["lufs"], state["final"] = value, lufs, True

        threading.Thread(target=work, name="vu-auto", daemon=True).start()

    def _paint_alignment(self):
        value = self._alignment()
        if self.engine is not None and self.engine.vu_align_dbfs != value:
            self.engine.set_alignment(value)
        if self._layout is not None and getattr(self, "_marked", None) != (value, self._layout["w"]):
            self._marked = (value, self._layout["w"])  # where 0 VU sits on the dBFS bars
            left, right, *_ = self._bar_geometry()
            x = self._x(value, left, right, 3.0)
            self.canvas.delete("zero_vu")
            self.canvas.create_text(x, self._scale_y + theme.px(14), text="0 VU", anchor="n",
                                    fill=theme.MUTED, font=theme.font(theme.SIZE_TINY), tags="zero_vu")
        mode = self.align_var.get()
        auto = self._auto if self._auto is not None and self._auto["mode"] == mode else None
        name = "Auto (loudness)" if mode == AUTO_LOUDNESS else "Auto"
        if mode not in AUTOS:
            text = f"0 VU = a sine at {value:g} dBFS (fixed)"
        elif auto is None:
            text = f"{name}: waiting for music · 0 VU = {value:g} dBFS meanwhile"
        elif auto["final"]:
            if mode == AUTO_LOUDNESS:
                album = f"{auto['lufs']:.1f} LUFS" if auto["lufs"] is not None else "measured"
                text = (f"0 VU = {value:g} dBFS · Auto (loudness): a 1 kHz tone as loud as the album "
                        f"({album}, {auto['folder'].name})")
                stored = [value, auto["lufs"]] if auto["lufs"] is not None else value
            else:
                text = f"0 VU = {value:g} dBFS · Auto, measured for this album ({auto['folder'].name})"
                stored = value
            cache = self._auto_cache(mode)
            if cache.get(str(auto["folder"])) != stored:
                cache[str(auto["folder"])] = stored
                while len(cache) > AUTO_CACHE_SIZE:
                    cache.pop(next(iter(cache)))
        elif auto["failed"]:
            text = (f"0 VU = {value:g} dBFS · {name}: from this song (the rest of the album couldn't be measured)"
                    if auto["value"] is not None else f"{name}: couldn't measure this album · 0 VU = {value:g} dBFS")
        elif auto.get("waiting"):
            text = (f"0 VU = {value:g} dBFS · {name}: from this song, until the rest of the album has downloaded"
                    if auto["value"] is not None
                    else f"{name}: measured once this album's songs have downloaded · 0 VU = {value:g} dBFS meanwhile")
        elif auto["value"] is not None:
            text = f"0 VU = {value:g} dBFS · {name}: from this song, while the rest of the album is measured…"
        else:
            text = f"{name}: measuring this album… · 0 VU = {value:g} dBFS meanwhile"
        if self.align_text.cget("text") != text:
            self.align_text.configure(text=text)

    # -- drawing -------------------------------------------------------------
    def _draw_static(self):
        c = self.canvas
        w, h = c.winfo_width(), c.winfo_height()
        if w < 50 or h < 50:
            return
        gap = theme.px(10)
        face_w = (w - gap) // 2
        bars_need = len(self._bar_rows()) * theme.px(19) + theme.px(18) + theme.px(36)
        face_h = max(theme.px(60), min(int(face_w * 0.6), h - bars_need - theme.px(16)))
        self._layout = {"w": w, "h": h, "face_w": face_w, "face_h": face_h, "gap": gap,
                        "bars_top": face_h + theme.px(16)}
        self._face = self._render_face(face_w, face_h)
        c.delete("all")
        self._marked = None
        self._live_shown = _UNSET
        for i in range(2):
            c.create_image(i * (face_w + gap), 0, image=self._face, anchor="nw")
        self._draw_bar_scales()

    def _palette_changed(self, _palette):
        """The faces are Pillow pictures: draw them (and the scales) again in the new colours."""
        self._draw_static()
        self.canvas.configure(bg=theme.BG)

    def _pivot(self, face_w, face_h):
        return face_w / 2, face_h * 1.18  # below the face, as on a real meter

    def _render_face(self, face_w: int, face_h: int):
        k = 3  # draw big, shrink: smooth lines and text
        W, H = face_w * k, face_h * k
        img = Image.new("RGB", (W, H), theme.METER_FACE)
        ink, red = theme.METER_INK, theme.METER_RED
        d = ImageDraw.Draw(img)
        px, py = self._pivot(W, H)
        radius = H * 0.9
        try:
            font = ImageFont.truetype(theme.METER_FONT_FILE, int(H * theme.METER_FACE_TEXT))
            big = ImageFont.truetype(theme.METER_FONT_BOLD_FILE, int(H * theme.METER_FACE_BIG))
        except OSError:
            font = big = ImageFont.load_default()

        def point(fraction, r):
            angle = math.radians(-SWEEP / 2 + fraction * SWEEP)
            return px + r * math.sin(angle), py - r * math.cos(angle)

        # The arc, red from 0 VU up.
        steps = 120
        zero = vu_fraction(0)
        arc = [point(i / steps * 1.0, radius) for i in range(steps + 1)]
        d.line([p for p, i in zip(arc, range(steps + 1)) if i / steps <= zero], fill=ink, width=k * 2)
        d.line([p for p, i in zip(arc, range(steps + 1)) if i / steps >= zero], fill=red, width=k * 5)
        for mark in VU_MARKS:
            f = vu_fraction(mark)
            colour = red if mark > 0 else ink
            d.line([point(f, radius), point(f, radius * 1.08)], fill=colour, width=k * 2)
            x, y = point(f, radius * 1.15)
            label = str(abs(mark)) if mark else "0"
            d.text((x, y), label, fill=colour, font=font, anchor="mm")
        d.text((W * 0.1, H * 0.12), "−", fill=ink, font=big, anchor="mm")
        d.text((W * 0.9, H * 0.12), "+", fill=red, font=big, anchor="mm")
        d.text((W / 2, H * 0.62), "VU", fill=ink, font=big, anchor="mm")
        d.rectangle((0, 0, W - 1, H - 1), outline=theme.METER_RIM, width=k)
        image = img.resize((face_w, face_h), Image.LANCZOS)
        return ImageTk.PhotoImage(image, master=self.win)

    def _bar_rows(self):
        names = ["PPM", "RMS", "True peak"]
        rows = [(f"{name} {ch}", name, i) for name in names for i, ch in enumerate(("L", "R"))]
        return rows + [("Momentary", "Momentary", 0), ("Short-term", "Short-term", 0)]  # both channels together

    def _row_y(self, r: int, top: int, row_h: int) -> int:
        """Rows in pairs, with a small gap between the pairs."""
        return top + r * row_h + (r // 2) * theme.px(6)

    def _bar_geometry(self):
        lay = self._layout
        label_w, value_w = theme.px(90), theme.px(78)
        left, right = label_w, lay["w"] - value_w
        rows = self._bar_rows()
        bar_h = theme.px(12)
        row_h = bar_h + theme.px(7)
        top = lay["bars_top"]
        return left, right, rows, bar_h, row_h, top

    def _x(self, value, left, right, top_db):
        f = (max(BAR_FLOOR, min(top_db, value)) - BAR_FLOOR) / (top_db - BAR_FLOOR)
        return left + f * (right - left)

    def _draw_bar_scales(self):
        c = self.canvas
        left, right, rows, bar_h, row_h, top = self._bar_geometry()
        for r, (label, _name, _ch) in enumerate(rows):
            y = self._row_y(r, top, row_h)
            c.create_text(0, y + bar_h / 2, text=label, anchor="w", fill=theme.MUTED, font=theme.font(theme.SIZE_SMALL))
            c.create_rectangle(left, y, right, y + bar_h, fill=theme.SURFACE, width=0)
        scale_y = self._row_y(len(rows), top, row_h) + theme.px(8)
        for db_mark in range(int(BAR_FLOOR), 1, 6):
            x = self._x(db_mark, left, right, 3.0)
            c.create_line(x, scale_y - theme.px(4), x, scale_y, fill=theme.MUTED)
            c.create_text(x, scale_y + theme.px(2), text=str(db_mark), anchor="n", fill=theme.MUTED,
                          font=theme.font(theme.SIZE_TINY))
        self._scale_y = scale_y  # the "0 VU" mark below follows the alignment (see _paint_alignment)
        c.create_text(0, scale_y + theme.px(2), text="dBFS · dBTP · LUFS", anchor="nw", fill=theme.MUTED,
                      font=theme.font(theme.SIZE_TINY))

    def _step(self):
        self._follow_album()
        self._paint_alignment()
        if toolwin.on_screen(self.win):   # minimised: the engine measures on; nothing is drawn
            self._draw_live(self.engine.reading_at(time.perf_counter()) if self.engine else None)

    def _draw_live(self, reading: Reading | None):
        if self._layout is None or reading == self._live_shown:
            return                     # (nothing playing: the same as the frame on show)
        self._live_shown = reading
        c = self.canvas
        c.delete("live")
        lay = self._layout
        # Needles.
        for i in range(2):
            vu = reading.vu[min(i, len(reading.vu) - 1)] if reading else float("-inf")
            ox = i * (lay["face_w"] + lay["gap"])
            px, py = self._pivot(lay["face_w"], lay["face_h"])
            angle = math.radians(-SWEEP / 2 + vu_fraction(vu) * SWEEP)
            r_out, r_in = lay["face_h"] * 1.02, lay["face_h"] * 0.42
            x1, y1 = ox + px + r_in * math.sin(angle), py - r_in * math.cos(angle)
            x2, y2 = ox + px + r_out * math.sin(angle), py - r_out * math.cos(angle)
            c.create_line(x1, y1, x2, y2, fill=theme.METER_INK, width=max(1, theme.px(1.6)), tags="live")
            text = "—" if not reading or vu < -60 else theme.figures(f"{vu:+.1f} VU")
            c.create_text(ox + lay["face_w"] - theme.px(6), lay["face_h"] - theme.px(5), text=text,
                          anchor="se", fill=theme.METER_INK, font=theme.numbers(theme.SIZE_SMALL), tags="live")
            c.create_text(ox + theme.px(6), lay["face_h"] - theme.px(5), text="LR"[i], anchor="sw",
                          fill=theme.METER_INK, font=theme.font(theme.SIZE_SMALL, "bold"), tags="live")
        # Bars.
        left, right, rows, bar_h, row_h, top = self._bar_geometry()
        for r, (_label, name, ch) in enumerate(rows):
            y = self._row_y(r, top, row_h)
            if reading is None:
                value = float("-inf")
            elif name in ("Momentary", "Short-term"):
                value = reading.momentary if name == "Momentary" else reading.short_term
                value = float("-inf") if value is None else value
            else:
                values = {"PPM": reading.ppm, "RMS": reading.rms, "True peak": reading.true_peak}[name]
                value = values[min(ch, len(values) - 1)]
            if value > BAR_FLOOR:
                x = self._x(value, left, right, 3.0)
                if name == "True peak":
                    colour = theme.DANGER if value > 0 else theme.WARN if value > -1 else theme.ACCENT
                elif name == "PPM":
                    colour = theme.WARN if value > -9 else theme.ACCENT  # -9 dBFS: permitted maximum (EBU R68)
                elif name in ("Momentary", "Short-term"):
                    colour = theme.METER_LOUDNESS
                else:
                    colour = theme.METER_LEVEL
                c.create_rectangle(left, y, x, y + bar_h, fill=colour, width=0, tags="live")
            text = "—" if value == float("-inf") or value <= -119 else theme.figures(f"{value:+.1f}")
            c.create_text(lay["w"], y + bar_h / 2, text=text, anchor="e", fill=theme.TEXT,
                          font=theme.numbers(theme.SIZE_BODY), tags="live")
        if reading is not None:
            mx = reading.max_true_peak
            text, colour = f"Max true peak since reset: {mx:+.2f} dBTP", theme.DANGER if mx > 0 else theme.TEXT
        else:
            text, colour = "Max true peak since reset: —", theme.MUTED
        text = theme.figures(text)
        if self.max_label.cget("text") != text:
            self.max_label.configure(text=text, fg=colour)
        text = theme.figures(describe_loudness(reading))
        if self.loud_label.cget("text") != text:
            self.loud_label.configure(text=text)
        text = theme.figures(describe_psr(reading))
        if self.psr_label.cget("text") != text:
            self.psr_label.configure(text=text)

    def _reset_max(self):
        if self.engine is not None:
            self.engine.reset_max()

    def _reset_loudness(self):
        if self.engine is not None:
            self.engine.reset_loudness()

    def _closing(self):
        if self.engine is not None:
            self.engine.stop()
        if self._auto is not None:
            self._auto["cancel"].set()
        self.player.unmonitor(self.feed)
