// l0wagent's service worker: the installable app's shell offline, and push notifications.
// The API is never cached here (the app keeps its own offline copy in IndexedDB).
const CACHE = 'l0w-shell-v1'
const SHELL = ['/', '/manifest.webmanifest', '/favicon.svg', '/icons/icon-192.png']

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()))
})

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()))
})

self.addEventListener('fetch', (e) => {
  const req = e.request
  const url = new URL(req.url)
  if (req.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return
  if (url.pathname.startsWith('/assets/')) {
    // Hashed file names: a cached copy is always right.
    e.respondWith(caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy)) }
      return res
    })))
    return
  }
  if (req.mode === 'navigate') {
    // The app: the network first (so updates arrive), the cached shell when offline.
    e.respondWith(fetch(req).then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put('/', copy)) }
      return res
    }).catch(() => caches.match('/')))
    return
  }
  e.respondWith(caches.match(req).then((hit) => {
    const net = fetch(req).then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy)) }
      return res
    })
    return hit || net
  }))
})

self.addEventListener('push', (e) => {
  let data = {}
  try { data = e.data ? e.data.json() : {} } catch { data = { title: e.data && e.data.text() } }
  e.waitUntil(self.registration.showNotification(data.title || 'l0wagent', {
    body: data.body || '', tag: data.tag, data: { url: data.url || '/' }, icon: '/icons/icon-192.png', badge: '/icons/icon-192.png',
  }))
})

self.addEventListener('notificationclick', (e) => {
  e.notification.close()
  const url = (e.notification.data && e.notification.data.url) || '/'
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
    for (const w of wins) {
      if (new URL(w.url).origin === self.location.origin) { w.focus(); return w.navigate(url) }
    }
    return self.clients.openWindow(url)
  }))
})
