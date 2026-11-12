"""Proactive followups are server work, not a side effect of opening a friend list."""
import hashlib
import json

from app.v3.identity import set_current_user, reset_current_user
from app.v3.account_archive import user_is_available


def schedule_followups(services, queue):
    db=services.db
    with db._lock:
        friends=[db._row_to_dict(r,'friends') for r in db.conn.execute(
            "SELECT * FROM friends WHERE status='active' AND open_loops<>'[]'")]
    scheduled=0
    for friend in friends:
        if not user_is_available(db, friend['owner_user_id']):
            continue
        due=services.memory.due_open_loops(friend)
        if not due: continue
        loop=due[0]
        key='followup:'+hashlib.sha256(json.dumps([friend['id'],loop.get('event'),loop.get('follow_up_after')],sort_keys=True).encode()).hexdigest()
        with db._lock:
            exists=db.conn.execute('SELECT 1 FROM background_jobs WHERE dedupe_key=?',(key,)).fetchone()
        if exists: continue
        queue.enqueue('followup',{'user_id':friend['owner_user_id'],'friend_id':friend['id'],
                                 'event':loop.get('event'),'follow_up_after':loop.get('follow_up_after')},
                      dedupe_key=key,priority=160)
        scheduled+=1
    return scheduled


async def deliver_followup(services,payload):
    if not user_is_available(services.db, payload['user_id']):
        return
    token=set_current_user(payload['user_id'])
    try:
        friend=services.db.get_friend(payload['friend_id'],owner_user_id=payload['user_id'])
        if not friend: return
        due=services.memory.due_open_loops(friend)
        loop=next((x for x in due if x.get('event')==payload['event'] and
                   x.get('follow_up_after')==payload['follow_up_after']),None)
        if not loop:return
        text=await services.memory.followup_message(friend,loop)
        if not user_is_available(services.db, payload['user_id']):
            return
        if not text.strip(): raise RuntimeError('empty-followup')
        await services.chat.store_friend_voice(friend,text)
        services.memory.mark_open_loop_done(friend['id'],loop['index'])
    finally:
        reset_current_user(token)
