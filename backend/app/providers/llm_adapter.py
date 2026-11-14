"""Same-model LLM pool routing; an emitted stream is never replayed."""
from __future__ import annotations

import asyncio
import time
import json
from contextlib import asynccontextmanager

_ROUTING_FIELDS = {'model', 'messages', 'stream', 'api_key', 'authorization',
                   'base_url', 'max_tokens', 'max_completion_tokens'}


def safe_extra(value):
    return {key: item for key, item in (value or {}).items() if key.lower() not in _ROUTING_FIELDS}


def estimated_tokens(parent, messages, maximum, extra=None):
    from app.llm.client import _normalize_messages
    content = {'messages': _normalize_messages(messages), 'extra': safe_extra(extra or parent.cfg.extra_body)}
    # Model-independent conservative estimate. Actual JSON response usage is
    # reconciled; unknown streaming usage retains the estimate, never fake zero.
    return len(json.dumps(content, ensure_ascii=False).encode('utf-8')) + max(0, int(maximum or 0)) + 64


def failure_status(exc):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        status = getattr(exc, 'status_code', 0) or getattr(exc, 'status', 0)
        if status:
            return int(status)
        exc = exc.__cause__
    return 0


def _settings(parent, lease):
    return parent.cfg.model_copy(deep=True, update={
        'api_key': lease.api_key, 'base_url': lease.base_url,
        'model': lease.model, 'suggestion_model': lease.model, 'max_retries': 0,
        'extra_headers': {key: value for key, value in parent.cfg.extra_headers.items()
                          if key.lower() not in ('authorization', 'api-key', 'x-api-key')},
        'extra_body': safe_extra(parent.cfg.extra_body),
    })


def _factory(parent):
    def create(lease):
        from app.llm.client import LLMClient
        settings = _settings(parent, lease)
        # This object owns only the credential/version transport. Request
        # settings and headers belong to an immutable per-call view below.
        settings.extra_headers = {}
        child = LLMClient(settings)
        child._pool_managed = True
        child.usage_budget = getattr(parent, 'usage_budget', None)
        return child
    return create


def _request_view(parent, lease, cached):
    from app.llm.client import LLMClient
    view = LLMClient(_settings(parent, lease))
    view._pool_managed = True
    view._pool_response_format_supported = getattr(cached, '_pool_response_format_supported', True)
    view._client = cached._ensure_client()
    view.usage_budget = getattr(parent, 'usage_budget', None)
    return view


@asynccontextmanager
async def _lease(pool, *args, **kwargs):
    async with pool.lease(*args, **kwargs) as lease:
        try:
            yield lease
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            lease.failure(status=failure_status(exc))
            raise


async def stream_chat(parent, pool, messages, **kwargs):
    from app.providers.pool import PoolError
    attempted = []
    deadline = time.monotonic() + min(30.0, parent.cfg.timeout_s)
    emitted = False
    kwargs['extra'] = safe_extra(kwargs.get('extra'))
    estimate = estimated_tokens(parent, messages, kwargs.get('max_tokens') or parent.cfg.max_tokens, kwargs['extra'])
    for attempt in range(2):
        lease = None
        try:
            async with _lease(pool, 'llm', parent.cfg.model, deadline=deadline,
                                  exclude_resource_ids=attempted, estimated_tokens=estimate) as lease:
                attempted.append(lease.resource_id)
                async with pool.client(lease, _factory(parent)) as cached:
                    child = _request_view(parent, lease, cached)
                    iterator = child.stream_chat(messages, **kwargs)
                    try:
                        async for delta in iterator:
                            emitted = True
                            lease.mark_output_started()
                            yield delta
                    finally:
                        await iterator.aclose()
                    lease.success()
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status = failure_status(exc)
            if lease is not None:
                lease.failure(status=status)
            if lease is not None and not emitted and status in (429, 503) and attempt == 0:
                continue
            if isinstance(exc, PoolError):
                raise
            raise PoolError('LLM request could not be completed',
                            status_code=429 if status == 429 else 502) from None


async def complete_json(parent, pool, messages, **kwargs):
    from app.providers.pool import PoolError
    model = kwargs.get('model') or parent.cfg.suggestion_model or parent.cfg.model
    attempted = []
    deadline = time.monotonic() + min(30.0, kwargs.get('timeout_s') or parent.cfg.timeout_s)
    estimate = estimated_tokens(parent, messages, kwargs.get('max_tokens') or parent.cfg.suggestion_max_tokens)
    for attempt in range(2):
        lease = child = None
        try:
            async with _lease(pool, 'llm', model, deadline=deadline, exclude_resource_ids=attempted, estimated_tokens=estimate) as lease:
                async with pool.client(lease, _factory(parent)) as cached:
                    child = _request_view(parent, lease, cached)
                    try:
                        result = await child.complete_json(messages, **dict(kwargs, model=model))
                    except Exception:
                        if not getattr(child, '_pool_response_format_supported', True):
                            cached._pool_response_format_supported = False
                        raise
                    lease.success(getattr(child, '_last_pool_usage', None))
                    return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status = failure_status(exc)
            if lease is not None:
                lease.failure(status=status)
            if status == 400 and child is not None and not getattr(child, '_pool_response_format_supported', True) and attempt == 0:
                # Retry the same credential without unsupported JSON mode, but
                # through a new lease so every actual request consumes budget.
                continue
            if lease is not None:
                attempted.append(lease.resource_id)
            if lease is not None and status in (429, 503) and attempt == 0:
                continue
            if isinstance(exc, PoolError):
                raise
            raise PoolError('LLM structured response could not be completed',
                            status_code=429 if status == 429 else 502) from None
