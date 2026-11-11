"""V3 业务服务层（``V3Service``）集成测试。

覆盖点
------
- 信息流游标分页（不重复、不遗漏）与三种可见性；
- Persona 详情、加好友幂等、删除越权；
- 社交事件：延迟、物化幂等、重复触发不重复建事件、每日上限、人物冷却、
  friend_request 事件 → 好友申请 → 接受。

说明
----
- 每个测试用 ``tmp_path`` 下的独立 SQLite，不污染 ``data/``，也不碰 8000 端口服务。
- 事件延迟不 sleep：直接构造 ``delay=0`` 的配置，或把 ``scheduled_at`` 改到过去
  来验证到期路径（见 ``test_event_delay_defers_materialization``）。
- ``V3Service`` 内部有一小部分逻辑（``db.create_friend``）仍通过 contextvar
  读取当前用户；这里用 :func:`as_user` 模拟请求上下文。
"""

from __future__ import annotations

import asyncio
import random
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.v2.db import DB  # noqa: E402
from app.v3 import social_engine as engine  # noqa: E402
from app.v3.identity import (  # noqa: E402
    LOCAL_USER_ID,
    reset_current_user,
    set_current_user,
)
from app.v3.schema import ensure_schema  # noqa: E402
from app.v3.service import V3Service  # noqa: E402


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def social_config(**overrides) -> engine.SocialConfig:
    base = dict(
        enabled=True,
        base_probability=1.0,
        type_weights={"like": 1.0, "comment": 0.0, "friend_request": 0.0},
        delay_min_s=0,
        delay_max_s=0,
        persona_cooldown_s=0,
        max_events_per_post=1,
        max_events_per_user_day=99,
        max_candidates=3,
        min_match_score=1,
    )
    base.update(overrides)
    return engine.SocialConfig(**base)


def run(coro):
    """在同步测试里执行 service 的 async 方法。"""
    return asyncio.run(coro)


def make_service(tmp_path: Path, *, config=None, seed: int = 42):
    db = DB(str(tmp_path / "v3.db"))
    db.init()
    ensure_schema(db)
    engines = SimpleNamespace(llm=SimpleNamespace(uses_mock=True))
    service = V3Service(db, engines, str(tmp_path))
    if config is not None:
        service.config = config
    service.rng = random.Random(seed)
    return db, service


@contextmanager
def as_user(user_id: str):
    """模拟请求上下文（``bind_current_user`` 依赖做的事）。"""
    token = set_current_user(user_id)
    try:
        yield user_id
    finally:
        reset_current_user(token)


def _event_count(db: DB, user_id: str) -> int:
    with db._lock:  # noqa: SLF001
        return int(
            db.conn.execute(
                "SELECT COUNT(*) AS n FROM social_events WHERE user_id=?", (user_id,)
            ).fetchone()["n"]
        )


def test_feed_hydrates_selected_page_with_bounded_relationship_queries(tmp_path):
    db, service = make_service(tmp_path)
    statements = []
    db.conn.set_trace_callback(statements.append)
    try:
        page = service.feed("u_batched", limit=5)
        assert len(page["items"]) == 5
        related = [sql for sql in statements if "SELECT" in sql.upper() and (
            "persona_post_comments" in sql or "persona_post_likes" in sql
        )]
        assert len(related) <= 3
        assert all(" IN (" in sql for sql in related)
    finally:
        db.conn.set_trace_callback(None)
        db.close()


def test_feed_speech_url_revision_changes_with_synthesis_model(tmp_path, monkeypatch):
    from app.voice import voice_registry

    db, service = make_service(tmp_path)
    cfg = SimpleNamespace(provider="edge", fish_model="model-a", fish_format="mp3")
    monkeypatch.setattr(voice_registry, "load_cfg", lambda store=None: cfg)
    first = service.feed("local", limit=1)["items"][0]["speech_version"]
    cfg.fish_model = "model-b"
    second = service.feed("local", limit=1)["items"][0]["speech_version"]
    assert first != second
    db.close()


