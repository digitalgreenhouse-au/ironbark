"""When something goes wrong: a plain box the user can act on, instead of silence or a raw traceback.

Every error is written to crash.log first (__main__ does that, and app.log gets it too). Then:
- in the window's code, or on another thread while the window is up: an in-app box (a
  popups.Prompt) — "Something went wrong" — with Copy details · Open log folder · Close. Other
  threads never touch Tk: `report` only queues the error, and the window's own thread takes it
  from there (`attach`).
- when there's no working window (Ironbark couldn't start): Windows' own message box with the
  same three buttons (`startup_failed`).

Never a stream of boxes: one at a time, each kind of error once a session, at most MAX_BOXES a
session and no more often than once every MIN_GAP_S; the rest are only logged.

After a crash: a clean exit is noted at the end of crash.log (`clean_exit_line`), so at the next start
an entry written after the last one — an error, or the dump of a crash — means Ironbark closed
unexpectedly (`closed_unexpectedly`). The window then says so once in its status line, with a link
to Save diagnostics… (`take_unexpected_exit`). A log from before clean exits were noted says nothing:
it starts noting them (TRACKING) and only what comes after counts.
"""

from __future__ import annotations

import datetime
import logging
import os
import platform
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import APP_NAME, __version__

log = logging.getLogger(__name__)

MIN_GAP_S = 60.0     # seconds between two boxes at the least
MAX_BOXES = 3        # boxes a session at the most
DRAIN_MS = 500       # how often the window's thread looks for errors from other threads
NATIVE_ROUNDS = 10   # the start-up box comes back after Copy details / Open log folder, this often at most

TITLE = "Something went wrong"
MESSAGE = ("Part of Ironbark hit a problem it didn't expect. The music carries on, but if something "
           "stops working, close the player and start it again.")
DETAIL = "The details are saved in the log. Copy them to paste into a problem report."
COPY, FOLDER, CLOSE = "copy", "folder", "close"
SHORT_FOLDER_LABEL = "Log folder"   # "Open log folder", where the box is too narrow for it

# crash.log's lines about how Ironbark closed: a clean exit, and where noting them began (a log from
# before they were noted, or a new one).
TRACKING = f"--- {APP_NAME} notes how it closes from here ---"
_CLEAN = re.compile(rf"^--- {re.escape(APP_NAME)} closed normally \d\d/\d\d/\d{{4}} .*---$")
_STARTED = re.compile(r"^--- .+ started \d\d/\d\d/\d{4} .*---$")
_unexpected = False   # the last run ended in a crash (found at start: note_last_run)


def clean_exit_line(now: datetime.datetime | None = None) -> str:
    """The line written at the end of crash.log as Ironbark closes normally."""
    return f"--- {APP_NAME} closed normally {(now or datetime.datetime.now()):%d/%m/%Y %H:%M:%S} ---"


def _marks_exit(line: str) -> bool:
    line = line.strip()
    return line == TRACKING or bool(_CLEAN.match(line))


def tracks_exits(text: str) -> bool:
    """Whether crash.log's text notes how Ironbark closes (a clean exit, or where noting them began)."""
    return any(_marks_exit(line) for line in text.splitlines())


def closed_unexpectedly(text: str) -> bool:
    """crash.log's text, read as Ironbark starts: did it close unexpectedly after its last clean exit?
    Yes when anything but a start line follows the last clean exit (an error, a crash's dump). A log
    that has never noted a clean exit can't tell: no."""
    lines = text.splitlines()
    last = max((i for i, line in enumerate(lines) if _marks_exit(line)), default=None)
    if last is None:
        return False
    return any(line.strip() and not _STARTED.match(line.strip()) for line in lines[last + 1:])


def note_last_run(text: str) -> None:
    """At start-up, before this run writes anything to crash.log: remember whether the last run crashed."""
    global _unexpected
    _unexpected = closed_unexpectedly(text)


def take_unexpected_exit() -> bool:
    """Did the last run crash? Answered once (the window says so once)."""
    global _unexpected
    found, _unexpected = _unexpected, False
    return found


@dataclass(frozen=True)
class Report:
    where: str        # "in the window code", "in the tab-load thread" …
    text: str         # the details: version, system, traceback
    signature: tuple  # the kind of error: its type and where it was raised


def describe(where: str, exc_type, value, tb) -> Report:
    """A report on an error (any thread)."""
    trace = "".join(traceback.format_exception(exc_type, value, tb))
    last = traceback.extract_tb(tb)[-1] if tb is not None else None
    signature = (getattr(exc_type, "__name__", str(exc_type)),
                 (Path(last.filename).name, last.lineno) if last is not None else None)
    text = f"{APP_NAME} {__version__} · {platform.platform()} · Python {platform.python_version()}\n" \
           f"Error {where}:\n{trace}"
    return Report(where, text, signature)


