"""Sending a file to the Recycle Bin (a Mac's Bin, Linux's Trash), so it can be restored — how a
playlist is deleted. No UI.

Windows keeps a Recycle Bin only on its fixed drives: a file deleted from a network drive or a USB
stick is gone for good, so `can_recycle` says whether this one can go there (the player then asks
differently). `recycle` uses the shell's own delete (SHFileOperationW with FOF_ALLOWUNDO, no
windows of its own) on the calling thread — which must not be Tk's: the shell can run a message
loop of its own, and a Tk callback inside it would break the program.

On Linux it's the freedesktop.org Trash, as the file manager uses it (so it restores from there): a
file in the home folder's file system goes to ~/.local/share/Trash, one on another disk (a USB
stick) to that disk's own .Trash-<uid> (or the shared .Trash/<uid>), each with its .trashinfo. A
network folder (SMB, NFS, the desktop's gvfs mounts) has none, as on Windows; that's told from the
mount table alone, so asking never waits on a server.
"""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path
from urllib.parse import quote

from . import mounts, platforms

BIN_NAME = platforms.RECYCLE_BIN

_DRIVE_FIXED = 3
_FO_DELETE = 3
_FOF_SILENT = 0x0004           # no progress window
_FOF_NOCONFIRMATION = 0x0010   # no "are you sure?" (the player has asked)
_FOF_ALLOWUNDO = 0x0040        # to the Recycle Bin
_FOF_NOERRORUI = 0x0400        # errors come back as a code, not a window
_FOF_WANTNUKEWARNING = 0x4000  # …except when it would be deleted for good after all: the shell asks then

_MOUNTS = mounts.MOUNTS   # (Linux: the mount table; tests point it at a file of their own)


def can_recycle(path) -> bool:
    """Whether deleting `path` can send it to the Recycle Bin: on Windows, a file on a fixed drive
    (not a network drive or a removable one, which have none); on a Mac, when there's a Bin."""
    if platforms.IS_WINDOWS:
        import ctypes

        full = os.path.abspath(os.fspath(path))
        drive = os.path.splitdrive(full)[0]
        if not drive or drive.startswith(("\\\\", "//")):
            return False   # (a network share)
        kernel32 = ctypes.WinDLL("kernel32")   # (its own handle: argtypes set here change nothing else)
        kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
        kernel32.GetDriveTypeW.restype = ctypes.c_uint
        return kernel32.GetDriveTypeW(drive + "\\") == _DRIVE_FIXED
    if platforms.IS_MAC:
        return _same_disk_as_mac_bin(os.path.abspath(os.fspath(path)))
    if platforms.IS_LINUX:
        return _trash_for(_resolved(os.path.abspath(os.fspath(path)))) is not None
    return False


def _same_disk_as_mac_bin(full: str) -> bool:
    """A Mac: the file can go to ~/.Trash only from the same disk (a move to another one fails, and
    another disk's own .Trashes is Finder's to manage). Anything under /Volumes is another disk or
    a network share: told from its path alone, so asking never waits on a server."""
    if full == "/Volumes" or full.startswith("/Volumes/"):
        return False
    trash = Path.home() / ".Trash"
    try:
        here = os.stat(os.path.dirname(os.path.realpath(full))).st_dev
        return trash.is_dir() and here == trash.stat().st_dev
    except OSError:
        return False


def recycle(path) -> None:
    """Send the file to the Recycle Bin. OSError when it couldn't go (it's left where it was). Not
    on Tk's thread (see the module notes)."""
    full = os.path.abspath(os.fspath(path))
    if not os.path.exists(full):
        raise FileNotFoundError(f"“{full}” isn't there")
    if os.path.isdir(full):   # (only ever a file: an empty path, say, would otherwise be this folder)
        raise IsADirectoryError(f"“{full}” is a folder, not a file")
    if platforms.IS_WINDOWS:
        _shell_delete(full)
    elif platforms.IS_MAC:
        _to_mac_bin(full)
    elif platforms.IS_LINUX:
        _to_trash(full)
    else:
        raise OSError(f"there's no {BIN_NAME} here to move it to")


