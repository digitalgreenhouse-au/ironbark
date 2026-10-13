"""The window's side of the network playback cache: the status line and hover for each phase,
the transport showing the intent to play, Now Playing's Source rows and the proof's wording, the
Settings section behind its Experimental switch, the measuring workers never remembering a song
that simply hadn't downloaded yet, nothing at all of the cache while the switch is off, and the
register of every place that decodes or indexes a file (so none can bypass the cache unnoticed).
No real network anywhere."""
from __future__ import annotations

import ast
import queue
import socket
import threading
import time
from collections import Counter, deque
from pathlib import Path
from types import SimpleNamespace

import pytest
from netfakes import FakeClassifier, FakeShare, wait_until
from test_light_mini import _pump, make_window  # noqa: F401 — (make_window is a fixture)

from ironbark import audiofile, netcache, settings
from ironbark.engine import NetworkView, State, Status
from ironbark.library import Track
from ironbark.proof import Proof
from ironbark.statustext import COPY_FAILED, network_hover, network_line
from ironbark.tools.details import describe_proof, source_rows


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to use the network")
    monkeypatch.setattr(socket.socket, "connect", refuse)


def view(phase="preparing", auto=True, percent=None, detail="", **more):
    return NetworkView(phase, auto, percent, detail, **more)


# -- the status line, the hover and the transport ---------------------------------------------------
def test_the_status_line_says_where_a_network_song_has_got_to():
    assert network_line(None) == ("", "plain") and network_line(view("playing")) == ("", "plain")
    assert network_line(view(percent=34))[0] == "Preparing track… 34%"
    assert network_line(view())[0] == "Preparing track…"
    assert network_line(view(detail="waiting for \\\\192.168.80.173 (not answering) · starts by itself when "
                                    "it's back")) == (
        "Preparing track… \\\\192.168.80.173 isn't answering · starts when it's back", "warn")
    assert network_line(view("downloading", percent=34))[0] == "Downloading track… 34%"
    assert network_line(view("checking"))[0] == "Checking the download…"
    assert network_line(view(auto=False, percent=34))[0] == "Paused · the song keeps downloading (34%)"
    assert network_line(view("waiting", at=134.0)) == (
        "Waiting for the network · paused at 2:14 · carries on by itself", "warn")
    assert network_line(view("waiting", auto=False, at=134.0))[0].endswith("press play to carry on")
    assert network_line(view("fetching", percent=61, at=192.0))[0] == "Fetching from 3:12… 61%"
    assert network_line(view("fetching", percent=64), mp3=True)[0] == (
        "Fetching… an MP3 has to be read from the start up to here: 64%")
    assert network_line(view("released"))[0] == ("Paused: the network isn't answering · carries on by itself "
                                                 "when it's back")
    assert network_hover(view("released")).endswith("(or press play to try now)")   # (the fuller words)


def test_the_hover_and_now_playings_source_rows():
    playing = view("playing", copy="downloading", cached=0.72, next_cached=0.15, speed=12.8e6)
    assert network_hover(playing) == ("Playing the local copy · 72% downloaded at 12.8 MB/s (megabytes per second)"
                                      " · next song 15%")
    assert network_hover(view("playing", copy="failed", cached=1.0)) == COPY_FAILED
    track = Track(Path("//192.168.80.173/Shared/workoutmusic/Song.flac"), "Song", size=28_300_000, md5=1)
    done = view("playing", copy="verified", cached=1.0, checks="size+identity+decode+flac-md5")
    rows = dict(source_rows(track, done))
    assert rows["Where"].startswith("network drive ") and rows["Where"].endswith("workoutmusic")
    assert rows["Playing"] == "the copy on this computer (the cache)"
    assert rows["Downloaded"].startswith("100% of 27.0 MB · checked: the same size and date as on the drive")
    assert "MD5 (Message Digest 5) fingerprint matches" in rows["Downloaded"]
    plain = dict(source_rows(track, view("playing", copy="verified", cached=1.0, checks="size+identity")))
    assert "MD5" not in plain["Downloaded"]                     # (only what was really checked is said)
    waiting = dict(source_rows(track, view(percent=34)))
    assert waiting["Playing"] == "not yet: Preparing track… 34%"
    assert dict(source_rows(track, view("playing", copy="stale", cached=1.0)))["Downloaded"].startswith(
        "✗ Playing an older copy")


