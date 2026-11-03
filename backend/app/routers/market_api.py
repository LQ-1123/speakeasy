"""口音市场 / 自定义角色 / 自定义场景 的 HTTP API。

端点一览
--------
口音市场
    GET    /api/market/accents             全量口音（可按 kind/locale 过滤）
    GET    /api/market/regions             地区列表（含声线数）
    GET    /api/market/summary             市场概览
    POST   /api/market/preview             试听某个声线（不需要先建角色）
    POST   /api/market/assign              给角色设置口音（覆盖内置默认）

自定义角色
    GET    /api/custom/personas            列表
    POST   /api/custom/personas            新建/更新
    DELETE /api/custom/personas/{id}       删除
    POST   /api/custom/personas/preview    用表单内容试听开场白

自定义场景
    GET    /api/custom/scenes              列表
    GET    /api/custom/scene-templates     场景模板
    POST   /api/custom/scenes              新建/更新
    DELETE /api/custom/scenes/{id}         删除
"""

from __future__ import annotations

import io
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["market"])


def _engines(request: Request) -> Any:
    engines = getattr(request.app.state, "engines", None)
    if engines is None:
        raise HTTPException(status_code=503, detail="引擎尚未初始化")
    return engines


def _library(engines: Any) -> Any:
    lib = getattr(engines, "personas", None)
    if lib is None:
        raise HTTPException(status_code=503, detail="角色库未初始化")
    return lib


# --------------------------------------------------------------------------- #
# 口音市场
# --------------------------------------------------------------------------- #
@router.get("/market/summary")
async def market_summary() -> dict[str, Any]:
    from app.voice.accent_market import market_summary as _sum  # noqa: PLC0415

    return _sum()


@router.get("/market/regions")
async def market_regions() -> dict[str, Any]:
    from app.voice.accent_market import regions  # noqa: PLC0415

    rows = regions()
    return {
        "regions": rows,
        "official_count": sum(1 for r in rows if r["kind"] == "official"),
        "fallback_count": sum(1 for r in rows if r["kind"] != "official"),
    }


@router.get("/market/accents")
async def market_accents(
    kind: str = Query("", description="official / foreign_fallback / 空=全部"),
    locale: str = Query("", description="如 en-GB"),
) -> dict[str, Any]:
    from app.voice.accent_market import all_accents, by_kind, by_locale  # noqa: PLC0415

    if kind:
        rows = by_kind(kind)
    elif locale:
        rows = by_locale(locale)
    else:
        rows = all_accents()

    # 按音色分组，方便前端展示
    by_loc: dict[str, list[dict]] = {}
    for a in rows:
        by_loc.setdefault(a["locale"], []).append(a)

    return {
        "total": len(rows),
        "accents": rows,
        "by_locale": by_loc,
    }


class PreviewIn(BaseModel):
    voice: str = Field(..., description="声线，如 en-IN-PrabhatNeural")
    text: str = Field(
        default="Right then, that's a flat white for you. Anything else?",
        description="试听文本",
    )


@router.post("/market/preview")
async def market_preview(payload: PreviewIn) -> Response:
    """试听任意声线（不需要先创建角色）。"""
    from app.voice.accent_market import get_accent  # noqa: PLC0415
    from app.voice.edge_tts_engine import EdgeTTSEngine  # noqa: PLC0415

    acc = get_accent(payload.voice)
    if acc is None:
        raise HTTPException(status_code=404, detail=f"未知声线 {payload.voice}")

    try:
        eng = EdgeTTSEngine(sample_rate=24000)
        await eng.ensure_ready()
        arr = await eng.synthesize_voice(payload.text[:300], payload.voice)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"合成失败: {exc}") from exc

    import soundfile as sf  # noqa: PLC0415

    buf = io.BytesIO()
    sf.write(buf, arr, 24000, format="WAV", subtype="PCM_16")
    return Response(content=buf.getvalue(), media_type="audio/wav")


class AssignIn(BaseModel):
    persona_id: str
    voice: str = Field("", description="空 = 取消覆盖，回到内置默认")


@router.post("/market/assign")
async def market_assign(request: Request, payload: AssignIn) -> dict[str, Any]:
    """给角色设置口音（改角色默认口音，立即生效）。"""
    engines = _engines(request)
    lib = _library(engines)

    if payload.voice:
        from app.voice.accent_market import get_accent  # noqa: PLC0415

        if get_accent(payload.voice) is None:
            raise HTTPException(status_code=404, detail=f"未知声线 {payload.voice}")

    # 自定义角色的声线存在角色本身，内置角色才用覆盖表
    if payload.persona_id.startswith("u_"):
        cp = lib.custom.get_persona(payload.persona_id) if lib.custom else None
        if cp is None:
            raise HTTPException(status_code=404, detail="自定义角色不存在")
        lib.custom.upsert_persona(
            {**cp.to_public(), "voice": payload.voice or cp.voice}
        )
        msg = f"角色 {cp.name} 的口音已更新"
    else:
        if not lib.has(payload.persona_id):
            raise HTTPException(status_code=404, detail="角色不存在")
        try:
            lib.set_voice_override(payload.persona_id, payload.voice)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        msg = (
            f"角色 {payload.persona_id} 已切换到 {payload.voice}"
            if payload.voice
            else f"角色 {payload.persona_id} 已恢复内置口音"
        )

    # 让 TTS 立刻用新声线（resolve_voice 每次都查覆盖表，无需额外同步）
    log.info("口音分配: %s -> %s", payload.persona_id, payload.voice or "(默认)")
    return {
        "ok": True,
        "persona_id": payload.persona_id,
        "voice": payload.voice,
        "message": msg,
        "overrides": lib.voice_overrides(),
    }


