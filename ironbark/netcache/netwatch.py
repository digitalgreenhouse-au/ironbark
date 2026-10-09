"""Hearing when this computer's network changes (Wi-Fi back, a cable plugged in, a VPN up), so a
server that stopped answering is asked again straight away instead of at its next back-off.

Windows: NotifyIpInterfaceChange (iphlpapi, no COM). Its callback runs on a thread of Windows'
own and only calls `on_change()`, which must be quick and touch no Tk (the cache just sets an
event). Elsewhere there's nothing to start: the back-off still finds the server again.
"""

from __future__ import annotations

import atexit
import logging

from .. import platforms

log = logging.getLogger(__name__)

AF_UNSPEC = 0


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


def start(on_change) -> Watch | None:
    """Call `on_change()` (from another thread) whenever a network interface changes; None where
    that can't be heard."""
    if not platforms.IS_WINDOWS:
        return None
    try:
        return Watch(on_change)
    except (OSError, AttributeError):
        log.warning("network changes can't be heard here: servers are asked again on their back-off",
                    exc_info=True)
        return None
