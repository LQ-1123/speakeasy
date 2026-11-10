"""头像一致性回归测试（task-2：聊天/通讯录/信息流/主页/通知同一个人同一张脸）。

线上问题
--------
「聊天的头像没有更新，或者更新慢；头像仍然存在两种情况，没有做到统一。」

后端根因
--------
1. ``_friend_row_for_persona`` 用 ``persona.id + 姓名前缀`` 拼 ``avatar_seed``，
   与 persona 自己的 ``avatar_seed`` 不一致 —— 客户端同一个人的两种 payload
   拿到两个 seed，就渲染出两张脸；
2. 老人物 / 种子人物缺 ``avatar_style + avatar_options`` 时，各接口返回空，
   客户端只能各自回落到 personas 插画或服务端 SVG，画风不统一；
3. 已有好友行不会随 persona 头像变化而更新。

本文件锁定修复后的契约：
- friend 行与 persona 的 ``avatar_seed / avatar_style / avatar_options`` 完全一致；
- 缺部件的人物读取时得到**确定性** options（不调 LLM、不写库，两次读取结果相同）；
- ``sync_persona_friends`` 幂等，会修复老好友行，并跟随 persona 头像变化更新；
- 人物接口 / 信息流作者 / 评论作者 / 通知人物 / 好友关系 payload 同源；
- 兜底 SVG URL 带渲染版本号（头像变化 → URL 变化 → 老客户端缓存失效）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.v2.db import DB
from app.v2.router import router as v2_router
from app.v3.avatar_style import STYLE_ID, TOP, default_options, persona_avatar_meta
from app.v3.router import router as v3_router
from app.v3.schema import ensure_schema, seed_if_needed, sync_persona_friends
from app.v3.service import V3Service


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture()
def world_db(tmp_path):
    db = DB(str(tmp_path / "v3_avatar.db"))
    db.init()
    ensure_schema(db, seed=False)
    seed_if_needed(db)
    yield db
    db.close()


@pytest.fixture()
def service(world_db, tmp_path):
    return V3Service(
        world_db,
        SimpleNamespace(llm=SimpleNamespace(uses_mock=True)),
        str(tmp_path),
    )


def _row(db, table: str, row_id: str) -> dict:
    with db._lock:  # noqa: SLF001 - 测试直接读库校验持久化契约
        row = db.conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
    return dict(row) if row else {}


def _make_app(db, tmp_path):
    service = V3Service(db, SimpleNamespace(llm=SimpleNamespace(uses_mock=True)), str(tmp_path))
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
    return app


def _register(client: TestClient, device: str) -> dict:
    data = client.post("/api/v3/auth/register", json={"device_key": device}).json()
    return {"X-User-Id": data["user_id"], "X-User-Token": data["token"]}


# --------------------------------------------------------------------------- #
# 1. 好友行直接复制 persona 的头像元数据（seed 不再拼接姓名）
# --------------------------------------------------------------------------- #
def test_friend_row_copies_persona_avatar_meta(service, world_db):
    persona = service.persona_by_id("p_jiwoo")
    assert persona is not None

    result = service.add_friend("u_avatar_friend", "p_jiwoo")
    friend = world_db.get_friend(result["friend_id"], any_owner=True)
    brief = service._persona_brief(persona)  # noqa: SLF001 - 校验接口 payload 契约

    # seed = persona.avatar_seed（不再拼接昵称前缀）
    assert persona["avatar_seed"] == "persona-jiwoo"
    assert friend["avatar_seed"] == brief["avatar_seed"] == "persona-jiwoo"
    assert friend["avatar_seed"] != f"{persona['id']}-{persona['nickname'][:2].lower()}"

    # style + options 与 persona payload 完全一致 → 客户端渲染出同一张脸
    assert friend["avatar_style"] == brief["avatar_style"] == STYLE_ID
    assert json.loads(friend["avatar_options"]) == brief["avatar_options"]
    assert brief["avatar_options"]["top"][0] in TOP

    # 同一份 payload 生成逻辑比对：friend 行 vs persona payload
    compat = service._friend_row_for_persona(persona, status="active")  # noqa: SLF001
    assert compat["avatar_seed"] == brief["avatar_seed"]
    assert compat["avatar_style"] == brief["avatar_style"]
    assert json.loads(compat["avatar_options"]) == brief["avatar_options"]


# --------------------------------------------------------------------------- #
# 2. 缺部件的人物：读取时得到确定性 options（不依赖 LLM / 不写库）
# --------------------------------------------------------------------------- #
def test_persona_avatar_meta_defaults_are_deterministic():
    persona = {
        "id": "p_jiwoo",
        "nickname": "Jiwoo",
        "gender": "female",
        "country": "Korea",
        "age": 23,
    }
    first = persona_avatar_meta(persona)
    second = persona_avatar_meta(dict(persona))
    assert first == second, "同一人物两次读取必须得到同一组部件"
    assert first["avatar_style"] == STYLE_ID
    options = first["avatar_options"]
    assert options["top"][0] in TOP
    assert "skinColor" in options and "clothing" in options

    # 只有 style 没有 options 的老人物：同样补齐，且结果一致
    only_style = persona_avatar_meta({**persona, "avatar_style": STYLE_ID, "avatar_options": ""})
    assert only_style == first

    # 已有部件（LLM 侧写）原样返回，不被兜底覆盖
    explicit = {"top": ["hat"], "topProbability": 100}
    got = persona_avatar_meta(
        {**persona, "avatar_style": STYLE_ID, "avatar_options": json.dumps(explicit)}
    )
    assert got["avatar_options"] == explicit

    # 确定性兜底本身可复现
    assert default_options(persona) == default_options(persona)


# --------------------------------------------------------------------------- #
# 3. 同步幂等：修复老好友行，并跟随 persona 头像变化
# --------------------------------------------------------------------------- #
def test_sync_heals_existing_friend_rows_and_is_idempotent(service, world_db):
    result = service.add_friend("u_avatar_sync", "p_adrian")
    friend_id = result["friend_id"]

    # 模拟老库：好友行 seed 是「persona.id-姓名前缀」且没有部件；人物种子也丢了
    with world_db._lock, world_db.conn:  # noqa: SLF001
        world_db.conn.execute(
            "UPDATE friends SET avatar_seed=?, avatar_style='', avatar_options='', avatar_url=''"
            " WHERE id=?",
            ("p_adrian-ad", friend_id),
        )
        world_db.conn.execute("UPDATE personas SET avatar_seed='' WHERE id='p_adrian'")

    sync_persona_friends(world_db)

    persona_row = _row(world_db, "personas", "p_adrian")
    expected = persona_avatar_meta(persona_row)
    friend = world_db.get_friend(friend_id, any_owner=True)

    # 老人物种子被确定性补回，好友行复制同一个 seed
    assert persona_row["avatar_seed"] == "persona-adrian"
    assert friend["avatar_seed"] == persona_row["avatar_seed"]
    assert friend["avatar_style"] == expected["avatar_style"] == STYLE_ID
    assert json.loads(friend["avatar_options"]) == expected["avatar_options"]

    snapshot = {
        key: friend[key]
        for key in ("avatar_seed", "avatar_style", "avatar_options", "avatar_url")
    }
    # 幂等：再同步一次数据完全不变
    sync_persona_friends(world_db)
    again = world_db.get_friend(friend_id, any_owner=True)
    assert {key: again[key] for key in snapshot} == snapshot


def test_sync_follows_persona_avatar_changes(service, world_db):
    result = service.add_friend("u_avatar_change", "p_lin")
    friend_id = result["friend_id"]
    sync_persona_friends(world_db)
    before = world_db.get_friend(friend_id, any_owner=True)

    new_options = {
        "top": ["hat"],
        "topProbability": 100,
        "hairColor": ["2c1b18"],
        "skinColor": ["ffdbb4"],
        "clothing": ["hoodie"],
        "clothesColor": ["262e33"],
        "eyes": ["default"],
        "eyebrows": ["defaultNatural"],
        "mouth": ["smile"],
        "backgroundColor": ["e8f3ec"],
        "accessoriesProbability": 0,
        "facialHairProbability": 0,
    }
    with world_db._lock, world_db.conn:  # noqa: SLF001
        world_db.conn.execute(
            "UPDATE personas SET avatar_style='avataaars', avatar_options=? WHERE id='p_lin'",
            (json.dumps(new_options),),
        )

    sync_persona_friends(world_db)
    after = world_db.get_friend(friend_id, any_owner=True)

    # 头像变化立刻同步到好友行（客户端下一次轮询就能拿到新头像）
    assert json.loads(after["avatar_options"])["top"] == ["hat"]
    assert json.loads(after["avatar_options"]) != json.loads(before["avatar_options"])
    # 兜底 SVG URL 带渲染版本号：部件变了 URL 也变 → 老客户端图片缓存立刻失效
    assert after["avatar_url"] != before["avatar_url"]


# --------------------------------------------------------------------------- #
# 4. 同一 person 的各接口 payload 同源（人物/动态作者/评论作者/通知/好友关系）
# --------------------------------------------------------------------------- #
def test_all_persona_payloads_share_the_same_avatar_identity(service, world_db):
    persona = service.persona_by_id("p_jiwoo")
    brief = service._persona_brief(persona)  # noqa: SLF001

    profile = service.persona_profile("p_jiwoo", user_id="u_avatar_payload")
    posts = service.persona_posts("p_jiwoo", user_id="u_avatar_payload", limit=3)["items"]
    assert posts, "种子人物应该有动态"
    post_author = posts[0]["author"]

    comment_row = {
        "id": "c_avatar",
        "content": "nice one",
        "created_at": "2026-01-01T00:00:00+00:00",
        "author_type": "persona",
        "author_persona_id": "p_jiwoo",
    }
    comment_author = service._comment_public(  # noqa: SLF001
        comment_row, service._persona_map()  # noqa: SLF001
    )["author"]

    friendship = service.add_friend("u_avatar_payload", "p_jiwoo")
    assert friendship["created"] is True
    fs = service._friendship_public(  # noqa: SLF001
        service.get_friendship("u_avatar_payload", "p_jiwoo"),
        service._persona_map(),  # noqa: SLF001
    )
    friend_persona = fs["persona"]

    for payload in (profile, post_author, comment_author, friend_persona):
        assert payload["avatar_seed"] == brief["avatar_seed"], payload
        assert payload["avatar_style"] == STYLE_ID, payload
        assert payload["avatar_options"] == brief["avatar_options"], payload

    # 通知（social event）payload 也同源，且不再用空的 avatar 字段
    with world_db._lock, world_db.conn:  # noqa: SLF001
        world_db.conn.execute(
            "INSERT INTO social_events (id, user_id, persona_id, post_id, event_type, payload,"
            " status, is_read, dedupe_key, scheduled_at, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "ev_avatar",
                "u_avatar_event",
                "p_jiwoo",
                "",
                "like",
                "{}",
                "pending",
                0,
                "dedupe-avatar-1",
                "2020-01-01T00:00:00+00:00",
                "2020-01-01T00:00:00+00:00",
            ),
        )
    events = service.list_events("u_avatar_event")
    event_persona = events["items"][0]["persona"]
    assert event_persona == brief, "通知人物 payload 必须与人物接口逐字段同源"
    assert event_persona["avatar_seed"] == brief["avatar_seed"]
    assert event_persona["avatar_style"] == STYLE_ID
    assert event_persona["avatar_options"] == brief["avatar_options"]
    assert event_persona["avatar"].startswith("/api/v3/avatar/p_jiwoo.svg")
    # gender 必须下发：客户端 resolveAvatar 用它选择发型池，缺了会「同 seed 不同脸」
    assert event_persona["gender"] == brief["gender"]


# --------------------------------------------------------------------------- #
# 5. 端到端：/api/v2/friends 与 /api/v3/personas/* 的头像字段严格一致
# --------------------------------------------------------------------------- #
def test_api_friend_and_persona_payloads_match(world_db, tmp_path):
    app = _make_app(world_db, tmp_path)
    with TestClient(app) as client:
        headers = _register(client, "device_avatar_consistency_0001")
        add = client.post("/api/v3/personas/p_jiwoo/friend", headers=headers)
        assert add.status_code == 200, add.text
        friend_id = add.json()["friend_id"]

        friends = client.get("/api/v2/friends", headers=headers)
        assert friends.status_code == 200, friends.text
        friend = next(f for f in friends.json()["friends"] if f["id"] == friend_id)

        persona = client.get("/api/v3/personas/p_jiwoo", headers=headers)
        assert persona.status_code == 200, persona.text
        profile = persona.json()

        # 聊天列表（friends 行）与人物主页（persona payload）同一张脸：
        # 同一 seed + 同一组 Avataaars 部件
        assert friend["avatar_seed"] == profile["avatar_seed"]
        assert friend["avatar_style"] == profile["avatar_style"] == STYLE_ID
        assert json.loads(friend["avatar_options"]) == profile["avatar_options"]
        assert profile["avatar_options"]["top"][0] in TOP
