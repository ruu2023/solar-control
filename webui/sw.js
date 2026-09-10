// Minimal service worker: cache the app shell so it opens instantly / offline.
// Live data (/status, /load) is always fetched from the network.
const SHELL = "solar-shell-v4";
const ASSETS = ["./", "./index.html", "./manifest.webmanifest", "./icon.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

const SHELL_PATHS = new Set(["/", "/index.html", "/manifest.webmanifest", "/icon.svg", "/sw.js"]);

self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  const url = new URL(e.request.url);
  // Only the app shell is cache-first; every API path always goes to the network.
  if (!SHELL_PATHS.has(url.pathname)) return;
  e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request)));
});
