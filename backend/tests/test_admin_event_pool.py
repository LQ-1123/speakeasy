"""P3 事件公共池 / 角色事件 验收。

覆盖的硬约束（对应 task-7）：
1. **领取幂等**：``UNIQUE(pool_id, persona_id)`` + 写前查重，重复 claim 只返回
   ``skipped``，不会让同一角色领两次、也不会重复生成 ``persona_events``。
2. **preview 不落库**：预演只算分，``event_pool_claims`` 一张都不写。
3. **分项得分可见**：``score_detail`` 每一项都带 ``contribution``（数值），
   且各维贡献之和 = 总分（前端按占比画进度条）。
4. **自动领取语义**（Lead 补充）：body 不带 ``persona_ids`` 时按预演得分从高到低
   挑前 ``slots`` 个角色，不报错。

另外覆盖：事件池 CRUD、删除级联 claims、角色事件列表/手动补事件。

不联网、不启动 lifespan：最小 FastAPI app + 临时 SQLite。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.admin import auth
from app.admin.router import router as admin_router
from app.v2.db import DB
from app.v3.schema import ensure_schema
from app.v3.service import V3Service


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture()
def db(tmp_path):
    database = DB(str(tmp_path / "admin_events.db"))
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


def _personas(db, limit: int = 3) -> list[dict]:
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT id, nickname, city, language, interests, occupation, speech_dna"
            " FROM personas WHERE status='active' ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def _create_pool(client, *, slots: int = 2, days: str = "2026-12-01T10:00:00+00:00", **overrides):
    payload = {
        "title": "周末市集",
        "description": "河边的手作市集，很多人带着狗来。",
        "event_type": "social",
        "start_at": days,
        "location": "Berlin",
        "slots": slots,
        "conditions": {},
    }
    payload.update(overrides)
    return client.post("/api/admin/event-pool", json=payload)


def _claim_rows(db, pool_id: str) -> list[dict]:
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT * FROM event_pool_claims WHERE pool_id=? ORDER BY claimed_at", (pool_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def _pool_events(db, pool_id: str) -> list[dict]:
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT * FROM persona_events WHERE source_reference=? ORDER BY id", (pool_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def _pool_status(db, pool_id: str) -> str:
    with db._lock:  # noqa: SLF001
        row = db.conn.execute("SELECT status FROM event_pool WHERE id=?", (pool_id,)).fetchone()
    return str(row["status"]) if row is not None else ""


# --------------------------------------------------------------------------- #
# 事件池 CRUD
# --------------------------------------------------------------------------- #
def test_create_and_list_event_pool(client, db):
    created = _create_pool(client, slots=2).json()
    assert created["ok"] is True
    pool_id = created["id"]

    payload = client.get("/api/admin/event-pool").json()
    assert payload["ok"] is True and payload["total"] == 1
    item = payload["items"][0]
    assert item["id"] == pool_id
    assert item["title"] == "周末市集"
    assert item["slots"] == 2
    assert item["status"] == "open"
    assert isinstance(item["conditions"], dict)
    assert item["claims"] == []
    assert item["claimed_count"] == 0

    assert client.get("/api/admin/event-pool", params={"status": "filled"}).json()["total"] == 0

    # 缺必填 → 400（参数本身非法）
    assert client.post("/api/admin/event-pool", json={"event_type": "social"}).status_code == 400
    assert client.post("/api/admin/event-pool", json={"title": "x"}).status_code == 400


def test_update_event_pool_is_partial(client, db):
    pool_id = _create_pool(client).json()["id"]

    updated = client.put(
        f"/api/admin/event-pool/{pool_id}", json={"title": "改过的标题", "slots": 3}
    ).json()
    assert updated["ok"] is True
    assert updated["item"]["title"] == "改过的标题"
    assert updated["item"]["slots"] == 3
    # 没传的字段保持原值（部分更新）
    assert updated["item"]["description"] == "河边的手作市集，很多人带着狗来。"
    assert updated["item"]["location"] == "Berlin"

    assert client.put("/api/admin/event-pool/p_nope", json={"title": "x"}).json()["ok"] is False


# --------------------------------------------------------------------------- #
# 预演：分数明细、不落库
# --------------------------------------------------------------------------- #
def _targeted_pool(client, persona: dict, *, slots: int = 2) -> str:
    interests = json.loads(persona["interests"] or "[]")
    conditions: dict = {
        "interests": interests[:2],
        "occupation": [persona["occupation"]] if persona["occupation"] else [],
    }
    if persona["city"]:
        conditions["city"] = persona["city"]
    return _create_pool(client, slots=slots, conditions=conditions).json()["id"]


def test_preview_scores_are_explained_and_not_persisted(client, db):
    personas = _personas(db, 3)
    target = personas[0]
    pool_id = _targeted_pool(client, target, slots=2)
    before_events = len(_pool_events(db, pool_id))

    payload = client.post(f"/api/admin/event-pool/{pool_id}/preview", json={"limit": 5}).json()
    assert payload["ok"] is True
    # Lead 补充：前端要显示「名额 N / 已领 M」
    assert payload["slots"] == 2
    assert payload["claimed"] == 0
    assert payload["items"], "预演至少要给出一个候选角色"
    assert payload["items"][0]["persona_id"] == target["id"], "命中最多的角色应排第一"

    detail = payload["items"][0]["score_detail"]
    dims = ("occupation", "interests", "city", "dna")
    for name in dims:
        assert name in detail, name
        assert isinstance(detail[name]["contribution"], (int, float))
        assert detail[name]["contribution"] >= 0
    total_from_parts = sum(detail[name]["contribution"] for name in dims)
    assert abs(total_from_parts - payload["items"][0]["score"]) <= 0.05
    assert detail["interests"]["matched"], "兴趣命中要能看出命中了哪些"

    # 预演不落库
    assert _claim_rows(db, pool_id) == []
    assert len(_pool_events(db, pool_id)) == before_events


# --------------------------------------------------------------------------- #
# 领取：显式 persona_ids + 幂等
# --------------------------------------------------------------------------- #
def test_claim_explicit_is_idempotent_and_backfills_event(client, db):
    personas = _personas(db, 3)
    first, second = personas[0], personas[1]
    pool_id = _targeted_pool(client, first, slots=2)

    claimed = client.post(
        f"/api/admin/event-pool/{pool_id}/claim", json={"persona_ids": [first["id"]]}
    ).json()
    assert claimed["ok"] is True
    assert [item["persona_id"] for item in claimed["claimed"]] == [first["id"]]
    assert claimed["claimed"][0]["event_id"]
    assert claimed["filled"] is False

    rows = _claim_rows(db, pool_id)
    assert len(rows) == 1
    assert rows[0]["persona_id"] == first["id"]
    assert rows[0]["score"] > 0
    assert rows[0]["event_id"] == claimed["claimed"][0]["event_id"]

    events = _pool_events(db, pool_id)
    assert len(events) == 1
    assert events[0]["id"] == rows[0]["event_id"]
    assert events[0]["persona_id"] == first["id"]
    assert events[0]["source_type"] == "event_pool"
    assert events[0]["day"]

    # 重复领取同一个角色：幂等，返回 skipped、不再生成事件
    again = client.post(
        f"/api/admin/event-pool/{pool_id}/claim", json={"persona_ids": [first["id"]]}
    ).json()
    assert again["ok"] is True
    assert again["claimed"] == []
    assert [item["persona_id"] for item in again["skipped"]] == [first["id"]]
    assert len(_claim_rows(db, pool_id)) == 1
    assert len(_pool_events(db, pool_id)) == 1

    # 领满 slots → 状态 filled
    filled = client.post(
        f"/api/admin/event-pool/{pool_id}/claim", json={"persona_ids": [second["id"]]}
    ).json()
    assert filled["ok"] is True and filled["filled"] is True
    assert _pool_status(db, pool_id) == "filled"
    assert len(_claim_rows(db, pool_id)) == 2

    # 名额已满：明确拒绝
    third = personas[2]
    full = client.post(
        f"/api/admin/event-pool/{pool_id}/claim", json={"persona_ids": [third["id"]]}
    ).json()
    assert full["ok"] is False and full["issues"] == ["名额已满"]


def test_claim_without_persona_ids_auto_picks_top_slots(client, db):
    """Lead 补充语义：{} → 按预演得分从高到低自动挑前 slots 个，不报错。"""
    personas = _personas(db, 3)
    pool_id = _targeted_pool(client, personas[0], slots=2)

    preview = client.post(f"/api/admin/event-pool/{pool_id}/preview", json={"limit": 5}).json()
    expected = [item["persona_id"] for item in preview["items"][:2]]

    claimed = client.post(f"/api/admin/event-pool/{pool_id}/claim", json={}).json()
    assert claimed["ok"] is True
    assert [item["persona_id"] for item in claimed["claimed"]] == expected
    assert claimed["filled"] is True
    assert len(_claim_rows(db, pool_id)) == 2


def test_delete_event_pool_cascades_claims(client, db):
    personas = _personas(db, 2)
    pool_id = _create_pool(
        client,
        slots=1,
        conditions={"interests": json.loads(personas[0]["interests"] or "[]")[:1]},
    ).json()["id"]
    client.post(f"/api/admin/event-pool/{pool_id}/claim", json={"persona_ids": [personas[0]["id"]]})
    assert len(_claim_rows(db, pool_id)) == 1

    deleted = client.delete(f"/api/admin/event-pool/{pool_id}").json()
    assert deleted["ok"] is True and deleted["deleted"] is True
    assert _claim_rows(db, pool_id) == []
    assert client.get("/api/admin/event-pool").json()["total"] == 0
    assert client.delete("/api/admin/event-pool/p_nope").json()["ok"] is False


# --------------------------------------------------------------------------- #
# 角色事件
# --------------------------------------------------------------------------- #
def test_create_and_list_persona_events(client, db):
    persona = _personas(db, 1)[0]

    created = client.post(
        "/api/admin/events",
        json={
            "persona_id": persona["id"],
            "event_type": "social",
            "title": "跟朋友吃了顿很长的晚饭",
            "day": "2026-12-02",
            "visibility": "public",
        },
    ).json()
    assert created["ok"] is True
    assert created["created"] is True
    event_id = created["id"]

    listed = client.get("/api/admin/events", params={"persona_id": persona["id"]}).json()
    assert listed["ok"] is True and listed["total"] >= 1
    item = next(row for row in listed["items"] if row["id"] == event_id)
    assert item["persona_nickname"] == persona["nickname"]
    assert item["event_type"] == "social"
    assert item["title"] == "跟朋友吃了顿很长的晚饭"
    assert item["source_type"] == "manual"
    assert item["day"] == "2026-12-02"

    filtered = client.get("/api/admin/events", params={"status": "planned"}).json()
    assert any(row["id"] == event_id for row in filtered["items"])


def test_create_persona_event_validation(client, db):
    persona = _personas(db, 1)[0]
    assert client.post("/api/admin/events", json={"event_type": "social"}).status_code == 400
    assert client.post("/api/admin/events", json={"persona_id": persona["id"]}).status_code == 400

    unknown_type = client.post(
        "/api/admin/events", json={"persona_id": persona["id"], "event_type": "not_a_type"}
    ).json()
    assert unknown_type["ok"] is False and "未知事件类型" in unknown_type["issues"][0]

    unknown_persona = client.post(
        "/api/admin/events", json={"persona_id": "p_nope", "event_type": "social"}
    ).json()
    assert unknown_persona["ok"] is False and unknown_persona["issues"] == ["角色不存在"]
