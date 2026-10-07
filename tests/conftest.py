"""Shared test set-up.

- Every test gets its own empty cache folder (settings.cache_folder: where the library database
  lives), so no test can touch the real one. (A test that points the settings somewhere else
  takes the cache with them anyway: settings.cache_folder follows settings_path.)
- Performance benchmarks are marked `perf` and only run with `--perf`
  (`uv run pytest -q -p no:cacheprovider --perf -m perf`): they build big synthetic libraries
  and take a while.
"""
from __future__ import annotations

import itertools

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
