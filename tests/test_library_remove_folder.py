"""Taking a folder out of the Library (Settings → Library → Remove) takes its songs out of the main
Library tab too: they used to stay listed there as "(file not found)" — the files were still on disk,
only no longer in the library."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import soundfile as sf
from tagging import write_vorbis
from test_light_mini import _pump, make_window  # noqa: F401  (make_window is a fixture)


def _wait(root, ready, seconds: float = 15.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if ready():
            return True
        _pump(root, 0.05)
    return ready()


def _flac(path: Path, **tags) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, np.zeros((4410, 2), dtype=np.int16), 44100, subtype="PCM_16")
    write_vorbis(path, **tags)
    return path


def _titles(tracks) -> list[str]:
    return [tracks[i].title for i in range(len(tracks))]


def test_a_folder_taken_out_of_the_library_leaves_the_library_tab(make_window, tmp_path):
    from ironbark.popups import Prompt

    window, root, _player, errors = make_window()
    first, other = tmp_path / "Music", tmp_path / "More"
    _flac(first / "Kept" / "01 Kept.flac", TITLE="Kept Song", ARTIST="Someone", ALBUM="Kept")
    gone = _flac(other / "Leaving" / "01 Leaving.flac", TITLE="Leaving Song", ARTIST="Someone", ALBUM="Leaving")
    window._set_library_folders([first, other])
    window._index().refresh()                                   # (both folders read)
    main = window._main_tab(make=True)
    window.select_tab(main)
    tab = window.tabs[main]
    assert _wait(root, lambda: tab.tracks is not None and not tab.loading and "Leaving Song" in _titles(tab.tracks))

    root.after(100, lambda: Prompt.stack[-1]._chosen(0))       # it asks first: Remove
    window._remove_library_folder(other)
    window._index().refresh()                                   # (the look that stores the change)
    _pump(root, 0.5)
    assert _wait(root, lambda: tab.tracks is not None and not tab.loading and "Leaving Song" not in _titles(tab.tracks)), \
        _titles(tab.tracks)
    assert "Kept Song" in _titles(tab.tracks)
    assert not any(tab.tracks[i].missing for i in range(len(tab.tracks)))   # no "(file not found)" rows
    assert gone.exists() and not errors                         # (the music file itself is untouched)


def test_the_song_playing_from_a_folder_taken_out_isnt_cut_off(make_window, tmp_path):
    """The Library tab is playing a song of the folder taken out: the tab keeps its list (the song
    plays on) and takes the new one only once the song has moved on — a while later."""
    from ironbark import ui
    from ironbark.popups import Prompt

    window, root, player, errors = make_window()
    first, other = tmp_path / "Music", tmp_path / "More"
    _flac(first / "Kept" / "01 Kept.flac", TITLE="Kept Song", ARTIST="Someone", ALBUM="Kept")
    _flac(other / "Leaving" / "01 Leaving.flac", TITLE="Leaving Song", ARTIST="Someone", ALBUM="Leaving")
    window._set_library_folders([first, other])
    window._index().refresh()
    main = window._main_tab(make=True)
    window.select_tab(main)
    tab = window.tabs[main]
    assert _wait(root, lambda: tab.tracks is not None and not tab.loading and "Leaving Song" in _titles(tab.tracks))
    leaving = _titles(tab.tracks).index("Leaving Song")
    player.track = tab.tracks[leaving]                          # it's playing from the Library tab
    window.playing_tab = main
    handed = []
    real_set_tracks = player.set_tracks
    player.set_tracks = lambda tracks, **kw: (handed.append(len(tracks)), real_set_tracks(tracks, **kw))

    root.after(100, lambda: Prompt.stack[-1]._chosen(0))
    window._remove_library_folder(other)
    window._index().refresh()
    assert _wait(root, lambda: tab.follow_after > time.monotonic())   # the check found its song gone…
    assert "Leaving Song" in _titles(tab.tracks)                       # …and left the list alone
    assert handed == []                                                # (the player wasn't handed a list without it)

    player.track = tab.tracks[_titles(tab.tracks).index("Kept Song")]   # the next song came on
    tab.follow_after = 0.0                                               # (the wait, cut short)
    window._follow_library()
    assert _wait(root, lambda: "Leaving Song" not in _titles(tab.tracks)), _titles(tab.tracks)
    assert ui.FOLLOW_WAIT_S >= 10 and not errors


def test_settings_rebuild_the_library_starts_it_again_and_the_library_tab_follows(make_window, tmp_path):
    """Settings → Library → Rebuild the library…: asked first, then the library is read again from
    scratch; an album deleted from the disk leaves the Library tab, and the status line says so."""
    import shutil

    from ironbark.popups import Prompt

    window, root, _player, errors = make_window()
    music = tmp_path / "Music"
    _flac(music / "Kept" / "01 Kept.flac", TITLE="Kept Song", ARTIST="Someone", ALBUM="Kept")
    _flac(music / "Deleted" / "01 Deleted.flac", TITLE="Deleted Song", ARTIST="Someone", ALBUM="Deleted")
    window._index().refresh()
    main = window._main_tab(make=True)
    window.select_tab(main)
    tab = window.tabs[main]
    assert _wait(root, lambda: tab.tracks is not None and not tab.loading and "Deleted Song" in _titles(tab.tracks))
    shutil.rmtree(music / "Deleted")                            # gone from the disk

    root.after(100, lambda: Prompt.stack[-1]._chosen(1))       # asked first: Keep it — nothing happens
    window._rebuild_library()
    _pump(root, 0.3)
    assert "Rebuil" not in window._notice
    root.after(100, lambda: Prompt.stack[-1]._chosen(0))       # Rebuild
    window._rebuild_library()
    assert _wait(root, lambda: "Rebuilt the library" in window._notice, 20), window._notice
    assert _wait(root, lambda: not tab.loading and "Deleted Song" not in _titles(tab.tracks)), _titles(tab.tracks)
    assert "Kept Song" in _titles(tab.tracks)
    assert not any(tab.tracks[i].missing for i in range(len(tab.tracks))) and not errors


def test_changing_the_library_folder_leaves_only_the_new_folders_songs(make_window, tmp_path, monkeypatch):
    """Settings → Library → Change…: the Library tab lists the new folder's songs, and none of the
    old one's (they used to stay as "(file not found)" rows)."""
    from ironbark import ui

    window, root, _player, errors = make_window()
    old, new = tmp_path / "Old", tmp_path / "New"
    _flac(old / "Before" / "01 Before.flac", TITLE="Old Song", ARTIST="Someone", ALBUM="Before")
    _flac(new / "After" / "01 After.flac", TITLE="New Song", ARTIST="Someone", ALBUM="After")
    window._set_library_folders([old])
    window._index().refresh()
    main = window._main_tab(make=True)
    window.select_tab(main)
    tab = window.tabs[main]
    assert _wait(root, lambda: tab.tracks is not None and not tab.loading and "Old Song" in _titles(tab.tracks))

    monkeypatch.setattr(ui.filedialog, "askdirectory", lambda **kw: str(new))
    window._change_library_folder()
    window._index().refresh()
    _pump(root, 0.5)
    assert _wait(root, lambda: tab.tracks is not None and not tab.loading and _titles(tab.tracks) == ["New Song"]), \
        _titles(tab.tracks)
    assert not any(tab.tracks[i].missing for i in range(len(tab.tracks))) and not errors


