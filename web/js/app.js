/* Ironbark for the web — the page: the song list, what plays next, the keys, settings,
   Details and the file check. The pieces it uses: IronbarkTags (tags.js), IronbarkLibrary
   (library.js), IronbarkPlayer (player.js), IronbarkMeters (meters.js), IronbarkVerify
   (verify.js). */
(function () {
  "use strict";

  const Tags = window.IronbarkTags, Lib = window.IronbarkLibrary, Verify = window.IronbarkVerify;
  const $ = (id) => document.getElementById(id);
  const ROW_H = 30;
  const PREFS_KEY = "ironbark.web.prefs";

  // -- settings (kept in this browser only) --------------------------------------------------
  const prefs = Object.assign({ theme: "dark", volume: 1, shuffle: false, repeat: "off", meters: false, metersMode: "levels", remember: true }, load());
  function load() {
    try { return JSON.parse(localStorage.getItem(PREFS_KEY) || "{}") || {}; } catch (e) { return {}; }
  }
  function save() {
    try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch (e) { /* private window: not kept */ }
  }

  // -- state ---------------------------------------------------------------------------------
  const player = new window.IronbarkPlayer();
  const meters = new window.IronbarkMeters.Meters($("meterCanvas"), player);
  let songs = [];            // every song in the list
  let nextId = 0;
  let covers = new Map();    // folder -> cover picture File
  let albums = [];
  let rows = [];             // what the list shows: { album } or { song, album }
  let queue = [];            // the play order (songs)
  let qi = -1;               // where in the queue we are
  let current = null;        // the song loaded in the player
  let selected = -1;         // index in rows
  let query = "";
  let reading = { total: 0, done: 0 };
  let listGen = 0;           // bumped when the list is cleared or replaced: older tag reads stop
  let baseOrder = [];        // the play order before shuffling (shuffle off goes back to it)
  let skipTimer = 0;         // the pending skip past a song that won't play
  let playGen = 0;           // the latest playAt call: an older one stops after its await
  let coverUrl = null;
  let failedInARow = 0;
  let seeking = false;
  const liveUrls = new Map();   // song id -> blob: URL (only the playing and the next song)

  // -- small helpers -------------------------------------------------------------------------
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function clock(sec) {
    if (!Number.isFinite(sec) || sec < 0) sec = 0;
    sec = Math.floor(sec);
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(s).padStart(2, "0");
  }
  function khz(hz) { return Lib.formatRate(hz) + " kHz"; }
  function channelsWord(n) { return n === 1 ? "mono" : n === 2 ? "stereo" : n ? n + " channels" : ""; }
  function fold(s) { return String(s || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase(); }
  function songTitle(s) { return (s.tags && s.tags.title) || s.name.replace(/\.[^.]+$/, ""); }
  function songArtist(s) { return (s.tags && (s.tags.artist || s.tags.albumArtist)) || ""; }
  function status(text) { $("status").textContent = text || ""; }

  function formatLine(t) {
    if (!t || !t.format) return "";
    const lossless = /FLAC|WAV|AIFF|ALAC/.test(t.format) || t.codec === "PCM" || t.codec === "PCM float";
    const parts = [t.format];
    if (lossless && t.bitsPerSample && t.sampleRate) parts.push(t.bitsPerSample + "-bit / " + khz(t.sampleRate));
    else {
      if (t.bitrate) parts.push(t.bitrate + " kbps");
      if (t.sampleRate) parts.push(khz(t.sampleRate));
    }
    if (t.channels) parts.push(channelsWord(t.channels));
    return parts.join(" · ");
  }

  // the formats as the browser's canPlayType knows them: the opening screen's list, and telling a
  // damaged file from a format the browser doesn't play
  const KINDS = [["FLAC", "audio/flac"], ["MP3", "audio/mpeg"], ["Ogg Vorbis", 'audio/ogg; codecs="vorbis"'], ["Opus", 'audio/ogg; codecs="opus"'],
    ["WAV", 'audio/wav; codecs="1"'], ["AAC", 'audio/mp4; codecs="mp4a.40.2"'], ["ALAC", 'audio/mp4; codecs="alac"'], ["AIFF", "audio/aiff"]];
  const probe = document.createElement("audio");
  function browserPlays(t) {
    const name = t.format === "FLAC (Ogg)" ? "FLAC" : t.format;
    const kind = KINDS.find((k) => k[0] === name);
    return !!(kind && probe.canPlayType(kind[1]));
  }

  function urlFor(song) {
    if (!liveUrls.has(song.id)) liveUrls.set(song.id, URL.createObjectURL(song.file));
    return liveUrls.get(song.id);
  }
  function keepOnly(...keep) {
    const ids = new Set(keep.filter(Boolean).map((s) => s.id));
    for (const [id, url] of liveUrls) if (!ids.has(id)) { URL.revokeObjectURL(url); liveUrls.delete(id); }
  }

  // -- theme ---------------------------------------------------------------------------------
  function applyTheme() {
    const root = document.documentElement;
    if (prefs.theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", prefs.theme);
    for (const b of $("themeSeg").querySelectorAll("button")) b.setAttribute("aria-checked", String(b.dataset.theme === prefs.theme));
    requestAnimationFrame(() => {
      const bg = getComputedStyle(document.body).backgroundColor;
      document.querySelector('meta[name="theme-color"]').setAttribute("content", bg);
      meters.readColours();
    });
  }
  matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => { if (prefs.theme === "system") applyTheme(); });

  // -- loading music -------------------------------------------------------------------------
  async function addEntries(entries, { replace }) {
    const split = Lib.splitEntries(entries);
    if (!split.songs.length) {
      status(entries.length ? "No music found there (Ironbark plays FLAC, MP3, Ogg, Opus, WAV, AIFF and M4A files)." : "");
      return;
    }
    if (replace) resetList();
    // ids stay unique when adding to what's there
    for (const s of split.songs) { s.id = "s" + (nextId++); s.tags = null; }
    songs = songs.concat(split.songs);
    for (const [k, v] of split.covers) if (!covers.has(k)) covers.set(k, v);
    $("app").classList.remove("empty");
    rebuild();
    await readAllTags(split.songs);
  }

  async function readAllTags(list) {
    const gen = listGen;
    reading.total += list.length;
    let i = 0, lastPaint = 0;
    const worker = async () => {
      while (i < list.length && gen === listGen) {
        const s = list[i++];
        try {
          s.tags = await Tags.readTags(s.file, { pictures: false });   // a cover is read when its song plays
        } catch (e) {
          s.tags = { format: Tags.extension(s.name).toUpperCase() };
        }
        delete s.tags.picture;
        if (gen !== listGen) return;           // the list was cleared or replaced meanwhile
        const t = s.tags;
        s.hay = fold([t.title, t.artist, t.album, t.albumArtist, t.genre, t.date, s.path].join(" \u0001 "));
        reading.done++;
        const now = performance.now();
        // redraw now and then while reading; less often for a big library (each redraw sorts it all)
        if (now - lastPaint > Math.max(400, songs.length / 20)) {
          lastPaint = now; rebuild();
          status("Reading tags… " + reading.done.toLocaleString() + " of " + reading.total.toLocaleString());
        }
      }
    };
    await Promise.all(Array.from({ length: 12 }, worker));
    if (gen !== listGen) return;
    if (reading.done >= reading.total) reading = { total: 0, done: 0 };
    rebuild();
    summary();
    if (current) {
      showNowPlaying(current, { keepCover: true });
      reorderQueue();
    }
  }

  // Tags can re-sort the list after a song has started (track numbers, albums): the play order
  // follows the list again, keeping the song that's playing.
  function reorderQueue() {
    const pos = new Map(rows.filter((r) => r.song).map((r, i) => [r.song, i]));
    const byList = (a, b) => (pos.get(a) ?? Infinity) - (pos.get(b) ?? Infinity);
    baseOrder = baseOrder.slice().sort(byList);
    if (!prefs.shuffle) {
      queue = baseOrder.slice();
      qi = queue.indexOf(current);
      preloadNext();
    }
  }

  function resetList() {
    listGen++;
    reading = { total: 0, done: 0 };
    clearTimeout(skipTimer);
    player.stop();
    keepOnly();
    songs = []; covers = new Map(); queue = []; baseOrder = []; qi = -1; current = null; selected = -1;
    showNowPlaying(null);
  }

  function summary() {
    if (reading.total) return;
    const n = songs.length, a = albums.length;
    const secs = songs.reduce((t, s) => t + ((s.tags && s.tags.duration) || 0), 0);
    status(n ? "Loaded " + n.toLocaleString() + " song" + (n === 1 ? "" : "s") + " from " + a.toLocaleString() + " album" + (a === 1 ? "" : "s") + (secs ? " · " + clock(secs) : "") : "");
  }

  async function openFolder({ replace }) {
    if (window.showDirectoryPicker) {
      let handle;
      try {
        handle = await window.showDirectoryPicker({ id: "ironbark-music", mode: "read" });
      } catch (e) {
        if (e && e.name === "AbortError") return;
        $("pickFolder").dataset.replace = replace ? "1" : "";
        $("pickFolder").click();
        return;
      }
      await openHandle(handle, { replace });
      if (prefs.remember) rememberHandle(handle);
      return;
    }
    $("pickFolder").dataset.replace = replace ? "1" : "";
    $("pickFolder").click();
  }

  async function openHandle(handle, { replace }) {
    status("Looking in “" + handle.name + "”…");
    try {
      const entries = await Lib.collectDirectoryHandle(handle);
      await addEntries(entries, { replace });
    } catch (e) {
      status("Couldn't read that folder: " + (e && e.message || e));
    }
  }

  function openFiles({ replace }) {
    $("pickFiles").dataset.replace = replace ? "1" : "";
    $("pickFiles").click();
  }

  for (const id of ["pickFiles", "pickFolder"]) {
    $(id).addEventListener("change", async (ev) => {
      const input = ev.target;
      const entries = Lib.collectFileList(input.files);
      const replace = input.dataset.replace === "1";
      input.value = "";
      if (entries.length) await addEntries(entries, { replace });
    });
  }

  // remembering the folder (browsers with the File System Access API)
  function idb() {
    return new Promise((res, rej) => {
      if (!window.indexedDB) return rej(new Error("no IndexedDB"));
      const r = indexedDB.open("ironbark-web", 1);
      r.onupgradeneeded = () => r.result.createObjectStore("handles");
      r.onsuccess = () => res(r.result);
      r.onerror = () => rej(r.error);
    });
  }
  async function rememberHandle(handle) {
    try {
      const db = await idb();
      db.transaction("handles", "readwrite").objectStore("handles").put(handle, "last");
    } catch (e) { /* not kept */ }
  }
  async function rememberedHandle() {
    try {
      const db = await idb();
      return await new Promise((res) => {
        const r = db.transaction("handles").objectStore("handles").get("last");
        r.onsuccess = () => res(r.result || null);
        r.onerror = () => res(null);
      });
    } catch (e) { return null; }
  }
  async function forgetHandle() {
    try { const db = await idb(); db.transaction("handles", "readwrite").objectStore("handles").delete("last"); } catch (e) { /* nothing kept */ }
  }
  async function offerReopen() {
    const b = $("wReopen");
    b.hidden = true;
    if (!prefs.remember || !window.showDirectoryPicker) return;
    const handle = await rememberedHandle();
    if (!handle) return;
    b.textContent = "Reopen “" + handle.name + "”";
    b.hidden = false;
    b.onclick = async () => {
      try {
        const ok = (await handle.queryPermission({ mode: "read" })) === "granted" || (await handle.requestPermission({ mode: "read" })) === "granted";
        if (ok) await openHandle(handle, { replace: true });
      } catch (e) {
        status("That folder can't be opened any more. Open it again with “Open a folder”.");
        forgetHandle();
        b.hidden = true;
      }
    };
  }

  // -- the list ------------------------------------------------------------------------------
  function matches(s, words) {
    if (!words.length) return true;
    const hay = s.hay || (s.hay = fold(s.path));   // the song's words, folded once when its tags are read
    return words.every((w) => hay.includes(w));
  }

  function rebuild() {
    albums = Lib.buildAlbums(songs, covers);
    const words = fold(query).split(/\s+/).filter(Boolean);
    const selSong = rows[selected] && (rows[selected].song || null);
    rows = [];
    for (const a of albums) {
      const shown = a.songs.filter((s) => matches(s, words));
      if (!shown.length) continue;
      rows.push({ album: a });
      const multiDisc = new Set(a.songs.map(Lib.discOf)).size > 1;
      for (const s of shown) {
        const t = s.tags || {};
        const track = t.track ?? Lib.leadNumber(s.name);   // no track tag: "03 Song.wav" stands in, as in the sort
        rows.push({ song: s, album: a, num: track != null ? (multiDisc ? Lib.discOf(s) + "." + track : track) : "" });
      }
    }
    selected = selSong ? rows.findIndex((r) => r.song === selSong) : Math.min(selected, rows.length - 1);
    $("listSpacer").style.height = rows.length * ROW_H + "px";
    renderList();
    if (!songs.length) $("app").classList.add("empty");
  }

  function renderList() {
    const list = $("list");
    const top = list.scrollTop, h = list.clientHeight || 400;
    const first = Math.max(0, Math.floor(top / ROW_H) - 8), last = Math.min(rows.length, Math.ceil((top + h) / ROW_H) + 8);
    let html = "";
    for (let i = first; i < last; i++) {
      const r = rows[i], y = i * ROW_H;
      if (!r.song) {
        const a = r.album;
        html += `<div class="row-album${i === selected ? " sel" : ""}" data-i="${i}" style="top:${y}px" title="${esc(a.dir)}"><span class="a-title">${esc(a.title)}${a.artist ? " · " + esc(a.artist) : ""}</span><span class="a-fmt">${esc(a.format)}</span></div>`;
      } else {
        const s = r.song, t = s.tags || {};
        const cls = "row-song" + (i === selected ? " sel" : "") + (s === current ? " cur" : "") + (s.bad ? " bad" : "");
        const artist = songArtist(s), albumArtist = r.album.artist;
        const extra = artist && artist !== albumArtist ? ` <small>· ${esc(artist)}</small>`
          : !t.title && t.format ? ` <small>· ${esc(t.format)}</small>` : "";
        html += `<div class="${cls}" data-i="${i}" style="top:${y}px" role="option" aria-selected="${i === selected}" title="${esc(s.bad || s.path)}"><span class="n">${esc(r.num)}</span><span class="t">${esc(songTitle(s))}${extra}</span><span class="d">${t.duration ? clock(t.duration) : ""}</span></div>`;
      }
    }
    if (!rows.length && songs.length) html = `<div class="list-empty">No songs match “${esc(query)}”.</div>`;
    // the inline top positions are set through CSSOM so the page's style lock allows them
    const box = $("listRows");
    box.innerHTML = html.replace(/ style="top:(\d+)px"/g, ' data-top="$1"');
    for (const el of box.children) if (el.dataset.top) el.style.top = el.dataset.top + "px";
  }

  $("list").addEventListener("scroll", () => requestAnimationFrame(renderList), { passive: true });
  new ResizeObserver(() => renderList()).observe($("list"));

  function rowAt(ev) {
    const el = ev.target.closest("[data-i]");
    return el ? parseInt(el.dataset.i, 10) : -1;
  }
  $("listRows").addEventListener("click", (ev) => { const i = rowAt(ev); if (i >= 0) select(i); });
  $("listRows").addEventListener("dblclick", (ev) => { const i = rowAt(ev); if (i >= 0) playRow(i); });

  function select(i, { scroll } = {}) {
    if (!rows.length) return;
    selected = Math.max(0, Math.min(rows.length - 1, i));
    if (scroll) {
      const list = $("list"), y = selected * ROW_H;
      if (y < list.scrollTop) list.scrollTop = y;
      else if (y + ROW_H > list.scrollTop + list.clientHeight) list.scrollTop = y + ROW_H - list.clientHeight;
    }
    // change the rows already drawn rather than drawing them again: a double-click needs both
    // clicks to land on the same element
    for (const el of $("listRows").children) {
      const on = parseInt(el.dataset.i, 10) === selected;
      el.classList.toggle("sel", on);
      if (el.classList.contains("row-song")) el.setAttribute("aria-selected", String(on));
    }
  }

  function visibleSongs() { return rows.filter((r) => r.song).map((r) => r.song); }

  function playRow(i) {
    const r = rows[i];
    if (!r) return;
    const order = visibleSongs();
    const song = r.song || r.album.songs.find((s) => order.includes(s));
    if (!song) return;
    startQueue(order, song);
  }

  // -- the play order ------------------------------------------------------------------------
  function shuffled(list, first) {
    const rest = list.filter((s) => s !== first);
    for (let i = rest.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [rest[i], rest[j]] = [rest[j], rest[i]]; }
    return first ? [first, ...rest] : rest;
  }

  function startQueue(order, song) {
    baseOrder = order.slice();
    queue = prefs.shuffle ? shuffled(order, song) : order.slice();
    qi = queue.indexOf(song);
    failedInARow = 0;
    playAt(qi);
  }

  function nextIndex(auto) {
    if (!queue.length) return -1;
    if (auto && prefs.repeat === "one") return qi;
    if (qi + 1 < queue.length) return qi + 1;
    if (prefs.repeat === "all" || (!auto && prefs.repeat === "one")) return 0;
    return -1;
  }

  async function playAt(i, { autoplay = true } = {}) {
    const song = queue[i];
    if (!song) return;
    const gen = ++playGen;
    clearTimeout(skipTimer);
    qi = i;
    current = song;
    keepOnly(song, player.spareUrl ? findByUrl(player.spareUrl) : null);
    player.ensureContext();
    meters.resetPeaks();
    meters.mono = !!(song.tags && song.tags.channels === 1);
    await player.load(urlFor(song), { autoplay });
    if (gen !== playGen) return;              // another song was started meanwhile: it carries on
    showNowPlaying(song);
    renderList();
    scrollToCurrent();
    preloadNext();
  }

  // Loads the song after this one into the spare player; frees every other song's address.
  function preloadNext() {
    if (!current) return;
    const next = queue[nextIndex(true)] || null;
    if (next && next !== current) player.preload(urlFor(next));
    else player.forgetSpare();
    keepOnly(current, next);
  }

  function findByUrl(url) {
    for (const [id, u] of liveUrls) if (u === url) return songs.find((s) => s.id === id) || null;
    return null;
  }

  function scrollToCurrent() {
    const i = rows.findIndex((r) => r.song === current);
    if (i < 0) return;
    const list = $("list"), y = i * ROW_H;
    if (y < list.scrollTop || y + ROW_H > list.scrollTop + list.clientHeight) list.scrollTop = Math.max(0, y - list.clientHeight / 3);
  }

  function next(auto) {
    const i = nextIndex(auto);
    if (i < 0) {
      if (auto) { player.pause(); player.seek(0); updatePlayButton(); }
      return;
    }
    if (auto && i === qi) { player.seek(0); player.play(); return; }
    playAt(i);
  }

  function previous() {
    if (player.position > 3 || qi <= 0) {
      if (qi < 0 && rows.length) return playRow(Math.max(0, selected));
      player.seek(0);
      return;
    }
    playAt(qi - 1);
  }

  function togglePlay() {
    if (!current) {
      if (!rows.length) return openFolder({ replace: true });
      return playRow(selected >= 0 ? selected : 0);
    }
    player.toggle();
  }

  player.addEventListener("ended", () => next(true));
  player.addEventListener("state", updatePlayButton);
  player.addEventListener("time", updateTime);
  player.addEventListener("loaded", () => { updateTime(); updateOutputLine(); });
  player.addEventListener("error", (ev) => {
    const failed = current;
    if (!failed || failed.failedAt === playGen) return;   // one report per try, however the browser says it
    failed.failedAt = playGen;
    const t = failed.tags || {};
    const what = t.codec && t.format !== t.codec ? t.format + " (" + t.codec + ")" : t.format || Tags.extension(failed.name).toUpperCase();
    // a format this browser plays, or a decode error partway in (code 3): the file itself is at fault
    const code = ev.detail && ev.detail.code;
    failed.bad = code === -1 ? "Couldn't start: " + ev.detail.message
      : t.format && (code === 3 || browserPlays(t)) ? "This " + what + " file looks damaged: the browser couldn't decode it"
      : t.format ? "This browser can't play " + what + " files" : "This browser can't play this file (damaged, or not music)";
    setOutline(failed.bad + ".", "bad");
    renderList();
    failedInARow++;
    clearTimeout(skipTimer);
    if (failedInARow < 5 && nextIndex(false) >= 0 && nextIndex(false) !== qi) {
      skipTimer = setTimeout(() => { if (current === failed) next(false); }, 700);
    }
  });
  player.addEventListener("state", () => { if (player.playing) failedInARow = 0; });

  // -- now playing ---------------------------------------------------------------------------
  async function showNowPlaying(song, { keepCover } = {}) {
    const t = (song && song.tags) || {};
    $("title").textContent = song ? songTitle(song) : "Ironbark";
    const artist = song ? songArtist(song) : "";
    const album = t.album || (song ? Lib.nameOf(song.dir) : "");
    $("sub").textContent = song ? [artist, album].filter(Boolean).join(" — ") : "Your music, on your computer";
    $("fmt").textContent = song ? formatLine(t) : "";
    $("seek").disabled = !song;
    $("dot").hidden = !(song && song.check);
    if (song && song.check) $("dot").className = "dot " + song.check.cls;
    updateOutputLine();
    document.title = song ? songTitle(song) + (artist ? " — " + artist : "") + " · Ironbark" : "Ironbark";
    if (!keepCover) await showCover(song);
    mediaSession(song);
  }

  async function showCover(song) {
    const box = $("coverBox"), img = $("cover");
    if (coverUrl) { URL.revokeObjectURL(coverUrl); coverUrl = null; }
    box.classList.remove("has-art");
    img.removeAttribute("src");
    if (!song) return;
    let blob = null;
    if (song.tags && song.tags.hasPicture) {
      try {
        const full = await Tags.readTags(song.file);
        if (full.picture) blob = new Blob([full.picture.data], { type: full.picture.mime });
      } catch (e) { /* no picture after all */ }
    }
    if (!blob) blob = covers.get(song.dir) || null;
    if (!blob || song !== current) return;
    coverUrl = URL.createObjectURL(blob);
    img.onload = () => box.classList.add("has-art");
    img.src = coverUrl;
    mediaSession(song);
  }

  function setOutline(text, cls) {
    const el = $("outline");
    el.textContent = text || "";
    el.className = "outline" + (cls ? " " + cls : "");
  }

  function updateOutputLine() {
    if (!current) return setOutline("");
    if (current.bad) return setOutline(current.bad + ".", "bad");
    const out = player.outputRate, rate = current.tags && current.tags.sampleRate;
    if (!out) return setOutline("");
    if (rate && rate !== out) setOutline("Browser plays it at " + khz(out) + " (resampled from " + khz(rate) + ")", "warn");
    else setOutline("Browser plays it at " + khz(out) + " · not bit-perfect", "");
    $("outline").title = "A browser mixes the sound of every page and plays it at the rate the computer's output is set to. The desktop Ironbark plays bit-perfect.";
  }

  function updatePlayButton() {
    const playing = player.playing;
    $("playIcon").setAttribute("href", playing ? "#i-pause" : "#i-play");
    $("btnPlay").title = playing ? "Pause (Space)" : "Play (Space)";
    $("btnPlay").setAttribute("aria-label", playing ? "Pause" : "Play");
    if ("mediaSession" in navigator) navigator.mediaSession.playbackState = current ? (playing ? "playing" : "paused") : "none";
    if (playing && prefs.meters) meters.start();
  }

  function updateTime() {
    const d = player.duration || (current && current.tags && current.tags.duration) || 0;
    const p = player.position;
    if (!seeking) {
      const v = d ? Math.round((p / d) * 1000) : 0;
      $("seek").value = v;
      $("seek").style.setProperty("--pct", v / 10 + "%");
      $("tPos").textContent = clock(p);
    }
    $("tLeft").textContent = "−" + clock(Math.max(0, d - p));
    if ("mediaSession" in navigator && navigator.mediaSession.setPositionState && d) {
      try { navigator.mediaSession.setPositionState({ duration: d, position: Math.min(p, d), playbackRate: 1 }); } catch (e) { /* ignore */ }
    }
  }

  const seek = $("seek");
  seek.addEventListener("input", () => {
    seeking = true;
    const d = player.duration;
    seek.style.setProperty("--pct", seek.value / 10 + "%");
    $("tPos").textContent = clock((seek.value / 1000) * d);
  });
  seek.addEventListener("change", () => {
    seeking = false;
    player.seek((seek.value / 1000) * player.duration);
  });

  function mediaSession(song) {
    if (!("mediaSession" in navigator)) return;
    if (!song) { navigator.mediaSession.metadata = null; return; }
    const t = song.tags || {};
    try {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: songTitle(song), artist: songArtist(song), album: t.album || "",
        artwork: coverUrl ? [{ src: coverUrl, sizes: "512x512" }] : [{ src: "icons/icon_256.png", sizes: "256x256", type: "image/png" }],
      });
    } catch (e) { /* older browser */ }
  }
  if ("mediaSession" in navigator) {
    const ms = navigator.mediaSession;
    const on = (a, f) => { try { ms.setActionHandler(a, f); } catch (e) { /* not supported */ } };
    on("play", () => togglePlay());
    on("pause", () => player.pause());
    on("previoustrack", () => previous());
    on("nexttrack", () => next(false));
    on("seekbackward", (d) => player.seek(player.position - (d.seekOffset || 10)));
    on("seekforward", (d) => player.seek(player.position + (d.seekOffset || 10)));
    on("seekto", (d) => player.seek(d.seekTime));
    on("stop", () => player.pause());
  }

  // -- buttons -------------------------------------------------------------------------------
  $("btnPlay").addEventListener("click", togglePlay);
  $("btnNext").addEventListener("click", () => next(false));
  $("btnPrev").addEventListener("click", previous);
  $("btnShuffle").addEventListener("click", toggleShuffle);
  $("btnRepeat").addEventListener("click", cycleRepeat);
  $("wFolder").addEventListener("click", () => openFolder({ replace: true }));
  $("wFiles").addEventListener("click", () => openFiles({ replace: true }));

  function toggleShuffle() {
    prefs.shuffle = !prefs.shuffle; save();
    if (current) {
      // shuffle the list that's playing; shuffle off goes back to its own order
      const order = prefs.shuffle ? shuffled(baseOrder, current) : baseOrder.slice();
      queue = order.includes(current) ? order : [current, ...order];
      qi = queue.indexOf(current);
      preloadNext();
    }
    showModes();
    status(prefs.shuffle ? "Shuffle on" : "Shuffle off");
  }

  function cycleRepeat() {
    prefs.repeat = prefs.repeat === "off" ? "all" : prefs.repeat === "all" ? "one" : "off"; save();
    showModes();
    status({ off: "Repeat off", all: "Repeat the list", one: "Repeat the song" }[prefs.repeat]);
    preloadNext();
  }

  function showModes() {
    $("btnShuffle").setAttribute("aria-pressed", String(prefs.shuffle));
    $("btnRepeat").setAttribute("aria-pressed", String(prefs.repeat !== "off"));
    $("repeatIcon").setAttribute("href", prefs.repeat === "one" ? "#i-repeat-one" : "#i-repeat");
    $("btnRepeat").title = { off: "Repeat: off (R)", all: "Repeat: the list (R)", one: "Repeat: the song (R)" }[prefs.repeat];
  }

  // -- pop-up menus --------------------------------------------------------------------------
  let openMenu = null;
  function showMenu(menu, anchor) {
    closeMenu();
    menu.hidden = false;
    const r = anchor.getBoundingClientRect(), m = menu.getBoundingClientRect();
    const left = Math.max(8, Math.min(window.innerWidth - m.width - 8, r.right - m.width));
    const top = r.bottom + 6 + m.height > window.innerHeight ? r.top - m.height - 6 : r.bottom + 6;
    menu.style.left = left + "px";
    menu.style.top = Math.max(8, top) + "px";
    openMenu = { menu, anchor };
    const first = menu.querySelector("button, input");
    if (first) first.focus();
  }
  function closeMenu() {
    if (!openMenu) return;
    openMenu.menu.hidden = true;
    const a = openMenu.anchor;
    openMenu = null;
    if (document.activeElement && document.activeElement.closest(".menu")) a.focus();
  }
  document.addEventListener("pointerdown", (ev) => {
    if (openMenu && !openMenu.menu.contains(ev.target) && !openMenu.anchor.contains(ev.target)) closeMenu();
  });

  $("btnAdd").addEventListener("click", (ev) => (openMenu && openMenu.menu === $("addMenu") ? closeMenu() : showMenu($("addMenu"), ev.currentTarget)));
  $("addMenu").addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    closeMenu();
    const act = b.dataset.act;
    if (act === "open-folder") openFolder({ replace: true });
    else if (act === "open-files") openFiles({ replace: true });
    else if (act === "add-folder") openFolder({ replace: !songs.length });
    else if (act === "add-files") openFiles({ replace: !songs.length });
    else if (act === "clear") clearList();
  });
  $("addMenu").addEventListener("keydown", (ev) => menuKeys(ev, $("addMenu")));

  function menuKeys(ev, menu) {
    const items = Array.from(menu.querySelectorAll("button"));
    const i = items.indexOf(document.activeElement);
    if (ev.key === "ArrowDown") { items[(i + 1) % items.length].focus(); ev.preventDefault(); }
    else if (ev.key === "ArrowUp") { items[(i - 1 + items.length) % items.length].focus(); ev.preventDefault(); }
    else if (ev.key === "Escape") { closeMenu(); ev.preventDefault(); ev.stopPropagation(); }
  }

  function clearList() {
    resetList();
    query = "";
    $("search").value = "";
    rebuild();
    status("");
    offerReopen();
  }

  // volume
  const vol = $("vol");
  $("btnVol").addEventListener("click", (ev) => (openMenu && openMenu.menu === $("volMenu") ? closeMenu() : showMenu($("volMenu"), ev.currentTarget)));
  vol.addEventListener("input", () => setVolume(vol.value / 100));
  $("volMute").addEventListener("click", () => setMuted(!player.muted));
  $("volMenu").addEventListener("keydown", (ev) => { if (ev.key === "Escape") { closeMenu(); ev.stopPropagation(); } });

  function setVolume(v) {
    player.volume = v;
    if (v > 0 && player.muted) player.muted = false;
    prefs.volume = player.volume; save();
    showVolume();
  }
  function setMuted(m) { player.muted = m; showVolume(); }
  function showVolume() {
    const pct = Math.round(player.volume * 100);
    const pill = $("btnVol");
    pill.textContent = player.muted ? "Muted" : pct + "%";
    pill.classList.toggle("reduced", !player.muted && pct < 100);
    pill.classList.toggle("muted", player.muted);
    vol.value = pct;
    vol.style.setProperty("--pct", pct + "%");
    const g = player.gainValue;
    $("volDb").textContent = player.muted ? "Muted" : g >= 1 ? "0 dB · untouched" : g <= 0 ? "Silent" : (20 * Math.log10(g)).toFixed(1) + " dB";
    $("volMute").textContent = player.muted ? "Unmute" : "Mute";
  }

  // meters
  function showMeters(on) {
    prefs.meters = on; save();
    $("meters").hidden = !on;
    $("btnMeters").setAttribute("aria-pressed", String(on));
    if (on) { meters.readColours(); meters.start(); } else meters.stop();
    renderList();
  }
  function meterMode(mode) {
    prefs.metersMode = mode; save();
    meters.mode = mode;
    $("mLevels").setAttribute("aria-selected", String(mode === "levels"));
    $("mSpectrum").setAttribute("aria-selected", String(mode === "spectrum"));
  }
  $("btnMeters").addEventListener("click", () => showMeters($("meters").hidden));
  $("mLevels").addEventListener("click", () => meterMode("levels"));
  $("mSpectrum").addEventListener("click", () => meterMode("spectrum"));
  $("mReset").addEventListener("click", () => meters.resetPeaks());

  // search
  const search = $("search");
  let searchTimer = 0;
  search.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { query = search.value; $("list").scrollTop = 0; selected = -1; rebuild(); }, 120);
  });
  search.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") { search.value = ""; query = ""; rebuild(); $("list").focus(); ev.preventDefault(); }
    else if (ev.key === "ArrowDown" || ev.key === "Enter") {
      ev.preventDefault();
      $("list").focus();
      const first = rows.findIndex((r) => r.song);
      if (first >= 0) { select(first, { scroll: true }); if (ev.key === "Enter") playRow(first); }
    }
  });

  // -- dialogs -------------------------------------------------------------------------------
  for (const d of document.querySelectorAll("dialog")) {
    d.addEventListener("click", (ev) => {
      if (ev.target.closest("[data-close]") || ev.target === d) d.close();
    });
  }
  function openDialog(d) { closeMenu(); if (!d.open) d.showModal(); }

  $("btnSettings").addEventListener("click", () => openSettings());
  function openSettings() {
    $("optRemember").checked = prefs.remember;
    $("optRemember").disabled = !window.showDirectoryPicker;
    if (!window.showDirectoryPicker) $("rememberNote").textContent = "This browser can't keep a folder between visits: open it again each time.";
    $("optMeters").checked = prefs.meters;
    $("about").textContent = "Ironbark for the web " + (window.IronbarkVersion || "") + " · free and open source (MIT licence) · made by Digital Greenhouse in Australia.";
    openDialog($("dlgSettings"));
  }
  $("themeSeg").addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    prefs.theme = b.dataset.theme; save(); applyTheme();
  });
  $("optRemember").addEventListener("change", (ev) => {
    prefs.remember = ev.target.checked; save();
    if (!prefs.remember) forgetHandle();
  });
  $("optMeters").addEventListener("change", (ev) => { prefs.meters = ev.target.checked; save(); showMeters(prefs.meters); });
  $("btnKeys").addEventListener("click", () => { $("dlgSettings").close(); openKeys(); });

  $("btnInfo").addEventListener("click", () => openDetails());
  function openDetails() {
    const s = current || (rows[selected] && rows[selected].song);
    const body = $("detailsBody"), foot = $("detailsFoot");
    foot.innerHTML = "";
    foot.dataset.song = "";
    if (!s) {
      body.innerHTML = `<p class="about">Play or pick a song to see its details.</p>`;
      openDialog($("dlgDetails"));
      return;
    }
    const t = s.tags || {};
    const out = player.outputRate;
    const row = (k, v, cls) => (v == null || v === "" ? "" : `<tr><th>${esc(k)}</th><td${cls ? ` class="${cls}"` : ""}>${v}</td></tr>`);
    const sizeMb = s.file.size < 1048576 ? Math.max(1, Math.round(s.file.size / 1024)) + " KB"
      : (s.file.size / 1048576).toFixed(s.file.size > 104857600 ? 0 : 1) + " MB";
    let outText = null, outCls = "";
    if (s === current && out) {
      if (t.sampleRate && t.sampleRate !== out) { outText = esc("Browser, " + khz(out) + ": resampled from " + khz(t.sampleRate) + ", mixed with other sound — not bit-perfect"); outCls = "warn"; }
      else outText = esc("Browser, " + khz(out) + ": mixed with other sound — not bit-perfect");
    }
    body.innerHTML = `<table class="facts">
      ${row("Title", esc(songTitle(s)))}
      ${row("Artist", esc(t.artist))}
      ${row("Album", esc(t.album))}
      ${row("Album artist", t.albumArtist && t.albumArtist !== t.artist ? esc(t.albumArtist) : null)}
      ${row("Track", t.track != null ? esc(t.track + (t.trackTotal ? " of " + t.trackTotal : "")) : null)}
      ${row("Disc", t.disc != null ? esc(t.disc + (t.discTotal ? " of " + t.discTotal : "")) : null)}
      ${row("Year", esc(t.date))}
      ${row("Genre", esc(t.genre))}
      ${row("Format", esc([t.format, t.codec && t.codec !== t.format ? t.codec : null].filter(Boolean).join(" · ")))}
      ${row("Sample rate", t.sampleRate ? esc(khz(t.sampleRate)) : null)}
      ${row("Bit depth", t.bitsPerSample ? esc(t.bitsPerSample + "-bit") : null)}
      ${row("Channels", t.channels ? esc(channelsWord(t.channels)) : null)}
      ${row("Length", t.duration ? esc(clock(t.duration)) : null)}
      ${row("Bit rate", t.bitrate ? esc(t.bitrate.toLocaleString() + " kbps") : null)}
      ${row("Output", outText, outCls)}
      ${row("File", esc(s.path) + " · " + esc(sizeMb))}
      ${row("MD5 (stored)", t.md5 ? `<code>${esc(t.md5)}</code>` : null)}
      ${row("Problem", s.bad ? esc(s.bad) : null, "bad")}
    </table>`;
    if (t.format === "FLAC") {
      foot.innerHTML = `<div class="verify"><div class="v-head"><button class="btn" id="btnVerify">Check this file</button><span class="v-verdict" id="vVerdict"></span><progress id="vProgress" max="1" value="0" hidden></progress></div><p id="vDetail">Decodes the whole file and compares it with the fingerprint (MD5) its encoder stored. A match proves every sample is intact.</p></div>`;
      foot.dataset.song = s.id;
      if (s.check) showCheck(s.check);
      if (checking) showChecking(s);
      $("btnVerify").addEventListener("click", () => runCheck(s));
    }
    openDialog($("dlgDetails"));
  }

  function showCheck(c) {
    const v = $("vVerdict");
    if (!v) return;
    v.textContent = (c.cls === "good" ? "✓ " : c.cls === "bad" ? "✗ " : "") + c.verdict;
    v.className = "v-verdict " + c.cls;
    $("vDetail").textContent = c.detail;
  }

  // One check at a time (it can hold a lot of memory); its progress and result go to the Details
  // sheet only while that sheet still shows the same song.
  let checking = null;
  const sheetShows = (s) => $("dlgDetails").open && $("detailsFoot").dataset.song === s.id && $("vVerdict");

  async function runCheck(s) {
    if (checking) return;
    checking = s;
    showChecking(s);
    let r;
    try {
      r = await Verify.verifyFlac(s.file, s.tags, (p, label) => {
        if (!sheetShows(s)) return;
        $("vProgress").value = p;
        $("vDetail").textContent = label + " " + Math.round(p * 100) + "%";
      });
    } catch (e) {
      r = { ok: null, verdict: "Couldn't check it", detail: String(e && e.message || e) };
    } finally {
      checking = null;
    }
    s.check = { verdict: r.verdict, detail: r.detail, cls: r.ok === true ? "good" : r.ok === false ? "bad" : "warn" };
    if (s === current) { $("dot").hidden = false; $("dot").className = "dot " + s.check.cls; $("dot").title = s.check.verdict; }
    if (!sheetShows(s)) return;
    $("vProgress").hidden = true;
    $("btnVerify").disabled = false;
    $("btnVerify").textContent = "Check again";
    showCheck(s.check);
  }

  function showChecking(s) {
    const btn = $("btnVerify");
    if (!btn) return;
    btn.disabled = !!checking;
    $("vProgress").hidden = checking !== s;
    if (checking === s) {
      $("vVerdict").textContent = "";
      $("vVerdict").className = "v-verdict";
      $("vDetail").textContent = "Checking…";
    } else if (checking) {
      $("vDetail").textContent = "Another file is being checked: one at a time.";
    }
  }

  // -- keys ----------------------------------------------------------------------------------
  // One table: binds the keys and fills the Keyboard shortcuts sheet (the desktop player's keys).
  const KEYS = [
    ["Playback", "Space", "Play / pause", () => togglePlay()],
    ["Playback", "N  ·  Ctrl+→", "Next song", () => next(false)],
    ["Playback", "P  ·  Ctrl+←", "Previous song (after 3 s: back to its start)", () => previous()],
    ["Playback", "← / →", "Back / forward 5 seconds", null],
    ["Playback", "Shift+← / Shift+→", "Back / forward 30 seconds", null],
    ["Playback", "0 – 9", "Jump to 0% – 90% of the song", null],
    ["Playback", "S", "Shuffle on / off", () => toggleShuffle()],
    ["Playback", "R", "Repeat: off, the list, the song", () => cycleRepeat()],
    ["Playback", "Ctrl+↑ / Ctrl+↓", "Volume up / down 5%", null],
    ["Playback", "Ctrl+Shift+↓", "Mute / unmute", () => setMuted(!player.muted)],
    ["Playback", "Ctrl+Shift+↑", "Volume 100% (untouched)", () => setVolume(1)],
    ["Lists", "Ctrl+O", "Open a folder", () => openFolder({ replace: true })],
    ["Lists", "Ctrl+F  ·  /", "Search", () => { search.focus(); search.select(); }],
    ["Lists", "↑ / ↓  ·  PgUp / PgDn", "Move in the list", null],
    ["Lists", "Enter", "Play the song (an album row: the album)", null],
    ["Lists", "Esc", "Clear the search", null],
    ["Windows", "I", "Details (and Check this file)", () => openDetails()],
    ["Windows", "M", "Meters", () => showMeters($("meters").hidden)],
    ["Windows", "F", "Meters: level / spectrum", () => { if ($("meters").hidden) showMeters(true); meterMode(meters.mode === "levels" ? "spectrum" : "levels"); }],
    ["Windows", "Ctrl+,", "Settings", () => openSettings()],
    ["Windows", "?  ·  F1", "Keyboard shortcuts (this list)", () => openKeys()],
  ];

  function openKeys() {
    let html = "", group = "";
    for (const [g, k, what] of KEYS) {
      if (g !== group) { group = g; html += `<tr><th colspan="2">${esc(g)}</th></tr>`; }
      html += `<tr><td>${k.split("  ·  ").map((x) => `<kbd>${esc(x)}</kbd>`).join(" ")}</td><td>${esc(what)}</td></tr>`;
    }
    $("keysTable").innerHTML = html;
    openDialog($("dlgKeys"));
  }

  const act = (label) => KEYS.find((k) => k[1] === label)[3]();

  document.addEventListener("keydown", (ev) => {
    if (ev.defaultPrevented) return;
    const tag = ev.target && ev.target.tagName;
    const typing = tag === "INPUT" && ev.target.type !== "range" && ev.target.type !== "checkbox" || tag === "TEXTAREA";
    const ctrl = ev.ctrlKey || ev.metaKey, key = ev.key, k = key.length === 1 ? key.toLowerCase() : key;
    if (document.querySelector("dialog[open]")) return;      // a dialog has the keys (Esc closes it)
    if (ctrl && k === " ") { ev.preventDefault(); return togglePlay(); }
    if (ctrl && k === "o") { ev.preventDefault(); return act("Ctrl+O"); }
    if (ctrl && k === "f") { ev.preventDefault(); return act("Ctrl+F  ·  /"); }
    if (ctrl && k === ",") { ev.preventDefault(); return openSettings(); }
    if (typing) return;
    if (ctrl && ev.shiftKey && key === "ArrowDown") { ev.preventDefault(); return setMuted(!player.muted); }
    if (ctrl && ev.shiftKey && key === "ArrowUp") { ev.preventDefault(); return setVolume(1); }
    if (ctrl && key === "ArrowUp") { ev.preventDefault(); return setVolume(Math.round(player.volume * 100 + 5) / 100); }
    if (ctrl && key === "ArrowDown") { ev.preventDefault(); return setVolume(Math.round(player.volume * 100 - 5) / 100); }
    if (ctrl && key === "ArrowRight") { ev.preventDefault(); return next(false); }
    if (ctrl && key === "ArrowLeft") { ev.preventDefault(); return previous(); }
    if (ctrl || ev.altKey) return;
    if (openMenu && (key === "ArrowDown" || key === "ArrowUp")) return;
    const inList = document.activeElement === $("list");
    if (tag === "INPUT" && ev.target.type === "range" && /^Arrow|^Page|^Home|^End/.test(key)) return;   // the slider moves
    switch (key) {
      case " ": if (tag === "BUTTON") return; ev.preventDefault(); return togglePlay();
      case "ArrowLeft": case "ArrowRight": {
        ev.preventDefault();
        const step = (ev.shiftKey ? 30 : 5) * (key === "ArrowLeft" ? -1 : 1);
        return player.seek(player.position + step);
      }
      case "ArrowDown": case "ArrowUp": case "PageDown": case "PageUp": case "Home": case "End": {
        if (!rows.length) return;
        ev.preventDefault();
        if (!inList) $("list").focus();
        const page = Math.max(1, Math.floor($("list").clientHeight / ROW_H) - 1);
        const d = { ArrowDown: 1, ArrowUp: -1, PageDown: page, PageUp: -page }[key];
        if (key === "Home") return select(0, { scroll: true });
        if (key === "End") return select(rows.length - 1, { scroll: true });
        return select(selected < 0 ? 0 : selected + d, { scroll: true });
      }
      case "Enter": if (inList && selected >= 0) { ev.preventDefault(); playRow(selected); } return;
      case "F1": ev.preventDefault(); return openKeys();
      case "?": return openKeys();
      case "/": ev.preventDefault(); return act("Ctrl+F  ·  /");
      case "Escape": closeMenu(); return;
    }
    if (/^[0-9]$/.test(k) && current) return player.seek((parseInt(k, 10) / 10) * player.duration);
    const single = { n: "N  ·  Ctrl+→", p: "P  ·  Ctrl+←", s: "S", r: "R", i: "I", m: "M", f: "F" }[k];
    if (single) { ev.preventDefault(); act(single); }
  });

  // -- drag and drop -------------------------------------------------------------------------
  let dragDepth = 0;
  const hasFiles = (ev) => ev.dataTransfer && Array.from(ev.dataTransfer.types || []).includes("Files");
  document.addEventListener("dragenter", (ev) => { if (!hasFiles(ev)) return; ev.preventDefault(); dragDepth++; $("drop").hidden = false; });
  document.addEventListener("dragover", (ev) => { if (!hasFiles(ev)) return; ev.preventDefault(); ev.dataTransfer.dropEffect = "copy"; });
  document.addEventListener("dragleave", () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) $("drop").hidden = true; });
  document.addEventListener("drop", async (ev) => {
    if (!hasFiles(ev)) return;
    ev.preventDefault();
    dragDepth = 0;
    $("drop").hidden = true;
    status("Looking at what you dropped…");
    const entries = await Lib.collectDrop(ev.dataTransfer);
    await addEntries(entries, { replace: !songs.length });
  });

  // Opened by the installed app ("Open with" Ironbark, in browsers that install web apps)
  if ("launchQueue" in window) {
    window.launchQueue.setConsumer(async (params) => {
      if (!params.files || !params.files.length) return;
      const entries = [];
      for (const h of params.files) { try { const f = await h.getFile(); entries.push({ file: f, path: f.name }); } catch (e) { /* skip */ } }
      await addEntries(entries, { replace: true });
      if (rows.length) playRow(0);
    });
  }

  // -- start ---------------------------------------------------------------------------------
  function formatsLine() {
    const yes = [], no = [];
    for (const [name, type] of KINDS) (probe.canPlayType(type) ? yes : no).push(name);
    return "This browser plays " + yes.join(", ") + "." + (no.length ? " Not " + no.join(" or ") + ": the desktop Ironbark plays those." : "");
  }

  function start() {
    document.documentElement.classList.add("started");
    applyTheme();
    player.volume = prefs.volume;
    showVolume();
    showModes();
    meters.mode = prefs.metersMode;
    meterMode(prefs.metersMode);
    if (prefs.meters) showMeters(true);
    $("formats").textContent = formatsLine();
    rebuild();
    offerReopen();
    if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost" || location.hostname === "127.0.0.1")) {
      navigator.serviceWorker.register("sw.js").catch(() => { /* works without it, just not offline */ });
    }
  }

  // for tests: the page's state, read only
  window.IronbarkApp = { get songs() { return songs; }, get rows() { return rows; }, get current() { return current; }, get queue() { return queue; }, player, prefs, addEntries };

  start();
})();
