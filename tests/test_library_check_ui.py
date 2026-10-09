"""The quality check in the windows: the Library's Needs a look view (asking first with how long it
takes, the list filling in, its status line and buttons, progress in the other views' status
line), Now Playing showing a stored result at once and keeping its own checks, the clipping guard
taking a stored true peak; and the leftovers — Tag history read off Tk's thread with Clear
history…, and a playlist in an older encoding asked about before it's rewritten. Tk tests skip
without a display."""
from __future__ import annotations

import gc
import os
import queue
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from ironbark import library_check, library_index, playlists, popups, settings, tag_editor, theme
from ironbark.library import Track, read_track
from ironbark.library_check import Checker, Status

RATE = 44100


def noise(seconds, amp=0.25, seed=1):
    rng = np.random.default_rng(seed)
    return (amp * rng.standard_normal((int(seconds * RATE), 2))).clip(-0.99, 0.99)


def flac(path: Path, data, subtype="PCM_16", **tags) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, data, RATE, format="FLAC", subtype=subtype)
    if tags:
        from ironbark.tagio.flac import FLACFile

        audio = FLACFile(path)
        for key, value in tags.items():
            audio[key] = value
        audio.save()
    return path


@pytest.fixture
def root():
    tk = pytest.importorskip("tkinter")
    try:
        r = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"no display: {error}")
    r.withdraw()
    theme.apply(r)
    errors = []
    r.report_callback_exception = lambda *info: errors.append(info)
    r.errors = errors
    yield r
    theme.use(theme.DARK)
    r.destroy()
    del r
    gc.collect()   # on this thread (Tk objects freed on another thread abort Python)


def texts(frame) -> set[str]:
    """Every piece of text shown in a frame (and the frames in it)."""
    found = set()
    for child in frame.winfo_children():
        try:
            found.add(str(child.cget("text")))
        except Exception:   # (a frame: no text of its own)
            pass
        found |= texts(child)
    return found


