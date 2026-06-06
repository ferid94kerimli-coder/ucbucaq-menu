const CACHE = 'menu-v1';
const MAX_AGE = 5 * 60 * 1000; // 5 minutes

self.addEventListener('install', e => {
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // Only cache GET requests to /menu/ paths
  if (e.request.method !== 'GET' || !url.pathname.startsWith('/menu/')) return;

  e.respondWith(
    caches.open(CACHE).then(cache =>
      cache.match(e.request).then(cached => {
        const now = Date.now();
        const fetchPromise = fetch(e.request).then(response => {
          if (response.ok) {
            const clone = response.clone();
            // Store with timestamp header
            const headers = new Headers(clone.headers);
            headers.append('sw-cached-at', String(now));
            clone.blob().then(body =>
              cache.put(e.request, new Response(body, {
                status: clone.status,
                statusText: clone.statusText,
                headers
              }))
            );
          }
          return response;
        }).catch(() => cached);

        if (!cached) return fetchPromise;

        const cachedAt = parseInt(cached.headers.get('sw-cached-at') || '0');
        const stale = (now - cachedAt) > MAX_AGE;

        if (stale) {
          // Serve stale immediately, update in background
          fetchPromise; // background update
          return cached;
        }
        return cached;
      })
    )
  );
});
