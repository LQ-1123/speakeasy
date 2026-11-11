"""管理 API · 客户端版本发布（``/api/admin/app-release/*``）。

从 ``app/admin/router.py`` 原样迁移，**路径与返回结构完全不变**：
``GET /app-release``、``POST /app-release/upload``、``POST /app-release/rollback``。

``app_release`` 保持函数内延迟导入：它依赖磁盘上的安装包目录，管理台其它分区
不该为 APK 目录扫描付费。
"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, Request, UploadFile

from app.admin.api import deps
from app.logging_utils import get_logger

log = get_logger(__name__)

#: 不声明 tags：父路由已带 ``tags=["admin"]``，重复声明会让签名里出现两个 "admin"。
router = APIRouter(prefix="/app-release")

@router.get("")
async def get_app_release(request: Request) -> dict:
    """当前 APK 版本、历史版本与服务器上的安装包文件。"""
    from app import app_release  # noqa: PLC0415

    return app_release.status()

@router.post("/upload")
async def upload_app_release(
    request: Request,
    file: UploadFile = File(...),
    version_code: int = Form(...),
    version_name: str = Form(...),
    changelog: str = Form(""),
    force: bool = Form(False),
    min_version_code: int = Form(0),
    platform: str = Form("android"),
    channel: str = Form("release"),
) -> dict:
    """上传 APK 并发布（旧版本自动进历史，可回滚）。"""
    from app import app_release  # noqa: PLC0415

    data = await file.read()
    return app_release.publish(
        file.filename or "upload.apk",
        data,
        version_code=version_code,
        version_name=version_name,
        changelog=changelog,
        force=force,
        min_version_code=min_version_code,
        platform=platform,
        channel=channel,
    )

@router.post("/rollback")
async def rollback_app_release(request: Request) -> dict:
    """回滚到上一版 APK。"""
    from app import app_release  # noqa: PLC0415

    return app_release.rollback()

__all__ = ["router"]
