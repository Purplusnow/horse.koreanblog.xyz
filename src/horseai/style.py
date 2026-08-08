"""각질(脚質) 판정 — 선행 · 선입 · 추입.

한국 경마 팬이 출마표를 읽을 때 가장 먼저 보는 게 각질이다. 시중 예상지가
전개도에 마번을 늘어놓고 '선행 8·11 / 선입 4·1·7·5 / 추입 3·6·9·2·10' 식으로
분류해 두는 이유가 그것이다. 어떤 말이 빠른가보다 **어디서 달리는가**가 경주
전개를 결정하고, 전개가 결과를 크게 좌우하기 때문이다.

경주성적 API가 구간 통과순위(S1F, 코너별, G1F)를 주므로 이걸 데이터로 판정할 수
있다. 과거 경주에서 그 말이 초반에 어디에 있었는지를 상대 위치(0=선두, 1=최후미)로
환산해 평균 내면 그것이 각질이다.

이 값은 화면 표시용이면서 동시에 **예측 피처**다. 같은 경주에 선행마가 몰리면
페이스가 빨라져 추입마가 유리해지는 식의 상호작용이 실제로 존재한다.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pandas as pd

# 코드, 표시명, 짧은 배지, 설명
STYLES: List[Dict[str, str]] = [
    {"code": "front", "label": "선행", "desc": "출발 직후부터 선두권에서 경주를 끌고 간다"},
    {"code": "stalk", "label": "선입", "desc": "중위권에서 따라가다 직선에서 승부를 건다"},
    {"code": "close", "label": "추입", "desc": "후미에 대기하다 막판에 몰아친다"},
]
STYLE_LABEL = {s["code"]: s["label"] for s in STYLES}
STYLE_ORDER = {"front": 0, "stalk": 1, "close": 2, "unknown": 3}

# 초반 상대위치 임계값. 실제 예상지의 3분류 비율에 대략 맞춘 값.
FRONT_MAX = 0.28
STALK_MAX = 0.58

# 이 정도 출주 이력은 있어야 각질을 단정한다
MIN_RUNS = 2


def early_position(df: pd.DataFrame) -> pd.Series:
    """초반 상대위치. 0 = 선두, 1 = 최후미.

    S1F 순위를 우선하고, 없으면 첫 코너 순위로 대체한다. 출주 두수로 나눠
    두수가 다른 경주 간에도 비교 가능하게 만든다.
    """
    field = pd.to_numeric(df.get("field_size"), errors="coerce")
    field = field.fillna(df.groupby("race_key")["hr_no"].transform("count"))

    rank = pd.to_numeric(df.get("s1f_rank"), errors="coerce")
    for fallback in ("c1_rank", "c2_rank"):
        if fallback in df.columns:
            rank = rank.fillna(pd.to_numeric(df[fallback], errors="coerce"))

    denom = (field - 1).replace(0, np.nan)
    return ((rank - 1) / denom).clip(0, 1)


def classify(pos: Optional[float], runs: int = MIN_RUNS) -> str:
    """평균 초반위치 → 각질 코드."""
    if pos is None or (isinstance(pos, float) and np.isnan(pos)) or runs < MIN_RUNS:
        return "unknown"
    if pos <= FRONT_MAX:
        return "front"
    if pos <= STALK_MAX:
        return "stalk"
    return "close"


def attach_history(df: pd.DataFrame) -> pd.DataFrame:
    """과거 이력에 초반위치와 '직전까지의' 각질 지표를 붙인다.

    누수 방지 원칙은 여기서도 동일하다 — 각질은 반드시 shift 된 과거 경주에서만
    집계한다. 당일 경주의 통과순위는 경주가 끝나야 알 수 있는 값이다.
    """
    df = df.copy()
    df["early_pos"] = early_position(df)

    hr = df["hr_no"]
    prior = df.groupby(hr)["early_pos"]
    df["style_pos"] = prior.transform(lambda s: s.shift(1).rolling(6, min_periods=1).mean())
    df["style_runs"] = prior.transform(lambda s: s.shift(1).notna().cumsum())
    df["style_pos_last"] = prior.shift(1)
    df["style_code"] = [
        classify(p, r if r == r else 0)
        for p, r in zip(df["style_pos"], df["style_runs"].fillna(0))
    ]
    df["is_front"] = (df["style_code"] == "front").astype(int)
    df["is_close"] = (df["style_code"] == "close").astype(int)
    return df


def add_race_context(df: pd.DataFrame) -> pd.DataFrame:
    """경주 단위 전개 맥락.

    선행마가 몇 두인지가 페이스를 결정한다. 선행마가 몰리면 서로 끌어당겨
    초반이 빨라지고 막판에 힘이 빠져 추입마에게 유리해진다. 반대로 선행마가
    한 두뿐이면 그 말이 편하게 도주해 그대로 굳어지기 쉽다.
    """
    df = df.copy()
    g = df.groupby("race_key")
    n = g["hr_no"].transform("count")
    front_n = g["is_front"].transform("sum")
    close_n = g["is_close"].transform("sum")

    df["race_front_n"] = front_n
    df["race_front_ratio"] = front_n / n.replace(0, np.nan)
    df["race_close_ratio"] = close_n / n.replace(0, np.nan)
    # 선행마가 혼자면 +1(단독 도주 유리), 몰리면 음수(페이스 과열)
    df["pace_edge"] = np.where(
        df["is_front"] == 1, 2.0 - front_n,
        np.where(df["is_close"] == 1, front_n - 2.0, 0.0),
    )
    df["style_pos_rel"] = df["style_pos"] - g["style_pos"].transform("mean")
    return df


FEATURES = [
    "style_pos", "style_pos_last", "style_pos_rel", "style_runs",
    "is_front", "is_close", "race_front_n", "race_front_ratio",
    "race_close_ratio", "pace_edge",
]


def pace_map(runners: List[Dict]) -> Dict[str, List[Dict]]:
    """전개도용 그룹핑 — 예상지의 '선행 / 선입 / 추입' 배치와 같은 형태."""
    buckets: Dict[str, List[Dict]] = {s["code"]: [] for s in STYLES}
    buckets["unknown"] = []
    for r in sorted(runners, key=lambda r: r.get("pred_rank") or 99):
        buckets.setdefault(r.get("style_code") or "unknown", []).append(r)
    return buckets
