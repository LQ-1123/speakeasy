"""管理端配置（模型 / 密钥）测试：掩码、先验证再落盘、强制保存、客户端锁定。

不联网：LLM 验证、TTS 探测、STT 验证都在测试里 monkeypatch。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin import config as admin_config
from app.admin.router import router as admin_router
from app.llm import runtime_config as rc
from app.voice.stt_config import STTConfigStore
from app.voice.tts_config import TTSConfigStore


class FakeEngines:
    """最小可用引擎壳：只实现配置读写所需的部分。"""

    def __init__(self, tmp_path):
        self.settings = SimpleNamespace(
            data_path=str(tmp_path),
            llm=SimpleNamespace(
                base_url="https://api.deepseek.com/v1",
                api_key="sk-env-key-0000",
                model="deepseek-chat",
                suggestion_model="",
                temperature=0.8,
                max_tokens=1024,
            ),
            stt=SimpleNamespace(
                provider="auto",
                siliconflow_base_url="https://api.siliconflow.cn/v1",
                siliconflow_model="FunAudioLLM/SenseVoiceSmall",
                siliconflow_api_key="",
                language="en",
                siliconflow_timeout_s=30.0,
            ),
        )
        self.llm_config = rc.LLMConfigStore(
            str(tmp_path),
            env_defaults={
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "sk-env-key-0000",
                "model": "deepseek-chat",
                "suggestion_model": "",
                "temperature": 0.8,
                "max_tokens": 1024,
            },
        )
        self.stt_config = STTConfigStore(
            str(tmp_path),
            env_defaults={
                "provider": "auto",
                "base_url": "https://api.siliconflow.cn/v1",
                "model": "FunAudioLLM/SenseVoiceSmall",
                "api_key": "",
                "language": "en",
                "timeout_s": 30.0,
            },
        )
        self.stt = SimpleNamespace(apply_cloud_config=lambda cfg: setattr(self, "_stt_applied", cfg))
        self.applied: list[dict] = []

    async def apply_llm_config(self, **changes):
        cfg = self.llm_config.save(**changes)
        self.applied.append(changes)
        return {"config": cfg.public(), "verified": None}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engines = FakeEngines(tmp_path)
    app = FastAPI()
    app.state.v2 = SimpleNamespace(engines=engines)
    app.include_router(admin_router)
    monkeypatch.delenv("SE_ADMIN_TOKEN", raising=False)
    with TestClient(app) as http:
        http.engines = engines  # type: ignore[attr-defined]
        yield http


# --------------------------------------------------------------------------- #
# 快照 / 掩码
# --------------------------------------------------------------------------- #
def test_config_snapshot_masks_keys(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOW_KEY_EDIT", "0")
    TTSConfigStore(str(tmp_path)).save(provider="fish", fish_api_key="sk-fish-abcdefgh1234")
    data = client.get("/api/admin/config").json()
    assert data["llm"]["base_url"].startswith("https://")
    # 明文 key 绝不出现在响应里
    assert "sk-env-key-0000" not in str(data)
    assert "sk-fish-abcdefgh1234" not in str(data)
    assert data["tts"]["fish_api_key"]  # 只给指纹
    assert data["client_locked"] is True
    assert data["fish"] is not None


# --------------------------------------------------------------------------- #
# LLM：先验证再落盘
# --------------------------------------------------------------------------- #
def test_llm_save_requires_verification_success(client):
    async def fake_verify(cfg, **kwargs):
        assert cfg.model == "deepseek-chat"
        return {"ok": True, "message": "ok", "latency_ms": 10.0, "model_reply": "OK"}

    import app.admin.config as ac

    ac_verify = fake_verify
    original = rc.verify_config
    rc.verify_config = ac_verify  # type: ignore[assignment]
    try:
        res = client.put(
            "/api/admin/config/llm",
            json={"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
        ).json()
    finally:
        rc.verify_config = original  # type: ignore[assignment]
    assert res["ok"] is True
    assert client.engines.applied  # 已热应用


def test_llm_save_blocked_when_verification_fails(client):
    async def fake_verify(cfg, **kwargs):
        return {"ok": False, "message": "401 invalid api key"}

    original = rc.verify_config
    rc.verify_config = fake_verify  # type: ignore[assignment]
    try:
        res = client.put(
            "/api/admin/config/llm",
            json={"api_key": "sk-bad-key-123456"},
        ).json()
    finally:
        rc.verify_config = original  # type: ignore[assignment]
    assert res["ok"] is False
    assert "连接测试失败" in res["issues"][0]
    assert client.engines.applied == []  # 没有落盘/应用


def test_llm_force_save_skips_failed_verify(client):
    async def fake_verify(cfg, **kwargs):
        return {"ok": False, "message": "network down"}

    original = rc.verify_config
    rc.verify_config = fake_verify  # type: ignore[assignment]
    try:
        res = client.put(
            "/api/admin/config/llm",
            json={"model": "qwen2.5-7b", "force": True},
        ).json()
    finally:
        rc.verify_config = original  # type: ignore[assignment]
    assert res["ok"] is True
    assert client.engines.llm_config.config.model == "qwen2.5-7b"


def test_llm_invalid_config_rejected(client):
    res = client.put("/api/admin/config/llm", json={"base_url": "", "model": ""}).json()
    assert res["ok"] is False
    assert res["issues"]


# --------------------------------------------------------------------------- #
# TTS / STT
# --------------------------------------------------------------------------- #
def test_tts_fish_probe_failure_blocks_save_then_force(client, tmp_path):
    async def fake_probe(cfg):
        return {"ok": False, "message": "fish unreachable"}

    import app.admin.config as ac

    original = ac._probe_tts
    ac._probe_tts = fake_probe  # type: ignore[assignment]
    try:
        blocked = client.put(
            "/api/admin/config/tts",
            json={"provider": "fish", "fish_api_key": "sk-fish-12345678"},
        ).json()
        assert blocked["ok"] is False
        assert TTSConfigStore(str(tmp_path)).load().provider == "edge"

        forced = client.put(
            "/api/admin/config/tts",
            json={"provider": "fish", "fish_api_key": "sk-fish-12345678", "force": True},
        ).json()
        assert forced["ok"] is True
        assert TTSConfigStore(str(tmp_path)).load().provider == "fish"
    finally:
        ac._probe_tts = original  # type: ignore[assignment]


def test_tts_edge_needs_no_key(client, tmp_path):
    res = client.put("/api/admin/config/tts", json={"provider": "edge"}).json()
    assert res["ok"] is True
    assert TTSConfigStore(str(tmp_path)).load().provider == "edge"


def test_stt_local_saves_without_network(client, tmp_path):
    res = client.put("/api/admin/config/stt", json={"provider": "local"}).json()
    assert res["ok"] is True
    assert STTConfigStore(str(tmp_path)).config.provider == "local"
    assert getattr(client.engines, "_stt_applied", None) is not None


# --------------------------------------------------------------------------- #
# 客户端锁定
# --------------------------------------------------------------------------- #
def test_client_settings_locked_by_default(monkeypatch):
    from fastapi import HTTPException

    from app.v2.router import _key_edit_guard

    monkeypatch.delenv("ALLOW_KEY_EDIT", raising=False)
    with pytest.raises(HTTPException) as exc:
        _key_edit_guard()
    assert exc.value.status_code == 403
    assert "管理端" in str(exc.value.detail)

    monkeypatch.setenv("ALLOW_KEY_EDIT", "1")
    _key_edit_guard()  # 不抛异常


# --------------------------------------------------------------------------- #
# Deepgram（通话流式识别）
# --------------------------------------------------------------------------- #
def test_config_snapshot_includes_deepgram(client, monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test-key-1234567890abcdef")
    data = client.get("/api/admin/config").json()
    dg = data["deepgram"]
    assert dg["available"] is True
    assert dg["source"] == "env"
    assert dg["key_masked"]
    assert "dg-test-key-1234567890abcdef" not in str(data)


def test_deepgram_test_endpoint(client, monkeypatch):
    import app.admin.config as ac

    async def fake_probe(key: str):
        return {"ok": True, "message": "Deepgram 连接成功"}

    monkeypatch.setattr(ac, "_probe_deepgram", fake_probe)
    res = client.post("/api/admin/config/deepgram/test").json()
    assert res["ok"] is True


def test_deepgram_save_rejects_short_key(client):
    res = client.put("/api/admin/config/deepgram", json={"api_key": "short"}).json()
    assert res["ok"] is False
    assert res["issues"]


def test_deepgram_save_success_and_clear(client, monkeypatch, tmp_path):
    import app.admin.config as ac
    from app.voice import deepgram_stream as dg

    saved: dict = {}
    monkeypatch.setattr(dg, "_config_path", lambda: tmp_path / "deepgram_config.json")
    monkeypatch.setattr(dg, "save_runtime_key", lambda key: saved.update(api_key=key) or True)

    async def fake_probe(key: str):
        return {"ok": True, "message": "ok"}

    monkeypatch.setattr(ac, "_probe_deepgram", fake_probe)
    key = "dg-" + "a" * 32
    res = client.put("/api/admin/config/deepgram", json={"api_key": key}).json()
    assert res["ok"] is True
    assert saved["api_key"] == key

    res = client.put("/api/admin/config/deepgram", json={"api_key": "__CLEAR__"}).json()
    assert res["ok"] is True
    assert saved["api_key"] == ""
