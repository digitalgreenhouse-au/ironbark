# How Ironbark is put together

A short map for anyone reading or changing the code. The user-facing description is in
[README.md](README.md); this page is about the shape of the program and the rules that keep it.

## One sentence

Ironbark is a Python desktop player with four layers: **file-format readers** at the bottom, the
**player** (engine, library, measurement) above them, the **window** on top, and the **Check tools**
to one side, which build on the player and never the other way round.

## The layers

```
ironbark/tools/      the Check tools: Now Playing, Meters, PCM stream, Spectrogram, Stereo scope,
                     Clipping, Compare, ABX, Test signals, rip verification, Tape reels
        │  import from the core only
        ▼
ironbark/ui.py       the one main window (PlayerWindow): tabs, transport, settings, lists, keys
  + the widget kit   tabbar, controls, popups, theme, shapes, keyfocus, toolwin, virtual_list,
                     the *_view and *_window modules
        │
        ▼
the player           engine (the Player) + session (one open stream: decoder, queue, callback),
                     devices, library + library_db + library_sql + indexer (the library),
                     analysis, loudness, levelling, proof (measurement the player itself needs),
                     settings, experiments, placement, logs, crash, diagnostics (the app platform),
                     platforms (which platform this is and what it provides: the one place
                     the differences are named; nothing else tests sys.platform),
                     places + netcache/ (where files live; the network playback cache)
        │
        ▼
readers              audiofile (opening any supported file), flacframes, mp3frames, m4a, cuesheet,
                     musicfiles, and tagio/ (our own tag code: FLAC, ID3, MP4, Ogg, RIFF)
```

The rules, checked by `lint-imports` on every push and by `tests/test_architecture.py` in the
quick test tier:

1. `tagio` imports nothing else in Ironbark.
2. The engine, the readers, the library database and the network cache never import the window,
   the tools, the widget kit or tkinter, directly or indirectly.
3. Nothing outside `tools/` imports a tool except the main window; the tools never import `ui`.
4. `ui.py` is imported only by the entry point (`__main__`) and the self-check.
5. Only the windows and the widget kit import tkinter.

A new feature that needs the window gets its own module that the window calls; `PlayerWindow`
gains no new methods.

## The threads

| Thread | What runs there | Rules |
|---|---|---|
| **The sound card's callback** (started by PortAudio) | `session._Session._callback`: fills each request from the block queue, measures the levels and the track peak, feeds the meters | Nothing that waits, no file, console or network access, no logging, no tkinter, as little work as possible. Written as the callback's docstring; held by `tests/test_callback_rules.py`. |
| **The decoder** (one per open stream) | Reads blocks from the file into the queue; lines up the next track for gapless playback; owns the files it reads | Only it closes a file it reads. |
| **Tk's thread** (the main thread) | Every widget, every window, the poll that updates the display | Nothing slow. Tk is single-threaded: a widget touched from another thread can abort the program. |
| **Workers** (`offthread.Background`) | Disk and database reads for the window | `self.work.run(work, then=…)`: `work()` on a worker thread, `then(value)` back on Tk's thread through **one** queue drained on Tk's timer. Progress goes the same way (`post`). |
| **Library check processes** | The whole-library quality check | Separate processes; held off while a blind test or a measurement has its window open. |

Two consequences worth knowing: the cycle collector runs on Tk's thread (`tkgc`), because a Tk
object freed by another thread's garbage collection aborts the process; and the test fixture
`tk_thread_rule` fails any test in which a thread started during the test calls into tkinter.

## Playback, briefly

A `_Session` (session.py) is one open output stream. The decoder thread reads blocks into a queue; the
callback drains it. Near the end of a track the decoder carries straight on into the next one when
the sample rate and channel count match, on the same stream, so there is no gap; a track that
can't join gets a new stream and the player says why. Two output modes: shared (float32 through
the system mixer) and exclusive (WASAPI on Windows: the file's own rate, left-justified int32,
dithering off, so at full volume the device receives the file's samples untouched). If the device
refuses exclusive mode the player falls back to shared on the same device, never to a different
one. The `proof` module records what was sent so the Now Playing window can show it.

## Where things live on disk

| What | Where (Windows) |
|---|---|
| Settings (autosaved) | `%APPDATA%\Ironbark\settings.json` |
| Playlists | `%APPDATA%\Ironbark\Playlists\*.m3u8` |
| App log, crash log | `%APPDATA%\Ironbark\app.log`, `crash.log` |
| Library database (plays and favourites live only here) | `%LOCALAPPDATA%\Ironbark\library.db` |
| Network playback cache | the cache folder beside the database |

Ironbark never uses the internet. *Local network access* (off by default) gates reading music
from shares and the cache.

## Adding a Check tool

1. The measurement in its own module under `tools/` (pure code, testable without a window).
2. A window class on `toolwin.ToolWindow` (title, size, remembered place, Esc, a tick loop that
   survives an error, one sound at a time).
3. One line in `tools/registry.py` (its key action, label, window class, what it needs, whether
   an Experimental switch gates it, whether the library check should wait while it is open) and one
   `Action` in `keys.py` for its key. The main window opens it through the table.

## Versions, branches, checks

- Semantic versioning; `__version__` in `ironbark/__init__.py` is the one place it is set.
- `main` holds released versions, each tagged; `dev` is where work happens; features branch from `dev`.
- Every push runs ruff, the import rules and the quick tier (every test that makes no window) on
  Windows and Ubuntu; pull requests and releases run the whole suite. `uv run pre-commit install`
  runs the quick checks before each commit.
- Type checking (mypy) covers a listed set of modules in `pyproject.toml`; a module joins the
  list once it is clean.
