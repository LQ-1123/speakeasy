"""全量替换 App 图标：Android 桌面/自适应/启动图 + Web/PWA 图标。

源图：assets/brand/icon-source.png（1254×1254，带白色背景）
"""

import os
import shutil

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "assets", "brand", "icon-source.png")
RES = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "res")
PUB = os.path.join(ROOT, "frontend", "public")

icon = Image.open(SRC).convert("RGBA")
print("source:", icon.size)

# ---------- Android launcher ----------
DENSITIES = {
    "mdpi": 48,
    "hdpi": 72,
    "xhdpi": 96,
    "xxhdpi": 144,
    "xxxhdpi": 192,
}
ADAPTIVE = {
    "mdpi": 108,
    "hdpi": 162,
    "xhdpi": 216,
    "xxhdpi": 324,
    "xxxhdpi": 432,
}

for dens, size in DENSITIES.items():
    folder = os.path.join(RES, f"mipmap-{dens}")
    os.makedirs(folder, exist_ok=True)
    square = icon.resize((size, size), Image.LANCZOS)
    square.save(os.path.join(folder, "ic_launcher.png"))
    square.save(os.path.join(folder, "ic_launcher_round.png"))

    # 自适应前景：整张图铺满 108dp 画布，系统遮罩裁切后只留中间绿色区域
    fg_size = ADAPTIVE[dens]
    fg = Image.new("RGBA", (fg_size, fg_size), (0, 0, 0, 0))
    fg.paste(icon.resize((fg_size, fg_size), Image.LANCZOS), (0, 0))
    fg.save(os.path.join(folder, "ic_launcher_foreground.png"))

print("android launcher done")

# ---------- Android splash ----------
splash_dir = os.path.join(RES, "drawable")
splash_files = []
for base, dirs, files in os.walk(RES):
    for f in files:
        if f == "splash.png":
            splash_files.append(os.path.join(base, f))

for path in splash_files:
    with Image.open(path) as old:
        w, h = old.size
    canvas = Image.new("RGB", (w, h), (255, 255, 255))
    side = int(min(w, h) * 0.28)
    logo = icon.resize((side, side), Image.LANCZOS)
    canvas.paste(logo, ((w - side) // 2, (h - side) // 2), logo)
    canvas.save(path)
print(f"android splash done: {len(splash_files)} 张")

# ---------- Web / PWA ----------
def save_web(rel, size, maskable=False):
    path = os.path.join(PUB, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if maskable:
        canvas = Image.new("RGBA", (size, size), (255, 255, 255, 255))
        inner = int(size * 0.8)
        logo = icon.resize((inner, inner), Image.LANCZOS)
        canvas.paste(logo, ((size - inner) // 2, (size - inner) // 2), logo)
        canvas.convert("RGB").save(path)
    else:
        icon.resize((size, size), Image.LANCZOS).save(path)
    print("  web:", rel, size)


for rel, size in [
    ("icons/brand-32.png", 32),
    ("icons/brand-180.png", 180),
    ("icons/brand-192.png", 192),
    ("icons/brand-512.png", 512),
    ("icons/apple-touch-icon.png", 180),
    ("icons/icon-192.png", 192),
    ("icons/icon-512.png", 512),
    ("brand/app-icon.png", 1024),
    ("brand/mark.png", 1024),
    ("brand/mark-256.png", 256),
]:
    save_web(rel, size)

save_web("icons/brand-maskable-512.png", 512, maskable=True)
save_web("icons/maskable-512.png", 512, maskable=True)

# favicon.ico 多尺寸
ico = os.path.join(PUB, "favicon.ico")
icon.resize((256, 256), Image.LANCZOS).save(
    ico, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
)
print("favicon.ico done")
