# Changelog

Ironbark follows [semantic versioning](https://semver.org): MAJOR.MINOR.PATCH — a PATCH fixes
something, a MINOR adds a feature; MAJOR stays 0 until version 1.0. The version is shown at the
bottom of Settings. Dates are dd/mm/yyyy. Newest first.

## 0.31.0 — 07/10/2026

New features for big collections, the keyboard, and everyday listening.

**Library**
- The Library now keeps a database of your music, built for large collections. The first look
  reads tags in the background at low priority (in several processes for a big library) and can
  be stopped: it carries on where it left off next time. The player never freezes while the Library, start-up or the new-music check
  works things out.
- Several music folders: Settings → Library lists them, with Add folder… and Remove. A folder
  inside one already listed isn't added twice.
- Your old library index is brought across once, with every date kept, so Recently added and the
  new-music dots carry on as before.
- An unplugged drive, a sleeping network share or a folder Windows won't open never empties the
  Library or makes music look new when it comes back. A download still growing in the music
  folder is looked at on its own, less and less often, instead of the whole library.
- Searching ignores accents and special letters ("lodz" finds Łódź, "strasse" finds Straße) and
  now also looks at album artist, composer, genre and year. Loading the whole library into a tab
  is much quicker.
- Songs you listen to (half a song, or 4 minutes of a long one) are counted, ready for Most played
  and Recently played views to come.
- If the library database is ever damaged, it's repaired at the next start, keeping your play
  counts and favourites.

**Keyboard**
- Playback, volume and window shortcuts now work in every Ironbark window (Now Playing, Meters,
  the Library…), not only the main one, and letter keys work with Caps Lock or Shift held.
  Single-letter keys wait while you type in a box (Ctrl+Space still plays and pauses).
- New keys for volume, mute, jumping through a song, changing tabs, Up next, undo (Ctrl+Z), Stop
  after this song (Ctrl+.), the sleep timer (Ctrl+Shift+S) and lyrics (L). Moving a song in a
  playlist is now Alt+↑ / ↓.
- F1 (or ?) shows every key, with a filter box. A new About window has the version, the licences,
  the privacy summary and Copy version info. Settings has a new Help section.
- Removing or moving a song in a playlist, or clearing Up next, can be undone.
- Edit tags (Experimental): Enter moves to the next field, Ctrl+S saves, and closing with unsaved
  changes asks first.

**Opening, problems and updates**
- Only one Ironbark runs at a time: opening it again, or opening music files with it, hands them
  to the open player and brings it to the front. Several files opened together arrive in one tab.
- The Windows installer can add Ironbark to Explorer's "Open with" list (optional, unticked; it
  never becomes the default player).
- If something goes wrong, a box says so, with Copy details, Open log folder and Close.
- Settings → Help → Save diagnostics… saves a zip you can attach to a problem report, with folder
  names left out. It is never sent anywhere.
- Settings → Help → Check for updates (off by default, and only with network access allowed):
  once a day it asks digitalgreenhouse.com.au whether a newer version is out and says so in the
  status line. It never downloads or installs anything.
- The self-check (--self-check) also tests the library database, its background reader, the
  licences and one-player-at-a-time, and its windows no longer appear on screen.

**Listening**
- Volume levelling (Settings → Sound, off by default): Track, Album or Smart, with a pre-amp. It
  uses ReplayGain tags or measures the loudness itself, and never turns a song up past its peak.
  While it changes a song's level the status square turns amber: the samples aren't the file's
  own then, so it isn't bit-perfect.
- Stop after this song, and a sleep timer (15–90 minutes, or the end of the album), from a song's
  right-click menu, the Up next list or the keys. In shared mode the music fades out gently; in
  bit-perfect mode it stops at the end of a song.
- Lyrics (L): from the song's tags or a .lrc or .txt file beside it, never from the internet.
  Synced lyrics light up line by line.
- Now Playing → Check → Rip: "Check against rip log" compares each track with the checksums your
  EAC or XLD log recorded when the CD was ripped. No internet needed.

## 0.30.0 — 07/10/2026

A full code review of the player, and fixes for what it found. Two new warnings; otherwise
no new features.

**Playback safety**
- Bit-perfect mode: 32/64-bit floating-point WAV and AIFF files now play at their own level
  (they played as silence), and loud Ogg Vorbis and Opus peaks are held at full scale instead of
  crackling.
- Bit-perfect mode never switches to another output by itself: if the chosen one isn't
  connected, the player (and ABX or Clipping → Hear it) says so and waits.
