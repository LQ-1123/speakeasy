"""Offline adapter contracts; every upstream and pool account is synthetic."""
from __future__ import annotations

import asyncio
import json
import inspect
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from app.voice import deepgram_stream, fish_tts, sensevoice, voice_registry


class FakeLease:
    def __init__(self, key="account-a", *, model="s2.1-pro", base_url="https://pooled.invalid"):
        self.resource_id = key
        self.quota_group_id = key
        self.revision = 1
        self.api_key = key
        self.base_url = base_url
        self.model = model
        self.options = {}
        self.failures = []
        self.outputs = 0
        self.successes = 0
        self.releases = 0

    def failure(self, status=0, code="", retry_after=None):
        self.failures.append((status, code, retry_after))

    def mark_output_started(self):
        self.outputs += 1

    def success(self, usage=None):
        self.successes += 1


class FakePool:
    def __init__(self, handler, *, leases=None, applies=True, error=None):
        self.handler = handler
        self.leases = leases or [FakeLease()]
        self.enabled = applies
        self.error = error
        self.routes = []
        self.acquired = []
        self.clients = {}
        self.client_calls = []

    def applies(self, provider, model):
        self.routes.append((provider, model))
        return self.enabled

    @asynccontextmanager
    async def lease(self, provider, model, **kwargs):
        if self.error:
            raise self.error
        lease = self.leases[min(len(self.acquired), len(self.leases) - 1)]
        self.acquired.append((lease, provider, model, kwargs))
        try:
            yield lease
        finally:
            lease.releases += 1

    @asynccontextmanager
    async def client(self, lease, factory, kind="http"):
        self.client_calls.append(kind)
        if kind == "ws":
            client = factory(lease)
            if inspect.isawaitable(client):
                client = await client
            try:
                yield client
            finally:
                await client.close()
            return
        key = (lease.resource_id, lease.revision, kind)
        if key not in self.clients:
            self.clients[key] = factory(lease)
        yield self.clients[key]

    def available(self, provider, model, capability=None):
        return self.enabled and self.error is None

    async def aclose(self):
        for client in self.clients.values():
            await client.aclose()


@pytest.fixture
def install_pool(monkeypatch):
    pools = []
    original_client = httpx.AsyncClient

    def install(pool):
        pools.append(pool)
        for module in (fish_tts, sensevoice, deepgram_stream, voice_registry):
            monkeypatch.setattr(module, "get_pool", lambda: pool, raising=False)
        # Only actual network I/O is replaced. The adapter still owns requests,
        # request headers, parsing, client construction, and resource outcomes.
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(
            **{**kwargs, "transport": httpx.MockTransport(pool.handler)}))
        return pool

    yield install
    # Async clients are closed explicitly by each test using a pooled client.


async def test_fish_pool_uses_lease_identity_and_capability_without_legacy_gate(install_pool, monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=b"p" * 1024)

    pool = install_pool(FakePool(handler))
    monkeypatch.setattr(fish_tts, "_state", lambda: pytest.fail("legacy five-slot gate used"))
    with fish_tts.priority("call"):
        result = await fish_tts.synthesize("hello", api_key="", model="s2.1-pro",
                                           reference_id="voice-owned", audio_format="mp3")
    assert result == b"p" * 1024
    assert str(seen[0].url) == "https://pooled.invalid/v1/tts"
    assert seen[0].headers["authorization"] == "Bearer account-a"
    assert seen[0].headers["model"] == "s2.1-pro"
    assert json.loads(seen[0].content)["reference_id"] == "voice-owned"
    assert pool.acquired[0][3]["capability"] == {"reference_id": "voice-owned"}
    assert pool.acquired[0][3]["priority"] == "call"
    assert pool.leases[0].outputs == pool.leases[0].successes == pool.leases[0].releases == 1
    await pool.aclose()


