"""管理端（控制面）：词库管理，与客户端完全解耦。

三条原则
--------
1. **仅本机**：默认只允许回环地址（``127.0.0.1`` / ``::1``）访问；如需远程，
   设置 ``SE_ADMIN_TOKEN`` 并在请求头带 ``X-Admin-Token``。
2. **不碰角色**：这里只管理"捏人原料"（枚举词 / 场景 / 口头禅 / 素材池），
   不编辑具体某个人物——人物是成品，改了会漂移。
3. **改不坏**：保存前必须过引用完整性校验（枚举值要带数值映射、默认值必须存在…），
   写盘原子 + 保留上一版；校验不过直接 400，不会落盘。
"""

from __future__ import annotations

import os
import time
from typing import Any

from fastapi import (
    APIRouter,
    Body,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import HTMLResponse, RedirectResponse

from app.admin.page import render_admin_page
from app.logging_utils import get_logger
from app.admin import auth as admin_auth
from app.admin import media_admin
from app.admin import config as admin_config
from app.v3 import lexicon

log = get_logger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])
page_router = APIRouter(tags=["admin"])


def _admin_html(content: str) -> HTMLResponse:
    return HTMLResponse(
        content,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@page_router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request) -> HTMLResponse:
    """管理台页面：Cookie 登录态 / 旧 ?token= 链接 / 本机直通（未配口令时）。"""
    query_token = str(request.query_params.get("token", "") or "").strip()
    if admin_auth.authenticated(request) or admin_auth.token_ok(query_token):
        injected = query_token if admin_auth.token_ok(query_token) else ""
        return _admin_html(render_admin_page(admin_token=injected))
    if admin_auth.password_configured() or admin_auth.token_configured():
        next_url = str(request.query_params.get("next", "") or "/admin")
        return _admin_html(admin_auth.render_login_page(next_url=next_url))
    client_host = str(getattr(getattr(request, "client", None), "host", "") or "")
    if client_host in _LOOPBACK:
        return _admin_html(render_admin_page(admin_token=""))
    raise HTTPException(status_code=403, detail="管理端仅允许本机访问")


