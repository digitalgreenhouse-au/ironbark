"""winstate: minimised or not, and bringing a window back, also where the window manager keeps a
minimised window mapped (Cinnamon, GNOME). No window: Tk and X are stood in for."""

from types import SimpleNamespace

from ironbark import platforms, winstate, x11


def window(state="normal", viewable=True, calls=None):
    calls = [] if calls is None else calls
    return SimpleNamespace(state=lambda: state, winfo_viewable=lambda: viewable,
                           deiconify=lambda: calls.append("deiconify"))


def stays_mapped(monkeypatch, hidden: bool, calls: list):
    monkeypatch.setattr(platforms, "MINIMISED_STAYS_MAPPED", True)
    monkeypatch.setattr(x11, "minimised", lambda w: hidden)
    monkeypatch.setattr(x11, "restore", lambda w: calls.append("window manager"))


def test_tk_knows_when_the_window_manager_unmaps_it(monkeypatch):
    monkeypatch.setattr(platforms, "MINIMISED_STAYS_MAPPED", False)
    calls = []
    assert winstate.minimised(window("iconic")) and not winstate.minimised(window())
    assert not winstate.on_screen(window("iconic", viewable=False)) and winstate.on_screen(window())
    winstate.restore(window("iconic", calls=calls))
    assert calls == ["deiconify"]


def test_a_window_minimised_but_still_mapped_is_minimised_and_off_screen(monkeypatch):
    calls = []
    stays_mapped(monkeypatch, True, calls)
    w = window(calls=calls)
    assert winstate.minimised(w) and not winstate.on_screen(w)
    winstate.restore(w)
    assert calls == ["window manager"]   # Tk's deiconify would change nothing


def test_a_window_on_screen_is_left_to_tk(monkeypatch):
    calls = []
    stays_mapped(monkeypatch, False, calls)
    w = window(calls=calls)
    assert not winstate.minimised(w) and winstate.on_screen(w)
    winstate.restore(window("withdrawn", viewable=False, calls=calls))
    assert calls == ["deiconify"]


def test_without_x_nothing_is_minimised(monkeypatch):
    monkeypatch.setattr(x11, "_displays", {":9": None})
    top = SimpleNamespace(winfo_ismapped=lambda: True, winfo_screen=lambda: ":9")
    top.winfo_toplevel = lambda: top
    assert not x11.minimised(top)
    x11.restore(top)   # (nothing to ask: nothing happens)


def test_an_x_error_on_our_own_connection_never_ends_the_program(monkeypatch):
    # Xlib's default handler exits the program on any X error. Ours goes in front of the one in
    # place (Tk's): it takes the errors of our own connections and hands every other one on.
    handed_on = []

    def tks_handler(display, event):
        handed_on.append(display)
        return 0
    previous = x11._ERROR_HANDLER(tks_handler)
    installed = []

    def set_handler(handler):
        installed.append(handler)
        return previous
    fake = SimpleNamespace(XSetErrorHandler=set_handler)
    monkeypatch.setattr(x11, "_handler", [])
    monkeypatch.setattr(x11, "_ours", {1234})
    monkeypatch.setattr(x11, "_errors", {})
    x11._install_error_handler(fake)
    x11._install_error_handler(fake)   # (once only)
    assert len(installed) == 1
    ours = installed[0]
    assert ours(1234, None) == 0 and x11._errors == {1234: 1}   # ours: counted, nothing else
    ours(5678, None)
    assert handed_on == [5678]   # Tk's own connection's errors go to Tk, as before