def test_the_proof_says_what_it_was_checked_against():
    proof = Proof(sent=192.0, checked=192.0)
    text, tone = describe_proof(proof, None, 1.0, 1.0)
    assert "cached copy" not in text
    proof.copy = "verified"
    text, tone = describe_proof(proof, None, 1.0, 1.0)
    assert text.endswith("checked against the cached copy · the copy matches the original")
    proof.copy = "downloading"
    assert describe_proof(proof, None, 1.0, 1.0)[0].endswith(
        "its match with the original is checked when the download finishes")
    proof.copy = "failed"
    assert describe_proof(proof, None, 1.0, 1.0) == (f"{COPY_FAILED} {describe_proof(Proof(192.0, 192.0), None, 1.0, 1.0)[0]}",
                                                     "warn")


def test_the_window_shows_the_intent_to_play_while_a_song_waits(make_window):
    window, root, player, errors = make_window()
    track = player.track
    shown = {}
    player.status = lambda: Status(State.PAUSED, 0, track, 0.0, track.duration, network=shown["network"])
    shown["network"] = view(percent=34)
    window._refresh_view()
    assert window.play_button.playing is True                  # it starts by itself: the transport shows pause
    assert window._status_text == "Preparing track… 34%"
    assert "Preparing track… 34%" in window._status_detail
    shown["network"] = view(auto=False, percent=34)
    window._refresh_view()
    assert window.play_button.playing is False
    assert window._status_text == "Paused · the song keeps downloading (34%)"
    shown["network"] = view("waiting", at=134.0)
    window._refresh_view()
    assert window.play_button.playing is True
    assert window._status_text == "Waiting for the network · paused at 2:14 · carries on by itself"
    assert window._media_state(player.status()) == "paused"   # Windows' media panel: nothing is sounding
    assert errors == []


# -- the Settings section and the switch -----------------------------------------------------------------
class StubCache:
    def __init__(self):
        self.notices = deque(maxlen=20)
        self.invalidated = []
        self.emptied = 0

    def snapshot(self):
        return netcache.CacheView(int(3.2e9), 84, 80, {})

    def invalidate(self, path):
        self.invalidated.append(path)

    def empty(self):
        self.emptied += 1
        done = threading.Event()
        done.set()
        return done


def test_the_settings_section_comes_with_local_network_access_and_saves_its_choices(make_window, monkeypatch):
    stub = StubCache()
    installed = []
    monkeypatch.setattr(netcache, "install", lambda prefs, player: installed.append(prefs) or stub)
    window, root, player, errors = make_window()
    assert window._netcache is None and not installed           # Local network access off: nothing starts
    assert window.netplay_box.winfo_manager() == ""
    window.local_network.set(True)
    window._local_network_changed()
    assert window.netplay_box.winfo_manager() == "pack"
    assert window.prefs["local_network"] and "next starts" in window._notice
    assert window.cache_text.cget("text") == "Cache: starts when Ironbark next starts"
    window.net_mode_box.current(1)
    window.net_mode_box.event_generate("<<ComboboxSelected>>")
    window.net_buffer_box.current(2)
    window.net_buffer_box.event_generate("<<ComboboxSelected>>")
    window.net_predownload_box.current(0)
    window.net_predownload_box.event_generate("<<ComboboxSelected>>")
    root.update()
    window.net_verify.set(False)
    window._net_verify_changed()
    assert (window.prefs["net_mode"], window.prefs["net_buffer"], window.prefs["net_predownload"],
            window.prefs["net_verify"]) == ("fast", 60, "off", False)
    settings.save(window.prefs)
    again = settings.load()
    assert (again["net_mode"], again["net_buffer"], again["net_predownload"], again["net_verify"]) == (
        "fast", 60, "off", False)
    settings.save({**again, "net_mode": "turbo", "net_buffer": 7})   # (hand-edited: dropped back to the defaults)
    again = settings.load()
    assert (again["net_mode"], again["net_buffer"]) == ("intelligent", 30)
    window._start_cache()                                        # (as the next start does)
    assert installed and window._netcache is stub
    assert window.cache_text.cget("text") == "Cache: 3.2 of 10 GB used · 84 songs"
    stub.notices.append("The music cache is full: songs further ahead aren't being downloaded")
    window._notice = ""
    window._take_cache_notices()
    assert window._notice.startswith("The music cache is full") and not stub.notices
    window._apply_new_tags([Path("//fakehost/share/a.flac")])
    assert stub.invalidated == [Path("//fakehost/share/a.flac")]
    monkeypatch.setattr("ironbark.popups.confirm", lambda *a, **k: True)
    window._empty_cache()
    assert stub.emptied == 1
    window.local_network.set(False)
    window._local_network_changed()
    assert window.netplay_box.winfo_manager() == ""
    assert errors == []


