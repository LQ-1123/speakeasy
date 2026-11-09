from app.v2.db import DB


def test_cursor_does_not_skip_messages_with_equal_timestamp(tmp_path):
    db=DB(str(tmp_path/'messages.db'))
    db.init()
    friend=db.create_friend({'name':'Friend'})
    ids=[db.add_message(friend['id'],'user',str(i),created_at='2026-01-01T00:00:00+00:00')['id'] for i in range(7)]
    got=[]
    before=None
    for _ in range(4):
        page=db.list_messages(friend['id'],limit=3,before=before)
        if not page: break
        got=[m['id'] for m in page]+got
        before=page[0]['created_at']+'|'+page[0]['id']
    assert got==ids
    db.close()
