"""V3 Persona 持续情绪状态（``app.v3.emotion``）测试。

覆盖点
------
- ``default_state`` 合法、随 ai_config 能量变化；
- ``normalize_state`` 修脏输入（None / 非 JSON / 越界 / 乱写 mood）；
- ``decay`` 向基线回落、边界钳制、0 小时不变；
- ``apply_event`` 单事件变化 ≤0.15、钳制 0..1、planned/cancelled 不改变情绪；
- ``apply_chat`` 幅度更小、方向合理（bad_news 增压、good_news 恢复）；
- ``emotion_prompt_block`` **不含任何数字**、包含情绪词与上下文；
- 状态能写回 ``personas.emotion_state`` 再读出来（与 Lead 的 persona_context 对齐）。

说明：情绪模块是纯函数，只有最后一个用例需要数据库；其余不依赖 SQLite。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.v2.db import DB  # noqa: E402
from app.v3 import emotion as em  # noqa: E402
from app.v3 import life_events as le  # noqa: E402
from app.v3.schema import ensure_schema  # noqa: E402

PERSONA = {
    "id": "p_adrian",
    "nickname": "Adrian",
    "occupation": "Product designer",
    "ai_config": {"temperament": "witty", "energy": 0.55},
}

BASE = {
    "mood": "content",
    "energy": 0.55,
    "stress": 0.35,
    "social_battery": 0.55,
    "emotional_context": "",
    "updated_at": "2026-11-05T09:00:00+00:00",
}


def _numeric(state: dict) -> dict[str, float]:
    return {key: float(state[key]) for key in ("energy", "stress", "social_battery")}


def _max_change(before: dict, after: dict) -> float:
    a, b = _numeric(before), _numeric(after)
    return max(abs(a[key] - b[key]) for key in a)


# --------------------------------------------------------------------------- #
# default_state / normalize_state
# --------------------------------------------------------------------------- #
def test_default_state_is_valid_and_keyed():
    state = em.default_state(PERSONA)
    assert set(state) == {"mood", "energy", "stress", "social_battery", "emotional_context", "updated_at"}
    assert state["mood"] in em.MOODS
    for key in em.BASELINE:
        assert 0.0 <= state[key] <= 1.0
    assert state["updated_at"].endswith("+00:00")


def test_default_state_follows_ai_config_energy():
    low = em.default_state({"id": "a", "ai_config": {"energy": 0.2, "temperament": "calm"}})
    high = em.default_state({"id": "b", "ai_config": {"energy": 0.9, "temperament": "energetic"}})
    assert high["energy"] > low["energy"]
    assert high["social_battery"] >= low["social_battery"]
    # 没有 ai_config 也不能崩
    bare = em.default_state({"id": "c"})
    assert bare["mood"] in em.MOODS


def test_normalize_state_repairs_dirty_input():
    for dirty in (None, {}, "not json at all", 42, [], {"energy": "x"}):
        state = em.normalize_state(dirty)
        assert state["mood"] in em.MOODS
        for key in em.BASELINE:
            assert 0.0 <= state[key] <= 1.0

    clamped = em.normalize_state({"energy": 5, "stress": -3, "social_battery": 0.5, "mood": "banana"})
    assert clamped["energy"] == 1.0
    assert clamped["stress"] == 0.0
    assert clamped["mood"] in em.MOODS

    from_json = em.normalize_state(json.dumps({"mood": "tired", "energy": 0.2}))
    assert from_json["mood"] == "tired"
    assert from_json["energy"] == 0.2

    # 缺 updated_at 不伪造新鲜度（否则会挡住 decay）
    assert em.normalize_state({"mood": "calm"})["updated_at"] == ""


# --------------------------------------------------------------------------- #
# decay
# --------------------------------------------------------------------------- #
def test_decay_pulls_back_toward_baseline():
    drained = {"mood": "drained", "energy": 0.10, "stress": 0.90, "social_battery": 0.05,
               "emotional_context": "a bit rattled after an argument",
               "updated_at": "2026-11-05T00:00:00+00:00"}
    decayed = em.decay(drained, hours=24)
    assert decayed["energy"] > drained["energy"]
    assert decayed["stress"] < drained["stress"]
    assert decayed["social_battery"] > drained["social_battery"]
    assert 0.0 <= decayed["energy"] <= 1.0
    assert decayed["emotional_context"] == ""  # 6 小时以上，余韵淡掉

    # 0 小时：状态不变（仍要合法）
    same = em.decay(drained, hours=0)
    assert _numeric(same) == pytest.approx(_numeric(drained))
    assert same["emotional_context"] == drained["emotional_context"]


def test_decay_stays_in_bounds_for_extreme_inputs():
    for state in (
        {"energy": 0.0, "stress": 1.0, "social_battery": 0.0},
        {"energy": 1.0, "stress": 0.0, "social_battery": 1.0},
        {"energy": -5, "stress": 99, "social_battery": "x"},
    ):
        out = em.decay(state, hours=100)
        for key in em.BASELINE:
            assert 0.0 <= out[key] <= 1.0
        assert out["mood"] in em.MOODS


# --------------------------------------------------------------------------- #
# apply_event
# --------------------------------------------------------------------------- #
def test_apply_event_single_event_change_is_at_most_0_15():
    for event_type in le.EVENT_TYPES:
        state = em.apply_event(BASE, {"event_type": event_type, "event_status": "completed",
                                      "title": f"{event_type} thing"})
        assert _max_change(BASE, state) <= em.EVENT_DELTA_CAP + 1e-6, event_type
        assert state["mood"] in em.MOODS
        assert state["updated_at"]


def test_apply_event_is_clamped_at_bounds():
    top = {"mood": "cheerful", "energy": 0.99, "stress": 0.0, "social_battery": 0.98}
    bottom = {"mood": "drained", "energy": 0.01, "stress": 0.99, "social_battery": 0.02}
    for state in (top, bottom):
        for event_type in ("achievement", "conflict", "unexpected_event", "social", "travel"):
            out = em.apply_event(state, {"event_type": event_type, "event_status": "completed",
                                         "title": "x"})
            for key in em.BASELINE:
                assert 0.0 <= out[key] <= 1.0


def test_apply_event_ignores_planned_and_cancelled():
    planned = em.apply_event(BASE, {"event_type": "conflict", "event_status": "planned",
                                    "title": "argument later"})
    cancelled = em.apply_event(BASE, {"event_type": "conflict", "event_status": "cancelled",
                                      "title": "cancelled argument"})
    # 计划/取消的事不影响情绪（"计划不等于事实"）
    assert _numeric(planned) == pytest.approx(_numeric(em.normalize_state(BASE)))
    assert _numeric(cancelled) == pytest.approx(_numeric(em.normalize_state(BASE)))
    assert planned["emotional_context"] == ""


def test_apply_event_ongoing_is_weaker_than_completed():
    completed = em.apply_event(BASE, {"event_type": "achievement", "event_status": "completed",
                                      "title": "won"})
    ongoing = em.apply_event(BASE, {"event_type": "achievement", "event_status": "ongoing",
                                    "title": "winning"})
    assert _max_change(BASE, ongoing) < _max_change(BASE, completed)


# --------------------------------------------------------------------------- #
# apply_chat
# --------------------------------------------------------------------------- #
def test_apply_chat_changes_are_small_and_directional():
    bad = em.apply_chat(BASE, intent="bad_news", behavior="comfort")
    assert bad["stress"] > BASE["stress"]
    assert _max_change(BASE, bad) <= em.CHAT_DELTA_CAP + 1e-6
    assert bad["emotional_context"]  # 值得记一笔的会话会留下余韵

    good = em.apply_chat(BASE, intent="good_news", behavior="celebrate")
    assert good["energy"] > BASE["energy"]
    assert good["stress"] < BASE["stress"]

    # 认不出来的枚举不改变数值（枚举漂移不能崩链路）
    unknown = em.apply_chat(BASE, intent="???", behavior="???")
    assert _numeric(unknown) == pytest.approx(_numeric(em.normalize_state(BASE)))


# --------------------------------------------------------------------------- #
# prompt block
# --------------------------------------------------------------------------- #
def test_emotion_prompt_block_has_no_digits():
    states = [
        em.default_state(PERSONA),
        em.normalize_state(BASE),
        em.normalize_state({"mood": "drained", "energy": 0.0, "stress": 1.0,
                            "social_battery": 0.0,
                            "emotional_context": "a bit heavy after hearing something hard"}),
        em.normalize_state({"mood": "cheerful", "energy": 1.0, "stress": 0.0,
                            "social_battery": 1.0}),
        {"garbage": True},
    ]
    for state in states:
        for language in ("en", "zh", "pt"):
            block = em.emotion_prompt_block(state, language=language)
            assert block.strip()
            assert not re.search(r"\d", block), block  # 绝不把 0.42 这类数字塞进 prompt


def test_emotion_prompt_block_contains_mood_and_context():
    state = em.normalize_state({
        "mood": "tired", "energy": 0.2, "stress": 0.5, "social_battery": 0.3,
        "emotional_context": "a bit drained after a long stretch of work",
    })
    block = em.emotion_prompt_block(state)
    assert "tired" in block
    assert "a bit drained after a long stretch of work" in block
    assert "How you feel right now" in block


# --------------------------------------------------------------------------- #
# 落库往返（与 persona_context 的读法一致）
# --------------------------------------------------------------------------- #
def test_state_roundtrip_through_persona_row(tmp_path):
    database = DB(str(tmp_path / "emotion.db"))
    database.init()
    ensure_schema(database, seed=False)
    try:
        with database._lock, database.conn:  # noqa: SLF001
            database.conn.execute(
                "INSERT INTO personas (id, nickname, status, lifecycle, created_at)"
                " VALUES ('p_adrian','Adrian','active','permanent','')"
            )
        state = em.apply_event(em.default_state(PERSONA),
                               {"event_type": "achievement", "event_status": "completed",
                                "title": "shipped the redesign"})
        with database._lock, database.conn:  # noqa: SLF001
            database.conn.execute(
                "UPDATE personas SET emotion_state=? WHERE id='p_adrian'",
                (json.dumps(state, ensure_ascii=False),),
            )
        with database._lock:  # noqa: SLF001
            row = database.conn.execute(
                "SELECT emotion_state FROM personas WHERE id='p_adrian'"
            ).fetchone()
        restored = em.normalize_state(row["emotion_state"])
        assert restored["mood"] == state["mood"]
        assert restored["emotional_context"] == state["emotional_context"]
        assert _numeric(restored) == pytest.approx(_numeric(state))
        # 读出来后按 updated_at 衰减也不炸
        decayed = em.decay(restored, hours=3.5)
        assert decayed["mood"] in em.MOODS
    finally:
        database.close()
