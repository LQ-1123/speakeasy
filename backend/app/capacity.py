"""Bounded foreground admission and persistent opt-in LLM request budgets.

One process owns admission; SQLite transactions make usage limits restart-safe.
Request counts are usage ceilings, not an estimate of the provider's currency bill.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
import weakref
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone


def env_int(name: str, default: int, minimum: int = 0, maximum: int = 10000) -> int:
    try:
        return max(minimum, min(maximum, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


class CapacityError(RuntimeError):
    def __init__(self, message: str, *, retry_after: int = 3, status_code: int = 429):
        super().__init__(message)
        self.retry_after = retry_after
        self.status_code = status_code


class ChatAdmission:
    def __init__(self, *, concurrency: int | None = None, queue_size: int | None = None,
                 timeout_s: float = 30):
        self.limit = max(1, concurrency if concurrency is not None else env_int('SE_CHAT_CONCURRENCY', 10, 1, 100))
        self.queue_size = max(0, queue_size if queue_size is not None else env_int('SE_CHAT_QUEUE_SIZE', 20, 0, 200))
        self.timeout_s = max(.001, min(30.0, timeout_s))
        self.active = 0
        self._users: set[str] = set()
        self._waiting: deque[asyncio.Future] = deque()
        self._counts = {'accepted': 0, 'rejected': 0, 'timeouts': 0, 'completed': 0}
        self.last_activity = 0.0

    @property
    def busy(self) -> bool:
        return bool(self._users)

    def stats(self) -> dict:
        return {**self._counts, 'active': self.active, 'waiting': len(self._waiting),
                'limit': self.limit, 'queue_limit': self.queue_size, 'deadline_s': self.timeout_s}

    def _release(self) -> None:
        self.active -= 1
        while self._waiting and self.active < self.limit:
            future = self._waiting.popleft()
            if future.done():
                continue
            self.active += 1
            future.set_result(None)

    @asynccontextmanager
    async def turn(self, user_id: str):
        user_id = str(user_id)
        if user_id in self._users or (self.active >= self.limit and len(self._waiting) >= self.queue_size):
            self._counts['rejected'] += 1
            raise CapacityError('当前消息仍在处理或服务繁忙，请稍后重试')
        self._users.add(user_id)
        self.last_activity = time.monotonic()
        self._counts['accepted'] += 1
        future = None
        acquired = False
        try:
            # Covers queue waiting, LLM, STT and audio completion together.
            async with asyncio.timeout(self.timeout_s):
                if self.active < self.limit and not self._waiting:
                    self.active += 1
                    acquired = True
                else:
                    future = asyncio.get_running_loop().create_future()
                    self._waiting.append(future)
                    await future
                    acquired = True
                yield
                self._counts['completed'] += 1
        except TimeoutError as exc:
            self._counts['timeouts'] += 1
            raise CapacityError('回复超时，请稍后重试', status_code=504) from exc
        finally:
            if future is not None:
                # A slot can be handed off just as its waiter is cancelled.
                if not acquired and future.done() and not future.cancelled() and future.exception() is None:
                    acquired = True
                if future in self._waiting:
                    self._waiting.remove(future)
            if acquired:
                self._release()
            self._users.discard(user_id)
            self.last_activity = time.monotonic()


class UsageBudget:
    def __init__(self, conn, lock, *, daily_limit: int | None = None, monthly_limit: int | None = None):
        self.conn, self.lock = conn, lock
        self.daily_limit = env_int('SE_LLM_DAILY_REQUESTS', 0) if daily_limit is None else daily_limit
        self.monthly_limit = env_int('SE_LLM_MONTHLY_REQUESTS', 0, maximum=10000000) if monthly_limit is None else monthly_limit
        with lock, conn:
            conn.execute('CREATE TABLE IF NOT EXISTS provider_usage '
                         '(provider TEXT NOT NULL, day TEXT NOT NULL, requests INTEGER NOT NULL, '
                         'PRIMARY KEY(provider,day))')

    def reserve(self, provider: str = 'llm', *, day: str | None = None) -> None:
        day = day or datetime.now(timezone.utc).strftime('%Y-%m-%d')
        with self.lock:
            try:
                self.conn.execute('BEGIN IMMEDIATE')
                daily = self.conn.execute('SELECT requests FROM provider_usage WHERE provider=? AND day=?',
                                          (provider, day)).fetchone()
                monthly = self.conn.execute('SELECT COALESCE(SUM(requests),0) FROM provider_usage '
                                            'WHERE provider=? AND day>=? AND day<?',
                                            (provider, day[:7]+'-01', day[:7]+'-32')).fetchone()[0]
                if (self.daily_limit > 0 and (daily[0] if daily else 0) >= self.daily_limit
                        or self.monthly_limit > 0 and monthly >= self.monthly_limit):
                    raise CapacityError('今日或本月 AI 调用额度已用完', retry_after=3600)
                self.conn.execute('INSERT INTO provider_usage VALUES (?,?,1) '
                                  'ON CONFLICT(provider,day) DO UPDATE SET requests=requests+1', (provider, day))
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def stats(self, *, day: str | None = None) -> dict:
        day = day or datetime.now(timezone.utc).strftime('%Y-%m-%d')
        with self.lock:
            rows = self.conn.execute('SELECT day, requests FROM provider_usage '
                                     'WHERE provider=? AND day>=? AND day<?',
                                     ('llm', day[:7]+'-01', day[:7]+'-32')).fetchall()
        return {'daily_requests': sum(r[1] for r in rows if r[0] == day),
                'monthly_requests': sum(r[1] for r in rows), 'daily_limit': self.daily_limit,
                'monthly_limit': self.monthly_limit}


_llm_slots: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


@asynccontextmanager
async def llm_slot():
    from app.jobs import background_is_busy, wait_for_background_capacity
    loop = asyncio.get_running_loop()
    gate = _llm_slots.get(loop)
    if gate is None:
        gate = _llm_slots[loop] = ChatAdmission(
            concurrency=env_int('SE_LLM_CONCURRENCY', 10, 1, 100),
            queue_size=env_int('SE_LLM_QUEUE_SIZE', 20, 0, 200))
    while True:
        await wait_for_background_capacity()
        async with gate.turn(uuid.uuid4().hex):
            # Foreground may have arrived while this task queued. Release the
            # handed-off slot before waiting so a one-slot configuration works.
            if background_is_busy():
                continue
            yield
            return
