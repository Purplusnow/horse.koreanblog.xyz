/* 웹푸시 구독 창구 — Cloudflare Worker.
 *
 * 사이트는 GitHub Pages 라 POST 를 받을 수 없다. 알림을 보내려면 브라우저가
 * 건네주는 구독 정보를 어딘가 적어 둬야 하는데, 그 '어딘가'가 여기다.
 * 발송은 이 워커가 하지 않는다 — 이미 여섯 시간마다 도는 GitHub Actions 가
 * 여기서 명단을 받아 보낸다. 워커는 명단만 지킨다.
 *
 * 경로
 *   POST /subscribe      브라우저가 구독을 등록한다 (공개)
 *   POST /unsubscribe    구독을 지운다 (공개)
 *   GET  /subscriptions  명단 전체 — 발송기용 (토큰 필요)
 *   POST /prune          죽은 구독을 지운다 — 발송기용 (토큰 필요)
 *   GET  /state/:key     마지막으로 알림을 보낸 날짜 등 (토큰 필요)
 *   PUT  /state/:key     "   (토큰 필요)
 *
 * 구독 등록 경로는 공개일 수밖에 없다 — 브라우저가 직접 부르기 때문이다.
 * 그래서 아무 값이나 받지 않는다 — 진짜 푸시 서비스의 주소인지, 키가 제대로
 * 붙어 있는지, 크기가 터무니없지 않은지를 보고 걸러낸다. 안 그러면 이 창구가
 * 남의 쓰레기통이 된다.
 */

const ALLOWED_ORIGINS = [
  "https://horse.koreanblog.xyz",
];

// 브라우저 제조사가 운영하는 푸시 서비스만 받는다.
const ALLOWED_PUSH_HOSTS = [
  /\.googleapis\.com$/,              // Chrome / Edge (FCM)
  /\.push\.services\.mozilla\.com$/, // Firefox
  /\.notify\.windows\.com$/,         // Windows
  /\.push\.apple\.com$/,             // Safari
];

const MAX_BODY = 4096;      // 구독 하나는 1KB 를 넘지 않는다
const SUB_PREFIX = "sub:";
const STATE_PREFIX = "state:";

const json = (data, status = 200, extra = {}) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...extra },
  });

function corsHeaders(request) {
  const origin = request.headers.get("Origin") || "";
  if (!ALLOWED_ORIGINS.includes(origin)) return null;
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
    "Vary": "Origin",
  };
}

async function keyFor(endpoint) {
  // 주소를 그대로 키로 쓰면 KV 키 길이 제한에 걸리고 로그에도 다 남는다.
  const digest = await crypto.subtle.digest(
    "SHA-256", new TextEncoder().encode(endpoint));
  return SUB_PREFIX + [...new Uint8Array(digest)]
    .map((b) => b.toString(16).padStart(2, "0")).join("");
}

function validSubscription(sub) {
  if (!sub || typeof sub.endpoint !== "string") return "endpoint 없음";
  let url;
  try { url = new URL(sub.endpoint); } catch { return "endpoint 가 주소가 아님"; }
  if (url.protocol !== "https:") return "https 아님";
  if (!ALLOWED_PUSH_HOSTS.some((re) => re.test(url.hostname)))
    return "알 수 없는 푸시 서비스";
  const k = sub.keys || {};
  if (typeof k.p256dh !== "string" || typeof k.auth !== "string")
    return "암호화 키 없음";
  return null;
}

function authed(request, env) {
  const got = request.headers.get("Authorization") || "";
  return env.ADMIN_TOKEN && got === `Bearer ${env.ADMIN_TOKEN}`;
}

async function readBody(request) {
  const text = await request.text();
  if (text.length > MAX_BODY) throw new Error("본문이 너무 큽니다");
  return JSON.parse(text);
}

async function listAll(env) {
  const out = [];
  let cursor;
  do {
    const page = await env.SUBS.list({ prefix: SUB_PREFIX, cursor });
    for (const k of page.keys) {
      const v = await env.SUBS.get(k.name, "json");
      if (v) out.push(v);
    }
    cursor = page.list_complete ? undefined : page.cursor;
  } while (cursor);
  return out;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";
    const cors = corsHeaders(request);

    if (request.method === "OPTIONS") {
      return cors ? new Response(null, { status: 204, headers: cors })
                  : new Response(null, { status: 403 });
    }

    // ── 브라우저가 부르는 두 경로 ──────────────────────────────
    if (path === "/subscribe" || path === "/unsubscribe") {
      if (!cors) return json({ error: "허용되지 않은 출처" }, 403);
      if (request.method !== "POST")
        return json({ error: "POST 만 받습니다" }, 405, cors);

      let body;
      try { body = await readBody(request); }
      catch (e) { return json({ error: String(e.message || e) }, 400, cors); }

      if (path === "/unsubscribe") {
        if (typeof body.endpoint !== "string")
          return json({ error: "endpoint 없음" }, 400, cors);
        await env.SUBS.delete(await keyFor(body.endpoint));
        return new Response(null, { status: 204, headers: cors });
      }

      const bad = validSubscription(body);
      if (bad) return json({ error: bad }, 400, cors);

      await env.SUBS.put(await keyFor(body.endpoint), JSON.stringify({
        endpoint: body.endpoint,
        keys: { p256dh: body.keys.p256dh, auth: body.keys.auth },
        // 언제 받은 구독인지는 남겨 둔다 — 오래된 명단을 솎을 때 쓴다.
        at: new Date().toISOString(),
      }));
      return new Response(null, { status: 204, headers: cors });
    }

    // ── 발송기만 부르는 경로 ──────────────────────────────────
    if (!authed(request, env)) return json({ error: "인증 필요" }, 401);

    if (path === "/subscriptions" && request.method === "GET") {
      const subs = await listAll(env);
      return json({ count: subs.length, subscriptions: subs });
    }

    if (path === "/prune" && request.method === "POST") {
      const body = await readBody(request).catch(() => null);
      const list = (body && body.endpoints) || [];
      for (const e of list) await env.SUBS.delete(await keyFor(e));
      return json({ pruned: list.length });
    }

    if (path.startsWith("/state/")) {
      const key = STATE_PREFIX + path.slice("/state/".length);
      if (request.method === "GET")
        return json({ value: await env.SUBS.get(key) });
      if (request.method === "PUT") {
        const body = await readBody(request).catch(() => ({}));
        await env.SUBS.put(key, String(body.value ?? ""));
        return new Response(null, { status: 204 });
      }
    }

    return json({ error: "없는 경로" }, 404);
  },
};
