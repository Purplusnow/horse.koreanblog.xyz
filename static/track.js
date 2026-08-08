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

  /* 마번별 모자색. 실제 경마에서 마번을 구분하는 방식 그대로다 —
     각질(선행·추입)로 칠하면 같은 색이 여러 마리라 서로 구별되지 않는다.
     [배경, 글자] 순. 흰색·노란색 모자는 글자를 어둡게 쓴다. */
  var GATE_COLOR = [
    ['#ffffff', '#22262c'], ['#22262c', '#ffffff'], ['#d43b34', '#ffffff'],
    ['#2f5490', '#ffffff'], ['#f2c53d', '#22262c'], ['#2e9e5b', '#ffffff'],
    ['#e8802a', '#ffffff'], ['#e77fa5', '#ffffff'], ['#7fc4e8', '#22262c'],
    ['#8b5cc7', '#ffffff'], ['#a8cf4a', '#22262c'], ['#8a5a3c', '#ffffff'],
    ['#9aa3ad', '#ffffff'], ['#1f3f7a', '#ffffff'], ['#00a0a0', '#ffffff'],
    ['#c0392b', '#ffffff']
  ];
  function gateColor(g) { return GATE_COLOR[((g || 1) - 1) % GATE_COLOR.length]; }

  var playing = false, t = 0, speed = 1, raf = null, last = 0, follow = true;
  var leadIdx = 0;

  /* ── 주로 기하 ────────────────────────────────────────────────
     경주로를 '스타디움' 모양으로 본다: 직선 2개 + 반원 2개.

     **결승선은 직선주로의 끝**이다. 마지막 코너를 돈 뒤 ST 미터를 달려 결승선에
     들어온다. 결승선을 코너 바로 뒤에 두면 그 직선이 통째로 사라져, 실제
     경주로와 전혀 다른 그림이 된다.

     결승선을 진행거리 0 으로 두고 **거꾸로** 재면 다음 순서다.
       0 ~ ST         결승 직선주로
       ST ~ ST+CV     마지막 곡선 (3·4코너)
       ~ 2ST+CV       반대편 직선
       ~ 2ST+2CV      첫 곡선 (1·2코너)
     거리가 길수록 출발점이 뒤로 물러난다 — 실제 경마와 같다.

     s 는 결승선 기준 '남은 거리'(음수가 아니라 뒤로 간 거리)로 받는다. */
  function trackPoint(s) {
    var back = ((-s % LAP) + LAP) % LAP;      // 결승선에서 거꾸로 간 거리
    // 거꾸로 가면 곡선의 좌우도 뒤바뀐다. 결승선(우하단)에서 홈 직선을 왼쪽으로
    // 거슬러 오르면 그 앞은 **왼쪽** 곡선이다. 여기를 바꾸지 않으면 좌하단에서
    // 우측 곡선으로 건너뛰어 경로가 끊긴다.
    if (back < ST) return { seg: 'S', side: 1, k: 1 - back / ST };        // 홈 직선 우→좌
    back -= ST;
    if (back < CV) return { seg: 'C', side: -1, a: Math.PI * (1 - back / CV) };  // 좌곡선 아래→위
    back -= CV;
    if (back < ST) return { seg: 'S', side: -1, k: back / ST };           // 백스트레치 좌→우
    back -= ST;
    return { seg: 'C', side: 1, a: Math.PI * (1 - back / CV) };           // 우곡선 위→아래
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

  /* 시각 t 에서의 주행 레인. 구간별 값을 진행률로 보간한다.
     실제 경주처럼 안쪽이 비면 들어가고 막히면 밖으로 나므로, 경주 내내
     같은 자리를 도는 그림이 나오지 않는다. */
  function laneAt(r, p) {
    var L = r.lanes;
    if (!L || !L.length) return r.lane || 0;
    var f = p * (L.length - 1), k = Math.floor(f), g = f - k;
    if (k >= L.length - 1) return L[L.length - 1];
    return L[k] + (L[k + 1] - L[k]) * g;
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

  /* 화면 배치.
     반원은 안쪽 레일 반지름(ry)만큼 좌우로 더 나간다. 그 몫을 폭에서 빼지 않으면
     코너가 캔버스 밖으로 잘린다 — 처음에 그렇게 그려졌다. */
  function layout(W, H) {
    var lanes = Math.max(5, Math.min(14, runners.length));
    // 레인 간격은 마커가 '반지름 이상 겹치지 않을' 만큼은 돼야 한다.
    // 4px 이던 때는 레인을 제대로 계산하고도 12두가 한 점에 뭉쳐 보였다.
    var laneStep = 10.0;
    // 말은 가장 바깥 레인 + 마커 반지름까지 나간다. 그 몫을 다 빼야 잘리지 않는다.
    var outer = lanes * laneStep + 12;
    var pad = 10;
    var ry = Math.max(26, (H - pad * 2) / 2 - outer);
    var innerW = Math.max(60, W - pad * 2 - (ry + outer) * 2);
    return {
      left: pad + ry + outer, innerW: innerW,
      innerH: ry * 2, cy: H / 2, laneStep: laneStep, lanes: lanes
    };
  }

  function draw() {
    var size = cssSize();
    var W = size.w, H = size.h;
    var box = layout(W, H);
    var lanes = box.lanes;
    var st = getComputedStyle(document.documentElement);
    var cFaint = st.getPropertyValue('--text-faint') || '#8b95a3';
    var cText = st.getPropertyValue('--text') || '#14181f';
    var cSunken = st.getPropertyValue('--bg-sunken') || '#eef0f3';

    ctx.clearRect(0, 0, W, H);

    /* 카메라 — 무리를 따라가며 확대한다.
       경주 중 말들은 실제로 뭉쳐 다니므로, 전체를 담으면 마번을 읽을 수 없다.
       다만 **트랙만 확대하고 마커는 화면 좌표에 고정 크기로 그린다**. 마커까지
       배율에 곱하면 원이 커져 서로 겹치고, 확대한 의미가 사라진다. */
    var raw = runners.map(function (r) {
      var p = progressAt(r.splits, t);
      return toPx(trackPoint(-distance + p * distance), box, laneAt(r, p) + 0.6);
    });
    var cam = { z: 1, cx: W / 2, cy: H / 2 };
    if (follow && raw.length) {
      var xs = raw.map(function (p) { return p.x; }), ys = raw.map(function (p) { return p.y; });
      cam.cx = (Math.min.apply(null, xs) + Math.max.apply(null, xs)) / 2;
      cam.cy = (Math.min.apply(null, ys) + Math.max.apply(null, ys)) / 2;
      var spread = Math.max(
        Math.max.apply(null, xs) - Math.min.apply(null, xs),
        (Math.max.apply(null, ys) - Math.min.apply(null, ys)) * 1.6) + 60;
      cam.z = Math.max(1, Math.min(2.6, Math.min(W, H * 1.7) / spread));
    }
    function screenPt(p) {
      return { x: W / 2 + (p.x - cam.cx) * cam.z, y: H / 2 + (p.y - cam.cy) * cam.z };
    }
    var pts = raw.map(screenPt);

    ctx.save();
    ctx.translate(W / 2, H / 2);
    ctx.scale(cam.z, cam.z);
    ctx.translate(-cam.cx, -cam.cy);

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
    /* 출발선 */
    var s0 = toPx(trackPoint(-distance), box, 0);
    var s1 = toPx(trackPoint(-distance), box, lanes);
    ctx.strokeStyle = cFaint; ctx.lineWidth = 2; ctx.globalAlpha = 0.7;
    ctx.beginPath(); ctx.moveTo(s0.x, s0.y); ctx.lineTo(s1.x, s1.y); ctx.stroke();
    ctx.globalAlpha = 1;

    /* 현재 순위 */
    var order = runners.map(function (r, i) {
      return { i: i, p: progressAt(r.splits, t) };
    }).sort(function (a, b) { return b.p - a.p; });
    var rankOf = {};
    order.forEach(function (o, idx) { rankOf[o.i] = idx + 1; });

    ctx.restore();

    /* 말 — 원은 숫자가 들어갈 만큼만 작게, 숫자는 원에 꽉 차게.
       지시선을 빼고도 읽히게 하려는 것이다. 선이 없으면 화면이 훨씬 조용하고,
       가려지는 말은 모자색으로 구분된다(아래 범례).

       뒤에서부터 그려 선두가 맨 위에 온다 — 겹칠 때 가장 중요한 말이 보인다. */
    var idxByRank = order.map(function (o) { return o.i; });
    leadIdx = idxByRank[0];
    for (var z = idxByRank.length - 1; z >= 0; z--) {
      var i = idxByRank[z], pt = pts[i], r = runners[i];
      var col = gateColor(r.gate), label = String(r.gate);
      // 겹쳐도 반지름 이상은 겹치지 않도록, 레인 간격(10px)에 맞춰 잡는다
      var rad = label.length > 1 ? 8.5 : 7.5;
      ctx.beginPath();
      ctx.arc(pt.x, pt.y, rad, 0, Math.PI * 2);
      ctx.fillStyle = col[0]; ctx.fill();
      ctx.strokeStyle = z === 0 ? '#b4842a' : 'rgba(0,0,0,.4)';
      ctx.lineWidth = z === 0 ? 2.2 : 1.2;
      ctx.stroke();
      ctx.fillStyle = col[1];
      ctx.font = 'bold ' + (label.length > 1 ? 11 : 13) + 'px system-ui, sans-serif';
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(label, pt.x, pt.y + 0.5);
    }
    ctx.textBaseline = 'alphabetic';

    /* 미니맵 — 카메라가 따라가면 전체에서 어디쯤인지 알 수 없다.
       구석에 주로 전체를 작게 그리고, 출발~현재 구간과 선두 위치를 표시한다. */
    drawMiniMap(W, H, cFaint);

    /* 표기는 배율 밖에 그린다 — 따라가기 중에 글자까지 커지면 읽기 어렵다. */
    ctx.fillStyle = cFaint;
    ctx.font = '11.5px system-ui, sans-serif';
    ctx.textAlign = 'left';
    ctx.fillText((data.track && data.track.meet ? data.track.meet + ' · ' : '')
      + Math.round(distance) + 'm · ' + (spec.corners || 0) + '코너', 10, H - 10);
    ctx.textAlign = 'right';
    ctx.fillStyle = '#b4842a';
    ctx.font = 'bold 11.5px system-ui, sans-serif';
    ctx.fillText(follow ? '따라가기' : '전체 보기', W - 10, H - 10);

    renderOrder(order);
  }

  /* 순위는 캔버스 밖 목록에 그린다. 안에 그리면 트랙과 겹치고, 카메라 배율을
     따라 글자가 커졌다 작아졌다 해서 읽기 어렵다. */
  /* 마번·모자색 범례. 겹쳐서 숫자가 가린 말은 색으로 찾는다. */
  var legendEl = document.getElementById('tk-legend');
  if (legendEl) {
    legendEl.innerHTML = runners.slice()
      .sort(function (a, b) { return (a.gate || 99) - (b.gate || 99); })
      .map(function (r) {
        var c = gateColor(r.gate);
        return '<li><span class="tk-cap" style="background:' + c[0]
          + ';color:' + c[1] + '">' + r.gate + '</span>' + r.name + '</li>';
      }).join('');
  }

  var orderEl = document.getElementById('tk-order');
  var lastOrder = '';
  function renderOrder(order) {
    if (!orderEl) return;
    var key = order.slice(0, 5).map(function (o) { return o.i; }).join(',');
    if (key === lastOrder) return;
    lastOrder = key;
    /* 5등까지만. 전부 늘어놓으면 목록이 길어져 오히려 순위가 안 읽힌다. */
    orderEl.innerHTML = order.slice(0, 5).map(function (o, idx) {
      var r = runners[o.i], c = gateColor(r.gate);
      return '<li class="' + (idx === 0 ? 'is-lead' : '') + '">'
        + '<span class="tk-pos">' + (idx + 1) + '위</span>'
        + '<span class="tk-dot" style="background:' + c[0] + '"></span>'
        + r.gate + ' ' + r.name + '</li>';
    }).join('');
  }

  /* 미니맵. 오른쪽 위 구석에 주로 전체를 작게 그린다.
     이미 지나온 구간은 진하게, 남은 구간은 옅게 — 한눈에 진행도가 읽힌다. */
  function drawMiniMap(W, H, cFaint) {
    var mw = Math.min(150, W * 0.2), mh = mw * 0.52;
    var mx = W - mw - 12, my = 12;
    var mbox = {
      left: mx + mh * 0.28, innerW: mw - mh * 0.56,
      innerH: mh * 0.44, cy: my + mh / 2, laneStep: 0
    };

    function mini(sv) { return toPx(trackPoint(sv), mbox, 0); }

    ctx.save();
    ctx.globalAlpha = 0.9;
    ctx.fillStyle = 'rgba(255,255,255,.72)';
    ctx.strokeStyle = 'rgba(0,0,0,.10)'; ctx.lineWidth = 1;
    roundRect(mx - 8, my - 8, mw + 16, mh + 16, 8);
    ctx.fill(); ctx.stroke();

    /* 주로 전체 */
    ctx.strokeStyle = 'rgba(0,0,0,.16)'; ctx.lineWidth = 3.5;
    ctx.beginPath();
    for (var k = 0; k <= 120; k++) {
      var pt = mini(-k / 120 * LAP);
      if (k === 0) ctx.moveTo(pt.x, pt.y); else ctx.lineTo(pt.x, pt.y);
    }
    ctx.closePath(); ctx.stroke();

    /* 이번 경주 구간 — 출발부터 결승까지 */
    ctx.strokeStyle = 'rgba(0,0,0,.30)'; ctx.lineWidth = 3.5;
    ctx.beginPath();
    for (var k2 = 0; k2 <= 80; k2++) {
      var p2 = mini(-distance + k2 / 80 * distance);
      if (k2 === 0) ctx.moveTo(p2.x, p2.y); else ctx.lineTo(p2.x, p2.y);
    }
    ctx.stroke();

    /* 지나온 구간 */
    var lead = progressAt(runners[leadIdx].splits, t);
    ctx.strokeStyle = '#b4842a'; ctx.lineWidth = 3.5;
    ctx.beginPath();
    for (var k3 = 0; k3 <= 80; k3++) {
      var p3 = mini(-distance + (k3 / 80) * distance * lead);
      if (k3 === 0) ctx.moveTo(p3.x, p3.y); else ctx.lineTo(p3.x, p3.y);
    }
    ctx.stroke();

    /* 선두 위치 */
    var lp = mini(-distance + distance * lead);
    ctx.beginPath(); ctx.arc(lp.x, lp.y, 3.2, 0, Math.PI * 2);
    ctx.fillStyle = '#b4842a'; ctx.fill();

    /* 결승선 */
    var fp = mini(0);
    ctx.strokeStyle = '#b4842a'; ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(fp.x, fp.y - 5); ctx.lineTo(fp.x, fp.y + 5); ctx.stroke();
    ctx.restore();
  }

  function roundRect(x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
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
  var rateBtns = Array.prototype.slice.call(document.querySelectorAll('.tk-rate'));
  var btnZoom = document.getElementById('tk-zoom');
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
  /* 배속은 브라우저에 저장한다. 경주마다 다시 고르게 하면 성가시다. */
  var RATE_KEY = 'horseai.playRate';
  function setRate(v, save) {
    speed = parseFloat(v) || 1;
    rateBtns.forEach(function (b) {
      b.classList.toggle('is-on', parseFloat(b.dataset.rate) === speed);
    });
    if (save) { try { localStorage.setItem(RATE_KEY, String(speed)); } catch (e) {} }
  }
  rateBtns.forEach(function (b) {
    b.addEventListener('click', function () { setRate(b.dataset.rate, true); });
  });
  var saved = null;
  try { saved = localStorage.getItem(RATE_KEY); } catch (e) {}
  setRate(saved || 1, false);
  if (btnZoom) btnZoom.addEventListener('click', function () {
    follow = !follow;
    btnZoom.setAttribute('aria-pressed', String(follow));
    btnZoom.textContent = follow ? '🔍 따라가기' : '⤢ 전체 보기';
  });
  if (seek) seek.addEventListener('input', function () {
    stop(); t = (parseFloat(seek.value) / 1000) * duration; sync();
  });
  window.addEventListener('resize', draw);

  sync();
  raf = requestAnimationFrame(tick);
})();
