"""管理台登录态：密码登录 + 签名 Cookie（兼容原 token 链接）。

- 密码来自环境变量 ``SE_ADMIN_PASSWORD``；未设置时仍可用旧 ``?token=`` 链接/请求头。
- 登录成功后写 HttpOnly Cookie（默认 30 天），``/api/admin/*`` 自动认 Cookie，
  管理页 JS 不用再在请求头里带 token。
- Cookie 值 = ``v1.<过期时间戳>.<HMAC-SHA256 前 40 位>``，密钥由
  ``SE_ADMIN_SESSION_SECRET``（未设则由密码+token 派生）决定；
  改密码后旧会话自动失效。
- 暴力破解：同一 IP 连续 5 次失败锁 5 分钟（进程内，重启清零）。

路由守卫与 CSRF（第 1 期新增，见 ``docs/ADMIN_CONSOLE_CONTRACT.md`` §1.2）
--------------------------------------------------------------------------
- ``require_admin`` 是 **FastAPI 依赖**，语义与旧的 ``_guard(request)`` 一致：
  Cookie 会话 → ``X-Admin-Token`` 头 → 未配口令时的回环直通；不通过抛 403。
  路由挂一次即可：``APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])``。
- 同一条依赖链顺带做两件事，handler 不用手写：
  1. **CSRF**：非 GET/HEAD/OPTIONS 必须同源 + 带 ``X-Admin-CSRF`` 头；
  2. **操作日志**：写操作成功后落一条 ``admin_audit``（见 ``app.admin.audit``）。
- CSRF 令牌用 HMAC 从 ``_secret()`` 派生（单管理员场景，稳定值即可）；
  带合法 ``X-Admin-Token`` 的脚本请求、以及「未配口令的回环直通」豁免 CSRF。
"""

from __future__ import annotations

import hashlib
import hmac
import html
import os
import secrets
import time
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from app.admin import audit
from app.logging_utils import get_logger

log = get_logger(__name__)

COOKIE_NAME = "se_admin_session"
SESSION_TTL = 30 * 24 * 3600
_FAILS: dict[str, list[float]] = {}
MAX_FAILS = 5
FAIL_WINDOW = 300.0

#: 回环来源白名单（``testclient`` 是 FastAPI TestClient 的默认 host）
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
#: 不校验 CSRF 的方法（只读）
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: CSRF 头名
CSRF_HEADER = "X-Admin-CSRF"
#: CSRF 令牌派生前缀（改这里 = 让所有在用的令牌失效）
_CSRF_LABEL = b"admin-csrf.v1"


def _env(name: str) -> str:
    return str(os.getenv(name, "") or "").strip()


def admin_password() -> str:
    return _env("SE_ADMIN_PASSWORD")


def admin_token() -> str:
    return _env("SE_ADMIN_TOKEN")


def password_configured() -> bool:
    return bool(admin_password())


def token_configured() -> bool:
    return bool(admin_token())


def _secret() -> bytes:
    explicit = _env("SE_ADMIN_SESSION_SECRET")
    if explicit:
        return explicit.encode("utf-8")
    seed = f"speakeasy-admin|{admin_password()}|{admin_token()}"
    return hashlib.sha256(seed.encode("utf-8")).digest()


def password_ok(value: str) -> bool:
    expected = admin_password()
    if not expected:
        return False
    return secrets.compare_digest(str(value or ""), expected)


def token_ok(value: str) -> bool:
    expected = admin_token()
    if not expected:
        return False
    return secrets.compare_digest(str(value or ""), expected)


# --------------------------------------------------------------------------- #
# 会话 cookie
# --------------------------------------------------------------------------- #
def make_session(*, now: float | None = None, ttl: int = SESSION_TTL) -> str:
    exp = int((now if now is not None else time.time()) + max(60, int(ttl)))
    payload = f"v1.{exp}"
    sig = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:40]
    return f"{payload}.{sig}"


