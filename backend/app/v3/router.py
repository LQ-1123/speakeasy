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

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
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


def _mark_interaction(request: Request, service, *, persona_id: str = "", post_id: str = "") -> None:
    """记录真实互动（失败不影响主流程）。"""
    try:
        from app.v3.lifecycle import mark_interaction  # noqa: PLC0415

        mark_interaction(service.db, persona_id=persona_id, post_id=post_id)
    except Exception:  # noqa: BLE001
        pass


async def _run(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


def _warm_audio(request: Request, service: V3Service, items: list[dict]) -> int:
    """把这一页 Persona 动态放进朗读预热队列（不阻塞响应）。"""
    if getattr(request.app.state, "background_jobs", None) is not None:
        # Production maintenance owns bounded, call-preemptible audio prewarming.
        return 0
    cache = getattr(request.app.state, "v3_audio", None)
    if cache is None or not items:
        return 0
    queued = 0
    for item in items:
        if item.get("kind") != "persona_post":
            continue
        try:
            post = service.get_persona_post_row(str(item.get("id") or ""))
            persona = service.persona_by_id(str(post["persona_id"])) if post else None
            if post and persona:
                cache.enqueue(post, persona)
                queued += 1
        except Exception:  # noqa: BLE001 - 预热失败不影响返回
            continue
    return queued


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
    audio_cache = getattr(request.app.state, "v3_audio", None)
    audio_stats = audio_cache.stats() if audio_cache is not None else {}
    daily: dict = {}
    if db is not None:
        try:
            from app.v3.daily_world import daily_status  # noqa: PLC0415

            daily = await asyncio.to_thread(daily_status, db)
        except Exception:  # noqa: BLE001
            daily = {}
    voice: dict = {}
    if db is not None:
        try:
            from app.voice import voice_registry  # noqa: PLC0415

            cfg = await asyncio.to_thread(voice_registry.load_cfg)
            status = voice_registry.fish_status()
            # Fish 曾失败且冷却已过：顺手做一次轻量探测（不合成音频），
            # 网络恢复后四条链路会自动切回 Fish。
            if cfg is not None and status.get("attempt_ok") is not True and status.get("retry_in_s", 0) <= 0:
                await voice_registry.probe(cfg)
                status = voice_registry.fish_status()
            rows = await _run(
                lambda: service.db.conn.execute(
                    "SELECT COUNT(*) AS n,"
                    " SUM(CASE WHEN COALESCE(fish_voice_id,'')<>'' THEN 1 ELSE 0 END) AS with_fish"
                    " FROM personas"
                ).fetchone()
            )
            voice = {
                "provider": str(getattr(cfg, "provider", "") or "none"),
                "fish": status,
                "personas_total": int(rows["n"] or 0) if rows else 0,
                "personas_with_fish": int(rows["with_fish"] or 0) if rows else 0,
                # 一致性契约：这些链路共用同一个 resolver
                "paths": ["chat", "post_speech", "call", "sample"],
            }
        except Exception as exc:  # noqa: BLE001 - 健康检查不因诊断失败而失败
            voice = {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "status": "ok",
        "version": V3_VERSION,
        "social": service.config.public(),
        "world": world,
        "audio": audio_stats,
        "daily": daily,
        "voice": voice,
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
    data = await _run(service.feed, user_id, cursor=cursor, limit=limit)
    # 毫秒级朗读：本页动态立刻进后台合成队列，用户点「朗读」时文件通常已就绪
    try:
        await asyncio.to_thread(_warm_audio, request, service, data.get("items") or [])
    except Exception:  # noqa: BLE001
        pass
    return data


@router.get("/feed/summary")
async def feed_summary(
    request: Request, user_id: str = Depends(bind_current_user)
) -> dict:
    return await _run(_v3(request).feed_summary, user_id)


# --------------------------------------------------------------------------- #
# Persona
# --------------------------------------------------------------------------- #
@router.get("/friends/{friend_id}/posts")
async def friend_posts(
    request: Request,
    friend_id: str,
    limit: int = Query(default=6, ge=1, le=20),
    user_id: str = Depends(bind_current_user),
) -> dict:
    """好友资料页「TA 的动态」：人物帖子 / 老好友的朋友圈。"""
    service = _v3(request)
    return await _run(service.friend_posts, user_id, friend_id, limit=limit)


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
    _mark_interaction(request, service, persona_id=persona_id)
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


# --------------------------------------------------------------------------- #
# 种子头像（确定性 SVG，无图片资源）
# --------------------------------------------------------------------------- #
class ProfileIn(BaseModel):
    nickname: str | None = None
    target_languages: list[str] | None = None
    birthday: str | None = None
    city: str | None = None
    job: str | None = None
    gender: str | None = None
    interests: list[str] | None = None
    native_language: str | None = None
    level: str | None = None
    levels: dict[str, str] | None = None


@router.put("/profile")
async def update_profile(
    request: Request,
    body: ProfileIn,
    user_id: str = Depends(bind_current_user),
) -> dict:
    """更新个人资料：昵称/目标语言/生日/城市/职业/兴趣标签（兴趣进 fact_sheet）。"""
    service = _v3(request)
    profile = await _run(
        service.update_profile_fields,
        nickname=body.nickname,
        targets=body.target_languages,
        birthday=body.birthday,
        city=body.city,
        job=body.job,
        gender=body.gender,
        interests=body.interests,
        native_language=body.native_language,
        level=body.level,
        levels=body.levels,
        user_id=user_id,
    )
    return {"profile": profile}


@router.post("/media/upload")
async def upload_media(
    request: Request,
    file: UploadFile = File(...),
    user_id: str = Depends(bind_current_user),
) -> dict:
    """上传一张动态配图：校验 → 去 EXIF → 1600px → webp，返回 /uploads/... URL。"""
    service = _v3(request)
    from app.v3.uploads import MAX_BYTES, UploadError, save_user_image  # noqa: PLC0415

    raw = await file.read(MAX_BYTES + 1)
    try:
        data = await asyncio.to_thread(
            save_user_image,
            raw,
            user_id,
            os.path.join(service.data_dir or "data", "uploads"),
        )
    except UploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return data


@router.get("/world/daily")
async def world_daily(request: Request) -> dict:
    """今日世界状态：生成时间、今日人物/动态数、可见动态数、下次生成时间。"""
    service = _v3(request)
    from app.v3.daily_world import daily_status  # noqa: PLC0415

    return await asyncio.to_thread(daily_status, service.db)


@router.get("/avatar/{seed}.svg")
async def avatar(seed: str, g: str = "", r: str = "", a: int | None = None):
    """按种子生成插画头像。

    - 同一 seed 永远返回同一张脸（sha256 派生五官/发型/配色）
    - ``g`` = male/female，``r`` = 地域（east_asian/latino/...），只影响肤色/发型/胡子
    - 纯函数、无状态，可直接长缓存
    """
    from app.v3.avatar import avatar_svg  # noqa: PLC0415

    svg = await asyncio.to_thread(avatar_svg, seed, gender=g, region=r, age=a)
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Avatar-Seed": seed,
        },
    )


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
    # 偶遇卡片上的「听 TA 的声音」也提前合成
    cache = getattr(request.app.state, "v3_audio", None)
    if cache is not None and data.get("post_id"):
        try:
            post = service.get_persona_post_row(str(data["post_id"]))
            persona = service.persona_by_id(str(data["persona"]["id"]))
            if post and persona:
                cache.enqueue(post, persona)
        except Exception:  # noqa: BLE001
            pass
    return data


