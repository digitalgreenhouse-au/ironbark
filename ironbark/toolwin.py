"""What the tool windows share (Now Playing, Meters, PCM stream, Spectrogram, Stereo, Clipping,
Compare, ABX, Test signals). No drawing here.

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
    return bool(window.winfo_viewable())


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
