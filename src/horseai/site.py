"""정적 사이트 생성기.

DB → Jinja2 → dist/ 정적 HTML. 서버가 없으므로 검색 기능은 클라이언트 측 JSON
인덱스로 처리하고, 나머지는 전부 빌드 타임에 확정한다.

SEO 를 의식한 선택들:
  * 경주별 개별 URL (/race/seoul-20260807-3/) — 롱테일 검색 유입의 핵심.
  * URL 은 로마자 슬러그. 한글 경로는 인코딩되어 공유·분석 시 깨져 보인다.
  * JSON-LD 구조화 데이터, canonical, OG 태그, sitemap.xml 자동 생성.
  * 적중률 페이지를 최상위에 두고 내부 링크를 몰아준다. 경마 예상 사이트에서
    검증된 적중률은 유일한 차별점이자 가장 강력한 신뢰 신호다.

    python -m horseai.site --db data/horseai.sqlite --out dist
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .clock import now_kst, today_kst
from .kra.normalize import MAX_ORD, ORD_STATUS
from .kra.store import session
from .style import STYLE_LABEL, STYLES, pace_map
from .verify import (
    POOL_LABEL, _combos, build_report, load_dividends, set_min_sample,
)

log = logging.getLogger(__name__)

MEET_SLUG = {"서울": "seoul", "부산경남": "busan", "제주": "jeju", "영천": "yeongcheon"}
WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]


def race_slug(race_key: str) -> str:
    """'서울-20260807-01' → 'seoul-20260807-1'"""
    parts = race_key.split("-")
    if len(parts) != 3:
        return re.sub(r"[^A-Za-z0-9-]", "", race_key) or "race"
    meet, date, no = parts
    return f"{MEET_SLUG.get(meet, 'x')}-{date}-{int(no)}"


# ---------------------------------------------------------------------------
# 데이터 조회
# ---------------------------------------------------------------------------

RACE_LIST_SQL = """
SELECT r.race_key, r.meet, r.rc_date, r.rc_no, r.rc_name, r.distance, r.grade,
       r.post_time, r.field_size, r.has_result, c.headline,
       s.conf_score, s.conf_label
FROM races r
LEFT JOIN commentaries c ON c.race_key = r.race_key
LEFT JOIN simulations s ON s.race_key = r.race_key
WHERE EXISTS (SELECT 1 FROM predictions p WHERE p.race_key = r.race_key)
"""

DETAIL_SQL = """
SELECT e.chul_no, e.hr_name, e.hr_no, e.sex, e.age, e.origin, e.burden, e.rating,
       e.jk_no, e.jk_name, e.tr_no, e.tr_name, e.ow_name,
       e.career_1st, e.career_2nd, e.career_3rd, e.career_starts,
       p.p_win, p.p_place, p.p_top2, p.pred_rank, p.style_code, p.tags,
       res.ord, res.win_odds, res.place_odds
