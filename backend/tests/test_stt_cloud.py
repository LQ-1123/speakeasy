"""离线单测：STTEngine 云端热切换（SiliconFlow SenseVoiceSmall）。

覆盖：
- ``apply_cloud_config`` 支持 dict / 普通对象 / Lead 的 ``STTRuntimeConfig``；
- provider=auto / local / siliconflow 的启用语义与 ``backend_name``；
- ``cloud_health()`` 字段完整、计数与时间戳正确；
- 云端成功 → 返回 ``backend="sensevoice-siliconflow"`` 且不加载本地模型；
- 云端失败（401/超时）→ 自动回退本地 mock，绝不抛；
- provider=local 时绝不调用云端；
- ``transcribe(..., language=...)`` 透传到本地 faster-whisper（fake model，不加载真模型），
  云端路径不受影响、mock 路径的语言元数据同步覆盖。

全程不联网（httpx.MockTransport）、不加载 faster-whisper（backend=mock 或 fake model）。
"""

from __future__ import annotations

import types

import httpx
import numpy as np
import pytest

from app.config import STTSettings
from app.voice.audio_utils import SR_IN, float32_to_pcm16
from app.voice.sensevoice import DEFAULT_MODEL
from app.voice import stt as stt_mod
from app.voice.stt import STTEngine

BASE = "https://api.siliconflow.cn/v1"
API_KEY = "sk-test-abcdef"
FALLBACK_TEXT = "fallback text from mock"


def _tone_pcm(duration_s: float = 1.0, *, freq: float = 300.0) -> bytes:
    n = max(1, int(round(duration_s * SR_IN)))
    t = np.arange(n, dtype=np.float32) / float(SR_IN)
    return float32_to_pcm16(0.2 * np.sin(2.0 * np.pi * freq * t))


def _engine(**overrides) -> STTEngine:
    """本地 mock 引擎（不加载模型），用于验证云端失败后的回退。"""
    cfg = {
        "backend": "mock",
        "mock_text": FALLBACK_TEXT,
        "mock_latency_ms": 0,
    }
    cfg.update(overrides)
    return STTEngine(STTSettings(**cfg))


def _cloud_cfg(**overrides) -> dict:
    cfg = {
        "provider": "siliconflow",
        "enabled": True,
        "base_url": BASE,
        "api_key": API_KEY,
        "model": DEFAULT_MODEL,
        "language": "en",
        "timeout_s": 10.0,
    }
    cfg.update(overrides)
    return cfg


def _patch_transport(monkeypatch, handler) -> None:
    """让 apply_cloud_config 构造的客户端走 MockTransport（离线）。"""
    real_cls = stt_mod.SiliconFlowSTT

    def factory(**kwargs):
        kwargs = dict(kwargs)
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_cls(**kwargs)

    monkeypatch.setattr(stt_mod, "SiliconFlowSTT", factory)


# --------------------------------------------------------------------------- #
# apply_cloud_config / 热切换
# --------------------------------------------------------------------------- #
def test_apply_cloud_config_dict_hot_switches_to_sensevoice() -> None:
    engine = _engine()
    assert engine.backend_name == "mock"

    engine.apply_cloud_config(_cloud_cfg())

    health = engine.cloud_health()
    assert health["enabled"] is True
    assert health["provider"] == "siliconflow"
    assert health["configured"] is True
    assert health["backend"] == "sensevoice"
    assert health["model"] == DEFAULT_MODEL
    assert engine.backend_name == "sensevoice"

    # 关掉云 → 立即回到本地语义（_backend 未变）
    engine.apply_cloud_config({"provider": "local"})
    assert engine.backend_name == "mock"
    assert engine.cloud_health()["enabled"] is False
    assert engine.cloud_health()["configured"] is False


