"""The words of the player's status lines: the line under the format (what the output does to
the sound, and whether it's bit-perfect), the network playback cache's status line, hover and Up
next marks, and the short formatters they share. No Tk here: the main window, Now Playing and the
tests all use these.
"""

from __future__ import annotations

import math

from . import levelling, passthrough, platforms
from .engine import OutputInfo
from .library import format_rate

MIXER = "Windows" if platforms.IS_WINDOWS else "system"


def levelling_text(level, trim: float = 1.0) -> str:
    """"Volume levelling −4.3 dB (album)" for a track that volume levelling changes (the gain the
    track really gets: when the clipping guard holds it lower, the guard's figure, and that's said).
    "" when the track's gain comes to exactly 0 dB: its samples go out untouched (proof.apply_gain)."""
    if level is None or trim == 1.0:
        return ""
    if 0 < trim < level.gain * (1 - 1e-6):
        return (f"Volume levelling {20 * math.log10(trim):+.1f} dB ({level.mode}, held down by the clipping "
                "guard)").replace("-", "−")
    return levelling.describe(level)


def output_line(output: OutputInfo | None, gain: float, float_source: bool = False,
                lossy: bool = False, trim: float = 1.0, bits: int = 0, codec: str = "",
                level=None, fading: bool = False, show_bits: bool = False,
                encoded: str = "") -> tuple[str, str]:
    """(text, tone) for the line under the format, as _output_line, plus a word for an encoded
    stream (`encoded` "DTS" / "HDCD", passthrough.py): kept at its own level when it goes out
    untouched; amber, saying what it needs, when the volume, a fade or the shared mixer changes it."""
    text, tone = _output_line(output, gain, float_source, lossy, trim, bits, codec, level, fading, show_bits)
    if not encoded or output is None:
        return text, tone
    if output.exclusive and gain >= 1.0 and not fading and not float_source and not fewer_bits(output, bits):
        return f"{text} · {passthrough.note(encoded)}", tone
    breaks = "the receiver gets noise" if encoded == passthrough.DTS else "the decoder loses its codes"
    needs = "exclusive mode" if not output.exclusive else "volume at 100%" if gain < 1.0 else "the samples exactly"
    return f"{text} · this {encoded} stream needs {needs}, or {breaks}", "warn"


def _output_line(output: OutputInfo | None, gain: float, float_source: bool = False,
                 lossy: bool = False, trim: float = 1.0, bits: int = 0, codec: str = "",
                 level=None, fading: bool = False, show_bits: bool = False) -> tuple[str, str]:
    """(text, tone) for the line under the format. tone is "good", "warn" or "plain".
    float_source: the file holds floating-point samples (some WAVs), which the device
    can only take after conversion to whole numbers — so never bit-perfect.
    lossy: an MP3, Ogg Vorbis or Opus file (`codec` names it) — delivery can be exact, but only
    of what the decoder rebuilt.
    bits: the track's bits per sample; a device that takes fewer (OutputInfo.device_bits) drops
    the rest, so that isn't bit-perfect either.
    level: volume levelling on this track (levelling.Levelling) — amber, never bit-perfect (unless
    its gain comes to exactly 0 dB, which leaves the samples as they are).
    fading: the sleep timer is fading the music out.
    show_bits: name the bits per sample the device takes in exclusive mode, when known ("96 kHz
    24-bit exclusive"): the Details window's longer line."""
    if output is None:
        return "", "plain"
    changes = [text for text in (levelling_text(level, trim), "fading out (sleep timer)" if fading else "") if text]
    if changes:   # volume levelling (it takes in the clipping guard) or a fade: the level is changed on purpose
        device = output.device
        if output.exclusive:
            base = f"→ {device} · {_exclusive_format(output, show_bits)}"
            volume = " · volume below 100%" if gain < 1.0 else ""
            return f"{base}{volume} · {' · '.join(changes)}, so not bit-perfect", "warn"
        mixed = (f"resampled to {format_rate(output.mixer_rate)} by the {MIXER} mixer ⚠" if output.resampled
                 else f"{format_rate(output.rate)} · shared ({MIXER} mixer)")
        return f"→ {device} · {mixed} · {' · '.join(changes)}", "warn"
    guard = (f" · clipping guard {20 * math.log10(trim):.1f} dB on this loud recording (it would clip "
             f"the converter)") if trim < 1.0 else ""
    device = output.device
    if output.exclusive:
        base = f"→ {device} · {_exclusive_format(output, show_bits)}"
        if gain < 1.0:
            return f"{base} · volume below 100%, so not bit-perfect", "warn"
        if float_source:
            return f"{base} · floating-point file converted for the device, so not bit-perfect", "warn"
        if lossy:
            return f"{base} · decoded {codec or 'MP3'} sent untouched, but the source is lossy{guard}", "plain"
        if fewer_bits(output, bits):
            return (f"{base} · the device takes {output.device_bits}-bit samples, so this {bits}-bit track "
                    f"isn't bit-perfect{guard}", "warn")
        if guard:
            # (0.24.0) The clipping guard wins over Bit-perfect (crackle-free first), and says so.
            return f"{base}{guard}, so this track isn't bit-perfect (the guard comes first)", "good"
        if output.amp_volume and not output.hardware_volume:
            return f"{base} · bit-perfect ✓ · volume on your amplifier", "good"
        if output.hardware_volume is False:
            return f"{base} · bit-perfect ✓ · no device volume: use the player or headphone volume", "warn"
        if output.hardware_volume:
            return f"{base} · bit-perfect ✓ · volume by device ✓", "good"
        return f"{base} · bit-perfect ✓", "good"
    if output.resampled:
        return (f"→ {device} · resampled to {format_rate(output.mixer_rate)} by the {MIXER} mixer ⚠{guard}",
                "warn")
    return f"→ {device} · {format_rate(output.rate)} · shared ({MIXER} mixer){guard}", "plain"


