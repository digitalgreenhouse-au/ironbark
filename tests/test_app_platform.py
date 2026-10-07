"""The app platform: one player at a time (a later launch hands its files over), files named on
the command line or through "Open with", the crash box, diagnostics, the rotating log, the settings
file's format, the opt-in update check, and Python's cycle collector kept on Tk's thread."""

from __future__ import annotations

import gc
import io
import json
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from ironbark import (APP_NAME, __version__, crash, diagnostics, dragdrop, instance, logs, settings, tag_editor,
                      tkgc, updates)
from ironbark import __main__ as start
from ironbark.engine import State
from ironbark.ui import PlayerWindow, Tab

from test_dragdrop import flac, music, touch  # noqa: F401  (music is a fixture)
from test_light_mini import _pump, make_window  # noqa: F401  (make_window is a fixture)

REAL_NATIVE_BOX = crash.native_box     # (the shared set-up swaps these for stand-ins in every test)
REAL_DEFAULT_KEY = instance.default_key
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def unique_key() -> str:
    return f"IronbarkTest-{uuid.uuid4().hex[:12]}"


# -- one player at a time ------------------------------------------------------------------------------
@windows_only
def test_a_named_mutex_tells_a_second_launch_one_is_running():
    name = instance.mutex_name(unique_key())
    first, existed = instance.create_mutex(name)
    second, existed_then = instance.create_mutex(name)
    try:
        assert existed is False and existed_then is True
    finally:
        instance._close_handle(second)
        instance._close_handle(first)
    again, existed = instance.create_mutex(name)   # gone with its last handle: a new player may start
    instance._close_handle(again)
    assert existed is False


@windows_only
def test_a_later_launch_hands_its_files_over_the_pipe():
    key = unique_key()
    running = instance.claim(key)
    later = instance.claim(key)
    try:
        assert running.first and not later.first
        assert later.forward([r"C:\Music\Album\01 One.flac", r"D:\Music"], front=True, timeout=5)
        assert running.requests.get(timeout=5) == instance.Request((r"C:\Music\Album\01 One.flac", r"D:\Music"), True)
        assert later.forward([], front=True, timeout=5)    # (a Start-menu click: just come forward)
        assert running.requests.get(timeout=5) == instance.Request((), True)
    finally:
        running.close()
    assert not later.forward(["x"], timeout=0.3)   # nobody listening: a quick, quiet False
    after = instance.claim(key)
    after.close()
    assert after.first   # once the running copy has closed, the next launch is the player


def test_the_socket_way_works_the_same(tmp_path):
    key = unique_key()
    running = instance.claim_with_socket(key, tmp_path)
    later = instance.claim_with_socket(key, tmp_path)
    try:
        assert running.first and not later.first
        assert later.forward(["/music/a.flac"], front=False, timeout=5)
        assert running.requests.get(timeout=5) == instance.Request(("/music/a.flac",), False)
    finally:
        running.close()
    assert not (tmp_path / f"{key}.json").exists()   # its port and key go with it
    after = instance.claim_with_socket(key, tmp_path)
    assert after.first
    after.close()


@windows_only
def test_a_launch_while_the_player_is_closing_waits_for_it_then_takes_its_place():
    key = unique_key()
    running = instance.claim(key)
    running.stop_listening()                        # its window has started closing…
    later = instance.claim(key)
    assert running.first and not later.first
    threading.Timer(0.5, running.close).start()     # …and it's gone half a second later
    began = time.monotonic()
    assert later.forward(["a.flac"], timeout=5) is False   # never handed to a window that's closing
    assert time.monotonic() - began < 3                    # nor kept waiting the whole time
    assert running.requests.empty()
    after = instance.claim(key)
    after.close()
    assert after.first


def test_the_socket_way_waits_for_a_closing_copy_too(tmp_path):
    key = unique_key()
    running = instance.claim_with_socket(key, tmp_path)
    running.stop_listening()
    later = instance.claim_with_socket(key, tmp_path)
    assert not later.first
    threading.Timer(0.5, running.close).start()
    began = time.monotonic()
    assert later.forward(["a.flac"], timeout=5) is False and time.monotonic() - began < 3
    assert running.requests.empty()
    after = instance.claim_with_socket(key, tmp_path)
    assert after.first
    after.close()


@windows_only
def test_a_copy_that_takes_the_call_but_never_answers_doesnt_hold_the_launch(monkeypatch):
    key = unique_key()
    release = threading.Event()

    def hung(self, size):   # the running copy connects, then never reads or answers
        release.wait(10)
        return b""
    monkeypatch.setattr(instance.PipeServer, "_read", hung)
    running = instance.claim(key)
    later = instance.claim(key)
    try:
        began = time.monotonic()
        assert later.forward(["a.flac"], timeout=0.5) is False
        assert time.monotonic() - began < instance.ANSWER_WAIT_S + 2
    finally:
        release.set()
        running.close()


def test_the_self_check_proves_one_player_at_a_time_under_a_name_of_its_own(monkeypatch):
    from ironbark import selfcheck

    monkeypatch.setattr(instance, "default_key", lambda: pytest.fail("used the real player's name"))
    assert selfcheck._check_single_instance() in ("named mutex + pipe", "lock file + local socket")


def test_requests_without_the_key_or_in_bad_shape_are_ignored():
    body = instance.encode(["a"], key="secret")[4:]
    assert instance.decode(body, "secret") == instance.Request(("a",), True)
    assert instance.decode(body, "other") is None
    for junk in (b"", b"\xff\xfe", b"[1, 2]", b'{"paths": "C:\\\\x"}', b"{" * 5000):
        assert instance.decode(junk) is None
    assert instance.decode(b'{"paths": ["a", 3, "", null]}') == instance.Request(("a",), True)
    long = io.BytesIO((instance.MAX_REQUEST + 1).to_bytes(4, "little"))
    assert instance._read_frame(long.read) is None   # an absurd length is refused before reading


