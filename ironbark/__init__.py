"""Ironbark (formerly DG FLAC Player) — a lossless music player that proves what it plays, by Digital Greenhouse."""

import sys as _sys

# The one place the version is set (pyproject.toml reads it from here). Semantic versioning:
# MAJOR.MINOR.PATCH — PATCH for fixes, MINOR for new features; MAJOR stays 0 until the public
# release (1.0.0). Every change gets a bump and a CHANGELOG.md entry.
__version__ = "0.54.0"
APP_NAME = "Ironbark"
OLD_APP_NAME = "DG FLAC Player"   # (0.26.0) the name before; its settings folder is carried over

if _sys.platform == "win32":   # (in the player and in its worker processes alike: see winlisting.py)
    from . import winlisting as _winlisting

    _winlisting.install()