# -- how often a box may be shown ---------------------------------------------------------------------
_pending: queue.Queue = queue.Queue()   # errors from any thread, waiting for the window's thread
_lock = threading.Lock()
_seen: set = set()
_shown = 0
_last_shown = float("-inf")
_open = False


def reset() -> None:
    """Forget everything shown or waiting (a new session; tests)."""
    global _shown, _last_shown, _open
    with _lock:
        _seen.clear()
        _shown, _last_shown, _open = 0, float("-inf"), False
    while True:
        try:
            _pending.get_nowait()
        except queue.Empty:
            break


def may_show(report: Report, now: float | None = None) -> bool:
    """May a box be shown for this error now? If so, it counts as shown."""
    global _shown, _last_shown
    now = time.monotonic() if now is None else now
    with _lock:
        if _open or report.signature in _seen or _shown >= MAX_BOXES or now - _last_shown < MIN_GAP_S:
            return False
        _seen.add(report.signature)
        _shown += 1
        _last_shown = now
        return True


def report(where: str, exc_type, value, tb) -> None:
    """An error to tell the user about once the window's thread gets to it. Safe on any thread."""
    try:
        _pending.put(describe(where, exc_type, value, tb))
    except Exception:  # noqa: BLE001 — reporting must never raise in an error handler
        pass


def log_folder() -> Path:
    from . import settings

    return settings.settings_path().parent


# -- in the window -------------------------------------------------------------------------------
def attach(root, before: Callable[[], None] | None = None, folder: Callable[[], Path] = log_folder) -> None:
    """Show reported errors in `root` from now on (an `after` loop on its thread). `before()` runs
    before a box goes up (the player leaves its mini strip first).

    The box holds every click in the program until it's answered, so it only goes up while `root` is
    on screen, and brings it in front of the program's other windows: in a minimised window it would
    leave the Library (say) not answering, with nothing to show why. Errors met meanwhile wait (one
    of each kind, a few at most) until the window is back."""
    waiting: list[Report] = []

    def drain():
        try:
            while True:
                try:
                    found = _pending.get_nowait()
                except queue.Empty:
                    break
                if len(waiting) < MAX_BOXES and all(found.signature != w.signature for w in waiting):
                    waiting.append(found)
            while waiting and _on_screen(root):
                found = waiting.pop(0)
                if may_show(found):
                    if before is not None:
                        try:
                            before()
                        except Exception:  # noqa: BLE001
                            log.debug("getting ready for the error box failed", exc_info=True)
                    root.lift()
                    show_in_app(root, found, folder())
        except Exception:  # noqa: BLE001 — the error box itself failing mustn't make more of them
            log.exception("couldn't show the error box")
        finally:
            try:
                root.after(DRAIN_MS, drain)
            except Exception:  # noqa: BLE001 — the window has gone
                pass

    root.after(DRAIN_MS, drain)


def _on_screen(root) -> bool:
    try:
        return root.state() not in ("iconic", "withdrawn")
    except Exception:  # noqa: BLE001 — the window has gone
        return False


def show_in_app(root, found: Report, folder: Path):
    """The in-app box. It doesn't wait: the error handler returns at once, and the box answers
    for itself (Copy details and Open log folder keep it open; Close closes it)."""
    global _open
    from . import popups

    class CrashPrompt(popups.Prompt):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            # In the player's narrowest window the three buttons don't quite fit: the middle one
            # then says less.
            from . import theme

            room = self._width() - 2 * theme.px(theme.GAP_WIDE) - 2 * theme.px(popups.WARN_EDGE)
            need = sum(b.winfo_reqwidth() for b in self.buttons) + 2 * theme.px(theme.GAP)
            if need > room:
                self.buttons[1].set_text(SHORT_FOLDER_LABEL)
                self._seat()

        def _chosen(self, index: int):
            value = self.values[index]
            if value == COPY:
                copied = copy_text(found.text, root)
                self.buttons[index].set_text("Copied" if copied else "Couldn't copy")
            elif value == FOLDER:
                open_folder(folder)
            else:
                super()._chosen(index)

        def answer(self, value):
            global _open
            _open = False
            super().answer(value)

    _open = True
    try:
        return CrashPrompt(root, TITLE, MESSAGE, detail=DETAIL, tone="warn", cancel=CLOSE, default=2,
                           buttons=(("Copy details", COPY), ("Open log folder", FOLDER), ("Close", CLOSE)))
    except Exception:
        _open = False
        raise


