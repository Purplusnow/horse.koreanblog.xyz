# -*- coding: utf-8 -*-
"""앱 아이콘 품질 검사 — 작게 줄였을 때 알아볼 수 있는가.

    .venv/bin/python tools/check_icon.py static/icon-512.png [...]

**아이콘은 1024 로 보면 다 괜찮아 보인다.** 실제로 쓰이는 크기는 런처에서
48dp 남짓이다. 그래서 작게 줄여 놓고 네 가지를 잰다.

  1) 잉크 비율  — 바탕 말고 '그림'이 차지하는 면적. 너무 적으면 허전하고
                  너무 많으면 런처에서 뭉갠다.
  2) 명암비     — 주제와 바탕의 WCAG 명암비. 낮으면 작게 줄일 때 사라진다.
  3) 테두리 여백 — 바깥 띠에 그림이 닿는지. 닿으면 원형으로 깎일 때 잘린다.
  4) 디테일 생존 — 48px 로 줄였다 되키웠을 때 원본과 얼마나 달라지는가.
                  많이 달라질수록 '작으면 뭉개지는' 그림이다.

판정선은 눈대중이 아니라, 통과/탈락이 뒤집히는 지점을 여러 장으로 확인해
잡았다. 숫자는 비교용이지 절대 기준이 아니다 — 마지막엔 눈으로 본다.
"""
import sys, pathlib
from PIL import Image, ImageFilter


def luminance(c):
    f = lambda v: (v / 255) / 12.92 if v / 255 <= 0.03928 else (((v / 255) + 0.055) / 1.055) ** 2.4
    r, g, b = (f(x) for x in c[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    x, y = sorted((luminance(a), luminance(b)), reverse=True)
    return (x + 0.05) / (y + 0.05)


def analyse(path):
    im = Image.open(path).convert("RGB")
    im = im.resize((256, 256), Image.LANCZOS)
    px = im.load()
    W = H = 256

    # 바탕색 = 모서리 네 곳의 평균. 아이콘은 가장자리가 바탕이다.
    corners = [px[2, 2], px[W - 3, 2], px[2, H - 3], px[W - 3, H - 3]]
    bg = tuple(sum(c[i] for c in corners) // 4 for i in range(3))

    # 바탕과 충분히 다른 화소 = 그림
    def is_ink(c):
        return abs(c[0] - bg[0]) + abs(c[1] - bg[1]) + abs(c[2] - bg[2]) > 60

    ink = [(x, y) for y in range(H) for x in range(W) if is_ink(px[x, y])]
    ratio = len(ink) / (W * H)

    # 주제색 = 그림 화소의 평균
    if ink:
        fg = tuple(sum(px[x, y][i] for x, y in ink) // len(ink) for i in range(3))
    else:
        fg = bg

    # 바깥 띠(한 변의 열두 분의 일)에 그림이 닿는가 — 원형 크롭에서 잘린다
    m = W // 12
    edge = sum(1 for x, y in ink if x < m or y < m or x >= W - m or y >= H - m)

    # **마스커블은 다른 자로 재야 한다.** 런처가 바깥을 깎으므로 그림을 일부러
    # 작게 넣는데, 일반 아이콘과 같은 잉크 기준을 들이대면 '허전하다'고 틀리게
    # 걸린다(실제로 그렇게 걸렸다). 마스커블의 무대는 사각형이 아니라
    # 안전영역(지름이 한 변의 열의 여덟, 가운데 원)이다. 그 원을 분모로 삼고,
    # 그림이 그 원 밖으로 나가지 않는지를 함께 본다.
    maskable = "maskable" in pathlib.Path(path).name
    if maskable and ink:
        import math
        R = 0.4 * W
        cx = cy = W / 2
        ratio = len(ink) / (math.pi * R * R)
        outside = sum(1 for x, y in ink
                      if (x - cx) ** 2 + (y - cy) ** 2 > R * R)
    else:
        outside = 0

    # 작게 줄였다 되키운 뒤 얼마나 달라지는가
    small = im.resize((48, 48), Image.LANCZOS).resize((256, 256), Image.NEAREST)
    sp = small.load()
    diff = sum(abs(px[x, y][0] - sp[x, y][0]) for y in range(0, H, 2) for x in range(0, W, 2))
    diff /= (W // 2) * (H // 2) * 255

    return {"bg": bg, "fg": fg, "ink": ratio, "contrast": contrast(fg, bg),
            "edge": edge, "blur": diff, "maskable": maskable, "outside": outside}


def main():
    paths = sys.argv[1:]
    if not paths:
        sys.exit(__doc__)
    print(f"{'파일':<34}{'잉크':>7}{'명암비':>8}{'테두리':>8}{'뭉갬':>8}   판정")
    worst = 0
    for p in paths:
        r = analyse(p)
        bad = []
        if not (0.10 <= r["ink"] <= 0.55): bad.append("잉크")
        if r["contrast"] < 3.0: bad.append("명암비")
        if r["blur"] > 0.06: bad.append("뭉갬")
        if r["maskable"]:
            # 마스커블은 사각 테두리가 아니라 안전영역 밖으로 나갔는지를 본다.
            if r["outside"] > 0: bad.append("안전영역밖")
        elif r["edge"] > 0:
            bad.append("테두리")
        worst = max(worst, len(bad))
        print(f"{pathlib.Path(p).name:<34}{r['ink']:>6.1%}{r['contrast']:>8.1f}"
              f"{(r['outside'] if r['maskable'] else r['edge']):>8}{r['blur']:>8.3f}   "
              + ("좋음" if not bad else "걸림: " + "·".join(bad)))
    print("\n잉크 10~55% · 명암비 3 이상 · 테두리(마스커블은 안전영역밖) 0 · 뭉갬 0.06 이하")
    print("마스커블의 잉크는 사각형이 아니라 안전영역 원을 분모로 잽니다.")


if __name__ == "__main__":
    main()
