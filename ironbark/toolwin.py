"""What the tool windows share (Meters, PCM stream, Spectrogram, Stereo, Clipping, Compare, ABX,
Test signals, Tape reels, Lyrics). No drawing here.

- `ToolWindow`: the window itself, made and run the same way by every tool. A subclass sets its
  TITLE, SIZE, MIN_SIZE and the settings key its place is saved under, builds its contents in
  `__init__` after calling `super().__init__`, draws in `_step` (called every REFRESH_MS once
  `start_ticking` is called) and lets go of what it holds in `_closing`.
- `KeepGoing`: a window's refresh loop survives an error in one refresh. The error is reported
  once (crash.log), then skipped, so the window doesn't freeze and the log doesn't fill up.
- `on_screen`: drawing pauses while a window is minimised or hidden (its data still flows).
- One sound at a time: the windows that play their own sound (ABX, Clipping → Hear it, Test
  signals) call `take_sound` first; the window that was playing stops and says so. Each of them
  also pauses the player first (Player.suspend), so nothing ever plays over anything else.
"""

from __future__ import annotations

import tkinter as tk
import traceback
import weakref

from . import APP_NAME, placement, theme, winstate


class KeepGoing:
    """`with keep_going:` around one refresh of a window. An error inside is reported through
    Tk's own error report (crash.log in the app) the first time it happens at that place, and
    after that only skipped; the code after the `with` (lining up the next refresh) always runs."""

    def __init__(self, widget: tk.Misc):
        self._widget = widget
        self._seen: set = set()

    def __enter__(self):
        return self

    def __exit__(self, kind, error, trace) -> bool:
        if kind is None or not issubclass(kind, Exception):
            return False
        where = traceback.extract_tb(trace)[-1] if trace is not None else None
        key = (kind, where.filename if where else "", where.lineno if where else 0)
        if key not in self._seen:
            self._seen.add(key)
            try:
                self._widget.nametowidget(".").report_callback_exception(kind, error, trace)
            except tk.TclError:
                pass   # the app is closing
        return True


def on_screen(window: tk.Misc) -> bool:
    """False while the window is minimised or hidden: nothing it draws can be seen."""
    return winstate.on_screen(window)


class ToolWindow:
    """A tool window: a Toplevel with its title, opening size and smallest size, placed where it
    was last left (prefs[GEOMETRY_KEY], saved as it moves), the dark title bar, Esc and the close
    box closing it. `start_ticking()` runs `_step` every REFRESH_MS (an error in one step is
    reported once and skipped: KeepGoing). `close` stops the loop, calls `_closing` (stop engines,
    let go of the player's feed and any pictures), destroys the window and tells `on_close`."""

    TITLE = ""
    SIZE = (420, 640)            # px at 100%: the size it opens at the first time
    MIN_SIZE = (300, 420)        # px at 100%
    GEOMETRY_KEY = ""            # the settings key its place is kept under ("": not kept)
    REFRESH_MS = 100

    def __init__(self, parent: tk.Misc, prefs: dict, on_close=lambda: None):
        self.prefs, self.on_close = prefs, on_close
        self.win = tk.Toplevel(parent, bg=theme.BG, class_=APP_NAME)
        self.win.title(self.TITLE)
        self.win.geometry(f"{theme.px(self.SIZE[0])}x{theme.px(self.SIZE[1])}")
        self.win.minsize(theme.px(self.MIN_SIZE[0]), theme.px(self.MIN_SIZE[1]))
        if self.GEOMETRY_KEY:
            placement.restore(self.win, prefs.get(self.GEOMETRY_KEY, ""))
        theme.dark_title_bar(self.win)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<Escape>", self._escape)
        self.win.bind("<Configure>", self._moved, add="+")
        self._keep_going = KeepGoing(self.win)
        self._job = None

    # -- the refresh loop -------------------------------------------------------------------------
    def start_ticking(self):
        self._job = self.win.after(self.REFRESH_MS, self._tick)

    def _tick(self):
        self._job = None
        if not self.win.winfo_exists():
            return
        with self._keep_going:
            self._step()
        self._job = self.win.after(self.REFRESH_MS, self._tick)

    def _step(self):
        """One refresh (a subclass draws here)."""

    def on_screen(self) -> bool:
        return on_screen(self.win)

    # -- the window -------------------------------------------------------------------------------
    def _escape(self, event=None):
        self.close()

    def _keeps_place(self) -> bool:
        """Whether the window's place is saved just now (not while it's full screen, say)."""
        return True

    def _moved(self, event):
        if event.widget is self.win and self.GEOMETRY_KEY and self._keeps_place():
            geometry = placement.current(self.win)
            if geometry:
                self.prefs[self.GEOMETRY_KEY] = geometry

    def lift(self):
        winstate.restore(self.win)
        self.win.lift()

    def _closing(self):
        """Let go of what the window holds (a subclass: stop its engine, unmonitor the player, drop
        its pictures), before the window is destroyed."""

    def close(self):
        if self._job:
            self.win.after_cancel(self._job)
            self._job = None
        self._closing()
        self.win.destroy()
        self.on_close()


_sound: weakref.ref | None = None   # the window whose own sound plays (or played last)


def take_sound(owner) -> None:
    """`owner` (a window) is about to play its own sound: the window playing before it stops,
    through its `sound_taken()`."""
    global _sound
    previous = _sound() if _sound is not None else None
    _sound = weakref.ref(owner)
    if previous is not None and previous is not owner:
        try:
            previous.sound_taken()
        except tk.TclError:
            pass   # its window has gone


def give_back_sound(owner) -> None:
    """`owner` is closing (its sound already stopped)."""
    global _sound
    if _sound is not None and _sound() is owner:
        _sound = None
