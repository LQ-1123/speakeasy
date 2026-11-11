"""V3 · 青少年模式自助接口（``/api/v3/teen/*``）。

客户端用它查询「我现在能不能用」以及连续使用提醒；监护关系用确认码完成绑定。
写操作都会落到 :mod:`app.v3.teen`，不做任何额外持久化。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.logging_utils import get_logger
from app.v3 import teen
from app.v3.security import bind_current_user

log = get_logger(__name__)
router = APIRouter(prefix="/api/v3/teen", tags=["teen"])


def _db(request: Request) -> Any:
    services = getattr(request.app.state, "v2", None)
    db = getattr(services, "db", None)
    if db is None:  # pragma: no cover - 装配异常
        raise HTTPException(status_code=503, detail="数据库未初始化")
    teen.ensure_teen_schema(db)
    return db


async def _run(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


class GuardianRequestIn(BaseModel):
    note: str = Field(default="", max_length=200)


class GuardianConfirmIn(BaseModel):
    code: str = Field(min_length=4, max_length=32)


@router.get("/status")
async def status(request: Request, user_id: str = Depends(bind_current_user)) -> dict:
    db = _db(request)
    return await _run(teen.status, db, user_id)


@router.post("/heartbeat")
async def heartbeat(request: Request, user_id: str = Depends(bind_current_user)) -> dict:
    """一次活动心跳：累计今日使用时长并返回最新门禁结论（客户端据此弹提醒/退出）。"""
    db = _db(request)
    await _run(teen.record_activity, db, user_id)
    return await _run(teen.status, db, user_id)


@router.post("/session/end")
async def end_session(request: Request, user_id: str = Depends(bind_current_user)) -> dict:
    """主动结束连续会话（退出登录/挂断电话时调用），不清当日累计。"""
    db = _db(request)
    await _run(teen.reset_session, db, user_id)
    return {"ok": True}


@router.post("/guardian/request")
async def request_guardian(
    body: GuardianRequestIn, request: Request, user_id: str = Depends(bind_current_user)
) -> dict:
    db = _db(request)
    try:
        result = await _run(teen.request_guardian, db, user_id, note=body.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"ok": True, **result}


@router.post("/guardian/confirm")
async def confirm_guardian(
    body: GuardianConfirmIn, request: Request, user_id: str = Depends(bind_current_user)
) -> dict:
    db = _db(request)
    try:
        result = await _run(teen.confirm_guardian, db, user_id, body.code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"ok": True, **result}
