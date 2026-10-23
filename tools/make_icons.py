# -*- coding: utf-8 -*-
"""PWA·스토어 아이콘 생성 — 원본 아트에서 말만 떼어 정확히 가운데 앉힌다.

    .venv/bin/python tools/make_icons.py      (Pillow 필요 — 로컬 전용 도구)

산출물은 저장소에 커밋되므로 CI 는 이 스크립트를 돌리지 않는다.
원본 아트(tools/icon_src/)도 함께 둔다 — 색이나 크기를 바꿔 다시 뽑으려면
그 원본이 있어야 한다.

**예전에는 🐎 이모지를 그대로 썼다.** 갈색 말이 딥그린 위에 올라가 명암비가
1.3 이었고(기준 3.0), 런처 크기로 줄면 그냥 초록 사각형으로 보였다.
지금은 흰 말 실루엣 + 산뜻한 그린이라 3.1 이다. tools/check_icon.py 가 잰다.

**마스커블은 따로 만든다.** 안드로이드 런처는 모양(원·사각·물방울)에 맞춰
바깥을 깎으므로 안쪽만 안전하다. 일반 아이콘을 마스커블로 신고하면 다리가
잘린다. 그래서 여백을 더 준 판을 따로 낸다.
"""
import pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "tools/icon_src/horse_white_1024.png"
STATIC = ROOT / "static"

# 바탕색. 산뜻하되 흰 말과의 명암비가 기준(3.0)을 넘는 선에서 고른 값이다 —
# 한 단 더 밝히면(#2bb673) 2.5 로 떨어져 작은 크기에서 말이 묻힌다.
BG = (34, 160, 107)          # #22a06b

# (파일명, 변 길이, 말이 차지할 가로 비율)
ICONS = [
    ("icon-192.png",          192, 0.80),
    ("icon-512.png",          512, 0.80),
    ("icon-maskable-512.png", 512, 0.64),   # 안전영역(가운데 원) 안에 들어가는 최대치 부근
]


def main():
    try:
        from PIL import Image
        import numpy as np
    except ImportError:
        sys.exit("Pillow 와 numpy 가 필요합니다: .venv/bin/pip install Pillow numpy")

    if not SRC.exists():
        sys.exit(f"원본 아트가 없습니다: {SRC}")

    src = Image.open(SRC).convert("RGB")
    a = np.asarray(src).astype(int)
    # 흰 말만 고른다. **세 채널을 다 봐야 한다** — 바탕 초록은 G 가 206 이라
    # 밝기 하나만 보면 바탕까지 '흰색'으로 걸린다(실제로 그렇게 틀렸다).
    white = a.min(axis=2) > 170
    ys, xs = np.where(white)
    if not len(xs):
        sys.exit("원본에서 흰 말을 찾지 못했습니다.")
    box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    mask = Image.fromarray((white[box[1]:box[3], box[0]:box[2]] * 255).astype("uint8"))
    mw, mh = mask.size
    print(f"  말 경계 {box} → {mw}x{mh}")

    for name, size, ratio in ICONS:
        tw = round(size * ratio)
        th = round(tw * mh / mw)
        m = mask.resize((tw, th), Image.LANCZOS)
        canvas = Image.new("RGB", (size, size), BG)
        # 가운데는 눈대중이 아니라 경계에서 계산한다.
        canvas.paste(Image.new("RGB", (tw, th), (255, 255, 255)),
                     ((size - tw) // 2, (size - th) // 2), m)
        canvas.save(STATIC / name, optimize=True)
        print(f"  {name}  {size}x{size}  말 {tw}x{th}")

    print(f"\n{len(ICONS)}개 생성 → {STATIC}")
    print("검사: .venv/bin/python tools/check_icon.py static/icon-*.png")


if __name__ == "__main__":
    main()
