import asyncio
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from app.providers.pool import ProviderPool, PoolError
from app.providers.store import ProviderStore


def test_pool_capacity_rejection_uses_existing_capacity_error_path():
    from app.capacity import CapacityError
    try:
        raise PoolError('temporarily busy', status_code=503, retry_after=2.5)
    except CapacityError as error:
        assert error.status_code == 503 and error.retry_after == 3


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv('PROVIDER_POOL_MASTER_KEY', raising=False)
    conn = sqlite3.connect(tmp_path / 'pool.sqlite', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    result = ProviderStore(SimpleNamespace(conn=conn, _lock=threading.RLock()), tmp_path)
    yield result
    conn.close()


def add(store, key, **patch):
    item = store.create(dict(provider='fish', name=key, api_key=f'secret-{key}-12345',
                             model='s1', base_url='https://api.fish.audio', **patch))
    return store.verification_result(item['id'], item['revision'], True)


@pytest.mark.asyncio
async def test_shared_group_cap_and_independent_groups(store):
    first = add(store, 'a', concurrency_limit=2)
    add(store, 'b', quota_group_id=first['quota_group_id'])
    add(store, 'c', concurrency_limit=2)
    pool = ProviderPool(store)
    await pool.reload()
    release = asyncio.Event()
    entered = []

    async def work():
        async with pool.lease('fish', 's1', deadline=asyncio.get_running_loop().time() + 2) as lease:
            entered.append(lease.quota_group_id)
            await release.wait()
            lease.success()

    tasks = [asyncio.create_task(work()) for _ in range(5)]
    for _ in range(100):
        if len(entered) == 4:
            break
        await asyncio.sleep(.001)
    assert len(entered) == 4
    assert entered.count(first['quota_group_id']) == 2
    tasks[-1].cancel()
    release.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert pool.stats()['active'] == 0 and pool.stats()['waiting'] == 0
    await pool.aclose()


@pytest.mark.asyncio
async def test_pending_does_not_apply_but_activated_unavailable_route_does(store):
    item = store.create(dict(provider='fish', name='a', api_key='secret-a', model='s1'))
    pool = ProviderPool(store)
    await pool.reload()
    assert not pool.applies('fish', 's1')
    store.verification_result(item['id'], item['revision'], True)
    store.update(item['id'], {'enabled': False})
    await pool.reload()
    assert pool.applies('fish', 's1') and not pool.applies('fish', 's2')
    with pytest.raises(PoolError):
        async with pool.lease('fish', 's1', deadline=asyncio.get_running_loop().time() + .02):
            pass
    await pool.aclose()


@pytest.mark.asyncio
async def test_rotation_keeps_active_secret_but_new_lease_uses_new_revision(store):
    item = add(store, 'a', concurrency_limit=2)
    pool = ProviderPool(store)
    await pool.reload()
    async with pool.lease('fish', 's1') as old:
        updated = store.update(item['id'], {'api_key': 'new-secret'})
        store.verification_result(item['id'], updated['revision'], True)
        await pool.reload()
        async with pool.lease('fish', 's1') as new:
            assert old.api_key == 'secret-a-12345'
            assert new.api_key == 'new-secret'
            assert new.revision != old.revision
    assert pool.stats()['active'] == 0
    await pool.aclose()


@pytest.mark.asyncio
async def test_429_and_401_remove_resource_without_secret_leak(store):
    item = add(store, 'a')
    pool = ProviderPool(store)
    await pool.reload()
    async with pool.lease('fish', 's1') as lease:
        lease.failure(status=429, retry_after=10)
    assert pool.resource_stats(item['id'])['state'] == 'cooldown'
    with pytest.raises(PoolError):
        async with pool.lease('fish', 's1', deadline=asyncio.get_running_loop().time() + .01):
            pass
    assert 'secret-a' not in str(pool.stats())
    await pool.aclose()


@pytest.mark.asyncio
async def test_lazy_client_cache_eviction_and_active_client_drain(store):
    item = add(store, 'a', concurrency_limit=2)
    pool = ProviderPool(store, client_limit=1)
    await pool.reload()
    made = []

    class Client:
        closed = False
        async def aclose(self):
            self.closed = True

    def factory(lease):
        client = Client()
        made.append(client)
        return client

    assert not made
    async with pool.lease('fish', 's1') as lease:
        async with pool.client(lease, factory) as old:
            updated = store.update(item['id'], {'api_key': 'new-secret'})
            store.verification_result(item['id'], updated['revision'], True)
            await pool.reload()
            assert not old.closed
        assert old.closed
    async with pool.lease('fish', 's1') as lease:
        async with pool.client(lease, factory) as current:
            assert current is not old
    await pool.aclose()
    assert all(c.closed for c in made)


@pytest.mark.asyncio
async def test_probe_pending_key_obeys_shared_capacity_and_does_not_activate(store):
    item = store.create(dict(provider='fish', name='draft', api_key='draft-secret', model='s1', concurrency_limit=1))
    pool = ProviderPool(store)
    await pool.reload()
    async with pool.probe(item['id'], item['revision']) as lease:
        assert lease.api_key == 'draft-secret'
        with pytest.raises(PoolError):
            async with pool.probe(item['id'], item['revision'], deadline=asyncio.get_running_loop().time() + .01):
                pass
        lease.success()
    assert not pool.applies('fish', 's1')
    assert store.get(item['id'])['state'] == 'pending'
    await pool.aclose()


@pytest.mark.asyncio
async def test_fifo_queue_cancellation_and_bounded_queue(store):
    add(store, 'a')
    pool = ProviderPool(store, queue_limit=2)
    await pool.reload()
    order = []

    async def waiting(name, priority='chat'):
        async with pool.lease('fish', 's1', priority=priority):
            order.append(name)

    async with pool.lease('fish', 's1'):
        first = asyncio.create_task(waiting('first'))
        await asyncio.sleep(0)
        second = asyncio.create_task(waiting('second'))
        await asyncio.sleep(0)
        with pytest.raises(PoolError):
            async with pool.lease('fish', 's1'):
                pass
    await asyncio.gather(first, second)
    assert order == ['first', 'second']
    assert pool.stats()['active'] == 0
    await pool.aclose()


@pytest.mark.asyncio
async def test_group_budget_limits_across_independent_pool_instances(store):
    add(store, 'a', daily_limit=1)
    pool = ProviderPool(store)
    await pool.reload()
    async with pool.lease('fish', 's1'):
        pass
    with pytest.raises(PoolError) as error:
        async with pool.lease('fish', 's1'):
            pass
    assert error.value.status_code == 429
    assert pool.stats()['active'] == 0
    await pool.aclose()


@pytest.mark.asyncio
async def test_exact_tokens_reconcile_and_rpm_shared_limit(store):
    item = add(store, 'a', concurrency_limit=3, rpm_limit=2, tpm_limit=100)
    add(store, 'b', quota_group_id=item['quota_group_id'])
    pool = ProviderPool(store)
    await pool.reload()
    async with pool.lease('fish', 's1', estimated_tokens=70) as lease:
        lease.success({'total_tokens': 10})
    async with pool.lease('fish', 's1', estimated_tokens=70) as lease:
        lease.success({'total_tokens': 20})
    with pytest.raises(PoolError):
        async with pool.lease('fish', 's1', estimated_tokens=1, deadline=asyncio.get_running_loop().time() + .01):
            pass
    await pool.aclose()
    row = store.conn.execute('SELECT SUM(tokens) FROM provider_group_usage').fetchone()
    assert row[0] == 30


@pytest.mark.asyncio
async def test_safe_candidate_exclusion_and_health_classification(store):
    first = add(store, 'a')
    second = add(store, 'b')
    pool = ProviderPool(store)
    await pool.reload()
    async with pool.lease('fish', 's1', exclude_resource_ids={first['id']}) as lease:
        assert lease.resource_id == second['id']
        lease.failure(status=401)
    assert pool.resource_stats(second['id'])['state'] == 'disabled'
    async with pool.lease('fish', 's1') as lease:
        assert lease.resource_id == first['id']
        lease.failure(status=402)
    assert pool.resource_stats(first['id'])['state'] == 'exhausted'
    await pool.aclose()


@pytest.mark.asyncio
async def test_three_temporary_errors_open_breaker(store):
    item = add(store, 'a')
    pool = ProviderPool(store)
    await pool.reload()
    for _ in range(3):
        async with pool.lease('fish', 's1') as lease:
            lease.failure(status=503)
    stats = pool.resource_stats(item['id'])
    assert stats['consecutive_failures'] == 3 and stats['cooldown_seconds'] > 0
    with pytest.raises(PoolError):
        async with pool.lease('fish', 's1', deadline=asyncio.get_running_loop().time() + .01):
            pass
    await pool.aclose()


@pytest.mark.asyncio
async def test_deleted_active_resource_drains_and_route_remains_managed(store):
    item = add(store, 'a')
    pool = ProviderPool(store)
    await pool.reload()
    async with pool.lease('fish', 's1') as lease:
        store.delete(item['id'])
        await pool.reload()
        assert pool.resource_stats(item['id'])['state'] == 'draining'
        assert store.get(item['id'])['state'] == 'draining'
        assert lease.api_key == 'secret-a-12345'
    await pool.aclose()
    with pytest.raises(KeyError):
        store.get(item['id'])
    other = ProviderPool(store)
    await other.reload()
    assert other.applies('fish', 's1')
    await other.aclose()


@pytest.mark.asyncio
async def test_cancelled_async_factory_is_closed_after_rotation(store):
    item = add(store, 'a')
    pool = ProviderPool(store)
    await pool.reload()
    created = asyncio.Event()
    finish = asyncio.Event()
    clients = []

    class Client:
        closed = False
        async def aclose(self):
            self.closed = True

    async def factory(lease):
        created.set()
        await finish.wait()
        client = Client()
        clients.append(client)
        return client

    async def work():
        async with pool.lease('fish', 's1') as lease:
            async with pool.client(lease, factory):
                pass
    task = asyncio.create_task(work())
    await created.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    updated = store.update(item['id'], {'api_key': 'new-secret'})
    store.verification_result(item['id'], updated['revision'], True)
    await pool.reload()
    finish.set()
    for _ in range(50):
        if clients and clients[0].closed:
            break
        await asyncio.sleep(.001)
    assert clients[0].closed
    assert pool.stats()['active'] == 0 and pool.stats()['clients'] == 0
    await pool.aclose()


@pytest.mark.asyncio
async def test_voice_capability_filter_and_specific_denial_do_not_disable_key(store):
    first = add(store, 'a', capabilities={'voices': ['voice-a', 'voice-b']})
    pool = ProviderPool(store)
    await pool.reload()
    async with pool.lease('fish', 's1', capability={'reference_id': 'voice-a'}) as lease:
        lease.failure(status=403, code='voice_denied:voice-a')
    assert pool.resource_stats(first['id'])['state'] == 'active'
    async with pool.lease('fish', 's1', capability={'reference_id': 'voice-b'}):
        pass
    with pytest.raises(PoolError):
        async with pool.lease('fish', 's1', capability={'reference_id': 'voice-a'},
                              deadline=asyncio.get_running_loop().time() + .01):
            pass
    with pytest.raises(PoolError):
        async with pool.lease('fish', 's1', capability={'reference_id': 'unknown'},
                              deadline=asyncio.get_running_loop().time() + .01):
            pass
    await pool.aclose()


@pytest.mark.asyncio
async def test_ws_sessions_are_disposable_and_survive_connect_deadline(store):
    item = store.create(dict(provider='deepgram', name='ws', api_key='fake-ws-secret', model='nova-3', concurrency_limit=2))
    store.verification_result(item['id'], item['revision'], True)
    pool = ProviderPool(store)
    await pool.reload()
    made = []

    class Socket:
        closed = False
        async def close(self):
            self.closed = True

    async def factory(lease):
        value = Socket()
        made.append(value)
        return value

    contexts = []
    async def connect():
        lease_context = pool.lease('deepgram', 'nova-3', deadline=asyncio.get_running_loop().time() + .03)
        lease = await lease_context.__aenter__()
        client_context = pool.client(lease, factory, kind='ws')
        socket = await client_context.__aenter__()
        contexts.extend([lease_context, client_context, socket])

    await asyncio.create_task(connect())
    await asyncio.sleep(.04)
    assert not contexts[2].closed
    await contexts[1].__aexit__(None, None, None)
    await contexts[0].__aexit__(None, None, None)
    assert contexts[2].closed
    async with pool.lease('deepgram', 'nova-3') as lease:
        async with pool.client(lease, factory, kind='ws') as socket:
            assert socket is not contexts[2]
    assert len(made) == 2 and all(socket.closed for socket in made)
    await pool.aclose()


@pytest.mark.asyncio
async def test_reload_after_successful_reverification_clears_cooldown(store):
    item = add(store, 'a')
    pool = ProviderPool(store)
    await pool.reload()
    async with pool.lease('fish', 's1') as lease:
        lease.failure(status=429, retry_after=60)
    await asyncio.gather(*list(pool._tasks))
    store.verification_result(item['id'], item['revision'], True)
    await pool.reload()
    assert pool.available('fish', 's1')
    async with pool.lease('fish', 's1', deadline=asyncio.get_running_loop().time() + .05):
        pass
    await pool.aclose()


@pytest.mark.asyncio
async def test_summary_stats_skip_resource_records(store):
    add(store, 'a')
    pool = ProviderPool(store)
    await pool.reload()
    summary = pool.stats(include_resources=False)
    assert summary['active'] == 0 and summary['resources'] == []
    assert summary['providers']['fish']['resources'] == 1
    await pool.aclose()


@pytest.mark.asyncio
async def test_restart_routes_around_persisted_exhausted_group(store):
    first = add(store, 'a', daily_limit=1)
    second = add(store, 'b', daily_limit=1)
    store.reserve_budget(first['quota_group_id'])
    pool = ProviderPool(store)
    await pool.reload()
    async with pool.lease('fish', 's1', deadline=asyncio.get_running_loop().time() + .1) as lease:
        assert lease.resource_id == second['id']
    await pool.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['http', 'ws'])
