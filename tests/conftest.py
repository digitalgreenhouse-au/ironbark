"""Shared test set-up.

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
import threading

import pytest

from ironbark import settings

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
