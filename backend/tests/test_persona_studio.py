"""角色工坊：列表/编辑/重捏/新建/删除/全量重建 测试。"""

from __future__ import annotations

from types import SimpleNamespace

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin import persona_studio as ps
from app.admin.router import router as admin_router
from app.v2.db import DB
from app.v3.schema import ensure_schema


@pytest.fixture()
def db(tmp_path):
    database = DB(str(tmp_path / "studio.db"))
    database.init()
    ensure_schema(database, seed=True)
    yield database
    database.close()


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.delenv("SE_ADMIN_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(admin_router)
    app.state.v2 = SimpleNamespace(db=db)
    app.state.v3 = None
    app.state.conversation_store = None
    with TestClient(app) as http:
        yield http


def _first_persona(db):
    rows = ps.list_personas(db, limit=5)
    assert rows, "种子数据库里应该有人物"
    return rows[0]["id"]


def test_list_and_detail(db):
    rows = ps.list_personas(db)
    assert len(rows) >= 1
    pid = rows[0]["id"]
    detail = ps.persona_detail(db, pid)
    assert detail and detail["id"] == pid
    assert isinstance(detail["interests"], list)
    assert isinstance(detail["speech_dna"], dict)
    assert detail["_counts"]["examples"] >= 0


def test_update_persona_fields(db):
    pid = _first_persona(db)
    res = ps.update_persona(
        db,
        pid,
        {
            "nickname": "测试小姐姐",
            "gender": "female",
            "age": 28,
            "city": "Tokyo",
            "interests": ["coffee", "anime"],
            "bio": "hello from admin",
            "fish_voice_id": "voice-test-1",
        },
    )
    assert res["ok"] is True
    detail = ps.persona_detail(db, pid)
    assert detail["nickname"] == "测试小姐姐"
    assert detail["age"] == 28
    assert detail["interests"] == ["coffee", "anime"]
    assert detail["fish_voice_id"] == "voice-test-1"


def test_update_rejects_empty_nickname(db):
    pid = _first_persona(db)
    res = ps.update_persona(db, pid, {"nickname": "  "})
    assert res["ok"] is False


def test_reroll_changes_profile(db):
    pid = _first_persona(db)
    before = ps.persona_detail(db, pid)
    res = ps.reroll_persona(db, pid, regen_voice=False)
    assert res["ok"] is True
    after = ps.persona_detail(db, pid)
    assert after["id"] == pid
    # 人设至少有一项变化（昵称/年龄/城市）
    assert (
        after["nickname"] != before["nickname"]
        or after["age"] != before["age"]
        or after["city"] != before["city"]
    )


def test_create_and_delete_persona(db):
    res = ps.create_persona(
        db,
        None,
        {
            "nickname": "QA Bot",
            "gender": "male",
            "language": "en",
            "country": "US",
            "city": "Austin",
            "age": 30,
            "bio": "test persona",
            "interests": ["testing"],
            "fish_voice_id": "voice-qa",
        },
        posts=["first post", "second post"],
    )
    assert res["ok"] is True
    pid = res["persona"]["id"]
    assert any(row["id"] == pid for row in ps.list_personas(db))
    assert res["persona"]["_counts"]["posts"] == 2

    deleted = ps.delete_persona(db, None, pid, backup=True)
    assert deleted["ok"] is True
    assert deleted["backup"]
    assert not ps.persona_detail(db, pid)


def test_rebuild_reroll(db, tmp_path):
    before = ps.rebuild_preview(db)
    res = asyncio.run(ps.rebuild_all(db, None, mode="reroll"))
    assert res["ok"] is True
    assert res["rerolled"] == before["persona_count"]
    assert res["backup"]
    after = ps.rebuild_preview(db)
    assert after["persona_count"] == before["persona_count"]


def test_rebuild_recreate_requires_reset_and_works(db):
    before = ps.rebuild_preview(db)
    refused = asyncio.run(ps.rebuild_all(db, None, mode="recreate", count=3))
    assert refused["ok"] is False

    res = asyncio.run(ps.rebuild_all(db, None, mode="recreate", count=3, confirm="RESET"))
    assert res["ok"] is True
    assert res["created"] == 3
    assert res["backup"]
    preview = ps.rebuild_preview(db)
    assert preview["persona_count"] == 3
    assert before["counts"]["personas"] >= 1
    # 每个新角色至少 1 条动态
    assert preview["counts"]["persona_posts"] >= 3


def test_admin_persona_endpoints(client, db):
    data = client.get("/api/admin/personas").json()
    assert data["ok"] is True
    assert data["personas"]
    pid = data["personas"][0]["id"]

    detail = client.get(f"/api/admin/personas/{pid}").json()
    assert detail["ok"] is True

    updated = client.put(
        f"/api/admin/personas/{pid}", json={"nickname": "接口改名", "age": 31}
    ).json()
    assert updated["ok"] is True
    assert updated["persona"]["nickname"] == "接口改名"

    rerolled = client.post(f"/api/admin/personas/{pid}/reroll", json={}).json()
    assert rerolled["ok"] is True

    preview = client.get("/api/admin/personas/rebuild-preview").json()
    assert preview["ok"] is True and preview["persona_count"] >= 1

    users = client.get("/api/admin/personas/users").json()
    assert users["ok"] is True

    created = client.post(
        "/api/admin/personas",
        json={"nickname": "接口新建", "gender": "female", "language": "zh", "interests": ["a"]},
    ).json()
    assert created["ok"] is True
    new_id = created["persona"]["id"]
    deleted = client.delete(f"/api/admin/personas/{new_id}").json()
    assert deleted["ok"] is True


def test_update_voice_by_pool_name(db, monkeypatch):
    from app.admin import voice_pool as vpm

    monkeypatch.setattr(
        vpm,
        "load_pool",
        lambda: [
            {
                "reference_id": "voice-abc-1234567890",
                "name": "女大学生",
                "gender": "female",
                "language": "zh",
                "source": "manual",
                "added_at": "",
            }
        ],
    )
    pid = _first_persona(db)
    res = ps.update_persona(db, pid, {"fish_voice_id": "女大学生"})
    assert res["ok"] is True
    assert res["persona"]["fish_voice_id"] == "voice-abc-1234567890"
    assert res["persona"]["fish_voice_name"] == "女大学生"

    bad = ps.update_persona(db, pid, {"fish_voice_id": "不存在的名字"})
    assert bad["ok"] is False
    assert "找不到音色" in bad["issues"][0]


def test_redistribute_voices_balanced(db, monkeypatch):
    from app.admin import voice_pool as vpm

    fake_pool = [
        {"reference_id": "vv-1", "name": "A", "gender": "female", "language": "en"},
        {"reference_id": "vv-2", "name": "B", "gender": "male", "language": "en"},
        {"reference_id": "vv-3", "name": "C", "gender": "female", "language": "zh"},
        {"reference_id": "vv-4", "name": "D", "gender": "male", "language": "zh"},
    ]
    monkeypatch.setattr(vpm, "load_pool", lambda: fake_pool)
    res = ps.redistribute_voices(db, None)
    assert res["ok"] is True
    assert res["updated"] >= 1
    counts = {item["reference_id"]: item["count"] for item in res["distribution"]}
    # 至少用到了两个不同音色，且分布不会全压在一个人身上
    assert len([c for c in counts.values() if c > 0]) >= 2
    assert max(counts.values()) <= max(1, res["updated"] // 2 + 1)


def test_regenerate_posts_preserves_authors_and_existing_posts(db):
    before = [tuple(row) for row in db.conn.execute("SELECT * FROM persona_posts ORDER BY id")]
    res = ps.regenerate_posts(db, None, per_persona=4)
    assert res["ok"] is True
    assert res["personas"] >= 1
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT persona_id, COUNT(*) AS n FROM persona_posts GROUP BY persona_id"
        ).fetchall()
    assert rows
    assert res["posts"] == 0
    assert res["reason"] == "source_grounded_generation_required"
    assert [tuple(row) for row in db.conn.execute("SELECT * FROM persona_posts ORDER BY id")] == before


def test_post_media_attached_from_local_library(db, tmp_path, monkeypatch):
    media_root = tmp_path / "media"
    media_root.mkdir()
    (media_root / "demo.webp").write_bytes(b"RIFF0000WEBP")
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO media_library (external_id, source, path, title, tags, license,"
            " author, source_url, fetched_at, used_count)"
            " VALUES ('x1','commons','/media/demo.webp','Espresso Machine, Cafe','[]','CC0','A','u','2026-01-01',1)"
        )
    bank = ps._media_bank(db, data_dir=str(media_root))
    assert len(bank) == 1 and bank[0]["path"] == "/media/demo.webp"

    monkeypatch.setattr(ps, "_media_bank", lambda *a, **k: bank)
    monkeypatch.setattr(ps, "_post_bank", lambda language: ["Coffee by the window, slow morning."])
    res = ps.regenerate_posts(db, None, per_persona=3)
    assert res["ok"] is True
    assert res["posts_with_media"] >= 1


def test_fallback_persona_alternates_gender():
    from app.v3 import daily_world as dw

    assert dw._fallback_persona(0, "2026-01-01", "de")["gender"] == "female"
    assert dw._fallback_persona(1, "2026-01-01", "de")["gender"] == "male"
    assert dw._fallback_persona(0, "2026-01-01", "en", gender="male")["gender"] == "male"


def test_rebalance_genders(db):
    res = ps.rebalance_genders(db, None)
    assert res["ok"] is True
    assert res["total"] >= 2
    assert abs(int(res["female"]) - int(res["male"])) <= 1
    assert int(res["changed"]) > 0


def test_dedupe_nicknames(db):
    with db._lock, db.conn:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT id FROM personas WHERE status='active' ORDER BY id LIMIT 2"
        ).fetchall()
        assert len(rows) >= 2
        db.conn.execute("UPDATE personas SET nickname='SameName' WHERE id=?", (rows[0][0],))
        db.conn.execute("UPDATE personas SET nickname='SameName' WHERE id=?", (rows[1][0],))
    changed, _samples = ps._dedupe_nicknames(db)
    assert changed >= 1
    with db._lock:  # noqa: SLF001
        left = db.conn.execute(
            "SELECT COUNT(*) FROM personas WHERE nickname='SameName'"
        ).fetchone()[0]
    assert int(left) == 1


