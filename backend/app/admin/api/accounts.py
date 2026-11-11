"""管理台 · 账号运营（``/api/admin/accounts*``）。

给运营用的最小面：
- 邀请码：创建 / 列表 / 停用（邀请内测的主要准入手段）；
- 账号：按邮箱/用户 id 查询，返回验证状态与当前能力，不返回密码哈希与 token；
- 分龄：写入年龄核验结论（运营核验年龄组，不收集证件），变更自动进 ``v3_age_audit``。

写操作由管理台统一鉴权与审计（``app.admin.auth.require_admin`` 挂在聚合路由上），
本模块不再重复鉴权，也不碰客户端会话。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.admin.api import deps
from app.v3 import accounts

router = APIRouter(prefix="/accounts")

#: 分龄核验允许的取值（与 accounts.VALID_* 保持同一来源）
AGE_GROUPS = accounts.VALID_AGE_GROUPS
AGE_STATUS = accounts.VALID_AGE_STATUS


def _db(request: Request) -> Any:
    db = deps.db(request)
    if db is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")
    accounts.ensure_accounts_schema(db)
    return db


class InviteIn(BaseModel):
    code: str = Field(default="", max_length=64)
    note: str = Field(default="", max_length=200)
    max_uses: int = Field(default=1, ge=1, le=10000)
    expires_at: str = Field(default="", max_length=40)


class AgeIn(BaseModel):
    age_group: str = Field(min_length=2, max_length=16)
    status: str = Field(default="verified", min_length=2, max_length=16)
    method: str = Field(default="ops", max_length=40)
    guardian_user_id: str = Field(default="", max_length=64)
    note: str = Field(default="", max_length=400)
    expires_at: str = Field(default="", max_length=40)


@router.get("/invites")
async def list_invites(request: Request, limit: int = Query(default=200, ge=1, le=1000)) -> dict:
    db = _db(request)
    return {"items": accounts.list_invite_codes(db, limit=limit)}


@router.post("/invites")
async def create_invite(request: Request, body: InviteIn) -> dict:
    db = _db(request)
    code = str(body.code or "").strip()
    if code:
        existing = {row["code"] for row in accounts.list_invite_codes(db, limit=1000)}
        if code in existing:
            raise HTTPException(status_code=409, detail="邀请码已存在")
    try:
        created = accounts.create_invite_code(
            db, code, note=body.note, max_uses=body.max_uses, expires_at=body.expires_at
        )
    except Exception as exc:  # noqa: BLE001 - 参数问题统一 400
        raise HTTPException(status_code=400, detail=f"创建邀请码失败: {type(exc).__name__}") from None
    return {"ok": True, "code": created}


@router.post("/invites/{code}/disable")
async def disable_invite(code: str, request: Request) -> dict:
    db = _db(request)
    if not accounts.disable_invite_code(db, code):
        raise HTTPException(status_code=404, detail="邀请码不存在")
    return {"ok": True, "code": code}


@router.get("/users")
async def search_users(
    request: Request,
    query: str = "",
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    db = _db(request)
    return {"items": accounts.search_accounts(db, query, limit=limit)}


@router.get("/users/{user_id}/capabilities")
async def user_capabilities(user_id: str, request: Request) -> dict:
    db = _db(request)
    return accounts.capabilities(db, user_id)


@router.post("/users/{user_id}/age")
async def set_age(user_id: str, body: AgeIn, request: Request) -> dict:
    db = _db(request)
    group = str(body.age_group or "").strip().lower()
    state = str(body.status or "").strip().lower()
    if group not in AGE_GROUPS:
        raise HTTPException(status_code=400, detail=f"age_group 必须是 {AGE_GROUPS}")
    if state not in AGE_STATUS:
        raise HTTPException(status_code=400, detail=f"status 必须是 {AGE_STATUS}")
    admin = getattr(request.state, "admin_user", "") or "admin"
    try:
        record = accounts.set_age_verification(
            db,
            user_id,
            age_group=group,
            status=state,
            method=body.method,
            guardian_user_id=body.guardian_user_id,
            note=body.note,
            actor=str(admin),
            expires_at=body.expires_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"ok": True, "record": record, "capabilities": accounts.capabilities(db, user_id)}
