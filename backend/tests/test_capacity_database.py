"""Offline capacity regressions using disposable SQLite databases."""
from datetime import datetime, timedelta, timezone
import json
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from app.v2.db import DB
from app.v3 import event_posts
from app.v3.identity import set_current_user, reset_current_user
from app.v3.schema import ensure_schema


@pytest.fixture
def db(tmp_path):
    database = DB(str(tmp_path / "capacity.db"))
    database.init()
    ensure_schema(database, seed=False)
    yield database
    database.close()


def event(db, eid, *, owner="owner", participants=None, status="completed", when=None):
    when = when or datetime(2026, 11, 10, tzinfo=timezone.utc)
    timestamp = when.isoformat()
    with db.conn:
        db.conn.execute(
            "INSERT INTO persona_events (id,persona_id,event_type,title,start_time,participants,"
            "event_status,dedupe_key,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (eid, owner, "daily_routine", eid, timestamp, json.dumps(participants or []),
             status, eid, timestamp, timestamp),
        )


def test_completed_lookup_filters_status_and_exact_members_before_limit(db):
    now = datetime(2026, 11, 14, tzinfo=timezone.utc)
    event(db, "real", participants=["p_em"])
    for index in range(90):
        event(db, f"noise-{index}", participants=["p_emily"], when=now-timedelta(hours=1))
        event(db, f"planned-{index}", participants=["p_em"], status="planned", when=now)
    assert [row["id"] for row in event_posts._events_for_persona(db, "p_em", now=now)] == ["real"]


def test_event_membership_tracks_all_sql_mutations_and_timestamp_offsets(db):
    now = datetime(2026, 11, 14, tzinfo=timezone.utc)
    event(db, "boundary", participants=["p_em"], when=now-timedelta(days=7))
    event(db, "old", participants=["p_em"], when=now-timedelta(days=7, seconds=1))
    event(db, "offset", participants=["p_em"], when=datetime.fromisoformat("2026-11-14T07:30:00+08:00"))
    assert {r["id"] for r in event_posts._events_for_persona(db, "p_em", now=now)} == {"boundary", "offset"}
    with db.conn:
        db.conn.execute("UPDATE persona_events SET persona_id='p_new',participants='[\"p_new\"]',event_status='planned',start_time='2026-11-15T00:00:00Z' WHERE id='boundary'")
        db.conn.execute("DELETE FROM persona_events WHERE id='offset'")
    assert event_posts._events_for_persona(db, "p_em", now=now) == []
    assert [r["id"] for r in event_posts._planned_events_for_persona(db, "p_new", now=now)] == ["boundary"]
    assert db.conn.execute("SELECT COUNT(*) FROM persona_event_participants WHERE event_id='offset'").fetchone()[0] == 0


def test_membership_migration_backfills_once_on_old_database(db):
    # Simulate the previous schema, including malformed legacy JSON.
    for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='persona_events'").fetchall():
        db.conn.execute(f'DROP TRIGGER "{row[0]}"')
    db.conn.execute("DROP TABLE IF EXISTS persona_event_participants")
    db.conn.execute("DELETE FROM v3_meta WHERE key='event_membership_v1'")
    event(db, "legacy", participants=["p_em", "p_em"])
    with db.conn:
        db.conn.execute("UPDATE persona_events SET participants='broken' WHERE id='legacy'")
    db.close()
    db.init()
    ensure_schema(db, seed=False)
    assert db.conn.execute("SELECT persona_id FROM persona_event_participants WHERE event_id='legacy'").fetchone()[0] == "owner"
    traced = []
    db.conn.set_trace_callback(traced.append)
    ensure_schema(db, seed=False)
    event_posts._events_for_persona(db, "owner")
    db.conn.set_trace_callback(None)
    assert not any("INSERT" in sql.upper() or "PRAGMA TABLE_INFO" in sql.upper() for sql in traced)


def test_atomic_start_is_idempotent_and_calibrates_history_without_count(db):
    db.create_friend({"id": "friend", "name": "Friend", "total_turns": 999})
    for index in range(9):
        db.add_message("friend", "user", f"history-{index}", msg_type="text")
    db.add_message("friend", "friend", "ad", msg_type="ad")
    db.add_expression("better", "explanation")
    traced = []
    db.conn.set_trace_callback(traced.append)
    first = db.start_chat_turn("friend", "new", client_message_id="retry")
    second = db.start_chat_turn("friend", "new", client_message_id="retry")
    db.conn.set_trace_callback(None)
    assert first["inserted"] is True and second["inserted"] is False
    assert first["user_message"]["id"] == second["user_message"]["id"]
    assert first["total_turns"] == second["total_turns"] == 10
    assert first["closeness_level"] == 2
    assert len(first["history"]) == 9
    assert all(row["msg_type"] != "ad" for row in first["history"])
    assert len(first["review_expressions"]) == 1
    assert not any("COUNT(" in sql.upper() for sql in traced)
    call = db.start_chat_turn("friend", "call", record_message=False)
    assert call["user_message"] is None and call["total_turns"] == 10


