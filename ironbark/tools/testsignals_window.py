"""The Test signals window (key T): measure a DAC, an amp or the software path with test signals.

1. Pick a signal (1 kHz tone → THD+N and noise floor; J-test → jitter sidebands; sweep →
   frequency response), its level (−20 dBFS unless you change it) and, for the J-test, 16 or 24
   bits.
2. Pick where it plays (an output) and where it comes back (a recording input — a cable from the
   DAC's or amp's output into it — or "Windows loopback", Windows' own recording of what the
   output plays, which tests the software side only).
3. "Play and measure": a clear "turn your amp down" question first, then the player pauses (it
   lets go of the device, as the ABX test does), the signal loops, a few seconds are recorded and
   measured (testsignals.py), and the sound stops. "Play" keeps it going until Stop, for an
   outside meter.

Sound goes through audition.py (seamless loop: every signal is exactly periodic). The level is the
test's own — the player's volume slider doesn't apply — and it never plays on a device other than
the one chosen (no falling back to the default output). Stop (or the space bar) always works, also
part-way through a measurement. Another window that plays its own sound (ABX, Clipping → Hear it)
stops this one first, and the other way round.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk

import numpy as np

from .. import controls, devices, platforms, popups, theme, toolwin
from ..engine import State
from . import audition, loopback, testsignals

REFRESH_MS = 100
# What the output plays, recorded by the system itself: Windows' loopback, the sound server's
# monitor on Linux. None where there's no such recording (a Mac): then only real inputs are listed.
LOOPBACK = (None if not loopback.AVAILABLE else "Windows loopback (what the output plays)" if devices.IS_WINDOWS
            else "The sound server's monitor (what the output plays)")
RECORD_TIMEOUT_S = 5.0   # past the recording's length: a device that stops sending sound is given up on
WHO = platforms.SOUND_SYSTEM[:1].upper() + platforms.SOUND_SYSTEM[1:]   # "Windows" / "The sound server"
WHOSE = platforms.SOUND_SYSTEMS   # "Windows'" / "the sound server's"
WHOSE_OWN = WHOSE[:1].upper() + WHOSE[1:]   # starting a sentence
KINDS = (("tone", "1 kHz tone (THD+N)"), ("jtest", "J-test (jitter)"), ("sweep", "Sweep (frequency response)"))
TONES = {"good": "GoodValue.TLabel", "warn": "WarnValue.TLabel", "plain": "TLabel"}
PLOT_HEIGHT = 150


def record(input_name: str, output: str | None, rate: int, seconds: float) -> tuple[np.ndarray, int, list[str]]:
    """Record `seconds` from the input (frames × channels, full scale 1.0). Blocks: call it on a
    worker thread. Returns (audio, rate, notes)."""
    if LOOPBACK is not None and input_name == LOOPBACK:
        loopback.thread_ready()
        take = loopback.record(output, seconds)
        if take is None:
            raise RuntimeError(f"{WHO} wouldn't record this output" + (
                "" if devices.IS_WINDOWS else " (choose the sound server's output, not a card's own hw: one, "
                "and check pactl and parec are installed)"))
        notes = [f"{WHO} reported {take.gaps} break(s) in its recording"] if take.gaps else []
        return take.audio, take.rate, notes
    import sounddevice as sd

    index = devices.find_input(input_name)
    if index is None:
        raise RuntimeError(f"{input_name} isn't connected")
    channels = max(1, min(2, int(sd.query_devices(index)["max_input_channels"])))
    extra = sd.WasapiSettings(auto_convert=True) if devices.IS_WINDOWS else None
    # Filled from the stream's callback and waited for with a time limit: a blocking read would
    # wait for good on a device that stops sending sound.
    take = audition.Take(int(round(seconds * rate)), channels)

    def heard(indata, frames, time_info, status):
        take.add(indata, bool(status and status.input_overflow))

    stream = sd.InputStream(device=index, channels=channels, samplerate=rate, dtype="float32",
                            extra_settings=extra, callback=heard)
    devices.holding(stream)   # (a PortAudio re-scan would cut it off: none while it's open)
    try:
        with stream:
            if not take.full.wait(seconds + RECORD_TIMEOUT_S):
                raise RuntimeError(f"{input_name} stopped sending sound ({take.filled / rate:.1f} of "
                                   f"{seconds:.1f} s recorded)")
    finally:
        devices.released(stream)
    notes = ["The recording overflowed (samples lost)"] if take.overflowed else []
    return take.audio.astype(np.float64), rate, notes


def same_device(output: str, input_name: str) -> tuple[int, int] | None:
    """(input index, channels) when Test signals would play on and record from one device — its
    own input, where the platform needs a single duplex stream for that (DUPLEX_SAME_DEVICE). None
    otherwise, or when either isn't connected."""
    if not platforms.DUPLEX_SAME_DEVICE or (LOOPBACK is not None and input_name == LOOPBACK):
        return None
    import sounddevice as sd

    try:   # (a failed look-up means "not the same device", never an error out of measure())
        in_index, out_index = devices.find_input(input_name), devices.find_device(output, rescan_ok=False)
        if in_index is None or out_index is None:
            return None
        out_name = sd.query_devices(out_index)["name"]
        channels = max(1, min(2, int(sd.query_devices(in_index)["max_input_channels"])))
    except (sd.PortAudioError, ValueError, KeyError, TypeError):
        return None
    return (in_index, channels) if in_index == out_index or out_name == input_name else None


