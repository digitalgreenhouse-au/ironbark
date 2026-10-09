"""Help: the Keyboard shortcuts sheet (F1 or ?) and the About Ironbark window, both reachable
from Settings → Help too.

The sheet is made from the one keyboard table (keys.py), grouped as it is (Playback, Tabs & lists,
Windows & tools, Library & search, Editing), with a filter box: type a key or a word. The arrow
keys move through it, Home / End jump to its ends, Esc clears the filter and then closes it.

About: the version, the copyright and Ironbark's own MIT licence, every bundled component's licence
(read from the licenses folder the installer puts next to Ironbark.exe; from the source tree when
running from source), the privacy summary, the website (opened in the web browser only when
clicked) and "Copy version info" — the version, Windows, Python, Tk, libsndfile, PortAudio and the
experiments switched on, for a bug report.
"""

from __future__ import annotations

import platform
import sys
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable

from . import APP_NAME, __version__, controls, experiments, keys, placement, theme
from .playlist_view import fit_columns
from .searchfold import fold
from .virtual_list import CELL_ROOM, ELLIPSIS

WEBSITE = "https://www.digitalgreenhouse.com.au"
THIRD_PARTY = "THIRD_PARTY_LICENSES.md"
FIT_MARGIN = 48       # px at 100%: kept free below a window that first opens as tall as the screen allows
LICENCE_LINES = 6     # About: the licence text is never shorter than this (the page scrolls instead)

# Ironbark's own licence (the LICENSE file; a test keeps the two the same). Built in, so it shows
# however the program was installed.
MIT_LICENCE = """MIT License

Copyright (c) 2026 Digital Greenhouse

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

# The README's Privacy section, word for word (a test keeps the two the same).
PRIVACY = ("Ironbark doesn't collect anything about you or your music. Nothing uses the internet until you "
           "switch on Settings → Network → Allow network access. Then only two features can:\n"
           "\n"
           "• Rip verification: when you press Verify rip online, it sends only the disc's IDs and track "
           "layout to AccurateRip and CUETools DB. As with any request, those websites see your computer's "
           "internet address and the Ironbark version. The CUETools DB look-up isn't encrypted, so others on "
           "your network can see which disc is looked up.\n"
           "• Check for updates (Settings → Help, its own switch, also off by default): once a day it asks "
           "digitalgreenhouse.com.au for one small file saying what the latest version is. The request names "
           "the Ironbark version, and the website sees your computer's internet address, as any website you "
           "visit does; nothing else goes with it. Nothing is downloaded or installed: a newer version is only "
           "mentioned in the status line.\n"
           "\n"
           "Lyrics, the rip-log check and the library's song search all work offline, from files on your "
           "computer. Music on network drives (\\\\server\\share, or a mapped drive like Z:) and OneDrive plays "
           "without network access: Windows fetches those files, as it does for any program. The network "
           "playback cache (Experimental) only talks to the network drives your music is already on; it never "
           "uses the internet.\n"
           "\n"
           "Settings, playlists and the logs are kept on your computer (on Windows, in %APPDATA%\\Ironbark); "
           "the library database (with your play counts and favourites) and the network playback cache's "
           "copies are beside them (on Windows, in %LOCALAPPDATA%\\Ironbark). If something goes wrong, "
           "Settings → Help → Save diagnostics… saves a "
           "zip on your computer that you can choose to attach to a problem report (versions, sound outputs,"
           " logs and settings). In it, the folders in your settings, your home folder and the names of tabs, "
           "playlists and sound outputs are replaced by stand-ins such as <folder 1>. Song, album and artist "
           "names the logs mention, and folders outside those, can still show: look through it before you "
           "send it. It is never sent anywhere.")


def copyright_line() -> str:
    """"© 2026 Digital Greenhouse", from the licence's own copyright line."""
    line = next(text for text in MIT_LICENCE.splitlines() if text.startswith("Copyright (c)"))
    return "©" + line.removeprefix("Copyright (c)")


# -- the licence files ----------------------------------------------------------------------------
def licence_folder() -> Path | None:
    """Where the bundled components' licences are. The installed program (PyInstaller sets
    sys.frozen) has them in the licenses folder beside Ironbark.exe, put there by the build; run
    from source there's no such folder (None) and the source tree's own files are used."""
    if not getattr(sys, "frozen", False):
        return None
    folder = Path(sys.executable).resolve().parent / "licenses"
    return folder if folder.is_dir() else None


