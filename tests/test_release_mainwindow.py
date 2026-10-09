"""The main window before release: a song asked for by name from the album playing after that album's
folder changed, and "Use current" from the mini strip.
The real window on temp settings with a fake player (test_light_mini.make_window): no device is opened
and nothing of the user's is touched."""

from __future__ import annotations

import time

import numpy as np
import soundfile as sf
from test_light_mini import _pump, make_window  # noqa: F401  (make_window is a fixture)

from ironbark.library import Track


def _flac(path):
    sf.write(path, np.zeros((4410, 2), dtype=np.int16), 44100, subtype="PCM_16")
    return path


def _open(make_window, size=(420, 800), scaling=1.0, **prefs):
    w, h = size
    window, root, player, errors = make_window(scaling=scaling, volume=0.8,
                                               geometry=f"{round(w * scaling)}x{round(h * scaling)}+0+0", **prefs)
    _pump(root, 0.5)
    return window, root, player, errors


def _wait(root, ready, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if ready():
            return True
        _pump(root, 0.05)
    return ready()


# -- a song asked for by name from the album playing ---------------------------------------------------
def test_a_song_new_to_the_playing_album_plays_when_asked_for_by_name(make_window, tmp_path, monkeypatch):
    window, root, player, errors = make_window()
    tab = window.tabs[0]
    player.set_tracks(tab.tracks)                   # (as when the tab was played: the player holds its list)
    album = tmp_path / "Music" / "Album"
    intro = _flac(album / "00 Intro.flac")          # added to the folder while the album plays
    played = []
    monkeypatch.setattr(player, "play", lambda index, start_at=0.0: played.append(index))
    window.play_song(Track(path=intro, title="Intro"), album)
    assert _wait(root, lambda: played and not tab.loading)
    assert [t.path.name for t in tab.tracks] == ["00 Intro.flac", "01 Song.flac"]
    assert player.tracks[played[-1]].path == intro         # the song asked for plays …
    assert player.tracks is tab.tracks                     # … from the list as read again
    assert window.playing_tab == 0 and not errors


# -- "Use current" (make this spot home) when the window has no spot of its own ------------------------
def test_use_current_says_why_it_cant_from_the_mini_strip(make_window):
    window, root, player, errors = _open(make_window, size=(420, 800))
    window.toggle_mini()                         # the mini strip: home is a spot for the full window
    _pump(root, 0.4)
    window.make_home()
    assert window.prefs["home_geometry"] == "" and "full window" in window._notice
    window.toggle_mini()
    _pump(root, 0.4)
    window.make_home()
    assert window.prefs["home_geometry"] and window._notice.startswith("Home set")
    assert not errors
