"""경주 단평 생성 (Claude API).

역할 분담이 이 프로젝트의 핵심 설계다.

  * **숫자는 전부 예측 엔진이 만든다.** 승률·연승률·순위는 이미 계산된 값이고,
    언어모델은 그 값을 바꿀 수 없다.
  * **언어모델은 '읽히는 글'만 만든다.** 주어진 사실 목록 밖의 내용을 쓰지 못하도록
    프롬프트에서 강하게 제약하고, 구조화 출력으로 형식을 고정한다.

경마 예상에서 환각은 곧 신뢰도 파산이다. 없는 전적이나 부상 이력을 지어내면
사이트의 유일한 자산인 '검증 가능한 적중률'이 무의미해진다. 그래서 모델에
넘기는 데이터는 DB에서 뽑은 사실만으로 구성하고, 프롬프트는 "주어진 수치 외에는
어떤 사실도 추가하지 말라"를 반복해서 못박는다.

    python -m horseai.comment --db data/horseai.sqlite
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from .kra.store import session, upsert

log = logging.getLogger(__name__)

MODEL = "claude-opus-5"
# 짧은 한국어 카피를 경주 수백 건에 대해 생성하는 작업이라, 최고 난도 추론은
# 필요 없고 비용·지연이 중요하다. 의도적으로 medium 을 선택한다.
EFFORT = "medium"

SYSTEM_PROMPT = """\
당신은 한국 경마 전문지의 예상 기자입니다. 한국마사회(KRA) 경주의 출마표와 \
산출된 승률 자료를 받아, 독자가 읽기 쉬운 한국어 경주 단평을 씁니다.

# 절대 규칙 (위반 시 결과물은 폐기됩니다)
1. **입력으로 주어진 수치와 사실만 사용하십시오.** 부상 이력, 조교 상태, 혈통, \
컨디션, 기수 인터뷰, 날씨 전망 등 입력에 없는 정보는 어떤 경우에도 언급하지 마십시오.
2. **숫자를 바꾸거나 새로 만들지 마십시오.** 승률·전적·부담중량·레이팅은 주어진 \
값을 그대로 인용해야 합니다. 반올림 외의 가공은 금지입니다.
3. 근거가 부족하면 "데이터상 근거가 뚜렷하지 않다"고 쓰십시오. 추측으로 채우지 마십시오.
4. 특정 마권 구매를 권유하거나 수익을 보장하는 표현을 쓰지 마십시오. \
"추천", "예상"까지는 가능하지만 "확실", "필승", "보장"은 금지입니다.
5. **"AI", "인공지능", "모델", "알고리즘" 같은 표현을 쓰지 마십시오.** 수치를 그대로 \
서술하거나 "예측 엔진", "산출 승률"로 지칭합니다. 독자가 읽는 것은 기술 소개가 아니라 \
경주 단평입니다.

# 문체
- 경마 전문지 단평 톤. 간결하고 리듬감 있게, 단정적이되 과장하지 않게.
- 각 문단 2~3문장. 전체 350~500자.
- 말 이름은 처음 언급할 때 "3번 천군만마"처럼 마번을 함께 씁니다.
- 승률은 백분율 정수로 표기합니다 (예: 32%).
"""


class Commentary(BaseModel):
    """생성 결과 스키마. 구조화 출력으로 형식을 강제한다."""

    headline: str = Field(description="25자 이내의 경주 요약 헤드라인. 전문지 표제처럼 간결하게.")
    body: str = Field(description="350~500자의 경주 단평 본문. 문단은 빈 줄로 구분.")
    key_horse: str = Field(description="본문에서 가장 비중 있게 다룬 말의 마명")
    confidence: str = Field(description="예상 자신도. '높음' | '보통' | '낮음' 중 하나.")


RACE_SQL = """
SELECT r.race_key, r.meet, r.rc_date, r.rc_no, r.rc_name, r.distance, r.grade,
       r.age_cond, r.post_time, r.field_size, r.prize1
FROM races r
WHERE r.race_key = ?
"""

RUNNER_SQL = """
SELECT e.chul_no, e.hr_name, e.hr_no, e.sex, e.age, e.origin, e.burden, e.rating,
       e.jk_name, e.tr_name, e.career_1st, e.career_2nd, e.career_3rd, e.career_starts,
       e.y1_1st, e.y1_starts,
       p.p_win, p.p_place, p.pred_rank
