/* 서비스 워커 — 오프라인 열람과 홈 화면 설치를 위한 최소 구성.
 *
 * **예상·결과는 절대 캐시를 먼저 보여 주지 않는다.** 이 사이트의 화면은
 * 시간에 묶여 있다 — 발주 전 예상, 방금 끝난 경주의 착순. 캐시 우선으로
 * 두면 네트워크가 멀쩡한데도 '어제 화면'이 떠서, 사용자는 그것이 오늘자인
 * 줄 안다. 조용히 틀린 화면이 조용히 틀린 숫자보다 나을 게 없다.
 * 그래서 문서 요청은 network-first, 캐시는 끊겼을 때의 보험으로만 쓴다.
 *
 * 반대로 /static/ 의 CSS·JS·아이콘은 주소에 내용 해시(?v=)가 붙어 있어
 * 바뀌면 주소가 바뀐다. 그러니 캐시 우선으로 둬도 낡을 수가 없다.
 *
 * 교차 출처 요청(애드센스·GA)은 **가로채지 않는다.** 광고 요청에 손대면
 * 게재가 깨지거나 집계가 틀어지고, 얻을 것도 없다.
 *
 * 이 파일은 templates/sw.js 에서 렌더된다 — CACHE 이름의 판 번호가 배포마다
 * 바뀌어야 옛 캐시가 남지 않기 때문이다. /sw.js (뿌리)로 나가야 사이트
 * 전체를 담당한다. /static/ 아래에 두면 그 폴더만 맡는다.
 */
const CACHE = 'horseai-{{ sw_version }}';
const SHELL = {{ sw_shell | tojson }};

self.addEventListener('install', (e) => {
  // 경주 당일 고친 것이 다음 방문에 바로 닿아야 한다 — 기다리지 않는다.
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter((n) => n !== CACHE).map((n) => caches.delete(n)));
    await self.clients.claim();
  })());
});

// 네트워크가 아예 없을 때 내보낼 마지막 화면. 캐시에 '/' 조차 없을 때만 쓴다.
const OFFLINE_HTML =
  '<!doctype html><html lang="ko"><meta charset="utf-8">' +
  '<meta name="viewport" content="width=device-width,initial-scale=1">' +
  '<title>연결 없음</title>' +
  '<style>body{font-family:-apple-system,"Apple SD Gothic Neo",sans-serif;' +
  'margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;' +
  'background:#f6f7f9;color:#14181f;text-align:center;padding:24px}' +
  'p{color:#5a6472;line-height:1.6}</style>' +
  '<div><h1>🐎</h1><h2>연결이 없습니다</h2>' +
  '<p>인터넷에 연결되면 오늘의 예상을 다시 불러옵니다.<br>' +
  '한 번 열어 본 화면은 연결 없이도 볼 수 있습니다.</p></div>';

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;   // 광고·집계는 건드리지 않는다

  // ── 문서: 네트워크 우선 ────────────────────────────────────
  if (req.mode === 'navigate') {
    e.respondWith((async () => {
      try {
        const res = await fetch(req);
        // 성공한 응답만 보관한다. 404 를 캐시하면 그 주소가 영영 404 가 된다.
        if (res && res.ok) (await caches.open(CACHE)).put(req, res.clone());
        return res;
      } catch (err) {
        const hit = (await caches.match(req)) || (await caches.match('/'));
        return hit || new Response(OFFLINE_HTML,
          { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } });
      }
    })());
    return;
  }

  // ── /static/ : 캐시 우선 (주소에 해시가 붙어 낡을 수 없다) ──
  if (url.pathname.startsWith('/static/')) {
    e.respondWith((async () => {
      const hit = await caches.match(req);
      if (hit) return hit;
      const res = await fetch(req);
      if (res && res.ok) (await caches.open(CACHE)).put(req, res.clone());
      return res;
    })());
  }

  // 그 밖(races.json 등)은 그대로 둔다 — 최신이어야 하고, 실패해도 화면이 산다.
});
