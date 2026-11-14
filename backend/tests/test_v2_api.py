"""v2 REST API 集成测试（离线：mock STT / 规则 LLM / Fake voice）。"""

from __future__ import annotations

import io
import math
import struct
import sys
import time
import wave
from datetime import datetime
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
from app.v2.db import DB
from app.v2.media_store import MediaStore  # noqa: E402
from app.v2.memory import MemoryManager  # noqa: E402
from app.v2.router import router as v2_router  # noqa: E402
from app.v2.services import V2Services  # noqa: E402
from app.v2.spawner import PersonaSpawner  # noqa: E402
from app.voice.audio_utils import write_wav  # noqa: E402
from app.voice.stt_config import STTConfigStore  # noqa: E402

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #
class FakeSTT:
    backend_name = "mock"

    def __init__(self) -> None:
        self.last_language: str | None = "unset"
        self.last_prefer_local: bool | None = None
        self.last_fast: bool | None = None

    async def transcribe(
        self,
        pcm16: bytes,
        sample_rate: int = 16000,
        *,
        language=None,
        prefer_local=False,
        fast=False,
    ):
        self.last_language = language
        self.last_prefer_local = prefer_local
        self.last_fast = fast
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
    media = MediaStore(str(tmp_path))
    engines = FakeEngines()
    memory = MemoryManager(db, engines.llm)
    voice = FakeVoice()
    spawner = PersonaSpawner(db, engines.llm)
    chat = ChatService(
        engines, db, memory, voice, spawner, audio, media, memory_idle_s=0
    )
    engines.stt_config = STTConfigStore(str(tmp_path))
    services = V2Services(
        db=db, engines=engines, audio=audio, memory=memory, voice=voice,
        spawner=spawner, chat=chat, data_dir=str(tmp_path), media=media,
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
    assert friend["status"] == "pending_request"
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


async def test_discover_japanese_friend_replies_in_japanese(client):
    http, services = client
    r = http.post("/api/v2/friends/discover", json={"nationality": "Japan", "language": "ja"})
    assert r.status_code == 200, r.text
    friend = r.json()["friend"]
    assert friend["nationality"] == "Japan"
    assert friend["reply_language"] == "ja"
    assert friend["voice_id"].startswith("ja-JP")
    assert friend["native_lang"] == "ja"

    r = http.post(f"/api/v2/chat/{friend['id']}/text", json={"text": "こんにちは！最近どう？"})
    assert r.status_code == 200
    data = r.json()
    reply = data["friend_messages"][0]["transcript"]
    assert data["reply_language"] == "ja"
    # 日语假名（平假名/片假名）出现在回复里
    assert any("\u3040" <= ch <= "\u30ff" for ch in reply), reply
    assert "Japanese" in services.chat._system_prompt(services.db.get_friend(friend["id"]), [])

    # 语音回合：STT 应收到 language="ja"（本地 whisper 用日语识别）
    files = {"audio": ("voice.wav", _wav_bytes(0.3), "audio/wav")}
    r = http.post(f"/api/v2/chat/{friend['id']}/voice", files=files)
    assert r.status_code == 200
    assert services.engines.stt.last_language == "ja"


async def test_discover_english_reply_for_japanese_friend(client):
    http, services = client
    r = http.post("/api/v2/friends/discover", json={"nationality": "Japan", "language": "en"})
    assert r.status_code == 200, r.text
    friend = r.json()["friend"]
    assert friend["nationality"] == "Japan"
    assert friend["reply_language"] == "en"
    # 仍是日本声线（英语带日本口音）
    assert friend["voice_id"].startswith("ja-JP")


async def test_discover_unsupported_language_returns_422(client):
    http, _ = client
    r = http.post("/api/v2/friends/discover", json={"language": "ar"})
    assert r.status_code == 422
    detail = r.json().get("detail", "")
    assert "ar" in detail or "不支持" in detail or "支持" in detail


async def test_delete_friend_cascades_messages_and_audio(client):
    http, services = client
    friend = await _make_friend(services)
    # 再造一条用户语音，保证有用户音频文件
    files = {"audio": ("voice.wav", _wav_bytes(0.3), "audio/wav")}
    http.post(f"/api/v2/chat/{friend['id']}/voice", files=files)

    msgs = http.get(f"/api/v2/friends/{friend['id']}/messages", params={"limit": 50}).json()["messages"]
    audio_urls = [m["audio_url"] for m in msgs if m.get("audio_url")]
    assert audio_urls, "应存在好友/用户语音文件"

    # 一个被该好友推荐的好友：删除后推荐关系应被清空
    referred = await services.spawner.spawn(
        referred_by_friend_id=friend["id"], status="pending_request"
    )

    r = http.delete(f"/api/v2/friends/{friend['id']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["friend_id"] == friend["id"]
    assert body["deleted_audio"] >= 1

    assert http.get(f"/api/v2/friends/{friend['id']}").status_code == 404
    assert http.get(f"/api/v2/friends/{friend['id']}/messages").status_code == 404
    for url in audio_urls:
        assert http.get(url).status_code == 404, url
    assert services.db.get_friend(friend["id"]) is None
    assert services.db.get_friend(referred["id"])["referred_by_friend_id"] is None

    # 重复删除 → 404
    assert http.delete(f"/api/v2/friends/{friend['id']}").status_code == 404


async def test_delete_pending_request_friend(client):
    http, services = client
    friend = await _make_friend(services)
    pending = await services.spawner.spawn(
        referred_by_friend_id=friend["id"], status="pending_request"
    )
    assert http.delete(f"/api/v2/friends/{pending['id']}").status_code == 200
    assert http.get(f"/api/v2/friends/{pending['id']}").status_code == 404
    assert services.db.get_friend(friend["id"]) is not None


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
    assert data["suggestion_pending"] is False
    requested = http.post(f"/api/v2/messages/{data['user_message']['id']}/suggestion")
    assert requested.status_code == 200, requested.text
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

    # 离线/无模型时不编造动态：朋友圈是可选产出，不再是「每位好友必须一条」
    gen = http.post("/api/v2/moments/generate")
    assert gen.status_code == 200
    assert gen.json()["moments"] == []
    assert http.get("/api/v2/moments").json()["moments"] == []

    # 已有动态时 generate 幂等：不重复生成、直接返回已有内容
    feed_date = datetime.now().astimezone().strftime("%Y-%m-%d")
    services.db.add_moment(
        friend["id"],
        "Fixed the squeaky chair with tape. Still slightly squeaky.",
        local_time_str="Mon 09:40",
        feed_date=feed_date,
    )
    gen2 = http.post("/api/v2/moments/generate").json()
    assert gen2["generated"] is False
    moments = http.get("/api/v2/moments").json()["moments"]
    assert len(moments) == 1
    moment = moments[0]
    assert moment["friend"]["id"] == friend["id"]
    assert moment["local_time_str"] and moment["content"]
    assert moment["friend"]["name"] == friend["name"]

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
    assert audio.headers["content-type"].startswith("audio/wav")  # Safari 兼容：不用 audio/x-wav

    sample = http.get("/api/v2/tts/sample", params={"text": "Hello there", "voice": "en-GB-RyanNeural"})
    assert sample.status_code == 200
    assert len(sample.content) > 1000

    archetypes = http.get("/api/v2/archetypes").json()
    assert len(archetypes["archetypes"]) >= 20
    assert archetypes["countries"]

    assert http.get("/api/v2/audio/nope.wav").status_code == 404
    assert http.get("/api/v2/health").json()["status"] == "ok"


# --------------------------------------------------------------------------- #
# 图片多模态 / 带图动态 / 通话摘要
# --------------------------------------------------------------------------- #
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


async def test_chat_image_upload_and_media_serving(client):
    http, services = client
    friend = await _make_friend(services)
    r = http.post(
        f"/api/v2/chat/{friend['id']}/image",
        files={"image": ("cat.png", PNG_BYTES, "image/png")},
        data={"text": "看我的猫"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    user_msg = data["user_message"]
    assert user_msg["msg_type"] == "image"
    assert user_msg["transcript"] == "看我的猫"
    assert user_msg["image_url"].startswith("/api/v2/media/")
    friend_msg = data["friend_messages"][0]
    assert friend_msg["audio_url"]
    media = http.get(user_msg["image_url"])
    assert media.status_code == 200
    assert media.headers["content-type"].startswith("image/png")
    assert media.content == PNG_BYTES


async def test_user_moment_photo_and_call_summary(client):
    http, services = client
    friend = await _make_friend(services)
    r = http.post(
        "/api/v2/moments/photo",
        files={"image": ("sunset.png", PNG_BYTES, "image/png")},
        data={"content": "傍晚的天空", "mood_tag": "🌇 傍晚"},
    )
    assert r.status_code == 200, r.text
    moment = r.json()["moment"]
    assert moment["author_type"] == "user"
    assert moment["author_name"] == "我"
    assert moment["image_url"].startswith("/api/v2/media/")
    assert http.get(moment["image_url"]).status_code == 200

    r = http.post(
        f"/api/v2/chat/{friend['id']}/call/summary",
        json={"duration_s": 83, "turns": 4},
    )
    assert r.status_code == 200, r.text
    msg = r.json()["message"]
    assert msg["msg_type"] == "call_summary"
    assert "01:23" in msg["transcript"] and "4 轮" in msg["transcript"]


# --------------------------------------------------------------------------- #
# 通话流式回合 / warmup
# --------------------------------------------------------------------------- #
async def test_call_stream_turn_events_and_persistence(client):
    http, services = client
    friend = await _make_friend(services)
    previous_messages = services.db.list_messages(friend["id"])
    events = [
        event
        async for event in services.chat.handle_call_turn_stream(
            friend["id"], _wav_bytes(0.6), "call.wav", 600
        )
    ]
    kinds = [e["type"] for e in events]
    assert "transcript" in kinds
    assert "sentence" in kinds
    assert kinds[-1] == "done"
    assert services.engines.stt.last_prefer_local is True  # 通话走本地 STT
    assert services.engines.stt.last_fast is True  # 通话走快速模型
    transcript_event = next(e for e in events if e["type"] == "transcript")
    assert transcript_event["text"].startswith("Hi, I really like")
    sentence_events = [e for e in events if e["type"] == "sentence"]
    assert all(e["audio_url"].startswith("/api/v2/audio/") for e in sentence_events)
    done = events[-1]
    # Call turns stay transient; the call ends with one summary instead of
    # creating an ordinary voice chat message for every sentence.
    assert done["friend_message"] is None
    assert services.db.list_messages(friend["id"]) == previous_messages


async def test_call_warmup_only_prewarms(client):
    http, services = client
    friend = await _make_friend(services)
    r = http.get(f"/api/v2/chat/{friend['id']}/call/warmup")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert "filler_url" not in r.json()


async def test_friend_replying_status_and_list_flag(client):
    http, services = client
    friend = await _make_friend(services)
    services.chat.mark_replying(friend["id"])
    try:
        status = http.get(f"/api/v2/friends/{friend['id']}/status").json()
        assert status["replying"] is True
        assert status["started_at"]
        friends = http.get("/api/v2/friends").json()["friends"]
        row = next(f for f in friends if f["id"] == friend["id"])
        assert row["replying"] is True
        # 聊天接口返回的 payload / bootstrap 也带 replying
        bootstrap = http.get("/api/v2/bootstrap").json()
        row = next(f for f in bootstrap["friends"] if f["id"] == friend["id"])
        assert row["replying"] is True
    finally:
        services.chat.clear_replying(friend["id"])
    status = http.get(f"/api/v2/friends/{friend['id']}/status").json()
    assert status["replying"] is False and status["started_at"] is None


async def test_call_summary_not_replayed_into_chat_context(client):
    http, services = client
    friend = await _make_friend(services)
    services.db.add_message(
        friend["id"], "friend", "📞 语音通话 00:26 · 9 轮对话", msg_type="call_summary"
    )

    captured: list[list[dict]] = []

    class CaptureLLM:
        uses_mock = False

        async def stream_chat(self, messages, **kwargs):
            captured.append(messages)
            yield "Sure, tell me more."

    old_llm = services.chat.engines.llm
    services.chat.engines = SimpleNamespace(llm=CaptureLLM(), stt=services.engines.stt)
    try:
        await services.chat.handle_text(friend["id"], "Do you have any pets?")
    finally:
        services.chat.engines = old_llm
    assert captured, "LLM 应被调用"
    joined = str(captured[0])
    assert "语音通话" not in joined
    assert "Do you have any pets?" in joined
