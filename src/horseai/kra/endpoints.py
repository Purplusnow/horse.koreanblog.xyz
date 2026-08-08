"""한국마사회 오픈API 엔드포인트 레지스트리.

data.go.kr 데이터셋 페이지에 임베드된 Swagger 명세에서 오퍼레이션명을 확정했다.
다만 포털 메타데이터의 ``host`` 필드가 데이터셋마다 API 번호 세그먼트를 포함하기도
하고 누락하기도 해서, 실제 호출 경로는 후보를 두고 ``probe`` 로 확정한다.
확정된 경로는 ``config/endpoints.resolved.json`` 에 캐시된다.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

RESOLVED_PATH = Path(os.environ.get("HORSEAI_ENDPOINTS", "config/endpoints.resolved.json"))

# 경마장 코드. 영천은 2024년 이후 시행분부터 조회 가능.
MEETS: Dict[int, str] = {1: "서울", 2: "제주", 3: "부산경남", 4: "영천"}
# 사이트에서 실제로 다루는 경마장 (영천은 경주 수가 적어 기본 제외)
ACTIVE_MEETS: List[int] = [1, 2, 3]


@dataclass
class Endpoint:
    """하나의 오픈API 오퍼레이션."""

    key: str                      # 내부 식별자
    title: str                    # 한글 명칭
    operation: str                # Swagger 에서 확정한 오퍼레이션명
    api_segments: List[str]       # 경로 후보 (앞에서부터 시도)
    date_params: List[str] = field(default_factory=list)  # 지원하는 날짜 파라미터
    dataset_pk: str = ""          # data.go.kr 데이터셋 번호 (문서 추적용)
    note: str = ""

    def candidate_paths(self) -> List[str]:
        return [f"{seg}/{self.operation}" if seg else self.operation for seg in self.api_segments]


# ---------------------------------------------------------------------------
# 레지스트리
# ---------------------------------------------------------------------------

ENTRY_SHEET = Endpoint(
    key="entry_sheet",
    title="출전표 상세정보",
    operation="entrySheet_2",
    api_segments=["API26_2", ""],
    date_params=["rc_date", "rc_month"],
    dataset_pk="15058677",
    note=(
        "경주 시행 전 출전마 정보. 마번·부담중량·레이팅·기수·조교사·마주와 "
        "통산/최근1년 성적(1·2·3위 횟수, 출주 횟수)까지 포함해 예측 피처의 근간이 된다. "
        "날짜 변수를 모두 생략하면 최근 한 달치가 표출된다."
    ),
)

RACE_RESULT = Endpoint(
    key="race_result",
    title="경주성적정보",
    operation="RaceDetailResult_1",
    api_segments=["API214_1", ""],
    date_params=["rc_date", "rc_month", "rc_year"],
    dataset_pk="15063979",
    note=(
        "경주 시행 후 성적. 착순·경주기록·마체중·구간기록·날씨·주로 상태와 "
        "단승/연승 배당률을 제공한다. 학습 레이블과 적중률 검증의 원천."
    ),
)

HORSE_INFO = Endpoint(
    key="horse_info",
    title="경주마 상세정보",
    operation="raceHorseInfo_2",
    api_segments=["API8_2", ""],
    dataset_pk="15058115",
    note="현역 경주마 마스터. 마번으로 등급·생년월일·조교사·마주를 보강한다.",
)

JOCKEY_RESULT = Endpoint(
    key="jockey_result",
    title="기수 성적 정보",
    operation="jockeyResult_1",
    api_segments=["API11_1", ""],
    dataset_pk="15056591",
    note="현역 기수의 누적 성적. 기수 승률 피처의 사전(prior) 값으로 쓴다.",
)

TRAINER_RESULT = Endpoint(
    key="trainer_result",
    title="조교사 성적 정보",
    operation="trainerResult_1",
    api_segments=["API12_1", "API19_1", ""],
    dataset_pk="15056593",
    note="현역 조교사의 누적 성적. 오퍼레이션명 미확정이라 프로브로 검증 필요.",
)

HIGHEST_DIVIDEND = Endpoint(
    key="highest_dividend",
    title="승식별 최고배당률 정보",
    operation="highestDividendRateInfo_1",
    api_segments=["API35_1", ""],
    dataset_pk="15059267",
    note="승식별 최고 배당률. 콘텐츠용 '고배당 경주' 코너에 쓴다.",
)

# --- 2026-08-07 추가 활용신청분 (실제 호출로 확인 완료) --------------------

HORSE_BASIC = Endpoint(
    key="horse_basic",
    title="마필기본연계",
    operation="HorseBasicInfo",
    api_segments=["API281"],
    dataset_pk="15108997",
    note="품종·성별·산지국·도입국·생년월일. 마필 페이지의 신상 정보.",
)

HORSE_DETAIL = Endpoint(
    key="horse_detail",
    title="마필상세연계",
    operation="HorseDetailInfo",
    api_segments=["API282"],
    dataset_pk="15108998",
    note="외모 특징(머리·목·몸통·다리)과 낙인. 예측 가치는 낮고 마필 페이지 콘텐츠용.",
)

HORSE_BLOOD = Endpoint(
    key="horse_blood",
    title="혈통정보기본",
    operation="HorseBloodBasicInfo",
    api_segments=["API284"],
    dataset_pk="15109000",
    note=(
        "부계·모계 계통과 혈통지수(도세이지 계열), 근친계수(dsaCoiRt). "
        "정량 지표라 피처가 될 수 있으나 수록 두수가 적어 커버리지 확인이 먼저다."
    ),
)

DIVIDEND_TOTAL = Endpoint(
    key="dividend_total",
    title="확정배당율종합",
    operation="Dividend_rate_total",
    api_segments=["API301"],
    date_params=["rc_date"],
    dataset_pk="15119558",
    note=(
        "승식별(pool) 확정배당. 단승뿐 아니라 복승·쌍승·삼복승까지 있어 "
        "'추천 조합'이 실제로 얼마를 돌려줬는지 검증할 수 있다."
    ),
)

RESULT_TOTAL = Endpoint(
    key="result_total",
    title="경주결과종합",
    operation="Race_Result_total",
    api_segments=["API299"],
    date_params=["rc_date"],
    dataset_pk="15119524",
    note="경주성적정보(API214_1)와 거의 동일한 94개 필드. 상호 보완·검증용.",
)

RC_RACE_INFO = Endpoint(
    key="rc_race_info",
    title="RC경마경주정보",
    operation="SeoulRace_1",
    api_segments=["API186_1"],
    dataset_pk="15063950",
    note="경주별 착순·배당 요약 55개 필드.",
)

START_TRAINING = Endpoint(
    key="start_training",
    title="서울 출발조교현황",
    operation="textDataSeGtscol",
    api_segments=["API329"],
    dataset_pk="15140265",
    note=(
        "출발조교 기록과 비고('출발자세불량' 등). 응답은 가장 최근 조교일 "
        "하루치뿐이고 날짜 파라미터가 없다 — 매일 받아 쌓아야 학습에 쓸 수 있다."
    ),
)

ENTRY_WEIGHT = Endpoint(
    key="entry_weight",
    title="서울 출전마체중",
    operation="textDataHoldSeWegInfo",
    api_segments=["API317"],
    dataset_pk="15133762",
    note=(
        "경주 전 계측 마체중. 개최일 당일에만 값이 실린다(평시 totalCount=0). "
        "이력 축적이 안 되므로 피처가 아니라 표시 정보로 다룬다."
    ),
)

RACE_CANCEL = Endpoint(
    key="race_cancel",
    title="경주마 출전취소 정보",
    operation="raceHorseCancelInfo_1",
    api_segments=["API9_1"],
    dataset_pk="15056779",
    note=(
        "출주 취소마와 사유. 한 번 부르면 최근 한 달치가 함께 오므로 자주 부를 "
        "필요가 없다. 예상에는 반영하지 않고(발주 직전 결정) 결과 집계에서만 뺀다."
    ),
)

REGISTRY: Dict[str, Endpoint] = {
    e.key: e
    for e in (
        ENTRY_SHEET,
        RACE_RESULT,
        RACE_CANCEL,
        START_TRAINING,
        ENTRY_WEIGHT,
        HORSE_BASIC,
        HORSE_DETAIL,
        HORSE_BLOOD,
        DIVIDEND_TOTAL,
        RESULT_TOTAL,
        RC_RACE_INFO,
        HORSE_INFO,
        JOCKEY_RESULT,
        TRAINER_RESULT,
        HIGHEST_DIVIDEND,
    )
}

# 사이트가 동작하려면 반드시 살아 있어야 하는 엔드포인트
REQUIRED_KEYS = ["entry_sheet", "race_result"]


# ---------------------------------------------------------------------------
# 확정 경로 캐시
# ---------------------------------------------------------------------------

def load_resolved(path: Optional[Path] = None) -> Dict[str, str]:
    p = path or RESOLVED_PATH
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("paths", {})
        except (ValueError, OSError) as e:
            log.warning("확정 경로 캐시를 읽지 못했습니다 (%s): %s", p, e)
    return {}


def save_resolved(paths: Dict[str, str], meta: Optional[dict] = None, path: Optional[Path] = None) -> Path:
    p = path or RESOLVED_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"paths": paths, "meta": meta or {}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return p


def resolve(key: str, resolved: Optional[Dict[str, str]] = None) -> str:
    """확정된 경로를 돌려준다. 없으면 첫 번째 후보를 쓴다."""
    resolved = load_resolved() if resolved is None else resolved
    if key in resolved:
        return resolved[key]
    ep = REGISTRY[key]
    return ep.candidate_paths()[0]
