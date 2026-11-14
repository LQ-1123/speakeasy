"""SpeakEasy 2.0 · SQLite 数据层。

设计
----
- 标准库 ``sqlite3``（不引入 aiosqlite），单连接 + :class:`threading.RLock`；
  API 层用 ``asyncio.to_thread`` 调用这些**同步**方法。
- 5 张表严格对应 PRD §4：``user_profile`` / ``friends`` / ``messages`` /
  ``moments`` / ``saved_expressions``。JSON 列读写自动编解码。
- 时间统一 UTC ISO8601 字符串；展示层再按好友时区转换。

注意
----
- 不在此层做业务判断（熟稔度、记忆提炼、朋友圈），只做持久化。
- 所有方法可被多线程调用；写操作持锁，读也持锁（本地单用户，简单优先）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from app.logging_utils import get_logger
from app.v3.identity import LOCAL_USER_ID, current_user_id

log = get_logger(__name__)

#: 每张表里需要 JSON 编解码的列
_JSON_COLUMNS: dict[str, tuple[str, ...]] = {
    "user_profile": ("target_languages", "fact_sheet"),
    "friends": ("speech_quirks", "shared_episodes", "open_loops"),
    "messages": ("suggestion",),
    "moments": (),
    "saved_expressions": (),
}

_DEFAULT_PROFILE: dict[str, Any] = {
    "nickname": "Traveler",
    "native_language": "zh-CN",
    "target_languages": ["en"],
    #: 首次建档（Onboarding）完成时间；空字符串 = 尚未完成。必须始终存在于兜底 dict，
    #: 前端（bootstrap / GET /api/v3/profile）据此决定是否展示引导。
    "profile_setup_completed_at": "",
    "fact_sheet": {
        "profile": {
            "nickname": "Traveler",
            "city": "",
            "major_or_job": "",
            "routine": "",
        },
        "interests": [],
        "language_profile": {"target": "en", "weaknesses": []},
        "recent_events": [],
    },
}

_FRIEND_DEFAULTS: dict[str, Any] = {
    "owner_user_id": LOCAL_USER_ID,
    "avatar_seed": "",
    "avatar_url": "",
    "voice_rate": "+0%",
    "voice_pitch": "+0Hz",
    "fish_voice_id": "",
    "gender": "",
    "language_mix_mode": "en_only",
    "reply_language": "en",
    "age": 25,
    "occupation": "",
    "personality_prompt": "",
    "speech_quirks": [],
    "bio": "",
    "status": "active",
    "referred_by_friend_id": None,
    "request_message": "",
    "closeness_level": 1,
    "total_turns": 0,
    "shared_episodes": [],
    "open_loops": [],
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _uuid() -> str:
    return uuid.uuid4().hex


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_loads(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _sqlite_synchronous() -> str:
    """WAL 下的落盘策略（``SE_SQLITE_SYNCHRONOUS``，默认 ``NORMAL``）。

    SQLite 默认 ``FULL``：**每次提交都 fsync**。一轮聊天有十来次提交，云盘上一次
    fsync 是毫秒级，而且 fsync 期间整库的锁都被占着——并发一高，所有请求都在排队等
    磁盘。WAL + ``NORMAL`` 只在 checkpoint 时 fsync：进程崩溃不丢数据、库文件不会损坏，
    只有整机断电 / 内核崩溃才可能丢最后几笔提交（聊天场景可接受，客户端幂等键可重发）。
    需要逐笔落盘时设 ``SE_SQLITE_SYNCHRONOUS=FULL``。
    """
    value = str(os.getenv("SE_SQLITE_SYNCHRONOUS", "NORMAL")).strip().upper()
    return value if value in ("OFF", "NORMAL", "FULL", "EXTRA") else "NORMAL"


def _sqlite_cache_kb() -> int:
    """页缓存大小（KB，``SE_SQLITE_CACHE_KB``，默认 16MB；SQLite 自带默认只有 2MB）。"""
    try:
        value = int(os.getenv("SE_SQLITE_CACHE_KB", "16000"))
    except ValueError:
        value = 16000
    return max(2000, min(value, 262144))


def legacy_shared_enabled() -> bool:
    """老库单用户兼容开关：owner='local' 的历史数据对当前身份只读可见。

    默认关闭（多用户隔离优先）；部署旧库的单用户服务器时设置
    ``SE_LEGACY_SHARED_OWNER=1`` 打开。
    """
    from app.v3.auth_policy import email_code_auth_enabled
    if email_code_auth_enabled():
        return False
    return str(os.getenv("SE_LEGACY_SHARED_OWNER", "0")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class DB:
    """SQLite 持久层（同步方法）。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("DB 尚未 init()")
        return self._conn

    def init(self) -> None:
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(f"PRAGMA synchronous={_sqlite_synchronous()}")
        conn.execute(f"PRAGMA cache_size=-{_sqlite_cache_kb()}")
        # 排序 / 分组的临时结构放内存；WAL 文件在 checkpoint 后截到 64MB 以内
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA journal_size_limit=67108864")
        self._conn = conn
        with self._lock, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS user_profile (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    nickname TEXT DEFAULT 'Traveler',
                    native_language TEXT DEFAULT 'zh-CN',
                    target_languages JSON DEFAULT '["en"]',
                    birthday TEXT DEFAULT '',
                    fact_sheet JSON NOT NULL DEFAULT '{}',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    profile_setup_completed_at TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id TEXT PRIMARY KEY,
                    nickname TEXT DEFAULT 'Traveler',
                    native_language TEXT DEFAULT 'zh-CN',
                    target_languages JSON DEFAULT '["en"]',
                    birthday TEXT DEFAULT '',
                    fact_sheet JSON NOT NULL DEFAULT '{}',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    profile_setup_completed_at TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS friends (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    avatar_seed TEXT NOT NULL DEFAULT '',
                    avatar_url TEXT DEFAULT '',
                    archetype_id TEXT NOT NULL DEFAULT '',
                    voice_id TEXT NOT NULL DEFAULT '',
                    voice_rate TEXT DEFAULT '+0%',
                    voice_pitch TEXT DEFAULT '+0Hz',
                    nationality TEXT NOT NULL DEFAULT '',
                    city TEXT NOT NULL DEFAULT '',
                    timezone TEXT NOT NULL DEFAULT 'UTC',
                    native_lang TEXT NOT NULL DEFAULT 'en',
                    language_mix_mode TEXT DEFAULT 'en_only',
                    reply_language TEXT DEFAULT 'en',
                    age INTEGER DEFAULT 25,
                    occupation TEXT DEFAULT '',
                    personality_prompt TEXT NOT NULL DEFAULT '',
                    speech_quirks JSON DEFAULT '[]',
                    bio TEXT DEFAULT '',
                    status TEXT DEFAULT 'active',
                    referred_by_friend_id TEXT,
                    request_message TEXT DEFAULT '',
                    closeness_level INTEGER DEFAULT 1,
                    total_turns INTEGER DEFAULT 0,
                    shared_episodes JSON DEFAULT '[]',
                    open_loops JSON DEFAULT '[]',
                    last_interaction_at DATETIME,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    friend_id TEXT NOT NULL REFERENCES friends(id) ON DELETE CASCADE,
                    sender TEXT NOT NULL,
                    msg_type TEXT DEFAULT 'voice',
                    transcript TEXT NOT NULL DEFAULT '',
                    audio_path TEXT,
                    image_path TEXT DEFAULT '',
                    duration_ms INTEGER DEFAULT 0,
                    suggestion JSON DEFAULT NULL,
                    context TEXT,
                    client_message_id TEXT DEFAULT NULL,
                    is_read BOOLEAN DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_messages_friend_time
                    ON messages(friend_id, created_at);

                CREATE TABLE IF NOT EXISTS moments (
                    id TEXT PRIMARY KEY,
                    friend_id TEXT NOT NULL REFERENCES friends(id) ON DELETE CASCADE,
                    content TEXT NOT NULL,
                    mood_tag TEXT,
                    local_time_str TEXT,
                    feed_date TEXT,
                    is_replied BOOLEAN DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_moments_date ON moments(feed_date);

                CREATE TABLE IF NOT EXISTS user_moments (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    mood_tag TEXT DEFAULT '',
                    image_path TEXT DEFAULT '',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS moment_comments (
                    id TEXT PRIMARY KEY,
                    moment_id TEXT NOT NULL REFERENCES user_moments(id) ON DELETE CASCADE,
                    friend_id TEXT,
                    content TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_moment_comments_moment
                    ON moment_comments(moment_id);

                CREATE TABLE IF NOT EXISTS saved_expressions (
                    id TEXT PRIMARY KEY,
                    original_text TEXT,
                    better_expression TEXT NOT NULL,
                    explanation TEXT,
                    source_friend_id TEXT,
                    mastery_count INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'learning',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            # 轻量迁移：老 DB 补 reply_language 列（v2.1 多语言好友）
            columns = {row[1] for row in conn.execute("PRAGMA table_info(friends)").fetchall()}
            if "reply_language" not in columns:
                conn.execute("ALTER TABLE friends ADD COLUMN reply_language TEXT DEFAULT 'en'")
                log.info("migrated friends table: added reply_language column")
            if "fish_voice_id" not in columns:
                conn.execute("ALTER TABLE friends ADD COLUMN fish_voice_id TEXT DEFAULT ''")


            if "gender" not in columns:
                conn.execute(
                    "ALTER TABLE friends ADD COLUMN gender TEXT DEFAULT ''"
                )
                log.info("migrated friends table: added gender column")
                log.info("migrated friends table: added fish_voice_id column")

            # V3 多用户：好友归属。老数据统一归 local（单用户行为不变）
            if "avatar_url" not in columns:
                conn.execute("ALTER TABLE friends ADD COLUMN avatar_url TEXT DEFAULT ''")
                log.info("migrated friends table: added avatar_url column")
            if "owner_user_id" not in columns:
                conn.execute(
                    "ALTER TABLE friends ADD COLUMN owner_user_id TEXT DEFAULT 'local'"
                )
                log.info("migrated friends table: added owner_user_id column")
            if "voice_pending_status" not in columns:
                conn.execute("ALTER TABLE friends ADD COLUMN voice_pending_status TEXT NOT NULL DEFAULT 'pending_request'")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_friends_owner ON friends(owner_user_id)"
            )

            profile_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(user_profile)").fetchall()
            }
            if profile_columns and "birthday" not in profile_columns:
                conn.execute("ALTER TABLE user_profile ADD COLUMN birthday TEXT DEFAULT ''")
                log.info("migrated user_profile: added birthday column")

            # 首次建档（Onboarding）状态：老库补列 + **一次性**回填。
            #
            # 为什么回填：本功能上线前就已存在的用户不应该被强制重新走引导，
            # 因此迁移时把已有资料的完成时间设为 updated_at（历史用户视为已完成）。
            # 迁移只会在「列缺失 → ALTER」的那一次执行：列已存在时整个分支跳过，
            # 重复启动不会改写任何人的完成时间，也不会把上线后新注册的用户误标完成。
            setup_column_added = False
            for table in ("user_profile", "user_profiles"):
                columns = {
                    row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if columns and "profile_setup_completed_at" not in columns:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN profile_setup_completed_at TEXT DEFAULT ''"
                    )
                    log.info(
                        "migrated %s: added profile_setup_completed_at column", table
                    )
                    setup_column_added = True
            if setup_column_added:
                now = _now_iso()
                # 回填 1：两张资料表里迁移前已存在的行 → 已完成（优先沿用 updated_at）。
                for table in ("user_profile", "user_profiles"):
                    cur = conn.execute(
                        f"UPDATE {table} SET profile_setup_completed_at="
                        " COALESCE(NULLIF(TRIM(COALESCE(updated_at, '')), ''), ?)"
                        " WHERE COALESCE(profile_setup_completed_at, '') = ''",
                        (now,),
                    )
                    log.info(
                        "backfilled %s: %d existing row(s) marked profile-setup-completed",
                        table,
                        int(cur.rowcount or 0),
                    )
                # 回填 2：迁移前已注册的 V3 账号 → 补一行 user_profiles 并标记完成。
                # ``v3_users`` 由 ``app.v3.schema`` 创建，初始化顺序不定（正常流程：
                # build_services → db.init() → main.py ensure_schema()，此时该表已存在），
                # 这里用 sqlite_master 判断：表还不存在就跳过（不致命，下次启动列已存在、
                # 不会再进本分支；这些账号在首次 PUT 资料时会正常建行，不会强制弹引导）。
                has_v3_users = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='v3_users'"
                ).fetchone()
                if has_v3_users is not None:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO user_profiles"
                        " (user_id, nickname, native_language, target_languages, birthday,"
                        "  fact_sheet, updated_at, profile_setup_completed_at)"
                        " SELECT id, ?, ?, ?, '', ?, ?, ? FROM v3_users"
                        " WHERE COALESCE(id, '') <> ''",
                        (
                            _DEFAULT_PROFILE["nickname"],
                            _DEFAULT_PROFILE["native_language"],
                            _json_dumps(_DEFAULT_PROFILE["target_languages"]),
                            "{}",
                            now,
                            now,
                        ),
                    )
                    conn.execute(
                        "UPDATE user_profiles SET profile_setup_completed_at=?"
                        " WHERE COALESCE(profile_setup_completed_at, '') = ''",
                        (now,),
                    )
                    log.info(
                        "backfilled v3_users: %d legacy account(s) marked profile-setup-completed",
                        int(cur.rowcount or 0),
                    )
                else:  # pragma: no cover - 仅在非常规初始化顺序下出现
                    log.info(
                        "skip v3_users onboarding backfill: v3_users not created yet"
                    )

            msg_columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
            if msg_columns and "image_path" not in msg_columns:
                conn.execute("ALTER TABLE messages ADD COLUMN image_path TEXT DEFAULT ''")
                log.info("migrated messages table: added image_path column")
            for column in ("audio_status", "audio_error", "reply_to_message_id"):
                if column not in msg_columns:
                    conn.execute(f"ALTER TABLE messages ADD COLUMN {column} TEXT DEFAULT ''")
            # 幂等发送：客户端消息 id（可为空；为空时唯一索引不生效）
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_reply_to "
                         "ON messages(friend_id,reply_to_message_id) WHERE reply_to_message_id<>''")
            if msg_columns and "client_message_id" not in msg_columns:
                conn.execute("ALTER TABLE messages ADD COLUMN client_message_id TEXT DEFAULT NULL")
                log.info("migrated messages table: added client_message_id column")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_client_id"
                " ON messages(friend_id, client_message_id)"
                " WHERE client_message_id IS NOT NULL"
            )
            # 未读计数 / 轮数统计走索引：否则这两类查询会随聊天记录变长而线性变慢
            # （好友列表、bootstrap、每一轮聊天都会用到）。
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_unread ON messages(friend_id)"
                " WHERE sender='friend' AND is_read=0"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_friend_sender"
                " ON messages(friend_id, sender)"
            )
            self._init_message_counts(conn)

            # V3 多用户：用户动态 / 表达本归属
            um_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(user_moments)").fetchall()
            }
            if um_columns and "user_id" not in um_columns:
                conn.execute(
                    "ALTER TABLE user_moments ADD COLUMN user_id TEXT DEFAULT 'local'"
                )
                log.info("migrated user_moments: added user_id column")
            ex_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(saved_expressions)").fetchall()
            }
            if ex_columns and "user_id" not in ex_columns:
                conn.execute(
                    "ALTER TABLE saved_expressions ADD COLUMN user_id TEXT DEFAULT 'local'"
                )
                log.info("migrated saved_expressions: added user_id column")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_expressions_user_review"
                " ON saved_expressions(user_id,status,mastery_count,created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_moments_user_time"
                " ON user_moments(user_id,created_at DESC)"
            )

            row = conn.execute("SELECT id FROM user_profile WHERE id=1").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO user_profile (id, nickname, native_language, target_languages, fact_sheet, updated_at)"
                    " VALUES (1, ?, ?, ?, ?, ?)",
                    (
                        _DEFAULT_PROFILE["nickname"],
                        _DEFAULT_PROFILE["native_language"],
                        _json_dumps(_DEFAULT_PROFILE["target_languages"]),
                        _json_dumps(_DEFAULT_PROFILE["fact_sheet"]),
                        _now_iso(),
                    ),
                )

    def _init_message_counts(self, conn: sqlite3.Connection) -> None:
        """Calibrate historical user turns once, then maintain with every SQL write."""
        conn.execute("CREATE TABLE IF NOT EXISTS db_migrations (key TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS friend_message_counts ("
            "friend_id TEXT PRIMARY KEY REFERENCES friends(id) ON DELETE CASCADE,"
            "user_turns INTEGER NOT NULL DEFAULT 0)"
        )
        if conn.execute("SELECT 1 FROM db_migrations WHERE key='message_counts_v1'").fetchone() is None:
            conn.execute(
                "INSERT OR REPLACE INTO friend_message_counts(friend_id,user_turns)"
                " SELECT friend_id,COUNT(*) FROM messages WHERE sender='user' GROUP BY friend_id"
            )
            conn.execute("INSERT INTO db_migrations(key) VALUES ('message_counts_v1')")
        for statement in ("""
            CREATE TRIGGER IF NOT EXISTS message_count_insert AFTER INSERT ON messages
            WHEN new.sender='user' BEGIN
                INSERT INTO friend_message_counts(friend_id,user_turns) VALUES(new.friend_id,1)
                ON CONFLICT(friend_id) DO UPDATE SET user_turns=user_turns+1;
            END;
        """, """
            CREATE TRIGGER IF NOT EXISTS message_count_delete AFTER DELETE ON messages
            WHEN old.sender='user' BEGIN
                UPDATE friend_message_counts SET user_turns=MAX(0,user_turns-1) WHERE friend_id=old.friend_id;
            END;
        """, """
            CREATE TRIGGER IF NOT EXISTS message_count_update AFTER UPDATE OF sender,friend_id ON messages BEGIN
                UPDATE friend_message_counts SET user_turns=MAX(0,user_turns-1)
                    WHERE friend_id=old.friend_id AND old.sender='user';
                INSERT INTO friend_message_counts(friend_id,user_turns)
                    SELECT new.friend_id,1 WHERE new.sender='user'
                    ON CONFLICT(friend_id) DO UPDATE SET user_turns=user_turns+1;
            END;
        """):
            conn.execute(statement)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None, table: str) -> dict | None:
        if row is None:
            return None
        data = dict(row)
        for col in _JSON_COLUMNS.get(table, ()):
            default = [] if col in ("speech_quirks", "shared_episodes", "open_loops", "target_languages") else {}
            if col == "suggestion":
                default = None
            data[col] = _json_loads(data.get(col), default)
        return data

    def _fetch_one(self, table: str, where: str, params: Iterable[Any]) -> dict | None:
        with self._lock:
            row = self.conn.execute(f"SELECT * FROM {table} WHERE {where} LIMIT 1", tuple(params)).fetchone()
        return self._row_to_dict(row, table)

    def _fetch_all(self, table: str, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [d for d in (self._row_to_dict(r, table) for r in rows) if d is not None]

    # ------------------------------------------------------------------ #
    # 用户画像
    # ------------------------------------------------------------------ #
    def get_profile(self, *, user_id: str | None = None) -> dict:
        owner = user_id or current_user_id()
        if owner == LOCAL_USER_ID:
            data = self._fetch_one("user_profile", "id=1", ())
        else:
            with self._lock:
                row = self.conn.execute("SELECT * FROM user_profiles WHERE user_id=?", (owner,)).fetchone()
            data = self._row_to_dict(row, "user_profile")
            if data is not None:
                data["id"] = owner
        if data is None:
            data = dict(_DEFAULT_PROFILE)
            data["fact_sheet"] = json.loads(_json_dumps(_DEFAULT_PROFILE["fact_sheet"]))
            data["target_languages"] = list(_DEFAULT_PROFILE["target_languages"])
            data["id"] = 1 if owner == LOCAL_USER_ID else owner
        # 兜底：极老的库/异常路径也必须带这个键（前端据此判断是否展示引导）
        data.setdefault("profile_setup_completed_at", "")
        return data

    def update_profile(self, *, user_id: str | None = None, **fields: Any) -> dict:
        owner = user_id or current_user_id()
        allowed = {
            "nickname",
            "native_language",
            "target_languages",
            "fact_sheet",
            "birthday",
            "profile_setup_completed_at",
        }
        payload = {k: v for k, v in fields.items() if k in allowed}
        if "target_languages" in payload:
            payload["target_languages"] = _json_dumps(payload["target_languages"])
        if "fact_sheet" in payload:
            payload["fact_sheet"] = _json_dumps(payload["fact_sheet"])
        payload["updated_at"] = _now_iso()
        sets = ", ".join(f"{k}=?" for k in payload)
        with self._lock, self.conn:
            if owner == LOCAL_USER_ID:
                self.conn.execute(f"UPDATE user_profile SET {sets} WHERE id=1", tuple(payload.values()))
            else:
                defaults = self.get_profile(user_id=owner)
                self.conn.execute(
                    "INSERT OR IGNORE INTO user_profiles "
                    "(user_id,nickname,native_language,target_languages,birthday,fact_sheet,updated_at) VALUES (?,?,?,?,?,?,?)",
                    (owner, defaults["nickname"], defaults["native_language"],
                     _json_dumps(defaults["target_languages"]), defaults.get("birthday", ""),
                     _json_dumps(defaults["fact_sheet"]), _now_iso()),
                )
                self.conn.execute(f"UPDATE user_profiles SET {sets} WHERE user_id=?", (*payload.values(), owner))
        return self.get_profile(user_id=owner)

    def set_fact_sheet(self, fact: dict) -> dict:
        return self.update_profile(fact_sheet=fact)

    # ------------------------------------------------------------------ #
    # 好友
    # ------------------------------------------------------------------ #
    def list_friends(self, status: str | None = None, *, owner_user_id: str | None = None) -> list[dict]:
        """当前用户的好友列表（V3 起按 owner_user_id 隔离）。"""
        owner = owner_user_id or current_user_id()
        if legacy_shared_enabled():
            # 单用户老库：owner='local' 的历史好友对当前身份只读可见
            sql = "SELECT * FROM friends WHERE (owner_user_id=? OR owner_user_id=?)"
            params: tuple[Any, ...] = (owner, LOCAL_USER_ID)
        else:
            sql = "SELECT * FROM friends WHERE owner_user_id=?"
            params = (owner,)
        if status:
            sql += " AND status=?"
            params = (*params, status)
        sql += " ORDER BY (last_interaction_at IS NULL), last_interaction_at DESC, created_at DESC"
        return self._fetch_all("friends", sql, params)

    def get_friend(self, fid: str, *, any_owner: bool = False, owner_user_id: str | None = None) -> dict | None:
        """按 id 取好友。

        默认只返回**当前用户**的好友；``any_owner=True`` 供服务端内部
        （后台任务/迁移）读取，不要用在前端请求路径上。
        """
        if not fid:
            return None
        if any_owner:
            return self._fetch_one("friends", "id=?", (fid,))
        owner = owner_user_id or current_user_id()
        if legacy_shared_enabled():
            return self._fetch_one(
                "friends",
                "id=? AND (owner_user_id=? OR owner_user_id=?)",
                (fid, owner, LOCAL_USER_ID),
            )
        return self._fetch_one("friends", "id=? AND owner_user_id=?", (fid, owner))

    def create_friend(self, data: dict) -> dict:
        payload = dict(_FRIEND_DEFAULTS)
        payload.update({k: v for k, v in data.items() if v is not None})
        payload["owner_user_id"] = (
            str(data.get("owner_user_id") or "").strip() or current_user_id()
        )
        payload["id"] = data.get("id") or _uuid()
        payload.setdefault("created_at", _now_iso())
        if not payload.get("avatar_seed"):
            payload["avatar_seed"] = f"{payload.get('name', 'friend')[:2].lower()}-{payload['id'][:4]}"
        cols = list(payload)
        for col in ("speech_quirks", "shared_episodes", "open_loops"):
            if col in cols:
                payload[col] = _json_dumps(payload[col])
        placeholders = ", ".join("?" for _ in cols)
        with self._lock, self.conn:
            self.conn.execute(
                f"INSERT INTO friends ({', '.join(cols)}) VALUES ({placeholders})",
                tuple(payload[c] for c in cols),
            )
        # 显式按 owner 读回：调用方可能不在请求上下文（后台任务/CLI/测试），
        # 此时 current_user_id() 不等于 owner_user_id，用严格模式会读不到刚插入的行。
        created = self.get_friend(payload["id"], any_owner=True)
        assert created is not None
        return created

    def update_friend(self, fid: str, **fields: Any) -> dict:
        allowed = {
            "name", "avatar_seed", "avatar_url", "archetype_id", "voice_id", "voice_rate", "voice_pitch",
            "nationality", "city", "timezone", "native_lang", "language_mix_mode", "age",
            "occupation", "personality_prompt", "speech_quirks", "bio", "status",
            "reply_language", "language_mix_mode",
            "referred_by_friend_id", "request_message", "closeness_level", "total_turns",
            "shared_episodes", "open_loops", "last_interaction_at",
        }
        payload = {k: v for k, v in fields.items() if k in allowed}
        if not payload:
            return self.get_friend(fid) or {}
        for col in ("speech_quirks", "shared_episodes", "open_loops"):
            if col in payload:
                payload[col] = _json_dumps(payload[col])
        sets = ", ".join(f"{k}=?" for k in payload)
        with self._lock, self.conn:
            self.conn.execute(f"UPDATE friends SET {sets} WHERE id=?", (*payload.values(), fid))
        friend = self.get_friend(fid)
        if friend is None:
            raise KeyError(f"friend not found: {fid}")
        return friend

    def delete_friend(self, fid: str) -> None:
        """删除好友；messages/moments 由外键级联删除；清掉指向它的推荐关系。"""
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE friends SET referred_by_friend_id=NULL WHERE referred_by_friend_id=?", (fid,)
            )
            self.conn.execute("DELETE FROM friends WHERE id=?", (fid,))

    def friend_audio_paths(self, fid: str) -> list[str]:
        """该好友所有消息的音频文件名（删除好友时顺带清理磁盘文件）。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT audio_path FROM messages WHERE friend_id=? AND audio_path IS NOT NULL AND audio_path<>''",
                (fid,),
            ).fetchall()
        return [str(r[0]) for r in rows if r[0]]

    def used_archetype_ids(self) -> set[str]:
        with self._lock:
            rows = self.conn.execute("SELECT archetype_id FROM friends").fetchall()
        return {str(r[0]) for r in rows if r[0]}

    def touch_friend(self, fid: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("UPDATE friends SET last_interaction_at=? WHERE id=?", (_now_iso(), fid))

    def add_turns(self, fid: str, n: int = 1) -> dict:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE friends SET total_turns=COALESCE(total_turns,0)+? WHERE id=?", (int(n), fid)
            )
        friend = self.get_friend(fid)
        if friend is None:
            raise KeyError(f"friend not found: {fid}")
        return friend

    # ------------------------------------------------------------------ #
    # 消息
    # ------------------------------------------------------------------ #
    def start_chat_turn(
        self, friend_id: str, transcript: str, *, msg_type: str = "text",
        audio_path: str | None = None, image_path: str | None = None,
        duration_ms: int = 0, context: str | None = None,
        client_message_id: str | None = None, history_limit: int = 10,
        review_limit: int = 2, record_message: bool = True,
    ) -> dict:
        """Read prompt inputs and persist a turn in one owned transaction.

        A new turn's history precedes its user message; retrying an idempotency
        key returns its original row without increasing the calibrated counter.
        Calls can set ``record_message=False`` to retain summary-only storage.
        """
        from app.v2.social import closeness_level

        owner = current_user_id()
        inserted = False
        user_message = None
        with self._lock, self.conn:
            # IMMEDIATE also serializes separate DB connections/processes before
            # the history snapshot and duplicate check, not merely the INSERT.
            self.conn.execute("BEGIN IMMEDIATE")
            friend = self._row_to_dict(self.conn.execute(
                "SELECT * FROM friends WHERE id=? AND owner_user_id=?", (friend_id, owner)
            ).fetchone(), "friends")
            if friend is None:
                raise KeyError(friend_id)
            if record_message and client_message_id:
                user_message = self._row_to_dict(self.conn.execute(
                    "SELECT * FROM messages WHERE friend_id=? AND client_message_id=?",
                    (friend_id, client_message_id),
                ).fetchone(), "messages")
            history = self.recent_messages(friend_id, max(1, int(history_limit)))
            if user_message is None and record_message:
                message_id = _uuid()
                self.conn.execute(
                    "INSERT INTO messages(id,friend_id,sender,transcript,msg_type,audio_path,"
                    "image_path,duration_ms,context,client_message_id,is_read,created_at)"
                    " VALUES (?,?,'user',?,?,?,?,?,?,?,1,?)",
                    (message_id, friend_id, transcript or "", msg_type, audio_path,
                     image_path or "", int(duration_ms or 0), context,
                     client_message_id or None, _now_iso()),
                )
                user_message = self._row_to_dict(self.conn.execute(
                    "SELECT * FROM messages WHERE id=?", (message_id,)
                ).fetchone(), "messages")
                inserted = True
            count = self.conn.execute(
                "SELECT user_turns FROM friend_message_counts WHERE friend_id=?", (friend_id,)
            ).fetchone()
            turns = int(count[0]) if count else 0
            level = closeness_level(turns)
            if inserted or not record_message:
                interaction_at = _now_iso()
                self.conn.execute(
                    "UPDATE friends SET total_turns=?,closeness_level=?,last_interaction_at=?"
                    " WHERE id=? AND owner_user_id=?",
                    (turns, level, interaction_at, friend_id, owner),
                )
                friend.update(total_turns=turns, closeness_level=level,
                              last_interaction_at=interaction_at)
            reviews = self.pick_review_expressions(review_limit)
        return {"friend": friend, "history": history, "user_message": user_message,
                "review_expressions": reviews, "total_turns": turns,
                "closeness_level": level, "inserted": inserted}

    def friend_message_metadata(
        self, friend_ids: Iterable[str], *, owner_user_id: str | None = None,
    ) -> dict[str, dict]:
        """Batch latest previews and indexed unread counts for visible friends."""
        ids = list(dict.fromkeys(str(fid) for fid in friend_ids if fid))
        if not ids:
            return {}
        owner = owner_user_id or current_user_id()
        owners = [owner, LOCAL_USER_ID] if legacy_shared_enabled() else [owner]
        result: dict[str, dict] = {}
        with self._lock:
            # Keep below older SQLite builds' 999-variable limit.
            for offset in range(0, len(ids), 500):
                chunk = ids[offset:offset + 500]
                sql = (
                    "SELECT f.id AS metadata_friend_id,"
                    " (SELECT COUNT(*) FROM messages u WHERE u.friend_id=f.id"
                    " AND u.sender='friend' AND u.is_read=0) AS metadata_unread,m.*"
                    " FROM friends f LEFT JOIN messages m ON m.rowid=("
                    " SELECT latest.rowid FROM messages latest WHERE latest.friend_id=f.id"
                    " ORDER BY latest.created_at DESC,latest.rowid DESC LIMIT 1)"
                    f" WHERE f.id IN ({','.join('?' for _ in chunk)})"
                    f" AND f.owner_user_id IN ({','.join('?' for _ in owners)})"
                )
                for row in self.conn.execute(sql, (*chunk, *owners)).fetchall():
                    message = self._row_to_dict(row, "messages") if row["id"] is not None else None
                    if message is not None:
                        message.pop("metadata_friend_id", None)
                        message.pop("metadata_unread", None)
                    result[str(row["metadata_friend_id"])] = {
                        "unread": int(row["metadata_unread"]), "last_message": message,
                    }
        return result

    def add_message(
        self,
        friend_id: str,
        sender: str,
        transcript: str,
        *,
        msg_type: str = "voice",
        audio_path: str | None = None,
        image_path: str | None = None,
        duration_ms: int = 0,
        suggestion: dict | None = None,
        is_read: bool = True,
        context: str | None = None,
        created_at: str | None = None,
        client_message_id: str | None = None,
        audio_status: str = "",
        audio_error: str = "",
        reply_to_message_id: str = "",
    ) -> dict:
        mid = _uuid()
        row = {
            "id": mid,
            "friend_id": friend_id,
            "sender": sender,
            "msg_type": msg_type,
            "transcript": transcript or "",
            "audio_path": audio_path,
            "image_path": image_path or "",
            "duration_ms": int(duration_ms or 0),
            "suggestion": _json_dumps(suggestion) if suggestion is not None else None,
            "context": context,
            "client_message_id": client_message_id or None,
            "is_read": bool(is_read),
            "created_at": created_at or _now_iso(),
            "audio_status": audio_status,
            "audio_error": audio_error,
            "reply_to_message_id": reply_to_message_id,
        }
        cols = list(row)
        verb = "INSERT OR IGNORE" if client_message_id else "INSERT"
        with self._lock, self.conn:
            cursor = self.conn.execute(
                f"{verb} INTO messages ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                tuple(row[c] for c in cols),
            )
            inserted = cursor.rowcount > 0
        if client_message_id and not inserted:
            existing = self.get_message_by_client_id(friend_id, client_message_id)
            if existing is not None:
                return existing
        msg = self.get_message(mid)
        assert msg is not None
        return msg

    def get_message_by_client_id(self, friend_id: str, client_message_id: str) -> dict | None:
        """按客户端幂等键找消息（同一好友下唯一）。"""
        if not client_message_id:
            return None
        return self._fetch_one(
            "messages", "friend_id=? AND client_message_id=?", (friend_id, client_message_id)
        )

    def get_message(self, mid: str) -> dict | None:
        return self._fetch_one("messages", "id=?", (mid,))

    def update_message(self, mid: str, **fields: Any) -> dict:
        allowed = {"transcript", "audio_path", "duration_ms", "suggestion", "is_read", "context", "msg_type", "audio_status", "audio_error"}
        payload = {k: v for k, v in fields.items() if k in allowed}
        if "suggestion" in payload and payload["suggestion"] is not None:
            payload["suggestion"] = _json_dumps(payload["suggestion"])
        if "is_read" in payload:
            payload["is_read"] = bool(payload["is_read"])
        if payload:
            sets = ", ".join(f"{k}=?" for k in payload)
            with self._lock, self.conn:
                self.conn.execute(f"UPDATE messages SET {sets} WHERE id=?", (*payload.values(), mid))
        msg = self.get_message(mid)
        if msg is None:
            raise KeyError(f"message not found: {mid}")
        return msg

    def list_messages(self, friend_id: str, *, limit: int = 30, before: str | None = None) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        sql = "SELECT * FROM messages WHERE friend_id=?"
        params: list[Any] = [friend_id]
        if before:
            timestamp, separator, message_id = before.partition("|")
            if separator:
                sql += " AND (created_at < ? OR (created_at=? AND rowid < (SELECT rowid FROM messages WHERE id=? AND friend_id=?)))"
                params.extend((timestamp, timestamp, message_id, friend_id))
            else:
                sql += " AND created_at < ?"
                params.append(before)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        rows = self._fetch_all("messages", sql, params)
        rows.reverse()
        return rows

    def recent_messages(self, friend_id: str, limit: int = 10) -> list[dict]:
        """给 prompt/记忆用的最近消息：**过滤商业段落**（广告不进入长期记忆与人物经历）。"""
        rows = self.list_messages(friend_id, limit=max(1, int(limit)) * 3)
        clean = [row for row in rows if str(row.get("msg_type") or "") != "ad"]
        return clean[-max(1, int(limit)):]

    def count_turns(self, friend_id: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE friend_id=? AND sender='user'", (friend_id,)
            ).fetchone()
        return int(row[0]) if row else 0

    def count_messages(self, friend_id: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE friend_id=?", (friend_id,)
            ).fetchone()
        return int(row[0]) if row else 0

    def last_message(self, friend_id: str) -> dict | None:
        rows = self.list_messages(friend_id, limit=1)
        return rows[-1] if rows else None

    def unread_count(self, friend_id: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE friend_id=? AND sender='friend' AND is_read=0",
                (friend_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def mark_read(self, friend_id: str) -> None:
        with self._lock, self.conn:
            # 只改真正未读的行：不加 is_read=0 的话，每次打开会话都会把这个好友的
            # **全部**历史消息重写一遍（聊得越久，这一笔写入越大）。
            self.conn.execute(
                "UPDATE messages SET is_read=1 WHERE friend_id=? AND sender='friend' AND is_read=0",
                (friend_id,),
            )

    # ------------------------------------------------------------------ #
    # 朋友圈
    # ------------------------------------------------------------------ #
    def add_moment(
        self,
        friend_id: str,
        content: str,
        mood_tag: str = "",
        local_time_str: str = "",
        feed_date: str = "",
    ) -> dict:
        mid = _uuid()
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO moments (id, friend_id, content, mood_tag, local_time_str, feed_date, is_replied, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                (mid, friend_id, content, mood_tag, local_time_str, feed_date, _now_iso()),
            )
        moment = self.get_moment(mid)
        assert moment is not None
        return moment

    def list_moments(self, limit: int = 30) -> list[dict]:
        """好友动态只返回当前用户好友发的（V3 起按 owner 隔离）。"""
        owner = current_user_id()
        limit = max(1, min(int(limit), 100))
        if legacy_shared_enabled():
            return self._fetch_all(
                "moments",
                "SELECT m.* FROM moments m JOIN friends f ON f.id=m.friend_id"
                " WHERE (f.owner_user_id=? OR f.owner_user_id=?)"
                " ORDER BY m.created_at DESC, m.rowid DESC LIMIT ?",
                (owner, LOCAL_USER_ID, limit),
            )
        return self._fetch_all(
            "moments",
            "SELECT m.* FROM moments m JOIN friends f ON f.id=m.friend_id"
            " WHERE f.owner_user_id=? ORDER BY m.created_at DESC, m.rowid DESC LIMIT ?",
            (owner, limit),
        )

    def get_moment(self, mid: str) -> dict | None:
        return self._fetch_one("moments", "id=?", (mid,))

    def mark_moment_replied(self, mid: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("UPDATE moments SET is_replied=1 WHERE id=?", (mid,))

    # ---------------------------------------------------------------- #
    # 用户动态（用户自己发） + AI 好友评论
    # ---------------------------------------------------------------- #
    def add_user_moment(
        self, content: str, mood_tag: str = "", image_path: str = "", *, user_id: str | None = None
    ) -> dict:
        mid = _uuid()
        now = _now_iso()
        owner = user_id or current_user_id()
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO user_moments (id, user_id, content, mood_tag, image_path, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (mid, owner, content, mood_tag or "", image_path or "", now),
            )
            row = self.conn.execute(
                "SELECT * FROM user_moments WHERE id=?", (mid,)
            ).fetchone()
        return dict(row) if row else {"id": mid}

    def delete_user_moment(self, mid: str) -> bool:
        """删除用户自己发的动态（评论表 ON DELETE CASCADE 一起清）。"""
        owner = current_user_id()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "DELETE FROM user_moments WHERE id=? AND user_id=?", (mid, owner)
            )
            return cur.rowcount > 0

    def list_user_moments(self, limit: int = 30) -> list[dict]:
        owner = current_user_id()
        return self._fetch_all(
            "user_moments",
            "SELECT * FROM user_moments WHERE user_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (owner, max(1, min(int(limit), 100))),
        )

    def get_user_moment(self, mid: str, *, any_owner: bool = False) -> dict | None:
        if any_owner:
            return self._fetch_one("user_moments", "id=?", (mid,))
        return self._fetch_one(
            "user_moments", "id=? AND user_id=?", (mid, current_user_id())
        )

    def add_moment_comment(self, moment_id: str, friend_id: str, content: str) -> dict:
        cid = _uuid()
        now = _now_iso()
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO moment_comments (id, moment_id, friend_id, content, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (cid, moment_id, friend_id or "", content, now),
            )
            row = self.conn.execute(
                "SELECT * FROM moment_comments WHERE id=?", (cid,)
            ).fetchone()
        return dict(row) if row else {"id": cid}

    def list_moment_comments(self, moment_id: str) -> list[dict]:
        return self._fetch_all(
            "moment_comments",
            "SELECT * FROM moment_comments WHERE moment_id=? ORDER BY created_at, rowid",
            (moment_id,),
        )

    def comments_for_moments(self, moment_ids: list[str]) -> dict[str, list[dict]]:
        if not moment_ids:
            return {}
        placeholders = ",".join("?" for _ in moment_ids)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM moment_comments WHERE moment_id IN ({placeholders})"
                " ORDER BY created_at, rowid",
                tuple(moment_ids),
            ).fetchall()
        grouped: dict[str, list[dict]] = {mid: [] for mid in moment_ids}
        for row in rows:
            grouped.setdefault(str(row["moment_id"]), []).append(dict(row))
        return grouped

    def moments_exist_for_date(self, feed_date: str) -> bool:
        owner = current_user_id()
        shared = legacy_shared_enabled()
        with self._lock:
            if shared:
                row = self.conn.execute(
                    "SELECT COUNT(*) FROM moments m JOIN friends f ON f.id=m.friend_id"
                    " WHERE m.feed_date=? AND (f.owner_user_id=? OR f.owner_user_id=?)",
                    (feed_date, owner, LOCAL_USER_ID),
                ).fetchone()
            else:
                row = self.conn.execute(
                    "SELECT COUNT(*) FROM moments m JOIN friends f ON f.id=m.friend_id"
                    " WHERE m.feed_date=? AND f.owner_user_id=?",
                    (feed_date, owner),
                ).fetchone()
        return bool(row and row[0])

    # ------------------------------------------------------------------ #
    # 表达本
    # ------------------------------------------------------------------ #
    def add_expression(
        self,
        original_text: str,
        better_expression: str,
        explanation: str | None = None,
        source_friend_id: str | None = None,
    ) -> dict:
        eid = _uuid()
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO saved_expressions"
                " (id, user_id, original_text, better_expression, explanation, source_friend_id,"
                "  mastery_count, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 0, 'learning', ?)",
                (eid, current_user_id(), original_text or "", better_expression, explanation,
                 source_friend_id, _now_iso()),
            )
        expr = self.get_expression(eid, any_owner=True)
        assert expr is not None
        return expr

    def list_expressions(self, status: str | None = None) -> list[dict]:
        owner = current_user_id()
        sql = "SELECT * FROM saved_expressions WHERE user_id=?"
        params: tuple[Any, ...] = (owner,)
        if status:
            sql += " AND status=?"
            params = (owner, status)
        sql += " ORDER BY created_at DESC"
        return self._fetch_all("saved_expressions", sql, params)

    def get_expression(self, eid: str, *, any_owner: bool = False) -> dict | None:
        if any_owner:
            return self._fetch_one("saved_expressions", "id=?", (eid,))
        return self._fetch_one(
            "saved_expressions", "id=? AND user_id=?", (eid, current_user_id())
        )

    def update_expression(self, eid: str, **fields: Any) -> dict:
        allowed = {"original_text", "better_expression", "explanation", "mastery_count", "status"}
        payload = {k: v for k, v in fields.items() if k in allowed}
        if payload:
            sets = ", ".join(f"{k}=?" for k in payload)
            with self._lock, self.conn:
                self.conn.execute(
                    f"UPDATE saved_expressions SET {sets} WHERE id=? AND user_id=?",
                    (*payload.values(), eid, current_user_id()),
                )
        expr = self.get_expression(eid)
        if expr is None:
            raise KeyError(f"expression not found: {eid}")
        return expr

    def delete_expression(self, eid: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "DELETE FROM saved_expressions WHERE id=? AND user_id=?",
                (eid, current_user_id()),
            )

    def pick_review_expressions(self, n: int = 2) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM saved_expressions WHERE status='learning' AND user_id=?"
                " ORDER BY mastery_count ASC, created_at ASC LIMIT ?",
                (current_user_id(), max(0, int(n))),
            ).fetchall()
        return [d for d in (self._row_to_dict(r, "saved_expressions") for r in rows) if d is not None]
