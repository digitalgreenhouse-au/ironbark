"""The keyboard (keys.py): one table of shortcuts used for the bindings, the Keyboard shortcuts sheet,
the README and the menus' hints. Keys are case-blind (Caps Lock, Shift on letters), single keys keep
out of the way while you type, every Ironbark window answers them, a window's own keys win, and the
new keys work: volume / mute / 100%, jumps, long seeks, Ctrl+1…9, undo of playlist changes, Edit tags'
Enter / Ctrl+S / Esc. Tk tests use the real window on temp settings with a fake player (nothing is
played, nothing of the user's is read or written) and are skipped without a display."""

from __future__ import annotations

import re
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from types import SimpleNamespace

import pytest

from ironbark import help_window, keys
from ironbark.keys import ALT, CONTROL, SHIFT
from ironbark.library import Track

from test_light_mini import _pump, make_window  # noqa: F401  (make_window is a fixture)

REPO = Path(__file__).resolve().parents[1]
LOCK = 0x2
_KEYSYMS = {"space": "space", "return": "Return", "left": "Left", "right": "Right", "up": "Up", "down": "Down",
            "prior": "Prior", "next": "Next", "tab": "Tab", "delete": "Delete", "comma": "comma",
            "question": "question"}


def press(text: str, caps: bool = False) -> tuple[str, int]:
    """A key from the table as Tk on Windows reports it: (keysym, state). Shift (or Caps Lock) on a
    letter gives its capital, as Windows does."""
    mods, base = keys.parse(text)
    state = ((CONTROL if "Ctrl" in mods else 0) | (ALT if "Alt" in mods else 0)
             | (SHIFT if "Shift" in mods or base == "question" else 0) | (LOCK if caps else 0))
    keysym = _KEYSYMS.get(base)
    if keysym is None:
        if base[:1] == "f" and base[1:].isdigit():
            keysym = base.upper()
        elif len(base) == 1 and base.isalpha() and ("Shift" in mods) != caps:
            keysym = base.upper()
        else:
            keysym = base
    return keysym, state


# -- the table (no display needed) ---------------------------------------------------------------
def test_keys_are_normalised_caps_lock_ignored():
    assert keys.normalise("T", CONTROL | LOCK) == (frozenset({"Ctrl"}), "t")      # Caps Lock + Ctrl+T
    assert keys.normalise("T", CONTROL | SHIFT) == (frozenset({"Ctrl", "Shift"}), "t")
    assert keys.normalise("S", LOCK) == (frozenset(), "s")
    assert keys.normalise("s", SHIFT | LOCK) == (frozenset({"Shift"}), "s")      # Caps Lock + Shift: lower case
    assert keys.normalise("ISO_Left_Tab", CONTROL) == (frozenset({"Ctrl", "Shift"}), "tab")
    assert keys.normalise("Up", ALT | 0x8) == (frozenset({"Alt"}), "up")         # (Num Lock is ignored)
    assert keys.normalise("KP_Enter", CONTROL) == (frozenset({"Ctrl"}), "return")
    assert keys.parse("Ctrl+Shift+PgUp") == (frozenset({"Ctrl", "Shift"}), "prior")
    assert keys.parse("Ctrl+,") == (frozenset({"Ctrl"}), "comma") and keys.parse("?") == (frozenset(), "question")


def test_shift_counts_only_where_the_table_says_so():
    found = keys.build_map()

    def ids(text, shift=False):
        mods, base = keys.parse(text)
        return [a.id for a in found.get((mods | ({"Shift"} if shift else set()), base), [])]

    assert ids("S") == ids("S", shift=True) == ["shuffle"]                     # S and Shift+S both shuffle
    assert ids("Ctrl+T") == ["new_tab"] and ids("Ctrl+Shift+T") == ["reopen_tab"]
    assert ids("Ctrl+O") == ["open_folder"] and ids("Ctrl+Shift+O") == ["open_playlist"]
    assert ids("?", shift=True) == ["help"]                                    # ? is Shift+/
    assert ids("5", shift=True) == []                                          # Shift+5 is "%", not a jump
    assert ids("Left", shift=True) == ["seek_long"] and ids("Left") == ["seek"]


