"""Diagnostics: one .zip the user saves and can send with a problem report. No UI code.

Settings → Help → "Save diagnostics…" asks where to save it; nothing is sent anywhere. Inside:
- README.txt — what's in it;
- about.txt — Ironbark's version, whether it's the packaged program, the experiments switched on,
  Windows' version, Python / Tk / libsndfile / PortAudio versions;
- audio-outputs.txt — the names of the sound outputs connected, and the default one;
- settings.json — the settings, with every folder or file path replaced by "<folder 1>",
  "<folder 2>"… (the same path always gets the same name), so no user name or music shows;
- crash.log (and crash.old.log) and app.log (and its older copies), with the same paths replaced,
  and the user's home folder shown as "<home>".
"""

from __future__ import annotations

import datetime
import io
import json
import platform
import re
import sys
import zipfile
from pathlib import Path
from typing import Callable

from . import APP_NAME, __version__, experiments, logs

# A full path: a drive (C:\ or C:/), a network share (\\server\share) or, off Windows, from the root.
_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/]|/(?!/))")
# Where a path may begin in a log: a drive, or slashes that don't carry on a name (a network share,
# a path from the root, or one inside quotes, where Python writes every backslash twice: 'C:\\Users').
_PATH_START = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:(?=[\\/])|(?<![\w:.~$\\/-])[\\/]+(?=[^\\/\s])")
_SEPARATORS = frozenset("\\/")
_NOT_IN_NAMES = frozenset('\\/\r\n\t"<>|*?:')   # (and line ends): one part of a path stops at these
# A path written in a log may also end inside the part that follows it, just before one of these:
# "D:\Music\Abba (missing)", "'E:\\x.flac',".
_MAY_END_BEFORE = frozenset(" '.,;)]}")
CRASH_LOGS = ("crash.log", "crash.old.log")
README = f"""{APP_NAME} diagnostics

Made by {APP_NAME} at your request, to send with a problem report. Nothing has been sent anywhere:
you choose whether to share this file, and with whom.

about.txt          {APP_NAME}'s version, Windows' version, and the parts it's built from
audio-outputs.txt  the sound outputs connected when this was made
settings.json      your settings, with folder and file paths replaced by <folder 1>, <folder 2>...
crash.log          errors {APP_NAME} has met (if any)
app.log            what {APP_NAME} noted while running (more with Settings > Help > Detailed logging)

In the logs, the same paths are replaced, and your home folder shows as <home>. Music file names a
log mentions in passing may still show: look through the files before sending if that matters.
"""


def default_name(now: datetime.datetime | None = None) -> str:
    now = now or datetime.datetime.now()
    return f"{APP_NAME}-diagnostics-{now:%Y%m%d-%H%M}.zip"


def _normal(path: str) -> str:
    """How paths are compared: each run of slashes one backslash, none at the end, letter case ignored."""
    return re.sub(r"[\\/]+", r"\\", path).rstrip("\\").casefold()


class Redactor:
    """Replaces paths with "<folder N>" (numbered in the order first met; the same path, whatever
    its letter case or slashes, always gets the same number)."""

    def __init__(self, home: str | None = None):
        self.names: dict[str, str] = {}   # normalised path (`_normal`) → its stand-in
        self.home = home if home is not None else str(Path.home())

    @staticmethod
    def is_path(text) -> bool:
        return isinstance(text, str) and bool(_PATH.match(text))

    def name(self, path: str) -> str:
        key = _normal(path)
        if key not in self.names:
            self.names[key] = f"<folder {len(self.names) + 1}>"
        return self.names[key]

    def value(self, value):
        """A settings value with its paths (in keys too) replaced."""
        if isinstance(value, str):
            return self.name(value) if self.is_path(value) else value
        if isinstance(value, list):
            return [self.value(v) for v in value]
        if isinstance(value, dict):
            return {(self.name(k) if self.is_path(k) else k): self.value(v) for k, v in value.items()}
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
            return text
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
        return "".join(pieces)


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
        for cut in [len(part)] + [i for i in range(len(part) - 1, 0, -1) if part[i] in _MAY_END_BEFORE]:
            stand_in = known.get(f"{key}\\{part[:cut].casefold()}")
            if stand_in is not None:
                best = (first + cut, stand_in)
                break
        key = f"{key}\\{part.casefold()}"
        at = stop
    return best


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


def audio_outputs() -> str:
    """The outputs connected now (Windows' own list), and the default one."""
    from . import devices

    names = devices.output_names()
    default = devices.default_output_name()
    lines = [f"Default output: {default or 'unknown'}", "Connected outputs:"]
    lines += [f"  {name}" for name in names] or ["  (none found)"]
    return "\n".join(lines) + "\n"


def contents(prefs: dict, folder: Path, outputs: Callable[[], str] = audio_outputs,
             home: str | None = None) -> dict[str, str]:
    """Every file for the zip, by name."""
    redactor = Redactor(home)
    files = {"README.txt": README, "about.txt": about(prefs)}
    try:
        files["audio-outputs.txt"] = outputs()
    except Exception as error:  # noqa: BLE001
        files["audio-outputs.txt"] = f"The list of outputs couldn't be read: {error}\n"
    files["settings.json"] = json.dumps(redactor.value(prefs), indent=2, ensure_ascii=False) + "\n"
    for path in [folder / name for name in CRASH_LOGS] + logs.log_files(folder):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files[path.name] = redactor.text(text)
    return files


def write_zip(destination: Path, prefs: dict, folder: Path, outputs: Callable[[], str] = audio_outputs,
              home: str | None = None) -> Path:
    """Write the zip (whole, or not at all). Touches the disk and asks Windows for the outputs:
    call it off the window's thread. Raises OSError when it can't be written."""
    files = contents(prefs, folder, outputs, home)
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
