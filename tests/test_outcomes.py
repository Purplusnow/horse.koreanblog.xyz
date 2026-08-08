"""공개 예측 무결성 회귀 테스트 (출주 취소 처리 · 예측 동결).

취소 처리는 조용히 틀리기 쉽다. 취소마를 그냥 두면 실제로는 달리지도 않은 말을
'본명'으로 세어 적중률이 떨어지고, 통째로 경주를 버리면 표본이 줄어든다. 어느
쪽이든 화면에는 그럴듯한 숫자가 찍히므로 눈으로는 발견되지 않는다.

    python tests/test_no_leakage.py 와 같은 방식으로 단독 실행한다.
    PYTHONPATH=src python tests/test_outcomes.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from horseai.kra.store import session  # noqa: E402
from horseai.site import load_outcomes  # noqa: E402
from horseai.verify import load_verified, race_level  # noqa: E402

KEY = "서울-20260101-01"


def build_db(path: str) -> None:
    """한 경주만 담은 최소 DB.

    예상 1순위(1번마)가 출주 취소되고, 2순위(2번마)가 실제로 1착을 한 상황.
    올바른 처리라면 취소마를 뺀 뒤 2번마가 본명이 되고 '적중'으로 판정돼야 한다.
    """
    with session(path) as conn:
        conn.execute(
            "INSERT INTO races (race_key, meet, rc_date, rc_no, distance, "
            "field_size, has_result) VALUES (?,?,?,?,?,?,1)",
            (KEY, "서울", "2026-01-01", 1, 1200, 4),
        )
        horses = [
            # hr_no, name, chul_no, pred_rank, p_win, ord
            ("H1", "취소마", 1, 1, 0.40, None),
            ("H2", "실제우승", 2, 2, 0.30, 1),
            ("H3", "삼번마", 3, 3, 0.20, 2),
            ("H4", "사번마", 4, 4, 0.10, 3),
        ]
        for hr_no, name, chul, rank, p, ordv in horses:
            conn.execute(
                "INSERT INTO entries (race_key, hr_no, hr_name, chul_no) VALUES (?,?,?,?)",
                (KEY, hr_no, name, chul))
            conn.execute(
                "INSERT INTO predictions (race_key, hr_no, pred_rank, p_win, p_place, "
                "model_version) VALUES (?,?,?,?,?,'test')",
                (KEY, hr_no, rank, p, min(1.0, p * 2)))
            if ordv is not None:
                conn.execute(
                    "INSERT INTO results (race_key, hr_no, hr_name, chul_no, ord, win_odds) "
                    "VALUES (?,?,?,?,?,?)",
                    (KEY, hr_no, name, chul, ordv, 3.5))
        conn.execute(
            "INSERT INTO cancellations (race_key, hr_no, hr_name, reason) VALUES (?,?,?,?)",
            (KEY, "H1", "취소마", "질병"))
        conn.commit()


def test_site_outcome_reranks(conn: sqlite3.Connection) -> None:
    out = load_outcomes(conn)
    assert KEY in out, "결과가 있는 경주가 집계에서 빠졌다"
    o = out[KEY]
    assert len(o["cancelled"]) == 1, f"취소마 인식 실패: {o['cancelled']}"
    assert o["top1"]["hr_name"] == "실제우승", \
        f"취소마를 빼고 재순위를 매기지 않았다 (본명={o['top1']['hr_name']})"
    assert o["top1"]["mark"] == "◎", "재순위 후 마크가 갱신되지 않았다"
    assert o["hit_win"] is True, "재순위 본명이 1착인데 적중으로 판정되지 않았다"
    assert o["winner_pick"]["adj_rank"] == 1, "우승마의 표시 순위가 취소 전 값이다"
    print("  ✓ 화면용 결과: 취소마 제외 후 재순위 · 적중 판정")


def test_verify_excludes_cancelled(conn: sqlite3.Connection) -> None:
    df = load_verified(conn)
    assert not df.empty, "검증 대상이 비었다"
    assert "H1" not in set(df["hr_no"]), "취소마가 적중률 집계에 남아 있다"
    top1 = df[df["pred_rank"] == 1].iloc[0]
    assert top1["hr_no"] == "H2", f"집계 순위가 재부여되지 않았다 ({top1['hr_no']})"

    rl = race_level(df)
    assert len(rl) == 1 and rl.iloc[0]["hit_win"] == 1.0, \
        "취소 반영 후에도 적중이 실패로 집계된다"
    print("  ✓ 적중률 집계: 취소마 제외 · 재순위 기준 판정")


def test_no_cancellation_is_unchanged(conn: sqlite3.Connection) -> None:
    """취소가 없으면 원래 순위가 그대로여야 한다 — 재순위가 부작용을 내지 않는지."""
    conn.execute("DELETE FROM cancellations")
    conn.commit()
    o = load_outcomes(conn)[KEY]
    assert o["top1"]["hr_name"] == "취소마", "취소가 없는데 순위가 바뀌었다"
    assert o["hit_win"] is False, "1순위가 1착이 아닌데 적중으로 판정됐다"
    print("  ✓ 취소 없는 경주: 순위·판정 불변")


def test_freeze_by_post_time(path: str) -> None:
    """발주 시각이 지난 경주는 결과가 오기 전에도 잠겨야 한다.

    날짜 단위로만 잠그면 당일 이미 달린 경주가 결과 도착 전까지 열려 있어,
    다음 갱신에서 예측이 다시 쓰인다. '발주 전에 게재한 예상'이라는 전제가
    무너지므로 공개 적중률 전체가 무의미해진다.
    """
    import datetime as dt

    from horseai.clock import now_kst
    from horseai.predict import frozen_race_keys

    # 프로덕션과 같은 기준(KST)으로 픽스처를 만든다. 로컬 시각을 쓰면 UTC 러너에서
    # 과거/미래가 뒤바뀌어, 정작 시간대 버그를 잡아야 할 곳에서 테스트가 통과한다.
    now = now_kst()
    today = now.date().isoformat()
    past = (now - dt.timedelta(hours=1)).strftime("%H:%M")
    future = (now + dt.timedelta(hours=1)).strftime("%H:%M")

    # 취소 테스트와 같은 DB를 쓰면 외래키 때문에 경주를 지울 수 없다. 따로 만든다.
    Path(path).unlink(missing_ok=True)
    with session(path) as conn:
        for no, post in ((1, past), (2, future)):
            key = f"서울-{today.replace('-', '')}-{no:02d}"
            conn.execute(
                "INSERT INTO races (race_key, meet, rc_date, rc_no, distance, "
                "post_time, has_result) VALUES (?,?,?,?,1200,?,0)",
                (key, "서울", today, no, post))
            conn.execute(
                "INSERT INTO predictions (race_key, hr_no, pred_rank, p_win, "
                "p_place, model_version) VALUES (?,'H9',1,0.5,0.7,'test')", (key,))
        conn.commit()

        frozen = frozen_race_keys(conn)
        started = f"서울-{today.replace('-', '')}-01"
        upcoming = f"서울-{today.replace('-', '')}-02"
        assert started in frozen, "발주 시각이 지난 경주가 잠기지 않았다 — 예측 재작성 가능"
        assert upcoming not in frozen, "아직 발주 전인 경주가 잠겼다 — 예측 갱신이 막힌다"
    print("  ✓ 예측 동결: 발주 시각 기준으로 잠김")


def main() -> int:
    path = "data/_test_outcomes.sqlite"
    Path(path).unlink(missing_ok=True)
    build_db(path)
    print("출주 취소 처리 검사")
    with session(path) as conn:
        test_site_outcome_reranks(conn)
        test_verify_excludes_cancelled(conn)
        test_no_cancellation_is_unchanged(conn)
    test_freeze_by_post_time("data/_test_freeze.sqlite")
    Path(path).unlink(missing_ok=True)
    Path("data/_test_freeze.sqlite").unlink(missing_ok=True)
    print("모든 검사 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