def _source_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _title(relative: Path) -> str:
    if relative.name == THIRD_PARTY:
        return "Third-party licences (summary)"
    parts = [*relative.parts[:-1], relative.stem if relative.suffix in (".txt", ".md") else relative.name]
    return " · ".join(parts)


def licence_documents() -> list[tuple[str, Path]]:
    """(name, file) for every bundled component's licence that can be found — the summary
    (THIRD_PARTY_LICENSES.md) first. Ironbark's own licence isn't listed: it's built in. An empty
    list when none can be found (a copy put together by hand, say)."""
    folder = licence_folder()
    if folder is not None:   # (Python packages' licences sit in a folder each: "numpy · LICENSE")
        found = [(p.relative_to(folder), p) for p in folder.rglob("*")
                 if p.is_file() and p.name != "Ironbark-LICENSE.txt"]
    elif getattr(sys, "frozen", False):
        found = []
    else:
        source = _source_root()
        files = [source / THIRD_PARTY, *(source / "packaging" / "licenses").glob("*.txt")]
        found = [(Path(p.name), p) for p in files if p.is_file()]
    found.sort(key=lambda item: (item[0].name != THIRD_PARTY, str(item[0]).lower()))
    return [(_title(relative), path) for relative, path in found]


def read_document(path: Path | None) -> str:
    if path is None:
        return MIT_LICENCE
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return f"Couldn't read {path.name}: {error}"


# -- version info ----------------------------------------------------------------------------------
def version_info(widget: tk.Misc | None = None, prefs: dict | None = None) -> str:
    """What a bug report needs, one item per line."""
    lines = [f"{APP_NAME} {__version__}" + (" (installed)" if getattr(sys, "frozen", False) else " (from source)")]
    if sys.platform == "win32":
        release, version, _service_pack, _kind = platform.win32_ver()
        lines.append(f"Windows {release} ({version})")
    else:
        lines.append(platform.platform())
    lines.append(f"Python {platform.python_version()} ({platform.architecture()[0]})")
    try:
        tk_version = str(widget.tk.call("info", "patchlevel")) if widget is not None else str(tk.TkVersion)
    except tk.TclError:
        tk_version = str(tk.TkVersion)
    lines.append(f"Tk {tk_version}")
    try:
        import soundfile

        lines.append(f"libsndfile {soundfile.__libsndfile_version__}")
    except (ImportError, OSError, AttributeError) as error:
        lines.append(f"libsndfile: not loaded ({error})")
    try:
        import sounddevice

        text = sounddevice.get_portaudio_version()[1]   # ("PortAudio V19.7.0-devel, revision …")
        lines.append(text if text.startswith("PortAudio") else f"PortAudio {text}")
    except (ImportError, OSError, AttributeError) as error:
        lines.append(f"PortAudio: not loaded ({error})")
    on = [e.key for e in experiments.switched_on(prefs or {})]
    lines.append(f"Experiments on: {', '.join(on) if on else 'none'}")
    return "\n".join(lines)


def _window(parent: tk.Misc, title: str, size: tuple[int, int], minimum: tuple[int, int], geometry: str) -> tk.Toplevel:
    # The first size is no taller than the screen the player is on (a laptop at 150% display scaling
    # has about 1,030 px), measured before the window is made so nothing half-made is drawn.
    room = placement.work_height(parent) - theme.px(FIT_MARGIN)
    height = max(theme.px(minimum[1]), min(theme.px(size[1]), room))
    win = tk.Toplevel(parent, bg=theme.BG)
    win.title(title)
    win.geometry(f"{theme.px(size[0])}x{height}")
    win.minsize(theme.px(minimum[0]), theme.px(minimum[1]))
    placement.restore(win, geometry)
    theme.dark_title_bar(win)
    return win


