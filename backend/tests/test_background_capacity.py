"""Background jobs yield to chat and never hold SQLite work on the event loop."""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from app import jobs, maintenance
from app.jobs import DurableJobQueue
from app.v2.db import DB
from app.v2.memory import MemoryManager


async def test_async_queue_claim_and_finish_do_not_block_event_loop(tmp_path):
    conn = sqlite3.connect(tmp_path / "jobs.db", check_same_thread=False)
    queue = DurableJobQueue(conn)
    queue.enqueue("test", {}, dedupe_key="test:1")
    entered, release = threading.Event(), threading.Event()
    original = queue._claim

    def delayed_claim(**kwargs):
        entered.set()
        assert release.wait(1)
        return original(**kwargs)

    queue._claim = delayed_claim
    task = asyncio.create_task(queue.run_one_async({"test": lambda _: None}))
    try:
        await asyncio.sleep(0.01)
        assert entered.is_set()
        assert not task.done(), "SQLite claim blocked the event loop until its timeout"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        conn.close()


def test_due_slots_do_not_write_unchanged_periodic_jobs(tmp_path):
    conn = sqlite3.connect(tmp_path / "jobs.db")
    queue = DurableJobQueue(conn)
    slots = maintenance.PeriodicSchedule()
    rhythm = {"life_tick_s": 30, "posts_tick_s": 60}
    assert slots.enqueue_due(queue, now=1000, day="2026-11-14", rhythm=rhythm) == 8
    before = conn.total_changes
    for _ in range(10):
        assert slots.enqueue_due(queue, now=1001, day="2026-11-14", rhythm=rhythm) == 0
    assert conn.total_changes == before
    assert slots.enqueue_due(queue, now=1020, day="2026-11-14", rhythm=rhythm) == 3
    conn.close()


async def test_background_guard_waits_between_paid_calls_without_cancelling_inflight():
    busy = False
    seen = []

    async def work():
        with jobs.background_scope(lambda: busy):
            await jobs.wait_for_background_capacity()
            seen.append("first")
            await asyncio.sleep(0.01)
            await jobs.wait_for_background_capacity()
            seen.append("second")

    task = asyncio.create_task(work())
    await asyncio.sleep(0)
    busy = True
    await asyncio.sleep(0.03)
    assert seen == ["first"]
    assert not task.done()
    busy = False
    await asyncio.wait_for(task, 0.5)
    assert seen == ["first", "second"]


def test_periodic_cleanup_preserves_permanent_and_unfinished_dedupe_records(tmp_path):
    conn = sqlite3.connect(tmp_path / "jobs.db")
    queue = DurableJobQueue(conn)
    records = [("life_events", "life_events:123", "done"),
               ("daily_world", "daily_world:2026-11-01", "done"),
               ("world", "world:startup:123", "done"),
               ("followup", "followup:alice:friend:event", "done"),
               ("life_events", "life_events:124", "retry"),
               ("life_events", "life_events:manual", "done")]
    for kind, key, status in records:
        identity = queue.enqueue(kind, {}, dedupe_key=key)
        conn.execute("UPDATE background_jobs SET status=?,updated_at=0 WHERE id=?", (status, identity))
        conn.commit()
    assert queue.prune_periodic(now=8 * 86400) == 2
    remaining = {row[0] for row in conn.execute("SELECT dedupe_key FROM background_jobs")}
    assert remaining == {key for _, key, _ in records[2:]}
    conn.close()


