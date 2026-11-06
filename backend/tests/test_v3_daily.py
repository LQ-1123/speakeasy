"""每日世界 / 生命周期测试。"""

from __future__ import annotations

import asyncio
import random
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.v2.db import DB  # noqa: E402
from app.v3.daily_world import (  # noqa: E402
    DAILY_PERSONAS,
    POSTS_PER_PERSONA,
    daily_status,
    generate_daily,
    iso,
    now_local,
)
from app.v3.lifecycle import cleanup, mark_interaction  # noqa: E402
from app.v3.router import router as v3_router  # noqa: E402
from app.v3.schema import ensure_schema  # noqa: E402
from app.v3.service import V3Service  # noqa: E402


@pytest.fixture()
def daily_db(tmp_path):
    db = DB(str(tmp_path / "daily.db"))
    db.init()
    ensure_schema(db, seed=False)
    yield db
    db.close()


def _make_app(db, tmp_path: Path):
    mock = SimpleNamespace(uses_mock=True)
    service = V3Service(
        db,
        SimpleNamespace(llm=mock, voice=SimpleNamespace(voice_id="voice")),
        str(tmp_path),
    )
    service.rng = random.Random(7)
    services = SimpleNamespace(
        db=db,
        llm=mock,
        engines=SimpleNamespace(llm=mock),
        voice=SimpleNamespace(voice_id="voice"),
        onboarding_pending=False,
        followup_inflight=set(),
        spawn_bg=lambda coro: (coro.close(), None)[1],
    )
    app = FastAPI()
    app.state.v2 = services
    app.state.v3 = service
    app.include_router(v3_router)
    return app, service


def _register(client: TestClient, device: str) -> dict:
    data = client.post("/api/v3/auth/register", json={"device_key": device}).json()
    return {"X-User-Id": data["user_id"], "X-User-Token": data["token"]}


def test_generate_daily_is_idempotent(daily_db):
    first = asyncio.run(generate_daily(daily_db, None, day="2026-11-06"))
    assert first["skipped"] is False
    assert first["personas"] == DAILY_PERSONAS
    assert first["posts"] == DAILY_PERSONAS * POSTS_PER_PERSONA

    guests = daily_db.conn.execute(
        "SELECT * FROM personas WHERE lifecycle='guest'"
    ).fetchall()
    assert len(guests) == DAILY_PERSONAS
    assert all(row["expires_at"] for row in guests)
    posts = daily_db.conn.execute(
        "SELECT * FROM persona_posts WHERE persona_id LIKE 'p_%'"
    ).fetchall()
    assert len(posts) == DAILY_PERSONAS * POSTS_PER_PERSONA
    assert all(row["expires_at"] for row in posts)

    again = asyncio.run(generate_daily(daily_db, None, day="2026-11-06"))
    assert again["skipped"] is True
    status = daily_status(daily_db)
    assert status["visible_posts"] >= DAILY_PERSONAS * POSTS_PER_PERSONA


def test_cleanup_keeps_interacted_and_recycles_guests(daily_db):
    asyncio.run(generate_daily(daily_db, None, day="2026-11-06"))
    posts = daily_db.conn.execute("SELECT * FROM persona_posts").fetchall()
    keep = posts[0]
    drop = posts[1]
    persona_id = str(keep["persona_id"])
    mark_interaction(daily_db, post_id=str(keep["id"]))
    # 人为把所有内容置为过期
    past = iso(now_local() - timedelta(hours=1))
    with daily_db._lock, daily_db.conn:
        daily_db.conn.execute("UPDATE persona_posts SET expires_at=?", (past,))
        daily_db.conn.execute("UPDATE personas SET expires_at=? WHERE lifecycle='guest'", (past,))

    result = cleanup(daily_db)
    assert result["deleted_posts"] >= 1
    # 被互动的动态被延长（仍存在）；未互动的被删
    assert daily_db.conn.execute(
        "SELECT COUNT(*) AS n FROM persona_posts WHERE id=?", (keep["id"],)
    ).fetchone()["n"] == 1
    assert daily_db.conn.execute(
        "SELECT COUNT(*) AS n FROM persona_posts WHERE id=?", (drop["id"],)
    ).fetchone()["n"] == 0
    # 被互动的游客人物转正（adopted），不会被回收
    row = daily_db.conn.execute(
        "SELECT lifecycle, interaction_count FROM personas WHERE id=?", (persona_id,)
    ).fetchone()
    assert row is not None and row["lifecycle"] == "adopted" and row["interaction_count"] >= 1


def test_feed_hides_expired_posts_and_pages_by_10(daily_db, tmp_path):
    asyncio.run(generate_daily(daily_db, None, day="2026-11-06"))
    app, _service = _make_app(daily_db, tmp_path)
    with TestClient(app) as client:
        headers = _register(client, "device_daily_feed")
        page = client.get("/api/v3/feed", headers=headers).json()
        assert len(page["items"]) == 10
        assert page["next_cursor"]

        # 把这一页的动态全部置为过期 → feed 应为空
        past = iso(now_local() - timedelta(hours=1))
        with daily_db._lock, daily_db.conn:
            daily_db.conn.execute("UPDATE persona_posts SET expires_at=?", (past,))
        empty = client.get("/api/v3/feed", headers=headers).json()
        assert empty["items"] == []

        daily = client.get("/api/v3/world/daily").json()
        assert daily["generated"] is True
        assert daily["config"]["personas_per_day"] == DAILY_PERSONAS
