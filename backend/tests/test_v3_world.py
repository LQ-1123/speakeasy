"""V3 · 世界生成 / 偶遇 / 动态朗读 测试。

对应产品规则：
1. 「捏人」只发生一次：生成世界 = 人物 + 他们的动态（一次生成，长期复用）
2. 偶遇只是从已生成的世界里挑人，**不再现场捏人**，且头像/资料/声音与信息流一致
3. 信息流内容可翻译、可用人物自己的声音朗读
"""

from __future__ import annotations

import random
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.v2.db import DB  # noqa: E402
from app.v2.router import router as v2_router  # noqa: E402
from app.v3.identity import reset_current_user, set_current_user  # noqa: E402
from app.v3.router import router as v3_router  # noqa: E402
from app.v3.schema import ensure_schema, seed_if_needed  # noqa: E402
from app.v3.service import V3Service  # noqa: E402
from app.v3.world import ensure_world, world_status  # noqa: E402


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def empty_db(tmp_path):
    """空库（未生成世界），用于测试世界生成。"""
    db = DB(str(tmp_path / "v3_empty.db"))
    db.init()
    ensure_schema(db, seed=False)
    yield db, tmp_path
    db.close()


@pytest.fixture()
def world_db(tmp_path):
    """已生成世界的库（常规用例）。"""
    db = DB(str(tmp_path / "v3_world.db"))
    db.init()
    ensure_schema(db, seed=False)
    seed_if_needed(db)
    yield db, tmp_path
    db.close()


@pytest.fixture()
def service(world_db):
    db, tmp_path = world_db
    return V3Service(db, SimpleNamespace(llm=SimpleNamespace(uses_mock=True)), str(tmp_path))


class _FakeVoice:
    """假的语音合成：写一个最小 wav，避免测试依赖网络/模型。"""

    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.calls: list[tuple[str, str]] = []

    async def synth_to_wav(self, text, voice_id, *, rate="+0%", pitch="+0Hz", out_dir):
        self.calls.append((text, voice_id))
        path = Path(out_dir) / f"fake_{len(self.calls)}.wav"
        path.write_bytes(b"RIFF" + b"\x00" * 64)
        return str(path), 1234


def _make_app(db, tmp_path: Path):
    service = V3Service(db, SimpleNamespace(llm=SimpleNamespace(uses_mock=True)), str(tmp_path))
    service.rng = random.Random(11)
    voice = _FakeVoice(tmp_path)
    services = SimpleNamespace(
        db=db,
        voice=voice,
        audio=SimpleNamespace(audio_dir=str(tmp_path)),
        engines=SimpleNamespace(llm=SimpleNamespace(uses_mock=True)),
        onboarding_pending=False,
        followup_inflight=set(),
        spawn_bg=lambda coro: (coro.close(), None)[1],
    )
    app = FastAPI()
    app.state.v2 = services
    app.state.v3 = service
    app.include_router(v2_router)
    app.include_router(v3_router)
    return app, service, voice


def _register(client: TestClient, device: str) -> dict:
    data = client.post("/api/v3/auth/register", json={"device_key": device}).json()
    return {"X-User-Id": data["user_id"], "X-User-Token": data["token"]}


# --------------------------------------------------------------------------- #
# 世界生成
# --------------------------------------------------------------------------- #
async def test_ensure_world_seeds_once(empty_db):
    db, _ = empty_db
    first = await ensure_world(db, None)
    assert first["personas"] == 12 and first["posts"] == 36
    assert first["source"] == "seed"
    assert first["generated_at"]

    second = await ensure_world(db, None)
    assert second["personas"] == 12 and second["posts"] == 36  # 不重复插入
    assert world_status(db)["personas"] == 12


def test_seed_is_skipped_when_world_exists(empty_db):
    """已有世界（例如 LLM 生成的）不能被预置语料重复灌入。"""
    db, _ = empty_db
    assert seed_if_needed(db) == 36
    assert seed_if_needed(db) == 0  # 第二次不再写入
    assert db.conn.execute("SELECT COUNT(*) FROM personas").fetchone()[0] == 12


