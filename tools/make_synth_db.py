"""합성 경주 DB 생성기 (개발/CI 스모크 테스트용).

실제 API 키 없이 파이프라인 전체(피처 → 학습 → 검증 → 사이트 빌드)를 돌려보기
위한 픽스처. 마필/기수/조교사에 잠재 능력치를 부여하고 그 능력치 + 노이즈로
착순과 기록을 만든다. 배당률은 '진짜 확률에 노이즈를 섞은' 시장으로 흉내 내므로,
모델이 시장 근처까지 따라오면 파이프라인이 정상이라는 신호다.

    python tools/make_synth_db.py --db data/synth.sqlite --years 4
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from horseai.kra.store import session, upsert  # noqa: E402

MEETS = ["서울", "부산경남"]
GRADES = ["국6", "국5", "국4", "국3", "국2", "국1"]
DISTANCES = [1000, 1200, 1300, 1400, 1600, 1800, 2000]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/synth.sqlite")
    ap.add_argument("--years", type=float, default=4)
    ap.add_argument("--horses", type=int, default=1400)
    ap.add_argument("--jockeys", type=int, default=90)
    ap.add_argument("--trainers", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--upcoming", type=int, default=12, help="결과 없는 다가올 경주 수")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    if Path(args.db).exists():
        Path(args.db).unlink()

    horses = {
        f"H{i:05d}": {
            "ability": rng.gauss(0, 1),
            "age": rng.randint(2, 7),
            "sex": rng.choice(["수", "암", "거"]),
            "name": f"합성마{i:04d}",
            # 각질 성향: 0 = 무조건 앞으로(선행), 1 = 끝까지 참았다 몰아침(추입)
            "pace": min(max(rng.betavariate(2.0, 2.0), 0.02), 0.98),
        }
        for i in range(args.horses)
    }
    jockeys = {f"J{i:03d}": rng.gauss(0, 0.45) for i in range(args.jockeys)}
    trainers = {f"T{i:03d}": rng.gauss(0, 0.3) for i in range(args.trainers)}
    hr_ids, jk_ids, tr_ids = list(horses), list(jockeys), list(trainers)

    end = dt.date.today() - dt.timedelta(days=3)
    start = end - dt.timedelta(days=int(365 * args.years))

    race_rows, entry_rows, result_rows = [], [], []
    n_races = 0
    day = start
    while day <= end:
        if day.weekday() not in (4, 5, 6):
            day += dt.timedelta(days=1)
            continue
        # 실제 경마에서 한 마리가 같은 날 두 번 뛰는 일은 없다. 픽스처도 그 제약을
        # 지켜야 이력 피처 테스트가 의미를 갖는다.
        available = hr_ids[:]
        rng.shuffle(available)
        cursor = 0
        for meet in MEETS:
            for rc_no in range(1, rng.randint(8, 12)):
                field = rng.randint(7, 14)
                if cursor + field > len(available):
                    break
                runners = available[cursor:cursor + field]
                cursor += field
                n_races += 1
                distance = rng.choice(DISTANCES)
                grade = rng.choice(GRADES)
                key = f"{meet}-{day.strftime('%Y%m%d')}-{rc_no:02d}"

                race_rows.append({
                    "race_key": key, "meet": meet, "rc_date": day.isoformat(), "rc_no": rc_no,
                    "rc_day": "금토일"[day.weekday() - 4], "rc_name": f"제{rc_no}경주",
                    "distance": distance, "grade": grade, "field_size": field,
                    "post_time": f"{10 + rc_no % 8:02d}:{rng.choice(['00','20','40'])}",
                    "prize1": rng.choice([12, 18, 25, 40, 60]) * 1_000_000,
                    "weather": rng.choice(["맑음", "흐림", "비"]),
                    "track_cond": rng.choice(["건조", "양호", "다습", "포화"]),
                    "has_result": 1,
                })

                # 초반 위치 경쟁 — 각질 성향 + 당일 편차로 S1F 통과순위가 정해진다
                early = sorted(
                    ((horses[hr]["pace"] + rng.gauss(0, 0.13), hr) for hr in runners)
                )
                s1f_rank = {hr: i + 1 for i, (_, hr) in enumerate(early)}
                # 선행마가 몇 두인지가 페이스를 만든다
                n_front = sum(1 for hr in runners if horses[hr]["pace"] < 0.28)

                perf = []
                for gate, hr in enumerate(runners, start=1):
                    jk, tr = rng.choice(jk_ids), rng.choice(tr_ids)
                    # 레이팅은 출주를 몇 번 해야 부여된다. 실데이터에서도 27%가
                    # 비어 있고, 같은 말이라도 초반엔 없다가 나중에 생긴다.
                    # 생성기가 항상 채워 두면 '말마다 고정' 이 되어, 조회 시점
                    # 스냅샷을 잡는 누수 테스트가 헛돈다(그 테스트를 위한 픽스처다).
                    horses[hr]["starts"] = horses[hr].get("starts", 0) + 1
                    rating = (40 + horses[hr]["ability"] * 12 + rng.gauss(0, 4)
                              if horses[hr]["starts"] > 3 else None)
                    burden = round(rng.uniform(51, 58) * 2) / 2
                    pace = horses[hr]["pace"]
                    # 전개 유불리: 선행마가 몰리면 서로 물고 늘어져 앞이 무너지고
                    # 추입마가 어부지리를 얻는다. 한 두뿐이면 편하게 도주한다.
                    if pace < 0.28:
                        pace_edge = (2 - n_front) * 0.18
                    elif pace > 0.58:
                        pace_edge = (n_front - 2) * 0.14
                    else:
                        pace_edge = 0.0
                    latent = (
                        horses[hr]["ability"] * 1.0
                        + jockeys[jk] * 0.5
                        + trainers[tr] * 0.3
                        - (burden - 54.5) * 0.08
                        + pace_edge
                        + rng.gauss(0, 0.95)
                    )
                    perf.append((latent, hr, jk, tr, gate, rating, burden))

                perf.sort(key=lambda t: -t[0])
                base_sec = distance / 16.8
                for pos, (latent, hr, jk, tr, gate, rating, burden) in enumerate(perf, start=1):
                    sec = round(base_sec - latent * 0.55 + rng.gauss(0, 0.25), 1)
                    # 시장: 진짜 실력에 노이즈를 얹은 추정 → 대체로 정확하지만 완벽하진 않다
                    est = (
                        horses[hr]["ability"] + jockeys[jk] * 0.5 + trainers[tr] * 0.3
                    ) + rng.gauss(0, 0.5)
                    perf_est = math.exp(est * 1.25)
                    entry_rows.append({
                        "race_key": key, "chul_no": gate, "hr_no": hr,
                        "hr_name": horses[hr]["name"], "sex": horses[hr]["sex"],
                        "age": horses[hr]["age"], "burden": burden, "rating": round(rating, 1) if rating is not None else None,
                        "jk_no": jk, "jk_name": f"기수{jk[1:]}", "tr_no": tr,
                        "tr_name": f"조교사{tr[1:]}", "ow_name": f"마주{rng.randint(1,300):03d}",
                        "origin": rng.choice(["한", "미", "일"]),
                    })
                    result_rows.append({
                        "race_key": key, "chul_no": gate, "hr_no": hr,
                        "hr_name": horses[hr]["name"], "ord": pos,
                        "jk_no": jk, "jk_name": f"기수{jk[1:]}", "tr_no": tr,
                        "tr_name": f"조교사{tr[1:]}",
                        "burden": burden, "rating": round(rating, 1) if rating is not None else None,
                        "horse_weight": 440 + horses[hr]["ability"] * 8 + rng.gauss(0, 6),
                        "record_sec": sec,
                        "s1f_rank": s1f_rank[hr],
                        # 코너 순위는 초반 위치에서 최종 착순으로 서서히 수렴한다
                        "c1_rank": max(1, round(s1f_rank[hr] * 0.8 + pos * 0.2)),
                        "c2_rank": max(1, round(s1f_rank[hr] * 0.6 + pos * 0.4)),
                        "c3_rank": max(1, round(s1f_rank[hr] * 0.4 + pos * 0.6)),
                        "c4_rank": max(1, round(s1f_rank[hr] * 0.2 + pos * 0.8)),
                        "g1f_rank": pos,
                        # 시장이 경주 전에 본 실력 추정 (배당률 산출용, 착순과 독립)
                        "_mkt_est": perf_est,
                    })

        day += dt.timedelta(days=1)

    # 시장 추정을 경주 단위 확률로 정규화 → 단승 배당(공제율 20% 가정).
    # 배당은 '경주 전 추정'에서만 나오므로 착순을 직접 베끼지 않는다.
    by_race: dict = {}
    for r in result_rows:
        by_race.setdefault(r["race_key"], []).append(r)
    for rows in by_race.values():
        ests = [r.pop("_mkt_est") for r in rows]
        tot = sum(ests) or 1.0
        for r, e in zip(rows, ests):
            p = max(e / tot, 0.005)
            r["win_odds"] = round(0.8 / p, 1)
            r["place_odds"] = round(max(1.0, 0.8 / min(1.0, p * 2.6)), 1)

    # ---- 다가올 경주: 출전표만 있고 결과는 없다 (예측·사이트 빌드 경로 검증용) ----
    upcoming_races, upcoming_entries = [], []
    d = dt.date.today()
    while len(upcoming_races) < args.upcoming:
        if d.weekday() in (4, 5, 6):
            available = hr_ids[:]
            rng.shuffle(available)
            cursor = 0
            for meet in MEETS:
                for rc_no in range(1, 7):
                    if len(upcoming_races) >= args.upcoming:
                        break
                    field = rng.randint(8, 13)
                    if cursor + field > len(available):
                        break
                    runners = available[cursor:cursor + field]
                    cursor += field
                    key = f"{meet}-{d.strftime('%Y%m%d')}-{rc_no:02d}"
                    distance = rng.choice(DISTANCES)
                    upcoming_races.append({
                        "race_key": key, "meet": meet, "rc_date": d.isoformat(), "rc_no": rc_no,
                        "rc_day": "금토일"[d.weekday() - 4], "rc_name": f"제{rc_no}경주",
                        "distance": distance, "grade": rng.choice(GRADES), "field_size": field,
                        "post_time": f"{10 + rc_no:02d}:{rng.choice(['00','20','40'])}",
                        "prize1": rng.choice([12, 18, 25, 40, 60]) * 1_000_000,
                        "age_cond": "연령오픈", "has_result": 0,
                    })
                    for gate, hr in enumerate(runners, start=1):
                        jk, tr = rng.choice(jk_ids), rng.choice(tr_ids)
                        upcoming_entries.append({
                            "race_key": key, "chul_no": gate, "hr_no": hr,
                            "hr_name": horses[hr]["name"], "sex": horses[hr]["sex"],
                            "age": horses[hr]["age"],
                            "burden": round(rng.uniform(51, 58) * 2) / 2,
                            "rating": round(40 + horses[hr]["ability"] * 12 + rng.gauss(0, 4), 1),
                            "jk_no": jk, "jk_name": f"기수{jk[1:]}", "tr_no": tr,
                            "tr_name": f"조교사{tr[1:]}",
                            "ow_name": f"마주{rng.randint(1, 300):03d}",
                            "origin": rng.choice(["한", "미", "일"]),
                        })
        d += dt.timedelta(days=1)

    with session(args.db) as conn:
        upsert(conn, "races", race_rows + upcoming_races, ["race_key"])
        upsert(conn, "entries", entry_rows + upcoming_entries, ["race_key", "chul_no"])
        upsert(conn, "results", result_rows, ["race_key", "hr_no"])

    print(f"합성 DB 생성 완료 → {args.db}")
    print(f"  과거   경주 {n_races:,}회 / 출주 {len(result_rows):,}두 / {start} ~ {end}")
    print(f"  다가올 경주 {len(upcoming_races):,}회 / 출주 {len(upcoming_entries):,}두 (결과 없음)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
