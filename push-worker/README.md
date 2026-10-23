# 경주일 알림 — 구독 창구 (Cloudflare Worker)

최종 업데이트: 2026-10-23

사이트는 GitHub Pages 라 POST 를 받을 수 없다. 브라우저가 건네는 **구독 정보를
적어 두는 창구**만 여기서 맡고, 실제 발송은 이미 여섯 시간마다 도는
`update.yml` 안의 `tools/push/send.js` 가 한다.

```
브라우저 ──구독──▶ 이 워커(KV) ◀──명단 조회── GitHub Actions ──알림──▶ 브라우저
```

## 처음 한 번만 하는 일

```bash
cd push-worker

npx wrangler login                        # 1) 브라우저가 열린다

npx wrangler kv namespace create SUBS     # 2) 출력된 id 를 wrangler.toml 에 붙인다
#   [[kv_namespaces]]
#   binding = "SUBS"
#   id = "여기"

npx wrangler secret put ADMIN_TOKEN       # 3) 발송기와 나눠 가질 토큰
#   값은 아래 "토큰" 항목 참고

npx wrangler deploy                       # 4) https://horseai-push.<계정>.workers.dev
```

배포하면 나오는 주소를 두 곳에 적는다.

| 어디 | 무엇 |
|---|---|
| `config.yaml` 의 `push.worker_url` | 사이트가 구독을 보낼 주소 |
| 저장소 Variables 의 `PUSH_WORKER_URL` | 발송기가 명단을 받을 주소 |

## 저장소에 들어가야 하는 값

**Variables** (공개돼도 되는 값)

- `PUSH_WORKER_URL` — 위 워커 주소
- `VAPID_PUBLIC_KEY` — `config.yaml` 의 `push.vapid_public` 과 **같은 값**

**Secrets** (절대 공개되면 안 되는 값)

- `VAPID_PRIVATE_KEY` — 공개키의 짝
- `PUSH_ADMIN_TOKEN` — 워커의 `ADMIN_TOKEN` 과 **같은 값**

> 공개키와 개인키는 **짝이어야 한다.** 한쪽만 바꾸면 발송이 전부 실패하는데,
> 실패가 `continue-on-error` 에 삼켜져 워크플로는 초록색으로 끝난다.
> 바꿀 때는 `config.yaml`·Variables·Secrets 셋을 한 번에 바꾼다.

## 왜 이렇게 나눴나

- **발송을 워커에 두지 않았다.** 언제 보낼지는 그날 편성과 정산 상태를 봐야
  정해지는데, 그 판단에 필요한 `settle.json`·`races.json` 은 빌드 직후
  Actions 작업공간에 있다. 워커가 그걸 보려면 사이트를 다시 긁어야 한다.
- **구독 등록은 공개 경로일 수밖에 없다.** 브라우저가 직접 부르기 때문이다.
  그래서 워커가 출처(Origin)와 푸시 서비스 주소를 검사해 걸러낸다.
- **명단 조회·상태 기록은 토큰으로 막았다.** 명단이 새면 누가 구독했는지가
  드러나고, 남의 기기로 알림을 보낼 수 있다.

## 확인

```bash
# 명단 보기 (토큰 필요)
curl -H "Authorization: Bearer $TOKEN" https://<워커주소>/subscriptions

# 보낼 조건만 따져 보고 실제로는 안 보내기
PUSH_WORKER_URL=... PUSH_ADMIN_TOKEN=... VAPID_PUBLIC_KEY=... VAPID_PRIVATE_KEY=... \
  node tools/push/send.js --dist dist --dry-run
```
