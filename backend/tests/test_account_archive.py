"""Archive keeps business data while closing credentials, devices and jobs."""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
import threading

import pytest

from app.v2.db import DB
from app.v3.schema import ensure_schema
from app.v3 import accounts, account_archive
from app.v3.client_api import ensure_client_schema
from app.jobs import DurableJobQueue


@pytest.fixture()
def db(tmp_path):
    database = DB(str(tmp_path / "archive.db"))
    database.init()
    ensure_schema(database, seed=False)
    accounts.ensure_accounts_schema(database)
    ensure_client_schema(database)
    account_archive.ensure_archive_schema(database)
    yield database
    database.close()


def add_user(db, uid, email=""):
    with db._lock, db.conn:
        db.conn.execute("INSERT INTO v3_users(id,device_key,token_hash,created_at) VALUES (?,?,?,?)",
                        (uid, "device-" + uid, "old-device-token", "2026-11-01T00:00:00+00:00"))
        if email:
            db.conn.execute("INSERT INTO v3_accounts(user_id,email,password_hash,email_verified_at,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                            (uid, email, "old-password-hash", "2026-11-01", "2026-11-01", "2026-11-01"))


def test_archive_preserves_posts_releases_email_and_stops_delivery(db):
    add_user(db, "u_old", "me@example.com")
    add_user(db, "u_keep", "keep@example.com")
    session = accounts.create_session(db, "u_old")
    token = accounts.issue_email_token(db, "u_old", "verify_email")
    queue = DurableJobQueue(db.conn, lock=db._lock)
    job = queue.enqueue("followup", {"user_id": "u_old"}, dedupe_key="old-job")
    with db._lock, db.conn:
        db.conn.execute("INSERT INTO user_posts(id,user_id,content,created_at) VALUES ('post1','u_old','keep this','2026-11-01')")
        db.conn.execute("INSERT INTO client_devices VALUES ('registration-old','u_old','android','',1,0)")
        db.conn.execute("INSERT INTO client_events(id,user_id,type,created_at) VALUES ('old-event','u_old','message','2026-11-01')")
    result = account_archive.archive_users(db, ["u_old"], migration_id="test-cutover")
    assert result["archived"] == 1
    assert account_archive.is_archived(db, "u_old")
    assert not account_archive.is_archived(db, "u_keep")
    assert accounts.account_for_email(db, "me@example.com") is None
    assert accounts.authenticate_session(db, session["session_token"]) is None
    assert accounts.consume_email_token(db, token, "verify_email") is None
    assert db.conn.execute("SELECT content FROM user_posts WHERE id='post1'").fetchone()[0] == "keep this"
    assert db.conn.execute("SELECT COUNT(*) FROM client_devices WHERE user_id='u_old'").fetchone()[0] == 0
    assert db.conn.execute("SELECT push_status FROM client_events WHERE id='old-event'").fetchone()[0] == "suppressed"
    assert queue.get(job)["status"] == "cancelled"
    historical = db.conn.execute("SELECT account_json FROM v3_identity_archives WHERE user_id='u_old'").fetchone()[0]
    assert json.loads(historical)["email"] == "me@example.com"
    assert account_archive.archive_users(db, ["u_old"], migration_id="test-cutover")["archived"] == 0


def test_archive_is_atomic_for_unknown_id(db):
    add_user(db, "u_old", "me@example.com")
    with pytest.raises(ValueError, match="不存在"):
        account_archive.archive_users(db, ["u_old", "u_missing"], migration_id="test")
    assert not account_archive.is_archived(db, "u_old")
    assert accounts.account_for_email(db, "me@example.com") is not None


def test_formal_metrics_distinguish_accounts_sessions_devices_and_history(db):
    from app.admin.user_scope import account_counts
    add_user(db, "u_live", "me@example.com")
    add_user(db, "u_history")
    accounts.create_session(db, "u_live")
    accounts.create_session(db, "u_live")
    with db._lock, db.conn:
        db.conn.execute("INSERT INTO client_devices VALUES ('registration-one','u_live','android','',1,0)")
        db.conn.execute("INSERT INTO client_devices VALUES ('registration-two','u_live','android','',1,0)")
    metrics = account_counts(db)
    assert metrics == {"formal": 1, "history": 1, "sessions": 2, "devices": 2}


def test_archive_helper_works_for_old_db_without_modifying_on_read():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db = SimpleNamespace(conn=conn, _lock=threading.RLock())
    conn.execute("CREATE TABLE v3_users(id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO v3_users VALUES ('u_old')")
    conn.commit()
    assert not account_archive.is_archived(db, "u_old")
    assert len(conn.execute("PRAGMA table_info(v3_users)").fetchall()) == 1
    account_archive.ensure_archive_schema(db)
    account_archive.ensure_archive_schema(db)
    assert "archived_at" in {r[1] for r in conn.execute("PRAGMA table_info(v3_users)")}
    conn.close()


def test_admin_defaults_to_formal_and_history_remains_searchable(db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.admin.api.users import router
    monkeypatch.setenv("SE_EMAIL_CODE_AUTH", "1")
    add_user(db, "u_live", "me@example.com")
    add_user(db, "u_history", "old@example.com")
    account_archive.archive_users(db, ["u_history"], migration_id="test")
    app = FastAPI()
    app.state.v2 = SimpleNamespace(db=db)
    app.include_router(router)
    with TestClient(app) as client:
        data = client.get("/users").json()
        assert data["total"] == 1
        assert data["items"][0]["email"] == "me@example.com"
        assert data["summary"]["history"] == 1
        history = client.get("/users", params={"scope": "history", "search": "old@example.com"}).json()
        assert history["total"] == 1
        assert history["items"][0]["archived_at"]
        assert client.post("/users/u_history/friends", json={"persona_id":"p_old"}).status_code == 403
        assert client.delete("/users/u_history/friends/old-link").status_code == 403
        assert client.post("/users/u_history/push-test").status_code == 403


def test_overview_counts_formal_and_beijing_activity(db, monkeypatch):
    from app.admin.api import overview
    monkeypatch.setenv("SE_EMAIL_CODE_AUTH", "1")
    add_user(db, "u_live", "me@example.com")
    add_user(db, "u_legacy")
    with db._lock, db.conn:
        db.conn.execute("UPDATE v3_users SET last_seen_at='2026-11-11T17:00:00+00:00'")
    monkeypatch.setattr(overview, "_account_day_start_utc", lambda: "2026-11-11T16:00:00+00:00")
    data = overview._users(db)
    assert data["total"] == 1
    assert data["history"] == 1
    assert data["active_today"] == 1


@pytest.mark.asyncio
async def test_archived_followup_does_not_generate_a_message(db, monkeypatch):
    from app.v3.followup_jobs import deliver_followup
    from unittest.mock import AsyncMock
    add_user(db, "u_old")
    account_archive.archive_users(db, ["u_old"], migration_id="test")
    memory = SimpleNamespace(due_open_loops=lambda f: [{"event":"later", "follow_up_after":"now", "index":0}],
                             followup_message=AsyncMock(return_value="must not generate"),
                             mark_open_loop_done=lambda *a: None)
    services = SimpleNamespace(db=db, memory=memory, chat=SimpleNamespace(store_friend_voice=AsyncMock()))
    friend = db.create_friend({"name":"Friend", "owner_user_id":"u_old"})
    await deliver_followup(services, {"user_id":"u_old", "friend_id":friend["id"], "event":"later", "follow_up_after":"now"})
    memory.followup_message.assert_not_called()


def test_formal_account_never_inherits_local_data_even_with_legacy_deploy_flag(db, monkeypatch):
    from app.v3.identity import set_current_user, reset_current_user
    monkeypatch.setenv("SE_LEGACY_SHARED_OWNER", "1")
    monkeypatch.setenv("SE_EMAIL_CODE_AUTH", "1")
    old_friend = db.create_friend({"name":"Old local friend", "owner_user_id":"local"})
    context = set_current_user("u_new_account")
    try:
        assert db.list_friends() == []
        assert db.get_friend(old_friend["id"]) is None
    finally:
        reset_current_user(context)
