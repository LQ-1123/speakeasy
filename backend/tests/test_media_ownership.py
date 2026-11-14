"""多用户媒体隔离：私聊音频 / 聊天图片 / 动态图片的归属校验 + 短时媒体令牌。

覆盖 PRD「数据隔离」一期验收点：
1. A 的资产被 B 直接 GET → 403；
2. A 自己 GET → 200；
3. A 申请 token 后 B 用 → 401/403（身份头与令牌不一致）；
4. A 用 token（不带身份头）→ 200（令牌即凭证）；
5. 未登记的历史文件 → 200 + ``X-Legacy-Asset: 1``。

全程离线：mock STT / mock LLM / FakeVoice（只写 wav，不联网）。
"""

from __future__ import annotations

import io
import math
import struct
import sys
import uuid
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
from app.v2.media_store import MediaStore  # noqa: E402
from app.v2.memory import MemoryManager  # noqa: E402
from app.v2.router import router as v2_router  # noqa: E402
from app.v2.services import V2Services  # noqa: E402
from app.v2.spawner import PersonaSpawner  # noqa: E402
from app.v3 import accounts  # noqa: E402
from app.v3 import media_access  # noqa: E402
from app.v3.media_access import (  # noqa: E402
    ensure_media_schema,
    issue_media_token,
    owner_of,
    register_asset,
    verify_media_token,
)
from app.v3.media_access_router import router as media_access_router  # noqa: E402
from app.v3.schema import ensure_schema  # noqa: E402
from app.v3.security import register_user  # noqa: E402
from app.voice.audio_utils import write_wav  # noqa: E402
from app.voice.stt_config import STTConfigStore  # noqa: E402

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


# --------------------------------------------------------------------------- #
# 离线测试替身（与 test_v2_api.py 保持一致的最小集合）
# --------------------------------------------------------------------------- #
class FakeSTT:
    backend_name = "mock"

    async def transcribe(self, pcm16, sample_rate=16000, **kwargs):
        return SimpleNamespace(
            text="Hi, I really like indie games and coffee.",
            language="en",
            latency_ms=1.0,
            backend="mock",
        )


class FakeLLM:
    uses_mock = True

    def health(self) -> dict:
        return {"configured": False, "model": "mock", "mock": True}

    async def stream_chat(self, messages, **kwargs):
        for token in "Oh nice! What kind of games are you into these days?".split():
            yield token + " "

    async def complete_json(self, *args, **kwargs):  # pragma: no cover
        return "{}"


class FakeVoice:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    async def synth_to_wav(self, text, voice_id, *, rate="+0%", pitch="+0Hz", out_dir):
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


def _png_upload(name: str = "cat.png"):
    return {"image": (name, PNG_BYTES, "image/png")}


def _headers(user: dict) -> dict[str, str]:
    return {"X-User-Id": user["user_id"], "X-User-Token": user["token"]}


def test_email_mode_signed_audio_bypasses_header_gate_but_not_archived_owner(env, monkeypatch):
    from app.v3.account_archive import archive_users
    db, http, uid = env.db, env.http, env.a["user_id"]
    with db._lock, db.conn:
        db.conn.execute("INSERT INTO v3_accounts(user_id,email,password_hash,email_verified_at,created_at,updated_at) VALUES (?,?,'','verified','','')", (uid, "a@example.com"))
    monkeypatch.setenv("SE_EMAIL_CODE_AUTH", "1")
    monkeypatch.setenv("SE_REQUIRE_AUTH", "1")
    path = Path(env.services.audio.audio_dir) / "signed.wav"
    path.write_bytes(b"RIFFtest")
    register_asset(db, uid, "audio", path.name)
    token = issue_media_token(db, uid, "audio", path.name)
    response = http.get(f"/api/v2/audio/{path.name}", params={"t": token})
    assert response.status_code == 200, response.text
    http.cookies.set("sonder_session", "stale-webview-cookie")
    assert http.get(f"/api/v2/audio/{path.name}", params={"t": token}).status_code == 200
    http.cookies.clear()
    assert http.get(f"/api/v2/audio/{path.name}").status_code in (401, 403)
    archive_users(db, [uid], migration_id="test")
    assert http.get(f"/api/v2/audio/{path.name}", params={"t":token}).status_code in (401, 403)


