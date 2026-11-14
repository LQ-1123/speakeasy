"""Bounded, loop-local provider scheduling and lazy clients.

Only reload reads the resource catalogue. Route heaps index cached metadata;
admission touches a route, its shared quota counters and one short off-loop SQLite
budget transaction. Unknown token usage is charged conservatively (1024 tokens,
or a caller-supplied estimate); ``success(usage)`` reconciles exact upstream usage.
No retry is performed here: adapters own the at-most-two safe candidate policy.
"""
from __future__ import annotations

import asyncio
import heapq
import inspect
import math
import threading
import time
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from app.capacity import CapacityError
from app.jobs import run_db_thread


class PoolError(CapacityError):
    def __init__(self, message='Provider capacity is temporarily unavailable', *, status_code=429, retry_after=1):
        super().__init__(message, status_code=status_code,
                         retry_after=max(0, math.ceil(float(retry_after or 0))))


@dataclass
class _Resource:
    meta: dict
    active: int = 0
    ewma: float = 1.0
    failures: int = 0
    cooldown_until: float = 0
    breaker_until: float = 0
    half_open: bool = False
    state: str = 'pending'
    completed: int = 0
    errors: int = 0
    generation: int = 0
    denied_voices: OrderedDict = field(default_factory=OrderedDict)
    health_updated_ns: int = 0
    pending_meta: dict | None = None


@dataclass
class _Group:
    meta: dict
    active: int = 0
    events: deque = field(default_factory=deque)
    tokens: int = 0
    budget_until: float = 0
    ewma: float = 1.0
    completed: int = 0
    generation: int = 0


@dataclass
class _Waiter:
    future: asyncio.Future
    provider: str
    model: str
    capability: str | dict | None
    priority: int
    sequence: int
    deadline: float
    estimated_tokens: int | None
    excluded: frozenset
    probe_meta: dict | None = None


@dataclass
class _Client:
    key: tuple
    task: asyncio.Task | None = None
    value: object = None
    refs: int = 0
    draining: bool = False
    closed: bool = False
    close_task: asyncio.Task | None = None
    lease: object = None
    slot_released: bool = False


def _retry_seconds(value):
    try:
        return max(0.0, float(value))
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(str(value))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0.0, date.timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return 1.0


def _usage_tokens(usage):
    if usage is None:
        return None
    if isinstance(usage, (int, float)):
        return max(0, int(usage))
    if not isinstance(usage, dict):
        usage = {name: getattr(usage, name, None) for name in ('total_tokens', 'prompt_tokens', 'completion_tokens', 'input_tokens', 'output_tokens')}
    if usage.get('total_tokens') is not None:
        return max(0, int(usage['total_tokens']))
    pair = ('prompt_tokens', 'completion_tokens') if any(usage.get(k) is not None for k in ('prompt_tokens', 'completion_tokens')) else ('input_tokens', 'output_tokens')
    if any(usage.get(k) is not None for k in pair):
        return sum(max(0, int(usage.get(k) or 0)) for k in pair)
    return None


class ProviderLease:
    def __init__(self, pool, resource, group, waiter, event):
        self._pool, self._resource, self._group = pool, resource, group
        meta = waiter.probe_meta or resource.meta
        self.resource_id, self.quota_group_id = meta['id'], meta['quota_group_id']
        self.revision = meta['revision']
        self.api_key, self.base_url, self.model = meta['api_key'], meta['base_url'], meta['model']
        self.provider = meta['provider']
        self.options = dict(meta.get('options') or {})
        self.capabilities = dict(meta.get('capabilities') or {})
        self.deadline = waiter.deadline
        self.output_started = False
        self._started = time.monotonic()
        self._event = event
        self._reservation = None
        self._actual_tokens = None
        self._outcome = None
        self._released = False
        self._is_probe = waiter.probe_meta is not None
        self._open_sessions = 0
        self._release_requested = False

    def __repr__(self):
        return f'<ProviderLease resource_id={self.resource_id!r} revision={self.revision}>'

    def mark_output_started(self):
        self.output_started = True

    def success(self, usage=None):
        if self._outcome is not None or self._released:
            return
        try:
            self._actual_tokens = _usage_tokens(usage)
        except (TypeError, ValueError, OverflowError):
            self._actual_tokens = None
        self._outcome = 'success'

    def failure(self, status=0, code='', retry_after=None):
        if self._outcome is not None or self._released:
            return
        self._outcome = 'failure'
        self._pool._failure(self, status, code, retry_after)