def confirm_play(parent, signal: testsignals.TestSignal, output: str) -> bool:
    """The safety question before any sound. (0.21.0) An in-app box, kept as clear as the Windows one:
    modal, alone over a plain background, amber edge and warning sign; Esc = don't play."""
    return popups.confirm(
        parent, "Turn your amp down first",
        "Turn your amplifier or headphone volume right down before you press Play, then bring it up "
        "slowly if you need to. A steady tone — and the sweep's top end — can damage tweeters and "
        "hearing when played loud.",
        detail=f"About to play: {signal.name}\non: {output}\n\nThe player's volume slider doesn't apply "
               "here: the level is the test's own.",
        ok="Play", tone="warn", cover=True)


class TestSignalsWindow(toolwin.ToolWindow):
    TITLE = "Test signals"
    SIZE = (520, 720)
    MIN_SIZE = (400, 560)
    GEOMETRY_KEY = "testsignals_geometry"
    REFRESH_MS = REFRESH_MS

    def __init__(self, parent: tk.Misc, player, prefs: dict, on_close=lambda: None, stream_factory=None,
                 recorder=record, confirm=confirm_play, duplex_factory=None):
        self.player = player
        self._stream_factory, self._recorder, self._confirm = stream_factory, recorder, confirm
        self._duplex_factory = duplex_factory
        super().__init__(parent, prefs, on_close)

        outer = ttk.Frame(self.win, padding=theme.px(theme.GAP_WIDE))
        outer.pack(fill="both", expand=True)
        intro = ("Measure your DAC or amp: play a test signal, record it back, see what was added. A real "
                 "measurement needs a cable from the DAC's (or amp's) output into a recording input")
        if LOOPBACK is not None:
            intro += "; " + ("Windows loopback" if devices.IS_WINDOWS else "the sound server's monitor") + \
                     " checks the software side only."
        else:
            intro += "."
        self.intro = ttk.Label(outer, style="Muted.TLabel", justify="left", wraplength=theme.px(480), text=intro)
        self.intro.pack(fill="x")

        controls.section(outer, "Signal")
        self.kind = tk.StringVar(value=prefs.get("testsignals_kind", "tone"))
        # (0.22.0) A row of words (controls.ChoiceRow) instead of radio buttons.
        self.kind_choice = controls.ChoiceRow(outer, KINDS, self.kind, command=self._options_changed)
        self.kind_choice.pack(anchor="w")
        options = ttk.Frame(outer)
        options.pack(fill="x", pady=theme.pad(4, 0))
        ttk.Label(options, text="Level", style="Muted.TLabel").pack(side="left")
        self.level = tk.StringVar(value=self._level_text(prefs.get("testsignals_level", testsignals.LEVEL_DB)))
        self.level_box = ttk.Combobox(options, textvariable=self.level, width=10, state="readonly",
                                      values=[self._level_text(v) for v in testsignals.LEVEL_CHOICES])
        self.level_box.pack(side="left", padx=theme.pad(4, 12))
        self.bits_label = ttk.Label(options, text="J-test bits", style="Muted.TLabel")
        self.bits_label.pack(side="left")
        self.bits = tk.StringVar(value=str(prefs.get("testsignals_bits", 16)))
        self.bits_box = ttk.Combobox(options, textvariable=self.bits, width=4, state="readonly",
                                     values=[str(b) for b in testsignals.JTEST_BITS])
        self.bits_box.pack(side="left", padx=theme.pad(4, 0))
        for box in (self.level_box, self.bits_box):
            box.bind("<<ComboboxSelected>>", lambda e: self._options_changed())
        self.signal_line = ttk.Label(outer, text="", style="Muted.TLabel", justify="left",
                                     wraplength=theme.px(480))
        self.signal_line.pack(fill="x", pady=theme.pad(4, 0))

        controls.section(outer, "Route")
        route = ttk.Frame(outer)
        route.pack(fill="x")
        route.columnconfigure(1, weight=1)
        ttk.Label(route, text="Play on", style="Muted.TLabel").grid(row=0, column=0, sticky="w", padx=theme.pad(0, 8))
        self.output = tk.StringVar(value=prefs.get("testsignals_output") or player.output_device
                                   or devices.default_output_name() or "")
        self.output_box = ttk.Combobox(route, textvariable=self.output, state="readonly",
                                       values=devices.output_names(), postcommand=self._list_outputs)
        self.output_box.grid(row=0, column=1, sticky="ew", pady=theme.px(2))
        ttk.Label(route, text="Record from", style="Muted.TLabel").grid(row=1, column=0, sticky="w", padx=theme.pad(0, 8))
        inputs = devices.input_names()
        offered = ([LOOPBACK] if LOOPBACK is not None else []) + inputs
        chosen = prefs.get("testsignals_input") or ""
        self.input = tk.StringVar(value=chosen if chosen in offered else (offered[0] if offered else ""))
        self.input_box = ttk.Combobox(route, textvariable=self.input, state="readonly", values=offered)
        self.input_box.grid(row=1, column=1, sticky="ew", pady=theme.px(2))
        for box in (self.output_box, self.input_box):
            box.bind("<<ComboboxSelected>>", lambda e: self._options_changed())
        self.exclusive = tk.BooleanVar(value=bool(getattr(player, "exclusive", False)))
        self.exclusive_check = controls.Switch(outer, "Bit-perfect (exclusive mode)", self.exclusive,
                                               self._options_changed)
        self.exclusive_check.pack(anchor="w", pady=theme.pad(theme.GAP, theme.GAP_SMALL))
        self.route_line = ttk.Label(outer, text="", style="Muted.TLabel", justify="left",
                                    wraplength=theme.px(480))
        self.route_line.pack(fill="x")

        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=theme.pad(10, 0))
        self.measure_button = controls.FlatButton(bar, "Play and measure", self.measure, kind="primary")
        self.measure_button.pack(side="left")
        self.play_button = controls.FlatButton(bar, "Play", self.play_only, label="Play the signal without measuring")
        self.play_button.pack(side="left", padx=theme.pad(theme.GAP, 0))
        self.stop_button = controls.FlatButton(bar, "■ Stop", self.stop)
        self.stop_button.pack(side="left", padx=theme.pad(theme.GAP, 0))
        self.status = ttk.Label(outer, text="", style="Muted.TLabel", justify="left", wraplength=theme.px(480))
        self.status.pack(fill="x", pady=theme.pad(6, 0))

        controls.section(outer, "Result")
        self.results = ttk.Frame(outer)
        self.results.pack(fill="x")
        self.results.columnconfigure(0, minsize=theme.px(84))
        self.results.columnconfigure(1, weight=1)
        self.plot = tk.Canvas(outer, height=theme.px(PLOT_HEIGHT), bg=theme.BG, highlightthickness=0)
        self.plot.pack(fill="x", pady=theme.pad(8, 0))
        self.plot.bind("<Configure>", lambda e: self._draw_plot())
        self.note = ttk.Label(outer, text="", style="Muted.TLabel", justify="left", wraplength=theme.px(480))
        self.note.pack(side="bottom", fill="x", pady=theme.pad(8, 0))
        outer.bind("<Configure>", self._rewrap)

        self.audition: audition.Audition | None = None
        self.signal: testsignals.TestSignal | None = None
        self.measurement: testsignals.Measurement | None = None
        self._result: dict | None = None      # what the measuring thread hands back
        self._message = ""
        self._rates: dict[str, int] = {}      # output name -> its rate (asked once)
        self._options_changed()
        # Space stops the test sound (it never starts one: that's asked first), and only then is it
        # this window's: otherwise it plays / pauses the music, as in every window.
        self.win.bind("<space>", lambda e: (self.stop() or "break") if self.playing or self.measuring else None)
        self.start_ticking()

    # -- options ------------------------------------------------------------------------------
    @staticmethod
    def _level_text(value: float) -> str:
        return f"{float(value):g} dBFS"

    def _level_db(self) -> float:
        try:
            return float(self.level.get().split()[0])
        except (ValueError, IndexError):
            return testsignals.LEVEL_DB

    def _rate(self) -> int:
        """The output's own rate (Windows' mixer rate in shared mode): nothing gets resampled."""
        name = self.output.get()
        if name not in self._rates:
            index = devices.find_device(name or None, rescan_ok=False)   # the player may be playing
            self._rates[name] = devices.mixer_rate(index) or 48000
        return self._rates[name]

    @property
    def loopback(self) -> bool:
        return LOOPBACK is not None and self.input.get() == LOOPBACK

    def _list_outputs(self):
        """The outputs as Windows lists them now: one plugged in since the window opened shows up."""
        self.output_box.configure(values=devices.output_names())

    def _options_changed(self):
        self.prefs.update(testsignals_kind=self.kind.get(), testsignals_level=self._level_db(),
                          testsignals_bits=int(self.bits.get()), testsignals_output=self.output.get(),
                          testsignals_input="" if self.loopback else self.input.get())
        if self.loopback:
            self.exclusive.set(False)      # a device taken in exclusive mode bypasses Windows: nothing to record
        self._refresh()

    def build_signal(self, rate: int | None = None) -> testsignals.TestSignal:
        return testsignals.make(self.kind.get(), rate or self._rate(), self._level_db(), int(self.bits.get()))

    # -- sound --------------------------------------------------------------------------------
    @property
    def playing(self) -> bool:
        return self.audition is not None and self.audition.playing

    @property
    def measuring(self) -> bool:
        return self._result is not None

    def _start_sound(self, measuring: bool = False) -> bool:
        """Ask first, then play the signal on the chosen output. False if it didn't start. Measuring
        from the output's own input where the platform needs it, playing and recording share one
        duplex stream (same_device)."""
        output = self.output.get()
        found = devices.find_device(output, rescan_ok=False) if output else None
        # Plugged in since the player started: Windows lists it, but the sound system hasn't looked
        # for it yet (it looks again only with no sound playing: below, once everything has stopped).
        plugged_in = found is None and bool(output) and output in devices.output_names()
        if found is None and not plugged_in:
            self._message = "Choose an output that's connected — nothing was played"
            self._refresh()
            return False
        try:
            signal = self.build_signal()
        except ValueError as exc:
            self._message = str(exc)
            self._refresh()
            return False
        if not self._confirm(self.win, signal, output):
            self._message = "Not played"
            self._refresh()
            return False
        self.stop()
        toolwin.take_sound(self)  # another window's sound stops first
        self.player.suspend()     # the player pauses and lets go of the device (one sound at a time)
        if plugged_in:
            if devices.find_device(output) is None:   # (now it may look again: nothing else is open)
                self._message = (f"{output} isn't ready for the sound system yet: play some music on it "
                                 "once, or restart the player — nothing was played")
                self._refresh()
                return False
            self._rates.pop(output, None)
            signal = self.build_signal()   # at the device's own rate, now that it can be asked
        self.signal, self.measurement = signal, None
        switcher = audition.Switcher([signal.stereo()], signal.rate, seamless=True)
        duplex = same_device(output, self.input.get()) if measuring else None
        self.audition = audition.Audition(switcher, device=output, exclusive=self.exclusive.get(),
                                          stream_factory=self._stream_factory, record_input=duplex,
                                          duplex_factory=self._duplex_factory)
        try:
            self.audition.play(0)
        except Exception as exc:  # noqa: BLE001 — a device that won't open is a message, not a crash
            self.audition = None
            self._message = f"Can't open the sound device ({exc})"
            self._refresh()
            return False
        self._message = ""
        return True

    def play_only(self):
        if self._start_sound():
            self._refresh()

    def measure(self):
        if self.measuring or not self._start_sound(measuring=True):
            return
        signal, output, source = self.signal, self.output.get(), self.input.get()
        result: dict = {}
        self._result = result
        # The thread holds the result and the recorder, never the window (see toolwin.Handback).
        recorder = self._recorder
        duplex = self.audition if self.audition.records else None   # (an Audition: no Tk in it)

        def work():
            try:
                if duplex is not None:   # (one duplex stream plays and records: same_device)
                    audio, overflowed = duplex.recording(signal.seconds_needed(),
                                                                signal.seconds_needed() + RECORD_TIMEOUT_S)
                    rate, notes = signal.rate, ["The recording overflowed (samples lost)"] if overflowed else []
                else:
                    audio, rate, notes = recorder(source, output, signal.rate, signal.seconds_needed())
                measurement = testsignals.measure(signal, audio, rate)
                measurement.notes.extend(notes)
                result["measurement"] = measurement
            except Exception as exc:  # noqa: BLE001 — shown in the window
                result["error"] = f"The recording failed: {exc}"
            finally:
                result["done"] = True

        threading.Thread(target=work, name="test-signals", daemon=True).start()
        self._refresh()

    def stop(self):
        if self.measuring:   # stopped part-way: the recording still running is dropped when it ends
            self._result = None
            self._message = "Stopped — no measurement"
        if self.audition is not None:
            self.audition.stop()
        self._refresh()

    def sound_taken(self):
        """Another window started its own sound: this one stops, and says why."""
        if self.playing:
            measuring, self._result = self.measuring, None
            self.audition.stop()
            self._message = "Stopped: another window is playing" + (" — no measurement" if measuring else "")
            self._refresh()

    def _take_result(self):
        result, self._result = self._result, None
        self.stop()
        if "error" in result:
            self._message = result["error"]
        else:
            self.measurement = result["measurement"]
            self._message = ""
        self._show_results()
        self._refresh()

    # -- showing ------------------------------------------------------------------------------
    def _show_results(self):
        for widget in self.results.winfo_children():
            widget.destroy()
        if self.measurement is None:
            self._draw_plot()
            return
        wrap = max(200, self.results.winfo_width() - theme.px(96))
        for row, (label, text, tone) in enumerate(testsignals.describe(self.measurement)):
            ttk.Label(self.results, text=label, style="Muted.TLabel").grid(row=row, column=0, sticky="nw", pady=theme.px(1))
            ttk.Label(self.results, text=text, style=TONES[tone], wraplength=wrap, justify="left").grid(
                row=row, column=1, sticky="w", pady=theme.px(1))
        self._draw_plot()

    def _draw_plot(self):
        c = self.plot
        c.delete("all")
        points = testsignals.plot_points(self.measurement) if self.measurement is not None else None
        if points is None:
            return
        freqs, levels, log_scale = points
        w, h = max(50, c.winfo_width()), max(40, c.winfo_height())
        pad_l, pad_r, pad_t, pad_b = theme.px(40), theme.px(8), theme.px(6), theme.px(16)
        f0, f1 = float(freqs[0]), float(freqs[-1])
        response = self.measurement.signal.kind == "sweep"
        if response:
            low = min(-3.0, float(np.floor(np.min(levels))))
            top, bottom, step = max(1.0, float(np.ceil(np.max(levels)))), max(low, -40.0), 1.0 if low >= -3 else 10.0
        else:
            top, bottom, step = 0.0, -180.0, 20.0

        def x_of(f):
            share = (np.log(f / f0) / np.log(f1 / f0)) if log_scale else (f - f0) / (f1 - f0)
            return pad_l + share * (w - pad_l - pad_r)

        def y_of(v):
            return pad_t + (top - np.clip(v, bottom, top)) / (top - bottom) * (h - pad_t - pad_b)

        font = theme.font(theme.SIZE_TINY)
        value = top
        while value >= bottom - 1e-9:
            y = y_of(value)
            c.create_line(pad_l, y, w - pad_r, y, fill=theme.BORDER)
            c.create_text(pad_l - 4, y, text=f"{value:g}", anchor="e", fill=theme.MUTED, font=font)
            value -= step
        marks = [20, 100, 1000, 10000, 20000] if log_scale else np.linspace(f0, f1, 5)
        for f in marks:
            if f0 <= f <= f1:
                x = x_of(f)
                c.create_line(x, pad_t, x, h - pad_b, fill=theme.BORDER)
                c.create_text(x, h - pad_b + 2, text=f"{f / 1000:.3g}k" if f >= 1000 else f"{f:g}", anchor="n",
                              fill=theme.MUTED, font=font)
        coords = np.column_stack([x_of(np.asarray(freqs, dtype=float)), y_of(np.asarray(levels, dtype=float))])
        if len(coords) > 1:
            c.create_line(*coords.ravel().tolist(), fill=theme.ACCENT, width=1)
        unit = "dB against 1 kHz" if response else "dBFS"
        c.create_text(w - pad_r, pad_t, text=f"Left channel · {unit}", anchor="ne", fill=theme.MUTED, font=font)

    def _refresh(self):
        jtest = self.kind.get() == "jtest"
        busy = self.playing or self.measuring
        for widget in (self.level_box, self.bits_box, self.output_box, self.input_box):
            usable = not busy and (widget is not self.bits_box or jtest)
            widget.state(["!disabled"] if usable else ["disabled"])
        self.exclusive_check.state(["disabled"] if busy or self.loopback else ["!disabled"])
        self.measure_button.state(["disabled"] if busy else ["!disabled"])
        self.play_button.state(["disabled"] if busy else ["!disabled"])
        self.stop_button.state(["!disabled"] if self.playing else ["disabled"])   # (measuring too)

        try:
            preview = self.build_signal()
            text = preview.name
            if jtest and abs(preview.level_db - self._level_db()) > 0.05:
                text += (f" — the J-test's level goes in 6 dB steps (so every bit still flips), "
                         f"{preview.level_db:.2f} dBFS is the nearest at or below {self._level_db():g}")
            self.signal_line.configure(text=text, style="Muted.TLabel")
        except ValueError as exc:
            self.signal_line.configure(text=str(exc), style="WarnValue.TLabel")
        if self.loopback:
            route = (f"{WHOSE_OWN} own recording of the output: shared mode only, so this measures the player "
                     f"and {WHOSE} mixer — not the DAC.")
        else:
            route = ("Connect the DAC's or amp's output to this input (mind the levels: a speaker output needs "
                     "an attenuator). Measures everything in the chain, the input included.")
        self.route_line.configure(text=route)

        if self.measuring:
            where = self.audition.device_name if self.audition else self.output.get()
            mode = "bit-perfect mode" if self.audition and self.audition.exclusive else "shared mode"
            self.status.configure(text=f"Playing on {where} ({mode}) and recording for "
                                       f"{self.signal.seconds_needed():.0f} s…", style="TLabel")
        elif self.playing:
            mode = "bit-perfect mode" if self.audition.exclusive else "shared mode"
            self.status.configure(text=f"Playing {self.signal.name} on {self.audition.device_name} ({mode}). "
                                       "Stop when done.", style="TLabel")
        else:
            self.status.configure(text=self._message, style="WarnValue.TLabel" if self._message else "Muted.TLabel")
        notes = []
        if self.audition is not None and self.audition.note:
            notes.append(self.audition.note)
        notes.append("Safety: the level is the test's own (−20 dBFS unless you change it) and the player's volume "
                     "doesn't apply. You're asked to turn the amp down before anything plays. The player pauses "
                     "while a test plays.")
        self.note.configure(text="\n".join(notes))

    def _step(self):
        if self._result is not None and self._result.get("done"):
            self._take_result()
        elif self.playing and self.player.status().state is State.PLAYING:   # play was pressed in the player
            self._result = None
            self.stop()
            self._message = "Stopped: the player started playing again"
            self._refresh()

    def _rewrap(self, event):
        width = max(200, event.width - theme.px(24))
        for label in (self.intro, self.signal_line, self.route_line, self.status, self.note):
            label.configure(wraplength=width)

    # -- window -------------------------------------------------------------------------------
    def _closing(self):
        self._result = None
        if self.audition is not None:
            self.audition.stop()
        toolwin.give_back_sound(self)
        # Its Tk variables go here, on Tk's thread (the choice row and switch let go of theirs too).
        self.kind = self.level = self.bits = self.output = self.input = self.exclusive = None
