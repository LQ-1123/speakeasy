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
        # 第四阶段：朗读用**角色自己**的声线（测试环境无 Fish → edge-<persona.voice_id>）
        assert "pp_lin_27" in path and lang == "zh"
        assert voice_id == f"edge-{persona['voice_id']}"

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


def test_avatar_options_defaults_are_valid_and_deterministic():
    from app.v3 import avatar_style

    persona = {
        "id": "p_jiwoo", "nickname": "Jiwoo", "gender": "female",
        "country": "Korea", "age": 23,
    }
    a = avatar_style.default_options(persona)
    b = avatar_style.default_options(persona)
    assert a == b, "同一人物必须得到同一组部件"
    assert a["top"][0] in avatar_style.TOP
    assert a["skinColor"][0] in avatar_style.SKIN
    assert a["clothing"][0] in avatar_style.CLOTHING
    # 不同性别/年龄 → 结果不同
    male = avatar_style.default_options({**persona, "gender": "male"})
    mature = avatar_style.default_options({**persona, "age": 40})
    assert a != male and a != mature


def test_api_persona_carries_avatar_options(world_db):
    """补齐侧写部件后，人物接口返回 avatar_style + avatar_options。"""
    import asyncio

    from app.v3 import avatar_style as avatar_style_mod

    db, tmp_path = world_db
    app, service, _voice = _make_app(db, tmp_path)
    rows = [dict(r) for r in db.conn.execute("SELECT * FROM personas").fetchall()]
    updated = asyncio.run(avatar_style_mod.ensure_avatar_options(db, rows, None))
    assert updated == 12

    with TestClient(app) as client:
        profile = client.get("/api/v3/personas/p_jiwoo").json()
        assert profile["avatar_style"] == "avataaars"
        options = profile["avatar_options"]
        assert isinstance(options, dict) and options.get("top")
        assert options["top"][0] in avatar_style_mod.TOP

        headers = _register(client, "device_avatar_0001")
        data = client.post("/api/v3/personas/p_jiwoo/friend", headers=headers).json()
        friend = db.get_friend(data["friend_id"], any_owner=True)
        assert friend["avatar_style"] == "avataaars"
        assert friend["avatar_options"]


def test_user_post_media_limited_to_mock_and_tags_cleaned(world_db):
    """用户动态只允许内置图片；标签去 #、去重、限量。"""
    db, tmp_path = world_db
    app, _service, _voice = _make_app(db, tmp_path)
    with TestClient(app) as client:
        headers = _register(client, "device_user_media_0001")

        ok = client.post(
            "/api/v3/posts",
            headers=headers,
            json={
                "content": "today was good",
                "visibility": "public",
                "media": ["/mock/photos/cafe.jpg", "/mock/photos/beach.jpg"],
                "tags": ["#咖啡", "咖啡", "", " 旅行 ", "旅行"],
            },
        )
        assert ok.status_code == 200
        post = ok.json()["post"]
        assert post["media"] == ["/mock/photos/cafe.jpg", "/mock/photos/beach.jpg"]
        assert post["tags"] == ["咖啡", "旅行"]

        for bad in (
            "https://example.com/cat.jpg",
            "data:image/png;base64,AAAA",
            "/media/abc.webp",
        ):
            res = client.post(
                "/api/v3/posts",
                headers=headers,
                json={"content": "nope", "media": [bad]},
            )
            assert res.status_code == 400, bad
            assert res.json()["detail"]

        # 标签上限 6 个
        many = client.post(
            "/api/v3/posts",
            headers=headers,
            json={"content": "tags", "tags": [f"t{i}" for i in range(9)]},
        )
        assert many.status_code == 200
        assert len(many.json()["post"]["tags"]) == 6


def test_user_image_upload_and_post(world_db):
    """上传真实图片 → /uploads/... → 动态可引用；非图片被拒。"""
    import io as _io

    from PIL import Image  # noqa: PLC0415

    db, tmp_path = world_db
    app, service, _voice = _make_app(db, tmp_path)

    buf = _io.BytesIO()
    Image.new("RGB", (640, 480), (30, 120, 200)).save(buf, "PNG")
    png = buf.getvalue()

    with TestClient(app) as client:
        headers = _register(client, "device_upload_0001")

        up = client.post(
            "/api/v3/media/upload",
            headers=headers,
            files={"file": ("photo.png", png, "image/png")},
        )
        assert up.status_code == 200, up.text
        data = up.json()
        assert data["url"].startswith("/uploads/") and data["url"].endswith(".webp")
        assert data["width"] == 640 and data["height"] == 480
        saved = Path(service.data_dir) / data["url"].lstrip("/")
        assert saved.exists()

        post = client.post(
            "/api/v3/posts",
            headers=headers,
            json={"content": "my own photo", "media": [data["url"]]},
        )
        assert post.status_code == 200, post.text
        assert post.json()["post"]["media"] == [data["url"]]

        bad = client.post(
            "/api/v3/media/upload",
            headers=headers,
            files={"file": ("note.txt", b"hello world", "text/plain")},
        )
        assert bad.status_code == 400


