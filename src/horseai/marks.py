"""예상 기호.

기호 규칙은 화면(site)과 검증(verify) 양쪽이 똑같이 써야 한다. 한쪽에만 두면
언젠가 갈리고, 그때 "기호별 실적"은 화면에서 벌어지는 일과 무관한 숫자가 된다.
그래서 규칙은 여기 한 곳에만 둔다.
"""

from __future__ import annotations

from typing import Dict, List

# 예상 기호.
#
# 국내 예상지 관습대로 **자리 수를 고정**해 5두에 배분한다.
#
#   기본        ◎ ◎ ○ △ ※
#   우세 뚜렷    ★ ◎ ○ △ ※
#
# 자리를 고정하면 기호는 '절대적 약속'이 아니라 **경주 안에서의 상대 순위**가
# 된다. 접전 경주에도 ◎ 가 두 개 붙으므로, ◎ 자체가 "2착 이내 유력"을 보장하지는
# 않는다. 그 절대 신호는 **★ 와 신뢰도 등급(강승부/중승부/약승부)**이 맡는다.
# 시중 예상지가 실제로 그렇게 동작하고, 읽는 쪽의 기대와도 맞다.
#
# ★ 는 1순위가 2착 이내에 들 확률이 충분히 높을 때만 준다 — 임계값은 손으로
# 정하지 않고 과거 경주에서 보정한다 (tools/calibrate_marks.py).
MARK_SEQUENCE = ["◎", "◎", "○", "△", "※"]
MARK_SEQUENCE_STAR = ["★", "◎", "○", "△", "※"]
MARK_LIMIT = len(MARK_SEQUENCE)

# 보정 결과 (시간순 교차검증 6,103경주 · 무작위는 1착 9.7% / 2착이내 19.3%):
#   0.58 → 경주의 17%에 등장, ★ 1착 48.2% · 2착이내 68.5%
#   0.62 → 경주의 10%,        ★ 1착 52.8% · 2착이내 72.6%   ← 채택
#   0.66 → 경주의  6%,        ★ 1착 60.5% · 2착이내 78.7%
# ★ 를 단 말이 절반 넘게 우승하는 선을 택했다. 더 올리면 정확해지지만 며칠에
# 한 번 나와 예상지로서 존재감이 없고, 내리면 흔해져 '우세가 뚜렷'이 무색해진다.
MARK_THRESHOLDS = {"star": 0.62}

# 기호 자체가 표기이므로 화면에 이름은 붙이지 않는다. 다만 처음 보는 사람을 위해
# '무엇을 뜻하는가'는 범례와 툴팁으로 남긴다 — 이름이 아니라 뜻이다.
MARK_MEANING = {
    "★": "우세가 뚜렷한 축",
    "◎": "축",
    "○": "상위 후보",
    "△": "복병",
    "※": "참고",
}


def assign_marks(runners: List[Dict]) -> None:
    """예상 기호를 붙인다 (제자리 수정).

    자리 수가 고정이므로 순위대로 배분하면 된다. 다만 1순위가 확실히 앞선
    경주에서는 ◎ 를 둘 붙이는 대신 ★ 하나로 우세를 드러낸다 — 같은 ◎ 두 개는
    '둘 중 하나'라는 뜻이 되어, 실제로는 한 마리가 압도하는 경주를 잘못 전한다.
    """
    ordered = sorted(runners, key=lambda r: r.get("pred_rank") or 99)
    top = ordered[0] if ordered else None
    star = bool(top and (top.get("p_top2") or 0.0) >= MARK_THRESHOLDS["star"])
    seq = MARK_SEQUENCE_STAR if star else MARK_SEQUENCE

    for i, r in enumerate(ordered):
        mark = seq[i] if i < MARK_LIMIT else ""
        r["mark"] = mark
        r["mark_meaning"] = MARK_MEANING.get(mark, "")

