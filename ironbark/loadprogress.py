"""What a tab's load says while it runs — the status line's "Opening…", "Looking for music…",
"Reading… 12 of 40 songs (30%)" — and how often it says it.

A folder on a network drive can take a long time to open: listing it is slow, and every song's
tags are read over the wire. The scan (library.scan_folder / scan_album) tells a `Reporter` how far
it has got, from its own thread; the reporter turns that into one short line and hands it on no
more than every `every` seconds (and always at the end of a stage), so the window isn't asked to
repaint for every file.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from . import library

EVERY = 0.15   # seconds between two lines (the window can't usefully show more)


def plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def opening_text(what: str) -> str:
    """Before anything is known: the folder (or file) is being opened — on a sleeping network
    drive, that alone can take a while."""
    return f"Opening “{what}”…"


def loading_text(what: str, stage: str, done: int, total: int) -> str:
    """One line for where a scan has got: `stage` and the counts as library.scan_folder gives them."""
    if stage == library.LOOKING:
        found = f" {plural(done, 'music file')} found" if done else ""
        return f"Looking for music in “{what}”…{found}"
    if stage == library.READING and total:
        percent = int(100 * done / total)
        return f"Reading “{what}”… {done} of {plural(total, 'song')} ({percent}%)"
    return f"Reading “{what}”…"


def album_text(name: str, done: int, total: int) -> str:
    """A tab of many albums read from the files (not the library database): which one it's on."""
    return f"Reading album {done + 1} of {total}: “{name}”…"


class Reporter:
    """`say(text)` is called with each new line, from the scanning thread, at most every `every`
    seconds — except that a stage's last call (done == total, or a new stage) always goes through,
    so the line never stops short of the truth."""

    def __init__(self, what: str, say: Callable[[str], None], every: float = EVERY,
                 clock: Callable[[], float] = time.monotonic):
        self.what = what
        self._say = say
        self._every = every
        self._clock = clock
        self._last_at = -1.0
        self._last_stage: str | None = None
        self.text = ""

    def __call__(self, stage: str, done: int, total: int) -> None:
        now = self._clock()
        final = total and done >= total
        if stage == self._last_stage and not final and now - self._last_at < self._every:
            return
        self._last_stage, self._last_at = stage, now
        self.say(loading_text(self.what, stage, done, total))

    def say(self, text: str) -> None:
        """A line of its own (e.g. "Opening…"), sent at once."""
        if text != self.text:
            self.text = text
            self._say(text)
