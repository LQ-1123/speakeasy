"""管理端地基验收：``require_admin`` 守卫 + CSRF + 操作日志。

覆盖契约（``docs/ADMIN_CONSOLE_CONTRACT.md`` §1.2–1.4）：

- 未登录 403；Cookie 登录后 GET 通过
- 已登录但 POST 缺 ``X-Admin-CSRF`` → 403；令牌**错误**（非空）同样 403
- 带正确 CSRF 头 → 通过
- 合法 ``X-Admin-Token`` 头豁免 CSRF
- ``Origin``/``Referer`` 与 Host 不同源 → 403
- 写操作落 ``admin_audit``，GET 不落
- 配置保存的 summary 只含字段名，不含密钥明文

用独立的最小 app 挂 ``require_admin``，不依赖 ``admin/router.py`` 的拆分进度。
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, Body, Depends, FastAPI, Request
from fastapi.testclient import TestClient

from app.admin import audit as admin_audit
from app.admin import auth
from app.v2.db import DB
from app.v3.schema import ensure_schema


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture()
def db(tmp_path):
    database = DB(str(tmp_path / "admin_guard.db"))
    database.init()
    ensure_schema(database, seed=False)
    admin_audit.reset_ensure_cache()
    yield database
    database.close()


@pytest.fixture()
def app(db) -> FastAPI:
    """最小管理 app：路由级挂 require_admin，复刻拆分后的挂法。"""
    application = FastAPI()
    application.state.v2 = type("Services", (), {"db": db})()

    router = APIRouter(prefix="/api/admin", dependencies=[Depends(auth.require_admin)])

    @router.get("/status")
    async def status() -> dict:
        return {"ok": True}

    @router.get("/session")
    async def session(request: Request) -> dict:
        return {"csrf": auth.csrf_token(request)}

    @router.post("/echo")
    async def echo(payload: dict = Body(...)) -> dict:
        return {"ok": True, "keys": sorted(payload.keys())}

    @router.put("/config/{target}")
    async def put_config(target: str, payload: dict = Body(...)) -> dict:
        return {"ok": True, "target": target}

    @router.delete("/personas/{persona_id}")
    async def delete_persona(persona_id: str) -> dict:
        return {"ok": True, "id": persona_id}

    application.include_router(router)
    return application


@pytest.fixture()
def configured(monkeypatch):
    """配好口令：Cookie 登录是唯一身份，回环直通失效 → CSRF 必然生效。"""
    monkeypatch.setenv("SE_ADMIN_PASSWORD", "pw-123456")
    monkeypatch.setenv("SE_ADMIN_TOKEN", "tok-abc")
    monkeypatch.delenv("SE_ADMIN_SESSION_SECRET", raising=False)
    auth._FAILS.clear()
    yield
    auth._FAILS.clear()


@pytest.fixture()
def loopback_open(monkeypatch):
    """未配口令也未配 token：TestClient 的 ``testclient`` host 属回环白名单。"""
    monkeypatch.delenv("SE_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SE_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("SE_ADMIN_SESSION_SECRET", raising=False)
    yield


def _login(client: TestClient) -> None:
    client.cookies.set(auth.COOKIE_NAME, auth.make_session())


def _rows(db: DB) -> list[dict]:
    return [
        dict(row)
        for row in db.conn.execute(
            "SELECT id, at, ip, action, target, summary FROM admin_audit"
            " ORDER BY at, id"
        ).fetchall()
    ]


# --------------------------------------------------------------------------- #
# 1. 未登录 / 已登录
# --------------------------------------------------------------------------- #
def test_unauthenticated_is_403(app, configured):
    fresh = TestClient(app)
    assert fresh.get("/api/admin/status").status_code == 403
    assert fresh.post("/api/admin/echo", json={"a": 1}).status_code == 403


def test_cookie_get_allowed_without_csrf(app, configured):
    client = TestClient(app)
    _login(client)
    assert client.get("/api/admin/status").status_code == 200
    # GET 不需要 CSRF 头
    assert "csrf" in client.get("/api/admin/session").json()


def test_cookie_post_without_csrf_is_403(app, configured):
    client = TestClient(app)
    _login(client)
    response = client.post("/api/admin/echo", json={"a": 1})
    assert response.status_code == 403
    assert "CSRF" in str(response.json()["detail"])


def test_cookie_post_with_wrong_csrf_is_403(app, configured):
    """非空但错误的令牌也必须拒绝（只测"缺失"不够）。"""
    client = TestClient(app)
    _login(client)
    bad = client.post(
        "/api/admin/echo",
        json={"a": 1},
        headers={auth.CSRF_HEADER: "0" * 40},
    )
    assert bad.status_code == 403
    assert "CSRF" in str(bad.json()["detail"])


def test_cookie_post_with_correct_csrf_passes(app, configured):
    client = TestClient(app)
    _login(client)
    token = client.get("/api/admin/session").json()["csrf"]
    ok = client.post(
        "/api/admin/echo", json={"a": 1}, headers={auth.CSRF_HEADER: token}
    )
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"ok": True, "keys": ["a"]}


def test_token_header_exempts_csrf(app, configured):
    """脚本/CLI 场景：合法 X-Admin-Token 直接放行，不需要 CSRF 头。"""
    fresh = TestClient(app)
    assert fresh.post(
        "/api/admin/echo", json={"a": 1}, headers={"X-Admin-Token": "tok-abc"}
    ).status_code == 200
    # 错误 token 不给豁免
    assert fresh.post(
        "/api/admin/echo", json={"a": 1}, headers={"X-Admin-Token": "wrong"}
    ).status_code == 403


def test_loopback_passthrough_exempts_csrf(app, loopback_open):
    """未配口令时的本机直通：既不要求登录，也不要求 CSRF。"""
    fresh = TestClient(app)
    assert fresh.get("/api/admin/status").status_code == 200
    assert fresh.post("/api/admin/echo", json={"a": 1}).status_code == 200


# --------------------------------------------------------------------------- #
# 2. 同源校验
# --------------------------------------------------------------------------- #
def test_cross_origin_post_is_403(app, configured):
    client = TestClient(app)
    _login(client)
    token = client.get("/api/admin/session").json()["csrf"]
    evil = client.post(
        "/api/admin/echo",
        json={"a": 1},
        headers={
            auth.CSRF_HEADER: token,
            "Origin": "https://evil.example.com",
            "Host": "testserver",
        },
    )
    assert evil.status_code == 403
    assert "同源" in str(evil.json()["detail"])


def test_same_origin_post_passes(app, configured):
    client = TestClient(app)
    _login(client)
    token = client.get("/api/admin/session").json()["csrf"]
    ok = client.post(
        "/api/admin/echo",
        json={"a": 1},
        headers={
            auth.CSRF_HEADER: token,
            "Origin": "http://testserver",
        },
    )
    assert ok.status_code == 200, ok.text


def test_referer_used_when_origin_missing(app, configured):
    client = TestClient(app)
    _login(client)
    token = client.get("/api/admin/session").json()["csrf"]
    bad = client.post(
        "/api/admin/echo",
        json={"a": 1},
        headers={
            auth.CSRF_HEADER: token,
            "Referer": "https://evil.example.com/admin",
        },
    )
    assert bad.status_code == 403
    good = client.post(
        "/api/admin/echo",
        json={"a": 1},
        headers={
            auth.CSRF_HEADER: token,
            "Referer": "http://testserver/admin",
        },
    )
    assert good.status_code == 200, good.text


def test_sec_fetch_site_cross_site_without_origin_is_403(app, configured):
    """两个来源头都缺失时看 Sec-Fetch-Site：cross-site / none 直接拒绝。"""
    client = TestClient(app)
    _login(client)
    token = client.get("/api/admin/session").json()["csrf"]
    for site in ("cross-site", "none"):
        response = client.post(
            "/api/admin/echo",
            json={"a": 1},
            headers={auth.CSRF_HEADER: token, "Sec-Fetch-Site": site},
        )
        assert response.status_code == 403, site


def test_no_origin_but_valid_csrf_passes(app, configured):
    """老客户端/脚本没有来源头：不无条件放行，退回令牌头校验（有令牌即通过）。"""
    client = TestClient(app)
    _login(client)
    token = client.get("/api/admin/session").json()["csrf"]
    assert client.post(
        "/api/admin/echo", json={"a": 1}, headers={auth.CSRF_HEADER: token}
    ).status_code == 200


# --------------------------------------------------------------------------- #
# 3. 令牌派生
# --------------------------------------------------------------------------- #
def test_csrf_token_is_stable_and_secret_derived(configured):
    assert auth.csrf_token() == auth.csrf_token()
    assert len(auth.csrf_token()) == 40
    # 换密钥 → 令牌变化（不是公开常量）
    import os

    os.environ["SE_ADMIN_SESSION_SECRET"] = "another-secret"
    try:
        assert auth.csrf_token() != ""
        assert auth.csrf_token() == auth.csrf_token()
        first = auth.csrf_token()
    finally:
        os.environ.pop("SE_ADMIN_SESSION_SECRET", None)
    assert first != auth.csrf_token()


# --------------------------------------------------------------------------- #
# 4. 操作日志
# --------------------------------------------------------------------------- #
def test_write_operation_is_audited(app, db, configured):
    client = TestClient(app)
    _login(client)
    token = client.get("/api/admin/session").json()["csrf"]
    assert client.post(
        "/api/admin/echo", json={"a": 1}, headers={auth.CSRF_HEADER: token}
    ).status_code == 200
    rows = _rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["at"]
    assert row["ip"]  # TestClient 的 host
    assert row["action"] == "echo.create"
    assert row["target"] == "/api/admin/echo"
    assert "a" in row["summary"]


def test_get_is_not_audited(app, db, configured):
    client = TestClient(app)
    _login(client)
    assert client.get("/api/admin/status").status_code == 200
    assert client.get("/api/admin/session").status_code == 200
    assert _rows(db) == []


def test_failed_write_is_not_audited(app, db, configured):
    """handler 抛错（校验失败）不落日志：失败的操作不该留在日志里。"""
    client = TestClient(app)
    _login(client)
    # 缺 body → FastAPI 422（在依赖之后、handler 之前）
    assert client.post(
        "/api/admin/echo", headers={auth.CSRF_HEADER: auth.csrf_token()}
    ).status_code == 422
    assert _rows(db) == []


def test_delete_and_put_actions_named(app, db, configured):
    client = TestClient(app)
    _login(client)
    token = auth.csrf_token()
    assert client.delete(
        "/api/admin/personas/p_1", headers={auth.CSRF_HEADER: token}
    ).status_code == 200
    assert client.put(
        "/api/admin/config/llm",
        json={"base_url": "https://api.example.com/v1"},
        headers={auth.CSRF_HEADER: token},
    ).status_code == 200
    actions = {row["action"] for row in _rows(db)}
    assert actions == {"personas.delete", "config.update"}


def test_config_summary_has_no_secret_plaintext(app, db, configured):
    """配置保存的 summary 只记「哪些字段被改」，不记值。"""
    client = TestClient(app)
    _login(client)
    secret = "sk-live-SUPERSECRET-9f3a2b1c"
    assert client.put(
        "/api/admin/config/llm",
        json={"base_url": "https://api.example.com/v1", "api_key": secret},
        headers={auth.CSRF_HEADER: auth.csrf_token()},
    ).status_code == 200
    rows = _rows(db)
    assert len(rows) == 1
    summary = rows[0]["summary"]
    assert "base_url" in summary and "api_key" in summary
    assert secret not in summary
    assert "SUPERSECRET" not in summary
    # 整行都不能出现明文（包括 target/summary）
    assert secret not in str(rows[0])


def test_audit_redacts_secret_values_in_summary(app, db, configured):
    """即使调用方自己传了含密钥的 summary，落库前也要打码（兜底）。"""
    client = TestClient(app)
    _login(client)
    secret = "sk-abcdefgh12345678"

    # 通过 request.state 覆盖摘要（模拟 handler 手写摘要）
    router = APIRouter(prefix="/api/admin", dependencies=[Depends(auth.require_admin)])

    @router.post("/custom")
    async def custom(request: Request) -> dict:
        request.state.audit_summary = f"保存密钥 api_key={secret}"
        return {"ok": True}

    app.include_router(router)
    assert client.post(
        "/api/admin/custom", headers={auth.CSRF_HEADER: auth.csrf_token()}
    ).status_code == 200
    summary = _rows(db)[-1]["summary"]
    assert secret not in summary


def test_audit_skip_opt_out(app, db, configured):
    """handler 可用 request.state.audit_skip 跳过落库（如纯只读 POST）。"""
    client = TestClient(app)
    _login(client)
    router = APIRouter(prefix="/api/admin", dependencies=[Depends(auth.require_admin)])

    @router.post("/preview")
    async def preview(request: Request) -> dict:
        request.state.audit_skip = True
        return {"ok": True}

    app.include_router(router)
    assert client.post(
        "/api/admin/preview", headers={auth.CSRF_HEADER: auth.csrf_token()}
    ).status_code == 200
    assert _rows(db) == []


# --------------------------------------------------------------------------- #
# 5. list_audit
# --------------------------------------------------------------------------- #
def test_list_audit_pagination_and_filters(app, db, configured):
    client = TestClient(app)
    _login(client)
    token = auth.csrf_token()
    client.post("/api/admin/echo", json={"a": 1}, headers={auth.CSRF_HEADER: token})
    client.post("/api/admin/echo", json={"b": 2}, headers={auth.CSRF_HEADER: token})
    client.put(
        "/api/admin/config/llm",
        json={"model": "m", "api_key": "sk-should-not-leak-1234"},
        headers={auth.CSRF_HEADER: token},
    )

    page = admin_audit.list_audit(db, offset=0, limit=2)
    assert page["total"] == 3
    assert len(page["items"]) == 2
    assert page["offset"] == 0 and page["limit"] == 2
    assert {"id", "at", "ip", "action", "target", "summary"} <= set(page["items"][0])

    by_action = admin_audit.list_audit(db, action="config.update")
    assert by_action["total"] == 1
    assert by_action["items"][0]["action"] == "config.update"

    by_target = admin_audit.list_audit(db, target="/api/admin/echo")
    assert by_target["total"] == 2
    assert all("echo" in row["target"] for row in by_target["items"])

    # 全量里都不允许出现密钥明文
    everything = str(admin_audit.list_audit(db, limit=100))
    assert "sk-should-not-leak-1234" not in everything


# --------------------------------------------------------------------------- #
# 6. 过渡期：只有鉴权的挂法（旧 page.py 内联管理台用）
# --------------------------------------------------------------------------- #
def test_require_admin_identity_only_guards_identity(db, monkeypatch):
    """``require_admin_identity`` 只判身份、不校验 CSRF、不记日志。

    这是给旧内联管理台（``page.py``，Cookie 会话下不带 CSRF 头）留的过渡挂法；
    新 React 管理台必须用 ``require_admin``。
    """
    monkeypatch.setenv("SE_ADMIN_PASSWORD", "pw-123456")
    monkeypatch.delenv("SE_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("SE_ADMIN_SESSION_SECRET", raising=False)

    app = FastAPI()
    app.state.v2 = type("Services", (), {"db": db})()
    router = APIRouter(
        prefix="/api/admin", dependencies=[Depends(auth.require_admin_identity)]
    )

    @router.put("/lexicons/{name}")
    async def put_lexicon(name: str, payload: dict = Body(...)) -> dict:
        return {"ok": True}

    app.include_router(router)
    client = TestClient(app)

    # 未登录仍然 403（身份照旧）
    assert client.put("/api/admin/lexicons/dna", json={"data": {}}).status_code == 403
    # Cookie 登录后：无 CSRF 头也放行（旧管理台的既有行为）
    _login(client)
    assert client.put("/api/admin/lexicons/dna", json={"data": {}}).status_code == 200
    # 且不写操作日志
    assert _rows(db) == []


# --------------------------------------------------------------------------- #
# 7. 兜底建表：库没跑 ensure_schema 也不丢日志
# --------------------------------------------------------------------------- #
def test_audit_table_self_heals_without_ensure_schema(tmp_path, monkeypatch):
    """``write_audit`` 在表缺失时自己补建一次（服务先起、schema 后建也不会丢日志）。"""
    monkeypatch.setenv("SE_ADMIN_PASSWORD", "pw-123456")
    monkeypatch.delenv("SE_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("SE_ADMIN_SESSION_SECRET", raising=False)

    bare = DB(str(tmp_path / "bare.db"))
    bare.init()  # 只建 V2 表，不跑 ensure_schema
    admin_audit.reset_ensure_cache()
    try:
        app = FastAPI()
        app.state.v2 = type("Services", (), {"db": bare})()
        router = APIRouter(
            prefix="/api/admin", dependencies=[Depends(auth.require_admin)]
        )

        @router.post("/echo")
        async def echo(payload: dict = Body(...)) -> dict:
            return {"ok": True}

        app.include_router(router)
        client = TestClient(app)
        _login(client)
        assert client.post(
            "/api/admin/echo",
            json={"a": 1},
            headers={auth.CSRF_HEADER: auth.csrf_token()},
        ).status_code == 200
        rows = _rows(bare)
        assert len(rows) == 1
        assert rows[0]["action"] == "echo.create"
    finally:
        bare.close()
        admin_audit.reset_ensure_cache()


# --------------------------------------------------------------------------- #
# 8. 写失败不落日志（handler 抛 HTTPException）
# --------------------------------------------------------------------------- #
def test_handler_error_is_not_audited(app, db, configured):
    client = TestClient(app)
    _login(client)
    router = APIRouter(prefix="/api/admin", dependencies=[Depends(auth.require_admin)])

    @router.post("/reject")
    async def reject() -> dict:
        from fastapi import HTTPException  # noqa: PLC0415

        raise HTTPException(status_code=400, detail="校验失败")

    app.include_router(router)
    assert client.post(
        "/api/admin/reject", headers={auth.CSRF_HEADER: auth.csrf_token()}
    ).status_code == 400
    assert _rows(db) == []
