"""统一标签体系（稳定 ID + 多语言 + 别名迁移）验收。

覆盖：
- 中英文别名归一到同一 ID；
- 未知自定义标签保留（custom: 前缀，不丢原文信息）；
- 显示名按语言取，未知退回可读文本；
- 列表清洗：去重/保序/上限；
- 历史数据迁移幂等：资料 fact_sheet、人物兴趣、人物/用户动态标签。
"""

from __future__ import annotations

import json

import pytest

from app.v2.db import DB
from app.v3 import taxonomy
from app.v3.schema import ensure_schema


def test_aliases_collapse_to_same_id():
    for raw in ("咖啡", "coffee", "Coffee", "#coffee", "espresso", "cafe", "咖啡馆"):
        assert taxonomy.canonical_tag(raw) == "coffee"
    for raw in ("骑行", "bicycle", "bike", "单车"):
        assert taxonomy.canonical_tag(raw) == "cycling"
    for raw in ("摄影", "photo", "camera", "拍照"):
        assert taxonomy.canonical_tag(raw) == "photography"


def test_mixed_separators_and_whitespace():
    assert taxonomy.canonical_tag("  #咖啡 ") == "coffee"
    assert taxonomy.canonical_tag("咖啡/摄影") == "coffee"
    assert taxonomy.canonical_tag("music / guitar") == "music"
    assert taxonomy.canonical_tag("COFFEE SHOP") == "coffee"


def test_unknown_tag_is_preserved_as_custom():
    tag = taxonomy.canonical_tag("自制酸面包")
    assert tag.startswith("custom:")
    assert taxonomy.canonical_tag(tag) == tag  # 幂等
    # 未知英文也可读 slug
    assert taxonomy.canonical_tag("sourdough baking") == "custom:sourdough-baking"
    # 显示名不丢
    assert taxonomy.label("custom:sourdough-baking") == "sourdough baking"


def test_empty_and_dedup_and_limit():
    assert taxonomy.canonical_tag("") == ""
    assert taxonomy.canonical_tag(None) == ""
    values = ["咖啡", "coffee", "咖啡", "音乐", "music", "摄影", "旅行", "阅读", "游戏", "瑜伽"]
    cleaned = taxonomy.canonical_tags(values, limit=5)
    assert cleaned == ["coffee", "music", "photography", "travel", "reading"]


def test_labels_multilingual():
    assert taxonomy.label("coffee", "zh") == "咖啡"
    assert taxonomy.label("coffee", "en") == "Coffee"
    mapping = taxonomy.labels_map(["coffee", "custom:mystery"])
    assert mapping["coffee"]["known"] is True and mapping["coffee"]["zh"] == "咖啡"
    assert mapping["custom:mystery"]["known"] is False
    assert taxonomy.labels_for(["coffee", "cycling"], "en") == ["Coffee", "Cycling"]


def test_every_tag_has_zh_and_en_label():
    for tag_id, meta in taxonomy.TAGS.items():
        assert meta.get("zh") and meta.get("en"), tag_id
        assert taxonomy.canonical_tag(tag_id) == tag_id


@pytest.fixture()
def db(tmp_path):
    database = DB(str(tmp_path / "taxonomy.db"))
    database.init()
    ensure_schema(database, seed=True)
    yield database
    database.close()


def test_migrate_existing_free_text(db):
    # 资料：中英文混排 + 重复
    db.update_profile(
        fact_sheet={"interests": ["咖啡", "coffee", "摄影", "自制酸面包"]},
        user_id="u_tax",
    )
    # 人物：种子数据里的英文兴趣
    pid = db.conn.execute("SELECT id, interests FROM personas LIMIT 1").fetchone()["id"]
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "UPDATE personas SET interests=? WHERE id=?",
            (json.dumps(["Coffee", "骑行", "music"], ensure_ascii=False), pid),
        )
        db.conn.execute(
            "INSERT INTO persona_posts (id, persona_id, content, media, tags, language,"
            " visibility, like_count, comment_count, created_at)"
            " VALUES ('pp_tax', ?, 'x', '[]', ?, 'en', 'public', 0, 0, '2026-01-01T00:00:00+00:00')",
            (pid, json.dumps(["摄影", "camera"], ensure_ascii=False)),
        )

    first = taxonomy.migrate_tags(db)
    assert first["profiles"] == 1
    # 种子人物/动态里也有自由文本标签，会被一并归一（数量不固定）
    assert first["personas"] >= 1
    assert first["persona_posts"] >= 1

    profile = db.get_profile(user_id="u_tax")
    interests = profile["fact_sheet"]["interests"]
    assert interests[:2] == ["coffee", "photography"]
    assert interests[2].startswith("custom:")

    persona_interests = json.loads(
        db.conn.execute("SELECT interests FROM personas WHERE id=?", (pid,)).fetchone()["interests"]
    )
    assert persona_interests == ["coffee", "cycling", "music"]

    post_tags = json.loads(
        db.conn.execute("SELECT tags FROM persona_posts WHERE id='pp_tax'").fetchone()["tags"]
    )
    assert post_tags == ["photography"]

    # 幂等：再跑一次不产生写入
    second = taxonomy.migrate_tags(db)
    assert second == {"profiles": 0, "personas": 0, "persona_posts": 0, "user_posts": 0}
