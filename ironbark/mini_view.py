"""Mini mode (0.19.0): the player shrunk to a small strip for a corner of the screen — the song's
title and artist, previous / play / next, a slim seek bar, the status square and (0.25.0) a gear
for Settings. (0.25.0: no cover in the strip — it's text and buttons only.)

It's the same window as the full player, not a second one: switching hides the tab strip and
everything under it, shows this strip and moves the window to where the strip was last left
(each keeps its own position and size). So the music never notices, and everything tied to the
window carries on — Keep on top, the media keys and Windows' media panel (they're attached to
this window), drops from Explorer. ui.PlayerWindow does the switching (`toggle_mini`: Settings,
key Z, or double-clicking the full window's cover); this module only draws the strip and reports
clicks. (0.25.0) The strip has its own Keep on top (on at first) — ui.PlayerWindow swaps it in and
out with the mode — and its gear opens Settings under the strip (the window grows for it, and
goes back to the strip's size when Settings closes).

Like the rest of the app it takes every colour from theme.py, so it follows the Light / Dark
theme. (0.20.0) The seek bar is controls.Slider, the same drawn slider as the full window's.
(0.25.0) The buttons are only as big as their icons — clicks above or below them drag the strip
instead of pressing a button (they used to fill the strip's whole height).

One look with the full window: play is the same drawn green button (smaller), previous and next
the same drawn glyphs, the other buttons round discs under the pointer, and the status a small
drawn dot — so there is one play button style in the app, not three. Each button's tooltip names
its key too, from the one key table (keys.py), as the full window's do. (0.41.1) The glyphs are weighed
against each other: previous / next a little smaller than the full window's (SKIP_GLYPH), beside the
strip's smaller play button, and the gear / full player / close at the strip's icon size (theme.ICON),
so no button looks drawn with a thicker pen than its neighbours.
"""

from __future__ import annotations

import sys
import tkinter as tk
from typing import Callable

from . import chrome, controls, keys, shapes, theme
from .tabbar import RoundIconButton, Tooltip

DEFAULT_SIZE = (380, 76)    # px at 100%: the strip's size the first time it's opened
MIN_SIZE = (260, 64)        # px at 100%: smallest the strip can be made (title, artist and seek bar still fit)
SEEK_HEIGHT = 12            # px at 100%: the seek bar's strip (the bar itself is thinner)
PLAY_DIAMETER = 32          # px at 100%: the strip's play button
BUTTON = 28                 # px at 100%: previous / next: their round disc…
SMALL_BUTTON = 24           # …and Settings, full player and close
SKIP_GLYPH = 10             # px at 100%: previous / next glyphs — lighter than the full window's (12), to sit
                            # beside the strip's smaller play button instead of outweighing it
STATUS_DOT = 8              # px at 100%: the status dot


def double_click_ms() -> int:
    """Windows' own double-click time (Settings → Mouse), so a single click on the cover waits
    no longer than it must before acting. 500 ms when it can't be asked: Windows' default (Microsoft
    Learn, SetDoubleClickTime: "the default double-click time of 500 milliseconds")."""
    if sys.platform == "win32":
        try:
            import ctypes

            return int(ctypes.windll.user32.GetDoubleClickTime()) or 500
        except (AttributeError, OSError):
            pass
    return 500


def click_or_double(widget: tk.Misc, on_click: Callable[[], None], on_double: Callable[[], None]) -> None:
    """One click → `on_click` (once it's clear no second click is coming); a double click →
    `on_double` only. Used for the cover: click = Now Playing, double-click = mini / full."""
    job = [None]

    def fire():
        job[0] = None
        on_click()

    def click(_event):
        if job[0]:
            widget.after_cancel(job[0])
        job[0] = widget.after(double_click_ms(), fire)

    def double(_event):
        if job[0]:
            widget.after_cancel(job[0])
            job[0] = None
        on_double()
        return "break"

    widget.bind("<Button-1>", click)
    widget.bind("<Double-Button-1>", double)


