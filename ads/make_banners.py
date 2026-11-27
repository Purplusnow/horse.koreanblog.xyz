# -*- coding: utf-8 -*-
"""구글애즈 디스플레이 배너 생성 — HTML 을 Chrome 헤드리스로 렌더한다.

    python3 ads/make_banners.py

**숫자는 전부 ads/numbers.json 에서 읽는다.** 광고에 적힌 수치는 방문자가 랜딩에서
그대로 확인할 수 있어야 하므로, 주말 집계로 사이트 숫자가 바뀌면 numbers.json 만
고치고 이 스크립트를 다시 돌린다. 배너 안에 숫자를 박아 두면 사이트와 조용히
어긋나고, 어긋난 줄은 아무도 알려 주지 않는다.

팔레트는 사이트 토큰(static/style.css 의 --accent #1a6b4a, --gold #b4842a)에서 왔다.
"""
import json, subprocess, pathlib

HERE = pathlib.Path(__file__).resolve().parent
N = json.loads((HERE / "numbers.json").read_text(encoding="utf-8"))
OUT = HERE
TMP = pathlib.Path("/tmp/horseai_banner_html"); TMP.mkdir(exist_ok=True)
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

BASE = """<!doctype html><html lang="ko"><meta charset="utf-8"><style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:%(W)spx;height:%(H)spx;overflow:hidden}
body{font-family:"Apple SD Gothic Neo","Noto Sans KR",sans-serif;
 background:linear-gradient(145deg,#12513a 0%%,#1a6b4a 55%%,#155f42 100%%);
 color:#fff;display:flex;flex-direction:column;justify-content:center;
 padding:%(PAD)spx;position:relative;-webkit-font-smoothing:antialiased}
body::after{content:"";position:absolute;inset:0;
 background:radial-gradient(circle at 88%% 12%%,rgba(212,168,74,.22),transparent 55%%)}
.wrap{position:relative;z-index:1;display:flex;flex-direction:column;height:100%%;
 justify-content:space-between}
.brand{display:flex;align-items:center;gap:%(S10)spx;font-weight:800;
 font-size:%(FBRAND)spx;letter-spacing:-.02em;opacity:.95}
.brand i{font-style:normal;font-size:%(FBRANDI)spx}
.kick{display:inline-block;align-self:flex-start;font-size:%(FKICK)spx;font-weight:800;
 letter-spacing:.14em;color:#f2d79a;border:%(BW)spx solid rgba(242,215,154,.55);
 border-radius:999px;padding:%(KPY)spx %(KPX)spx;margin-bottom:%(S14)spx}
.big{font-size:%(FBIG)spx;font-weight:900;line-height:.92;letter-spacing:-.045em;
 font-variant-numeric:tabular-nums;text-shadow:0 %(SH)spx %(SH2)spx rgba(0,0,0,.18)}
.big u{text-decoration:none;font-size:%(FBIGU)spx;color:#f2d79a;margin-left:%(S4)spx}
.sub{font-size:%(FSUB)spx;font-weight:700;line-height:1.35;margin-top:%(S12)spx}
.sub b{color:#f2d79a}
.rows{margin-top:%(S16)spx;display:flex;flex-direction:column;gap:%(S6)spx}
.row{display:flex;align-items:center;gap:%(S8)spx;font-size:%(FROW)spx;font-weight:700;
 background:rgba(255,255,255,.10);border-radius:%(S8)spx;padding:%(S6)spx %(S10)spx}
.row s{text-decoration:none;color:#f2d79a;font-weight:900;width:%(MARKW)spx}
.row em{font-style:normal;opacity:.75;font-weight:600;font-size:%(FROWS)spx}
.row span{margin-left:auto;background:#f2d79a;color:#123d2b;font-weight:900;
 border-radius:999px;padding:%(S2)spx %(S8)spx;font-size:%(FROWS)spx}
.chips{display:flex;gap:%(S8)spx;margin-top:%(S14)spx;flex-wrap:wrap}
.chip{background:rgba(255,255,255,.12);border-radius:%(S8)spx;padding:%(S8)spx %(S10)spx;
 font-size:%(FCHIP)spx;font-weight:700;line-height:1.3}
.chip b{display:block;font-size:%(FCHIPB)spx;font-weight:900;color:#f2d79a;
 font-variant-numeric:tabular-nums}
.foot{display:flex;align-items:center;justify-content:space-between;gap:%(S10)spx;
 margin-top:%(S14)spx}
.cta{background:#f2d79a;color:#123d2b;font-weight:900;font-size:%(FCTA)spx;
 border-radius:999px;padding:%(CPY)spx %(CPX)spx;white-space:nowrap}
.url{font-size:%(FURL)spx;font-weight:700;opacity:.85;letter-spacing:-.01em}
.fine{font-size:%(FFINE)spx;opacity:.62;line-height:1.35;margin-top:%(S8)spx}
%(EXTRA)s</style><body><div class="wrap">%(BODY)s</div></body></html>"""

