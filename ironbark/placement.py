"""Put windows back where the user left them — but never onto a monitor that's gone.

Tk describes a window as "WIDTHxHEIGHT+X+Y". On a multi-monitor setup X and Y can be
negative ("+-1920+0" for a screen left of the main one), and if that screen is later
unplugged a restored window would open somewhere nobody can see it.
"""

from __future__ import annotations

import re
import sys
import tkinter as tk

_GEOMETRY = re.compile(r"^(?:(\d+)x(\d+))?\+(-?\d+)\+(-?\d+)$")
TITLE_BAR = 40  # px of the window's top edge that must be on a screen, so it can be dragged


def parse(geometry: str) -> tuple[int | None, int | None, int, int] | None:
    match = _GEOMETRY.match(geometry.strip())
    if not match:
        return None
    width, height, x, y = match.groups()
    return (int(width) if width else None, int(height) if height else None, int(x), int(y))


def on_screen(x: int, y: int, width: int) -> bool:
    """Is the top strip of a window at (x, y) on any connected monitor?"""
    if sys.platform != "win32":
        return True  # other platforms keep windows on-screen themselves
    import ctypes
    import ctypes.wintypes as wt

    strip = wt.RECT(x, y, x + max(width, 100), y + TITLE_BAR)
    return bool(ctypes.windll.user32.MonitorFromRect(ctypes.byref(strip), 0))  # 0 = none if off-screen


def restore(window: tk.Misc, geometry: str, keep_size: bool = True) -> bool:
    """Apply a saved geometry if it's still on a connected monitor. Returns whether it was."""
    parsed = parse(geometry) if geometry else None
    if parsed is None:
        return False
    width, height, x, y = parsed
    if not on_screen(x, y, width or 200):
        if keep_size and width and height:
            window.geometry(f"{width}x{height}")  # keep the size, let the system place it
        return False
    window.geometry(geometry if keep_size else f"+{x}+{y}")
    return True


def current(window: tk.Misc) -> str | None:
    """The window's geometry, or None while it's minimised (Windows then reports -32000)."""
    try:
        if window.state() != "normal":
            return None
    except tk.TclError:
        return None
    return window.geometry()


def work_height(window: tk.Misc) -> int:
    """(0.22.0) The usable height (without the taskbar) of the monitor `window` is on, in pixels —
    for a window that grows with its content but must still fit on the screen (Now Playing)."""
    if sys.platform == "win32":
        try:
            import ctypes
            import ctypes.wintypes as wt

            class MonitorInfo(ctypes.Structure):
                _fields_ = [("cb", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

            # A private handle, so setting its types can't change anyone else's calls.
            user32 = ctypes.WinDLL("user32")
            user32.MonitorFromWindow.restype = wt.HMONITOR
            user32.MonitorFromWindow.argtypes = (wt.HWND, wt.DWORD)
            user32.GetMonitorInfoW.argtypes = (wt.HMONITOR, ctypes.POINTER(MonitorInfo))
            window.update_idletasks()
            hwnd = user32.GetParent(window.winfo_id()) or window.winfo_id()
            info = MonitorInfo()
            info.cb = ctypes.sizeof(MonitorInfo)
            monitor = user32.MonitorFromWindow(hwnd, 2)   # 2 = the nearest monitor
            if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                return info.rcWork.bottom - info.rcWork.top
        except (OSError, AttributeError, tk.TclError):
            pass
    return window.winfo_screenheight()
