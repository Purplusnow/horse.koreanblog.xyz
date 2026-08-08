"""시뮬레이션 vs 기존 모델 적중률 비교 (과거 경주 기준).

시뮬레이션이 화면용 연출에 그치는지, 실제로 예측력이 있는지를 가른다.
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
from horseai.simulate import build_runners, confidence, simulate  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/horseai.sqlite")
    ap.add_argument("--races", type=int, default=1200)
    ap.add_argument("--sims", type=int, default=600)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    df = build_training_frame(conn)
    conn.close()

    # 최근 구간에서 표본 추출 (이력이 충분히 쌓인 시기)
    df = df.sort_values("rc_date")
    keys = df[df["rc_date"] >= df["rc_date"].quantile(0.7)]["race_key"].unique()
    keys = keys[-args.races:]

    rows = []
    for k in keys:
        sub = df[df["race_key"] == k]
        if len(sub) < 6 or sub["ord"].isna().all():
            continue
        dist = float(pd.to_numeric(sub["distance"], errors="coerce").iloc[0])
        if not np.isfinite(dist) or dist <= 0:
            continue
        runners = build_runners(sub.to_dict("records"))
        sim = simulate(runners, dist, n_sims=args.sims)
        conf = confidence(sim)

        ordn = pd.to_numeric(sub["ord"], errors="coerce").to_numpy()
        odds = pd.to_numeric(sub["win_odds"], errors="coerce").to_numpy()
        if not np.isfinite(ordn).any() or (ordn == 1).sum() != 1:
            continue

        sim_top = int(np.argmax(sim.win_prob))
        sim_top3 = set(np.argsort(sim.win_prob)[::-1][:3])
        mkt_top = int(np.nanargmin(odds)) if np.isfinite(odds).any() else -1

        rows.append({
            "race_key": k,
            "sim_hit": float(ordn[sim_top] == 1),
            "sim_place": float(ordn[sim_top] <= 3),
            "sim_top3": float(any(ordn[i] == 1 for i in sim_top3)),
            "sim_odds": odds[sim_top] if np.isfinite(odds[sim_top]) else np.nan,
            "mkt_hit": float(ordn[mkt_top] == 1) if mkt_top >= 0 else np.nan,
            "conf": conf["score"],
            "label": conf["label"],
        })

    t = pd.DataFrame(rows)
    print(f"\n검증 {len(t):,}경주\n")
    print(f"{'':<18}{'단승':>9}{'연승':>9}{'3순위내':>10}")
    print("-" * 48)
    print(f"{'시뮬레이션':<18}{t['sim_hit'].mean():>8.1%}{t['sim_place'].mean():>9.1%}{t['sim_top3'].mean():>10.1%}")
    print(f"{'[기준] 시장 1인기':<18}{t['mkt_hit'].mean():>8.1%}{'':>9}{'':>10}")
    roi = float((t['sim_hit'] * t['sim_odds'].fillna(0)).sum() / t['sim_odds'].notna().sum())
    print(f"\n  시뮬 단승 회수율 {roi:.1%}")

    print(f"\n── 신뢰도 등급별 (지표가 실제로 작동하는가) ──")
    print(f"{'등급':<8}{'경주':>7}{'단승':>9}{'연승':>9}{'시장':>9}")
    for lab in ("강승부", "중승부", "약승부"):
        s = t[t["label"] == lab]
        if len(s) < 20:
            continue
        print(f"{lab:<8}{len(s):>7,}{s['sim_hit'].mean():>9.1%}"
              f"{s['sim_place'].mean():>9.1%}{s['mkt_hit'].mean():>9.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
