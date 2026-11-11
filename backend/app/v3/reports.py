"""V3 · 内容举报（给「随时举报 / 运营处理」补上后端闭环）。

覆盖范围：人物、动态、评论、私聊消息、广告、用户。设计原则：

- 登录用户可举报**自己看得到的东西**；同一对象同一用户 24 小时内只记一次（去重）；
  每用户每天最多 20 条（防刷）。
- 只记录举报人、对象引用、类别与可选说明；不复制被举报内容正文（留证引用由对象本身提供），
  避免在举报表里再存一份敏感内容。
- 状态机：``open → reviewing → resolved | rejected``，运营写处理结论；写操作进管理台审计。
- 广告另有更细的投放事件（``/api/v3/ads/events`` type=report），举报表用于通用内容。
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from app.logging_utils import get_logger
from app.v3.identity import LOCAL_USER_ID, current_user_id

log = get_logger(__name__)

TARGET_TYPES = ("persona", "post", "comment", "message", "ad", "user")
CATEGORIES = (
    "harassment", "sexual", "violence", "self_harm", "misinformation", "spam", "other",
)
STATUSES = ("open", "reviewing", "resolved", "rejected")
STATUS_TRANSITIONS = {
    "open": {"reviewing", "resolved", "rejected"},
    "reviewing": {"resolved", "rejected", "open"},
    "resolved": set(),
    "rejected": set(),
}
MAX_PER_DAY = 20
DETAIL_MAX = 500

DDL = """
CREATE TABLE IF NOT EXISTS v3_reports (
    id TEXT PRIMARY KEY,
    reporter_user_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    category TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    resolution TEXT NOT NULL DEFAULT '',
    handled_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_status ON v3_reports(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reports_reporter ON v3_reports(reporter_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reports_target ON v3_reports(target_type, target_id);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime | None = None) -> str:
    return (moment or _now()).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _new_id() -> str:
    return "rp_" + secrets.token_hex(8)


def ensure_reports_schema(db: Any) -> None:
    if db is None:
        return
    try:
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.executescript(DDL)
        setattr(db, "_reports_schema_ready", True)
    except Exception as exc:  # noqa: BLE001
        log.warning("ensure_reports_schema 失败: %s: %s", type(exc).__name__, exc)


def _ensure(db: Any) -> bool:
    if db is None:
        return False
    if getattr(db, "_reports_schema_ready", False):
        return True
    ensure_reports_schema(db)
    return bool(getattr(db, "_reports_schema_ready", False))


def _normalize_target(target_type: str, target_id: str) -> tuple[str, str]:
    kind = str(target_type or "").strip().lower()
    ident = str(target_id or "").strip()[:200]
    if kind not in TARGET_TYPES:
        raise ValueError("非法举报对象类型")
    if not ident:
        raise ValueError("举报对象不能为空")
    if kind == "ad":
        # 广告既可传投放 ID，也允许直接传站内落地页 URL（运营易复制）
        if ident.startswith(("http://", "https://")):
            ident = urlsplit(ident).path or ident
    return kind, ident


def create_report(
    db: Any, *, reporter_user_id: str, target_type: str, target_id: str,
    category: str, detail: str = "", now: datetime | None = None,
) -> dict:
    uid = str(reporter_user_id or "").strip()
    if not uid or uid == LOCAL_USER_ID:
        raise PermissionError("需要登录后才能举报")
    kind, ident = _normalize_target(target_type, target_id)
    cat = str(category or "").strip().lower()
    if cat not in CATEGORIES:
        raise ValueError("举报类别不合法")
    text = str(detail or "").strip()[:DETAIL_MAX]
    if not _ensure(db):
        raise ValueError("举报功能暂不可用")
    moment = now or _now()
    day_ago = _iso(moment - timedelta(hours=24))
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT COUNT(*) AS n FROM v3_reports WHERE reporter_user_id=? AND created_at>=?",
            (uid, day_ago),
        ).fetchone()
        if int(row["n"] if row else 0) >= MAX_PER_DAY:
            raise PermissionError("举报过于频繁，请稍后再试")
        dup = db.conn.execute(
            "SELECT id FROM v3_reports WHERE reporter_user_id=? AND target_type=? AND target_id=?"
            " AND status IN ('open','reviewing') AND created_at>=? LIMIT 1",
            (uid, kind, ident, day_ago),
        ).fetchone()
        if dup is not None:
            raise ValueError("你已举报过该内容，运营正在处理")
        report_id = _new_id()
        stamp = _iso(moment)
        db.conn.execute(
            "INSERT INTO v3_reports (id, reporter_user_id, target_type, target_id, category,"
            " detail, status, resolution, handled_by, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,'open','','',?,?)",
            (report_id, uid, kind, ident, cat, text, stamp, stamp),
        )
        db.conn.commit()
    return {
        "id": report_id,
        "target_type": kind,
        "target_id": ident,
        "category": cat,
        "status": "open",
        "created_at": stamp,
    }


def list_mine(db: Any, user_id: str, *, limit: int = 50) -> list[dict]:
    if not _ensure(db):
        return []
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT * FROM v3_reports WHERE reporter_user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, max(1, min(int(limit), 200))),
        ).fetchall()
    return [dict(r) for r in rows]


def list_reports(db: Any, *, status: str = "", target_type: str = "", limit: int = 100) -> list[dict]:
    if not _ensure(db):
        return []
    sql = "SELECT * FROM v3_reports WHERE 1=1"
    params: list[Any] = []
    if status:
        sql += " AND status=?"
        params.append(str(status))
    if target_type:
        sql += " AND target_type=?"
        params.append(str(target_type))
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


def update_status(
    db: Any, report_id: str, status: str, *, resolution: str = "", handled_by: str = "",
) -> dict:
    if not _ensure(db):
        raise ValueError("举报功能暂不可用")
    target = str(status or "").strip().lower()
    if target not in STATUSES:
        raise ValueError("非法状态")
    with db._lock:  # noqa: SLF001
        row = db.conn.execute("SELECT * FROM v3_reports WHERE id=?", (report_id,)).fetchone()
        if row is None:
            raise ValueError("举报不存在")
        current = str(row["status"])
        if target not in STATUS_TRANSITIONS.get(current, set()):
            raise ValueError(f"不允许从 {current} 变到 {target}")
        stamp = _iso()
        db.conn.execute(
            "UPDATE v3_reports SET status=?, resolution=?, handled_by=?, updated_at=? WHERE id=?",
            (target, str(resolution or "")[:DETAIL_MAX], str(handled_by or "")[:64], stamp, report_id),
        )
    return dict_from_row(db, report_id)


def dict_from_row(db: Any, report_id: str) -> dict:
    with db._lock:  # noqa: SLF001
        row = db.conn.execute("SELECT * FROM v3_reports WHERE id=?", (report_id,)).fetchone()
    return dict(row) if row is not None else {}


def stats(db: Any) -> dict:
    if not _ensure(db):
        return {"items": []}
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT status, COUNT(*) AS n FROM v3_reports GROUP BY status"
        ).fetchall()
    return {"items": [{"status": r["status"], "count": int(r["n"])} for r in rows]}


__all__ = [
    "CATEGORIES",
    "MAX_PER_DAY",
    "STATUSES",
    "TARGET_TYPES",
    "create_report",
    "ensure_reports_schema",
    "list_mine",
    "list_reports",
    "stats",
    "update_status",
]
