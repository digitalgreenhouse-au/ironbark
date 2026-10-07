"""Shared test set-up.

On Windows the test windows are made on a hidden desktop of their own, so a test run never puts
windows in front of whatever the person at the computer is doing. They are still real windows —
mapped, laid out and measured exactly as on the visible desktop. Set IRONBARK_TESTS_VISIBLE=1 to
watch them on the normal desktop instead.
"""
import os
import sys

HIDDEN_DESKTOP = "IronbarkTests"


def _use_hidden_desktop():
    """Move this (main) thread to a hidden desktop before any window exists.

    Windows only lets a thread change desktop while it owns no windows, so this has to run before
    tkinter makes its first one — conftest is imported before any test module is.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.CreateDesktopW.restype = wintypes.HANDLE
    user32.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
                                      wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    user32.SetThreadDesktop.restype = wintypes.BOOL
    user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
    generic_all = 0x10000000
    desktop = user32.CreateDesktopW(HIDDEN_DESKTOP, None, None, 0, generic_all, None)
    if not desktop or not user32.SetThreadDesktop(desktop):
        return None   # carry on on the normal desktop
    return desktop    # kept for the life of the process: closing it would strand the windows


_hidden_desktop = None
if sys.platform == "win32" and os.environ.get("IRONBARK_TESTS_VISIBLE") != "1":
    _hidden_desktop = _use_hidden_desktop()
