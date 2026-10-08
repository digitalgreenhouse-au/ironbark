"""The player's log: app.log beside the settings. No UI code.

Warnings and errors are written by default; Settings → Help → "Detailed logging" adds the
player's own notes of what it's doing (INFO and DEBUG), for tracking down a problem. Other
libraries stay at warnings either way. The file is kept small: at MAX_BYTES it moves to app.log.1
(app.log.2 …) and the oldest goes, so FILES files at most.

crash.log is separate and stays: it is where a hard crash's traceback of every thread lands
(faulthandler writes straight into it), and every error is written there too.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

FILE_NAME = "app.log"
MAX_BYTES = 1_000_000
FILES = 3              # app.log and two older ones
FORMAT = "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"
DATE_FORMAT = "%d/%m/%Y %H:%M:%S"
PACKAGE_LOGGER = "ironbark"

_handler: RotatingFileHandler | None = None


def setup(folder: Path, detailed: bool = False) -> Path | None:
    """Start writing app.log in `folder` (replacing an earlier set-up). Returns its path, or None
    when the folder can't be written to (the player then runs without one)."""
    shutdown()
    global _handler
    path = Path(folder) / FILE_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # (errors: a file name Windows allows but UTF-8 can't hold is written escaped, not dropped)
        handler = RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=FILES - 1, encoding="utf-8",
                                      errors="backslashreplace")
    except OSError:
        return None
    handler.setFormatter(logging.Formatter(FORMAT, DATE_FORMAT))
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.WARNING:
        root.setLevel(logging.WARNING)
    logging.captureWarnings(True)   # Python's own warnings (deprecations…) too
    _handler = handler
    set_detailed(detailed)
    return path


def set_detailed(on: bool) -> None:
    """Detailed logging on (the player's INFO and DEBUG notes too) or off (warnings and errors)."""
    logging.getLogger(PACKAGE_LOGGER).setLevel(logging.DEBUG if on else logging.NOTSET)


def detailed() -> bool:
    return logging.getLogger(PACKAGE_LOGGER).level == logging.DEBUG


def shutdown() -> None:
    """Stop writing app.log (its file is closed)."""
    global _handler
    if _handler is None:
        return
    logging.getLogger().removeHandler(_handler)
    try:
        _handler.close()
    finally:
        _handler = None


def log_files(folder: Path) -> list[Path]:
    """The log files there are, newest first: app.log, app.log.1, …"""
    names = [FILE_NAME] + [f"{FILE_NAME}.{n}" for n in range(1, FILES)]
    return [Path(folder) / name for name in names if (Path(folder) / name).is_file()]