def test_paths_are_made_absolute_where_the_later_launch_started(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert instance.absolute(["song.flac", "", str(tmp_path / "x")]) == [str(tmp_path / "song.flac"),
                                                                          str(tmp_path / "x")]


def test_the_name_follows_the_settings_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "a" / "settings.json")
    one = REAL_DEFAULT_KEY()
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "b" / "settings.json")
    assert REAL_DEFAULT_KEY() != one and one.startswith(f"{APP_NAME}-")


# -- the command line ------------------------------------------------------------------------------
class FakeInstance:
    def __init__(self, first, answers=True):
        self.first, self.answers = first, answers
        self.requests, self.sent, self.closed = queue.Queue(), [], False

    def forward(self, paths, front=True, timeout=0):
        self.sent.append(list(paths))
        return self.answers

    def stop_listening(self):
        pass

    def close(self):
        self.closed = True


def test_a_second_launch_forwards_its_paths_and_starts_nothing(monkeypatch, tmp_path):
    later = FakeInstance(first=False)
    monkeypatch.setattr(instance, "claim", lambda *a, **k: later)
    monkeypatch.setattr(start, "_run", lambda *a: pytest.fail("a second player was started"))
    monkeypatch.chdir(tmp_path)
    start.main(["song.flac", str(tmp_path / "Album")])
    assert later.sent == [[str(tmp_path / "song.flac"), str(tmp_path / "Album")]]


def test_a_second_launch_never_loads_the_player_or_the_library_database(tmp_path):
    # It hands its files over and ends before the engine, the window or the library database is
    # even imported, so it can't open library.db or start a look at the library alongside the
    # running copy. Run as a program of its own, as Explorer would start it.
    import subprocess

    import textwrap

    script = textwrap.dedent("""
        import sys
        from ironbark import __main__ as start, instance

        class Running:
            first = False

            def forward(self, paths, front=True, timeout=0):
                return True

        instance.claim = lambda *a, **k: Running()
        start.main(["song.flac"])
        loaded = [m for m in ("sqlite3", "numpy", "ironbark.library_db", "ironbark.indexer",
                              "ironbark.library_index", "ironbark.engine", "ironbark.ui") if m in sys.modules]
        print("LOADED", loaded)
        """)
    env = dict(os.environ, APPDATA=str(tmp_path), LOCALAPPDATA=str(tmp_path))
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60, env=env,
                          cwd=str(tmp_path))
    assert done.returncode == 0, done.stderr
    assert "LOADED []" in done.stdout, done.stdout
    assert not any(tmp_path.rglob("library.db*"))


def test_the_first_launch_opens_the_window_with_its_paths(monkeypatch, tmp_path):
    first = FakeInstance(first=True)
    ran = []
    monkeypatch.setattr(instance, "claim", lambda *a, **k: first)
    monkeypatch.setattr(start, "_run", lambda paths, here: ran.append((paths, here)))
    start.main([str(tmp_path / "a.flac")])
    assert ran == [([str(tmp_path / "a.flac")], first)] and first.closed


def test_a_copy_that_closed_while_another_launch_took_its_place_gets_the_files(monkeypatch):
    # Several files opened just as the player was closing: one of the launches becomes the player,
    # and the others hand their files to it (no "isn't answering").
    closing, new = FakeInstance(first=False, answers=False), FakeInstance(first=False, answers=True)
    claims = iter([closing, new])
    monkeypatch.setattr(instance, "claim", lambda *a, **k: next(claims))
    monkeypatch.setattr(crash, "tell_native", lambda *a, **k: pytest.fail("said it isn't answering"))
    monkeypatch.setattr(start, "_run", lambda *a: pytest.fail("a second player was started"))
    start.main([r"C:\a.flac"])
    assert closing.sent == new.sent == [[r"C:\a.flac"]]


def test_a_running_copy_that_doesnt_answer_is_reported(monkeypatch):
    stuck = FakeInstance(first=False, answers=False)
    told = []
    monkeypatch.setattr(instance, "FORWARD_TIMEOUT", 0.3)   # (each real try waits this long itself)
    monkeypatch.setattr(instance, "claim", lambda *a, **k: stuck)
    monkeypatch.setattr(crash, "tell_native", lambda title, text, warning=True: told.append(title))
    with pytest.raises(SystemExit) as stop:
        start.main([])
    assert stop.value.code == 1 and "answering" in told[0]


def test_self_check_never_looks_for_a_running_player(monkeypatch):
    monkeypatch.setattr(instance, "claim", lambda *a, **k: pytest.fail("looked for a running player"))
    import ironbark.selfcheck as selfcheck

    monkeypatch.setattr(selfcheck, "run", lambda report, hidden=False: 0)
    with pytest.raises(SystemExit) as stop:
        start.main(["--self-check"])
    assert stop.value.code == 0


def test_version_and_help_show_in_a_box_without_a_console(monkeypatch):
    told = []
    monkeypatch.setattr(crash, "tell_native", lambda title, text, warning=True: told.append(text))
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    with pytest.raises(SystemExit) as stop:
        start.main(["--version"])
    assert stop.value.code == 0 and told == [f"{APP_NAME} {__version__}"]
    with pytest.raises(SystemExit):
        start.main(["--help"])
    assert "PATH" in told[1]
    with pytest.raises(SystemExit) as stop:
        start.main(["--no-such-option"])
    assert stop.value.code == 2 and "--no-such-option" in told[2] and len(told) == 3


