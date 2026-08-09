/* 발주 시각이 지난 경주를 흐리게 하고, 다음 경주까지 남은 시간을 띄운다.

   서버는 하루 몇 번만 굽는다. 그 사이에도 시간은 흐르므로, 이미 끝난 경주가
   앞으로 뛸 경주와 같은 낯으로 남아 있으면 목록이 낡아 보인다. 판단 근거는
   브라우저 시계 하나뿐이라 API 도 갱신도 필요 없다.

   경주 시각은 모두 한국 시각이다. 방문자가 어느 시간대에 있든 같은 값이
   나오도록 UTC 에서 KST 를 직접 만든다. */
(function () {
  var KST = 9 * 60 * 60 * 1000;
  var cards = [].slice.call(document.querySelectorAll('.race-item[data-post]'));
  if (!cards.length) return;

  var items = cards.map(function (el) {
    var t = el.getAttribute('data-post');           // "2026-08-09T10:35"
    var m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(t);
    if (!m) return null;
    // Date.UTC 로 만든 뒤 아홉 시간을 빼면 그 KST 시각의 실제 순간이 된다
    var at = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]) - KST;
    return { el: el, at: at };
  }).filter(Boolean);

  function tick() {
    var now = Date.now();
    items.forEach(function (it) {
      it.el.classList.toggle('is-past', now >= it.at);
    });
  }

  tick();
  setInterval(tick, 30000);
  // 탭을 다시 열었을 때 즉시 맞춘다 — 배경 탭은 타이머가 느려진다
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) tick();
  });
})();