def test_start_and_batch_metadata_enforce_owner_and_preserve_latest_ties(db):
    db.create_friend({"id": "mine", "name": "Mine"})
    db.create_friend({"id": "other", "name": "Other", "owner_user_id": "someone"})
    db.add_message("mine", "friend", "first", is_read=False, created_at="2026-11-14T00:00:00Z")
    latest = db.add_message("mine", "friend", "latest", is_read=False, created_at="2026-11-14T00:00:00Z")
    assert db.friend_message_metadata(["mine", "other"] ) == {"mine": {"unread": 2, "last_message": latest}}
    token = set_current_user("someone")
    try:
        with pytest.raises(KeyError):
            db.start_chat_turn("mine", "forbidden")
        assert db.friend_message_metadata(["mine"]) == {}
    finally:
        reset_current_user(token)
    assert db.count_turns("mine") == 0


def test_audio_status_round_trips_and_updates(db):
    db.create_friend({"id": "friend", "name": "Friend"})
    msg = db.add_message("friend", "friend", "hello", audio_status="queued")
    assert msg["audio_status"] == "queued"
    msg = db.update_message(msg["id"], audio_status="failed", audio_error="timeout")
    assert msg["audio_status"] == "failed" and msg["audio_error"] == "timeout"


def test_turn_count_migration_reconciles_legacy_and_tracks_delete_and_move(db):
    db.create_friend({"id": "friend", "name": "Friend", "total_turns": 900})
    db.create_friend({"id": "destination", "name": "Destination"})
    first = db.add_message("friend", "user", "first")
    second = db.add_message("friend", "user", "second")
    for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='messages'").fetchall():
        db.conn.execute(f'DROP TRIGGER "{row[0]}"')
    with db.conn:
        db.conn.execute("DROP TABLE friend_message_counts")
        db.conn.execute("DELETE FROM db_migrations WHERE key='message_counts_v1'")
    db.close()
    db.init()
    started = db.start_chat_turn("friend", "third")
    assert started["total_turns"] == 3
    with db.conn:
        db.conn.execute("DELETE FROM messages WHERE id=?", (first["id"],))
        db.conn.execute("UPDATE messages SET friend_id='destination' WHERE id=?", (second["id"],))
    assert db.start_chat_turn("friend", "call", record_message=False)["total_turns"] == 1
    assert db.start_chat_turn("destination", "call", record_message=False)["total_turns"] == 1
    db.close()
    db.init()
    assert db.start_chat_turn("friend", "call", record_message=False)["total_turns"] == 1


def test_start_rolls_back_message_and_counter_if_transaction_fails(db):
    db.create_friend({"id": "friend", "name": "Friend"})
    db.conn.execute("CREATE TRIGGER fail_turn BEFORE UPDATE ON friends BEGIN SELECT RAISE(ABORT,'test failure'); END")
    with pytest.raises(Exception, match="test failure"):
        db.start_chat_turn("friend", "failed", client_message_id="retry")
    assert db.get_message_by_client_id("friend", "retry") is None
    assert db.count_turns("friend") == 0
    db.conn.execute("DROP TRIGGER fail_turn")
    assert db.start_chat_turn("friend", "retry", client_message_id="retry")["total_turns"] == 1


def test_same_turn_on_separate_connections_inserts_once(db):
    db.create_friend({"id": "friend", "name": "Friend"})
    other = DB(db.path)
    other.init()
    barrier = threading.Barrier(2)

    def start(connection):
        barrier.wait(timeout=5)
        return connection.start_chat_turn("friend", "concurrent", client_message_id="same")

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(start, [db, other]))
        assert sorted(result["inserted"] for result in results) == [False, True]
        assert len({result["user_message"]["id"] for result in results}) == 1
        assert [result["total_turns"] for result in results] == [1, 1]
        assert db.count_turns("friend") == 1
    finally:
        other.close()


def test_recent_event_queries_do_bounded_work_with_large_global_history(db):
    with db.conn:
        db.conn.executemany(
            "INSERT INTO persona_events (id,persona_id,event_type,title,start_time,participants,"
            "event_status,dedupe_key,created_at,updated_at) VALUES (?, 'other', 'daily_routine',"
            "'noise','2026-11-13T00:00:00Z','[\"p_emily\"]','completed',?,'','')",
            [(f"history-{index}", f"history-{index}") for index in range(10000)],
        )
    event(db, "wanted", participants=["p_em"])
    statements = []
    db.conn.set_trace_callback(statements.append)
    # A global scan would exceed this budget even though almost all events belong
    # to another persona. Indexed lookups execute a few hundred VM operations.
    db.conn.set_progress_handler(lambda: 1, 5000)
    try:
        rows = event_posts._events_for_persona(db, "p_em", now=datetime(2026, 11, 14, tzinfo=timezone.utc))
        assert [row["id"] for row in rows] == ["wanted"]
    finally:
        db.conn.set_progress_handler(None, 0)
        db.conn.set_trace_callback(None)
    query = next(sql for sql in statements if sql.lstrip().upper().startswith("SELECT"))
    plan = [row[3] for row in db.conn.execute("EXPLAIN QUERY PLAN " + query)]
    assert any("idx_event_members_completed" in detail for detail in plan)


def test_review_selection_and_user_moment_queries_use_owner_indexes(db):
    queries = [
        ("SELECT * FROM saved_expressions WHERE status='learning' AND user_id='local' ORDER BY mastery_count,created_at LIMIT 2", "idx_expressions_user_review"),
        ("SELECT * FROM user_moments WHERE user_id='local' ORDER BY created_at DESC,rowid DESC LIMIT 30", "idx_user_moments_user_time"),
    ]
    for sql, index in queries:
        assert any(index in row[3] for row in db.conn.execute("EXPLAIN QUERY PLAN " + sql))