class ProviderPool:
    def __init__(self, store, http_limit=32, ws_limit=16, queue_limit=64, client_limit=16):
        self.store = store
        self.http_limit, self.ws_limit = max(1, int(http_limit)), max(1, int(ws_limit))
        self.queue_limit, self.client_limit = max(0, int(queue_limit)), max(1, int(client_limit))
        self._lock = threading.RLock()
        self._resources, self._groups, self._routes = {}, {}, {}
        self._members, self._member_ids, self._group_routes, self._route_groups = {}, {}, {}, {}
        self._activated, self._modes = set(), {}
        self._waiting = []
        self._sequence = 0
        self._client_sequence = 0
        self._closed = False
        self._reloading = False
        self._reload_lock = asyncio.Lock()
        self._loop = None
        self._timer = None
        self._tasks = set()
        self._clients = OrderedDict()
        self._sessions = {}
        self._closing_clients = {}
        self._close_pool_task = None
        self._client_changed = asyncio.Event()
        self._transport = {'http': asyncio.Semaphore(self.http_limit), 'ws': asyncio.Semaphore(self.ws_limit)}
        self._transport_active = {'http': 0, 'ws': 0}
        self._counts = dict(accepted=0, completed=0, rejected=0, timeouts=0)

    def _bind(self):
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError('ProviderPool belongs to a different event loop')
        self._loop = loop
        return loop

    def _background(self, awaitable):
        task = self._loop.create_task(awaitable)
        self._tasks.add(task)
        def done(finished):
            self._tasks.discard(finished)
            if not finished.cancelled():
                # Consume errors without logging upstream secrets or credentials.
                finished.exception()
        task.add_done_callback(done)
        return task

    async def reload(self):
        self._bind()
        # Concurrent admin mutations must never apply snapshots out of order.
        # Pause new admission during the short off-loop credential read stage.
        async with self._reload_lock:
            with self._lock:
                self._reloading = True
            try:
                await self._reload_snapshot()
            finally:
                with self._lock:
                    self._reloading = False
                    self._dispatch()

    async def _reload_snapshot(self):
        self._bind()
        snapshot = await run_db_thread(self.store.snapshot)
        close, finalize = [], []
        with self._lock:
            if self._closed:
                return
            self._modes = snapshot['modes']
            self._activated = {(r['provider'], r['model']) for r in snapshot['route_groups'] if r['activated_once']}
            incoming = {r['id']: r for r in snapshot['resources']}
            incoming_groups = {r['quota_group_id'] for r in incoming.values()}
            for gid, old in list(self._groups.items()):
                if not old.active and gid not in incoming_groups:
                    self._groups.pop(gid, None)
            new_groups = set()
            for group in snapshot['quota_groups']:
                if group['id'] not in self._groups:
                    new_groups.add(group['id'])
                runtime = self._groups.setdefault(group['id'], _Group(group))
                runtime.meta = group
                runtime.budget_until = self._budget_reset(group)
            for rid, old in list(self._resources.items()):
                if rid not in incoming:
                    if old.active:
                        old.pending_meta = None
                        old.state = 'draining'
                        old.meta = dict(old.meta, deleted=True, enabled=False)
                    else:
                        self._resources.pop(rid, None)
            for rid, meta in incoming.items():
                old = self._resources.get(rid)
                if old is None:
                    old = self._resources[rid] = _Resource(meta, state=meta['state'])
                    metric = snapshot.get('metrics', {}).get(rid)
                    if metric and metric['revision'] == meta['revision']:
                        old.ewma = max(.001, float(metric['ewma']))
                        old.completed, old.errors = metric['completed'], metric['errors']
                if old.active and (old.pending_meta is not None or old.meta['quota_group_id'] != meta['quota_group_id']):
                    # A lease owns the ledger it reserved. Keep that effective
                    # identity until all pinned work ends, and admit no new work
                    # against either the old or newly configured account.
                    old.pending_meta = meta
                    old.state = 'draining'
                    old.generation += 1
                    continue
                changed = old.meta['revision'] != meta['revision']
                old.meta = meta
                if changed:
                    old.failures = 0
                    old.breaker_until = 0
                    old.half_open = False
                    old.denied_voices.clear()
                # The DB snapshot can race a health write; preserve same-revision
                # cooldown until its timer, unless configuration was changed.
                preserve = not changed and meta.get('health_updated_ns', 0) < old.health_updated_ns
                if not preserve:
                    old.state = meta['state']
                    old.cooldown_until = time.monotonic() + max(0, float(meta.get('health_until', 0)) - time.time())
                    old.health_updated_ns = meta.get('health_updated_ns', 0)
                if meta.get('deleted'):
                    old.state = 'draining'
                    if not old.active:
                        finalize.append(rid)
                old.generation += 1
            self._rebuild_indices()
            for gid in new_groups:
                members = [self._resources[rid] for route in self._group_routes.get(gid, ())
                           for rid in self._member_ids.get((route, gid), ())]
                if members:
                    self._groups[gid].ewma = sum(r.ewma for r in members) / len(members)
                    self._groups[gid].completed = sum(r.completed for r in members)
            for route, group_ids in self._route_groups.items():
                self._routes[route] = [self._group_item(gid) for gid in group_ids]
                heapq.heapify(self._routes[route])
            for key, entry in list(self._clients.items()):
                configured = incoming.get(key[0])
                if configured is None or configured['revision'] != key[1] or not configured['enabled'] or configured.get('deleted'):
                    entry.draining = True
                    if not entry.refs:
                        self._clients.pop(key, None)
                        close.append(entry)
            self._client_changed.set()
            self._dispatch()
        for entry in close:
            await self._close_client(entry)
        for rid in finalize:
            await run_db_thread(self.store.finalize_delete, rid)
            with self._lock:
                resource = self._resources.get(rid)
                if resource and not resource.active and resource.meta.get('deleted'):
                    self._resources.pop(rid, None)

    def applies(self, provider, model):
        with self._lock:
            return not self._closed and self._modes.get(provider, False) and (provider, model) in self._activated

    def ensure_update_allowed(self, resource_id, patch):
        """Optional API preflight; reload still safely defers a racing edit."""
        with self._lock:
            resource = self._resources.get(resource_id)
            target = patch.get('quota_group_id')
            if resource and resource.active and target is not None and target != resource.meta['quota_group_id']:
                raise PoolError('Resource has active leases; wait for it to drain before changing its quota group',
                                status_code=409)

    def _rebuild_indices(self):
        """Rebuild cached indices at reload or a rare deferred admin handoff."""
        self._routes, self._members, self._member_ids, self._group_routes, self._route_groups = {}, {}, {}, {}, {}
        for resource in self._resources.values():
            meta = resource.meta
            if meta.get('enabled') and meta.get('verified_at') and meta.get('api_key') and not meta.get('deleted'):
                route = (meta['provider'], meta['model'])
                gid = meta['quota_group_id']
                self._members.setdefault((route, gid), []).append(self._heap_item(resource))
                self._member_ids.setdefault((route, gid), set()).add(meta['id'])
                self._group_routes.setdefault(gid, set()).add(route)
                self._route_groups.setdefault(route, set()).add(gid)
        for heap in self._members.values():
            heapq.heapify(heap)
        for route, group_ids in self._route_groups.items():
            self._routes[route] = [self._group_item(gid) for gid in group_ids]
            heapq.heapify(self._routes[route])

    def _apply_pending(self, resource):
        resource.meta = resource.pending_meta
        resource.pending_meta = None
        resource.state = resource.meta['state']
        resource.failures = 0
        resource.half_open = False
        resource.breaker_until = 0
        resource.denied_voices.clear()
        resource.cooldown_until = time.monotonic() + max(0, float(resource.meta.get('health_until', 0)) - time.time())
        resource.health_updated_ns = resource.meta.get('health_updated_ns', 0)
        resource.generation += 1
        self._rebuild_indices()

    @staticmethod
    def _heap_item(resource):
        cap = max(1, resource.meta.get('concurrency_limit', 1))
        score = (resource.active + 1) * resource.ewma / cap
        return (score, resource.completed, resource.meta['id'], resource.generation)

    def _group_item(self, gid):
        group = self._groups[gid]
        score = (group.active + 1) * group.ewma / max(1, group.meta['concurrency_limit'])
        return (score, group.completed, gid, group.generation)

    def _index(self, resource):
        resource.generation += 1
        route = (resource.meta['provider'], resource.meta['model'])
        gid = resource.meta['quota_group_id']
        members = self._members.get((route, gid))
        if members is not None:
            heapq.heappush(members, self._heap_item(resource))
            if len(members) > max(64, 4 * len(self._member_ids.get((route, gid), ()))):
                members[:] = [self._heap_item(self._resources[rid]) for rid in self._member_ids[(route, gid)]
                              if rid in self._resources]
                heapq.heapify(members)
        group = self._groups.get(gid)
        if group:
            group.generation += 1
            for group_route in self._group_routes.get(gid, ()):
                heap = self._routes[group_route]
                heapq.heappush(heap, self._group_item(gid))
                if len(heap) > max(64, 4 * len(self._route_groups[group_route])):
                    heap[:] = [self._group_item(member) for member in self._route_groups[group_route]
                               if member in self._groups]
                    heapq.heapify(heap)

    def _route_resources(self, route):
        for gid in self._route_groups.get(route, ()):
            for rid in self._member_ids.get((route, gid), ()):
                resource = self._resources.get(rid)
                if resource is not None:
                    yield resource

    def _trim_events(self, group, now):
        while group.events and group.events[0][0] <= now - 60:
            event = group.events.popleft()
            group.tokens -= event[1]

    @staticmethod
    def _budget_reset(meta):
        now = datetime.now(timezone.utc)
        day = now.strftime('%Y-%m-%d')
        if meta.get('usage_day', day) != day:
            if meta.get('usage_day', day)[:7] != day[:7]:
                meta['monthly_requests'] = 0
            meta['daily_requests'] = 0
            meta['usage_day'] = day
        seconds = 0
        if meta.get('daily_limit') and meta.get('daily_requests', 0) >= meta['daily_limit']:
            reset = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            seconds = max(seconds, (reset - now).total_seconds())
        if meta.get('monthly_limit') and meta.get('monthly_requests', 0) >= meta['monthly_limit']:
            year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
            seconds = max(seconds, (datetime(year, month, 1, tzinfo=timezone.utc) - now).total_seconds())
        return time.monotonic() + seconds if seconds else 0

    def _all_budget_blocked(self, waiter, now):
        if waiter.probe_meta:
            group = self._groups.get(waiter.probe_meta['quota_group_id'])
            return bool(group and group.budget_until > now)
        found = False
        for resource in self._route_resources((waiter.provider, waiter.model)):
            if resource.meta['id'] in waiter.excluded:
                continue
            if not self._matches_capability(resource, waiter.capability):
                continue
            group = self._groups.get(resource.meta['quota_group_id'])
            if group:
                found = True
                if group.budget_until <= now:
                    return False
        return found

    @staticmethod
    def _matches_capability(resource, requested):
        if not requested:
            return True
        capabilities = resource.meta.get('capabilities') or {}
        if isinstance(requested, str):
            return bool(capabilities.get(requested))
        if not isinstance(requested, dict):
            return False
        for key, value in requested.items():
            if key == 'reference_id':
                if value in resource.denied_voices:
                    return False
                voices = capabilities.get('voices') or capabilities.get('reference_ids')
                if voices and value not in voices:
                    return False
            elif key == 'language':
                languages = capabilities.get('languages')
                if languages and value not in languages:
                    return False
            elif capabilities.get(key) != value:
                return False
        return True

    def _choose(self, waiter, now):
        if waiter.probe_meta is not None:
            return self._choose_probe(waiter, now)
        if not self._modes.get(waiter.provider, False):
            return None
        heap = self._routes.get((waiter.provider, waiter.model))
        if not heap:
            return None
        skipped_groups, selected = [], None
        while heap:
            group_item = heapq.heappop(heap)
            group = self._groups.get(group_item[2])
            if not group or group_item[3] != group.generation:
                continue
            self._trim_events(group, now)
            limits = group.meta
            estimated = waiter.estimated_tokens
            if estimated is None:
                estimated = min(1024, limits['tpm_limit']) if limits['tpm_limit'] else 0
            blocked = (group.active >= limits['concurrency_limit'] or group.budget_until > now
                       or (limits['rpm_limit'] and len(group.events) >= limits['rpm_limit'])
                       or (limits['tpm_limit'] and group.tokens + estimated > limits['tpm_limit']))
            resource = None if blocked else self._choose_member(waiter, group, now)
            if resource:
                selected = resource, group, estimated
                break
            skipped_groups.append(group_item)
        for item in skipped_groups:
            heapq.heappush(heap, item)
        if selected:
            resource, group, estimated = selected
            event = [now, estimated]
            group.events.append(event)
            group.tokens += estimated
            group.active += 1
            resource.active += 1
            if resource.failures >= 3:
                resource.half_open = True
            self._index(resource)
            return ProviderLease(self, resource, group, waiter, event)
        return None

    def _choose_member(self, waiter, group, now):
        heap = self._members.get(((waiter.provider, waiter.model), group.meta['id']), [])
        skipped, selected = [], None
        while heap:
            item = heapq.heappop(heap)
            resource = self._resources.get(item[2])
            if resource is None or item[3] != resource.generation:
                continue
            meta = resource.meta
            blocked = (meta['id'] in waiter.excluded or not meta.get('enabled') or not meta.get('api_key') or meta.get('deleted')
                       or not meta.get('verified_at'))
            if not blocked and waiter.capability:
                blocked = not self._matches_capability(resource, waiter.capability)
            if not blocked and meta.get('expires_at'):
                try:
                    expiry = datetime.fromisoformat(meta['expires_at'].replace('Z', '+00:00'))
                    if expiry.tzinfo is None:
                        expiry = expiry.replace(tzinfo=timezone.utc)
                    blocked = expiry.timestamp() <= time.time()
                except (ValueError, TypeError):
                    blocked = True
            if not blocked:
                if resource.state == 'cooldown' and now >= resource.cooldown_until:
                    resource.state = 'active'
                blocked = resource.state != 'active' or resource.cooldown_until > now
            if not blocked:
                blocked = (resource.active >= group.meta['concurrency_limit'] or resource.breaker_until > now
                           or (resource.failures >= 3 and resource.half_open))
                if not blocked:
                    selected = resource
                    break
            skipped.append(item)
        for item in skipped:
            heapq.heappush(heap, item)
        return selected

    def _choose_probe(self, waiter, now):
        meta = waiter.probe_meta
        resource = self._resources.get(meta['id'])
        if resource is None or resource.meta.get('deleted') or resource.meta['revision'] != meta['revision']:
            waiter.future.set_exception(PoolError('Resource revision changed during verification', status_code=409))
            return None
        group = self._groups.get(meta['quota_group_id'])
        if group is None:
            return None
        self._trim_events(group, now)
        limits = group.meta
        estimated = waiter.estimated_tokens
        if estimated is None:
            estimated = min(1024, limits['tpm_limit']) if limits['tpm_limit'] else 0
        if (resource.active >= limits['concurrency_limit'] or group.active >= limits['concurrency_limit']
                or group.budget_until > now or (limits['rpm_limit'] and len(group.events) >= limits['rpm_limit'])
                or (limits['tpm_limit'] and group.tokens + estimated > limits['tpm_limit'])):
            return None
        event = [now, estimated]
        group.events.append(event)
        group.tokens += estimated
        group.active += 1
        resource.active += 1
        self._index(resource)
        return ProviderLease(self, resource, group, waiter, event)

    def _dispatch(self):
        if self._closed or self._reloading:
            return
        now = time.monotonic()
        remaining = []
        for waiter in sorted(self._waiting, key=lambda w: (w.priority, w.sequence)):
            if waiter.future.done():
                continue
            if now >= waiter.deadline:
                waiter.future.set_exception(PoolError('Provider request deadline exceeded', status_code=504))
                continue
            lease = self._choose(waiter, now)
            if lease:
                waiter.future.set_result(lease)
                self._counts['accepted'] += 1
            elif not waiter.future.done():
                if self._all_budget_blocked(waiter, now):
                    waiter.future.set_exception(PoolError('Provider request budget exhausted', retry_after=3600))
                else:
                    remaining.append(waiter)
        self._waiting = remaining
        if self._timer:
            self._timer.cancel()
            self._timer = None
        if remaining:
            # A single bounded timer covers RPM windows, cooldowns, breakers and
            # deadlines even when no in-flight request can wake the queue.
            delay = max(.001, min(.1, min(w.deadline for w in remaining) - now))
            self._timer = self._loop.call_later(delay, self._timer_wake)

    def _timer_wake(self):
        with self._lock:
            self._timer = None
            self._dispatch()

    @asynccontextmanager
    async def lease(self, provider, model, capability=None, priority='chat', deadline=None,
                    estimated_tokens=None, exclude_resource_ids=None, _probe=None):
        loop = self._bind()
        end = min(loop.time() + 30, float(deadline) if deadline is not None else loop.time() + 30)
        priorities = {'call': 0, 'chat': 0, 'voice': 0, 'probe': 1, 'admin': 1, 'background': 2}
        future = loop.create_future()
        with self._lock:
            if self._closed:
                raise PoolError('Provider pool is closed', status_code=503)
            if len(self._waiting) >= self.queue_limit and self._waiting:
                self._counts['rejected'] += 1
                raise PoolError('Provider request queue is full')
            self._sequence += 1
            waiter = _Waiter(future, provider, model, capability, priorities.get(priority, 1), self._sequence, end,
                             max(0, int(estimated_tokens)) if estimated_tokens is not None else None,
                             frozenset(exclude_resource_ids or ()), _probe)
            self._waiting.append(waiter)
            self._dispatch()
            if self.queue_limit == 0 and not future.done():
                self._waiting.remove(waiter)
                future.cancel()
                self._counts['rejected'] += 1
                raise PoolError('Provider capacity is temporarily unavailable')
        acquired = None
        reservation_task = None
        try:
            async with asyncio.timeout_at(end):
                acquired = await future
                # Admission reserves counters before I/O. Shield the short DB
                # transaction so cancellation cannot leave an unknown reservation.
                reservation_task = asyncio.create_task(asyncio.to_thread(self.store.reserve_budget,
                    acquired.quota_group_id, acquired._event[1]))
                try:
                    acquired._reservation = await asyncio.shield(reservation_task)
                    with self._lock:
                        meta = acquired._group.meta
                        self._budget_reset(meta)
                        # Exact counters from the transaction avoid double
                        # counting when a reload overlaps this reservation.
                        meta['daily_requests'] = max(meta.get('daily_requests', 0), acquired._reservation['daily_requests'])
                        meta['monthly_requests'] = max(meta.get('monthly_requests', 0), acquired._reservation['monthly_requests'])
                        acquired._group.budget_until = self._budget_reset(meta)
                except ValueError as exc:
                    with self._lock:
                        acquired._group.budget_until = time.monotonic() + max(1, getattr(exc, 'retry_after', 1))
                    raise PoolError('Provider request budget exhausted', retry_after=getattr(exc, 'retry_after', 3600)) from None
            # Admission is bounded; a pinned upstream WS may outlive a turn and
            # be closed from a different task. HTTP client contexts and the
            # existing foreground admission guard retain the complete deadline.
            try:
                yield acquired
            except BaseException as exc:
                if not isinstance(exc, (asyncio.CancelledError, GeneratorExit)) and acquired._outcome is None:
                    response = getattr(exc, 'response', None)
                    status = getattr(exc, 'status_code', 0) or getattr(response, 'status_code', 0)
                    headers = getattr(response, 'headers', {}) or {}
                    acquired.failure(status=status, retry_after=headers.get('retry-after'))
                raise
            else:
                acquired.success()
        except TimeoutError:
            with self._lock:
                self._counts['timeouts'] += 1
            raise PoolError('Provider request deadline exceeded', status_code=504) from None
        finally:
            if acquired is None and future.done() and not future.cancelled() and future.exception() is None:
                acquired = future.result()
            with self._lock:
                self._waiting = [w for w in self._waiting if w is not waiter]
                if not future.done():
                    future.cancel()
            if reservation_task is not None and acquired is not None and acquired._reservation is None:
                # Keep cancellation prompt; the DB transaction still finishes
                # and its attempted-request count remains conservatively charged.
                self._background(self._finish_reservation(reservation_task, acquired))
            if acquired is not None:
                self._release(acquired)
            else:
                with self._lock:
                    self._dispatch()

    @asynccontextmanager
    async def probe(self, resource_id, revision, *, deadline=None, estimated_tokens=None):
        """Pin a draft credential under the same account quotas, without activation."""
        self._bind()
        meta = await run_db_thread(self.store.get, resource_id, True)
        if meta['revision'] != int(revision):
            raise PoolError('Resource revision changed during verification', status_code=409)
        async with self.lease(meta['provider'], meta['model'], priority='probe', deadline=deadline,
                              estimated_tokens=estimated_tokens, _probe=meta) as lease:
            yield lease

    async def _finish_reservation(self, task, lease):
        try:
            lease._reservation = await task
        except (ValueError, asyncio.CancelledError):
            return

    def _failure(self, lease, status, code, retry_after):
        if lease._is_probe:
            return
        with self._lock:
            resource = lease._resource
            if resource.pending_meta is not None:
                resource.errors += 1
                return
            if resource.meta['revision'] != lease.revision:
                return
            try:
                status = int(status or 0)
            except (TypeError, ValueError):
                status = 0
            resource.errors += 1
            resource.half_open = False
            now = time.monotonic()
            if status == 403 and str(code).startswith('voice_denied:'):
                voice = str(code).split(':', 1)[1][:500]
                resource.denied_voices[voice] = True
                resource.denied_voices.move_to_end(voice)
                while len(resource.denied_voices) > 128:
                    resource.denied_voices.popitem(last=False)
            elif status == 401 or status == 403:
                resource.state = 'disabled'
            elif status == 402:
                resource.state = 'exhausted'
            elif status == 429:
                resource.state = 'cooldown'
                resource.cooldown_until = now + max(1, _retry_seconds(retry_after))
            elif status == 0 or status >= 500 or status in (408, 425):
                resource.failures += 1
                if resource.failures >= 3:
                    resource.state = 'cooldown'
                    resource.breaker_until = now + 30
                    resource.cooldown_until = resource.breaker_until
            self._persist(resource)
            self._index(resource)

    def _persist(self, resource):
        until = time.time() + max(0, resource.cooldown_until - time.monotonic())
        stamp = max(time.time_ns(), resource.health_updated_ns + 1)
        resource.health_updated_ns = stamp
        self._background(asyncio.to_thread(self.store.persist_health, resource.meta['id'], resource.meta['revision'],
                                          resource.state, '', until, stamp))

    def _release(self, lease):
        finalize = None
        with self._lock:
            if lease._released:
                return
            if lease._open_sessions:
                # A failed close is visible to its caller, but it cannot make
                # the account's still-open realtime session disappear.
                lease._release_requested = True
                return
            lease._released = True
            resource, group = lease._resource, lease._group
            resource.active -= 1
            group.active -= 1
            same_revision = resource.meta['revision'] == lease.revision
            if lease._outcome == 'success' and same_revision and not lease._is_probe:
                duration = max(.001, time.monotonic() - lease._started)
                resource.ewma = .8 * resource.ewma + .2 * duration
                group.ewma = .8 * group.ewma + .2 * duration
                group.completed += 1
                resource.completed += 1
                resource.failures = 0
                resource.half_open = False
                resource.breaker_until = 0
                if resource.state == 'active':
                    self._persist(resource)
            elif same_revision:
                resource.half_open = False
            if lease._actual_tokens is not None:
                difference = lease._actual_tokens - lease._event[1]
                if lease._event in group.events:
                    group.tokens += difference
                    lease._event[1] = lease._actual_tokens
            if lease._reservation and lease._actual_tokens is not None:
                self._background(asyncio.to_thread(self.store.reconcile_budget, lease._reservation, lease._actual_tokens))
            self._counts['completed'] += 1
            if not resource.active and resource.pending_meta is not None:
                self._apply_pending(resource)
            else:
                self._index(resource)
            if not resource.active and resource.meta.get('deleted'):
                finalize = resource.meta['id']
                self._resources.pop(finalize, None)
            self._dispatch()
        if finalize:
            self._background(asyncio.to_thread(self.store.finalize_delete, finalize))

    async def _make_client(self, entry, factory, lease):
        async with asyncio.timeout_at(lease.deadline):
            value = factory(lease)
            if inspect.isawaitable(value):
                value = await value
        entry.value = value
        if entry.draining and not entry.refs:
            await self._close_client(entry)
        return value

    async def _close_client(self, entry):
        if entry.closed:
            return
        if entry.task and not entry.task.done() and entry.task is not asyncio.current_task():
            entry.draining = True
            return
        if entry.close_task is None or (entry.close_task.done() and
                (entry.close_task.cancelled() or entry.close_task.exception() is not None)):
            async def close_once():
                value = entry.value
                close = (getattr(value, 'aclose', None) or getattr(value, 'close', None)) if value is not None else None
                if close:
                    result = close()
                    if inspect.isawaitable(result):
                        await result
                # Only a completed close is idempotently closed. Failures stay
                # registered and can be retried by a later aclose().
                entry.closed = True
                self._closing_clients.pop(id(entry), None)
            self._closing_clients[id(entry)] = entry
            entry.close_task = self._background(close_once())
        await self._drain_cleanup(entry.close_task)

    @staticmethod
    async def _drain_cleanup(task):
        """Finish owned cleanup even under repeated cancellation, then re-raise."""
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancelled = True
                if task.done():
                    result = task.result()
                    break
        if cancelled:
            raise asyncio.CancelledError
        return result

    @asynccontextmanager
    async def client(self, lease, factory, kind='http'):
        self._bind()
        if kind not in self._transport:
            raise ValueError('Client kind must be http or ws')
        with self._lock:
            current = self._resources.get(lease.resource_id)
            draining = current is None or current.pending_meta is not None or current.meta['revision'] != lease.revision
        if kind == 'ws' or lease._is_probe or draining:
            async with self._session_client(lease, factory, kind=kind) as value:
                yield value
            return
        key = (lease.resource_id, lease.revision, 'http')
        entry = None
        transport_acquired = False
        try:
            async with asyncio.timeout_at(lease.deadline):
                await self._transport[kind].acquire()
                transport_acquired = True
                with self._lock:
                    self._transport_active[kind] += 1
                while entry is None:
                    evict = None
                    with self._lock:
                        if self._closed:
                            raise PoolError('Provider pool is closed', status_code=503)
                        entry = self._clients.get(key)
                        if entry and entry.draining:
                            entry = None
                        if entry is None and len(self._clients) >= self.client_limit:
                            for old_key, old in self._clients.items():
                                if not old.refs and (old.task is None or old.task.done()):
                                    evict = old
                                    self._clients.pop(old_key)
                                    break
                        if entry is None and len(self._clients) < self.client_limit:
                            entry = _Client(key)
                            self._clients[key] = entry
                            entry.task = self._loop.create_task(self._make_client(entry, factory, lease))
                            # A canceled caller must not leave an unobserved task error.
                            entry.task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
                        if entry:
                            entry.refs += 1
                            self._clients.move_to_end(key)
                        else:
                            self._client_changed.clear()
                    if evict:
                        await self._close_client(evict)
                    if entry is None:
                        await self._client_changed.wait()
                value = await asyncio.shield(entry.task)
            async with asyncio.timeout_at(lease.deadline):
                yield value
        except TimeoutError:
            raise PoolError('Provider client deadline exceeded', status_code=504) from None
        finally:
            close = None
            with self._lock:
                if entry:
                    entry.refs -= 1
                    if entry.refs == 0 and (entry.draining or entry.task.cancelled() or (entry.task.done() and entry.task.exception() is not None)):
                        if self._clients.get(key) is entry:
                            self._clients.pop(key, None)
                        close = entry
                    self._client_changed.set()
                if transport_acquired:
                    self._transport_active[kind] -= 1
                    self._transport[kind].release()
            if close:
                await self._close_client(close)

    @asynccontextmanager
    async def _session_client(self, lease, factory, *, kind):
        """Sockets and probe clients cannot poison the business-client LRU."""
        entry = None
        acquired = False
        try:
            async with asyncio.timeout_at(lease.deadline):
                await self._transport[kind].acquire()
                acquired = True
                with self._lock:
                    if self._closed:
                        raise PoolError('Provider pool is closed', status_code=503)
                    self._client_sequence += 1
                    key = (lease.resource_id, lease.revision, kind, self._client_sequence)
                    entry = _Client(key, refs=1, draining=True, lease=lease)
                    self._sessions[key] = entry
                    lease._open_sessions += 1
                    self._transport_active[kind] += 1
                    entry.task = self._loop.create_task(self._make_client(entry, factory, lease))
                # Disposable factories are never shielded as orphaned work.
                value = await entry.task
            if kind == 'ws':
                yield value
            else:
                async with asyncio.timeout_at(lease.deadline):
                    yield value
        except TimeoutError:
            raise PoolError('Provider connection deadline exceeded', status_code=504) from None
        finally:
            if entry:
                entry.refs = 0
                cleanup = self._background(self._finish_session(entry, kind))
                await self._drain_cleanup(cleanup)
            elif acquired:
                self._transport[kind].release()

    async def _finish_session(self, entry, kind):
        try:
            if entry.task and not entry.task.done():
                entry.task.cancel()
                await asyncio.gather(entry.task, return_exceptions=True)
            await self._close_client(entry)
        finally:
            # Even when the caller was cancelled, close has been drained before
            # any socket, semaphore or account counter can be released.
            with self._lock:
                if entry.closed and not entry.slot_released:
                    entry.slot_released = True
                    self._sessions.pop(entry.key, None)
                    self._transport_active[kind] -= 1
                    self._transport[kind].release()
                    entry.lease._open_sessions -= 1
                    if entry.lease._release_requested and not entry.lease._open_sessions:
                        self._release(entry.lease)

    def available(self, provider, model, capability=None):
        """Report healthy configured eligibility, independently of current load."""
        with self._lock:
            if not self.applies(provider, model):
                return False
            now = time.monotonic()
            for resource in self._route_resources((provider, model)):
                if ((resource.state == 'active' or (resource.state == 'cooldown' and resource.cooldown_until <= now))
                        and resource.meta.get('enabled') and self._matches_capability(resource, capability)):
                    return True
            return False

    @property
    def closed(self):
        return self._closed

    def observe_probe(self, resource_id, revision, latency_ms):
        try:
            seconds = float(latency_ms) / 1000
        except (TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(seconds) or seconds <= 0:
            return False
        with self._lock:
            resource = self._resources.get(resource_id)
            if resource is None or resource.meta['revision'] != int(revision) or resource.meta.get('deleted'):
                return False
            resource.ewma = max(.001, seconds)
            group = self._groups.get(resource.meta['quota_group_id'])
            if group:
                group.ewma = resource.ewma if not group.completed else .8 * group.ewma + .2 * resource.ewma
            self._index(resource)
            self._dispatch()
            return True

    def metric_snapshots(self):
        with self._lock:
            stamp = time.time_ns()
            return [dict(resource_id=rid, at=stamp, revision=resource.meta['revision'], ewma=resource.ewma,
                         completed=resource.completed, errors=resource.errors)
                    for rid, resource in self._resources.items() if not resource.meta.get('deleted')]

    async def flush_metrics(self):
        from .metrics import flush_metrics
        return await flush_metrics(self)

    def resource_stats(self, rid):
        with self._lock:
            resource = self._resources.get(rid)
            if resource is None:
                return {}
            result = dict(id=rid, resource_id=rid, provider=resource.meta['provider'], model=resource.meta['model'],
                        quota_group_id=resource.meta['quota_group_id'], revision=resource.meta['revision'], state=resource.state,
                        active=resource.active, ewma_seconds=round(resource.ewma, 4), completed=resource.completed,
                        failures=resource.errors, consecutive_failures=resource.failures,
                        cooldown_seconds=max(0, math.ceil(resource.cooldown_until - time.monotonic())))
            if resource.pending_meta is not None:
                result.update(pending_quota_group_id=resource.pending_meta['quota_group_id'],
                              pending_revision=resource.pending_meta['revision'])
            return result

    def stats(self, provider=None, *, include_resources=True):
        with self._lock:
            resources = []
            names = ('fish', 'deepgram', 'llm', 'siliconflow')
            providers = {name: dict(enabled=self._modes.get(name, False), active=0, resources=0, waiting=0)
                         for name in names if provider is None or name == provider}
            for rid, resource in self._resources.items():
                name = resource.meta['provider']
                if name not in providers:
                    continue
                providers[name]['active'] += resource.active
                providers[name]['resources'] += 1
                if include_resources:
                    resources.append(self.resource_stats(rid))
            for waiter in self._waiting:
                if waiter.provider in providers:
                    providers[waiter.provider]['waiting'] += 1
            return dict(providers=providers, resources=resources, active=sum(p['active'] for p in providers.values()),
                        waiting=sum(provider is None or w.provider == provider for w in self._waiting),
                        queue_limit=self.queue_limit, clients=len(self._clients), client_limit=self.client_limit,
                        http_active=self._transport_active['http'], ws_active=self._transport_active['ws'], **self._counts)

    async def aclose(self):
        self._bind()
        if self._close_pool_task is None or (self._close_pool_task.done() and
                (self._close_pool_task.cancelled() or self._close_pool_task.exception() is not None)):
            self._close_pool_task = self._loop.create_task(self._close_pool())
        await self._drain_cleanup(self._close_pool_task)

    async def _close_pool(self):
        close = []
        with self._lock:
            self._closed = True
            if self._timer:
                self._timer.cancel()
                self._timer = None
            for waiter in self._waiting:
                if not waiter.future.done():
                    waiter.future.set_exception(PoolError('Provider pool is closing', status_code=503))
            self._waiting.clear()
            for key, entry in list(self._clients.items()):
                entry.draining = True
                if not entry.refs:
                    self._clients.pop(key, None)
                    close.append(entry)
            sessions = list(self._sessions.values())
            session_ids = {id(entry) for entry in sessions}
            close.extend(entry for entry in self._closing_clients.values() if id(entry) not in session_ids)
            close = list({id(entry): entry for entry in close}.values())
            self._client_changed.set()
        operations = []
        for entry in close:
            if entry.task and not entry.task.done():
                await asyncio.gather(entry.task, return_exceptions=True)
            operations.append(self._close_client(entry))
        operations.extend(self._finish_session(entry, entry.key[2]) for entry in sessions)
        results = await asyncio.gather(*operations, return_exceptions=True)
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        await self.flush_metrics()
        for result in results:
            if isinstance(result, BaseException):
                raise result
