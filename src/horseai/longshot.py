"""복병 — 추천 5두 밖에서 깜짝 입상할 말을 하나 짚는다.

추천 5두 밖에도 3착에 드는 말이 여섯에 하나(15.9%) 꼴로 나온다. 그 자리를
짚을 수 있으면 삼복승·복연승에서 값어치가 크다.

**본 모델과 목표가 다르다.** 본 모델은 '이 말이 1착할 확률'을 재는데, 여기서는
'예측 6순위 이하인데 3착에 들 확률'만 본다. 문제가 다르면 학습되는 것도 다르다.

측정 (6,105경주 시간순 교차검증, 검증 13,065두 / 2,418경주):

    선정 방식                경주    3착 이내
    6순위 이하 전체         ―        15.2%   ← 기준선
    단일 지표 상위 10%      ―        23.1%
    경주당 1두 (전 경주)   2,418     24.2%
    확률 상위 50% 경주     1,209     27.6%
    확률 상위 30% 경주       726     29.8%   ← 채택
    확률 상위 15% 경주       363     31.1%

상위 30%를 쓴다. 15%로 조이면 1.3%p 더 얻자고 게재 횟수가 절반이 되고, 매
경주 내면(24.2%) 흔해져서 아무 말도 하지 않는 것과 같아진다 — ★ 을 드물게
쓰는 것과 같은 이치다.

**5두 자리를 뺏지 않는다.** 같은 조건(전 경주)에서 복병 24.2% 는 5순위 27.6%
보다 낮다. 자리를 바꿀 근거가 없고, 바꾸면 '우리 예상'의 정의가 경주마다
달라져 적중률 집계가 무너진다. 번외로 둔다.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

from .features import FEATURE_COLUMNS

log = logging.getLogger(__name__)

MODEL_PATH = Path("models/longshot.joblib")
VERSION = "ls1"

# 추천 5두 밖 — 여기서부터가 복병 후보다.
OUTSIDE_FROM = 6
# 이 확률 분위 아래로는 내지 않는다. 자신 없는 경주까지 억지로 채우면
# 적중률이 희석되고 표기 자체가 가벼워진다.
PICK_QUANTILE = 0.70


def _matrix(df: pd.DataFrame) -> pd.DataFrame:
    """학습·예측 공용 입력. 범주형은 코드로 바꾼다."""
    cols = [c for c in FEATURE_COLUMNS if c in df.columns]
    num = [c for c in cols if pd.api.types.is_numeric_dtype(df[c])]
    X = df[num].copy()
    for c in (c for c in cols if c not in num):
        X[c] = df[c].astype("category").cat.codes
    return X


def fit(pred: pd.DataFrame, seed: int = 0) -> Optional[Dict]:
    """시간순 교차검증으로 복원한 예측 프레임에서 학습한다.

    pred 에는 pred_rank(장외 판정용)와 ord(정답)가 있어야 한다. 본 모델의
    순위가 필요하므로 walk_forward 결과를 그대로 받는다.
    """
    df = pred[pd.to_numeric(pred["ord"], errors="coerce").notna()].copy()
    df["ord_n"] = pd.to_numeric(df["ord"], errors="coerce")
    out = df[df["pred_rank"] >= OUTSIDE_FROM].copy()
    if len(out) < 3000:
        log.warning("복병 학습 표본이 부족합니다 (%d두)", len(out))
        return None

    y = (out["ord_n"] <= 3).astype(int)
    X = _matrix(out)
    m = HistGradientBoostingClassifier(
        max_iter=300, learning_rate=0.06, max_leaf_nodes=24,
        min_samples_leaf=60, l2_regularization=1.0, random_state=seed)
    m.fit(X, y)

    # 게재 기준값. 경주별 최고 확률의 분위로 잡는다 — 확률 자체의 분위로
    # 잡으면 후보가 많은 경주가 과대 대표된다.
    p = m.predict_proba(X)[:, 1]
    best = pd.DataFrame({"race_key": out["race_key"], "p": p}) \
        .groupby("race_key")["p"].max()
    return {"model": m, "columns": list(X.columns), "version": VERSION,
            "threshold": float(best.quantile(PICK_QUANTILE)),
            "n_train": int(len(out)), "base_rate": float(y.mean())}


def pick(bundle: Optional[Dict], race: pd.DataFrame) -> Optional[Dict]:
    """한 경주에서 복병 한 마리. 기준에 못 미치면 내지 않는다.

    race 는 그 경주의 예측 프레임이어야 하고 pred_rank 가 매겨져 있어야 한다.
    """
    if not bundle or race.empty:
        return None
    out = race[race["pred_rank"] >= OUTSIDE_FROM]
    if out.empty:
        return None
    X = _matrix(out).reindex(columns=bundle["columns"], fill_value=np.nan)
    p = bundle["model"].predict_proba(X)[:, 1]
    i = int(np.argmax(p))
    if p[i] < bundle["threshold"]:
        return None                      # 자신 없으면 침묵한다
    r = out.iloc[i]
    return {"hr_no": r.get("hr_no"), "chul_no": r.get("chul_no"),
            "hr_name": r.get("hr_name"), "p_top3": float(p[i]),
            "pred_rank": int(r.get("pred_rank"))}


def save(bundle: Dict, path: Path = MODEL_PATH) -> None:
    import joblib
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, path)


def load(path: Path = MODEL_PATH) -> Optional[Dict]:
    import joblib
    if not path.exists():
        return None
    try:
        return joblib.load(path)
    except Exception as e:                                    # noqa: BLE001
        log.warning("복병 모델을 읽지 못했습니다: %s", type(e).__name__)
        return None
