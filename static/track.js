/* 경주 미리보기 플레이어.
 *
 * 시뮬레이션이 만든 '구간별 통과 시각'을 시간축으로 보간해 각 마필의 위치를
 * 그린다. 서버가 넘겨주는 건 마리당 숫자 몇 개뿐이고, 나머지는 전부 여기서
 * 계산하므로 정적 사이트에서도 그대로 돈다.
 */
(function () {
  var el = document.getElementById('sim-data');
  var canvas = document.getElementById('track');
  if (!el || !canvas || !canvas.getContext) return;

  var data;
  try { data = JSON.parse(el.textContent); } catch (e) { return; }
  if (!data || !data.runners || !data.runners.length) return;

  var ctx = canvas.getContext('2d');
  var runners = data.runners;
  var nSeg = data.n_segments;
  var duration = data.duration;
  var distance = data.distance;

  var STYLE_COLOR = {
    front: '#c8621f',   // 선행 — 따뜻한 쪽
    stalk: '#2f5490',   // 선입
    close: '#1f6b4c',   // 추입 — 잔디 그린
    unknown: '#7a8593'
  };

  var playing = false, t = 0, speed = 1, raf = null, last = 0;
  var PAD_L = 54, PAD_R = 96, PAD_T = 34, PAD_B = 20;

  /* 시각 t 에서의 진행률(0~1). 구간 통과 시각 사이를 선형 보간한다. */
  function progressAt(splits, time) {
    if (time <= 0) return 0;
    if (time >= splits[splits.length - 1]) return 1;
    var prev = 0;
    for (var k = 0; k < splits.length; k++) {
      if (time < splits[k]) {
        var span = splits[k] - prev || 1;
        return (k + (time - prev) / span) / splits.length;
      }
      prev = splits[k];
    }
    return 1;
  }

  function cssSize() {
    var w = canvas.clientWidth || canvas.parentNode.clientWidth || 900;
    var dpr = window.devicePixelRatio || 1;
    var h = Math.max(210, Math.min(430, 54 + runners.length * 26));
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      canvas.style.height = h + 'px';
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { w: w, h: h };
  }

  function draw() {
    var size = cssSize();
    var W = size.w, H = size.h;
    var laneH = (H - PAD_T - PAD_B) / runners.length;
    var x0 = PAD_L, x1 = W - PAD_R;
    var style = getComputedStyle(document.documentElement);
    var cText = style.getPropertyValue('--text') || '#14181f';
    var cFaint = style.getPropertyValue('--text-faint') || '#8b95a3';
    var cBorder = style.getPropertyValue('--border') || '#e3e6ea';
    var cSunken = style.getPropertyValue('--bg-sunken') || '#eef0f3';

    ctx.clearRect(0, 0, W, H);

    /* 구간 눈금 — 200m 마다 */
    ctx.font = '11px system-ui, sans-serif';
    ctx.textAlign = 'center';
    for (var k = 0; k <= nSeg; k++) {
      var gx = x0 + (x1 - x0) * (k / nSeg);
      ctx.strokeStyle = (k === nSeg) ? '#b4842a' : cBorder;
      ctx.lineWidth = (k === nSeg) ? 2 : 1;
      ctx.beginPath(); ctx.moveTo(gx, PAD_T - 12); ctx.lineTo(gx, H - PAD_B + 2); ctx.stroke();
      ctx.fillStyle = cFaint;
      var remain = Math.round(distance - (distance * k / nSeg));
      ctx.fillText(k === nSeg ? '결승' : (remain + 'm'), gx, PAD_T - 18);
    }

    /* 각 마필의 레인 */
    var order = runners.map(function (r, i) {
      return { i: i, p: progressAt(r.splits, t) };
    }).sort(function (a, b) { return b.p - a.p; });

    var rankOf = {};
    order.forEach(function (o, idx) { rankOf[o.i] = idx + 1; });

    runners.forEach(function (r, i) {
      var y = PAD_T + laneH * i + laneH / 2;
      ctx.strokeStyle = cSunken;
      ctx.lineWidth = Math.max(9, laneH - 8);
      ctx.lineCap = 'round';
      ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(x1, y); ctx.stroke();

      var p = progressAt(r.splits, t);
      var color = STYLE_COLOR[r.style] || STYLE_COLOR.unknown;

      /* 지나온 궤적 */
      if (p > 0) {
        ctx.strokeStyle = color;
        ctx.globalAlpha = 0.22;
        ctx.lineWidth = Math.max(9, laneH - 8);
        ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(x0 + (x1 - x0) * p, y); ctx.stroke();
        ctx.globalAlpha = 1;
      }

      /* 마번 마커 */
      var mx = x0 + (x1 - x0) * p;
      var rad = Math.max(8, Math.min(12, laneH / 2 - 1));
      ctx.beginPath(); ctx.arc(mx, y, rad, 0, Math.PI * 2);
      ctx.fillStyle = color; ctx.fill();
      if (rankOf[i] === 1) {           /* 현재 선두는 테두리로 표시 */
        ctx.strokeStyle = '#b4842a'; ctx.lineWidth = 2.5; ctx.stroke();
      }
      /* 두 자리 마번은 글자를 줄여 원 밖으로 넘치지 않게 한다 */
      var label = String(r.gate);
      ctx.fillStyle = '#fff';
      ctx.font = 'bold ' + Math.round(rad * (label.length > 1 ? 0.82 : 1.05)) + 'px system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(label, mx, y + 0.5);

      /* 왼쪽: 순위 · 오른쪽: 마명 */
      ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
      ctx.fillStyle = cFaint;
      ctx.font = '12px system-ui, sans-serif';
      ctx.fillText(rankOf[i] + '위', x0 - 10, y);

      ctx.textAlign = 'left';
      ctx.fillStyle = (p >= 1) ? cText : cFaint;
      ctx.font = (rankOf[i] === 1 ? 'bold ' : '') + '12.5px system-ui, sans-serif';
      ctx.fillText(r.name, x1 + 10, y);
    });

    ctx.textBaseline = 'alphabetic';
  }

  function tick(now) {
    if (!last) last = now;
    var dt = (now - last) / 1000;
    last = now;
    if (playing) {
      t += dt * speed;
      if (t >= duration) { t = duration; stop(); }
      sync();
    }
    draw();
    raf = requestAnimationFrame(tick);
  }

  var btnPlay = document.getElementById('tk-play');
  var btnReplay = document.getElementById('tk-replay');
  var selSpeed = document.getElementById('tk-speed');
  var seek = document.getElementById('tk-seek');
  var clock = document.getElementById('tk-clock');

  /* 108.4초 보다 '1분 48.4초' 가 경마 기록으로 읽힌다. 실제 중계·기록지도
     분 단위로 적는다. 1분 미만은 초만 쓴다. */
  function fmtTime(sec) {
    var m = Math.floor(sec / 60);
    var s = sec - m * 60;
    return m > 0 ? m + '분 ' + s.toFixed(1) + '초' : s.toFixed(1) + '초';
  }

  function sync() {
    if (seek) seek.value = String(Math.round((t / duration) * 1000));
    if (clock) clock.textContent = fmtTime(t);
  }
  function start() { playing = true; if (btnPlay) btnPlay.textContent = '❚❚ 정지'; }
  function stop() { playing = false; if (btnPlay) btnPlay.textContent = '▶ 재생'; }

  if (btnPlay) btnPlay.addEventListener('click', function () {
    if (t >= duration) t = 0;
    playing ? stop() : start();
  });
  if (btnReplay) btnReplay.addEventListener('click', function () { t = 0; sync(); start(); });
  if (selSpeed) selSpeed.addEventListener('change', function () { speed = parseFloat(selSpeed.value) || 1; });
  if (seek) seek.addEventListener('input', function () {
    stop(); t = (parseFloat(seek.value) / 1000) * duration; sync();
  });
  window.addEventListener('resize', draw);

  sync();
  raf = requestAnimationFrame(tick);
})();
