"""Admin-only provider resources; explicit probes are the only real-key tests."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import websockets
from fastapi import APIRouter, Body, HTTPException, Request

from app.admin.api import deps
from app.jobs import run_db_thread
from app.providers.probes import probe_resource
from app.providers.pool import PoolError

router = APIRouter(prefix='/provider-resources')


def services(request):
    value = deps.services(request)
    if not getattr(value, 'provider_pool', None):
        raise HTTPException(status_code=503, detail='上游资源池尚未初始化')
    return value


def public_error(exc):
    status = getattr(exc, 'status_code', 422 if isinstance(exc, ValueError) else 404 if isinstance(exc, KeyError) else 503)
    # Store errors contain field names, never raw values. Upstream errors use
    # fixed business-probe messages, not bodies that could echo an API key.
    from app.providers.store import StoreError
    message = str(exc) if isinstance(exc, (StoreError, PoolError)) else '资源暂不可用，请刷新配置后重试'
    headers = {'Retry-After': str(getattr(exc, 'retry_after', 1))} if status in (429, 503) else None
    return HTTPException(status_code=status, detail=message, headers=headers)


async def mutate(request, function, *args, **kwargs):
    value = services(request)
    try:
        async def apply():
            result = await run_db_thread(function, *args, **kwargs)
            await value.provider_pool.reload()
            return result
        task = asyncio.create_task(apply())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Configuration and its live snapshot are one logical operation.
            # A disconnected admin cannot leave a committed old key in use.
            await task
            raise
    except Exception as exc:
        raise public_error(exc) from None


def group_budgets(store, groups):
    groups = list(set(groups))
    if not groups:
        return {}
    day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    with store._lock:
        rows = store.conn.execute(
            'SELECT quota_group_id,SUM(CASE WHEN day=? THEN requests ELSE 0 END),SUM(requests) '
            'FROM provider_group_usage WHERE quota_group_id IN (' + ','.join('?' for _ in groups) + ') '
            'AND day>=? AND day<? GROUP BY quota_group_id',
            (day, *groups, day[:7]+'-01', day[:7]+'-32')).fetchall()
    result = {group: {'daily_requests': 0, 'monthly_requests': 0} for group in groups}
    result.update({row[0]: {'daily_requests': int(row[1]), 'monthly_requests': int(row[2])} for row in rows})
    return result


@router.get('/stats')
async def stats(request: Request, provider: str | None = None, resource_ids: str = ''):
    pool = services(request).provider_pool
    ids = [item for item in resource_ids.split(',') if item][:100]
    snapshot = pool.stats(provider, include_resources=False)
    groups = snapshot.get('providers', {})
    if isinstance(groups, dict):
        names = [provider] if provider else ['fish', 'deepgram', 'llm', 'siliconflow']
        groups = [dict(groups.get(name, {'enabled': False}), provider=name) for name in names]
    resources = [pool.resource_stats(rid) for rid in ids if pool.resource_stats(rid)]
    budgets = await run_db_thread(group_budgets, pool.store, [row['quota_group_id'] for row in resources])
    for row in resources:
        row.update(budgets.get(row['quota_group_id'], {}))
    return {'providers': groups, 'resources': resources}


@router.get('/quota-groups')
async def quota_groups(request: Request, provider: str | None = None):
    return {'items': await run_db_thread(services(request).provider_store.list_quota_groups, provider)}


@router.post('/quota-groups')
async def quota_group(request: Request, payload: dict = Body(...)):
    return {'group': await mutate(request, services(request).provider_store.upsert_quota_group, payload)}


@router.get('/routing-groups')
async def routing_groups(request: Request, provider: str | None = None):
    return {'items': await run_db_thread(services(request).provider_store.list_route_groups, provider)}


@router.post('/routing-groups')
async def routing_group(request: Request, payload: dict = Body(...)):
    return {'group': await mutate(request, services(request).provider_store.upsert_route_group, payload)}


@router.put('/{provider}/mode')
async def mode(request: Request, provider: str, payload: dict = Body(...)):
    if not isinstance(payload.get('enabled'), bool):
        raise HTTPException(status_code=422, detail='enabled 必须为布尔值')
    await mutate(request, services(request).provider_store.set_provider_enabled, provider, payload['enabled'])
    return {'provider': provider, 'enabled': payload['enabled']}


@router.get('')
async def resources(request: Request, provider: str | None = None, page: int = 1,
                    page_size: int = 25, status: str | None = None, route_group_id: str | None = None):
    value = services(request)
    try:
        result = await run_db_thread(value.provider_store.list_resources, provider, page, page_size, status, route_group_id)
    except Exception as exc:
        raise public_error(exc) from None
    budgets = await run_db_thread(group_budgets, value.provider_store, [row['quota_group_id'] for row in result['items']])
    for resource in result['items']:
        runtime = value.provider_pool.resource_stats(resource['id']) or {}
        runtime.update(budgets.get(resource['quota_group_id'], {}))
        resource['runtime'] = runtime
        resource['state'] = runtime.get('state', resource['state'])
    return result


@router.post('')
async def create(request: Request, payload: dict = Body(...)):
    return {'resource': await mutate(request, services(request).provider_store.create, payload)}


@router.patch('/{resource_id}')
async def update(request: Request, resource_id: str, payload: dict = Body(...)):
    patch = dict(payload)
    revision = patch.pop('revision', None)
    try:
        services(request).provider_pool.ensure_update_allowed(resource_id, patch)
    except PoolError as exc:
        raise public_error(exc) from None
    return {'resource': await mutate(request, services(request).provider_store.update, resource_id, patch, revision)}


class _ProbeSocket:
    def __init__(self, socket, context):
        self.socket, self.context = socket, context
    def __aiter__(self):
        return self.socket.__aiter__()
    async def send(self, value):
        await self.socket.send(value)
    async def close(self):
        context, self.context = self.context, None
        if context:
            await context.__aexit__(None, None, None)


@router.post('/{resource_id}/verify')
async def verify(request: Request, resource_id: str, payload: dict | None = Body(None)):
    value = services(request)
    pool, store = value.provider_pool, value.provider_store
    try:
        resource = await run_db_thread(store.get, resource_id, True)
        revision = (payload or {}).get('revision', resource['revision'])
        if revision != resource['revision']:
            raise HTTPException(status_code=409, detail='配置已更改，请刷新后重新验证')
        slots = getattr(value, '_provider_probe_slots', None)
        if slots is None:
            value._provider_probe_slots = slots = asyncio.Semaphore(2)
        async with asyncio.timeout(20):
            async with slots:
                async with pool.probe(resource_id, revision) as lease:
                    if resource['provider'] == 'deepgram':
                        async def connect(url, **kwargs):
                            context = pool.client(lease, lambda _: websockets.connect(url, **kwargs), kind='ws')
                            socket = await context.__aenter__()
                            return _ProbeSocket(socket, context)
                        result = await probe_resource(resource, ws_connect=connect)
                    else:
                        factory = lambda _: httpx.AsyncClient(timeout=15,
                            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1))
                        async with pool.client(lease, factory) as client:
                            budget = getattr(getattr(value, 'chat', None), 'usage_budget', None)
                            if resource['provider'] == 'llm' and budget is not None:
                                await run_db_thread(budget.reserve, 'llm')
                            result = await probe_resource(resource, client=client)
                    if result.get('ok'):
                        lease.success()
                    else:
                        lease.failure(status=result.get('status_code', 0), code='probe_failed')
        public = await mutate(request, store.verification_result, resource_id, revision,
                              bool(result.get('ok')), result.get('message', ''))
        if result.get('ok'):
            pool.observe_probe(resource_id, revision, result.get('latency_ms', 0))
        return {'ok': bool(result.get('ok')), 'resource': public, 'message': result.get('message', '')}
    except HTTPException:
        raise
    except Exception as exc:
        raise public_error(exc) from None


@router.delete('/{resource_id}')
async def delete(request: Request, resource_id: str):
    result = await mutate(request, services(request).provider_store.delete, resource_id)
    return {'ok': True, 'resource': result}