FROM entries e
JOIN predictions p ON p.race_key = e.race_key AND p.hr_no = e.hr_no
WHERE e.race_key = ?
ORDER BY p.pred_rank
"""


def _fmt_record(r: sqlite3.Row) -> str:
    starts = r["career_starts"]
    if not starts:
        return "통산 전적 정보 없음"
    wins = r["career_1st"] or 0
    seconds = r["career_2nd"] or 0
    thirds = r["career_3rd"] or 0
    rate = f" (승률 {wins / starts:.0%})" if starts else ""
    return f"통산 {starts}전 {wins}승 2착 {seconds}회 3착 {thirds}회{rate}"


def build_facts(conn: sqlite3.Connection, race_key: str, top_n: int = 6) -> Optional[str]:
    """DB 사실만으로 프롬프트 입력을 구성한다. 여기 없는 건 모델도 쓸 수 없다."""
    race = conn.execute(RACE_SQL, (race_key,)).fetchone()
    if not race:
        return None
    runners = conn.execute(RUNNER_SQL, (race_key,)).fetchall()
    if not runners:
        return None

    lines = [
        "# 경주 정보",
        f"- 경마장: {race['meet']}",
        # rc_name 이 그냥 '제N경주'인 경우가 흔해 중복 표기를 피한다.
        f"- 일자: {race['rc_date']} 제{race['rc_no']}경주"
        + (f" ({race['rc_name']})" if race["rc_name"]
           and race["rc_name"].strip() != f"제{race['rc_no']}경주" else ""),
        f"- 거리: {race['distance']}m",
        f"- 등급: {race['grade'] or '정보 없음'}",
        f"- 출주 두수: {race['field_size'] or len(runners)}두",
    ]
    if race["post_time"]:
        lines.append(f"- 발주 예정: {race['post_time']}")
    if race["prize1"]:
        lines.append(f"- 1착 상금: {int(race['prize1']):,}원")

    lines.append("\n# 상위 출주마 (산출 승률 내림차순)")
    for r in runners[:top_n]:
        p_win = r["p_win"] or 0
        p_place = r["p_place"] or 0
        parts = [
            f"\n## {r['pred_rank']}순위 — {r['chul_no']}번 {r['hr_name']}",
            f"- 산출 승률: {p_win:.0%} / 연승(3착 이내) 확률: {p_place:.0%}",
            f"- {_fmt_record(r)}",
        ]
        if r["y1_starts"]:
            parts.append(f"- 최근 1년: {r['y1_starts']}전 {r['y1_1st'] or 0}승")
        meta = []
        if r["sex"]:
            meta.append(f"{r['sex']}마")
        if r["age"]:
            meta.append(f"{r['age']}세")
        if r["origin"]:
            meta.append(f"산지 {r['origin']}")
        if meta:
            parts.append("- " + ", ".join(meta))
        if r["burden"]:
            parts.append(f"- 부담중량: {r['burden']}kg")
        if r["rating"]:
            parts.append(f"- 레이팅: {int(r['rating'])}")
        if r["jk_name"]:
            parts.append(f"- 기수: {r['jk_name']}" + (f" / 조교사: {r['tr_name']}" if r["tr_name"] else ""))
        lines.extend(parts)

    rest = runners[top_n:]
    if rest:
        others = ", ".join(f"{r['chul_no']}번 {r['hr_name']}({(r['p_win'] or 0):.0%})" for r in rest)
        lines.append(f"\n# 그 외 출주마\n{others}")

    return "\n".join(lines)


def generate_one(client, facts: str) -> Commentary:
    response = client.messages.parse(
        model=MODEL,
        max_tokens=4096,
        output_config={"effort": EFFORT},
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                # 시스템 프롬프트는 경주마다 동일하므로 캐시가 그대로 재사용된다.
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": (
                    "아래는 한 경주의 출마표와 산출된 승률 자료입니다. "
                    "이 정보만 사용해 경주 단평을 작성하십시오.\n\n" + facts
                ),
            }
        ],
        output_format=Commentary,
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"모델이 응답을 거부했습니다: {response.stop_details}")
    return response.parsed_output


def pending_races(conn: sqlite3.Connection, limit: int = 50) -> List[str]:
    """예측은 있는데 코멘트가 아직 없는, 아직 시행 전인 경주."""
    rows = conn.execute(
        "SELECT DISTINCT p.race_key FROM predictions p "
        "JOIN races r ON r.race_key = p.race_key "
        "LEFT JOIN commentaries c ON c.race_key = p.race_key "
        "WHERE c.race_key IS NULL AND COALESCE(r.has_result,0) = 0 "
        "ORDER BY r.rc_date, r.meet, r.rc_no LIMIT ?",
        (limit,),
    ).fetchall()
    return [r[0] for r in rows]


def run(conn: sqlite3.Connection, race_keys: Optional[List[str]] = None,
        limit: int = 50) -> int:
    keys = race_keys or pending_races(conn, limit)
    if not keys:
        log.info("코멘트를 생성할 경주가 없습니다.")
        return 0

    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning("ANTHROPIC_API_KEY 가 없어 코멘트 생성을 건너뜁니다. "
                    "(예측 수치만으로도 사이트는 정상 빌드됩니다)")
        return 0

    import anthropic

    client = anthropic.Anthropic()
    written = 0
    for key in keys:
        facts = build_facts(conn, key)
        if not facts:
            log.warning("%s: 사실 데이터를 구성하지 못해 건너뜁니다.", key)
            continue
        try:
            c = generate_one(client, facts)
        except anthropic.APIStatusError as e:
            log.error("%s: API 오류 %s — 건너뜁니다.", key, e.status_code)
            continue
        except Exception as e:  # noqa: BLE001 - 한 경주 실패가 전체를 막지 않게 한다
            log.error("%s: 코멘트 생성 실패 (%s) — 건너뜁니다.", key, e)
            continue

        upsert(conn, "commentaries", [{
            "race_key": key,
            "headline": c.headline,
            "body": c.body,
            "model": MODEL,
        }], ["race_key"])
        conn.commit()
        written += 1
        log.info("%s  %s", key, c.headline)
    return written


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="경주 단평 생성")
    ap.add_argument("--db", default="data/horseai.sqlite")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--race", nargs="*", help="특정 race_key 만")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with session(args.db) as conn:
        n = run(conn, args.race, args.limit)
    print(f"코멘트 {n}건 생성")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
