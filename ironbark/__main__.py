"""Start the player:  python -m ironbark [music files, folders, playlists or CUE sheets…]

A file named here (a double-click on one, or Explorer's "Open with") opens in a tab and plays at
once: asking for it is the request. Folders open as tabs. If Ironbark is already running, all of
it goes to that copy instead (instance.py), which comes to the front, and this launch ends."""

from __future__ import annotations

import argparse
import datetime
import faulthandler
import logging
import sys
import threading
import time
import tkinter as tk
import traceback

from . import APP_NAME, __version__, appicon, crash, experiments, instance, logs, settings

log = logging.getLogger("ironbark.main")

# The player itself is imported once it's known this launch opens a window (`_player_classes`):
# a second launch hands its files over and exits without loading it all first.
Player = None
PlayerWindow = None

_crash_file = None  # kept open for the life of the program
_last_logged = None  # the error written last (one caught and re-raised isn't written twice)


class _Parser(argparse.ArgumentParser):
    """The packaged program has no console: --help, --version and a mistake in the options are
    shown in a message box instead of vanishing."""

    def _print_message(self, message, file=None):
        if message and (file if file is not None else sys.stdout) is None:
            crash.tell_native(APP_NAME, message.strip(), warning=False)
            return
        super()._print_message(message, file)

    def error(self, message):
        if sys.stderr is None:
            crash.tell_native(APP_NAME, f"{message}\n\n{self.format_usage().strip()}")
            self.exit(2)
        super().error(message)


def main(argv: list[str] | None = None) -> None:
    parser = _Parser(prog="ironbark", description=f"{APP_NAME}: a lossless music player that proves what it plays.")
    parser.add_argument("paths", nargs="*", metavar="PATH",
                        help="music files, folders, playlists (.m3u, .m3u8) or CUE sheets to open; "
                             "a file plays at once")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("--self-check", nargs="?", const="", metavar="REPORT",
                        help="check this copy works (no sound, nothing saved), optionally writing "
                             "the report to a file; exit code 0 = passed")
    args = parser.parse_args(argv)
    if args.self_check is not None:   # (never looks for, or talks to, a running player)
        _enable_crisp_text_on_windows()
        from . import selfcheck

        sys.exit(selfcheck.run(args.self_check or None, hidden=True))   # (its windows never on screen)

    paths = instance.absolute(args.paths)
    here = _claim(paths)
    if here is None:
        return   # the running copy has them
    try:
        _run(paths, here)
    finally:
        here.close()


def _claim(paths: list[str]) -> instance.Instance | None:
    """This launch's place. None when another copy is running and has taken `paths` (it comes to
    the front). A copy that was closing just then is waited for; if another launch takes its place
    first (several files opened at once), that one is handed the files instead."""
    began = time.monotonic()
    here = instance.claim()
    while not here.first:
        if here.forward(paths):
            return None
        here = instance.claim()
        if time.monotonic() - began >= instance.FORWARD_TIMEOUT:
            break   # (a copy there all along that never answered)
        time.sleep(0.05)
    if here.first:
        return here
    crash.tell_native(f"{APP_NAME} isn't answering",
                      f"{APP_NAME} is already open, but didn't answer. If it has stopped responding, close it "
                      f"in Task Manager, then start {APP_NAME} again.")
    sys.exit(1)


def _player_classes():
    global Player, PlayerWindow
    if Player is None:
        from .engine import Player
    if PlayerWindow is None:
        from .ui import PlayerWindow
    return Player, PlayerWindow


