"""P3 动态待发队列 + 图片上传 验收。

覆盖的硬约束（对应 task-7 / Lead 补充契约）：
1. **发布路径复用**：``publish-now`` 写 ``persona_posts`` 时走
   ``event_posts._insert_post``（同一条路），所以 ``visibility='public'``、
   ``like_count/comment_count=0``、``published_at``、``source_event_id``
   这些字段一个都不缺；测试直接断言这些列。
2. **发布幂等**：同一个队列条目重复 ``publish-now`` 只产生一条动态，第二次
   返回 ``already=true`` 与同一个 ``post_id``。
3. **已发布不可编辑**：``PUT`` 已发布条目 → ``{ok:false, issues:["已发布，不能编辑"]}``。
4. **取消是软删除**：``DELETE`` 只把 ``status`` 改 ``cancelled``，行还在（留痕）。
5. **图片上传**：只收图片、服务端重命名（防路径穿越）、落 ``media_library``。

不联网、不启动 lifespan：最小 FastAPI app + 临时 SQLite。
"""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.admin import auth, media_admin
from app.admin.router import router as admin_router
from app.v2.db import DB
from app.v3.schema import ensure_schema
from app.v3.service import V3Service


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture()
def db(tmp_path):
    database = DB(str(tmp_path / "admin_posts.db"))
    database.init()
    ensure_schema(database, seed=True)
    yield database
    database.close()


