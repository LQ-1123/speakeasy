"""Push bindings are released when their owning login session ends."""


DDL = """CREATE TABLE IF NOT EXISTS client_device_sessions (
    registration_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT NOT NULL)"""


def release_revoked_devices(db, user_id: str = "") -> int:
    """Runs inside caller's transaction; old databases without bindings are safe."""
    if db.conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('client_device_sessions','v3_sessions')").fetchone()[0] != 2:
        return 0
    sql = ("SELECT b.registration_id,b.user_id FROM client_device_sessions b"
           " LEFT JOIN v3_sessions s ON s.id=b.session_id WHERE"
           " (s.id IS NULL OR s.revoked_at<>'' OR datetime(s.expires_at)<=datetime('now'))")
    args = ()
    if user_id:
        sql += " AND b.user_id=?"
        args = (user_id,)
    rows = db.conn.execute(sql, args).fetchall()
    for row in rows:
        db.conn.execute("DELETE FROM client_devices WHERE registration_id=? AND user_id=?", tuple(row))
        db.conn.execute("DELETE FROM client_device_sessions WHERE registration_id=? AND user_id=?", tuple(row))
    return len(rows)
