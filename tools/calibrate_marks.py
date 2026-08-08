"""예상 기호 검증 · ★ 기준 보정.

기호는 국내 예상지 관습대로 자리 수가 고정돼 있다.

    기본       ◎ ◎ ○ △ ※
    우세 뚜렷   ★ ◎ ○ △ ※

자리가 고정이므로 ◎○△※ 는 '경주 안에서의 상대 순위'다. 여기서 검증할 것은
두 가지다.

  1. **순위가 실제로 갈리는가** — 기호가 셀수록 착순이 좋아야 한다. 그렇지
     않으면 순위 자체가 틀린 것이다.
  2. **★ 가 약속을 지키는가** — ★ 는 유일하게 절대적인 주장("이 경주는 축이
     뚜렷하다")이므로, 실제 1착·2착 이내 비율이 그에 걸맞아야 하고 동시에
     너무 흔하면 안 된다. 매 경주에 ★ 가 뜨면 아무 말도 하지 않는 것과 같다.

    python tools/calibrate_marks.py --scan          # ★ 기준 후보 훑기
    python tools/calibrate_marks.py --save models/metrics.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from horseai import site  # noqa: E402
from horseai.features import build_training_frame  # noqa: E402
from horseai.model import walk_forward  # noqa: E402

MARKS = ["★", "◎", "○", "△", "※"]


def apply_marks(pred: pd.DataFrame, star: float) -> pd.Series:
    """기호 부여는 site.assign_marks 를 그대로 쓴다.

    규칙을 여기에 다시 구현하면 언젠가 사이트와 갈리고, 그때 이 검증 결과는
    화면에서 실제로 벌어지는 일과 무관한 숫자가 된다.
    """
    site.MARK_THRESHOLDS = {"star": star}
    marks = pd.Series("", index=pred.index, dtype=object)
    for _, g in pred.groupby("race_key", sort=False):
        rows = [{"pred_rank": r.pred_rank,
                 "p_top2": getattr(r, "p_top2_norm", 0) or 0,
                 "_idx": r.Index} for r in g.itertuples()]
        site.assign_marks(rows)
        for row in rows:
            marks.at[row["_idx"]] = row["mark"]
    return marks


def summarize(pred: pd.DataFrame, star: float) -> pd.DataFrame:
    d = pred.assign(mark=apply_marks(pred, star))
    d = d[d["mark"] != ""]
    n_races = pred["race_key"].nunique()
    rows = []
    for m in MARKS:
        s = d[d["mark"] == m]
        if s.empty:
            continue
        o = pd.to_numeric(s["ord"], errors="coerce")
        rows.append({
            "기호": m,
            "두수": len(s),
            "출현율": s["race_key"].nunique() / n_races,
            "1착": float((o == 1).mean()),
            "2착이내": float((o <= 2).mean()),
            "3착이내": float((o <= 3).mean()),
        })
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/horseai.sqlite")
    ap.add_argument("--star", type=float, default=0.58, help="★ 기준 (1순위 p_top2)")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--save", default="")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    df = build_training_frame(conn)
    conn.close()
    _, pred = walk_forward(df)
    pred = pred[pd.to_numeric(pred["ord"], errors="coerce").notna()]
    n_races = pred["race_key"].nunique()
    base = pred.groupby("race_key")["hr_no"].count().mean()
    print(f"\n시간순 교차검증 {n_races:,}경주 · 평균 {base:.1f}두")
    print(f"무작위로 찍으면 1착 {1/base:.1%} · 2착이내 {2/base:.1%} · 3착이내 {3/base:.1%}")

    if args.scan:
        print(f"\n── ★ 기준 후보 ──")
        print(f"{'기준':>6}{'★ 출현':>9}{'★ 1착':>9}{'★ 2착이내':>11}{'◎ 1착':>9}")
        for st in (0.50, 0.54, 0.58, 0.62, 0.66, 0.70):
            tab = summarize(pred, st)
            star = tab[tab["기호"] == "★"]
            circ = tab[tab["기호"] == "◎"]
            if star.empty:
                print(f"{st:>6.2f}      (해당 경주 없음)")
                continue
            s0, c0 = star.iloc[0], circ.iloc[0]
            print(f"{st:>6.2f}{s0['출현율']:>9.0%}{s0['1착']:>9.1%}"
                  f"{s0['2착이내']:>11.1%}{c0['1착']:>9.1%}")
        return 0

    tab = summarize(pred, args.star)
    print(f"\n★ 기준: 1순위 p_top2 ≥ {args.star}")
    print(f"\n{'기호':<5}{'두수':>8}{'출현율':>9}{'1착':>9}{'2착이내':>10}{'3착이내':>10}")
    print("-" * 52)
    for r in tab.itertuples():
        print(f"{r.기호:<5}{r.두수:>8,}{r.출현율:>9.0%}{r._4:>9.1%}{r._5:>10.1%}{r._6:>10.1%}")

    # 약속 점검 — 어긋나면 그대로 말한다
    print()
    star = tab[tab["기호"] == "★"]
    if star.empty:
        print("  ⚠ ★ 가 한 경주도 나오지 않는다 — 기준이 너무 높다")
    else:
        s0 = star.iloc[0]
        if s0["2착이내"] < 0.60:
            print(f"  ⚠ ★ 의 2착 이내 {s0['2착이내']:.1%} — '우세가 뚜렷'이라 하기 어렵다")
        if s0["출현율"] > 0.35:
            print(f"  ⚠ ★ 가 경주의 {s0['출현율']:.0%} 에 등장 — 너무 흔하면 아무 말도 하지 않는 셈")
    # 기호가 셀수록 착순이 좋아지는가
    order = [r._6 for r in tab.itertuples()]           # 3착이내
    if order != sorted(order, reverse=True):
        print(f"  ⚠ 기호 순서와 실제 착순이 어긋난다: {[f'{x:.1%}' for x in order]}")
    else:
        print("  ✓ 기호가 셀수록 착순이 좋아진다 (단조)")

    if args.save:
        path = Path(args.save)
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            blob = {}
        blob["marks"] = {
            "n_races": int(n_races),
            "star_threshold": args.star,
            "levels": [
                {"mark": r.기호, "share_of_races": round(r.출현율, 4),
                 "hit_win": round(r._4, 4), "hit_top2": round(r._5, 4),
                 "hit_top3": round(r._6, 4)}
                for r in tab.itertuples()
            ],
        }
        path.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  → {path} 저장")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
