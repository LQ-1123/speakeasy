"""V3 数据结构与语料入库（schema/seed）幂等性测试。

覆盖点
------
- ``ensure_schema`` 反复执行不重复建表、不重复插入 persona / persona_post；
- ``seed_if_needed`` 的版本标记与 ``force=True`` 行为；
- 语料规模（12 persona / 36 posts）与基本一致性（每人 3 条、id 唯一）。

全部使用 ``tmp_path`` 下的临时数据库，不触碰 ``data/``。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.v2.db import DB  # noqa: E402
from app.v3.schema import (  # noqa: E402
    SEED_KEY,
    SEED_VERSION,
    ensure_schema,
    seed_if_needed,
)
from app.v3.seed_data import PERSONAS, POSTS  # noqa: E402


def _make_db(tmp_path: Path) -> DB:
    db = DB(str(tmp_path / "v3_schema.db"))
    db.init()
    return db


def _count(db: DB, table: str) -> int:
    with db._lock:  # noqa: SLF001 - 测试里直接读计数
        return int(db.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


def test_ensure_schema_is_idempotent(tmp_path):
    db = _make_db(tmp_path)
    try:
        ensure_schema(db)

        assert _count(db, "personas") == len(PERSONAS) == 12
        assert _count(db, "persona_posts") == len(POSTS) == 36

        # 第二次、第三次执行：建表 + 语料都不重复
        ensure_schema(db)
        ensure_schema(db)
        assert _count(db, "personas") == 12
        assert _count(db, "persona_posts") == 36

        # seed_if_needed 在已入库后返回 0（没有新写入）
        assert seed_if_needed(db) == 0

        # v3_meta 版本标记写对了
        row = db.conn.execute(
            "SELECT value FROM v3_meta WHERE key=?", (SEED_KEY,)
        ).fetchone()
        assert row is not None and row["value"] == SEED_VERSION
    finally:
        db.close()


def test_seed_data_shapes_are_consistent(tmp_path):
    db = _make_db(tmp_path)
    try:
        ensure_schema(db)

        # 每条 persona_post 的 id 唯一、persona 外键都存在
        with db._lock:  # noqa: SLF001
            post_rows = db.conn.execute(
                "SELECT id, persona_id FROM persona_posts"
            ).fetchall()
            persona_ids = {
                str(r["id"]) for r in db.conn.execute("SELECT id FROM personas").fetchall()
            }
        post_ids = [str(r["id"]) for r in post_rows]
        assert len(post_ids) == 36
        assert len(set(post_ids)) == 36
        assert {str(r["persona_id"]) for r in post_rows} == persona_ids

        # 每个人恰好 3 条动态（语料契约）
        with db._lock:  # noqa: SLF001
            per = db.conn.execute(
                "SELECT persona_id, COUNT(*) AS n FROM persona_posts GROUP BY persona_id"
            ).fetchall()
        assert len(per) == 12
        assert all(int(r["n"]) == 3 for r in per)

        # persona 字段完整（信息流/详情页依赖）
        with db._lock:  # noqa: SLF001
            row = db.conn.execute(
                "SELECT * FROM personas WHERE id='p_giulia'"
            ).fetchone()
        assert row is not None
        assert row["nickname"] == "Giulia"
        assert "jazz" in row["interests"]
    finally:
        db.close()


def test_seed_force_does_not_duplicate(tmp_path):
    db = _make_db(tmp_path)
    try:
        ensure_schema(db)

        # force=True 只重新 upsert persona，已存在的 post 跳过 → 不新增
        written = seed_if_needed(db, force=True)
        assert written == 0
        assert _count(db, "personas") == 12
        assert _count(db, "persona_posts") == 36

        # 数据没被重复，也没有把已有动态删掉
        with db._lock:  # noqa: SLF001
            ids = [
                str(r["id"])
                for r in db.conn.execute("SELECT id FROM persona_posts").fetchall()
            ]
        assert len(ids) == len(set(ids)) == 36
    finally:
        db.close()
