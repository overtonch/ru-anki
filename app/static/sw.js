// ru-anki service worker — keeps the app shell available offline.
// Data (transcripts, the outbound card queue) lives in IndexedDB, handled by
// the page itself; this SW only makes sure the page can load with no network.
const SHELL = 'ru-anki-shell-v21';
const SHELL_URLS = ['/', '/sw.js', '/manifest.json',
  '/icons/icon-192.png', '/icons/apple-touch-icon.png', '/icons/icon.svg'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(SHELL_URLS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== SHELL).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

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
