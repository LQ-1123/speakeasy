"""管理端操作日志：所有写操作留痕（``admin_audit`` 表）。

三条原则
--------
1. **摘要绝不记录密钥明文**：自动摘要只写"哪些字段被改"（字段名），值一律丢弃；
   即便调用方自己传了 summary，落库前也会再过一遍 ``redact()`` 兜底。
2. **日志是旁路**：写库失败（表不存在 / 库只读）只打 warning，不影响业务响应。
3. **不引入迁移框架**：表结构由 ``app.v3.schema.DDL`` 用 ``CREATE TABLE IF NOT EXISTS``
   建（``ensure_schema`` 幂等执行），本模块只负责读写。

写入时机由 ``app.admin.auth.require_admin`` 依赖在 handler 成功返回后触发，
路由不需要在每个 handler 里手写一行。
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from app.logging_utils import get_logger

log = get_logger(__name__)

#: 这些字样的字段只记名不记值（真正保证"值不落库"靠的是只写字段名，这里是兜底）
SECRET_HINTS = (
    "key",
    "secret",
    "token",
    "password",
    "passwd",
    "pwd",
    "credential",
    "authorization",
    "cookie",
    "session",
)

#: 常见密钥形状（防调用方把值塞进 summary）
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:sk|dg|pk|rk|ak|api)[-_][A-Za-z0-9_\-]{8,}"), "***"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password|passwd|authorization)\b\s*[=:]\s*\S+"
        ),
        r"\1=***",
    ),
)

#: 写方法 → 动作动词
_WRITE_VERBS = {"POST": "create", "PUT": "update", "PATCH": "patch", "DELETE": "delete"}

_SECRET_RE_KW = re.compile("|".join(SECRET_HINTS), re.IGNORECASE)

#: 兜底建表（正常路径由 ``app.v3.schema.ensure_schema`` 建；这里只在表缺失时补一次，
#: 保证「先起服务、后建 schema」或测试自建 DB 时操作日志不会静默丢失）
_AUDIT_DDL = (
    "CREATE TABLE IF NOT EXISTS admin_audit ("
    " id TEXT PRIMARY KEY,"
    " at TEXT NOT NULL,"
    " ip TEXT NOT NULL DEFAULT '',"
    " action TEXT NOT NULL,"
    " target TEXT NOT NULL DEFAULT '',"
    " summary TEXT NOT NULL DEFAULT '')",
    "CREATE INDEX IF NOT EXISTS idx_admin_audit_at ON admin_audit(at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_admin_audit_action"
    " ON admin_audit(action, at DESC)",
)

#: 已确认建过表的 DB（按 id 记录，避免每次写日志都试建）
_ENSURED: set[int] = set()


def ensure_audit_table(db: Any) -> bool:
    """兜底建 ``admin_audit``（幂等）；返回表是否可用。"""
    conn = getattr(db, "conn", None)
    if conn is None:
        return False
    key = id(db)
    if key in _ENSURED:
        return True
    lock = getattr(db, "_lock", None)  # noqa: SLF001
    try:
        if lock is not None:
            with lock, conn:
                for statement in _AUDIT_DDL:
                    conn.execute(statement)
        else:  # pragma: no cover - 非标准 DB 壳
            with conn:
                for statement in _AUDIT_DDL:
                    conn.execute(statement)
    except sqlite3.Error as exc:
        log.warning("admin_audit 建表失败（忽略）: %s", exc)
        return False
    _ENSURED.add(key)
    return True


def reset_ensure_cache() -> None:
    """清空「已建表」缓存（测试用）。"""
    _ENSURED.clear()


# --------------------------------------------------------------------------- #
# 摘要与脱敏
# --------------------------------------------------------------------------- #
def is_secret_field(name: str) -> bool:
    """字段名是否像密钥（决定摘要里要不要额外标注）。"""
    return bool(_SECRET_RE_KW.search(str(name or "")))


def redact(text: str) -> str:
    """把看上去像密钥的值打码；摘要落库前的最后一道闸。"""
    value = str(text or "")
    for pattern, replacement in _SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def route_segments(route_path: str) -> list[str]:
    """``/api/admin/config/{target}`` → ``['config']``（去掉 api/admin 前缀与 {} 占位段）。"""
    segments = [
        segment
        for segment in str(route_path or "").split("/")
        if segment and not segment.startswith("{")
    ]
    if segments and segments[0] == "api":
        segments = segments[1:]
    if segments and segments[0] == "admin":
        segments = segments[1:]
    return segments


def derive_action(method: str, route_path: str) -> str:
    """由 HTTP 方法与路由模板派生动作名，如 ``config.update`` / ``personas.rebuild``。

    只有静态路径段参与命名：``{persona_id}`` 这类占位段跳过，
    所以同一路由的不同取值会归到同一个 action，便于按动作检索。
    """
    verb = _WRITE_VERBS.get(str(method or "").upper(), str(method or "").lower())
    segments = route_segments(route_path)
    if len(segments) >= 2:
        # 有二级静态段（rebuild / validate / test…）就用它当动词
        return f"{segments[0]}.{segments[-1]}"
    if segments:
        return f"{segments[0]}.{verb}"
    return f"admin.{verb}"


def summarize(
    *,
    method: str,
    route_path: str,
    path_params: dict[str, Any] | None = None,
    body_keys: Iterable[str] = (),
) -> str:
    """生成默认摘要：**只写字段名，不写值**。

    - 配置类（``/api/admin/config/*``）：``更新 llm 配置：base_url, model, api_key``
    - 其它写操作：``PUT /api/admin/lexicons/{name}（字段：data）``

    这是"配置保存的 summary 不含密钥明文"的结构性保证：值从不进入摘要。
    """
    params = dict(path_params or {})
    keys = [str(key) for key in body_keys]
    segments = route_segments(route_path)
    if segments and segments[0] == "config":
        target = str(params.get("target") or "").strip()
        prefix = f"更新 {target} 配置" if target else "更新配置"
        return f"{prefix}：{', '.join(keys)}" if keys else prefix
    if keys:
        return f"{method} {route_path}（字段：{', '.join(keys)}）"
    return f"{method} {route_path}"


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    """带毫秒的 UTC 时间戳。

    为什么不是秒级：``list_audit`` 用 ``ORDER BY at DESC, id DESC`` 排序，
    而管理台一次操作常在同一秒内产生多条写（比如"发布动态"= 入队 + 改状态）。
    秒级精度下这些记录时间完全相同，先后只能靠随机 uuid 打破——
    排障时看到"同一秒里三条、顺序随机"会误导判断。

    兼容性：老记录是 ``...:12+00:00``，新记录是 ``...:12.345+00:00``；
    两者都能被前端的时间裁剪正则匹配，字典序对 DESC 排序也正确
    （同秒内 ``.`` 排在 ``+`` 之后，新记录排在前面）。
    """
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}+00:00"


def _clip(value: Any, limit: int) -> str:
    text = str(value if value is not None else "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def write_audit(
    db: Any,
    *,
    ip: str,
    action: str,
    target: str = "",
    summary: str = "",
    at: str | None = None,
    audit_id: str | None = None,
) -> dict[str, Any] | None:
    """写一条操作日志；成功返回落库的记录，失败返回 ``None``（不影响业务）。

    调用方只需要给 ``ip`` / ``action`` / ``target`` / ``summary``，
    值里的密钥形状会再过一次 ``redact()``。
    """
    record = {
        "id": str(audit_id or uuid.uuid4().hex),
        "at": str(at or _now_iso()),
        "ip": _clip(redact(ip), 64),
        "action": _clip(redact(action), 120),
        "target": _clip(redact(target), 240),
        "summary": _clip(redact(summary), 500),
    }
    conn = getattr(db, "conn", None)
    lock = getattr(db, "_lock", None)  # noqa: SLF001 - 与 DB 层共用连接与锁
    if conn is None:
        log.debug("操作日志跳过：数据库未就绪")
        return None
    ensure_audit_table(db)
    try:
        if lock is not None:
            with lock, conn:
                conn.execute(
                    "INSERT INTO admin_audit (id, at, ip, action, target, summary)"
                    " VALUES (?,?,?,?,?,?)",
                    (
                        record["id"],
                        record["at"],
                        record["ip"],
                        record["action"],
                        record["target"],
                        record["summary"],
                    ),
                )
        else:  # pragma: no cover - 非标准 DB 壳
            with conn:
                conn.execute(
                    "INSERT INTO admin_audit (id, at, ip, action, target, summary)"
                    " VALUES (?,?,?,?,?,?)",
                    (
                        record["id"],
                        record["at"],
                        record["ip"],
                        record["action"],
                        record["target"],
                        record["summary"],
                    ),
                )
    except sqlite3.Error as exc:
        log.warning("操作日志写入失败（忽略）: %s", exc)
        return None
    return record


def list_audit(
    db: Any,
    *,
    offset: int = 0,
    limit: int = 50,
    action: str = "",
    target: str = "",
) -> dict[str, Any]:
    """分页读取操作日志（``at`` 倒序）；``action`` 精确匹配，``target`` 模糊匹配。"""
    try:
        offset_v = max(0, int(offset))
    except (TypeError, ValueError):
        offset_v = 0
    try:
        limit_v = min(500, max(1, int(limit)))
    except (TypeError, ValueError):
        limit_v = 50

    where: list[str] = []
    params: list[Any] = []
    action_v = str(action or "").strip()
    target_v = str(target or "").strip()
    if action_v:
        where.append("action=?")
        params.append(action_v)
    if target_v:
        where.append("target LIKE ?")
        params.append(f"%{target_v}%")
    clause = f" WHERE {' AND '.join(where)}" if where else ""

    empty = {"items": [], "total": 0, "offset": offset_v, "limit": limit_v}
    conn = getattr(db, "conn", None)
    lock = getattr(db, "_lock", None)  # noqa: SLF001
    if conn is None:
        return empty
    ensure_audit_table(db)
    try:
        if lock is not None:
            with lock:
                total = int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM admin_audit{clause}", tuple(params)
                    ).fetchone()[0]
                )
                rows = conn.execute(
                    "SELECT id, at, ip, action, target, summary FROM admin_audit"
                    f"{clause} ORDER BY at DESC, id DESC LIMIT ? OFFSET ?",
                    (*params, limit_v, offset_v),
                ).fetchall()
        else:  # pragma: no cover - 非标准 DB 壳
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM admin_audit{clause}", tuple(params)
                ).fetchone()[0]
            )
            rows = conn.execute(
                "SELECT id, at, ip, action, target, summary FROM admin_audit"
                f"{clause} ORDER BY at DESC, id DESC LIMIT ? OFFSET ?",
                (*params, limit_v, offset_v),
            ).fetchall()
    except sqlite3.Error as exc:
        log.warning("操作日志读取失败（忽略）: %s", exc)
        return empty
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "offset": offset_v,
        "limit": limit_v,
    }


__all__ = [
    "SECRET_HINTS",
    "derive_action",
    "ensure_audit_table",
    "is_secret_field",
    "list_audit",
    "redact",
    "reset_ensure_cache",
    "route_segments",
    "summarize",
    "write_audit",
]
