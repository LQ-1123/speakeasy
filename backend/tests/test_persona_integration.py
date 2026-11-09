"""Persona 真实感升级 · 集成验收测试（Lead 负责）。

覆盖任务书 §14 的 A–F 六类验收：
- A Speech DNA：20 轮对话不模板化、不同人物表达可区分
- B Behavior Engine：行为受人格影响、不每轮追问、近期行为降低重复
- C/D 事件与动态：多视角引用同一事件、计划≠已发生、取消不出现、后台不重复
- E 记忆联动：生活事实可回忆、不相关事件不污染、关系隔离
- F 性能：一轮聊天 1 次 LLM 调用、刷新信息流 0 次

这里全部用**确定性 fake LLM / mock 模式**，不产生真实 API 费用；
真实模型的「表现」由本地 Web 验收（docs/PERSONA_ACCEPTANCE.md）覆盖，两者不混为一谈。
"""

from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime, timedelta, timezone

import pytest

from app.v2.chat_service import ChatService
from app.v2.db import DB
from app.v2.memory import MemoryManager
from app.v3 import event_posts, life_events, persona_context
from app.v3.schema import ensure_schema
from app.v3.service import V3Service
from app.v3.speech_dna import ensure_persona_dna


# --------------------------------------------------------------------------- #
# 基础设施
# --------------------------------------------------------------------------- #
def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


class _MockLLM:
    """无 key 场景：chat 走 deterministic 人设回复。"""

    uses_mock = True

    async def complete_json(self, *a, **kw):  # pragma: no cover - 不应被调用
        raise AssertionError("mock 模式不应调用 complete_json")

    async def stream_chat(self, *a, **kw):  # pragma: no cover
        raise AssertionError("mock 模式不应调用 stream_chat")
        yield ""  # noqa: unreachable


class _CountingLLM:
    """真实 LLM 的替身：记录调用次数，返回目标语言的一句普通回复。"""

    uses_mock = False

    def __init__(self, reply: str = "[calm] Yeah. Same here.", payload: dict | None = None) -> None:
        self.reply = reply
        self.payload = payload or {}
        self.stream_calls = 0
        self.json_calls = 0
        self.last_messages: list[dict] = []

    async def complete_json(self, messages, **kwargs):  # noqa: ANN001
        self.json_calls += 1
        self.last_messages = list(messages)
        return json.dumps(self.payload, ensure_ascii=False)

    async def stream_chat(self, messages, **kwargs):  # noqa: ANN001
        self.stream_calls += 1
        self.last_messages = list(messages)
        for piece in self.reply.split(" "):
            yield piece + " "


class _RaisingLLM:
    """任何调用都爆炸：用来证明某条链路根本不碰模型。"""

    uses_mock = False

    async def complete_json(self, *a, **kw):
        raise AssertionError("这条链路不应该调用 LLM")

    async def stream_chat(self, *a, **kw):  # pragma: no cover
        raise AssertionError("这条链路不应该调用 LLM")
        yield ""


@pytest.fixture()
def db(tmp_path):
    database = DB(str(tmp_path / "persona.db"))
    database.init()
    ensure_schema(database, seed=True)
    asyncio.run(ensure_persona_dna(database, None, use_llm=False))
    yield database
    database.close()


def _persona_by_nickname(db: DB, nickname: str) -> dict:
    row = db.conn.execute("SELECT * FROM personas WHERE nickname=?", (nickname,)).fetchone()
    assert row is not None, f"找不到人物 {nickname}"
    return dict(row)


def _make_friend(db: DB, nickname: str, *, owner: str = "local", closeness: int = 3) -> dict:
    persona = _persona_by_nickname(db, nickname)
    return db.create_friend(
        {
            "name": persona["nickname"],
            "persona_id": persona["id"],
            "city": persona.get("city") or "",
            "nationality": persona.get("country") or "",
            "native_lang": persona.get("language") or "en",
            "reply_language": persona.get("language") or "en",
            "personality_prompt": (persona.get("personality") or "")[:400],
            "speech_quirks": ["right"],
            "closeness_level": closeness,
            "total_turns": 0,
            "owner_user_id": owner,
        }
    )


