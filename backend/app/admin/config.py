"""管理端配置：模型 / 语音识别 / 语音合成（含密钥）集中管理。

为什么放这里
------------
客户端是给零基础客户用的，不应该出现任何 key 输入框；配置面统一收进管理端
（仅本机 / 带 token 可访问）。所有"写"都遵循同一条原则：

1. **先验证，再落盘**：LLM 用真实 1 次极小请求验证；TTS 用 Fish 音色列表探测；
   STT 硅基流动走官方 model 列表探测。验证失败默认**不保存**（force=True 才强存）。
2. **密钥只回指纹**：GET 只返回 ``sk-a…2345``，不回传明文。
3. **空值 = 不改**：表单留空的 key 不会被覆盖；``__CLEAR__`` 才显式清空。
"""

from __future__ import annotations

import dataclasses
import os
from typing import Any

from app.logging_utils import get_logger

log = get_logger(__name__)

_LLM_FIELDS = ("base_url", "model", "api_key", "suggestion_model", "temperature", "max_tokens")
_STT_FIELDS = ("provider", "base_url", "model", "api_key", "language", "timeout_s")
_TTS_FIELDS = (
    "provider",
    "fish_api_key",
    "fish_base_url",
    "fish_model",
    "fish_reference_id",
    "fish_format",
)


def _engines(services: Any) -> Any:
    engines = getattr(services, "engines", None)
    if engines is None:
        raise RuntimeError("引擎未初始化")
    return engines


def _data_dir(engines: Any) -> str:
    return str(getattr(getattr(engines, "settings", None), "data_path", "") or "data")


