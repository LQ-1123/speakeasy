"""管理台 · 商业运营（``/api/admin/ads*``）。

给运营的最小完整链路：
品牌 → 产品/活动（草稿→审核→启用→暂停/结束，修改产生新版本、可还原）
→ 素材（独立审核，通过后才能投放）→ 角色分配（系统建议 + 运营确认）
→ 预览 / 投放记录 / 汇总。

写操作沿用管理台统一鉴权与审计（``app.admin.auth.require_admin`` 挂在聚合路由），
本模块只做参数校验与响应组装；投放与频控逻辑在 :mod:`app.v3.ads`。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.admin.api import deps
from app.v3 import ads

router = APIRouter(prefix="/ads")


def _db(request: Request) -> Any:
    db = deps.db(request)
    if db is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")
    ads.ensure_ads_schema(db)
    return db


class BrandIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    industry: str = Field(default="", max_length=40)
    qualification: str = Field(default="", max_length=2000)
    landing_url: str = Field(default="", max_length=500)
    note: str = Field(default="", max_length=500)


class BrandPatch(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    industry: str | None = Field(default=None, max_length=40)
    qualification: str | None = Field(default=None, max_length=2000)
    landing_url: str | None = Field(default=None, max_length=500)
    status: str | None = Field(default=None, pattern="^(active|disabled)$")
    note: str | None = Field(default=None, max_length=500)


class CampaignIn(BaseModel):
    brand_id: str = Field(min_length=2, max_length=64)
    title: str = Field(min_length=1, max_length=120)
    purpose: str = Field(default="", max_length=200)
    required_tags: list[str] = Field(default_factory=list)
    constraints: dict = Field(default_factory=dict)
    facts: list[str] = Field(default_factory=list)
    landing_url: str = Field(default="", max_length=500)
    channels: list[str] = Field(default_factory=lambda: ["chat", "feed"])
    valid_from: str = Field(default="", max_length=40)
    valid_until: str = Field(default="", max_length=40)


class CampaignPatch(BaseModel):
    brand_id: str | None = None
    title: str | None = Field(default=None, max_length=120)
    purpose: str | None = Field(default=None, max_length=200)
    required_tags: list[str] | None = None
    constraints: dict | None = None
    facts: list[str] | None = None
    landing_url: str | None = Field(default=None, max_length=500)
    channels: list[str] | None = None
    valid_from: str | None = Field(default=None, max_length=40)
    valid_until: str | None = Field(default=None, max_length=40)


class StatusIn(BaseModel):
    status: str = Field(min_length=3, max_length=16)


class CreativeIn(BaseModel):
    campaign_id: str = Field(min_length=2, max_length=64)
    channel: str = Field(pattern="^(chat|feed)$")
    title: str = Field(default="", max_length=120)
    body_template: str = Field(min_length=1, max_length=600)
    facts: list[str] = Field(default_factory=list)


class ReviewIn(BaseModel):
    approved: bool
    note: str = Field(default="", max_length=500)


# --------------------------------------------------------------------------- #
# 品牌
# --------------------------------------------------------------------------- #
@router.get("/brands")
async def list_brands(request: Request, limit: int = Query(default=100, ge=1, le=500)) -> dict:
    return {"items": ads.list_brands(_db(request), limit=limit)}


@router.post("/brands")
async def create_brand(body: BrandIn, request: Request) -> dict:
    brand = ads.create_brand(_db(request), **body.model_dump())
    return {"ok": True, "brand": brand}


@router.patch("/brands/{brand_id}")
async def update_brand(brand_id: str, body: BrandPatch, request: Request) -> dict:
    try:
        brand = ads.update_brand(_db(request), brand_id, **body.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"ok": True, "brand": brand}


# --------------------------------------------------------------------------- #
# 活动
# --------------------------------------------------------------------------- #
@router.get("/campaigns")
async def list_campaigns(
    request: Request, status: str = "", limit: int = Query(default=100, ge=1, le=500)
) -> dict:
    return {"items": ads.list_campaigns(_db(request), status=status, limit=limit)}


@router.post("/campaigns")
async def create_campaign(body: CampaignIn, request: Request) -> dict:
    try:
        campaign = ads.create_campaign(_db(request), **body.model_dump(), created_by="admin")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"ok": True, "campaign": campaign}


@router.get("/campaigns/{campaign_id}")
async def get_campaign(campaign_id: str, request: Request) -> dict:
    campaign = ads.get_campaign(_db(request), campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="活动不存在")
    return {"campaign": campaign}


@router.patch("/campaigns/{campaign_id}")
async def update_campaign(campaign_id: str, body: CampaignPatch, request: Request) -> dict:
    try:
        campaign = ads.update_campaign(
            _db(request), campaign_id, created_by="admin",
            **body.model_dump(exclude_none=True),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"ok": True, "campaign": campaign}


@router.post("/campaigns/{campaign_id}/status")
async def set_status(campaign_id: str, body: StatusIn, request: Request) -> dict:
    try:
        campaign = ads.set_campaign_status(_db(request), campaign_id, body.status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"ok": True, "campaign": campaign}


@router.get("/campaigns/{campaign_id}/versions")
async def list_versions(campaign_id: str, request: Request) -> dict:
    return {"items": ads.list_campaign_versions(_db(request), campaign_id)}


@router.post("/campaigns/{campaign_id}/restore/{version}")
async def restore_version(campaign_id: str, version: int, request: Request) -> dict:
    try:
        campaign = ads.restore_campaign_version(
            _db(request), campaign_id, version, created_by="admin"
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"ok": True, "campaign": campaign}


@router.get("/campaigns/{campaign_id}/preview")
async def preview(campaign_id: str, request: Request, channel: str = "chat") -> dict:
    db = _db(request)
    try:
        data = ads.preview(db, campaign_id, channel=channel)
        data["eligible_for_preview"] = True
        return data
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


@router.get("/campaigns/{campaign_id}/assignments")
async def list_assignments(campaign_id: str, request: Request) -> dict:
    return {"items": ads.list_assignments(_db(request), campaign_id)}


@router.post("/campaigns/{campaign_id}/assignments/suggest")
async def suggest_assignments(
    campaign_id: str, request: Request, limit: int = Query(default=10, ge=1, le=50)
) -> dict:
    try:
        items = ads.suggest_persona_assignments(_db(request), campaign_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"items": items}


@router.post("/campaigns/{campaign_id}/assignments/{persona_id}/confirm")
async def confirm_assignment(
    campaign_id: str, persona_id: str, request: Request, confirmed: bool = True
) -> dict:
    try:
        return {"ok": True, **ads.confirm_assignment(
            _db(request), campaign_id, persona_id, confirmed=confirmed
        )}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


# --------------------------------------------------------------------------- #
# 素材
# --------------------------------------------------------------------------- #
@router.get("/creatives")
async def list_creatives(
    request: Request, campaign_id: str = "", channel: str = "", approved_only: bool = False
) -> dict:
    return {
        "items": ads.list_creatives(
            _db(request), campaign_id, channel=channel, approved_only=approved_only
        )
    }


@router.post("/creatives")
async def add_creative(body: CreativeIn, request: Request) -> dict:
    try:
        creative = ads.add_creative(_db(request), **body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"ok": True, "creative": creative}


@router.post("/creatives/{creative_id}/review")
async def review_creative(creative_id: str, body: ReviewIn, request: Request) -> dict:
    try:
        creative = ads.review_creative(
            _db(request), creative_id, approved=body.approved, reviewer="admin", note=body.note
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"ok": True, "creative": creative}


# --------------------------------------------------------------------------- #
# 投放记录 / 汇总 / 留痕
# --------------------------------------------------------------------------- #
@router.get("/deliveries")
async def list_deliveries(
    request: Request, user_id: str = "", campaign_id: str = "",
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return {
        "items": ads.list_deliveries(
            _db(request), user_id=user_id, campaign_id=campaign_id, limit=limit
        )
    }


@router.get("/stats")
async def stats(request: Request, campaign_id: str = "") -> dict:
    return ads.campaign_stats(_db(request), campaign_id)


@router.post("/audit/cleanup")
async def cleanup_audit(request: Request) -> dict:
    removed = ads.cleanup_audit(_db(request))
    return {"ok": True, "deleted": int(removed)}
