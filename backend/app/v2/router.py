"""SpeakEasy 2.0 REST 路由（``/api/v2``）。

消息条、好友、朋友圈、表达本、记忆、音频。所有数据库同步调用都走
``asyncio.to_thread``；LLM/TTS 失败由下层降级，接口尽量不 500。
"""

from __future__ import annotations

import random

import asyncio
import json
import mimetypes
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.llm import runtime_config as llm_runtime
from app.logging_utils import get_logger
from app.v2 import moments as moments_mod
from app.v2 import translate as translate_mod
from app.v2 import user_moments as user_moments_mod
from app.v2.timeutil import local_date_str, local_state
from app.v2.voices import load_archetypes
from app.v3.identity import LOCAL_USER_ID, current_user_id
from app.v3.security import bind_current_user
from app.voice.stt_config import (
    STT_PRESETS,
    STTRuntimeConfig,
    STTConfigStore,
    verify_config as verify_stt_config,
)

log = get_logger(__name__)

router = APIRouter(
    prefix="/api/v2",
    tags=["v2"],
    # V3 多用户：每个请求先绑定当前用户（不带身份头 = local，兼容老客户端）
    dependencies=[Depends(bind_current_user)],
)

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
    birthday: str | None = None


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


def _friendship_set_status(services: Any, friend_id: str, status: str) -> None:
    """V2 好友申请接受/拒绝时，同步 V3 的 ``friendships`` 状态（表可能不存在，忽略即可）。"""
    db = services.db
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with db._lock, db.conn:  # noqa: SLF001 - 与 DB 共用连接与锁
            db.conn.execute(
                "UPDATE friendships SET status=?, updated_at=? WHERE friend_id=? AND user_id=?",
                (status, now, friend_id, current_user_id()),
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("同步 friendship 状态失败: %s", exc)


def _friendship_drop(services: Any, friend_id: str) -> None:
    """删除好友时清掉对应的 V3 好友关系。"""
    db = services.db
    try:
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                "DELETE FROM friendships WHERE friend_id=? AND user_id=?",
                (friend_id, current_user_id()),
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("清理 friendship 失败: %s", exc)


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
    # 好友申请试听：直接用 TA 那条招呼语音的文件，前端点开即播，不用现场合成
    last_audio = (last or {}).get("audio_path")
    data["greeting"] = str((last or {}).get("transcript") or "")
    data["greeting_audio"] = (
        f"/api/v2/audio/{os.path.basename(str(last_audio))}" if last_audio else None
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


def _llm_store(engines: Any) -> Any:
    """取（并懒创建）运行时 LLM 配置 store。"""
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
    return store


def _llm_config_payload(engines: Any) -> dict:
    store = _llm_store(engines)
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


def _key_edit_guard() -> None:
    """客户端默认**不允许**改模型/密钥：配置统一在管理端（本机 /admin）。

    需要临时放开（开发调试）时设 ``ALLOW_KEY_EDIT=1``。
    """
    import os as _os

    allow = _os.getenv("ALLOW_KEY_EDIT", "0").strip().lower()
    if allow not in ("1", "true", "yes", "on"):
        raise HTTPException(
            status_code=403,
            detail="配置已移至管理端：请在本机打开 http://127.0.0.1:8000/admin 的「模型与密钥」修改",
        )


@router.post("/settings/llm")
async def save_llm_settings(request: Request, body: LLMConfigIn) -> dict:
    _key_edit_guard()
    """保存对话模型配置：写文件 → 应用 → 重建 LLM 客户端（立即生效）。"""
    engines = _engines_obj(request)
    store = _llm_store(engines)
    changes = body.model_dump(exclude_none=True, exclude={"verify"})
    if not changes:
        raise HTTPException(status_code=422, detail="没有要更新的字段")
    try:
        cfg = await asyncio.to_thread(store.save, **changes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # 应用新配置并重建客户端，下一条消息就生效
    from app.llm.client import LLMClient  # noqa: PLC0415

    llm_runtime.apply_to_settings(engines.settings.llm, cfg)
    engines.llm = LLMClient(engines.settings.llm)
    suggestions = getattr(engines, "suggestions", None)
    if suggestions is not None and hasattr(suggestions, "llm"):
        suggestions.llm = engines.llm

    verified: dict[str, Any] | None = None
    if body.verify:
        try:
            verified = await asyncio.to_thread(engines.llm.health)
        except Exception as exc:  # noqa: BLE001
            verified = {"configured": False, "error": f"{type(exc).__name__}: {exc}"}

    return {
        "ok": True,
        "config": cfg.public(),
        "health": _llm_config_payload(engines)["health"],
        "verified": verified,
    }


@router.post("/settings/stt")
async def save_stt_settings(request: Request, body: SttConfigIn) -> dict:
    _key_edit_guard()
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
    _key_edit_guard()
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


def _archetype_rows(services: Any) -> list[dict[str, Any]]:
    sp = getattr(services, "spawner", None)
    rows = getattr(sp, "archetypes", None) or []
    return [r for r in rows if isinstance(r, dict)]


def _pick_onboarding_picks(services: Any) -> tuple[dict[str, Any], ...]:
    """随机挑两个语言不同的国家；优先照顾自画像里的 target_languages。"""
    rows = _archetype_rows(services)
    lang_countries: dict[str, list[str]] = {}
    for r in rows:
        li = r.get("locale_info") or {}
        lang = str(li.get("native_lang") or "").strip()
        country = str(li.get("country") or "").strip()
        if lang and country:
            lang_countries.setdefault(lang, [])
            if country not in lang_countries[lang]:
                lang_countries[lang].append(country)

    if not lang_countries:
        # 兜底：历史行为
        return ({"nationality": "United Kingdom"}, {"nationality": "Italy"})

    targets: list[str] = []
    try:
        profile = services.db.get_profile() or {}
        for code in profile.get("target_languages") or []:
            code = str(code or "").strip()
            if code in lang_countries and code not in targets:
                targets.append(code)
    except Exception:  # noqa: BLE001 - 读画像失败不该挡住 onboarding
        targets = []

    pool = targets if len(targets) >= 1 else list(lang_countries.keys())
    picked: list[str] = []
    langs = random.sample(pool, k=min(2, len(pool)))
    if len(langs) < 2:
        # 目标语言只有一种 → 补一个别的语言，保持反差
        rest = [c for c in lang_countries if c not in langs]
        if rest:
            langs.append(random.choice(rest))
    for lang in langs:
        picked.append({"nationality": random.choice(lang_countries[lang])})

    random.shuffle(picked)
    return tuple(picked[:2])


async def _run_onboarding(services: Any) -> None:
    """好友为空时：按画像目标语言，主动来 1~3 个人加你（好友申请形式）。

    语言只用真实声线能说的（Fish ∩ 原型库），保证第一条招呼语音能正常生成。
    """
    try:
        supported = await _discover_languages(services)
        profile: dict = {}
        try:
            profile = services.db.get_profile() or {}
        except Exception:  # noqa: BLE001 - 读画像失败就随机
            profile = {}
        targets = [
            str(x).strip()
            for x in (profile.get("target_languages") or [])
            if str(x or "").strip()
        ]
        langs = [lang for lang in targets if not supported or lang in supported]
        if not langs:
            langs = list(supported) or ["en"]

        rows = load_archetypes()
        countries_by_lang: dict[str, list[str]] = {}
        for a in rows:
            li = a.get("locale_info") or {}
            lang = str(li.get("native_lang") or "").strip()
            country = str(li.get("country") or "").strip()
            if lang and country:
                countries_by_lang.setdefault(lang, []).append(country)

        count = random.randint(1, 3)
        picks = random.sample(langs, k=min(count, len(langs)))
        while len(picks) < count:
            picks.append(random.choice(langs))

        for lang in picks:
            countries = countries_by_lang.get(lang)
            friend = await services.spawner.spawn(
                nationality=random.choice(countries) if countries else None,
                language=lang,
                status="pending_request",
                strict_filters=False,
            )
            message = await _send_icebreaker(services, friend)
            try:
                await _thread(
                    services.db.update_friend,
                    friend["id"],
                    request_message=str(message.get("transcript") or "")[:160],
                )
            except Exception:  # noqa: BLE001
                pass
            log.info(
                "onboarding: 好友申请 %s (%s, %s)",
                friend.get("name"),
                friend.get("city"),
                lang,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("onboarding 失败: %s: %s", type(exc).__name__, exc)
    finally:
        services.onboarding_task = None


def _ensure_onboarding(services: Any, all_friends: list[dict]) -> bool:
    """空好友时自动来 1~3 个好友申请。

    V3 起：**带设备身份的新用户不再自动捏人**——他们直接进入 Persona 信息流，
    通过浏览内容自己加好友（避免为每个新用户预生成一批完整 AI 好友）。
    本地 `local` 用户保留原有引导行为。
    """
    if current_user_id() != LOCAL_USER_ID:
        return False
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


def _weighted_target_language(services: Any) -> str | None:
    """按自画像目标语言决定偶遇语种：70% 从目标语言里挑，30% 完全随机（惊喜）。"""
    try:
        profile = services.db.get_profile() or {}
        targets = [
            str(x).strip()
            for x in (profile.get("target_languages") or [])
            if str(x or "").strip()
        ]
    except Exception:  # noqa: BLE001 - 读画像失败就完全随机
        targets = []
    if not targets:
        return None
    if random.random() < TARGET_LANG_SKIP_RATE:
        return None
    return random.choice(targets)


#: 偶遇时「不走目标语言」的概率（保留惊喜感）
TARGET_LANG_SKIP_RATE = 0.3


@router.post("/friends/discover")
async def discover_friend(request: Request, body: DiscoverBody | None = None) -> dict:
    services = _services(request)
    body = body or DiscoverBody()
    has_filters = bool(
        (body.nationality or "").strip() or (body.language or "").strip() or (body.gender or "").strip()
    )

    # 用户没显式筛语种 → 用自画像的目标语言做「为主 + 偶发其他」。
    # 注意仍保持 strict_filters=False，抽不到时 spawner 会放宽而不是报 422。
    supported_langs = await _discover_languages(services)
    pick_language = body.language
    if body.language and supported_langs and body.language not in supported_langs:
        raise HTTPException(
            status_code=422,
            detail="这个语言暂时没有可用的真人声线，换一个试试",
        )
    if not has_filters:
        pick_language = _weighted_target_language(services)
        # 画像里的目标语言如果没声线，就退回有声线的语言
        if pick_language and supported_langs and pick_language not in supported_langs:
            pick_language = random.choice(supported_langs)

    try:
        friend = await services.spawner.spawn(
            nationality=body.nationality,
            language=pick_language,
            gender=body.gender,
            status="pending_request",
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
    # 把招呼语写进 request_message：好友申请列表里也能看到/试听这句话
    try:
        await _thread(
            services.db.update_friend,
            friend["id"],
            request_message=str(message.get("transcript") or "")[:160],
        )
    except Exception:  # noqa: BLE001 - 写不进去不影响偶遇
        pass
    friend = await _thread(services.db.get_friend, friend["id"]) or friend
    return {
        "friend": await _friend_public(services, friend),
        "message": _message_public(message),
    }


class VoiceChangeBody(BaseModel):
    archetype_id: str = Field(min_length=1, max_length=64)


@router.patch("/friends/{friend_id}/voice")
async def change_friend_voice(request: Request, friend_id: str, body: VoiceChangeBody) -> dict:
    """更换好友的口音（限定在它当前回复语言的声线里）。

    改的是 voice_id + rate/pitch —— 只改 voice_id 会让语速/音高沿用旧原型，听感不到位。
    """
    services = _services(request)
    friend = await _thread(services.db.get_friend, friend_id)
    if not friend:
        raise HTTPException(status_code=404, detail="好友不存在")

    lang = str(friend.get("reply_language") or "en")
    rows = load_archetypes()

    target = next(
        (a for a in rows if str(a.get("archetype_id")) == body.archetype_id),
        None,
    )
    if target is None:
        raise HTTPException(status_code=404, detail="没有这个声线原型")

    target_lang = str((target.get("locale_info") or {}).get("native_lang") or "")
    if target_lang != lang:
        raise HTTPException(
            status_code=422,
            detail=f"这个声线是 {target_lang} 的，和好友当前语言 {lang} 不一致",
        )

    prosody = target.get("prosody") or {}
    updated = await _thread(
        services.db.update_friend,
        friend_id,
        archetype_id=body.archetype_id,
        voice_id=str(target.get("voice_id") or ""),
        voice_rate=str(prosody.get("rate") or "+0%"),
        voice_pitch=str(prosody.get("pitch") or "+0Hz"),
    )
    return {
        "friend": await _friend_public(services, updated),
        "accent_label": target.get("accent_label") or "",
        "voice_id": updated.get("voice_id"),
    }


class LLMConfigIn(BaseModel):
    """保存对话模型配置；只传想改的字段。"""

    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    suggestion_model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    verify: bool = True


class TTSConfigBody(BaseModel):
    provider: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    reference_id: str | None = None


def _tts_store(request: Request) -> Any:
    store = getattr(request.app.state, "tts_config", None)
    if store is None:
        from app.config import get_settings  # noqa: PLC0415
        from app.voice.tts_config import TTSConfigStore  # noqa: PLC0415

        store = TTSConfigStore(str(get_settings().data_path))
        request.app.state.tts_config = store
    return store


@router.get("/settings/tts")
async def get_tts_settings(request: Request) -> dict:
    return {"tts": _tts_store(request).load().public()}


@router.post("/settings/tts")
async def set_tts_settings(request: Request, body: TTSConfigBody) -> dict:
    _key_edit_guard()
    store = _tts_store(request)
    patch: dict[str, Any] = {}
    if body.provider is not None:
        patch["provider"] = body.provider.strip()
    if body.api_key:
        patch["fish_api_key"] = body.api_key.strip()
    if body.base_url:
        patch["fish_base_url"] = body.base_url.strip()
    if body.model:
        patch["fish_model"] = body.model.strip()
    if body.reference_id is not None:
        patch["fish_reference_id"] = body.reference_id.strip()
    return {"ok": True, "tts": store.save(**patch).public()}


@router.post("/settings/tts/reset")
async def reset_tts_settings(request: Request) -> dict:
    _key_edit_guard()
    return {"ok": True, "tts": _tts_store(request).reset().public()}


@router.get("/settings/tts/voices")
async def list_tts_voices(
    request: Request,
    q: str = "",
    language: str = "",
    page: int = 1,
) -> dict:
    """Fish 平台音色（1000+）：支持关键字与语言过滤。

    Fish 的列表接口没有搜索参数，这里取一页（最多 100 条）后在服务端过滤，
    结果有 10 分钟缓存，切换语言/翻页不会反复打上游。
    """
    from app.voice import fish_tts  # noqa: PLC0415

    cfg = _tts_store(request).load()
    try:
        rows = await fish_tts.list_models(
            api_key=cfg.fish_api_key,
            base_url=cfg.fish_base_url,
            page_number=max(1, int(page)),
            page_size=100,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": str(exc), "body": getattr(exc, "body", ""), "voices": []}

    needle = (q or "").strip().lower()
    lang = (language or "").strip().lower()
    out = []
    for v in rows:
        if lang and lang not in [str(x).lower() for x in (v.get("languages") or [])]:
            continue
        if needle and needle not in (v.get("title") or "").lower() and needle not in (
            v.get("description") or ""
        ).lower():
            continue
        out.append(v)
    return {"ok": True, "total": rows[0].get("total") if rows else 0, "voices": out[:60]}


@router.post("/settings/tts/test")
async def test_tts_settings(request: Request) -> dict:
    """用当前配置合成一句。失败时把 Fish 的原始报错一并回传。"""
    import os  # noqa: PLC0415
    import uuid  # noqa: PLC0415

    from app.config import get_settings  # noqa: PLC0415
    from app.voice import fish_tts  # noqa: PLC0415

    cfg = _tts_store(request).load()
    if cfg.provider != "fish":
        return {"ok": True, "provider": "edge", "detail": "当前使用 edge，无需测试"}

    test_text = "[excited] Hey! This is your new voice - pretty different, right?"
    try:
        audio = await fish_tts.synthesize(
            test_text,
            api_key=cfg.fish_api_key,
            base_url=cfg.fish_base_url,
            model=cfg.fish_model,
            reference_id=_ref,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "provider": "fish",
            "detail": str(exc),
            "fish_status": getattr(exc, "status", 0),
            "fish_body": getattr(exc, "body", ""),
        }

    out_dir = os.path.join(str(get_settings().data_path), "audio")
    os.makedirs(out_dir, exist_ok=True)
    name = f"tts_test_{uuid.uuid4().hex[:12]}.wav"
    with open(os.path.join(out_dir, name), "wb") as f:
        f.write(audio)
    return {"ok": True, "provider": "fish", "audio_url": f"/api/v2/audio/{name}", "bytes": len(audio)}


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
    _friendship_set_status(services, friend_id, "accepted")
    existing = await _thread(services.db.recent_messages, friend_id, 1)
    if existing:
        # 偶遇时已经说过第一句了：接受时不再补一条，避免出现两条招呼
        return {
            "friend": await _friend_public(services, friend),
            "message": _message_public(existing[-1]),
        }
    message = await _send_icebreaker(services, friend)
    return {"friend": await _friend_public(services, friend), "message": _message_public(message)}


@router.post("/friends/{friend_id}/decline")
async def decline_friend(request: Request, friend_id: str) -> dict:
    services = _services(request)
    friend = await _thread(services.db.get_friend, friend_id)
    if friend is None:
        raise HTTPException(status_code=404, detail="好友不存在")
    _friendship_set_status(services, friend_id, "declined")
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
    _friendship_drop(services, friend_id)
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
    friend = await _thread(services.db.get_friend, friend_id)
    if friend is None:
        raise HTTPException(status_code=404, detail="好友不存在")
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
                [{"role": "user", "content": "Hi"}], max_tokens=64, temperature=0.0
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



class CallTurnTextBody(BaseModel):
    """流式 STT 已拿到文本时，直接请求通话回复。"""

    text: str = ""
    duration_ms: int = 0


@router.websocket("/stt/stream")
async def stt_stream(websocket: WebSocket, language: str = "en") -> None:
    """手机 PCM 分片 → Deepgram 流式识别 → partial/final 文本。

    客户端协议：
    - 二进制帧：PCM16LE / 16kHz / 单声道
    - 文本帧：{"type":"stop"} 表示本句说完，服务端 Finalize 并回 {"type":"done","text":...}
    """
    from app.voice.deepgram_stream import (
        DeepgramStreamer,
        StreamUnavailable,
        deepgram_key,
        language_code,
    )

    await websocket.accept()
    key = deepgram_key()
    if not key:
        await websocket.send_json({"type": "error", "message": "stream-stt-not-configured"})
        await websocket.close()
        return
    streamer = DeepgramStreamer(key, language_code(language))
    try:
        await streamer.connect()
    except StreamUnavailable as exc:
        await websocket.send_json({"type": "error", "message": f"connect:{exc}"})
        await websocket.close()
        return

    final_evt = asyncio.Event()

    async def pump() -> None:
        try:
            async for ev in streamer.events():
                try:
                    await websocket.send_json(ev)
                except Exception:  # noqa: BLE001
                    break
                if ev.get("type") == "final":
                    final_evt.set()
        except Exception:  # noqa: BLE001 - 上游断开
            pass
        finally:
            final_evt.set()

    task = asyncio.create_task(pump())
    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            chunk = msg.get("bytes")
            if chunk:
                await streamer.send(chunk)
                continue
            raw = msg.get("text")
            if not raw:
                continue
            try:
                cmd = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            if cmd.get("type") == "stop":
                final_evt.clear()
                await streamer.finalize()
                try:
                    await asyncio.wait_for(final_evt.wait(), timeout=1.2)
                except asyncio.TimeoutError:
                    pass
                try:
                    await websocket.send_json({"type": "done", "text": streamer.final_text})
                except Exception:  # noqa: BLE001
                    pass
                break
            if cmd.get("type") == "close":
                break
    finally:
        task.cancel()
        await streamer.close()
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


@router.get("/stt/stream/health")
async def stt_stream_health() -> dict:
    from app.voice.deepgram_stream import deepgram_status

    return {"available": deepgram_status()["available"]}


class DeepgramKeyBody(BaseModel):
    api_key: str = ""


@router.get("/settings/deepgram")
async def get_deepgram_settings() -> dict:
    from app.voice.deepgram_stream import deepgram_status

    return {"deepgram": deepgram_status()}


@router.post("/settings/deepgram")
async def save_deepgram_settings(body: DeepgramKeyBody) -> dict:
    """保存 Deepgram 流式识别 Key（App 设置页）——仍由服务器保管。"""
    _key_edit_guard()
    from app.voice.deepgram_stream import deepgram_status, save_runtime_key

    key = (body.api_key or "").strip()
    if len(key) < 16:
        raise HTTPException(status_code=422, detail="Key 格式不正确")
    if not save_runtime_key(key):
        raise HTTPException(status_code=500, detail="保存失败")
    return {"ok": True, "deepgram": deepgram_status()}


@router.post("/chat/{friend_id}/call/turn-text")
async def call_turn_text(
    request: Request, friend_id: str, body: CallTurnTextBody
) -> StreamingResponse:
    """通话回合（文本路径）：流式 STT 已经给出文本，直接生成回复。"""
    services = _services(request)
    friend = await _thread(services.db.get_friend, friend_id)
    if friend is None:
        raise HTTPException(status_code=404, detail="好友不存在")

    async def gen():
        try:
            async for event in services.chat.handle_call_turn_stream(
                friend_id,
                b"",
                "stream.webm",
                int(body.duration_ms or 0),
                transcript=(body.text or "").strip(),
                stt_meta={"backend": "deepgram-stream", "latency_ms": 0.0},
            ):
                if event.get("type") == "done":
                    event["user_message"] = _message_public(event.get("user_message"))
                    event["friend_message"] = _message_public(event.get("friend_message"))
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("通话文本回合失败: %s", exc)
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


@router.delete("/moments/{moment_id}")
async def delete_user_moment(request: Request, moment_id: str) -> dict:
    """只能删自己发的动态；好友发的动态不允许删。"""
    services = _services(request)
    moment = await _thread(services.db.get_user_moment, moment_id)
    if moment is None:
        raise HTTPException(status_code=404, detail="只能删除自己发的动态")
    ok = await _thread(services.db.delete_user_moment, moment_id)
    if not ok:
        raise HTTPException(status_code=404, detail="动态不存在")
    return {"deleted": moment_id}


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
async def tts_sample(
    request: Request,
    text: str,
    voice: str = "en-GB-RyanNeural",
    friend_id: str | None = None,
) -> FileResponse:
    """试听：**有 friend_id 时必须用该角色的固定音色**（与聊天/朗读/通话一致）。

    ``voice`` 参数只用于没有角色上下文的老调用；AI 角色不再接受外部指定声线，
    否则「换口音」这类入口会让同一个角色出现多个声音。
    """
    services = _services(request)
    text = (text or "").strip()[:300]
    if not text:
        raise HTTPException(status_code=422, detail="text 不能为空")

    from app.voice import voice_registry  # noqa: PLC0415

    binding = None
    if friend_id:
        try:
            friend = await asyncio.to_thread(services.db.get_friend, friend_id)
            if friend is not None:
                binding = await asyncio.to_thread(
                    voice_registry.binding_for_friend, services.db, friend
                )
        except Exception as exc:  # noqa: BLE001
            log.debug("tts/sample 解析角色音色失败: %s", exc)

    result = await voice_registry.synthesize(
        {"id": (binding.persona_id if binding else ""), "voice_id": voice},
        text,
        binding=binding,
        edge_engine=services.voice,
        audio_format="mp3",
        out_dir=services.audio.audio_dir,
    )
    if result is None or not result.data:
        raise HTTPException(status_code=502, detail="合成失败：Fish 与 Edge 均不可用")
    fname = f"sample_{uuid.uuid4().hex[:10]}.{result.ext}"
    fpath = os.path.join(services.audio.audio_dir, fname)
    with open(fpath, "wb") as fh:  # noqa: PTH123
        fh.write(result.data)
    return FileResponse(
        fpath,
        media_type="audio/mpeg" if result.ext == "mp3" else "audio/wav",
        filename=fname,
        headers={
            "Cache-Control": "no-store",
            "X-Voice": result.token,
            "X-Voice-Provider": result.provider,
        },
    )


async def _fish_languages(services: Any) -> set[str]:
    """Fish 账号里实际可用的音色语言（读不到就返回空集 = 不限制）。

    Fish 不可达时**立刻返回空集**：这个函数在「偶遇」链路上，每次等 30 秒超时
    会让用户点一下就转圈半分钟。
    """
    try:
        from app.config import get_settings as _gs  # noqa: PLC0415
        from app.voice import fish_tts as _ft  # noqa: PLC0415
        from app.voice import voice_registry as _vr  # noqa: PLC0415
        from app.voice.tts_config import TTSConfigStore as _TCS  # noqa: PLC0415

        cfg = _TCS(str(_gs().data_path)).load()
        if cfg.provider != "fish" or not cfg.fish_api_key.strip():
            return set()
        if not _vr.fish_usable(cfg):
            return set()  # 熔断窗口内：不再尝试联网
        try:
            rows = await _ft.list_models(
                api_key=cfg.fish_api_key,
                base_url=cfg.fish_base_url,
                page_number=1,
                page_size=100,
                timeout=5.0,
            )
        except Exception as exc:  # noqa: BLE001
            _vr.mark_fish_failure(exc)
            raise
        langs: set[str] = set()
        for r in rows:
            for lang in r.get("languages") or []:
                code = str(lang).strip().lower()
                if code:
                    langs.add(code)
        return langs
    except Exception as exc:  # noqa: BLE001 - 拿不到就不限制
        log.warning("读取 Fish 支持语言失败: %s", exc)
        return set()


def _archetype_languages() -> list[str]:
    """原型库里的语言（中文是母语，不作为练习目标）。"""
    langs = sorted({str(a.get("locale_info", {}).get("native_lang") or "") for a in load_archetypes()})
    return [lang for lang in langs if lang and lang != "zh"]


async def _discover_languages(services: Any) -> list[str]:
    """能被真实声线说出来的语言：原型库 ∩ Fish 音色语言。"""
    arch = _archetype_languages()
    fish = await _fish_languages(services)
    if not fish:
        return arch
    supported = [lang for lang in arch if lang in fish]
    return supported or arch


@router.get("/discover/languages")
async def discover_languages(request: Request) -> dict:
    """偶遇可选语言：只列真实声线能说的。"""
    services = _services(request)
    return {"languages": await _discover_languages(services)}


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