def verify_session(value: str, *, now: float | None = None) -> bool:
    raw = str(value or "").strip()
    try:
        version, exp_s, sig = raw.split(".", 2)
        if version != "v1":
            return False
        exp = int(exp_s)
    except (TypeError, ValueError):
        return False
    expected = hmac.new(
        _secret(), f"v1.{exp}".encode("utf-8"), hashlib.sha256
    ).hexdigest()[:40]
    if not hmac.compare_digest(sig, expected):
        return False
    return (now if now is not None else time.time()) < exp


def cookie_ok(request: Any) -> bool:
    try:
        value = request.cookies.get(COOKIE_NAME, "")
    except Exception:  # noqa: BLE001
        value = ""
    return verify_session(value)


def header_token_ok(request: Any) -> bool:
    try:
        value = str(request.headers.get("X-Admin-Token", "") or "").strip()
    except Exception:  # noqa: BLE001
        value = ""
    return token_ok(value)


def authenticated(request: Any) -> bool:
    """请求是否已带有效管理身份（cookie 或旧的 X-Admin-Token 头）。"""
    if cookie_ok(request) or header_token_ok(request):
        return True
    return False


def client_host(request: Any) -> str:
    """调用方 IP（拿不到时给 ``?``，与登录限流的口径一致）。"""
    return str(getattr(getattr(request, "client", None), "host", "") or "?")


def loopback_passthrough(request: Any) -> bool:
    """未配口令也未配 token 时的本机直通（旧 ``_guard`` 的第二条分支）。"""
    if password_configured() or token_configured():
        return False
    return client_host(request) in LOOPBACK_HOSTS


# --------------------------------------------------------------------------- #
# CSRF
# --------------------------------------------------------------------------- #
def csrf_token(request: Any = None) -> str:
    """会话派生的 CSRF 令牌（``GET /api/admin/session`` 下发给前端）。

    单管理员场景：稳定值即可，不随请求变化；但由 ``_secret()``（密码/口令密钥）
    HMAC 派生，**不是**可预测的公开常量——改密码/改 ``SE_ADMIN_SESSION_SECRET``
    会同时让旧会话与旧令牌失效。``request`` 参数保留，便于将来做 per-session 派生。
    """
    return hmac.new(_secret(), _CSRF_LABEL, hashlib.sha256).hexdigest()[:40]


def csrf_header_ok(request: Any) -> bool:
    """请求头里的 CSRF 令牌是否与当前密钥派生值一致（常数时间比较）。"""
    try:
        value = str(request.headers.get(CSRF_HEADER, "") or "").strip()
    except Exception:  # noqa: BLE001
        value = ""
    return bool(value) and hmac.compare_digest(value, csrf_token(request))


def _host_key(value: str) -> str:
    """把 ``http://host:port`` / ``host:port`` / ``host`` 归一成可比较的 host[:port]。"""
    raw = str(value or "").strip()
    if not raw or raw.lower() == "null":
        return ""
    if "://" not in raw:
        raw = "//" + raw.lstrip("/")
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
        if not host:
            return ""
        port = parts.port
    except ValueError:  # 端口非法等
        return ""
    scheme = (parts.scheme or "").lower()
    if port is None or port == {"http": 80, "https": 443}.get(scheme):
        return host
    return f"{host}:{port}"


def _origin_of(request: Any) -> tuple[str, str]:
    """取请求来源：优先 ``Origin``，缺失退化到 ``Referer``；返回 (值, 头名)。"""
    try:
        origin = str(request.headers.get("origin", "") or "").strip()
        if origin:
            return origin, "Origin"
        referer = str(request.headers.get("referer", "") or "").strip()
        if referer:
            return referer, "Referer"
    except Exception:  # noqa: BLE001
        pass
    return "", ""


def same_origin(request: Any) -> bool:
    """``Origin``/``Referer`` 与请求 Host 是否同源。

    两个来源头都没有时**不作结论**（返回 ``True``）：此时由 ``Sec-Fetch-Site``
    或 CSRF 令牌头兜底，见 ``check_csrf``。
    """
    origin, _name = _origin_of(request)
    if not origin:
        return True
    try:
        host = str(request.headers.get("host", "") or "").strip()
    except Exception:  # noqa: BLE001
        host = ""
    expected = _host_key(host)
    actual = _host_key(origin)
    return bool(expected) and bool(actual) and actual == expected


