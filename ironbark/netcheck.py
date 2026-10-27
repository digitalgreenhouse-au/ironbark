"""Why a library folder won't load — above all one on the network — in plain English. No UI code.

- `meaning(error)`: what an error from a network folder comes to, and what to try (Windows' error
  numbers; elsewhere the errno). The Library's own message when it can't read a library folder
  says it after the system's words (indexer), and so does the log.
- `check(folder, prefs)`: one library folder, step by step: where it is (a mapped drive: the server
  path it stands for), whether the share and the folder answer and how fast, Python's way of
  listing it and File Explorer's way (some file servers answer only the second: winlisting.py),
  what the Library gets, and an album inside — then what that adds up to (`Check.verdict`).
- `report(folders, prefs)`: every library folder's check as text (Settings → Library → Check
  folders… saves it and opens it; Save diagnostics… puts it in the .zip, names left out).

Nothing is changed and nothing is sent: it only looks, at the folders the Library reads anyway. A
folder on the network is looked at only while Local network access is on. Every step has a time
limit (STEP_LIMIT_S): a server that's asleep or gone can keep a call waiting a long time (42 s was
measured), so a step that hasn't answered by then is reported as not answering, and left to end on
its own thread.
"""

from __future__ import annotations

import datetime
import errno
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import APP_NAME, __version__, places, platforms, winlisting

STEP_LIMIT_S = 20.0    # a step with no answer by then is reported as not answering
SLOW_MS = 2000         # a listing slower than this is said to be slow

# What Windows' errors from a network folder come to, and what to try. (The error itself carries
# Windows' own words; these say what they mean for a music folder.)
_MEANINGS = {
    winlisting.BAD_SIGNATURE: (
        "the file server sent a reply that failed Windows' check of its signature (SMB signing), so "
        "Windows threw it away. Ironbark then asks the way File Explorer does; if it still can't, the "
        "server's file-sharing settings need looking at"),
    51: "the server wasn't found on the network: it may be off or asleep, or its address may have changed",
    53: "the server wasn't found on the network: it may be off or asleep, or its address may have changed",
    67: "the server answered, but it has no shared folder by that name (renamed, or no longer shared)",
    64: "the connection to the server dropped while reading (the server went to sleep, or the network blinked)",
    59: "the network connection failed partway (a short network hiccup, or the server restarting)",
    121: "the server took too long to answer: it may be waking up, so try again in a minute",
    1231: "this computer can't reach the server's network (Wi-Fi, the cable, or a VPN)",
    1232: "this computer can't reach the server's network (Wi-Fi, the cable, or a VPN)",
    1222: "the network isn't started on this computer",
    86: "the server turned down the password: reconnect the drive in File Explorer with the right login",
    1326: "the server turned down the user name or password: reconnect the drive in File Explorer with the right login",
    1219: "Windows is already connected to this server under another user name, and it allows only one",
    5: "the server says this login may not read the folder",
    1201: ("the drive letter isn't connected just now (File Explorer shows it with a red cross): open it "
           "once in File Explorer and Windows connects it again"),
    2250: "the drive letter isn't connected to a server",
    2: "the folder isn't there: renamed or moved on the server, or the drive letter now leads somewhere else",
    3: "the folder isn't there: renamed or moved on the server, or the drive letter now leads somewhere else",
}

