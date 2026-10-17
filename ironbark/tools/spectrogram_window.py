"""The Spectrogram window: a picture of the sound — time across, frequency up, brightness =
strength. Two views:
- Live: the music being heard, scrolling right to left (from the player's live feed, each
  slice shown when it's heard).
- Whole track: the full file at once, read in the background — where the sound stops, and
  how sharply, is easy to see here (a lossy source's wall, an upsampled file's empty top),
  with the cut-off marked (Now Playing → Check's figure when it has one).
The maths is in spectrum.py. The live picture is kept between frames: each frame colours only the
slices heard since and scrolls them in. Nothing is drawn while nothing changed, or while the
window is minimised.
"""

from __future__ import annotations

import collections
import logging
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

import numpy as np
from PIL import Image, ImageTk

from .. import controls, theme, toolwin
from . import spectrum

log = logging.getLogger(__name__)

REFRESH_MS = 100
COLUMN_PX = 2           # each live slice (1/25 s of sound) is this many pixels wide
RELAYOUT_MS = 250       # wait this long after a resize before re-reading the whole track
TICKS_HZ = (50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 40000, 80000)


def fmt_hz(hz: float) -> str:
    return f"{hz / 1000:g}k" if hz >= 1000 else f"{hz:g}"


def fmt_clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


