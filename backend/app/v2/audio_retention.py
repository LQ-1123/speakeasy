"""音频保留策略：总量上限 2 GB，超出按最旧优先删，并清掉 DB 里的引用。

为什么
----
每条语音都会落一个音频文件（edge 的 wav ~316KB，Fish 的 mp3 ~96KB），
之前没有任何清理逻辑 → 只进不出。这里加一个上限，避免长期跑下去把磁盘吃掉。

策略
----
* 上限 2 GB；超过后删到 **1.6 GB**（留 hysteresis，避免每次都在边界反复删）
* 只删 `audio/` 里的音频文件，按 mtime 从旧到新
* 删文件的同时把 messages.audio_path 置空 → 前端不再显示可点的播放按钮，
  但**转写文本/记忆/表达本全部保留**，对话不会丢
* 单次最多删 2000 个文件，避免一次卡太久
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time

log = logging.getLogger(__name__)

MAX_BYTES = 2 * 1024 * 1024 * 1024      # 2 GB 上限
TRIM_TO_BYTES = 1600 * 1024 * 1024      # 触发后降到 1.6 GB
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".ogg", ".flac"}
MAX_DELETE_PER_RUN = 2000


def _audio_files(audio_dir: str) -> list[tuple[str, int, float]]:
    rows: list[tuple[str, int, float]] = []
    try:
        for name in os.listdir(audio_dir):
            if os.path.splitext(name)[1].lower() not in AUDIO_EXT:
                continue
            path = os.path.join(audio_dir, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            rows.append((name, st.st_size, st.st_mtime))
    except FileNotFoundError:
        return []
    return rows


def total_bytes(audio_dir: str) -> int:
    return sum(size for _, size, _ in _audio_files(audio_dir))


def enforce_cap(
    *,
    db_path: str,
    audio_dir: str,
    max_bytes: int = MAX_BYTES,
    trim_to_bytes: int = TRIM_TO_BYTES,
    dry_run: bool = False,
) -> dict:
    """执行一次清理，返回统计。"""
    rows = _audio_files(audio_dir)
    total = sum(size for _, size, _ in rows)
    stats = {
        "before_bytes": total,
        "before_files": len(rows),
        "deleted_files": 0,
        "deleted_bytes": 0,
        "cleared_rows": 0,
        "after_bytes": total,
        "skipped": total <= max_bytes,
        "dry_run": dry_run,
    }
    if total <= max_bytes:
        return stats

    rows.sort(key=lambda r: r[2])  # 最旧的先删
    victims: list[str] = []
    freed = 0
    for name, size, _ in rows:
        if total - freed <= trim_to_bytes or len(victims) >= MAX_DELETE_PER_RUN:
            break
        victims.append(name)
        freed += size

    if not victims:
        return stats
    if dry_run:
        stats["deleted_files"] = len(victims)
        stats["deleted_bytes"] = freed
        stats["after_bytes"] = total - freed
        return stats

    deleted = 0
    freed_actual = 0
    for name in victims:
        path = os.path.join(audio_dir, name)
        try:
            size = os.path.getsize(path)
            os.remove(path)
            deleted += 1
            freed_actual += size
        except OSError:
            continue

    # 清掉 DB 里的引用（audio_path 存的是文件名或完整路径，两种都覆盖）
    cleared = 0
    if victims:
        try:
            conn = sqlite3.connect(db_path, timeout=10.0)
            try:
                marks = ",".join("?" for _ in victims)
                cur = conn.execute(
                    f"UPDATE messages SET audio_path = NULL "
                    f"WHERE audio_path IS NOT NULL AND ("
                    f"audio_path IN ({marks})"
                    + "".join(f" OR audio_path LIKE ?" for _ in victims)
                    + ")",
                    [*victims, *[f"%{v}" for v in victims]],
                )
                cleared = cur.rowcount or 0
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("清理音频时更新 DB 失败: %s", exc)

    stats.update(
        deleted_files=deleted,
        deleted_bytes=freed_actual,
        cleared_rows=cleared,
        after_bytes=total_bytes(audio_dir),
    )
    log.info(
        "音频清理完成: 删除 %d 个文件（%.1f MB），清除 %d 条引用，剩余 %.1f MB",
        deleted,
        freed_actual / 1024 / 1024,
        cleared,
        stats["after_bytes"] / 1024 / 1024,
    )
    return stats


# --------------------------------------------------------------------------- #
# 定时任务
# --------------------------------------------------------------------------- #
async def retention_loop(
    *,
    db_path: str,
    audio_dir: str,
    interval_seconds: float = 3600.0,
    max_bytes: int = MAX_BYTES,
) -> None:
    """启动时跑一次，之后每小时跑一次。异常不退出。"""
    import asyncio  # noqa: PLC0415

    while True:
        try:
            enforce_cap(db_path=db_path, audio_dir=audio_dir, max_bytes=max_bytes)
        except Exception as exc:  # noqa: BLE001
            log.warning("音频清理失败（忽略，下次再试）: %s", exc)
        await asyncio.sleep(max(60.0, interval_seconds))
