# Ironbark for the web

Ironbark in a web browser: a player for the music on your own computer. Open a folder or some
songs, or drop them on the page, and play them. **Your music never leaves your computer.** The page
reads your files where they are; nothing is uploaded, and the page itself can't reach the internet
(see [Privacy](#privacy)).

It has the desktop player's look and its tall, narrow shape: albums with their covers, search, shuffle
and repeat, a level meter and a spectrum, the same keyboard shortcuts, media keys, and a light and a
dark theme. It also brings one of Ironbark's proof tools: **Check this file** decodes a FLAC file and
compares it, sample for sample, with the fingerprint (MD5) its encoder stored.

## Use it

Open `index.html` from a web address: the Ironbark web page, or your own copy (below). Then:

- **Open a folder** (Ctrl+O) or **Open songs**, or drag songs and folders onto the page.
- Double-click a song (or select it and press Enter) to play it; an album row plays the album.
- **+** opens more music, adds it to the list, or clears the list.
- **ⓘ** (I) shows the song's details. For a FLAC file, **Check this file** proves it decodes exactly.
- Press **?** for every keyboard shortcut.

In Chrome and Edge, Ironbark can remember the folder you open, so the next visit offers "Reopen"
(the browser asks you to allow it again each time). Firefox and Safari open a folder through their
own folder picker instead, and you pick it again on each visit. Chrome and Edge can also install the
page as an app: it then opens in its own window, works offline, and can be picked in "Open with"
for music files.

### Formats

The browser does the decoding, so what plays depends on the browser. FLAC plays in every current
major browser. In the Chromium browser Ironbark was tested in, FLAC, MP3, Ogg Vorbis, Opus, WAV and
AAC play and AIFF doesn't; other browsers differ. The page lists what *your* browser plays on its
opening screen, and marks any song it can't play. The desktop Ironbark plays all of these.

**Apple Lossless (ALAC `.m4a`)** plays in the usual browsers: Safari plays it itself, and in a browser that
can't (Chrome, Edge, Firefox), Ironbark decodes it on your computer first, sample for sample, into
an uncompressed copy held in memory. That takes a moment before a song starts (a second or two for
a four-minute song); the next song is decoded while this one plays, so the change of song stays
short. Mono and stereo, 16 to 32 bits; a song that would need more than about 1.2 GB of memory
decoded (about 18 minutes at 24-bit / 192 kHz) is refused. AAC `.m4a` files are left to the browser.

### What a browser can't do

- **Bit-perfect playback.** A browser mixes the sound of every page and hands it to the computer at
  the rate the output is set to, so the samples are mixed and often resampled on the way. The page says
  so for each song ("Browser plays it at 48 kHz (resampled from 96 kHz)"). The desktop Ironbark plays
  bit-perfect and proves it while it plays.
- **Gapless playback.** The next song is loaded before the current one ends, so the change is
  short, but a browser can't promise no gap at all.
- **Very long hi-res checks.** Check this file decodes the whole song into memory, so it refuses a
  file that would need more than about 1.2 GB (an hour at 192 kHz, say).

## Your own copy

The folder is the whole player: plain HTML, CSS and JavaScript, with no build step and no
libraries. Any web server can serve it. To try it on your computer:

```
cd web
python -m http.server 8000
```

then open <http://localhost:8000>. (Opening `index.html` straight from the folder works in some
browsers and not others. If it doesn't start, the page says so: use a web address.)

## Privacy

- The page carries a Content Security Policy, the browser's own lock: it may load its own files
  and nothing else, and it may not connect anywhere (`connect-src 'none'`). Songs and pictures are
  read from your files through `blob:` addresses that exist only inside the page.
- Settings (theme, volume, shuffle, repeat, the meters) are kept in your browser's local storage.
  The remembered folder, if you allow it, is kept in the browser's own database. Neither is sent
  anywhere. Clearing the site's data in the browser removes both.
- When the page is served from a web address, a small service worker keeps a copy of the page's
  own files, so it opens offline. It never sees your music.

## For developers

- `js/tags.js` reads tags and stream facts (FLAC, ID3v1/v2, MP3 Xing/Info/VBRI, Ogg Vorbis/Opus/FLAC,
  WAV, AIFF, MP4/M4A), reading only the parts of a file it needs.
- `js/verify.js` holds an MD5 (RFC 1321) and the FLAC check.
- `js/alac.js` decodes Apple Lossless `.m4a` files to WAV, in a Web Worker, for browsers that can't
  play them; written from Apple's published ALAC format. `tests/test_web_alac.py` checks it decodes
  files made by Apple's encoder (and FFmpeg's) exactly.
- `js/library.js` turns picked files into albums; `js/player.js` is the playback engine;
  `js/meters.js` draws the meters; `js/app.js` is the page.
- Tests: `node --test "web/tests/*.test.js"` (Node.js 18 or later). The repository's
  `uv run pytest tests/test_web_player.py` runs them too, and checks the page's privacy promises.
