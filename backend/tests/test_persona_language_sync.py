"""角色改语言后，好友的回复语言（friends.reply_language）必须一起同步。

背景（线上问题）：后台把角色语言从 en 改成 zh，人物资料页显示中文，但聊天仍整段英文。
原因：聊天/TTS 的回复语言取自 ``friends.reply_language``，而 ``_sync_friend_rows`` 只同步了
``native_lang``，没有同步 ``reply_language``。
"""

from __future__ import annotations

import sqlite3
import threading

from app.admin import persona_studio
from app.v2.chat_service import _language_of


class _FakeDB:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.conn.execute(
            "CREATE TABLE friends (id TEXT PRIMARY KEY, persona_id TEXT, name TEXT,"
            " native_lang TEXT, reply_language TEXT, voice_id TEXT, fish_voice_id TEXT,"
            " nationality TEXT, city TEXT, timezone TEXT, age INTEGER)"
        )
        self.conn.execute(
            "CREATE TABLE friendships (id TEXT PRIMARY KEY, friend_id TEXT, persona_id TEXT)"
        )


def _persona(language: str = "en") -> dict:
    return {
        "id": "p_test",
        "nickname": "诺亚",
        "language": language,
        "country": "China",
        "city": "上海",
        "timezone": "Asia/Shanghai",
        "age": 24,
        "avatar_seed": "seed",
        "avatar_style": "",
        "avatar_options": {},
        "archetype_id": "",
        "voice_id": "en-US-AvaMultilingualNeural",
        "fish_voice_id": "",
    }


def _seed_friend(db: _FakeDB, reply_language: str = "en") -> None:
    db.conn.execute(
        "INSERT INTO friends (id, persona_id, name, native_lang, reply_language, voice_id)"
        " VALUES ('f1','p_test','诺亚','en',?, 'en-US-AvaMultilingualNeural')",
        (reply_language,),
    )
    db.conn.commit()


def test_language_change_syncs_friend_reply_language():
    db = _FakeDB()
    _seed_friend(db)

    persona = _persona("zh")
    synced = persona_studio._sync_friend_rows(db, persona, sync_reply_language=True)  # noqa: SLF001

    row = dict(db.conn.execute("SELECT * FROM friends WHERE id='f1'").fetchone())
    assert synced == 1
    assert row["native_lang"] == "zh"
    assert row["reply_language"] == "zh"
    # 聊天入口读的就是这个字段
    assert _language_of(row) == "zh"


def test_other_persona_edits_keep_friend_reply_language():
    """只改昵称/城市时不能顺手改掉用户正在练的回复语言。"""
    db = _FakeDB()
    _seed_friend(db, reply_language="ja")

    persona = _persona("en")
    synced = persona_studio._sync_friend_rows(db, persona, sync_reply_language=False)  # noqa: SLF001

    row = dict(db.conn.execute("SELECT * FROM friends WHERE id='f1'").fetchone())
    assert synced == 1
    assert row["native_lang"] == "en"
    assert row["reply_language"] == "ja"
