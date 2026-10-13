/* Ironbark for the web — turning the files someone picks (or drops) into albums of songs.

   Files never leave the computer: the page only gets File objects from the browser's own picker
   or a drag and drop, and reads them where they are.

   collect*() return [{ file, path }] — path is the folder path inside what was picked
   ("Album/CD1/01 Song.flac"), used for grouping and for finding cover pictures.
   buildAlbums() groups songs by folder, sorts them by disc, track and name, and finds each
   folder's cover picture. Works in a browser; the pure parts also in Node 18+. */
(function (root) {
  "use strict";

  const Tags = root.IronbarkTags || (typeof require !== "undefined" ? require("./tags.js") : null);
  const COVER_NAMES = ["cover", "folder", "front", "album", "albumart", "albumartlarge", "albumartsmall"];
  const IMAGE_EXT = new Set(["jpg", "jpeg", "png", "webp", "gif"]);

  function dirOf(path) {
    const i = path.lastIndexOf("/");
    return i < 0 ? "" : path.slice(0, i);
  }

  function nameOf(path) {
    return path.slice(path.lastIndexOf("/") + 1);
  }

  // -- collecting ----------------------------------------------------------------------------
  // From <input type=file multiple> or <input webkitdirectory>.
  function collectFileList(list) {
    const out = [];
    for (const file of Array.from(list || [])) {
      out.push({ file, path: (file.webkitRelativePath || file.name).replace(/\\/g, "/") });
    }
    return out;
  }

  // From the File System Access API (showDirectoryPicker): walks the folder.
  async function collectDirectoryHandle(handle, prefix, out, budget) {
    out = out || [];
    budget = budget || { files: 0 };
    prefix = prefix == null ? handle.name : prefix;
    for await (const entry of handle.values()) {
      if (budget.files > 200000) break;
      const path = prefix ? prefix + "/" + entry.name : entry.name;
      if (entry.kind === "file") {
        if (Tags.isMusicName(entry.name) || IMAGE_EXT.has(Tags.extension(entry.name))) {
          out.push({ file: await entry.getFile(), path });
          budget.files++;
        }
      } else if (entry.kind === "directory" && !entry.name.startsWith(".")) {
        await collectDirectoryHandle(entry, path, out, budget);
      }
    }
    return out;
  }

  // From a drop: DataTransfer items may be folders (webkitGetAsEntry), which are walked.
  async function collectDrop(dataTransfer) {
    const items = Array.from(dataTransfer.items || []);
    const entries = items.map((it) => (it.webkitGetAsEntry ? it.webkitGetAsEntry() : null));
    if (!entries.some(Boolean)) return collectFileList(dataTransfer.files);
    const out = [];
    const fileOf = (entry) => new Promise((res, rej) => entry.file(res, rej));
    const readAll = (dir) => new Promise((res) => {
      const reader = dir.createReader(), all = [];
      const next = () => reader.readEntries((batch) => {
        if (!batch.length) return res(all);
        all.push(...batch); next();
      }, () => res(all));
      next();
    });
    async function walk(entry, path) {
      if (!entry) return;
      if (entry.isFile) {
        if (Tags.isMusicName(entry.name) || IMAGE_EXT.has(Tags.extension(entry.name))) {
          try { out.push({ file: await fileOf(entry), path }); } catch (e) { /* unreadable: skip */ }
        }
      } else if (entry.isDirectory && !entry.name.startsWith(".")) {
        for (const e of await readAll(entry)) await walk(e, path + "/" + e.name);
      }
    }
    for (const e of entries) if (e) await walk(e, e.name);
    return out;
  }

  // -- grouping ------------------------------------------------------------------------------
  const collator = typeof Intl !== "undefined" ? new Intl.Collator(undefined, { numeric: true, sensitivity: "base" }) : null;
  const cmp = (a, b) => (collator ? collator.compare(a, b) : a < b ? -1 : a > b ? 1 : 0);

  function coverRank(name) {
    const stem = name.replace(/\.[^.]+$/, "").toLowerCase();
    const i = COVER_NAMES.indexOf(stem);
    if (i >= 0) return i;
    if (/cover|front|folder/.test(stem)) return 50;
    return 100;
  }

  // entries: [{ file, path }]. Returns { songs: [{ id, file, path, dir, name }], covers: Map(dir -> File) }
  function splitEntries(entries) {
    const songs = [], images = new Map();
    let id = 0;
    for (const e of entries) {
      const ext = Tags.extension(e.path);
      const dir = dirOf(e.path);
      if (Tags.isMusicName(e.path)) {
        songs.push({ id: "s" + (id++), file: e.file, path: e.path, dir, name: nameOf(e.path) });
      } else if (IMAGE_EXT.has(ext)) {
        const best = images.get(dir);
        if (!best || coverRank(nameOf(e.path)) < coverRank(nameOf(best.path))) images.set(dir, e);
      }
    }
    const covers = new Map();
    for (const [dir, e] of images) if (coverRank(nameOf(e.path)) < 100) covers.set(dir, e.file);
    // a "CD1" / "Disc 2" folder uses its album folder's cover
    for (const s of songs) {
      if (!covers.has(s.dir) && /^(cd|disc|disk)\s*\d+$/i.test(nameOf(s.dir)) && covers.has(dirOf(s.dir))) {
        covers.set(s.dir, covers.get(dirOf(s.dir)));
      }
    }
    return { songs, covers };
  }

  // The folder an album lives in ("CD1" / "Disc 2" folders count as their album's folder).
  function albumFolder(song) {
    return /^(cd|disc|disk)\s*\d+$/i.test(nameOf(song.dir)) ? dirOf(song.dir) : song.dir;
  }

  // The album a song belongs to: its album tag within its folder. A song with no album tag
  // joins its folder's album when the folder holds just one; otherwise it's grouped by folder.
  function albumKey(song, onlyAlbum) {
    const t = song.tags || {};
    const folder = albumFolder(song);
    const album = t.album || (onlyAlbum && onlyAlbum.get(folder)) || "";
    return folder + "\u0000" + album.toLowerCase();
  }

  // A disc's number: its tag, else a "CD 2" / "Disc 2" folder's number, else 1.
  function discOf(song) {
    const t = song.tags || {};
    if (t.disc) return t.disc;
    const m = /^(?:cd|disc|disk)\s*(\d+)$/i.exec(nameOf(song.dir));
    return m ? parseInt(m[1], 10) : 1;
  }

  function sortSongs(a, b) {
    const ta = a.tags || {}, tb = b.tags || {};
    const da = discOf(a), db = discOf(b);
    if (da !== db) return da - db;
    // no track tag: a number at the start of the file name ("03 Song.wav") stands in
    const lead = (s) => { const m = /^(\d{1,3})(?!\d)/.exec(s.name); return m ? parseInt(m[1], 10) : null; };
    const na = ta.track ?? lead(a), nb = tb.track ?? lead(b);
    if (na != null && nb != null && na !== nb) return na - nb;
    if (na != null && nb == null) return -1;
    if (na == null && nb != null) return 1;
    return cmp(a.path, b.path);
  }

  // songs (with .tags filled in) -> [{ key, title, artist, songs, cover, format }], in path order
  function buildAlbums(songs, covers) {
    const perFolder = new Map();
    for (const s of songs) {
      const a = s.tags && s.tags.album;
      if (!a) continue;
      const f = albumFolder(s);
      if (!perFolder.has(f)) perFolder.set(f, new Set());
      perFolder.get(f).add(a.toLowerCase());
    }
    const onlyAlbum = new Map();
    for (const [f, set] of perFolder) if (set.size === 1) onlyAlbum.set(f, [...set][0]);
    const map = new Map();
    for (const s of songs) {
      const k = albumKey(s, onlyAlbum);
      if (!map.has(k)) map.set(k, { key: k, songs: [], dir: s.dir });
      map.get(k).songs.push(s);
    }
    const albums = Array.from(map.values());
    for (const a of albums) {
      a.songs.sort(sortSongs);
      const t = a.songs.map((s) => s.tags || {});
      a.title = t.find((x) => x.album)?.album || nameOf(a.dir) || "Loose songs";
      const albumArtists = new Set(t.map((x) => x.albumArtist).filter(Boolean));
      const artists = new Set(t.map((x) => x.artist).filter(Boolean));
      a.artist = albumArtists.size === 1 ? [...albumArtists][0]
        : artists.size === 1 ? [...artists][0]
        : artists.size > 1 ? "Various artists" : "";
      a.cover = covers ? (covers.get(a.dir) || null) : null;
      // "16/44.1" when every song shares it, as the desktop player's album rows do
      const shapes = new Set(t.map((x) => (x.bitsPerSample && x.sampleRate ? x.bitsPerSample + "/" + formatRate(x.sampleRate) : x.format || "")));
      a.format = shapes.size === 1 ? [...shapes][0] : "mixed";
    }
    albums.sort((x, y) => cmp(x.dir, y.dir) || cmp(x.title, y.title));
    return albums;
  }

  function formatRate(hz) {
    const k = hz / 1000;
    return Number.isInteger(k) ? String(k) : String(Math.round(k * 10) / 10);
  }

  const api = { collectFileList, collectDirectoryHandle, collectDrop, splitEntries, buildAlbums, albumKey, sortSongs, discOf, formatRate, dirOf, nameOf, coverRank };
  root.IronbarkLibrary = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
