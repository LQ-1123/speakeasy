from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_deleted_legacy_migration_is_not_recreated_on_restart(tmp_path):
    from app.config import LLMSettings
    from app.v2.db import DB
    from app.providers.runtime import initialize_pool, unbind_pool
    db = DB(str(tmp_path/'runtime.db'))
    db.init()
    engines = SimpleNamespace(llm=SimpleNamespace(cfg=LLMSettings(api_key='legacy-only',model='same')))
    services = SimpleNamespace(db=db)
    first = await initialize_pool(services,engines,tmp_path)
    resource = services.provider_store.list_resources()['items'][0]
    services.provider_store.delete(resource['id'])
    services.provider_store.finalize_delete(resource['id'])
    await first.aclose()
    unbind_pool(first)
    second = await initialize_pool(services,engines,tmp_path)
    assert services.provider_store.list_resources()['total'] == 0
    assert not services.provider_store.provider_enabled('llm')
    await second.aclose()
    unbind_pool(second)
    db.close()
