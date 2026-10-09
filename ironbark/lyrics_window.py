"""The Lyrics window (key L): the playing song's lyrics, from its own file or the files beside it
(lyrics.py — never the internet).

Synced lyrics (.lrc, ID3 SYLT) light up line by line as the song plays and scroll to keep the
line being sung in the middle; plain lyrics just scroll. With none, the window says so and where
it looked.

`LyricsView` is the panel itself (a frame: it can sit in any window); `LyricsFollower` keeps one on
the playing song; `LyricsWindow` is the window that holds one and follows the player. A wide main
window docks the same view and follower in its right pane (the view switch's "Lyrics"). Lyrics are
read on a worker; the follower only polls for them (and for the play position) from Tk's own timer.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk

from . import audiofile, controls, lyrics, theme, toolwin

REFRESH_MS = 200     # how often the window follows the play position
SIZE = (360, 520)    # px at 100%: the window's first size


class LyricsView(tk.Frame):
    """Lyrics in a read-only text panel: `show(found, where)` / `loading()`, then `follow(seconds)`
    as the song plays (synced lyrics: the line being sung lights up and is kept in view)."""

    def __init__(self, parent, bg: str = "BG"):
        super().__init__(parent, bg=theme.colour(bg))
        self.text = tk.Text(self, wrap="word", bd=0, highlightthickness=0, relief="flat", cursor="arrow",
                            bg=theme.colour(bg), fg=theme.TEXT, font=theme.font(theme.SIZE_LARGE),
                            padx=theme.px(theme.GAP_WIDE), pady=theme.px(theme.GAP_LARGE),
                            spacing1=theme.px(theme.GAP_HAIR), spacing3=theme.px(theme.GAP_HAIR),
                            takefocus=False)
        self.scrollbar = controls.AutoScrollbar(self, command=self.text.yview)
        self.scrollbar.auto_pack(side="right", fill="y")
        self.text.configure(yscrollcommand=self.scrollbar.set)
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("line", foreground=theme.TEXT)
        self.text.tag_configure("sung", foreground=theme.MUTED)
        self.text.tag_configure("current", foreground=theme.ACCENT_TEXT, font=theme.font(theme.SIZE_LARGE, "bold"))
        self.text.tag_configure("note", foreground=theme.MUTED, font=theme.font(theme.SIZE_BODY))
        self.text.tag_configure("empty", foreground=theme.TEXT, font=theme.font(theme.SIZE_HEADING, "bold"))
        self.text.configure(state="disabled")
        self.text.bind("<Configure>", lambda e: self._keep_in_view())
        self.lyrics: lyrics.Lyrics | None = None
        self.current = -1
        self._bg = bg
        theme.on_change(self._palette_changed)

    def _palette_changed(self, _palette):
        """Light / Dark switched: the text's own colours (tags aren't widget options theme.use can see)."""
        if not self.winfo_exists():
            return
        bg = theme.colour(self._bg)
        self.configure(bg=bg)
        self.text.configure(bg=bg, fg=theme.TEXT)
        self.text.tag_configure("line", foreground=theme.TEXT)
        self.text.tag_configure("sung", foreground=theme.MUTED)
        self.text.tag_configure("current", foreground=theme.ACCENT_TEXT)
        self.text.tag_configure("note", foreground=theme.MUTED)
        self.text.tag_configure("empty", foreground=theme.TEXT)

    def _write(self, pieces: list[tuple[str, str]]):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for words, tag in pieces:
            self.text.insert("end", words, tag)
        self.text.configure(state="disabled")
        self.text.yview_moveto(0)

    def loading(self):
        self.lyrics, self.current = None, -1
        self._write([("Looking for lyrics…", "note")])

    def nothing(self, where: str = ""):
        """The empty state: no lyrics for this song, and where they were looked for."""
        self.lyrics, self.current = None, -1
        self._write([("No lyrics found for this song\n", "empty")] + ([(f"\n{where}", "note")] if where else []))

    def idle(self):
        self.lyrics, self.current = None, -1
        self._write([("Nothing playing", "note")])

    def show(self, found: lyrics.Lyrics | None, where: str = ""):
        if found is None:
            self.nothing(where)
            return
        self.lyrics, self.current = found, -1
        # One text line per lyric line (follow() lights lines up by number): a line break inside
        # a piece of text (a SYLT syllable can hold one) is shown as a space.
        self._write([(" ".join(line.text.splitlines()) + ("\n" if k < len(found.lines) - 1 else ""), "line")
                     for k, line in enumerate(found.lines)])

    def follow(self, seconds: float) -> int:
        """Light up the line being sung at `seconds` (synced lyrics only). Returns its number."""
        found = self.lyrics
        if found is None or not found.synced:
            return -1
        now = found.line_at(seconds)
        if now != self.current:
            self.current = now
            self.text.tag_remove("current", "1.0", "end")
            self.text.tag_remove("sung", "1.0", "end")
            if now >= 0:
                self.text.tag_add("sung", "1.0", f"{now + 1}.0")
                self.text.tag_add("current", f"{now + 1}.0", f"{now + 1}.end")
            self._keep_in_view()
        return now

    def _keep_in_view(self):
        """The line being sung in the middle of the panel (as near as the ends allow)."""
        if self.lyrics is None or self.current < 0:
            return
        line = f"{self.current + 1}.0"
        try:
            before = self.text.count("1.0", line, "displaylines") or (0,)
            total = self.text.count("1.0", "end", "displaylines") or (1,)
        except tk.TclError:
            return
        before = before[0] if isinstance(before, tuple) else before
        total = max(1, total[0] if isinstance(total, tuple) else total)
        top = self.text.dlineinfo("@0,0")   # (the first line on show: always there)
        rows = max(1, self.text.winfo_height()) / max(1, top[3] if top else theme.px(theme.GAP_WIDE * 2))
        self.text.yview_moveto(max(0.0, (before - rows / 2 + 1) / total))


class LyricsFollower:
    """Keeps a LyricsView on the song playing: each new song's lyrics are read on a worker (`reader`),
    then the line being sung lights up as it plays. `on_song(title, line)` hears the song's title
    and a line under it — its artist and album while the lyrics are looked for, then where they came
    from ("From Song.lrc beside the song · synced"); ("Nothing playing", "") with no song. Call
    `follow(visible)` from a timer: it never waits."""

    def __init__(self, view: LyricsView, player, reader=lyrics.find, on_song=lambda title, line: None):
        self.view, self.player, self.reader, self.on_song = view, player, reader, on_song
        self._path = None          # the song whose lyrics are on show (or being read)
        # path -> its lyrics (or None) from the workers. By path, so a slow read of an earlier song
        # finishing late can't take the place of the song playing now.
        self._found: dict = {}
        view.idle()

    def follow(self, visible: bool = True):
        """One step: a new song's lyrics asked for, lyrics that have come shown, and (`visible`) the
        line being sung lit up."""
        status = self.player.status()
        track = status.track
        path = track.path if track is not None else None
        if path != self._path:
            self._path = path
            self._found.clear()
            if track is None:
                self.on_song("Nothing playing", "")
                self.view.idle()
                return
            self.on_song(track.title, track.display_artist_album or audiofile.display_name(path))
            self.view.loading()
            self._read(path)
        if path in self._found:
            found = self._found.pop(path)
            self.view.show(found, lyrics.where_looked(path))
            if found is not None:
                kind = "synced" if found.synced else "words only"
                self.on_song(track.title, f"From {found.source} · {kind}")
        if visible:
            self.view.follow(status.position)

    def _read(self, path):
        """Read the lyrics on a worker; `follow` picks them up (the worker never touches Tk)."""
        reader = self.reader

        def work():
            try:
                found = reader(path)
            except Exception:  # noqa: BLE001 — unreadable: shown as none found
                found = None
            self._found[path] = found

        threading.Thread(target=work, name="lyrics", daemon=True).start()


class LyricsWindow(toolwin.ToolWindow):
    TITLE = "Lyrics"
    SIZE = SIZE
    MIN_SIZE = (240, 200)
    GEOMETRY_KEY = "lyrics_geometry"
    REFRESH_MS = REFRESH_MS

    def __init__(self, parent: tk.Misc, player, prefs: dict, reader=lyrics.find):
        super().__init__(parent, prefs)
        self.player, self.reader = player, reader

        head = ttk.Frame(self.win, padding=theme.pad(theme.GAP_WIDE, theme.GAP_LARGE, theme.GAP_WIDE, theme.GAP_SMALL))
        head.pack(fill="x")
        self.title_label = ttk.Label(head, text="Nothing playing", style="Title.TLabel", anchor="w")
        self.title_label.pack(fill="x")
        self.source_label = ttk.Label(head, text="", style="Muted.TLabel", anchor="w")
        self.source_label.pack(fill="x")
        self.view = LyricsView(self.win)
        self.view.pack(fill="both", expand=True)
        self.follower = LyricsFollower(self.view, player, reader, self._song_shown)
        self.start_ticking()

    def _step(self):
        self.follower.follow(self.on_screen())

    def _song_shown(self, title: str, line: str):
        self.title_label.configure(text=title)
        self.source_label.configure(text=line)
