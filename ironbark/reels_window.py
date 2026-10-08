"""The Tape reels window (Experimental, key W): a reel-to-reel deck playing the song you hear.

The physics is in reels.py; this file only draws. The reels follow the player's position: the
album playing is the tape, the supply reel empties onto the take-up reel as it plays, and each
reel turns at its own true speed (its rpm shown beside it). A seek spools the tape there.

The deck is drawn side by side when the window is wide and stacked (turned a quarter turn) when
it's tall, so it suits the side player's shape; F11 or a double-click fills the screen, Esc
comes back. Drawing pauses while the window is minimised.
"""

from __future__ import annotations

import math
import time
import tkinter as tk
from tkinter import ttk

from . import controls, placement, reels, theme, toolwin
from .engine import State

EXPERIMENT = "tape_reels"

REFRESH_MS = 20          # ~50 frames a second: the reels' windows move smoothly at 100 rpm
STATUS_EVERY_S = 0.1     # how often the player is asked where it is (in between, the clock runs on)
SPEED_CHOICES = tuple(reels.SPEED_NAMES.items())
REEL_CHOICES = (("7", "7-inch reels"), ("10.5", "10½-inch"))

# Things on a deck keep their own colours in either theme (theme.py's REEL_ colours).
FLANGE, FLANGE_RIM, TAPE = theme.REEL_FLANGE, theme.REEL_FLANGE_RIM, theme.REEL_TAPE
HUB, HUB_EAR, HEAD = theme.REEL_HUB, theme.REEL_HUB_EAR, theme.REEL_HEAD
CAPSTAN, RUBBER = theme.REEL_CAPSTAN, theme.REEL_RUBBER

WINDOWS = 3              # the cut-outs in each flange, through which the tape pack shows
WINDOW_SPAN = math.radians(72)
ARC_POINTS = 14          # points along each curved edge
OPEN = WINDOWS * WINDOW_SPAN / (2 * math.pi)   # how much of a turn is cut-out (what a blur lets through)
BLUR_STEP = 0.6          # radians a frame: a reel turning faster than this is drawn blurred


def _sector(cx, cy, inner, outer, start, span, n=ARC_POINTS):
    """An annular sector's outline (deck coordinates): the outer arc out, the inner arc back."""
    pts = []
    for i in range(n + 1):
        a = start + span * i / n
        pts.append((cx + outer * math.cos(a), cy + outer * math.sin(a)))
    for i in range(n, -1, -1):
        a = start + span * i / n
        pts.append((cx + inner * math.cos(a), cy + inner * math.sin(a)))
    return pts


def tangent_point(cx, cy, r, px, py, right: bool):
    """Where a straight tape from (px, py) touches the circle: on its right-hand side (larger x)
    when `right`, else its left."""
    dx, dy = px - cx, py - cy
    d = math.hypot(dx, dy)
    if d <= r:
        return px, py
    base, spread = math.atan2(dy, dx), math.acos(r / d)
    points = [(cx + r * math.cos(base + s), cy + r * math.sin(base + s)) for s in (spread, -spread)]
    return max(points, key=lambda p: p[0]) if right else min(points, key=lambda p: p[0])


def describe_rpm(value: float) -> str:
    value = abs(value)
    return theme.figures(f"{value:,.0f} rpm" if value >= 100 else f"{value:.1f} rpm")


