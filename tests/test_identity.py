"""0.23.0: the app icon (drawn in code, saved in the package, on every window, the
taskbar's AppUserModelID) and the tab strip that repaints its tabs instead of rebuilding them."""

from __future__ import annotations

import gc
import subprocess
import sys
import tkinter as tk
from importlib import resources
from types import SimpleNamespace

import pytest
from PIL import Image, ImageChops

from ironbark import appicon, theme

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows icons / AppUserModelID")


@pytest.fixture
def root():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display ({exc})")
    root.geometry("460x300+0+0")
    yield root
    root.destroy()
    del root
    gc.collect()  # here, on the main thread (Tk objects freed on another thread abort Python)


# -- the picture ------------------------------------------------------------------------------
def _close(a, b, tolerance=12):
    return all(abs(x - y) <= tolerance for x, y in zip(a, b))


@pytest.mark.parametrize("size", appicon.SIZES)
def test_every_size_is_a_green_tile_with_a_dark_record_and_a_green_label(size):
    icon = appicon.draw(size)
    assert icon.size == (size, size) and icon.mode == "RGBA"
    c = size // 2
    green, ink = theme.rgb(theme.DARK.ACCENT), theme.rgb(theme.DARK.ACCENT_INK)
    assert icon.getpixel((0, 0))[3] < 64                          # rounded corner: see-through
    assert _close(icon.getpixel((c, 0))[:3], green)                # the tile's edge
    record, label_r = appicon.DISC * size, appicon.DISC * size * appicon.LABEL
    ring = c + round((record + label_r) / 2)
    assert _close(icon.getpixel((c, ring))[:3], ink, 30)           # the record, between label and edge
    label = c + max(1, round(label_r * 0.6))
    assert _close(icon.getpixel((label, c))[:3], green, 40)        # the centre label
    assert all(p[3] == 255 for p in [icon.getpixel((c, c)), icon.getpixel((1, c))])


def test_the_colours_are_the_theme_tokens_and_read_on_both_backgrounds():
    # The record against the tile, and the tile against Dark's strip and Light's lightest surface:
    # each at least the 3:1 WCAG asks of icons (SC 1.4.11).
    assert theme.contrast_ratio(theme.DARK.ACCENT, theme.DARK.ACCENT_INK) >= theme.AA_GRAPHICS
    for background in (theme.DARK.STRIP, theme.DARK.BG, theme.LIGHT.SURFACE, theme.LIGHT.BG):
        assert theme.contrast_ratio(theme.DARK.ACCENT, background) >= theme.AA_GRAPHICS


def test_fine_details_only_where_they_can_be_seen():
    hole, grooves = appicon.HOLE_FROM, appicon.GROOVE_FROM
    assert min(appicon.SIZES) < hole <= grooves < max(appicon.SIZES)
    small, big = appicon.draw(16), appicon.draw(256)
    assert _close(small.getpixel((8, 8))[:3], theme.rgb(theme.DARK.ACCENT), 40)   # 16 px: no hole
    assert _close(big.getpixel((128, 128))[:3], theme.rgb(theme.DARK.ACCENT_INK), 8)  # 256 px: the hole


# -- the files in the package ------------------------------------------------------------------
def test_the_files_are_in_the_package_and_match_the_drawing():
    """The committed PNGs are what `draw` makes (rebuild them with `python -m ironbark.appicon`
    after changing the design), and they're read through importlib.resources, as an installed
    package would."""
    folder = resources.files("ironbark") / "assets"
    for size in appicon.SIZES:
        stored = appicon.load(size)
        assert (folder / appicon.png_name(size)).is_file()
        assert stored.size == (size, size)
        diff = ImageChops.difference(stored, appicon.draw(size)).getextrema()
        assert max(high for _low, high in diff) <= 2, f"{size} px differs from draw()"


def test_the_ico_holds_every_size_drawn_for_that_size():
    path = appicon.assets() / appicon.ICO_NAME
    ico = Image.open(path)
    assert sorted(ico.info["sizes"]) == [(s, s) for s in appicon.SIZES]
    for size in appicon.SIZES:
        ico.size = (size, size)
        frame = ico.convert("RGBA")
        diff = ImageChops.difference(frame, appicon.load(size)).getextrema()
        assert max(high for _low, high in diff) <= 2, f"the .ico's {size} px isn't the drawn one"


def test_write_assets_rebuilds_the_set(tmp_path):
    written = appicon.write_assets(tmp_path)
    assert {p.name for p in written} == {appicon.png_name(s) for s in appicon.SIZES} | {appicon.ICO_NAME}


