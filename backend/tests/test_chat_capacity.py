from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_admission_is_fifo_bounded_and_blocks_same_user():
    from app.capacity import ChatAdmission, CapacityError

    gate = ChatAdmission(concurrency=1, queue_size=1, timeout_s=1)
    entered, release = asyncio.Event(), asyncio.Event()
    order = []

    async def hold():
        async with gate.turn('alice'):
            entered.set()
            await release.wait()

    async def second():
        async with gate.turn('bob'):
            order.append('bob')

    first = asyncio.create_task(hold())
    await entered.wait()
    waiting = asyncio.create_task(second())
    await asyncio.sleep(0)
    with pytest.raises(CapacityError):
        async with gate.turn('alice'):
            pytest.fail('same user admitted twice')
    with pytest.raises(CapacityError):
        async with gate.turn('charlie'):
            pytest.fail('queue overflow admitted')
    release.set()
    await asyncio.gather(first, waiting)
    assert order == ['bob']
    assert gate.stats()['active'] == gate.stats()['waiting'] == 0


@pytest.mark.asyncio
async def test_admission_deadline_covers_work_and_releases_user():
    from app.capacity import ChatAdmission, CapacityError

    gate = ChatAdmission(concurrency=1, queue_size=1, timeout_s=.02)
    with pytest.raises(CapacityError, match='超时'):
        async with gate.turn('alice'):
            await asyncio.Event().wait()
    async with gate.turn('alice'):
        pass
    assert gate.stats()['active'] == 0


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_hold_user_or_capacity():
    from app.capacity import ChatAdmission

    gate = ChatAdmission(concurrency=1, queue_size=1, timeout_s=1)
    async with gate.turn('alice'):
        async def wait():
            async with gate.turn('bob'):
                pass
        task = asyncio.create_task(wait())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert gate.stats()['waiting'] == 0
    async with gate.turn('bob'):
        pass


def test_usage_budget_is_persistent_atomic_and_day_scoped(tmp_path):
    import sqlite3
    import threading
    from app.capacity import UsageBudget, CapacityError

    conn = sqlite3.connect(tmp_path / 'budget.db')
    budget = UsageBudget(conn, threading.RLock(), daily_limit=2, monthly_limit=3)
    budget.reserve('llm', day='2026-11-14')
    budget.reserve('llm', day='2026-11-14')
    with pytest.raises(CapacityError):
        budget.reserve('llm', day='2026-11-14')
    conn.close()
    conn = sqlite3.connect(tmp_path / 'budget.db')
    reopened = UsageBudget(conn, threading.RLock(), daily_limit=2, monthly_limit=3)
    reopened.reserve('llm', day='2026-11-15')
    with pytest.raises(CapacityError):
        reopened.reserve('llm', day='2026-11-15')
    assert reopened.stats(day='2026-11-15')['monthly_requests'] == 3
    conn.close()


@pytest.mark.asyncio
async def test_failed_audio_is_visible_and_retry_does_not_repeat_llm(tmp_path, monkeypatch):
    from backend.tests.test_chat_idempotency import _client
    monkeypatch.setenv('SE_TTS_DEFER_AUDIO', '0')
    client, services, db = _client(tmp_path)
    friend = db.create_friend({'name': 'Niamh'})
    async def fail(*args):
        return None, 0
    services.chat._synth_friend_audio = fail
    result = await services.chat.handle_text(friend['id'], 'Hello', client_message_id='client-failed-001')
    message = result['friend_messages'][0]
    assert message['audio_status'] == 'failed'
    async def succeed(*args):
        return 'ready.mp3', 3000
    services.chat._synth_friend_audio = succeed
    ready = await services.chat.retry_audio(message['id'])
    assert ready['id'] == message['id']
    assert ready['audio_status'] == 'ready'
    assert len(db.list_messages(friend['id'])) == 2
    await services.chat.aclose()
    db.close()


@pytest.mark.asyncio
async def test_main_chat_does_not_schedule_paid_polishing_by_default(tmp_path, monkeypatch):
    from backend.tests.test_chat_idempotency import _client
    monkeypatch.delenv('SE_CHAT_AUTO_SUGGESTIONS', raising=False)
    client, services, db = _client(tmp_path)
    friend = db.create_friend({'name': 'Niamh'})
    called = []
    async def suggestion(*args):
        called.append(args)
    services.chat._suggestion_worker = suggestion
    result = await services.chat.handle_text(friend['id'], 'Hello')
    await asyncio.sleep(0)
    assert result['suggestion_pending'] is False
    assert called == []
    await services.chat.aclose()
    db.close()


