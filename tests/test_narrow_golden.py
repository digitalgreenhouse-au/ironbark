"""The narrow window must stay exactly as it was before the wide layouts were added.

golden_narrow.json was recorded from the main window as it was then, at three narrow sizes and at
100% and 150% display scaling, and after each of the things that rebuild part of the layout (a trip
to a wide size and back, a theme switch, songs queued, the cover switched off and on, a mini-strip
round trip, closing the last tab and opening one again). For each: how the parts are packed (order
and options), where every part sits and how big it is, the cover's size, and the track list's
columns. Positions depend on the fonts, so they are kept per setup (Windows, Tk's default font and
its size, the Tk, and whether the process is DPI-aware — once a test has made it so, Tk's default
fonts follow the screen's real scaling) and compared only on a setup they were recorded on; the
packing is compared everywhere. (In a whole test run an earlier test can leave Tk's default font at
another size for later windows: then only the packing is compared. Run this file on its own for the
full check.)

Recording it again (only when the narrow layout is meant to change), for both setups:
    set IRONBARK_RECORD_GOLDEN=1 && uv run pytest -q -p no:cacheprovider tests/test_narrow_golden.py
    set IRONBARK_GOLDEN_DPI_AWARE=1 && (the same again)
"""

from __future__ import annotations

import json
import os
import sys
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont

import pytest

from ironbark import theme
from test_light_mini import _pump, make_window  # noqa: F401  (make_window is a fixture)

GOLDEN = Path(__file__).with_name("golden_narrow.json")
RECORD = os.environ.get("IRONBARK_RECORD_GOLDEN") == "1"
if os.environ.get("IRONBARK_GOLDEN_DPI_AWARE") == "1" and sys.platform == "win32":   # (to record that setup)
    import ctypes

    ctypes.windll.shcore.SetProcessDpiAwareness(1)
SIZES = ((420, 1000), (520, 900), (300, 260))   # logical px (real px = these × the scaling)
SCALINGS = (1.0, 1.5)

# The window's parts, by the name they have on the window (dotted: a part of a part).
NAMED = ("bar", "body", "head", "art_box", "art_label", "head_text", "title_label", "sub_label", "info_row",
         "format_label", "head_icons", "info_button", "lock_button", "status_dot", "seek_row", "seek", "pos_label",
         "remain_label", "transport", "shuffle_button", "previous_button", "play_button", "next_button",
         "repeat_button", "volume_badge", "status_row", "status_label", "undo_link", "up_next_label", "empty_view",
         "empty_button", "playlist.frame", "playlist.search", "playlist.tree", "playlist.scrollbar",
         "bar.add_btn", "bar.settings_btn", "bar.min_btn", "bar.close_btn")
# The containers whose packing is compared.
PACKED = ("root", "body", "head", "head_text", "info_row", "seek_row", "transport", "playlist.frame")


def _part(window, name: str):
    if name == "root":
        return window.root
    if name == "info_row":
        return window.format_label.master
    target = window
    for step in name.split("."):
        target = getattr(target, step, None)
        if target is None:
            return None
    return target


def environment(root) -> str:
    """What the recorded positions depend on, as one key."""
    default = tkfont.nametofont("TkDefaultFont", root=root).actual()
    aware = False
    if sys.platform == "win32":
        import ctypes

        aware = bool(ctypes.windll.user32.IsProcessDPIAware())
    return (f"{sys.platform} · Tk {root.tk.call('info', 'patchlevel')} · {default['family']} {default['size']}"
            f" · {theme.SEMIBOLD} · {'DPI-aware' if aware else 'not DPI-aware'}")


def snapshot(window, root) -> dict:
    """The narrow layout once it has settled (two looks a moment apart agree): packing, places and
    sizes, the cover, the list's columns."""
    _steady(window, root)
    last = None
    for _ in range(12):
        now = _look(window, root)
        if now == last:
            break
        last = now
        _pump(root, 0.25)
    return now


def _look(window, root) -> dict:
    root.update_idletasks()
    names = {}
    for name in NAMED + PACKED:
        widget = _part(window, name)
        if widget is not None:
            names[str(widget)] = name

    def called(widget) -> str:
        return names.get(str(widget), widget.winfo_class())

    packing = {}
    for name in PACKED:
        container = _part(window, name)
        slaves = []
        for slave in container.pack_slaves():
            info = {k: (called(v) if k == "in" else str(v)) for k, v in slave.pack_info().items()}
            slaves.append([called(slave), info])
        packing[name] = slaves
    left, top = root.winfo_rootx(), root.winfo_rooty()
    places = {}
    for name in NAMED:
        widget = _part(window, name)
        if widget is None:
            continue
        places[name] = ([widget.winfo_rootx() - left, widget.winfo_rooty() - top, widget.winfo_width(),
                         widget.winfo_height()] if widget.winfo_ismapped() else None)
    tree = window.playlist.tree
    shown = [str(c) for c in tree.cget("displaycolumns")]
    req = {name: _part(window, name).winfo_reqheight() for name in ("bar", "head_text", "seek_row", "transport",
                                                                      "status_row", "body")}
    return {"packing": packing, "places": places, "art_size": window.art_size, "requested": req,
            "window": [root.winfo_width(), root.winfo_height()],
            "tree": {"style": str(tree.cget("style")), "columns": shown,
                     "widths": [int(tree.column(c, "width")) for c in shown] if tree.winfo_ismapped() else None}}


