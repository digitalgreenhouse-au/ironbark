"""The pause / play button on the status line while music plays in another tab; the X on Settings."""

from __future__ import annotations

from pathlib import Path

from test_light_mini import _pump, make_window  # noqa: F401  (make_window is a fixture)

from ironbark import theme
from ironbark.engine import State, Status
from ironbark.library import Track
from ironbark.tabbar import glyph
from ironbark.ui import Tab


class _Event:
    def __init__(self, x=2, y=2):
        self.x, self.y = x, y


def _second_tab(window) -> Tab:
    folder = Path(window.tabs[0].path).parent / "Other"
    folder.mkdir(exist_ok=True)
    track = Track(path=folder / "01 Other.flac", title="Other Song", artist="Other", album="Other Album",
                  duration=300.0, sample_rate=44100, bits=16, channels=2)
    tab = Tab("album", str(folder), tracks=[track])
    window.tabs.append(tab)
    return tab


def test_the_status_line_pauses_and_resumes_the_music_in_another_tab(make_window):
    window, root, player, errors = make_window()
    _second_tab(window)
    button = window.elsewhere_button
    assert not button.winfo_manager()                            # on the playing tab: no button

    window.select_tab(1)
    _pump(root, 0.5)
    assert window.current == 1 and window.playing_tab == 0
    assert window.status_label.cget("text").startswith("♪ Playing in another tab: Song One")
    assert button.winfo_manager() == "place" and button.winfo_ismapped()
    assert button.cget("text") == glyph(root, "pause") and button.playing is True
    assert "Pause" in button.hint
    button._click(_Event())                                      # pause: the music, not this tab
    assert player.calls == ["toggle_pause"] and window.playing_tab == 0 and window.current == 1

    # Paused there: the line and the button say so (play), and a click carries on.
    player.status = lambda: Status(State.PAUSED, 0, player.track, 42.0, player.track.duration)
    _pump(root, 0.5)
    assert window.status_label.cget("text").startswith("♪ Paused in another tab: Song One")
    assert button.cget("text") == glyph(root, "play") and button.playing is False and "Carry on" in button.hint
    button._click(_Event())
    assert player.calls == ["toggle_pause", "toggle_pause"]

    # The transport's play still belongs to the tab on show: it starts this one.
    window.toggle_play()
    assert player.calls[-1] == "play" and window.playing_tab == 1

    # On the playing tab (now this one): no button; on the other, it's back.
    _pump(root, 0.3)
    assert not button.winfo_manager()
    window.select_tab(0)
    _pump(root, 0.3)
    assert button.winfo_manager() == "place"                     # (the fake player still says paused)
    assert window.status_label.cget("text").startswith("♪ Paused in another tab: Song One")
    window.select_tab(1)
    _pump(root, 0.3)
    assert not button.winfo_manager()
    assert errors == []


def test_a_notice_takes_the_line_and_the_button_goes_with_it(make_window):
    window, root, player, errors = make_window()
    _second_tab(window)
    window.select_tab(1)
    _pump(root, 0.4)
    assert window.elsewhere_button.winfo_manager() == "place"
    window._set_notice("Something else to say", 0)
    _pump(root, 0.4)
    assert not window.elsewhere_button.winfo_manager()
    window._set_notice("", 0)
    _pump(root, 0.4)
    assert window.elsewhere_button.winfo_manager() == "place"
    # The mini strip has its own play button: the line's button stays off the strip.
    window.toggle_mini()
    _pump(root, 0.4)
    assert not window.elsewhere_button.winfo_manager()
    window.toggle_mini()
    _pump(root, 0.4)
    assert window.elsewhere_button.winfo_manager() == "place"
    # A palette change repaints it in the new colours.
    theme.use(theme.LIGHT, root)
    _pump(root, 0.3)
    assert window.elsewhere_button.cget("bg") == theme.LIGHT.BG
    assert errors == []


def test_settings_has_an_x_at_the_top_right_that_goes_back_to_the_player(make_window):
    window, root, player, errors = make_window()
    window._open_settings()
    _pump(root, 0.4)
    assert window.settings.is_open
    x = window.settings.close_button
    assert x.winfo_ismapped() and x.cget("text") == glyph(root, "close")
    assert x.winfo_rootx() > window.mini_button.winfo_rootx()    # at the right end of the head, past Mini player
    x._click(_Event())
    _pump(root, 0.4)
    assert not window.settings.is_open and window.playlist.frame.winfo_ismapped()
    assert player.calls == [] and errors == []


def test_a_tab_still_loading_speaks_on_the_status_line_only_while_it_is_on_show(make_window):
    """A load can take minutes (a big library's first look): its progress shows while its tab is on
    show, and doesn't hide what the line says on another tab (the music playing elsewhere)."""
    window, root, player, errors = make_window()
    loading = _second_tab(window)
    loading.tracks = None
    progress = {"tracks": None, "text": "Building the library: reading songs… 5 of 80 files (6%) · 0:12"}
    loading.load_progress = progress
    window._say_loading(loading, progress)                        # (not on show: the line isn't taken)
    assert window._notice != progress["text"]
    window._loading_notice_follows(window.tabs[0], loading)       # shown: its progress is up
    window.current = 1
    assert window._notice == progress["text"]
    window._say_loading(loading, progress)
    assert window._notice == progress["text"]
    window._loading_notice_follows(loading, window.tabs[0])       # left: the line is the other tab's again
    window.current = 0
    assert window._notice == ""
