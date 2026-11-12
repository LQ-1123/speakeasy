"""好友语音异步补全：先返回文字消息，后台再把 audio_path 补上。"""

from __future__ import annotations

import asyncio

import pytest

from app.v2.chat_service import ChatService


class FakeDB:
    def __init__(self) -> None:
        self.added: list[dict] = []
        self.updates: list[dict] = []
        self.touched: list[str] = []

    def add_message(self, friend_id: str, sender: str, text: str, **kwargs) -> dict:
        row = {
            "id": "m1",
            "friend_id": friend_id,
            "sender": sender,
            "transcript": text,
            **kwargs,
        }
        self.added.append(row)
        return dict(row)

    def touch_friend(self, friend_id: str) -> None:
        self.touched.append(friend_id)

    def update_message(self, message_id: str, **fields) -> None:
        self.updates.append({"id": message_id, **fields})


@pytest.mark.asyncio
async def test_deferred_voice_returns_text_then_updates_audio(monkeypatch) -> None:
    monkeypatch.setenv("SE_TTS_DEFER_AUDIO", "1")
    db = FakeDB()
    service = object.__new__(ChatService)
    service.db = db
    service._tasks = set()

    async def fake_synth(friend, text, tts_text=None):
        await asyncio.sleep(0.05)
        return "fish_test.mp3", 3200

    service._synth_friend_audio = fake_synth  # type: ignore[method-assign]

    msg = await service.store_friend_voice({"id": "f1"}, "你好呀", "你好呀")

    assert msg["msg_type"] == "text"
    assert msg["audio_path"] is None
    assert db.added and db.added[0]["msg_type"] == "text"

    for _ in range(50):
        if db.updates:
            break
        await asyncio.sleep(0.01)

    assert db.updates == [
        {
            "id": "m1",
            "msg_type": "voice",
            "audio_path": "fish_test.mp3",
            "duration_ms": 3200,
        }
    ]