def _seed_media_post(db, tmp_path):
    import json

    media_root = tmp_path / "media"
    media_root.mkdir(exist_ok=True)
    (media_root / "m1.webp").write_bytes(b"x")
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT OR REPLACE INTO media_library (external_id, source, path, title, tags,"
            " license, author, source_url, fetched_at, used_count)"
            " VALUES ('m1','commons','/media/m1.webp','Espresso Machine, Cafe','[]','CC0','A','u','2026-01-01',1)"
        )
        pid = db.conn.execute("SELECT id FROM personas LIMIT 1").fetchone()[0]
        db.conn.execute(
            "INSERT INTO persona_posts (id, persona_id, content, media, tags, language,"
            " visibility, like_count, comment_count, created_at, expires_at, post_kind, alt_text)"
            " VALUES ('pp_media_test', ?, 'unrelated text', ?, '[]', 'en', 'public', 0, 0,"
            " '2099-01-01T00:00:00+00:00', '', 'life_photo', '')",
            (pid, json.dumps(["/media/m1.webp"])),
        )
    return pid


def test_rematch_media_rewrites_caption(db, tmp_path, monkeypatch):
    _seed_media_post(db, tmp_path)

    async def fake_caption(llm, persona, image):
        return "Coffee and a quiet corner, right where I needed it."

    monkeypatch.setattr(ps, "_llm_caption", fake_caption)
    res = asyncio.run(ps.rematch_media(db, object(), limit=5))
    assert res["ok"] is True
    assert res["fixed"] >= 1
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT content, source_ref FROM persona_posts WHERE id='pp_media_test'"
        ).fetchone()
    assert "Coffee" in row["content"]
    import json

    assert json.loads(row["source_ref"] or "{}").get("caption") == "llm"


def test_rematch_media_removes_image_when_llm_fails(db, tmp_path, monkeypatch):
    _seed_media_post(db, tmp_path)

    async def empty_caption(llm, persona, image):
        return ""

    monkeypatch.setattr(ps, "_llm_caption", empty_caption)
    res = asyncio.run(ps.rematch_media(db, object(), limit=5))
    assert res["ok"] is True
    assert res["removed"] >= 1
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT media, post_kind FROM persona_posts WHERE id='pp_media_test'"
        ).fetchone()
    assert row["media"] == "[]"
