from types import SimpleNamespace
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.v2.db import DB
from app.v3 import calls
from app.v3.call_sessions import CallSessionRegistry
from app.v3.schema import ensure_schema
from app.v3.security import register_user
from app.v3.identity import set_current_user, reset_current_user


class FakeChat:
    def __init__(self, directory):
        self.engines = SimpleNamespace(llm=SimpleNamespace(uses_mock=False, stream_chat=self.stream))
        self.audio=SimpleNamespace(audio_dir=str(directory))
    def _call_turn_plan(self, *args): return None
    def _call_system_prompt(self, *args): return 'Respond briefly.'
    async def stream(self, *args, **kwargs): yield 'Hello there.'
    async def _synth_sentence(self, *args, **kwargs):
        from pathlib import Path
        import uuid
        directory=Path(kwargs.get('out_dir') or self.audio.audio_dir)
        directory.mkdir(parents=True,exist_ok=True)
        path=directory/(uuid.uuid4().hex+'.wav')
        path.write_bytes(b'RIFF'+b'a'*100)
        return {'url':'/api/v2/audio/test.wav', 'duration_ms':400, 'path':str(path)}


@pytest.fixture
def client(tmp_path, monkeypatch):
    db=DB(str(tmp_path/'calls.db'))
    db.init()
    ensure_schema(db,seed=False)
    user=register_user(db,'call-device-1')
    stranger=register_user(db,'call-device-2')
    context=set_current_user(user['user_id'])
    friend=db.create_friend({'name':'Friend','reply_language':'en'})
    reset_current_user(context)
    app=FastAPI()
    app.state.v2=SimpleNamespace(db=db,chat=FakeChat(tmp_path))
    app.include_router(calls.router)
    monkeypatch.setattr(calls,'REGISTRY',CallSessionRegistry())
    monkeypatch.setattr(calls,'RUNTIMES',{})
    monkeypatch.setattr(calls,'deepgram_key',lambda:'')
    h=lambda u:{'X-User-Id':u['user_id'],'X-User-Token':u['token']}
    with TestClient(app) as client:
        yield client,friend,h(user),h(stranger),db
    db.close()


def test_call_auth_and_friend_ownership(client):
    c,f,h,other,_=client
    assert c.post('/api/v3/calls',json={'friend_id':f['id']}).status_code==401
    assert c.post('/api/v3/calls',headers=other,json={'friend_id':f['id']}).status_code==404


def test_persistent_socket_handles_multiple_turns_and_acknowledgements(client):
    c,f,h,_,db=client
    session=c.post('/api/v3/calls',headers=h,json={'friend_id':f['id'],'request_id':'one'}).json()
    with c.websocket_connect(session['ws_path']) as ws:
        # Browser sockets cannot attach custom HTTP authentication headers.
        ws.send_json({'type':'auth','ticket':session['ticket']})
        assert ws.receive_json()['type']=='ready'
        for turn in ('first','second'):
            ws.send_json({'type':'text','text':'Hello','turn_id':turn})
            assert ws.receive_json()['type']=='transcript'
            assert ws.receive_json()['type']=='responding'
            audio=ws.receive_json()
            assert audio['type']=='audio'
            assert audio['turn_id']==turn
            assert c.get(audio['audio_url']).status_code==200
            assert c.get(audio['audio_url'].split('?')[0]).status_code==403
            ws.send_json({'type':'playback_done','audio_id':audio['audio_id'],'turn_id':turn})
            assert ws.receive_json()['type']=='turn_done'
        ws.send_json({'type':'ping'})
        assert ws.receive_json()['type']=='pong'
    runtime=calls.RUNTIMES[session['call_id']]
    assert runtime.played_turns==2
    assert c.delete('/api/v3/calls/'+session['call_id'],headers=h).status_code==200
    assert not calls.REGISTRY.contains(session['call_id'])


def test_creation_request_id_is_idempotent(client):
    c,f,h,_,_=client
    a=c.post('/api/v3/calls',headers=h,json={'friend_id':f['id'],'request_id':'same'}).json()
    b=c.post('/api/v3/calls',headers=h,json={'friend_id':f['id'],'request_id':'same'}).json()
    assert a['call_id']==b['call_id']
    assert a['ticket']!=b['ticket']


def test_authorized_resume_takes_over_stale_connected_socket(client):
    c,f,h,_,_=client
    session=c.post('/api/v3/calls',headers=h,json={'friend_id':f['id']}).json()
    with c.websocket_connect(session['ws_path']) as original:
        original.send_json({'type':'auth','ticket':session['ticket']})
        assert original.receive_json()['type']=='ready'
        renewed=c.post('/api/v3/calls/'+session['call_id']+'/ticket',headers=h)
        assert renewed.status_code==200
        with c.websocket_connect(session['ws_path']) as restored:
            restored.send_json({'type':'auth','ticket':renewed.json()['ticket']})
            assert restored.receive_json()['type']=='ready'
            restored.send_json({'type':'ping'})
            assert restored.receive_json()['type']=='pong'
    c.delete('/api/v3/calls/'+session['call_id'],headers=h)


def test_streaming_recognizer_stays_open_for_successive_utterances(client, monkeypatch):
    class Recognizer:
        connections=0
        closes=0
        def __init__(self,*args):
            self.queue=asyncio.Queue()
            self.seconds=0
            self.previous=0
        async def connect(self): Recognizer.connections+=1
        async def send(self,pcm): self.seconds+=len(pcm)/32000
        async def finalize(self):
            await self.queue.put({'type':'final','text':'Hello','finalized':True,
                                  'start':self.previous,'duration':self.seconds-self.previous})
            self.previous=self.seconds
        async def keepalive(self): pass
        async def close(self): Recognizer.closes+=1
        async def events(self):
            while True: yield await self.queue.get()
    monkeypatch.setattr(calls,'deepgram_key',lambda:'fake')
    monkeypatch.setattr(calls,'DeepgramStreamer',Recognizer)
    c,f,h,_,_=client
    ticket=c.post('/api/v3/calls',headers=h,json={'friend_id':f['id']}).json()
    with c.websocket_connect(ticket['ws_path']) as ws:
        ws.send_json({'type':'auth','ticket':ticket['ticket']})
        assert ws.receive_json()['streaming_stt'] is True
        for turn in ('one','two'):
            ws.send_bytes(b'\x00\x01'*1000)
            ws.send_json({'type':'utterance_end','turn_id':turn})
            assert ws.receive_json()['type']=='transcript'
            assert ws.receive_json()['type']=='responding'
            assert ws.receive_json()['type']=='audio'
            assert ws.receive_json()['type']=='turn_done'
        assert Recognizer.connections==1
        assert Recognizer.closes==0
    c.delete('/api/v3/calls/'+ticket['call_id'],headers=h)
    assert Recognizer.closes==1
