"""v2 朋友圈生成离线单测：mock LLM + 临时 SQLite，全程不联网。"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.v2.db import DB
from app.v2.moments import generate_daily, moment_for_prompt

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


async def test_unavailable_model_does_not_fabricate_daily_posts(db):
    friend = make_friend(db)
    llm = MockLLM()
    assert await generate_daily(db, llm, [friend], feed_date=FEED_DATE, now=NOW) == []
    assert db.list_moments() == []
    assert llm.calls == 0


async def test_generate_daily_is_idempotent_and_keeps_native_language(db):
    friend = make_friend(db)
    llm = FakeJSONLLM({"moments": [{"friend_id": friend["id"], "content": "Preferisco il caffè amaro."}]})
    first = await generate_daily(db, llm, [friend], feed_date=FEED_DATE, now=NOW)
    second = await generate_daily(db, llm, [friend], feed_date=FEED_DATE, now=NOW)
    assert len(first) == 1
    assert first == second
    assert len(llm.calls) == 1
    assert first[0]["content"] == "Preferisco il caffè amaro."
    assert first[0]["mood_tag"] == ""


async def test_unknown_inactive_and_wrong_language_authors_are_skipped(db):
    active = make_friend(db, native_lang="zh")
    pending = make_friend(db, status="pending_request")
    llm = FakeJSONLLM({"moments": [
        {"friend_id": active["id"], "content": "This is clearly an English sentence."},
        {"friend_id": pending["id"], "content": "Preferisco il caffè amaro."},
        {"friend_id": "missing", "content": "hello"},
    ]})
    assert await generate_daily(db, llm, [active, pending], feed_date=FEED_DATE, now=NOW) == []


async def test_short_post_is_not_padded_and_limit_is_a_ceiling(db):
    friend = make_friend(db, native_lang="zh")
    llm = FakeJSONLLM({"moments": [
        {"friend_id": friend["id"], "content": "不想加班"},
        {"friend_id": friend["id"], "content": "不想加班"},
        {"friend_id": friend["id"], "content": "周末想安静待着。"},
        {"friend_id": friend["id"], "content": "再说吧"},
    ]})
    rows = await generate_daily(db, llm, [friend], feed_date=FEED_DATE, now=NOW, per_friend_max=2)
    assert [row["content"] for row in rows] == ["不想加班", "周末想安静待着。"]


async def test_private_episodes_are_not_used_as_public_material(db):
    friend = make_friend(db, shared_episodes=["PRIVATE_SECRET: user's medical appointment"])
    llm = FakeJSONLLM({"moments": []})
    assert await generate_daily(db, llm, [friend], feed_date=FEED_DATE, now=NOW) == []
    assert "PRIVATE_SECRET" not in json.dumps(llm.calls)


async def test_invalid_generation_does_not_fill_quota(db):
    friend = make_friend(db)
    assert await generate_daily(db, FakeJSONLLM("not json"), [friend], feed_date=FEED_DATE, now=NOW) == []
    llm = FakeJSONLLM({"moments": [{"friend_id": friend["id"], "content": "x" * 401}]})
    assert await generate_daily(db, llm, [friend], feed_date=FEED_DATE, now=NOW) == []
    assert db.list_moments() == []


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
