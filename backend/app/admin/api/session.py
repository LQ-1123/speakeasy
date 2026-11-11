"""管理 API · 会话与退出（``/api/admin/session``、``/api/admin/logout``）。

与旧的 ``/admin/login``（页面表单，303 重定向）分工
--------------------------------------------------
- 页面侧：``POST /admin/login`` 写 HttpOnly Cookie 后 303 跳回 ``/admin``
  （在 ``app/admin/router.py`` 的 ``page_router``，本任务**不动**）
- API 侧：``GET /api/admin/session`` 给前端读会话状态；``POST /api/admin/logout``
  清 Cookie 并返回 JSON（前端 fetch 后自己跳转，不依赖 3xx）
- 旧的 ``POST /admin/logout``（303 重定向版）**保留**，由另一个任务维护

CSRF 令牌的归属
---------------
``GET /api/admin/session`` 响应里的 ``csrf`` 字段来自 ``app.admin.auth.csrf_token()``
——由 P1 鉴权任务（task-1）实现，是 ``_secret()`` HMAC 派生的稳定值，改密码/改
``SE_ADMIN_SESSION_SECRET`` 会让旧令牌失效。本模块只负责**下发**，不重复实现派生逻辑；
服务端校验在 ``auth.check_csrf``（同样用 ``csrf_token()``），两边天然一致。

前端消费方式（``frontend/src/admin/api.ts``）：拿到后缓存，非 GET 请求带
``X-Admin-CSRF`` 头；403 时失效重取一次。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.admin import auth as admin_auth
from app.logging_utils import get_logger

log = get_logger(__name__)

#: 不声明 tags：父路由已带 ``tags=["admin"]``，重复声明会让签名里出现两个 "admin"。
router = APIRouter()

#: 免守卫的子路由：``/session`` 与 ``/logout`` 必须能在**未登录**时调用。
#:
#: 为什么需要单独一个 router（P4 发现的回归）
#: ------------------------------------------
#: P4 把 ``require_admin`` 挂到了父路由的 ``dependencies`` 上，于是这两个接口
#: 也要求登录——但它们的语义恰恰相反：
#:   - ``GET /session``：前端靠它判断"要不要跳登录页"，拒绝它前端只能猜状态码；
#:   - ``POST /logout``：必须幂等，会话已过期时"退出"按钮仍要能用。
#: 所以把它们从受守卫的路由里拆出来，由 ``router.py`` 单独 include（无依赖）。
public_router = APIRouter()


def _server_version(request: Request) -> str:
    """服务端版本号。

    优先读 ``app.state.version``（``app/main.py`` 启动时写入），取不到再用包版本，
    保证在最小化测试 app（未跑 lifespan）里也有稳定值。
    """
    version = str(getattr(request.app.state, "version", "") or "")
    if version:
        return version
    try:
        from app import __version__  # noqa: PLC0415

        return str(__version__)
    except Exception:  # noqa: BLE001 - 版本号取不到不该让会话接口失败
        return ""


def _csrf_token(request: Request) -> str:
    """CSRF 令牌（委托给 ``app.admin.auth.csrf_token``）。

    刻意**不**在这里重新派生：令牌的派生密钥与校验逻辑必须只有一个来源，
    否则前端拿到的令牌和服务端校验用的值一旦漂移，所有写操作都会 403。
    取不到时退化成空串（前端按"服务端未启用"处理，不会误发错误令牌）。
    """
    try:
        return str(admin_auth.csrf_token(request) or "")
    except Exception as exc:  # noqa: BLE001 - 会话接口不该因令牌派生失败而 500
        log.debug("session: csrf_token 派生失败（降级空串）: %s", exc)
        return ""


@public_router.get("/session")
async def get_session(request: Request) -> dict:
    """当前管理会话：是否已登录、CSRF 令牌、口令是否配置、服务端版本。

    **不抛 403**：未登录也要返回 ``authenticated: false``，前端据此决定跳登录页。
    这与 ``/api/admin/*`` 其它接口"未登录一律 403"的语义不同，是刻意设计——
    会话查询本身就是用来判断"要不要登录"的，拒绝它会让前端只能看状态码猜。
    """
    return {
        "authenticated": bool(admin_auth.authenticated(request)),
        "csrf": _csrf_token(request),
        "password_configured": bool(admin_auth.password_configured()),
        "version": _server_version(request),
    }


@public_router.post("/logout")
async def logout(request: Request) -> JSONResponse:
    """清管理会话 Cookie，返回 JSON（不是重定向，供前端 fetch 调用）。

    刻意**不**走 ``deps.guard``：退出登录必须是幂等的——会话已过期时前端仍需要
    能调通它（否则"退出"按钮在过期会话下会报 403）。它只清调用方自己的 Cookie，
    不泄漏也不改动任何服务端状态。CSRF 防护由 P1 鉴权任务统一提供。
    """
    response = JSONResponse({"ok": True})
    response.delete_cookie(admin_auth.COOKIE_NAME, path="/")
    log.info("管理会话已退出: ip=%s", getattr(getattr(request, "client", None), "host", "?"))
    return response


__all__ = ["public_router", "router"]