# 치수는 가로형(1200x628)으로 잡고 사이즈별로 배율만 바꾼다.
SIZES = {
 "1200x628":  dict(W=1200, H=628,  k=0.94, PAD=48),
 "1200x1200": dict(W=1200, H=1200, k=1.52, PAD=72),
 "1200x300":  dict(W=1200, H=300,  k=0.62, PAD=30,
                   extra=".wrap{justify-content:center;gap:14px}"
                         ".brand{font-size:22px}.brand i{font-size:26px}"),
}

def dims(k, W, H, PAD):
    s = lambda v: max(1, round(v * k))
    return dict(W=W, H=H, PAD=PAD,
        FBRAND=s(26), FBRANDI=s(30), FKICK=s(19), BW=s(2), KPY=s(7), KPX=s(18),
        FBIG=s(150), FBIGU=s(58), FSUB=s(29), FROW=s(25), FROWS=s(20),
        FCHIP=s(20), FCHIPB=s(42), FCTA=s(26), FURL=s(22), FFINE=s(15),
        MARKW=s(26), SH=s(3), SH2=s(10), CPY=s(13), CPX=s(26),
        S2=s(3), S4=s(5), S6=s(8), S8=s(11), S10=s(13), S12=s(15), S14=s(18),
        S16=s(20))

BRAND = '<div class="brand"><i>🐎</i> 머신러닝 경마연구소</div>'
TOP, ALL, STR = N["최고배당"], N["전체"], N["강승부"]

def foot(cta="무료로 보기"):
    return (f'<div class="foot"><div class="cta">{cta}</div>'
            '<div class="url">horse.koreanblog.xyz</div></div>')

def fine(txt):
    return f'<div class="fine">{txt}</div>'

# 표본을 섞지 않는다 — 강승부 수치 옆에 전체 경주 수를 적으면 표본을 오인한다.
FINE_A = fine("역대 최고 기록입니다. 적중을 보장하지 않습니다 · 만 19세 이상 · 예상은 참고 정보")
FINE_B = fine(f"강승부 {STR['검증경주']}경주 기준 · 전체 {ALL['검증경주']}경주 성적은 사이트에 공개 · "
              "적중을 보장하지 않습니다 · 만 19세 이상")
FINE_C = fine(f"{N['기간']} 게재분 {ALL['검증경주']}경주 집계 · 적중을 보장하지 않습니다 · 만 19세 이상")

HORSE_ROWS = "".join(
    f'<div class="row"><s>{h["기호"]}</s>{h["번호"]} {h["이름"]} '
    f'<em>단승 {h["단승"]}배</em><span>{h["착순"]}</span></div>' for h in TOP["말"])

CONCEPTS = {
 "a-payout": BRAND + f"""
 <div>
   <div class="kick">역대 고배당 적중</div>
   <div class="big">{TOP['배당']}<u>배</u></div>
   <div class="sub">{TOP['승식']} · {TOP['경주']} <b>1·2·3착 순서까지</b></div>
   <div class="rows">{HORSE_ROWS}</div>
 </div>""" + foot() + FINE_A,

 "b-hitrate": BRAND + f"""
 <div>
   <div class="kick">게재한 예상 그대로 검증</div>
   <div class="big">{STR['1순위_1착']}<u>%</u></div>
   <div class="sub">신뢰도 <b>강승부</b> 경주의 1순위 1착률</div>
   <div class="chips">
     <div class="chip"><b>{STR['1순위_3착이내']}%</b>1순위 3착 이내</div>
     <div class="chip"><b>{STR['상위3두중_우승마']}%</b>상위 3두 중 우승마</div>
     <div class="chip"><b>{STR['검증경주']}</b>강승부 검증 경주</div>
   </div>
 </div>""" + foot("적중률 보기") + FINE_B,

 "c-trust": BRAND + f"""
 <div>
   <div class="kick">발주 전 공개 · 사후 수정 없음</div>
   <div class="big" style="font-size:%(FTITLE)s">맞힌 것도<br>틀린 것도<br>전부 그대로</div>
   <div class="sub">{ALL['검증경주']}경주 성적을 승식별로 <b>전부 공개</b>합니다</div>
   <div class="chips">
     <div class="chip"><b>{TOP['배당']}배</b>역대 최고 적중</div>
     <div class="chip"><b>{N['학습경주']}</b>학습 경주</div>
     <div class="chip"><b>{N['지표수']}</b>산출 지표</div>
   </div>
 </div>""" + foot("전체 결과 보기") + FINE_C,
}

