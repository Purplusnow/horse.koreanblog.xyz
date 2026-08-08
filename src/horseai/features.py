"""피처 엔지니어링.

설계 원칙 — **경주 시작 전에 알 수 있는 정보만 쓴다.**

이 프로젝트에서 가장 쉽게 저지르는 실수가 데이터 누수다. 착순·경주기록·마체중
(당일 계측)·배당률은 전부 경주 이후(또는 직전)에 확정되는 값이라 피처로 쓰면
백테스트 적중률만 비현실적으로 좋아지고 실전에서 무너진다. 그래서

  * 당일 값 중 피처로 쓰는 것: 부담중량, 레이팅, 연령, 성별, 출주번호, 두수,
    거리, 등급 — 모두 출전표에 실려 경주 전에 공개된다.
  * 과거 성적은 ``rc_date`` 오름차순 정렬 후 ``shift(1)`` 기반으로만 집계한다.
    같은 날 앞 경주의 결과는 뒤 경주 시점에 실제로 알 수 있으므로 허용한다.
  * 배당률(``win_odds``)은 **피처에서 제외**하고 평가 단계에서만 쓴다. 시장을
    복제하는 모델이 아니라 시장과 비교 가능한 모델을 만들기 위해서다.

학습은 ``results`` 테이블(과거 경주)로, 추론은 ``entries`` 테이블(출전표)로 하되
두 경로가 **완전히 같은 피처 컬럼**을 만들도록 맞춘다.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from . import style
from .kra.normalize import MAX_ORD, ORD_RAN

log = logging.getLogger(__name__)

# 경주 안에서 '몇 번째로 좋은가'로 바꿔 볼 피처들.
# 경마는 절대 능력이 아니라 그날 그 경주에 모인 상대와의 비교로 결정된다.
# 승률 12%는 국1군에서는 평범하고 국6군에서는 최상위인데, 절대값만 주면 모델이
# 그 맥락을 매번 다른 조합의 다른 피처들로부터 재구성해야 한다.
RELATIVE_BASE = [
    "win_rate", "top3_rate", "place_rate",
    "speed_last", "speed_avg3", "speed_best",
    "abs_speed_last", "abs_speed_avg3", "abs_speed_best", "speed_sd5",
    "early_speed_avg3", "early_speed_best",
    "late_speed_avg3", "late_speed_best",
    "finish_speed_avg3", "finish_speed_best",
    "last_ord_pct", "avg_ord_pct_3", "avg_ord_pct_5",
    "jk_win_rate", "jk_top3_rate", "jk_recent_win_rate",
    "tr_win_rate", "tr_top3_rate",
    "dist_top3_rate", "starts_prior", "days_since_last",
    "rating", "burden", "class_move", "weight_change",
]
RELATIVE_FEATURES = [f"{c}_pct" for c in RELATIVE_BASE]

# 모델에 투입되는 피처 (학습/추론 공통)
FEATURE_COLUMNS: List[str] = [
    # 당일 공개 정보
    "distance", "field_size", "chul_no", "burden", "rating", "age", "is_male", "is_gelding",
    "gate_ratio", "burden_rel", "rating_rel", "rating_rank_pct", "is_rating_missing",
    "dist_bucket",
    # 마필 과거 이력
    "starts_prior", "win_rate", "place_rate", "top3_rate",
    "days_since_last", "last_ord_pct", "avg_ord_pct_3", "avg_ord_pct_5",
    # 조교(일별훈련) 피처는 **빼 두었다**. TRAINING_FEATURES 를 여기에 이어
    # 붙이면 바로 되살아난다.
    #
    # 왜 뺐나: 시간순 교차검증에서 넣으나 빼나 같았다(단승 -0.1%p, AUC -0.0007).
    # 개별 피처를 뜯어봐도 승률 상관이 0.01~0.03 이고, 마필 자신의 평소 대비
    # 편차로 바꿔도 0.04 를 넘지 못했다(쓸모 있는 피처는 0.15 이상이다).
    #
    # 원인은 자료의 성격에 있다. API18_1 이 주는 것은 조교 **시간·횟수·기승자**
    # 이고 **조교 시계(기록)** 가 없다. 마방과 시장이 실제로 보는 것은 얼마나
    # 오래 탔는지가 아니라 몇 초에 뛰었는지다. 그 숫자가 없으면 이 자료로는
    # 시장과의 격차를 좁힐 수 없다.
    #
    # 수집은 계속한다 — 하루치를 놓치면 되돌릴 수 없고, 조교 기록이 담긴 자료를
    # 나중에 확보하면 이 이력과 붙여 쓸 수 있다.
    "speed_last", "speed_avg3", "speed_best",
    "abs_speed_last", "abs_speed_avg3", "abs_speed_best", "speed_sd5",
    "early_speed_avg3", "early_speed_best",
    "late_speed_avg3", "late_speed_best",
    "finish_speed_avg3", "finish_speed_best",
    "dist_starts", "dist_top3_rate", "meet_starts",
    # exact_dist_starts · is_new_distance · dist_change 는 계산은 하되 넣지 않는다.
    #
    # 원자료에는 분명한 차이가 있다 — 거리 첫 도전이 승률 11.8% 대 8.8% 로 오히려
    # 높고, 250m 넘게 단축하면 6.9% 까지 떨어진다. 그런데 모델에 넣어도 성능이
    # 움직이지 않는다(단승 -0.3%p, AUC +0.0006). '1순위가 거리 첫 도전' 인 경주만
    # 골라 봐도 +0.3%p 로 잡음 수준이다.
    #
    # 이미 있는 피처(통산 출주 수·승률·class_move)가 같은 정보를 담고 있어서다.
    # 거리를 바꿔 나오는 말은 대개 올라가는 중이고, 그 사실은 승률과 승급 이력에
    # 이미 들어 있다.
    #
    # 계산은 남겨 둔다 — 화면에서 '이 거리 첫 도전' 을 알려 주는 데는 쓸 수 있고,
    # 되살리려면 여기에 이어 붙이면 된다.
    "last_weight", "weight_change",
    "class_move",
    # 기수 / 조교사
    "jk_starts", "jk_win_rate", "jk_top3_rate", "jk_recent_win_rate",
    "tr_starts", "tr_win_rate", "tr_top3_rate",
    "jk_hr_starts", "jk_hr_top3_rate",
] + style.FEATURES + RELATIVE_FEATURES

# 출전표의 공식 통산 성적(off_*)은 **피처로 쓰지 않는다.**
#
# 과거 날짜로 조회해도 API 가 '조회 시점의 현재 전적'을 돌려주기 때문이다.
# 한 마리의 모든 경주에서 career_starts 가 똑같은 값으로 나오는 것으로 확인했다
# (미스터카라 15전 → 15개 경주 전부 '15'). 2021년 경주 행에 2026년까지의 성적이
# 들어 있는 셈이라, 학습에 넣으면 단승 적중률이 52%까지 치솟는 명백한 미래 누수다.
#
# 다만 **다가올 경주**에서는 '현재 전적'이 곧 경주 직전의 참값이므로,
# 화면 표시용으로는 그대로 쓸 수 있다. add_official() 은 그 용도로 남겨 둔다.
OFFICIAL_DISPLAY_ONLY = [
    "off_starts", "off_win_rate", "off_top3_rate", "off_prize_per_start",
    "off_y1_starts", "off_y1_win_rate", "off_y1_top3_rate",
    "off_prize_1y", "off_prize_6m", "off_recent_share", "off_rest_days",
]

CATEGORICAL: List[str] = ["dist_bucket"]

# 등급 문자열 → 서열 (숫자가 클수록 상위 등급)
GRADE_ORDER = {
    "국6": 1, "국5": 2, "국4": 3, "국3": 4, "국2": 5, "국1": 6,
    "6": 1, "5": 2, "4": 3, "3": 4, "2": 5, "1": 6,
    "6군": 1, "5군": 2, "4군": 3, "3군": 4, "2군": 5, "1군": 6,
    "오픈": 7, "OPEN": 7, "대상": 8, "G3": 8, "G2": 9, "G1": 10,
}


def grade_rank(g: Optional[str]) -> float:
    if not g:
        return np.nan
    s = str(g).strip().upper().replace(" ", "")
    if s in GRADE_ORDER:
        return float(GRADE_ORDER[s])
    for k, v in GRADE_ORDER.items():
        if k.upper() in s:
            return float(v)
    return np.nan


# ---------------------------------------------------------------------------
# 원천 로딩
# ---------------------------------------------------------------------------

HISTORY_SQL = """
SELECT
    r.race_key, r.rc_date, r.meet, r.rc_no, r.distance, r.grade,
    r.field_size, r.track_cond, r.weather,
    res.hr_no, res.hr_name, res.chul_no, res.jk_no, res.tr_no,
    res.age, res.sex, res.origin, res.burden, res.rating,
    res.horse_weight, res.weight_delta, res.record_sec,
    res.ord, res.win_odds, res.place_odds,
    res.s1f_rank, res.g1f_rank, res.c1_rank, res.c2_rank, res.c3_rank, res.c4_rank,
    res.s1f_sec, res.g3f_sec, res.g1f_sec