async def test_global_transport_limits_cover_shared_and_disposable_clients(store, kind):
    add(store, 'a', concurrency_limit=6)
    pool = ProviderPool(store, http_limit=2, ws_limit=2)
    await pool.reload()
    release = asyncio.Event()
    entered = []

    class Client:
        async def aclose(self):
            pass

    async def work():
        async with pool.lease('fish', 's1') as lease:
            async with pool.client(lease, lambda _: Client(), kind=kind):
                entered.append(1)
                await release.wait()
    tasks = [asyncio.create_task(work()) for _ in range(6)]
    try:
        for _ in range(100):
            if len(entered) == 2:
                break
            await asyncio.sleep(.001)
        assert len(entered) == 2 and pool.stats()[f'{kind}_active'] == 2
    finally:
        release.set()
        await asyncio.gather(*tasks)
        await pool.aclose()


@pytest.mark.asyncio
async def test_ws_session_does_not_evict_or_block_http_cache(store):
    add(store, 'a', concurrency_limit=2)
    pool = ProviderPool(store, client_limit=1)
    await pool.reload()

    class Client:
        async def aclose(self):
            pass
    async with pool.lease('fish', 's1') as first:
        async with pool.client(first, lambda _: Client()) as http:
            async with pool.lease('fish', 's1', deadline=asyncio.get_running_loop().time() + .1) as second:
                async with pool.client(second, lambda _: Client(), kind='ws') as socket:
                    assert socket is not http
                    assert pool.stats()['clients'] == 1 and pool.stats()['ws_active'] == 1
    await pool.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(('shared', 'expected'), [(True, 5), (False, 15)])
