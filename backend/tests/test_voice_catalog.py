from __future__ import annotations

import asyncio
import pytest

from app.v2.db import DB
from app.v3.schema import ensure_schema
from app.voice.catalog import repair_persona_voices, voice_gaps


def test_repair_preserves_first_owner_and_waits_when_catalog_exhausted(tmp_path):
    db = DB(str(tmp_path / "voices.db"))
    db.init()
    ensure_schema(db, seed=False)
    with db._lock, db.conn:
        for pid in ("p_first", "p_second", "p_third"):
            db.conn.execute(
                "INSERT INTO personas(id,nickname,language,voice_id,status,created_at) VALUES (?,?,?,'ja-JP-KeitaNeural','active',?)",
                (pid, pid, "ja", pid),
            )
    repair_persona_voices(db)
    rows = {r["id"]: dict(r) for r in db.conn.execute("SELECT * FROM personas")}
    assert rows["p_first"]["voice_id"] == "ja-JP-KeitaNeural"
    assert rows["p_second"]["voice_id"] == "ja-JP-NanamiNeural"
    assert rows["p_third"]["status"] == "waiting_voice"
    assert voice_gaps(db)["waiting_voice"] == 1
    assert repair_persona_voices(db) == 0
    db.close()


def test_strict_daily_generation_keeps_failed_verification_waiting(tmp_path, monkeypatch):
    from app.v3.daily_world import _insert_persona
    from app.voice import catalog

    db = DB(str(tmp_path / "waiting.db"))
    db.init()
    ensure_schema(db, seed=False)
    monkeypatch.setenv("SE_VOICE_STRICT", "1")

    async def unavailable(*args, **kwargs):
        return False

    monkeypatch.setattr(catalog, "verify_voice", unavailable)
    pid, written = asyncio.run(_insert_persona(
        db, {"nickname": "Waiting", "language": "en", "posts": [{"content": "A quiet day."}]},
        day="2026-11-09", expires_at="2099-01-01T00:00:00+00:00",
    ))
    row = db.conn.execute("SELECT status FROM personas WHERE id=?", (pid,)).fetchone()
    assert row[0] == "waiting_voice"
    assert written == 1
    assert db.conn.execute("SELECT visibility FROM persona_posts WHERE persona_id=?", (pid,)).fetchone()[0] == "waiting_voice"
    db.close()


def test_waiting_persona_can_be_activated_after_verified_recovery(tmp_path, monkeypatch):
    from app.voice import catalog

    db = DB(str(tmp_path / "recover.db"))
    db.init()
    ensure_schema(db, seed=False)
    with db._lock, db.conn:
        db.conn.execute("INSERT INTO personas(id,nickname,language,status) VALUES ('p_wait','Waiting','ja','waiting_voice')")

    async def ready(*args, **kwargs):
        return True

    monkeypatch.setattr(catalog, "verify_voice", ready)
    assert asyncio.run(catalog.ensure_ready_personas(db)) == 1
    row = db.conn.execute("SELECT status,voice_id FROM personas WHERE id='p_wait'").fetchone()
    assert tuple(row) == ("active", "ja-JP-KeitaNeural")
    assert asyncio.run(catalog.ensure_ready_personas(db)) == 0
    db.close()


def test_catalog_allocation_keeps_personas_gender_and_language(tmp_path):
    from app.v3.daily_world import _pick_edge_voice

    db = DB(str(tmp_path / "gender.db"))
    db.init()
    ensure_schema(db, seed=False)
    assert _pick_edge_voice(db, "ja", "female") == "ja-JP-NanamiNeural"
    assert _pick_edge_voice(db, "ja", "male") == "ja-JP-KeitaNeural"
    db.close()


def test_strict_repair_preserves_distinct_fish_voices_with_shared_unused_edge(tmp_path, monkeypatch):
    db = DB(str(tmp_path / "fish-preserve.db"))
    db.init()
    ensure_schema(db, seed=False)
    monkeypatch.setenv("SE_VOICE_STRICT", "1")
    with db._lock, db.conn:
        for index in range(2):
            db.conn.execute(
                "INSERT INTO personas(id,nickname,status,language,voice_id,fish_voice_id) "
                "VALUES (?,?,'active','ja','ja-JP-KeitaNeural',?)",
                (f"p_{index}", f"P{index}", f"fish-distinct-{index}"),
            )
    repair_persona_voices(db)
    rows = list(db.conn.execute("SELECT status,fish_voice_id FROM personas ORDER BY id"))
    assert [tuple(r) for r in rows] == [("active", "fish-distinct-0"), ("active", "fish-distinct-1")]
    db.close()