def test_copies_left_on_this_computer_while_the_cache_isnt_running_are_shown_and_can_be_emptied(
        make_window, monkeypatch):
    from ironbark.netcache.store import OwnerLock
    monkeypatch.setattr(netcache, "install", lambda prefs, player: None)   # (it didn't start this time)
    window, root, player, errors = make_window(local_network=True)
    _pump(root, 0.2)
    folder = netcache.folder()
    (folder / "Tracks").mkdir(parents=True)
    (folder / "Tracks" / "Song [0123456789abcdef].flac").write_bytes(b"\0" * 3_000_000)
    (folder / "netcache.db").write_bytes(b"\0" * 4096)
    window._netcache = None
    window._paint_cache()
    assert window.cache_text.cget("text") == "Cache: starts when Ironbark next starts · 3 MB still on this computer"
    monkeypatch.setattr("ironbark.popups.confirm", lambda *a, **k: True)
    other = OwnerLock(folder / "owner.lock")                     # another copy of Ironbark is using it
    assert other.acquire()
    window._empty_cache()
    assert wait_until(lambda: (root.update(), window._notice.startswith("Another copy of Ironbark"))[1], 5)
    assert (folder / "Tracks").exists()
    other.release()
    window._empty_cache()
    assert wait_until(lambda: (root.update(), not (folder / "Tracks").exists())[1], 5)
    assert wait_until(lambda: (root.update(), window.cache_text.cget("text") ==
                               "Cache: starts when Ironbark next starts")[1], 5)
    window._notice = ""
    window._empty_cache()
    assert window._notice == "The network playback cache is empty"
    assert errors == []


# -- switched off, nothing of the cache; switched on, it starts after the window and stops with it -----
def test_switched_off_nothing_of_the_cache_runs(make_window):
    window, root, player, errors = make_window()
    _pump(root, 0.3)
    assert window._netcache is None and netcache.manager() is None and audiofile._resolver is None
    assert player._preparer is None
    assert not [t for t in threading.enumerate() if t.name.startswith("netcache")]
    assert not (settings.cache_folder() / "NetCache").exists()


def test_switched_on_the_cache_starts_once_the_window_is_up_and_stops_with_it(make_window, monkeypatch, tmp_path):
    real_install = netcache.install
    share = FakeShare()
    monkeypatch.setattr(netcache, "install", lambda prefs, player: real_install(
        prefs, player, where=tmp_path / "NetCache", classifier=FakeClassifier(), fetcher=share.fetcher,
        probe_host=lambda name: True))
    window, root, player, errors = make_window(local_network=True)
    assert wait_until(lambda: (root.update(), window._netcache is not None)[1], 5)
    assert netcache.manager() is window._netcache and player._preparer is window._netcache
    assert audiofile._resolver is not None
    window._stop_cache()
    assert netcache.manager() is None and audiofile._resolver is None and player._preparer is netcache.DIRECT
    assert wait_until(lambda: not [t for t in threading.enumerate() if t.name.startswith("netcache-scheduler")], 5)