class MiniView(tk.Frame):
    """The mini strip. The window shows it in place of the full layout; `show(...)` is called by
    the window's poll with what to display (the same song, time and status as the full view)."""

    def __init__(self, parent, root: tk.Tk, *, on_play: Callable[[], None], on_previous: Callable[[], None],
                 on_next: Callable[[], None], on_seek: Callable[[float], None], on_full: Callable[[], None],
                 on_details: Callable[[], None], on_quit: Callable[[], None],
                 on_settings: Callable[[], None] = lambda: None):
        super().__init__(parent, bg=theme.BG)
        self.app_root = root   # (not _root: that name is a Tk method and would shadow it)
        pad = theme.px(theme.GAP)

        # Right-hand end: close, back to full, Settings, the status dot, then the transport.
        # (0.25.0) Packed without fill="y": each button is just its icon, centred on the strip's
        # height, so only the icon takes a click (the strip around it drags the window).
        def small(name, command, label, **options):
            return RoundIconButton(self, root, name, command, size=theme.ICON, diameter=SMALL_BUTTON,
                                   width=SMALL_BUTTON, bg="BG", label=label, **options)

        self.close_button = small("close", on_quit, f"Close ({keys.hint('quit')})", hover="CLOSE_HOVER",
                                  pressed="CLOSE_HOVER", hover_fg="ON_ACCENT")
        self.close_button.pack(side="right", padx=(0, theme.px(theme.GAP)))
        self.full_button = small("full", on_full, f"Full player ({keys.hint('mini')})")
        self.full_button.pack(side="right", padx=(0, theme.px(theme.GAP_HAIR)))
        self.settings_button = small("settings", on_settings, f"Settings ({keys.hint('settings')})")
        self.settings_button.pack(side="right", padx=(0, theme.px(theme.GAP_HAIR)))
        self.status_dot = tk.Label(self, bg=theme.BG, bd=0, cursor="hand2")
        self.status_dot.pack(side="right", padx=theme.pad(theme.GAP, theme.GAP))
        self.status_dot.bind("<Button-1>", lambda e: on_details())
        self.status_square = self.status_dot   # (its old name)
        self._status_detail = ""
        self._status_colour = None
        Tooltip(self.status_dot, lambda: self._status_detail)
        self._paint_status("BG")
        self.next_button = RoundIconButton(self, root, "next", on_next, diameter=BUTTON, width=BUTTON, bg="BG",
                                           drawn="next", glyph=SKIP_GLYPH, label=f"Next ({keys.hint('next')})")
        self.next_button.pack(side="right")
        self.play_button = controls.PlayButton(self, on_play, diameter=PLAY_DIAMETER,
                                               label=f"Play / pause ({keys.hint('play')})")
        self.play_button.pack(side="right", padx=max(0, theme.px(theme.GAP_SMALL) - self.play_button.margin))
        self.previous_button = RoundIconButton(self, root, "prev", on_previous, diameter=BUTTON, width=BUTTON,
                                               bg="BG", drawn="prev", glyph=SKIP_GLYPH,
                                               label=f"Previous ({keys.hint('previous')})")
        self.previous_button.pack(side="right")

        # The rest: title, artist (or a passing note), the seek bar.
        text = tk.Frame(self, bg=theme.BG)
        text.pack(side="left", fill="both", expand=True, padx=(theme.px(theme.GAP_LARGE), 0), pady=pad)
        self.title_label = tk.Label(text, text="", bg=theme.BG, fg=theme.TEXT, anchor="w",
                                    font=theme.font(theme.SIZE_BODY, "bold"))
        self.title_label.pack(fill="x")
        self.sub_label = tk.Label(text, text="", bg=theme.BG, fg=theme.MUTED, anchor="w",
                                  font=theme.font(theme.SIZE_SMALL))
        self.sub_label.pack(fill="x")
        self.seek = controls.Slider(text, on_release=on_seek, height=SEEK_HEIGHT)
        self.seek.pack(side="bottom", fill="x")
        Tooltip(self.title_label, lambda: self.title_label.cget("text"))   # a long title is cut off
        # Tab follows the stacking order, which is the order they were made (right to left, for the
        # packing): raised left to right, Tab goes seek bar, previous, play, next … and Close last.
        for widget in (text, self.previous_button, self.play_button, self.next_button, self.status_dot,
                       self.settings_button, self.full_button, self.close_button):
            self.tk.call("raise", str(widget))   # (a canvas's own lift() raises its items instead)

        # No title bar in mini mode either: the strip's empty parts drag the window.
        for widget in (self, text, self.title_label, self.sub_label):
            widget.bind("<ButtonPress-1>", lambda e: chrome.start_move(self.app_root))
        theme.on_change(self._palette_changed)

    def _palette_changed(self, _palette):
        colour, self._status_colour = self._status_colour, None   # (the same role: a new colour)
        self._paint_status(colour or "BG")

    # -- what's shown --------------------------------------------------------------------------
    def show(self, *, title: str, artist: str, position: float, duration: float, playing: bool,
             status_colour: str, status_detail: str, notice: str = "", band: tuple = ()):
        """Bring the strip up to date (cheap when nothing changed). `notice` (e.g. "Volume 40%")
        stands in for the artist line while it's set; `band` is what of a network song is downloaded
        (the seek bar's faint band)."""
        sub = notice or artist
        if self.title_label.cget("text") != title:
            self.title_label.configure(text=title)
        if self.sub_label.cget("text") != sub:
            self.sub_label.configure(text=sub)
        self.seek.set(position, duration)
        self.seek.set_band(band)
        self.play_button.set_playing(playing)
        self._paint_status(status_colour)
        self._status_detail = status_detail

    def _paint_status(self, colour: str):
        """The status dot in `colour` (a role, or the background's colour: no dot)."""
        if colour == self._status_colour:
            return
        self._status_colour = colour
        self._status_photo = shapes.disc_photo(self.status_dot, theme.px(STATUS_DOT), colour, "BG")
        self.status_dot.configure(image=self._status_photo, bg=theme.BG)
