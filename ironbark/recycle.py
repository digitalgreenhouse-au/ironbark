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
import re
import stat
import time
from pathlib import Path
from urllib.parse import quote

from . import platforms

BIN_NAME = platforms.RECYCLE_BIN

_DRIVE_FIXED = 3
_FO_DELETE = 3
_FOF_SILENT = 0x0004           # no progress window
_FOF_NOCONFIRMATION = 0x0010   # no "are you sure?" (the player has asked)
_FOF_ALLOWUNDO = 0x0040        # to the Recycle Bin
_FOF_NOERRORUI = 0x0400        # errors come back as a code, not a window
_FOF_WANTNUKEWARNING = 0x4000  # …except when it would be deleted for good after all: the shell asks then

# Linux file systems that are a server's: no Trash there. (fuse.gvfsd-fuse: the desktop's SMB, SFTP… mounts.)
_NETWORK_FILESYSTEMS = {"cifs", "smb3", "smbfs", "nfs", "nfs4", "ceph", "9p", "afs", "fuse.sshfs",
                        "fuse.gvfsd-fuse", "fuse.rclone", "davfs", "fuse.davfs2", "fuse.smbnetfs"}
_MOUNTS = "/proc/self/mounts"


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
        return (Path.home() / ".Trash").is_dir()
    if platforms.IS_LINUX:
        return _trash_for(os.path.abspath(os.fspath(path))) is not None
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
    best = ("/", "")
    try:
        with open(_MOUNTS, encoding="utf-8", errors="surrogateescape") as table:
            for line in table:
                fields = line.split()
                if len(fields) < 3:
                    continue
                point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[1])   # (a space: \040)
                inside = full == point or full.startswith(point.rstrip("/") + "/")
                if inside and len(point) >= len(best[0]):
                    best = (point, fields[2])
    except OSError:
        pass
    return best


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
    if kind in _NETWORK_FILESYSTEMS or kind.startswith("fuse.gvfs"):
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


def _to_trash(full: str) -> None:
    """Into the Trash as the file manager puts it there: the file in files/, and beside it in info/
    a .trashinfo saying where it came from and when (so it can be put back), under a name not taken
    there yet ("Mix.2.m3u8")."""
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
