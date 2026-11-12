"""V3 · 账号 HTTP 接口（``/api/v3/auth/*``）。

设计要点
--------
- 邮箱 + 密码登录；会话凭据可撤销（``v3_sessions``）。
- 同时支持两种凭据：
  * 请求头 ``X-Session-Token``（Android/脚本，沿用现有加密身份存储）；
  * HttpOnly Cookie ``sonder_session``（Web），写操作必须带 ``X-CSRF-Token``。
- 旧设备身份仍可 ``POST /auth/bind-legacy`` 升级为邮箱账号（保留好友/聊天/动态）。
- 一切失败都用明确的 HTTP 状态码：401 未登录/凭据错，403 权限（邀请码/停用），
  409 冲突（邮箱已注册），422 参数不合法。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.logging_utils import get_logger
from app.v3 import accounts, email_codes
from app.v3.auth_policy import email_code_auth_enabled, require_legacy_auth
from app.v3.identity import LOCAL_USER_ID, current_user_id
from app.v3.security import _session_from_request, authenticate, bind_current_user

log = get_logger(__name__)

router = APIRouter(prefix="/api/v3/auth", tags=["auth"])


def _db(request: Request) -> Any:
    services = getattr(request.app.state, "v2", None)
    db = getattr(services, "db", None)
    if db is None:  # pragma: no cover - 装配异常
        raise HTTPException(status_code=503, detail="数据库未初始化")
    return db


async def _run(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


def _set_auth_cookies(response: Response, request: Request, session: dict, user_id: str) -> str:
    """写会话 Cookie + CSRF 双提交 Cookie，返回 csrf token。"""
    token = str(session.get("session_token") or "")
    csrf = accounts.new_csrf_token()
    secure = (request.url.scheme == "https") or (
        str(request.headers.get("x-forwarded-proto") or "").lower() == "https"
    )
    max_age = 30 * 24 * 3600
    response.set_cookie(
        accounts.SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        accounts.CSRF_COOKIE,
        csrf,
        max_age=max_age,
        httponly=False,
        secure=secure,
        samesite="lax",
        path="/",
    )
    response.headers["X-Session-Expires"] = str(session.get("expires_at") or "")
    return csrf


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(accounts.SESSION_COOKIE, path="/")
    response.delete_cookie(accounts.CSRF_COOKIE, path="/")


# --------------------------------------------------------------------------- #
# 请求体
# --------------------------------------------------------------------------- #
class EmailRegisterIn(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=8, max_length=128)
    invite_code: str = Field(default="", max_length=64)
    display_name: str = Field(default="", max_length=40)
    device_key: str = Field(default="", max_length=128)
    platform: str = Field(default="", max_length=32)


class LoginIn(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=1, max_length=128)
    device_key: str = Field(default="", max_length=128)
    platform: str = Field(default="", max_length=32)


class BindLegacyIn(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=8, max_length=128)
    invite_code: str = Field(default="", max_length=64)


class TokenIn(BaseModel):
    token: str = Field(min_length=8, max_length=256)


class EmailOnlyIn(BaseModel):
    email: str = Field(min_length=5, max_length=254)


class ResetIn(BaseModel):
    token: str = Field(min_length=8, max_length=256)
    password: str = Field(min_length=8, max_length=128)


class ChangePasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class EmailCodeVerifyIn(BaseModel):
    challenge_id: str = Field(min_length=1, max_length=128)
    code: str = Field(max_length=128)
    invite_code: str = Field(default="", max_length=64)
    device_key: str = Field(default="", max_length=128)
    platform: str = Field(default="web", max_length=32)


def _code_error(exc: email_codes.CodeError) -> HTTPException:
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
    return HTTPException(status_code=exc.status, detail=exc.detail, headers=headers)


def _require_email_code_mode() -> None:
    if not email_code_auth_enabled():
        raise HTTPException(status_code=404, detail="邮箱验证码登录未启用")


@router.get("/config")
async def auth_config() -> dict:
    return {"email_code_auth": email_code_auth_enabled(), "invite_required": accounts.invite_required()}


@router.post("/email-code/request")
async def request_email_code(body: EmailOnlyIn, request: Request) -> dict:
    _require_email_code_mode()
    # Forwarded headers are deliberately ignored. Configure the ASGI server's
    # trusted proxy list if a deployment needs the original peer address.
    ip = request.client.host if request.client else "unknown"
    try:
        result = await _run(email_codes.request_code, _db(request), body.email, ip)
    except email_codes.CodeError as exc:
        log.info("auth_email_code_request outcome=failure reason=%s status=%s", exc.detail["code"], exc.status)
        raise _code_error(exc) from None
    log.info("auth_email_code_request outcome=success")
    return result


@router.post("/email-code/verify")
async def verify_email_code(body: EmailCodeVerifyIn, request: Request, response: Response) -> dict:
    _require_email_code_mode()
    try:
        result = await _run(
            email_codes.verify_code, _db(request), body.challenge_id, body.code,
            invite_code=body.invite_code, device_key=body.device_key, platform=body.platform,
        )
    except email_codes.CodeError as exc:
        log.info("auth_email_code_verify outcome=failure reason=%s status=%s", exc.detail["code"], exc.status)
        raise _code_error(exc) from None
    log.info("auth_email_code_verify outcome=success created=%s user_id=%s", result["created"], result["user_id"])
    response.headers["Cache-Control"] = "no-store"
    if body.platform.lower() == "android":
        result["csrf_token"] = ""
    else:
        result["csrf_token"] = _set_auth_cookies(response, request, result, result["user_id"])
        result.pop("session_token", None)
    return result


# --------------------------------------------------------------------------- #
# 注册 / 登录 / 退出
# --------------------------------------------------------------------------- #
@router.post("/register/email")
async def register_email(body: EmailRegisterIn, request: Request, response: Response) -> dict:
    require_legacy_auth()
    db = _db(request)
    try:
        result = await _run(
            accounts.register_email,
            db,
            body.email,
            body.password,
            invite_code=body.invite_code,
            display_name=body.display_name,
            device_key=body.device_key,
            platform=body.platform,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except ValueError as exc:
        message = str(exc)
        status = 409 if "已被注册" in message else 400
        raise HTTPException(status_code=status, detail=message) from None
    result["csrf_token"] = _set_auth_cookies(response, request, result, result["user_id"])
    return result


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response) -> dict:
    require_legacy_auth()
    db = _db(request)
    try:
        result = await _run(
            accounts.login, db, body.email, body.password,
            device_key=body.device_key, platform=body.platform,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    result["csrf_token"] = _set_auth_cookies(response, request, result, result["user_id"])
    return result


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    user_id: str = Depends(bind_current_user),
) -> dict:
    """撤销当前会话；无 Cookie 时凭 ``X-Session-Token`` 头撤销对应会话。"""
    db = _db(request)
    token, _ = _session_from_request(request)
    if token:
        session = await _run(accounts.authenticate_session, db, token)
        if session and session["user_id"] == user_id:
            await _run(accounts.revoke_session, db, user_id, session["session_id"])
    _clear_auth_cookies(response)
    return {"ok": True}


@router.post("/logout-all")
async def logout_all(
    request: Request,
    response: Response,
    user_id: str = Depends(bind_current_user),
) -> dict:
    db = _db(request)
    revoked = await _run(accounts.revoke_user_sessions, db, user_id)
    _clear_auth_cookies(response)
    return {"ok": True, "revoked": int(revoked)}


# --------------------------------------------------------------------------- #
# 邮箱验证 / 找回密码
# --------------------------------------------------------------------------- #
@router.post("/verify-email")
async def verify_email(body: TokenIn, request: Request) -> dict:
    require_legacy_auth()
    db = _db(request)
    try:
        user_id = await _run(accounts.verify_email, db, body.token)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"ok": True, "user_id": user_id}


@router.post("/resend-verification")
async def resend_verification(
    request: Request, user_id: str = Depends(bind_current_user)
) -> dict:
    require_legacy_auth()
    db = _db(request)
    try:
        token = await _run(accounts.resend_verification, db, user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    payload: dict = {"ok": True}
    if accounts.mail_dev_mode():
        payload["dev_verification_token"] = token
    return payload


@router.post("/password/forgot")
async def forgot_password(body: EmailOnlyIn, request: Request) -> dict:
    """无论邮箱是否存在都返回 ok（防枚举）；dev 模式下发令牌便于联调。"""
    require_legacy_auth()
    db = _db(request)
    token = await _run(accounts.request_password_reset, db, body.email)
    payload: dict = {"ok": True}
    if token and accounts.mail_dev_mode():
        payload["dev_reset_token"] = token
    return payload


@router.post("/password/reset")
async def reset_password(body: ResetIn, request: Request, response: Response) -> dict:
    require_legacy_auth()
    db = _db(request)
    try:
        user_id = await _run(accounts.reset_password, db, body.token, body.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    _clear_auth_cookies(response)
    return {"ok": True, "user_id": user_id, "sessions_revoked": True}


@router.post("/password/change")
async def change_password(
    body: ChangePasswordIn, request: Request, response: Response,
    user_id: str = Depends(bind_current_user),
) -> dict:
    require_legacy_auth()
    db = _db(request)
    try:
        await _run(accounts.change_password, db, user_id, body.old_password, body.new_password)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    _clear_auth_cookies(response)
    return {"ok": True, "sessions_revoked": True}


# --------------------------------------------------------------------------- #
# 旧设备身份绑定
# --------------------------------------------------------------------------- #
@router.post("/bind-legacy")
async def bind_legacy(
    body: BindLegacyIn, request: Request, response: Response
) -> dict:
    """用 ``X-User-Id`` + ``X-User-Token`` 证明设备身份归属后绑定邮箱。

    绑定成功后设备 token 立即作废（``register_user`` 不再为该身份签发），
    后续一律走会话凭据。
    """
    require_legacy_auth()
    db = _db(request)
    raw_id = (request.headers.get("x-user-id") or "").strip()
    raw_token = (request.headers.get("x-user-token") or "").strip()
    if not raw_id or not raw_token:
        raise HTTPException(status_code=401, detail="需要旧的设备身份头")
    if raw_id == LOCAL_USER_ID:
        raise HTTPException(status_code=403, detail="local 身份不能绑定账号")
    ok = await _run(authenticate, db, raw_id, raw_token)
    if not ok:
        raise HTTPException(status_code=401, detail="设备身份校验失败")
    try:
        result = await _run(
            accounts.bind_legacy_user, db, raw_id, body.email, body.password,
            invite_code=body.invite_code,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except ValueError as exc:
        message = str(exc)
        status = 409 if "已被注册" in message else 400
        raise HTTPException(status_code=status, detail=message) from None
    # 设备 token 立即失效：清空哈希，设备身份路径彻底关闭
    with db._lock, db.conn:  # noqa: SLF001 - 一次性收口
        db.conn.execute("UPDATE v3_users SET token_hash='' WHERE id=?", (raw_id,))
    result["csrf_token"] = _set_auth_cookies(response, request, result, raw_id)
    result["device_identity_disabled"] = True
    return result


# --------------------------------------------------------------------------- #
# 会话管理 / 能力
# --------------------------------------------------------------------------- #
@router.get("/sessions")
async def sessions(request: Request, user_id: str = Depends(bind_current_user)) -> dict:
    db = _db(request)
    items = await _run(accounts.list_sessions, db, user_id)
    current = getattr(request.state, "session_id", "")
    for item in items:
        item["current"] = item["session_id"] == current
    return {"items": items}


@router.delete("/sessions/{session_id}")
async def revoke_session(
    session_id: str, request: Request, user_id: str = Depends(bind_current_user)
) -> dict:
    db = _db(request)
    ok = await _run(accounts.revoke_session, db, user_id, session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="会话不存在或已撤销")
    return {"ok": True, "session_id": session_id}


@router.get("/capabilities")
async def capabilities(
    request: Request, user_id: str = Depends(bind_current_user)
) -> dict:
    db = _db(request)
    return await _run(accounts.capabilities, db, user_id)


@router.get("/identity")
async def identity(request: Request, response: Response, user_id: str = Depends(bind_current_user)) -> dict:
    """当前请求身份的解析结果（排查 401/降级 local 时使用）。"""
    db = _db(request)
    response.headers["Cache-Control"] = "private, no-store"
    account = await _run(accounts.account_for_user, db, user_id)
    token, cookie_auth = _session_from_request(request)
    session = await _run(accounts.authenticate_session, db, token) if token else None
    expires_at = str((session or {}).get("expires_at") or "")
    return {
        "user_id": user_id,
        "resolved_by": "session" if (token or cookie_auth) else "device_header",
        "has_account": account is not None,
        "email": str((account or {}).get("email") or ""),
        "email_verified": bool((account or {}).get("email_verified_at")),
        "expires_at": expires_at,
        "csrf_token": str(request.cookies.get(accounts.CSRF_COOKIE) or "") if cookie_auth else "",
        "current_user_context": current_user_id(),
    }
