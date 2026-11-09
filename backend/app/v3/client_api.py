"""Small authenticated bootstrap, preference, device and user-event API."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import secrets
import time
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.v3.calls import require_user
from app.v2.router import _message_public, _expression_public
from app.v2.timeutil import local_state

router = APIRouter(prefix="/api/v3/client", tags=["client"])
DEFAULT_PREFERENCES = {"messages": True, "interactions": True, "sound": True, "theme": "system"}
EVENT_TICKETS: dict[str, tuple[str, float]] = {}
DDL = """
CREATE TABLE IF NOT EXISTS client_preferences(user_id TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS client_devices(registration_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,
 platform TEXT NOT NULL,vendor TEXT NOT NULL DEFAULT '',enabled INTEGER NOT NULL DEFAULT 1,updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_client_devices_user ON client_devices(user_id);
CREATE TABLE IF NOT EXISTS client_events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,
 user_id TEXT NOT NULL,type TEXT NOT NULL,friend_id TEXT NOT NULL DEFAULT '',post_id TEXT NOT NULL DEFAULT '',
 payload TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,is_read INTEGER NOT NULL DEFAULT 0,
 push_status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_client_events_user_seq ON client_events(user_id,sequence);
CREATE TABLE IF NOT EXISTS client_event_state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
"""


def ensure_client_schema(db):
    with db._lock, db.conn:
        db.conn.executescript(DDL)


def preferences(db, user):
    with db._lock:
        row = db.conn.execute("SELECT value FROM client_preferences WHERE user_id=?", (user,)).fetchone()
    return {**DEFAULT_PREFERENCES, **(json.loads(row[0]) if row else {})}


class PreferencePatch(BaseModel):
    messages: bool | None = None
    interactions: bool | None = None
    sound: bool | None = None
    theme: Literal["system", "light", "dark"] | None = None


@router.get("/preferences")
async def get_preferences(request: Request):
    user = await require_user(request)
    return await asyncio.to_thread(preferences, request.app.state.v2.db, user)


@router.patch("/preferences")
async def set_preferences(body: PreferencePatch, request: Request):
    user = await require_user(request)
    db = request.app.state.v2.db
    def save():
        with db._lock, db.conn:
            data = {**preferences(db, user), **body.model_dump(exclude_none=True)}
            db.conn.execute("INSERT INTO client_preferences VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET value=excluded.value",
                            (user, json.dumps(data)))
            return data
    return await asyncio.to_thread(save)


def bootstrap_data(db, user):
    with db._lock:
        rows = db.conn.execute("SELECT * FROM friends WHERE owner_user_id=? AND status IN ('active','pending_request') ORDER BY last_interaction_at DESC,created_at DESC", (user,)).fetchall()
        latest = db.conn.execute("""SELECT m.* FROM messages m JOIN friends f ON f.id=m.friend_id
          WHERE f.owner_user_id=? AND m.rowid=(SELECT x.rowid FROM messages x WHERE x.friend_id=m.friend_id
          ORDER BY x.created_at DESC,x.rowid DESC LIMIT 1)""", (user,)).fetchall()
        counts = db.conn.execute("SELECT m.friend_id,COUNT(*) n FROM messages m JOIN friends f ON f.id=m.friend_id WHERE f.owner_user_id=? AND m.sender='friend' AND m.is_read=0 GROUP BY m.friend_id", (user,)).fetchall()
        unread_events = db.conn.execute("SELECT COUNT(*) FROM social_events WHERE user_id=? AND is_read=0 AND status<>'declined' AND scheduled_at<=?", (user, datetime.now(timezone.utc).isoformat())).fetchone()[0]
        # Profile is per-user in v3; retain the v2 serialization consumed by the UI.
        profile = db.get_profile(user_id=user)
        last_by_id = {r['friend_id']: db._row_to_dict(r, 'messages') for r in latest}
        unread = {r['friend_id']: int(r['n']) for r in counts}
        active, pending, conversations = [], [], []
        for row in rows:
            friend = db._row_to_dict(row, 'friends')
            state = local_state(friend.get('timezone') or 'UTC')
            last = _message_public(last_by_id.get(friend['id']))
            friend.update(unread=unread.get(friend['id'], 0), last_message=last,
                          local_time=state['local_time'], local_period=state['period'],
                          local_activity=state['activity'], replying=False,
                          greeting=str((last or {}).get('transcript') or ''),
                          greeting_audio=(last or {}).get('audio_url'))
            if friend['status'] == 'active':
                active.append(friend)
                conversations.append({'friend_id': friend['id'], 'last_message': last, 'unread': friend['unread']})
            else:
                pending.append(friend)
        expressions = [_expression_public(e) for e in db.list_expressions()]
    return {'profile': profile, 'friends': active, 'requests': pending,
            'conversations': conversations, 'expressions': expressions, 'unread_events': unread_events,
            'capabilities': {'call_protocol': 1, 'event_protocol': 1, 'min_client_version': '4.0.0'}}


@router.get('/bootstrap')
async def bootstrap(request: Request):
    user = await require_user(request)
    return await asyncio.to_thread(bootstrap_data, request.app.state.v2.db, user)


class DeviceIn(BaseModel):
    registration_id: str = Field(min_length=8, max_length=256, pattern=r'^[A-Za-z0-9_:-]+$')
    platform: Literal['android'] = 'android'
    vendor: str = Field(default='', max_length=40)
    enabled: bool = True


@router.post('/devices')
async def register_device(body: DeviceIn, request: Request):
    user = await require_user(request)
    db = request.app.state.v2.db
    def save():
        with db._lock, db.conn:
            existing = db.conn.execute('SELECT user_id FROM client_devices WHERE registration_id=?', (body.registration_id,)).fetchone()
            if existing and existing[0] != user:
                raise HTTPException(409, '设备已绑定其他身份')
            db.conn.execute('INSERT INTO client_devices VALUES (?,?,?,?,?,?) ON CONFLICT(registration_id) DO UPDATE SET enabled=excluded.enabled,vendor=excluded.vendor,updated_at=excluded.updated_at',
                            (body.registration_id,user,body.platform,body.vendor,int(body.enabled),time.time()))
    await asyncio.to_thread(save)
    return {'registered': True}


@router.post('/event-ticket')
async def event_ticket(request: Request):
    user = await require_user(request)
    now = time.time()
    for key, (_, expiry) in list(EVENT_TICKETS.items()):
        if expiry < now:
            EVENT_TICKETS.pop(key, None)
    if len(EVENT_TICKETS) >= 2000:
        raise HTTPException(429, '连接繁忙')
    ticket = secrets.token_urlsafe(32)
    EVENT_TICKETS[hashlib.sha256(ticket.encode()).hexdigest()] = (user, now+60)
    db=request.app.state.v2.db
    with db._lock:
        sequence=db.conn.execute('SELECT COALESCE(MAX(sequence),0) FROM client_events WHERE user_id=?',(user,)).fetchone()[0]
    return {'ticket': ticket, 'ws_path': '/api/v3/client/events', 'expires_in': 60, 'current_sequence':sequence}


def event_rows(db, user, after):
    with db._lock:
        rows = db.conn.execute('SELECT * FROM client_events WHERE user_id=? AND sequence>? ORDER BY sequence LIMIT 100', (user,after)).fetchall()
    return [{'id': r['id'], 'sequence': r['sequence'], 'type':r['type'], 'friend_id':r['friend_id'],
             'post_id':r['post_id'], 'payload':json.loads(r['payload']), 'created_at':r['created_at'], 'is_read':bool(r['is_read'])} for r in rows]


@router.websocket('/events')
async def event_socket(ws: WebSocket):
    await ws.accept()
    try:
        auth = await asyncio.wait_for(ws.receive_json(), 5)
        entry = EVENT_TICKETS.pop(hashlib.sha256(str(auth.get('ticket') or '').encode()).hexdigest(), None)
        if auth.get('type') != 'auth' or not entry or entry[1] < time.time():
            await ws.close(code=4401)
            return
        user, _ = entry
        cursor = max(0, int(auth.get('last_sequence') or 0))
    except (ValueError, AttributeError, asyncio.TimeoutError, WebSocketDisconnect):
        with contextlib.suppress(Exception):
            await ws.close(code=4401)
        return
    db = ws.app.state.v2.db
    try:
        await ws.send_json({'type':'ready','sequence':cursor})
        while True:
            rows = await asyncio.to_thread(event_rows, db, user, cursor)
            for event in rows:
                await ws.send_json(event)
                cursor = event['sequence']
            try:
                message = await asyncio.wait_for(ws.receive_json(), 1)
                if message.get('type') == 'ping':
                    await ws.send_json({'type':'pong','sequence':cursor})
            except asyncio.TimeoutError:
                continue
    except (WebSocketDisconnect, RuntimeError, ValueError):
        return


@router.patch('/events/{event_id}/read')
async def read_event(event_id: str, request: Request):
    user = await require_user(request)
    db = request.app.state.v2.db
    def update():
        with db._lock, db.conn:
            row = db.conn.execute('SELECT * FROM client_events WHERE id=? AND user_id=?',(event_id,user)).fetchone()
            if not row:
                raise HTTPException(404,'通知不存在')
            db.conn.execute("UPDATE client_events SET is_read=1,push_status='suppressed' WHERE id=?",(event_id,))
            if event_id.startswith('social:'):
                db.conn.execute('UPDATE social_events SET is_read=1 WHERE id=? AND user_id=?',(event_id[7:],user))
            elif event_id.startswith('message:'):
                db.conn.execute('UPDATE messages SET is_read=1 WHERE id=? AND friend_id IN (SELECT id FROM friends WHERE owner_user_id=?)',(event_id[8:],user))
    await asyncio.to_thread(update)
    return {'read': True}
