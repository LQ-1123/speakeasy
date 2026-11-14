"""Explicit archival of legacy identities; business data is never reassigned."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any


def _table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def ensure_archive_schema(db: Any) -> None:
    with db._lock, db.conn:
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(v3_users)")}
        if cols and "archived_at" not in cols:
            db.conn.execute("ALTER TABLE v3_users ADD COLUMN archived_at TEXT NOT NULL DEFAULT ''")
        db.conn.execute("""CREATE TABLE IF NOT EXISTS v3_identity_archives (
            user_id TEXT PRIMARY KEY, archived_at TEXT NOT NULL, migration_id TEXT NOT NULL,
            account_json TEXT NOT NULL DEFAULT '{}', devices_json TEXT NOT NULL DEFAULT '[]')""")


def is_archived(db: Any, user_id: str) -> bool:
    """Read-only, including on a pre-migration DB."""
    try:
        with db._lock:
            row = db.conn.execute("SELECT archived_at FROM v3_users WHERE id=?", (user_id,)).fetchone()
        return bool(row and row[0])
    except sqlite3.OperationalError as exc:
        if "no such" in str(exc):
            return False
        raise


def user_is_available(db: Any, user_id: str) -> bool:
    if is_archived(db, user_id):
        return False
    from app.v3.auth_policy import email_code_auth_enabled
    if not email_code_auth_enabled():
        return True
    try:
        with db._lock:
            row = db.conn.execute("""SELECT 1 FROM v3_accounts a JOIN v3_users u ON u.id=a.user_id
                WHERE a.user_id=? AND a.status='active' AND a.email_verified_at<>'' AND u.archived_at=''""",
                                  (user_id,)).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False


def archive_users(db: Any, user_ids: list[str], *, migration_id: str,
                  expected_created_at: dict[str, str] | None = None) -> dict[str, int]:
    """Archive an explicit reviewed ID list atomically; caller backs up first.

    Run with workers stopped. IDs created after the reviewed manifest or changed
    since it was prepared are rejected. A repeat of the same migration is harmless.
    """
    ids = sorted(set(user_ids))
    if not ids or not migration_id.strip():
        raise ValueError("需要明确的身份清单和迁移标识")
    ensure_archive_schema(db)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    archived = 0
    with db._lock, db.conn:
        db.conn.execute("BEGIN IMMEDIATE")
        for uid in ids:
            row = db.conn.execute("SELECT * FROM v3_users WHERE id=?", (uid,)).fetchone()
            if row is None:
                raise ValueError(f"身份不存在: {uid}")
            if expected_created_at is not None and (uid not in expected_created_at or
                    str(row["created_at"] or "") != expected_created_at[uid]):
                raise ValueError(f"身份与演练清单不一致: {uid}")
            if row["archived_at"]:
                continue
            account = (db.conn.execute("SELECT * FROM v3_accounts WHERE user_id=?", (uid,)).fetchone()
                       if _table(db.conn, "v3_accounts") else None)
            devices = (db.conn.execute("SELECT * FROM client_devices WHERE user_id=?", (uid,)).fetchall()
                       if _table(db.conn, "client_devices") else [])
            db.conn.execute("INSERT INTO v3_identity_archives VALUES (?,?,?,?,?)",
                            (uid, now, migration_id, json.dumps(dict(account) if account else {}, ensure_ascii=False),
                             json.dumps([dict(d) for d in devices], ensure_ascii=False)))
            db.conn.execute("UPDATE v3_users SET archived_at=?,token_hash='' WHERE id=?", (now, uid))
            if _table(db.conn, "v3_sessions"):
                db.conn.execute("UPDATE v3_sessions SET revoked_at=? WHERE user_id=? AND revoked_at=''", (now, uid))
            if _table(db.conn, "v3_email_tokens"):
                db.conn.execute("UPDATE v3_email_tokens SET used_at=? WHERE user_id=? AND used_at=''", (now, uid))
            if _table(db.conn, "v3_accounts"):
                db.conn.execute("DELETE FROM v3_accounts WHERE user_id=?", (uid,))
            if _table(db.conn, "client_devices"):
                # Current ownership must be released, not just enabled=0 (the PK
                # would otherwise prevent a fresh account using the same phone).
                db.conn.execute("DELETE FROM client_devices WHERE user_id=?", (uid,))
            if _table(db.conn, "client_device_sessions"):
                db.conn.execute("DELETE FROM client_device_sessions WHERE user_id=?", (uid,))
            if _table(db.conn, "client_events"):
                db.conn.execute("UPDATE client_events SET push_status='suppressed' WHERE user_id=? AND push_status<>'sent'", (uid,))
            if _table(db.conn, "social_events"):
                db.conn.execute("UPDATE social_events SET processed_at=? WHERE user_id=? AND processed_at IS NULL", (now, uid))
            archived += 1
        if _table(db.conn, "background_jobs"):
            selected = set(ids)
            jobs = db.conn.execute("SELECT id,payload FROM background_jobs WHERE status IN ('queued','retry','running')").fetchall()
            for job in jobs:
                try:
                    owner = json.loads(job["payload"]).get("user_id")
                except (ValueError, AttributeError):
                    continue
                if owner in selected:
                    db.conn.execute("UPDATE background_jobs SET status='cancelled',lease_until=0,updated_at=?,last_error='identity_archived' WHERE id=?",
                                    (time.time(), job["id"]))
    # Sleeping event sockets re-check their (now revoked) session right away.
    from app.v3 import wakeups
    wakeups.notify(ids)
    return {"archived": archived, "selected": len(ids)}
