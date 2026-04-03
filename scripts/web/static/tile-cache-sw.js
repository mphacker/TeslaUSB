/**
 * Service Worker for caching OpenStreetMap tiles offline.
 *
 * Strategy: Network-first with cache fallback.
 * - Tile requests go to the network first.
 * - Successful responses are cached for offline use.
 * - If offline, serve from cache.
 * - Tiles are cached indefinitely (OSM tiles rarely change).
 */

const TILE_CACHE = 'teslausb-map-tiles-v1';
const TILE_PATTERN = /^https:\/\/[a-c]?\.?tile\.openstreetmap\.org\//;

self.addEventListener('install', event => {
    self.skipWaiting();
});

self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys().then(keys =>
            Promise.all(
                keys.filter(k => k.startsWith('teslausb-map-tiles-') && k !== TILE_CACHE)
                    .map(k => caches.delete(k))
            )
        ).then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', event => {
    const url = event.request.url;

    // Only intercept OSM tile requests
    if (!TILE_PATTERN.test(url)) return;

    event.respondWith(
        fetch(event.request)
            .then(response => {
                if (response.ok) {
                    const clone = response.clone();
                    caches.open(TILE_CACHE).then(cache => cache.put(event.request, clone));
                }
                return response;
            })
            .catch(() => {
                // Offline — serve from cache
                return caches.match(event.request).then(cached => {
                    if (cached) return cached;
                    // Return a transparent 1x1 PNG as fallback for missing tiles
                    return new Response(
                        Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVQI12NgAAIABQABNjN9GQAAAABJRUEFTkSuQmCC'), c => c.charCodeAt(0)),
                        { headers: { 'Content-Type': 'image/png' } }
                    );
                });
            })
    );
});