def describe_seconds(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


class ReelsWindow:
    def __init__(self, parent: tk.Misc, player, prefs: dict, on_close=lambda: None):
        self.player, self.prefs, self.on_close = player, prefs, on_close
        self.win = tk.Toplevel(parent, bg=theme.BG)
        self.win.title("Tape reels")
        self.win.geometry(f"{theme.px(420)}x{theme.px(640)}")
        self.win.minsize(theme.px(260), theme.px(320))
        placement.restore(self.win, prefs.get("reels_geometry", ""))
        theme.dark_title_bar(self.win)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<Escape>", self._escape)
        self.win.bind("<F11>", lambda e: self.toggle_full_screen())

        self.bar = ttk.Frame(self.win, padding=theme.pad(10, 8, 10, 4))
        self.bar.pack(fill="x")
        speed = prefs.get("reels_speed", "15")
        self.speed = tk.StringVar(value=speed if speed in reels.SPEEDS else "15")
        controls.ChoiceRow(self.bar, SPEED_CHOICES, self.speed, command=self._choice_changed).pack(side="left")
        size = prefs.get("reels_size", "10.5")
        self.size = tk.StringVar(value=size if size in reels.REELS else "10.5")
        controls.ChoiceRow(self.bar, REEL_CHOICES, self.size, command=self._choice_changed).pack(side="right")

        self.note = ttk.Label(self.win, style="Muted.TLabel", padding=theme.pad(10, 0, 10, 8),
                              text="Each reel turns as fast as the tape needs: the tape speed ÷ its pack's "
                                   "circumference. F11 or double-click: full screen.")
        self.note.pack(side="bottom", fill="x")
        self.note.bind("<Configure>", lambda e: self.note.configure(wraplength=max(200, e.width - 24)))
        self.info = tk.Label(self.win, text="", bg=theme.BG, fg=theme.TEXT, font=theme.numbers(theme.SIZE_BODY),
                             anchor="w", justify="left", padx=theme.px(theme.GAP_LARGE))
        self.info.pack(side="bottom", fill="x")
        self.info.bind("<Configure>", lambda e: self.info.configure(wraplength=max(200, e.width - 24)))
        self.canvas = tk.Canvas(self.win, bg=theme.BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=theme.px(10), pady=theme.pad(4, 6))
        self.canvas.bind("<Configure>", lambda e: self._layout())
        self.canvas.bind("<Double-Button-1>", lambda e: self.toggle_full_screen())

        self.transport = reels.Transport()
        self._full_screen = False
        self._items: dict = {}
        self._frame = None        # (scale, origin x, origin y, stacked) of the drawing
        self._label_gap = theme.px(11)
        self._report = None       # (status, time it was read)
        self._asked = 0.0
        self._last = time.perf_counter()
        self._shown_info = ""
        self._keep_going = toolwin.KeepGoing(self.win)
        theme.on_change(lambda _palette: self._layout())
        self.win.bind("<Configure>", self._moved, add="+")
        self._job = self.win.after(REFRESH_MS, self._tick)

    # -- settings -------------------------------------------------------------------------------
    def _reel(self) -> reels.ReelSize:
        return reels.REELS[self.size.get()]

    def _tape_speed(self) -> float:
        return reels.SPEEDS[self.speed.get()]

    def _choice_changed(self):
        self.prefs["reels_speed"] = self.speed.get()
        self.prefs["reels_size"] = self.size.get()
        self.transport.key = None   # a different tape: threaded afresh where the song is
        self._layout()

    # -- where the song is -----------------------------------------------------------------------
    def _song(self, now: float):
        """(status, the song's position now): asked every STATUS_EVERY_S, the clock run on between."""
        if self._report is None or now - self._asked >= STATUS_EVERY_S:
            self._report, self._asked = (self.player.status(), now), now
        status, read_at = self._report
        position = status.position
        if status.state is State.PLAYING:
            position = min(status.duration or position, position + (now - read_at))
        return status, position

    def _tape_now(self, now: float):
        """(spool, metres of tape on it, target place in metres, playing?)."""
        status, position = self._song(now)
        track = status.track
        lengths, playing = reels.album_run(list(getattr(self.player, "tracks", [])), status.index, track)
        if track is not None and lengths:
            lengths[playing] = status.duration or lengths[playing]   # the player's own length wins
        speed = self._tape_speed()
        reel = self._reel()
        capacity = reel.standard_length / speed
        album = (reels.album_key(track) if track is not None else ("",), self.speed.get(), self.size.get())
        spool, seconds = reels.spool_for(lengths, playing, position, capacity, album)
        return spool, spool.seconds * speed, seconds * speed, status.state is State.PLAYING

    # -- frames ------------------------------------------------------------------------------------
    def _tick(self):
        if not self.win.winfo_exists():
            return
        with self._keep_going:
            self._step()
        self._job = self.win.after(REFRESH_MS, self._tick)

    def _step(self):
        now = time.perf_counter()
        dt, self._last = now - self._last, now
        spool, length, target, playing = self._tape_now(now)
        speed = self._tape_speed()
        if self.transport.key != spool.key:
            self.transport.load(spool.key, target, speed if playing else 0.0)
        else:
            self.transport.step(dt, target, playing, speed, length)
        if not toolwin.on_screen(self.win) or self._frame is None:
            return
        supply, take = reels.reel_states(self._reel(), length, self.transport.place, self.transport.speed)
        self._draw(supply, take)
        self._paint_info(spool, length, speed)

    # -- drawing -----------------------------------------------------------------------------------
    def _deck(self):
        """The deck's parts in metres, x across with the reels side by side, y down."""
        reel = self._reel()
        flange = reel.flange_radius
        apart = flange * 1.12
        head_y = flange * 1.3
        guide_x = flange * 0.62
        return reel, flange, apart, head_y, guide_x

    def _layout(self):
        """Fit the deck to the canvas (side by side or stacked) and make its items afresh."""
        c = self.canvas
        w, h = c.winfo_width(), c.winfo_height()
        if w < 20 or h < 20:
            return
        reel, flange, apart, head_y, guide_x = self._deck()
        # The rpm labels' band grows with the window (full screen on a big monitor), and their text with it.
        text_band = max(theme.px(22), round(min(w, h) * 0.05))
        self._label_gap = text_band / 2
        label_font = (theme.numbers()[0], -max(theme.px(12), round(text_band * 0.5)))
        deck_w, deck_h = 2 * (apart + flange), flange + head_y + flange * 0.22
        side = (w / deck_w) >= ((h - text_band) / deck_h) * 0.8 and w > h * 0.9
        if side:
            scale = min(w / deck_w, (h - text_band) / deck_h) * 0.96
            ox, oy = w / 2, text_band + (h - text_band - deck_h * scale) / 2 + flange * scale
        else:   # stacked: turned a quarter turn clockwise, supply reel on top
            scale = min((w) / deck_h, (h - 2 * text_band) / deck_w) * 0.96
            ox = (w - deck_h * scale) / 2 + (head_y + flange * 0.22) * scale
            oy = h / 2
        self._frame = (scale, ox, oy, not side)
        c.configure(bg=theme.BG)
        c.delete("all")
        # In order, back to front: the heads, the tape (it runs between each reel's flanges, so the
        # front flange hides it), the reels, then the guides and capstan it wraps round.
        self._draw_heads(flange, head_y)
        self._items = {"tape": c.create_line(0, 0, 0, 0, fill=TAPE, width=max(2, round(flange * scale * 0.012)),
                                             joinstyle="round")}
        for name in ("supply", "take"):
            self._items[name] = {
                "flange": c.create_oval(0, 0, 0, 0, fill=FLANGE, outline=FLANGE_RIM, width=max(1, theme.px(1.5))),
                "holes": [c.create_polygon(0, 0, 0, 0, fill=theme.BG, outline=FLANGE_RIM) for _ in range(WINDOWS)],
                "packs": [c.create_polygon(0, 0, 0, 0, fill=TAPE, outline="") for _ in range(WINDOWS)],
                # Too fast to follow (spooling): the cut-outs smear into rings, as the eye sees them.
                "blur": c.create_oval(0, 0, 0, 0, fill=theme.mix(FLANGE, theme.BG, OPEN), outline="", state="hidden"),
                "blur_pack": c.create_oval(0, 0, 0, 0, fill=theme.mix(FLANGE, TAPE, OPEN), outline="",
                                           state="hidden"),
                "blur_core": c.create_oval(0, 0, 0, 0, fill=FLANGE, outline="", state="hidden"),
                "last": None,
                "hub":c.create_oval(0, 0, 0, 0, fill=HUB, outline=""),
                "ears": [c.create_polygon(0, 0, 0, 0, fill=HUB_EAR, outline="") for _ in range(3)],
                "spindle": c.create_oval(0, 0, 0, 0, fill=CAPSTAN, outline=""),
                "rpm": c.create_text(0, 0, text="", fill=theme.TEXT, font=label_font),
            }
        self._draw_rollers(flange, head_y, guide_x)
        self.info.configure(bg=theme.BG, fg=theme.TEXT)
        self._shown_info = ""

    def _to_screen(self, x: float, y: float) -> tuple[float, float]:
        scale, ox, oy, stacked = self._frame
        if stacked:
            x, y = -y, x
        return ox + x * scale, oy + y * scale

    def _flat(self, points) -> list[float]:
        out = []
        for x, y in points:
            out.extend(self._to_screen(x, y))
        return out

    def _circle(self, item, cx, cy, r):
        sx, sy = self._to_screen(cx, cy)
        r *= self._frame[0]
        self.canvas.coords(item, sx - r, sy - r, sx + r, sy + r)

    def _draw_heads(self, flange, head_y):
        """The erase, record and play heads, just under the tape: they don't move."""
        heads_w, heads_h = flange * 0.16, flange * 0.12
        for x in (-flange * 0.34, -flange * 0.1, flange * 0.14):
            pts = [(x - heads_w / 2, head_y), (x + heads_w / 2, head_y),
                   (x + heads_w / 2, head_y + heads_h), (x - heads_w / 2, head_y + heads_h)]
            self.canvas.create_polygon(self._flat(pts), fill=HEAD, outline=FLANGE_RIM)

    def _draw_rollers(self, flange, head_y, guide_x):
        """The tape guides, capstan and pinch roller (drawn standing still)."""
        c = self.canvas
        roller = flange * 0.05
        for x in (-guide_x, guide_x):
            item = c.create_oval(0, 0, 0, 0, fill=CAPSTAN, outline=FLANGE_RIM, tags="rollers")
            self._circle(item, x, head_y, roller)
        capstan_x = flange * 0.4
        item = c.create_oval(0, 0, 0, 0, fill=CAPSTAN, outline=FLANGE_RIM, tags="rollers")
        self._circle(item, capstan_x, head_y + roller * 0.6, roller * 0.6)
        item = c.create_oval(0, 0, 0, 0, fill=RUBBER, outline="", tags="rollers")
        self._circle(item, capstan_x, head_y - roller * 1.6, roller * 1.6)

    def _draw_reel(self, items, cx, cy, state: reels.ReelState, label: str):
        reel = self._reel()
        c = self.canvas
        flange = reel.flange_radius
        self._circle(items["flange"], cx, cy, flange)
        inner, outer = reel.core_radius * 1.08, flange * 0.9
        # How far it turned since the last frame: past BLUR_STEP the cut-outs can't be followed (they
        # would seem to stand still or turn backwards), so they're drawn as the smear the eye sees.
        last, items["last"] = items["last"], state.angle
        blurred = last is not None and abs(state.angle - last) > BLUR_STEP
        shown = "hidden" if blurred else "normal"
        for i in range(WINDOWS):
            start = state.angle + i * 2 * math.pi / WINDOWS - WINDOW_SPAN / 2
            c.coords(items["holes"][i], *self._flat(_sector(cx, cy, inner, outer, start, WINDOW_SPAN)))
            c.itemconfigure(items["holes"][i], state=shown)
            if state.radius > inner:
                pack = _sector(cx, cy, inner, min(outer, state.radius), start, WINDOW_SPAN)
                c.coords(items["packs"][i], *self._flat(pack))
                c.itemconfigure(items["packs"][i], state=shown)
            else:
                c.itemconfigure(items["packs"][i], state="hidden")
        pack_shows = state.radius > inner
        for name, radius, on in (("blur", outer, blurred),
                                 ("blur_pack", min(outer, state.radius), blurred and pack_shows),
                                 ("blur_core", inner, blurred)):
            self._circle(items[name], cx, cy, radius)
            c.itemconfigure(items[name], state="normal" if on else "hidden")
        hub = reel.core_radius * 0.62
        self._circle(items["hub"], cx, cy, hub)
        for i in range(3):   # the hub's three locking ears turn with the reel
            a = state.angle + i * 2 * math.pi / 3
            w = 0.38
            pts = [(cx + hub * 0.35 * math.cos(a - w), cy + hub * 0.35 * math.sin(a - w)),
                   (cx + hub * 0.95 * math.cos(a - w / 2), cy + hub * 0.95 * math.sin(a - w / 2)),
                   (cx + hub * 0.95 * math.cos(a + w / 2), cy + hub * 0.95 * math.sin(a + w / 2)),
                   (cx + hub * 0.35 * math.cos(a + w), cy + hub * 0.35 * math.sin(a + w))]
            c.coords(items["ears"][i], *self._flat(pts))
            c.itemconfigure(items["ears"][i], state="hidden" if blurred else "normal")
        self._circle(items["spindle"], cx, cy, hub * 0.22)
        # The rpm: above the reel side by side; above the top reel and below the bottom one stacked.
        stacked = self._frame[3]
        sx, sy = self._to_screen(cx, cy)
        r = flange * self._frame[0]
        gap = self._label_gap
        if stacked and cx > 0:
            sy, anchor = sy + r + gap, "center"
        else:
            sy, anchor = sy - r - gap, "center"
        c.coords(items["rpm"], sx, sy)
        c.itemconfigure(items["rpm"], text=label, anchor=anchor)

    def _draw(self, supply: reels.ReelState, take: reels.ReelState):
        reel, flange, apart, head_y, guide_x = self._deck()
        spooling = self.transport.winding != 0.0   # (a seek: far faster than a deck would wind)
        for name, x, state in (("supply", -apart, supply), ("take", apart, take)):
            self._draw_reel(self._items[name], x, 0.0, state, "spooling…" if spooling else describe_rpm(state.rpm))
        # The tape: down the supply pack's right side, across the heads, up onto the take-up's left side.
        left = tangent_point(-apart, 0.0, supply.radius, -guide_x, head_y, right=True)
        right = tangent_point(apart, 0.0, take.radius, guide_x, head_y, right=False)
        self.canvas.coords(self._items["tape"],
                           *self._flat([left, (-guide_x, head_y), (guide_x, head_y), right]))

    def _paint_info(self, spool: reels.Spool, length: float, speed: float):
        reel = self._reel()
        left = max(0.0, length - self.transport.place) / speed if speed else 0.0
        reel_text = f"Reel {spool.number} of {spool.count} · " if spool.count > 1 else ""
        text = theme.figures(
            f"{reel_text}{reels.SPEED_NAMES[self.speed.get()]} ({speed * 100:.2f} cm/s)\n"
            f"{reel.name} reels, {reel.standard_name} of standard-play tape\n"
            f"On this reel {describe_seconds(length / speed if speed else 0)} · left {describe_seconds(left)}")
        if text != self._shown_info:
            self._shown_info = text
            self.info.configure(text=text)

    # -- window ------------------------------------------------------------------------------------
    def toggle_full_screen(self):
        self._full_screen = not self._full_screen
        if self._full_screen:
            self.bar.pack_forget()
            self.note.pack_forget()
            self.info.pack_forget()
        else:
            self.canvas.pack_forget()
            self.bar.pack(fill="x")
            self.note.pack(side="bottom", fill="x")
            self.info.pack(side="bottom", fill="x")
            self.canvas.pack(fill="both", expand=True, padx=theme.px(10), pady=theme.pad(4, 6))
        self.win.attributes("-fullscreen", self._full_screen)

    def _escape(self, _event=None):
        if self._full_screen:
            self.toggle_full_screen()
        else:
            self.close()

    def _moved(self, event):
        if event.widget is self.win and not self._full_screen:
            geometry = placement.current(self.win)
            if geometry:
                self.prefs["reels_geometry"] = geometry

    def lift(self):
        self.win.deiconify()
        self.win.lift()

    def close(self):
        if self._job:
            self.win.after_cancel(self._job)
            self._job = None
        self.win.destroy()
        self.on_close()
