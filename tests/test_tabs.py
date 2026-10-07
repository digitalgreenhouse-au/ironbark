from types import SimpleNamespace

import pytest

from ironbark.ui import PlayerWindow, Tab


def _window(n, current, playing):
    return SimpleNamespace(tabs=[Tab("album", str(i)) for i in range(n)], current=current, playing_tab=playing,
                           _shown_key=None, _refresh_tabs=lambda: None)


@pytest.mark.parametrize("source,target,order,current,playing", [
    (0, 3, ["1", "2", "3", "0"], 0, 2),
    (3, 0, ["3", "0", "1", "2"], 2, 0),
    (1, 2, ["0", "2", "1", "3"], 2, 3),
])
def test_moving_a_tab_keeps_the_same_tabs_on_show_and_playing(source, target, order, current, playing):
    # Before: viewing tab "1", playing tab "3".
    w = _window(4, current=1, playing=3)
    PlayerWindow.move_tab(w, source, target)
    assert [t.path for t in w.tabs] == order
    assert w.tabs[w.current].path == "1" and w.current == current
    assert w.tabs[w.playing_tab].path == "3" and w.playing_tab == playing


def test_moving_past_either_end_does_nothing():
    w = _window(3, current=0, playing=None)
    PlayerWindow.move_tab(w, 0, -1)
    PlayerWindow.move_tab(w, 2, 3)
    assert [t.path for t in w.tabs] == ["0", "1", "2"] and w.playing_tab is None


def test_albums_added_to_a_tab_are_saved_with_it():
    tab = Tab("album", r"C:\Music\Tapestry")
    tab.extras.append(r"C:\Music\Brothers in Arms")
    assert tab.source()["extras"] == [r"C:\Music\Brothers in Arms"]


def test_a_tab_with_nothing_added_saves_no_extras():
    assert "extras" not in Tab("album", r"C:\Music\Tapestry").source()


def test_tabs_dont_share_their_added_albums():
    first, second = Tab("album", "a"), Tab("album", "b")
    first.extras.append("c")
    assert second.extras == []


def _opener(state):
    from types import SimpleNamespace

    from ironbark.engine import Status

    calls = []
    w = SimpleNamespace(tabs=[], notices=[], calls=calls, _tier="column", view="tracks",
                        player=SimpleNamespace(status=lambda: Status(state, 0, None, 0.0, 0.0)))
    w._set_notice = lambda text, ms=5000: w.notices.append(text)
    w.select_tab = lambda index, then_play=False: calls.append((index, then_play))
    w._mark_opened = lambda folder: None   # (0.17.0: opening an album takes its "new" dot away)
    return w


def test_opening_an_album_while_music_plays_does_not_interrupt_it():
    from ironbark.engine import State
    from ironbark.ui import PlayerWindow

    for state in (State.PLAYING, State.PAUSED):
        w = _opener(state)
        PlayerWindow.open_tab(w, "album", "C:/Music/Some Album", play=True)
        assert w.calls == [(0, False)] and "carries on" in w.notices[0]
    w = _opener(State.STOPPED)
    PlayerWindow.open_tab(w, "album", "C:/Music/Some Album", play=True)
    assert w.calls == [(0, True)] and not w.notices  # nothing playing: it starts, as before
