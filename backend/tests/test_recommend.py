"""个性化推荐流（快照游标 + 综合排序 + 20% 探索）测试。

覆盖 task-6 的验收点：
1) 翻页稳定（同 cursor 同顺序、不重复）；
2) 兴趣命中排序更高；
3) 已看 48h 内降权（仍保留）；
4) 20% 探索位；
5) 在练语言过滤保持；
6) 本人动态全部包含且优先（三种可见性、is_mine=True）；
7) ``_user_interests`` 修复：用户之间兴趣不串；
8) 快照过期 / 非法 / 他人 cursor 不 500（自动重建）。

说明
----
- 每个用例独立临时库；``ensure_schema(db, seed=False)`` 后手工插入少量人物/动态，
  这样排序断言不受 36 条种子数据干扰。
- 不 sleep：过期场景直接改 ``expires_at``。
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.v2.db import DB  # noqa: E402
from app.v3 import recommend, taxonomy  # noqa: E402
from app.v3.identity import reset_current_user, set_current_user  # noqa: E402
from app.v3.schema import ensure_schema  # noqa: E402
from app.v3.service import V3Service  # noqa: E402


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _iso(dt: datetime | None = None) -> str:
    value = dt or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def make_service(tmp_path: Path) -> tuple[DB, V3Service]:
    db = DB(str(tmp_path / "recommend.db"))
    db.init()
    # 不写种子数据：排序断言需要可控候选集
    ensure_schema(db, seed=False)
    recommend.ensure_recommend_schema(db)
    engines = SimpleNamespace(llm=SimpleNamespace(uses_mock=True))
    return db, V3Service(db, engines, str(tmp_path))


def add_persona(
    db: DB,
    persona_id: str,
    *,
    language: str = "en",
    interests: list[str] | None = None,
    status: str = "active",
) -> None:
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO personas (id, nickname, bio, personality, interests, language,"
            " country, city, timezone, occupation, age, gender, status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                persona_id,
                f"P {persona_id}",
                "",
                "",
                json.dumps(interests or [], ensure_ascii=False),
                language,
                "",
                "",
                "UTC",
                "",
                25,
                "",
                status,
                _iso(),
            ),
        )


def add_post(
    db: DB,
    post_id: str,
    persona_id: str,
    *,
    tags: list[str] | None = None,
    created_at: str | None = None,
    language: str = "en",
    visibility: str = "public",
    expires_at: str = "",
) -> None:
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO persona_posts (id, persona_id, content, media, tags, language,"
            " visibility, like_count, comment_count, created_at, expires_at)"
            " VALUES (?,?,?,?,?,?,?,0,0,?,?)",
            (
                post_id,
                persona_id,
                f"post {post_id}",
                "[]",
                json.dumps(tags or [], ensure_ascii=False),
                language,
                visibility,
                created_at or _iso(),
                expires_at,
            ),
        )


def set_interests(db: DB, user_id: str, interests: list[str]) -> None:
    db.update_profile(user_id=user_id, fact_sheet={"interests": list(interests)})


def ids_of(snapshot: dict) -> list[str]:
    return [str(item["id"]) for item in snapshot["items"]]


@contextmanager
def as_user(user_id: str):
    token = set_current_user(user_id)
    try:
        yield user_id
    finally:
        reset_current_user(token)


# --------------------------------------------------------------------------- #
# 1) 翻页稳定
# --------------------------------------------------------------------------- #
def test_snapshot_cursor_pagination_stable(tmp_path):
    db, service = make_service(tmp_path)
    try:
        for i in range(12):
            add_persona(db, f"p_pg_{i:02d}")
            add_post(db, f"pp_pg_{i:02d}", f"p_pg_{i:02d}")

        first = service.feed("u_page", limit=5)
        assert len(first["items"]) == 5
        assert first["next_cursor"].startswith("snap:")
        cursor = first["next_cursor"]

        second = service.feed("u_page", cursor=cursor, limit=5)
        assert len(second["items"]) == 5
        ids1 = [item["id"] for item in first["items"]]
        ids2 = [item["id"] for item in second["items"]]
        assert set(ids1).isdisjoint(ids2), "翻页出现重复条目"

        # 同一个 cursor 再请求：同一份快照 → 完全相同的顺序
        again = service.feed("u_page", cursor=cursor, limit=5)
        assert [item["id"] for item in again["items"]] == ids2

        # 一直翻到底：不重不漏、共 12 条
        collected = ids1 + ids2
        current = again
        guard = 0
        while current["next_cursor"]:
            current = service.feed("u_page", cursor=current["next_cursor"], limit=5)
            collected.extend(item["id"] for item in current["items"])
            guard += 1
            assert guard < 10, "游标分页疑似死循环"
        assert len(collected) == len(set(collected)) == 12
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 2) 兴趣命中排序
# --------------------------------------------------------------------------- #
def test_interest_tag_match_ranks_higher(tmp_path):
    db, service = make_service(tmp_path)
    try:
        set_interests(db, "u_interest", ["coffee"])
        stamp = _iso()  # 同龄动态：排除新鲜度差异
        add_persona(db, "p_hit")
        add_post(db, "pp_hit", "p_hit", tags=["coffee"], created_at=stamp)
        add_persona(db, "p_miss")
        add_post(db, "pp_miss", "p_miss", tags=[], created_at=stamp)

        items = service.feed("u_interest", limit=10)["items"]
        ids = [item["id"] for item in items]
        assert ids.index("pp_hit") < ids.index("pp_miss"), "兴趣命中的动态应排在前面"

        # 别名也算命中（「咖啡」与 coffee 是同一稳定 ID）
        set_interests(db, "u_alias", ["咖啡"])
        alias_items = service.feed("u_alias", limit=10)["items"]
        alias_ids = [item["id"] for item in alias_items]
        assert alias_ids.index("pp_hit") < alias_ids.index("pp_miss")
        assert taxonomy.canonical_tags(["咖啡"]) == ["coffee"]
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 3) 已看降权
# --------------------------------------------------------------------------- #
def test_seen_item_demoted_but_kept(tmp_path):
    db, service = make_service(tmp_path)
    try:
        stamp = _iso()
        for i in range(6):
            add_persona(db, f"p_seen_{i}")
            add_post(db, f"pp_seen_{i}", f"p_seen_{i}", created_at=stamp)

        first_snapshot = recommend.build_snapshot(db, "u_seen")
        first_id = ids_of(first_snapshot)[0]

        recommend.mark_seen(db, "u_seen", [{"kind": "persona_post", "id": first_id}])
        second_snapshot = recommend.build_snapshot(db, "u_seen")
        ids = ids_of(second_snapshot)

        assert len(ids) == 6
        assert ids[0] != first_id, "48h 内看过的条目不应再排第一"
        assert first_id in ids, "已看只降权，仍保留在流里（避免空流）"
    finally:
        db.close()


def test_mark_seen_is_idempotent(tmp_path):
    db, _service = make_service(tmp_path)
    try:
        recommend.mark_seen(db, "u_idem", [{"kind": "persona_post", "id": "pp_1"}])
        recommend.mark_seen(db, "u_idem", [("persona_post", "pp_1")])
        recommend.mark_seen(db, "u_idem", ["persona_post:pp_1"])
        with db._lock:  # noqa: SLF001
            rows = db.conn.execute(
                "SELECT item_key, COUNT(*) AS n FROM v3_feed_seen WHERE user_id=? GROUP BY item_key",
                ("u_idem",),
            ).fetchall()
        assert [(row["item_key"], row["n"]) for row in rows] == [("persona_post:pp_1", 1)]
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 4) 20% 探索位
# --------------------------------------------------------------------------- #
def test_exploration_slots_keep_low_score_candidates(tmp_path):
    db, _service = make_service(tmp_path)
    try:
        set_interests(db, "u_explore", ["coffee"])
        fresh = _iso()
        old = _iso(datetime.now(timezone.utc) - timedelta(days=60))
        # 8 条高分（新鲜 + 兴趣命中）；2 条低分（很旧，只剩兴趣分）→ 本会被正序挤到最后
        for i in range(8):
            add_persona(db, f"p_hi_{i}")
            add_post(db, f"pp_hi_{i}", f"p_hi_{i}", tags=["coffee"], created_at=fresh)
        for i in range(2):
            add_persona(db, f"p_lo_{i}")
            add_post(db, f"pp_lo_{i}", f"p_lo_{i}", tags=["coffee"], created_at=old)

        snapshot = recommend.build_snapshot(db, "u_explore")
        ids = ids_of(snapshot)
        assert len(ids) == 10
        assert {"pp_lo_0", "pp_lo_1"} <= set(ids), "低分候选应通过探索位进入快照"
        low_score = sum(1 for item_id in ids if item_id.startswith("pp_lo_"))
        ratio = low_score / len(ids)
        assert 0.1 <= ratio <= 0.3, f"探索位应约为 20%（实际 {ratio:.0%}）"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 5) 语言过滤
# --------------------------------------------------------------------------- #
def test_target_language_filter_kept(tmp_path):
    db, service = make_service(tmp_path)
    try:
        db.update_profile(user_id="u_lang", target_languages=["en", "de"])
        add_persona(db, "p_en", language="en")
        add_post(db, "pp_en", "p_en", language="en")
        add_persona(db, "p_de", language="de")
        add_post(db, "pp_de", "p_de", language="de")
        add_persona(db, "p_fr", language="fr")
        add_post(db, "pp_fr", "p_fr", language="fr")

        items = service.feed("u_lang", limit=40)["items"]
        languages = {
            item["author"]["language"]
            for item in items
            if item["kind"] == "persona_post"
        }
        assert languages <= {"en", "de"}, languages
        assert "pp_fr" not in {item["id"] for item in items}
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 6) 本人动态必在且优先
# --------------------------------------------------------------------------- #
def test_own_posts_always_included_and_first(tmp_path):
    db, service = make_service(tmp_path)
    try:
        for i in range(3):
            add_persona(db, f"p_own_{i}")
            add_post(db, f"pp_own_{i}", f"p_own_{i}")

        posts = [
            service.create_user_post("u_owner", "public one", visibility="public"),
            service.create_user_post("u_owner", "friends one", visibility="friends"),
            service.create_user_post("u_owner", "private one", visibility="private"),
        ]

        feed = service.feed("u_owner", limit=40)
        items = {item["id"]: item for item in feed["items"]}
        for post in posts:
            assert post["id"] in items, f"本人动态缺失: {post['visibility']}"
            assert items[post["id"]]["is_mine"] is True

        assert [item["kind"] for item in feed["items"][:3]] == ["user_post"] * 3, (
            "本人动态应排在最前"
        )

        # 首屏（limit=2）也必须是本人动态，而不是人物动态
        page = service.feed("u_owner", limit=2)
        assert [item["kind"] for item in page["items"]] == ["user_post", "user_post"]
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 7) _user_interests 修复：用户之间不串
# --------------------------------------------------------------------------- #
def test_user_interests_do_not_leak_between_users(tmp_path):
    db, service = make_service(tmp_path)
    try:
        stamp = _iso()
        add_persona(db, "p_coffee")
        add_post(db, "pp_coffee", "p_coffee", tags=["coffee"], created_at=stamp)
        add_persona(db, "p_hiking")
        add_post(db, "pp_hiking", "p_hiking", tags=["hiking"], created_at=stamp)
        set_interests(db, "u_a", ["coffee"])
        set_interests(db, "u_b", ["hiking"])

        with as_user("u_a"):
            assert service._user_interests() == {"coffee"}
            ids_a = ids_of(recommend.build_snapshot(db, "u_a"))
        with as_user("u_b"):
            assert service._user_interests() == {"hiking"}
            ids_b = ids_of(recommend.build_snapshot(db, "u_b"))

        assert ids_a.index("pp_coffee") < ids_a.index("pp_hiking")
        assert ids_b.index("pp_hiking") < ids_b.index("pp_coffee")
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 8) 快照过期 / 非法 / 他人 cursor：不 500，自动重建
# --------------------------------------------------------------------------- #
def test_invalid_or_expired_cursor_rebuilds(tmp_path):
    db, service = make_service(tmp_path)
    try:
        for i in range(6):
            add_persona(db, f"p_cur_{i}")
            add_post(db, f"pp_cur_{i}", f"p_cur_{i}")

        first = service.feed("u_cur", limit=3)
        cursor = first["next_cursor"]
        assert cursor

        # 各种非法 cursor：200 + 自动重建（有 items、有 next_cursor）
        for bad in (
            "not-a-cursor",
            "snap::3",
            "snap:bogus:abc",
            "snap:missing:0",
            "2026-01-01T00:00:00+00:00|pp_pg_00",
        ):
            data = service.feed("u_cur", cursor=bad, limit=3)
            assert data["items"], f"非法 cursor 应重建并返回内容: {bad}"
            assert data["next_cursor"], bad

        # 过期快照：重建出另一份快照
        snapshot_id = cursor.split(":")[1]
        past = _iso(datetime.now(timezone.utc) - timedelta(minutes=1))
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                "UPDATE v3_feed_snapshots SET expires_at=? WHERE id=?", (past, snapshot_id)
            )
        rebuilt = service.feed("u_cur", cursor=cursor, limit=3)
        assert rebuilt["items"]
        assert rebuilt["next_cursor"].startswith("snap:")
        assert rebuilt["next_cursor"].split(":")[1] != snapshot_id

        # 别人的快照游标：归属校验失败 → 重建自己的快照，不泄露对方顺序
        other = service.feed("u_other", limit=3)
        other_snapshot_id = other["next_cursor"].split(":")[1]
        hijacked = service.feed("u_cur", cursor=other["next_cursor"], limit=3)
        assert hijacked["items"]
        assert hijacked["next_cursor"].split(":")[1] != other_snapshot_id
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 附加：profile_state 的 interest_labels（additive 字段）
# --------------------------------------------------------------------------- #
def test_profile_state_exposes_interest_labels(tmp_path):
    db, service = make_service(tmp_path)
    try:
        db.update_profile(
            user_id="u_labels", fact_sheet={"interests": ["coffee", "自制酸面包"]}
        )
        state = service.profile_state("u_labels")
        labels = state["interest_labels"]
        assert labels["coffee"]["zh"] == "咖啡"
        assert labels["coffee"]["known"] is True
        custom_keys = [key for key in labels if key.startswith(taxonomy.CUSTOM_PREFIX)]
        assert custom_keys, "未知自定义标签应保留 custom: 稳定 ID"
        assert all(labels[key]["zh"] for key in labels)
        # 旧字段不受影响（additive）
        assert state["profile"]["fact_sheet"]["interests"]
        assert "setup" in state and "missing" in state["setup"]
    finally:
        db.close()