@pytest.mark.asyncio
async def test_llm_reserves_each_actual_attempt_before_network(tmp_path):
    import sqlite3
    import threading
    from types import SimpleNamespace
    from app.capacity import UsageBudget, CapacityError
    from app.config import LLMSettings
    from app.llm.client import LLMClient

    conn = sqlite3.connect(tmp_path / 'llm-budget.db', check_same_thread=False)
    budget = UsageBudget(conn, threading.RLock(), daily_limit=1)
    llm = LLMClient(LLMSettings(api_key='offline-test-key'))
    llm.usage_budget = budget
    dispatched = []
    async def create(**kwargs):
        dispatched.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{}'))])
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    assert await llm.complete_json([{'role': 'user', 'content': 'hello'}]) == '{}'
    with pytest.raises(CapacityError):
        await llm.complete_json([{'role': 'user', 'content': 'hello again'}])
    assert len(dispatched) == 1
    conn.close()


@pytest.mark.asyncio
async def test_audio_timeout_is_terminal_and_does_not_leave_slots(tmp_path):
    from backend.tests.test_chat_idempotency import _client
    from app.capacity import CapacityError
    client, services, db = _client(tmp_path)
    friend = db.create_friend({'name': 'Niamh'})
    started = asyncio.Event()
    async def blocked(*args):
        started.set()
        await asyncio.Event().wait()
    services.chat._synth_friend_audio = blocked
    services.chat.admission.timeout_s = .1
    with pytest.raises(CapacityError):
        await services.chat.handle_text(friend['id'], 'Hello', client_message_id='timeout-test-001')
    assert started.is_set()
    messages = db.list_messages(friend['id'])
    assert messages[-1]['audio_status'] == 'failed'
    assert services.chat.admission.stats()['active'] == 0
    assert not services.chat._voice_tasks
    await services.chat.aclose()
    db.close()


@pytest.mark.asyncio
async def test_retry_old_client_id_only_returns_its_own_reply(tmp_path):
    from backend.tests.test_chat_idempotency import _client
    client, services, db = _client(tmp_path)
    friend = db.create_friend({'name': 'Niamh'})
    first = await services.chat.handle_text(friend['id'], 'first', client_message_id='distinct-first-001')
    second = await services.chat.handle_text(friend['id'], 'second', client_message_id='distinct-second-001')
    for i in range(65):
        db.add_message(friend['id'], 'user', f'later {i}', created_at='2099-01-01T00:00:00+00:00')
    retried = await services.chat.handle_text(friend['id'], 'first', client_message_id='distinct-first-001')
    assert [m['id'] for m in retried['friend_messages']] == [m['id'] for m in first['friend_messages']]
    assert first['friend_messages'][0]['id'] != second['friend_messages'][0]['id']
    await services.chat.aclose()
    db.close()


@pytest.mark.asyncio
async def test_cancel_during_message_insert_cannot_leave_queued_audio(tmp_path):
    import threading
    from backend.tests.test_chat_idempotency import _client
    client, services, db = _client(tmp_path)
    friend = db.create_friend({'name': 'Niamh'})
    entered, release = threading.Event(), threading.Event()
    original = db.add_message
    def slow_insert(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)
    db.add_message = slow_insert
    task = asyncio.create_task(services.chat.store_friend_voice(friend, 'hello'))
    while not entered.is_set():
        await asyncio.sleep(.001)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(.02)
    messages = db.list_messages(friend['id'])
    assert len(messages) == 1
    assert messages[0]['audio_status'] == 'failed'
    await services.chat.aclose()
    db.close()


@pytest.mark.asyncio
async def test_memory_friends_share_one_background_generation_slot(tmp_path):
    from backend.tests.test_chat_idempotency import _client
    client, services, db = _client(tmp_path)
    peak = active = 0
    release = asyncio.Event()
    async def memory(*args, **kwargs):
        nonlocal peak, active
        active += 1
        peak = max(peak, active)
        try:
            await release.wait()
            return {'updated': False}
        finally:
            active -= 1
    services.memory.after_turn = memory
    for i in range(5):
        friend = db.create_friend({'name': f'Friend {i}'})
        db.add_message(friend['id'], 'user', 'hello')
        services.chat._schedule_memory(friend['id'])
    await asyncio.sleep(.05)
    assert peak == 1
    release.set()
    await asyncio.gather(*list(services.chat._tasks))
    await services.chat.aclose()
    db.close()


