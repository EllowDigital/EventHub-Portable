/* ====================================================================
   EventHub Operations Portal — Service Worker v2.6
   ==================================================================== */

const CACHE_NAME = 'eventhub-v2.6';

// Core routes and static assets to pre-cache on installation
const PRECACHE_ASSETS = [
  '/',
  '/scanner',
  '/register',
  '/stats',
  '/static/site.webmanifest?v=2.6',
  '/static/favicon/favicon-96x96.png?v=2.6',
  '/static/favicon/favicon.svg?v=2.6',
  '/static/favicon/favicon.ico?v=2.6',
  '/static/favicon/apple-touch-icon.png?v=2.6',
  'https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js',
  'https://cdn.jsdelivr.net/npm/jsqr@1.4.0/dist/jsQR.min.js',
  'https://unpkg.com/@zxing/library@0.21.3/umd/index.min.js'
];

// Helper Function: Fetch with Timeout to prevent hanging on terrible Wi-Fi connections
const fetchWithTimeout = (request, timeout = 3000) => {
  return new Promise((resolve, reject) => {
    const controller = new AbortController();
    const id = setTimeout(() => {
      controller.abort();
      reject(new Error("Network connection timed out"));
    }, timeout);

    fetch(request, { signal: controller.signal })
      .then((response) => {
        clearTimeout(id);
        resolve(response);
      })
      .catch((error) => {
        clearTimeout(id);
        reject(error);
      });
  });
};

// 1. INSTALLATION STAGE
self.addEventListener('install', event => {
  self.skipWaiting(); // Instantly activate new service worker versions
  event.waitUntil(
    caches.open(CACHE_NAME).then(async cache => {
      // Use Promise.allSettled to prevent individual failed external requests from stopping installation
      const results = await Promise.allSettled(
        PRECACHE_ASSETS.map(async url => {
          try {
            const request = new Request(url, {
              mode: url.startsWith('http') && !url.includes(location.origin) ? 'no-cors' : 'cors'
            });
            const response = await fetch(request);
            if (response.ok || response.type === 'opaque') {
              return cache.put(request, response);
            }
          } catch (err) {
            console.warn(`[EventHub SW v2.6] Pre-cache warning for: ${url}`, err);
          }
        })
      );
      return results;
    })
  );
});

// 2. ACTIVATION & CACHE CLEANUP STAGE
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(cacheNames => {
      return Promise.all(
        cacheNames.map(cache => {
          if (cache !== CACHE_NAME) {
            console.log(`[EventHub SW v2.6] Purging old cache cluster: ${cache}`);
            return caches.delete(cache);
          }
        })
      );
    }).then(() => self.clients.claim()) // Immediately take control of active browser tabs
  );
});

// 3. FETCH INTERCEPTION & STRATEGIES
self.addEventListener('fetch', event => {
  const req = event.request;
  const url = new URL(req.url);

  // Bypass 1: Do not cache non-GET requests (e.g., POST checkins/registrations)
  if (req.method !== 'GET') return;

  // Bypass 2: Do not cache API endpoints or WebSockets
  if (url.pathname.startsWith('/api/') || url.protocol === 'ws:' || url.protocol === 'wss:') return;

  // Bypass 3: Ignore Chrome Extension or non-HTTP resources
  if (!url.protocol.startsWith('http')) return;

  event.respondWith(
    (async () => {
      const cache = await caches.open(CACHE_NAME);

      // STRATEGY A: Network-First with Timeout for Navigation (HTML Page Loading)
      // Ensures users get the latest UI updates, but if the network takes longer than 3 seconds (bad Wi-Fi),
      // it instantly falls back to the cache so staff don't get stuck on a loading screen.
      if (req.mode === 'navigate') {
        try {
          const networkResponse = await fetchWithTimeout(req, 3000); 
          if (networkResponse && networkResponse.ok) {
            cache.put(req, networkResponse.clone());
          }
          return networkResponse;
        } catch (networkError) {
          console.warn(`[EventHub SW v2.6] Network slow or offline, falling back to cache for: ${url.pathname}`);
          const cachedResponse = await cache.match(req);
          if (cachedResponse) return cachedResponse;

          // Fallback to primary hub index if target sub-page isn't cached
          const indexFallback = await cache.match('/');
          if (indexFallback) return indexFallback;

          throw networkError;
        }
      }

      // STRATEGY B: Stale-While-Revalidate for Static Assets (CSS, JS, Fonts, Images, External Libraries)
      // Serves cached copies immediately for instant loading, while silently revalidating via network in the background.
      const cachedAsset = await cache.match(req);

      const fetchPromise = fetch(req).then(networkResponse => {
        if (networkResponse && (networkResponse.status === 200 || networkResponse.type === 'opaque')) {
          cache.put(req, networkResponse.clone());
        }
        return networkResponse;
      }).catch(() => {
        // Silently catch offline failures for background revalidation
      });

      return cachedAsset || fetchPromise;
    })()
  );
});

// 4. CLIENT MESSAGE HANDLING
self.addEventListener('message', event => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});