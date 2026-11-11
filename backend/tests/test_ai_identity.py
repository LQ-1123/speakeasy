"""AI 身份披露（合规）：人物保留风格，但被问到时必须诚实说明自己是 AI。

对应升级方案：「移除『绝不承认自己是 AI』的提示词要求，保留人物风格，同时明确 AI 身份、
支持随时退出和举报」。本文件锁住四条 prompt 链路的措辞，防止回退：

1. ``prompt_composer`` 文字角色卡 boundary；
2. ``prompt_composer`` 通话角色卡 boundary；
3. ``memory.build_context`` 的 [Rules]；
4. ``ChatService._fallback_system_prompt``（composer 不可用时的兜底）。
"""

from __future__ import annotations

import asyncio

import pytest

from app.v2.chat_service import ChatService
from app.v2.db import DB
from app.v2.memory import MemoryManager
from app.v3 import prompt_composer as pc
from app.v3.schema import ensure_schema
from app.v3.speech_dna import ensure_persona_dna


@pytest.fixture()
def db(tmp_path):
    database = DB(str(tmp_path / "identity.db"))
    database.init()
    ensure_schema(database, seed=True)
    asyncio.run(ensure_persona_dna(database, None, use_llm=False))
    yield database
    database.close()


def _persona_id(db: DB) -> str:
    row = db.conn.execute(
        "SELECT id FROM personas WHERE status='active' ORDER BY id LIMIT 1"
    ).fetchone()
    assert row is not None
    return str(row["id"])


def _assert_honest(text: str) -> None:
    assert "AI companion" in text, text[:400]
    assert "not a real human" in text
    assert "if the user asks" in text.lower() or "if asked" in text.lower()


def test_text_character_card_states_ai_identity(db):
    composed = pc.compose_for_persona(db, _persona_id(db), user_text="are you a real person?")
    _assert_honest(composed.text)
    # 不再出现「绝不能承认是 AI」这类指令
    lowered = composed.text.lower()
    assert "never say you are an ai" not in lowered
    # 「不要自称真人」仍然保留（与诚实披露兼容）
    assert "never claim to be a real human" in lowered or "not a real human" in lowered


def test_call_character_card_states_ai_identity(db):
    composed = pc.compose_for_persona(db, _persona_id(db), mode="call")
    card = composed.module("character_card")
    assert card is not None
    assert "AI companion" in card.text
    assert "if the user asks" in card.text.lower()


def test_memory_rules_state_ai_identity(db):
    friend = db.create_friend({"name": "Mia"})
    context = MemoryManager(db, None).build_context(friend)
    assert "AI companion, not a real human" in context
    assert "never say you are an AI" not in context
    assert "never claim to be a real person" in context


def test_fallback_system_prompt_states_ai_identity():
    prompt = ChatService._fallback_system_prompt(
        None, {"name": "Adrian", "reply_language": "en"}
    )
    assert "AI companion, not a real human" in prompt
    assert "never say you are an AI" not in prompt.lower()
    assert "answer honestly" in prompt
