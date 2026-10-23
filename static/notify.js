/* 경주일 알림 — 구독 신청과 해지.
 *
 * **권한은 절대 저절로 묻지 않는다.** 화면이 열리자마자 알림 권한을 물으면
 * 크롬이 아예 막아 버리고, 사람은 영문도 모르고 거절한다. 한 번 거절당하면
 * 그 기기에는 다시 물을 수 없다 — 권한은 한 번뿐인 자산이라 눌러서 들어온
 * 사람에게만 쓴다.
 *
 * 설치 바와 동시에 띄우지 않는다. 둘 다 아래에서 올라오면 화면을 반이나
 * 덮고, 어느 쪽도 제대로 안 읽힌다. 설치가 먼저다 — 홈 화면에 들어간 다음에
 * 알림을 권해야 받을 이유가 분명해진다.
 *
 * 주소와 공개키는 템플릿이 data 속성으로 넘긴다. 둘 중 하나라도 비어 있으면
 * 이 파일은 아무 일도 하지 않는다(설정 전에는 기능 자체가 없는 것과 같다).
 */
(function () {
  'use strict';

  var me = document.currentScript;
  var API = (me && me.dataset.endpoint || '').replace(/\/+$/, '');
  var VAPID = me && me.dataset.key || '';
  if (!API || !VAPID) return;

  var DISMISS_KEY = 'horseai.notify.dismissed';
  var DISMISS_DAYS = 30;

  // 구분은 이름으로 한다 — 매개변수는 GA4 에 맞춤 측정기준으로 등록해야만
  // 조회되고, 등록을 잊으면 쌓이기만 하고 아무도 못 본다(pwa.js 와 같은 규칙).
  function track(name) {
    if (typeof window.gtag === 'function') window.gtag('event', name);
  }
  function store(fn, dflt) {
    try { return fn(); } catch (e) { return dflt; }
  }

  // base64url 공개키를 푸시 API 가 받는 바이트 배열로. 패딩을 되돌려야 한다.
  function keyBytes(b64) {
    var pad = '='.repeat((4 - (b64.length % 4)) % 4);
    var raw = atob((b64 + pad).replace(/-/g, '+').replace(/_/g, '/'));
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  function post(path, body) {
    return fetch(API + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }

  var supported = 'serviceWorker' in navigator &&
                  'PushManager' in window &&
                  'Notification' in window;
  if (!supported) return;

  // ── 구독 ────────────────────────────────────────────────
  async function subscribe() {
    var reg = await navigator.serviceWorker.ready;
    var sub = await reg.pushManager.getSubscription();
    if (sub) {
      // 공개키를 바꾼 적이 있으면 옛 구독으로는 못 보낸다 — 갈아 끼운다.
      var cur = new Uint8Array(sub.options.applicationServerKey || []);
      var want = keyBytes(VAPID);
      var same = cur.length === want.length &&
                 cur.every(function (v, i) { return v === want[i]; });
      if (!same) { await sub.unsubscribe(); sub = null; }
    }
    if (!sub) {
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,            // 조용한 푸시는 쓰지 않는다
        applicationServerKey: keyBytes(VAPID),
      });
    }
    var res = await post('/subscribe', sub.toJSON());
    if (!res.ok) throw new Error('창구가 구독을 받지 않았습니다 (' + res.status + ')');
    return sub;
  }

  async function unsubscribe() {
    var reg = await navigator.serviceWorker.ready;
    var sub = await reg.pushManager.getSubscription();
    if (!sub) return;
    await post('/unsubscribe', { endpoint: sub.endpoint }).catch(function () {});
    await sub.unsubscribe();
  }

  // ── 화면 ────────────────────────────────────────────────
  function toast(text) {
    var t = document.createElement('div');
    t.className = 'nt-toast';
    t.textContent = text;
    document.body.appendChild(t);
    setTimeout(function () { t.remove(); }, 4000);
  }

  function showFooterToggle() {
    if (document.getElementById('nt-off')) return;
    var foot = document.querySelector('.site-footer .wrap');
    if (!foot) return;
    var p = document.createElement('p');
    p.className = 'footer-meta';
    p.innerHTML = '경주일 아침에 알림을 받고 있습니다 · ' +
                  '<button type="button" id="nt-off" class="nt-link">알림 끄기</button>';
    foot.appendChild(p);
    p.querySelector('#nt-off').addEventListener('click', async function () {
      try {
        await unsubscribe();
        p.textContent = '알림을 껐습니다.';
        track('push_unsubscribed');
      } catch (e) { toast('알림을 끄지 못했습니다.'); }
    });
  }

  function dismissedRecently() {
    var at = store(function () { return localStorage.getItem(DISMISS_KEY); }, null);
    return !!at && (Date.now() - Number(at)) < DISMISS_DAYS * 864e5;
  }

  function showBar() {
    if (document.getElementById('nt-bar') || document.getElementById('pwa-bar')) return;
    var bar = document.createElement('div');
    bar.id = 'nt-bar';
    bar.className = 'pwa-bar nt-bar';
    bar.innerHTML =
      '<span class="pwa-mark" aria-hidden="true">🔔</span>' +
      '<span class="pwa-text"><b>경주일 아침에 알려드릴까요?</b>' +
      '<small>출전표가 차고 그날 예상이 올라오면 한 번만 알립니다</small></span>' +
      '<button type="button" class="pwa-go nt-go">알림 받기</button>' +
      '<button type="button" class="pwa-close nt-close" aria-label="닫기">×</button>';
    document.body.appendChild(bar);
    track('push_prompt_shown');

    bar.querySelector('.nt-go').addEventListener('click', async function () {
      bar.remove();
      var perm = await Notification.requestPermission();
      track('push_permission_' + perm);   // granted · denied · default
      if (perm !== 'granted') {
        if (perm === 'denied') toast('브라우저 설정에서 알림을 허용해야 받을 수 있습니다.');
        return;
      }
      try {
        await subscribe();
        toast('경주일 아침에 알려드리겠습니다.');
        track('push_subscribed');
        showFooterToggle();
      } catch (e) {
        toast('알림 신청에 실패했습니다. 잠시 뒤 다시 시도해 주세요.');
        track('push_subscribe_failed');
      }
    });

    bar.querySelector('.nt-close').addEventListener('click', function () {
      bar.remove();
      store(function () { return localStorage.setItem(DISMISS_KEY, String(Date.now())); });
      track('push_prompt_dismissed');
    });
  }

  // ── 시작 ────────────────────────────────────────────────
  //
  // **설치 바에 양보하되, 비켜날 때까지 기다린다.** 예전에는 설치 바가 떠
  // 있으면 그 방문은 그냥 포기했는데, 설치 바는 설치하거나 닫기 전까지 계속
  // 떠 있으므로 앱을 안 깐 사람에게는 알림 신청이 영영 안 나왔다. 양보는
  // 자리를 비키라는 뜻이지 사라지라는 뜻이 아니다.
  var WAIT_MS = 3000;
  var MAX_WAIT = 40;          // 3초 × 40 = 2분이면 설치 바는 결판이 나 있다

  function tryShow(tries) {
    if (document.getElementById('pwa-bar')) {
      if (tries < MAX_WAIT) setTimeout(function () { tryShow(tries + 1); }, WAIT_MS);
      return;
    }
    showBar();
  }

  // load 를 기다리지 않는다 — 이 파일은 body 끝에서 실행되므로 DOM 은 이미
  // 서 있고, 광고 iframe 이 늦으면 load 는 한참 뒤에 뜬다.
  setTimeout(async function () {
    if (Notification.permission === 'granted') {
      // 이미 허락한 사람. 창구에 명단이 남아 있는지는 알 수 없으므로 조용히
      // 다시 등록한다 — 워커가 같은 키로 덮어쓰므로 중복이 쌓이지 않는다.
      try { await subscribe(); showFooterToggle(); } catch (e) { /* 다음 방문에 */ }
      return;
    }
    if (Notification.permission === 'denied') return;   // 다시 물을 수 없다
    if (dismissedRecently()) return;
    tryShow(0);
  }, 1500);   // 화면이 자리를 잡은 뒤에 올라오게 한다
})();