# -- the measuring workers ---------------------------------------------------------------------------------
def worker_window(**more):
    from ironbark.ui import PlayerWindow
    ordinary = SimpleNamespace(check=lambda path, cancel=None: "")   # no DTS or HDCD stream in these songs
    more.setdefault("_encoded", ordinary)
    w = SimpleNamespace(_peaks={}, _gain_tags={}, _lufs={}, _peak_jobs=queue.Queue(), _peak_wanted=set(),
                        _level_wanted=set(), _stored_peak=lambda path: None, **more)
    w._again_later = PlayerWindow._again_later.__get__(w)
    w._measure_for_levelling = PlayerWindow._measure_for_levelling.__get__(w)
    threading.Thread(target=PlayerWindow._measure_peaks, args=(w,), daemon=True).start()
    return w


def test_a_song_not_downloaded_yet_is_measured_again_not_remembered_as_silent(monkeypatch):
    from ironbark import analysis, levelling
    tries = Counter()

    def true_peak(path, cancel=None):
        tries[path.name] += 1
        if tries[path.name] == 1:
            raise audiofile.NotCached("it took too long to download", 0.4, "timeout")
        return 0.9

    def measure(path, peak=True, loudness=True, cancel=None):
        tries["level " + path.name] += 1
        if tries["level " + path.name] == 1:
            raise audiofile.NotCached("it took too long to download", 0.4, "timeout")
        return 0.7, -14.0

    monkeypatch.setattr(analysis, "true_peak", true_peak)
    monkeypatch.setattr(levelling, "measure", measure)
    monkeypatch.setattr(levelling, "read_tags", lambda path: None)
    song, other = Path("//fakehost/share/song.flac"), Path("//fakehost/share/other.flac")
    w = worker_window()
    w._peak_wanted, w._level_wanted = {song}, {other}
    w._peak_jobs.put(song)
    w._peak_jobs.put(("level", other))
    assert wait_until(lambda: song in w._peaks and other in w._lufs, 5)
    assert w._peaks[song] == 0.9 and tries["song.flac"] == 2      # never 0.0 ("no peak") from the wait
    assert w._lufs[other] == -14.0 and w._peaks[other] == 0.7 and tries["level other.flac"] == 2


def test_a_song_skipped_past_while_it_downloads_is_simply_dropped(monkeypatch):
    from ironbark import analysis
    asked = []

    def true_peak(path, cancel=None):
        asked.append(path)
        raise audiofile.NotCached("no longer wanted", 0.2, "cancel")

    monkeypatch.setattr(analysis, "true_peak", true_peak)
    song = Path("//fakehost/share/song.flac")
    w = worker_window()
    w._peak_wanted = {song}
    w._peak_jobs.put(song)
    assert wait_until(lambda: asked, 3)
    time.sleep(0.2)
    assert asked == [song] and song not in w._peaks


def test_a_network_album_is_never_measured_from_part_of_it(monkeypatch, tmp_path):
    from ironbark import loudness
    from ironbark.ui import PlayerWindow

    def file_powers(path, cancel=None, progress=None, wait=None):
        assert wait is False                                     # (an album sweep fetches nothing)
        raise audiofile.NotCached("this network song hasn't been downloaded yet", 0.3, "not-cached")

    monkeypatch.setattr(loudness, "file_powers", file_powers)
    with pytest.raises(audiofile.NotCached):
        loudness.album_loudness([tmp_path / "a.flac", tmp_path / "b.flac"])
    album = tmp_path / "Album"
    album.mkdir()
    (album / "01.flac").write_bytes(b"")
    monkeypatch.setattr(loudness, "album_loudness", lambda files, cancel=None: (_ for _ in ()).throw(
        audiofile.NotCached("not yet", 0.5, "not-cached")))
    w = SimpleNamespace(_album_lufs={}, _album_jobs=queue.Queue(), _album_wanted=album, _album_later={},
                        _album_cancel=threading.Event())
    threading.Thread(target=PlayerWindow._measure_albums, args=(w,), daemon=True).start()
    w._album_jobs.put((album, album / "01.flac"))
    assert wait_until(lambda: album in w._album_later, 3)
    assert album not in w._album_lufs and w._album_wanted is None
    PlayerWindow._want_album(w, album, album / "01.flac")
    assert w._album_jobs.empty()                                  # (asked again only after a while)