# -- what was named opens like a drop; a file asked for plays ----------------------------------------
def window_stand_in(state=State.PLAYING):
    opened = []
    w = SimpleNamespace(player=SimpleNamespace(status=lambda: SimpleNamespace(state=state)),
                        _asked_for=PlayerWindow._asked_for,
                        open_tab=lambda kind, path, play=False, interrupt=False: opened.append(
                            (kind, Path(path).name, play, interrupt)),
                        open_files_tab=lambda files, play=False: opened.append(("files", len(files), play)))
    return w, opened


def test_a_named_file_plays_even_over_music_playing(music):
    w, opened = window_stand_in(State.PLAYING)
    plan = dragdrop.plan_drop([music / "Album", music / "Loose" / "a.flac"])
    assert PlayerWindow._open_dropped(w, plan, asked=True) == "Opened 2 tabs"
    assert opened == [("folder", "Album", False, True), ("files", 1, True)]


def test_a_named_playlist_or_cue_sheet_plays_and_folders_alone_wait(music):
    w, opened = window_stand_in(State.PLAYING)
    PlayerWindow._open_dropped(w, dragdrop.plan_drop([music / "Loose" / "Mix.M3U8"]), asked=True)
    assert opened == [("playlist", "Mix.M3U8", True, True)]
    opened.clear()
    PlayerWindow._open_dropped(w, dragdrop.plan_drop([music / "Album" / "Album.cue"]), asked=True)
    assert opened == [("folder", "Album", True, True)]
    opened.clear()
    PlayerWindow._open_dropped(w, dragdrop.plan_drop([music / "Album"]), asked=True)
    assert opened == [("folder", "Album", False, False)]   # a folder doesn't stop the music
    w, opened = window_stand_in(State.STOPPED)
    PlayerWindow._open_dropped(w, dragdrop.plan_drop([music / "Album"]), asked=True)
    assert opened == [("folder", "Album", True, False)]    # …but plays when nothing else is


def test_the_same_files_named_again_go_back_to_their_tab(music):
    files = [music / "Loose" / "a.flac"]
    tab = Tab("files", str(music / "Loose"))
    tab.extras = [str(files[0])]
    chosen = []
    w = SimpleNamespace(tabs=[Tab("folder", str(music)), tab],
                        select_tab=lambda i, then_play=False: chosen.append((i, then_play)))
    PlayerWindow.open_files_tab(w, files, play=True)
    assert chosen == [(1, True)] and len(w.tabs) == 2


def test_requests_that_arrive_together_open_together():
    requests = queue.Queue()
    for paths in (("C:\\a.flac",), ("C:\\b.flac",), ()):
        requests.put(instance.Request(paths, True))
    calls = []
    w = SimpleNamespace(_requests=requests, _closing=False, _named=[], bring_to_front=lambda: calls.append("front"),
                        open_paths=lambda paths: calls.append(list(paths)))
    PlayerWindow._take_requests(w)
    assert calls == ["front"]                       # at once: the window comes forward…
    PlayerWindow._take_requests(w)
    assert calls == ["front", ["C:\\a.flac", "C:\\b.flac"]]   # …and once nothing more comes, one tab
    PlayerWindow._take_requests(w)
    assert calls == ["front", ["C:\\a.flac", "C:\\b.flac"]]


def test_launches_a_moment_apart_and_the_first_ones_own_files_open_as_one():
    # Several files chosen in Explorer with no player open: one launch becomes the player, the
    # others hand theirs over while its window is still being built, or a poll or two later.
    requests = queue.Queue()
    calls = []
    w = SimpleNamespace(_requests=requests, _closing=False, _named=[("C:\\Album\\03.flac",)],
                        bring_to_front=lambda: None, open_paths=lambda paths: calls.append(list(paths)))
    requests.put(instance.Request(("C:\\Album\\02.flac",), True))
    PlayerWindow._take_requests(w)
    requests.put(instance.Request(("C:\\Album\\01.flac",), True))
    PlayerWindow._take_requests(w)
    assert calls == []
    PlayerWindow._take_requests(w)
    assert calls == [["C:\\Album\\01.flac", "C:\\Album\\02.flac", "C:\\Album\\03.flac"]]   # in name order
    w._named = [("C:\\b.flac", "C:\\a.flac")]   # one launch's own list keeps its order
    PlayerWindow._take_requests(w)
    assert calls[-1] == ["C:\\b.flac", "C:\\a.flac"]


def test_the_player_stops_listening_as_soon_as_it_starts_closing(make_window):
    window, root, player, errors = make_window()
    stopped, opened = [], []
    window._on_close = lambda: stopped.append(window._closing)
    window._requests = queue.Queue()
    window.open_paths = opened.append
    window.quit()
    assert stopped == [True]
    window._requests.put(instance.Request(("C:\\a.flac",), True))   # (taken just before it stopped)
    window._take_requests()
    assert opened == [] and errors == []


def test_the_window_is_told_how_to_stop_listening(monkeypatch):
    made = []
    monkeypatch.setattr(start, "_start_crash_log", lambda: None)
    monkeypatch.setattr(start.settings, "carry_over_old_folder", lambda: False)
    monkeypatch.setattr(start, "_enable_crisp_text_on_windows", lambda: None)
    monkeypatch.setattr(start.appicon, "set_app_id", lambda: None)
    monkeypatch.setattr(start.tk, "Tk", lambda: SimpleNamespace(report_callback_exception=None, mainloop=lambda: None))
    monkeypatch.setattr(start, "Player", lambda: None)
    monkeypatch.setattr(start, "PlayerWindow", lambda root, player, **kwargs: made.append(kwargs) or SimpleNamespace())
    monkeypatch.setattr(crash, "attach", lambda root, before=None: None)
    monkeypatch.setattr(start, "_sweep_tag_copies", lambda: None)
    here = FakeInstance(first=True)
    start._run(["C:\\a.flac"], here)
    assert made[0]["on_close"] == here.stop_listening and made[0]["requests"] is here.requests
    assert made[0]["paths"] == ["C:\\a.flac"]


