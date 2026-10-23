/*
 * Novel Downloader service worker.
 *
 * Deliberately minimal: it exists so the app can be installed and can show a
 * cached shell when the network is gone. All real-time behaviour belongs to
 * NiceGUI's WebSocket connection, which this worker never touches.
 */

// Bump this when the precache list or the offline strategy changes.
const CACHE_VERSION = "v1";
const CACHE_NAME = `novel-downloader-${CACHE_VERSION}`;

// Static shell assets warmed during install. The root document is included so
// an offline navigation can fall back to the cached application shell.
const PRECACHE_URLS = [
  "/static/manifest.webmanifest",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE_NAME);
      // Warm each URL independently: a single failing resource (for example
      // an unauthenticated proxy response) must not abort the whole install.
      await Promise.all(
        PRECACHE_URLS.map((url) =>
          cache.add(new Request(url, { cache: "reload" })).catch(() => {})
        )
      );
      await self.skipWaiting();
    })()
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys
          .filter((key) => key.startsWith("novel-downloader-") && key !== CACHE_NAME)
          .map((key) => caches.delete(key))
      );
      await self.clients.claim();
    })()
  );
});

/**
 * Network-first: always prefer the live server, fall back to the cache only
 * when the network is unavailable.
 */
async function networkFirst(request, fallbackUrl) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetch(request);
    if (response && response.ok) {
      cache.put(request, response.clone()).catch(() => {});
    }
    return response;
  } catch (error) {
    const cached = await cache.match(request);
    if (cached) {
      return cached;
    }
    if (fallbackUrl) {
      const fallback = await cache.match(fallbackUrl);
      if (fallback) {
        return fallback;
      }
    }
    throw error;
  }
}

self.addEventListener("fetch", (event) => {
  const request = event.request;

  // Only plain GETs are cacheable. WebSocket upgrades (ws:/wss:) never reach a
  // fetch handler anyway, but the guard keeps that contract explicit: this
  // worker must not interfere with NiceGUI's real-time connection.
  if (request.method !== "GET") {
    return;
  }

  const url = new URL(request.url);
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    return;
  }
  if (url.origin !== self.location.origin) {
    return;
  }

  // Offline-capable navigation: serve the cached app shell as a last resort.
  if (request.mode === "navigate") {
    event.respondWith(networkFirst(request, "/"));
    return;
  }

  // Static assets: network-first with cache fallback (they are exactly the
  // files warmed during install). NiceGUI's internal endpoints and the export
  // downloads are intentionally left untouched and go straight to the network.
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(networkFirst(request, null));
  }
});
