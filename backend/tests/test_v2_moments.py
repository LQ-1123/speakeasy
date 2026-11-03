"""v2 朋友圈生成离线单测：mock LLM + 临时 SQLite，全程不联网。"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.v2.db import DB
from app.v2.moments import MAX_WORDS, MIN_WORDS, generate_daily, moment_for_prompt

NOW = datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc)  # Rome 09:00 / Mumbai 13:30 / New York 03:00
FEED_DATE = "2026-11-03"


class MockLLM:
    uses_mock = True

    def __init__(self) -> None:
        self.calls = 0

    async def complete_json(self, *args, **kwargs):  # pragma: no cover - 不该走到
        self.calls += 1
        raise AssertionError("mock 模式不应真正调用 LLM")


class FakeJSONLLM:
    uses_mock = False

    def __init__(self, payload) -> None:
        self.payload = payload
        self.calls: list = []

    async def complete_json(self, messages, **kwargs):
        self.calls.append(messages)
        if isinstance(self.payload, str):
            return self.payload
        return json.dumps(self.payload, ensure_ascii=False)


@pytest.fixture()
def db(tmp_path):
    database = DB(str(tmp_path / "v2.db"))
    database.init()
    yield database
    database.close()


def make_friend(db: DB, **overrides) -> dict:
    data = {
        "name": "Marco",
        "nationality": "Italy",
        "city": "Rome",
        "timezone": "Europe/Rome",
        "native_lang": "it",
        "occupation": "barista",
        "personality_prompt": "Warm, sarcastic, obsessed with espresso.",
        "age": 27,
        "status": "active",
    }
    data.update(overrides)
    return db.create_friend(data)


def _valid_words(content: str) -> bool:
    return MIN_WORDS <= len(content.split()) <= MAX_WORDS


# --------------------------------------------------------------------------- #
# 确定性兜底
# --------------------------------------------------------------------------- #
async def test_generate_daily_fallback_with_active_filter(db):
    marco = make_friend(db)
    priya = make_friend(
        db, name="Priya", nationality="India", city="Mumbai", timezone="Asia/Kolkata", native_lang="hi"
    )
    emily = make_friend(
        db,
        name="Emily",
        nationality="United States",
        city="New York",
        timezone="America/New_York",
        status="pending_request",
    )

    created = await generate_daily(db, MockLLM(), db.list_friends(), feed_date=FEED_DATE, now=NOW)

    assert len(created) == 2
    assert {moment["friend_id"] for moment in created} == {marco["id"], priya["id"]}
    assert emily["id"] not in {moment["friend_id"] for moment in created}

    for moment in created:
        assert _valid_words(moment["content"]), moment["content"]
        assert moment["mood_tag"]
        assert moment["local_time_str"]
        assert "·" in moment["local_time_str"]
        assert moment["feed_date"] == FEED_DATE

    rows = db.list_moments()
    assert len(rows) == 2
    marco_moment = next(row for row in rows if row["friend_id"] == marco["id"])
    assert "Rome" in marco_moment["local_time_str"]
    assert marco_moment["mood_tag"] == "☕ 晨间咖啡"  # Rome 09:00 → morning


async def test_generate_daily_is_idempotent(db):
    make_friend(db)
    make_friend(db, name="Lena", nationality="Germany", city="Berlin", timezone="Europe/Berlin")

    first = await generate_daily(db, MockLLM(), db.list_friends(), feed_date=FEED_DATE, now=NOW)
    assert len(first) == 2

    second = await generate_daily(db, MockLLM(), db.list_friends(), feed_date=FEED_DATE, now=NOW)
    assert {moment["id"] for moment in second} == {moment["id"] for moment in first}
    assert len(db.list_moments()) == 2


async def test_generate_daily_empty_and_pending_only(db):
    pending = make_friend(db, status="pending_request")
    assert await generate_daily(db, MockLLM(), [pending], feed_date=FEED_DATE, now=NOW) == []
    assert await generate_daily(db, MockLLM(), [], feed_date=FEED_DATE, now=NOW) == []
    assert db.list_moments() == []


async def test_generate_daily_respects_per_friend_max(db):
    friend = make_friend(db)
    created = await generate_daily(
        db, MockLLM(), [friend], feed_date=FEED_DATE, per_friend_max=2, now=NOW
    )
    assert len(created) == 2
    assert all(moment["friend_id"] == friend["id"] for moment in created)
    assert created[0]["content"] != created[1]["content"]
    assert all(_valid_words(moment["content"]) for moment in created)


async def test_fallback_is_deterministic(db):
    friend = make_friend(db, id="f-fixed")
    first = await generate_daily(db, MockLLM(), [friend], feed_date=FEED_DATE, now=NOW)

    other_path = db.path + "-other"
    other = DB(other_path)
    other.init()
    try:
        other_friend = other.create_friend(
            {
                "id": "f-fixed",
                "name": friend["name"],
                "nationality": friend["nationality"],
                "city": friend["city"],
                "timezone": friend["timezone"],
                "native_lang": friend["native_lang"],
                "occupation": friend["occupation"],
                "personality_prompt": friend["personality_prompt"],
                "age": friend["age"],
                "status": "active",
            }
        )
        second = await generate_daily(other, MockLLM(), [other_friend], feed_date=FEED_DATE, now=NOW)
    finally:
        other.close()

    assert first[0]["content"] == second[0]["content"]
    assert first[0]["mood_tag"] == second[0]["mood_tag"]


# --------------------------------------------------------------------------- #
# LLM 路径 + 合规校验
# --------------------------------------------------------------------------- #
async def test_llm_path_uses_valid_items_and_falls_back_for_bad_ones(db):
    marco = make_friend(db)
    lena = make_friend(db, name="Lena", nationality="Germany", city="Berlin", timezone="Europe/Berlin")
    llm_content = (
        "Just finished a long walk around the old neighbourhood and the light was unreal today, honestly."
    )
    llm = FakeJSONLLM(
        {
            "moments": [
                {"friend_id": marco["id"], "content": llm_content, "mood_tag": "🎧 音乐时刻"},
                {"friend_id": lena["id"], "content": "Too short.", "mood_tag": ""},
            ]
        }
    )

    created = await generate_daily(db, llm, [marco, lena], feed_date=FEED_DATE, now=NOW)

    assert len(llm.calls) == 1
    assert len(created) == 2
    marco_moment = next(moment for moment in created if moment["friend_id"] == marco["id"])
    lena_moment = next(moment for moment in created if moment["friend_id"] == lena["id"])
    assert marco_moment["content"] == llm_content
    assert marco_moment["mood_tag"] == "🎧 音乐时刻"
    # 不合规的那条走确定性兜底
    assert _valid_words(lena_moment["content"])
    assert lena_moment["mood_tag"]


async def test_llm_garbage_payload_falls_back_for_everyone(db):
    friend = make_friend(db)
    llm = FakeJSONLLM("not json at all")
    created = await generate_daily(db, llm, [friend], feed_date=FEED_DATE, now=NOW)
    assert len(created) == 1
    assert _valid_words(created[0]["content"])


# --------------------------------------------------------------------------- #
# moment_for_prompt
# --------------------------------------------------------------------------- #
def test_moment_for_prompt_contains_fields(db):
    friend = make_friend(db)
    moment = db.add_moment(
        friend["id"],
        "Morning coffee and a very slow start, honestly.",
        mood_tag="☕ 晨间咖啡",
        local_time_str="9:00 AM · Rome",
        feed_date=FEED_DATE,
    )
    text = moment_for_prompt(moment)
    assert "Morning coffee" in text
    assert "9:00 AM · Rome" in text
    assert "☕ 晨间咖啡" in text


def test_moment_for_prompt_handles_empty():
    assert moment_for_prompt({}) == ""
    assert moment_for_prompt(None) == ""  # type: ignore[arg-type]
