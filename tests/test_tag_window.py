"""Tag editing's windows and the player's ways in (0.16.0). With the Experimental switch off there is
no tag-editing UI at all — no menu item, no key, no Library item; on, the Edit tags window saves
through tag_editor and the songs in open tabs show the new tags straight away. Tk smoke tests,
skipped without a display."""
import gc
import time
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
from ironbark.tagio.flac import FLACFile

from ironbark import experiments, layout, library, settings, tag_editor, theme
from ironbark.engine import State
from ironbark.library_window import LibraryWindow
from ironbark.playlist_view import PlaylistView
from ironbark.tag_window import TagHistoryWindow, TagWindow
from ironbark.favourites import Favourites
from ironbark.ui import PlayerWindow, Tab

ON = {"experiments": {tag_editor.EXPERIMENT: True}}


def flac(path: Path, **tags) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, (np.random.default_rng(3).standard_normal((4410, 2)) * 2000).astype(np.int16), 44100,
             subtype="PCM_16", format="FLAC")
    audio = FLACFile(path)
    for key, value in tags.items():
        audio[key] = value
    audio.save()
    return path


@pytest.fixture
def root():
    try:
        root = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"no display ({error})")
    root.withdraw()
    theme.apply(root)
    yield root
    root.destroy()
    del root
    gc.collect()  # on this thread (Tk objects freed on another thread abort Python)


