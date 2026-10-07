"""The Edit tags window and the Tag history window (0.16.0) — only reachable while Settings →
Experimental → "Tag editing" is on (ui.PlayerWindow checks before opening either).

Edit tags: one song or many (a whole album). A field the songs don't share shows "(various)" in
grey and is left alone unless you type over it. The cover can be replaced (a JPEG or PNG) or
removed. Save asks first ("Save changes to 12 files?"), then tag_editor does the careful part on
its own thread: old tags to the history, each file copied, changed, checked sample for sample,
swapped in. A song that's playing waits until the player moves on.

Tag history: every save, newest first; Undo puts the old tags back the same careful way.
"""

from __future__ import annotations

import hashlib
import io
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Callable

from PIL import Image, ImageOps, ImageTk

from . import audiofile, controls, placement, popups, tag_editor, theme
from .tag_editor import VARIOUS, Edit, TagSave

COVER_PX = 88
PICTURE_TYPES = [("Pictures (JPEG, PNG)", "*.jpg *.jpeg *.png"), ("All files", "*.*")]


def ask(parent, ok: str = "OK") -> Callable[[str, str], bool]:
    """The confirm question ("Save changes to 12 files?"), as an in-app box over the window — modal,
    Enter = `ok`, Esc = Cancel (popups.confirm, 0.21.0; tests pass their own)."""
    return lambda message, detail: popups.confirm(parent, message, detail, ok=ok)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


