// PEV Buddy service worker: makes the app installable (PWA) and keeps the app
// shell + map library/tiles available on flaky connections. API calls and
// /data GeoJSON bypass this worker — routes and POIs must never be stale.
// (The big display road layer, /data/roads.display.geojson, is cached by the
// browser's HTTP cache instead: the server sends Cache-Control + ETag.)
const CACHE = "pev-buddy-v29";

const SHELL = [
  "/",
  "/index.html",
  "/style.css?v=29",
  "/app.js?v=29",
  "/manifest.webmanifest",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  // Live data and API: network only.
  if (url.origin === location.origin && (url.pathname.startsWith("/api/") || url.pathname.startsWith("/data/"))) {
    return;
  }
  // Navigations (index.html): network-first. The HTML pins every asset to a
  // cache-busted ?v=N, so a stale HTML would pin the whole app to an old
  // version — exactly the failure mode this avoids.
  if (e.request.mode === "navigate") {
    e.respondWith(
      fetch(e.request)
        .then((res) => {
          const clone = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, clone));
          return res;
        })
        .catch(() => caches.match(e.request).then((hit) => hit || caches.match("/")))
    );
    return;
  }
  // App shell and CDN assets (MapLibre, basemap tiles/fonts): cache-first,
  // filling the cache as we go. ?v=N assets are immutable per version.
  if (url.origin === location.origin || /jsdelivr\.net|basemaps\.cartocdn\.com/.test(url.host)) {
    e.respondWith(
      caches.match(e.request).then(
        (hit) =>
          hit ||
          fetch(e.request).then((res) => {
            if (res.ok || res.type === "opaque") {
              const clone = res.clone();
              caches.open(CACHE).then((c) => c.put(e.request, clone));
            }
            return res;
          })
      )
    );
  }
});
