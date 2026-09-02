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
from .kra.collect import (REPLAY_DAY_RATIO, REPLAY_MIN_HORSES,
                          REPLAY_MIN_RACES, REPLAY_MIN_RATIO)
from .kra.store import session

# 승식 일곱 종이 모두 발매되므로, 시행된 경주에는 배당이 이만큼 있어야 한다.
EXPECTED_POOLS = 7

# 파이프라인을 멈춰 세울 만한 결손.
#
# '배당 일부'는 뺀다. 예전에는 넷만 들어온 경주에서 나머지 셋이 불발로
# 집계돼 수치를 망가뜨렸지만, 이제 승식별로 자료가 있을 때만 판정하므로
# 없는 승식은 그냥 집계에서 빠진다(verify.race_level). 수치가 틀리지 않는
# 결손으로 매일 빨간 불을 켜면 진짜 문제를 가린다 — 실제로 8/28 제주 8R
# 하나가 그날 실행 두 번을 다 막았다. 보고는 계속 하되 막지는 않는다.
BLOCKING = ("배당 결손", "착순 결손", "예측 누락", "복제 의심")

# 오래됐다고 봐주지 않는 결손.
#
# 나머지는 '마사회가 끝내 안 보낸 자료'라 며칠 기다린 뒤 포기하는 것이 맞다.
# 복제는 다르다 — 가짜 자료가 DB 에 앉아 마필 전적과 집계를 오염시키고 있는
# 것이라, 시간이 지난다고 나아지지 않는다. 지울 때까지 막는다.
ALWAYS = ("복제 의심",)


