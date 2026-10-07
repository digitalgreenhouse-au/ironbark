"""Diagnostics: one .zip the user saves and can send with a problem report. No UI code.

Settings → "Save diagnostics…" asks where to save it; nothing is sent anywhere. Inside:
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
CRASH_LOGS = ("crash.log", "crash.old.log")
README = f"""{APP_NAME} diagnostics

Made by {APP_NAME} at your request, to send with a problem report. Nothing has been sent anywhere:
you choose whether to share this file, and with whom.

about.txt          {APP_NAME}'s version, Windows' version, and the parts it's built from
audio-outputs.txt  the sound outputs connected when this was made
settings.json      your settings, with folder and file paths replaced by <folder 1>, <folder 2>...
crash.log          errors {APP_NAME} has met (if any)
app.log            what {APP_NAME} noted while running (more with Settings > Detailed logging)

In the logs, the same paths are replaced, and your home folder shows as <home>. Music file names a
log mentions in passing may still show: look through the files before sending if that matters.
"""


def default_name(now: datetime.datetime | None = None) -> str:
    now = now or datetime.datetime.now()
    return f"{APP_NAME}-diagnostics-{now:%Y%m%d-%H%M}.zip"


class Redactor:
    """Replaces paths with "<folder N>" (numbered in the order first met; the same path, whatever
    its letter case or slashes, always gets the same number)."""

    def __init__(self, home: str | None = None):
        self.names: dict[str, str] = {}   # normalised path → its stand-in
        self.originals: dict[str, str] = {}   # the path as first written → its stand-in
        self.home = home if home is not None else str(Path.home())

    @staticmethod
    def is_path(text) -> bool:
        return isinstance(text, str) and bool(_PATH.match(text))

    @staticmethod
    def _key(path: str) -> str:
        return path.replace("/", "\\").rstrip("\\").casefold()

    def name(self, path: str) -> str:
        key = self._key(path)
        if key not in self.names:
            self.names[key] = f"<folder {len(self.names) + 1}>"
        self.originals.setdefault(path, self.names[key])
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
        """A log's text with the paths already met replaced (longest first, so a folder inside
        another is replaced whole), then the home folder."""
        variants = {}
        for original, stand_in in self.originals.items():
            for form in {original, original.replace("\\", "/"), original.replace("/", "\\")}:
                variants[form.rstrip("\\/")] = stand_in
        for form in sorted(variants, key=len, reverse=True):
            if len(form) > 3:   # (a drive's root, "C:", would take every "C:" in the log with it)
                text = re.sub(re.escape(form), lambda _m, s=variants[form]: s, text, flags=re.IGNORECASE)
        if self.home and len(self.home) > 3:
            for form in {self.home, self.home.replace("\\", "/")}:
                text = re.sub(re.escape(form), "<home>", text, flags=re.IGNORECASE)
        return text


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