# -- the register of every decode and frame-index call ------------------------------------------------------
# Every place in ironbark/ that decodes a file (audiofile.open / audiofile.info) or builds a frame index.
# Each was reviewed for songs on network drives: whole-file tools open by name, so the cache's resolver
# hands them the complete, checked copy (waiting off the window's thread); playback, its reopens and the
# proof open the LocalTrack the cache pinned; the cache's own probe and check open its copies; frame
# indexes are built from audiofile.local_copy. A new call shows up here, to be reviewed the same way.
REVIEWED = {
    ("ironbark.tools.abx", "prepare", "audiofile.open"): 2,
    ("ironbark.analysis", "album_measure", "audiofile.open"): 1,
    ("ironbark.analysis", "analyse", "audiofile.open"): 1,
    ("ironbark.analysis", "true_peak", "audiofile.open"): 1,
    ("ironbark.audiofile", "mp3_shortfall", "mp3frames.build_index"): 1,
    ("ironbark.tools.clipping", "listening_versions", "audiofile.open"): 1,
    ("ironbark.tools.clipping", "scan", "audiofile.open"): 1,
    ("ironbark.tools.compare", "compare_files", "audiofile.open"): 2,
    ("ironbark.tools.compare", "info", "audiofile.open"): 1,
    ("ironbark.tools.details", "NowPlayingWindow._on_track_change", "flacframes.build_index"): 1,
    ("ironbark.tools.details", "NowPlayingWindow._on_track_change", "mp3frames.build_index"): 1,
    ("ironbark.session", "_Session._reopen", "audiofile.open"): 1,
    ("ironbark.session", "_Source.__init__", "audiofile.open"): 1,
    ("ironbark.formats", "scan", "audiofile.open"): 1,
    ("ironbark.passthrough", "detect", "audiofile.open"): 1,
    ("ironbark.levelling", "measure", "audiofile.open"): 1,
    ("ironbark.library", "find_albums", "audiofile.info"): 1,
    ("ironbark.loudness", "file_powers", "audiofile.open"): 1,
    ("ironbark.tools.meters", "album_alignment_loudness", "audiofile.info"): 1,
    ("ironbark.tools.meters", "vu_levels", "audiofile.open"): 1,
    ("ironbark.netcache.manager", "Manager._probe", "audiofile.open"): 1,
    ("ironbark.netcache.verify", "check", "audiofile.open"): 1,
    ("ironbark.proof", "SentRecord._check_loop", "audiofile.open"): 1,
    ("ironbark.tools.ripcheck", "_read_info", "audiofile.info"): 1,
    ("ironbark.tools.ripcheck", "checksums", "audiofile.open"): 1,
    ("ironbark.tools.spectrum", "track_picture", "audiofile.open"): 1,
    ("ironbark.tag_editor", "same_audio", "audiofile.open"): 2,
}
WATCHED = {("audiofile", "open"), ("audiofile", "info"), ("flacframes", "build_index"), ("mp3frames", "build_index")}


def decode_sites() -> Counter:
    """(module, function, what) for every call of a watched function — and every use of one
    imported by name (passed on to be called, as Now Playing does)."""
    root = Path(audiofile.__file__).parent
    found: Counter = Counter()
    for path in sorted(root.rglob("*.py")):
        module = path.relative_to(root.parent).with_suffix("").as_posix().replace("/", ".")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                source = node.module.split(".")[-1]
                for name in node.names:
                    if (source, name.name) in WATCHED:
                        aliases[name.asname or name.name] = f"{source}.{name.name}"
        stack: list[str] = []

        def visit(node):
            named = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            if named:
                stack.append(node.name)
            what = None
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and (f.value.id, f.attr) in WATCHED:
                    what = f"{f.value.id}.{f.attr}"
            elif isinstance(node, ast.Name) and node.id in aliases and isinstance(node.ctx, ast.Load):
                what = aliases[node.id]
            if what:
                found[(module, ".".join(stack) or "<module>", what)] += 1
            for child in ast.iter_child_nodes(node):
                visit(child)
            if named:
                stack.pop()

        visit(tree)
    return found


def test_every_decode_and_index_call_is_in_the_reviewed_register():
    found = dict(decode_sites())
    new = {k: v for k, v in found.items() if REVIEWED.get(k) != v}
    gone = {k: v for k, v in REVIEWED.items() if k not in found}
    assert not new, f"a new or changed decode call: review it for songs on network drives, then list it: {new}"
    assert not gone, f"listed but not found any more (take it off the list): {gone}"


