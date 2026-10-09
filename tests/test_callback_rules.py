"""The audio thread keeps to the rules in _Session._callback's docstring: nothing that waits, no
file, console or network access, no logging, no new thread. Checked by driving the callback through
a stand-in sound card (as test_gapless does) with those calls forbidden on the calling thread for
exactly as long as the callback runs; the decoder thread, which may do all of them, is untouched.
The other rule, that nothing here calls into the window toolkit, is a matter of imports and is
checked in test_architecture.py."""
from __future__ import annotations

import builtins
import logging
import os
import queue
import socket
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
import sounddevice as sd
import soundfile as sf

from ironbark import engine
from ironbark.engine import BLOCK_FRAMES, _Session

RNG = np.random.default_rng(11)
TIME_INFO = SimpleNamespace(outputBufferDacTime=0.0, currentTime=0.0)
OK = SimpleNamespace(output_underflow=False)
UNDERFLOW = SimpleNamespace(output_underflow=True)


class FakeStream:
    def __init__(self, **kwargs):
        self.callback = kwargs["callback"]
        self.latency, self.device = 0.1, 0

    def start(self):
        pass

    stop = abort = close = start


@pytest.fixture
def fake_card(monkeypatch):
    monkeypatch.setattr(engine.sd, "OutputStream", FakeStream)
    monkeypatch.setattr(engine.devices, "find_device", lambda name: None)
    monkeypatch.setattr(engine.devices, "mixer_rate", lambda index: None)


class Forbidden:
    """While on, each guarded call raises on the thread that made this (and on no other)."""

    def __init__(self, monkeypatch):
        self.on = False
        self.thread = threading.current_thread()
        self.broken: list[str] = []
        self._monkeypatch = monkeypatch

    def guard(self, owner, name: str, what: str, unless=None):
        original = getattr(owner, name)

        def guarded(*args, **kwargs):
            if (self.on and threading.current_thread() is self.thread
                    and not (unless is not None and unless(*args, **kwargs))):
                self.broken.append(what)
                raise AssertionError(f"the audio thread must not {what}")
            return original(*args, **kwargs)

        self._monkeypatch.setattr(owner, name, guarded)

    def __enter__(self):
        self.on = True
        return self

    def __exit__(self, *exc):
        self.on = False


def wav(tmp_path, name, frames, rate=44100):
    data = (RNG.standard_normal((frames, 2)) * 4000).astype(np.int16)
    path = tmp_path / name
    sf.write(path, data, rate, subtype="PCM_16")
    return path


def pull_under_the_rules(session, forbid, limit=200) -> int:
    """Ask for blocks the way the sound card does, each request under the rules, until the
    session stops; how many requests carried music."""
    filled = 0
    for n in range(limit):
        deadline = time.monotonic() + 2
        while ((session._seek_request is not None or (session._queue.qsize() < 2 and not session._draining))
               and time.monotonic() < deadline):
            time.sleep(0.002)  # let the decoder keep ahead, as it does in real playback
        block = np.zeros((BLOCK_FRAMES, 2), np.float32)
        with forbid:
            try:
                session._callback(block, BLOCK_FRAMES, TIME_INFO, UNDERFLOW if n == 3 else OK)
            except sd.CallbackStop:
                return filled
        filled += bool(block.any())
    raise AssertionError("never reached the end")


def test_the_callback_keeps_to_the_rules(tmp_path, fake_card, monkeypatch):
    a = wav(tmp_path, "a.wav", BLOCK_FRAMES * 5)
    b = wav(tmp_path, "b.wav", BLOCK_FRAMES * 5)
    forbid = Forbidden(monkeypatch)
    forbid.guard(builtins, "open", "open a file")
    forbid.guard(builtins, "print", "print")
    forbid.guard(os, "stat", "read the file system")
    forbid.guard(os, "listdir", "read the file system")
    forbid.guard(time, "sleep", "sleep")
    forbid.guard(socket, "socket", "use the network")
    forbid.guard(logging.Logger, "isEnabledFor", "log")
    forbid.guard(logging.Logger, "_log", "log")
    forbid.guard(threading.Event, "wait", "wait on an event")
    forbid.guard(threading.Thread, "start", "start a thread")
    forbid.guard(queue.Queue, "get", "wait on a queue", unless=lambda self, block=True, timeout=None: not block)
    forbid.guard(queue.Queue, "put", "wait on a queue",
                 unless=lambda self, item, block=True, timeout=None: not block)

    advances = []
    slow_viewer: queue.Queue = queue.Queue(maxsize=1)   # a meter that never reads: drop, never wait
    session = _Session(a, lambda path: 1.0, lambda s: None, index=0, follow={0: (1, b)}.get,
                       on_advance=lambda s, previous, new: advances.append((previous.path, new.path)),
                       monitors=[slow_viewer])
    session.start()
    try:
        filled = pull_under_the_rules(session, forbid)
    finally:
        session.close()
    assert filled >= 10 and advances == [(a, b)]   # both tracks went out, the join included
    assert slow_viewer.full() and forbid.broken == []


def test_the_rules_bite(monkeypatch):
    """The guard itself: a forbidden call on this thread fails, a blocking-free one and one on
    another thread pass (so a pass above means the callback kept the rules, not that nobody looked)."""
    forbid = Forbidden(monkeypatch)
    forbid.guard(time, "sleep", "sleep")
    forbid.guard(queue.Queue, "get", "wait on a queue", unless=lambda self, block=True, timeout=None: not block)
    q: queue.Queue = queue.Queue()
    q.put(1)
    with forbid:
        assert q.get_nowait() == 1
        with pytest.raises(AssertionError, match="must not sleep"):
            time.sleep(0)
        slept = []
        other = threading.Thread(target=lambda: slept.append(time.sleep(0)))
        other.start()
        other.join()
    assert slept == [None] and forbid.broken == ["sleep"]
    time.sleep(0)   # off again
