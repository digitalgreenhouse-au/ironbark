"""Tape reels (Experimental): the deck's physics, and its window."""

import math
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ironbark import experiments, keys, theme
from ironbark.tools import reels
from ironbark.engine import State

TEN = reels.REELS["10.5"]
SEVEN = reels.REELS["7"]
FIFTEEN = reels.SPEEDS["15"]


def song(n, seconds, album="Album", folder="C:/Music/Album"):
    return SimpleNamespace(path=Path(f"{folder}/{n:02d}.flac"), album=album, duration=seconds)


def test_speeds_are_the_standard_ones():
    assert reels.SPEEDS["7.5"] == pytest.approx(0.1905)    # 7½ ips = 19.05 cm/s
    assert reels.SPEEDS["15"] == pytest.approx(0.381)      # 15 ips = 38.1 cm/s


def test_reels_hold_their_published_running_times():
    """2,500 ft runs 33⅓ min at 15 ips (and twice that at 7½); 1,200 ft runs 32 min at 7½."""
    assert TEN.standard_length / FIFTEEN / 60 == pytest.approx(33.33, abs=0.01)
    assert TEN.standard_length / reels.SPEEDS["7.5"] / 60 == pytest.approx(66.67, abs=0.01)
    assert SEVEN.standard_length / reels.SPEEDS["7.5"] / 60 == pytest.approx(32.0, abs=0.01)


def test_cores_and_thickness_follow_from_the_published_lengths():
    for reel in (TEN, SEVEN):
        assert 0 < reel.core_radius < reel.full_radius < reel.flange_radius
        # long-play tape's published length fills it, and so does standard-play's
        assert reels.pack_radius(reel, reel.standard_length) == pytest.approx(reel.full_radius)
        lp = math.sqrt(reel.core_radius ** 2 + reel.long_play_length * reels.LONG_PLAY_THICKNESS / math.pi)
        assert lp == pytest.approx(reel.full_radius)
    # standard-play tape works out near the 1.99 mil measured on 1.5 mil studio tape
    assert 45e-6 < TEN.thickness < 55e-6
    assert TEN.core_radius * 2 == pytest.approx(0.138, abs=0.002)


def test_a_turn_adds_one_thickness_and_the_angle_is_the_turns():
    length = 100.0
    r = reels.pack_radius(TEN, length)
    # one more turn of tape (its circumference long, near enough) adds one thickness
    assert reels.pack_radius(TEN, length + 2 * math.pi * r) - r == pytest.approx(TEN.thickness, rel=1e-3)
    # the turns are the integral of dl / (2πr): checked by summing small steps
    steps, total = 20000, 0.0
    for i in range(steps):
        total += (length / steps) / (2 * math.pi * reels.pack_radius(TEN, (i + 0.5) * length / steps))
    assert reels.turns(TEN, length) == pytest.approx(total, rel=1e-6)


def test_rpm_is_tape_speed_over_circumference():
    assert reels.rpm(FIFTEEN, 0.1) == pytest.approx(60 * 0.381 / (2 * math.pi * 0.1))
    supply, take = reels.reel_states(TEN, TEN.standard_length, 0.0, FIFTEEN)
    assert supply.radius == pytest.approx(TEN.full_radius) and take.radius == pytest.approx(TEN.core_radius)
    assert supply.rpm == pytest.approx(27.9, abs=0.1) and take.rpm == pytest.approx(52.7, abs=0.1)
    # half way the reels hold the same tape and turn at the same speed
    supply, take = reels.reel_states(TEN, 500.0, 250.0, FIFTEEN)
    assert supply.rpm == pytest.approx(take.rpm) and supply.radius == pytest.approx(take.radius)


def test_both_reels_turn_clockwise_as_the_tape_plays():
    before = reels.reel_states(TEN, 700.0, 100.0, FIFTEEN)
    after = reels.reel_states(TEN, 700.0, 101.0, FIFTEEN)
    for a, b in zip(before, after):
        assert b.angle > a.angle
    # each turns by (tape moved) / (its circumference)
    assert after[1].angle - before[1].angle == pytest.approx(1.0 / before[1].radius, rel=1e-2)
    assert after[0].angle - before[0].angle == pytest.approx(1.0 / before[0].radius, rel=1e-2)


def test_the_album_around_the_song_is_the_tape():
    tracks = [song(1, 200, "Other", "C:/Music/Other"), song(1, 100), song(2, 150), song(3, 0), song(4, 90)]
    assert reels.album_run(tracks, 2, tracks[2]) == ([100, 150], 1)   # a song of no known length ends it
    assert reels.album_run(tracks, 0, tracks[0]) == ([200], 0)
    queued = song(9, 60, "Queued", "C:/Music/Queued")
    assert reels.album_run(tracks, -1, queued) == ([60], 0)            # from Up next: alone on its tape
    assert reels.album_run(tracks, 0, None) == ([], -1)


