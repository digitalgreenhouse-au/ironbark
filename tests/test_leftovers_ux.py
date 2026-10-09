"""Finishing touches: Tab round a prompt's buttons, the notice after a crash, asking before a
library folder is taken out, what the internet switches say they send, names left out of the
diagnostics, and the self-check's window keeping away from the user's music."""

from __future__ import annotations

import gc
import json
import re
import sys
import threading
from pathlib import Path

import pytest

from ironbark import crash, diagnostics, help_window, library_index, logs, selfcheck, settings, theme, ui
from ironbark.popups import Prompt
from test_light_mini import _pump, make_window  # noqa: F401  (make_window is a fixture)

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def root():
    tk = pytest.importorskip("tkinter")
    try:
        r = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"no display: {error}")
    r.geometry("420x520+60+60")
    theme.apply(r)
    errors = []
    r.report_callback_exception = lambda *info: errors.append(info)
    r.errors = errors
    _pump(r, 0.2)
    yield r
    for prompt in list(Prompt.stack):
        prompt.answer(None)
    r.destroy()
    del r
    gc.collect()   # on this thread (Tk objects freed on another thread abort Python)


def _has_keyboard(root, widget) -> bool:
    return str(root.tk.call("focus", "-lastfor", root)) == str(widget)


# -- prompts: Tab between the buttons --------------------------------------------------------------------
def test_tab_goes_round_a_prompts_buttons_and_enter_presses_the_one_it_is_on(root):
    prompt = Prompt(root, "Delete the playlist “Gym”?", buttons=(("Delete", True, "danger"), ("Keep", False)),
                    cancel=False)
    _pump(root, 0.1)
    delete, keep = prompt.buttons
    assert prompt.default == 1                                # Enter's safe answer: Keep
    prompt.tab(prompt, 1)                                     # the first Tab: the default button
    assert _has_keyboard(root, keep)
    prompt.tab(keep, 1)                                       # round to the start
    assert _has_keyboard(root, delete)
    prompt.tab(delete, -1)                                    # Shift+Tab: back
    assert _has_keyboard(root, keep)
    keep.event_generate("<FocusIn>")                          # (the app isn't the active one here)
    _pump(root, 0.05)
    assert keep.focused                                       # the green ring shows which has the keyboard
    # The keys themselves, through the prompts' own bindings.
    keep.focus_force()
    _pump(root, 0.05)
    keep.event_generate("<Tab>")
    _pump(root, 0.05)
    assert _has_keyboard(root, delete)
    delete.event_generate("<Shift-Tab>")
    _pump(root, 0.05)
    assert _has_keyboard(root, keep)
    delete.focus_set()
    delete._key_pressed(None)                                 # Enter on Delete: Delete, chosen on purpose
    assert prompt.done and prompt.result is True and not root.errors


def test_esc_still_cancels_and_enter_in_the_box_still_presses_the_default(root):
    prompt = Prompt(root, "Rename playlist", "New name:", entry="Mix", buttons=(("Rename", True), ("Cancel", None)))
    _pump(root, 0.1)
    assert prompt.focus_order() == [prompt.entry, *prompt.buttons]
    prompt.tab(prompt.entry, 1)                               # from the box: the first button
    assert _has_keyboard(root, prompt.buttons[0])
    prompt.entry.focus_force()
    _pump(root, 0.05)
    prompt.entry.event_generate("<Return>")
    _pump(root, 0.05)
    assert prompt.done and prompt.result == "Mix"
    other = Prompt(root, "Delete?", buttons=(("Delete", True, "danger"), ("Cancel", False)), cancel=False)
    _pump(root, 0.05)
    other.tab(other, 1)
    other.focus_force()
    _pump(root, 0.05)
    other.event_generate("<Escape>")
    _pump(root, 0.05)
    assert other.done and other.result is False and not root.errors


# -- after a crash -------------------------------------------------------------------------------------
CRASHED = (f"{crash.TRACKING}\n--- Ironbark 0.33.0 started 08/10/2026 09:00:00 ---\n"
           "Windows fatal exception: access violation\n\nThread 0x0000 (most recent call first):\n")


