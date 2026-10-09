"""The app platform before release: crash logging that can never take the player down with it,
diagnostics that leave out a mapped network drive's server, a launch made while the player is closing,
the update feed's download link, the test-signal settings, and the search key on keyboards where
'/' needs Shift."""

from __future__ import annotations

import errno
import io
import json
import queue
import sys
import threading
import time
import tkinter as tk

import pytest

from ironbark import crash, diagnostics, instance, keys, logs, network, ripcheck, settings, updates
from ironbark import __main__ as start
from ironbark.netcache import sources

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def error_info(message="boom", kind=ValueError):
    try:
        raise kind(message)
    except kind:
        return sys.exc_info()


# -- crash.log can never take the player down ----------------------------------------------------------
class FullDisk(io.StringIO):
    """A crash.log on a full drive (or a network folder that has just gone): every write fails."""

    def write(self, text):
        raise OSError(errno.ENOSPC, "No space left on device")


@pytest.fixture
def bare_root():
    try:
        root = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"no display: {error}")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass


def test_an_error_in_the_window_code_with_crash_log_unwritable_doesnt_end_the_player(monkeypatch, bare_root):
    monkeypatch.setattr(start, "_crash_file", FullDisk())
    monkeypatch.setattr(start, "_last_logged", None)
    bare_root.report_callback_exception = start._log_callback_error
    ran = []

    def broken():
        raise ValueError("an ordinary mistake in a callback")

    bare_root.after(10, broken)
    bare_root.after(150, lambda: (ran.append("still running"), bare_root.quit()))
    bare_root.mainloop()   # (an error escaping the handler would end the main loop here)
    assert ran == ["still running"]
    reported = crash._pending.get_nowait()   # the in-app box still hears of it
    assert "an ordinary mistake" in reported.text


def test_logging_an_error_never_raises_whatever_the_crash_log_does(monkeypatch):
    monkeypatch.setattr(start, "_crash_file", FullDisk())
    monkeypatch.setattr(start, "_last_logged", None)
    start._log_error("in the window code", *error_info("first"))
    start._log_callback_error(*error_info("second"))
    thread = threading.Thread(name="cover-art")
    start._log_thread_error(threading.ExceptHookArgs((*error_info("third"), thread)))
    closed = io.StringIO()
    closed.close()
    monkeypatch.setattr(start, "_crash_file", closed)   # (closed already, at the very end)
    start._log_callback_error(*error_info("fourth"))
    found = [crash._pending.get_nowait().where for _ in range(3)]
    assert found == ["in the window code", "in the cover-art thread", "in the window code"]


@pytest.fixture
def crash_log_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")
    monkeypatch.setattr(start.faulthandler, "enable", lambda **kwargs: None)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)   # put back afterwards
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(start, "_crash_file", None)
    monkeypatch.setattr(start, "_last_logged", None)
    yield tmp_path / "app"
    if start._crash_file is not None:
        start._crash_file.close()
    logs.shutdown()


def test_a_file_name_utf8_cant_hold_is_still_logged(crash_log_folder):
    # Windows allows a file name with half of a UTF-16 pair; Python carries it as a lone surrogate.
    start._start_crash_log()
    start._log_callback_error(*error_info("couldn't read D:\\Music\\half \udc80 a pair.flac"))
    start._crash_file.flush()
    logs.shutdown()
    assert "half \\udc80 a pair.flac" in (crash_log_folder / "crash.log").read_text(encoding="utf-8")
    assert "half \\udc80 a pair.flac" in (crash_log_folder / "app.log").read_text(encoding="utf-8")


def test_a_full_drive_at_start_up_doesnt_stop_the_player_starting(crash_log_folder, monkeypatch):
    monkeypatch.setattr(start, "open", lambda *args, **kwargs: FullDisk(), raising=False)
    start._start_crash_log()   # (the first lines written fail)
    assert sys.excepthook is start._log_uncaught and threading.excepthook is start._log_thread_error


