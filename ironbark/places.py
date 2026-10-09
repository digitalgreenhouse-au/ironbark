"""Where a song really lives: this computer, a network share, or a OneDrive file that's online only —
and whether the player may read it: Settings → Library → Local network access (`allowed`).

Classifying a path must never wait on the network: a `stat` of a path on an unreachable server
took 42 s (measured), and so did asking Windows what kind of drive a server path is. So:
- `\\\\server\\share\\…` (and `//server/share`, `\\\\?\\UNC\\…`) is a share by its spelling alone —
  never stat'ed, never asked about.
- A drive letter is looked up in a small table of drive types (Windows' GetDriveTypeW, refreshed
  every 30 s by the cache's own scheduler thread; a letter not seen yet is asked once, which takes
  microseconds for a connected drive). A remote drive is a share — mapped drives and `subst`
  drives that point at one alike.
- Only under a OneDrive folder is a file's attributes looked at (a local stat, which starts no
  download): an online-only file is a cloud file.
- Anything else is local. (Google Drive's G: looks like a plain disk and stays local.)

`source_key` names the file the same way whatever path reached it — a mapped `Z:\\x` and
`\\\\192.168.80.173\\Shared\\x` are one copy — using the mapping the scheduler looked up
(WNetGetConnectionW, QueryDosDeviceW). A letter not looked up yet keeps its own spelling: at
worst a second copy, never a wrong one.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections import OrderedDict
from enum import Enum

from .audiofile import UrlPath

DRIVE_REMOTE = 4
CLOUD_ATTRIBUTES = 0x400000 | 0x40000 | 0x1000   # recall on data access, recall on open, offline
DRIVE_CACHE_S = 30
CLOUD_ATTR_CACHE_S = 60
CLOUD_CACHE_SIZE = 4096


class Kind(Enum):
    LOCAL = "local"
    SHARE = "share"
    CLOUD = "cloud"
    URL = "url"


# -- Windows calls (replaced in tests) ----------------------------------------------------
def _drive_type(root: str) -> int:
    """GetDriveTypeW for a drive root ("Z:\\"). Only ever called with a drive letter."""
    if sys.platform != "win32":
        return 0
    import ctypes
    return int(ctypes.WinDLL("kernel32").GetDriveTypeW(ctypes.c_wchar_p(root)))


def _connection(letter: str) -> str | None:
    """The `\\\\server\\share` a mapped drive letter ("Z:") points at (WNetGetConnectionW), or None."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes
    mpr = ctypes.WinDLL("mpr")
    size = wintypes.DWORD(1024)
    buffer = ctypes.create_unicode_buffer(size.value)
    if mpr.WNetGetConnectionW(ctypes.c_wchar_p(letter), buffer, ctypes.byref(size)) != 0:
        return None
    return buffer.value or None


def _dos_device(letter: str) -> str | None:
    """What a `subst` drive letter ("Z:") stands for (QueryDosDeviceW), as a path, or None."""
    if sys.platform != "win32":
        return None
    import ctypes
    buffer = ctypes.create_unicode_buffer(1024)
    if not ctypes.WinDLL("kernel32").QueryDosDeviceW(ctypes.c_wchar_p(letter), buffer, 1024):
        return None
    target = buffer.value
    if target.startswith("\\??\\UNC\\"):
        return "\\\\" + target[len("\\??\\UNC\\"):]
    if target.startswith("\\??\\"):
        return target[len("\\??\\"):]
    return None   # a real device (\Device\HarddiskVolume3): not a subst


def _logical_drives() -> list[str]:
    if sys.platform != "win32":
        return []
    import ctypes
    mask = int(ctypes.WinDLL("kernel32").GetLogicalDrives())
    return [f"{chr(65 + i)}:" for i in range(26) if mask & (1 << i)]


def _stat(path: str):
    return os.stat(path)


def _onedrive_roots() -> list[str]:
    roots = []
    for name in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        value = os.environ.get(name)
        if value:
            roots.append(os.path.normcase(os.path.abspath(value)).rstrip("\\/") + os.sep)
    return roots


