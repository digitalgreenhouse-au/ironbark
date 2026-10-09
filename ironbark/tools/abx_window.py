"""The ABX blind test window (key X).

1. Pick A and B (A starts as the song playing) and the excerpt: a stretch of 5–30 s chosen on
   the little bar (click or drag). Both are decoded into memory and lined up (abx.prepare).
2. Listen: A and B as often as you like (keys A / B, space stops). The levels are checked;
   when they differ by more than 0.1 dB, "Match levels" turns the louder one down.
3. Start the test: each trial, X is A or B (drawn up front); listen to A, B and X as much as
   you like, then say which X is. The score stays hidden until the last trial; then the result,
   its p-value, and a text log saved in %APPDATA%\\Ironbark\\ABX\\.

Sound: audition.py loops the excerpt and switches at the same sample with an identical dip,
on the player's output and mode, at the player's volume. The player is paused while you listen
(suspend: it lets go of the sound device) and stays paused — press play there to carry on.
Another window that plays its own sound (Clipping → Hear it, Test signals) stops this one first,
and the other way round.
"""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from .. import controls, theme, toolwin
from ..engine import State
from . import abx, audition, compare
from .compare_window import FileRow, choose_file

REFRESH_MS = 100
RELOAD_AFTER_MS = 350      # wait for the bar to stop moving before decoding
SLOTS = {"A": 0, "B": 1, "X": 2}