# --------------------------------------------------------------------------- #
# 偶遇：从世界里挑人（不现场捏人）
# --------------------------------------------------------------------------- #
def test_discover_next_uses_existing_world(service):
    data = service.discover_next("u_alice")
    assert data is not None
    assert data["source"] == "world"
    persona = data["persona"]
    assert persona["id"].startswith("p_")
    assert persona["avatar"].startswith("/api/v3/avatar/p_")
    assert persona["is_ai"] is True
    # 打招呼内容 = 该人物最新一条动态（因此声音/内容与信息流一致）
    posts = service.persona_posts(persona["id"], user_id="u_alice", limit=3)["items"]
    assert posts and data["greeting"] == posts[0]["content"]
    assert data["post_id"] == posts[0]["id"]


def test_persona_language_matches_voice_and_posts(service):
    """国旗 / 文字 / 声线必须一致：语言代码 = 声线前缀 = 动态所用语言。"""
    voice_prefix = {
        "en": ("en-",),
        "ja": ("ja-",),
        "ko": ("ko-",),
        "de": ("de-",),
        "es": ("es-",),
        "it": ("it-",),
        "pt": ("pt-",),
        "zh": ("zh-",),
        "fr": ("fr-",),
    }
    personas = service._personas()  # noqa: SLF001 - 测试内部读取
    assert len(personas) == 12
    for persona in personas:
        lang = persona["language"]
        assert lang in voice_prefix, f"{persona['id']} 语言非法: {lang}"
        assert persona["voice_id"].startswith(voice_prefix[lang]), (
            f"{persona['id']} 语言={lang} 但声线={persona['voice_id']}"
        )
        posts = service.persona_posts(persona["id"], user_id="u_lang", limit=5)["items"]
        assert posts, f"{persona['id']} 没有动态"
        for post in posts:
            # 简单的语言一致性检查：不同语言的字符集/常见词
            content = post["content"]
            if lang == "ko":
                assert any("\uac00" <= ch <= "\ud7a3" for ch in content), content
            elif lang == "ja":
                assert any("\u3040" <= ch <= "\u30ff" or "\u4e00" <= ch <= "\u9fff" for ch in content), content
            elif lang == "zh":
                assert any("\u4e00" <= ch <= "\u9fff" for ch in content), content
            elif lang in ("de", "es", "it", "pt"):
                assert any(ch in content.lower() for ch in "aeiou"), content
                assert content != content.upper(), content


def test_friend_from_persona_speaks_own_language(service):
    """加好友后，好友的回复语言/声线 = 人物母语/声线（聊天与信息流一致）。"""
    j = service.persona_by_id("p_jiwoo")
    assert j is not None and j["language"] == "ko"
    result = service.add_friend("u_lang", "p_jiwoo")
    friend = service.db.get_friend(result["friend_id"], any_owner=True)
    assert friend is not None
    assert friend["reply_language"] == "ko"
    assert friend["native_lang"] == "ko"
    assert friend["voice_id"] == "ko-KR-SunHiNeural"


def test_discover_next_rotates_and_resets(service):
    seen = []
    for _ in range(12):
        data = service.discover_next("u_bob")
        assert data is not None
        seen.append(data["persona"]["id"])
    assert len(set(seen)) == 12  # 12 位人物都被轮换到，不重复

    # 看完全部之后自动重置，仍然能继续挑
    again = service.discover_next("u_bob")
    assert again is not None and again["persona"]["id"] in seen


def test_discover_next_skips_existing_friends(service):
    first = service.discover_next("u_carol")
    friend_id = first["persona"]["id"]
    service.add_friend("u_carol", friend_id)

    for _ in range(5):
        nxt = service.discover_next("u_carol")
        assert nxt is not None
        assert nxt["persona"]["id"] != friend_id


# --------------------------------------------------------------------------- #
# API：偶遇 + 朗读
# --------------------------------------------------------------------------- #
def test_discover_next_api(world_db):
    db, tmp_path = world_db
    app, service, _voice = _make_app(db, tmp_path)
    with TestClient(app) as client:
        headers = _register(client, "device_world_0001")
        response = client.get("/api/v3/discover/next", headers=headers)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["source"] == "world"
        assert data["persona"]["avatar"].startswith("/api/v3/avatar/p_")
        assert data["post_id"]

        # 加好友之后不再出现在偶遇里
        persona_id = data["persona"]["id"]
        assert client.post(f"/api/v3/personas/{persona_id}/friend", headers=headers).status_code == 200
        for _ in range(4):
            nxt = client.get("/api/v3/discover/next", headers=headers).json()
            assert nxt["persona"]["id"] != persona_id


