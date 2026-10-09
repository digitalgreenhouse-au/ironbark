"""The Compare window (key D): are these two files the same music, sample for sample?

Pick A and B, press Compare. The work (compare.py) runs on its own thread; this file only asks
and shows. Results in the usual style: green when identical, amber where they differ.
"""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from .. import audiofile, controls, theme, toolwin
from ..library import AUDIO_EXTENSIONS
from . import compare

# Every kind of file the player plays (AIFF, Apple Lossless, Ogg… as well as FLAC, WAV, MP3).
AUDIO_TYPES = [("Audio files", " ".join(f"*{suffix}" for suffix in sorted(AUDIO_EXTENSIONS))), ("All files", "*.*")]
TONES = {"good": "GoodValue.TLabel", "warn": "WarnValue.TLabel", "plain": "TLabel"}


def choose_file(parent, title: str, start: Path | None) -> Path | None:
    folder = start if start is not None and start.is_dir() else (start.parent if start is not None else None)
    name = filedialog.askopenfilename(title=title, parent=parent, filetypes=AUDIO_TYPES,
                                      initialdir=str(folder) if folder else None)
    return Path(name) if name else None


class FileRow:
    """'A  name of the file  [Choose…]' — one of the two files being compared."""

    def __init__(self, parent: ttk.Frame, row: int, letter: str, on_choose):
        ttk.Label(parent, text=letter, style="Title.TLabel").grid(row=row, column=0, sticky="w", padx=theme.pad(0, 8))
        self.label = ttk.Label(parent, text="(none)", style="Muted.TLabel", wraplength=theme.px(300))
        self.label.grid(row=row, column=1, sticky="w", pady=theme.px(2))
        self.button = controls.FlatButton(parent, "Choose…", on_choose)
        self.button.grid(row=row, column=2, sticky="e", padx=theme.pad(8, 0), pady=theme.px(2))
        self.path: Path | None = None

    def set(self, path: Path | None):
        self.path = path
        if path is None:
            self.label.configure(text="(none)", style="Muted.TLabel")
        else:   # (a CUE sheet track: "Album.flac · track 3")
            self.label.configure(text=f"{audiofile.display_name(path)}\n{path.parent}", style="TLabel")


class CompareWindow(toolwin.ToolWindow):
    TITLE = "Compare two files"
    SIZE = (480, 460)
    MIN_SIZE = (360, 300)
    GEOMETRY_KEY = "compare_geometry"
    REFRESH_MS = 100

    def __init__(self, parent: tk.Misc, prefs: dict, path_a=None, path_b=None, on_close=lambda: None):
        super().__init__(parent, prefs, on_close)

        outer = ttk.Frame(self.win, padding=theme.px(theme.GAP_WIDE))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Are two files the same music, sample for sample? The music is compared, not "
                              "the file bytes: a FLAC and a WAV of the same rip count as identical.",
                  style="Muted.TLabel", wraplength=theme.px(440), justify="left").pack(fill="x")
        files = ttk.Frame(outer)
        files.pack(fill="x", pady=theme.pad(10, 0))
        files.columnconfigure(1, weight=1)
        self.rows = {"A": FileRow(files, 0, "A", lambda: self._choose("A")),
                     "B": FileRow(files, 1, "B", lambda: self._choose("B"))}
        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=theme.pad(10, 0))
        self.go = controls.FlatButton(bar, "Compare", self.start, kind="primary")
        self.go.pack(side="left")
        self.status = ttk.Label(bar, text="", style="Muted.TLabel")
        self.status.pack(side="left", padx=theme.pad(10, 0))
        controls.section(outer, "Result")
        self.results = ttk.Frame(outer)
        self.results.pack(fill="both", expand=True)
        self.results.columnconfigure(0, minsize=theme.px(84))
        self.results.bind("<Configure>", self._rewrap)

        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._progress = 0.0
        self._outcome = None        # a Comparison, or an error message
        self._job = None
        self.result: compare.Comparison | None = None
        # The paths themselves: a CUE sheet track's path carries which part of the file it is.
        for letter, path in (("A", path_a), ("B", path_b)):
            self.rows[letter].set(None if not path else path if isinstance(path, Path) else Path(path))
        self._refresh_button()

    def _choose(self, letter: str):
        other = self.rows["B" if letter == "A" else "A"].path
        start = self.rows[letter].path or other
        path = choose_file(self.win, f"Choose file {letter}", start.parent if start else None)
        if path is not None:
            self.rows[letter].set(path)
            self._refresh_button()

    def _refresh_button(self):
        ready = all(r.path is not None for r in self.rows.values()) and self._thread is None
        self.go.state(["!disabled"] if ready else ["disabled"])

    # -- the work ---------------------------------------------------------------------------
    def start(self):
        a, b = self.rows["A"].path, self.rows["B"].path
        if a is None or b is None or self._thread is not None:
            return
        self._cancel = threading.Event()
        self._progress, self._outcome = 0.0, None
        self._show([])
        self.status.configure(text="Comparing…")
        cancel = self._cancel

        def work():
            try:
                outcome = compare.compare_files(a, b, cancel=cancel,
                                                progress=lambda f: setattr(self, "_progress", f))
            except Exception as exc:  # noqa: BLE001 — an unreadable file is a result to show, not a crash
                outcome = f"Couldn't compare: {exc}"
            self._outcome = outcome

        self._thread = threading.Thread(target=work, name="compare", daemon=True)
        self._thread.start()
        self._refresh_button()
        self._job = self.win.after(100, self._tick)

    def _tick(self):
        self._job = None
        if not self.win.winfo_exists():
            return
        if self._outcome is None:
            self.status.configure(text=f"Comparing… {self._progress:.0%}")
            self._job = self.win.after(100, self._tick)
            return
        outcome, self._thread = self._outcome, None
        self._refresh_button()
        if isinstance(outcome, str):
            self.status.configure(text="")
            self._show([("Result", outcome, "warn")])
            return
        self.result = outcome
        self.status.configure(text="Done")
        self._show(compare.describe(outcome))

    def _show(self, rows):
        for child in self.results.winfo_children():
            child.destroy()
        width = self._wrap_width()
        for row, (label, text, tone) in enumerate(rows):
            ttk.Label(self.results, text=label, style="Muted.TLabel").grid(row=row, column=0, sticky="nw", pady=theme.px(1))
            ttk.Label(self.results, text=text, style=TONES.get(tone, "TLabel"), wraplength=width,
                      justify="left").grid(row=row, column=1, sticky="w", pady=theme.px(1))

    def _wrap_width(self) -> int:
        return max(200, self.results.winfo_width() - theme.px(96))

    def _rewrap(self, event):
        width = self._wrap_width()
        for child in self.results.grid_slaves(column=1):
            child.configure(wraplength=width)

    # -- window -------------------------------------------------------------------------------
    def _closing(self):
        self._cancel.set()
