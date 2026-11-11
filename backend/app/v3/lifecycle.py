"""生命周期：互动标记 + TTL 清理（24h 可见 / 互动保留 7 天 / 游客人物回收 / 批次账本过期）。"""

from __future__ import annotations

import os
import sqlite3
from datetime import timedelta
from typing import Any

from app.logging_utils import get_logger
from app.v3.daily_world import (
    POST_KEEP_DAYS,
    iso,
    now_local,
    parse_iso,
    prune_batch_markers,
)

log = get_logger(__name__)

#: ``daily_world`` 批次账本保留天数：再早的批次已经没有任何行为会引用它
DAILY_WORLD_KEEP_DAYS = max(1, int(os.getenv("SE_DAILY_WORLD_KEEP_DAYS", "30")))


def mark_interaction(db: Any, *, persona_id: str = "", post_id: str = "") -> None:
    """记录一次真实互动（点赞/评论/加好友/看主页/朗读/翻译）。

    - 动态：interacted=1、interaction_count+1 → TTL 从 24h 延长到 7 天
    - 人物：interaction_count+1、last_interaction_at → 游客人物「转正」，不再回收
    """
    if not persona_id and not post_id:
        return
    now_iso = iso(now_local())
    with db._lock, db.conn:  # noqa: SLF001
        if post_id:
            row = db.conn.execute(
                "SELECT persona_id FROM persona_posts WHERE id=?", (post_id,)
            ).fetchone()
            if row is not None:
                persona_id = persona_id or str(row["persona_id"])
                db.conn.execute(
                    "UPDATE persona_posts SET interacted=1, interaction_count=interaction_count+1"
                    " WHERE id=?",
                    (post_id,),
                )
        if persona_id:
            db.conn.execute(
                "UPDATE personas SET interaction_count=interaction_count+1,"
                " last_interaction_at=?, lifecycle=CASE WHEN lifecycle='guest' THEN 'adopted'"
                " ELSE lifecycle END WHERE id=?",
                (now_iso, persona_id),
            )


def _table_exists(db: Any, name: str) -> bool:
    return db.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def cleanup(db: Any, *, now_iso: str | None = None) -> dict:
    """TTL 清理（幂等，可随时跑）。

    1. 被互动过的动态：过期后延长到「创建 + 7 天」
    2. 未互动的过期动态：删除
    3. 过期的游客人物：没有互动、也不是任何人的好友 → 删除（连带其动态）。
       **不看 ``status``**：``waiting_voice``（还没分配到声线）同样做最终回收，
       否则这些人物会永远卡在信息流之外。
    4. 超过 ``DAILY_WORLD_KEEP_DAYS``（默认 30）天的 ``daily_world`` 批次账本：删除
    5. 孤儿动态：``persona_id`` 已不存在于 ``personas`` 的 ``persona_posts``：删除
       （老库/手工删人/外键关闭时期留下的残留）
    """
    now = now_iso or iso(now_local())
    keep_seconds = POST_KEEP_DAYS * 24 * 3600
    extended = 0
    deleted_posts = 0
    deleted_personas = 0
    recycled_waiting_voice = 0
    deleted_orphan_posts = 0
    deleted_daily_world = 0
    deleted_batch_markers = 0

    with db._lock, db.conn:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT id, created_at, expires_at FROM persona_posts"
            " WHERE interacted=1 AND expires_at<>'' AND expires_at<=?",
            (now,),
        ).fetchall()
        for row in rows:
            created = parse_iso(str(row["created_at"]))
            if created is None:
                continue
            keep_until_ts = created.timestamp() + keep_seconds
            if keep_until_ts <= now_local().timestamp():
                continue
            from datetime import datetime, timezone  # noqa: PLC0415

            keep_until = datetime.fromtimestamp(keep_until_ts, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S+00:00"
            )
            db.conn.execute(
                "UPDATE persona_posts SET expires_at=? WHERE id=?",
                (keep_until, row["id"]),
            )
            extended += 1

        cur = db.conn.execute(
            "DELETE FROM persona_posts WHERE expires_at<>'' AND expires_at<=? AND interacted=0",
            (now,),
        )
        deleted_posts += int(cur.rowcount or 0)

        guests = db.conn.execute(
            "SELECT id, status FROM personas WHERE lifecycle='guest' AND expires_at<>''"
            " AND expires_at<=?"
            " AND interaction_count=0 AND NOT EXISTS ("
            "   SELECT 1 FROM friendships f WHERE f.persona_id = personas.id"
            " )",
            (now,),
        ).fetchall()
        for row in guests:
            pid = str(row["id"])
            if str(row["status"] or "") == "waiting_voice":
                recycled_waiting_voice += 1
            db.conn.execute("DELETE FROM persona_posts WHERE persona_id=?", (pid,))
            db.conn.execute("DELETE FROM personas WHERE id=?", (pid,))
            deleted_personas += 1

        # 孤儿动态：人物已不存在的动态（外键开启的新库通常为 0，老库可能有残留）
        cur = db.conn.execute(
            "DELETE FROM persona_posts WHERE NOT EXISTS ("
            " SELECT 1 FROM personas p WHERE p.id = persona_posts.persona_id)"
        )
        deleted_orphan_posts = int(cur.rowcount or 0)

        # 批次账本 + 中断标记：超过保留期就删（不再可能重试/引用）
        # day 键是本地日期（daily_world.day_key），所以截止日也要按本地时区算，
        # 否则 UTC 与本地跨日时会差一天。
        base = parse_iso(now) or now_local()
        local_tz = now_local().tzinfo
        if local_tz is not None and base.tzinfo is not None:
            base = base.astimezone(local_tz)
        cutoff_day = (base - timedelta(days=DAILY_WORLD_KEEP_DAYS)).date().isoformat()
        if _table_exists(db, "daily_world"):
            try:
                cur = db.conn.execute(
                    "DELETE FROM daily_world WHERE day<>'' AND day<?", (cutoff_day,)
                )
                deleted_daily_world = int(cur.rowcount or 0)
            except sqlite3.Error as exc:  # pragma: no cover - 最小库/老库
                log.debug("清理 daily_world 失败（忽略）: %s", exc)
        if _table_exists(db, "v3_meta"):
            # 中断标记同样过期：旧批次不会再被重试，留着只会拖长 v3_meta
            deleted_batch_markers = prune_batch_markers(db, before_day=cutoff_day)

    if (
        extended
        or deleted_posts
        or deleted_personas
        or deleted_orphan_posts
        or deleted_daily_world
        or deleted_batch_markers
    ):
        log.info(
            "生命周期清理：延长 %d 条、删除动态 %d 条、回收游客人物 %d 个（waiting_voice %d）、"
            "孤儿动态 %d 条、过期批次 %d 行、过期批次标记 %d 个",
            extended, deleted_posts, deleted_personas, recycled_waiting_voice,
            deleted_orphan_posts, deleted_daily_world, deleted_batch_markers,
        )
    return {
        "extended_posts": extended,
        "deleted_posts": deleted_posts,
        "deleted_personas": deleted_personas,
        "recycled_waiting_voice": recycled_waiting_voice,
        "deleted_orphan_posts": deleted_orphan_posts,
        "deleted_daily_world": deleted_daily_world,
        "deleted_batch_markers": deleted_batch_markers,
    }


__all__ = ["DAILY_WORLD_KEEP_DAYS", "cleanup", "mark_interaction"]
