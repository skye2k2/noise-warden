/**
 * Service Worker for Noise Warden Timeline.
 * Enables offline access to the timeline page and cached audio snippets.
 *
 * Strategy:
 * - Timeline page: network first, cache fallback (fresh data when online)
 * - Audio snippets: cache first, network fallback (once cached, always offline)
 * - Everything else: pass through (don't interfere with other pages)
 */
const CACHE = 'noise-warden-timeline-cache-v8'

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

/**
 * Slice a cached full response to satisfy a Range request from <audio> seeking.
 * Without this, the browser receives a 200 when it expects 206, and scrubbing breaks.
 * Only handles single-range requests (multi-range is vanishingly rare for audio).
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
