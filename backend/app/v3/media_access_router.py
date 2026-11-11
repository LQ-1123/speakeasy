"""V3 · 媒体访问令牌路由（多用户数据隔离配套）。

为什么要单独一个 router 文件
---------------------------
``app/v3/router.py`` 是多用户升级里其他人也在改的热点文件，媒体访问端点独立成
``media_access_router``，由 ``main.py`` 统一接线（Lead 负责）：

    from app.v3.media_access_router import router as media_access_router
    app.include_router(media_access_router)

端点
----
``GET /api/v3/media/access?kind=audio&filename=xxx.wav``
    → ``{"token": "...", "expires_in": 300, "kind": ..., "filename": ...}``

- **需要登录**：身份统一由 ``bind_current_user`` 解析——会话凭据（``X-Session-Token`` /
  ``Authorization: Bearer`` / ``sonder_session`` Cookie）与旧设备身份（``X-User-Id`` +
  ``X-User-Token``）都支持。解析结果是 ``local``（本地单用户模式）时返回 401：
  本地模式的 owner 恒为 ``local``，跨用户隔离不生效，也就不需要令牌。
- 只有资产 owner 能拿到令牌：已登记且 owner 不是当前用户 → 403。
- 未登记的历史文件也允许签发（历史文件本来就对所有人可读，令牌不增加暴露面；
  ``verify_media_token`` 会校验 token 里的 user 与资产 owner 一致，故无越权）。

令牌用法
--------
``<audio src="/api/v2/audio/xxx.wav?t=<token>">`` / ``/api/v2/media/xxx.png?t=<token>``；
音频/图片读取端在「已登记且 owner 不是当前用户」时只认这个 ``t`` 参数。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request

from app.logging_utils import get_logger
from app.v3.identity import LOCAL_USER_ID
from app.v3.media_access import (
    ensure_media_schema,
    issue_media_token,
    normalize_filename,
    owner_of,
)
from app.v3.security import bind_current_user

log = get_logger(__name__)

router = APIRouter(prefix="/api/v3/media", tags=["v3-media"])

_KIND_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

_DEFAULT_TTL_S = 300
_MAX_TTL_S = 3600


def _services_db(request: Request):
    services = getattr(request.app.state, "v2", None)
    db = getattr(services, "db", None)
    if db is None:  # pragma: no cover - 装配异常
        raise HTTPException(status_code=503, detail="服务未初始化")
    return db


@router.get("/access")
async def get_media_access(
    request: Request,
    kind: str,
    filename: str,
    ttl_s: int = _DEFAULT_TTL_S,
    user_id: str = Depends(bind_current_user),
) -> dict:
    """签发一条媒体的短时访问令牌（需登录，且只给资产 owner）。"""
    # 「需登录」：bind_current_user 已统一解析会话凭据（X-Session-Token / Bearer /
    # sonder_session Cookie）与旧设备身份；解析为 local 表示没有真实账号身份——
    # 本地单用户模式不需要令牌（owner 恒为 local），这里直接 401 更明确。
    if not user_id or user_id == LOCAL_USER_ID:
        raise HTTPException(status_code=401, detail="需要登录后才能申请媒体访问令牌")

    kind_value = str(kind or "").strip().lower()
    if not _KIND_RE.match(kind_value):
        raise HTTPException(status_code=400, detail="kind 不合法")
    name = normalize_filename(filename)
    if not name:
        raise HTTPException(status_code=400, detail="filename 不合法")

    try:
        ttl = int(ttl_s)
    except (TypeError, ValueError):
        ttl = _DEFAULT_TTL_S
    ttl = max(30, min(ttl, _MAX_TTL_S))

    db = _services_db(request)
    ensure_media_schema(db)
    owner = owner_of(db, kind_value, name)
    if owner is not None and owner != user_id:
        raise HTTPException(status_code=403, detail="无权访问该媒体资产")

    token = issue_media_token(db, user_id, kind_value, name, ttl_s=ttl)
    return {
        "token": token,
        "kind": kind_value,
        "filename": name,
        "expires_in": ttl,
    }


__all__ = ["router"]
