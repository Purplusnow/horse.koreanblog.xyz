"""실제 예측 적중률 검증.

백테스트가 아니라 **사이트에 실제로 공개했던 예측**을 결과와 대조한다. 백테스트
숫자는 얼마든지 예쁘게 만들 수 있으므로, 방문자에게 보여줄 신뢰 지표는 반드시
이쪽이어야 한다. 경주일이 지난 예측은 predict.py 가 동결하므로 사후 조작 여지가
없다.

    python -m horseai.verify --db data/horseai.sqlite
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .clock import today_kst
from .kra.store import session
from .marks import assign_marks

# 승식 정의. (코드, 이름, 우리 조합을 만드는 함수, 매수)
# 조합은 순서 없는 승식이면 정렬한다 — 저장된 적중 조합과 표기를 맞추기 위해서다.
POOL_LABEL = {"WIN": "단승", "PLC": "연승", "QNL": "복승", "EXA": "쌍승",
              "QPL": "복연승", "TLA": "삼복승", "TRI": "삼쌍승"}


def _combos(gates: List[int]) -> Dict[str, List[tuple]]:
    """추천 마번(순위순)에서 승식별로 '우리가 사는 조합'을 만든다."""
    from itertools import combinations
    g = [x for x in gates if x]
    if not g:
        return {}
    j = lambda *xs: "-".join(str(x) for x in xs)              # noqa: E731
    srt = lambda xs: j(*sorted(xs))                            # noqa: E731
    out: Dict[str, List[str]] = {}
    out["단승"] = ("WIN", [j(g[0])])
    out["연승"] = ("PLC", [j(g[0])])
    if len(g) >= 2:
        out["복승"] = ("QNL", [srt(g[:2])])
        out["쌍승"] = ("EXA", [j(g[0], g[1])])
        out["복연승"] = ("QPL", [srt(g[:2])])
    if len(g) >= 3:
        out["삼복승"] = ("TLA", [srt(g[:3])])
        out["삼쌍승"] = ("TRI", [j(*g[:3])])
    return out


def load_dividends(conn: sqlite3.Connection) -> Dict[str, Dict[str, Dict[str, float]]]:
    """적중 조합의 배당표. {race_key: {pool: {combo: odds}}}"""
    out: Dict[str, Dict[str, Dict[str, float]]] = {}
    try:
        rows = conn.execute("SELECT race_key, pool, combo, odds FROM dividends").fetchall()
    except Exception:  # noqa: BLE001
        return out
    for r in rows:
        out.setdefault(r["race_key"], {}).setdefault(r["pool"], {})[r["combo"]] = r["odds"]
    return out

log = logging.getLogger(__name__)

WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]

# 이 아래로는 표본이 얕아 숫자가 잡음이 된다. 구간을 나눌수록 더 그렇다.
MIN_GROUP_RACES = 20
MIN_TIER_RACES = 30


def set_min_sample(n: Optional[int]) -> None:
    """표본 하한을 낮춘다 (개발 중 화면 확인용). config.build.min_sample."""
    global MIN_GROUP_RACES, MIN_TIER_RACES
    if n:
        MIN_GROUP_RACES = MIN_TIER_RACES = int(n)


def _distance_band(m) -> str:
    """거리대. 단거리와 장거리는 사실상 다른 종목이라 따로 봐야 한다."""
    try:
        d = float(m)
    except (TypeError, ValueError):
        return ""
    if d <= 1200:
        return "단거리 (~1200m)"
    if d <= 1700:
        return "중거리 (1300~1700m)"
    return "장거리 (1800m~)"


def _field_band(n) -> str:
    """출주 두수. 두수가 늘수록 맞히기 어려워지는 게 정상이다."""
    try:
        v = int(n)
    except (TypeError, ValueError):
        return ""
    if v <= 8:
        return "소두수 (~8두)"
    if v <= 11:
        return "중두수 (9~11두)"
    return "다두수 (12두~)"


def _grade_band(g) -> str:
    """등급을 굵게 묶는다. 국1~국6을 그대로 나누면 구간마다 표본이 얕아진다."""
    t = str(g or "").strip()
    if not t:
        return ""
    for hi in ("1", "2", "3"):
        if hi in t:
            return "상위 등급"
    return "하위 등급" if any(c in t for c in "456") else t

VERIFY_SQL = """
SELECT
    p.race_key, p.hr_no, p.pred_rank, p.p_win, p.p_place, p.p_top2,
    p.model_version, p.created_at,
    r.rc_date, r.meet, r.rc_no, r.distance, r.grade, r.field_size, r.track_cond,
    res.ord, res.win_odds, res.place_odds, res.hr_name, res.chul_no,
    s.conf_label, s.conf_score,
    CASE WHEN c.hr_no IS NOT NULL THEN 1 ELSE 0 END AS cancelled
