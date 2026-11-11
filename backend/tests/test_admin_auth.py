"""管理台密码登录：会话 + 登录流程测试。"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin import auth
from app.admin.router import page_router, router


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(page_router)
    app.include_router(router)
    return app


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setenv("SE_ADMIN_TOKEN", "tok-abc")
    monkeypatch.setenv("SE_ADMIN_PASSWORD", "pw-123456")
    monkeypatch.delenv("SE_ADMIN_SESSION_SECRET", raising=False)
    auth._FAILS.clear()
    yield
    auth._FAILS.clear()


def test_session_sign_and_expiry(configured):
    token = auth.make_session(now=1000.0, ttl=60)
    assert auth.verify_session(token, now=1005.0) is True
    assert auth.verify_session(token, now=1061.0) is False
    assert auth.verify_session(token + "x", now=1005.0) is False
    assert auth.verify_session("garbage", now=1005.0) is False


def test_password_and_token_checks(configured):
    assert auth.password_ok("pw-123456") is True
    assert auth.password_ok("wrong") is False
    assert auth.token_ok("tok-abc") is True
    assert auth.token_ok("nope") is False


def test_login_flow_cookie(configured):
    app = _app()
    client = TestClient(app)

    page = client.get("/admin")
    assert page.status_code == 200
    assert "管理密码" in page.text
    # 登录页不能是旧管理台本体（它顶多在回退链接里提到"旧版词库管理台"）
    assert "/api/admin/status" not in page.text

    bad = client.post("/admin/login", data={"password": "wrong", "next": "/admin"})
    assert bad.status_code == 401
    assert "密码不正确" in bad.text

    ok = client.post(
        "/admin/login",
        data={"password": "pw-123456", "next": "/admin"},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    assert auth.COOKIE_NAME in ok.cookies

    admin_page = client.get("/admin")
    # P1 起 /admin 由 React 管理台接管，旧台移到 /admin/legacy。
    # 这里只断言「登录后拿到的是控制台而不是登录页」，不绑定具体前端实现。
    assert admin_page.status_code == 200
    assert "/admin/login" not in admin_page.text
    assert "管理密码" not in admin_page.text
    assert client.get("/api/admin/status").status_code == 200

    # P4 删除了旧管理台：/admin/legacy 不再返回旧页面，而是走新控制台的 catch-all。
    assert "词库管理" not in client.get("/admin/legacy").text

    # 没 Cookie 的新客户端被拒
    fresh = TestClient(app)
    assert fresh.get("/api/admin/status").status_code == 403
    # 旧 token 方式仍然可用
    assert (
        fresh.get("/api/admin/status", headers={"X-Admin-Token": "tok-abc"}).status_code
        == 200
    )
    assert fresh.get("/admin/legacy?token=tok-abc").status_code == 200

    # 退出登录
    logout = client.post("/admin/logout", follow_redirects=False)
    assert logout.status_code == 303
    assert "管理密码" in client.get("/admin").text


def test_login_rate_limit(configured):
    app = _app()
    client = TestClient(app)
    for _ in range(5):
        assert client.post("/admin/login", data={"password": "bad"}).status_code == 401
    blocked = client.post("/admin/login", data={"password": "pw-123456"})
    assert blocked.status_code == 429
    assert "尝试次数过多" in blocked.text
