"""Sending a file to the Recycle Bin (a Mac's Bin), so it can be restored — how a playlist is
deleted. No UI.

Windows keeps a Recycle Bin only on its fixed drives: a file deleted from a network drive or a USB
stick is gone for good, so `can_recycle` says whether this one can go there (the player then asks
differently). `recycle` uses the shell's own delete (SHFileOperationW with FOF_ALLOWUNDO, no
windows of its own) on the calling thread — which must not be Tk's: the shell can run a message
loop of its own, and a Tk callback inside it would break the program.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

BIN_NAME = "Bin" if sys.platform == "darwin" else "Recycle Bin"

_DRIVE_FIXED = 3
_FO_DELETE = 3
_FOF_SILENT = 0x0004           # no progress window
_FOF_NOCONFIRMATION = 0x0010   # no "are you sure?" (the player has asked)
_FOF_ALLOWUNDO = 0x0040        # to the Recycle Bin
_FOF_NOERRORUI = 0x0400        # errors come back as a code, not a window
_FOF_WANTNUKEWARNING = 0x4000  # …except when it would be deleted for good after all: the shell asks then


def can_recycle(path) -> bool:
    """Whether deleting `path` can send it to the Recycle Bin: on Windows, a file on a fixed drive
    (not a network drive or a removable one, which have none); on a Mac, when there's a Bin."""
    if sys.platform == "win32":
        import ctypes

        full = os.path.abspath(os.fspath(path))
        drive = os.path.splitdrive(full)[0]
        if not drive or drive.startswith(("\\\\", "//")):
            return False   # (a network share)
        kernel32 = ctypes.WinDLL("kernel32")   # (its own handle: argtypes set here change nothing else)
        kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
        kernel32.GetDriveTypeW.restype = ctypes.c_uint
        return kernel32.GetDriveTypeW(drive + "\\") == _DRIVE_FIXED
    if sys.platform == "darwin":
        return (Path.home() / ".Trash").is_dir()
    return False


def recycle(path) -> None:
    """Send the file to the Recycle Bin. OSError when it couldn't go (it's left where it was). Not
    on Tk's thread (see the module notes)."""
    full = os.path.abspath(os.fspath(path))
    if not os.path.exists(full):
        raise FileNotFoundError(f"“{full}” isn't there")
    if sys.platform == "win32":
        _shell_delete(full)
    elif sys.platform == "darwin":
        _to_mac_bin(full)
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
