"""Long-lived connections retain a session ID, never a bearer token."""
from app.v3.auth_policy import email_code_auth_enabled
from app.v3.account_archive import user_is_available


def session_is_live(db, user_id: str, session_id: str) -> bool:
    if not user_is_available(db, user_id):
        return False
    if not email_code_auth_enabled():
        return True
    if not session_id:
        return False
    with db._lock:
        return db.conn.execute("SELECT 1 FROM v3_sessions WHERE id=? AND user_id=?"
                               " AND revoked_at='' AND datetime(expires_at)>datetime('now')",
                               (session_id, user_id)).fetchone() is not None