async def test_three_keys_reach_only_their_aggregate_capacity(store, shared, expected):
    first = add(store, 'a', concurrency_limit=5)
    for key in ('b', 'c'):
        add(store, key, **({'quota_group_id': first['quota_group_id']} if shared else {'concurrency_limit': 5}))
    pool = ProviderPool(store)
    await pool.reload()
    release = asyncio.Event()
    entered = []

    async def work():
        async with pool.lease('fish', 's1'):
            entered.append(1)
            await release.wait()
    tasks = [asyncio.create_task(work()) for _ in range(16)]
    try:
        for _ in range(500):
            if len(entered) == expected:
                break
            await asyncio.sleep(.001)
        assert len(entered) == expected
        assert pool.stats()['active'] == expected
    finally:
        release.set()
        await asyncio.gather(*tasks)
        await pool.aclose()


@pytest.mark.asyncio
async def test_thousand_stored_resources_reload_without_clients(store):
    first = add(store, 'account-0')
    for number in range(1, 1000):
        add(store, f'account-{number}', quota_group_id=first['quota_group_id'])
    pool = ProviderPool(store)
    await pool.reload()
    stats = pool.stats(include_resources=False)
    assert stats['providers']['fish']['resources'] == 1000
    assert stats['clients'] == stats['active'] == stats['http_active'] == stats['ws_active'] == 0
    async with pool.lease('fish', 's1'):
        assert pool.stats(include_resources=False)['clients'] == 0
    await pool.aclose()


