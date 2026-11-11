"""管理 API · 角色工坊与音色池（``/api/admin/personas/*``、``/api/admin/voice-pool/*``、
``/api/admin/preview-personas``）。

从 ``app/admin/router.py`` 原样迁移，**路径与返回结构完全不变**：

角色工坊
    ``GET  /personas``、``GET /personas/users``、``GET /personas/rebuild-preview``、
    ``POST /personas/rematch-media``、``POST /personas/rebalance-genders``、
    ``POST /personas/redistribute-voices``、``POST /personas/regenerate-posts``、
    ``POST /personas/init-life``、``POST /personas/rebuild``、``POST /personas``、
    ``GET/PUT/DELETE /personas/{persona_id}``、``POST /personas/{persona_id}/reroll``

音色池
    ``GET  /voice-pool``、``GET /voice-pool/models``、``POST /voice-pool/validate``、
    ``POST /voice-pool/add``、``POST /voice-pool/remove``、``POST /voice-pool/resolve``、
    ``POST /voice-pool/preview``（返回 mp3 字节）

预览
    ``GET /preview-personas``（试捏人物，不写库、不调模型）

注意：``voice_pool`` / ``daily_world`` 保持**函数内延迟导入**，与迁移前一致——
这些模块会拉起音频与素材依赖，管理台其它分区不该为此付费。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request, Response

from app.admin.api import deps
from app.logging_utils import get_logger

log = get_logger(__name__)

#: 不声明 tags：父路由已带 ``tags=["admin"]``，重复声明会让签名里出现两个 "admin"。
router = APIRouter()

#: 数据库未就绪时的统一降级响应（沿用旧管理端的 200 + ok=False 约定）。
_DB_NOT_READY: dict[str, Any] = {"ok": False, "issues": ["数据库未就绪"]}

# --------------------------------------------------------------------------- #
# 角色工坊：全量重捏 / 单角色属性 / 新建测试角色
# --------------------------------------------------------------------------- #
@router.get("/preview-personas")
async def preview_personas(
    request: Request, count: int = 3, language: str = "en"
) -> dict:
    """按当前素材池试捏人物（不写库、不调模型），用于预览管理台改动的效果。"""
    try:
        from app.v3 import daily_world  # noqa: PLC0415

        return daily_world.preview_personas(count=count, language=language)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}", "personas": []}

@router.get("/personas")
async def admin_list_personas(request: Request, query: str = "", limit: int = 300) -> dict:
    persona_studio, db, _service, _store = deps.studio(request)
    if db is None:
        return _DB_NOT_READY
    return {
        "ok": True,
        "personas": persona_studio.list_personas(db, query=query, limit=limit),
        "counts": persona_studio.rebuild_preview(db)["counts"],
        "users": persona_studio.list_users(db, limit=100),
    }

@router.get("/personas/users")
async def admin_persona_users(request: Request) -> dict:
    persona_studio, db, _service, _store = deps.studio(request)
    return {"ok": True, "users": persona_studio.list_users(db, limit=200) if db else []}

@router.get("/personas/rebuild-preview")
async def admin_persona_rebuild_preview(request: Request) -> dict:
    persona_studio, db, _service, _store = deps.studio(request)
    return {"ok": True, **persona_studio.rebuild_preview(db)}

@router.post("/personas/rematch-media")
async def admin_rematch_media(request: Request, body: dict = Body(...)) -> dict:
    """修正存量图文帖：用 LLM 按图片内容重写文案（图片不变，文字与图相关）。"""
    persona_studio, db, _service, _store = deps.studio(request)
    if db is None:
        return _DB_NOT_READY
    engines = getattr(request.app.state, "engines", None)
    llm = getattr(engines, "llm", None) if engines is not None else None
    return await persona_studio.rematch_media(db, llm, limit=int(body.get("limit") or 20))

@router.post("/personas/rebalance-genders")
async def admin_rebalance_genders(request: Request, body: dict = Body(...)) -> dict:
    """把现有 AI 角色性别调整到大致 1:1（保留角色 ID/好友/聊天，只换名字/性别/头像/声音）。"""
    persona_studio, db, service, _store = deps.studio(request)
    if db is None:
        return _DB_NOT_READY
    return persona_studio.rebalance_genders(
        db, service, female_ratio=float(body.get("female_ratio") or 0.5)
    )

@router.post("/personas/redistribute-voices")
async def admin_redistribute_voices(request: Request) -> dict:
    """把素材池音色均匀重新分配给全部角色。"""
    persona_studio, db, service, _store = deps.studio(request)
    if db is None:
        return _DB_NOT_READY
    return persona_studio.redistribute_voices(db, service)

@router.post("/personas/regenerate-posts")
async def admin_regenerate_posts(request: Request, body: dict = Body(...)) -> dict:
    """重建全部角色的动态（每人 N 条、错峰、按语言素材库随机）。"""
    persona_studio, db, service, _store = deps.studio(request)
    if db is None:
        return _DB_NOT_READY
    return persona_studio.regenerate_posts(
        db, service, per_persona=int(body.get("per_persona") or 5)
    )

@router.post("/personas/init-life")
async def admin_init_life_layer(request: Request) -> dict:
    """补齐作息表 / Speech DNA / 示例对话 / 人物关系（0 次 LLM）。"""
    persona_studio, db, service, _store = deps.studio(request)
    if db is None:
        return _DB_NOT_READY
    result = await persona_studio.init_life_layer(db)
    persona_studio.invalidate(service)
    return {"ok": True, **result}

@router.post("/personas/rebuild")
async def admin_persona_rebuild(request: Request, body: dict = Body(...)) -> dict:
    persona_studio, db, service, store = deps.studio(request)
    if db is None:
        return _DB_NOT_READY
    return await persona_studio.rebuild_all(
        db,
        service,
        mode=str(body.get("mode") or "reroll"),
        count=int(body.get("count") or 0),
        auto_friend_users=bool(body.get("auto_friend_users")),
        confirm=str(body.get("confirm") or ""),
        conversation_store=store,
    )

@router.post("/personas")
async def admin_create_persona(request: Request, body: dict = Body(...)) -> dict:
    persona_studio, db, service, _store = deps.studio(request)
    if db is None:
        return _DB_NOT_READY
    payload = {k: v for k, v in body.items() if k not in ("bind_user_id", "posts")}
    posts = body.get("posts") or []
    if isinstance(posts, str):
        posts = [line for line in posts.splitlines() if line.strip()]
    return persona_studio.create_persona(
        db,
        service,
        payload,
        bind_user_id=str(body.get("bind_user_id") or ""),
        posts=[str(x) for x in posts][:5],
    )

@router.get("/personas/{persona_id}")
async def admin_get_persona(request: Request, persona_id: str) -> dict:
    persona_studio, db, _service, _store = deps.studio(request)
    detail = persona_studio.persona_detail(db, persona_id) if db else None
    if not detail:
        raise HTTPException(status_code=404, detail="角色不存在")
    return {"ok": True, "persona": detail}

@router.put("/personas/{persona_id}")
async def admin_update_persona(request: Request, persona_id: str, body: dict = Body(...)) -> dict:
    persona_studio, db, service, _store = deps.studio(request)
    result = persona_studio.update_persona(db, persona_id, body or {})
    if result.get("ok"):
        persona_studio.invalidate(service)
    return result

@router.post("/personas/{persona_id}/reroll")
async def admin_reroll_persona(request: Request, persona_id: str, body: dict = Body(...)) -> dict:
    persona_studio, db, service, _store = deps.studio(request)
    result = persona_studio.reroll_persona(
        db,
        persona_id,
        regen_voice=bool(body.get("regen_voice", True)),
        regen_avatar=bool(body.get("regen_avatar", True)),
    )
    if result.get("ok"):
        persona_studio.invalidate(service)
    return result

@router.delete("/personas/{persona_id}")
async def admin_delete_persona(request: Request, persona_id: str) -> dict:
    persona_studio, db, service, store = deps.studio(request)
    return persona_studio.delete_persona(
        db, service, persona_id, backup=True, conversation_store=store
    )

# --------------------------------------------------------------------------- #
# 音色池（Fish Audio）
# --------------------------------------------------------------------------- #
@router.get("/voice-pool")
async def get_voice_pool(request: Request) -> dict:
    """Fish 音色池：素材池 + 已分配统计 + Fish 可用状态。"""
    from app.admin import voice_pool  # noqa: PLC0415

    return voice_pool.status(deps.db(request))

@router.get("/voice-pool/models")
async def list_voice_pool_models(request: Request, refresh: bool = False) -> dict:
    """列出 Fish 账号下全部音色（分页拉取，最多 1000 条）。"""
    from app.admin import voice_pool  # noqa: PLC0415

    try:
        models = await voice_pool.list_all_models(refresh=refresh)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "issues": [f"{type(exc).__name__}: {exc}"], "models": []}
    return {"ok": True, "models": models, "count": len(models)}

@router.post("/voice-pool/validate")
async def validate_voice_pool(request: Request, body: dict = Body(...)) -> dict:
    """输入音色 id → 真实合成一句做检测；返回检测结果与试听缓存状态。"""
    from app.admin import voice_pool  # noqa: PLC0415

    reference_id = voice_pool.resolve_id(body.get("reference_id") or "")
    if not reference_id:
        return {"ok": False, "issues": ["音色 id（或素材池名称）不能为空"]}
    try:
        audio, media = await voice_pool.preview_audio(reference_id, refresh=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "issues": [f"{type(exc).__name__}: {exc}"]}
    db = deps.db(request)
    if db is not None:
        try:
            from app.voice import catalog  # noqa: PLC0415

            catalog.mark_verified(db, "fish", reference_id)
        except Exception:  # noqa: BLE001 - 验证记录失败不影响试听
            log.debug("voice-pool: mark_verified 失败 (%s)", reference_id, exc_info=True)
    return {
        "ok": True,
        "reference_id": reference_id,
        "bytes": len(audio),
        "media": media,
        "message": f"检测成功，已生成试听（{len(audio)} 字节）",
    }

@router.post("/voice-pool/add")
async def add_voice_pool(request: Request, body: dict = Body(...)) -> dict:
    """检测通过后加入素材池（捏人时优先使用）。"""
    from app.admin import voice_pool  # noqa: PLC0415

    reference_id = voice_pool.resolve_id(body.get("reference_id") or "")
    if not reference_id:
        return {"ok": False, "issues": ["音色 id（或素材池名称）不能为空"]}
    if not body.get("skip_validate"):
        try:
            await voice_pool.preview_audio(reference_id, refresh=True)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "issues": [f"检测失败，未加入：{type(exc).__name__}: {exc}"],
            }
        # 合成本身成功了 → 记为已验证，严格模式下重分配音色才认它
        db = deps.db(request)
        if db is not None:
            try:
                from app.voice import catalog  # noqa: PLC0415

                catalog.mark_verified(db, "fish", reference_id)
            except Exception:  # noqa: BLE001
                log.debug("voice-pool: mark_verified 失败 (%s)", reference_id, exc_info=True)
    return voice_pool.add_voice(
        reference_id,
        name=str(body.get("name") or ""),
        gender=str(body.get("gender") or ""),
        language=str(body.get("language") or ""),
        source=str(body.get("source") or "manual"),
    )

@router.post("/voice-pool/verify")
async def verify_voice_pool(request: Request) -> dict:
    """逐个确认池内音色可合成，并写入 ``voice_catalog``（严格模式下的可用集合）。

    已有试听缓存直接复用，不重复花 Fish 调用；对失败项只记录不删除。
    """
    from app.admin import voice_pool  # noqa: PLC0415
    from app.voice import catalog  # noqa: PLC0415

    db = deps.db(request)
    if db is None:
        return _DB_NOT_READY
    pool = voice_pool.load_pool()
    if not pool:
        return {"ok": False, "issues": ["音色池为空，先到「音色池」加音色"]}
    verified = 0
    failed: list[dict[str, str]] = []
    for item in pool:
        reference_id = str(item.get("reference_id") or "")
        if not reference_id:
            continue
        try:
            await voice_pool.preview_audio(reference_id)
        except Exception as exc:  # noqa: BLE001
            failed.append({"reference_id": reference_id, "error": f"{type(exc).__name__}: {exc}"[:200]})
            continue
        catalog.mark_verified(db, "fish", reference_id)
        verified += 1
    return {
        "ok": True,
        "total": len(pool),
        "verified": verified,
        "failed": failed,
    }

@router.post("/voice-pool/remove")
async def remove_voice_pool(request: Request, body: dict = Body(...)) -> dict:
    from app.admin import voice_pool  # noqa: PLC0415

    return voice_pool.remove_voice(str(body.get("reference_id") or ""))

@router.post("/voice-pool/resolve")
async def resolve_voice_pool(request: Request, body: dict = Body(...)) -> dict:
    """把「id 或名称」解析成素材池音色（给角色工坊输入框用）。"""
    from app.admin import voice_pool  # noqa: PLC0415

    query = str(body.get("q") or body.get("reference_id") or "").strip()
    hit = voice_pool.resolve(query)
    return {"ok": True, "match": hit, "resolved_id": (hit or {}).get("reference_id") or query}

@router.post("/voice-pool/preview")
async def preview_voice_pool(request: Request, body: dict = Body(...)) -> Response:
    """试听：返回 mp3 字节（管理页用 fetch+blob 播放，不把 token 放到 URL）。"""
    from app.admin import voice_pool  # noqa: PLC0415

    reference_id = voice_pool.resolve_id(body.get("reference_id") or "")
    if not reference_id:
        raise HTTPException(status_code=400, detail="音色 id（或素材池名称）不能为空")
    if body.get("refresh"):
        try:
            audio, media = await voice_pool.preview_audio(reference_id, refresh=True)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc
        return Response(content=audio, media_type=media)
    try:
        audio, media = await voice_pool.preview_audio(reference_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc
    return Response(content=audio, media_type=media)

__all__ = ["router"]
