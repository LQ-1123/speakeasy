"""STT 运行时配置（SenseVoiceSmall @ SiliconFlow）与个人设置 API 的离线测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.v2 import router as v2_router_module  # noqa: E402
from app.v2.router import router as v2_router  # noqa: E402
from app.voice.stt_config import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    STTConfigStore,
    STTRuntimeConfig,
)


# --------------------------------------------------------------------------- #
# Config store
# --------------------------------------------------------------------------- #
def test_store_defaults_and_auto_semantics(tmp_path):
    store = STTConfigStore(str(tmp_path))
    cfg = store.config
    assert cfg.provider == "auto"
    assert cfg.base_url == DEFAULT_BASE_URL
    assert cfg.model == DEFAULT_MODEL
    assert cfg.enabled is False  # auto + 无 key → 本地
    assert cfg.configured is False
    public = store.public()
    assert public["api_key_set"] is False
    assert public["source"] == "env"


def test_store_env_defaults(tmp_path):
    store = STTConfigStore(
        str(tmp_path),
        env_defaults={
            "provider": "siliconflow",
            "base_url": "https://api.siliconflow.cn/v1/",
            "model": "FunAudioLLM/SenseVoiceSmall",
            "api_key": "sk-env-12345678",
            "language": "zh",
            "timeout_s": 12,
        },
    )
    cfg = store.config
    assert cfg.provider == "siliconflow"
    assert cfg.base_url == "https://api.siliconflow.cn/v1"
    assert cfg.enabled is True and cfg.configured is True
    assert cfg.timeout_s == 12
    assert store.public()["api_key_masked"] == "sk-e…5678"


def test_store_save_mask_persist_and_clear(tmp_path):
    store = STTConfigStore(str(tmp_path))
    cfg = store.save(
        provider="siliconflow",
        base_url="api.siliconflow.cn/v1",
        model="FunAudioLLM/SenseVoiceSmall",
        api_key="sk-test-abcdefgh",
        timeout_s=999,
    )
    assert cfg.base_url == "https://api.siliconflow.cn/v1"
    assert cfg.timeout_s == 120  # clamp
    assert cfg.configured is True

    public = store.public()
    assert public["api_key_set"] is True
    assert public["api_key_masked"] == "sk-t…efgh"
    assert "api_key" not in public

    # 文件已写入且权限尽量收紧
    saved = json.loads((tmp_path / "stt_config.json").read_text(encoding="utf-8"))
    assert saved["api_key"] == "sk-test-abcdefgh"
    assert saved["provider"] == "siliconflow"

    # 重新加载 → 文件优先
    store2 = STTConfigStore(str(tmp_path), env_defaults={"provider": "local"})
    assert store2.config.provider == "siliconflow"
    assert store2.config.source == "file"

    # 空 key = 不改
    store2.save(api_key="", model="FunAudioLLM/SenseVoiceSmall")
    assert store2.config.api_key == "sk-test-abcdefgh"

    # __CLEAR__ = 清空
    store2.save(api_key="__CLEAR__")
    assert store2.config.api_key == ""
    assert store2.config.enabled is False

    # 删除文件 → 回到 env（这里 env 是 local）
    store2.reset()
    assert store2.config.provider == "local"
    assert not (tmp_path / "stt_config.json").exists()


def test_store_rejects_bad_provider(tmp_path):
    store = STTConfigStore(str(tmp_path))
    with pytest.raises(ValueError):
        store.save(provider="openai")


def test_runtime_config_engine_dict():
    cfg = STTRuntimeConfig(provider="auto", api_key="sk-x")
    payload = cfg.to_engine_dict()
    assert payload["provider"] == "auto"
    assert payload["enabled"] is True
    assert payload["api_key"] == "sk-x"


# --------------------------------------------------------------------------- #
# Settings API
# --------------------------------------------------------------------------- #
class FakeSTT:
    backend_name = "faster-whisper"

    def __init__(self) -> None:
        self.applied: dict | None = None
        self._cfg: dict = {"provider": "auto", "enabled": False, "model": DEFAULT_MODEL}

    def apply_cloud_config(self, cfg) -> None:
        self.applied = dict(cfg)
        self._cfg = dict(cfg)

    def cloud_health(self) -> dict:
        enabled = bool(self._cfg.get("enabled"))
        return {
            "enabled": enabled,
            "provider": self._cfg.get("provider", "auto"),
            "configured": enabled and bool(self._cfg.get("api_key")),
            "backend": "sensevoice" if enabled else self.backend_name,
            "model": self._cfg.get("model", DEFAULT_MODEL),
            "last_error": None,
            "last_latency_ms": None,
            "requests": 0,
            "last_success_at": None,
        }


def _make_engines(tmp_path) -> SimpleNamespace:
    stt = FakeSTT()
    settings = SimpleNamespace(
        data_path=str(tmp_path),
        stt=SimpleNamespace(
            provider="auto",
            siliconflow_base_url=DEFAULT_BASE_URL,
            siliconflow_model=DEFAULT_MODEL,
            siliconflow_api_key="",
            language="en",
            siliconflow_timeout_s=30.0,
        ),
        llm=SimpleNamespace(
            base_url="https://api.deepseek.com/v1",
            api_key="",
            model="deepseek-chat",
            suggestion_model="",
            temperature=0.85,
            max_tokens=220,
        ),
    )
    engines = SimpleNamespace(
        stt=stt,
        tts=SimpleNamespace(backend_name="edge"),
        llm=SimpleNamespace(health=lambda: {"configured": False, "mock": True}),
        settings=settings,
        llm_config=None,
        stt_config=None,
    )
    return engines


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engines = _make_engines(tmp_path)
    app = FastAPI()
    app.include_router(v2_router)
    app.state.engines = engines

    calls: list[dict] = []

    async def fake_verify(cfg, **kwargs):
        calls.append(cfg.to_engine_dict())
        return {"ok": True, "message": "连接成功，SenseVoiceSmall 正常响应", "latency_ms": 12.3, "text": ""}

    monkeypatch.setattr(v2_router_module, "verify_stt_config", fake_verify)
    with TestClient(app) as c:
        yield c, engines, calls


def test_settings_aggregate(client):
    http, engines, _ = client
    data = http.get("/api/v2/settings").json()
    assert data["tts"]["backend"] == "edge"
    assert data["llm"]["config"]["model"] == "deepseek-chat"
    assert data["llm"]["presets"]
    stt = data["stt"]
    assert stt["config"]["provider"] == "auto"
    assert stt["config"]["api_key_set"] is False
    assert [p["id"] for p in stt["presets"]] == ["local", "siliconflow"]
    assert stt["health"]["backend"] == "faster-whisper"


def test_save_stt_config_hot_applies(client):
    http, engines, calls = client
    resp = http.post(
        "/api/v2/settings/stt",
        json={
            "provider": "siliconflow",
            "base_url": "https://api.siliconflow.cn/v1",
            "model": "FunAudioLLM/SenseVoiceSmall",
            "api_key": "sk-live-12345678",
            "verify": True,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["config"]["provider"] == "siliconflow"
    assert data["config"]["api_key_masked"] == "sk-l…5678"
    assert data["verified"]["ok"] is True
    engine_applied = engines.stt.applied
    assert engine_applied
    assert engine_applied["api_key"] == "sk-live-12345678"
    assert engine_applied["enabled"] is True
    assert calls and calls[-1]["provider"] == "siliconflow"

    # GET 反映已保存
    stt = http.get("/api/v2/settings/stt").json()
    assert stt["config"]["configured"] is True
    assert stt["health"]["enabled"] is True
    assert stt["health"]["backend"] == "sensevoice"

    # 空 key 保存不覆盖
    http.post("/api/v2/settings/stt", json={"provider": "siliconflow", "api_key": ""})
    assert engines.stt.applied["api_key"] == "sk-live-12345678"


def test_save_stt_config_verify_failure_still_saves(client):
    http, engines, calls = client

    async def failing_verify(cfg, **kwargs):
        return {"ok": False, "message": "API Key 无效或无权限（401）", "latency_ms": 5.0, "text": ""}

    original = v2_router_module.verify_stt_config
    v2_router_module.verify_stt_config = failing_verify
    try:
        resp = http.post(
            "/api/v2/settings/stt",
            json={"provider": "siliconflow", "api_key": "sk-bad", "verify": True},
        )
    finally:
        v2_router_module.verify_stt_config = original
    data = resp.json()
    assert data["ok"] is True
    assert data["verified"]["ok"] is False
    assert "401" in data["verified"]["message"]


def test_stt_test_endpoint_uses_current_key(client):
    http, engines, calls = client
    http.post("/api/v2/settings/stt", json={"provider": "siliconflow", "api_key": "sk-saved-1234", "verify": False})
    resp = http.post("/api/v2/settings/stt/test", json={"provider": "siliconflow"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert calls[-1]["api_key"] == "sk-saved-1234"

    # 测试时显式覆盖 key
    http.post("/api/v2/settings/stt/test", json={"api_key": "sk-override-999"})
    assert calls[-1]["api_key"] == "sk-override-999"


def test_stt_reset(client):
    http, engines, _ = client
    http.post("/api/v2/settings/stt", json={"provider": "siliconflow", "api_key": "sk-x-12345678"})
    resp = http.post("/api/v2/settings/stt/reset")
    data = resp.json()
    assert data["config"]["provider"] == "auto"
    assert data["config"]["api_key_set"] is False
    assert engines.stt.applied["enabled"] is False


def test_invalid_provider_returns_422(client):
    http, _, _ = client
    resp = http.post("/api/v2/settings/stt", json={"provider": "whisper-cloud"})
    assert resp.status_code == 422