def test_moving_the_music_to_a_new_folder_lists_it_from_there(make_window, tmp_path, monkeypatch):
    """The music moved (copied to a NAS, the old copy deleted) and Change… points the Library at its
    new place: the same songs, now from the new folder, none of them "(file not found)"."""
    import shutil

    from ironbark import ui

    window, root, _player, errors = make_window()
    old, new = tmp_path / "Old", tmp_path / "NAS" / "MUSIC"
    _flac(old / "Album" / "01 One.flac", TITLE="One", ARTIST="Someone", ALBUM="Album")
    _flac(old / "Album" / "02 Two.flac", TITLE="Two", ARTIST="Someone", ALBUM="Album")
    window._set_library_folders([old])
    window._index().refresh()
    main = window._main_tab(make=True)
    window.select_tab(main)
    tab = window.tabs[main]
    assert _wait(root, lambda: tab.tracks is not None and not tab.loading and len(tab.tracks) == 2)

    new.parent.mkdir(parents=True)
    shutil.copytree(old, new, copy_function=shutil.copy2)       # (copy2: the same sizes and times)
    shutil.rmtree(old)
    monkeypatch.setattr(ui.filedialog, "askdirectory", lambda **kw: str(new))
    window._change_library_folder()
    window._index().refresh()
    _pump(root, 0.5)

    def settled():
        return (tab.tracks is not None and not tab.loading and len(tab.tracks) == 2
                and all(str(tab.tracks[i].path).startswith(str(new)) for i in range(2)))

    assert _wait(root, settled), [str(tab.tracks[i].path) for i in range(len(tab.tracks))]
    assert not any(tab.tracks[i].missing for i in range(2)) and not errors