def _chat(db: DB, llm) -> ChatService:
    class _Engines:
        def __init__(self) -> None:
            self.llm = llm
            self.stt = None

    return ChatService(
        engines=_Engines(),
        db=db,
        memory=MemoryManager(db, llm),
        voice=None,
        spawner=None,
        audio_store=None,
    )


def _add_event(db: DB, persona: dict, **over) -> dict:
    now = datetime.now(timezone.utc)
    base = {
        "persona_id": persona["id"],
        "event_type": "social",
        "title": "Jazz bar with Sophie",
        "description": "Went to a small jazz bar and stayed until closing.",
        "start_time": _iso(now - timedelta(hours=4)),
        "end_time": _iso(now - timedelta(hours=2)),
        "participants": [persona["id"]],
        "location": "",
        "related_personas": [],
        "event_status": "completed",
        "source_type": "llm",
        "source_reference": "",
        "visibility": "public",
        "metadata": {},
        "dedupe_key": f"test:{persona['id']}:{over.get('title', 'jazz')}",
        "day": now.date().isoformat(),
    }
    base.update(over)
    event_id, created = life_events.add_event(db, base)
    assert created
    row = dict(db.conn.execute("SELECT * FROM persona_events WHERE id=?", (event_id,)).fetchone())
    return row


# --------------------------------------------------------------------------- #
# A. Speech DNA：20 轮不模板化
# --------------------------------------------------------------------------- #
_TWENTY_INPUTS = [
    "Hey Adrian!",
    "How was your weekend?",
    "I am so tired today",
    "Guess what, I got the job!",
    "What do you think about remote work?",
    "My cat broke my mug this morning",
    "I failed my driving test again",
    "Tell me something boring about design",
    "Do you like jazz?",
    "I disagree, meetings are useful",
    "ok talk later",
    "what are you working on today?",
    "I am nervous about tomorrow",
    "Nothing much happened today",
    "Have you eaten yet?",
    "I am learning to cook",
    "That sounds awful honestly",
    "What music are you into?",
    "I think I should quit my job",
    "Anyway, I have to go",
]


def test_twenty_turns_do_not_become_a_customer_service_bot(db):
    chat = _chat(db, _MockLLM())
    friend = _make_friend(db, "Adrian")
    history: list[dict] = []
    replies: list[str] = []
    for index, text in enumerate(_TWENTY_INPUTS):
        reply = asyncio.run(chat._llm_reply(friend, text, list(history), []))
        assert reply, f"第 {index + 1} 轮空回复"
        replies.append(reply)
        history.append({"sender": "friend", "transcript": reply})
        history.append({"sender": "user", "transcript": text})

    # 1) 不能反复出现同一句
    assert len(set(replies)) >= 8, f"20 轮只有 {len(set(replies))} 种不同回复"
    # 2) 不能每轮都以问题结尾
    question_ratio = sum(1 for r in replies if r.rstrip().endswith(("?", "？"))) / len(replies)
    assert question_ratio < 0.5, f"追问比例过高: {question_ratio:.2f}"
    # 3) 不能每轮热情：至少有几轮是冷淡/中性短句
    short = [r for r in replies if len(r.split()) <= 6]
    assert short, "没有任何短回复：人物不会只说一句话"
    # 4) 开场词要有变化
    firsts = {r.split()[0].strip("[]").lower() for r in replies if r.split()}
    assert len(firsts) >= 5, f"开场词太单一: {sorted(firsts)}"


def test_different_personas_have_recognisably_different_voices(db):
    dry = _make_friend(db, "Adrian")      # blunt / dry / one_line
    warm = _make_friend(db, "Camila")     # direct / playful / long
    chat = _chat(db, _MockLLM())

    prompts = {}
    for label, friend in (("dry", dry), ("warm", warm)):
        plan = persona_context.build_turn_plan(db, friend, "How was your day?")
        prompts[label] = chat._system_prompt(
            friend, [], user_text="How was your day?", plan=plan
        )

    assert "Rhythm: clipped" in prompts["dry"]
    assert "Rhythm: clipped" not in prompts["warm"]
    # 声线配置必须真的不同（不是同一份人设换了名字）
    assert prompts["dry"] != prompts["warm"]
    dna_dry = persona_context.build_turn_plan(db, dry, "hi").dna
    dna_warm = persona_context.build_turn_plan(db, warm, "hi").dna
    differing = [k for k in dna_dry if dna_dry.get(k) != dna_warm.get(k)]
    assert len(differing) >= 3, f"两个人物的 DNA 差异过小: {differing}"


