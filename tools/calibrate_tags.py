"""승식 태그 기준값 보정 — **보류된 기능** (2026-08-08 조사).

결론부터: 태그는 달지 않기로 했다. 이 파일은 그 판단의 근거를 남기려고 둔다.

6,105경주(2024-02~2026-08) 시간순 교차검증에서 세 지표 모두 단조로 작동했다.
상위 20% 지점 기준 적중률은 단승 47.6%(기준선 31.3%), 복승 21.5%(13.3%),
삼복승 12.3%(7.5%). 즉 **적중률을 올리는 것은 사실이다.**

그런데 8월 8일 17경주에 적용하니 태그 6개가 모두 불발이었고, 그날 전 승식을
적중시킨(삼쌍승 350배) 제주 5R 은 삼복승 지표가 -0.083 으로 최하위권이라 태그가
하나도 붙지 않았다. 표본 6통으로 증명되는 것은 없지만, 방향이 설명된다 —
태그된 경주는 본질적으로 1순위가 강한 경주라 적중률은 오르고 배당은 낮다.
시장도 같은 것을 보기 때문이다. 환수율을 만드는 것은 예상 밖의 고배당이다.

**적중률 향상은 확인됐고 환수율 향상은 확인되지 않았다.** 과거 배당 자료가 없어
지금은 검증할 방법이 없다. 배당이 충분히 쌓이면 이 스크립트로 다시 확인하고,
그때도 환수율이 따라오지 않으면 이 기능은 버린다.


경주마다 '어느 승식에 무게를 둘지' 태그를 붙이려면 기준값이 필요하다. 그 값을
눈대중으로 정하면 태그는 그냥 문구가 된다. 여기서는 **시간순 교차검증으로 전
기간 예측을 복원해** 각 후보 지표가 실제로 그 승식의 적중률을 끌어올리는지
확인하고, 끌어올린다면 어느 지점부터인지 찾는다.

게재된 예측만으로는 못 한다 — 사이트를 연 이후 경주밖에 없어 표본이 열몇 개다.

후보 지표는 셋이다.
  * **p1**     1순위 우승 확률 (경주 내 정규화)         → 단승
  * **t2sum**  1·2순위 입상 확률의 합                   → 복승
  * **t3gap**  3순위 입상 확률에서 4순위를 뺀 값        → 삼복승

    python tools/calibrate_tags.py
    python tools/calibrate_tags.py --save models/metrics.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from horseai.features import build_training_frame  # noqa: E402
from horseai.model import walk_forward  # noqa: E402

# (지표, 판정할 승식, 화면에 쓸 이름)
CANDIDATES = [
    ("p1", "win", "단승"),
    ("t2sum", "qnl", "복승"),
    ("t3gap", "tla", "삼복승"),
]
QUANTILES = (0.60, 0.70, 0.80, 0.90, 0.95)


def race_frame(pred: pd.DataFrame) -> pd.DataFrame:
    """경주 한 줄 = 지표들 + 승식별 적중 여부."""
    pred = pred[pd.to_numeric(pred["ord"], errors="coerce").notna()]
    rows = []
    for key, g in pred.groupby("race_key", sort=False):
        g = g.sort_values("pred_rank")
        o = pd.to_numeric(g["ord"], errors="coerce").to_numpy()
        if (o == 1).sum() != 1 or len(o) < 4:
            continue
        pw = pd.to_numeric(g["p_win_norm"], errors="coerce").to_numpy()
        pt = pd.to_numeric(g.get("p_top2_norm", g["p_win_norm"]), errors="coerce").to_numpy()
        if np.isnan(pw).any() or np.isnan(pt).any():
            continue
        rows.append({
            "race_key": key,
            "rc_date": g["rc_date"].iloc[0],
            "p1": pw[0],
            "t2sum": pt[0] + pt[1],
            "t3gap": pt[2] - pt[3],
            "win": bool(o[0] == 1),
            "qnl": bool(set(o[:2]) <= {1, 2}),
            "tla": bool(set(o[:3]) <= {1, 2, 3}),
        })
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/horseai.sqlite")
    ap.add_argument("--save", default="")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    df = build_training_frame(conn)
    conn.close()
    _, pred = walk_forward(df)
    R = race_frame(pred)

    print(f"\n시간순 교차검증 {len(R):,}경주 · {R['rc_date'].min()} ~ {R['rc_date'].max()}")
    print(f"기준선 — 단승 {R.win.mean():.1%} · 복승 {R.qnl.mean():.1%} "
          f"· 삼복승 {R.tla.mean():.1%}")

    picked = {}
    for col, bet, label in CANDIDATES:
        base = R[bet].mean()
        print(f"\n── {label} (지표 {col}) ──")
        print(f"{'기준':>8}{'경주':>9}{'비중':>8}{'적중률':>9}{'기준선 대비':>12}")
        best = None
        for q in QUANTILES:
            th = float(R[col].quantile(q))
            sel = R[R[col] >= th]
            if len(sel) < 30:            # 표본이 얇으면 기준으로 삼지 않는다
                continue
            hit = float(sel[bet].mean())
            print(f"{th:>8.3f}{len(sel):>9,}{len(sel)/len(R):>8.0%}"
                  f"{hit:>9.1%}{(hit-base)*100:>+11.1f}p")
            # 표본을 지나치게 줄이지 않으면서 개선폭이 가장 큰 지점
            if best is None or hit - base > best[1]:
                best = (th, hit - base, len(sel), hit)
        if best and best[1] > 0.02:
            picked[label] = {"metric": col, "threshold": round(best[0], 4),
                             "share": round(best[2] / len(R), 4),
                             "hit_rate": round(best[3], 4), "base": round(base, 4)}
            print(f"  → 채택: {col} ≥ {best[0]:.3f} "
                  f"({best[2]/len(R):.0%} 경주, {best[3]:.1%} vs 기준선 {base:.1%})")
        else:
            print(f"  → 기준선을 의미 있게 넘기는 지점이 없다. {label} 태그는 달지 않는다")

    if args.save and picked:
        path = Path(args.save)
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            blob = {}
        blob["bet_tags"] = {"n_races": int(len(R)), "levels": picked}
        path.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  → {path} 저장")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
