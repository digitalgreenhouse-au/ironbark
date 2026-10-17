"""Shared test set-up.

- On Windows the test windows are made on a hidden desktop of their own (one per run), so a test
  run never puts windows in front of whatever the person at the computer is doing. They are still real
  windows — mapped, laid out and measured exactly as on the visible desktop. Set
  IRONBARK_TESTS_VISIBLE=1 to watch them on the normal desktop instead.
- Every test gets its own empty cache folder (settings.cache_folder: where the library database
  lives), so no test can touch the real one. (A test that points the settings somewhere else
  takes the cache with them anyway: settings.cache_folder follows settings_path.)
- Two tiers: a test module that makes windows (it names tkinter, or one of the player's windows)
  is marked `ui` here at collection, so `uv run pytest -q -m "not ui"` is the quick tier (no
  window; a few minutes) and the plain run is the whole suite.
- Performance benchmarks are marked `perf` and only run with `--perf`
  (`uv run pytest -q -p no:cacheprovider --perf -m perf`): they build big synthetic libraries
  and take a while.
- Every test runs as if it were the only copy of the player: nothing a test does can show a
  Windows message box, reach a player that's really running, or leave the log or the cycle
  collector set up for the next test.
- `tk_thread_rule`: a test using it fails if any thread started during the test calls into
  tkinter (a Tk call off Tk's own thread can abort the program): the list of (thread, what) it
  caught, checked empty at the end.
- A test run reaches nothing system-wide: no window registers with Windows' media panel or the
  taskbar, and no keyboard hook is installed (DG_LIVE_WINDOWS=1 allows the live checks that do).
  No test makes the test process DPI-aware (that can't be undone, and would leave every later
  test's text too wide for its layout).
  And it runs at below-normal priority, so the computer's other programs come first.
- Nothing a test deletes goes to the computer's real Recycle Bin (recycle.py): it's deleted, and
  noted in `binned`.
"""
from __future__ import annotations

import gc
import itertools
import logging
import os
import re
import sys
import threading
import uuid

import pytest

from ironbark import settings  # makes no window, so it may come before the desktop switch

# One hidden desktop per test run: two runs at once (two branches' suites, say) would otherwise
# share one, and a window of the other run sitting over this one's fools any check of what's at a
# point on the screen (winfo_containing).
HIDDEN_DESKTOP = f"IronbarkTests-{os.getpid()}"


def _use_hidden_desktop():
    """Move this (main) thread to a hidden desktop before any window exists.

    Windows only lets a thread change desktop while it owns no windows, so this has to run before
    tkinter makes its first one — conftest is imported before any test module is.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.CreateDesktopW.restype = wintypes.HANDLE
    user32.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
                                      wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    user32.SetThreadDesktop.restype = wintypes.BOOL
    user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
    generic_all = 0x10000000
    desktop = user32.CreateDesktopW(HIDDEN_DESKTOP, None, None, 0, generic_all, None)
    if not desktop or not user32.SetThreadDesktop(desktop):
        return None   # carry on on the normal desktop
    return desktop    # kept for the life of the process: closing it would strand the windows


def _below_normal_priority():
    """Let the computer's other programs have the processor first (worker processes inherit it)."""
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x00004000)   # BELOW_NORMAL_PRIORITY_CLASS


_hidden_desktop = None
if sys.platform == "win32" and os.environ.get("IRONBARK_TESTS_VISIBLE") != "1":
    _hidden_desktop = _use_hidden_desktop()
if sys.platform == "win32":
    _below_normal_priority()

LIVE_WINDOWS = os.environ.get("DG_LIVE_WINDOWS") == "1"

_numbers = itertools.count()


def pytest_addoption(parser):
    parser.addoption("--perf", action="store_true", default=False,
                     help="also run the performance benchmarks (marked 'perf')")


# A test module that makes windows: it names tkinter, the player's ui module or one of its
# windows (…Window / …View), or a window fixture — itself, or through a test module it imports
# fixtures from (`from test_light_mini import make_window`).
_MAKES_WINDOWS = re.compile(r"\btkinter\b|\bui\b|_window\b|[A-Z][A-Za-z]*(?:Window|View)\b|make_window"
                            r"|\bTk\(|Toplevel|winfo_|update_idletasks|mainloop")
_IMPORTS_TEST = re.compile(r"^from (test_\w+) import", re.MULTILINE)
_window_modules: dict[str, bool] = {}


def _makes_windows(path, seen: frozenset = frozenset()) -> bool:
    path = os.path.abspath(str(path))
    if path in _window_modules:
        return _window_modules[path]
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        text = ""
    found = bool(_MAKES_WINDOWS.search(text))
    if not found:
        folder = os.path.dirname(path)
        for name in _IMPORTS_TEST.findall(text):
            other = os.path.join(folder, name + ".py")
            if other not in seen and _makes_windows(other, seen | {path}):
                found = True
                break
    _window_modules[path] = found
    return found


@pytest.hookimpl(tryfirst=True)   # (before pytest's own -m filter looks at the marks)
def pytest_collection_modifyitems(config, items):
    ui = pytest.mark.ui
    for item in items:
        if _makes_windows(item.path):
            item.add_marker(ui)
    if config.getoption("--perf"):
        return
    skip = pytest.mark.skip(reason="performance benchmark: run with --perf")
    for item in items:
        if "perf" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _private_cache_folder(monkeypatch, tmp_path_factory):
    folder = tmp_path_factory.getbasetemp() / "cache" / str(next(_numbers))
    monkeypatch.setattr(settings, "_cache_base", lambda: folder)