def _run(paths: list[str], here: instance.Instance) -> None:
    settings.carry_over_old_folder()   # (0.26.0) renamed: bring the old settings across first
    _start_crash_log()
    _enable_crisp_text_on_windows()
    # (0.23.0) Before any window: Windows groups the player on the taskbar as itself (its own
    # icon), not as python.exe. Media controls (SMTC) take the same ID.
    appicon.set_app_id()
    root = None
    try:
        player_class, window_class = _player_classes()
        root = tk.Tk()
        root.report_callback_exception = _log_callback_error
        window = window_class(root, player_class(), paths=paths, requests=here.requests,
                              on_close=here.stop_listening)
    except Exception:
        # A packaged build has no console, and may never pass this to sys.excepthook: log it here,
        # then say so in a box the user can copy the details from.
        error = sys.exc_info()
        _log_error("starting up", *error)
        _close_half_built(root)
        crash.startup_failed(*error)
        if getattr(sys, "frozen", False):
            sys.exit(1)   # (the box said it all: no second, raw one from the packaged program)
        raise
    collector = getattr(window, "collector", None)
    if collector is not None:   # the program ends with the window: the cycle collector stays off till then
        collector.restore_on_close = False
    crash.attach(root, before=getattr(window, "_for_prompt", None))
    threading.Thread(target=_sweep_tag_copies, name="tag-copies-sweep", daemon=True).start()
    root.mainloop()


def _close_half_built(root) -> None:
    """A window half-built when start-up failed goes before the message box: Windows' box runs a
    message loop of its own, and Tk mustn't be answering events inside it."""
    if root is None:
        return
    try:
        root.destroy()
    except Exception:  # noqa: BLE001 — it may never have been a working window
        pass


def _sweep_tag_copies() -> None:
    """Copies an unfinished tag save left beside the music (tag_editor.sweep_leftover_copies)."""
    try:
        from . import tag_editor

        removed = tag_editor.sweep_leftover_copies()
        if removed:
            log.info("deleted %d copies left by an unfinished tag save", removed)
    except Exception:  # noqa: BLE001 — tidying up must never stop the player
        log.warning("couldn't tidy up after an unfinished tag save", exc_info=True)


def _start_crash_log() -> None:
    """The player runs without a console, so errors would vanish. Record them in crash.log
    next to the settings: Python errors in the window code, on any other thread, or not caught
    anywhere, and a traceback of every thread if the program dies outright (faulthandler).
    The running log, app.log, starts here too (logs.py)."""
    global _crash_file
    folder = settings.settings_path().parent
    logs.setup(folder)
    try:   # (only details for the logs: a settings file it can't read mustn't stop the start)
        prefs = settings.load()
        on = [e.key for e in experiments.switched_on(prefs)]
    except Exception:
        prefs, on = {}, []
    logs.set_detailed(bool(prefs.get("detailed_logging")))
    try:
        path = folder / "crash.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 1_000_000:  # keep it from growing forever
            path.replace(path.with_suffix(".old.log"))
        _crash_file = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        _crash_file = None
        return
    trying = f" · experiments on: {', '.join(on)}" if on else ""
    _crash_file.write(f"\n--- {APP_NAME} {__version__} started {datetime.datetime.now():%d/%m/%Y %H:%M:%S}"
                      f"{trying} ---\n")
    faulthandler.enable(file=_crash_file, all_threads=True)
    threading.excepthook = _log_thread_error
    sys.excepthook = _log_uncaught


def _log_error(where: str, exc_type, value, tb) -> None:
    """Write a traceback to crash.log (or the console, when there's no log), once per error; it
    goes to app.log too."""
    global _last_logged
    if value is not None and value is _last_logged:   # (already logged where it was caught)
        return
    _last_logged = value
    text = "".join(traceback.format_exception(exc_type, value, tb))
    stream = _crash_file or sys.stderr
    if stream is not None:
        stream.write(f"{datetime.datetime.now():%H:%M:%S} error {where}:\n{text}")
        stream.flush()
    if logging.getLogger().handlers:   # (with none, logging would print it to the console a second time)
        log.error("error %s", where, exc_info=(exc_type, value, tb))


def _log_callback_error(exc_type, value, tb):
    _log_error("in the window code", exc_type, value, tb)
    crash.report("in the window code", exc_type, value, tb)


def _log_thread_error(args: threading.ExceptHookArgs):
    if args.exc_type is SystemExit:   # (a thread ending itself on purpose, as Python's own hook allows)
        return
    name = args.thread.name if args.thread is not None else "?"
    _log_error(f"in the {name} thread", args.exc_type, args.exc_value, args.exc_traceback)
    crash.report(f"in the {name} thread", args.exc_type, args.exc_value, args.exc_traceback)


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
