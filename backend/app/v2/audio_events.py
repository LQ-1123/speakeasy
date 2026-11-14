"""Persist audio readiness events without creating push notifications."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from app.v3 import wakeups


def publish(db, friend: dict, message_id: str, status: str) -> None:
    owner = str(friend.get('owner_user_id') or 'local')
    with db._lock, db.conn:
        if db.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='client_events'").fetchone() is None:
            return
        kind = 'audio_ready' if status == 'ready' else 'audio_failed'
        db.conn.execute('INSERT INTO client_events '
                        '(id,user_id,type,friend_id,payload,created_at,is_read,push_status) '
                        'VALUES (?,?,?,?,?,?,1,?)',
                        (f'{kind}:{message_id}:{time.time_ns()}', owner, kind, friend['id'],
                         json.dumps({'message_id': message_id}),
                         datetime.now(timezone.utc).isoformat(), 'suppressed'))
    wakeups.notify([owner])
