"""데이터 누수 회귀 테스트.

핵심 아이디어: 어떤 경주일 D 의 피처는 D 이전 정보만으로 결정되어야 한다.
따라서 **전체 데이터로 계산한 D일 피처**와 **D일까지만 남기고 잘라낸 데이터로
계산한 D일 피처**가 완전히 같아야 한다. 하나라도 다르면 미래가 새고 있는 것이다.

    PYTHONPATH=src python -m pytest tests/ -q
    PYTHONPATH=src python tests/test_no_leakage.py     # pytest 없이도 실행 가능
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from horseai.features import (  # noqa: E402
    FEATURE_COLUMNS,
    build_history_index,
    finalize,
    load_history,
)

DB = ROOT / "data" / "synth.sqlite"

# 경주 후에만 알 수 있는 값 — 피처에 절대 들어가면 안 된다
FORBIDDEN = {
    "ord", "ord_pct", "is_win", "is_top3", "is_place",
    "record_sec", "speed_fig", "win_odds", "place_odds",
    "horse_weight", "margin",
    # 조건 보정·구간 속도도 당일 경주 결과에서 나온 값이다.
    # 반드시 shift 된 이력(_last/_avg3/_best)으로만 써야 한다.
    "abs_speed", "early_speed", "late_speed", "finish_speed",
    "moisture", "s1f_sec", "g3f_sec", "g1f_sec",
    "s1f_rank", "c1_rank", "c2_rank", "c3_rank", "c4_rank", "g1f_rank",
    "early_pos", "weight_delta",
}


def _require_db():
    if not DB.exists():
        raise SystemExit(
            f"합성 DB가 없습니다: {DB}\n  python tools/make_synth_db.py --db {DB} --years 4"
        )


def test_forbidden_columns_absent():
    """경주 후 정보가 피처 목록에 섞여 있지 않은지."""
    overlap = FORBIDDEN & set(FEATURE_COLUMNS)
    assert not overlap, f"경주 후 정보가 피처에 포함됨: {sorted(overlap)}"


def test_history_features_are_past_only():
    """전체 데이터 vs 절단 데이터에서 같은 날 피처가 동일해야 한다."""
    _require_db()
    conn = sqlite3.connect(str(DB))
    try:
        hist = load_history(conn)
    finally:
        conn.close()

    assert not hist.empty, "합성 DB가 비어 있습니다"

    dates = pd.Series(hist["rc_date"].unique()).sort_values().reset_index(drop=True)
    cutoff = dates.iloc[int(len(dates) * 0.8)]

    full = finalize(build_history_index(hist))
    truncated = finalize(build_history_index(hist[hist["rc_date"] <= cutoff]))

    key = ["race_key", "hr_no"]
    a = full[full["rc_date"] == cutoff].set_index(key).sort_index()
    b = truncated[truncated["rc_date"] == cutoff].set_index(key).sort_index()

    assert len(a) > 0, "절단 기준일에 경주가 없습니다"
    assert a.index.equals(b.index), "절단 전후 행 구성이 다릅니다"

    mismatched = []
    for col in FEATURE_COLUMNS:
        x, y = a[col], b[col]
        if x.dtype == object or y.dtype == object:
            same = (x.astype(str) == y.astype(str)).all()
        else:
            same = np.allclose(
                pd.to_numeric(x, errors="coerce").fillna(-9e9),
                pd.to_numeric(y, errors="coerce").fillna(-9e9),
                rtol=1e-9, atol=1e-9,
            )
        if not same:
            mismatched.append(col)

    assert not mismatched, (
        f"미래 정보 누수 의심 피처 {len(mismatched)}개: {mismatched}\n"
        f"(기준일 {str(cutoff)[:10]}, 대상 {len(a)}행)"
    )


def test_prior_stats_exclude_self():
    """직전까지 집계가 자기 자신을 포함하지 않는지 — 첫 출전마는 이력이 비어야 한다."""
    _require_db()
    conn = sqlite3.connect(str(DB))
    try:
        hist = load_history(conn)
    finally:
        conn.close()

    built = build_history_index(hist)
    # build_history_index 와 동일한 정렬 키 + 안정 정렬을 써야 '진짜 첫 출전'을 집는다.
    first_runs = built.sort_values(
        ["rc_date", "meet", "rc_no", "chul_no"], kind="mergesort"
    ).groupby("hr_no").head(1)

    assert (pd.to_numeric(first_runs["starts_prior"], errors="coerce").fillna(0) == 0).all(), \
        "첫 출전인데 과거 출주수가 0이 아닙니다 (자기 자신 포함 의심)"
    assert pd.to_numeric(first_runs["win_rate"], errors="coerce").isna().all(), \
        "첫 출전인데 과거 승률이 존재합니다 (자기 자신 포함 의심)"
    assert pd.to_numeric(first_runs["speed_last"], errors="coerce").isna().all(), \
        "첫 출전인데 직전 스피드지수가 존재합니다"


def test_no_as_of_now_snapshot_features():
    """'조회 시점 스냅샷' 필드가 피처에 섞이지 않았는지.

    앞선 두 테스트는 *우리가 계산한* 이력 피처만 검증한다. API 가 통째로 건네주는
    값이 실은 '현재 시점 집계'라면, 과거 데이터를 잘라내도 그 값은 변하지 않으므로
    두 테스트를 모두 통과하면서 미래를 흘린다.

    실제로 출전표의 통산 전적이 그랬다 — 한 마리의 모든 경주에서 career_starts 가
    동일한 값으로 나왔다. 그런 필드의 지문은 **'말 단위로 값이 전혀 변하지 않는다'**
    이므로, 그걸 직접 검사한다.
    """
    _require_db()
    conn = sqlite3.connect(str(DB))
    try:
        hist = load_history(conn)
    finally:
        conn.close()

    df = finalize(build_history_index(hist))
    runs = df.groupby("hr_no").size()
    veterans = runs[runs >= 5].index
    sub = df[df["hr_no"].isin(veterans)]
    if sub.empty:
        return  # 표본 부족 — 검사 생략

    # 성별·산지처럼 원래 불변인 값은 제외 대상이 아니다.
    STATIC_OK = {"is_male", "is_gelding", "dist_bucket"}
    suspicious = []
    for col in FEATURE_COLUMNS:
        if col in STATIC_OK:
            continue
        vals = pd.to_numeric(sub[col], errors="coerce")
        if vals.notna().sum() < len(sub) * 0.2:
            continue  # 대부분 결측이면 판단 보류
        constant_ratio = (
            vals.groupby(sub["hr_no"]).nunique(dropna=True).le(1).mean()
        )
        if constant_ratio > 0.9:
            suspicious.append(f"{col}({constant_ratio:.0%})")

    assert not suspicious, (
        "말 단위로 값이 변하지 않는 피처 — 조회 시점 스냅샷일 가능성이 높습니다: "
        + ", ".join(suspicious)
    )


def test_official_stats_excluded_from_features():
    """출전표 공식 전적은 표시 전용이며 학습 피처가 아니어야 한다."""
    from horseai.features import OFFICIAL_DISPLAY_ONLY

    leaked = [c for c in OFFICIAL_DISPLAY_ONLY if c in FEATURE_COLUMNS]
    assert not leaked, f"미래가 섞인 공식 전적이 피처에 포함됨: {leaked}"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"✓ {name}")
            except AssertionError as e:
                failures += 1
                print(f"✗ {name}\n    {e}")
    print(f"\n{'통과' if not failures else f'{failures}건 실패'}")
    raise SystemExit(1 if failures else 0)
