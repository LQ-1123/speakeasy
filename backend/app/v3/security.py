"""SpeakEasy V3 · 身份鉴权（会话凭据 + 旧设备身份兼容）。

身份解析顺序
------------
1. ``X-Session-Token`` 请求头（Android/脚本）或 ``Authorization: Bearer <token>``
   → 查 :mod:`app.v3.accounts` 的可撤销会话表；
2. ``sonder_session`` HttpOnly Cookie（Web）
   → 同样走会话表；**非安全方法必须带 ``X-CSRF-Token`` 且与 ``sonder_csrf`` 一致**；
3. 旧 ``X-User-Id`` + ``X-User-Token``（未升级的设备身份）
   → 该身份一旦绑定邮箱账号就拒绝（401），必须走登录；
4. 什么头都没有 → ``local``（兼容老客户端/本地单用户），
   但生产部署设 ``SE_REQUIRE_AUTH=1`` 后直接 401，不再隐式降级。

安全取舍
--------
- token 只存 ``sha256(token)``，注册/登录时明文只返回一次。
- 带 id 却缺 token → 401；格式非法 → 400；这样改参数无法越权。
- 会话可撤销、可过期；改密码/找回密码会撤销全部会话。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import secrets
import time
import uuid
from contextvars import ContextVar
from typing import Any

from fastapi import HTTPException, Request

from app.logging_utils import get_logger
from app.v3.auth_policy import email_code_auth_enabled, require_legacy_auth
from app.v3.identity import LOCAL_USER_ID, is_valid_user_id, set_current_user

log = get_logger(__name__)

#: ``/api/v3/auth/register`` 幂等键
_DEVICE_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

#: 当前请求使用的 DB（由 :func:`bind_current_user` 写入，供序列化层签媒体 URL）
_REQUEST_DB: ContextVar[Any | None] = ContextVar("request_db", default=None)


def current_request_db() -> Any | None:
    """取当前请求的 DB（没有请求上下文时返回 ``None``）。"""
    return _REQUEST_DB.get()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


def require_auth() -> bool:
    """生产环境打开后，无凭据请求不再降级成 local。"""
    return str(os.getenv("SE_REQUIRE_AUTH", "0")).strip().lower() in ("1", "true", "yes")


def _accounts() -> Any:
    """延迟导入，避免 security ⇄ accounts 的循环依赖。"""
    from app.v3 import accounts  # noqa: PLC0415

    return accounts


def register_user(db: Any, device_key: str | None, display_name: str = "") -> dict:
    """注册或取回设备身份（旧客户端兼容路径）。

    幂等：同一个 ``device_key`` 永远返回同一条用户记录。但**一旦该身份绑定邮箱账号**，
    设备身份路径立即关闭：不再签发 token，返回 401 要求登录。
    """
    require_legacy_auth()
    key = (device_key or "").strip()
    if key and not _DEVICE_KEY_RE.match(key):
        raise HTTPException(status_code=400, detail="device_key 格式不合法")

    if key:
        accounts = _accounts()
        with db._lock:  # noqa: SLF001 - 与 DB 层保持同一把锁
            existing = db.conn.execute(
                "SELECT id FROM v3_users WHERE device_key=?", (key,)
            ).fetchone()
        if existing is not None and accounts.has_account(db, str(existing["id"])):
            raise HTTPException(status_code=401, detail="该设备已绑定账号，请使用邮箱登录")

        with db._lock, db.conn:  # noqa: SLF001
            row = db.conn.execute(
                "SELECT id, display_name, token_hash FROM v3_users WHERE device_key=?", (key,)
            ).fetchone()
            if row is not None:
                if not str(row["token_hash"] or ""):
                    # 账号体系创建的行（token_hash 为空）：设备路径不可用
                    raise HTTPException(
                        status_code=401, detail="该设备已绑定账号，请使用邮箱登录"
                    )
                token = secrets.token_hex(24)
                db.conn.execute(
                    "UPDATE v3_users SET token_hash=?, last_seen_at=? WHERE id=?",
                    (_hash_token(token), _now(), row["id"]),
                )
                return {
                    "user_id": row["id"],
                    "token": token,
                    "display_name": row["display_name"] or "",
                    "created": False,
                }

        user_id = "u_" + uuid.uuid4().hex
        token = secrets.token_hex(24)
        name = (display_name or "").strip()[:40]
        db.conn.execute(
            "INSERT INTO v3_users (id, device_key, token_hash, display_name, created_at, last_seen_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, key or None, _hash_token(token), name, _now(), _now()),
        )
    else:
        with db._lock, db.conn:  # noqa: SLF001
            user_id = "u_" + uuid.uuid4().hex
            token = secrets.token_hex(24)
            name = (display_name or "").strip()[:40]
            db.conn.execute(
                "INSERT INTO v3_users (id, device_key, token_hash, display_name, created_at, last_seen_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, None, _hash_token(token), name, _now(), _now()),
            )
    log.info("v3 新用户注册（设备身份）: %s", user_id)
    return {"user_id": user_id, "token": token, "display_name": name, "created": True}


def authenticate(db: Any, user_id: str, token: str) -> bool:
    """校验旧设备身份：用户 id 与设备 token 是否匹配。"""
    if email_code_auth_enabled() or not user_id or not token:
        return False
    from app.v3.account_archive import user_is_available

    if not user_is_available(db, user_id):
        return False
    if user_id == LOCAL_USER_ID:
        return False  # local 身份不接受带 token 的远程声明
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT token_hash FROM v3_users WHERE id=?", (user_id,)
        ).fetchone()
    if row is None:
        return False
    expected = str(row["token_hash"] or "")
    return bool(expected) and hmac.compare_digest(expected, _hash_token(token))


def _session_from_request(request: Request) -> tuple[str, bool]:
    """返回 ``(token, from_cookie)``；没有则空串。"""
    header = (request.headers.get("x-session-token") or "").strip()
    if not header:
        auth = (request.headers.get("authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            header = auth[7:].strip()
    if header:
        return header, False
    cookie = str(request.cookies.get(_accounts().SESSION_COOKIE) or "").strip()
    return (cookie, True) if cookie else ("", False)


def _csrf_ok(request: Request) -> bool:
    accounts = _accounts()
    expected = str(request.cookies.get(accounts.CSRF_COOKIE) or "")
    supplied = str(request.headers.get(accounts.CSRF_HEADER) or "")
    return bool(expected) and bool(supplied) and hmac.compare_digest(expected, supplied)


async def bind_current_user(request: Request) -> str:
    """FastAPI 依赖：解析并绑定当前请求用户。"""
    services = getattr(request.app.state, "v2", None)
    db = getattr(services, "db", None)
    # 供序列化层（消息/动态图片 URL 签名）取库；同一 async 任务内可用
    _REQUEST_DB.set(db)

    token, from_cookie = _session_from_request(request)
    if token:
        if db is None:  # pragma: no cover - 装配异常
            raise HTTPException(status_code=503, detail="服务未初始化")
        accounts = _accounts()
        # CSRF 不通过的请求不更新活跃时间；仍保持「会话无效 401 优先于 CSRF 403」
        if from_cookie and request.method not in _SAFE_METHODS and not _csrf_ok(request):
            session = await asyncio.to_thread(accounts.authenticate_session, db, token)
            if session is None:
                raise HTTPException(status_code=401, detail="会话已失效，请重新登录")
            raise HTTPException(status_code=403, detail="CSRF 校验失败")
        # 校验 + 活跃时间合并成一次线程切换（每个带登录态的请求都走这里）
        session = await asyncio.to_thread(accounts.authenticate_and_touch, db, token)
        if session is None:
            raise HTTPException(status_code=401, detail="会话已失效，请重新登录")
        user_id = str(session["user_id"])
        request.state.session_id = str(session["session_id"])
        set_current_user(user_id)
        return user_id

    raw_id = (request.headers.get("x-user-id") or "").strip()
    if email_code_auth_enabled():
        raise HTTPException(status_code=401, detail="请使用邮箱验证码登录")
    if not raw_id:
        if require_auth():
            raise HTTPException(status_code=401, detail="未登录")
        set_current_user(LOCAL_USER_ID)
        return LOCAL_USER_ID

    if not is_valid_user_id(raw_id):
        raise HTTPException(status_code=400, detail="X-User-Id 格式不合法")

    if db is None:  # pragma: no cover - 装配异常
        raise HTTPException(status_code=503, detail="服务未初始化")

    # 绑定过邮箱账号的身份，禁止再用设备 token 访问
    if await asyncio.to_thread(_accounts().has_account, db, raw_id):
        raise HTTPException(status_code=401, detail="该账号已启用邮箱登录，请重新登录")

    token_value = (request.headers.get("x-user-token") or "").strip()
    ok = await asyncio.to_thread(authenticate, db, raw_id, token_value)
    if not ok:
        raise HTTPException(status_code=401, detail="身份校验失败，请重新登录")

    set_current_user(raw_id)
    # 更新活跃时间（低频写，失败不影响请求）
    try:
        def touch():
            with db._lock, db.conn:  # noqa: SLF001
                db.conn.execute(
                    "UPDATE v3_users SET last_seen_at=? WHERE id=?"
                    " AND (last_seen_at IS NULL OR datetime(last_seen_at)<datetime('now','-5 minutes'))",
                    (_now(), raw_id),
                )
        await asyncio.to_thread(touch)
    except Exception:  # noqa: BLE001
        pass
    return raw_id


__all__ = [
    "authenticate",
    "bind_current_user",
    "current_request_db",
    "register_user",
    "require_auth",
]
