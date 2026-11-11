"""v2 记忆引擎离线单测：mock LLM + 临时 SQLite，全程不联网。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from app.v2 import social
from app.v2.db import DB
from app.v2.memory import FACT_SHEET_CHAR_BUDGET, MAX_RECENT_EVENTS, MemoryManager

FIXED_NOW = datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc)  # Rome = 09:00 周二
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}: ")


class MockLLM:
    """无 key 环境：``uses_mock=True``，complete_json_safe 直接走 fallback。"""

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


class BrokenLLM:
    uses_mock = False

    async def complete_json(self, *args, **kwargs):
        raise RuntimeError("boom")


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


def add_turn(db: DB, fid: str, user_text: str, friend_text: str = "Tell me more, yeah?") -> None:
    db.add_message(fid, "user", user_text)
    db.add_message(fid, "friend", friend_text)


# --------------------------------------------------------------------------- #
# build_context
# --------------------------------------------------------------------------- #
def test_build_context_injects_time_facts_episodes_reviews(db):
    db.update_profile(
        fact_sheet={
            "profile": {
                "nickname": "Alex",
                "city": "Shanghai",
                "major_or_job": "software engineering student",
                "routine": "night owl",
            },
            "interests": ["hiking", "indie music"],
            "language_profile": {"target": "en", "weaknesses": ["past tense"]},
            "recent_events": ["2026-11-01: moved to a new flat"],
        }
    )
    friend = make_friend(
        db,
        total_turns=35,
        shared_episodes=["2026-11-02: 聊了面试的事", "2026-11-03: 聊了周末计划"],
        open_loops=[
            {"event": "周四下午有场技术面试", "follow_up_after": "2099-01-01T18:00:00+08:00", "status": "pending"}
        ],
    )
    manager = MemoryManager(db, MockLLM())
    context = manager.build_context(
        friend,
        review_expressions=[{"better_expression": "I'm down for that", "explanation": "表示同意"}],
        now=FIXED_NOW,
    )

    # 好友当地时间 / 状态（Rome 11 月 = UTC+1 → 09:00 周二 morning）
    assert "Your local time is 09:00" in context
    assert "周二" in context
    assert "morning" in context
    # 熟稔度语气
    assert "Closeness level 3/5" in context
    assert social.closeness_style(3) in context
    # UserFactSheet
    assert "Alex" in context and "Shanghai" in context
    assert "hiking" in context and "past tense" in context
    # SharedEpisodes
    assert "2026-11-02: 聊了面试的事" in context
    # OpenLoops（未到期也要列出）
    assert "周四下午有场技术面试" in context
    # 待复习表达
    assert "I'm down for that" in context
    # 硬约束：人设/事实约束保留；「每轮 1-2 句 + 以问题结尾」已移除（改由行为指令决定）
    # 多用户/合规升级：不再要求「绝不承认是 AI」，改为要求「被问到时诚实说明是 AI 伙伴」
    assert "AI companion, not a real human" in context
    assert "[This turn]" in context
    assert "end with a hook" not in context
    assert "1-2 short spoken English sentences" not in context
    assert len(context) < 5000


def test_build_context_auto_picks_review_expressions(db):
    db.add_expression("I very like it", "I'm really into it", "very 不修饰动词")
    friend = make_friend(db)
    context = MemoryManager(db, MockLLM()).build_context(friend, now=FIXED_NOW)
    assert "I'm really into it" in context


def test_build_context_handles_sparse_friend(db):
    friend = db.create_friend({"name": "Mia"})
    context = MemoryManager(db, MockLLM()).build_context(friend, now=FIXED_NOW)
    assert "Mia" in context
    assert "None" not in context
    assert "Closeness level 1/5" in context
    assert "AI companion, not a real human" in context


def test_build_context_lists_only_pending_open_loops(db):
    friend = make_friend(
        db,
        open_loops=[
            {"event": "已结清的旧事", "follow_up_after": "2026-10-01T10:00:00+08:00", "status": "resolved"},
            {"event": "到期的技术面试", "follow_up_after": "2026-11-03T10:00:00+08:00", "status": "pending"},
            {"event": "未来的考试", "follow_up_after": "2099-01-01T10:00:00+08:00", "status": "pending"},
        ],
    )
    context = MemoryManager(db, MockLLM()).build_context(friend, now=FIXED_NOW)
    assert "到期的技术面试" in context
    assert "DUE NOW" in context
    assert "未来的考试" in context
    assert "已结清的旧事" not in context


# --------------------------------------------------------------------------- #
# after_turn（mock 兜底）
# --------------------------------------------------------------------------- #
async def test_after_turn_mock_extracts_loop_and_facts(db):
    friend = make_friend(db)
    add_turn(db, friend["id"], "I have a tech interview on Thursday afternoon, I'm so nervous")
    add_turn(db, friend["id"], "I love playing guitar and I live in Shanghai")

    manager = MemoryManager(db, MockLLM())
    result = await manager.after_turn(db.get_friend(friend["id"]))

    assert result["updated"] is True
    persisted = db.get_friend(friend["id"])
    loops = persisted["open_loops"]
    assert loops, "mock 兜底必须能提取 open loop"
    assert any("面试" in loop["event"] for loop in loops)
    for loop in loops:
        assert loop["status"] == "pending"
        match = ISO_RE.match(loop["follow_up_after"])
        assert match, loop["follow_up_after"]
        due = datetime.fromisoformat(loop["follow_up_after"])
        assert due.tzinfo is not None
        assert due > datetime.now(timezone.utc)

    episodes = persisted["shared_episodes"]
    assert episodes
    assert all(DATE_PREFIX_RE.match(episode) for episode in episodes)

    fact = db.get_profile()["fact_sheet"]
    assert "Shanghai" in fact["profile"]["city"]
    assert any("guitar" in item for item in fact["interests"])
    assert fact["recent_events"]
    assert result["closeness_level"] == social.closeness_level(db.count_turns(friend["id"]))


async def test_after_turn_preserves_existing_memory(db):
    db.update_profile(
        fact_sheet={
            "profile": {"nickname": "Alex", "city": "Shanghai", "major_or_job": "student", "routine": "night owl"},
            "interests": ["hiking"],
            "language_profile": {"target": "en", "weaknesses": ["past tense"]},
            "recent_events": ["2026-11-01: moved to a new flat"],
        }
    )
    friend = make_friend(
        db,
        shared_episodes=["2026-10-01: 聊了搬家的事"],
        open_loops=[
            {"event": "旧的已了事", "follow_up_after": "2026-10-01T10:00:00+08:00", "status": "resolved"},
            {"event": "旧的待办事", "follow_up_after": "2099-01-01T10:00:00+08:00", "status": "pending"},
        ],
    )
    add_turn(db, friend["id"], "I have an exam next week and I'm studying Python")

    result = await MemoryManager(db, MockLLM()).after_turn(db.get_friend(friend["id"]))
    assert result["updated"] is True

    persisted = db.get_friend(friend["id"])
    assert "2026-10-01: 聊了搬家的事" in persisted["shared_episodes"]
    statuses = {loop["event"]: loop["status"] for loop in persisted["open_loops"]}
    assert statuses["旧的已了事"] == "resolved"  # 已结清的不被破坏
    assert statuses["旧的待办事"] == "pending"
    assert any("考试" in event for event in statuses)

    fact = db.get_profile()["fact_sheet"]
    assert fact["profile"]["nickname"] == "Alex"
    assert "hiking" in fact["interests"]
    assert "past tense" in fact["language_profile"]["weaknesses"]


async def test_after_turn_is_idempotent(db):
    friend = make_friend(db)
    add_turn(db, friend["id"], "I have a tech interview tomorrow")
    manager = MemoryManager(db, MockLLM())

    await manager.after_turn(db.get_friend(friend["id"]))
    first = db.get_friend(friend["id"])
    episodes_before = len(first["shared_episodes"])
    loops_before = len(first["open_loops"])

    await manager.after_turn(db.get_friend(friend["id"]))
    second = db.get_friend(friend["id"])
    assert len(second["shared_episodes"]) == episodes_before
    assert len(second["open_loops"]) == loops_before


async def test_after_turn_with_no_messages_is_safe(db):
    friend = make_friend(db, shared_episodes=["2026-10-01: 聊了搬家的事"])
    result = await MemoryManager(db, MockLLM()).after_turn(friend)
    assert result["updated"] is False
    assert result["shared_episodes"] == ["2026-10-01: 聊了搬家的事"]


async def test_after_turn_never_raises(db):
    friend = make_friend(db)
    add_turn(db, friend["id"], "I have a tech interview next Friday")
    manager = MemoryManager(db, BrokenLLM())

    result = await manager.after_turn(db.get_friend(friend["id"]))
    assert result["updated"] is True
    assert db.get_friend(friend["id"])["open_loops"]

    # 不存在的 friend 也不能抛
    missing = await manager.after_turn({"id": "does-not-exist", "shared_episodes": []})
    assert missing["updated"] is False


async def test_after_turn_llm_path_merges(db):
    friend = make_friend(db)
    add_turn(db, friend["id"], "I moved to Berlin and I'm into techno now")
    llm = FakeJSONLLM(
        {
            "fact_sheet": {
                "profile": {"city": "Berlin"},
                "interests": ["techno"],
                "language_profile": {"target": "en", "weaknesses": ["articles"]},
                "recent_events": ["2026-11-03: went to a techno party"],
            },
            "shared_episodes": ["2026-11-03: 聊了柏林和 techno"],
            "open_loops": [
                {"event": "周五有个面试", "follow_up_after": "2026-11-06T18:00:00+08:00", "status": "pending"}
            ],
        }
    )
    manager = MemoryManager(db, llm)
    result = await manager.after_turn(db.get_friend(friend["id"]))

    assert len(llm.calls) == 1
    assert result["updated"] is True
    fact = db.get_profile()["fact_sheet"]
    assert fact["profile"]["city"] == "Berlin"
    assert "techno" in fact["interests"]
    assert "articles" in fact["language_profile"]["weaknesses"]

    persisted = db.get_friend(friend["id"])
    assert any("techno" in episode for episode in persisted["shared_episodes"])
    assert any(loop["event"] == "周五有个面试" for loop in persisted["open_loops"])


async def test_after_turn_keeps_fact_sheet_small(db):
    huge = {
        "profile": {"nickname": "Alex"},
        "interests": [f"interest-{i}" for i in range(40)],
        "language_profile": {"target": "en", "weaknesses": [f"weak-{i}" for i in range(20)]},
        "recent_events": [f"2026-10-{i:02d}: event {i}" for i in range(1, 29)],
    }
    db.update_profile(fact_sheet=huge)
    friend = make_friend(db)
    add_turn(db, friend["id"], "I have an exam next week")

    await MemoryManager(db, MockLLM()).after_turn(db.get_friend(friend["id"]))
    fact = db.get_profile()["fact_sheet"]
    assert len(json.dumps(fact, ensure_ascii=False)) <= FACT_SHEET_CHAR_BUDGET
    assert len(fact["recent_events"]) <= MAX_RECENT_EVENTS


# --------------------------------------------------------------------------- #
# open loop 生命周期
# --------------------------------------------------------------------------- #
def test_due_open_loops_filters_and_indexes(db):
    friend = make_friend(
        db,
        open_loops=[
            {"event": "already done", "follow_up_after": "2026-11-01T10:00:00+08:00", "status": "resolved"},
            {"event": "due interview", "follow_up_after": "2026-11-03T10:00:00+08:00", "status": "pending"},
            {"event": "future exam", "follow_up_after": "2099-01-01T10:00:00+08:00", "status": "pending"},
            {"event": "due trip", "follow_up_after": "2026-11-03T11:00:00+08:00", "status": "pending"},
            {"event": "no time", "status": "pending"},
        ],
    )
    due = MemoryManager(db, MockLLM()).due_open_loops(friend, now=FIXED_NOW)
    assert [loop["event"] for loop in due] == ["due interview", "due trip"]
    assert [loop["index"] for loop in due] == [1, 3]
    assert all(loop["status"] == "pending" for loop in due)


def test_due_open_loops_handles_sparse_friend(db):
    manager = MemoryManager(db, MockLLM())
    assert manager.due_open_loops({"id": "x"}) == []
    assert manager.due_open_loops({"open_loops": "not-a-list"}) == []
    assert manager.due_open_loops(None) == []  # type: ignore[arg-type]


def test_mark_open_loop_done_persists(db):
    friend = make_friend(
        db,
        open_loops=[
            {"event": "a", "follow_up_after": "2099-01-01T10:00:00+08:00", "status": "pending"},
            {"event": "b", "follow_up_after": "2099-01-02T10:00:00+08:00", "status": "pending"},
        ],
    )
    manager = MemoryManager(db, MockLLM())
    manager.mark_open_loop_done(friend["id"], 1)

    loops = db.get_friend(friend["id"])["open_loops"]
    assert loops[0]["status"] == "pending"
    assert loops[1]["status"] == "resolved"

    # 重复结清 / 越界 / 未知好友都不能抛
    manager.mark_open_loop_done(friend["id"], 1)
    manager.mark_open_loop_done(friend["id"], 99)
    manager.mark_open_loop_done(friend["id"], -1)
    manager.mark_open_loop_done("nope", 0)
    assert db.get_friend(friend["id"])["open_loops"][1]["status"] == "resolved"


# --------------------------------------------------------------------------- #
# followup_message
# --------------------------------------------------------------------------- #
async def test_followup_message_mock_is_english_and_deterministic(db):
    friend = make_friend(db)
    loop = {
        "event": "周四下午有场技术面试",
        "follow_up_after": "2026-11-05T18:00:00+08:00",
        "status": "pending",
    }
    manager = MemoryManager(db, MockLLM())
    message = await manager.followup_message(friend, loop)

    assert message
    assert "interview" in message.lower()
    assert not re.search(r"[\u4e00-\u9fff]", message)
    assert len(message.split()) <= 30
    assert message == await manager.followup_message(friend, loop)


async def test_followup_message_llm_path(db):
    friend = make_friend(db)
    llm = FakeJSONLLM({"message": "Hey! How did the interview go?"})
    manager = MemoryManager(db, llm)
    message = await manager.followup_message(friend, {"event": "技术面试", "status": "pending"})
    assert message == "Hey! How did the interview go?"
    assert len(llm.calls) == 1


async def test_followup_message_failure_uses_fallback(db):
    friend = make_friend(db)
    manager = MemoryManager(db, BrokenLLM())
    message = await manager.followup_message(friend, {"event": "技术面试", "status": "pending"})
    assert "interview" in message.lower()

# --------------------------------------------------------------------------- #
# Lead 集成测试反馈：兜底提取覆盖 + 多轮合并 + interview loop 全链路
# --------------------------------------------------------------------------- #
async def test_after_turn_mock_extracts_interests_from_messy_phrases(db):
    """I very like / My hobby is / I'm really into + 结尾省略号 都要能抽到兴趣。"""
    friend = make_friend(db)
    db.add_message(
        friend["id"],
        "user",
        "I very like this coffee, it is too much good. My hobby is play indie games.",
    )
    db.add_message(friend["id"], "friend", "Ha, noted.")
    db.add_message(
        friend["id"],
        "user",
        "Honestly, I'm really into this coffee, it's actually amazing.",
    )

    await MemoryManager(db, MockLLM()).after_turn(db.get_friend(friend["id"]))

    interests = db.get_profile()["fact_sheet"]["interests"]
    joined = " | ".join(interests).lower()
    assert "coffee" in joined
    assert "indie games" in joined