def test_coming_to_the_front_from_the_taskbar():
    calls = []
    root = SimpleNamespace(state=lambda: "iconic", deiconify=lambda: calls.append("deiconify"),
                           lift=lambda: calls.append("lift"), focus_force=lambda: calls.append("focus"),
                           attributes=lambda *a: calls.append(a), after_idle=lambda fn: calls.append("later") or fn())
    w = SimpleNamespace(root=root, on_top=SimpleNamespace(get=lambda: False))
    PlayerWindow.bring_to_front(w)
    assert calls == ["deiconify", "lift", ("-topmost", True), "later", ("-topmost", False), "focus"]


def test_paths_open_in_the_real_window_and_a_later_launch_is_heard(make_window, tmp_path):
    window, root, player, errors = make_window()
    song = tmp_path / "Music" / "Album" / "01 Song.flac"
    window._requests = queue.Queue()
    window._requests.put(instance.Request((str(song),), False))   # (front=False: no focus taken in a test)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any(t.kind == "files" for t in window.tabs):
        _pump(root, 0.1)
    files = [t for t in window.tabs if t.kind == "files"]
    assert files and files[0].extras == [str(song)] and window.current == window.tabs.index(files[0])
    assert window.collector.running and not gc.isenabled()   # the collector runs on Tk's thread now
    assert errors == []


# -- the crash box ---------------------------------------------------------------------------------
def error_info(message="boom"):
    try:
        raise RuntimeError(message)
    except RuntimeError:
        return sys.exc_info()


def test_the_box_is_shown_at_most_once_a_kind_and_never_in_a_stream():
    first = crash.describe("in the window code", *error_info())
    assert crash.may_show(first, now=1000.0)
    assert not crash.may_show(first, now=5000.0)                 # the same kind: once a session
    other = crash.Report("x", "", ("KeyError", ("ui.py", 1)))
    assert not crash.may_show(other, now=1000.0 + crash.MIN_GAP_S - 1)   # too soon after the last
    assert crash.may_show(other, now=1000.0 + crash.MIN_GAP_S)
    for n in range(10):
        crash.may_show(crash.Report("x", "", ("E", n)), now=10000.0 + n * crash.MIN_GAP_S)
    assert crash._shown == crash.MAX_BOXES


def test_startup_failure_uses_windows_box_and_its_buttons_work(monkeypatch, tmp_path):
    answers = iter([crash.COPY, crash.FOLDER, crash.CLOSE])
    shown, copied, opened = [], [], []
    monkeypatch.setattr(crash, "native_box", lambda title, text: (shown.append((title, text)), next(answers))[1])
    monkeypatch.setattr(crash, "copy_text", lambda text, root=None: copied.append(text) or True)
    monkeypatch.setattr(crash, "open_folder", lambda folder: opened.append(folder) or True)
    crash.startup_failed(*error_info("the window couldn't be built"), folder=tmp_path)
    assert len(shown) == 3 and shown[0][0] == f"{APP_NAME} couldn't start"
    assert "RuntimeError: the window couldn't be built" in shown[0][1]
    assert "copied" in shown[1][1]                                  # the box comes back saying so
    assert "Traceback" in copied[0] and __version__ in copied[0] and opened == [tmp_path]


@windows_only
def test_the_windows_box_names_its_buttons_or_says_which_is_which(monkeypatch):
    shown = []

    def fake(hook_handle):
        user32 = SimpleNamespace(
            SetWindowsHookExW=lambda *a: hook_handle, CallNextHookEx=lambda *a: 0, UnhookWindowsHookEx=lambda *a: 1,
            MessageBoxW=lambda owner, text, title, flags: (shown.append((text, flags)), crash.IDNO)[1])
        return user32, SimpleNamespace(GetCurrentThreadId=lambda: 1)

    monkeypatch.setattr(crash, "_dlls", lambda: fake(1234))
    assert REAL_NATIVE_BOX("t", "text") == crash.FOLDER
    assert shown[-1][0] == "text" and shown[-1][1] & 0xF == crash.MB_YESNOCANCEL
    monkeypatch.setattr(crash, "_dlls", lambda: fake(0))   # no hook: the buttons keep Windows' names
    assert REAL_NATIVE_BOX("t", "text") == crash.FOLDER
    assert "Yes: copy the details" in shown[-1][0]


def test_a_start_up_failure_shows_the_box_after_closing_the_half_built_window(monkeypatch):
    events = []
    monkeypatch.setattr(start, "_crash_file", io.StringIO())
    monkeypatch.setattr(start, "_start_crash_log", lambda: None)
    monkeypatch.setattr(start.settings, "carry_over_old_folder", lambda: False)
    monkeypatch.setattr(start, "_enable_crisp_text_on_windows", lambda: None)
    monkeypatch.setattr(start.appicon, "set_app_id", lambda: None)
    monkeypatch.setattr(start.tk, "Tk", lambda: SimpleNamespace(report_callback_exception=None,
                                                                destroy=lambda: events.append("closed")))
    monkeypatch.setattr(start, "Player", lambda: None)

    def broken(*args, **kwargs):
        raise RuntimeError("no window")
    monkeypatch.setattr(start, "PlayerWindow", broken)
    monkeypatch.setattr(crash, "startup_failed", lambda kind, value, tb: events.append(f"box: {value}"))
    with pytest.raises(RuntimeError):
        start.main([])
    assert events == ["closed", "box: no window"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)   # the packaged program: no second, raw box
    events.clear()
    with pytest.raises(SystemExit) as stop:
        start.main([])
    assert stop.value.code == 1 and events == ["closed", "box: no window"]


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"no display: {error}")
    r.geometry("420x520+60+60")
    from ironbark import theme

    theme.apply(r)
    _pump(r, 0.1)
    yield r
    from ironbark.popups import Prompt

    for prompt in list(Prompt.stack):
        prompt.answer(None)
    try:
        r.destroy()
    except tk.TclError:   # (a test closed it itself)
        pass
    del r
    gc.collect()   # here, on the main thread (Tk objects freed on another thread abort Python)