def test_apply_cloud_config_accepts_plain_object() -> None:
    engine = _engine()
    cfg = types.SimpleNamespace(
        provider="siliconflow",
        enabled=True,
        base_url=BASE,
        api_key=API_KEY,
        model=DEFAULT_MODEL,
        language="en",
        timeout_s=8.0,
    )
    engine.apply_cloud_config(cfg)

    health = engine.cloud_health()
    assert health["enabled"] is True
    assert health["configured"] is True
    assert health["backend"] == "sensevoice"
    assert engine._cloud is not None
    assert engine._cloud.timeout_s == 8.0


def test_apply_cloud_config_accepts_lead_runtime_config() -> None:
    """兼容 Lead 的 STTRuntimeConfig（走 to_engine_dict）。"""
    from app.voice.stt_config import STTRuntimeConfig

    engine = _engine()
    cfg = STTRuntimeConfig(
        provider="siliconflow",
        base_url=BASE,
        model=DEFAULT_MODEL,
        api_key=API_KEY,
        language="en",
        timeout_s=12.0,
    )
    engine.apply_cloud_config(cfg)

    health = engine.cloud_health()
    assert health["enabled"] is True
    assert health["configured"] is True
    assert engine.backend_name == "sensevoice"


def test_apply_cloud_config_auto_requires_key() -> None:
    engine = _engine()
    engine.apply_cloud_config(_cloud_cfg(provider="auto", api_key=""))
    health = engine.cloud_health()
    assert health["enabled"] is False
    assert health["configured"] is False
    assert engine.backend_name == "mock"

    engine.apply_cloud_config(_cloud_cfg(provider="auto", api_key=API_KEY))
    assert engine.cloud_health()["enabled"] is True
    assert engine.backend_name == "sensevoice"


def test_apply_cloud_config_none_disables_cloud() -> None:
    engine = _engine()
    engine.apply_cloud_config(_cloud_cfg())
    assert engine.backend_name == "sensevoice"

    engine.apply_cloud_config(None)
    assert engine.cloud_health()["enabled"] is False
    assert engine.backend_name == "mock"


def test_siliconflow_without_key_enabled_but_not_configured() -> None:
    engine = _engine()
    engine.apply_cloud_config(_cloud_cfg(api_key=""))
    health = engine.cloud_health()
    assert health["enabled"] is True
    assert health["configured"] is False
    assert engine.backend_name == "mock"  # configured=False → 不生效


def test_hot_switch_rebuilds_client_with_new_key() -> None:
    engine = _engine()
    engine.apply_cloud_config(_cloud_cfg(api_key="sk-first"))
    assert engine._cloud is not None
    first_client = engine._cloud
    assert first_client.api_key == "sk-first"

    engine.apply_cloud_config(_cloud_cfg(api_key="sk-second"))
    assert engine._cloud is not None
    assert engine._cloud is not first_client
    assert engine._cloud.api_key == "sk-second"


def test_backend_name_auto_before_prewarm_semantics() -> None:
    engine = STTEngine(STTSettings(backend="auto"))
    assert engine.backend_name == "auto"  # prewarm 前保持 1.0 语义
    engine.apply_cloud_config(_cloud_cfg())
    assert engine.backend_name == "sensevoice"
    engine.apply_cloud_config({"provider": "local"})
    assert engine.backend_name == "auto"


# --------------------------------------------------------------------------- #
# cloud_health
# --------------------------------------------------------------------------- #
def test_cloud_health_has_contract_fields() -> None:
    engine = _engine()
    health = engine.cloud_health()
    assert set(health) == {
        "enabled",
        "provider",
        "configured",
        "backend",
        "model",
        "last_error",
        "last_latency_ms",
        "requests",
        "last_success_at",
    }
    assert health["enabled"] is False
    assert health["provider"] == "auto"
    assert health["configured"] is False
    assert health["requests"] == 0
    assert health["last_error"] is None
    assert health["last_latency_ms"] is None
    assert health["last_success_at"] is None
    assert health["model"]  # 默认也展示 model 名