- If the output is unplugged or stops responding, the player pauses at the same spot and says
  so, instead of showing Playing in silence.
- New warnings: when the device takes fewer bits per sample than the track (so it isn't
  bit-perfect), and when an MP3 with no length header can only be played in part.
- A damaged stretch of a file is skipped and reported, and the player moves on. A damaged Apple
  Lossless file, a surround file on a stereo output, or a file with no audio in it is skipped
  with a message instead of stopping the album.
- Pressing Next or Stop during a long Ogg Vorbis seek, or while a slow drive wakes up, no longer
  crashes the player.
- Changes to the list, Up next, repeat or shuffle in the last seconds of a track now apply to
  the very next track, and songs added to Up next are never lost.
- Hi-res files (96 kHz and up) get about 2 seconds of read-ahead, like CD-quality files, and very
  long tracks no longer make the player sluggish.
- Starting up in bit-perfect mode checks the device's volume control, as switching it on does.
- A song played from Up next with no tab open is shown, and Space, the play button or the media
  keys pause it.
- Dragging the seek bar while the song changes no longer seeks in the new song.
- Opening the Output list or switching bit-perfect mode no longer freezes the window.

**Library**
- Unplugging the library drive, or a network share going to sleep, no longer empties the
  Library or brings every album back as "new". A folder or file that can't be read for a moment
  keeps its place, and the Library window says when it can't see the folder.
- Tabs on a drive that isn't connected at start-up are kept, and load once it's back.
- macOS "._" files are no longer listed as tracks, the Recycle Bin and system folders aren't
  searched, and names with "²" or "①" no longer break scanning or sorting.
- Japanese (Shift-JIS) and UTF-16 CUE sheets and playlists are read correctly, and a file name
  that isn't valid Unicode no longer stops a playlist or the Library index saving.
- Playlists: CUE tracks keep their track number when edited while the drive is unplugged;
  adding to a playlist that isn't open no longer freezes the window or loses songs; renaming by
  letter case works; Enter in "Delete playlist?" now answers Cancel.
- Songs dragged onto a tab come back the same after a restart.
- Searching a very long track list or a large Library no longer freezes the window, and Enter
  or Down straight after typing act on what was typed.
- A damaged settings file no longer stops the player starting: bad values fall back to their
  defaults one by one, the last good copy is used, and the player says so. Settings, playlists
  and the Library index are written so a power cut can't leave them half-written.
- Music files are opened fewer times while being read, which speeds up indexing on network
  drives.
- The default library folder is Windows' own Music folder, even when it has been moved.

**Tags**
- Ogg Vorbis and Opus files with a large cover open and save in well under a second (they took
  tens of seconds).
- A tag that can't be read to the end is left untouched, instead of losing its later fields and
  cover when edited. Specially crafted tags can no longer exhaust memory or hang the player.
- Rare encrypted or compressed ID3 frames and older ID3 date frames are kept when editing.
  Replacing a cover keeps the file's other pictures. Dates like 2004-05 and numbered genres on
  MP3s no longer give a confusing error.
- Saving tags twice for a song that's playing no longer loses the first edit. A full disk or
  other error leaves no temporary copy beside the song, and closing the player during a save
  lets it finish the file it's on.
- The tag history stores each cover once and keeps the newest 500 saves; Undo no longer freezes
  the player.
- Linked files (symbolic or hard links) are left alone, and Ogg files with extra bytes after the
  last page can be tagged.

**Windows**
- Media controls: switching them off hides the taskbar thumbnail buttons; switching off and
  quickly on again no longer leaves Windows' media panel off; an unusual file name no longer
  leaves the panel showing the previous song.
- One error in the main window's or a tool window's refresh no longer freezes it, and the window
  always closes. Errors on background threads and during start-up now go into crash.log.
- A window is no longer restored with only a sliver showing (for example behind the taskbar).
- One touchpad swipe or smooth-scrolling wheel turn no longer jumps the volume.
- Buttons, switches, menu items and a tab's close button act only when released over them.
- The portable version started from a very deep folder tells you to move it.
- The Windows download includes the licence text of every library bundled with it.
- Settings → Network now says the CUETools DB lookup isn't encrypted.

**Tools**
- Check: floating-point files are measured properly; a cut-off MP3 is measured only as far as
  it decodes and reported as damaged; true peak includes the last samples of a track and is
  worked out 2–4 times faster; lossless 32 kHz and 22.05 kHz recordings are no longer called
  lossy; loudness of quad, 6.1 and 7.1 files follows ITU-R BS.1770-5.
