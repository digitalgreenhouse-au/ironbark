"""One player at a time. No UI code.

A second launch — a double-click on a music file with Ironbark under "Open with", the Start menu
while the player is already open, `ironbark some\\folder` — doesn't start a second player (two
would both answer the media keys, fight over the sound card in bit-perfect mode and write over
each other's settings). It hands what it was asked to open to the running copy, asks it to come
to the front, and exits.

Windows: a named mutex says a copy is running (one per user, Windows session and settings folder),
and that copy listens on a named pipe that only programs of the same user on the same computer
can write to. Elsewhere (best effort until the Mac build): a lock file beside the settings, and a
socket on 127.0.0.1 whose port, with a random key a request must carry, is written beside it.

Listening runs on a thread of its own that only ever puts `Request`s on a queue: the window takes
them from there on its own thread (Tk must never be called from any other).

A request on the wire: a 4-byte little-endian length, then that many bytes of UTF-8 JSON
{"paths": [...], "front": true, "key": "..."}; the running copy answers one byte, 1 = taken.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import secrets
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import APP_NAME

log = logging.getLogger(__name__)

FORWARD_TIMEOUT = 8.0     # s a second launch keeps trying to reach the running copy (it may still be starting)
MAX_REQUEST = 1 << 20     # bytes: the longest request taken (thousands of paths)
ACK = b"\x01"
# The installer looks for this before uninstalling ("close Ironbark first"). Not per user: the
# installer only needs to know a copy is open in this Windows session.
INSTALLER_MUTEX = "DigitalGreenhouse.Ironbark.Running"


@dataclass(frozen=True)
class Request:
    """What a later launch asked for: files / folders / playlists to open, and to come forward."""
    paths: tuple[str, ...] = ()
    front: bool = True


def encode(paths, front: bool = True, key: str = "") -> bytes:
    """One request, framed for the wire."""
    body = json.dumps({"paths": [str(p) for p in paths], "front": bool(front), "key": key}).encode("utf-8")
    return struct.pack("<I", len(body)) + body


def decode(body: bytes, key: str = "") -> Request | None:
    """A request's JSON (without its length), or None for anything that isn't one (or whose key
    is wrong)."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("key", "") != key:
        return None
    paths = data.get("paths", [])
    if not isinstance(paths, list):
        return None
    return Request(tuple(p for p in paths if isinstance(p, str) and p), bool(data.get("front", True)))


def absolute(paths) -> list[str]:
    """Paths as the launch meant them: relative ones are relative to where it was started from,
    which the running copy can't know."""
    return [os.path.abspath(p) for p in paths if p]


def default_key() -> str:
    """Names this user's copy: their settings folder (one player per settings folder) and, on
    Windows, the session they're signed in to."""
    from . import settings

    folder = os.path.normcase(str(settings.settings_path().parent))
    digest = hashlib.sha256(folder.encode("utf-8")).hexdigest()[:16]
    return f"{APP_NAME}-{_session()}-{digest}"


class Instance:
    """This launch's place. `first`: it's the running copy, and later launches' requests arrive
    on `requests`. Otherwise `forward` hands this launch's own over. `close` lets go (at exit)."""

    def __init__(self, first: bool, requests: queue.Queue | None = None,
                 send: Callable[[list[str], bool, float], bool] | None = None,
                 closers: tuple[Callable[[], None], ...] = ()):
        self.first = first
        self.requests: queue.Queue = requests if requests is not None else queue.Queue()
        self._send = send
        self._closers = list(closers)

    @classmethod
    def alone(cls) -> "Instance":
        """Running without the check (it couldn't be made): the only copy, as far as it knows."""
        return cls(True)

    def forward(self, paths, front: bool = True, timeout: float = FORWARD_TIMEOUT) -> bool:
        """Hand `paths` to the running copy (and ask it to come forward). True once it has them."""
        if self.first or self._send is None:
            return False
        try:
            return self._send(list(paths), front, timeout)
        except OSError:
            log.warning("couldn't reach the running copy", exc_info=True)
            return False

    def close(self) -> None:
        closers, self._closers = self._closers, []
        for close in reversed(closers):
            try:
                close()
            except Exception:  # noqa: BLE001 — closing is best effort
                log.debug("letting go of the instance failed", exc_info=True)