def test_a_crash_log_that_cant_be_set_aside_is_still_written_to(crash_log_folder, monkeypatch):
    crash_log_folder.mkdir(parents=True)
    big = crash_log_folder / "crash.log"
    big.write_text("x" * 1_000_100 + "\n", encoding="utf-8")
    real_replace = type(big).replace

    def locked(self, target):
        if self.name == "crash.log":
            raise PermissionError(errno.EACCES, "The process cannot access the file")
        return real_replace(self, target)

    monkeypatch.setattr(type(big), "replace", locked)
    start._start_crash_log()
    assert start._crash_file is not None and sys.excepthook is start._log_uncaught
    start._log_callback_error(*error_info("after a rotation that failed"))
    start._crash_file.flush()
    assert "ValueError: after a rotation that failed" in big.read_text(encoding="utf-8")


# -- diagnostics: a mapped network drive ----------------------------------------------------------------
def test_diagnostics_leave_out_a_mapped_drives_server_and_its_server_paths(tmp_path, monkeypatch):
    # The library on Z:, which Windows maps to \\SAMS-NAS\Media: the network playback cache writes
    # the server path and the server's name, which the settings never spell out.
    monkeypatch.setattr(sources, "_connection", lambda letter: "\\\\SAMS-NAS\\Media" if letter == "Z:" else None)
    monkeypatch.setattr(sources, "_dos_device", lambda letter: None)
    folder = tmp_path / "Ironbark"
    folder.mkdir()
    (folder / "app.log").write_text(
        "07/10/2026 10:00:00 WARNING ironbark.netcache.manager: music cache: sams-nas isn't answering\n"
        "music cache: \\\\sams-nas\\media\\Music\\Abba\\Gold\\01 Waterloo.flac failed its check: size\n"
        "music cache: part of the copy of \\\\SAMS-NAS\\Media\\Music\\Abba\\Gold\\02 SOS.flac on this "
        "computer went missing; fetching it again\n", encoding="utf-8")
    prefs = {**settings.DEFAULTS, "library_folders": ["Z:\\Music"], "last_folder": "D:\\Rips"}
    files = diagnostics.contents(prefs, folder, outputs=lambda: (None, []), home="C:\\Users\\Jane")
    assert "sams" not in "\n".join(files.values()).lower()
    library = json.loads(files["settings.json"])["library_folders"][0]
    # As for a library on this computer: the library folder and the server go, what's inside it stays.
    assert "music cache: <server 1> isn't answering" in files["app.log"]
    assert f"music cache: {library}\\Abba\\Gold\\01 Waterloo.flac failed its check" in files["app.log"]
    assert f"the copy of {library}\\Abba\\Gold\\02 SOS.flac on this computer" in files["app.log"]


def test_a_mapped_drives_root_as_the_library_still_hides_its_server_path(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "_connection", lambda letter: "\\\\nas-of-jane\\music" if letter == "Y:" else None)
    monkeypatch.setattr(sources, "_dos_device", lambda letter: None)
    folder = tmp_path / "Ironbark"
    folder.mkdir()
    (folder / "crash.log").write_text("error reading \\\\NAS-of-Jane\\Music\\Kate Bush\\x.flac\n", encoding="utf-8")
    prefs = {**settings.DEFAULTS, "library_folders": ["Y:\\"]}
    files = diagnostics.contents(prefs, folder, outputs=lambda: (None, []), home="")
    assert "error reading <folder 1>\\Kate Bush\\x.flac" in files["crash.log"] and "Jane" not in files["crash.log"]


def test_a_failed_look_up_of_a_drive_letter_still_gives_diagnostics(tmp_path):
    def broken(letter):
        raise OSError("the network went away")
    folder = tmp_path / "Ironbark"
    folder.mkdir()
    prefs = {**settings.DEFAULTS, "library_folders": ["Z:\\Music"]}
    files = diagnostics.contents(prefs, folder, outputs=lambda: (None, []), home="", network=broken)
    assert json.loads(files["settings.json"])["library_folders"] == ["<folder 1>"]


