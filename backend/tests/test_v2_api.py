"""v2 REST API 集成测试（离线：mock STT / 规则 LLM / Fake voice）。"""

from __future__ import annotations

import io
import math
import struct
import sys
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.v2.audio_store import AudioStore  # noqa: E402
from app.v2.chat_service import ChatService  # noqa: E402
from app.v2.db import DB  # noqa: E402
from app.v2.memory import MemoryManager  # noqa: E402
from app.v2.router import router as v2_router  # noqa: E402
from app.v2.services import V2Services  # noqa: E402
from app.v2.spawner import PersonaSpawner  # noqa: E402
from app.voice.audio_utils import write_wav  # noqa: E402

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #
class FakeSTT:
    backend_name = "mock"

    async def transcribe(self, pcm16: bytes, sample_rate: int = 16000):
        return SimpleNamespace(text="Hi, I really like indie games and coffee.", language="en",
                               latency_ms=1.0, backend="mock")


class FakeLLM:
    uses_mock = True

    def health(self) -> dict:
        return {"configured": False, "model": "mock", "mock": True}

    async def stream_chat(self, messages, **kwargs):
        for token in "Oh nice! What kind of games are you into these days?".split():
            yield token + " "

    async def complete_json(self, *args, **kwargs):  # pragma: no cover - uses_mock 时不会走到
        return "{}"


class FakeVoice:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    async def synth_to_wav(self, text, voice_id, *, rate="+0%", pitch="+0Hz", out_dir):
        import uuid

        self.calls.append((text, voice_id, rate, pitch))
        path = Path(out_dir) / f"fake_{uuid.uuid4().hex}.wav"
        samples = [0.2 * math.sin(2 * math.pi * 220 * i / 24000) for i in range(12000)]
        write_wav(str(path), samples, 24000)
        return str(path), 500


class FakeEngines:
    def __init__(self) -> None:
        self.stt = FakeSTT()
        self.llm = FakeLLM()
        self.settings = SimpleNamespace(data_path="")


