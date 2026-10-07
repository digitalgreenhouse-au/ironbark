"""The main window's robustness: work kept off the Tk thread, loops that survive errors, tabs that
never stay stuck "Loading…", songs that aren't lost from playlists, closing safely during a tag
save, crash logging, and window placement. Window code is driven either through stand-ins
(SimpleNamespace + PlayerWindow's own methods) or the real window on temp settings with a fake
player (test_light_mini.make_window): no device is opened and nothing of the user's is touched."""

import io
import os
import queue
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from ironbark import analysis, artwork, audiofile, library, placement, playlists, settings, ui
from ironbark.engine import State, Status
from ironbark.library import Track
from ironbark.ui import SAFE_VOLUME, PlayerWindow, Tab
from test_cue_playback import make_image
from test_light_mini import _pump, make_window  # noqa: F401  (make_window is a fixture)

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def wav(path: Path, seconds: float = 0.2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, np.zeros((int(44100 * seconds), 2), dtype=np.int16), 44100, subtype="PCM_16")
    return path


def song(path, title="", **fields) -> Track:
    path = Path(path)
    return Track(path, title or path.stem, duration=60.0, **fields)


def wait_for(condition, seconds: float = 3.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def pump_until_closed(root, seconds: float) -> bool:
    """Run the window's events; True once it has been destroyed."""
    import tkinter as tk

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            root.update()
        except tk.TclError:
            return True
        time.sleep(0.02)
    try:
        root.winfo_exists()
    except tk.TclError:
        return True
    return False


class Root:
    """A stand-in Tk root that only notes what was scheduled."""

    def __init__(self):
        self.scheduled = []

    def after(self, ms, *call):
        self.scheduled.append((ms, call))
        return f"after#{len(self.scheduled)}"


# -- the album line: worked out once, not on every poll ---------------------------------------------
def test_the_album_summary_is_worked_out_once_per_change(monkeypatch):
    tracks = [song(f"C:/Music/Album {i // 10}/{i:02d}.flac", codec="FLAC", bits=16, sample_rate=44100)
              for i in range(200)]
    tab = Tab("library", "C:/Music", tracks=tracks)
    looked_up = []
    real = ui.album_folder
    monkeypatch.setattr(ui, "album_folder", lambda path: (looked_up.append(path), real(path))[1])
    first = PlayerWindow._album_summary(SimpleNamespace(), tab)
    assert first == "FLAC · 16-bit / 44.1 kHz · 10 tracks · 10:00"
    looked_up.clear()
    assert PlayerWindow._album_summary(SimpleNamespace(), tab) == first
    assert looked_up == []                       # the second poll does no work
    tab.tracks = tracks[:5]                      # a new list (an edit, an album added): worked out again
    assert PlayerWindow._album_summary(SimpleNamespace(), tab).endswith("5 tracks · 5:00")


# -- bit-perfect at start-up: the same ear check as switching it on ------------------------------
def test_starting_in_bit_perfect_mode_checks_the_device_volume(make_window, monkeypatch):
    from test_light_mini import FakePlayer

    told = []
    monkeypatch.setattr(ui.devices, "exclusive_supported", lambda: True)
    monkeypatch.setattr(FakePlayer, "refresh_hardware_volume", lambda self, *device: False)
    monkeypatch.setattr(ui.popups, "tell", lambda window, title, *args, **kwargs: told.append(title))
    window, root, player, errors = make_window(exclusive=True, volume=1.0)
    assert player.volume == pytest.approx(SAFE_VOLUME)
    assert told == ["Volume turned down to protect your ears"] and not errors


# -- a song from Up next with no tab behind it ---------------------------------------------------
def test_a_song_from_up_next_with_no_tab_behind_it_can_be_paused():
    queued = song("C:/Music/Other/03.flac", "Queued")
    calls = []
    status = Status(State.PLAYING, -1, queued, 10.0, 60.0)
    w = SimpleNamespace(playing_tab=None, current=0, tabs=[Tab("album", "C:/Music/Album", tracks=[song("C:/a")])],
                        player=SimpleNamespace(status=lambda: status,
                                               toggle_pause=lambda: calls.append("toggle_pause")))
    w._play_tab = lambda *args, **kwargs: calls.append("play the tab")
    w.select_tab = lambda *args, **kwargs: calls.append("select")
    PlayerWindow.toggle_play(w)                       # Space / the play button
    assert calls == ["toggle_pause"]                  # pauses it, rather than cutting it off
    assert PlayerWindow._media_state(w) == "playing"  # the media keys see it too
    status.state = State.STOPPED
    assert PlayerWindow._media_state(w) == "stopped"


def test_a_song_from_up_next_with_no_tab_open_is_shown(make_window):
    window, root, player, errors = make_window()
    track = player.track
    player.status = lambda: Status(State.PLAYING, -1, track, 42.0, track.duration)
    window.tabs, window.current, window.playing_tab = [], -1, None
    window._refresh_tabs()
    _pump(root, 0.6)
    assert root.title() == "Song One — Artist"                  # the taskbar shows it
    assert window._media_state() == "playing"
    assert "Song One" in window.status_label.cget("text")      # (the empty window hides the head)
    assert window.play_button.playing and not errors


# -- tabs never stay "Loading…" ----------------------------------------------------------------------
def _other_album(tmp_path) -> Path:
    folder = tmp_path / "Music" / "Other"
    wav(folder / "01 First.wav")
    wav(folder / "02 Second.wav")
    return folder


def _slow_scan(window, gate):
    real = window._scan

    def scan(folder, whole_tree=False):
        gate.wait(5)
        return real(folder, whole_tree)
    return scan


def test_a_tab_closed_while_loading_loads_when_reopened(make_window, tmp_path):
    window, root, player, errors = make_window()
    other = _other_album(tmp_path)
    gate = threading.Event()
    window._scan = _slow_scan(window, gate)
    window.tabs.append(Tab("folder", str(other)))
    window.select_tab(1)
    _pump(root, 0.2)
    tab = window.tabs[1]
    assert tab.loading
    window.close_tab(1)
    _pump(root, 0.2)
    window.reopen_tab()
    gate.set()
    _pump(root, 0.8)
    assert not tab.loading and [t.title for t in tab.tracks] == ["First", "Second"]
    assert not errors


def test_play_pressed_while_a_tab_loads_plays_it_once_loaded(make_window, tmp_path):
    window, root, player, errors = make_window()
    other = _other_album(tmp_path)
    gate = threading.Event()
    window._scan = _slow_scan(window, gate)
    window.tabs.append(Tab("folder", str(other)))
    window.select_tab(1)
    _pump(root, 0.1)
    player.calls.clear()
    window.select_tab(1, then_play=True)     # e.g. Space while it's still loading
    gate.set()
    _pump(root, 0.8)
    assert "play" in player.calls and window.playing_tab == 1


def test_a_loader_that_fails_unexpectedly_still_finishes(make_window, tmp_path):
    window, root, player, errors = make_window()
    other = _other_album(tmp_path)

    def broken(folder, whole_tree=False):
        raise ValueError("a file the readers didn't expect")
    window._scan = broken
    window.tabs.append(Tab("folder", str(other)))
    window.select_tab(1)
    _pump(root, 0.6)
    tab = window.tabs[1]
    assert not tab.loading and tab.tracks == []


def test_tab_info_is_published_even_when_the_file_cant_be_read(tmp_path, monkeypatch):
    folder = tmp_path / "Album"
    wav(folder / "01.wav")

    def unreadable(path):
        raise OSError("gone")
    monkeypatch.setattr(ui, "read_track", unreadable)
    tab = Tab("album", str(folder))
    PlayerWindow._load_tab_info(SimpleNamespace(root=Root(), _finish_tab_info=lambda tab: None), tab)
    assert wait_for(lambda: tab.info is not None)
    assert tab.info[0] == "" and tab.info[3] == ""


def test_adding_to_a_closed_tab_that_was_loading_stops_waiting(tmp_path):
    tab = Tab("album", str(tmp_path / "A"))
    tab.loading = True                      # closed mid-load: it isn't in the window's tabs any more
    root, notices = Root(), []
    w = SimpleNamespace(root=root, tabs=[], _set_notice=lambda text, ms=0: notices.append(text))
    w._tab_index = lambda t: PlayerWindow._tab_index(w, t)
    PlayerWindow._finish_add_album(w, tab, tmp_path / "B", {"tracks": [song(tmp_path / "B" / "1.flac")]},
                                   lambda message: None)
    assert root.scheduled == []
    plan = SimpleNamespace(note=lambda: "", nothing=False)
    PlayerWindow._finish_drop(w, tab, {"plan": plan, "songs": ([], [])})
    assert root.scheduled == [] and notices[-1] == "The tab it was dropped on has been closed"


# -- playlists: no tag reading on the Tk thread; nothing lost while a tab loads ---------------------
def test_adding_to_a_playlist_that_isnt_open_reads_no_tags_on_the_window_thread(make_window, tmp_path, monkeypatch):
    window, root, player, errors = make_window()
    files = [wav(tmp_path / "Songs" / f"{n}.wav") for n in ("a", "b", "c")]
    path = tmp_path / "Mix.m3u8"
    playlists.write(path, [library.read_track(f) for f in files[:2]])
    added = library.read_track(files[2])
    readers = []
    real = ui.read_track
    monkeypatch.setattr(ui, "read_track", lambda p: (readers.append(threading.current_thread()), real(p))[1])
    window.add_to_playlist(path, [added])
    assert threading.main_thread() not in readers
    _pump(root, 0.6)
    assert [p.name for p in playlists.read(path)] == ["a.wav", "b.wav", "c.wav"]
    assert threading.main_thread() not in readers and not errors


def test_songs_added_while_a_playlist_tab_loads_are_kept(make_window, tmp_path, monkeypatch):
    window, root, player, errors = make_window()
    files = [wav(tmp_path / "Songs" / f"{n}.wav") for n in ("a", "b", "c")]
    path = tmp_path / "Mix.m3u8"
    playlists.write(path, [library.read_track(f) for f in files[:2]])
    gate = threading.Event()
    real = PlayerWindow._read_playlist

    def slow(playlist):
        tracks = real(playlist)          # the loader's snapshot is taken first...
        if threading.current_thread().name == "tab-load":
            gate.wait(5)                 # ...and handed back a while later
        return tracks
    window._read_playlist = slow
    window.open_tab("playlist", path)
    _pump(root, 0.2)
    window.add_to_playlist(path, [library.read_track(files[2])])
    gate.set()
    _pump(root, 0.8)
    tab = window.tabs[window.current]
    assert [t.path.name for t in tab.tracks] == ["a.wav", "b.wav", "c.wav"]
    window._remove_from_playlist(0)      # the next edit in that tab must not bring back the old list
    assert [p.name for p in playlists.read(path)] == ["b.wav", "c.wav"]
    assert not errors


def test_editing_a_playlist_that_isnt_playing_keeps_its_place(tmp_path):
    path = tmp_path / "Mix.m3u8"
    tracks = [song(tmp_path / f"{n}.flac") for n in range(1, 7)]
    tab = Tab("playlist", str(path), tracks=list(tracks))
    tab.last_index, tab.resume_at = 4, 200.0               # paused at 3:20 on song 5
    w = SimpleNamespace(tabs=[Tab("album", "C:/A"), tab], playing_tab=0, current=0,
                        _show_current_tab=lambda: None)
    w._playlist_tab = lambda p: PlayerWindow._playlist_tab(w, p)
    w._tab_index = lambda t: PlayerWindow._tab_index(w, t)
    PlayerWindow._save_playlist(w, path, tracks[:1] + tracks[2:])     # song 2 removed
    assert tab.view_track() is tracks[4] and tab.resume_at == 200.0
    PlayerWindow._save_playlist(w, path, [t for t in tab.tracks if t is not tracks[4]])  # song 5 removed
    assert tab.last_index == -1 and tab.resume_at == 0.0


# -- songs dragged onto a tab come back the same after a restart ------------------------------------
def _window_for_adding(tab):
    w = SimpleNamespace(tabs=[tab], current=0, playing_tab=None, _show_current_tab=lambda: None,
                        player=SimpleNamespace(set_tracks=lambda tracks, keep_playing=False: None))
    w._tab_index = lambda t: PlayerWindow._tab_index(w, t)
    return w


def test_only_the_songs_actually_added_are_remembered(tmp_path):
    a1, a2, b1 = song(tmp_path / "A" / "1.flac"), song(tmp_path / "A" / "2.flac"), song(tmp_path / "B" / "1.flac")
    tab = Tab("album", str(tmp_path / "A"), tracks=[a1, a2])
    w = _window_for_adding(tab)
    dragged = [song(a1.path), b1]
    message = PlayerWindow._add_tracks_to_tab(w, tab, dragged, [audiofile.key(t.path) for t in dragged])
    assert message == "Added 1 song to “A”" and tab.extras == [audiofile.key(b1.path)]


def test_cue_tracks_dragged_onto_a_tab_are_told_apart_and_restored(make_window, tmp_path):
    image, _data = make_image(tmp_path / "Image")
    one, _two, three = library.scan_album(tmp_path / "Image")
    tab = Tab("files", str(tmp_path / "Image"), tracks=[one])
    w = _window_for_adding(tab)
    assert PlayerWindow._add_tracks_to_tab(w, tab, [three], [audiofile.key(three.path)]).startswith("Added 1 song")
    assert tab.extras == [f"{image}#3"]
    # After a restart the tab is read from its extras: track 3 comes back as track 3, not the whole image.
    window, root, player, errors = make_window()
    window.tabs.append(Tab("files", str(tmp_path / "Image"), extras=[audiofile.key(one.path), f"{image}#3"]))
    window.select_tab(1)
    _pump(root, 0.8)
    assert [t.title for t in window.tabs[1].tracks] == ["One", "Three"]
    assert audiofile.cue_of(window.tabs[1].tracks[1].path).number == 3


def test_a_song_added_from_its_own_album_isnt_listed_twice_after_a_restart(make_window, tmp_path):
    album = _other_album(tmp_path)
    window, root, player, errors = make_window()
    window.tabs.append(Tab("album", str(album), extras=[str(album / "01 First.wav")]))
    window.select_tab(1)
    _pump(root, 0.8)
    assert [t.title for t in window.tabs[1].tracks] == ["First", "Second"]


# -- tabs on a drive that isn't connected ------------------------------------------------------------
def test_tabs_on_a_missing_drive_are_kept_with_their_place(tmp_path):
    missing = tmp_path / "USB" / "Album"
    w = SimpleNamespace(tabs=[], prefs={"tabs": [{"kind": "album", "path": str(missing),
                                                  "resume": {"path": str(missing / "01.wav"), "at": 125}},
                                                 "not a tab", {"kind": "album", "path": 5}],
                                        "current_tab": "x"},
                        selected=[])
    w.select_tab = lambda index, then_play=False: w.selected.append(index)
    w._refresh_tabs = lambda: None
    PlayerWindow._restore_tabs(w, None)
    assert [t.path for t in w.tabs] == [str(missing)] and w.selected == [0]
    assert w.tabs[0].source()["resume"] == {"path": str(missing / "01.wav"), "at": 125}


def test_a_missing_drives_tab_loads_once_it_is_connected(make_window, tmp_path):
    window, root, player, errors = make_window()
    missing = tmp_path / "USB" / "Album"
    tab = Tab("album", str(missing))
    tab.resume_path, tab.resume_at = str(missing / "02.wav"), 125.0
    window.tabs.append(tab)
    window.select_tab(1)
    _pump(root, 0.6)
    assert not tab.loading and tab.tracks is None and "connected" in window.format_label.cget("text")
    assert tab.source()["resume"] == {"path": str(missing / "02.wav"), "at": 125}
    wav(missing / "01.wav")
    wav(missing / "02.wav")
    window.select_tab(1)                     # chosen again once the drive is back
    _pump(root, 0.8)
    assert len(tab.tracks) == 2 and tab.last_index == 1 and tab.resume_at == 125.0 and not errors


# -- the poll loop and closing ---------------------------------------------------------------------
def test_one_error_doesnt_stop_the_poll_loop(make_window):
    window, root, player, errors = make_window()

    def broken():
        raise RuntimeError("one bad report")
    window._watch_reports = broken
    before = window._polls
    _pump(root, 1.2)
    assert window._polls >= before + 3


def test_quit_closes_the_window_even_when_a_step_fails(make_window):
    window, root, player, errors = make_window()

    def broken():
        raise TypeError("a pref that isn't JSON")
    window._save_if_changed = broken
    window.quit()
    assert pump_until_closed(root, 0.2)


def test_a_failed_settings_write_is_tried_again(make_window, monkeypatch):
    window, root, player, errors = make_window()
    writes = []
    monkeypatch.setattr(settings, "save", lambda data: writes.append(dict(data)) or False)
    window.prefs["library_view"] = "artists"
    assert window._save_if_changed() is False
    window._save_if_changed()
    assert len(writes) == 2          # not taken as saved: written again at the next autosave


def _saving_window():
    saving = SimpleNamespace(cancel=threading.Event(), finished=False, save=None)
    return SimpleNamespace(saving=saving, win=SimpleNamespace(winfo_exists=lambda: True),
                           close=lambda: saving.cancel.set())


def test_closing_during_a_tag_save_lets_it_finish_the_file_in_hand(make_window):
    window, root, player, errors = make_window()
    tags = _saving_window()
    window._tag_window = tags
    window.quit()
    assert not pump_until_closed(root, 0.4)       # still open: the save is stopping after its file
    assert tags.saving.cancel.is_set()
    tags.saving = None                            # it has stopped cleanly (its temporary copy removed)
    assert pump_until_closed(root, 0.6)


def test_quitting_mid_tag_save_leaves_no_copy_beside_the_songs(make_window, tmp_path, monkeypatch):
    from ironbark import tag_editor
    from ironbark.tag_window import TagWindow
    from test_tag_window import flac, wait

    window, root, player, errors = make_window()
    album = tmp_path / "Tagged"
    paths = [flac(album / "01.flac", title="One", album="Album"), flac(album / "02.flac", title="Two", album="Album")]
    checking = threading.Event()
    real = tag_editor.same_audio

    def slow(original, copy, kind, cancel=None):
        checking.set()
        while not cancel.is_set():   # a long check (a big file), until it's asked to stop
            time.sleep(0.01)
        return real(original, copy, kind, cancel)
    monkeypatch.setattr(tag_editor, "same_audio", slow)
    tags = TagWindow(root, library.scan_album(album), window.prefs, on_saved=window._tags_saved,
                     busy=lambda: set(), confirm=lambda message, detail: True)
    window._tag_window = tags
    wait(root, lambda: str(tags.save_button.cget("state")) == "normal")
    tags.vars["album"].set("Changed")
    tags.save()
    wait(root, checking.is_set)
    assert tag_editor.temp_path(paths[0]).exists()          # the first song's copy is being checked
    window.quit()
    assert pump_until_closed(root, 10)
    assert sorted(p.name for p in album.iterdir()) == ["01.flac", "02.flac"]   # no copy left behind
    assert {library.read_track(p).album for p in paths} == {"Album"}           # and nothing half done


def test_a_tag_save_that_never_stops_doesnt_keep_the_player_open(make_window, monkeypatch):
    monkeypatch.setattr(ui, "QUIT_WAIT_MS", 300)
    window, root, player, errors = make_window()
    window._tag_history = _saving_window()
    window.quit()
    assert pump_until_closed(root, 1.5)


# -- cover art: a slow, older request never replaces a newer one ----------------------------------
def test_a_slow_cover_never_replaces_a_newer_one(monkeypatch):
    def cover_for(path, size):
        time.sleep(0.3 if path.name == "a.flac" else 0.0)
        return path.name
    monkeypatch.setattr(artwork, "cover_for", cover_for)
    w = SimpleNamespace(show_art=SimpleNamespace(get=lambda: True), art_size=100, _art_key=None, _art_result=None,
                        _art_track=None, _show_art=lambda image: None)
    PlayerWindow._request_art(w, Path("a.flac"))
    PlayerWindow._request_art(w, Path("b.flac"))
    for thread in [t for t in threading.enumerate() if t.name == "cover-art"]:
        thread.join(2)
    assert w._art_result == ((Path("b.flac"), 100), "b.flac")


# -- the clipping guard measures what's playing and next, and never stops ------------------------------
def test_the_clipping_guard_measures_only_what_is_wanted_and_survives_errors(monkeypatch):
    measured = []

    def true_peak(path):
        measured.append(path.name)
        if path.name == "odd.flac":
            raise KeyError("something unexpected")
        return 1.0
    monkeypatch.setattr(analysis, "true_peak", true_peak)
    w = SimpleNamespace(_peaks={}, _peak_jobs=queue.Queue(), _peak_wanted={Path("odd.flac"), Path("now.flac")})
    for name in ("skipped 1.flac", "skipped 2.flac", "odd.flac", "now.flac"):
        w._peak_jobs.put(Path(name))
    threading.Thread(target=PlayerWindow._measure_peaks, args=(w,), daemon=True).start()
    assert wait_for(lambda: Path("now.flac") in w._peaks)
    assert measured == ["odd.flac", "now.flac"] and w._peaks[Path("odd.flac")] == 0.0


def test_the_guard_wants_the_song_playing_and_the_next():
    w = SimpleNamespace(clip_guard=SimpleNamespace(get=lambda: True), _guard_for=None, _peaks={},
                        _peak_jobs=queue.Queue(), _peak_wanted=set(),
                        player=SimpleNamespace(following_path=lambda: Path("next.flac")))
    PlayerWindow._apply_guard(w, song("now.flac"), 0)
    assert w._peak_wanted == {Path("now.flac"), Path("next.flac")}
    assert [w._peak_jobs.get_nowait(), w._peak_jobs.get_nowait()] == [Path("now.flac"), Path("next.flac")]


# -- the mouse wheel over the transport -------------------------------------------------------------
@windows_only
def test_the_mouse_wheel_moves_the_volume_a_step_per_notch_not_per_message():
    player = SimpleNamespace(volume=0.5)
    player.set_volume = lambda value: setattr(player, "volume", max(0.0, min(1.0, value)))
    w = SimpleNamespace(player=player, volume_var=SimpleNamespace(set=lambda value: None),
                        _set_notice=lambda *args, **kwargs: None, _wheel_delta=0)
    for _ in range(12):                      # one swipe on a precision touchpad: one notch's worth
        PlayerWindow._wheel_volume(w, SimpleNamespace(delta=10))
    assert player.volume == pytest.approx(0.55)
    PlayerWindow._wheel_volume(w, SimpleNamespace(delta=0))
    assert player.volume == pytest.approx(0.55)
    PlayerWindow._wheel_volume(w, SimpleNamespace(delta=-120))
    assert player.volume == pytest.approx(0.50)


# -- after a tag edit, tabs are compared by identity (cheap), not field by field -------------------
def test_after_a_tag_edit_big_tabs_are_not_compared_field_by_field(monkeypatch):
    edited = song("C:/Edited/01.flac", "Old")
    big = Tab("folder", "C:/Big", tracks=[song(f"C:/Big/{i}.flac") for i in range(2000)])
    album = Tab("album", "C:/Edited", tracks=[edited])
    monkeypatch.setattr(ui, "read_track", lambda path: song(path, "New"))
    compared = []
    monkeypatch.setattr(Track, "__eq__", lambda self, other: compared.append(1) or self is other)
    w = SimpleNamespace(tabs=[big, album], player=SimpleNamespace(up_next=lambda: []), _art_track=None,
                        _show_current_tab=lambda: None, _index=lambda: SimpleNamespace(update=lambda paths: None),
                        _index_updated=False, TAG_FIELDS=PlayerWindow.TAG_FIELDS)
    big.info_requested = album.info_requested = True
    PlayerWindow._apply_new_tags(w, [Path("C:/Edited/01.flac")])
    assert edited.title == "New" and album.info_requested is False and big.info_requested is True
    assert compared == []


# -- crash.log: errors on other threads and at start-up --------------------------------------------
def test_errors_on_other_threads_reach_crash_log(tmp_path, monkeypatch):
    from ironbark import __main__ as start

    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")
    monkeypatch.setattr(start.faulthandler, "enable", lambda **kwargs: None)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)   # put back afterwards
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(start, "_crash_file", None)
    start._start_crash_log()
    try:
        worker = threading.Thread(target=lambda: 1 / 0, name="cover-art")
        worker.start()
        worker.join()
        try:
            raise LookupError("not caught anywhere")
        except LookupError:
            sys.excepthook(*sys.exc_info())
        text = (tmp_path / "app" / "crash.log").read_text(encoding="utf-8")
    finally:
        start._crash_file.close()
    assert "cover-art" in text and "ZeroDivisionError" in text
    assert "LookupError: not caught anywhere" in text


