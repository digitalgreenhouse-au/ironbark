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


def elapsed_text(seconds: float) -> str:
    """How long something has been going: "0:42", "12:05", "1:02:05"."""
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


ETA_AFTER_S = 20.0   # a look's reading says how long the rest will take once it has read this long…
ETA_AFTER_SHARE = 0.01   # …and this much of it (the first files, and starting the workers, are slower)


def left_text(seconds: float) -> str:
    """How long something has left, roughly: "less than a minute left", "about 12 min left",
    "about 1 h 20 min left"."""
    minutes = round(seconds / 60)
    if minutes < 1:
        return "less than a minute left"
    if minutes < 60:
        return f"about {minutes} min left"
    hours, minutes = divmod(minutes, 60)
    return f"about {hours} h {minutes} min left" if minutes else f"about {hours} h left"


def library_text(progress, now: float, what: str = "Building the library") -> str:
    """One line for where a library look has got (`progress`: an indexer.Progress), with the time
    it has taken so far — and, once the reading is well under way, about how long the rest will
    take — so a big library's first look (minutes, or hours on a very large drive) is plainly
    still going: "Building the library: looking through your folders… 1,234 folders · 0:42",
    "Building the library: reading songs… 5,000 of 80,000 (6%) · 3:10 · about 45 min left"."""
    phase, done, total = progress.phase, progress.done, progress.total
    if phase == "looking":
        text = f"{what}: looking through your folders… {plural(done, 'folder')}" if done else \
            f"{what}: looking through your folders…"
    elif phase in ("reading", "upgrading") and total:
        verb = "reading songs" if phase == "reading" else "bringing the old index across"
        text = f"{what}: {verb}… {done:,} of {plural(total, 'file')} ({int(100 * done / total)}%)"
    elif phase == "reading":
        text = f"{what}: reading songs…"
    elif phase == "tidying":
        text = f"{what}: tidying up…"
    else:
        text = f"{what}…"
    if progress.started and phase:
        text += f" · {elapsed_text(now - progress.started)}"
    reading_for = now - progress.since if progress.since else 0.0
    if (phase == "reading" and 0 < done < total and reading_for >= ETA_AFTER_S
            and done >= total * ETA_AFTER_SHARE):
        text += f" · {left_text(reading_for / done * (total - done))}"
    return text


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
