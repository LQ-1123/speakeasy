"""``/uploads/*`` 受保护静态路由（多用户图片归属校验）。

背景
----
上传的聊天/动态图片落在 ``data/uploads/{user_hash}/{digest}.webp``，此前是
``StaticFiles`` 直挂——拿到别人的 URL 就能读。归属在落盘时已写入
``media_assets``（``kind='post_image'``），本模块把静态挂载换成**带校验的挂载**：

- 未登记的历史文件：保持可读（``X-Legacy-Asset: 1`` 标明兼容），不改变老数据行为；
- 已登记文件：只有 owner 本人（会话/设备身份均可）或持有该文件有效 ``?t=<media_token>``
  的请求可读；其他已登录用户 403，未登录/无凭据 403（浏览器 ``<img>`` 可带 Cookie，
  或先调 ``/api/v3/media/access?kind=post_image&filename=<相对路径>`` 领 token 再拼 URL）；
- 拒绝 ``..``、隐藏文件等非法路径（交给 :func:`media_access.normalize_filename` 判定）。

用法（``main.py``）::

    app.mount("/uploads", ProtectedUploads(directory=uploads_dir, kind="post_image"), name="uploads")
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.requests import Request
from starlette.staticfiles import StaticFiles

from app.logging_utils import get_logger
from app.v3.media_access import (
    normalize_filename,
    owner_of_filename,
    verify_media_token,
)

log = get_logger(__name__)


class ProtectedUploads(StaticFiles):
    """按 ``media_assets`` 归属校验的静态文件挂载。"""

    def __init__(self, *, directory: str, kind: str = "post_image", **kwargs: Any) -> None:
        super().__init__(directory=directory, **kwargs)
        self.asset_kind = kind

    @staticmethod
    def _db(scope: dict) -> Any:
        app = scope.get("app")
        services = getattr(getattr(app, "state", None), "v2", None)
        return getattr(services, "db", None)

    def _owner(self, db: Any, rel_path: str) -> tuple[str, str] | None:
        try:
            return owner_of_filename(db, rel_path, kinds=(self.asset_kind,))
        except Exception as exc:  # noqa: BLE001 - 归属查询失败不阻塞读（历史兼容优先）
            log.debug("uploads 归属查询失败（按未登记处理）: %s", exc)
            return None

    async def get_response(self, path: str, scope: dict):  # type: ignore[override]
        rel = normalize_filename(path)
        db = self._db(scope)
        if not rel or db is None:
            # 非法路径 / 没有库：交给 StaticFiles 的默认行为（404 或读取）
            return await super().get_response(path, scope)

        owner = self._owner(db, rel)
        if owner is None:
            response = await super().get_response(path, scope)
            response.headers["X-Legacy-Asset"] = "1"
            return response

        owner_user, owner_kind = owner
        request = Request(scope)
        token = request.query_params.get("t") or ""
        if token:
            verified = verify_media_token(db, token)
            if verified is not None:
                token_user, token_kind, token_file = verified
                if token_file == rel and token_user == owner_user:
                    return await super().get_response(path, scope)
            return JSONResponse({"detail": "媒体令牌无效或已过期"}, status_code=403)

        # 无 token：用统一身份解析（会话凭据 / 旧设备身份 / local 单用户模式）
        try:
            from app.v3.security import bind_current_user  # noqa: PLC0415

            user_id = await bind_current_user(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        except Exception as exc:  # noqa: BLE001
            log.debug("uploads 身份解析失败: %s", exc)
            return JSONResponse({"detail": "需要登录后访问该媒体"}, status_code=401)
        if user_id and user_id == owner_user:
            return await super().get_response(path, scope)
        return JSONResponse(
            {"detail": f"无权访问该媒体（owner={owner_kind}）"}, status_code=403
        )


__all__ = ["ProtectedUploads"]
