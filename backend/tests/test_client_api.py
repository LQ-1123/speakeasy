from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.v2.db import DB
from app.v3.client_api import router, ensure_client_schema
from app.v3.schema import ensure_schema
from app.v3.security import register_user
from app.v3.identity import set_current_user, reset_current_user


@pytest.fixture
def fixture(tmp_path):
    db = DB(str(tmp_path / 'client.db'))
    db.init()
    ensure_schema(db, seed=False)
    ensure_client_schema(db)
    user = register_user(db, 'device-alice-1')
    other = register_user(db, 'device-bob-2')
    app = FastAPI()
    app.state.v2 = SimpleNamespace(db=db)
    app.include_router(router)
    def headers(u):
        return {'X-User-Id': u['user_id'], 'X-User-Token': u['token']}
    with TestClient(app) as client:
        yield client, db, user, other, headers
    db.close()


def test_preferences_persist_and_are_isolated(fixture):
    client, db, alice, bob, headers = fixture
    assert client.get('/api/v3/client/preferences').status_code == 401
    assert client.get('/api/v3/client/preferences',headers={'X-User-Id':'   '}).status_code == 401
    saved = client.patch('/api/v3/client/preferences', headers=headers(alice), json={'messages': False, 'theme':'dark'})
    assert saved.status_code == 200
    assert client.get('/api/v3/client/preferences',headers=headers(alice)).json()['messages'] is False
    assert client.get('/api/v3/client/preferences',headers=headers(bob)).json()['messages'] is True
    assert client.patch('/api/v3/client/preferences',headers=headers(alice),json={'theme':'invalid'}).status_code == 422


def test_bootstrap_contains_only_latest_message_for_each_owned_friend(fixture):
    client, db, alice, bob, headers = fixture
    context = set_current_user(alice['user_id'])
    try:
        friend = db.create_friend({'name': 'Alice friend'})
        for i in range(3):
            db.add_message(friend['id'], 'friend', f'message {i}', is_read=False)
    finally:
        reset_current_user(context)
    db.create_friend({'name':'Bobs friend','owner_user_id': bob['user_id']})
    result = client.get('/api/v3/client/bootstrap',headers=headers(alice))
    assert result.status_code == 200
    boot = result.json()
    assert len(boot['friends']) == 1
    assert len(boot['conversations']) == 1
    assert boot['conversations'][0]['last_message']['transcript'] == 'message 2'
    assert boot['conversations'][0]['unread'] == 3


def test_event_ticket_is_one_use_and_replay_owned(fixture):
    client, db, alice, bob, headers = fixture
    response = client.post('/api/v3/client/event-ticket', headers=headers(alice))
    ticket = response.json()['ticket']
    with client.websocket_connect('/api/v3/client/events') as ws:
        ws.send_json({'type':'auth','ticket': ticket})
        assert ws.receive_json()['type'] == 'ready'
        ws.send_json({'type':'ping'})
        assert ws.receive_json()['type'] == 'pong'


def test_device_token_cannot_be_claimed_by_second_user(fixture):
    client, db, alice, bob, headers = fixture
    body = {'platform':'android','registration_id':'r'*24,'enabled':True}
    assert client.post('/api/v3/client/devices',headers=headers(alice),json=body).status_code == 200
    assert client.post('/api/v3/client/devices',headers=headers(bob),json=body).status_code == 409
