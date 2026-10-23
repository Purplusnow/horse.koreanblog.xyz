/* 워커 동작 검사 — 배포 전에 실제로 호출해 본다.
 *
 *     npm --prefix push-worker test
 *
 * **글자 검사로는 이 종류의 버그가 안 잡힌다.** "출처를 검사한다"는 코드가
 * 있는 것과, 낯선 출처로 들어온 요청이 실제로 막히는 것은 다른 얘기다.
 * 그래서 가짜 KV 를 끼워 넣고 진짜 Request 로 전 경로를 돌린다 —
 * 막혀야 할 것이 막히는지, 통과해야 할 것이 통과하는지 둘 다 본다.
 */
import worker from './src/index.js';

const kv = new Map();
const env = {
  ADMIN_TOKEN: 'secret-token',
  SUBS: {
    async put(k, v) { kv.set(k, v); },
    async get(k, type) { const v = kv.get(k); return type === 'json' && v ? JSON.parse(v) : (v ?? null); },
    async delete(k) { kv.delete(k); },
    async list({ prefix }) {
      return { keys: [...kv.keys()].filter((k) => k.startsWith(prefix)).map((name) => ({ name })),
               list_complete: true };
    },
  },
};

const ORIGIN = 'https://horse.koreanblog.xyz';
const GOOD = { endpoint: 'https://fcm.googleapis.com/fcm/send/abc123',
               keys: { p256dh: 'p'.repeat(87), auth: 'a'.repeat(22) } };

const call = (method, path, { origin, token, body } = {}) =>
  worker.fetch(new Request('https://w.dev' + path, {
    method,
    headers: {
      ...(origin ? { Origin: origin } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(body ? { 'Content-Type': 'application/json' } : {}),
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
  }), env);

let fail = 0;
async function check(label, got, want) {
  const ok = got === want;
  if (!ok) fail++;
  console.log(`  ${ok ? 'OK  ' : 'FAIL'}  ${label}  (${got}${ok ? '' : ' ≠ ' + want})`);
}

console.log('출처 검사');
await check('허용 출처 OPTIONS → 204', (await call('OPTIONS', '/subscribe', { origin: ORIGIN })).status, 204);
await check('낯선 출처 OPTIONS → 403', (await call('OPTIONS', '/subscribe', { origin: 'https://evil.example' })).status, 403);
await check('출처 없는 구독 → 403', (await call('POST', '/subscribe', { body: GOOD })).status, 403);

console.log('\n구독 값 검사');
await check('정상 구독 → 204', (await call('POST', '/subscribe', { origin: ORIGIN, body: GOOD })).status, 204);
await check('남의 호스트 → 400', (await call('POST', '/subscribe', { origin: ORIGIN,
  body: { ...GOOD, endpoint: 'https://evil.example/push/1' } })).status, 400);
await check('http → 400', (await call('POST', '/subscribe', { origin: ORIGIN,
  body: { ...GOOD, endpoint: 'http://fcm.googleapis.com/x' } })).status, 400);
await check('키 없음 → 400', (await call('POST', '/subscribe', { origin: ORIGIN,
  body: { endpoint: GOOD.endpoint } })).status, 400);
await check('거대한 본문 → 400', (await call('POST', '/subscribe', { origin: ORIGIN,
  body: { ...GOOD, junk: 'x'.repeat(5000) } })).status, 400);

console.log('\n같은 구독 두 번 → 한 줄만 남는가');
await call('POST', '/subscribe', { origin: ORIGIN, body: GOOD });
let list = await (await call('GET', '/subscriptions', { token: 'secret-token' })).json();
await check('명단 수', list.count, 1);

console.log('\n토큰 검사');
await check('토큰 없이 명단 → 401', (await call('GET', '/subscriptions')).status, 401);
await check('틀린 토큰 → 401', (await call('GET', '/subscriptions', { token: 'nope' })).status, 401);

console.log('\n상태 기록');
await check('PUT state → 204', (await call('PUT', '/state/last_sent', { token: 'secret-token', body: { value: '2026-10-24' } })).status, 204);
const st = await (await call('GET', '/state/last_sent', { token: 'secret-token' })).json();
await check('GET state 값', st.value, '2026-10-24');

console.log('\n해지·솎아내기');
await check('unsubscribe → 204', (await call('POST', '/unsubscribe', { origin: ORIGIN, body: { endpoint: GOOD.endpoint } })).status, 204);
list = await (await call('GET', '/subscriptions', { token: 'secret-token' })).json();
await check('명단 비었는가', list.count, 0);

console.log(`\n${fail ? fail + '건 실패' : '전부 통과'}`);
process.exit(fail ? 1 : 0);
