"""AI 事件与动态（阶段 6）：词库校验 / 热生效 / 模拟今天。

覆盖：
- events：价值档 0..3、来源三选一、分享权重是倍率 0..2、至少各有一个规则与叙事来源；
- rhythm：参数范围（配额上限 30、tick 下限 60 等）；
- 热生效：改价值档/分享权重/停用类型 → `event_posts` 与 `life_events` 立刻变化；
  改节奏 → 每天规则事件数、叙事上限、动态配额立刻变化；
- 回滚：恢复上一版后运行时回到原值；
- 模拟：空库/有库都能返回结构化估算，且不写库。
"""

from __future__ import annotations

import json

import pytest

from app.v2.db import DB
from app.v3 import event_posts, lexicon, life_events
from app.v3.schema import ensure_schema


@pytest.fixture()
def lib_dir(tmp_path, monkeypatch):
    target = tmp_path / "lexicons"
    target.mkdir()
    monkeypatch.setattr(lexicon, "lexicon_dir", lambda: str(target))
    lexicon.refresh()
    yield target
    monkeypatch.undo()
    lexicon.refresh()


def _events_payload() -> dict:
    return {
        "version": 1,
        "types": {
            "work": {"zh": "工作", "value_tier": 1, "source": "rule", "share_weight": 0.62,
                     "show_title": True, "tags": ["work"], "enabled": True},
            "meal": {"zh": "吃饭", "value_tier": 0, "source": "rule", "share_weight": 0.35,
                     "show_title": True, "tags": ["meal"], "enabled": True},
            "achievement": {"zh": "成就", "value_tier": 3, "source": "narrative",
                            "share_weight": 1.3, "show_title": True, "tags": ["achievement"],
                            "enabled": True},
            "conflict": {"zh": "冲突", "value_tier": 3, "source": "narrative",
                         "share_weight": 1.1, "show_title": False, "tags": ["conflict"],
                         "enabled": True},
        },
    }


def _rhythm_payload(**overrides) -> dict:
    params = {
        "rule_events_per_day": 4,
        "narrative_events_per_day": 2,
        "event_posts_per_day": 8,
        "tension_penalty_step": 0.18,
        "tension_penalty_floor": 0.15,
        "comment_backfill_hours": 6.0,
        "life_tick_s": 300,
        "posts_tick_s": 240,
        "posts_max_events": 4,
        "posts_since_hours": 6,
    }
    params.update(overrides)
    return {"version": 1, "params": params}


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def test_validate_events_accepts_valid(lib_dir):
    assert lexicon.validate("events", _events_payload()) == []


def test_validate_events_rejects_bad_weight_source_and_empty(lib_dir):
    payload = _events_payload()
    payload["types"]["work"]["share_weight"] = 2.5  # 倍率上限 2
    payload["types"]["meal"]["source"] = "sometimes"
    issues = lexicon.validate("events", payload)
    assert any("share_weight" in item for item in issues)
    assert any("source" in item for item in issues)

    only_rule = _events_payload()
    only_rule["types"]["achievement"]["enabled"] = False
    only_rule["types"]["conflict"]["enabled"] = False
    issues = lexicon.validate("events", only_rule)
    assert any("叙事来源" in item for item in issues)


def test_validate_rhythm_bounds(lib_dir):
    assert lexicon.validate("rhythm", _rhythm_payload()) == []
    over = lexicon.validate("rhythm", _rhythm_payload(event_posts_per_day=50))
    assert any("event_posts_per_day" in item for item in over)
    tick = lexicon.validate("rhythm", _rhythm_payload(life_tick_s=10))
    assert any("life_tick_s" in item for item in tick)
    missing = _rhythm_payload()
    del missing["params"]["posts_tick_s"]
    assert any("缺少参数" in item for item in lexicon.validate("rhythm", missing))


