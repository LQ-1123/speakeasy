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
async def test_deferred_voice_returns_text_then_updates_audio(monkeypatch, tmp_path) -> None:
    from app.v2.db import DB
    monkeypatch.setenv("SE_TTS_DEFER_AUDIO", "1")
    db = DB(str(tmp_path / 'deferred.db'))
    db.init()
    friend = db.create_friend({'name': 'Friend'})
    service = object.__new__(ChatService)
    service.db = db
    service._tasks = set()

    async def fake_synth(friend, text, tts_text=None):
        await asyncio.sleep(0.05)
        return "fish_test.mp3", 3200

    service._synth_friend_audio = fake_synth  # type: ignore[method-assign]

    msg = await service.store_friend_voice(friend, "你好呀", "你好呀")

    # 即使选择延后合成，落库也必须是语音消息：客户端不该先看到文字再变成语音
    assert msg["msg_type"] == "voice"
    assert msg["audio_path"] is None
    assert msg['audio_status'] == 'queued'

    for _ in range(50):
        ready = db.get_message(msg['id'])
        if ready['audio_status'] == 'ready':
            break
        await asyncio.sleep(0.01)

    assert ready['audio_path'] == 'fish_test.mp3'
    assert ready['duration_ms'] == 3200
    assert ready['audio_status'] == 'ready'
    await asyncio.gather(*list(service._tasks))
    db.close()
