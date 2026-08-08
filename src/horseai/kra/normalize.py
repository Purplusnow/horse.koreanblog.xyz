"""KRA 응답 레코드 → 내부 표준 스키마 정규화.

포털 API 는 데이터셋마다 필드명 표기가 미묘하게 다르고(``rcDate``/``rc_date``,
``hrNo``/``hr_no``), 예고 없이 표기가 바뀌기도 한다. 그래서

  1) 원본 레코드는 항상 ``raw_json`` 으로 통째 보존하고,
  2) 분석에 쓰는 컬럼만 별칭 목록으로 뽑아낸다.

별칭이 하나도 안 맞으면 값은 None 이 되고, ``audit_coverage`` 로 어떤 필드가
비었는지 즉시 확인할 수 있다. 필드명이 바뀌어도 별칭 한 줄만 추가하면 된다.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterable, List, Optional

# ---------------------------------------------------------------------------
# 값 파서
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def to_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def to_int(v: Any) -> Optional[int]:
    f = to_float(v)
    return int(f) if f is not None else None


def to_float(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").strip()
    if not s or s in {"-", "--", "N/A"}:
        return None
    m = _NUM_RE.search(s)
    return float(m.group()) if m else None


def to_date(v: Any) -> Optional[str]:
    """YYYYMMDD / YYYY-MM-DD 를 YYYY-MM-DD 로."""
    s = to_str(v)
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return s


def to_time(v: Any) -> Optional[str]:
    """출발시각(1015 / 10:15) 을 HH:MM 으로."""
    s = to_str(v)
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) in (3, 4):
        digits = digits.zfill(4)
        return f"{digits[:2]}:{digits[2:]}"
    return s


def to_seconds(v: Any) -> Optional[float]:
    """경주기록('1:12.3', '72.3', '0:00.0') → 초."""
    s = to_str(v)
    if not s:
        return None
    if ":" in s:
        parts = s.split(":")
        try:
            mins = float(parts[0])
            secs = float(parts[1])
        except ValueError:
            return None
        total = mins * 60 + secs
    else:
        f = to_float(s)
        if f is None:
            return None
        total = f
    return total if total > 0 else None


def to_pos(v: Any) -> Optional[float]:
    """0 을 결측으로 취급한다.

    KRA 응답은 '해당 없음'을 0 으로 채워 보낸다. 경마장마다 측정 구간이 달라
    서울 필드는 부경 경주에서 0, 레이팅은 하위 등급에서 0 이 된다. 이를 0 이라는
    값으로 학습하면 '레이팅 0인 말'이라는 존재하지 않는 개념이 생긴다.
    """
    f = to_float(v)
    return f if f and f > 0 else None


def to_pos_int(v: Any) -> Optional[int]:
    f = to_pos(v)
    return int(f) if f is not None else None


def to_weight(v: Any) -> Optional[float]:
    """마체중 '301(-3)' → 301.0"""
    s = to_str(v)
    if not s:
        return None
    head = s.split("(")[0]
    return to_pos(head)


def to_weight_delta(v: Any) -> Optional[float]:
    """마체중 '301(-3)' → -3.0 (직전 대비 증감). 괄호가 없으면 결측."""
    s = to_str(v)
    if not s or "(" not in s:
        return None
    inner = s[s.index("(") + 1:].rstrip(")")
    return to_float(inner)


# 같은 경마장을 API마다 다르게 적는다: 출전표는 '부산경남', 경주성적은 '부경'.
# 이걸 놓치면 두 테이블의 race_key 가 어긋나 조인이 조용히 절반만 성립한다.
MEET_CANON = {
    "부경": "부산경남", "부산": "부산경남", "부산경남": "부산경남",
    "서울": "서울", "제주": "제주", "영천": "영천",
}


def to_meet(v: Any) -> Optional[str]:
    """경마장명을 하나의 표기로 통일한다."""
    s = to_str(v)
    if not s:
        return None
    return MEET_CANON.get(s.strip(), s.strip())


def to_ord(v: Any) -> Optional[int]:
    """착순. 실격/제외/낙마 등 비완주는 None 으로 떨어뜨린다."""
    s = to_str(v)
    if not s:
        return None
    if re.search(r"[가-힣A-Za-z]", s) and not _NUM_RE.search(s):
        return None
    n = to_int(s)
    return n if n and n > 0 else None


# ---------------------------------------------------------------------------
# 필드 사양
# ---------------------------------------------------------------------------

class Field:
    __slots__ = ("name", "aliases", "cast", "required")

    def __init__(self, name: str, aliases: Iterable[str], cast: Callable[[Any], Any] = to_str,
                 required: bool = False):
        self.name = name
        self.aliases = list(aliases)
        self.cast = cast
        self.required = required


def _norm_key(k: str) -> str:
    """비교용 키 정규화: 대소문자·언더스코어 무시."""
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


def extract(record: Dict[str, Any], fields: List[Field]) -> Dict[str, Any]:
    """별칭 매칭으로 표준 컬럼을 뽑는다.

    핵심은 '먼저 존재하는 키'가 아니라 **'먼저 쓸모 있는 값을 내는 키'**를
    고른다는 점이다. KRA 응답은 경마장별 구간 필드를 전부 담아 보내면서 해당
    없는 쪽을 0 으로 채운다. 존재 여부로만 고르면 서울 경주에서도 제주 필드(0)가
    먼저 선택돼 값이 통째로 사라진다. 캐스팅 결과가 None 이면 다음 별칭으로 넘긴다.
    """
    index = {_norm_key(k): v for k, v in record.items()}
    out: Dict[str, Any] = {}
    for f in fields:
        value = None
        for alias in f.aliases:
            got = index.get(_norm_key(alias))
            if got is None or str(got).strip() == "":
                continue
            cast = f.cast(got)
            if cast is not None:
                value = cast
                break
        out[f.name] = value
    return out


def audit_coverage(records: List[Dict[str, Any]], fields: List[Field]) -> Dict[str, float]:
    """정규화 후 각 컬럼이 실제로 채워진 비율. 별칭 오류 조기 발견용."""
    if not records:
        return {}
    rows = [extract(r, fields) for r in records]
    n = len(rows)
    return {f.name: round(sum(1 for r in rows if r[f.name] is not None) / n, 3) for f in fields}


def unmapped_keys(records: List[Dict[str, Any]], fields: List[Field]) -> List[str]:
    """어떤 별칭에도 걸리지 않은 원본 키들 — 놓친 정보 점검용."""
    known = {_norm_key(a) for f in fields for a in f.aliases}
    seen: Dict[str, None] = {}
    for r in records:
        for k in r:
            if _norm_key(k) not in known:
                seen[k] = None
    return sorted(seen)


# ---------------------------------------------------------------------------
# 출전표(entrySheet_2) — 경주 전 정보
# ---------------------------------------------------------------------------

ENTRY_FIELDS: List[Field] = [
    Field("meet", ["meet"], to_meet, required=True),
    Field("rc_date", ["rcDate"], to_date, required=True),
    Field("rc_no", ["rcNo"], to_int, required=True),
    Field("rc_day", ["rcDay"], to_str),
    Field("chul_no", ["chulNo"], to_int, required=True),
    Field("hr_no", ["hrNo"], to_str, required=True),
    Field("hr_name", ["hrName"], to_str, required=True),
    Field("hr_name_en", ["hrNameEn"], to_str),
    Field("origin", ["prd", "hrOrigin", "prdcNat", "nation"], to_str),
    Field("sex", ["sex"], to_str),
    Field("age", ["age"], to_int),
    Field("burden", ["wgBudam"], to_float),
    # 레이팅은 하위 등급 경주에서 0 으로 온다 — 0 은 '없음'이지 값이 아니다.
    Field("rating", ["rating"], to_pos),
    Field("jk_no", ["jkNo"], to_str),
    Field("jk_name", ["jkName"], to_str),
    Field("tr_no", ["trNo"], to_str),
    Field("tr_name", ["trName"], to_str),
    Field("ow_no", ["owNo"], to_str),
    Field("ow_name", ["owName"], to_str),
    Field("distance", ["rcDist"], to_int),
    Field("field_size", ["dusu"], to_int),
    # 등급은 'rank'(제6등급). rating 별칭에서 rank 를 빼야 여기로 온다.
    Field("grade", ["rank", "rcClass", "gradeCond"], to_str),
    Field("prize_cond", ["prizeCond"], to_str),
    Field("age_cond", ["ageCond"], to_str),
    Field("sex_cond", ["sexCond"], to_str),
    Field("post_time", ["stTime", "startTime"], to_time),
    Field("budam_type", ["budam"], to_str),
    Field("rc_name", ["rcName"], to_str),
    Field("prize1", ["chaksun1"], to_float),
    Field("prize2", ["chaksun2"], to_float),
    Field("prize3", ["chaksun3"], to_float),
    Field("prize4", ["chaksun4"], to_float),
    Field("prize5", ["chaksun5"], to_float),
    Field("ilsu", ["ilsu"], to_int),
    # 통산/최근 성적 — 출전표가 직접 제공하므로 별도 조회 없이 피처가 된다.
    Field("career_prize", ["chaksunT"], to_float),
    Field("prize_1y", ["chaksunY"], to_float),
    Field("prize_6m", ["chaksun_6m"], to_float),
    Field("career_1st", ["ord1CntT"], to_int),
    Field("career_2nd", ["ord2CntT"], to_int),
    Field("career_3rd", ["ord3CntT"], to_int),
    Field("career_starts", ["rcCntT"], to_int),
    Field("y1_1st", ["ord1CntY"], to_int),
    Field("y1_2nd", ["ord2CntY"], to_int),
    Field("y1_3rd", ["ord3CntY"], to_int),
    Field("y1_starts", ["rcCntY"], to_int),
]

# ---------------------------------------------------------------------------
# 경주성적(RaceDetailResult_1) — 경주 후 결과
# ---------------------------------------------------------------------------

RESULT_FIELDS: List[Field] = [
    Field("meet", ["meet"], to_meet, required=True),
    Field("rc_date", ["rcDate"], to_date, required=True),
    Field("rc_no", ["rcNo"], to_int, required=True),
    Field("rc_day", ["rcDay"], to_str),
    Field("chul_no", ["chulNo"], to_int),
    Field("hr_no", ["hrNo"], to_str, required=True),
    Field("hr_name", ["hrName"], to_str),
    Field("ord", ["ord"], to_ord),
    Field("ord_note", ["ordBigo"], to_str),
    Field("jk_no", ["jkNo"], to_str),
    Field("jk_name", ["jkName"], to_str),
    Field("tr_no", ["trNo"], to_str),
    Field("tr_name", ["trName"], to_str),
    Field("ow_no", ["owNo"], to_str),
    Field("ow_name", ["owName"], to_str),
    Field("distance", ["rcDist"], to_int),
    Field("grade", ["rank", "rcClass"], to_str),
    Field("age_cond", ["ageCond"], to_str),
    Field("prize_cond", ["prizeCond"], to_str),
    Field("budam_type", ["budam"], to_str),
    Field("weather", ["weather"], to_str),
    Field("track_cond", ["track"], to_str),
    Field("rc_name", ["rcName"], to_str),
    Field("age", ["age"], to_int),
    Field("sex", ["sex"], to_str),
    Field("origin", ["name"], to_str),
    Field("birthday", ["birthday"], to_date),
    Field("burden", ["wgBudam"], to_float),
    Field("burden_note", ["wgBudamBigo"], to_str),
    Field("jk_weight", ["wgJk"], to_pos),
    Field("rating", ["rating"], to_pos),
    # 마체중은 '301(-3)' 형태 — 체중과 직전 대비 증감이 한 필드에 들어 있다.
    Field("horse_weight", ["wgHr"], to_weight),
    Field("weight_delta", ["wgHr"], to_weight_delta),
    Field("record_sec", ["rcTime"], to_seconds),
    Field("margin", ["diffUnit"], to_str),
    Field("win_odds", ["winOdds"], to_pos),
    Field("place_odds", ["plcOdds"], to_pos),
    Field("gear", ["hrTool"], to_str),
    Field("rank_rise", ["rankRise"], to_int),
    Field("ilsu", ["ilsu"], to_int),
    Field("prize1", ["chaksun1"], to_float),
    Field("prize2", ["chaksun2"], to_float),
    Field("prize3", ["chaksun3"], to_float),
    Field("prize4", ["chaksun4"], to_float),
    Field("prize5", ["chaksun5"], to_float),
    Field("bonus1", ["buga1"], to_float),
    Field("bonus2", ["buga2"], to_float),
    Field("bonus3", ["buga3"], to_float),
    # --- 구간 통과순위 -----------------------------------------------------
    # 순위는 경마장과 무관하게 sj* 로 통일돼 있고, 부경은 자체 필드도 함께 준다.
    # 해당 없는 구간은 0 으로 오므로 to_pos_int 로 결측 처리한다.
    Field("s1f_rank", ["sjS1fOrd", "buS1fOrd"], to_pos_int),
    Field("c1_rank", ["sj_1cOrd", "buG8fOrd"], to_pos_int),
    Field("c2_rank", ["sj_2cOrd", "buG6fOrd"], to_pos_int),
    Field("c3_rank", ["sj_3cOrd", "buG4fOrd"], to_pos_int),
    Field("c4_rank", ["sj_4cOrd", "buG3fOrd"], to_pos_int),
    Field("g3f_rank", ["sjG3fOrd", "buG2fOrd"], to_pos_int),
    Field("g1f_rank", ["sjG1fOrd", "buG1fOrd"], to_pos_int),
    # --- 구간 기록(초) -----------------------------------------------------
    # 여기만 경마장별로 필드가 다르다: 서울 se*AccTime / 부경 bu* / 제주 je*Time
    Field("s1f_sec", ["jeS1fTime", "buS1fTime", "seS1fAccTime", "buS1fAccTime"], to_pos),
    Field("g3f_sec", ["jeG3fTime", "seG3fAccTime", "buG3fAccTime"], to_pos),
    Field("g1f_sec", ["jeG1fTime", "seG1fAccTime", "buG1fAccTime"], to_pos),
    Field("c4_sec", ["je_4cTime", "se_4cAccTime", "buG4fAccTime"], to_pos),
]


def race_key(meet: Any, rc_date: Any, rc_no: Any) -> str:
    """경주 고유키: 경마장-일자-경주번호.

    경마장명은 반드시 정규화해서 넣는다. API마다 '부경'/'부산경남'을 섞어 쓰기
    때문에, 여기서 통일하지 않으면 같은 경주가 두 개의 키로 갈라진다.
    """
    d = re.sub(r"\D", "", str(to_date(rc_date) or ""))
    return f"{to_meet(meet)}-{d}-{int(to_float(rc_no) or 0):02d}"
