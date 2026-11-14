"""Small authenticated bootstrap, preference, device and user-event API."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.v3 import teen, wakeups
from app.v3.calls import minor_gate, require_user
from app.v2.router import _message_public, _expression_public
from app.v2.timeutil import local_state

router = APIRouter(prefix="/api/v3/client", tags=["client"])
DEFAULT_PREFERENCES = {
    "messages": True,
    "interactions": True,
    "sound": True,
    "theme": "system",
    # 合作推荐（广告）默认关闭：只有成年且主动开启的用户才可能看到
    "ads": False,
}
EVENT_TICKETS: dict[str, tuple[str, float, str]] = {}
#: 事件长连接的兜底轮询间隔（秒）。平时由 :mod:`app.v3.wakeups` 按需唤醒；这个定时只负责
#: 两件事：万一漏掉一次唤醒也能在这个时间内补上，以及周期性复核会话是否仍然有效
#: （与通话连接的 10 秒复核一致）。
EVENT_POLL_FALLBACK_S = max(1.0, float(os.getenv("SE_EVENT_POLL_FALLBACK_S", "10") or 10))
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
CREATE INDEX IF NOT EXISTS idx_client_events_unread ON client_events(id) WHERE is_read=0;
CREATE INDEX IF NOT EXISTS idx_client_events_pending ON client_events(sequence) WHERE push_status='pending';
CREATE TABLE IF NOT EXISTS client_event_state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
"""


def ensure_client_schema(db):
    with db._lock, db.conn:
        db.conn.executescript(DDL)
        from app.v3.device_sessions import DDL as binding_ddl
        db.conn.execute(binding_ddl)


def preferences(db, user):
    with db._lock:
        row = db.conn.execute("SELECT value FROM client_preferences WHERE user_id=?", (user,)).fetchone()
    return {**DEFAULT_PREFERENCES, **(json.loads(row[0]) if row else {})}


class PreferencePatch(BaseModel):
    messages: bool | None = None
    interactions: bool | None = None
    sound: bool | None = None
    theme: Literal["system", "light", "dark"] | None = None
    ads: bool | None = None


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
        # 每个好友一次索引定位取最后一条；旧写法是「对好友的每条消息各跑一次子查询」，
        # 成本随聊天记录总量增长。
        latest = db.conn.execute("""SELECT m.* FROM friends f JOIN messages m
          ON m.rowid=(SELECT x.rowid FROM messages x WHERE x.friend_id=f.id
          ORDER BY x.created_at DESC,x.rowid DESC LIMIT 1) WHERE f.owner_user_id=?""", (user,)).fetchall()
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
    minor_gate(request.app.state.v2.db, user, feature="feed")
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
    from app.v3.security import _session_from_request
    from app.v3 import accounts
    from app.v3.auth_policy import email_code_auth_enabled
    from app.v3.device_sessions import release_revoked_devices
    session_token, _ = _session_from_request(request)
    session = await asyncio.to_thread(accounts.authenticate_session, db, session_token) if session_token else None
    def save():
        with db._lock, db.conn:
            if email_code_auth_enabled():
                if session is None:
                    raise HTTPException(401, '请先登录')
                release_revoked_devices(db)
            existing = db.conn.execute('SELECT user_id FROM client_devices WHERE registration_id=?', (body.registration_id,)).fetchone()
            if existing and existing[0] != user:
                raise HTTPException(409, '设备已绑定其他身份')
            db.conn.execute('INSERT INTO client_devices VALUES (?,?,?,?,?,?) ON CONFLICT(registration_id) DO UPDATE SET enabled=excluded.enabled,vendor=excluded.vendor,updated_at=excluded.updated_at',
                            (body.registration_id,user,body.platform,body.vendor,int(body.enabled),time.time()))
            if session:
                db.conn.execute('INSERT INTO client_device_sessions VALUES (?,?,?) ON CONFLICT(registration_id) DO UPDATE SET user_id=excluded.user_id,session_id=excluded.session_id',
                                (body.registration_id, user, session['session_id']))
    await asyncio.to_thread(save)
    return {'registered': True}


