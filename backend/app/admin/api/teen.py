"""管理台 · 青少年模式运营（``/api/admin/accounts/{user_id}/teen*``）。

给运营/客服的最小工具面：
- 查看未成年人状态（监护关系、限制、今日用量、当前门禁结论）；
- 代绑/解绑监护关系（内测按运营核验，不收集证件）；
- 调整限制（只能收紧：时长≤60 分钟、宵禁必须覆盖 22:00–06:00）。

写操作沿用管理台统一鉴权与审计，本模块不重复鉴权。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.admin.api import deps
from app.v3 import teen

router = APIRouter(prefix="/accounts")


def _db(request: Request) -> Any:
    db = deps.db(request)
    if db is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")
    teen.ensure_teen_schema(db)
    return db


class GuardianIn(BaseModel):
    guardian_user_id: str = Field(default="", max_length=64)
    method: str = Field(default="ops", max_length=40)
    note: str = Field(default="", max_length=400)


class MinorSettingsIn(BaseModel):
    daily_minutes: int | None = Field(default=None, ge=5, le=60)
    session_minutes: int | None = Field(default=None, ge=5, le=30)
    curfew_start: str | None = Field(default=None, max_length=5)
    curfew_end: str | None = Field(default=None, max_length=5)
    timezone: str | None = Field(default=None, max_length=64)
    blocked_personas: list[str] | None = None


@router.get("/{user_id}/teen")
async def teen_status(user_id: str, request: Request) -> dict:
    db = _db(request)
    return teen.status(db, user_id)


@router.post("/{user_id}/teen/guardian")
async def bind_guardian(user_id: str, body: GuardianIn, request: Request) -> dict:
    db = _db(request)
    try:
        link = teen.bind_guardian(
            db,
            user_id,
            guardian_user_id=body.guardian_user_id,
            method=body.method,
            note=body.note,
            actor="admin",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    # 运营代绑同时把年龄核验结论落成「14–17 岁已核验」
    teen.set_minor_settings(db, user_id)  # 幂等：确保有默认限制行
    return {"ok": True, "link": link, "status": teen.status(db, user_id)}


@router.delete("/{user_id}/teen/guardian")
async def revoke_guardian(user_id: str, request: Request) -> dict:
    db = _db(request)
    changed = teen.revoke_guardian(db, user_id, actor="admin")
    if not changed:
        raise HTTPException(status_code=404, detail="没有可撤销的监护关系")
    return {"ok": True, "status": teen.status(db, user_id)}


@router.post("/{user_id}/teen/settings")
async def update_settings(user_id: str, body: MinorSettingsIn, request: Request) -> dict:
    db = _db(request)
    fields = body.model_dump(exclude_none=True)
    try:
        settings = teen.set_minor_settings(db, user_id, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"ok": True, "settings": settings, "status": teen.status(db, user_id)}
