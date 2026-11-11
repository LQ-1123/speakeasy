"""P3 用户管理 / 好友指派 / 测试推送 验收。

覆盖的硬约束（对应 task-7 的三个"最容易做错"）：
1. **幂等**：直接加好友重复调用 → ``{ok:false, issues:["已经是好友"]}``，
   ``friendships`` 不出现重复行；好友申请同理。
2. **不泄露敏感值**：列表只回 ``device_key_tail``，不回完整 ``device_key``；
   好友指派的消息正文落库，但**审计摘要不抄正文**。
3. **通知通道复用**：``mode='request'`` 写一条 ``social_events``（现有
   outbox/JPush 链路的入口），而不是另造通知表。

另外覆盖：详情不含聊天记录、候选角色排除好友并带 ``match_hint``、
解除好友、测试推送的形状（``{ok, sent, devices, message}``）。

不联网、不启动 lifespan：最小 FastAPI app + 临时 SQLite（与 ``test_admin_api_split.py``
的做法一致）。聚合路由额外挂一次 ``require_admin``，这样写操作会真的落
``admin_audit``——审计摘要是本次要验证的交付物之一。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.admin import auth
from app.admin.api import users as users_api
from app.admin.router import router as admin_router
from app.v2.db import DB
from app.v3 import push_config
from app.v3.client_api import ensure_client_schema
from app.v3.schema import ensure_schema
from app.v3.service import V3Service


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture()
def db(tmp_path):
    """带 V3 schema + client_devices（推送设备）的临时库。"""
    database = DB(str(tmp_path / "admin_users.db"))
    database.init()
    ensure_schema(database, seed=True)
    ensure_client_schema(database)
    yield database
    database.close()


@pytest.fixture()
def client(db, monkeypatch):
    """最小管理 app：聚合路由 + require_admin（写操作会落操作日志）。"""
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


def _add_user(db, user_id: str, *, device_key: str, display_name: str = "Tester") -> None:
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT OR REPLACE INTO v3_users"
            " (id, device_key, token_hash, display_name, created_at, last_seen_at)"
            " VALUES (?,?,'',?, '2026-01-01T00:00:00+00:00', '2026-01-02T00:00:00+00:00')",
            (user_id, device_key, display_name),
        )


def _add_device(
    db, registration_id: str, user_id: str, *, enabled: int = 1, platform: str = "android"
) -> None:
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT OR REPLACE INTO client_devices"
            " (registration_id, user_id, platform, vendor, enabled, updated_at)"
            " VALUES (?,?,?,'',?,1700000000.0)",
            (registration_id, user_id, platform, enabled),
        )


def _add_user_post(
    db,
    user_id: str,
    content: str,
    post_id: str | None = None,
    created_at: str = "2026-01-03T00:00:00+00:00",
) -> str:
    post_id = post_id or f"up_{user_id}_{len(content)}"
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO user_posts (id, user_id, author_name, content, media, tags,"
            " visibility, like_count, comment_count, created_at)"
            " VALUES (?,?,?,?, '[]', '[]', 'public', 0, 0, ?)",
            (post_id, user_id, "Tester", content, created_at),
        )
    return post_id


def _personas(db, limit: int = 3) -> list[dict]:
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT id, nickname, city, language, interests, occupation FROM personas"
            " WHERE status='active' ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def _friendship_count(db, user_id: str, persona_id: str) -> int:
    with db._lock:  # noqa: SLF001
        return int(
            db.conn.execute(
                "SELECT COUNT(*) AS n FROM friendships WHERE user_id=? AND persona_id=?",
                (user_id, persona_id),
            ).fetchone()["n"]
        )


def _last_audit(db) -> dict:
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT action, target, summary FROM admin_audit ORDER BY at DESC, id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row is not None else {}


# --------------------------------------------------------------------------- #
# 列表 / 详情 / 候选角色
# --------------------------------------------------------------------------- #
def test_list_users_masks_device_key_and_counts(client, db):
    _add_user(db, "u_a", device_key="device-0123456789abcdef", display_name="Alice")
    _add_user(db, "u_b", device_key="device-fedcba9876543210", display_name="Bob")
    _add_device(db, "reg-a1", "u_a")
    _add_device(db, "reg-a2", "u_a")
    _add_user_post(db, "u_a", "hello world")
    persona = _personas(db, 1)[0]
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO friendships (id, user_id, persona_id, status, friend_id,"
            " request_message, created_at, updated_at)"
            " VALUES ('f_a','u_a',?,'accepted','', '', '2026-01-01T00:00:00+00:00',"
            " '2026-01-01T00:00:00+00:00')",
            (persona["id"],),
        )

    payload = client.get("/api/admin/users").json()
    assert payload["ok"] is True
    assert payload["total"] == 2
    by_id = {item["id"]: item for item in payload["items"]}
    alice = by_id["u_a"]
    # 只回尾号，完整设备号绝不下发
    assert alice["device_key_tail"] == "abcdef"
    assert "device_key" not in alice
    assert "device-0123456789abcdef" not in json.dumps(payload)
    assert alice["counts"] == {"posts": 1, "friends": 1, "devices": 2}

    searched = client.get("/api/admin/users", params={"search": "Alice"}).json()
    assert searched["total"] == 1
    assert searched["items"][0]["id"] == "u_a"

    sorted_by_posts = client.get("/api/admin/users", params={"sort": "posts"}).json()
    assert sorted_by_posts["items"][0]["id"] == "u_a"

    assert client.get("/api/admin/users", params={"sort": "nope"}).status_code == 400


def test_list_users_scope_separates_shells_from_traced_users(client, db):
    """``scope`` 把"用过的"和"注册后没痕迹的"分开——这正是 91 行 vs 真人的问题。

    背景：客户端在空白 localStorage 里启动就会新造一个 user_id（无痕窗口、
    重装 App、清数据），所以 ``v3_users`` 行数远大于人数。管理台必须能分辨，
    否则"用户总数"永远是个误导性数字。
    """
    # u_real：有设备注册 + 发过动态 → 有痕迹
    _add_user(db, "u_real", device_key="device-real-000000000001")
    _add_device(db, "reg-real", "u_real")
    _add_user_post(db, "u_real", "hello")
    # u_shell：裸注册，什么都没做
    _add_user(db, "u_shell", device_key="device-shell-000000000002")

    everything = client.get("/api/admin/users", params={"scope": "all"}).json()
    assert everything["total"] == 2
    assert everything["scope"] == "all"
    # 汇总不受 scope 过滤影响
    assert everything["summary"] == {"total": 2, "real": 1, "empty": 1, "registered": 1}
    by_id = {item["id"]: item for item in everything["items"]}
    assert by_id["u_real"]["has_trace"] is True
    assert by_id["u_shell"]["has_trace"] is False

    traced = client.get("/api/admin/users", params={"scope": "real"}).json()
    assert traced["total"] == 1
    assert [item["id"] for item in traced["items"]] == ["u_real"]

    shells = client.get("/api/admin/users", params={"scope": "empty"}).json()
    assert shells["total"] == 1
    assert [item["id"] for item in shells["items"]] == ["u_shell"]

    # scope 与 search 同时生效（这里搜 u_shell，但限定 real → 空）
    combo = client.get(
        "/api/admin/users", params={"scope": "real", "search": "u_shell"}
    ).json()
    assert combo["total"] == 0

    assert client.get("/api/admin/users", params={"scope": "nope"}).status_code == 400


def test_list_users_scope_real_is_empty_when_no_user_has_traces(client, db):
    """全是空壳时 ``scope=real`` 必须返回 0，而不是"条件被省掉 → 返回全部"。

    这是 ``IN ()`` 空集合的经典坑：拼不出占位符就忘了加条件，结果筛选形同虚设。
    """
    _add_user(db, "u_only", device_key="device-only-000000000003")

    traced = client.get("/api/admin/users", params={"scope": "real"}).json()
    assert traced["total"] == 0
    assert traced["items"] == []

    shells = client.get("/api/admin/users", params={"scope": "empty"}).json()
    assert shells["total"] == 1
    assert shells["items"][0]["id"] == "u_only"


def test_user_detail_has_profile_devices_friends_posts_and_no_chat(client, db):
    _add_user(db, "u_detail", device_key="device-abcdefghi123456", display_name="Detail")
    _add_device(db, "reg-d1", "u_detail", enabled=1)
    _add_device(db, "reg-d2", "u_detail", enabled=0, platform="ios")
    _add_user_post(
        db,
        "u_detail",
        "my own post",
        post_id="up_detail_0",
        created_at="2026-06-01T00:00:00+00:00",  # 最新一条，列表第一条即可见
    )
    for index in range(1, 22):  # 21 条，用来验证「列表截断但计数是全量」
        _add_user_post(db, "u_detail", f"extra post {index}", post_id=f"up_detail_{index}")
    persona = _personas(db, 1)[0]
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO friendships (id, user_id, persona_id, status, friend_id,"
            " request_message, created_at, updated_at)"
            " VALUES ('f_detail','u_detail',?,'accepted','', '',"
            " '2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')",
            (persona["id"],),
        )

    payload = client.get("/api/admin/users/u_detail").json()
    assert payload["ok"] is True
    assert payload["user"]["id"] == "u_detail"
    assert payload["user"]["device_key_tail"] == "123456"
    assert isinstance(payload["profile"], dict) and payload["profile"]
    assert [d["registration_id"] for d in payload["devices"]] == ["reg-d1", "reg-d2"]
    assert payload["devices"][0]["enabled"] is True
    assert payload["devices"][1]["enabled"] is False
    assert payload["friends"][0]["persona_id"] == persona["id"]
    assert payload["friends"][0]["persona_nickname"] == persona["nickname"]
    assert payload["friends"][0]["status"] == "accepted"
    assert "my own post" in [item["content"] for item in payload["posts"]]
    # 列表最多回 20 条，但 counts 是全量（否则前端显示「22 条」却只有 20 条）
    assert len(payload["posts"]) == 20
    assert payload["counts"]["posts"] == 22
    # 产品决定：管理台不看用户对话
    assert "messages" not in payload

    missing = client.get("/api/admin/users/u_nope").json()
    assert missing["ok"] is False
    assert missing["issues"] == ["用户不存在"]


def test_candidate_personas_excludes_friends_and_has_match_hint(client, db):
    _add_user(db, "u_cand", device_key="device-abcdefghijklmn")
    personas = _personas(db, 3)
    target = personas[0]
    friend = personas[1]
    # 用户画像：与 target 同城市、同兴趣
    db.update_profile(
        user_id="u_cand",
        native_language="en",
        target_languages=["en"],
        fact_sheet={
            "profile": {"city": target["city"]},
            "interests": json.loads(target["interests"]) if target["interests"] else [],
        },
    )
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO friendships (id, user_id, persona_id, status, friend_id,"
            " request_message, created_at, updated_at)"
            " VALUES ('f_cand','u_cand',?,'accepted','', '',"
            " '2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')",
            (friend["id"],),
        )

    payload = client.get("/api/admin/users/u_cand/candidate-personas", params={"limit": 50}).json()
    assert payload["ok"] is True
    ids = [item["id"] for item in payload["items"]]
    assert friend["id"] not in ids  # 已是好友的不出现
    if target["id"] in ids:
        item = next(x for x in payload["items"] if x["id"] == target["id"])
        hint = item["match_hint"]
        assert set(hint) >= {"same_city", "same_language", "shared_interests", "reasons", "score"}
        assert hint["same_city"] is True
        assert hint["reasons"]

    missing = client.get("/api/admin/users/u_nope/candidate-personas").json()
    assert missing["ok"] is False


# --------------------------------------------------------------------------- #
# 好友指派：幂等 / 通知 / 审计
# --------------------------------------------------------------------------- #
def test_assign_friend_direct_is_idempotent(client, db):
    _add_user(db, "u_friend", device_key="device-abcdefghijklmn")
    persona = _personas(db, 1)[0]

    first = client.post(
        "/api/admin/users/u_friend/friends",
        json={"persona_id": persona["id"], "mode": "direct"},
    ).json()
    assert first["ok"] is True
    assert first["friendship"]["status"] == "accepted"
    assert first["friendship"]["friend_id"]
    assert _friendship_count(db, "u_friend", persona["id"]) == 1

    second = client.post(
        "/api/admin/users/u_friend/friends",
        json={"persona_id": persona["id"], "mode": "direct"},
    ).json()
    assert second["ok"] is False
    assert second["issues"] == ["已经是好友"]
    # 没有重复插入
    assert _friendship_count(db, "u_friend", persona["id"]) == 1


def test_assign_friend_request_notifies_and_does_not_leak_message(client, db):
    _add_user(db, "u_req", device_key="device-abcdefghijklmn")
    persona = _personas(db, 1)[0]
    secret = "SECRET-REQUEST-MESSAGE-BODY"

    payload = client.post(
        "/api/admin/users/u_req/friends",
        json={"persona_id": persona["id"], "mode": "request", "message": secret},
    ).json()
    assert payload["ok"] is True
    assert payload["mode"] == "request"
    assert payload["notified"] is True
    assert payload["friendship"]["status"] == "pending"
    assert _friendship_count(db, "u_req", persona["id"]) == 1

    # 消息落库（friendships + 通知 payload）
    with db._lock:  # noqa: SLF001
        friendship = db.conn.execute(
            "SELECT request_message FROM friendships WHERE user_id='u_req' AND persona_id=?",
            (persona["id"],),
        ).fetchone()
        event = db.conn.execute(
            "SELECT event_type, payload FROM social_events WHERE user_id='u_req'"
        ).fetchone()
    assert friendship["request_message"] == secret
    assert event is not None and event["event_type"] == "friend_request"
    assert secret in event["payload"]
    # 审计摘要不抄正文（只记模式与角色 id）
    audit = _last_audit(db)
    assert audit["action"] == "users.friends"
    assert "mode=request" in audit["summary"]
    assert secret not in audit["summary"]

    again = client.post(
        "/api/admin/users/u_req/friends",
        json={"persona_id": persona["id"], "mode": "request", "message": secret},
    ).json()
    assert again["ok"] is False
    assert again["issues"] == ["已有好友申请"]
    assert _friendship_count(db, "u_req", persona["id"]) == 1


def test_assign_friend_validation(client, db):
    _add_user(db, "u_val", device_key="device-abcdefghijklmn")
    persona = _personas(db, 1)[0]

    # 缺必填 → 400（参数本身非法）
    assert (
        client.post("/api/admin/users/u_val/friends", json={"mode": "direct"}).status_code == 400
    )
    assert (
        client.post(
            "/api/admin/users/u_val/friends",
            json={"persona_id": persona["id"], "mode": "weird"},
        ).status_code
        == 400
    )
    # 业务失败 → 200 + ok=False
    missing_user = client.post(
        "/api/admin/users/u_nope/friends", json={"persona_id": persona["id"], "mode": "direct"}
    ).json()
    assert missing_user["ok"] is False and missing_user["issues"] == ["用户不存在"]
    missing_persona = client.post(
        "/api/admin/users/u_val/friends",
        json={"persona_id": "p_nope", "mode": "direct"},
    ).json()
    assert missing_persona["ok"] is False and missing_persona["issues"] == ["角色不存在"]


def test_remove_friend_deletes_relation(client, db):
    _add_user(db, "u_rm", device_key="device-abcdefghijklmn")
    persona = _personas(db, 1)[0]
    created = client.post(
        "/api/admin/users/u_rm/friends",
        json={"persona_id": persona["id"], "mode": "direct"},
    ).json()
    friendship_id = created["friendship"]["id"]
    friend_id = created["friendship"]["friend_id"]

    removed = client.delete(f"/api/admin/users/u_rm/friends/{friendship_id}").json()
    assert removed["ok"] is True and removed["removed"] is True
    assert _friendship_count(db, "u_rm", persona["id"]) == 0
    with db._lock:  # noqa: SLF001
        left = db.conn.execute("SELECT COUNT(*) AS n FROM friends WHERE id=?", (friend_id,)).fetchone()
    assert int(left["n"]) == 0  # 聊天侧的 friends 行也一起删了

    again = client.delete(f"/api/admin/users/u_rm/friends/{friendship_id}").json()
    assert again["ok"] is False and again["removed"] is False


# --------------------------------------------------------------------------- #
# 测试推送
# --------------------------------------------------------------------------- #
class _FakeResponse:
    status_code = 200
    is_success = True


class _FakeAsyncClient:
    """替代 httpx.AsyncClient：记录请求，不联网。"""

    calls: list[dict] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def post(self, url: str, json: dict | None = None) -> _FakeResponse:
        _FakeAsyncClient.calls.append({"url": url, "json": json, "kwargs": self.kwargs})
        return _FakeResponse()


def test_push_test_shapes(client, db, monkeypatch):
    _add_user(db, "u_push", device_key="device-abcdefghijklmn")
    _add_device(db, "reg-p1", "u_push")
    _add_device(db, "reg-p2", "u_push")
    _add_device(db, "reg-p3", "u_push", enabled=0)  # 停用的设备不推

    # 未配置 JPush → 明确的 ok=False
    monkeypatch.setattr(
        push_config, "effective", lambda: {"app_key": "", "master_secret": "", "source": ""}
    )
    unconfigured = client.post("/api/admin/users/u_push/push-test").json()
    assert unconfigured["ok"] is False
    assert unconfigured["issues"] == ["JPush 未配置"]

    # 配置好 → 发一次，形状固定为 {ok, sent, devices, message}
    _FakeAsyncClient.calls = []
    monkeypatch.setattr(
        push_config,
        "effective",
        lambda: {"app_key": "k" * 20, "master_secret": "s" * 20, "source": "env"},
    )
    monkeypatch.setattr(users_api.httpx, "AsyncClient", _FakeAsyncClient)
    sent = client.post("/api/admin/users/u_push/push-test").json()
    assert sent["ok"] is True
    assert sent["devices"] == 2
    assert sent["sent"] == 2
    assert isinstance(sent["message"], str) and sent["message"]
    assert len(_FakeAsyncClient.calls) == 1
    call = _FakeAsyncClient.calls[0]
    assert call["url"] == users_api.JPUSH_PUSH_URL
    assert call["json"]["audience"]["registration_id"] == ["reg-p1", "reg-p2"]
    assert call["kwargs"]["auth"] == ("k" * 20, "s" * 20)

    # 没有已启用设备 → ok=False
    _add_user(db, "u_nodevice", device_key="device-abcdefghijklmn")
    no_device = client.post("/api/admin/users/u_nodevice/push-test").json()
    assert no_device["ok"] is False
    assert no_device["issues"] == ["该用户没有已启用的推送设备"]