@pytest.mark.parametrize("status", [429, 503])
async def test_fish_retries_only_explicit_rejection_at_most_two_accounts(install_pool, status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"Retry-After": "2"}, text="busy")

    leases = [FakeLease("account-a"), FakeLease("account-b"), FakeLease("account-c")]
    pool = install_pool(FakePool(handler, leases=leases))
    with pytest.raises(fish_tts.FishTTSError) as error:
        await fish_tts.synthesize("hello", api_key="legacy")
    assert error.value.status == status
    assert len(requests) == len(pool.acquired) == 2
    assert [lease.failures[0][0] for lease in leases[:2]] == [status, status]
    assert all(lease.outputs == 0 for lease in leases)
    assert [lease.releases for lease in leases] == [1, 1, 0]
    await pool.aclose()


@pytest.mark.parametrize("failure", [401, 402, 403, "timeout", "partial"])
async def test_fish_never_switches_account_on_auth_timeout_or_short_output(install_pool, failure):
    def handler(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("ambiguous", request=request)
        if failure == "partial":
            return httpx.Response(200, content=b"partial")
        return httpx.Response(failure, text="rejected")

    pool = install_pool(FakePool(handler))
    with pytest.raises(fish_tts.FishTTSError):
        await fish_tts.synthesize("hello", api_key="legacy")
    assert len(pool.acquired) == 1
    assert pool.leases[0].releases == 1
    if isinstance(failure, int):
        assert pool.leases[0].failures[0][0] == failure
    else:
        assert pool.leases[0].failures
    assert pool.leases[0].outputs == (1 if failure == "partial" else 0)
    await pool.aclose()


async def test_activated_unavailable_fish_does_not_fall_back_to_legacy_key(install_pool):
    error = RuntimeError("no eligible pooled resource")
    error.status_code = 503
    pool = install_pool(FakePool(lambda request: pytest.fail("legacy network called"), error=error))
    with pytest.raises(fish_tts.FishTTSError) as result:
        await fish_tts.synthesize("hello", api_key="legacy")
    assert result.value.status == 503
    assert not pool.clients


async def test_fish_bitrate_rejection_is_scoped_to_resource_revision(install_pool, monkeypatch):
    seen = []

    def handler(request):
        key = request.headers["authorization"]
        body = json.loads(request.content)
        seen.append((key, body.get("mp3_bitrate")))
        if key == "Bearer account-a" and body.get("mp3_bitrate"):
            return httpx.Response(422, text="mp3_bitrate unsupported")
        return httpx.Response(200, content=b"a" * 1024)

    a, b = FakeLease("account-a"), FakeLease("account-b")
    pool = install_pool(FakePool(handler, leases=[a, a, a, b]))
    monkeypatch.setattr(fish_tts, "_bitrate_rejected", True)
    monkeypatch.setenv("SE_FISH_MP3_BITRATE", "64")
    for _ in range(3):
        await fish_tts.synthesize("hello", api_key="", audio_format="mp3")
    assert seen == [("Bearer account-a", 64), ("Bearer account-a", None),
                    ("Bearer account-a", None), ("Bearer account-b", 64)]
    await pool.aclose()


async def test_fish_catalog_uses_pooled_account_without_legacy_key(install_pool):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"total": 1, "items": [{"_id": "public-voice", "title": "Voice"}]})

    pool = install_pool(FakePool(handler))
    rows = await fish_tts.list_models(api_key="", page_size=1)
    assert rows[0]["id"] == "public-voice"
    assert seen[0].url.path == "/model"
    assert seen[0].headers["authorization"] == "Bearer account-a"
    assert len(pool.acquired) == 1
    await pool.aclose()


async def test_fish_voice_permission_rejection_can_try_one_same_voice_candidate(install_pool):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content)["reference_id"])
        if request.headers["authorization"] == "Bearer denied":
            return httpx.Response(403, text="reference_id voice permission denied")
        return httpx.Response(200, content=b"v" * 1024)

    denied, allowed = FakeLease("denied"), FakeLease("allowed")
    pool = install_pool(FakePool(handler, leases=[denied, allowed]))
    result = await fish_tts.synthesize("hello", api_key="", reference_id="exact-voice")
    assert result == b"v" * 1024
    assert seen == ["exact-voice", "exact-voice"]
    assert denied.failures[0][1] == "voice_denied:exact-voice"
    assert allowed.successes == 1
    await pool.aclose()


