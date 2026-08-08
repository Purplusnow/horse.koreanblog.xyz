/* 경주 미리보기 플레이어 — 원형(타원) 주로.
 *
 * 직선 막대로 그리면 '누가 앞서 있나'는 보이지만 경마로 읽히지 않는다. 실제
 * 주로는 직선과 곡선이 번갈아 나오고, 곡선에서 바깥으로 도는 말은 그만큼 더
 * 뛴다 — 마번 1~4번이 6번 이후보다 승률이 2%p 높은 이유가 그것이다.
 *
 * 서버가 주는 것은 마리당 '구간 통과 시각'과 '레인(안쪽에서 몇 두 밖)' 뿐이고,
 * 트랙 좌표 계산은 전부 여기서 한다. 정적 사이트에서 그대로 돈다.
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
  var duration = data.duration;
  var distance = data.distance;
  var spec = data.track || { lap: 1600, straight: 450, curve: 350 };
  var LAP = spec.lap, ST = spec.straight, CV = spec.curve;

  var STYLE_COLOR = {
    front: '#c8621f',   // 선행 — 따뜻한 쪽
    stalk: '#2f5490',   // 선입
    close: '#1f6b4c',   // 추입 — 잔디 그린
    unknown: '#7a8593'
  };

  var playing = false, t = 0, speed = 1, raf = null, last = 0;

  /* ── 주로 기하 ────────────────────────────────────────────────
     경주로를 '스타디움' 모양으로 본다: 직선 2개 + 반원 2개.
     결승선을 진행거리 0 으로 두고, 거꾸로 distance 만큼 물러난 곳이 출발점이다.

     s(m) → 정규화 좌표. seg 는 어느 구간인지(straight/curve), a 는 반원에서의 각도.
     반시계 방향(한국마사회 세 경마장 모두)으로 돈다. */
  function trackPoint(s) {
    var u = ((s % LAP) + LAP) % LAP;
    if (u < ST) return { seg: 'S', side: 1, k: u / ST };           // 홈스트레치
    u -= ST;
    if (u < CV) return { seg: 'C', side: 1, a: Math.PI * (u / CV) }; // 1·2코너
    u -= CV;
    if (u < ST) return { seg: 'S', side: -1, k: 1 - u / ST };      // 백스트레치
    u -= ST;
    return { seg: 'C', side: -1, a: Math.PI * (u / CV) };          // 3·4코너
  }

  /* 정규화 좌표 + 레인 → 화면 픽셀 */
  function toPx(p, box, lane) {
    var off = (lane || 0) * box.laneStep;
    var ry = box.innerH / 2;
    if (p.seg === 'S') {
      var x = box.left + (p.side === 1 ? p.k : p.k) * box.innerW;
      return { x: x, y: box.cy + p.side * (ry + off) };
    }
    var cx = box.left + (p.side === 1 ? box.innerW : 0);
    var rr = ry + off;
    var dir = p.side === 1 ? 1 : -1;
    return {
      x: cx + dir * Math.sin(p.a) * rr,
      y: box.cy + dir * Math.cos(p.a) * rr
    };
  }

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
    var h = Math.max(250, Math.min(400, w * 0.5));
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      canvas.style.height = h + 'px';
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { w: w, h: h };
  }

  function pathAt(box, lane) {
    ctx.beginPath();
    var n = 200;
    for (var k = 0; k <= n; k++) {
      var pt = toPx(trackPoint(k / n * LAP), box, lane);
      if (k === 0) ctx.moveTo(pt.x, pt.y); else ctx.lineTo(pt.x, pt.y);
    }
    ctx.closePath();
  }

  function draw() {
    var size = cssSize();
    var W = size.w, H = size.h;
    var lanes = Math.max(5, Math.min(12, runners.length));
    var box = {
      left: 92, innerW: W - 92 - 34,
      innerH: H - 74, cy: H / 2, laneStep: 3.0
    };
    var st = getComputedStyle(document.documentElement);
    var cFaint = st.getPropertyValue('--text-faint') || '#8b95a3';
    var cText = st.getPropertyValue('--text') || '#14181f';
    var cSunken = st.getPropertyValue('--bg-sunken') || '#eef0f3';

    ctx.clearRect(0, 0, W, H);

    /* 주로 바닥 */
    ctx.strokeStyle = cSunken;
    ctx.lineWidth = lanes * box.laneStep + 14;
    ctx.lineJoin = 'round';
    pathAt(box, lanes / 2);
    ctx.stroke();

    /* 안쪽·바깥 레일 */
    ctx.strokeStyle = cFaint; ctx.lineWidth = 1.2; ctx.globalAlpha = 0.4;
    pathAt(box, 0); ctx.stroke();
    pathAt(box, lanes); ctx.stroke();
    ctx.globalAlpha = 1;

    /* 결승선 */
    var f0 = toPx(trackPoint(0), box, 0);
    var f1 = toPx(trackPoint(0), box, lanes);
    ctx.strokeStyle = '#b4842a'; ctx.lineWidth = 3;
    ctx.beginPath(); ctx.moveTo(f0.x, f0.y); ctx.lineTo(f1.x, f1.y); ctx.stroke();
    ctx.fillStyle = '#b4842a';
    ctx.font = 'bold 11px system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('결승', f0.x, f0.y + 15);

    /* 출발점 */
    var s0 = toPx(trackPoint(-distance), box, lanes / 2);
    ctx.fillStyle = cFaint;
    ctx.font = '11px system-ui, sans-serif';
    ctx.fillText('출발 ' + Math.round(distance) + 'm', s0.x, s0.y - 12);

    /* 현재 순위 */
    var order = runners.map(function (r, i) {
      return { i: i, p: progressAt(r.splits, t) };
    }).sort(function (a, b) { return b.p - a.p; });
    var rankOf = {};
    order.forEach(function (o, idx) { rankOf[o.i] = idx + 1; });

    /* 말 — 레인은 안쪽(0)부터 바깥으로 */
    runners.forEach(function (r, i) {
      var p = progressAt(r.splits, t);
      var pt = toPx(trackPoint(-distance + p * distance), box, (r.lane || 0) + 0.6);
      var color = STYLE_COLOR[r.style] || STYLE_COLOR.unknown;
      ctx.beginPath();
      ctx.arc(pt.x, pt.y, 9.5, 0, Math.PI * 2);
      ctx.fillStyle = color; ctx.fill();
      if (rankOf[i] === 1) { ctx.strokeStyle = '#b4842a'; ctx.lineWidth = 2.5; ctx.stroke(); }
      var label = String(r.gate);
      ctx.fillStyle = '#fff';
      ctx.font = 'bold ' + (label.length > 1 ? 9 : 11) + 'px system-ui, sans-serif';
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(label, pt.x, pt.y + 0.5);
    });
    ctx.textBaseline = 'alphabetic';

    /* 순위표 — 트랙 왼쪽 바깥 */
    ctx.textAlign = 'left';
    ctx.font = '11.5px system-ui, sans-serif';
    order.slice(0, 6).forEach(function (o, idx) {
      var r = runners[o.i];
      ctx.fillStyle = idx === 0 ? cText : cFaint;
      ctx.fillText((idx + 1) + '. ' + r.gate + ' ' + r.name, 6, 16 + idx * 15);
    });
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

  /* 108.4초 보다 '1분 48.4초' 가 경마 기록으로 읽힌다. 중계도 기록지도 그렇게 적는다. */
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
