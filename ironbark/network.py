"""The one place Ironbark goes online, behind the one switch.

Settings → Network → Allow network access (`network_allowed`, off unless turned on) gates every
request: rip verification's look-ups (tools/ripcheck.py) and Check for updates (updates.py) both
come through `fetch`. Music on network drives doesn't come through here: Windows fetches those
files, as it does for any program.
"""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request

from . import APP_NAME, __version__

TIMEOUT = 20                 # seconds the server may stay silent (connecting, or between data)
DEADLINE = 30                # seconds for a whole answer, however slowly it trickles in
MAX_BYTES = 4 << 20          # an answer bigger than this isn't what we asked for
CHUNK = 64 << 10             # bytes read at a time, so a slow answer can be given up on

_urlopen = urllib.request.urlopen   # tests replace this; nothing else in the player goes online


class NetworkOff(RuntimeError):
    """Raised if anything tries to go online while Settings → Allow network access is off."""


class Cancelled(Exception):
    """The request was cancelled (its window closed) while waiting for an answer."""


def network_allowed(prefs: dict | None) -> bool:
    """The one switch: Settings → Network → Allow network access (off unless turned on)."""
    return bool(prefs and prefs.get("allow_network"))


def fetch(url: str, prefs: dict | None, cancel: threading.Event | None = None,
          https_only: bool = False) -> bytes | None:
    """GET one address. None = not found (404). Refuses unless network access is allowed —
    checked here, at the one place this program goes online, not just in the window. Gives up
    after DEADLINE seconds (TimeoutError) and stops when `cancel` is set (Cancelled), checked
    between pieces of the answer. `https_only`: an answer that a redirect brought from a plain
    http:// address is refused (URLError)."""
    if not network_allowed(prefs):
        raise NetworkOff("network access is off (Settings → Network)")
    if cancel is not None and cancel.is_set():
        raise Cancelled()
    request = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME.replace(' ', '-')}/{__version__}"})
    deadline = time.monotonic() + DEADLINE
    try:
        with _urlopen(request, timeout=TIMEOUT) as response:
            answered_from = str(getattr(response, "url", None) or url)   # (where any redirects ended)
            if https_only and not answered_from.lower().startswith("https://"):
                raise urllib.error.URLError(f"the answer came from a plain http address: {answered_from}")
            read = getattr(response, "read1", response.read)   # read1: whatever has arrived
            data = bytearray()
            while len(data) < MAX_BYTES:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                if time.monotonic() > deadline:
                    raise TimeoutError(f"no complete answer in {DEADLINE} s")
                piece = read(min(CHUNK, MAX_BYTES - len(data)))
                if not piece:
                    break
                data += piece
            return bytes(data)
    except urllib.error.HTTPError as error:
        error.close()   # its connection, now rather than whenever it's garbage-collected
        if error.code == 404:
            return None
        raise