- Album DR and loudness count a damaged track up to where it stops, and say which tracks were
  left out.
- Compare: exact copies of looped music or test tones are "identical"; a copy that is cut short
  or empty no longer gets a green tick; a CUE sheet track is compared and named as the track.
- ABX uses far less memory on long hi-res excerpts and its keys never play an old excerpt. ABX,
  Clipping → Hear it and Test signals stop each other, so only one plays at a time.
- Rip check: a two-disc album or box set in one folder is checked one disc at a time; MP3 copies
  or a hi-res bonus track beside the rip are left out; a stalled server is given up after 30
  seconds; a garbled answer can't show as a damaged rip; certificate problems are explained.
- Stereo scope, Spectrogram, PCM stream and Meters use less CPU, and pause their drawing while
  minimised. The PCM stream window no longer freezes after a mono track is followed by a stereo
  one.
- Test signals: Stop (and the space bar) works during "Play and measure", and a DAC plugged in
  after the player started can be used.
- Now Playing's Check button stays greyed out while a check runs, and a check that fails says so.
- Clipping → Hear it no longer plays the previous song's loop; the meter window stays smooth
  during very long recordings.

## 0.29.0 — 05/10/2026

- Windows build: `packaging\windows\build.ps1` makes an installer (`Ironbark-Setup-X.Y.Z.exe`,
  current user, no administrator prompt), a portable zip and checksums. Not code-signed yet.
- New `--self-check` option: checks the sound libraries, icons and main window work, without
  playing or saving anything; exit code 0 = passed. The build runs it on the packaged program.
- Fixed: closing the window could log a harmless error from the cover-art resize.

## 0.28.5 — 05/10/2026

- Playlists mark a track from a CUE sheet with an `#IRONBARK-CUE:<track number>` line (was
  `#DG-CUE:`). Other players still skip the line and play the whole file.

## 0.28.4 — 05/10/2026

- Code comments now explain themselves instead of pointing to notes that aren't in this
  repository. No change to the player.

## 0.28.3 — 05/10/2026

- The automatic tests can also be started by hand from GitHub's Actions tab. No change to the
  player.

## 0.28.2 — 05/10/2026

**First public version.** Everything in the [README](README.md) is in this release.
- Fixed: scrolling Settings to a section could stop one step short at 100% display scaling.

## Before 0.28.2 — private development versions

Ironbark was developed privately by Digital Greenhouse (under the working name "DG FLAC Player"
until 0.26.0). The main steps:

- **0.1–0.3** — album playback, tabs, playlists, bit-perfect mode (WASAPI exclusive) with a
  sample-for-sample proof, dropout early warning, VU / PPM / RMS / true-peak meters, the PCM stream
  viewer.
- **0.4** — loudness meters (EBU R128 / ITU-R BS.1770), gapless playback.
- **0.5** — Settings → Experimental (new features off until switched on).
- **0.6** — Now Playing → Check: is the file genuine? Bit depth, lossy or upsampled sources,
  clipping, true peak, PLR, stereo figures, album DR, damaged files.
- **0.7** — Spectrogram window and bitscope.
- **0.8** — Stereo scope, PSR, VU alignment from loudness.
- **0.9** — HDCD, MQA and pre-emphasis detection.
- **0.10** — Compare two files; ABX blind test.
- **0.11** — Rip verification (AccurateRip, CUETools DB), behind an "Allow network access" switch.
- **0.12** — Test signals: THD+N, J-test, frequency response.
- **0.13** — Media keys, Windows' media panel, drag and drop.
- **0.14** — AIFF, Apple Lossless, Ogg Vorbis / Opus and CUE sheets.
- **0.15** — Saved library index, browse views, sortable columns, Play next.
- **0.16** — Tag editing (Experimental).
- **0.17** — Spotting new music.
- **0.18–0.23** — Design system, light theme, mini player, a polish of every window, in-app
  prompts and menus, the app icon.
- **0.24** — Clipping window: see (and, experimentally, hear) inter-sample overs; clipping guard
  measured at 8× oversampling, with a "My DAC has headroom" setting.
- **0.25** — Mini player: Settings from the strip, its own Keep on top.
- **0.26** — Renamed Ironbark.
- **0.27** — MIT licence; music tags read and written by Ironbark's own code (no GPL code).
- **0.28** — The Growth Rings icon at large sizes; contribution guide and automatic tests.
