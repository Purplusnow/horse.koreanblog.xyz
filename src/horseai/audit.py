"""자료 결손 점검.

**없는 줄 모르는 자료가 가장 위험하다.** 실제로 8월 8일 17경주 중 7경주의
배당이 안 들어와 있었는데, 배당표에는 적중 조합만 저장되므로 '표에 없으면
불발'로 판정돼 공개 적중률이 실제의 3분의 1로 나갔다. 파이프라인은 모두
성공으로 끝났고 어디에도 경고가 없었다.

성공한 파이프라인이 조용한 것은 신호가 아니다. 그래서 굽기 전에 자료가
있어야 할 자리에 있는지 따로 센다. 부족하면 **비정상 종료**해 워크플로가
빨갛게 뜨도록 한다 — 경고를 로그에만 남기면 아무도 보지 않는다.

    python -m horseai.audit --db data/horseai.sqlite
    python -m horseai.audit --db data/horseai.sqlite --days 30 --strict
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from typing import Dict, List

import pandas as pd

from .clock import today_kst
from .kra.store import session

# 승식 일곱 종이 모두 발매되므로, 시행된 경주에는 배당이 이만큼 있어야 한다.
EXPECTED_POOLS = 7


def check(conn, days: int) -> List[Dict]:
    """최근 며칠을 훑어 결손을 찾는다. 반환값은 사람이 읽을 문제 목록이다."""
    since = (today_kst() - dt.timedelta(days=days)).isoformat()
    df = pd.read_sql(
        """
        SELECT g.race_key, g.rc_date, g.meet, g.rc_no,
               COALESCE(g.has_result, 0)                                  AS has_result,
               (SELECT COUNT(*) FROM predictions p WHERE p.race_key = g.race_key)  AS n_pred,
               (SELECT COUNT(*) FROM results  r WHERE r.race_key = g.race_key
                                             AND r.ord IS NOT NULL)       AS n_ord,
               (SELECT COUNT(DISTINCT v.pool) FROM dividends v
                 WHERE v.race_key = g.race_key)                           AS n_pool,
               (SELECT COUNT(*) FROM entries e WHERE e.race_key = g.race_key)      AS n_entry
        FROM races g
        WHERE g.rc_date >= ?
        """,
        conn, params=[since])
    if df.empty:
        return []

    done = df[df["has_result"] == 1]
    issues: List[Dict] = []

    def add(kind: str, rows: pd.DataFrame, note: str) -> None:
        if rows.empty:
            return
        issues.append({
            "kind": kind,
            "n": len(rows),
            "note": note,
            "races": [f"{r.meet} {int(r.rc_no)}R ({str(r.rc_date)[:10]})"
                      for r in rows.head(8).itertuples()],
        })

    # 시행됐는데 배당이 없거나 일부 승식만 들어온 경주.
    # 이것이 이번에 놓쳤던 결손이고, 적중률·환수율을 직접 망가뜨린다.
    add("배당 결손", done[done["n_pool"] == 0],
        "시행된 경주에 배당이 하나도 없다 — 승식 판정이 전부 불발로 집계된다")
    add("배당 일부", done[(done["n_pool"] > 0) & (done["n_pool"] < EXPECTED_POOLS)],
        f"승식 {EXPECTED_POOLS}종 중 일부만 들어왔다")

    # 시행됐는데 착순이 없는 경주. 마사회가 1~3착을 먼저 내므로 경주 직후에는
    # 정상이지만, 하루가 지나도 남아 있으면 수집이 빠진 것이다.
    stale = done[(done["n_ord"] == 0) &
                 (pd.to_datetime(done["rc_date"]).dt.date < today_kst())]
    add("착순 결손", stale, "전날 이전 경주인데 착순이 없다")

    # 출전표는 있는데 예측이 없는 경주 — 게재 자체가 빠진 것이다.
    # 게재를 시작하기 전의 경주는 예측이 없는 것이 정상이므로 제외한다.
    first = conn.execute(
        "SELECT MIN(g.rc_date) FROM races g JOIN predictions p "
        "ON p.race_key = g.race_key").fetchone()[0]
    if first:
        add("예측 누락", df[(df["n_entry"] > 0) & (df["n_pred"] == 0) &
                         (df["has_result"] == 1) & (df["rc_date"] >= first)],
            "출전표가 있는데 예측이 없다 — 그날 게재가 빠졌다")

    return issues


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="자료 결손 점검")
    ap.add_argument("--db", default="data/horseai.sqlite")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--strict", action="store_true",
                    help="결손이 있으면 1 로 끝낸다 (CI 에서 빨갛게 뜬다)")
    args = ap.parse_args(argv)

    with session(args.db) as conn:
        issues = check(conn, args.days)

    if not issues:
        print(f"자료 점검 최근 {args.days}일 — 결손 없음")
        return 0

    print(f"자료 점검 최근 {args.days}일 — 결손 {len(issues)}종")
    for it in issues:
        print(f"\n  ▸ {it['kind']} {it['n']}경주 — {it['note']}")
        for r in it["races"]:
            print(f"      {r}")
        if it["n"] > len(it["races"]):
            print(f"      … 외 {it['n'] - len(it['races'])}경주")
    print()
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