# -- with no window: Windows' own box -----------------------------------------------------------------
def startup_failed(exc_type, value, tb, folder: Path | None = None) -> None:
    """Ironbark couldn't start: say so, with the details a click away."""
    found = describe("starting up", exc_type, value, tb)
    folder = folder if folder is not None else log_folder()
    first = traceback.format_exception_only(exc_type, value)[-1].strip() if exc_type is not None else ""
    show_native(f"{APP_NAME} couldn't start",
                f"Something went wrong while {APP_NAME} was starting.\n\n{first}\n\n"
                f"The details are saved in crash.log, in {folder}. Copy them to paste into a problem report.",
                found.text, folder)


def show_native(title: str, message: str, details: str, folder: Path) -> None:
    """Windows' message box, buttons Copy details · Open log folder · Close. The first two do their
    job and bring the box back, so both can be used."""
    note = ""
    for _ in range(NATIVE_ROUNDS):
        choice = native_box(title, message + note)
        if choice == COPY:
            note = "\n\nThe details are copied: paste them where you need them." if copy_text(details) \
                else "\n\nThe details couldn't be copied: they're in crash.log."
        elif choice == FOLDER:
            open_folder(folder)
            note = ""
        else:
            return


def tell_native(title: str, message: str, warning: bool = True) -> None:
    """A plain message with an OK button, for when there's no window (or no console) to say it in."""
    if sys.platform != "win32":
        if sys.stderr is not None:
            sys.stderr.write(f"{title}\n{message}\n")
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    user32.MessageBoxW.argtypes = (wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT)
    user32.MessageBoxW(None, message, title, (MB_ICONWARNING if warning else MB_ICONINFORMATION) | MB_SETFOREGROUND)


MB_YESNOCANCEL = 0x3
MB_DEFBUTTON3 = 0x200   # Enter = Close
MB_ICONERROR = 0x10
MB_ICONWARNING = 0x30
MB_ICONINFORMATION = 0x40
MB_SETFOREGROUND = 0x10000
IDYES, IDNO, IDCANCEL = 6, 7, 2
WH_CBT, HCBT_ACTIVATE = 5, 5
WM_GETFONT = 0x31
SWP_NOZORDER, SWP_NOMOVE, SWP_NOACTIVATE = 0x4, 0x2, 0x10
LABELS = ((IDYES, "Copy details"), (IDNO, "Open log folder"), (IDCANCEL, "Close"))


def native_box(title: str, text: str) -> str:
    """Show the box; COPY, FOLDER or CLOSE. Windows' box only offers Yes / No / Cancel, so the
    buttons are renamed (and widened to fit) as it opens; if that can't be done, the text says
    which button does what."""
    if sys.platform != "win32":
        if sys.stderr is not None:
            sys.stderr.write(f"{title}\n{text}\n")
        return CLOSE
    import ctypes
    from ctypes import wintypes

    user32, kernel32 = _dlls()
    hook_proc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    user32.SetWindowsHookExW.argtypes = (ctypes.c_int, hook_proc, wintypes.HINSTANCE, wintypes.DWORD)
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    user32.CallNextHookEx.restype = ctypes.c_ssize_t
    user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
    user32.MessageBoxW.argtypes = (wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT)
    hook = {"handle": None}

    def on_window(code, wparam, lparam):
        if code == HCBT_ACTIVATE:   # the box exists, laid out, about to be shown
            try:
                _relabel(user32, wparam)
            finally:
                user32.UnhookWindowsHookEx(hook["handle"])
                hook["handle"] = None
            return 0
        return user32.CallNextHookEx(hook["handle"], code, wparam, lparam)

    callback = hook_proc(on_window)   # (kept referenced until the box has closed)
    hook["handle"] = user32.SetWindowsHookExW(WH_CBT, callback, None, kernel32.GetCurrentThreadId())
    if not hook["handle"]:
        text += "\n\nYes: copy the details · No: open the log folder · Cancel: close"
    try:
        answer = user32.MessageBoxW(None, text, title,
                                    MB_YESNOCANCEL | MB_DEFBUTTON3 | MB_ICONERROR | MB_SETFOREGROUND)
    finally:
        if hook["handle"]:
            user32.UnhookWindowsHookEx(hook["handle"])
        del callback
    return {IDYES: COPY, IDNO: FOLDER}.get(answer, CLOSE)


def _dlls():
    """user32 and kernel32 on handles of our own (their types are set here, and the shared
    ctypes.windll ones are used by other modules with other types)."""
    import ctypes

    return ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")


