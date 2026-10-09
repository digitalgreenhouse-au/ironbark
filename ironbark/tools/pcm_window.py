"""The PCM stream window: the actual sample values being played, as they're heard.

Two views of each sample:
- "In the file": the decoded value at the file's own size — a 16-bit file's samples are
  whole numbers from -32,768 to 32,767, a 24-bit file's from -8,388,608 to 8,388,607
  (two's complement, as stored).
- "To the driver": the exact 32-bit word handed to the sound driver — in bit-perfect mode
  the file's value shifted up into a 32-bit integer (e.g. 24-bit 0x123456 -> 0x12345600),
  in shared mode a 32-bit floating-point number (IEEE 754).

A bitscope strip along the top shows, per channel, how often each bit is 1 (see
spectrum.BitScope): the bits a recording really uses, at a glance.

Shown in decimal, hex or binary. A stereo stream is 88,200+ numbers a second, far too many
to read, so each refresh pushes a few consecutive samples from the moment being heard onto
the top of a stack (newest on top, older ones moving down), with a dim line saying how many
went past unshown in between. Freeze holds one whole block (about 93 ms) to scroll through.
The stack is rebuilt only when something changed (paused: left alone, so a value can be
selected and copied), and nothing is drawn while the window is minimised.
"""

from __future__ import annotations

import collections
import queue
import struct
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

import numpy as np

from .. import controls, theme, toolwin
from . import spectrum
from .details import describe_bits

REFRESH_MS = 120
GROUP_ROWS = 4      # consecutive samples added to the top of the stack each refresh
HISTORY_GROUPS = 40  # refreshes kept: about the last 5 seconds, 160 rows
SCOPE_HEIGHT = 66    # the bitscope strip, px at 100% (two channel rows + the bit numbers)


def to_file_value(source: np.ndarray, bits: int | None) -> np.ndarray:
    """The file's own integer values from a decoded block (int32 left-justified, or float)."""
    if bits is None:
        return source
    if source.dtype == np.int32:
        return source.astype(np.int64) >> (32 - bits)
    return np.rint(source.astype(np.float64) * 2 ** (bits - 1)).astype(np.int64)


def fmt_int(value: int, bits: int, base: str) -> str:
    """A signed integer of `bits` bits, as decimal, or its two's-complement hex / binary."""
    if base == "Decimal":
        width = len(str(-(2 ** (bits - 1))))
        return f"{value:>{width},}" if bits > 8 else f"{value:>{width}}"
    pattern = value & ((1 << bits) - 1)
    if base == "Hex":
        return "0x" + f"{pattern:0{bits // 4}X}"
    text = f"{pattern:0{bits}b}"
    return " ".join(text[i:i + 8] for i in range(0, bits, 8))  # in bytes, as stored


def fmt_float(value: float, base: str) -> str:
    """A 32-bit float as a number, or its IEEE 754 bits."""
    if base == "Decimal":
        return f"{value:>+.9f}"
    pattern = struct.unpack("<I", struct.pack("<f", float(value)))[0]
    if base == "Hex":
        return f"0x{pattern:08X}"
    text = f"{pattern:032b}"
    return f"{text[0]} {text[1:9]} {text[9:]}"  # sign · exponent · fraction


def fmt_time(frame: int, rate: int) -> str:
    seconds = frame / rate
    return f"{int(seconds // 60)}:{seconds % 60:09.6f}"


class BitCounter:
    """Counts the bits of each block once it's heard, on a thread of its own (a hi-res block takes
    milliseconds, too long for the window's thread). The strip is drawn from what has been counted
    so far: read `scope`, `block` and `news` under `lock`. Holds nothing of Tk's, so its thread
    never keeps a closed window alive."""

    def __init__(self):
        self.scope = spectrum.BitScope()
        self.lock = threading.Lock()
        self.blocks: queue.Queue = queue.Queue()   # blocks to count, in order
        self.block = None    # the newest block counted (its bits, channels, path)
        self.news = True     # something counted that the strip doesn't show yet
        self._stop = threading.Event()
        threading.Thread(target=self._run, name="bitscope", daemon=True).start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                block = self.blocks.get(timeout=0.2)
            except queue.Empty:
                continue
            wait = block["dac_time"] - time.perf_counter()
            if wait > 0 and self._stop.wait(wait):
                return
            bits = block["bits"]
            values = to_file_value(block["source"], bits) if bits else None
            with self.lock:
                if bits:
                    self.scope.add(values, bits, block["rate"], block["path"])
                if bits or self.block is None or self.block["bits"] != bits:
                    self.news = True
                self.block = block