async def test_memory_database_stages_run_off_loop_and_merge_latest_state(tmp_path, monkeypatch):
    db = DB(str(tmp_path / "memory.db"))
    db.init()
    friend = db.create_friend({"name": "Friend", "shared_episodes": ["old"]})
    manager = MemoryManager(db, SimpleNamespace(uses_mock=False))
    loop_thread = threading.get_ident()
    calls = []
    for name in ("get_friend", "get_profile", "recent_messages", "update_friend", "update_profile", "count_turns"):
        original = getattr(db, name)

        def checked(*args, _original=original, _name=name, **kwargs):
            assert threading.get_ident() != loop_thread, f"{_name} blocked event loop"
            calls.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(db, name, checked)

    async def extract(*args, **kwargs):
        # A foreground update while the network is pending must survive the merge.
        await asyncio.to_thread(db.update_friend, friend["id"], shared_episodes=["old", "during network"])
        return {"fact_sheet": {"interests": ["hiking"]}, "shared_episodes": ["new"]}

    monkeypatch.setattr("app.v2.memory.complete_json_safe", extract)
    try:
        result = await manager.after_turn(friend, recent_messages=[{"sender": "user", "transcript": "I enjoy hiking."}])
        assert result["updated"], result
        assert result["shared_episodes"] == ["old", "during network", "new"]
        assert {"get_profile", "update_profile", "get_friend", "update_friend"} <= set(calls)
    finally:
        db.close()


def test_chat_admission_and_calls_both_pause_maintenance():
    admission = SimpleNamespace(busy=True)
    app = SimpleNamespace(state=SimpleNamespace(v2=SimpleNamespace(chat=SimpleNamespace(admission=admission))))
    assert maintenance.foreground_busy(app)
    admission.busy = False
    assert not maintenance.foreground_busy(app)
    assert maintenance.foreground_busy(app, active_calls=1)


async def test_cancelled_async_claim_is_released_before_shutdown(tmp_path):
    conn = sqlite3.connect(tmp_path / "cancel.db", check_same_thread=False)
    queue = DurableJobQueue(conn)
    identity = queue.enqueue("test", {}, dedupe_key="test:cancel")
    entered, release = threading.Event(), threading.Event()
    original = queue._claim

    def slow_claim(**kwargs):
        claimed = original(**kwargs)
        entered.set()
        assert release.wait(1)
        return claimed

    queue._claim = slow_claim
    task = asyncio.create_task(queue.run_one_async({"test": lambda _: pytest.fail("cancelled job executed")}))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done(), "shutdown must drain the outstanding SQLite operation"
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert queue.get(identity)["status"] == "retry"
    conn.close()


async def test_finish_runs_in_worker_thread(tmp_path):
    conn = sqlite3.connect(tmp_path / "finish.db", check_same_thread=False)
    queue = DurableJobQueue(conn)
    identity = queue.enqueue("test", {}, dedupe_key="test:finish")
    loop_thread = threading.get_ident()
    original = queue._finish

    def checked_finish(*args):
        assert threading.get_ident() != loop_thread
        return original(*args)

    queue._finish = checked_finish
    try:
        assert await queue.run_one_async({"test": lambda _: None})
        assert queue.get(identity)["status"] == "done"
    finally:
        conn.close()


async def test_background_fish_waits_without_provider_slot():
    import httpx
    from app.voice import fish_tts

    busy = True
    seen = []
    state = fish_tts._state()
    state.client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: seen.append(request.url) or httpx.Response(200, content=b"audio" * 200)))
    try:
        with jobs.background_scope(lambda: busy), fish_tts.priority("background"):
            task = asyncio.create_task(fish_tts.synthesize("hello", api_key="offline"))
        await asyncio.sleep(0.01)
        assert state.limiter.active == state.limiter.waiting == 0
        assert not seen
        busy = False
        assert await asyncio.wait_for(task, 0.5) == b"audio" * 200
    finally:
        await fish_tts.aclose()


async def test_memory_cancellation_drains_outstanding_database_stage(tmp_path, monkeypatch):
    db = DB(str(tmp_path / "drain.db"))
    db.init()
    friend = db.create_friend({"name": "Friend"})
    manager = MemoryManager(db, SimpleNamespace(uses_mock=True))
    entered, release = threading.Event(), threading.Event()
    original = manager._read_memory_snapshot

    def slow(*args):
        entered.set()
        assert release.wait(1)
        return original(*args)

    monkeypatch.setattr(manager, "_read_memory_snapshot", slow)
    task = asyncio.create_task(manager.after_turn(friend))
    try:
        while not entered.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done(), "shutdown closed DB while its worker is still running"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        db.close()
