"""Checking for a newer Ironbark — opt-in. No UI code.

Off unless Settings → Help → "Check for updates" is switched on, and then only while "Allow
network access" is on too (the one switch every use of the internet answers to: the fetch is
ripcheck.fetch, which checks it). At most once a day it reads one small file, UPDATE_URL, which
says what the latest version is. What goes out is the request itself: it names the player and its
version (the User-Agent), and the website sees the computer's internet address, as any website
does; nothing else is sent. No file there yet, no connection, a slow server: all quietly mean
"nothing new".

It never downloads or installs anything. A newer version shows a line in the status line; clicking
it opens the download page in the web browser. The file's format is in docs/update-feed.md.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

from . import __version__, ripcheck

log = logging.getLogger(__name__)

UPDATE_URL = "https://www.digitalgreenhouse.com.au/ironbark/version.json"
DOWNLOAD_PAGE = "https://www.digitalgreenhouse.com.au/ironbark"
CHECK_EVERY_S = 24 * 3600
# The download link must lead to Digital Greenhouse's own site, so a tampered file can't send
# anyone elsewhere (it falls back to DOWNLOAD_PAGE).
TRUSTED_HOSTS = {"www.digitalgreenhouse.com.au", "digitalgreenhouse.com.au"}
NOTES_LIMIT = 300   # characters of the release notes kept


@dataclass(frozen=True)
class Release:
    version: str
    date: str = ""     # dd/mm/yyyy, as published
    url: str = DOWNLOAD_PAGE
    notes: str = ""


def enabled(prefs: dict | None) -> bool:
    """Both switches on: Check for updates, and Allow network access."""
    return bool(prefs and prefs.get("check_updates")) and ripcheck.network_allowed(prefs)


def due(prefs: dict | None, now: float | None = None) -> bool:
    """Time for a check: switched on, and a day since the last (or the clock has gone backwards)."""
    if not enabled(prefs):
        return False
    now = time.time() if now is None else now
    try:
        last = float(prefs.get("update_checked") or 0.0)
    except (TypeError, ValueError):
        last = 0.0
    return now - last >= CHECK_EVERY_S or last > now


def version_tuple(text) -> tuple[int, int, int] | None:
    """"1.2.3" (a leading v, or anything after the third number, is ignored) → (1, 2, 3)."""
    match = re.match(r"\s*v?(\d{1,6})\.(\d{1,6})\.(\d{1,6})", text) if isinstance(text, str) else None
    return tuple(int(n) for n in match.groups()) if match else None


def newer(candidate: str, current: str = __version__) -> bool:
    found, have = version_tuple(candidate), version_tuple(current)
    return found is not None and have is not None and found > have


def _trusted(url) -> bool:
    if not isinstance(url, str):
        return False
    parts = urlsplit(url.strip())
    return parts.scheme == "https" and (parts.hostname or "").lower() in TRUSTED_HOSTS


def parse(data: bytes | None) -> Release | None:
    """The feed's JSON → a Release, or None for anything that isn't a usable one."""
    if not data:
        return None
    try:
        info = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(info, dict) or version_tuple(info.get("version")) is None:
        return None
    text = lambda key: info.get(key) if isinstance(info.get(key), str) else ""  # noqa: E731
    url = info.get("url")
    return Release(version=info["version"].strip(), date=text("date").strip(),
                   url=url.strip() if _trusted(url) else DOWNLOAD_PAGE,
                   notes=" ".join(text("notes").split())[:NOTES_LIMIT].rstrip())


def check(prefs: dict, fetch: Callable | None = None) -> Release | None:
    """Ask once. A newer release, or None (up to date, nothing published yet, switched off, no
    connection, any problem — logged, never raised). Waits on the network: call it off the window's
    thread."""
    if not enabled(prefs):
        return None
    fetch = fetch or ripcheck.fetch
    try:
        release = parse(fetch(UPDATE_URL, prefs))
    except Exception as error:  # noqa: BLE001 — a failed check is simply "nothing new"
        log.info("update check: %s", error)
        return None
    if release is None or not newer(release.version):
        return None
    log.info("update check: %s is available", release.version)
    return release
