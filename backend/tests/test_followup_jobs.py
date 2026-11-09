import asyncio
from types import SimpleNamespace

from app.v2.db import DB
from app.jobs import DurableJobQueue
from app.v3.followup_jobs import schedule_followups, deliver_followup


def test_followups_are_scheduled_without_open_client_and_only_once(tmp_path):
    db=DB(str(tmp_path/'follow.db')); db.init()
    friend=db.create_friend({'name':'Friend','owner_user_id':'u_alice',
        'open_loops':[{'event':'interview','status':'pending','follow_up_after':'2020-01-01T00:00:00Z'}]})
    queue=DurableJobQueue(db.conn,lock=db._lock)
    from app.v2.memory import MemoryManager
    memory=MemoryManager(db,SimpleNamespace(uses_mock=True))
    services=SimpleNamespace(db=db,memory=memory)
    assert schedule_followups(services,queue)==1
    assert schedule_followups(services,queue)==0
    sent=[]
    async def voice(friend,text): sent.append((friend['owner_user_id'],text))
    async def text(*args): return 'How was your interview?'
    services.chat=SimpleNamespace(store_friend_voice=voice)
    services.memory.followup_message=text
    asyncio.run(queue.run_one_async({'followup':lambda p:deliver_followup(services,p)}))
    assert sent==[('u_alice','How was your interview?')]
    assert db.get_friend(friend['id'],any_owner=True)['open_loops'][0]['status']=='resolved'
    assert schedule_followups(services,queue)==0
    db.close()