class SpectrogramWindow(toolwin.ToolWindow):
    TITLE = "Spectrogram"
    SIZE = (900, 480)
    MIN_SIZE = (420, 260)
    GEOMETRY_KEY = "spectrogram_geometry"
    REFRESH_MS = REFRESH_MS

    def __init__(self, parent: tk.Misc, player, prefs: dict, on_close=lambda: None,
                 analysis_for=lambda path: None):
        """analysis_for(path) -> the Now Playing check's Analysis for that file, or None."""
        self.player = player
        self.analysis_for = analysis_for
        super().__init__(parent, prefs, on_close)

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
        self._pixels: np.ndarray | None = None   # the live picture (height x width x RGB), newest on the right
        self._pixels_key = None    # (height, columns across, rate) it was coloured for
        self._pixels_made = 0      # the live columns made (_made_total) it shows
        self._recolour_due = False  # stretched while the window is resized: colour afresh once it settles
        self._live_photo = None
        self._photo = None         # the whole-track picture
        self._photo_key = None
        self._key_photo = None
        self._key_height = 0
        self._plot = (0, 0, 1, 1)  # x, y, width, height of the picture area
        self._drawn = None         # what the canvas shows (see _draw)
        self._track_path = None
        self._lossy_by_design = False  # an MP3: its trimmed top end is by design, not a clue
        self._picture: spectrum.TrackPicture | None = None
        self._progress = 0.0
        self._failed = False
        self._reading: toolwin.Handback | None = None   # the whole-track reading's progress and picture, from its thread
        self._cancel = threading.Event()
        self._relayout_job = None
        self._made_total = 0       # live columns made so far (to redraw only when there's news)
        self.start_ticking()

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
        """After a resize settles: the live picture is coloured afresh at the new size (it was only
        stretched meanwhile), the whole-track picture re-read."""
        self._relayout_job = None
        if self._recolour_due:
            self._recolour_due = False
            self._pixels_key = None
            if self.view.get() == "live":
                self._drawn = None
                self._draw()
        if self.view.get() == "track":
            self._start_track()

    def _view_changed(self):
        self.prefs["spectrogram_view"] = self.view.get()
        if self.view.get() == "track":
            self._start_track()
        self._draw()

    # -- data ------------------------------------------------------------------------
    def _drain(self) -> bool:
        """Feed the live slices everything now heard (also while they're not on show, so the
        picture has no gap when they are). True if it gained a column."""
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
        self._made_total += made
        return bool(made)

    @staticmethod
    def _stripes(columns, height: int, rate: int) -> np.ndarray:
        """Live columns as picture stripes (height x COLUMN_PX each, side by side, top row = highest
        frequency); a column at another sample rate is left dark."""
        starts = spectrum.log_rows(height, rate, spectrum.LIVE_FFT)
        stripes = [spectrum.colours(spectrum.to_rows(db, starts)[::-1] if r == rate
                                    else np.full(height, spectrum.EMPTY)) for db, r in columns]
        return np.repeat(np.stack(stripes, axis=1), COLUMN_PX, axis=1)

    def _start_track(self):
        track = self.player.status().track
        self._cancel.set()
        self._reading = None
        if track is None:
            self._track_path, self._picture = None, None
            return
        self._cancel = cancel = threading.Event()
        self._track_path, self._progress, self._picture, self._failed = track.path, 0.0, None, False
        self._lossy_by_design = bool(getattr(track, "lossy", False))  # MP3, Vorbis, Opus
        _, _, width, height = self._plot
        path = track.path
        # The thread fills this in and _take_track copies it across (it never holds the window: toolwin.Handback).
        self._reading = job = toolwin.Handback(progress=0.0, failed=False, picture=None)

        def work():
            try:
                picture = spectrum.track_picture(path, max(10, width), max(10, height),
                                                 progress=lambda p: setattr(job, "progress", p), cancel=cancel)
            except (OSError, RuntimeError, ValueError):
                picture = None
                job.failed = not cancel.is_set()
            except Exception:  # noqa: BLE001 — said in the window ("couldn't be read"), never a spinner for ever
                log.exception("reading the whole track for the spectrogram")
                picture = None
                job.failed = not cancel.is_set()
            if picture is not None and not cancel.is_set():
                job.picture = picture

        threading.Thread(target=work, name="spectrogram", daemon=True).start()

    def _take_track(self):
        """The whole-track reading's progress and picture, from its thread."""
        job = self._reading
        if job is not None:
            self._progress, self._failed = job.progress, job.failed
            if self._picture is None and job.picture is not None:
                self._picture = job.picture

    def _step(self):
        self._drain()
        if self.view.get() == "track":
            track = self.player.status().track
            if (track.path if track else None) != self._track_path:
                self._start_track()
        self._take_track()
        if toolwin.on_screen(self.win):   # minimised: nothing drawn
            self._draw()

    # -- drawing -----------------------------------------------------------------------
    def _draw(self):
        x0, y0, width, height = self._plot
        live = self.view.get() == "live"
        news = (self._made_total if live
                else (id(self._picture), f"{self._progress:.0%}", self._failed, self._track_path,
                      id(self.analysis_for(self._track_path)) if self._track_path else None))
        key = (live, news, self._plot, theme.current.name)
        if key == self._drawn:      # nothing new: leave the canvas as it is
            if not live and self._picture is not None:
                self._draw_position()
            return
        self._drawn = key
        c = self.canvas
        c.delete("all")
        if live:
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
        """The live picture, kept between frames: the columns heard since the last frame are
        coloured and scrolled in from the right. A new size stretches the old picture until the
        size settles (_relayout); then only the columns on show are coloured afresh."""
        columns = self.live.columns
        if not columns:
            return None, None
        rate = columns[-1][1]
        count = max(1, width // COLUMN_PX)
        key = (height, count, rate)
        new = self._made_total - self._pixels_made
        old = self._pixels
        if key != self._pixels_key and old is not None and self._pixels_key and self._pixels_key[2] == rate:
            stretched = np.asarray(Image.fromarray(old).resize((old.shape[1], height), Image.NEAREST))
            pixels = np.zeros((height, count * COLUMN_PX, 3), dtype=np.uint8)
            keep = min(pixels.shape[1], old.shape[1])
            pixels[:, pixels.shape[1] - keep:] = stretched[:, old.shape[1] - keep:]
            self._pixels, self._pixels_key, self._recolour_due = pixels, key, True
        if key != self._pixels_key or new >= count:
            shown = list(columns)[-min(count, len(columns)):]
            pixels = np.zeros((height, count * COLUMN_PX, 3), dtype=np.uint8)
            pixels[:, pixels.shape[1] - len(shown) * COLUMN_PX:] = self._stripes(shown, height, rate)
            self._pixels, self._pixels_key = pixels, key
        elif new > 0:
            stripes = self._stripes(list(columns)[-new:], height, rate)
            self._pixels[:, :-stripes.shape[1]] = self._pixels[:, stripes.shape[1]:]
            self._pixels[:, -stripes.shape[1]:] = stripes
        self._pixels_made = self._made_total
        image = Image.fromarray(self._pixels)
        if self._live_photo is not None and (self._live_photo.width(), self._live_photo.height()) == image.size:
            self._live_photo.paste(image)
        else:
            self._live_photo = ImageTk.PhotoImage(image, master=self.win)
        return self._live_photo, rate

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
        self.canvas.delete("position")
        status = self.player.status()
        if status.track is None or status.track.path != self._track_path or not self._picture.duration:
            return
        x0, y0, width, height = self._plot
        x = x0 + width * min(1.0, status.position / self._picture.duration)
        self.canvas.create_line(x, y0, x, y0 + height, fill=theme.TEXT, tags="position")

    # -- window --------------------------------------------------------------------------
    def _closing(self):
        self._cancel.set()
        if self._relayout_job:
            self.win.after_cancel(self._relayout_job)
        self.player.unmonitor(self.feed)
        self._photo = self._live_photo = self._key_photo = None   # its pictures go with the window, on this thread
        self._reading = None
        self.view = None   # and its Tk variable (the choice row lets go of it too)
