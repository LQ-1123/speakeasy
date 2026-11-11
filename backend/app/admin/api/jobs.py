"""管理 API · 后台任务（``/api/admin/jobs*``）。

为什么需要这个域
----------------
``background_jobs`` 表由 ``app/jobs.py::DurableJobQueue`` 维护，任务是"低优先级
维护工作"（世界生成、生活事件推进、音频预热…）。旧管理台**没有任何入口**能看到
任务状态，失败只能翻日志——这是运维盲区。本模块补上只读列表 + 手动触发。

接口
----
``GET  /api/admin/jobs``              任务列表（状态 / 类型筛选 + 分页）
``GET  /api/admin/jobs/kinds``        合法任务类型
``POST /api/admin/jobs/{kind}/run``   立即排队一次

合法 kind 的来源
----------------
以 ``app/maintenance.py::maintenance_loop`` 里的 ``handlers`` 字典为准（worker 只认
这些 kind，入队其它 kind 只会产生一条永远失败的记录）。该字典是函数内局部变量，
无法直接 import，因此在本模块声明 :data:`JOB_KINDS` 镜像它，并由
``backend/tests/test_admin_api_split.py`` 用 ``ast`` 静态解析
``maintenance.py`` 的 ``handlers`` 字面量做**防漂移断言**——两边一旦不同步，
测试会立刻失败，而不是等线上发现任务跑不动。
"""

from __future__ import annotations

import sqlite3
import time

from fastapi import APIRouter, HTTPException, Query, Request

from app.admin.api import deps
from app.logging_utils import get_logger

log = get_logger(__name__)

#: 不声明 tags：父路由已带 ``tags=["admin"]``，重复声明会让签名里出现两个 "admin"。
router = APIRouter(prefix="/jobs")

#: 合法任务类型，顺序与 ``app/maintenance.py`` 的 handlers 字典一致。
#: 前 9 个由循环按周期入队，``followups`` 由周期任务安排、``followup`` 是单条投递。
JOB_KINDS: tuple[str, ...] = (
    "world",
    "daily_world",
    "life_events",
    "persona_dna",
    "voice_readiness",
    "audio_warm",
    "cleanup",
    "event_posts",
    "call_memory",
    "followups",
    "followup",
)

#: 手动触发的优先级。数值越小越先执行（与 maintenance 周期任务 100~200 相比，
#: 50 表示"人工点按比定时任务更急"，但仍会被正在执行的任务挡住）。
MANUAL_PRIORITY = 50

#: 列表返回的列（``payload`` 含业务细节，可能在消息里带用户内容，不下发）。
_LIST_COLUMNS = (
    "id",
    "kind",
    "status",
    "attempts",
    "priority",
    "run_after",
    "last_error",
    "created_at",
    "updated_at",
)

#: ``background_jobs`` 的合法状态（与 ``DurableJobQueue`` 的写入值一致）。
JOB_STATUSES: tuple[str, ...] = ("queued", "retry", "running", "done", "failed")

def _today() -> str:
    """今天（本地时区）的 day key，与每日世界/生活事件用的是同一个口径。"""
    try:
        from app.v3.daily_world import day_key  # noqa: PLC0415

        return str(day_key())
    except Exception:  # noqa: BLE001 - 时区模块不可用时退化成 UTC 日期
        return time.strftime("%Y-%m-%d", time.gmtime())

def _db_or_503(request: Request):
    db = deps.db(request)
    if db is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")
    return db

@router.get("/kinds")
async def list_job_kinds(request: Request) -> dict:
    """合法任务类型（供管理台渲染「立即执行」按钮）。"""
    return {"kinds": list(JOB_KINDS)}

@router.get("")
async def list_jobs(
    request: Request,
    status: str = Query("", description="按状态筛选，空=全部"),
    kind: str = Query("", description="按任务类型筛选，空=全部"),
    offset: int = Query(0, ge=0),
    limit: int = Query(40, ge=1, le=200),
) -> dict:
    """后台任务列表。

    失败时也返回 200 + 空列表：任务表缺失（极老库）不该让总览整页 500，
    与 ``stats_persona`` 里 ``scalar()`` 的容错口径一致。
    """
    db = _db_or_503(request)

    where: list[str] = []
    params: list[object] = []
    status_value = str(status or "").strip()
    if status_value:
        where.append("status=?")
        params.append(status_value)
    kind_value = str(kind or "").strip()
    if kind_value:
        where.append("kind=?")
        params.append(kind_value)
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    columns = ", ".join(_LIST_COLUMNS)

    items: list[dict] = []
    total = 0
    try:
        with db._lock:  # noqa: SLF001 - 与 DB 层共用同一把锁
            row = db.conn.execute(
                f"SELECT COUNT(*) FROM background_jobs{clause}", tuple(params)
            ).fetchone()
            total = int((row[0] if row is not None else 0) or 0)
            rows = db.conn.execute(
                f"SELECT {columns} FROM background_jobs{clause}"
                " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (*params, int(limit), int(offset)),
            ).fetchall()
            items = [dict(r) for r in rows]
    except sqlite3.Error as exc:  # pragma: no cover - 极老库没有该表
        log.debug("读取 background_jobs 失败（忽略）: %s", exc)
        items, total = [], 0

    return {"items": items, "total": total, "offset": int(offset), "limit": int(limit)}

@router.post("/{kind}/run")
async def run_job(request: Request, kind: str) -> dict:
    """把指定任务**排队**一次（不立即执行）。

    刻意只入队不直接调用 handler：handler 内部依赖 ``app.state`` 上的运行期对象
    （``v2`` / ``v3`` / ``engines``），在请求线程里跑会和 worker 抢 SQLite 连接与
    锁，还可能因为 HTTP 超时把长任务掐断。入队后由既有 worker 按优先级领取——
    与 maintenance 周期任务走完全相同的执行路径，行为可预期。

    ``dedupe_key`` 带纳秒时间戳，保证连续点击也能排多次（不会像周期任务那样被
    去重合并）；``priority=50`` 让手动任务排在同周期任务之前。
    """
    kind_value = str(kind or "").strip()
    if kind_value not in JOB_KINDS:
        # 旧代码里"未知资源"用 404；这里参数值不合法，按契约 §1.3 用 422。
        # 沿用本代码库既有的 `detail` 字符串写法（前端 api.ts 会把它当 message 展示）。
        raise HTTPException(
            status_code=422,
            detail=f"未知任务类型: {kind_value}（可选 {', '.join(JOB_KINDS)}）",
        )

    queue = deps.queue(request)
    if queue is None:
        raise HTTPException(status_code=503, detail="后台任务队列未就绪")

    job_id = queue.enqueue(
        kind_value,
        {"day": _today()},
        dedupe_key=f"manual:{kind_value}:{time.time_ns()}",
        priority=MANUAL_PRIORITY,
    )
    log.info("管理台手动入队任务: kind=%s id=%s", kind_value, job_id)
    return {"ok": True, "id": job_id}

__all__ = ["JOB_KINDS", "JOB_STATUSES", "MANUAL_PRIORITY", "router"]