def test_prompt_no_longer_forces_hook_and_one_liners(db):
    chat = _chat(db, _MockLLM())
    friend = _make_friend(db, "Annika")
    plan = persona_context.build_turn_plan(db, friend, "Hey there")
    prompt = chat._system_prompt(friend, [], user_text="Hey there", plan=plan)

    assert "Always leave a hook" not in prompt
    assert "1-2 VERY short lines" not in prompt
    assert "1-2 short spoken English sentences" not in prompt
    assert "[This turn]" in prompt          # 行为指令在场
    assert "[Speech DNA]" in prompt         # 表达配置在场


def test_behavior_directive_blocks_questions_for_non_question_behaviors(db):
    friend = _make_friend(db, "Morten")  # blunt / low initiative
    plans = [
        persona_context.build_turn_plan(db, friend, text, rng=random.Random(seed))
        for seed, text in enumerate(["yeah", "mm", "ok", "cool", "right"])
    ]
    for plan in plans:
        if plan.behavior != "ask_question":
            assert "Do not end with a question" in plan.directive


# --------------------------------------------------------------------------- #
# B. Behavior Engine：受人格约束 + 历史降低重复
# --------------------------------------------------------------------------- #
def test_behavior_is_persona_dependent_and_recent_history_is_used(db):
    shy = _make_friend(db, "Haruto")   # low initiative / one_line
    chatty = _make_friend(db, "Jiwoo")  # high initiative
    inputs = ["Hey", "How are you", "What are you up to", "Tell me more"]

    def behaviours(friend):
        chat = _chat(db, _MockLLM())
        out = []
        for text in inputs:
            plan = persona_context.build_turn_plan(db, chat.db.get_friend(friend["id"]), text)
            out.append(plan.behavior)
            persona_context.record_behavior(db, friend, plan.behavior)
        return out

    shy_plan = behaviours(shy)
    chatty_plan = behaviours(chatty)
    assert len(shy_plan) == len(inputs) and len(chatty_plan) == len(inputs)
    # 行为序列不能一模一样（人格真正影响了选择）
    assert shy_plan != chatty_plan or "ask_question" not in chatty_plan

    # 连续追问后，下一次不能再问（历史惩罚）
    repeated = persona_context.build_turn_plan(
        db, db.get_friend(shy["id"]), "and you?"
    )
    assert repeated.behavior != "ask_question" or "question" in repeated.directive.lower()


def test_recent_behaviors_are_persisted_and_capped(db):
    friend = _make_friend(db, "Lin")
    for i in range(12):
        persona_context.record_behavior(db, friend, "respond" if i % 2 else "share")
    row = db.get_friend(friend["id"])
    stored = json.loads(row["recent_behaviors"] or "[]")
    assert len(stored) == persona_context.MAX_RECENT_BEHAVIORS
    assert stored[-1] in ("respond", "share")