def test_the_table_is_consistent_and_keeps_the_old_keys():
    assert len({a.id for a in keys.ACTIONS}) == len(keys.ACTIONS)
    for action in keys.ACTIONS:
        assert action.group in keys.GROUPS and action.scope in keys.SCOPES, action.id
        assert action.keys and action.label, action.id
    for key, actions in keys.build_map().items():
        scopes = [a.scope for a in actions]
        assert len(scopes) == len(set(scopes)), f"{key} means two things in one scope: {[a.id for a in actions]}"
        # A key the whole app uses never also means something narrower (it would act differently by window).
        assert not ("app" in scopes and len(scopes) > 1), [a.id for a in actions]
    kept = {"Space": "play", "N": "next", "P": "previous", "Left": "seek", "Right": "seek", "S": "shuffle",
            "R": "repeat", "I": "now_playing", "M": "meters", "B": "pcm", "C": "clipping", "F": "spectrogram",
            "O": "scope", "D": "compare", "X": "abx", "T": "test_signals", "E": "edit_tags", "Z": "mini",
            "Ctrl+L": "library", "Ctrl+O": "open_folder", "Ctrl+N": "new_playlist", "Ctrl+F": "search_tab",
            "Ctrl+W": "close_tab", "Ctrl+Tab": "next_tab", "Ctrl+Shift+Tab": "previous_tab",
            "Ctrl+Shift+T": "reopen_tab", "Ctrl+Shift+PgUp": "move_tab", "Ctrl+Shift+PgDn": "move_tab",
            "Ctrl+Shift+H": "snap_home", "Ctrl+,": "settings"}
    found = keys.build_map()
    for text, action_id in kept.items():
        assert action_id in [a.id for a in found[keys.parse(text)]], text
    reserved = [a.id for a in keys.ACTIONS if a.reserved]
    assert reserved == []
    assert "full_screen" not in keys.BY_ID   # no full screen: the window is a side player
    assert keys.BY_ID["find"].keys == ("Ctrl+K", "Ctrl+Shift+F")


def test_single_keys_wait_while_you_type():
    typing = keys.works_while_typing
    for text in ("N", "Space", "Left", "Shift+Left", "5", "?", "Delete", "Enter", "Ctrl+Left", "Ctrl+Right", "Ctrl+Z"):
        assert not typing(keys.parse(text)), text
    for text in ("Ctrl+Space", "Ctrl+Up", "Ctrl+Shift+Down", "Ctrl+L", "Ctrl+Tab", "F1", "F5", "Ctrl+S", "Ctrl+Q"):
        assert typing(keys.parse(text)), text


def test_menu_hints_come_from_the_table():
    assert keys.hint("move_song", 0) == "Alt+↑" and keys.hint("move_song", 1) == "Alt+↓"
    assert keys.hint("remove") == "Delete" and keys.hint("list_queue") == "Ctrl+Enter"
    assert keys.keys_text(keys.BY_ID["volume"]) == "Ctrl+↑ / Ctrl+↓"
    assert keys.keys_text(keys.BY_ID["next"]) == "N · Ctrl+→"
    source = (REPO / "ironbark" / "ui.py").read_text(encoding="utf-8")
    assert '"Ctrl+Up"' not in source and '"Ctrl+Down"' not in source   # (reordering moved to Alt+↑ / ↓)
    assert "def _add_help_buttons(self" in source                       # the hook for more Help buttons


def test_readme_keyboard_section_is_the_table():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    section = re.search(r"^## Keyboard\n.*?(?=^## )", readme, flags=re.S | re.M).group(0)
    assert section.rstrip() == keys.readme_section().rstrip()
    for action in keys.listed():
        assert f"| {keys.keys_text(action)} | {action.label} |" in section, action.id
    assert "F11" not in section and "full screen" not in section.lower()


def test_mit_licence_and_privacy_match_the_files():
    assert help_window.MIT_LICENCE == (REPO / "LICENSE").read_text(encoding="utf-8")
    assert help_window.copyright_line() == "© 2026 Digital Greenhouse"
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    privacy = re.search(r"^## Privacy\n\n(.*?)\n+## ", readme, flags=re.S | re.M).group(1)
    paragraphs = []   # (the whole section: its paragraphs, and a list's items as "• " lines)
    for block in privacy.strip().split("\n\n"):
        items = re.split(r"\n(?=- )", block)
        paragraphs.append("\n".join("• " + " ".join(item[2:].split()) if item.startswith("- ")
                                    else " ".join(item.split()) for item in items))
    plain = re.sub(r"[*`]", "", "\n\n".join(paragraphs))
    assert help_window.PRIVACY == plain


def test_licence_files_from_source_and_from_the_installed_program(tmp_path, monkeypatch):
    found = help_window.licence_documents()                       # from source: the repo's own files
    names = [path.name for _name, path in found]
    assert names[0] == "THIRD_PARTY_LICENSES.md" and found[0][0] == "Third-party licences (summary)"
    for text in (REPO / "packaging" / "licenses").glob("*.txt"):
        assert text.name in names
    assert "Ironbark-LICENSE.txt" not in names
    # The installed program: the licenses folder beside Ironbark.exe (sub-folders for Python packages).
    folder = tmp_path / "Ironbark" / "licenses"
    (folder / "numpy").mkdir(parents=True)
    for name in ("THIRD_PARTY_LICENSES.md", "Ironbark-LICENSE.txt", "zlib-LICENSE.txt", "numpy/LICENSE.txt"):
        (folder / name).write_text(name, encoding="utf-8")
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys.executable", str(tmp_path / "Ironbark" / "Ironbark.exe"))
    assert [name for name, _p in help_window.licence_documents()] == [
        "Third-party licences (summary)", "numpy · LICENSE", "zlib-LICENSE"]
    assert help_window.read_document(folder / "zlib-LICENSE.txt") == "zlib-LICENSE.txt"
    assert help_window.read_document(None) == help_window.MIT_LICENCE
    monkeypatch.setattr("sys.executable", str(tmp_path / "Elsewhere" / "Ironbark.exe"))
    assert help_window.licence_documents() == []                  # none found: an empty list, no error