def test_errors_on_other_threads_reach_the_window_as_one_box(root, monkeypatch, tmp_path):
    from ironbark.popups import Prompt

    opened, copied = [], []
    monkeypatch.setattr(crash, "open_folder", lambda folder: opened.append(folder) or True)
    monkeypatch.setattr(crash, "copy_text", lambda text, window=None: copied.append((text, window)) or True)
    crash.attach(root, folder=lambda: tmp_path)
    worker = threading.Thread(target=lambda: crash.report("in the cover-art thread", *error_info("lost")))
    worker.start()
    worker.join()
    crash.report("in the cover-art thread", *error_info("lost"))   # the same kind again: no second box
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not Prompt.stack:
        _pump(root, 0.1)
    assert len(Prompt.stack) == 1
    box = Prompt.stack[0]
    assert box.heading == crash.TITLE
    box._chosen(0)                       # Copy details: copied (the real clipboard is left alone here), box stays
    assert "RuntimeError: lost" in copied[0][0] and copied[0][1] is root and Prompt.stack == [box]
    assert box.buttons[0].text_value == "Copied"
    box._chosen(1)                       # Open log folder
    assert opened == [tmp_path] and Prompt.stack == [box]
    box.press_default()                  # Close
    assert Prompt.stack == [] and not crash._open
    _pump(root, 1.2)
    assert Prompt.stack == []


def test_the_box_waits_while_the_window_is_minimised(root, tmp_path, monkeypatch):
    # It holds every click in the program: hidden in a minimised window, it would leave the program's
    # other windows (the Library, say) not answering, with nothing to show why.
    from ironbark.popups import Prompt

    copied = []
    monkeypatch.setattr(crash, "copy_text", lambda text, window=None: copied.append(text) or True)
    crash.attach(root, folder=lambda: tmp_path)
    root.iconify()
    _pump(root, 0.2)
    for n in range(50):   # (one kind: the same line, again and again)
        crash.report("in the cover-art thread", *error_info(f"lost {n}"))
    _pump(root, 1.2)
    assert Prompt.stack == [] and crash._pending.empty()   # none shown, and the rest not piling up
    root.deiconify()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not Prompt.stack:
        _pump(root, 0.1)
    assert len(Prompt.stack) == 1
    Prompt.stack[0]._chosen(0)
    assert "RuntimeError: lost 0" in copied[0]


def test_the_box_fits_the_players_narrowest_window(root, tmp_path):
    from ironbark import theme
    from ironbark.popups import Prompt

    found = crash.describe("in the window code", *error_info())
    for width, label in ((theme.px(700), "Open log folder"), (theme.px(300), crash.SHORT_FOLDER_LABEL)):
        root.geometry(f"{width}x{theme.px(500)}")
        _pump(root, 0.2)
        box = crash.show_in_app(root, found, tmp_path)
        _pump(root, 0.1)
        assert box.buttons[1].text_value == label
        right = max(b.winfo_rootx() + b.winfo_width() for b in box.buttons)
        left = min(b.winfo_rootx() for b in box.buttons)
        assert left >= box.winfo_rootx() and right <= box.winfo_rootx() + box.winfo_width()   # none cut off
        box.press_cancel()
    assert Prompt.stack == [] and not crash._open


# -- diagnostics ---------------------------------------------------------------------------------------
def test_diagnostics_zip_holds_the_logs_and_versions_with_folders_left_out(tmp_path):
    home = "C:\\Users\\Jane"
    folder = tmp_path / "Ironbark"
    folder.mkdir()
    (folder / "crash.log").write_text(f"error in D:\\Music\\Abba\\01.flac\nfrom {home}\\AppData\n", encoding="utf-8")
    (folder / "app.log").write_text("WARNING couldn't read the tab d:/music\n", encoding="utf-8")
    (folder / "app.log.1").write_text("older\n", encoding="utf-8")
    prefs = {**settings.DEFAULTS, "library_folder": "D:\\Music", "last_folder": "D:\\Music\\Abba",
             "tabs": [{"kind": "folder", "path": "D:\\Music\\Abba", "extras": ["E:\\Rips\\x.flac"]}],
             "vu_auto": {"D:\\Music\\Abba": -18.0}, "experiments": {"tag_editor": True}}
    zip_path = diagnostics.write_zip(tmp_path / "out" / "d.zip", prefs, folder,
                                     outputs=lambda: "Default output: Speakers\n", home=home)
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
        read = lambda name: archive.read(name).decode("utf-8")  # noqa: E731
        assert {"README.txt", "about.txt", "audio-outputs.txt", "settings.json", "crash.log", "app.log",
                "app.log.1"} <= names
        saved = json.loads(read("settings.json"))
        about, crash_log, app_log = read("about.txt"), read("crash.log"), read("app.log")
    # Numbered in the order met (the same path always gets the same number).
    assert saved["last_folder"] == "<folder 1>" and saved["library_folder"] == "<folder 2>"
    assert saved["tabs"][0]["path"] == "<folder 1>" and saved["tabs"][0]["extras"] == ["<folder 3>"]
    assert saved["vu_auto"] == {"<folder 1>": -18.0} and saved["volume"] == settings.DEFAULTS["volume"]
    assert "Music" not in json.dumps(saved) and "Abba" not in json.dumps(saved)
    assert "<folder 1>\\01.flac" in crash_log and "<home>\\AppData" in crash_log and "Jane" not in crash_log
    assert "<folder 2>" in app_log                                   # (written with other slashes and case)
    assert f"{APP_NAME} {__version__}" in about and "tag_editor" in about and "Python" in about
    assert not (tmp_path / "out" / "d.zip.partial").exists()


