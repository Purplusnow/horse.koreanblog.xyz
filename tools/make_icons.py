# -*- coding: utf-8 -*-
"""PWA·스토어 아이콘 생성 — 원본 아트에서 캐릭터만 떼어 가운데 앉힌다.

    .venv/bin/python tools/make_icons.py
    .venv/bin/python tools/check_icon.py static/icon-*.png     (검사)

Pillow·numpy 가 필요한 **로컬 전용 도구**다. 산출물은 커밋되므로 CI 는 돌리지 않는다.
원본 아트도 tools/icon_src/ 에 함께 둔다 — 없으면 다음 사람이 처음부터 다시 만든다.

**바탕을 다시 깐다.** 생성된 그림은 피사체가 가장자리까지 차 있어서, 그대로
쓰면 안드로이드 런처가 원형으로 깎을 때 귀와 다리가 잘린다. 그래서 캐릭터만
떼어내 여백을 준 새 바탕에 앉힌다.

**분리는 초록 색조로 한다.** 고리별 중앙값으로 바탕을 추정하는 방법을 먼저
썼는데, 피사체가 큰 고리에서는 중앙값이 피사체 색이 되어 바탕 일부가 피사체로
딸려 왔다 — 결과물에 연두색 후광이 남았다. 바탕이 초록 한 가지뿐이므로
'G 가 R·B 보다 뚜렷이 큰가'로 가르는 편이 정확하다.
"""
import pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "tools/icon_src/horse_casual_1024.png"
STATIC = ROOT / "static"

# 바탕 그라데이션(가운데 밝고 바깥 짙은 초록). 사이트 딥그린과 같은 계열이되
# 캐주얼한 느낌이 나도록 한 단 밝다.
# 값은 눈대중이 아니라 재서 골랐다. 한 단 더 밝으면(64,196,120) 명암비가
# 2.7 로 떨어져 기준(3.0) 아래다 — 캐릭터가 밤색·크림이라 바탕이 밝으면
# 작은 크기에서 서로 묻힌다. 이 값이 '산뜻함을 가장 덜 깎으면서 넘는' 선이다.
BG_INNER, BG_OUTER = (52, 170, 104), (18, 100, 60)

# (파일명, 변 길이, 캐릭터가 차지할 비율)
#   마스커블은 런처가 바깥을 깎으므로 안전영역(가운데 원) 안에 들어가야 한다.
ICONS = [("icon-192.png", 192, 0.76),
         ("icon-512.png", 512, 0.76),
         ("icon-maskable-512.png", 512, 0.58)]


def main():
    try:
        from PIL import Image, ImageDraw, ImageFilter
        import numpy as np
    except ImportError:
        sys.exit("Pillow 와 numpy 가 필요합니다: .venv/bin/pip install Pillow numpy")
    if not SRC.exists():
        sys.exit(f"원본 아트가 없습니다: {SRC}")

    img = Image.open(SRC).convert("RGB")
    a = np.asarray(img).astype(int)
    # 바탕(초록)과 피사체(밤색 몸·크림 갈기·청록 안장)를 색조로 가른다.
    greenness = a[:, :, 1] - np.maximum(a[:, :, 0], a[:, :, 2])
    mask = Image.fromarray(((greenness < 30) * 255).astype("uint8"))
    mask = (mask.filter(ImageFilter.MedianFilter(7))          # 점잡음
                .filter(ImageFilter.MaxFilter(5))             # 구멍 메우기
                .filter(ImageFilter.MinFilter(7))             # 후광 깎기
                .filter(ImageFilter.GaussianBlur(1.0)))       # 경계 부드럽게
    box = mask.getbbox()
    if not box:
        sys.exit("원본에서 캐릭터를 찾지 못했습니다.")
    subj, sm = img.crop(box), mask.crop(box)
    print(f"  캐릭터 경계 {box} → {subj.width}x{subj.height}")

    def radial(size):
        g = Image.new("RGB", (size, size), BG_OUTER)
        d = ImageDraw.Draw(g)
        for i in range(size // 2, 0, -1):
            t = i / (size // 2)
            d.ellipse((size // 2 - i,) * 2 + (size // 2 + i,) * 2,
                      fill=tuple(round(BG_OUTER[c] * t + BG_INNER[c] * (1 - t))
                                 for c in range(3)))
        return g.filter(ImageFilter.GaussianBlur(size / 70))

    for name, size, ratio in ICONS:
        tw = round(size * ratio)
        th = round(tw * subj.height / subj.width)
        if th > size * ratio:                 # 세로가 더 길면 세로를 기준으로 맞춘다
            th = round(size * ratio)
            tw = round(th * subj.width / subj.height)
        canvas = radial(size)
        canvas.paste(subj.resize((tw, th), Image.LANCZOS),
                     ((size - tw) // 2, (size - th) // 2),
                     sm.resize((tw, th), Image.LANCZOS))
        canvas.save(STATIC / name, optimize=True)
        print(f"  {name}  {size}x{size}  캐릭터 {tw}x{th}")

    print(f"\n{len(ICONS)}개 생성 → {STATIC}")


if __name__ == "__main__":
    main()