# -- spelling -----------------------------------------------------------------------------
def _unc_of(text: str) -> str | None:
    """`\\\\server\\share\\…` (backslashes) if `text` is spelt as a server path, else None."""
    t = text.replace("/", "\\")
    upper = t[:8].upper()
    if upper.startswith("\\\\?\\UNC\\") or upper.startswith("\\\\.\\UNC\\"):
        return "\\\\" + t[8:]
    if t.startswith("\\\\?\\") or t.startswith("\\\\.\\"):
        return None
    if t.startswith("\\\\"):
        return t
    return None


def _strip_long(text: str) -> str:
    """`\\\\?\\C:\\x` → `C:\\x`; a device path (`\\\\.\\C:`) is left as it is."""
    t = text.replace("/", "\\")
    if t.startswith("\\\\?\\") and t[5:6] == ":":
        return t[4:]
    return t


def _letter(text: str) -> str | None:
    if len(text) >= 2 and text[1] == ":" and text[0].isalpha():
        return text[0].upper() + ":"
    return None


def network_target(letter: str) -> str | None:
    """The server path (`\\\\server\\share`) a drive letter ("Z:") stands for — a mapped network
    drive, or a `subst` onto a server path — or None. Asks Windows, which may wait on the network:
    never on the window's thread."""
    target = _connection(letter) or _dos_device(letter)
    return target.rstrip("\\") if target and _unc_of(target) else None