def test_the_log_says_whether_the_last_run_closed_unexpectedly():
    assert not crash.closed_unexpectedly("")
    old = "--- Ironbark 0.32.0 started 07/10/2026 10:00:00 ---\n10:00:05 error in the window code:\nboom\n"
    assert not crash.closed_unexpectedly(old) and not crash.tracks_exits(old)   # (before exits were noted)
    assert crash.closed_unexpectedly(CRASHED)
    clean = CRASHED + crash.clean_exit_line() + "\n\n--- Ironbark 0.33.0 started 08/10/2026 10:00:00 ---\n"
    assert not crash.closed_unexpectedly(clean)                # a crash before the last clean exit: old news
    assert crash.closed_unexpectedly(clean + "10:30:00 error in the tab-load thread:\nTraceback …\n")


def test_after_a_crash_the_next_start_says_so_once_with_a_link(make_window, monkeypatch):
    crash.note_last_run(CRASHED)
    window, root, player, errors = make_window()
    assert window._notice == ui.CRASH_NOTICE
    assert window.undo_link.winfo_manager() == "place" and window.undo_link.cget("text") == "Save diagnostics…"
    saved = []
    monkeypatch.setattr(window, "save_diagnostics", lambda destination=None: saved.append(destination))
    window._link_clicked()
    window._status_clicked()
    assert saved == [None, None]
    assert not crash.take_unexpected_exit()                   # said once
    second, root2, _player, errors2 = make_window()
    assert second._notice != ui.CRASH_NOTICE and not errors and not errors2


def test_a_clean_exit_is_noted_and_a_crash_after_it_is_found(tmp_path, monkeypatch):
    from ironbark import __main__ as start

    folder = tmp_path / "app"
    folder.mkdir()
    (folder / "crash.log").write_text("--- Ironbark 0.32.0 started 07/10/2026 10:00:00 ---\n"
                                      "10:00:05 error in the window code:\nboom\n", encoding="utf-8")
    monkeypatch.setattr(settings, "settings_path", lambda: folder / "settings.json")
    monkeypatch.setattr(start.faulthandler, "enable", lambda **kwargs: None)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)   # put back afterwards
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(start, "_crash_file", None)

    def run(then):
        start._start_crash_log()
        try:
            found = crash.take_unexpected_exit()
            then()
        finally:
            start._crash_file.close()
            start._crash_file = None
        return found

    try:
        assert run(start._note_clean_exit) is False           # an old log: noted from here, nothing said
        text = (folder / "crash.log").read_text(encoding="utf-8")
        assert crash.TRACKING in text and "closed normally" in text
        assert run(lambda: start._crash_file.write("10:00:00 error in the window code:\nTraceback …\n")) is False
        assert run(start._note_clean_exit) is True             # it crashed after that error: said
        assert run(lambda: None) is False                      # (closed normally last time)
    finally:
        logs.shutdown()


# -- Settings → Library → Remove asks first ----------------------------------------------------------------
def test_removing_a_library_folder_asks_first_and_keep_is_the_safe_answer(make_window, tmp_path):
    window, root, player, errors = make_window()
    first, other = tmp_path / "Music", tmp_path / "More"
    other.mkdir()
    window._set_library_folders([first, other])
    _pump(root, 0.3)
    seen = []

    def answer(how):
        def go():
            if not Prompt.stack:
                root.after(20, go)
                return
            box = Prompt.stack[-1]
            seen.append((box.heading, [b.text_value for b in box.buttons], box.default))
            how(box)
        root.after(50, go)

    answer(Prompt.press_default)                               # Enter: Keep
    window._remove_library_folder(other)
    answer(Prompt.press_cancel)                                # Esc: Keep
    window._remove_library_folder(other)
    assert window._index().roots == [first, other]
    assert seen[0] == ("Take “More” out of the Library?", ["Remove", "Keep"], 1)
    answer(lambda box: box._chosen(0))                         # Remove, clicked
    window._remove_library_folder(other)
    _pump(root, 0.3)
    assert window._index().roots == [first] and "Took" in window._notice and not errors