# --------------------------------------------------------------------------- #
# 自定义角色
# --------------------------------------------------------------------------- #
class CustomPersonaIn(BaseModel):
    id: str = ""
    name: str = Field(..., min_length=1, max_length=40)
    city: str = ""
    job: str = ""
    voice: str = "en-GB-RyanNeural"
    gender: str = "male"
    traits: str = ""
    style: str = ""
    opening_line: str = ""
    difficulty: int = 3
    scene_id: str = ""
    extra: str = ""


@router.get("/custom/personas")
async def list_custom_personas(request: Request) -> dict[str, Any]:
    engines = _engines(request)
    lib = _library(engines)
    rows = lib.custom.list_personas() if lib.custom else []
    return {
        "total": len(rows),
        "personas": [p.to_public() for p in rows],
    }


@router.post("/custom/personas")
async def upsert_custom_persona(request: Request, payload: CustomPersonaIn) -> dict[str, Any]:
    engines = _engines(request)
    lib = _library(engines)
    if lib.custom is None:
        raise HTTPException(status_code=503, detail="自定义存储不可用")

    from app.voice.accent_market import get_accent  # noqa: PLC0415

    if get_accent(payload.voice) is None:
        raise HTTPException(status_code=400, detail=f"未知声线 {payload.voice}")

    try:
        cp = lib.custom.upsert_persona(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    lib.reload_custom()
    log.info("自定义角色已保存: %s (%s)", cp.id, cp.name)
    return {"ok": True, "persona": cp.to_public()}


@router.delete("/custom/personas/{persona_id}")
async def delete_custom_persona(request: Request, persona_id: str) -> dict[str, Any]:
    engines = _engines(request)
    lib = _library(engines)
    if lib.custom is None:
        raise HTTPException(status_code=503, detail="自定义存储不可用")
    ok = lib.custom.delete_persona(persona_id)
    if not ok:
        raise HTTPException(status_code=404, detail="角色不存在")
    lib.reload_custom()
    return {"ok": True, "message": f"角色 {persona_id} 已删除"}


class PreviewPersonaIn(BaseModel):
    name: str = ""
    city: str = ""
    job: str = ""
    voice: str = "en-GB-RyanNeural"
    opening_line: str = ""
    traits: str = ""
    style: str = ""


@router.post("/custom/personas/preview")
async def preview_custom_persona(payload: PreviewPersonaIn) -> Response:
    """用表单内容试听开场白（不用先保存）。"""
    from app.voice.accent_market import get_accent  # noqa: PLC0415
    from app.voice.edge_tts_engine import EdgeTTSEngine  # noqa: PLC0415

    if get_accent(payload.voice) is None:
        raise HTTPException(status_code=400, detail=f"未知声线 {payload.voice}")
    text = (payload.opening_line or "Hello! Nice to meet you.").strip()[:300]

    try:
        eng = EdgeTTSEngine(sample_rate=24000)
        await eng.ensure_ready()
        arr = await eng.synthesize_voice(text, payload.voice)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"合成失败: {exc}") from exc

    import soundfile as sf  # noqa: PLC0415

    buf = io.BytesIO()
    sf.write(buf, arr, 24000, format="WAV", subtype="PCM_16")
    return Response(content=buf.getvalue(), media_type="audio/wav")


# --------------------------------------------------------------------------- #
# 自定义场景
# --------------------------------------------------------------------------- #
class CustomSceneIn(BaseModel):
    id: str = ""
    name: str = Field(..., min_length=1, max_length=40)
    template: str = ""
    place: str = ""
    goal: str = ""
    details: str = ""


@router.get("/custom/scene-templates")
async def scene_templates() -> dict[str, Any]:
    from app.personas.custom_store import CustomStore  # noqa: PLC0415

    rows = CustomStore.templates()
    return {"total": len(rows), "templates": rows}


@router.get("/custom/scenes")
async def list_custom_scenes(request: Request) -> dict[str, Any]:
    engines = _engines(request)
    lib = _library(engines)
    rows = lib.custom.list_scenes() if lib.custom else []
    return {
        "total": len(rows),
        "scenes": [s.to_public() for s in rows],
    }


@router.post("/custom/scenes")
async def upsert_custom_scene(request: Request, payload: CustomSceneIn) -> dict[str, Any]:
    engines = _engines(request)
    lib = _library(engines)
    if lib.custom is None:
        raise HTTPException(status_code=503, detail="自定义存储不可用")
    scenario = lib.custom.upsert_scene(payload.model_dump())
    lib.reload_custom()
    log.info("自定义场景已保存: %s (%s)", scenario.id, scenario.name)
    return {"ok": True, "scene": scenario.to_public()}


@router.delete("/custom/scenes/{scene_id}")
async def delete_custom_scene(request: Request, scene_id: str) -> dict[str, Any]:
    engines = _engines(request)
    lib = _library(engines)
    if lib.custom is None:
        raise HTTPException(status_code=503, detail="自定义存储不可用")
    ok = lib.custom.delete_scene(scene_id)
    if not ok:
        raise HTTPException(status_code=404, detail="场景不存在")
    lib.reload_custom()
    return {"ok": True, "message": f"场景 {scene_id} 已删除"}
