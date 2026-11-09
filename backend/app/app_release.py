"""APK 发布 + 在线更新元数据（公网只读接口 + 管理员发布）。

数据都在 ``data/`` 下：

    data/app_release.json      # 当前版本 + 历史（可回滚）
    data/releases/*.apk        # 安装包（Range 下载，支持断点续传）

公网接口（不需要登录）：

    GET /api/app/latest?platform=android&channel=release
    GET /api/app/download/<filename>

管理接口在 ``app.admin.router``（口令/回环守卫）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.logging_utils import get_logger

log = get_logger(__name__)
router = APIRouter(tags=["app-release"])

MAX_HISTORY = 8
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+\.apk$")


def _root() -> Path:
    from app.config import get_settings  # noqa: PLC0415

    return Path(str(get_settings().data_path or "data"))


def _meta_path() -> Path:
    return _root() / "app_release.json"


def apk_dir() -> Path:
    path = _root() / "releases"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_meta() -> dict[str, Any]:
    path = _meta_path()
    if not path.exists():
        return {"current": None, "history": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning("app_release.json 读取失败: %s", exc)
        return {"current": None, "history": []}
    if not isinstance(data, dict):
        return {"current": None, "history": []}
    data.setdefault("current", None)
    data.setdefault("history", [])
    return data


def save_meta(meta: dict[str, Any]) -> None:
    path = _meta_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    if path.exists():
        shutil.copy2(path, path.with_suffix(".bak.json"))
    os.replace(tmp, path)


def _public_view(entry: dict[str, Any] | None) -> dict[str, Any]:
    if not entry:
        return {"available": False, "versionCode": 0}
    filename = str(entry.get("filename") or "")
    return {
        "available": True,
        "platform": entry.get("platform", "android"),
        "channel": entry.get("channel", "release"),
        "versionCode": int(entry.get("version_code") or 0),
        "versionName": str(entry.get("version_name") or ""),
        "apkUrl": f"/api/app/download/{filename}",
        "sha256": str(entry.get("sha256") or ""),
        "size": int(entry.get("size") or 0),
        "changelog": str(entry.get("changelog") or ""),
        "force": bool(entry.get("force")),
        "minVersionCode": int(entry.get("min_version_code") or 0),
        "publishedAt": str(entry.get("published_at") or ""),
    }


def latest_info(platform: str = "android", channel: str = "release") -> dict[str, Any]:
    entry = load_meta().get("current")
    if not entry:
        return {"available": False, "versionCode": 0}
    if str(entry.get("platform") or "android") != platform:
        return {"available": False, "versionCode": 0}
    if str(entry.get("channel") or "release") != channel:
        return {"available": False, "versionCode": 0}
    return _public_view(entry)


# --------------------------------------------------------------------------- #
# 公网只读
# --------------------------------------------------------------------------- #
@router.get("/api/app/latest")
async def app_latest(platform: str = "android", channel: str = "release") -> dict[str, Any]:
    return latest_info(platform=platform, channel=channel)


@router.get("/api/app/download/{filename}")
async def app_download(filename: str, request: Request) -> FileResponse:
    if not _SAFE_NAME.match(filename or ""):
        raise HTTPException(status_code=404, detail="文件不存在")
    path = apk_dir() / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(
        path,
        media_type="application/vnd.android.package-archive",
        filename=filename,
        headers={"Cache-Control": "no-cache", "Accept-Ranges": "bytes"},
    )


# --------------------------------------------------------------------------- #
# 管理端（被 app.admin.router 调用）
# --------------------------------------------------------------------------- #
def status() -> dict[str, Any]:
    meta = load_meta()
    files = sorted(
        [
            {
                "filename": p.name,
                "size": p.stat().st_size,
                "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime)),
            }
            for p in apk_dir().glob("*.apk")
        ],
        key=lambda item: item["modified"],
        reverse=True,
    )
    return {
        "current": _public_view(meta.get("current")),
        "raw": meta.get("current"),
        "history": [_public_view(item) for item in (meta.get("history") or [])],
        "files": files,
    }


def publish(
    filename: str,
    data: bytes,
    *,
    version_code: int,
    version_name: str,
    changelog: str = "",
    force: bool = False,
    min_version_code: int = 0,
    platform: str = "android",
    channel: str = "release",
) -> dict[str, Any]:
    """保存 APK + 发布新版本（旧版本进历史，可回滚）。"""
    version_code = int(version_code or 0)
    if version_code <= 0:
        return {"ok": False, "issues": ["versionCode 必须是正整数（且要比上一版大）"]}
    version_name = str(version_name or "").strip()
    if not version_name:
        return {"ok": False, "issues": ["versionName 不能为空"]}
    if not data or len(data) < 1024:
        return {"ok": False, "issues": ["APK 文件为空或过小"]}
    platform = str(platform or "android").strip().lower() or "android"
    channel = str(channel or "release").strip().lower() or "release"

    meta = load_meta()
    current = meta.get("current")
    if current and int(current.get("version_code") or 0) == version_code:
        return {"ok": False, "issues": ["同 versionCode 已经发布过（覆盖请先回滚或换版本号）"]}

    sha = hashlib.sha256(data).hexdigest()
    safe_name = f"speakeasy-{re.sub(r'[^0-9A-Za-z._-]', '_', version_name)}-{version_code}.apk"
    target = apk_dir() / safe_name
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)

    entry = {
        "platform": platform,
        "channel": channel,
        "version_code": version_code,
        "version_name": version_name,
        "filename": safe_name,
        "sha256": sha,
        "size": len(data),
        "changelog": str(changelog or "").strip(),
        "force": bool(force),
        "min_version_code": int(min_version_code or 0),
        "published_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_filename": filename,
    }
    history = list(meta.get("history") or [])
    if current:
        history.insert(0, current)
    meta["current"] = entry
    meta["history"] = history[:MAX_HISTORY]
    save_meta(meta)
    log.info("APK 已发布: %s (%s) sha256=%s", safe_name, version_name, sha[:12])
    return {"ok": True, "current": _public_view(entry)}


def rollback() -> dict[str, Any]:
    meta = load_meta()
    history = list(meta.get("history") or [])
    current = meta.get("current")
    if not history:
        return {"ok": False, "issues": ["没有可回滚的历史版本"]}
    restored = history.pop(0)
    if current:
        history.insert(0, current)
    meta["current"] = restored
    meta["history"] = history[:MAX_HISTORY]
    save_meta(meta)
    return {"ok": True, "current": _public_view(restored)}


__all__ = [
    "apk_dir",
    "latest_info",
    "load_meta",
    "publish",
    "rollback",
    "router",
    "status",
]
