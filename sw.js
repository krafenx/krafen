/* ==========================================================================
   Krafen — Service Worker (PWA Shell Caching, Offline Fallback & Web Push)
   ========================================================================== */

const CACHE_VERSION = 'krafen-v5';
const PRECACHE_ASSETS = [
    '/',
    '/style.css',
    '/app.js',
    '/cmdk.js',
    '/favicon.png',
    '/icon-192.png',
    '/icon-512.png',
    '/manifest.webmanifest',
    '/danonsha/theme.css',
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_VERSION).then(async (cache) => {
            await Promise.allSettled(
                PRECACHE_ASSETS.map(async (assetUrl) => {
                    const res = await fetch(assetUrl, { cache: 'reload' });
                    if (res && res.ok) {
                        await cache.put(assetUrl, res);
                    }
                })
            );
        }).then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((keys) => {
            return Promise.all(
                keys.filter((key) => key !== CACHE_VERSION).map((key) => caches.delete(key))
            );
        }).then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (event) => {
    const request = event.request;
    if (request.method !== 'GET') return;

    const url = new URL(request.url);

    // Внешние запросы и API не перехватываем через Service Worker кэш
    if (url.origin !== self.location.origin || url.pathname.startsWith('/api/')) {
        return;
    }

    // 1. Навигация по страницам (HTML): Network-first с офлайн-резервом
    if (request.mode === 'navigate') {
        event.respondWith(
            fetch(request)
                .then((networkResponse) => {
                    if (networkResponse && networkResponse.status === 200) {
                        const copy = networkResponse.clone();
                        caches.open(CACHE_VERSION).then((cache) => cache.put(request, copy));
                    }
                    return networkResponse;
                })
                .catch(async () => {
                    const cached = await caches.match(request);
                    if (cached) return cached;
                    const home = await caches.match('/');
                    return home || new Response('Офлайн режим', {
                        status: 503,
                        headers: { 'Content-Type': 'text/plain; charset=utf-8' },
                    });
                })
        );
        return;
    }

    // 2. Статические ассеты (CSS, JS, картинки, манифест): Stale-While-Revalidate
    event.respondWith(
        caches.match(request).then((cachedResponse) => {
            const fetchPromise = fetch(request).then((networkResponse) => {
                if (networkResponse && networkResponse.status === 200) {
                    const copy = networkResponse.clone();
                    caches.open(CACHE_VERSION).then((cache) => cache.put(request, copy));
                }
                return networkResponse;
            }).catch(() => cachedResponse);

            return cachedResponse || fetchPromise;
        })
    );
});

self.addEventListener('push', (event) => {
    let payload = {};
    try {
        payload = event.data ? event.data.json() : {};
    } catch {
        payload = { body: event.data ? event.data.text() : 'Стрим начался!' };
    }
    const title = payload.title || 'Прямой эфир начался!';
    const options = {
        body: payload.body || 'Заходи на трансляцию',
        icon: payload.icon || '/icon-192.png',
        badge: payload.badge || '/favicon.png',
        tag: payload.tag || 'krafen-stream-live',
        renotify: true,
        data: { url: payload.url || '/' },
    };
    event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    const targetUrl = (event.notification.data && event.notification.data.url) || '/';
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windowClients) => {
            for (const client of windowClients) {
                if (client.url === targetUrl && 'focus' in client) {
                    return client.focus();
                }
            }
            if (clients.openWindow) {
                return clients.openWindow(targetUrl);
            }
        })
    );
});