def test_persona_post_speech_uses_persona_voice(world_db):
    db, tmp_path = world_db
    app, service, voice = _make_app(db, tmp_path)
    with TestClient(app) as client:
        post = service.get_persona_post_row("pp_adrian_00")
        assert post is not None
        persona = service.persona_by_id(post["persona_id"])
        assert persona is not None

        # 公共音频：不带身份头也能播放（<audio> 标签无法加自定义头）
        response = client.get("/api/v3/persona-posts/pp_adrian_00/speech")
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("audio/wav")
        assert response.content[:4] == b"RIFF"
        assert voice.calls and voice.calls[0][1] == persona["voice_id"]
        # 英文帖子 → 英文声线
        assert response.headers["x-language"] == "en"
        assert response.headers["x-voice"].startswith("en-")

        assert client.get("/api/v3/persona-posts/nope/speech").status_code == 404


def test_speech_voice_follows_post_language(world_db):
    """朗读声线跟随**帖子语言**，不是人物国籍。"""
    db, tmp_path = world_db
    app, service, voice = _make_app(db, tmp_path)
    with TestClient(app) as client:
        # Lin 是中文人物，帖子也是中文 → 中文声线
        lin_post = service.get_persona_post_row("pp_lin_27")
        assert lin_post is not None and lin_post["language"] == "zh"
        r = client.get("/api/v3/persona-posts/pp_lin_27/speech")
        assert r.status_code == 200
        assert r.headers["x-language"] == "zh"
        assert r.headers["x-voice"].startswith("zh-")

        # 跨语言：让英文人物 Adrian 发一条韩语帖子 → 必须用韩语声线
        with db._lock, db.conn:  # noqa: SLF001 - 测试内构造跨度数据
            db.conn.execute(
                "INSERT INTO persona_posts (id, persona_id, content, media, tags, language,"
                " visibility, like_count, comment_count, created_at)"
                " VALUES ('pp_mixed_lang','p_adrian','오늘은 정말 피곤한 하루였다.','[]','[]','ko',"
                " 'public',0,0,'2026-11-06T00:00:00+00:00')"
            )
        r2 = client.get("/api/v3/persona-posts/pp_mixed_lang/speech?v=abc123")
        assert r2.status_code == 200
        assert r2.headers["x-language"] == "ko"
        assert r2.headers["x-voice"].startswith("ko-")
        # URL 带内容版本时才允许长缓存
        assert "immutable" in r2.headers.get("cache-control", "")
        assert voice.calls[-1][1].startswith("ko-")


def test_audio_cache_serves_from_disk_without_resynth(world_db):
    """预生成的朗读音频：第二次请求不再合成（毫秒级读取），且文件名内容寻址。"""
    db, tmp_path = world_db
    app, service, voice = _make_app(db, tmp_path)
    from app.v3.audio_cache import PersonaAudioCache

    cache = PersonaAudioCache(db, voice, str(tmp_path / "v3_audio"))
    app.state.v3_audio = cache
    with TestClient(app) as client:
        post = service.get_persona_post_row("pp_lin_27")
        persona = service.persona_by_id(str(post["persona_id"]))
        path, lang, voice_id = cache.path_for(post, persona)
        assert "pp_lin_27" in path and lang == "zh" and voice_id.startswith("zh-")

        first = client.get("/api/v3/persona-posts/pp_lin_27/speech")
        assert first.status_code == 200
        assert first.headers["x-audio-cached"] == "1"
        assert first.headers["x-voice"] == voice_id
        calls = len(voice.calls)

        second = client.get("/api/v3/persona-posts/pp_lin_27/speech")
        assert second.status_code == 200
        assert len(voice.calls) == calls, "命中磁盘缓存时不应再次合成"
        cached_files = list(Path(cache.cache_dir).glob("pp_lin_27-*"))
        assert cached_files and cached_files[0].stat().st_size > 0


