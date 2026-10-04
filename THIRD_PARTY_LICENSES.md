# Third-party software in Ironbark

Ironbark itself is MIT-licensed (see [LICENSE](LICENSE), © Digital Greenhouse). It runs on and
ships with the open-source software below. Licences were checked on 05/10/2026 from each
package's installed metadata and licence files (versions as locked in `uv.lock` on that date).
There is **no GPL code** in Ironbark: since 0.27.0 its music-tag reading and writing is its own
code (`ironbark/tagio/`, written from the published format specifications), replacing mutagen
(GPL-2.0-or-later).

## Shipped with the app

| Package | Licence | Notes |
|---|---|---|
| [CPython](https://www.python.org) | PSF License | The Python runtime. |
| Tcl/Tk (with CPython's tkinter) | BSD-style (Tcl/Tk licence) | The windows and controls. |
| [NumPy](https://numpy.org) | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | Audio maths. |
| [Pillow](https://python-pillow.org) | MIT-CMU | Cover art. |
| [soundfile](https://github.com/bastibe/python-soundfile) | BSD-3-Clause | Decoding (FLAC, WAV, AIFF, MP3, Ogg, ALAC). |
| ↳ libsndfile (bundled with soundfile) | **LGPL-2.1** | Loaded at run time as a separate library file (DLL on Windows, dylib on a Mac) that can be replaced with another build — which is what the LGPL asks of software that uses it. Its own licence text travels with it. |
| [sounddevice](https://github.com/spatialaudio/python-sounddevice) | MIT | Audio output. |
| ↳ PortAudio (bundled with sounddevice) | MIT | The audio-device layer. The `*-asio` DLL variants sounddevice also bundles contain Steinberg's ASIO SDK, which is under Steinberg's own licence terms; Ironbark doesn't use ASIO, so **leave the ASIO DLLs out of packaged builds**. |
| [tkinterdnd2](https://github.com/Eliav2/tkinterdnd2) | MIT | Drag and drop from Explorer. |
| ↳ tkdnd (bundled with tkinterdnd2) | BSD-style | The Tk drag-and-drop extension. |
| [pycaw](https://github.com/AndreMiras/pycaw) | MIT | Windows only: Windows' audio settings. |
| [comtypes](https://github.com/enthought/comtypes) | MIT | Windows only: used by pycaw. |
| [psutil](https://github.com/giampaolo/psutil) | BSD-3-Clause | Windows only: used by pycaw. |
| [cffi](https://cffi.readthedocs.io) | MIT-0 | Used by soundfile and sounddevice. |
| [pycparser](https://github.com/eliben/pycparser) | BSD-3-Clause | Used by cffi. |
| [typing-extensions](https://github.com/python/typing_extensions) | PSF-2.0 | Used by soundfile. |

## Used only for development and testing (not shipped)

| Tool | Licence | Notes |
|---|---|---|
| [pytest](https://pytest.org) | MIT | The test runner, with iniconfig (MIT), pluggy (MIT), packaging (Apache-2.0 OR BSD-2-Clause), Pygments (BSD-2-Clause) and colorama (BSD). |
| [FFmpeg](https://ffmpeg.org) (`ffmpeg`, `ffprobe`) | LGPL / GPL, depending on the build | Optional: some tests check that tags Ironbark writes are read by other software. Run as a separate program if it's installed; never shipped or linked. |

## Format specifications Ironbark's tag code follows

FLAC — RFC 9639 · Ogg — RFC 3533 · Ogg Opus — RFC 7845 · Vorbis I and Vorbis comments —
Xiph.Org · ID3v1, ID3v2.2, ID3v2.3, ID3v2.4 — id3.org · MPEG audio frame header and the
Xing / LAME "Info Tag" — mp3-tech.org · MP4 — ISO/IEC 14496-12 and Apple's QuickTime File
Format (metadata, "well-known types") · RIFF WAVE — Microsoft/IBM Multimedia Programming
Interface 1.0 · AIFF / AIFF-C — Apple. Details and dates are in the comments of each
`ironbark/tagio/` module.

## Format references (credited; no code copied)

Ironbark's own code was written from these projects' published descriptions of two file formats.
Neither project's code is included in Ironbark; both licences are permissive (checked 05/10/2026).

| Project | Used for | Licence |
|---|---|---|
| FFmpeg `libavfilter/af_hdcd.c` (Chris Moeller, Gumboot, Burt P.; from Christopher Key's HDCD decoder) | The HDCD control-packet format, for the HDCD flag in Now Playing → Check | BSD-3-Clause (the file's own header) |
| [MQA_identifier](https://github.com/purpl3F0x/MQA_identifier) (purpl3F0x) | The MQA sync word and rate code, for the MQA flag in Now Playing → Check | Apache-2.0 |
