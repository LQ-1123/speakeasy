"""SpeakEasy V3 · 多用户身份（ContextVar）。

设计
----
- **本地单用户模式**：请求不带 ``X-User-Id`` → 当前用户固定为 ``local``，
  行为与 V2 完全一致（老数据、老前端、老测试都不受影响）。
- **多用户 / 云端模式**：前端首次启动调 ``POST /api/v3/auth/register`` 拿到
  ``(user_id, token)``，之后每个请求带 ``X-User-Id`` + ``X-User-Token``；
  服务端校验 token 与 user_id 的绑定关系（token 只存 sha256）。
- 数据层（``app/v2/db.py`` 与 ``app/v3/service.py``）通过 :func:`current_user_id`
  读取「当前请求用户」，从而把好友关系、动态、社交事件按 user_id 隔离开。

注意
----
- 这里只放**上下文**，不放校验逻辑（校验需要 DB，见 ``security.py``），避免循环依赖。
- ``asyncio.to_thread`` / ``create_task`` 会复制 contextvars，因此数据库调用与后台任务
  都能拿到同一个请求用户。
"""

from __future__ import annotations

import re
from contextvars import ContextVar, Token

#: 不带身份头时使用的本地用户 id（与老库数据的默认 owner 一致）
LOCAL_USER_ID = "local"

#: 合法用户 id：3–64 位，字母数字开头，允许 ``_ . : -``
_USER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,63}$")

_user_var: ContextVar[str] = ContextVar("speakeasy_user_id", default=LOCAL_USER_ID)


def current_user_id() -> str:
    """当前请求用户 id（默认 ``local``）。"""
    return _user_var.get() or LOCAL_USER_ID


def set_current_user(user_id: str) -> Token:
    """绑定当前上下文用户，返回可用于还原的 token。"""
    return _user_var.set(user_id or LOCAL_USER_ID)


def reset_current_user(token: Token) -> None:
    try:
        _user_var.reset(token)
    except ValueError:  # pragma: no cover - 跨上下文还原时忽略
        pass


def is_valid_user_id(user_id: str) -> bool:
    return bool(user_id) and bool(_USER_ID_RE.match(user_id))


__all__ = [
    "LOCAL_USER_ID",
    "current_user_id",
    "is_valid_user_id",
    "reset_current_user",
    "set_current_user",
]