def check_csrf(request: Any) -> None:
    """非安全方法的 CSRF 校验；不通过抛 403。安全方法直接返回。

    三条同时成立才算通过（豁免在 ``require_admin`` 里处理）：
    1. 有 ``Origin``/``Referer`` 时必须与 Host 同源；
    2. 两个来源头都没有时看 ``Sec-Fetch-Site``：``cross-site``/``none`` 直接拒绝；
    3. 必须带与 ``csrf_token()`` 一致的 ``X-Admin-CSRF`` 头。
    """
    try:
        method = str(request.method or "").upper()
    except Exception:  # noqa: BLE001
        method = ""
    if method in SAFE_METHODS:
        return

    origin, name = _origin_of(request)
    if origin:
        if not same_origin(request):
            raise HTTPException(
                status_code=403,
                detail=f"CSRF 校验失败：{name} 与本站不同源",
            )
    else:
        try:
            site = str(request.headers.get("sec-fetch-site", "") or "").strip().lower()
        except Exception:  # noqa: BLE001
            site = ""
        if site in ("cross-site", "none"):
            raise HTTPException(
                status_code=403,
                detail=f"CSRF 校验失败：Sec-Fetch-Site={site}",
            )
        # 老客户端/脚本没有来源信息：不作无条件放行，退回令牌头校验。

    if not csrf_header_ok(request):
        raise HTTPException(
            status_code=403,
            detail=f"CSRF 校验失败：缺少或错误的 {CSRF_HEADER} 请求头",
        )


# --------------------------------------------------------------------------- #
# 路由守卫（FastAPI 依赖）
# --------------------------------------------------------------------------- #
def require_admin_identity(request: Request) -> str:
    """身份判定，返回本次请求的放行方式；不通过抛 403。

    返回值用于 CSRF 豁免判断：``cookie`` / ``token`` / ``loopback``。
    判定顺序：``X-Admin-Token`` 头 → 未配口令的本机直通 → Cookie 会话
    （前两者豁免 CSRF，只有 Cookie 身份需要 CSRF 校验）。

    单独导出是为了给**过渡期**留口子：旧管理台（``page.py`` 内联 JS）在 Cookie
    登录下不会带 CSRF 头，需要「只鉴权、不校验 CSRF」的挂法时可单独依赖本函数；
    新 React 管理台一律用 ``require_admin``（鉴权 + CSRF + 操作日志）。
    """
    if header_token_ok(request):
        return "token"
    if loopback_passthrough(request):
        return "loopback"
    if cookie_ok(request):
        return "cookie"
    if password_configured() or token_configured():
        raise HTTPException(status_code=403, detail="请先登录管理台")
    raise HTTPException(status_code=403, detail="管理端仅允许本机访问")


async def _json_body_keys(request: Any) -> list[str]:
    """读 JSON body 的顶层字段名（**只要名字，不要值**）。

    只为生成"改了哪些字段"的摘要。三个保守条件，任一不满足就跳过：
    非 JSON、没有 ``Content-Length``（chunked 大上传）、或超过 256 KiB——
    绝不为了写摘要把大 body 读进内存。读取会命中 Starlette 的 body 缓存，
    不影响 handler 里 ``Body(...)`` 的解析。
    """
    try:
        method = str(request.method or "").upper()
        if method in SAFE_METHODS:
            return []
        content_type = str(request.headers.get("content-type", "") or "").lower()
        if "json" not in content_type:
            return []
        raw_length = request.headers.get("content-length")
        if raw_length is None:  # chunked 上传：大小不可知，不读
            return []
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            return []
        if length <= 0 or length > 262144:  # 空 body / 超 256 KiB 都不解析
            return []
        raw = await request.body()
    except Exception:  # noqa: BLE001 - 摘要失败不影响请求
        return []
    if not raw:
        return []
    try:
        import json  # noqa: PLC0415 - 只在写操作摘要时用到

        payload = json.loads(raw)
    except Exception:  # noqa: BLE001
        return []
    if isinstance(payload, dict):
        return [str(key) for key in payload.keys()]
    return []


