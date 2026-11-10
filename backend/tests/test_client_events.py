from types import SimpleNamespace

import pytest

from app.v2.db import DB
from app.v3.schema import ensure_schema
from app.v3.client_api import ensure_client_schema, event_rows
from app.v3.client_events import collect_events, dispatch_push
from app.v3.identity import set_current_user, reset_current_user


@pytest.fixture
def db(tmp_path):
    db=DB(str(tmp_path/'events.db'))
    db.init()
    ensure_schema(db,seed=False)
    ensure_client_schema(db)
    yield db
    db.close()


def test_collect_is_idempotent_and_separates_user_events(db):
    collect_events(db)  # First launch establishes historical baseline.
    for user in ('u_a','u_b'):
        ctx=set_current_user(user)
        friend=db.create_friend({'name':user})
        db.add_message(friend['id'],'friend','hello',is_read=False)
        reset_current_user(ctx)
    collect_events(db)
    collect_events(db)
    a=event_rows(db,'u_a',0)
    b=event_rows(db,'u_b',0)
    assert len(a)==len(b)==1
    assert a[0]['id']!=b[0]['id']
    assert a[0]['type']=='message'


def test_historical_messages_do_not_notify_on_first_install(db):
    ctx=set_current_user('u_a')
    friend=db.create_friend({'name':'friend'})
    db.add_message(friend['id'],'friend','old',is_read=False)
    reset_current_user(ctx)
    collect_events(db)
    assert event_rows(db,'u_a',0)==[]


@pytest.mark.asyncio
async def test_missing_push_credentials_never_marks_delivery_sent(db, monkeypatch):
    collect_events(db)
    ctx=set_current_user('u_a')
    friend=db.create_friend({'name':'friend'})
    db.add_message(friend['id'],'friend','hello',is_read=False)
    reset_current_user(ctx)
    collect_events(db)
    monkeypatch.delenv('JPUSH_APP_KEY',raising=False)
    monkeypatch.delenv('JPUSH_MASTER_SECRET',raising=False)
    from app.config import get_settings  # noqa: PLC0415

    blank = get_settings().model_copy(update={'jpush_app_key': '', 'jpush_master_secret': ''})
    monkeypatch.setattr('app.config.get_settings', lambda: blank)
    assert await dispatch_push(db)==0
    # No credentials is a terminal state, not a retry loop: the worker marks the
    # outbox row instead of leaving it pending forever.
    assert db.conn.execute('SELECT push_status FROM client_events').fetchone()[0]=='unconfigured'


@pytest.mark.asyncio
async def test_read_message_is_suppressed_before_push(db, monkeypatch):
    collect_events(db)
    ctx=set_current_user('u_a')
    friend=db.create_friend({'name':'friend'})
    msg=db.add_message(friend['id'],'friend','hello',is_read=False)
    db.mark_read(friend['id'])
    reset_current_user(ctx)
    collect_events(db)
    assert event_rows(db,'u_a',0)==[]