# -- the shortcut sheet ------------------------------------------------------------------------------
class HelpWindow:
    """Every keyboard shortcut, grouped, with a filter box."""

    def __init__(self, parent: tk.Misc, prefs: dict, on_about: Callable[[], None] | None = None):
        self.prefs = prefs
        self.win = _window(parent, "Keyboard shortcuts", (600, 640), (380, 320), prefs.get("help_geometry", ""))
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<Escape>", lambda e: self.close())
        self.win.bind("<Configure>", self._moved, add="+")
        for sequence in ("<Control-Key-f>", "<Control-Lock-Key-F>"):   # (Ctrl+Shift+F stays the library's)
            self.win.bind(sequence, lambda e: self.focus_filter() or "break")

        outer = ttk.Frame(self.win, padding=theme.px(theme.GAP_WIDE))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Keyboard shortcuts", style="Title.TLabel").pack(anchor="w")
        self.note = ttk.Label(outer, text="Most keys work in every Ironbark window. While you type in a box, only "
                                          "the Ctrl and function-key shortcuts act (Ctrl+Space still plays and "
                                          "pauses).", style="Info.TLabel", justify="left", wraplength=theme.px(420))
        self.note.pack(fill="x", pady=theme.pad(theme.GAP_HAIR, theme.GAP_MEDIUM))
        outer.bind("<Configure>", self._rewrap, add="+")
        self.search = controls.SearchBox(outer, "Filter: a key or what it does", on_clear=self._clear_filter)
        self.search.pack(fill="x")
        self.query = self.search.query
        self._trace = self.query.trace_add("write", lambda *_: self.fill())
        self.search.entry.bind("<Down>", lambda e: self.focus_list() or "break")
        self.search.entry.bind("<Return>", lambda e: self.focus_list() or "break")

        bottom = ttk.Frame(outer)
        bottom.pack(side="bottom", fill="x", pady=theme.pad(theme.GAP_MEDIUM, 0))
        if on_about is not None:
            controls.FlatButton(bottom, "About Ironbark", on_about,
                                label="Version, licences and privacy").pack(side="left")
        controls.FlatButton(bottom, "Close", self.close).pack(side="right")

        table = ttk.Frame(outer)
        table.pack(fill="both", expand=True, pady=theme.pad(theme.GAP, 0))
        self.table = table
        # A row too long for the list ends with "…"; selected, its whole text shows here, under the list.
        self.full = ttk.Label(outer, text="", style="Muted.TLabel", justify="left", wraplength=theme.px(420))
        self.tree = ttk.Treeview(table, columns=("keys", "what"), show="headings", selectmode="browse")
        self.tree.heading("keys", text="Key", anchor="w")
        self.tree.heading("what", text="What it does", anchor="w")
        # The key column as wide as its longest keys; what they do gets the rest of the width.
        widest = max(tkfont.nametofont("TkDefaultFont").measure(row[1]) for row in keys.sheet_rows())
        self.tree.column("keys", width=widest + theme.px(theme.GAP_LARGE * 2), stretch=False, anchor="w")
        self.tree.column("what", width=theme.px(300), anchor="w")
        self._fit = fit_columns(self.tree, {"what": 1}, minimum=theme.px(120))
        self._colour_tags()
        theme.on_change(self._palette_changed)
        scrollbar = controls.AutoScrollbar(outer, command=self.tree.yview)   # (in the window's margin)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.in_margin(table)
        self.tree.bind("<Home>", lambda e: self._go(0) or "break")
        self.tree.bind("<End>", lambda e: self._go(-1) or "break")
        self.tree.bind("<Up>", self._up, add="+")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_full(), add="+")
        self.tree.bind("<Configure>", lambda e: self._fit_rows(), add="+")   # (after fit_columns' own)
        self.rows: dict[str, tuple[str, str, str]] = {}   # tree row id -> (group, keys, what)
        self.cut: set[str] = set()   # rows whose words end with "…" in the list (too long for it)
        self._fitted_width = 0
        self.win.bind("<Destroy>", self._destroyed, add="+")
        self.fill()
        self.search.entry.focus_set()

    def _colour_tags(self):
        # (Treeview row colours aren't widget options: a palette change can't swap them by itself.)
        self.tree.tag_configure("group", foreground=theme.ACCENT_TEXT, font=theme.font(theme.SIZE_BODY, "semibold"))

    def _palette_changed(self, _palette):
        self._colour_tags()

    def _destroyed(self, event):
        if event.widget is not self.win or self.query is None:
            return
        try:
            self.query.trace_remove("write", self._trace)
        except tk.TclError:
            pass
        self.query = None

    def fill(self):
        """List the rows the filter finds (every word typed, in the key, the words or the group),
        each under its group's heading."""
        words = [fold(w) for w in (self.query.get() if self.query is not None else "").split()]
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        shown_group = None
        for n, (group, keys_text, what) in enumerate(keys.sheet_rows()):
            haystack = fold(f"{keys_text} {what} {group}")
            if words and not all(w in haystack for w in words):
                continue
            if group != shown_group:
                self.tree.insert("", "end", iid=f"g{n}", values=(group, ""), tags=("group",))
                shown_group = group
            iid = f"r{n}"
            self.tree.insert("", "end", iid=iid, values=(keys_text, what))
            self.rows[iid] = (group, keys_text, what)
        if words and not self.rows:
            self.tree.insert("", "end", iid="none", values=("", "No shortcut matches"))
        self._fitted_width = 0
        self._fit_rows()
        self._show_full()

    def _fit_rows(self):
        """Each row's words as they fit the "What it does" column: whole, or cut with "…" (Treeview
        cells don't wrap). Done again when the column's width changes."""
        width = int(self.tree.column("what", "width"))
        if width == self._fitted_width:
            return
        self._fitted_width = width
        room = width - theme.px(CELL_ROOM)
        font = tkfont.nametofont("TkDefaultFont")
        self.cut.clear()
        for iid, (_group, _keys, what) in self.rows.items():
            shown = what
            if font.measure(what) > room:
                low, high = 0, len(what)   # (the longest start of it that fits with "…" after it)
                while low < high:
                    middle = (low + high + 1) // 2
                    if font.measure(what[:middle].rstrip() + ELLIPSIS) <= room:
                        low = middle
                    else:
                        high = middle - 1
                shown = what[:low].rstrip() + ELLIPSIS
                self.cut.add(iid)
            self.tree.set(iid, "what", shown)
        self._show_full()

    def _show_full(self):
        """The selected row's whole text under the list, while the list cuts it short."""
        selected = self.tree.selection()
        row = self.rows.get(selected[0]) if selected and selected[0] in self.cut else None
        text = f"{row[1]}: {row[2]}" if row is not None else ""
        self.full.configure(text=text)
        if text and not self.full.winfo_manager():
            self.full.pack(side="bottom", fill="x", pady=theme.pad(theme.GAP_SMALL, 0), before=self.table)
            self.tree.after_idle(self._see_selected)   # (the list is shorter now: keep the row in view)
        elif not text and self.full.winfo_manager():
            self.full.pack_forget()

    def _see_selected(self):
        if self.tree.winfo_exists() and self.tree.selection():
            self.tree.update_idletasks()   # (the list's new height is laid out a pass or two later)
            self.tree.see(self.tree.selection()[0])

    def shown_rows(self) -> list[tuple[str, str, str]]:
        """(group, keys, what) of every row listed now, top to bottom."""
        return [self.rows[iid] for iid in self.tree.get_children() if iid in self.rows]

    def focus_filter(self):
        self.search.entry.focus_set()
        self.search.entry.select_range(0, "end")

    def focus_list(self):
        rows = [iid for iid in self.tree.get_children() if iid in self.rows]
        if rows:
            self.tree.focus_set()
            if not self.tree.selection():
                self.tree.selection_set(rows[0])
            self.tree.focus(self.tree.selection()[0])

    def _go(self, which: int):
        rows = [iid for iid in self.tree.get_children() if iid in self.rows]
        if rows:
            self.tree.selection_set(rows[which])
            self.tree.focus(rows[which])
            self.tree.see(rows[which])

    def _up(self, _event):
        """↑ on the first row goes back up to the filter box."""
        rows = [iid for iid in self.tree.get_children() if iid in self.rows]
        if rows and self.tree.focus() == rows[0]:
            self.focus_filter()
            return "break"
        return None

    def _clear_filter(self):
        """Esc in the filter (or its ×): empty it — Esc in an empty box closes the window."""
        if self.query is not None and self.query.get():
            self.query.set("")
            return "break"
        return None

    def _rewrap(self, event):
        width = max(theme.px(160), event.width - 2 * theme.px(theme.GAP_WIDE))
        self.note.configure(wraplength=width)
        self.full.configure(wraplength=width)

    def _moved(self, event):
        if event.widget is self.win:
            geometry = placement.current(self.win)
            if geometry:
                self.prefs["help_geometry"] = geometry

    def lift(self):
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def close(self):
        if self.win.winfo_exists():
            self.win.destroy()