@page_router.post("/admin/login")
async def admin_login(request: Request):
    """密码登录：成功写 HttpOnly 会话 Cookie 并跳回管理台。"""
    form = await request.form()
    password = str(form.get("password") or "")
    next_url = str(form.get("next") or "/admin")
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/admin"
    ip = str(getattr(getattr(request, "client", None), "host", "") or "?")
    allowed, wait_s = admin_auth.check_rate(ip)
    if not allowed:
        return HTMLResponse(
            admin_auth.render_login_page(
                error=f"尝试次数过多，请 {wait_s} 秒后再试", next_url=next_url
            ),
            status_code=429,
        )
    if not admin_auth.password_configured():
        return HTMLResponse(
            admin_auth.render_login_page(
                error="服务端未设置 SE_ADMIN_PASSWORD；可继续用 ?token= 链接进入",
                next_url=next_url,
            ),
            status_code=400,
        )
    if not admin_auth.password_ok(password):
        admin_auth.record_fail(ip)
        return HTMLResponse(
            admin_auth.render_login_page(error="密码不正确", next_url=next_url),
            status_code=401,
        )
    admin_auth.reset_rate(ip)
    response = RedirectResponse(next_url or "/admin", status_code=303)
    response.set_cookie(
        admin_auth.COOKIE_NAME,
        admin_auth.make_session(),
        max_age=admin_auth.SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return response


@page_router.post("/admin/logout")
async def admin_logout():
    """清掉登录 Cookie（页面上的「退出登录」按钮用）。"""
    response = RedirectResponse("/admin", status_code=303)
    response.delete_cookie(admin_auth.COOKIE_NAME, path="/")
    return response


_LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


def _guard(request: Request) -> None:
    """管理接口守卫：Cookie 会话 / X-Admin-Token / 未配口令时的本机直通。"""
    if admin_auth.authenticated(request):
        return
    if not admin_auth.password_configured() and not admin_auth.token_configured():
        client_host = str(getattr(getattr(request, "client", None), "host", "") or "")
        if client_host in _LOOPBACK:
            return
        raise HTTPException(status_code=403, detail="管理端仅允许本机访问")
    raise HTTPException(status_code=403, detail="请先登录管理台")


def _summary() -> list[dict[str, Any]]:
    import os  # noqa: PLC0415

    items: list[dict[str, Any]] = []
    for name, title in lexicon.LEXICON_FILES.items():
        path = lexicon.path_for(name)
        exists = os.path.exists(path)
        issues = lexicon.validate(name, lexicon.load(name)) if exists else []
        items.append(
            {
                "name": name,
                "title": title,
                "exists": exists,
                "path": path,
                "bytes": os.path.getsize(path) if exists else 0,
                "mtime": os.path.getmtime(path) if exists else 0.0,
                "issues": issues,
                "ok": exists and not issues,
            }
        )
    return items


@router.get("/media/stats")
async def admin_media_stats(request: Request) -> dict:
    _guard(request)
    _persona_studio, db, _service, _store = _studio(request)
    if db is None:
        return {"ok": False, "issues": ["数据库未就绪"]}
    return media_admin.stats(db)


@router.get("/media/library")
async def admin_media_library(
    request: Request, offset: int = 0, limit: int = 60, only_unused: bool = False
) -> dict:
    """图库中所有已入库图片（分页，标注引用情况）。"""
    _guard(request)
    _persona_studio, db, _service, _store = _studio(request)
    if db is None:
        return {"ok": False, "issues": ["数据库未就绪"]}
    return media_admin.list_library(db, offset=offset, limit=limit, only_unused=only_unused)


@router.get("/media/search")
async def admin_media_search(
    request: Request, query: str = "", persona_id: str = "", limit: int = 8
) -> dict:
    _guard(request)
    _persona_studio, db, _service, _store = _studio(request)
    if db is None:
        return {"ok": False, "issues": ["数据库未就绪"]}
    return await media_admin.search(db, query, persona_id=persona_id, limit=limit)


@router.post("/media/fetch")
async def admin_media_fetch(request: Request, body: dict = Body(...)) -> dict:
    _guard(request)
    _persona_studio, db, _service, _store = _studio(request)
    if db is None:
        return {"ok": False, "issues": ["数据库未就绪"]}
    engines = getattr(request.app.state, "engines", None)
    llm = getattr(engines, "llm", None) if engines is not None else None
    return await media_admin.fetch(
        db,
        str(body.get("query") or ""),
        persona_id=str(body.get("persona_id") or ""),
        limit=int(body.get("limit") or 2),
        with_caption=bool(body.get("with_caption")),
        llm=llm,
        external_id=str(body.get("external_id") or ""),
    )


@router.post("/media/fill")
async def admin_media_fill(request: Request, body: dict = Body(...)) -> dict:
    _guard(request)
    _persona_studio, db, _service, _store = _studio(request)
    if db is None:
        return {"ok": False, "issues": ["数据库未就绪"]}
    engines = getattr(request.app.state, "engines", None)
    llm = getattr(engines, "llm", None) if engines is not None else None
    ids = body.get("persona_ids")
    return await media_admin.fill(
        db,
        llm,
        persona_ids=[str(x) for x in ids] if isinstance(ids, list) else None,
        per_persona=int(body.get("per_persona") or 1),
        with_caption=bool(body.get("with_caption", True)),
    )


@router.post("/media/prune")
async def admin_media_prune(request: Request, body: dict = Body(...)) -> dict:
    _guard(request)
    _persona_studio, db, _service, _store = _studio(request)
    if db is None:
        return {"ok": False, "issues": ["数据库未就绪"]}
    return media_admin.prune(
        db,
        ttl_days=int(body["ttl_days"]) if body.get("ttl_days") else None,
        max_bytes=int(body["max_bytes"]) if body.get("max_bytes") else None,
        max_files=int(body["max_files"]) if body.get("max_files") else None,
        dry_run=bool(body.get("dry_run")),
    )


@router.get("/status")
async def admin_status(request: Request) -> dict:
    _guard(request)
    return {
        "applied": lexicon.applied_status(),
        "lexicons": _summary(),
        "server_time": time.time(),
    }


@router.get("/lexicons")
async def list_lexicons(request: Request) -> dict:
    _guard(request)
    return {"items": _summary()}


@router.get("/lexicons/{name}")
async def get_lexicon(request: Request, name: str) -> dict:
    _guard(request)
    if name not in lexicon.LEXICON_FILES:
        raise HTTPException(status_code=404, detail=f"未知词库: {name}")
    data = lexicon.load(name, fresh=True)
    return {
        "name": name,
        "title": lexicon.LEXICON_FILES[name],
        "exists": data is not None,
        "data": data or {},
        "issues": lexicon.validate(name, data) if data is not None else ["词库文件不存在"],
        "path": lexicon.path_for(name),
    }


@router.put("/lexicons/{name}")
async def put_lexicon(
    request: Request, name: str, payload: dict = Body(...)
) -> dict:
    _guard(request)
    if name not in lexicon.LEXICON_FILES:
        raise HTTPException(status_code=404, detail=f"未知词库: {name}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="body 必须是 {'data': {...}}")
    try:
        result = lexicon.save(name, data)
    except lexicon.LexiconError as exc:
        return {
            "ok": False,
            "issues": exc.issues,
            "message": str(exc),
        }
    log.info("词库已保存: %s -> %s", name, result["path"])
    return {"ok": True, **result, "applied": lexicon.applied_status()}


@router.post("/lexicons/{name}/validate")
async def validate_lexicon(
    request: Request, name: str, payload: dict = Body(...)
) -> dict:
    _guard(request)
    if name not in lexicon.LEXICON_FILES:
        raise HTTPException(status_code=404, detail=f"未知词库: {name}")
    data = payload.get("data")
    issues = lexicon.validate(name, data)
    return {"ok": not issues, "issues": issues}


@router.post("/lexicons/{name}/reset")
async def reset_lexicon(request: Request, name: str) -> dict:
    _guard(request)
    if name not in lexicon.LEXICON_FILES:
        raise HTTPException(status_code=404, detail=f"未知词库: {name}")
    result = lexicon.reset_to_default(name)
    return {"ok": True, **result, "applied": lexicon.applied_status()}


@router.get("/usage")
async def lexicon_usage(request: Request) -> dict:
    _guard(request)
    services = getattr(request.app.state, "v2", None)
    db = getattr(services, "db", None)
    return lexicon.usage_stats(db)


def _services(request: Request) -> Any:
    services = getattr(request.app.state, "v2", None)
    if services is None:
        raise HTTPException(status_code=503, detail="服务未初始化")
    return services


@router.get("/config")
async def get_config(request: Request) -> dict:
    """模型 / STT / TTS 配置快照（密钥只返回指纹）。"""
    _guard(request)
    return admin_config.snapshot(_services(request))


@router.put("/config/{target}")
async def put_config(request: Request, target: str, payload: dict = Body(...)) -> dict:
    """保存某类配置；默认**先验证再落盘**，验证失败不保存（force=true 可强存）。"""
    _guard(request)
    handlers = {
        "llm": admin_config.save_llm,
        "stt": admin_config.save_stt,
        "tts": admin_config.save_tts,
        "deepgram": admin_config.save_deepgram,
    }
    handler = handlers.get(target)
    if handler is None:
        raise HTTPException(status_code=404, detail=f"未知配置项: {target}")
    return await handler(_services(request), payload)


@router.post("/config/{target}/test")
async def test_config(request: Request, target: str) -> dict:
    """用当前配置做连通性测试。"""
    _guard(request)
    handlers = {
        "llm": admin_config.test_llm,
        "stt": admin_config.test_stt,
        "tts": admin_config.test_tts,
        "deepgram": admin_config.test_deepgram,
    }
    handler = handlers.get(target)
    if handler is None:
        raise HTTPException(status_code=404, detail=f"未知配置项: {target}")
    try:
        return await handler(_services(request))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}


@router.get("/preview-personas")
async def preview_personas(
    request: Request, count: int = 3, language: str = "en"
) -> dict:
    """按当前素材池试捏人物（不写库、不调模型），用于预览管理台改动的效果。"""
    _guard(request)
    try:
        from app.v3 import daily_world  # noqa: PLC0415

        return daily_world.preview_personas(count=count, language=language)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}", "personas": []}