def test_version_info_says_what_a_bug_report_needs():
    text = help_window.version_info(prefs={"experiments": {"tape_reels": True}})
    lines = text.splitlines()
    from ironbark import __version__

    assert lines[0].startswith(f"Ironbark {__version__}")
    for start in ("Python ", "Tk ", "libsndfile", "PortAudio"):
        assert any(line.startswith(start) for line in lines), start
    assert lines[-1] == "Experiments on: tape_reels"
    assert help_window.version_info(prefs={}).splitlines()[-1] == "Experiments on: none"


def test_volume_words():
    from ironbark.ui import volume_words

    assert volume_words(1.0) == "100% · untouched" and volume_words(0.0) == "0% · muted"
    assert volume_words(0.75) == "75% · −5.0 dB"     # (squared: the slider's 75% is −5 dB, a minus sign)


# -- Tk: the real window ----------------------------------------------------------------------------
def _run(window, widget, text: str, caps: bool = False) -> bool:
    keysym, state = press(text, caps)
    return window.keys.run(widget, keysym, state)


def _resolved(window, widget, text: str):
    found = window.keys.resolve(widget, *press(text))
    return found[0].id if found else None


def _focus(root, widget):
    widget.focus_force()
    _pump(root, 0.08)
    if root.focus_get() is not widget:
        pytest.skip("this session can't give a window the keyboard")


def test_every_window_answers_every_key_in_its_scope(make_window, monkeypatch):
    window, root, player, errors = make_window()
    window.show_details()
    window.show_meters()
    window.show_pcm()
    window.show_clipping()
    window.show_spectrogram()
    window.show_scope()
    window.show_compare()
    window.show_abx()
    window.show_testsignals()
    window.show_lyrics()
    window.open_library()
    window.show_help()
    window.show_about()
    window.edit_tags(window.tabs[0].tracks)
    window.show_tag_history()
    _pump(root, 0.5)
    windows = {"main": root}
    for name in ("_now_playing", "_meters", "_pcm", "_clip", "_spectrogram", "_scope", "_compare", "_abx",
                 "_testsignals", "_lyrics", "_library", "_help", "_about", "_tag_window", "_tag_history"):
        windows[name] = getattr(window, name).win
    for name, win in windows.items():
        assert window.keys.attached(win), name
        tags = win.bindtags()
        assert tags[tags.index(str(win)) + 1] == keys.TAG, name           # straight after the window's own tag
        scopes = window.keys.scopes_of(win)
        for action in keys.ACTIONS:
            if action.scope not in scopes:
                continue
            for text in action.keys:
                assert _resolved(window, win, text) == action.id, (name, text)
        assert "app" in scopes
    # The track list's own keys, on the track list.
    for action in (a for a in keys.ACTIONS if a.scope == "list"):
        for text in action.keys:
            assert _resolved(window, window.playlist.tree, text) == action.id, text
    assert window.keys.scopes_of(windows["_library"]) == {"app", "library"}
    assert window.keys.scopes_of(windows["_tag_window"]) == {"app", "tags"}
    assert _resolved(window, root, "Ctrl+F") == "search_tab"
    assert _resolved(window, windows["_library"], "Ctrl+F") == "library_search"
    assert _resolved(window, windows["_meters"], "Ctrl+W") is None        # a tab key: only the player's
    # Every widget in a window carries the tag (after the window's own), so the keys reach it.
    for widget in (window.playlist.tree, window.playlist.entry, window._library.tree, window._help.tree):
        tags = widget.bindtags()
        top = str(widget.winfo_toplevel())
        assert tags[tags.index(top) + 1] == keys.TAG
    assert not errors