# -- what's downloaded, the network's quality, Up next's marks (v2a) ---------------------------------------
def test_the_network_now_line_up_next_marks_and_the_quality_row():
    from ironbark.netcache import NetworkNow, Progress
    from ironbark.statustext import cache_mark, network_now_line
    assert network_now_line(None) == "Network now: nothing fetched yet"
    assert network_now_line(NetworkNow("192.168.80.173", "up", "good", 12.8e6, 0.4e6)) == (
        "Network now: Good — 12.8 MB/s (megabytes per second); this song needs 0.4 MB/s")
    assert network_now_line(NetworkNow("192.168.80.173", "up", "", None, None)) == "Network now: measuring"
    assert network_now_line(NetworkNow("192.168.80.173", "down", "poor", None, None)) == (
        "Network now: \\\\192.168.80.173 isn't answering")

    def progress(state, fraction, **more):
        return Progress(state, fraction, None, "", host="192.168.80.173", **more)

    assert cache_mark(None) == ("", "", "")
    assert cache_mark(progress("ready", 1.0)) == ("●", "accent", "In the cache: plays without the network")
    assert cache_mark(progress("downloading", 0.72)) == ("◐ 72%", "plain", "72% downloaded")
    assert cache_mark(progress("queued", 0.0)) == ("○", "plain", "Waiting to download")
    assert cache_mark(progress("partial", 0.2, host_state="down")) == (
        "!", "warn", "Couldn't download: \\\\192.168.80.173 isn't answering · will try again")
    assert cache_mark(progress("failed", 0.0, error="it's no longer on the share"))[2] == (
        "Couldn't download: it's no longer on the share · will try again")
    track = Track(Path("//192.168.80.173/Shared/Song.flac"), "Song", size=28_300_000)
    rows = dict(source_rows(track, view("playing", copy="downloading", cached=0.5, quality="good", speed=2e6)))
    assert rows["Network"] == "Good" and rows["Speed"].startswith("2.0 MB/s")
    assert "Network" not in dict(source_rows(track, view("playing", copy="verified", cached=1.0)))


class NetStub(StubCache):
    """A cache with songs at different stages, for the window's marks and lines."""

    def __init__(self, stages):
        super().__init__()
        self.stages = stages            # path name -> netcache.Progress (absent: a local song)
        self.narrowed = False
        self.changed = 0

    def progress(self, path):
        return self.stages.get(Path(path).name)

    def network_now(self):
        return netcache.NetworkNow("192.168.80.173", "up", "excellent", 12.8e6, 0.4e6)

    def settings_changed(self):
        self.changed += 1


def test_settings_offers_every_pre_download_keep_and_size_choice(make_window, monkeypatch):
    stub = NetStub({})
    monkeypatch.setattr(netcache, "install", lambda prefs, player: stub)
    window, root, player, errors = make_window(local_network=True)
    window._start_cache()
    assert [*window.net_predownload_box.cget("values")] == ["Off", "Next track", "Next 2 tracks", "Up to 200 songs ahead"]
    assert [*window.net_keep_box.cget("values")] == ["Don't keep", "Until playback finishes", "For 1 day",
                                                    "Until the cache is full"]
    assert [*window.net_size_box.cget("values")] == ["1 GB", "5 GB", "10 GB", "25 GB", "50 GB", "Unlimited"]
    assert window.net_keep_box.get() == "Until the cache is full" and window.net_size_box.get() == "10 GB"
    for box, index in ((window.net_predownload_box, 3), (window.net_keep_box, 1), (window.net_size_box, 5)):
        box.current(index)
        box.event_generate("<<ComboboxSelected>>")
    root.update()
    assert (window.prefs["net_predownload"], window.prefs["net_keep"], window.prefs["net_cache_gb"]) == (
        "queue", "session", 0)
    assert stub.changed == 3                                    # the cache plans and tidies again at once
    assert window.cache_text.cget("text") == "Cache: 3.2 GB used · 84 songs"   # (no limit: no "of")
    assert window.net_now_text.cget("text") == (
        "Network now: Excellent — 12.8 MB/s (megabytes per second); this song needs 0.4 MB/s")
    settings.save(window.prefs)
    again = settings.load()
    assert (again["net_predownload"], again["net_keep"], again["net_cache_gb"]) == ("queue", "session", 0)
    settings.save({**again, "net_keep": "forever", "net_cache_gb": 3})
    again = settings.load()
    assert (again["net_keep"], again["net_cache_gb"]) == ("limit", 10)
    assert errors == []