async def test_sensevoice_routes_exact_model_and_request_auth_without_legacy_gate(install_pool, monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"text": "pooled transcript"})

    lease = FakeLease("stt-account", model=sensevoice.DEFAULT_MODEL,
                      base_url="https://stt.invalid/v1/")
    pool = install_pool(FakePool(handler, leases=[lease]))
    client = sensevoice.SiliconFlowSTT(base_url="", api_key="", model=sensevoice.DEFAULT_MODEL)
    monkeypatch.setattr(client, "_request_pool", lambda: pytest.fail("legacy STT cap used"))
    assert client.configured
    transcript = await client.transcribe(b"\0\0" * 1600)
    assert transcript.text == "pooled transcript"
    assert str(seen[0].url) == "https://stt.invalid/v1/audio/transcriptions"
    assert seen[0].headers["authorization"] == "Bearer stt-account"
    assert b"FunAudioLLM/SenseVoiceSmall" in seen[0].content
    assert ("siliconflow", sensevoice.DEFAULT_MODEL) in pool.routes
    assert lease.successes == lease.releases == 1
    await pool.aclose()


@pytest.mark.parametrize("status", [401, 402, 429, 503])
async def test_sensevoice_marks_status_before_wrapping_and_bounds_retries(install_pool, status):
    pool = install_pool(FakePool(lambda request: httpx.Response(status, text="rejected"),
                                leases=[FakeLease("stt-a", model="exact"), FakeLease("stt-b", model="exact")]))
    client = sensevoice.SiliconFlowSTT(base_url="https://legacy.invalid", api_key="legacy", model="exact")
    with pytest.raises(sensevoice.SenseVoiceError) as result:
        await client.transcribe(b"\0\0" * 1600)
    assert result.value.status_code == status
    assert len(pool.acquired) == (2 if status in (429, 503) else 1)
    assert pool.leases[0].failures[0][0] == status
    await pool.aclose()