@pytest.fixture()
def client(db, monkeypatch):
    monkeypatch.delenv("SE_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SE_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("SE_ADMIN_SESSION_SECRET", raising=False)

    app = FastAPI()
    app.include_router(admin_router, dependencies=[Depends(auth.require_admin)])
    app.state.v2 = SimpleNamespace(db=db)
    app.state.v3 = V3Service(db, SimpleNamespace(llm=None))
    app.state.conversation_store = None
    with TestClient(app) as http:
        yield http


@pytest.fixture()
def media_root(tmp_path, monkeypatch):
    root = tmp_path / "media"
    root.mkdir()
    monkeypatch.setattr(media_admin, "media_dir", lambda: str(root))
    return root


def _personas(db, limit: int = 2) -> list[dict]:
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT id, nickname FROM personas WHERE status='active' ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def _post_row(db, post_id: str) -> dict:
    with db._lock:  # noqa: SLF001
        row = db.conn.execute("SELECT * FROM persona_posts WHERE id=?", (post_id,)).fetchone()
    return dict(row) if row is not None else {}


def _queue_row(db, queue_id: str) -> dict:
    with db._lock:  # noqa: SLF001
        row = db.conn.execute("SELECT * FROM post_queue WHERE id=?", (queue_id,)).fetchone()
    return dict(row) if row is not None else {}


def _count_posts_with_source(db, source_event_id: str, persona_id: str) -> int:
    with db._lock:  # noqa: SLF001
        return int(
            db.conn.execute(
                "SELECT COUNT(*) AS n FROM persona_posts"
                " WHERE source_event_id=? AND persona_id=?",
                (source_event_id, persona_id),
            ).fetchone()["n"]
        )


def _make_png(width: int = 8, height: int = 8) -> bytes:
    from PIL import Image  # noqa: PLC0415

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# 待发队列：列表 / 创建 / 编辑
# --------------------------------------------------------------------------- #
def test_create_queued_and_list_pending(client, db):
    persona = _personas(db, 1)[0]
    created = client.post(
        "/api/admin/posts",
        json={
            "persona_id": persona["id"],
            "content": "明天的动态：去河边走走",
            "media": ["/media/a.webp"],
            "publish_at": "2026-12-03T09:00:00+00:00",
        },
    ).json()
    assert created["ok"] is True and created["published"] is False
    queue_id = created["id"]
    assert created["item"]["status"] == "queued"
    assert created["item"]["persona_nickname"] == persona["nickname"]
    assert created["item"]["media"] == ["/media/a.webp"]

    pending = client.get("/api/admin/posts/pending").json()
    assert pending["ok"] is True and pending["total"] == 1
    assert pending["items"][0]["id"] == queue_id
    # 只返回 queued
    assert {row["status"] for row in pending["items"]} == {"queued"}

    personas = client.get("/api/admin/posts/personas").json()
    assert personas["ok"] is True
    assert persona["id"] in [row["id"] for row in personas["items"]]


def test_create_post_validation(client, db):
    persona = _personas(db, 1)[0]
    # 参数缺失 → 400
    assert client.post("/api/admin/posts", json={"content": "x"}).status_code == 400
    # 业务校验 → 200 + ok=False
    unknown = client.post(
        "/api/admin/posts", json={"persona_id": "p_nope", "content": "x"}
    ).json()
    assert unknown["ok"] is False and unknown["issues"] == ["角色不存在"]

    too_long = client.post(
        "/api/admin/posts", json={"persona_id": persona["id"], "content": "字" * 401}
    ).json()
    assert too_long["ok"] is False
    assert "超过" in too_long["issues"][0]

    empty = client.post("/api/admin/posts", json={"persona_id": persona["id"]}).json()
    assert empty["ok"] is False and empty["issues"] == ["正文与图片不能同时为空"]


def test_edit_queue_is_partial_and_only_queued(client, db):
    persona = _personas(db, 1)[0]
    queue_id = client.post(
        "/api/admin/posts",
        json={
            "persona_id": persona["id"],
            "content": "原始正文",
            "media": ["/media/keep.webp"],
            "publish_at": "2026-12-03T09:00:00+00:00",
        },
    ).json()["id"]

    updated = client.put(
        f"/api/admin/posts/{queue_id}", json={"content": "只改正文"}
    ).json()
    assert updated["ok"] is True
    assert updated["item"]["content"] == "只改正文"
    # 没传的键保持原值（PUT 按 patch 语义处理）
    assert updated["item"]["media"] == ["/media/keep.webp"]
    assert updated["item"]["publish_at"] == "2026-12-03T09:00:00+00:00"

    published = client.post(f"/api/admin/posts/{queue_id}/publish-now").json()
    assert published["ok"] is True

    rejected = client.put(f"/api/admin/posts/{queue_id}", json={"content": "改不了"}).json()
    assert rejected["ok"] is False
    assert rejected["issues"] == ["已发布，不能编辑"]
    assert client.put("/api/admin/posts/pq_nope", json={"content": "x"}).json()["ok"] is False


def test_cancel_is_soft_and_idempotent(client, db):
    persona = _personas(db, 1)[0]
    queue_id = client.post(
        "/api/admin/posts", json={"persona_id": persona["id"], "content": "要取消的动态"}
    ).json()["id"]

    cancelled = client.delete(f"/api/admin/posts/{queue_id}").json()
    assert cancelled["ok"] is True and cancelled["cancelled"] is True
    row = _queue_row(db, queue_id)
    assert row, "取消是软删除，行必须还在"
    assert row["status"] == "cancelled"

    again = client.delete(f"/api/admin/posts/{queue_id}").json()
    assert again["ok"] is True and again.get("already") is True

    # 已取消不能发布
    publish = client.post(f"/api/admin/posts/{queue_id}/publish-now").json()
    assert publish["ok"] is False and publish["issues"] == ["已取消，不能发布"]
    # 已取消不显示在待发列表
    assert client.get("/api/admin/posts/pending").json()["total"] == 0


# --------------------------------------------------------------------------- #
# 发布：复用 event_posts 路径 + 幂等
# --------------------------------------------------------------------------- #
def test_publish_now_writes_through_existing_path(client, db):
    persona = _personas(db, 1)[0]
    created = client.post(
        "/api/admin/posts",
        json={"persona_id": persona["id"], "content": "立即发布的一条", "publish_now": True},
    ).json()
    assert created["ok"] is True and created["published"] is True
    post = created["post"]
    post_id = post["id"]
    assert post_id.startswith("pp_")

    row = _post_row(db, post_id)
    # 复用 event_posts._insert_post 维护的字段，一个都不能少
    assert row["visibility"] == "public"
    assert int(row["like_count"]) == 0
    assert int(row["comment_count"]) == 0
    assert row["published_at"]
    assert row["created_at"]
    assert row["source_event_id"]
    assert row["post_kind"] == "life_text"
    assert row["post_type"] == "life_event"

    published = client.get(
        "/api/admin/posts/published", params={"persona_id": persona["id"]}
    ).json()
    assert published["total"] >= 1
    assert post_id in [item["id"] for item in published["items"]]


def test_queue_publish_now_is_idempotent(client, db):
    persona = _personas(db, 1)[0]
    queue_id = client.post(
        "/api/admin/posts",
        json={"persona_id": persona["id"], "content": "排队后立即发"},
    ).json()["id"]

    first = client.post(f"/api/admin/posts/{queue_id}/publish-now").json()
    assert first["ok"] is True and first["published"] is True
    post_id = first["post_id"]
    assert post_id

    row = _queue_row(db, queue_id)
    assert row["status"] == "published"
    assert row["published_post_id"] == post_id

    # 重复点「立即发布」：不新增动态，返回同一个 post_id
    second = client.post(f"/api/admin/posts/{queue_id}/publish-now").json()
    assert second["ok"] is True and second.get("already") is True
    assert second["post_id"] == post_id
    assert _count_posts_with_source(db, f"queue:{queue_id}", persona["id"]) == 1


def test_publish_with_media_records_media_column(client, db, media_root):
    persona = _personas(db, 1)[0]
    created = client.post(
        "/api/admin/posts",
        json={
            "persona_id": persona["id"],
            "content": "配图动态",
            "media": ["/media/pic.webp"],
            "publish_now": True,
        },
    ).json()
    assert created["ok"] is True
    post_id = created["post"]["id"]
    row = _post_row(db, post_id)
    assert json.loads(row["media"]) == ["/media/pic.webp"]
    assert row["post_kind"] == "life_photo"
    assert created["post"]["media"] == ["/media/pic.webp"]


def test_overview_pending_count_uses_queue(client, db):
    """P3 接线：总览 ``today.posts_pending`` 改为真查 ``post_queue``（不再恒为 0）。"""
    persona = _personas(db, 1)[0]
    overview = client.get("/api/admin/overview").json()
    assert overview["today"]["posts_pending"] == 0

    queue_id = client.post(
        "/api/admin/posts", json={"persona_id": persona["id"], "content": "待发的"}
    ).json()["id"]
    assert client.get("/api/admin/overview").json()["today"]["posts_pending"] == 1

    client.delete(f"/api/admin/posts/{queue_id}")
    assert client.get("/api/admin/overview").json()["today"]["posts_pending"] == 0


# --------------------------------------------------------------------------- #
# 图片上传
# --------------------------------------------------------------------------- #
def test_media_upload_saves_renamed_file_and_library_row(client, db, media_root):
    persona = _personas(db, 1)[0]
    data = _make_png()
    response = client.post(
        "/api/admin/media/upload",
        files={"file": ("../../evil name.png", data, "image/png")},
        data={"persona_id": persona["id"]},
    ).json()
    assert response["ok"] is True
    assert response["size"] == len(data)
    assert response["path"].startswith("/media/upload_")
    assert response["url"] == response["path"]
    # 服务端重命名：原始文件名（含路径）不参与落盘路径
    assert "evil" not in response["path"] and ".." not in response["path"]
    saved_name = response["path"].rsplit("/", 1)[-1]
    assert (media_root / saved_name).is_file()
    assert (media_root / saved_name).read_bytes() == data

    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT title, persona_id, source FROM media_library WHERE path=?",
            (response["path"],),
        ).fetchone()
    assert row is not None
    assert row["source"] == "upload"
    assert row["persona_id"] == persona["id"]
    assert row["title"] == "evil name.png"  # 原名只作展示 title（已去掉目录）


