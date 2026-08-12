const CACHE_NAME = 'eventhub-v2.6';
const URLS_TO_CACHE = [
  '/',
  '/scanner',
  '/register',
  '/stats',
  '/manifest.json',
  'https://unpkg.com/html5-qrcode',
  'https://upload.wikimedia.org/wikipedia/commons/thumb/c/c2/QR_icon.svg/512px-QR_icon.svg.png'
];

self.addEventListener('install', event => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => {
        // Use Promise.allSettled so a single failed CDN fetch doesn't crash the entire installation
        return Promise.allSettled(
          URLS_TO_CACHE.map(url => {
            const request = new Request(url, { mode: url.startsWith('http') ? 'no-cors' : 'cors' });
            return fetch(request).then(response => {
              if (response.ok || response.type === 'opaque') {
                return cache.put(request, response);
              }
            }).catch(err => console.warn(`[EventHub SW] Failed to cache: ${url}`, err));
          })
        );
      })
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(cacheNames => {
      return Promise.all(
        cacheNames.map(cache => {
          if (cache !== CACHE_NAME) {
            console.log(`[EventHub SW] Deleting old cache: ${cache}`);
            return caches.delete(cache);
          }
        })
      );
    }).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const req = event.request;

  // Let API requests and POST methods completely bypass the cache
  if (req.method !== 'GET' || req.url.includes('/api/')) {
    return;
  }

  event.respondWith(
    (async () => {
      const cache = await caches.open(CACHE_NAME);

      // Strategy 1: Network-First for Navigation (HTML Pages)
      // Ensures operators always get the newest UI updates if the network is available.
      if (req.mode === 'navigate') {
        try {
          const networkResponse = await fetch(req);
          if (networkResponse.ok) cache.put(req, networkResponse.clone());
          return networkResponse;
        } catch (error) {
          const cachedResponse = await cache.match(req);
          if (cachedResponse) return cachedResponse;
          throw error;
        }
      }

      // Strategy 2: Cache-First with Background Update (Stale-While-Revalidate) for Static Assets
      // Instantly loads scripts/icons from cache for high speed, updates cache silently in background.
      const cachedResponse = await cache.match(req);
      
      const fetchPromise = fetch(req).then(networkResponse => {
        if (networkResponse && (networkResponse.status === 200 || networkResponse.type === 'opaque')) {
          cache.put(req, networkResponse.clone());
        }
        return networkResponse;
      }).catch(() => {
        // Silently swallow fetch errors for static assets if we are offline
      });

      return cachedResponse || fetchPromise;
    })()
  );
});