def test_the_wheel_takes_the_whole_package_folder():
    """pyproject builds the wheel from the package folder (hatch: every file in it, not only .py),
    so the icon files ship with it. (Checked by building a wheel on 05/10/2026.)"""
    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads((Path(appicon.__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["ironbark"]
    assert "exclude" not in wheel and "only-include" not in wheel


@windows_only
def test_windows_reads_every_size_of_the_ico():
    import ctypes

    user32 = ctypes.WinDLL("user32")
    user32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_uint]
    user32.LoadImageW.restype = ctypes.c_void_p
    user32.DestroyIcon.argtypes = [ctypes.c_void_p]
    for size in appicon.SIZES:
        handle = user32.LoadImageW(None, str(appicon.assets() / appicon.ICO_NAME), appicon.IMAGE_ICON, size, size,
                                   appicon.LR_LOADFROMFILE)
        assert handle, f"Windows couldn't read the {size} px icon"
        assert _icon_size(handle) == size
        user32.DestroyIcon(handle)


# -- on the windows ------------------------------------------------------------------------------
def _icon_size(hicon) -> int:
    import ctypes
    from ctypes import wintypes

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL), ("x", wintypes.DWORD), ("y", wintypes.DWORD),
                    ("mask", ctypes.c_void_p), ("colour", ctypes.c_void_p)]

    class BITMAP(ctypes.Structure):
        _fields_ = [("type", ctypes.c_long), ("width", ctypes.c_long), ("height", ctypes.c_long),
                    ("stride", ctypes.c_long), ("planes", wintypes.WORD), ("bits", wintypes.WORD),
                    ("data", ctypes.c_void_p)]

    user32, gdi32 = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32")
    user32.GetIconInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(ICONINFO)]
    gdi32.GetObjectW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    info = ICONINFO()
    assert user32.GetIconInfo(hicon, ctypes.byref(info))
    bitmap = BITMAP()
    gdi32.GetObjectW(info.colour, ctypes.sizeof(bitmap), ctypes.byref(bitmap))
    gdi32.DeleteObject(info.colour)
    gdi32.DeleteObject(info.mask)
    return bitmap.width


def _class_icons(window) -> tuple[int, int]:
    """(big, small) size of the icons Windows holds for this Tk window's class."""
    import ctypes

    user32 = ctypes.WinDLL("user32")
    user32.GetClassLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.GetClassLongPtrW.restype = ctypes.c_void_p
    window.update()
    hwnd = int(window.wm_frame(), 16)
    return (_icon_size(user32.GetClassLongPtrW(hwnd, appicon.GCLP_HICON)),
            _icon_size(user32.GetClassLongPtrW(hwnd, appicon.GCLP_HICONSM)))


def test_apply_puts_the_icon_on_the_window_and_keeps_the_pictures(root):
    assert appicon.apply(root)
    assert len(root._dg_icon_images) == len(appicon.WINDOW_SIZES)
    assert sorted(p.width() for p in root._dg_icon_images) == sorted(appicon.WINDOW_SIZES)


def test_apply_never_raises(root, monkeypatch):
    monkeypatch.setattr(appicon, "load", lambda size: (_ for _ in ()).throw(OSError("gone")))
    assert appicon.apply(root) is False      # Tk's own icon stays; nothing breaks


@windows_only
def test_every_window_gets_the_icon_at_the_size_windows_asks_for(root):
    """Tk's own iconphoto would leave its 16 / 32 px pictures on the class (sharp only at 100%);
    apply() swaps in the .ico's pictures at Windows' own sizes — and a Toplevel made afterwards
    (Now Playing, Meters, Library …) shares them."""
    import ctypes

    user32 = ctypes.WinDLL("user32")
    wanted = (user32.GetSystemMetrics(appicon.SM_CXICON), user32.GetSystemMetrics(appicon.SM_CXSMICON))
    appicon.apply(root)
    assert _class_icons(root) == wanted
    later = tk.Toplevel(root)
    assert _class_icons(later) == wanted
    later.destroy()


@windows_only
def test_the_app_id_is_set_in_its_own_process():
    """Run in a separate Python so the test run's own process keeps its ID."""
    code = "from ironbark import appicon; print(appicon.app_id()); print(appicon.set_app_id()); print(appicon.app_id())"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["None", "True", appicon.APP_ID]


def test_the_player_sets_its_app_id_before_making_any_window(monkeypatch):
    from ironbark import __main__ as start

    order = []

    class Stop(Exception):
        pass

    def no_tk():
        order.append("window")
        raise Stop

    monkeypatch.setattr(start, "_start_crash_log", lambda: None)
    monkeypatch.setattr(start, "_enable_crisp_text_on_windows", lambda: None)
    monkeypatch.setattr(start.appicon, "set_app_id", lambda: order.append("app id"))
    monkeypatch.setattr(start.tk, "Tk", no_tk)
    with pytest.raises(Stop):
        start.main([])
    assert order == ["app id", "window"]


