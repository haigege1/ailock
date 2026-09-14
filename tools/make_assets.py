"""生成程序图标与默认壁纸。图标绘制的是一枚简洁的挂锁。"""

import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "assets"
OUT.mkdir(parents=True, exist_ok=True)

ACCENT = (56, 189, 248, 255)      # #38BDF8
DARK = (15, 23, 42, 255)          # #0F172A
BG = (11, 18, 32, 255)            # #0B1220


def rounded_rect(draw, box, radius, fill, width=0):
    draw.rounded_rectangle(box, radius=radius, fill=fill, width=width,
                           outline=None)


def draw_lock(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size

    # 背板
    rounded_rect(d, [0, 0, s - 1, s - 1], int(s * 0.22), BG)

    cx = s / 2
    # 锁梁：两段竖线 + 上方半圆
    shackle_w = max(2, int(s * 0.075))
    shackle_top = int(s * 0.20)
    shackle_bottom = int(s * 0.50)
    shackle_r = int(s * 0.155)
    d.arc([cx - shackle_r, shackle_top, cx + shackle_r, shackle_top + 2 * shackle_r],
          start=180, end=360, fill=ACCENT, width=shackle_w)
    d.line([(cx - shackle_r, shackle_top + shackle_r),
            (cx - shackle_r, shackle_bottom)], fill=ACCENT, width=shackle_w)
    d.line([(cx + shackle_r, shackle_top + shackle_r),
            (cx + shackle_r, shackle_bottom)], fill=ACCENT, width=shackle_w)

    # 锁体
    body_l = int(s * 0.235)
    body_r = int(s * 0.765)
    body_t = shackle_bottom - max(1, int(s * 0.02))
    body_b = int(s * 0.81)
    rounded_rect(d, [body_l, body_t, body_r, body_b], int(s * 0.09), ACCENT)

    # 锁孔
    key_r = max(1, int(s * 0.052))
    d.ellipse([cx - key_r, body_t + int(s * 0.10) - key_r,
               cx + key_r, body_t + int(s * 0.10) + key_r], fill=BG)
    slot_w = max(1, int(s * 0.038))
    d.rectangle([cx - slot_w // 2, body_t + int(s * 0.10),
                 cx + slot_w // 2, body_t + int(s * 0.215)], fill=BG)
    return img


def make_ico(path: Path) -> None:
    sizes = [256, 128, 64, 48, 32, 16]
    base = draw_lock(256)
    path.parent.mkdir(parents=True, exist_ok=True)
    base.save(path, format="ICO",
              sizes=[(s, s) for s in sizes],
              bitmap_format="png")


def make_wallpaper(path: Path, w: int = 1920, h: int = 1080) -> None:
    """默认壁纸：由 #0B1220 到 #1E3A5F 的对角渐变，加一点斜向光带。"""
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    top = (11, 18, 32)
    bottom = (30, 58, 95)
    for y in range(h):
        t = y / max(1, h - 1)
        d.line([(0, y), (w, y)],
               fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))

    # 两条柔和的斜向亮带，避免大面积渐变过于单调
    glow = Image.new("RGB", (w, h), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.polygon([(int(w * 0.05), h), (int(w * 0.42), 0),
                (int(w * 0.62), 0), (int(w * 0.25), h)],
               fill=(16, 30, 52))
    gd.polygon([(int(w * 0.62), h), (int(w * 0.98), 0),
                (w, 0), (int(w * 0.72), h)],
               fill=(13, 24, 42))
    img = Image.blend(img, glow, 0.55)
    img.save(path, quality=92)


if __name__ == "__main__":
    ico = OUT / "ailock.ico"
    wall = OUT / "default_wallpaper.jpg"
    make_ico(ico)
    make_wallpaper(wall)
    print(f"图标: {ico} ({ico.stat().st_size} bytes)")
    print(f"壁纸: {wall} ({wall.stat().st_size} bytes)")