@pytest.mark.asyncio
async def test_concurrent_reload_cannot_restore_older_credential_snapshot(store, monkeypatch):
    item = add(store, 'a')
    pool = ProviderPool(store)
    await pool.reload()
    entered = threading.Event()
    release = threading.Event()
    original = store.snapshot
    calls = 0

    def delayed_snapshot():
        nonlocal calls
        calls += 1
        result = original()
        if calls == 1:
            entered.set()
            release.wait(timeout=2)
        return result

    monkeypatch.setattr(store, 'snapshot', delayed_snapshot)
    old_reload = asyncio.create_task(pool.reload())
    await asyncio.to_thread(entered.wait, 1)
    updated = store.update(item['id'], {'api_key': 'newest-secret'})
    store.verification_result(item['id'], updated['revision'], True)
    new_reload = asyncio.create_task(pool.reload())
    await asyncio.sleep(.02)
    release.set()
    await asyncio.gather(old_reload, new_reload)
    async with pool.lease('fish', 's1') as lease:
        assert lease.api_key == 'newest-secret'
    await pool.aclose()


@pytest.mark.asyncio
async def test_weighted_selection_counts_shared_group_load_not_credential_load(store):
    from contextlib import AsyncExitStack
    first = add(store, 'shared-a', concurrency_limit=5)
    for key in ('shared-b', 'shared-c'):
        add(store, key, quota_group_id=first['quota_group_id'])
    other = add(store, 'independent', concurrency_limit=5)
    pool = ProviderPool(store)
    await pool.reload()
    selected = []
    async with AsyncExitStack() as stack:
        for _ in range(4):
            lease = await stack.enter_async_context(pool.lease('fish', 's1'))
            selected.append(lease.quota_group_id)
        assert selected.count(first['quota_group_id']) == 2
        assert selected.count(other['quota_group_id']) == 2
    await pool.aclose()


