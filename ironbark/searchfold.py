"""One way of folding text for every search in the player, and for the library database's search
index. No UI.

Folding makes a search ignore case and accents: "lodz" finds "Łódź", "bjork" finds "Björk",
"strasse" finds "Straße", "agaetis" finds "Ágætis". Unicode's own decomposition (NFKD) takes the
accents off most letters and turns full-width letters and ligatures ("ﬁ") into plain ones; a few
letters are their own letters rather than accented ones (ł ø ð đ æ œ þ ı), so they're spelt out by
a small table of their usual plain-letter spellings.

- `fold(text)`: the folded text.
- `words(query)`: the folded words typed, split at spaces (the in-tab and Library filters look
  for each one anywhere in a song's text, so "ight" finds "Night" and "ac/dc" finds "AC/DC").
- `tokens(text)`: the folded words as the search index sees them: runs of letters and digits,
  so "AC/DC" is "ac" + "dc".
- `fts_match(query)`: a search typed by anyone, made safe for SQLite's full-text index: each word
  must start a word somewhere ("beet 9" finds Beethoven's 9th). Quotes, brackets, AND / OR / NEAR,
  "-" and "title:" are taken as ordinary text, never as search commands.
- `haystack(track)`: what the in-tab filter looks through for a song.

`FOLD_VERSION` changes whenever folding does, so text folded and stored by an older version
(the library database's search index) is known to need folding again.
"""

from __future__ import annotations

import re
import unicodedata

FOLD_VERSION = 1

# Letters NFKD leaves alone because they aren't an accented letter, spelt the usual plain way.
# (Applied after case folding, so only the lower-case forms are needed; "ß" and "ẞ" already
# fold to "ss".)
_EXTRA = str.maketrans({"ł": "l", "ø": "o", "ð": "d", "đ": "d", "æ": "ae", "œ": "oe", "þ": "th", "ı": "i",
                        "ß": "ss"})
# A word as the search index (SQLite FTS5's "unicode61" tokenizer) sees it: letters and digits;
# everything else (spaces, punctuation, symbols, "_") separates words.
_TOKEN = re.compile(r"[^\W_]+")


def fold(text: str | None) -> str:
    """Lower-case, accents off, and the extra letters spelt plainly: "Łódź" -> "lodz"."""
    if not text:
        return ""
    if text.isascii():
        return text.lower()   # (most text: nothing to take off, and much quicker)
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c)).translate(_EXTRA)


def words(query: str) -> list[str]:
    """The folded words of a search, split at spaces."""
    return fold(query).split()


def tokens(text: str) -> list[str]:
    """The folded words of `text` as the search index sees them ("AC/DC" -> ["ac", "dc"])."""
    return _TOKEN.findall(fold(text))


def fts_match(query: str) -> str | None:
    """An FTS5 MATCH expression for a typed search: every word must start a word somewhere
    ('"beet"* AND "9"*'). None when nothing searchable was typed. Each word is quoted, and only
    letters and digits get through, so nothing typed can act as FTS5 syntax."""
    found = tokens(query)
    if not found:
        return None
    return " AND ".join(f'"{word}"*' for word in found)


def haystack(track) -> str:
    """What the in-tab filter looks through for a song: title, artist, album, album artist,
    composer, genre, year and file name (the whole file's, for a CUE track), folded."""
    year = track.year
    return fold(" ".join((track.title, track.artist, track.album, track.album_artist, track.composer,
                          track.genre, str(year) if year else "", track.path.stem)))