async def test_sensevoice_pooled_injected_transport_and_ambiguous_failure_no_retry(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("ambiguous", request=request)

    pool = FakePool(None, leases=[FakeLease("stt", model="exact")])
    monkeypatch.setattr(sensevoice, "get_pool", lambda: pool)
    client = sensevoice.SiliconFlowSTT(base_url="", api_key="", model="exact",
                                     transport=httpx.MockTransport(handler))
    with pytest.raises(sensevoice.SenseVoiceError):
        await client.transcribe(b"\0\0" * 1600)
    assert len(requests) == len(pool.acquired) == 1
    assert pool.leases[0].failures[0][1] == "ReadTimeout"
    assert pool.leases[0].releases == 1
    await pool.aclose()


async def test_deepgram_pins_account_until_idempotent_close(install_pool, monkeypatch):
    lease = FakeLease("deepgram-a", model="nova-3", base_url="https://deepgram.invalid/v1/listen")
    pool = install_pool(FakePool(lambda request: pytest.fail("HTTP used"), leases=[lease]))
    sent = []
    ws = SimpleNamespace(send=lambda data: None)

    async def send(data):
        sent.append(data)

    async def close():
        sent.append("closed")

    ws.send, ws.close = send, close
    seen = {}

    async def connect(url, **kwargs):
        seen.update(url=url, **kwargs)
        return ws

    monkeypatch.setattr(deepgram_stream.websockets, "connect", connect)
    streamer = deepgram_stream.DeepgramStreamer("")
    await streamer.connect()
    assert lease.releases == 0
    await streamer.send(b"pcm")
    await streamer.close()
    await streamer.close()
    assert seen["url"].startswith("wss://deepgram.invalid/v1/listen?model=nova-3")
    assert seen["additional_headers"] == {"Authorization": "Token deepgram-a"}
    assert lease.releases == lease.successes == 1
    assert pool.client_calls == ["ws"]
    assert b"pcm" in sent


@pytest.mark.parametrize("cancel", [False, True])
async def test_deepgram_connect_failure_or_cancel_releases_once(install_pool, monkeypatch, cancel):
    lease = FakeLease(model="nova-3")
    pool = install_pool(FakePool(lambda request: pytest.fail("HTTP used"), leases=[lease]))

    async def connect(*args, **kwargs):
        if cancel:
            raise asyncio.CancelledError()
        error = RuntimeError("handshake rejected")
        error.status_code = 401
        raise error

    monkeypatch.setattr(deepgram_stream.websockets, "connect", connect)
    streamer = deepgram_stream.DeepgramStreamer("legacy")
    with pytest.raises(asyncio.CancelledError if cancel else deepgram_stream.StreamUnavailable):
        await streamer.connect()
    await streamer.close()
    assert len(pool.acquired) == lease.releases == 1
    if not cancel:
        assert lease.failures[0][0] == 401


async def test_deepgram_partial_then_socket_failure_stays_on_account_and_marks_failure(install_pool, monkeypatch):
    lease = FakeLease(model="nova-3")
    pool = install_pool(FakePool(None, leases=[lease]))

    class Socket:
        async def send(self, data):
            pass

        async def close(self):
            pass

        async def __aiter__(self):
            yield json.dumps({"type": "Results", "channel": {"alternatives": [{"transcript": "hello"}]}})
            raise RuntimeError("socket lost after partial result")

    async def connect(*args, **kwargs):
        return Socket()

    monkeypatch.setattr(deepgram_stream.websockets, "connect", connect)
    streamer = deepgram_stream.DeepgramStreamer("")
    await streamer.connect()
    stream = streamer.events()
    assert (await anext(stream))["text"] == "hello"
    with pytest.raises(deepgram_stream.StreamUnavailable, match="Deepgram transcription unavailable"):
        await anext(stream)
    await streamer.close()
    assert len(pool.acquired) == 1
    assert lease.outputs == lease.releases == 1
    assert lease.failures and lease.successes == 0


async def test_deepgram_cancelled_close_releases_socket_and_account_once(install_pool, monkeypatch):
    lease = FakeLease(model="nova-3")
    pool = install_pool(FakePool(None, leases=[lease]))
    closing = asyncio.Event()
    closed = []

    class Socket:
        async def send(self, data):
            closing.set()
            await asyncio.Event().wait()

        async def close(self):
            closed.append(True)

    async def connect(*args, **kwargs):
        return Socket()

    monkeypatch.setattr(deepgram_stream.websockets, "connect", connect)
    streamer = deepgram_stream.DeepgramStreamer("")
    await streamer.connect()
    task = asyncio.create_task(streamer.close())
    await closing.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await streamer.close()
    assert closed == [True]
    assert lease.releases == 1


async def test_deepgram_concurrent_close_keeps_lease_until_socket_close(install_pool, monkeypatch):
    lease = FakeLease(model="nova-3")
    install_pool(FakePool(None, leases=[lease]))
    started, finish = asyncio.Event(), asyncio.Event()
    closed = []

    class Socket:
        async def send(self, data):
            started.set()
            await finish.wait()

        async def close(self):
            closed.append(True)

    async def connect(*args, **kwargs):
        return Socket()

    monkeypatch.setattr(deepgram_stream.websockets, "connect", connect)
    streamer = deepgram_stream.DeepgramStreamer("")
    await streamer.connect()
    first = asyncio.create_task(streamer.close())
    await started.wait()
    second = asyncio.create_task(streamer.close())
    try:
        await asyncio.sleep(0)
        assert lease.releases == 0
    finally:
        finish.set()
        await asyncio.gather(first, second)
    assert lease.releases == 1
    assert closed == [True]


def test_pooled_fish_ignores_legacy_global_breaker_and_empty_key(install_pool, monkeypatch):
    install_pool(FakePool(lambda request: pytest.fail("network called")))
    monkeypatch.setenv("SE_TTS_FISH", "1")
    cfg = SimpleNamespace(provider="fish", fish_model="s2.1-pro", fish_api_key="")
    voice_registry.mark_fish_failure(RuntimeError("one account failed"))
    assert voice_registry.fish_configured(cfg)
    assert voice_registry.fish_usable(cfg)
    voice_registry.reset_state()


async def test_pooled_fish_probe_reports_availability_without_bypassing_unavailable_route(install_pool, monkeypatch):
    install_pool(FakePool(None, error=RuntimeError("resources cooling")))
    monkeypatch.setenv("SE_TTS_FISH", "1")
    cfg = SimpleNamespace(provider="edge", fish_model="s2.1-pro", fish_api_key="")
    assert voice_registry.fish_configured(cfg)
    assert voice_registry.fish_usable(cfg)
    assert await voice_registry.probe(cfg) is False


def test_deepgram_configured_status_uses_pool_without_legacy_key(install_pool, monkeypatch):
    install_pool(FakePool(lambda request: pytest.fail("network called")))
    monkeypatch.setattr(deepgram_stream, "load_runtime_key", lambda: "")
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    assert deepgram_stream.deepgram_status()["configured"]
    assert deepgram_stream.deepgram_status()["source"] == "pool"


def test_deepgram_unavailable_pool_remains_configured(install_pool, monkeypatch):
    install_pool(FakePool(None, error=RuntimeError("all resources cooling")))
    monkeypatch.setattr(deepgram_stream, "load_runtime_key", lambda: "")
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    status = deepgram_stream.deepgram_status()
    assert status["configured"] is True
    assert status["available"] is False


@pytest.fixture
def real_store(tmp_path):
    from app.providers.store import ProviderStore
    from app.v2.db import DB

    db = DB(str(tmp_path / "synthetic-provider-test.db"))
    db.init()
    store = ProviderStore(db, tmp_path)
    yield store
    db.close()


def activate(store, provider, key, model, **extra):
    row = store.create({"provider": provider, "name": "Synthetic test resource", "api_key": key,
                        "model": model, "base_url": "https://synthetic.invalid/v1" if provider == "siliconflow"
                        else "https://synthetic.invalid", **extra})
    return store.verification_result(row["id"], row["revision"], True)


async def test_real_pool_fish_keeps_voice_filters_and_disables_only_rejected_account(real_store, monkeypatch):
    from app.providers.pool import ProviderPool
    from app.providers.runtime import bind_pool, unbind_pool

    wrong = activate(real_store, "fish", "synthetic-wrong-voice", "s2.1-pro", capabilities={"voices": ["other"]})
    denied = activate(real_store, "fish", "synthetic-denied", "s2.1-pro", capabilities={"voices": ["exact"]})
    allowed = activate(real_store, "fish", "synthetic-allowed", "s2.1-pro", capabilities={"voices": ["exact"]})
    original = httpx.AsyncClient
    seen = []

    def handler(request):
        key = request.headers["authorization"]
        seen.append(key)
        assert json.loads(request.content)["reference_id"] == "exact"
        return httpx.Response(401 if len(seen) == 1 else 200, content=b"p" * 1024)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        **{**kwargs, "transport": httpx.MockTransport(handler)}))
    pool = ProviderPool(real_store)
    await pool.reload()
    bind_pool(pool)
    try:
        with pytest.raises(fish_tts.FishTTSError) as error:
            await fish_tts.synthesize("one", api_key="legacy-never-use", reference_id="exact")
        assert error.value.status == 401
        assert await fish_tts.synthesize("two", api_key="legacy-never-use", reference_id="exact") == b"p" * 1024
        assert set(seen) == {"Bearer synthetic-denied", "Bearer synthetic-allowed"}
        assert pool.resource_stats(wrong["id"])["active"] == 0
        rejected, healthy = (denied, allowed) if seen[0] == "Bearer synthetic-denied" else (allowed, denied)
        assert pool.resource_stats(rejected["id"])["state"] == "disabled"
        assert pool.resource_stats(healthy["id"])["state"] == "active"
        assert pool.stats()["active"] == 0
    finally:
        unbind_pool(pool)
        await pool.aclose()