FROM predictions p
JOIN entries e ON e.race_key = p.race_key AND e.hr_no = p.hr_no
LEFT JOIN results res ON res.race_key = p.race_key AND res.hr_no = p.hr_no
WHERE p.race_key = ?
ORDER BY p.pred_rank
"""


from .marks import (  # noqa: F401 — 템플릿과 하위 모듈이 이 이름으로 참조한다
    MARK_LIMIT, MARK_MEANING, MARK_SEQUENCE, MARK_SEQUENCE_STAR, MARK_THRESHOLDS,
    assign_marks,
)


def _row_to_dict(row: sqlite3.Row) -> Dict:
    return {k: row[k] for k in row.keys()}


def betting_combos(runners: List[Dict]) -> List[Dict]:
    """AI 확률에서 유도한 조합.

    예상지의 '기본 / 방어 / 삼복승' 칸에 대응한다. 구매를 권하는 것이 아니라
    모델 확률을 마권 형식으로 옮겨 적은 것이며, 화면에도 그렇게 표기한다.
    """
    if len(runners) < 3:
        return []
    g = [r["chul_no"] for r in runners[:5] if r.get("chul_no")]
    if len(g) < 3:
        return []

    combos = [{
        "label": "기본",
        "value": f"{g[0]}-{g[1]}",
        "note": "승률 1·2순위 조합",
    }, {
        "label": "방어",
        "value": f"{g[2]}-{g[0]},{g[1]}",
        "note": "3순위를 축으로 상위 두 두와 묶은 대비책",
    }]
    if len(g) >= 5:
        combos.append({
            "label": "삼복승",
            "value": f"{g[0]}-{g[1]}-{g[2]},{g[3]},{g[4]}",
            "note": "1·2순위 고정, 3~5순위로 확장",
        })
    return combos


def focus_points(runners: List[Dict]) -> List[Dict]:
    """'AI가 주목한 포인트' — 예상지의 승부지수 카테고리에 대응.

    태그를 뒤집어 카테고리별 마번으로 묶는다. 같은 정보를 말 중심이 아니라
    포인트 중심으로 한 번 더 보여주면 훑어보기가 빨라진다.
    """
    buckets: Dict[str, List[int]] = {}
    for r in runners:
        for t in r.get("tag_list") or []:
            # 수치가 섞인 태그(승률 23%)는 카테고리로 묶기 어렵다
            key = "높은 승률" if t.startswith("승률") else t
            buckets.setdefault(key, []).append(r["chul_no"])
    order = ["레이팅 1위", "레이팅 상위", "높은 승률", "최근 3전 호조", "단독 선행",
             "전개 수혜", "최경량", "기수 호조", "첫 출전", "장기 휴양 후"]
    out = []
    for k in order:
        if buckets.get(k):
            out.append({"label": k, "gates": buckets[k][:6]})
    return out


def load_metrics(path: Path = Path("models/metrics.json")) -> Dict:
    """학습·검증 규모를 화면에 그대로 노출하기 위한 지표.

    시중 예상지는 표본 크기도, 검증 방법도, 빗나간 예상도 공개하지 않는다.
    그것을 공개하는 것 자체가 이 사이트의 차별점이므로, 근거 수치는 숨기지 않고
    첫 화면에 올린다.
    """
    from .features import FEATURE_COLUMNS

    out = {"n_features": len(FEATURE_COLUMNS)}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out

    wf = raw.get("walk_forward") or {}
    out.update({
        "trained_races": raw.get("trained_races"),
        "trained_rows": raw.get("trained_rows"),
        "verified_races": wf.get("n_races"),
        "hit_win": wf.get("top1_win"),
        "hit_place": wf.get("top1_top3"),
        "mkt_hit_win": wf.get("mkt_top1_win"),
        "mkt_hit_place": wf.get("mkt_top1_top3"),
    })
    if out.get("hit_win") is not None and out.get("mkt_hit_win") is not None:
        out["edge_win"] = out["hit_win"] - out["mkt_hit_win"]
    if out.get("hit_place") is not None and out.get("mkt_hit_place") is not None:
        out["edge_place"] = out["hit_place"] - out["mkt_hit_place"]

    # 신뢰도 등급별 실적 — tools/calibrate_confidence.py 가 기록한 값.
    # 화면 수치는 재현 가능한 산출물에서만 가져온다.
    # 기호별 실제 착순 — tools/calibrate_marks.py 가 기록한 값
    marks = raw.get("marks") or {}
    out["mark_n_races"] = marks.get("n_races")
    out["mark_levels"] = marks.get("levels") or []

    conf = raw.get("confidence") or {}
    out["conf_n_races"] = conf.get("n_races")
    out["conf_tiers"] = conf.get("tiers") or []
    for tier in conf.get("tiers", []):
        if tier.get("label") == "강승부":
            out["strong_hit_win"] = tier.get("hit_win")
            out["strong_hit_place"] = tier.get("hit_place")
            out["strong_share"] = tier.get("share")
    return out


def _post_order(race: Dict) -> tuple:
    """발주 시각 기준 정렬 키. 시각이 없으면 그 날의 맨 뒤로 보낸다."""
    return (race["rc_date"] or "", race.get("post_time") or "99:99",
            race.get("rc_no") or 0, race.get("meet") or "")


def load_races(conn: sqlite3.Connection) -> List[Dict]:
    # 발주 시각 순. 경마장별로 묶으면 방문자는 '지금 곧 뛰는 경주'를 찾으려고
    # 두 덩어리를 번갈아 훑어야 한다. 경마 팬이 목록에서 하는 일은 경마장 고르기가
    # 아니라 시간표 읽기다. 발주시각이 비어 있으면 경주번호로 대신한다.
    rows = conn.execute(
        RACE_LIST_SQL
        + " ORDER BY r.rc_date DESC, COALESCE(NULLIF(r.post_time,''), '99:99'),"
          " r.rc_no, r.meet"
    ).fetchall()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        d["slug"] = race_slug(d["race_key"])
        d["url"] = f"/race/{d['slug']}/"
        try:
            date = dt.date.fromisoformat(d["rc_date"])
            d["weekday"] = WEEKDAY_KO[date.weekday()]
            d["date_obj"] = date
        except (TypeError, ValueError):
            d["weekday"] = ""
            d["date_obj"] = None
        out.append(d)
    return out


def load_runners(conn: sqlite3.Connection, race_key: str) -> List[Dict]:
    rows = conn.execute(DETAIL_SQL, (race_key,)).fetchall()
    runners = []
    for r in rows:
        d = _row_to_dict(r)
        starts = d.get("career_starts") or 0
        wins = d.get("career_1st") or 0
        d["career_text"] = (
            f"{starts}전 {wins}승" if starts else "-"
        )
        d["win_rate_text"] = f"{wins / starts:.0%}" if starts else "-"
        d["p_win_pct"] = round((d.get("p_win") or 0) * 100, 1)
        d["p_place_pct"] = round((d.get("p_place") or 0) * 100, 1)
        d["style_label"] = STYLE_LABEL.get(d.get("style_code") or "", "")
        try:
            d["tag_list"] = json.loads(d.get("tags") or "[]")
        except (ValueError, TypeError):
            d["tag_list"] = []
        runners.append(d)
    assign_marks(runners)
    return runners


# 마필 한 마리의 과거 출주 이력. 예상지의 개별 마필 블록에 해당한다.
FORM_SQL = """
SELECT r.rc_date, r.meet, r.rc_no, r.distance, r.grade, r.track_cond, r.weather,
       r.field_size, r.race_key,
       res.ord, res.chul_no, res.burden, res.jk_name, res.horse_weight,
       res.record_sec, res.win_odds, res.margin, res.rating,
       res.s1f_rank, res.c4_rank
