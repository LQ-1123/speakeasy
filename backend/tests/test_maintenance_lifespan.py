from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI

from app.v2.db import DB


def test_lifespan_starts_without_provider_work_and_closes_workers_before_db(tmp_path, monkeypatch):
    from app import main
    from app.v2 import services
    from app.v3 import client_events, world
    from app.config import get_settings

    settings = get_settings().model_copy(deep=True)
    settings.data_dir = str(tmp_path)
    settings.tts.prewarm = False
    settings.stt.backend = "mock"
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "gpu_info", lambda: {})
    monkeypatch.setenv("SE_VOICE_STRICT", "0")
    lifecycle = []
    db = DB(str(tmp_path / "lifecycle.db"))
    db.init()

    async def close():
        lifecycle.append("db_close")
        db.close()

    async def engine_close():
        lifecycle.append("engine_close")

    engines = SimpleNamespace(llm=SimpleNamespace(uses_mock=True),
                              ref_audio=SimpleNamespace(validate=lambda: []), aclose=engine_close)
    monkeypatch.setattr(main, "build_engines", lambda _: engines)
    fake_services = SimpleNamespace(db=db, voice=SimpleNamespace(), aclose=close)
    monkeypatch.setattr(services, "build_services", lambda *_: fake_services)

    async def no_world(*_args, **_kwargs):
        raise AssertionError("provider/world work must not block startup")

    async def events(_db, _service=None):
        try:
            await asyncio.Event().wait()
        finally:
            lifecycle.append("event_stop")

    monkeypatch.setattr(world, "ensure_world", no_world)
    monkeypatch.setattr(client_events, "event_worker", events)

    async def run():
        app = FastAPI()
        async with main.lifespan(app):
            assert app.state.v3 is not None
            assert app.state.background_jobs is not None
            assert not app.state.maintenance_task.done()
            await asyncio.sleep(0)
        assert app.state.maintenance_task.done()

    asyncio.run(run())
    assert lifecycle.index("event_stop") < lifecycle.index("db_close")