def pump(root, until, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        root.update()
        if until():
            return
        time.sleep(0.02)
    raise AssertionError("it never happened")


@pytest.fixture
def music(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")
    root = tmp_path / "Music"
    flac(root / "Fine" / "01.flac", noise(3, seed=1), title="Fine", artist="Ann", album="Fine")
    clipped = noise(3, amp=0.1, seed=2)
    clipped[RATE: RATE + 5] = 1.0
    flac(root / "Loud" / "01.flac", clipped, title="Loud", artist="Bob", album="Loud")
    flac(root / "Wide" / "01.flac", np.round(noise(3, seed=3) * 32767) / 32768, subtype="PCM_24",
         title="Wide", artist="Cat", album="Wide")
    return root


# -- the Library's Needs a look -------------------------------------------------------------------------
def test_needs_a_look_asks_first_then_lists_what_the_check_found(root, music, monkeypatch, tk_thread_rule):
    from ironbark import library_window
    from ironbark.library_window import LibraryWindow
    from test_library_window import column, wait_for_index

    monkeypatch.setattr(library_window, "CHECK_REREAD_S", 0.0)
    states = []
    checker = Checker(lambda: library_index.shared(music).db, on_state=states.append, in_process=True)
    w = LibraryWindow(root, music, None, on_choose=lambda p: None, on_root_changed=lambda p: None,
                      on_load_all=lambda r, q: None, checker=checker)
    wait_for_index(w, root)
    w.set_view("needs_look")
    pump(root, lambda: w._songs_have == w._wanted())
    assert tuple(w.tree.cget("displaycolumns")) == ("title", "artist", "album", "found")
    assert w.status.cget("text").startswith("“Check the whole library…” reads every song once")
    assert w.check_button.winfo_manager() and w.check_button.cget("text") == "Check the whole library…"
    assert not w.check_other.winfo_manager() and not w.load_all_button.winfo_manager()
    asked = []
    monkeypatch.setattr(popups, "confirm", lambda *args, **kwargs: asked.append((args, kwargs)) or False)
    w.check_button.invoke()                       # how long? worked out on the query thread, then asked
    pump(root, lambda: asked)
    (args, kwargs), = asked
    assert args[1] == "Check every song in the library?" and args[2].startswith("3 songs (1 minute of music) are")
    assert "about 1 minute" in kwargs["detail"] and kwargs["ok"] == "Start checking"
    assert states == [] and checker.state == "off"           # "Not now": nothing starts
    monkeypatch.setattr(popups, "confirm", lambda *args, **kwargs: True)
    w.check_button.invoke()
    pump(root, lambda: checker.state == "on")
    assert checker.idle(60)
    pump(root, lambda: len(column(w, "title")) == 2)
    assert sorted(column(w, "found")) == ["Clipped when mastered (2 runs stuck at full scale)",
                                          "Only 16 of 24 bits used: 16-bit stored as 24-bit"]
    pump(root, lambda: w.status.cget("text").startswith("Every song checked (3 songs) · 2 songs need a look: "))
    assert w.status.cget("text").endswith("1 padded · 1 clipped")
    assert w.check_other.cget("text") == "Stop checking new music" and not w.check_button.winfo_manager()
    w.search_var.set("clipped")                   # the search looks through what was found too
    w._flush_search()
    assert column(w, "title") == ["Loud"]
    w.search_var.set("")
    w._flush_search()
    # While it runs, every view's status line says how far it has got.
    checker.status = Status(state="on", running=True, done=12, total=40, seconds_left=600)
    w.set_view("albums")
    pump(root, lambda: w.status.cget("text").startswith("Checking quality: 12 of 40 songs · about 10 minutes left · "))
    checker.status = Status(state="paused", done=12, total=40)
    w.set_view("needs_look")
    pump(root, lambda: w.check_button.winfo_manager())
    assert (w.check_button.cget("text"), w.check_other.cget("text")) == ("Resume", "Stop")
    pump(root, lambda: w.status.cget("text").startswith("Quality check paused at 12 of 40 songs · 2 songs need"))
    w.check_other.invoke()                        # Stop: what was found stays listed
    assert checker.state == "off" and states == ["on", "off"]
    pump(root, lambda: w.check_button.cget("text") == "Check the whole library…")
    assert len(column(w, "title")) == 2
    w.close()
    checker.close()
    library_index.close_shared()
    assert not root.errors


# -- Now Playing ---------------------------------------------------------------------------------------
class ShownPlayer:
    volume = 1.0

    def __init__(self, track):
        self.track = track

    def status(self):
        from ironbark.engine import OutputInfo, State, Status as PlayerStatus
        return PlayerStatus(State.PLAYING, 0, self.track, 1.0, self.track.duration,
                            output=OutputInfo("Test output", 44100, 48000, False))


def test_now_playing_shows_a_stored_check_at_once_and_keeps_its_own(root, music, monkeypatch):
    from ironbark.analysis import analyse
    from ironbark.details import NowPlayingWindow

    loud = read_track(music / "Loud" / "01.flac")
    result = analyse(loud.path, loud.bits, loud.md5)
    asked, threads = [], []

    def stored(path):
        asked.append(path)
        threads.append(threading.current_thread())
        return result, time.mktime((2026, 10, 7, 12, 0, 0, 0, 0, -1))

    monkeypatch.setattr(NowPlayingWindow, "_analysis_cache", {})
    monkeypatch.setattr(NowPlayingWindow, "_stored_when", {})
    win = NowPlayingWindow(root, ShownPlayer(loud), prefs={}, stored=stored)
    pump(root, lambda: NowPlayingWindow.cached_analysis(loud.path) is not None and win._analysis is not None)
    assert asked == [loud.path] and threads[0] is not threading.main_thread()
    rows = texts(win.check_frame)
    assert "Checked" in rows and any(text.startswith("07/10/2026 by the quality check") for text in rows)
    win.close()
    # No stored result: Check runs here, and its result is kept for the library check.
    fine = read_track(music / "Fine" / "01.flac")
    kept = []
    win = NowPlayingWindow(root, ShownPlayer(fine), prefs={}, stored=lambda path: None,
                           remember=lambda track, res, file: kept.append((track.path, res.clip_runs, file)))
    pump(root, lambda: hasattr(win, "check_button"))
    win.check_button.invoke()
    pump(root, lambda: kept and win._analysis is not None)
    stat = os.stat(fine.path)
    assert kept == [(fine.path, 0, (stat.st_size, stat.st_mtime_ns))]
    assert "Checked" not in texts(win.check_frame)     # (checked here and now)
    win.close()
    assert not root.errors


# -- the clipping guard ------------------------------------------------------------------------------------
def test_the_clipping_guard_takes_a_stored_true_peak(tmp_path):
    from ironbark.ui import PlayerWindow

    measured = []
    w = SimpleNamespace(_peaks={}, _peak_jobs=queue.Queue(), _peak_wanted=set(), _gain_tags={}, _lufs={},
                        _level_wanted=set(), _stored_peak=lambda path: 1.25 if path.name == "known.flac" else None)
    w._measure_for_levelling = lambda path: PlayerWindow._measure_for_levelling(w, path)
    known, unknown = tmp_path / "known.flac", tmp_path / "other.flac"
    flac(known, noise(1, amp=0.5))
    flac(unknown, noise(1, amp=0.5))
    w._peak_wanted = {known, unknown}
    from ironbark import analysis
    real = analysis.true_peak
    analysis.true_peak = lambda path, cancel=None: measured.append(path) or real(path, cancel)
    try:
        threading.Thread(target=PlayerWindow._measure_peaks, args=(w,), daemon=True).start()
        w._peak_jobs.put(known)
        w._peak_jobs.put(unknown)
        deadline = time.monotonic() + 20
        while len(w._peaks) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        analysis.true_peak = real
    assert w._peaks[known] == 1.25 and measured == [unknown]          # the stored one isn't read again
    # Volume levelling's look-ahead takes it too (no true-peak pass for that file).
    w._peaks.clear()
    w._level_wanted = {known}
    seen = []
    from ironbark import levelling
    real_measure = levelling.measure
    levelling.measure = lambda path, peak=True, loudness=True, cancel=None: seen.append((peak, loudness)) or (None, -14.0)
    try:
        PlayerWindow._measure_for_levelling(w, known)
    finally:
        levelling.measure = real_measure
    assert w._peaks[known] == 1.25 and seen == [(False, True)]


# -- Tag history ---------------------------------------------------------------------------------------
def test_tag_history_is_read_off_tk_and_clear_history_keeps_what_still_waits(root, tmp_path, monkeypatch):
    from ironbark.tag_editor import Edit, TagSave
    from ironbark.tag_window import TagHistoryWindow

    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")
    songs = [flac(tmp_path / f"{n}.flac", noise(0.2, seed=n), album="Old") for n in (1, 2)]
    for n, song in enumerate(songs):
        TagSave(tag_editor.edit_jobs([read_track(song)], Edit({"album": f"New {n}"})), f"edit {n}").run()
    entries = tag_editor.history()
    waiting = entries[0]
    waiting["files"][0]["state"] = "waiting"     # a save still waiting for the player to let go of a song
    tag_editor._write_json(Path(waiting["file"]), {k: v for k, v in waiting.items() if k != "file"})
    read_on = []
    real = tag_editor.history
    monkeypatch.setattr(tag_editor, "history", lambda: read_on.append(threading.current_thread()) or real())
    asked = []
    hw = TagHistoryWindow(root, {}, on_saved=lambda s: None, busy=lambda: set(),
                          confirm_clear=lambda message, detail: asked.append((message, detail)) or True)
    assert hw.loading is not None and hw.clear_button.instate(["disabled"])   # (still being read)
    pump(root, lambda: hw.loading is None)
    assert read_on and read_on[0] is not threading.main_thread() and len(hw.entries) == 2
    assert hw.clear_button.instate(["!disabled"])
    hw.clear()
    assert asked == [("Clear the tag history (1 save)?",
                      "Those edits can't be undone after this. The music files themselves aren't touched.\n\n"
                      "1 save still waiting to finish (a song that was playing) is kept.")]
    pump(root, lambda: not hw.clearing and hw.loading is None)
    assert [e["id"] for e in hw.entries] == [waiting["id"]] and hw.status.cget("text") == (
        "Cleared 1 save from the tag history")
    assert hw.clear_button.instate(["disabled"])      # only the waiting one is left: nothing to clear
    assert all(read_track(song).album != "Old" for song in songs)   # the music files keep their new tags
    hw.close()
    assert not root.errors


def test_clearing_the_history_sweeps_pictures_only_it_held(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")
    blobs = tag_editor._blob_folder()
    blobs.mkdir(parents=True)
    old = tag_editor._put_blob(b"x" * 5000)
    kept = tag_editor._put_blob(b"y" * 5000)
    tag_editor._write_json(tag_editor.history_folder() / "TagEdit_1.json",
                           {"format": 1, "files": [{"state": "saved", "before": {"history_blob": old}}]})
    tag_editor._write_json(tag_editor.history_folder() / "TagEdit_2.json",
                           {"format": 1, "files": [{"state": "pending", "before": {"history_blob": kept}}]})
    assert tag_editor.clear_history() == 1
    assert not (blobs / old).exists() and (blobs / kept).exists()   # (no grace needed: no save is running)


# -- a playlist in an older encoding -------------------------------------------------------------------------
def test_a_playlist_in_an_older_encoding_is_asked_about_before_it_is_rewritten(root, tmp_path, monkeypatch):
    from ironbark.ui import PlayerWindow

    old = tmp_path / "Old.m3u"
    original = "#EXTM3U\r\nC:\\Music\\Caf\xe9.flac\r\n".encode("cp1252")
    old.write_bytes(original)
    assert playlists.fallback_encoding(old).startswith("Windows")   # (this PC's code page: Windows-1252 here)
    utf8 = tmp_path / "New.m3u8"
    utf8.write_bytes("#EXTM3U\nC:\\Music\\Café.flac\n".encode("utf-8"))
    assert playlists.fallback_encoding(utf8) == "" and playlists.fallback_encoding(tmp_path / "none.m3u") == ""
    answers, asked = [False], []
    monkeypatch.setattr(popups, "confirm", lambda *args, **kwargs: asked.append((args[1], kwargs["ok"])) or answers[0])
    w = SimpleNamespace(root=root, _for_prompt=lambda: None, _playlist_tab=lambda path: None)
    w._may_rewrite = lambda path: PlayerWindow._may_rewrite(w, path)
    song = Track(Path("C:/Music/Café.flac"), "Café")
    assert PlayerWindow._save_playlist(w, old, [song]) is False      # Don't save: the file is left exactly as it was
    assert old.read_bytes() == original and asked == [("Save “Old” as UTF-8?", "Save as UTF-8")]
    answers[0] = True
    assert PlayerWindow._save_playlist(w, old, [song]) is True
    assert "Café.flac" in old.read_bytes().decode("utf-8") and playlists.fallback_encoding(old) == ""
    assert PlayerWindow._save_playlist(w, old, [song, song]) is True and len(asked) == 2   # (UTF-8 now: not asked)
    assert PlayerWindow._save_playlist(w, utf8, [song]) is True and len(asked) == 2


# -- deleting a playlist: to the Recycle Bin ---------------------------------------------------------------
def test_recycle_says_where_a_bin_exists_and_never_runs_on_tk(tmp_path, monkeypatch):
    import sys

    from ironbark import cuesheet, recycle

    assert recycle.can_recycle("\\\\server\\share\\Mix.m3u8") is False          # a network share: no bin
    with pytest.raises(FileNotFoundError):
        recycle.recycle(tmp_path / "gone.m3u8")
    if sys.platform == "win32":
        assert recycle.can_recycle(tmp_path / "Mix.m3u8") is (os.path.splitdrive(str(tmp_path))[0].upper() == "C:")
    threads = []
    monkeypatch.setattr(recycle, "_shell_delete", lambda full: threads.append(threading.current_thread())
                        or os.remove(full))
    monkeypatch.setattr(recycle, "_to_mac_bin", lambda full: threads.append(threading.current_thread())
                        or os.remove(full))
    mix = tmp_path / "Mix.m3u8"
    mix.write_text("#EXTM3U\n", encoding="utf-8")
    worker = threading.Thread(target=recycle.recycle, args=(mix,))
    worker.start()
    worker.join(10)
    assert not mix.exists() and threads == [worker]
    # The text decoder says which encoding read a file (the playlist warning goes by it).
    assert cuesheet.decode_with("Café".encode("utf-8"))[1] == "utf-8"
    assert cuesheet.decode_with("\ufeffCafé".encode("utf-8"))[1] == "utf-8"
    assert cuesheet.decode_with("Café".encode("utf-16"))[1] == "utf-16"
    assert not cuesheet.decode_with("Caf\xe9".encode("cp1252"))[1].startswith("utf")


def test_the_check_holds_off_while_a_blind_test_window_is_open(root):
    import tkinter as tk

    from ironbark.ui import PlayerWindow

    holds = []
    w = SimpleNamespace(checker=SimpleNamespace(hold=lambda reason, on=True: holds.append((reason, on))))
    window = tk.Toplevel(root)
    tk.Label(window, text="x").pack()
    PlayerWindow._hold_check_while(w, window, "abx")
    assert holds == [("abx", True)]
    window.destroy()                              # (its children's Destroy events don't count)
    root.update()
    assert holds == [("abx", True), ("abx", False)]