def test_profile_interests_saved_and_rank_discover(world_db):
    """兴趣写入 fact_sheet（清洗后），偶遇优先返回兴趣重叠的人物。"""
    db, tmp_path = world_db
    app, _service, _voice = _make_app(db, tmp_path)
    with TestClient(app) as client:
        headers = _register(client, "device_interest_0001")

        long_tag = "x" * 30
        res = client.put(
            "/api/v3/profile",
            headers=headers,
            json={
                "city": "上海",
                "job": "软件工程学生",
                "interests": ["咖啡", "#旅行", "咖啡", long_tag],
            },
        )
        assert res.status_code == 200, res.text
        profile = res.json()["profile"]
        fact = profile["fact_sheet"]
        assert fact["profile"]["city"] == "上海"
        assert fact["profile"]["major_or_job"] == "软件工程学生"
        # 去 #、去重、超长截断到 16 字
        assert fact["interests"] == ["咖啡", "旅行", "x" * 16]

        # 库里确实有人物带 coffee 兴趣 → 偶遇应优先返回兴趣重叠的人
        rows = db.conn.execute("SELECT interests FROM personas").fetchall()
        has_coffee = any("coffee" in str(r["interests"]).lower() for r in rows)
        if has_coffee:
            client.put(
                "/api/v3/profile", headers=headers, json={"interests": ["coffee"]}
            )
            found = client.get("/api/v3/discover/next", headers=headers).json()
            assert "match_tags" in found
            assert found["match_tags"], "有共同兴趣时应返回 match_tags"


def test_profile_gender_and_user_post_author_consistency(world_db):
    """性别写入资料；用户动态作者名跟随当前资料（不再出现 Traveler）。"""
    db, tmp_path = world_db
    app, _service, _voice = _make_app(db, tmp_path)
    with TestClient(app) as client:
        headers = _register(client, "device_identity_0001")

        created = client.post(
            "/api/v3/posts", headers=headers, json={"content": "身份一致性测试"}
        )
        assert created.status_code == 200

        res = client.put(
            "/api/v3/profile",
            headers=headers,
            json={"nickname": "淮安", "gender": "female", "interests": ["咖啡"]},
        )
        assert res.status_code == 200, res.text
        fact = res.json()["profile"]["fact_sheet"]
        assert fact["profile"]["gender"] == "female"

        feed = client.get("/api/v3/feed", headers=headers).json()
        mine = [it for it in feed["items"] if it["kind"] == "user_post"]
        assert mine, "应能看到自己发的动态"
        assert mine[0]["author"]["nickname"] == "淮安"

        # 非法性别值被清空
        res2 = client.put("/api/v3/profile", headers=headers, json={"gender": "xyz"})
        assert res2.json()["profile"]["fact_sheet"]["profile"]["gender"] == ""


def test_profile_language_levels_and_feed_filter(world_db):
    """资料保存 native/level/levels；feed 只返回在练语言的人物动态；每日生成语言跟随。"""
    import asyncio

    from app.v3.daily_world import generate_daily

    db, tmp_path = world_db
    app, _service, _voice = _make_app(db, tmp_path)
    with TestClient(app) as client:
        headers = _register(client, "device_lang_0001")

        res = client.put(
            "/api/v3/profile",
            headers=headers,
            json={
                "nickname": "淮安",
                "gender": "female",
                "native_language": "zh",
                "target_languages": ["en", "de"],
                "level": "能聊几句",
                "levels": {"en": "能聊几句", "de": "刚开始"},
                "interests": ["咖啡"],
            },
        )
        assert res.status_code == 200, res.text
        profile = res.json()["profile"]
        assert profile["native_language"] == "zh"
        fact = profile["fact_sheet"]
        assert fact["profile"]["gender"] == "female"
        assert fact["profile"]["level"] == "能聊几句"
        assert fact["language_profile"]["levels"] == {"en": "能聊几句", "de": "刚开始"}

        # feed：人物动态只保留 en/de
        feed = client.get("/api/v3/feed?limit=40", headers=headers).json()
        persona_langs = {
            it["author"]["language"]
            for it in feed["items"]
            if it["kind"] == "persona_post"
        }
        assert persona_langs <= {"en", "de"}, persona_langs

        # 每日生成：语言按在练语言轮换，水平写进 prompt（mock llm 走模板兜底）
        result = asyncio.run(
            generate_daily(db, None, day="2026-11-07", force=True, media_dir=str(tmp_path / "m"))
        )
        assert result["skipped"] is False
        rows = db.conn.execute(
            "SELECT language FROM personas WHERE lifecycle='guest'"
        ).fetchall()
        langs = {str(r["language"]) for r in rows}
        assert langs <= {"en", "de"}, langs
        assert "de" in langs


