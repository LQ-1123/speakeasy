"""管理端（控制面）：词库管理，与客户端完全解耦。

三条原则
--------
1. **仅本机**：默认只允许回环地址（``127.0.0.1`` / ``::1``）访问；如需远程，
   设置 ``SE_ADMIN_TOKEN`` 并在请求头带 ``X-Admin-Token``。
2. **不碰角色**：这里只管理"捏人原料"（枚举词 / 场景 / 口头禅 / 素材池），
   不编辑具体某个人物——人物是成品，改了会漂移。
3. **改不坏**：保存前必须过引用完整性校验（枚举值要带数值映射、默认值必须存在…），
   写盘原子 + 保留上一版；校验不过直接 400，不会落盘。

本模块的职责（P1 路由拆分后）
-----------------------------
``router`` 退化为**聚合器**：按域拆到 :mod:`app.admin.api` 包下的子模块，
这里只负责 ``include_router`` 并保留对外前缀 ``/api/admin``。

- 页面路由（``/admin``、``/admin/login``、``/admin/logout``）仍在 ``page_router``，
  **不属于** API 包
- 各子模块只声明自己的域前缀（如 ``/media``），完整路径由两层拼接
- 旧路径与返回结构**完全不变**（984 个基线用例守护）

新增路由只加到 ``app/admin/api/`` 对应模块，不要往本文件堆 handler。
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app.admin import auth as admin_auth
from app.logging_utils import get_logger

log = get_logger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    # 鉴权 / CSRF / 审计统一挂在路由级：handler 里不再逐个写 `deps.guard`。
    # 这样做了两件事（都是 P4 的收口目标）：
    #   1. CSRF 从过渡期的「灰名单」变成**全强制**——Cookie 身份的写请求必须带
    #      `X-Admin-CSRF`（token / 本机直通豁免）；
    #   2. 写操作成功后由 `_audit_after` 真正落 `admin_audit`，
    #      操作日志页才有数据可看。
    dependencies=[Depends(admin_auth.require_admin)],
)
page_router = APIRouter(tags=["admin"])

#: 不鉴权的管理 API（``/api/admin/session``、``/api/admin/logout``）。
#: 与 ``router`` 同前缀但**不带** ``require_admin`` 依赖——由 ``app/main.py`` 单独 include。
api_public_router = APIRouter(prefix="/api/admin", tags=["admin"])

#: Next 静态导出产物：管理台单页。
#: ``backend/app/static/app/admin/index.html``，由 ``frontend`` 的
#: ``next build`` + ``scripts/copy-out.mjs`` 生成。
_ADMIN_APP_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "app"
)


def _admin_index_path() -> str:
    """管理台 index.html 的路径。

    用函数而不是模块常量：测试要能把它指到不存在的路径，
    验证「产物缺失」时的降级行为（常量无法被 monkeypatch 替换）。
    """
    return os.path.join(_ADMIN_APP_DIR, "admin", "index.html")


def _admin_html(content: str) -> HTMLResponse:
    return HTMLResponse(
        content,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


def _login_response(request: Request, *, error: str = ""):
    """统一的登录页响应（带回跳地址）。"""
    next_url = str(request.query_params.get("next", "") or "/admin")
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/admin"
    return _admin_html(admin_auth.render_login_page(error=error, next_url=next_url))


def _denied(request: Request):
    """未登录时的响应。

    三种形态（顺序不能变，否则会把本机直通挡掉）：
    1. 未配口令/token + 回环来源 → 放行（本机调试形态，与迁移前一致）
    2. 配了口令/token → 登录页
    3. 其它 → 登录页（不给 403，避免前端靠状态码猜）
    """
    host = str(getattr(getattr(request, "client", None), "host", "") or "")
    if (
        not admin_auth.password_configured()
        and not admin_auth.token_configured()
        and host in _LOOPBACK
    ):
        return None  # 调用方据此放行
    return _admin_html(admin_auth.render_login_page(next_url="/admin"))


def _console_response() -> HTMLResponse:
    """返回 React 管理台页面；产物缺失时给出可操作的提示而不是 404。"""
    index_path = _admin_index_path()
    if os.path.exists(index_path):
        return HTMLResponse(
            content=_read_text(index_path),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )
    return HTMLResponse(
        status_code=503,
        content=(
            "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
            "<title>Sonder · 管理台未构建</title>"
            "<body style=\"font:14px/1.6 system-ui;padding:40px;max-width:640px\">"
            "<h1 style='font-size:18px'>管理台前端尚未构建</h1>"
            "<p>服务端找不到 <code>backend/app/static/app/admin/index.html</code>。</p>"
            "<p>请在 <code>frontend/</code> 下执行 <code>npm run build:backend</code> 后重试。</p>"
            "</body></html>"
        ),
        headers={"Cache-Control": "no-store"},
    )


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as handle:  # noqa: PTH123
        return handle.read()


@page_router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request) -> HTMLResponse:
    """新管理台入口（React 单页）。

    鉴权：Cookie 会话 / 旧 ``?token=`` 链接 / 未配口令时的本机直通。
    **必须显式注册**：``main.py`` 末尾把整个静态目录挂在 ``/``，
    若不在这里拦一道，``/admin/index.html`` 会被 StaticFiles 直接吐出来，
    绕过登录。
    """
    query_token = str(request.query_params.get("token", "") or "").strip()
    if admin_auth.authenticated(request) or admin_auth.token_ok(query_token):
        return _console_response()
    denied = _denied(request)
    if denied is None:  # 未配口令 + 回环直通
        return _console_response()
    return denied


@page_router.get("/admin/{path:path}", response_class=HTMLResponse)
async def admin_page_deep(request: Request, path: str) -> HTMLResponse:
    """新管理台的深链接（``/admin/index.html``、``/admin/anything``）。

    hash 路由下正常不会走到这里，但用户可能直接访问 ``/admin/index.html``，
    或把 ``/admin/`` 当目录访问——这两种都必须同样受登录保护，
    否则等于绕开守卫（``/admin/index.html`` 会被静态挂载直接吐出）。
    """
    return await admin_page(request)


@page_router.post("/admin/login")
async def admin_login(request: Request):
    """密码登录：成功写 HttpOnly 会话 Cookie 并跳回管理台。"""
    form = await request.form()
    password = str(form.get("password") or "")
    next_url = str(form.get("next") or "/admin")
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/admin"
    ip = str(getattr(getattr(request, "client", None), "host", "") or "?")
    allowed, wait_s = admin_auth.check_rate(ip)
    if not allowed:
        return HTMLResponse(
            admin_auth.render_login_page(
                error=f"尝试次数过多，请 {wait_s} 秒后再试", next_url=next_url
            ),
            status_code=429,
        )
    if not admin_auth.password_configured():
        return HTMLResponse(
            admin_auth.render_login_page(
                error="服务端未设置 SE_ADMIN_PASSWORD；可继续用 ?token= 链接进入",
                next_url=next_url,
            ),
            status_code=400,
        )
    if not admin_auth.password_ok(password):
        admin_auth.record_fail(ip)
        return HTMLResponse(
            admin_auth.render_login_page(error="密码不正确", next_url=next_url),
            status_code=401,
        )
    admin_auth.reset_rate(ip)
    response = RedirectResponse(next_url or "/admin", status_code=303)
    response.set_cookie(
        admin_auth.COOKIE_NAME,
        admin_auth.make_session(),
        max_age=admin_auth.SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return response


@page_router.post("/admin/logout")
async def admin_logout():
    """清掉登录 Cookie（页面上的「退出登录」按钮用）。"""
    response = RedirectResponse("/admin", status_code=303)
    response.delete_cookie(admin_auth.COOKIE_NAME, path="/")
    return response


_LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


# --------------------------------------------------------------------------- #
# API 聚合：按域拆分的子路由（见 app/admin/api/__init__.py 的模块分布表）
# --------------------------------------------------------------------------- #
def _include_api_routers() -> None:
    """把各域子路由挂到 ``router`` 上。

    延迟到函数内导入：子模块会 ``from app.admin.api import deps``，而 ``deps`` 又
    在运行时才 ``from app.admin import auth``；放在模块顶层导入虽不构成循环，
    但会让 ``app.admin.router`` 一被 import 就拉起全部管理端依赖（图片库、音色池、
    APK 发布…）。改到函数内后，import 顺序与迁移前保持一致。
    """
    from app.admin.api import (  # noqa: PLC0415
        accounts,
        ads,
        audit,
        config,
        events,
        jobs,
        lexicons,
        media,
        overview,
        personas,
        posts,
        prompts,
        release,
        reports,
        session,
        teen,
        users,
    )
    for module in (
        # 新增接口（P1）
        overview,
        jobs,
        # 旧路由迁移（路径与行为不变）
        media,
        personas,
        prompts,
        lexicons,
        config,
        release,
        # 新增接口（内容举报）：列表 / 处理 / 汇总
        reports,
        # 新增接口（P3）
        users,
        events,
        posts,
        # 新增接口（账号运营）：邀请码 / 账号查询 / 年龄核验
        accounts,
        # 新增接口（青少年模式）：监护绑定 / 限制 / 状态
        teen,
        # 新增接口（商业运营）：品牌 / 活动 / 素材 / 投放记录
        ads,
        # 新增接口（P4）：操作日志
        audit,
    ):
        router.include_router(module.router)


def _include_public_routers() -> None:
    """挂**不鉴权**的管理接口。

    ``include_router`` 会把父路由的 ``dependencies`` 一并带给子路由，所以
    “必须允许未登录访问”的接口（会话查询、退出登录）**不能**挂在 ``router`` 下，
    只能走这个平行路由，由 ``app/main.py`` 单独 include。

    ``session`` 模块因此不参与上面的循环，直接以 ``public_router`` 形式挂入。
    """
    from app.admin.api import session  # noqa: PLC0415

    api_public_router.include_router(session.public_router)


_include_api_routers()
_include_public_routers()

__all__ = ["api_public_router", "page_router", "router"]
