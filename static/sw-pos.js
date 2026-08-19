/* Service worker — cache tài nguyên POS tĩnh (vendor JS/CSS). */
const CACHE = 'keto-pos-shell-v1';
const ASSETS = [
    '/static/vendor/bootstrap/bootstrap.min.css',
    '/static/vendor/bootstrap/bootstrap.bundle.min.js',
    '/static/vendor/jquery/jquery-3.6.0.min.js',
    '/static/vendor/fontawesome/css/all.min.css',
    '/static/js/pos-offline.js',
];

self.addEventListener('install', function (ev) {
    ev.waitUntil(
        caches.open(CACHE).then(function (cache) {
            return cache.addAll(ASSETS.map(function (u) {
                return new Request(u, { credentials: 'same-origin' });
            })).catch(function () {});
        }).then(function () { return self.skipWaiting(); })
    );
});

self.addEventListener('activate', function (ev) {
    ev.waitUntil(
        caches.keys().then(function (keys) {
            return Promise.all(keys.filter(function (k) { return k !== CACHE; }).map(function (k) {
                return caches.delete(k);
            }));
        }).then(function () { return self.clients.claim(); })
    );
});

self.addEventListener('fetch', function (ev) {
    const url = ev.request.url;
    if (ev.request.method !== 'GET') return;
    if (!url.includes('/static/')) return;
    ev.respondWith(
        caches.match(ev.request).then(function (cached) {
            if (cached) return cached;
            return fetch(ev.request).then(function (resp) {
                if (resp && resp.status === 200) {
                    const copy = resp.clone();
                    caches.open(CACHE).then(function (c) { c.put(ev.request, copy); });
                }
                return resp;
            });
        })
    );
});
