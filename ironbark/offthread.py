"""Work off Tk's thread, and the one way back onto it.

Tk is single-threaded: nothing that touches a widget may run anywhere but the thread Tk runs on.
Anything that reads the disk or the library database for the window is done here instead, and
its answer handed back on Tk's thread:

    self.work.run(read_the_songs, then=show_them, name="read-songs")

`run` starts `work()` on a thread of its own and, once it returns, calls `then(value)` on Tk's
thread (an error in `work` is logged and reported to `then` as an `Error`, so a window never waits
for an answer that isn't coming). `post(fn, *args)` is the same handover for anything else: a
worker that wants to say how far it has got calls `post` and carries on. Both go through one queue
that the window drains every DRAIN_MS on Tk's own timer, so there is one timer for all of it rather
than one polling loop per request.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import tkinter as tk
from collections.abc import Callable

log = logging.getLogger(__name__)

DRAIN_MS = 50   # how often the answers waiting are taken onto Tk's thread


class Error:
    """What `then` gets when `work` raised: the exception, so the window can say so."""

    def __init__(self, error: BaseException):
        self.error = error

    def __repr__(self) -> str:
        return f"Error({self.error!r})"


class Background:
    def __init__(self, root: tk.Misc):
        self.root = root
        self._queue: queue.Queue = queue.Queue()
        self._job = root.after(DRAIN_MS, self._drain)
        self._closed = False

    def run(self, work: Callable[[], object], then: Callable[[object], None] | None = None, *,
            name: str = "work") -> threading.Thread:
        """`work()` on its own thread; `then(value)` on Tk's thread once it returns."""
        def go():
            try:
                value = work()
            except Exception as error:
                log.exception("%s failed", name)
                value = Error(error)
            if then is not None:
                self.post(then, value)

        thread = threading.Thread(target=go, name=name, daemon=True)
        thread.start()
        return thread

    def post(self, fn: Callable, *args) -> None:
        """Run `fn(*args)` on Tk's thread, soon (from any thread)."""
        self._queue.put((fn, args))

    def _drain(self):
        """Run what's waiting, each on its own: one that raises is reported as Tk reports an error in
        any callback, and the rest still run — and the drain always comes round again, so one slip
        never leaves every later answer undelivered for the rest of the session."""
        self._job = None
        if self._closed:
            return
        try:
            while True:
                try:
                    fn, args = self._queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    fn(*args)
                except Exception:
                    if self._closed:
                        return   # (the window went while this was waiting)
                    self.root.report_callback_exception(*sys.exc_info())
        finally:
            if not self._closed:
                self._job = self.root.after(DRAIN_MS, self._drain)

    def drain_now(self) -> None:
        """Take what's waiting now (tests, and the last moments before closing)."""
        if self._job is not None:
            self.root.after_cancel(self._job)
            self._job = None
        self._drain()

    def close(self) -> None:
        self._closed = True
        if self._job is not None:
            self.root.after_cancel(self._job)
            self._job = None
