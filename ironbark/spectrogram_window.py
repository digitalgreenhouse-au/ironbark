"""The Spectrogram window: a picture of the sound — time across, frequency up, brightness =
strength. Two views:
- Live: the music being heard, scrolling right to left (from the player's live feed, each
  slice shown when it's heard).
- Whole track: the full file at once, read in the background — where the sound stops, and
  how sharply, is easy to see here (a lossy source's wall, an upsampled file's empty top),
  with the cut-off marked (Now Playing → Check's figure when it has one).
The maths is in spectrum.py.
"""

from __future__ import annotations

import collections
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

import numpy as np
from PIL import Image, ImageTk

from . import controls, placement, spectrum, theme

REFRESH_MS = 100
COLUMN_PX = 2           # each live slice (1/25 s of sound) is this many pixels wide
RELAYOUT_MS = 250       # wait this long after a resize before re-reading the whole track
TICKS_HZ = (50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 40000, 80000)


def fmt_hz(hz: float) -> str:
    return f"{hz / 1000:g}k" if hz >= 1000 else f"{hz:g}"


def fmt_clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


class SpectrogramWindow:
    def __init__(self, parent: tk.Misc, player, prefs: dict, on_close=lambda: None,
                 analysis_for=lambda path: None):
        """analysis_for(path) -> the Now Playing check's Analysis for that file, or None."""
        self.player, self.prefs, self.on_close = player, prefs, on_close
        self.analysis_for = analysis_for
        self.win = tk.Toplevel(parent, bg=theme.BG)
        self.win.title("Spectrogram")
        self.win.geometry(f"{theme.px(900)}x{theme.px(480)}")
        self.win.minsize(theme.px(420), theme.px(260))
        placement.restore(self.win, prefs.get("spectrogram_geometry", ""))
        theme.dark_title_bar(self.win)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<Escape>", lambda e: self.close())

        bar = ttk.Frame(self.win, padding=theme.pad(10, 8, 10, 4))
        bar.pack(fill="x")
        view = prefs.get("spectrogram_view", "live")
        self.view = tk.StringVar(value=view if view in ("live", "track") else "live")
        # (0.22.0) A row of words (controls.ChoiceRow), as the other windows' choices.
        self.view_choice = controls.ChoiceRow(bar, (("live", "Live"), ("track", "Whole track")), self.view,
                                              command=self._view_changed)
        self.view_choice.pack(side="left")
        self.info = ttk.Label(self.win, style="Muted.TLabel", padding=theme.pad(10, 0, 10, 6), justify="left")
        self.info.pack(fill="x")
        self.info.bind("<Configure>", lambda e: self.info.configure(wraplength=max(200, e.width - 24)))
        self.canvas = tk.Canvas(self.win, bg=theme.BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=theme.px(10), pady=theme.pad(0, 10))
        self.canvas.bind("<Configure>", self._resized)

        self.feed = player.monitor()
        self.blocks: collections.deque = collections.deque(maxlen=64)   # waiting to be heard
        self.live = spectrum.LiveSpectrogram()
        self._rgb: collections.deque = collections.deque(maxlen=4000)    # live columns as picture rows
        self._rgb_key = None       # (height, rate) the colour columns were made for
        self._photo = None
        self._photo_key = None
        self._key_photo = None
        self._key_height = 0
        self._plot = (0, 0, 1, 1)  # x, y, width, height of the picture area
        self._track_path = None
        self._lossy_by_design = False  # an MP3: its trimmed top end is by design, not a clue
        self._picture: spectrum.TrackPicture | None = None
        self._progress = 0.0
        self._failed = False
        self._cancel = threading.Event()
        self._relayout_job = None
        self._made_total = 0       # live columns made so far (to redraw only when there's news)
        self.win.bind("<Configure>", self._moved, add="+")
        self._job = self.win.after(REFRESH_MS, self._tick)

    # -- layout --------------------------------------------------------------------
    def _resized(self, event=None):
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        left, right, top, bottom = theme.px(44), theme.px(70), 4, theme.px(22)
        self._plot = (left, top, max(10, w - left - right), max(10, h - top - bottom))
        if self._relayout_job:
            self.win.after_cancel(self._relayout_job)
        self._relayout_job = self.win.after(RELAYOUT_MS, self._relayout)
        self._draw()

    def _relayout(self):
        """After a resize settles: the whole-track picture is re-read at the new size."""
        self._relayout_job = None
        if self.view.get() == "track":
            self._start_track()

    def _view_changed(self):
        self.prefs["spectrogram_view"] = self.view.get()
        if self.view.get() == "track":
            self._start_track()
        self._draw()

    # -- data ------------------------------------------------------------------------
    def _drain(self) -> bool:
        """Feed the live picture everything now heard. True if it gained a column."""
        try:
            while True:
                self.blocks.append(self.feed.get_nowait())
        except queue.Empty:
            pass
        now = time.perf_counter()
        made = 0
        while self.blocks and self.blocks[0]["dac_time"] <= now:
            block = self.blocks.popleft()
            made += self.live.add(spectrum.to_float(block["source"]), block["rate"], block.get("path"),
                                  block.get("frame"))
        if made:
            self._made_total += made
            self._colour_new(made)
        return bool(made)

    def _colour_new(self, count: int):
        """Turn the newest `count` live columns into picture rows (all of them again if the
        picture's height or the sample rate changed)."""
        height = self._plot[3]
        if not self.live.columns:
            return
        rate = self.live.columns[-1][1]
        if self._rgb_key != (height, rate):
            self._rgb_key = (height, rate)
            self._rgb.clear()
            count = len(self.live.columns)
        starts = spectrum.log_rows(height, rate, spectrum.LIVE_FFT)
        new = list(self.live.columns)[-count:]
        for db, r in new:
            rows = spectrum.to_rows(db, starts)[::-1] if r == rate else np.full(height, spectrum.EMPTY)
            self._rgb.append(spectrum.colours(rows))

    def _start_track(self):
        track = self.player.status().track
        self._cancel.set()
        if track is None:
            self._track_path, self._picture = None, None
            return
        self._cancel = cancel = threading.Event()
        self._track_path, self._progress, self._picture, self._failed = track.path, 0.0, None, False
        self._lossy_by_design = bool(getattr(track, "lossy", False))  # MP3, Vorbis, Opus
        _, _, width, height = self._plot
        path = track.path

        def work():
            try:
                picture = spectrum.track_picture(path, max(10, width), max(10, height),
                                                 progress=lambda p: setattr(self, "_progress", p), cancel=cancel)
            except (OSError, RuntimeError, ValueError):
                picture = None
                self._failed = not cancel.is_set()
            if picture is not None and not cancel.is_set():
                self._picture = picture

        threading.Thread(target=work, name="spectrogram", daemon=True).start()

    def _tick(self):
        if not self.win.winfo_exists():
            return
        self._drain()
        if self.view.get() == "track":
            track = self.player.status().track
            if (track.path if track else None) != self._track_path:
                self._start_track()
        self._draw()
        self._job = self.win.after(REFRESH_MS, self._tick)

    # -- drawing -----------------------------------------------------------------------
    def _draw(self):
        c = self.canvas
        c.delete("all")
        x0, y0, width, height = self._plot
        if self.view.get() == "live":
            image, rate = self._live_image(width, height)
            if image is None:
                self.info.configure(text="Live: nothing heard yet. Start the music and the picture scrolls "
                                         "in from the right as it's heard (newest on the right).")
            else:
                seconds = width / COLUMN_PX / spectrum.LIVE_COLUMNS_PER_SECOND
                self.info.configure(text=f"Live, newest on the right · the last {seconds:.0f} seconds · "
                                         f"time across, frequency up (log scale), brightness = strength")
        else:
            image, rate = self._track_image(width, height)
        if image is None:
            c.create_rectangle(x0, y0, x0 + width, y0 + height, outline=theme.BORDER)
            return
        c.create_image(x0, y0, image=image, anchor="nw")
        self._draw_axes(rate)
        if self.view.get() == "track":
            self._draw_cutoff()
            self._draw_position()

    def _live_image(self, width: int, height: int):
        if self.live.columns and self._rgb_key != (height, self.live.columns[-1][1]):
            self._colour_new(len(self.live.columns))  # resized: recolour everything kept
        if not self._rgb:
            return None, None
        rate = self._rgb_key[1]
        key = ("live", self._made_total, width, height)
        if self._photo_key == key:
            return self._photo, rate
        count = max(1, width // COLUMN_PX)
        recent = list(self._rgb)[-count:]
        picture = np.zeros((height, count, 3), dtype=np.uint8)
        picture[:, count - len(recent):] = np.stack(recent, axis=1)
        picture = np.repeat(picture, COLUMN_PX, axis=1)
        self._photo, self._photo_key = ImageTk.PhotoImage(Image.fromarray(picture), master=self.win), key
        return self._photo, rate

    def _track_image(self, width: int, height: int):
        picture = self._picture
        if picture is None:
            if self._failed:
                text = "Whole track: this file couldn't be read."
            elif self._track_path:
                text = f"Whole track: reading the file… {self._progress:.0%}"
            else:
                text = "Whole track: nothing playing."
            self.info.configure(text=text)
            return None, None
        self.info.configure(text=self._track_info(picture))
        key = ("track", id(picture), width, height)
        if self._photo_key != key:
            image = Image.fromarray(spectrum.colours(picture.levels))
            if image.size != (width, height):
                image = image.resize((width, height), Image.NEAREST)
            self._photo, self._photo_key = ImageTk.PhotoImage(image, master=self.win), key
        return self._photo, picture.rate

    def _cutoff(self, picture: spectrum.TrackPicture):
        """(cut-off Hz, sharp, verdict, from Check?) — Check's figure (every sample read) when it
        has one for this file, else this picture's own estimate."""
        result = self.analysis_for(self._track_path) if self._track_path else None
        lossy = self._lossy_by_design or (result is not None and result.lossy)
        if result is not None and result.cutoff_hz is not None:
            verdict = result.verdict if result.verdict in ("lossy", "upsampled") else ""
            return result.cutoff_hz, result.cutoff_sharp, "" if lossy else verdict, True
        return picture.cutoff_hz, picture.cutoff_sharp, "" if lossy else picture.verdict, False

    def _track_info(self, picture: spectrum.TrackPicture) -> str:
        cutoff, sharp, verdict, checked = self._cutoff(picture)
        where = ""
        if cutoff is not None:
            source = " (Now Playing → Check)" if checked else " (estimated here; Now Playing → Check reads every sample)"
            if cutoff >= picture.rate / 2 - 300:
                where = " · sound right to the top of the range"
            else:
                how = "sharply (a wall)" if sharp else "gradually"
                where = f" · the sound reaches {cutoff / 1000:.1f} kHz and stops {how}{source}"
        meaning = {"lossy": " A wall this low suggests it was made from a lossy file (MP3, AAC…).",
                   "upsampled": " A wall here suggests it was upsampled from CD quality."}.get(verdict, "")
        if self._lossy_by_design:
            meaning = " (An MP3 trims the top end by design.)"
        damaged = (f" ⚠ The file stops decoding at {fmt_clock(picture.damaged_at)}: the rest is left dark."
                   if picture.damaged_at is not None else "")
        return f"Whole track · {fmt_clock(picture.duration)} across{where}.{meaning}{damaged}"

    def _draw_axes(self, rate: int):
        c = self.canvas
        x0, y0, width, height = self._plot
        small = theme.font(theme.SIZE_SMALL)
        for hz in TICKS_HZ:
            if hz >= rate / 2:
                break
            y = y0 + spectrum.row_of(hz, height, rate)
            if y < y0 + 14 * theme.scale():
                break  # too close to the top: it would sit on the "Hz" label
            c.create_line(x0 - 4, y, x0, y, fill=theme.MUTED)
            c.create_text(x0 - 6, y, text=fmt_hz(hz), anchor="e", fill=theme.MUTED, font=small)
        c.create_text(x0 - 6, y0 + 2, text="Hz", anchor="ne", fill=theme.MUTED, font=small)
        bottom = y0 + height
        if self.view.get() == "track" and self._picture is not None:
            duration = self._picture.duration
            step = next((s for s in (10, 15, 30, 60, 120, 300, 600) if duration / s <= 10), 1200)
            for t in range(0, int(duration) + 1, step):
                x = x0 + width * t / max(duration, 1e-9)
                c.create_line(x, bottom, x, bottom + 4, fill=theme.MUTED)
                c.create_text(x, bottom + 6, text=fmt_clock(t), anchor="n", fill=theme.MUTED, font=small)
        else:  # live: seconds back from now, at the right
            seconds = width / COLUMN_PX / spectrum.LIVE_COLUMNS_PER_SECOND
            for t in range(0, int(seconds) + 1, 5):
                x = x0 + width - width * t / seconds
                c.create_line(x, bottom, x, bottom + 4, fill=theme.MUTED)
                c.create_text(x, bottom + 6, text="now" if t == 0 else f"-{t} s",
                              anchor="ne" if t == 0 else "n", fill=theme.MUTED, font=small)
        # The colour key, top = loudest.
        kx = x0 + width + theme.px(14)
        kw = theme.px(12)
        if self._key_height != height:
            key = np.linspace(spectrum.DB_TOP, spectrum.DB_BOTTOM, height)[:, None].repeat(kw, axis=1)
            self._key_photo = ImageTk.PhotoImage(Image.fromarray(spectrum.colours(key)), master=self.win)
            self._key_height = height
        c.create_image(kx, y0, image=self._key_photo, anchor="nw")
        for db in range(int(spectrum.DB_TOP), int(spectrum.DB_BOTTOM) - 1, -20):
            y = y0 + height * (spectrum.DB_TOP - db) / (spectrum.DB_TOP - spectrum.DB_BOTTOM)
            c.create_text(kx + kw + 4, y, text=f"{db}", anchor="w", fill=theme.MUTED, font=small)
        c.create_text(kx + kw + 4, bottom + 6, text="dBFS", anchor="nw", fill=theme.MUTED, font=small)

    def _draw_cutoff(self):
        picture = self._picture
        cutoff, sharp, verdict, _ = self._cutoff(picture)
        if cutoff is None or cutoff >= picture.rate / 2 - 300 or cutoff <= spectrum.LOW_HZ:
            return
        x0, y0, width, height = self._plot
        y = y0 + spectrum.row_of(cutoff, height, picture.rate)
        colour = theme.WARN if verdict else theme.MUTED   # amber only when Check would flag it
        self.canvas.create_line(x0, y, x0 + width, y, fill=colour, dash=(4, 3))
        above = y - y0 > 16 * theme.scale()   # the label sits above the line, or below it near the top
        self.canvas.create_text(x0 + width - 4, y - 2 if above else y + 3,
                                text=f"sound stops: {cutoff / 1000:.1f} kHz",
                                anchor="se" if above else "ne", fill=colour, font=theme.font(theme.SIZE_SMALL))

    def _draw_position(self):
        """Where the music is now, as a thin line down the whole-track picture."""
        status = self.player.status()
        if status.track is None or status.track.path != self._track_path or not self._picture.duration:
            return
        x0, y0, width, height = self._plot
        x = x0 + width * min(1.0, status.position / self._picture.duration)
        self.canvas.create_line(x, y0, x, y0 + height, fill=theme.TEXT)

    # -- window --------------------------------------------------------------------------
    def _moved(self, event):
        if event.widget is self.win:
            geometry = placement.current(self.win)
            if geometry:
                self.prefs["spectrogram_geometry"] = geometry

    def lift(self):
        self.win.deiconify()
        self.win.lift()

    def close(self):
        self._cancel.set()
        for job in (self._job, self._relayout_job):
            if job:
                self.win.after_cancel(job)
        self.player.unmonitor(self.feed)
        self.win.destroy()
        self.on_close()