@router.post('/event-ticket')
async def event_ticket(request: Request):
    user = await require_user(request)
    minor_gate(request.app.state.v2.db, user, feature="feed")
    now = time.time()
    for key, entry in list(EVENT_TICKETS.items()):
        if entry[1] < now:
            EVENT_TICKETS.pop(key, None)
    if len(EVENT_TICKETS) >= 2000:
        raise HTTPException(429, '连接繁忙')
    ticket = secrets.token_urlsafe(32)
    EVENT_TICKETS[hashlib.sha256(ticket.encode()).hexdigest()] = (user, now+60, getattr(request.state, 'session_id', ''))
    db=request.app.state.v2.db
    with db._lock:
        sequence=db.conn.execute('SELECT COALESCE(MAX(sequence),0) FROM client_events WHERE user_id=?',(user,)).fetchone()[0]
    return {'ticket': ticket, 'ws_path': '/api/v3/client/events', 'expires_in': 60, 'current_sequence':sequence}


def event_rows(db, user, after):
    with db._lock:
        rows = db.conn.execute('SELECT * FROM client_events WHERE user_id=? AND sequence>? ORDER BY sequence LIMIT 100', (user,after)).fetchall()
    return [{'id': r['id'], 'sequence': r['sequence'], 'type':r['type'], 'friend_id':r['friend_id'],
             'post_id':r['post_id'], 'payload':json.loads(r['payload']), 'created_at':r['created_at'], 'is_read':bool(r['is_read'])} for r in rows]


def _poll_events(db, user, session_id, cursor):
    """一次线程切换里做完两件事：复核会话仍然有效，再取这条连接还没看过的事件。"""
    from app.v3.session_liveness import session_is_live
    if not session_is_live(db, user, session_id):
        return False, []
    return True, event_rows(db, user, cursor)


def _discard_task(task) -> None:
    """收尾：取消还在等的任务，并把已结束任务的异常取走（否则退出时会刷未处理异常告警）。"""
    if task is None:
        return
    if not task.done():
        task.cancel()
    elif not task.cancelled():
        task.exception()


@router.websocket('/events')
async def event_socket(ws: WebSocket):
    await ws.accept()
    try:
        auth = await asyncio.wait_for(ws.receive_json(), 5)
        entry = EVENT_TICKETS.pop(hashlib.sha256(str(auth.get('ticket') or '').encode()).hexdigest(), None)
        if auth.get('type') != 'auth' or not entry or entry[1] < time.time():
            await ws.close(code=4401)
            return
        user, _, session_id = entry
        cursor = max(0, int(auth.get('last_sequence') or 0))
    except (ValueError, AttributeError, asyncio.TimeoutError, WebSocketDisconnect):
        with contextlib.suppress(Exception):
            await ws.close(code=4401)
        return
    db = ws.app.state.v2.db
    from app.v3.session_liveness import session_is_live
    if not await asyncio.to_thread(session_is_live, db, user, session_id):
        await ws.close(code=4401)
        return
    # 青少年模式：事件流属于"主动内容"，建连前再判一次（ticket 可能在允许时段签发）。
    try:
        teen.enforce(db, user, feature="feed")
    except PermissionError as exc:
        with contextlib.suppress(Exception):
            await ws.close(code=4003, reason=f"minor:{exc}"[:120])
        return
    # 连接平时睡着：有新事件 / 会话被撤销时由 wakeups 唤醒，另有 EVENT_POLL_FALLBACK_S 兜底。
    # （旧实现每秒醒一次、每次两趟线程切换，在线人数一多全是空转。）
    waiter = wakeups.subscribe(user)
    receiver = wake = None
    try:
        await ws.send_json({'type':'ready','sequence':cursor})
        poll = True
        while True:
            if poll:
                live, rows = await asyncio.to_thread(_poll_events, db, user, session_id, cursor)
                if not live:
                    await ws.close(code=4401)
                    return
                for event in rows:
                    await ws.send_json(event)
                    cursor = event['sequence']
                if len(rows) >= 100:
                    continue  # 一页取满说明还有积压：先取完再睡
            if receiver is None:
                receiver = asyncio.create_task(ws.receive_json())
            if wake is None:
                wake = asyncio.create_task(waiter.event.wait())
            done, _ = await asyncio.wait({receiver, wake}, timeout=EVENT_POLL_FALLBACK_S,
                                         return_when=asyncio.FIRST_COMPLETED)
            poll = not done  # 谁都没动静 = 兜底定时到点
            if wake in done:
                # 先清标志再查库：清之后才到的通知会让下一轮立刻再醒一次，不会漏
                waiter.event.clear()
                wake, poll = None, True
            if receiver in done:
                message, receiver = receiver.result(), None
                if isinstance(message, dict) and message.get('type') == 'ping':
                    await ws.send_json({'type':'pong','sequence':cursor})
    except (WebSocketDisconnect, RuntimeError, ValueError):
        return
    finally:
        wakeups.unsubscribe(user, waiter)
        _discard_task(receiver)
        _discard_task(wake)


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
