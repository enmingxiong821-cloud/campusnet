/* 校园网自连 · PWA 外壳缓存。
 *
 * 控制台由本机服务托管，离线时那个服务自然也不可达。这里只缓存外壳
 * （index.html / 图标 / manifest），让「添加到主屏幕」的窗口离线时也能
 * 打开并显示「服务不可达」，而不是白屏；业务数据一律走 /api/ 实时请求，
 * 不缓存、不兜底——失败就让前端的 api() 正常报错。
 */
const CACHE = 'campusnet-shell-v1';
const ASSETS = [
  '/',
  '/index.html',
  '/manifest.webmanifest',
  '/icons/icon.svg',
  '/icons/icon192.png',
  '/icons/icon512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  const url = new URL(event.request.url);
  // 业务接口不缓存、不兜底：离线就离线，让页面如实报「服务不可达」。
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(fetch(event.request));
    return;
  }
  event.respondWith(
    fetch(event.request).catch(() =>
      caches.match(event.request).then((hit) => hit || caches.match('/index.html'))
    )
  );
});