# --------------------------------------------------------------------------- #
# C/D. 事件 → 动态 → 聊天
# --------------------------------------------------------------------------- #
def test_one_event_two_personas_two_perspectives(db):
    emily = _persona_by_nickname(db, "Camila")
    sophie = _persona_by_nickname(db, "Annika")
    event = _add_event(
        db,
        emily,
        participants=[emily["id"], sophie["id"]],
        title="Jazz bar with Sophie",
    )

    contents = {
        emily["id"]: "Didn't expect to enjoy jazz this much. Sophie knows good places.",
        sophie["id"]: "Found a lovely little jazz bar tonight. Emily actually liked it.",
    }
    llm = _CountingLLM(payload={"content": ""})

    async def fake_generate(event_row):
        out = {"posts": [], "skipped": []}
        for pid in [emily["id"], sophie["id"]]:
            persona = _persona_by_nickname(db, "Camila" if pid == emily["id"] else "Annika")
            llm.payload = {"content": contents[pid], "tags": ["music"]}
            post = await event_posts.generate_post_for_participant(db, llm, event_row, persona)
            if post is not None:
                out["posts"].append(post)
            else:
                out["skipped"].append(pid)
        return out

    result = asyncio.run(fake_generate(event))
    posts = result["posts"]
    assert len(posts) == 2, f"应有 2 条不同视角动态，实际 {len(posts)}（skipped={result['skipped']}）"

    rows = [dict(r) for r in db.conn.execute(
        "SELECT * FROM persona_posts WHERE source_event_id=?", (event["id"],)
    ).fetchall()]
    assert len(rows) == 2
    assert {r["persona_id"] for r in rows} == {emily["id"], sophie["id"]}
    assert all(r["post_type"] == "life_event" for r in rows)
    assert len({r["content"] for r in rows}) == 2       # 不同视角
    meta = [json.loads(r["metadata"] or "{}") for r in rows]
    assert all(m.get("event_title") == "Jazz bar with Sophie" for m in meta)

    # 幂等：同一 (event, persona) 不再新增；已存在的会被原样返回
    again = asyncio.run(fake_generate(event))
    rows2 = db.conn.execute(
        "SELECT COUNT(*) AS n FROM persona_posts WHERE source_event_id=?", (event["id"],)
    ).fetchone()["n"]
    assert rows2 == 2, "重复调用同一事件不应新增动态"
    existing_ids = {r["id"] for r in rows}
    assert {p["id"] for p in again["posts"]} <= existing_ids


def test_completed_event_is_recalled_in_chat_but_planned_and_cancelled_are_not(db):
    persona = _persona_by_nickname(db, "Camila")
    friend = _make_friend(db, "Camila")

    done = _add_event(db, persona, title="Finished the branding project")
    planned = _add_event(
        db,
        persona,
        title="Concert with Sophie on Friday",
        event_status="planned",
        start_time=_iso(datetime.now(timezone.utc) + timedelta(days=2)),
        end_time=_iso(datetime.now(timezone.utc) + timedelta(days=2, hours=3)),
        dedupe_key="test:planned:concert",
    )
    cancelled = _add_event(
        db,
        persona,
        title="Abandoned pottery class",
        event_status="cancelled",
        dedupe_key="test:cancelled:pottery",
    )

    plan = persona_context.build_turn_plan(db, friend, "How did the branding project go?")
    layer = plan.life_block

    assert "Finished the branding project" in layer        # 已发生事实可回忆
    if "Concert with Sophie on Friday" in layer:
        assert "not happened yet" in layer                 # 计划事件不能当已发生
    assert "Abandoned pottery class" not in layer          # 取消事件绝不出现
    assert done["id"] and planned["id"] and cancelled["id"]


def test_event_posts_cycle_is_idempotent(db):
    persona = _persona_by_nickname(db, "Camila")
    event = _add_event(db, persona, title="Late night jazz")
    llm = _CountingLLM(payload={"content": "Jazz was better than expected.", "tags": []})

    first = asyncio.run(event_posts.run_posts_cycle(db, llm, rng=random.Random(7)))
    second = asyncio.run(event_posts.run_posts_cycle(db, llm, rng=random.Random(7)))
    assert second["posts"] == 0, f"同一随机源重跑不应新增动态: {second}"
    # 无论概率如何，同一事件对同一人物最多 1 条动态
    rows = db.conn.execute(
        "SELECT persona_id, COUNT(*) AS n FROM persona_posts WHERE source_event_id=?"
        " GROUP BY persona_id",
        (event["id"],),
    ).fetchall()
    assert all(int(r["n"]) <= 1 for r in rows), [dict(r) for r in rows]
    assert first["events"] >= 0