@pytest.mark.asyncio
async def test_json_provider_concurrency_is_bounded_before_dispatch(monkeypatch):
    from types import SimpleNamespace
    from app.config import LLMSettings
    from app.llm.client import LLMClient
    monkeypatch.setenv('SE_LLM_CONCURRENCY', '1')
    release = asyncio.Event()
    active = peak = 0
    async def create(**kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await release.wait()
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{}'))])
        finally:
            active -= 1
    llm = LLMClient(LLMSettings(api_key='offline-key'))
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    tasks = [asyncio.create_task(llm.complete_json([{'role': 'user', 'content': 'hello'}])) for _ in range(3)]
    await asyncio.sleep(.01)
    assert peak == 1
    release.set()
    assert await asyncio.gather(*tasks) == ['{}'] * 3


@pytest.mark.asyncio
async def test_background_waiting_for_llm_slot_yields_it_to_foreground(monkeypatch):
    from app.capacity import llm_slot
    from app.jobs import background_scope
    monkeypatch.setenv('SE_LLM_CONCURRENCY', '1')
    busy = False
    release_background, foreground_started = asyncio.Event(), asyncio.Event()
    async def background():
        with background_scope(lambda: busy):
            async with llm_slot():
                await release_background.wait()
    async def foreground():
        async with llm_slot():
            foreground_started.set()
    tasks = []
    try:
        async with llm_slot():
            tasks.append(asyncio.create_task(background()))
            await asyncio.sleep(.005)
            busy = True
            tasks.append(asyncio.create_task(foreground()))
        await asyncio.wait_for(foreground_started.wait(), .1)
    finally:
        busy = False
        release_background.set()
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_cancel_after_deferred_voice_spawn_drains_turn_audio(tmp_path, monkeypatch):
    from backend.tests.test_chat_idempotency import _client
    from app.capacity import CapacityError
    monkeypatch.setenv('SE_TTS_DEFER_AUDIO', '1')
    client, services, db = _client(tmp_path)
    friend = db.create_friend({'name': 'Niamh'})
    async def blocked(*args):
        await asyncio.Event().wait()
    services.chat._synth_friend_audio = blocked
    services.chat._maybe_ad_message = blocked
    services.chat.admission.timeout_s = .1
    with pytest.raises(CapacityError):
        await services.chat.handle_text(friend['id'], 'hello')
    assert not services.chat._voice_tasks
    assert db.list_messages(friend['id'])[-1]['audio_status'] == 'failed'
    assert services.chat.admission.stats()['active'] == 0
    await services.chat.aclose()
    db.close()


@pytest.mark.asyncio
async def test_failed_turn_retries_do_not_borrow_another_turn_reply(tmp_path):
    from backend.tests.test_chat_idempotency import _client
    client, services, db = _client(tmp_path)
    friend = db.create_friend({'name': 'Niamh'})
    async def fail(*args):
        raise RuntimeError('offline injected generation failure')
    services.chat._llm_reply = fail
    for key in ('failed-turn-a', 'failed-turn-b'):
        with pytest.raises(RuntimeError):
            await services.chat.handle_text(friend['id'], key, client_message_id=key)
    async def reply(friend, text, *args):
        return 'reply ' + text
    services.chat._llm_reply = reply
    first = await services.chat.handle_text(friend['id'], 'failed-turn-a', client_message_id='failed-turn-a')
    second = await services.chat.handle_text(friend['id'], 'failed-turn-b', client_message_id='failed-turn-b')
    assert first['friend_messages'][0]['id'] != second['friend_messages'][0]['id']
    assert second['friend_messages'][0]['reply_to_message_id'] == second['user_message']['id']
    await services.chat.aclose()
    db.close()


def test_explicit_suggestion_propagates_provider_failure(tmp_path):
    from backend.tests.test_chat_idempotency import _client
    client, services, db = _client(tmp_path)
    services.engines.llm.uses_mock = False
    async def fail(*args, **kwargs):
        raise RuntimeError('offline provider failure')
    services.engines.llm.complete_json = fail
    friend = db.create_friend({'name': 'Niamh'})
    message = db.add_message(friend['id'], 'user', 'I went home')
    with client:
        response = client.post(f"/api/v2/messages/{message['id']}/suggestion")
        assert response.status_code == 502
    db.close()


@pytest.mark.asyncio
async def test_service_restart_marks_unfinished_audio_retryable(tmp_path):
    from types import SimpleNamespace
    from app.v2.db import DB
    from app.v2.services import build_services
    path = tmp_path / 'speakeasy_v2.db'
    db = DB(str(path))
    db.init()
    friend = db.create_friend({'name': 'Niamh'})
    message = db.add_message(friend['id'], 'friend', 'hello', audio_status='generating')
    db.close()
    services = build_services(SimpleNamespace(llm=SimpleNamespace()), str(tmp_path))
    assert services.db.get_message(message['id'])['audio_status'] == 'failed'
    assert services.db.get_message(message['id'])['audio_error']
    await services.aclose()
