"""HTTP 路由（契约见 ``docs/CONTRACTS.md`` 第 10 节、协议见 ``docs/PROTOCOL.md`` 第 3 节）。

全部依赖 ``request.app.state.engines``（``app.engines.Engines``，由 Lead 在
``main.py`` 的 lifespan 里装配），**不在 router 内 new 任何重量级引擎**。

端点：

======================  =====================================================
GET  /api/health        引擎状态（模型加载中会体现为 loading）
GET  /api/personas      角色 + 模式列表（公开字段）
GET  /api/config        前端需要的非敏感配置
GET  /api/metrics/last  最近一回合延迟分解（无数据返回 {}）
POST /api/echo          仅文本链路调试：LLM + 断句，不合成音频
POST /api/suggest       同步返回润色建议
GET  /api/ref-audio/{id} 参考音频 wav（不存在返回 404 JSON）
======================  =====================================================
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from app.config import get_settings
from app.llm.client import ChatMessage, LLMConfigError, LLMError
from app.llm.sentence_splitter import StreamingSentenceSplitter
from app.logging_utils import get_logger
from app.pipeline import metrics as metrics_mod
from app.schemas import (
    AudioConfig,
    ConfigResponse,
    EchoRequest,
    EchoResponse,
    HealthResponse,
    ModePublic,
    PersonasResponse,
    SuggestRequest,
    SuggestResponse,
)

log = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["api"])

PROTOCOL_VERSION = 1

#: /api/echo 的 LLM 超时（避免调试端点挂死）
ECHO_TIMEOUT_S = 40.0


# --------------------------------------------------------------------------- #
# 依赖取用
# --------------------------------------------------------------------------- #
def _engines(request_or_ws: Any) -> Any:
    """从 ``app.state.engines`` 取引擎；缺失时抛 503。"""
    app = getattr(request_or_ws, "app", None)
    engines = getattr(getattr(app, "state", None), "engines", None)
    if engines is None:
        raise HTTPException(status_code=503, detail="引擎尚未初始化（应用正在启动）")
    return engines


def _version(request: Request) -> str:
    return str(getattr(request.app.state, "version", "0.1.0"))


# --------------------------------------------------------------------------- #
# GET /api/health
# --------------------------------------------------------------------------- #
@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """各引擎状态。引擎构造不加载权重，因此这个端点永远快速返回。"""
    engines = _engines(request)
    settings = getattr(engines, "settings", None) or get_settings()

    try:
        llm_health = engines.llm.health()
    except Exception as exc:  # noqa: BLE001 - 健康检查绝不 500
        log.warning("llm.health() failed: %s", exc)
        llm_health = {"configured": False, "error": f"{type(exc).__name__}: {exc}"}

    engines_snapshot: dict[str, Any] = {}
    try:
        engines_snapshot = engines.snapshot()
    except Exception as exc:  # noqa: BLE001
        log.debug("engines.snapshot() failed: %s", exc)

    warm = engines_snapshot.get("warm") or {}
    # 有预热任务在进行 → loading；全部 ready → ok；有失败 → degraded
    if any(str(v).startswith(("pending", "loading")) for v in warm.values()):
        status = "loading"
    elif any(str(v).startswith("failed") for v in warm.values()):
        status = "degraded"
    elif not warm and getattr(settings.tts, "prewarm", True):
        status = "loading"
    else:
        status = "ok"

    gpu: dict[str, Any] = {}
    try:
        from app.engines import gpu_info  # noqa: PLC0415 - 延迟 import 以免循环依赖

        gpu = gpu_info()
    except Exception as exc:  # noqa: BLE001
        gpu = {"available": False, "error": f"{type(exc).__name__}"}

    return HealthResponse(
        status=status,  # type: ignore[arg-type]
        version=_version(request),
        engines=engines_snapshot or {"stt": "unknown", "tts": "unknown"},
        llm=llm_health,
        gpu=gpu,
    )


# --------------------------------------------------------------------------- #
# GET /api/personas
# --------------------------------------------------------------------------- #
@router.get("/personas", response_model=PersonasResponse)
async def personas(request: Request) -> PersonasResponse:
    """角色 + 模式列表（公开字段，不含 prompt 全文）。"""
    engines = _engines(request)
    lib = engines.personas
    return PersonasResponse(
        personas=lib.public_list(),
        modes=[ModePublic(**m) for m in lib.mode_public_list()],
        default_persona_id=lib.default_id,
    )


# --------------------------------------------------------------------------- #
# GET /api/config
# --------------------------------------------------------------------------- #
@router.get("/config", response_model=ConfigResponse)
async def config(request: Request) -> ConfigResponse:
    """前端需要的非敏感配置。"""
    engines = _engines(request)
    settings = getattr(engines, "settings", None) or get_settings()
    try:
        llm_health = engines.llm.health()
    except Exception as exc:  # noqa: BLE001
        log.debug("llm.health() failed: %s", exc)
        llm_health = {"configured": False}

    tts_backends = ["tone", "cosyvoice2", "f5tts", "mock"]

    return ConfigResponse(
        protocol_version=PROTOCOL_VERSION,
        audio=AudioConfig(
            format="pcm_s16le",
            input_sample_rate=16000,
            sample_rate=int(getattr(settings.tts, "sample_rate", 24000)),
            channels=1,
        ),
        engines={
            "stt": _safe_attr(engines.stt, "backend_name", "mock"),
            "tts": _safe_attr(engines.tts, "backend_name", "tone"),
            "tts_backends": tts_backends,
            "vad": str(getattr(settings.vad, "backend", "auto")),
        },
        llm=llm_health,
        features={
            "suggestions": bool(getattr(settings.pipeline, "suggestions_enabled", True)),
            "suggestions_auto": bool(getattr(settings.pipeline, "suggestions_auto", True)),
            "barge_in": bool(getattr(settings.pipeline, "barge_in_enabled", True)),
            "ref_audio": True,
            "text_input": True,
        },
    )


def _safe_attr(obj: Any, name: str, default: str) -> str:
    """读属性但绝不抛（引擎可能尚未加载）。"""
    try:
        value = getattr(obj, name, default)
        return str(value) if value is not None else default
    except Exception:  # noqa: BLE001
        return default


# --------------------------------------------------------------------------- #
# GET /api/metrics/last
# --------------------------------------------------------------------------- #
@router.get("/metrics/last")
async def metrics_last() -> dict[str, Any]:
    """最近一回合延迟分解；无数据返回 ``{}``。"""
    try:
        return metrics_mod.last()
    except Exception as exc:  # noqa: BLE001
        log.debug("metrics.last failed: %s", exc)
        return {}


@router.get("/metrics/history")
async def metrics_history(limit: int = 20) -> dict[str, Any]:
    """最近若干回合（旧→新），便于前端画延迟曲线。"""
    limit = max(0, min(int(limit), 200))
    items = metrics_mod.history(limit or None)
    return {
        "items": items,
        "count": metrics_mod.count(),
        "avg_first_audio_ms": metrics_mod.average("first_audio_ms"),
    }


# --------------------------------------------------------------------------- #
# POST /api/echo —— 仅文本链路
# --------------------------------------------------------------------------- #
@router.post("/echo", response_model=EchoResponse)
async def echo(request: Request, payload: EchoRequest) -> EchoResponse:
    """只跑 LLM + 断句（不合成音频），用于无麦克风 / 无前端时调试。

    返回断句结果与拼接后的完整回复。
    """
    engines = _engines(request)
    settings = getattr(engines, "settings", None) or get_settings()

    persona_id = payload.persona_id or engines.personas.default_id
    persona = engines.personas.get(persona_id)
    mode_id = payload.mode_id
    system_prompt = engines.personas.system_prompt_for(persona.id, mode_id)
    if persona.scene:
        system_prompt = system_prompt.rstrip() + f"\n\n## Scene\n{persona.scene.strip()}"

    messages = [
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(role="user", content=payload.text.strip()),
    ]

    splitter = StreamingSentenceSplitter.from_settings(settings.pipeline)
    sentences: list[str] = []
    parts: list[str] = []
    started = time.perf_counter()
    error: str | None = None

    async def _consume() -> None:
        async for delta in engines.llm.stream_chat(messages):
            parts.append(delta)
            sentences.extend(splitter.feed(delta))

    try:
        await asyncio.wait_for(_consume(), timeout=ECHO_TIMEOUT_S)
    except asyncio.TimeoutError:
        error = f"LLM 超时（>{ECHO_TIMEOUT_S:.0f}s）"
        log.warning("echo 超时 persona=%s", persona.id)
    except (LLMConfigError, LLMError) as exc:
        error = str(exc)
        log.warning("echo LLM 失败: %s", exc)
    except Exception as exc:  # noqa: BLE001 - 调试端点不 500
        error = f"{type(exc).__name__}: {exc}"
        log.warning("echo 未预期失败: %s", exc)

    sentences.extend(splitter.flush())
    reply = "".join(parts).strip()
    latency_ms = (time.perf_counter() - started) * 1000.0

    if error and not reply:
        # 用 200 + 空内容返回，前端好处理；错误信息放在 detail 之外不方便，
        # 因此这里用 HTTPException 让调试者一眼看到原因。
        raise HTTPException(status_code=502, detail=error)

    return EchoResponse(
        sentences=[s for s in sentences if s.strip()],
        reply_preview=reply,
        system_prompt_chars=len(system_prompt),
        latency_ms=round(latency_ms, 1),
    )


# --------------------------------------------------------------------------- #
# POST /api/suggest
# --------------------------------------------------------------------------- #
@router.post("/suggest", response_model=SuggestResponse)
async def suggest(request: Request, payload: SuggestRequest) -> SuggestResponse:
    """同步返回润色建议（失败返回空列表，不报错）。"""
    engines = _engines(request)
    persona_id = payload.persona_id or engines.personas.default_id
    persona = engines.personas.get(persona_id)

    try:
        items = await engines.suggestions.suggest(payload.text, persona_id=persona.id)
    except Exception as exc:  # noqa: BLE001 - 建议永不 500
        log.debug("POST /api/suggest failed: %s: %s", type(exc).__name__, exc)
        items = []

    return SuggestResponse(items=[i.to_dict() for i in items])


# --------------------------------------------------------------------------- #
# GET /api/ref-audio/{persona_id}
# --------------------------------------------------------------------------- #
@router.get("/ref-audio/{persona_id}")
async def ref_audio(request: Request, persona_id: str) -> Any:
    """返回角色的参考音频 wav；不存在返回 404 JSON。"""
    engines = _engines(request)
    settings = getattr(engines, "settings", None) or get_settings()
    lib = engines.personas

    if not lib.has(persona_id):
        return JSONResponse(
            status_code=404,
            content={"detail": f"未知角色: {persona_id}", "code": "persona_not_found"},
        )

    ref_dir = str(getattr(settings, "ref_audio_path", "") or "")
    if not ref_dir:
        try:
            from app.config import REF_AUDIO_DIR  # noqa: PLC0415

            ref_dir = REF_AUDIO_DIR
        except Exception:  # pragma: no cover
            ref_dir = ""

    candidates: list[str] = []
    # 1) 优先用 personas.json 里声明的文件名
    persona = lib.get(persona_id)
    declared = getattr(getattr(persona, "voice", None), "ref_audio", "") or ""
    if declared:
        candidates.append(declared)
    # 2) 常见命名兜底
    candidates.extend([f"{persona_id}.wav", f"{persona_id}_ref.wav", f"{persona_id}.mp3"])

    for name in candidates:
        if not name:
            continue
        path = name if os.path.isabs(name) else os.path.join(ref_dir, name)
        path = os.path.normpath(path)
        if os.path.isfile(path):
            media = "audio/mpeg" if path.lower().endswith(".mp3") else "audio/wav"
            return FileResponse(path, media_type=media, filename=os.path.basename(path))

    return JSONResponse(
        status_code=404,
        content={
            "detail": f"角色 {persona_id} 的参考音频尚未录制（放到 {ref_dir or 'assets/ref_audio'}）",
            "code": "ref_audio_missing",
            "persona_id": persona_id,
        },
    )


__all__ = ["router"]