FROM predictions p
JOIN races  r   ON r.race_key = p.race_key
LEFT JOIN results res ON res.race_key = p.race_key AND res.hr_no = p.hr_no
LEFT JOIN simulations s ON s.race_key = p.race_key
LEFT JOIN cancellations c ON c.race_key = p.race_key AND c.hr_no = p.hr_no
WHERE COALESCE(r.has_result, 0) = 1
"""


def load_verified(conn: sqlite3.Connection) -> pd.DataFrame:
    """공개했던 예측을 결과와 맞춘다.

    **출주 취소마 처리** — 취소는 발주 직전에 결정되므로 예상에는 반영하지 않는다.
    다만 집계에서는 빼야 한다. 취소마를 1순위로 꼽았던 경주까지 '실패'로 세면
    우리가 통제할 수 없는 사유로 적중률이 깎이고, 반대로 그냥 두면 순위가
    밀려 올라간 말이 1순위인 척하게 된다. 그래서 **취소마를 제거한 뒤 남은
    말들로 순위를 다시 매겨** 평가한다. 실제로 출주한 말들 사이의 예측만 남는다.
    """
    df = pd.read_sql_query(VERIFY_SQL, conn)
    if df.empty:
        return df
    df["rc_date"] = pd.to_datetime(df["rc_date"], errors="coerce")
    for c in ("ord", "win_odds", "place_odds", "pred_rank", "p_win", "p_top2", "cancelled"):
        df[c] = pd.to_numeric(df[c], errors="coerce")

    # 취소마 제외 후 남은 출주마들로 예측 순위를 다시 부여
    df = df[df["cancelled"].fillna(0) == 0].copy()
    df["pred_rank"] = (df.groupby("race_key")["p_win"]
                       .rank(ascending=False, method="first").astype(int))

    # 1착이 정확히 한 마리 확인되는 경주만 (동착·미확정 제외)
    ok = df.groupby("race_key")["ord"].transform(lambda s: (s == 1).sum() == 1)
    return df[ok].copy()


def race_level(df: pd.DataFrame, div: Optional[Dict] = None) -> pd.DataFrame:
    """경주 단위 적중 여부 테이블.

    한국마사회가 발매하는 마권 7종을 모두 판정한다. 예상지 독자는 단승만 사지
    않으므로, 단승 적중률 하나로는 이 예상이 자기에게 쓸모 있는지 알 수 없다.
    """
    if df.empty:
        return df
    rows = []
    for key, g in df.groupby("race_key"):
        g = g.sort_values("pred_rank")
        if g.empty or (g["pred_rank"] == 1).sum() != 1:
            continue
        t1 = g[g["pred_rank"] == 1].iloc[0]

        # 순위별 착순. 마권 적중 판정은 전부 이 표에서 나온다.
        o = {int(r.pred_rank): (r.ord if pd.notna(r.ord) else None)
             for r in g.itertuples() if pd.notna(r.pred_rank)}
        top_n = lambda n: {v for k, v in o.items() if k <= n and v}   # noqa: E731
        top2, top3, top4, top5 = (top_n(2), top_n(3), top_n(4), top_n(5))
        at = o.get

        # 연승 기준 착순은 출주 두수로 갈린다 (마사회 규정: 7두 이하는 2착까지)
        fs = g["field_size"].iloc[0]
        cut = 2 if (pd.notna(fs) and float(fs) <= 7) else 3

        # 화면에 실제로 나간 기호를 그대로 다시 매긴다 — 규칙은 marks 한 곳이다
        rows_for_mark = g.to_dict("records")
        assign_marks(rows_for_mark)
        marks = [r["mark"] for r in rows_for_mark]

        # ── 승식별 적중·배당 ────────────────────────────────────────
        # 배당은 '적중 조합' 만 저장돼 있으므로, 우리 조합이 그 표에 있으면
        # 적중이고 없으면 불발이다. 배당은 그대로 회수액이 된다(1배 = 원금).
        gates = [g[g["pred_rank"] == r]["chul_no"].iloc[0] if (g["pred_rank"] == r).any()
                 else None for r in range(1, 6)]
        gates = [int(x) if pd.notna(x) else None for x in gates]
        # **배당표가 없는 경주는 판정하지 않는다.** 적중 조합만 저장되므로
        # '표에 없으면 불발'인데, 자료 자체가 안 들어온 경주까지 그렇게 세면
        # 맞힌 경주가 불발로 남는다. 실제로 8/8 서울 8~10R 처럼 1순위가 1착한
        # 경주가 0/7 로 찍혔다. 자료가 올 때까지 집계에서 빼는 것이 맞다.
        bets = {}
        table = (div or {}).get(key)
        if table:
            for name, (pool, mine) in _combos(gates).items():
                paid = sum(table.get(pool, {}).get(c) or 0.0 for c in mine)
                bets[name] = {"cost": len(mine), "payout": paid, "hit": paid > 0}

        rows.append({
            "race_key": key,
            "bets": bets,
            "conf_label": g["conf_label"].iloc[0] if "conf_label" in g else None,
            "rc_date": g["rc_date"].iloc[0],
            "meet": g["meet"].iloc[0],
            "rc_no": g["rc_no"].iloc[0],
            "distance": g["distance"].iloc[0],
            "grade": g["grade"].iloc[0],
            "field_size": fs,
            "track_cond": g["track_cond"].iloc[0],
            "weekday": (WEEKDAY_KO[g["rc_date"].iloc[0].weekday()]
                        if pd.notna(g["rc_date"].iloc[0]) else ""),
            "top1_hr_name": t1.get("hr_name"),
            "top1_ord": at(1),
            "top1_odds": t1["win_odds"],
            "star_race": float("★" in marks),

            # ── 마권 7종 (한국마사회 발매 기준) ──────────────────────────
            "hit_win": float(at(1) == 1),                                   # 단승
            "hit_place": float(bool(at(1)) and at(1) <= cut),               # 연승
            "hit_quinella": float(top2 == {1.0, 2.0}),                      # 복승
            "hit_exacta": float(at(1) == 1 and at(2) == 2),                 # 쌍승
            "hit_quinella_place": float(bool(at(1)) and bool(at(2))
                                        and at(1) <= 3 and at(2) <= 3),     # 복연승
            "hit_trio": float(top3 == {1.0, 2.0, 3.0}),                     # 삼복승
            "hit_trifecta": float(at(1) == 1 and at(2) == 2 and at(3) == 3),  # 삼쌍승

            # 박스 — 예상지 독자가 실제로 사는 방식
            "hit_quinella_b3": float({1.0, 2.0} <= top3),
            "hit_quinella_b5": float({1.0, 2.0} <= top5),
            "hit_trio_b4": float({1.0, 2.0, 3.0} <= top4),
            "hit_trio_b5": float({1.0, 2.0, 3.0} <= top5),

            "hit_top3_has_winner": float(1.0 in top3),
            "hit_top5_winner": float(1.0 in top5),
            "payout_win": (float(t1["win_odds"])
                           if at(1) == 1 and pd.notna(t1["win_odds"]) else 0.0),
        })
    return pd.DataFrame(rows)


def breakdown(rl: pd.DataFrame, key, label: str,
              min_races: int = MIN_GROUP_RACES) -> List[Dict]:
    """축 하나로 갈라 집계한다. 표본이 얕은 구간은 내보내지 않는다."""
    if rl.empty:
        return []
    col = rl[key].map(key if callable(key) else (lambda v: v)) if callable(key) else rl[key]
    out = []
    for name, g in rl.assign(_k=col).groupby("_k"):
        if not str(name) or len(g) < min_races:
            continue
        row = summarize(g)
        row[label] = str(name)
        out.append(row)
    return out


def summarize(rl: pd.DataFrame) -> Dict:
    if rl.empty:
        return {"n_races": 0}
    n = len(rl)
    bets = rl["top1_odds"].notna().sum()
    return {
        "n_races": int(n),
        "hit_win": float(rl["hit_win"].mean()),
        "hit_place": float(rl["hit_place"].mean()),
        "hit_top3_has_winner": float(rl["hit_top3_has_winner"].mean()),
        **{c: float(rl[c].mean()) for c in (
            "hit_quinella", "hit_exacta", "hit_quinella_place", "hit_trio",
            "hit_trifecta", "hit_quinella_b3", "hit_quinella_b5",
            "hit_trio_b4", "hit_trio_b5", "hit_top5_winner",
        ) if c in rl},
        "roi_win": float(rl.loc[rl["top1_odds"].notna(), "payout_win"].sum() / bets) if bets else None,
        "avg_win_odds": float(rl.loc[rl["hit_win"] == 1, "top1_odds"].mean())
        if (rl["hit_win"] == 1).any() else None,
        "first_date": str(rl["rc_date"].min())[:10],
        "last_date": str(rl["rc_date"].max())[:10],
    }


# 마사회가 파는 일곱 가지 승식만 센다.
#
# 박스는 승식이 아니라 '몇 통을 사느냐'는 구매 방식이다. 표에 섞어 두니 통합
# 환수율에서 박스만 빼야 했고, 그 예외가 표를 읽는 사람에게 설명되지 않았다.
# 게다가 순위에 신호가 있다면 박스는 아래 순위 조합을 덧사는 것이라 환수율이
# 낮아지는 게 정상이다 — 표에 두면 '박스가 불리하다'는 결론으로 읽히지만
# 실제로는 적중률과 환수율을 맞바꾼 것뿐이라, 오해만 남는다.
BET_ORDER = ["단승", "연승", "복승", "쌍승", "복연승", "삼복승", "삼쌍승"]


def bet_summary(rl: pd.DataFrame) -> List[Dict]:
    """승식별 누적 적중률과 환수율.

    환수율은 '그 방식으로 매 경주 균등하게 샀다면 얼마가 돌아왔나'다. 박스는
    매수가 늘어난 만큼 원금도 늘어나므로 분모에 그대로 반영된다 — 그래야
    넓게 사는 방식이 유리해 보이는 착시가 없다.
    """
    if rl.empty or "bets" not in rl:
        return []
    agg: Dict[str, Dict[str, float]] = {}
    for bets in rl["bets"]:
        for name, b in (bets or {}).items():
            a = agg.setdefault(name, {"n": 0, "hit": 0, "cost": 0.0, "payout": 0.0})
            a["n"] += 1
            a["hit"] += int(b["hit"])
            a["cost"] += b["cost"]
            a["payout"] += b["payout"]
    out = []
    # 일곱 승식을 합친 값. 각 승식을 매 경주 한 통씩 산 포트폴리오의 성적이다.
    # 개별 환수율만 늘어놓으면 좋은 것만 눈에 들어오는데, 실제로 예상을 그대로
    # 따라 산 사람의 손익은 이 한 줄이다.
    tot = {"n": 0, "hit": 0, "cost": 0.0, "payout": 0.0}
    for name in BET_ORDER:
        a = agg.get(name)
        if not a or not a["n"]:
            continue
        for k in tot:
            tot[k] += a[k]
        out.append({
            "name": name,
            "n_races": int(a["n"]),
            "tickets": int(a["cost"] / max(1, a["n"])),
            "hit_rate": a["hit"] / a["n"],
            "roi": a["payout"] / a["cost"] if a["cost"] else None,
            "best": None,
        })
    if tot["cost"]:
        # 경주 수는 승식마다 같으므로 최댓값이 곧 실제 경주 수다.
        # tot["n"] 은 '경주 × 승식' 이라 여기 쓰면 안 된다.
        races = max((r["n_races"] for r in out), default=0)
        out.append({
            "name": "전체 통합",
            "n_races": races,
            "tickets": int(round(tot["cost"] / races)) if races else 0,
            "hit_rate": tot["hit"] / tot["n"],
            "roi": tot["payout"] / tot["cost"],
            "best": None,
            "is_total": True,
        })
    return out


def daily_summary(rl: pd.DataFrame, limit: int = 60) -> List[Dict]:
    """경주일별 성적.

    누적만 보면 좋았던 날과 나빴던 날이 한 숫자에 뭉개진다. 특히 환수율은
    고배당 적중 한 건이 오래 남아, 그 뒤로 계속 잃어도 누적은 좋아 보인다.
    날짜별로 끊어 두면 어느 날에 무슨 일이 있었는지 그대로 드러난다.
    """
    if rl.empty or "bets" not in rl or "rc_date" not in rl:
        return []
    out = []
    for day, g in rl.groupby(rl["rc_date"].dt.date):
        cost = pay = hit = n = 0
        per = {}
        for bets in g["bets"]:
            for name, b in (bets or {}).items():
                if name not in BET_ORDER:
                    continue
                cost += b["cost"]; pay += b["payout"]
                hit += int(b["hit"]); n += 1
                d = per.setdefault(name, {"hit": 0, "n": 0})
                d["hit"] += int(b["hit"]); d["n"] += 1
        if not cost:
            continue
        out.append({
            "date": str(day),
            "n_races": int(g["race_key"].nunique()),
            "tickets": int(cost),
            "hits": int(hit),
            "hit_rate": hit / n if n else None,
            "roi": pay / cost,
            "by_bet": [{"name": k, "hit": v["hit"], "n": v["n"]}
                       for k, v in per.items()],
        })
    out.sort(key=lambda r: r["date"], reverse=True)
    return out[:limit]


# 성과 카드에 올릴 최소 배당. 이보다 낮으면 '터졌다' 고 말하기 어렵다.
HIGHLIGHT_MIN_ODDS = 10.0


def highlights(rl: pd.DataFrame, limit: int = 6) -> Dict[str, List[Dict]]:
    """최근 고배당 적중.

    적중률·환수율 표는 성실하지만 눈에 걸리지 않는다. 처음 온 사람이 이 사이트를
    한 번 더 볼 이유는 '어제 삼쌍승 350배가 터졌다' 같은 구체적인 장면이다.
    표에 이미 들어 있는 사실을 앞으로 꺼내는 것이므로 없는 말을 지어내지 않는다.
    """
    if rl.empty or "bets" not in rl:
        return {"top": [], "recent": []}
    out = []
    for r in rl.itertuples():
        for name in BET_ORDER:
            b = (r.bets or {}).get(name)
            if not b or not b["hit"]:
                continue
            odds = b["payout"] / max(1, b["cost"])
            if odds < HIGHLIGHT_MIN_ODDS:
                continue
            out.append({
                "race_key": r.race_key,
                "date": str(r.rc_date)[:10],
                "meet": r.meet,
                "rc_no": int(r.rc_no) if pd.notna(r.rc_no) else None,
                "bet": name,
                "odds": round(odds, 1),
            })
    # 한 경주에서 여러 승식이 터지면 가장 큰 것 하나만 남긴다. 같은 경주가
    # 카드로 세 번 나오면 성과가 여럿인 것처럼 보여 오히려 신뢰를 깎는다.
    best = {}
    for h in out:
        cur = best.get(h["race_key"])
        if not cur or h["odds"] > cur["odds"]:
            h["also"] = (cur or {}).get("also", 0) + (1 if cur else 0)
            best[h["race_key"]] = h
        else:
            best[h["race_key"]]["also"] = best[h["race_key"]].get("also", 0) + 1
    rows = list(best.values())

    # 두 갈래로 낸다.
    #   역대  — 오래돼도 남길 만한 기록. 시간이 지나면 최근 목록에서 밀려나는데,
    #          이 사이트가 무엇까지 맞혀 봤는지는 계속 말할 수 있어야 한다.
    #   최근  — 지금도 맞히고 있다는 증거. 옛 기록만 걸어 두면 과거형이 된다.
    top = sorted(rows, key=lambda h: -h["odds"])[:limit]
    recent = sorted(rows, key=lambda h: (h["date"], h["odds"]), reverse=True)[:limit]
    # 최근 목록에 이미 있는 것을 역대에 또 걸면 같은 카드가 두 번 나온다.
    seen = {h["race_key"] for h in recent}
    top = [h for h in top if h["race_key"] not in seen]
    return {"top": top, "recent": recent}


def build_report(conn: sqlite3.Connection) -> Dict:
    df = load_verified(conn)
    rl = race_level(df, load_dividends(conn))
    if rl.empty:
        empty = {"overall": {"n_races": 0}, "monthly": [], "recent": [],
                 "daily": [], "highlights": {"top": [], "recent": []}}
        for k in ("by_bet", "by_meet", "by_conf", "by_mark", "by_distance", "by_field",
                  "by_grade", "by_weekday", "by_track"):
            empty[k] = []
        empty["star"] = {"n_races": 0}
        return empty

    rl = rl.sort_values("rc_date")
    monthly = []
    for period, g in rl.groupby(rl["rc_date"].dt.to_period("M")):
        s = summarize(g)
        s["month"] = str(period)
        monthly.append(s)

    # ── 축별 집계 ────────────────────────────────────────────────
    # 하나의 총계만 내밀면 '어디서 강하고 어디서 약한가'를 알 수 없다. 축을
    # 갈라 두면 방문자는 자기가 사는 경주에 맞는 수치를 골라 볼 수 있고, 우리도
    # 약한 구간을 숨길 수 없게 된다.
    by_meet = breakdown(rl, "meet", "meet", min_races=1)
    rl = rl.assign(
        _dist=rl["distance"].map(_distance_band),
        _field=rl["field_size"].map(_field_band),
        _grade=rl["grade"].map(_grade_band),
    )
    by_distance = breakdown(rl, "_dist", "band")
    by_field = breakdown(rl, "_field", "band")
    by_grade = breakdown(rl, "_grade", "band")
    by_weekday = breakdown(rl, "weekday", "band")
    by_track = breakdown(rl, "track_cond", "band")

    # 신뢰도 등급이 실제로 작동하는지 — 강승부가 정말 더 잘 맞는지 공개한다.
    # 표본이 적으면 등급별 적중률은 거의 잡음이다. 6경주에서 83%가 찍히면
    # 우리 의도와 무관하게 과장으로 읽히므로, 의미를 가질 때까지는 내보내지 않는다.
    by_conf = []
    if "conf_label" in rl and rl["conf_label"].notna().any():
        for label in ("강승부", "중승부", "약승부"):
            g = rl[rl["conf_label"] == label]
            if len(g) < MIN_TIER_RACES:
                continue
            row = summarize(g)
            row["label"] = label
            by_conf.append(row)

    # ★ 가 붙은 경주만 따로. 유일하게 절대적인 주장이므로 실적도 따로 보여야 한다.
    star = rl[rl["star_race"] == 1] if "star_race" in rl else rl.iloc[0:0]
    star_stats = summarize(star) if len(star) >= MIN_GROUP_RACES else {"n_races": int(len(star))}

    # 기호별 — 그 기호를 받은 마필이 실제로 어떤 착순에 들었나
    by_mark = []
    if not df.empty:
        marked = []
        for key, g in df.groupby("race_key"):
            rows = g.to_dict("records")
            assign_marks(rows)
            marked.extend(rows)
        md = pd.DataFrame(marked)
        if not md.empty and "mark" in md:
            o = pd.to_numeric(md["ord"], errors="coerce")
            md = md.assign(_o=o)
            for m in ("★", "◎", "○", "△", "※"):
                g = md[md["mark"] == m]
                if len(g) < MIN_GROUP_RACES:
                    continue
                by_mark.append({
                    "mark": m,
                    "n": int(len(g)),
                    "hit_win": float((g["_o"] == 1).mean()),
                    "hit_top2": float((g["_o"] <= 2).mean()),
                    "hit_top3": float((g["_o"] <= 3).mean()),
                })

    # 최신 경주가 위로 온다. (화면에서는 결과 페이지가 이 역할을 하고,
    # 여기 값은 accuracy.json 을 직접 읽는 쪽을 위해 남긴다.) tail(60) 을 먼저 하면 날짜순이 아니라 표의 원래
    # 순서(race_key 알파벳순)에서 뒤 60건을 집게 되어, 최근 것이 빠질 수 있다.
    # 같은 날 안에서는 늦게 뛴 경주를 먼저 둔다 — 경마장은 그다음 기준이다.
    recent = (rl.sort_values(["rc_date", "rc_no", "meet"],
                             ascending=[False, False, True])
                .head(60))
    recent_rows = [
        {
            "race_key": r.race_key,
            "date": str(r.rc_date)[:10],
            "meet": r.meet,
            "rc_no": int(r.rc_no) if pd.notna(r.rc_no) else None,
            "pick": r.top1_hr_name,
            "ord": int(r.top1_ord) if pd.notna(r.top1_ord) else None,
            "odds": float(r.top1_odds) if pd.notna(r.top1_odds) else None,
            "hit_win": bool(r.hit_win),
            "hit_place": bool(r.hit_place),
            # ◎ 의 1착 여부만으로 성패를 적으면 이 예상이 실제로 쓸모 있었는지
            # 알 수 없다. 5두를 추천하고 일곱 승식으로 평가하는데, 목록에서만
            # 단승 하나로 판정하면 삼복승·삼쌍승을 맞힌 경주가 '실패'로 남는다.
            "hit_bets": [k for k in BET_ORDER
                         if (r.bets or {}).get(k, {}).get("hit")],
            "n_bets": len([k for k in BET_ORDER if k in (r.bets or {})]),
        }
        for r in recent.itertuples()
    ]

    last90 = rl[rl["rc_date"] >= rl["rc_date"].max() - pd.Timedelta(days=90)]
    return {
        "overall": summarize(rl),
        "last_90d": summarize(last90),
        "monthly": monthly,
        "by_bet": bet_summary(rl),
        "highlights": highlights(rl),
        "daily": daily_summary(rl),
        "by_meet": by_meet,
        "by_conf": by_conf,
        "by_mark": by_mark,
        "by_distance": by_distance,
        "by_field": by_field,
        "by_grade": by_grade,
        "by_weekday": by_weekday,
        "by_track": by_track,
        "star": star_stats,
        "recent": recent_rows,
    }


def report_text(rep: Dict) -> str:
    o = rep.get("overall", {})
    if not o.get("n_races"):
        return "아직 검증 가능한 예측이 없습니다. (예측 생성 후 경주 결과가 들어와야 집계됩니다)"
    lines = [
        f"공개 예측 검증  {o['first_date']} ~ {o['last_date']}  총 {o['n_races']:,}경주",
        "-" * 52,
        f"  ◎ 1착        {o['hit_win']:6.1%}",
        f"  ◎ 3착 이내   {o['hit_place']:6.1%}",
        f"  ◎○▲ 중 우승마            {o['hit_top3_has_winner']:6.1%}",
        f"  복승 (상위2두)              {o['hit_quinella']:6.1%}",
        f"  삼복승 (상위5두 박스)        {o['hit_trio_b5']:6.1%}",
    ]
    if o.get("roi_win") is not None:
        lines.append(f"  단승 회수율(ROI)            {o['roi_win']:6.1%}")
    if o.get("avg_win_odds"):
        lines.append(f"  적중 시 평균 배당           {o['avg_win_odds']:6.1f}배")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="공개 예측 적중률 검증")
    ap.add_argument("--db", default="data/horseai.sqlite")
    ap.add_argument("--out", default="data/accuracy.json")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with session(args.db) as conn:
        rep = build_report(conn)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(report_text(rep))
    print(f"\n검증 리포트 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