FROM results res
JOIN races r ON r.race_key = res.race_key
WHERE res.hr_no = ? AND r.rc_date < ?
ORDER BY r.rc_date DESC
LIMIT ?
"""


def fmt_time(sec: Optional[float]) -> str:
    """경주기록 초 → '1:12.4' 표기. 경마 기록은 이 형식으로만 읽힌다."""
    if not sec or sec != sec or sec <= 0:
        return "-"
    m, s = divmod(float(sec), 60)
    return f"{int(m)}:{s:04.1f}" if m else f"{s:.1f}"


def load_form(conn: sqlite3.Connection, hr_no: str, as_of: str,
              limit: int = 6) -> List[Dict]:
    """as_of 이전의 출주 이력만 돌려준다.

    as_of 를 두는 이유는 예측 동결과 같다 — 지난 경주 페이지를 열었을 때
    '그 경주 이후'의 성적이 전적표에 섞여 보이면, 예상 당시에는 알 수 없던
    정보를 근거처럼 보여주는 셈이 된다.
    """
    rows = conn.execute(FORM_SQL, (hr_no, as_of, limit)).fetchall()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        fs = d.get("field_size") or 0
        ordn = d.get("ord")
        # 91~99 는 착순이 아니라 상태(실격·출전취소·경주취소 등)다.
        d["ord_status"] = ORD_STATUS.get(ordn or 0, "")
        d["ord_text"] = (d["ord_status"] or f"{ordn}착") if ordn else "-"
        d["field_text"] = f"{fs}두" if fs else ""
        d["record_text"] = fmt_time(d.get("record_sec"))
        d["slug"] = race_slug(d["race_key"])
        d["url"] = f"/race/{d['slug']}/"
        d["is_win"] = ordn == 1
        d["is_top3"] = bool(ordn and ordn <= 3)
        d["ran"] = bool(ordn and ordn <= MAX_ORD)
        # 초반 위치를 한눈에 — 각질 판정의 근거를 그대로 노출한다
        s1f = d.get("s1f_rank")
        d["s1f_text"] = f"{s1f}" if s1f else "-"
        out.append(d)
    return out


CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def circled(n: Optional[int]) -> str:
    """착순을 원문자로. 예상지에서 전적을 한 줄로 압축하는 관습적 표기다.

    91~99 는 착순이 아니라 상태 코드이므로 숫자로 찍지 않는다 — '99' 가 줄에
    섞이면 그 말이 99착을 한 것처럼 읽힌다. 착순이 없었다는 표시로 대신한다.
    """
    if not n or n < 1:
        return "·"
    if n > MAX_ORD:
        return "×"
    return CIRCLED[n - 1] if n <= len(CIRCLED) else str(n)


def ord_string(form: List[Dict], n: int = 6) -> str:
    """최근 착순을 '③⑤①⑦②' 한 줄로. 최근 것이 왼쪽."""
    return "".join(circled(f.get("ord")) for f in form[:n])


# 직전 경주의 착순표. 예상지 개별 마필 블록의 핵심이 이것이다 —
# 몇 착을 했는가보다 '누구에게 몇 마신 차로 졌는가'가 실력을 말해 준다.
RESULT_LINE_SQL = """
SELECT res.hr_no, res.hr_name, res.ord, res.margin, res.record_sec,
       res.jk_name, res.burden
