"""Performance of the indexer on a tree of 20,000 tiny FLAC files on disk (marked `perf`: runs only
with --perf, e.g. `uv run pytest -q -p no:cacheprovider --perf -m perf -s tests/test_indexer_perf.py`):
the first look (tags read in worker processes), a look when nothing has changed (a walk only), one
album added, and every file retagged (all read again, as a ReplayGain pass over the whole library
would, by the 2 gentle workers a later look uses). Run before each release; timings are printed
with -s."""
from __future__ import annotations

import os
import shutil
import time

import numpy as np
import pytest
import soundfile as sf
from tagging import write_vorbis

from ironbark.indexer import Indexer
from ironbark.library_db import LibraryDB

pytestmark = pytest.mark.perf

SIZE = 20_000
PER_ALBUM = 11


def timed(fn, *args):
    started = time.perf_counter()
    value = fn(*args)
    return time.perf_counter() - started, value


@pytest.fixture(scope="module")
def music(tmp_path_factory):
    """20,000 tiny tagged FLACs, 11 to an album folder, under 300 artist folders."""
    root = tmp_path_factory.mktemp("indexer-perf") / "Music"
    started = time.perf_counter()
    made = album = 0
    while made < SIZE:
        folder = root / f"Artist {album % 300}" / f"{1950 + album % 70} - Album {album}"
        folder.mkdir(parents=True)
        first = folder / "01.flac"
        sf.write(first, np.zeros((441, 2), np.int16), 44100, subtype="PCM_16", format="FLAC")
        write_vorbis(first, title=f"Song {album}", artist=f"Artist {album % 300}", album=f"Album {album}",
                     date=str(1950 + album % 70), tracknumber="1", genre="Rock")
        data = first.read_bytes()
        count = min(PER_ALBUM, SIZE - made)
        for n in range(2, count + 1):
            (folder / f"{n:02d}.flac").write_bytes(data)
        made += count
        album += 1
    print(f"\nmade {made:,} tiny FLACs in {album:,} album folders in {time.perf_counter() - started:.1f} s")
    return root


@pytest.fixture(scope="module")
def indexed(music, tmp_path_factory):
    db = LibraryDB.open(tmp_path_factory.mktemp("indexer-perf-db") / "library.db")
    look = Indexer(db)
    took, diff = timed(look.check, [music])
    yield db, look, took, diff
    db.close()


def test_the_first_look(indexed):
    db, look, took, diff = indexed
    print(f"first look: {len(diff.added):,} files, {len(db.albums()):,} albums in {took:.1f} s "
          f"({look.fast} worker processes; {len(diff.added) / took:,.0f} files/s)")
    assert len(diff.added) == SIZE and look.used_pool and not diff.error
    assert took < 600


def test_a_look_when_nothing_changed(indexed, music):
    db, look, _took, _diff = indexed
    took, diff = timed(look.check, [music])
    print(f"no-change look: {took:.2f} s")
    assert diff.nothing and not look.used_pool
    assert took < 15


def test_one_album_added(indexed, music):
    db, look, _took, _diff = indexed
    source = next((music / "Artist 0").iterdir())
    shutil.copytree(source, music / "Artist 0" / "New Album")
    took, diff = timed(look.check, [music])
    print(f"one album (11 files) added: {took:.2f} s")
    assert len(diff.added) == 11 and len(db.albums()) == len({p.parent for p in music.rglob("*.flac")})
    assert took < 15


def test_every_file_retagged(indexed, music):
    db, look, _took, _diff = indexed
    later = time.time_ns() + 2_000_000_000
    files = list(music.rglob("*.flac"))
    for path in files:   # (a new modified time: read again, as after a tag pass over the whole library)
        os.utime(path, ns=(later, later))
    took, diff = timed(look.check, [music])
    print(f"every file retagged: {len(diff.changed):,} read again in {took:.1f} s ({look.gentle} worker processes; "
          f"{len(diff.changed) / took:,.0f} files/s)")
    assert len(diff.changed) == len(files) and not diff.added
    assert took < 900
