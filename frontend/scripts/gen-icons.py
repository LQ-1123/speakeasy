#!/usr/bin/env python3
"""生成 SpeakEasy 2.0 PWA 图标（PNG + SVG）。

不依赖仓库内任何第三方库之外的包；只用 Pillow 画几何图形。
运行：python3 scripts/gen-icons.py（产物写入 public/icons/）
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "public" / "icons"

BG_TOP = (55, 190, 118)
BG_BOTTOM = (8, 118, 72)


def lerp(a: int, b: int, t: float) -> int:
    return int(round(a + (b - a) * t))


def make_background(size: int, corner_ratio: float) -> Image.Image:
    """竖向渐变 + 可选圆角（iOS / any 图标用；maskable 为满版）。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    grad = Image.new("RGBA", (1, size))
    px = grad.load()
    for y in range(size):
        t = y / max(1, size - 1)
        px[0, y] = (
            lerp(BG_TOP[0], BG_BOTTOM[0], t),
            lerp(BG_TOP[1], BG_BOTTOM[1], t),
            lerp(BG_TOP[2], BG_BOTTOM[2], t),
            255,
        )
    grad = grad.resize((size, size), Image.Resampling.NEAREST)
    img.alpha_composite(grad)

    # 左上角柔光，让纯色不呆板
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse(
        (int(-size * 0.35), int(-size * 0.45), int(size * 0.75), int(size * 0.55)),
        fill=(255, 255, 255, 34),
    )
    img.alpha_composite(glow)

    if corner_ratio > 0:
        radius = int(size * corner_ratio)
        mask = Image.new("L", (size, size), 0)
        md = ImageDraw.Draw(mask)
        md.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
        img.putalpha(mask)
    return img


def draw_bubble(img: Image.Image, size: int, scale: float = 1.0) -> None:
    """白色对话气泡 + 绿色三点（打字中）。scale<1 用于 maskable 安全区。"""
    d = ImageDraw.Draw(img)
    w = size * 0.62 * scale
    h = w * 0.74
    cx = size / 2
    cy = size / 2 - size * 0.015
    left = cx - w / 2
    top = cy - h / 2
    right = cx + w / 2
    bottom = cy + h / 2
    radius = h * 0.28

    # 气泡尾巴（左下）
    tail_w = w * 0.2
    tail_h = h * 0.22
    tail = [
        (left + w * 0.22, bottom - radius * 0.1),
        (left + w * 0.22, bottom + tail_h),
        (left + w * 0.22 + tail_w, bottom - radius * 0.4),
    ]
    d.polygon(tail, fill=(255, 255, 255, 255))

    d.rounded_rectangle(
        (left, top, right, bottom),
        radius=radius,
        fill=(255, 255, 255, 255),
    )

    # 三个圆点（用渐层绿色）
    dot_r = h * 0.095
    gap = w * 0.135
    dot_cy = cy
    for index in (-1, 0, 1):
        cx_dot = cx + index * gap
        d.ellipse(
            (cx_dot - dot_r, dot_cy - dot_r, cx_dot + dot_r, dot_cy + dot_r),
            fill=(7, 193, 96, 255),
        )

    # 一道高光弧，增加质感
    d.arc(
        (left + w * 0.06, top + h * 0.08, right - w * 0.32, top + h * 0.5),
        start=200,
        end=320,
        fill=(255, 255, 255, 255),
        width=max(2, int(h * 0.05)),
    )


def build(size: int, *, rounded: bool, maskable: bool) -> Image.Image:
    img = make_background(size, 0.225 if rounded else 0.0)
    draw_bubble(img, size, scale=0.78 if maskable else 0.92)
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    targets = [
        ("icon-192.png", 192, True, False),
        ("icon-512.png", 512, True, False),
        ("apple-touch-icon.png", 180, False, False),  # iOS 自己切圆角，给满版
        ("maskable-512.png", 512, False, True),
    ]
    for name, size, rounded, maskable in targets:
        img = build(size, rounded=rounded, maskable=maskable)
        path = OUT / name
        img.save(path, format="PNG", optimize=True)
        print(f"[icons] {path.name:24s} {size}x{size} {path.stat().st_size / 1024:.1f} KB")

    # 额外一个 SVG 版本（部分启动器/文档使用）
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512" role="img" aria-label="SpeakEasy">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#37be76"/>
      <stop offset="1" stop-color="#087648"/>
    </linearGradient>
  </defs>
  <rect width="512" height="512" rx="115" fill="url(#g)"/>
  <path d="M156 300 Q156 236 256 236 Q356 236 356 300 Q356 356 256 356 L196 356 L206 330 Q156 330 156 300 Z" fill="#ffffff" opacity="0"/>
  <rect x="150" y="196" width="212" height="152" rx="42" fill="#ffffff"/>
  <path d="M196 336 L196 390 L246 340 Z" fill="#ffffff"/>
  <circle cx="206" cy="272" r="18" fill="#07c160"/>
  <circle cx="256" cy="272" r="18" fill="#07c160"/>
  <circle cx="306" cy="272" r="18" fill="#07c160"/>
</svg>
"""
    svg_path = OUT / "icon-192.svg"
    svg_path.write_text(svg, encoding="utf-8")
    print(f"[icons] {svg_path.name:24s} svg")


if __name__ == "__main__":
    main()