# --------------------------------------------------------------------------- #
# 云端成功 / 失败回退
# --------------------------------------------------------------------------- #
async def test_cloud_success_returns_sensevoice_transcript(monkeypatch) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"text": "G'day mate, how's it going?"})

    _patch_transport(monkeypatch, handler)
    engine = _engine()
    engine.apply_cloud_config(_cloud_cfg())

    transcript = await engine.transcribe(_tone_pcm(0.5))

    assert transcript.backend == "sensevoice-siliconflow"
    assert transcript.text == "G'day mate, how's it going?"
    assert transcript.language == "en"
    assert transcript.duration_s == pytest.approx(0.5, abs=0.05)
    assert len(calls) == 1
    assert engine.model_loaded is False  # 云端路径不依赖本地模型加载

    health = engine.cloud_health()
    assert health["requests"] == 1
    assert health["last_error"] is None
    assert health["last_latency_ms"] is not None
    assert health["last_latency_ms"] >= 0
    assert isinstance(health["last_success_at"], str)
    assert "T" in health["last_success_at"]


async def test_cloud_401_falls_back_to_local_mock(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "invalid api key"})

    _patch_transport(monkeypatch, handler)
    engine = _engine()
    engine.apply_cloud_config(_cloud_cfg())

    transcript = await engine.transcribe(_tone_pcm(1.0))

    # 自动回退本地 mock 链路，不抛异常
    assert transcript.backend == "mock"
    assert transcript.text == FALLBACK_TEXT
    assert engine.model_loaded is False

    health = engine.cloud_health()
    assert health["requests"] == 1
    assert health["last_error"] is not None
    assert "401" in health["last_error"] or "SenseVoiceError" in health["last_error"]
    assert health["last_success_at"] is None