# What a Mac's or Linux's errors from a network mount come to — by errno name, as the numbers differ
# from one system to another (and Windows' error numbers above mean other things: 5 there is "access
# denied", here it's EIO, a mount that dropped).
_POSIX_MEANINGS = {
    "EIO": "the network share stopped answering while it was read (it dropped, or the server went to sleep)",
    "EACCES": "the server says this login may not read the folder",
    "EPERM": "the server says this login may not read the folder",
    "ENOENT": "the folder isn't there: renamed or moved on the server, or the share isn't mounted just now",
    "ENOTCONN": "the network share isn't connected just now: mount it again",
    "ESTALE": "the share changed on the server (or was mounted again): open the folder again, or mount the share again",
    "ETIMEDOUT": "the server took too long to answer: it may be waking up, so try again in a minute",
    "EHOSTDOWN": "the server wasn't found on the network: it may be off or asleep",
    "EHOSTUNREACH": "this computer can't reach the server's network (Wi-Fi, the cable, or a VPN)",
    "ENETUNREACH": "this computer can't reach the server's network (Wi-Fi, the cable, or a VPN)",
    "ENETDOWN": "the network isn't up on this computer",
}
_BY_ERRNO = {getattr(errno, name): text for name, text in _POSIX_MEANINGS.items() if hasattr(errno, name)}


def code_of(error: BaseException) -> int | None:
    """Windows' error number for an OSError (signed, as Python gives it), else its errno."""
    if not isinstance(error, OSError):
        return None
    code = getattr(error, "winerror", None)
    if isinstance(code, int) and code >= 1 << 31:   # (an HRESULT given unsigned)
        code -= 1 << 32
    return code if code else error.errno


def meaning(error: BaseException) -> str:
    """What this error from a folder comes to, in plain words ("" when there's nothing to add): by
    Windows' error number when it carries one, else by its errno (a Mac or Linux mount)."""
    if isinstance(getattr(error, "winerror", None), int):
        code = code_of(error)
        if code in _MEANINGS:
            return _MEANINGS[code]
    elif isinstance(error, OSError) and error.errno in _BY_ERRNO:
        return _BY_ERRNO[error.errno]
    if isinstance(error, TimeoutError):
        return "the server took too long to answer: it may be asleep or busy"
    return ""


def words(error: BaseException) -> str:
    """The system's words for an error, with its number: what to look up."""
    text = (getattr(error, "strerror", None) or str(error) or type(error).__name__).rstrip(". ")
    code = code_of(error)
    return f"{text}, error {code}" if code else text


@dataclass
class Step:
    name: str                   # what was tried: "The folder answers"
    ok: bool | None             # None: no answer within STEP_LIMIT_S
    detail: str = ""            # what came back ("1,846 names"), or why not
    ms: float = 0.0
    error: BaseException | None = None

    def line(self) -> str:
        mark = "OK" if self.ok else ("NO ANSWER" if self.ok is None else "FAILED")
        took = f" ({self.ms:,.0f} ms)" if self.ok is not None else ""
        text = f"{mark:<9} {self.name}{took}"
        return f"{text}: {self.detail}" if self.detail else text


@dataclass
class Check:
    folder: str
    where: str = ""                                 # "this computer", "Y: is \\\\server\\share"…
    steps: list[Step] = field(default_factory=list)
    verdict: str = ""

    @property
    def readable(self) -> bool:
        return any(step.name == LIBRARY_GETS and step.ok for step in self.steps)


LIBRARY_GETS = "What the Library gets"


def _timed(call: Callable[[], object], limit: float) -> tuple[bool | None, object, float]:
    """call() on a thread of its own, waited for at most `limit` s: (True, its value, ms), (False,
    the exception, ms) or (None, None, the limit in ms) when it hasn't answered (it carries on
    alone: a call waiting on a server can't be stopped)."""
    found: dict = {}

    def run():
        start = time.perf_counter()
        try:
            found["value"] = call()
        except BaseException as error:  # noqa: BLE001 — reported as the step's answer
            found["error"] = error
        found["ms"] = (time.perf_counter() - start) * 1000

    thread = threading.Thread(target=run, name="netcheck-step", daemon=True)
    thread.start()
    thread.join(limit)
    if thread.is_alive() or "ms" not in found:
        return None, None, limit * 1000
    if "error" in found:
        return False, found["error"], found["ms"]
    return True, found.get("value"), found["ms"]


