/* 경주일 아침 알림 발송기 — 빌드 직후 GitHub Actions 가 돌린다.
 *
 *   node tools/push/send.js --dist dist [--dry-run]
 *
 * **크론은 하루에 여러 번 돈다. 알림은 경주일당 한 번만 나가야 한다.**
 * 그래서 보내기 전에 창구(KV)에 적힌 '마지막으로 보낸 날짜'를 보고, 오늘
 * 날짜가 이미 적혀 있으면 아무것도 하지 않는다. 적는 건 보낸 뒤다 — 먼저
 * 적고 보내다 실패하면 그날은 영영 안 나간다.
 *
 * 보낼 조건은 넷이고, 하나라도 어긋나면 조용히 끝낸다.
 *   1) 오늘 경주가 있다                      (settle.json total > 0)
 *   2) 아직 안 끝난 경주가 있다              (settled < total)
 *   3) 오늘 날짜로 아직 안 보냈다            (창구의 last 기록)
 *   4) 지금이 아침이다                       (KST 07~12시)
 *
 * 2)가 중요하다. 경주가 다 끝난 저녁에 "오늘 예상이 올라왔습니다"가 가면
 * 그건 거짓말이고, 한 번이면 사람들은 알림을 끈다.
 */
const fs = require('fs');
const path = require('path');
const webpush = require('web-push');

const API = (process.env.PUSH_WORKER_URL || '').replace(/\/+$/, '');
const TOKEN = process.env.PUSH_ADMIN_TOKEN || '';
const PUB = process.env.VAPID_PUBLIC_KEY || '';
const PRIV = process.env.VAPID_PRIVATE_KEY || '';
const CONTACT = process.env.VAPID_CONTACT || 'mailto:noreply@horse.koreanblog.xyz';
const SITE = process.env.SITE_URL || 'https://horse.koreanblog.xyz';
// 아침에만 보낸다. 검사할 때는 창을 넓혀야 나머지 조건을 돌려볼 수 있어서
// 환경변수로 뺀다 — 기본값이 실제 운영값이다.
const HOUR_FROM = Number(process.env.PUSH_HOUR_FROM ?? 7);
const HOUR_TO = Number(process.env.PUSH_HOUR_TO ?? 12);

const args = process.argv.slice(2);
const distDir = (() => {
  const i = args.indexOf('--dist');
  return i >= 0 ? args[i + 1] : 'dist';
})();
const dryRun = args.includes('--dry-run');

const log = (...a) => console.log('[push]', ...a);

function readJSON(p) {
  try { return JSON.parse(fs.readFileSync(p, 'utf8')); }
  catch (e) { return null; }
}

// KST 기준 오늘 날짜와 시각. 러너는 UTC 라 그대로 쓰면 하루가 밀린다.
function kstNow() {
  const s = new Date().toLocaleString('sv-SE', { timeZone: 'Asia/Seoul' });
  return { date: s.slice(0, 10), hour: Number(s.slice(11, 13)) };
}

async function api(method, path, body) {
  const res = await fetch(API + path, {
    method,
    headers: {
      'Authorization': `Bearer ${TOKEN}`,
      ...(body ? { 'Content-Type': 'application/json' } : {}),
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  if (!res.ok) throw new Error(`${method} ${path} → ${res.status}`);
  return res.status === 204 ? null : res.json();
}

function buildMessage(races, date) {
  // 오늘 경주를 경마장별로 센다. "제주 7 · 부산경남 9" 처럼 한 줄로 보인다.
  const today = races.filter((r) => r.d === date);
  const byMeet = new Map();
  for (const r of today) byMeet.set(r.m, (byMeet.get(r.m) || 0) + 1);
  const parts = [...byMeet.entries()].map(([m, n]) => `${m} ${n}경주`);
  return {
    title: '오늘의 예상이 올라왔습니다',
    body: parts.length ? `${parts.join(' · ')} · 추천 5두와 신뢰도를 확인하세요`
                       : '추천 5두와 신뢰도를 확인하세요',
    url: '/?src=push',
    tag: `raceday-${date}`,
  };
}

async function main() {
  for (const [k, v] of Object.entries({ PUSH_WORKER_URL: API, PUSH_ADMIN_TOKEN: TOKEN,
                                        VAPID_PUBLIC_KEY: PUB, VAPID_PRIVATE_KEY: PRIV })) {
    if (!v) { log(`${k} 가 비어 있습니다 — 보내지 않고 끝냅니다.`); return; }
  }

  const settle = readJSON(path.join(distDir, 'settle.json'));
  const races = readJSON(path.join(distDir, 'races.json')) || [];
  if (!settle) { log('settle.json 을 못 읽었습니다 — 끝냅니다.'); return; }

  const { date, hour } = kstNow();
  log(`오늘(KST) ${date} ${hour}시 · 편성 ${settle.total} · 정산 ${settle.settled}`);

  // 1) 오늘 경주가 있는가
  if (!settle.total) { log('오늘은 경주가 없습니다.'); return; }
  // 2) 아직 남은 경주가 있는가
  if (settle.settled >= settle.total) { log('오늘 경주가 모두 끝났습니다.'); return; }
  // 4) 아침인가 (3은 창구에 물어봐야 하므로 뒤로 민다)
  if (hour < HOUR_FROM || hour > HOUR_TO) {
    log(`알림을 보낼 시간대가 아닙니다 (${HOUR_FROM}~${HOUR_TO}시).`); return;
  }

  // 3) 오늘 날짜로 이미 보냈는가
  const last = await api('GET', '/state/last_sent');
  if (last && last.value === date) { log('오늘은 이미 보냈습니다.'); return; }

  const { count, subscriptions } = await api('GET', '/subscriptions');
  log(`구독 ${count}건`);
  if (!count) {
    // 받을 사람이 없어도 날짜는 적는다. 안 적으면 다음 크론에서 또 조회한다.
    if (!dryRun) await api('PUT', '/state/last_sent', { value: date });
    return;
  }

  const msg = buildMessage(races, date);
  log('문구:', msg.title, '/', msg.body);
  if (dryRun) { log('--dry-run 이라 실제로 보내지 않습니다.'); return; }

  webpush.setVapidDetails(CONTACT, PUB, PRIV);
  const payload = JSON.stringify({ ...msg, url: SITE.replace(/\/+$/, '') + msg.url });

  let ok = 0;
  const dead = [];
  for (const sub of subscriptions) {
    try {
      await webpush.sendNotification(sub, payload, { TTL: 6 * 3600 });
      ok++;
    } catch (e) {
      // 404·410 은 '그 구독은 이제 없다'는 뜻이다. 남겨 두면 매번 실패하며
      // 명단만 불어나므로 바로 솎아낸다. 그 밖의 오류는 일시적일 수 있어 둔다.
      if (e.statusCode === 404 || e.statusCode === 410) dead.push(sub.endpoint);
      else log('보내기 실패:', e.statusCode || e.message);
    }
  }
  log(`보냄 ${ok}건 · 만료 ${dead.length}건`);
  if (dead.length) await api('POST', '/prune', { endpoints: dead });
  await api('PUT', '/state/last_sent', { value: date });
  log('완료');
}

// 알림이 실패해도 배포는 성공이다. 여기서 0 이 아닌 값을 내면 워크플로가
// 빨갛게 되고, 정작 사이트는 멀쩡히 올라가 있는 채로 매번 사람을 부른다.
main().catch((e) => { log('오류:', e.message); process.exitCode = 0; });