def test_media_upload_rejects_non_image_and_oversize(client, db, media_root, monkeypatch):
    # mime 不是图片
    wrong_type = client.post(
        "/api/admin/media/upload",
        files={"file": ("note.txt", b"hello", "text/plain")},
    ).json()
    assert wrong_type["ok"] is False

    # mime 伪装成图片，但内容不是有效图片
    fake_image = client.post(
        "/api/admin/media/upload",
        files={"file": ("fake.png", b"not really a png", "image/png")},
    ).json()
    assert fake_image["ok"] is False
    assert "不是有效图片" in fake_image["issues"][0]

    # 超过上限（复用 media_admin.limits()["download_max_bytes"]）
    monkeypatch.setattr(
        media_admin,
        "limits",
        lambda: {"max_bytes": 10, "max_files": 10, "ttl_days": 1, "download_max_bytes": 64},
    )
    oversize = client.post(
        "/api/admin/media/upload",
        files={"file": ("big.png", _make_png(64, 64), "image/png")},
    ).json()
    assert oversize["ok"] is False
    assert "超过上限" in oversize["issues"][0]


def test_media_upload_validates_persona(client, db, media_root):
    bad_persona = client.post(
        "/api/admin/media/upload",
        files={"file": ("ok.png", _make_png(), "image/png")},
        data={"persona_id": "p_nope"},
    ).json()
    assert bad_persona["ok"] is False
    assert bad_persona["issues"] == ["角色不存在"]


def test_media_upload_requires_db(client, db, media_root, monkeypatch):
    """数据库未就绪 → 200 + ok=False（项目既有降级约定，不抛 500）。"""
    app = FastAPI()
    app.include_router(admin_router)
    app.state.v2 = SimpleNamespace(db=None)
    app.state.v3 = None
    app.state.conversation_store = None
    with TestClient(app) as http:
        response = http.post(
            "/api/admin/media/upload",
            files={"file": ("ok.png", _make_png(), "image/png")},
        ).json()
    assert response["ok"] is False
    assert response["issues"] == ["数据库未就绪"]
