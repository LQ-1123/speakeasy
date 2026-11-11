"""构造部署包（白名单方式）。

为什么不用 `git ls-files`：本期新增的 `backend/app/admin/api/audit.py`、
`frontend/src/admin/speechDna.tsx`、以及前端构建产物都还是未跟踪状态（`??`），
只打已跟踪文件会漏掉它们——上线后表现为"新管理台 404 / 表达 DNA 还是旧文本框"。

也不用"整个目录 tar 再排除"：工作区里有 1.1G 的 `pytest-of-Administrator`、
一堆 `pip-unpack-*`、`.tmp/`、`试听对比/` 等垃圾，排除规则很难写全。
白名单更安全：**只带运行时真正需要的目录**。

绝不打包：data/（线上 253M 真实数据）、.venv/、.git/、node_modules/、
                 以及一切构建缓存与临时产物。
"""

from __future__ import annotations

import os
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 白名单：打包这些顶层条目。顺序无关。
INCLUDE_DIRS = [
    "backend",
    "deploy",
    "scripts",
    "assets",
    "sounds",
    "design",
    "docs",
    "mobile",
]
INCLUDE_FILES = [
    "requirements.txt",
    "requirements-cloud.txt",
    "requirements-tts.txt",
    "pyproject.toml",
    "README.md",
]

#: 即使在这些目录里也要跳过的模式（依赖、缓存、构建中间物）。
SKIP_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".next",
    ".venv",
    "venv",
    ".git",
    ".tmp",
}
SKIP_SUFFIXES = {".pyc", ".pyo", ".log", ".bak"}


def should_skip(path: Path) -> bool:
    if any(part in SKIP_DIR_NAMES for part in path.parts):
        return True
    if path.suffix in SKIP_SUFFIXES:
        return True
    return False


def main() -> int:
    stamp = os.environ.get("STAMP") or time.strftime("%Y%m%d-%H%M")
    out_dir = ROOT / "dist"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"speakeasy-v3-deploy-{stamp}.tar.gz"

    added = 0
    skipped = 0
    with tarfile.open(out_path, "w:gz", compresslevel=6) as tar:
        for name in INCLUDE_DIRS:
            base = ROOT / name
            if not base.is_dir():
                print(f"  ! 缺少目录，跳过: {name}")
                continue
            for path in sorted(base.rglob("*")):
                if should_skip(path):
                    skipped += 1
                    continue
                arc = path.relative_to(ROOT).as_posix()
                tar.add(path, arcname=arc, recursive=False)
                if path.is_file():
                    added += 1
        for name in INCLUDE_FILES:
            path = ROOT / name
            if not path.is_file():
                print(f"  ! 缺少文件，跳过: {name}")
                continue
            tar.add(path, arcname=name)
            added += 1

    size_mb = out_path.stat().st_size / 1024 / 1024
    print(f"打包完成: {out_path}")
    print(f"  文件 {added} 个，跳过 {skipped} 个，{size_mb:.1f} MB")

    # 自检：关键文件必须在包里
    must_have = [
        "backend/app/admin/api/audit.py",
        "backend/app/admin/router.py",
        "backend/app/admin/auth.py",
        "backend/app/static/app/admin/index.html",
        "backend/app/v3/push_config.py",
        "backend/app/v3/prompt_overrides.py",
        "frontend/src/admin/speechDna.tsx",
        "deploy/deploy_v3.sh",
    ]
    with tarfile.open(out_path) as tar:
        names = set(tar.getnames())
    print("\n自检关键文件：")
    missing = []
    for item in must_have:
        ok = item in names
        print(f"  {'OK  ' if ok else 'MISS'} {item}")
        if not ok:
            missing.append(item)
    if missing:
        print(f"\n!! 缺少 {len(missing)} 个关键文件，中止")
        return 1

    print(f"\nPATH={out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