class PcmWindow(toolwin.ToolWindow):
    TITLE = "PCM stream"
    SIZE = (1040, 740)
    MIN_SIZE = (560, 360)
    GEOMETRY_KEY = "pcm_geometry"
    REFRESH_MS = REFRESH_MS

    def __init__(self, parent: tk.Misc, player, prefs: dict, on_close=lambda: None,
                 analysis_for=lambda path: None):
        """analysis_for(path) -> the Now Playing check's Analysis for that file, or None."""
        self.player = player
        self.analysis_for = analysis_for
        super().__init__(parent, prefs, on_close)

        bar = ttk.Frame(self.win, padding=theme.pad(10, 8, 10, 4))
        bar.pack(fill="x")
        # (0.22.0) The choices as rows of words (controls.ChoiceRow) instead of radio buttons, a faint
        # line between the two, and a flat Freeze button that keeps its width as it says Resume.
        self.base = tk.StringVar(value=prefs.get("pcm_base", "Decimal"))
        self.base_choice = controls.ChoiceRow(bar, [(name, name) for name in ("Decimal", "Hex", "Binary")],
                                              self.base, command=self._repaint)
        self.base_choice.pack(side="left")
        tk.Frame(bar, bg=theme.BORDER, width=1).pack(side="left", fill="y", padx=theme.px(theme.GAP_LARGE),
                                                     pady=theme.px(theme.GAP_SMALL))
        self.view = tk.StringVar(value=prefs.get("pcm_view", "file"))
        self.view_choice = controls.ChoiceRow(bar, [("file", "In the file"), ("driver", "To the driver (32-bit)")],
                                              self.view, command=self._repaint)
        self.view_choice.pack(side="left")
        self.freeze_button = controls.FlatButton(bar, "Freeze", self._toggle_freeze, fit="Resume",
                                                 label="Hold one whole block still to read it")
        self.freeze_button.pack(side="right")

        # The bitscope strip: one bar per bit, per channel.
        scope = ttk.Frame(self.win, padding=theme.pad(10, 2, 10, 4))
        scope.pack(fill="x")
        self.scope_canvas = tk.Canvas(scope, height=theme.px(SCOPE_HEIGHT), bg=theme.BG,
                                      highlightthickness=0)
        self.scope_canvas.pack(fill="x")
        self.scope_canvas.bind("<Configure>", lambda e: self._paint_scope())
        self.scope_info = ttk.Label(scope, style="Muted.TLabel", justify="left")
        self.scope_info.pack(fill="x")
        self.scope_info.bind("<Configure>",
                             lambda e: self.scope_info.configure(wraplength=max(200, e.width - 8)))
        self.counter = BitCounter()
        self.scope = self.counter.scope

        self.info = ttk.Label(self.win, style="Muted.TLabel", padding=theme.pad(10, 0, 10, 6), justify="left")
        self.info.pack(fill="x")
        self.info.bind("<Configure>", lambda e: self.info.configure(wraplength=max(200, e.width - 24)))
        mono = tkfont.Font(family=theme.MONO, size=theme.SIZE_MONO)
        # The column headings stay put above the stack (their own one-line strip, scrolled
        # sideways together with it).
        self.header = tk.Text(self.win, font=mono, bg=theme.SURFACE, fg=theme.ACCENT_TEXT, relief="flat", wrap="none",
                              height=1, padx=theme.px(8), pady=theme.px(4), highlightthickness=0, cursor="arrow")
        # In line with the text below, which has the slim scrollbar on its right.
        self.header.pack(fill="x", padx=(theme.px(10), theme.px(10) + max(4, theme.px(theme.SCROLLBAR_WIDTH))))
        self.header.tag_configure("head", foreground=theme.ACCENT_TEXT)
        body = ttk.Frame(self.win, padding=theme.pad(10, 0, 10, 10))
        body.pack(fill="both", expand=True)
        self.text = tk.Text(body, font=mono, bg=theme.SURFACE, fg=theme.TEXT, insertbackground=theme.TEXT,
                            relief="flat", wrap="none", padx=theme.px(8), pady=theme.px(6), highlightthickness=0)
        # (0.22.0) Slim scrollbars: down always there (the header lines up with it), across only
        # when the rows are wider than the window (binary in a narrow window).
        scroll = controls.AutoScrollbar(body, command=self.text.yview)
        across = controls.AutoScrollbar(body, orient="horizontal",
                                        command=lambda *a: (self.text.xview(*a), self.header.xview(*a)))
        self.text.configure(yscrollcommand=scroll.set,
                            xscrollcommand=lambda first, last: (across.set(first, last),
                                                                self.header.xview_moveto(first)))
        across.auto_pack(side="bottom", fill="x", pady=(theme.px(theme.GAP_HAIR), 0), before=scroll)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("dim", foreground=theme.MUTED)
        self.text.tag_configure("gap", foreground=theme.MUTED, font=theme.font(theme.SIZE_SMALL))
        self.text.configure(state="disabled")
        self.header.configure(state="disabled")

        self.feed = player.monitor()
        self.blocks: collections.deque = collections.deque(maxlen=48)
        self.history: collections.deque = collections.deque(maxlen=HISTORY_GROUPS)  # (block, start, count)
        self.frozen: dict | None = None
        self._idle = True          # nothing new heard (paused or stopped)
        self._painted = None       # what the live stack on show was made from (see _paint_live)
        self.start_ticking()

    # -- data ------------------------------------------------------------------
    def _drain(self):
        try:
            while True:
                block = self.feed.get_nowait()
                self.blocks.append(block)
                self.counter.blocks.put(block)
        except queue.Empty:
            pass

    def _current(self):
        """The block being heard right now, and the index of the latest sample heard in it."""
        now = time.perf_counter()
        heard = [b for b in self.blocks if b["dac_time"] <= now]
        if not heard:
            return None, 0
        block = heard[-1]
        offset = int((now - block["dac_time"]) * block["rate"])
        if offset >= len(block["sent"]) + block["rate"] // 2:
            return None, 0  # nothing new for half a second: stopped or paused
        return block, min(offset, len(block["sent"]) - 1)

    def _step(self):
        self._drain()
        shown = toolwin.on_screen(self.win)   # minimised: the data is kept, nothing is drawn
        if shown and self.counter.news:
            self.counter.news = False
            self._paint_scope()
        if self.frozen is None:
            block, latest = self._current()
            if block is not None:
                start = max(0, latest - GROUP_ROWS + 1)
                group = (block, start, latest - start + 1)
                last = self.history[-1] if self.history else None
                if last is None or last[0] is not block or last[1] != start:  # (paused: nothing new)
                    self.history.append(group)
            self._idle = block is None
            if not shown:
                return
            if self.text.yview()[0] > 0.001:
                text = "Scrolled down: live updates wait until you're back at the top."
                if self.info.cget("text") != text:
                    self.info.configure(text=text)
                self._painted = None   # (back at the top: shown afresh)
            else:
                self._paint_live(self._idle)

    # -- display -----------------------------------------------------------------
    def _toggle_freeze(self):
        if self.frozen is None:
            block, _ = self._current()
            if block is None:
                return
            self.frozen = block
            self.freeze_button.configure(text="Resume")
            self._paint_frozen()
        else:
            self.frozen = None
            self.freeze_button.configure(text="Freeze")
            self.text.yview_moveto(0)

    def _repaint(self):
        self.prefs["pcm_base"], self.prefs["pcm_view"] = self.base.get(), self.view.get()
        if self.frozen is not None:
            self._paint_frozen()
        else:
            self._paint_live(self._idle)

    def _formatter(self, block):
        """(values, cell(value) -> text, description) for a block in the chosen view and base."""
        base, bits, sent = self.base.get(), block["bits"], block["sent"]
        if self.view.get() == "file":
            values = to_file_value(block["source"], bits)
            if bits:
                kind = (f"{bits}-bit signed integers, as decoded from the file (range {-(2 ** (bits - 1)):,} to "
                        f"{2 ** (bits - 1) - 1:,}; hex and binary show the two's-complement bits)")
                return values, lambda v: fmt_int(int(v), bits, base), kind
            return values, lambda v: fmt_float(float(v), base), \
                "floating-point values, as decoded (this file has no integer sample size)"
        if sent.dtype == np.int32:
            kind = ("the 32-bit integers handed to the driver (bit-perfect mode: the file's value in the top "
                    f"{bits or 32} bits, the rest zero — unless the volume is below 100%)")
            return sent, lambda v: fmt_int(int(v), 32, base), kind
        return sent, lambda v: fmt_float(float(v), base), \
            "the 32-bit floating-point numbers handed to Windows' mixer (shared mode; binary: sign · exponent · fraction)"

    def _rows(self, block, start, count):
        """Formatted rows for block[start:start+count], newest (highest frame) first."""
        values, cell, _ = self._formatter(block)
        rate = block["rate"]
        return [(block["frame"] + k, fmt_time(block["frame"] + k, rate),
                 [cell(values[k, c]) for c in range(values.shape[1])])
                for k in range(start + count - 1, start - 1, -1)]

    def _paint_live(self, idle: bool):
        """The stack: the newest samples on top, older ones pushed down; a dim line marks the
        samples that went past between refreshes without being shown. Left as it is when nothing
        changed since it was made."""
        newest = self.history[-1] if self.history else (None, None, 0)
        key = (id(newest[0]), newest[1], len(self.history), self.base.get(), self.view.get(), idle)
        if key == self._painted:
            return
        self._painted = key
        if not self.history:
            self._show([], "Nothing playing. Start the music and the numbers appear here as they're heard, "
                           "newest on top.")
            return
        newest = self.history[-1][0]
        rate, channels = newest["rate"], newest["sent"].shape[1]
        _, _, kind = self._formatter(newest)
        lines = []
        newer = None  # (block, lowest frame shown) of the group above
        for block, start, count in reversed(self.history):
            first = block["frame"] + start
            if newer is not None:
                above_block, above_first = newer
                if above_block["track"] != block["track"]:
                    lines.append(("gap", f"⋯ new track: {above_block['track']} ⋯"))
                else:
                    skipped = above_first - (first + count)
                    lines.append(("gap", f"⋯ {skipped:,} samples not shown ⋯" if skipped >= 0
                                  else "⋯ jumped (a seek) ⋯"))
            lines += [("row", row) for row in self._rows(block, start, count)]
            newer = (block, first)
        state = "Paused or stopped — the last samples heard are below. " if idle else ""
        offset = newest.get("offset") or 0  # a CUE sheet track: sample numbers count from its start
        where = f" (its sample 0 is sample {offset:,} of the file)" if offset else ""
        self._show(lines, f"{state}{newest['track']}{where} · {rate:,} Hz · Live, newest on top: "
                          f"{GROUP_ROWS} consecutive samples each refresh, out of {rate * channels:,} numbers a "
                          f"second. Showing {kind}.")

    def _paint_frozen(self):
        self._painted = None   # (the live stack is shown afresh on Resume)
        block = self.frozen
        _, _, kind = self._formatter(block)
        rate, n = block["rate"], len(block["sent"])
        lines = [("row", row) for row in self._rows(block, 0, n)]
        self._show(lines, f"{block['track']} · {rate:,} Hz · Frozen: all {n:,} samples of one "
                          f"{n / rate * 1000:.0f} ms block, newest on top. Showing {kind}.")

    def _show(self, lines, info: str):
        self.info.configure(text=info)
        rows = [row for kind, row in lines if kind == "row"]
        # Older rows may be from a track with fewer channels (mono, then stereo): each column is
        # as wide as the rows that have it.
        channels = max((len(r[2]) for r in rows), default=2)
        widths = [max([len(r[2][c]) for r in rows if c < len(r[2])] + [5]) for c in range(channels)]
        names = ["Left", "Right"] if channels == 2 else [f"Ch {i + 1}" for i in range(channels)]
        head = f"{'Frame':>10}  {'Time':>12}  " + "  ".join(f"{n:>{w}}" for n, w in zip(names, widths))
        for widget, content in ((self.header, [("head", head)]), (self.text, lines)):
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            if widget is self.header:
                widget.insert("end", head, "head")
            else:
                for kind, item in content:
                    if kind == "gap":
                        widget.insert("end", f"{'':>26}{item}\n", "gap")
                    else:
                        frame, clock, cells = item
                        widget.insert("end", f"{frame:>10,}  {clock:>12}  ", "dim")
                        widget.insert("end", "  ".join(f"{c:>{w}}" for c, w in zip(cells, widths)) + "\n")
            widget.configure(state="disabled")

    # -- bitscope ------------------------------------------------------------------
    def _paint_scope(self):
        """One row per channel; one slot per bit, the sign first, then the size of the sample from
        its loudest bit to its quietest. Bar height = how often that bit was 1 over the last
        second; a short grey stub = used earlier in this track but not lately; empty = never used."""
        c = self.scope_canvas
        c.delete("all")
        with self.counter.lock:   # (counted on its own thread: take what's there now, all together)
            block = self.counter.block
            shares, key = self.scope.shares(), self.scope.key
            ever = (None if self.scope.ever is None
                    else np.concatenate([self.scope.ever_negative[:, None], self.scope.ever], axis=1))
            used = self.scope.bits_in_use()
        if block is None:
            self.scope_info.configure(text="Bitscope: nothing heard yet.")
            return
        bits = block["bits"]
        if not bits:
            self.scope_info.configure(text="Bitscope: this file has floating-point samples, so no fixed bits to show.")
            return
        if shares is None or key is None or key[1] != bits or ever is None:
            return
        negative, size = shares
        channels = size.shape[0]
        share = np.concatenate([negative[:, None], size], axis=1)      # channels × bits, sign first
        w = max(100, c.winfo_width())
        small = theme.font(theme.SIZE_SMALL)
        left, label_h = theme.px(26), theme.px(13)
        row_h = (theme.px(SCOPE_HEIGHT) - label_h) / channels
        slot = (w - left) / bits
        names = ["L", "R"] if channels == 2 else [f"{k + 1}" for k in range(channels)]
        stub = max(2, theme.px(2))
        for ch in range(channels):
            top, bottom = ch * row_h + 1, (ch + 1) * row_h - 2
            c.create_text(left - 6, (top + bottom) / 2, text=names[ch], anchor="e", fill=theme.MUTED, font=small)
            for b in range(bits):
                x0, x1 = left + b * slot + 1, left + (b + 1) * slot - 1
                c.create_rectangle(x0, top, x1, bottom, fill=theme.SURFACE, outline="")
                if share[ch, b] > 0:
                    height = max(stub, share[ch, b] * (bottom - top))
                    c.create_rectangle(x0, bottom - height, x1, bottom, fill=theme.ACCENT, outline="")
                elif ever[ch, b]:
                    c.create_rectangle(x0, bottom - stub, x1, bottom, fill=theme.MUTED, outline="")
        every = 1 if slot >= 16 * theme.scale() else 4
        y = channels * row_h + 1
        for b in range(bits):
            number = b + 1  # counted from the top: 1 = the sign
            if b == 0 or number % every == 0 or number == bits:
                c.create_text(left + (b + 0.5) * slot, y, text="±" if b == 0 else str(number), anchor="n",
                              fill=theme.MUTED, font=small)
        track = f"Since this track began: {used} of {bits} bits used. " if used else ""
        result = self.analysis_for(block["path"])
        whole = f"Whole file: {describe_bits(result)}" if result is not None and describe_bits(result) else             "Whole file: not checked yet (Now Playing → Check)."
        self.scope_info.configure(text=f"Bitscope · bar = how often each bit is 1 over the last second (± = the sign, "
                                       f"then each sample's size, loudest bit first; empty = never used). {track}{whole}")

    def _closing(self):
        self.counter.stop()
        self.player.unmonitor(self.feed)
