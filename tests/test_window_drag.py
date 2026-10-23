"""Dragging the window by our own title bar (chrome.py; Windows, where the system's is taken
away): the window follows the pointer, held where it was grabbed, and stays put once the button is
let go. (On Windows 10 the old hand-off to Windows' own move loop could miss the release, and the
window ran on after the pointer.) Run here with the custom title bar switched on."""
from __future__ import annotations

import gc
from types import SimpleNamespace

import pytest

from ironbark import chrome, placement, theme


@pytest.fixture
def root(monkeypatch):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"no display ({error})")
    theme.apply(root)
    monkeypatch.setattr(chrome, "CUSTOM", True)
    root.geometry("400x300+100+120")
    root.update()
    yield root
    chrome._moving.clear()
    root.destroy()
    del root
    gc.collect()


def at(root) -> tuple[int, int]:
    root.update_idletasks()
    parsed = placement.parse(root.geometry())
    return parsed[2], parsed[3]


def pointer(x_root: int, y_root: int, **more) -> SimpleNamespace:
    return SimpleNamespace(x_root=x_root, y_root=y_root, **more)


def test_the_window_follows_the_pointer_and_stops_when_let_go(root):
    start = at(root)
    chrome.start_move(root, pointer(start[0] + 50, start[1] + 10))
    for step in range(1, 61):                     # a long drag: a second or two of movement
        assert chrome.follow_move(root, pointer(start[0] + 50 + 7 * step, start[1] + 10 + step))
    assert at(root) == (start[0] + 420, start[1] + 60)
    assert chrome.end_move(root)
    assert not chrome.follow_move(root, pointer(5, 5))   # let go: the pointer moving on moves nothing
    root.update()
    assert at(root) == (start[0] + 420, start[1] + 60) and not chrome.end_move(root)


def test_off_windows_the_system_title_bar_does_the_dragging(root, monkeypatch):
    monkeypatch.setattr(chrome, "CUSTOM", False)
    chrome.start_move(root, pointer(150, 130))
    assert not chrome.follow_move(root, pointer(400, 400)) and at(root) == (100, 120)


def test_the_tab_strip_drags_the_window_from_its_empty_part(root):
    from ironbark.tabbar import TabBar

    bar = TabBar(root, root, on_select=lambda i: None, on_close=lambda i: None, on_new=lambda: None,
                 on_settings=lambda: None, on_double=lambda: None, on_quit=lambda: None)
    bar.pack(fill="x")
    bar.set_tabs(["One"], 0, None)
    root.update()
    y = theme.px(theme.ROW_Y)
    empty = next(x for x in range(bar.canvas.winfo_width() // 2, 0, -1) if bar._slot_at(x, y) is None)
    start = at(root)
    press = pointer(start[0] + 200, start[1] + 8, x=empty, y=y)
    bar._pressed(press)
    bar._drag_motion(pointer(start[0] + 260, start[1] + 48, x=empty + 60, y=y + 40))
    assert at(root) == (start[0] + 60, start[1] + 40)
    bar._released(pointer(start[0] + 260, start[1] + 48, x=empty + 60, y=y + 40))
    bar._drag_motion(pointer(start[0] + 900, start[1] + 48, x=empty + 700, y=y + 40))
    assert at(root) == (start[0] + 60, start[1] + 40)   # (let go: it stays)
    bar.destroy()


def test_on_windows_each_step_is_a_move_alone_by_the_pointers_travel(root, monkeypatch):
    """On Windows the frame is moved by itself (SetWindowPos: no size, no repaint), as Windows'
    own drag does: moved through Tk, the whole window was redrawn at every step, and on Windows 10
    it flickered in stripes while it moved. A step that doesn't move it sends nothing."""
    from ironbark import platforms

    moves = []
    monkeypatch.setattr(platforms, "IS_WINDOWS", True)
    monkeypatch.setattr(chrome, "_frame_place", lambda window: (77, (93, 112)))   # (the frame's own rect)
    monkeypatch.setattr(chrome, "_set_window_pos", lambda hwnd, x, y: moves.append((hwnd, x, y)))
    geometry = []
    monkeypatch.setattr(root, "geometry", lambda *a: geometry.append(a) or "400x300+100+120")
    chrome.start_move(root, pointer(150, 130))
    assert chrome.follow_move(root, pointer(160, 135))
    assert chrome.follow_move(root, pointer(160, 135))       # (the same place: nothing sent)
    assert chrome.follow_move(root, pointer(400, 90))
    assert moves == [(77, 103, 117), (77, 343, 72)]
    assert all(not a for a in geometry)                      # (Tk only asked where it was, never told)
    assert chrome.end_move(root) and not chrome.follow_move(root, pointer(500, 500)) and len(moves) == 2
