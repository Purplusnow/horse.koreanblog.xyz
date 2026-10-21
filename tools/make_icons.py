# -*- coding: utf-8 -*-
"""PWA 홈 화면 아이콘 생성 — 🐎 글리프를 Chrome 으로 렌더해 정확히 가운데 둔다.

    .venv/bin/python tools/make_icons.py      (Pillow 필요 — 로컬 전용 도구)

산출물은 저장소에 커밋되므로 CI 는 이 스크립트를 돌리지 않는다.

**글리프는 글자 상자 가운데에 있지 않다.** flex 로 가운데 정렬만 하면 말이
위로 떠서 아이콘이 미묘하게 비뚤어진다. 그래서 큰 판에 한 번 그린 뒤 **실제
픽셀 경계를 재서** 다시 앉힌다 — 눈대중 상수를 두지 않는다.

**마스커블은 따로 만든다.** 안드로이드 런처는 모양(원·사각·물방울)에 맞춰
아이콘 바깥을 깎아내므로 안쪽 80%만 안전하다. 일반 아이콘을 마스커블로
신고하면 말이 잘린다.

색은 사이트 토큰(static/style.css 의 --accent)과 같은 값이다.
"""
import pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
TMP = pathlib.Path("/tmp/horseai_icon_html"); TMP.mkdir(exist_ok=True)
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

BG = "#1a6b4a"
SRC = 1024          # 글리프를 재기 위한 원본 판. 클수록 경계가 정확하다.

# (파일명, 변 길이, 말이 차지할 비율). 마스커블은 안전영역 안에 들어가야 한다.
ICONS = [("icon-192.png", 192, 0.70),
         ("icon-512.png", 512, 0.70),
         ("icon-maskable-512.png", 512, 0.46)]

TPL = """<!doctype html><html><meta charset="utf-8"><style>
*{{margin:0;padding:0}}
html,body{{width:{s}px;height:{s}px;overflow:hidden}}
body{{background:{bg};display:flex;align-items:center;justify-content:center}}
.m{{font-size:{f}px;line-height:1}}
</style><body><div class="m">🐎</div></body></html>"""


def shoot(html: str, out: pathlib.Path, size: int) -> None:
    f = TMP / (out.stem + ".html")
    f.write_text(html, encoding="utf-8")
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--force-device-scale-factor=1", "--virtual-time-budget=1200",
                    f"--window-size={size},{size}", f"--screenshot={out}", f.as_uri()],
                   check=True, capture_output=True)


def main() -> None:
    try:
        from PIL import Image, ImageChops
    except ImportError:
        sys.exit("Pillow 가 필요합니다: .venv/bin/pip install Pillow")

    # 1) 큰 판에 한 번 그리고 말의 실제 경계를 잰다.
    src = TMP / "source.png"
    shoot(TPL.format(s=SRC, f=round(SRC * 0.72), bg=BG), src, SRC)
    im = Image.open(src).convert("RGB")
    box = ImageChops.difference(im, Image.new("RGB", im.size, BG)).getbbox()
    if not box:
        sys.exit("말 글리프를 찾지 못했습니다 — 배경만 렌더됐습니다.")
    horse = im.crop(box)
    print(f"  글리프 경계 {box} → {horse.width}x{horse.height}")

    # 2) 크기별로 비율을 맞춰 정확히 가운데 앉힌다.
    for name, size, ratio in ICONS:
        target = round(size * ratio)
        scale = target / max(horse.width, horse.height)
        w, h = max(1, round(horse.width * scale)), max(1, round(horse.height * scale))
        canvas = Image.new("RGB", (size, size), BG)
        canvas.paste(horse.resize((w, h), Image.LANCZOS),
                     ((size - w) // 2, (size - h) // 2))
        canvas.save(STATIC / name, optimize=True)
        print(f"  {name}  {size}x{size}  말 {w}x{h}")
    print(f"\n{len(ICONS)}개 생성 → {STATIC}")


if __name__ == "__main__":
    main()