async def test_real_pool_sensevoice_uses_injected_transport_without_legacy_credentials(real_store):
    from app.providers.pool import ProviderPool
    from app.providers.runtime import bind_pool, unbind_pool

    activate(real_store, "siliconflow", "synthetic-stt", sensevoice.DEFAULT_MODEL)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"text": "real pool transcript"})

    pool = ProviderPool(real_store)
    await pool.reload()
    bind_pool(pool)
    try:
        client = sensevoice.SiliconFlowSTT(base_url="", api_key="", model=sensevoice.DEFAULT_MODEL,
                                         transport=httpx.MockTransport(handler))
        assert (await client.transcribe(b"\0\0" * 1600)).text == "real pool transcript"
        assert seen[0].headers["authorization"] == "Bearer synthetic-stt"
        assert pool.stats()["http_active"] == pool.stats()["active"] == 0
    finally:
        unbind_pool(pool)
        await pool.aclose()


async def test_real_pool_fish_queue_budget_does_not_cut_off_accepted_audio(real_store, monkeypatch):
    from app.providers.pool import ProviderPool
    from app.providers.runtime import bind_pool, unbind_pool

    activate(real_store, "fish", "synthetic-delayed", "s2.1-pro")
    original = httpx.AsyncClient

    async def handler(request):
        await asyncio.sleep(0.1)
        return httpx.Response(200, content=b"a" * 1024)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        **{**kwargs, "transport": httpx.MockTransport(handler)}))
    monkeypatch.setenv("SE_FISH_TOTAL_TIMEOUT_S", "0.5")
    pool = ProviderPool(real_store)
    await pool.reload()
    bind_pool(pool)
    try:
        assert await fish_tts.synthesize("hello", api_key="", queue_timeout=0.05) == b"a" * 1024
        assert pool.stats()["active"] == pool.stats()["http_active"] == 0
    finally:
        unbind_pool(pool)
        await pool.aclose()


