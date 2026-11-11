"""V3 · 商业合作用户侧接口（``/api/v3/ads/*``）。

- 查看自己是否可以接收合作推荐（成年 + 已核验 + 主动开启）；
- 开启/关闭合作推荐（默认关闭，符合方案「广告默认关闭」）；
- 上报投放事件：可见曝光 / 点击 / 关闭 / 举报（关闭与举报会屏蔽该活动 30 天）。

动态流里的广告由推荐流直接返回 ``kind="sponsored"`` 条目（含 ``delivery_id``），
聊天里的商业段落以独立消息类型返回；本路由只做状态与事件，不生成文案。
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.logging_utils import get_logger
from app.v3 import ads, client_api
from app.v3.security import bind_current_user

log = get_logger(__name__)
router = APIRouter(prefix="/api/v3/ads", tags=["ads"])


def _db(request: Request) -> Any:
    services = getattr(request.app.state, "v2", None)
    db = getattr(services, "db", None)
    if db is None:  # pragma: no cover - 装配异常
        raise HTTPException(status_code=503, detail="数据库未初始化")
    ads.ensure_ads_schema(db)
    return db


async def _run(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


class OptInIn(BaseModel):
    enabled: bool


class AdEventIn(BaseModel):
    delivery_id: str = Field(min_length=4, max_length=64)
    type: Literal["impression", "click", "close", "report"]
    payload: dict = Field(default_factory=dict)


@router.get("/status")
async def ad_status(request: Request, user_id: str = Depends(bind_current_user)) -> dict:
    db = _db(request)
    eligible, reason = await _run(ads.ad_eligible, db, user_id)
    prefs = await _run(client_api.preferences, db, user_id)
    return {
        "eligible": bool(eligible),
        "reason": reason,
        "ads_opt_in": bool(prefs.get("ads")),
        "age_gate": await _run(_age_group, db, user_id),
        "label": "广告 · 品牌合作",
    }


def _age_group(db: Any, user_id: str) -> str:
    try:
        return str(accounts_caps(db, user_id)["age"]["group"])
    except Exception:  # noqa: BLE001
        return "unknown"


def accounts_caps(db: Any, user_id: str) -> dict:
    from app.v3 import accounts  # noqa: PLC0415

    return accounts.capabilities(db, user_id)


@router.post("/opt-in")
async def set_opt_in(
    body: OptInIn, request: Request, user_id: str = Depends(bind_current_user)
) -> dict:
    db = _db(request)
    if body.enabled:
        eligible, reason = await _run(ads.ad_eligible, db, user_id)
        caps = await _run(accounts_caps, db, user_id)
        if caps["age"]["group"] != "adult" or not caps["age"]["verified"]:
            raise HTTPException(status_code=403, detail="只有已核验成年账号才能开启合作推荐")
        if not caps["features"].get("ads", False):
            raise HTTPException(status_code=403, detail="当前账号不可用合作推荐能力")

    def save() -> dict:
        data = client_api.preferences(db, user_id)
        data["ads"] = bool(body.enabled)
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                "INSERT INTO client_preferences VALUES (?,?)"
                " ON CONFLICT(user_id) DO UPDATE SET value=excluded.value",
                (user_id, __import__("json").dumps(data, ensure_ascii=False)),
            )
        return data

    data = await _run(save)
    return {"ok": True, "ads_opt_in": bool(data.get("ads"))}


@router.post("/events")
async def ad_event(
    body: AdEventIn, request: Request, user_id: str = Depends(bind_current_user)
) -> dict:
    db = _db(request)
    try:
        return await _run(
            ads.record_event, db, user_id=user_id, delivery_id=body.delivery_id,
            event_type=body.type, payload=body.payload,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
