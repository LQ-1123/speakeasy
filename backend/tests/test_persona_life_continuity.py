"""Regression checks for grounded time and continuity across generated days."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.v2.db import DB
from app.v3 import life_events as life
from app.v3 import event_posts
from app.v3 import persona_context as context
from app.v3.schema import ensure_schema


NOW = datetime(2026, 11, 12, 6, 30, tzinfo=timezone.utc)


def activity(**changes):
    event = {
        "label": "fixing a shelf",
        "start_time": "2026-11-12T06:00:00.123456+00:00",
        "end_time": "2026-11-12T07:00:00.123456+00:00",
        "event_status": "ongoing",
        "metadata": {"timezone": "Asia/Shanghai", "inferred": False},
    }
    event.update(changes)
    return event


def test_current_activity_uses_personas_local_time():
    line = context.life_now_line(activity(), moment=NOW)
    assert "14:00" in line
    assert "06:00" not in line


@pytest.mark.parametrize("end", ["2026-11-12T06:15:00.123456+00:00", "2026-11-12T06:15:00"])
def test_expired_activity_with_fractional_or_legacy_timestamp_is_not_current(end):
    assert context.life_now_line(activity(end_time=end), moment=NOW) == ""


def test_future_activity_is_not_current():
    assert context.life_now_line(activity(start_time="2026-11-12T07:00:00Z"), moment=NOW) == ""


def test_inferred_schedule_is_not_a_confirmed_experience():
    line = context.life_now_line(activity(metadata={"inferred": True}), moment=NOW)
    assert "schedule" in line.lower()
    assert "unconfirmed" in line.lower()
    assert "Right now:" not in line


def test_life_fact_keeps_the_date_across_weeks():
    event = activity(title="fixing a shelf", event_status="completed")
    assert "2026-11-12" in life.event_fact_block(event)


def test_call_directive_preserves_message_priority_and_optional_questions():
    directive = "\n".join((
        "Action: share if relevant.",
        "Questions are optional; do not interrogate them.",
        "Their actual message takes priority over this suggestion.",
        "Write the reply in English.",
    ))
    assert context.compact_directive(directive) == directive


@pytest.mark.parametrize("builder", [context.build_persona_layer, context.build_call_layer])
def test_prompt_layer_keeps_the_turns_time_snapshot(monkeypatch, builder):
    monkeypatch.setattr(context.life_events_mod, "current_activity", lambda *a, **kw: activity())
    plan = context.build_turn_plan(None, {"persona_id": "p_test"}, "hi", now=NOW)
    monkeypatch.setattr(context, "_now", lambda: NOW + timedelta(days=1))
    assert "fixing a shelf" in builder(None, {}, plan)


@pytest.fixture()
def db(tmp_path):
    database = DB(str(tmp_path / "continuity.db"))
    database.init()
    ensure_schema(database, seed=False)
    yield database
    database.close()


def add_event(db, title, day, *, status="completed", persona_id="p_test", **changes):
    event = {
        "persona_id": persona_id,
        "event_type": "achievement",
        "title": title,
        "description": "Finished putting the first shelf up.",
        "start_time": f"{day}T06:00:00+00:00",
        "end_time": f"{day}T07:00:00+00:00",
        "event_status": status,
        "source_type": "llm",
        "dedupe_key": f"{persona_id}:{title}",
        "day": day,
    }
    event.update(changes)
    return life.add_event(db, event)[0]


def test_stale_open_ended_event_cannot_keep_a_person_permanently_busy(db):
    event_id = add_event(db, "long over", "2026-11-10", status="ongoing", end_time="")
    current = life.current_activity(db, "p_test", now=NOW)
    assert current is None or current.get("id") != event_id


def test_narrative_generation_receives_recent_life_without_unrelated_or_future_events(db, monkeypatch):
    monkeypatch.setenv("SE_LIFE_LLM", "1")
    add_event(db, "the shelf is finally up", "2026-11-11")
    add_event(db, "CANCELLED_BOOKING", "2026-11-11", status="cancelled")
    add_event(db, "SOMEONE_ELSES_NEWS", "2026-11-11", persona_id="p_other")
    add_event(db, "FUTURE_OUTCOME", "2026-11-14")
    add_event(db, "ANCIENT_NEWS", "2026-10-01")

    class RecordingLLM:
        uses_mock = False
        messages = []

        async def complete_json(self, messages, **kwargs):
            self.messages = messages
            return json.dumps({"events": []})

    llm = RecordingLLM()
    result = asyncio.run(life.generate_narrative_events(
        db, llm, {"id": "p_test", "timezone": "Asia/Shanghai"}, "2026-11-12",
    ))
    prompt = "\n".join(message["content"] for message in llm.messages)
    assert result == []
    assert "the shelf is finally up" in prompt
    for excluded in ("CANCELLED_BOOKING", "SOMEONE_ELSES_NEWS", "FUTURE_OUTCOME", "ANCIENT_NEWS"):
        assert excluded not in prompt
    assert "2026-11-11" in prompt


def test_chat_facts_use_event_time_and_exclude_future_completed_rows(db, monkeypatch):
    monkeypatch.setattr(event_posts, "_utcnow", lambda: NOW)
    add_event(db, "shelf finally up", "2026-11-11", completed_at="2026-11-12T05:00:00+00:00")
    add_event(db, "FUTURE_EVENT", "2026-11-15", completed_at="2026-11-12T05:00:00+00:00")
    add_event(db, "OLD_BACKFILLED_EVENT", "2026-10-01", completed_at="2026-11-12T05:00:00+00:00")
    block = event_posts.persona_recent_facts(db, "p_test")
    assert "DONE 2026-11-11: shelf finally up" in block
    assert "FUTURE_EVENT" not in block
    assert "OLD_BACKFILLED_EVENT" not in block


def test_chat_facts_do_not_expose_scheduled_posts_or_future_comments(db, monkeypatch):
    monkeypatch.setattr(event_posts, "_utcnow", lambda: NOW)
    with db._lock, db.conn:
        db.conn.execute("INSERT INTO personas (id, nickname, created_at) VALUES ('p_test', 'Test', ?)",
                        (NOW.isoformat(),))
        db.conn.executemany(
            "INSERT INTO persona_posts (id, persona_id, content, created_at, published_at) VALUES (?,?,?,?,?)",
            [("past", "p_test", "PAST_POST", "2026-11-11T06:00:00+00:00", "2026-11-11T06:00:00+00:00"),
             ("future", "p_test", "FUTURE_POST", "2026-11-11T06:00:00+00:00", "2026-11-14T06:00:00+00:00")],
        )
        db.conn.executemany(
            "INSERT INTO persona_post_comments (id, post_id, content, created_at) VALUES (?,?,?,?)",
            [("c1", "past", "PAST_COMMENT", "2026-11-11T07:00:00+00:00"),
             ("c2", "past", "FUTURE_COMMENT", "2026-11-14T07:00:00+00:00"),
             ("c3", "future", "UNPUBLISHED_COMMENT", "2026-11-11T07:00:00+00:00")],
        )
    block = event_posts.persona_recent_facts(db, "p_test")
    assert "PAST_POST" in block and "PAST_COMMENT" in block
    assert "FUTURE_POST" not in block
    assert "FUTURE_COMMENT" not in block
    assert "UNPUBLISHED_COMMENT" not in block
