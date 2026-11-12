"""Durable user-event outbox and bounded JPush delivery worker."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

import httpx

from app.v3 import teen
from app.v3.client_api import preferences
from app.v3.account_archive import user_is_available

log = logging.getLogger(__name__)

#: 青少年推送抑制后的重试间隔：保留 pending，等门禁放行或下个活跃时段再推。
MINOR_PUSH_RETRY_S = 3600

# A missing deployment credential is a permanent state until the process restarts
# with keys; warn once instead of logging on every worker tick.
_unconfigured_warned = False


def mark_push_unconfigured(db) -> int:
    """Move every pending outbox row to a terminal unconfigured state.

    Idempotent and covers rows created before the credential check was added:
    the worker must never spin on the same pending rows forever.
    """
    with db._lock, db.conn:
        return db.conn.execute("UPDATE client_events SET push_status='unconfigured' WHERE push_status='pending'").rowcount


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
        rows = db.conn.execute("""SELECT m.rowid AS source_row,m.*,f.owner_user_id,f.name,f.avatar_url FROM messages m
             JOIN friends f ON f.id=m.friend_id WHERE m.rowid>? ORDER BY m.rowid LIMIT 500""", (int(state[0]),)).fetchall()
        count = 0
        for row in rows:
            if not user_is_available(db, row['owner_user_id']):
                continue
            if row['sender'] == 'friend' and not row['is_read'] and row['msg_type'] != 'call_summary':
                cursor = db.conn.execute("""INSERT OR IGNORE INTO client_events
                    (id,user_id,type,friend_id,payload,created_at,next_attempt) VALUES (?,?,'message',?,?,?,?)""",
                    ('message:'+row['id'],row['owner_user_id'],row['friend_id'],
                     json.dumps({'name':row['name'],'text':row['transcript'][:180],
                                 'avatar':row['avatar_url'] or '',
                                 'route':'#/chat/'+row['friend_id']},ensure_ascii=False),row['created_at'],time.time()+2))
                count += cursor.rowcount
        if rows:
            db.conn.execute("UPDATE client_event_state SET value=? WHERE key='message_row'", (str(rows[-1]['source_row']),))
        social = db.conn.execute("""SELECT s.*,p.nickname FROM social_events s LEFT JOIN personas p ON p.id=s.persona_id
             WHERE s.status<>'declined' AND s.is_read=0 AND s.scheduled_at<=? AND s.scheduled_at>=?
             AND NOT EXISTS(SELECT 1 FROM client_events e WHERE e.id='social:'||s.id)
             ORDER BY s.scheduled_at LIMIT 100""",(now,installed)).fetchall()
        for row in social:
            if not user_is_available(db, row['user_id']):
                continue
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
    global _unconfigured_warned
    from app.config import get_settings
    from app.v3 import push_config
    from app.v3.device_sessions import release_revoked_devices
    with db._lock, db.conn:
        release_revoked_devices(db)

    # 凭据来源（env > 管理台保存 > .env）统一由 push_config 解析。
    # 以前这里内联读 env/settings，导致管理台根本改不了推送配置。
    creds = push_config.effective()
    key = creds["app_key"]
    secret = creds["master_secret"]
    # settings 后面还要用来拼通知大图的 base url，别删。
    settings = get_settings()
    if not key or not secret:
        marked = mark_push_unconfigured(db)
        if not _unconfigured_warned:
            _unconfigured_warned = True
            log.warning('push outbox unconfigured: JPUSH_APP_KEY/JPUSH_MASTER_SECRET are missing; '
                        '%d pending event(s) marked push_status=unconfigured', marked)
        return 0
    with db._lock:
        rows = [dict(r) for r in db.conn.execute("SELECT * FROM client_events WHERE push_status='pending' AND is_read=0 AND next_attempt<=? ORDER BY sequence LIMIT 20",(time.time(),)).fetchall()]
    sent = 0
    async with httpx.AsyncClient(timeout=8, auth=(key,secret)) as client:
        for event in rows:
            if not user_is_available(db, event['user_id']):
                with db._lock, db.conn:
                    db.conn.execute("UPDATE client_events SET push_status='suppressed' WHERE id=?", (event['id'],))
                continue
            # 青少年模式：宵禁 / 当日超时 / 未核验 / 未绑监护期间不主动推送。
            # 不标 failed/suppressed，只把 next_attempt 推后，等允许时再发。
            if teen.push_suppressed(db, event['user_id']):
                with db._lock, db.conn:
                    db.conn.execute('UPDATE client_events SET next_attempt=? WHERE id=?',
                                    (time.time() + MINOR_PUSH_RETRY_S, event['id']))
                continue
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
            title='Sonder'
            text=(info.get('text') or '').strip()
            # Real chat apps show the message preview. Keep it single-line and short
            # so the collapsed notification stays readable on every OEM skin.
            alert='你有一条新消息' if event['type']=='message' else '你有新的动态互动'
            if len(alert) > 120:
                alert = alert[:119] + '…'
            android={'alert':alert,'title':title,'extras':{'event_id':event['id'],'route':info.get('route','#/events'), 'user_id':event['user_id']},
                     'channel_id':'speakeasy_messages' if event['type']=='message' else 'speakeasy_social'}
            # 通知栏右侧大图：优先发信人/AI 头像；没有形象时用 Dynamic 渐变图标。
            avatar=(info.get('avatar') or '').strip()
            base=(os.getenv('PUBLIC_BASE_URL') or settings.public_base_url).rstrip('/')
            if avatar.startswith('http'):
                avatar_png=avatar
            elif avatar:
                avatar_png=base + avatar.replace('.svg', '.png')
            else:
                avatar_png=base + '/icons/brand-512.png'
            android['large_icon']=avatar_png
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
                        if not user_is_available(db, user):
                            continue
                        service.materialize_due_events(user)
                await asyncio.to_thread(materialize)
            await asyncio.to_thread(collect_events,db)
            await dispatch_push(db)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('event outbox iteration failed: %s',type(exc).__name__)
        await asyncio.sleep(2)
