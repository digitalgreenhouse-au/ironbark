"""Our own title bar (tabs live there) while Windows keeps doing the window work.

On Windows we take away the standard caption but keep the resizable frame, so
the window still resizes from its edges, snaps to screen edges, minimises and
maximises like any other. Pressing on our tab bar hands the drag to Windows
itself (as if the caption were clicked), so Aero Snap and Snap Layouts behave
natively. Elsewhere the system title bar stays and our bar sits under it.

Full screen (F11) is borderless, on the same window (`enter_fullscreen` / `exit_fullscreen`).
"""

from __future__ import annotations

import sys
import tkinter as tk

GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_SYSMENU = 0x00080000
SWP_FLAGS = 0x0001 | 0x0002 | 0x0004 | 0x0020  # NOSIZE | NOMOVE | NOZORDER | FRAMECHANGED
WM_NCLBUTTONDOWN = 0x00A1
HTCAPTION = 2

CUSTOM = sys.platform == "win32"  # draw our own title bar?


def _hwnd(window: tk.Misc) -> int:
    import ctypes

    window.update_idletasks()
    return ctypes.windll.user32.GetParent(window.winfo_id())


def remove_caption(window: tk.Misc) -> None:
    """Drop the standard title bar but keep the resizable frame and min/max behaviour."""
    if not CUSTOM:
        return
    import ctypes

    user32 = ctypes.windll.user32
    hwnd = _hwnd(window)
    style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
    style = (style & ~WS_CAPTION) | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU
    user32.SetWindowLongPtrW(hwnd, GWL_STYLE, style)
    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_FLAGS)


def start_move(window: tk.Misc) -> None:
    """Let Windows drag the window from here, exactly as if its title bar were grabbed.

    The request is POSTED, never sent: SendMessage would sit inside this Python callback
    for the whole drag (with Python's lock released), while Windows keeps delivering the
    window's events, and the first one that runs Python code kills the program
    ("Fatal Python error: ... the GIL is released"). Posted, the drag starts from Tk's own
    message loop once this callback has returned, just like grabbing a normal title bar."""
    if not CUSTOM:
        return
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.windll.user32
    cursor = wt.POINT()
    user32.GetCursorPos(ctypes.byref(cursor))
    where = ((cursor.y & 0xFFFF) << 16) | (cursor.x & 0xFFFF)  # screen point, as signed 16-bit halves
    user32.ReleaseCapture()
    user32.PostMessageW(_hwnd(window), WM_NCLBUTTONDOWN, HTCAPTION, where)


def toggle_maximise(window: tk.Misc) -> None:
    window.state("normal" if window.state() == "zoomed" else "zoomed")


