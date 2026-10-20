"""Temporary (debug-hang.yml): take one of this branch's Library view changes back out."""
import sys
from pathlib import Path

path = Path("ironbark/library_view.py")
s = path.read_text(encoding="utf-8")
variant = sys.argv[1]
if variant == "old-status":
    s = s.replace('text = loadprogress.library_text(self.index.indexer.progress, time.time(), what)',
                  'text = "Looking through the library…"')
    s = s.replace('''text += " · " + loadprogress.library_text(self.index.indexer.progress, time.time(),
                                                          "checking for changes")''', 'text += " · checking for changes…"')
elif variant == "no-filling":
    s = s.replace('        if not (job.get("fresh") or job.get("first")):\n            return\n',
                  '        return\n')
elif variant == "no-first":
    s = s.replace('job["first"] = bool(job["index"].unbuilt())', 'pass')
else:
    raise SystemExit(f"no variant {variant}")
path.write_text(s, encoding="utf-8")
print("applied", variant)
