/* Service worker — cache shell POS + tài nguyên tĩnh để bán offline trên VPS. */
const CACHE_SHELL = 'keto-pos-shell-v2';
const CACHE_API = 'keto-pos-api-v1';

const ASSETS = [
    '/static/vendor/bootstrap/bootstrap.min.css',
    '/static/vendor/bootstrap/bootstrap.bundle.min.js',
    '/static/vendor/jquery/jquery-3.6.0.min.js',
    '/static/vendor/fontawesome/css/all.min.css',
    '/static/js/pos-offline.js',
    '/static/manifest-pos.json',
];

/* Trang POS — cache sau lần truy cập online đầu tiên */
const POS_PAGES = [
    '/sale',
];

self.addEventListener('install', function (ev) {
    ev.waitUntil(
        caches.open(CACHE_SHELL).then(function (cache) {
            return cache.addAll(ASSETS.map(function (u) {
                return new Request(u, { credentials: 'same-origin' });
            })).catch(function () {});
        }).then(function () { return self.skipWaiting(); })
    );
});

self.addEventListener('activate', function (ev) {
    ev.waitUntil(
        caches.keys().then(function (keys) {
            return Promise.all(keys.filter(function (k) {
                return k !== CACHE_SHELL && k !== CACHE_API;
            }).map(function (k) { return caches.delete(k); }));
        }).then(function () { return self.clients.claim(); })
    );
});

function isPosPage(url) {
    try {
        const p = new URL(url).pathname.replace(/\/+$/, '') || '/';
        if (p === '/sale' || p.endsWith('/sale')) return true;
        if (p.includes('F&B_service') || p.includes('fb_service')) return true;
    } catch (_) {}
    return false;
}

function isCatalogApi(url) {
    return url.includes('/api/pos/catalog');
}

self.addEventListener('fetch', function (ev) {
    if (ev.request.method !== 'GET') return;
    const url = ev.request.url;

    /* Tài nguyên tĩnh */
    if (url.includes('/static/')) {
        ev.respondWith(
            caches.match(ev.request).then(function (cached) {
                if (cached) return cached;
                return fetch(ev.request).then(function (resp) {
                    if (resp && resp.status === 200) {
                        const copy = resp.clone();
                        caches.open(CACHE_SHELL).then(function (c) { c.put(ev.request, copy); });
                    }
                    return resp;
                });
            })
        );
        return;
    }

    /* Trang bán hàng — network first, fallback cache (offline vẫn mở được POS) */
    if (isPosPage(url)) {
        ev.respondWith(
            fetch(ev.request).then(function (resp) {
                if (resp && resp.status === 200) {
                    const copy = resp.clone();
                    caches.open(CACHE_SHELL).then(function (c) { c.put(ev.request, copy); });
                }
                return resp;
            }).catch(function () {
                return caches.match(ev.request).then(function (cached) {
                    return cached || caches.match('/sale');
                });
            })
        );
        return;
    }

    /* Catalog API — cache để search offline */
    if (isCatalogApi(url)) {
        ev.respondWith(
            fetch(ev.request).then(function (resp) {
                if (resp && resp.status === 200) {
                    const copy = resp.clone();
                    caches.open(CACHE_API).then(function (c) { c.put(ev.request, copy); });
                }
                return resp;
            }).catch(function () {
                return caches.match(ev.request);
            })
        );
    }
});