@pytest.fixture
def music(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")
    album = tmp_path / "Music" / "Album"
    flac(album / "01.flac", title="One", artist="X", album="Album", tracknumber="1")
    flac(album / "02.flac", title="Two", artist="X", album="Album", tracknumber="2")
    return album


@pytest.fixture
def popups(monkeypatch):
    """The right-click menus that would have opened (0.21.0: in-app popups.PopupMenu, not tk.Menu)."""
    from ironbark import popups as menus

    shown = []
    monkeypatch.setattr(menus.PopupMenu, "popup", lambda self, x, y: shown.append(self))
    return shown


def labels(menu) -> list[str]:
    return menu.labels()


def window(root, prefs, tracks, **extra):
    """Just enough of a PlayerWindow for the track menu and the tag-editing methods."""
    w = SimpleNamespace(root=root, prefs=prefs, tabs=[Tab("album", "C:/Music/Album")], current=0, _tier=layout.COLUMN,
                        playlist=SimpleNamespace(columns=(), wanted=lambda: (), shown=(),
                                                 selected_rows=lambda: [0, 1]),
                        opened=[], _tag_window=None, _tag_history=None, sleep_timer=None,
                        player=SimpleNamespace(stop_after=None), favourites=Favourites(lambda: None), **extra)
    w.tabs[0].tracks = tracks
    for name in ("_menu", "_track_menu", "_columns_menu", "_sleep_menu", "stop_after_current", "_tag_editing",
                 "edit_selected_tags", "edit_album_tags", "show_tag_history"):
        setattr(w, name, getattr(PlayerWindow, name).__get__(w))
    w._known_playlists = lambda: []
    w.edit_tags = lambda tracks: w.opened.append(("edit", len(tracks))) if w._tag_editing() else None
    return w


def test_with_the_switch_off_there_is_no_tag_editing_ui(root, music, popups):
    tracks = library.scan_album(music)
    w = window(root, {}, tracks)
    w._track_menu([0], SimpleNamespace(x_root=0, y_root=0))
    assert not any("tag" in label.lower() for label in labels(popups[-1]))
    w.edit_selected_tags()                                   # the E key
    PlayerWindow.edit_tags(w, tracks)                        # even called directly: nothing opens
    w.show_tag_history()
    w.edit_album_tags(music)
    root.update()
    assert w.opened == [] and w._tag_window is None and w._tag_history is None
    assert root.winfo_children() == [] or all(not isinstance(c, tk.Toplevel) for c in root.winfo_children())


def test_with_the_switch_on_the_menu_and_key_offer_it(root, music, popups):
    tracks = library.scan_album(music)
    w = window(root, dict(ON), tracks)
    w._track_menu([0, 1], SimpleNamespace(x_root=0, y_root=0))
    assert "Edit tags of these 2 songs…" in labels(popups[-1]) and "Tag history (undo)…" in labels(popups[-1])
    w.edit_selected_tags()
    assert w.opened == [("edit", 2)]
    experiments.set_enabled(w.prefs, tag_editor.EXPERIMENT, False)  # switched off again: gone at once
    w._track_menu([0], SimpleNamespace(x_root=0, y_root=0))
    assert not any("tag" in label.lower() for label in labels(popups[-1]))


def test_the_library_offers_it_only_while_switched_on(root, music, popups):
    switch = {"on": False}
    edited = []
    lib = LibraryWindow(root, music.parent, None, on_choose=lambda p: None, on_root_changed=lambda p: None,
                        on_load_all=lambda r, q: None, on_edit_tags=edited.append,
                        can_edit_tags=lambda: switch["on"])
    deadline = time.monotonic() + 20
    while lib._refresh is not None and time.monotonic() < deadline:
        root.update()
        time.sleep(0.02)
    iid = next(iter(lib._rows))
    lib.tree.see(iid)
    root.update()
    x, y, _w, h = lib.tree.bbox(iid)
    event = SimpleNamespace(x=x + 5, y=y + h // 2, x_root=0, y_root=0)
    lib._context(event)
    assert "Edit tags…" not in labels(popups[-1])
    switch["on"] = True
    lib._context(event)
    assert "Edit tags…" in labels(popups[-1])
    lib.close()


def wait(root, until, seconds=30):
    deadline = time.monotonic() + seconds
    while not until() and time.monotonic() < deadline:
        root.update()
        time.sleep(0.02)
    assert until()


def test_edit_an_album_then_undo_it(root, music):
    tracks = library.scan_album(music)
    saves, asked = [], []
    tw = TagWindow(root, tracks, {}, on_saved=saves.append, busy=lambda: set(),
                   confirm=lambda message, detail: asked.append(message) or True)
    wait(root, lambda: str(tw.save_button.cget("state")) == "normal")
    assert tw.vars["title"].get() == tag_editor.VARIOUS and str(tw.entries["title"].cget("style")) == "Various.TEntry"
    assert tw.vars["album"].get() == "Album" and tw.vars["artist"].get() == "X"
    tw.vars["album"].set("Better album")
    tw.vars["year"].set("1977")
    tw.save()
    assert asked == ["Save changes to 2 files?"]
    wait(root, lambda: saves)
    assert saves[0].saved == [t.path for t in tracks]
    assert [library.read_track(t.path).title for t in tracks] == ["One", "Two"]    # (various): left alone
    assert {library.read_track(t.path).album for t in tracks} == {"Better album"}
    assert {library.read_track(t.path).year for t in tracks} == {1977}

    history = TagHistoryWindow(root, {}, on_saved=saves.append, busy=lambda: set(),
                               confirm=lambda message, detail: True)
    wait(root, lambda: history.loading is None)          # (read on a worker thread)
    assert len(history.entries) == 1 and history.tree.set("0", "what").startswith("Album and Year")
    history.tree.selection_set("0")
    root.update()
    history.undo()
    wait(root, lambda: len(saves) == 2)
    assert {library.read_track(t.path).album for t in tracks} == {"Album"}
    wait(root, lambda: history.loading is None and len(history.entries) == 2)
    assert history.tree.set("1", "state") == "undone"
    history.close()


def test_typing_a_bad_number_is_refused_and_nothing_changed_is_said(root, music):
    tracks = library.scan_album(music)[:1]
    tw = TagWindow(root, tracks, {}, on_saved=lambda s: None, busy=lambda: set(),
                   confirm=lambda message, detail: pytest.fail("should not ask"))
    wait(root, lambda: str(tw.save_button.cget("state")) == "normal")
    tw.save()
    assert tw.status.cget("text").startswith("Nothing changed")
    tw.vars["track"].set("first")
    tw.save()
    assert "Track: a number" in tw.status.cget("text")
    tw.close()


def test_new_tags_show_in_open_tabs_straight_away(tmp_path, music, monkeypatch):
    tracks = library.scan_album(music)
    tab = Tab("album", str(music))
    tab.tracks = tracks
    updated, shown = [], []
    index = SimpleNamespace(update=lambda paths: updated.append(paths))
    w = SimpleNamespace(tabs=[tab], player=SimpleNamespace(up_next=lambda: []), _art_track=None,
                        _show_current_tab=lambda: shown.append(True), _index=lambda: index, _index_updated=False,
                        TAG_FIELDS=PlayerWindow.TAG_FIELDS)
    audio = FLACFile(tracks[0].path)
    audio["title"], audio["album"] = "Renamed", "New album"
    audio.save()
    same_object = tracks[0]
    PlayerWindow._apply_new_tags(w, [tracks[0].path])
    assert same_object.title == "Renamed" and same_object.album == "New album" and tab.tracks[0] is same_object
    assert tracks[1].title == "Two" and shown == [True] and tab.info_requested is False
    deadline = time.monotonic() + 5
    while not w._index_updated and time.monotonic() < deadline:
        time.sleep(0.01)
    assert updated == [[tracks[0].path]]  # the index re-reads it: a change, never something new


def test_waiting_saves_go_through_once_the_player_moves_on(music):
    tracks = library.scan_album(music)
    save = tag_editor.TagSave(tag_editor.edit_jobs(tracks[:1], tag_editor.Edit({"album": "Later"})), "x")
    save.run(busy=lambda p: True)
    playing = SimpleNamespace(state=State.PLAYING, track=tracks[0])
    applied, notices = [], []
    w = SimpleNamespace(_tag_waiting=[save], player=SimpleNamespace(status=lambda: playing,
                                                                    following_path=lambda: None),
                        _apply_new_tags=applied.append, _set_notice=lambda text, ms=0: notices.append(text))
    w._busy_files = lambda: PlayerWindow._busy_files(w)
    PlayerWindow._retry_tag_saves(w)
    assert applied == [] and w._tag_waiting == [save]       # still playing: not forced
    playing.state = State.STOPPED
    PlayerWindow._retry_tag_saves(w)
    assert applied == [[tracks[0].path]] and w._tag_waiting == [] and notices == ["Saved the new tags for “01”"]
    assert library.read_track(tracks[0].path).album == "Later"


def test_switching_it_off_closes_its_windows(root, music):
    tracks = library.scan_album(music)
    prefs = dict(ON)
    tw = TagWindow(root, tracks, prefs, on_saved=lambda s: None, busy=lambda: set())
    hw = TagHistoryWindow(root, prefs, on_saved=lambda s: None, busy=lambda: set())
    w = SimpleNamespace(prefs=prefs, experiment_vars={tag_editor.EXPERIMENT: tk.BooleanVar(root, value=False)},
                        _tag_window=tw, _tag_history=hw, _paint_version=lambda: None)
    w._tag_editing = lambda: PlayerWindow._tag_editing(w)
    PlayerWindow._toggle_experiment(w, tag_editor.EXPERIMENT)
    root.update()
    assert not experiments.enabled(prefs, tag_editor.EXPERIMENT)
    assert not tw.win.winfo_exists() and not hw.win.winfo_exists()
