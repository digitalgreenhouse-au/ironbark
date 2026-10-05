# Changelog

Ironbark follows [semantic versioning](https://semver.org): MAJOR.MINOR.PATCH — a PATCH fixes
something, a MINOR adds a feature; MAJOR stays 0 until version 1.0. The version is shown at the
bottom of Settings. Dates are dd/mm/yyyy. Newest first.

## 0.28.5 — 05/10/2026

- Playlists mark a track from a CUE sheet with an `#IRONBARK-CUE:<track number>` line (was
  `#DG-CUE:`). Other players still skip the line and play the whole file.

## 0.28.4 — 05/10/2026

- Code comments now explain themselves instead of pointing to notes that aren't in this
  repository. No change to the player.

## 0.28.3 — 05/10/2026

- The automatic tests can also be started by hand from GitHub's Actions tab. No change to the
  player.

## 0.28.2 — 05/10/2026

**First public version.** Everything in the [README](README.md) is in this release.
- Fixed: scrolling Settings to a section could stop one step short at 100% display scaling.

## Before 0.28.2 — private development versions

Ironbark was developed privately by Digital Greenhouse (under the working name "DG FLAC Player"
until 0.26.0). The main steps:

- **0.1–0.3** — album playback, tabs, playlists, bit-perfect mode (WASAPI exclusive) with a
  sample-for-sample proof, dropout early warning, VU / PPM / RMS / true-peak meters, the PCM stream
  viewer.
- **0.4** — loudness meters (EBU R128 / ITU-R BS.1770), gapless playback.
- **0.5** — Settings → Experimental (new features off until switched on).
- **0.6** — Now Playing → Check: is the file genuine? Bit depth, lossy or upsampled sources,
  clipping, true peak, PLR, stereo figures, album DR, damaged files.
- **0.7** — Spectrogram window and bitscope.
- **0.8** — Stereo scope, PSR, VU alignment from loudness.
- **0.9** — HDCD, MQA and pre-emphasis detection.
- **0.10** — Compare two files; ABX blind test.
- **0.11** — Rip verification (AccurateRip, CUETools DB), behind an "Allow network access" switch.
- **0.12** — Test signals: THD+N, J-test, frequency response.
- **0.13** — Media keys, Windows' media panel, drag and drop.
- **0.14** — AIFF, Apple Lossless, Ogg Vorbis / Opus and CUE sheets.
- **0.15** — Saved library index, browse views, sortable columns, Play next.
- **0.16** — Tag editing (Experimental).
- **0.17** — Spotting new music.
- **0.18–0.23** — Design system, light theme, mini player, a polish of every window, in-app
  prompts and menus, the app icon.
- **0.24** — Clipping window: see (and, experimentally, hear) inter-sample overs; clipping guard
  measured at 8× oversampling, with a "My DAC has headroom" setting.
- **0.25** — Mini player: Settings from the strip, its own Keep on top.
- **0.26** — Renamed Ironbark.
- **0.27** — MIT licence; music tags read and written by Ironbark's own code (no GPL code).
- **0.28** — The Growth Rings icon at large sizes; contribution guide and automatic tests.
