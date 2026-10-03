"""지면(레거시 예상지) 생성 — 하루치 예상을 전문지 지면 한 장으로 굽는다.

사이트와 **같은 자료**(우리 예측)를 쓰되, 보여 주는 방식만 1990년대 경마 전문지의
윤전 지면을 흉내 낸다. 경쟁지 내용은 한 줄도 가져오지 않는다.

경마장으로 가르지 않고 **발주 시각 순**으로 늘어놓는다 — 지면을 든 사람은
서울과 제주를 번갈아 보는 것이 아니라 다음 경주를 보기 때문이다.

**왜 경주 블록만 파이썬에서 찍고 페이지 껍데기는 Jinja 인가.**
경주 블록은 표가 네 겹으로 겹친 조각이고, 구간기록 판별·조사 교정·순위 번호가
다 거기 얽혀 있다. 그 부분은 눈으로 검증해 맞춰 놓은 것이라 템플릿으로 옮기면
얻는 것 없이 깨질 위험만 진다. 반면 제호·광고 자리·GA4·canonical 은 리포의
다른 페이지와 **같은 규칙으로** 다뤄야 하므로 templates/paper.html 에 둔다.

    config build.paper 로 끈다. 끄면 페이지도 사이트맵 항목도 생기지 않는다.
"""

from __future__ import annotations

import datetime as dt
import html
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

from .marks import assign_marks
from .style import STYLES

STYLE_LABEL = {s["code"]: s["label"] for s in STYLES}
WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]

# 광고를 몇 경주마다 끼울 것인가.
#
# 이 지면의 수익은 노출 수가 아니라 **viewable 노출**에서 난다. 세로로 긴 한
# 장이라 하단 유닛은 끝까지 스크롤한 사람만 보지만, 경주 사이 유닛은 지면을
# 읽는 유일한 방법이 그걸 지나가는 것이어서 노출이 구조적으로 보장된다.
#
# 그렇다고 경주마다 넣지는 않는다. 유닛을 늘리면 같은 방문자를 같은 경매에 더
# 많이 내놓는 것이라 노출당 값이 내려가고(수익은 유닛 수에 비례하지 않는다),
# 무엇보다 '자료 사이에 광고'가 아니라 '광고 사이에 자료'로 읽히면 스크롤이
# 중간에 멈춰 아래쪽 유닛이 통째로 죽는다. 밀도를 올려 아래를 죽이는 자기모순이다.
# 2경주마다. 출주마 일람(전 두수 출마표)이 들어가면서 경주 블록 하나가 표
# 네 겹으로 늘어 지면이 78% 길어졌다(193KB → 345KB). 간격을 3경주로 두면
# 광고 1개당 본문 59행이 되어 Better Ads 가 보는 모바일 광고 밀도 30% 선에서
# 한참 아래로 내려간다 — 자리를 비워 두는 것이지 지면을 지키는 것이 아니다.
# 2경주로 조이면 광고당 약 41행, 밀도는 여전히 10% 안쪽이다.
AD_EVERY = 2
# 첫 간격(1경주 뒤)은 비운다. 지면에 들어온 사람이 읽는 리듬을 잡기 전에 끊는
# 자리라 이탈로 가장 비싸게 치른다. 이탈한 세션은 어느 유닛에서도 벌지 못한다.
AD_SKIP_FIRST = True


def stars(conf):
    """혼전도 — 신뢰도가 높을수록 별이 적다. 다섯이면 '축마 없는 혼전'."""
    if conf is None:
        return 5
    return 2 if conf >= 52 else 3 if conf >= 48 else 4 if conf >= 41 else 5


def headline(label, top):
    name = top["hr_name"] or ""
    if label == "강승부":
        return f"{name} 선전 기대"
    if label == "중승부":
        return f"{name} 우세 속 혼전"
    return "축마 없는 혼전"


def jo(word, with_batchim, without):
    """받침에 맞춰 조사를 고른다. '원평캣는'처럼 쓰면 지면이 아니라 기계가 된다."""
    w = (word or "").strip()
    if not w:
        return without
    ch = w[-1]
    if "가" <= ch <= "힣":
        return with_batchim if (ord(ch) - 0xAC00) % 28 else without
    return without if ch in "02345679aeiouAEIOU" else with_batchim


def un(w):  return jo(w, "은", "는")
def iga(w): return jo(w, "이", "가")
def eul(w): return jo(w, "을", "를")
def wa(w):  return jo(w, "과", "와")


def esc(v):
    return html.escape("" if v is None else str(v))


def pct(v):
    return "—" if v is None else f"{v * 100:.1f}"


