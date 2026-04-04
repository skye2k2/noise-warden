/**
 * Service Worker for Noise Warden Timeline.
 * Enables offline access to the timeline page and cached audio snippets.
 *
 * Strategy:
 * - Timeline page: network first, cache fallback (fresh data when online)
 * - Audio snippets: cache first, network fallback (once cached, always offline)
 * - Everything else: pass through (don't interfere with other pages)
 */
const CACHE = 'noise-warden-timeline-cache-v7'

self.addEventListener('install', () => self.skipWaiting())

self.addEventListener('activate', (event) => {
  // Purge old cache versions on activation
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(k => k !== CACHE).map(k => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  )
})

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url)

  // Timeline page: network first, cache fallback.
  // Normalize cache key (strip query params) so ?view=day and ?view=week share one cache entry.
  if (url.pathname === '/timeline') {
    const cacheKey = new Request(url.origin + '/timeline')
    event.respondWith(
      fetch(event.request)
        .then(response => {
          if (response.ok) {
            const clone = response.clone()
            caches.open(CACHE).then(cache => cache.put(cacheKey, clone))
          }
          return response
        })
        .catch(() => caches.match(cacheKey))
    )
    return
  }

  // Audio snippets: cache first, network fallback.
  // Once a snippet is cached, it will always be served from cache (offline-safe).
  if (url.pathname.startsWith('/snippets/')) {
    event.respondWith(
      caches.match(event.request)
        .then(cached => {
          if (cached) { return cached }
          return fetch(event.request).then(response => {
            if (response.ok) {
              const clone = response.clone()
              caches.open(CACHE).then(cache => cache.put(event.request, clone))
            }
            return response
          })
        })
        // If both cache and network fail, return a graceful empty response
        .catch(() => new Response('', { status: 503, statusText: 'Offline' }))
    )
    return
  }

  // All other requests: pass through to network without caching
})
