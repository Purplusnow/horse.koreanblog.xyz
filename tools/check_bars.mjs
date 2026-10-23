/* 설치 바·알림 바가 실제로 뜨는지 잰다.
 *
 *     node tools/check_bars.mjs [url]
 *
 * **"코드가 있다"와 "화면에 뜬다"는 다른 얘기다.** 실제로 설치 바가 떠 있으면
 * 알림 바가 양보하도록 만들어 놓고 그 다음을 안 만든 적이 있다 — 앱을 안 깐
 * 사람에게는 알림 신청이 영영 안 나왔고, 코드·문법·빌드 검사는 전부 통과했다.
 * 그래서 진짜 브라우저로 띄우고 **요소가 생기는지**를 본다.
 *
 * 두 경우를 다 본다.
 *   1) 설치 바가 떠 있는 동안 → 알림 바는 기다려야 한다
 *   2) 설치 바를 닫은 뒤      → 알림 바가 올라와야 한다
 */
import { spawn } from 'node:child_process';

const URL_ = process.argv[2] || 'https://horse.koreanblog.xyz/';
const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const PORT = 9334;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-first-run',
  `--remote-debugging-port=${PORT}`, '--user-data-dir=/tmp/checkbars_profile',
  '--window-size=390,800', 'about:blank'], { stdio: 'ignore' });

let fail = 0;
const ok = (label, cond, detail = '') => {
  if (!cond) fail++;
  console.log(`  ${cond ? 'OK  ' : 'FAIL'}  ${label}${detail ? '  — ' + detail : ''}`);
};

try {
  await sleep(3000);
  const targets = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const ws = new WebSocket(targets.find((t) => t.type === 'page').webSocketDebuggerUrl);
  await new Promise((r) => (ws.onopen = r));

  let id = 0;
  const pending = new Map();
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  };
  const send = (method, params = {}) => new Promise((res) => {
    const n = ++id; pending.set(n, res);
    ws.send(JSON.stringify({ id: n, method, params }));
  });
  const js = async (expr) => (await send('Runtime.evaluate',
    { expression: expr, awaitPromise: true, returnByValue: true })).result?.result?.value;

  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: URL_ });
  await sleep(6000);

  console.log(`대상: ${URL_}\n`);
  console.log('준비');
  ok('notify.js 가 실려 있다', await js('!!document.querySelector(\'script[src*="notify.js"]\')'));
  ok('알림 권한이 아직 기본값', (await js('Notification.permission')) === 'default',
     await js('Notification.permission'));

  console.log('\n설치 바가 떠 있는 동안');
  const hasPwa = await js('!!document.getElementById("pwa-bar")');
  ok('설치 바가 떴다', hasPwa);
  ok('알림 바는 아직 양보 중', !(await js('!!document.getElementById("nt-bar")')));

  if (hasPwa) {
    console.log('\n설치 바를 닫은 뒤');
    await js('document.querySelector("#pwa-bar .pwa-close").click()');
    ok('설치 바가 사라졌다', !(await js('!!document.getElementById("pwa-bar")')));
    // 양보 루프가 3초마다 다시 본다 — 넉넉히 기다린다.
    for (let i = 0; i < 6 && !(await js('!!document.getElementById("nt-bar")')); i++) await sleep(2000);
    const shown = await js('!!document.getElementById("nt-bar")');
    ok('알림 바가 올라왔다', shown);
    if (shown) {
      ok('"알림 받기" 버튼이 있다', await js('!!document.querySelector("#nt-bar .nt-go")'));
      ok('버튼이 화면 안에 있다', await js(`(() => {
        const b = document.querySelector("#nt-bar .nt-go").getBoundingClientRect();
        return b.width > 0 && b.right <= innerWidth + 1 && b.left >= -1;
      })()`));
    }
  }
  ws.close();
} finally {
  chrome.kill();
}

console.log(`\n${fail ? fail + '건 실패' : '전부 통과'}`);
process.exit(fail ? 1 : 0);
