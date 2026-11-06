"""V3 REST API（``/api/v3/*``）集成测试。

覆盖点
------
- ``/api/v3/health`` 无身份可访问、``auth/register`` 发 token；
- 带 token 访问、伪造/缺失 token → 401、非法 id → 400；
- 两个用户之间 feed / posts / friendships / events / v2 好友互相隔离；
- 加好友后复用 V2 聊天链路（``/api/v2/friends``、消息接口）；
- 删除动态越权 404；
- V2 回归：不带身份头时仍按 ``local`` 用户工作。

实现方式
--------
用 ``TestClient`` 在进程内装配 ``v2_router`` + ``v3_router`` 与独立的
``tmp_path`` 数据库；不会动 ``data/speakeasy_v2.db``，也不涉及 8000 端口。
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
from app.v3 import social_engine as engine  # noqa: E402
from app.v3.identity import LOCAL_USER_ID, reset_current_user, set_current_user  # noqa: E402
from app.v3.router import router as v3_router  # noqa: E402
from app.v3.router import V3_VERSION  # noqa: E402
from app.v3.schema import ensure_schema  # noqa: E402
from app.v3.service import V3Service  # noqa: E402


# --------------------------------------------------------------------------- #
# 装配
# --------------------------------------------------------------------------- #
def social_config(**overrides) -> engine.SocialConfig:
    base = dict(
        enabled=True,
        base_probability=1.0,
        type_weights={"like": 1.0, "comment": 0.0, "friend_request": 0.0},
        delay_min_s=0,
        delay_max_s=0,
        persona_cooldown_s=0,
        max_events_per_post=2,
        max_events_per_user_day=99,
        max_candidates=3,
        min_match_score=1,
    )
    base.update(overrides)
    return engine.SocialConfig(**base)


def _make_app(tmp_path: Path):
    db = DB(str(tmp_path / "v3_api.db"))
    db.init()
    ensure_schema(db)
    service = V3Service(
        db, SimpleNamespace(llm=SimpleNamespace(uses_mock=True)), str(tmp_path)
    )
    service.config = social_config()
    service.rng = random.Random(7)

    # 最小可用的 v2 services 壳：好友列表/聊天消息只需要 db 与几个属性
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
    return app, db, service


@pytest.fixture()
def api(tmp_path):
    app, db, service = _make_app(tmp_path)
    with TestClient(app) as client:
        yield client, db, service
    db.close()


def _register(client: TestClient, device_key: str, display_name: str = ""):
    response = client.post(
        "/api/v3/auth/register",
        json={"device_key": device_key, "display_name": display_name},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    headers = {"X-User-Id": data["user_id"], "X-User-Token": data["token"]}
    return data, headers


# --------------------------------------------------------------------------- #
# 健康 & 身份
# --------------------------------------------------------------------------- #
def test_health_is_public(api):
    client, _, _ = api
    response = client.get("/api/v3/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["version"] == V3_VERSION
    assert data["personas"] == 12
    assert "base_probability" in data["social"]


def test_register_and_token_auth(api):
    client, _, _ = api

    first = client.post(
        "/api/v3/auth/register",
        json={"device_key": "device_key_test_0001", "display_name": "Alice"},
    )
    assert first.status_code == 200
    data = first.json()
    assert data["created"] is True
    assert data["user_id"].startswith("u_")
    assert data["display_name"] == "Alice"
    headers = {"X-User-Id": data["user_id"], "X-User-Token": data["token"]}

    me = client.get("/api/v3/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["user_id"] == data["user_id"]

    # 同一 device_key 再注册：同一用户、created=False、token 轮换（旧的立即失效）
    again = client.post(
        "/api/v3/auth/register", json={"device_key": "device_key_test_0001"}
    ).json()
    assert again["created"] is False
    assert again["user_id"] == data["user_id"]
    assert again["token"] != data["token"]
    assert client.get("/api/v3/me", headers=headers).status_code == 401

    rotated = {"X-User-Id": again["user_id"], "X-User-Token": again["token"]}
    assert client.get("/api/v3/me", headers=rotated).status_code == 200

    # 伪造 token / 缺 token → 401
    forged = {"X-User-Id": again["user_id"], "X-User-Token": "0" * 48}
    assert client.get("/api/v3/feed", headers=forged).status_code == 401
    assert (
        client.get("/api/v3/feed", headers={"X-User-Id": again["user_id"]}).status_code
        == 401
    )
    # 非法 id 格式 → 400
    assert (
        client.get("/api/v3/feed", headers={"X-User-Id": "x!"}).status_code == 400
    )
    # 不带身份头 = local（兼容旧客户端）
    assert client.get("/api/v3/me").json()["user_id"] == LOCAL_USER_ID


def test_feed_with_token_paginates(api):
    client, _, _ = api
    _, headers = _register(client, "device_key_feed_0001")

    page1 = client.get(
        "/api/v3/feed", params={"limit": 5}, headers=headers
    )
    assert page1.status_code == 200
    data = page1.json()
    assert len(data["items"]) == 5
    assert data["next_cursor"]

    page2 = client.get(
        "/api/v3/feed",
        params={"limit": 5, "cursor": data["next_cursor"]},
        headers=headers,
    ).json()
    assert page2["items"]
    ids1 = {item["id"] for item in data["items"]}
    ids2 = {item["id"] for item in page2["items"]}
    assert ids1.isdisjoint(ids2)


# --------------------------------------------------------------------------- #
# 多用户隔离
# --------------------------------------------------------------------------- #
def test_two_users_are_isolated(api, monkeypatch):
    client, _, _ = api

    alice, alice_headers = _register(client, "device_key_alice_0001", "Alice")
    bob, bob_headers = _register(client, "device_key_bob_0001", "Bob")

    # 同步触发社交事件，避免依赖后台任务
    monkeypatch.setenv("SE_SOCIAL_SYNC", "1")
    created = client.post(
        "/api/v3/posts",
        json={
            "content": "I love climbing",
            "visibility": "public",
            "tags": ["climbing"],
        },
        headers=alice_headers,
    )
    assert created.status_code == 200, created.text
    public_id = created.json()["post"]["id"]

    private = client.post(
        "/api/v3/posts",
        json={"content": "my private diary", "visibility": "private"},
        headers=alice_headers,
    ).json()["post"]

    # 信息流：Bob 看得到公开动态，看不到私密动态
    bob_feed_ids = {
        item["id"]
        for item in client.get(
            "/api/v3/feed", params={"limit": 40}, headers=bob_headers
        ).json()["items"]
    }
    assert public_id in bob_feed_ids
    assert private["id"] not in bob_feed_ids

    # 我的动态隔离
    assert client.get("/api/v3/posts/mine", headers=bob_headers).json()["items"] == []
    assert len(client.get("/api/v3/posts/mine", headers=alice_headers).json()["items"]) == 2

    # 好友关系隔离（v3 friendships + v2 friends）
    persona = client.get(
        "/api/v3/personas", params={"limit": 1}, headers=alice_headers
    ).json()["items"][0]
    added = client.post(
        f"/api/v3/personas/{persona['id']}/friend", headers=alice_headers
    )
    assert added.status_code == 200
    friend_id = added.json()["friend_id"]

    assert client.get("/api/v3/friendships", headers=bob_headers).json() == {
        "pending": [],
        "accepted": [],
    }
    assert (
        len(client.get("/api/v3/friendships", headers=alice_headers).json()["accepted"])
        == 1
    )
    assert client.get("/api/v2/friends", headers=bob_headers).json()["friends"] == []
    alice_v2_friends = client.get("/api/v2/friends", headers=alice_headers).json()[
        "friends"
    ]
    assert [friend["id"] for friend in alice_v2_friends] == [friend_id]

    # 社交事件隔离：Alice 的公开动态触发了事件，Bob 一条都没有
    alice_events = client.get("/api/v3/events", headers=alice_headers).json()
    assert alice_events["items"], "公开动态应触发社交事件"
    assert all(event["post_id"] == public_id for event in alice_events["items"])
    assert client.get("/api/v3/events", headers=bob_headers).json()["items"] == []
    assert (
        client.get("/api/v3/feed/summary", headers=bob_headers).json()["unread_events"]
        == 0
    )


# --------------------------------------------------------------------------- #
# 加好友 → 复用 V2 聊天链路
# --------------------------------------------------------------------------- #
def test_add_friend_exposes_v2_chat_chain(api):
    client, _, _ = api
    _, headers = _register(client, "device_key_chat_0001")

    persona = client.get("/api/v3/personas", params={"limit": 1}, headers=headers).json()[
        "items"
    ][0]
    added = client.post(
        f"/api/v3/personas/{persona['id']}/friend",
        json={"message": "hey, let's practice English"},
        headers=headers,
    )
    assert added.status_code == 200
    payload = added.json()
    assert payload["created"] is True
    assert payload["friendship"]["status"] == "accepted"

    # 幂等：第二次 created=False，friend_id 不变
    again = client.post(
        f"/api/v3/personas/{persona['id']}/friend", headers=headers
    ).json()
    assert again["created"] is False
    assert again["friend_id"] == payload["friend_id"]

    friends = client.get("/api/v2/friends", headers=headers).json()["friends"]
    assert len(friends) == 1
    friend = friends[0]
    assert friend["id"] == payload["friend_id"]
    assert friend["name"] == persona["nickname"]
    assert friend["status"] == "active"
    assert friend["owner_user_id"]  # 归属当前用户

    # 复用 V2 聊天链路：消息列表可访问（新好友没有消息，但接口通）
    messages = client.get(
        f"/api/v2/friends/{payload['friend_id']}/messages", headers=headers
    )
    assert messages.status_code == 200
    assert "messages" in messages.json()


# --------------------------------------------------------------------------- #
# 删除动态越权
# --------------------------------------------------------------------------- #
def test_delete_post_cross_user_forbidden(api):
    client, _, _ = api
    _, alice_headers = _register(client, "device_key_del_a_0001")
    _, bob_headers = _register(client, "device_key_del_b_0001")

    post = client.post(
        "/api/v3/posts",
        json={"content": "hello world", "visibility": "public"},
        headers=alice_headers,
    ).json()["post"]

    assert client.delete(f"/api/v3/posts/{post['id']}", headers=bob_headers).status_code == 404
    assert client.delete(f"/api/v3/posts/{post['id']}", headers=alice_headers).status_code == 200
    assert client.delete(f"/api/v3/posts/{post['id']}", headers=alice_headers).status_code == 404


# --------------------------------------------------------------------------- #
# 事件已读（顺带验证跨用户越权）
# --------------------------------------------------------------------------- #
def test_mark_event_read_and_cross_user_404(api, monkeypatch):
    client, _, _ = api
    monkeypatch.setenv("SE_SOCIAL_SYNC", "1")
    _, alice_headers = _register(client, "device_key_ev_a_0001")
    _, bob_headers = _register(client, "device_key_ev_b_0001")

    client.post(
        "/api/v3/posts",
        json={"content": "climbing time", "visibility": "public", "tags": ["climbing"]},
        headers=alice_headers,
    )
    events = client.get("/api/v3/events", headers=alice_headers).json()
    assert events["items"]
    event = events["items"][0]
    assert event["is_read"] is False

    assert client.post(f"/api/v3/events/{event['id']}/read", headers=bob_headers).status_code == 404

    assert client.post(
        f"/api/v3/events/{event['id']}/read", headers=alice_headers
    ).json() == {"read": True}
    unread = client.get(
        "/api/v3/events", params={"unread_only": True}, headers=alice_headers
    ).json()
    assert unread["items"] == []


# --------------------------------------------------------------------------- #
# V2 回归：不带身份头仍按 local 工作
# --------------------------------------------------------------------------- #
def test_v2_endpoints_still_work_as_local_without_headers(api):
    client, db, _ = api
    _, headers = _register(client, "device_key_local_0001")

    # 无身份头的表达本 → 归属 local
    created = client.post(
        "/api/v2/expressions",
        json={
            "original_text": "I very like it",
            "better_expression": "I'm really into it",
        },
    )
    assert created.status_code == 200
    local_expression_id = created.json()["expression"]["id"]
    local_expressions = client.get("/api/v2/expressions").json()["expressions"]
    assert [item["id"] for item in local_expressions] == [local_expression_id]

    # 注册用户看不到 local 的本地数据
    assert client.get("/api/v2/expressions", headers=headers).json()["expressions"] == []

    # 无身份头的 v2 好友列表仍按 local 工作
    token = set_current_user(LOCAL_USER_ID)
    try:
        local_friend = db.create_friend({"name": "LocalBuddy"})
    finally:
        reset_current_user(token)
    assert local_friend["owner_user_id"] == LOCAL_USER_ID

    local_friends = client.get("/api/v2/friends").json()["friends"]
    assert [friend["id"] for friend in local_friends] == [local_friend["id"]]
    assert client.get("/api/v2/friends", headers=headers).json()["friends"] == []

    # v3 的 local 模式
    assert client.get("/api/v3/me").json()["user_id"] == LOCAL_USER_ID
