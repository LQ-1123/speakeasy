"""Fish 音色池：存储 / 挑选 / 试听 / 管理接口测试。"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin import voice_pool as vp
from app.admin.router import router as admin_router


@pytest.fixture()
def pool_root(tmp_path, monkeypatch):
    monkeypatch.setattr(vp, "_data_root", lambda: tmp_path)
    yield tmp_path


def test_pool_file_roundtrip(pool_root):
    assert vp.load_pool() == []
    res = vp.add_voice("voice-a", name="Alice", gender="female", language="en")
    assert res["ok"] is True
    assert len(vp.load_pool()) == 1
    row = vp.load_pool()[0]
    assert row["reference_id"] == "voice-a"
    assert row["name"] == "Alice"
    assert row["gender"] == "female"

    # 重复添加=更新而不是新增
    res = vp.add_voice("voice-a", name="Alice2")
    assert res["updated"] is True
    assert len(vp.load_pool()) == 1
    assert vp.load_pool()[0]["name"] == "Alice2"

    vp.add_voice("voice-b", name="Bob", gender="male", language="zh")
    entries = vp.pool_entries()
    assert {e["id"] for e in entries} == {"voice-a", "voice-b"}
    assert {"id": "voice-b", "languages": ["zh"], "tags": ["male"]} in entries

    vp.remove_voice("voice-a")
    assert [v["reference_id"] for v in vp.load_pool()] == ["voice-b"]


def test_pick_from_pool_prefers_language_and_gender(pool_root):
    vp.add_voice("v-zh-f", gender="female", language="zh")
    vp.add_voice("v-en-m", gender="male", language="en")
    vp.add_voice("v-en-f", gender="female", language="en")
    assert vp.pick_from_pool("zh", "female") == "v-zh-f"
    assert vp.pick_from_pool("en", "female") == "v-en-f"
    # 已占用 → 换下一个
    picked = vp.pick_from_pool("en", "female", used={"v-en-f"})
    assert picked and picked != "v-en-f"
    # 池子空
    vp.remove_voice("v-zh-f")
    vp.remove_voice("v-en-m")
    vp.remove_voice("v-en-f")
    assert vp.pick_from_pool("en", "female") == ""


@pytest.fixture()
def client(pool_root, monkeypatch):
    monkeypatch.delenv("SE_ADMIN_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(admin_router)
    app.state.v2 = SimpleNamespace(db=None)
    with TestClient(app) as http:
        yield http


def test_admin_pool_endpoints(client, monkeypatch):
    async def fake_preview(reference_id, *, refresh=False):
        return b"ID3" + b"\x00" * 1024, "audio/mpeg"

    async def fake_models(*, refresh=False, max_pages=10):
        return [{"id": "fm-1", "title": "Fish Voice", "languages": ["en"], "tags": ["female"]}]

    monkeypatch.setattr(vp, "preview_audio", fake_preview)
    monkeypatch.setattr(vp, "list_all_models", fake_models)

    # 检测
    res = client.post("/api/admin/voice-pool/validate", json={"reference_id": "voice-a"}).json()
    assert res["ok"] is True and res["bytes"] > 1000

    # 加入
    res = client.post(
        "/api/admin/voice-pool/add",
        json={"reference_id": "voice-a", "name": "Alice", "gender": "female", "language": "en"},
    ).json()
    assert res["ok"] is True
    status = client.get("/api/admin/voice-pool").json()
    assert len(status["voices"]) == 1
    assert status["voices"][0]["reference_id"] == "voice-a"

    # 试听返回音频
    audio = client.post("/api/admin/voice-pool/preview", json={"reference_id": "voice-a"})
    assert audio.status_code == 200
    assert audio.headers["content-type"].startswith("audio/mpeg")
    assert len(audio.content) > 100

    # 全部音色
    models = client.get("/api/admin/voice-pool/models").json()
    assert models["ok"] is True and models["count"] == 1

    # 移除
    res = client.post("/api/admin/voice-pool/remove", json={"reference_id": "voice-a"}).json()
    assert res["ok"] is True
    assert client.get("/api/admin/voice-pool").json()["voices"] == []


def test_add_rejected_when_validation_fails(client, monkeypatch):
    async def boom(reference_id, *, refresh=False):
        raise RuntimeError("Fish 返回 404")

    monkeypatch.setattr(vp, "preview_audio", boom)
    res = client.post("/api/admin/voice-pool/add", json={"reference_id": "bad-voice"}).json()
    assert res["ok"] is False
    assert "检测失败" in res["issues"][0]
    assert vp.load_pool() == []


def test_preview_error_maps_502(client, monkeypatch):
    async def boom(reference_id, *, refresh=False):
        raise RuntimeError("synthesis failed")

    monkeypatch.setattr(vp, "preview_audio", boom)
    res = client.post("/api/admin/voice-pool/preview", json={"reference_id": "x"})
    assert res.status_code == 502


def test_voice_pool_requires_token(client, monkeypatch):
    monkeypatch.setenv("SE_ADMIN_TOKEN", "secret-xyz")
    assert client.get("/api/admin/voice-pool").status_code == 403
    ok = client.get("/api/admin/voice-pool", headers={"X-Admin-Token": "secret-xyz"})
    assert ok.status_code == 200


def test_resolve_by_id_name_and_substring(pool_root):
    vp.add_voice("v-1", name="女大学生", gender="female", language="zh")
    vp.add_voice("v-2", name="Russian Girl", gender="female", language="en")
    assert vp.resolve("v-1")["reference_id"] == "v-1"
    assert vp.resolve("女大学生")["reference_id"] == "v-1"
    assert vp.resolve("russian girl")["reference_id"] == "v-2"
    assert vp.resolve("russian")["reference_id"] == "v-2"
    assert vp.resolve("不存在") is None
    assert vp.resolve_id("女大学生") == "v-1"
    assert vp.resolve_id("some-raw-id") == "some-raw-id"
