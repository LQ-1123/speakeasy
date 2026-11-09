"""Small durable SQLite queue for low-priority background maintenance."""

from __future__ import annotations

import json
import inspect
import asyncio
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable


class DurableJobQueue:
    def __init__(self, conn: sqlite3.Connection, *, max_attempts: int = 5, lock: Any = None) -> None:
        self.conn = conn
        self.lock = lock if lock is not None else threading.RLock()
        self.max_attempts = max(1, int(max_attempts))
        with self.lock:
            self._initialize()

    def _initialize(self) -> None:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS background_jobs ("
            "id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,"
            "dedupe_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'queued',"
            "attempts INTEGER NOT NULL DEFAULT 0, priority INTEGER NOT NULL DEFAULT 100,"
            "run_after REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,"
            "last_error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_background_jobs_ready "
            "ON background_jobs(status, run_after, priority, created_at)"
        )
        self.conn.commit()

    def enqueue(
        self, kind: str, payload: dict[str, Any], *, dedupe_key: str,
        priority: int = 100, run_after: float = 0,
    ) -> str:
        with self.lock:
            return self._enqueue(kind, payload, dedupe_key=dedupe_key, priority=priority, run_after=run_after)

    def _enqueue(
        self, kind: str, payload: dict[str, Any], *, dedupe_key: str,
        priority: int = 100, run_after: float = 0,
    ) -> str:
        now = time.time()
        job_id = uuid.uuid4().hex
        self.conn.execute(
            "INSERT OR IGNORE INTO background_jobs "
            "(id,kind,payload,dedupe_key,priority,run_after,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (job_id, str(kind), json.dumps(payload, separators=(",", ":")),
             str(dedupe_key), int(priority), float(run_after), now, now),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM background_jobs WHERE dedupe_key=?", (str(dedupe_key),)
        ).fetchone()
        return str(row[0])

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            cur = self.conn.execute("SELECT * FROM background_jobs WHERE id=?", (job_id,))
            row = cur.fetchone()
            return dict(zip((c[0] for c in cur.description), row)) if row else None

    def _claim(
        self, *, now: float | None = None, active_calls: int = 0,
        call_budget: int = 1, lease_seconds: float = 120,
    ) -> tuple[dict[str, Any], float] | None:
        if int(active_calls) >= int(call_budget):
            return None
        current = time.time() if now is None else float(now)
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute(
                "SELECT * FROM background_jobs WHERE "
                "((status IN ('queued','retry') AND run_after<=?) OR "
                "(status='running' AND lease_until<=?)) "
                "ORDER BY priority ASC, created_at ASC LIMIT 1", (current, current)
            ).fetchone()
            if row is None:
                self.conn.commit()
                return None
            names = [c[0] for c in self.conn.execute("SELECT * FROM background_jobs LIMIT 0").description]
            job = dict(zip(names, row))
            job["attempts"] = int(job["attempts"]) + 1
            self.conn.execute(
                "UPDATE background_jobs SET status='running', attempts=?, lease_until=?, updated_at=? WHERE id=?",
                (job["attempts"], current + lease_seconds, current, job["id"]),
            )
            self.conn.commit()
        return job, current

    def _finish(self, job: dict[str, Any], current: float, error: Exception | None = None) -> None:
        with self.lock:
            if error is not None:
                attempts = int(job["attempts"])
                terminal = attempts >= self.max_attempts
                delay = min(3600, 2 ** min(attempts, 10))
                self.conn.execute(
                    "UPDATE background_jobs SET status=?,run_after=?,lease_until=0,last_error=?,updated_at=? WHERE id=?",
                    ("failed" if terminal else "retry", current + delay,
                     f"{type(error).__name__}: {error}"[:500], current, job["id"]),
                )
            else:
                self.conn.execute(
                    "UPDATE background_jobs SET status='done',lease_until=0,last_error='',updated_at=? WHERE id=?",
                    (current, job["id"]),
                )
            self.conn.commit()

    async def run_one_async(
        self, handlers: dict[str, Callable[[dict[str, Any]], Any]], *,
        now: float | None = None, active_calls: int = 0, lease_seconds: float = 120,
    ) -> bool:
        """Await one handler without holding the connection lock across network work."""
        claimed = self._claim(now=now, active_calls=active_calls, lease_seconds=lease_seconds)
        if claimed is None:
            return False
        job, current = claimed
        try:
            handler = handlers.get(str(job["kind"]))
            if handler is None:
                raise KeyError(f"No handler registered for job kind {job['kind']}")
            result = handler(json.loads(job["payload"]))
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            self._finish(job, current, RuntimeError("maintenance_interrupted"))
            raise
        except Exception as exc:
            self._finish(job, current, exc)
        else:
            self._finish(job, current)
        return True

    def run_one(
        self, handlers: dict[str, Callable[[dict[str, Any]], Any]], *,
        now: float | None = None, active_calls: int = 0, call_budget: int = 1,
        lease_seconds: float = 120,
    ) -> bool:
        """Claim and execute one job; background work pauses at the call budget."""
        claimed = self._claim(now=now, active_calls=active_calls, call_budget=call_budget,
                              lease_seconds=lease_seconds)
        if claimed is None:
            return False
        job, current = claimed
        try:
            handler = handlers.get(str(job["kind"]))
            if handler is None:
                raise KeyError(f"No handler registered for job kind {job['kind']}")
            result = handler(json.loads(job["payload"]))
            if inspect.isawaitable(result):
                result.close()
                raise TypeError("Use run_one_async for asynchronous handlers")
        except Exception as exc:
            self._finish(job, current, exc)
        else:
            self._finish(job, current)
        return True


async def run_due_jobs(
    conn: sqlite3.Connection,
    handlers: dict[str, Callable[[dict[str, Any]], Any]],
    *, active_calls: int = 0, lock: Any = None,
) -> bool:
    """Integration hook for scheduler ticks; handles at most one low-priority job."""
    return await DurableJobQueue(conn, lock=lock).run_one_async(handlers, active_calls=active_calls)