def _relabel(user32, dialog) -> None:
    """Rename the box's buttons, and widen them (and the box, if need be) to fit the new names."""
    import ctypes
    from ctypes import wintypes

    gdi32 = ctypes.WinDLL("gdi32")
    user32.GetDlgItem.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.GetDlgItem.restype = wintypes.HWND
    user32.SetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPCWSTR)
    user32.SendMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.GetDC.argtypes = (wintypes.HWND,)
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
    user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    user32.GetClientRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    user32.MapWindowPoints.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.POINTER(wintypes.RECT), wintypes.UINT)
    user32.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, wintypes.UINT)
    gdi32.SelectObject.argtypes = (wintypes.HDC, wintypes.HGDIOBJ)
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.GetTextExtentPoint32W.argtypes = (wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.SIZE))

    buttons = []
    for ident, label in LABELS:
        button = user32.GetDlgItem(dialog, ident)
        if not button:
            continue
        user32.SetWindowTextW(button, label)
        size = wintypes.SIZE()
        dc = user32.GetDC(button)
        old = gdi32.SelectObject(dc, user32.SendMessageW(button, WM_GETFONT, 0, 0))
        gdi32.GetTextExtentPoint32W(dc, label, len(label), ctypes.byref(size))
        gdi32.SelectObject(dc, old)
        user32.ReleaseDC(button, dc)
        rect = wintypes.RECT()
        user32.GetWindowRect(button, ctypes.byref(rect))
        user32.MapWindowPoints(None, dialog, ctypes.byref(rect), 2)
        buttons.append((button, size.cx, rect))
    if not buttons:
        return
    buttons.sort(key=lambda b: b[2].left)
    height = buttons[0][2].bottom - buttons[0][2].top
    width = buttons[0][2].right - buttons[0][2].left
    widest = max(text_width for _, text_width, _ in buttons)
    want = widest + height   # the name, with about half a button's height each side
    if want <= width:
        return
    gap = buttons[1][2].left - buttons[0][2].right if len(buttons) > 1 else 0
    client = wintypes.RECT()
    user32.GetClientRect(dialog, ctypes.byref(client))
    margin = client.right - buttons[-1][2].right   # the room right of the last button, kept on the left too
    lefts, x = [], buttons[-1][2].right
    for _ in buttons:
        lefts.insert(0, x - want)
        x -= want + gap
    short = margin - lefts[0]
    if short > 0:   # the box is too narrow for the wider buttons: widen it, keeping its centre
        outer = wintypes.RECT()
        user32.GetWindowRect(dialog, ctypes.byref(outer))
        user32.SetWindowPos(dialog, None, outer.left - short // 2, outer.top, outer.right - outer.left + short,
                            outer.bottom - outer.top, SWP_NOZORDER | SWP_NOACTIVATE)
        lefts = [left + short for left in lefts]
    for (button, _, rect), left in zip(buttons, lefts):
        user32.SetWindowPos(button, None, left, rect.top, want, height, SWP_NOZORDER | SWP_NOACTIVATE)


# -- the buttons' jobs ------------------------------------------------------------------------------
def copy_text(text: str, root=None) -> bool:
    """Put `text` on the clipboard: through Tk when there's a window, else Windows' own."""
    if root is not None:
        try:
            root.clipboard_clear()
            root.clipboard_append(text)
            return True
        except Exception:  # noqa: BLE001
            log.warning("couldn't copy to the clipboard", exc_info=True)
            return False
    if sys.platform != "win32":
        return False
    return _copy_native(text)


def _copy_native(text: str) -> bool:
    import ctypes
    from ctypes import wintypes

    user32, kernel32 = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
    user32.OpenClipboard.argtypes = (wintypes.HWND,)
    user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalFree.argtypes = (wintypes.HGLOBAL,)
    data = text.encode("utf-16-le") + b"\0\0"
    for _ in range(10):   # another program may hold the clipboard for a moment
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return False
    try:
        user32.EmptyClipboard()
        memory = kernel32.GlobalAlloc(0x2, len(data))   # GMEM_MOVEABLE
        if not memory:
            return False
        pointer = kernel32.GlobalLock(memory)
        if not pointer:
            kernel32.GlobalFree(memory)
            return False
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(memory)
        if not user32.SetClipboardData(13, memory):   # CF_UNICODETEXT; the clipboard owns it from here
            kernel32.GlobalFree(memory)
            return False
        return True
    finally:
        user32.CloseClipboard()


def open_folder(folder: Path) -> bool:
    """Open a folder in Explorer (Finder, the file manager)."""
    try:
        Path(folder).mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(folder))   # noqa: S606 — a folder of our own, opened for the user
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(folder)])
    except OSError:
        log.warning("couldn't open the folder %s", folder, exc_info=True)
        return False
    return True
