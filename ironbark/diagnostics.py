"""Diagnostics: one .zip the user saves and can send with a problem report. No UI code.

Settings → Help → "Save diagnostics…" asks where to save it; nothing is sent anywhere. Inside:
- README.txt — what's in it;
- about.txt — Ironbark's version, whether it's the packaged program, the experiments switched on,
  Windows' version, Python / Tk / libsndfile / PortAudio versions;
- audio-outputs.txt — how many sound outputs are connected, which is the default and which is
  chosen in Settings;
- settings.json — the settings, with every folder or file path replaced by "<folder 1>",
  "<folder 2>"… (the same path always gets the same name), so no user name or music shows;
- crash.log (and crash.old.log) and app.log (and its older copies), with the same paths replaced,
  and the user's home folder shown as "<home>".

Names can say who someone is too ("Sam's AirPods", a playlist named after a person), so in every
file the sound outputs' names show as "<device 1>", "<device 2>"…, the open tabs' titles as "<tab 1>"…,
the playlists' names as "<playlist 1>"… and searches as "<search 1>"… — again the same name always
the same stand-in, so the files still agree with each other. A network folder's server ("\\\\sams-nas")
is named on its own in the logs too (the network playback cache's notes), so it shows as "<server 1>"….
A folder on a mapped network drive ("Z:\\Music") is also known by its server path, which the cache's
notes use: Windows is asked what each such drive letter stands for, and that path gets the same stand-in.
"""

from __future__ import annotations

import datetime
import io
import json
import platform
import re
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path

from . import APP_NAME, __version__, experiments, logs

# A full path: a drive (C:\ or C:/), a network share (\\server\share) or, off Windows, from the root.
_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/]|/(?!/))")
# Where a path may begin in a log: a drive, or slashes that don't carry on a name (a network share,
# a path from the root, or one inside quotes, where Python writes every backslash twice: 'C:\\Users').
_PATH_START = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:(?=[\\/])|(?<![\w:.~$\\/-])[\\/]+(?=[^\\/\s])")
# A network share's server: \\server\share (or //server/share).
_SERVER = re.compile(r"^[\\/]{2}([^\\/]+)[\\/]")
_SEPARATORS = frozenset("\\/")
_NOT_IN_NAMES = frozenset('\\/\r\n\t"<>|*?:')   # (and line ends): one part of a path stops at these
# A path written in a log may also end inside the part that follows it, just before one of these:
# "D:\Music\Abba (missing)", "'E:\\x.flac',".
_MAY_END_BEFORE = frozenset(" '.,;)]}")
# Settings whose value names a sound output (or, for the hardware test, an input), and ones that
# hold something typed into a search box.
DEVICE_KEYS = frozenset({"output_device", "testsignals_output", "testsignals_input"})
SEARCH_KEYS = frozenset({"query", "last_filter"})
NAME_MIN = 3   # characters: a shorter name isn't replaced in the logs (it would hit ordinary words)
# Tab titles the player gives every such tab, which say nothing about anyone.
GENERIC_TITLES = frozenset({"Library", "Files"})
CRASH_LOGS = ("crash.log", "crash.old.log")
README = f"""{APP_NAME} diagnostics

Made by {APP_NAME} at your request, to send with a problem report. Nothing has been sent anywhere:
you choose whether to share this file, and with whom.

about.txt          {APP_NAME}'s version, Windows' version, and the parts it's built from
audio-outputs.txt  how many sound outputs were connected when this was made, the default one
                   and the one chosen in Settings
settings.json      your settings, with folder and file paths replaced by <folder 1>, <folder 2>...
crash.log          errors {APP_NAME} has met (if any)
app.log            what {APP_NAME} noted while running (more with Settings > Help > Detailed logging)

In the logs, the same paths are replaced, and your home folder shows as <home>. In every file the
names of your sound outputs show as <device 1>, <device 2>..., your open tabs' titles as <tab 1>...,
your playlists' names as <playlist 1>..., searches as <search 1>... and network servers as
<server 1>... Song, album and artist names a log mentions (a song that wouldn't play, say), and
folders outside the ones in your settings and your home folder, can still show: look through the
files before sending if that matters.
"""


