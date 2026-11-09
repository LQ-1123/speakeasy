from __future__ import annotations

import sqlite3
import asyncio
import threading

from app.jobs import DurableJobQueue


def test_jobs_are_deduplicated_durable_and_retryable(tmp_path):
    path = tmp_path / "jobs.sqlite"
    db = sqlite3.connect(path)
    queue = DurableJobQueue(db)
    first = queue.enqueue("daily", {"day": "2026-11-09"}, dedupe_key="daily:2026-11-09")
    second = queue.enqueue("daily", {"day": "2026-11-09"}, dedupe_key="daily:2026-11-09")
    assert first == second
    db.close()

    reopened = sqlite3.connect(path)
    queue = DurableJobQueue(reopened)
    attempts = []

    def flaky(payload):
        attempts.append(payload)
        if len(attempts) == 1:
            raise RuntimeError("temporary")

    assert queue.run_one({"daily": flaky}, now=100) is True
    assert queue.get(first)["status"] == "retry"
    assert queue.run_one({"daily": flaky}, now=1000) is True
    assert queue.get(first)["status"] == "done"
    assert len(attempts) == 2


def test_active_calls_block_background_claims(tmp_path):
    db = sqlite3.connect(tmp_path / "jobs.sqlite")
    queue = DurableJobQueue(db)
    job_id = queue.enqueue("refill", {}, dedupe_key="refill:one")
    assert queue.run_one({"refill": lambda _: None}, active_calls=3) is False
    assert queue.get(job_id)["status"] == "queued"


def test_async_dispatcher_awaits_handlers_and_yields_to_any_call(tmp_path):
    db = sqlite3.connect(tmp_path / "jobs.sqlite")
    queue = DurableJobQueue(db, lock=threading.RLock())
    job_id = queue.enqueue("daily", {}, dedupe_key="daily:async")
    seen = []

    async def handle(payload):
        await asyncio.sleep(0)
        seen.append(payload)

    assert asyncio.run(queue.run_one_async({"daily": handle}, active_calls=1)) is False
    assert asyncio.run(queue.run_one_async({"daily": handle})) is True
    assert seen == [{}]
    assert queue.get(job_id)["status"] == "done"


def test_independent_workers_do_not_execute_the_same_claim(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "concurrent.sqlite"
    started = threading.Event()
    release = threading.Event()
    setup = sqlite3.connect(path)
    job_id = DurableJobQueue(setup).enqueue("hold", {}, dedupe_key="hold:one")
    setup.close()

    def work():
        conn = sqlite3.connect(path)
        queue = DurableJobQueue(conn)

        def hold(payload):
            started.set()
            assert release.wait(5)

        result = queue.run_one({"hold": hold})
        conn.close()
        return result

    with ThreadPoolExecutor(max_workers=1) as workers:
        running = workers.submit(work)
        assert started.wait(5)
        second = sqlite3.connect(path)
        assert DurableJobQueue(second).run_one({"hold": lambda _: None}) is False
        release.set()
        assert running.result(timeout=5) is True
        assert DurableJobQueue(second).get(job_id)["status"] == "done"
        second.close()
