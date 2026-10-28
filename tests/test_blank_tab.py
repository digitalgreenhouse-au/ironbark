"""The + button opens a blank tab, as a browser does; and the player always opens as the full window."""

from __future__ import annotations

from pathlib import Path

from test_light_mini import FITS_A_SMALL_SCREEN, _pump, _where, make_window  # noqa: F401  (make_window is a fixture)

from ironbark import settings
from ironbark.library import Track
from ironbark.ui import Tab, place_new_tab


def test_the_player_opens_as_the_full_window_even_if_left_as_the_mini_strip(make_window):
    window, root, player, errors = make_window(geometry=FITS_A_SMALL_SCREEN, mini_mode=True,
                                               mini_geometry="600x130+200+90")
    assert not window.mini_mode and window.bar.winfo_ismapped() and not window.mini.winfo_ismapped()
    assert _where(root)[2:] == (120, 120)                       # the full window's own place
    window.toggle_mini()                                         # the strip is still there for the asking…
    _pump(root, 0.4)
    assert window.mini_mode and _where(root)[2:] == (200, 90)   # …at its own last place
    window.toggle_mini()
    _pump(root, 0.4)
    window._save_if_changed()
    assert settings.load()["mini_mode"] is True                  # (an old setting: neither read nor written now)
    assert errors == []


def test_place_new_tab_uses_up_a_blank_tab_or_goes_at_the_end():
    tabs = [Tab("library", ""), Tab("new", "")]
    album = Tab("album", r"C:\Music\Blue")
    library = tabs[0]
    assert place_new_tab(tabs, 1, album) == 1 and tabs == [library, album]
    other = Tab("album", r"C:\Music\Red")
    assert place_new_tab(tabs, 0, other) == 2 and tabs[2] is other   # (the Library tab isn't blank)
    assert place_new_tab([], -1, album) == 0


def test_tabs_are_told_apart_as_tabs_not_by_their_contents():
    first, second = Tab("new", ""), Tab("new", "")
    assert first != second and [first].count(second) == 0
    tabs = [first, second]
    assert tabs.index(second) == 1
    tabs.remove(second)
    assert tabs == [first] and second not in tabs


def test_the_plus_button_opens_a_blank_tab_and_the_first_thing_opened_takes_its_place(make_window, tmp_path):
    window, root, player, errors = make_window()
    assert window.tabs[0].kind == "album" and window.current == 0
    window.new_blank_tab()                                       # the + button, or Ctrl+T
    _pump(root, 0.4)
    blank = window.tabs[1]
    assert blank.kind == "new" and blank.blank and blank.title == "New tab" and window.current == 1
    assert window.bar._titles[-1] == "New tab"
    assert blank.tracks == [] and not blank.loading              # (nothing on disk was looked at)
    # Its page: the empty view's buttons, in place of the cover, transport and track list.
    assert window.empty_view.winfo_ismapped() and not window.head.winfo_manager()
    assert not window.transport.winfo_manager() and not window.playlist.frame.winfo_manager()
    for button in (window.empty_button, window.empty_folder_button, window.empty_playlist_button,
                   window.empty_file_button):
        assert button.winfo_ismapped()
    assert window.status_label.winfo_manager() == "pack"        # the status line stays
    assert window._notice == ""                                  # (no "No music files in New tab")
    assert window.title_label.cget("text") == "New tab"          # (what the mini strip would show)

    # The first thing opened from it takes its place on the strip: no third tab.
    album = tmp_path / "Music" / "Second"                        # (not the album already open in tab 1)
    album.mkdir()
    window.open_tab("album", album)
    _pump(root, 0.6)
    assert len(window.tabs) == 2 and window.tabs[1].kind == "album" and Path(window.tabs[1].path) == album
    assert window.current == 1 and not window.empty_view.winfo_manager()
    assert window.playlist.frame.winfo_ismapped() and window.transport.winfo_ismapped()
    assert window.playing_tab == 0 and player.calls == []        # the music in the first tab never noticed

    # Closing a blank tab leaves nothing to reopen.
    window.new_blank_tab()
    _pump(root, 0.3)
    assert len(window.tabs) == 3 and window.current == 2
    before = list(window._closed_tabs)
    window.close_tab(2)
    _pump(root, 0.3)
    assert len(window.tabs) == 2 and window._closed_tabs == before and not window._notice.startswith("Closed")
    assert errors == []