@router.get("/persona-posts/{post_id}/speech")
async def persona_post_speech(request: Request, post_id: str, v: str | None = None):
    """朗读一条动态（公共内容，可被 <audio> 直接播放）。

    **毫秒级**：朗读音频在后台按「信息流 / 偶遇」的浏览顺序提前合成好并落盘，
    点击时只做文件读取（~几毫秒）；没预热到才同步合成兜底。

    规则：发帖语言决定朗读声线。
    - 帖子语言 == 人物母语 → 用人物自己的声线（保持角色一致）
    - 否则 → 按帖子语言挑一条该语言的声线
    - ``?v=<content_hash>`` 只用于缓存版本：内容变了 URL 就变
    """
    service = _v3(request)
    post = await _run(service.get_persona_post_row, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="动态不存在")
    persona = await _run(service.persona_by_id, str(post["persona_id"]))
    if persona is None:
        raise HTTPException(status_code=404, detail="人物不存在")

    _mark_interaction(request, service, post_id=post_id)

    cache = getattr(request.app.state, "v3_audio", None)
    if cache is not None:
        try:
            path, lang, voice = await cache.get_or_synth(post, persona)
            media_type = "audio/mpeg" if str(path).endswith(".mp3") else "audio/wav"
            return FileResponse(
                path,
                media_type=media_type,
                filename=os.path.basename(path),
                headers={
                    "Cache-Control": "public, max-age=31536000, immutable",
                    "X-Voice": voice,
                    "X-Voice-Provider": ("fish" if "-fish-fish-" in os.path.basename(str(path)) or "-fish-edge-" in os.path.basename(str(path)) else "edge"),
                    "X-Language": lang,
                    "X-Audio-Cached": "1",
                },
            )
        except Exception as exc:  # noqa: BLE001 - 落到下面的同步兜底
            log.warning("朗读缓存失败，改同步合成 %s: %s", post_id, exc)

    services = getattr(request.app.state, "v2", None)
    voice_engine = getattr(services, "voice", None) if services is not None else None
    if voice_engine is None:
        raise HTTPException(status_code=503, detail="语音引擎未就绪")

    from app.v3.seed_data import detect_language, voice_for_language  # noqa: PLC0415

    content = str(post["content"] or "")
    persona_lang = str(persona.get("language") or "en").strip().lower()
    post_lang = (
        str(post.get("language") or "").strip().lower()
        or detect_language(content)
        or persona_lang
    )
    persona_voice = str(persona.get("voice_id") or "")
    if post_lang == persona_lang and persona_voice:
        voice = persona_voice
    else:
        voice = voice_for_language(post_lang, seed=str(persona.get("id") or ""),
                                   gender=str(persona.get("gender") or ""))

    try:
        path, _duration = await voice_engine.synth_to_wav(
            content[:280], voice, out_dir=services.audio.audio_dir
        )
    except Exception as exc:  # noqa: BLE001 - 合成失败由前端回落
        log.warning("动态朗读合成失败 %s: %s", post_id, exc)
        raise HTTPException(status_code=503, detail="语音合成失败") from exc

    return FileResponse(
        path,
        media_type="audio/wav",
        filename=os.path.basename(path),
        # URL 带内容版本（?v=hash）后才允许长缓存；没带版本就只缓存一次请求周期
        headers={
            "Cache-Control": "public, max-age=31536000, immutable" if v else "no-cache",
            "X-Voice": voice,
            "X-Language": post_lang,
        },
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
    _mark_interaction(request, service, persona_id=persona_id)
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
        result = await _run(service.toggle_persona_post_like, user_id, post_id)
        _mark_interaction(request, service, post_id=post_id)
        return result
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
        result = await _run(service.comment_persona_post, user_id, post_id, body.content)
        _mark_interaction(request, service, post_id=post_id)
        return result
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


@router.get("/stats/persona")
async def persona_stats(request: Request) -> dict:
    """Persona 生活系统只读观测（调参/验收用）。

    只做聚合查询：不触发任何 LLM / 不写库 / 不包含任何用户数据。
    """
    return await _run(_v3(request).stats_persona)


@router.get("/stats/voice")
async def voice_stats(request: Request, persona_id: str = "") -> dict:
    """音色一致性自检（只读，不合成音频）。

    在**服务端进程内**同时算「聊天侧绑定」和「朗读侧绑定」，
    两者必须完全一致——这正是用户反馈「消息和动态声音不一样」的验收点。
    """
    service = _v3(request)
    from app.voice import voice_registry  # noqa: PLC0415

    cfg = await asyncio.to_thread(voice_registry.load_cfg)
    db = service.db

    def _resolve(pid: str) -> dict | None:
        persona = service.persona_by_id(pid)
        if persona is None:
            return None
        speech_binding = voice_registry.binding_for_persona(persona, cfg)
        chat_binding = None
        try:
            row = db.conn.execute(
                "SELECT * FROM friends WHERE persona_id=? LIMIT 1", (pid,)
            ).fetchone()
            if row is not None:
                chat_binding = voice_registry.binding_for_friend(db, dict(row), cfg)
        except Exception:  # noqa: BLE001
            chat_binding = None
        return {
            "persona_id": pid,
            "nickname": str(persona.get("nickname") or ""),
            "speech": {
                "provider": speech_binding.provider,
                "token": speech_binding.token,
                "edge_voice": speech_binding.edge_voice,
            },
            "chat": (
                {
                    "provider": chat_binding.provider,
                    "token": chat_binding.token,
                    "edge_voice": chat_binding.edge_voice,
                }
                if chat_binding is not None
                else None
            ),
            "consistent": (
                chat_binding is None or chat_binding.token == speech_binding.token
            ),
        }

    if persona_id:
        ids = [persona_id]
    else:
        ids = [
            str(r[0])
            for r in db.conn.execute(
                "SELECT id FROM personas ORDER BY nickname LIMIT 6"
            ).fetchall()
        ]
    items = [item for item in (_resolve(pid) for pid in ids) if item is not None]
    return {
        "provider": str(getattr(cfg, "provider", "") or "none"),
        "fish": voice_registry.fish_status(),
        "paths": ["chat", "post_speech", "call", "sample"],
        "items": items,
        "all_consistent": all(bool(item["consistent"]) for item in items),
    }


__all__ = ["V3_VERSION", "router"]