def test_keys_reach_other_windows_and_wait_while_typing(make_window):
    window, root, player, errors = make_window()
    window.show_meters()
    _pump(root, 0.2)
    meters = window._meters.win
    _focus(root, meters)
    meters.event_generate("<KeyPress-n>")
    meters.event_generate("<KeyPress-S>")                  # Shift+S / Caps Lock: still shuffle
    _pump(root, 0.05)
    assert player.calls == ["next"] and player.shuffle
    entry = window.playlist.entry                           # typing in the search box…
    _focus(root, entry)
    entry.event_generate("<KeyPress-n>")
    entry.event_generate("<KeyPress-space>")
    _pump(root, 0.05)
    assert player.calls == ["next"] and window.playlist.query.get() == "n "
    entry.event_generate("<Control-Key-space>")            # …but Ctrl+Space still plays / pauses
    _pump(root, 0.05)
    assert player.calls == ["next", "toggle_pause"]
    assert not errors


def test_a_windows_own_keys_win(make_window):
    window, root, player, errors = make_window()
    window.show_abx()
    _pump(root, 0.2)
    abx = window._abx
    heard = []
    abx.hear = lambda letter: heard.append(letter)
    abx.stop = lambda: heard.append("stop")
    _focus(root, abx.win)
    for sequence in ("<KeyPress-b>", "<KeyPress-X>", "<KeyPress-space>"):
        abx.win.event_generate(sequence)
    _pump(root, 0.05)
    assert heard == ["B", "X", "A"]                        # (Space: hear the last letter, or A)
    assert window._pcm is None and player.calls == []      # not the PCM stream, not play / pause
    # Clipping: A / B / D / Space are its own (Hear it is always showing since 0.37.0).
    window.show_clipping()
    _pump(root, 0.2)
    clip = window._clip
    clip.hear = lambda letter: heard.append(f"clip {letter}")
    _focus(root, clip.win)
    clip.win.event_generate("<KeyPress-b>")
    _pump(root, 0.1)
    assert heard[-1] == "clip B" and window._pcm is None      # B is the window's, not the app's PCM stream
    assert not errors


def test_list_keys_dont_also_move_the_list(make_window):
    window, root, player, errors = make_window()
    tree = window.playlist.tree
    window._show_current_tab()
    _pump(root, 0.3)
    tree.selection_set("0")
    tree.focus("0")
    volume = player.volume
    _focus(root, tree)
    tree.event_generate("<Control-Key-Down>")              # volume down, the selection stays
    tree.event_generate("<KeyPress-space>")                # play / pause, once (no fold as well)
    _pump(root, 0.05)
    assert player.volume == pytest.approx(volume - 0.05) and tree.selection() == ("0",)
    assert player.calls == ["toggle_pause"]
    assert window._notice.startswith("Volume ")
    assert not errors


def test_volume_mute_jump_and_seek_keys(make_window):
    window, root, player, errors = make_window()
    seeks = []
    player.seek = lambda seconds: seeks.append(seconds)
    player.set_volume(0.8)
    assert _run(window, root, "Ctrl+Up") and player.volume == pytest.approx(0.85)
    assert window._notice == "Volume 85% · −2.8 dB" and window.volume_var.get() == pytest.approx(85)
    _run(window, root, "Ctrl+Down")
    _run(window, root, "Ctrl+Down")
    assert player.volume == pytest.approx(0.75)
    assert _run(window, root, "Ctrl+Shift+Down") and player.volume == 0.0 and window._notice.startswith("Muted")
    _run(window, root, "Ctrl+Shift+Down")                   # unmute: back where it was
    assert player.volume == pytest.approx(0.75)
    _run(window, root, "Ctrl+Shift+Up")
    assert player.volume == 1.0 and window._notice == "Volume 100% · untouched"
    _run(window, root, "Ctrl+Up", caps=True)                # (Caps Lock makes no difference)
    assert player.volume == 1.0
    window._seek_target_playing = True                     # (the playing song is on show: 0:42 of 3:20)
    for text in ("5", "0", "9", "Left", "Right", "Shift+Left", "Shift+Right"):
        _run(window, root, text)
    assert seeks == [pytest.approx(100.0), 0.0, pytest.approx(180.0), 37.0, 47.0, 12.0, 72.0]
    assert not errors


def test_tab_keys(make_window, tmp_path):
    from ironbark.ui import Tab

    window, root, player, errors = make_window()
    for n in (2, 3):
        window.tabs.append(Tab("album", str(tmp_path / f"Album {n}"), tracks=[]))
    window._refresh_tabs()
    _run(window, root, "Ctrl+2")
    assert window.current == 1
    _run(window, root, "Ctrl+9")                            # the last tab
    assert window.current == 2
    _run(window, root, "Ctrl+8")                            # no tab 8: nothing changes
    assert window.current == 2
    _run(window, root, "Ctrl+PgUp")
    assert window.current == 1
    _run(window, root, "Ctrl+PgDn")
    _run(window, root, "Ctrl+1")
    assert window.current == 0
    _run(window, root, "Ctrl+T")                            # the + button's menu
    _pump(root, 0.1)
    assert window.new_menu.is_open
    window.new_menu.hide()
    assert not errors