def _step(steps: list[Step], name: str, call: Callable[[], object], describe: Callable[[object], str] = str,
          limit: float = STEP_LIMIT_S) -> Step:
    ok, value, ms = _timed(call, limit)
    if ok:
        step = Step(name, True, describe(value), ms)
    elif ok is None:
        step = Step(name, None, f"nothing after {limit:.0f} s: the server may be asleep, off, or very busy", ms)
    else:
        assert isinstance(value, BaseException)
        explained = meaning(value)
        step = Step(name, False, words(value) + (f" — {explained}" if explained else ""), ms, value)
    steps.append(step)
    return step


class _Partway(OSError):
    """A listing that gave some names, then failed."""

    def __init__(self, given: int, error: OSError):
        super().__init__(error.errno, error.strerror, error.filename)
        self.winerror = getattr(error, "winerror", None)
        self.given = given


def _python_way(folder: str) -> int:
    """Python's own listing (os.scandir unwrapped), read to the end: how many names."""
    given = 0
    with winlisting._scandir(folder) as listing:
        try:
            for _entry in listing:
                given += 1
        except OSError as error:
            if not given:
                raise
            raise _Partway(given, error) from error
    return given


def _names(count: object) -> str:
    return f"{count:,} name{'' if count == 1 else 's'}"


def _look(path: str):
    """Does it answer? (os.stat: a server that's asleep keeps this waiting.)"""
    return os.stat(path)


def _share_root(folder: str) -> str:
    """The share (or drive) a folder is on: "Y:\\" or "\\\\server\\share\\"."""
    drive = os.path.splitdrive(folder)[0]
    return os.path.join(drive, os.sep) if drive else os.sep


def check(folder: str | Path, prefs: dict | None, limit: float = STEP_LIMIT_S,
          target: Callable[[str], str | None] = places.network_target, name_album: bool = True) -> Check:
    """One library folder, step by step (see the module notes). Waits on the network: never on the
    window's thread. `target("Y:")`: the server path a drive letter stands for. `name_album`: the
    album looked inside is named (not in Save diagnostics' copy: no music's names leave the PC)."""
    folder = str(folder)
    result = Check(folder)
    remote = places.is_remote(folder)
    letter = os.path.splitdrive(folder)[0] if platforms.IS_WINDOWS else ""
    if not remote:
        result.where = "this computer"
    elif len(letter) == 2 and letter[1] == ":":
        ok, server, _ms = _timed(lambda: target(letter), limit)
        result.where = (f"a network drive: {letter} is {server}" if ok and server
                        else f"a network drive ({letter}: Windows didn't say which server)")
    else:
        result.where = f"a network share (server {places.host_of(folder) or 'unknown'})"
    if remote and not places.allowed(folder, prefs):
        result.verdict = ("Not read: it's on your network, and Local network access is off "
                          "(Settings → Library). Nothing else was tried.")
        return result
    steps = result.steps
    if remote and os.path.splitdrive(folder)[0]:   # (a drive letter or \\server\share; a Mac / Linux mount has no root of its own here)
        share = _step(steps, "The share answers", lambda: _look(_share_root(folder)), lambda _v: "", limit)
        if share.ok is not True:
            result.verdict = _verdict(result)
            return result
    here = _step(steps, "The folder answers", lambda: _look(folder), lambda _v: "", limit)
    if here.ok is not True:
        result.verdict = _verdict(result)
        return result
    python = _step(steps, "Python's way of listing", lambda: _python_way(folder), _names, limit)
    if python.ok is False and isinstance(python.error, _Partway):
        python.detail = f"the first {_names(python.error.given)}, then {python.detail}"
    if platforms.IS_WINDOWS:
        _step(steps, "File Explorer's way of listing", lambda: len(winlisting.list_basic(folder)), _names, limit)
    found: list = []
    gets = _step(steps, LIBRARY_GETS, lambda: _library_way(folder, found), _names, limit)
    if gets.ok:
        album = next((entry for entry in found if _is_dir(entry)), None)
        if album is not None:
            name = f"An album inside (“{album.name}”)" if name_album else "An album inside"
            _step(steps, name, lambda: len(os.listdir(album.path)), _names, limit)
    result.verdict = _verdict(result)
    return result


