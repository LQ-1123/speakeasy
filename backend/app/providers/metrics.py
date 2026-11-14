"""Batched, bounded provider diagnostics; never used as quota authority."""
from __future__ import annotations

import asyncio
import math

from app.jobs import run_db_thread


def ensure_schema(store):
    with store._lock, store.conn:
        store.conn.execute('''CREATE TABLE IF NOT EXISTS provider_metric_snapshots (
            resource_id TEXT PRIMARY KEY, at INTEGER NOT NULL, revision INTEGER NOT NULL,
            ewma REAL NOT NULL, completed INTEGER NOT NULL, errors INTEGER NOT NULL
        )''')


def write_snapshots(store, snapshots):
    """One allowlisted row/resource; ignore stale revisions and observations."""
    values = []
    for row in snapshots:
        ewma = float(row['ewma'])
        if not math.isfinite(ewma) or ewma <= 0:
            continue
        values.append((str(row['resource_id']), max(0, int(row['at'])), int(row['revision']),
                       ewma, max(0, int(row['completed'])), max(0, int(row['errors']))))
    with store._lock, store.conn:
        store.conn.executemany('''
            INSERT INTO provider_metric_snapshots(resource_id,at,revision,ewma,completed,errors)
            SELECT ?,?,?,?,?,? WHERE EXISTS (
                SELECT 1 FROM provider_resources WHERE id=? AND revision=? AND deleted=0
            )
            ON CONFLICT(resource_id) DO UPDATE SET at=excluded.at,revision=excluded.revision,
                ewma=excluded.ewma,completed=excluded.completed,errors=excluded.errors
            WHERE excluded.revision>=provider_metric_snapshots.revision
                AND excluded.at>=provider_metric_snapshots.at
        ''', [(*row, row[0], row[2]) for row in values])
        store.conn.execute('DELETE FROM provider_metric_snapshots WHERE NOT EXISTS '
                           '(SELECT 1 FROM provider_resources WHERE id=resource_id AND deleted=0)')
    return len(values)


async def flush_metrics(pool):
    return await run_db_thread(write_snapshots, pool.store, pool.metric_snapshots())


async def periodic_flush(pool, *, interval=10.0):
    """Application-owned optional task; cancellation drains any started DB stage."""
    interval = max(.01, float(interval))
    while not pool.closed:
        await asyncio.sleep(interval)
        if not pool.closed:
            await pool.flush_metrics()