FROM results res
WHERE res.race_key = ? AND res.ord IS NOT NULL
ORDER BY res.ord
LIMIT ?
"""


def load_result_lines(conn: sqlite3.Connection, race_key: str, focus_hr: str,
                      limit: int = 8) -> List[Dict]:
    rows = conn.execute(RESULT_LINE_SQL, (race_key, limit)).fetchall()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        d["record_text"] = fmt_time(d.get("record_sec"))
        d["is_focus"] = d.get("hr_no") == focus_hr
        out.append(d)
    return out


PERSON_SQL = """
SELECT COUNT(*) n,
       SUM(CASE WHEN res.ord = 1 THEN 1 ELSE 0 END) wins,
       SUM(CASE WHEN res.ord <= 3 THEN 1 ELSE 0 END) top3
FROM results res
JOIN races r ON r.race_key = res.race_key
WHERE res.{col} = ? AND r.rc_date < ? AND r.rc_date >= ? AND res.ord IS NOT NULL
"""


def load_person_stats(conn: sqlite3.Connection, col: str, key: Optional[str],
                      as_of: str) -> Dict:
    """기수·조교사 성적을 최근 1년과 통산 두 구간으로.

    예상지가 '100R / 1년 / 통산' 세 구간을 나란히 싣는 이유는, 통산 성적만으로는
    지금 물이 올랐는지 식었는지 알 수 없기 때문이다.
    """
    if not key:
        return {}
    out: Dict[str, Dict] = {}
    year_ago = (dt.date.fromisoformat(as_of) - dt.timedelta(days=365)).isoformat() \
        if as_of and as_of[0].isdigit() else "0000-00-00"
    for label, since in (("y1", year_ago), ("all", "0000-00-00")):
        row = conn.execute(PERSON_SQL.format(col=col), (key, as_of, since)).fetchone()
        n = row["n"] or 0
        out[label] = {
            "n": n,
            "wins": row["wins"] or 0,
            "top3": row["top3"] or 0,
            "win_rate": (row["wins"] or 0) / n if n else None,
            "top3_rate": (row["top3"] or 0) / n if n else None,
        }
    return out


def form_summary(form: List[Dict]) -> Dict:
    """전적표 아래 붙는 요약 — 최근 N전 성적 한 줄.

    실격·출전취소처럼 착순이 남지 않은 출주는 '몇 전'에서 뺀다. 넣으면 분모만
    커져 승률이 실제보다 낮게 보인다.
    """
    done = [f for f in form if f.get("ran")]
    if not done:
        return {}
    return {
        "n": len(done),
        "wins": sum(1 for f in done if f["ord"] == 1),
        "top3": sum(1 for f in done if f["ord"] <= 3),
        "best_odds": max((f["win_odds"] for f in done
                          if f.get("win_odds") and f["ord"] == 1), default=None),
    }


PICKS_SQL = """
SELECT p.race_key, p.pred_rank, p.p_win, p.p_place, p.p_top2, p.style_code,
       e.chul_no, e.hr_name, res.ord
FROM predictions p
JOIN entries e ON e.race_key = p.race_key AND e.hr_no = p.hr_no
LEFT JOIN results res ON res.race_key = p.race_key AND res.hr_no = p.hr_no
WHERE p.pred_rank <= 5
"""


def load_picks(conn: sqlite3.Connection) -> Dict[str, List[Dict]]:
    """경주별 상위 5두. 목록 화면에서 '클릭하지 않고도' 예상을 읽게 하려는 것.

    5두인 이유는 국내 예상지가 그렇게 싣기 때문이다. 3두만 내밀면 마권을 짤 때
    한 번 더 눌러 들어가야 하고, 그 순간 예상지 구실을 못 한다.

    경마 팬이 목록에서 가장 먼저 알고 싶은 건 거리나 두수가 아니라
    '이 경주는 뭘 미는가'다. 그 답을 목록에 바로 올린다.
    """
    out: Dict[str, List[Dict]] = {}
    for r in conn.execute(PICKS_SQL + " ORDER BY p.race_key, p.pred_rank"):
        d = _row_to_dict(r)
        d["p_win_pct"] = round((d.get("p_win") or 0) * 100)
        d["style_label"] = STYLE_LABEL.get(d.get("style_code") or "", "")
        # 시행이 끝난 경주는 승률 대신 실제 착순을 보여 준다.
        # 91~99 는 착순이 아니라 상태 코드(출전취소 등)다.
        o = d.get("ord")
        d["ord_text"] = (ORD_STATUS.get(o) or f"{o}착") if o else ""
        d["is_win"] = o == 1
        out.setdefault(d["race_key"], []).append(d)
    # 기호는 경주 단위로 정해진다 — 한 마리만 보고는 붙일 수 없다
    for picks in out.values():
        assign_marks(picks)
    return out


OUTCOME_SQL = """
SELECT p.race_key, p.pred_rank, p.hr_no, p.p_win, p.p_place, p.p_top2,
       e.hr_name, e.chul_no,
       res.ord, res.win_odds, res.record_sec,
       CASE WHEN c.hr_no IS NOT NULL THEN 1 ELSE 0 END AS cancelled
