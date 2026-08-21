"""수집기: 출전표·경주성적을 SQLite 로 적재한다.

    python -m horseai.kra.collect backfill --years 5      # 과거 성적 백필(학습용)
    python -m horseai.kra.collect results --days 14       # 최근 성적 갱신(검증용)
    python -m horseai.kra.collect entries --ahead 7       # 다가올 경주 출전표(예측용)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .client import KraApiError, KraClient, redact
from .endpoints import ACTIVE_MEETS, MEETS, resolve
from .normalize import (
    ENTRY_FIELDS,
    RESULT_FIELDS,
    audit_coverage,
    extract,
    race_key,
    to_date,
    to_float,
    to_int,
    to_meet,
    to_str,
    to_weight,
    to_weight_delta,
    unmapped_keys,
)
from ..clock import today_kst
from .store import DEFAULT_DB, dumps, already_fetched, log_fetch, session, upsert

log = logging.getLogger(__name__)

# 평상시 시행일은 금·토·일이지만 공휴일에는 월요일에도 열린다(실제로
# 2026-08-17 광복절 대체공휴일에 시행됐다). 요일로 거르면 그런 날을 통째로
# 놓치고, 놓쳤다는 사실조차 드러나지 않는다 — 조회를 안 했으니 실패도 아니다.
#
# 매일 물어보는 비용은 작다. 경주가 없는 날은 빈 응답 한 건으로 끝나고,
# 그 편이 '있는데 안 물어보는' 것보다 훨씬 낫다.
RACE_WEEKDAYS = tuple(range(7))


# 이 일수 안의 날짜는 수집 기록이 있어도 다시 받는다. 결과는 경주가 끝나는 대로
# 순차 게시되고, 배당 확정이나 심판 판정으로 뒤늦게 바뀌기도 한다.
RESETTLE_DAYS = 2

# 이만큼 연달아 응답을 못 받으면 포털이 죽은 것으로 보고 끊는다.
# 끝까지 밀어붙여 봐야 시간만 태우고 결과는 같다.
NETWORK_FAIL_STREAK = 6


def race_days(start: dt.date, end: dt.date) -> List[dt.date]:
    """구간 내 경마 시행 가능일. 공휴일 시행이 있어 모든 요일을 훑는다."""
    out, d = [], start
    while d <= end:
        if d.weekday() in RACE_WEEKDAYS:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def _race_row(rec: Dict, norm: Dict, key: str) -> Dict:
    """경주 단위 메타 행을 만든다 (출전표/성적 공통)."""
    return {
        "race_key": key,
        "meet": norm.get("meet"),
        "rc_date": norm.get("rc_date"),
        "rc_no": norm.get("rc_no"),
        "rc_day": norm.get("rc_day"),
        "rc_name": norm.get("rc_name"),
        "distance": norm.get("distance"),
        "grade": norm.get("grade"),
        "age_cond": norm.get("age_cond"),
        "budam_type": norm.get("budam_type"),
        "post_time": norm.get("post_time"),
        "field_size": norm.get("field_size"),
        "prize1": norm.get("prize1"),
        "weather": norm.get("weather"),
        "track_cond": norm.get("track_cond"),
        # has_result 는 여기서 쓰지 않는다. 출전표 수집이 0 으로 되돌리면 이미
        # 시행된 경주가 다시 '예정'으로 살아난다. 성적 수집이 1착을 확인했을 때
        # 별도 UPDATE 로만 세운다.
    }


ENTRY_COLS = {f.name for f in ENTRY_FIELDS}
RESULT_COLS = {f.name for f in RESULT_FIELDS}

_ENTRY_TABLE_COLS = [
    "race_key", "chul_no", "hr_no", "hr_name", "origin", "sex", "age", "burden", "rating",
    "jk_no", "jk_name", "tr_no", "tr_name", "ow_no", "ow_name", "ilsu",
    "career_prize", "prize_1y", "prize_6m", "career_1st", "career_2nd", "career_3rd",
    "career_starts", "y1_1st", "y1_2nd", "y1_3rd", "y1_starts",
]
_RESULT_TABLE_COLS = [
    "race_key", "chul_no", "hr_no", "hr_name", "ord", "ord_note", "jk_no", "jk_name",
    "tr_no", "tr_name", "age", "sex", "origin", "burden", "rating",
    "horse_weight", "weight_delta", "rank_rise", "record_sec", "margin",
    "win_odds", "place_odds", "jk_reduction", "gear",
    "s1f_rank", "g1f_rank", "c1_rank", "c2_rank", "c3_rank", "c4_rank",
    "s1f_sec", "g3f_sec", "g1f_sec",
]


def _ingest(conn: sqlite3.Connection, records: List[Dict], *, kind: str) -> int:
    """정규화 후 races + entries/results 에 적재."""
    if not records:
        return 0
    fields = ENTRY_FIELDS if kind == "entries" else RESULT_FIELDS
    table_cols = _ENTRY_TABLE_COLS if kind == "entries" else _RESULT_TABLE_COLS

    race_rows: Dict[str, Dict] = {}
    detail_rows: List[Dict] = []
    settled_keys: set = set()

    for rec in records:
        norm = extract(rec, fields)
        if not (norm.get("meet") and norm.get("rc_date") and norm.get("rc_no") is not None):
            continue
        key = race_key(norm["meet"], norm["rc_date"], norm["rc_no"])
        prev = race_rows.get(key)
        # 성적 API 는 **발주 전에도** 출주 정보를 돌려준다. 착순이 비어 있는데
        # '시행 완료'로 찍으면, 아직 달리지도 않은 경주가 결과 페이지에 전부
        # 불발로 올라간다. 게다가 착순 칸에는 91~99 상태 코드(출전취소 등)가
        # 먼저 실리기도 해서 '값이 있으면 완료'로도 판단할 수 없다.
        # 1착이 확인될 때만 그 경주는 끝난 것이다.
        settled = kind == "results" and norm.get("ord") == 1
        row = _race_row(rec, norm, key)
        if settled:
            settled_keys.add(key)
        if prev:  # 같은 경주의 여러 말 레코드 → 비어 있는 값만 채운다
            for k, v in row.items():
                if prev.get(k) in (None, "") and v not in (None, ""):
                    prev[k] = v
        else:
            race_rows[key] = row

        detail = {c: norm.get(c) for c in table_cols if c in norm}
        detail["race_key"] = key
        detail["raw_json"] = dumps(rec)
        if kind == "entries" and detail.get("chul_no") is None:
            continue  # 출전표 PK 는 (race_key, chul_no)
        if not detail.get("hr_no"):
            continue
        detail_rows.append(detail)

    upsert(conn, "races", list(race_rows.values()), ["race_key"])
    if kind == "entries":
        n = upsert(conn, "entries", detail_rows, ["race_key", "chul_no"])
    else:
        n = upsert(conn, "results", detail_rows, ["race_key", "hr_no"])
        if settled_keys:
            conn.executemany(
                "UPDATE races SET has_result=1 WHERE race_key=?",
                [(k,) for k in settled_keys],
            )
    return n


def fetch_day(
    client: KraClient,
    conn: sqlite3.Connection,
    *,
    kind: str,
    meet: int,
    day: dt.date,
    force: bool = False,
) -> int:
    """특정 경마장·날짜 하루치를 수집한다."""
    ep_key = "entry_sheet" if kind == "entries" else "race_result"
    ymd = day.strftime("%Y%m%d")

    # 최근 며칠은 수집 기록이 있어도 다시 받는다.
    #
    # fetch_log 는 '한 번 받았다'만 기록하고 그날이 **끝났는지**는 모른다. 경주가
    # 진행 중일 때 받으면 1경주만 담긴 채 완료로 표시되고, 그 뒤 크론은 그날을
    # 통째로 건너뛴다 — 나머지 경주의 결과와 적중률이 영영 비게 된다.
    # 지난 날짜는 더 바뀔 것이 없으므로 기록을 그대로 믿는다.
    settled = day < today_kst() - dt.timedelta(days=RESETTLE_DAYS)
    if not force and settled and already_fetched(conn, ep_key, str(meet), ymd):
        return -1  # 이미 수집됨 (확정된 과거 날짜)

    path = resolve(ep_key)
    try:
        records = client.fetch(path, {"meet": meet, "rc_date": ymd}, rows=500)
    except KraApiError as e:
        if e.fatal:
            raise
        log.warning("%s %s %s 수집 실패: %s", kind, MEETS.get(meet, meet), ymd, redact(e))
        # -2 는 '실패'. 0(자료 없음)과 반드시 구분해야 한다 — 세션 한도나 포털
        # 장애로 응답을 못 받은 것을 '그날 경주가 없다' 로 세면, 실제로 열린
        # 경주일을 통째로 놓치고도 정상 종료로 보인다.
        return -2

    n = _ingest(conn, records, kind=kind)
    log_fetch(conn, ep_key, str(meet), ymd, len(records))
    conn.commit()
    if records:
        log.info("  %s %s %s → %d건", MEETS.get(meet, meet), ymd, kind, n)
    return n


def run_range(
    client: KraClient,
    conn: sqlite3.Connection,
    *,
    kind: str,
    start: dt.date,
    end: dt.date,
    meets: Optional[Iterable[int]] = None,
    force: bool = False,
) -> Dict[str, int]:
    meets = list(meets or ACTIVE_MEETS)
    days = race_days(start, end)
    total, skipped, empty, failed = 0, 0, 0, 0
    log.info("%s 수집: %s ~ %s, 시행일 %d일 × 경마장 %d곳",
             kind, start, end, len(days), len(meets))
    # 포털이 죽으면 한 건마다 40초씩 태우다 배치가 통째로 타임아웃된다. 요일
    # 제한을 푼 뒤 조회가 9건에서 33건으로 늘어 실제로 그렇게 죽었다(8/18~21
    # 나흘 연속). 연달아 실패하면 나머지는 물어봐야 결과가 뻔하므로 끊는다 —
    # 남는 시간은 상위의 재시도가 쓴다.
    streak = 0
    for day in days:
        if streak >= NETWORK_FAIL_STREAK:
            break
        for meet in meets:
            n = fetch_day(client, conn, kind=kind, meet=meet, day=day, force=force)
            if n == -2:
                failed += 1
                streak += 1
                if streak >= NETWORK_FAIL_STREAK:
                    log.warning("  ⚠ %d건 연속 실패 — 포털이 응답하지 않는다. "
                                "남은 조회를 중단한다", streak)
                    break
                continue
            streak = 0
            if n < 0:
                skipped += 1
            elif n == 0:
                empty += 1
            else:
                total += n
    out = {"ingested": total, "skipped": skipped, "empty": empty,
           "failed": failed, "days": len(days)}
    if failed:
        log.warning("  ⚠ %d건 응답을 받지 못했다 — '자료 없음' 과 다르다. "
                    "그날 경주가 있었는지 아직 알 수 없다", failed)
    return out


MEET_CODE = {"서울": 1, "제주": 2, "부산경남": 3}


# 승식 코드. 조합 수로 확인한 것이다 — 10두 경주에서 TRI 는 720개(=10P3, 순서
# 있음)이고 TLA 는 120개(=10C3, 순서 없음)다. 이름만 보고 TRI 를 삼복승으로
# 잡았다가 틀린 조합의 배당을 저장할 뻔했다.
POOL_NAMES = {
    "WIN": "단승", "PLC": "연승", "QNL": "복승", "EXA": "쌍승",
    "QPL": "복연승", "TLA": "삼복승", "TRI": "삼쌍승",
}
# 순서를 가리지 않는 승식은 조합을 정렬해 두어야 조회가 맞는다.
UNORDERED_POOLS = {"QNL", "QPL", "TLA"}


def winning_combos(conn: sqlite3.Connection, race_keys) -> Dict[str, Dict[str, set]]:
    """경주별 '적중 조합'을 만든다.

    배당 API 는 전체 배당률 보드를 준다 — 서울 하루치가 1만 3천 행이다. 그중
    의미 있는 것은 실제로 적중한 조합뿐이므로(나머지는 사지 않은 조합의 가정
    배당), 착순에서 적중 조합을 만들어 그것만 골라 담는다.
    """
    out: Dict[str, Dict[str, set]] = {}
    if not race_keys:
        return out
    qs = ",".join("?" for _ in race_keys)
    rows = conn.execute(
        f"SELECT race_key, chul_no, ord FROM results "
        f"WHERE race_key IN ({qs}) AND ord BETWEEN 1 AND 3 AND chul_no IS NOT NULL",
        list(race_keys)).fetchall()
    by_race: Dict[str, Dict[int, int]] = {}
    for r in rows:
        by_race.setdefault(r["race_key"], {})[int(r["ord"])] = int(r["chul_no"])
    for key, o in by_race.items():
        a, b, c = o.get(1), o.get(2), o.get(3)
        if not a:
            continue
        j = lambda *xs: "-".join(str(x) for x in xs)          # noqa: E731
        srt = lambda *xs: j(*sorted(xs))                       # noqa: E731
        combos = {"WIN": {j(a)}, "PLC": {j(x) for x in (a, b, c) if x}}
        if b:
            combos["QNL"] = {srt(a, b)}
            combos["EXA"] = {j(a, b)}
        if b and c:
            combos["QPL"] = {srt(a, b), srt(a, c), srt(b, c)}
            combos["TLA"] = {srt(a, b, c)}      # 삼복승 — 순서 무관
            combos["TRI"] = {j(a, b, c)}        # 삼쌍승 — 순서까지
        out[key] = combos
    return out


def collect_dividends(client: KraClient, conn: sqlite3.Connection,
                      start: dt.date, end: dt.date) -> Dict[str, int]:
    """승식별 확정배당(API301)에서 **적중 조합의 배당만** 받아 쌓는다.

    단승·연승 배당은 경주성적에도 있지만 복승·쌍승·복연승·삼복승·삼쌍승은 여기에만
    있다. 예상지 독자는 단승만 사지 않으므로, 이것이 없으면 '우리 추천대로
    샀다면 얼마가 됐나'를 답할 수 없다.
    """
    stats = {"days": 0, "rows": 0, "skipped": 0}
    for day in race_days(start, end):
        ymd = day.strftime("%Y%m%d")
        for meet in ACTIVE_MEETS:
            # 결과 수집과 같은 이유로 최근 며칠은 기록이 있어도 다시 받는다.
            # 경주가 진행 중일 때 받으면 그 시점까지 끝난 경주만 담긴 채 완료로
            # 표시되고, 나머지 경주의 배당은 영영 비게 된다.
            settled = day < today_kst() - dt.timedelta(days=RESETTLE_DAYS)
            if settled and already_fetched(conn, "dividends", str(meet), ymd):
                stats["skipped"] += 1
                continue
            keys = [r[0] for r in conn.execute(
                "SELECT race_key FROM races WHERE rc_date = ? AND meet = ? "
                "AND COALESCE(has_result,0) = 1",
                (day.isoformat(), MEETS.get(meet, meet)))]
            if not keys:
                continue
            want = winning_combos(conn, keys)
            if not want:
                continue
            try:
                recs = client.fetch(resolve("dividend_total"),
                                    {"meet": meet, "rc_date": ymd},
                                    rows=1000, max_pages=40)
            except Exception as e:  # noqa: BLE001
                log.warning("배당 %s %s 수집 실패: %s", MEETS.get(meet, meet), ymd, redact(e))
                continue

            rows = []
            for r in recs:
                pool = to_str(r.get("pool"))
                key = race_key(to_meet(r.get("meet")), to_date(r.get("rcDate")),
                               to_int(r.get("rcNo")))
                nums = [to_int(r.get(k)) for k in ("chulNo", "chulNo2", "chulNo3")]
                nums = [n for n in nums if n]
                if not (pool and key and nums):
                    continue
                if pool in UNORDERED_POOLS:
                    nums = sorted(nums)
                combo = "-".join(str(n) for n in nums)
                if combo not in want.get(key, {}).get(pool, ()):
                    continue                      # 적중하지 않은 조합은 버린다
                rows.append({"race_key": key, "pool": pool, "combo": combo,
                             "odds": to_float(r.get("odds"))})
            n = upsert(conn, "dividends", rows, ["race_key", "pool", "combo"])
            log_fetch(conn, "dividends", str(meet), ymd, len(recs))
            conn.commit()
            stats["rows"] += n
            if n:
                log.info("  배당 %s %s → %d경주 %d건", MEETS.get(meet, meet), ymd, len(want), n)
        stats["days"] += 1
    return stats


def collect_training(client: KraClient, conn: sqlite3.Connection,
                     start: dt.date, end: dt.date) -> Dict[str, int]:
    """일별훈련 상세(API18_1)를 기간으로 받아 쌓는다.

    이 API 는 tr_date 로 과거 조회가 되고 2019년까지 소급된다. 그래서 매일
    받아 축적할 필요 없이 **한 번에 백필**할 수 있다 — 서울 전용이고 하루치만
    주던 API329 와는 성격이 다르다.

    조교는 경주일이 아니라 거의 매일 이뤄지므로 날짜를 하나씩 훑는다.
    이미 받은 날은 fetch_log 로 건너뛴다.
    """
    stats = {"days": 0, "rows": 0, "skipped": 0}
    day = start
    while day <= end:
        ymd = day.strftime("%Y%m%d")
        if already_fetched(conn, "daily_training", "ALL", ymd):
            stats["skipped"] += 1
            day += dt.timedelta(days=1)
            continue
        try:
            recs = client.fetch(resolve("daily_training"), {"tr_date": ymd},
                                rows=500, max_pages=12)
        except Exception as e:  # noqa: BLE001 — 하루 실패가 전체를 막지 않는다
            log.warning("조교 %s 수집 실패: %s", ymd, redact(e))
            day += dt.timedelta(days=1)
            continue

        rows = []
        for r in recs:
            hr_no = to_str(r.get("hrNo"))
            trng_dt = to_date(r.get("trDate"))
            if not (hr_no and trng_dt):
                continue
            rows.append({
                "meet": to_meet(r.get("meet")),
                "trng_dt": trng_dt,
                "hr_no": hr_no,
                "hr_name": to_str(r.get("hrName")),
                "tr_name": to_str(r.get("trName")),
                "part": to_str(r.get("part")),
                "pr_gubun": to_str(r.get("prGubun")),
                "tr_term": to_int(r.get("trTerm")),
                "run1_cnt": to_int(r.get("run1Cnt")),
                "run2_cnt": to_int(r.get("run2Cnt")),
                "chul_gubun": to_str(r.get("chulGubun")),
                "st_time": to_str(r.get("stTime")),
                "raw_json": dumps(r),
            })
        # 같은 말이 하루에 여러 번 조교하기도 한다 — 마지막 것만 남기지 않고
        # 시작시각까지 키에 넣어 전부 보존한다.
        n = upsert(conn, "daily_training", rows, ["meet", "trng_dt", "hr_name"])
        log_fetch(conn, "daily_training", "ALL", ymd, len(recs))
        conn.commit()
        stats["days"] += 1
        stats["rows"] += n
        if recs:
            log.info("  조교 %s → %d건", ymd, n)
        day += dt.timedelta(days=1)
    return stats


def collect_daily(client: KraClient, conn: sqlite3.Connection) -> Dict[str, int]:
    """조교·마체중·출주취소를 받아 이력으로 쌓는다.

    셋의 성격이 다르다 (실제 응답으로 확인한 것):

      * **조교현황** — 가장 최근 조교일 *하루치만* 준다. 날짜 파라미터가 없어
        지나간 날은 다시 받을 수 없다. **하루를 거르면 그 하루는 영구히 사라진다.**
        매일 한 번은 돌아야 하는 유일한 이유다.
      * **출주취소** — 한 번 부르면 최근 한 달치를 함께 준다. 급하지 않다.
      * **마체중** — 개최일에만 값이 실린다(평시 totalCount=0). 응답이 자주
        끊기므로 실패를 정상 경로로 취급한다.

    셋을 '전부 오늘 것만 온다'고 뭉뚱그리면 필요 없는 호출을 반복하게 된다.
    """
    today = today_kst().isoformat()
    stats = {"training": 0, "weight": 0, "cancel": 0}

    # --- 조교 현황 (서울) --------------------------------------------------
    try:
        recs = client.fetch(resolve("start_training"), {}, rows=500, max_pages=6)
        rows = []
        for r in recs:
            name = (r.get("hrnm") or "").strip()
            if not name:
                continue
            rows.append({
                "meet": "서울",
                "trng_dt": to_date(r.get("trngDt")) or today,
                "hr_name": name,
                "belo_no": to_str(r.get("beloNo")),
                "trng_cnt": to_int(r.get("beloTrngNo")),
                "rider": to_str(r.get("ridrNm")),
                "remark": to_str(r.get("remkTxt")),
                "raw_json": dumps(r),
            })
        stats["training"] = upsert(conn, "daily_training", rows, ["meet", "trng_dt", "hr_name"])
        conn.commit()
    except Exception as e:  # noqa: BLE001 — 한 소스 실패가 나머지를 막으면 안 된다
        log.warning("조교 현황 수집 실패: %s", redact(e))

    # --- 출전마 체중 (서울) — 개최일에만 값이 실린다 -------------------------
    try:
        recs = client.fetch(resolve("entry_weight"), {}, rows=500, max_pages=6)
        rows = []
        for r in recs:
            name = (r.get("hrnm") or r.get("hrName") or "").strip()
            if not name:
                continue
            rows.append({
                "meet": "서울",
                "rc_date": to_date(r.get("rcDate") or r.get("raceDe")) or today,
                "hr_name": name,
                "rc_no": to_int(r.get("rcNo")),
                "chul_no": to_int(r.get("chulNo")),
                "weight": to_weight(r.get("wgHr") or r.get("weight")),
                "weight_delta": to_weight_delta(r.get("wgHr") or r.get("weight")),
                "raw_json": dumps(r),
            })
        stats["weight"] = upsert(conn, "entry_weight", rows, ["meet", "rc_date", "hr_name"])
        conn.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("출전마 체중 수집 실패: %s", redact(e))

    # --- 출주 취소 (전 경마장) ----------------------------------------------
    rows = []
    for meet in ACTIVE_MEETS:
        try:
            recs = client.fetch(resolve("race_cancel"), {"meet": meet}, rows=300, max_pages=4)
        except Exception as e:  # noqa: BLE001
            log.warning("출주취소 수집 실패(%s): %s", MEETS.get(meet, meet), redact(e))
            continue
        for r in recs:
            hr_no = to_str(r.get("hrNo"))
            if not hr_no:
                continue
            rows.append({
                "race_key": race_key(r.get("meet"), r.get("rcDate"), r.get("rcNo")),
                "hr_no": hr_no,
                "hr_name": to_str(r.get("hrName")),
                "chul_no": to_int(r.get("chulNo")),
                "reason": to_str(r.get("reason")),
            })
    stats["cancel"] = upsert(conn, "cancellations", rows, ["race_key", "hr_no"])

    conn.commit()
    return stats


def reingest(conn: sqlite3.Connection, kind: str, batch: int = 5000) -> int:
    """보존해 둔 raw_json 으로 다시 정규화한다.

    별칭이 틀렸거나 스키마에 컬럼이 빠져 있었다는 게 나중에 드러났을 때,
    수천 번의 API 재호출 없이 로컬에서 복구하기 위한 경로다. raw_json 을
    남겨 두기로 한 결정이 값을 하는 지점이다.
    """
    table = "entries" if kind == "entries" else "results"
    fields = ENTRY_FIELDS if kind == "entries" else RESULT_FIELDS
    total = 0
    offset = 0
    while True:
        rows = conn.execute(
            f"SELECT rowid, race_key, raw_json FROM {table} "
            f"WHERE raw_json IS NOT NULL ORDER BY rowid LIMIT ? OFFSET ?",
            (batch, offset),
        ).fetchall()
        if not rows:
            break
        recs = []
        for r in rows:
            try:
                rec = json.loads(r["raw_json"])
            except (ValueError, TypeError):
                continue
            norm = extract(rec, fields)
            norm["race_key"] = r["race_key"]
            norm["_rowid"] = r["rowid"]
            recs.append(norm)

        cols = _ENTRY_TABLE_COLS if kind == "entries" else _RESULT_TABLE_COLS
        target_cols = [c for c in cols if c != "race_key"]
        for rec in recs:
            sets = {c: rec.get(c) for c in target_cols if c in rec}
            if not sets:
                continue
            assign = ", ".join(f"{c}=?" for c in sets)
            conn.execute(f"UPDATE {table} SET {assign} WHERE rowid=?",
                         (*sets.values(), rec["_rowid"]))
        conn.commit()
        total += len(recs)
        offset += batch
        log.info("  재정규화 %s: %d행", table, total)
    return total


def audit(client: KraClient, kind: str, meet: int = 1) -> None:
    """별칭 매핑이 실제 응답과 맞는지 점검한다 (프로브 이후 1회 실행 권장)."""
    ep_key = "entry_sheet" if kind == "entries" else "race_result"
    fields = ENTRY_FIELDS if kind == "entries" else RESULT_FIELDS
    path = resolve(ep_key)

    today = today_kst()
    for back in range(0, 30):
        day = today - dt.timedelta(days=back)
        if day.weekday() not in RACE_WEEKDAYS:
            continue
        records = client.fetch(path, {"meet": meet, "rc_date": day.strftime("%Y%m%d")}, rows=50)
        if records:
            break
    else:
        print("최근 30일 내 조회 가능한 경주가 없습니다.")
        return

    cov = audit_coverage(records, fields)
    print(f"[{kind}] {day} {MEETS.get(meet)} — 레코드 {len(records)}건")
    print("\n  매핑 실패(0%) 컬럼:")
    bad = [k for k, v in cov.items() if v == 0.0]
    print("    " + (", ".join(bad) if bad else "없음 ✓"))
    print("\n  부분 결측 컬럼:")
    part = [f"{k}={v:.0%}" for k, v in cov.items() if 0 < v < 0.9]
    print("    " + (", ".join(part) if part else "없음 ✓"))
    print("\n  별칭에 안 걸린 원본 필드:")
    print("    " + (", ".join(unmapped_keys(records, fields)) or "없음 ✓"))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="한국마사회 데이터 수집")
    ap.add_argument("command",
                    choices=["backfill", "results", "entries", "audit", "stats",
                             "prune", "reingest", "daily", "training", "dividends"])
    ap.add_argument("--years", type=float, default=5, help="backfill 기간(년)")
    ap.add_argument("--days", type=int, default=14, help="results 소급 일수")
    ap.add_argument("--ahead", type=int, default=7, help="entries 조회 일수(미래)")
    ap.add_argument("--meets", type=int, nargs="*", default=None, help="경마장 코드")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--force", action="store_true", help="이미 수집한 날짜도 다시 받기")
    ap.add_argument("--kind", default="entries", choices=["entries", "results"], help="audit 대상")
    ap.add_argument("--keep-days", type=int, default=180, help="prune: 원본 JSON 보존 기간")
    ap.add_argument("--train-days", type=int, default=90,
                    help="training 소급 일수 (조교는 경주일이 아니라 거의 매일 있다)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    today = today_kst()

    with session(args.db) as conn:
        if args.command == "stats":
            from .store import counts
            for k, v in counts(conn).items():
                print(f"  {k:<14} {v:>8,}")
            row = conn.execute("SELECT MIN(rc_date) a, MAX(rc_date) b FROM races").fetchone()
            print(f"  기간           {row['a']} ~ {row['b']}")
            return 0

        if args.command == "reingest":
            n = reingest(conn, args.kind)
            print(f"{args.kind} {n:,}행 재정규화 완료 (API 재호출 없음)")
            return 0

        if args.command == "prune":
            from .store import prune_raw_json
            n = prune_raw_json(conn, keep_days=args.keep_days)
            size = Path(args.db).stat().st_size / 1e6 if Path(args.db).exists() else 0
            print(f"원본 JSON {n:,}행 정리, DB 크기 {size:.1f}MB")
            return 0

        try:
            client = KraClient.from_env()
        except ValueError as e:
            print(f"✗ {e}", file=sys.stderr)
            return 2

        if args.command == "dividends":
            start = today - dt.timedelta(days=args.days)
            print(f"배당 수집: {start} ~ {today}")
            st = collect_dividends(client, conn, start, today)
            print(f"완료: {st['rows']:,}건 · 기수집 {st['skipped']}건")
            return 0

        if args.command == "training":
            start = today - dt.timedelta(days=args.train_days)
            print(f"조교 수집: {start} ~ {today}  ({args.train_days}일)")
            st = collect_training(client, conn, start, today)
            print(f"완료: 신규 {st['days']}일 / {st['rows']:,}건 · 기수집 {st['skipped']}일")
            return 0

        if args.command == "daily":
            stats = collect_daily(client, conn)
            print(f"조교 {stats['training']:,}건 · 마체중 {stats['weight']:,}건 "
                  f"· 출주취소 {stats['cancel']:,}건")
            return 0

        if args.command == "audit":
            audit(client, args.kind, (args.meets or [1])[0])
            return 0

        if args.command == "backfill":
            start = today - dt.timedelta(days=int(365 * args.years))
            # --kind entries 로 출전표도 소급 수집한다. 출전표에는 KRA 공식 통산
            # 전적·상금·레이팅이 실려 있어 학습 피처의 질을 좌우한다.
            stats = run_range(client, conn, kind=args.kind, start=start, end=today,
                              meets=args.meets, force=args.force)
        elif args.command == "results":
            start = today - dt.timedelta(days=args.days)
            stats = run_range(client, conn, kind="results", start=start, end=today,
                              meets=args.meets, force=args.force)
        else:  # entries
            stats = run_range(client, conn, kind="entries", start=today,
                              end=today + dt.timedelta(days=args.ahead),
                              meets=args.meets, force=True)

        log.info("완료: %s", stats)

        # 응답을 하나도 못 받았으면 실패로 끝낸다.
        #
        # 8/12 에 출전표 수집 15건이 전부 ConnectTimeout 이었는데 continue-on-error
        # 때문에 단계가 초록으로 넘어갔고, 이후 예측·빌드가 빈 자료로 돌아 정상
        # 완료처럼 보였다. 경주 이틀 전이라 우연히 발견했지만, 금요일 새벽에
        # 같은 일이 나면 경주 당일까지 빈 사이트가 된다.
        #
        # '자료가 아직 없다'(empty)와는 다르다 — 그건 정상이다. 여기서 막는 것은
        # **물어보지도 못한** 경우뿐이다.
        if stats.get("failed") and not stats.get("ingested"):
            log.error("✗ %d건 모두 응답을 받지 못했다 — 수집분이 없다",
                      stats["failed"])
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
