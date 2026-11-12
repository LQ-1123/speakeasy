"""Email code authentication contracts; SMTP is captured, never contacted."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.v2.db import DB
from app.v3 import accounts
from app.v3.accounts_router import router as auth_router
from app.v3.router import router as v3_router
from app.v3.schema import ensure_schema


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("SE_EMAIL_CODE_AUTH", "1")
    monkeypatch.setenv("SE_EMAIL_CODE_SECRET", "test-only-email-code-secret-32-characters")
    monkeypatch.setenv("SE_MAIL_DEV", "0")
    monkeypatch.setenv("SE_INVITE_REQUIRED", "1")
    monkeypatch.setenv("SE_REQUIRE_AUTH", "1")
    db = DB(str(tmp_path / "codes.db"))
    db.init()
    ensure_schema(db)
    accounts.ensure_accounts_schema(db)
    accounts.create_invite_code(db, "invite", max_uses=5)
    messages = []
    monkeypatch.setattr(accounts, "_send_mail", lambda to, subject, body: messages.append((to, body)) or True)
    app = FastAPI()
    app.state.v2 = SimpleNamespace(db=db)
    app.include_router(auth_router)
    app.include_router(v3_router)
    with TestClient(app) as client:
        yield client, db, messages
    db.close()


def request_code(client, email="Alice@Example.com"):
    return client.post("/api/v3/auth/email-code/request", json={"email": email})


def verify(client, challenge, code, **kwargs):
    return client.post("/api/v3/auth/email-code/verify", json={"challenge_id": challenge, "code": code, **kwargs})


def sent_code(messages):
    import re
    return re.search(r"\b\d{6}\b", messages[-1][1]).group()


def start(client, messages, email="Alice@Example.com"):
    response = request_code(client, email)
    assert response.status_code == 200, response.text
    assert set(response.json()) == {"challenge_id", "retry_after", "expires_in"}
    assert response.json()["retry_after"] == 60
    assert response.json()["expires_in"] == 600
    return response.json()["challenge_id"], sent_code(messages)


def test_code_flow_requires_invite_without_consuming_and_uses_web_cookie(api):
    client, db, messages = api
    assert client.get("/api/v3/auth/config").json() == {"email_code_auth": True, "invite_required": True}
    challenge, code = start(client, messages)
    missing = verify(client, challenge, code)
    assert missing.status_code == 403
    assert missing.json()["detail"]["code"] == "INVITE_REQUIRED"
    bad = verify(client, challenge, code, invite_code="bad")
    assert bad.status_code == 403
    ok = verify(client, challenge, code, invite_code="invite")
    assert ok.status_code == 200, ok.text
    data = ok.json()
    assert data["email"] == "alice@example.com"
    assert data["created"] is True and data["email_verified"] is True
    assert "session_token" not in data
    assert "HttpOnly" in ok.headers["set-cookie"]
    assert client.get("/api/v3/auth/identity").json()["user_id"] == data["user_id"]
    assert client.post("/api/v3/auth/logout").status_code == 403
    assert client.post("/api/v3/auth/logout", headers={"X-CSRF-Token": data["csrf_token"]}).status_code == 200
    assert client.get("/api/v3/auth/identity").status_code == 401
    assert verify(client, challenge, code, invite_code="invite").status_code == 400
    assert db.conn.execute("SELECT used_count FROM v3_invite_codes WHERE code='invite'").fetchone()[0] == 1


def test_android_returns_header_session_and_never_reuses_device_identity(api):
    client, db, messages = api
    db.conn.execute("INSERT INTO v3_users (id,device_key,token_hash,created_at,last_seen_at) VALUES ('u_old','same-device','old','','')")
    db.conn.commit()
    challenge, code = start(client, messages)
    data = verify(client, challenge, code, invite_code="invite", device_key="same-device", platform="android").json()
    assert data["user_id"] != "u_old"
    assert data["session_token"]
    client.cookies.clear()
    headers = {"X-Session-Token": data["session_token"]}
    assert client.get("/api/v3/auth/identity", headers=headers).json()["user_id"] == data["user_id"]
    assert client.post("/api/v3/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/v3/auth/identity", headers=headers).status_code == 401


def test_five_wrong_attempts_persist_and_lock_the_challenge(api):
    client, db, messages = api
    challenge, code = start(client, messages)
    wrong = "000000" if code != "000000" else "999999"
    for _ in range(5):
        assert verify(client, challenge, wrong).status_code == 400
    assert db.conn.execute("SELECT attempts FROM v3_email_code_challenges WHERE id=?", (challenge,)).fetchone()[0] == 5
    assert verify(client, challenge, code, invite_code="invite").status_code == 400
    assert not db.conn.execute("SELECT * FROM v3_accounts").fetchall()


def test_resend_throttles_and_failed_mail_preserves_challenge(api, monkeypatch):
    from app.v3 import email_codes
    client, db, messages = api
    challenge, code = start(client, messages)
    limited = request_code(client, " alice@example.com ")
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) > 0
    now = email_codes._now()
    with monkeypatch.context() as scoped:
        scoped.setattr(email_codes, "_now", lambda: now + 61)
        scoped.setattr(accounts, "_send_mail", lambda *args: False)
        assert request_code(client).status_code == 503
    assert verify(client, challenge, code, invite_code="invite").status_code == 200


def test_codes_expire_and_resend_invalidates_only_after_success(api, monkeypatch):
    from app.v3 import email_codes
    client, db, messages = api
    challenge, code = start(client, messages)
    now = email_codes._now()
    monkeypatch.setattr(email_codes, "_now", lambda: now + 61)
    newer, next_code = start(client, messages)
    assert verify(client, challenge, code, invite_code="invite").status_code == 400
    monkeypatch.setattr(email_codes, "_now", lambda: now + 662)
    assert verify(client, newer, next_code, invite_code="invite").status_code == 400


def test_legacy_auth_is_disabled_and_code_not_exposed(api):
    client, _, messages = api
    for path, body in [("register", {}), ("register/email", {"email": "a@example.com", "password": "password"}), ("login", {"email": "a@example.com", "password": "password"}), ("bind-legacy", {"email": "a@example.com", "password": "password"})]:
        assert client.post("/api/v3/auth/" + path, json=body).status_code == 403
    challenge, code = start(client, messages)
    assert code not in challenge
    assert client.get("/api/v3/me", headers={"X-User-Id": "u_old", "X-User-Token": "old"}).status_code == 401


def test_missing_secret_and_mail_service_fail_closed(api, monkeypatch, caplog):
    client, db, messages = api
    monkeypatch.delenv("SE_EMAIL_CODE_SECRET")
    assert request_code(client).status_code == 503
    monkeypatch.setenv("SE_EMAIL_CODE_SECRET", "test-only-email-code-secret-32-characters")
    monkeypatch.setattr(accounts, "_send_mail", lambda *args: False)
    assert request_code(client).status_code == 503
    assert not db.conn.execute("SELECT * FROM v3_accounts").fetchall()
    assert not db.conn.execute("SELECT * FROM v3_sessions").fetchall()


def test_success_rolls_back_invite_and_challenge_if_session_insert_fails(api):
    client, db, messages = api
    challenge, code = start(client, messages)
    db.conn.execute("CREATE TRIGGER fail_session BEFORE INSERT ON v3_sessions BEGIN SELECT RAISE(ABORT, 'session failure'); END")
    db.conn.commit()
    with pytest.raises(Exception, match="session failure"):
        verify(client, challenge, code, invite_code="invite")
    assert not db.conn.execute("SELECT * FROM v3_accounts").fetchall()
    assert db.conn.execute("SELECT used_count FROM v3_invite_codes WHERE code='invite'").fetchone()[0] == 0
    db.conn.execute("DROP TRIGGER fail_session")
    db.conn.commit()
    assert verify(client, challenge, code, invite_code="invite").status_code == 200


def test_parallel_verify_consumes_once(api):
    client, db, messages = api
    challenge, code = start(client, messages)
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: verify(client, challenge, code, invite_code="invite", platform="android"), range(2)))
    assert sorted(reply.status_code for reply in replies) == [200, 400]
    assert db.conn.execute("SELECT COUNT(*) FROM v3_accounts").fetchone()[0] == 1
    assert db.conn.execute("SELECT used_count FROM v3_invite_codes WHERE code='invite'").fetchone()[0] == 1


def test_parallel_send_same_email_delivers_only_one_code(api):
    from app.v3 import email_codes
    _, db, messages = api
    other = DB(db.path)
    other.init()
    try:
        def attempt(database):
            try:
                return email_codes.request_code(database, "race@example.com", "127.0.0.1")
            except email_codes.CodeError as exc:
                return exc.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, [db, other]))
        assert sum(isinstance(value, dict) for value in results) == 1
        assert 429 in results
        assert len(messages) == 1
    finally:
        other.close()


def test_last_invite_slot_cannot_be_spent_by_two_accounts(api):
    from app.v3 import email_codes
    client, db, messages = api
    accounts.create_invite_code(db, "last-slot", max_uses=1)
    one, code_one = start(client, messages, "one@example.com")
    two, code_two = start(client, messages, "two@example.com")
    other = DB(db.path)
    other.init()
    try:
        def attempt(args):
            database, challenge, code = args
            try:
                return email_codes.verify_code(database, challenge, code, invite_code="last-slot")
            except email_codes.CodeError as exc:
                return exc.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, [(db, one, code_one), (other, two, code_two)]))
        assert sum(isinstance(value, dict) for value in results) == 1
        assert 403 in results
        assert db.conn.execute("SELECT used_count FROM v3_invite_codes WHERE code='last-slot'").fetchone()[0] == 1
        loser = (one, code_one) if results[0] == 403 else (two, code_two)
        assert verify(client, *loser, invite_code="invite").status_code == 200
    finally:
        other.close()


def test_database_contains_only_keyed_code_hash_and_hashed_session(api):
    client, db, messages = api
    challenge, code = start(client, messages)
    stored = db.conn.execute("SELECT code_hash FROM v3_email_code_challenges WHERE id=?", (challenge,)).fetchone()[0]
    assert stored != code and len(stored) == 64
    data = verify(client, challenge, code, invite_code="invite", platform="android").json()
    token_hash = db.conn.execute("SELECT token_hash FROM v3_sessions WHERE id=?", (data["session_id"],)).fetchone()[0]
    assert token_hash != data["session_token"] and len(token_hash) == 64


@pytest.mark.parametrize("mutation", ["missing_account", "disabled", "unverified", "archived", "missing_user"])
def test_session_rejects_unavailable_account(api, mutation):
    client, db, messages = api
    challenge, code = start(client, messages)
    data = verify(client, challenge, code, invite_code="invite", platform="android").json()
    uid = data["user_id"]
    sql = {
        "missing_account": "DELETE FROM v3_accounts WHERE user_id=?",
        "disabled": "UPDATE v3_accounts SET status='disabled' WHERE user_id=?",
        "unverified": "UPDATE v3_accounts SET email_verified_at='' WHERE user_id=?",
        "archived": "UPDATE v3_users SET archived_at='2026-11-12' WHERE id=?",
        "missing_user": "DELETE FROM v3_users WHERE id=?",
    }[mutation]
    db.conn.execute(sql, (uid,))
    db.conn.commit()
    client.cookies.clear()
    assert client.get("/api/v3/auth/identity", headers={"X-Session-Token": data["session_token"]}).status_code == 401


def test_returning_account_needs_no_new_invite_and_bearer_logout_revokes(api, monkeypatch):
    from app.v3 import email_codes
    client, db, messages = api
    challenge, code = start(client, messages)
    first = verify(client, challenge, code, invite_code="invite", platform="android").json()
    now = email_codes._now()
    monkeypatch.setattr(email_codes, "_now", lambda: now + 61)
    challenge, code = start(client, messages)
    second = verify(client, challenge, code, platform="android").json()
    assert second["user_id"] == first["user_id"]
    assert second["created"] is False
    assert second["session_token"] != first["session_token"]
    assert db.conn.execute("SELECT used_count FROM v3_invite_codes").fetchone()[0] == 1
    headers = {"Authorization": "Bearer " + second["session_token"]}
    assert client.get("/api/v3/auth/identity", headers=headers).status_code == 200
    assert client.post("/api/v3/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/v3/auth/identity", headers=headers).status_code == 401


def test_email_limit_survives_a_fresh_database_connection(api, monkeypatch):
    from app.v3 import email_codes
    client, db, messages = api
    now = email_codes._now()
    for offset in range(5):
        monkeypatch.setattr(email_codes, "_now", lambda offset=offset: now + offset * 61)
        start(client, messages)
    monkeypatch.setattr(email_codes, "_now", lambda: now + 305)
    restarted = DB(db.path)
    restarted.init()
    try:
        with pytest.raises(email_codes.CodeError) as exc:
            email_codes.request_code(restarted, "alice@example.com", "new-ip")
        assert exc.value.status == 429
        assert exc.value.retry_after > 0
    finally:
        restarted.close()


def test_ip_limit_ignores_spoofed_forwarded_headers(api):
    client, _, _ = api
    for index in range(30):
        response = client.post("/api/v3/auth/email-code/request", json={"email": f"person{index}@example.com"},
                               headers={"X-Forwarded-For": f"10.0.0.{index}"})
        assert response.status_code == 200
    limited = request_code(client, "blocked@example.com")
    assert limited.status_code == 429
    assert 0 < int(limited.headers["Retry-After"]) <= 3600


def test_same_email_challenges_cannot_create_two_accounts(api, monkeypatch):
    from app.v3 import email_codes
    client, db, messages = api
    challenge, _ = start(client, messages)
    now = email_codes._now()
    monkeypatch.setattr(email_codes, "_now", lambda: now + 61)
    challenge, code = start(client, messages)
    # Resend has one live challenge; a second connection races the exact same code.
    other = DB(db.path)
    other.init()
    try:
        def attempt(database):
            try:
                return email_codes.verify_code(database, challenge, code, invite_code="invite")
            except email_codes.CodeError as exc:
                return exc.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, [db, other]))
        assert sum(isinstance(value, dict) for value in results) == 1
        assert 400 in results
        assert db.conn.execute("SELECT COUNT(*) FROM v3_accounts").fetchone()[0] == 1
    finally:
        other.close()


def test_no_smtp_does_not_log_code_or_claim_delivery(api, monkeypatch, caplog):
    client, db, _ = api
    from app.v3 import email_codes
    monkeypatch.delenv("SE_SMTP_HOST", raising=False)
    monkeypatch.setattr(email_codes.secrets, "randbelow", lambda _: 812345)
    # Restore the real transport, captured before the fixture patches it.
    monkeypatch.setattr(accounts, "_send_mail", REAL_SEND_MAIL)
    assert request_code(client).status_code == 503
    assert "812345" not in caplog.text
    assert db.conn.execute("SELECT COUNT(*) FROM v3_email_code_challenges").fetchone()[0] == 0


REAL_SEND_MAIL = accounts._send_mail


def test_slow_smtp_releases_shared_lock_and_other_email_can_send(api, monkeypatch):
    from app.v3 import email_codes
    _, db, _ = api
    other = DB(db.path)
    other.init()
    entered, release = Event(), Event()
    now = email_codes._now()

    def send(email, *_):
        if email == "slow@example.com":
            entered.set()
            assert release.wait(3)
        return True

    monkeypatch.setattr(accounts, "_send_mail", send)
    pool = ThreadPoolExecutor(max_workers=3)
    first = pool.submit(email_codes.request_code, db, "slow@example.com", "127.0.0.1")
    try:
        assert entered.wait(1)
        # Same connection's shared read lock and another connection's writes
        # must both remain usable while the transport is paused.
        read = pool.submit(accounts.account_for_email, db, "unrelated@example.com")
        assert read.result(timeout=0.5) is None
        second = pool.submit(email_codes.request_code, other, "fast@example.com", "127.0.0.1")
        assert second.result(timeout=0.5)["challenge_id"]
        monkeypatch.setattr(email_codes, "_now", lambda: now + 61)
        with pytest.raises(email_codes.CodeError) as exc:
            email_codes.request_code(other, "slow@example.com", "127.0.0.1")
        assert exc.value.status == 429
        assert exc.value.retry_after >= 59
    finally:
        release.set()
        pool.shutdown(wait=True)
        other.close()
    assert first.result()["challenge_id"]


def test_stale_sender_cannot_replace_newer_delivered_challenge(api, monkeypatch):
    from app.v3 import email_codes
    client, db, messages = api
    other = DB(db.path)
    other.init()
    entered, release = Event(), Event()
    now = email_codes._now()
    original_send = accounts._send_mail

    def send(email, subject, body):
        if not entered.is_set():
            entered.set()
            assert release.wait(3)
        return original_send(email, subject, body)

    monkeypatch.setattr(accounts, "_send_mail", send)
    pool = ThreadPoolExecutor(max_workers=1)
    first = pool.submit(email_codes.request_code, db, "alice@example.com", "127.0.0.1")
    try:
        assert entered.wait(1)
        monkeypatch.setattr(email_codes, "_now", lambda: now + 121)
        current = email_codes.request_code(other, "alice@example.com", "127.0.0.1")
        current_code = sent_code(messages)
    finally:
        release.set()
        pool.shutdown(wait=True)
        other.close()
    with pytest.raises(email_codes.CodeError) as exc:
        first.result()
    assert exc.value.status == 503
    assert verify(client, current["challenge_id"], current_code, invite_code="invite").status_code == 200


def test_smtp_overall_deadline_preserves_old_code_and_pending_lease(api, monkeypatch):
    from app.v3 import email_codes
    client, db, messages = api
    old, old_code = start(client, messages)
    entered, release = Event(), Event()
    now = email_codes._now()
    monkeypatch.setattr(email_codes, "_now", lambda: now + 61)
    monkeypatch.setattr(email_codes, "SEND_TIMEOUT_SECONDS", 0.02, raising=False)

    def send(*_):
        entered.set()
        release.wait(2)
        return True

    monkeypatch.setattr(accounts, "_send_mail", send)
    try:
        response = request_code(client)
        assert entered.is_set()
        assert response.status_code == 503
        assert db.conn.execute("SELECT status FROM v3_email_code_requests ORDER BY created_at DESC LIMIT 1").fetchone()[0] == "failed"
        assert verify(client, old, old_code, invite_code="invite").status_code == 200
        monkeypatch.setattr(email_codes, "_now", lambda: now + 122)
        limited = request_code(client)
        assert limited.status_code == 429
    finally:
        release.set()


def test_sqlite_busy_is_structured_service_unavailable(api):
    from app.v3 import email_codes
    client, db, messages = api
    challenge, code = start(client, messages)
    blocker = DB(db.path)
    blocker.init()
    db.conn.execute("PRAGMA busy_timeout=10")
    blocker.conn.execute("BEGIN IMMEDIATE")
    try:
        request = request_code(client, "busy@example.com")
        assert request.status_code == 503
        assert request.json()["detail"]["code"] == "AUTH_UNAVAILABLE"
        verification = verify(client, challenge, code, invite_code="invite")
        assert verification.status_code == 503
        assert verification.json()["detail"]["code"] == "AUTH_UNAVAILABLE"
    finally:
        blocker.conn.rollback()
        blocker.close()


def test_old_request_schema_migrates_without_losing_rate_limits(api):
    from app.v3 import email_codes
    _, db, _ = api
    # Reproduce the initial Task 1 layout, before pending/status support.
    db.conn.execute("CREATE TABLE v3_email_code_requests (id TEXT PRIMARY KEY,email TEXT NOT NULL,ip TEXT NOT NULL,created_at REAL NOT NULL,delivered INTEGER NOT NULL DEFAULT 0)")
    db.conn.execute("INSERT INTO v3_email_code_requests VALUES ('old-request','old@example.com','127.0.0.1',?,1)", (email_codes._now(),))
    db.conn.commit()
    with pytest.raises(email_codes.CodeError) as exc:
        email_codes.request_code(db, "old@example.com", "127.0.0.1")
    assert exc.value.status == 429
    assert db.conn.execute("SELECT status FROM v3_email_code_requests WHERE id='old-request'").fetchone()[0] == "delivered"
