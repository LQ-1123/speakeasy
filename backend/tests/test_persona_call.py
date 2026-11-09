"""第二阶段验收：语音通话 Persona 化 + 只读观测端点。

覆盖：
- 通话 prompt 含 Speech DNA / 本轮行为指令 / 此刻生活，且长度目标随 verbosity 变化；
- 通话计划**不注入示例对话**（保 TTFT），文字计划会注入；
- 不同人物同一句话的通话层明显不同；
- 通话回合结束后行为历史与情绪被写回；
- `/api/v3/stats/persona` 只读、字段齐全、**不触发任何 LLM**。

全部使用 mock/fake，不产生真实 API 费用；真实通话表现由本地 Web 验收覆盖。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.v2.chat_service import ChatService
from app.v2.db import DB
from app.v2.memory import MemoryManager
from app.v3 import persona_context
from app.v3.router import router as v3_router
from app.v3.schema import ensure_schema
from app.v3.service import V3Service
from app.v3.speech_dna import ensure_persona_dna


class _RaisingLLM:
    """任何调用都爆炸：证明这条链路不碰模型。"""

    uses_mock = False

    async def complete_json(self, *a, **kw):
        raise AssertionError("这条链路不应该调用 LLM")

    async def stream_chat(self, *a, **kw):  # pragma: no cover
        raise AssertionError("这条链路不应该调用 LLM")
        yield ""


class _MockLLM:
    uses_mock = True

    async def complete_json(self, *a, **kw):  # pragma: no cover
        raise AssertionError("mock 模式不应调用 complete_json")

    async def stream_chat(self, *a, **kw):  # pragma: no cover
        raise AssertionError("mock 模式不应调用 stream_chat")
        yield ""


@pytest.fixture()
def db(tmp_path):
    database = DB(str(tmp_path / "call.db"))
    database.init()
    ensure_schema(database, seed=True)
    asyncio.run(ensure_persona_dna(database, None, use_llm=False))
    yield database
    database.close()


def _persona(db: DB, nickname: str) -> dict:
    row = db.conn.execute("SELECT * FROM personas WHERE nickname=?", (nickname,)).fetchone()
    assert row is not None, nickname
    return dict(row)


def _friend(db: DB, nickname: str) -> dict:
    persona = _persona(db, nickname)
    return db.create_friend(
        {
            "name": persona["nickname"],
            "persona_id": persona["id"],
            "city": persona.get("city") or "",
            "nationality": persona.get("country") or "",
            "native_lang": persona.get("language") or "en",
            "reply_language": persona.get("language") or "en",
            "personality_prompt": (persona.get("personality") or "")[:400],
            "speech_quirks": ["right"],
            "closeness_level": 3,
            "total_turns": 20,
        }
    )


def _chat(db: DB, llm) -> ChatService:
    return ChatService(
        engines=SimpleNamespace(llm=llm, stt=None),
        db=db,
        memory=MemoryManager(db, llm),
        voice=None,
        spawner=None,
        audio_store=None,
    )


def test_call_prompt_contains_dna_and_turn_directive(db):
    friend = _friend(db, "Adrian")  # blunt / dry / one_line / low initiative
    chat = _chat(db, _MockLLM())
    plan = chat._call_turn_plan(friend, "hey, how was your day?")
    assert plan is not None
    prompt = chat._call_system_prompt(friend, plan)

    assert "[Speech DNA on the phone]" in prompt
    assert "[This turn — you are SPEAKING on a live call" in prompt
    assert "ONE short spoken line" in prompt                # verbosity=one_line → 12 词目标
    assert "Do not end with a question" in prompt or "may end with a question" in prompt
    # 通话 prompt 里不应再出现旧的「统一 18 词」硬规则
    assert "max ~18 words" not in prompt


def test_call_length_target_follows_verbosity(db):
    short_friend = _friend(db, "Adrian")   # one_line
    long_friend = _friend(db, "Camila")    # long
    chat = _chat(db, _MockLLM())

    short_prompt = chat._call_system_prompt(
        short_friend, chat._call_turn_plan(short_friend, "tell me about your day")
    )
    long_prompt = chat._call_system_prompt(
        long_friend, chat._call_turn_plan(long_friend, "tell me about your day")
    )

    assert "max ~12 words" in short_prompt
    assert "max ~40 words" in long_prompt
    assert "max ~12 words" not in long_prompt


def test_call_plan_skips_examples_but_text_plan_has_them(db):
    friend = _friend(db, "Adrian")
    call_plan = persona_context.build_call_plan(db, friend, "what's up?")
    text_plan = persona_context.build_turn_plan(db, friend, "what's up?")

    assert call_plan.examples == []
    assert len(text_plan.examples) > 0


def test_call_layer_is_bounded_and_persona_specific(db):
    chat = _chat(db, _MockLLM())
    layers = {}
    for nickname in ("Adrian", "Camila", "Jiwoo"):
        friend = _friend(db, nickname)
        plan = chat._call_turn_plan(friend, "hey there")
        layer = persona_context.build_call_layer(
            db, friend, plan, language=plan.language
        )
        assert len(layer) <= persona_context.MAX_CALL_LAYER_CHARS, (nickname, len(layer))
        layers[nickname] = layer

    assert layers["Adrian"] != layers["Camila"]
    # 韩语人物的通话层必须有韩语示例不留、但 DNA/行为指令仍在
    assert "[Speech DNA on the phone]" in layers["Jiwoo"]


def test_call_turn_records_behavior_and_emotion(db):
    friend = _friend(db, "Callum")
    chat = _chat(db, _MockLLM())
    plan = chat._call_turn_plan(friend, "I had a terrible day")
    assert plan is not None

    before = json.loads(db.get_friend(friend["id"])["recent_behaviors"] or "[]")
    bean = _persona(db, "Callum").get("emotion_state") or "{}"

    chat._after_reply_hooks(friend, plan, "I had a terrible day")

    after = json.loads(db.get_friend(friend["id"])["recent_behaviors"] or "[]")
    assert len(after) == len(before) + 1
    assert after[-1] == plan.behavior

    emo = db.conn.execute(
        "SELECT emotion_state FROM personas WHERE id=?", (plan.persona_id,)
    ).fetchone()["emotion_state"]
    assert emo not in ("", "{}", bean)


def test_call_prompt_building_never_calls_llm(db):
    friend = _friend(db, "Morten")
    chat = _chat(db, _RaisingLLM())
    # 只构建 prompt：不应该触达 LLM
    plan = chat._call_turn_plan(friend, "hi")
    prompt = chat._call_system_prompt(friend, plan)
    assert "LIVE voice call" in prompt


# --------------------------------------------------------------------------- #
# 观测端点
# --------------------------------------------------------------------------- #
def test_stats_persona_endpoint_is_read_only_and_never_calls_llm(db):
    service = V3Service(db, SimpleNamespace(llm=_RaisingLLM()), "")
    services = SimpleNamespace(
        db=db,
        onboarding_pending=False,
        followup_inflight=set(),
        spawn_bg=lambda coro: (coro.close(), None)[1],
    )
    app = FastAPI()
    app.state.v2 = services
    app.state.v3 = service
    app.include_router(v3_router)

    with TestClient(app) as client:
        response = client.get("/api/v3/stats/persona")
        assert response.status_code == 200, response.text
        data = response.json()

    for key in ("personas", "events", "posts", "comments", "relationships", "emotion", "background"):
        assert key in data, key
    assert data["personas"]["total"] >= 12
    assert data["personas"]["with_speech_dna"] >= 12
    assert data["personas"]["with_examples"] >= 12
    assert isinstance(data["events"]["by_type"], dict)
    assert "event_posts_per_day" in data["background"]
    # 端点里不能出现用户数据字段
    flattened = json.dumps(data, ensure_ascii=False)
    assert "user_id" not in flattened