@pytest.fixture(autouse=True)
def _isolated_app_platform(monkeypatch):
    from ironbark import crash, instance, logs

    key = f"IronbarkTest-{uuid.uuid4().hex[:12]}"   # a mutex / pipe of this test's own
    monkeypatch.setattr(instance, "default_key", lambda: key)
    monkeypatch.setattr(crash, "native_box", lambda title, text: crash.CLOSE)
    monkeypatch.setattr(crash, "tell_native", lambda *args, **kwargs: None)
    crash.reset()
    yield
    crash.reset()
    logs.shutdown()
    logging.getLogger(logs.PACKAGE_LOGGER).setLevel(logging.NOTSET)
    if not gc.isenabled():   # (a window left open by a test keeps it off)
        gc.enable()


class _NoKeyboardHook:
    """Stands in for mediakeys.KeyboardHook: a real one would see every keystroke on the computer."""

    failed = False
    thread_id = 0

    def __init__(self, on_press):
        self.on_press = on_press

    def close(self):
        pass


def _dpi_awareness() -> int | None:
    """This process's DPI awareness on Windows (0 = unaware, 1 = system aware); None elsewhere."""
    if sys.platform != "win32":
        return None
    import ctypes

    value = ctypes.c_int()
    try:
        ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(value))
    except (AttributeError, OSError):
        return None
    return value.value


@pytest.fixture(autouse=True)
def _nothing_system_wide(monkeypatch):
    """No test registers a window with Windows' media panel or the taskbar, or hooks the keyboard.
    Tests that want those pieces put in stand-ins of their own (or are live checks).

    Nor does a test make the test process DPI-aware, as the player's start does: Windows never lets
    that be undone, and every Tk window made after it gets its fonts at the screen's real scaling
    while Tk's own scaling (and so theme.px) stays at 100%, so text is too wide for its layout in
    whichever tests happen to run later. A test that changes it anyway fails here, by name."""
    from ironbark import __main__ as start

    monkeypatch.setattr(start, "_enable_crisp_text_on_windows", lambda: None)
    before = _dpi_awareness()
    if not LIVE_WINDOWS:
        from ironbark import mediakeys, mpris

        monkeypatch.setattr(mediakeys, "start_windows", lambda hub, hwnd: None)
        monkeypatch.setattr(mediakeys, "KeyboardHook", _NoKeyboardHook)
        monkeypatch.setattr(mpris, "start", lambda hub, cover_folder=None: None)   # (never the real session bus)
    yield
    if _dpi_awareness() != before:
        pytest.fail("this test made the test process DPI-aware: later tests' text would be laid out "
                    "too wide (stand in for __main__._enable_crisp_text_on_windows)", pytrace=False)


def minimise(window) -> None:
    """Minimise `window` the way its window manager does, and wait (3 s at most) until it has: an X11
    window manager applies it a moment later, and Cinnamon's keeps the window mapped (winstate.py)."""
    import time

    from ironbark import winstate

    window.iconify()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and winstate.on_screen(window):
        window.update()
        time.sleep(0.01)


def restore(window) -> None:
    """Bring `window` back from `minimise`, as the player does (winstate.restore: Tk's deiconify
    doesn't on Cinnamon), and wait (3 s at most) until it's on screen again."""
    import time

    from ironbark import winstate

    winstate.restore(window)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not winstate.on_screen(window):
        window.update()
        time.sleep(0.01)


@pytest.fixture(autouse=True)
def _never_hold_a_real_device(monkeypatch):
    """Bit-perfect on a Mac hogs the device and changes its format. Tests that switch it on
    with stand-in streams must never do that to the real sound card they run beside. (Linux's
    claim only reads the device's name, never the card: its rule is kept, as it decides what plays.)"""
    from ironbark import devices

    real = devices.claim

    def claim(index, rate):
        # (a stand-in device the test calls bit-perfect-capable is a sound card's own on Linux too:
        # Linux's rule is kept for the tests that set out the device list themselves)
        if devices.IS_LINUX and getattr(devices, "_tests_name_devices", False):
            return real(index, rate)
        return devices.Claim()

    monkeypatch.setattr(devices, "claim", claim)
    monkeypatch.setattr(devices, "restore_formats", lambda keep=None: None)


binned: list[str] = []   # what tests "sent to the Recycle Bin" (deleted instead, and noted here)


@pytest.fixture(autouse=True)
def _no_real_recycle_bin(monkeypatch):
    """Nothing a test deletes goes to the computer's real Recycle Bin: it's deleted, and noted in
    `binned`."""
    from ironbark import recycle

    def delete(full):
        os.remove(full)
        binned.append(full)

    monkeypatch.setattr(recycle, "_shell_delete", delete)
    monkeypatch.setattr(recycle, "_to_mac_bin", delete)
    monkeypatch.setattr(recycle, "_to_trash", delete)


@pytest.fixture
def tk_thread_rule():
    """Notes every call into tkinter (its Python code, or _tkinter's) made by a thread started
    during the test that isn't the main thread; the test fails if any was made."""
    import tkinter

    folder = os.path.normcase(os.path.dirname(tkinter.__file__))
    caught: list[tuple[str, str]] = []

    def watch(frame, event, arg):
        if threading.current_thread() is threading.main_thread():
            return
        if event == "call" and os.path.normcase(frame.f_code.co_filename).startswith(folder):
            caught.append((threading.current_thread().name, frame.f_code.co_name))
        elif event == "c_call" and (getattr(arg, "__module__", None) == "_tkinter"
                                    or type(getattr(arg, "__self__", None)).__module__ == "_tkinter"):
            caught.append((threading.current_thread().name, getattr(arg, "__name__", repr(arg))))

    threading.setprofile(watch)
    try:
        yield caught
    finally:
        threading.setprofile(None)
    assert caught == [], f"tkinter called off Tk's thread: {caught[:5]}"
