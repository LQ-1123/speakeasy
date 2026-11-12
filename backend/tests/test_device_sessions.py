"""Push ownership follows the authenticated session, including explicit logout."""
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.v2.db import DB
from app.v3.schema import ensure_schema
from app.v3 import accounts
from app.v3.client_api import router, ensure_client_schema
from app.v3.accounts_router import router as auth_router


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SE_EMAIL_CODE_AUTH", "1")
    db = DB(str(tmp_path / "bindings.db")); db.init(); ensure_schema(db, seed=False)
    accounts.ensure_accounts_schema(db); ensure_client_schema(db)
    headers = []
    for uid in ("u_alice", "u_bob"):
        with db._lock, db.conn:
            db.conn.execute("INSERT INTO v3_users(id) VALUES (?)", (uid,))
            db.conn.execute("INSERT INTO v3_accounts(user_id,email,password_hash,email_verified_at,created_at,updated_at) VALUES (?,?,'','verified','','')", (uid, uid + "@example.com"))
        session = accounts.create_session(db, uid)
        headers.append({"X-Session-Token": session["session_token"]})
    app = FastAPI(); app.state.v2 = SimpleNamespace(db=db)
    app.include_router(router); app.include_router(auth_router)
    with TestClient(app) as client:
        yield client, db, headers
    db.close()


def test_other_account_cannot_overwrite_push_binding_but_logout_releases_it(env):
    client, db, (alice, bob) = env
    body = {"registration_id":"registration-12345", "platform":"android"}
    assert client.post("/api/v3/client/devices", headers=alice, json=body).status_code == 200
    assert client.post("/api/v3/client/devices", headers=bob, json=body).status_code == 409
    assert client.post("/api/v3/auth/logout", headers=alice).status_code == 200
    assert client.post("/api/v3/client/devices", headers=bob, json=body).status_code == 200
    assert db.conn.execute("SELECT user_id FROM client_devices").fetchone()[0] == "u_bob"


def test_revoke_all_releases_all_that_accounts_devices(env):
    client, db, (alice, bob) = env
    for number, headers in enumerate((alice, bob)):
        assert client.post("/api/v3/client/devices", headers=headers,
                           json={"registration_id":f"registration-{number}12345"}).status_code == 200
    assert client.post("/api/v3/auth/logout-all", headers=alice).status_code == 200
    rows = db.conn.execute("SELECT user_id FROM client_devices").fetchall()
    assert [r[0] for r in rows] == ["u_bob"]


def test_revoked_event_ticket_cannot_open_a_socket(env):
    from starlette.websockets import WebSocketDisconnect
    client, db, (alice, bob) = env
    ticket = client.post("/api/v3/client/event-ticket", headers=alice).json()["ticket"]
    client.post("/api/v3/auth/logout", headers=alice)
    with client.websocket_connect("/api/v3/client/events") as ws:
        ws.send_json({"type":"auth", "ticket":ticket, "last_sequence":0})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_account_activity_updates_user_and_session_list_marks_current(env):
    client, db, (alice, bob) = env
    with db._lock, db.conn:
        db.conn.execute("UPDATE v3_users SET last_seen_at='2000-01-01' WHERE id='u_alice'")
    assert client.get("/api/v3/client/preferences", headers=alice).status_code == 200
    assert db.conn.execute("SELECT last_seen_at FROM v3_users WHERE id='u_alice'").fetchone()[0] != "2000-01-01"
    sessions = client.get("/api/v3/auth/sessions", headers=alice).json()["items"]
    assert len(sessions) == 1 and sessions[0]["current"] is True
