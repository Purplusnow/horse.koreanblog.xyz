"""승률 모델 학습 · 워크포워드 검증 · 추론.

모델 자체는 평범한 그래디언트 부스팅이다. 이 파일에서 정말 중요한 건 **정직한
평가**다. 경마 예측은 "그럴듯한 숫자"를 만들기는 쉽고 실제로 쓸모 있기는 어렵다.
그래서 두 가지를 강제한다.

1. **워크포워드 검증** — 시간 순서대로 과거로 학습해 미래를 맞힌다. 무작위 분할은
   같은 경주의 다른 말이 학습셋에 들어가 성능을 부풀린다.
2. **시장(배당률) 베이스라인 동시 측정** — 1인기(최저 단승배당)를 그대로 찍었을
   때의 적중률을 같이 낸다. 이걸 못 이기면 예상 사이트로서 존재 이유가 없다.
   배당률은 피처로 쓰지 않으므로 이 비교는 공정하다.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import log_loss, roc_auc_score

from .features import CATEGORICAL, FEATURE_COLUMNS, build_training_frame

log = logging.getLogger(__name__)

MODEL_DIR = Path("models")
MODEL_PATH = MODEL_DIR / "model.joblib"
METRICS_PATH = MODEL_DIR / "metrics.json"

MODEL_VERSION = "v1"


def _make_estimator(seed: int = 42) -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
        max_iter=400,
        learning_rate=0.05,
        max_depth=6,
        min_samples_leaf=40,
        l2_regularization=1.0,
        early_stopping=True,
        validation_fraction=0.12,
        n_iter_no_change=30,
        categorical_features=[FEATURE_COLUMNS.index(c) for c in CATEGORICAL],
        random_state=seed,
    )


def _matrix(df: pd.DataFrame) -> pd.DataFrame:
    """FEATURE_COLUMNS 순서를 고정한 입력 행렬. 범주형은 코드로 인코딩."""
    X = df.reindex(columns=FEATURE_COLUMNS).copy()
    for c in CATEGORICAL:
        X[c] = pd.Categorical(X[c].astype(str)).codes.astype(float)
    return X.astype(float)


def normalize_within_race(df: pd.DataFrame, prob_col: str, out_col: str) -> pd.DataFrame:
    """경주 내 확률 합이 1이 되도록 정규화.

    이진 분류기는 말 단위로 독립 예측하므로 합이 1이 아니다. 경주는 정확히 한
    마리만 이기므로 정규화해야 배당률과 비교 가능한 확률이 된다.
    """
    df = df.copy()
    p = pd.to_numeric(df[prob_col], errors="coerce").clip(1e-6, 1 - 1e-6)
    total = p.groupby(df["race_key"]).transform("sum")
    df[out_col] = (p / total.replace(0, np.nan)).fillna(1.0 / df.groupby("race_key")["hr_no"].transform("count"))
    return df


# ---------------------------------------------------------------------------
# 평가
# ---------------------------------------------------------------------------

@dataclass
class Eval:
    n_races: int
    n_runners: int
    top1_win: float          # 1순위 추천마의 단승 적중률
    top1_top3: float         # 1순위 추천마가 3착 이내 (연승 적중률)
    top2_exacta_box: float   # 상위 2두가 실제 1·2착을 (순서 무관) 모두 차지
    top3_has_winner: float   # 상위 3두 안에 1착마 포함
    logloss: float
    auc: float
    roi_win: float           # 1순위 단승 100원 베팅 회수율
    # 시장 베이스라인 (1인기 = 최저 단승배당)
    mkt_top1_win: float
    mkt_top1_top3: float
    mkt_top3_has_winner: float
    mkt_roi_win: float
    coverage_odds: float     # 배당률이 있어 시장 비교가 가능한 경주 비율


def evaluate(pred: pd.DataFrame) -> Eval:
    """경주 단위 지표를 계산한다. pred 는 p_win_norm, ord, win_odds 를 포함."""
    df = pred.copy()
    df["ord"] = pd.to_numeric(df["ord"], errors="coerce")
    df["win_odds"] = pd.to_numeric(df["win_odds"], errors="coerce")

    # 1착마가 확인되는 경주만 평가 대상
    valid_keys = df.groupby("race_key")["ord"].apply(lambda s: (s == 1).sum() == 1)
    df = df[df["race_key"].isin(valid_keys[valid_keys].index)]
    if df.empty:
        raise ValueError("평가 가능한 경주가 없습니다 (1착 기록 부재).")

    df["model_rank"] = df.groupby("race_key")["p_win_norm"].rank(ascending=False, method="first")
    has_odds = df.groupby("race_key")["win_odds"].transform(lambda s: s.notna().sum() >= 2)
    df["mkt_rank"] = (
        df[has_odds].groupby("race_key")["win_odds"].rank(ascending=True, method="first")
        if has_odds.any() else np.nan
    )

    races = df["race_key"].nunique()

    def _pick_rate(rank_col: str, rank_val: int, cond) -> float:
        sub = df[df[rank_col] == rank_val]
        return float(cond(sub).mean()) if len(sub) else float("nan")

    top1_win = _pick_rate("model_rank", 1, lambda s: s["ord"] == 1)
    top1_top3 = _pick_rate("model_rank", 1, lambda s: s["ord"] <= 3)

    top3 = df[df["model_rank"] <= 3]
    top3_has_winner = float(
        top3.groupby("race_key")["ord"].apply(lambda s: (s == 1).any()).mean()
    )
    top2 = df[df["model_rank"] <= 2]
    top2_exacta = float(
        top2.groupby("race_key")["ord"].apply(lambda s: set(s.dropna()) == {1.0, 2.0}).mean()
    )

    y = (df["ord"] == 1).astype(int)
    p = df["p_win_norm"].clip(1e-6, 1 - 1e-6)
    try:
        ll = float(log_loss(y, p, labels=[0, 1]))
        auc = float(roc_auc_score(y, p))
    except ValueError:
        ll, auc = float("nan"), float("nan")

    # ROI: 1순위 단승 100원. 배당률이 있는 경주만.
    bet = df[(df["model_rank"] == 1) & df["win_odds"].notna()]
    roi = float(((bet["ord"] == 1) * bet["win_odds"]).sum() / len(bet)) if len(bet) else float("nan")

    mkt = df[df["mkt_rank"].notna()] if "mkt_rank" in df else df.iloc[0:0]
    m1 = mkt[mkt["mkt_rank"] == 1]
    mkt_top1_win = float((m1["ord"] == 1).mean()) if len(m1) else float("nan")
    mkt_top1_top3 = float((m1["ord"] <= 3).mean()) if len(m1) else float("nan")
    mkt_top3 = mkt[mkt["mkt_rank"] <= 3]
    mkt_top3_has_winner = (
        float(mkt_top3.groupby("race_key")["ord"].apply(lambda s: (s == 1).any()).mean())
        if len(mkt_top3) else float("nan")
    )
    mkt_roi = float(((m1["ord"] == 1) * m1["win_odds"]).sum() / len(m1)) if len(m1) else float("nan")

    return Eval(
        n_races=int(races),
        n_runners=int(len(df)),
        top1_win=top1_win,
        top1_top3=top1_top3,
        top2_exacta_box=top2_exacta,
        top3_has_winner=top3_has_winner,
        logloss=ll,
        auc=auc,
        roi_win=roi,
        mkt_top1_win=mkt_top1_win,
        mkt_top1_top3=mkt_top1_top3,
        mkt_top3_has_winner=mkt_top3_has_winner,
        mkt_roi_win=mkt_roi,
        coverage_odds=float(mkt["race_key"].nunique() / races) if races else 0.0,
    )


# ---------------------------------------------------------------------------
# 학습
# ---------------------------------------------------------------------------

def fit(df: pd.DataFrame, seed: int = 42) -> Dict[str, HistGradientBoostingClassifier]:
    X = _matrix(df)
    models: Dict[str, HistGradientBoostingClassifier] = {}
    for target, col in (("win", "y_win"), ("top2", "y_top2"), ("top3", "y_top3")):
        y = pd.to_numeric(df[col], errors="coerce")
        mask = y.notna()
        if mask.sum() < 500 or y[mask].nunique() < 2:
            log.warning("%s 레이블이 부족해 학습을 건너뜁니다 (%d건)", target, int(mask.sum()))
            continue
        m = _make_estimator(seed)
        m.fit(X[mask], y[mask].astype(int))
        models[target] = m
    return models


def predict_frame(models: Dict, df: pd.DataFrame) -> pd.DataFrame:
    X = _matrix(df)
    out = df.copy()
    out["p_win_raw"] = models["win"].predict_proba(X)[:, 1] if "win" in models else np.nan
    out["p_top2_raw"] = models["top2"].predict_proba(X)[:, 1] if "top2" in models else np.nan
    out["p_top3_raw"] = models["top3"].predict_proba(X)[:, 1] if "top3" in models else np.nan
    out = normalize_within_race(out, "p_win_raw", "p_win_norm")
    # 착순 확률은 경주당 자리 수가 정해져 있다 — 2착 이내는 두 자리, 3착 이내는
    # 세 자리. 개별 예측의 합이 그 자리 수가 되도록 경주 안에서 맞춰야 확률로
    # 읽을 수 있다. 기호 부여가 이 값의 절대 수준에 의존하므로 특히 중요하다.
    n = out.groupby("race_key")["hr_no"].transform("count")
    for raw, norm, slots in (("p_top2_raw", "p_top2_norm", 2),
                             ("p_top3_raw", "p_top3_norm", 3)):
        p = pd.to_numeric(out[raw], errors="coerce").clip(1e-6, 1 - 1e-6)
        total = p.groupby(out["race_key"]).transform("sum")
        scale = (np.minimum(slots, n) / total).replace([np.inf, -np.inf], 1)
        out[norm] = (p * scale).clip(0, 1)
    out["pred_rank"] = out.groupby("race_key")["p_win_norm"].rank(ascending=False, method="first").astype(int)
    return out


def walk_forward(df: pd.DataFrame, n_folds: int = 4, min_train_races: int = 1500,
                 seed: int = 42) -> Tuple[List[Eval], pd.DataFrame]:
    """시간순 확장 학습 검증."""
    df = df.sort_values("rc_date").reset_index(drop=True)
    dates = df["rc_date"].dropna().sort_values().unique()
    if len(dates) < 20:
        raise ValueError("검증에 필요한 경주일이 부족합니다.")

    # 마지막 절반을 n_folds 로 쪼개 순차 검증
    split_points = np.linspace(len(dates) * 0.5, len(dates), n_folds + 1).astype(int)
    evals: List[Eval] = []
    all_preds: List[pd.DataFrame] = []

    for i in range(n_folds):
        cut = dates[split_points[i] - 1]
        end = dates[min(split_points[i + 1] - 1, len(dates) - 1)]
        train = df[df["rc_date"] <= cut]
        test = df[(df["rc_date"] > cut) & (df["rc_date"] <= end)]
        if train["race_key"].nunique() < min_train_races or test.empty:
            log.info("fold %d 건너뜀 (학습 %d경주, 검증 %d행)", i + 1,
                     train["race_key"].nunique(), len(test))
            continue

        models = fit(train, seed)
        if "win" not in models:
            continue
        pred = predict_frame(models, test)
        try:
            ev = evaluate(pred)
        except ValueError as e:
            log.info("fold %d 평가 불가: %s", i + 1, e)
            continue
        evals.append(ev)
        pred["fold"] = i + 1
        all_preds.append(pred)
        log.info(
            "fold %d | 학습 ~%s (%d경주) | 검증 %d경주 → 단승 %.1f%% (시장 %.1f%%) 연승 %.1f%% (시장 %.1f%%)",
            i + 1, str(cut)[:10], train["race_key"].nunique(), ev.n_races,
            ev.top1_win * 100, ev.mkt_top1_win * 100, ev.top1_top3 * 100, ev.mkt_top1_top3 * 100,
        )

    combined = pd.concat(all_preds, ignore_index=True) if all_preds else pd.DataFrame()
    return evals, combined


def summarize(evals: List[Eval]) -> Dict:
    if not evals:
        return {}
    keys = [k for k in asdict(evals[0]) if k not in ("n_races", "n_runners")]
    total_races = sum(e.n_races for e in evals)
    out = {"folds": len(evals), "n_races": total_races,
           "n_runners": sum(e.n_runners for e in evals)}
    for k in keys:
        vals = [(getattr(e, k), e.n_races) for e in evals if not np.isnan(getattr(e, k))]
        out[k] = float(np.average([v for v, _ in vals], weights=[w for _, w in vals])) if vals else None
    return out


def save(models: Dict, metrics: Dict, path: Path = MODEL_PATH) -> None:
    import joblib

    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"models": models, "features": FEATURE_COLUMNS, "version": MODEL_VERSION}, path)

    # metrics.json 은 학습 지표만의 파일이 아니다. 보정 도구들이 각자의 블록을
    # 여기에 남기고 사이트가 그 값을 읽는다. 통째로 덮어쓰면 화면에서 수치가
    # 조용히 사라지므로(실제로 신뢰도 블록이 그렇게 날아갔다), 기존 내용을
    # 읽어 병합한다. 재보정이 돌기 전까지는 직전 값이 유지된다.
    blob: Dict = {}
    try:
        blob = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    blob.update(metrics)
    METRICS_PATH.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")


def load(path: Path = MODEL_PATH) -> Dict:
    import joblib

    if not path.exists():
        raise FileNotFoundError(f"학습된 모델이 없습니다: {path}  (python -m horseai.model train)")
    return joblib.load(path)


def _fmt(v: Optional[float], pct: bool = True) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "  n/a"
    return f"{v * 100:5.1f}%" if pct else f"{v:5.3f}"


def report(summary: Dict) -> str:
    if not summary:
        return "검증 결과가 없습니다."
    lines = [
        f"검증 경주 {summary['n_races']:,}회 / 출주 {summary['n_runners']:,}두  (fold {summary['folds']})",
        "",
        f"{'지표':<24}{'AI 모델':>10}{'시장(1인기)':>14}{'차이':>10}",
        "-" * 60,
    ]
    rows = [
        ("단승 적중률 (1순위)", "top1_win", "mkt_top1_win"),
        ("연승 적중률 (1순위)", "top1_top3", "mkt_top1_top3"),
        ("3순위 내 1착 포함", "top3_has_winner", "mkt_top3_has_winner"),
        ("단승 회수율(ROI)", "roi_win", "mkt_roi_win"),
    ]
    for label, mk, bk in rows:
        m, b = summary.get(mk), summary.get(bk)
        # ROI 도 100원 대비 회수율(%)로 통일해 표기한다.
        diff = ""
        if m is not None and b is not None and not (np.isnan(m) or np.isnan(b)):
            diff = f"{(m - b) * 100:+6.1f}%p"
        lines.append(f"{label:<24}{_fmt(m):>10}{_fmt(b):>14}{diff:>10}")
    lines += [
        "-" * 60,
        f"로그손실 {_fmt(summary.get('logloss'), False)}   AUC {_fmt(summary.get('auc'), False)}"
        f"   배당률 커버리지 {_fmt(summary.get('coverage_odds'))}",
    ]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="승률 모델 학습/검증")
    ap.add_argument("command", choices=["train", "validate"])
    ap.add_argument("--db", default="data/horseai.sqlite")
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--min-train-races", type=int, default=1500)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    conn = sqlite3.connect(args.db)
    try:
        df = build_training_frame(conn)
    finally:
        conn.close()

    if df.empty:
        print("학습 데이터가 없습니다. 먼저 수집하세요:\n"
              "  python -m horseai.kra.collect backfill --years 5", file=sys.stderr)
        return 1

    log.info("학습 프레임: %d행 / %d경주 / %s ~ %s",
             len(df), df["race_key"].nunique(),
             str(df["rc_date"].min())[:10], str(df["rc_date"].max())[:10])

    evals, preds = walk_forward(df, n_folds=args.folds, min_train_races=args.min_train_races)
    summary = summarize(evals)
    print("\n" + report(summary) + "\n")

    if args.command == "train":
        models = fit(df)
        if "win" not in models:
            print("모델 학습 실패: 레이블 부족", file=sys.stderr)
            return 1
        save(models, {"walk_forward": summary, "trained_rows": len(df),
                      "trained_races": int(df["race_key"].nunique()),
                      "date_max": str(df["rc_date"].max())[:10]})
        print(f"모델 저장 → {MODEL_PATH}\n검증 지표 → {METRICS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