# -- what the internet switches say ---------------------------------------------------------------------
# -- diagnostics: names left out ------------------------------------------------------------------------
def test_diagnostics_leave_out_tab_playlist_and_output_names(tmp_path):
    folder = tmp_path / "Ironbark"
    folder.mkdir()
    (folder / "crash.log").write_text("error playing Abbey Road from Gym Mix on Headphones (Sam's AirPods)\n"
                                      "the Library window and the Help menu\n", encoding="utf-8")
    prefs = {**settings.DEFAULTS, "output_device": "Headphones (Sam's AirPods)",
             "testsignals_input": "Microphone (Sam's AirPods)", "last_filter": "beatles",
             "tabs": [{"kind": "library", "path": "D:\\Music", "query": "beatles"}]}
    files = diagnostics.contents(prefs, folder,
                                 outputs=lambda: ("Speakers (Realtek)", ["Speakers (Realtek)",
                                                                         "Headphones (Sam's AirPods)"]),
                                 home="C:\\Users\\Jane",
                                 names={"tab": ["Abbey Road", "Library"], "playlist": ["Gym Mix"],
                                        "search": ["beatles"]})
    every = "\n".join(files.values())
    assert "Sam" not in every and "Realtek" not in every and "Abbey Road" not in every and "Gym Mix" not in every
    assert "beatles" not in files["settings.json"]
    saved = json.loads(files["settings.json"])
    assert saved["output_device"] == "<device 2>" and saved["testsignals_input"] == "<device 3>"
    assert saved["tabs"][0]["query"] == "<search 1>" and saved["last_filter"] == "<search 1>"
    assert "error playing <tab 1> from <playlist 1> on <device 2>" in files["crash.log"]
    assert "the Library window and the Help menu" in files["crash.log"]   # (ordinary words left alone)
    outputs = files["audio-outputs.txt"]
    assert "Default output: <device 1>" in outputs and "Chosen in Settings: <device 2> (connected)" in outputs


def test_diagnostics_leave_out_a_network_servers_name(tmp_path):
    """A NAS is often named after someone, and the network playback cache's notes name the server on
    its own (in small letters), not only inside a folder's path."""
    folder = tmp_path / "Ironbark"
    folder.mkdir()
    (folder / "app.log").write_text("music cache: sams-nas isn't answering\n"
                                    "Preparing track… \\\\sams-nas isn't answering\n"
                                    "reading \\\\SAMS-NAS\\Music\\x.flac at frame 3\n", encoding="utf-8")
    prefs = {**settings.DEFAULTS, "library_folders": ["\\\\SAMS-NAS\\Music"]}
    files = diagnostics.contents(prefs, folder, outputs=lambda: (None, []), home="C:\\Users\\Jane")
    assert "sams" not in "\n".join(files.values()).lower()
    assert "music cache: <server 1> isn't answering" in files["app.log"]
    assert "reading <folder 1>\\x.flac" in files["app.log"]


def test_a_folder_named_like_the_start_of_another_keeps_its_own_name():
    redactor = diagnostics.Redactor(home="")
    redactor.value({"library_folder": "D:\\Music", "last_folder": "D:\\Music\\Abba"})
    text = redactor.text("couldn't read D:\\Music Videos\\clip.mkv\nD:\\Music Videos\n"
                         "D:\\Music\\Abba Gold\\01.flac\nD:\\Music\\Abba is gone. D:\\Music\\Abba.\n"
                         "D:\\Music\\Abba.flac\nD:\\Music\\Abba (missing)\n")
    assert "<folder 1> Videos" not in text
    assert "D:\\Music Videos\\clip.mkv" in text and "\nD:\\Music Videos\n" in text   # a folder of its own
    assert "<folder 1>\\Abba Gold\\01.flac" in text                                  # (inside the known one)
    assert "<folder 2> is gone." in text and " <folder 2>.\n" in text
    assert "<folder 1>\\Abba.flac" in text and "<folder 2> (missing)" in text


# -- the self-check's window ---------------------------------------------------------------------------
def test_the_self_checks_window_never_reads_the_users_music(monkeypatch):
    pytest.importorskip("tkinter")
    from ironbark.ui import PlayerWindow

    looked, started = [], []
    real = library_index.shared
    monkeypatch.setattr(library_index, "shared", lambda roots: looked.append(list(roots)) or real(roots))
    monkeypatch.setattr(PlayerWindow, "_start_cache", lambda self: started.append(self))
    # Even with Local network access on by default, the check window keeps the cache off.
    monkeypatch.setitem(settings.DEFAULTS, "local_network", True)
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
    assert looked and all("ironbark-selfcheck-" in str(root) for roots in looked for root in roots)
    assert started == []