def test_standalone_waiting_friend_recovers_original_request_status(tmp_path, monkeypatch):
    from app.voice import catalog

    db = DB(str(tmp_path / "friend-recover.db"))
    db.init()
    ensure_schema(db, seed=False)
    friend = db.create_friend({"name": "Waiting", "status": "waiting_voice", "voice_pending_status": "pending_request",
                               "voice_id": "ja-JP-NanamiNeural", "reply_language": "ja", "gender": "female"})

    async def ready(*args, **kwargs):
        return True

    monkeypatch.setattr(catalog, "verify_voice", ready)
    assert asyncio.run(catalog.ensure_ready_friends(db)) == 1
    assert db.get_friend(friend["id"])["status"] == "pending_request"
    assert asyncio.run(catalog.ensure_ready_friends(db)) == 0
    db.close()


@pytest.mark.parametrize("fish", [False, True])
def test_older_waiting_persona_never_steals_established_active_binding(monkeypatch, fish):
    db = DB(":memory:")
    db.init()
    ensure_schema(db, seed=False)
    monkeypatch.setenv("SE_VOICE_STRICT", "1")
    with db._lock, db.conn:
        for pid, status, created in (("p_wait", "waiting_voice", "2020"), ("p_owner", "active", "2021")):
            db.conn.execute(
                "INSERT INTO personas(id,nickname,status,language,gender,voice_id,fish_voice_id,created_at) "
                "VALUES (?,?,?,'ja','male','ja-JP-KeitaNeural',?,?)",
                (pid, pid, status, "existing-fish" if fish else "", created),
            )
    repair_persona_voices(db)
    owner = db.conn.execute("SELECT status,voice_id,fish_voice_id FROM personas WHERE id='p_owner'").fetchone()
    assert tuple(owner) == ("active", "ja-JP-KeitaNeural", "existing-fish" if fish else "")
    assert db.conn.execute("SELECT status FROM personas WHERE id='p_wait'").fetchone()[0] == "waiting_voice"
    assert repair_persona_voices(db) == 0
    db.close()


@pytest.mark.parametrize("kind", ["personas", "friends"])
def test_readiness_batches_rotate_persistently_past_unavailable_oldest_roles(tmp_path, monkeypatch, kind):
    from app.voice import catalog

    current_time = [catalog.time.time()]
    monkeypatch.setattr(catalog.time, "time", lambda: current_time[0])
    path = str(tmp_path / f"rotate-{kind}.db")
    db = DB(path)
    db.init()
    ensure_schema(db, seed=False)
    with db._lock, db.conn:
        for index in range(4):
            if kind == "personas":
                db.conn.execute(
                    "INSERT INTO personas(id,nickname,status,language,fish_voice_id,created_at) "
                    "VALUES (?,?,'waiting_voice','ja',?,?)",
                    (f"p_{index}", f"P{index}", f"voice-{index}", str(index)),
                )
            else:
                db.create_friend({"id": f"f_{index}", "name": f"F{index}", "status": "waiting_voice",
                                  "fish_voice_id": f"voice-{index}", "created_at": str(index)})
    attempted = []

    async def available_fourth(db, provider, reference, **kwargs):
        attempted.append(reference)
        return reference == "voice-3"

    monkeypatch.setattr(catalog, "verify_voice", available_fourth)
    recover = catalog.ensure_ready_personas if kind == "personas" else catalog.ensure_ready_friends
    assert asyncio.run(recover(db, limit=3)) == 0
    assert attempted == ["voice-0", "voice-1", "voice-2"]
    db.close()
    # Failed-attempt ordering and cooldown survive worker/database restart.
    db = DB(path)
    db.init()
    ensure_schema(db, seed=False)
    assert asyncio.run(recover(db, limit=3)) == 1
    assert attempted == ["voice-0", "voice-1", "voice-2", "voice-3"]
    assert asyncio.run(recover(db, limit=3)) == 0
    assert len(attempted) == 4
    current_time[0] += 31
    assert asyncio.run(recover(db, limit=3)) == 0
    assert attempted[4:] == ["voice-0", "voice-1", "voice-2"]
    attempts = db.conn.execute(
        "SELECT attempts,last_attempt,retry_after FROM voice_readiness_attempts WHERE entity_kind=?",
        (kind,),
    ).fetchall()
    assert len(attempts) == 3
    assert all(tuple(row) == (2, current_time[0], current_time[0] + 60) for row in attempts)
    current_time[0] += 31
    assert asyncio.run(recover(db, limit=3)) == 0
    assert len(attempted) == 7
    db.close()
