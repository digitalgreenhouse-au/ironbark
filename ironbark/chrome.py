"""Our own title bar (tabs live there) while Windows keeps doing the window work.

On Windows we take away the standard caption but keep the resizable frame, so
the window still resizes from its edges, snaps to screen edges and minimises
like any other. It can't be maximised: the player is a tall side strip whose
width is capped (ui.FULL_MAX_WIDTH), so the maximise box goes too and Windows+Up
does nothing. Pressing on our tab bar (or the mini strip) drags the window:
`start_move` / `follow_move` / `end_move`, the window kept where the pointer
holds it. (The press used to be handed to Windows' own move loop, as if the
caption were clicked; on Windows 10 that loop could miss the button being let
go, and the window ran on after the pointer.) On Windows each step is a move
alone (SetWindowPos: no size, no repaint), as Windows' own drag does: moved
through Tk (`wm geometry`), the whole window was redrawn at every step, and on
Windows 10 it flickered in stripes while it moved. Windows+← / → still snap it
to a side. Elsewhere the system title bar stays and our bar sits under it.
"""

from __future__ import annotations

import tkinter as tk

from . import placement, platforms

GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_SYSMENU = 0x00080000
SWP_FLAGS = 0x0001 | 0x0002 | 0x0004 | 0x0020  # NOSIZE | NOMOVE | NOZORDER | FRAMECHANGED
SWP_MOVE_ONLY = 0x0001 | 0x0004 | 0x0010        # NOSIZE | NOZORDER | NOACTIVATE: the picture moves as it is

CUSTOM = platforms.CUSTOM_TITLE_BAR  # draw our own title bar?


def _hwnd(window: tk.Misc) -> int:
    import ctypes

    window.update_idletasks()
    return ctypes.windll.user32.GetParent(window.winfo_id())


def remove_caption(window: tk.Misc) -> None:
    """Drop the standard title bar (and the maximise box) but keep the resizable frame and minimise."""
    if not CUSTOM:
        return
    import ctypes

    user32 = ctypes.windll.user32
    hwnd = _hwnd(window)
    style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
    style = (style & ~WS_CAPTION & ~WS_MAXIMIZEBOX) | WS_THICKFRAME | WS_MINIMIZEBOX | WS_SYSMENU
    user32.SetWindowLongPtrW(hwnd, GWL_STYLE, style)
    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_FLAGS)


class _Drag:
    """A drag under way: where the pointer holds the window (from its top left), and on Windows
    the window's own handle and its frame's place when it was grabbed (moved by those, not
    through Tk)."""

    __slots__ = ("grip", "hwnd", "frame", "pointer", "last")

    def __init__(self, grip: tuple[int, int], hwnd: int = 0, frame: tuple[int, int] = (0, 0),
                 pointer: tuple[int, int] = (0, 0)):
        self.grip, self.hwnd, self.frame, self.pointer = grip, hwnd, frame, pointer
        self.last: tuple[int, int] | None = None


_moving: dict[str, _Drag] = {}   # window → its drag, while it's dragged


def start_move(window: tk.Misc, event: tk.Event) -> None:
    """A press on an empty part of our title bar: the window follows the pointer (follow_move)
    until the button is let go (end_move), held where it was grabbed. Its place is always the
    pointer's less that grip, never a speed carried on: let go, and it stays put."""
    if not CUSTOM:
        return
    at = _place(window)
    if at is None:
        return
    drag = _Drag((event.x_root - at[0], event.y_root - at[1]))
    if platforms.IS_WINDOWS:
        frame = _frame_place(window)
        if frame is not None:
            drag.hwnd, drag.frame = frame
            drag.pointer = (event.x_root, event.y_root)
    _moving[str(window)] = drag


def follow_move(window: tk.Misc, event: tk.Event) -> bool:
    """The pointer moved with the button held: the window follows, if a drag of it is under way
    (True). Tk keeps sending the pointer to the pressed widget, even off the window."""
    drag = _moving.get(str(window))
    if drag is None:
        return False
    if drag.hwnd:   # (Windows: the frame moved by the pointer's travel, as it is — nothing redrawn)
        x = drag.frame[0] + event.x_root - drag.pointer[0]
        y = drag.frame[1] + event.y_root - drag.pointer[1]
        if (x, y) != drag.last:
            drag.last = (x, y)
            _set_window_pos(drag.hwnd, x, y)
        return True
    x, y = event.x_root - drag.grip[0], event.y_root - drag.grip[1]
    if (x, y) != _place(window):
        window.geometry(f"+{x}+{y}")
    return True


def _frame_place(window: tk.Misc) -> tuple[int, tuple[int, int]] | None:
    """(Windows) The window's frame handle and the top left of its rectangle on the screen, or
    None if Windows can't say."""
    import ctypes
    from ctypes import wintypes

    try:
        hwnd = _hwnd(window)
        rect = wintypes.RECT()
        if not hwnd or not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
    except (AttributeError, OSError):
        return None
    return hwnd, (rect.left, rect.top)


def _set_window_pos(hwnd: int, x: int, y: int) -> None:
    import ctypes

    ctypes.windll.user32.SetWindowPos(hwnd, 0, x, y, 0, 0, SWP_MOVE_ONLY)


def _place(window: tk.Misc) -> tuple[int, int] | None:
    """Where the window is, as `wm geometry` gives and takes it (the frame's top left): read the
    way it's set, so a drag never starts with a jump of the frame's width."""
    parsed = placement.parse(window.geometry())
    return None if parsed is None else (parsed[2], parsed[3])


def end_move(window: tk.Misc) -> bool:
    """The button was let go: the drag is over (True if there was one)."""
    return _moving.pop(str(window), None) is not None


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