def test_a_drive_letters_server_path_is_looked_up_only_for_network_drives(monkeypatch):
    monkeypatch.setattr(sources, "_connection", lambda letter: "\\\\server\\share\\" if letter == "Z:" else None)
    monkeypatch.setattr(sources, "_dos_device",
                        lambda letter: {"S:": "\\\\other\\share", "T:": "C:\\Work"}.get(letter))
    assert sources.network_target("Z:") == "\\\\server\\share"   # mapped
    assert sources.network_target("S:") == "\\\\other\\share"    # a subst onto a server path
    assert sources.network_target("T:") is None                 # a subst onto a local folder
    assert sources.network_target("C:") is None


# -- a launch while the player is closing ----------------------------------------------------------------
class ClosingCopy:
    """A running copy that's closing: its mutex is there but it has stopped listening, until `gone_at`
    (after which the next claim is this launch's own)."""

    def __init__(self, gone_at: float):
        self.gone_at = gone_at
        self.first = False
        self.requests = queue.Queue()

    def forward(self, paths, front=True, timeout=0):
        time.sleep(0.05)
        return False

    def closing(self):
        return True

    def stop_listening(self):
        pass

    def close(self):
        pass


def _closing_until(monkeypatch, gone_at):
    mine = ClosingCopy(0)
    mine.first = True

    def claim(*args, **kwargs):
        return mine if time.monotonic() >= gone_at else ClosingCopy(gone_at)
    monkeypatch.setattr(instance, "claim", claim)
    return mine


def test_a_launch_waits_for_a_copy_that_takes_longer_than_usual_to_close(monkeypatch):
    # Closing can take tens of seconds (a tag save finishing its file, a playlist being written).
    monkeypatch.setattr(instance, "FORWARD_TIMEOUT", 0.3)
    monkeypatch.setattr(instance, "CLOSING_WAIT_S", 5.0)
    mine = _closing_until(monkeypatch, time.monotonic() + 1.0)
    told, ran = [], []
    monkeypatch.setattr(crash, "tell_native", lambda title, text, warning=True: told.append(title))
    monkeypatch.setattr(start, "_run", lambda paths, here: ran.append((paths, here)))
    start.main([r"C:\Music\a.flac"])
    assert told == [] and ran == [([r"C:\Music\a.flac"], mine)]


def test_a_copy_still_closing_after_the_longest_wait_is_called_closing_not_stuck(monkeypatch):
    monkeypatch.setattr(instance, "FORWARD_TIMEOUT", 0.2)
    monkeypatch.setattr(instance, "CLOSING_WAIT_S", 0.6)
    _closing_until(monkeypatch, time.monotonic() + 60)
    told = []
    monkeypatch.setattr(crash, "tell_native", lambda title, text, warning=True: told.append((title, text)))
    began = time.monotonic()
    with pytest.raises(SystemExit) as stop:
        start.main([])
    assert stop.value.code == 1 and time.monotonic() - began >= 0.6
    assert "closing" in told[0][0] and "Task Manager" not in told[0][1]


@windows_only
def test_a_later_launch_can_tell_the_running_copy_is_closing():
    key = f"IronbarkTest-closing-{time.monotonic_ns()}"
    running = instance.claim(key)
    later = instance.claim(key)
    try:
        assert not later.closing()          # listening: a launch hands its files over
        running.stop_listening()            # its window has started closing
        assert instance.claim(key).closing()
    finally:
        running.close()


def test_the_socket_way_can_tell_the_running_copy_is_closing(tmp_path):
    key = f"IronbarkTest-closing-{time.monotonic_ns()}"
    running = instance.claim_with_socket(key, tmp_path)
    try:
        assert not instance.claim_with_socket(key, tmp_path).closing()
        running.stop_listening()
        assert instance.claim_with_socket(key, tmp_path).closing()
    finally:
        running.close()


# -- the update feed's download link ------------------------------------------------------------------
def feed(url):
    return json.dumps({"version": "99.0.0", "url": url}).encode()


@pytest.mark.parametrize("url", [
    "https://evil.example\\@www.digitalgreenhouse.com.au/ironbark",     # a browser reads \ as /
    "https://evil.example%5C@www.digitalgreenhouse.com.au/ironbark",
    "https://someone:secret@www.digitalgreenhouse.com.au/ironbark",
    "https://www.digitalgreenhouse.com.au:8443/ironbark",
    "https://www.digitalgreenhouse.com.au/iron bark",
    "https://www.digitalgreenhouse.com.au/iron\tbark",                    # (Python leaves a tab out)
    "https://www.digitalgreenhouse.com.au/ironbark\n.evil",
    "https://www.digitalgreenhouse.com.au/ironbark/télécharger",
])
def test_a_download_link_a_browser_could_read_another_way_isnt_trusted(url):
    assert updates.parse(feed(url)).url == updates.DOWNLOAD_PAGE


