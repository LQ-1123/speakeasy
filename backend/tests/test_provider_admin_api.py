from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient


@pytest.fixture
def admin(tmp_path, monkeypatch):
    from app.v2.db import DB
    from app.providers.store import ProviderStore
    from app.providers.pool import ProviderPool
    from app.admin.api.provider_resources import router
    from app.admin.auth import require_admin
    monkeypatch.setenv('SE_ADMIN_TOKEN', 'offline-admin')
    db = DB(str(tmp_path / 'pool.db'))
    db.init()
    store = ProviderStore(db, tmp_path)
    pool = ProviderPool(store)
    app = FastAPI()
    app.include_router(router, prefix='/api/admin', dependencies=[Depends(require_admin)])
    app.state.v2 = SimpleNamespace(db=db, provider_pool=pool, provider_store=store)
    with TestClient(app) as client:
        client.headers['X-Admin-Token'] = 'offline-admin'
        yield client, store, pool
    db.close()


def test_create_does_not_reveal_key_or_activate_before_verification(admin):
    client, store, pool = admin
    response = client.post('/api/admin/provider-resources', json={
        'provider':'fish', 'name':'team-a', 'api_key':'secret-admin-resource',
        'base_url':'https://fake.test', 'model':'s2.1-pro-free','concurrency_limit':5})
    assert response.status_code == 200, response.text
    assert 'secret-admin-resource' not in response.text
    resource = response.json()['resource']
    assert resource['state'] == 'pending'
    assert not store.provider_enabled('fish')
    got = client.get('/api/admin/provider-resources?provider=fish').json()
    assert got['total'] == 1
    assert 'api_key' not in got['items'][0]


def test_verify_activates_and_explicit_rollback_remains_off(admin, monkeypatch):
    from app.admin.api import provider_resources
    client, store, pool = admin
    async def probe(*args, **kwargs):
        return {'ok':True, 'message':'OK','status_code':200,'latency_ms':1}
    monkeypatch.setattr(provider_resources, 'probe_resource', probe)
    resource = client.post('/api/admin/provider-resources',json={
        'provider':'fish','name':'team-a','api_key':'offline-key-a',
        'base_url':'https://fake.test','model':'free'}).json()['resource']
    verified = client.post(f"/api/admin/provider-resources/{resource['id']}/verify")
    assert verified.status_code == 200, verified.text
    assert verified.json()['ok']
    assert store.provider_enabled('fish')
    client.put('/api/admin/provider-resources/fish/mode',json={'enabled':False})
    assert not store.provider_enabled('fish')
    client.post(f"/api/admin/provider-resources/{resource['id']}/verify")
    assert not store.provider_enabled('fish')


def test_stats_without_ids_never_returns_all_resource_records(admin):
    client, store, pool = admin
    for i in range(3):
        store.create({'provider':'llm','name':f'key-{i}','api_key':f'key-{i}',
                      'base_url':'https://fake.test/v1','model':'same'})
    result = client.get('/api/admin/provider-resources/stats').json()
    assert result['resources'] == []
    assert len(result['providers']) == 4
    assert all(row.get('waiting') == 0 for row in result['providers'])


def test_provider_keys_are_not_available_without_admin_identity(admin):
    client, _, _ = admin
    del client.headers['X-Admin-Token']
    assert client.get('/api/admin/provider-resources').status_code == 403
    assert client.post('/api/admin/provider-resources',json={'api_key':'do-not-store'}).status_code == 403


@pytest.mark.asyncio
async def test_cancelled_mutation_applies_committed_revision_to_runtime(tmp_path):
    import asyncio
    import threading
    from starlette.requests import Request
    from app.v2.db import DB
    from app.providers.store import ProviderStore
    from app.providers.pool import ProviderPool
    from app.admin.api.provider_resources import mutate
    db = DB(str(tmp_path / 'cancel.db'))
    db.init()
    store = ProviderStore(db,tmp_path)
    resource = store.create({'provider':'fish','name':'old','api_key':'old-key',
        'base_url':'https://fake.test','model':'same'})
    store.verification_result(resource['id'],resource['revision'],True)
    pool = ProviderPool(store)
    await pool.reload()
    app = FastAPI()
    app.state.v2 = SimpleNamespace(db=db,provider_pool=pool,provider_store=store)
    request = Request({'type':'http','app':app})
    entered, release = threading.Event(), threading.Event()
    def update():
        entered.set()
        assert release.wait(3)
        return store.update(resource['id'],{'api_key':'new-key'},resource['revision'])
    task = asyncio.create_task(mutate(request,update))
    while not entered.is_set():
        await asyncio.sleep(.001)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pool.resource_stats(resource['id'])['revision'] == store.get(resource['id'])['revision']
    assert not pool.available('fish','same')
    await pool.aclose()
    db.close()


@pytest.mark.asyncio
async def test_model_defaults_use_verified_pool_without_probing_legacy_key(tmp_path, monkeypatch):
    from starlette.requests import Request
    from app.v2.db import DB
    from app.providers.store import ProviderStore
    from app.providers.pool import ProviderPool
    from app.admin.api import config
    db = DB(str(tmp_path/'defaults.db'))
    db.init()
    store = ProviderStore(db,tmp_path)
    item = store.create({'provider':'llm','name':'pool-only','api_key':'pool-key',
        'base_url':'https://fake.test/v1','model':'pool-model'})
    store.verification_result(item['id'],item['revision'],True)
    pool = ProviderPool(store)
    await pool.reload()
    app = FastAPI()
    app.state.v2 = SimpleNamespace(db=db,provider_pool=pool,provider_store=store)
    seen = []
    async def save(services,payload):
        seen.append(payload)
        return {'ok':True}
    monkeypatch.setitem(config._SAVE_HANDLERS,'llm',save)
    try:
        await config.put_config(Request({'type':'http','app':app}),'llm',{'model':'pool-model'})
        assert seen[0].get('skip_verify') is True
    finally:
        await pool.aclose()
        db.close()
