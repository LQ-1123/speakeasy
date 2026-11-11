"""管理 API · 操作日志（``/api/admin/audit/*``）。

为什么要有这个页面
------------------
管理台能改词库、改角色、发动态、改 Prompt——单人管理时"我记得我刚改了什么"够用，
一旦出事（比如某个角色突然说话很怪），第一个问题永远是**"最近谁改了什么"**。
没有这条时间线，只能靠翻日志猜。

设计上的两个取舍
----------------
1. **只记成功的写操作**（由 ``require_admin`` 的 yield 依赖保证：handler 抛错就不写）。
   失败的写不进来——否则日志会被误操作刷屏，真正生效的改动反而被淹没。
2. **摘要只记字段名，不记值**（见 :func:`app.admin.audit.summarize`）。
   值里可能有密钥、可能有用户内容，进了日志就等于二次泄露；
   而"改了哪些字段"已足够定位问题。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.admin import audit as admin_audit
from app.admin.api import deps

#: 不声明 tags：父路由已带 ``tags=["admin"]``，重复声明会让签名里出现两个 "admin"。
router = APIRouter(prefix="/audit")

@router.get("")
async def admin_audit_list(
    request: Request,
    offset: int = 0,
    limit: int = 50,
    action: str = "",
    target: str = "",
) -> dict:
    """分页读取操作日志（``at`` 倒序）。``action`` 精确匹配，``target`` 模糊匹配。"""
    db = deps.db(request)
    if db is None:
        return {"ok": False, "issues": ["数据库未就绪"], "items": [], "total": 0}
    payload = admin_audit.list_audit(
        db, offset=offset, limit=limit, action=action, target=target
    )
    return {"ok": True, **payload}

@router.get("/actions")
async def admin_audit_actions(request: Request) -> dict:
    """出现过的 action 清单（给筛选下拉用）。

    从已有日志里聚合，而不是维护一份硬编码枚举——枚举一定会漏，
    漏掉的 action 在筛选里就选不到，等于那类记录查不出来。
    """
    db = deps.db(request)
    if db is None:
        return {"ok": False, "issues": ["数据库未就绪"], "actions": []}
    admin_audit.ensure_audit_table(db)
    conn = getattr(db, "conn", None)
    if conn is None:
        return {"ok": True, "actions": []}
    lock = getattr(db, "_lock", None)  # noqa: SLF001
    sql = (
        "SELECT action, COUNT(*) AS n FROM admin_audit"
        " GROUP BY action ORDER BY n DESC, action ASC"
    )
    try:
        if lock is not None:
            with lock:
                rows = conn.execute(sql).fetchall()
        else:  # pragma: no cover - 非标准 DB 壳
            rows = conn.execute(sql).fetchall()
    except Exception:  # noqa: BLE001 - 表还没建时当作没有日志
        return {"ok": True, "actions": []}
    return {
        "ok": True,
        "actions": [{"action": str(row["action"]), "count": int(row["n"])} for row in rows],
    }

__all__ = ["router"]