def test_avatar_is_seeded_deterministic_svg(world_db):
    """头像由种子确定性生成：同 seed 同图、不同 seed 不同图、纯 SVG。"""
    db, tmp_path = world_db
    app, _service, _voice = _make_app(db, tmp_path)
    with TestClient(app) as client:
        a = client.get("/api/v3/avatar/persona-jiwoo.svg?g=female&r=east_asian")
        b = client.get("/api/v3/avatar/persona-jiwoo.svg?g=female&r=east_asian")
        c = client.get("/api/v3/avatar/persona-adrian.svg?g=male&r=white")
        assert a.status_code == 200 and c.status_code == 200
        assert a.headers["content-type"].startswith("image/svg+xml")
        assert "immutable" in a.headers.get("cache-control", "")
        assert a.text.startswith("<svg") and "</svg>" in a.text
        assert a.text == b.text, "同一 seed 必须生成同一张脸"
        assert a.text != c.text, "不同 seed 应该生成不同的脸"
        # 性别/地域参数会改变肤色/发型，从而改变结果
        d = client.get("/api/v3/avatar/persona-jiwoo.svg?g=male&r=black")
        assert d.text != a.text


def test_seed_data_has_no_mock_images():
    """Persona 头像不再依赖 mock 图片，全部走种子生成。"""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import importlib

    seed_data = importlib.import_module("app.v3.seed_data")
    assert seed_data.PERSONAS
    for persona in seed_data.PERSONAS:
        assert persona.get("avatar_seed"), persona["id"]
        assert "avatar" not in persona or not persona["avatar"]
        assert not str(persona.get("avatar", "")).startswith("/mock/")


def test_avatar_distinguishes_region_gender_age(world_db):
    """人种/性别/年龄三个维度都会改变生成结果。"""
    db, tmp_path = world_db
    app, _service, _voice = _make_app(db, tmp_path)
    with TestClient(app) as client:
        base = "/api/v3/avatar/p_demo.svg"
        asian_young = client.get(f"{base}?g=female&r=east_asian&a=22").text
        asian_mature = client.get(f"{base}?g=female&r=east_asian&a=41").text
        male_same = client.get(f"{base}?g=male&r=east_asian&a=22").text
        black_same = client.get(f"{base}?g=female&r=black&a=22").text

        # 年龄：同人同性别同地域，年龄档不同 → 结果不同（灰发/皱纹/眼镜概率）
        assert asian_young != asian_mature
        # 性别不同 → 发型/胡须/唇色不同
        assert asian_young != male_same
        # 人种（地域）不同 → 肤色/发色不同
        assert asian_young != black_same
        # 同参数可复现
        again = client.get(f"{base}?g=female&r=east_asian&a=22").text
        assert again == asian_young


def test_persona_avatar_url_encodes_seed_and_identity():
    from app.v3.avatar import persona_avatar_url

    url = persona_avatar_url(
        {"id": "p_jiwoo", "gender": "female", "country": "Korea", "age": 23}
    )
    assert url.startswith("/api/v3/avatar/p_jiwoo.svg")
    assert "g=female" in url and "r=east_asian" in url and "a=23" in url


def test_health_reports_world(world_db):
    db, tmp_path = world_db
    app, _service, _voice = _make_app(db, tmp_path)
    with TestClient(app) as client:
        data = client.get("/api/v3/health").json()
        # 测试 app 没有走 ensure_world，world 统计可能为空；但 personas 计数必须正确
        assert data["personas"] == 12


def test_translate_endpoint_still_works_for_feed(world_db):
    """信息流「翻译」复用 V2 翻译接口：mock LLM 下也要 200。"""
    db, tmp_path = world_db
    app, _service, _voice = _make_app(db, tmp_path)
    with TestClient(app) as client:
        headers = _register(client, "device_world_0002")
        response = client.post(
            "/api/v2/translate",
            headers=headers,
            json={"text": "Spent 40 minutes aligning two buttons.", "target": "zh"},
        )
        assert response.status_code == 200
        assert "translation" in response.json()