def test_ctrl_l_brings_the_open_library_forward(make_window):
    window, root, player, errors = make_window()
    window.open_library()
    _pump(root, 0.3)
    first = window._library
    first.search_var.set("abc")
    rescans = []
    first.rescan = lambda: rescans.append(1)
    _run(window, root, "Ctrl+L")
    assert window._library is first and first.search_var.get() == "abc"   # the same window, search kept
    view = first.view
    _run(window, first.win, "Ctrl+Tab")
    assert first.view != view
    _run(window, first.win, "Ctrl+Shift+Tab")
    assert first.view == view
    _run(window, first.win, "F5")
    assert rescans == [1]
    _run(window, root, "Ctrl+K")                            # search the library: its search box
    assert window._library is first
    assert not errors


def _playlist_window(make_window, tmp_path):
    from ironbark.ui import Tab

    window, root, player, errors = make_window()
    songs = [Track(tmp_path / f"{n}.flac", f"Song {n}", duration=60.0) for n in (1, 2, 3)]
    path = tmp_path / "Mix.m3u8"
    tab = Tab("playlist", str(path), tracks=list(songs))
    window.tabs.append(tab)
    window.select_tab(len(window.tabs) - 1)
    _pump(root, 0.1)
    return window, root, player, errors, tab, songs


def test_delete_move_and_clear_can_be_undone(make_window, tmp_path):
    window, root, player, errors, tab, songs = _playlist_window(make_window, tmp_path)
    tree = window.playlist.tree
    window.playlist.select(1)
    assert _run(window, tree, "Delete")
    assert tab.tracks == [songs[0], songs[2]] and window._notice == "Removed “Song 2” from “Mix”"
    window._refresh_view()
    assert window.undo_link.winfo_manager() == "place"      # "Undo (Ctrl+Z)" at the end of the status line
    _run(window, root, "Ctrl+Z")
    assert tab.tracks == songs and window._notice == "Put “Song 2” back"
    assert window.playlist.selected_track() == 1
    window._refresh_view()
    assert window.undo_link.winfo_manager() == ""
    window.playlist.select(0)
    assert _run(window, tree, "Alt+Down")                   # (Ctrl+↓ is the volume now)
    assert tab.tracks == [songs[1], songs[0], songs[2]]
    window._status_clicked()                                # clicking the status line undoes too
    assert tab.tracks == songs
    # A change made since can't be undone over.
    window.playlist.select(2)
    _run(window, tree, "Delete")
    window._save_playlist(Path(tab.path), [songs[0]])
    _run(window, root, "Ctrl+Z")
    assert tab.tracks == [songs[0]] and window._notice.startswith("Can't undo")
    # Up next cleared: put back.
    player.add_to_queue(songs)
    window.clear_up_next()
    assert player.up_next() == [] and window._notice == "Cleared Up next (3 songs)"
    _run(window, root, "Ctrl+Z")
    assert player.up_next() == songs
    _run(window, root, "Ctrl+Z")
    assert window._notice == "Nothing to undo"
    assert not errors


def test_enter_ctrl_enter_and_ctrl_shift_enter_in_the_track_list(make_window, tmp_path):
    window, root, player, errors, tab, songs = _playlist_window(make_window, tmp_path)
    tree = window.playlist.tree
    played = []
    window._play_tab = lambda index, track, start_at=0.0: played.append(track)
    window.playlist.on_play = lambda i: window._play_tab(window.current, i)
    window.playlist.select(2)
    assert _run(window, tree, "Ctrl+Enter")
    assert player.up_next() == [songs[2]]
    window.playlist.select(0)
    assert _run(window, tree, "Ctrl+Shift+Enter")
    assert player.up_next() == [songs[0], songs[2]]
    assert _run(window, tree, "Enter") and played == [0]
    tree.selection_remove(*tree.selection())
    assert not _run(window, tree, "Enter")                  # nothing selected: the key goes on to the list
    assert _resolved(window, root, "Enter") is None         # (Enter is the track list's, not the window's)
    assert not errors


def test_settings_has_help_and_about(make_window):
    window, root, player, errors = make_window()
    window._open_settings()
    _pump(root, 0.3)
    texts = []
    stack = [window.help_box]
    while stack:
        w = stack.pop()
        stack.extend(w.winfo_children())
        if hasattr(w, "text_value"):
            texts.append(w.text_value)
    assert set(texts) == {"Keyboard shortcuts (F1)", "About Ironbark", "Save diagnostics…", "Open log folder"}

    def labels(container):
        found, stack = [], [container]
        while stack:
            w = stack.pop()
            stack.extend(w.winfo_children())
            if isinstance(w, (tk.Label, ttk.Label)):
                found.append(str(w.cget("text")))
        return found

    # Detailed logging is a switch in Help, and only there.
    in_help, everywhere = labels(window.help_box), labels(window.settings_pop.body)
    for title in ("Detailed logging",):
        assert title in in_help and everywhere.count(title) == 1, title
    window._from_settings(window.show_help)
    _pump(root, 0.2)
    assert not window.settings_pop.is_open and window._help.win.winfo_exists()
    assert not errors


