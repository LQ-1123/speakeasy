"""Phase B：图片上传 → 多模态 LLM 讨论 + 媒体存储（全离线）。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.llm.client import ChatMessage  # noqa: E402
from app.v2.chat_service import ChatService  # noqa: E402
from app.v2.db import DB  # noqa: E402
from app.v2.media_store import MediaStore  # noqa: E402

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


class FakeMemory:
    def build_context(self, friend, **kwargs):
        return ""


class FakeVoice:
    async def synth_to_wav(self, text, voice_id, rate="+0%", pitch="+0Hz", out_dir="."):
        path = os.path.join(out_dir, "fake_reply.wav")
        with open(path, "wb") as fh:
            fh.write(b"RIFF....WAVE")
        return path, 800


class VisionLLM:
    uses_mock = False

    def __init__(self) -> None:
        self.calls: list = []

    async def stream_chat(self, messages, **kwargs):
        self.calls.append(messages)
        for token in ("That photo looks great! ", "Is that a cat on the sofa?"):
            yield token


class MockLLM:
    uses_mock = True

    async def stream_chat(self, messages, **kwargs):  # pragma: no cover
        raise AssertionError("mock 不应调度 LLM")


@pytest.fixture()
def ctx(tmp_path):
    db = DB(str(tmp_path / "v2.db"))
    db.init()
    friend = db.create_friend(
        {
            "name": "Tom",
            "nationality": "United Kingdom",
            "city": "Bristol",
            "timezone": "UTC",
            "native_lang": "en",
            "reply_language": "en",
            "voice_id": "en-GB-RyanNeural",
            "archetype_id": "en_test",
            "personality_prompt": "You are Tom.",
        }
    )
    media = MediaStore(str(tmp_path))
    llm = VisionLLM()
    chat = ChatService(
        engines=SimpleNamespace(llm=llm, stt=None),
        db=db,
        memory=FakeMemory(),
        voice=FakeVoice(),
        spawner=None,
        audio_store=None,
        media_store=media,
        memory_idle_s=100000,
    )
    yield chat, db, media, friend, llm
    asyncio.run(chat.aclose())
    db.close()


def test_media_store_saves_and_validates(tmp_path):
    store = MediaStore(str(tmp_path))
    name = store.save_image(PNG_BYTES, "image/png", "cat.png")
    assert name.endswith(".png")
    assert os.path.isfile(store.path_for(name))
    assert store.mime_for(name) == "image/png"
    assert store.url_for(name) == f"/api/v2/media/{name}"

    with pytest.raises(ValueError):
        store.save_image(b"not an image", "text/plain", "x.txt")


@pytest.mark.asyncio
async def test_image_turn_uses_multimodal_message(ctx):
    chat, db, media, friend, llm = ctx
    result = await chat.handle_image(
        friend["id"], PNG_BYTES, "cat.png", "image/png", "看我的猫"
    )
    user_msg = result["user_message"]
    assert user_msg["msg_type"] == "image"
    assert user_msg["image_path"].endswith(".png")
    assert user_msg["transcript"] == "看我的猫"
    assert result["friend_messages"][0]["transcript"].startswith("That photo")
    assert result["reply_language"] == "en"

    # 最后一条用户消息必须是多模态 parts（带 data URL）
    sent = llm.calls[0]
    multimodal = [m for m in sent if isinstance(m, ChatMessage) and m.images]
    assert multimodal, "应把图片作为多模态消息传给 LLM"
    assert multimodal[0].images[0].startswith("data:image/png;base64,")
    assert "看我的猫" in multimodal[0].content

    # 文件确实落盘，消息里可拿到 image_url
    stored = db.recent_messages(friend["id"], 5)
    image_rows = [m for m in stored if m.get("image_path")]
    assert image_rows and os.path.isfile(media.path_for(image_rows[0]["image_path"]))


@pytest.mark.asyncio
async def test_image_turn_mock_falls_back_in_reply_language(ctx):
    chat, db, media, friend, _ = ctx
    chat.engines = SimpleNamespace(llm=MockLLM(), stt=None)
    result = await chat.handle_image(friend["id"], PNG_BYTES, "x.png", "image/png", "")
    reply = result["friend_messages"][0]["transcript"]
    assert "photo" in reply.lower() or "foto" in reply.lower()
    # 空 caption 时也有占位文本
    assert result["user_message"]["transcript"] == "（发来一张图片）"
