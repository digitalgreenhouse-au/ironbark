"""Python's cycle collector, run only on Tk's thread.

Python frees most objects the moment they're no longer used, but objects that refer to each other
in a loop (a closed window and its widgets, a widget and a method bound to it) wait for the cycle
collector, which normally runs on whichever thread happens to be allocating when it's due: often
a decoder or analysis thread. If what it frees there is a Tk object (a widget, a variable, a
picture, a whole closed Tk), Tk is called from the wrong thread, which can abort the program
("Windows fatal exception 0x80000003") or fail ("main thread is not in main loop").

So once the window is up, the automatic collector is switched off and this runs it instead, from
Tk's own event loop: the young objects whenever there are enough of them to be worth it (what the
automatic collector would have done), and everything every FULL_EVERY_S. Each run waits for a
quiet moment (Tk's idle time), so it never delays a click or a redraw already waiting.
"""

from __future__ import annotations

import gc
import logging
import time

log = logging.getLogger(__name__)

TICK_MS = 1000        # how often it looks
FULL_EVERY_S = 30.0   # a full collection at least this often


class Collector:
    def __init__(self, root, tick_ms: int = TICK_MS, full_every_s: float = FULL_EVERY_S):
        self.root = root
        self.tick_ms, self.full_every_s = tick_ms, full_every_s
        self._job = None
        self._was_enabled = gc.isenabled()
        self._next_full = time.monotonic() + full_every_s
        self.collections = 0   # runs made (for tests)
        self.running = False
        # Switch the automatic collector back on when stopped (a window in a longer-lived program).
        # The player turns this off: it ends with its window, and what's left is then freed at exit,
        # on this thread, rather than by whichever thread happens to be running in the meantime.
        self.restore_on_close = True

    def start(self) -> None:
        """Take over from the automatic collector (put back by `stop`, or when the window closes)."""
        if self.running:
            return
        self.running = True
        self._was_enabled = gc.isenabled()
        gc.disable()
        self.root.bind("<Destroy>", self._destroyed, add="+")
        self._job = self.root.after(self.tick_ms, self._tick)

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        if self._job is not None:
            try:
                self.root.after_cancel(self._job)
            except Exception:  # noqa: BLE001 — the window may already be gone
                pass
            self._job = None
        if self._was_enabled and self.restore_on_close:
            gc.enable()

    def _destroyed(self, event):
        if event.widget is self.root:
            self.stop()

    def _tick(self):
        self._job = None
        if not self.running:
            return
        try:
            self._job = self.root.after_idle(self.collect)
        except Exception:  # noqa: BLE001 — the window has gone
            self.stop()

    def collect(self, full: bool | None = None) -> None:
        """One run, here on Tk's thread; then the next is booked."""
        self._job = None
        try:
            now = time.monotonic()
            if full or (full is None and now >= self._next_full):
                gc.collect()
                self._next_full = now + self.full_every_s
                self.collections += 1
            elif full is None and gc.get_count()[0] >= gc.get_threshold()[0]:
                gc.collect(0)
                self.collections += 1
        except Exception:  # noqa: BLE001 — a finaliser that raises mustn't stop the collecting
            log.warning("garbage collection failed", exc_info=True)
        finally:
            if self.running:
                try:
                    self._job = self.root.after(self.tick_ms, self._tick)
                except Exception:  # noqa: BLE001
                    self.stop()
