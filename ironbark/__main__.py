"""Start the player:  python -m ironbark [album-folder]"""

from __future__ import annotations

import argparse
import datetime
import faulthandler
import logging
import sys
import tkinter as tk
import traceback

from . import APP_NAME, __version__, appicon, experiments, settings
from .engine import Player
from .ui import PlayerWindow

_crash_file = None  # kept open for the life of the program


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ironbark", description=APP_NAME)
    parser.add_argument("folder", nargs="?", help="album folder to open and play")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("--self-check", nargs="?", const="", metavar="REPORT",
                        help="check this copy works (no sound, nothing saved), optionally writing "
                             "the report to a file; exit code 0 = passed")
    args = parser.parse_args(argv)
    if args.self_check is not None:
        _enable_crisp_text_on_windows()
        from . import selfcheck

        sys.exit(selfcheck.run(args.self_check or None))

    settings.carry_over_old_folder()   # (0.26.0) renamed: bring the old settings across first
    _start_crash_log()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s",
                        stream=_crash_file or sys.stderr)
    _enable_crisp_text_on_windows()
    # (0.23.0) Before any window: Windows groups the player on the taskbar as itself (its own
    # icon), not as python.exe. Media controls (SMTC) take the same ID.
    appicon.set_app_id()
    root = tk.Tk()
    root.report_callback_exception = _log_callback_error
    PlayerWindow(root, Player(), folder=args.folder)
    root.mainloop()


def _start_crash_log() -> None:
    """The player runs without a console, so errors would vanish. Record them in crash.log
    next to the settings: Python errors in the window code, and a traceback of every thread
    if the program dies outright (faulthandler)."""
    global _crash_file
    try:
        path = settings.settings_path().parent / "crash.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 1_000_000:  # keep it from growing forever
            path.replace(path.with_suffix(".old.log"))
        _crash_file = open(path, "a", encoding="utf-8", buffering=1)
        on = [e.key for e in experiments.switched_on(settings.load())]
        trying = f" · experiments on: {', '.join(on)}" if on else ""
        _crash_file.write(f"\n--- {APP_NAME} {__version__} started {datetime.datetime.now():%d/%m/%Y %H:%M:%S}"
                          f"{trying} ---\n")
        faulthandler.enable(file=_crash_file, all_threads=True)
    except OSError:
        _crash_file = None


def _log_callback_error(exc_type, value, tb):
    text = "".join(traceback.format_exception(exc_type, value, tb))
    stream = _crash_file or sys.stderr
    if stream is not None:
        stream.write(f"{datetime.datetime.now():%H:%M:%S} error in the window code:\n{text}")
        stream.flush()


def _enable_crisp_text_on_windows() -> None:
    """Without this, Windows stretches the window on high-DPI screens and it looks blurry."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass


if __name__ == "__main__":
    main()