class _Saving:
    """Runs a TagSave on its own thread and reports back on the Tk thread (shared by both windows).
    `stop()` asks it to stop after the file in hand; `wait()` waits for the thread (it never
    touches Tk, so the player can wait for it before it closes)."""

    def __init__(self, owner: tk.Misc, save: TagSave, busy: set[Path], on_progress, on_done):
        self.owner, self.save = owner, save
        self.on_progress, self.on_done = on_progress, on_done
        self.cancel = threading.Event()
        self.finished = False
        self.error: Exception | None = None
        self._at = (0, len(save.jobs), None)
        self._thread = threading.Thread(target=self._work, args=(busy,), name="tag-save", daemon=True)
        self._thread.start()
        owner.after(100, self._tick)

    def _work(self, busy: set[Path]):
        try:
            self.save.run(busy=lambda path: path in busy, cancel=self.cancel,
                          progress=lambda i, n, path: setattr(self, "_at", (i, n, path)))
        except Exception as error:  # noqa: BLE001 — reported in the window, not lost on stderr
            self.error = error
            self.save.give_up(f"stopped by an unexpected error ({error})")
        finally:
            self.finished = True

    def stop(self):
        """Stop after the file in hand (a file is never left half done)."""
        self.cancel.set()

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for the save to finish; True once it has."""
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _tick(self):
        if not self.finished:
            self.on_progress(*self._at)
            self.owner.after(100, self._tick)
            return
        self.on_done(self.save)


class TagWindow:
    def __init__(self, parent: tk.Misc, tracks: list, prefs: dict, on_saved: Callable[[TagSave], None],
                 busy: Callable[[], set[Path]], on_history: Callable[[], None] = lambda: None,
                 confirm: Callable[[str, str], bool] | None = None,
                 choose_picture: Callable[[], str] | None = None):
        self.parent, self.prefs = parent, prefs
        self.on_saved, self.busy, self.on_history = on_saved, busy, on_history
        self.editable = [t for t in tracks if not tag_editor.why_read_only(t)]
        self.skipped = [(t, tag_editor.why_read_only(t)) for t in tracks if tag_editor.why_read_only(t)]
        self.shown: dict[str, str] = {}
        # Each song's embedded cover as a SHA-256 (None = no cover), and the first cover's bytes
        # for the thumbnail: a big selection never holds every file's picture at once.
        self.covers: list[str | None] = []
        self.cover_preview: bytes | None = None
        self.cover_change: tag_editor.Cover | None = None
        self.saving: _Saving | None = None
        self._loaded: dict | None = None

        self.win = tk.Toplevel(parent, bg=theme.BG)
        self.win.title("Edit tags")
        self.win.geometry(f"{theme.px(440)}x{theme.px(560)}")
        self.win.minsize(theme.px(340), theme.px(420))
        placement.restore(self.win, prefs.get("tags_geometry", ""))
        theme.dark_title_bar(self.win)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<Escape>", lambda e: self.close())
        self.win.bind("<Configure>", self._moved, add="+")
        self.confirm = confirm or ask(self.win, "Save")
        self.choose_picture = choose_picture or (lambda: filedialog.askopenfilename(
            parent=self.win, title="Choose the cover picture", filetypes=PICTURE_TYPES))

        outer = ttk.Frame(self.win, padding=theme.px(theme.GAP_WIDE))
        outer.pack(fill="both", expand=True)
        self.heading = ttk.Label(outer, text=self._heading(), style="Title.TLabel", wraplength=theme.px(400),
                                 justify="left")
        self.heading.pack(fill="x")
        ttk.Label(outer, text="Experimental. Your old tags are kept first (Tag history), so every save can be "
                              "undone, and each file's sound is checked sample for sample before it's replaced.",
                  style="Info.TLabel", wraplength=theme.px(400), justify="left").pack(fill="x", pady=theme.pad(2, 0))
        if self.skipped:
            reasons = sorted({reason for _t, reason in self.skipped})
            what = _plural(len(self.skipped), "song")
            ttk.Label(outer, text=f"{what} can't be edited here: " + "; ".join(reasons) + ".",
                      style="Warn.TLabel", wraplength=theme.px(400), justify="left").pack(fill="x", pady=theme.pad(4, 0))

        # Bottom first, so a short window squeezes the fields rather than hiding the buttons.
        bottom = ttk.Frame(outer)
        bottom.pack(side="bottom", fill="x", pady=theme.pad(10, 0))
        controls.FlatButton(bottom, "Tag history…", self.on_history).pack(side="left")
        self.save_button = controls.FlatButton(bottom, "Save", self.save, kind="primary", state="disabled",
                                               label="Save (Enter)")
        self.save_button.pack(side="right")
        self.cancel_button = controls.FlatButton(bottom, "Cancel", self.close)
        self.cancel_button.pack(side="right", padx=theme.pad(0, theme.GAP))
        self.status = ttk.Label(outer, text="Reading tags…", style="Muted.TLabel", wraplength=theme.px(400),
                                justify="left")
        self.status.pack(side="bottom", fill="x", pady=theme.pad(8, 0))

        form = ttk.Frame(outer)
        form.pack(fill="both", expand=True, pady=theme.pad(10, 0))
        form.columnconfigure(1, weight=1)
        self.vars: dict[str, tk.StringVar] = {}
        self.entries: dict[str, ttk.Entry] = {}
        for row, (name, label) in enumerate(tag_editor.FIELDS):
            ttk.Label(form, text=label, style="Muted.TLabel").grid(row=row, column=0, sticky="w", padx=theme.pad(0, 10), pady=theme.px(2))
            var = tk.StringVar()
            entry = ttk.Entry(form, textvariable=var, state="disabled")
            entry.grid(row=row, column=1, sticky="ew", pady=theme.px(2))
            entry.bind("<FocusIn>", lambda e, w=entry: w.select_range(0, "end"))
            entry.bind("<Return>", lambda e: self.save())
            var.trace_add("write", lambda *_, n=name: self._paint_field(n))
            self.vars[name], self.entries[name] = var, entry

        cover_row = ttk.Frame(form)
        cover_row.grid(row=len(tag_editor.FIELDS), column=0, columnspan=2, sticky="ew", pady=theme.pad(10, 0))
        size = theme.px(COVER_PX)
        self.cover_image = tk.Label(cover_row, bg=theme.SURFACE, width=size, height=size, bd=0)
        self.cover_image.pack(side="left")
        side = ttk.Frame(cover_row)
        side.pack(side="left", fill="both", expand=True, padx=theme.pad(10, 0))
        ttk.Label(side, text="Cover", style="Muted.TLabel").pack(anchor="w")
        self.cover_text = ttk.Label(side, text="", wraplength=theme.px(260), justify="left")
        self.cover_text.pack(anchor="w", fill="x")
        buttons = ttk.Frame(side)
        buttons.pack(anchor="w", pady=theme.pad(6, 0))
        self.choose_button = controls.FlatButton(buttons, "Choose…", self.choose_cover, state="disabled")
        self.choose_button.pack(side="left")
        self.remove_button = controls.FlatButton(buttons, "Remove", self.remove_cover, state="disabled")
        self.remove_button.pack(side="left", padx=theme.pad(theme.GAP, 0))
        self.keep_button = controls.FlatButton(buttons, "Keep as is", self.keep_cover)
        self._photo = None

        threading.Thread(target=self._read, name="tag-read", daemon=True).start()
        self.win.after(50, self._wait_for_read)

    def _heading(self) -> str:
        songs = self.editable or [t for t, _r in self.skipped]
        if len(songs) == 1:
            return f"“{songs[0].title}”"
        albums = {t.album for t in songs}
        album = next(iter(albums)) if len(albums) == 1 and next(iter(albums)) else ""
        return f"{len(songs)} songs" + (f" of “{album}”" if album else "")

    # -- reading what's there ---------------------------------------------------------------------
    def _read(self):
        values, covers, failed, preview = [], [], [], None
        try:
            for track in self.editable:
                path, kind = Path(audiofile.real_path(track.path)), tag_editor.kind_of(track)
                try:
                    fields = tag_editor.read_fields(path, kind)
                    cover = tag_editor.read_cover(path, kind)
                except Exception as error:  # noqa: BLE001 — a damaged tag is a reason to show, not a crash
                    failed.append((track, f"its tags couldn't be read ({error})"))
                    continue
                values.append(fields)
                covers.append(hashlib.sha256(cover).hexdigest() if cover else None)
                if preview is None and cover:
                    preview = cover
        finally:   # (always: the window waits for this)
            self._loaded = {"values": values, "covers": covers, "preview": preview, "failed": failed}

    def _wait_for_read(self):
        if not self.win.winfo_exists():
            return
        if self._loaded is None:
            self.win.after(50, self._wait_for_read)
            return
        failed = self._loaded["failed"]
        if failed:
            gone = {id(t) for t, _r in failed}
            self.editable = [t for t in self.editable if id(t) not in gone]
            self.skipped += failed
        self.covers, self.cover_preview = self._loaded["covers"], self._loaded["preview"]
        self.shown = tag_editor.common(self._loaded["values"]) if self.editable else {}
        for name, var in self.vars.items():
            var.set(self.shown.get(name, ""))
            self.entries[name].configure(state="normal" if self.editable else "disabled")
        self._paint_cover()
        if self.editable:
            self.save_button.configure(state="normal")
            self.choose_button.configure(state="normal")
            self.remove_button.configure(state="normal")
            self.status.configure(text=f"{_plural(len(self.editable), 'file')} to edit", style="Muted.TLabel")
            self.entries["title" if len(self.editable) == 1 else "album"].focus_set()
        else:
            reasons = "; ".join(sorted({r for _t, r in self.skipped}))
            self.status.configure(text=f"Nothing here can be edited: {reasons}", style="Warn.TLabel")

    def _paint_field(self, name: str):
        various = self.vars[name].get() == VARIOUS and self.shown.get(name) == VARIOUS
        self.entries[name].configure(style="Various.TEntry" if various else "TEntry")

    # -- the cover ------------------------------------------------------------------------------------
    def _paint_cover(self):
        change = self.cover_change
        if change is not None and change.data is not None:
            data, text = change.data, (f"New: {change.name or 'picture'} — {change.width} × {change.height}, "
                                       "saved when you press Save")
        elif change is not None:
            data, text = None, "Removed when you press Save (every picture stored in the file; undo brings them back)"
        else:
            distinct = set(self.covers)
            if not self.covers:
                data, text = None, ""
            elif len(distinct) > 1:
                data, text = self.cover_preview, "(various) — the songs have different covers"
            elif self.covers[0] is None:
                data, text = None, ("No cover stored in the file" + ("s" if len(self.covers) > 1 else "")
                                    + " (the player shows a picture from the folder, if there is one)")
            else:
                data = self.cover_preview
                text = "Stored in the file" if len(self.covers) == 1 else f"The same cover in all {len(self.covers)}"
        self.cover_text.configure(text=text)
        size = theme.px(COVER_PX)
        image = None
        if data:
            try:
                with Image.open(io.BytesIO(data)) as picture:
                    image = ImageOps.fit(ImageOps.exif_transpose(picture).convert("RGB"), (size, size))
            except (OSError, ValueError, Image.DecompressionBombError):
                image = None
        if image is None:
            image = Image.new("RGB", (size, size), theme.SURFACE)
        self._photo = ImageTk.PhotoImage(image, master=self.win)
        self.cover_image.configure(image=self._photo, width=size, height=size)
        if change is not None:
            self.keep_button.pack(side="left", padx=theme.pad(6, 0))
        else:
            self.keep_button.pack_forget()

    def choose_cover(self):
        name = self.choose_picture()
        if not name:
            return
        try:
            self.cover_change = tag_editor.load_cover(name)
        except tag_editor.SaveError as error:
            self.status.configure(text=str(error), style="Warn.TLabel")
            return
        self._paint_cover()

    def remove_cover(self):
        self.cover_change = tag_editor.REMOVE_COVER
        self._paint_cover()

    def keep_cover(self):
        self.cover_change = None
        self._paint_cover()

    # -- saving -------------------------------------------------------------------------------------
    def edit(self) -> Edit:
        typed = {name: var.get() for name, var in self.vars.items()}
        return Edit(tag_editor.changes(self.shown, typed), self.cover_change)

    def save(self):
        if self.saving is not None or not self.editable:
            return
        edit = self.edit()
        if not edit:
            self.status.configure(text="Nothing changed yet — type over a field, or choose a cover",
                                  style="Muted.TLabel")
            return
        problems = tag_editor.validate(edit.fields, {tag_editor.kind_of(t) for t in self.editable})
        if problems:
            self.status.configure(text=" · ".join(problems), style="Warn.TLabel")
            return
        n = len(self.editable)
        message = f"Save changes to {_plural(n, 'file')}?"
        detail = (f"Changing: {edit.describe()}.\n\nYour old tags are kept first (Tag history), so this can be "
                  "undone. Each file is copied, the copy checked sample for sample, and only then swapped in.")
        if not self.confirm(message, detail):
            return
        what = self._heading()
        save = TagSave(tag_editor.edit_jobs(self.editable, edit), f"{edit.describe()} — {what}")
        self._start(save)

    def _start(self, save: TagSave):
        for entry in self.entries.values():
            entry.configure(state="disabled")
        for button in (self.save_button, self.choose_button, self.remove_button, self.keep_button):
            button.configure(state="disabled")
        self.cancel_button.configure(text="Stop")
        self.saving = _Saving(self.win, save, self.busy(), self._progress, self._done)

    def _progress(self, i: int, n: int, path: Path | None):
        name = f" — {path.name}" if path is not None else ""
        self.status.configure(text=f"Saving {min(i + 1, n)} of {n}{name}: checking the sound is untouched…",
                              style="Muted.TLabel")

    def _done(self, save: TagSave):
        self.saving = None
        self.on_saved(save)
        if not save.failed and not save.waiting:
            self.status.configure(text=f"✓ {save.describe()}", style="Good.TLabel")
            self.win.after(1200, self._close_now)
            return
        lines = [save.describe()]
        lines += [f"{r.path.name}: {r.reason}" for r in save.results if r.state in ("failed", "waiting")]
        self.status.configure(text="\n".join(lines[:8]), style="Warn.TLabel" if save.failed else "Muted.TLabel")
        self.cancel_button.configure(text="Close")

    # -- window ---------------------------------------------------------------------------------------
    def _moved(self, event):
        if event.widget is self.win:
            geometry = placement.current(self.win)
            if geometry:
                self.prefs["tags_geometry"] = geometry

    def lift(self):
        self.win.deiconify()
        self.win.lift()

    def close(self):
        if self.saving is not None:  # never left half done: stop after the file in hand
            self.saving.cancel.set()
            self.status.configure(text="Stopping after this file…", style="Muted.TLabel")
            return
        self._close_now()

    def _close_now(self):
        if self.win.winfo_exists():
            self.win.destroy()


class TagHistoryWindow:
    """Every tag save, newest first. Undo puts the old tags back (the same careful way)."""

    def __init__(self, parent: tk.Misc, prefs: dict, on_saved: Callable[[TagSave], None],
                 busy: Callable[[], set[Path]], confirm: Callable[[str, str], bool] | None = None):
        self.prefs, self.on_saved, self.busy = prefs, on_saved, busy
        self.saving: _Saving | None = None
        self.reading = False   # Undo is reading the entry's files (on a thread) before it asks
        self.entries: list[dict] = []
        self.win = tk.Toplevel(parent, bg=theme.BG)
        self.win.title("Tag history")
        self.win.geometry(f"{theme.px(560)}x{theme.px(360)}")
        self.win.minsize(theme.px(380), theme.px(240))
        placement.restore(self.win, prefs.get("tag_history_geometry", ""))
        theme.dark_title_bar(self.win)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<Escape>", lambda e: self.close())
        self.win.bind("<Configure>", self._moved, add="+")
        self.confirm = confirm or ask(self.win, "Undo")

        outer = ttk.Frame(self.win, padding=theme.px(theme.GAP_WIDE))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=f"Every tag save, newest first. Undo puts that save's old tags back. Kept in "
                              f"{tag_editor.history_folder()}", style="Info.TLabel",
                  wraplength=theme.px(520), justify="left").pack(fill="x")
        bottom = ttk.Frame(outer)
        bottom.pack(side="bottom", fill="x", pady=theme.pad(8, 0))
        self.status = ttk.Label(bottom, text="", style="Muted.TLabel", wraplength=theme.px(400), justify="left")
        self.status.pack(side="left", fill="x", expand=True)
        self.undo_button = controls.FlatButton(bottom, "Undo", self.undo, state="disabled",
                                               label="Put this save's old tags back")
        self.undo_button.pack(side="right")
        table = ttk.Frame(outer)
        table.pack(fill="both", expand=True, pady=theme.pad(8, 0))
        self.tree = ttk.Treeview(table, columns=("when", "what", "files", "state"), show="headings",
                                 selectmode="browse")
        for column, title, width, stretch in (("when", "When", 128, False), ("what", "What", 260, True),
                                              ("files", "Files", 52, False), ("state", "", 90, False)):
            self.tree.heading(column, text=title, anchor="w")
            self.tree.column(column, width=theme.px(width), stretch=stretch, anchor="w")
        # (0.22.0) The slim scrollbar, only there when the list is longer than its window.
        scrollbar = controls.AutoScrollbar(table, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.auto_pack(side="right", fill="y", padx=(theme.px(theme.GAP_HAIR), 0), before=self.tree)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._paint_button())
        self.reload()

    def reload(self):
        self.entries = tag_editor.history()
        self.tree.delete(*self.tree.get_children())
        for i, entry in enumerate(self.entries):
            files = entry.get("files", [])
            saved = sum(1 for f in files if f.get("saved"))
            waiting = sum(1 for f in files if f.get("state") == "waiting")
            state = ("undone" if entry.get("undone_by") else "waiting" if waiting
                     else "not saved" if not saved else "")
            self.tree.insert("", "end", iid=str(i), values=(entry.get("when", ""), entry.get("summary", ""),
                                                            f"{saved} of {len(files)}" if saved != len(files)
                                                            else str(saved), state))
        if not self.entries:
            self.status.configure(text="No tag edits yet")
        self._paint_button()

    def _selected(self) -> dict | None:
        selection = self.tree.selection()
        return self.entries[int(selection[0])] if selection else None

    def _paint_button(self):
        entry = self._selected()
        ok = (entry is not None and self.saving is None and not self.reading and not entry.get("undone_by")
              and any(f.get("saved") for f in entry.get("files", [])))
        self.undo_button.configure(state="normal" if ok else "disabled")

    def undo(self, entry: dict | None = None):
        """Read the entry's files on a thread (every file is opened: slow for an album on a network
        drive), then ask, then put the old tags back."""
        entry = entry or self._selected()
        if entry is None or self.saving is not None or self.reading or entry.get("undone_by"):
            return
        found: dict = {}

        def work():
            try:
                found["jobs"] = tag_editor.undo_jobs(entry)
            except Exception as error:  # noqa: BLE001 — shown as the reason, not a crash
                found["error"] = error

        self.reading = True
        self.undo_button.configure(state="disabled")
        self.status.configure(text="Reading the files…", style="Muted.TLabel")
        threading.Thread(target=work, name="tag-undo-read", daemon=True).start()
        self.win.after(50, self._wait_for_jobs, entry, found)

    def _wait_for_jobs(self, entry: dict, found: dict):
        if not self.win.winfo_exists():
            return
        if not found:
            self.win.after(50, self._wait_for_jobs, entry, found)
            return
        self.reading = False
        self.status.configure(text="")
        if "error" in found:
            self.status.configure(text=f"Couldn't read the files: {found['error']}", style="Warn.TLabel")
            self._paint_button()
            return
        jobs, changed, gone = found["jobs"]
        if not jobs:
            self.status.configure(text="Nothing to undo: its files have gone (moved or deleted)", style="Warn.TLabel")
            self._paint_button()
            return
        detail = "Each file is copied, checked sample for sample, and only then swapped in."
        if changed:
            detail = (f"{_plural(len(changed), 'file')} {'has' if len(changed) == 1 else 'have'} been edited again "
                      "since — undoing puts the older tags back, so those later changes go too.\n\n" + detail)
        if gone:
            detail = f"{_plural(len(gone), 'file')} can't be found and will be left out.\n\n" + detail
        if not self.confirm(f"Undo “{entry.get('summary', '')}” on {_plural(len(jobs), 'file')}?", detail):
            self._paint_button()
            return
        save = TagSave(jobs, f"Undo: {entry.get('summary', '')}", undo_of=entry)
        self.undo_button.configure(state="disabled")
        self.saving = _Saving(self.win, save, self.busy(), self._progress, self._done)

    def _progress(self, i: int, n: int, path: Path | None):
        self.status.configure(text=f"Undoing {min(i + 1, n)} of {n}…", style="Muted.TLabel")

    def _done(self, save: TagSave):
        self.saving = None
        self.on_saved(save)
        tone = "Warn.TLabel" if save.failed else "Good.TLabel" if not save.waiting else "Muted.TLabel"
        lines = [save.describe()] + [f"{r.path.name}: {r.reason}" for r in save.results
                                     if r.state in ("failed", "waiting")][:5]
        self.status.configure(text="\n".join(lines), style=tone)
        self.reload()

    def _moved(self, event):
        if event.widget is self.win:
            geometry = placement.current(self.win)
            if geometry:
                self.prefs["tag_history_geometry"] = geometry

    def lift(self):
        self.win.deiconify()
        self.win.lift()

    def close(self):
        if self.saving is not None:
            self.saving.cancel.set()
            return
        self.win.destroy()