async def test_after_turn_ellipsis_interest_is_cleaned(db):
    friend = make_friend(db)
    db.add_message(friend["id"], "user", "I'm into indie games…")
    await MemoryManager(db, MockLLM()).after_turn(db.get_friend(friend["id"]))
    interests = db.get_profile()["fact_sheet"]["interests"]
    assert "indie games" in [item.lower() for item in interests]


async def test_after_turn_keeps_interests_across_rounds(db):
    """第二轮没有新兴趣（只聊天气）时，第一轮的兴趣不能被清空。"""
    friend = make_friend(db)
    manager = MemoryManager(db, MockLLM())

    db.add_message(friend["id"], "user", "My hobby is play indie games.")
    await manager.after_turn(db.get_friend(friend["id"]))
    first = db.get_profile()["fact_sheet"]["interests"]
    assert any("indie games" in item.lower() for item in first)

    db.add_message(friend["id"], "user", "The weather is so nice today, honestly.")
    db.add_message(friend["id"], "friend", "Rain tomorrow though.")
    await manager.after_turn(db.get_friend(friend["id"]))

    final = db.get_profile()["fact_sheet"]["interests"]
    assert any("indie games" in item.lower() for item in final), final


async def test_after_turn_mock_creates_interview_open_loop_and_lifecycle(db):
    """job interview on Friday at 3pm → 周五下午的 pending loop，且 due/mark/followup 可用。"""
    friend = make_friend(db)
    db.add_message(friend["id"], "user", "I have a job interview on Friday at 3pm")
    manager = MemoryManager(db, MockLLM())
    await manager.after_turn(db.get_friend(friend["id"]))

    persisted = db.get_friend(friend["id"])
    loops = persisted["open_loops"]
    assert loops
    loop = loops[0]
    assert loop["status"] == "pending"
    assert "面试" in loop["event"]
    assert "周五" in loop["event"]

    due_at = datetime.fromisoformat(loop["follow_up_after"])
    assert due_at.tzinfo is not None
    local = due_at.astimezone(timezone(timedelta(hours=8)))
    assert local.weekday() == 4  # Friday
    assert local.hour >= 15  # interview 15:00 之后

    due = manager.due_open_loops(persisted, now=due_at + timedelta(minutes=1))
    assert any(item["event"] == loop["event"] for item in due)
    assert due[0]["index"] == 0

    message = await manager.followup_message(persisted, loop)
    assert "interview" in message.lower()

    manager.mark_open_loop_done(friend["id"], 0)
    assert db.get_friend(friend["id"])["open_loops"][0]["status"] == "resolved"