def _shell_delete(full: str) -> None:
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", ctypes.c_wchar_p),
                    ("pTo", ctypes.c_wchar_p), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_wchar_p)]

    shell32 = ctypes.WinDLL("shell32")
    ole32 = ctypes.WinDLL("ole32")
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    shell32.SHFileOperationW.restype = ctypes.c_int
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    # The shell works through COM: this thread joins a single-threaded apartment for the call.
    started = ole32.CoInitializeEx(None, 0x2 | 0x4) >= 0   # COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE
    try:
        # The list of files ends with an empty one (two NULs): the buffer adds the second.
        source = ctypes.create_unicode_buffer(full + "\0")
        operation = SHFILEOPSTRUCTW(None, _FO_DELETE, ctypes.cast(source, ctypes.c_wchar_p), None,
                                    _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI
                                    | _FOF_WANTNUKEWARNING, False, None, None)
        code = shell32.SHFileOperationW(ctypes.byref(operation))
    finally:
        if started:
            ole32.CoUninitialize()
    if code != 0:
        raise OSError(f"Windows couldn't move it to the Recycle Bin (error {code:#x})")
    if operation.fAnyOperationsAborted or os.path.exists(full):
        raise OSError("it wasn't moved to the Recycle Bin")


def _to_mac_bin(full: str) -> None:
    """Into ~/.Trash under a name not taken there yet (Finder's "Put Back" isn't offered this way,
    but it can be dragged out again)."""
    trash = Path.home() / ".Trash"
    target = trash / os.path.basename(full)
    if target.exists():
        stem, suffix = os.path.splitext(target.name)
        target = trash / f"{stem} {time.strftime('%H.%M.%S')}{suffix}"
    os.replace(full, target)


# -- Linux: the freedesktop.org Trash ---------------------------------------------------------------
def _mount_of(full: str) -> tuple[str, str]:
    """The mount point `full` is under and its file system type, from the mount table (no stat)."""
    try:
        with open(_MOUNTS, encoding="utf-8", errors="surrogateescape") as table:
            found = mounts.mount_of(full, mounts.parse(table.read()))
    except OSError:
        return "/", ""
    return found.point, found.kind


def _on_network(kind: str) -> bool:
    return kind in mounts.NETWORK_FILESYSTEMS or kind.startswith("fuse.gvfs")


def _home_trash() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "Trash"


def _device(path: Path) -> int:
    """The device of `path`, or of the nearest folder above it that exists."""
    while True:
        try:
            return path.stat().st_dev
        except FileNotFoundError:
            if path.parent == path:
                raise
            path = path.parent


def _trash_for(full: str) -> tuple[Path, str | None] | None:
    """The Trash folder for `full`, and the folder its .trashinfo names it from (None: by its full
    path); None when there's no Trash for it (a network folder, a disk we can't write a Trash on)."""
    point, kind = _mount_of(full)
    if _on_network(kind):
        return None
    home = _home_trash()
    try:
        if os.stat(os.path.dirname(full)).st_dev == _device(home):
            return home, None
    except OSError:
        return None
    top = Path(point)
    uid = str(os.getuid())
    shared = top / ".Trash"
    try:
        found = os.lstat(shared)
        if stat.S_ISDIR(found.st_mode) and found.st_mode & stat.S_ISVTX:   # (a real folder, sticky)
            return shared / uid, point
    except OSError:
        pass
    own = top / f".Trash-{uid}"
    if own.is_dir() or os.access(top, os.W_OK):
        return own, point
    return None


def _resolved(full: str) -> str:
    """`full` with the folders above it resolved (a link to a folder on another disk is followed, so
    the disk, and its Trash, are the ones the file is really on). The file itself isn't: a link is
    put in the Trash as a link, as the file manager does. A path the mount table already places on
    a network folder is left as it is: resolving it would ask the server."""
    if _on_network(_mount_of(full)[1]):
        return full
    return os.path.join(os.path.realpath(os.path.dirname(full)), os.path.basename(full))


def _to_trash(full: str) -> None:
    """Into the Trash as the file manager puts it there: the file in files/, and beside it in info/
    a .trashinfo saying where it came from and when (so it can be put back), under a name not taken
    there yet ("Mix.2.m3u8")."""
    full = _resolved(full)
    found = _trash_for(full)
    if found is None:
        raise OSError(f"there's no {BIN_NAME} for “{full}”")
    trash, top = found
    files, info = trash / "files", trash / "info"
    for folder in (trash, files, info):
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    original = os.path.relpath(full, top) if top else full
    stem, suffix = os.path.splitext(os.path.basename(full))
    number = 1
    while True:
        name = f"{stem}{suffix}" if number == 1 else f"{stem}.{number}{suffix}"
        number += 1
        note = info / f"{name}.trashinfo"
        try:
            # Making the .trashinfo is what claims the name (the spec's way: another program
            # trashing a file of the same name at the same moment gets the next one).
            handle = os.open(note, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        if not (files / name).exists():
            break
        os.close(handle)
        os.remove(note)
    with os.fdopen(handle, "w", encoding="utf-8") as text:
        text.write(f"[Trash Info]\nPath={quote(os.fsencode(original), safe='/')}\n"
                   f"DeletionDate={time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
    try:
        os.rename(full, files / name)
    except OSError:
        os.remove(note)
        raise
