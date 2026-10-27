"""winstate: minimised or not, and bringing a window back, also where the window manager keeps a
minimised window mapped (Cinnamon, GNOME). No window: Tk and X are stood in for."""

from types import SimpleNamespace

import pytest

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


def _fake_xlib(events: list, tks_handler):
    """Xlib's one error handler for the process, and XSync, as calls noted in `events`."""
    state = {"handler": tks_handler}

    def set_handler(handler):
        previous, state["handler"] = state["handler"], handler
        events.append(("handler", "ours" if handler is x11._TAKE else "Tk's"))
        return previous
    fake = SimpleNamespace(XSetErrorHandler=set_handler, XSync=lambda display, discard: events.append(("sync", display)))
    return fake, state


def _our_connection(fake, handle: int):
    display = object.__new__(x11._Display)   # (no X here: just the parts guarded() uses)
    display.x, display.handle, display._atoms = fake, handle, {}
    return display


def test_an_x_error_on_our_own_connection_never_ends_the_program(monkeypatch):
    # Xlib's default handler exits the program on any X error. Ours stands in front of the one in
    # place (Tk's) while our own calls run: it takes our connection's errors, hands any other one on.
    handed_on, events = [], []

    def tks(display, event):
        handed_on.append(display)
        return 0
    tks_handler = x11._ERROR_HANDLER(tks)
    fake, state = _fake_xlib(events, tks_handler)
    monkeypatch.setattr(x11, "_ours", {1234})
    monkeypatch.setattr(x11, "_errors", {})
    display = _our_connection(fake, 1234)

    def our_calls():
        assert state["handler"] is x11._TAKE
        assert state["handler"](1234, None) == 0 and x11._errors == {1234: 1}   # ours: counted, nothing else
        state["handler"](5678, None)
        assert handed_on == [5678]                                     # any other: on to Tk's, as before
        return display.guarded(lambda: "inner")                        # (a call inside shares it)
    assert display.guarded(our_calls) == "inner"
    assert events == [("handler", "ours"), ("sync", 1234), ("sync", 1234), ("handler", "Tk's")]   # every error read first
    assert state["handler"] is tks_handler and x11._handing_on == [None]


def test_calls_on_two_connections_inside_each_other_put_tks_handler_back_once(monkeypatch):
    # (Two screens: a call on one inside a call on the other must not hand back ours as "the one
    # before", nor leave Xlib's own handler — which ends the program — in place afterwards.)
    events = []
    tks_handler = x11._ERROR_HANDLER(lambda display, event: 0)
    fake, state = _fake_xlib(events, tks_handler)
    first, second = _our_connection(fake, 1234), _our_connection(fake, 5678)
    assert first.guarded(lambda: second.guarded(lambda: state["handler"] is x11._TAKE))
    assert events == [("handler", "ours"), ("sync", 5678), ("sync", 1234), ("handler", "Tk's")]
    assert state["handler"] is tks_handler and x11._depth == [0] and x11._handing_on == [None]


def test_tks_error_handler_is_back_between_our_calls_even_after_one_fails(monkeypatch):
    # Xlib's error handler is one for the whole process, and Tk's connection meets X errors all the
    # time: left in place, ours ran Python inside Tk's Xlib calls, and under a virtual screen with no
    # window manager (the Linux test machine's) the process crashed in a later test's focus -force.
    events = []
    tks_handler = x11._ERROR_HANDLER(lambda display, event: 0)
    fake, state = _fake_xlib(events, tks_handler)
    display = _our_connection(fake, 1234)

    def failing():
        raise OSError("gone")
    with pytest.raises(OSError):
        display.guarded(failing)
    assert state["handler"] is tks_handler and x11._depth == [0]
    assert events == [("handler", "ours"), ("sync", 1234), ("handler", "Tk's")]