def test_diagnostics_never_go_online_and_survive_a_failed_device_list(tmp_path, monkeypatch):
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("went online"))

    def no_devices():
        raise OSError("no audio")
    files = diagnostics.contents(dict(settings.DEFAULTS), tmp_path, outputs=no_devices)
    assert "couldn't be read" in files["audio-outputs.txt"] and "crash.log" not in files


def test_a_drive_root_in_the_settings_doesnt_blank_out_the_logs():
    redactor = diagnostics.Redactor(home="")
    redactor.value({"library_folder": "C:\\"})
    assert redactor.text("C:\\Program Files\\x and C: drive") == "C:\\Program Files\\x and C: drive"


def test_paths_in_error_messages_are_left_out_too():
    # Python writes a path in an error message with every backslash doubled; a log line may carry
    # on straight after a path ("(missing)", a closing quote, a comma).
    redactor = diagnostics.Redactor(home="C:\\Users\\Jane")
    redactor.value({"library_folder": "D:\\Music", "tabs": [{"extras": ["E:\\Rips\\x.flac"]}],
                    "last_folder": "D:\\Music\\Abba"})
    text = redactor.text("PermissionError: [WinError 5] Access is denied: 'C:\\\\Users\\\\jane\\\\Music\\\\a.flac'\n"
                         "couldn't read the tab D:\\Music\\Abba (missing)\n"
                         "['E:\\\\Rips\\\\x.flac', 'd:/music/abba/02.flac']\n"
                         "\\\\NAS\\share\\x · 07/10/2026 · https://example.com/a")
    assert "Jane" not in text and "jane" not in text and "Abba" not in text and "Rips" not in text
    assert "'<home>\\\\Music\\\\a.flac'" in text and "<folder 3> (missing)" in text
    assert "['<folder 2>', '<folder 3>/02.flac']" in text
    assert text.endswith("\\\\NAS\\share\\x · 07/10/2026 · https://example.com/a")   # nothing else touched


def test_a_long_users_settings_and_full_logs_are_redacted_quickly():
    # Auto VU keeps a setting for every album played: thousands of paths, and logs of megabytes.
    albums = [f"D:\\Music\\Artist {n % 300}\\Album {n}" for n in range(3000)]
    redactor = diagnostics.Redactor(home="C:\\Users\\Jane")
    redactor.value({"vu_auto": {album: -14.0 for album in albums}})
    log_text = "\n".join(f"07/10/2026 10:00:00 WARNING ironbark.ui: couldn't read the tab {albums[n * 7 % 3000]}"
                         f"\\01 Song.flac" for n in range(25000))
    began = time.monotonic()
    redacted = redactor.text(log_text)
    assert time.monotonic() - began < 5   # (it took minutes when each path was a pass of its own)
    assert "Artist" not in redacted and redacted.count("<folder ") == 25000


# -- the rotating log -------------------------------------------------------------------------------
def test_the_log_rotates_and_keeps_three_files(tmp_path, monkeypatch):
    monkeypatch.setattr(logs, "MAX_BYTES", 2000)
    path = logs.setup(tmp_path)
    test_log = logging.getLogger("ironbark.test_rotation")
    for n in range(400):
        test_log.warning("line %d of a log that keeps growing", n)
    logs.shutdown()
    assert path == tmp_path / "app.log"
    assert [p.name for p in logs.log_files(tmp_path)] == ["app.log", "app.log.1", "app.log.2"]
    assert not (tmp_path / "app.log.3").exists()
    assert all(p.stat().st_size <= 2200 for p in logs.log_files(tmp_path))
    assert "line 399" in (tmp_path / "app.log").read_text(encoding="utf-8")


def test_detailed_logging_adds_the_players_own_notes(tmp_path):
    logs.setup(tmp_path)
    notes = logging.getLogger("ironbark.test_detail")
    notes.info("quiet by default")
    logs.set_detailed(True)
    notes.debug("written when detailed")
    logging.getLogger("comtypes.test").info("other libraries stay quiet")
    logs.set_detailed(False)
    notes.info("quiet again")
    logs.shutdown()
    text = (tmp_path / "app.log").read_text(encoding="utf-8")
    assert "written when detailed" in text and "DEBUG" in text
    assert "quiet by default" not in text and "quiet again" not in text and "other libraries" not in text
    assert "/20" in text.split(" ")[0]   # dates as dd/mm/yyyy


def test_errors_reach_app_log_as_well_as_crash_log(tmp_path, monkeypatch):
    logs.setup(tmp_path)
    monkeypatch.setattr(start, "_crash_file", io.StringIO())
    monkeypatch.setattr(start, "_last_logged", None)
    start._log_error("in the window code", *error_info("logged twice"))
    logs.shutdown()
    assert "RuntimeError: logged twice" in start._crash_file.getvalue()
    assert "RuntimeError: logged twice" in (tmp_path / "app.log").read_text(encoding="utf-8")


def test_window_and_thread_errors_are_reported_for_the_box(monkeypatch):
    monkeypatch.setattr(start, "_crash_file", io.StringIO())
    start._log_callback_error(*error_info("in a callback"))
    thread = threading.Thread(name="tab-load")
    start._log_thread_error(threading.ExceptHookArgs((*error_info("in a thread"), thread)))
    reported = [crash._pending.get_nowait(), crash._pending.get_nowait()]
    assert reported[0].where == "in the window code" and "in a callback" in reported[0].text
    assert reported[1].where == "in the tab-load thread"


# -- the settings file's format --------------------------------------------------------------------
@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "Ironbark" / "settings.json"
    monkeypatch.setattr(settings, "settings_path", lambda: path)
    monkeypatch.setattr(settings, "problem", "")
    path.parent.mkdir(parents=True)
    return path


