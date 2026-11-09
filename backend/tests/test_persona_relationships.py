"""第三阶段验收：关系张力在聊天 / Persona 主页 / 观测端点里的表现。

覆盖：
- Persona 主页返回 `relations`（≤3 条、字段齐全、不含内部 ID 之外的东西）；
- 聊天 Persona 层出现 `[Your people]`，有摩擦的人优先，且整体有字符预算；
- 观测端点暴露 avg_tension / tense_pairs；
- 这些读取路径**不触发任何 LLM**。

关系写入（tension 变化）由 life_events 的测试覆盖；这里只验证「读取侧」把张力用对了。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.v2.chat_service import ChatService
from app.v2.db import DB
from app.v2.memory import MemoryManager
from app.v3 import persona_context
from app.v3.router import router as v3_router
from app.v3.schema import ensure_schema
from app.v3.service import V3Service
from app.v3.speech_dna import ensure_persona_dna


class _RaisingLLM:
    uses_mock = False

    async def complete_json(self, *a, **kw):
        raise AssertionError("这条链路不应该调用 LLM")

    async def stream_chat(self, *a, **kw):  # pragma: no cover
        raise AssertionError("这条链路不应该调用 LLM")
        yield ""


class _MockLLM:
    uses_mock = True

    async def complete_json(self, *a, **kw):  # pragma: no cover
        raise AssertionError("mock 模式不应调用 complete_json")

    async def stream_chat(self, *a, **kw):  # pragma: no cover
        raise AssertionError("mock 模式不应调用 stream_chat")
        yield ""


@pytest.fixture()
def db(tmp_path):
    database = DB(str(tmp_path / "relations.db"))
    database.init()
    ensure_schema(database, seed=True)
    asyncio.run(ensure_persona_dna(database, None, use_llm=False))
    yield database
    database.close()


def _persona(db: DB, nickname: str) -> dict:
    row = db.conn.execute("SELECT * FROM personas WHERE nickname=?", (nickname,)).fetchone()
    assert row is not None, nickname
    return dict(row)


def _friend(db: DB, nickname: str) -> dict:
    persona = _persona(db, nickname)
    return db.create_friend(
        {
            "name": persona["nickname"],
            "persona_id": persona["id"],
            "city": persona.get("city") or "",
            "nationality": persona.get("country") or "",
            "native_lang": persona.get("language") or "en",
            "reply_language": persona.get("language") or "en",
            "personality_prompt": (persona.get("personality") or "")[:400],
            "closeness_level": 3,
            "total_turns": 30,
        }
    )


def _add_relation(
    db: DB,
    a: dict,
    b: dict,
    *,
    closeness: int = 3,
    tension: int = 0,
    shared: int = 2,
) -> None:
    now = "2026-11-07T14:00:00+00:00"
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO persona_relationships"
            " (id, persona_a, persona_b, relation_type, closeness, shared_events,"
            "  last_shared_at, tension, last_conflict_at, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"pr_{uuid.uuid4().hex[:10]}",
                a["id"],
                b["id"],
                "friend",
                closeness,
                shared,
                now if shared else "",
                tension,
                now if tension else "",
                now,
                now,
            ),
        )


def test_persona_profile_exposes_relations_without_internal_ids(db):
    adrian = _persona(db, "Adrian")
    for index, nickname in enumerate(("Camila", "Finn", "Giulia", "Morten")):
        _add_relation(db, adrian, _persona(db, nickname), closeness=3 + (index % 2), shared=index)
    service = V3Service(db, SimpleNamespace(llm=_RaisingLLM()), "")

    profile = service.persona_profile(adrian["id"], "local")
    assert profile is not None
    relations = profile.get("relations")
    assert isinstance(relations, list) and 0 < len(relations) <= 3

    first = relations[0]
    assert first["nickname"]
    assert first["persona_id"] and first["persona_id"] != adrian["id"]
    for key in ("relation_type", "closeness", "tension", "shared_events"):
        assert key in first
    # 主页是给人看的：不要出现内部事件 id / 内部字段名
    dumped = json.dumps(relations, ensure_ascii=False)
    assert "dedupe_key" not in dumped
    assert "pe_" not in dumped


def test_people_lines_prioritise_tension_and_stay_bounded(db):
    adrian = _persona(db, "Adrian")
    _add_relation(db, adrian, _persona(db, "Callum"), closeness=4, tension=4, shared=3)
    _add_relation(db, adrian, _persona(db, "Camila"), closeness=5, tension=0, shared=5)
    _add_relation(db, adrian, _persona(db, "Giulia"), closeness=4, tension=0, shared=2)

    lines = persona_context._people_lines(db, adrian["id"])
    assert lines, "有关系证据时应该至少返回一行"
    assert len(lines) <= persona_context.MAX_PEOPLE_LINES
    assert sum(len(line) for line in lines) <= persona_context.MAX_PEOPLE_CHARS
    joined = "\n".join(lines)
    # 有摩擦的人优先出现
    assert "Callum" in joined
    assert "tense" in joined.lower()
    # 不向模型暴露内部数字
    assert "closeness" not in joined.lower() and "tension:" not in joined.lower()
    assert "4" not in joined.replace("Callum", "")


def test_persona_layer_contains_people_block(db):
    adrian = _persona(db, "Adrian")
    _add_relation(db, adrian, _persona(db, "Camila"), closeness=5, tension=0, shared=4)
    friend = _friend(db, "Adrian")
    chat = ChatService(
        engines=SimpleNamespace(llm=_MockLLM(), stt=None),
        db=db,
        memory=MemoryManager(db, _MockLLM()),
        voice=None,
        spawner=None,
        audio_store=None,
    )
    plan = persona_context.build_turn_plan(db, friend, "hey, what's up?")
    prompt = chat._system_prompt(friend, [], user_text="hey, what's up?", plan=plan)
    assert "[Your people" in prompt
    assert "Camila" in prompt


def test_people_block_absent_when_no_qualifying_relation(db):
    adrian = _persona(db, "Adrian")
    # 只有普通关系（closeness 2、无张力）→ 不应该把 11 个熟人全塞进 prompt
    _add_relation(db, adrian, _persona(db, "Finn"), closeness=2, tension=0, shared=1)
    friend = _friend(db, "Adrian")
    plan = persona_context.build_turn_plan(db, friend, "hi")
    layer = persona_context.build_persona_layer(db, friend, plan, language="en")
    assert "[Your people" not in layer


def test_stats_persona_reports_tension(db):
    adrian = _persona(db, "Adrian")
    _add_relation(db, adrian, _persona(db, "Callum"), closeness=3, tension=4, shared=2)
    _add_relation(db, adrian, _persona(db, "Camila"), closeness=4, tension=0, shared=3)

    service = V3Service(db, SimpleNamespace(llm=_RaisingLLM()), "")
    stats = service.stats_persona()
    relationships = stats["relationships"]
    assert relationships["total"] >= 2
    assert relationships["tense_pairs"] >= 1
    assert 0 <= relationships["avg_tension"] <= 5

    # 端点同样不触发 LLM
    services = SimpleNamespace(
        db=db,
        onboarding_pending=False,
        followup_inflight=set(),
        spawn_bg=lambda coro: (coro.close(), None)[1],
    )
    app = FastAPI()
    app.state.v2 = services
    app.state.v3 = service
    app.include_router(v3_router)
    with TestClient(app) as client:
        response = client.get("/api/v3/stats/persona")
    assert response.status_code == 200
    assert "avg_tension" in response.json()["relationships"]
