"""邮箱账号体系测试：注册 / 登录 / 邮箱验证 / 会话撤销 / 找回密码 / 设备身份绑定 / Cookie+CSRF / 分龄能力。

覆盖方案「第一期 · 账号体系」的验收点：
- 邀请码准入、邮箱唯一、密码强度、Argon2id/scrypt 双格式校验；
- 会话凭据可撤销，改密/找回密码撤销全部会话；
- 旧设备身份绑定邮箱后保留数据，且设备身份路径立即失效（401，不再签发）；
- Web Cookie 会话 + CSRF 双提交；
- 分龄能力（广告/亲密关系/青少年门禁）随核验结论变化。
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
from app.v3 import accounts  # noqa: E402
from app.v3.accounts_router import router as accounts_router  # noqa: E402
from app.v3.router import router as v3_router  # noqa: E402
from app.v3.schema import ensure_schema  # noqa: E402
from app.v3.service import V3Service  # noqa: E402

INVITE = "invite_test_0001"


def _make_app(tmp_path: Path):
    db = DB(str(tmp_path / "auth.db"))
    db.init()
    ensure_schema(db)
    accounts.ensure_accounts_schema(db)
    service = V3Service(
        db, SimpleNamespace(llm=SimpleNamespace(uses_mock=True)), str(tmp_path)
    )
    service.rng = random.Random(7)
    services = SimpleNamespace(
        db=db,
        onboarding_pending=False,
        followup_inflight=set(),
        spawn_bg=lambda coro: (coro.close(), None)[1],
    )
    app = FastAPI()
    app.state.v2 = services
    app.state.v3 = service
    app.include_router(v2_router)
    app.include_router(v3_router)
    app.include_router(accounts_router)
    return app, db


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("SE_MAIL_DEV", "1")
    monkeypatch.setenv("SE_INVITE_REQUIRED", "1")
    monkeypatch.setenv("SE_AGE_GATE", "0")
    monkeypatch.delenv("SE_REQUIRE_AUTH", raising=False)
    app, db = _make_app(tmp_path)
    accounts.create_invite_code(db, INVITE, max_uses=10)
    with TestClient(app) as client:
        yield client, db
    db.close()


def _register(client, email="a@example.com", password="pa55word!", code=INVITE, device_key=""):
    return client.post(
        "/api/v3/auth/register/email",
        json={
            "email": email,
            "password": password,
            "invite_code": code,
            "display_name": "Alice",
            "device_key": device_key,
        },
    )


def _h(token: str) -> dict:
    return {"X-Session-Token": token}


# --------------------------------------------------------------------------- #
# 注册 / 邀请码 / 邮箱验证
# --------------------------------------------------------------------------- #
def test_register_requires_valid_invite(api):
    client, db = api
    no_invite = client.post(
        "/api/v3/auth/register/email",
        json={"email": "a@example.com", "password": "pa55word!"},
    )
    assert no_invite.status_code == 403

    bad = _register(client, code="nope-nope")
    assert bad.status_code == 403

    ok = _register(client)
    assert ok.status_code == 200, ok.text
    data = ok.json()
    assert data["user_id"].startswith("u_")
    assert data["email"] == "a@example.com"
    assert data["email_verified"] is False
    assert data["session_token"]
    assert data["password_algo"] in ("argon2id", "scrypt")
    # dev 模式可直接拿到邮箱验证令牌
    assert data["dev_verification_token"]

    # 同一邮箱再注册 → 409
    accounts.create_invite_code(db, "invite_test_0002")
    dup = _register(client, code="invite_test_0002")
    assert dup.status_code == 409


def test_verify_email_flips_capability(api):
    client, db = api
    reg = _register(client).json()
    token = reg["dev_verification_token"]
    assert client.post("/api/v3/auth/verify-email", json={"token": token}).status_code == 200
    caps = client.get("/api/v3/auth/capabilities", headers=_h(reg["session_token"])).json()
    assert caps["account"]["email_verified"] is True
    assert caps["account"]["type"] == "email"
    # 令牌一次性
    assert client.post("/api/v3/auth/verify-email", json={"token": token}).status_code == 400


# --------------------------------------------------------------------------- #
# 登录 / 会话撤销 / 频率限制
# --------------------------------------------------------------------------- #
def test_login_and_session_revocation(api):
    client, db = api
    reg = _register(client).json()
    # 先记录注册会话，再登录，避免撤销到当前会话
    reg_sessions = client.get(
        "/api/v3/auth/sessions", headers=_h(reg["session_token"])
    ).json()["items"]
    assert len(reg_sessions) == 1
    register_session_id = reg_sessions[0]["session_id"]
    client.cookies.clear()

    bad = client.post(
        "/api/v3/auth/login", json={"email": "a@example.com", "password": "wrong-pass"}
    )
    assert bad.status_code == 401

    login = client.post(
        "/api/v3/auth/login", json={"email": "a@example.com", "password": "pa55word!"}
    )
    assert login.status_code == 200, login.text
    token = login.json()["session_token"]
    assert client.get("/api/v3/me", headers=_h(token)).status_code == 200

    sessions = client.get("/api/v3/auth/sessions", headers=_h(token)).json()["items"]
    assert len(sessions) == 2  # 注册会话 + 登录会话

    # 撤销注册会话：只影响那一个
    revoked = client.delete(
        f"/api/v3/auth/sessions/{register_session_id}", headers=_h(token)
    )
    assert revoked.status_code == 200
    assert client.get("/api/v3/me", headers=_h(reg["session_token"])).status_code == 401

    # 退出登录撤销当前会话
    assert client.post("/api/v3/auth/logout", headers=_h(token)).status_code == 200
    assert client.get("/api/v3/me", headers=_h(token)).status_code == 401


def test_login_rate_limit(api):
    client, _ = api
    _register(client).json()
    client.cookies.clear()
    accounts.clear_failures()
    for _ in range(5):
        r = client.post(
            "/api/v3/auth/login", json={"email": "a@example.com", "password": "bad-pass"}
        )
        assert r.status_code == 401
    limited = client.post(
        "/api/v3/auth/login", json={"email": "a@example.com", "password": "pa55word!"}
    )
    assert limited.status_code == 401
    assert "次数" in limited.json()["detail"]
    accounts.clear_failures()


# --------------------------------------------------------------------------- #
# 找回密码
# --------------------------------------------------------------------------- #
def test_password_reset_revokes_all_sessions(api):
    client, _ = api
    reg = _register(client).json()
    client.cookies.clear()

    forgot = client.post("/api/v3/auth/password/forgot", json={"email": "a@example.com"})
    assert forgot.status_code == 200
    reset_token = forgot.json()["dev_reset_token"]

    reset = client.post(
        "/api/v3/auth/password/reset",
        json={"token": reset_token, "password": "newpa55word!"},
    )
    assert reset.status_code == 200, reset.text
    # 旧会话全部失效
    assert client.get("/api/v3/me", headers=_h(reg["session_token"])).status_code == 401
    # 旧密码不可用、新密码可用
    assert (
        client.post(
            "/api/v3/auth/login", json={"email": "a@example.com", "password": "pa55word!"}
        ).status_code
        == 401
    )
    ok = client.post(
        "/api/v3/auth/login", json={"email": "a@example.com", "password": "newpa55word!"}
    )
    assert ok.status_code == 200

    # 不存在的邮箱：同样 200，且不下发令牌（防枚举）
    unknown = client.post("/api/v3/auth/password/forgot", json={"email": "nobody@example.com"})
    assert unknown.status_code == 200
    assert "dev_reset_token" not in unknown.json()


# --------------------------------------------------------------------------- #
# 旧设备身份绑定
# --------------------------------------------------------------------------- #
def test_bind_legacy_keeps_data_and_disables_device_identity(api):
    client, db = api
    device = client.post(
        "/api/v3/auth/register", json={"device_key": "device_key_legacy_1", "display_name": "Old"}
    ).json()
    legacy_headers = {"X-User-Id": device["user_id"], "X-User-Token": device["token"]}
    created = client.post(
        "/api/v3/posts", json={"content": "legacy post", "visibility": "private"},
        headers=legacy_headers,
    )
    assert created.status_code == 200, created.text
    client.cookies.clear()

    bound = client.post(
        "/api/v3/auth/bind-legacy",
        json={"email": "old@example.com", "password": "pa55word!", "invite_code": INVITE},
        headers=legacy_headers,
    )
    assert bound.status_code == 200, bound.text
    data = bound.json()
    assert data["user_id"] == device["user_id"]
    assert data["device_identity_disabled"] is True

    # 数据还在
    mine = client.get("/api/v3/posts/mine", headers=_h(data["session_token"])).json()["items"]
    assert [p["content"] for p in mine] == ["legacy post"]

    # 旧设备头立即失效（清掉刚拿到的会话 Cookie，确保只测设备头路径）
    client.cookies.clear()
    assert client.get("/api/v3/me", headers=legacy_headers).status_code == 401
    # 同一 device_key 不再签发身份
    again = client.post("/api/v3/auth/register", json={"device_key": "device_key_legacy_1"})
    assert again.status_code == 401

    # 绑定后的身份可以正常登录
    client.cookies.clear()
    login = client.post(
        "/api/v3/auth/login", json={"email": "old@example.com", "password": "pa55word!"}
    )
    assert login.status_code == 200
    assert login.json()["user_id"] == device["user_id"]


# --------------------------------------------------------------------------- #
# Cookie 会话 + CSRF
# --------------------------------------------------------------------------- #
def test_cookie_session_requires_csrf_for_writes(api):
    client, _ = api
    reg = _register(client).json()
    csrf = reg["csrf_token"]
    # TestClient 已保存 Cookie；不带头也能识别身份
    assert client.get("/api/v3/me").json()["user_id"] == reg["user_id"]

    no_csrf = client.post("/api/v3/posts", json={"content": "cookie write"})
    assert no_csrf.status_code == 403

    ok = client.post(
        "/api/v3/posts",
        json={"content": "cookie write"},
        headers={"X-CSRF-Token": csrf},
    )
    assert ok.status_code == 200, ok.text
    # 只读请求不需要 CSRF
    assert client.get("/api/v3/feed").status_code == 200


def test_anonymous_is_rejected_when_require_auth(api, monkeypatch):
    client, _ = api
    monkeypatch.setenv("SE_REQUIRE_AUTH", "1")
    client.cookies.clear()
    assert client.get("/api/v3/me").status_code == 401
    # 会话仍可用
    reg = _register(client).json()
    assert client.get("/api/v3/me", headers=_h(reg["session_token"])).status_code == 200


# --------------------------------------------------------------------------- #
# 分龄能力
# --------------------------------------------------------------------------- #
def test_age_gate_capabilities(api, monkeypatch):
    client, db = api
    reg = _register(client).json()
    uid = reg["user_id"]
    headers = _h(reg["session_token"])

    # 默认未知年龄：广告永不投放
    caps = client.get("/api/v3/auth/capabilities", headers=headers).json()
    assert caps["age"]["group"] == "unknown"
    assert caps["features"]["ads"] is False

    # 成年核验 + 门禁开启：可投广告，亲密关系可用
    monkeypatch.setenv("SE_AGE_GATE", "1")
    accounts.set_age_verification(db, uid, age_group="adult")
    caps = client.get("/api/v3/auth/capabilities", headers=headers).json()
    assert caps["age"]["verified"] is True
    assert caps["features"]["ads"] is True
    assert caps["features"]["intimate_relationship"] is True
    assert caps["features"]["chat"] is True

    # 青少年：无广告、无亲密关系，普通交流保留
    accounts.set_age_verification(db, uid, age_group="teen", method="ops", note="guardian on file")
    caps = client.get("/api/v3/auth/capabilities", headers=headers).json()
    assert caps["features"]["ads"] is False
    assert caps["features"]["intimate_relationship"] is False
    assert caps["features"]["chat"] is True

    # 未知/待核验：只保留注册处理与帮助入口
    accounts.set_age_verification(db, uid, age_group="unknown", status="pending")
    caps = client.get("/api/v3/auth/capabilities", headers=headers).json()
    assert caps["features"]["chat"] is False
    assert caps["features"]["feed"] is False
    assert caps["features"]["help"] is True


def test_suspended_account_cannot_use_session(api):
    client, db = api
    reg = _register(client).json()
    token = reg["session_token"]
    assert client.get("/api/v3/me", headers=_h(token)).status_code == 200
    with db._lock, db.conn:  # noqa: SLF001 - 直接改状态模拟运营停用
        db.conn.execute(
            "UPDATE v3_accounts SET status='suspended' WHERE user_id=?", (reg["user_id"],)
        )
    assert client.get("/api/v3/me", headers=_h(token)).status_code == 401
    client.cookies.clear()
    assert (
        client.post(
            "/api/v3/auth/login", json={"email": "a@example.com", "password": "pa55word!"}
        ).status_code
        == 401
    )


# --------------------------------------------------------------------------- #
# 密码哈希
# --------------------------------------------------------------------------- #
def test_password_hash_roundtrip():
    encoded = accounts.hash_password("pa55word!")
    assert accounts.verify_password("pa55word!", encoded) is True
    assert accounts.verify_password("pa55word", encoded) is False
    assert accounts.verify_password("", "") is False
    assert accounts.hash_password("pa55word!") != encoded  # 加盐
