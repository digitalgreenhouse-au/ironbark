"""Shared test set-up: every test runs as if it were the only copy of the player, and nothing a
test does can show a Windows message box, reach a player that's really running, or leave the log
or the cycle collector set up for the next test."""

from __future__ import annotations

import gc
import logging
import uuid

import pytest


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
