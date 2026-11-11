"""管理台 · 举报处理（``/api/admin/reports*``）。

- ``GET /api/admin/reports``：按状态/对象类型筛选举报；
- ``POST /api/admin/reports/{id}/status``：处理（reviewing/resolved/rejected，写结论）；
- ``GET /api/admin/reports/stats``：按状态汇总。

写操作沿用管理台统一鉴权与审计；本模块只做参数校验与响应组装。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.admin.api import deps
from app.v3 import reports

router = APIRouter(prefix="/reports")


def _db(request: Request) -> Any:
    db = deps.db(request)
    if db is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")
    reports.ensure_reports_schema(db)
    return db


class StatusIn(BaseModel):
    status: Literal["reviewing", "resolved", "rejected", "open"]
    resolution: str = Field(default="", max_length=500)


@router.get("")
async def list_reports(
    request: Request, status: str = "", target_type: str = "",
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return {
        "items": reports.list_reports(_db(request), status=status, target_type=target_type, limit=limit)
    }


@router.get("/stats")
async def report_stats(request: Request) -> dict:
    return reports.stats(_db(request))


@router.post("/{report_id}/status")
async def update_status(report_id: str, body: StatusIn, request: Request) -> dict:
    try:
        row = reports.update_status(
            _db(request), report_id, body.status,
            resolution=body.resolution, handled_by="admin",
        )
    except ValueError as exc:
        detail = str(exc)
        code = 404 if detail == "举报不存在" else 400
        raise HTTPException(status_code=code, detail=detail) from None
    return {"ok": True, "report": row}


__all__ = ["router"]