def test_a_failure_while_starting_up_is_logged(monkeypatch):
    from ironbark import __main__ as start

    log = io.StringIO()
    monkeypatch.setattr(start, "_crash_file", log)
    monkeypatch.setattr(start, "_start_crash_log", lambda: None)
    monkeypatch.setattr(start.settings, "carry_over_old_folder", lambda: False)
    monkeypatch.setattr(start, "_enable_crisp_text_on_windows", lambda: None)
    monkeypatch.setattr(start.appicon, "set_app_id", lambda: None)
    monkeypatch.setattr(start.tk, "Tk", lambda: SimpleNamespace(report_callback_exception=None))
    monkeypatch.setattr(start, "Player", lambda: None)

    def broken(*args, **kwargs):
        raise RuntimeError("the window couldn't be built")
    monkeypatch.setattr(start, "PlayerWindow", broken)
    with pytest.raises(RuntimeError):
        start.main([])
    assert "RuntimeError: the window couldn't be built" in log.getvalue()


# -- window placement: enough of the top strip must show ----------------------------------------------
def test_a_window_needs_more_than_a_sliver_on_screen():
    work = (0, 0, 3840, 2088)                                   # a monitor's area above the taskbar
    assert placement.shows_enough((100, 100, 520, 140), work)
    assert not placement.shows_enough((100, 2086, 520, 2126), work)   # 2 px above the taskbar
    assert not placement.shows_enough((3838, 100, 4258, 140), work)   # 2 px at the right edge
    assert not placement.shows_enough((5000, 100, 5420, 140), work)   # not on it at all