# --------------------------------------------------------------------------- #
# 信息流
# --------------------------------------------------------------------------- #
def test_feed_cursor_pagination_no_duplicates_no_gaps(tmp_path):
    db, service = make_service(tmp_path)
    try:
        # feed 现在只返回「在练语言」的人物动态：本用例覆盖全部语言，保持 36 条
        langs = [
            str(r[0])
            for r in db.conn.execute(
                "SELECT DISTINCT language FROM persona_posts"
            ).fetchall()
            if r[0]
        ]
        db.update_profile(user_id="u_feed", target_languages=langs)

        public_post = service.create_user_post("u_feed", "my public update")
        private_post = service.create_user_post(
            "u_feed", "my private diary", visibility="private"
        )

        ids: list[str] = []
        cursor = None
        pages = 0
        while True:
            page = service.feed("u_feed", cursor=cursor, limit=7)
            assert len(page["items"]) <= 7
            ids.extend(item["id"] for item in page["items"])
            pages += 1
            cursor = page["next_cursor"]
            if not cursor:
                break
            assert pages < 20, "游标分页疑似死循环"

        assert pages > 1
        assert len(ids) == len(set(ids)), "同一动态在分页里重复出现"
        # 36 条公共 Persona 动态 + 自己的 2 条（含 private，作者可见）
        assert len(ids) == 38
        assert sum(1 for post_id in ids if post_id.startswith("pp_")) == 36
        assert public_post["id"] in ids
        assert private_post["id"] in ids

        summary = service.feed_summary("u_feed")
        assert summary["persona_posts"] == 36
        assert summary["my_posts"] == 2
    finally:
        db.close()


def test_feed_visibility_rules(tmp_path):
    db, service = make_service(tmp_path)
    try:
        public_post = service.create_user_post("u_author", "public hello")
        friends_post = service.create_user_post(
            "u_author", "friends only hello", visibility="friends"
        )
        private_post = service.create_user_post(
            "u_author", "private note", visibility="private"
        )

        # 多用户隔离第一期：信息流只查本人动态，别人一条都看不到（含 public）
        other_ids = {item["id"] for item in service.feed("u_reader", limit=40)["items"]}
        assert public_post["id"] not in other_ids
        assert friends_post["id"] not in other_ids
        assert private_post["id"] not in other_ids

        # 作者自己的信息流包含三种可见性，且 is_mine=True
        own = {item["id"]: item for item in service.feed("u_author", limit=40)["items"]}
        for post in (public_post, friends_post, private_post):
            assert post["id"] in own
            assert own[post["id"]]["is_mine"] is True

        # 存储层数据仍在（读路径不再跨用户暴露，但数据本身没有丢）
        assert service.get_user_post(private_post["id"]) is not None
        assert service.get_user_post(private_post["id"])["visibility"] == "private"
    finally:
        db.close()


def test_persona_profile(tmp_path):
    db, service = make_service(tmp_path)
    try:
        profile = service.persona_profile("p_giulia", "u_view")
        assert profile["nickname"] == "Giulia"
        assert profile["is_ai"] is True
        assert profile["post_count"] == 3
        # 与前端类型一致：profile.posts 是分页对象 {items, next_cursor}
        posts = profile["posts"]
        assert len(posts["items"]) == 3
        assert posts["next_cursor"] == ""
        assert profile["friendship"] is None
        assert service.persona_profile("p_does_not_exist") is None

        with as_user("u_view"):
            added = service.add_friend("u_view", "p_giulia")
        assert added["created"] is True

        again = service.persona_profile("p_giulia", "u_view")
        assert again["friendship"]["status"] == "accepted"
        assert again["friendship"]["persona_id"] == "p_giulia"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 好友关系
# --------------------------------------------------------------------------- #
def test_add_friend_is_idempotent_and_reuses_v2_friends(tmp_path):
    db, service = make_service(tmp_path)
    try:
        with as_user("u_friend"):
            first = service.add_friend("u_friend", "p_morten", request_message="hi")
            second = service.add_friend("u_friend", "p_morten")

        assert first["created"] is True
        assert second["created"] is False
        assert first["friend_id"] == second["friend_id"]
        assert first["friendship"]["status"] == "accepted"

        rows = db.list_friends(owner_user_id="u_friend")
        assert len(rows) == 1
        assert rows[0]["id"] == first["friend_id"]
        assert rows[0]["name"] == "Morten"
        assert rows[0]["status"] == "active"
        assert rows[0]["owner_user_id"] == "u_friend"

        friendships = service.list_friendships("u_friend")
        assert len(friendships["accepted"]) == 1
        assert friendships["pending"] == []

        # 其他用户拿不到这条好友关系
        assert db.list_friends(owner_user_id="u_other") == []
        assert service.list_friendships("u_other") == {"pending": [], "accepted": []}
        assert service.get_friendship("u_other", "p_morten") is None
    finally:
        db.close()


