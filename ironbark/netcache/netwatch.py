"""Hearing when this computer's network changes (Wi-Fi back, a cable plugged in, a VPN up), so a
server that stopped answering is asked again straight away instead of at its next back-off.

Windows: NotifyIpInterfaceChange (iphlpapi, no COM). Its callback runs on a thread of Windows'
own and only calls `on_change()`, which must be quick and touch no Tk (the cache just sets an
event). Linux: the kernel's own announcements of links and addresses coming and going (a netlink
socket, from the standard library), read on a thread of our own. Elsewhere there's nothing to
start: the back-off still finds the server again.
"""

from __future__ import annotations

import atexit
import logging
import os
import select
import socket
import threading

from .. import platforms

log = logging.getLogger(__name__)

AF_UNSPEC = 0
# Linux netlink: the link and IPv4/IPv6 address groups (linux/rtnetlink.h).
RTMGRP_LINK, RTMGRP_IPV4_IFADDR, RTMGRP_IPV6_IFADDR = 0x1, 0x10, 0x100
NETLINK_ROUTE = 0
SETTLE_S = 0.5   # a change comes as a burst of messages: one call once they've settled


class Watch:
    """One registration with Windows; `close()` ends it (idempotent; also run at exit)."""

    def __init__(self, on_change):
        import ctypes
        from ctypes import wintypes

        self._on_change = on_change
        callback_type = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int)
        lib = ctypes.WinDLL("iphlpapi")   # (a private handle: its argtypes are ours alone)
        notify = lib.NotifyIpInterfaceChange
        notify.argtypes = [ctypes.c_ushort, callback_type, ctypes.c_void_p, ctypes.c_ubyte,
                           ctypes.POINTER(wintypes.HANDLE)]
        notify.restype = ctypes.c_ulong
        self._cancel = lib.CancelMibChangeNotify2
        self._cancel.argtypes = [wintypes.HANDLE]
        self._cancel.restype = ctypes.c_ulong
        self._callback = callback_type(self._fired)   # (kept: Windows calls it until close)
        handle = wintypes.HANDLE()
        error = notify(AF_UNSPEC, self._callback, None, 0, ctypes.byref(handle))
        if error:
            raise OSError(error, f"NotifyIpInterfaceChange failed ({error})")
        self._handle = handle
        atexit.register(self.close)

    def _fired(self, context, row, kind):
        try:
            self._on_change()
        except Exception:   # noqa: BLE001 — never raise into Windows' thread
            pass

    def close(self) -> None:
        handle, self._handle = getattr(self, "_handle", None), None
        if handle is not None and handle.value:
            try:
                self._cancel(handle)
            except OSError:
                pass
        atexit.unregister(self.close)


class LinuxWatch:
    """Linux: the kernel's link and address changes, heard on a netlink socket by a thread of our
    own; a burst of them makes one `on_change()`. `close()` ends it (idempotent; also at exit)."""

    def __init__(self, on_change):
        self._on_change = on_change
        self._socket = self._open()
        self._wake_r, self._wake_w = os.pipe()
        self._thread = threading.Thread(target=self._run, name="netwatch", daemon=True)
        self._thread.start()
        atexit.register(self.close)

    @staticmethod
    def _open() -> socket.socket:
        """The kernel's netlink socket, joined to the link and address groups."""
        family = socket.AF_NETLINK   # type: ignore[attr-defined]  # (Linux only; mypy here runs on Windows)
        sock = socket.socket(family, socket.SOCK_RAW, NETLINK_ROUTE)
        try:
            sock.bind((0, RTMGRP_LINK | RTMGRP_IPV4_IFADDR | RTMGRP_IPV6_IFADDR))
        except OSError:
            sock.close()
            raise
        return sock

    def _run(self):
        sock, wake = self._socket, self._wake_r
        pending = False
        while True:
            try:
                ready, _, _ = select.select([sock, wake], [], [], SETTLE_S if pending else None)
            except (OSError, ValueError):
                return
            if wake in ready:
                return
            if sock in ready:
                try:
                    sock.recv(65536)
                except OSError:
                    return
                pending = True
                continue
            if pending:   # (quiet for SETTLE_S: the burst is over)
                pending = False
                try:
                    self._on_change()
                except Exception:   # noqa: BLE001 — never let the watcher's thread die of it
                    log.debug("network change handler failed", exc_info=True)

    def close(self) -> None:
        wake_w, self._wake_w = getattr(self, "_wake_w", None), None
        if wake_w is not None:
            try:
                os.write(wake_w, b"x")
            except OSError:
                pass
            self._thread.join(2)
            for fd in (self._wake_r, wake_w):
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._socket.close()
        atexit.unregister(self.close)


def start(on_change) -> Watch | LinuxWatch | None:
    """Call `on_change()` (from another thread) whenever a network interface changes; None where
    that can't be heard."""
    if platforms.IS_LINUX:
        try:
            return LinuxWatch(on_change)
        except (OSError, AttributeError):
            log.warning("network changes can't be heard here: servers are asked again on their back-off",
                        exc_info=True)
            return None
    if not platforms.IS_WINDOWS:
        return None
    try:
        return Watch(on_change)
    except (OSError, AttributeError):
        log.warning("network changes can't be heard here: servers are asked again on their back-off",
                    exc_info=True)
        return None
