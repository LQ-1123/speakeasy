"""青少年门禁：通话 / 事件实时通道 / 后台推送抑制（task-5）。

覆盖：
1) 成年账号在 ``SE_AGE_GATE=1`` 下照常创建通话；
2) teen 未绑监护 → ``minor:guardian_required``；宵禁 → ``minor:curfew``（create 与 renew 都拦）；
3) 事件通道：宵禁时 ``/event-ticket``、``/bootstrap`` 返回 403，WS ``/events`` 以 4003 关闭；
4) 推送：宵禁中的 teen 事件保持 pending、next_attempt 推后 1 小时、绝不发网络；
   非宵禁时同一事件正常发送（保证抑制是按状态判定，不是永远抑制）；
5) 挂断通话会清空 teen 的连续会话起点（``continuous_started_at``）；
6) 旧设备身份（无邮箱账号）在门禁开启时不受影响。

时间全部用 monkeypatch ``teen._now`` 固定，避免依赖本机时钟/时区。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDisconnect

from app.v2.db import DB
from app.v3 import accounts, client_events, teen
from app.v3.call_sessions import CallSessionRegistry
from app.v3.calls import router as calls_router
from app.v3.client_api import ensure_client_schema
from app.v3.client_api import router as client_router
from app.v3.identity import reset_current_user, set_current_user
from app.v3.schema import ensure_schema
from app.v3.security import register_user

# 本地时间 = UTC+8（teen 默认时区 Asia/Shanghai）
ALLOWED_UTC = datetime(2026, 11, 11, 7, 0, tzinfo=timezone.utc)   # 本地 15:00，非宵禁
CURFEW_UTC = datetime(2026, 11, 11, 16, 30, tzinfo=timezone.utc)  # 本地次日 00:30，宵禁


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SE_INVITE_REQUIRED", "0")
    monkeypatch.setenv("SE_AGE_GATE", "1")
    monkeypatch.delenv("SE_REQUIRE_AUTH", raising=False)
    db = DB(str(tmp_path / "teen_realtime.db"))
    db.init()
    ensure_schema(db, seed=False)
    ensure_client_schema(db)
    accounts.ensure_accounts_schema(db)
    teen.ensure_teen_schema(db)
    yield db, monkeypatch
    db.close()


@pytest.fixture
def app_client(env, monkeypatch):
    from app.v3 import calls as calls_module

    db, _ = env
    monkeypatch.setattr(calls_module, "REGISTRY", CallSessionRegistry())
    monkeypatch.setattr(calls_module, "RUNTIMES", {})
    app = FastAPI()
    app.state.v2 = SimpleNamespace(db=db)
    app.include_router(calls_module.router)
    app.include_router(client_router)
    with TestClient(app) as client:
        yield client, db


def register(db, email: str) -> dict:
    return accounts.register_email(db, email, "pa55word!")


def headers(account: dict) -> dict:
    """邮箱账号走会话凭据；旧式头只为满足 calls.require_user 的存在性检查。"""
    return {
        "X-Session-Token": account["session_token"],
        "X-User-Id": account["user_id"],
        "X-User-Token": account["session_token"],
    }


def add_friend(db, user_id: str, name: str = "Mia") -> dict:
    context = set_current_user(user_id)
    try:
        return db.create_friend({"name": name, "reply_language": "en"})
    finally:
        reset_current_user(context)


def make_teen(db, email: str, guardian: dict) -> dict:
    kid = register(db, email)
    teen.bind_guardian(db, kid["user_id"], guardian_user_id=guardian["user_id"])
    return kid


def insert_pending_event(db, user_id: str, event_id: str = "message:teen-1") -> None:
    with db._lock, db.conn:
        db.conn.execute(
            "INSERT INTO client_events (id,user_id,type,friend_id,post_id,payload,created_at,"
            "is_read,push_status,attempts,next_attempt) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                user_id,
                "message",
                "f1",
                "",
                json.dumps({"name": "Mia", "text": "hi"}, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                0,
                "pending",
                0,
                0.0,
            ),
        )


def event_row(db, event_id: str) -> dict:
    with db._lock:
        return dict(
            db.conn.execute("SELECT * FROM client_events WHERE id=?", (event_id,)).fetchone()
        )


class _NoNetworkClient:
    """断言"被抑制的推送绝不联网"的 httpx.AsyncClient 替身。"""

    def __init__(self, calls: list):
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None):
        self.calls.append({"url": url, "json": json})
        raise AssertionError("被门禁抑制的事件不允许发起推送请求")


class _OkResponse:
    status_code = 200

    @property
    def is_success(self) -> bool:
        return True


class _RecordingClient(_NoNetworkClient):
    """允许发送时记录请求并返回成功。"""

    async def post(self, url, json=None):
        self.calls.append({"url": url, "json": json})
        return _OkResponse()


# --------------------------------------------------------------------------- #
# 1) 成年账号不受门禁影响
# --------------------------------------------------------------------------- #
def test_adult_account_can_create_call_under_age_gate(app_client):
    client, db = app_client
    adult = register(db, "adult@example.com")
    accounts.set_age_verification(db, adult["user_id"], age_group="adult", status="verified")
    friend = add_friend(db, adult["user_id"])

    response = client.post(
        "/api/v3/calls", headers=headers(adult), json={"friend_id": friend["id"]}
    )
    assert response.status_code == 200, response.text
    call_id = response.json()["call_id"]
    assert client.delete(f"/api/v3/calls/{call_id}", headers=headers(adult)).status_code == 200


# --------------------------------------------------------------------------- #
# 2) teen：未绑监护 / 宵禁，create 与 renew 都拒绝
# --------------------------------------------------------------------------- #
def test_teen_without_guardian_is_blocked_with_reason(app_client):
    client, db = app_client
    adult = register(db, "guardian@example.com")
    kid = register(db, "kid-noguardian@example.com")
    accounts.set_age_verification(db, kid["user_id"], age_group="teen", status="verified")
    friend = add_friend(db, kid["user_id"])

    response = client.post(
        "/api/v3/calls", headers=headers(kid), json={"friend_id": friend["id"]}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "minor:guardian_required"
    # 未绑监护的账号在成年监护人名下不产生副作用
    assert add_friend(db, adult["user_id"])["owner_user_id"] == adult["user_id"]


def test_teen_curfew_blocks_create_and_ticket_renewal(app_client, monkeypatch):
    client, db = app_client
    adult = register(db, "guardian2@example.com")
    kid = make_teen(db, "kid-curfew@example.com", adult)
    friend = add_friend(db, kid["user_id"])

    clock = {"now": ALLOWED_UTC}
    monkeypatch.setattr(teen, "_now", lambda: clock["now"])

    created = client.post(
        "/api/v3/calls", headers=headers(kid), json={"friend_id": friend["id"]}
    )
    assert created.status_code == 200, created.text
    call_id = created.json()["call_id"]

    clock["now"] = CURFEW_UTC
    renewed = client.post(f"/api/v3/calls/{call_id}/ticket", headers=headers(kid))
    assert renewed.status_code == 403
    assert renewed.json()["detail"] == "minor:curfew"

    blocked = client.post(
        "/api/v3/calls",
        headers=headers(kid),
        json={"friend_id": friend["id"], "request_id": "curfew-new"},
    )
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "minor:curfew"

    clock["now"] = ALLOWED_UTC
    assert client.delete(f"/api/v3/calls/{call_id}", headers=headers(kid)).status_code == 200


# --------------------------------------------------------------------------- #
# 3) 事件实时通道：event-ticket / bootstrap / WS 建连
# --------------------------------------------------------------------------- #
def test_teen_curfew_blocks_event_channel(app_client, monkeypatch):
    client, db = app_client
    adult = register(db, "guardian3@example.com")
    kid = make_teen(db, "kid-events@example.com", adult)

    clock = {"now": ALLOWED_UTC}
    monkeypatch.setattr(teen, "_now", lambda: clock["now"])

    ticket_response = client.post("/api/v3/client/event-ticket", headers=headers(kid))
    assert ticket_response.status_code == 200
    ticket = ticket_response.json()["ticket"]

    clock["now"] = CURFEW_UTC
    blocked_ticket = client.post("/api/v3/client/event-ticket", headers=headers(kid))
    assert blocked_ticket.status_code == 403
    assert blocked_ticket.json()["detail"] == "minor:curfew"

    blocked_bootstrap = client.get("/api/v3/client/bootstrap", headers=headers(kid))
    assert blocked_bootstrap.status_code == 403
    assert blocked_bootstrap.json()["detail"] == "minor:curfew"

    # 允许时段签发的 ticket 在宵禁时建连也必须被 4003 关闭。
    with client.websocket_connect("/api/v3/client/events") as ws:
        ws.send_json({"type": "auth", "ticket": ticket})
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
        assert excinfo.value.code == 4003

    clock["now"] = ALLOWED_UTC
    assert client.get("/api/v3/client/bootstrap", headers=headers(kid)).status_code == 200


# --------------------------------------------------------------------------- #
# 4) 推送抑制：只延后、不失败、不联网
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_curfew_teen_push_is_deferred_without_network(env, monkeypatch):
    db, _ = env
    adult = register(db, "guardian4@example.com")
    kid = make_teen(db, "kid-push@example.com", adult)
    insert_pending_event(db, kid["user_id"])

    monkeypatch.setattr(teen, "_now", lambda: CURFEW_UTC)
    assert teen.push_suppressed(db, kid["user_id"]) is True

    monkeypatch.setattr(
        "app.v3.push_config.effective",
        lambda: {"app_key": "a" * 24, "master_secret": "s" * 24, "source": "env"},
    )
    calls: list = []
    monkeypatch.setattr(
        client_events.httpx, "AsyncClient", lambda **kwargs: _NoNetworkClient(calls)
    )

    before = time.time()
    assert await client_events.dispatch_push(db) == 0
    assert calls == []
    row = event_row(db, "message:teen-1")
    assert row["push_status"] == "pending"
    assert row["attempts"] == 0
    assert row["next_attempt"] >= before + client_events.MINOR_PUSH_RETRY_S - 5


@pytest.mark.asyncio
async def test_teen_push_still_delivered_outside_curfew(env, monkeypatch):
    db, _ = env
    adult = register(db, "guardian5@example.com")
    kid = make_teen(db, "kid-push-ok@example.com", adult)
    insert_pending_event(db, kid["user_id"])
    with db._lock, db.conn:
        db.conn.execute(
            "INSERT INTO client_devices VALUES (?,?,?,?,?,?)",
            ("rid-teen", kid["user_id"], "android", "xiaomi", 1, time.time()),
        )

    monkeypatch.setattr(teen, "_now", lambda: ALLOWED_UTC)
    assert teen.push_suppressed(db, kid["user_id"]) is False
    monkeypatch.setattr(
        "app.v3.push_config.effective",
        lambda: {"app_key": "a" * 24, "master_secret": "s" * 24, "source": "env"},
    )
    calls: list = []
    monkeypatch.setattr(
        client_events.httpx, "AsyncClient", lambda **kwargs: _RecordingClient(calls)
    )

    assert await client_events.dispatch_push(db) == 1
    assert len(calls) == 1
    assert event_row(db, "message:teen-1")["push_status"] == "sent"


# --------------------------------------------------------------------------- #
# 5) 挂断通话清空连续会话
# --------------------------------------------------------------------------- #
def test_hangup_clears_teen_continuous_session(app_client, monkeypatch):
    client, db = app_client
    adult = register(db, "guardian6@example.com")
    kid = make_teen(db, "kid-hangup@example.com", adult)
    monkeypatch.setattr(teen, "_now", lambda: ALLOWED_UTC)

    teen.record_activity(db, kid["user_id"])
    assert teen.usage_for(db, kid["user_id"])["continuous_started_at"]

    friend = add_friend(db, kid["user_id"])
    created = client.post(
        "/api/v3/calls", headers=headers(kid), json={"friend_id": friend["id"]}
    )
    assert created.status_code == 200, created.text
    call_id = created.json()["call_id"]

    ended = client.delete(f"/api/v3/calls/{call_id}", headers=headers(kid))
    assert ended.status_code == 200
    assert teen.usage_for(db, kid["user_id"])["continuous_started_at"] == ""


# --------------------------------------------------------------------------- #
# 6) 旧设备身份（无账号）在门禁开启时不受影响
# --------------------------------------------------------------------------- #
def test_legacy_device_identity_unaffected(app_client):
    client, db = app_client
    device = register_user(db, "legacy-device-77")
    friend = add_friend(db, device["user_id"])
    legacy_headers = {"X-User-Id": device["user_id"], "X-User-Token": device["token"]}

    created = client.post(
        "/api/v3/calls", headers=legacy_headers, json={"friend_id": friend["id"]}
    )
    assert created.status_code == 200, created.text
    assert client.post("/api/v3/client/event-ticket", headers=legacy_headers).status_code == 200
    assert (
        client.delete(f"/api/v3/calls/{created.json()['call_id']}", headers=legacy_headers).status_code
        == 200
    )
