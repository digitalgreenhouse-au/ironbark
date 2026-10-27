"""Why a library folder won't load, in plain English (netcheck.py): the steps of the check, what they
add up to, the report, and the button in Settings → Library. The network is stood in for: the
steps' calls are replaced, so nothing here needs a server."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
from test_light_mini import _pump, make_window  # noqa: F401  (make_window is a fixture)

from ironbark import netcheck, places, winlisting


def _wait(root, ready, seconds: float = 15.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if ready():
            return True
        _pump(root, 0.05)
    return bool(ready())


def _error(code: int, text: str) -> OSError:
    error = OSError(22, text)
    error.winerror = code
    return error


def test_each_network_error_gets_plain_words_and_its_number():
    signature = _error(winlisting.BAD_SIGNATURE, "Invalid Signature")
    assert netcheck.words(signature) == "Invalid Signature, error -2146893818"
    assert "signature (SMB signing)" in netcheck.meaning(signature)
    unsigned = _error(winlisting.BAD_SIGNATURE & 0xFFFFFFFF, "Invalid Signature")
    assert netcheck.code_of(unsigned) == winlisting.BAD_SIGNATURE   # (however Windows gives the number)
    assert "asleep" in netcheck.meaning(_error(53, "The network path was not found"))
    assert "user name or password" in netcheck.meaning(_error(1326, "The user name or password is incorrect"))
    assert "red cross" in netcheck.meaning(_error(1201, "The device is not currently connected"))
    assert netcheck.meaning(_error(9999, "Something else")) == ""       # (nothing to add: its own words stand)
    assert netcheck.meaning(TimeoutError()) != ""


@pytest.fixture
def on_the_network(monkeypatch):
    """Every folder counts as a network share; Local network access decides."""
    monkeypatch.setattr(places, "is_remote", lambda path: True)


def _share_step(folder: Path) -> list[str]:
    """The share is tried first where a folder has a drive or \\\\server\\share of its own (Windows); a
    Mac or Linux mount has no root of its own to try, so the folder is the first step there."""
    return ["The share answers"] if os.path.splitdrive(str(folder))[0] else []


def test_a_folder_python_cant_list_but_explorer_can_is_readable_and_says_why(tmp_path, monkeypatch, on_the_network):
    (tmp_path / "Album").mkdir()
    (tmp_path / "Album" / "01.flac").write_bytes(b"")

    def refusing(path="."):
        raise _error(winlisting.BAD_SIGNATURE, "Invalid Signature")

    monkeypatch.setattr(netcheck, "_python_way", lambda folder: refusing())
    monkeypatch.setattr(winlisting, "list_basic", lambda folder: [object()] * 1846)
    result = netcheck.check(str(tmp_path), {"local_network": True}, target=lambda letter: r"\\nas\music")
    names = [step.name for step in result.steps]
    expected = _share_step(tmp_path) + ["The folder answers", "Python's way of listing"]
    expected += ["File Explorer's way of listing"] if os.name == "nt" else []
    assert names == expected + [netcheck.LIBRARY_GETS, "An album inside (“Album”)"]
    python = next(step for step in result.steps if step.name == "Python's way of listing")
    assert python.ok is False and "Invalid Signature, error -2146893818" in python.detail and "SMB signing" in python.detail
    assert result.readable and result.verdict.startswith("Readable: the Library gets 1 name.")
    if os.name == "nt":
        assert "File Explorer's works, and Ironbark uses File Explorer's way" in result.verdict
    album = result.steps[-1]
    assert album.name == "An album inside (“Album”)" and album.ok and album.detail == "1 name"


def test_when_every_way_of_listing_fails_the_verdict_says_what_the_library_gets(tmp_path, monkeypatch, on_the_network):
    def refusing(*args):
        raise _error(64, "The specified network name is no longer available")

    monkeypatch.setattr(netcheck, "_python_way", refusing)
    monkeypatch.setattr(netcheck, "_library_way", refusing)
    monkeypatch.setattr(winlisting, "list_basic", refusing)
    result = netcheck.check(str(tmp_path), {"local_network": True}, target=lambda letter: None)
    assert not result.readable
    assert result.verdict == ("Not readable now: The specified network name is no longer available, error 64 — "
                              "the connection to the server dropped while reading (the server went to sleep, or "
                              "the network blinked).")
    assert result.steps[-1].name == netcheck.LIBRARY_GETS              # (no album tried after it)


def test_a_server_that_doesnt_answer_is_said_so_within_the_time_limit(tmp_path, monkeypatch, on_the_network):
    held = threading.Event()
    monkeypatch.setattr(netcheck, "_look", lambda path: held.wait(5))   # (a server asleep: no answer)
    try:
        result = netcheck.check(str(tmp_path), {"local_network": True}, limit=0.3, target=lambda letter: None)
    finally:
        held.set()
    assert [step.ok for step in result.steps] == [None]                 # nothing tried after it
    first = (_share_step(tmp_path) or ["The folder answers"])[0]
    assert result.steps[0].line().startswith(f"NO ANSWER {first}")
    assert not result.readable and "no answer within 0 s" in result.verdict


def test_a_network_folder_isnt_looked_at_while_local_network_access_is_off(tmp_path, monkeypatch, on_the_network):
    monkeypatch.setattr(netcheck, "_timed", lambda *a, **k: pytest.fail("looked at"))
    result = netcheck.check(r"\\nas\music\Albums", {"local_network": False})
    assert result.steps == [] and "Local network access is off" in result.verdict


def test_a_folder_that_isnt_there_says_so(tmp_path):
    result = netcheck.check(str(tmp_path / "gone"), {})
    assert result.where == "this computer" and result.steps[0].name == "The folder answers"
    assert result.steps[0].ok is False and "isn't there" in result.steps[0].detail and not result.readable


def test_the_report_has_every_folder_and_a_summary(tmp_path, monkeypatch):
    music = tmp_path / "Music"
    (music / "Album").mkdir(parents=True)
    text, checks = netcheck.report([str(music), str(tmp_path / "gone")], {}, signing=lambda: True)
    assert str(music) in text and "Where: this computer" in text and "→ Readable" in text
    assert "Nothing was changed and nothing was sent" in text
    assert "SMB signing" not in text                                    # (only said for a signature error)
    assert netcheck.summary(checks).startswith(f"{tmp_path / 'gone'}: Not readable now")
    assert netcheck.summary(checks[:1]) == "All 1 library folder can be read"
    assert netcheck.summary([]) == "There are no library folders to check"


def test_python_s_way_says_how_far_it_got_before_it_failed(tmp_path, monkeypatch):
    class Part:
        def __init__(self):
            self.n = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def __iter__(self):
            return self

        def __next__(self):
            self.n += 1
            if self.n <= 2:
                return object()
            raise _error(winlisting.BAD_SIGNATURE, "Invalid Signature")

    monkeypatch.setattr(winlisting, "_scandir", lambda folder: Part())
    with pytest.raises(OSError) as raised:
        netcheck._python_way(str(tmp_path))
    assert raised.value.given == 2 and netcheck.code_of(raised.value) == winlisting.BAD_SIGNATURE


def test_settings_check_folders_saves_the_report_opens_it_and_says_how_it_went(make_window, tmp_path, monkeypatch):
    from ironbark import netcheck_view

    window, root, _player, errors = make_window()
    opened = []
    monkeypatch.setattr(netcheck_view, "open_file", lambda path: opened.append(Path(path)) or True)
    window._open_settings()
    _pump(root, 0.3)
    window.check_folders_button.command()
    assert _wait(root, lambda: opened, 20), window._notice
    report = opened[0]
    assert report.name == netcheck_view.REPORT_NAME and report.is_file()
    assert str(tmp_path / "Music") in report.read_text(encoding="utf-8")
    assert window._notice.startswith("All 1 library folder can be read · the report is open")
    assert not errors


def test_save_diagnostics_has_the_folders_check_with_the_names_left_out(tmp_path):
    from ironbark import diagnostics

    music = tmp_path / "Private Music"
    music.mkdir()
    prefs = {"library_folders": [str(music)], "library_folder": str(music)}
    files = diagnostics.contents(prefs, tmp_path, outputs=lambda: (None, []), home=str(tmp_path / "home"),
                                 network=lambda letter: None,
                                 folders_check=lambda: netcheck.report([str(music)], prefs)[0])
    text = files["library-folders-check.txt"]
    assert "Private Music" not in text and "<folder 1>" in text and "→ Readable" in text
    assert "library-folders-check.txt" in files["README.txt"]


def test_a_mac_or_linux_error_is_explained_by_its_own_errno_not_by_windows_numbers():
    import errno

    dropped = OSError(errno.EIO, "Input/output error")
    assert "login" not in netcheck.meaning(dropped) and "stopped answering" in netcheck.meaning(dropped)
    assert "login may not read" in netcheck.meaning(OSError(errno.EACCES, "Permission denied"))
    assert "isn't there" in netcheck.meaning(OSError(errno.ENOENT, "No such file or directory"))
    assert netcheck.meaning(OSError(errno.ESRCH, "No such process")) == ""   # (Windows' 3 is "path not found")
    assert "login may not read" in netcheck.meaning(_error(5, "Access is denied"))   # (Windows' 5 still is)


def test_save_diagnostics_leaves_the_albums_name_out_of_the_folders_check(make_window, tmp_path):
    import zipfile

    window, root, _player, errors = make_window()
    music = tmp_path / "My library"   # (not "Music": the test window has one of its own)
    (music / "Sam Smith - In The Lonely Hour").mkdir(parents=True)
    (music / "Sam Smith - In The Lonely Hour" / "01.flac").write_bytes(b"")
    window.prefs["library_folders"] = [str(music)]
    window.prefs["library_folder"] = str(music)
    destination = tmp_path / "diagnostics.zip"
    window.save_diagnostics(str(destination))
    assert _wait(root, lambda: window._notice.startswith("Diagnostics saved"), 30), window._notice
    with zipfile.ZipFile(destination) as archive:
        text = archive.read("library-folders-check.txt").decode("utf-8")
    assert "An album inside" in text and "Lonely Hour" not in text
    assert not errors
    # (Check folders… — a report kept on this PC — still names it: what was looked inside)
    named = netcheck.report([str(music)], window.prefs)[0]
    assert "An album inside (“Sam Smith - In The Lonely Hour”)" in named