def default_name(now: datetime.datetime | None = None) -> str:
    now = now or datetime.datetime.now()
    return f"{APP_NAME}-diagnostics-{now:%Y%m%d-%H%M}.zip"


def _normal(path: str) -> str:
    """How paths are compared: each run of slashes one backslash, none at the end, letter case ignored."""
    return re.sub(r"[\\/]+", r"\\", path).rstrip("\\").casefold()


class Redactor:
    """Replaces paths with "<folder N>" (numbered in the order first met; the same path, whatever
    its letter case or slashes, always gets the same number), and names that aren't paths — sound
    outputs, tab titles, playlists, searches, a network folder's server — with "<device N>",
    "<tab N>", "<playlist N>", "<search N>", "<server N>" (`word`; numbered per kind, the same name
    always the same stand-in)."""

    def __init__(self, home: str | None = None):
        self.names: dict[str, str] = {}   # normalised path (`_normal`) → its stand-in
        self.words: dict[str, str] = {}   # a name, exactly as written → its stand-in
        self._counts: dict[str, int] = {}
        self.home = home if home is not None else str(Path.home())

    @staticmethod
    def is_path(text) -> bool:
        return isinstance(text, str) and bool(_PATH.match(text))

    def name(self, path: str) -> str:
        key = _normal(path)
        if key not in self.names:
            self.names[key] = f"<folder {len(self.names) + 1}>"
            self._server_in(path)
        return self.names[key]

    def _server_in(self, path: str) -> None:
        server = _SERVER.match(path)
        if server:   # (the logs also name it on its own, in small letters: "sams-nas isn't answering")
            stand_in = self.word(server.group(1), "server")
            self.words.setdefault(server.group(1).lower(), stand_in)

    def learn_network_drives(self, target: Callable[[str], str | None]) -> None:
        """A path met on a mapped network drive ("Z:\\Music") is also written in the logs by its
        server path ("\\\\sams-nas\\media\\Music": the network playback cache's notes): that gets the
        same stand-in, and the server's own name is left out too. `target("Z:")` is the server path a
        drive letter stands for, or None; each letter is asked once."""
        found: dict[str, str | None] = {}
        for key, stand_in in list(self.names.items()):
            if not re.match(r"[a-z]:(?:\\|$)", key):
                continue
            letter = key[0].upper() + ":"
            if letter not in found:
                try:
                    found[letter] = target(letter)
                except Exception:  # noqa: BLE001 — a lookup that fails only means one name fewer left out
                    found[letter] = None
            if found[letter]:
                server_path = found[letter].rstrip("\\/") + key[2:]
                self.names.setdefault(_normal(server_path), stand_in)
                self._server_in(server_path)

    def word(self, text: str, kind: str) -> str:
        """The stand-in for a name of this kind ("device", "tab", "playlist", "search", "server")."""
        found = self.words.get(text)
        if found is None:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            found = self.words[text] = f"<{kind} {self._counts[kind]}>"
        return found

    def learn(self, names: dict[str, list[str]]) -> None:
        """Names to leave out wherever they appear, by kind ({"tab": [...], "playlist": [...]})."""
        for kind, found in names.items():
            for text in found:
                if isinstance(text, str) and text.strip() and text not in GENERIC_TITLES:
                    self.word(text, kind)

    def value(self, value, key: str = ""):
        """A settings value with its paths (in keys too) replaced; under `key` a value may be a name
        (an output, a tab's title, a search) and is replaced as one."""
        if isinstance(value, str):
            if self.is_path(value):
                return self.name(value)
            if value and key in DEVICE_KEYS:
                return self.word(value, "device")
            if value and key in SEARCH_KEYS:
                return self.word(value, "search")
            if value and key == "title" and value not in GENERIC_TITLES:
                return self.word(value, "tab")
            return value
        if isinstance(value, list):
            return [self.value(v, key) for v in value]
        if isinstance(value, dict):
            return {(self.name(k) if self.is_path(k) else k): self.value(v, k) for k, v in value.items()}
        return value

    def text(self, text: str) -> str:
        """A log's text with the paths already met replaced, and the home folder as <home>: written
        with either slash, in any case, or with every backslash doubled (as Python writes a path in
        an error message), and each time the longest that fits, so a folder inside another is
        replaced whole. One pass over the text, however many paths the settings hold."""
        # (Never a drive's root: "C:" begins every path on the drive.)
        known = {key: stand_in for key, stand_in in self.names.items() if len(key) > 3}
        home = _normal(self.home) if self.home else ""
        if len(home) > 3:
            known.setdefault(home, "<home>")
        if not known:
            return self._names_in(text)
        pieces, done = [], 0
        for start in _PATH_START.finditer(text):
            if start.start() < done:   # (inside a path just replaced)
                continue
            found = _known_at(known, text, start.start(), start.end())
            if found is not None:
                end, stand_in = found
                pieces += [text[done:start.start()], stand_in]
                done = end
        pieces.append(text[done:])
        return self._names_in("".join(pieces))

    def _names_in(self, text: str) -> str:
        """The names met so far replaced wherever they stand alone (not inside a longer word), the
        longest first, written exactly as they were (case matters: "Help" the album, not "help")."""
        # (only the names the text holds at all go into the pattern: a log is megabytes long)
        words = sorted((w for w in self.words if len(w) >= NAME_MIN and w in text), key=len, reverse=True)
        if not words:
            return text
        pattern = "|".join(("(?<!\\w)" if w[0].isalnum() or w[0] == "_" else "") + re.escape(w)
                           + ("(?!\\w)" if w[-1].isalnum() or w[-1] == "_" else "") for w in words)
        return re.sub(pattern, lambda m: self.words[m.group(0)], text)


