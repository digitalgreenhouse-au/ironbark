"""What X11's window manager knows about a window and Tk doesn't: whether it's minimised, and how
to bring it back. No UI.

Tk learns that a window is minimised only when the window manager unmaps it. Cinnamon and GNOME
(Muffin, Mutter) keep a minimised window mapped, for their live previews, so Tk goes on reporting
it as "normal" and on screen, even right after its own `iconify`, and its `deiconify` then does
nothing. The window manager does mark it, on the window it manages (Tk's wrapper, the parent of
the toplevel's own window): ICCCM's WM_STATE is Iconic and EWMH's _NET_WM_STATE holds
_NET_WM_STATE_HIDDEN (both checked with xprop). `minimised` reads those; `restore` asks the window
manager to activate the window (_NET_ACTIVE_WINDOW), which brings a minimised one back where it was.

Through libX11 with ctypes, on a connection of our own, used from Tk's thread only. Only windows
Tk has seen mapped are asked about, so each one exists on the server by then (an X error on this
connection would end the program: Xlib's default handler exits). Without libX11 or a display,
nothing is ever minimised here, as before.
"""

from __future__ import annotations

import ctypes
import ctypes.util

_ICONIC = 3                 # WM_STATE's IconicState
_CLIENT_MESSAGE = 33
_SUBSTRUCTURE_MASKS = (1 << 19) | (1 << 20)   # SubstructureNotifyMask | SubstructureRedirectMask
_FROM_APPLICATION = 1       # _NET_ACTIVE_WINDOW's source indication: a normal application

_ulong = ctypes.c_ulong


class _ClientMessage(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", _ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("window", _ulong), ("message_type", _ulong),
                ("format", ctypes.c_int), ("data", ctypes.c_long * 5)]


class _Event(ctypes.Union):
    _fields_ = [("xclient", _ClientMessage), ("pad", ctypes.c_long * 24)]   # (an XEvent is 24 longs)


class _Display:
    """One connection to the X server a window is on, with the calls used here."""

    def __init__(self, name: str):
        found = ctypes.util.find_library("X11")
        if not found:
            raise OSError("no libX11")
        x = self.x = ctypes.CDLL(found)
        x.XOpenDisplay.restype = ctypes.c_void_p
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XInternAtom.restype = _ulong
        x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        x.XQueryTree.argtypes = [ctypes.c_void_p, _ulong, ctypes.POINTER(_ulong), ctypes.POINTER(_ulong),
                                 ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint)]
        x.XGetWindowProperty.argtypes = [
            ctypes.c_void_p, _ulong, _ulong, ctypes.c_long, ctypes.c_long, ctypes.c_int, _ulong,
            ctypes.POINTER(_ulong), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(_ulong),
            ctypes.POINTER(_ulong), ctypes.POINTER(ctypes.c_void_p)]
        x.XFree.argtypes = [ctypes.c_void_p]
        x.XSendEvent.argtypes = [ctypes.c_void_p, _ulong, ctypes.c_int, ctypes.c_long, ctypes.POINTER(_Event)]
        x.XFlush.argtypes = [ctypes.c_void_p]
        self.handle = x.XOpenDisplay(name.encode())
        if not self.handle:
            raise OSError(f"can't open display {name!r}")
        self._atoms: dict[str, int] = {}

    def atom(self, name: str) -> int:
        if name not in self._atoms:
            self._atoms[name] = self.x.XInternAtom(self.handle, name.encode(), 0)
        return self._atoms[name]

    def parent_and_root(self, window: int) -> tuple[int, int]:
        root, parent, children, count = _ulong(), _ulong(), ctypes.c_void_p(), ctypes.c_uint()
        if not self.x.XQueryTree(self.handle, window, ctypes.byref(root), ctypes.byref(parent),
                                 ctypes.byref(children), ctypes.byref(count)):
            return 0, 0
        if children:
            self.x.XFree(children)
        return parent.value, root.value

    def longs(self, window: int, name: str) -> list[int]:
        """A 32-bit property's values (as C longs, as Xlib hands them over); [] when it isn't set."""
        kind, size, count, left, data = _ulong(), ctypes.c_int(), _ulong(), _ulong(), ctypes.c_void_p()
        status = self.x.XGetWindowProperty(self.handle, window, self.atom(name), 0, 64, 0, 0,   # (any type)
                                           ctypes.byref(kind), ctypes.byref(size), ctypes.byref(count),
                                           ctypes.byref(left), ctypes.byref(data))
        if status != 0 or not data:
            return []
        try:
            return list(ctypes.cast(data, ctypes.POINTER(ctypes.c_long))[:count.value]) if size.value == 32 else []
        finally:
            self.x.XFree(data)

    def activate(self, window: int, root: int) -> None:
        event = _Event()
        message = event.xclient
        message.type, message.send_event, message.display = _CLIENT_MESSAGE, 1, self.handle
        message.window, message.message_type, message.format = window, self.atom("_NET_ACTIVE_WINDOW"), 32
        message.data[0] = _FROM_APPLICATION
        self.x.XSendEvent(self.handle, root, 0, _SUBSTRUCTURE_MASKS, ctypes.byref(event))
        self.x.XFlush(self.handle)


_displays: dict[str, _Display | None] = {}


def _display(window) -> _Display | None:
    name = window.winfo_screen()
    if name not in _displays:
        try:
            _displays[name] = _Display(name)
        except (OSError, AttributeError):
            _displays[name] = None
    return _displays[name]


def _managed(window) -> tuple[_Display, int, int] | None:
    """The display, the window the window manager manages for `window`'s toplevel (Tk's wrapper)
    and the root window; None when it isn't mapped (or there's no X to ask)."""
    top = window.winfo_toplevel()
    if not top.winfo_ismapped():
        return None
    display = _display(top)
    if display is None:
        return None
    wrapper, root = display.parent_and_root(top.winfo_id())
    if not wrapper or wrapper == root:
        return None
    return display, wrapper, root


def minimised(window) -> bool:
    """Is `window`'s toplevel minimised, though Tk still calls it mapped?"""
    found = _managed(window)
    if found is None:
        return False
    display, wrapper, _ = found
    if display.longs(wrapper, "WM_STATE")[:1] == [_ICONIC]:
        return True
    return display.atom("_NET_WM_STATE_HIDDEN") in display.longs(wrapper, "_NET_WM_STATE")


def restore(window) -> None:
    """Ask the window manager to bring `window`'s toplevel back from minimised and make it active."""
    found = _managed(window)
    if found is not None:
        display, wrapper, root = found
        display.activate(wrapper, root)