def test_an_unnumbered_file_is_brought_up_to_date_step_by_step(settings_file, monkeypatch):
    steps = []

    def step_0(saved):
        steps.append(0)
        saved["volume"] = saved.pop("old_volume")
        return saved

    def step_1(saved):
        steps.append(1)
        return saved

    monkeypatch.setattr(settings, "SETTINGS_VERSION", 2)
    monkeypatch.setattr(settings, "MIGRATIONS", {0: step_0, 1: step_1})
    settings_file.write_text(json.dumps({"old_volume": 0.4}), encoding="utf-8")
    prefs = settings.load()
    assert steps == [0, 1] and prefs["volume"] == 0.4 and prefs["settings_version"] == 2
    assert settings.problem == ""


def test_the_real_steps_take_every_old_file(settings_file):
    settings_file.write_text(json.dumps({"volume": 0.5, "tabs": []}), encoding="utf-8")
    prefs = settings.load()
    assert prefs["settings_version"] == settings.SETTINGS_VERSION and prefs["volume"] == 0.5
    assert settings.DEFAULTS["settings_version"] == settings.SETTINGS_VERSION
    assert sorted(settings.MIGRATIONS) == list(range(settings.SETTINGS_VERSION))


def test_a_broken_step_leaves_the_values_as_they_were(settings_file, monkeypatch):
    def broken(saved):
        raise KeyError("nope")
    monkeypatch.setattr(settings, "MIGRATIONS", {0: broken})
    settings_file.write_text(json.dumps({"volume": 0.3}), encoding="utf-8")
    assert settings.load()["volume"] == 0.3


def test_a_newer_versions_file_is_copied_aside_before_it_is_saved_over(settings_file):
    newer = {"settings_version": 99, "volume": 0.6, "a_setting_from_the_future": [1, 2]}
    text = json.dumps(newer)
    settings_file.write_text(text, encoding="utf-8")
    prefs = settings.load()
    copy = settings_file.with_name("settings.json.v99.bak")
    assert copy.read_text(encoding="utf-8") == text
    assert prefs["volume"] == 0.6 and prefs["a_setting_from_the_future"] == [1, 2]   # kept, not lost
    assert "newer version" in settings.problem and copy.name in settings.problem
    assert settings.save(prefs)
    assert json.loads(settings_file.read_text(encoding="utf-8"))["settings_version"] == settings.SETTINGS_VERSION
    assert copy.read_text(encoding="utf-8") == text


# -- the update check ------------------------------------------------------------------------------
def feed(version="99.0.0", **extra) -> bytes:
    return json.dumps({"version": version, "date": "15/11/2026",
                       "url": "https://www.digitalgreenhouse.com.au/ironbark/download", "notes": "New.", **extra}).encode()


ON = {"check_updates": True, "allow_network": True, "update_checked": 0.0}


def test_a_newer_release_is_found_and_nothing_else_is():
    asked = []

    def fetch(url, prefs):
        asked.append(url)
        return feed()
    release = updates.check(dict(ON), fetch=fetch)
    assert asked == [updates.UPDATE_URL]
    assert release == updates.Release("99.0.0", "15/11/2026", "https://www.digitalgreenhouse.com.au/ironbark/download",
                                      "New.")
    assert updates.check(dict(ON), fetch=lambda url, prefs: feed(__version__)) is None    # up to date
    assert updates.check(dict(ON), fetch=lambda url, prefs: feed("0.0.1")) is None        # older
    assert updates.check(dict(ON), fetch=lambda url, prefs: None) is None                 # 404: no file yet
    assert updates.check(dict(ON), fetch=lambda url, prefs: b"<html>") is None            # not the feed


def test_timeouts_and_errors_are_quiet():
    def slow(url, prefs):
        raise TimeoutError("no answer in 30 s")
    assert updates.check(dict(ON), fetch=slow) is None

    def broken(url, prefs):
        raise OSError("no route to host")
    assert updates.check(dict(ON), fetch=broken) is None


def test_nothing_is_fetched_unless_both_switches_are_on(monkeypatch):
    from ironbark import ripcheck

    monkeypatch.setattr(ripcheck, "_urlopen", lambda *a, **k: pytest.fail("went online"))
    for prefs in ({"check_updates": True, "allow_network": False}, {"check_updates": False, "allow_network": True}):
        assert updates.check(prefs, fetch=lambda url, p: pytest.fail("fetched")) is None
        assert not updates.due(prefs, now=10 ** 9)
    # Even if asked directly, the one place that goes online refuses while network access is off.
    assert updates.check({"check_updates": True, "allow_network": False}) is None


def test_at_most_once_a_day():
    day = updates.CHECK_EVERY_S
    assert updates.due({**ON, "update_checked": 0.0}, now=10 * day)
    assert not updates.due({**ON, "update_checked": 10 * day}, now=10 * day + day - 1)
    assert updates.due({**ON, "update_checked": 10 * day}, now=11 * day)
    assert updates.due({**ON, "update_checked": 20 * day}, now=10 * day)   # the clock went backwards


def test_the_download_link_must_be_digital_greenhouses():
    for url in ("http://www.digitalgreenhouse.com.au/x", "https://evil.example/ironbark",
                "https://digitalgreenhouse.com.au.evil.example/", "javascript:alert(1)", 42):
        assert updates.parse(feed(url=url)).url == updates.DOWNLOAD_PAGE
    assert updates.parse(feed(url="https://digitalgreenhouse.com.au/ironbark")).url == \
        "https://digitalgreenhouse.com.au/ironbark"
    assert updates.parse(feed(version="v1.2.3")).version == "v1.2.3" and updates.newer("v1.2.3", "1.2.2")
    assert updates.parse(feed(notes="x " * 500)).notes == ("x " * 150).strip()[:updates.NOTES_LIMIT]
    assert updates.parse(b'{"version": "soon"}') is None