def test_persona_fish_voice_assignment_and_friend_sync(world_db):
    """每个 AI 人物分到**唯一**的 Fish 音色；好友行同步同一音色（第四阶段）。"""
    import asyncio

    from app.v3.seed_data import ensure_persona_voices

    db, tmp_path = world_db
    app, service, _voice = _make_app(db, tmp_path)  # noqa: F841 - 只用于建 app

    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(personas)")}
    assert "fish_voice_id" in cols

    # 离线注入音色池：不需要联网，也能验证「一人一色 + 同步」
    pool = [
        {"id": f"fish-{lang}-{gender}-{idx}", "languages": [lang], "tags": [gender]}
        for lang in ("en", "es", "de", "fr", "ja", "ko", "it", "pt", "zh")
        for gender in ("male", "female")
        for idx in range(3)
    ]
    assigned = asyncio.run(ensure_persona_voices(db, limit=40, pool=pool))
    assert assigned >= 12

    rows = db.conn.execute(
        "SELECT id, fish_voice_id FROM personas WHERE COALESCE(fish_voice_id,'') <> ''"
    ).fetchall()
    assert len(rows) >= 12
    voices = [str(r["fish_voice_id"]) for r in rows]
    assert len(set(voices)) == len(voices), "不允许两个人物共用同一个 Fish 音色"

    # 幂等：再跑一次不会改动任何已分配的音色
    again = asyncio.run(ensure_persona_voices(db, limit=40, pool=pool))
    assert again == 0
    assert [
        str(r["fish_voice_id"])
        for r in db.conn.execute(
            "SELECT fish_voice_id FROM personas ORDER BY id"
        ).fetchall()
    ] == [
        str(r["fish_voice_id"])
        for r in db.conn.execute(
            "SELECT fish_voice_id FROM personas ORDER BY id"
        ).fetchall()
    ]

    # 加好友：好友行必须继承人物音色（聊天与动态朗读才能一致）
    persona_id = str(rows[0]["id"])
    persona_voice = str(rows[0]["fish_voice_id"])
    result = service.add_friend("u_fish_voice_test", persona_id)
    friend = db.get_friend(result["friend_id"], any_owner=True)
    assert friend is not None
    assert friend["fish_voice_id"] == persona_voice


def test_speech_version_tracks_persona_voice(world_db):
    """feed 返回 speech_version；人物音色变化后同一帖子的 speech_version 必须变化。"""
    db, tmp_path = world_db
    app, _service, _voice = _make_app(db, tmp_path)
    with TestClient(app) as client:
        headers = _register(client, "device_speech_ver_0001")

        feed = client.get("/api/v3/feed?limit=10", headers=headers).json()
        posts = [it for it in feed["items"] if it["kind"] == "persona_post"]
        assert posts, "演示世界应有 AI 人物动态"
        post = posts[0]
        v1 = post["speech_version"]
        assert v1, "缺少 speech_version"

        persona_id = post["author"]["id"]
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                "UPDATE personas SET fish_voice_id='voice-B' WHERE id=?", (persona_id,)
            )
        # 模拟服务内部失效缓存（真实流程 assign_fish_voices 会做）
        _service._persona_cache = {"at": 0.0, "rows": []}
        _service._feed_cache = {"at": 0.0, "key": "", "items": []}

        feed2 = client.get("/api/v3/feed?limit=10", headers=headers).json()
        post2 = next(it for it in feed2["items"] if it["id"] == post["id"])
        assert post2["speech_version"] != v1

        # 偶遇接口也带 speech_version（没有最新帖时为空串）
        found = client.get("/api/v3/discover/next", headers=headers).json()
        assert "speech_version" in found


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


def test_friend_posts_persona_and_legacy_moments(world_db):
    """好友动态：人物好友返回 TA 的帖子；老好友回落到 V2 朋友圈。"""
    db, tmp_path = world_db
    app, service, _voice = _make_app(db, tmp_path)
    with TestClient(app) as client:
        headers = _register(client, "device_friend_posts_0001")
        uid = headers["X-User-Id"]

        pid = db.conn.execute(
            "SELECT id FROM personas WHERE status='active' LIMIT 1"
        ).fetchone()["id"]
        added = service.add_friend(uid, pid)
        fid = added["friend_id"]

        res = client.get(f"/api/v3/friends/{fid}/posts?limit=3", headers=headers)
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["source"] == "persona"
        assert data["items"], "人物好友应有动态"
        assert data["items"][0]["author"]["nickname"]

        # 老好友（没有 friendships 链接）回落到 moments
        db.create_friend(
            {
                "id": "legacy_friend_1",
                "name": "Old Friend",
                "owner_user_id": "local",
            }
        )
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                "INSERT INTO moments"
                " (id, friend_id, content, mood_tag, local_time_str, feed_date, is_replied, created_at)"
                " VALUES ('m_legacy_test','legacy_friend_1','旧好友的一条动态','', '', '', 0,"
                " '2026-01-01T00:00:00+00:00')"
            )
        res2 = client.get("/api/v3/friends/legacy_friend_1/posts?limit=3", headers=headers)
        assert res2.status_code == 200, res2.text
        data2 = res2.json()
        assert data2["source"] == "moments"
        assert data2["items"][0]["content"] == "旧好友的一条动态"
