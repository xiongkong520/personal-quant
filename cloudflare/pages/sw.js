/* PWA Service Worker：静态资源缓存 + API 网络优先 */
const CACHE = "quant-static-v2";
const STATIC = ["/", "/app.js", "/styles.css", "/manifest.json", "/icon-192.png", "/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(STATIC)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET") return;
  if (url.pathname.startsWith("/api/")) return;
  event.respondWith(
    caches.match(event.request).then((hit) => hit || fetch(event.request).then((resp) => {
      const copy = resp.clone();
      caches.open(CACHE).then((cache) => cache.put(event.request, copy)).catch(() => {});
      return resp;
    }).catch(() => hit))
  );
});
