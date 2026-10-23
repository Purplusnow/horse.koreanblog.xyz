/* 홈 화면 설치 — 서비스 워커 등록 + 설치 유도 바 + 계측.
 *
 * **설치를 권하지 않으면 아무도 설치하지 않는다.** 크롬은 설치 가능 조건을
 * 갖춘 사이트에서도 기본 배너를 거의 띄우지 않는다. 그래서 직접 권하되,
 * 크롬이 beforeinstallprompt 를 줄 때만 — 즉 이미 설치 가능하다고 판단한
 * 기기에서만 — 띄운다. iOS 처럼 그 이벤트가 없는 곳에서는 아무것도 하지 않는다.
 *
 * **계측이 이 단계의 목적이다.** 앱으로 갈지 말지는 "알림·홈 화면을 실제로
 * 쓰는 사람이 있는가" 로 정해야 한다. 그래서 설치 제안·수락·실행 모드를
 * GA4 로 보낸다. 숫자가 없으면 다음 결정이 또 감이 된다.
 */
(function () {
  'use strict';

  var DISMISS_KEY = 'horseai.pwa.dismissed';
  var DISMISS_DAYS = 14;          // 거절한 사람에게 매번 들이밀지 않는다

  // **구분은 매개변수가 아니라 이름으로 한다.** GA4 는 이벤트 이름은 그냥
  // 보여 주지만 매개변수는 '맞춤 측정기준'으로 등록해야 조회된다. 등록을
  // 잊으면 이벤트는 멀쩡히 쌓이는데 아무도 못 보고, 그 사실조차 안 보인다.
  // 가짓수가 적은 구분이라 이름에 녹이는 편이 싸다.
  function track(name) {
    // ?noga=1 로 집계를 끈 기기에서는 gtag 자체가 없다 — 조용히 넘어간다.
    if (typeof window.gtag === 'function') window.gtag('event', name);
  }

  function store(fn, dflt) {
    try { return fn(); } catch (e) { return dflt; }   // 사생활 보호 모드 대비
  }

  // ── 1. 서비스 워커 ───────────────────────────────────────
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/sw.js').catch(function () {
        /* 등록이 안 돼도 사이트는 그대로 돈다 — 조용히 넘어간다 */
      });
    });
  }

  // ── 2. 어떤 모드로 열렸나 (홈 화면 실행인지 브라우저인지) ──
  var standalone = store(function () {
    return window.matchMedia('(display-mode: standalone)').matches ||
           window.navigator.standalone === true;
  }, false);
  track(standalone ? 'app_open_standalone' : 'app_open_browser');

  // 홈 화면에서 열었으면 설치를 권할 이유가 없다.
  if (standalone) return;

  // ── 3. 설치 유도 바 ──────────────────────────────────────
  var deferred = null;

  function dismissedRecently() {
    var at = store(function () { return localStorage.getItem(DISMISS_KEY); }, null);
    if (!at) return false;
    return (Date.now() - Number(at)) < DISMISS_DAYS * 864e5;
  }

  function hide(bar) {
    if (bar && bar.parentNode) bar.parentNode.removeChild(bar);
  }

  function show() {
    if (document.getElementById('pwa-bar')) return;

    var bar = document.createElement('div');
    bar.id = 'pwa-bar';
    bar.className = 'pwa-bar';
    bar.innerHTML =
      '<span class="pwa-mark" aria-hidden="true">🐎</span>' +
      '<span class="pwa-text"><b>홈 화면에 추가</b>' +
      '<small>주소창 없이 한 번에 열리고, 연결이 없어도 열어 본 화면은 그대로 보입니다</small></span>' +
      '<button type="button" class="pwa-go">추가</button>' +
      '<button type="button" class="pwa-close" aria-label="닫기">×</button>';
    document.body.appendChild(bar);
    track('pwa_prompt_shown');

    bar.querySelector('.pwa-go').addEventListener('click', function () {
      if (!deferred) return hide(bar);
      hide(bar);
      deferred.prompt();
      deferred.userChoice.then(function (choice) {
        track(choice && choice.outcome === 'accepted'
          ? 'pwa_prompt_accepted' : 'pwa_prompt_declined');
        deferred = null;
      });
    });

    bar.querySelector('.pwa-close').addEventListener('click', function () {
      hide(bar);
      store(function () { return localStorage.setItem(DISMISS_KEY, String(Date.now())); });
      track('pwa_prompt_dismissed');
    });
  }

  window.addEventListener('beforeinstallprompt', function (e) {
    e.preventDefault();          // 크롬 기본 배너 대신 우리 바를 쓴다
    deferred = e;
    if (!dismissedRecently()) show();
  });

  window.addEventListener('appinstalled', function () {
    track('pwa_installed');
    hide(document.getElementById('pwa-bar'));
  });
})();