@pytest.mark.asyncio
async def test_probe_observation_seeds_latency_and_rejects_stale_revision(store):
    item = add(store, 'a')
    pool = ProviderPool(store)
    await pool.reload()
    assert pool.observe_probe(item['id'], item['revision'], 250)
    assert pool.resource_stats(item['id'])['ewma_seconds'] == .25
    updated = store.update(item['id'], {'api_key': 'new-secret'})
    store.verification_result(item['id'], updated['revision'], True)
    await pool.reload()
    assert not pool.observe_probe(item['id'], item['revision'], 10000)
    assert pool.observe_probe(item['id'], updated['revision'], 400)
    assert pool.resource_stats(item['id'])['ewma_seconds'] == .4
    await pool.aclose()


@pytest.mark.asyncio
async def test_metric_snapshots_are_bounded_secret_free_and_revision_guarded(store):
    from app.providers.metrics import write_snapshots
    item = add(store, 'a')
    pool = ProviderPool(store)
    await pool.reload()
    pool.observe_probe(item['id'], item['revision'], 250)
    async with pool.lease('fish', 's1'):
        pass
    await pool.flush_metrics()
    await pool.flush_metrics()
    rows = store.conn.execute('SELECT * FROM provider_metric_snapshots').fetchall()
    assert len(rows) == 1 and rows[0]['completed'] == 1
    assert 'secret-a' not in repr([tuple(row) for row in rows])
    old = dict(rows[0])
    stale_observation = dict(old, at=old['at'] - 1, completed=500)
    write_snapshots(store, [stale_observation])
    assert store.conn.execute('SELECT completed FROM provider_metric_snapshots').fetchone()[0] == 1
    updated = store.update(item['id'], {'api_key': 'replacement-secret'})
    store.verification_result(item['id'], updated['revision'], True)
    await pool.reload()
    await pool.flush_metrics()
    old['at'] += 1000000
    old['completed'] = 500
    write_snapshots(store, [old])
    row = store.conn.execute('SELECT * FROM provider_metric_snapshots').fetchone()
    assert row['revision'] == updated['revision'] and row['completed'] == 1
    await pool.aclose()
    restarted = ProviderPool(store)
    await restarted.reload()
    assert restarted.resource_stats(item['id'])['completed'] == 1
    assert restarted.stats()['active'] == 0
    store.delete(item['id'])
    await restarted.reload()
    await restarted.flush_metrics()
    assert store.conn.execute('SELECT COUNT(*) FROM provider_metric_snapshots').fetchone()[0] == 0
    await restarted.aclose()