def test_help_sheet_is_the_table_with_a_filter(make_window):
    window, root, player, errors = make_window()
    _run(window, root, "F1")
    _pump(root, 0.3)
    sheet = window._help
    assert sheet is not None and sheet.shown_rows() == keys.sheet_rows()
    for action in keys.listed():
        assert (action.group, keys.keys_text(action), action.label) in sheet.shown_rows(), action.id
    groups = [sheet.tree.set(iid, "keys") for iid in sheet.tree.get_children() if iid.startswith("g")]
    assert groups == list(keys.GROUPS)
    sheet.query.set("volume")
    rows = sheet.shown_rows()
    assert rows and all("volume" in f"{k} {w}".lower() for _g, k, w in rows)
    _run(window, root, "?")                                 # F1 / ? again: the same sheet, brought forward
    assert window._help is sheet
    sheet.query.set("no such key at all")
    assert sheet.shown_rows() == [] and sheet.tree.get_children() == ("none",)
    assert sheet._clear_filter() == "break" and sheet.query.get() == ""   # Esc: the filter first…
    assert sheet._clear_filter() is None                    # …then the window's own Esc closes it
    sheet.close()
    assert not errors


def test_about_window(make_window):
    window, root, player, errors = make_window()
    copied, visited = [], []
    about = help_window.AboutWindow(root, window.prefs, copy=copied.append, open_url=visited.append)
    _pump(root, 0.3)
    assert about.shown_text() == help_window.MIT_LICENCE.rstrip("\n") or \
        about.shown_text() == help_window.MIT_LICENCE
    assert about.privacy.cget("text") == help_window.PRIVACY
    names = [name for name, _p in about.documents]
    assert names[0] == "Ironbark — MIT licence" and "Third-party licences (summary)" in names
    about.show_document(names.index("Third-party licences (summary)"))
    assert "Third-party" in about.shown_text() or "licen" in about.shown_text().lower()
    about.copy_version_info()
    assert copied and copied[0].startswith("Ironbark ") and about.copied.cget("text") == "Copied"
    assert visited == []                                    # nothing opened until the link is clicked
    about.link.event_generate("<Button-1>")
    _pump(root, 0.05)
    assert visited == [help_window.WEBSITE]
    about.close()
    assert not errors


def test_edit_tags_enter_moves_on_ctrl_s_saves_esc_asks(make_window):
    from ironbark.tag_window import TagWindow

    window, root, player, errors = make_window()
    asked, discard = [], {"answer": False}
    tags = TagWindow(root, window.tabs[0].tracks, window.prefs, on_saved=lambda save: None, busy=set,
                     confirm=lambda message, detail: asked.append(message) or False,
                     confirm_discard=lambda message, detail: asked.append(message) or discard["answer"])
    window._with_keys(tags, "tags", tags.key_handlers())
    for _ in range(40):
        _pump(root, 0.05)
        if tags._baseline is not None:
            break
    assert tags._baseline is not None and not tags.dirty()
    title = tags.entries["title"]
    title.focus_set()
    tags.vars["title"].set("New title")
    assert tags.dirty()
    title.event_generate("<Return>")                        # Enter: the next field, nothing written
    _pump(root, 0.05)
    assert asked == []
    assert _run(window, title, "Ctrl+S")                     # Ctrl+S: Save (it asks first)
    assert asked == ["Save changes to 1 file?"]
    tags.request_close()                                     # Esc / Cancel / ×: unsaved, so it asks
    assert asked[-1] == "Discard changes?" and tags.win.winfo_exists()
    discard["answer"] = True
    tags.win.event_generate("<Escape>")
    _pump(root, 0.05)
    assert not tags.win.winfo_exists()
    assert not errors


def test_help_and_about_follow_the_theme(make_window):
    from ironbark import theme
    from test_light_mini import _colours_in_use, _only_in

    window, root, player, errors = make_window()
    window.show_help()
    window.show_about()
    _pump(root, 0.3)
    window.theme_box.current(1)                             # Light
    window._theme_chosen()
    _pump(root, 0.5)
    for win in (window._help.win, window._about.win):
        assert _colours_in_use(win) & _only_in(theme.DARK, theme.LIGHT) == set()
    assert str(window._help.tree.tag_configure("group", "foreground")).lower() == theme.LIGHT.ACCENT_TEXT.lower()
    assert not errors


