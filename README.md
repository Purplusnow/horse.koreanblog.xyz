# 경마천재 — 한국 경마 예상 사이트

최종 업데이트: 2026-08-08

<https://horse.koreanblog.xyz>

한국마사회 공공데이터를 학습한 모델이 경주별 승률을 산출하고, 몬테카를로
시뮬레이션으로 경주 전개를 재현해 **미리보기 애니메이션**과 예측 신뢰도를 만들며,
정적 사이트로 자동 배포되는 파이프라인입니다.
수집 → 예측 → 코멘트 → 검증 → 빌드 → 배포 전 과정이 GitHub Actions 에서 무인으로 돕니다.

```
공공데이터포털 API ──► SQLite ──► 피처 ──► 승률 모델 ──┬──► 예측 (동결)
   출전표 / 경주성적                                    │
                                                        ├──► Claude 코멘트
                                                        │
              실제 착순 ──► 적중률 검증 ◄────────────────┘
                                    │
                                    └──► 정적 사이트 → GitHub Pages
```

## 설계 원칙

이 프로젝트의 모든 구조적 결정은 세 문장으로 요약됩니다.

1. **경주 전에 알 수 있는 것만 예측에 쓴다.** 착순·경주기록·당일 마체중은 피처에서
   완전히 배제하고, 자동 회귀 테스트로 매번 검증합니다 ([tests/test_no_leakage.py](tests/test_no_leakage.py)).
2. **배당률은 예측에 넣지 않는다.** 배당률을 넣으면 적중률은 쉽게 오르지만 모델이
   인기 순위를 베끼게 되고, 적중률이 '시장 베끼기'를 재는 수치가 됩니다. 1인기(최저
   단승배당)는 **내부 기준선**으로만 두고 매 학습마다 격차를 측정합니다(화면 비공개).
3. **공개한 예측은 수정하지 않는다.** 경주일이 지난 예측은 어떤 경우에도 다시 쓰지
   않습니다. 사후 수정이 가능하면 적중률 전체가 무의미해집니다.

## 시작하기

### 1. API 키 발급 (필수)

