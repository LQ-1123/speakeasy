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
import os
import sqlite3
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
    avatar_seed TEXT NOT NULL DEFAULT '',
    avatar_style TEXT NOT NULL DEFAULT '',
    avatar_options TEXT NOT NULL DEFAULT '',
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
    created_at TEXT NOT NULL DEFAULT '',
    lifecycle TEXT NOT NULL DEFAULT 'permanent',
    expires_at TEXT NOT NULL DEFAULT '',
    last_interaction_at TEXT NOT NULL DEFAULT '',
    interaction_count INTEGER NOT NULL DEFAULT 0,
    speech_dna TEXT NOT NULL DEFAULT '{}',
    emotion_state TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_personas_status ON personas(status);

CREATE TABLE IF NOT EXISTS persona_posts (
    id TEXT PRIMARY KEY,
    persona_id TEXT NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    media TEXT NOT NULL DEFAULT '[]',
    tags TEXT NOT NULL DEFAULT '[]',
    language TEXT NOT NULL DEFAULT '',
    visibility TEXT NOT NULL DEFAULT 'public',
    like_count INTEGER NOT NULL DEFAULT 0,
    comment_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL DEFAULT '',
    interacted INTEGER NOT NULL DEFAULT 0,
    interaction_count INTEGER NOT NULL DEFAULT 0,
    post_kind TEXT NOT NULL DEFAULT 'life_text',
    alt_text TEXT NOT NULL DEFAULT '',
    source_ref TEXT NOT NULL DEFAULT '',
    source_event_id TEXT NOT NULL DEFAULT '',
    post_type TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}'
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
    user_id TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    author_persona_id TEXT NOT NULL DEFAULT '',
    author_type TEXT NOT NULL DEFAULT 'user'
);
CREATE INDEX IF NOT EXISTS idx_ppc_post ON persona_post_comments(post_id, created_at);

