# Security

## Reporting a problem

Please report security problems **privately**, not in a public issue:

- on GitHub: the repository's **Security** tab → **Report a vulnerability**, or
- by email: info@digitalgreenhouse.com.au (subject "Ironbark security").

Tell us what you found, how to reproduce it, and which version you used (shown at the bottom of
Settings). We'll acknowledge it, keep you posted, and credit you in the fix's release notes if you'd
like.

## What's in scope

- Ironbark reads music files, cue sheets, playlists and images from your computer — a file crafted
  to crash or misuse those readers is in scope.
- Ironbark never uses the internet (since 0.38.0): no update check, no look-ups.
- The network playback cache (behind Settings → Library → Local network access, off by default)
  copies songs from the network drives your music is already on to this computer, and plays them
  from there. A crafted file on such a drive is in scope.
- Ironbark changes your files only when you ask: tag edits in the tag editor, which
  proves the audio is untouched before replacing a file, and playlists you edit, rename or delete
  (a deleted one goes to the Recycle Bin when its drive has one).

## Supported versions

Ironbark is in early development (0.x): fixes go into the latest release only.
