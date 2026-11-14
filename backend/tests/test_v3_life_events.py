"""V3 人物生活事件（``app.v3.life_events``）测试。

覆盖点
------
- 作息差异化（设计师 / 学生 / 自由职业 / 咖啡店主至少 2 个时段不同）与确定性；
- ``ensure_schedule`` 幂等落库；
- 规则事件：只产出 work/study/meal/daily_routine、planned、dedupe_key 规范；
- ``add_event`` 幂等（同 dedupe_key 第二次 created=False）；
- ``refresh_statuses``：planned→ongoing→completed、超期 ongoing 收敛、重复调用 0；
- ``relevant_events`` 只给 completed、cancelled 永不出现；``upcoming_events`` 只给 planned；
- ``event_fact_block`` 的 PLAN / DONE / cancelled 语义；
- ``detect_conflicts`` 的时间重叠与参与者判定；
- ``ensure_relationships`` 幂等且人人都有熟人；
- ``generate_day_events`` 幂等、LLM 路径 / 失败回落 / 环境变量开关；
- ``life_loop`` 启动即生成（不需要真实等待一个 tick）；
- 老库没有新表时全部函数静默降级。

说明
----
- 每个测试用 ``tmp_path`` 下独立 SQLite + ``ensure_schema(seed=False)``，不碰 ``data/``；
- LLM 全部是 Mock（返回固定 JSON 或抛异常），**不调真实模型**；
- 时间用固定 ``NOW`` 传参，不 sleep、不依赖运行时的真实时钟。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.v2.db import DB  # noqa: E402
from app.v3 import life_events as le  # noqa: E402
from app.v3.schema import ensure_schema  # noqa: E402

#: 固定"现在"：2026-11-05 是周四，测试不依赖真实时间
NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)
DAY = "2026-11-05"
WEEKEND_DAY = "2026-11-07"  # 周六


# --------------------------------------------------------------------------- #
# 夹具与工具
# --------------------------------------------------------------------------- #
PERSONAS: list[dict] = [
    {
        "id": "p_designer", "nickname": "Mara", "occupation": "Product designer",
        "language": "en", "country": "United Kingdom", "city": "Manchester",
        "timezone": "Europe/London", "interests": ["design", "coffee"], "age": 28,
        "ai_config": {"temperament": "witty", "energy": 0.55},
    },
    {
        "id": "p_student", "nickname": "Kenji", "occupation": "Computer science student",
        "language": "ja", "country": "Japan", "city": "Kyoto", "timezone": "Asia/Tokyo",
        "interests": ["coding", "books"], "age": 20,
    },
    {
        "id": "p_freelance", "nickname": "Luca", "occupation": "Freelance illustrator",
        "language": "it", "country": "Italy", "city": "Bologna", "timezone": "Europe/Rome",
        "interests": ["art", "music"], "age": 31,
    },
    {
        "id": "p_cafe", "nickname": "Sofia", "occupation": "Cafe owner",
        "language": "pt", "country": "Portugal", "city": "Lisbon",
        "timezone": "Europe/Lisbon", "interests": ["coffee", "food"], "age": 33,
    },
]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _future_day(days: int = 1) -> str:
    """相对**真实时钟**的未来日期（``upcoming_events`` 用的是真实 now）。"""
    return (datetime.now(timezone.utc) + timedelta(days=days)).date().isoformat()


@pytest.fixture()
def db(tmp_path):
    database = DB(str(tmp_path / "life.db"))
    database.init()
    ensure_schema(database, seed=False)
    yield database
    database.close()


def _insert_persona(database: DB, spec: dict, *, lifecycle: str = "permanent",
                    status: str = "active") -> None:
    with database._lock, database.conn:  # noqa: SLF001
        database.conn.execute(
            "INSERT INTO personas (id, nickname, bio, personality, interests, language,"
            " country, city, timezone, occupation, age, gender, ai_config, status,"
            " created_at, lifecycle)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                spec["id"], spec["nickname"], spec.get("bio", ""), spec.get("personality", ""),
                json.dumps(spec.get("interests") or [], ensure_ascii=False),
                spec.get("language", "en"), spec.get("country", ""), spec.get("city", ""),
                spec.get("timezone", "UTC"), spec.get("occupation", ""), spec.get("age", 27),
                spec.get("gender", "female"),
                json.dumps(spec.get("ai_config") or {}, ensure_ascii=False),
                status, _iso(NOW), lifecycle,
            ),
        )


@pytest.fixture()
def world(db):
    for spec in PERSONAS:
        _insert_persona(db, spec)
    return db


def _persona(database: DB, persona_id: str) -> dict:
    with database._lock:  # noqa: SLF001
        row = database.conn.execute("SELECT * FROM personas WHERE id=?", (persona_id,)).fetchone()
    assert row is not None
    return dict(row)


def _event(**overrides) -> dict:
    """构造一个合法事件 dict（默认 completed，方便直接测"事实"）。"""
    base = {
        "persona_id": "p_designer",
        "event_type": "daily_routine",
        "title": "Something happened",
        "description": "",
        "start_time": "2026-11-05T09:00:00+00:00",
        "end_time": "2026-11-05T10:00:00+00:00",
        "participants": [],
        "location": "",
        "related_personas": [],
        "event_status": "completed",
        "source_type": "rule",
        "source_reference": "test",
        "visibility": "private",
        "metadata": {"timezone": "UTC"},
        "dedupe_key": "test:unique:key",
        "day": DAY,
    }
    base.update(overrides)
    return base


def _count(database: DB, sql: str, params: tuple = ()) -> int:
    with database._lock:  # noqa: SLF001
        return int(database.conn.execute(sql, params).fetchone()["n"])


def _events(database: DB) -> list[dict]:
    with database._lock:  # noqa: SLF001
        rows = database.conn.execute("SELECT * FROM persona_events ORDER BY start_time").fetchall()
    return [le._row_to_event(row) for row in rows]  # noqa: SLF001 - 测试里复用统一解码


#: 情绪接线的基线状态（用固定 updated_at，避免真实时钟让断言漂移）
EMOTION_BASE: dict = {
    "mood": "content",
    "energy": 0.50,
    "stress": 0.35,
    "social_battery": 0.50,
    "emotional_context": "",
    "updated_at": "2026-11-05T00:00:00+00:00",
}


def _set_emotion(database: DB, persona_id: str, state: dict) -> None:
    with database._lock, database.conn:  # noqa: SLF001
        database.conn.execute(
            "UPDATE personas SET emotion_state=? WHERE id=?",
            (json.dumps(state, ensure_ascii=False), persona_id),
        )


def _get_emotion(database: DB, persona_id: str) -> dict:
    with database._lock:  # noqa: SLF001
        row = database.conn.execute(
            "SELECT emotion_state FROM personas WHERE id=?", (persona_id,)
        ).fetchone()
    assert row is not None
    return json.loads(row["emotion_state"] or "{}")


def _insert_relationship(
    database: DB,
    persona_a: str,
    persona_b: str,
    *,
    relation_type: str = "friend",
    closeness: int = 2,
    shared_events: int = 0,
    last_shared_at: str = "",
    tension: int = 0,
    last_conflict_at: str = "",
    reverse: bool = False,
) -> str:
    """手工插入一条关系（测试需要精确控制 closeness / shared_events / tension）。"""
    pair = [persona_a, persona_b] if reverse else sorted([persona_a, persona_b])
    rel_id = f"pr_{pair[0]}__{pair[1]}"
    with database._lock, database.conn:  # noqa: SLF001
        database.conn.execute(
            "INSERT INTO persona_relationships (id, persona_a, persona_b, relation_type,"
            " closeness, shared_events, last_shared_at, tension, last_conflict_at,"
            " created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rel_id, pair[0], pair[1], relation_type, closeness, shared_events,
             last_shared_at, tension, last_conflict_at, _iso(NOW), _iso(NOW)),
        )
    return rel_id


def _read_relationship(database: DB, persona_a: str, persona_b: str) -> dict | None:
    pair = sorted([persona_a, persona_b])
    with database._lock:  # noqa: SLF001
        row = database.conn.execute(
            "SELECT * FROM persona_relationships WHERE persona_a=? AND persona_b=?",
            (pair[0], pair[1]),
        ).fetchone()
    return dict(row) if row is not None else None


# --------------------------------------------------------------------------- #
# Mock LLM
# --------------------------------------------------------------------------- #
class _MockLLM:
    """确定性 Mock：返回固定 JSON；``raises=True`` 时模拟模型故障。"""

    uses_mock = False

    def __init__(self, payload: dict | None = None, *, raises: bool = False) -> None:
        self.payload = payload or {"events": []}
        self.raises = raises
        self.calls = 0

    async def complete_json(self, messages, *, schema_hint="", temperature=None, max_tokens=None):
        self.calls += 1
        if self.raises:
            raise RuntimeError("llm down")
        return json.dumps(self.payload, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# build_schedule / ensure_schedule
# --------------------------------------------------------------------------- #
def test_build_schedule_is_visibly_different_by_occupation():
    schedules = {spec["id"]: le.build_schedule(spec) for spec in PERSONAS}
    archetypes = {pid: schedule["archetype"] for pid, schedule in schedules.items()}
    assert archetypes == {
        "p_designer": "creative",
        "p_student": "student",
        "p_freelance": "freelance",
        "p_cafe": "service",
    }

    def signature(schedule: dict) -> set[tuple[str, str]]:
        return {(block["start"], block["end"]) for block in schedule["weekday_blocks"]}

    ids = list(schedules)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = signature(schedules[ids[i]]), signature(schedules[ids[j]])
            # 至少 2 个时段不同（"明显不同"，不是抖动级别）
            assert len(a ^ b) >= 2, (ids[i], ids[j], a ^ b)

    # 具体差异：学生有课、设计师有客户电话、自由职业夜里还在工作、咖啡店主早班 + 周末上班
    assert any("lectures" in block["label"] for block in schedules["p_student"]["weekday_blocks"])
    assert any("Client call" in block["label"] for block in schedules["p_designer"]["weekday_blocks"])
    assert any(
        block["start"] >= "21:00" for block in schedules["p_freelance"]["weekday_blocks"]
    )
    assert any(block["start"] <= "06:15" for block in schedules["p_cafe"]["weekday_blocks"])
    assert any(block["kind"] == "work" for block in schedules["p_cafe"]["weekend_blocks"])


def test_build_schedule_shape_and_determinism():
    schedule = le.build_schedule(PERSONAS[0])
    assert {"weekday_blocks", "weekend_blocks", "work_hours", "timezone"} <= set(schedule)
    for key in ("weekday_blocks", "weekend_blocks"):
        blocks = schedule[key]
        assert blocks
        previous_end = ""
        for block in blocks:
            assert set(block) == {"start", "end", "kind", "label"}
            assert re.match(r"^\d{2}:\d{2}$", block["start"]) and re.match(r"^\d{2}:\d{2}$", block["end"])
            assert block["start"] < block["end"]
            assert block["start"] >= previous_end  # 有序且不重叠
            previous_end = block["end"]
    assert all(1 <= day <= 7 for day in schedule["work_hours"]["days"])
    assert le.build_schedule(PERSONAS[0]) == schedule  # 确定性


def test_ensure_schedule_is_idempotent(world):
    first = le.ensure_schedule(world, "p_designer")
    assert first["weekday_blocks"]
    assert _count(world, "SELECT COUNT(*) AS n FROM persona_schedules") == 1
    second = le.ensure_schedule(world, "p_designer")
    assert second["weekday_blocks"] == first["weekday_blocks"]
    assert second["work_hours"] == first["work_hours"]
    assert _count(world, "SELECT COUNT(*) AS n FROM persona_schedules") == 1


# --------------------------------------------------------------------------- #
# 规则事件
# --------------------------------------------------------------------------- #
def test_rule_events_for_day_are_planned_and_normalized(world):
    persona = _persona(world, "p_designer")
    events = le.rule_events_for_day(persona, DAY)
    # 完整作息有 6-8 个块，但落库只取最重要的 4 条（控制事件表增长）
    assert len(events) == le.RULE_EVENTS_PER_DAY_DEFAULT
    for event in events:
        assert event["persona_id"] == "p_designer"
        assert event["event_status"] == "planned"
        assert event["source_type"] == "rule"
        assert event["day"] == DAY
        assert event["event_type"] in le.RULE_EVENT_TYPES
        assert event["participants"] == []
        assert event["start_time"] < event["end_time"]
        assert re.match(
            r"^p_designer:(work|study|meal|daily_routine):2026-11-05T\d{2}:[a-z0-9-]+$",
            event["dedupe_key"],
        ), event["dedupe_key"]
    # 优先级：工作/学习一定保留，且事件按时间有序
    assert any(event["event_type"] == "work" for event in events)
    assert [event["start_time"] for event in events] == sorted(
        event["start_time"] for event in events
    )
    # 完整作息没有丢：persona_schedules 里仍是全部块，供"此刻在做什么"推断
    assert len(le.build_schedule(persona)["weekday_blocks"]) > len(events)

    # 同一 (persona, day) 永远同一批事件（纯函数，无随机）
    assert le.rule_events_for_day(persona, DAY) == events

    # 周末用 weekend_blocks：咖啡店周六照样上班，办公室原型没有"通勤"
    cafe = le.rule_events_for_day(_persona(world, "p_cafe"), WEEKEND_DAY)
    assert any(event["event_type"] == "work" for event in cafe)


def test_rule_events_cap_is_configurable(world, monkeypatch):
    persona = _persona(world, "p_designer")
    monkeypatch.setenv(le.RULE_EVENTS_ENV, "3")
    events = le.rule_events_for_day(persona, DAY)
    assert len(events) == 3
    # 上限之下仍然优先保留工作块
    assert any(event["event_type"] == "work" for event in events)

    # 调大上限 → 与完整作息对齐（需要完整还原时用这个）
    monkeypatch.setenv(le.RULE_EVENTS_ENV, "99")
    full = le.rule_events_for_day(persona, DAY)
    assert len(full) == len(le.build_schedule(persona)["weekday_blocks"])
    assert len(full) > len(events)


def test_add_event_is_idempotent(world):
    event = le.rule_events_for_day(_persona(world, "p_designer"), DAY)[0]
    event_id, created = le.add_event(world, event)
    assert created is True and event_id
    again_id, again_created = le.add_event(world, event)
    assert again_created is False
    assert again_id == event_id
    assert _count(world, "SELECT COUNT(*) AS n FROM persona_events") == 1

    stored = le.events_for_persona(world, "p_designer", since_days=0)
    assert len(stored) == 1
    assert stored[0]["title"] == event["title"]
    assert stored[0]["id"] == event_id


# --------------------------------------------------------------------------- #
# refresh_statuses
# --------------------------------------------------------------------------- #
def test_refresh_statuses_progresses_planned_ongoing_completed(world):
    ongoing_id, _ = le.add_event(world, _event(
        event_status="planned",
        title="Client call",
        start_time=_iso(NOW - timedelta(hours=1)),
        end_time=_iso(NOW + timedelta(hours=1)),
        dedupe_key="k:ongoing",
    ))
    past_id, _ = le.add_event(world, _event(
        event_status="planned",
        title="Old lunch",
        start_time=_iso(NOW - timedelta(hours=3)),
        end_time=_iso(NOW - timedelta(hours=2)),
        dedupe_key="k:past",
    ))

    assert le.refresh_statuses(world, now=NOW) == 2
    by_id = {event["id"]: event for event in _events(world)}
    assert by_id[ongoing_id]["event_status"] == "ongoing"
    assert by_id[past_id]["event_status"] == "completed"
    assert by_id[past_id]["completed_at"]

    # 再推进 2 小时：ongoing → completed
    assert le.refresh_statuses(world, now=NOW + timedelta(hours=2)) == 1
    assert {event["id"]: event for event in _events(world)}[ongoing_id]["event_status"] == "completed"

    # 幂等：重复调用返回 0
    assert le.refresh_statuses(world, now=NOW + timedelta(hours=2)) == 0


def test_refresh_statuses_closes_stale_ongoing_without_end(world):
    stale_id, _ = le.add_event(world, _event(
        event_status="ongoing",
        title="Something long",
        start_time=_iso(NOW - timedelta(hours=8)),
        end_time="",
        dedupe_key="k:stale",
    ))
    fresh_id, _ = le.add_event(world, _event(
        event_status="ongoing",
        title="Still going",
        start_time=_iso(NOW - timedelta(hours=1)),
        end_time="",
        dedupe_key="k:fresh",
    ))
    le.refresh_statuses(world, now=NOW)
    by_id = {event["id"]: event for event in _events(world)}
    assert by_id[stale_id]["event_status"] == "completed"
    assert by_id[fresh_id]["event_status"] == "ongoing"


# --------------------------------------------------------------------------- #
# 完成事件 → 情绪变化（幂等，只在真正迁移的那一次）
# --------------------------------------------------------------------------- #
def test_refresh_statuses_applies_emotion_once_for_completed_event(world):
    _set_emotion(world, "p_designer", EMOTION_BASE)
    _set_emotion(world, "p_student", EMOTION_BASE)
    event_id, _ = le.add_event(world, _event(
        event_type="achievement",
        title="Finished the mural commission",
        event_status="planned",
        participants=["p_student"],
        start_time=_iso(NOW - timedelta(hours=3)),
        end_time=_iso(NOW - timedelta(hours=2)),
        dedupe_key="k:emotion:achievement",
    ))
    before_designer = _get_emotion(world, "p_designer")
    before_student = _get_emotion(world, "p_student")

    changed = le.refresh_statuses(world, now=NOW)
    assert changed >= 1
    by_id = {event["id"]: event for event in _events(world)}
    assert by_id[event_id]["event_status"] == "completed"

    after_designer = _get_emotion(world, "p_designer")
    after_student = _get_emotion(world, "p_student")
    # 主体与参与者都被事件影响，且幅度符合契约（单事件 ≤0.15）
    assert after_designer != before_designer
    assert after_student != before_student
    assert after_designer["energy"] > before_designer["energy"]  # achievement 提神
    assert after_student["energy"] > before_student["energy"]
    for before, after in ((before_designer, after_designer), (before_student, after_student)):
        for key in ("energy", "stress", "social_battery"):
            assert abs(float(after[key]) - float(before[key])) <= 0.15 + 1e-6
            assert 0.0 <= float(after[key]) <= 1.0

    # 幂等：再刷新一次，状态不再变化、情绪也不再叠加
    snapshot = {pid: _get_emotion(world, pid) for pid in ("p_designer", "p_student")}
    assert le.refresh_statuses(world, now=NOW) == 0
    assert le.refresh_statuses(world, now=NOW + timedelta(hours=1)) == 0
    for pid, state in snapshot.items():
        assert _get_emotion(world, pid) == state


def test_refresh_statuses_does_not_apply_emotion_to_cancelled_or_future(world):
    _set_emotion(world, "p_designer", EMOTION_BASE)
    before = _get_emotion(world, "p_designer")

    # 已取消（时间已过）→ 状态不动、情绪不动
    cancelled_id, _ = le.add_event(world, _event(
        event_type="conflict",
        title="Cancelled argument",
        event_status="cancelled",
        start_time=_iso(NOW - timedelta(hours=2)),
        end_time=_iso(NOW - timedelta(hours=1)),
        dedupe_key="k:emotion:cancelled",
    ))
    # 还没到时间 → 不能算"已发生"
    future_id, _ = le.add_event(world, _event(
        event_type="conflict",
        title="Argument later",
        event_status="planned",
        start_time=_iso(NOW + timedelta(hours=2)),
        end_time=_iso(NOW + timedelta(hours=3)),
        dedupe_key="k:emotion:future",
    ))

    changed = le.refresh_statuses(world, now=NOW)
    # 没有任何事件发生 planned/ongoing → completed 的迁移
    assert changed == 0
    by_id = {event["id"]: event for event in _events(world)}
    assert by_id[cancelled_id]["event_status"] == "cancelled"
    assert by_id[future_id]["event_status"] == "planned"
    assert _get_emotion(world, "p_designer") == before


# --------------------------------------------------------------------------- #
# 事实检索 / 计划
# --------------------------------------------------------------------------- #
def test_relevant_events_only_returns_completed_facts(world):
    le.add_event(world, _event(
        title="Finished the mural project", description="The gallery accepted it.",
        event_status="completed", start_time=_iso(NOW - timedelta(days=2)),
        end_time=_iso(NOW - timedelta(days=2) + timedelta(hours=2)), dedupe_key="k:mural",
    ))
    le.add_event(world, _event(
        title="Plan: mural project with a client", event_status="planned",
        start_time=_iso(NOW + timedelta(days=1)),
        end_time=_iso(NOW + timedelta(days=1) + timedelta(hours=2)), dedupe_key="k:plan",
    ))
    cancelled_id, _ = le.add_event(world, _event(
        title="Mural project pitch cancelled", event_status="cancelled",
        start_time=_iso(NOW - timedelta(days=1)),
        end_time=_iso(NOW - timedelta(days=1) + timedelta(hours=1)), dedupe_key="k:cancel",
    ))

    hits = le.relevant_events(world, "p_designer", "mural project")
    assert hits, "已完成的 mural 事件应该被检索到"
    assert all(event["event_status"] == "completed" for event in hits)
    assert all(event["id"] != cancelled_id for event in hits)
    assert hits[0]["title"] == "Finished the mural project"

    # 查询词完全无关时也不泄漏 planned/cancelled
    fallback = le.relevant_events(world, "p_designer", "quantum physics")
    assert fallback and all(event["event_status"] == "completed" for event in fallback)


def test_upcoming_events_is_plan_not_fact(world):
    # 注意：upcoming_events 用真实时钟取"未来"，所以时间相对 now 构造
    soon = datetime.now(timezone.utc) + timedelta(days=2)
    future_id, _ = le.add_event(world, _event(
        title="Weekend trip to the coast", event_status="planned",
        start_time=_iso(soon),
        end_time=_iso(soon + timedelta(hours=6)), dedupe_key="k:trip",
    ))
    le.add_event(world, _event(
        title="Already started", event_status="ongoing",
        start_time=_iso(NOW - timedelta(hours=1)),
        end_time=_iso(NOW + timedelta(hours=1)), dedupe_key="k:started",
    ))
    le.add_event(world, _event(
        title="Done", event_status="completed", dedupe_key="k:done",
    ))
    upcoming = le.upcoming_events(world, "p_designer")
    assert [event["id"] for event in upcoming] == [future_id]
    assert upcoming[0]["event_status"] == "planned"


def test_event_fact_block_plan_vs_done(world):
    planned = le.rule_events_for_day(_persona(world, "p_designer"), DAY)[0]
    text = le.event_fact_block(planned)
    assert text.startswith("PLAN (not happened yet): ")
    # 即使调用方不想显示状态前缀，PLAN 标记也必须在（安全不变式）
    assert le.event_fact_block(planned, include_status=False).startswith("PLAN (not happened yet): ")

    done = _event(
        title="Finished the mural project",
        event_status="completed",
        location="Studio",
        metadata={"timezone": "UTC", "participant_names": ["Luca"]},
    )
    done_text = le.event_fact_block(done)
    assert done_text.startswith("DONE: ")
    assert "Finished the mural project" in done_text
    assert "Studio" in done_text and "Luca" in done_text
    assert not le.event_fact_block(done, include_status=False).startswith("DONE:")

    assert le.event_fact_block(_event(event_status="cancelled")) == ""
    assert le.event_fact_block({}) == ""


def test_event_fact_block_accepts_raw_db_rows(world):
    """回归：``persona_events.metadata/participants`` 在库里是 JSON **字符串**。

    外部调用方（集成测试 / persona_context）经常把 ``dict(row)`` 直接传进来，
    ``event_fact_block`` 必须同时接受「原始 DB 行」和「解析过的事件 dict」。
    """
    planned_id, _ = le.add_event(world, _event(
        event_status="planned",
        title="Weekend trip to the coast",
        start_time=_iso(NOW + timedelta(days=2)),
        end_time=_iso(NOW + timedelta(days=2) + timedelta(hours=4)),
        participants=["p_student"],
        metadata={"timezone": "Europe/London", "tags": ["travel"]},
        dedupe_key="k:raw:planned",
    ))
    completed_id, _ = le.add_event(world, _event(
        event_status="completed",
        title="Finished the mural commission",
        description="Delivered early.",
        metadata={"timezone": "UTC", "participant_names": ["Luca"]},
        dedupe_key="k:raw:done",
    ))
    with world._lock:  # noqa: SLF001 - 故意用未解析的原始行
        raw_rows = {
            str(row["id"]): dict(row)
            for row in world.conn.execute("SELECT * FROM persona_events").fetchall()
        }
    planned_row = raw_rows[planned_id]
    completed_row = raw_rows[completed_id]
    assert isinstance(planned_row["metadata"], str)  # 确认测试确实覆盖了字符串路径

    planned_text = le.event_fact_block(planned_row)
    assert "not happened yet" in planned_text
    assert "DONE" not in planned_text  # planned 绝不能出现"已完成"措辞
    assert re.search(r"[A-Z][a-z]{2} \d{2}:\d{2}", planned_text)  # 本地时间仍能格式化

    completed_text = le.event_fact_block(completed_row)
    assert completed_text.startswith("DONE: ")
    assert "not happened yet" not in completed_text
    assert le.event_fact_block(completed_row, include_status=False).startswith(
        "Finished the mural commission"
    )

    # 原始行喂给冲突检测 / 相关性打分也不能崩
    assert le.detect_conflicts(
        [planned_row], ["p_student"], planned_row["start_time"], planned_row["end_time"]
    ) == [planned_row]
    assert isinstance(
        le._relevance_score(  # noqa: SLF001 - 直接验证打分容错
            planned_row, {"weekend", "trip"}
        ),
        float,
    )


# --------------------------------------------------------------------------- #
# 冲突检测
# --------------------------------------------------------------------------- #
def test_detect_conflicts():
    existing = [
        _event(persona_id="p_a", title="Work", event_status="ongoing",
               start_time="2026-11-05T09:00:00+00:00", end_time="2026-11-05T12:00:00+00:00"),
        _event(persona_id="p_b", title="Other person", start_time="2026-11-05T09:00:00+00:00",
               end_time="2026-11-05T12:00:00+00:00"),
        _event(persona_id="p_a", title="Cancelled thing", event_status="cancelled",
               start_time="2026-11-05T09:30:00+00:00", end_time="2026-11-05T10:30:00+00:00"),
        _event(persona_id="p_a", title="Adjacent", start_time="2026-11-05T13:00:00+00:00",
               end_time="2026-11-05T14:00:00+00:00"),
        _event(persona_id="p_c", title="Shared event", participants=["p_a"],
               start_time="2026-11-05T15:00:00+00:00", end_time="2026-11-05T16:00:00+00:00"),
    ]

    hits = le.detect_conflicts(existing, ["p_a"], "2026-11-05T10:00:00+00:00", "2026-11-05T11:00:00+00:00")
    titles = {event["title"] for event in hits}
    assert titles == {"Work"}, titles  # 已取消/别人的不算

    # 半开区间：end == 下一段 start 不算冲突
    assert le.detect_conflicts(
        existing, ["p_a"], "2026-11-05T12:00:00+00:00", "2026-11-05T13:00:00+00:00"
    ) == []

    # 参与者里的 p_a 也算 p_a 被占用
    shared = le.detect_conflicts(
        existing, ["p_a"], "2026-11-05T15:30:00+00:00", "2026-11-05T15:45:00+00:00"
    )
    assert [event["title"] for event in shared] == ["Shared event"]

    # 不相关的人 → 无冲突
    assert le.detect_conflicts(
        existing, ["p_zzz"], "2026-11-05T10:00:00+00:00", "2026-11-05T11:00:00+00:00"
    ) == []


# --------------------------------------------------------------------------- #
# current_activity
# --------------------------------------------------------------------------- #
def test_current_activity_prefers_ongoing_event(world):
    le.add_event(world, _event(
        title="Client call", event_status="ongoing",
        start_time=_iso(NOW - timedelta(minutes=10)),
        end_time=_iso(NOW + timedelta(minutes=20)), dedupe_key="k:call",
    ))
    activity = le.current_activity(world, "p_designer", now=NOW)
    assert activity is not None
    assert activity["label"] == "Client call"
    assert activity["start_time"]


def test_current_activity_falls_back_to_schedule_without_writing(world):
    persona = _persona(world, "p_student")
    schedule = le.build_schedule(persona)
    block = schedule["weekday_blocks"][0]
    hour, minute = (int(x) for x in block["start"].split(":"))
    # 用人物本地时区构造"正在这个时间段里"的时刻（DAY 是周四 → 用 weekday_blocks）
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(persona["timezone"])
    moment = datetime(2026, 11, 5, hour, minute, tzinfo=tz) + timedelta(minutes=5)
    activity = le.current_activity(world, "p_student", now=moment)
    assert activity is not None
    assert activity["label"] == block["label"]
    assert activity["metadata"]["inferred"] is True
    # 推断不写库
    assert _count(world, "SELECT COUNT(*) AS n FROM persona_events") == 0


# --------------------------------------------------------------------------- #
# 人物关系种子
# --------------------------------------------------------------------------- #
def test_ensure_relationships_is_idempotent_and_connects_everyone(world):
    created = le.ensure_relationships(world, [dict(spec, id=spec["id"]) for spec in PERSONAS])
    assert created >= 2
    with world._lock:  # noqa: SLF001
        rows = world.conn.execute("SELECT * FROM persona_relationships").fetchall()
    connected: set[str] = set()
    for row in rows:
        connected.update({str(row["persona_a"]), str(row["persona_b"])})
        assert row["relation_type"] in le.RELATION_TYPES
        assert 1 <= int(row["closeness"]) <= 5
    assert connected == {spec["id"] for spec in PERSONAS}  # 人人都有熟人
    assert le.ensure_relationships(world) == 0  # 幂等


def test_ensure_relationships_without_personas_skips_guests(world):
    # 每日游客人物（24h 回收）不应该被写进长期关系里
    _insert_persona(world, {"id": "p_guest", "nickname": "Guest"})
    with world._lock, world.conn:  # noqa: SLF001
        world.conn.execute("UPDATE personas SET lifecycle='guest' WHERE id='p_guest'")

    created = le.ensure_relationships(world)  # main.py 启动时的调用形态：不传 personas
    assert created >= 2
    with world._lock:  # noqa: SLF001
        rows = world.conn.execute("SELECT persona_a, persona_b FROM persona_relationships").fetchall()
    assert rows
    for row in rows:
        assert "p_guest" not in (row["persona_a"], row["persona_b"])

    connected: set[str] = set()
    for row in rows:
        connected.update({str(row["persona_a"]), str(row["persona_b"])})
    assert connected == {spec["id"] for spec in PERSONAS}
    assert le.ensure_relationships(world) == 0  # 再调一次：幂等


# --------------------------------------------------------------------------- #
# 人物关系演进（共同经历 → closeness）
# --------------------------------------------------------------------------- #
def test_relationship_between_is_direction_normalized(world):
    rel_id = _insert_relationship(world, "p_designer", "p_student", closeness=3, shared_events=1)

    forward = le.relationship_between(world, "p_designer", "p_student")
    backward = le.relationship_between(world, "p_student", "p_designer")
    assert forward is not None and backward is not None
    assert forward["id"] == backward["id"] == rel_id  # A→B 与 B→A 是同一条
    assert {"id", "persona_a", "persona_b", "relation_type", "closeness",
            "shared_events", "last_shared_at"} <= set(forward)
    assert forward["closeness"] == 3
    assert forward["shared_events"] == 1

    # 未归一化存储方向（persona_a > persona_b）也能双向查到
    reversed_id = _insert_relationship(
        world, "p_freelance", "p_cafe", closeness=2, reverse=True
    )
    found = le.relationship_between(world, "p_cafe", "p_freelance")
    assert found is not None and found["id"] == reversed_id

    # 不存在的关系统一 None；自己和自己也 None（不造关系）
    assert le.relationship_between(world, "p_designer", "p_cafe") is None
    assert le.relationship_between(world, "p_designer", "p_designer") is None


def test_bump_relationship_thresholds_monotonic_and_capped(world):
    rel_id = _insert_relationship(world, "p_designer", "p_student", closeness=2, shared_events=0)

    sequence: list[tuple[int, int]] = []
    for _ in range(14):
        updated = le.bump_relationship(world, "p_designer", "p_student", now=NOW)
        assert updated is not None
        sequence.append((updated["shared_events"], updated["closeness"]))

    # shared_events 每次 +1
    assert [shared for shared, _closeness in sequence] == list(range(1, 15))
    # closeness 只在 shared_events ∈ {2,5,9} 时 +1，且 14 次后已被上限 5 截住
    assert sequence == [
        (1, 2), (2, 3), (3, 3), (4, 3), (5, 4), (6, 4), (7, 4),
        (8, 4), (9, 5), (10, 5), (11, 5), (12, 5), (13, 5), (14, 5),
    ]
    # 单调不减 + 封顶
    closeness_values = [closeness for _shared, closeness in sequence]
    assert closeness_values == sorted(closeness_values)
    assert max(closeness_values) == le.MAX_CLOSENESS
    assert set(le.CLOSENESS_BUMPS) <= {shared for shared, _c in sequence}

    stored = _read_relationship(world, "p_designer", "p_student")
    assert stored is not None
    assert stored["id"] == rel_id
    assert stored["shared_events"] == 14
    assert stored["closeness"] == 5
    assert stored["last_shared_at"] == _iso(NOW)  # now 注入可复现
    assert stored["updated_at"] == _iso(NOW)


def test_bump_relationship_keeps_existing_high_closeness(world):
    """单调不减：既有手写 closeness=5 的关系不会因为按比例重算而掉回 2。"""
    _insert_relationship(world, "p_designer", "p_student", closeness=5, shared_events=0)
    updated = le.bump_relationship(world, "p_designer", "p_student", now=NOW)
    assert updated is not None
    assert updated["closeness"] == 5
    assert updated["shared_events"] == 1


def test_bump_relationship_never_creates_missing_relationship(world):
    before = _count(world, "SELECT COUNT(*) AS n FROM persona_relationships")
    assert le.bump_relationship(world, "p_designer", "p_student", now=NOW) is None
    assert le.bump_relationship(world, "p_designer", "p_designer", now=NOW) is None
    assert _count(world, "SELECT COUNT(*) AS n FROM persona_relationships") == before


def test_refresh_statuses_bumps_relationship_once_per_completed_event(world):
    _insert_relationship(world, "p_designer", "p_student", closeness=2, shared_events=0)
    event_id, _ = le.add_event(world, _event(
        persona_id="p_cafe",
        event_type="social",
        title="Dinner together",
        event_status="planned",
        participants=["p_designer", "p_student"],
        start_time=_iso(NOW - timedelta(hours=3)),
        end_time=_iso(NOW - timedelta(hours=2)),
        dedupe_key="k:rel:once",
    ))

    assert le.refresh_statuses(world, now=NOW) >= 1
    by_id = {event["id"]: event for event in _events(world)}
    assert by_id[event_id]["event_status"] == "completed"

    stored = _read_relationship(world, "p_designer", "p_student")
    assert stored["shared_events"] == 1
    assert stored["closeness"] == 2  # 1 次还没到第一个阈值
    assert stored["last_shared_at"] == _iso(NOW)

    # 幂等：再 refresh 不会重复 +1
    assert le.refresh_statuses(world, now=NOW) == 0
    assert _read_relationship(world, "p_designer", "p_student")["shared_events"] == 1

    # 第二次共同经历 → shared_events=2 触发 closeness +1
    le.add_event(world, _event(
        persona_id="p_cafe",
        event_type="social",
        title="Another dinner together",
        event_status="planned",
        participants=["p_student", "p_designer"],  # 顺序无关
        start_time=_iso(NOW - timedelta(hours=1)),
        end_time=_iso(NOW - timedelta(minutes=30)),
        dedupe_key="k:rel:twice",
    ))
    le.refresh_statuses(world, now=NOW)
    stored = _read_relationship(world, "p_designer", "p_student")
    assert stored["shared_events"] == 2
    assert stored["closeness"] == 3


def test_refresh_statuses_bumps_subject_with_single_participant(world):
    """新口径：主体 + 1 个参与者 = 2 人参与集合 → 算一次共同经历。

    真实数据里 LLM 叙事事件的 ``participants`` 不含主体
    （``persona_id='p_annika', participants=['p_x']`` 就是 Annika 和对方一起喝酒）。
    """
    _insert_relationship(world, "p_designer", "p_student", closeness=2, shared_events=0)
    event_id, _ = le.add_event(world, _event(
        persona_id="p_designer",
        event_type="social",
        title="Quick beer with the student",
        event_status="planned",
        participants=["p_student"],
        start_time=_iso(NOW - timedelta(hours=2)),
        end_time=_iso(NOW - timedelta(hours=1)),
        dedupe_key="k:rel:subject-plus-one",
    ))

    le.refresh_statuses(world, now=NOW)
    by_id = {event["id"]: event for event in _events(world)}
    assert by_id[event_id]["event_status"] == "completed"
    stored = _read_relationship(world, "p_designer", "p_student")
    assert stored["shared_events"] == 1
    assert stored["closeness"] == 2

    # 幂等：再刷新不加
    assert le.refresh_statuses(world, now=NOW) == 0
    assert _read_relationship(world, "p_designer", "p_student")["shared_events"] == 1


def test_refresh_statuses_bumps_all_pairs_for_real_shaped_event(world):
    """真实数据形状：participants 不含主体 → 主体也必须参与配对（三对都要 bump）。"""
    _insert_relationship(world, "p_designer", "p_student", closeness=2, shared_events=0)
    _insert_relationship(world, "p_designer", "p_cafe", closeness=2, shared_events=0)
    _insert_relationship(world, "p_student", "p_cafe", closeness=2, shared_events=0)

    # 与库里的真实数据同形：persona_id 是主体，participants 只列另外两人
    event_id, _ = le.add_event(world, _event(
        persona_id="p_designer",
        event_type="social",
        title="Pub quiz: second place by one point",
        event_status="planned",
        participants=["p_student", "p_cafe"],
        start_time=_iso(NOW - timedelta(hours=3)),
        end_time=_iso(NOW - timedelta(hours=2)),
        dedupe_key="k:rel:three-way",
    ))

    le.refresh_statuses(world, now=NOW)
    by_id = {event["id"]: event for event in _events(world)}
    assert by_id[event_id]["event_status"] == "completed"
    for first, second in (("p_designer", "p_student"), ("p_designer", "p_cafe"),
                          ("p_student", "p_cafe")):
        stored = _read_relationship(world, first, second)
        assert stored["shared_events"] == 1, (first, second)
    # 去重后每对只 +1（不会因为列表顺序/重复出现而多加）
    assert le.refresh_statuses(world, now=NOW) == 0
    for first, second in (("p_designer", "p_student"), ("p_designer", "p_cafe"),
                          ("p_student", "p_cafe")):
        assert _read_relationship(world, first, second)["shared_events"] == 1


def test_refresh_statuses_skips_bump_for_solo_event_and_dirty_ids(world):
    _insert_relationship(world, "p_designer", "p_student", closeness=2, shared_events=0)
    # 脏数据关系（对方不在 personas 表里）也不能被 bump
    _insert_relationship(world, "p_designer", "p_ghost", closeness=2, shared_events=0)

    # 独角事件：参与集合只有主体 1 人 → 不 bump
    solo_id, _ = le.add_event(world, _event(
        persona_id="p_designer",
        event_type="personal_milestone",
        title="Third attempt at the sourdough",
        event_status="planned",
        participants=[],
        start_time=_iso(NOW - timedelta(hours=2)),
        end_time=_iso(NOW - timedelta(hours=1)),
        dedupe_key="k:rel:solo",
    ))
    # 脏 id：participants 里是 personas 表里不存在的人 → 过滤后只剩主体 1 人
    dirty_id, _ = le.add_event(world, _event(
        persona_id="p_designer",
        event_type="social",
        title="Met a ghost",
        event_status="planned",
        participants=["p_ghost", ""],
        start_time=_iso(NOW - timedelta(hours=2)),
        end_time=_iso(NOW - timedelta(hours=1)),
        dedupe_key="k:rel:dirty",
    ))
    # 已取消的多参与者事件 → 不加
    cancelled_id, _ = le.add_event(world, _event(
        persona_id="p_designer",
        event_type="social",
        title="Cancelled dinner",
        event_status="cancelled",
        participants=["p_student"],
        start_time=_iso(NOW - timedelta(hours=2)),
        end_time=_iso(NOW - timedelta(hours=1)),
        dedupe_key="k:rel:cancelled",
    ))

    le.refresh_statuses(world, now=NOW)
    by_id = {event["id"]: event for event in _events(world)}
    assert by_id[solo_id]["event_status"] == "completed"
    assert by_id[dirty_id]["event_status"] == "completed"
    assert by_id[cancelled_id]["event_status"] == "cancelled"
    assert _read_relationship(world, "p_designer", "p_student")["shared_events"] == 0
    assert _read_relationship(world, "p_designer", "p_ghost")["shared_events"] == 0


def test_relationship_summary_lists_acquaintances(world):
    _insert_relationship(world, "p_designer", "p_student", closeness=4, shared_events=7,
                         relation_type="friend", last_shared_at=_iso(NOW))
    _insert_relationship(world, "p_designer", "p_cafe", closeness=2, shared_events=1,
                         relation_type="colleague")

    summary = le.relationship_summary(world, "p_designer")
    assert [item["persona_id"] for item in summary][:2] == ["p_student", "p_cafe"]
    top = summary[0]
    assert top["nickname"] == "Kenji"  # 昵称来自 personas
    assert top["relation_type"] == "friend"
    assert top["closeness"] == 4
    assert top["shared_events"] == 7
    assert top["last_shared_at"] == _iso(NOW)

    assert len(le.relationship_summary(world, "p_designer", limit=1)) == 1
    assert le.relationship_summary(world, "p_nobody") == []


def test_relationship_evolution_degrades_on_unmigrated_db(db):
    """老库还没跑 ``shared_events`` / ``last_shared_at`` 迁移时：读给默认值，写静默失败。

    真实库上线前就是这个状态（列是后来 ALTER 加的）。生活事件主链路
    （状态推进 / 情绪）不能被"关系写不进去"拖垮。
    """
    _insert_persona(db, {"id": "p_old_a", "nickname": "Old A", "timezone": "UTC"})
    _insert_persona(db, {"id": "p_old_b", "nickname": "Old B", "timezone": "UTC"})
    with db._lock, db.conn:  # noqa: SLF001 - 手工造一张"未迁移"的老表
        db.conn.execute("DROP TABLE persona_relationships")
        db.conn.execute(
            "CREATE TABLE persona_relationships ("
            " id TEXT PRIMARY KEY, persona_a TEXT NOT NULL, persona_b TEXT NOT NULL,"
            " relation_type TEXT NOT NULL DEFAULT 'friend',"
            " closeness INTEGER NOT NULL DEFAULT 2,"
            " created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
            " UNIQUE(persona_a, persona_b))"
        )
        db.conn.execute(
            "INSERT INTO persona_relationships (id, persona_a, persona_b, relation_type,"
            " closeness, created_at, updated_at) VALUES ('pr_legacy','p_old_a','p_old_b',"
            " 'friend', 3, '', '')"
        )

    rel = le.relationship_between(db, "p_old_a", "p_old_b")
    assert rel is not None
    assert rel["shared_events"] == 0  # 缺列 → 默认值，不崩
    assert rel["last_shared_at"] == ""
    assert rel["closeness"] == 3
    assert le.bump_relationship(db, "p_old_a", "p_old_b", now=NOW) is None  # 写失败静默返回 None
    assert len(le.relationship_summary(db, "p_old_a")) == 1

    # 多参与者事件完成后，状态推进与情绪照常，关系写失败不影响主链路
    _set_emotion(db, "p_old_a", EMOTION_BASE)
    event_id, _ = le.add_event(db, _event(
        persona_id="p_old_a",
        event_type="social",
        title="Old dinner",
        event_status="planned",
        participants=["p_old_a", "p_old_b"],
        start_time=_iso(NOW - timedelta(hours=2)),
        end_time=_iso(NOW - timedelta(hours=1)),
        dedupe_key="k:rel:legacy",
    ))
    assert le.refresh_statuses(db, now=NOW) >= 1
    by_id = {event["id"]: event for event in _events(db)}
    assert by_id[event_id]["event_status"] == "completed"
    assert _get_emotion(db, "p_old_a") != EMOTION_BASE  # 情绪仍然生效


def test_relationship_closeness_raises_comment_probability(world):
    """联测：共同经历变多 → closeness↑ → event_posts 的评论概率↑（写入端驱动行为端）。"""
    event_posts = pytest.importorskip("app.v3.event_posts")
    persona = {"id": "p_designer", "emotion_state": "{}"}
    traits = {"warmth": 0.5}

    low = event_posts.comment_probability(persona, traits, {"closeness": 2})
    high = event_posts.comment_probability(persona, traits, {"closeness": 5})
    assert 0.0 < low < high <= 1.0
    # event_posts 读的 1..5 语义与我们的写入语义一致
    assert event_posts._relationship_closeness({"closeness": 2}) == 0.25  # noqa: SLF001
    assert event_posts._relationship_closeness({"closeness": 5}) == 1.0  # noqa: SLF001

    # 真实链路：bump 到 closeness=5 后，评论概率确实比初始 closeness=2 高
    _insert_relationship(world, "p_designer", "p_student", closeness=2, shared_events=0)
    initial = le.relationship_between(world, "p_designer", "p_student")
    for _ in range(9):  # shared_events=2/5/9 各 +1 → closeness 2→5
        le.bump_relationship(world, "p_designer", "p_student", now=NOW)
    final = le.relationship_between(world, "p_designer", "p_student")
    assert final["closeness"] == 5
    assert event_posts.comment_probability(persona, traits, final) > event_posts.comment_probability(
        persona, traits, initial
    )


# --------------------------------------------------------------------------- #
# 关系张力（第三阶段）：raise / ease / recent_event / prompt
# --------------------------------------------------------------------------- #
def test_raise_ease_tension_clamp_and_boundaries(world):
    _insert_relationship(world, "p_designer", "p_student", closeness=3, tension=0)

    # raise：每次 +1，clamp 到 5，并更新 last_conflict_at
    raised = [le.raise_tension(world, "p_designer", "p_student", now=NOW)["tension"]
              for _ in range(7)]
    assert raised == [1, 2, 3, 4, 5, 5, 5]
    stored = _read_relationship(world, "p_designer", "p_student")
    assert stored["tension"] == le.MAX_TENSION == 5
    assert stored["last_conflict_at"] == _iso(NOW)

    # ease：每次 -1，clamp 不为负；不改 last_conflict_at
    eased = [le.ease_tension(world, "p_designer", "p_student", now=NOW)["tension"]
             for _ in range(7)]
    assert eased == [4, 3, 2, 1, 0, 0, 0]
    stored = _read_relationship(world, "p_designer", "p_student")
    assert stored["tension"] == 0
    assert stored["last_conflict_at"] == _iso(NOW)  # 仍是最后一次冲突时间

    # delta：绝对值生效、脏输入回退 1、0 表示不改
    assert le.raise_tension(world, "p_designer", "p_student", now=NOW, delta=2)["tension"] == 2
    assert le.raise_tension(world, "p_designer", "p_student", now=NOW, delta="junk")["tension"] == 3
    assert le.raise_tension(world, "p_designer", "p_student", now=NOW, delta=0)["tension"] == 3
    assert le.ease_tension(world, "p_designer", "p_student", now=NOW, delta=-2)["tension"] == 1

    # 关系不存在 → None，且不凭空造关系
    before = _count(world, "SELECT COUNT(*) AS n FROM persona_relationships")
    assert le.raise_tension(world, "p_designer", "p_cafe", now=NOW) is None
    assert le.ease_tension(world, "p_designer", "p_cafe", now=NOW) is None
    assert le.raise_tension(world, "p_designer", "p_designer", now=NOW) is None
    assert _count(world, "SELECT COUNT(*) AS n FROM persona_relationships") == before


def test_refresh_statuses_applies_tension_by_event_type(world):
    _insert_relationship(world, "p_designer", "p_student", closeness=3, tension=2)
    counter = {"tag": 0}

    def complete(event_type: str, title: str) -> None:
        counter["tag"] += 1
        le.add_event(world, _event(
            persona_id="p_designer",
            event_type=event_type,
            title=title,
            event_status="planned",
            participants=["p_student"],
            start_time=_iso(NOW - timedelta(hours=2)),
            end_time=_iso(NOW - timedelta(hours=1)),
            dedupe_key=f"k:tension:{counter['tag']}",
        ))
        le.refresh_statuses(world, now=NOW)

    def rel() -> dict:
        return _read_relationship(world, "p_designer", "p_student")

    # conflict → tension +1、shared_events 仍 +1、记 last_conflict_at
    complete("conflict", "Pub quiz argument")
    assert rel()["tension"] == 3
    assert rel()["shared_events"] == 1
    assert rel()["last_conflict_at"] == _iso(NOW)
    assert le.refresh_statuses(world, now=NOW) == 0  # 幂等
    assert rel()["tension"] == 3 and rel()["shared_events"] == 1

    # social → tension -1（和解），shared_events +1
    complete("social", "Coffee after work")
    assert rel()["tension"] == 2
    assert rel()["shared_events"] == 2

    # achievement / entertainment / relationship 也是和解类
    complete("achievement", "Finished the project together")
    complete("entertainment", "Cinema night")
    complete("relationship", "A proper talk")
    assert rel()["tension"] == 0  # 3 次 ease 从 3? 实际 2→1→0→0（恰好到底）
    assert rel()["shared_events"] == 5

    # 其他类型只 bump，不动 tension
    complete("work", "Worked on the brief")
    assert rel()["tension"] == 0
    assert rel()["shared_events"] == 6

    # cancelled conflict 不产生任何变化
    le.add_event(world, _event(
        persona_id="p_designer", event_type="conflict", title="Cancelled argument",
        event_status="cancelled", participants=["p_student"],
        start_time=_iso(NOW - timedelta(hours=2)), end_time=_iso(NOW - timedelta(hours=1)),
        dedupe_key="k:tension:cancelled",
    ))
    le.refresh_statuses(world, now=NOW)
    assert rel()["tension"] == 0
    assert rel()["shared_events"] == 6

    # conflict 但关系不存在 → 不崩、不造关系
    before = _count(world, "SELECT COUNT(*) AS n FROM persona_relationships")
    le.add_event(world, _event(
        persona_id="p_designer", event_type="conflict", title="Argument with a stranger",
        event_status="planned", participants=["p_cafe"],
        start_time=_iso(NOW - timedelta(hours=2)), end_time=_iso(NOW - timedelta(hours=1)),
        dedupe_key="k:tension:nobody",
    ))
    le.refresh_statuses(world, now=NOW)
    assert _count(world, "SELECT COUNT(*) AS n FROM persona_relationships") == before


def test_relationship_summary_recent_event_and_sorting(world):
    _insert_relationship(world, "p_designer", "p_student", closeness=4, shared_events=3,
                         tension=1, last_shared_at=_iso(NOW))
    _insert_relationship(world, "p_designer", "p_cafe", closeness=4, shared_events=3,
                         tension=0, last_shared_at=_iso(NOW))
    _insert_relationship(world, "p_designer", "p_freelance", closeness=2, shared_events=1)

    # 共同经历事件（真实形状：participants 不含主体）
    def add_shared(tag: str, when: datetime, *, persona_id="p_designer",
                   participants=("p_student",), status="completed", etype="social") -> str:
        event_id, _ = le.add_event(world, _event(
            persona_id=persona_id, event_type=etype, title=tag, event_status=status,
            participants=list(participants),
            start_time=_iso(when), end_time=_iso(when + timedelta(hours=1)),
            dedupe_key=f"k:recent:{tag}",
        ))
        return event_id

    older = NOW - timedelta(days=3)
    newer = NOW - timedelta(days=1)
    add_shared("Jazz bar with Sophie", older)
    add_shared("Coffee after the design review", newer)
    # 更近的事件但还没完成 / 已取消 → recent_event 不能取
    add_shared("Planned dinner", NOW, status="planned")
    add_shared("Cancelled jazz", NOW, status="cancelled")

    summary = le.relationship_summary(world, "p_designer")
    assert [item["persona_id"] for item in summary] == ["p_cafe", "p_student", "p_freelance"]
    # shared_events DESC → closeness DESC → tension ASC 的次级排序
    assert summary[0]["shared_events"] == summary[1]["shared_events"] == 3
    assert summary[0]["tension"] == 0 < summary[1]["tension"] == 1
    assert summary[2]["shared_events"] == 1

    student = next(item for item in summary if item["persona_id"] == "p_student")
    assert student["recent_event"] is not None
    assert student["recent_event"]["title"] == "Coffee after the design review"  # 最近一条 completed
    assert student["recent_event"]["event_type"] == "social"
    assert student["recent_event"]["start_time"] == _iso(newer)
    assert set(student["recent_event"]) == {"title", "event_type", "start_time"}
    assert student["tension"] == 1 and student["last_shared_at"] == _iso(NOW)
    assert student["nickname"] == "Kenji"

    # 没有共同完成事件的熟人 → None
    freelance = next(item for item in summary if item["persona_id"] == "p_freelance")
    assert freelance["recent_event"] is None

    # limit 生效
    assert len(le.relationship_summary(world, "p_designer", limit=1)) == 1
    assert le.relationship_summary(world, "p_nobody") == []


def test_relationship_summary_recent_event_uses_single_event_query(world):
    """性能契约：最多 5 条关系，但事件查询只能有 1 次（不许 N+1）。"""
    for other in ("p_student", "p_cafe", "p_freelance"):
        _insert_relationship(world, "p_designer", other, shared_events=1)
    statements: list[str] = []
    world.conn.set_trace_callback(statements.append)
    try:
        summary = le.relationship_summary(world, "p_designer")
    finally:
        world.conn.set_trace_callback(None)
    assert len(summary) == 3
    event_queries = [sql for sql in statements if "FROM persona_events" in sql]
    assert len(event_queries) == 1, event_queries


def test_narrative_prompt_marks_tense_acquaintances(world):
    _insert_relationship(world, "p_designer", "p_student", closeness=3, tension=4,
                         last_conflict_at=_iso(NOW))
    _insert_relationship(world, "p_designer", "p_cafe", closeness=3, tension=1)
    acquaintances = le._acquaintances(world, "p_designer")  # noqa: SLF001
    by_id = {item["id"]: item for item in acquaintances}
    assert by_id["p_student"]["tension"] == 4
    assert by_id["p_cafe"]["tension"] == 1

    persona = _persona(world, "p_designer")
    prompt = le._narrative_prompt(persona, DAY, acquaintances, [], 2)  # noqa: SLF001
    lines = [line for line in prompt.splitlines() if line.startswith("- id=")]
    tense_line = next(line for line in lines if "p_student" in line)
    calm_line = next(line for line in lines if "p_cafe" in line)
    assert "(currently a bit tense with you)" in tense_line
    assert "(currently a bit tense with you)" not in calm_line
    # 规则文本明确要求不要把 tense 的人写成愉快共同活动
    assert "Do NOT write those" in prompt
    assert "currently a bit tense with you" in prompt

    # 端到端：generate_narrative_events 真的把带标记的 prompt 发给 LLM
    class _CapturingLLM(_MockLLM):
        def __init__(self) -> None:
            super().__init__({"events": []})
            self.messages: list = []

        async def complete_json(self, messages, *, schema_hint="", temperature=None,
                                max_tokens=None):
            self.messages.append(messages)
            return await super().complete_json(messages, schema_hint=schema_hint,
                                               temperature=temperature, max_tokens=max_tokens)

    llm = _CapturingLLM()
    asyncio.run(le.generate_narrative_events(world, llm, persona, DAY))
    sent = "\n".join(str(m.get("content") or "") for m in llm.messages[0])
    assert "p_student" in sent
    assert tense_line in sent  # 同一个带标记的熟人行


# --------------------------------------------------------------------------- #
# generate_day_events
# --------------------------------------------------------------------------- #
def test_generate_day_events_rule_only_is_idempotent(world):
    llm = SimpleNamespace(uses_mock=True)
    first = asyncio.run(le.generate_day_events(world, llm, day=DAY))
    assert first["skipped"] is False
    assert first["created"] > 0
    assert first["source"] == "rule"
    assert first["personas"] == len(PERSONAS)
    # 规模：每人每天 ≤ 上限条规则事件（12 人才会到几十条，属预期）
    assert first["created"] == len(PERSONAS) * le.RULE_EVENTS_PER_DAY_DEFAULT
    total = first["events"]
    assert total == first["created"]

    stored = _events(world)
    assert len(stored) == total
    assert all(event["source_type"] == "rule" for event in stored)
    assert all(event["event_status"] in ("planned", "ongoing", "completed") for event in stored)
    assert _count(world, "SELECT COUNT(DISTINCT dedupe_key) AS n FROM persona_events") == total

    # 同一天连跑第二次：一条都不能新增（幂等标记 + dedupe_key 双保险）
    second = asyncio.run(le.generate_day_events(world, llm, day=DAY))
    assert second["created"] == 0
    assert second["skipped"] is True
    assert second["source"] == "skipped"
    assert second["events"] == total
    assert _count(world, "SELECT COUNT(*) AS n FROM persona_events") == total


def test_generate_narrative_events_validates_participants_and_conflicts(world):
    le.ensure_relationships(world)
    with world._lock:  # noqa: SLF001
        row = world.conn.execute(
            "SELECT * FROM persona_relationships WHERE persona_a='p_designer' OR persona_b='p_designer'"
        ).fetchone()
    assert row is not None
    partner = row["persona_b"] if row["persona_a"] == "p_designer" else row["persona_a"]

    # 先把当天规则事件落库：10:00-11:00 应该撞上设计师的工作块
    for event in le.rule_events_for_day(_persona(world, "p_designer"), DAY):
        le.add_event(world, event)

    payload = {"events": [
        {
            "event_type": "achievement",
            "title": "Finished the mural commission",
            "description": "Delivered a day early.",
            "start_time": "13:30", "end_time": "13:55",
            "participants": [partner, "p_ghost"], "location": "Studio",
            "visibility": "public",
        },
        {
            "event_type": "conflict",
            "title": "Argued about the rent",
            "description": "", "start_time": "10:00", "end_time": "11:00",
            "participants": [], "location": "Home",
        },
    ]}
    llm = _MockLLM(payload)
    events = asyncio.run(
        le.generate_narrative_events(world, llm, _persona(world, "p_designer"), DAY)
    )
    assert llm.calls == 1  # 每人每天只调用一次
    assert len(events) == 1  # 冲突的那条被丢掉
    event = events[0]
    assert event["event_type"] == "achievement"
    assert event["event_status"] == "planned"
    assert event["source_type"] == "llm"
    assert event["participants"] == [partner]  # 不认识的 p_ghost 被丢弃
    assert event["dedupe_key"] == f"p_designer:achievement:{DAY}:1"
    # §5.3 事件 dict 形状齐全
    assert {
        "persona_id", "event_type", "title", "description", "start_time", "end_time",
        "participants", "location", "related_personas", "event_status", "source_type",
        "source_reference", "visibility", "metadata", "dedupe_key", "day",
    } <= set(event)

    # 落库 → 还是 planned（计划不等于事实）
    event_id, created = le.add_event(world, event)
    assert created is True
    # This checks persisted status, independent of how far the fixture day is in the past.
    assert le.events_for_persona(world, "p_designer", statuses=["planned"], since_days=0)
    assert event_id


def test_generate_day_events_uses_llm_and_stays_idempotent(world):
    # 用未来的一天：refresh_statuses 不会把当天事件追认成 completed，
    # 才能验证"叙事事件生来是 planned"（今天的情况见 event_fact_block 用例）。
    day = _future_day(1)
    payload = {"events": [
        {
            "event_type": "achievement", "title": "Finished the mural commission",
            "description": "Delivered early.", "start_time": "23:00", "end_time": "23:30",
            "participants": [], "location": "Studio", "visibility": "public",
        }
    ]}
    llm = _MockLLM(payload)
    first = asyncio.run(le.generate_day_events(world, llm, day=day))
    assert first["source"] == "llm"
    assert llm.calls == len(PERSONAS)  # 每个 persona 最多 1 次
    llm_events = [event for event in _events(world) if event["source_type"] == "llm"]
    assert llm_events, "至少有一个叙事事件落库"
    assert all(event["event_status"] == "planned" for event in llm_events)
    assert all(event["day"] == day for event in llm_events)

    # force 重跑：dedupe_key 已存在 → 不会重复写入
    again = asyncio.run(le.generate_day_events(world, llm, day=day, force=True))
    assert again["created"] == 0
    assert _count(world, "SELECT COUNT(*) AS n FROM persona_events") == first["events"]

    # 非 force 再跑：幂等标记直接跳过，created 必须是 0
    repeat = asyncio.run(le.generate_day_events(world, llm, day=day))
    assert repeat["created"] == 0
    assert repeat["skipped"] is True
    assert _count(world, "SELECT COUNT(*) AS n FROM persona_events") == first["events"]


def test_generate_day_events_survives_llm_failure(world):
    llm = _MockLLM(raises=True)
    result = asyncio.run(le.generate_day_events(world, llm, day=DAY))
    assert result["created"] > 0
    assert result["source"] == "rule"  # 静默回落到规则事件
    assert all(event["source_type"] == "rule" for event in _events(world))


def test_generate_day_events_respects_life_llm_env(world, monkeypatch):
    monkeypatch.setenv("SE_LIFE_LLM", "0")
    llm = _MockLLM({"events": []})
    result = asyncio.run(le.generate_day_events(world, llm, day=DAY))
    assert llm.calls == 0  # 关掉开关 = 零 LLM 调用
    assert result["source"] == "rule"
    assert result["created"] > 0


# --------------------------------------------------------------------------- #
# 回归：lifecycle 语义（老库空串 / adopted 也算人物，guest 不算）
# --------------------------------------------------------------------------- #
def test_generate_day_events_covers_legacy_and_adopted_personas_but_not_guests(db):
    """回归：真实库里老种子人物的 ``lifecycle`` 是空串（ALTER TABLE 历史迁移）。

    过滤器必须是「不是 guest」而不是 ``lifecycle='permanent'``，否则主宇宙
    12 个人物一条生活事件都不会生成。
    """
    _insert_persona(db, {
        "id": "p_legacy", "nickname": "Legacy", "occupation": "Product designer",
        "timezone": "UTC", "language": "en", "city": "Manchester",
    }, lifecycle="")  # 老库历史数据
    _insert_persona(db, {
        "id": "p_adopted", "nickname": "Adopted", "occupation": "Teacher",
        "timezone": "UTC", "language": "en", "city": "Leeds",
    }, lifecycle="adopted")  # 游客被互动后转正
    _insert_persona(db, {
        "id": "p_guest", "nickname": "Guest", "occupation": "Barista",
        "timezone": "UTC", "language": "en", "city": "London",
    }, lifecycle="guest")  # 24h 后回收的临时人物

    result = asyncio.run(le.generate_day_events(db, None, day=DAY))
    assert result["created"] > 0
    assert result["personas"] == 2  # guest 不算

    with db._lock:  # noqa: SLF001
        counts = {
            str(row["persona_id"]): int(row["n"])
            for row in db.conn.execute(
                "SELECT persona_id, COUNT(*) AS n FROM persona_events WHERE day=?"
                " GROUP BY persona_id",
                (DAY,),
            ).fetchall()
        }
    per = le.RULE_EVENTS_PER_DAY_DEFAULT
    assert counts.get("p_legacy", 0) == per
    assert counts.get("p_adopted", 0) == per
    assert "p_guest" not in counts

    # 作息只给非游客
    with db._lock:  # noqa: SLF001
        schedule_ids = {
            str(row["persona_id"])
            for row in db.conn.execute("SELECT persona_id FROM persona_schedules").fetchall()
        }
    assert {"p_legacy", "p_adopted"} <= schedule_ids
    assert "p_guest" not in schedule_ids

    # 关系只给非游客，且这两位互相认识
    with db._lock:  # noqa: SLF001
        pairs = db.conn.execute(
            "SELECT persona_a, persona_b FROM persona_relationships"
        ).fetchall()
    assert pairs
    for row in pairs:
        assert "p_guest" not in (row["persona_a"], row["persona_b"])
    connected = {p for row in pairs for p in (row["persona_a"], row["persona_b"])}
    assert connected == {"p_legacy", "p_adopted"}

    # 幂等：第二次 created=0
    again = asyncio.run(le.generate_day_events(db, None, day=DAY))
    assert again["created"] == 0
    assert again["skipped"] is True


def test_generate_day_events_recovers_from_poisoned_marker(db):
    """回归：历史 bug 写过 ``{"events":0,"personas":0}`` 的标记，不能让当天永久跳过。"""
    _insert_persona(db, {
        "id": "p_legacy", "nickname": "Legacy", "occupation": "Product designer",
        "timezone": "UTC", "language": "en", "city": "Manchester",
    }, lifecycle="")
    marker_key = f"{le.META_PREFIX}{DAY}"
    poisoned = json.dumps({"day": DAY, "events": 0, "source": "rule", "personas": 0})
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute("INSERT INTO v3_meta (key, value) VALUES (?,?)", (marker_key, poisoned))

    result = asyncio.run(le.generate_day_events(db, None, day=DAY))
    assert result["skipped"] is False
    assert result["created"] > 0
    assert result["personas"] == 1
    # 标记被真实结果覆盖，之后才允许跳过
    with db._lock:  # noqa: SLF001
        stored = json.loads(db.conn.execute(
            "SELECT value FROM v3_meta WHERE key=?", (marker_key,)
        ).fetchone()["value"])
    assert stored["personas"] == 1 and stored["events"] == result["events"] > 0

    again = asyncio.run(le.generate_day_events(db, None, day=DAY))
    assert again["created"] == 0
    assert again["skipped"] is True


def test_generate_day_events_without_personas_does_not_mark_day(db):
    """没有人物的那次不能写 ``life_events_day:<day>``，否则当天会被永久跳过。"""
    marker_key = f"{le.META_PREFIX}{DAY}"
    assert _count(db, "SELECT COUNT(*) AS n FROM v3_meta WHERE key=?", (marker_key,)) == 0

    first = asyncio.run(le.generate_day_events(db, None, day=DAY))
    assert first["created"] == 0
    assert first["personas"] == 0
    assert first["events"] == 0
    assert first["skipped"] is False  # 不是"已完成"，是"这次没人生成"
    assert _count(db, "SELECT COUNT(*) AS n FROM v3_meta WHERE key=?", (marker_key,)) == 0

    # 人物稍后才到（老库迁移 / 导入）→ 仍然会重试并正常生成
    _insert_persona(db, {
        "id": "p_late", "nickname": "Late", "occupation": "Computer science student",
        "timezone": "UTC", "language": "en", "city": "Kyoto",
    }, lifecycle="")
    second = asyncio.run(le.generate_day_events(db, None, day=DAY))
    assert second["created"] > 0
    assert second["personas"] == 1
    assert _count(db, "SELECT COUNT(*) AS n FROM v3_meta WHERE key=?", (marker_key,)) == 1


# --------------------------------------------------------------------------- #
# 后台循环 / 降级
# --------------------------------------------------------------------------- #
def test_life_loop_startup_generates_today(world):
    from app.v3.daily_world import day_key

    today = day_key()

    async def _run_once():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(le.life_loop(world, None, tick_s=30.0), timeout=0.4)

    asyncio.run(_run_once())
    with world._lock:  # noqa: SLF001
        marker = world.conn.execute(
            "SELECT value FROM v3_meta WHERE key=?", (f"{le.META_PREFIX}{today}",)
        ).fetchone()
    assert marker is not None, "启动时必须生成今天的事件"
    assert _count(world, "SELECT COUNT(*) AS n FROM persona_events WHERE day=?", (today,)) > 0


def test_functions_degrade_when_new_tables_are_missing(tmp_path):
    """老库没有 persona_events / v3_meta → 全部静默降级，不抛异常。"""
    database = DB(str(tmp_path / "old.db"))
    database.init()  # 只有 v2 表
    try:
        assert le.refresh_statuses(database) == 0
        assert le.add_event(database, _event()) == ("", False)
        assert le.events_for_persona(database, "p_designer") == []
        assert le.relevant_events(database, "p_designer", "anything") == []
        assert le.upcoming_events(database, "p_designer") == []
        assert le.ensure_relationships(database, [{"id": "a"}, {"id": "b"}]) == 0
        assert le.ensure_schedule(database, "p_x")["weekday_blocks"]  # 内存作息兜底
        result = asyncio.run(le.generate_day_events(database, None, day=DAY))
        assert result["created"] == 0
    finally:
        database.close()