def _wav_bytes(seconds: float = 0.4, sr: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        frames = bytearray()
        for i in range(int(seconds * sr)):
            frames += struct.pack("<h", int(0.2 * 32767 * math.sin(2 * math.pi * 440 * i / sr)))
        wf.writeframes(bytes(frames))
    return buf.getvalue()


@pytest.fixture()
def client(tmp_path):
    db = DB(str(tmp_path / "v2.db"))
    db.init()
    audio = AudioStore(str(tmp_path))
    engines = FakeEngines()
    memory = MemoryManager(db, engines.llm)
    voice = FakeVoice()
    spawner = PersonaSpawner(db, engines.llm)
    chat = ChatService(
        engines, db, memory, voice, spawner, audio, memory_idle_s=0
    )
    services = V2Services(
        db=db, engines=engines, audio=audio, memory=memory, voice=voice,
        spawner=spawner, chat=chat, data_dir=str(tmp_path),
    )
    app = FastAPI()
    app.include_router(v2_router)
    app.state.v2 = services
    with TestClient(app) as test_client:
        yield test_client, services
    db.close()


async def _make_friend(services, **kwargs):
    friend = await services.spawner.spawn(**kwargs)
    await services.chat.store_friend_voice(
        friend, f"Hey! It's {friend['name']}. How's your day going?"
    )
    return friend


# --------------------------------------------------------------------------- #
# 好友 & 引导
# --------------------------------------------------------------------------- #
async def test_bootstrap_with_friend(client):
    http, services = client
    friend = await _make_friend(services)
    r = http.get("/api/v2/bootstrap")
    assert r.status_code == 200
    data = r.json()
    assert data["version"] == "2.0.0"
    assert data["llm"]["mock"] is True
    assert data["stats"]["friends"] == 1
    got = data["friends"][0]
    assert got["id"] == friend["id"]
    assert got["local_time"] and got["local_icon"]
    assert got["unread"] == 1
    assert got["last_message"]["sender"] == "friend"


async def test_discover_and_accept_pending(client):
    http, services = client
    r = http.post("/api/v2/friends/discover", json={"nationality": "Italy"})
    assert r.status_code == 200
    data = r.json()
    friend = data["friend"]
    assert friend["status"] == "active"
    assert friend["nationality"] == "Italy"
    assert data["message"]["sender"] == "friend"
    assert data["message"]["audio_url"].startswith("/api/v2/audio/")

    pending = await services.spawner.spawn(referred_by_friend_id=friend["id"], status="pending_request")
    listed = http.get("/api/v2/friends").json()
    assert any(f["id"] == pending["id"] for f in listed["requests"])

    accepted = http.post(f"/api/v2/friends/{pending['id']}/accept")
    assert accepted.status_code == 200
    assert accepted.json()["friend"]["status"] == "active"
    assert accepted.json()["message"]["transcript"]


async def test_decline_pending(client):
    http, services = client
    friend = await _make_friend(services)
    pending = await services.spawner.spawn(referred_by_friend_id=friend["id"], status="pending_request")
    assert http.post(f"/api/v2/friends/{pending['id']}/decline").status_code == 200
    assert http.get(f"/api/v2/friends/{pending['id']}").status_code == 404


# --------------------------------------------------------------------------- #
# 聊天
# --------------------------------------------------------------------------- #
async def test_text_turn_and_suggestion_polling(client):
    http, services = client
    friend = await _make_friend(services)
    r = http.post(f"/api/v2/chat/{friend['id']}/text",
                  json={"text": "I very like this coffee, it is too much good"})
    assert r.status_code == 200
    data = r.json()
    assert data["user_message"]["sender"] == "user"
    assert data["suggestion_pending"] is True
    reply = data["friend_messages"][0]
    assert reply["sender"] == "friend"
    assert reply["audio_url"]
    assert data["total_turns"] == 1

    # 后台润色 worker
    suggestion = None
    for _ in range(30):
        msgs = http.get(f"/api/v2/friends/{friend['id']}/messages").json()["messages"]
        user_msg = next(m for m in msgs if m["id"] == data["user_message"]["id"])
        if user_msg.get("suggestion") is not None:
            suggestion = user_msg["suggestion"]
            break
        time.sleep(0.1)
    assert suggestion is not None, "润色批注未在预期时间内生成"
    assert suggestion["better_version"]

    # 表达本收藏
    saved = http.post("/api/v2/expressions", json={
        "original_text": data["user_message"]["transcript"],
        "better_expression": suggestion["better_version"],
        "explanation": suggestion["explanation"],
        "source_friend_id": friend["id"],
    })
    assert saved.status_code == 200
    assert saved.json()["expression"]["status"] == "learning"


async def test_voice_turn_and_read(client):
    http, services = client
    friend = await _make_friend(services)
    files = {"audio": ("voice.wav", _wav_bytes(0.5), "audio/wav")}
    r = http.post(f"/api/v2/chat/{friend['id']}/voice", files=files, data={"duration_ms": "500"})
    assert r.status_code == 200
    data = r.json()
    assert data["user_message"]["msg_type"] == "voice"
    assert "indie games" in data["user_message"]["transcript"].lower()
    assert data["friend_messages"][0]["duration_ms"] > 0

    assert http.post(f"/api/v2/friends/{friend['id']}/read").json()["ok"] is True
    assert http.get("/api/v2/friends").json()["friends"][0]["unread"] == 0

    # 无效音频 → 422，不 500
    bad = http.post(f"/api/v2/chat/{friend['id']}/voice",
                    files={"audio": ("bad.webm", b"not-audio", "audio/webm")})
    assert bad.status_code == 422

    # 不存在的 friend → 404
    assert http.post("/api/v2/chat/nope/text", json={"text": "hello"}).status_code == 404


# --------------------------------------------------------------------------- #
# 朋友圈
# --------------------------------------------------------------------------- #
async def test_moments_generate_and_comment(client):
    http, services = client
    friend = await _make_friend(services)
    gen = http.post("/api/v2/moments/generate")
    assert gen.status_code == 200
    moments = gen.json()["moments"]
    assert len(moments) == 1
    moment = moments[0]
    assert moment["friend"]["id"] == friend["id"]
    assert moment["local_time_str"] and moment["content"]
    assert moment["friend"]["name"] == friend["name"]

    # 幂等
    gen2 = http.post("/api/v2/moments/generate").json()
    assert gen2["generated"] is False
    assert len(http.get("/api/v2/moments").json()["moments"]) == 1

    comment = http.post(
        f"/api/v2/moments/{moment['id']}/comment",
        json={"text": "Looks so cozy! What are you drinking?"},
    )
    assert comment.status_code == 200
    payload = comment.json()
    assert payload["friend_id"] == friend["id"]
    assert payload["reply"]["sender"] == "friend"
    assert payload["moment"]["is_replied"] == 1


# --------------------------------------------------------------------------- #
# 表达本 / 记忆
# --------------------------------------------------------------------------- #
async def test_expressions_api(client):
    http, services = client
    created = http.post("/api/v2/expressions", json={
        "original_text": "I very like it",
        "better_expression": "I'm really into it",
        "explanation": "very 不修饰动词",
    }).json()["expression"]
    assert created["status"] == "learning"

    listed = http.get("/api/v2/expressions?status=learning").json()["expressions"]
    assert [e["id"] for e in listed] == [created["id"]]

    patched = http.patch(f"/api/v2/expressions/{created['id']}", json={"status": "mastered"})
    assert patched.json()["expression"]["status"] == "mastered"

    assert http.delete(f"/api/v2/expressions/{created['id']}").status_code == 200
    assert http.get("/api/v2/expressions").json()["expressions"] == []


async def test_memories_api(client):
    http, services = client
    friend = await _make_friend(services)
    data = http.get("/api/v2/memories").json()
    assert data["profile"]["nickname"] == "Traveler"
    assert data["friends"][0]["id"] == friend["id"]

    updated = http.put("/api/v2/memories", json={
        "nickname": "Yulin",
        "fact_sheet": {
            "profile": {"nickname": "Yulin", "city": "Shanghai"},
            "interests": ["indie games"],
            "language_profile": {"target": "en", "weaknesses": ["articles"]},
            "recent_events": [],
        },
    }).json()
    assert updated["profile"]["nickname"] == "Yulin"
    assert updated["fact_sheet"]["interests"] == ["indie games"]

    friend_data = http.put(f"/api/v2/friends/{friend['id']}/memory", json={
        "shared_episodes": ["2026-11-03: chatted about indie games"],
        "open_loops": [{"event": "game demo", "follow_up_after": "2030-01-01T00:00:00+00:00",
                        "status": "pending"}],
    }).json()
    assert friend_data["memory"]["shared_episodes"]
    assert friend_data["memory"]["open_loops"][0]["event"] == "game demo"


# --------------------------------------------------------------------------- #
# 音频 / 声线
# --------------------------------------------------------------------------- #
async def test_audio_and_tts_sample(client):
    http, services = client
    friend = await _make_friend(services)
    msgs = http.get(f"/api/v2/friends/{friend['id']}/messages").json()["messages"]
    audio_url = msgs[-1]["audio_url"]
    assert audio_url
    audio = http.get(audio_url)
    assert audio.status_code == 200
    assert audio.headers["content-type"].startswith("audio/")

    sample = http.get("/api/v2/tts/sample", params={"text": "Hello there", "voice": "en-GB-RyanNeural"})
    assert sample.status_code == 200
    assert len(sample.content) > 1000

    archetypes = http.get("/api/v2/archetypes").json()
    assert len(archetypes["archetypes"]) >= 20
    assert archetypes["countries"]

    assert http.get("/api/v2/audio/nope.wav").status_code == 404
    assert http.get("/api/v2/health").json()["status"] == "ok"