def test_songs_are_packed_onto_reels_whole():
    assert reels.pack_reels([300, 300, 300], 700) == [(0, 600), (600, 300)]
    assert reels.pack_reels([1500], 700) == [(0, 700), (700, 700), (1400, 100)]
    assert reels.pack_reels([], 700) == [(0, 0)]
    spool, at = reels.spool_for([300, 300, 300], 2, 50, 700, ("a",))
    assert (spool.number, spool.count, spool.start, spool.seconds, at) == (2, 2, 600, 300, 50)
    first, _ = reels.spool_for([300, 300, 300], 0, 10, 700, ("a",))
    assert first.key != spool.key


def test_transport_plays_at_tape_speed_and_stops_like_a_deck():
    t = reels.Transport()
    t.load("k", 10.0, FIFTEEN)
    target = 10.0
    for _ in range(100):
        target += FIFTEEN * 0.02
        t.step(0.02, target, True, FIFTEEN, 1000.0)
    assert t.speed == pytest.approx(FIFTEEN, rel=0.02) and t.place == pytest.approx(target, abs=0.02)
    # pause: it stops within a fraction of a second, close to where the music stopped
    for _ in range(25):
        t.step(0.02, target, False, FIFTEEN, 1000.0)
    assert abs(t.speed) < 0.01 and t.place == pytest.approx(target, abs=0.05)


def test_a_seek_spools_there_in_about_a_second_and_lands_on_the_spot():
    t = reels.Transport()
    t.load("k", 0.0, FIFTEEN)
    target, elapsed, fastest = 600.0, 0.0, 0.0
    while t.winding or elapsed == 0.0:
        target += FIFTEEN * 0.02
        t.step(0.02, target, True, FIFTEEN, 1000.0)
        elapsed += 0.02
        fastest = max(fastest, t.speed)
        assert t.place <= target + 1e-9   # never overshoots
        assert elapsed < 3
    assert elapsed == pytest.approx(reels.WIND_TIME, abs=0.3)
    assert t.place == pytest.approx(target) and t.speed == pytest.approx(FIFTEEN)
    assert fastest > 100   # it really did spool


def test_transport_stays_on_the_tape():
    t = reels.Transport()
    t.load("k", 0.0, 0.0)
    for _ in range(200):
        t.step(0.02, 5000.0, True, FIFTEEN, 100.0)
    assert 0.0 <= t.place <= 100.0


def test_listed_as_an_experiment_with_a_key():
    assert "tape_reels" in experiments.known()
    assert not experiments.enabled({}, "tape_reels")
    assert keys.BY_ID["reels"].keys == ("W",)


# -- the window ------------------------------------------------------------------------------------

class AlbumPlayer:
    def __init__(self, tracks):
        self.tracks, self.index, self.position, self.state = tracks, 0, 0.0, State.PLAYING

    def status(self):
        track = self.tracks[self.index]
        return SimpleNamespace(track=track, position=self.position, state=self.state, index=self.index,
                               duration=track.duration)


@pytest.fixture
def root():
    tk = pytest.importorskip("tkinter")
    try:
        r = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"no display: {error}")
    theme.apply(r)
    errors = []
    r.report_callback_exception = lambda *info: errors.append(info)
    r.errors = errors
    r.withdraw()
    yield r
    theme.use(theme.DARK)
    r.destroy()


def _pump(root, seconds=0.1):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.01)


def test_window_follows_the_song_and_remembers_its_choices(root):
    from ironbark.tools.reels_window import ReelsWindow

    player = AlbumPlayer([song(1, 240), song(2, 300), song(3, 200)])
    prefs: dict = {}
    win = ReelsWindow(root, player, prefs)
    win.win.geometry("420x640+20+20")
    _pump(root, 0.3)
    assert win._frame is not None and win._frame[3]          # tall: stacked
    texts = [win.canvas.itemcget(win._items[n]["rpm"], "text") for n in ("supply", "take")]
    assert all(t.endswith("rpm") for t in texts)
    player.index, player.position = 2, 100.0                   # a jump: spools, then plays from there
    _pump(root, 1.8)
    expected = (240 + 300 + 100) * FIFTEEN
    assert win.transport.place == pytest.approx(expected, abs=1.5)
    win.size.set("7")
    win._choice_changed()
    win.speed.set("7.5")
    win._choice_changed()
    assert prefs["reels_size"] == "7" and prefs["reels_speed"] == "7.5"
    win.win.geometry("1000x560+20+20")
    _pump(root, 0.3)
    assert not win._frame[3]                                   # wide: side by side
    assert "7-inch reels" in win.info.cget("text")
    win.close()
    assert not root.errors


def test_window_blurs_a_reel_too_fast_to_follow(root):
    from ironbark.tools.reels_window import ReelsWindow

    player = AlbumPlayer([song(1, 1800)])
    win = ReelsWindow(root, player, {})
    _pump(root, 0.3)
    items = win._items["take"]
    assert win.canvas.itemcget(items["blur"], "state") == "hidden"
    player.position = 1500.0
    _pump(root, 0.35)                                          # mid-spool
    assert win.canvas.itemcget(items["blur"], "state") == "normal"
    assert win.canvas.itemcget(items["rpm"], "text") == "spooling…"
    win.close()
    assert not root.errors
