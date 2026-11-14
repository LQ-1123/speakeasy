"""Application-owned provider pools, scoped to an event loop for clean tests."""
from __future__ import annotations

import asyncio
import weakref

_pools: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def bind_pool(pool):
    loop = asyncio.get_running_loop()
    previous = _pools.get(loop)
    _pools[loop] = pool
    return previous


def get_pool():
    try:
        return _pools.get(asyncio.get_running_loop())
    except RuntimeError:
        return None


def unbind_pool(pool=None):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if pool is None or _pools.get(loop) is pool:
        _pools.pop(loop, None)


async def initialize_pool(services, engines, data_dir):
    """Import legacy keys as pending without probing or changing their mode."""
    from app.jobs import run_db_thread
    from app.providers.store import ProviderStore
    from app.providers.pool import ProviderPool
    from pathlib import Path
    import json

    def setup():
        store = ProviderStore(services.db, data_dir)
        with store._lock, store.conn:
            store.conn.execute('CREATE TABLE IF NOT EXISTS provider_legacy_imports '
                               '(provider TEXT PRIMARY KEY, imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
        def import_once(provider, payload):
            with store._lock:
                if store.conn.execute('SELECT 1 FROM provider_legacy_imports WHERE provider=?', (provider,)).fetchone():
                    return
                store.import_legacy(provider, payload)
                with store.conn:
                    store.conn.execute('INSERT OR IGNORE INTO provider_legacy_imports(provider) VALUES (?)', (provider,))
        settings = getattr(engines, 'settings', None)
        llm = getattr(engines, 'llm', None)
        llm_cfg = getattr(llm, 'cfg', None) or getattr(settings, 'llm', None)
        if llm_cfg is not None and getattr(llm_cfg, 'api_key', ''):
            import_once('llm', {'name': '原有 LLM 配置', 'api_key': llm_cfg.api_key,
                'base_url': llm_cfg.base_url, 'model': llm_cfg.model, 'concurrency_limit': 10})
        for filename, provider, fields in (
            ('tts_config.json', 'fish', ('fish_api_key', 'fish_base_url', 'fish_model')),
            ('stt_config.json', 'siliconflow', ('api_key', 'base_url', 'model')),
            ('deepgram_config.json', 'deepgram', ('api_key', '', '')),
        ):
            path = Path(data_dir) / filename
            if not path.exists():
                continue
            try:
                config = json.loads(path.read_text(encoding='utf-8'))
            except (ValueError, OSError):
                continue
            key = config.get(fields[0]) or ''
            if not key:
                continue
            from app.providers.store import DEFAULT_URLS
            import_once(provider, {'name': f'原有 {provider} 配置', 'api_key': key,
                'base_url': config.get(fields[1]) or DEFAULT_URLS[provider],
                'model': config.get(fields[2]) or ('nova-3' if provider == 'deepgram' else 's2.1-pro' if provider == 'fish' else 'FunAudioLLM/SenseVoiceSmall'),
                'concurrency_limit': 5 if provider == 'fish' else 4 if provider == 'siliconflow' else 1})
        return store

    store = await run_db_thread(setup)
    from app.capacity import env_int
    pool = ProviderPool(store,
        http_limit=env_int('SE_PROVIDER_HTTP_LIMIT', 32, 1, 256),
        ws_limit=env_int('SE_PROVIDER_WS_LIMIT', 16, 1, 100),
        queue_limit=env_int('SE_PROVIDER_QUEUE_LIMIT', 64, 0, 1000),
        client_limit=env_int('SE_PROVIDER_CLIENT_LIMIT', 16, 1, 128))
    await pool.reload()
    services.provider_store, services.provider_pool = store, pool
    bind_pool(pool)
    llm = getattr(engines, 'llm', None)
    if getattr(services, 'chat', None) is not None:
        services.chat.provider_pool = pool
        # Capacity is independent from the number of saved credentials.
        from app.capacity import env_int
        services.chat.admission.limit = env_int('SE_CHAT_CONCURRENCY', 32, 1, 100)
        services.chat.admission.queue_size = env_int('SE_CHAT_QUEUE_SIZE', 64, 0, 200)
    if llm is not None:
        llm.provider_pool = pool
    return pool
