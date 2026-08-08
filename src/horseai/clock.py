"""도메인 시각은 항상 한국 시간으로 읽는다.

한국마사회 경주일과 발주 시각은 KST 로 발표된다. 그런데 이 코드는 GitHub
Actions 의 UTC 러너에서도 돈다. ``date.today()`` 를 그대로 쓰면 러너에서만
날짜가 하루 어긋나고, 그 결과는 조용하다 — 예상이 하루 늦게 올라가거나,
이미 달린 경주가 '아직 발주 전'으로 취급돼 예측이 다시 쓰인다.

그래서 도메인 날짜·시각은 전부 이 모듈을 거친다. 한국은 서머타임이 없으므로
고정 오프셋으로 충분하고, tzdata 설치 여부에 의존하지 않는다.
"""

from __future__ import annotations

import datetime as dt

KST = dt.timezone(dt.timedelta(hours=9), "KST")


def now_kst() -> dt.datetime:
    """현재 한국 시각 (naive — DB·API 문자열과 그대로 비교하기 위함)."""
    return dt.datetime.now(dt.timezone.utc).astimezone(KST).replace(tzinfo=None)


def today_kst() -> dt.date:
    """오늘 (한국 기준 경주일)."""
    return now_kst().date()
