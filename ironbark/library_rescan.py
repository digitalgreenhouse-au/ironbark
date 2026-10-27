"""The ↻ button beside the Library tab's search box (and F5 in the player window): look through the
library folders now for music added, changed or gone, and take it in — the look Ironbark makes at
start-up, without restarting. Only what changed is read (names, sizes and modified times are
compared; tags are read for the files that changed), so it's quick for a library already read.

What follows is what follows any look: the Library tab takes in the change by itself
(ui._follow_library, within a few seconds), and new albums are marked and "New music: …" said
(ui._watch_reports). This says how the look went — what changed, that nothing did, or why it
couldn't look — and greys the button while it looks. In the Library's views (Albums, Artists…)
their own ↻ is the views' Rescan, which shows its progress in the view's status line."""

from __future__ import annotations

import weakref
from pathlib import Path

from . import library_view, offthread

LOOKING = "Looking for added or changed music in the library…"
NOTE_MS = 8000

_busy: weakref.WeakSet = weakref.WeakSet()   # the windows whose look is under way


def busy(win) -> bool:
    return win in _busy


def rescan(win) -> None:
    """The button / F5 (`win`: the player window)."""
    library = getattr(win, "_library", None)
    if library is not None and (library.shown or _has_focus(library)):   # (their own Rescan, with its progress)
        library.rescan()
        return
    index = win._index()
    if busy(win) or index.indexer.running:
        win._set_notice("Already looking through the library…", 4000)
        return
    if not win._library_roots():
        listed = [Path(folder) for folder in win.prefs.get("library_folders") or [] if isinstance(folder, str)]
        win._set_notice(library_view.unsearched_note(listed, win.prefs, "Not read") or library_view.NO_FOLDERS_TEXT,
                        NOTE_MS)
        return
    look = win.watcher

    def work():   # (the watcher and the index only: nothing of the window's, nothing of Tk's)
        try:
            report = look.check(new_albums=True)
            look.results.put(report)   # (shown as any look's is: new albums marked, "New music: …")
            return report
        finally:
            index.db.release()

    def then(result):
        _busy.discard(win)
        paint(win)
        if isinstance(result, offthread.Error):
            win._set_notice(f"Couldn't look through the library: {result.error}", NOTE_MS)
        elif not result.news or win._notice == LOOKING:   # ("New music: …" says more, and leads to it)
            win._set_notice(summary(result), NOTE_MS)

    _busy.add(win)
    paint(win)
    win._set_notice(LOOKING, 0)
    win.work.run(work, then, name="library-rescan")


def _has_focus(library) -> bool:
    """The keyboard is in the Library's views (F5 pressed there)."""
    try:
        focus = library.frame.focus_get()
    except Exception:   # noqa: BLE001 — (KeyError for a menu's pop-down, TclError for a window going: not the views)
        return False
    return focus is not None and str(focus).startswith(str(library.frame))


def summary(report) -> str:
    """How a look went, for the status line."""
    diff = report.diff
    if report.error and diff.nothing:
        return report.error
    if diff.cancelled:
        return "The look at the library was stopped part-way: press ↻ to carry on"
    if diff.nothing and not diff.waiting:
        text = "The library is up to date: nothing added, changed or gone"
    else:
        text = f"Library updated: {diff.note()}" if not diff.nothing else f"Nothing to take in yet: {diff.note()}"
        if diff.waiting:   # (left for the next look while they're still being copied)
            text += " — press ↻ again once they're copied"
    if report.error:
        text += f" · {report.error}"
    return text


def paint(win) -> None:
    """The Library tab's ↻: greyed while a look is under way."""
    button = getattr(getattr(win, "playlist", None), "rescan_button", None)
    if button is not None and button.winfo_exists():
        button.set_enabled(not busy(win))