def test_up_next_marks_each_network_song_and_the_seek_bar_shows_whats_downloaded(make_window, monkeypatch):
    from ironbark.netcache import Progress
    window, root, player, errors = make_window()
    songs = [Track(Path(f"//192.168.80.173/Shared/{name}.flac"), name.title(), duration=200.0)
             for name in ("ready", "half", "waiting", "away")] + [Track(Path("C:/Music/local.flac"), "Local")]
    stub = NetStub({"ready.flac": Progress("ready", 1.0, None, "verified"),
                    "half.flac": Progress("downloading", 0.72, 3e6, "downloading", host="192.168.80.173"),
                    "waiting.flac": Progress("queued", 0.0, None, "downloading", host="192.168.80.173"),
                    "away.flac": Progress("partial", 0.1, None, "downloading", host="192.168.80.173",
                                          host_state="down")})
    window._netcache = stub
    player.add_to_queue(songs)
    window._show_up_next()
    root.update()
    rows = [w for w in window.up_next_pop.body.winfo_children() if hasattr(w, "lead")]
    assert [row.lead.cget("text") if row.lead is not None else "" for row in rows] == ["●", "◐ 72%", "○", "!", ""]
    assert not any("short wait" in getattr(w, "cget", lambda _: "")("text")
                   for w in window.up_next_pop.body.winfo_children() if w.winfo_class() == "TLabel")
    stub.stages["half.flac"] = Progress("downloading", 0.9, 3e6, "downloading", host="192.168.80.173")
    stub.narrowed = True
    window._paint_up_next()                                     # (as each poll does while it's open)
    root.update()
    rows = [w for w in window.up_next_pop.body.winfo_children() if hasattr(w, "lead")]
    assert rows[1].lead.cget("text") == "◐ 90%"
    assert any("short wait" in w.cget("text") for w in window.up_next_pop.body.winfo_children()
               if w.winfo_class() == "TLabel")
    window.up_next_pop.hide()
    track = player.track
    band = ((0.0, 0.25), (0.6, 0.8))
    player.status = lambda: Status(State.PLAYING, 0, track, 42.0, track.duration,
                                   network=view("playing", copy="downloading", cached=0.45, spans=band))
    window._refresh_view()
    assert window.seek.band == band
    player.status = lambda: Status(State.PLAYING, 0, track, 42.0, track.duration)
    window._refresh_view()
    assert window.seek.band == ()
    assert errors == []


def test_a_playlist_asks_a_server_that_doesnt_answer_only_once(tmp_path, monkeypatch):
    import numpy as np
    import soundfile as sf

    from ironbark import playlists, ui
    from ironbark.ui import PlayerWindow
    here = tmp_path / "here.flac"
    sf.write(here, np.zeros((4410, 2), np.int16), 44100, subtype="PCM_16")
    songs = [Path(f"//nas/share/{k}.flac") for k in range(5)] + [here]
    monkeypatch.setattr(playlists, "read", lambda path: songs)
    monkeypatch.setattr(ui, "SLOW_LOOK_S", 0.05)
    looked = []
    real = Path.is_file

    def is_file(self):
        if self.anchor.lower() == songs[0].anchor.lower():
            looked.append(self)
            time.sleep(0.1)                     # (a NAS switched off: a slow "no")
            return False
        return real(self)

    monkeypatch.setattr(Path, "is_file", is_file)
    tracks = PlayerWindow._read_playlist(tmp_path / "list.m3u8")
    assert len(looked) == 1                     # asked once, not five times
    assert [t.missing for t in tracks] == [True] * 5 + [False]
    assert [t.path for t in tracks] == songs    # nothing lost from the playlist