def _audit_after(request: Any, body_keys: list[str]) -> None:
    """写操作成功后落一条 ``admin_audit``。

    **任何异常都在这里吞掉**：业务已经成功（响应即将返回 200），
    绝不能因为"记日志失败"把它变成 500——那会让运营以为没生效然后重试，
    造成重复写。审计是旁路，不是关键路径。
    """
    state = getattr(request, "state", None)
    if state is not None and getattr(state, "audit_skip", False):
        return
    db = getattr(getattr(getattr(request, "app", None), "state", None), "v2", None)
    db = getattr(db, "db", None)
    if db is None:
        log.debug("操作日志跳过：数据库未就绪")
        return
    try:
        route = request.scope.get("route")
        route_path = str(getattr(route, "path", "") or "") or str(request.url.path)
        method = str(request.method or "").upper()
        path_params = dict(getattr(request, "path_params", None) or {})
        action = str(getattr(state, "audit_action", "") or "") or audit.derive_action(
            method, route_path
        )
        target = str(getattr(state, "audit_target", "") or "") or str(request.url.path)
        summary = str(getattr(state, "audit_summary", "") or "") or audit.summarize(
            method=method,
            route_path=route_path,
            path_params=path_params,
            body_keys=body_keys,
        )
        audit.write_audit(
            db,
            ip=client_host(request),
            action=action,
            target=target,
            summary=summary,
        )
    except Exception as exc:  # noqa: BLE001 - 审计失败不影响业务结果
        log.debug("操作日志写入失败（忽略）: %s", exc)


async def require_admin(request: Request) -> AsyncIterator[None]:
    """管理端统一守卫：鉴权 + CSRF + 写操作日志。

    用法（路由级挂一次，handler 里不用再写 ``_guard``）::

        router = APIRouter(
            prefix="/api/admin", dependencies=[Depends(require_admin)]
        )

    - 身份：Cookie 会话 → ``X-Admin-Token`` 头 → 未配口令时的本机直通；
    - CSRF：非 GET/HEAD/OPTIONS 必须同源 + 带 ``X-Admin-CSRF``；
      带合法 ``X-Admin-Token`` 或走本机直通的请求豁免（脚本/CLI 场景）；
    - 日志：写方法**成功返回**后自动记一条 ``admin_audit``；
      handler 可用 ``request.state.audit_summary`` / ``audit_target`` /
      ``audit_action`` 覆盖默认值，或 ``audit_skip = True`` 跳过。
    """
    identity = require_admin_identity(request)
    if identity == "cookie":
        # 只有 Cookie 身份需要 CSRF（token / 本机直通豁免）
        check_csrf(request)

    method = str(getattr(request, "method", "") or "").upper()
    if method in SAFE_METHODS:
        yield
        return

    body_keys = await _json_body_keys(request)
    try:
        yield
    except Exception:
        # handler 抛错（校验失败 / 500）不记日志：失败的写操作不该留在操作日志里
        raise
    _audit_after(request, body_keys)


# --------------------------------------------------------------------------- #
# 失败限流
# --------------------------------------------------------------------------- #
def _prune(fails: list[float], now: float) -> None:
    while fails and now - fails[0] > FAIL_WINDOW:
        fails.pop(0)


def check_rate(ip: str, *, now: float | None = None) -> tuple[bool, int]:
    now = now if now is not None else time.time()
    fails = _FAILS.setdefault(str(ip or "?"), [])
    _prune(fails, now)
    if len(fails) >= MAX_FAILS:
        return False, max(1, int(FAIL_WINDOW - (now - fails[0])))
    return True, 0


def record_fail(ip: str, *, now: float | None = None) -> None:
    now = now if now is not None else time.time()
    fails = _FAILS.setdefault(str(ip or "?"), [])
    _prune(fails, now)
    fails.append(now)