def _exclusive_format(output: OutputInfo, show_bits: bool) -> str:
    """"96 kHz exclusive", or "96 kHz 24-bit exclusive" with `show_bits` and the device's bits known."""
    bits = f" {output.device_bits}-bit" if show_bits and output.device_bits else ""
    return f"{format_rate(output.rate)}{bits} exclusive"


def fewer_bits(output: OutputInfo | None, bits: int) -> bool:
    """Whether the device (exclusive mode) takes fewer bits per sample than the track holds."""
    return bool(output is not None and output.exclusive and output.device_bits and bits
                and bits > output.device_bits)


def fmt_size(size: int) -> str:
    return f"{size / 1_048_576:.1f} MB" if size else "—"


def fmt_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def fmt_clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def to_db(level: float) -> float:
    return 20 * math.log10(level) if level > 0 else float("-inf")


def tone_of(text: str) -> str:
    """(0.22.0) A Check / File line's colour, as the main window colours its status: a line that
    starts ✓ is good news (green), ✗ or ⚠ something to look at (amber), anything else plain."""
    if text.startswith("✓"):
        return "good"
    if text.startswith(("✗", "⚠")):
        return "warn"
    return "plain"


COPY_FAILED = "✗ This played from a copy that failed its check. Downloading it again."
COPY_SKIPPED = "✗ This played from a copy that didn't match the original. The song is skipped for now."


def copy_failed_text(copy: str) -> str:
    """The warning for a song played from a copy that failed its check ("" for any other copy):
    a fresh copy is downloading ("failed"), or — failed again — the song is skipped for now
    ("rejected")."""
    return {"failed": COPY_FAILED, "rejected": COPY_SKIPPED}.get(copy, "")


def copy_note(copy: str) -> str:
    """What the proof's subject was, for a song played from the network playback cache: it's
    checked against the copy on this computer, and that copy against the original."""
    if copy == "verified":
        return "checked against the cached copy · the copy matches the original"
    if copy in ("downloading", "checking"):
        return "checked against the cached copy · its match with the original is checked when the download finishes"
    if copy == "stale":
        return "checked against the cached copy (an older version: the file on the drive has changed since)"
    return ""


RELEASED_HINT = " (or press play to try now)"   # the status dot's hover adds this to the released line