@pytest.mark.asyncio
async def test_probe_latency_weights_predicted_group_finish(store):
    from contextlib import AsyncExitStack
    fast = add(store, 'fast', concurrency_limit=5)
    slow = add(store, 'slow', concurrency_limit=5)
    pool = ProviderPool(store)
    await pool.reload()
    pool.observe_probe(fast['id'], fast['revision'], 250)
    pool.observe_probe(slow['id'], slow['revision'], 1000)
    selected = []
    async with AsyncExitStack() as stack:
        for _ in range(5):
            lease = await stack.enter_async_context(pool.lease('fish', 's1'))
            selected.append(lease.resource_id)
        assert selected.count(fast['id']) == 4
        assert selected.count(slow['id']) == 1
    await pool.aclose()


@pytest.mark.asyncio
async def test_periodic_metric_flush_and_shutdown_write_latest_counters(store):
    from app.providers.metrics import periodic_flush
    item = add(store, 'a')
    pool = ProviderPool(store)
    await pool.reload()
    periodic = asyncio.create_task(periodic_flush(pool, interval=.01))
    try:
        async with pool.lease('fish', 's1'):
            pass
        for _ in range(100):
            with store._lock:
                row = store.conn.execute('SELECT completed FROM provider_metric_snapshots').fetchone()
            if row:
                break
            await asyncio.sleep(.002)
        assert row[0] == 1
    finally:
        periodic.cancel()
        await asyncio.gather(periodic, return_exceptions=True)
    async with pool.lease('fish', 's1'):
        pass
    await pool.aclose()
    row = store.conn.execute('SELECT completed,resource_id FROM provider_metric_snapshots').fetchone()
    assert row[0] == 2 and row[1] == item['id']


@pytest.mark.asyncio
async def test_probe_http_client_is_disposable_and_cannot_poison_business_cache(store):
    item = store.create(dict(provider='llm', name='draft', api_key='fake-llm-secret', model='model-a'))
    pool = ProviderPool(store)
    await pool.reload()

    class PlainProbeClient:
        closed = False
        async def aclose(self):
            self.closed = True

    class BusinessClient:
        async def aclose(self):
            pass

    async with pool.probe(item['id'], item['revision']) as lease:
        async with pool.client(lease, lambda _: PlainProbeClient()) as probe:
            assert isinstance(probe, PlainProbeClient)
    assert probe.closed and pool.stats()['clients'] == 0
    store.verification_result(item['id'], item['revision'], True)
    await pool.reload()
    async with pool.lease('llm', 'model-a') as lease:
        async with pool.client(lease, lambda _: BusinessClient()) as business:
            assert isinstance(business, BusinessClient)
    await pool.aclose()


@pytest.mark.asyncio
async def test_hot_quota_reassignment_drains_old_group_before_applying_new_group(store):
    first = add(store, 'a', concurrency_limit=1)
    second = add(store, 'b', concurrency_limit=1)
    pool = ProviderPool(store)
    await pool.reload()
    old_context = pool.lease('fish', 's1', exclude_resource_ids={second['id']})
    old = await old_context.__aenter__()
    try:
        updated = store.update(first['id'], {'quota_group_id': second['quota_group_id']})
        await pool.reload()
        current = pool.resource_stats(first['id'])
        assert current['state'] == 'draining'
        assert current['quota_group_id'] == first['quota_group_id']
        assert current['pending_quota_group_id'] == second['quota_group_id']
        assert current['pending_revision'] == updated['revision']
        assert old.quota_group_id == first['quota_group_id']
        async with pool.lease('fish', 's1') as other:
            assert other.resource_id == second['id']
            assert pool._groups[first['quota_group_id']].active == 1
            assert pool._groups[second['quota_group_id']].active == 1
            with pytest.raises(PoolError):
                async with pool.lease('fish', 's1', exclude_resource_ids={second['id']},
                                      deadline=asyncio.get_running_loop().time() + .01):
                    pass
    finally:
        await old_context.__aexit__(None, None, None)
    current = pool.resource_stats(first['id'])
    assert current['state'] == 'active' and current['quota_group_id'] == second['quota_group_id']
    assert pool._groups[first['quota_group_id']].active == 0
    async with pool.lease('fish', 's1', exclude_resource_ids={first['id']}):
        with pytest.raises(PoolError):
            async with pool.lease('fish', 's1', exclude_resource_ids={second['id']},
                                  deadline=asyncio.get_running_loop().time() + .01):
                pass
    async with pool.lease('fish', 's1', exclude_resource_ids={second['id']}) as moved:
        assert moved.resource_id == first['id'] and moved.quota_group_id == second['quota_group_id']
    await pool.aclose()


