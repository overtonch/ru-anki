// ru-anki service worker — keeps the app shell available offline.
// Data (transcripts, the outbound card queue) lives in IndexedDB, handled by
// the page itself; this SW only makes sure the page can load with no network.
const SHELL = 'ru-anki-shell-v80';
const SHELL_URLS = ['/', '/sw.js', '/manifest.json',
  '/icons/icon-192.png', '/icons/apple-touch-icon.png', '/icons/icon.svg'];
// caches that survive an activate/version bump (managed by the page, not the shell)
const KEEP = ['ru-anki-thumbs', 'ru-anki-srs-media'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(SHELL_URLS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== SHELL && !KEEP.includes(k)).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// SRS review clips + frames: cache-first from 'ru-anki-srs-media' (the page
// pre-downloads them via /srs/offline). We always store the full 200; if the
// media element asks for a byte range we slice the cached copy ourselves so an
// offline <audio> gets a proper 206.
async function srsMedia(request) {
  const cache = await caches.open('ru-anki-srs-media');
  const keyReq = new Request(request.url);            // strip Range for the lookup
  let full = await cache.match(keyReq, { ignoreVary: true });
  if (!full) {
    try {
      const net = await fetch(keyReq);
      if (net && net.status === 200) { cache.put(keyReq, net.clone()); full = net; }
      else return net || Response.error();
    } catch (_) {
      return new Response('', { status: 504 });
    }
  }
  const range = request.headers.get('range');
  if (!range || !full) return full;
  const m = /bytes=(\d+)-(\d*)/.exec(range);
  if (!m) return full;
  const buf = await full.clone().arrayBuffer();
  const start = +m[1];
  const end = m[2] ? Math.min(+m[2], buf.byteLength - 1) : buf.byteLength - 1;
  if (start >= buf.byteLength) return new Response('', { status: 416 });
  return new Response(buf.slice(start, end + 1), {
    status: 206,
    headers: {
      'Content-Type': full.headers.get('Content-Type') || 'application/octet-stream',
      'Content-Range': `bytes ${start}-${end}/${buf.byteLength}`,
      'Content-Length': String(end - start + 1),
      'Accept-Ranges': 'bytes',
    },
  });
}

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== self.location.origin) return;

  // App shell: network-first (so updates land), fall back to cache offline.
  if (url.pathname === '/' || url.pathname === '/sw.js') {
    e.respondWith(
      fetch(e.request)
        .then(res => { caches.open(SHELL).then(c => c.put(e.request, res.clone())); return res; })
        .catch(() => caches.match(e.request).then(r => r || caches.match('/')))
    );
    return;
  }

  // Manifest + icons: cache-first (they rarely change, needed for standalone).
  if (url.pathname === '/manifest.json' || url.pathname.startsWith('/icons/')) {
    e.respondWith(
      caches.open(SHELL).then(c => c.match(e.request).then(hit => hit ||
        fetch(e.request).then(res => { c.put(e.request, res.clone()); return res; })))
    );
    return;
  }

  // SRS review media (audio clips + frame thumbnails): cache-first so a review
  // session runs with no connection. The page pre-downloads these into
  // 'ru-anki-srs-media' via /srs/offline; anything not pre-cached still falls
  // through to the network when online.
  if (url.pathname.startsWith('/videos/') &&
      (url.pathname.includes('/clip') || url.pathname.includes('/frame'))) {
    e.respondWith(srsMedia(e.request));
    return;
  }

  // YouTube thumbnails: cache-first so downloaded videos still show art offline.
  if (url.hostname === 'i.ytimg.com') {
    e.respondWith(
      caches.open('ru-anki-thumbs').then(c =>
        c.match(e.request).then(hit => hit ||
          fetch(e.request).then(res => { c.put(e.request, res.clone()); return res; })
            .catch(() => hit))
      )
    );
  }
  // Everything else (API): let it hit the network; the page handles failures.
});
