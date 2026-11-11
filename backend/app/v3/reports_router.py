"""V3 · 举报用户侧接口（``/api/v3/reports``）。

- ``POST /api/v3/reports``：举报人物/动态/评论/私聊消息/广告/用户；
- ``GET /api/v3/reports/mine``：我提交的举报及处理状态。

需要登录（``bind_current_user``；``local`` 单用户模式不支持举报）。防刷与去重见
:mod:`app.v3.reports`。运营处理入口在管理台 ``/api/admin/reports``。
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.logging_utils import get_logger
from app.v3 import reports
from app.v3.identity import LOCAL_USER_ID
from app.v3.security import bind_current_user

log = get_logger(__name__)
router = APIRouter(prefix="/api/v3/reports", tags=["reports"])


def _db(request: Request) -> Any:
    services = getattr(request.app.state, "v2", None)
    db = getattr(services, "db", None)
    if db is None:  # pragma: no cover - 装配异常
        raise HTTPException(status_code=503, detail="数据库未初始化")
    reports.ensure_reports_schema(db)
    return db


class ReportIn(BaseModel):
    target_type: Literal["persona", "post", "comment", "message", "ad", "user"]
    target_id: str = Field(min_length=1, max_length=200)
    category: Literal[
        "harassment", "sexual", "violence", "self_harm", "misinformation", "spam", "other",
    ]
    detail: str = Field(default="", max_length=500)


@router.post("")
async def create_report(
    body: ReportIn, request: Request, user_id: str = Depends(bind_current_user)
) -> dict:
    if not user_id or user_id == LOCAL_USER_ID:
        raise HTTPException(status_code=401, detail="需要登录后才能举报")
    db = _db(request)
    try:
        data = await asyncio.to_thread(
            reports.create_report, db, reporter_user_id=user_id,
            target_type=body.target_type, target_id=body.target_id,
            category=body.category, detail=body.detail,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"ok": True, "report": data}


@router.get("/mine")
async def my_reports(
    request: Request,
    user_id: str = Depends(bind_current_user),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    if not user_id or user_id == LOCAL_USER_ID:
        raise HTTPException(status_code=401, detail="需要登录后才能查看举报记录")
    db = _db(request)
    items = await asyncio.to_thread(reports.list_mine, db, user_id, limit=limit)
    return {"items": items}


__all__ = ["router"]
