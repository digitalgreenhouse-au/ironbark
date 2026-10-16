"""The web player (web/): its promises checked from here, and its own JavaScript tests run.

The web player is plain HTML, CSS and JavaScript with no build step. These tests check what it
promises: it can't reach the internet (a Content Security Policy, and no network calls in its
scripts), it loads nothing from outside, its version is the player's, its colours are the
design tokens, and its offline copy lists every file the page needs. The JavaScript tests
(web/tests, Node's own test runner) run when Node.js is installed and are skipped when it isn't.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

import ironbark
from ironbark import theme

WEB = Path(__file__).resolve().parent.parent / "web"
PAGE = WEB / "index.html"
SCRIPTS = sorted((WEB / "js").glob("*.js"))
SOURCES = [PAGE, WEB / "ironbark.css", WEB / "sw.js", WEB / "manifest.webmanifest", *SCRIPTS]
SVG_NAMESPACE = "http://www.w3.org/2000/svg"


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def csp() -> dict[str, list[str]]:
    match = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', text(PAGE))
    assert match, "the page has no Content Security Policy"
    rules = {}
    for part in match.group(1).split(";"):
        words = part.split()
        if words:
            rules[words[0]] = words[1:]
    return rules


def test_the_page_is_locked_away_from_the_internet():
    rules = csp()
    assert rules["default-src"] == ["'none'"]
    assert rules["connect-src"] == ["'none'"]          # no fetch, XHR, WebSocket or beacon
    assert rules["script-src"] == ["'self'"]           # its own scripts only: none inline, none from outside
    assert rules["style-src"] == ["'self'"]
    assert rules["media-src"] == ["blob:"]             # songs come from the listener's own files
    assert set(rules["img-src"]) <= {"'self'", "blob:", "data:"}
    assert rules["form-action"] == ["'none'"]
    assert rules["base-uri"] == ["'none'"]


def test_no_outside_addresses_or_network_calls():
    for path in SOURCES:
        body = text(path).replace(SVG_NAMESPACE, "")
        assert not re.search(r"(https?:)?//[a-z0-9.-]+\.[a-z]{2,}", body, re.I), f"{path.name} names an outside address"
    for path in SCRIPTS:
        body = text(path)
        for call in ("fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon", "EventSource", "importScripts"):
            assert call not in body, f"{path.name} uses {call}"
    # the offline copy fetches only the page's own files, from where the page came from
    assert "url.origin !== self.location.origin" in text(WEB / "sw.js")
    # …and the browser waits for each saved copy to be written before it may stop the worker
    assert "ev.waitUntil(caches.open(CACHE).then((c) => c.put(" in text(WEB / "sw.js")


def test_nothing_inline_that_the_lock_would_block():
    page = text(PAGE)
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", page), "an inline script"
    assert not re.search(r"\sstyle=", page), "an inline style attribute"
    assert not re.search(r"\son[a-z]+=", page), "an inline event handler"
    assert "<style" not in page


def test_the_version_is_the_players():
    assert re.search(r'IronbarkVersion = "([^"]+)"', text(WEB / "js" / "version.js")).group(1) == ironbark.__version__
    assert re.search(r'const VERSION = "([^"]+)"', text(WEB / "sw.js")).group(1) == ironbark.__version__


def test_the_offline_copy_lists_every_file_the_page_loads():
    files = re.search(r"const FILES = \[(.*?)\];", text(WEB / "sw.js"), re.S).group(1)
    listed = set(re.findall(r'"([^"]+)"', files)) - {"./"}
    for name in listed:
        assert (WEB / name).is_file(), f"sw.js lists a missing file: {name}"
    page = text(PAGE)
    loaded = set(re.findall(r'<script src="([^"]+)"', page)) | set(re.findall(r'<link rel="(?:stylesheet|icon|manifest|apple-touch-icon)" href="([^"]+)"', page))
    assert loaded <= listed, f"not in the offline copy: {sorted(loaded - listed)}"
    for script in SCRIPTS:
        assert f"js/{script.name}" in listed


@pytest.mark.parametrize(("palette", "selector"), [
    (theme.DARK, ':root[data-theme="dark"]'),
    (theme.LIGHT, ':root[data-theme="light"]'),
    (theme.LIGHT, ":root:not("),               # Follow system, when the system is light
], ids=["dark", "light", "system-light"])
def test_the_colours_are_the_design_tokens(palette, selector):
    css = text(WEB / "ironbark.css")
    block = css[css.index(selector):]
    block = block[: block.index("}")]
    found = dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-f]{6})", block))
    assert found, selector
    checked = 0
    for role, colour in found.items():
        name = role.upper().replace("-", "_")
        if name in theme.ROLES:
            assert colour == getattr(palette, name), f"--{role} in {palette.name}"
            checked += 1
    assert checked >= (15 if selector.endswith('"]') else 10)   # the system block repeats only what differs


def test_the_shortcuts_sheet_and_the_desktop_player_agree_on_the_common_keys():
    app = text(WEB / "js" / "app.js")
    for keys, what in [("Space", "Play / pause"), ("S", "Shuffle on / off"), ("M", "Meters"), ("I", "Details")]:
        assert re.search(rf'"{re.escape(keys)}", "{re.escape(what)}', app), keys


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js isn't installed")
def test_the_javascript_tests_pass():
    tests = sorted(str(p) for p in (WEB / "tests").glob("*.test.js"))
    assert tests
    # The TAP reporter, named: Node's default differs by version and by whether output is a terminal.
    run = subprocess.run(["node", "--test", "--test-reporter=tap", *tests], capture_output=True, text=True,
                         encoding="utf-8", timeout=120)
    assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-2000:]
    assert re.search(r"^# fail 0$", run.stdout, re.M), run.stdout[-2000:]
    assert not re.search(r"^# pass 0$", run.stdout, re.M), run.stdout[-2000:]   # (and something ran)