def test_a_blank_tab_is_saved_and_comes_back(make_window):
    window, root, player, errors = make_window()
    window.new_blank_tab()
    _pump(root, 0.3)
    assert window.tabs[1].source() == {"kind": "new", "path": "", "query": ""}
    window.prefs["tabs"] = [t.source() for t in window.tabs]
    window.tabs, window.current, window.playing_tab = [], -1, None
    window._restore_tabs(None)
    _pump(root, 0.3)
    kinds = [t.kind for t in window.tabs]
    assert kinds == ["library", "album", "new"] and window.tabs[-1].title == "New tab"
    # Dropped files go into a blank tab too.
    window.select_tab(2)
    _pump(root, 0.3)
    song = Path(window.tabs[1].path) / "01 Song.flac"
    window.open_files_tab([song])
    _pump(root, 0.5)
    assert [t.kind for t in window.tabs] == ["library", "album", "files"] and window.current == 2
    assert errors == []


def test_space_on_a_blank_tab_does_nothing_to_the_music(make_window):
    window, root, player, errors = make_window()
    window.tabs.append(Tab("album", str(Path(window.tabs[0].path)),
                           tracks=[Track(path=Path(window.tabs[0].path) / "x.flac", title="x", duration=1.0)]))
    window.new_blank_tab()
    _pump(root, 0.3)
    window.toggle_play()
    assert player.calls == [] and window.playing_tab == 0
    assert errors == []


def _blank_on_show(window, root):
    window.new_blank_tab()
    _pump(root, 0.3)
    blank = window.tabs[-1]
    assert blank.blank and window.current == len(window.tabs) - 1
    return blank


def _shows_its_songs(window, tab, count):
    assert not tab.blank and tab.title != "New tab"
    assert len(tab.tracks) == count
    assert not window.empty_view.winfo_manager() and window.playlist.frame.winfo_ismapped()
    assert window.bar._titles[window._tab_index(tab)] == tab.title


def test_songs_added_to_a_blank_tab_are_shown_and_it_becomes_a_real_tab(make_window, tmp_path):
    # Library → Add to tab, songs dragged from a track list, files dropped from Explorer: each adds
    # songs to the tab through the same call.
    window, root, player, errors = make_window()
    blank = _blank_on_show(window, root)
    song = Path(window.tabs[0].path) / "01 Song.flac"
    track = Track(path=song, title="Song One", artist="Artist", album="Album", duration=200.0)
    message = window._add_tracks_to_tab(blank, [track], [str(song)])
    _pump(root, 0.4)
    assert message == f"Added 1 song to “{blank.title}”"
    assert blank.kind == "files" and Path(blank.path) == song.parent and blank.extras == [str(song)]
    _shows_its_songs(window, blank, 1)
    assert blank.title == "Album"
    assert blank.source()["kind"] == "files"                    # (saved as what it now is)
    before = list(window._closed_tabs)
    window.close_tab(window._tab_index(blank))                   # and it can be reopened, as any tab
    _pump(root, 0.3)
    assert len(window._closed_tabs) == len(before) + 1
    assert errors == []


def test_an_album_added_to_a_blank_tab_opens_there(make_window, tmp_path):
    # Library → Add to tab with an album selected, or an album dragged onto a blank tab.
    window, root, player, errors = make_window()
    blank = _blank_on_show(window, root)
    album = Path(window.tabs[0].path)
    told = []
    window.add_album_to_current(album, on_done=told.append, tab=blank)
    _pump(root, 0.8)
    assert told and told[0].startswith("Opened “")
    assert blank.kind == "album" and Path(blank.path) == album and blank.extras == []
    _shows_its_songs(window, blank, 1)
    assert errors == []


def test_songs_dropped_on_a_blank_tab_that_isnt_on_show(make_window, tmp_path):
    window, root, player, errors = make_window()
    blank = _blank_on_show(window, root)
    window.select_tab(0)
    _pump(root, 0.3)
    song = Path(window.tabs[0].path) / "01 Song.flac"
    window._add_tracks_to_tab(blank, [Track(path=song, title="Song One", album="Album")], [str(song)])
    _pump(root, 0.3)
    assert blank.kind == "files" and len(blank.tracks) == 1
    assert window.bar._titles[window._tab_index(blank)] != "New tab"
    assert errors == []
