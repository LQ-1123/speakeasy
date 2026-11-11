"""写操作 CSRF：P4 起**全强制**（不再有过渡期灰名单）。

历史
----
P1~P3 期间旧管理台（``page.py``）还在 ``/admin/legacy`` 兜底，它的内联 JS 不带
``X-Admin-CSRF``。当时为了"新台受保护 + 旧台还能用"，做了灰名单：
**带了头就必须对，不带维持旧行为**。那是一条有意的临时策略。

P4 删掉旧台后，兼容理由消失，于是收口为全强制：
Cookie 身份的写请求**一律**要求合法 ``X-Admin-CSRF``。

本文件钉死收口后的行为。如果有人把强制改回灰名单，这里会红——
那不是"测试太严"，而是 CSRF 保护被削弱了。

为什么 ``X-Admin-Token`` 仍然免 CSRF（这不是漏洞）
--------------------------------------------------
CSRF 攻击的前提是**浏览器自动带上凭据**。``X-Admin-Token`` 是脚本自己塞进
请求头的自定义头，跨站页面无法伪造（会触发 CORS 预检且拿不到响应），
所以不构成 CSRF 面。豁免它是为了不破坏 CLI / 脚本。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.admin import auth
from app.admin.router import api_public_router, page_router, router


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.include_router(api_public_router)
    app.include_router(page_router)
    return app


@pytest.fixture()
def cookie_client(monkeypatch):
    """已用密码登录（Cookie 会话）的客户端。"""
    monkeypatch.setenv("SE_ADMIN_PASSWORD", "pw-csrf")
    monkeypatch.setenv("SE_ADMIN_TOKEN", "tok-csrf")
    monkeypatch.delenv("SE_ADMIN_SESSION_SECRET", raising=False)
    auth._FAILS.clear()
    client = TestClient(_app())
    response = client.post(
        "/admin/login",
        data={"password": "pw-csrf", "next": "/admin"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    yield client
    auth._FAILS.clear()


def _csrf(client: TestClient) -> str:
    return client.get("/api/admin/session").json()["csrf"]


# 一个必定返回业务错误的写请求（dna 校验失败）。
# 用它只为区分"被 CSRF/鉴权挡在 403"和"放行到了 handler"。
_WRITE = "/api/admin/lexicons/dna"
_BAD_PAYLOAD = {"data": {"fields": {}}}


def test_cookie_write_without_csrf_is_403(cookie_client):
    """收口的核心断言：Cookie 身份、不带 CSRF 头 → 必须拒绝。"""
    response = cookie_client.put(_WRITE, json=_BAD_PAYLOAD)
    assert response.status_code == 403, "CSRF 保护被削弱：不带头的写请求被放行了"


def test_cookie_write_with_empty_csrf_is_403(cookie_client):
    """空白值不能被当成"没带"而放行。"""
    response = cookie_client.put(_WRITE, json=_BAD_PAYLOAD, headers={"X-Admin-CSRF": "   "})
    assert response.status_code == 403


def test_cookie_write_with_wrong_csrf_is_403(cookie_client):
    response = cookie_client.put(
        _WRITE, json=_BAD_PAYLOAD, headers={"X-Admin-CSRF": "definitely-wrong"}
    )
    assert response.status_code == 403


def test_cookie_write_with_correct_csrf_reaches_handler(cookie_client):
    """带对令牌要能真正走到 handler（否则是"保护过度"把正常写也挡了）。"""
    response = cookie_client.put(
        _WRITE, json=_BAD_PAYLOAD, headers={"X-Admin-CSRF": _csrf(cookie_client)}
    )
    assert response.status_code != 403


def test_cross_site_write_is_403(cookie_client):
    """跨站来源的写请求一律拒绝——即使带了看似合法的 CSRF 头。"""
    response = cookie_client.put(
        _WRITE,
        json=_BAD_PAYLOAD,
        headers={"Sec-Fetch-Site": "cross-site", "X-Admin-CSRF": _csrf(cookie_client)},
    )
    assert response.status_code == 403


def test_read_without_csrf_is_unaffected(cookie_client):
    """GET 不做 CSRF 校验（否则每次刷新页面都要重取令牌）。"""
    assert cookie_client.get("/api/admin/status").status_code == 200


def test_unauthenticated_write_is_403(monkeypatch):
    monkeypatch.setenv("SE_ADMIN_PASSWORD", "pw-csrf")
    monkeypatch.setenv("SE_ADMIN_TOKEN", "tok-csrf")
    auth._FAILS.clear()
    client = TestClient(_app())
    assert client.put(_WRITE, json=_BAD_PAYLOAD).status_code == 403


def test_token_header_write_works_without_csrf(monkeypatch):
    """脚本/CLI 走 X-Admin-Token：不带 CSRF 也能写（自定义头不构成 CSRF 面）。"""
    monkeypatch.setenv("SE_ADMIN_PASSWORD", "pw-csrf")
    monkeypatch.setenv("SE_ADMIN_TOKEN", "tok-csrf")
    auth._FAILS.clear()
    client = TestClient(_app())
    response = client.put(_WRITE, json=_BAD_PAYLOAD, headers={"X-Admin-Token": "tok-csrf"})
    assert response.status_code != 403


def test_session_endpoint_is_reachable_without_login(monkeypatch):
    """``/session`` 必须未登录可访问：前端靠它决定跳不跳登录页。

    P4 把 ``require_admin`` 挂到父路由时曾把这条也挡住（未登录直接 403），
    前端就只能靠状态码猜。这里锁住。
    """
    monkeypatch.setenv("SE_ADMIN_PASSWORD", "pw-csrf")
    monkeypatch.delenv("SE_ADMIN_TOKEN", raising=False)
    client = TestClient(_app())
    response = client.get("/api/admin/session")
    assert response.status_code == 200
    assert response.json()["authenticated"] is False


def test_logout_is_idempotent_without_login(monkeypatch):
    """会话已过期时「退出」按钮仍要能用。"""
    monkeypatch.setenv("SE_ADMIN_PASSWORD", "pw-csrf")
    client = TestClient(_app())
    assert client.post("/api/admin/logout").status_code == 200


def test_csrf_token_is_stable_across_calls(cookie_client):
    """令牌必须稳定：否则前端缓存的令牌每次都对不上，写操作全 403。"""
    first = _csrf(cookie_client)
    second = _csrf(cookie_client)
    assert first and first == second


def test_csrf_token_matches_server_side_source(cookie_client):
    """下发的令牌与服务端校验用的是同一个派生源（漂移会让所有写 403）。"""
    assert _csrf(cookie_client) == auth.csrf_token()
