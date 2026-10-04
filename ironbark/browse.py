"""The Library's browse views, no UI: albums made from indexed tracks, grouped by artist, genre,
year or composer, or listed newest first; and the sort order for each column.

- **Album artist vs track artist**: an album is listed under its album-artist tag. Without one,
  under the artist when every track has the same one; a compilation (tagged as one, or tracks by
  several artists with no album artist) goes under **Various Artists**.
- **Genres / composers**: a tag can hold several ("Rock; Pop"); the album is listed under each.
- **Sorting**: names sort the way people expect — "The Beatles" under B, "2" before "10".
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import audiofile
from .library import Album, Track, album_folder, natural_key, play_order, sort_name

VARIOUS = "Various Artists"
UNKNOWN = {"artist": "Unknown artist", "genre": "No genre", "year": "Unknown year", "composer": "No composer"}

VIEWS = ("albums", "artists", "genres", "years", "composers", "recent")
VIEW_TITLES = {"albums": "Albums", "artists": "Artists", "genres": "Genres", "years": "Years",
               "composers": "Composers", "recent": "Recently added"}
COLUMNS = ("album", "artist", "year", "quality", "tracks", "added")
# The sort each view starts with: (column, newest/biggest first?)
DEFAULT_SORT = {"recent": ("added", True)}


def split_values(text: str) -> list[str]:
    """ "Rock; Pop" -> ["Rock", "Pop"] (the index joins several values of a tag with "; ")."""
    return [part.strip() for part in (text or "").split(";") if part.strip()]


def album_artist(tracks: list[Track]) -> str:
    """Who an album is by (see the module notes)."""
    tagged = Counter(t.album_artist for t in tracks if t.album_artist)
    if tagged:
        return tagged.most_common(1)[0][0]
    artists: dict[str, str] = {}
    for track in tracks:
        if track.artist.strip():
            artists.setdefault(track.artist.strip().casefold(), track.artist.strip())
    if any(t.compilation for t in tracks) or len(artists) > 1:
        return VARIOUS
    return next(iter(artists.values()), "")


def _most_common(values) -> object:
    counted = Counter(v for v in values if v)
    return counted.most_common(1)[0][0] if counted else None


def _distinct(texts) -> tuple[str, ...]:
    seen: dict[str, str] = {}
    for text in texts:
        for value in split_values(text):
            seen.setdefault(value.casefold(), value)
    return tuple(sorted(seen.values(), key=sort_name))


def make_album(folder: Path, tracks: list[Track], added: float = 0.0) -> Album:
    """One Album (an album folder's tracks, already in play order) with its browse details."""
    first = next((t for t in tracks if t.sample_rate), tracks[0] if tracks else None)
    codecs = tuple(sorted({t.codec for t in tracks}))
    return Album(path=folder, name=folder.name, track_count=len(tracks),
                 sample_rate=first.sample_rate if first else 0, bits=first.bits if first else 0, codecs=codecs,
                 artist=album_artist(tracks), year=_most_common(t.year for t in tracks),
                 genres=_distinct(t.genre for t in tracks), composers=_distinct(t.composer for t in tracks),
                 added=added, title=_most_common(t.album for t in tracks) or "")


def albums_from(tracks: list[Track], added: dict[Path, float] | None = None, root: Path | None = None) -> list[Album]:
    """Albums from a flat list of tracks: a folder of music is an album, "CD1" / "Disc 2" folders
    count towards their parent (as library.find_albums). `added`: when each real file arrived."""
    by_folder: dict[Path, list[Track]] = {}
    for track in tracks:
        by_folder.setdefault(album_folder(audiofile.real_path(track.path)), []).append(track)
    albums = []
    for folder, group in by_folder.items():
        when = max((added or {}).get(audiofile.real_path(t.path), 0.0) for t in group)
        albums.append(make_album(folder, play_order(folder, group), when))
    names = Counter(a.name for a in albums)  # same folder name in two places: say where each lives
    for album in albums:
        if names[album.name] > 1 and album.path != root:
            album.name = f"{album.name}  ({album.path.parent.name})"
    return sorted(albums, key=lambda a: natural_key(a.name))


def quality_rank(album: Album) -> tuple:
    """Quality column order: lossy first, then by bit depth and sample rate, "mixed" last."""
    if len(album.codecs) > 1:
        return (3, 0, 0)
    if album.codecs and album.quality == album.codecs[0]:  # a lossy codec
        return (0, 0, 0)
    return (1 if album.sample_rate else 2, album.bits, album.sample_rate)


def sort_key(column: str):
    """Key function for sorting albums by a column (blanks / unknowns go last when ascending)."""
    if column == "artist":
        return lambda a: (not a.artist, sort_name(a.artist), sort_name(a.name))
    if column == "year":
        return lambda a: (a.year is None, a.year or 0, sort_name(a.name))
    if column == "quality":
        return lambda a: (quality_rank(a), sort_name(a.name))
    if column == "tracks":
        return lambda a: (a.track_count, sort_name(a.name))
    if column == "added":
        return lambda a: (a.added, sort_name(a.name))
    return lambda a: sort_name(a.name)


def sort_albums(albums: list[Album], column: str = "album", reverse: bool = False) -> list[Album]:
    return sorted(albums, key=sort_key(column), reverse=reverse)


@dataclass
class Group:
    name: str                 # "Pink Floyd", "Rock", "1997", "Various Artists", "Unknown year"…
    albums: list[Album]
    unknown: bool = False     # the catch-all group for albums without that tag (listed last)


def _grouped(albums: list[Album], keys_of, unknown: str) -> list[Group]:
    groups: dict[str, Group] = {}
    for album in albums:
        keys = keys_of(album)
        for key in keys or [None]:
            label = unknown if key is None else str(key)
            fold = label.casefold() if key is not None else "\x00unknown"
            if fold not in groups:
                groups[fold] = Group(label, [], unknown=key is None)
            groups[fold].albums.append(album)
    return list(groups.values())


def group_albums(albums: list[Album], view: str) -> list[Group]:
    """The groups a browse view shows, in order: artists / genres / composers A–Z ("The" ignored;
    Various Artists after the named artists), years newest first; albums without the tag last.
    "albums" and "recent" are one flat group."""
    if view == "artists":
        groups = _grouped(albums, lambda a: [a.artist] if a.artist else [], UNKNOWN["artist"])
        order = lambda g: (g.unknown, g.name == VARIOUS, sort_name(g.name))  # noqa: E731
    elif view == "genres":
        groups = _grouped(albums, lambda a: list(a.genres), UNKNOWN["genre"])
        order = lambda g: (g.unknown, sort_name(g.name))  # noqa: E731
    elif view == "composers":
        groups = _grouped(albums, lambda a: list(a.composers), UNKNOWN["composer"])
        order = lambda g: (g.unknown, sort_name(g.name))  # noqa: E731
    elif view == "years":
        groups = _grouped(albums, lambda a: [a.year] if a.year else [], UNKNOWN["year"])
        order = lambda g: (g.unknown, -int(g.name) if not g.unknown else 0)  # noqa: E731
    else:
        return [Group("", list(albums))]
    return sorted(groups, key=order)


def matches(album: Album, words: list[str], fold=str.casefold) -> bool:
    """Library search: every word typed appears in the album's name, title, artist, year,
    genres or composers."""
    haystack = fold(" ".join([album.name, album.title, album.artist, str(album.year or ""),
                              *album.genres, *album.composers]))
    return all(word in haystack for word in words)
