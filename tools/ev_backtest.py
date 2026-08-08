"""기댓값 기반 마권 선별 백테스트 (진단 도구).

예측 자체는 능력만으로 하고, **베팅 대상 선별에만** 배당을 쓴다.
'어느 말이 이기나'와 '이 마권이 살 만한가'는 다른 질문이며, 후자에서만
시장 가격을 참조하는 것은 예측을 오염시키지 않는다.

    python tools/ev_backtest.py --db data/horseai.sqlite
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

TAKEOUT = 0.20  # 단승 공제율 — 배당에서 내재확률을 역산할 때 쓴다


def summarize(bets: pd.DataFrame, label: str, total_races: int) -> dict:
    if bets.empty:
        return {"전략": label, "베팅수": 0}
    hit = (bets["ord"] == 1)
    payout = (hit * bets["win_odds"]).sum()
    return {
        "전략": label,
        "베팅수": len(bets),
        "베팅비율": len(bets) / total_races,
        "적중률": hit.mean(),
        "회수율": payout / len(bets),
        "평균배당": bets["win_odds"].mean(),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/horseai.sqlite")
    ap.add_argument("--folds", type=int, default=4)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    df = build_training_frame(conn)
    conn.close()

    _, preds = walk_forward(df, n_folds=args.folds)
    if preds.empty:
        print("검증 예측을 만들지 못했습니다.", file=sys.stderr)
        return 1

    p = preds[preds["ord"].notna() & preds["win_odds"].notna()].copy()
    p["win_odds"] = pd.to_numeric(p["win_odds"], errors="coerce")
    p = p[p["win_odds"] > 0]

    # 배당 → 시장 내재확률 (공제율 보정)
    p["mkt_prob"] = (1 - TAKEOUT) / p["win_odds"]
    # 기댓값 배수: 우리가 시장보다 몇 배 높게 보는가
    p["edge"] = p["p_win_norm"] / p["mkt_prob"].replace(0, np.nan)
    # 단승 기대수익 = 확률 × 배당
    p["ev"] = p["p_win_norm"] * p["win_odds"]

    races = p["race_key"].nunique()
    top1 = p[p["pred_rank"] == 1]
    rows = [
        summarize(top1, "1순위 무조건 (현재)", races),
        summarize(top1[top1["edge"] > 1.0], "1순위 + 기댓값>1", races),
        summarize(top1[top1["win_odds"] >= 2.0], "1순위 + 배당 2.0배 이상", races),
        summarize(top1[top1["win_odds"] >= 3.0], "1순위 + 배당 3.0배 이상", races),
    ]
    # 순위 무관, 경주당 기댓값 최대 1두
    best_ev = p.sort_values("ev", ascending=False).groupby("race_key").head(1)
    rows.append(summarize(best_ev, "기댓값 최대 1두 (순위 무관)", races))
    rows.append(summarize(best_ev[best_ev["edge"] > 1.2], "기댓값 최대 + edge>1.2", races))
    # 시장 기준선
    mkt = p.sort_values("win_odds").groupby("race_key").head(1)
    rows.append(summarize(mkt, "[기준선] 1인기 단승", races))

    print(f"\n검증 {races:,}경주 · 단승 100원 균등 베팅 가정\n")
    print(f"{'전략':<26}{'베팅수':>8}{'경주대비':>9}{'적중률':>8}{'회수율':>9}{'평균배당':>9}")
    print("-" * 70)
    for r in rows:
        if not r.get("베팅수"):
            continue
        print(f"{r['전략']:<26}{r['베팅수']:>8,}{r['베팅비율']:>8.0%}"
              f"{r['적중률']:>8.1%}{r['회수율']:>9.1%}{r['평균배당']:>9.1f}")
    print("\n회수율 100% 초과가 이론상 손익분기입니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
