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

from .kra.store import session

log = logging.getLogger(__name__)

VERIFY_SQL = """
SELECT
    p.race_key, p.hr_no, p.pred_rank, p.p_win, p.p_place, p.model_version, p.created_at,
    r.rc_date, r.meet, r.rc_no, r.distance, r.grade,
    res.ord, res.win_odds, res.place_odds, res.hr_name,
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
    for c in ("ord", "win_odds", "place_odds", "pred_rank", "p_win", "cancelled"):
        df[c] = pd.to_numeric(df[c], errors="coerce")

    # 취소마 제외 후 남은 출주마들로 예측 순위를 다시 부여
    df = df[df["cancelled"].fillna(0) == 0].copy()
    df["pred_rank"] = (df.groupby("race_key")["p_win"]
                       .rank(ascending=False, method="first").astype(int))

    # 1착이 정확히 한 마리 확인되는 경주만 (동착·미확정 제외)
    ok = df.groupby("race_key")["ord"].transform(lambda s: (s == 1).sum() == 1)
    return df[ok].copy()


def race_level(df: pd.DataFrame) -> pd.DataFrame:
    """경주 단위 적중 여부 테이블."""
    if df.empty:
        return df
    rows = []
    for key, g in df.groupby("race_key"):
        g = g.sort_values("pred_rank")
        top1 = g[g["pred_rank"] == 1]
        if top1.empty:
            continue
        t1 = top1.iloc[0]
        top3_ords = set(g[g["pred_rank"] <= 3]["ord"].dropna())
        top2_ords = set(g[g["pred_rank"] <= 2]["ord"].dropna())
        rows.append({
            "race_key": key,
            "conf_label": g["conf_label"].iloc[0] if "conf_label" in g else None,
            "rc_date": g["rc_date"].iloc[0],
            "meet": g["meet"].iloc[0],
            "rc_no": g["rc_no"].iloc[0],
            "distance": g["distance"].iloc[0],
            "grade": g["grade"].iloc[0],
            "top1_hr_name": t1.get("hr_name"),
            "top1_ord": t1["ord"],
            "top1_odds": t1["win_odds"],
            "hit_win": float(t1["ord"] == 1),
            "hit_place": float(t1["ord"] <= 3),
            "hit_top3_has_winner": float(1.0 in top3_ords),
            "hit_exacta_box": float(top2_ords == {1.0, 2.0}),
            "payout_win": float(t1["win_odds"]) if t1["ord"] == 1 and pd.notna(t1["win_odds"]) else 0.0,
        })
    return pd.DataFrame(rows)


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
        "hit_exacta_box": float(rl["hit_exacta_box"].mean()),
        "roi_win": float(rl.loc[rl["top1_odds"].notna(), "payout_win"].sum() / bets) if bets else None,
        "avg_win_odds": float(rl.loc[rl["hit_win"] == 1, "top1_odds"].mean())
        if (rl["hit_win"] == 1).any() else None,
        "first_date": str(rl["rc_date"].min())[:10],
        "last_date": str(rl["rc_date"].max())[:10],
    }


def build_report(conn: sqlite3.Connection) -> Dict:
    df = load_verified(conn)
    rl = race_level(df)
    if rl.empty:
        return {"overall": {"n_races": 0}, "monthly": [], "by_meet": [], "recent": []}

    rl = rl.sort_values("rc_date")
    monthly = []
    for period, g in rl.groupby(rl["rc_date"].dt.to_period("M")):
        s = summarize(g)
        s["month"] = str(period)
        monthly.append(s)

    by_meet = []
    for meet, g in rl.groupby("meet"):
        s = summarize(g)
        s["meet"] = meet
        by_meet.append(s)

    # 신뢰도 등급이 실제로 작동하는지 — 강승부가 정말 더 잘 맞는지 공개한다.
    # 다만 표본이 적으면 등급별 적중률은 거의 잡음이다. 6경주에서 83%가 찍히면
    # 우리 의도와 무관하게 과장으로 읽히므로, 의미를 가질 때까지는 내보내지 않는다.
    MIN_TIER_RACES = 30
    by_conf = []
    if "conf_label" in rl and rl["conf_label"].notna().any():
        for label in ("강승부", "중승부", "약승부"):
            g = rl[rl["conf_label"] == label]
            if len(g) < MIN_TIER_RACES:
                continue
            s = summarize(g)
            s["label"] = label
            by_conf.append(s)

    recent = rl.tail(60).sort_values("rc_date", ascending=False)
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
        }
        for r in recent.itertuples()
    ]

    last90 = rl[rl["rc_date"] >= rl["rc_date"].max() - pd.Timedelta(days=90)]
    return {
        "overall": summarize(rl),
        "last_90d": summarize(last90),
        "monthly": monthly,
        "by_meet": by_meet,
        "by_conf": by_conf,
        "recent": recent_rows,
    }


def report_text(rep: Dict) -> str:
    o = rep.get("overall", {})
    if not o.get("n_races"):
        return "아직 검증 가능한 예측이 없습니다. (예측 생성 후 경주 결과가 들어와야 집계됩니다)"
    lines = [
        f"공개 예측 검증  {o['first_date']} ~ {o['last_date']}  총 {o['n_races']:,}경주",
        "-" * 52,
        f"  단승 적중률 (1순위 → 1착)   {o['hit_win']:6.1%}",
        f"  연승 적중률 (1순위 → 3착내) {o['hit_place']:6.1%}",
        f"  3순위 내 1착 포함           {o['hit_top3_has_winner']:6.1%}",
        f"  복승 박스 (상위2두=1·2착)   {o['hit_exacta_box']:6.1%}",
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
