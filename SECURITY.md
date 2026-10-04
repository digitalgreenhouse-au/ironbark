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
- Rip verification (off unless you allow network access) talks to AccurateRip and CUETools DB.
- Ironbark writes to your files only in the Experimental tag editor, which proves the audio is
  untouched before replacing a file.

## Supported versions

Ironbark is in early development (0.x): fixes go into the latest release only.