def claim(key: str | None = None, folder: Path | None = None) -> Instance:
    """Is this the only copy? A failure to tell (no mutex, no pipe) counts as yes: better a
    second player than none."""
    key = key or default_key()
    try:
        if sys.platform == "win32":
            return _claim_windows(key)
        if folder is None:
            from . import settings

            folder = settings.settings_path().parent
        return claim_with_socket(key, folder)
    except OSError:
        log.warning("couldn't check for another running copy", exc_info=True)
        return Instance.alone()


# -- shared: reading a framed request --------------------------------------------------------------
def _read_frame(read: Callable[[int], bytes]) -> bytes | None:
    """A request's JSON, read with `read(n)` (which returns b"" at the end). None if it's cut short
    or too long."""
    head = _read_exactly(read, 4)
    if head is None:
        return None
    (size,) = struct.unpack("<I", head)
    if size > MAX_REQUEST:
        return None
    return _read_exactly(read, size)


def _read_exactly(read: Callable[[int], bytes], size: int) -> bytes | None:
    data = bytearray()
    while len(data) < size:
        piece = read(size - len(data))
        if not piece:
            return None
        data += piece
    return bytes(data)


# -- Windows: named mutex + named pipe ---------------------------------------------------------------
ERROR_ALREADY_EXISTS = 183
ERROR_PIPE_CONNECTED = 535
ERROR_PIPE_BUSY = 231
ERROR_BROKEN_PIPE = 109
ERROR_MORE_DATA = 234
PIPE_ACCESS_DUPLEX = 0x3
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x80000
PIPE_REJECT_REMOTE_CLIENTS = 0x8   # (with PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT, all 0)
GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
OPEN_EXISTING = 3
PIPE_BUFFER = 65536

_kernel = None