FROM results res
JOIN races r ON r.race_key = res.race_key
WHERE r.rc_date IS NOT NULL
"""

ENTRY_SQL = """
SELECT
    r.race_key, r.rc_date, r.meet, r.rc_no, r.distance, r.grade,
    r.field_size, r.post_time, r.rc_name, r.age_cond, r.prize1,
    e.hr_no, e.hr_name, e.chul_no, e.jk_no, e.jk_name, e.tr_no, e.tr_name,
    e.ow_name, e.burden, e.rating, e.age, e.sex, e.origin,
    e.career_1st, e.career_2nd, e.career_3rd, e.career_starts,
    e.y1_1st, e.y1_2nd, e.y1_3rd, e.y1_starts, e.career_prize, e.prize_1y
FROM entries e
JOIN races r ON r.race_key = e.race_key
"""


def load_history(conn: sqlite3.Connection) -> pd.DataFrame:
    """과거 경주 결과 (학습 데이터이자 모든 이력 피처의 원천)."""
    df = pd.read_sql_query(HISTORY_SQL, conn)
    if df.empty:
        return df
    df["rc_date"] = pd.to_datetime(df["rc_date"], errors="coerce")
    df = df.dropna(subset=["rc_date", "hr_no"])
    # 연령/성별은 성적 테이블에 없을 수 있어 별도 조회로 보강
    return df.sort_values(["rc_date", "meet", "rc_no", "chul_no"]).reset_index(drop=True)


def load_entries(conn: sqlite3.Connection, race_keys: Optional[List[str]] = None) -> pd.DataFrame:
    sql = ENTRY_SQL
    params: tuple = ()
    if race_keys:
        sql += f" WHERE e.race_key IN ({','.join('?' * len(race_keys))})"
        params = tuple(race_keys)
    df = pd.read_sql_query(sql, conn, params=params)
    if df.empty:
        return df
    df["rc_date"] = pd.to_datetime(df["rc_date"], errors="coerce")
    return df.sort_values(["rc_date", "meet", "rc_no", "chul_no"]).reset_index(drop=True)



# ---------------------------------------------------------------------------
# 출전표 공식 지표
# ---------------------------------------------------------------------------

OFFICIAL_COLS = [
    "career_starts", "career_1st", "career_2nd", "career_3rd",
    "y1_starts", "y1_1st", "y1_2nd", "y1_3rd",
    "career_prize", "prize_1y", "prize_6m", "ilsu",
]


def add_official(df: pd.DataFrame) -> pd.DataFrame:
    """출전표의 공식 통산 성적을 파생 지표로 변환한다.

    우리 DB에서 역산한 이력은 백필 시작일 이후만 담긴다. 5년 전에 데뷔한 말은
    그 이전 전적이 통째로 빠지고, 전입마는 아예 신마처럼 보인다. 출전표는 KRA가
    집계한 **전 생애 기록**을 실어 주므로 그 공백을 메운다. 경주 전에 공개되는
    값이라 누수도 아니다.
    """
    df = df.copy()
    for c in OFFICIAL_COLS:
        if c not in df.columns:
            df[c] = np.nan
        df[c] = pd.to_numeric(df[c], errors="coerce")

    starts = df["career_starts"]
    denom = starts.where(starts > 0)
    df["off_starts"] = starts
    df["off_win_rate"] = df["career_1st"] / denom
    df["off_top3_rate"] = (df["career_1st"] + df["career_2nd"] + df["career_3rd"]) / denom
    df["off_prize_per_start"] = df["career_prize"] / denom

    y1 = df["y1_starts"]
    y1d = y1.where(y1 > 0)
    df["off_y1_starts"] = y1
    df["off_y1_win_rate"] = df["y1_1st"] / y1d
    df["off_y1_top3_rate"] = (df["y1_1st"] + df["y1_2nd"] + df["y1_3rd"]) / y1d

    df["off_prize_1y"] = df["prize_1y"]
    df["off_prize_6m"] = df["prize_6m"]
    # 통산 상금 중 최근 6개월 비중 — 지금 물이 올랐는지를 한 값으로 요약한다.
    cp = df["career_prize"].where(df["career_prize"] > 0)
    df["off_recent_share"] = (df["prize_6m"] / cp).clip(0, 1)
    df["off_rest_days"] = df["ilsu"]
    return df



# ---------------------------------------------------------------------------
# 스피드 지수 (거리·주로 보정 + 구간 분리)
# ---------------------------------------------------------------------------

_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def parse_moisture(v) -> float:
    """주로상태 '건조 (2%)' → 함수율 2.0.

    같은 1200m라도 함수율 2%의 건조 주로와 15%의 포화 주로는 기준 기록이
    통째로 다르다. 보정하지 않으면 비 온 날 뛴 말이 전부 느린 말로 기록된다.
    """
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return np.nan
    m = _PCT_RE.search(str(v))
    if m:
        return float(m.group(1))
    text = str(v)
    for kw, approx in (("건조", 3.0), ("양호", 8.0), ("다습", 13.0), ("포화", 18.0)):
        if kw in text:
            return approx
    return np.nan


def _par_z(df: pd.DataFrame, value: pd.Series, key: pd.Series) -> pd.Series:
    """조건이 같은 과거 경주들의 기준기록 대비 z점수 (빠를수록 +).

    반드시 shift(1) 로 **자기 경주 이전**의 기록만 써서 기준을 만든다.
    당일 기록이 기준에 섞이면 그 자체가 미래 정보다.
    """
    v = pd.to_numeric(value, errors="coerce")
    g = v.groupby(key)
    par = g.transform(lambda x: x.shift(1).expanding(min_periods=20).mean())
    sd = g.transform(lambda x: x.shift(1).expanding(min_periods=20).std())
    return (-(v - par) / sd.replace(0, np.nan)).clip(-4, 4)


# 구간 스피드: (표시명, 기록 컬럼, 구간 거리 m)
SECTIONS = [("early", "s1f_sec", 200), ("late", "g3f_sec", 600), ("finish", "g1f_sec", 200)]


def add_speed_figure(df: pd.DataFrame) -> pd.DataFrame:
    """세 가지 속도 관점을 함께 만든다.

    1) ``speed_fig``  — 같은 경주 안에서의 상대 우위 (기존)
    2) ``abs_speed``  — 거리·주로 조건이 같은 과거 경주 대비 절대 속도
    3) 구간 스피드     — 초반(S1F)·막판600(G3F)·최후200(G1F) 각각의 속도

    1번만 있으면 '느린 경주에서 1등한 말'과 '빠른 경주에서 1등한 말'이 구분되지
    않는다. 3번이 있어야 '초반이 빠른 말'과 '뒷심이 좋은 말'을 나눌 수 있다.
    """
    df = df.copy()

    # (1) 경주 내 상대 속도
    sec = pd.to_numeric(df.get("record_sec"), errors="coerce")
    sec = sec.where(sec > 0)
    g = sec.groupby(df["race_key"])
    mean, std = g.transform("mean"), g.transform("std")
    df["speed_fig"] = (-(sec - mean) / std.replace(0, np.nan)).clip(-3, 3)

    # (2) 조건 보정 절대 속도
    dist = pd.to_numeric(df.get("distance"), errors="coerce")
    df["moisture"] = df.get("track_cond").map(parse_moisture) if "track_cond" in df else np.nan
    cond = pd.cut(pd.to_numeric(df["moisture"], errors="coerce"),
                  bins=[-1, 5, 10, 15, 100], labels=["건조", "양호", "다습", "포화"])
    cond_key = (df["meet"].astype(str) + "|" + dist.astype("Int64").astype(str)
                + "|" + cond.astype(str))
    df["_cond_key"] = cond_key
    df["abs_speed"] = _par_z(df, sec / dist.replace(0, np.nan) * 200, cond_key)

    # (3) 구간별 속도
    for name, col, seg_m in SECTIONS:
        raw = pd.to_numeric(df.get(col), errors="coerce") if col in df else pd.Series(np.nan, index=df.index)
        df[f"{name}_speed"] = _par_z(df, raw / seg_m * 200, cond_key)
    return df

# ---------------------------------------------------------------------------
# 이력 피처 (shift 기반, 누수 없음)
# ---------------------------------------------------------------------------

def _expanding_prior(group_keys: pd.Series, values: pd.Series, how: str = "mean") -> pd.Series:
    """그룹별로 '현재 행 직전까지'의 확장 집계. shift(1) 로 자기 자신을 배제한다."""
    s = pd.to_numeric(values, errors="coerce")
    grouped = s.groupby(group_keys)
    if how == "mean":
        agg = grouped.transform(lambda x: x.shift(1).expanding().mean())
    elif how == "sum":
        agg = grouped.transform(lambda x: x.shift(1).expanding().sum())
    elif how == "max":
        agg = grouped.transform(lambda x: x.shift(1).expanding().max())
    elif how == "count":
        agg = grouped.transform(lambda x: x.shift(1).expanding().count())
    else:
        raise ValueError(how)
    return agg


def _rolling_prior(group_keys: pd.Series, values: pd.Series, window: int) -> pd.Series:
    s = pd.to_numeric(values, errors="coerce")
    return s.groupby(group_keys).transform(
        lambda x: x.shift(1).rolling(window, min_periods=1).mean()
    )


def build_history_index(hist: pd.DataFrame) -> pd.DataFrame:
    """과거 결과에 이력 집계 컬럼을 붙인다.

    반환 프레임은 (a) 학습용 피처 원천이자 (b) 추론 시 '마지막 상태' 조회용
    스냅샷 원천으로 함께 쓰인다.
    """
    df = add_speed_figure(hist)
    df = df.sort_values(["rc_date", "meet", "rc_no", "chul_no"]).reset_index(drop=True)

    fs = pd.to_numeric(df["field_size"], errors="coerce")
    ordn = pd.to_numeric(df["ord"], errors="coerce")
    # 91~99 는 착순이 아니라 상태 코드(실격·출전취소·경주취소 등)다. 순위로 쓰면
    # ord_pct 가 1.0(꼴찌)로 clip 되어, 달리지도 않은 말이 최악의 성적으로
    # 학습된다. 순위 지표에서는 통째로 빼고, 승패 레이블은 성격에 따라 나눈다.
    status = ordn.where(ordn >= 91)
    ordn = ordn.where(ordn <= MAX_ORD)          # 순위로 쓸 값만 남긴다
    # 달렸지만 성적이 없는 경우(실격·주행중지)는 패배로 센다. 나머지(출전취소·
    # 경주취소 등)는 경주 자체가 성립하지 않았으므로 레이블에서 제외한다.
    ran = status.isin(list(ORD_RAN))
    lost = ordn.notna() | ran
    # 두수가 비어 있으면 경주별 실제 출주 두수로 대체
    fs = fs.fillna(df.groupby("race_key")["hr_no"].transform("count"))
    df["field_size"] = fs
    df["ord_pct"] = ((ordn - 1) / (fs - 1).replace(0, np.nan)).clip(0, 1)
    df["is_win"] = (ordn == 1).astype(float).where(lost)
    df["is_top2"] = (ordn <= 2).astype(float).where(lost)
    df["is_top3"] = (ordn <= 3).astype(float).where(lost)
    df["is_place"] = (ordn <= 2).astype(float).where(lost)
    df["grade_rank"] = df["grade"].map(grade_rank)

    hr = df["hr_no"]
    df["starts_prior"] = _expanding_prior(hr, df["is_win"].notna().astype(float), "sum").fillna(0)
    df["win_rate"] = _expanding_prior(hr, df["is_win"], "mean")
    df["place_rate"] = _expanding_prior(hr, df["is_place"], "mean")
    df["top3_rate"] = _expanding_prior(hr, df["is_top3"], "mean")
    df["last_ord_pct"] = df.groupby(hr)["ord_pct"].shift(1)
    df["avg_ord_pct_3"] = _rolling_prior(hr, df["ord_pct"], 3)
    df["avg_ord_pct_5"] = _rolling_prior(hr, df["ord_pct"], 5)
    df["speed_last"] = df.groupby(hr)["speed_fig"].shift(1)
    df["speed_avg3"] = _rolling_prior(hr, df["speed_fig"], 3)
    df["speed_best"] = _expanding_prior(hr, df["speed_fig"], "max")
    # 조건 보정 절대 속도 — '어떤 경주에서 냈는가'까지 반영된 실력 추정
    df["abs_speed_last"] = df.groupby(hr)["abs_speed"].shift(1)
    df["abs_speed_avg3"] = _rolling_prior(hr, df["abs_speed"], 3)
    df["abs_speed_best"] = _expanding_prior(hr, df["abs_speed"], "max")
    # 기복 — 평균이 같아도 편차가 큰 말은 신뢰도가 낮다
    df["speed_sd5"] = df.groupby(hr)["abs_speed"].transform(
        lambda x: x.shift(1).rolling(5, min_periods=2).std())
    # 구간별 능력: 초반 스피드 / 막판 600m / 최후 200m 뒷심
    for name, _col, _m in SECTIONS:
        df[f"{name}_speed_avg3"] = _rolling_prior(hr, df[f"{name}_speed"], 3)
        df[f"{name}_speed_best"] = _expanding_prior(hr, df[f"{name}_speed"], "max")
    df["last_weight"] = df.groupby(hr)["horse_weight"].shift(1)
    prev_prev = df.groupby(hr)["horse_weight"].shift(2)
    df["weight_change"] = df["last_weight"] - prev_prev

    prev_date = df.groupby(hr)["rc_date"].shift(1)
    df["days_since_last"] = (df["rc_date"] - prev_date).dt.days

    prev_grade = df.groupby(hr)["grade_rank"].shift(1)
    df["class_move"] = df["grade_rank"] - prev_grade

    # 거리 적성 / 경마장 경험
    df["dist_bucket"] = pd.cut(
        pd.to_numeric(df["distance"], errors="coerce"),
        bins=[0, 1000, 1200, 1400, 1700, 2000, 10000],
        labels=["~1000", "1100-1200", "1300-1400", "1500-1700", "1800-2000", "2000+"],
    ).astype(str)
    hr_dist = df["hr_no"].astype(str) + "|" + df["dist_bucket"].astype(str)
    df["dist_starts"] = _expanding_prior(hr_dist, df["is_top3"].notna().astype(float), "sum").fillna(0)
    df["dist_top3_rate"] = _expanding_prior(hr_dist, df["is_top3"], "mean")
    # 구간이 아니라 **정확히 그 거리**를 뛴 적이 있는가.
    # 구간(1300-1400)으로 묶으면 1300m 7전이 1400m 경험으로 읽혀, '이 거리
    # 첫 도전' 이라는 사실이 통째로 가려진다. 시장이 조심하는 조건이므로
    # 모델도 볼 수 있어야 한다.
    #
    # 참고로 데이터상 첫 도전이 불리하지는 않다 — 승률 11.8% 대 8.8% 로 오히려
    # 높다. 거리를 바꾸는 말이 대개 올라가는 중이기 때문으로 보인다.
    hr_exact = df["hr_no"].astype(str) + "|" + df["distance"].astype(str)
    df["exact_dist_starts"] = _expanding_prior(
        hr_exact, df["is_top3"].notna().astype(float), "sum").fillna(0)
    df["is_new_distance"] = (df["exact_dist_starts"] == 0).astype(float)
    # 직전 경주 대비 거리 변화. 크게 단축하면 승률이 떨어진다(250m+ 단축 6.9%,
    # 연장 10.2%). 거리를 줄여 나오는 말은 제 거리를 못 찾은 경우가 많다.
    _prev_dist = df.groupby(hr)["distance"].shift(1)
    df["dist_change"] = pd.to_numeric(df["distance"], errors="coerce") - _prev_dist

    hr_meet = df["hr_no"].astype(str) + "|" + df["meet"].astype(str)
    df["meet_starts"] = _expanding_prior(hr_meet, df["is_top3"].notna().astype(float), "sum").fillna(0)

    # 기수 / 조교사
    jk = df["jk_no"].fillna("?")
    df["jk_starts"] = _expanding_prior(jk, df["is_win"].notna().astype(float), "sum").fillna(0)
    df["jk_win_rate"] = _expanding_prior(jk, df["is_win"], "mean")
    df["jk_top3_rate"] = _expanding_prior(jk, df["is_top3"], "mean")
    df["jk_recent_win_rate"] = _rolling_prior(jk, df["is_win"], 60)

    tr = df["tr_no"].fillna("?")
    df["tr_starts"] = _expanding_prior(tr, df["is_win"].notna().astype(float), "sum").fillna(0)
    df["tr_win_rate"] = _expanding_prior(tr, df["is_win"], "mean")
    df["tr_top3_rate"] = _expanding_prior(tr, df["is_top3"], "mean")

    # 기수-마필 콤비
    combo = df["jk_no"].fillna("?").astype(str) + "|" + df["hr_no"].astype(str)
    df["jk_hr_starts"] = _expanding_prior(combo, df["is_top3"].notna().astype(float), "sum").fillna(0)
    df["jk_hr_top3_rate"] = _expanding_prior(combo, df["is_top3"], "mean")

    # 각질(선행·선입·추입)과 경주 전개 맥락. 과거 구간순위에서만 집계한다.
    df = style.attach_history(df)
    df = style.add_race_context(df)

    return df


# ---------------------------------------------------------------------------
# 경주 내 상대 피처
# ---------------------------------------------------------------------------

def add_within_race(df: pd.DataFrame) -> pd.DataFrame:
    """같은 경주 안에서의 상대적 위치. 경마는 본질적으로 순위 문제다."""
    df = df.copy()
    fs = pd.to_numeric(df["field_size"], errors="coerce")
    fs = fs.fillna(df.groupby("race_key")["hr_no"].transform("count"))
    df["field_size"] = fs

    chul = pd.to_numeric(df["chul_no"], errors="coerce")
    df["chul_no"] = chul
    df["gate_ratio"] = (chul / fs.replace(0, np.nan)).clip(0, 1)

    burden = pd.to_numeric(df["burden"], errors="coerce")
    df["burden"] = burden
    df["burden_rel"] = burden - burden.groupby(df["race_key"]).transform("mean")

    rating = pd.to_numeric(df["rating"], errors="coerce")
    df["is_rating_missing"] = rating.isna().astype(int)
    df["rating"] = rating
    df["rating_rel"] = rating - rating.groupby(df["race_key"]).transform("mean")
    df["rating_rank_pct"] = rating.groupby(df["race_key"]).rank(pct=True, ascending=False)

    sex = df.get("sex", pd.Series(index=df.index, dtype=object)).astype(str)
    df["is_male"] = sex.str.contains("수", na=False).astype(int)
    df["is_gelding"] = sex.str.contains("거", na=False).astype(int)

    if "dist_bucket" not in df:
        df["dist_bucket"] = pd.cut(
            pd.to_numeric(df["distance"], errors="coerce"),
            bins=[0, 1000, 1200, 1400, 1700, 2000, 10000],
            labels=["~1000", "1100-1200", "1300-1400", "1500-1700", "1800-2000", "2000+"],
        ).astype(str)

    for c in ("distance", "age"):
        df[c] = pd.to_numeric(df.get(c), errors="coerce")
    return df


def add_relative(df: pd.DataFrame) -> pd.DataFrame:
    """각 피처를 경주 내 백분위로 변환해 나란히 둔다.

    절대값은 그대로 남겨 둔다 — 등급 자체가 담는 정보도 있기 때문이다.
    상대값을 함께 주면 모델이 '이 경주에서 상대적으로 어느 위치인가'를
    직접 쓸 수 있다.
    """
    df = df.copy()
    g = df.groupby("race_key")
    for c in RELATIVE_BASE:
        if c not in df.columns:
            df[f"{c}_pct"] = np.nan
            continue
        v = pd.to_numeric(df[c], errors="coerce")
        # 한 마리만 값이 있는 경주에서는 백분위가 의미 없으므로 비운다
        n_valid = v.groupby(df["race_key"]).transform("count")
        pct = v.groupby(df["race_key"]).rank(pct=True)
        df[f"{c}_pct"] = pct.where(n_valid >= 3)
    return df


def finalize(df: pd.DataFrame) -> pd.DataFrame:
    """FEATURE_COLUMNS 를 모두 갖춘 프레임으로 정리."""
    df = add_within_race(df)
    df = add_relative(df)
    for c in FEATURE_COLUMNS:
        if c not in df.columns:
            df[c] = np.nan
    for c in FEATURE_COLUMNS:
        if c not in CATEGORICAL:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["dist_bucket"] = df["dist_bucket"].astype(str).fillna("unknown")
    return df


TRAINING_FEATURES = [
    "trg_days_since", "trg_count_14", "trg_term_mean_14", "trg_jockey_14",
    "trg_run1_14", "trg_run2_14", "trg_entry_planned", "trg_term_trend",
]


def add_training(df: pd.DataFrame, conn: sqlite3.Connection) -> pd.DataFrame:
    """조교(일별훈련) 이력을 붙인다.

    공개 데이터 중 마방 사정에 가장 가까운 자료다. 시장이 보고 우리가 못 보던
    것이 여기 있을 가능성이 높다 — 7개월 쉬고 나온 말이 연승 1.0배로 지지받는
    상황은 전적만 봐서는 설명되지 않는다.

    **경주일 이전 기록만 쓴다.** 조교는 경주 당일 아침에도 이뤄지고 발주 전에
    공시되지만, '당일 것을 쓸 수 있는가'는 수집 시점에 따라 달라진다. 경계를
    날짜로 못 박아 두면 학습과 추론이 어긋날 여지가 없다.
    """
    for c in TRAINING_FEATURES:
        df[c] = np.nan
    try:
        tr = pd.read_sql_query(
            "SELECT hr_no, trng_dt, tr_term, run1_cnt, run2_cnt, pr_gubun, chul_gubun "
            "FROM daily_training WHERE hr_no IS NOT NULL AND trng_dt IS NOT NULL", conn)
    except Exception:  # noqa: BLE001 — 조교 테이블이 없어도 나머지는 돌아야 한다
        return df
    if tr.empty:
        return df

    tr["d"] = pd.to_datetime(tr["trng_dt"], errors="coerce")
    tr = tr.dropna(subset=["d"]).sort_values(["hr_no", "d"])
    tr["is_jk"] = (tr["pr_gubun"].astype(str) == "기수").astype(float)
    tr["is_entry"] = tr["chul_gubun"].astype(str).str.contains("금주").astype(float)
    for c in ("tr_term", "run1_cnt", "run2_cnt"):
        tr[c] = pd.to_numeric(tr[c], errors="coerce")

    cols = ["d", "tr_term", "run1_cnt", "run2_cnt", "is_jk", "is_entry"]
    by_horse = {h: g[cols].to_numpy() for h, g in tr.groupby("hr_no")}
    dates = {h: g["d"].to_numpy() for h, g in tr.groupby("hr_no")}

    race_d = pd.to_datetime(df["rc_date"], errors="coerce").to_numpy()
    hr = df["hr_no"].to_numpy()
    out = np.full((len(df), len(TRAINING_FEATURES)), np.nan)
    day = np.timedelta64(1, "D")

    for i in range(len(df)):
        h, rd = hr[i], race_d[i]
        arr = by_horse.get(h)
        if arr is None or rd != rd:
            continue
        ds = dates[h]
        end = np.searchsorted(ds, rd)          # 경주일 이전만
        if end == 0:
            continue
        last = ds[end - 1]
        w14 = np.searchsorted(ds, rd - 14 * day)
        w7 = np.searchsorted(ds, rd - 7 * day)
        w30 = np.searchsorted(ds, rd - 30 * day)
        a14 = arr[w14:end]
        term7 = arr[w7:end, 1]
        term30 = arr[w30:w7, 1]

        out[i, 0] = (rd - last) / day
        out[i, 1] = len(a14)
        out[i, 2] = np.nanmean(a14[:, 1]) if len(a14) else np.nan
        out[i, 3] = np.nansum(a14[:, 4]) if len(a14) else 0.0
        out[i, 4] = np.nansum(a14[:, 2]) if len(a14) else 0.0
        out[i, 5] = np.nansum(a14[:, 3]) if len(a14) else 0.0
        # 마방의 출전 계획 — 직전 조교에 '금주출전예정' 이 찍혔는가
        out[i, 6] = arr[end - 1, 5]
        # 최근 일주일 조교량이 그전 3주 대비 늘었나 (컨디션 끌어올리는 중인가)
        if len(term7) and len(term30):
            a, b = np.nanmean(term7), np.nanmean(term30)
            if b and b == b:
                out[i, 7] = a / b

    for j, c in enumerate(TRAINING_FEATURES):
        df[c] = out[:, j]
    return df

def build_frame(conn: sqlite3.Connection,
                race_keys: Optional[List[str]] = None) -> pd.DataFrame:
    """학습·추론이 **하나의** 프레임에서 나오게 한다.

    이전 구조는 학습을 경주성적으로, 추론을 출전표로 만들었다. 그러면 출전표에만
    있는 KRA 공식 통산 성적을 모델이 학습한 적이 없는데 추론 때만 들이미는 꼴이
    된다. 학습·추론 불일치는 조용히 성능을 갉아먹는 대표적인 원인이다.

    그래서 경주-마필 한 쌍당 정확히 한 행을 만든다:
      * 이미 시행된 경주 → 경주성적 행에 출전표의 공식 지표를 병합 (레이블 있음)
      * 아직 안 열린 경주 → 출전표 행을 덧붙임 (레이블 없음)
    이력 피처는 이 통합 프레임 위에서 한 번만 계산되므로 두 경로가 문자 그대로
    같은 코드를 탄다.
    """
    hist = load_history(conn)
    ent = load_entries(conn, race_keys)

    if hist.empty and ent.empty:
        return pd.DataFrame()

    if not ent.empty:
        merge_cols = ["race_key", "hr_no"] + [c for c in OFFICIAL_COLS if c in ent.columns]
        official = ent[merge_cols].drop_duplicates(subset=["race_key", "hr_no"])
    else:
        official = pd.DataFrame(columns=["race_key", "hr_no"])

    if not hist.empty:
        base = hist.merge(official, on=["race_key", "hr_no"], how="left")
        seen = set(zip(base["race_key"], base["hr_no"]))
    else:
        base = pd.DataFrame()
        seen = set()

    if not ent.empty:
        mask = [(k, h) not in seen for k, h in zip(ent["race_key"], ent["hr_no"])]
        future = ent[pd.Series(mask, index=ent.index)].copy()
    else:
        future = pd.DataFrame()

    if not future.empty:
        for c in ("ord", "record_sec", "horse_weight", "win_odds", "place_odds",
                  "s1f_rank", "c1_rank", "c2_rank", "c3_rank", "c4_rank", "g1f_rank"):
            future[c] = np.nan
        for c in ("track_cond", "weather"):
            future[c] = None

    frames = [f for f in (base, future) if not f.empty]
    cols = sorted({c for f in frames for c in f.columns})
    combined = pd.concat([f.reindex(columns=cols) for f in frames], ignore_index=True)

    combined = combined.sort_values(
        ["rc_date", "meet", "rc_no", "chul_no"], na_position="last"
    ).reset_index(drop=True)

    built = build_history_index(combined)
    built = add_official(built)
    built = add_training(built, conn)
    built = finalize(built)
    built = built.copy()  # 컬럼을 많이 붙여 조각난 프레임을 한 번 정리
    built["y_win"] = built["is_win"]
    # 예상 기호가 '2착 이내 수준 / 3착 이내 수준'을 뜻하므로, 그 수준을 직접
    # 추정할 헤드가 필요하다. 승률에서 유추하면 접전 경주에서 특히 어긋난다.
    built["y_top2"] = built["is_top2"]
    built["y_top3"] = built["is_top3"]
    return built


def build_training_frame(conn: sqlite3.Connection) -> pd.DataFrame:
    """레이블이 있는 행만 — 즉 이미 시행이 끝난 경주."""
    df = build_frame(conn)
    if df.empty:
        return df
    return df[df["y_win"].notna()].reset_index(drop=True)


def build_prediction_frame(
    conn: sqlite3.Connection, race_keys: Optional[List[str]] = None
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """지정한 경주의 추론용 행. 학습과 동일한 프레임에서 잘라낸다."""
    entries = load_entries(conn, race_keys)
    if entries.empty:
        return entries, entries
    df = build_frame(conn, race_keys)
    if df.empty:
        return df, entries
    keys = set(race_keys or entries["race_key"].unique())
    target = df[df["race_key"].isin(keys)].copy()
    return target, entries
