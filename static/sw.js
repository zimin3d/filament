// Сервис-воркер: страницы — из сети (данные всегда свежие), при отсутствии сети —
// последняя сохранённая копия; статика и фото — из кэша.
const CACHE = "filament-v3";  // v3: скрипты вынесены из страниц (строгий CSP)
const scope = self.registration.scope;

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll([scope, scope + "static/style.css", scope + "offline"]))
    .then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET" || !req.url.startsWith(scope)) return;
  const url = new URL(req.url);
  // фото и миниатюры не меняются (уникальные имена) — из кэша;
  // стили и скрипты — из кэша, но тут же обновляются в фоне
  const immutable = /\/(thumbs|uploads)\//.test(url.pathname);
  if (immutable || /\/static\//.test(url.pathname)) {
    e.respondWith(caches.open(CACHE).then((c) => c.match(req).then((hit) => {
      if (hit && immutable) return hit;
      const net = fetch(req).then((res) => { if (res.ok) c.put(req, res.clone()); return res; });
      if (hit) { net.catch(() => {}); return hit; }
      return net;
    })));
    return;
  }
  if (req.mode === "navigate") {
    e.respondWith(fetch(req).then((res) => {
      const noStore = /no-store/.test(res.headers.get("Cache-Control") || "");
      if (res.ok && !res.redirected && !noStore) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); }
      return res;
    }).catch(() => caches.match(req).then((hit) => hit || caches.match(scope + "offline"))));
  }
});