# -- the tab strip: repainted, not rebuilt ----------------------------------------------------------
def _bar(root, **calls):
    from ironbark.tabbar import TabBar

    noop = lambda *a: None  # noqa: E731
    handlers = dict(on_select=noop, on_close=noop, on_new=noop, on_settings=noop, on_double=noop, on_quit=noop)
    handlers.update(calls)
    bar = TabBar(root, root, **handlers)
    bar.pack(fill="x")
    root.update()
    return bar


def _widgets(widget) -> set[str]:
    out = {str(widget)}
    for child in widget.winfo_children():
        out |= _widgets(child)
    return out


def test_switching_tabs_makes_and_destroys_no_widgets(root):
    bar = _bar(root)
    bar.set_tabs(["Tapestry", "Kind of Blue", "Rumours"], 0, 2)
    root.update()
    before, slots = _widgets(bar), list(bar._tab_widgets)
    for current in (1, 2, 0, 1):
        bar.set_tabs(["Tapestry", "Kind of Blue", "Rumours"], current, 2)
        root.update()
        assert _widgets(bar) == before and bar._tab_widgets == slots
        for i, tab in enumerate(bar._tab_widgets):
            assert tab.cget("bg") == (theme.BG if i == current else theme.STRIP)
            assert tab.label.cget("fg") == (theme.TEXT if i == current else theme.MUTED)
            assert tab.line.cget("bg") == (theme.ACCENT if i == 2 else tab.cget("bg"))   # playing line


def test_slots_follow_the_number_of_tabs(root):
    bar = _bar(root)
    bar.set_tabs(["a", "b", "c"], 0, None)
    first = bar._tab_widgets[0]
    bar.set_tabs(["a", "b", "c", "d", "e"], 4, None)
    assert len(bar._tab_widgets) == 5 and bar._tab_widgets[0] is first
    bar.set_tabs(["a", "b"], 1, None)
    root.update()
    assert len(bar._tab_widgets) == 2 and len(bar.tabs_frame.winfo_children()) == 2
    bar.set_tabs([], -1, None)
    assert bar._tab_widgets == [] and bar.tabs_frame.winfo_children() == []


def test_names_and_tooltips_follow_the_tabs(root):
    bar = _bar(root)
    long = "A very long album name that can't possibly fit in one tab of a narrow window"
    bar.set_tabs(["Short", long], 0, None)
    root.update()
    assert bar._tab_widgets[0].label.cget("text") == "Short" and bar._full_name(0) == ""
    assert bar._tab_widgets[1].label.cget("text").endswith("…") and bar._full_name(1) == long
    bar.set_tabs([long, "Short"], 1, None)      # moved: the slots swap names, the tips follow
    root.update()
    assert bar._full_name(0) == long and bar._tab_widgets[1].label.cget("text") == "Short"


def test_a_slim_tab_hides_its_name_and_tips_on_the_tab_itself(root):
    root.geometry("200x300")
    bar = _bar(root)
    bar.set_tabs([f"Album {i}" for i in range(12)], 0, None)
    root.update()
    slim = bar._tab_widgets[3]
    assert not slim.label.winfo_manager() and slim.shown == ""
    assert bar._full_name(3) == "Album 3"


def test_dragging_a_tab_still_moves_it_along(root):
    """The drag state lives on the bar; the tab pressed keeps its widget now, and the names move
    along the slots as the tabs swap (ui.move_tab → set_tabs)."""
    titles = ["One", "Two", "Three", "Four"]
    moves = []

    def on_move(source, target):
        moves.append((source, target))
        titles.insert(target, titles.pop(source))
        bar.set_tabs(list(titles), target, None)

    bar = _bar(root, on_move=on_move, on_select=lambda i: bar.set_tabs(list(titles), i, None))
    bar.set_tabs(list(titles), 0, None)
    root.update()
    x0 = bar._tab_widgets[0].winfo_rootx() + 5
    bar._press(0, SimpleNamespace(x_root=x0))
    for step in range(1, 4):
        bar._drag_motion(SimpleNamespace(x_root=x0 + step * bar._slot))
    bar._drag_end()
    assert moves == [(0, 1), (1, 2), (2, 3)]
    assert titles == ["Two", "Three", "Four", "One"]
    assert [t.label.cget("text") for t in bar._tab_widgets] == titles


def test_the_font_list_is_asked_for_once(root, monkeypatch):
    from ironbark import tabbar

    calls = []
    real = tabbar.tkfont.families
    monkeypatch.setattr(tabbar.tkfont, "families", lambda r=None: calls.append(1) or real(r))
    for _ in range(5):
        tabbar.icon_font(root)
    assert len(calls) <= 1