def _k32():
    """kernel32 with its types set, on a handle of our own (the shared ctypes.windll one is used
    by other modules with other types)."""
    global _kernel
    if _kernel is None:
        import ctypes
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        k.CreateMutexW.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        k.CreateNamedPipeW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                       wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID)
        k.CreateNamedPipeW.restype = wintypes.HANDLE
        k.ConnectNamedPipe.argtypes = (wintypes.HANDLE, wintypes.LPVOID)
        k.DisconnectNamedPipe.argtypes = (wintypes.HANDLE,)
        k.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        k.CreateFileW.restype = wintypes.HANDLE
        k.ReadFile.argtypes = (wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                               wintypes.LPVOID)
        k.WriteFile.argtypes = (wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                wintypes.LPVOID)
        k.FlushFileBuffers.argtypes = (wintypes.HANDLE,)
        k.WaitNamedPipeW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
        k.GetNamedPipeServerProcessId.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG))
        k.ProcessIdToSessionId.argtypes = (wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
        _kernel = k
    return _kernel


def _invalid(handle) -> bool:
    return handle is None or handle in (0, 0xFFFFFFFFFFFFFFFF, 0xFFFFFFFF, -1)


def _session() -> int:
    """The Windows session this program runs in (0 elsewhere)."""
    if sys.platform != "win32":
        return 0
    import ctypes
    from ctypes import wintypes

    session = wintypes.DWORD(0)
    try:
        _k32().ProcessIdToSessionId(os.getpid(), ctypes.byref(session))
    except OSError:
        return 0
    return session.value


def mutex_name(key: str) -> str:
    return f"Local\\{key}"


def pipe_name(key: str) -> str:
    return f"\\\\.\\pipe\\{key}"


def create_mutex(name: str) -> tuple[int, bool]:
    """(handle, already there): the mutex is made if no program has it, else opened."""
    import ctypes

    handle = _k32().CreateMutexW(None, False, name)
    error = ctypes.get_last_error()
    if _invalid(handle):
        raise ctypes.WinError(error)
    return handle, error == ERROR_ALREADY_EXISTS


def _close_handle(handle) -> None:
    _k32().CloseHandle(handle)


def _claim_windows(key: str) -> Instance:
    handle, taken = create_mutex(mutex_name(key))
    if taken:
        _close_handle(handle)
        return Instance(False, send=lambda paths, front, timeout: send_to_pipe(pipe_name(key), encode(paths, front),
                                                                               timeout))
    requests: queue.Queue = queue.Queue()
    closers = [lambda: _close_handle(handle)]
    try:
        server = PipeServer(pipe_name(key), requests)
    except OSError:
        log.warning("couldn't listen for later launches", exc_info=True)   # still the only copy
    else:
        closers.append(server.close)
    try:
        installer, _ = create_mutex(INSTALLER_MUTEX)
    except OSError:
        pass
    else:
        closers.append(lambda: _close_handle(installer))
    return Instance(True, requests, closers=tuple(closers))


class PipeServer:
    """The running copy's end of the pipe: each request on its own short connection, read on a
    thread of its own and put on `requests`."""

    def __init__(self, name: str, requests: queue.Queue):
        import ctypes

        self.name, self.requests = name, requests
        self._stop = threading.Event()
        self.handle = _k32().CreateNamedPipeW(name, PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE,
                                              PIPE_REJECT_REMOTE_CLIENTS, 1, PIPE_BUFFER, PIPE_BUFFER, 0, None)
        if _invalid(self.handle):
            raise ctypes.WinError(ctypes.get_last_error())
        self.thread = threading.Thread(target=self._serve, name="instance-pipe", daemon=True)
        self.thread.start()

    def _serve(self):
        import ctypes

        k = _k32()
        try:
            while not self._stop.is_set():
                if not k.ConnectNamedPipe(self.handle, None):
                    error = ctypes.get_last_error()
                    if error != ERROR_PIPE_CONNECTED:   # (connected before we asked: that's fine)
                        if self._stop.is_set():
                            break
                        k.DisconnectNamedPipe(self.handle)   # (a caller that hung up at once)
                        time.sleep(0.05)   # never a busy loop, whatever the pipe's trouble
                        continue
                if self._stop.is_set():
                    break
                try:
                    body = _read_frame(self._read)
                    request = decode(body) if body is not None else None
                    if request is not None:
                        self.requests.put(request)
                        self._write(ACK)
                        k.FlushFileBuffers(self.handle)   # (the answer is read before we hang up)
                except Exception:  # noqa: BLE001 — one bad caller mustn't stop the listening
                    log.warning("a request from another launch couldn't be read", exc_info=True)
                finally:
                    k.DisconnectNamedPipe(self.handle)
        finally:
            _close_handle(self.handle)

    def _read(self, size: int) -> bytes:
        import ctypes
        from ctypes import wintypes

        buffer = ctypes.create_string_buffer(min(size, PIPE_BUFFER))
        got = wintypes.DWORD(0)
        if not _k32().ReadFile(self.handle, buffer, len(buffer), ctypes.byref(got), None):
            if ctypes.get_last_error() != ERROR_MORE_DATA:
                return b""
        return buffer.raw[:got.value]

    def _write(self, data: bytes) -> None:
        import ctypes
        from ctypes import wintypes

        done = wintypes.DWORD(0)
        _k32().WriteFile(self.handle, data, len(data), ctypes.byref(done), None)

    def close(self) -> None:
        """Stop listening: the thread waits for a caller, so it's woken by connecting to it (it
        then sees the stop). Tried a few times: it may be between two callers just then."""
        self._stop.set()
        for _ in range(20):
            if not self.thread.is_alive():
                return
            handle = _k32().CreateFileW(self.name, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
            if not _invalid(handle):
                _close_handle(handle)
            self.thread.join(0.1)


def send_to_pipe(name: str, frame: bytes, timeout: float = FORWARD_TIMEOUT) -> bool:
    """A later launch's side: connect (waiting while the running copy is still starting up, or busy
    with another launch), let it take the foreground, send the request, wait for its answer."""
    import ctypes
    from ctypes import wintypes

    k = _k32()
    deadline = time.monotonic() + timeout
    while True:
        handle = k.CreateFileW(name, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
        if not _invalid(handle):
            break
        error = ctypes.get_last_error()
        if time.monotonic() >= deadline:
            log.warning("the running copy didn't answer (Windows error %d)", error)
            return False
        if error == ERROR_PIPE_BUSY:
            k.WaitNamedPipeW(name, 200)
        else:
            time.sleep(0.05)   # not listening yet
    try:
        server = wintypes.ULONG(0)
        if k.GetNamedPipeServerProcessId(handle, ctypes.byref(server)):
            # Windows only lets the program the user just started bring a window forward: pass that on.
            ctypes.WinDLL("user32").AllowSetForegroundWindow(wintypes.DWORD(server.value))
        sent = 0
        while sent < len(frame):
            done = wintypes.DWORD(0)
            if not k.WriteFile(handle, frame[sent:], len(frame) - sent, ctypes.byref(done), None):
                return False
            sent += done.value
        answer = ctypes.create_string_buffer(1)
        got = wintypes.DWORD(0)
        return bool(k.ReadFile(handle, answer, 1, ctypes.byref(got), None)) and answer.raw[:got.value] == ACK
    finally:
        _close_handle(handle)


# -- elsewhere: lock file + local socket ------------------------------------------------------------
def _lock(file) -> bool:
    """Take the lock file for this program (False: another program holds it). Released when the
    file is closed, or when the program ends however it ends."""
    try:
        if sys.platform == "win32":
            import msvcrt

            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(file) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        file.close()


def claim_with_socket(key: str, folder: Path) -> Instance:
    """The lock file + socket way (Mac and Linux; tests use it on Windows too)."""
    folder.mkdir(parents=True, exist_ok=True)
    lock_path, info_path = folder / f"{key}.lock", folder / f"{key}.json"
    lock = open(lock_path, "a+b")
    if not _lock(lock):
        lock.close()
        return Instance(False, send=lambda paths, front, timeout: send_to_socket(info_path, paths, front, timeout))
    requests: queue.Queue = queue.Queue()
    try:
        server = SocketServer(info_path, requests)
    except OSError:
        log.warning("couldn't listen for later launches", exc_info=True)
        return Instance(True, requests, closers=(lambda: _unlock(lock),))
    return Instance(True, requests, closers=(lambda: _unlock(lock), server.close))


class SocketServer:
    """Listens on 127.0.0.1 (a port the system picks); the port and a random key go in `info_path`,
    which only this user can normally read."""

    def __init__(self, info_path: Path, requests: queue.Queue):
        self.info_path, self.requests = info_path, requests
        self.key = secrets.token_hex(16)
        self._stop = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.sock.bind(("127.0.0.1", 0))
            self.sock.listen(8)
            self.sock.settimeout(0.5)   # (so the thread notices `close`)
            from .settings import write_file

            write_file(info_path, json.dumps({"port": self.sock.getsockname()[1], "key": self.key,
                                              "pid": os.getpid()}))
        except (OSError, ValueError):
            self.sock.close()
            raise OSError("couldn't set up the local socket")
        self.thread = threading.Thread(target=self._serve, name="instance-socket", daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = self.sock.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                with conn:
                    try:
                        conn.settimeout(2.0)
                        body = _read_frame(conn.recv)
                        request = decode(body, self.key) if body is not None else None
                        if request is not None:
                            self.requests.put(request)
                            conn.sendall(ACK)
                    except OSError:
                        log.warning("a request from another launch couldn't be read", exc_info=True)
        finally:
            self.sock.close()

    def close(self) -> None:
        self._stop.set()
        self.thread.join(2)
        try:
            self.info_path.unlink()
        except OSError:
            pass


def send_to_socket(info_path: Path, paths, front: bool = True, timeout: float = FORWARD_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
            port, key = int(info["port"]), str(info["key"])
            with socket.create_connection(("127.0.0.1", port), timeout=2.0) as conn:
                conn.sendall(encode(paths, front, key))
                return conn.recv(1) == ACK
        except (OSError, ValueError, KeyError, TypeError):
            if time.monotonic() >= deadline:
                log.warning("the running copy didn't answer", exc_info=True)
                return False
            time.sleep(0.05)   # not listening yet