# -- the edge cases: read-only boxes, question boxes, loud jumps, unsaved edits, failed saves ------
def _loaded_tag_window(window, root, **kwargs):
    from ironbark.tag_window import TagWindow

    tags = TagWindow(root, window.tabs[0].tracks, window.prefs, on_saved=lambda save: None, busy=set,
                     confirm=lambda message, detail: False, **kwargs)
    window._with_keys(tags, "tags", tags.key_handlers())
    for _ in range(40):
        _pump(root, 0.05)
        if tags._baseline is not None:
            break
    assert tags._baseline is not None
    return tags


def test_read_only_boxes_dont_stop_the_keys(make_window):
    from tkinter import ttk

    window, root, player, errors = make_window()
    window.show_pcm()
    window.show_meters()
    _pump(root, 0.3)
    # The PCM stream's columns are a read-only text area: clicked, it has the keyboard, but nothing
    # can be typed there, so Space still plays / pauses and ← still seeks.
    text = window._pcm.text
    assert str(text.cget("state")) == "disabled" and not keys.is_text_box(text)
    assert _resolved(window, text, "Space") == "play" and _resolved(window, text, "Left") == "seek"
    # A choice list (a read-only combobox) likewise.
    boxes, stack = [], [window._meters.win]
    while stack:
        widget = stack.pop()
        stack.extend(widget.winfo_children())
        if isinstance(widget, ttk.Combobox):
            boxes.append(widget)
    assert boxes and all(_resolved(window, box, "N") == "next" for box in boxes)
    # Boxes that are typed into keep their single keys.
    assert keys.is_text_box(window.playlist.entry) and _resolved(window, window.playlist.entry, "N") is None
    text.configure(state="normal")
    assert keys.is_text_box(text) and _resolved(window, text, "Space") is None
    text.configure(state="disabled")
    assert not errors


def test_ctrl_k_in_a_search_box_keeps_what_was_typed(make_window):
    window, root, player, errors = make_window()
    entry = window.playlist.entry
    window.playlist.query.set("abcdef")
    _focus(root, entry)
    entry.icursor(2)
    entry.event_generate("<Control-Key-k>")                 # (Tk's entries deleted to the end of the line)
    _pump(root, 0.3)
    assert window.playlist.query.get() == "abcdef"
    assert window._quick is not None and window._quick.is_open   # …and the song search opened
    assert not errors


def test_no_key_acts_while_a_question_box_waits(make_window):
    from ironbark import popups

    window, root, player, errors = make_window()
    window.show_meters()
    _pump(root, 0.2)
    box = popups.Prompt(root, "A question", "Over the player window", buttons=(("OK", True), ("Cancel", False)))
    _pump(root, 0.05)
    for text in ("N", "Ctrl+Q", "Ctrl+Shift+Up", "Delete"):
        assert not _run(window, window._meters.win, text), text   # (not even in another window)
    assert player.calls == [] and not window._closing
    box.answer(True)
    assert _run(window, window._meters.win, "N") and player.calls == ["next"]
    assert not errors


def test_full_volume_and_unmute_never_jump_loud_in_bit_perfect(make_window, monkeypatch):
    from ironbark import popups, ui

    window, root, player, errors = make_window()
    asked = []
    answer = {"yes": False}
    dangerous = []
    monkeypatch.setattr(popups, "confirm", lambda *a, **k: asked.append(a[1]) or dangerous.append(k.get("danger"))
                        or answer["yes"])
    # Bit-perfect on a device without its own volume: the player's volume is the only one.
    window.exclusive.set(True)
    player.hardware_volume = False
    player.set_volume(ui.SAFE_VOLUME)
    _run(window, root, "Ctrl+Shift+Up")
    assert asked == ["Turn the volume up to 100%?"] and player.volume == ui.SAFE_VOLUME   # asked: not turned up
    assert dangerous == [True]   # (a reflex Enter keeps the level: only "Turn it up" on purpose goes ahead)
    answer["yes"] = True
    _run(window, root, "Ctrl+Shift+Up")
    assert player.volume == 1.0
    _run(window, root, "Ctrl+Shift+Down")                   # muted and back, the same device: the same level
    _run(window, root, "Ctrl+Shift+Down")
    assert player.volume == 1.0 and len(asked) == 2
    # Muted with the Windows mixer in charge; bit-perfect on (no device volume) meanwhile: unmuting
    # comes back at the safe level, not at full scale.
    window.exclusive.set(False)
    _run(window, root, "Ctrl+Shift+Down")
    assert player.volume == 0.0
    window.exclusive.set(True)
    _run(window, root, "Ctrl+Shift+Down")
    assert player.volume == ui.SAFE_VOLUME and window._notice.startswith("Unmuted at 30%")
    # Without bit-perfect, no question: 100% straight away.
    window.exclusive.set(False)
    _run(window, root, "Ctrl+Shift+Up")
    assert player.volume == 1.0 and len(asked) == 2
    assert not errors


