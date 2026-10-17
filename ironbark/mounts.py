"""Linux: which of this computer's folders are a server's — from the mount table alone, so asking
never waits on a server (a `stat` on a share whose server has gone can hang for a minute or more).

Two kinds of network folder:
- A mounted file system whose type is a server's (`cifs`, `nfs`, `fuse.sshfs`, …), from
  /etc/fstab or `mount`: the server is the mount's source (`//nas/music`, `nas:/export/music`).
- The desktop's own: a share opened in the file manager (Nemo, Nautilus) appears under the
  `gvfsd-fuse` mount, `/run/user/<uid>/gvfs/`, one folder per share, named for it
  (`smb-share:server=nas,share=music`). The whole gvfs mount is one entry in the table, so the
  server is read from that folder's name.

The table is read again at most every `MOUNTS_CACHE_S` (a share mounted meanwhile is seen then),
and only by the caller's thread: reading it is a file in /proc, never the network. No UI.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass

# File systems that are a server's. (fuse.gvfsd-fuse: the desktop's SMB, SFTP, WebDAV… mounts.)
NETWORK_FILESYSTEMS = frozenset({
    "cifs", "smb3", "smbfs", "nfs", "nfs4", "ceph", "9p", "afs", "fuse.sshfs", "fuse.gvfsd-fuse",
    "fuse.rclone", "davfs", "fuse.davfs2", "fuse.smbnetfs", "glusterfs", "fuse.glusterfs",
})
# Of those, the ones served over SMB (the file-sharing port the cache's server check knocks on).
SMB_FILESYSTEMS = frozenset({"cifs", "smb3", "smbfs"})
MOUNTS = "/proc/self/mounts"
MOUNTS_CACHE_S = 5.0

_GVFS_SHARE = re.compile(r"^(?P<scheme>[a-z0-9+]+?)(?:-share)?:(?P<keys>.*)$")


@dataclass(frozen=True)
class Mount:
    point: str      # where it's mounted ("/mnt/music")
    kind: str       # its file system type ("cifs")
    source: str     # what's mounted there ("//nas/music", "nas:/export/music", "gvfsd-fuse")


@dataclass(frozen=True)
class Place:
    """Where a network folder's files really are: `root` is the folder that stands for the share
    (the mount point, or the share's own folder under gvfs); `server` its server, lower case
    ("" when it can't be told); `share` what on the server `root` is (an SMB share's name, lower
    case as SMB ignores case; an export's path), "" when unknown; `smb` whether it's reached over SMB."""
    root: str
    server: str
    smb: bool
    share: str = ""


def _unescape(field: str) -> str:
    """The mount table writes a space, tab, newline and backslash in octal (\\040 …)."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def parse(text: str) -> list[Mount]:
    found = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 3:
            found.append(Mount(_unescape(fields[1]), fields[2], _unescape(fields[0])))
    return found


def _inside(path: str, point: str) -> bool:
    return path == point or path.startswith(point.rstrip("/") + "/")


def mount_of(path: str, mounts: list[Mount]) -> Mount:
    """The mount `path` is under (the deepest one), from `mounts`; the root when none matches."""
    best = Mount("/", "", "")
    for mount in mounts:
        if _inside(path, mount.point) and len(mount.point) >= len(best.point):
            best = mount
    return best


def _of_source(kind: str, source: str) -> tuple[str, str]:
    """(server, share) named by a mount's source: `//nas/music` (SMB), `nas:/export` (NFS),
    `user@nas:/path` (sshfs). ("", "") when it names none."""
    if source.startswith("//"):
        server, _, share = source[2:].partition("/")
        return server.lower(), share.strip("/").lower()
    if ":" in source and not source.startswith("/"):
        host, _, share = source.partition(":")
        return host.rsplit("@", 1)[-1].strip("[]").lower(), share.strip("/")
    return "", ""


def _gvfs_place(path: str, point: str) -> Place | None:
    """The share a path under the gvfs mount belongs to, from the folder named for it."""
    rest = path[len(point.rstrip("/")) + 1:] if _inside(path, point) and path != point else ""
    if not rest:
        return None
    folder = rest.split("/", 1)[0]
    match = _GVFS_SHARE.match(folder)
    if match is None:
        return Place(point.rstrip("/") + "/" + folder, "", False)
    keys = dict(part.split("=", 1) for part in match.group("keys").split(",") if "=" in part)
    server = (keys.get("server") or keys.get("host") or "").lower()
    smb = match.group("scheme") == "smb"
    share = keys.get("share", "")
    return Place(point.rstrip("/") + "/" + folder, server, smb, share.lower() if smb else share.strip("/"))


def place_of(path: str, mounts: list[Mount]) -> Place | None:
    """The network folder `path` is in, or None when it's on this computer."""
    mount = mount_of(path, mounts)
    if mount.kind not in NETWORK_FILESYSTEMS and not mount.kind.startswith("fuse.gvfs"):
        return None
    if mount.kind.startswith("fuse.gvfs"):
        return _gvfs_place(path, mount.point)   # (None for the gvfs folder itself: no song lives there)
    server, share = _of_source(mount.kind, mount.source)
    return Place(mount.point, server, mount.kind in SMB_FILESYSTEMS, share)


class Table:
    """The mount table, read again when it's older than `MOUNTS_CACHE_S`. Safe from any thread."""

    def __init__(self, where: str = MOUNTS, clock=time.monotonic):
        self.where = where
        self._clock = clock
        self._lock = threading.Lock()
        self._mounts: list[Mount] = []
        self._read_at = -1e18

    def mounts(self) -> list[Mount]:
        with self._lock:
            if self._clock() - self._read_at < MOUNTS_CACHE_S:
                return self._mounts
        try:
            with open(self.where, encoding="utf-8", errors="surrogateescape") as table:
                found = parse(table.read())
        except OSError:
            found = []
        with self._lock:
            self._mounts, self._read_at = found, self._clock()
        return found

    def place_of(self, path: str) -> Place | None:
        return place_of(path, self.mounts())
