"""The numbered genre list that ID3v1 started (also used by ID3v2's "(17)" references and MP4's
numeric "gnre" atom). No UI.

0–79: the ID3v2.3.0 specification, Appendix A "Genre List from ID3v1" (id3.org/id3v2.3.0, read
05/10/2026), spelt exactly as published. 80–191: the later Winamp additions, as listed on
Wikipedia's "List of ID3v1 genres" (read 05/10/2026) — no single official spelling exists for
these, so they're unverified against an original source.
"""

GENRES = (
    "Blues", "Classic Rock", "Country", "Dance", "Disco", "Funk", "Grunge", "Hip-Hop", "Jazz", "Metal",
    "New Age", "Oldies", "Other", "Pop", "R&B", "Rap", "Reggae", "Rock", "Techno", "Industrial",
    "Alternative", "Ska", "Death Metal", "Pranks", "Soundtrack", "Euro-Techno", "Ambient", "Trip-Hop",
    "Vocal", "Jazz+Funk", "Fusion", "Trance", "Classical", "Instrumental", "Acid", "House", "Game",
    "Sound Clip", "Gospel", "Noise", "AlternRock", "Bass", "Soul", "Punk", "Space", "Meditative",
    "Instrumental Pop", "Instrumental Rock", "Ethnic", "Gothic", "Darkwave", "Techno-Industrial",
    "Electronic", "Pop-Folk", "Eurodance", "Dream", "Southern Rock", "Comedy", "Cult", "Gangsta", "Top 40",
    "Christian Rap", "Pop/Funk", "Jungle", "Native American", "Cabaret", "New Wave", "Psychadelic", "Rave",
    "Showtunes", "Trailer", "Lo-Fi", "Tribal", "Acid Punk", "Acid Jazz", "Polka", "Retro", "Musical",
    "Rock & Roll", "Hard Rock",
    # 80–191 (Winamp extensions; see the module note)
    "Folk", "Folk rock", "National folk", "Swing", "Fast fusion", "Bebop", "Latin", "Revival", "Celtic",
    "Bluegrass", "Avantgarde", "Gothic rock", "Progressive rock", "Psychedelic rock", "Symphonic rock",
    "Slow rock", "Big band", "Chorus", "Easy listening", "Acoustic", "Humour", "Speech", "Chanson", "Opera",
    "Chamber music", "Sonata", "Symphony", "Booty bass", "Primus", "Porn groove", "Satire", "Slow jam",
    "Club", "Tango", "Samba", "Folklore", "Ballad", "Power ballad", "Rhythmic Soul", "Freestyle", "Duet",
    "Punk rock", "Drum solo", "A cappella", "Euro-house", "Dance hall", "Goa music", "Drum & bass",
    "Club-house", "Hardcore techno", "Terror", "Indie", "Britpop", "Negerpunk", "Polsk punk", "Beat",
    "Christian gangsta rap", "Heavy metal", "Black metal", "Crossover", "Contemporary Christian",
    "Christian rock", "Merengue", "Salsa", "Thrash metal", "Anime", "Jpop", "Synthpop", "Christmas",
    "Art rock", "Baroque", "Bhangra", "Big beat", "Breakbeat", "Chillout", "Downtempo", "Dub", "EBM",
    "Eclectic", "Electro", "Electroclash", "Emo", "Experimental", "Garage", "Global", "IDM", "Illbient",
    "Industro-Goth", "Jam band", "Krautrock", "Leftfield", "Lounge", "Math rock", "New romantic",
    "Nu-breakz", "Post-punk", "Post-rock", "Psytrance", "Shoegaze", "Space rock", "Trop rock",
    "World music", "Neoclassical", "Audiobook", "Audio theatre", "Neue Deutsche Welle", "Podcast",
    "Indie rock", "G-Funk", "Dubstep", "Garage rock", "Psybient",
)
assert len(GENRES) == 192


def name(number: int) -> str:
    """The genre for a number, or "" if the number isn't in the list."""
    return GENRES[number] if 0 <= number < len(GENRES) else ""


def number(genre: str) -> int | None:
    """The number for a genre name (ignoring case), or None."""
    lowered = genre.strip().lower()
    for i, known in enumerate(GENRES):
        if known.lower() == lowered:
            return i
    return None