def test_a_plain_download_link_on_the_site_is_kept():
    for url in ("https://www.digitalgreenhouse.com.au/ironbark/download",
                "https://digitalgreenhouse.com.au/ironbark?from=app#windows"):
        assert updates.parse(feed(url)).url == url


class Answer(io.BytesIO):
    def __init__(self, data: bytes, url: str):
        super().__init__(data)
        self.url = url


def test_the_update_feed_is_never_taken_from_a_plain_http_redirect(monkeypatch):
    on = {"check_updates": True, "allow_network": True}
    monkeypatch.setattr(network, "_urlopen", lambda request, timeout: Answer(
        json.dumps({"version": "99.0.0"}).encode(), "http://www.digitalgreenhouse.com.au/ironbark/version.json"))
    assert updates.check(on) is None
    monkeypatch.setattr(network, "_urlopen", lambda request, timeout: Answer(
        json.dumps({"version": "99.0.0"}).encode(), "https://www.digitalgreenhouse.com.au/ironbark/version.json"))
    assert updates.check(on).version == "99.0.0"
    # Rip verification's own plain-http look-ups are unchanged.
    monkeypatch.setattr(network, "_urlopen", lambda request, timeout: Answer(b"ok", "http://db.cuetools.net/x"))
    assert ripcheck.fetch("http://db.cuetools.net/x", on) == b"ok"


# -- the test-signal settings ------------------------------------------------------------------------------
def test_test_signal_settings_out_of_range_fall_back_to_their_defaults(tmp_path, monkeypatch):
    path = tmp_path / "Ironbark" / "settings.json"
    monkeypatch.setattr(settings, "settings_path", lambda: path)
    monkeypatch.setattr(settings, "problem", "")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"testsignals_level": 20, "testsignals_bits": 17, "testsignals_kind": "sine"}),
                    encoding="utf-8")
    loaded = settings.load()
    assert loaded["testsignals_level"] == -20.0 and loaded["testsignals_bits"] == 16
    assert loaded["testsignals_kind"] == "tone"
    path.write_text(json.dumps({"testsignals_level": -40, "testsignals_bits": 24, "testsignals_kind": "jtest"}),
                    encoding="utf-8")
    loaded = settings.load()
    assert (loaded["testsignals_level"], loaded["testsignals_bits"], loaded["testsignals_kind"]) == (-40, 24, "jtest")


def test_every_level_the_test_signals_window_offers_is_a_level_settings_keep():
    from ironbark import testsignals

    check = settings._CHECKS["testsignals_level"]
    assert all(check(level) == level for level in testsignals.LEVEL_CHOICES)
    assert check(testsignals.LEVEL_DB) == testsignals.LEVEL_DB
    for bad in (0.0, -2.0, 20.0, -81.0, True, "-20", float("nan")):
        assert check(bad) is settings._BAD
    kinds = settings._CHECKS["testsignals_kind"]
    from ironbark.testsignals_window import KINDS

    assert all(kinds(kind) == kind for kind, _ in KINDS)


# -- '/' where it needs Shift ------------------------------------------------------------------------------
def test_slash_searches_on_keyboards_where_it_needs_shift():
    # German, French, Spanish and Nordic layouts type '/' with Shift: Tk reports "slash" with Shift held.
    found = keys.build_map()
    shifted = (frozenset({"Shift"}), "slash")
    assert [a.id for a in found[shifted]] == ["library_search", "search_tab"]
    assert [a.id for a in found[(frozenset(), "slash")]] == ["library_search", "search_tab"]
    assert [a.id for a in found[(frozenset({"Shift"}), "question")]] == ["help"]   # (US: Shift+/ is still ?)
    assert keys.normalise("slash", keys.SHIFT) == shifted
