"""生成批次恢复 / 最终过期清理 / 人物供给跟随活跃关系 的回归测试。

覆盖 task-7 的 5 个验收点：
1) 同一天连续 ``generate_daily(force=True)``：确定性 id + ``INSERT OR IGNORE`` → 不翻倍
2) 批次中途失败：不落 ``daily_world`` 成功行；重试按**同一批人数**补齐且无重复
3) ``cleanup``：过期 ``daily_world`` 行 / 孤儿动态 / 过期 ``waiting_voice`` 游客最终回收，
   且「互动动态延长 7 天」的既有行为不回退
4) ``supply_target`` 跟随近 7 天活跃关系与内容缺口，显式 ``SE_DAILY_PERSONAS`` 优先
5) 公共世界生成不吃某个用户的 level（语言轮换保留）
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.v2.db import DB  # noqa: E402
from app.v3 import daily_world  # noqa: E402
from app.v3.daily_world import (  # noqa: E402
    DAILY_PERSONAS,
    POSTS_PER_PERSONA,
    generate_daily,
    iso,
    now_local,
    supply_target,
)
from app.v3.lifecycle import cleanup, mark_interaction  # noqa: E402
from app.v3.schema import ensure_schema  # noqa: E402


@pytest.fixture()
def daily_db(tmp_path, monkeypatch):
    """离线、空库、无显式批次配置：走 supply_target 的自适应路径。"""
    monkeypatch.setenv("SE_MEDIA_ENABLED", "0")
    monkeypatch.delenv("SE_DAILY_PERSONAS", raising=False)
    db = DB(str(tmp_path / "recovery.db"))
    db.init()
    ensure_schema(db, seed=False)
    yield db
    db.close()


def _count(db: DB, sql: str, params: tuple = ()) -> int:
    row = db.conn.execute(sql, params).fetchone()
    return int(row[0] if row is not None else 0)


def _guest_ids(db: DB) -> list[str]:
    return [
        str(row["id"])
        for row in db.conn.execute(
            "SELECT id FROM personas WHERE lifecycle='guest' ORDER BY id"
        ).fetchall()
    ]


def _daily_row(db: DB, day: str):
    return db.conn.execute("SELECT * FROM daily_world WHERE day=?", (day,)).fetchone()


def _insert_orphan_post(db: DB, post_id: str, persona_id: str) -> None:
    """绕过外键直接落一条孤儿动态（模拟老库/手工删人留下的残留）。"""
    raw = sqlite3.connect(str(db.path))
    try:
        raw.execute(
            "INSERT INTO persona_posts (id, persona_id, content, created_at)"
            " VALUES (?,?,?,?)",
            (post_id, persona_id, "orphan post", iso(now_local())),
        )
        raw.commit()
    finally:
        raw.close()


# --------------------------------------------------------------------------- #
# 1) 幂等：force 重跑不重复造人/造帖
# --------------------------------------------------------------------------- #
def test_generate_daily_force_rerun_does_not_duplicate(daily_db):
    day = "2026-11-06"
    first = asyncio.run(generate_daily(daily_db, None, day=day, force=True))
    assert first["skipped"] is False
    # 空库冷启动：按配置批次规模起世界
    assert first["personas"] == DAILY_PERSONAS
    assert first["posts"] == DAILY_PERSONAS * POSTS_PER_PERSONA

    # 确定性 id：p_d{YYYYMMDD}{index:02d} / pp_{persona_id}_{index:02d}
    expected_personas = {f"p_d20261106{index:02d}" for index in range(DAILY_PERSONAS)}
    assert set(_guest_ids(daily_db)) == expected_personas
    post_ids = {
        str(row["id"]) for row in daily_db.conn.execute("SELECT id FROM persona_posts")
    }
    expected_posts = {
        f"pp_p_d20261106{index:02d}_{post:02d}"
        for index in range(DAILY_PERSONAS)
        for post in range(POSTS_PER_PERSONA)
    }
    assert post_ids == expected_posts

    # force 重跑：同一批人数补跑，但已存在行全部跳过 → 数量不翻倍
    second = asyncio.run(generate_daily(daily_db, None, day=day, force=True))
    assert second["skipped"] is False
    assert second["created_personas"] == 0
    assert second["personas"] == DAILY_PERSONAS
    assert len(_guest_ids(daily_db)) == DAILY_PERSONAS
    assert _count(daily_db, "SELECT COUNT(*) FROM persona_posts") == len(expected_posts)

    # 成功行不被纯重跑覆盖
    row = _daily_row(daily_db, day)
    assert row is not None
    assert int(row["personas"]) == DAILY_PERSONAS
    assert int(row["posts"]) == DAILY_PERSONAS * POSTS_PER_PERSONA

    # 非 force 调用照旧幂等跳过
    skipped = asyncio.run(generate_daily(daily_db, None, day=day))
    assert skipped["skipped"] is True


# --------------------------------------------------------------------------- #
# 2) 批次中途失败：不落成功行、重试补齐且无重复
# --------------------------------------------------------------------------- #
def test_generate_daily_resumes_after_mid_batch_failure(daily_db, monkeypatch):
    day = "2026-11-07"
    real_insert = daily_world._insert_persona  # noqa: SLF001 - 测试注入故障
    state = {"calls": 0}

    async def flaky_insert(db, persona, *, day, expires_at, index=None):
        state["calls"] += 1
        if state["calls"] == 2:  # 第 2 个人物炸掉
            raise RuntimeError("boom")
        return await real_insert(db, persona, day=day, expires_at=expires_at, index=index)

    monkeypatch.setattr(daily_world, "_insert_persona", flaky_insert)
    failed = asyncio.run(generate_daily(daily_db, None, day=day))
    assert failed["skipped"] is False
    assert failed["failed_personas"] == 1
    assert failed["personas"] == DAILY_PERSONAS - 1
    # 中断不写成功行，但保留「这一批要生成几个人」的标记
    assert _daily_row(daily_db, day) is None
    assert daily_world._read_batch_target(daily_db, day) == DAILY_PERSONAS  # noqa: SLF001
    assert len(_guest_ids(daily_db)) == DAILY_PERSONAS - 1

    # 修复后重试：按同一批人数补齐（不会因为供给目标变小而漏人）
    monkeypatch.setattr(daily_world, "_insert_persona", real_insert)
    retried = asyncio.run(generate_daily(daily_db, None, day=day))
    assert retried["skipped"] is False
    assert retried["resumed"] is True
    assert retried["personas"] == DAILY_PERSONAS

    row = _daily_row(daily_db, day)
    assert row is not None
    assert int(row["personas"]) == DAILY_PERSONAS
    assert int(row["posts"]) == DAILY_PERSONAS * POSTS_PER_PERSONA
    ids = _guest_ids(daily_db)
    assert len(ids) == len(set(ids)) == DAILY_PERSONAS
    assert set(ids) == {f"p_d20261107{index:02d}" for index in range(DAILY_PERSONAS)}
    assert _count(daily_db, "SELECT COUNT(*) FROM persona_posts") == (
        DAILY_PERSONAS * POSTS_PER_PERSONA
    )
    # 成功收尾后标记清掉
    assert daily_world._read_batch_target(daily_db, day) is None  # noqa: SLF001


# --------------------------------------------------------------------------- #
# 3) 最终清理：过期批次 / 孤儿动态 / waiting_voice 游客 + 既有延长行为
# --------------------------------------------------------------------------- #
def test_cleanup_prunes_batches_orphans_and_waiting_voice_guests(daily_db):
    day = "2026-11-08"
    asyncio.run(generate_daily(daily_db, None, day=day))
    posts = daily_db.conn.execute("SELECT * FROM persona_posts ORDER BY id").fetchall()
    keep_post = posts[0]
    keep_persona = str(keep_post["persona_id"])
    mark_interaction(daily_db, post_id=str(keep_post["id"]))

    now = now_local()
    past = iso(now - timedelta(hours=1))
    old_day = (now - timedelta(days=31)).date().isoformat()
    with daily_db._lock, daily_db.conn:
        daily_db.conn.execute("UPDATE persona_posts SET expires_at=?", (past,))
        daily_db.conn.execute("UPDATE personas SET expires_at=? WHERE lifecycle='guest'", (past,))
        # 过期且一直在等声线的游客（正常流程里会永久卡住的那批）
        daily_db.conn.execute(
            "INSERT INTO personas (id, nickname, lifecycle, status, expires_at, created_at)"
            " VALUES ('p_voiceless','Voiceless','guest','waiting_voice',?,?)",
            (past, iso(now - timedelta(hours=2))),
        )
        daily_db.conn.execute(
            "INSERT INTO persona_posts (id, persona_id, content, created_at, expires_at)"
            " VALUES ('pp_voiceless_00','p_voiceless','hello',?,?)",
            (iso(now - timedelta(hours=2)), past),
        )
        # 31 天前的批次账本（今天这一批的账本由 generate_daily 写入，必须保留）
        daily_db.conn.execute(
            "INSERT INTO daily_world (day, generated_at, personas, posts, source)"
            " VALUES (?,?,?,?,?)",
            (old_day, iso(now - timedelta(days=31)), 3, 9, "template"),
        )
    _insert_orphan_post(daily_db, "pp_orphan_recovery", "p_ghost_recovery")

    result = cleanup(daily_db)

    # 旧键保持向后兼容
    for key in ("extended_posts", "deleted_posts", "deleted_personas"):
        assert key in result
    # 新增统计键
    assert result["deleted_daily_world"] >= 1
    assert result["deleted_orphan_posts"] >= 1
    assert result["recycled_waiting_voice"] >= 1
    assert "deleted_batch_markers" in result

    # 过期批次行删掉，今天的批次保留
    days = {str(row["day"]) for row in daily_db.conn.execute("SELECT day FROM daily_world")}
    assert old_day not in days
    assert day in days

    # waiting_voice 游客最终回收（连带其动态）
    assert _count(
        daily_db, "SELECT COUNT(*) FROM personas WHERE id='p_voiceless'"
    ) == 0
    assert _count(
        daily_db, "SELECT COUNT(*) FROM persona_posts WHERE persona_id='p_voiceless'"
    ) == 0

    # 孤儿动态删掉
    assert _count(
        daily_db, "SELECT COUNT(*) FROM persona_posts WHERE id='pp_orphan_recovery'"
    ) == 0

    # 既有行为不回退：被互动的动态仍存在，且过期时间被延长到 7 天窗口内
    kept = daily_db.conn.execute(
        "SELECT expires_at, interacted FROM persona_posts WHERE id=?", (keep_post["id"],)
    ).fetchone()
    assert kept is not None and int(kept["interacted"]) == 1
    assert str(kept["expires_at"]) > past
    # 被互动的人物转正，不参与游客回收
    assert _count(
        daily_db, "SELECT COUNT(*) FROM personas WHERE id=?", (keep_persona,)
    ) == 1
    # 未互动的过期动态被删
    assert result["deleted_posts"] >= 1


# --------------------------------------------------------------------------- #
# 4) 供给跟随近 7 天活跃关系 / 内容缺口；显式配置优先
# --------------------------------------------------------------------------- #
def test_supply_target_follows_recent_relationships(daily_db, monkeypatch):
    monkeypatch.delenv("SE_DAILY_PERSONAS", raising=False)
    # 空库 + 无活跃关系 → 最小供给（≤3）
    assert supply_target(daily_db) <= 3

    # 内容充足（15 条可见动态）但依然没有活跃关系 → 仍是最小供给
    asyncio.run(generate_daily(daily_db, None, day="2026-11-09"))
    assert supply_target(daily_db) <= 3

    now = now_local()
    guest_ids = _guest_ids(daily_db)
    assert len(guest_ids) == DAILY_PERSONAS
    with daily_db._lock, daily_db.conn:
        daily_db.conn.execute(
            "INSERT INTO personas (id, nickname, lifecycle, created_at)"
            " VALUES ('p_supply_extra','Extra','permanent',?)",
            (iso(now),),
        )
        for index, pid in enumerate((guest_ids + ["p_supply_extra"])[:6]):
            daily_db.conn.execute(
                "INSERT INTO friendships (id, user_id, persona_id, status, friend_id,"
                " request_message, created_at, updated_at)"
                " VALUES (?,?,?, 'accepted', '', '', ?,?)",
                (
                    f"fr_recent_{index}",
                    f"u_recent_{index}",
                    pid,
                    iso(now - timedelta(hours=index + 1)),
                    iso(now - timedelta(hours=index + 1)),
                ),
            )
    # 6 个近 7 天的 accepted 关系（去重 persona）→ 供给上调
    assert supply_target(daily_db) >= 4

    # 全部滑出 7 天窗口 → 不再计入
    with daily_db._lock, daily_db.conn:
        daily_db.conn.execute(
            "UPDATE friendships SET updated_at=?", (iso(now - timedelta(days=8)),)
        )
    assert supply_target(daily_db) <= 3

    # 显式环境变量优先（运营/测试可强制规模）
    monkeypatch.setenv("SE_DAILY_PERSONAS", "3")
    assert supply_target(daily_db) == 3


# --------------------------------------------------------------------------- #
# 5) 公共世界生成不吃某个用户的 level（语言轮换保留）
# --------------------------------------------------------------------------- #
def test_world_generation_does_not_use_user_level(daily_db, monkeypatch):
    day = "2026-11-10"
    with daily_db._lock, daily_db.conn:
        daily_db.conn.execute(
            "INSERT OR REPLACE INTO user_profiles"
            " (user_id, target_languages, fact_sheet, updated_at)"
            " VALUES ('u_level', ?, ?, ?)",
            (
                json.dumps(["en"]),
                json.dumps({"language_profile": {"levels": {"en": "比较流利"}}}),
                iso(now_local()),
            ),
        )

    captured: list[dict] = []

    async def fake_llm_persona(llm, index, day, **kwargs):
        captured.append({"index": index, "day": day, **kwargs})
        return None

    monkeypatch.setattr(daily_world, "_llm_persona", fake_llm_persona)
    result = asyncio.run(generate_daily(daily_db, object(), day=day))
    assert result["skipped"] is False
    assert len(captured) == DAILY_PERSONAS

    for call in captured:
        # 世界生成不再把某个用户的水平传给人物 prompt
        assert not str(call.get("level") or "").strip(), call
        # 语言轮换保留（社区在练语言）
        assert call.get("language") == "en"
    assert "比较流利" not in json.dumps(captured, ensure_ascii=False)
    assert "比较流利" not in json.dumps(result, ensure_ascii=False)