def test_remove_friend_cleans_both_tables(tmp_path):
    db, service = make_service(tmp_path)
    try:
        with as_user("u_rm"):
            service.add_friend("u_rm", "p_lin")

        assert service.remove_friend("u_rm", "p_lin") is True
        assert service.get_friendship("u_rm", "p_lin") is None
        assert db.list_friends(owner_user_id="u_rm") == []
        # 再删一次 → False（API 层会转 404）
        assert service.remove_friend("u_rm", "p_lin") is False
    finally:
        db.close()


# --- 回归：P2 显式 user_id 不再依赖请求上下文 ------------------------------- #
def test_add_friend_works_when_context_user_differs(tmp_path):
    """显式 user_id ≠ contextvar 当前用户时（非请求上下文）也不能失败。

    旧实现 ``db.create_friend`` 用 ``get_friend()`` 读回刚插入的行，
    而 ``get_friend`` 默认按 ``current_user_id()`` 过滤；后台任务/CLI/测试
    等场景会读到 None 触发 ``AssertionError``，并留下孤儿 friends 行。
    """
    db, service = make_service(tmp_path)
    try:
        # 模拟「当前上下文是 local，但显式操作 u_bg」的错位场景
        with as_user(LOCAL_USER_ID):
            first = service.add_friend("u_bg", "p_morten")
            second = service.add_friend("u_bg", "p_morten")

        assert first["created"] is True
        assert first["friendship"]["status"] == "accepted"
        assert second["created"] is False
        assert second["friend_id"] == first["friend_id"]

        # 不产生孤儿：friends 恰好 1 行，且 friendships 恰好指向它
        rows = db.list_friends(owner_user_id="u_bg")
        assert len(rows) == 1
        assert rows[0]["id"] == first["friend_id"]
        assert rows[0]["owner_user_id"] == "u_bg"
        assert rows[0]["status"] == "active"
        accepted = service.list_friendships("u_bg")["accepted"]
        assert len(accepted) == 1
        assert accepted[0]["friend_id"] == first["friend_id"]
    finally:
        db.close()


# --- 回归：P3 persona_posts 最后一页 next_cursor 必须为空 -------------------- #
def test_persona_posts_last_full_page_has_no_cursor(tmp_path):
    """恰好 3 条动态、limit=3 时不应再返回 cursor（否则多一次空页请求）。"""
    db, service = make_service(tmp_path)
    try:
        page = service.persona_posts("p_morten", user_id="u_cursor", limit=3)
        assert len(page["items"]) == 3
        assert page["next_cursor"] == ""

        # limit=2 时第一页仍有下一页，第二页（1 条）cursor 为空
        first = service.persona_posts("p_morten", user_id="u_cursor", limit=2)
        assert len(first["items"]) == 2
        assert first["next_cursor"]
        second = service.persona_posts(
            "p_morten", user_id="u_cursor", limit=2, cursor=first["next_cursor"]
        )
        assert len(second["items"]) == 1
        assert second["next_cursor"] == ""

        ids = [item["id"] for item in first["items"] + second["items"]]
        assert len(ids) == len(set(ids)) == 3
    finally:
        db.close()