[공공데이터포털](https://www.data.go.kr/)에 가입 후 아래 두 API에 활용신청합니다.
자동승인이라 보통 즉시 사용 가능합니다.

| API | 데이터셋 번호 | 용도 |
|---|---|---|
| 한국마사회 출전표 상세정보 | [15058677](https://www.data.go.kr/data/15058677/openapi.do) | 경주 전 출주마·기수·부담중량·레이팅 |
| 한국마사회_경주성적정보 | [15063979](https://www.data.go.kr/data/15063979/openapi.do) | 착순·기록·배당률 (학습 레이블 + 적중률 검증) |

```bash
cp .env.example .env
# .env 에 KRA_SERVICE_KEY 를 붙여넣습니다 (인코딩/디코딩 어느 쪽이든 무방)
```

### 2. 설치

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3. 엔드포인트 확정

포털이 API 명세를 첨부문서 안에만 공개해서, 실제 경로를 코드에 하드코딩하면
언젠가 조용히 깨집니다. 실제 키로 후보 경로를 때려보고 살아 있는 것을 캐시합니다.

```bash
PYTHONPATH=src .venv/bin/python -m horseai.kra.probe
```

응답 필드 덤프가 `config/api_fields.json` 에 저장됩니다. 이어서 아래로 필드 매핑이
실제 응답과 맞는지 확인하세요. **매핑 실패(0%) 컬럼이 있으면**
[src/horseai/kra/normalize.py](src/horseai/kra/normalize.py) 의 별칭 목록에 한 줄 추가하면 됩니다.

```bash
PYTHONPATH=src .venv/bin/python -m horseai.kra.collect audit --kind entries
PYTHONPATH=src .venv/bin/python -m horseai.kra.collect audit --kind results
```

### 4. 데이터 수집과 학습

```bash
# 과거 5년치 경주 성적 (수천 회 호출이라 수십 분 걸립니다)
PYTHONPATH=src .venv/bin/python -m horseai.kra.collect backfill --years 5

# 학습 + 워크포워드 검증 + 시장 대비 성능 비교
PYTHONPATH=src .venv/bin/python -m horseai.model train
```

### 5. 예측과 사이트 생성

```bash
PYTHONPATH=src .venv/bin/python -m horseai.kra.collect entries --ahead 10
PYTHONPATH=src .venv/bin/python -m horseai.predict
PYTHONPATH=src .venv/bin/python -m horseai.comment     # ANTHROPIC_API_KEY 필요 (없으면 생략)
PYTHONPATH=src .venv/bin/python -m horseai.verify
PYTHONPATH=src .venv/bin/python -m horseai.site --out dist

cd dist && python3 -m http.server 8000   # http://localhost:8000
```

### API 키 없이 파이프라인 확인하기

합성 데이터로 전 과정을 돌려볼 수 있습니다. 실제 키 발급 전에 구조를 확인하거나
CI 스모크 테스트로 씁니다.

```bash
.venv/bin/python tools/make_synth_db.py --db data/synth.sqlite --years 4
PYTHONPATH=src .venv/bin/python -m horseai.model train --db data/synth.sqlite --folds 3 --min-train-races 1000
.venv/bin/python tests/test_no_leakage.py
.venv/bin/python tools/seed_demo_predictions.py --db data/synth.sqlite
PYTHONPATH=src .venv/bin/python -m horseai.verify --db data/synth.sqlite
PYTHONPATH=src .venv/bin/python -m horseai.site --db data/synth.sqlite --out dist
```

> 합성 데이터의 적중률 수치는 **파이프라인이 동작한다는 증거일 뿐 실력의 증거가
> 아닙니다.** 합성 시장은 실제 경마 배당률보다 훨씬 비효율적으로 만들어져 있어
> 이기기 쉽습니다. 실제 성능은 진짜 데이터로 학습한 뒤에야 알 수 있습니다.

## 배포

1. GitHub 저장소를 만들고 푸시합니다.
2. **Settings → Secrets and variables → Actions** 에 등록:
   - `KRA_SERVICE_KEY` (필수)
   - `ANTHROPIC_API_KEY` (선택 — 없으면 코멘트 없이 수치만 발행)
3. **Settings → Pages → Source** 를 `GitHub Actions` 로 설정합니다.
4. [config.yaml](config.yaml) 의 `site.url` 을 실제 도메인으로 바꿉니다.
   (canonical·sitemap·OG 태그에 쓰이므로 안 바꾸면 SEO가 망가집니다. 빌드 시 경고합니다.)
5. Actions 탭에서 **예상 갱신 및 배포** 를 수동 실행합니다. 첫 실행은 백필 때문에 오래 걸립니다.

이후 6시간마다 자동으로 돌고, 매주 월요일에 모델을 재학습합니다.

### 상태 보관 방식

GitHub Actions 는 무상태인데 이 프로젝트는 DB가 곧 자산입니다(예측 동결 기록과
적중률 이력). 저장소에 커밋하면 바이너리 diff 로 금세 비대해지므로, **`db-state`
릴리스의 자산으로 압축 보관**하고 매 실행마다 복원·갱신합니다. 원본 JSON은
180일이 지나면 자동으로 비워 크기를 유지합니다.

## 프로젝트 구조

| 경로 | 역할 |
|---|---|
| [src/horseai/kra/client.py](src/horseai/kra/client.py) | 포털 API 클라이언트 (서비스키 정규화, 재시도, XML 오류 처리, 페이징) |
| [src/horseai/kra/endpoints.py](src/horseai/kra/endpoints.py) | 엔드포인트 레지스트리 + 확정 경로 캐시 |
| [src/horseai/kra/probe.py](src/horseai/kra/probe.py) | 실제 키로 살아 있는 경로 탐침 |
| [src/horseai/kra/normalize.py](src/horseai/kra/normalize.py) | 별칭 기반 필드 정규화 (필드명이 바뀌어도 한 줄로 대응) |
| [src/horseai/kra/store.py](src/horseai/kra/store.py) | SQLite 스키마 · upsert · 원본 JSON 보존 |
| [src/horseai/kra/collect.py](src/horseai/kra/collect.py) | 수집 CLI (backfill / results / entries / audit / prune) |
| [src/horseai/features.py](src/horseai/features.py) | 피처 엔지니어링 (shift 기반, 누수 차단) |
| [src/horseai/model.py](src/horseai/model.py) | 학습 · 워크포워드 검증 · 시장 베이스라인 비교 |
| [src/horseai/predict.py](src/horseai/predict.py) | 출전표 → 예측 (동결 규칙) |
| [src/horseai/comment.py](src/horseai/comment.py) | Claude 예상 코멘트 (환각 차단 프롬프트 + 구조화 출력) |
| [src/horseai/verify.py](src/horseai/verify.py) | 공개 예측 vs 실제 착순 적중률 집계 |
| [src/horseai/site.py](src/horseai/site.py) | 정적 사이트 생성 (SEO · sitemap · JSON-LD) |

## 수익화

무료 공개 + 애드센스 구조입니다. [config.yaml](config.yaml) 의 `adsense.client` 와
슬롯 ID를 채우면 광고 영역이 활성화되고, 비어 있으면 아예 렌더링되지 않습니다.

애드센스 승인에는 콘텐츠 축적이 필요하므로, **경주 상세 페이지가 수백 개 쌓이고
적중률 데이터가 몇 주 누적된 뒤** 신청하는 편이 통과율이 높습니다.

## 법적 유의사항

- 한국마사회법은 **유료** 경마정보 제공업을 규제 대상으로 봅니다. 이 프로젝트는
  전량 무료 공개 + 광고 수익 구조로 설계되어 있습니다. 유료 구독으로 전환하려면
  법률 검토를 먼저 받으세요.
- 모든 페이지에 참고 정보 고지, 만 19세 이용 안내, 도박 문제 상담(1336) 링크가
  자동으로 들어갑니다. 애드센스 정책상으로도 필요한 요소입니다.
- 데이터 출처는 공공데이터포털 공개 API이며, 사이트에 출처를 명시합니다.

## 현재 상태

**검증된 것** — 합성 데이터(11,340경주 / 119,023두)로 전 구간 실행 확인:
피처 생성, 누수 회귀 테스트 3종 통과, 워크포워드 검증, 예측 생성, 적중률 집계,
316페이지 사이트 빌드 및 렌더링.

**실제 키가 있어야 확인 가능한 것** — API 엔드포인트 경로 확정(`probe`),
응답 필드명과 별칭 매핑의 일치 여부(`audit`), 그리고 무엇보다 **실제 경마
데이터에서의 진짜 성능**. 실제 마권 시장은 합성 시장보다 훨씬 효율적이라,
1인기 대비 우위를 내는 것은 여기서부터가 진짜 승부입니다.
