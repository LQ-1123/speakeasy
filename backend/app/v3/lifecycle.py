"""生命周期：互动标记 + TTL 清理（24h 可见 / 互动保留 7 天 / 游客人物回收）。"""

from __future__ import annotations

from typing import Any

from app.logging_utils import get_logger
from app.v3.daily_world import (
    POST_KEEP_DAYS,
    iso,
    now_local,
    parse_iso,
)

log = get_logger(__name__)


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


def cleanup(db: Any, *, now_iso: str | None = None) -> dict:
    """TTL 清理（幂等，可随时跑）。

    1. 被互动过的动态：过期后延长到「创建 + 7 天」
    2. 未互动的过期动态：删除
    3. 过期的游客人物：没有互动、也不是任何人的好友 → 删除（连带其动态）
    """
    now = now_iso or iso(now_local())
    keep_seconds = POST_KEEP_DAYS * 24 * 3600
    extended = 0
    deleted_posts = 0
    deleted_personas = 0

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
            "SELECT id FROM personas WHERE lifecycle='guest' AND expires_at<>'' AND expires_at<=?"
            " AND interaction_count=0 AND NOT EXISTS ("
            "   SELECT 1 FROM friendships f WHERE f.persona_id = personas.id"
            " )",
            (now,),
        ).fetchall()
        for row in guests:
            pid = str(row["id"])
            db.conn.execute("DELETE FROM persona_posts WHERE persona_id=?", (pid,))
            db.conn.execute("DELETE FROM personas WHERE id=?", (pid,))
            deleted_personas += 1

    if extended or deleted_posts or deleted_personas:
        log.info(
            "生命周期清理：延长 %d 条、删除动态 %d 条、回收游客人物 %d 个",
            extended, deleted_posts, deleted_personas,
        )
    return {
        "extended_posts": extended,
        "deleted_posts": deleted_posts,
        "deleted_personas": deleted_personas,
    }


__all__ = ["cleanup", "mark_interaction"]
