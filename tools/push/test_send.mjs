/* 발송 판단 검사 — 가짜 창구와 가짜 산출물로 send.js 를 실제로 돌린다.
 *
 *     node tools/push/test_send.mjs
 *
 * **여기서 틀리면 조용히 틀린다.** 크론은 하루에 열 번 넘게 돌고, 조건을
 * 하나 잘못 보면 같은 알림이 열 번 가거나 한 번도 안 간다. 둘 다 사용자는
 * 알림을 끄는 것으로 답한다. 그래서 글자가 아니라 **판단 결과**를 본다.
 */
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

const TOKEN = 'test-token';
let state = {};          // 가짜 KV
let subs = [];
let sent = [];           // /prune·PUT 호출 기록

const server = createServer((req, res) => {
  const url = new URL(req.url, 'http://x');
  let body = '';
  req.on('data', (c) => (body += c));
  req.on('end', () => {
    if (req.headers.authorization !== `Bearer ${TOKEN}`) {
      res.writeHead(401).end('{}'); return;
    }
    if (url.pathname === '/subscriptions') {
      res.writeHead(200, { 'Content-Type': 'application/json' })
         .end(JSON.stringify({ count: subs.length, subscriptions: subs }));
    } else if (url.pathname.startsWith('/state/')) {
      const k = url.pathname.slice(7);
      if (req.method === 'PUT') {
        state[k] = JSON.parse(body || '{}').value; sent.push(`PUT ${k}=${state[k]}`);
        res.writeHead(204).end();
      } else {
        res.writeHead(200, { 'Content-Type': 'application/json' })
           .end(JSON.stringify({ value: state[k] ?? null }));
      }
    } else { res.writeHead(404).end('{}'); }
  });
});
await new Promise((r) => server.listen(0, r));
const API = `http://127.0.0.1:${server.address().port}`;

const dist = mkdtempSync(path.join(tmpdir(), 'senddist-'));
const today = new Date().toLocaleString('sv-SE', { timeZone: 'Asia/Seoul' }).slice(0, 10);

function writeDist({ total, settled }) {
  writeFileSync(path.join(dist, 'settle.json'), JSON.stringify({ date: today, total, settled }));
  writeFileSync(path.join(dist, 'races.json'), JSON.stringify([
    { k: 'a', d: today, m: '제주', n: 1 }, { k: 'b', d: today, m: '제주', n: 2 },
    { k: 'c', d: today, m: '부산경남', n: 1 },
  ]));
}

function run(env = {}, dry = true) {
  return new Promise((resolve) => {
    const argv = ['tools/push/send.js', '--dist', dist];
    if (dry) argv.push('--dry-run');
    const p = spawn(process.execPath, argv, {
      env: { ...process.env, PUSH_WORKER_URL: API, PUSH_ADMIN_TOKEN: TOKEN,
             VAPID_PUBLIC_KEY: 'pub', VAPID_PRIVATE_KEY: 'priv',
             PUSH_HOUR_FROM: '0', PUSH_HOUR_TO: '23', ...env },
      cwd: path.resolve('.'),
    });
    let out = '';
    p.stdout.on('data', (d) => (out += d));
    p.stderr.on('data', (d) => (out += d));
    p.on('close', () => resolve(out));
  });
}

let fail = 0;
async function scenario(label, setup, expect) {
  setup();
  const out = await run(setup.env || {}, setup.dry !== false);
  const ok = out.includes(expect);
  if (!ok) fail++;
  console.log(`  ${ok ? 'OK  ' : 'FAIL'}  ${label}`);
  if (!ok) console.log(out.split('\n').map((l) => '        ' + l).join('\n'));
}

console.log('보낼 조건');
await scenario('오늘 경주가 없으면 안 보낸다',
  () => { state = {}; subs = []; writeDist({ total: 0, settled: 0 }); },
  '오늘은 경주가 없습니다');

await scenario('경주가 다 끝났으면 안 보낸다',
  () => { state = {}; writeDist({ total: 16, settled: 16 }); },
  '오늘 경주가 모두 끝났습니다');

await scenario('아침이 아니면 안 보낸다',
  Object.assign(() => { state = {}; writeDist({ total: 16, settled: 0 }); },
                { env: { PUSH_HOUR_FROM: '7', PUSH_HOUR_TO: '7' } }),
  '보낼 시간대가 아닙니다');

await scenario('오늘 이미 보냈으면 안 보낸다',
  () => { state = { last_sent: today }; writeDist({ total: 16, settled: 0 }); },
  '오늘은 이미 보냈습니다');

await scenario('조건이 맞으면 보낸다',
  () => { state = {}; subs = [{ endpoint: 'https://fcm.googleapis.com/x', keys: { p256dh: 'p', auth: 'a' } }];
          writeDist({ total: 16, settled: 3 }); },
  '오늘의 예상이 올라왔습니다');

console.log('\n문구');
await scenario('경마장별 경주 수가 들어간다',
  () => { state = {}; writeDist({ total: 16, settled: 3 }); },
  '제주 2경주 · 부산경남 1경주');

console.log('\n설정이 비었을 때');
await scenario('창구 주소가 없으면 조용히 끝낸다',
  Object.assign(() => { state = {}; }, { env: { PUSH_WORKER_URL: '' } }),
  'PUSH_WORKER_URL 가 비어 있습니다');

console.log('\n중복 방지 표식');
// 받을 사람이 0명이어도 날짜는 적어야 한다. 안 적으면 다음 크론이 또 조회하고,
// 그 사이 누가 구독하면 '오늘 치'가 한 번 더 나간다.
state = {}; subs = []; writeDist({ total: 16, settled: 3 });
await run({}, false);
if (state.last_sent === today) {
  console.log('  OK    구독 0건이어도 날짜를 적는다');
} else {
  fail++; console.log(`  FAIL  구독 0건이어도 날짜를 적는다 (적힌 값: ${state.last_sent})`);
}
// 적힌 뒤에는 다시 돌려도 안 나가야 한다.
const again = await run({}, false);
if (again.includes('오늘은 이미 보냈습니다')) {
  console.log('  OK    같은 날 두 번째 실행은 막힌다');
} else {
  fail++; console.log('  FAIL  같은 날 두 번째 실행은 막힌다'); console.log(again);
}

server.close();
console.log(`\n${fail ? fail + '건 실패' : '전부 통과'}`);
process.exit(fail ? 1 : 0);