class AbxWindow(toolwin.ToolWindow):
    TITLE = "ABX blind test"
    SIZE = (500, 640)
    MIN_SIZE = (380, 520)
    GEOMETRY_KEY = "abx_geometry"
    REFRESH_MS = REFRESH_MS

    def __init__(self, parent: tk.Misc, player, prefs: dict, path_a=None, start_s: float | None = None,
                 on_close=lambda: None, stream_factory=None):
        self.player = player
        self._stream_factory = stream_factory   # tests: a stand-in for the sound device
        super().__init__(parent, prefs, on_close)

        outer = ttk.Frame(self.win, padding=theme.px(theme.GAP_WIDE))
        outer.pack(fill="both", expand=True)
        self.outer = outer
        intro = ("Can you really hear the difference? Listen to A and B, then the test: X is secretly one of "
                 "them each time, and you say which. Guessing gets about half right.")
        ttk.Label(outer, text=intro, style="Muted.TLabel", wraplength=theme.px(460),
                  justify="left").pack(fill="x")

        files = ttk.Frame(outer)
        files.pack(fill="x", pady=theme.pad(10, 0))
        files.columnconfigure(1, weight=1)
        self.rows = {"A": FileRow(files, 0, "A", lambda: self._choose("A")),
                     "B": FileRow(files, 1, "B", lambda: self._choose("B"))}

        controls.section(outer, "Excerpt")
        self.bar = tk.Canvas(outer, height=theme.px(26), bg=theme.BG, highlightthickness=0)
        self.bar.pack(fill="x")
        self.bar.bind("<Configure>", lambda e: self._draw_bar())
        self.bar.bind("<Button-1>", self._bar_click)
        self.bar.bind("<B1-Motion>", self._bar_click)
        options = ttk.Frame(outer)
        options.pack(fill="x", pady=theme.pad(4, 0))
        self.excerpt_label = ttk.Label(options, text="", style="Muted.TLabel")
        self.excerpt_label.pack(side="left")
        self.trials = tk.StringVar(value=str(prefs.get("abx_trials", abx.TRIALS)))
        self.trials_box = ttk.Combobox(options, textvariable=self.trials, width=4, state="readonly",
                                       values=[str(n) for n in abx.TRIAL_CHOICES])
        self.trials_box.pack(side="right")
        ttk.Label(options, text="Trials", style="Muted.TLabel").pack(side="right", padx=theme.pad(10, 4))
        self.length = tk.StringVar(value=f"{prefs.get('abx_length', abx.LENGTH)} s")
        self.length_box = ttk.Combobox(options, textvariable=self.length, width=5, state="readonly",
                                       values=[f"{n} s" for n in abx.LENGTH_CHOICES])
        self.length_box.pack(side="right")
        self.length_box.bind("<<ComboboxSelected>>", lambda e: self._excerpt_changed())
        self.trials_box.bind("<<ComboboxSelected>>", lambda e: self.prefs.update(abx_trials=int(self.trials.get())))
        ttk.Label(options, text="Length", style="Muted.TLabel").pack(side="right", padx=theme.pad(0, 4))

        self.level_label = ttk.Label(outer, text="", wraplength=theme.px(460), justify="left")
        self.level_label.pack(fill="x", pady=theme.pad(8, 0))
        self.match = tk.BooleanVar(value=True)
        self.match_check = controls.Switch(outer, "Match levels (turn the louder one down)", self.match,
                                           self._gains_changed)
        self.match_check.pack(anchor="w", pady=theme.pad(theme.GAP, 0))

        controls.section(outer, "Listen")
        listen = ttk.Frame(outer)
        listen.pack(fill="x")
        self.play_buttons = {}
        for letter in ("A", "B", "X"):
            button = controls.FlatButton(listen, letter, lambda l=letter: self.hear(l), fit="▶ X",
                                         label=f"Listen to {letter} (key {letter.lower()})")
            button.pack(side="left", padx=theme.pad(0, theme.GAP))
            self.play_buttons[letter] = button
        controls.FlatButton(listen, "■ Stop", self.stop, label="Stop (space)").pack(
            side="left", padx=theme.pad(theme.GAP, theme.GAP))
        controls.FlatButton(listen, "⟲ From the start", self._restart).pack(side="left")
        self.hearing = ttk.Label(outer, text="", style="Muted.TLabel", wraplength=theme.px(460),
                                 justify="left")
        self.hearing.pack(anchor="w", pady=theme.pad(4, 0))

        controls.section(outer, "Test")
        test = ttk.Frame(outer)
        test.pack(fill="x")
        self.start_button = controls.FlatButton(test, "Start the test", self.start_test, kind="primary",
                                                fit="Start the test")
        self.start_button.pack(side="left")
        self.answer_buttons = {g: controls.FlatButton(test, f"X is {g}", lambda g=g: self.answer(g))
                               for g in ("A", "B")}
        for button in self.answer_buttons.values():
            button.pack(side="left", padx=theme.pad(theme.GAP, 0))
        self.trial_label = ttk.Label(outer, text="", style="Title.TLabel")
        self.trial_label.pack(anchor="w", pady=theme.pad(6, 0))
        self.result_label = ttk.Label(outer, text="", wraplength=theme.px(460), justify="left")
        self.result_label.pack(fill="x", pady=theme.pad(4, 0))
        self.note = ttk.Label(outer, text="", style="Muted.TLabel", wraplength=theme.px(460),
                              justify="left")
        self.note.pack(side="bottom", fill="x", pady=theme.pad(8, 0))
        outer.bind("<Configure>", self._rewrap)

        # This window's own keys (either case, so Caps Lock doesn't matter) win over the app's —
        # B and X would otherwise open the PCM stream and this window, Space play the music.
        for letter in "ABX":
            for key in (letter.lower(), letter):
                self.win.bind(key, lambda e, l=letter: self.hear(l) or "break")
        self.win.bind("<space>", lambda e: (self.stop() if self.playing else self.hear(self._last or "A")) or "break")

        self.pair: abx.Pair | None = None
        self.test: abx.AbxTest | None = None
        self.audition: audition.Audition | None = None
        self.log_path: Path | None = None
        self._last: str | None = None      # the letter last asked for
        self._duration = 0.0                # A's length in seconds
        self.start_s = 0.0
        self._loading: threading.Thread | None = None
        self._load: dict = {}               # the latest load: {"result": Pair or error text} once done
        self._cancel = threading.Event()
        self._reload_job = None
        self._message = ""
        if path_a:
            # The path itself: a CUE sheet track's path carries which part of the file it is.
            self._set_file("A", path_a if isinstance(path_a, Path) else Path(path_a), start_s)
        self._refresh()
        self.start_ticking()

    # -- files and the excerpt --------------------------------------------------------------
    def _choose(self, letter: str):
        other = self.rows["B" if letter == "A" else "A"].path
        start = self.rows[letter].path or other
        path = choose_file(self.win, f"Choose {letter}", start.parent if start else None)
        if path is not None:
            self._set_file(letter, path)

    def _set_file(self, letter: str, path: Path, start_s: float | None = None):
        if letter == "A":
            try:
                self._duration = compare.info(path).seconds
            except Exception as exc:  # noqa: BLE001
                self._message = f"Can't read A: {exc}"
                self._refresh()
                return
            length = self._length()
            self.start_s = (start_s if start_s is not None else self._duration / 3) - length / 2
            self.start_s = max(0.0, min(self.start_s, self._duration - length))
        self.rows[letter].set(path)
        self._excerpt_changed()

    def _length(self) -> float:
        try:
            value = float(self.length.get().split()[0])
        except (ValueError, IndexError):
            value = abx.LENGTH
        return max(1.0, min(value, self._duration or value))

    def _excerpt_changed(self):
        """A file or the excerpt moved: decode again, once things settle."""
        if self.test is not None and not self.test.done:
            return
        try:
            self.prefs["abx_length"] = int(float(self.length.get().split()[0]))
        except (ValueError, IndexError):
            pass
        self.start_s = max(0.0, min(self.start_s, self._duration - self._length()))
        self._draw_bar()
        if self._reload_job:
            self.win.after_cancel(self._reload_job)
        self._reload_job = self.win.after(RELOAD_AFTER_MS, self.load)

    def _bar_geometry(self) -> tuple[int, int]:
        pad = theme.px(4)
        return pad, max(pad + 10, self.bar.winfo_width() - pad)

    def _bar_click(self, event):
        if not self._duration or (self.test is not None and not self.test.done):
            return
        left, right = self._bar_geometry()
        centre = (event.x - left) / max(1, right - left) * self._duration
        self.start_s = centre - self._length() / 2
        self._excerpt_changed()

    def _draw_bar(self):
        c = self.bar
        c.delete("all")
        left, right = self._bar_geometry()
        h = c.winfo_height() or theme.px(26)
        mid = h // 2
        c.create_rectangle(left, mid - 3, right, mid + 3, fill=theme.SURFACE, width=0)
        if self._duration:
            length = self._length()
            x0 = left + self.start_s / self._duration * (right - left)
            x1 = left + min(self._duration, self.start_s + length) / self._duration * (right - left)
            c.create_rectangle(x0, 2, max(x1, x0 + 3), h - 2, fill=theme.SELECT, outline=theme.ACCENT)
            self.excerpt_label.configure(
                text=f"{compare._clock(self.start_s)}–{compare._clock(self.start_s + length)} of "
                     f"{compare._clock(self._duration)} (click or drag to move)")
        else:
            self.excerpt_label.configure(text="Choose A to pick the excerpt")

    # -- decoding -----------------------------------------------------------------------------
    def load(self):
        """Decode both excerpts into memory and line them up (on a thread)."""
        self._reload_job = None
        a, b = self.rows["A"].path, self.rows["B"].path
        if a is None or b is None:
            self._refresh()
            return
        self.stop()
        self.audition = None   # the old excerpt is never heard while this one loads (or if it fails)
        self._cancel.set()
        self._cancel = cancel = threading.Event()
        self.pair = None
        self._load = load = {}   # (a load started later has its own: this one's result is then ignored)
        start, length = self.start_s, self._length()

        def work():
            try:
                result = abx.prepare(a, b, start, length, cancel=cancel)
            except InterruptedError:
                return
            except Exception as exc:  # noqa: BLE001 — shown in the window
                result = f"Couldn't load the excerpt: {exc}"
            load["result"] = result

        self._message = "Loading the excerpt…"
        self._loading = threading.Thread(target=work, name="abx-load", daemon=True)
        self._loading.start()
        self._refresh()

    def _take_loaded(self):
        loaded = self._load.pop("result")
        self._loading = None
        if isinstance(loaded, str):
            self._message = loaded
            self._refresh()
            return
        self.pair = loaded
        self._message = ""
        ga, gb = self._gains()
        switcher = audition.Switcher([loaded.a, loaded.b, loaded.a], loaded.rate, gains=[ga, gb, ga])
        if self.audition is not None:
            self.audition.stop()
        self.audition = audition.Audition(switcher, device=self.player.output_device,
                                          exclusive=self.player.exclusive, stream_factory=self._stream_factory,
                                          default_check=self.player.check_default)
        self._refresh()

    def _gains(self) -> tuple[float, float]:
        if self.pair is None or not self.match.get():
            return 1.0, 1.0
        return audition.level_gains(self.pair.difference_db)

    def _gains_changed(self):
        if self.audition is not None and self.pair is not None and (self.test is None or self.test.done):
            ga, gb = self._gains()
            switcher = self.audition.switcher
            switcher.gains = [ga, gb, ga if switcher.slots[2] is self.pair.a else gb]
        self._refresh()

    # -- listening ----------------------------------------------------------------------------
    @property
    def playing(self) -> bool:
        return self.audition is not None and self.audition.playing

    def hear(self, letter: str):
        if (self.audition is None or self.pair is None
                or (letter == "X" and (self.test is None or self.test.done))):
            return
        self._last, self._message = letter, ""
        self.audition.switcher.volume = self.player.volume ** 2
        if not self.playing:
            toolwin.take_sound(self)   # another window's sound stops first
            self.player.suspend()      # the player pauses and lets go of the device while you listen
            try:
                self.audition.play(SLOTS[letter])
            except Exception as exc:  # noqa: BLE001 — a device that won't open is a message, not a crash
                self._message = f"Can't open the sound device ({exc})"
        else:
            self.audition.play(SLOTS[letter])
        self._refresh()

    def stop(self):
        if self.audition is not None:
            self.audition.stop()
        self._refresh()

    def sound_taken(self):
        """Another window started its own sound: this one stops, and says why."""
        if self.playing:
            self.audition.stop()
            self._message = "Stopped: another window is playing"
            self._refresh()

    def _restart(self):
        if self.playing:
            self.audition.switcher.restart()

    # -- the test -----------------------------------------------------------------------------
    def start_test(self):
        if self.pair is None:
            return
        self.test = abx.AbxTest(int(self.trials.get()))
        self.log_path = None
        self._next_x()
        self._refresh()

    def _next_x(self):
        ga, gb = self._gains()
        x = self.test.x
        sound, gain = (self.pair.a, ga) if x == "A" else (self.pair.b, gb)
        self.audition.switcher.assign(SLOTS["X"], sound, gain)

    def answer(self, guess: str):
        if self.test is None or self.test.done:
            return
        self.test.answer(guess)
        if self.test.done:
            self._finish()
        else:
            self._next_x()
        self._refresh()

    def _finish(self):
        output = ""
        if self.audition is not None and self.audition.device_name:
            mode = "bit-perfect (exclusive) mode" if self.audition.exclusive else "shared mode"
            output = f"{self.audition.device_name}, {mode}, volume {self.player.volume:.0%}"
        text = abx.log_text(self.test, self.pair, output=output, matched=self._gains() != (1.0, 1.0))
        try:
            self.log_path = abx.save_log(text)
        except OSError as exc:
            self._message = f"The log couldn't be saved: {exc}"

    # -- showing ------------------------------------------------------------------------------
    def _refresh(self):
        testing = self.test is not None and not self.test.done
        ready = self.pair is not None and self.audition is not None
        for letter, button in self.play_buttons.items():
            usable = ready and (letter != "X" or testing)
            button.state(["!disabled"] if usable else ["disabled"])
            heard = self.playing and self._last == letter
            button.configure(text=f"▶ {letter}" if heard else letter)
        for widget in (self.rows["A"].button, self.rows["B"].button, self.length_box, self.trials_box,
                       self.match_check):
            widget.state(["disabled"] if testing else ["!disabled"])
        self.start_button.state(["!disabled"] if ready and not testing else ["disabled"])
        self.start_button.configure(text="Start again" if self.test is not None and self.test.done
                                    else "Start the test")
        for button in self.answer_buttons.values():
            button.state(["!disabled"] if testing else ["disabled"])

        if self.pair is not None:
            text, fine = abx.describe_levels(self.pair, matched=self.match.get())
            self.level_label.configure(text=text, style="GoodValue.TLabel" if fine else "WarnValue.TLabel")
        else:
            self.level_label.configure(text=self._message or "Choose A and B", style="Muted.TLabel")

        if testing:
            self.trial_label.configure(text=f"Trial {self.test.trial + 1} of {self.test.trials}: which is X?")
            self.result_label.configure(text="Listen to A, B and X as often as you like. The score stays "
                                             "hidden until the end.", style="Muted.TLabel")
        elif self.test is not None:
            verdict, heard = self.test.verdict()
            self.trial_label.configure(text=f"Result: {self.test.score} of {self.test.trials}")
            saved = f"\nSaved: {self.log_path}" if self.log_path else ""
            self.result_label.configure(text=verdict + saved, style="GoodValue.TLabel" if heard else "TLabel")
        else:
            self.trial_label.configure(text="")
            needed = abx.needed(int(self.trials.get()))
            self.result_label.configure(text=f"{self.trials.get()} trials: {needed} or more right beats guessing "
                                             "(p ≤ 0.05).", style="Muted.TLabel")

        if self.playing:
            letter = self._last or "A"
            where = self.audition.device_name
            mode = "bit-perfect mode" if self.audition.exclusive else "shared mode"
            self.hearing.configure(text=f"Hearing {letter} · {where} ({mode}) · volume {self.player.volume:.0%}")
        else:
            self.hearing.configure(text=self._message if self.pair is not None and self._message else "")
        notes = []
        if self.pair is not None:
            notes += self.pair.notes
            if self.pair.lined_up and abs(self.pair.offset_s) > 0:
                notes.append(f"B lined up with A: it runs {self.pair.offset_s * 1000:+.1f} ms from A")
        if self.audition is not None and self.audition.note:
            notes.append(self.audition.note)
        notes.append("Plays on the player's output at its volume; the player pauses while you listen (press play "
                     "there to carry on). Switching dips the sound for 40 ms the same way every time, so it "
                     "never gives the answer away. Not bit-perfect: it fades and matches levels.")
        self.note.configure(text="\n".join(notes))

    def _step(self):
        if "result" in self._load:
            self._take_loaded()
        if self.playing:
            self.audition.switcher.volume = self.player.volume ** 2
            if self.player.status().state is State.PLAYING:   # play was pressed in the player
                self.audition.stop()
                self._message = "Stopped: the player started playing again"
            self._refresh()

    def _rewrap(self, event):
        width = max(200, event.width - theme.px(24))
        for label in (self.level_label, self.result_label, self.note, self.hearing):
            label.configure(wraplength=width)

    # -- window -------------------------------------------------------------------------------
    def _closing(self):
        self._cancel.set()
        if self._reload_job:
            self.win.after_cancel(self._reload_job)
        if self.audition is not None:
            self.audition.stop()
        toolwin.give_back_sound(self)