FROM predictions p
JOIN entries e ON e.race_key = p.race_key AND e.hr_no = p.hr_no
LEFT JOIN results res ON res.race_key = p.race_key AND res.hr_no = p.hr_no
LEFT JOIN cancellations c ON c.race_key = p.race_key AND c.hr_no = p.hr_no
JOIN races r ON r.race_key = p.race_key
WHERE COALESCE(r.has_result, 0) = 1
ORDER BY p.race_key, p.pred_rank
"""


def load_outcomes(conn: sqlite3.Connection,
                  div: Optional[Dict] = None) -> Dict[str, Dict]:
    """시행이 끝난 경주의 '예상 vs 실제'.

    출주 취소마는 예상에서 빼고 나머지로 순위를 다시 매긴다. 취소는 발주 직전에
    결정되므로 예상 시점에 알 수 없었고, 그걸 실패로 세면 우리가 통제할 수 없는
    사유로 성적이 깎인다. verify.py 의 집계와 같은 규칙이다.
    """
    grouped: Dict[str, List[Dict]] = {}
    for r in conn.execute(OUTCOME_SQL):
        grouped.setdefault(r["race_key"], []).append(_row_to_dict(r))

    out: Dict[str, Dict] = {}
    for key, rows in grouped.items():
        cancelled = [r for r in rows if r["cancelled"]]
        live = [r for r in rows if not r["cancelled"]]
        if not live:
            continue
        # 취소마를 뺀 상태에서 예상 순위와 기호를 다시 부여.
        # 기호는 순위가 아니라 착순 확률 수준에서 나오므로, 남은 말들 기준으로
        # 다시 매겨야 화면과 판정이 어긋나지 않는다.
        live.sort(key=lambda r: r["pred_rank"])
        for i, r in enumerate(live, 1):
            r["adj_rank"] = i
        for r in live:
            r["pred_rank"] = r["adj_rank"]
        assign_marks(live)

        top1 = live[0]
        ords = {r["adj_rank"]: r["ord"] for r in live[:3] if r["ord"]}
        winner = next((r for r in live if r["ord"] == 1), None)
        has_ord = any(r["ord"] for r in live)

        out[key] = {
            # 화면에 나간 추천은 다섯 두다. 셋만 넘기면 결과 목록이
            # 1순위 하나로만 판정하는 것처럼 보인다.
            "picks": live[:5],
            "cancelled": cancelled,
            "top1": top1,
            "top1_ord": top1["ord"],
            "top1_odds": top1["win_odds"],
            "winner_pick": winner,          # 1착마를 우리가 몇 순위로 봤나
            "hit_win": bool(top1["ord"] == 1),
            "hit_place": bool(top1["ord"] and top1["ord"] <= 3),
            "hit_top3": bool(1 in ords.values()),
            "settled": has_ord,
        }
        # 승식별 판정. 1순위의 착순 하나로 성패를 적으면 이 예상이 실제로
        # 쓸모 있었는지 드러나지 않는다 — 1순위가 2착이어도 복승·삼복승은
        # 적중일 수 있다. 배당 자료가 없는 경주는 판정하지 않는다.
        table = (div or {}).get(key)
        if table:
            bets = race_bets(live, table)
            out[key]["hit_bets"] = [b["name"] for b in bets if b["hit"]]
            out[key]["n_bets"] = len(bets)
        else:
            out[key]["hit_bets"] = []
            out[key]["n_bets"] = 0
    return out


def race_bets(runners: List[Dict], div_table: Dict) -> List[Dict]:
    """이 경주에서 우리 추천이 승식별로 어떻게 됐는지.

    배당표에는 적중 조합만 담겨 있으므로, 우리 조합이 거기 있으면 적중이고
    배당이 곧 회수액이다. 착순 배지만으로는 '이 경주에서 삼복승까지 맞았다'는
    사실이 드러나지 않는다.
    """
    gates = [r.get("chul_no") for r in sorted(runners, key=lambda r: r.get("pred_rank") or 99)[:5]]
    gates = [int(g) for g in gates if g]
    out = []
    for name, (pool, mine) in _combos(gates).items():
        table = div_table.get(pool, {})
        paid = sum(table.get(c) or 0.0 for c in mine)
        out.append({
            "name": name, "pool": POOL_LABEL.get(pool, pool),
            "combo": " / ".join(mine) if len(mine) <= 3 else f"{len(mine)}조합",
            "tickets": len(mine), "hit": paid > 0, "payout": paid,
            "roi": paid / len(mine) if mine else None,
        })
    return out


def load_simulation(conn: sqlite3.Connection, race_key: str) -> Optional[Dict]:
    """미리보기 대본과 신뢰도. 예측과 함께 동결된 값을 그대로 읽는다."""
    row = conn.execute(
        "SELECT payload, conf_score, conf_label, conf_desc, n_sims "
        "FROM simulations WHERE race_key = ?", (race_key,)).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row["payload"])
    except (ValueError, TypeError):
        return None
    if not payload.get("runners"):
        return None
    return {
        "payload": payload,
        "payload_json": row["payload"],
        "score": row["conf_score"],
        "label": row["conf_label"],
        "desc": row["conf_desc"],
        "n_sims": row["n_sims"],
    }


def load_commentary(conn: sqlite3.Connection, race_key: str) -> Optional[Dict]:
    row = conn.execute(
        "SELECT headline, body, model, created_at FROM commentaries WHERE race_key=?",
        (race_key,),
    ).fetchone()
    return _row_to_dict(row) if row else None


# ---------------------------------------------------------------------------
# 렌더링
# ---------------------------------------------------------------------------

def make_env(template_dir: Path) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["pct"] = lambda v: f"{v:.0%}" if isinstance(v, (int, float)) else "-"
    env.filters["comma"] = lambda v: f"{int(v):,}" if v not in (None, "") else "-"
    return env


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def build(db: str, out_dir: Path, config: Dict, template_dir: Path,
          static_dir: Path) -> Dict[str, int]:
    set_min_sample(config.get("build", {}).get("min_sample"))
    env = make_env(template_dir)
    # 이전 빌드의 잔여 페이지를 지운다. 남겨 두면 삭제된 경주가 사이트에 계속
    # 살아 있고, sitemap 과 실제 페이지가 어긋난다.
    # about 은 페이지를 없앤 뒤에도 이전 빌드 산출물이 남아 있으면 계속 살아 있다
    for stale in ("race", "horse", "about"):
        if (out_dir / stale).exists():
            shutil.rmtree(out_dir / stale)
    out_dir.mkdir(parents=True, exist_ok=True)

    with session(db) as conn:
        races = load_races(conn)
        accuracy = build_report(conn)
        # 성과 카드는 경주 상세로 이어져야 한다. 슬러그 규칙은 한 곳에 두고
        # 템플릿에는 완성된 주소만 넘긴다.
        for group in accuracy.get("highlights", {}).values():
            for h in group:
                h["url"] = f"/race/{race_slug(h['race_key'])}/"
        picks = load_picks(conn)
        dividends = load_dividends(conn)
        outcomes = load_outcomes(conn, dividends)
        metrics = load_metrics()

        today = today_kst()
        # 오늘 이후 경주는 이미 시행된 것도 그날 목록에 함께 싣는다.
        # 끝난 경주를 아래로 빼면 그날 카드가 앞뒤로 갈려, 방문자가 시간표를
        # 이어서 볼 수 없다. 시행된 경주는 승률 대신 착순이 찍힌다.
        upcoming = [r for r in races if r["date_obj"] and r["date_obj"] >= today]
        upcoming.sort(key=_post_order)
        upcoming = upcoming[: config["build"]["upcoming_limit"]]

        # 지난 경주는 최근 것이 위로 — 같은 날 안에서도 늦게 뛴 경주가 먼저다
        past = sorted((r for r in races if r["has_result"]),
                      key=_post_order, reverse=True)[: config["build"]["past_races"]]
        # 홈 하단에는 '어제 이전' 만 남긴다 (오늘 것은 위 목록에 이미 있다)
        recent_past = [r for r in past if r["date_obj"] and r["date_obj"] < today]

        # 정적 파일 주소에 붙일 내용 해시.
        #
        # 없으면 브라우저가 예전 track.js·style.css 를 계속 쓴다. 실제로 레인
        # 간격을 고쳐 배포하고도 화면이 그대로여서 한참 딴 데를 짚었다.
        assets = {}
        for f in ("track.js", "style.css", "clock.js"):
            src = static_dir / f
            if src.exists():
                assets[f] = hashlib.md5(src.read_bytes()).hexdigest()[:8]

        ctx_base = {
            "assets": assets,
            "site": config["site"],
            "adsense": config["adsense"],
            "analytics": config["analytics"],
            "accuracy": accuracy,
            "metrics": metrics,
            "build_time": now_kst().strftime("%Y-%m-%d %H:%M"),
            # 화면 표기는 사람이 읽기 좋게, datetime 속성은 기계가 읽도록 나눈다.
            "build_time_short": now_kst().strftime("%m월 %d일 %H:%M"),
            "build_iso": now_kst().strftime("%Y-%m-%dT%H:%M:00+09:00"),
            "today": today.isoformat(),
        }

        # 개별 경주 페이지
        detail_pages = []
        for r in upcoming + past:
            r["outcome"] = outcomes.get(r["race_key"])
            runners = load_runners(conn, r["race_key"])
            if not runners:
                continue
            commentary = load_commentary(conn, r["race_key"])
            sim = load_simulation(conn, r["race_key"])
            bets = (race_bets(runners, dividends.get(r["race_key"], {}))
                    if r["has_result"] else [])
            for run in runners:
                run["form"] = load_form(conn, run["hr_no"], r["rc_date"])
                run["form_summary"] = form_summary(run["form"])
                run["ord_string"] = ord_string(run["form"])
                run["horse_url"] = f"/horse/{run['hr_no']}/"
                # 직전 경주 착순표 — 그 성적이 어떤 상대에게서 나온 것인지 보여준다
                run["last_race"] = run["form"][0] if run["form"] else None
                run["last_lines"] = (
                    load_result_lines(conn, run["form"][0]["race_key"], run["hr_no"])
                    if run["form"] else []
                )
                run["jk_stats"] = load_person_stats(conn, "jk_no", run.get("jk_no"), r["rc_date"])
                run["tr_stats"] = load_person_stats(conn, "tr_no", run.get("tr_no"), r["rc_date"])
            html = env.get_template("race.html").render(
                **ctx_base, race=r, runners=runners, commentary=commentary,
                sim=sim, bets=bets,
                combos=betting_combos(runners),
                focus=focus_points(runners),
                pace=pace_map(runners), styles=STYLES,
                page_url=r["url"],
            )
            write(out_dir / "race" / r["slug"] / "index.html", html)
            detail_pages.append(r)

        # 목록/요약 페이지
        for r in upcoming + past:
            r["picks"] = picks.get(r["race_key"], [])
            r["outcome"] = outcomes.get(r["race_key"])
            # 당일 경주는 결과를 그리지 않는다. 착순은 경주마다 들어오지만
            # 배당은 그날 밤에 한 번 받으므로, 낮 동안 '착순은 있는데 적중은
            # 모름' 이라는 어중간한 화면이 된다. 게재한 예상 그대로 두고 밤에
            # 한 번 갱신한다.
            r["show_result"] = bool(r["has_result"]
                                    and r["date_obj"] and r["date_obj"] < today)

        # 추천경주 — 시뮬레이션이 결과를 좁게 몰아준 경주만 따로 세운다.
        # 매 경주를 똑같은 자신감으로 내미는 예상지는 신뢰를 얻지 못한다.
        # 이미 뛴 경주는 뺀다. '추천'인데 결과가 나와 있으면 볼 이유가 없다.
        featured = [r for r in upcoming
                    if r.get("conf_label") == "강승부" and not r.get("has_result")]
        featured.sort(key=lambda r: -(r.get("conf_score") or 0))

        by_day: Dict[str, List[Dict]] = {}
        for r in upcoming:
            by_day.setdefault(r["rc_date"], []).append(r)

        write(out_dir / "index.html", env.get_template("index.html").render(
            **ctx_base, upcoming_by_day=sorted(by_day.items()),
            featured=featured[:4],
            recent_past=recent_past[:12], page_url="/",
        ))
        write(out_dir / "accuracy" / "index.html",
              env.get_template("accuracy.html").render(**ctx_base, page_url="/accuracy/"))
        write(out_dir / "results" / "index.html",
              env.get_template("results.html").render(**ctx_base, past=past,
                                                      page_url="/results/"))
        # 예측 방식 페이지는 내렸다. 어떤 지표를 어떻게 조합하는지가 그대로
        # 적혀 있어 경쟁 예상지에 그대로 넘어간다. 산출 기준의 요지는 홈의
        # '예상 산출 기준' 과 적중률 페이지의 검증 표에 남아 있다.

        # 마필 개별 페이지 — 전적 조회는 경마 팬의 기본 동선이고,
        # '마명 + 전적' 검색어는 롱테일 유입의 큰 축이다.
        horse_rows = conn.execute(
            "SELECT DISTINCT e.hr_no, e.hr_name FROM entries e "
            "WHERE EXISTS (SELECT 1 FROM predictions p WHERE p.race_key = e.race_key)"
        ).fetchall()
        horse_pages = []
        limit = config["build"].get("horse_form_limit", 20)
        for hr in horse_rows:
            form = load_form(conn, hr["hr_no"], "9999-12-31", limit)
            if not form:
                continue
            prof = conn.execute(
                "SELECT hr_name, sex, age, origin, tr_name, ow_name, rating "
                "FROM entries WHERE hr_no = ? ORDER BY rowid DESC LIMIT 1",
                (hr["hr_no"],),
            ).fetchone()
            horse = {**_row_to_dict(prof), "hr_no": hr["hr_no"],
                     "url": f"/horse/{hr['hr_no']}/"}
            write(out_dir / "horse" / hr["hr_no"] / "index.html",
                  env.get_template("horse.html").render(
                      **ctx_base, horse=horse, form=form,
                      summary=form_summary(form), page_url=horse["url"]))
            horse_pages.append(horse)

        # 클라이언트 측 검색/필터용 인덱스
        write(out_dir / "races.json", json.dumps([
            {"k": r["race_key"], "u": r["url"], "m": r["meet"], "d": r["rc_date"],
             "n": r["rc_no"], "g": r["grade"], "dist": r["distance"]}
            for r in detail_pages
        ], ensure_ascii=False))

        # 갱신 상태 표식. 10분 정산을 걷어내면서 쓰는 곳이 없어졌지만, 배포된
        # 사이트가 어느 날짜까지 반영됐는지 밖에서 확인할 수 있어 남겨 둔다.
        today_races = [r for r in races if r["date_obj"] == today]
        write(out_dir / "settle.json", json.dumps({
            "date": today.isoformat(),
            "total": len(today_races),
            "settled": sum(1 for r in today_races if r["has_result"]),
            "built_at": now_kst().isoformat(timespec="seconds"),
        }, ensure_ascii=False))

        # sitemap / robots
        base = config["site"]["url"].rstrip("/")
        urls = (["/", "/accuracy/", "/results/"]
                + [r["url"] for r in detail_pages]
                + [h["url"] for h in horse_pages])
        write(out_dir / "sitemap.xml", env.get_template("sitemap.xml").render(
            base=base, urls=urls, lastmod=today.isoformat()))
        # 커스텀 도메인용 CNAME.
        #
        # Actions 로 배포할 때는 저장소 Pages 설정이 우선이고 이 파일은 참고에
        # 그친다(설정은 API 로 한 번 지정해 두었다). 다만 브랜치 배포로 바꾸거나
        # 다른 정적 호스팅으로 옮기면 이 파일이 도메인을 유지하는 유일한 근거가
        # 되므로, 산출물에 항상 포함시켜 둔다.
        host = urlparse(config["site"]["url"]).hostname or ""
        if host and not host.endswith("github.io"):
            write(out_dir / "CNAME", host + "\n")

        write(out_dir / "robots.txt",
              f"User-agent: *\nAllow: /\n\nSitemap: {base}/sitemap.xml\n")

        # ads.txt — 이 도메인의 광고를 누가 팔 수 있는지 밝히는 파일.
        # 없으면 구글이 '승인되지 않은 판매자' 로 보고 광고 게재를 제한한다.
        # 태그를 아무리 정확히 넣어도 여기가 없으면 빈 자리만 남는다.
        pub = str(config["adsense"].get("client") or "")
        if pub:
            write(out_dir / "ads.txt",
                  f"google.com, {pub.replace('ca-', '')}, DIRECT, f08c47fec0942fa0\n")

    if static_dir.exists():
        shutil.copytree(static_dir, out_dir / "static", dirs_exist_ok=True)

    return {"races": len(detail_pages), "upcoming": len(upcoming),
            "past": len(past), "horses": len(horse_pages)}


def load_config(path: Path) -> Dict:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg.setdefault("adsense", {})
    cfg.setdefault("analytics", {})
    cfg.setdefault("build", {})
    cfg["build"].setdefault("past_races", 300)
    cfg["build"].setdefault("upcoming_limit", 120)
    return cfg


def main(argv: Optional[List[str]] = None) -> int:
    root = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(description="정적 사이트 빌드")
    ap.add_argument("--db", default="data/horseai.sqlite")
    ap.add_argument("--out", default="dist")
    ap.add_argument("--config", default=str(root / "config.yaml"))
    ap.add_argument("--templates", default=str(root / "templates"))
    ap.add_argument("--static", default=str(root / "static"))
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = load_config(Path(args.config))
    stats = build(args.db, Path(args.out), cfg, Path(args.templates), Path(args.static))
    print(f"빌드 완료 → {args.out}")
    print(f"  경주 상세 {stats['races']}p (다가올 {stats['upcoming']} / 지난 {stats['past']})")
    print(f"  마필 페이지 {stats['horses']}p")
    if cfg["site"]["url"].startswith("https://example.com"):
        print("\n⚠ config.yaml 의 site.url 이 아직 example.com 입니다. "
              "배포 전 실제 도메인으로 바꾸세요 (canonical·sitemap에 영향).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