def network_line(network, mp3: bool = False) -> tuple[str, str]:
    """(status line, tone) while a song from a network drive isn't simply playing: preparing,
    waiting for the network, fetching after a seek, or let go of the device. ("", …) otherwise.
    Short enough for one status line in the narrow window; the cache's own fuller words
    (`network.detail`) are the status dot's hover."""
    if network is None or network.phase == "playing":
        return "", "plain"
    percent = network.percent
    pct = f" {percent}%" if percent is not None else ""
    if network.phase == "waiting":
        at = fmt_clock(network.at)
        if network.auto and network.detail.startswith("the download is slower"):
            # (its server answers, just more slowly than the music plays: "…: building up 45 s")
            building = network.detail.partition(": ")[2] or "building up"
            return f"The network is slower than the music · paused at {at} · {building}", "warn"
        then = "carries on by itself" if network.auto else "press play to carry on"
        return f"Waiting for the network · paused at {at} · {then}", "warn"
    if network.phase == "fetching":
        if mp3:
            return f"Fetching… an MP3 has to be read from the start up to here:{pct}", "plain"
        return f"Fetching from {fmt_clock(network.at)}…{pct}", "plain"
    if network.phase == "released":
        if network.auto:
            return "Paused: the network isn't answering · carries on by itself when it's back", "warn"
        return "Paused for the network · press play to carry on", "plain"
    if not network.auto:
        return f"Paused · the song keeps downloading{f' ({percent}%)' if percent is not None else ''}", "plain"
    if network.phase == "checking":
        return "Checking the download…", "plain"
    if network.phase == "downloading":
        return f"Downloading track…{pct}", "plain"
    if network.detail.startswith("waiting for"):   # its server isn't answering: "waiting for <server> (not …"
        server = network.detail[len("waiting for "):].partition(" (not answering)")[0].strip() or "its server"
        return f"Preparing track… {server} isn't answering · starts when it's back", "warn"
    return f"Preparing track…{pct}", "plain"


def fmt_speed(speed: float | None) -> str:
    return f"{speed / 1e6:.1f} MB/s (megabytes per second)" if speed else ""


QUALITY_WORDS = {"excellent": "Excellent", "good": "Good", "poor": "Poor", "unstable": "Unstable"}


def server_name(host: str) -> str:
    """A server as people write it: \\\\192.168.80.173 (or OneDrive)."""
    if host == "cloud":
        return "OneDrive"
    return "\\\\" + host if host else "its server"


def network_now_line(now) -> str:
    """Settings → Network playback's line about the network, as the song playing (or the server
    last fetched from) sees it: "Network now: Good — 12.8 MB/s (megabytes per second); this song
    needs 0.4 MB/s"."""
    if now is None:
        return "Network now: nothing fetched yet"
    if now.state == "down":
        return f"Network now: {server_name(now.host)} isn't answering"
    words = QUALITY_WORDS.get(now.quality, "measuring")
    speed = f" — {fmt_speed(now.speed)}" if now.speed else ""
    need = f"; this song needs {now.song_rate / 1e6:.1f} MB/s" if now.song_rate else ""
    return f"Network now: {words}{speed}{need}"


def cache_mark(progress) -> tuple[str, str, str]:
    """(mark, tone, what it means) for a network song in Up next: ● in the cache, ◐ 72% partly
    downloaded, ○ waiting to download, ! couldn't download. ("", "", "") for a song on this computer.
    Tones: "accent", "warn", "plain"."""
    if progress is None:
        return "", "", ""
    if progress.state == "ready":
        return "●", "accent", "In the cache: plays without the network"
    if progress.state == "failed" or progress.host_state == "down":
        server = server_name(progress.host)
        why = (f"{server} isn't answering" if progress.host_state == "down"
               else progress.error or "it couldn't be read")
        return "!", "warn", f"Couldn't download: {why} · will try again"
    percent = int(progress.fraction * 100)
    if percent > 0:
        return f"◐ {percent}%", "plain", f"{percent}% downloaded"
    return "○", "plain", "Waiting to download"


def network_hover(network) -> str:
    """The status square's hover text for a song from a network drive (one line)."""
    if network is None:
        return ""
    if network.phase != "playing":
        if network.detail:
            return network.detail
        line = network_line(network)[0]
        return line + RELEASED_HINT if network.phase == "released" and network.auto else line
    if copy_failed_text(network.copy):
        return copy_failed_text(network.copy)
    parts = ["Playing the local copy"]
    if network.cached < 1.0:
        speed = fmt_speed(network.speed)
        parts.append(f"{network.cached:.0%} downloaded" + (f" at {speed}" if speed else ""))
    elif network.copy == "verified":
        parts.append("complete and checked")
    if network.next_cached is not None:
        parts.append(f"next song {network.next_cached:.0%}")
    return " · ".join(parts)
