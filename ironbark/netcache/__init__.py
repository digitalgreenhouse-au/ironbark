"""The network playback cache: songs on network drives play from a copy on this computer.

The network is treated as a source of files, not something playback depends on: a song on a
share (`\\\\server\\share`, a mapped drive) or an online-only OneDrive file is copied into
`%LOCALAPPDATA%\\Ironbark\\NetCache` and played from there — a short "Preparing…" first, then it
keeps playing if the network drops. A copy is never treated as complete until every byte is here
and it has been checked; until then the player reads it only through a reader that waits for
the bytes it needs (see netcache.growing and audiofile's notes).

Nothing changes until `install` runs (the Experimental switch "Network playback cache", read when
Ironbark starts): with it off, every song is read directly, exactly as before, and no cache thread
or file exists. No Tk anywhere in this package; it reaches the window only through the notices
and figures the window reads.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from pathlib import Path

from .. import audiofile, settings
from . import sources
from .manager import CacheView, Manager, NetworkNow, NotReady, Progress, Unavailable
from .store import OwnerLock

__all__ = ["DIRECT", "Manager", "NetworkNow", "NotReady", "Progress", "Unavailable", "CacheView", "clear_folder",
           "folder", "install", "left_on_disk", "manager", "uninstall"]

log = logging.getLogger(__name__)
_lock = threading.Lock()
_manager: Manager | None = None
_player = None


class _Direct:
    """The preparer when the cache isn't installed: every song is opened directly, as before."""

    def prepare(self, path, *, role: str = "play", at: float = 0.0, force: bool = False):
        return audiofile.LocalTrack.plain(path)

    def notify_when_startable(self, path, at: float, post) -> int:
        post()
        return 0

    def forget(self, token) -> None:
        pass

    def resumable(self, local, frame: int, stalls: int, quick: bool = False) -> bool:
        return True

    def outlook(self, path, at: float = 0.0):
        return None

    def plan_changed(self) -> None:
        pass

    def position_hint(self, path, seconds: float) -> None:
        pass

    def progress(self, path):
        return None


DIRECT = _Direct()


def folder():
    """Where the cache lives: beside the library database, in the local (not roaming) app data."""
    return settings.cache_folder() / "NetCache"


def install(prefs: dict, player=None, *, where=None, **options) -> Manager | None:
    """Start the cache (cheap here: a folder, the owner lock, the threads; the database is read on
    the cache's own thread). None if another copy of the player owns the cache, or it would be on
    a network drive itself — songs then play directly, as before."""
    global _manager, _player
    with _lock:
        if _manager is not None:
            return _manager
        place = where if where is not None else folder()
        if sources.is_remote(place):
            log.warning("the music cache can't live on a network drive (%s): not started", place)
            return None
        place.mkdir(parents=True, exist_ok=True)
        owner = OwnerLock(place / "owner.lock")
        if not owner.acquire():
            log.warning("another copy of Ironbark is using the music cache: network songs play directly")
            return None
        made = Manager(place, prefs, player, owner=owner, **options)
        made.start()
        audiofile.set_resolver(made.resolve, info=made.known_info, partial_folder=made.store.partial)
        setter = getattr(player, "set_preparer", None)
        if setter is not None:
            setter(made)
        _manager, _player = made, player
        return made


def uninstall() -> None:
    """Stop the cache: every song is read directly again."""
    global _manager, _player
    with _lock:
        made, player, _manager, _player = _manager, _player, None, None
    if made is None:
        return
    audiofile.set_resolver(None)
    setter = getattr(player, "set_preparer", None)
    if setter is not None:
        setter(DIRECT)
    made.shutdown()


def manager() -> Manager | None:
    return _manager


def left_on_disk(where=None) -> int:
    """Bytes the cache's files take on this computer (its folder, read: for while it isn't running)."""
    total = 0
    for root, _folders, files in os.walk(Path(where) if where is not None else folder()):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def clear_folder(where=None) -> bool:
    """Delete what the cache keeps on this computer while it isn't running (its copies and its list:
    everything can be downloaded again). False if a copy of the player is using it."""
    place = Path(where) if where is not None else folder()
    if _manager is not None:
        return False
    if not place.is_dir():
        return True
    owner = OwnerLock(place / "owner.lock")
    if not owner.acquire():
        return False
    try:
        for entry in place.iterdir():
            if entry.name == owner.path.name:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                try:
                    entry.unlink()
                except OSError:
                    pass
    finally:
        owner.release()
    return True