async def test_real_pool_fish_still_bounds_wait_with_explicit_queue_budget(real_store, monkeypatch):
    from app.providers.pool import ProviderPool
    from app.providers.runtime import bind_pool, unbind_pool

    activate(real_store, "fish", "synthetic-busy", "s2.1-pro")
    monkeypatch.setenv("SE_FISH_TOTAL_TIMEOUT_S", "1")
    pool = ProviderPool(real_store)
    await pool.reload()
    bind_pool(pool)
    try:
        async with pool.lease("fish", "s2.1-pro"):
            with pytest.raises(fish_tts.FishTTSError) as error:
                await asyncio.wait_for(fish_tts.synthesize("hello", api_key="", queue_timeout=0.01), 0.2)
            assert error.value.status == 504
        assert pool.stats()["active"] == pool.stats()["waiting"] == 0
    finally:
        unbind_pool(pool)
        await pool.aclose()


async def test_real_pool_deepgram_keeps_distinct_sessions_and_releases_bounds(real_store, monkeypatch):
    from app.providers.pool import ProviderPool
    from app.providers.runtime import bind_pool, unbind_pool

    activate(real_store, "deepgram", "synthetic-deepgram", "nova-3", concurrency_limit=2)
    sockets = []

    class Socket:
        def __init__(self):
            self.closed = 0

        async def send(self, data):
            pass

        async def close(self):
            self.closed += 1

    async def connect(url, **kwargs):
        assert kwargs["additional_headers"] == {"Authorization": "Token synthetic-deepgram"}
        ws = Socket()
        sockets.append(ws)
        return ws

    monkeypatch.setattr(deepgram_stream.websockets, "connect", connect)
    pool = ProviderPool(real_store, ws_limit=2)
    await pool.reload()
    bind_pool(pool)
    first, second = deepgram_stream.DeepgramStreamer(""), deepgram_stream.DeepgramStreamer("")
    try:
        await first.connect()
        await second.connect()
        assert len(sockets) == 2
        assert pool.stats()["ws_active"] == pool.stats()["active"] == 2
        await first.close()
        assert pool.stats()["ws_active"] == pool.stats()["active"] == 1
        await second.close()
        assert pool.stats()["ws_active"] == pool.stats()["active"] == 0
        assert [socket.closed for socket in sockets] == [1, 1]
    finally:
        await first.close()
        await second.close()
        unbind_pool(pool)
        await pool.aclose()


