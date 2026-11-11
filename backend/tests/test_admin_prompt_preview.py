"""管理端 Prompt 调试接口验收：版本 / 预览 / 模块覆盖 / 守卫。

本机未配置口令时 `_guard` 直通（TestClient 的 client.host 是 "testclient"，
在 _LOOPBACK 白名单里）；配置口令后必须带 token，普通请求 403。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin.router import router as admin_router
from app.v2.db import DB
from app.v3 import prompt_composer as pc
from app.v3.schema import ensure_schema
from app.v3.speech_dna import ensure_persona_dna


@pytest.fixture()
def db(tmp_path):
    database = DB(str(tmp_path / "admin_prompt.db"))
    database.init()
    ensure_schema(database, seed=True)
    asyncio.run(ensure_persona_dna(database, None, use_llm=False))
    yield database
    database.close()


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.delenv("SE_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("SE_ADMIN_PASSWORD", raising=False)
    app = FastAPI()
    app.include_router(admin_router)
    app.state.v2 = SimpleNamespace(db=db)
    app.state.v3 = None
    app.state.conversation_store = None
    with TestClient(app) as http:
        yield http


def _persona_id(db: DB, nickname: str = "Adrian") -> str:
    row = db.conn.execute("SELECT id FROM personas WHERE nickname=?", (nickname,)).fetchone()
    assert row is not None, nickname
    return str(row["id"])


# --------------------------------------------------------------------------- #
# 版本
# --------------------------------------------------------------------------- #
def test_prompt_versions(client):
    response = client.get("/api/admin/prompt/versions")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["current"] == pc.PROMPT_VERSION
    versions = data["versions"]
    assert isinstance(versions, list) and len(versions) >= 2
    assert versions[0]["version"] == pc.PROMPT_VERSION
    assert any(item.get("status") == "history" for item in versions)


# --------------------------------------------------------------------------- #
# GET 预览
# --------------------------------------------------------------------------- #
def test_prompt_preview_structure(client, db):
    pid = _persona_id(db)
    response = client.get(
        "/api/admin/prompt/preview",
        params={"persona_id": pid, "user_id": "debug-user", "text": "hey, how was your day?", "mode": "text"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    for key in ("version", "mode", "language", "chars", "modules", "full_text", "plan"):
        assert key in data, key
    assert data["version"] == pc.PROMPT_VERSION
    assert data["mode"] == "text"
    assert data["chars"] == len(data["full_text"])
    module_keys = [module["key"] for module in data["modules"]]
    assert module_keys == [key for key, *_ in pc.TEXT_MODULE_SPEC]
    for module in data["modules"]:
        for field in ("key", "layer", "title", "chars", "source", "enabled", "text"):
            assert field in module, field
    assert data["plan"]["behavior"]
    assert "scene" in data["plan"]
    # 角色卡必须在场（不是空 payload）
    assert "Adrian" in data["full_text"]


def test_prompt_preview_call_mode(client, db):
    pid = _persona_id(db)
    response = client.get(
        "/api/admin/prompt/preview",
        params={"persona_id": pid, "mode": "call", "text": "hey"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["mode"] == "call"
    assert len(data["full_text"]) <= pc.MAX_CALL_CHARS
    assert "examples" not in [module["key"] for module in data["modules"]]


# --------------------------------------------------------------------------- #
# 参数校验
# --------------------------------------------------------------------------- #
def test_prompt_preview_unknown_persona_404(client):
    response = client.get(
        "/api/admin/prompt/preview", params={"persona_id": "p_does_not_exist"}
    )
    assert response.status_code == 404


def test_prompt_preview_bad_arguments_400(client, db):
    pid = _persona_id(db)
    assert client.get("/api/admin/prompt/preview", params={"mode": "text"}).status_code == 400
    assert (
        client.get(
            "/api/admin/prompt/preview", params={"persona_id": pid, "mode": "telepathy"}
        ).status_code
        == 400
    )


# --------------------------------------------------------------------------- #
# POST 预览 + module_overrides
# --------------------------------------------------------------------------- #
def test_prompt_preview_post_with_overrides(client, db):
    pid = _persona_id(db)
    response = client.post(
        "/api/admin/prompt/preview",
        json={
            "persona_id": pid,
            "user_id": "debug-user",
            "text": "tell me about your day",
            "mode": "text",
            "module_overrides": {"global": "[DEBUG-OVERRIDE] stay in character."},
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    global_module = next(m for m in data["modules"] if m["key"] == "global")
    assert global_module["text"] == "[DEBUG-OVERRIDE] stay in character."
    assert "[DEBUG-OVERRIDE]" in data["full_text"]
    # 调试接口不落库：再次 GET 预览不应看到覆盖内容
    fresh = client.get(
        "/api/admin/prompt/preview", params={"persona_id": pid}
    ).json()
    assert "[DEBUG-OVERRIDE]" not in fresh["full_text"]
    # 未知模块 key 被忽略（不新增模块、不破坏 A–H 顺序）
    unknown = client.post(
        "/api/admin/prompt/preview",
        json={"persona_id": pid, "module_overrides": {"no_such_module": "x"}},
    )
    assert unknown.status_code == 200
    assert [m["key"] for m in unknown.json()["modules"]] == [key for key, *_ in pc.TEXT_MODULE_SPEC]


def test_prompt_preview_post_bad_overrides_400(client, db):
    pid = _persona_id(db)
    bad = client.post(
        "/api/admin/prompt/preview",
        json={"persona_id": pid, "module_overrides": ["not", "a", "dict"]},
    )
    assert bad.status_code == 400


# --------------------------------------------------------------------------- #
# 守卫：普通用户不得访问
# --------------------------------------------------------------------------- #
def test_prompt_endpoints_require_admin_when_configured(db, monkeypatch):
    monkeypatch.setenv("SE_ADMIN_TOKEN", "secret-xyz")
    monkeypatch.delenv("SE_ADMIN_PASSWORD", raising=False)
    app = FastAPI()
    app.include_router(admin_router)
    app.state.v2 = SimpleNamespace(db=db)
    app.state.v3 = None
    app.state.conversation_store = None
    with TestClient(app) as http:
        pid = _persona_id(db)
        assert http.get("/api/admin/prompt/versions").status_code == 403
        assert http.get("/api/admin/prompt/preview", params={"persona_id": pid}).status_code == 403
        assert (
            http.get(
                "/api/admin/prompt/versions", headers={"X-Admin-Token": "secret-xyz"}
            ).status_code
            == 200
        )
