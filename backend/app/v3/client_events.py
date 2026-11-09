"""Durable user-event outbox and bounded JPush delivery worker."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

import httpx

from app.v3.client_api import preferences

log = logging.getLogger(__name__)


def collect_events(db) -> int:
    """Transactional high-water marks: retrying collection never duplicates an event.

    Social rows are collected when they become due, including records scheduled before
    the watermark. Bootstrap skips historical messages but keeps future social events.
    """
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    with db._lock, db.conn:
        state = db.conn.execute("SELECT value FROM client_event_state WHERE key='message_row' LIMIT 1").fetchone()
        if state is None:
            latest = db.conn.execute('SELECT COALESCE(MAX(rowid),0) FROM messages').fetchone()[0]
            db.conn.execute("INSERT INTO client_event_state VALUES ('message_row',?)", (str(latest),))
            db.conn.execute("INSERT INTO client_event_state VALUES ('installed_at',?)", (now,))
            return 0
        installed = db.conn.execute("SELECT value FROM client_event_state WHERE key='installed_at'").fetchone()[0]
        rows = db.conn.execute("""SELECT m.rowid AS source_row,m.*,f.owner_user_id,f.name FROM messages m
             JOIN friends f ON f.id=m.friend_id WHERE m.rowid>? ORDER BY m.rowid LIMIT 500""", (int(state[0]),)).fetchall()
        count = 0
        for row in rows:
            if row['sender'] == 'friend' and not row['is_read'] and row['msg_type'] != 'call_summary':
                cursor = db.conn.execute("""INSERT OR IGNORE INTO client_events
                    (id,user_id,type,friend_id,payload,created_at,next_attempt) VALUES (?,?,'message',?,?,?,?)""",
                    ('message:'+row['id'],row['owner_user_id'],row['friend_id'],
                     json.dumps({'name':row['name'],'text':row['transcript'][:180], 'route':'#/chat/'+row['friend_id']},ensure_ascii=False),row['created_at'],time.time()+2))
                count += cursor.rowcount
        if rows:
            db.conn.execute("UPDATE client_event_state SET value=? WHERE key='message_row'", (str(rows[-1]['source_row']),))
        social = db.conn.execute("""SELECT s.*,p.nickname FROM social_events s LEFT JOIN personas p ON p.id=s.persona_id
             WHERE s.status<>'declined' AND s.is_read=0 AND s.scheduled_at<=? AND s.scheduled_at>=?
             AND NOT EXISTS(SELECT 1 FROM client_events e WHERE e.id='social:'||s.id)
             ORDER BY s.scheduled_at LIMIT 100""",(now,installed)).fetchall()
        for row in social:
            kind = 'friend_request' if row['event_type']=='friend_request' else 'social'
            detail = json.loads(row['payload'] or '{}')
            detail.update(name=row['nickname'] or '',event_type=row['event_type'],
                          route='#/events',source_event_id=row['id'])
            cursor=db.conn.execute("""INSERT OR IGNORE INTO client_events
              (id,user_id,type,post_id,payload,created_at,next_attempt) VALUES (?,?,?,?,?,?,?)""",
              ('social:'+row['id'],row['user_id'],kind,row['post_id'],json.dumps(detail,ensure_ascii=False),row['created_at'],time.time()+2))
            count+=cursor.rowcount
        # Reads from either the old or new API converge to the same outbox state.
        db.conn.execute("""UPDATE client_events SET is_read=1,push_status='suppressed'
           WHERE is_read=0 AND ((id LIKE 'message:%' AND EXISTS(SELECT 1 FROM messages m WHERE 'message:'||m.id=client_events.id AND m.is_read=1))
           OR (id LIKE 'social:%' AND EXISTS(SELECT 1 FROM social_events s WHERE 'social:'||s.id=client_events.id AND (s.is_read=1 OR s.status='declined'))))""")
        db.conn.execute("DELETE FROM client_events WHERE is_read=1 AND datetime(created_at)<datetime('now','-7 days')")
        db.conn.execute("UPDATE client_events SET push_status='expired' WHERE push_status='pending' AND datetime(created_at)<datetime('now','-1 day')")
        return count


async def dispatch_push(db) -> int:
    from app.config import get_settings
    settings = get_settings()
    key = (os.getenv('JPUSH_APP_KEY') or settings.jpush_app_key).strip()
    secret = (os.getenv('JPUSH_MASTER_SECRET') or settings.jpush_master_secret).strip()
    if not key or not secret:
        return 0
    with db._lock:
        rows = [dict(r) for r in db.conn.execute("SELECT * FROM client_events WHERE push_status='pending' AND is_read=0 AND next_attempt<=? ORDER BY sequence LIMIT 20",(time.time(),)).fetchall()]
    sent = 0
    async with httpx.AsyncClient(timeout=8, auth=(key,secret)) as client:
        for event in rows:
            pref = await asyncio.to_thread(preferences,db,event['user_id'])
            enabled = pref['messages'] if event['type']=='message' else pref['interactions']
            with db._lock:
                devices = [r[0] for r in db.conn.execute('SELECT registration_id FROM client_devices WHERE user_id=? AND enabled=1',(event['user_id'],)).fetchall()]
                unread = db.conn.execute('SELECT is_read FROM client_events WHERE id=?',(event['id'],)).fetchone()
            if not enabled or not unread or unread[0]:
                with db._lock,db.conn:
                    db.conn.execute("UPDATE client_events SET push_status='suppressed' WHERE id=?",(event['id'],))
                continue
            if not devices:
                with db._lock,db.conn:
                    db.conn.execute("UPDATE client_events SET next_attempt=? WHERE id=?",(time.time()+300,event['id']))
                continue
            info=json.loads(event['payload'])
            title=info.get('name') or 'SpeakEasy'
            # Do not send raw conversation contents to the lock-screen by default.
            alert='发来一条消息' if event['type']=='message' else '有新的动态互动'
            android={'alert':alert,'title':title,'extras':{'event_id':event['id'],'route':info.get('route','#/events')},'channel_id':'speakeasy_messages' if event['type']=='message' else 'speakeasy_social'}
            if not pref['sound']:
                android['channel_id'] += '_silent'
            request={'platform':'android','audience':{'registration_id':devices},
                     'notification':{'android':android},'options':{'time_to_live':3600}}
            status='pending'
            try:
                response=await client.post('https://api.jpush.cn/v3/push',json=request)
                if response.is_success:
                    status='sent'
                    sent+=1
                elif response.status_code in (400,401,403):
                    status='failed'
                else:
                    response.raise_for_status()
            except httpx.HTTPError:
                pass
            attempts=event['attempts']+1
            if attempts>=5 and status=='pending':
                status='failed'
            with db._lock,db.conn:
                db.conn.execute('UPDATE client_events SET push_status=?,attempts=?,next_attempt=? WHERE id=? AND is_read=0',
                               (status,attempts,time.time()+min(300,5*2**attempts),event['id']))
    return sent


async def event_worker(db, service=None):
    while True:
        try:
            if service is not None:
                def materialize():
                    now=datetime.now(timezone.utc).isoformat(timespec='seconds')
                    with db._lock:
                        owners=[r[0] for r in db.conn.execute('SELECT DISTINCT user_id FROM social_events WHERE processed_at IS NULL AND scheduled_at<=? LIMIT 100',(now,))]
                    for user in owners:
                        service.materialize_due_events(user)
                await asyncio.to_thread(materialize)
            await asyncio.to_thread(collect_events,db)
            await dispatch_push(db)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('event outbox iteration failed: %s',type(exc).__name__)
        await asyncio.sleep(2)
