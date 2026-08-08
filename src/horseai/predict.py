"""출전표 → 예측 생성.

    python -m horseai.predict --db data/horseai.sqlite

**예측 동결 규칙** — 적중률 공개가 이 사이트의 유일한 자산이므로, 경주일이 지난
예측은 어떤 경우에도 다시 쓰지 않는다. 경주일 이전에는 기수 변경·출주 취소가
반영되도록 갱신을 허용한다. 이 규칙이 깨지면 적중률 통계 전체가 무의미해진다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sqlite3
import sys
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .clock import now_kst
from .features import build_prediction_frame
from .kra.store import session, upsert
from .model import MODEL_VERSION, load, predict_frame
from dataclasses import replace

from .simulate import (
    animation_payload, build_runners, confidence, expected_run, fit_noise, simulate,
)

log = logging.getLogger(__name__)


def upcoming_race_keys(conn: sqlite3.Connection, days_ahead: int = 10) -> List[str]:
    """아직 결과가 없고 출전표가 있는 경주."""
    today = now_kst().date().isoformat()
    until = (now_kst().date() + dt.timedelta(days=days_ahead)).isoformat()
    rows = conn.execute(
        "SELECT DISTINCT r.race_key FROM races r "
        "JOIN entries e ON e.race_key = r.race_key "
        "WHERE r.rc_date >= ? AND r.rc_date <= ? AND COALESCE(r.has_result,0) = 0 "
        "ORDER BY r.rc_date, r.meet, r.rc_no",
        (today, until),
    ).fetchall()
    return [r[0] for r in rows]


def _tags(r, race: pd.DataFrame) -> List[str]:
    """출주마 한 줄 특징 태그.

    시중 예상지의 '마필단평'에 해당하지만, 사람의 인상평이 아니라 데이터에서
    기계적으로 뽑는다. 근거 없는 문장을 만들지 않으므로 검증 가능하고,
    경주당 열 몇 두를 LLM으로 돌리는 비용도 들지 않는다.
    """
    tags: List[str] = []
    n = len(race)

    rating = pd.to_numeric(race["rating"], errors="coerce")
    if pd.notna(r.rating) and rating.notna().sum() >= 3:
        rank = int((rating > r.rating).sum()) + 1
        if rank == 1:
            tags.append("레이팅 1위")
        elif rank <= max(2, n // 4):
            tags.append("레이팅 상위")

    # 부담중량 태그는 '남들보다 가볍다'는 뜻이어야 한다. 최소값 이하면 붙이면,
    # 전원이 같은 중량인 경주에서 열 마리 모두에게 붙어 아무것도 구별하지
    # 못한다(실제로 제6등급 경주에서 그렇게 나왔다). 실제로 차이가 있고,
    # 그 최저 중량이 소수일 때만 정보가 된다.
    burden = pd.to_numeric(race["burden"], errors="coerce").dropna()
    if pd.notna(r.burden) and len(burden) >= 3 and burden.min() < burden.max():
        lightest = burden[burden == burden.min()]
        if r.burden == burden.min() and len(lightest) <= max(1, len(burden) // 3):
            tags.append("최경량")

    if getattr(r, "is_front", 0) == 1 and getattr(r, "race_front_n", 0) == 1:
        tags.append("단독 선행")
    if getattr(r, "pace_edge", 0) and r.pace_edge > 0.5:
        tags.append("전개 수혜")

    starts = getattr(r, "starts_prior", 0) or 0
    if starts == 0:
        tags.append("첫 출전")
    else:
        wr = getattr(r, "win_rate", None)
        if wr is not None and wr == wr and starts >= 5 and wr >= 0.20:
            tags.append(f"승률 {wr:.0%}")
        recent = getattr(r, "avg_ord_pct_3", None)
        if recent is not None and recent == recent and recent <= 0.30:
            tags.append("최근 3전 호조")

    jk = getattr(r, "jk_win_rate", None)
    if jk is not None and jk == jk and (getattr(r, "jk_starts", 0) or 0) >= 100 and jk >= 0.12:
        tags.append("기수 호조")

    # 간격 태그는 '드물어야' 정보가 된다. 느슨하게 잡으면 절반 넘는 말에 붙어
    # 아무것도 구별해 주지 못한다. 양 극단만 남긴다.
    days = getattr(r, "days_since_last", None)
    if days is not None and days == days:
        if days >= 150:
            tags.append("장기 휴양 후")
        elif days <= 8:
            tags.append("강행군")

    return tags[:4]


def build_rows(pred: pd.DataFrame) -> List[Dict]:
    """예측 프레임 → predictions 테이블 행. 각질·태그도 이때 함께 확정한다."""
    rows: List[Dict] = []
    for key, race in pred.groupby("race_key"):
        for r in race.itertuples():
            rows.append({
                "race_key": key,
                "hr_no": r.hr_no,
                "chul_no": int(r.chul_no) if pd.notna(r.chul_no) else None,
                "p_win": float(r.p_win_norm),
                "p_place": float(r.p_top3_norm) if pd.notna(r.p_top3_norm) else None,
                "p_top2": float(r.p_top2_norm) if pd.notna(getattr(r, "p_top2_norm", None)) else None,
                "pred_rank": int(r.pred_rank),
                "model_version": MODEL_VERSION,
                "style_code": getattr(r, "style_code", None) or "unknown",
                "tags": json.dumps(_tags(r, race), ensure_ascii=False),
            })
    return rows


def build_simulations(pred: pd.DataFrame, n_sims: int = 2000) -> List[Dict]:
    """경주별 시뮬레이션을 돌려 미리보기 대본과 신뢰도를 만든다."""
    out: List[Dict] = []
    for key, race in pred.groupby("race_key"):
        race = race.sort_values("pred_rank")
        dist = pd.to_numeric(race["distance"], errors="coerce").dropna()
        if dist.empty or len(race) < 3:
            continue
        distance = float(dist.iloc[0])
        runners = build_runners(race.to_dict("records"))
        target = race["p_win_norm"].tolist()
        # 경주마다 이변의 여지를 보정해 시뮬 승률을 게재 승률에 맞춘다
        noise = fit_noise(runners, distance, target)
        sim = simulate(runners, distance, n_sims=n_sims, noise_scale=noise)
        conf = confidence(sim)
        # 승률은 위 시행(분포)에서, 미리보기는 '예상대로 전개될 경우'에서 나온다.
        # 둘을 섞으면 추천 순서와 화면이 어긋난다.
        seg = expected_run(runners, distance)
        sim = replace(sim, seg_times=seg, positions=np.cumsum(seg, axis=1))
        out.append({
            "race_key": key,
            "payload": json.dumps(animation_payload(sim, distance), ensure_ascii=False),
            "conf_score": conf["score"], "conf_label": conf["label"],
            "conf_desc": conf["desc"], "n_sims": n_sims,
            "noise_scale": round(float(noise), 3),
        })
    return out


def frozen_race_keys(conn: sqlite3.Connection) -> set:
    """예측 수정이 금지된 경주.

    동결 기준은 **발주 시각**이다. 날짜 단위로만 잠그면, 당일 12:30 에 이미 달린
    경주가 결과가 들어오기 전까지 몇 시간 동안 열려 있어 15:00 갱신에서 예측이
    다시 쓰일 수 있다. 그러면 '발주 전에 게재한 예상'이라는 전제가 무너지고,
    공개 적중률 전체가 의미를 잃는다. 결과 도착은 발주보다 늦으므로 has_result
    만으로는 이 구간을 막지 못한다.

    발주 시각이 비어 있는 경주는 그 날의 마지막 경주가 끝났다고 볼 수 없으므로
    날짜만으로 판단한다(당일이면 열어 둔다).
    """
    now = now_kst()
    today = now.date().isoformat()
    rows = conn.execute(
        "SELECT DISTINCT p.race_key, r.rc_date, r.post_time, "
        "       COALESCE(r.has_result, 0) AS has_result "
        "FROM predictions p JOIN races r ON r.race_key = p.race_key "
        "WHERE r.rc_date <= ? OR COALESCE(r.has_result,0) = 1",
        (today,),
    ).fetchall()

    frozen = set()
    for key, rc_date, post_time, has_result in rows:
        if has_result or (rc_date or "") < today:
            frozen.add(key)
            continue
        if rc_date != today or not post_time:
            continue
        try:
            hh, mm = str(post_time).strip().split(":")[:2]
            post = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        except (ValueError, TypeError):
            continue
        if now >= post:
            frozen.add(key)
    return frozen


def generate(conn: sqlite3.Connection, race_keys: Optional[List[str]] = None,
             days_ahead: int = 10) -> pd.DataFrame:
    keys = race_keys or upcoming_race_keys(conn, days_ahead)
    if not keys:
        log.info("예측할 신규 경주가 없습니다.")
        return pd.DataFrame()

    frozen = frozen_race_keys(conn)
    keys = [k for k in keys if k not in frozen]
    if not keys:
        log.info("대상 경주가 모두 동결 상태입니다 (경주일 경과).")
        return pd.DataFrame()

    target, _ = build_prediction_frame(conn, keys)
    if target.empty:
        log.warning("출전표에서 피처를 만들지 못했습니다.")
        return target

    bundle = load()
    pred = predict_frame(bundle["models"], target)

    rows = build_rows(pred)
    upsert(conn, "predictions", rows, ["race_key", "hr_no", "model_version"])
    upsert(conn, "simulations", build_simulations(pred), ["race_key"])
    conn.commit()
    log.info("예측 생성: %d경주 / %d두", pred["race_key"].nunique(), len(rows))
    return pred


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="출전표 기반 예측 생성")
    ap.add_argument("--db", default="data/horseai.sqlite")
    ap.add_argument("--days-ahead", type=int, default=10)
    ap.add_argument("--race", nargs="*", help="특정 race_key 만")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with session(args.db) as conn:
        try:
            pred = generate(conn, args.race, args.days_ahead)
        except FileNotFoundError as e:
            print(f"✗ {e}", file=sys.stderr)
            return 1
    if pred.empty:
        return 0

    for key, grp in pred.groupby("race_key"):
        g = grp.sort_values("pred_rank").head(3)
        picks = "  ".join(
            f"{int(r.chul_no)}번 {r.hr_name}({r.p_win_norm:.0%})" for r in g.itertuples()
        )
        print(f"{key}  {picks}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
