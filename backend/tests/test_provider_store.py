import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from app.providers.store import ProviderStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv('PROVIDER_POOL_MASTER_KEY', raising=False)
    conn = sqlite3.connect(tmp_path / 'pool.sqlite', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db = SimpleNamespace(conn=conn, _lock=threading.RLock())
    result = ProviderStore(db, tmp_path)
    yield result
    conn.close()


def resource(store, **patch):
    return store.create(dict(provider='fish', name='account', api_key='secret-example-123456',
                             base_url='https://api.fish.audio', model='s1', **patch))


def test_credentials_encrypted_and_duplicate_rejected(store):
    item = resource(store)
    assert item['state'] == 'pending'
    assert 'api_key' not in item and 'secret-example' not in json.dumps(item)
    assert store.get(item['id'], secret=True)['api_key'] == 'secret-example-123456'
    rows = store.conn.execute('SELECT * FROM provider_resources').fetchall()
    assert 'secret-example-123456' not in repr([tuple(r) for r in rows])
    with pytest.raises(ValueError):
        resource(store)


def test_verification_cas_rotation_and_explicit_rollback(store):
    item = resource(store)
    rotated = store.update(item['id'], {'api_key': 'replacement-secret-654321'}, revision=item['revision'])
    with pytest.raises(ValueError):
        store.verification_result(item['id'], item['revision'], True)
    assert store.get(item['id'])['state'] == 'pending'
    verified = store.verification_result(item['id'], rotated['revision'], True)
    assert verified['state'] == 'active' and store.provider_enabled('fish')
    store.set_provider_enabled('fish', False)
    store.verification_result(item['id'], rotated['revision'], True)
    assert not store.provider_enabled('fish')


def test_missing_master_key_never_replaces_existing_encryption(store):
    resource(store)
    key = store.data_dir / 'provider-master.key'
    key.unlink()
    reopened = ProviderStore(store.db, store.data_dir)
    with pytest.raises(ValueError, match='key'):
        reopened.get(store.list_resources()['items'][0]['id'], secret=True)
    assert not key.exists()


def test_groups_model_isolation_and_pagination(store):
    first = resource(store)
    second = store.create(dict(provider='fish', name='other model', api_key='secret-example-123456',
                               base_url='https://api.fish.audio', model='s2'))
    assert first['quota_group_id'] == second['quota_group_id']
    assert first['route_group_id'] != second['route_group_id']
    with pytest.raises(ValueError):
        store.update(second['id'], {'route_group_id': first['route_group_id']})
    assert store.list_resources(page=2, page_size=1)['items'][0]['id'] == second['id']


def test_persistent_daily_budget_atomic_across_restart(store):
    first = resource(store, daily_limit=2, monthly_limit=3)
    group = first['quota_group_id']
    store.reserve_budget(group)
    reopened = ProviderStore(store.db, store.data_dir)
    reopened.reserve_budget(group)
    with pytest.raises(ValueError):
        reopened.reserve_budget(group)
    assert reopened.budget_stats(group)['daily_requests'] == 2


def test_group_revision_cas_and_invalid_values_do_not_mutate(store):
    item = resource(store)
    group = store.list_quota_groups()[0]
    updated = store.upsert_quota_group(dict(id=group['id'], revision=group['revision'], concurrency_limit=3))
    assert updated['revision'] == 2
    with pytest.raises(ValueError):
        store.upsert_quota_group(dict(id=group['id'], revision=1, concurrency_limit=9))
    assert store.get(item['id'])['concurrency_limit'] == 3
    with pytest.raises(ValueError):
        store.update(item['id'], {'balance_value': float('nan')})


def test_budget_atomic_across_independent_connections(store, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    item = resource(store, daily_limit=7)
    connections = []
    stores = []
    for _ in range(3):
        conn = sqlite3.connect(tmp_path / 'pool.sqlite', check_same_thread=False, timeout=5)
        conn.row_factory = sqlite3.Row
        connections.append(conn)
        stores.append(ProviderStore(SimpleNamespace(conn=conn, _lock=threading.RLock()), tmp_path))
    def attempt(index):
        try:
            stores[index % 3].reserve_budget(item['quota_group_id'])
            return True
        except ValueError:
            return False
    try:
        with ThreadPoolExecutor(max_workers=9) as executor:
            admitted = list(executor.map(attempt, range(30)))
        assert sum(admitted) == 7
        assert store.budget_stats(item['quota_group_id'])['daily_requests'] == 7
    finally:
        for conn in connections:
            conn.close()


def test_out_of_order_health_writes_cannot_override_newer_observation(store):
    import time
    item = resource(store)
    store.verification_result(item['id'], item['revision'], True)
    stamp = time.time_ns()
    store.persist_health(item['id'], item['revision'], 'cooldown', health_updated_ns=stamp + 200)
    store.persist_health(item['id'], item['revision'], 'active', health_updated_ns=stamp + 100)
    assert store.get(item['id'])['state'] == 'cooldown'


def test_legacy_import_is_idempotent_and_does_not_change_active_mode(store):
    payload = dict(name='migrated', api_key='legacy-offline-key', model='s1')
    first = store.import_legacy('fish', payload)
    store.verification_result(first['id'], first['revision'], True)
    store.set_provider_enabled('fish', False)
    second = ProviderStore(store.db, store.data_dir).import_legacy('fish', payload)
    assert second['id'] == first['id'] and second['state'] == 'active'
    assert not store.provider_enabled('fish')
    assert store.list_resources()['total'] == 1


def test_rotating_disabled_key_invalidates_previous_verification(store):
    item = resource(store)
    store.verification_result(item['id'],item['revision'],True)
    paused = store.update(item['id'],{'enabled':False})
    rotated = store.update(item['id'],{'api_key':'unverified-new-key'},paused['revision'])
    assert rotated['verified_at'] is None
    enabled = store.update(item['id'],{'enabled':True},rotated['revision'])
    assert enabled['state'] == 'pending'
