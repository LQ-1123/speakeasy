/* Public shell and build assets only. Private API/audio never enter shared caches. */
const VERSION = "se-v2-4";
const SHELL_CACHE = `${VERSION}-shell`;
const ASSET_CACHE = `${VERSION}-assets`;
const SHELL_ASSETS = ["/", "/manifest.webmanifest", "/icons/icon-192.png", "/icons/icon-512.png", "/icons/apple-touch-icon.png"];
const cacheable = (response) => response?.ok && !/private|no-store/i.test(response.headers.get("Cache-Control") || "");

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL_CACHE).then(async (cache) => {
    await Promise.all(SHELL_ASSETS.map(async (path) => {
      try { const response = await fetch(path, { cache: "reload" }); if (cacheable(response)) await cache.put(path, response); } catch { /* install online assets as available */ }
    }));
  }).catch(() => undefined).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((key) => key.startsWith("se-") && !key.startsWith(VERSION)).map((key) => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws")) return;
  if (request.headers?.get("Authorization") || request.headers?.get("X-Session-Token") || request.headers?.get("X-User-Token")) return;

  // The app routes via hashes. Never save unrelated or authenticated HTML as '/'.
  if (request.mode === "navigate") {
    if (!["/", "/index.html"].includes(url.pathname) || url.search) return;
    event.respondWith(fetch(request).then((response) => {
      if (cacheable(response)) { const copy = response.clone(); event.waitUntil(caches.open(SHELL_CACHE).then((cache) => cache.put("/", copy)).catch(() => undefined)); }
      return response;
    }).catch(() => caches.match("/")));
    return;
  }
  const immutable = url.pathname.startsWith("/_next/static/");
  const publicAsset = immutable || url.pathname.startsWith("/icons/") || url.pathname.startsWith("/worklets/") || url.pathname === "/manifest.webmanifest";
  if (!publicAsset || url.search) return;
  const download = async () => {
    const response = await fetch(request);
    if (cacheable(response)) { const copy = response.clone(); event.waitUntil(caches.open(ASSET_CACHE).then((cache) => cache.put(request, copy)).catch(() => undefined)); }
    return response;
  };
  // Next build resources are content addressed: a cache hit requires no network.
  event.respondWith(immutable
    ? caches.match(request).then((cached) => cached || download())
    : download().catch(() => caches.match(request)));
});
