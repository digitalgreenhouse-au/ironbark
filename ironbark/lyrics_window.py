"""The Lyrics window (key L): the playing song's lyrics, from its own file or the files beside it
(lyrics.py — never the internet).

Synced lyrics (.lrc, ID3 SYLT) light up line by line as the song plays and scroll to keep the
line being sung in the middle; plain lyrics just scroll. With none, the window says so and where
it looked.

`LyricsView` is the panel itself (a frame: it can sit in any window); `LyricsWindow` is the
window that holds one and follows the player. Lyrics are read on a worker; the window only polls
for them (and for the play position) from Tk's own timer.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk

from . import audiofile, controls, lyrics, placement, theme, toolwin

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
        self._write([(line.text + ("\n" if k < len(found.lines) - 1 else ""), "line")
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


class LyricsWindow:
    def __init__(self, parent: tk.Misc, player, prefs: dict, reader=lyrics.find):
        self.player, self.prefs, self.reader = player, prefs, reader
        self.win = tk.Toplevel(parent, bg=theme.BG)
        self.win.title("Lyrics")
        self.win.geometry(f"{theme.px(SIZE[0])}x{theme.px(SIZE[1])}")
        self.win.minsize(theme.px(240), theme.px(200))
        placement.restore(self.win, prefs.get("lyrics_geometry", ""))
        theme.dark_title_bar(self.win)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<Escape>", lambda e: self.close())

        head = ttk.Frame(self.win, padding=theme.pad(theme.GAP_WIDE, theme.GAP_LARGE, theme.GAP_WIDE, theme.GAP_SMALL))
        head.pack(fill="x")
        self.title_label = ttk.Label(head, text="Nothing playing", style="Title.TLabel", anchor="w")
        self.title_label.pack(fill="x")
        self.source_label = ttk.Label(head, text="", style="Muted.TLabel", anchor="w")
        self.source_label.pack(fill="x")
        self.view = LyricsView(self.win)
        self.view.pack(fill="both", expand=True)
        self.view.idle()

        self._path = None          # the song whose lyrics are on show (or being read)
        self._loaded: tuple | None = None   # (path, lyrics) from the worker, picked up by _tick
        self._keep_going = toolwin.KeepGoing(self.win)
        self.win.bind("<Configure>", self._moved, add="+")
        self._job = self.win.after(REFRESH_MS, self._tick)

    def _tick(self):
        with self._keep_going:
            self._follow()
        self._job = self.win.after(REFRESH_MS, self._tick)

    def _follow(self):
        status = self.player.status()
        track = status.track
        path = track.path if track is not None else None
        if path != self._path:
            self._path = path
            self._loaded = None
            if track is None:
                self.title_label.configure(text="Nothing playing")
                self.source_label.configure(text="")
                self.view.idle()
                return
            self.title_label.configure(text=track.title)
            self.source_label.configure(text=track.display_artist_album or audiofile.display_name(path))
            self.view.loading()
            self._read(path)
        loaded = self._loaded
        if loaded is not None and loaded[0] == path:
            self._loaded = None
            found = loaded[1]
            self.view.show(found, lyrics.where_looked(path))
            if found is not None:
                kind = "synced" if found.synced else "words only"
                self.source_label.configure(text=f"From {found.source} · {kind}")
        if toolwin.on_screen(self.win):
            self.view.follow(status.position)

    def _read(self, path):
        """Read the lyrics on a worker; `_follow` picks them up (the worker never touches Tk)."""
        reader = self.reader

        def work():
            try:
                found = reader(path)
            except Exception:  # noqa: BLE001 — unreadable: shown as none found
                found = None
            self._loaded = (path, found)

        threading.Thread(target=work, name="lyrics", daemon=True).start()

    def _moved(self, event):
        if event.widget is self.win:
            geometry = placement.current(self.win)
            if geometry:
                self.prefs["lyrics_geometry"] = geometry

    def lift(self):
        self.win.deiconify()
        self.win.lift()

    def close(self):
        if self._job:
            self.win.after_cancel(self._job)
            self._job = None
        self.win.destroy()
