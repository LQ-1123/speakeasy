"""LLM 显式配置端点：GET 当前值 / POST 保存并热生效 / POST 连通性测试。

协议见 ``docs/PROTOCOL.md`` 第 3 节。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.llm import runtime_config as rc
from app.logging_utils import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/llm", tags=["llm"])


# --------------------------------------------------------------------------- #
# 模型
# --------------------------------------------------------------------------- #
class LLMConfigIn(BaseModel):
    """保存配置。所有字段可选：只改传了的那些。"""

    base_url: str | None = Field(default=None, description="OpenAI 兼容端点，如 https://api.deepseek.com/v1")
    api_key: str | None = Field(
        default=None,
        description="密钥。留空=不改动；传 '__CLEAR__' = 清除已保存的密钥",
    )
    model: str | None = Field(default=None, description="模型名，如 deepseek-chat")
    suggestion_model: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=16, le=8192)
    verify: bool = Field(default=True, description="保存后立刻发一次请求验证可用性")


class LLMTestIn(BaseModel):
    """只测试、不保存。字段缺省时用当前已保存的值。"""

    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


# --------------------------------------------------------------------------- #
# 取引擎
# --------------------------------------------------------------------------- #
def _engines(request: Request) -> Any:
    engines = getattr(request.app.state, "engines", None)
    if engines is None:
        raise_runtime = RuntimeError("引擎尚未初始化")
        raise raise_runtime
    return engines


def _store(engines: Any) -> rc.LLMConfigStore:
    store = getattr(engines, "llm_config", None)
    if store is None:
        s = engines.settings
        store = rc.LLMConfigStore(
            s.data_path,
            env_defaults={
                "base_url": s.llm.base_url,
                "api_key": s.llm.api_key,
                "model": s.llm.model,
                "suggestion_model": s.llm.suggestion_model,
                "temperature": s.llm.temperature,
                "max_tokens": s.llm.max_tokens,
            },
        )
        engines.llm_config = store  # type: ignore[attr-defined]
    return store


# --------------------------------------------------------------------------- #
# GET /api/llm/config
# --------------------------------------------------------------------------- #
@router.get("/config")
async def get_llm_config(request: Request) -> dict[str, Any]:
    """当前生效的 LLM 配置（**不含 key 明文**）+ 供应商预设。"""
    engines = _engines(request)
    store = _store(engines)
    health: dict[str, Any] = {}
    try:
        health = engines.llm.health()
    except Exception as exc:  # noqa: BLE001
        health = {"configured": False, "error": f"{type(exc).__name__}: {exc}"}

    return {
        "config": store.public(),
        "health": health,
        "presets": rc.PROVIDER_PRESETS,
        "note": "密钥只保存在本机 data/llm_config.json，接口永不回传明文。",
    }


# --------------------------------------------------------------------------- #
# POST /api/llm/config
# --------------------------------------------------------------------------- #
@router.post("/config")
async def save_llm_config(request: Request, payload: LLMConfigIn) -> Any:
    """保存并**立即生效**（无需重启进程）。""" 
    engines = _engines(request)
    store = _store(engines)

    changes: dict[str, Any] = {}
    for f in ("base_url", "model", "suggestion_model", "temperature", "max_tokens", "api_key"):
        v = getattr(payload, f)
        if v is not None:
            changes[f] = v

    if not changes:
        return JSONResponse(
            status_code=400,
            content={"detail": "没有要更新的字段", "code": "no_changes"},
        )

    # 保存（写盘 + 重建客户端 + 可选校验）
    try:
        result = await engines.apply_llm_config(**changes, verify=payload.verify)
    except OSError as exc:
        return JSONResponse(
            status_code=500,
            content={"detail": f"写入配置文件失败: {exc}", "code": "write_failed"},
        )

    verified = result.get("verified")
    ok = True
    message = "已保存并生效"
    if verified is not None and not verified.get("ok"):
        # 保存成功但连不上：明确告诉用户"配置存了，但用不了"
        ok = False
        message = f"配置已保存，但连接测试失败：{verified.get('message')}"

    log.info("LLM 配置已更新 ok=%s base_url=%s model=%s", ok, changes.get("base_url"), changes.get("model"))

    return JSONResponse(
        status_code=200,
        content={
            "ok": ok,
            "message": message,
            "config": result.get("config"),
            "verified": verified,
        },
    )


# --------------------------------------------------------------------------- #
# POST /api/llm/test
# --------------------------------------------------------------------------- #
@router.post("/test")
async def test_llm_config(request: Request, payload: LLMTestIn) -> dict[str, Any]:
    """只做连通性测试，不保存、不改变当前生效配置。

    未传的字段用**当前已保存**的值（所以测试时不必重新输入 key）。
    """
    engines = _engines(request)
    store = _store(engines)
    cur = store.config

    cfg = rc.LLMRuntimeConfig(
        base_url=(payload.base_url or cur.base_url),
        api_key=(payload.api_key if payload.api_key else cur.api_key),
        model=(payload.model or cur.model),
    )
    result = await rc.verify_config(cfg)
    return {"config_preview": cfg.public(), **result}


# --------------------------------------------------------------------------- #
# POST /api/llm/reset
# --------------------------------------------------------------------------- #
@router.post("/reset")
async def reset_llm_config(request: Request) -> dict[str, Any]:
    """删除运行时配置，回到 .env 的默认值。"""
    engines = _engines(request)
    store = _store(engines)
    store.reset()
    rc.apply_to_settings(engines.settings.llm, store.config)

    from app.llm.client import LLMClient
    from app.llm.suggestions import SuggestionService

    old = engines.llm
    engines.llm = LLMClient(engines.settings.llm)
    engines.suggestions = SuggestionService(
        engines.llm, engines.personas, engines.settings.llm, engines.settings.pipeline
    )
    try:
        await old.aclose()
    except Exception:  # pragma: no cover
        pass

    return {"ok": True, "message": "已恢复为 .env 默认配置", "config": store.public()}
