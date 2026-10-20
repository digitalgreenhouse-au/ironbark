"""Settings → Library → Check folders…: every library folder checked (netcheck.py) on a worker —
the network folders above all: whether the server and the folder answer, and which way of listing
works. The report is saved in the settings folder, beside the log (REPORT_NAME), and opened; the
status line says how it went. Nothing is changed or sent."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from . import netcheck, offthread, platforms, settings

log = logging.getLogger(__name__)

REPORT_NAME = "library-folders-check.txt"
_running = False   # (one check at a time: a server that doesn't answer can keep one going a while)


def listed_folders(prefs: dict) -> list[Path]:
    """The library folders the settings list (each once), network ones included — the check says
    when one isn't read because Local network access is off."""
    folders = prefs.get("library_folders") or ([prefs["library_folder"]] if prefs.get("library_folder") else [])
    seen, found = set(), []
    for folder in folders:
        if isinstance(folder, str) and folder and os.path.normcase(folder) not in seen:
            seen.add(os.path.normcase(folder))
            found.append(Path(folder))
    return found


def report_path() -> Path:
    return settings.settings_path().parent / REPORT_NAME


def run(win) -> None:
    """The button (`win`: the player window)."""
    global _running
    if _running:
        win._set_notice("Checking the library folders already…", 4000)
        return
    folders = listed_folders(win.prefs) or [win._library_root()]   # (none set: the system's Music folder)
    prefs = dict(win.prefs)   # (a copy of its own for the thread)
    path = report_path()

    def work():
        text, checks = netcheck.report([str(folder) for folder in folders], prefs)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        log.info("library folders check: %s", netcheck.summary(checks))
        return checks

    def then(result):
        global _running
        _running = False
        if isinstance(result, offthread.Error):
            win._set_notice(f"Couldn't check the library folders: {result.error}", 8000)
            return
        opened = open_file(path)
        win._set_notice(f"{netcheck.summary(result)} · "
                        + ("the report is open" if opened else f"the report is {path}"), 15000)

    _running = True
    win._set_notice("Checking the library folders… (a server that's asleep can take a while)", 0)
    win.work.run(work, then, name="folders-check")


def open_file(path: Path) -> bool:
    """Open a file of our own with the system's program for it (Notepad, TextEdit…)."""
    try:
        if platforms.IS_WINDOWS:
            os.startfile(str(path))   # noqa: S606 — our own report, opened for the user
        else:
            subprocess.Popen(["open" if platforms.IS_MAC else "xdg-open", str(path)])
    except OSError:
        log.warning("couldn't open %s", path, exc_info=True)
        return False
    return True
