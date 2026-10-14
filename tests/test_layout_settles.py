"""Layouts that settle: a label that wraps at the width it's laid out at must never ask for more than
that width, or a page whose width nothing outside fixes (Settings, built while the window is hidden)
can keep growing and rewrapping for ever — Tk's idle loop never ends and the window freezes. Seen on
a 1024 x 768 screen (the test runner's), and here at the display scalings below. Tk tests (skipped
without a display)."""

from __future__ import annotations

import _tkinter
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace

import pytest
from test_light_mini import make_window  # noqa: F401  (make_window is a fixture)

from ironbark import settings_view, theme
from ironbark.tabbar import SettingRow

IDLE_ONLY = _tkinter.IDLE_EVENTS | _tkinter.DONT_WAIT
IDLE_ROUNDS = 20000   # far more rounds of idle work than any window here needs to settle


class NeverSettles(AssertionError):
    pass


def _bounded_update_idletasks(self):
    """update_idletasks, but a layout that never settles fails the test instead of hanging it."""
    for _ in range(IDLE_ROUNDS):
        if not self.tk.dooneevent(IDLE_ONLY):
            return
    raise NeverSettles("Tk's idle work never ran out: a layout that never settles")


def _bounded_update(self):
    """update, bounded the same way (the window fixture pumps events with it)."""
    for _ in range(IDLE_ROUNDS):
        if not self.tk.dooneevent(_tkinter.ALL_EVENTS | _tkinter.DONT_WAIT):
            return
    raise NeverSettles("Tk's events never ran out: a layout that never settles")


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"no display: {error}")
    r.withdraw()
    theme.apply(r)
    yield r
    theme.use(theme.DARK)
    r.destroy()


def test_a_setting_s_description_never_asks_for_more_than_its_column(root):
    row = SettingRow(root, root, "Local network", settings_view.LOCAL_NETWORK_TEXT, tk.BooleanVar())
    for width in range(80, 700):
        row._column_changed(SimpleNamespace(width=width))
        assert row.about.winfo_reqwidth() <= width, width


def test_wrap_inside_keeps_a_ttk_label_inside_its_width(root):
    for style in ("Muted.TLabel", "Info.TLabel"):
        label = ttk.Label(root, text=settings_view.LOCAL_NETWORK_TEXT, style=style, justify="left")
        for width in range(200, 700):   # (narrower, one word can be wider than the line: no wrap helps)
            theme.wrap_inside(label, width)
            assert label.winfo_reqwidth() <= width, (style, width)


# The display scalings at which the player's hidden Settings page never settled (measured on the
# development PC, 15/10/2026): which widths fight depends on the fonts, so other machines hang at
# other scalings — the runner at 100% — but none may here.
@pytest.mark.parametrize("scaling", [1.0, 2.5, 2.55, 2.7, 2.75])
def test_the_player_window_settles(make_window, monkeypatch, scaling):  # noqa: F811
    monkeypatch.setattr(tk.Misc, "update_idletasks", _bounded_update_idletasks)
    monkeypatch.setattr(tk.Misc, "update", _bounded_update)
    try:
        window, root, player, errors = make_window(scaling=scaling)
        window._open_settings()     # (and shown: its width now comes from the window)
        root.update_idletasks()
        window._close_settings()
        root.update_idletasks()
    except NeverSettles:
        # Every root in the process shares one idle queue: the window that never settles is
        # destroyed here, or its layout would keep running inside every later test.
        if tk._default_root is not None:
            tk._default_root.destroy()
        raise
    assert not errors