def check_replay(conn, days: int) -> List[Dict]:
    """다른 날 편성이 통째로 복제돼 들어온 날이 있는가.

    수집 단계에서 막지만(kra.collect._is_replay) 그 방어는 **새로 받는 자료**
    에만 걸린다. 이미 들어와 있는 것, 그리고 방어가 다시 뚫렸을 때는 아무도
    모른다 — 8/24~26 과 8/31 두 번 다 파이프라인은 초록불이었고 대표가 먼저
    알아챘다. 자료가 제자리에 있는지만 세고 **그것이 진짜인지는 세지 않았기**
    때문이다. 그래서 매 실행마다 다시 훑는다.

    판정 기준은 수집 쪽과 같다. 경주 하나가 겹치는 것은 실제로 있는 일이므로,
    편성 여럿이 같은 하나의 날짜를 가리킬 때만 복제로 본다.
    """
    since = (today_kst() - dt.timedelta(days=days)).isoformat()
    cards = conn.execute(
        "SELECT g.rc_date, g.meet, g.rc_no, e.hr_no FROM entries e "
        "JOIN races g ON g.race_key = e.race_key "
        "WHERE g.rc_date >= ? AND e.hr_no IS NOT NULL", (since,)).fetchall()
    races: Dict[tuple, set] = {}
    for rc_date, meet, rc_no, hr_no in cards:
        races.setdefault((str(rc_date)[:10], meet, rc_no), set()).add(hr_no)

    days_seen: Dict[tuple, List[tuple]] = {}
    for (rc_date, meet, rc_no), horses in races.items():
        days_seen.setdefault((rc_date, meet), []).append((rc_no, horses))

    issues: List[Dict] = []
    for (rc_date, meet), items in sorted(days_seen.items()):
        if len(items) < REPLAY_MIN_RACES:
            continue
        votes: Dict[str, int] = {}
        for rc_no, horses in items:
            if len(horses) < REPLAY_MIN_HORSES:
                continue
            marks = ",".join("?" * len(horses))
            row = conn.execute(
                f"SELECT g.rc_date, COUNT(DISTINCT e.hr_no) n FROM entries e "
                f"JOIN races g ON g.race_key = e.race_key "
                f"WHERE g.meet = ? AND g.rc_no = ? AND g.rc_date <> ? "
                f"AND e.hr_no IN ({marks}) "
                f"GROUP BY g.rc_date ORDER BY n DESC LIMIT 1",
                (meet, rc_no, rc_date, *horses)).fetchone()
            if row and row[1] >= REPLAY_MIN_HORSES and row[1] / len(horses) >= REPLAY_MIN_RATIO:
                day = str(row[0])[:10]
                votes[day] = votes.get(day, 0) + 1
        if not votes:
            continue
        src, n = max(votes.items(), key=lambda kv: kv[1])
        # **나중 것이 복제다.** 두 날짜가 같은 편성을 공유하면 어느 쪽이
        # 가짜인지 내용만으로는 못 가른다 — 그대로 두면 진짜인 8/23 까지
        # '8/31 의 사본' 으로 함께 신고돼 어느 쪽을 지워야 할지 알 수 없다.
        # 포털은 지나간 자료를 물어본 날짜로 바꿔 주므로 복제는 언제나 원본
        # 뒤에 온다. 더 이른 쪽을 원본으로 본다.
        if src >= rc_date:
            continue
        if n >= REPLAY_MIN_RACES and n / len(items) >= REPLAY_DAY_RATIO:
            issues.append({
                "kind": "복제 의심",
                "n": len(items),
                "note": f"{meet} {rc_date} 편성이 {src} 의 사본으로 보인다 "
                        f"— 그날은 경주가 없었을 수 있다",
                "newest": rc_date,
                "races": [f"{meet} {rc_no}R ({rc_date}) ← {src}"
                          for rc_no, _ in sorted(items)[:8]],
            })
    return issues


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
        newest = max(str(r.rc_date)[:10] for r in rows.itertuples())
        issues.append({
            "kind": kind,
            "n": len(rows),
            "note": note,
            "newest": newest,          # 가장 최근 결손일 — 막을지 판단하는 기준
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
                    help="최근 결손이 있으면 1 로 끝낸다 (CI 에서 빨갛게 뜬다)")
    ap.add_argument("--fresh-days", type=int, default=3,
                    help="이 기간 안의 결손만 실패로 본다")
    args = ap.parse_args(argv)

    with session(args.db) as conn:
        issues = check(conn, args.days) + check_replay(conn, args.days)

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
    # 오래된 결손으로 파이프라인을 막지 않는다.
    #
    # 마사회가 끝내 채우지 않는 자료가 있다(8/24 서울 1·3·8·9R 은 사흘이 지나도
    # 1~3착만 있고 배당이 안 왔다). 그런 것 하나가 남으면 --strict 가 매일
    # 실패해 진짜 문제를 가린다 — 실제로 나흘 연속 그렇게 막혔다.
    #
    # 최근 결손만 막는다. 오래된 것은 계속 보고하되 통과시킨다.
    # 경계는 배타적이다. fresh_days=3 이면 오늘·어제·그제 결손만 막고, 딱
    # 사흘 전은 통과시킨다 — 마사회가 하루 이틀은 늦게 올리므로 그만큼 기다린
    # 뒤에도 안 왔으면 끝내 안 오는 것으로 본다.
    cutoff = (today_kst() - dt.timedelta(days=args.fresh_days)).isoformat()
    blocking = [it for it in issues if it["kind"] in BLOCKING]
    fresh = [it for it in blocking
             if it["newest"] > cutoff or it["kind"] in ALWAYS]
    old = [it for it in blocking
           if it["newest"] <= cutoff and it["kind"] not in ALWAYS]
    if len(issues) > len(blocking):
        print(f"  ({len(issues) - len(blocking)}종은 집계를 틀리게 하지 않아 "
              f"통과시킨다 — 없는 승식은 판정에서 빠진다)")
    if old:
        print(f"  ({len(old)}종은 {args.fresh_days}일보다 오래돼 통과시킨다 — "
              f"자료가 끝내 안 올 수 있다)")
    print()
    return 1 if (args.strict and fresh) else 0


if __name__ == "__main__":
    sys.exit(main())