def test_an_undo_that_cant_be_written_can_be_tried_again(make_window, tmp_path, monkeypatch):
    from ironbark import playlists, popups

    window, root, player, errors, tab, songs = _playlist_window(make_window, tmp_path)
    told = []
    monkeypatch.setattr(popups, "tell", lambda window, title, message="", **k: told.append(title))
    window.playlist.select(1)
    assert _run(window, window.playlist.tree, "Delete") and tab.tracks == [songs[0], songs[2]]
    real = playlists.write

    def unplugged(path, tracks):
        raise OSError("The device is not ready")
    monkeypatch.setattr(playlists, "write", unplugged)
    _run(window, root, "Ctrl+Z")
    assert told == ["Couldn't save the playlist"] and not window._notice.startswith("Put")
    assert tab.tracks == [songs[0], songs[2]] and window._undo is not None   # nothing changed: kept to try again
    monkeypatch.setattr(playlists, "write", real)
    _run(window, root, "Ctrl+Z")
    assert tab.tracks == songs and window._notice == "Put “Song 2” back"
    assert not errors


def test_discarding_tag_edits_asks_first_even_when_quitting(make_window):
    from ironbark import popups

    window, root, player, errors = make_window()
    # The real question: Enter keeps editing (only a click on Discard drops the edits).
    tags = _loaded_tag_window(window, root)
    assert tags.may_discard()                               # nothing typed: no question
    tags.vars["title"].set("Typed, not saved")
    root.after(150, lambda: popups.Prompt.stack[-1].press_default())
    tags.request_close()
    assert tags.win.winfo_exists() and tags.dirty()
    tags.close()
    # Quitting asks too: Ctrl+Q works in Edit tags, and closing the player would drop the edits.
    asked, answer = [], {"discard": False}
    tags = _loaded_tag_window(window, root, confirm_discard=lambda message, detail: asked.append(message)
                              or answer["discard"])
    window._tag_window = tags
    tags.vars["title"].set("Typed, not saved")
    assert _run(window, tags.entries["title"], "Ctrl+Q")   # Ctrl+Q from inside Edit tags…
    assert asked == ["Discard changes?"] and not window._closing and tags.win.winfo_exists()   # …kept
    # Edit tags of another song (E) while edits wait: asked too, and the window stays as it was.
    window.edit_tags(window.tabs[0].tracks)
    assert window._tag_window is tags and tags.vars["title"].get() == "Typed, not saved"
    assert asked == ["Discard changes?"] * 2
    answer["discard"] = True
    window.quit()
    assert window._closing and asked == ["Discard changes?"] * 3
    assert not errors


def test_ctrl_l_shows_the_new_folder_after_the_library_folder_changed(make_window, tmp_path):
    window, root, player, errors = make_window()
    window.open_library()
    _pump(root, 0.3)
    elsewhere = tmp_path / "Other music"
    elsewhere.mkdir()
    window._set_library_folders([elsewhere])                # (Settings → Library → Change…): it follows
    _run(window, root, "Ctrl+L")
    _pump(root, 0.2)
    assert window._library.win.winfo_exists() and window._library.root_folder == elsewhere
    second = window._library
    _run(window, root, "Ctrl+L")                            # the same folder: brought forward, as it was
    assert window._library is second
    # Folders changed without the open window being told: Ctrl+L makes it afresh for the new ones.
    third = tmp_path / "Third"
    third.mkdir()
    window.prefs["library_folders"] = [str(third)]
    _run(window, root, "Ctrl+L")
    _pump(root, 0.2)
    assert window._library is not second and not second.win.winfo_exists()
    assert window._library.root_folder == third
    assert not errors


def test_stop_after_and_the_sleep_timer_menu_from_the_keys(make_window, monkeypatch):
    from ironbark import popups

    window, root, player, errors = make_window()
    assert _resolved(window, root, "Ctrl+.") == "stop_after"
    assert _run(window, root, "Ctrl+.") and player.stop_after == "song"
    assert _run(window, root, "Ctrl+.") and player.stop_after is None   # again: carry on (still handled)
    shown = []
    monkeypatch.setattr(popups.PopupMenu, "popup", lambda menu, x, y: shown.append((menu.labels(), x, y)))
    assert _resolved(window, root, "Ctrl+Shift+S") == "sleep_timer"
    assert _run(window, root, "Ctrl+Shift+S", caps=True)                # (Caps Lock makes no difference)
    labels, x, y = shown[-1]
    assert labels == ["Stop after the current song", "Sleep timer"]
    assert (x, y) == (window.status_label.winfo_rootx(), window.status_label.winfo_rooty())
    assert keys.hint("stop_after") == "Ctrl+." and keys.hint("sleep_timer") == "Ctrl+Shift+S"
    assert not errors