def segment(rec, dist, raw, seg_m):
    """구간 소요시간을 되찾는다.

    **경마장마다 값의 뜻이 다르다.** 서울·부산경남은 출발부터의 통과 누적시간을
    주고(1200m 기록 74.3 / G3F 36.4), 제주는 구간 소요시간을 그대로 준다
    (1000m 기록 82.2 / G3F 48.6). 어느 쪽인지 적어 둔 곳이 없어 경마장 이름으로
    가르면 영천 같은 새 경마장에서 또 깨진다.

    그래서 **속도가 말이 되는 쪽**을 행마다 고른다. 그 구간의 평균 속도는 경주
    전체 평균 속도와 크게 다를 수 없다 — 말이 막판에 두 배로 빨라지지는 않는다.
    """
    if not (rec and dist and raw) or rec <= 0:
        return None
    base = dist / rec                      # 경주 전체 평균 (m/s)
    best, score = None, None
    for cand in (raw, rec - raw):          # 구간 그대로 / 누적에서 되짚기
        if cand is None or cand <= 1:
            continue
        ratio = (seg_m / cand) / base
        if not 0.75 <= ratio <= 1.25:
            continue
        off = abs(ratio - 1.0)
        if score is None or off < score:
            best, score = cand, off
    return best


# ── 자료 ─────────────────────────────────────────────────────

def load_day(c, day: str) -> List[Dict]:
    """그 날짜의 경주를 발주 순으로, 출전마·과거전적·구간기록까지 붙여 돌려준다."""
    races = c.execute(
        """SELECT race_key, meet, rc_no, distance, grade, age_cond, budam_type,
                  post_time, field_size
           FROM races WHERE rc_date=? ORDER BY post_time, meet""",
        (day,),
    ).fetchall()

    # 과거 출주 전체. 착순 사다리와 구간기록이 둘 다 여기서 나온다.
    #
    # **g3f_sec·g1f_sec 는 구간 기록이 아니라 통과 누적시간이다.** 마사회가
    # '3F 통과'로 주는 값이라, 후반 3화롱은 주파기록에서 빼야 나온다.
    # 그대로 실으면 후반 1F 가 74초인 말이 생긴다.
    past = defaultdict(list)
    for r in c.execute(
        """SELECT r.hr_no, g.rc_date, g.meet, g.rc_no, g.distance, g.field_size, r.ord,
                  r.s1f_sec, r.g3f_sec, r.g1f_sec, r.record_sec, r.c4_rank, r.horse_weight
           FROM results r JOIN races g ON g.race_key=r.race_key
           WHERE r.ord IS NOT NULL AND g.rc_date < ?
             AND r.hr_no IN (SELECT p.hr_no FROM predictions p
                             JOIN races x ON x.race_key=p.race_key WHERE x.rc_date=?)
           ORDER BY g.rc_date DESC""",
        (day, day),
    ):
        d = dict(r)
        rec, dist = d["record_sec"], d["distance"]
        d["last3f"] = segment(rec, dist, d["g3f_sec"], 600)
        d["last1f"] = segment(rec, dist, d["g1f_sec"], 200)
        d["kmh"] = (dist / rec * 3.6) if (rec and dist) else None
        past[d["hr_no"]].append(d)

    out = []
    for g in races:
        sim = c.execute(
            "SELECT conf_score, conf_label, conf_desc FROM simulations WHERE race_key=?",
            (g["race_key"],),
        ).fetchone()
        rows = c.execute(
            """SELECT p.hr_no, p.pred_rank, p.chul_no, p.p_win, p.p_place, p.p_top2,
                      p.style_code, COALESCE(p.longshot,0) longshot,
                      e.hr_name, e.jk_name, e.tr_name, e.burden, e.sex, e.age,
                      e.career_starts, e.career_1st, e.career_2nd, e.career_3rd,
                      e.y1_starts, e.y1_1st
               FROM predictions p
               LEFT JOIN entries e ON e.race_key=p.race_key AND e.hr_no=p.hr_no
               WHERE p.race_key=? ORDER BY p.pred_rank""",
            (g["race_key"],),
        ).fetchall()
        runners = []
        for r in rows:
            d = dict(r)
            d["past"] = past.get(d["hr_no"], [])
            d["tempo"] = tempo(d["past"], g["distance"])
            # 우리 자료는 2021-08 이후뿐이다. 마사회 공식 전적이 그보다 많으면
            # 이 말의 옛 경주는 우리에게 없다 — 그 사실을 문장이 알아야 한다.
            d["tempo"]["full"] = (d["career_starts"] or 0) <= len(d["past"])
            runners.append(d)
        picks = [r for r in runners if (r["pred_rank"] or 99) <= 5]
        assign_marks(picks)
        rec = dict(g)
        rec["all"] = runners
        rec["picks"] = picks
        rec["longshot"] = next((r for r in runners if r["longshot"]), None)
        rec["conf_label"] = sim["conf_label"] if sim else None
        rec["conf_score"] = sim["conf_score"] if sim else None
        rec["conf_desc"] = sim["conf_desc"] if sim else ""
        out.append(rec)
    return out


# ── 조각 ─────────────────────────────────────────────────────

def horse_cell(hs):
    out = []
    for h in hs:
        if not h:
            continue
        out.append(
            '<span class="hs"><b class="no">%s</b><span class="nm">%s</span></span>'
            % (esc(h["chul_no"]), esc(h["hr_name"]))
        )
    return "".join(out) or '<span class="none">—</span>'


