# Ironbark

**A music player that proves what it plays.** Ironbark plays your lossless music collection —
FLAC, WAV, AIFF, Apple Lossless — and shows, sample for sample, that what reaches your sound
device is exactly what's in the file. It can also tell you whether a file is what it claims to be,
measure it to published standards, and help you check your own ears.

Made by [Digital Greenhouse](https://www.digitalgreenhouse.com.au) in Australia. Free and open
source under the [MIT licence](LICENSE).

> **Status: experimental.** Ironbark is early software (version 0.x), used every day and tested on
> Windows 11, with Linux and macOS builds too. Expect rough edges, and please
> [report them](https://github.com/digitalgreenhouse-au/ironbark/issues).

## Download

Get the latest version from [Releases](https://github.com/digitalgreenhouse-au/ironbark/releases/latest):

| | |
|---|---|
| **Windows 10 and 11** | `Ironbark-Setup-X.Y.Z.exe` (installer) or `Ironbark-X.Y.Z-windows-portable.zip` (no install). Not yet code-signed, so Windows SmartScreen warns about an unknown publisher: choose *More info* → *Run anyway*. Check the download against `SHA256SUMS.txt` if you like. |
| **macOS** | `Ironbark-X.Y.Z-mac-arm64.zip`: unzip it and drag Ironbark to Applications. For Macs with Apple silicon; tested on macOS 26. Not yet signed, so the first time macOS says *"Ironbark" Not Opened*: choose **Done**, open **System Settings → Privacy & Security**, and next to *"Ironbark" was blocked* choose **Open Anyway**, confirm with your password or Touch ID, then **Open Anyway** again. Checksums: `SHA256SUMS-mac.txt`. |
| **Linux** | `Ironbark-X.Y.Z-linux-x86_64.tar.gz`: unpack it anywhere and run `Ironbark/Ironbark`; `Ironbark/install.sh` adds it to your applications menu. Needs a 64-bit PC and a recent distribution (Ubuntu 24.04, Linux Mint 22, Debian 13, Fedora 39 or newer). Built and tested on Linux Mint 22.3 (Cinnamon, X11); Wayland not tested yet. For bit-perfect, choose the sound card's own output (a name ending in `(hw:…)`) under Output; the other outputs go through the system's mixer. |
| **In a web browser** | [Ironbark for the web](https://www.digitalgreenhouse.com.au/labs/ironbark/): nothing to install, and your music never leaves your computer (see below). |

## Why Ironbark

- **Bit-perfect, and proven.** Plenty of players *say* they're bit-perfect. Ironbark fingerprints
  every block it sends to the sound driver and checks it against a second, independent decode of
  the file while the song plays — so "bit-perfect ✓" is a measurement, not a setting.
- **Is this file genuine?** One click checks for files made from MP3s, upsampled "hi-res", 16-bit
  recordings padded to 24-bit, clipping, damaged files, and formats such as HDCD and MQA.
- **Measurement to the standards.** VU, PPM, loudness (EBU R128), true peak, spectrogram, stereo
  scope — each built to its published standard and tested against the official test signals
  where they exist.
- **Simple to live with.** A small window for the corner of your screen: tabs for albums, a
  library that copes with big collections, playlists, gapless playback, a shortcut for everything,
  media keys, a mini player, light and dark themes.

## Install and run

To run from source instead of a download, you need [uv](https://docs.astral.sh/uv/), which also
fetches the right Python version for you.

```
git clone https://github.com/digitalgreenhouse-au/ironbark.git
cd ironbark
uv run ironbark                          # opens your last album, or lets you choose one
uv run ironbark "D:\Music\Some Album"    # or open a folder directly
uv run ironbark "D:\Music\Song.flac"     # a file plays at once (playlists and CUE sheets too)
```

Only one Ironbark runs at a time: starting it again, or opening files with it, hands them to the one
already open and brings it to the front.

Run the tests with `uv run pytest` (add `--perf -m perf` for the large-library speed benchmarks).
`uv run ironbark --self-check` checks that a copy works (sound libraries, the library database and
its background reader, one-player-at-a-time, icons, licences, the main window) without playing
anything or saving anything; its windows open on a hidden desktop, so nothing appears on screen.

### Building the Windows installer

```
pwsh packaging\windows\build.ps1
```

This runs the tests, packages Ironbark with [PyInstaller](https://pyinstaller.org) (no Python
needed on the PC it's installed on), runs the packaged `Ironbark.exe --self-check`, then makes
`Ironbark-Setup-X.Y.Z.exe` with [Inno Setup 6](https://jrsoftware.org/isinfo.php) (if installed),
a portable zip and `SHA256SUMS.txt`, all in `dist\windows\`. The installer installs for the
current user only (no administrator prompt), into `%LOCALAPPDATA%\Programs\Ironbark`; uninstalling
removes the network playback cache's copies of songs (they can always be downloaded again) and
keeps your settings, playlists and library database (play counts and favourites) unless you choose
to remove them. An optional, unticked task adds Ironbark to Explorer's *Open with* list for music
files, playlists and CUE sheets (it never becomes the default). The builds are not yet code-signed,
so Windows SmartScreen warns about an unknown publisher.

### Building the Linux package

```
packaging/linux/build.sh
```

On Linux, with the distribution's Python 3.12 and its Tk (`python3-tk`): uv's own Python can't be
used, as its Tk on Linux draws every font as a bitmap. It runs the tests, packages Ironbark with
PyInstaller, runs the packaged program's `--self-check`, then makes
`Ironbark-X.Y.Z-linux-x86_64.tar.gz` and `SHA256SUMS-linux.txt` in `dist/linux/`. Build on the
oldest distribution the package should run on (it needs that one's C library or newer).

### In a web browser

`web/` is Ironbark for the web: the same look and shape, played by the browser, for the music on
the listener's own computer. Open a folder or some songs, or drop them on the page. Nothing is
uploaded, and a Content Security Policy keeps the page off the internet. It has albums, search,
shuffle and repeat, a level meter and a spectrum, the desktop player's keys, media keys, both themes
and *Check this file* (a FLAC file's MD5, proved by decoding it). A browser can't play bit-perfect
or promise gapless playback, and the page says so. Plain HTML, CSS and JavaScript with no build
step: see [web/README.md](web/README.md). Use it at
<https://www.digitalgreenhouse.com.au/labs/ironbark/>, or run your own copy:
`python -m http.server 8000 -d web`, then <http://localhost:8000>.

## Features

### Playing music

- **Formats:** FLAC, WAV, AIFF and Apple Lossless (ALAC `.m4a`) play exactly, including hi-res
  (24-bit, 88.2 kHz and up). MP3, Ogg Vorbis and Opus play too, clearly labelled **lossy**.
  AAC `.m4a` and FLAC-inside-Ogg are listed but can't be played (said plainly).
- **CUE sheets:** a CD ripped to one long file plus a `.cue` plays as its separate tracks, each
  starting and stopping on its exact sample.
- **Gapless:** tracks run straight into each other with nothing in between (live albums, DJ mixes,
  classical works) whenever they share a sample rate.
- **Tabs** for albums, folders, playlists and the Library. Each tab remembers where it was; opening a
  new tab never interrupts what's playing (the status line pauses the music in another tab). The +
  button opens a blank tab, as a browser does. Drag tabs to reorder them.
- **The Library tab** is always there: every song of every library folder, grouped by album, with
  the search box searching the whole library. Its **views** (Ctrl+L, or the Songs ▾ button) show inside
  the tab: browse by album, artist, genre, year, composer or recently added; sort by any column;
  search. **Most played**, **Recently played** and **Favourites** (♥ — right-click any song) keep your
  listening close, with optional Plays and Last played columns. **Play next / Add to Up next** from any
  song or album; a playlist's *Add songs from the Library…* picks songs there.
- **Select several songs** (Shift+click, Ctrl+click, Shift+↑ / ↓, Ctrl+A) in the track list, the
  Library's song views and the search results: play them next, queue them, add
  them to a playlist or your favourites, edit their tags, drag them onto a tab, or take them out of a
  playlist — one Ctrl+Z puts them all back.
- **Search the whole library** (Ctrl+K): the Library tab's search box finds songs as you type, from
  any window; in any other tab the box lists that tab's matches first, then "From your library", and a
  playlist tab adds a library song with one click on its "+".
- **Built for big collections:** the Library keeps a database of your music, read once in the
  background (at low priority; in several processes for a big library) and then only what changed,
  so the player never freezes. Use **several music folders** (Settings → Library). An unplugged
  drive or a sleeping network share never empties the Library. The Library tab (the whole library)
  opens, scrolls and searches as quickly as an album, even with 200,000
  songs, and an optional **Plays** column shows how often each song has been played.
- **Search** ignores accents and special letters ("lodz" finds Łódź, "strasse" finds Straße) and
  looks at title, artist, album, album artist, composer, genre, year and file name.
- **Spots new music:** albums copied into your music folder appear in the Library by themselves,
  marked as new until you open them.
- **Volume levelling** (Settings → Sound, off by default): Track, Album or Smart, from ReplayGain
  tags or Ironbark's own loudness measurement, never turning a song up past its peak. While it
  changes a song's level the samples aren't the file's own, so the status dot turns amber.
- **Stop after this song** (Ctrl+.) and a **sleep timer** (15–90 minutes or the end of the album;
  Ctrl+Shift+S) — a gentle fade in shared mode, a clean stop at the end of a song in bit-perfect mode.
- **Lyrics** (L): from the song's tags or a `.lrc` / `.txt` file beside it, never from the internet;
  synced lyrics light up line by line.
- **Playlists** are plain `.m3u8` files any player can open. Missing songs stay listed as "file not
  found", so a playlist never quietly loses them; web addresses are listed (not played yet) and saved
  back exactly as they were. A deleted playlist goes to the Recycle Bin, and one
  saved by an older player in an older text format is only rewritten once you've said so.
- **Drag and drop** folders, songs and playlists from Explorer onto the player or onto a tab.
- **Media keys and Windows' media panel**, with play / pause / next / previous on the taskbar preview.
- **Keyboard:** playback, volume and window shortcuts work in every Ironbark window; **F1** lists
  them all. **Tab** reaches every button, switch, choice and slider (a green ring shows where you
  are), and Settings can be worked entirely from the keyboard. Removing or moving a song in a
  playlist can be undone (Ctrl+Z).
- **Opens files from Explorer** (double-click, or *Open with* after the installer's optional task),
  and only one Ironbark runs at a time.
- **Mini player** (Z): a small strip that stays on top, with its own position.
- **A side player, by design:** the window is a tall strip that sits beside whatever you're working
  on. Make it a little narrower or wider and as tall as you like; it never becomes a wide or
  full-screen player — there are good players for that already.
- **Light or dark** themes (or follow Windows); both meet the WCAG AA contrast guidelines. A quiet,
  rounded look: browser-style tabs, pill buttons, rounded fields and lists, and menus with a rounded
  highlight.
- **Files are identified by their contents**, so an MP3 renamed `.flac` is flagged.

### Proof of playback quality (Now Playing, I)

- **Bit-perfect mode** (Windows): uses the sound device in WASAPI exclusive mode at the file's own
  sample rate — the device receives the file's samples untouched. Ironbark checks whether your
  device has its own volume control and protects your ears if it doesn't.
- **The proof:** every block sent is checked, sample for sample, against an independent decode. Any
  change (volume, clipping guard, volume levelling) is reported with where it started.
- **What Windows does to the sound** (shared mode): Windows' own recording of the device's output
  is compared with what Ironbark sent — untouched, resampled, level-changed, or altered by effects.
- **Dropout early warning:** buffer health, slow reads and a log of every dropout with its likely
  cause.
- **Status dot:** green when everything is bit-perfect and proven, amber when something alters
  the sound — hover it to see what.
- **Clipping guard:** many loud masters peak *between* samples above full scale, which can crackle in
  a converter. Ironbark measures each track's true peak ahead of time and turns down only the
  tracks that need it, by just enough. Everything else stays bit-perfect, and so do DTS music CDs
  and HDCD discs, whatever their peaks: any change of level would break their encoding.

### Is this file genuine? (Now Playing → Check)

- **Real bit depth** — a 24-bit file that only ever uses 16 bits is flagged as padded.
- **Where the sound stops** — a sharp wall below 20 kHz suggests a lossy (MP3/AAC) source; a wall at
  20–24.5 kHz in an 88.2 kHz-or-higher file suggests it was upsampled from CD. Worded as clues, never "fake".
- **Clipping**, true peak, **PLR** (peak-to-loudness ratio), **dynamic range** (DR), **loudness**
  (integrated LUFS and loudness range) — per track and per album.
- **Stereo:** correlation, balance, DC offset, mono stored as stereo, a possibly inverted channel.
- **Format flags:** HDCD, MQA and pre-emphasis are detected (not decoded).
- **Lossless check:** decodes the whole file and verifies its stored MD5; damaged files are reported
  with where they stop.
- **Rip-log check** (offline): compares each track with the checksums your EAC or XLD log recorded
  when the CD was ripped.
- **The whole library at once** (Library → *Needs a look*, when you choose to start it): every song
  gets the same check in the background, at low priority, and the view lists what needs a look —
  damaged files, MD5 mismatches, lossy-source, upsampling and padded-bit-depth clues, clipping,
  misnamed files, and HDCD / MQA / pre-emphasis for information. It says how long it will take
  first, can be paused, and carries on where it left off; each result is kept, so Now Playing's
  Check is instant for a song already checked, and new music is checked as it arrives.

### Measuring and listening tools

| Key | Window | What it does |
|---|---|---|
| M | **Meters** | VU (IEC 60268-17), PPM Type I / II (IEC 60268-10), RMS (AES17), true peak, loudness (EBU R128 / ITU-R BS.1770-5) and PSR |
| B | **PCM stream** | The actual sample values as they're heard, in decimal, hex or binary, plus a bitscope |
| F | **Spectrogram** | Live, or the whole track with the cut-off marked |
| O | **Stereo scope** | Vectorscope, correlation, balance and DC offset |
| C | **Clipping** | Every inter-sample over, live and across the whole song; click one to go there |
| D | **Compare** | Are two files the same music, sample for sample? If not, by how much? |
| X | **ABX test** | A blind listening test with a hidden score and its p-value |
| T | **Test signals** | 1 kHz tone (THD+N), J-test (jitter) and sweep (frequency response) to measure a DAC or amp |

### When something goes wrong

- A clear message with **Copy details** and **Open log folder**, never a flood of boxes.
- After Ironbark closed unexpectedly, the next start says so once in the status line, with a
  *Save diagnostics…* link.
- Settings → Help → **Save diagnostics…** saves a zip for a problem report (versions, sound outputs,
  logs and settings) on your computer. The folders in your settings, your home folder and the names
  of tabs, playlists and sound outputs are replaced by stand-ins; song and album names in the logs
  can still show. It is never sent anywhere.
- Settings → Help → **About Ironbark**: the version, every bundled component's licence, and a *Copy
  version info* button.

### Experimental (Settings → Experimental, off until you switch them on)

- **Tag editing** (E): edit titles, artists, albums, covers and more, one song or a whole album. Every
  save keeps a copy of the old tags (so it can be undone) and proves the audio is untouched before
  the file is replaced.
- **Tape reels** (W): a reel-to-reel deck playing along. The album playing is the tape: the supply
  reel empties onto the take-up reel as the songs play, and each reel turns at its true speed — the
  tape runs at a steady 7½ or 15 inches a second, so a reel's rpm is that speed over its tape pack's
  circumference (the full reel slowly, the emptier one faster). 7-inch or 10½-inch reels; a seek
  spools the tape there. F11 or a double-click fills the screen.
- **Hear clipping:** in the Clipping window, switch between an ordinary converter's clipped sound and
  the clean version at the same point in the music.
- **Network playback cache:** songs on a network drive (a NAS) play from a copy fetched to this
  computer: a short "Preparing track…" first, then a dropped network or a sleeping server doesn't
  interrupt the song — a longer outage pauses it at a quiet moment and it carries on by itself from
  exactly the same sample. The next songs download ahead, Up next and the track list's *Cached*
  column show how far each has got, and the seek bar shows what's downloaded. A copy deleted behind
  its back is fetched again from the same spot. Settings → Network playback sets the buffering, how far ahead
  to fetch, how long copies are kept and how much room they may take. Each finished copy is checked
  against the original (a FLAC file's own fingerprint too).

## Keyboard

Press **F1** (or **?**) in the player for this list, with a filter box. Most keys work in every
Ironbark window; while you're typing in a box, only the Ctrl and function-key shortcuts act
(Ctrl+Space still plays and pauses).

### Playback

| Key | Action |
|---|---|
| Space · Ctrl+Space | Play / pause (Ctrl+Space works while you type) |
| N · Ctrl+→ | Next song |
| P · Ctrl+← | Previous song |
| ← / → | Back / forward 5 seconds |
| Shift+← / Shift+→ | Back / forward 30 seconds |
| 0 – 9 | Jump to 0% – 90% of the song (0: the start) |
| S | Shuffle on / off |
| R | Repeat: off, the album, the song |
| Ctrl+↑ / Ctrl+↓ | Volume up / down 5% |
| Ctrl+Shift+↓ | Mute / unmute |
| Ctrl+Shift+↑ | Volume 100% (untouched) |
| Ctrl+. | Stop after this song (again: carry on) |
| Ctrl+Shift+S | Sleep timer and Stop after (a menu) |

### Tabs & lists

| Key | Action |
|---|---|
| Ctrl+T | New tab (the + button) |
| Ctrl+W | Close the tab |
| Ctrl+Shift+T | Reopen the tab closed last |
| Ctrl+Tab · Ctrl+PgDn | Next tab |
| Ctrl+Shift+Tab · Ctrl+PgUp | Previous tab |
| Ctrl+Shift+PgUp / Ctrl+Shift+PgDn | Move the tab left / right |
| Ctrl+1 – Ctrl+8 | Go to tab 1 – 8 |
| Ctrl+9 | Go to the last tab |
| Ctrl+O | Open a folder |
| Ctrl+Alt+O | Open a playlist file |
| Ctrl+N | New playlist |
| Ctrl+F · / | Search this tab |
| Enter | Track list: play the song (an album row: the album) |
| Ctrl+Enter | Track list: add to Up next |
| Ctrl+Shift+Enter | Track list: play next |
| Ctrl+A | Track list: select every song |
| Ctrl+Shift+O | Track list: open the song's album in a new tab |
| Ctrl+Shift+A | Track list: add the library song found to this playlist |
| Menu key · Shift+F10 | The selected row's right-click menu |
| Shift+click · Ctrl+click · Shift+↑ / ↓ | Track list, the Library's songs, the library search's results: select several songs |

### Windows & tools

| Key | Action |
|---|---|
| I | Now Playing |
| M | Meters |
| B | PCM stream |
| C | Clipping |
| F | Spectrogram |
| O | Stereo scope |
| D | Compare two files |
| X | ABX blind test |
| T | Test signals |
| L | Lyrics |
| W | Tape reels (Experimental) |
| Z · Ctrl+Shift+M | Mini player and back |
| Ctrl+, · Ctrl+P | Settings |
| Ctrl+Shift+H | Snap the window to its home spot |
| F1 · ? | Keyboard shortcuts (this list) |
| Ctrl+Q | Quit Ironbark |
| Esc | Close a menu or window (a search box: clear it first) |
| Tab · Shift+Tab | A question box: go from button to button (Enter presses the one ringed in green) |
| A · B · X · Space | ABX window: hear A, B or X · stop or replay |
| A · B · D · Space | Clipping (Hear it on): hear A, B or D · stop or replay |

### Library & search

| Key | Action |
|---|---|
| Ctrl+L | The Library's views (Albums, Artists, …) in the Library tab; again: back |
| Ctrl+K · Ctrl+Shift+F | Search the whole library (the Library tab's search box) |
| Ctrl+F · / | Library views: search box |
| Ctrl+A | Library views: select every song (the song views) |
| Enter | Library views: open the album in a new tab (a song: play it) |
| Ctrl+Enter | Library views: add the album to Up next |
| Ctrl+Shift+Enter | Library views: play the album next |
| Ctrl+Tab / Ctrl+Shift+Tab | Library views: next / previous view (Albums, Artists, …) |
| F5 | Library: look for added or changed music (the ↻ by its search box) |
| ↓ · Enter | Search box: go to the list · play (open) the first result |
| Enter · Shift+Enter · Ctrl+Enter · Alt+Enter | Library search (Ctrl+K): play · play next · add to Up next · open the album |
| A – Z · 0 – 9 | Library list: jump to the first name starting with what you type |

### Editing

| Key | Action |
|---|---|
| Delete | Remove the song from the playlist |
| Alt+↑ / Alt+↓ | Move the song up / down the playlist |
| Ctrl+Z | Undo: songs removed or moved, or taken out of Up next |
| F2 | Rename the tab (a playlist: its file) |
| E | Edit tags of the selected song (Experimental) |
| Ctrl+S | Edit tags: save |

## Privacy

Ironbark doesn't collect anything about you or your music, and **it never uses the internet**: no
update check, no look-ups, no usage tracking. Lyrics, the rip-log check and the library's song search
all work from files on your computer.

Music on your home network (a NAS, a shared folder, a mapped drive like Z:) and OneDrive plays as
any program reads a file. With Settings → Library → **Local network access** on,
the network playback cache keeps copies of those songs on this computer; it only ever talks to the
drives your music is already on.

Settings, playlists and the logs are kept on your computer (on Windows, in `%APPDATA%\Ironbark`);
the library database (with your play counts and favourites) and the network playback cache's copies
are beside them (on Windows, in `%LOCALAPPDATA%\Ironbark`). If something goes wrong, Settings →
Help → *Save diagnostics…* saves a zip on your computer that you can choose to attach to a problem
report (versions, sound outputs, logs and settings). In it, the folders in your settings, your home
folder and the names of tabs, playlists and sound outputs are replaced by stand-ins such as
`<folder 1>`. Song, album and artist names the logs mention, and folders outside those, can still
show: look through it before you send it. It is never sent anywhere.

## Feedback

Found a bug or have an idea? Please open an [issue](https://github.com/digitalgreenhouse-au/ironbark/issues).
Bug fixes are welcome as pull requests — see [CONTRIBUTING.md](CONTRIBUTING.md). Security problems:
please report them privately, as [SECURITY.md](SECURITY.md) explains.

## Licence

MIT — see [LICENSE](LICENSE) (copyright Digital Greenhouse). Built with
[soundfile](https://github.com/bastibe/python-soundfile) (BSD-3, bundles libsndfile under LGPL-2.1,
loaded as a separate, replaceable library),
[sounddevice](https://github.com/spatialaudio/python-sounddevice) (MIT, bundles PortAudio under MIT),
NumPy (BSD-3 and others), [Pillow](https://python-pillow.org) (MIT-CMU) and
[tkinterdnd2](https://github.com/Eliav2/tkinterdnd2) (MIT, bundles the tkdnd Tk extension under
its BSD-style licence). Music tags are read and written by Ironbark's own code — there is no GPL
code in Ironbark. The full list, with every licence, is in
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).

_Ironbark was called "DG FLAC Player" until version 0.26.0._
