/* Keeps the one thing a dead server cannot serve: a page that says so.
 *
 * A browser only reaches this service worker when the page has been loaded at
 * least once while the server was up. From then on, a navigation that fails to
 * reach the network falls back to cached offline.html instead of Chrome's
 * connection error, at the same URL. Only navigations are intercepted — images,
 * CSS and JSON still go straight to the server, so nothing here can serve a
 * stale asset by accident.
 */

const CACHE = "xr-offline-v1";
const OFFLINE = "offline.html";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll([OFFLINE]))
      .then(() => self.skipWaiting())
      .catch(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET" || request.mode !== "navigate") return;

  event.respondWith(
    fetch(request).catch(() =>
      caches.match(OFFLINE).then(
        (cached) =>
          cached ||
          new Response(
            "<!doctype html><meta name=viewport content='width=device-width'><title>offline</title>" +
              "<body style='background:#050507;color:#dcdce4;font:12px monospace;padding:32px'>" +
              "The local preview server isn't running. Start it with <code>python tools/serve.py --supervise</code>.</body>",
            { headers: { "Content-Type": "text/html; charset=utf-8" } }
          )
      )
    )
  );
});