def reset_rate(ip: str) -> None:
    _FAILS.pop(str(ip or "?"), None)


# --------------------------------------------------------------------------- #
# 登录页
# --------------------------------------------------------------------------- #
def render_login_page(*, error: str = "", next_url: str = "/admin") -> str:
    err = (
        f'<div class="err">{html.escape(error)}</div>'
        if error
        else '<div class="hint">输入管理密码后进入控制台（登录状态保持 30 天）</div>'
    )
    token_hint = (
        '<div class="hint">未设置 SE_ADMIN_PASSWORD？可用旧的 ?token= 链接进入。</div>'
        if not password_configured()
        else ""
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<meta name="robots" content="noindex" />
<title>Sonder · 管理台登录</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
    background:linear-gradient(168deg,#eef1fb 0%,#f4f1fb 45%,#fdf3ef 100%); color:#232946;
    font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif; }}
  .card {{ width:min(92vw, 380px); background:#faf8ff; border:1px solid #d9dbe8; border-radius:18px;
    padding:30px 26px 24px; box-shadow:0 18px 60px rgba(60,70,120,.16); }}
  .logo {{ font-size:21px; font-weight:700; letter-spacing:.3px; }}
  .sub {{ margin:6px 0 22px; font-size:12.5px; color:#6c7090; }}
  label {{ display:block; font-size:12.5px; color:#4a4f6a; margin-bottom:8px; }}
  input {{ width:100%; padding:11px 12px; border-radius:10px; border:1px solid #c3c7db;
    background:#fff; color:#232946; font-size:14px; outline:none; }}
  input:focus {{ border-color:#4c5e8b; box-shadow:0 0 0 3px rgba(76,94,139,.14); }}
  button {{ width:100%; margin-top:16px; padding:11px 12px; border:0; border-radius:10px;
    background:#4c5e8b; color:#f9f8ff; font-size:14.5px; font-weight:650; cursor:pointer; }}
  button:active {{ opacity:.88; }}
  .err {{ margin:0 0 14px; padding:10px 12px; border-radius:10px; background:#ffdad6; color:#93000a;
    font-size:12.5px; line-height:1.5; }}
  .hint {{ margin:14px 0 0; font-size:12px; color:#767b95; line-height:1.6; }}
  @media (prefers-color-scheme:dark) {{
    body {{ background:linear-gradient(168deg,#191b28 0%,#15161f 42%,#1e1a24 100%); color:#f3f2f9; }}
    .card {{ background:#1b1d29; border-color:#2c3145; box-shadow:0 18px 60px rgba(0,0,0,.5); }}
    .sub {{ color:#9a9db8; }}
    label {{ color:#b9bcd0; }}
    input {{ background:#12141d; border-color:#333950; color:#f3f2f9; }}
  }}
</style></head>
<body>
  <form class="card" method="post" action="/admin/login">
    <div class="logo">Sonder</div>
    <div class="sub">运营控制台 · 用户 / AI 世界 / 捏人 / 系统</div>
    {err}
    <label for="password">管理密码</label>
    <input id="password" name="password" type="password" autocomplete="current-password"
      autofocus placeholder="请输入密码" />
    <input type="hidden" name="next" value="{html.escape(next_url or '/admin')}" />
    <button type="submit">登录</button>
    {token_hint}
  </form>
</body></html>"""


__all__ = [
    "COOKIE_NAME",
    "CSRF_HEADER",
    "LOOPBACK_HOSTS",
    "SAFE_METHODS",
    "SESSION_TTL",
    "admin_password",
    "admin_token",
    "authenticated",
    "check_csrf",
    "check_rate",
    "client_host",
    "cookie_ok",
    "csrf_header_ok",
    "csrf_token",
    "header_token_ok",
    "loopback_passthrough",
    "make_session",
    "password_configured",
    "password_ok",
    "record_fail",
    "render_login_page",
    "require_admin",
    "require_admin_identity",
    "reset_rate",
    "same_origin",
    "token_configured",
    "token_ok",
    "verify_session",
]
