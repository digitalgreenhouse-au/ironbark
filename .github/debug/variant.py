"""Temporary (debug-hang.yml): take one of this branch's Library view changes back out."""
import sys
from pathlib import Path

path = Path("ironbark/library_view.py")
s = path.read_text(encoding="utf-8")
variants = sys.argv[1].split("+")
for variant in variants:
  if variant == "no-wrap":
    s = s.replace('self.status.bind("<Configure>", lambda e: theme.wrap_inside(self.status, e.width), add="+")', 'pass')
  elif variant == "old-status":
    s = s.replace('text = loadprogress.library_text(self.index.indexer.progress, time.time(), what)',
                  'text = "Looking through the library…"')
    s = s.replace('''text += " · " + loadprogress.library_text(self.index.indexer.progress, time.time(),
                                                          "checking for changes")''', 'text += " · checking for changes…"')
  elif variant == "old-paint":
    new = s.replace('''        if job is not None and (not total or job.get("filling")):
            what = "Rebuilding the library" if job.get("fresh") else "Building the library"
            text = loadprogress.library_text(self.index.indexer.progress, time.time(), what)
            if total:
                text = f"{total:,} albums so far · {text}"''', '''        if job is not None and not total:
            done, of = self.index.progress
            text = (f"Building the library index (first time only)… {done} of {of} files" if of
                    else f"Looking through {place}…")''')
    new = new.replace('''                text += " · " + loadprogress.library_text(self.index.indexer.progress, time.time(),
                                                          "checking for changes")''', '''                done, of = self.index.progress
                text += f" · checking for changes… {done} of {of}" if of else " · checking for changes…"''')
    assert new.count("first time only") == 1 and "checking for changes… {done}" in new
    s = new
  elif variant == "no-filling":
    s = s.replace('        if not (job.get("fresh") or job.get("first")):\n            return\n',
                  '        return\n')
  elif variant == "no-first":
    s = s.replace('job["first"] = bool(job["index"].unbuilt())', 'pass')
  else:
    raise SystemExit(f"no variant {variant}")
path.write_text(s, encoding="utf-8")
print("applied", variant)