def _library_way(folder: str, found: list) -> int:
    """The listing the Library gets (os.scandir, as the player has it: winlisting on Windows), into
    `found`: how many names."""
    with os.scandir(folder) as listing:
        found.extend(listing)
    return len(found)


def _is_dir(entry) -> bool:
    try:
        return entry.is_dir()
    except OSError:
        return False


def _verdict(result: Check) -> str:
    """What the steps add up to, in a sentence or two."""
    by = {step.name: step for step in result.steps}
    failed = next((step for step in result.steps if step.ok is not True), None)
    gets = by.get(LIBRARY_GETS)
    python = by.get("Python's way of listing")
    if gets is not None and gets.ok:
        slow = [step for step in result.steps if step.ok and step.ms > SLOW_MS]
        text = f"Readable: the Library gets {gets.detail}."
        if python is not None and python.ok is False:
            text += (" Python's own way of listing it fails, File Explorer's works, and Ironbark uses "
                     "File Explorer's way for it.")
        elif failed is not None:
            text += f" But: {failed.name.lower()} — {failed.detail}."
        if slow:
            text += f" Slow: {slow[0].name.lower()} took {slow[0].ms / 1000:.1f} s."
        return text
    if gets is not None and gets.ok is False:   # (asked every way and still not: that's what counts)
        return f"Not readable now: {gets.detail}."
    if failed is None:
        return "Not checked."
    if failed.ok is None:
        return f"Not readable now: {failed.name.lower()} — no answer within {failed.ms / 1000:.0f} s (the server may be asleep or off)."
    return f"Not readable now: {failed.name.lower()} — {failed.detail}."


def signing_required() -> bool | None:
    """Does this PC's file-sharing client require signed replies? (Windows: Get-SmbClientConfiguration;
    None when it can't be told.)"""
    if not platforms.IS_WINDOWS:
        return None
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                              "(Get-SmbClientConfiguration).RequireSecuritySignature"],
                             capture_output=True, text=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip().lower()
    except (OSError, subprocess.SubprocessError):
        return None
    return {"true": True, "false": False}.get(out)


def report(folders: list, prefs: dict | None, limit: float = STEP_LIMIT_S,
           target: Callable[[str], str | None] = places.network_target,
           signing: Callable[[], bool | None] = signing_required,
           name_album: bool = True) -> tuple[str, list[Check]]:
    """Every library folder checked, as the report's text, and the checks themselves (`name_album`:
    see `check`)."""
    checks = [check(folder, prefs, limit, target, name_album) for folder in folders]
    lines = [f"{APP_NAME} {__version__} — library folders check, {datetime.datetime.now():%d/%m/%Y %H:%M:%S}",
             "Nothing was changed and nothing was sent: this only looks at the folders.", ""]
    if not checks:
        lines.append("There are no library folders.")
    for result in checks:
        lines += [result.folder, f"  Where: {result.where}"]
        lines += [f"  {step.line()}" for step in result.steps]
        lines += [f"  → {result.verdict}", ""]
    if any(code_of(step.error) == winlisting.BAD_SIGNATURE for result in checks for step in result.steps
           if step.error is not None):
        required = signing()
        lines.append("This PC's file-sharing client requires signed replies (SMB signing): "
                     + {True: "yes", False: "no", None: "couldn't be told"}[required] + ".")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n", checks


def summary(checks: list[Check]) -> str:
    """One line for the status line: how the check went."""
    bad = [result for result in checks if not result.readable]
    if not checks:
        return "There are no library folders to check"
    if not bad:
        return f"All {len(checks)} library folder{'' if len(checks) == 1 else 's'} can be read"
    first = bad[0]
    more = f" (and {len(bad) - 1} more)" if len(bad) > 1 else ""
    return f"{first.folder}{more}: {first.verdict}"