def _known_at(known: dict[str, str], text: str, start: int, root_end: int) -> tuple[int, str] | None:
    """The longest known path written at text[start:] (it begins with a drive or slashes, which end
    at `root_end`): (where it ends, its stand-in), or None. Walks the path part by part, so it takes
    as long as the path is long, whatever the number of known paths."""
    drive = text[start] not in _SEPARATORS
    key = text[start:root_end].casefold() if drive else ""
    at, size, best = (root_end if drive else start), len(text), None
    while at < size and text[at] in _SEPARATORS:
        first = at
        while first < size and text[first] in _SEPARATORS:
            first += 1
        stop = first
        while stop < size and text[stop] not in _NOT_IN_NAMES:
            stop += 1
        part = text[first:stop]
        if not part:
            break
        for cut in [len(part)] + [i for i in range(len(part) - 1, 0, -1) if _may_end(part, i)]:
            stand_in = known.get(f"{key}\\{part[:cut].casefold()}")
            if stand_in is not None:
                best = (first + cut, stand_in)
                break
        key = f"{key}\\{part.casefold()}"
        at = stop
    return best


def _may_end(part: str, at: int) -> bool:
    """May a path end inside this part of it, just before part[at]? Before a quote, a comma or a
    bracket, yes. Before a space only when what follows isn't more of a name ("D:\\Music\\Abba (missing)",
    "…Abba is gone", but not "D:\\Music Videos", a folder of its own, nor "Abba Gold"); before a full stop
    only at the end of a sentence ("…Abba." but not "Abba.flac")."""
    mark = part[at]
    if mark not in _MAY_END_BEFORE:
        return False
    rest = part[at + 1:]
    if mark == " ":
        rest = rest.lstrip(" ")
        return not rest or not (rest[0].isupper() or rest[0].isdigit())
    if mark == ".":
        return not rest or rest[0] in " '\")]},;"
    return True


