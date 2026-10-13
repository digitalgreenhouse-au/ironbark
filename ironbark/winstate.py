"""Is a window minimised, and bringing it back: Tk's own `wm state` and `deiconify`, and where the
window manager keeps a minimised window mapped (platforms.MINIMISED_STAYS_MAPPED), the window
manager's own answer (x11.py), since Tk's then says "normal" and its deiconify changes nothing.
No UI of its own.
"""

from __future__ import annotations

from . import platforms


def minimised(window) -> bool:
    """Is `window` (a Tk or Toplevel) minimised?"""
    if window.state() == "iconic":
        return True
    if platforms.MINIMISED_STAYS_MAPPED and window.state() == "normal":
        from . import x11

        return x11.minimised(window)
    return False


def on_screen(window) -> bool:
    """Can anything `window` (any widget) draws be seen? Not while its window is minimised or hidden."""
    if not window.winfo_viewable():
        return False
    if platforms.MINIMISED_STAYS_MAPPED:
        from . import x11

        return not x11.minimised(window)
    return True


def restore(window) -> None:
    """Show `window` again, from minimised or hidden: Tk's deiconify, or the window manager's way
    when it's a mapped window minimised there."""
    if platforms.MINIMISED_STAYS_MAPPED and window.state() == "normal":
        from . import x11

        if x11.minimised(window):
            x11.restore(window)
            return
    window.deiconify()