@pytest.mark.asyncio
async def test_active_quota_change_public_guard_rejects_until_drained(store):
    first = add(store, 'a')
    second = add(store, 'b')
    pool = ProviderPool(store)
    await pool.reload()
    async with pool.lease('fish', 's1', exclude_resource_ids={second['id']}):
        with pytest.raises(PoolError) as caught:
            pool.ensure_update_allowed(first['id'], {'quota_group_id': second['quota_group_id']})
        assert caught.value.status_code == 409
        pool.ensure_update_allowed(first['id'], {'name': 'allowed'})
    pool.ensure_update_allowed(first['id'], {'quota_group_id': second['quota_group_id']})
    await pool.aclose()


@pytest.mark.asyncio
async def test_cancellation_during_actual_ws_close_drains_before_releasing_any_slot(store):
    add(store, 'a')
    pool = ProviderPool(store, ws_limit=1)
    await pool.reload()
    entered, finish, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    closes = []

    class Socket:
        async def close(self):
            closes.append(1)
            entered.set()
            await finish.wait()
            completed.set()

    async def work():
        async with pool.lease('fish', 's1') as lease:
            async with pool.client(lease, lambda _: Socket(), kind='ws'):
                pass

    task = asyncio.create_task(work())
    await entered.wait()
    try:
        task.cancel()
        await asyncio.sleep(.01)
        task.cancel()
        await asyncio.sleep(.01)
        assert not task.done()
        assert pool.stats()['active'] == 1 and pool.stats()['ws_active'] == 1
        assert len(pool._sessions) == 1 and pool._transport['ws']._value == 0
        assert not completed.is_set()
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled() and completed.is_set()
    assert pool.stats()['active'] == pool.stats()['ws_active'] == 0
    assert not pool._sessions and pool._transport['ws']._value == 1
    assert len(closes) == 1
    await pool.aclose()


@pytest.mark.asyncio
async def test_failed_ws_close_is_visible_and_retains_capacity_until_successful_retry(store):
    add(store, 'a')
    pool = ProviderPool(store, ws_limit=1)
    await pool.reload()
    attempts = []

    class Socket:
        async def close(self):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError('offline close failure')

    with pytest.raises(RuntimeError, match='offline close failure'):
        async with pool.lease('fish', 's1') as lease:
            async with pool.client(lease, lambda _: Socket(), kind='ws'):
                pass
    assert pool.stats()['active'] == pool.stats()['ws_active'] == 1
    assert pool._transport['ws']._value == 0 and len(pool._sessions) == 1
    await pool.aclose()
    assert len(attempts) == 2
    assert pool.stats()['active'] == pool.stats()['ws_active'] == 0
    assert pool._transport['ws']._value == 1 and not pool._sessions


@pytest.mark.asyncio
async def test_pool_shutdown_drains_all_client_closes_under_repeated_cancellation(store):
    first = add(store, 'a')
    second = add(store, 'b')
    pool = ProviderPool(store)
    await pool.reload()
    entered, finish = asyncio.Event(), asyncio.Event()
    clients = []

    class Client:
        closed = False
        async def aclose(self):
            if sum(getattr(client, 'closing', False) for client in clients) == 1:
                entered.set()
            self.closing = True
            await finish.wait()
            self.closed = True

    def factory(lease):
        client = Client()
        clients.append(client)
        return client

    for excluded in ({second['id']}, {first['id']}):
        async with pool.lease('fish', 's1', exclude_resource_ids=excluded) as lease:
            async with pool.client(lease, factory):
                pass
    shutdown = asyncio.create_task(pool.aclose())
    await entered.wait()
    try:
        shutdown.cancel()
        await asyncio.sleep(.01)
        shutdown.cancel()
        await asyncio.sleep(.01)
        assert not shutdown.done() and not any(client.closed for client in clients)
    finally:
        finish.set()
        await asyncio.gather(shutdown, return_exceptions=True)
    assert shutdown.cancelled() and all(client.closed for client in clients)
    assert not pool._closing_clients and pool.stats()['clients'] == 0
    await pool.aclose()