# --- 回归：P3 pending 申请时 add_friend 应升级为 accepted ------------------- #
def test_add_friend_promotes_pending_request_to_accepted(tmp_path):
    """Persona 已发来好友申请（friendship=pending）时，"加好友" = 接受申请。"""
    cfg = social_config(
        type_weights={"like": 0.0, "comment": 0.0, "friend_request": 1.0}
    )
    db, service = make_service(tmp_path, config=cfg)
    try:
        with as_user("u_pending"):
            post = service.create_user_post(
                "u_pending", "climbing partner wanted", tags=["climbing"]
            )
            run(service.trigger_social_events("u_pending", post))
            assert service.materialize_due_events("u_pending") == 1

        # 物化后：friendship=pending，v2 friends=pending_request
        assert service.get_friendship("u_pending", "p_morten")["status"] == "pending"
        pending_friend_ids = [
            friend["id"] for friend in db.list_friends(owner_user_id="u_pending")
        ]
        assert len(pending_friend_ids) == 1
        assert (
            db.list_friends(owner_user_id="u_pending")[0]["status"] == "pending_request"
        )

        # 用户主动加好友 → 升级为 accepted，复用同一条 friend 行（不留孤儿）
        upgraded = service.add_friend("u_pending", "p_morten")
        assert upgraded["created"] is True
        assert upgraded["friendship"]["status"] == "accepted"
        assert upgraded["friend_id"] == pending_friend_ids[0]

        rows = db.list_friends(owner_user_id="u_pending")
        assert len(rows) == 1
        assert rows[0]["id"] == pending_friend_ids[0]
        assert rows[0]["status"] == "active"
        accepted = service.list_friendships("u_pending")["accepted"]
        assert len(accepted) == 1
        assert accepted[0]["status"] == "accepted"
        assert service.list_friendships("u_pending")["pending"] == []
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 用户动态删除（越权）
# --------------------------------------------------------------------------- #
def test_delete_user_post_ownership(tmp_path):
    db, service = make_service(tmp_path)
    try:
        post = service.create_user_post("u_owner", "mine")
        assert service.delete_user_post("u_intruder", post["id"]) is False
        assert service.get_user_post(post["id"]) is not None

        assert service.delete_user_post("u_owner", post["id"]) is True
        assert service.get_user_post(post["id"]) is None
        assert service.delete_user_post("u_owner", post["id"]) is False
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 社交事件
# --------------------------------------------------------------------------- #
def test_events_zero_delay_are_materialized_once(tmp_path):
    db, service = make_service(tmp_path, config=social_config())
    try:
        post = service.create_user_post(
            "u_ev", "I finally sent my first 6a climbing route!", tags=["climbing"]
        )
        result = run(service.trigger_social_events("u_ev", post))
        assert result["skipped"] == ""
        events = result["events"]
        assert len(events) == 1
        event = events[0]
        assert event["type"] == "like"
        # climbing 只命中 p_morten，结果完全确定
        assert event["persona"]["id"] == "p_morten"
        assert event["post_id"] == post["id"]

        scheduled = datetime.fromisoformat(event["scheduled_at"])
        created = datetime.fromisoformat(event["created_at"])
        assert abs((scheduled - created).total_seconds()) <= 2  # delay=0

        # 只是排期，未物化前动态上还没有点赞
        assert service.get_user_post(post["id"])["like_count"] == 0
        assert service.materialize_due_events("u_ev") == 1

        updated = service.get_user_post(post["id"])
        assert updated["like_count"] == 1
        likes = service.list_user_posts("u_ev")[0]["liked_by"]
        assert [lik["persona_id"] for lik in likes] == ["p_morten"]

        # 物化幂等：再来一次不重复点赞
        assert service.materialize_due_events("u_ev") == 0
        assert service.get_user_post(post["id"])["like_count"] == 1
    finally:
        db.close()


def test_event_delay_defers_listing_and_materialization(tmp_path):
    cfg = social_config(delay_min_s=3600, delay_max_s=3600)
    db, service = make_service(tmp_path, config=cfg)
    try:
        post = service.create_user_post("u_ev2", "climbing again", tags=["climbing"])
        result = run(service.trigger_social_events("u_ev2", post))
        assert len(result["events"]) == 1
        event = result["events"][0]

        scheduled = datetime.fromisoformat(event["scheduled_at"])
        created = datetime.fromisoformat(event["created_at"])
        assert (scheduled - created).total_seconds() >= 3600

        # 未到期：列表看不到、也不会物化
        assert service.list_events("u_ev2")["items"] == []
        assert service.materialize_due_events("u_ev2") == 0
        assert service.get_user_post(post["id"])["like_count"] == 0

        # 不 sleep：直接把排期改到过去，验证「到期 → 物化」路径
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                "UPDATE social_events SET scheduled_at=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", event["id"]),
            )

        assert service.materialize_due_events("u_ev2") == 1
        assert service.get_user_post(post["id"])["like_count"] == 1
        listed = service.list_events("u_ev2")["items"]
        assert [item["id"] for item in listed] == [event["id"]]
    finally:
        db.close()


def test_trigger_twice_does_not_create_duplicate_events(tmp_path):
    cfg = social_config(max_events_per_post=2)
    db, service = make_service(tmp_path, config=cfg)
    try:
        post = service.create_user_post("u_twice", "climbing day", tags=["climbing"])
        first = run(service.trigger_social_events("u_twice", post))
        assert len(first["events"]) == 1

        second = run(service.trigger_social_events("u_twice", post))
        assert second["skipped"] == "evaluated"
        assert [e["id"] for e in second["events"]] == [
            e["id"] for e in first["events"]
        ]

        with db._lock:  # noqa: SLF001
            total = db.conn.execute(
                "SELECT COUNT(*) AS n FROM social_events WHERE post_id=?", (post["id"],)
            ).fetchone()["n"]
            evaluated = db.conn.execute(
                "SELECT COUNT(*) AS n FROM social_event_attempts"
                " WHERE post_id=? AND event_type='evaluated'",
                (post["id"],),
            ).fetchone()["n"]
        assert total == 1
        assert evaluated == 1
    finally:
        db.close()


