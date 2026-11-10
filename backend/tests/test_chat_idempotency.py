"""聊天发送幂等：同一个 client_message_id 重复提交只落一条用户消息、一条回复。

覆盖用户真机问题：发消息后对方没回时退出聊天页/重试，旧实现会重复发送并重复落库。
"""

from __future__ import annotations

import math
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.v2.audio_store import AudioStore
from app.v2.chat_service import ChatService
from app.v2.db import DB
from app.v2.media_store import MediaStore
from app.v2.memory import MemoryManager
from app.v2.router import router as v2_router
from app.v2.services import V2Services
from app.v2.spawner import PersonaSpawner
from app.voice.audio_utils import write_wav
from app.voice.stt_config import STTConfigStore


class _FakeLLM:
    uses_mock = True

    def health(self) -> dict:
        return {"configured": False, "model": "mock", "mock": True}

    async def stream_chat(self, messages, **kwargs):
        for token in "Sounds good. What have you been up to?".split():
            yield token + " "

    async def complete_json(self, *args, **kwargs):  # pragma: no cover
        return "{}"


class _FakeVoice:
    async def synth_to_wav(self, text, voice_id, *, rate="+0%", pitch="+0Hz", out_dir):
        path = Path(out_dir) / f"fake_{uuid.uuid4().hex}.wav"
        samples = [0.05 * math.sin(2 * math.pi * 220 * i / 24000) for i in range(2400)]
        write_wav(str(path), samples, 24000)
        return str(path), 200


def _client(tmp_path):
    db = DB(str(tmp_path / "idempotent.db"))
    db.init()
    audio = AudioStore(str(tmp_path))
    media = MediaStore(str(tmp_path))
    engines = SimpleNamespace(llm=_FakeLLM(), settings=SimpleNamespace(data_path=""), stt=None)
    memory = MemoryManager(db, engines.llm)
    voice = _FakeVoice()
    spawner = PersonaSpawner(db, engines.llm)
    chat = ChatService(engines, db, memory, voice, spawner, audio, media, memory_idle_s=0)
    services = V2Services(
        db=db,
        engines=engines,
        audio=audio,
        memory=memory,
        voice=voice,
        spawner=spawner,
        chat=chat,
        data_dir=str(tmp_path),
        media=media,
    )
    engines.stt_config = STTConfigStore(str(tmp_path))
    app = FastAPI()
    app.include_router(v2_router)
    app.state.v2 = services
    return TestClient(app), services, db


def _user_messages(db, friend_id, client_message_id):
    with db._lock:  # noqa: SLF001 - 只读断言
        rows = db.conn.execute(
            "SELECT * FROM messages WHERE friend_id=? AND sender='user' AND client_message_id=?",
            (friend_id, client_message_id),
        ).fetchall()
    return [dict(r) for r in rows]


def test_db_unique_client_message_id(tmp_path):
    db = DB(str(tmp_path / "unique.db"))
    db.init()
    friend = db.create_friend({"name": "Friend"})
    first = db.add_message(friend["id"], "user", "hello", client_message_id="client-abc-123")
    second = db.add_message(friend["id"], "user", "hello", client_message_id="client-abc-123")
    assert first["id"] == second["id"]
    assert len(_user_messages(db, friend["id"], "client-abc-123")) == 1
    db.add_message(friend["id"], "user", "hello", client_message_id="client-abc-456")
    assert len(_user_messages(db, friend["id"], "client-abc-456")) == 1
    # 不带幂等键的历史行为不变：每次都落新消息
    db.add_message(friend["id"], "user", "no key")
    db.add_message(friend["id"], "user", "no key")
    with db._lock:
        count = db.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE friend_id=? AND sender='user' AND client_message_id IS NULL",
            (friend["id"],),
        ).fetchone()[0]
    assert count == 2
    db.close()


def test_repeated_text_send_is_idempotent(tmp_path):
    client, services, db = _client(tmp_path)
    with client:
        friend = db.create_friend({"name": "Niamh", "nationality": "Ireland"})
        body = {"text": "Hello", "client_message_id": "client-msg-001"}
        first = client.post(f"/api/v2/chat/{friend['id']}/text", json=body)
        assert first.status_code == 200, first.text
        second = client.post(f"/api/v2/chat/{friend['id']}/text", json=body)
        assert second.status_code == 200, second.text
        rows = _user_messages(db, friend["id"], "client-msg-001")
        assert len(rows) == 1
        assert rows[0]["transcript"] == "Hello"
        # 第一条请求已经产生了回复；重复提交不再产生第二条回复
        with db._lock:
            replies = db.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE friend_id=? AND sender='friend'",
                (friend["id"],),
            ).fetchone()[0]
        assert replies == 1
        assert first.json()["user_message"]["client_message_id"] == "client-msg-001"
        assert second.json()["user_message"]["id"] == first.json()["user_message"]["id"]
        db.close()


def test_text_send_without_key_still_allows_multiple_messages(tmp_path):
    client, services, db = _client(tmp_path)
    with client:
        friend = db.create_friend({"name": "Amelia", "nationality": "United Kingdom"})
        for text in ("one", "two"):
            resp = client.post(f"/api/v2/chat/{friend['id']}/text", json={"text": text})
            assert resp.status_code == 200, resp.text
        with db._lock:
            count = db.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE friend_id=? AND sender='user'",
                (friend["id"],),
            ).fetchone()[0]
        assert count == 2
        db.close()


def test_invalid_client_message_id_is_rejected(tmp_path):
    client, services, db = _client(tmp_path)
    with client:
        friend = db.create_friend({"name": "Callum"})
        too_short = client.post(
            f"/api/v2/chat/{friend['id']}/text",
            json={"text": "hi", "client_message_id": "short"},
        )
        assert too_short.status_code == 422
        bad_chars = client.post(
            f"/api/v2/chat/{friend['id']}/text",
            json={"text": "hi", "client_message_id": "client msg with spaces"},
        )
        assert bad_chars.status_code == 422
        db.close()
