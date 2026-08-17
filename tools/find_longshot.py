"""6순위 이하에서 깜짝 입상하는 말의 특징을 찾는다.

추천 5두 밖의 말 중에도 3착 안에 드는 경우가 꾸준히 있다. 그런 말을 미리
짚어낼 수 있으면 삼복승·복연승에서 값어치가 크다. 다만 '복병'을 눈대중으로
정의하면 그럴듯한 문구만 남는다 — 승식 태그에서 이미 겪었다.

그래서 정의를 먼저 정하지 않는다. 6순위 이하 전체를 모아 **입상한 쪽과 못 한
쪽이 무엇이 다른지** 재고, 차이가 큰 지표만 후보로 올린다.

    python tools/find_longshot.py
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from horseai.features import FEATURE_COLUMNS, build_training_frame  # noqa: E402
from horseai.model import walk_forward  # noqa: E402

# 이 순위 밖이 '추천 5두 밖'이다.
OUTSIDE_FROM = 6
# **비교 대상은 실제 예측에 쓰는 지표뿐이다.**
#
# 처음에는 수치형 열을 전부 훑었는데, 상위가 ord_pct·speed_fig·g1f_rank·
# win_odds 로 채워졌다. 전부 그 경주의 결과에서 나온 값이라 '입상한 말은
# 빨랐다'는 동어반복이다. 발주 전에 알 수 있는 것만 봐야 한다.


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/horseai.sqlite")
    ap.add_argument("--top", type=int, default=14, help="보여줄 지표 수")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    df = build_training_frame(conn)
    conn.close()
    _, pred = walk_forward(df)

    pred = pred[pd.to_numeric(pred["ord"], errors="coerce").notna()].copy()
    pred["ord_n"] = pd.to_numeric(pred["ord"], errors="coerce")
    out = pred[pred["pred_rank"] >= OUTSIDE_FROM].copy()
    out["hit"] = (out["ord_n"] <= 3).astype(int)

    n, k = len(out), int(out["hit"].sum())
    print(f"\n{OUTSIDE_FROM}순위 이하 {n:,}두 중 3착 이내 {k:,}두 ({k/n:.1%})")
    print(f"경주 {pred['race_key'].nunique():,}개 · 평균 {n/pred['race_key'].nunique():.1f}두/경주\n")

    # 수치형 지표만 비교한다. 입상한 쪽과 못 한 쪽의 평균 차이를 표준편차로
    # 나눠(효과크기) 정렬한다 — 단위가 다른 지표를 같은 자로 재기 위해서다.
    cols = [c for c in FEATURE_COLUMNS if c in out.columns]
    num = out[cols].select_dtypes(include=[np.number])
    a, b = num[out["hit"] == 1], num[out["hit"] == 0]
    rows = []
    for c in num.columns:
        x, y = a[c].dropna(), b[c].dropna()
        if len(x) < 200 or len(y) < 200:
            continue
        sd = np.sqrt((x.var() + y.var()) / 2)
        if not sd or np.isnan(sd):
            continue
        rows.append({"지표": c, "입상": x.mean(), "불발": y.mean(),
                     "효과크기": (x.mean() - y.mean()) / sd})
    t = pd.DataFrame(rows).sort_values("효과크기", key=abs, ascending=False)

    print(f"{'지표':<26}{'입상 평균':>12}{'불발 평균':>12}{'효과크기':>10}")
    print("-" * 60)
    for r in t.head(args.top).itertuples():
        print(f"{r.지표:<26}{r.입상:>12.3f}{r.불발:>12.3f}{r.효과크기:>+10.3f}")

    # 가장 센 지표 몇 개로 실제 입상률이 갈리는지 확인한다. 효과크기가 커도
    # 상위 구간에서 입상률이 안 오르면 화면에 쓸 수 없다.
    print("\n\n상위 지표별 — 그 지표 상위 10%의 3착 이내 비율")
    print(f"{'지표':<26}{'상위10% 입상률':>16}{'기준선 대비':>12}")
    print("-" * 56)
    base = k / n
    for r in t.head(8).itertuples():
        col = out[r.지표]
        hi = out[col >= col.quantile(0.9)] if r.효과크기 > 0 else out[col <= col.quantile(0.1)]
        if len(hi) < 200:
            continue
        rate = hi["hit"].mean()
        print(f"{r.지표:<26}{rate:>15.1%}{(rate - base) * 100:>+11.1f}p")
    print(f"\n기준선(6순위 이하 전체) {base:.1%}")

    # ── 전용 모델 ──────────────────────────────────────────────
    # 개별 지표는 하나씩만 본 것이라 서로 겹치지 않는 정보를 못 쓴다. '6순위
    # 이하 중 3착 이내'만 맞히는 작은 모델을 따로 세워 얼마나 나아지는지 본다.
    #
    # 이 모델도 시간순으로 가른다. 무작위로 나누면 같은 경주의 다른 말이 학습에
    # 섞여 성능이 부풀려진다 — 본 모델에서 이미 지키는 규칙이다.
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import roc_auc_score

    # 문자열 지표(거리대 등)는 코드로 바꾼다. 본 모델은 파이프라인에서 처리하는데
    # 여기서는 직접 쓰므로 같은 일을 해 줘야 한다.
    num_cols = [c for c in cols if pd.api.types.is_numeric_dtype(out[c])]
    cat_cols = [c for c in cols if c not in num_cols]
    X = out[num_cols].copy()
    for c in cat_cols:
        X[c] = out[c].astype("category").cat.codes
    print(f"  지표 {len(num_cols)}개 + 범주형 {len(cat_cols)}개")

    out = out.sort_values("rc_date")
    X = X.loc[out.index]
    cut = out["rc_date"].quantile(0.6)
    m_tr = out["rc_date"] <= cut
    tr, te = out[m_tr], out[~m_tr]
    X_tr, X_te = X[m_tr.values], X[~m_tr.values]
    y_tr, y_te = tr["hit"], te["hit"]
    print(f"\n\n── 복병 전용 모델 ──")
    print(f"학습 {len(tr):,}두 (~{str(cut)[:10]}) · 검증 {len(te):,}두")

    m = HistGradientBoostingClassifier(
        max_iter=300, learning_rate=0.06, max_leaf_nodes=24,
        min_samples_leaf=60, l2_regularization=1.0, random_state=0)
    m.fit(X_tr, y_tr)
    p_hit = m.predict_proba(X_te)[:, 1]
    te = te.assign(p_hit=p_hit)
    print(f"AUC {roc_auc_score(y_te, p_hit):.4f} · 검증 기준선 {y_te.mean():.1%}")

    print(f"\n{'선정 방식':<28}{'경주':>8}{'입상률':>9}{'기준선 대비':>12}")
    print("-" * 58)
    # 경주마다 확률이 가장 높은 한 마리만 고른다 — 화면에 낼 방식 그대로다.
    pick = te.loc[te.groupby("race_key")["p_hit"].idxmax()]
    r0 = pick["hit"].mean()
    print(f"{'경주당 1두 (전 경주)':<28}{len(pick):>8,}{r0:>8.1%}{(r0-y_te.mean())*100:>+11.1f}p")

    # 확률이 낮으면 아예 내지 않는 편이 낫다. 기준을 올려 가며 본다.
    for q in (0.5, 0.7, 0.85):
        th = pick["p_hit"].quantile(q)
        sel = pick[pick["p_hit"] >= th]
        r = sel["hit"].mean()
        print(f"{f'  확률 상위 {(1-q)*100:.0f}% 경주만':<28}{len(sel):>8,}{r:>8.1%}"
              f"{(r-y_te.mean())*100:>+11.1f}p")

    print(f"\n비교 — 단일 지표(speed_last_pct) 상위 10%: "
          f"{te[te['speed_last_pct'] >= te['speed_last_pct'].quantile(0.9)]['hit'].mean():.1%}")

    # ── 자리를 바꿀 것인가 ────────────────────────────────────
    # 복병을 5두 안에 넣으려면 5순위를 빼야 한다. 그러려면 복병이 5순위보다
    # 자주 입상해야 한다. 만약 그렇다면 교체 문제가 아니라 **본 모델의 순위
    # 매김이 틀렸다**는 뜻이므로, 그쪽이 훨씬 중요한 발견이다.
    pred["ord_n"] = pd.to_numeric(pred["ord"], errors="coerce")
    same = pred[pred["rc_date"] > cut]                    # 같은 검증 구간
    print("\n\n── 5두 안팎 비교 (같은 검증 구간) ──")
    print(f"{'대상':<26}{'두수':>8}{'3착 이내':>10}")
    print("-" * 46)
    for rank in (1, 2, 3, 4, 5):
        g = same[same["pred_rank"] == rank]
        print(f"{f'{rank}순위':<26}{len(g):>8,}{(g['ord_n'] <= 3).mean():>9.1%}")
    top30 = pick[pick["p_hit"] >= pick["p_hit"].quantile(0.7)]
    print(f"{'복병 (상위 30% 경주)':<26}{len(top30):>8,}{top30['hit'].mean():>9.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
