"""Spotting new music (0.17.0): the watcher brings the saved index up to date from a cheap folder
walk — added / changed / removed files, files still being copied left until they've settled — and
works out which albums are new (until opened). A tag edit  counts as changed, never new.
All in a temp folder with temp settings."""
import os
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from ironbark.tagio.flac import FLACFile

from ironbark import library, library_index, playlists, settings, watcher
from ironbark.library_index import LibraryIndex
from ironbark.tag_editor import Edit, TagSave, edit_jobs
from ironbark.ui import PlayerWindow
from ironbark.watcher import Report, Settling, Watcher

RNG = np.random.default_rng(12)
HOUR = 3600.0


def flac(path: Path, title="Song", album="Album", **tags) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, (RNG.standard_normal((4410, 2)) * 3000).astype(np.int16), 44100, subtype="PCM_16", format="FLAC")
    audio = FLACFile(path)
    audio["title"], audio["album"], audio["artist"] = title, album, "Band"
    for key, value in tags.items():
        audio[key] = value
    audio.save()
    return path


def age(path: Path, seconds: float = HOUR):
    """Make a file look as if it was last written `seconds` ago (a finished copy keeps its old time)."""
    when = time.time_ns() - int(seconds * 1e9)
    os.utime(path, ns=(when, when))


