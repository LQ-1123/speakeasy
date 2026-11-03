"""SpeakEasy 2.0 REST 路由（``/api/v2``）。

消息条、好友、朋友圈、表达本、记忆、音频。所有数据库同步调用都走
``asyncio.to_thread``；LLM/TTS 失败由下层降级，接口尽量不 500。
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.llm import runtime_config as llm_runtime
from app.logging_utils import get_logger
from app.v2 import moments as moments_mod
from app.v2 import translate as translate_mod
from app.v2 import user_moments as user_moments_mod
from app.v2.timeutil import local_date_str, local_state
from app.v2.voices import load_archetypes
from app.voice.stt_config import (
    STT_PRESETS,
    STTRuntimeConfig,
    STTConfigStore,
    verify_config as verify_stt_config,
)

log = get_logger(__name__)

router = APIRouter(prefix="/api/v2", tags=["v2"])

V2_VERSION = "2.0.0"


# --------------------------------------------------------------------------- #
# 请求体
# --------------------------------------------------------------------------- #
class DiscoverBody(BaseModel):
    nationality: str | None = None
    language: str | None = None
    gender: str | None = None


class TextTurnBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    context: str | None = None


class ExpressionBody(BaseModel):
    original_text: str = ""
    better_expression: str = Field(min_length=1, max_length=500)
    explanation: str | None = None
    source_friend_id: str | None = None


class ExpressionPatch(BaseModel):
    status: str | None = None
    mastery_count: int | None = None


class MemoryUpdate(BaseModel):
    nickname: str | None = None
    native_language: str | None = None
    target_languages: list[str] | None = None
    fact_sheet: dict | None = None


class FriendMemoryBody(BaseModel):
    shared_episodes: list[str] | None = None
    open_loops: list[dict] | None = None


class SttConfigIn(BaseModel):
    """个人设置：保存 STT 配置（api_key 留空=不改，__CLEAR__=清空）。"""

    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    language: str | None = None
    timeout_s: float | None = None
    verify: bool = True


class TranslateIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    target: str | None = None
    source: str = "auto"
    friend_id: str | None = None


class TranslateBatchIn(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=20)
    target: str | None = None
    friend_id: str | None = None


class CallSummaryIn(BaseModel):
    duration_s: int = Field(0, ge=0, le=36000)
    turns: int = Field(0, ge=0, le=2000)


class UserMomentIn(BaseModel):
    content: str = Field(min_length=1, max_length=500)
    mood_tag: str = Field("", max_length=30)


class SttTestIn(BaseModel):
    """只测不存；缺省字段沿用当前配置。"""

    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    language: str | None = None
    timeout_s: float | None = None


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _services(request: Request) -> Any:
    services = getattr(request.app.state, "v2", None)
    if services is None:  # pragma: no cover - 仅在装配遗漏时
        raise HTTPException(status_code=503, detail="v2 services 未初始化")
    return services


def _thread(fn, *args, **kwargs):
    return asyncio.to_thread(fn, *args, **kwargs)


def _message_public(msg: dict | None) -> dict | None:
    if msg is None:
        return None
    data = dict(msg)
    name = data.get("audio_path")
    data["audio_url"] = f"/api/v2/audio/{os.path.basename(name)}" if name else None
    image = data.get("image_path")
    data["image_url"] = f"/api/v2/media/{os.path.basename(image)}" if image else None
    return data


def _user_today() -> str:
    """用户本机时区的日期（朋友圈幂等）。"""
    return datetime.now().astimezone().strftime("%Y-%m-%d")


async def _friend_public(services: Any, friend: dict) -> dict:
    db = services.db
    fid = friend["id"]
    unread, last = await asyncio.gather(
        _thread(db.unread_count, fid), _thread(db.last_message, fid)
    )
    state = local_state(friend.get("timezone") or "UTC")
    loops = friend.get("open_loops") or []
    now = datetime.now(timezone.utc)
    due = 0
    for loop in loops:
        if (loop or {}).get("status") != "pending":
            continue
        try:
            when = datetime.fromisoformat(str(loop.get("follow_up_after")).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when <= now:
                due += 1
        except (TypeError, ValueError):
            continue
    data = dict(friend)
    data.update(
        {
            "local_time": state["local_time"],
            "local_period": state["period"],
            "local_icon": state["icon"],
            "local_activity": state["activity"],
            "local_weekday": state["weekday"],
            "is_awake": state["is_awake"],
            "unread": unread,
            "open_loop_due": due,
            "replying": bool(
                getattr(getattr(services, "chat", None), "is_replying", lambda _fid: False)(fid)
            ),
            "last_message": _public_preview(last),
        }
    )
    return data


def _public_preview(msg: dict | None) -> dict | None:
    if msg is None:
        return None
    return {
        "sender": msg.get("sender"),
        "transcript": msg.get("transcript"),
        "msg_type": msg.get("msg_type"),
        "duration_ms": msg.get("duration_ms"),
        "created_at": msg.get("created_at"),
    }


async def _friend_memory(friend: dict) -> dict:
    return {
        "shared_episodes": friend.get("shared_episodes") or [],
        "open_loops": friend.get("open_loops") or [],
        "closeness_level": friend.get("closeness_level", 1),
        "total_turns": friend.get("total_turns", 0),
    }


# --------------------------------------------------------------------------- #
# 运行时配置（LLM + STT）——个人设置页
# --------------------------------------------------------------------------- #
def _engines_obj(request: Request) -> Any:
    engines = getattr(request.app.state, "engines", None)
    if engines is None:
        raise HTTPException(status_code=503, detail="引擎未初始化")
    return engines


def _llm_config_payload(engines: Any) -> dict:
    store = getattr(engines, "llm_config", None)
    if store is None:
        settings = engines.settings
        store = llm_runtime.LLMConfigStore(
            settings.data_path,
            env_defaults={
                "base_url": settings.llm.base_url,
                "api_key": settings.llm.api_key,
                "model": settings.llm.model,
                "suggestion_model": settings.llm.suggestion_model,
                "temperature": settings.llm.temperature,
                "max_tokens": settings.llm.max_tokens,
            },
        )
        engines.llm_config = store
    try:
        health = engines.llm.health()
    except Exception as exc:  # noqa: BLE001
        health = {"configured": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"config": store.public(), "health": health, "presets": llm_runtime.PROVIDER_PRESETS}


def _get_stt_store(engines: Any) -> STTConfigStore:
    store = getattr(engines, "stt_config", None)
    if store is None:
        settings = getattr(engines, "settings", None)
        stt = getattr(settings, "stt", None)
        store = STTConfigStore(
            getattr(settings, "data_path", "") or "data",
            env_defaults={
                "provider": getattr(stt, "provider", "auto"),
                "base_url": getattr(stt, "siliconflow_base_url", ""),
                "model": getattr(stt, "siliconflow_model", ""),
                "api_key": getattr(stt, "siliconflow_api_key", ""),
                "language": getattr(stt, "language", "en"),
                "timeout_s": getattr(stt, "siliconflow_timeout_s", 30.0),
            },
        )
        engines.stt_config = store
    return store


def _apply_stt_config(engines: Any, cfg: STTRuntimeConfig) -> None:
    apply_fn = getattr(engines.stt, "apply_cloud_config", None)
    if callable(apply_fn):
        apply_fn(cfg.to_engine_dict())


def _stt_health(engines: Any) -> dict:
    fn = getattr(engines.stt, "cloud_health", None)
    if callable(fn):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            return {"enabled": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "enabled": False,
        "provider": "auto",
        "configured": False,
        "backend": getattr(engines.stt, "backend_name", "unknown"),
        "model": "",
        "last_error": "STTEngine 未实现 cloud_health",
        "last_latency_ms": None,
        "requests": 0,
        "last_success_at": None,
    }


def _stt_payload(engines: Any) -> dict:
    store = _get_stt_store(engines)
    return {
        "config": store.public(),
        "health": _stt_health(engines),
        "presets": STT_PRESETS,
    }


@router.get("/settings")
async def get_settings(request: Request) -> dict:
    """个人设置首屏：LLM + STT + TTS 当前状态。"""
    engines = _engines_obj(request)
    return {
        "llm": _llm_config_payload(engines),
        "stt": _stt_payload(engines),
        "tts": {"backend": getattr(engines.tts, "backend_name", "unknown")},
    }


@router.get("/settings/stt")
async def get_stt_settings(request: Request) -> dict:
    return _stt_payload(_engines_obj(request))


@router.post("/settings/stt")
async def save_stt_settings(request: Request, body: SttConfigIn) -> dict:
    engines = _engines_obj(request)
    store = _get_stt_store(engines)
    changes = body.model_dump(exclude_none=True, exclude={"verify"})
    try:
        cfg = await asyncio.to_thread(store.save, **changes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await asyncio.to_thread(_apply_stt_config, engines, cfg)
    verified = None
    if body.verify:
        verified = await verify_stt_config(cfg)
    return {
        "ok": True,
        "config": cfg.public(),
        "health": _stt_health(engines),
        "verified": verified,
    }


@router.post("/settings/stt/test")
async def test_stt_settings(request: Request, body: SttTestIn | None = None) -> dict:
    engines = _engines_obj(request)
    store = _get_stt_store(engines)
    body = body or SttTestIn()
    current = store.config
    changes: dict[str, Any] = {}
    for name in ("provider", "base_url", "model", "language", "timeout_s"):
        value = getattr(body, name)
        if value is not None and value != "":
            changes[name] = value
    if body.api_key:
        changes["api_key"] = body.api_key
    merged = {
        "provider": changes.get("provider", current.provider),
        "base_url": changes.get("base_url", current.base_url),
        "model": changes.get("model", current.model),
        "api_key": changes.get("api_key", current.api_key),
        "language": changes.get("language", current.language),
        "timeout_s": changes.get("timeout_s", current.timeout_s),
    }
    temp = STTRuntimeConfig(**merged, source="test")
    if temp.provider not in ("auto", "local", "siliconflow"):
        raise HTTPException(status_code=422, detail="provider 不合法")
    return await verify_stt_config(temp)


@router.post("/settings/stt/reset")
async def reset_stt_settings(request: Request) -> dict:
    engines = _engines_obj(request)
    store = _get_stt_store(engines)
    cfg = await asyncio.to_thread(store.reset)
    await asyncio.to_thread(_apply_stt_config, engines, cfg)
    return {"config": cfg.public(), "health": _stt_health(engines)}


# --------------------------------------------------------------------------- #
# 引导 & 好友
# --------------------------------------------------------------------------- #
async def _send_icebreaker(services: Any, friend: dict) -> dict:
    """新好友的第一条语音条。"""
    try:
        text = await services.spawner.icebreaker(friend)
    except Exception as exc:  # noqa: BLE001
        log.warning("icebreaker 生成失败（用兜底）: %s", exc)
        text = f"Hey! I'm {friend.get('name', 'your new friend')}. What are you up to today?"
    return await services.chat.store_friend_voice(friend, text)


async def _run_onboarding(services: Any) -> None:
    """PRD §3.2.1：默认 2 位性格反差明显的好友。

    第 1 位：英音（温和、语速适中，适合破冰）；
    第 2 位：带鲜明口音/文化的跨文化朋友（意大利，Multilingual 声线）。
    spawner 的过滤是分级放宽的，池子不够时不会失败。
    """
    picks = (
        {"nationality": "United Kingdom"},
        {"nationality": "Italy"},
    )
    try:
        for pick in picks:
            friend = await services.spawner.spawn(**pick)
            await _send_icebreaker(services, friend)
            log.info("onboarding: 已生成好友 %s (%s)", friend.get("name"), friend.get("city"))
    except Exception as exc:  # noqa: BLE001
        log.warning("onboarding 失败: %s: %s", type(exc).__name__, exc)
    finally:
        services.onboarding_task = None


def _ensure_onboarding(services: Any, all_friends: list[dict]) -> bool:
    if all_friends or services.onboarding_pending:
        return services.onboarding_pending
    services.onboarding_task = services.spawn_bg(_run_onboarding(services))
    return True


async def _send_followup(services: Any, friend: dict, loop: dict) -> None:
    fid = friend["id"]
    try:
        text = await services.memory.followup_message(friend, loop)
        await services.chat.store_friend_voice(friend, text)
        raw = await _thread(services.db.get_friend, fid) or friend
        loops = raw.get("open_loops") or []
        idx = next(
            (i for i, item in enumerate(loops) if (item or {}).get("event") == loop.get("event")),
            None,
        )
        if idx is not None:
            await _thread(services.memory.mark_open_loop_done, fid, idx)
        log.info("open loop 追问已发送 friend=%s: %s", fid, loop.get("event"))
    except Exception as exc:  # noqa: BLE001
        log.warning("open loop 追问失败: %s: %s", type(exc).__name__, exc)
    finally:
        services.followup_inflight.discard(fid)


async def _check_followups(services: Any, active: list[dict]) -> None:
    for friend in active:
        fid = friend["id"]
        if fid in services.followup_inflight:
            continue
        try:
            due = services.memory.due_open_loops(friend)
        except Exception as exc:  # noqa: BLE001
            log.debug("due_open_loops 失败: %s", exc)
            continue
        if due:
            services.followup_inflight.add(fid)
            services.spawn_bg(_send_followup(services, friend, due[0]))


async def _friends_payload(services: Any) -> dict:
    all_friends = await _thread(services.db.list_friends)
    pending_onboarding = _ensure_onboarding(services, all_friends)
    active = [f for f in all_friends if f.get("status") == "active"]
    requests_ = [f for f in all_friends if f.get("status") == "pending_request"]
    await _check_followups(services, active)
    friends_public = await asyncio.gather(*(_friend_public(services, f) for f in active))
    requests_public = await asyncio.gather(*(_friend_public(services, f) for f in requests_))
    return {
        "friends": list(friends_public),
        "requests": list(requests_public),
        "pending_onboarding": pending_onboarding,
    }


@router.get("/bootstrap")
async def bootstrap(request: Request) -> dict:
    services = _services(request)
    payload = await _friends_payload(services)
    profile = await _thread(services.db.get_profile)
    expressions = await _thread(services.db.list_expressions)
    engines = services.engines
    return {
        "version": V2_VERSION,
        "profile": profile,
        "llm": engines.llm.health(),
        "stt": _stt_payload(engines),
        "audio": {"format": "wav", "sample_rate": 24000},
        **payload,
        "stats": {
            "friends": len(payload["friends"]),
            "requests": len(payload["requests"]),
            "expressions": len(expressions),
            "learning": sum(1 for e in expressions if e.get("status") == "learning"),
            "mastered": sum(1 for e in expressions if e.get("status") == "mastered"),
        },
    }


@router.get("/friends")
async def list_friends(request: Request, status: str | None = None) -> dict:
    services = _services(request)
    payload = await _friends_payload(services)
    if status == "pending_request":
        return {"friends": payload["requests"], "requests": payload["requests"],
                "pending_onboarding": payload["pending_onboarding"]}
    return payload


@router.post("/friends/discover")
async def discover_friend(request: Request, body: DiscoverBody | None = None) -> dict:
    services = _services(request)
    body = body or DiscoverBody()
    has_filters = bool(
        (body.nationality or "").strip() or (body.language or "").strip() or (body.gender or "").strip()
    )
    try:
        friend = await services.spawner.spawn(
            nationality=body.nationality,
            language=body.language,
            gender=body.gender,
            status="active",
            strict_filters=has_filters,
        )
    except Exception as exc:  # noqa: BLE001
        # 用户明确筛了国家/语种时不能静默换人：返回 422 + 可读原因
        from app.v2.spawner import ArchetypeNotFound  # noqa: PLC0415

        if isinstance(exc, ArchetypeNotFound):
            raise HTTPException(
                status_code=422,
                detail=getattr(exc, "detail", None) or str(exc),
            ) from exc
        log.exception("discover 失败")
        raise HTTPException(status_code=502, detail=f"捏人失败: {exc}") from exc
    message = await _send_icebreaker(services, friend)
    return {
        "friend": await _friend_public(services, friend),
        "message": _message_public(message),
    }


@router.get("/friends/{friend_id}")
async def get_friend(request: Request, friend_id: str) -> dict:
    services = _services(request)
    friend = await _thread(services.db.get_friend, friend_id)
    if friend is None:
        raise HTTPException(status_code=404, detail="好友不存在")
    return {
        "friend": await _friend_public(services, friend),
        "memory": await _friend_memory(friend),
        "request_message": friend.get("request_message") or "",
    }


@router.post("/friends/{friend_id}/accept")
async def accept_friend(request: Request, friend_id: str) -> dict:
    services = _services(request)
    friend = await _thread(services.db.get_friend, friend_id)
    if friend is None:
        raise HTTPException(status_code=404, detail="好友不存在")
    if friend.get("status") != "active":
        friend = await _thread(services.db.update_friend, friend_id, status="active")
    message = await _send_icebreaker(services, friend)
    return {"friend": await _friend_public(services, friend), "message": _message_public(message)}


@router.post("/friends/{friend_id}/decline")
async def decline_friend(request: Request, friend_id: str) -> dict:
    services = _services(request)
    friend = await _thread(services.db.get_friend, friend_id)
    if friend is None:
        raise HTTPException(status_code=404, detail="好友不存在")
    await _thread(services.db.delete_friend, friend_id)
    return {"ok": True}


@router.delete("/friends/{friend_id}")
async def delete_friend(request: Request, friend_id: str) -> dict:
    """删除好友：级联删除聊天记录/朋友圈，并清理对应音频文件。"""
    services = _services(request)
    friend = await _thread(services.db.get_friend, friend_id)
    if friend is None:
        raise HTTPException(status_code=404, detail="好友不存在")
    audio_names = await _thread(services.db.friend_audio_paths, friend_id)
    await _thread(services.db.delete_friend, friend_id)
    services.followup_inflight.discard(friend_id)
    removed = 0
    for name in audio_names:
        try:
            path = services.audio.path_for(name)
            if os.path.isfile(path):
                os.remove(path)
                removed += 1
        except (OSError, ValueError) as exc:  # noqa: BLE001 - 清理失败不影响删除
            log.debug("删除好友音频失败 %s: %s", name, exc)
    log.info(
        "删除好友: %s (%s/%s) | 音频清理 %d/%d",
        friend.get("name"), friend.get("city"), friend.get("nationality"),
        removed, len(audio_names),
    )
    return {
        "ok": True,
        "friend_id": friend_id,
        "name": friend.get("name"),
        "deleted_audio": removed,
    }


@router.get("/friends/{friend_id}/status")
async def friend_status(request: Request, friend_id: str) -> dict:
    """轻量轮询：好友是否正在生成回复（「对方正在说话」跨页面/重进聊天仍可恢复）。"""
    services = _services(request)
    friend = await _thread(services.db.get_friend, friend_id)
    if friend is None:
        raise HTTPException(status_code=404, detail="好友不存在")
    chat = getattr(services, "chat", None)
    replying = bool(getattr(chat, "is_replying", lambda _fid: False)(friend_id))
    return {
        "friend_id": friend_id,
        "replying": replying,
        "started_at": getattr(chat, "replying_started_at", lambda _fid: None)(friend_id)
        if replying
        else None,
    }


@router.get("/friends/{friend_id}/messages")
async def friend_messages(
    request: Request, friend_id: str, limit: int = 30, before: str | None = None
) -> dict:
    services = _services(request)
    friend = await _thread(services.db.get_friend, friend_id)
    if friend is None:
        raise HTTPException(status_code=404, detail="好友不存在")
    rows = await _thread(services.db.list_messages, friend_id, limit=limit, before=before)
    return {
        "friend": await _friend_public(services, friend),
        "messages": [_message_public(m) for m in rows],
    }


@router.post("/friends/{friend_id}/read")
async def read_messages(request: Request, friend_id: str) -> dict:
    services = _services(request)
    await _thread(services.db.mark_read, friend_id)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 聊天（异步语音条）
# --------------------------------------------------------------------------- #
@router.get("/chat/{friend_id}/call/warmup")
async def call_warmup(request: Request, friend_id: str) -> dict:
    """通话接通预热：后台加载本地 STT 模型 + 预热 LLM 连接（不生成任何占位语音）。"""
    services = _services(request)
    friend = await _thread(services.db.get_friend, friend_id)
    if friend is None:
        raise HTTPException(status_code=404, detail="好友不存在")
    lang = str(friend.get("reply_language") or "en").strip().lower()
    stt_engine = getattr(services.engines, "stt", None)
    prewarm = getattr(stt_engine, "prewarm", None)
    if callable(prewarm):
        try:
            services.spawn_bg(prewarm(language=lang, fast=True))
        except TypeError:
            try:
                services.spawn_bg(prewarm(language=lang))
            except TypeError:
                services.spawn_bg(prewarm())
        except Exception:  # noqa: BLE001 - 预热失败不影响通话
            pass

    async def _warm_llm() -> None:
        """通话开始时用 1 token 预热 LLM 连接/冷启动，降低首轮 TTFT。"""
        llm = services.engines.llm
        if getattr(llm, "uses_mock", False):
            return
        try:
            async for _ in llm.stream_chat(
                [{"role": "user", "content": "Hi"}], max_tokens=1, temperature=0.0
            ):
                break
        except Exception as exc:  # noqa: BLE001 - 预热失败不影响通话
            log.debug("通话 LLM 预热失败: %s", exc)

    services.spawn_bg(_warm_llm())
    return {"ok": True}


@router.post("/chat/{friend_id}/call/turn")
async def call_turn_stream(
    request: Request,
    friend_id: str,
    audio: UploadFile = File(...),
    duration_ms: int = Form(0),
) -> StreamingResponse:
    """通话回合：NDJSON 流式返回 STT 转写 + 逐句语音，降低首字延迟。"""
    services = _services(request)
    friend = await _thread(services.db.get_friend, friend_id)
    if friend is None:
        raise HTTPException(status_code=404, detail="好友不存在")
    data = await audio.read()
    filename = audio.filename or "call.webm"

    async def gen():
        try:
            async for event in services.chat.handle_call_turn_stream(
                friend_id, data, filename, duration_ms
            ):
                if event.get("type") == "done":
                    event["user_message"] = _message_public(event.get("user_message"))
                    event["friend_message"] = _message_public(event.get("friend_message"))
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 流内错误转事件，避免连接直接断
            log.exception("通话流式回合失败: %s", exc)
            yield json.dumps(
                {"type": "error", "message": f"通话生成失败：{exc}"}, ensure_ascii=False
            ) + "\n"

    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/chat/{friend_id}/call/summary")
async def save_call_summary(
    request: Request, friend_id: str, body: CallSummaryIn
) -> dict:
    """通话结束落一条 call_summary 消息（进入聊天记录）。"""
    services = _services(request)
    friend = await _thread(services.db.get_friend, friend_id)
    if friend is None:
        raise HTTPException(status_code=404, detail="好友不存在")
    total = max(0, int(body.duration_s))
    minutes, seconds = divmod(total, 60)
    turns = max(0, int(body.turns))
    transcript = f"📞 语音通话 {minutes:02d}:{seconds:02d} · {turns} 轮对话"
    msg = await _thread(
        services.db.add_message,
        friend_id,
        "friend",
        transcript,
        msg_type="call_summary",
        is_read=True,
    )
    return {"message": _message_public(msg)}


@router.post("/chat/{friend_id}/image")
async def chat_image(
    request: Request,
    friend_id: str,
    image: UploadFile = File(...),
    text: str = Form(""),
) -> dict:
    """用户发图片 → 多模态 LLM 结合图片回复（语音条）。"""
    services = _services(request)
    friend = await _thread(services.db.get_friend, friend_id)
    if friend is None:
        raise HTTPException(status_code=404, detail="好友不存在")
    data = await image.read()
    try:
        result = await services.chat.handle_image(
            friend_id,
            data,
            image.filename or "image.jpg",
            image.content_type or "",
            text,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {
        "user_message": _message_public(result["user_message"]),
        "friend_messages": [_message_public(m) for m in result["friend_messages"]],
        "suggestion_pending": result.get("suggestion_pending", False),
        "closeness_level": result.get("closeness_level", 1),
        "total_turns": result.get("total_turns", 0),
        "memory_updated": result.get("memory_updated", False),
        "reply_language": result.get("reply_language", "en"),
        "stt": result.get("stt", {"backend": "image", "latency_ms": 0.0}),
    }


@router.post("/chat/{friend_id}/voice")
async def chat_voice(
    request: Request,
    friend_id: str,
    audio: UploadFile = File(...),
    duration_ms: int = Form(0),
) -> dict:
    services = _services(request)
    data = await audio.read()
    try:
        result = await services.chat.handle_voice(
            friend_id, data, audio.filename or "voice.webm", duration_ms
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="好友不存在") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _turn_public(result)


@router.post("/chat/{friend_id}/text")
async def chat_text(request: Request, friend_id: str, body: TextTurnBody) -> dict:
    services = _services(request)
    try:
        result = await services.chat.handle_text(friend_id, body.text, body.context)
    except KeyError:
        raise HTTPException(status_code=404, detail="好友不存在") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _turn_public(result)


def _turn_public(result: dict) -> dict:
    data = dict(result)
    data["user_message"] = _message_public(result.get("user_message"))
    data["friend_messages"] = [_message_public(m) for m in result.get("friend_messages") or []]
    return data


@router.post("/chat/{friend_id}/idle")
async def chat_idle(request: Request, friend_id: str) -> dict:
    services = _services(request)
    services.chat.trigger_memory(friend_id)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 翻译（聊天内对照）
# --------------------------------------------------------------------------- #
async def _translate_one(
    services: Any,
    text: str,
    *,
    target: str | None,
    friend: dict | None,
) -> dict:
    resolved = translate_mod.resolve_target(text, friend=friend, target=target)
    context = ""
    if friend:
        context = (
            f"{friend.get('name', 'friend')} from {friend.get('city', '')} "
            f"({friend.get('nationality', '')}), chatting casually"
        )
    return await translate_mod.translate_text(
        services.engines.llm, text, target=resolved, context=context
    )


@router.post("/translate")
async def translate_message(request: Request, body: TranslateIn) -> dict:
    """翻译一条消息；target 省略时自动判断方向（外语→中文 / 中文→好友语言）。"""
    services = _services(request)
    friend = None
    if body.friend_id:
        friend = await _thread(services.db.get_friend, body.friend_id)
    return await _translate_one(
        services, body.text, target=body.target, friend=friend
    )


@router.post("/translate/batch")
async def translate_batch(request: Request, body: TranslateBatchIn) -> dict:
    """批量翻译（聊天记录对照）；target 省略时逐条自动判断方向。"""
    services = _services(request)
    friend = None
    if body.friend_id:
        friend = await _thread(services.db.get_friend, body.friend_id)
    items = list(
        await asyncio.gather(
            *(
                _translate_one(services, text, target=body.target, friend=friend)
                for text in body.texts
            )
        )
    )
    first_error = next((item for item in items if not item.get("ok")), None)
    return {
        "ok": bool(items) and all(item.get("ok") for item in items),
        "target": translate_mod.normalize_language(body.target) or None,
        "items": items,
        "message": (first_error or {}).get("message", ""),
    }


# --------------------------------------------------------------------------- #
# 朋友圈
# --------------------------------------------------------------------------- #
def _friend_mini(friend: dict | None) -> dict | None:
    if not friend:
        return None
    return {
        "id": friend.get("id"),
        "name": friend.get("name"),
        "avatar_seed": friend.get("avatar_seed"),
        "city": friend.get("city"),
        "nationality": friend.get("nationality"),
        "timezone": friend.get("timezone"),
        "voice_id": friend.get("voice_id"),
    }


async def _comment_public(services: Any, comment: dict) -> dict:
    friend = await _thread(services.db.get_friend, comment.get("friend_id"))
    data = dict(comment)
    data["friend"] = _friend_mini(friend)
    return data


async def _moment_public(services: Any, moment: dict) -> dict:
    friend = await _thread(services.db.get_friend, moment.get("friend_id"))
    data = dict(moment)
    data.pop("_kind", None)
    data["author_type"] = "friend"
    data["author_name"] = friend.get("name") if friend else "好友"
    data["friend"] = _friend_mini(friend)
    data["comments"] = []
    return data


async def _user_moment_public(services: Any, moment: dict) -> dict:
    comments = await _thread(services.db.list_moment_comments, moment["id"])
    data = dict(moment)
    data.pop("_kind", None)
    data.update(
        {
            "author_type": "user",
            "author_name": "我",
            "friend_id": "",
            "friend": None,
            "local_time": "",
            "feed_date": _user_today(),
            "replied": False,
        }
    )
    image = data.get("image_path")
    data["image_url"] = f"/api/v2/media/{os.path.basename(image)}" if image else None
    data["comments"] = list(await asyncio.gather(*(_comment_public(services, c) for c in comments)))
    return data


async def _moments_payload(services: Any, rows: list[dict]) -> list[dict]:
    """rows 里每条带 ``_kind``（friend/user），分别序列化后合并。"""
    results = await asyncio.gather(
        *(
            _user_moment_public(services, row)
            if row.get("_kind") == "user"
            else _moment_public(services, row)
            for row in rows
        )
    )
    return list(results)


def _merged_moment_rows(friend_rows: list[dict], user_rows: list[dict], limit: int) -> list[dict]:
    rows: list[dict] = []
    for row in friend_rows:
        item = dict(row)
        item["_kind"] = "friend"
        rows.append(item)
    for row in user_rows:
        item = dict(row)
        item["_kind"] = "user"
        rows.append(item)
    rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return rows[: max(1, min(limit, 100))]


@router.get("/moments")
async def get_moments(request: Request, limit: int = 30) -> dict:
    services = _services(request)
    friend_rows, user_rows = await asyncio.gather(
        _thread(services.db.list_moments, limit),
        _thread(services.db.list_user_moments, limit),
    )
    rows = _merged_moment_rows(friend_rows, user_rows, limit)
    return {
        "moments": await _moments_payload(services, rows),
        "generated_at": rows[0]["created_at"] if rows else None,
    }


@router.post("/moments")
async def create_user_moment(request: Request, body: UserMomentIn) -> dict:
    """用户发布动态；AI 好友评论在后台生成（前端轮询 /moments 获取）。"""
    services = _services(request)
    content = " ".join(body.content.split()).strip()
    if not content:
        raise HTTPException(status_code=422, detail="动态内容不能为空")
    moment = await _thread(
        services.db.add_user_moment, content, (body.mood_tag or "").strip()
    )
    friends = [
        f
        for f in await _thread(services.db.list_friends, "active")
        if str(f.get("status") or "active") == "active"
    ]
    services.spawn_bg(
        user_moments_mod.generate_comments(
            services.db, services.engines.llm, moment, friends
        )
    )
    return {
        "moment": await _user_moment_public(services, moment),
        "comments_pending": True,
    }


@router.post("/moments/photo")
async def create_user_moment_photo(
    request: Request,
    content: str = Form(""),
    mood_tag: str = Form(""),
    image: UploadFile = File(...),
) -> dict:
    """带图片的用户动态（多模态）；AI 好友评论同样后台生成。"""
    services = _services(request)
    if not hasattr(services, "media") or services.media is None:
        raise HTTPException(status_code=503, detail="图片存储未初始化")
    data = await image.read()
    try:
        image_name = await _thread(
            services.media.save_image, data, image.content_type or "", image.filename or ""
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    text = " ".join((content or "").split()).strip() or "（分享了一张图片）"
    if len(text) > 500:
        raise HTTPException(status_code=422, detail="动态内容过长")
    moment = await _thread(
        services.db.add_user_moment, text, (mood_tag or "").strip(), image_name
    )
    friends = [
        f
        for f in await _thread(services.db.list_friends, "active")
        if str(f.get("status") or "active") == "active"
    ]
    services.spawn_bg(
        user_moments_mod.generate_comments(
            services.db, services.engines.llm, moment, friends
        )
    )
    return {
        "moment": await _user_moment_public(services, moment),
        "comments_pending": True,
    }


@router.post("/moments/generate")
async def generate_moments(request: Request) -> dict:
    services = _services(request)
    feed_date = _user_today()
    if await _thread(services.db.moments_exist_for_date, feed_date):
        rows = await _thread(services.db.list_moments, 30)
        return {"moments": await _moments_payload(services, rows), "generated": False}
    friends = [
        f
        for f in await _thread(services.db.list_friends, "active")
        if f.get("status") == "active"
    ][:5]
    try:
        await moments_mod.generate_daily(
            services.db, services.engines.llm, friends, feed_date=feed_date
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("朋友圈生成失败（返回已有）: %s: %s", type(exc).__name__, exc)
    rows = await _thread(services.db.list_moments, 30)
    return {"moments": await _moments_payload(services, rows), "generated": True}


@router.post("/moments/{moment_id}/comment")
async def comment_moment(request: Request, moment_id: str) -> dict:
    services = _services(request)
    moment = await _thread(services.db.get_moment, moment_id)
    if moment is None:
        raise HTTPException(status_code=404, detail="动态不存在")
    friend = await _thread(services.db.get_friend, moment["friend_id"])
    if friend is None:
        raise HTTPException(status_code=404, detail="好友不存在")

    context = (
        f'Moment by {friend.get("name")} ({friend.get("city")}): "{moment.get("content")}"'
    )
    content_type = (request.headers.get("content-type") or "").lower()
    result: dict
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("audio")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(status_code=422, detail="缺少 audio 文件")
        data = await upload.read()
        filename = getattr(upload, "filename", None) or "comment.webm"
        try:
            result = await services.chat.handle_voice(
                friend["id"], data, filename, 0, context=context
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    else:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        text = str((body or {}).get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=422, detail="缺少 text 或 audio")
        result = await services.chat.handle_text(friend["id"], text, context=context)

    await _thread(services.db.mark_moment_replied, moment_id)
    moment = await _thread(services.db.get_moment, moment_id) or moment
    return {
        "friend_id": friend["id"],
        "moment": await _moment_public(services, moment),
        "user_message": _message_public(result.get("user_message")),
        "reply": _message_public((result.get("friend_messages") or [None])[0]),
    }


# --------------------------------------------------------------------------- #
# 表达本
# --------------------------------------------------------------------------- #
def _expression_public(expr: dict) -> dict:
    return dict(expr)


@router.get("/expressions")
async def list_expressions(request: Request, status: str | None = None) -> dict:
    services = _services(request)
    rows = await _thread(services.db.list_expressions, status)
    return {"expressions": [_expression_public(e) for e in rows]}


@router.post("/expressions")
async def create_expression(request: Request, body: ExpressionBody) -> dict:
    services = _services(request)
    expr = await _thread(
        services.db.add_expression,
        body.original_text,
        body.better_expression,
        body.explanation,
        body.source_friend_id,
    )
    return {"expression": _expression_public(expr)}


@router.patch("/expressions/{expression_id}")
async def patch_expression(request: Request, expression_id: str, body: ExpressionPatch) -> dict:
    services = _services(request)
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status_code=422, detail="没有要更新的字段")
    try:
        expr = await _thread(services.db.update_expression, expression_id, **fields)
    except KeyError:
        raise HTTPException(status_code=404, detail="表达不存在") from None
    return {"expression": _expression_public(expr)}


@router.delete("/expressions/{expression_id}")
async def delete_expression(request: Request, expression_id: str) -> dict:
    services = _services(request)
    await _thread(services.db.delete_expression, expression_id)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 记忆 / 画像
# --------------------------------------------------------------------------- #
@router.get("/memories")
async def get_memories(request: Request) -> dict:
    services = _services(request)
    profile = await _thread(services.db.get_profile)
    friends = await _thread(services.db.list_friends)
    friend_memories = [
        {
            "id": f["id"],
            "name": f["name"],
            "city": f.get("city"),
            "nationality": f.get("nationality"),
            "closeness_level": f.get("closeness_level", 1),
            "total_turns": f.get("total_turns", 0),
            "shared_episodes": f.get("shared_episodes") or [],
            "open_loops": f.get("open_loops") or [],
        }
        for f in friends
        if f.get("status") == "active"
    ]
    return {
        "profile": profile,
        "fact_sheet": profile.get("fact_sheet") or {},
        "friends": friend_memories,
    }


@router.put("/memories")
async def update_memories(request: Request, body: MemoryUpdate) -> dict:
    services = _services(request)
    fields = body.model_dump(exclude_none=True)
    if fields:
        profile = await _thread(services.db.update_profile, **fields)
    else:
        profile = await _thread(services.db.get_profile)
    return {"profile": profile, "fact_sheet": profile.get("fact_sheet") or {}}


@router.put("/friends/{friend_id}/memory")
async def update_friend_memory(
    request: Request, friend_id: str, body: FriendMemoryBody
) -> dict:
    services = _services(request)
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status_code=422, detail="没有要更新的字段")
    try:
        friend = await _thread(services.db.update_friend, friend_id, **fields)
    except KeyError:
        raise HTTPException(status_code=404, detail="好友不存在") from None
    return {"friend": await _friend_public(services, friend), "memory": await _friend_memory(friend)}


# --------------------------------------------------------------------------- #
# 音频 / 声线
# --------------------------------------------------------------------------- #
@router.get("/audio/{filename}")
async def get_audio(request: Request, filename: str) -> FileResponse:
    services = _services(request)
    try:
        path = services.audio.path_for(filename)
    except ValueError:
        raise HTTPException(status_code=404, detail="音频不存在") from None
    if not os.path.isfile(path):
        log.warning("音频文件不存在: %s", filename)
        raise HTTPException(status_code=404, detail="音频不存在")
    # 显式声明标准 MIME：Safari 对 audio/x-wav 兼容性不佳，统一用 audio/wav
    ext = os.path.splitext(path)[1].lower()
    media_type = {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
        ".ogg": "audio/ogg",
        ".opus": "audio/opus",
    }.get(ext) or (mimetypes.guess_type(path)[0] or "application/octet-stream")
    return FileResponse(path, media_type=media_type)


@router.get("/tts/sample")
async def tts_sample(request: Request, text: str, voice: str = "en-GB-RyanNeural") -> FileResponse:
    services = _services(request)
    text = (text or "").strip()[:300]
    if not text:
        raise HTTPException(status_code=422, detail="text 不能为空")
    try:
        path, _ = await services.voice.synth_to_wav(
            text, voice, out_dir=services.audio.audio_dir
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"合成失败: {exc}") from exc
    return FileResponse(path, media_type="audio/wav", filename=os.path.basename(path))


@router.get("/archetypes")
async def list_archetypes() -> dict:
    archetypes = load_archetypes()
    countries = sorted({a["locale_info"]["country"] for a in archetypes})
    languages = sorted({a["locale_info"]["native_lang"] for a in archetypes})
    return {
        "archetypes": archetypes,
        "countries": countries,
        "languages": languages,
    }


@router.get("/health")
async def v2_health(request: Request) -> dict:
    services = _services(request)
    friends = await _thread(services.db.list_friends)
    return {
        "status": "ok",
        "version": V2_VERSION,
        "friends": len([f for f in friends if f.get("status") == "active"]),
        "pending_requests": len([f for f in friends if f.get("status") == "pending_request"]),
        "onboarding": services.onboarding_pending,
        "llm": services.engines.llm.health(),
        "stt": _stt_payload(services.engines),
    }


@router.get("/media/{filename}")
async def get_media(request: Request, filename: str) -> FileResponse:
    """用户上传图片的静态服务（显式 MIME，移动端 Safari 兼容）。"""
    services = _services(request)
    if not hasattr(services, "media") or services.media is None:
        raise HTTPException(status_code=404, detail="图片不存在")
    try:
        path = services.media.path_for(filename)
    except ValueError:
        raise HTTPException(status_code=404, detail="图片不存在") from None
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(
        path,
        media_type=services.media.mime_for(filename),
        headers={"Cache-Control": "public, max-age=86400"},
    )
