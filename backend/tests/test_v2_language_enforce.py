"""好友回复语言纠偏：中文好友突然回英文时必须纠回来（离线）。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.v2 import chat_service as cs  # noqa: E402


class FakeMemory:
    def build_context(self, friend, **_kwargs):
        return ""


class SeqLLM:
    """按顺序吐出预设回复的假 LLM（用于模拟「先说英文、再被纠正」）。"""

    uses_mock = False

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    async def stream_chat(self, messages, **kwargs):
        self.calls.append(messages)
        text = self.replies.pop(0) if self.replies else ""
        for ch in text:
            yield ch


def make_chat(replies: list[str]) -> tuple[cs.ChatService, SeqLLM]:
    llm = SeqLLM(replies)
    chat = cs.ChatService(
        engines=SimpleNamespace(llm=llm, stt=None),
        db=None,
        memory=FakeMemory(),
        voice=None,
        spawner=None,
        audio_store=None,
        memory_idle_s=0,
    )
    return chat, llm


def zhang_friend() -> dict:
    return {
        "id": "f-zh",
        "name": "Yilin",
        "city": "Nanjing",
        "nationality": "China",
        "native_lang": "zh",
        "reply_language": "zh",
        "speech_quirks": ["哎呀", "就是说嘛"],
        "personality_prompt": (
            "You are Yilin. You always talk to the learner in natural Chinese (中文). "
            "Never switch to English (or Chinese), even if the learner writes in English or Chinese."
        ),
    }


# --------------------------------------------------------------------------- #
# 判定工具
# --------------------------------------------------------------------------- #
def test_matches_language_scripts():
    assert cs._matches_language("哎呀，今天过得怎么样？", "zh")
    assert not cs._matches_language("哎呀, I'm curious — what are you up to later today?", "zh")
    assert not cs._matches_language("Allora, morning coffee?", "zh")
    assert cs._matches_language("こんにちは、元気ですか？", "ja")
    assert not cs._matches_language("Hello there", "ja")
    assert cs._matches_language("안녕하세요, 뭐 해요?", "ko")
    assert cs._matches_language("Right then, one flat white coming up.", "en")
    assert not cs._matches_language("哎呀，今天天气不错。", "en")
    assert cs._matches_language("Ciao, come stai?", "it")  # 拉丁语系内部不做强判定


def test_sanitize_old_persona_prompt():
    fixed = cs._sanitize_persona_language(
        "Never switch to English (or Chinese), even if the learner writes in English or Chinese."
    )
    assert "Never switch to English or any other language" in fixed
    assert "(or Chinese)" not in fixed


# --------------------------------------------------------------------------- #
# 纠偏链路
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_chinese_friend_english_reply_is_rewritten_to_chinese():
    chat, llm = make_chat([
        "哎呀, I'm curious — what are you up to later today?",  # 第 1 次：跑偏成英文
        "哎呀，今天有点忙，你晚上打算做什么？",                    # 第 2 次：纠偏成功
    ])
    reply = await chat._llm_reply(zhang_friend(), "我一般做爱", [], [])
    assert cs._matches_language(reply, "zh"), reply
    assert len(llm.calls) == 2
    rewrite_prompt = llm.calls[1][0]["content"]
    assert "Chinese" in rewrite_prompt
    # 系统 prompt 里旧的矛盾句已被清洗
    assert "(or Chinese)" not in llm.calls[0][0]["content"]


@pytest.mark.asyncio
async def test_rewrite_failure_falls_back_to_chinese_template():
    chat, llm = make_chat([
        "哎呀, I'm curious — what are you up to later today?",
        "Still English again, sorry.",  # 纠偏仍然失败
    ])
    reply = await chat._llm_reply(zhang_friend(), "我一般做爱", [], [])
    assert cs._matches_language(reply, "zh"), reply
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_english_friend_reply_not_rewritten():
    friend = {
        "id": "f-en",
        "name": "Tom",
        "city": "Bristol",
        "nationality": "United Kingdom",
        "native_lang": "en",
        "reply_language": "en",
        "speech_quirks": ["right"],
        "personality_prompt": "You are Tom from Bristol.",
    }
    chat, llm = make_chat(["Right then, one flat white coming up. Fancy one?"])
    reply = await chat._llm_reply(friend, "Hi", [], [])
    assert "flat white" in reply
    assert len(llm.calls) == 1
