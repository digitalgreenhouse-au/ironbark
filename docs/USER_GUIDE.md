# Ironbark user guide

Ironbark is a music player for your own music files. It plays them exactly as they were recorded,
and it can **prove** it: you don't have to take "bit-perfect" on trust. It also has tools that tell
you whether a file is what it claims to be, measure it, and test your own ears.

This guide covers Ironbark 0.33 on Windows. Every key mentioned here is also listed in the player:
press **F1** (or **?**).

**Contents:** [Getting started](#getting-started) · [Playing music](#playing-music) ·
[The Library and song search](#the-library-and-song-search) ·
[Bit-perfect, and the proof](#bit-perfect-and-the-proof) ·
[The Check and the measuring tools](#the-check-and-the-measuring-tools) ·
[Network playback (Experimental)](#network-playback-experimental) · [Keyboard](#keyboard) ·
[Troubleshooting](#troubleshooting) · [Privacy](#privacy) ·
[Where Ironbark keeps things](#where-ironbark-keeps-things)

## Getting started

### Installing

- **The installer** (`Ironbark-Setup-….exe`) installs Ironbark for you only, so it never asks for
  an administrator password. It goes into `%LOCALAPPDATA%\Programs\Ironbark` and adds a Start menu
  entry. Two optional boxes, unticked unless you tick them, add a desktop icon and add Ironbark to
  Explorer's *Open with* list for music files, playlists and CUE sheets. Ironbark never makes itself
  the default player.
- **The portable zip** needs no installing: unzip it and run `Ironbark.exe`. Unzip it somewhere with
  a short path, such as `C:\Ironbark`. Windows can't load the program's files from a folder buried
  very deep, and Ironbark tells you so if that happens.
- To install a newer version, run its installer. If Ironbark is open, the installer offers to close
  it first; the last page's *Launch Ironbark* box starts it again. Your settings, playlists and
  library are kept.

### The first start

The first time, the window is empty apart from an **Open your library** button. Ironbark looks for
music in your Windows *Music* folder to begin with. If your music is somewhere else (another drive,
a network drive), open **Settings** (the ⚙ button at the top, or Ctrl+,) and use **Library → Add folder…**.
You can add several folders.

The Library reads your folders once, in the background and at low priority, so the player stays
responsive while it works. After that it only looks at what has changed. With **Watch for new
music** on (Settings → Library), albums you copy in later turn up by themselves, marked as new until
you open them.

You don't have to wait for the Library to play something. Any of these works straight away:

- **Ctrl+O** opens a folder (an album, or a whole drive) in a tab.
- **Drag** a folder, songs or a playlist from Explorer onto the window or onto a tab.
- **Double-click** a music file in Explorer, or use *Open with* (if you ticked that box). A file
  opens in the Ironbark that's already running: only one copy of Ironbark runs at a time.
- The **+** button beside the tabs (Ctrl+T) offers *Browse library…*, *Open a folder…*,
  *New playlist…* and *Open a playlist file…*.

### The window

Ironbark is designed to live in a tall, narrow window at the side of your screen while you work or
do other things. You can make it a little narrower or wider, and as tall as you like; it never
becomes a wide or full-screen player. **Z** shrinks it to a **mini player**, a small strip that
stays on top, and Z brings it back.

To keep the window in a favourite spot, put it there and choose **Use current** in Settings →
Window. **Snap to home** (or Ctrl+Shift+H, or a double-click on the empty strip at the top) brings
it back to that spot. **Open at home** starts it there.

Settings → Window also has the **Theme** (Dark, Light, or Follow Windows), **Album art** on or off,
**Keep on top**, and **Media keys** (your keyboard's play / pause / next keys and Windows' own media
panel).

## Playing music

### Tabs

Each album, folder, playlist or Library view you open gets its own **tab**. A tab remembers where it
was, and opening a new tab never stops what's playing. Drag tabs to put them in order;
Ctrl+W closes one and Ctrl+Shift+T opens the last one closed again.

In a tab, double-click a song (or press Enter) to play it. Right-click a song for everything else:
*Play next*, *Add to Up next*, *Add to playlist*, *Add to favourites* (♥) and more (in a
playlist, *Move up*, *Move down* and *Remove from playlist* too). You can select several songs at once with Shift+click, Ctrl+click,
Shift+↑ / ↓ or Ctrl+A, and act on them all together.

### Up next

**Up next** is a short list of songs to play before the tab carries on. *Play next* puts a song at
its front, *Add to Up next* at its end. The Up next line above the track list shows what's coming;
click it to see the list, start a song from it, or take one out.

### Shuffle, repeat, stopping

- **S** turns shuffle on and off. **R** repeats the album, then the song, then neither.
- **Stop after this song** (Ctrl+.) does what it says; press it again to carry on.
- The **sleep timer** (Ctrl+Shift+S) stops the music after 15 to 90 minutes, or at the end of the
  album. Normally the music fades out. In bit-perfect mode it stops cleanly at the end of a song
  instead, because a fade would change the samples.

### Playlists

Playlists are plain `.m3u8` files that any player can open. **Ctrl+N** makes a new one; drag songs
or albums onto its tab, or right-click songs and choose *Add to playlist*. In a playlist, **Delete**
takes a song out, **Alt+↑ / ↓** moves it, and **Ctrl+Z** undoes either. **F2** renames the
playlist. A song that can't be found (a drive unplugged, a file moved) stays listed as "file not
found", so a playlist never quietly loses songs. A playlist you delete goes to the Recycle Bin.

### What it plays

- **Lossless:** FLAC, WAV, AIFF and Apple Lossless (ALAC `.m4a`), including hi-res (24-bit,
  88.2 kHz and higher). These play exactly.
- **Lossy:** MP3, Ogg Vorbis and Opus play too, and are clearly labelled lossy. (Lossy formats throw
  away part of the sound to save space.)
- **Not playable:** AAC `.m4a` files and FLAC inside an Ogg file are listed, and Ironbark says
  plainly that it can't play them.
- **CUE sheets:** a CD saved as one long file plus a `.cue` file plays as its separate tracks.
- **Gapless:** songs run straight into each other, with no gap, whenever they share a sample rate
  (live albums, DJ mixes, classical works).

### Volume levelling

Settings → Sound → **Volume levelling** (off at first) plays songs at about the same loudness.
*Track* levels each song on its own; *Album* keeps the balance between an album's songs; *Smart*
uses Album while an album plays in order and Track otherwise. Ironbark uses ReplayGain tags (loudness
notes some programs write into files) or measures the songs itself, and never turns a song up past
the point where it would clip. While levelling changes a song's level, the song isn't bit-perfect,
and the player says so.

### Lyrics

**L** shows the playing song's lyrics, from the file's own tags or a `.lrc` or `.txt` file beside
it. Lyrics with timings light up line by line. Ironbark never fetches lyrics from the internet.

## The Library and song search

### The Library window

**Ctrl+L** opens the Library. Across the top are its views: **Albums**, **Artists**, **Genres**,
**Years**, **Composers**, **Recently added**, **Songs**, **Most played**, **Recently played**,
**Favourites** and **Needs a look** (see [the whole library at once](#the-whole-library-at-once)).
Ctrl+Tab moves between them. Click a column heading to sort by it, and type in the search box to
narrow the list.

Double-click an album (or press Enter) to open it in a tab. Ctrl+Enter adds it to Up next and
Ctrl+Shift+Enter plays it next. **Open all albums in a tab** puts every album on show into one tab.
That works even for a very large library: a tab of 200,000 songs opens in a few seconds. **F5**
looks for music added or changed since the last look.

An unplugged drive or a sleeping network drive never empties the Library: its albums wait until
the drive is back. If you plug in a different drive under the same letter, the Library notices and
waits for its own drive.

### Searching for a song

**Ctrl+K** (from any Ironbark window) opens the song search. Type two or more letters: songs, albums
and artists appear as you type. Search looks at the title, artist, album, album artist, composer,
genre, year and file name, and it ignores accents ("lodz" finds Łódź, "strasse" finds Straße).

In the results, **Enter** plays the song in its album, **Shift+Enter** plays it next,
**Ctrl+Enter** adds it to Up next, and **Alt+Enter** opens its album.

### Favourites and plays

Right-click any song and choose *Add to favourites* to give it a ♥. A song counts as played once
you've heard half of it (or 4 minutes of a long one), so **Most played** and **Recently played** fill
themselves. The track list can show a **Plays** column too (right-click a song, then *Columns*), and
the Library's song views can show **Plays** and **Last played**. Play counts and favourites are kept
on your computer, in the library database.

## Bit-perfect, and the proof

### What "bit-perfect" means

A music file is a long list of numbers, called samples. **Bit-perfect** means your sound device
(your DAC, which stands for digital-to-analogue converter, or your computer's own sound) receives
exactly those numbers, unchanged.

Normally Windows mixes the sound of every program together (this is called **shared mode**). To do
that it may change the volume and convert the music to its own sample rate, so the device doesn't
get the file's own samples. Settings → Sound → **Bit-perfect** switches Ironbark to WASAPI
exclusive mode (Windows' way of letting one program use the device on its own). The device then gets
the file's samples at the file's own rate. While a song plays this way, other programs' sounds go
quiet.

### Your ears come first

In bit-perfect mode the Windows volume slider is bypassed. If your device has a volume control of
its own, Ironbark uses it. If it hasn't (many DACs haven't), Ironbark's own volume is the only one,
so Ironbark checks for this, starts quietly, and **asks before turning the volume right up**. For
that question, Enter and Esc keep the volume where it is. Only choosing *Turn it up* on purpose
goes ahead. Turn your amplifier or headphone volume down before you turn Ironbark up.

### The proof

Plenty of players claim to be bit-perfect. Ironbark checks. While a song plays it fingerprints every
block of samples it hands to the sound driver, and compares it with a second, independent reading of
the file. Open **Now Playing** (I) and look at **Proof**:

- **✓ Bit-perfect, proven** means every sample sent so far is identical to the file.
- If something changed the samples, Proof says what (the volume, volume levelling, the clipping
  guard) and where it started.
- In shared mode Proof shows that Ironbark sent the samples unchanged. Windows then mixes them.
  **Check what Windows does to the sound** compares Windows' own recording of the device's output
  with what Ironbark sent, and tells you whether Windows left it untouched, resampled it, changed its
  level, or altered it with effects.

The small **status dot** at the top right, beside the lock (Bit-perfect) and ⓘ (Now Playing)
buttons, sums this up: green when everything is bit-perfect and proven, amber when something is
changing the sound, grey when all is well but the music isn't bit-perfect (shared mode). Hover over
it to see why; click it for Now Playing.

### The clipping guard

Many loud recordings have peaks that go above full scale *between* the samples. Some converters
crackle on those. With the **Clipping guard** on (Settings → Sound), Ironbark measures each song's
true peak ahead of time and turns down only the songs that need it, by just enough. Every other song
stays bit-perfect. If your converter is known to have room above full scale, **My DAC has headroom**
tells the guard how much.

## The Check and the measuring tools

### Is this file what it claims to be?

Open **Now Playing** (I) and press **Verify & examine** (for a FLAC file) or **Examine** under
**Check**. Ironbark reads the whole file and reports:

- **Real bit depth:** a 24-bit file that only ever uses 16 bits is flagged as padded.
- **Where the sound stops:** a sharp cut-off below 20 kHz suggests the file was made from an MP3 or
  AAC; a cut-off near 20–24 kHz in a hi-res file suggests it was upsampled from a CD. These are
  given as clues, never as "fake".
- **Clipping**, **true peak**, **loudness** (in LUFS, which stands for Loudness Units relative to
  Full Scale), **loudness range**, **dynamic range** (DR) and **PLR** (peak-to-loudness ratio).
- **Stereo:** balance, correlation, DC offset (a constant offset in the signal), mono saved as
  stereo, and a channel that may be upside down.
- **Format flags:** HDCD, MQA and pre-emphasis are spotted (not decoded).
- **A lossless check:** a FLAC file stores its own fingerprint (an MD5 checksum). Ironbark decodes
  the whole file and checks it. A damaged file is reported with where the damage starts.

Below that you can **measure the whole album** (dynamic range and loudness), **check a rip against
its rip log** (the EAC or XLD log saved when the CD was ripped; this works offline), and **verify a
rip online** against the AccurateRip and CUETools databases. The online check only works once you
allow network access (see [Privacy](#privacy)).

### The whole library at once

The Library's **Needs a look** view can check every song in your library the same way. It tells you
how long that will take before it starts, runs in the background at low priority, can be paused,
and carries on where it left off. It then lists what needs a look: damaged files, fingerprint
mismatches, lossy-source and upsampling clues, padded bit depth, clipping and misnamed files.
Results are kept, so Now Playing's Check is instant for a song already checked, and new music is
checked as it arrives.

### The measuring and listening tools

| Key | Window | What it's for |
|---|---|---|
| M | **Meters** | VU and PPM meters, RMS, true peak, loudness (EBU R128) and PSR (peak-to-short-term-loudness ratio), each built to its published standard |
| B | **PCM stream** | The actual sample values as you hear them, in decimal, hex or binary, plus a bitscope |
| F | **Spectrogram** | The sound's frequencies over time, live or for the whole song, with any cut-off marked |
| O | **Stereo scope** | A picture of the stereo image, with correlation, balance and DC offset |
| C | **Clipping** | Every over (a peak past full scale), live and across the whole song: click one to go there |
| D | **Compare** | Are two files the same music, sample for sample? If not, by how much? |
| X | **ABX test** | A blind listening test: can you really hear the difference between A and B? |
| T | **Test signals** | A 1 kHz tone, a J-test and a sweep, for measuring a DAC or amplifier |

The ABX test and Test signals play sound of their own. Test signals start at a safe level
(−20 dBFS), but turn your amplifier down before you begin, just in case.

## Network playback (Experimental)

Music on a network drive (a NAS, `\\server\share`, or a mapped drive such as `Z:`) plays without any
special setting: Windows fetches the files, as it does for any program. On a busy or patchy network,
though, a song can stall.

The **network playback cache** fixes that. It's still Experimental, so it's off until you switch on
Settings → Experimental → **Network playback cache** and restart Ironbark. With it on, a network song
plays from a copy fetched to your computer. You'll see "Preparing track…" for a moment first. After
that, a dropped network or a sleeping server doesn't interrupt the song. A longer outage pauses it
at a quiet moment, and it carries on by itself from exactly the same sample. The next songs download
ahead, so they follow on without a wait.

Settings → **Network playback** (shown while the cache is on) has:

- **Playback buffering:** *Intelligent* measures your network and waits only as long as it needs
  to; *Fast start* starts as soon as the initial buffer is in; *Complete track* fetches and checks
  the whole song before it plays.
- **Initial buffer:** how much to fetch before a song starts (Intelligent waits less on a fast,
  steady network).
- **Pre-download:** how far ahead songs download (the next song, the next two, or up to 200 songs
  ahead).
- **Keep cached tracks** and **Maximum cache size:** how long copies stay and how much room they
  may take. At least 2 GB (or 5% of the drive) is always left free.
- **Check finished downloads:** each copy is checked against the original before it's trusted (a
  FLAC file's own fingerprint too).
- **Empty the cache** deletes the copies (never your music) and **Open folder** shows where they
  are.

In a tab of network songs, the track list's **Cached** column (and Up next) shows each song's state:
● in the cache, ◐ partly downloaded, ○ waiting, ! couldn't download (it will try again). The network
playback cache only talks to the network drives your music is already on. It never uses the
internet.

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
| Ctrl+T | New tab menu (the + button) |
| Ctrl+W | Close the tab |
| Ctrl+Shift+T | Reopen the tab closed last |
| Ctrl+Tab · Ctrl+PgDn | Next tab |
| Ctrl+Shift+Tab · Ctrl+PgUp | Previous tab |
| Ctrl+Shift+PgUp / Ctrl+Shift+PgDn | Move the tab left / right |
| Ctrl+1 – Ctrl+8 | Go to tab 1 – 8 |
| Ctrl+9 | Go to the last tab |
| Ctrl+O | Open a folder |
| Ctrl+Shift+O | Open a playlist file |
| Ctrl+N | New playlist |
| Ctrl+F · / | Search this tab |
| Enter | Track list: play the song (an album row: the album) |
| Ctrl+Enter | Track list: add to Up next |
| Ctrl+Shift+Enter | Track list: play next |
| Ctrl+A | Track list: select every song |
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
| Ctrl+L | Library (brings it to the front when it's open) |
| Ctrl+K · Ctrl+Shift+F | Search the library |
| Ctrl+F · / | Library: search box |
| Ctrl+A | Library: select every song (the song views) |
| Enter | Library: open the album in a new tab |
| Ctrl+Enter | Library: add the album to Up next |
| Ctrl+Shift+Enter | Library: play the album next |
| Ctrl+Tab / Ctrl+Shift+Tab | Library: next / previous view (Albums, Artists, …) |
| F5 | Library: look for added or changed music |
| ↓ · Enter | Search box: go to the list · play (open) the first result |
| Enter · Shift+Enter · Ctrl+Enter · Alt+Enter | Library search (Ctrl+K): play · play next · add to Up next · open the album |
| A – Z · 0 – 9 | Library list: jump to the first name starting with what you type |

### Editing

| Key | Action |
|---|---|
| Delete | Remove the song from the playlist |
| Alt+↑ / Alt+↓ | Move the song up / down the playlist |
| Ctrl+Z | Undo: songs removed or moved, or taken out of Up next |
| F2 | Rename the playlist |
| E | Edit tags of the selected song (Experimental) |
| Ctrl+S | Edit tags: save |

## Troubleshooting

**No sound, or the wrong speakers.** Open Settings → Sound and choose the **Output** at the top.
If the device you chose isn't connected, Ironbark says so and plays on the system default. If it
says *Can't open the sound device*, check the Output, then press play.

**Other programs went quiet.** That's bit-perfect mode: it has the device to itself while a song
plays. Switch **Bit-perfect** off in Settings → Sound to share the device again.

**"Device won't take … in bit-perfect mode (or another app holds it): playing shared".** Either the
device can't play that sample rate, or another program is using it in exclusive mode. Close the
other program and play the song again. Your music still plays meanwhile, through Windows' mixer.

**The status dot is amber.** Hover over it: it says what's changing the sound (for example the
volume below 100% in bit-perfect mode on a device without its own volume, volume levelling, or the
clipping guard on one song).

**Music is missing from the Library.** Check its folder is listed in Settings → Library (add it
with **Add folder…**), then press **F5** in the Library. If a whole drive's music has gone quiet,
the drive is probably unplugged or asleep: its albums come back when it does. A folder Windows can't
open is named in the Library's status line.

**A playlist says "file not found".** The song was moved, renamed or is on a drive that isn't
connected. Plug the drive in, or put the file back, and it plays again.

**Crackles on loud songs.** Switch on the **Clipping guard** (Settings → Sound).

**Network songs stall.** Try the [network playback cache](#network-playback-experimental).

**The portable copy says its folder path is too long.** Move the Ironbark folder somewhere shorter,
such as `C:\Ironbark`, and run it from there.

**Windows SmartScreen warns about the installer.** Builds that aren't code-signed yet show this
warning. Only run an installer you downloaded from Digital Greenhouse's own website.

**Ironbark closed unexpectedly.** The next time it starts, the status line says so once, with a
*Save diagnostics…* link.

### Diagnostics: what to send with a problem report

Settings → Help → **Save diagnostics…** saves a `.zip` file on your computer. Nothing is sent
anywhere: you decide whether to attach it to a problem report. It holds Ironbark's version, your
Windows version, the sound outputs, your settings and Ironbark's logs. The folders in your
settings, your home folder and the names of tabs, playlists and sound outputs are replaced by
stand-ins such as `<folder 1>`. Song, album and artist names the logs mention, and folders outside
those, can still show, so look through it before you send it.

Also in Settings → Help: **Open log folder** shows `crash.log` and `app.log`, and **Detailed
logging** makes the log note more while you reproduce a problem (turn it off again afterwards).
**About Ironbark** shows the version and has a *Copy version info* button.

## Privacy

Ironbark doesn't collect anything about you or your music, and it has no usage tracking. Nothing
uses the internet until you switch on Settings → Network → **Allow network access**. Then only two
features can, and only when you use them:

- **Rip verification:** when you press *Verify rip online*, Ironbark sends the disc's IDs and track
  layout to AccurateRip and CUETools DB. Those websites see your computer's internet address and the
  Ironbark version, as any website does. The CUETools DB look-up isn't encrypted.
- **Check for updates** (Settings → Help, a second switch, also off at first): once a day it asks
  digitalgreenhouse.com.au for a small file saying what the latest version is. Nothing is downloaded
  or installed; a newer version is only mentioned in the status line.

Lyrics, the rip-log check and song search all work offline. Diagnostics are never sent anywhere.

## Where Ironbark keeps things

| What | Where (Windows) |
|---|---|
| Settings, playlists, logs | `%APPDATA%\Ironbark` |
| The library database (with play counts and favourites) | `%LOCALAPPDATA%\Ironbark` |
| The network playback cache's copies | `%LOCALAPPDATA%\Ironbark\NetCache` |

Ironbark only changes your music files when you ask it to: editing tags (Experimental), or saving
and deleting playlists. Uninstalling removes the network playback cache's copies, and asks whether to
remove your settings, playlists and library database too (No keeps them for next time). Your music
files are never touched.