def about(prefs: dict) -> str:
    """Versions of everything the player is built from, and the system it's running on."""
    lines = [f"{APP_NAME} {__version__}" + (" (packaged)" if getattr(sys, "frozen", False) else " (from source)"),
             f"Made {datetime.datetime.now():%d/%m/%Y %H:%M:%S}",
             f"Experiments on: {', '.join(e.key for e in experiments.switched_on(prefs)) or 'none'}",
             f"System: {platform.platform()} ({platform.machine()})"]
    if sys.platform == "win32":
        version = sys.getwindowsversion()
        lines.append(f"Windows: {platform.win32_ver()[0]} build {version.build} "
                     f"({platform.win32_edition() or 'edition unknown'})")
    lines.append(f"Python {platform.python_version()}")
    for name, look_up in (("Tk", _tk_version), ("libsndfile", _sndfile_version), ("PortAudio", _portaudio_version),
                          ("numpy", lambda: __import__("numpy").__version__),
                          ("Pillow", lambda: __import__("PIL").__version__)):
        try:
            lines.append(f"{name} {look_up()}")
        except Exception as error:  # noqa: BLE001 — a part that won't load is itself worth knowing
            lines.append(f"{name}: not available ({error})")
    return "\n".join(lines) + "\n"


def _tk_version() -> str:
    import tkinter

    return str(tkinter.TkVersion)


def _sndfile_version() -> str:
    import soundfile

    return soundfile.__libsndfile_version__


def _portaudio_version() -> str:
    import sounddevice

    return sounddevice.get_portaudio_version()[1]


def audio_outputs() -> tuple[str | None, list[str]]:
    """(the default output, the outputs connected now) — Windows' own list."""
    from . import devices

    return devices.default_output_name(), devices.output_names()


def outputs_text(redactor: Redactor, default: str | None, connected: list[str], chosen: str) -> str:
    """audio-outputs.txt: the outputs by their stand-ins, so it still says which is the default, which
    is chosen and whether that one is connected."""
    lines = [f"Default output: {redactor.word(default, 'device') if default else 'unknown'}",
             f"Connected outputs ({len(connected)}):"]
    lines += [f"  {redactor.word(name, 'device')}" for name in connected] or ["  (none found)"]
    if chosen:
        here = "connected" if chosen in connected else "not connected now"
        lines.append(f"Chosen in Settings: {redactor.word(chosen, 'device')} ({here})")
    else:
        lines.append("Chosen in Settings: Windows' default output")
    lines.append("(Output names are left out: <device 1>, <device 2>... stand for them in every file.)")
    return "\n".join(lines) + "\n"


def network_target(letter: str) -> str | None:
    """The server path a drive letter ("Z:") stands for when it's a network drive, else None."""
    from .netcache import sources

    return sources.network_target(letter)


def contents(prefs: dict, folder: Path, outputs: Callable[[], tuple[str | None, list[str]]] = audio_outputs,
             home: str | None = None, names: dict[str, list[str]] | None = None,
             network: Callable[[str], str | None] = network_target) -> dict[str, str]:
    """Every file for the zip, by name. `names`: other names to leave out, by kind (the window's tab
    titles and playlist names: {"tab": [...], "playlist": [...]}). `network`: the server path a drive
    letter stands for (asked of Windows: call this off the window's thread)."""
    redactor = Redactor(home)
    files = {"README.txt": README, "about.txt": about(prefs)}
    try:
        default, connected = outputs()
        listed = outputs_text(redactor, default, list(connected), str(prefs.get("output_device") or ""))
    except Exception as error:  # noqa: BLE001
        listed = f"The list of outputs couldn't be read: {error}\n"
    redactor.learn(names or {})
    files["settings.json"] = json.dumps(redactor.value(prefs), indent=2, ensure_ascii=False) + "\n"
    files["audio-outputs.txt"] = listed
    redactor.learn_network_drives(network)   # (before the logs: they may name a mapped drive's server)
    for path in [folder / name for name in CRASH_LOGS] + logs.log_files(folder):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files[path.name] = redactor.text(text)
    return files


def write_zip(destination: Path, prefs: dict, folder: Path,
              outputs: Callable[[], tuple[str | None, list[str]]] = audio_outputs, home: str | None = None,
              names: dict[str, list[str]] | None = None) -> Path:
    """Write the zip (whole, or not at all). Touches the disk and asks Windows for the outputs:
    call it off the window's thread. Raises OSError when it can't be written."""
    files = contents(prefs, folder, outputs, home, names)
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, text in files.items():
            archive.writestr(name, text.encode("utf-8"))
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".partial")
    try:
        temporary.write_bytes(data.getvalue())
        temporary.replace(destination)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return destination