# --------------------------------------------------------------------------- #
# 热生效
# --------------------------------------------------------------------------- #
def test_events_hot_apply_value_tier_weight_and_disable(lib_dir):
    payload = _events_payload()
    payload["types"]["meal"]["value_tier"] = 3
    payload["types"]["meal"]["share_weight"] = 1.5
    lexicon.save("events", payload)
    assert event_posts.VALUE_TIERS["meal"] == 3
    assert event_posts.EVENT_TYPE_SHARE_WEIGHT["meal"] == pytest.approx(1.5)
    assert "WHEN 'meal' THEN 3" in event_posts._VALUE_TIER_SQL  # noqa: SLF001 - 断言生成结果
    assert "meal" in life_events.EVENT_TYPES

    # 停用 → 从运行时类型集合里消失（历史事件不受影响）
    payload["types"]["meal"]["enabled"] = False
    lexicon.save("events", payload)
    assert "meal" not in event_posts.VALUE_TIERS
    assert "meal" not in life_events.EVENT_TYPES


def test_rhythm_hot_apply_quota_and_events(lib_dir):
    lexicon.save(
        "rhythm",
        _rhythm_payload(
            rule_events_per_day=6,
            narrative_events_per_day=0,
            event_posts_per_day=3,
            tension_penalty_step=0.3,
        ),
    )
    assert life_events.RULE_EVENTS_PER_DAY_DEFAULT == 6
    assert life_events.NARRATIVE_EVENTS_PER_DAY_DEFAULT == 0
    assert event_posts.DEFAULT_EVENT_POSTS_PER_DAY == 3
    assert event_posts.TENSION_PENALTY_STEP == pytest.approx(0.3)


def test_events_reset_restores_previous(lib_dir):
    lexicon.save("events", _events_payload())
    assert event_posts.VALUE_TIERS["meal"] == 0
    changed = json.loads(json.dumps(_events_payload()))
    changed["types"]["meal"]["value_tier"] = 3
    lexicon.save("events", changed)
    assert event_posts.VALUE_TIERS["meal"] == 3
    lexicon.reset_to_default("events")
    assert event_posts.VALUE_TIERS["meal"] == 0


def test_removing_event_and_rhythm_overrides_restores_code_defaults(lib_dir):
    tiers = event_posts.VALUE_TIERS
    payload = _events_payload()
    payload["types"]["meal"]["value_tier"] = 3
    lexicon.save("events", payload)
    lexicon.save("rhythm", _rhythm_payload(event_posts_per_day=3, rule_events_per_day=6))
    lexicon.reset_to_default("events")
    lexicon.reset_to_default("rhythm")
    assert event_posts.VALUE_TIERS is tiers
    assert event_posts.VALUE_TIERS["meal"] == 0
    assert "relationship" in life_events.EVENT_TYPES
    assert event_posts.DEFAULT_EVENT_POSTS_PER_DAY == 8
    assert life_events.RULE_EVENTS_PER_DAY_DEFAULT == 4


# --------------------------------------------------------------------------- #
# 模拟今天
# --------------------------------------------------------------------------- #
def test_simulate_today_with_and_without_db(lib_dir, tmp_path):
    lexicon.save("events", _events_payload())
    lexicon.save("rhythm", _rhythm_payload(event_posts_per_day=5))

    empty = lexicon.simulate_today(None)
    assert "assumptions" in empty and isinstance(empty["assumptions"], list)
    assert empty["params"]["event_posts_per_day"] == 5

    db = DB(str(tmp_path / "sim.db"))
    db.init()
    ensure_schema(db, seed=True)
    try:
        out = lexicon.simulate_today(db)
        assert out["world"]["personas"] >= 1
        assert out["types"]["enabled"] == 4
        assert out["today"]["events"] == {}  # 新库今天还没事件
        assert out["pending"]["quota"] == 5
        assert out["next_day"]["posts_quota"] == 5
        assert isinstance(out["next_day"]["rule_events_est"], int)
        # 不写库：事件表仍为空
        assert db.conn.execute("SELECT COUNT(*) AS n FROM persona_events").fetchone()["n"] == 0
    finally:
        db.close()
