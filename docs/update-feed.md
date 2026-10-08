# Ironbark update feed

Ironbark can tell people when a newer version is out. It is **off by default**: it only runs when
someone switches on both Settings → Network → **Allow network access** and Settings → Help →
**Check for updates**. It then reads one small file, at most once a day:

```
https://www.digitalgreenhouse.com.au/ironbark/version.json
```

Publish that file (UTF-8 JSON, served over HTTPS) whenever a release goes out. Ironbark never
downloads or installs anything itself: when the file names a newer version, the status line says
"Ironbark X.Y.Z is available", and a click opens the `url` in the web browser.

## Format

```json
{
  "version": "1.0.0",
  "date": "15/11/2026",
  "url": "https://www.digitalgreenhouse.com.au/ironbark",
  "notes": "Opens files from Explorer's Open with, and tells you when something goes wrong."
}
```

| Field     | Required | What it is |
|-----------|----------|------------|
| `version` | yes      | The latest release, as `MAJOR.MINOR.PATCH` (a leading `v` is fine). Compared number by number with the running version. |
| `date`    | no       | The release date, Australian `dd/mm/yyyy`. |
| `url`     | no       | The download page. Must be a plain `https://` address on `digitalgreenhouse.com.au` or `www.digitalgreenhouse.com.au`: no port, no user name, no `@` or `\`, no spaces and only ASCII characters. Anything else (or nothing) opens `https://www.digitalgreenhouse.com.au/ironbark` instead. |
| `notes`   | no       | One or two plain sentences about what's new (the first 300 characters are used). |

Other fields are ignored, so more can be added later without breaking older copies.

## What the check sends

One HTTPS `GET` of the address above, with the User-Agent `Ironbark/<version>`. The web server sees
the computer's internet address, as with any request; nothing else about the person or their music
is sent. The file is only taken over HTTPS: if the website redirects the address to a plain `http://` one,
the answer is ignored. If the file is missing (404), the server doesn't answer (it gives up
after 30 seconds at the most) or there's no connection, Ironbark quietly tries again the next day.

## Testing a new file

Before publishing, check the file is valid JSON (for example paste it into a JSON validator) and
that `version` is exactly the new release's number. A file with a missing or malformed `version`
is ignored by every copy of Ironbark.
