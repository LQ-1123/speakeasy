"""把 Avatar Lab 模板 + esbuild 产物合成单文件 HTML（离线可双击打开）。

用法：
    python scripts/build_avatar_lab.py
产物：
    docs/avatar-lab.html
前置：
    在 frontend/ 目录执行
    npx esbuild avatar-lab-demo.js --bundle --format=iife --minify --outfile=../docs/avatar-lab/bundle.js
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "docs" / "avatar-lab" / "template.html"
BUNDLE = ROOT / "docs" / "avatar-lab" / "bundle.js"
OUT = ROOT / "docs" / "avatar-lab.html"
TOKEN = "/*__AVATAR_LAB_BUNDLE__*/"


def main() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")
    bundle = BUNDLE.read_text(encoding="utf-8")
    if TOKEN not in template:
        raise SystemExit(f"模板里找不到占位符 {TOKEN}")
    html = template.replace(TOKEN, bundle)
    OUT.write_text(html, encoding="utf-8")
    size_kb = OUT.stat().st_size / 1024
    print(f"written: {OUT} ({size_kb:.1f} KB)")


if __name__ == "__main__":
    main()
