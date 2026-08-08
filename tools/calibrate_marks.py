"""예상 기호 임계값 보정.

기호는 순위표가 아니라 **약속**이다.

    ◎  2착 이내가 유력하다
    ○  3착 이내가 유력하다
    △  조건이 맞으면 3착 이내
    ※  그 아래 참고

약속을 걸었으면 지켜지는지 재야 한다. 여기서는 시간순 교차검증 예측에 임계값을
씌워 **그 기호를 받은 말이 실제로 그 착순에 든 비율**을 본다. ◎ 를 받은 말이
2착 이내에 반도 못 들면 그 기호는 거짓말이고, 반대로 임계값이 너무 높으면
경주 대부분에 ◎ 가 없어 예상지 구실을 못 한다. 둘 사이를 잡는 것이 목적이다.

    python tools/calibrate_marks.py --save models/metrics.json
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
from horseai import site  # noqa: E402

LEVELS = ["◎", "○", "△", "※"]


def evaluate(pred: pd.DataFrame, t2: float, t3: float, t3w: float) -> pd.DataFrame:
    """기호 부여는 site.assign_marks 를 그대로 쓴다.

    규칙을 여기에 다시 구현하면 언젠가 사이트와 갈리고, 그때 이 보정 결과는
    화면에서 실제로 벌어지는 일과 무관한 숫자가 된다.
    """
    site.MARK_THRESHOLDS = {"top2": t2, "top3": t3, "top3_weak": t3w}
    marks = pd.Series("", index=pred.index, dtype=object)
    for _, g in pred.groupby("race_key", sort=False):
        rows = [{"pred_rank": r.pred_rank,
                 "p_top2": getattr(r, "p_top2_norm", 0),
                 "p_place": getattr(r, "p_top3_norm", 0),
                 "_idx": r.Index} for r in g.itertuples()]
        site.assign_marks(rows)
        for row in rows:
            marks.at[row["_idx"]] = row["mark"]

    d = pred.assign(mark=marks)
    d = d[d["mark"] != ""]
    ordn = pd.to_numeric(d["ord"], errors="coerce")
    n_races = pred["race_key"].nunique()

    rows = []
    for m in LEVELS:
        s = d[d["mark"] == m]
        if s.empty:
            continue
        o = pd.to_numeric(s["ord"], errors="coerce")
        rows.append({
            "기호": m,
            "두수": len(s),
            "경주당": len(s) / n_races,
            # 그 기호가 나온 경주 비율 — ◎ 가 어느 경주에나 있으면 변별력이 없고,
            # 거의 없으면 예상지로 못 쓴다.
            "출현율": s["race_key"].nunique() / n_races,
            "2착이내": float((o <= 2).mean()),
            "3착이내": float((o <= 3).mean()),
            "1착": float((o == 1).mean()),
        })
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/horseai.sqlite")
    ap.add_argument("--save", default="", help="확정 임계값을 metrics.json 에 병합")
    ap.add_argument("--t2", type=float, default=0.46)
    ap.add_argument("--t3", type=float, default=0.52)
    ap.add_argument("--t3w", type=float, default=0.33)
    ap.add_argument("--scan", action="store_true", help="후보 임계값을 훑어본다")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    df = build_training_frame(conn)
    conn.close()
    _, pred = walk_forward(df)
    pred = pred[pd.to_numeric(pred["ord"], errors="coerce").notna()]
    n_races = pred["race_key"].nunique()
    print(f"\n시간순 교차검증 {n_races:,}경주 / {len(pred):,}두")

    base = pred.groupby("race_key")["hr_no"].count().mean()
    print(f"평균 출주 {base:.1f}두 → 무작위로 찍었을 때 2착이내 {2/base:.1%} · 3착이내 {3/base:.1%}")

    if args.scan:
        print("\n── ◎ 임계값 후보 (1순위 p_top2) ──")
        print(f"{'임계값':>7}{'출현율':>9}{'2착이내':>10}{'1착':>9}")
        for t2 in (0.38, 0.42, 0.46, 0.50, 0.55, 0.60):
            tab = evaluate(pred, t2, args.t3, args.t3w)
            r = tab[tab["기호"] == "◎"]
            if r.empty:
                continue
            r = r.iloc[0]
            print(f"{t2:>7.2f}{r['출현율']:>9.0%}{r['2착이내']:>10.1%}{r['1착']:>9.1%}")

        print("\n── ○ 임계값 후보 (p_top3) ──")
        print(f"{'임계값':>7}{'경주당':>9}{'3착이내':>10}")
        for t3 in (0.42, 0.46, 0.50, 0.54, 0.58):
            tab = evaluate(pred, args.t2, t3, args.t3w)
            r = tab[tab["기호"] == "○"]
            if r.empty:
                continue
            r = r.iloc[0]
            print(f"{t3:>7.2f}{r['경주당']:>9.2f}{r['3착이내']:>10.1%}")
        return 0

    tab = evaluate(pred, args.t2, args.t3, args.t3w)
    print(f"\n임계값  ◎ p_top2≥{args.t2}  ○ p_top3≥{args.t3}  △ p_top3≥{args.t3w}")
    print(f"\n{'기호':<5}{'두수':>8}{'경주당':>8}{'출현율':>9}{'1착':>8}{'2착이내':>9}{'3착이내':>9}")
    print("-" * 56)
    for r in tab.itertuples():
        print(f"{r.기호:<5}{r.두수:>8,}{r.경주당:>8.2f}{r.출현율:>9.0%}"
              f"{r._7:>8.1%}{r._5:>9.1%}{r._6:>9.1%}")

    # 약속 점검 — 어긋나면 그대로 말한다
    print()
    for r in tab.itertuples():
        if r.기호 == "◎" and r._5 < 0.50:
            print(f"  ⚠ ◎ 의 2착 이내 비율 {r._5:.1%} — '유력'이라 하기 어렵다. 임계값을 올릴 것")
        if r.기호 == "○" and r._6 < 0.45:
            print(f"  ⚠ ○ 의 3착 이내 비율 {r._6:.1%} — 임계값을 올릴 것")
        if r.기호 == "△" and r._6 < 3 / base:
            print(f"  ⚠ △ 의 3착 이내 비율 {r._6:.1%} 이 무작위({3/base:.1%})보다 낮다")

    if args.save:
        path = Path(args.save)
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            blob = {}
        blob["marks"] = {
            "n_races": int(n_races),
            "thresholds": {"top2": args.t2, "top3": args.t3, "top3_weak": args.t3w},
            "levels": [
                {"mark": r.기호, "share_of_races": round(r.출현율, 4),
                 "per_race": round(r.경주당, 3), "hit_win": round(r._7, 4),
                 "hit_top2": round(r._5, 4), "hit_top3": round(r._6, 4)}
                for r in tab.itertuples()
            ],
        }
        path.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  → {path} 저장")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