async def test_fish_compatibility_retry_obeys_actual_request_budget(real_store, monkeypatch):
    from app.providers.pool import ProviderPool
    from app.providers.runtime import bind_pool, unbind_pool
    item = activate(real_store, 'fish', 'bitrate-budget-key', 'same', daily_limit=1, rpm_limit=1)
    requests = []
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(400,text='mp3_bitrate unsupported')
        return httpx.Response(200,content=b'a'*1024)
    pool = ProviderPool(real_store)
    await pool.reload()
    bind_pool(pool)
    monkeypatch.setattr(fish_tts,'_pool_client',lambda _: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        with pytest.raises(fish_tts.FishTTSError):
            await fish_tts.synthesize('hello',api_key='',model='same',audio_format='mp3')
        assert len(requests) == 1
        assert real_store.budget_stats(item['quota_group_id'])['daily_requests'] == 1
    finally:
        await pool.aclose()
        unbind_pool(pool)


async def test_deepgram_error_event_never_exposes_upstream_secret(monkeypatch):
    class Socket:
        def __aiter__(self):
            return self.events()
        async def events(self):
            yield json.dumps({'type':'Error','message':'Gateway auth failure: synthetic-pooled-secret'})
    stream = deepgram_stream.DeepgramStreamer('synthetic-pooled-secret')
    stream._ws = Socket()
    events = [event async for event in stream.events()]
    assert 'synthetic-pooled-secret' not in json.dumps(events)


async def test_fish_non_bitrate_validation_error_never_disables_account_bitrate(install_pool):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(400, text="reference_id is invalid")
        return httpx.Response(200, content=b"a" * 1024)

    pool = install_pool(FakePool(handler))
    with pytest.raises(fish_tts.FishTTSError) as error:
        await fish_tts.synthesize("hello", api_key="", audio_format="mp3")
    assert error.value.status == 400
    assert len(requests) == len(pool.acquired) == 1
    assert await fish_tts.synthesize("again", api_key="", audio_format="mp3") == b"a" * 1024
    assert requests[0]["mp3_bitrate"] == requests[1]["mp3_bitrate"]
    await pool.aclose()


async def test_fish_compatibility_and_busy_rejection_never_make_third_request(install_pool):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(422 if len(requests) == 1 else 429,
                              text="mp3_bitrate unsupported" if len(requests) == 1 else "busy")

    lease = FakeLease()
    pool = install_pool(FakePool(handler, leases=[lease, lease]))
    with pytest.raises(fish_tts.FishTTSError) as error:
        await fish_tts.synthesize("hello", api_key="", audio_format="mp3")
    assert error.value.status == 429
    assert len(requests) == len(pool.acquired) == lease.releases == 2
    assert "mp3_bitrate" in requests[0]
    assert "mp3_bitrate" not in requests[1]
    await pool.aclose()


async def test_deepgram_socket_close_reason_never_exposes_upstream_secret():
    class Socket:
        async def __aiter__(self):
            if False:
                yield ""
            raise RuntimeError("upstream close reason contains synthetic-pooled-secret")

    stream = deepgram_stream.DeepgramStreamer("synthetic-pooled-secret")
    stream._ws = Socket()
    with pytest.raises(deepgram_stream.StreamUnavailable) as error:
        await anext(stream.events())
    assert "synthetic-pooled-secret" not in str(error.value)


async def test_fish_allowed_compatibility_retry_reserves_each_actual_request(real_store, monkeypatch):
    from app.providers.pool import ProviderPool
    from app.providers.runtime import bind_pool, unbind_pool

    item = activate(real_store, "fish", "synthetic-two-attempts", "same", daily_limit=2, rpm_limit=2)
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(422, text="mp3_bitrate unsupported")
        return httpx.Response(200, content=b"a" * 1024)

    pool = ProviderPool(real_store)
    await pool.reload()
    bind_pool(pool)
    monkeypatch.setattr(fish_tts, "_pool_client", lambda _: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        assert await fish_tts.synthesize("hello", api_key="", model="same", audio_format="mp3") == b"a" * 1024
        assert len(requests) == 2
        assert "mp3_bitrate" in requests[0] and "mp3_bitrate" not in requests[1]
        assert real_store.budget_stats(item["quota_group_id"])["daily_requests"] == 2
        assert pool.stats()["active"] == pool.stats()["http_active"] == 0
    finally:
        await pool.aclose()
        unbind_pool(pool)
