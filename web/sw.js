/* Ironbark for the web — the offline copy. When the page is served from a web address, this
   keeps a copy of its own files in the browser so it opens without a connection. It caches only
   the page's own files; songs are never fetched (they're read from the listener's disk). */
"use strict";

const VERSION = "0.49.2";
const CACHE = "ironbark-web-" + VERSION;
const FILES = [
  "./", "index.html", "ironbark.css", "manifest.webmanifest",
  "js/version.js", "js/tags.js", "js/verify.js", "js/library.js", "js/player.js", "js/meters.js", "js/app.js",
  "icons/icon_32.png", "icons/icon_128.png", "icons/icon_256.png", "icons/icon_512.png",
];

self.addEventListener("install", (ev) => {
  // from the server itself, not the browser's own cache, so the copy is all one version
  ev.waitUntil(caches.open(CACHE).then((c) => c.addAll(FILES.map((f) => new Request(f, { cache: "reload" })))).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (ev) => {
  ev.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith("ironbark-web-") && k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

// Fresh from the server when it can be reached (so an update shows at once), the saved copy
// when it can't (offline).
self.addEventListener("fetch", (ev) => {
  const url = new URL(ev.request.url);
  if (ev.request.method !== "GET" || url.origin !== self.location.origin) return;
  ev.respondWith(
    fetch(ev.request, { cache: "no-cache" })
      .then((res) => {
        if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(ev.request, copy)); }
        return res;
      })
      .catch(() => caches.match(ev.request, { ignoreSearch: true }).then((hit) => hit || Response.error())),
  );
});
