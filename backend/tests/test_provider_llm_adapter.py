from __future__ import annotations

from types import SimpleNamespace
import httpx
import openai
import pytest


@pytest.mark.asyncio
async def test_pool_only_llm_uses_exact_model_and_shared_transport_cache(tmp_path, monkeypatch):
    from app.v2.db import DB
    from app.config import LLMSettings
    from app.llm.client import LLMClient
    from app.providers.store import ProviderStore
    from app.providers.pool import ProviderPool
    from app.providers.runtime import bind_pool, unbind_pool
    db = DB(str(tmp_path / 'llm.db'))
    db.init()
    store = ProviderStore(db, tmp_path)
    for key in ('pool-a', 'pool-b'):
        resource = store.create({'provider':'llm', 'name':key, 'api_key':key,
            'base_url':'https://fake.test/v1','model':'same','concurrency_limit':5})
        store.verification_result(resource['id'],resource['revision'],True)
    pool = ProviderPool(store)
    await pool.reload()
    bind_pool(pool)
    calls = []
    bodies = []
    def client_for(self):
        async def create(**kwargs):
            calls.append((self.cfg.api_key, kwargs['model']))
            bodies.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok":true}'))])
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(LLMClient, '_ensure_client', client_for)
    llm = LLMClient(LLMSettings(api_key='', model='same', extra_body={'model':'different','max_tokens':99999,'enable_thinking':False}))
    try:
        result = await llm.complete_json([{'role':'user','content':'hello'}])
        assert result == '{"ok":true}'
        assert calls[0][0] in ('pool-a','pool-b')
        assert calls[0][1] == 'same'
        assert not llm.uses_mock
        assert all(item[1] == 'same' for item in calls)
        # Pooled SDK instances cannot route via fields hidden in extra_body.
        cached = next(iter(pool._clients.values())).value
        assert 'model' not in cached.cfg.extra_body
        assert cached.cfg.extra_body['enable_thinking'] is False
        llm.cfg.suggestion_temperature = .12
        llm.cfg.suggestion_max_tokens = 123
        llm.cfg.extra_body = {'enable_thinking': True}
        await llm.complete_json([{'role':'user','content':'again'}])
        assert bodies[-1]['temperature'] == .12
        assert bodies[-1]['max_tokens'] == 123
        assert bodies[-1]['extra_body']['enable_thinking'] is True
        llm.cfg.suggestion_model = 'not-pooled'
        before = len(calls)
        assert await llm.complete_json([{'role':'user','content':'hello'}], model='not-pooled') == '{}'
        assert len(calls) == before
    finally:
        await pool.aclose()
        unbind_pool(pool)
        db.close()


@pytest.mark.asyncio
async def test_pool_llm_does_not_replay_after_stream_output(tmp_path, monkeypatch):
    from app.v2.db import DB
    from app.config import LLMSettings
    from app.llm.client import LLMClient
    from app.providers.store import ProviderStore
    from app.providers.pool import ProviderPool
    from app.providers.runtime import bind_pool, unbind_pool
    db = DB(str(tmp_path / 'stream.db'))
    db.init()
    store = ProviderStore(db,tmp_path)
    for key in ('stream-a','stream-b'):
        resource = store.create({'provider':'llm','name':key,'api_key':key,
            'base_url':'https://fake.test/v1','model':'same'})
        store.verification_result(resource['id'],resource['revision'],True)
    pool = ProviderPool(store)
    await pool.reload()
    bind_pool(pool)
    dispatched = []
    class Stream:
        def __aiter__(self):
            return self.iterate()
        async def iterate(self):
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='hello'))])
            response = httpx.Response(503,request=httpx.Request('POST','https://fake.test'))
            raise openai.APIStatusError('offline failure',response=response,body={})
        async def close(self):
            pass
    def client_for(self):
        async def create(**kwargs):
            dispatched.append(self.cfg.api_key)
            return Stream()
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(LLMClient,'_ensure_client',client_for)
    llm = LLMClient(LLMSettings(api_key='',model='same'))
    output = []
    try:
        with pytest.raises(Exception):
            async for token in llm.stream_chat([{'role':'user','content':'hi'}]):
                output.append(token)
        assert output == ['hello']
        assert len(dispatched) == 1
        with db._lock:
            reserved = db.conn.execute('SELECT SUM(tokens) FROM provider_group_usage').fetchone()[0]
        assert reserved >= llm.cfg.max_tokens
    finally:
        await pool.aclose()
        unbind_pool(pool)
        db.close()
