"""多用户一期 · 私聊情绪按「用户 × 人物」隔离。

验收点
------
- A 与人物私聊后，**公共基线** ``personas.emotion_state`` 保持不变；
- A 的私聊情绪写进 ``persona_user_emotion(user_id, persona_id)`` 并随时间推进；
- B 读到的是自己的关系情绪 / 公共基线，不受 A 的私聊影响；
- 第二轮私聊从「自己上一轮的关系情绪」续上，而不是回到公共基线；
- 世界事件（``life_events``）仍然只推进公共基线，且不碰任何人的关系情绪；
- ``ensure_privacy_schema`` 幂等，缺表时惰性自愈，``current_user_id()`` 在
  ``asyncio.to_thread`` 里依旧可用（contextvars 会被复制）。

说明：全部使用 tmp_path 下的独立 SQLite + mock，无网络、无 LLM、不依赖真实时钟
（聊天计划固定 ``now=NOW``，只有写回时间戳用真实时钟，且不影响本文件的断言）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.v2.db import DB  # noqa: E402
from app.v3 import emotion as em  # noqa: E402
from app.v3 import life_events as le  # noqa: E402
from app.v3 import persona_context  # noqa: E402
from app.v3 import privacy  # noqa: E402
from app.v3.identity import LOCAL_USER_ID, reset_current_user, set_current_user  # noqa: E402
from app.v3.schema import ensure_schema  # noqa: E402

#: 固定"现在"：聊天计划的衰减计算用它，测试不依赖真实时钟（2026-11-05 是周四）
NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)
DAY = "2026-11-05"

ALICE = "u_alice"
BOB = "u_bob"

PERSONA: dict = {
    "id": "p_adrian",
    "nickname": "Adrian",
    "occupation": "Product designer",
    "language": "en",
    "country": "United Kingdom",
    "timezone": "Europe/London",
    "ai_config": {"temperament": "witty", "energy": 0.55},
}

#: 公共基线（世界事件写进 personas.emotion_state 的那一层）
PUBLIC_BASELINE: dict = {
    "mood": "content",
    "energy": 0.58,
    "stress": 0.30,
    "social_battery": 0.60,
    "emotional_context": "",
    "updated_at": "2026-11-05T09:00:00+00:00",
}


# --------------------------------------------------------------------------- #
# 夹具 / 工具
# --------------------------------------------------------------------------- #
def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _insert_persona(database: DB, spec: dict, *, emotion_state: dict | None = None) -> None:
    with database._lock, database.conn:  # noqa: SLF001
        database.conn.execute(
            "INSERT INTO personas (id, nickname, bio, personality, interests, language,"
            " country, city, timezone, occupation, age, gender, ai_config, status,"
            " created_at, lifecycle, emotion_state)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                spec["id"], spec["nickname"], spec.get("bio", ""), spec.get("personality", ""),
                json.dumps(spec.get("interests") or [], ensure_ascii=False),
                spec.get("language", "en"), spec.get("country", ""), spec.get("city", ""),
                spec.get("timezone", "UTC"), spec.get("occupation", ""), spec.get("age", 28),
                spec.get("gender", "female"),
                json.dumps(spec.get("ai_config") or {}, ensure_ascii=False),
                "active", _iso(NOW), "permanent",
                json.dumps(emotion_state if emotion_state is not None else {}, ensure_ascii=False),
            ),
        )


@pytest.fixture()
def db(tmp_path):
    database = DB(str(tmp_path / "privacy.db"))
    database.init()
    ensure_schema(database, seed=False)
    _insert_persona(database, PERSONA, emotion_state=PUBLIC_BASELINE)
    privacy.ensure_privacy_schema(database)
    yield database
    database.close()


def _friend(persona: dict | None = None) -> dict:
    """最小好友行：build_turn_plan 只依赖 persona_id / 语言 / 熟稔度这些字段。"""
    spec = persona or PERSONA
    return {
        "id": f"friend_{spec['id']}",
        "name": spec["nickname"],
        "persona_id": spec["id"],
        "reply_language": spec.get("language", "en"),
        "native_lang": spec.get("language", "en"),
        "personality_prompt": spec.get("personality", ""),
        "closeness_level": 3,
        "total_turns": 5,
    }


def _public_raw(database: DB, persona_id: str) -> str:
    """公共基线的**原始文本**（直接断言"没被写过"，不经过 normalize）。"""
    with database._lock:  # noqa: SLF001
        row = database.conn.execute(
            "SELECT emotion_state FROM personas WHERE id=?", (persona_id,)
        ).fetchone()
    assert row is not None
    return row["emotion_state"]


def _expected_public_baseline() -> dict:
    """本轮没有关系情绪时，计划里应有的情绪 = 公共基线经 decay（与实现同一条公式）。"""
    state = em.normalize_state(PUBLIC_BASELINE)
    hours = (NOW - datetime(2026, 11, 5, 9, 0, 0, tzinfo=timezone.utc)).total_seconds() / 3600.0
    return em.decay(state, hours=hours)


def _run_turn(
    database: DB,
    friend: dict,
    text: str,
    user_id: str,
    *,
    intent: str = "bad_news",
):
    """模拟一次真实私聊回合：请求上下文里绑定用户 → 计划 → 写回。"""
    token = set_current_user(user_id)
    try:
        plan = persona_context.build_turn_plan(database, friend, text, now=NOW)
        state = persona_context.update_emotion_after_turn(
            database, plan, intent=intent, behavior_value=plan.behavior
        )
        return plan, state
    finally:
        reset_current_user(token)


# --------------------------------------------------------------------------- #
# 1) 私聊不动公共基线，只写自己的关系情绪
# --------------------------------------------------------------------------- #
def test_private_chat_never_touches_public_emotion(db):
    friend = _friend()
    public_before = _public_raw(db, PERSONA["id"])

    plan, state = _run_turn(db, friend, "I had a terrible day", ALICE)

    # 第一轮没有关系情绪 → 回退公共基线（经 decay），默认行为保持合理
    assert plan.user_id == ALICE
    assert plan.emotion == _expected_public_baseline()

    # 公共基线一个字节都没变
    assert _public_raw(db, PERSONA["id"]) == public_before

    # A 的关系情绪被写入，且是"从本轮读到的情绪继续走一小步"
    relation = privacy.get_user_persona_emotion(db, ALICE, PERSONA["id"])
    assert relation
    assert relation["updated_at"] == state["updated_at"]
    assert relation["stress"] > plan.emotion["stress"]  # bad_news 增压
    assert relation["emotional_context"]  # 值得记一笔的会话留下余韵
    for key in em.BASELINE:
        assert 0.0 <= float(relation[key]) <= 1.0


def test_local_user_mode_still_writes_relation_only(db):
    """不带身份头（本地单用户）时行为与多用户一致：仍只写关系表。"""
    friend = _friend()
    public_before = _public_raw(db, PERSONA["id"])

    plan, _ = _run_turn(db, friend, "hey, how are you?", LOCAL_USER_ID, intent="greeting")

    assert plan.user_id == LOCAL_USER_ID
    assert privacy.get_user_persona_emotion(db, LOCAL_USER_ID, PERSONA["id"])
    assert _public_raw(db, PERSONA["id"]) == public_before


# --------------------------------------------------------------------------- #
# 2) A 与 B 互不影响
# --------------------------------------------------------------------------- #
def test_users_emotions_are_isolated(db):
    friend = _friend()
    public_before = _public_raw(db, PERSONA["id"])

    # A 聊一轮 → 只有 A 的关系情绪出现
    _run_turn(db, friend, "I had a terrible day", ALICE)
    alice_state = privacy.get_user_persona_emotion(db, ALICE, PERSONA["id"])
    assert alice_state
    assert privacy.get_user_persona_emotion(db, BOB, PERSONA["id"]) == {}

    # B 第一次聊天：读到的是公共基线（不是 A 的情绪）
    token = set_current_user(BOB)
    try:
        bob_plan = persona_context.build_turn_plan(db, friend, "hi there", now=NOW)
    finally:
        reset_current_user(token)
    assert bob_plan.user_id == BOB
    assert bob_plan.emotion == _expected_public_baseline()
    assert bob_plan.emotion["energy"] != alice_state["energy"] or (
        bob_plan.emotion["stress"] != alice_state["stress"]
    )

    # B 也聊一轮（不同情绪走向）：两条关系情绪彼此独立，公共基线仍然没变
    _run_turn(db, friend, "I just got promoted!", BOB, intent="good_news")
    bob_state = privacy.get_user_persona_emotion(db, BOB, PERSONA["id"])
    assert bob_state
    assert bob_state != alice_state
    assert bob_state["energy"] > _expected_public_baseline()["energy"]  # good_news 提神
    assert privacy.get_user_persona_emotion(db, ALICE, PERSONA["id"]) == alice_state
    assert _public_raw(db, PERSONA["id"]) == public_before
    # 两条独立的行，而不是共用一个状态
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT user_id FROM persona_user_emotion WHERE persona_id=?", (PERSONA["id"],)
        ).fetchall()
    assert sorted(row["user_id"] for row in rows) == [ALICE, BOB]


def test_second_turn_continues_from_own_relation_emotion(db):
    friend = _friend()
    public_before = _public_raw(db, PERSONA["id"])

    _run_turn(db, friend, "I had a terrible day", ALICE)
    first = privacy.get_user_persona_emotion(db, ALICE, PERSONA["id"])

    token = set_current_user(ALICE)
    try:
        plan = persona_context.build_turn_plan(db, friend, "still thinking about it", now=NOW)
    finally:
        reset_current_user(token)

    # 第二轮从自己的关系情绪续上（刚写回，衰减为 0），而不是回到公共基线
    assert plan.emotion["energy"] == pytest.approx(float(first["energy"]))
    assert plan.emotion["stress"] == pytest.approx(float(first["stress"]))
    assert plan.emotion["emotional_context"] == first["emotional_context"]
    assert plan.emotion != _expected_public_baseline()
    assert _public_raw(db, PERSONA["id"]) == public_before


# --------------------------------------------------------------------------- #
# 3) 世界事件仍然只推进公共基线
# --------------------------------------------------------------------------- #
def _event(**overrides) -> dict:
    base = {
        "persona_id": PERSONA["id"],
        "event_type": "achievement",
        "title": "Finished the mural commission",
        "description": "",
        "start_time": _iso(NOW - timedelta(hours=3)),
        "end_time": _iso(NOW - timedelta(hours=2)),
        "participants": [],
        "location": "",
        "related_personas": [],
        "event_status": "planned",
        "source_type": "rule",
        "source_reference": "test",
        "visibility": "private",
        "metadata": {"timezone": "UTC"},
        "dedupe_key": "test:emotion-privacy:achievement",
        "day": DAY,
    }
    base.update(overrides)
    return base


def test_world_event_still_updates_public_emotion_only(db):
    friend = _friend()
    # A 先聊一轮，产生自己的关系情绪
    _run_turn(db, friend, "how was your day?", ALICE)
    alice_before = privacy.get_user_persona_emotion(db, ALICE, PERSONA["id"])
    public_before = json.loads(_public_raw(db, PERSONA["id"]))

    le.add_event(db, _event(event_status="planned"))
    changed = le.refresh_statuses(db, now=NOW)
    assert changed >= 1

    # 公共基线被世界事件推进（升能量、降压力）
    public_after = json.loads(_public_raw(db, PERSONA["id"]))
    assert public_after != public_before
    assert public_after["energy"] > public_before["energy"]
    assert public_after["stress"] < public_before["stress"]
    for key in em.BASELINE:
        assert abs(float(public_after[key]) - float(public_before[key])) <= em.EVENT_DELTA_CAP + 1e-6

    # A 的关系情绪不被世界事件改写
    assert privacy.get_user_persona_emotion(db, ALICE, PERSONA["id"]) == alice_before

    # 没有关系情绪的用户（B）第二轮读到的是新的公共基线
    token = set_current_user(BOB)
    try:
        bob_plan = persona_context.build_turn_plan(db, friend, "hi again", now=NOW)
    finally:
        reset_current_user(token)
    expected = em.decay(em.normalize_state(public_after), hours=0)
    assert bob_plan.emotion["energy"] == pytest.approx(float(expected["energy"]))
    assert bob_plan.emotion["energy"] > _expected_public_baseline()["energy"]


# --------------------------------------------------------------------------- #
# 4) 建表幂等 / 缺表惰性自愈 / 显式 user_id 优先 / to_thread 身份
# --------------------------------------------------------------------------- #
def test_ensure_privacy_schema_is_idempotent(db):
    privacy.ensure_privacy_schema(db)
    privacy.ensure_privacy_schema(db)  # 再调一次不报错
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (privacy.TABLE,),
        ).fetchone()
    assert row is not None


def test_missing_table_self_heals_and_save_roundtrips(tmp_path):
    """没显式接线建表时，第一次读/写会惰性建表（老库/漏接线不阻断聊天）。"""
    database = DB(str(tmp_path / "lazy.db"))
    database.init()
    ensure_schema(database, seed=False)
    try:
        assert privacy.get_user_persona_emotion(database, ALICE, PERSONA["id"]) == {}
        state = em.default_state(PERSONA)
        privacy.save_user_persona_emotion(database, ALICE, PERSONA["id"], state)
        stored = privacy.get_user_persona_emotion(database, ALICE, PERSONA["id"])
        assert stored["mood"] == state["mood"]
        assert stored["updated_at"] == state["updated_at"]
        # 脏数据 / 未知用户一律返回空 dict，不抛异常
        assert privacy.get_user_persona_emotion(database, "", PERSONA["id"]) == {}
        assert privacy.get_user_persona_emotion(database, BOB, PERSONA["id"]) == {}
    finally:
        database.close()


def test_explicit_user_id_wins_over_contextvar(db):
    friend = _friend()
    token = set_current_user(BOB)
    try:
        plan = persona_context.build_turn_plan(
            db, friend, "hello", now=NOW, user_id=ALICE
        )
        persona_context.update_emotion_after_turn(db, plan, intent="greeting")
    finally:
        reset_current_user(token)

    assert plan.user_id == ALICE
    assert privacy.get_user_persona_emotion(db, ALICE, PERSONA["id"])
    assert privacy.get_user_persona_emotion(db, BOB, PERSONA["id"]) == {}


def test_to_thread_keeps_current_user(db):
    """asyncio.to_thread 会复制 contextvars：聊天线程里 current_user_id() 仍是请求用户。"""
    friend = _friend()

    async def scenario():
        token = set_current_user(ALICE)
        try:
            return await asyncio.to_thread(
                persona_context.build_turn_plan, db, friend, "hello", now=NOW
            )
        finally:
            reset_current_user(token)

    plan = asyncio.run(scenario())
    assert plan.user_id == ALICE