def test_the_switch_needs_network_access_and_a_find_shows_in_the_status_line(make_window, monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    window, root, player, errors = make_window()
    window.check_updates.set(True)
    window._updates_changed()                         # network access is off: switched back off
    assert window.check_updates.get() is False and window.prefs["check_updates"] is False
    assert "Allow network access" in window._notice
    started = []
    monkeypatch.setattr(updates, "check", lambda prefs: started.append(dict(prefs)) or updates.Release("99.0.0"))
    window.allow_network.set(True)
    window._network_changed()
    window.check_updates.set(True)
    window._updates_changed()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and window.update_available is None:
        _pump(root, 0.1)
    assert started == [{"check_updates": True, "allow_network": True}]   # only the switches go to the thread
    assert window.prefs["update_checked"] > 0
    assert window._notice == f"{APP_NAME} 99.0.0 is available · click here for the download page"
    assert "99.0.0 available" in window.version_label.cget("text")
    window._status_clicked()
    assert opened == [updates.DOWNLOAD_PAGE]
    window._check_for_update_if_due()                  # checked just now: not again today
    assert len(started) == 1 and errors == []


def test_detailed_logging_switch_takes_effect_at_once(make_window):
    window, root, player, errors = make_window()
    window.detailed_logging.set(True)
    window._detailed_logging_changed()
    assert logs.detailed() and window.prefs["detailed_logging"] is True
    window.detailed_logging.set(False)
    window._detailed_logging_changed()
    assert not logs.detailed()


def test_save_diagnostics_from_the_window(make_window, tmp_path):
    window, root, player, errors = make_window()
    target = tmp_path / "diag.zip"
    window.save_diagnostics(str(target))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not window._notice.startswith("Diagnostics saved"):
        _pump(root, 0.1)
    assert target.is_file() and "nothing has been sent" in window._notice
    with zipfile.ZipFile(target) as archive:
        assert "settings.json" in archive.namelist()
    assert errors == []


# -- leftover tag copies after an unfinished save --------------------------------------------------
def test_leftover_tag_copies_are_swept_only_where_a_save_was_left_unfinished(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")
    crashed, untouched, in_hand = tmp_path / "Crashed", tmp_path / "Untouched", tmp_path / "Busy"
    song = flac(crashed / "01.flac")
    leftover = touch(tag_editor.temp_path(song))
    stray = touch(crashed / ".~other.flac.dgtmp")
    elsewhere = touch(tag_editor.temp_path(flac(untouched / "01.flac")))
    busy_song = flac(in_hand / "01.flac")
    busy_copy = touch(tag_editor.temp_path(busy_song))
    history = tag_editor.history_folder()
    history.mkdir(parents=True)
    entry = {"format": tag_editor.HISTORY_FORMAT, "id": "1", "when": "01/10/2026 10:00:00", "summary": "x",
             "undo_of": None, "files": [
                 {"path": str(song), "kind": "flac", "before": None, "after_digest": None, "saved": False,
                  "state": "waiting", "reason": ""},
                 {"path": str(busy_song), "kind": "flac", "before": None, "after_digest": None, "saved": True,
                  "state": "saving", "reason": ""}]}
    (history / "TagEdit_1.json").write_text(json.dumps(entry), encoding="utf-8")
    finished = dict(entry, id="2", files=[dict(entry["files"][0], path=str(untouched / "01.flac"), state="saved")])
    (history / "TagEdit_2.json").write_text(json.dumps(finished), encoding="utf-8")
    assert tag_editor._claim(busy_song, "a save in hand")
    try:
        assert tag_editor.sweep_leftover_copies() == 2
    finally:
        tag_editor._release(busy_song, "a save in hand")
    assert not leftover.exists() and not stray.exists()
    assert elsewhere.exists() and busy_copy.exists() and song.exists()
    after = json.loads((history / "TagEdit_1.json").read_text(encoding="utf-8"))
    assert after["files"][0]["state"] == "failed" and after["files"][0]["reason"] == tag_editor.STOPPED_REASON
    assert after["files"][1]["state"] == "saving"   # caught mid-swap: Undo can still put it back


# -- Python's cycle collector on Tk's thread --------------------------------------------------------
class Holder:
    """A reference cycle holding a widget; notes the thread that finally frees it."""
    freed_on: list = []

    def __init__(self, widget):
        self.widget, self.me = widget, self

    def __del__(self):
        Holder.freed_on.append(threading.current_thread().name)


def test_cycles_made_on_a_worker_are_freed_on_tks_thread(root):
    Holder.freed_on = []
    collector = tkgc.Collector(root, tick_ms=50, full_every_s=0.2)
    collector.start()
    try:
        assert not gc.isenabled()
        label = tk.Label(root)

        def worker():
            Holder(label)                               # garbage at once: a cycle, nothing else refers to it
            junk = [[] for _ in range(50000)]            # enough allocation to have set off the collector
            del junk

        thread = threading.Thread(target=worker, name="decoder-like")
        thread.start()
        thread.join()
        assert Holder.freed_on == []                     # not freed there…
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not Holder.freed_on:
            _pump(root, 0.05)
        assert Holder.freed_on == [threading.current_thread().name]   # …but here, on Tk's thread
        assert collector.collections >= 1
        label.destroy()
    finally:
        collector.stop()
    assert gc.isenabled() and not collector.running


def test_the_collector_lets_go_when_its_window_closes(root):
    collector = tkgc.Collector(root, tick_ms=50)
    collector.start()
    root.destroy()
    assert not collector.running and gc.isenabled()


def test_the_player_keeps_it_off_after_closing_when_asked(root):
    collector = tkgc.Collector(root)
    collector.restore_on_close = False
    collector.start()
    collector.stop()
    try:
        assert not gc.isenabled()
    finally:
        gc.enable()