def _llm_store(engines: Any) -> Any:
    from app.llm import runtime_config as rc  # noqa: PLC0415

    store = getattr(engines, "llm_config", None)
    if store is None:
        settings = engines.settings
        store = rc.LLMConfigStore(
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


def _stt_store(engines: Any) -> Any:
    from app.voice.stt_config import STTConfigStore  # noqa: PLC0415

    store = getattr(engines, "stt_config", None)
    if store is None:
        settings = engines.settings
        stt = getattr(settings, "stt", None)
        store = STTConfigStore(
            _data_dir(engines),
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


def _tts_store(engines: Any) -> Any:
    from app.voice.tts_config import TTSConfigStore  # noqa: PLC0415

    return TTSConfigStore(_data_dir(engines))


def _clean_patch(payload: dict, fields: tuple[str, ...]) -> dict[str, Any]:
    """只取已知字段；空字符串=不改；``__CLEAR__``=清空。"""
    patch: dict[str, Any] = {}
    for field in fields:
        if field not in payload:
            continue
        value = payload[field]
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if value == "":
                continue
            if value == "__CLEAR__":
                patch[field] = ""
                continue
        patch[field] = value
    return patch


def snapshot(services: Any) -> dict[str, Any]:
    """当前配置（密钥只给指纹）。失败部分返回 error，不影响其它部分。"""
    out: dict[str, Any] = {}
    try:
        engines = _engines(services)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    try:
        llm = _llm_store(engines).config
        out["llm"] = {
            **llm.public(),
            "configured": llm.is_configured(),
            "masked_key": llm.masked_key(),
        }
    except Exception as exc:  # noqa: BLE001
        out["llm"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        out["stt"] = _stt_store(engines).public()
    except Exception as exc:  # noqa: BLE001
        out["stt"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        tts = _tts_store(engines).load()
        out["tts"] = tts.public()
    except Exception as exc:  # noqa: BLE001
        out["tts"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        from app.voice import voice_registry  # noqa: PLC0415

        out["fish"] = voice_registry.fish_status()
    except Exception:  # noqa: BLE001
        out["fish"] = {}
    try:
        from app.voice.deepgram_stream import deepgram_status  # noqa: PLC0415

        out["deepgram"] = deepgram_status()
    except Exception as exc:  # noqa: BLE001
        out["deepgram"] = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    out["client_locked"] = str(os.getenv("ALLOW_KEY_EDIT", "0")).strip().lower() not in (
        "1",
        "true",
        "yes",
        "on",
    )
    return out


# --------------------------------------------------------------------------- #
# LLM
# --------------------------------------------------------------------------- #
async def save_llm(services: Any, payload: dict[str, Any]) -> dict[str, Any]:
    engines = _engines(services)
    store = _llm_store(engines)
    force = bool(payload.get("force"))
    patch = _clean_patch(payload, _LLM_FIELDS)
    if not patch:
        return {"ok": False, "issues": ["没有要更新的字段"]}

    from app.llm import runtime_config as rc  # noqa: PLC0415

    old = store.config
    merged = rc.LLMRuntimeConfig(**{**old.to_file_dict(), **patch})
    if not merged.is_configured():
        return {"ok": False, "issues": ["base_url 与 model 都必须填写"]}

    # 先验证，再落盘（避免"改坏了客户端直接用不了"）
    verified: dict[str, Any] | None = None
    if not payload.get("skip_verify"):
        try:
            verified = await rc.verify_config(merged)
        except Exception as exc:  # noqa: BLE001
            verified = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        if not verified.get("ok") and not force:
            return {
                "ok": False,
                "verified": verified,
                "issues": [
                    "连接测试失败，未保存（可在弹窗里勾选「忽略测试，强制保存」）："
                    + str(verified.get("message") or "未知错误")[:200]
                ],
            }
    applied = await engines.apply_llm_config(**patch)
    return {"ok": True, "config": applied["config"], "verified": verified}


async def test_llm(services: Any) -> dict[str, Any]:
    from app.llm import runtime_config as rc  # noqa: PLC0415

    engines = _engines(services)
    cfg = _llm_store(engines).config
    return await rc.verify_config(cfg)


# --------------------------------------------------------------------------- #
# TTS
# --------------------------------------------------------------------------- #
async def _probe_tts(cfg: Any) -> dict[str, Any]:
    if cfg.provider != "fish":
        return {"ok": True, "message": "当前使用 Edge（免费），无需密钥"}
    if not str(cfg.fish_api_key or "").strip():
        return {"ok": False, "message": "Fish 模式需要 API Key"}
    try:
        from app.voice import fish_tts  # noqa: PLC0415

        rows = await fish_tts.list_models(
            api_key=cfg.fish_api_key,
            base_url=cfg.fish_base_url,
            page_number=1,
            page_size=1,
            timeout=8.0,
        )
        return {"ok": True, "message": f"Fish 连通（拿到 {len(rows)} 个音色样本）"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}


async def save_tts(services: Any, payload: dict[str, Any]) -> dict[str, Any]:
    engines = _engines(services)
    store = _tts_store(engines)
    force = bool(payload.get("force"))
    patch = _clean_patch(payload, _TTS_FIELDS)
    if not patch:
        return {"ok": False, "issues": ["没有要更新的字段"]}
    old = store.load()
    try:
        candidate = dataclasses.replace(old, **patch)  # type: ignore[type-var]
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "issues": [f"配置不合法：{exc}"]}
    verified: dict[str, Any] | None = None
    if "provider" in patch or any(key.startswith("fish_") for key in patch):
        verified = await _probe_tts(candidate)
        if not verified.get("ok") and not force:
            return {"ok": False, "verified": verified, "issues": [str(verified.get("message"))]}
    cfg = store.save(**patch)
    try:
        from app.voice import voice_registry  # noqa: PLC0415

        voice_registry.reset_state()
        if verified and verified.get("ok"):
            voice_registry.mark_fish_success()
        elif cfg.provider == "fish" and verified and not verified.get("ok"):
            voice_registry.mark_fish_failure(RuntimeError(str(verified.get("message"))[:120]))
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "tts": cfg.public(), "verified": verified}


async def test_tts(services: Any) -> dict[str, Any]:
    engines = _engines(services)
    cfg = _tts_store(engines).load()
    result = await _probe_tts(cfg)
    # 连通性测试结果同步给熔断器：测通即恢复 Fish，测不通就继续降级
    try:
        from app.voice import voice_registry  # noqa: PLC0415

        if result.get("ok"):
            voice_registry.mark_fish_success()
        elif cfg.provider == "fish":
            voice_registry.mark_fish_failure(RuntimeError(str(result.get("message"))[:120]))
    except Exception:  # noqa: BLE001
        pass
    return result


# --------------------------------------------------------------------------- #
# STT
# --------------------------------------------------------------------------- #
async def save_stt(services: Any, payload: dict[str, Any]) -> dict[str, Any]:
    engines = _engines(services)
    store = _stt_store(engines)
    force = bool(payload.get("force"))
    patch = _clean_patch(payload, _STT_FIELDS)
    if not patch:
        return {"ok": False, "issues": ["没有要更新的字段"]}
    old = store.config
    try:
        candidate = dataclasses.replace(old, **patch)  # type: ignore[type-var]
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "issues": [f"配置不合法：{exc}"]}
    verified: dict[str, Any] | None = None
    if candidate.provider == "siliconflow" and not payload.get("skip_verify"):
        try:
            from app.voice.stt_config import verify_config as verify_stt_config  # noqa: PLC0415

            verified = await verify_stt_config(candidate)
        except Exception as exc:  # noqa: BLE001
            verified = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        if not verified.get("ok") and not force:
            return {"ok": False, "verified": verified, "issues": [str(verified.get("message"))]}
    cfg = store.save(**patch)
    apply_fn = getattr(getattr(engines, "stt", None), "apply_cloud_config", None)
    if callable(apply_fn):
        try:
            apply_fn(cfg.to_engine_dict())
        except Exception as exc:  # noqa: BLE001
            log.warning("应用 STT 配置失败: %s", exc)
    return {"ok": True, "stt": cfg.public(), "verified": verified}


# --------------------------------------------------------------------------- #
# Deepgram（通话流式识别）
# --------------------------------------------------------------------------- #
async def _probe_deepgram(key: str) -> dict[str, Any]:
    key = str(key or "").strip()
    if not key:
        return {"ok": False, "message": "未配置 Deepgram Key"}
    try:
        import httpx  # noqa: PLC0415

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://api.deepgram.com/v1/projects",
                headers={"Authorization": f"Token {key}"},
            )
        if resp.status_code == 200:
            return {"ok": True, "message": "Deepgram 连接成功（Key 有效）"}
        if resp.status_code in (401, 403):
            return {"ok": False, "message": f"Key 无效（HTTP {resp.status_code}）"}
        return {"ok": False, "message": f"Deepgram 返回 HTTP {resp.status_code}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}


async def save_deepgram(services: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """保存/清除 Deepgram Key。空=不改，__CLEAR__=清除运行时 key（env 仍生效）。"""
    from app.voice import deepgram_stream as dg  # noqa: PLC0415

    raw = payload.get("api_key")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {"ok": False, "issues": ["请填写 Deepgram API Key（留空 = 不修改）"]}
    key = str(raw).strip()
    if key == "__CLEAR__":
        dg.save_runtime_key("")
        return {"ok": True, "deepgram": dg.deepgram_status(), "verified": None}
    if len(key) < 16:
        return {"ok": False, "issues": ["Key 看起来不完整（Deepgram key 一般 32/40 位）"]}

    verified = await _probe_deepgram(key)
    if not verified.get("ok") and not payload.get("force"):
        return {
            "ok": False,
            "verified": verified,
            "issues": ["连接测试失败，未保存：" + str(verified.get("message"))],
        }
    if not dg.save_runtime_key(key):
        return {"ok": False, "issues": ["写入 data/deepgram_config.json 失败"]}
    return {
        "ok": True,
        "deepgram": dg.deepgram_status(),
        "verified": verified,
    }


async def test_deepgram(services: Any) -> dict[str, Any]:
    from app.voice.deepgram_stream import deepgram_key  # noqa: PLC0415

    return await _probe_deepgram(deepgram_key())


async def test_stt(services: Any) -> dict[str, Any]:
    from app.voice.stt_config import verify_config as verify_stt_config  # noqa: PLC0415

    engines = _engines(services)
    cfg = _stt_store(engines).config
    if cfg.provider == "local":
        return {"ok": True, "message": "本地 Whisper 模式，无需联网验证"}
    try:
        return await verify_stt_config(cfg)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}


__all__ = [
    "save_llm",
    "save_stt",
    "save_tts",
    "snapshot",
    "test_llm",
    "test_stt",
    "test_tts",
]