async def test_cloud_timeout_falls_back_to_local_mock(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    _patch_transport(monkeypatch, handler)
    engine = _engine()
    engine.apply_cloud_config(_cloud_cfg())

    transcript = await engine.transcribe(_tone_pcm(0.5))
    assert transcript.backend == "mock"
    assert transcript.text == FALLBACK_TEXT

    health = engine.cloud_health()
    assert health["requests"] == 1
    assert health["last_error"] is not None
    assert "超时" in health["last_error"]


async def test_provider_local_never_calls_cloud(monkeypatch) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不应被调用
        calls.append(request)
        return httpx.Response(200, json={"text": "should not happen"})

    _patch_transport(monkeypatch, handler)
    engine = _engine()
    engine.apply_cloud_config(_cloud_cfg(provider="local", api_key=API_KEY))

    transcript = await engine.transcribe(_tone_pcm(0.5))

    assert transcript.backend == "mock"
    assert transcript.text == FALLBACK_TEXT
    assert calls == []
    assert engine.cloud_health()["requests"] == 0


async def test_cloud_success_then_failure_keeps_health_history(monkeypatch) -> None:
    responses = [
        httpx.Response(200, json={"text": "first ok"}),
        httpx.Response(503, text="service unavailable"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    _patch_transport(monkeypatch, handler)
    engine = _engine()
    engine.apply_cloud_config(_cloud_cfg())

    first = await engine.transcribe(_tone_pcm(0.3))
    assert first.backend == "sensevoice-siliconflow"
    success_at = engine.cloud_health()["last_success_at"]
    assert success_at is not None

    second = await engine.transcribe(_tone_pcm(0.3))
    assert second.backend == "mock"  # 第二次云端 503 → 回退本地

    health = engine.cloud_health()
    assert health["requests"] == 2
    assert health["last_error"] is not None
    assert "503" in health["last_error"]
    # 失败不会清掉上一次成功时间
    assert health["last_success_at"] == success_at


# --------------------------------------------------------------------------- #
# STT language 透传（v2.1 多语言好友：chat_service 传 friend.reply_language）
# --------------------------------------------------------------------------- #
class _FakeSegment:
    def __init__(self, text: str = "こんにちは") -> None:
        self.text = text
        self.start = 0.0
        self.end = 0.5
        self.avg_logprob = -0.1
        self.no_speech_prob = 0.01


class _FakeInfo:
    pass


class _FakeWhisperModel:
    """只记录 ``model.transcribe(**kwargs)`` 的假 faster-whisper 模型。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        return [_FakeSegment()], _FakeInfo()


def test_transcribe_sync_passes_language_to_whisper() -> None:
    engine = STTEngine(STTSettings(backend="faster-whisper", language="en"))
    model = _FakeWhisperModel()
    audio = np.zeros(SR_IN // 2, dtype=np.float32)

    # 未显式传 language → 用 cfg.language
    engine._transcribe_sync(model, audio)
    assert model.calls[-1]["language"] == "en"

    # 显式传 language → 覆盖本次调用的 cfg.language
    engine._transcribe_sync(model, audio, "ja")
    assert model.calls[-1]["language"] == "ja"

    engine._transcribe_sync(model, audio, "ko")
    assert model.calls[-1]["language"] == "ko"


async def test_transcribe_forwards_language_on_local_path(monkeypatch) -> None:
    engine = STTEngine(STTSettings(backend="faster-whisper", language="en"))

    async def fake_ensure_model():
        return object()

    multi = object()

    async def fake_ensure_multilingual_model():
        return multi

    monkeypatch.setattr(engine, "_ensure_model", fake_ensure_model)
    monkeypatch.setattr(engine, "_ensure_multilingual_model", fake_ensure_multilingual_model)

    captured: dict = {}

    def fake_sync(model, audio, language=None):
        captured["language"] = language
        captured["model"] = model
        return "名前は？", [], language or "en"

    monkeypatch.setattr(engine, "_transcribe_sync", fake_sync)

    # 非英语 → 走多语言模型，语言透传
    transcript = await engine.transcribe(_tone_pcm(0.3), SR_IN, language="ja")
    assert captured["language"] == "ja"
    assert captured["model"] is multi
    assert transcript.language == "ja"
    assert transcript.backend == "faster-whisper"

    # 不传 language → 走主模型，透传 None（_transcribe_sync 内部回落 cfg.language）
    await engine.transcribe(_tone_pcm(0.3), SR_IN)
    assert captured["language"] is None
    assert captured["model"] is not multi


async def test_transcribe_language_override_on_mock_backend() -> None:
    engine = STTEngine(STTSettings(backend="mock", mock_text="hello", mock_latency_ms=0))

    transcript = await engine.transcribe(_tone_pcm(0.2), SR_IN, language="ko")
    assert transcript.language == "ko"
    assert transcript.backend == "mock"

    default_transcript = await engine.transcribe(_tone_pcm(0.2), SR_IN)
    assert default_transcript.language == "en"


async def test_cloud_path_unaffected_by_language_kw(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "こんにちは"})

    _patch_transport(monkeypatch, handler)
    engine = _engine()
    engine.apply_cloud_config(_cloud_cfg())

    transcript = await engine.transcribe(_tone_pcm(0.3), SR_IN, language="ja")
    # 云端 SenseVoice 自动识别：仍然走云端成功路径
    assert transcript.backend == "sensevoice-siliconflow"
    assert transcript.text == "こんにちは"


async def test_non_english_without_multilingual_model_returns_empty(monkeypatch) -> None:
    engine = STTEngine(STTSettings(backend="faster-whisper", language="en"))

    async def no_multilingual():
        return None

    monkeypatch.setattr(engine, "_ensure_multilingual_model", no_multilingual)
    transcript = await engine.transcribe(_tone_pcm(0.2), SR_IN, language="ja")
    assert transcript.text == ""
    assert transcript.language == "ja"
    assert transcript.backend == "unavailable"
