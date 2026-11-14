"""Phase A：用户动态 + AI 好友评论 + 回复 emoji。全离线。"""

from __future__ import annotations

import asyncio
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

from app.v2 import user_moments  # noqa: E402
from app.v2.chat_service import ChatService, _strip_emoji  # noqa: E402
from app.v2.db import DB  # noqa: E402
from app.v2.router import router as v2_router  # noqa: E402
from app.v2.spawner import PersonaSpawner  # noqa: E402


class FakeLLM:
    uses_mock = False

    def __init__(self, comment: str = "Sounds lovely! 😄") -> None:
        self.comment = comment
        self.calls = 0

    async def complete_json(self, messages, **kwargs):
        self.calls += 1
        return '{"comment": "%s"}' % self.comment


class MockLLM:
    uses_mock = True

    async def complete_json(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("mock 不应走 LLM")


class FakeMemory:
    def build_context(self, friend, **kwargs):
        return ""


@pytest.fixture()
def db(tmp_path):
    database = DB(str(tmp_path / "v2.db"))
    database.init()
    yield database
    database.close()


def make_friend(db: DB, name: str, lang: str, country: str) -> dict:
    return db.create_friend(
        {
            "name": name,
            "nationality": country,
            "city": country,
            "timezone": "UTC",
            "native_lang": lang,
            "reply_language": lang,
            "voice_id": "en-GB-RyanNeural",
            "archetype_id": f"{lang}_test",
            "personality_prompt": f"You are {name} from {country}.",
            "status": "active",
        }
    )


@pytest.fixture()
def three_friends(db):
    return [
        make_friend(db, "Yilin", "zh", "China"),
        make_friend(db, "Tom", "en", "United Kingdom"),
        make_friend(db, "Daiki", "ja", "Japan"),
    ]


@pytest.mark.asyncio
async def test_generate_comments_with_llm_and_idempotent(db, three_friends):
    moment = db.add_user_moment("今天终于把项目上线了，晚上吃火锅庆祝 🎉", "🔥 庆祝")
    llm = FakeLLM()
    comments = await user_moments.generate_comments(db, llm, moment, three_friends)
    assert len(comments) == 1  # Wrong-language comments are skipped.
    assert all(c["content"] for c in comments)
    assert {c["friend_id"] for c in comments} == {three_friends[1]["id"]}

    again = await user_moments.generate_comments(db, llm, moment, three_friends)
    assert len(again) == 1
    assert llm.calls == 3  # 幂等：第二次不再调 LLM
    assert len(db.list_moment_comments(moment["id"])) == 1


@pytest.mark.asyncio
async def test_generate_comments_without_model_skips_canned_reactions(db, three_friends):
    moment = db.add_user_moment("周末去爬山了，累但开心", "⛰️ 户外")
    comments = await user_moments.generate_comments(db, MockLLM(), moment, three_friends)
    assert comments == []


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(db, three_friends):
    def spawn_bg(coro):
        # 测试里不跑后台任务；关闭协程避免 "never awaited" 警告
        coro.close()
        return SimpleNamespace()

    llm = FakeLLM()
    app = FastAPI()
    app.include_router(v2_router)
    app.state.v2 = SimpleNamespace(
        db=db,
        engines=SimpleNamespace(llm=llm),
        spawn_bg=spawn_bg,
    )

    with TestClient(app) as c:
        yield c, db, llm


def test_post_user_moment_and_comments(client):
    http, db, llm = client
    r = http.post(
        "/api/v2/moments",
        json={"content": "今天第一次做提拉米苏，居然成功了！", "mood_tag": "🍰 成就感"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    moment = data["moment"]
    assert data["comments_pending"] is True
    assert moment["author_type"] == "user"
    assert moment["author_name"] == "我"
    assert moment["friend"] is None
    assert moment["comments"] == []

    # 评论生成由后台任务负责（服务层测试已覆盖），这里验证接口返回结构与合并 feed
    r = http.get("/api/v2/moments")
    assert r.status_code == 200
    moments = r.json()["moments"]
    user_moments = [m for m in moments if m["author_type"] == "user"]
    assert user_moments and user_moments[0]["id"] == moment["id"]


def test_merged_moments_include_user_moment(db, three_friends):
    moment = db.add_user_moment("测试用户动态")
    db.add_moment_comment(moment["id"], three_friends[0]["id"], "好棒 😄")

    captured = []
    app = FastAPI()
    app.include_router(v2_router)
    app.state.v2 = SimpleNamespace(
        db=db,
        engines=SimpleNamespace(llm=FakeLLM()),
        spawn_bg=lambda coro: captured.append(coro),
    )
    with TestClient(app) as http:
        data = http.get("/api/v2/moments").json()
        row = next(m for m in data["moments"] if m["id"] == moment["id"])
        assert row["author_type"] == "user"
        assert row["author_name"] == "我"
        assert len(row["comments"]) == 1
        assert row["comments"][0]["content"] == "好棒 😄"
        assert row["comments"][0]["friend"]["name"] == "Yilin"


# --------------------------------------------------------------------------- #
# Emoji
# --------------------------------------------------------------------------- #
def test_strip_emoji_for_tts():
    assert _strip_emoji("今天好开心 😂 真的✨!") == "今天好开心 真的!"
    assert _strip_emoji("Love it 😄") == "Love it"
    assert _strip_emoji("") == ""


def test_chat_prompt_allows_one_emoji():
    chat = ChatService(
        engines=SimpleNamespace(llm=FakeLLM(), stt=None),
        db=None,
        memory=FakeMemory(),
        voice=None,
        spawner=None,
        audio_store=None,
    )
    friend = {
        "name": "Tom",
        "city": "Bristol",
        "nationality": "United Kingdom",
        "native_lang": "en",
        "reply_language": "en",
        "personality_prompt": "You are Tom.",
    }
    prompt = chat._system_prompt(friend, [])
    assert "emoji" in prompt.lower()
    assert "Do not use markdown or emojis" not in prompt


def test_spawner_persona_prompt_allows_one_emoji():
    prompt = PersonaSpawner._build_personality_prompt(
        {"accent_label": "British", "locale_info": {"country": "United Kingdom"}},
        {
            "name": "Tom",
            "age": 28,
            "occupation": "barista",
            "city": "Bristol",
            "bio": "Likes coffee.",
            "initial_context": "On the bus.",
            "speech_quirks": ["right"],
            "personality_traits": ["friendly"],
        },
        "en",
    )
    assert "emoji" in prompt.lower()
    assert "No markdown, no emoji" not in prompt