def avg(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def tempo(past, dist):
    """구간기록 요약. 최근 5전으로 현재 몸 상태를, 통산으로 거리 적성을 본다."""
    recent = past[:5]
    same = [q for q in past if q["distance"] == dist and q["record_sec"]]
    return {
        "s1f": avg(q["s1f_sec"] for q in recent
                   if q["s1f_sec"] and q["record_sec"] and q["distance"]
                   and 0.6 <= (200 / q["s1f_sec"]) / (q["distance"] / q["record_sec"]) <= 1.35),
        "l3f": avg(q["last3f"] for q in recent),
        "l1f_best": min((q["last1f"] for q in recent if q["last1f"]), default=None),
        "kmh": avg(q["kmh"] for q in recent),
        "dist_n": len(same),
        "dist_best": min((q["record_sec"] for q in same), default=None),
        "n": len(past),
    }


def mmss(sec):
    """1:28.6 꼴. 경마 기록은 분·초로 읽는다."""
    if not sec:
        return "—"
    m, s = divmod(sec, 60)
    return "%d:%04.1f" % (int(m), s) if m else "%.1f" % s


def summary_table(races):
    body = []
    for r in races:
        p = r["picks"]
        axis = p[0] if r["conf_label"] == "강승부" else None
        win = [x for x in p[:2] if x is not axis]
        n = stars(r["conf_score"])
        body.append(
            """<tr>
 <td class="c-time"><b>%s</b></td>
 <th scope="row" class="c-rc"><span class="mt mt-%s">%s</span><b>%d</b><i>%dm</i></th>
 <td class="c-cond">%s<br><span class="dim">%d두</span></td>
 <td class="c-h">%s</td>
 <td class="c-h">%s</td>
 <td class="c-h">%s</td>
 <td class="c-h">%s</td>
 <td class="c-star"><span class="stars" aria-label="혼전도 5점 만점에 %d점">%s<u>%s</u></span></td>
</tr>"""
            % (
                esc(r["post_time"]), esc(r["meet"]), esc(r["meet"]), r["rc_no"], r["distance"],
                esc(r["grade"]), r["field_size"],
                horse_cell([axis]) if axis else '<span class="none">축마 없음</span>',
                horse_cell(win), horse_cell(p[2:4]),
                horse_cell([r["longshot"]]) if r["longshot"] else '<span class="none">—</span>',
                n, "★" * n, "★" * (5 - n),
            )
        )
    return """<div class="oval-wrap"><span class="oval">한눈에 보는 경주별 베팅 포인트</span></div>
<div class="scroll">
<table class="grid summary">
 <colgroup><col class="w-time"><col class="w-rc"><col class="w-cond"><col><col><col><col><col class="w-star"></colgroup>
 <thead>
  <tr>
   <th scope="col" rowspan="2">발주</th><th scope="col" rowspan="2">경주</th>
   <th scope="col" rowspan="2">등급·두수</th>
   <th scope="col" colspan="3">예 상</th>
   <th scope="col" rowspan="2">복병</th><th scope="col" rowspan="2">혼전도</th>
  </tr>
  <tr><th scope="col">강축</th><th scope="col">우승도전</th><th scope="col">입상도전</th></tr>
 </thead>
 <tbody>%s</tbody>
</table>
</div>""" % "".join(body)


def ladder(h, n_show=5):
    """최근 착순 사다리. 왼쪽이 최근이다."""
    ps = h["past"][:n_show]
    if not ps:
        return '<span class="rookie">신마</span>'
    out = []
    for q in ps:
        o = q["ord"]
        cls = "f1" if o == 1 else "f2" if o == 2 else "f3" if o == 3 else ""
        # 옛 경주에는 거리·두수가 비어 있는 것이 있다. 착순만 있어도 사다리는 선다.
        n = q["field_size"]
        dist = ("%dm" % q["distance"]) if q["distance"] else "거리 미상"
        tip = "%s %s %sR %s · %d착%s" % (
            q["rc_date"], q["meet"], q["rc_no"], dist, o,
            ("/%d두" % n) if n else "")
        out.append(
            '<span class="fin %s" title="%s"><b>%d</b><i>%s</i></span>'
            % (cls, esc(tip), o, (str(n) if n else "·"))
        )
    return '<span class="lad">%s</span>' % "".join(out)


def record(h):
    s = h["career_starts"] or 0
    w, p2, p3 = h["career_1st"] or 0, h["career_2nd"] or 0, h["career_3rd"] or 0
    if not s:
        return '<span class="none">—</span>'
    rate = (w + p2) / s * 100
    return ('<b>%d전</b> <span class="rec">%d·%d·%d</span>'
            '<i class="dim2">복승 %.0f%%</i>') % (s, w, p2, p3, rate)


def bar(v):
    if v is None:
        return "—"
    w = max(2.0, min(100.0, v * 100 * 2.2))
    return ('<span class="pb"><i style="width:%.1f%%"></i></span>'
            '<span class="pv">%.1f</span>') % (w, v * 100)


def pick_rows(r):
    rows = []
    for h in r["picks"]:
        mk = h.get("mark") or ""
        cls = "mk-star" if mk == "★" else ("mk-axis" if mk == "◎" else "")
        rows.append(
            """<tr>
 <td class="c-mk"><span class="mk %s">%s</span></td>
 <td class="c-no"><b>%s</b></td>
 <td class="c-nm"><span class="hn">%s</span><i class="sub">%s%s · %s</i></td>
 <td class="c-jk">%s<i class="sub">%s</i></td>
 <td class="c-st">%s</td>
 <td class="c-rd">%s</td>
 <td class="c-lad">%s</td>
 <td class="c-p">%s</td>
 <td class="c-p2">%s</td>
</tr>"""
            % (cls, esc(mk), esc(h["chul_no"]), esc(h["hr_name"]),
               esc(h["sex"] or ""), esc(h["age"] or ""),
               ("%.1fkg" % h["burden"]) if h["burden"] else "부중 —",
               esc(h["jk_name"] or "—"), esc(h["tr_name"] or ""),
               esc(STYLE_LABEL.get(h["style_code"], "—")),
               record(h), ladder(h), bar(h["p_win"]), pct(h["p_place"]))
        )
    ls = r["longshot"]
    if ls:
        rows.append(
            """<tr class="ls">
 <td class="c-mk"><span class="mk mk-ls">복<br>병</span></td>
 <td class="c-no"><b>%s</b></td>
 <td class="c-nm"><span class="hn">%s</span><i class="sub">%s%s · %s</i></td>
 <td class="c-jk">%s<i class="sub">%s</i></td>
 <td class="c-st">%s</td>
 <td class="c-rd">%s</td>
 <td class="c-lad">%s</td>
 <td class="c-p ls-note" colspan="2">추천 5두 밖 — 예상 %s위에서 3착 이내를 노린다</td>
</tr>"""
            % (esc(ls["chul_no"]), esc(ls["hr_name"]),
               esc(ls["sex"] or ""), esc(ls["age"] or ""),
               ("%.1fkg" % ls["burden"]) if ls["burden"] else "부중 —",
               esc(ls["jk_name"] or "—"), esc(ls["tr_name"] or ""),
               esc(STYLE_LABEL.get(ls["style_code"], "—")),
               record(ls), ladder(ls), esc(ls["pred_rank"]))
        )
    return "".join(rows)


def entry_table(r):
    """출마표 — 전 출전마를 **마번 순**으로 늘어놓는다.

    종이 예상지의 본체가 이 표다. 추천 다섯 두만 실으면 '왜 저 말은 안 뽑았나'
    를 확인할 길이 없고, 마권을 짜는 사람은 1번부터 차례로 훑으며 자기 조합을
    만든다. 추천은 우리 의견이고 이 표는 그 판의 전부다.

    맨 오른쪽에 우리 기호를 함께 세워 두 표를 눈으로 이을 수 있게 한다.
    """
    rows = []
    for h in sorted(r["all"], key=lambda x: int(x["chul_no"] or 0)):
        mk = h.get("mark") or ""
        if mk:
            tag = '<span class="mk %s">%s</span>' % (
                "mk-star" if mk == "★" else ("mk-axis" if mk == "◎" else ""), esc(mk))
        elif h["longshot"]:
            tag = '<span class="mk mk-ls">복병</span>'
        else:
            tag = '<i class="dim2">%s위</i>' % esc(h["pred_rank"] or "—")
        y1s, y1w = h["y1_starts"] or 0, h["y1_1st"] or 0
        y1 = ("%d전 %d승" % (y1s, y1w)) if y1s else '<span class="none">—</span>'
        rows.append(
            """<tr%s>
 <td class="c-no"><b>%s</b></td>
 <td class="e-nm"><span class="hn">%s</span><i class="sub">%s%s</i></td>
 <td class="e-bd">%s</td>
 <td class="c-jk">%s<i class="sub">%s</i></td>
 <td class="c-st">%s</td>
 <td class="c-rd">%s</td>
 <td class="e-y1">%s</td>
 <td class="c-lad">%s</td>
 <td class="e-pw">%s</td>
 <td class="e-mk">%s</td>
</tr>"""
            % (' class="is-pick"' if mk else (' class="ls"' if h["longshot"] else ""),
               esc(h["chul_no"]), esc(h["hr_name"]),
               esc(h["sex"] or ""), esc(h["age"] or ""),
               ("%.1f" % h["burden"]) if h["burden"] else "—",
               esc(h["jk_name"] or "—"), esc(h["tr_name"] or ""),
               esc(STYLE_LABEL.get(h["style_code"], "—")),
               record(h), y1, ladder(h, 3),
               pct(h["p_win"]), tag)
        )
    return ('<div class="entry-wrap">\n'
            ' <h4 class="sub-h">출주마 일람'
            '<span class="thn2">마번 순 · 이 경주에 나온 전 두수</span></h4>\n'
            ' <div class="scroll">\n'
            ' <table class="grid entry">\n'
            '  <colgroup><col class="w-no"><col class="w-enm"><col class="w-ebd">'
            '<col class="w-jk"><col class="w-st"><col class="w-rd"><col class="w-ey1">'
            '<col class="w-lad3"><col class="w-epw"><col class="w-emk"></colgroup>\n'
            '  <thead><tr>\n'
            '   <th scope="col">마번</th><th scope="col">마 명<span class="thn">성·연령</span></th>\n'
            '   <th scope="col">부중<span class="thn">kg</span></th>\n'
            '   <th scope="col">기수·조교사</th><th scope="col">각질</th>\n'
            '   <th scope="col">통산 전적<span class="thn">마사회 공식</span></th>\n'
            '   <th scope="col">금년</th>\n'
            '   <th scope="col">최근 3회<span class="thn">우리 보유</span></th>\n'
            '   <th scope="col">우승 %%</th><th scope="col">예상</th>\n'
            '  </tr></thead>\n  <tbody>%s</tbody>\n </table>\n </div>\n</div>') % "".join(rows)


def pace_line(r):
    """각질 전개도 — 출전 전체를 늘어놓는다. 추천 다섯 두만으로는 페이스를 못 읽는다."""
    buckets = {"front": [], "stalk": [], "close": []}
    picked = {h["chul_no"] for h in r["picks"]}
    for h in sorted(r["all"], key=lambda x: x["pred_rank"] or 99):
        if h["style_code"] in buckets:
            tag = "p" if h["chul_no"] in picked else ""
            buckets[h["style_code"]].append(
                '<em class="%s">%s</em>' % (tag, esc(h["chul_no"])))
    parts = [
        '<span class="pc"><b>%s</b>%s</span>' % (STYLE_LABEL[k], "".join(v) if v else "—")
        for k, v in buckets.items() if v
    ]
    return '<div class="pace">%s</div>' % "".join(parts) if parts else ""


def race_block(r):
    p = r["picks"]
    n = stars(r["conf_score"])
    top3 = sum(x["p_win"] or 0 for x in p[:3]) * 100
    cond = " · ".join(x for x in (r["age_cond"], r["budam_type"]) if x)
    kind, ptxt = pace_read(r)
    notes = "".join("<li>%s</li>" % x for x in race_note(r, kind))
    return """<article class="race" id="r-%s-%d">
 <header class="rh">
  <a class="rh-no" href="%s" title="이 경주 상세 예상"><b>%d</b><i>경주</i></a>
  <div class="rh-mid">
   <div class="rh-top">
    <span class="rh-meet mt-%s">%s</span>
    <span class="rh-time">발주 %s</span>
   </div>
   <div class="rh-dist">%d<u>m</u></div>
   <div class="rh-cond">%s%s · %d두 출전</div>
  </div>
  <div class="rh-star">
   <span class="rh-star-l">혼전도</span>
   <span class="stars" aria-label="혼전도 5점 만점에 %d점">%s<u>%s</u></span>
   <span class="rh-conf tier-%s">%s</span>
  </div>
 </header>
 <div class="rlead">
  <h3>%s</h3>
  <p>1순위 <b>%s</b> 우승확률 %s%% · 상위 3두 합산 %.1f%% · %s</p>
 </div>
 <div class="pace-box">
  <h4 class="sub-h">전개 예상 <span class="pace-kind pk-%s">%s</span></h4>
  %s
  <p class="pace-txt">%s</p>
 </div>
 <div class="scroll">
 <table class="grid picks">
  <colgroup><col class="w-mk"><col class="w-no"><col class="w-nm"><col class="w-jk"><col class="w-st"><col class="w-rd"><col class="w-lad"><col class="w-p"><col class="w-p2"></colgroup>
  <thead><tr>
   <th scope="col">기호</th><th scope="col">마번</th><th scope="col">마 명</th>
   <th scope="col">기수·조교사</th><th scope="col">각질</th>
   <th scope="col">통산 전적<span class="thn">마사회 공식</span></th><th scope="col">최근 5회 <span class="thn">우리 보유 기록 · 왼쪽이 최근</span></th>
   <th scope="col">우승확률 %%</th><th scope="col">입상 %%</th>
  </tr></thead>
  <tbody>%s</tbody>
 </table>
 </div>
 %s
 %s
 <div class="note">
  <h4 class="sub-h">경주 해설</h4>
  <ul>%s</ul>
 </div>
</article>""" % (
        esc(r["meet"]), r["rc_no"], esc(r["url"]), r["rc_no"],
        esc(r["meet"]), esc(r["meet"]),
        esc(r["post_time"]), r["distance"], esc(r["grade"]),
        (" · " + esc(cond)) if cond else "", r["field_size"],
        n, "★" * n, "★" * (5 - n), esc(r["conf_label"]), esc(r["conf_label"]),
        esc(headline(r["conf_label"], p[0])), esc(p[0]["hr_name"]),
        pct(p[0]["p_win"]), top3, esc(r["conf_desc"]),
        {"빠른 페이스": "fast", "느린 페이스": "slow"}.get(kind, "mid"), esc(kind),
        pace_line(r), esc(ptxt),
        pick_rows(r), tempo_table(r), entry_table(r), notes,
    )


# ── 기록·시속 ────────────────────────────────────────────────

def ranks(vals, low_is_good=True):
    """칸마다 1·2·3위에 번호를 단다. 전문지가 숫자 옆에 ①②③을 찍던 자리다."""
    got = [(i, v) for i, v in enumerate(vals) if v is not None]
    got.sort(key=lambda t: t[1], reverse=not low_is_good)
    return {i: pos + 1 for pos, (i, _) in enumerate(got[:3])}


def num(v, fmt="%.1f", rk=None):
    if v is None:
        return '<span class="none">—</span>'
    tag = ('<sup class="rk r%d">%s</sup>' % (rk, "①②③"[rk - 1])) if rk else ""
    return (fmt % v) + tag


def tempo_table(r):
    hs = r["picks"] + ([r["longshot"]] if r["longshot"] else [])
    hs = [h for h in hs if h["tempo"]["n"]]
    if not hs:
        return ""
    s1 = ranks([h["tempo"]["s1f"] for h in hs])
    l3 = ranks([h["tempo"]["l3f"] for h in hs])
    l1 = ranks([h["tempo"]["l1f_best"] for h in hs])
    kh = ranks([h["tempo"]["kmh"] for h in hs], low_is_good=False)
    rows = []
    for i, h in enumerate(hs):
        t = h["tempo"]
        if t["dist_best"]:
            dist = '%s<i class="dim2">%d회</i>' % (mmss(t["dist_best"]), t["dist_n"])
        else:
            dist = ('<span class="none">초출전</span>' if t["full"]
                    else '<span class="none">기록 없음</span>')
        rows.append(
            '<tr%s>\n <td class="c-no"><b>%s</b></td><td class="t-nm">%s</td>\n'
            ' <td class="t-v">%s</td><td class="t-v">%s</td><td class="t-v">%s</td>\n'
            ' <td class="t-v">%s</td><td class="t-v t-rec">%s</td>\n</tr>'
            % (' class="ls"' if h.get("longshot") else "",
               esc(h["chul_no"]), esc(h["hr_name"]),
               num(t["s1f"], "%.1f", s1.get(i)), num(t["l3f"], "%.1f", l3.get(i)),
               num(t["l1f_best"], "%.1f", l1.get(i)),
               num(t["kmh"], "%.1f", kh.get(i)), dist)
        )
    return ('<div class="tempo-wrap">\n'
            ' <h4 class="sub-h">한눈에 추리 가능한 기록·시속 정보'
            '<span class="thn2">최근 5전 평균 · ①②③은 이 경주 안에서의 순위</span></h4>\n'
            ' <div class="scroll">\n'
            ' <table class="grid tempo">\n'
            '  <colgroup><col class="w-no"><col class="w-tnm"><col><col><col><col><col class="w-trec"></colgroup>\n'
            '  <thead><tr>\n'
            '   <th scope="col">마번</th><th scope="col">마 명</th>\n'
            '   <th scope="col">초반 1F<span class="thn">출발 200m</span></th>\n'
            '   <th scope="col">후반 3F<span class="thn">마지막 600m</span></th>\n'
            '   <th scope="col">후반 1F<span class="thn">최고</span></th>\n'
            '   <th scope="col">평균 시속<span class="thn">km/h</span></th>\n'
            '   <th scope="col">이 거리 최고기록<span class="thn">우리 보유 기록</span></th>\n'
            '  </tr></thead>\n  <tbody>%s</tbody>\n </table>\n </div>\n</div>') % "".join(rows)


# ── 전개예상 · 경주해설 ───────────────────────────────────────

def pace_read(r):
    """각질 분포로 페이스를 읽는다. 선행마가 몰리면 앞이 빨라지고 뒤가 산다."""
    b = {}
    for h in r["all"]:
        b.setdefault(h["style_code"], []).append(h)
    nf, ns, nc = len(b.get("front", [])), len(b.get("stalk", [])), len(b.get("close", []))
    fs = r["field_size"] or max(1, nf + ns + nc)
    if nf >= 4 or nf / fs >= 0.36:
        kind = "빠른 페이스"
        txt = ("선행형이 %d두 몰렸다. 초반부터 자리다툼이 일어 앞이 빨라지기 쉽고, "
               "그만큼 막판에 뒤에서 들어오는 말에게 자리가 생긴다." % nf)
        fav = "close"
    elif nf <= 1:
        kind = "느린 페이스"
        txt = ("선행형이 %d두뿐이다. 앞선 말이 편하게 끌고 가면 페이스가 처지고, "
               "뒤에서 따라붙기가 그만큼 어려워진다." % nf)
        fav = "front"
    else:
        kind = "보통 페이스"
        txt = ("선행 %d두 · 선입 %d두로 자리가 고르다. 극단적인 전개는 아니고 "
               "각자 제 자리에서 겨루는 그림이다." % (nf, ns))
        fav = None
    if fav:
        who = [h for h in r["picks"] if h["style_code"] == fav]
        if who:
            txt += " 이 전개라면 득을 볼 수 있는 쪽은 추천 가운데 %s다." % " · ".join(
                "%s %s" % (h["chul_no"], h["hr_name"]) for h in who)
    return kind, txt


def form_phrase(h):
    ps = h["past"][:3]
    if not ps:
        if (h["career_starts"] or 0) > 0:
            return "지난 경주 기록이 우리 자료에 없다"
        return "출전 기록이 없는 신마다"
    o = [q["ord"] for q in ps]
    if len(o) == 1:
        return "직전 한 번 뛰어 %d착이다" % o[0]
    t = "최근 %d전 %s착" % (len(o), "·".join(str(x) for x in o))
    if all(x <= 3 for x in o):
        return t + "으로 내리 입상 중이다"
    if o[0] == 1:
        return t + "으로 직전 우승했다"
    if o[0] <= 3:
        return t + "으로 직전 입상했다"
    return t + "이다"


def dist_phrase(h, dist):
    """거리 적성.

    주의 — 거리별 기록은 **우리가 보관한 경주**에서만 뽑는다. 마사회 공식 전적이
    우리 보유분보다 많은 말(2021년 8월 이전에 뛴 말)에게 '이 거리는 처음'이라고
    쓰면 그건 해석이 아니라 허위다. 그래서 덮이지 않는 말은 말투를 바꾼다.
    """
    t = h["tempo"]
    if not t["dist_n"]:
        return ("%dm는 처음이다" % dist) if t["full"] else \
               ("%dm 기록은 우리 자료에 남아 있지 않다" % dist)
    if t["full"]:
        return "%dm는 %d회 뛰어 최고 %s" % (dist, t["dist_n"], mmss(t["dist_best"]))
    return "%dm는 우리 보유 기록 안에서 %d회 뛰어 최고 %s" % (
        dist, t["dist_n"], mmss(t["dist_best"]))


def race_note(r, pace_kind):
    """경주해설 — 우리 수치를 문장으로 옮긴 것뿐, 없는 말은 보태지 않는다."""
    p = r["picks"]
    top, sec = p[0], p[1]
    gap = ((top["p_win"] or 0) - (sec["p_win"] or 0)) * 100
    L = []
    tn, sn = top["hr_name"] or "", sec["hr_name"] or ""
    if gap >= 12:
        L.append("<b>%s %s</b>%s 2순위 %s%s %.1f%%p 앞선다. 이 경주의 중심은 분명하다."
                 % (esc(top["chul_no"]), esc(tn), iga(tn), esc(sn), eul(sn), gap))
    elif gap >= 6:
        L.append("<b>%s %s</b>%s 앞서지만 2순위 %s%s의 차이는 %.1f%%p다. "
                 "축으로 삼되 상대는 넓게 본다."
                 % (esc(top["chul_no"]), esc(tn), iga(tn), esc(sn), wa(sn), gap))
    else:
        L.append("1순위 <b>%s</b>%s 2순위 <b>%s</b>의 차이가 %.1f%%p에 지나지 않는다. "
                 "둘을 함께 놓고 봐야 하는 경주다." % (esc(tn), wa(tn), esc(sn), gap))

    L.append("%s%s %s. %s." % (esc(tn), un(tn), form_phrase(top),
                               dist_phrase(top, r["distance"])))
    t3 = p[2]
    n3 = t3["hr_name"] or ""
    L.append("뒤를 쫓는 %s %s%s %s." % (esc(t3["chul_no"]), esc(n3), un(n3), form_phrase(t3)))

    ls = r["longshot"]
    if ls:
        ln = ls["hr_name"] or ""
        L.append("다섯 두 밖에서는 <b>%s %s</b>%s 복병으로 짚는다. 예상 %s위지만 3착 이내 "
                 "가능성만 따로 셈한 결과 이 경주에서 가장 높게 나왔다."
                 % (esc(ls["chul_no"]), esc(ln), eul(ln), esc(ls["pred_rank"])))

    n = stars(r["conf_score"])
    if n <= 2:
        L.append("혼전도 두 별. 오늘 지면에서 우리가 가장 자신 있는 축에 드는 경주다.")
    elif n >= 5:
        L.append("혼전도 다섯 별. %s가 겹쳐 우리도 가려내지 못했으니 넓게 잡으시기 바란다."
                 % pace_kind)
    return L


def day_meta(races: List[Dict], day: str) -> Dict:
    """지면 머리와 제목에 쓸 집계. 숫자는 전부 같은 races 에서 나온다."""
    d = dt.date.fromisoformat(day)
    by_meet: Dict[str, int] = defaultdict(int)
    for r in races:
        by_meet[r["meet"]] += 1
    return {
        "day": day,
        "date_long": "%d년 %d월 %d일 (%s)" % (
            d.year, d.month, d.day, WEEKDAY_KO[d.weekday()]),
        "date_short": "%d·%d" % (d.month, d.day),
        "date_sentence": "%d년 %d월 %d일 %s요일" % (
            d.year, d.month, d.day, WEEKDAY_KO[d.weekday()]),
        "meet_txt": " · ".join("%s %d경주" % (m, n) for m, n in by_meet.items()),
        "total": len(races),
        "n_strong": sum(1 for r in races if r["conf_label"] == "강승부"),
        "n_longshot": sum(1 for r in races if r["longshot"]),
        "first_post": races[0]["post_time"],
        "last_post": races[-1]["post_time"],
    }


def ad_after(i: int, total: int) -> bool:
    """i 번째 경주 블록 **뒤에** 광고를 넣을 자리인가 (0부터).

    경주 블록 *안*에는 절대 넣지 않는다. 전개예상과 예상표 사이, 표와 해설
    사이는 하나의 추론 단위라 거기서 끊으면 문장 중간에 끼어든 것으로 읽힌다.
    항상 경주와 경주 사이 — 지면이 원래 쉬는 자리에만 둔다.
    """
    if i >= total - 1:            # 마지막 블록 뒤는 판권장 위 유닛이 맡는다
        return False
    if AD_SKIP_FIRST and i == 0:
        return False
    return (i + 1) % AD_EVERY == 0


def pick_day(all_days: List[str], today: str) -> List[str]:
    """하루만 내는 모드에서 어느 날짜를 낼지 고른다.

    그냥 오늘로 두면 **경마가 없는 날 지면이 통째로 사라진다.** 경마는 목~일만
    열리므로 월·화·수에 빌드가 돌면 /paper/ 가 없어지고, 공유된 주소가 404 가
    된다. 지면은 커뮤니티에 주소로 던지는 물건이라 그게 제일 아픈 고장이다.

    오늘 경주가 있으면 오늘, 없으면 **다가올** 가장 가까운 경주일, 그것도
    없으면 가장 최근 경주일. 돌아갈 날이 아예 없으면 빈 목록이다.
    """
    if not all_days:
        return []
    if today in all_days:
        return [today]
    return [next((d for d in all_days if d > today), all_days[-1])]


def build_paper_pages(env, out_dir: Path, ctx_base: Dict, conn,
                      days: List[str], today: str) -> List[str]:
    """지면을 굽고 사이트맵에 넣을 주소를 돌려준다.

    /paper/            가장 볼 만한 경주일 — 공유용 고정 주소
    /paper/<날짜>/     영구 보존

    예상이 남아 있는 날이면 **전부** 굽는다. 지난 지면도 그날 낸 그대로다
    (게재한 예상은 고치지 않는다). 날짜별로 쌓아야 경주일마다 고유 주소가
    늘어난다 — 한 장을 매일 덮어쓰면 색인되는 주소가 영원히 하나다.
    """
    # site 가 paper 를 불러오므로 모듈 수준에서 거꾸로 불러오면 순환이 된다.
    # 호출 시점에는 site 가 이미 올라와 있다.
    from .site import race_slug, write

    tpl = env.get_template("paper.html")
    urls: List[str] = []
    built: Dict[str, Dict] = {}
    for day in days:
        races = load_day(conn, day)
        if not races:
            continue
        for r in races:
            r["url"] = "/race/%s/" % race_slug(r["race_key"])
        meta = day_meta(races, day)
        page = {"paper": meta, "summary": summary_table(races),
                "blocks": [{"html": race_block(r), "ad": ad_after(i, len(races))}
                           for i, r in enumerate(races)]}
        url = "/paper/%s/" % day
        write(out_dir / "paper" / day / "index.html",
              tpl.render(**ctx_base, page_url=url, canonical_url=url, **page))
        urls.append(url)
        built[day] = page

    if not built:
        return urls

    # /paper/ 가 가리킬 날.
    #
    # 오늘 경주가 있으면 오늘, 없으면 **다가올** 가장 가까운 경주일, 그것도
    # 없으면 가장 최근 경주일. 공유된 링크가 지난 지면을 가리키고 있으면
    # 안 되므로 매 빌드마다 다시 찍는다.
    ds = sorted(built)
    front = (today if today in built
             else next((d for d in ds if d > today), ds[-1]))
    # canonical 은 날짜 주소로 보낸다 — 같은 내용이 두 주소에 있으면 색인이
    # 갈려 둘 다 약해진다. /paper/ 는 사람이 공유하는 입구로만 쓴다.
    write(out_dir / "paper" / "index.html",
          tpl.render(**ctx_base, page_url="/paper/",
                     canonical_url="/paper/%s/" % front, **built[front]))
    return urls
