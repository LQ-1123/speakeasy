"""SpeakEasy 2.0 REST 路由（``/api/v2``）。

消息条、好友、朋友圈、表达本、记忆、音频。所有数据库同步调用都走
``asyncio.to_thread``；LLM/TTS 失败由下层降级，接口尽量不 500。
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.llm import runtime_config as llm_runtime
from app.logging_utils import get_logger
from app.v2 import moments as moments_mod
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
    try:
        friend = await services.spawner.spawn(
            nationality=body.nationality,
            language=body.language,
            gender=body.gender,
            status="active",
        )
    except Exception as exc:  # noqa: BLE001
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
# 朋友圈
# --------------------------------------------------------------------------- #
async def _moment_public(services: Any, moment: dict) -> dict:
    friend = await _thread(services.db.get_friend, moment.get("friend_id"))
    data = dict(moment)
    data["friend"] = (
        {
            "id": friend.get("id"),
            "name": friend.get("name"),
            "avatar_seed": friend.get("avatar_seed"),
            "city": friend.get("city"),
            "nationality": friend.get("nationality"),
            "timezone": friend.get("timezone"),
            "voice_id": friend.get("voice_id"),
        }
        if friend
        else None
    )
    return data


async def _moments_payload(services: Any, rows: list[dict]) -> list[dict]:
    return list(await asyncio.gather(*(_moment_public(services, m) for m in rows)))


@router.get("/moments")
async def get_moments(request: Request, limit: int = 30) -> dict:
    services = _services(request)
    rows = await _thread(services.db.list_moments, limit)
    return {
        "moments": await _moments_payload(services, rows),
        "generated_at": rows[0]["created_at"] if rows else None,
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
        raise HTTPException(status_code=404, detail="音频不存在")
    media_type = mimetypes.guess_type(path)[0] or "audio/wav"
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
    return FileResponse(path, media_type="audio/wav")


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
