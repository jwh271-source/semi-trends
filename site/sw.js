// sw.js — 반도체 동향 PWA 서비스 워커 (바닐라, 의존성 없음)
// 전략: 네트워크 우선(network-first) + 런타임 캐시 갱신.
//  - 콘텐츠(index.html, summaries-*.json)는 매일 재생성되므로 온라인일 땐 항상 최신.
//  - 성공한 응답은 런타임 캐시에 저장 → 오프라인 시 "마지막으로 본 버전" 표시.
//  - 앱 셸(manifest, 아이콘)은 설치 시 1회 사전캐시.
const SHELL = 'st-shell-v1';
const RUNTIME = 'st-rt-v1';
const SHELL_FILES = [
  './manifest.webmanifest',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/icon-maskable-512.png',
  './icons/icon-180.png'
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== SHELL && k !== RUNTIME).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// 스코프(/semi-trends/) 내 same-origin GET만 처리.
// 카드의 "원문 보기" 외부 링크는 여기서 그냥 통과됨.
self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  const scopePath = new URL(self.registration.scope).pathname;
  if (!url.pathname.startsWith(scopePath)) return;

  e.respondWith(
    fetch(req).then((res) => {
      if (res && res.ok) {
        // 캐시 키를 pathname으로 정규화 — GitHub Pages의 Vary: Accept-Encoding
        // 헤더와 쿼리스트링(utm 등)으로 인한 캐시 매치 실패를 방지.
        const copy = res.clone();
        caches.open(RUNTIME).then((c) => c.put(new Request(url.pathname), copy));
      }
      return res;
    }).catch(() =>
      caches.match(url.pathname, { ignoreSearch: true }).then((cached) => cached ||
        // 네비게이션이고 정확한 키가 없으면 '/semi-trends/' 캐시라도 시도
        (req.mode === 'navigate' ? caches.match(scopePath) : undefined)
      ).then((r) => r || new Response(
        '오프라인 상태입니다. 이전에 불러온 적 없는 페이지는 볼 수 없습니다.',
        { status: 503, headers: { 'Content-Type': 'text/plain; charset=utf-8' } }))
    )
  );
});
