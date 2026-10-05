"""0.29.0: `ironbark --self-check` — the check the packaged builds run on the built program."""

from __future__ import annotations

import gc

import pytest

from ironbark import __main__ as main_module
from ironbark import selfcheck, settings, theme


def test_sound_file_check_round_trips_flac_bit_for_bit():
    assert "bit for bit" in selfcheck._check_sndfile()


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


def test_command_line_option_exits_with_the_checks_result(monkeypatch):
    seen = []
    monkeypatch.setattr(selfcheck, "run", lambda report=None: seen.append(report) or 3)
    with pytest.raises(SystemExit) as stop:
        main_module.main(["--self-check", "out.txt"])
    assert stop.value.code == 3 and seen == ["out.txt"]
    with pytest.raises(SystemExit):
        main_module.main(["--self-check"])
    assert seen[-1] is None
