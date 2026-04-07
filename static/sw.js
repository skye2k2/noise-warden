/**
 * Service Worker for Noise Warden.
 * Enables offline access to cached pages and audio snippets.
 *
 * Strategy:
 * - Static assets (CSS, favicon): cache first, network fallback (immutable between deploys)
 * - Timeline page: network first, cache fallback (fresh data when online)
 * - Dashboard, incidents, build pages: network first, cache fallback (functional offline)
 * - Audio snippets: cache first, network fallback (once cached, always offline)
 * - Config and calibration pages: network only (mutations disabled offline)
 * - Everything else: pass through (don't interfere with other routes)
 */
const CACHE = 'noise-warden-cache-v10'

/* Static assets to pre-cache on install — these rarely change between page loads */
const PRECACHE_ASSETS = [
  '/static/style.css',
  '/static/favicon.svg',
]

/* Pages that are useful offline (read-only views). Cached on first visit via
   network-first strategy. Config and calibration are intentionally excluded
   because saving changes offline would silently fail. */
const CACHEABLE_PAGES = ['/timeline', '/', '/incidents', '/build', '/thresholds']

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then(cache => cache.addAll(PRECACHE_ASSETS))
      .then(() => self.skipWaiting())
  )
})

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

/**
 * Slice a cached full response to satisfy a Range request from <audio> seeking.
 * Without this, the browser receives a 200 when it expects 206, and scrubbing breaks.
 * Only handles single-range requests (multi-range is vanishingly rare for audio).
 *
 * ARCHITECTURE NOTE — WHY RANGE HANDLING EXISTS IN TWO PLACES:
 * ─────────────────────────────────────────────────────────────
 * Audio scrubbing requires HTTP 206 Partial Content responses to Range requests.
 * This is implemented in BOTH the Service Worker (here) and the server (web.py):
 *
 *   1. Server (web.py get_snippet):
 *      Handles Range for first-load requests before the SW has cached the file.
 *      The browser fetches the snippet, SW intercepts and passes through to
 *      the server, which returns 206 with the requested byte range.
 *
 *   2. Service Worker (this function):
 *      Handles Range for CACHED snippets. Once a snippet is in the SW cache,
 *      subsequent requests (including Range seeks) are served from cache.
 *      The full file is cached; this function slices it to satisfy Range requests.
 *
 * If either layer is removed, scrubbing breaks for that scenario.
 * This has broken in at least two previous releases. Do not simplify.
 */
function maybeSliceForRange(request, cached) {
  const rangeHeader = request.headers.get('Range')
  if (!rangeHeader) { return cached }

  // Parse "bytes=START-END" (END is optional)
  const match = rangeHeader.match(/bytes=(\d+)-(\d*)/)
  if (!match) { return cached }

  return cached.arrayBuffer().then(buf => {
    const start = Number(match[1])
    const end = match[2] ? Number(match[2]) + 1 : buf.byteLength
    const sliced = buf.slice(start, end)

    return new Response(sliced, {
      status: 206,
      statusText: 'Partial Content',
      headers: {
        'Content-Type': cached.headers.get('Content-Type') || 'audio/wav',
        'Content-Length': sliced.byteLength,
        'Content-Range': 'bytes ' + start + '-' + (end - 1) + '/' + buf.byteLength,
        'Accept-Ranges': 'bytes',
      },
    })
  })
}

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url)

  // Static assets: cache first, network fallback (pre-cached on install)
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(event.request)
        .then(cached => cached || fetch(event.request).then(response => {
          if (response.ok) {
            const clone = response.clone()
            caches.open(CACHE).then(cache => cache.put(event.request, clone))
          }
          return response
        }))
        .catch(() => new Response('', { status: 503, statusText: 'Offline' }))
    )
    return
  }

  // Cacheable pages: network first, cache fallback.
  // Normalize cache key (strip query params) so view variants share one cache entry.
  if (CACHEABLE_PAGES.includes(url.pathname)) {
    const cacheKey = new Request(url.origin + url.pathname)
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
  // IMPORTANT: Range requests must be handled explicitly because <audio> seeks
  // send Range headers, and returning a cached 200 for a Range request breaks scrubbing.
  if (url.pathname.startsWith('/snippets/')) {
    event.respondWith(
      caches.match(event.request, { ignoreVary: true })
        .then(cached => {
          if (cached) { return maybeSliceForRange(event.request, cached) }
          return fetch(event.request).then(response => {
            if (response.ok && response.status === 200) {
              // Only cache the full (non-range) response so we have the complete file
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