# 1200x300 은 세로가 좁아 표·칩이 들어가지 않는다 — 한 줄 구성으로 따로 둔다.
WIDE_ONLY = {
 "a-payout": BRAND + f"""
 <div class="foot" style="margin-top:0">
   <div><div class="big" style="font-size:%(FBIGW)s">{TOP['배당']}<u>배</u></div></div>
   <div class="sub" style="margin:0;flex:1;padding-left:%(S16)s">
     {TOP['승식']} 적중 · {TOP['경주']}<br><b>발주 전 올린 추천 5두 그대로</b></div>
   <div class="cta">무료로 보기</div>
 </div>""",
 "b-hitrate": BRAND + f"""
 <div class="foot" style="margin-top:0">
   <div><div class="big" style="font-size:%(FBIGW)s">{STR['1순위_1착']}<u>%</u></div></div>
   <div class="sub" style="margin:0;flex:1;padding-left:%(S16)s">
     강승부 {STR['검증경주']}경주 1순위 1착률<br><b>적중·실패 전부 공개</b></div>
   <div class="cta">적중률 보기</div>
 </div>""",
 "c-trust": BRAND + f"""
 <div class="foot" style="margin-top:0">
   <div><div class="big" style="font-size:%(FBIGW)s">{ALL['검증경주']}<u>경주</u></div></div>
   <div class="sub" style="margin:0;flex:1;padding-left:%(S16)s">
     적중도 실패도 전부 공개<br><b>발주 전에 올리고 고치지 않습니다</b></div>
   <div class="cta">전체 결과 보기</div>
 </div>""",
}

LOGO = """<!doctype html><html lang="ko"><meta charset="utf-8"><style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:%(W)spx;height:%(H)spx;overflow:hidden}
body{font-family:"Apple SD Gothic Neo",sans-serif;background:#1a6b4a;color:#fff;
 display:flex;align-items:center;justify-content:center;gap:%(G)spx;
 flex-direction:%(DIR)s;-webkit-font-smoothing:antialiased}
.m{font-size:%(FM)spx;line-height:1}
.t{font-size:%(FT)spx;font-weight:900;letter-spacing:-.03em;text-align:center;
 line-height:1.15}
.t u{display:block;text-decoration:none;font-size:%(FU)spx;font-weight:700;
 color:#f2d79a;letter-spacing:.06em;margin-top:%(MT)spx}
</style><body><div class="m">🐎</div>
<div class="t">머신러닝<br>경마연구소<u>HORSE.KOREANBLOG.XYZ</u></div></body></html>"""


def render(html, out, W, H):
    f = TMP / (out.stem + ".html")
    f.write_text(html, encoding="utf-8")
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--force-device-scale-factor=1", "--virtual-time-budget=1500",
                    f"--window-size={W},{H}", f"--screenshot={out}", f.as_uri()],
                   check=True, capture_output=True)
    print(f"  {out.name}")


def main():
    made = []
    for name, body in CONCEPTS.items():
        for size, cfg in SIZES.items():
            d = dims(cfg["k"], cfg["W"], cfg["H"], cfg["PAD"])
            d["EXTRA"] = cfg.get("extra", "")
            b = WIDE_ONLY[name] if size == "1200x300" else body
            b = (b.replace("%(FTITLE)s", f'{d["FBIG"] * 0.46:.0f}px')
                  .replace("%(FBIGW)s", f'{d["FBIG"] * 1.15:.0f}px')
                  .replace("%(S16)s", f'{d["S16"]}px'))
            out = OUT / f"{name}_{size}.png"
            render(BASE % {**d, "BODY": b}, out, cfg["W"], cfg["H"])
            made.append(out)
    for size, (W, H, DIR, FM, FT, FU, G, MT) in {
            "1200x1200": (1200, 1200, "column", 300, 128, 46, 40, 24),
            "1200x300":  (1200, 300,  "row",    150, 66,  24, 36, 10)}.items():
        out = OUT / f"logo_{size}.png"
        render(LOGO % dict(W=W, H=H, DIR=DIR, FM=FM, FT=FT, FU=FU, G=G, MT=MT), out, W, H)
        made.append(out)
    print(f"\n{len(made)}개 생성 → {OUT}  (숫자 출처: numbers.json, 확인일 {N['확인일']})")


if __name__ == "__main__":
    main()
