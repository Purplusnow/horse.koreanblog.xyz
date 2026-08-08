"""신뢰도 등급 임계값 보정.

기존 임계값(72/52)은 앵커링 없는 시뮬 분포에서 손으로 잡은 상수였다. 실제
사이트는 모델 승률에 앵커링한 시뮬에서 신뢰도를 뽑으므로 분포가 완전히 다르고,
그 결과 어떤 경주도 '강승부'가 되지 못했다. 여기서는 **운영과 동일한 경로**를
과거 경주에 그대로 돌려 점수 분포를 얻고, 백분위로 임계값을 다시 잡는다.

임계값을 정하는 것보다 중요한 건 그 임계값에서 등급이 **실제로 갈리는지** 다.
등급별 적중률이 단조적으로 오르지 않으면 그 지표는 내보내면 안 된다.

    python tools/calibrate_confidence.py --races 900
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
from horseai.simulate import build_runners, confidence, fit_noise, simulate  # noqa: E402


def score_races(pred: pd.DataFrame, keys, n_sims: int) -> pd.DataFrame:
    rows = []
    for k in keys:
        sub = pred[pred["race_key"] == k].sort_values("pred_rank")
        if len(sub) < 6:
            continue
        dist = pd.to_numeric(sub["distance"], errors="coerce").dropna()
        if dist.empty or dist.iloc[0] <= 0:
            continue
        ordn = pd.to_numeric(sub["ord"], errors="coerce").to_numpy()
        if (ordn == 1).sum() != 1:
            continue

        distance = float(dist.iloc[0])
        runners = build_runners(sub.to_dict("records"))
        target = sub["p_win_norm"].tolist()
        # 운영과 동일: 게재 승률에 맞춰 변동폭을 보정한 뒤 대표 시나리오를 뽑는다
        noise = fit_noise(runners, distance, target)
        sim = simulate(runners, distance, n_sims=n_sims,
                       noise_scale=noise, scenario_winner=0)
        conf = confidence(sim)

        odds = pd.to_numeric(sub["win_odds"], errors="coerce").to_numpy()
        # 같은 경주에서 시장 1인기는 어땠는가. '강승부'가 그저 인기마가 뻔한
        # 경주를 골라낸 것뿐이라면 시장도 같이 올라가므로 여기서 드러난다.
        mkt = int(np.nanargmin(odds)) if np.isfinite(odds).any() else -1
        rows.append({
            "mkt_hit": float(ordn[mkt] == 1) if mkt >= 0 else np.nan,
            "race_key": k,
            "conf": conf["score"],
            # 사이트가 게재하는 건 모델의 1순위다. 신뢰도는 그 예상이 맞을
            # 가능성을 말해야 하므로, 평가 대상도 모델 1순위여야 한다.
            "hit_win": float(ordn[0] == 1),
            "hit_place": float(ordn[0] <= 3),
            "hit_top3": float(any(ordn[i] == 1 for i in range(min(3, len(sub))))),
            "odds": odds[0] if np.isfinite(odds[0]) else np.nan,
            "field": len(sub),
        })
    return pd.DataFrame(rows)


def tier_table(t: pd.DataFrame, hi: float, lo: float) -> pd.DataFrame:
    def label(c):
        return "강승부" if c >= hi else ("중승부" if c >= lo else "약승부")
    t = t.assign(label=t["conf"].map(label))
    out = []
    for lab in ("강승부", "중승부", "약승부"):
        s = t[t["label"] == lab]
        if s.empty:
            continue
        out.append({
            "등급": lab,
            "경주": len(s),
            "비중": len(s) / len(t),
            "단승": s["hit_win"].mean(),
            "연승": s["hit_place"].mean(),
            "3순위내": s["hit_top3"].mean(),
            "시장": s["mkt_hit"].mean(),
            "회수율": float((s["hit_win"] * s["odds"].fillna(0)).sum()
                          / max(1, s["odds"].notna().sum())),
        })
    return pd.DataFrame(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/horseai.sqlite")
    ap.add_argument("--races", type=int, default=900)
    ap.add_argument("--sims", type=int, default=600)
    ap.add_argument("--top-pct", type=float, default=0.20, help="강승부 상위 비중")
    ap.add_argument("--mid-pct", type=float, default=0.55, help="강+중 누적 비중")
    ap.add_argument("--save", default="", help="결과를 metrics.json 에 병합 (예: models/metrics.json)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    df = build_training_frame(conn)
    conn.close()

    # 워크포워드 = 학습에 쓰이지 않은 구간의 예측. 누수 없이 운영 상황을 재현한다.
    _, pred = walk_forward(df)
    pred = pred.sort_values("rc_date")
    keys = pred["race_key"].drop_duplicates().to_numpy()[-args.races:]
    print(f"\n대상 {len(keys):,}경주 · 시뮬 {args.sims}회/경주")

    t = score_races(pred, keys, args.sims)
    if t.empty:
        print("평가 가능한 경주가 없습니다.")
        return 1

    q = t["conf"].quantile([0.1, 0.25, 0.5, 0.75, 0.8, 0.9, 0.95]).round(1)
    print(f"\n점수 분포 (n={len(t):,})  최소 {t['conf'].min():.0f} · "
          f"중앙 {t['conf'].median():.0f} · 최대 {t['conf'].max():.0f}")
    print("  " + " · ".join(f"p{int(k*100)} {v:.0f}" for k, v in q.items()))

    hi = float(t["conf"].quantile(1 - args.top_pct))
    lo = float(t["conf"].quantile(1 - args.mid_pct))
    print(f"\n제안 임계값  강승부 ≥ {hi:.0f} · 중승부 ≥ {lo:.0f}")

    tab = tier_table(t, hi, lo)
    print(f"\n{'등급':<7}{'경주':>7}{'비중':>8}{'단승':>9}{'시장':>9}{'우위':>8}{'연승':>9}{'회수율':>9}")
    print("-" * 68)
    for r in tab.itertuples():
        edge = r.단승 - r.시장
        print(f"{r.등급:<7}{r.경주:>7,}{r.비중:>8.0%}{r.단승:>9.1%}"
              f"{r.시장:>9.1%}{edge*100:>+7.1f}p{r.연승:>9.1%}{r.회수율:>9.1%}")

    strong = tab[tab["등급"] == "강승부"]
    weak = tab[tab["등급"] == "약승부"]
    if len(strong) and len(weak):
        d = float(strong["단승"].iloc[0] - weak["단승"].iloc[0])
        verdict = "지표가 작동한다" if d >= 0.04 else "분리력이 약하다 — 게재 보류 검토"
        print(f"\n  강승부 − 약승부 = {d*100:+.1f}%p → {verdict}")

    print(f"\n전체 평균 단승 {t['hit_win'].mean():.1%} · 연승 {t['hit_place'].mean():.1%}")

    if args.save:
        # 화면에 띄우는 수치는 반드시 재현 가능한 산출물에서 나와야 한다.
        # 손으로 옮겨 적은 숫자는 다음 재학습 때 조용히 낡는다.
        import json
        path = Path(args.save)
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            blob = {}
        blob["confidence"] = {
            "n_races": int(len(t)),
            "thresholds": {"강승부": round(hi), "중승부": round(lo)},
            "tiers": [
                {"label": r.등급, "n_races": int(r.경주), "share": round(r.비중, 4),
                 "hit_win": round(r.단승, 4), "hit_place": round(r.연승, 4)}
                for r in tab.itertuples()
            ],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  → {path} 에 저장 (시장 대비 수치는 저장하지 않음 · 내부 관리)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