# --------------------------------------------------------------------------- #
# 角色工坊：全量重捏 / 单角色属性 / 新建测试角色
# --------------------------------------------------------------------------- #
def _studio(request: Request):
    from app.admin import persona_studio  # noqa: PLC0415

    db = getattr(getattr(request.app.state, "v2", None), "db", None)
    service = getattr(request.app.state, "v3", None)
    store = getattr(request.app.state, "conversation_store", None)
    return persona_studio, db, service, store


@router.get("/personas")
async def admin_list_personas(request: Request, query: str = "", limit: int = 300) -> dict:
    _guard(request)
    persona_studio, db, _service, _store = _studio(request)
    if db is None:
        return {"ok": False, "issues": ["数据库未就绪"]}
    return {
        "ok": True,
        "personas": persona_studio.list_personas(db, query=query, limit=limit),
        "counts": persona_studio.rebuild_preview(db)["counts"],
        "users": persona_studio.list_users(db, limit=100),
    }


@router.get("/personas/users")
async def admin_persona_users(request: Request) -> dict:
    _guard(request)
    persona_studio, db, _service, _store = _studio(request)
    return {"ok": True, "users": persona_studio.list_users(db, limit=200) if db else []}


@router.get("/personas/rebuild-preview")
async def admin_persona_rebuild_preview(request: Request) -> dict:
    _guard(request)
    persona_studio, db, _service, _store = _studio(request)
    return {"ok": True, **persona_studio.rebuild_preview(db)}


