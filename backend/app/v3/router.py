"""SpeakEasy V3 REST 路由（``/api/v3``）。

与 V2 的关系
------------
- ``/api/v2/*`` 全部保留（聊天、语音、通话、记忆、表达本、设置）；
- V3 只负责「人物 / 动态 / 好友关系 / 社交事件」这条新链路，
  加好友时写入 ``friends`` 表，因此后续聊天直接复用 V2 的接口。

鉴权
----
- ``POST /api/v3/auth/register`` 与 ``/api/v3/health`` 不需要身份；
- 其余路由都挂在 :func:`bind_current_user` 依赖下：请求必须带
  ``X-User-Id`` + ``X-User-Token``（不带则视为本地用户 ``local``）。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.logging_utils import get_logger
from app.v3.identity import current_user_id
from app.v3.security import bind_current_user, register_user
from app.v3.service import FEED_PAGE, V3Service

log = get_logger(__name__)

router = APIRouter(prefix="/api/v3", tags=["v3"])

V3_VERSION = "3.0.0"


# --------------------------------------------------------------------------- #
# 请求体
# --------------------------------------------------------------------------- #
class RegisterIn(BaseModel):
    device_key: str | None = Field(default=None, max_length=128)
    display_name: str | None = Field(default=None, max_length=40)


class UserPostIn(BaseModel):
    content: str = Field(min_length=1, max_length=1000)
    visibility: Literal["public", "friends", "private"] = "public"
    media: list[str] = Field(default_factory=list, max_length=9)
    tags: list[str] = Field(default_factory=list, max_length=12)


class CommentIn(BaseModel):
    content: str = Field(min_length=1, max_length=500)


class FriendBody(BaseModel):
    message: str | None = Field(default=None, max_length=200)


class ReadBody(BaseModel):
    read: bool = True


# --------------------------------------------------------------------------- #
# 依赖
# --------------------------------------------------------------------------- #
def _v3(request: Request) -> V3Service:
    service = getattr(request.app.state, "v3", None)
    if service is None:  # pragma: no cover - 装配异常
        raise HTTPException(status_code=503, detail="v3 service 未初始化")
    return service


async def _run(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


# --------------------------------------------------------------------------- #
# 健康 & 身份
# --------------------------------------------------------------------------- #
@router.get("/health")
async def health(request: Request) -> dict:
    service = _v3(request)
    services = getattr(request.app.state, "v2", None)
    db = getattr(services, "db", None)
    world = {}
    if db is not None:
        try:
            from app.v3.world import world_status  # noqa: PLC0415

            world = await asyncio.to_thread(world_status, db)
        except Exception:  # noqa: BLE001
            world = {}
    return {
        "status": "ok",
        "version": V3_VERSION,
        "social": service.config.public(),
        "world": world,
        "personas": world.get("personas") or len(service._personas()),  # noqa: SLF001 - 只读计数
    }


@router.post("/auth/register")
async def auth_register(request: Request, body: RegisterIn | None = None) -> dict:
    payload = body or RegisterIn()
    services = getattr(request.app.state, "v2", None)
    db = getattr(services, "db", None)
    if db is None:  # pragma: no cover
        raise HTTPException(status_code=503, detail="数据库未初始化")
    return await _run(register_user, db, payload.device_key, payload.display_name or "")


@router.get("/me")
async def me(user_id: str = Depends(bind_current_user)) -> dict:
    return {"user_id": user_id}


# --------------------------------------------------------------------------- #
# 信息流
# --------------------------------------------------------------------------- #
@router.get("/feed")
async def get_feed(
    request: Request,
    cursor: str | None = None,
    limit: int = Query(default=FEED_PAGE, ge=1, le=40),
    user_id: str = Depends(bind_current_user),
) -> dict:
    service = _v3(request)
    return await _run(service.feed, user_id, cursor=cursor, limit=limit)


@router.get("/feed/summary")
async def feed_summary(
    request: Request, user_id: str = Depends(bind_current_user)
) -> dict:
    return await _run(_v3(request).feed_summary, user_id)


# --------------------------------------------------------------------------- #
# Persona
# --------------------------------------------------------------------------- #
@router.get("/personas")
async def list_personas(
    request: Request,
    limit: int = Query(default=24, ge=1, le=60),
    offset: int = Query(default=0, ge=0),
    user_id: str = Depends(bind_current_user),
) -> dict:
    service = _v3(request)
    items = await _run(service.list_personas, limit=limit, offset=offset)
    return {"items": items}


@router.get("/personas/{persona_id}")
async def get_persona(
    request: Request, persona_id: str, user_id: str = Depends(bind_current_user)
) -> dict:
    service = _v3(request)
    profile = await _run(service.persona_profile, persona_id, user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="人物不存在")
    return profile


@router.get("/personas/{persona_id}/posts")
async def get_persona_posts(
    request: Request,
    persona_id: str,
    cursor: str | None = None,
    limit: int = Query(default=12, ge=1, le=40),
    user_id: str = Depends(bind_current_user),
) -> dict:
    service = _v3(request)
    if await _run(service.persona_by_id, persona_id) is None:
        raise HTTPException(status_code=404, detail="人物不存在")
    return await _run(service.persona_posts, persona_id, user_id=user_id, limit=limit, cursor=cursor)


@router.get("/discover/next")
async def discover_next(
    request: Request,
    language: str | None = None,
    user_id: str = Depends(bind_current_user),
) -> dict:
    """偶遇一个人：**从已生成的世界（Persona 池）里挑**，不重新捏人。

    世界生成（人物 + 信息流一次性生成）发生在服务启动/首次初始化阶段，
    见 docs/V3_API.md「世界生成」。
    """
    service = _v3(request)
    data = await _run(service.discover_next, user_id, language=language)
    if data is None:
        raise HTTPException(status_code=404, detail="世界还没有生成人物")
    return data


@router.get("/persona-posts/{post_id}/speech")
async def persona_post_speech(request: Request, post_id: str):
    """用人物自己的声音朗读一条动态（公共内容，可被 <audio> 直接播放）。

    合成结果落盘缓存：同一条动态只合成一次。TTS 失败时返回 503，
    前端可回落到 /api/v2/tts/sample。
    """
    service = _v3(request)
    post = await _run(service.get_persona_post_row, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="动态不存在")
    persona = await _run(service.persona_by_id, str(post["persona_id"]))
    if persona is None:
        raise HTTPException(status_code=404, detail="人物不存在")

    services = getattr(request.app.state, "v2", None)
    voice_engine = getattr(services, "voice", None) if services is not None else None
    if voice_engine is None:
        raise HTTPException(status_code=503, detail="语音引擎未就绪")

    try:
        path, _duration = await voice_engine.synth_to_wav(
            str(post["content"])[:280],
            str(persona.get("voice_id") or "en-GB-RyanNeural"),
            out_dir=services.audio.audio_dir,
        )
    except Exception as exc:  # noqa: BLE001 - 合成失败由前端回落
        log.warning("动态朗读合成失败 %s: %s", post_id, exc)
        raise HTTPException(status_code=503, detail="语音合成失败") from exc

    return FileResponse(
        path,
        media_type="audio/wav",
        filename=os.path.basename(path),
        headers={"Cache-Control": "public, max-age=86400"},
    )


# --------------------------------------------------------------------------- #
# 好友关系
# --------------------------------------------------------------------------- #
@router.post("/personas/{persona_id}/friend")
async def add_persona_friend(
    request: Request,
    persona_id: str,
    body: FriendBody | None = None,
    user_id: str = Depends(bind_current_user),
) -> dict:
    service = _v3(request)
    try:
        result = await _run(
            service.add_friend, user_id, persona_id, request_message=(body.message if body else "") or ""
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="人物不存在") from None
    return result


@router.delete("/personas/{persona_id}/friend")
async def remove_persona_friend(
    request: Request, persona_id: str, user_id: str = Depends(bind_current_user)
) -> dict:
    service = _v3(request)
    removed = await _run(service.remove_friend, user_id, persona_id)
    if not removed:
        raise HTTPException(status_code=404, detail="好友关系不存在")
    return {"removed": True}


@router.get("/friendships")
async def list_friendships(
    request: Request, user_id: str = Depends(bind_current_user)
) -> dict:
    return await _run(_v3(request).list_friendships, user_id)


# --------------------------------------------------------------------------- #
# 用户动态
# --------------------------------------------------------------------------- #
@router.get("/posts/mine")
async def my_posts(
    request: Request,
    limit: int = Query(default=30, ge=1, le=100),
    user_id: str = Depends(bind_current_user),
) -> dict:
    service = _v3(request)
    return {"items": await _run(service.list_user_posts, user_id, limit=limit)}


@router.post("/posts")
async def create_post(
    request: Request, body: UserPostIn, user_id: str = Depends(bind_current_user)
) -> dict:
    service = _v3(request)
    try:
        post = await _run(
            service.create_user_post,
            user_id,
            body.content,
            media=body.media,
            tags=body.tags,
            visibility=body.visibility,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    social: dict = {"scheduled": False, "instant": False, "events": []}
    if body.visibility == "public":
        try:
            if os.getenv("SE_SOCIAL_SYNC", "0").strip() in ("1", "true", "yes"):
                result = await service.trigger_social_events(user_id, post)
                social = {"scheduled": bool(result.get("events")), "instant": True,
                          "events": result.get("events") or [], "skipped": result.get("skipped")}
            else:
                services = getattr(request.app.state, "v2", None)
                coro = service.trigger_social_events(user_id, post)
                if services is not None and hasattr(services, "spawn_bg"):
                    services.spawn_bg(coro)
                    social = {"scheduled": True, "instant": False, "events": []}
                else:  # pragma: no cover - 无后台任务时同步执行
                    result = await coro
                    social = {"scheduled": bool(result.get("events")), "instant": True,
                              "events": result.get("events") or []}
        except Exception as exc:  # noqa: BLE001 - 事件失败不影响发布
            log.warning("社交事件触发失败: %s", exc)
    return {"post": post, "social": social}


@router.delete("/posts/{post_id}")
async def delete_post(
    request: Request, post_id: str, user_id: str = Depends(bind_current_user)
) -> dict:
    service = _v3(request)
    ok = await _run(service.delete_user_post, user_id, post_id)
    if not ok:
        raise HTTPException(status_code=404, detail="动态不存在")
    return {"deleted": True}


# --------------------------------------------------------------------------- #
# 互动
# --------------------------------------------------------------------------- #
@router.post("/persona-posts/{post_id}/like")
async def like_persona_post(
    request: Request, post_id: str, user_id: str = Depends(bind_current_user)
) -> dict:
    service = _v3(request)
    try:
        return await _run(service.toggle_persona_post_like, user_id, post_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="动态不存在") from None


@router.post("/persona-posts/{post_id}/comments")
async def comment_persona_post(
    request: Request,
    post_id: str,
    body: CommentIn,
    user_id: str = Depends(bind_current_user),
) -> dict:
    service = _v3(request)
    try:
        return await _run(service.comment_persona_post, user_id, post_id, body.content)
    except KeyError:
        raise HTTPException(status_code=404, detail="动态不存在") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --------------------------------------------------------------------------- #
# 社交事件
# --------------------------------------------------------------------------- #
@router.get("/events")
async def list_events(
    request: Request,
    unread_only: bool = False,
    limit: int = Query(default=50, ge=1, le=100),
    user_id: str = Depends(bind_current_user),
) -> dict:
    service = _v3(request)
    return await _run(service.list_events, user_id, unread_only=unread_only, limit=limit)


@router.post("/events/read-all")
async def read_all_events(
    request: Request, user_id: str = Depends(bind_current_user)
) -> dict:
    service = _v3(request)
    return {"updated": await _run(service.mark_all_events_read, user_id)}


@router.post("/events/{event_id}/read")
async def read_event(
    request: Request,
    event_id: str,
    body: ReadBody | None = None,
    user_id: str = Depends(bind_current_user),
) -> dict:
    service = _v3(request)
    read = True if body is None else body.read
    ok = await _run(service.mark_event_read, user_id, event_id, read=read)
    if not ok:
        raise HTTPException(status_code=404, detail="事件不存在")
    return {"read": read}


@router.post("/events/{event_id}/accept")
async def accept_event(
    request: Request, event_id: str, user_id: str = Depends(bind_current_user)
) -> dict:
    service = _v3(request)
    result = await _run(service.respond_friend_request, user_id, event_id, accept=True)
    if result is None:
        raise HTTPException(status_code=404, detail="事件不存在或不是好友申请")
    return result


@router.post("/events/{event_id}/decline")
async def decline_event(
    request: Request, event_id: str, user_id: str = Depends(bind_current_user)
) -> dict:
    service = _v3(request)
    result = await _run(service.respond_friend_request, user_id, event_id, accept=False)
    if result is None:
        raise HTTPException(status_code=404, detail="事件不存在或不是好友申请")
    return result


@router.get("/social/stats")
async def social_stats(
    request: Request, user_id: str = Depends(bind_current_user)
) -> dict:
    return await _run(_v3(request).stats, user_id)


__all__ = ["V3_VERSION", "router"]
