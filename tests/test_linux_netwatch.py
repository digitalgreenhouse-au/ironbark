"""netwatch on Linux: the kernel's link/address announcements are heard (a real netlink socket),
a burst of them makes one call, and closing ends the thread. Linux only."""

import socket
import sys
import time

import pytest

from ironbark.netcache import netwatch

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux's netlink")


def _wait(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not condition():
        time.sleep(0.02)
    return condition()


def test_the_watch_starts_on_the_kernels_socket_and_closes_cleanly():
    watch = netwatch.start(lambda: None)
    assert isinstance(watch, netwatch.LinuxWatch)
    assert watch._thread.is_alive()
    watch.close()
    assert not watch._thread.is_alive()
    watch.close()   # (idempotent)


def test_a_burst_of_announcements_makes_one_call(monkeypatch):
    monkeypatch.setattr(netwatch, "SETTLE_S", 0.1)
    ours, kernel = socket.socketpair()   # (stands in for the kernel's netlink socket)
    monkeypatch.setattr(netwatch.LinuxWatch, "_open", staticmethod(lambda: ours))
    calls = []
    watch = netwatch.LinuxWatch(lambda: calls.append(1))
    try:
        for _ in range(3):
            kernel.send(b"x")
        assert _wait(lambda: calls)
        time.sleep(0.3)
        assert calls == [1]
        kernel.send(b"x")   # a later change: one more
        assert _wait(lambda: len(calls) == 2)
    finally:
        watch.close()
        kernel.close()
    assert not watch._thread.is_alive()


def test_a_handler_that_fails_doesnt_stop_the_watch(monkeypatch):
    monkeypatch.setattr(netwatch, "SETTLE_S", 0.05)
    ours, kernel = socket.socketpair()
    monkeypatch.setattr(netwatch.LinuxWatch, "_open", staticmethod(lambda: ours))
    calls = []

    def handler():
        calls.append(1)
        raise RuntimeError("the cache's handler failed")

    watch = netwatch.LinuxWatch(handler)
    try:
        kernel.send(b"x")
        assert _wait(lambda: calls)
        time.sleep(0.2)
        kernel.send(b"x")
        assert _wait(lambda: len(calls) == 2)
    finally:
        watch.close()
        kernel.close()