@router.post("/personas/rematch-media")
async def admin_rematch_media(request: Request, body: dict = Body(...)) -> dict:
    """修正存量图文帖：用 LLM 按图片内容重写文案（图片不变，文字与图相关）。"""
    _guard(request)
    persona_studio, db, _service, _store = _studio(request)
    if db is None:
        return {"ok": False, "issues": ["数据库未就绪"]}
    engines = getattr(request.app.state, "engines", None)
    llm = getattr(engines, "llm", None) if engines is not None else None
    return await persona_studio.rematch_media(db, llm, limit=int(body.get("limit") or 20))


@router.post("/personas/rebalance-genders")
async def admin_rebalance_genders(request: Request, body: dict = Body(...)) -> dict:
    """把现有 AI 角色性别调整到大致 1:1（保留角色 ID/好友/聊天，只换名字/性别/头像/声音）。"""
    _guard(request)
    persona_studio, db, service, _store = _studio(request)
    if db is None:
        return {"ok": False, "issues": ["数据库未就绪"]}
    return persona_studio.rebalance_genders(
        db, service, female_ratio=float(body.get("female_ratio") or 0.5)
    )


@router.post("/personas/redistribute-voices")
async def admin_redistribute_voices(request: Request) -> dict:
    """把素材池音色均匀重新分配给全部角色。"""
    _guard(request)
    persona_studio, db, service, _store = _studio(request)
    if db is None:
        return {"ok": False, "issues": ["数据库未就绪"]}
    return persona_studio.redistribute_voices(db, service)


@router.post("/personas/regenerate-posts")
async def admin_regenerate_posts(request: Request, body: dict = Body(...)) -> dict:
    """重建全部角色的动态（每人 N 条、错峰、按语言素材库随机）。"""
    _guard(request)
    persona_studio, db, service, _store = _studio(request)
    if db is None:
        return {"ok": False, "issues": ["数据库未就绪"]}
    return persona_studio.regenerate_posts(
        db, service, per_persona=int(body.get("per_persona") or 5)
    )


@router.post("/personas/init-life")
async def admin_init_life_layer(request: Request) -> dict:
    """补齐作息表 / Speech DNA / 示例对话 / 人物关系（0 次 LLM）。"""
    _guard(request)
    persona_studio, db, service, _store = _studio(request)
    if db is None:
        return {"ok": False, "issues": ["数据库未就绪"]}
    result = await persona_studio.init_life_layer(db)
    persona_studio.invalidate(service)
    return {"ok": True, **result}


@router.post("/personas/rebuild")
async def admin_persona_rebuild(request: Request, body: dict = Body(...)) -> dict:
    _guard(request)
    persona_studio, db, service, store = _studio(request)
    if db is None:
        return {"ok": False, "issues": ["数据库未就绪"]}
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
    _guard(request)
    persona_studio, db, service, _store = _studio(request)
    if db is None:
        return {"ok": False, "issues": ["数据库未就绪"]}
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
    _guard(request)
    persona_studio, db, _service, _store = _studio(request)
    detail = persona_studio.persona_detail(db, persona_id) if db else None
    if not detail:
        raise HTTPException(status_code=404, detail="角色不存在")
    return {"ok": True, "persona": detail}


@router.put("/personas/{persona_id}")
async def admin_update_persona(request: Request, persona_id: str, body: dict = Body(...)) -> dict:
    _guard(request)
    persona_studio, db, service, _store = _studio(request)
    result = persona_studio.update_persona(db, persona_id, body or {})
    if result.get("ok"):
        persona_studio.invalidate(service)
    return result


@router.post("/personas/{persona_id}/reroll")
async def admin_reroll_persona(request: Request, persona_id: str, body: dict = Body(...)) -> dict:
    _guard(request)
    persona_studio, db, service, _store = _studio(request)
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
    _guard(request)
    persona_studio, db, service, store = _studio(request)
    result = persona_studio.delete_persona(
        db, service, persona_id, backup=True, conversation_store=store
    )
    return result