def _seen(state: dict) -> dict:
    """A snapshot without what can't be seen: the column widths of a list that isn't on screen (a
    window too small for it) are only fitted once it shows."""
    if state["places"].get("playlist.tree") is None:
        state = {**state, "tree": {**state["tree"], "widths": None}}
    return state


def _settle(root, seconds: float = 0.7):
    _pump(root, seconds)


def _steady(window, root):
    """The cover sized afresh for the window as it now is. (While a window is being resized the
    cover keeps its size through changes of 2 px or less, so without this it would depend on the
    sizes the window passed through on the way, which vary with timing.)"""
    window.art_size = 0
    window._fit_art()
    _pump(root, 0.5)


def walk_states(window, root, player, logical: tuple[int, int], scaling: float):
    """(state name, snapshot) for every state the narrow window is checked in."""
    w, h = round(logical[0] * scaling), round(logical[1] * scaling)
    here = f"{w}x{h}+40+40"
    root.geometry(here)
    _settle(root)
    yield "start", snapshot(window, root)

    root.geometry(f"{round(1920 * scaling)}x{round(1080 * scaling)}+40+40")
    _settle(root)
    root.geometry(here)
    _settle(root)
    yield "after a wide size", snapshot(window, root)

    window.prefs["theme"] = "light"
    window._apply_theme()
    _settle(root)
    yield "light", snapshot(window, root)
    window.prefs["theme"] = "dark"
    window._apply_theme()
    _settle(root)

    window._queue_tracks([player.track, player.track], False)
    _settle(root)
    yield "songs queued", snapshot(window, root)

    window.show_art.set(False)
    window._toggle_art()
    _settle(root)
    yield "cover off", snapshot(window, root)
    window.show_art.set(True)
    window._toggle_art()
    _settle(root)
    yield "cover on again", snapshot(window, root)

    window.toggle_mini()
    _settle(root)
    window.toggle_mini()
    _settle(root)
    yield "after the mini strip", snapshot(window, root)

    window.close_tab(0)
    _settle(root)
    yield "empty", snapshot(window, root)
    window.reopen_tab()
    for _ in range(100):   # read again from its folder on a worker: wait until it's in
        tab = window.tabs[0] if window.tabs else None
        if tab is not None and tab.tracks is not None and not tab.loading and tab.info is not None:
            break
        _pump(root, 0.1)
    _settle(root, 1.2)
    yield "a tab again", snapshot(window, root)


def write_golden(golden: dict):
    """One line per state (setup → case → state), so the file stays small and a change shows as the
    states it touched."""
    lines = ["{"]
    for i, (setup, cases) in enumerate(sorted(golden.items())):
        lines.append(f" {json.dumps(setup)}: {{")
        for j, (case, states) in enumerate(sorted(cases.items())):
            lines.append(f"  {json.dumps(case)}: {{")
            items = sorted(states.items())
            for k, (state, snap) in enumerate(items):
                comma = "," if k < len(items) - 1 else ""
                lines.append(f"   {json.dumps(state)}: {json.dumps(snap, sort_keys=True, separators=(',', ':'))}{comma}")
            lines.append("  }" + ("," if j < len(cases) - 1 else ""))
        lines.append(" }" + ("," if i < len(golden) - 1 else ""))
    lines.append("}")
    GOLDEN.write_text("\n".join(lines) + "\n", "utf-8", newline="\n")


def _key(logical, scaling) -> str:
    return f"{logical[0]}x{logical[1]}@{round(scaling * 100)}"


def _differences(want, got, path="") -> list[str]:
    if isinstance(want, dict) and isinstance(got, dict):
        out = []
        for key in sorted(set(want) | set(got)):
            out += _differences(want.get(key), got.get(key), f"{path}/{key}")
        return out
    return [] if want == got else [f"{path}: was {want!r}, now {got!r}"]


@pytest.mark.parametrize("scaling", SCALINGS, ids=lambda s: f"{round(s * 100)}%")
@pytest.mark.parametrize("logical", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_the_narrow_window_is_as_it_was(make_window, logical, scaling):
    window, root, player, errors = make_window(scaling=scaling, volume=0.8,
                                               geometry=f"{round(logical[0] * scaling)}x"
                                                        f"{round(logical[1] * scaling)}+40+40")
    got = dict(walk_states(window, root, player, logical, scaling))
    assert not errors
    key = _key(logical, scaling)
    setup = environment(root)
    if RECORD:
        golden = json.loads(GOLDEN.read_text("utf-8")) if GOLDEN.exists() else {}
        golden.setdefault(setup, {})[key] = got
        write_golden(golden)
        return
    golden = json.loads(GOLDEN.read_text("utf-8"))
    # On a recorded setup everything is compared; elsewhere (fonts differ) only how the parts are
    # packed, against whichever setup it matches (the gaps are rounded from the exact scaling).
    setups = [golden[setup]] if setup in golden else list(golden.values())
    tries = []
    for cases in setups:
        problems = []
        for state, want in cases[key].items():
            want, have = _seen(want), _seen(got[state])
            if setup not in golden:
                want, have = want["packing"], have["packing"]
            problems += [f"{state}{line}" for line in _differences(want, have)]
        tries.append(problems)
    problems = min(tries, key=len)
    assert not problems, "the narrow window changed:\n" + "\n".join(problems[:40])
