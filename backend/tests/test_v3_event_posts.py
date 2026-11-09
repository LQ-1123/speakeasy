"""事件驱动社交内容（``app.v3.event_posts``）测试。

覆盖点
------
- ``share_probability``：随 self-disclosure 单调上升；daily_routine 显著低于 achievement；
- ``should_post``：固定 rng 可复现、24h 发帖过多时拒绝；
- ``generate_post_for_participant``：写入 source_event_id/post_type/metadata、**幂等**、
  LLM 不可用或失败 → 返回 None 且不写库、LLM 编造事实 → 拒绝写库；
- **核心验收**：1 个事件 + 2 个参与者 → 2 条动态，``source_event_id`` 相同、
  metadata 里事件标题一致、正文不同（不同视角）、正文不新增事件事实；
- ``maybe_persona_comment``：幂等、author_type='persona'、必须有关系、不评论自己；
- ``run_posts_cycle``：第二次运行新增 0 条；planned/cancelled 事件不发动态；
- ``persona_recent_facts``：不含 cancelled、planned 带 "not happened yet"、含自己的动态与评论。

说明
----
- 每个测试用 ``tmp_path`` 下独立 SQLite（``ensure_schema(db, seed=False)``），
  人物/事件/关系全部直接 INSERT 造数据，不依赖并行开发中的 life_events 模块。
- 不调真实 LLM：用 ``FakeLLM`` 返回固定 JSON；失败分支用抛异常的假 LLM。
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.v2.db import DB  # noqa: E402
from app.v3 import event_posts as ep  # noqa: E402
from app.v3.event_posts import (  # noqa: E402
    MAX_POST_CHARS,
    POST_BUDGET_MAX,
    POST_BUDGET_MIN,
    TENSION_MAX,
    TENSION_PENALTY_FLOOR,
    TENSION_PENALTY_STEP,
    comment_backfill,
    comment_probability,
    dna_traits_for,
    generate_event_posts,
    generate_post_for_participant,
    maybe_persona_comment,
    persona_post_budget,
    persona_recent_facts,
    post_content_violations,
    posts_for_event,
    run_posts_cycle,
    share_probability,
    should_post,
    value_tier,
)
from app.v3.schema import ensure_schema  # noqa: E402


# --------------------------------------------------------------------------- #
# 测试夹具
# --------------------------------------------------------------------------- #
def run(coro):
    """在同步测试里执行 async 函数。"""
    return asyncio.run(coro)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def make_db(tmp_path: Path) -> DB:
    db = DB(str(tmp_path / "event_posts.db"))
    db.init()
    ensure_schema(db, seed=False)
    return db


#: DNA：Emily 外向爱分享（open/expressive），Sophie 收着一点（measured/warm）
_EMILY_DNA = {
    "sentence_rhythm": "clipped",
    "directness": "direct",
    "humor_style": "dry",
    "emotional_expression": "expressive",
    "vocabulary_style": "plain",
    "slang_usage": "rare",
    "emoji_usage": "rare",
    "verbosity": "short",
    "conversation_initiative": "high",
    "self_disclosure": "open",
    "social_distance": "intimate",
    "disagreement_style": "state_plainly",
    "topic_transition": "stay",
}
_SOPHIE_DNA = {
    "sentence_rhythm": "measured",
    "directness": "diplomatic",
    "humor_style": "playful",
    "emotional_expression": "warm",
    "vocabulary_style": "precise",
    "slang_usage": "none",
    "emoji_usage": "sometimes",
    "verbosity": "medium",
    "conversation_initiative": "medium",
    "self_disclosure": "measured",
    "social_distance": "familiar",
    "disagreement_style": "soften",
    "topic_transition": "gentle_shift",
}


def add_persona(
    db: DB,
    persona_id: str,
    nickname: str,
    *,
    language: str = "en",
    city: str = "London",
    country: str = "United Kingdom",
    occupation: str = "Teacher",
    dna: dict | None = None,
    emotion: dict | None = None,
) -> None:
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO personas (id, nickname, language, city, country, occupation,"
            " personality, interests, speech_dna, emotion_state, bio)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                persona_id, nickname, language, city, country, occupation,
                f"{nickname} is a normal person with a life of their own.",
                json.dumps(["music", "coffee"], ensure_ascii=False),
                json.dumps(dna or {}, ensure_ascii=False),
                json.dumps(emotion or {}, ensure_ascii=False),
                "",
            ),
        )


def add_relationship(
    db: DB, a: str, b: str, *, closeness: int = 4, relation_type: str = "friend"
) -> None:
    now = _iso(_now())
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO persona_relationships (id, persona_a, persona_b, relation_type,"
            " closeness, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (f"rel_{a}_{b}", a, b, relation_type, closeness, now, now),
        )


def add_event(
    db: DB,
    event_id: str,
    *,
    persona_id: str = "p_emily",
    participants: list[str] | None = None,
    event_type: str = "entertainment",
    title: str = "Jazz at the Blue Note",
    description: str = "Ended up at a small jazz bar after dinner.",
    location: str = "The Blue Note, Soho",
    status: str = "completed",
    start_hours_ago: float = 26.0,
    duration_hours: float = 2.0,
    visibility: str = "private",
    day: str = "",
) -> dict:
    start = _now() - timedelta(hours=start_hours_ago)
    end = start + timedelta(hours=duration_hours)
    people = participants if participants is not None else [persona_id]
    row = {
        "id": event_id,
        "persona_id": persona_id,
        "event_type": event_type,
        "title": title,
        "description": description,
        "start_time": _iso(start),
        "end_time": _iso(end),
        "participants": people,
        "location": location,
        "related_personas": [],
        "event_status": status,
        "source_type": "rule",
        "source_reference": "",
        "visibility": visibility,
        "metadata": {},
        "dedupe_key": f"dedupe:{event_id}",
        "day": day or start.strftime("%Y-%m-%d"),
        "created_at": _iso(start),
        "updated_at": _iso(end),
        "completed_at": _iso(end) if status == "completed" else "",
    }
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO persona_events (id, persona_id, event_type, title, description,"
            " start_time, end_time, participants, location, related_personas, event_status,"
            " source_type, source_reference, visibility, metadata, dedupe_key, day,"
            " created_at, updated_at, completed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["id"], row["persona_id"], row["event_type"], row["title"],
                row["description"], row["start_time"], row["end_time"],
                json.dumps(row["participants"]), row["location"],
                json.dumps(row["related_personas"]), row["event_status"],
                row["source_type"], row["source_reference"], row["visibility"],
                json.dumps(row["metadata"]), row["dedupe_key"], row["day"],
                row["created_at"], row["updated_at"], row["completed_at"],
            ),
        )
    return row


def count(db: DB, sql: str, params: tuple = ()) -> int:
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(sql, params).fetchone()
    return int(row["n"]) if row else 0


def post_count(db: DB) -> int:
    return count(db, "SELECT COUNT(*) AS n FROM persona_posts")


def comment_count(db: DB) -> int:
    return count(db, "SELECT COUNT(*) AS n FROM persona_post_comments")


def add_event_post(
    db: DB,
    post_id: str,
    *,
    persona_id: str,
    event_id: str,
    content: str = "A small moment worth remembering.",
    hours_ago: float = 2.0,
) -> str:
    """直接造一条「老的事件动态」（模拟上一轮 cycle 已经产出的动态）。"""
    created = _iso(_now() - timedelta(hours=hours_ago))
    meta = json.dumps({"event_id": event_id, "event_title": "old event"}, ensure_ascii=False)
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO persona_posts (id, persona_id, content, media, tags, language,"
            " visibility, like_count, comment_count, created_at, expires_at, post_kind,"
            " alt_text, source_ref, source_event_id, post_type, published_at, metadata)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (post_id, persona_id, content, "[]", "[]", "en", "public", 0, 0,
             created, "", "life_text", "", "", event_id, "life_event", created, meta),
        )
    return post_id


class FakeLLM:
    """确定性假 LLM：按 system prompt 里的昵称返回不同文案。"""

    def __init__(self, replies: dict[str, str] | None = None, default: str = "A good night."):
        self.uses_mock = False
        self.replies = replies or {}
        self.default = default
        self.calls: list[list[dict]] = []

    async def complete_json(self, messages, *, schema_hint="", temperature=None, max_tokens=None):
        self.calls.append(list(messages))
        text = "\n".join(str(m.get("content") or "") for m in messages)
        for nickname, reply in self.replies.items():
            if f"You are {nickname}" in text:
                key = "comment" if schema_hint.startswith('{"comment"') else "content"
                return json.dumps({key: reply}, ensure_ascii=False)
        key = "comment" if schema_hint.startswith('{"comment"') else "content"
        return json.dumps({key: self.default}, ensure_ascii=False)


class FailingLLM:
    """模拟网络/鉴权失败的 LLM：complete_json 直接抛异常。"""

    uses_mock = False

    async def complete_json(self, *args, **kwargs):
        raise RuntimeError("llm down")


class MockModeLLM:
    """没有配置 API key 时的 mock 模式（uses_mock=True）。"""

    uses_mock = True


class AlwaysShareRNG(random.Random):
    """总是掷 0.0 的 rng：把「概率闸门」固定为通过，让用例专测写库/幂等。

    生产环境不会用它；它只是让「多视角动态」「批处理」这类用例**不依赖运气**，
    同时不改变被测代码的分支（仍然要走 ``rng.random() < prob``）。
    """

    def random(self) -> float:  # noqa: D102 - 覆写即可
        return 0.0


class SequenceRNG(random.Random):
    """按给定序列返回 ``random()`` 的 rng：用来精确控制「这条事件发不发」。

    例如 ``SequenceRNG([0.0, 0.99, 0.0])`` = 第 1 条发、第 2 条拒绝、 第 3 条发。
    """

    def __init__(self, values: list[float]):
        super().__init__(0)
        self._values = list(values)
        self._index = 0

    def random(self) -> float:
        if not self._values:
            return 0.0
        value = self._values[self._index % len(self._values)]
        self._index += 1
        return value


class FixedRNG(random.Random):
    """每次都返回同一个 roll：用来断言「某个概率阈值」两侧的判定。"""

    def __init__(self, value: float):
        super().__init__(0)
        self._value = value

    def random(self) -> float:
        return self._value


_EMILY_POST = "Didn't expect to enjoy jazz this much. Sophie knows some surprisingly good places."
_SOPHIE_POST = "Found a lovely little jazz bar tonight. Emily actually liked it, which is a miracle."


def two_participant_scene(tmp_path: Path):
    """Emily + Sophie 同去一个事件的完整夹具。"""
    db = make_db(tmp_path)
    add_persona(db, "p_emily", "Emily", dna=_EMILY_DNA)
    add_persona(db, "p_sophie", "Sophie", dna=_SOPHIE_DNA)
    add_relationship(db, "p_emily", "p_sophie", closeness=5)
    event = add_event(
        db, "ev_jazz", persona_id="p_emily",
        participants=["p_emily", "p_sophie"],
        title="Jazz at the Blue Note",
        description="Emily and Sophie went to a small jazz bar on Friday night.",
        location="The Blue Note, Soho",
    )
    return db, event


# --------------------------------------------------------------------------- #
# share_probability / should_post
# --------------------------------------------------------------------------- #
def test_share_probability_rises_with_disclosure():
    event = {"event_type": "social", "visibility": "public"}
    values = [
        share_probability({}, {"disclosure": d, "social_distance": 0.5, "warmth": 0.5}, event)
        for d in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
    ]
    assert all(later > earlier for earlier, later in zip(values, values[1:])), values
    assert all(0.0 <= v <= 1.0 for v in values)


def test_share_probability_daily_routine_much_lower_than_achievement():
    persona = {"id": "p_x"}
    traits = {"disclosure": 0.6, "social_distance": 0.6, "warmth": 0.6}
    daily = share_probability(persona, traits, {"event_type": "daily_routine",
                                                "visibility": "public"})
    meal = share_probability(persona, traits, {"event_type": "meal", "visibility": "public"})
    achievement = share_probability(persona, traits, {"event_type": "achievement",
                                                      "visibility": "public"})
    assert achievement > daily * 3, (daily, achievement)
    assert achievement > meal * 2


def test_share_probability_reads_dna_from_persona():
    persona = {"id": "p_x", "speech_dna": json.dumps(_EMILY_DNA)}
    event = {"event_type": "social", "visibility": "public"}
    traits = dna_traits_for(persona)
    assert 0.0 <= share_probability(persona, traits, event) <= 1.0
    # Emily = open + intimate，应当明显高于一个保守人设
    shy = {"id": "p_y", "speech_dna": json.dumps({
        "self_disclosure": "guarded", "social_distance": "distant",
        "emotional_expression": "reserved",
    })}
    assert share_probability(persona, traits, event) > share_probability(
        shy, dna_traits_for(shy), event
    )


def test_should_post_is_reproducible_with_seed():
    persona = {"id": "p_emily", "speech_dna": json.dumps(_EMILY_DNA)}
    traits = dna_traits_for(persona)
    event = {"event_type": "entertainment", "visibility": "private"}

    def sample() -> list[bool]:
        rng = random.Random(42)
        return [
            should_post(persona=persona, dna_traits=traits, event=event,
                        recent_post_count=0, rng=rng)
            for _ in range(40)
        ]

    first, second = sample(), sample()
    assert first == second, "同一 seed 必须给出完全相同的判定序列"
    assert any(first) and not all(first), "概率应当既不是 0 也不是 1"


def test_should_post_rejects_when_persona_budget_used_up():
    """24h 上限由人物预算决定（Emily = open + intimate → budget 8）。"""
    persona = {"id": "p_emily", "speech_dna": json.dumps(_EMILY_DNA)}
    traits = dna_traits_for(persona)
    budget = persona_post_budget(traits)
    assert budget == 8
    event = {"event_type": "achievement", "visibility": "public"}
    for count in (budget, budget + 4, budget + 40):
        for seed in range(30):
            assert not should_post(persona=persona, dna_traits=traits, event=event,
                                   recent_post_count=count, rng=random.Random(seed))

    # 同一条事件：预算内仍可能发（AlwaysShare 必定发）
    assert should_post(persona=persona, dna_traits=traits, event=event,
                       recent_post_count=budget - 1, rng=AlwaysShareRNG())


def test_should_post_soft_suppression_at_half_budget():
    """用掉一半预算后概率 ×0.45：同一 roll 在预算前段通过、后半段被拒。"""
    persona = {"id": "p_emily", "speech_dna": json.dumps(_EMILY_DNA)}
    traits = dna_traits_for(persona)
    event = {"event_type": "entertainment", "visibility": "private"}
    budget = persona_post_budget(traits)
    assert budget == 8
    # roll=0.30：未打折的概率 ~0.45 会通过；×0.45 后 ~0.20 会被拒
    assert should_post(persona=persona, dna_traits=traits, event=event,
                       recent_post_count=1, rng=FixedRNG(0.30)) is True
    assert should_post(persona=persona, dna_traits=traits, event=event,
                       recent_post_count=int(budget * 0.5), rng=FixedRNG(0.30)) is False
    # 到达预算 → 无论 roll 多小都硬拒
    assert should_post(persona=persona, dna_traits=traits, event=event,
                       recent_post_count=budget, rng=FixedRNG(0.0)) is False


def test_should_post_low_budget_persona_quieter():
    """guarded/distant 的人物预算明显更小：同样已发 3 条，ta 已经不发了。"""
    quiet = {"id": "p_quiet", "speech_dna": json.dumps({
        "self_disclosure": "guarded", "social_distance": "distant",
        "emotional_expression": "reserved", "verbosity": "one_line",
    })}
    quiet_traits = dna_traits_for(quiet)
    assert persona_post_budget(quiet_traits) <= 4
    loud = {"id": "p_emily", "speech_dna": json.dumps(_EMILY_DNA)}
    loud_traits = dna_traits_for(loud)
    assert persona_post_budget(quiet_traits) < persona_post_budget(loud_traits)

    event = {"event_type": "social", "visibility": "public"}
    assert not should_post(persona=quiet, dna_traits=quiet_traits, event=event,
                           recent_post_count=3, rng=FixedRNG(0.0))
    assert should_post(persona=loud, dna_traits=loud_traits, event=event,
                       recent_post_count=3, rng=FixedRNG(0.0))


def test_should_post_daily_routine_rarer_than_achievement():
    persona = {"id": "p_emily", "speech_dna": json.dumps(_EMILY_DNA)}
    traits = dna_traits_for(persona)
    rng = random.Random(7)
    daily = sum(
        should_post(persona=persona, dna_traits=traits,
                    event={"event_type": "daily_routine", "visibility": "private"},
                    recent_post_count=0, rng=rng)
        for _ in range(300)
    )
    achievement = sum(
        should_post(persona=persona, dna_traits=traits,
                    event={"event_type": "achievement", "visibility": "public"},
                    recent_post_count=0, rng=rng)
        for _ in range(300)
    )
    assert achievement > daily * 2, (daily, achievement)


# --------------------------------------------------------------------------- #
# 事实校验
# --------------------------------------------------------------------------- #
def test_post_content_violations_blocks_invented_facts():
    event = {"title": "Jazz at the Blue Note", "description": "A jazz bar.",
             "location": "The Blue Note, Soho", "participants": ["p_emily"],
             "event_type": "entertainment", "event_status": "completed"}
    persona = {"id": "p_emily", "city": "London", "country": "United Kingdom"}

    assert post_content_violations(_EMILY_POST, event, persona) == []
    assert "ai_mention" in post_content_violations(
        "As an AI, I liked the jazz bar.", event, persona
    )
    assert any(v.startswith("invented_place") for v in post_content_violations(
        "The jazz bar in Tokyo was amazing.", event, persona
    ))
    assert any(v.startswith("invented_date") for v in post_content_violations(
        "Best jazz night of 2019.", event, persona
    ))
    assert post_content_violations("", event, persona) == ["empty"]


# --------------------------------------------------------------------------- #
# generate_post_for_participant
# --------------------------------------------------------------------------- #
def test_generate_post_writes_event_facts(tmp_path):
    db, event = two_participant_scene(tmp_path)
    llm = FakeLLM({"Emily": _EMILY_POST})
    persona = {"id": "p_emily", **{"nickname": "Emily", "language": "en",
                                   "speech_dna": json.dumps(_EMILY_DNA)},
               "city": "London", "country": "United Kingdom", "occupation": "Teacher"}

    post = run(generate_post_for_participant(db, llm, event, persona))

    assert post is not None
    assert post["source_event_id"] == "ev_jazz"
    assert post["post_type"] == "life_event"
    assert post["persona_id"] == "p_emily"
    assert post["content"] == _EMILY_POST
    assert post["published_at"], "published_at 必须写入（信息流按它排序）"
    assert post["metadata"]["event_title"] == "Jazz at the Blue Note"
    assert post["metadata"]["event_type"] == "entertainment"
    assert post["metadata"]["event_id"] == "ev_jazz"
    assert post["language"] == "en"

    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT * FROM persona_posts WHERE source_event_id='ev_jazz'"
        ).fetchone()
    assert row is not None and row["post_kind"] == "life_text"
    assert post_count(db) == 1


def test_generate_post_is_idempotent(tmp_path):
    db, event = two_participant_scene(tmp_path)
    llm = FakeLLM({"Emily": _EMILY_POST})
    persona = {"id": "p_emily", "nickname": "Emily", "language": "en",
               "speech_dna": json.dumps(_EMILY_DNA)}

    first = run(generate_post_for_participant(db, llm, event, persona))
    second = run(generate_post_for_participant(db, llm, event, persona))

    assert first["id"] == second["id"]
    assert post_count(db) == 1, "同一 (event_id, persona_id) 第二次不得新增行"
    assert len(llm.calls) == 1, "已有动态时应直接返回，不再调用 LLM"


def test_generate_post_force_regenerates_in_place(tmp_path):
    db, event = two_participant_scene(tmp_path)
    persona = {"id": "p_emily", "nickname": "Emily", "language": "en",
               "speech_dna": json.dumps(_EMILY_DNA)}
    first = run(generate_post_for_participant(db, FakeLLM({"Emily": _EMILY_POST}),
                                             event, persona))
    second = run(generate_post_for_participant(
        db, FakeLLM({"Emily": "Jazz, again. Still better than expected."}),
        event, persona, force=True,
    ))
    assert second["id"] == first["id"], "force 只重写正文，不新增行"
    assert post_count(db) == 1
    assert second["content"] == "Jazz, again. Still better than expected."
    assert second["metadata"]["event_title"] == "Jazz at the Blue Note"


@pytest.mark.parametrize("llm", [None, MockModeLLM(), FailingLLM()])
def test_generate_post_returns_none_without_llm(tmp_path, llm):
    db, event = two_participant_scene(tmp_path)
    persona = {"id": "p_emily", "nickname": "Emily", "language": "en",
               "speech_dna": json.dumps(_EMILY_DNA)}
    assert run(generate_post_for_participant(db, llm, event, persona)) is None
    assert post_count(db) == 0, "LLM 不可用/失败时绝不写库（不编造）"


def test_generate_post_rejects_non_completed_event(tmp_path):
    db = make_db(tmp_path)
    add_persona(db, "p_emily", "Emily", dna=_EMILY_DNA)
    for status in ("planned", "ongoing", "cancelled"):
        event = add_event(db, f"ev_{status}", persona_id="p_emily", status=status)
        persona = {"id": "p_emily", "nickname": "Emily"}
        assert run(generate_post_for_participant(
            db, FakeLLM({"Emily": _EMILY_POST}), event, persona)) is None
    assert post_count(db) == 0


def test_generate_post_requires_event_id(tmp_path):
    db = make_db(tmp_path)
    add_persona(db, "p_emily", "Emily", dna=_EMILY_DNA)
    persona = {"id": "p_emily", "nickname": "Emily"}
    event = {"persona_id": "p_emily", "participants": ["p_emily"], "title": "x",
             "event_type": "social", "event_status": "completed"}
    assert run(generate_post_for_participant(
        db, FakeLLM({"Emily": _EMILY_POST}), event, persona)) is None
    assert post_count(db) == 0


def test_generate_post_rejects_fact_violating_output(tmp_path):
    db, event = two_participant_scene(tmp_path)
    persona = {"id": "p_emily", "nickname": "Emily", "language": "en",
               "speech_dna": json.dumps(_EMILY_DNA), "city": "London"}
    llm = FakeLLM({"Emily": "Jazz in Tokyo was the best night of 2019."})
    assert run(generate_post_for_participant(db, llm, event, persona)) is None
    assert post_count(db) == 0, "编造地点/年份的动态不得入库"


# --------------------------------------------------------------------------- #
# 核心验收：同一事件 → 多视角动态
# --------------------------------------------------------------------------- #
def test_two_participants_same_event_different_angles(tmp_path):
    db, event = two_participant_scene(tmp_path)
    llm = FakeLLM({"Emily": _EMILY_POST, "Sophie": _SOPHIE_POST})

    result = run(generate_event_posts(db, llm, event, rng=AlwaysShareRNG()))
    posts = result["posts"]

    assert len(posts) == 2, f"两个参与者应各得一条动态，实际 {result}"
    assert {p["source_event_id"] for p in posts} == {"ev_jazz"}, "两条动态必须指向同一个事件"
    assert {p["persona_id"] for p in posts} == {"p_emily", "p_sophie"}
    assert len({p["metadata"]["event_title"] for p in posts}) == 1, "事件标题快照必须一致"
    assert posts[0]["content"] != posts[1]["content"], "两个视角的正文不能一模一样"
    assert post_count(db) == 2

    by_persona = {p["persona_id"]: p for p in posts}
    assert by_persona["p_emily"]["content"] == _EMILY_POST
    assert by_persona["p_sophie"]["content"] == _SOPHIE_POST

    # 正文不得新增事件事实（地点/年份/AI）
    personas = {
        "p_emily": {"id": "p_emily", "nickname": "Emily", "city": "London"},
        "p_sophie": {"id": "p_sophie", "nickname": "Sophie", "city": "London"},
    }
    for post in posts:
        assert post_content_violations(
            post["content"], event, personas[post["persona_id"]]
        ) == [], post["content"]

    # 两条动态都通过同一个事件 id 反查得到
    assert {p["id"] for p in posts_for_event(db, "ev_jazz")} == {p["id"] for p in posts}


def test_generate_event_posts_reports_skipped(tmp_path):
    db, event = two_participant_scene(tmp_path)
    # 事件里多了一个不存在的人物 → 记 skipped 而不是崩
    event = dict(event)
    event["participants"] = ["p_emily", "p_sophie", "p_ghost"]
    result = run(generate_event_posts(db, FakeLLM(
        {"Emily": _EMILY_POST, "Sophie": _SOPHIE_POST}), event, rng=AlwaysShareRNG()))
    reasons = {item["persona_id"]: item["reason"] for item in result["skipped"]}
    assert reasons.get("p_ghost") == "persona_missing"
    assert len(result["posts"]) == 2


# --------------------------------------------------------------------------- #
# 评论
# --------------------------------------------------------------------------- #
def _persona_dict(db: DB, persona_id: str) -> dict:
    with db._lock:  # noqa: SLF001
        row = db.conn.execute("SELECT * FROM personas WHERE id=?", (persona_id,)).fetchone()
    return {key: row[key] for key in row.keys()}


def test_comment_is_idempotent_and_marks_persona_author(tmp_path):
    db, event = two_participant_scene(tmp_path)
    llm = FakeLLM({"Emily": _EMILY_POST, "Sophie": "Told you. You never listen."})
    post = run(generate_post_for_participant(db, llm, event, _persona_dict(db, "p_emily")))
    commenter = _persona_dict(db, "p_sophie")

    first = run(maybe_persona_comment(
        db, llm, post, event, commenter, rng=AlwaysShareRNG()))
    assert first is not None, "关系存在 + 概率通过时应写入评论"
    assert first["author_type"] == "persona"
    assert first["author_persona_id"] == "p_sophie"
    assert comment_count(db) == 1

    second = run(maybe_persona_comment(
        db, llm, post, event, commenter, rng=AlwaysShareRNG()))
    assert second["id"] == first["id"]
    assert comment_count(db) == 1, "同一 (post_id, commenter) 只允许一条"

    with db._lock:  # noqa: SLF001
        row = db.conn.execute("SELECT * FROM persona_post_comments WHERE post_id=?",
                              (post["id"],)).fetchone()
    assert row["author_type"] == "persona"
    assert row["content"] == "Told you. You never listen."
    # comment_count 已同步（信息流读的是 COUNT，这里保持列一致）
    with db._lock:  # noqa: SLF001
        post_row = db.conn.execute("SELECT comment_count FROM persona_posts WHERE id=?",
                                   (post["id"],)).fetchone()
    assert int(post_row["comment_count"]) == 1


def test_comment_requires_relationship(tmp_path):
    db = make_db(tmp_path)
    add_persona(db, "p_emily", "Emily", dna=_EMILY_DNA)
    add_persona(db, "p_oliver", "Oliver", dna=_SOPHIE_DNA)
    event = add_event(db, "ev_solo", persona_id="p_emily")
    llm = FakeLLM({"Emily": _EMILY_POST, "Oliver": "Nice one."})
    post = run(generate_post_for_participant(db, llm, event, _persona_dict(db, "p_emily")))
    comment = run(maybe_persona_comment(
        db, llm, post, event, _persona_dict(db, "p_oliver"), rng=AlwaysShareRNG()))
    assert comment is None, "没有 persona_relationships 关系就不该评论"
    assert comment_count(db) == 0


def test_comment_skips_own_post(tmp_path):
    db, event = two_participant_scene(tmp_path)
    llm = FakeLLM({"Emily": _EMILY_POST})
    post = run(generate_post_for_participant(db, llm, event, _persona_dict(db, "p_emily")))
    assert run(maybe_persona_comment(
        db, llm, post, event, _persona_dict(db, "p_emily"))) is None
    assert comment_count(db) == 0


def test_comment_probability_zero_without_relationship():
    assert comment_probability({"id": "p_a"}, {"warmth": 0.9}, None) == 0.0
    assert 0.0 < comment_probability(
        {"id": "p_a"}, {"warmth": 0.9}, {"closeness": 5}
    ) <= 1.0


# --------------------------------------------------------------------------- #
# 阶段三 · 关系张力影响评论概率
# --------------------------------------------------------------------------- #
def test_comment_probability_drops_with_tension():
    """tension 0/1/3/5 → 评论概率单调下降（刚吵过架不会立刻热情互动）。"""
    persona = {"id": "p_a"}
    traits = {"warmth": 0.9}
    probs = {
        tension: comment_probability(
            persona, traits, {"closeness": 5, "tension": tension}
        )
        for tension in (0, 1, 3, 5)
    }
    ordered = [probs[0], probs[1], probs[3], probs[5]]
    assert all(a > b for a, b in zip(ordered, ordered[1:])), probs
    # 系数与下限按契约：0→×1.00, 1→×0.82, 3→×0.46, 5→×0.15（下限）
    base = comment_probability(persona, traits, {"closeness": 5})
    for tension in (1, 3):
        penalty = 1.0 - TENSION_PENALTY_STEP * tension
        assert abs(probs[tension] - base * penalty) < 1e-9
    assert abs(probs[5] - base * TENSION_PENALTY_FLOOR) < 1e-9
    # 极端张力也不会把概率压到 0（仍有极小可能是「主动和解」）
    assert probs[5] > 0


def test_comment_probability_missing_tension_equals_zero():
    persona = {"id": "p_a"}
    traits = {"warmth": 0.9}
    without = comment_probability(persona, traits, {"closeness": 4})
    zero = comment_probability(persona, traits, {"closeness": 4, "tension": 0})
    assert without == zero, "缺 tension 字段必须与 0 等价（向后兼容）"
    # 脏 tension 值按 0 处理
    for dirty in (None, "abc", True, float("nan"), -3):
        assert comment_probability(
            persona, traits, {"closeness": 4, "tension": dirty}
        ) == zero
    # 超界张力按上限 5 处理（不越界压低概率）
    assert comment_probability(
        persona, traits, {"closeness": 4, "tension": 99}
    ) == comment_probability(persona, traits, {"closeness": 4, "tension": TENSION_MAX})


def test_tension_lowers_comment_in_real_flow(tmp_path):
    """真实链路：同一对关系把 tension 写大后，评论概率下降（只读，不写库）。"""
    db, event = two_participant_scene(tmp_path)
    post = run(generate_post_for_participant(
        db, FakeLLM({"Emily": _EMILY_POST}), event, _persona_dict(db, "p_emily")))
    assert post is not None
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute("UPDATE persona_relationships SET tension=5")
    from app.v3.event_posts import _relationship  # noqa: PLC0415
    rel = _relationship(db, "p_emily", "p_sophie")
    assert rel["tension"] == 5
    low = comment_probability({"id": "p_sophie"}, {}, {"closeness": 5, "tension": 0})
    high = comment_probability({"id": "p_sophie"}, {}, rel)
    assert high < low


# --------------------------------------------------------------------------- #
# 阶段三 · 人物化发文预算
# --------------------------------------------------------------------------- #
def test_persona_post_budget_extremes():
    low = persona_post_budget({"disclosure": 0.0, "social_distance": 0.0})
    high = persona_post_budget({"disclosure": 1.0, "social_distance": 1.0})
    assert low == POST_BUDGET_MIN == 2
    assert high == POST_BUDGET_MAX == 8
    assert high - low == 6, "冷淡 vs 话痨必须有量级差异"

    # 缺键/默认人物：安全中间值
    assert persona_post_budget({}) == 5
    assert persona_post_budget(None) == 5


def test_persona_post_budget_dirty_inputs():
    for dirty in (
        {"disclosure": "abc", "social_distance": "xyz"},
        {"disclosure": None, "social_distance": None},
        {"disclosure": float("nan"), "social_distance": float("inf")},
        {"disclosure": True, "social_distance": False},
        "not-a-dict",
        None,
    ):
        budget = persona_post_budget(dirty)
        assert isinstance(budget, int)
        assert POST_BUDGET_MIN <= budget <= POST_BUDGET_MAX, (dirty, budget)
    # 越界值被 clamp（不是直接溢出）
    assert persona_post_budget({"disclosure": 10, "social_distance": 10}) == 8
    assert persona_post_budget({"disclosure": -10, "social_distance": -10}) == 2


def test_persona_post_budget_differs_by_dna():
    """真实 DNA：open+intimate 的人物预算高于 guarded+distant 的人物。"""
    loud = {"id": "p_loud", "speech_dna": json.dumps(_EMILY_DNA)}
    quiet = {"id": "p_quiet", "speech_dna": json.dumps({
        "self_disclosure": "guarded", "social_distance": "distant",
        "emotional_expression": "reserved",
    })}
    loud_budget = persona_post_budget(dna_traits_for(loud))
    quiet_budget = persona_post_budget(dna_traits_for(quiet))
    assert loud_budget >= quiet_budget + 3, (quiet_budget, loud_budget)
    assert quiet_budget <= 4 and loud_budget >= 7


def test_budget_and_daily_quota_take_min(tmp_path):
    """人物预算 8，但系统日配额 2 → 当天只出 2 条（两者取小）。"""
    db = make_db(tmp_path)
    add_persona(db, "p_emily", "Emily", dna=_EMILY_DNA)
    add_persona(db, "p_sophie", "Sophie", dna=_EMILY_DNA)
    add_event(db, "ev_1", persona_id="p_emily", start_hours_ago=4)
    add_event(db, "ev_2", persona_id="p_sophie", start_hours_ago=3)
    add_event(db, "ev_3", persona_id="p_emily", start_hours_ago=2)
    llm = FakeLLM({"Emily": _EMILY_POST, "Sophie": _SOPHIE_POST})
    stats = run(run_posts_cycle(db, llm, max_events=4, daily_quota=2,
                                rng=AlwaysShareRNG()))
    assert persona_post_budget(dna_traits_for(_persona_dict(db, "p_emily"))) == 8
    assert stats["posts"] == 2, "系统日配额先卡住"
    assert stats["today_posts"] == 2
    second = run(run_posts_cycle(db, llm, max_events=4, daily_quota=2,
                                 rng=AlwaysShareRNG()))
    assert second["posts"] == 0


def test_comment_none_when_llm_fails(tmp_path):
    db, event = two_participant_scene(tmp_path)
    post = run(generate_post_for_participant(
        db, FakeLLM({"Emily": _EMILY_POST}), event, _persona_dict(db, "p_emily")))
    assert run(maybe_persona_comment(
        db, FailingLLM(), post, event, _persona_dict(db, "p_sophie"),
        rng=AlwaysShareRNG())) is None
    assert comment_count(db) == 0


# --------------------------------------------------------------------------- #
# 批处理
# --------------------------------------------------------------------------- #
def test_run_posts_cycle_is_idempotent(tmp_path):
    db, _event = two_participant_scene(tmp_path)
    llm = FakeLLM({"Emily": _EMILY_POST, "Sophie": _SOPHIE_POST,
                   "Sophie ": "Told you."})

    first = run(run_posts_cycle(db, llm, rng=AlwaysShareRNG()))
    assert first["posts"] == 2, first
    assert first["events"] == 1
    assert post_count(db) == 2
    # 事件动态 ≤2 条，评论 ≤参与者数
    assert comment_count(db) <= 2

    before_posts, before_comments = post_count(db), comment_count(db)
    second = run(run_posts_cycle(db, llm, rng=AlwaysShareRNG()))
    assert second["posts"] == 0, "第二次运行不得新增动态"
    assert second["comments"] == 0, "第二次运行不得新增评论"
    assert post_count(db) == before_posts
    assert comment_count(db) == before_comments


def test_run_posts_cycle_since_hours_skips_stale_events(tmp_path):
    """since_hours：只处理「最近 N 小时内真正结束」的事件（避免首次启动补发历史）。

    - 刚完成的事件（1h 前结束）→ 处理；
    - 30h 前就结束的事件 → 不处理（产品上不该现在才发动态）；
    - 再跑一次仍然新增 0 条。
    """
    db = make_db(tmp_path)
    add_persona(db, "p_emily", "Emily", dna=_EMILY_DNA)
    # 3h 前开始、2h 时长 → 1h 前结束
    add_event(db, "ev_fresh", persona_id="p_emily", start_hours_ago=3,
              duration_hours=2)
    # 32h 前开始、2h 时长 → 30h 前结束
    add_event(db, "ev_stale", persona_id="p_emily", start_hours_ago=32,
              duration_hours=2)
    llm = FakeLLM({"Emily": _EMILY_POST})

    stats = run(run_posts_cycle(db, llm, since_hours=12, rng=AlwaysShareRNG()))
    assert stats["since_hours"] == 12
    assert stats["posts"] == 1
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT source_event_id FROM persona_posts ORDER BY source_event_id"
        ).fetchall()
    assert [row["source_event_id"] for row in rows] == ["ev_fresh"]

    second = run(run_posts_cycle(db, llm, since_hours=12, rng=AlwaysShareRNG()))
    assert second["posts"] == 0
    assert second["comments"] == 0
    assert post_count(db) == 1


def test_run_posts_cycle_since_hours_none_keeps_old_behavior(tmp_path):
    """since_hours 传 None / 0 / 非数字 → 与不传完全一致（向后兼容）。"""
    db = make_db(tmp_path)
    add_persona(db, "p_emily", "Emily", dna=_EMILY_DNA)
    add_event(db, "ev_stale", persona_id="p_emily", start_hours_ago=32,
              duration_hours=2)  # 30h 前结束
    llm = FakeLLM({"Emily": _EMILY_POST})

    stats = run(run_posts_cycle(db, llm, rng=AlwaysShareRNG()))
    assert stats["since_hours"] is None
    assert stats["posts"] == 1, "不传 since_hours 时历史事件照旧处理（旧行为）"

    # 0 / 负数 / 垃圾值都等价于「不限制」
    for index, bad in enumerate((0, -3, "abc", "")):
        case_dir = tmp_path / f"case_{index}"
        case_dir.mkdir()
        fresh = make_db(case_dir)
        add_persona(fresh, "p_emily", "Emily", dna=_EMILY_DNA)
        add_event(fresh, "ev_stale", persona_id="p_emily", start_hours_ago=32,
                  duration_hours=2)
        stats = run(run_posts_cycle(fresh, llm, since_hours=bad, rng=AlwaysShareRNG()))
        assert stats["since_hours"] is None, bad
        assert stats["posts"] == 1, bad


def test_completed_event_rows_skips_evaluated_events(tmp_path):
    """带 ``posts_evaluated_at`` 标记的事件不再进候选队列（Lead 指定的行为）。"""
    db = make_db(tmp_path)
    add_persona(db, "p_emily", "Emily", dna=_EMILY_DNA)
    add_event(db, "ev_keep", persona_id="p_emily", start_hours_ago=4)
    add_event(db, "ev_done", persona_id="p_emily", start_hours_ago=2)

    assert [e["id"] for e in ep._completed_event_rows(db, day=None, limit=10)] == [
        "ev_keep", "ev_done",
    ]
    ep._record_posts_attempt(db, "ev_done", generated=0, retryable=False)

    assert [e["id"] for e in ep._completed_event_rows(db, day=None, limit=10)] == [
        "ev_keep"
    ]


def test_run_posts_cycle_marks_evaluated_and_moves_on(tmp_path):
    """核心修复：被概率拒绝的事件也只评估一次，队列必须往前走。

    3 条 completed 事件（A 发、B 拒绝、C 发）：
    - 第一轮处理 A/B/C，产出 2 条动态；B 也被写入 posts_evaluated_at（posts_generated=0）；
    - 第二轮不再把任何一条当候选（events=0），因此不会每轮重试 B；
    - 幂等仍然成立：第二次运行新增 0 条。
    """
    db = make_db(tmp_path)
    add_persona(db, "p_emily", "Emily", dna=_EMILY_DNA)
    # 按完成时间从老到新：A(5h) → B(4h) → C(3h)，ASC 顺序正好是 A,B,C
    add_event(db, "ev_a", persona_id="p_emily", start_hours_ago=6, duration_hours=1)
    add_event(db, "ev_b", persona_id="p_emily", start_hours_ago=5, duration_hours=1)
    add_event(db, "ev_c", persona_id="p_emily", start_hours_ago=4, duration_hours=1)
    llm = FakeLLM({"Emily": _EMILY_POST})

    stats = run(run_posts_cycle(db, llm, rng=SequenceRNG([0.0, 0.99, 0.0])))
    assert stats["events"] == 3, stats
    assert stats["posts"] == 2, stats
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT source_event_id FROM persona_posts ORDER BY source_event_id"
        ).fetchall()
    assert [row["source_event_id"] for row in rows] == ["ev_a", "ev_c"]

    with db._lock:  # noqa: SLF001
        metas = {
            row["id"]: json.loads(row["metadata"] or "{}")
            for row in db.conn.execute("SELECT id, metadata FROM persona_events").fetchall()
        }
    for event_id in ("ev_a", "ev_b", "ev_c"):
        assert metas[event_id].get("posts_evaluated_at"), f"{event_id} 应已评估终结"
    assert metas["ev_b"]["posts_generated"] == 0, "B 是「不想发」，不是「没轮上」"
    assert post_count(db) == 2

    second = run(run_posts_cycle(db, llm, rng=SequenceRNG([0.0, 0.0])))
    assert second["events"] == 0, "已评估事件不得再进候选队列"
    assert second["posts"] == 0
    assert second["comments"] == 0
    assert post_count(db) == 2


def test_run_posts_cycle_processes_oldest_first(tmp_path):
    """排序改成 ASC：单轮名额有限时先消化最早的事件，积压才能被推完。"""
    db = make_db(tmp_path)
    add_persona(db, "p_emily", "Emily", dna=_EMILY_DNA)
    add_event(db, "ev_old", persona_id="p_emily", start_hours_ago=6, duration_hours=1)
    add_event(db, "ev_new", persona_id="p_emily", start_hours_ago=2, duration_hours=1)
    llm = FakeLLM({"Emily": _EMILY_POST})

    first = run(run_posts_cycle(db, llm, max_events=1, rng=AlwaysShareRNG()))
    assert first["events"] == 1 and first["posts"] == 1, first
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT source_event_id FROM persona_posts ORDER BY source_event_id"
        ).fetchall()
    assert [row["source_event_id"] for row in rows] == ["ev_old"], "最老的事件先处理"

    second = run(run_posts_cycle(db, llm, max_events=1, rng=AlwaysShareRNG()))
    assert second["posts"] == 1, "下一轮才轮到最新的事件"
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT source_event_id FROM persona_posts ORDER BY source_event_id"
        ).fetchall()
    assert [row["source_event_id"] for row in rows] == ["ev_new", "ev_old"]


def test_run_posts_cycle_retries_transient_llm_failure(tmp_path):
    """LLM 抖动（抛异常）不写库、也不终结事件；下一轮能补上。

    只有这样「偶发失败」才不会因为写了 posts_evaluated_at 而永久丢掉这个事件；
    而「概率拒绝」是终局决定（见上一个用例），两者必须区分开。
    """
    db = make_db(tmp_path)
    add_persona(db, "p_emily", "Emily", dna=_EMILY_DNA)
    add_event(db, "ev_flaky", persona_id="p_emily", start_hours_ago=3)

    stats = run(run_posts_cycle(db, FailingLLM(), rng=AlwaysShareRNG()))
    assert stats["posts"] == 0
    assert post_count(db) == 0
    with db._lock:  # noqa: SLF001
        meta = json.loads(
            db.conn.execute(
                "SELECT metadata FROM persona_events WHERE id='ev_flaky'"
            ).fetchone()["metadata"] or "{}"
        )
    assert not meta.get("posts_evaluated_at"), "LLM 偶发失败不应终结事件"
    assert meta.get("posts_attempts") == 1

    retry = run(run_posts_cycle(db, FakeLLM({"Emily": _EMILY_POST}), rng=AlwaysShareRNG()))
    assert retry["posts"] == 1, "下一轮应补上"
    with db._lock:  # noqa: SLF001
        meta = json.loads(
            db.conn.execute(
                "SELECT metadata FROM persona_events WHERE id='ev_flaky'"
            ).fetchone()["metadata"] or "{}"
        )
    assert meta.get("posts_evaluated_at")
    assert meta.get("posts_generated") == 1


def test_run_posts_cycle_stops_retrying_after_retry_limit(tmp_path):
    """连续失败达到上限后依然终结（否则一条坏事件会永久占住队首）。"""
    db = make_db(tmp_path)
    add_persona(db, "p_emily", "Emily", dna=_EMILY_DNA)
    add_event(db, "ev_broken", persona_id="p_emily", start_hours_ago=3)

    for _ in range(ep.POSTS_EVAL_RETRY_LIMIT):
        stats = run(run_posts_cycle(db, FailingLLM(), rng=AlwaysShareRNG()))
        assert stats["posts"] == 0

    with db._lock:  # noqa: SLF001
        meta = json.loads(
            db.conn.execute(
                "SELECT metadata FROM persona_events WHERE id='ev_broken'"
            ).fetchone()["metadata"] or "{}"
        )
    assert meta.get("posts_evaluated_at"), "达到重试上限后必须终结，避免队列卡死"
    assert meta.get("posts_attempts") == ep.POSTS_EVAL_RETRY_LIMIT

    # 之后即使 LLM 恢复，该事件也不再进队列
    after = run(run_posts_cycle(db, FakeLLM({"Emily": _EMILY_POST}),
                                rng=AlwaysShareRNG()))
    assert after["events"] == 0
    assert post_count(db) == 0


def test_run_posts_cycle_ignores_unfinished_events(tmp_path):
    db = make_db(tmp_path)
    add_persona(db, "p_emily", "Emily", dna=_EMILY_DNA)
    add_event(db, "ev_planned", persona_id="p_emily", status="planned",
              start_hours_ago=-24)
    add_event(db, "ev_ongoing", persona_id="p_emily", status="ongoing",
              start_hours_ago=1)
    add_event(db, "ev_cancelled", persona_id="p_emily", status="cancelled")
    stats = run(run_posts_cycle(db, FakeLLM({"Emily": _EMILY_POST}),
                                rng=AlwaysShareRNG()))
    assert stats["posts"] == 0, "只有 completed 事件才能派生出「已发生」动态"
    assert stats["comments"] == 0
    assert post_count(db) == 0


def test_run_posts_cycle_skips_persona_under_budget(tmp_path):
    """24h 内已发满自己预算的人物在批处理里被硬性跳过（不发不重复刷屏）。

    Emily 的 DNA（open + intimate）→ budget 8；预置 8 条即用完，Sophie（budget 6）照发。
    """
    db, _event = two_participant_scene(tmp_path)
    emily_budget = persona_post_budget(dna_traits_for(_persona_dict(db, "p_emily")))
    now = _iso(_now())
    with db._lock, db.conn:  # noqa: SLF001
        for index in range(emily_budget):
            db.conn.execute(
                "INSERT INTO persona_posts (id, persona_id, content, created_at)"
                " VALUES (?,?,?,?)",
                (f"pp_spam_{index}", "p_emily", f"old post {index}", now),
            )
    llm = FakeLLM({"Emily": _EMILY_POST, "Sophie": _SOPHIE_POST})
    stats = run(run_posts_cycle(db, llm, rng=AlwaysShareRNG()))
    assert stats["posts"] == 1, "Emily 预算用尽，只有 Sophie 发"
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT persona_id FROM persona_posts WHERE source_event_id='ev_jazz'"
        ).fetchall()
    assert [row["persona_id"] for row in rows] == ["p_sophie"]

    # 即使 Emily 后来预算恢复，同一事件也不该再补发（事件级幂等闸门）
    second = run(run_posts_cycle(db, llm, rng=AlwaysShareRNG()))
    assert second["posts"] == 0
    assert post_count(db) == emily_budget + 1  # 预置 + Sophie 的 1 条


def test_run_posts_cycle_filters_by_day(tmp_path):
    db, _ = two_participant_scene(tmp_path)
    today = (_now() - timedelta(hours=26)).strftime("%Y-%m-%d")
    llm = FakeLLM({"Emily": _EMILY_POST, "Sophie": _SOPHIE_POST})
    stats = run(run_posts_cycle(db, llm, day=today, rng=AlwaysShareRNG()))
    assert stats["posts"] == 2
    other = run(run_posts_cycle(db, llm, day="1999-01-01", rng=AlwaysShareRNG()))
    assert other["posts"] == 0


# --------------------------------------------------------------------------- #
# 阶段二 · 价值排序（当天先出现「值得分享」的事件）
# --------------------------------------------------------------------------- #
def test_value_tier_mapping():
    assert value_tier("achievement") == 3
    assert value_tier("conflict") == 3
    assert value_tier("personal_milestone") == 3
    assert value_tier("relationship") == 3
    assert value_tier("social") == 2
    assert value_tier("entertainment") == 2
    assert value_tier("travel") == 2
    assert value_tier("work") == 1
    assert value_tier("study") == 1
    assert value_tier("unexpected_event") == 1
    assert value_tier("meal") == 0
    assert value_tier("daily_routine") == 0
    # 未知 / 空值按最低层处理（不抢当天的位置）
    assert value_tier("nonsense") == 0
    assert value_tier(None) == 0


def test_value_ordering_prefers_achievement_over_meal(tmp_path):
    """meal 更早完成，但 achievement 价值更高 → max_events=1 时先处理 achievement。"""
    db = make_db(tmp_path)
    add_persona(db, "p_emily", "Emily", dna=_EMILY_DNA)
    add_event(db, "ev_meal", persona_id="p_emily", event_type="meal",
              title="Lunch", description="A sandwich.", start_hours_ago=5,
              duration_hours=0.5)
    add_event(db, "ev_ach", persona_id="p_emily", event_type="achievement",
              title="Finished the half marathon", description="Ran the whole thing.",
              start_hours_ago=1, duration_hours=0.5)

    rows = ep._completed_event_rows(db, day=None, limit=10)
    assert [row["id"] for row in rows] == ["ev_ach", "ev_meal"]

    llm = FakeLLM({"Emily": _EMILY_POST})
    stats = run(run_posts_cycle(db, llm, max_events=1, daily_quota=0,
                                rng=AlwaysShareRNG()))
    assert stats["posts"] == 1, stats
    assert [post["source_event_id"] for post in posts_for_event(db, "ev_ach")] == [
        "ev_ach"
    ]
    assert posts_for_event(db, "ev_meal") == [], "高价值事件先占当天名额"

    second = run(run_posts_cycle(db, llm, max_events=1, daily_quota=0,
                                 rng=AlwaysShareRNG()))
    assert second["posts"] == 1
    assert len(posts_for_event(db, "ev_meal")) == 1


# --------------------------------------------------------------------------- #
# 阶段二 · 每日配额
# --------------------------------------------------------------------------- #
def test_daily_quota_limits_posts_per_local_day(tmp_path):
    db = make_db(tmp_path)
    add_persona(db, "p_emily", "Emily", dna=_EMILY_DNA)
    add_event(db, "ev_1", persona_id="p_emily", start_hours_ago=4)
    add_event(db, "ev_2", persona_id="p_emily", start_hours_ago=3)
    llm = FakeLLM({"Emily": _EMILY_POST})

    first = run(run_posts_cycle(db, llm, max_events=4, daily_quota=1,
                                rng=AlwaysShareRNG()))
    assert first["daily_quota"] == 1
    assert first["posts"] == 1, "配额=1 时一轮最多 1 条"
    assert first["today_posts"] == 1
    assert post_count(db) == 1

    second = run(run_posts_cycle(db, llm, max_events=4, daily_quota=1,
                                 rng=AlwaysShareRNG()))
    assert second["posts"] == 0, "当天配额用尽，第二轮 0 条"
    assert second["today_posts"] == 1
    assert post_count(db) == 1


def test_daily_quota_caps_multi_participant_event(tmp_path):
    """配额是「动态条数」而不是「事件数」：2 个参与者 + 配额 1 → 只发 1 条。"""
    db, _event = two_participant_scene(tmp_path)
    llm = FakeLLM({"Emily": _EMILY_POST, "Sophie": _SOPHIE_POST})
    stats = run(run_posts_cycle(db, llm, max_events=4, daily_quota=1,
                                rng=AlwaysShareRNG()))
    assert stats["posts"] == 1, stats
    assert stats["today_posts"] == 1
    assert post_count(db) == 1


def test_daily_quota_from_env(monkeypatch, tmp_path):
    db = make_db(tmp_path)
    add_persona(db, "p_emily", "Emily", dna=_EMILY_DNA)
    add_event(db, "ev_1", persona_id="p_emily", start_hours_ago=2)
    llm = FakeLLM({"Emily": _EMILY_POST})

    monkeypatch.setenv("SE_EVENT_POSTS_PER_DAY", "3")
    stats = run(run_posts_cycle(db, llm, rng=AlwaysShareRNG()))
    assert stats["daily_quota"] == 3, "参数缺省时读 SE_EVENT_POSTS_PER_DAY"

    monkeypatch.setenv("SE_EVENT_POSTS_PER_DAY", "0")
    stats = run(run_posts_cycle(db, llm, rng=AlwaysShareRNG()))
    assert stats["daily_quota"] is None, "env=0 表示不限制"


def test_daily_quota_zero_means_unlimited(tmp_path):
    db = make_db(tmp_path)
    add_persona(db, "p_emily", "Emily", dna=_EMILY_DNA)
    add_event(db, "ev_1", persona_id="p_emily", start_hours_ago=4)
    add_event(db, "ev_2", persona_id="p_emily", start_hours_ago=3)
    llm = FakeLLM({"Emily": _EMILY_POST})
    stats = run(run_posts_cycle(db, llm, max_events=4, daily_quota=0,
                                rng=AlwaysShareRNG()))
    assert stats["daily_quota"] is None
    assert stats["posts"] == 2, "<=0 = 不限制"


# --------------------------------------------------------------------------- #
# 阶段二 · 评论补位
# --------------------------------------------------------------------------- #
def test_comment_backfill_comments_old_post_then_idempotent(tmp_path):
    """老动态（不是本轮刚发的）也能补到评论；第二次调用 0 条。"""
    db = make_db(tmp_path)
    add_persona(db, "p_emily", "Emily", dna=_EMILY_DNA)
    add_persona(db, "p_lena", "Lena", dna=_SOPHIE_DNA)
    add_relationship(db, "p_emily", "p_lena", closeness=4)
    add_event(db, "ev_old", persona_id="p_emily", participants=["p_emily"],
              start_hours_ago=6)
    add_event_post(db, "pp_old", persona_id="p_emily", event_id="ev_old",
                   hours_ago=5)
    llm = FakeLLM({"Lena": "That sounds like a good night."})

    stats = run(comment_backfill(db, llm, max_comments=2, rng=AlwaysShareRNG()))
    assert stats["comments"] == 1, stats
    assert stats["posts"] == 1
    assert comment_count(db) == 1
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT author_persona_id, author_type FROM persona_post_comments"
        ).fetchone()
    assert row["author_persona_id"] == "p_lena"
    assert row["author_type"] == "persona"

    second = run(comment_backfill(db, llm, max_comments=2, rng=AlwaysShareRNG()))
    assert second["comments"] == 0, "同一条动态只能补 1 条 AI 评论（幂等）"
    assert comment_count(db) == 1


def test_comment_backfill_respects_max_comments(tmp_path):
    db = make_db(tmp_path)
    add_persona(db, "p_emily", "Emily", dna=_EMILY_DNA)
    add_persona(db, "p_lena", "Lena", dna=_SOPHIE_DNA)
    add_relationship(db, "p_emily", "p_lena", closeness=4)
    add_event(db, "ev_1", persona_id="p_emily", participants=["p_emily"],
              start_hours_ago=5)
    add_event(db, "ev_2", persona_id="p_emily", participants=["p_emily"],
              start_hours_ago=4)
    add_event_post(db, "pp_1", persona_id="p_emily", event_id="ev_1", hours_ago=4)
    add_event_post(db, "pp_2", persona_id="p_emily", event_id="ev_2", hours_ago=3)
    llm = FakeLLM({"Lena": "That sounds like a good night."})

    first = run(comment_backfill(db, llm, max_comments=1, rng=AlwaysShareRNG()))
    assert first["comments"] == 1, "单次最多 max_comments 条"
    assert comment_count(db) == 1

    second = run(comment_backfill(db, llm, max_comments=2, rng=AlwaysShareRNG()))
    assert second["comments"] == 1, "剩下的那条动态被补上"
    assert comment_count(db) == 2

    third = run(comment_backfill(db, llm, max_comments=5, rng=AlwaysShareRNG()))
    assert third["comments"] == 0


def test_comment_backfill_skips_old_posts_outside_window(tmp_path):
    db = make_db(tmp_path)
    add_persona(db, "p_emily", "Emily", dna=_EMILY_DNA)
    add_persona(db, "p_lena", "Lena", dna=_SOPHIE_DNA)
    add_relationship(db, "p_emily", "p_lena", closeness=4)
    add_event(db, "ev_old", persona_id="p_emily", participants=["p_emily"],
              start_hours_ago=12)
    add_event_post(db, "pp_old", persona_id="p_emily", event_id="ev_old",
                   hours_ago=10)
    llm = FakeLLM({"Lena": "That sounds like a good night."})
    stats = run(comment_backfill(db, llm, window_hours=6,
                                 rng=AlwaysShareRNG()))
    assert stats["comments"] == 0, "超过 window_hours 的老动态不硬塞互动"
    assert comment_count(db) == 0


def test_comment_backfill_requires_relationship(tmp_path):
    db = make_db(tmp_path)
    add_persona(db, "p_emily", "Emily", dna=_EMILY_DNA)
    add_persona(db, "p_lena", "Lena", dna=_SOPHIE_DNA)  # 存在但没关系
    add_event(db, "ev_old", persona_id="p_emily", participants=["p_emily"],
              start_hours_ago=6)
    add_event_post(db, "pp_old", persona_id="p_emily", event_id="ev_old",
                   hours_ago=5)
    llm = FakeLLM({"Lena": "That sounds like a good night."})
    stats = run(comment_backfill(db, llm, rng=AlwaysShareRNG()))
    assert stats["comments"] == 0, "不认识的人不能评论（不凭空造关系）"
    assert comment_count(db) == 0


def test_comment_backfill_never_uses_event_participants(tmp_path):
    """评论者必须是「事件参与者之外」的熟人：同去的 Sophie 不算补位候选。"""
    db = make_db(tmp_path)
    add_persona(db, "p_emily", "Emily", dna=_EMILY_DNA)
    add_persona(db, "p_sophie", "Sophie", dna=_SOPHIE_DNA)
    add_relationship(db, "p_emily", "p_sophie", closeness=5)
    add_event(db, "ev_jazz", persona_id="p_emily",
              participants=["p_emily", "p_sophie"], start_hours_ago=6)
    add_event_post(db, "pp_jazz", persona_id="p_emily", event_id="ev_jazz",
                   hours_ago=5)
    llm = FakeLLM({"Sophie": "Told you."})
    stats = run(comment_backfill(db, llm, rng=AlwaysShareRNG()))
    assert stats["comments"] == 0, "参与者不能充当「补位评论者」"

    # 加一个不在场的熟人 → 立刻补上
    add_persona(db, "p_lena", "Lena", dna=_SOPHIE_DNA)
    add_relationship(db, "p_emily", "p_lena", closeness=3)
    stats = run(comment_backfill(db, llm, rng=AlwaysShareRNG()))
    assert stats["comments"] == 1
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT author_persona_id FROM persona_post_comments"
        ).fetchone()
    assert row["author_persona_id"] == "p_lena"


def test_run_posts_cycle_auto_backfills_and_can_be_disabled(tmp_path, monkeypatch):
    db = make_db(tmp_path)
    add_persona(db, "p_emily", "Emily", dna=_EMILY_DNA)
    add_persona(db, "p_lena", "Lena", dna=_SOPHIE_DNA)
    add_relationship(db, "p_emily", "p_lena", closeness=4)
    add_event(db, "ev_today", persona_id="p_emily", participants=["p_emily"],
              start_hours_ago=2)
    llm = FakeLLM({"Emily": _EMILY_POST, "Lena": "That sounds like a good night."})

    stats = run(run_posts_cycle(db, llm, daily_quota=0, rng=AlwaysShareRNG()))
    assert stats["posts"] == 1
    assert stats["backfill_comments"] == 1, "cycle 结尾自动补位"
    assert comment_count(db) == 1

    # 关掉补位：新事件动态不再产生评论
    case_dir = tmp_path / "off"
    case_dir.mkdir()
    db2 = make_db(case_dir)
    add_persona(db2, "p_emily", "Emily", dna=_EMILY_DNA)
    add_persona(db2, "p_lena", "Lena", dna=_SOPHIE_DNA)
    add_relationship(db2, "p_emily", "p_lena", closeness=4)
    add_event(db2, "ev_today", persona_id="p_emily", participants=["p_emily"],
              start_hours_ago=2)
    monkeypatch.setenv("SE_COMMENT_BACKFILL", "0")
    stats_off = run(run_posts_cycle(db2, llm, daily_quota=0, rng=AlwaysShareRNG()))
    assert stats_off["posts"] == 1
    assert stats_off["backfill_comments"] == 0
    assert comment_count(db2) == 0


# --------------------------------------------------------------------------- #
# persona_recent_facts
# --------------------------------------------------------------------------- #
def test_persona_recent_facts_empty_for_unknown_persona(tmp_path):
    db = make_db(tmp_path)
    assert persona_recent_facts(db, "") == ""
    assert persona_recent_facts(db, "p_nobody") == ""


def test_persona_recent_facts_excludes_cancelled_and_marks_planned(tmp_path):
    db = make_db(tmp_path)
    add_persona(db, "p_emily", "Emily", dna=_EMILY_DNA)
    add_event(db, "ev_done", persona_id="p_emily", title="Finished the marathon",
              description="Ran the whole thing.", status="completed",
              start_hours_ago=30)
    add_event(db, "ev_cancelled", persona_id="p_emily", title="Cancelled dinner",
              description="Nobody came.", status="cancelled", start_hours_ago=20)
    add_event(db, "ev_plan", persona_id="p_emily", title="Dinner with Sophie",
              description="Booked a table.", status="planned", start_hours_ago=-48)

    text = persona_recent_facts(db, "p_emily", "marathon")
    assert "Finished the marathon" in text
    assert "Cancelled dinner" not in text
    assert "Dinner with Sophie" in text
    assert "not happened yet" in text
    assert "PLAN" in text


def test_persona_recent_facts_includes_own_posts_and_comments(tmp_path):
    db, event = two_participant_scene(tmp_path)
    llm = FakeLLM({"Emily": _EMILY_POST, "Sophie": "Told you. You never listen."})
    post = run(generate_post_for_participant(db, llm, event, _persona_dict(db, "p_emily")))
    run(maybe_persona_comment(db, llm, post, event, _persona_dict(db, "p_sophie"),
                              rng=AlwaysShareRNG()))

    text = persona_recent_facts(db, "p_emily", "jazz")
    assert _EMILY_POST in text
    assert "Didn't expect to enjoy jazz" in text
    assert "Sophie:" in text, text
    assert "Told you. You never listen." in text
    assert "Jazz at the Blue Note" in text
    # 事实块是"已发生"，不该出现计划前缀
    assert "not happened yet" not in text


def test_persona_recent_facts_ranks_by_query(tmp_path):
    db = make_db(tmp_path)
    add_persona(db, "p_emily", "Emily", dna=_EMILY_DNA)
    add_event(db, "ev_jazz", persona_id="p_emily", title="Jazz bar with Sophie",
              description="Small jazz bar.", event_type="entertainment",
              start_hours_ago=30)
    add_event(db, "ev_gym", persona_id="p_emily", title="Gym session",
              description="Leg day.", event_type="work", start_hours_ago=5)

    text = persona_recent_facts(db, "p_emily", "jazz bar", limit=1)
    assert "Jazz bar with Sophie" in text
    assert "Gym session" not in text


def test_persona_recent_facts_uses_viewer_relative_companions(tmp_path):
    """同一条事件对不同参与者要写出「和谁在一起」——不能对着 Sophie 说「with Sophie」。"""
    db, event = two_participant_scene(tmp_path)
    emily_text = persona_recent_facts(db, "p_emily", "jazz")
    sophie_text = persona_recent_facts(db, "p_sophie", "jazz")
    assert "with Sophie" in emily_text
    assert "with Emily" in sophie_text
    assert "with Sophie" not in sophie_text


def test_persona_recent_facts_respects_max_chars(tmp_path):
    db = make_db(tmp_path)
    add_persona(db, "p_emily", "Emily", dna=_EMILY_DNA)
    add_event(db, "ev_long", persona_id="p_emily",
              title="A very long title " * 6,
              description="description " * 30, status="completed", start_hours_ago=10)
    text = persona_recent_facts(db, "p_emily", max_chars=120)
    assert len(text) <= 120
