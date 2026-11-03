"""V2 聊天内翻译：方向判断 / LLM 调用 / 缓存 / 无 Key / API 端点（全离线）。"""

from __future__ import annotations

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

from app.v2 import translate as tr  # noqa: E402
from app.v2.db import DB  # noqa: E402
from app.v2.router import router as v2_router  # noqa: E402


class FakeLLM:
    """最小 LLM 替身：按 target 返回固定译文，记录调用。"""

    uses_mock = False

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    async def stream_chat(self, messages, **kwargs):
        self.calls.append(messages)
        system = messages[0]["content"]
        if "日本語" in system:
            for token in ("こんにちは、", "元気ですか？"):
                yield token
        elif "简体中文" in system:
            for token in ("你好，", "最近怎么样？"):
                yield token
        else:
            for token in ("translated ", "text"):
                yield token


class MockLLM:
    uses_mock = True

    async def stream_chat(self, messages, **kwargs):  # pragma: no cover - 不会进入
        yield "nope"


class AuthErrorLLM:
    uses_mock = False

    async def stream_chat(self, messages, **kwargs):
        raise RuntimeError("AuthenticationError: 401 invalid key")
        yield ""  # pragma: no cover - 使其成为 async generator


@pytest.fixture(autouse=True)
def _clear_cache():
    tr.clear_cache()
    yield
    tr.clear_cache()


# --------------------------------------------------------------------------- #
# 方向判断
# --------------------------------------------------------------------------- #
def test_resolve_target_auto():
    assert tr.resolve_target("Hello, how are you?") == "zh"
    assert tr.resolve_target("こんにちは、元気ですか？") == "en"  # 无好友时
    friend = {"reply_language": "ja"}
    assert tr.resolve_target("你好，最近怎么样？", friend=friend) == "ja"
    assert tr.resolve_target("你好", friend=friend, target="en") == "en"
    assert tr.resolve_target("Hello", target="ZH-CN") == "zh"
    assert tr.resolve_target("Hello", target="日本語") == "ja"


def test_has_cjk():
    assert tr.has_cjk("你好")
    assert tr.has_cjk("こんにちは")
    assert tr.has_cjk("안녕하세요")
    assert not tr.has_cjk("Hello there")


# --------------------------------------------------------------------------- #
# 翻译核心
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_translate_english_to_chinese_and_cache():
    llm = FakeLLM()
    result = await tr.translate_text(llm, "Hello, how are you?", target="zh")
    assert result["ok"] is True
    assert result["target"] == "zh"
    assert result["target_label"] == "简体中文"
    assert result["translation"] == "你好，最近怎么样？"
    assert result["cached"] is False
    assert len(llm.calls) == 1
    assert "简体中文" in llm.calls[0][0]["content"]
    assert llm.calls[0][1]["content"] == "Hello, how are you?"

    again = await tr.translate_text(llm, "Hello, how are you?", target="zh")
    assert again["cached"] is True
    assert again["translation"] == result["translation"]
    assert len(llm.calls) == 1  # 命中缓存不重复请求


@pytest.mark.asyncio
async def test_translate_chinese_to_friend_language():
    llm = FakeLLM()
    result = await tr.translate_text(llm, "你好，最近怎么样？", target="ja")
    assert result["ok"] is True
    assert result["target"] == "ja"
    assert result["target_label"] == "日本語"
    assert "こんにちは" in result["translation"]


@pytest.mark.asyncio
async def test_translate_without_llm_key_returns_actionable_error():
    result = await tr.translate_text(MockLLM(), "Hello", target="zh")
    assert result["ok"] is False
    assert result["error"] == "llm_not_configured"
    assert "LLM" in result["message"] and "个人设置" in result["message"]
    assert result["translation"] == ""


@pytest.mark.asyncio
async def test_translate_auth_error_is_readable():
    result = await tr.translate_text(AuthErrorLLM(), "Hello", target="zh")
    assert result["ok"] is False
    assert result["error"] == "translate_failed"
    assert "API Key" in result["message"] or "401" in result["message"]


@pytest.mark.asyncio
async def test_translate_empty_text_short_circuits():
    result = await tr.translate_text(FakeLLM(), "   ", target="zh")
    assert result["ok"] is True
    assert result["translation"] == ""


# --------------------------------------------------------------------------- #
# API 端点
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(tmp_path):
    db = DB(str(tmp_path / "v2.db"))
    db.init()
    friend = db.create_friend(
        {
            "name": "Daiki",
            "nationality": "Japan",
            "city": "Tokyo",
            "timezone": "Asia/Tokyo",
            "native_lang": "ja",
            "reply_language": "ja",
            "voice_id": "ja-JP-KeitaNeural",
            "archetype_id": "jp_male_01",
        }
    )
    llm = FakeLLM()
    app = FastAPI()
    app.include_router(v2_router)
    app.state.v2 = SimpleNamespace(db=db, engines=SimpleNamespace(llm=llm))
    with TestClient(app) as c:
        yield c, friend, llm
    db.close()


def test_translate_endpoint_auto_direction(client):
    http, friend, llm = client
    r = http.post("/api/v2/translate", json={"text": "Hello there"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["target"] == "zh"
    assert data["target_label"] == "简体中文"
    assert data["translation"] == "你好，最近怎么样？"

    # 中文 + friend_id → 译成好友语言（日语）
    r = http.post(
        "/api/v2/translate",
        json={"text": "你好，最近怎么样？", "friend_id": friend["id"]},
    )
    data = r.json()
    assert data["ok"] is True
    assert data["target"] == "ja"
    assert data["target_label"] == "日本語"
    assert "こんにちは" in data["translation"]


def test_translate_endpoint_explicit_target_and_errors(client):
    http, _, _ = client
    r = http.post("/api/v2/translate", json={"text": "Hello", "target": "en"})
    assert r.json()["target"] == "en"

    # 模拟无 key（mock LLM）
    http.app.state.v2.engines.llm = MockLLM()
    r = http.post("/api/v2/translate", json={"text": "Hello"})
    data = r.json()
    assert data["ok"] is False
    assert data["error"] == "llm_not_configured"


def test_translate_batch_endpoint_mixed_directions(client):
    http, friend, _ = client
    r = http.post(
        "/api/v2/translate/batch",
        json={"texts": ["Hello there", "你好，最近怎么样？"], "friend_id": friend["id"]},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert len(data["items"]) == 2
    assert data["items"][0]["target"] == "zh"
    assert data["items"][1]["target"] == "ja"
    assert data["items"][0]["translation"] == "你好，最近怎么样？"
    assert "こんにちは" in data["items"][1]["translation"]
