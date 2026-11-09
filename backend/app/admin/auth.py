"""管理台登录态：密码登录 + 签名 Cookie（兼容原 token 链接）。

- 密码来自环境变量 ``SE_ADMIN_PASSWORD``；未设置时仍可用旧 ``?token=`` 链接/请求头。
- 登录成功后写 HttpOnly Cookie（默认 30 天），``/api/admin/*`` 自动认 Cookie，
  管理页 JS 不用再在请求头里带 token。
- Cookie 值 = ``v1.<过期时间戳>.<HMAC-SHA256 前 40 位>``，密钥由
  ``SE_ADMIN_SESSION_SECRET``（未设则由密码+token 派生）决定；
  改密码后旧会话自动失效。
- 暴力破解：同一 IP 连续 5 次失败锁 5 分钟（进程内，重启清零）。
"""

from __future__ import annotations

import hashlib
import hmac
import html
import os
import secrets
import time
from typing import Any

COOKIE_NAME = "se_admin_session"
SESSION_TTL = 30 * 24 * 3600
_FAILS: dict[str, list[float]] = {}
MAX_FAILS = 5
FAIL_WINDOW = 300.0


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
<title>SpeakEasy · 管理台登录</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
    background:#0e1116; color:#e8edf4;
    font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif; }}
  .card {{ width:min(92vw, 380px); background:#141a22; border:1px solid #223041; border-radius:16px;
    padding:28px 26px 24px; box-shadow:0 18px 60px rgba(0,0,0,.45); }}
  .logo {{ font-size:20px; font-weight:700; letter-spacing:.4px; }}
  .sub {{ margin:6px 0 20px; font-size:12.5px; color:#8b98a8; }}
  label {{ display:block; font-size:12.5px; color:#aab6c6; margin-bottom:8px; }}
  input {{ width:100%; padding:11px 12px; border-radius:10px; border:1px solid #2b3a4d;
    background:#0f151d; color:#e8edf4; font-size:14px; outline:none; }}
  input:focus {{ border-color:#3aae7d; }}
  button {{ width:100%; margin-top:16px; padding:11px 12px; border:0; border-radius:10px;
    background:#3aae7d; color:#08150f; font-size:14.5px; font-weight:700; cursor:pointer; }}
  button:active {{ opacity:.85; }}
  .err {{ margin:0 0 14px; padding:10px 12px; border-radius:10px; background:#3a1d22; color:#ff9aa6;
    font-size:12.5px; line-height:1.5; }}
  .hint {{ margin:14px 0 0; font-size:12px; color:#77869a; line-height:1.55; }}
</style></head>
<body>
  <form class="card" method="post" action="/admin/login">
    <div class="logo">SpeakEasy · 管理台</div>
    <div class="sub">词库 · 捏人素材 · 音色 · 角色 · APK</div>
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
    "SESSION_TTL",
    "admin_password",
    "admin_token",
    "authenticated",
    "check_rate",
    "cookie_ok",
    "header_token_ok",
    "make_session",
    "password_configured",
    "password_ok",
    "record_fail",
    "render_login_page",
    "reset_rate",
    "token_configured",
    "token_ok",
    "verify_session",
]