@router.get("/voice-pool")
async def get_voice_pool(request: Request) -> dict:
    """Fish 音色池：素材池 + 已分配统计 + Fish 可用状态。"""
    _guard(request)
    from app.admin import voice_pool  # noqa: PLC0415

    db = getattr(getattr(request.app.state, "v2", None), "db", None)
    return voice_pool.status(db)


@router.get("/voice-pool/models")
async def list_voice_pool_models(request: Request, refresh: bool = False) -> dict:
    """列出 Fish 账号下全部音色（分页拉取，最多 1000 条）。"""
    _guard(request)
    from app.admin import voice_pool  # noqa: PLC0415

    try:
        models = await voice_pool.list_all_models(refresh=refresh)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "issues": [f"{type(exc).__name__}: {exc}"], "models": []}
    return {"ok": True, "models": models, "count": len(models)}


@router.post("/voice-pool/validate")
async def validate_voice_pool(request: Request, body: dict = Body(...)) -> dict:
    """输入音色 id → 真实合成一句做检测；返回检测结果与试听缓存状态。"""
    _guard(request)
    from app.admin import voice_pool  # noqa: PLC0415

    reference_id = voice_pool.resolve_id(body.get("reference_id") or "")
    if not reference_id:
        return {"ok": False, "issues": ["音色 id（或素材池名称）不能为空"]}
    try:
        audio, media = await voice_pool.preview_audio(reference_id, refresh=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "issues": [f"{type(exc).__name__}: {exc}"]}
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
    _guard(request)
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
    return voice_pool.add_voice(
        reference_id,
        name=str(body.get("name") or ""),
        gender=str(body.get("gender") or ""),
        language=str(body.get("language") or ""),
        source=str(body.get("source") or "manual"),
    )


@router.post("/voice-pool/remove")
async def remove_voice_pool(request: Request, body: dict = Body(...)) -> dict:
    _guard(request)
    from app.admin import voice_pool  # noqa: PLC0415

    return voice_pool.remove_voice(str(body.get("reference_id") or ""))


@router.post("/voice-pool/resolve")
async def resolve_voice_pool(request: Request, body: dict = Body(...)) -> dict:
    """把「id 或名称」解析成素材池音色（给角色工坊输入框用）。"""
    _guard(request)
    from app.admin import voice_pool  # noqa: PLC0415

    query = str(body.get("q") or body.get("reference_id") or "").strip()
    hit = voice_pool.resolve(query)
    return {"ok": True, "match": hit, "resolved_id": (hit or {}).get("reference_id") or query}


@router.post("/voice-pool/preview")
async def preview_voice_pool(request: Request, body: dict = Body(...)) -> Response:
    """试听：返回 mp3 字节（管理页用 fetch+blob 播放，不把 token 放到 URL）。"""
    _guard(request)
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


@router.get("/app-release")
async def get_app_release(request: Request) -> dict:
    """当前 APK 版本、历史版本与服务器上的安装包文件。"""
    _guard(request)
    from app import app_release  # noqa: PLC0415

    return app_release.status()


@router.post("/app-release/upload")
async def upload_app_release(
    request: Request,
    file: UploadFile = File(...),
    version_code: int = Form(...),
    version_name: str = Form(...),
    changelog: str = Form(""),
    force: bool = Form(False),
    min_version_code: int = Form(0),
    platform: str = Form("android"),
    channel: str = Form("release"),
) -> dict:
    """上传 APK 并发布（旧版本自动进历史，可回滚）。"""
    _guard(request)
    from app import app_release  # noqa: PLC0415

    data = await file.read()
    result = app_release.publish(
        file.filename or "upload.apk",
        data,
        version_code=version_code,
        version_name=version_name,
        changelog=changelog,
        force=force,
        min_version_code=min_version_code,
        platform=platform,
        channel=channel,
    )
    if not result.get("ok"):
        return result
    return result


@router.post("/app-release/rollback")
async def rollback_app_release(request: Request) -> dict:
    """回滚到上一版 APK。"""
    _guard(request)
    from app import app_release  # noqa: PLC0415

    return app_release.rollback()


@router.get("/simulate")
async def simulate_world(request: Request) -> dict:
    """按当前词库参数估算今天会产出多少事件/动态/评论（只读，不写库、不调模型）。"""
    _guard(request)
    services = getattr(request.app.state, "v2", None)
    db = getattr(services, "db", None)
    return lexicon.simulate_today(db)


__all__ = ["page_router", "router"]
