"""SpeakEasy V3 · 设备身份注册与请求鉴权。

- ``register_user``：设备首启注册（幂等：同一 ``device_key`` 返回同一 user_id/token）
- ``authenticate``：校验 ``X-User-Id`` + ``X-User-Token``
- ``bind_current_user``：FastAPI 依赖，把请求用户写进 contextvar

安全取舍
--------
- token 只存 ``sha256(token)``，注册时明文返回一次。
- 不带 ``X-User-Id`` 的请求一律视为本地用户 ``local``（兼容 V2 客户端/测试），
  但**一旦带 id 就必须带对的 token**，否则 401——这样改请求参数无法越权。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
import time
import uuid
from typing import Any

from fastapi import HTTPException, Request

from app.logging_utils import get_logger
from app.v3.identity import LOCAL_USER_ID, is_valid_user_id, set_current_user

log = get_logger(__name__)

#: ``/api/v3/auth/register`` 幂等键
_DEVICE_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


def register_user(db: Any, device_key: str | None, display_name: str = "") -> dict:
    """注册或取回设备身份。

    幂等：同一个 ``device_key`` 永远返回同一条用户记录；但 token 只在首次返回，
    之后由客户端自己保存（丢失则重新注册）。
    """
    key = (device_key or "").strip()
    if key and not _DEVICE_KEY_RE.match(key):
        raise HTTPException(status_code=400, detail="device_key 格式不合法")

    with db._lock, db.conn:  # noqa: SLF001 - 与 DB 层保持同一把锁
        if key:
            row = db.conn.execute(
                "SELECT id, display_name FROM v3_users WHERE device_key=?", (key,)
            ).fetchone()
            if row is not None:
                # 已注册过：重新签发 token（客户端本地丢失时可恢复），并更新活跃时间
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
    log.info("v3 新用户注册: %s", user_id)
    return {"user_id": user_id, "token": token, "display_name": name, "created": True}


def authenticate(db: Any, user_id: str, token: str) -> bool:
    """校验用户 id 与 token 是否匹配。"""
    if not user_id or not token:
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


async def bind_current_user(request: Request) -> str:
    """FastAPI 依赖：解析并绑定当前请求用户。

    - 无 ``X-User-Id`` → ``local``（老客户端）
    - 有 id 但 token 缺失/不匹配 → 401
    """
    raw_id = (request.headers.get("x-user-id") or "").strip()
    if not raw_id:
        set_current_user(LOCAL_USER_ID)
        return LOCAL_USER_ID

    if not is_valid_user_id(raw_id):
        raise HTTPException(status_code=400, detail="X-User-Id 格式不合法")

    services = getattr(request.app.state, "v2", None)
    db = getattr(services, "db", None)
    if db is None:  # pragma: no cover - 装配异常
        raise HTTPException(status_code=503, detail="服务未初始化")

    token = (request.headers.get("x-user-token") or "").strip()
    ok = await asyncio.to_thread(authenticate, db, raw_id, token)
    if not ok:
        raise HTTPException(status_code=401, detail="身份校验失败，请重新登录")

    set_current_user(raw_id)
    # 更新活跃时间（低频写，失败不影响请求）
    try:
        await asyncio.to_thread(
            lambda: db.conn.execute(
                "UPDATE v3_users SET last_seen_at=? WHERE id=?", (_now(), raw_id)
            )
        )
    except Exception:  # noqa: BLE001
        pass
    return raw_id


__all__ = ["authenticate", "bind_current_user", "register_user"]
