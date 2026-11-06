"""SpeakEasy V3 · 数据库结构（与 V2 同一个 SQLite 文件）。

约定
----
- 只用 ``CREATE TABLE IF NOT EXISTS`` + 幂等的 ``ALTER TABLE``：可以反复执行。
- 公共内容：``personas`` / ``persona_posts``（全用户共享，一次入库，长期复用）
- 用户内容：``user_posts`` / ``user_post_likes`` / 评论
- 关系与事件：``friendships`` / ``social_events``
- 身份：``v3_users``（设备身份，token 只存哈希）
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.logging_utils import get_logger
from app.v3.seed_data import PERSONAS, POSTS

log = get_logger(__name__)

#: 全部 DDL（幂等）
DDL = """
CREATE TABLE IF NOT EXISTS v3_users (
    id TEXT PRIMARY KEY,
    device_key TEXT UNIQUE,
    token_hash TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    last_seen_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS personas (
    id TEXT PRIMARY KEY,
    nickname TEXT NOT NULL,
    avatar TEXT NOT NULL DEFAULT '',
    bio TEXT NOT NULL DEFAULT '',
    personality TEXT NOT NULL DEFAULT '',
    interests TEXT NOT NULL DEFAULT '[]',
    language TEXT NOT NULL DEFAULT 'en',
    country TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    timezone TEXT NOT NULL DEFAULT 'UTC',
    occupation TEXT NOT NULL DEFAULT '',
    age INTEGER NOT NULL DEFAULT 25,
    gender TEXT NOT NULL DEFAULT '',
    voice_id TEXT NOT NULL DEFAULT '',
    archetype_id TEXT NOT NULL DEFAULT '',
    ai_config TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_personas_status ON personas(status);

CREATE TABLE IF NOT EXISTS persona_posts (
    id TEXT PRIMARY KEY,
    persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    media TEXT NOT NULL DEFAULT '[]',
    tags TEXT NOT NULL DEFAULT '[]',
    visibility TEXT NOT NULL DEFAULT 'public',
    like_count INTEGER NOT NULL DEFAULT 0,
    comment_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_persona_posts_time ON persona_posts(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_persona_posts_persona ON persona_posts(persona_id, created_at DESC);

CREATE TABLE IF NOT EXISTS persona_post_likes (
    id TEXT PRIMARY KEY,
    post_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(post_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_ppl_post ON persona_post_likes(post_id);

CREATE TABLE IF NOT EXISTS persona_post_comments (
    id TEXT PRIMARY KEY,
    post_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ppc_post ON persona_post_comments(post_id, created_at);

CREATE TABLE IF NOT EXISTS friendships (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'accepted',
    friend_id TEXT,
    request_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, persona_id)
);
CREATE INDEX IF NOT EXISTS idx_friendships_user ON friendships(user_id, status);

CREATE TABLE IF NOT EXISTS user_posts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    author_name TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    media TEXT NOT NULL DEFAULT '[]',
    tags TEXT NOT NULL DEFAULT '[]',
    visibility TEXT NOT NULL DEFAULT 'public',
    like_count INTEGER NOT NULL DEFAULT 0,
    comment_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_posts_time ON user_posts(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_user_posts_user ON user_posts(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS user_post_likes (
    id TEXT PRIMARY KEY,
    post_id TEXT NOT NULL,
    persona_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(post_id, persona_id)
);
CREATE INDEX IF NOT EXISTS idx_upl_post ON user_post_likes(post_id);

CREATE TABLE IF NOT EXISTS user_post_comments (
    id TEXT PRIMARY KEY,
    post_id TEXT NOT NULL,
    persona_id TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_upc_post ON user_post_comments(post_id, created_at);

CREATE TABLE IF NOT EXISTS social_events (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    persona_id TEXT NOT NULL,
    post_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    is_read INTEGER NOT NULL DEFAULT 0,
    dedupe_key TEXT NOT NULL UNIQUE,
    scheduled_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    processed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_social_events_user ON social_events(user_id, scheduled_at DESC);
CREATE INDEX IF NOT EXISTS idx_social_events_pending ON social_events(user_id, status, scheduled_at);

CREATE TABLE IF NOT EXISTS social_event_attempts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    post_id TEXT NOT NULL DEFAULT '',
    persona_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL DEFAULT 'no_action',
    reason TEXT NOT NULL DEFAULT '',
    roll REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_user ON social_event_attempts(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS v3_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""

#: ``v3_meta`` 里标记「语料已入库」的键
SEED_KEY = "persona_seed_version"
SEED_VERSION = "2026-11-06.1"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def ensure_schema(db: Any) -> None:
    """建表（幂等）+ 首次入库 Persona 语料。"""
    with db._lock, db.conn:  # noqa: SLF001 - 与 DB 层共用连接与锁
        db.conn.executescript(DDL)
    seed_if_needed(db)


def seed_if_needed(db: Any, *, force: bool = False) -> int:
    """把 ``seed_data`` 里的人物与动态写入库（只在为空/版本变化时执行）。

    返回写入的 post 条数（0 表示跳过）。
    """
    with db._lock, db.conn:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT value FROM v3_meta WHERE key=?", (SEED_KEY,)
        ).fetchone()
        already = str(row["value"]) if row is not None else ""
        count = db.conn.execute("SELECT COUNT(*) AS n FROM personas").fetchone()["n"]
        if not force and count and already == SEED_VERSION:
            return 0

        now = _now_utc()
        for p in PERSONAS:
            db.conn.execute(
                "INSERT INTO personas (id, nickname, avatar, bio, personality, interests,"
                " language, country, city, timezone, occupation, age, gender, voice_id,"
                " archetype_id, ai_config, status, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET nickname=excluded.nickname, avatar=excluded.avatar,"
                " bio=excluded.bio, personality=excluded.personality, interests=excluded.interests,"
                " language=excluded.language, country=excluded.country, city=excluded.city,"
                " timezone=excluded.timezone, occupation=excluded.occupation, age=excluded.age,"
                " gender=excluded.gender, voice_id=excluded.voice_id,"
                " archetype_id=excluded.archetype_id, ai_config=excluded.ai_config,"
                " status=excluded.status",
                (
                    p["id"], p["nickname"], p["avatar"], p["bio"], p["personality"],
                    _json(p["interests"]), p["language"], p["country"], p["city"],
                    p["timezone"], p["occupation"], int(p["age"]), p["gender"],
                    p["voice_id"], p["archetype_id"], _json(p["ai_config"]),
                    p.get("status", "active"), _iso(now),
                ),
            )

        written = 0
        existing = {
            str(r["id"])
            for r in db.conn.execute("SELECT id FROM persona_posts").fetchall()
        }
        # 时间错开：越靠前的越新（第一条约 1 小时前），制造自然的信息流节奏
        for index, (pid, content, tags, media) in enumerate(POSTS):
            post_id = f"pp_{pid[2:]}_{index:02d}"
            if post_id in existing:
                continue
            created = now - timedelta(hours=(index * 5) % 120 + 1, minutes=(index * 17) % 60)
            db.conn.execute(
                "INSERT INTO persona_posts (id, persona_id, content, media, tags, visibility,"
                " like_count, comment_count, created_at)"
                " VALUES (?,?,?,?,?, 'public', 0, 0, ?)",
                (post_id, pid, content, _json(media), _json(tags), _iso(created)),
            )
            written += 1

        db.conn.execute(
            "INSERT INTO v3_meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SEED_KEY, SEED_VERSION),
        )
    if written:
        log.info("v3 语料入库：%d 个人物 / %d 条动态", len(PERSONAS), written)
    return written


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


async def ensure_schema_async(db: Any) -> None:
    await asyncio.to_thread(ensure_schema, db)


__all__ = ["DDL", "SEED_VERSION", "ensure_schema", "ensure_schema_async", "seed_if_needed"]
