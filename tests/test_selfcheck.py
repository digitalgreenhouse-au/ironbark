"""0.29.0: `ironbark --self-check` — the check the packaged builds run on the built program."""

from __future__ import annotations

import gc
import sys

import pytest

from ironbark import __main__ as main_module
from ironbark import selfcheck, settings, theme


def test_sound_file_check_round_trips_flac_bit_for_bit():
    assert "bit for bit" in selfcheck._check_sndfile()


def test_library_database_check_makes_a_database_and_searches_it():
    detail = selfcheck._check_library_db()
    assert "WAL" in detail and "FTS5" in detail


def test_icon_check_finds_every_size():
    assert "ironbark.ico" in selfcheck._check_icons()


def test_a_failed_check_is_reported_and_fails_the_run(tmp_path, monkeypatch, capsys):
    def broken():
        raise FileNotFoundError("icon_16.png")

    monkeypatch.setattr(selfcheck, "_check_icons", broken)
    monkeypatch.setattr(selfcheck, "_check_window", lambda: "skipped here")
    report = tmp_path / "report.txt"
    assert selfcheck.run(str(report)) == 1
    text = report.read_text(encoding="utf-8")
    assert "FAIL  icon files: FileNotFoundError('icon_16.png')" in text
    assert "OK    libsndfile" in text            # the other checks still ran
    assert text.rstrip().endswith("FAILED")
    assert capsys.readouterr().out == text


def test_the_window_check_leaves_the_settings_alone(tmp_path, monkeypatch):
    pytest.importorskip("tkinter")
    real = tmp_path / "real" / "settings.json"
    monkeypatch.setattr(settings, "settings_path", lambda: real)
    # As in the other window tests: no media-controls or new-music threads, which would outlive
    # the window inside the test run; and no real library folder.
    for key, value in dict(media_controls=False, watch_music=False, clip_guard=False,
                           library_folder=str(tmp_path / "Music")).items():
        monkeypatch.setitem(settings.DEFAULTS, key, value)
    try:
        detail = selfcheck._check_window()
    except Exception as error:
        if "display" in str(error).lower() or "TclError" in type(error).__name__:
            pytest.skip(f"no display: {error}")
        raise
    finally:
        theme.use(theme.DARK)
        gc.collect()
    assert "opened and closed" in detail
    assert settings.settings_path() == real      # put back afterwards
    assert not real.parent.exists()              # nothing written where the user's settings live


def test_the_window_check_registers_nothing_system_wide(monkeypatch):
    """With the player's own defaults (media keys and new-music watching on), the check window
    still never hands itself to Windows' media panel / taskbar / a keyboard hook."""
    pytest.importorskip("tkinter")
    from ironbark import mediakeys

    started = []
    monkeypatch.setattr(mediakeys, "start_windows", lambda hub, hwnd: started.append(hwnd))
    assert settings.DEFAULTS["media_controls"] is True and settings.DEFAULTS["watch_music"] is True
    try:
        selfcheck._check_window()
    except Exception as error:
        if "display" in str(error).lower() or "TclError" in type(error).__name__:
            pytest.skip(f"no display: {error}")
        raise
    finally:
        theme.use(theme.DARK)
        gc.collect()
    assert started == []


def test_command_line_option_exits_with_the_checks_result(monkeypatch):
    seen = []
    monkeypatch.setattr(selfcheck, "run", lambda report=None, hidden=False: seen.append((report, hidden)) or 3)
    with pytest.raises(SystemExit) as stop:
        main_module.main(["--self-check", "out.txt"])
    assert stop.value.code == 3 and seen == [("out.txt", False)]   # (its window on the normal desktop)
    with pytest.raises(SystemExit):
        main_module.main(["--self-check"])
    assert seen[-1] == (None, False)


def test_licence_check_finds_the_summary_first():
    assert "About -> Licences" in selfcheck._check_licences()


def test_licence_check_fails_when_none_are_found(monkeypatch):
    from ironbark import help_window

    monkeypatch.setattr(help_window, "licence_documents", lambda: [])
    with pytest.raises(FileNotFoundError, match="no licence files"):
        selfcheck._check_licences()


def test_a_hidden_desktop_is_only_used_when_this_thread_has_no_window_yet(monkeypatch):
    # Inside the test run the main thread already owns windows, so the move is refused and the
    # check carries on where it is (nothing breaks); off Windows it's never tried.
    if sys.platform != "win32":
        assert selfcheck._use_hidden_desktop() is False
        return
    import tkinter as tk

    root = tk.Tk()   # (makes sure this thread owns a window)
    try:
        assert selfcheck._use_hidden_desktop() is False
    finally:
        root.destroy()


def test_the_pieces_that_fail_quietly_in_the_player_are_checked(tmp_path, monkeypatch):
    # Windows' audio list, media controls and secure connections are switched off quietly in the
    # player when they're missing, so only the self-check would notice them gone from a build.
    assert "root certificates" in selfcheck._check_network()
    if sys.platform == "win32":
        assert "pycaw" in selfcheck._check_windows_audio()
        assert "taskbar buttons" in selfcheck._check_media_controls()
    monkeypatch.setattr(selfcheck, "_check_window", lambda: "skipped here")
    report = tmp_path / "report.txt"
    selfcheck.run(str(report))
    text = report.read_text(encoding="utf-8")
    for name in ("Windows audio", "media controls", "secure connections"):
        assert f"OK    {name}: " in text


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_a_broken_windows_audio_list_fails_the_check(monkeypatch):
    from ironbark import devices

    def broken():
        raise OSError("pycaw's COM interfaces didn't load")

    monkeypatch.setattr(devices, "_active_outputs", broken)
    with pytest.raises(OSError, match="didn't load"):
        selfcheck._check_windows_audio()
