"""구간별 대(對) 시장 성적 분석.

시장은 평균적으로 강하지만 모든 조건에서 균일하지는 않다. 정보가 적은 구간
(신마전, 장거리, 주로 급변)에서는 대중의 우위가 줄어들 수 있다. 우리가 실제로
앞서는 구간이 있다면 그것이 정직하게 내세울 수 있는 차별점이 된다.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from horseai.features import build_training_frame  # noqa: E402
from horseai.model import walk_forward  # noqa: E402


def race_table(preds: pd.DataFrame) -> pd.DataFrame:
    """경주 단위로 '우리 1순위'와 '시장 1인기'의 적중 여부를 나란히."""
    p = preds[preds["ord"].notna()].copy()
    p["win_odds"] = pd.to_numeric(p["win_odds"], errors="coerce")
    ok = p.groupby("race_key")["ord"].transform(lambda s: (s == 1).sum() == 1)
    p = p[ok & p["win_odds"].notna()]

    p["mkt_rank"] = p.groupby("race_key")["win_odds"].rank(method="first")
    ours = p[p["pred_rank"] == 1].set_index("race_key")
    mkt = p[p["mkt_rank"] == 1].set_index("race_key")
    top3 = (p[p["pred_rank"] <= 3].groupby("race_key")["ord"]
            .apply(lambda s: (s == 1).any()))

    out = pd.DataFrame({
        "our_hit": (ours["ord"] == 1).astype(float),
        "our_top3": top3,
        "our_odds": ours["win_odds"],
        "mkt_hit": (mkt["ord"] == 1).astype(float),
        "grade": ours["grade"],
        "distance": pd.to_numeric(ours["distance"], errors="coerce"),
        "field_size": pd.to_numeric(ours["field_size"], errors="coerce"),
        "moisture": pd.to_numeric(ours.get("moisture"), errors="coerce"),
        "starts_prior": pd.to_numeric(ours["starts_prior"], errors="coerce"),
        "fav_odds": mkt["win_odds"],
    }).dropna(subset=["our_hit", "mkt_hit"])
    return out


def report(df: pd.DataFrame, by: pd.Series, label: str, min_n: int = 150) -> None:
    g = df.groupby(by)
    rows = []
    for k, sub in g:
        if len(sub) < min_n:
            continue
        roi = float((sub["our_hit"] * sub["our_odds"]).sum() / len(sub))
        rows.append({
            "구간": str(k), "경주": len(sub),
            "우리": sub["our_hit"].mean(), "시장": sub["mkt_hit"].mean(),
            "차이": sub["our_hit"].mean() - sub["mkt_hit"].mean(),
            "회수율": roi,
        })
    if not rows:
        return
    print(f"\n── {label} ──")
    print(f"{'구간':<16}{'경주':>7}{'우리':>8}{'시장':>8}{'차이':>9}{'회수율':>9}")
    for r in sorted(rows, key=lambda x: -x["차이"]):
        mark = "  ◀" if r["차이"] > 0 else ""
        print(f"{r['구간']:<16}{r['경주']:>7,}{r['우리']:>8.1%}{r['시장']:>8.1%}"
              f"{r['차이']:>+8.1%}{r['회수율']:>9.1%}{mark}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/horseai.sqlite")
    ap.add_argument("--folds", type=int, default=4)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    df = build_training_frame(conn)
    conn.close()
    _, preds = walk_forward(df, n_folds=args.folds)
    t = race_table(preds)

    print(f"\n검증 {len(t):,}경주 · 전체 우리 {t['our_hit'].mean():.1%} vs 시장 {t['mkt_hit'].mean():.1%}")

    report(t, t["grade"].fillna("미상"), "등급별")
    report(t, pd.cut(t["distance"], [0, 1100, 1300, 1500, 1900, 9999],
                     labels=["~1100m", "1200~1300", "1400~1500", "1600~1800", "1900m~"]), "거리별")
    report(t, pd.cut(t["field_size"], [0, 8, 10, 12, 99],
                     labels=["~8두", "9~10두", "11~12두", "13두~"]), "두수별")
    report(t, pd.cut(t["moisture"], [-1, 5, 10, 15, 100],
                     labels=["건조", "양호", "다습", "포화"]), "주로 함수율별")
    report(t, pd.cut(t["starts_prior"], [-1, 0, 2, 5, 999],
                     labels=["신마(이력0)", "1~2전", "3~5전", "6전~"]), "1순위마 경험별")
    report(t, pd.cut(t["fav_odds"], [0, 2, 3, 5, 999],
                     labels=["1인기 ~2.0배", "2.0~3.0", "3.0~5.0", "5.0배~"]), "인기 집중도별")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