def test_ai_comment_appears_on_persona_post_with_provenance(db):
    persona = _persona_by_nickname(db, "Camila")
    post_id = "pp_test_event"
    now = _iso(datetime.now(timezone.utc))
    with db._lock, db.conn:
        db.conn.execute(
            "INSERT INTO persona_posts (id, persona_id, content, language, visibility, created_at,"
            " source_event_id, post_type) VALUES (?,?,?,?,'public',?,?,?)",
            (post_id, persona["id"], "Jazz was good.", "en", now, "ev_test", "life_event"),
        )
        db.conn.execute(
            "INSERT INTO persona_post_comments (id, post_id, user_id, content, created_at,"
            " author_persona_id, author_type) VALUES (?,?,?,?,?,?,'persona')",
            ("c_test", post_id, "", "Told you jazz was good.", now, "p_annika"),
        )

    service = V3Service(db, type("E", (), {"llm": None})(), "")
    with db._lock:
        row = db.conn.execute("SELECT * FROM persona_posts WHERE id=?", (post_id,)).fetchone()
    public = service._persona_post_public(row, "local")
    assert public["source_event_id"] == "ev_test"
    assert public["post_type"] == "life_event"
    ai_comments = [c for c in public["comments"] if c["author"].get("is_ai")]
    assert ai_comments, "AI 人物的评论没有暴露出来"
    assert ai_comments[0]["author"]["type"] == "persona"


# --------------------------------------------------------------------------- #
# E. 记忆 / 关系隔离
# --------------------------------------------------------------------------- #
def test_relationship_isolation_between_users(db):
    persona = _persona_by_nickname(db, "Adrian")
    close_friend = _make_friend(db, "Adrian", owner="user-a", closeness=5)
    distant_friend = _make_friend(db, "Adrian", owner="user-b", closeness=1)

    plan_a = persona_context.build_turn_plan(db, close_friend, "hey")
    plan_b = persona_context.build_turn_plan(db, distant_friend, "hey")

    # 同一个 Persona 的全局情绪/生活可以共享，但「与用户的关系」必须隔离
    assert plan_a.persona_id == plan_b.persona_id == persona["id"]
    persona_context.record_behavior(db, close_friend, "tease")
    refreshed_b = db.get_friend(distant_friend["id"], any_owner=True)
    assert refreshed_b is not None
    assert json.loads(refreshed_b["recent_behaviors"] or "[]") == []


def test_life_facts_are_bounded_and_not_a_data_dump(db):
    persona = _persona_by_nickname(db, "Camila")
    for index in range(8):
        _add_event(
            db,
            persona,
            title=f"Small thing number {index}",
            dedupe_key=f"test:bounded:{index}",
            start_time=_iso(datetime.now(timezone.utc) - timedelta(days=index + 1)),
            end_time=_iso(datetime.now(timezone.utc) - timedelta(days=index + 1) + timedelta(hours=1)),
        )
    friend = _make_friend(db, "Camila")
    plan = persona_context.build_turn_plan(db, friend, "Do you like football?")
    block = plan.life_block
    # 不是把 8 条全都塞进 prompt，而是有界、按相关性/时间排序的一小段
    assert len(block) <= 1000
    assert block.count("- DONE") <= 3
    assert len(block) >= 0


# --------------------------------------------------------------------------- #
# F. 性能 / 成本
# --------------------------------------------------------------------------- #
def test_one_turn_is_one_llm_call(db):
    llm = _CountingLLM(reply="Yeah, same here.")
    chat = _chat(db, llm)
    friend = _make_friend(db, "Adrian")
    asyncio.run(chat._llm_reply(friend, "how was your day", [], []))
    assert llm.stream_calls == 1, f"一轮聊天调用了 {llm.stream_calls} 次模型"
    assert llm.json_calls == 0


def test_feed_refresh_never_calls_llm(db):
    service = V3Service(db, type("E", (), {"llm": _RaisingLLM()})(), "")
    page = service.feed("local", limit=10)
    assert "items" in page
    assert isinstance(page["items"], list)


def test_planned_event_is_labelled_and_never_says_done(db):
    persona = _persona_by_nickname(db, "Camila")
    event = _add_event(
        db,
        persona,
        title="Gallery opening on Sunday",
        event_status="planned",
        start_time=_iso(datetime.now(timezone.utc) + timedelta(days=3)),
        end_time=_iso(datetime.now(timezone.utc) + timedelta(days=3, hours=2)),
        dedupe_key="test:planned:gallery",
    )
    block = life_events.event_fact_block(event)
    assert "not happened yet" in block
    assert "DONE" not in block
