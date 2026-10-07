"""Drag and drop: the drop rules (dragdrop.py) from plain lists of paths, and how the window
carries them out (adding to a tab, new tabs, reordering a playlist) without opening a window."""

import gc
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from ironbark import dragdrop, playlists
from ironbark.engine import State
from ironbark.library import Track
from ironbark.ui import PlayerWindow, Tab


def touch(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def flac(path: Path, seconds: float = 0.1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, np.zeros(int(44100 * seconds), dtype=np.int16), 44100, subtype="PCM_16", format="FLAC")
    return path


@pytest.fixture
def music(tmp_path):
    album = tmp_path / "Album"
    flac(album / "01 One.flac")
    flac(album / "02 Two.flac")
    touch(album / "cover.jpg")
    touch(album / "Album.cue", b'FILE "01 One.flac" WAVE\n')
    loose = tmp_path / "Loose"
    flac(loose / "b.flac")
    flac(loose / "a.flac")
    touch(loose / "notes.txt")
    touch(loose / "Mix.M3U8", b"#EXTM3U\n")
    touch(loose / "old.m3u", b"")
    (tmp_path / "Empty" / "Sub").mkdir(parents=True)
    return tmp_path


# -- the rules -------------------------------------------------------------------------------
def test_folders_files_and_playlists_are_sorted_and_files_keep_the_order_dropped(music):
    plan = dragdrop.plan_drop([music / "Loose" / "b.flac", music / "Album", music / "Loose" / "a.flac",
                               music / "Loose" / "Mix.M3U8", music / "Loose" / "old.m3u"])
    assert plan.folders == [music / "Album"]
    assert plan.files == [music / "Loose" / "b.flac", music / "Loose" / "a.flac"]  # as dropped, not sorted
    assert plan.playlists == [music / "Loose" / "Mix.M3U8", music / "Loose" / "old.m3u"]
    assert not plan.not_music and not plan.missing and not plan.nothing and plan.note() == ""


def test_anything_that_isnt_music_is_skipped_with_a_one_line_note(music):
    plan = dragdrop.plan_drop([music / "Loose" / "notes.txt", music / "Album" / "cover.jpg",
                               music / "Loose" / "a.flac"])
    assert plan.files == [music / "Loose" / "a.flac"]
    assert plan.note() == "2 files skipped — not music"
    one = dragdrop.plan_drop([music / "Loose" / "notes.txt"])
    assert one.nothing and one.note() == "1 file skipped — not music"


def test_missing_items_empty_folders_and_repeats(music):
    plan = dragdrop.plan_drop([music / "gone.flac", music / "Empty", music / "Album", str(music / "Album")])
    assert plan.missing == [music / "gone.flac"]
    assert plan.empty == [music / "Empty"]
    assert plan.folders == [music / "Album"]  # dropped twice, counted once
    assert plan.note() == "1 folder skipped — no music inside · 1 item not found"


def test_has_music_can_be_supplied(tmp_path):
    (tmp_path / "A").mkdir()
    assert dragdrop.plan_drop([tmp_path / "A"], has_music=lambda folder: True).folders == [tmp_path / "A"]


def test_a_drop_on_the_window_opens_a_tab_per_folder_one_for_loose_files_and_one_per_playlist(music):
    plan = dragdrop.plan_drop([music / "Album", music / "Loose" / "a.flac", music / "Loose", music / "Loose" / "b.flac",
                               music / "Loose" / "Mix.M3U8"])
    actions = dragdrop.actions_for(plan, on_tab=False)
    assert [a.kind for a in actions] == ["open_folder", "open_folder", "open_files", "open_playlist"]
    assert [a.path for a in actions if a.kind == "open_folder"] == [music / "Album", music / "Loose"]
    files = next(a for a in actions if a.kind == "open_files")
    assert files.paths == (music / "Loose" / "a.flac", music / "Loose" / "b.flac")


def test_a_drop_on_a_tab_adds_everything_to_it(music):
    plan = dragdrop.plan_drop([music / "Album", music / "Loose" / "a.flac", music / "Loose" / "Mix.M3U8"])
    actions = dragdrop.actions_for(plan, on_tab=True)
    assert len(actions) == 1 and actions[0].kind == "add"
    assert actions[0].folders == (music / "Album",)
    assert actions[0].paths == (music / "Loose" / "a.flac",)
    assert actions[0].playlists == (music / "Loose" / "Mix.M3U8",)
    assert dragdrop.actions_for(dragdrop.plan_drop([music / "Loose" / "notes.txt"]), on_tab=True) == []


def test_a_cue_sheet_opens_its_folder_until_cue_playback_exists(music):
    plan = dragdrop.plan_drop([music / "Album" / "Album.cue"])
    assert plan.cues == [music / "Album" / "Album.cue"] and not plan.nothing
    assert dragdrop.cue_folder(music / "Album" / "Album.cue") == music / "Album"   # the CUE hook
    assert dragdrop.actions_for(plan, on_tab=False) == [dragdrop.Action("open_folder", path=music / "Album")]
    # The folder and its cue dropped together: one tab, not two.
    both = dragdrop.plan_drop([music / "Album", music / "Album" / "Album.cue"])
    assert [a.path for a in dragdrop.actions_for(both, on_tab=False)] == [music / "Album"]


@pytest.mark.parametrize("source,target,expected", [
    (0, 2, ["b", "a", "c", "d"]),    # down one: the line sits above "c"
    (0, 4, ["b", "c", "d", "a"]),    # to the end
    (3, 0, ["d", "a", "b", "c"]),    # to the top
    (2, 1, ["a", "c", "b", "d"]),
    (1, 1, ["a", "b", "c", "d"]),    # dropped where it was
    (1, 2, ["a", "b", "c", "d"]),    # just below itself: still where it was
    (5, 0, ["a", "b", "c", "d"]),    # out of range: unchanged
])
def test_move_item(source, target, expected):
    assert dragdrop.move_item(list("abcd"), source, target) == expected


# -- carrying it out (window code, no window) --------------------------------------------------
def songs(folder, *names):
    return [Track(Path(folder) / f"{n}.flac", n, artist="X", duration=60) for n in names]


def test_reordering_by_drag_keeps_the_m3u8_correct(tmp_path):
    path = tmp_path / "Mix.m3u8"
    tracks = songs(tmp_path, "a", "b", "c", "d")
    playlists.write(path, tracks)
    tab = Tab("playlist", str(path))
    tab.tracks = list(tracks)
    selected = []

    def save(p, new):
        playlists.write(p, new)
        tab.tracks = list(new)
        return True

    w = SimpleNamespace(tabs=[tab], current=0, _save_playlist=save, playlist=SimpleNamespace(select=selected.append),
                        _set_notice=lambda text, ms=0: None)
    for name in ("_edit_playlist", "_remember_edit", "_tab_index", "undo"):
        setattr(w, name, getattr(PlayerWindow, name).__get__(w))
    PlayerWindow._reorder_playlist(w, 0, 3)  # drag "a" down to just above "d"
    assert [p.name for p in playlists.read(path)] == ["b.flac", "c.flac", "a.flac", "d.flac"]
    assert selected == [2]
    PlayerWindow._reorder_playlist(w, 3, 0)  # "d" to the top
    assert [p.name for p in playlists.read(path)] == ["d.flac", "b.flac", "c.flac", "a.flac"]
    before = path.read_bytes()
    PlayerWindow._reorder_playlist(w, 1, 1)  # no change: the file isn't rewritten
    assert path.read_bytes() == before
    w.undo()                                 # Ctrl+Z: the last drag is put back
    assert [p.name for p in playlists.read(path)] == ["b.flac", "c.flac", "a.flac", "d.flac"]


def _window_for_adding(tab):
    calls = SimpleNamespace(set_tracks=[], shown=0, added=[])
    w = SimpleNamespace(tabs=[tab], current=0, playing_tab=0,
                        player=SimpleNamespace(set_tracks=lambda t, keep_playing=False: calls.set_tracks.append(
                            (len(t), keep_playing))),
                        add_to_playlist=lambda path, tracks: calls.added.append((path, tracks)))
    w._tab_index = lambda t: PlayerWindow._tab_index(w, t)
    w._show_current_tab = lambda: setattr(calls, "shown", calls.shown + 1)
    return w, calls


def test_adding_to_an_album_tab_skips_songs_it_has_and_keeps_the_music_going(tmp_path):
    tab = Tab("album", str(tmp_path / "A"))
    tab.tracks = songs(tmp_path / "A", "1", "2")
    w, calls = _window_for_adding(tab)
    extra = songs(tmp_path / "B", "x", "y")
    message = PlayerWindow._add_tracks_to_tab(w, tab, tab.tracks[:1] + extra, [str(tmp_path / "B")])
    assert message == "Added 2 songs to “A”"
    assert [t.title for t in tab.tracks] == ["1", "2", "x", "y"]
    assert tab.extras == [str(tmp_path / "B")]       # saved with the tab, so it's there after a restart
    assert calls.set_tracks == [(4, True)] and calls.shown == 1
    assert PlayerWindow._add_tracks_to_tab(w, tab, extra, [str(tmp_path / "B")]) == "Already in “A”"
    assert tab.extras == [str(tmp_path / "B")]


def test_adding_to_a_playlist_tab_saves_into_its_file(tmp_path):
    tab = Tab("playlist", str(tmp_path / "Mix.m3u8"))
    tab.tracks = []
    w, calls = _window_for_adding(tab)
    extra = songs(tmp_path / "B", "x")
    assert PlayerWindow._add_tracks_to_tab(w, tab, extra, []) == "Added 1 song to “Mix”"
    assert calls.added == [(tmp_path / "Mix.m3u8", extra)]


def test_songs_dropped_on_a_tab_are_read_with_extras_to_find_them_again(music):
    plan = dragdrop.plan_drop([music / "Album", music / "Loose" / "a.flac"])
    w = SimpleNamespace(_read_playlist=PlayerWindow._read_playlist)
    tracks, extras = PlayerWindow._read_dropped(w, plan)
    assert [t.path.name for t in tracks] == ["01 One.flac", "02 Two.flac", "a.flac"]
    assert extras == [str(music / "Album"), str(music / "Loose" / "a.flac")]


def test_a_drop_on_the_window_opens_tabs_and_only_the_first_may_play(music):
    opened = []
    w = SimpleNamespace(player=SimpleNamespace(status=lambda: SimpleNamespace(state=State.STOPPED)),
                        open_tab=lambda kind, path, play=False: opened.append((kind, Path(path).name, play)),
                        open_files_tab=lambda files, play=False: opened.append(("files", len(files), play)))
    plan = dragdrop.plan_drop([music / "Album", music / "Loose" / "a.flac", music / "Loose" / "Mix.M3U8"])
    assert PlayerWindow._open_dropped(w, plan) == "Opened 3 tabs"
    assert opened == [("folder", "Album", True), ("files", 1, False), ("playlist", "Mix.M3U8", False)]
    # While music plays, nothing new starts.
    opened.clear()
    w.player = SimpleNamespace(status=lambda: SimpleNamespace(state=State.PLAYING))
    assert PlayerWindow._open_dropped(w, dragdrop.plan_drop([music / "Album"])) == "Opened Album"
    assert opened == [("folder", "Album", False)]


def test_a_tab_of_dropped_files_is_saved_and_restored(music):
    tab = Tab("files", str(music / "Loose"))
    tab.extras = [str(music / "Loose" / "b.flac"), str(music / "Loose" / "a.flac")]
    saved = tab.source()
    assert saved["kind"] == "files" and saved["extras"] == tab.extras


# -- the track list's drag (Tk) ----------------------------------------------------------------
@pytest.fixture
def root():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display ({exc})")
    root.geometry("300x300+0+0")
    yield root
    root.destroy()
    del root
    gc.collect()  # here, on the main thread: a Tk interpreter freed by another thread's collection aborts


def test_drop_index_and_line_in_a_playlist(root):
    from ironbark.playlist_view import PlaylistView

    view = PlaylistView(root, on_play=lambda i: None)
    view.set_tracks(songs("C:/x", "a", "b", "c"), flat=True)
    root.update()
    box = view.tree.bbox("1")
    top = view.tree.winfo_rooty() + box[1]
    assert view.drop_index(top + 2) == 1                 # upper half of "b": above it
    assert view.drop_index(top + box[3] - 2) == 2        # lower half: below it
    last = view.tree.bbox("2")
    assert view.drop_index(view.tree.winfo_rooty() + last[1] + last[3] + 20) == 3   # under the list: last
    view.show_drop_line(1)
    root.update()
    assert view._drop_line.winfo_ismapped()
    view.show_drop_line(None)
    root.update()
    assert not view._drop_line.winfo_ismapped()


def test_tab_bar_finds_and_lights_up_the_tab_under_the_pointer(root):
    from ironbark.tabbar import TabBar

    bar = TabBar(root, root, on_select=lambda i: None, on_close=lambda i: None, on_new=lambda: None,
                 on_settings=lambda: None, on_double=lambda: None, on_quit=lambda: None)
    bar.pack(fill="x")
    root.update()
    bar.set_tabs(["One", "Two"], 0, None)
    root.update()
    second = bar._tab_widgets[1]
    x, y = second.winfo_rootx() + 5, second.winfo_rooty() + 5
    assert bar.tab_at(x, y) == 1
    assert bar.tab_at(x, y + 500) is None
    bar.highlight(1)
    root.update()
    assert int(bar._tab_widgets[1].cget("highlightthickness")) >= 2
    bar.highlight(None)
    root.update()
    assert int(bar._tab_widgets[1].cget("highlightthickness")) == 0