@pytest.fixture
def music(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")
    root = tmp_path / "Music"
    for path in (flac(root / "Old Album" / "01.flac", "One", "Old Album"),
                 flac(root / "Old Album" / "02.flac", "Two", "Old Album"),
                 flac(root / "Other" / "01.flac", "Three", "Other")):
        age(path)
    return root


def build(music) -> tuple[LibraryIndex, Watcher]:
    index = LibraryIndex(music)
    look = Watcher(lambda: index)
    first = look.check()
    assert first.first_build and len(first.diff.added) == 3 and not first.news   # nothing "new" on a first build
    return index, look


def test_added_changed_and_removed(music):
    index, look = build(music)
    since = time.time() - 1
    new = flac(music / "Fresh" / "01.flac", "Fresh", "Fresh")
    age(new, 60)                                   # a copy that finished a minute ago
    two = music / "Old Album" / "02.flac"
    audio = FLACFile(two)
    audio["title"] = "Two (remastered)"
    audio.save()
    age(two, 30)
    (music / "Other" / "01.flac").unlink()
    report = look.check()
    assert report.diff.added == [new] and report.diff.changed == [two]
    assert report.diff.removed == [music / "Other" / "01.flac"] and report.news and not report.error
    # the Library: the new album is in, the removed one is gone, the changed song re-read
    names = {a.name for a in index.albums()}
    assert names == {"Old Album", "Fresh"}
    assert any(t.title == "Two (remastered)" for t in index.tracks())
    assert watcher.new_albums(index, since, {}) == [new.parent]
    assert watcher.arrived(report, [new.parent]) == [new.parent]
    assert watcher.note(report, 1) == "New music: 1 new album in the Library · 1 song gone from the Library"
    assert look.check().diff.nothing                # and it's all been taken in


def test_nothing_is_read_when_nothing_changed(music, monkeypatch):
    index, look = build(music)
    monkeypatch.setattr(library_index, "read_track", lambda p: pytest.fail(f"read {p}"))
    report = look.check()
    assert report.diff.nothing and not report.news and watcher.note(report, 0) == ""


def test_a_half_copied_file_waits_until_it_has_settled(music):
    index, look = build(music)
    whole = flac(music.parent / "elsewhere" / "01.flac", "Arriving", "Arriving").read_bytes()
    target = music / "Arriving" / "01.flac"
    target.parent.mkdir()
    target.write_bytes(whole[: len(whole) // 2])     # the copy is half way
    report = look.check()
    assert report.diff.waiting == [target] and not report.diff.added and not report.news
    assert str(target) not in index.entries          # not in the Library yet
    with open(target, "ab") as f:                    # the copy carries on…
        f.write(whole[len(whole) // 2:])
    age(target, 120)                                 # …and finishes with the original's modified time
    assert look.check().diff.waiting == [target]     # it moved since last time: one more look
    report = look.check()
    assert report.diff.added == [target] and not report.diff.waiting
    assert next(t for t in index.tracks() if t.path == target).title == "Arriving"


def test_a_file_growing_with_an_old_modified_time_still_waits(music):
    """Some copy tools set the modified time first: the size still growing is enough to wait."""
    index, look = build(music)
    target = music / "Slow" / "01.flac"
    data = flac(music.parent / "src" / "01.flac", "Slow", "Slow").read_bytes()
    target.parent.mkdir()
    target.write_bytes(data[:1000])
    age(target)
    look.settling.last[str(target)] = (500, os.stat(target).st_mtime_ns)   # smaller at the last look
    assert look.check().diff.waiting == [target]
    target.write_bytes(data)
    age(target)
    assert look.check().diff.waiting == [target]     # still different from last time
    assert look.check().diff.added == [target]


def test_a_changed_file_still_being_written_keeps_its_old_entry(music):
    index, look = build(music)
    one = music / "Old Album" / "01.flac"
    audio = FLACFile(one)
    audio["title"] = "Being rewritten"
    audio.save()                                      # modified just now
    report = look.check()
    assert report.diff.waiting == [one] and not report.diff.changed and not report.diff.removed
    assert next(t for t in index.tracks() if t.path == one).title == "One"   # as it was
    age(one, 30)
    look.check()                                      # moved since the last look: one more
    assert look.check().diff.changed == [one]
    assert next(t for t in index.tracks() if t.path == one).title == "Being rewritten"


def test_a_tag_edit_counts_as_changed_never_new(music):
    index, look = build(music)
    since = time.time() - 1
    one = music / "Old Album" / "01.flac"
    save = TagSave(edit_jobs([library.read_track(one)], Edit({"title": "Retitled"})), "Retitled")
    save.run()
    assert save.saved == [one]
    # the watcher gets there first (the player hasn't told the index yet): changed, never added
    look.settling.settle_s = 0
    report = look.check()
    assert report.diff.changed == [one] and not report.diff.added and not report.news
    assert watcher.new_albums(index, since, {}) == []
    # the player telling the index (library_index.update), as ui does after a save
    audio = FLACFile(one)
    audio["title"] = "Retitled again"
    audio.save()
    diff = index.update([one])
    assert diff.changed == [one] and not diff.added
    assert watcher.new_albums(index, since, {}) == []
    assert not any(p.name.endswith(".dgtmp") for p in one.parent.iterdir())   # no temp copy left to find


def test_new_albums_until_opened_and_again_when_more_arrives(music):
    index, look = build(music)
    since = time.time() - 1
    a = flac(music / "A" / "01.flac", "a", "A")
    age(a, 60)
    look.check()
    b = flac(music / "B" / "CD1" / "01.flac", "b", "B")   # a disc folder: the album is B
    age(b, 60)
    look.check()
    assert watcher.new_albums(index, since, {}) == [music / "B", music / "A"]     # newest first
    opened = {str(music / "B"): time.time()}
    assert watcher.new_albums(index, since, opened) == [music / "A"]
    time.sleep(0.01)
    b2 = flac(music / "B" / "CD2" / "01.flac", "b2", "B")   # a second disc arrives later
    age(b2, 60)
    look.check()
    assert music / "B" in watcher.new_albums(index, since, opened)
    # nothing that was there before watching began counts, however it's opened
    assert watcher.new_albums(index, time.time() + 1, {}) == []
    # tidying: forget albums that left the library and openings from before watching began
    tidy = watcher.tidy_opened({str(music / "B"): time.time(), str(music / "Gone"): time.time(),
                                str(music / "A"): since - 5}, index, since)
    assert list(tidy) == [str(music / "B")]


def test_removed_files_leave_the_library_but_playlists_keep_them(music, tmp_path):
    index, look = build(music)
    gone = music / "Other" / "01.flac"
    playlist = tmp_path / "list.m3u8"
    playlists.write(playlist, [library.read_track(gone), library.read_track(music / "Old Album" / "01.flac")])
    gone.unlink()
    report = look.check()
    assert report.diff.removed == [gone]
    assert "Other" not in {a.name for a in index.albums()}
    tracks = PlayerWindow._read_playlist(playlist)    # how a playlist tab reads it
    assert [t.path for t in tracks] == [gone, music / "Old Album" / "01.flac"]
    assert [t.missing for t in tracks] == [True, False]   # listed as "file not found"


def test_settling_rules():
    now = 1_000_000.0
    settling = Settling(10.0, clock=lambda: now)
    old = int((now - 60) * 1e9)
    assert not settling("a", (100, old, 0))                       # finished long ago
    assert settling("b", (100, int((now - 2) * 1e9), 0))          # written two seconds ago
    assert settling("c", (100, int((now + 5) * 1e9), 0))          # a moment in the future: wait
    assert not settling("d", (100, int((now + HOUR) * 1e9), 0))   # a clock far ahead: don't wait forever
    settling.begin()
    settling("b", (200, old, 0))                                  # b grew: still waiting
    settling.end()
    assert set(settling.last) == {"b"}                            # c wasn't asked about: forgotten


def test_the_thread_reports_and_stops(music):
    index = LibraryIndex(music)
    look = Watcher(lambda: index, every_s=0.05, recheck_s=0.05)
    look.start()
    deadline = time.monotonic() + 20
    reports: list[Report] = []
    while len(reports) < 2 and time.monotonic() < deadline:
        reports += look.drain()
        time.sleep(0.02)
    look.stop()
    assert reports[0].first_build and len(reports[0].diff.added) == 3
    assert reports[1].diff.nothing
    time.sleep(0.2)
    assert not look.running


def test_a_missing_library_folder_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")
    look = Watcher(lambda: LibraryIndex(tmp_path / "nowhere"))
    report = look.check()
    assert report.error and not report.news and watcher.note(report, 0) == ""


def test_watching_is_on_by_default():
    assert settings.DEFAULTS["watch_music"] is True


# -- the player and the Library window (Tk smoke tests, skipped without a display) ----------------
@pytest.fixture
def tk_root():
    import gc
    import tkinter as tk
    from ironbark import theme
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


def player_side(index, look, prefs):
    """The player's new-music methods on a stand-in (no audio, no main window)."""
    from types import SimpleNamespace
    notices = []
    w = SimpleNamespace(prefs=prefs, watcher=look, _new_albums=[], _library=None, _notice="",
                        _index=lambda: index, notices=notices)
    w._set_notice = lambda text, ms=0: (notices.append(text), setattr(w, "_notice", text))
    for name in ("_new_music_state", "_update_new_albums", "_watch_reports", "_mark_opened"):
        setattr(w, name, getattr(PlayerWindow, name).__get__(w))
    return w


def test_the_player_notes_new_albums_and_forgets_them_once_opened(music, tk_root, monkeypatch):
    from ironbark.library_window import NEW_DOT, LibraryWindow
    index, look = build(music)
    prefs = {"new_music": {"since": time.time() - 1, "opened": {}}}
    w = player_side(index, look, prefs)
    for name in ("Alpha", "Beta", "Gamma"):
        age(flac(music / name / "01.flac", name, name), 60)
    look.results.put(look.check())
    w._watch_reports()
    assert w.notices == ["New music: 3 new albums in the Library"]
    assert set(w._new_albums) == {music / "Alpha", music / "Beta", music / "Gamma"}
    monkeypatch.setattr(LibraryWindow, "_sorts", {})       # (the sort is kept for the session)
    monkeypatch.setattr(library_index, "_shared", index)   # the one index the player and the window share
    library = LibraryWindow(tk_root, music, None, lambda p: None, lambda p: None, lambda r, q: None,
                            new_albums=lambda: w._new_albums)
    w._library = library
    assert "3 new albums" in library.status.cget("text")
    names = [library.tree.set(iid, "album") for iid in library.tree.get_children()]
    assert names == [NEW_DOT + "Alpha", NEW_DOT + "Beta", NEW_DOT + "Gamma", "Old Album", "Other"]
    assert library._view_labels["recent"].cget("text") == "Recently added ●"
    library.set_view("recent")
    assert library.sort == ("added", True)        # Recently added: newest first
    w._mark_opened(music / "Beta")                 # opened in a tab: the dot goes
    assert str(music / "Beta") in prefs["new_music"]["opened"]
    names = [library.tree.set(iid, "album") for iid in library.tree.get_children()]
    assert NEW_DOT + "Beta" not in names and "Beta" in names and "2 new albums" in library.status.cget("text")
    w._mark_opened(music / "Alpha")
    w._mark_opened(music / "Gamma")
    assert library._view_labels["recent"].cget("text") == "Recently added"
    assert "new album" not in library.status.cget("text")
    # a later start: the opened ones stay opened (kept in the settings)
    w2 = player_side(index, look, prefs)
    w2._update_new_albums()
    assert w2._new_albums == []
    library.close()


def test_watch_for_new_music_is_in_settings_library(tk_root, tmp_path, monkeypatch):
    """Settings → Library has the slider, on by default; switching it off stops the watcher."""
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "app" / "settings.json")
    import inspect
    source = inspect.getsource(PlayerWindow._build_settings)
    assert '"Watch for new music"' in source and 'self._caption(b, "Library")' in source
    from types import SimpleNamespace
    import tkinter as tk
    look = Watcher(lambda: LibraryIndex(tmp_path / "Music"), every_s=60)
    w = SimpleNamespace(prefs={"new_music": None}, watcher=look, watch_music=tk.BooleanVar(value=True))
    for name in ("_new_music_state", "_start_watching", "_toggle_watch"):
        setattr(w, name, getattr(PlayerWindow, name).__get__(w))
    w._toggle_watch()
    assert look.running and isinstance(w.prefs["new_music"]["since"], float) and w.prefs["watch_music"] is True
    w.watch_music.set(False)
    w._toggle_watch()
    assert not look.running and w.prefs["watch_music"] is False