# -- About ---------------------------------------------------------------------------------------------
class AboutWindow:
    """The version, copyright, licences, privacy and website. `copy(text)` puts text on the
    clipboard and `open_url(url)` opens the web browser (tests pass their own)."""

    def __init__(self, parent: tk.Misc, prefs: dict, copy: Callable[[str], None] | None = None,
                 open_url: Callable[[str], object] | None = None):
        self.prefs = prefs
        self.copy = copy or self._to_clipboard
        self.open_url = open_url or webbrowser.open
        # (Wide enough for a licence's 80-column lines in the small fixed-width font.)
        self.win = _window(parent, "About Ironbark", (600, 780), (360, 360), prefs.get("about_geometry", ""))
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<Escape>", lambda e: self.close())
        self.win.bind("<Configure>", self._moved, add="+")

        # The buttons stay at the foot; everything above them is a page that scrolls when the window
        # is shorter than it, so the whole privacy text and the licences can always be read. The
        # licence text takes whatever height the page has left (never less than LICENCE_LINES).
        bottom = ttk.Frame(self.win, padding=theme.pad(theme.GAP_WIDE, theme.GAP_MEDIUM, theme.GAP_WIDE,
                                                        theme.GAP_WIDE))
        bottom.pack(side="bottom", fill="x")
        self.area = controls.ScrollArea(self.win, bg="BG")
        self.area.pack(fill="both", expand=True)
        outer = ttk.Frame(self.area.inner, padding=theme.pad(theme.GAP_WIDE, theme.GAP_WIDE, theme.GAP_WIDE, 0))
        outer.pack(fill="both", expand=True)
        controls.FlatButton(bottom, "Copy version info", self.copy_version_info,
                            label="For a bug report: the version, Windows, Python, Tk, libsndfile, PortAudio "
                                  "and the experiments switched on").pack(side="left")
        self.copied = ttk.Label(bottom, text="", style="Muted.TLabel")
        self.copied.pack(side="left", padx=theme.pad(theme.GAP_MEDIUM, 0))
        controls.FlatButton(bottom, "Close", self.close).pack(side="right")

        on = len(experiments.switched_on(prefs))
        trying = f" · {on} experiment{'' if on == 1 else 's'} on" if on else ""
        ttk.Label(outer, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
        self.version_label = ttk.Label(outer, text=f"Version {__version__}{trying}", style="Sub.TLabel")
        self.version_label.pack(anchor="w")
        self.wrapped: list[ttk.Label] = []
        self._para(outer, "A music player that proves what it plays.", pady=(theme.GAP_MEDIUM, 0))
        self._para(outer, f"{copyright_line()}. Open source under the MIT licence.", pady=(theme.GAP_SMALL, 0))
        self.link = ttk.Label(outer, text=WEBSITE.removeprefix("https://"), style="UpNext.TLabel", cursor="hand2",
                              takefocus=True)
        self.link.pack(anchor="w", pady=theme.pad(theme.GAP_SMALL, 0))
        self.link.bind("<Button-1>", lambda e: self.visit())
        for sequence in ("<Return>", "<space>"):   # (Space here opens the link, not play / pause)
            self.link.bind(sequence, lambda e: self.visit() or "break")

        controls.section(outer, "Privacy")
        self.privacy = self._para(outer, PRIVACY)

        controls.section(outer, "Licences")
        self.documents: list[tuple[str, Path | None]] = [("Ironbark — MIT licence", None)] + licence_documents()
        self.chooser = ttk.Combobox(outer, state="readonly", values=[name for name, _p in self.documents])
        self.chooser.pack(fill="x")
        self.chooser.current(0)
        self.chooser.bind("<<ComboboxSelected>>", lambda e: self.show_document(self.chooser.current()))
        if len(self.documents) == 1:
            self._para(outer, "The bundled components' licence files weren't found beside the program. They're "
                              "listed in THIRD_PARTY_LICENSES.md with Ironbark's source code.",
                       style="Warn.TLabel", pady=(theme.GAP_SMALL, 0))
        holder = ttk.Frame(outer)
        holder.pack(fill="both", expand=True, pady=theme.pad(theme.GAP, 0))
        self.text = tk.Text(holder, wrap="word", height=LICENCE_LINES, bd=0, highlightthickness=0,
                            bg=theme.SURFACE, fg=theme.TEXT, insertbackground=theme.TEXT,
                            selectbackground=theme.SELECT, selectforeground=theme.ON_ACCENT,
                            font=theme.mono(theme.SIZE_SMALL), padx=theme.px(theme.GAP_MEDIUM),
                            pady=theme.px(theme.GAP))
        scrollbar = controls.AutoScrollbar(holder, command=self.text.yview)
        self.text.configure(yscrollcommand=scrollbar.set)
        self.text.pack(side="left", fill="both", expand=True)
        scrollbar.auto_pack(side="right", fill="y", padx=(theme.px(theme.GAP_HAIR), 0), before=self.text)
        self.area.bind_wheel()
        # (The wheel over the licence text scrolls the text, not the page.)
        self.text.bindtags(tuple(tag for tag in self.text.bindtags() if tag != self.area.wheel_tag))
        self._wrap_width = 0
        self._fit_job = None
        outer.bind("<Configure>", self._rewrap, add="+")
        self.area.canvas.bind("<Configure>", lambda e: self._fit_text_later(), add="+")
        self.show_document(0)

    def _para(self, parent, text: str, style: str = "TLabel", pady=(0, 0)) -> ttk.Label:
        label = ttk.Label(parent, text=text, style=style, justify="left", wraplength=theme.px(440))
        label.pack(fill="x", pady=theme.pad(*pady))
        self.wrapped.append(label)
        return label

    def show_document(self, index: int):
        """Show licence `index` of `documents` in the text area (read-only)."""
        _name, path = self.documents[index]
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", read_document(path))
        self.text.configure(state="disabled")
        self.text.yview_moveto(0)
        self.chooser.current(index)
        self.chooser.selection_clear()

    def shown_text(self) -> str:
        return self.text.get("1.0", "end-1c")

    def visit(self):
        """The website, in the web browser — only ever when it's clicked (nothing else here goes online)."""
        self.open_url(WEBSITE)

    def copy_version_info(self):
        self.copy(version_info(self.win, self.prefs))
        self.copied.configure(text="Copied")
        self.win.after(2500, lambda: self.copied.winfo_exists() and self.copied.configure(text=""))

    def _to_clipboard(self, text: str):
        self.win.clipboard_clear()
        self.win.clipboard_append(text)

    def _rewrap(self, event):
        """The page changed size: the paragraphs wrap to its width, and the licence text is fitted
        again once Tk has laid out their new heights (the page's height changes with them)."""
        width = max(theme.px(160), event.width - 2 * theme.px(theme.GAP_WIDE))
        if width != self._wrap_width:
            self._wrap_width = width
            for label in self.wrapped:
                label.configure(wraplength=width)
        self._fit_text_later()

    def _fit_text_later(self):
        if self._fit_job is None:
            self._fit_job = self.win.after_idle(self._fit_text)

    def _fit_text(self):
        """The licence text as tall as the room the page has left (whole lines), never less than
        LICENCE_LINES: past that, the page scrolls instead."""
        self._fit_job = None
        if not self.win.winfo_exists():
            return
        room = self.area.canvas.winfo_height()
        if room <= 1:
            return   # (not laid out yet: its first <Configure> fits it)
        line = max(1, int(self.text.tk.call("font", "metrics", self.text.cget("font"), "-linespace")))
        others = self.area.inner.winfo_reqheight() - self.text.winfo_reqheight()
        lines = max(LICENCE_LINES, (room - others - 2 * int(self.text.cget("pady"))) // line)
        if lines != int(self.text.cget("height")):
            self.text.configure(height=lines)

    def _moved(self, event):
        if event.widget is self.win:
            geometry = placement.current(self.win)
            if geometry:
                self.prefs["about_geometry"] = geometry

    def lift(self):
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def close(self):
        if self.win.winfo_exists():
            self.win.destroy()
