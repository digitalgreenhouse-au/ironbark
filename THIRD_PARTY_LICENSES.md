# Third-party software in Ironbark

Ironbark itself is MIT-licensed (see [LICENSE](LICENSE), © Digital Greenhouse). It runs on and
ships with the open-source software below. Licences were checked on 05/10/2026 from each
package's installed metadata and licence files (versions as locked in `uv.lock` on that date).
There is **no GPL code** in Ironbark: since 0.27.0 its music-tag reading and writing is its own
code (`ironbark/tagio/`, written from the published format specifications), replacing mutagen
(GPL-2.0-or-later). The native libraries inside the Windows build (every DLL and `.pyd`) were
listed and matched to their licences on 07/10/2026; the build now stops if one has no licence file
(`packaging/windows/licence_check.py`).

## Shipped with the app

| Package | Licence | Notes |
|---|---|---|
| [CPython](https://www.python.org) | PSF License | The Python runtime (3.12, the python-build-standalone build that uv installs). Its licence file also covers bzip2. |
| ↳ [OpenSSL](https://www.openssl.org) 3.5 | Apache-2.0 | Inside the Python runtime: secure connections (the rip check's AccurateRip lookup). |
| ↳ [libffi](https://github.com/libffi/libffi) 3.4 | MIT | Inside the Python runtime: calling Windows' own functions (ctypes). |
| ↳ [Expat](https://libexpat.github.io) 2.7 | MIT | Inside the Python runtime: reading XML (the CUETools DB's answer). |
| ↳ libmpdec ([mpdecimal](https://www.bytereef.org/mpdecimal/)) 2.5 | BSD-2-Clause | Inside the Python runtime (the decimal module). |
| ↳ liblzma ([XZ Utils](https://tukaani.org/xz/)) 5.8 | 0BSD | Inside the Python runtime (the lzma module). |
| ↳ [zlib](https://zlib.net) 1.3 | Zlib | Inside the Python runtime (the zlib module). |
| ↳ [SQLite](https://www.sqlite.org) 3.53 | Public domain (no licence needed; checked at sqlite.org/copyright.html on 07/10/2026) | Inside the Python runtime (the sqlite3 module): the library database and its song search. |
| Tcl/Tk 8.6 (with CPython's tkinter) | BSD-style (Tcl/Tk licence) | The windows and controls. |
| Microsoft Visual C++ runtime (`VCRUNTIME140*.dll`, `msvcp140*.dll`) | Microsoft's redistributable-code terms | The C/C++ run-time library the Python runtime and NumPy are built with; Microsoft allows it to be shipped with programs. |
| [NumPy](https://numpy.org) | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | Audio maths. Its licence file also covers the OpenBLAS, LAPACK and GCC run-time code it bundles. |
| [Pillow](https://python-pillow.org) | MIT-CMU | Cover art. Its licence file also covers the image libraries it bundles. |
| [soundfile](https://github.com/bastibe/python-soundfile) | BSD-3-Clause | Decoding (FLAC, WAV, AIFF, MP3, Ogg, ALAC). |
| ↳ libsndfile 1.2.2 (bundled with soundfile) | **LGPL-2.1** | Loaded at run time as a separate library file (DLL on Windows, dylib on a Mac) that can be replaced with another build — which is what the LGPL asks of software that uses it. Its own licence text travels with it. See "LGPL libraries" below. |
| ↳ ↳ libFLAC 1.4.3, libogg, libvorbis 1.3.7, libopus 1.5.2 (inside libsndfile) | BSD-3-Clause (Xiph.Org) | FLAC, Ogg Vorbis and Ogg Opus. |
| ↳ ↳ mpg123 (inside libsndfile) | **LGPL-2.1** | MP3 decoding. See "LGPL libraries" below. |
| ↳ ↳ LAME 3.100 (inside libsndfile) | **LGPL-2.0-or-later** | MP3 encoding (Ironbark only plays MP3s). See "LGPL libraries" below. |
| [sounddevice](https://github.com/spatialaudio/python-sounddevice) | MIT | Audio output. |
| ↳ PortAudio (bundled with sounddevice) | MIT | The audio-device layer. The `*-asio` DLL variants sounddevice also bundles contain Steinberg's ASIO SDK, which is under Steinberg's own licence terms; Ironbark doesn't use ASIO, so **leave the ASIO DLLs out of packaged builds**. |
| [tkinterdnd2](https://github.com/Eliav2/tkinterdnd2) | MIT | Drag and drop from Explorer. |
| ↳ tkdnd 2.10 (bundled with tkinterdnd2) | BSD-style (Tcl/Tk-style licence) | The Tk drag-and-drop extension. |
| [pycaw](https://github.com/AndreMiras/pycaw) | MIT | Windows only: Windows' audio settings. |
| [comtypes](https://github.com/enthought/comtypes) | MIT | Windows only: Windows' media controls and taskbar buttons, and used by pycaw. |
| [psutil](https://github.com/giampaolo/psutil) | BSD-3-Clause | Windows only: used by pycaw. |
| [jeepney](https://gitlab.com/takluyver/jeepney) | MIT | Linux only: talks to the desktop over D-Bus, for the media keys and the sound menu (MPRIS). Pure Python. |
| [cffi](https://cffi.readthedocs.io) | MIT-0 | Used by soundfile and sounddevice. |
| [pycparser](https://github.com/eliben/pycparser) | BSD-3-Clause | Used by cffi. |
| [typing-extensions](https://github.com/python/typing_extensions) | PSF-2.0 | Used by soundfile. |

## LGPL libraries: replacing them, and their source code

libsndfile (LGPL-2.1), and the mpg123 (LGPL-2.1) and LAME (LGPL-2.0-or-later) code built into it,
are in one separate library file: `_internal\_soundfile_data\libsndfile_x64.dll` in the Windows
build. Ironbark only calls it; you may replace that file with your own build of libsndfile (it
must keep the same name and export the same functions). Ironbark's own source code is public (MIT).

The library file comes unchanged from soundfile's package, built by the python-soundfile project
(https://github.com/bastibe/libsndfile-binaries) from the published sources of libsndfile
(https://github.com/libsndfile/libsndfile, version 1.2.2), mpg123 (https://www.mpg123.de) and LAME
(https://lame.sourceforge.io). If you'd like a copy of the source code of these LGPL libraries as
used in a version of Ironbark we distributed, ask Digital Greenhouse
(info@digitalgreenhouse.com.au) within three years of that version's release, and we'll provide it
for no more than the cost of sending it.

## Also in the Windows installer and portable zip

The packaged builds carry each component's licence text in `Ironbark\licenses\`: the Python
packages' own licence files, and for the native libraries above the texts kept in
`packaging/licenses/` (each file says where it was copied from).

| Tool | Licence | Notes |
|---|---|---|
| [PyInstaller](https://pyinstaller.org) bootloader | GPL-2.0-or-later **with the Bootloader Exception** | The small launcher inside `Ironbark.exe`. The exception allows it in programs under any licence, including commercial ones; PyInstaller's run-time hooks are Apache-2.0. Not used when running from source. |
| [Inno Setup](https://jrsoftware.org/isinfo.php) | Inno Setup License | Builds the installer; its setup and uninstall code is inside `Ironbark-Setup-X.Y.Z.exe` and `unins000.exe`, not in Ironbark itself. |

## The Linux build (`Ironbark-X.Y.Z-linux-x86_64.tar.gz`)

The Linux build is made on the distribution's own Python 3.12 and Tcl/Tk 8.6 (built with Xft, so
text is drawn with the desktop's fonts), not the python-build-standalone runtime the table above
describes. So the Python runtime, Tcl/Tk, PortAudio, OpenSSL, SQLite, Expat, libffi, liblzma, bzip2
and zlib inside it are the distribution's builds, under the same licences as above. It also carries
**BLT 2.5** (`libBLT`, MIT licence), a Tk extension the distribution's tkinter is linked to. The C
and C++ run-time libraries (`libgcc_s`, `libstdc++`) are GPL-3.0-or-later **with the GCC Runtime
Library Exception**, which allows them in programs under any licence. Every library in the build has
its licence text in `Ironbark/licenses/`: the Python packages' own files, the texts in
`packaging/licenses/`, and each system library's copyright file from its distribution package
(listed in `licenses/SYSTEM-LIBRARIES.txt`). The build stops if a library has no licence file or is
GPL without such an exception (`packaging/linux/licence_check.py`).

Left out on purpose and used from the system instead, as every Linux desktop has them: X11 and its
font libraries, ALSA, JACK, and Berkeley DB (`libdb`, which the distribution's PortAudio is linked to
but never calls). The Windows-only packages above (pycaw, comtypes, psutil, the Visual C++ runtime)
aren't in it.

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
