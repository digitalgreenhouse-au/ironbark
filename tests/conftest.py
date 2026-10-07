"""Shared test set-up.

- On Windows the test windows are made on a hidden desktop of their own, so a test run never
  puts windows in front of whatever the person at the computer is doing. They are still real
  windows — mapped, laid out and measured exactly as on the visible desktop. Set
  IRONBARK_TESTS_VISIBLE=1 to watch them on the normal desktop instead.
- Every test gets its own empty cache folder (settings.cache_folder: where the library database
  lives), so no test can touch the real one. (A test that points the settings somewhere else
  takes the cache with them anyway: settings.cache_folder follows settings_path.)
- Performance benchmarks are marked `perf` and only run with `--perf`
  (`uv run pytest -q -p no:cacheprovider --perf -m perf`): they build big synthetic libraries
  and take a while.
- `tk_thread_rule`: a test using it fails if any thread started during the test calls into
  tkinter (a Tk call off Tk's own thread can abort the program): the list of (thread, what) it
  caught, checked empty at the end.
"""
from __future__ import annotations

import itertools
import os
import sys
import threading

import pytest

from ironbark import settings   # makes no window, so it may come before the desktop switch

HIDDEN_DESKTOP = "IronbarkTests"


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


_hidden_desktop = None
if sys.platform == "win32" and os.environ.get("IRONBARK_TESTS_VISIBLE") != "1":
    _hidden_desktop = _use_hidden_desktop()

_numbers = itertools.count()


def pytest_addoption(parser):
    parser.addoption("--perf", action="store_true", default=False,
                     help="also run the performance benchmarks (marked 'perf')")


def pytest_configure(config):
    config.addinivalue_line("markers", "perf: a performance benchmark (slow); runs only with --perf")


def pytest_collection_modifyitems(config, items):
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
