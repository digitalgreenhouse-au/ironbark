# Changelog

Ironbark follows [semantic versioning](https://semver.org): MAJOR.MINOR.PATCH — a PATCH fixes
something, a MINOR adds a feature; MAJOR stays 0 until version 1.0. The version is shown at the
bottom of Settings. Dates are dd/mm/yyyy. Newest first.

## 0.49.0 — 14/10/2026

- **DTS music CDs and HDCD discs stay bit-perfect.** Both carry something hidden in their samples —
  a DTS surround stream for a receiver to decode, or HDCD's control codes in the lowest bit — that any
  change of level destroys (a DTS receiver then plays loud noise). The clipping guard and volume
  levelling used to turn such tracks down like any other: a DTS stream looks like noise near full
  scale, and an HDCD master can peak above full scale between samples. Ironbark now recognises both
  from the first seconds of each track and plays them at their own level; the line under the format
  says "clipping guard and levelling kept off", or, in amber, that the stream needs exclusive mode or
  the volume at 100%.
- **Check** reports a DTS stream (and whether it is a DTS music CD's 14-bit packing) next to HDCD,
  MQA and pre-emphasis.

## 0.48.0 — 13/10/2026

- **Ironbark for the web** (`web/`): Ironbark in a web browser, for the music on your own computer.
  Open a folder or some songs, or drop them on the page; nothing is uploaded, and the page can't use
  the internet at all (its Content Security Policy forbids it). It has the desktop player's look and
  side-player shape: albums with covers, search, shuffle and repeat, the next song loaded before this
  one ends, a level meter and a spectrum, the same keyboard shortcuts, media keys, Light and Dark
  themes, and Details with **Check this file**, which decodes a FLAC file and compares it with the
  MD5 its encoder stored. It says plainly what a browser can't do: play bit-perfect (it names the
  rate the browser plays at, and any resampling). It plays FLAC, MP3, Ogg Vorbis, Opus, WAV and AAC
  where the browser does; it remembers the folder in Chrome and Edge and installs there as an app
  that works offline. Plain HTML, CSS and JavaScript with no build step and no libraries; see
  `web/README.md`.

## 0.47.0 — 12/10/2026

- **Settings → Volume on my amplifier**, kept for each output: for an output that feeds an
  amplifier or receiver, whose own knob sets the level. In Bit-perfect such an output often has no
  volume control of its own, and the player warned about it in amber and turned its own volume down
  to protect your ears. With this switched on for that output, the line is green ("bit-perfect ✓ ·
  volume on your amplifier") and the volume is left alone. Every other output with no volume of its
  own still gets the warning.

## 0.46.1 — 12/10/2026

- **24-bit tracks reach a USB device that takes 24-bit samples only in packed form.** Some
  USB Audio Class 1 devices (those built on the CMedia CM6542, for one) refuse 32-bit
  samples in bit-perfect mode; the sound library then fell back to 16-bit, so a 24-bit track lost
  its lowest 8 bits (the player said so, rightly: "not bit-perfect"). Bit-perfect mode now also
  asks such a device for packed 24-bit samples (three bytes each) and keeps whichever format
  carries more bits. 16- and 24-bit tracks are bit-perfect on these devices again, proven sample
  by sample as before. Devices that take 32-bit samples are opened exactly as before.
- The Details window's Output line names the bits the device takes in bit-perfect mode
  ("96 kHz 24-bit exclusive").

## 0.46.0 — 10/10/2026

- **The Settings page is built by its own module.** `settings_view.py` now makes the controls
  (the two pages, the Network playback box, the Help buttons, the output list) and holds the small
  widget helpers the page is made of; the main window keeps the handlers. The first of several
  slices that move construction out of the main window, one per version. Nothing about how the
  player behaves changed.

## 0.45.0 — 10/10/2026

- **The platform's differences are named in one place**, `platforms.py`: which platform this is,
  and what it provides (the custom title bar, media controls, exclusive output, the device's own
  volume, loopback recording, the single-instance pipe, the bin's name), each flag naming the
  module that implements it. Every platform check in the program goes through it now (62 of them,
  across 28 modules, used to test `sys.platform` on their own). Groundwork for the Mac build: a
  port reads one file. Nothing about how the player behaves on Windows changed.

## 0.44.0 — 10/10/2026

- **Two big modules split along the seams they already had**, with nothing moved but the lines:
  `session.py` is now the open output stream (the decoder thread, the block queue and the sound
  card's callback, with the constants and small classes only it uses); `engine.py` keeps the
  `Player` the rest of the program talks to and re-exports every name, so imports elsewhere are
  unchanged. `library_sql.py` holds the library database's schema (the tables, their upgrades, the
  SQL the queries are built from); `library_import.py` the one-off move from the old JSON index.
  `library_db.py` keeps the database itself. Nothing about how the player behaves changed.

## 0.43.0 — 10/10/2026

- **[ARCHITECTURE.md](ARCHITECTURE.md):** a map of the code — the four layers, the threads and
  their rules, where files live on disk, how to add a Check tool.
- **One table for the Check tools** (`tools/registry.py`): what each key opens, what it's called,
  what the window needs, whether an Experimental switch gates it and whether the library check
  waits while it's open. The main window opens every tool through it, so adding a tool is one line
  there and one key in `keys.py`. The named openers (`show_meters` and the rest) remain.
- **Type checking** (mypy) joins the checks on every push, gradually: sixteen modules of the tag
  code and the network cache are clean and listed in `pyproject.toml`; the rest follow module by
  module. Twenty-one small narrowings on the way (an `Optional` checked before use, a class
  attribute declared, an error raised with its cause). Nothing about how the player behaves changed.

## 0.42.0 — 10/10/2026

- **Fixed:** a Library tab with a search query failed at the search's folding step (a name that
  was never defined). Found by the new linter.
- **The audio thread's rules are written down** in the engine: nothing that waits, no file or
  network access, no logging, nothing that touches the window. A new test,
  `tests/test_callback_rules.py`, drives the sound-card callback with those calls forbidden.
- **For contributors:** the repo now runs **ruff** (the linter) and **import-linter** (which
  module may import which: the engine, the file readers, the library database and the network
  cache know nothing of the window or the tools; the Check tools import from the core, never
  back; the tag code is self-contained). `uv run pre-commit install` runs the quick checks before
  each commit; `tests/test_architecture.py` checks the same import rules in the quick tier.
  GitHub's tests now run in three tiers: the lint and import checks plus the quick tier on every
  push (Windows, and Ubuntu as an advisory run for now), the full suite on pull requests, on
  pushes to `main` and `dev`, and nightly.
- Mechanical tidy-ups from the linter: import order, unused imports, modern type spellings
  (`X | None`), two lambdas made into functions, one `raise` that now keeps its cause. Nothing
  about how the player behaves changed.

## 0.41.1 — 09/10/2026

- **Library folders on the network stay listed.** With *Local network access* off, a network
  folder in Settings → Library is greyed rather than read. *Add folder…* used to build the new list
  from the folders being read, so the greyed one was dropped for the one added (a second folder
  seemed to replace it); *Remove* could drop it the same way. Both now keep every listed folder.
- **A whole-library search says what it leaves out.** With the Whole library switch on, the first
  search of a session notes, in the status line, any library folder on the network that isn't
  searched while Local network access is off — the switch is the fix, in Settings → Library.
- **Mini strip:** the buttons' glyphs are weighed against each other — previous / next a little
  smaller beside the strip's play button, and the gear, full player and close at the strip's icon
  size — so no button looks drawn with a thicker pen than its neighbours.

## 0.41.0 — 09/10/2026

**The Library's views live in the Library tab.** What used to be a separate Library window — Albums,
Artists, Genres, Years, Composers, Recently added, Most played, Recently played, Favourites, Needs a
look — now shows inside the Library tab in place of its song list: Ctrl+L, the **Songs ▾** button
beside the tab's search box, or the + menu's *Browse library…*. The views sit in one drop-down (the
eleven words never fitted a narrow window); Esc, Ctrl+L again or opening an album puts the song list
back; the seek bar and transport stay, so the music can still be controlled. Narrower than about
480 px the lists show fewer columns ("Artist – Album" and Year; Title and Time).

- **Ctrl+K** (and Ctrl+Shift+F) now goes to the Library tab's search box with *Whole library* on —
  the search panel that used to open over the window is gone, as that box does the same job.
- **Add songs from the Library…** on a playlist tab's right-click menu: the Library tab opens with a
  bar at its foot; pick songs (click, Ctrl+click, Shift+click, Ctrl+A, or an album's header), **Add**
  puts them in the playlist and comes back; Cancel or Esc comes back with nothing added.
- The views' ⋯ menu holds Rescan, *Open all albums in a tab* and *Add folder…*; "Add to tab" adds to
  the tab you were on before the Library tab.
- The Library window's saved size and place are no longer used.

## 0.40.0 — 09/10/2026

**The Library is a tab.** The main Library tab holds every song of every library folder, grouped by
album. It's always there: it can't be closed, but it can be dragged along the strip like any other
tab, and it sits on a plate of its own, a shade apart from the rest. A song plays in place; the small
mark at the right of each row (or Ctrl+Shift+O) opens that song's album in a new tab, the music
untouched.

- **Rename any tab**: right-click → Rename tab… (or F2). The name is yours; the now-playing head
  still shows the album's real title, and "Use the album's name" puts it back. A playlist tab renames
  its file, as before.
- **The search box searches the whole library** — a small switch beside it, on by default. Typing
  lists this tab's matches first, then "From your library": a song outside this tab plays from the
  Library tab (selected and scrolled to it); in a playlist tab every library row carries a "+", and
  one click (or Enter, or Ctrl+Shift+A) adds the song to the playlist you're in, with Undo. Switch it
  off and the box filters the tab only, as before.
- Not yet: the Library window's views (Artists, Genres, Years, …) inside the tab, the bulk "Add
  songs…" picking mode, and Ctrl+K folding into the tab. Those are the next version.

## 0.39.0 — 09/10/2026

**Settings fills the window.** Press the gear (or Ctrl+,) and Settings takes the place of the cover,
transport and track list under the tab strip, the full width of the player, with a small **Done**
button at the bottom. Done, Esc, the gear again or a click on a tab bring the player back; the music
isn't touched.

- Two pages. **Simple**, what you see first: Output, Bit-perfect, Volume · Theme, Album art, Keep on
  top · the library folders and Local network access · Keyboard shortcuts, About, the version.
  **Advanced settings…**, a button at the bottom: Clipping guard and headroom, Volume levelling and
  pre-amp · Media keys, Home · Network playback · Experimental · Help (Save diagnostics…, Open log
  folder, Detailed logging). Back returns to the simple page.
- From the mini player the gear still grows the strip downwards to hold Settings, as before.

## 0.38.0 — 09/10/2026

**Ironbark never uses the internet, and nothing runs in the background.**

- The update check and the online rip look-ups (AccurateRip, CUETools DB) are gone, and with them
  Settings → Network. The rip-log check stays: it proves a disc against the checksums the ripper's
  own EAC / XLD log recorded, with nothing sent anywhere.
- **Local network access** (Settings → Library, off by default) now covers music on your home
  network too: with it off, a library folder on a NAS, a shared folder or a mapped drive is left out
  (listed greyed in Settings), and opening or dropping one says so. On, everything is as before.
- Nothing polls: Theme is Dark or Light (a saved "Follow Windows" becomes one or the other once);
  the library is looked at once at start-up and when you press Rescan, not every few minutes (the
  "Watch for new music" switch is gone; new-album dots and the "New music" line still work); the
  whole-library quality check runs only while you leave it on, and is paused at the next start
  until you press Resume.

## 0.37.0 — 09/10/2026

**Three features are now standard**, no switch needed: **tag editing** (right-click a song → Edit
tags…, or press E; Tag history undoes), **Hear it** in the Clipping window (A: an ordinary converter,
B: clean, D: just the difference), and the **network playback cache** for music on a NAS or a shared
folder. Tape reels stays Experimental.

- New in Settings → Library: **Local network access** (off by default). Switch it on and the cache
  runs: songs on network drives play from a copy on this computer and keep playing if the network
  drops; Settings → Network playback shows its choices. Off, nothing of the cache runs. Never the
  internet.
- An older settings file that had the three experiments switched off is simply ignored.

## 0.36.0 — 09/10/2026

**Under the bonnet.** Nothing new to see; the code is in better shape for what comes next.

- The side player's unreachable wide layout (two panes, full screen, the docked Up next and Lyrics
  views, the whole-library search beside the box) is gone from the code, not just locked away.
- The Check tools (Now Playing, Meters, PCM stream, Spectrogram, Stereo scope, Clipping, Compare, ABX,
  Test signals, rip verification, Tape reels) live in their own package, `ironbark/tools/`, and share
  one window base. The player itself is smaller and never depends on them.
- Everything the player reads off its own thread goes through one worker with one way back, instead
  of twenty-one threads each polling for its answer.
- One place goes online (`network.py`), behind the one switch.
- Tests: `uv run pytest -q -m "not ui"` runs the window-free tests in a few minutes.

## 0.35.0 — 08/10/2026

**Tape reels** (Experimental — switch it on in Settings → Experimental, then press W). A reel-to-reel
deck playing along with your music.

- The album playing is the tape: the left reel empties onto the right one as the songs play, and a
  long album fills more than one reel.
- Each reel turns at its true speed. The tape runs past the heads at a steady 7½ or 15 inches a
  second, so a reel's rpm is that speed divided by its tape pack's circumference: the full reel
  turns slowly, the emptier one faster, changing all the way through. Each reel's rpm is shown.
- 7-inch or 10½-inch reels. Pausing brakes the reels to a stop; skipping ahead spools the tape there.
- F11 or a double-click fills the screen; Esc comes back. The player itself stays a side player.

## 0.34.0 — 08/10/2026

**A side player, by design.** Ironbark is a tall strip that sits beside whatever you're working on,
and now it stays that way.

- The window can be made a little narrower or wider (300–560 px at 100% display scaling; the usual
  width is about 420) and as tall as you like. It no longer turns into a two-pane wide window, and it
  can't be maximised: the maximise button, F11 full screen and "opens maximised" are gone. Windows+Up
  and Snap's maximise do nothing to it, and snapping it to a side keeps it at its widest.
- Double-clicking the tab strip brings the window to its home spot (Settings → Window → Use current);
  until a home is set, it says how to set one.
- A window left wide by an earlier version opens at the new widest width, in the usual layout.

## 0.33.1 — 08/10/2026

Release-review fixes: the whole player was reviewed before its first release, and what the review found
has been fixed across playback, the library, the windows, the network cache, packaging and wording.

- **Your ears:** bit-perfect on "System default" plays only on the device whose volume was checked; if
  Windows switches to a new default, playback stops, the check runs again, and play carries on there.
  On a device with no volume control of its own, End, Page Up and a click far up the volume slider now
  ask first, as Ctrl+Shift+↑ does, and in that question Enter or Esc keep the volume where it is.
- **Playing:** adding songs no longer loses your place while paused by the sleep timer, a disconnected
  device, ABX or Test signals; the sleep timer no longer fades a bit-perfect song still downloading.
- **Up next:** "Add to queue" is now "Add to Up next" everywhere; a very long Up next no longer slows a
  wide window at every song change; the docked list keeps your selection on the songs you chose.
- **The Library:** another drive given your music drive's letter no longer empties it; a music drive
  back under a new letter, and renamed or moved album folders, keep their plays, favourites and quality
  checks; favourites or plays made while the Library is being read are no longer lost.
- **Windows and keyboard:** full screen with Keep on top no longer hides the other windows; mini mode
  and in-app boxes put the keyboard back where it was; About scrolls when the screen is short; "/"
  opens search on keyboards where it needs Shift.
- **Network playback (Experimental):** a file on the network drive is no longer held open after its
  download, a deleted cached copy is downloaded again, and drives come back after a network hiccup; the
  cache size is in the same gigabytes Settings shows.
- **Installing:** uninstalling removes the network playback cache and asks before removing your
  settings and library database (your music is never touched; upgrades keep everything); the program
  always uses its own PortAudio; opening a song while Ironbark is closing waits and then plays it.
- **Privacy:** Save diagnostics also hides mapped network drives' server names, and its wording now
  says plainly what is replaced by stand-ins and that song and album names in the logs can still show.
- **Wording:** one name for each thing ("network playback cache", "Format", "Now Playing"), "1 song"
  not "1 tracks", thousands separators, and messages that name the real controls; the search panel's
  status line wraps instead of being cut off in a narrow window.

## 0.33.0 — 08/10/2026

Very big tabs that open in a moment, several songs selected at once, Up next and the lyrics beside
the controls in a wide window, and finishing touches throughout.

**Big lists**
- The track list opens, scrolls and searches a tab of a whole library (200,000 songs) as quickly as
  an album. Library → "Open all albums in a tab" opens it in about a second and a half and uses
  about 5 MB instead of hundreds. A row not read yet shows "…" for a moment.
- The Library's album list is searched and sorted without freezing the window, however big the
  library.
- Searching inside a tab of a few albums from the Library is instant, even in a very big library.
- A Library song view opened while the library was first being read fills in when that read ends.

**Selecting several songs**
- In the track list, the Library's song views, the library search's results and (in a wide window)
  Up next: Shift+click, Ctrl+click and Shift+↑ / ↓ select several songs. Ctrl+A selects every song
  (not in the search's results).
- Right-click a selection: Play next, Add to queue, "Add these N songs to playlist", favourites,
  "Edit tags of these N songs…" and, in a playlist, "Remove these N songs from playlist". Delete
  removes every selected song from a playlist, dragging carries them all, and one Ctrl+Z puts them
  all back. A very big selection is read in the background ("Reading 200,000 songs…") instead of
  freezing the window.
- Shift across an album heading acts only on the songs lit, not the rest of that album.
- "/" goes to the search box (in the main window and the Library).

**Plays columns**
- An optional Plays column in the track list (right-click the headings → Columns → Plays), and
  optional Plays and Last played columns in the Library's song views. They show the new count
  after a play.

**Wide windows**
- A Tracks · Up next · Lyrics switch above the list: Up next and the lyrics show in the list's
  place, and the choice is kept for the session. Docked Up next: double-click or Enter plays a song
  now, Delete takes the selected songs out (Ctrl+Z puts them back), Clear, "Stop after the current
  song" and the Sleep timer.
- "This tab | Whole library" beside the search box: Whole library shows every matching song, album
  and artist in the list's place as you type (Enter plays, Shift+Enter plays next, Ctrl+Enter adds
  to Up next, Alt+Enter opens the album). Opening an album or folder goes back to This tab.
- The keyboard always goes to what's on show, so keys never act on a hidden list.

**Finishing touches**
- A long status message takes a second line drawn above the status line, so nothing moves; hover
  it for the whole message.
- Question boxes: Tab and Shift+Tab move between the buttons with a green ring; Enter presses the
  ringed one, Esc cancels. Removing a library folder in Settings now asks first (Keep is the
  default).
- The mini strip's tooltips show their keys, e.g. "Next (N)".
- After an unexpected close, the next start says "Ironbark closed unexpectedly last time", with a
  "Save diagnostics…" link.
- Saved diagnostics also leave out tab, playlist, search, sound-output and network server names.
- Clearer privacy wording for Check for updates, rip verification and Allow network access
  (Settings, README and About → Privacy).
- The menu key (or Shift+F10) opens the selected song's menu even after the list has scrolled away
  from it. Dragging a song to just below the last row on show in a long playlist puts it there.

**Network playback cache (Experimental)**
- A "Cached" column in the track list for a tab with songs on a network drive: ● in the cache,
  ◐ 72% part downloaded, ○ waiting, ! couldn't download.
- A song whose copy on this computer goes missing plays on to a quiet moment, then carries on from
  the same spot on a fresh copy, also when that happens while the network is down.
- A copy that fails its check twice is skipped for now (and says so). VU Auto on a network album
  waits for the songs to download instead of saying it couldn't measure. Now Playing's live bitrate
  and an MP3's "cut short" note appear as soon as the copy completes. After Stop, the song left
  behind stops downloading first.

## 0.32.0 — 08/10/2026

A new look, a layout for big windows, song search, new Library views, a whole-library quality
check, and (Experimental) a cache for music on network drives.

**New look**
- Browser-style tabs: the chosen tab has rounded top corners that flow into the page, the others
  are quiet names that light up on a rounded plate under the pointer. The playing tab shows a
  small green level mark; a tab's × appears after a moment and the name shortens to make room.
- Every button with words is the same rounded pill; text boxes and drop-downs are rounded with a
  thin rim and a neat chevron, and a green ring shows which one you're typing in. The search box
  shows Ctrl+F while it's empty and a round × to clear it.
- Lists have no frame: the selected row is a rounded green plate, headings are small grey words
  over a thin line, album and playing rows are semibold, and the slim scrollbar sits in the
  window's margin.
- Menus have taller rows with a rounded highlight, ticks that line up and drawn chevrons for
  submenus. Tooltips have rounded corners on Windows 11.
- The now-playing title is larger and semibold; previous and next are filled shapes that match
  the play button; shuffle and repeat turn green with a small dot when on; the status square is
  now a round dot; the lock and ⓘ are one size; the seek bar runs exactly edge to edge.
- Prompts' buttons no longer touch and a warning's title is no longer cut off; Settings
  descriptions wrap at the full width; the track list no longer jumps when the status line
  shows a message.

**Keyboard**
- Tab reaches every button, switch, choice and slider, with a green ring showing where you are;
  Enter or Space presses, the arrow keys move sliders and choices. Settings can be worked
  entirely from the keyboard: Ctrl+, opens it, Tab goes round it, Esc closes it.

**Big windows and full screen**
- Make the window wider than about 880 px (or maximise it) and it splits in two: the cover, title
  and controls on the left, the track list the full height on the right. Under 780 px it is
  exactly as before. Bigger windows get bigger type and roomier rows.
- Covers are never enlarged past their own pixels in a wide window: a small cover is shown 1:1 on
  a soft card (hover it to see its size).
- A wide window shows Artist, Album and Format only when they differ between songs, a count over
  the list ("Album · 13 songs · 51:57") and a caption over the title (NOW PLAYING, ALBUM, …).
- F11 is full screen (F11 again, Esc, double-clicking the tab strip or Z leaves it). Wide windows
  get a maximise button, and a window left maximised opens maximised.

**Song search**
- Ctrl+K (or Ctrl+Shift+F), from any Ironbark window, opens a search over the whole library: songs,
  albums and artists as you type, ignoring accents and case. Enter plays the song inside its album,
  Shift+Enter plays it next, Ctrl+Enter adds it to Up next, Alt+Enter opens the album; right-click
  for the rest, or drag a result onto a tab.
- A track-list search that finds nothing offers "Search the whole library".

**Library views and favourites**
- New views on a second row: Songs (every song, sorted by any column), Most played, Recently
  played and Favourites. Long lists stay quick however big the library is.
- Favourites: Add to / Remove from favourites in the song menus; a ♥ shows in the track list, the
  Library and the search.
- The Album column shows the album's tag (not the folder name); long names end with "…"; Year,
  Quality, Tracks and Added stay visible however narrow the window. Typing letters jumps to a name.
- "Open all N albums in a tab" asks first above 10,000 songs.

**Whole-library quality check**
- Library → Needs a look: Check the whole library… says how long it will take, then gives every
  song Now Playing's Check in the background, at low priority. Damaged files, MD5 mismatches,
  lossy-source and upsampling clues, padded bit depth, clipping and misnamed files are listed,
  worst first (HDCD, MQA and pre-emphasis greyed, for information). Pause, Resume and Stop; it
  carries on after a restart and checks new music as it arrives. It waits while ABX or Test
  signals is open.
- Now Playing → Check shows a checked song's result at once, and the clipping guard and volume
  levelling use its stored true peak.

**Network playback cache (Experimental, off until you switch it on)**
- Songs on a network drive play from a copy fetched to this computer: "Preparing track… 34%"
  first, then short network hiccups aren't heard; a longer outage pauses at a quiet moment and
  the song carries on by itself from exactly the same sample.
- Seeking ahead in a FLAC, WAV or AIFF fetches from that spot first; the next songs download
  ahead; Up next shows how far each has got (● ◐ ○ !) and the seek bar shows what's downloaded.
- Settings → Network playback: buffering, initial buffer, pre-download, how long copies are kept,
  the cache's size, a line on how the network is doing, Open folder and Empty the cache. Each
  finished copy is checked against the original. Now Playing gets a Source section.

**Fixes and smaller changes**
- A truncated Apple Lossless (.m4a) file stops with a "damaged here" message instead of playing
  noise to its end.
- Web addresses in playlists are listed ("not supported yet") and kept exactly when the playlist
  is saved; they used to be lost.
- Deleting a playlist sends it to the Recycle Bin (on a drive with no Recycle Bin it asks first).
  A playlist saved by an older player in an older text format asks before it is rewritten as
  UTF-8, and "Added …" is no longer shown when a save fails.
- Tag history loads without holding up its window and has Clear history….
- Opening a playlist whose songs are on a switched-off network drive checks that drive once, not
  once per song.
- Skipping a song stops its clipping-guard and levelling measurement instead of finishing it.
- Closing Settings or a menu by clicking the list or the search box leaves the keyboard where you
  clicked.
- A favourite given just before quitting is kept.
- Long lists paint much faster in any window (a full-width list used to take seconds).
- The Library tab's count is right: "Library · 214 songs · 13 albums · 13:52:11", not the first
  album's.

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
