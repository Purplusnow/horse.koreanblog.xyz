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

log = logging.getLogger(__name__)

WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]

# 이 아래로는 표본이 얕아 숫자가 잡음이 된다. 구간을 나눌수록 더 그렇다.
MIN_GROUP_RACES = 20
MIN_TIER_RACES = 30


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
    for c in ("ord", "win_odds", "place_odds", "pred_rank", "p_win", "p_top2", "cancelled"):
        df[c] = pd.to_numeric(df[c], errors="coerce")

    # 취소마 제외 후 남은 출주마들로 예측 순위를 다시 부여
    df = df[df["cancelled"].fillna(0) == 0].copy()
    df["pred_rank"] = (df.groupby("race_key")["p_win"]
                       .rank(ascending=False, method="first").astype(int))

    # 1착이 정확히 한 마리 확인되는 경주만 (동착·미확정 제외)
    ok = df.groupby("race_key")["ord"].transform(lambda s: (s == 1).sum() == 1)
    return df[ok].copy()


def race_level(df: pd.DataFrame) -> pd.DataFrame:
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

        rows.append({
            "race_key": key,
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


def build_report(conn: sqlite3.Connection) -> Dict:
    df = load_verified(conn)
    rl = race_level(df)
    if rl.empty:
        empty = {"overall": {"n_races": 0}, "monthly": [], "recent": []}
        for k in ("by_meet", "by_conf", "by_mark", "by_distance", "by_field",
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