def work_area(window: tk.Misc) -> tuple[int, int, int, int]:
    """(0.25.0) The usable part (no taskbar) of the monitor the window is on: left, top, right,
    bottom in px. The whole primary screen where Windows can't be asked."""
    if CUSTOM:
        try:
            import ctypes
            import ctypes.wintypes as wt

            class MonitorInfo(ctypes.Structure):
                _fields_ = [("cb", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

            user32 = ctypes.windll.user32
            info = MonitorInfo()
            info.cb = ctypes.sizeof(MonitorInfo)
            if user32.GetMonitorInfoW(user32.MonitorFromWindow(_hwnd(window), 2), ctypes.byref(info)):
                work = info.rcWork
                return work.left, work.top, work.right, work.bottom
        except (AttributeError, OSError, tk.TclError):
            pass
    return 0, 0, window.winfo_screenwidth(), window.winfo_screenheight()


def keep_maximise_in_work_area(window: tk.Misc) -> None:
    """Without a caption, Windows maximises over the taskbar. Pull it back to the work area."""
    if not CUSTOM:
        return
    import ctypes
    import ctypes.wintypes as wt

    class MonitorInfo(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

    user32 = ctypes.windll.user32

    def fit(event):
        if event.widget is not window or window.state() != "zoomed":
            return
        hwnd = _hwnd(window)
        info = MonitorInfo()
        info.cb = ctypes.sizeof(MonitorInfo)
        user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(info))
        work, screen = info.rcWork, info.rcMonitor
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        # A maximised window keeps its invisible resize border just outside the screen.
        border = max(0, (rect.right - rect.left - (screen.right - screen.left)) // 2)
        want = (work.left - border, work.top - border,
                work.right - work.left + 2 * border, work.bottom - work.top + 2 * border)
        if (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top) != want:
            user32.SetWindowPos(hwnd, 0, *want, 0x0004 | 0x0010)  # NOZORDER | NOACTIVATE

    window.bind("<Configure>", fit, add="+")


# -- full screen (F11) ------------------------------------------------------------------------------
# Tk's own full screen (`wm attributes -fullscreen`) destroys the window's Windows handle and makes a
# new one with the standard title bar: the taskbar buttons, Windows' media panel, drag and drop and
# the dark frame are all tied to the first handle. So full screen here is borderless on the same
# handle: the resizable frame is taken off and the window sized to its monitor; leaving puts the
# frame back and the window where it was (maximised again, if it was).
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020


def _user32():
    """A private user32 handle, its types set (setting them on ctypes.windll.user32 would change every
    other caller's)."""
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.WinDLL("user32")
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.GetWindowLongPtrW.argtypes = (wt.HWND, ctypes.c_int)
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = (wt.HWND, ctypes.c_int, ctypes.c_ssize_t)
    user32.SetWindowPos.argtypes = (wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    wt.UINT)
    user32.GetWindowRect.argtypes = (wt.HWND, ctypes.POINTER(wt.RECT))
    user32.MonitorFromWindow.restype = wt.HMONITOR
    user32.MonitorFromWindow.argtypes = (wt.HWND, wt.DWORD)
    return user32


def monitor_rect(window: tk.Misc) -> tuple[int, int, int, int]:
    """The whole monitor the window is on (taskbar included): left, top, width, height in px."""
    if CUSTOM:
        try:
            import ctypes
            import ctypes.wintypes as wt

            class MonitorInfo(ctypes.Structure):
                _fields_ = [("cb", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

            user32 = _user32()
            user32.GetMonitorInfoW.argtypes = (wt.HMONITOR, ctypes.POINTER(MonitorInfo))
            info = MonitorInfo()
            info.cb = ctypes.sizeof(MonitorInfo)
            if user32.GetMonitorInfoW(user32.MonitorFromWindow(_hwnd(window), 2), ctypes.byref(info)):
                r = info.rcMonitor
                return r.left, r.top, r.right - r.left, r.bottom - r.top
        except (AttributeError, OSError, tk.TclError):
            pass
    return 0, 0, window.winfo_screenwidth(), window.winfo_screenheight()


def is_fullscreen(window: tk.Misc) -> bool:
    return getattr(window, "_dg_fullscreen", None) is not None


def enter_fullscreen(window: tk.Misc) -> bool:
    """Borderless full screen on the window's own monitor. Returns whether it's full screen now."""
    if is_fullscreen(window):
        return True
    zoomed = window.state() == "zoomed"
    if not CUSTOM:
        window._dg_fullscreen = {"zoomed": zoomed}
        window.attributes("-fullscreen", True)
        return True
    import ctypes
    import ctypes.wintypes as wt

    if zoomed:   # from maximised: back to its normal place first, which is where leaving returns to
        window.state("normal")
        window.update_idletasks()
    user32 = _user32()
    hwnd = _hwnd(window)
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
    left, top, width, height = monitor_rect(window)
    window._dg_fullscreen = {"style": style, "zoomed": zoomed,
                             "rect": (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)}
    user32.SetWindowLongPtrW(hwnd, GWL_STYLE, style & ~WS_THICKFRAME)
    user32.SetWindowPos(hwnd, 0, left, top, width, height, SWP_NOZORDER | SWP_FRAMECHANGED)
    return True


def exit_fullscreen(window: tk.Misc) -> bool:
    """Back from full screen: the frame, the place and size it had (and maximised, if it was).
    Returns whether it was full screen."""
    saved = getattr(window, "_dg_fullscreen", None)
    if saved is None:
        return False
    window._dg_fullscreen = None
    if not CUSTOM:
        window.attributes("-fullscreen", False)
    else:
        user32 = _user32()
        hwnd = _hwnd(window)
        user32.SetWindowLongPtrW(hwnd, GWL_STYLE, saved["style"])
        user32.SetWindowPos(hwnd, 0, *saved["rect"], SWP_NOZORDER | SWP_FRAMECHANGED)
    if saved["zoomed"]:
        window.state("zoomed")
    return True