# --------------------------------------------------------------------------- #
# fixture：真实 DB + v2 路由 + 媒体令牌路由，离线引擎
# --------------------------------------------------------------------------- #
@pytest.fixture()
def env(tmp_path):
    db = DB(str(tmp_path / "v2.db"))
    db.init()
    ensure_schema(db, seed=False)  # v3_users（身份）等表
    accounts.ensure_accounts_schema(db)  # v3_sessions / v3_accounts（会话凭据）
    ensure_media_schema(db)

    user_a = register_user(db, "device_media_A01", "Alice")
    user_b = register_user(db, "device_media_B01", "Bob")
    # A 的「会话凭据」：没有 X-User-Id 头，靠 X-Session-Token 解析身份
    session_a = accounts.create_session(db, user_a["user_id"], platform="test")

    audio = AudioStore(str(tmp_path))
    media = MediaStore(str(tmp_path))
    engines = FakeEngines()
    engines.stt_config = STTConfigStore(str(tmp_path))
    memory = MemoryManager(db, engines.llm)
    voice = FakeVoice()
    spawner = PersonaSpawner(db, engines.llm)
    chat = ChatService(
        engines, db, memory, voice, spawner, audio, media, memory_idle_s=0
    )
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

    app = FastAPI()
    app.include_router(v2_router)
    # main.py 会负责接线（Lead）；这里直接挂上，验证端点本身可用
    app.include_router(media_access_router)
    app.state.v2 = services

    friend = db.create_friend(
        {
            "name": "Tom",
            "owner_user_id": user_a["user_id"],
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

    with TestClient(app) as http:
        yield SimpleNamespace(
            http=http,
            db=db,
            services=services,
            friend=friend,
            a=user_a,
            b=user_b,
            session_a=session_a,
            headers_a=_headers(user_a),
            headers_b=_headers(user_b),
            tmp_path=tmp_path,
        )
    db.close()


def _first_friend_audio(http, friend, headers) -> str:
    """发一条文字消息，返回好友回复语音的 URL。"""
    response = http.post(
        f"/api/v2/chat/{friend['id']}/text",
        json={"text": "hello"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    friend_messages = response.json()["friend_messages"]
    assert friend_messages, response.text
    url = friend_messages[0]["audio_url"]
    assert url and url.startswith("/api/v2/audio/")
    return url


def _filename(url: str) -> str:
    return url.split("?", 1)[0].rsplit("/", 1)[-1]


# --------------------------------------------------------------------------- #
# 1) 私有音频：A 可读、B 403
# --------------------------------------------------------------------------- #
def test_friend_audio_is_private_to_its_owner(env):
    url = _first_friend_audio(env.http, env.friend, env.headers_a)
    name = _filename(url)

    # 已登记资产（好友回复音频按 friends.owner_user_id 归属 A）
    assert owner_of(env.db, "audio", name) == env.a["user_id"]

    assert env.http.get(url, headers=env.headers_a).status_code == 200
    assert env.http.get(url, headers=env.headers_b).status_code == 403
    # 返回给客户端的 URL 自带签名，因此不带身份头也能播放；但去掉令牌后
    # 仍然是 local 身份，不能读别人的私聊音频。
    assert env.http.get(url).status_code == 200
    assert env.http.get(f"/api/v2/audio/{name}").status_code == 403


def test_message_audio_url_is_signed_for_instant_playback(env):
    """返回给客户端的语音 URL 必须带令牌：<audio src> 无法携带身份头。"""
    from urllib.parse import parse_qs, urlparse
    from app.v3.media_access import signed_url

    url = _first_friend_audio(env.http, env.friend, env.headers_a)
    query = parse_qs(urlparse(url).query)
    assert query.get("t"), url
    # 令牌即凭证：不带任何身份头也能直接播放（不用再现场合成）
    played = env.http.get(url)
    assert played.status_code == 200, played.text
    assert played.content[:4] == b"RIFF"

    # 二次序列化（bootstrap + messages）不能把已签名 URL 弄坏：
    # path 保持不变、只保留一个 t，且新令牌依然可播放。
    again = signed_url(env.db, env.a["user_id"], url)
    parsed = urlparse(again)
    assert parsed.path == urlparse(url).path
    assert len(parse_qs(parsed.query).get("t", [])) == 1
    assert env.http.get(again).status_code == 200


# --------------------------------------------------------------------------- #
# 2) 短时媒体令牌：A 申领 → A 匿名可用 → B 拿着无效
# --------------------------------------------------------------------------- #
def test_short_lived_media_token_grants_owner_only(env):
    url = _first_friend_audio(env.http, env.friend, env.headers_a)
    name = _filename(url)

    # B 不能给别人的资产申请令牌
    denied = env.http.get(
        "/api/v3/media/access",
        params={"kind": "audio", "filename": name},
        headers=env.headers_b,
    )
    assert denied.status_code == 403, denied.text

    granted = env.http.get(
        "/api/v3/media/access",
        params={"kind": "audio", "filename": name},
        headers=env.headers_a,
    )
    assert granted.status_code == 200, granted.text
    payload = granted.json()
    assert payload["kind"] == "audio" and payload["filename"] == name
    token = payload["token"]

    # A 用令牌（不带身份头，令牌即凭证）→ 200
    assert env.http.get(url, params={"t": token}).status_code == 200
    # B 带着自己的身份头 + A 的令牌 → 403（令牌只对 owner 生效）
    stolen = env.http.get(url, params={"t": token}, headers=env.headers_b)
    assert stolen.status_code in (401, 403)
    # 乱写令牌 → 403
    assert env.http.get(url, params={"t": "v1.bad.sig"}).status_code == 403

    # 令牌端点需要登录
    assert env.http.get(
        "/api/v3/media/access", params={"kind": "audio", "filename": name}
    ).status_code == 401


# --------------------------------------------------------------------------- #
# 3) 历史兼容：未登记文件仍可读，但打 X-Legacy-Asset
# --------------------------------------------------------------------------- #
def test_unregistered_legacy_audio_still_readable_with_header(env):
    legacy_name = f"legacy_{uuid.uuid4().hex}.wav"
    write_wav(
        str(Path(env.services.audio.audio_dir) / legacy_name),
        [0.1] * 2400,
        24000,
    )
    legacy_url = f"/api/v2/audio/{legacy_name}"

    response = env.http.get(legacy_url)
    assert response.status_code == 200, response.text
    assert response.headers.get("x-legacy-asset") == "1"
    assert response.content[:4] == b"RIFF"


# --------------------------------------------------------------------------- #
# 4) 聊天图片 / 动态图片同样按 owner 隔离
# --------------------------------------------------------------------------- #
def test_chat_image_is_private_to_sender(env):
    response = env.http.post(
        f"/api/v2/chat/{env.friend['id']}/image",
        files=_png_upload(),
        data={"text": "看我的猫"},
        headers=env.headers_a,
    )
    assert response.status_code == 200, response.text
    image_url = response.json()["user_message"]["image_url"]
    assert image_url.startswith("/api/v2/media/")

    assert env.http.get(image_url, headers=env.headers_a).status_code == 200
    assert env.http.get(image_url, headers=env.headers_b).status_code == 403


def test_moment_photo_is_private_to_poster(env):
    response = env.http.post(
        "/api/v2/moments/photo",
        files=_png_upload("sunset.png"),
        data={"content": "傍晚的天空"},
        headers=env.headers_a,
    )
    assert response.status_code == 200, response.text
    image_url = response.json()["moment"]["image_url"]
    assert image_url.startswith("/api/v2/media/")

    assert env.http.get(image_url, headers=env.headers_a).status_code == 200
    assert env.http.get(image_url, headers=env.headers_b).status_code == 403


def test_uploaded_image_registration_records_owner(env):
    """v3 上传落盘处（app.v3.uploads）登记 owner；URL 形状保持不变。"""
    from app.v3.uploads import save_user_image

    data = save_user_image(
        PNG_BYTES,
        env.a["user_id"],
        str(env.tmp_path / "uploads"),
        db=env.db,
    )
    assert data["url"].startswith("/uploads/")
    relative = data["url"][len("/uploads/") :]
    assert owner_of(env.db, "post_image", relative) == env.a["user_id"]


# --------------------------------------------------------------------------- #
# 5) 令牌单元行为：签名 / 过期 / kind 绑定 / 幂等登记
# --------------------------------------------------------------------------- #
def test_media_token_verification_rules(env, monkeypatch):
    token = issue_media_token(env.db, env.a["user_id"], "audio", "x.wav", ttl_s=60)
    assert verify_media_token(env.db, token) == (env.a["user_id"], "audio", "x.wav")

    # 篡改签名 / payload 都不认
    assert verify_media_token(env.db, token[:-1] + ("A" if token[-1] != "A" else "B")) is None
    assert verify_media_token(env.db, "v2.abc.def") is None
    assert verify_media_token(env.db, "") is None

    # 过期令牌：用可控的 time 替身验证 exp 语义
    fake_time = SimpleNamespace(time=lambda: 1_000_000.0)
    monkeypatch.setattr(media_access, "time", fake_time)
    token = issue_media_token(env.db, env.a["user_id"], "audio", "y.wav", ttl_s=10)
    fake_time.time = lambda: 1_000_011.0
    assert verify_media_token(env.db, token) is None


def test_session_credential_user_can_request_media_token(env):
    """会话凭据用户（X-Session-Token，无 X-User-Id）也能申请令牌。

    账号体系上线后身份由 ``bind_current_user`` 统一解析；媒体令牌端点不能只看
    ``X-User-Id`` 头，否则 Web/手机端的会话用户会被误判成「未登录」。
    """
    url = _first_friend_audio(env.http, env.friend, env.headers_a)
    name = _filename(url)

    response = env.http.get(
        "/api/v3/media/access",
        params={"kind": "audio", "filename": name},
        headers={"X-Session-Token": env.session_a["session_token"]},
    )
    assert response.status_code == 200, response.text
    token = response.json()["token"]
    assert verify_media_token(env.db, token)[0] == env.a["user_id"]

    # 会话用户签发的令牌可以被 <audio src="...?t=..."> 直接使用
    assert env.http.get(url, params={"t": token}).status_code == 200


def test_ensure_media_schema_is_idempotent_and_skips_repeat_ddl(env):
    from app.v3 import media_access as media_access_mod

    ensure_media_schema(env.db)
    ensure_media_schema(env.db)  # 重复调用必须幂等

    class _NoConn:
        """已建表的快路径不应再碰 DB；访问任何属性都视为回归。"""

        def __getattr__(self, name):
            raise AssertionError(f"ensure_media_schema 快路径访问了 DB 属性 {name}")

    ready = _NoConn()
    media_access_mod._schema_ready.add(ready)
    try:
        media_access_mod.ensure_media_schema(ready)  # 直接 return，不访问 conn/_lock
    finally:
        media_access_mod._schema_ready.discard(ready)

    columns = {
        row[1] for row in env.db.conn.execute("PRAGMA table_info(media_assets)").fetchall()
    }
    assert columns == {
        "asset_id",
        "owner_user_id",
        "kind",
        "filename",
        "created_at",
    }


def test_register_asset_is_idempotent_and_never_reassigns_owner(env):
    first = register_asset(env.db, env.a["user_id"], "chat_image", "img_shared.png")
    second = register_asset(env.db, env.b["user_id"], "chat_image", "img_shared.png")
    assert first is not None and second is not None
    # 第一次登记为准：B 无法改写 A 的资产归属
    assert second["asset_id"] == first["asset_id"]
    assert owner_of(env.db, "chat_image", "img_shared.png") == env.a["user_id"]
    # 目录穿越/空文件名被拒绝
    assert owner_of(env.db, "chat_image", "../etc/passwd") is None
    with pytest.raises(ValueError):
        register_asset(env.db, env.a["user_id"], "audio", "../escape.wav")
