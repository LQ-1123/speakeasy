"""图片库管理：统计 / 清理 / 搜图下载 / 按兴趣补图 测试。"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from app.admin import media_admin
from app.v2.db import DB
from app.v3.schema import ensure_schema


@pytest.fixture()
def db(tmp_path):
    database = DB(str(tmp_path / "media.db"))
    database.init()
    ensure_schema(database, seed=True)
    yield database
    database.close()


@pytest.fixture()
def media_root(tmp_path, monkeypatch):
    root = tmp_path / "media"
    root.mkdir()
    monkeypatch.setattr(media_admin, "media_dir", lambda: str(root))
    return root


def _add_file(root, name: str, size: int = 1024, age_days: float = 0.0) -> str:
    path = root / name
    path.write_bytes(b"x" * size)
    if age_days:
        old = path.stat().st_mtime - age_days * 86400
        os.utime(path, (old, old))
    return f"/media/{name}"


def _add_row(db, path: str, title: str = "demo") -> None:
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO media_library (external_id, source, path, title, tags, license,"
            " author, source_url, fetched_at, used_count) VALUES (?,?,?,?, '[]', 'CC0','A','u',"
            " '2026-01-01T00:00:00+00:00', 1)",
            (f"ext-{path}", "commons", path, title),
        )


def _add_post_with_media(db, path: str) -> None:
    with db._lock:  # noqa: SLF001
        pid = db.conn.execute("SELECT id FROM personas LIMIT 1").fetchone()[0]
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO persona_posts (id, persona_id, content, media, tags, language,"
            " visibility, like_count, comment_count, created_at, expires_at)"
            " VALUES ('pp_media_x', ?, 'demo', ?, '[]', 'en', 'public', 0, 0,"
            " '2026-01-01T00:00:00+00:00', '')",
            (pid, json.dumps([path])),
        )


def test_stats_counts_files_and_references(db, media_root):
    ref = _add_file(media_root, "ref.webp")
    _add_file(media_root, "orphan.webp")
    _add_row(db, ref)
    _add_post_with_media(db, ref)
    info = media_admin.stats(db)
    assert info["ok"] is True
    assert info["files"] == 2
    assert info["referenced_files"] == 1
    assert info["unreferenced_files"] == 1
    assert info["bytes"] == 2048


def test_prune_ttl_removes_old_unreferenced_only(db, media_root):
    ref = _add_file(media_root, "ref.webp")
    orphan = _add_file(media_root, "orphan.webp", age_days=10)
    _add_row(db, ref)
    _add_row(db, orphan)
    _add_post_with_media(db, ref)
    res = media_admin.prune(db, ttl_days=1)
    assert res["deleted_files"] == 1
    assert not (media_root / "orphan.webp").exists()
    assert (media_root / "ref.webp").exists()
    with db._lock:  # noqa: SLF001
        left = {r["path"] for r in db.conn.execute("SELECT path FROM media_library").fetchall()}
    assert orphan not in left and ref in left


def test_prune_quota_keeps_referenced(db, media_root):
    ref = _add_file(media_root, "ref.webp", age_days=100)
    old = _add_file(media_root, "old.webp", age_days=50)
    new = _add_file(media_root, "new.webp", age_days=1)
    _add_post_with_media(db, ref)
    res = media_admin.prune(db, ttl_days=3650, max_files=2, max_bytes=1024 * 1024)
    assert res["deleted_files"] >= 1
    assert (media_root / "ref.webp").exists()
    assert not (media_root / "old.webp").exists()
    assert (media_root / "new.webp").exists() or res["deleted_files"] == 2


def test_fetch_downloads_and_records(db, media_root, monkeypatch):
    async def fake_search(db_, query, *, persona_id="", limit=8):
        return {
            "ok": True,
            "items": [
                {
                    "external_id": "commons:1",
                    "source": "wikimedia",
                    "title": "Coffee cup on table",
                    "tags": ["coffee"],
                    "license": "CC0",
                    "author": "A",
                    "page_url": "https://example/1",
                    "thumb_url": "https://example/1.jpg",
                }
            ],
        }

    async def fake_download(client, item, media_dir_):
        path = os.path.join(media_dir_, "pic.webp")
        with open(path, "wb") as fh:
            fh.write(b"x" * 10)
        return "/media/pic.webp"

    monkeypatch.setattr(media_admin, "search", fake_search)
    monkeypatch.setattr(media_admin.media_mod, "download_image", fake_download)
    res = asyncio.run(media_admin.fetch(db, "coffee", limit=1))
    assert res["ok"] is True
    assert len(res["saved"]) == 1
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT external_id, path FROM media_library WHERE external_id='commons:1'"
        ).fetchone()
    assert row is not None and row["path"] == "/media/pic.webp"


def test_fill_creates_photo_posts(db, media_root, monkeypatch):
    async def fake_fetch(db_, query, *, persona_id="", limit=1, with_caption=False, llm=None):
        return {
            "ok": True,
            "saved": [
                {
                    "path": "/media/pic.webp",
                    "title": "Coffee cup",
                    "source": "commons",
                    "license": "CC0",
                    "author": "A",
                    "thumb_url": "",
                }
            ],
        }

    async def fake_post(db_, llm, persona, image):
        return {"persona_id": persona["id"], "content": "Coffee time", "image": image["path"]}

    monkeypatch.setattr(media_admin, "fetch", fake_fetch)
    monkeypatch.setattr(media_admin, "_post_photo", fake_post)
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute("UPDATE persona_posts SET media='[]'")
    res = asyncio.run(media_admin.fill(db, object(), with_caption=True))
    assert res["ok"] is True
    assert res["images_added"] >= 1
    assert res["posts_created"] >= 1


def test_list_library_marks_referenced_and_paginates(db, media_root):
    ref = _add_file(media_root, "ref.webp")
    unused = _add_file(media_root, "unused.webp")
    _add_row(db, ref, "Coffee ref")
    _add_row(db, unused, "Bike unused")
    _add_post_with_media(db, ref)

    page1 = media_admin.list_library(db, offset=0, limit=1)
    assert page1["ok"] is True
    assert page1["total"] == 2
    assert len(page1["items"]) == 1

    full = media_admin.list_library(db, offset=0, limit=10)
    by_path = {item["path"]: item for item in full["items"]}
    assert by_path[ref]["referenced"] is True
    assert by_path[unused]["referenced"] is False
    assert by_path[ref]["url"] == ref

    only_unused = media_admin.list_library(db, offset=0, limit=10, only_unused=True)
    assert [item["path"] for item in only_unused["items"]] == [unused]
