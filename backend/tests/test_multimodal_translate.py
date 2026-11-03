"""多模态与翻译的单元测试。"""

from __future__ import annotations

import asyncio
import io

from app.llm.client import ChatMessage, _normalize_messages
from app.llm.translate import clear_cache, translate

IMG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="


# --------------------------------------------------------------------------- #
# 多模态
# --------------------------------------------------------------------------- #
def test_chatmessage_text_only_is_plain_string():
    m = ChatMessage(role="user", content="hello")
    payload = m.to_openai()
    assert payload == {"role": "user", "content": "hello"}


def test_chatmessage_with_image_uses_parts():
    m = ChatMessage(role="user", content="what is this?", images=(IMG,))
    payload = m.to_openai()
    assert payload["role"] == "user"
    parts = payload["content"]
    assert isinstance(parts, list)
    assert parts[0] == {"type": "text", "text": "what is this?"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"] == IMG


def test_chatmessage_image_without_text():
    m = ChatMessage(role="user", content="", images=(IMG,))
    parts = m.to_openai()["content"]
    assert len(parts) == 1
    assert parts[0]["type"] == "image_url"


def test_normalize_messages_keeps_text_string():
    out = _normalize_messages([{"role": "user", "content": "hi"}])
    assert out[0]["content"] == "hi"


def test_normalize_messages_expands_images():
    out = _normalize_messages([{"role": "user", "content": "look", "images": [IMG]}])
    assert isinstance(out[0]["content"], list)
    assert out[0]["content"][0] == {"type": "text", "text": "look"}


def test_normalize_messages_passes_through_list_content():
    parts = [{"type": "text", "text": "x"}]
    out = _normalize_messages([{"role": "user", "content": parts}])
    assert out[0]["content"] == parts


def test_to_dict_hides_base64():
    m = ChatMessage(role="user", content="hi", images=(IMG,))
    d = m.to_dict()
    assert d["images"] == 1  # 只报数量，不泄露 base64
    assert "data:image" not in str(d)


# --------------------------------------------------------------------------- #
# 翻译
# --------------------------------------------------------------------------- #
class FakeLLM:
    """假的 LLM：回固定译文，或抛错。"""

    def __init__(self, reply: str = "好嘞，一杯馥芮白马上来。", fail: bool = False):
        self.reply = reply
        self.fail = fail
        self.calls = 0

    async def stream_chat(self, messages, **kwargs):  # noqa: ANN001
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        yield self.reply


def test_translate_basic():
    clear_cache()
    fake = FakeLLM()
    r = asyncio.run(translate("Right then, one flat white coming up.", client=fake))
    assert r["ok"] is True
    assert r["translation"] == "好嘞，一杯馥芮白马上来。"
    assert r["cached"] is False


def test_translate_cache_hit():
    clear_cache()
    fake = FakeLLM()
    asyncio.run(translate("same sentence", client=fake))
    r2 = asyncio.run(translate("same sentence", client=fake))
    assert r2["cached"] is True
    assert fake.calls == 1  # 第二次没再调用模型


def test_translate_empty_input():
    clear_cache()
    fake = FakeLLM()
    r = asyncio.run(translate("   ", client=fake))
    assert r["ok"] is True
    assert r["translation"] == ""
    assert fake.calls == 0


def test_translate_failure_is_soft():
    """翻译失败必须软失败，不能把主对话带崩。"""
    clear_cache()
    fake = FakeLLM(fail=True)
    r = asyncio.run(translate("something", client=fake))
    assert r["ok"] is False
    assert r["translation"] == ""
    assert r["error"]


def test_translate_strips_quotes():
    clear_cache()
    fake = FakeLLM(reply='"这是译文"')
    r = asyncio.run(translate("x y z", client=fake))
    assert r["translation"] == "这是译文"
