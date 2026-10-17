"""The system's request to close — SIGTERM, sent at logout or shutdown, by `kill`, or by a service
manager — runs the player's ordinary close, the same as closing the window: settings are saved and a
device bit-perfect changed (its sample rate or format) is put back. Without this, Python's default
ends the program at once from wherever it is, which can crash Tk's teardown and leaves the device
as bit-perfect set it. Not on Windows (platforms.QUIT_ON_SIGTERM): it sends a windowed program no
such signal.

A signal handler may run between any two Python instructions, Tk's own included, so it touches
nothing of Tk: it sets a flag, and a timer on Tk's thread (`CHECK_MS`) sees it and calls `close`.
"""

from __future__ import annotations

import logging
import signal
import threading

from . import platforms

log = logging.getLogger(__name__)

CHECK_MS = 200   # how often Tk's thread looks for a request (a close starts within this)
CLOSE_SIGNALS = (signal.SIGTERM,) if hasattr(signal, "SIGTERM") else ()


class QuitOnSignal:
    """Watches for SIGTERM while the window is open. `close` is called once, on Tk's thread."""

    def __init__(self, root, close, signals=CLOSE_SIGNALS):
        self.root, self._close = root, close
        self.asked = threading.Event()
        self._previous: dict = {}
        self._job = None
        if not platforms.QUIT_ON_SIGTERM or threading.current_thread() is not threading.main_thread():
            return   # (a handler can only be set from the main thread: in some tests it isn't)
        for number in signals:
            try:
                self._previous[number] = signal.signal(number, self._heard)
            except (OSError, ValueError):   # (not a signal this system can catch)
                log.info("can't watch for signal %s", number, exc_info=True)
        if self._previous:
            self._job = root.after(CHECK_MS, self._check)

    def _heard(self, number, frame):
        self.asked.set()   # (only this: see the module's note)

    def _check(self):
        """Close once per request. The handler stays installed until the window has gone, so a
        second SIGTERM while the close runs (it may wait for a save, or be called off for unsaved
        tag edits) is caught too rather than ending the program at once."""
        self._job = None
        if self.asked.is_set():
            self.asked.clear()
            log.info("asked to close by the system (SIGTERM)")
            self._close()
        try:
            self._job = self.root.after(CHECK_MS, self._check)
        except Exception:  # noqa: BLE001 — the window has gone: nothing to watch for
            pass

    def stop(self):
        """Stop watching: the timer goes and the previous handlers come back."""
        if self._job is not None:
            try:
                self.root.after_cancel(self._job)
            except Exception:  # noqa: BLE001 — the window has gone
                pass
            self._job = None
        for number, previous in self._previous.items():
            try:
                signal.signal(number, previous if previous is not None else signal.SIG_DFL)
            except (OSError, ValueError):
                pass
        self._previous = {}
