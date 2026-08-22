"""텔레그램 보고문 — 파이프라인이 무엇을 했는지 한 눈에.

'성공했습니다'만 보내면 사흘이면 안 읽게 되고, 그러면 정작 실패한 날도
지나친다. 매번 **무엇이 달라졌는지**를 담아야 읽을 이유가 생긴다.

    python -m horseai.report --db data/horseai.sqlite
"""
from __future__ import annotations

import argparse
import datetime as dt

from .clock import now_kst, today_kst
from .kra.store import session


def build(db: str) -> str:
    now = now_kst()
    today = today_kst()
    lines = [f"경마연구소 · {now:%m/%d %H:%M} KST", ""]

    with session(db) as c:
        q = lambda s, *a: c.execute(s, a).fetchone()          # noqa: E731

        # 앞으로 뛸 경주 — 예상이 올라가 있는지가 아침 보고의 핵심이다
        up = c.execute(
            "SELECT rc_date, COUNT(*) n, "
            "  SUM(EXISTS(SELECT 1 FROM predictions p WHERE p.race_key=g.race_key)) done "
            "FROM races g WHERE rc_date >= ? GROUP BY rc_date ORDER BY rc_date LIMIT 4",
            (today.isoformat(),)).fetchall()
        if up:
            lines.append("■ 게재 현황")
            for r in up:
                d = dt.date.fromisoformat(r["rc_date"])
                mark = "" if r["done"] == r["n"] else "  ← 미완"
                lines.append(f"  {d.month}/{d.day}  {r['done']}/{r['n']}경주{mark}")
        else:
            lines.append("■ 게재 현황\n  다가올 경주 없음 (출전표 미공개)")

        # 어제까지의 성적 — 저녁 보고의 핵심
        row = q("SELECT rc_date, COUNT(*) n, "
                "  SUM(COALESCE(has_result,0)) done FROM races "
                "WHERE rc_date = (SELECT MAX(rc_date) FROM races WHERE COALESCE(has_result,0)=1) "
                "GROUP BY rc_date")
        if row:
            d = dt.date.fromisoformat(row["rc_date"])
            lines += ["", f"■ 최근 정산 {d.month}/{d.day}",
                      f"  {row['done']}/{row['n']}경주"]

        # 복병은 새 기능이라 몇 개 붙었는지 계속 지켜본다
        ls = q("SELECT COUNT(DISTINCT p.race_key) n FROM predictions p "
               "JOIN races g ON g.race_key=p.race_key "
               "WHERE p.longshot=1 AND g.rc_date >= ?", today.isoformat())
        if ls and ls["n"]:
            lines.append(f"  복병 {ls['n']}경주 게재")

    # 누적 성적은 집계 결과에서 읽는다
    try:
        import json
        acc = json.load(open("data/accuracy.json", encoding="utf-8"))
        o = acc.get("overall") or {}
        if o.get("n_races"):
            lines += ["", "■ 누적 성적",
                      f"  {o['n_races']}경주 · 1순위 1착 {o['hit_win']*100:.1f}%"]
        daily = acc.get("daily") or []
        if daily:
            r = daily[0]
            lines.append(f"  최근 경주일 적중 {r['hit_rate']*100:.1f}% · "
                         f"환수 {r['roi']*100:.0f}%")
    except Exception:                                          # noqa: BLE001
        pass

    lines += ["", "https://horse.koreanblog.xyz"]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/horseai.sqlite")
    print(build(ap.parse_args().db))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