@windows_only
def test_a_window_mostly_behind_the_taskbar_is_not_restored_there():
    import ctypes
    import ctypes.wintypes as wt

    work = wt.RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(work), 0)   # SPI_GETWORKAREA
    if ctypes.windll.user32.GetSystemMetrics(1) - work.bottom < placement.TITLE_BAR:   # SM_CYSCREEN
        pytest.skip("needs the taskbar along the bottom of the main monitor")
    assert placement.on_screen(work.left + 10, work.top + 10, 420)
    assert not placement.on_screen(work.left + 10, work.bottom - 2, 420)


# -- the default library folder: Windows' own Music folder ------------------------------------------
@windows_only
def test_the_default_library_is_windows_own_music_folder():
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
        value, _kind = winreg.QueryValueEx(key, "My Music")
    expected = Path(os.path.expandvars(value))
    assert ui.default_music_folder() == expected
    assert PlayerWindow._library_root(SimpleNamespace(prefs={"library_folder": ""})) == expected
    assert PlayerWindow._library_root(SimpleNamespace(prefs={"library_folder": "D:/Music"})) == Path("D:/Music")


def test_without_a_known_music_folder_it_falls_back_to_home(monkeypatch):
    monkeypatch.setattr(ui, "_known_music_folder", lambda: None)
    ui.default_music_folder.cache_clear()
    try:
        assert ui.default_music_folder() == Path.home() / "Music"
    finally:
        ui.default_music_folder.cache_clear()
