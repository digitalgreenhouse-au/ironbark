"""Start the player:  python -m ironbark [album-folder]"""

from __future__ import annotations

import argparse
import datetime
import faulthandler
import logging
import sys
import threading
import tkinter as tk
import traceback

from . import APP_NAME, __version__, appicon, experiments, settings
from .engine import Player
from .ui import PlayerWindow

_crash_file = None  # kept open for the life of the program
_last_logged = None  # the error written last (one caught and re-raised isn't written twice)


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
    try:
        root = tk.Tk()
        root.report_callback_exception = _log_callback_error
        PlayerWindow(root, Player(), folder=args.folder)
    except Exception:
        # A packaged build has no console, and may never pass this to sys.excepthook: log it here.
        _log_error("starting up", *sys.exc_info())
        raise
    root.mainloop()


def _start_crash_log() -> None:
    """The player runs without a console, so errors would vanish. Record them in crash.log
    next to the settings: Python errors in the window code, on any other thread, or not caught
    anywhere, and a traceback of every thread if the program dies outright (faulthandler)."""
    global _crash_file
    try:
        path = settings.settings_path().parent / "crash.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 1_000_000:  # keep it from growing forever
            path.replace(path.with_suffix(".old.log"))
        _crash_file = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        _crash_file = None
        return
    try:   # (only a detail for the log: a settings file it can't read mustn't stop the start)
        on = [e.key for e in experiments.switched_on(settings.load())]
    except Exception:
        on = []
    trying = f" · experiments on: {', '.join(on)}" if on else ""
    _crash_file.write(f"\n--- {APP_NAME} {__version__} started {datetime.datetime.now():%d/%m/%Y %H:%M:%S}"
                      f"{trying} ---\n")
    faulthandler.enable(file=_crash_file, all_threads=True)
    threading.excepthook = _log_thread_error
    sys.excepthook = _log_uncaught


def _log_error(where: str, exc_type, value, tb) -> None:
    """Write a traceback to crash.log (or the console, when there's no log), once per error."""
    global _last_logged
    if value is not None and value is _last_logged:   # (already logged where it was caught)
        return
    _last_logged = value
    text = "".join(traceback.format_exception(exc_type, value, tb))
    stream = _crash_file or sys.stderr
    if stream is not None:
        stream.write(f"{datetime.datetime.now():%H:%M:%S} error {where}:\n{text}")
        stream.flush()


def _log_callback_error(exc_type, value, tb):
    _log_error("in the window code", exc_type, value, tb)


def _log_thread_error(args: threading.ExceptHookArgs):
    if args.exc_type is SystemExit:   # (a thread ending itself on purpose, as Python's own hook allows)
        return
    name = args.thread.name if args.thread is not None else "?"
    _log_error(f"in the {name} thread", args.exc_type, args.exc_value, args.exc_traceback)


def _log_uncaught(exc_type, value, tb):
    """Not caught anywhere: logged, then shown as Python would (on a console, if there is one)."""
    if not issubclass(exc_type, KeyboardInterrupt):
        _log_error("not caught anywhere", exc_type, value, tb)
    sys.__excepthook__(exc_type, value, tb)


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