-- 示例对话：每个 Persona 12 个情境各一组，按需检索注入（不是固定台词）
CREATE TABLE IF NOT EXISTS persona_example_dialogs (
    id TEXT PRIMARY KEY,
    persona_id TEXT NOT NULL,
    scene TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT '',
    user_line TEXT NOT NULL,
    persona_line TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    ordinal INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ped_persona ON persona_example_dialogs(persona_id, scene, ordinal);

-- 人物生活事件：人物自己的生活（与 social_events 的「用户通知」职责不同）
CREATE TABLE IF NOT EXISTS persona_events (
    id TEXT PRIMARY KEY,
    persona_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL DEFAULT '',
    participants TEXT NOT NULL DEFAULT '[]',
    location TEXT NOT NULL DEFAULT '',
    related_personas TEXT NOT NULL DEFAULT '[]',
    event_status TEXT NOT NULL DEFAULT 'planned',
    source_type TEXT NOT NULL DEFAULT 'rule',
    source_reference TEXT NOT NULL DEFAULT '',
    visibility TEXT NOT NULL DEFAULT 'private',
    metadata TEXT NOT NULL DEFAULT '{}',
    dedupe_key TEXT NOT NULL UNIQUE,
    day TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pe_persona ON persona_events(persona_id, start_time DESC);
CREATE INDEX IF NOT EXISTS idx_pe_status ON persona_events(event_status, start_time);
CREATE INDEX IF NOT EXISTS idx_pe_day ON persona_events(day);

-- 人物之间的人际关系（Persona 不能只认识用户）
CREATE TABLE IF NOT EXISTS persona_relationships (
    id TEXT PRIMARY KEY,
    persona_a TEXT NOT NULL,
    persona_b TEXT NOT NULL,
    relation_type TEXT NOT NULL DEFAULT 'friend',
    closeness INTEGER NOT NULL DEFAULT 2,
    shared_events INTEGER NOT NULL DEFAULT 0,
    last_shared_at TEXT NOT NULL DEFAULT '',
    tension INTEGER NOT NULL DEFAULT 0,
    last_conflict_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(persona_a, persona_b)
);

-- 人物作息（规则引擎的基座，不是逐分钟模拟）
CREATE TABLE IF NOT EXISTS persona_schedules (
    persona_id TEXT PRIMARY KEY,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    weekday_blocks TEXT NOT NULL DEFAULT '[]',
    weekend_blocks TEXT NOT NULL DEFAULT '[]',
    work_hours TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

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

-- 开放图库：下载过的图片与授权信息（用于去重与署名）
CREATE TABLE IF NOT EXISTS media_library (
    external_id TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    license TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL DEFAULT '',
    used_count INTEGER NOT NULL DEFAULT 1
);

-- 每日世界：每天 02:00 批量生成，幂等（同一天只生成一次）
CREATE TABLE IF NOT EXISTS daily_world (
    day TEXT PRIMARY KEY,
    generated_at TEXT NOT NULL,
    personas INTEGER NOT NULL DEFAULT 0,
    posts INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT ''
);

-- 偶遇轮换：记录每个用户看过哪些人物，保证「换一个」不会重复
CREATE TABLE IF NOT EXISTS v3_discover_views (
    user_id TEXT NOT NULL,
    persona_id TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (user_id, persona_id)
);
"""

#: ``v3_meta`` 里标记「语料已入库」的键
SEED_KEY = "persona_seed_version"
#: 语料版本：v3 = 种子化头像（Persona 头像由 avatar_seed 生成，不再用 mock 图片）
#: 2026-11-07.1 = Persona 升级：种子人物带上 Speech DNA（结构化表达配置）
SEED_VERSION = "2026-11-07.1"

#: 轻量迁移表：表名 → ((列名, 列定义), ...)。全部幂等，老库直接补列，数据不丢。
_COLUMN_MIGRATIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "personas": (
        ("speech_dna", "TEXT NOT NULL DEFAULT '{}'"),
        ("emotion_state", "TEXT NOT NULL DEFAULT '{}'"),
    ),
    "persona_posts": (
        ("source_event_id", "TEXT NOT NULL DEFAULT ''"),
        ("post_type", "TEXT NOT NULL DEFAULT ''"),
        ("published_at", "TEXT NOT NULL DEFAULT ''"),
        ("metadata", "TEXT NOT NULL DEFAULT '{}'"),
    ),
    "persona_post_comments": (
        ("author_persona_id", "TEXT NOT NULL DEFAULT ''"),
        ("author_type", "TEXT NOT NULL DEFAULT 'user'"),
    ),
    # V2 聊天主体：行为历史 + 反向指向 personas（激活 Persona 生活层）
    "friends": (
        ("recent_behaviors", "TEXT NOT NULL DEFAULT '[]'"),
        ("persona_id", "TEXT NOT NULL DEFAULT ''"),
    ),
    # 第二阶段：人物关系的「共同经历」证据；第三阶段：关系张力
    "persona_relationships": (
        ("shared_events", "INTEGER NOT NULL DEFAULT 0"),
        ("last_shared_at", "TEXT NOT NULL DEFAULT ''"),
        ("tension", "INTEGER NOT NULL DEFAULT 0"),
        ("last_conflict_at", "TEXT NOT NULL DEFAULT ''"),
    ),
}


def _migrate_columns(db: Any, table: str, columns: tuple[tuple[str, str], ...]) -> None:
    """给已存在的表补列（幂等）。表不存在时静默跳过（由调用方决定建表顺序）。"""
    try:
        rows = db.conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:  # pragma: no cover - 表不存在
        return
    existing = {row[1] for row in rows}
    if not existing:
        return
    for name, ddl in columns:
        if name in existing:
            continue
        try:
            db.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            log.info("migrated %s: added %s column", table, name)
        except sqlite3.Error as exc:  # pragma: no cover - 并发/只读库
            log.warning("migrated %s: 加 %s 列失败（忽略）: %s", table, name, exc)


#: 依赖「新增列」的索引：**必须在 _migrate_columns 之后执行**，
#: 否则老库里列还不存在，executescript 会报 "no such column"。
_EXTRA_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_pp_source_event"
    " ON persona_posts(source_event_id)",
    # 事件驱动动态的唯一性：一个事件对同一人物最多一条动态（部分索引，非事件动态不受影响）
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_pp_event_persona"
    " ON persona_posts(source_event_id, persona_id) WHERE source_event_id <> ''",
    # 一个 Persona 对同一条动态最多一条评论（用户评论不受影响）
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_ppc_post_persona"
    " ON persona_post_comments(post_id, author_persona_id) WHERE author_persona_id <> ''",
)


def _ensure_extra_indexes(db: Any) -> None:
    """建依赖新列的索引（幂等；列缺失时跳过而不是让整个迁移失败）。"""
    for statement in _EXTRA_INDEXES:
        try:
            db.conn.execute(statement)
        except sqlite3.Error as exc:  # pragma: no cover - 极端老库
            log.warning("建索引失败（忽略）: %s", exc)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def ensure_schema(db: Any, *, seed: bool = True) -> None:
    """建表（幂等）+ 首次入库 Persona 语料。

    ``seed=False`` 时只建表，由调用方决定世界生成方式（见 ``app.v3.world``）。
    """
    with db._lock, db.conn:  # noqa: SLF001 - 与 DB 层共用连接与锁
        db.conn.executescript(DDL)
        # 轻量迁移：给老的 persona_posts 补 language 列（决定朗读声线）
        cols = {
            row[1]
            for row in db.conn.execute("PRAGMA table_info(persona_posts)").fetchall()
        }
        if cols and "language" not in cols:
            db.conn.execute(
                "ALTER TABLE persona_posts ADD COLUMN language TEXT NOT NULL DEFAULT ''"
            )
            log.info("migrated persona_posts: added language column")

        pcols = {
            row[1]
            for row in db.conn.execute("PRAGMA table_info(personas)").fetchall()
        }
        if pcols and "avatar_seed" not in pcols:
            db.conn.execute(
                "ALTER TABLE personas ADD COLUMN avatar_seed TEXT NOT NULL DEFAULT ''"
            )
            log.info("migrated personas: added avatar_seed column")

        for col in ("lifecycle", "expires_at", "last_interaction_at"):
            if pcols and col not in pcols:
                db.conn.execute(
                    f"ALTER TABLE personas ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                )
                log.info("migrated personas: added %s column", col)
                pcols.add(col)
        if pcols and "fish_voice_id" not in pcols:
            db.conn.execute(
                "ALTER TABLE personas ADD COLUMN fish_voice_id TEXT NOT NULL DEFAULT ''"
            )
            log.info("migrated personas: added fish_voice_id column")
            pcols.add("fish_voice_id")
        if pcols and "interaction_count" not in pcols:
            db.conn.execute(
                "ALTER TABLE personas ADD COLUMN interaction_count INTEGER NOT NULL DEFAULT 0"
            )
            log.info("migrated personas: added interaction_count column")
            pcols.add("interaction_count")

        post_cols = {
            row[1]
            for row in db.conn.execute("PRAGMA table_info(persona_posts)").fetchall()
        }
        for col in ("expires_at",):
            if post_cols and col not in post_cols:
                db.conn.execute(
                    f"ALTER TABLE persona_posts ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                )
                log.info("migrated persona_posts: added %s column", col)
                post_cols.add(col)
        for col in ("post_kind", "alt_text", "source_ref"):
            if post_cols and col not in post_cols:
                default = "'life_text'" if col == "post_kind" else "''"
                db.conn.execute(
                    f"ALTER TABLE persona_posts ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}"
                )
                log.info("migrated persona_posts: added %s column", col)
                post_cols.add(col)

        for col in ("interacted", "interaction_count"):
            if post_cols and col not in post_cols:
                db.conn.execute(
                    f"ALTER TABLE persona_posts ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0"
                )
                log.info("migrated persona_posts: added %s column", col)
                post_cols.add(col)

        for col in ("avatar_style", "avatar_options"):
            if pcols and col not in pcols:
                db.conn.execute(
                    f"ALTER TABLE personas ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                )
                log.info("migrated personas: added %s column", col)
                pcols.add(col)

        # 好友表（V2 建的）也补上，聊天列表才能用同一套 Avataaars 部件
        fcols = {
            row[1]
            for row in db.conn.execute("PRAGMA table_info(friends)").fetchall()
        }
        for col in ("avatar_style", "avatar_options"):
            if fcols and col not in fcols:
                db.conn.execute(
                    f"ALTER TABLE friends ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                )
                log.info("migrated friends: added %s column", col)
                fcols.add(col)

        # Persona 升级：Speech DNA / 情绪状态 / 事件来源 / AI 评论 / 行为历史
        for table, columns in _COLUMN_MIGRATIONS.items():
            _migrate_columns(db, table, columns)
        # 图库：记录图片来源人物（按角色补图/去重/生命周期用）
        _migrate_columns(
            db,
            "media_library",
            (("persona_id", "TEXT NOT NULL DEFAULT ''"),),
        )
        # 依赖新列的索引必须在这里建（DDL 里的 CREATE TABLE IF NOT EXISTS 不会给老表补列）
        _ensure_extra_indexes(db)
        # 老库的 lifecycle 是「ALTER TABLE 加列」产生的 DEFAULT ''，
        # 而新表默认是 'permanent'。统一一下，否则「常驻人物」过滤器找不到人。
        try:
            cur = db.conn.execute(
                "UPDATE personas SET lifecycle='permanent'"
                " WHERE COALESCE(lifecycle, '') = ''"
            )
            if cur.rowcount:
                log.info("migrated personas: %d 行 lifecycle '' → permanent", cur.rowcount)
        except sqlite3.Error as exc:  # pragma: no cover
            log.debug("lifecycle 归一化失败（忽略）: %s", exc)
    if seed:
        seed_if_needed(db)


def seed_if_needed(db: Any, *, force: bool = False) -> int:
    """把 ``seed_data`` 里的人物与动态写入库（幂等，可重复执行）。

    - 空库 / 语料版本变化 / ``force`` 时会写入
    - 已有内容按主键 upsert：**不会重复插入**，但会把人物语言和动态文案更新到最新版本
      （这样老的英文世界可以平滑升级成「母语世界」，时间线保持不变）

    返回**新增**的 post 条数（0 表示没有新行）。
    """
    with db._lock, db.conn:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT value FROM v3_meta WHERE key=?", (SEED_KEY,)
        ).fetchone()
        already = str(row["value"]) if row is not None else ""
        count = db.conn.execute("SELECT COUNT(*) AS n FROM personas").fetchone()["n"]
        # 已经有世界且版本一致 → 什么都不做
        if not force and count and already == SEED_VERSION:
            return 0

        now = _now_utc()
        for p in PERSONAS:
            speech_dna = _json(p.get("speech_dna") or {})
            db.conn.execute(
                "INSERT INTO personas (id, nickname, avatar, avatar_seed, avatar_style,"
                " bio, personality, interests, language, country, city, timezone, occupation,"
                " age, gender, voice_id, archetype_id, ai_config, status, created_at, speech_dna)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET nickname=excluded.nickname,"
                " avatar=excluded.avatar, avatar_seed=excluded.avatar_seed,"
                " avatar_style=excluded.avatar_style,"
                " bio=excluded.bio, personality=excluded.personality, interests=excluded.interests,"
                " language=excluded.language, country=excluded.country, city=excluded.city,"
                " timezone=excluded.timezone, occupation=excluded.occupation, age=excluded.age,"
                " gender=excluded.gender,"
                " voice_id=CASE WHEN personas.voice_id<>'' THEN personas.voice_id ELSE excluded.voice_id END,"
                " archetype_id=excluded.archetype_id, ai_config=excluded.ai_config,"
                " status=personas.status,"
                # DNA 只在语料提供了非空值时才覆盖，保护已生成/已调优的旧数据
                " speech_dna=CASE WHEN excluded.speech_dna NOT IN ('', '{}')"
                " THEN excluded.speech_dna ELSE personas.speech_dna END",
                (
                    p["id"], p["nickname"], "", p.get("avatar_seed", ""), "avataaars",
                    p["bio"], p["personality"],
                    _json(p["interests"]), p["language"], p["country"], p["city"],
                    p["timezone"], p["occupation"], int(p["age"]), p["gender"],
                    p["voice_id"], p["archetype_id"], _json(p["ai_config"]),
                    ("waiting_voice" if os.getenv("SE_VOICE_STRICT", "0").lower() in ("1", "true", "yes") else p.get("status", "active")), _iso(now), speech_dna,
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
            created = now - timedelta(hours=(index * 5) % 120 + 1, minutes=(index * 17) % 60)
            persona_lang = next(
                (str(p.get("language") or "en") for p in PERSONAS if p["id"] == pid), "en"
            )
            if post_id in existing:
                # 已存在：刷新文案/标签/媒体/语言（保留原时间线），用于语料升级
                db.conn.execute(
                    "UPDATE persona_posts SET persona_id=?, content=?, media=?, tags=?, language=?"
                    " WHERE id=?",
                    (pid, content, _json(media), _json(tags), persona_lang, post_id),
                )
                continue
            db.conn.execute(
                "INSERT INTO persona_posts (id, persona_id, content, media, tags, language,"
                " visibility, like_count, comment_count, created_at)"
                " VALUES (?,?,?,?,?,?, 'public', 0, 0, ?)",
                (post_id, pid, content, _json(media), _json(tags), persona_lang, _iso(created)),
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


def backfill_post_languages(db: Any) -> int:
    """给 ``language`` 为空的动态回填语言（老库升级用，幂等）。

    优先级：人物母语 → 文本字符集推断 → ``en``。
    返回回填的行数。
    """
    from app.v3.seed_data import detect_language  # noqa: PLC0415 - 避免循环导入

    with db._lock, db.conn:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT pp.id, pp.content, p.language AS persona_lang FROM persona_posts pp"
            " LEFT JOIN personas p ON p.id = pp.persona_id"
            " WHERE COALESCE(pp.language,'') = ''"
        ).fetchall()
        updated = 0
        for row in rows:
            lang = (
                str(row["persona_lang"] or "").strip().lower()
                or detect_language(str(row["content"] or ""))
                or "en"
            )
            db.conn.execute(
                "UPDATE persona_posts SET language=? WHERE id=?", (lang, row["id"])
            )
            updated += 1
        return updated


def sync_persona_friends(db: Any) -> int:
    """把已加好友的 Persona 语言/声线/**头像元数据**同步成人物当前值（幂等，每次启动都跑）。

    一致性保证：
    - 信息流文字 = 聊天语言 = 语音条声音；
    - 聊天/通讯录（``friends`` 行）= 信息流/主页/通知（persona）**同一张脸**：
      ``avatar_seed / avatar_style / avatar_options`` 与 ``persona_avatar_meta`` 完全一致。
      缺失部件的老人物在读取时用确定性 ``default_options`` 归一化（不调 LLM），
      这里把同一份结果写回好友行，客户端 ``resolveAvatar`` 才能画出同一个头像。
    返回受影响的好友行数（老库没有 friendships 表时返回 0）。
    """
    with db._lock, db.conn:  # noqa: SLF001
        try:
            from app.v3.avatar import persona_avatar_url  # noqa: PLC0415
            from app.v3.avatar_style import persona_avatar_meta  # noqa: PLC0415

            # Persona 生活层外键：老库里的好友行也补上 persona_id（幂等）
            db.conn.execute(
                "UPDATE friends SET persona_id = ("
                "  SELECT f.persona_id FROM friendships f WHERE f.friend_id = friends.id LIMIT 1"
                ") WHERE COALESCE(persona_id, '') = '' AND EXISTS ("
                "  SELECT 1 FROM friendships f WHERE f.friend_id = friends.id)"
            )
            cur = db.conn.execute(
                """
                UPDATE friends SET
                    reply_language = (
                        SELECT p.language FROM friendships f
                        JOIN personas p ON p.id = f.persona_id
                        WHERE f.friend_id = friends.id LIMIT 1
                    ),
                    native_lang = (
                        SELECT p.language FROM friendships f
                        JOIN personas p ON p.id = f.persona_id
                        WHERE f.friend_id = friends.id LIMIT 1
                    ),
                    voice_id = (
                        SELECT p.voice_id FROM friendships f
                        JOIN personas p ON p.id = f.persona_id
                        WHERE f.friend_id = friends.id LIMIT 1
                    ),
                    fish_voice_id = (
                        SELECT p.fish_voice_id FROM friendships f
                        JOIN personas p ON p.id = f.persona_id
                        WHERE f.friend_id = friends.id LIMIT 1
                    )
                WHERE EXISTS (
                    SELECT 1 FROM friendships f WHERE f.friend_id = friends.id
                )
                """
            )
            # 老人物先补确定性种子（与 backfill_avatar_seeds 同规则），好友行才能复制到同一个 seed。
            # 放在同步好友之前，避免「world 启动时先 sync 后 backfill」导致好友行拿到空种子。
            for persona_row in db.conn.execute(
                "SELECT id FROM personas WHERE COALESCE(avatar_seed,'') = ''"
            ).fetchall():
                pid = str(persona_row["id"])
                seed = "persona-" + (pid[2:] if pid.startswith("p_") else pid)
                db.conn.execute(
                    "UPDATE personas SET avatar_seed=?, avatar='' WHERE id=?", (seed, pid)
                )
            rows = db.conn.execute(
                "SELECT f.friend_id AS friend_id, p.* FROM friendships f"
                " JOIN personas p ON p.id = f.persona_id"
            ).fetchall()
            for row in rows:
                persona = dict(row)
                friend_id = str(row["friend_id"])
                avatar_meta = persona_avatar_meta(persona)
                db.conn.execute(
                    "UPDATE friends SET avatar_url=?, avatar_seed=?, avatar_style=?, avatar_options=?"
                    " WHERE id=?",
                    (
                        persona_avatar_url(persona),
                        str(persona.get("avatar_seed") or "").strip()
                        or str(persona.get("id") or ""),
                        str(avatar_meta.get("avatar_style") or ""),
                        _json(avatar_meta.get("avatar_options") or {}),
                        friend_id,
                    ),
                )
            return int(cur.rowcount or 0)
        except sqlite3.Error as exc:  # pragma: no cover - 老库可能没有 friendships 表
            log.debug("同步 Persona 好友语言失败（忽略）: %s", exc)
            return 0


def backfill_avatar_seeds(db: Any) -> int:
    """给没有 avatar_seed 的 Persona 回填种子（老库/LLM 生成的人物，幂等）。"""
    with db._lock, db.conn:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT id, avatar_seed FROM personas WHERE COALESCE(avatar_seed,'') = ''"
        ).fetchall()
        for row in rows:
            pid = str(row["id"])
            seed = "persona-" + (pid[2:] if pid.startswith("p_") else pid)
            db.conn.execute(
                "UPDATE personas SET avatar_seed=?, avatar='' WHERE id=?", (seed, pid)
            )
        return len(rows)


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


async def ensure_schema_async(db: Any) -> None:
    await asyncio.to_thread(ensure_schema, db)


__all__ = ["DDL", "SEED_VERSION", "ensure_schema", "ensure_schema_async", "seed_if_needed"]
