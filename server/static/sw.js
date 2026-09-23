/* Emerovia observer PWA — service worker.
 *
 * App-shell caching ONLY. The world is live: every API request
 * (/world/*, /chat, /proposals, /agents, /stats/*, /health, /operator-log)
 * goes straight to the network and is never cached.
 *
 * Bump CACHE on each release so clients pick up the new shell.
 */
"use strict";

var CACHE = "emerovia-shell-v1";

var SHELL = [
  "/",
  "/manifest.webmanifest",
  "/pwa.css",
  "/pwa.js",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/icon-maskable-512.png",
  "/icons/apple-touch-icon.png"
];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE)
      .then(function (cache) { return cache.addAll(SHELL); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys()
      .then(function (keys) {
        return Promise.all(keys.map(function (key) {
          if (key !== CACHE && key.indexOf("emerovia-shell-") === 0) {
            return caches.delete(key);
          }
          return Promise.resolve(false);
        }));
      })
      .then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (event) {
  var req = event.request;
  if (req.method !== "GET") return;
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // fonts etc.: let the browser handle it

  var isShell = SHELL.indexOf(url.pathname) !== -1;
  if (!isShell) {
    // Live world data: network only, never cached.
    event.respondWith(fetch(req));
    return;
  }
  event.respondWith(
    caches.match(req).then(function (hit) {
      if (hit) return hit;
      return fetch(req).then(function (res) {
        var copy = res.clone();
        caches.open(CACHE).then(function (cache) { cache.put(req, copy); });
        return res;
      });
    })
  );
});
