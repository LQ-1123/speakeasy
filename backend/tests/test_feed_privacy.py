"""多用户升级第一期：信息流与人物动态评论按用户隔离。

覆盖点
------
- ``feed()`` 只返回本人的 ``user_posts``：A 的 public / friends / private 动态
  都不出现在 B 的信息流里，B 只看得到自己发的；``/posts/mine`` 与
  ``feed_summary.my_posts`` 同样各算各的；
- 人物动态（``persona_post``）是共享世界内容，但动态下的**真人评论**按评论者隔离：
  A 评论后，B 看到该动态的 ``comment_count`` 里不含 A 的评论、``comments`` 里也没有
  A 的内容；B 自己评论之后才看得到自己的（A 视角同样不含 B 的）；
- 世界级 AI 评论（``author_type='persona'`` 且 ``user_id=''``，由
  ``event_posts._insert_comment`` 写入）属于共享世界，所有人可见；
  带 ``user_id`` 的 AI 回复（针对某人的回复）只给该用户看，评论数也只算给他。

全部使用 ``tmp_path`` 下的临时数据库，不触碰 ``data/``。
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
from app.v3.daily_world import iso as _iso, now_local as _now_local  # noqa: E402
from app.v3.router import router as v3_router  # noqa: E402
from app.v3.schema import ensure_schema  # noqa: E402
from app.v3.service import V3Service  # noqa: E402


# --------------------------------------------------------------------------- #
# 装配
# --------------------------------------------------------------------------- #
def _make_app(tmp_path: Path):
    db = DB(str(tmp_path / "feed_privacy.db"))
    db.init()
    ensure_schema(db)
    # 账号体系（邮箱登录）的表由 main.py 在启动时创建；测试直接装配 app，
    # 这里补一次（幂等），否则 bind_current_user 的 has_account 会找不到 v3_accounts。
    try:
        from app.v3.accounts import ensure_accounts_schema  # noqa: PLC0415
    except Exception:  # pragma: no cover - 账号体系未接线时保持兼容
        pass
    else:
        ensure_accounts_schema(db)

    service = V3Service(
        db, SimpleNamespace(llm=SimpleNamespace(uses_mock=True)), str(tmp_path)
    )
    # 关掉社交事件：本测试只关心读路径的隔离，不要让人物点赞/评论混进来。
    service.config = engine.SocialConfig(enabled=False)
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
    return app, db, service


@pytest.fixture()
def api(tmp_path):
    app, db, service = _make_app(tmp_path)
    with TestClient(app) as client:
        yield client, db
    db.close()


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _register(client: TestClient, device_key: str, display_name: str = ""):
    response = client.post(
        "/api/v3/auth/register",
        json={"device_key": device_key, "display_name": display_name},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    headers = {"X-User-Id": data["user_id"], "X-User-Token": data["token"]}
    return data, headers


def _create_post(client: TestClient, headers: dict, content: str, visibility: str = "public") -> dict:
    response = client.post(
        "/api/v3/posts",
        json={"content": content, "visibility": visibility},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["post"]


def _feed(client: TestClient, headers: dict, limit: int = 40) -> dict:
    response = client.get("/api/v3/feed", params={"limit": limit}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _items(feed: dict) -> dict[str, dict]:
    return {item["id"]: item for item in feed["items"]}


def _first_persona_post(client: TestClient, headers: dict) -> dict:
    for item in _feed(client, headers)["items"]:
        if item["kind"] == "persona_post":
            return item
    raise AssertionError("信息流里应当有种子人物动态")


def _comment(client: TestClient, headers: dict, post_id: str, content: str) -> dict:
    response = client.post(
        f"/api/v3/persona-posts/{post_id}/comments",
        json={"content": content},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _comment_ids(item: dict) -> set[str]:
    return {str(c["id"]) for c in item["comments"]}


def _comment_contents(item: dict) -> set[str]:
    return {str(c["content"]) for c in item["comments"]}


# --------------------------------------------------------------------------- #
# 用户动态：只有本人
# --------------------------------------------------------------------------- #
def test_feed_only_returns_own_user_posts(api):
    client, _db = api
    alice, alice_headers = _register(client, "device_fp_alice_0001", "Alice")
    bob, bob_headers = _register(client, "device_fp_bob_0001", "Bob")

    alice_public = _create_post(client, alice_headers, "alice public", "public")
    alice_friends = _create_post(client, alice_headers, "alice friends", "friends")
    alice_private = _create_post(client, alice_headers, "alice private", "private")
    bob_public = _create_post(client, bob_headers, "bob public", "public")

    # Bob 的信息流里没有任何一条 Alice 的动态，只有自己的
    bob_items = _items(_feed(client, bob_headers))
    assert bob_public["id"] in bob_items
    for post in (alice_public, alice_friends, alice_private):
        assert post["id"] not in bob_items
    assert {
        item["id"] for item in bob_items.values() if item["kind"] == "user_post"
    } == {bob_public["id"]}

    # Alice 自己三种可见性都在，且 is_mine=True
    alice_items = _items(_feed(client, alice_headers))
    for post in (alice_public, alice_friends, alice_private):
        assert post["id"] in alice_items
        assert alice_items[post["id"]]["is_mine"] is True
    assert bob_public["id"] not in alice_items

    # 我的动态列表 / 摘要计数同样按用户
    alice_mine = {
        item["id"]
        for item in client.get("/api/v3/posts/mine", headers=alice_headers).json()["items"]
    }
    assert alice_mine == {alice_public["id"], alice_friends["id"], alice_private["id"]}
    bob_mine = {
        item["id"]
        for item in client.get("/api/v3/posts/mine", headers=bob_headers).json()["items"]
    }
    assert bob_mine == {bob_public["id"]}

    assert client.get("/api/v3/feed/summary", headers=alice_headers).json()["my_posts"] == 3
    assert client.get("/api/v3/feed/summary", headers=bob_headers).json()["my_posts"] == 1


# --------------------------------------------------------------------------- #
# 人物动态下的真人评论：按评论者隔离
# --------------------------------------------------------------------------- #
def test_persona_post_comments_are_isolated_between_users(api):
    client, _db = api
    _alice, alice_headers = _register(client, "device_fp_alice_0002", "Alice")
    _bob, bob_headers = _register(client, "device_fp_bob_0002", "Bob")

    post = _first_persona_post(client, alice_headers)
    post_id = post["id"]
    persona_id = post["author"]["id"]
    base_count = int(post["comment_count"])

    alice_text = "alice private comment 001"
    alice_comment = _comment(client, alice_headers, post_id, alice_text)

    # B 视角：计数不含 A 的评论，评论列表里也没有 A 的内容
    bob_item = _items(_feed(client, bob_headers))[post_id]
    assert int(bob_item["comment_count"]) == base_count
    assert alice_comment["id"] not in _comment_ids(bob_item)
    assert alice_text not in _comment_contents(bob_item)

    # A 视角：看得到自己刚写的
    alice_item = _items(_feed(client, alice_headers))[post_id]
    assert int(alice_item["comment_count"]) == base_count + 1
    assert alice_comment["id"] in _comment_ids(alice_item)

    bob_text = "bob private comment 002"
    bob_comment = _comment(client, bob_headers, post_id, bob_text)

    bob_item = _items(_feed(client, bob_headers))[post_id]
    assert int(bob_item["comment_count"]) == base_count + 1, "只应 +1（B 自己的那条）"
    assert bob_comment["id"] in _comment_ids(bob_item)
    assert alice_comment["id"] not in _comment_ids(bob_item)

    alice_item = _items(_feed(client, alice_headers))[post_id]
    assert int(alice_item["comment_count"]) == base_count + 1, "A 的计数不应含 B 的评论"
    assert alice_comment["id"] in _comment_ids(alice_item)
    assert bob_comment["id"] not in _comment_ids(alice_item)
    assert bob_text not in _comment_contents(alice_item)

    # 人物主页的动态列表走同一条 _batch_feed_details，口径必须一致
    persona_posts_b = {
        item["id"]: item
        for item in client.get(
            f"/api/v3/personas/{persona_id}/posts", headers=bob_headers
        ).json()["items"]
    }
    assert int(persona_posts_b[post_id]["comment_count"]) == base_count + 1
    assert alice_comment["id"] not in _comment_ids(persona_posts_b[post_id])


# --------------------------------------------------------------------------- #
# 世界级 AI 评论共享，带 user_id 的 AI 回复只给本人
# --------------------------------------------------------------------------- #
def test_world_ai_comments_shared_but_addressed_replies_are_private(api):
    client, db = api
    alice, alice_headers = _register(client, "device_fp_alice_0003", "Alice")
    _bob, bob_headers = _register(client, "device_fp_bob_0003", "Bob")

    post = _first_persona_post(client, alice_headers)
    post_id = post["id"]
    persona_id = post["author"]["id"]
    base_count = int(post["comment_count"])

    # 时间戳取很早：保证两条 AI 评论在 comments 列表的 rn<=2 截断里都排在最前，
    # 这样「评论列表可见性」的断言不依赖种子数据里是否已有评论。
    early = "2020-01-01T00:00:00+00:00"
    world_text = "world ai comment shared"
    reply_text = "ai reply addressed to alice"
    # 同一 (post, persona) 只允许一条评论（schema 唯一索引），换一个 AI 人物来回复。
    with db._lock:  # noqa: SLF001
        replier = db.conn.execute(
            "SELECT id FROM personas WHERE id<>? ORDER BY id LIMIT 1", (persona_id,)
        ).fetchone()["id"]
    with db._lock, db.conn:  # noqa: SLF001 - 测试直接造 AI 评论行
        db.conn.execute(
            "INSERT INTO persona_post_comments (id, post_id, user_id, content, created_at,"
            " author_persona_id, author_type) VALUES (?,?,?,?,?,?,'persona')",
            ("ppc_world_ai_fp", post_id, "", world_text, early, persona_id),
        )
        db.conn.execute(
            "INSERT INTO persona_post_comments (id, post_id, user_id, content, created_at,"
            " author_persona_id, author_type) VALUES (?,?,?,?,?,?,'persona')",
            ("ppc_reply_alice_fp", post_id, alice["user_id"], reply_text, early, str(replier)),
        )

    alice_item = _items(_feed(client, alice_headers))[post_id]
    bob_item = _items(_feed(client, bob_headers))[post_id]

    # 世界级 AI 评论：双方都看得到
    assert world_text in _comment_contents(alice_item)
    assert world_text in _comment_contents(bob_item)
    # 针对 Alice 的 AI 回复：只有 Alice 看得到
    assert reply_text in _comment_contents(alice_item)
    assert reply_text not in _comment_contents(bob_item)

    assert int(alice_item["comment_count"]) == base_count + 2
    assert int(bob_item["comment_count"]) == base_count + 1, "不得把别人收到的 AI 回复算进计数"

    # 评论数与时序无关，但 AI 评论必须被识别为 persona 作者（前端显示 AI 标识）
    assert any(
        c["author"].get("is_ai") for c in alice_item["comments"]
    ), "AI 评论的 author.is_ai 应为 True"