class Classifier:
    """Classifies paths with the cached tables described in the module note. `refresh()` is the
    only part that may wait on Windows' network calls: the cache's scheduler thread runs it."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._types: dict[str, int] = {}          # "Z:" -> drive type
        self._targets: dict[str, str] = {}        # "Z:" -> "\\server\share" (mapped or subst on a share)
        self._refreshed = -1e18
        self._cloud: OrderedDict[str, tuple[float, bool]] = OrderedDict()
        self._roots = _onedrive_roots()

    # The scheduler's part --------------------------------------------------------------
    def refresh(self, force: bool = False) -> None:
        """Re-read the drive table (types, and where remote letters point). May wait on Windows'
        networking calls: never call it from the window's thread or under a lock."""
        now = self._clock()
        if not force and now - self._refreshed < DRIVE_CACHE_S:
            return
        types, targets = {}, {}
        for letter in _logical_drives() or list(self._types):
            kind = _drive_type(letter + "\\")
            types[letter] = kind
            if kind == DRIVE_REMOTE:
                target = _connection(letter) or _dos_device(letter)
                if target:
                    targets[letter] = target.rstrip("\\")
            else:
                target = _dos_device(letter)   # a subst onto a server path
                if target and _unc_of(target):
                    types[letter] = DRIVE_REMOTE
                    targets[letter] = target.rstrip("\\")
        with self._lock:
            self._types, self._targets, self._refreshed = types, targets, now

    def due(self) -> bool:
        return self._clock() - self._refreshed >= DRIVE_CACHE_S

    # Anyone's part (no network I/O) ----------------------------------------------------
    def _type_of(self, letter: str) -> int:
        with self._lock:
            kind = self._types.get(letter)
        if kind is None:
            kind = _drive_type(letter + "\\")   # a letter not seen yet: asked once (microseconds when connected)
            with self._lock:
                self._types[letter] = kind
        return kind

    def _under_roots(self, text: str) -> bool:
        """Is this file (or folder, spelt with its last separator) inside a OneDrive folder?"""
        here = os.path.normcase(text)
        return any(here.startswith(root) for root in self._roots)

    def _is_cloud(self, text: str) -> bool:
        here = os.path.normcase(text)
        if not any(here.startswith(root) for root in self._roots):
            return False
        now = self._clock()
        with self._lock:
            hit = self._cloud.get(here)
            if hit is not None and now - hit[0] < CLOUD_ATTR_CACHE_S:
                self._cloud.move_to_end(here)
                return hit[1]
        try:
            attributes = getattr(_stat(text), "st_file_attributes", 0)
        except OSError:
            attributes = 0
        cloud = bool(attributes & CLOUD_ATTRIBUTES)
        with self._lock:
            self._cloud[here] = (now, cloud)
            self._cloud.move_to_end(here)
            while len(self._cloud) > CLOUD_CACHE_SIZE:
                self._cloud.popitem(last=False)
        return cloud

    def classify(self, path) -> Kind:
        if isinstance(path, UrlPath):
            return Kind.URL
        text = os.fspath(path)
        if _unc_of(text) is not None:
            return Kind.SHARE
        text = _strip_long(text)
        if text.startswith("\\\\"):
            return Kind.LOCAL   # a device path (\\.\C:)
        letter = _letter(text)
        if letter is not None and self._type_of(letter) == DRIVE_REMOTE:
            return Kind.SHARE
        if self._is_cloud(text):
            return Kind.CLOUD
        return Kind.LOCAL

    def is_remote(self, path) -> bool:
        return self.classify(path) is not Kind.LOCAL

    def any_remote(self, paths) -> bool:
        """Is any of `paths` on the network (a share, a mapped drive, an online-only OneDrive file)?
        Quick for a whole library: a drive letter is looked up once, whether a folder is inside a
        OneDrive folder once, and only files inside one are looked at one by one."""
        letters: dict[str, bool] = {}
        folders: dict[str, bool] = {}   # folder (with its last separator) -> inside a OneDrive folder
        for path in paths:
            if isinstance(path, UrlPath):
                continue
            text = os.fspath(path)
            if text[:2] in ("\\\\", "//"):   # a server path (or a device path: classified in full)
                if self.classify(path) is not Kind.LOCAL:
                    return True
                continue
            letter = _letter(text)
            if letter is not None:
                remote = letters.get(letter)
                if remote is None:
                    remote = letters[letter] = self._type_of(letter) == DRIVE_REMOTE
                if remote:
                    return True
            if self._roots:
                folder = text[:max(text.rfind("\\"), text.rfind("/")) + 1]
                inside = folders.get(folder)
                if inside is None:
                    inside = folders[folder] = self._under_roots(folder)
                if inside and self._is_cloud(text):
                    return True
        return False

    def unc(self, path) -> str | None:
        """The server path of a share song (a mapped letter turned into its server path when the
        mapping is known)."""
        text = os.fspath(path)
        unc = _unc_of(text)
        if unc is not None:
            return unc
        text = _strip_long(text)
        letter = _letter(text)
        if letter is None:
            return None
        with self._lock:
            target = self._targets.get(letter)
        return target + text[2:] if target else None

    def source_key(self, path) -> str:
        kind = self.classify(path)
        if kind is Kind.URL:
            return "url:" + path.url
        text = os.fspath(path)
        if kind is Kind.SHARE:
            return "smb:" + os.path.normcase(self.unc(path) or _strip_long(text))
        if kind is Kind.CLOUD:
            return "cloud:" + os.path.normcase(text)
        return "local:" + os.path.normcase(text)

    def host_of(self, path) -> str:
        kind = self.classify(path)
        if kind is Kind.SHARE:
            unc = self.unc(path)
            if unc:
                return unc[2:].split("\\", 1)[0].lower()
            letter = _letter(_strip_long(os.fspath(path)))
            return (letter or "share").lower()
        if kind is Kind.CLOUD:
            return "cloud"
        if kind is Kind.URL:
            from urllib.parse import urlsplit
            parts = urlsplit(path.url)
            return f"{parts.scheme}://{parts.hostname}:{parts.port or (443 if parts.scheme == 'https' else 80)}"
        return ""


_default = Classifier()


def default() -> Classifier:
    return _default


def classify(path) -> Kind:
    return _default.classify(path)


def is_remote(path) -> bool:
    return _default.is_remote(path)


def source_key(path) -> str:
    return _default.source_key(path)


def host_of(path) -> str:
    return _default.host_of(path)


# -- the switch ----------------------------------------------------------------------------------
def allowed(path, prefs: dict | None) -> bool:
    """May the player read this path? Anything on this computer, always; a network share, a mapped
    drive or an online-only OneDrive file only while Settings → Library → Local network access is on
    (`local_network`, off by default). Asked on worker threads (classifying a drive letter may ask
    Windows once)."""
    return bool(prefs and prefs.get("local_network")) or not is_remote(path)


def refused(paths, prefs: dict | None) -> list:
    """Those of `paths` the switch keeps the player from reading."""
    return [p for p in paths if not allowed(p, prefs)]
