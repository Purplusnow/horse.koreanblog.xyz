"""조교 피처가 실제로 성능을 올리는지 검증한다.

새 데이터를 붙였다는 사실 자체는 아무것도 증명하지 않는다. 시간순 교차검증에서
같은 구간·같은 조건으로 **넣었을 때와 뺐을 때**를 비교해, 올랐으면 올랐다고
안 올랐으면 안 올랐다고 말하는 것이 목적이다.

조교 자료는 수집 구간(2024-08~)에만 있으므로, 그 이전 경주는 결측이다. 비교는
자료가 있는 구간의 경주로만 한다 — 결측 구간을 섞으면 차이가 희석된다.

    python tools/eval_training_features.py
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from horseai import features as F  # noqa: E402
from horseai import model as M  # noqa: E402
from horseai.model import walk_forward  # noqa: E402


def run(df: pd.DataFrame, drop: bool, from_date: str) -> dict:
    """drop=True 면 조교 피처를 빼고 학습한다."""
    keep = list(F.FEATURE_COLUMNS)
    if drop:
        keep = [c for c in keep if c not in F.TRAINING_FEATURES]
    # model 은 임포트 시점에 목록을 복사해 둔다. features 쪽만 바꾸면 학습은
    # 그대로 전체 피처로 돌아가고, A/B 가 갈리지 않은 채 같은 수치가 나온다
    # (실제로 그렇게 나와서 이 주석이 있다).
    orig_f, orig_m = F.FEATURE_COLUMNS, M.FEATURE_COLUMNS
    F.FEATURE_COLUMNS = M.FEATURE_COLUMNS = keep
    try:
        _, pred = walk_forward(df)
    finally:
        F.FEATURE_COLUMNS, M.FEATURE_COLUMNS = orig_f, orig_m

    pred = pred[pred["rc_date"] >= from_date]
    pred = pred[pd.to_numeric(pred["ord"], errors="coerce").notna()]
    rows = []
    for _, g in pred.groupby("race_key"):
        g = g.sort_values("pred_rank")
        o = pd.to_numeric(g["ord"], errors="coerce").to_numpy()
        if (o == 1).sum() != 1:
            continue
        rows.append({"win": float(o[0] == 1), "place": float(o[0] <= 3),
                     "top3": float(1 in o[:3])})
    t = pd.DataFrame(rows)
    y = pd.to_numeric(pred["y_win"], errors="coerce")
    p = pd.to_numeric(pred["p_win_norm"], errors="coerce")
    ok = y.notna() & p.notna()
    from sklearn.metrics import roc_auc_score
    return {
        "n_races": len(t),
        "win": t["win"].mean(),
        "place": t["place"].mean(),
        "top3": t["top3"].mean(),
        "auc": roc_auc_score(y[ok], p[ok]) if ok.sum() else float("nan"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/horseai.sqlite")
    ap.add_argument("--from-date", default="2024-09-01",
                    help="조교 자료가 채워진 구간의 시작")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    df = F.build_training_frame(conn)
    conn.close()

    cov = df[df["rc_date"] >= args.from_date]
    have = cov["trg_count_14"].notna().mean() if "trg_count_14" in cov else 0
    print(f"\n비교 구간 {args.from_date} ~ · {cov['race_key'].nunique():,}경주 "
          f"· 조교 자료 커버 {have:.0%}")

    base = run(df, drop=True, from_date=args.from_date)
    with_tr = run(df, drop=False, from_date=args.from_date)

    print(f"\n{'':<12}{'경주':>8}{'단승':>9}{'연승':>9}{'3순위내':>10}{'AUC':>9}")
    print("-" * 58)
    for name, r in (("조교 제외", base), ("조교 포함", with_tr)):
        print(f"{name:<12}{r['n_races']:>8,}{r['win']:>9.1%}{r['place']:>9.1%}"
              f"{r['top3']:>10.1%}{r['auc']:>9.4f}")
    d_win = with_tr["win"] - base["win"]
    d_auc = with_tr["auc"] - base["auc"]
    print(f"{'차이':<12}{'':>8}{d_win*100:>+8.1f}p"
          f"{(with_tr['place']-base['place'])*100:>+8.1f}p"
          f"{(with_tr['top3']-base['top3'])*100:>+9.1f}p{d_auc:>+9.4f}")

    print()
    if d_auc > 0.004 or d_win > 0.01:
        print("  → 조교 피처가 성능을 올린다")
    elif d_auc < -0.004 or d_win < -0.01:
        print("  → 조교 피처가 오히려 성능을 떨어뜨린다. 빼는 게 맞다")
    else:
        print("  → 유의미한 차이가 없다. 지금 형태로는 도움이 되지 않는다")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
