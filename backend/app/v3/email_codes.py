"""Persistent one-time email login codes and atomic account/session issuance.

No plaintext code is stored or returned. Short IMMEDIATE transactions reserve
send quota and finalize delivery; SMTP never holds a database lock. Persistent
per-email leases serialize send generations and fence off abandoned senders.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any

from app.v3 import accounts

TTL = 600
RESEND_SECONDS = 60
MAX_ATTEMPTS = 5
SEND_TIMEOUT_SECONDS = 30
SEND_LEASE_SECONDS = 120
_DEV_SECRET = secrets.token_bytes(32)

DDL = """
CREATE TABLE IF NOT EXISTS v3_email_code_challenges (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    used_at REAL,
    invalidated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_v3_email_code_email ON v3_email_code_challenges(email, created_at);
CREATE TABLE IF NOT EXISTS v3_email_code_requests (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    ip TEXT NOT NULL,
    created_at REAL NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_v3_email_code_rate_email ON v3_email_code_requests(email, created_at);
CREATE INDEX IF NOT EXISTS idx_v3_email_code_rate_ip ON v3_email_code_requests(ip, created_at);
CREATE TABLE IF NOT EXISTS v3_email_code_pending (
    email TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    lease_expires_at REAL NOT NULL
);
"""


class CodeError(Exception):
    def __init__(self, status: int, code: str, message: str, retry_after: int = 0):
        super().__init__(message)
        self.status = status
        self.detail = {"code": code, "message": message}
        self.retry_after = retry_after


def _now() -> float:
    return time.time()


def ensure_schema(db: Any) -> None:
    if getattr(db, "_email_codes_schema_ready", False):
        return
    try:
        # Account helpers commit internally, so initialize before our transaction.
        if not getattr(db, "_accounts_schema_ready", False):
            accounts.ensure_accounts_schema(db)
        with _transaction(db) as conn:
            for statement in DDL.split(";"):
                if statement.strip():
                    conn.execute(statement)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(v3_email_code_requests)")}
            if "status" not in columns:
                conn.execute("ALTER TABLE v3_email_code_requests ADD COLUMN status TEXT NOT NULL DEFAULT 'failed'")
                conn.execute("UPDATE v3_email_code_requests SET status='delivered' WHERE delivered=1")
        db._email_codes_schema_ready = True
    except sqlite3.OperationalError as exc:
        _raise_if_busy(exc)
        raise


def _raise_if_busy(exc: sqlite3.OperationalError) -> None:
    code = int(getattr(exc, "sqlite_errorcode", 0)) & 0xFF
    if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or any(
        value in str(exc).lower() for value in ("database is locked", "database table is locked")
    ):
        raise CodeError(503, "AUTH_UNAVAILABLE", "验证服务繁忙，请稍后再试") from None


@contextmanager
def _transaction(db: Any):
    with db._lock:
        try:
            db.conn.execute("BEGIN IMMEDIATE")
            try:
                yield db.conn
                db.conn.commit()
            except BaseException:
                db.conn.rollback()
                raise
        except sqlite3.OperationalError as exc:
            _raise_if_busy(exc)
            raise


def _secret() -> bytes:
    value = os.getenv("SE_EMAIL_CODE_SECRET", "").strip()
    if len(value.encode("utf-8")) >= 32:
        return value.encode("utf-8")
    if accounts.mail_dev_mode():
        return _DEV_SECRET
    raise CodeError(503, "MAIL_UNAVAILABLE", "验证码服务暂不可用")


def _digest(secret: bytes, challenge_id: str, email: str, code: str) -> str:
    return hmac.new(secret, f"{challenge_id}\n{email}\n{code}".encode(), hashlib.sha256).hexdigest()


def _rate_error(retry: float) -> CodeError:
    return CodeError(429, "RATE_LIMITED", "请求过于频繁，请稍后再试", max(1, math.ceil(retry)))


def request_code(db: Any, email: str, ip: str) -> dict:
    try:
        email = accounts.validate_email(email)
    except ValueError as exc:
        raise CodeError(400, "INVALID_EMAIL", str(exc)) from None
    secret = _secret()
    ensure_schema(db)
    challenge_id = "ec_" + secrets.token_urlsafe(24)
    code = f"{secrets.randbelow(1_000_000):06d}"
    with _transaction(db) as conn:
        now = _now()
        pending = conn.execute("SELECT request_id,lease_expires_at FROM v3_email_code_pending WHERE email=?", (email,)).fetchone()
        if pending is not None:
            if float(pending["lease_expires_at"]) > now:
                raise _rate_error(float(pending["lease_expires_at"]) - now)
            # A crashed/timed-out sender cannot activate after the lease changes.
            conn.execute("UPDATE v3_email_code_requests SET status='expired' WHERE id=? AND status='pending'", (pending["request_id"],))
            conn.execute("DELETE FROM v3_email_code_pending WHERE email=?", (email,))
        # Failed SMTP attempts count too, so an unavailable relay cannot be hammered.
        recent = conn.execute(
            "SELECT created_at FROM v3_email_code_requests WHERE email=? AND created_at>? ORDER BY created_at",
            (email, now - 3600),
        ).fetchall()
        if recent and now - float(recent[-1]["created_at"]) < RESEND_SECONDS:
            raise _rate_error(RESEND_SECONDS - (now - float(recent[-1]["created_at"])))
        if len(recent) >= 5:
            raise _rate_error(3600 - (now - float(recent[0]["created_at"])))
        recent_ip = conn.execute(
            "SELECT created_at FROM v3_email_code_requests WHERE ip=? AND created_at>? ORDER BY created_at",
            (str(ip or "unknown")[:128], now - 3600),
        ).fetchall()
        if len(recent_ip) >= 30:
            raise _rate_error(3600 - (now - float(recent_ip[0]["created_at"])))
        conn.execute(
            "INSERT INTO v3_email_code_requests (id,email,ip,created_at,status) VALUES (?,?,?,?,'pending')",
            (challenge_id, email, str(ip or "unknown")[:128], now),
        )
        conn.execute("INSERT INTO v3_email_code_pending (email,request_id,lease_expires_at) VALUES (?,?,?)",
                     (email, challenge_id, now + SEND_LEASE_SECONDS))

    # No DB object/lock crosses the transport boundary. The worker owns only
    # message strings, and late transport completion cannot mutate the database.
    delivered, timed_out = _send_with_deadline(email, code)
    error = None
    with _transaction(db) as conn:
        now = _now()
        pending = conn.execute(
            "SELECT 1 FROM v3_email_code_pending WHERE email=? AND request_id=? AND lease_expires_at>?",
            (email, challenge_id, now),
        ).fetchone()
        if pending is None:
            conn.execute("UPDATE v3_email_code_requests SET status='expired' WHERE id=? AND status='pending'", (challenge_id,))
            error = CodeError(503, "MAIL_UNAVAILABLE", "发信请求已失效，请重新获取验证码")
        elif delivered:
            # Only this still-current, successfully delivered generation may
            # invalidate the last working code. Expiry starts after delivery.
            conn.execute(
                "UPDATE v3_email_code_challenges SET invalidated_at=? WHERE email=? AND used_at IS NULL AND invalidated_at IS NULL",
                (now, email),
            )
            conn.execute(
                "INSERT INTO v3_email_code_challenges (id,email,code_hash,created_at,expires_at) VALUES (?,?,?,?,?)",
                (challenge_id, email, _digest(secret, challenge_id, email, code), now, now + TTL),
            )
            conn.execute("UPDATE v3_email_code_requests SET delivered=1,status='delivered' WHERE id=?", (challenge_id,))
        else:
            conn.execute("UPDATE v3_email_code_requests SET status='failed' WHERE id=?", (challenge_id,))
            error = CodeError(503, "MAIL_UNAVAILABLE", "邮件暂时无法发送，请稍后再试")
        if pending is not None and not timed_out:
            conn.execute("DELETE FROM v3_email_code_pending WHERE email=? AND request_id=?", (email, challenge_id))
        # Timeout keeps its lease until expiry: the network operation may still
        # be winding down, so another same-email send must not start immediately.
    if error:
        raise error
    return {"challenge_id": challenge_id, "retry_after": RESEND_SECONDS, "expires_in": TTL}


def _send_with_deadline(email: str, code: str) -> tuple[bool, bool]:
    completed = threading.Event()
    result = [False]
    transport = accounts._send_mail

    def send() -> None:
        try:
            result[0] = bool(transport(email, "Sonder 登录验证码", f"登录验证码：{code}\n10 分钟内有效，请勿向他人透露。"))
        except Exception:
            # Transport exceptions can echo credentials/message contents.
            result[0] = False
        finally:
            completed.set()

    threading.Thread(target=send, name="email-code-smtp", daemon=True).start()
    if not completed.wait(SEND_TIMEOUT_SECONDS):
        return False, True
    return result[0], False


def verify_code(
    db: Any, challenge_id: str, code: str, *, invite_code: str = "", device_key: str = "", platform: str = ""
) -> dict:
    secret = _secret()
    ensure_schema(db)
    error = None
    result = None
    with _transaction(db) as conn:
        now = _now()
        challenge = conn.execute("SELECT * FROM v3_email_code_challenges WHERE id=?", (challenge_id,)).fetchone()
        if challenge is None:
            error = CodeError(400, "INVALID_CODE", "验证码无效或已失效")
        elif challenge["used_at"] is not None or challenge["invalidated_at"] is not None:
            error = CodeError(400, "CODE_USED", "验证码已失效，请重新获取")
        elif float(challenge["expires_at"]) <= now:
            error = CodeError(400, "CODE_EXPIRED", "验证码已过期，请重新获取")
        elif int(challenge["attempts"]) >= MAX_ATTEMPTS:
            error = CodeError(400, "CODE_ATTEMPTS_EXCEEDED", "验证失败次数过多，请重新获取")
        elif not hmac.compare_digest(str(challenge["code_hash"]), _digest(secret, challenge_id, str(challenge["email"]), code)):
            conn.execute("UPDATE v3_email_code_challenges SET attempts=attempts+1 WHERE id=?", (challenge_id,))
            error = CodeError(400, "INVALID_CODE", "验证码不正确")
        else:
            result = _complete_login(conn, challenge, invite_code, device_key, platform)
            conn.execute("UPDATE v3_email_code_challenges SET used_at=? WHERE id=?", (now, challenge_id))
    # In particular, failed-attempt writes must commit before the HTTP error.
    if error:
        raise error
    return result


def _complete_login(conn: Any, challenge: Any, invite_code: str, device_key: str, platform: str) -> dict:
    email = str(challenge["email"])
    account = conn.execute("SELECT * FROM v3_accounts WHERE email=?", (email,)).fetchone()
    created = account is None
    now = accounts._now()
    if account is None:
        if accounts.invite_required():
            if not invite_code.strip():
                raise CodeError(403, "INVITE_REQUIRED", "首次登录需要邀请码")
            changed = conn.execute(
                "UPDATE v3_invite_codes SET used_count=used_count+1 WHERE code=? AND disabled=0"
                " AND used_count<max_uses AND (expires_at='' OR expires_at>?)",
                (invite_code.strip(), now),
            ).rowcount
            if changed != 1:
                raise CodeError(403, "INVITE_INVALID", "邀请码无效或已用尽")
        uid = "u_" + secrets.token_hex(16)
        # Device keys belong to sessions, never to the new user's identity.
        conn.execute(
            "INSERT INTO v3_users (id,device_key,token_hash,display_name,created_at,last_seen_at) VALUES (?,NULL,'','',?,?)",
            (uid, now, now),
        )
        conn.execute(
            "INSERT INTO v3_accounts (user_id,email,password_hash,email_verified_at,status,created_at,updated_at)"
            " VALUES (?,?,'',?,'active',?,?)", (uid, email, now, now, now),
        )
    else:
        uid = str(account["user_id"])
        user = conn.execute("SELECT archived_at FROM v3_users WHERE id=?", (uid,)).fetchone()
        if str(account["status"]) != "active" or user is None or str(user["archived_at"] or ""):
            raise CodeError(403, "ACCOUNT_UNAVAILABLE", "账号不可用")
        conn.execute("UPDATE v3_accounts SET email_verified_at=?,updated_at=? WHERE user_id=?", (now, now, uid))
        conn.execute("UPDATE v3_users SET last_seen_at=? WHERE id=?", (now, uid))
    sid = "s_" + secrets.token_hex(12)
    token = accounts._new_token(32)
    expires = accounts._iso_after(days=30)
    conn.execute(
        "INSERT INTO v3_sessions (id,user_id,token_hash,device_key,platform,created_at,last_seen_at,expires_at) VALUES (?,?,?,?,?,?,?,?)",
        (sid, uid, accounts._hash_token(token), device_key[:128], platform[:32], now, now, expires),
    )
    return {"user_id": uid, "email": email, "created": created, "email_verified": True,
            "session_id": sid, "session_token": token, "expires_at": expires}