def test_daily_limit_stops_new_events(tmp_path):
    cfg = social_config(max_events_per_user_day=1)
    db, service = make_service(tmp_path, config=cfg)
    try:
        first_post = service.create_user_post(
            "u_day", "climbing today", tags=["climbing"]
        )
        first = run(service.trigger_social_events("u_day", first_post))
        assert len(first["events"]) == 1

        second_post = service.create_user_post("u_day", "jazz tonight", tags=["jazz"])
        second = run(service.trigger_social_events("u_day", second_post))
        assert second["skipped"] == "daily_limit"
        assert second["events"] == []

        assert _event_count(db, "u_day") == 1
        with db._lock:  # noqa: SLF001
            limited = db.conn.execute(
                "SELECT COUNT(*) AS n FROM social_event_attempts"
                " WHERE user_id=? AND reason='user_daily_limit'",
                ("u_day",),
            ).fetchone()["n"]
        assert limited == 1
    finally:
        db.close()


def test_persona_cooldown_blocks_repeat_events(tmp_path):
    cfg = social_config(persona_cooldown_s=3600, max_events_per_user_day=99)
    db, service = make_service(tmp_path, config=cfg)
    try:
        first_post = service.create_user_post(
            "u_cool", "climbing partner wanted", tags=["climbing"]
        )
        first = run(service.trigger_social_events("u_cool", first_post))
        assert [e["persona"]["id"] for e in first["events"]] == ["p_morten"]

        second_post = service.create_user_post(
            "u_cool", "another climbing session", tags=["climbing"]
        )
        second = run(service.trigger_social_events("u_cool", second_post))
        assert second["events"] == []
        assert second["skipped"] == "no_event"
        assert _event_count(db, "u_cool") == 1

        attempts = service.stats("u_cool")["attempts"]
        assert any(
            a["event_type"] == "no_action" and a["reason"] == "no_candidate"
            for a in attempts
        )
    finally:
        db.close()


@pytest.mark.parametrize("visibility", ["private", "friends"])
def test_non_public_posts_do_not_trigger_social_events(tmp_path, visibility):
    db, service = make_service(tmp_path, config=social_config())
    try:
        post = service.create_user_post(
            "u_vis", "climbing post", tags=["climbing"], visibility=visibility
        )
        result = run(service.trigger_social_events("u_vis", post))
        assert result["skipped"] == "not_public"
        assert result["events"] == []
        with db._lock:  # noqa: SLF001
            events = db.conn.execute(
                "SELECT COUNT(*) AS n FROM social_events WHERE post_id=?", (post["id"],)
            ).fetchone()["n"]
            attempts = db.conn.execute(
                "SELECT COUNT(*) AS n FROM social_event_attempts WHERE post_id=?",
                (post["id"],),
            ).fetchone()["n"]
        assert events == 0
        assert attempts == 0
    finally:
        db.close()


def test_friend_request_event_materializes_and_can_be_accepted(tmp_path):
    cfg = social_config(
        type_weights={"like": 0.0, "comment": 0.0, "friend_request": 1.0}
    )
    db, service = make_service(tmp_path, config=cfg)
    try:
        with as_user("u_fr"):
            post = service.create_user_post(
                "u_fr", "climbing partner wanted", tags=["climbing"]
            )
            result = run(service.trigger_social_events("u_fr", post))
            assert [e["type"] for e in result["events"]] == ["friend_request"]
            event = result["events"][0]
            assert event["status"] == "pending"

            assert service.materialize_due_events("u_fr") == 1
            pending = service.list_friendships("u_fr")["pending"]
            assert len(pending) == 1
            assert pending[0]["persona_id"] == "p_morten"

            v2_rows = db.list_friends(owner_user_id="u_fr")
            assert len(v2_rows) == 1
            assert v2_rows[0]["status"] == "pending_request"

            accepted = service.respond_friend_request(
                "u_fr", event["id"], accept=True
            )
            assert accepted is not None and accepted["status"] == "accepted"
            assert db.list_friends(owner_user_id="u_fr")[0]["status"] == "active"
            assert (
                service.list_friendships("u_fr")["accepted"][0]["status"] == "accepted"
            )

            listed = service.list_events("u_fr")["items"]
            assert listed[0]["status"] == "accepted"
            assert listed[0]["is_read"] is True
    finally:
        db.close()
