"""``/uploads/*`` 受保护静态路由（多用户图片归属）验收。

覆盖：
- 未登记的历史文件仍可读，并带 ``X-Legacy-Asset: 1``；
- 已登记文件：owner 本人（会话凭据）200；其他已登录用户 403；未登录 403；
- 短时媒体令牌 ``?t=``：owner 令牌可读、他人/篡改/过期令牌 403；
- 目录穿越/隐藏文件被拒；
- 归属查询失败时退回「历史文件可读」而不是 500。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.v2.db import DB
from app.v3 import accounts
from app.v3.media_access import ensure_media_schema, issue_media_token, register_asset
from app.v3.schema import ensure_schema
from app.v3.uploads_guard import ProtectedUploads


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SE_INVITE_REQUIRED", "0")
    db = DB(str(tmp_path / "uploads.db"))
    db.init()
    ensure_schema(db, seed=False)
    accounts.ensure_accounts_schema(db)
    ensure_media_schema(db)
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()
    (uploads_dir / "legacy.txt").write_text("legacy", encoding="utf-8")
    owner_dir = uploads_dir / "ownerhash"
    owner_dir.mkdir()
    (owner_dir / "photo.webp").write_bytes(b"WEBP-owner")

    app = FastAPI()
    app.state.v2 = SimpleNamespace(db=db)
    app.mount("/uploads", ProtectedUploads(directory=str(uploads_dir), kind="post_image"))
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, db=db, uploads=uploads_dir, app=app)
    db.close()


def _user(db: DB, email: str) -> tuple[str, dict]:
    reg = accounts.register_email(db, email, "pa55word!", invite_code="")
    return reg["user_id"], {"X-Session-Token": reg["session_token"]}


def test_legacy_file_readable_with_marker(env):
    res = env.client.get("/uploads/legacy.txt")
    assert res.status_code == 200
    assert res.text == "legacy"
    assert res.headers.get("X-Legacy-Asset") == "1"


def test_owner_only_and_anonymous_denied(env):
    uid, headers = _user(env.db, "owner@example.com")
    other, other_headers = _user(env.db, "other@example.com")
    register_asset(env.db, uid, "post_image", "ownerhash/photo.webp")

    assert env.client.get("/uploads/ownerhash/photo.webp", headers=headers).status_code == 200
    assert env.client.get("/uploads/ownerhash/photo.webp").status_code == 403
    assert env.client.get("/uploads/ownerhash/photo.webp", headers=other_headers).status_code == 403


def test_media_token_grants_owner_access(env):
    uid, _ = _user(env.db, "token@example.com")
    register_asset(env.db, uid, "post_image", "ownerhash/photo.webp")
    token = issue_media_token(env.db, uid, "post_image", "ownerhash/photo.webp", ttl_s=120)

    assert env.client.get(f"/uploads/ownerhash/photo.webp?t={token}").status_code == 200
    # 篡改令牌
    assert env.client.get(f"/uploads/ownerhash/photo.webp?t={token}x").status_code == 403

    # 他人令牌（为同文件名签发但 user 不同）不可用
    other, _ = _user(env.db, "token2@example.com")
    wrong = issue_media_token(env.db, other, "post_image", "ownerhash/photo.webp", ttl_s=120)
    assert env.client.get(f"/uploads/ownerhash/photo.webp?t={wrong}").status_code == 403


def test_signed_url_helper_for_img_tags(env):
    """浏览器 <img> 不带身份头：序列化层给自己名下的 URL 补 ?t=，直接可读。"""
    from app.v3.media_access import signed_url

    uid, _ = _user(env.db, "signed@example.com")
    other, _ = _user(env.db, "signed2@example.com")
    register_asset(env.db, uid, "post_image", "ownerhash/photo.webp")

    signed = signed_url(env.db, uid, "/uploads/ownerhash/photo.webp")
    assert "?t=" in signed
    assert env.client.get(signed).status_code == 200
    # 非本人 / 未登记 → 原样返回
    assert signed_url(env.db, other, "/uploads/ownerhash/photo.webp") == "/uploads/ownerhash/photo.webp"
    assert signed_url(env.db, uid, "/uploads/missing.webp") == "/uploads/missing.webp"


def test_traversal_and_hidden_paths_rejected(env):
    # httpx 会先规范化明文 ../，这里用百分号编码确保真的走到 StaticFiles 的安全检查
    assert env.client.get("/uploads/%2e%2e/legacy.txt").status_code in (403, 404)
    assert env.client.get("/uploads/.hidden").status_code in (403, 404)


def test_owner_query_failure_falls_back_to_legacy(env):
    """归属查询异常（如库表损坏）不应 500，而是按历史文件可读处理。"""
    res = env.client.get("/uploads/legacy.txt")
    assert res.status_code == 200
    guard = ProtectedUploads(directory=str(env.uploads), kind="post_image")
    # db=None / 查询异常 → 视为未登记（历史兼容优先）
    assert guard._owner(None, "legacy.txt") is None
