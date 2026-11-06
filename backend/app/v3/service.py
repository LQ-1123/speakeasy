"""SpeakEasy V3 · 业务服务层（SQLite + 社交事件编排）。

职责
----
- Persona / Post / Friendship / UserPost / SocialEvent 的读写
- 信息流合并与游标分页（Persona 公共动态 + 用户动态）
- 添加好友：建立 ``friendships`` 并复用 V2 的 ``friends`` 表（聊天/记忆/语音全部复用）
- 社交事件：匹配 → 概率 → 冷却/频率 → 延迟 → 物化（幂等）

约定
----
- 所有方法都是**同步**的，API 层用 ``asyncio.to_thread`` 调用（与 V2 一致）。
- 任何返回给前端的用户数据都必须带 ``user_id`` 归属校验；越权视为不存在。
- 事件物化（把 like/comment/friend_request 真正写进对应表）只在 ``scheduled_at``
  到期后执行；生成内容（评论/申请语）在触发时完成，保证读取路径不调模型。
"""

from __future__ import annotations

import hashlib
import json
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from app.logging_utils import get_logger
from app.v3 import social_engine as engine
from app.v3.identity import LOCAL_USER_ID, current_user_id
from app.v3.social_engine import SocialConfig

log = get_logger(__name__)

#: 信息流默认分页
FEED_PAGE = 10
MAX_FEED_PAGE = 40

#: 用户动态可见性
VISIBILITIES = ("public", "friends", "private")

#: 好友申请过期时间
FRIEND_REQUEST_TTL_S = 7 * 24 * 3600


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _uuid() -> str:
    return uuid.uuid4().hex


def _loads(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _clean(value: Any, limit: int = 2000) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit].strip()


# --------------------------------------------------------------------------- #
# 事件内容生成（LLM + 模板兜底）
# --------------------------------------------------------------------------- #
_COMMENT_TEMPLATES: dict[str, tuple[str, ...]] = {
    "witty": ("Ha, this is very you.", "Honestly? Relatable.", "That's a good one."),
    "calm": ("This made me slow down for a second.", "Really nice.", "Glad you shared this."),
    "sarcastic": ("Bold move. Respect.", "This is peak you.", "Can't argue with that."),
    "warm": ("Aww, love this!", "This just made my day.", "So happy to see this!"),
    "poetic": ("There's something quietly lovely about this.", "I like the way you put that.", "Feels like a page from a good book."),
    "passionate": ("Now THIS is the good stuff.", "Yes. Just yes.", "I feel strongly about this, obviously."),
    "quiet": ("Nice frame.", "I noticed that too.", "This stays with you a bit."),
    "dry": ("Fair enough.", "That checks out.", "Noted, and slightly admired."),
    "playful": ("Okay this is cute 😄", "Same energy, honestly.", "I'd upvote this twice."),
    "upbeat": ("Love the energy here!", "That's such a good day.", "Go you!"),
    "sleepy": ("This is exactly my vibe.", "Reading this from under a blanket.", "Same, but with tea."),
    "blunt": ("Good. Straight to the point.", "Approved.", "Simple and correct."),
}

_REQUEST_TEMPLATES: tuple[str, ...] = (
    "Hey! We keep liking each other's stuff — want to be friends?",
    "Hi! I saw your post and thought we'd get on. Add me?",
    "You seem fun. Want to practice English together sometime?",
)


def _fallback_comment(persona: dict[str, Any], post: dict[str, Any], rng: random.Random) -> str:
    temp = str((persona.get("ai_config") or {}).get("temperament") or "").strip().lower()
    pool = _COMMENT_TEMPLATES.get(temp) or (
        "Love this!",
        "This made me smile.",
        "Great point.",
    )
    return rng.choice(pool)


def _fallback_request(persona: dict[str, Any], rng: random.Random) -> str:
    return rng.choice(_REQUEST_TEMPLATES)


class V3Service:
    """V3 业务门面（挂在 ``app.state.v3``）。"""

    def __init__(self, db: Any, engines: Any, data_dir: str = "") -> None:
        self.db = db
        self.engines = engines
        self.data_dir = data_dir
        self.config: SocialConfig = SocialConfig.from_env()
        #: 可注入的随机源（测试里换成 ``random.Random(seed)``）
        self.rng: random.Random = random.Random()
        self._persona_cache: dict[str, Any] = {"at": 0.0, "rows": []}
        self._feed_cache: dict[str, Any] = {"at": 0.0, "key": "", "items": []}

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    @property
    def conn(self):
        return self.db.conn

    @property
    def lock(self):
        return self.db._lock  # noqa: SLF001 - 与 DB 共用连接与锁

    @staticmethod
    def _persona_public(row: Any) -> dict:
        return {
            "id": row["id"],
            "nickname": row["nickname"],
            "avatar": row["avatar"],
            "bio": row["bio"],
            "personality": row["personality"],
            "interests": _loads(row["interests"], []),
            "language": row["language"],
            "country": row["country"],
            "city": row["city"],
            "timezone": row["timezone"],
            "occupation": row["occupation"],
            "age": row["age"],
            "gender": row["gender"],
            "is_ai": True,
            "ai_label": "AI 人物",
        }

    @staticmethod
    def _persona_row(row: Any) -> dict:
        data = dict(row)
        data["interests"] = _loads(data.get("interests"), [])
        data["ai_config"] = _loads(data.get("ai_config"), {})
        return data

    def _personas(self) -> list[dict]:
        """全部 active Persona（60s 内存缓存，避免每页都扫表）。"""
        now = time.time()
        if now - float(self._persona_cache.get("at") or 0) < 60 and self._persona_cache.get("rows"):
            return list(self._persona_cache["rows"])
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM personas WHERE status='active' ORDER BY nickname"
            ).fetchall()
        personas = [self._persona_row(r) for r in rows]
        self._persona_cache = {"at": now, "rows": personas}
        return list(personas)

    def persona_by_id(self, persona_id: str) -> dict | None:
        for persona in self._personas():
            if persona["id"] == persona_id:
                return persona
        return None

    def get_persona_post_row(self, post_id: str) -> dict | None:
        """取一条 Persona 动态原始行（朗读/详情用）。"""
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM persona_posts WHERE id=?", (post_id,)
            ).fetchone()
        return dict(row) if row else None

    def recent_persona_posts(self, limit: int = 8) -> list[dict]:
        """最近的 Persona 动态原始行（用于朗读音频预热）。"""
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM persona_posts ORDER BY created_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(r) for r in rows]

    def _persona_map(self) -> dict[str, dict]:
        return {p["id"]: p for p in self._personas()}

    # ------------------------------------------------------------------ #
    # Persona
    # ------------------------------------------------------------------ #
    def list_personas(self, *, limit: int = 24, offset: int = 0) -> list[dict]:
        limit = max(1, min(int(limit), 60))
        personas = self._personas()
        return [self._persona_brief(p) for p in personas[offset : offset + limit]]

    @staticmethod
    def _persona_brief(persona: dict) -> dict:
        from app.v3.avatar import persona_avatar_url  # noqa: PLC0415
        from app.v3.avatar_style import persona_avatar_meta  # noqa: PLC0415

        return {
            "id": persona["id"],
            "nickname": persona["nickname"],
            # avatar 是兜底 URL（老客户端/封面用）；新客户端优先用 avatar_style + avatar_options
            "avatar": persona_avatar_url(persona),
            "avatar_seed": persona.get("avatar_seed", ""),
            **persona_avatar_meta(persona),
            "bio": persona["bio"],
            "interests": persona["interests"],
            "country": persona["country"],
            "city": persona["city"],
            "occupation": persona["occupation"],
            "age": persona["age"],
            "gender": persona["gender"],
            "language": persona["language"],
            "is_ai": True,
            "ai_label": "AI 人物",
        }

    def persona_profile(self, persona_id: str, user_id: str | None = None) -> dict | None:
        persona = self.persona_by_id(persona_id)
        if persona is None:
            return None
        uid = user_id or current_user_id()
        friendship = self.get_friendship(uid, persona_id)
        profile = self._persona_public_from_full(persona)
        profile["friendship"] = self._friendship_public(friendship)
        with self.lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM persona_posts WHERE persona_id=? AND visibility='public'",
                (persona_id,),
            ).fetchone()
        profile["post_count"] = int(row["n"]) if row else 0
        profile["posts"] = self.persona_posts(persona_id, user_id=uid, limit=30)
        return profile

    @staticmethod
    def _persona_public_from_full(persona: dict) -> dict:
        from app.v3.avatar import persona_avatar_url  # noqa: PLC0415
        from app.v3.avatar_style import persona_avatar_meta  # noqa: PLC0415

        return {
            "id": persona["id"],
            "nickname": persona["nickname"],
            "avatar": persona_avatar_url(persona),
            "avatar_seed": persona.get("avatar_seed", ""),
            **persona_avatar_meta(persona),
            "bio": persona["bio"],
            "personality": persona["personality"],
            "interests": persona["interests"],
            "language": persona["language"],
            "country": persona["country"],
            "city": persona["city"],
            "timezone": persona["timezone"],
            "occupation": persona["occupation"],
            "age": persona["age"],
            "gender": persona["gender"],
            "is_ai": True,
            "ai_label": "AI 人物",
        }

    # ------------------------------------------------------------------ #
    # 动态（Persona）
    # ------------------------------------------------------------------ #
    def persona_posts(
        self, persona_id: str, *, user_id: str | None = None, limit: int = 20, cursor: str | None = None
    ) -> dict:
        uid = user_id or current_user_id()
        limit = max(1, min(int(limit), 40))
        from app.v3.daily_world import iso as _iso, now_local as _now_local  # noqa: PLC0415

        sql = (
            "SELECT * FROM persona_posts WHERE persona_id=? AND visibility='public'"
            " AND (expires_at='' OR expires_at>?)"
        )
        params: list[Any] = [persona_id, _iso(_now_local())]
        if cursor:
            ts, _, cid = str(cursor).partition("|")
            sql += " AND (created_at < ? OR (created_at = ? AND id < ?))"
            params += [ts, ts, cid]
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        # 多取 1 条用于判断「是否还有下一页」，避免最后一页返回非空 cursor
        params.append(limit + 1)
        with self.lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        friends, pending = self._friendship_sets(uid)
        items = [self._persona_post_public(r, uid, friends=friends, pending=pending) for r in rows]
        next_cursor = ""
        if has_more and items:
            next_cursor = f"{items[-1]['created_at']}|{items[-1]['id']}"
        return {"items": items, "next_cursor": next_cursor}

    def _friendship_sets(self, user_id: str) -> tuple[set[str], set[str]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT persona_id, status FROM friendships WHERE user_id=?", (user_id,)
            ).fetchall()
        accepted = {str(r["persona_id"]) for r in rows if str(r["status"]) == "accepted"}
        pending = {str(r["persona_id"]) for r in rows if str(r["status"]) == "pending"}
        return accepted, pending

    def _persona_post_public(
        self,
        row: Any,
        user_id: str,
        *,
        friends: set[str] | None = None,
        pending: set[str] | None = None,
    ) -> dict:
        pmap = self._persona_map()
        persona = pmap.get(row["persona_id"]) or {}
        post_id = row["id"]
        pid = str(row["persona_id"])
        if friends is None or pending is None:
            friends, pending = self._friendship_sets(user_id)
        with self.lock:
            liked = self.conn.execute(
                "SELECT 1 FROM persona_post_likes WHERE post_id=? AND user_id=? LIMIT 1",
                (post_id, user_id),
            ).fetchone()
            comments = self.conn.execute(
                "SELECT * FROM persona_post_comments WHERE post_id=? ORDER BY created_at, id LIMIT 2",
                (post_id,),
            ).fetchall()
            comment_count = self.conn.execute(
                "SELECT COUNT(*) AS n FROM persona_post_comments WHERE post_id=?", (post_id,)
            ).fetchone()["n"]
        return {
            "kind": "persona_post",
            "id": post_id,
            "content": row["content"],
            "media": _loads(row["media"], []),
            "tags": _loads(row["tags"], []),
            "language": (row["language"] if "language" in row.keys() else "") or persona.get("language", "en"),
            # content_hash 供前端做语音缓存版本号：内容变了 URL 就变
            "content_hash": hashlib.sha1(str(row["content"]).encode("utf-8")).hexdigest()[:12],
            "visibility": "public",
            "created_at": row["created_at"],
            "like_count": int(row["like_count"] or 0),
            "comment_count": int(comment_count or 0),
            "liked_by_me": bool(liked),
            "is_mine": False,
            "friendship_status": ("accepted" if pid in friends else "pending" if pid in pending else "none"),
            "author": self._persona_brief(persona) if persona else {
                "id": row["persona_id"], "nickname": "AI 人物", "avatar": "",
                "is_ai": True, "ai_label": "AI 人物",
            },
            "comments": [
                {
                    "id": c["id"],
                    "content": c["content"],
                    "created_at": c["created_at"],
                    "author": {"type": "user", "nickname": "我", "is_ai": False},
                }
                for c in comments
            ],
        }

    # ------------------------------------------------------------------ #
    # 用户动态
    # ------------------------------------------------------------------ #
    def _user_name(self, user_id: str) -> str:
        if user_id == LOCAL_USER_ID:
            try:
                return str((self.db.get_profile() or {}).get("nickname") or "Traveler")
            except Exception:  # noqa: BLE001
                return "Traveler"
        with self.lock:
            row = self.conn.execute(
                "SELECT display_name FROM v3_users WHERE id=?", (user_id,)
            ).fetchone()
        return str((row["display_name"] if row else "") or "Traveler")

    def _user_post_public(self, row: Any, viewer_id: str) -> dict:
        pmap = self._persona_map()
        post_id = row["id"]
        with self.lock:
            likes = self.conn.execute(
                "SELECT persona_id FROM user_post_likes WHERE post_id=? ORDER BY created_at LIMIT 6",
                (post_id,),
            ).fetchall()
            comments = self.conn.execute(
                "SELECT * FROM user_post_comments WHERE post_id=? ORDER BY created_at, id LIMIT 8",
                (post_id,),
            ).fetchall()
        author_id = row["user_id"]
        return {
            "kind": "user_post",
            "id": post_id,
            "content": row["content"],
            "media": _loads(row["media"], []),
            "tags": _loads(row["tags"], []),
            "visibility": row["visibility"],
            "created_at": row["created_at"],
            "like_count": int(row["like_count"] or 0),
            "comment_count": int(row["comment_count"] or 0),
            "liked_by_me": any(str(x["persona_id"]) == viewer_id for x in likes),
            "is_mine": author_id == viewer_id,
            "author": {
                "id": author_id,
                "nickname": row["author_name"] or self._user_name(author_id),
                "avatar": "",
                "is_ai": False,
                "ai_label": "",
            },
            "liked_by": [
                {
                    "persona_id": x["persona_id"],
                    "nickname": (pmap.get(str(x["persona_id"])) or {}).get("nickname", ""),
                }
                for x in likes
            ],
            "comments": [
                {
                    "id": c["id"],
                    "content": c["content"],
                    "created_at": c["created_at"],
                    "author": {
                        "type": "persona",
                        "persona_id": c["persona_id"],
                        "nickname": (pmap.get(str(c["persona_id"])) or {}).get("nickname", "AI 人物"),
                        "is_ai": True,
                        "ai_label": "AI 人物",
                    },
                }
                for c in comments
            ],
        }

    def create_user_post(
        self,
        user_id: str,
        content: str,
        *,
        media: Iterable[str] | None = None,
        tags: Iterable[str] | None = None,
        visibility: str = "public",
        author_name: str = "",
    ) -> dict:
        text = _clean(content, 1000)
        if not text:
            raise ValueError("动态内容不能为空")
        vis = visibility if visibility in VISIBILITIES else "public"
        post_id = f"up_{_uuid()}"
        name = _clean(author_name, 40) or self._user_name(user_id)
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO user_posts (id, user_id, author_name, content, media, tags,"
                " visibility, like_count, comment_count, created_at)"
                " VALUES (?,?,?,?,?,?,?,0,0,?)",
                (post_id, user_id, name, text, _dumps(list(media or [])),
                 _dumps(list(tags or [])), vis, _iso()),
            )
            row = self.conn.execute("SELECT * FROM user_posts WHERE id=?", (post_id,)).fetchone()
        self._feed_cache["at"] = 0.0
        return self._user_post_public(row, user_id)

    def get_user_post(self, post_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM user_posts WHERE id=?", (post_id,)).fetchone()
        return dict(row) if row else None

    def list_user_posts(self, user_id: str, *, limit: int = 30) -> list[dict]:
        limit = max(1, min(int(limit), 100))
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM user_posts WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [self._user_post_public(r, user_id) for r in rows]

    def delete_user_post(self, user_id: str, post_id: str) -> bool:
        with self.lock, self.conn:
            row = self.conn.execute(
                "SELECT user_id FROM user_posts WHERE id=?", (post_id,)
            ).fetchone()
            if row is None or str(row["user_id"]) != user_id:
                return False
            self.conn.execute("DELETE FROM user_posts WHERE id=?", (post_id,))
            self.conn.execute("DELETE FROM user_post_likes WHERE post_id=?", (post_id,))
            self.conn.execute("DELETE FROM user_post_comments WHERE post_id=?", (post_id,))
            self.conn.execute("DELETE FROM social_events WHERE post_id=?", (post_id,))
        self._feed_cache["at"] = 0.0
        return True

    # ------------------------------------------------------------------ #
    # 信息流
    # ------------------------------------------------------------------ #
    def feed(self, user_id: str | None = None, *, cursor: str | None = None, limit: int = FEED_PAGE) -> dict:
        uid = user_id or current_user_id()
        limit = max(1, min(int(limit), MAX_FEED_PAGE))
        fetch_n = limit + 1

        from app.v3.daily_world import iso as _iso, now_local as _now_local  # noqa: PLC0415

        now_iso = _iso(_now_local())
        p_sql = (
            "SELECT * FROM persona_posts WHERE visibility='public'"
            " AND (expires_at='' OR expires_at>?)"
        )
        u_sql = ("SELECT * FROM user_posts WHERE (visibility='public' OR user_id=?)")
        p_params: list[Any] = [now_iso]
        u_params: list[Any] = [uid]
        if cursor:
            ts, _, cid = str(cursor).partition("|")
            clause = " AND (created_at < ? OR (created_at = ? AND id < ?))"
            p_sql += clause
            u_sql += clause
            p_params += [ts, ts, cid]
            u_params += [ts, ts, cid]
        p_sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        u_sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        p_params.append(fetch_n)
        u_params.append(fetch_n)

        with self.lock:
            p_rows = self.conn.execute(p_sql, tuple(p_params)).fetchall()
            u_rows = self.conn.execute(u_sql, tuple(u_params)).fetchall()

        merged: list[tuple[str, str, dict]] = []
        friends, pending = self._friendship_sets(uid)
        for row in p_rows:
            merged.append(
                (row["created_at"], row["id"],
                 self._persona_post_public(row, uid, friends=friends, pending=pending))
            )
        for row in u_rows:
            merged.append((row["created_at"], row["id"], self._user_post_public(row, uid)))
        merged.sort(key=lambda item: (item[0], item[1]), reverse=True)
        page = merged[:limit]
        next_cursor = f"{page[-1][0]}|{page[-1][1]}" if len(merged) > limit and page else ""
        return {"items": [item[2] for item in page], "next_cursor": next_cursor}

    def feed_summary(self, user_id: str) -> dict:
        uid = user_id
        with self.lock:
            from app.v3.daily_world import iso as _iso, now_local as _now_local  # noqa: PLC0415

            p = self.conn.execute(
                "SELECT COUNT(*) AS n FROM persona_posts"
                " WHERE expires_at='' OR expires_at>?",
                (_iso(_now_local()),),
            ).fetchone()["n"]
            mine = self.conn.execute(
                "SELECT COUNT(*) AS n FROM user_posts WHERE user_id=?", (uid,)
            ).fetchone()["n"]
            friends = self.conn.execute(
                "SELECT COUNT(*) AS n FROM friendships WHERE user_id=? AND status='accepted'", (uid,)
            ).fetchone()["n"]
            events = self.conn.execute(
                "SELECT COUNT(*) AS n FROM social_events WHERE user_id=? AND is_read=0 AND status<>'declined'",
                (uid,),
            ).fetchone()["n"]
        return {
            "persona_posts": int(p),
            "my_posts": int(mine),
            "friends": int(friends),
            "unread_events": int(events),
        }

    # ------------------------------------------------------------------ #
    # 互动（对 Persona 动态）
    # ------------------------------------------------------------------ #
    def toggle_persona_post_like(self, user_id: str, post_id: str) -> dict:
        with self.lock, self.conn:
            post = self.conn.execute(
                "SELECT * FROM persona_posts WHERE id=?", (post_id,)
            ).fetchone()
            if post is None:
                raise KeyError(post_id)
            existing = self.conn.execute(
                "SELECT id FROM persona_post_likes WHERE post_id=? AND user_id=?",
                (post_id, user_id),
            ).fetchone()
            if existing is not None:
                self.conn.execute("DELETE FROM persona_post_likes WHERE id=?", (existing["id"],))
                self.conn.execute(
                    "UPDATE persona_posts SET like_count=MAX(0, like_count-1) WHERE id=?", (post_id,)
                )
                liked = False
            else:
                self.conn.execute(
                    "INSERT INTO persona_post_likes (id, post_id, user_id, created_at) VALUES (?,?,?,?)",
                    (_uuid(), post_id, user_id, _iso()),
                )
                self.conn.execute(
                    "UPDATE persona_posts SET like_count=like_count+1 WHERE id=?", (post_id,)
                )
                liked = True
            updated = self.conn.execute(
                "SELECT like_count FROM persona_posts WHERE id=?", (post_id,)
            ).fetchone()
        return {"post_id": post_id, "liked": liked, "like_count": int(updated["like_count"])}

    def comment_persona_post(self, user_id: str, post_id: str, content: str) -> dict:
        text = _clean(content, 500)
        if not text:
            raise ValueError("评论内容不能为空")
        with self.lock, self.conn:
            post = self.conn.execute(
                "SELECT * FROM persona_posts WHERE id=?", (post_id,)
            ).fetchone()
            if post is None:
                raise KeyError(post_id)
            cid = _uuid()
            self.conn.execute(
                "INSERT INTO persona_post_comments (id, post_id, user_id, content, created_at)"
                " VALUES (?,?,?,?,?)",
                (cid, post_id, user_id, text, _iso()),
            )
            self.conn.execute(
                "UPDATE persona_posts SET comment_count=comment_count+1 WHERE id=?", (post_id,)
            )
        return {"id": cid, "post_id": post_id, "content": text, "created_at": _iso()}

    # ------------------------------------------------------------------ #
    # 偶遇：从已经生成好的「世界」里挑一个人，不再现场捏人
    # ------------------------------------------------------------------ #
    def discover_next(self, user_id: str | None = None, *, language: str | None = None) -> dict | None:
        """从 Persona 池里挑一位还没成为好友的人物。

        V3 的「捏人」发生在世界生成阶段（一次生成人物 + 信息流）；
        偶遇只是**浏览**这个世界，因此这里绝不再调用 spawner/LLM。

        选择策略：优先没加过好友的 → 按 (user, 日期, persona) 稳定哈希轮换，
        保证同一天同一个用户多次点「换一个」会得到不同的人，且可复现。
        """
        uid = user_id or current_user_id()
        personas = self._personas()
        if language:
            filtered = [p for p in personas if str(p.get("language")) == language]
            personas = filtered or personas
        if not personas:
            return None

        friends, pending = self._friendship_sets(uid)
        fresh = [p for p in personas if p["id"] not in friends and p["id"] not in pending]
        pool = fresh or personas

        # 轮换：优先没看过的；都看过了就清空记录重新开始
        with self.lock:
            seen = {
                str(r["persona_id"])
                for r in self.conn.execute(
                    "SELECT persona_id FROM v3_discover_views WHERE user_id=?", (uid,)
                ).fetchall()
            }
        unseen = [p for p in pool if p["id"] not in seen]
        if not unseen:
            with self.lock, self.conn:
                self.conn.execute(
                    "DELETE FROM v3_discover_views WHERE user_id=?", (uid,)
                )
            unseen = pool

        day = _now().strftime("%Y-%m-%d")
        unseen = sorted(
            unseen,
            key=lambda p: hashlib.sha256(f"{uid}:{day}:{p['id']}".encode("utf-8")).hexdigest(),
        )
        persona = unseen[0]
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO v3_discover_views (user_id, persona_id, seen_at)"
                " VALUES (?,?,?)",
                (uid, persona["id"], _iso()),
            )

        posts = self.persona_posts(persona["id"], user_id=uid, limit=3)["items"]
        latest = posts[0] if posts else None
        greeting = str(latest["content"]) if latest else str(persona.get("bio") or "")
        post_hash = (
            hashlib.sha1(greeting.encode("utf-8")).hexdigest()[:12] if latest else ""
        )
        return {
            "persona": self._persona_brief(persona),
            "greeting": greeting,
            "post_id": latest["id"] if latest else "",
            "post_hash": post_hash,
            "post_language": (latest.get("language") if latest else "") or persona.get("language", "en"),
            "friendship_status": (
                "accepted" if persona["id"] in friends
                else "pending" if persona["id"] in pending
                else "none"
            ),
            "remaining": max(0, len(fresh) - 1),
            "source": "world",  # 明确：来自已生成的世界，不是现场捏人
        }

    # ------------------------------------------------------------------ #
    # 好友关系（Persona）
    # ------------------------------------------------------------------ #
    def get_friendship(self, user_id: str, persona_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM friendships WHERE user_id=? AND persona_id=?",
                (user_id, persona_id),
            ).fetchone()
        return dict(row) if row else None

    def list_friendships(self, user_id: str) -> dict:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM friendships WHERE user_id=? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        pmap = self._persona_map()
        pending = [r for r in rows if str(r["status"]) == "pending"]
        accepted = [r for r in rows if str(r["status"]) == "accepted"]
        return {
            "pending": [self._friendship_public(dict(r), pmap) for r in pending],
            "accepted": [self._friendship_public(dict(r), pmap) for r in accepted],
        }

    @staticmethod
    def _friendship_public(friendship: dict | None, pmap: dict | None = None) -> dict | None:
        if not friendship:
            return None
        persona = (pmap or {}).get(str(friendship.get("persona_id"))) or {}
        return {
            "id": friendship.get("id"),
            "persona_id": friendship.get("persona_id"),
            "status": friendship.get("status"),
            "friend_id": friendship.get("friend_id"),
            "request_message": friendship.get("request_message") or "",
            "created_at": friendship.get("created_at"),
            "updated_at": friendship.get("updated_at"),
            "persona": {
                "id": persona.get("id"),
                "nickname": persona.get("nickname", ""),
                "avatar": persona.get("avatar", ""),
                "is_ai": True,
            } if persona else None,
        }

    def _friend_row_for_persona(self, persona: dict, *, status: str) -> dict:
        """把 Persona 映射成 ``friends`` 表的一行（复用 V2 聊天/记忆/语音）。

        语言一致性：好友的回复语言 = 人物母语，声线 = 人物声线，
        所以聊天文字和语音条永远是同一种语言。
        """
        name = str(persona.get("nickname") or "Friend")
        personality = str(persona.get("personality") or "")
        bio = str(persona.get("bio") or "")
        lang = str(persona.get("language") or "en").strip().lower()
        from app.v3.avatar import persona_avatar_url  # noqa: PLC0415

        return {
            "name": name,
            "avatar_seed": f"{persona['id']}-{name[:2].lower()}",
            "avatar_url": persona_avatar_url(persona),
            "avatar_style": str(persona.get("avatar_style") or ""),
            "avatar_options": str(persona.get("avatar_options") or ""),
            "archetype_id": str(persona.get("archetype_id") or ""),
            "voice_id": str(persona.get("voice_id") or ""),
            "nationality": str(persona.get("country") or ""),
            "city": str(persona.get("city") or ""),
            "timezone": str(persona.get("timezone") or "UTC"),
            "native_lang": lang,
            "language_mix_mode": "native",
            "reply_language": lang,
            "age": int(persona.get("age") or 25),
            "occupation": str(persona.get("occupation") or ""),
            "personality_prompt": (personality + ("\n" + bio if bio else ""))[:1500],
            "bio": bio,
            "gender": str(persona.get("gender") or ""),
            "status": status,
            "request_message": "",
            "closeness_level": 1,
            "total_turns": 0,
        }

    def add_friend(self, user_id: str, persona_id: str, *, request_message: str = "") -> dict:
        """用户主动加 Persona 为好友（幂等）。"""
        persona = self.persona_by_id(persona_id)
        if persona is None:
            raise KeyError(persona_id)

        existing = self.get_friendship(user_id, persona_id)
        if existing and str(existing.get("status")) == "accepted":
            return {
                "friendship": self._friendship_public(existing, {persona_id: persona}),
                "friend_id": existing.get("friend_id") or "",
                "created": False,
            }
        if existing and str(existing.get("status")) == "pending":
            # 用户主动加好友 = 接受 TA 的申请：直接把关系升为 accepted
            friend_id = str(existing.get("friend_id") or "")
            now = _iso()
            with self.lock, self.conn:
                self.conn.execute(
                    "UPDATE friendships SET status='accepted', updated_at=? WHERE id=?",
                    (now, existing["id"]),
                )
                if friend_id:
                    self.conn.execute(
                        "UPDATE friends SET status='active' WHERE id=?", (friend_id,)
                    )
                row = self.conn.execute(
                    "SELECT * FROM friendships WHERE id=?", (existing["id"],)
                ).fetchone()
            return {
                "friendship": self._friendship_public(dict(row), {persona_id: persona}),
                "friend_id": friend_id,
                "created": True,
            }

        friend_payload = self._friend_row_for_persona(persona, status="active")
        friend_payload["request_message"] = _clean(request_message, 200)
        friend = self.db.create_friend({**friend_payload, "owner_user_id": user_id})
        now = _iso()
        with self.lock, self.conn:
            if existing:
                self.conn.execute(
                    "UPDATE friendships SET status='accepted', friend_id=?, updated_at=?"
                    " WHERE id=?",
                    (friend["id"], now, existing["id"]),
                )
                fid = existing["id"]
            else:
                fid = _uuid()
                self.conn.execute(
                    "INSERT INTO friendships (id, user_id, persona_id, status, friend_id,"
                    " request_message, created_at, updated_at)"
                    " VALUES (?,?,?,'accepted',?,?,?,?)",
                    (fid, user_id, persona_id, friend["id"], _clean(request_message, 200), now, now),
                )
            row = self.conn.execute("SELECT * FROM friendships WHERE id=?", (fid,)).fetchone()
        return {
            "friendship": self._friendship_public(dict(row), {persona_id: persona}),
            "friend_id": friend["id"],
            "created": True,
        }

    def remove_friend(self, user_id: str, persona_id: str) -> bool:
        friendship = self.get_friendship(user_id, persona_id)
        if not friendship:
            return False
        friend_id = str(friendship.get("friend_id") or "")
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM friendships WHERE id=?", (friendship["id"],))
        if friend_id:
            try:
                self.db.delete_friend(friend_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("删除好友失败 %s: %s", friend_id, exc)
        return True

    def _ensure_request_friend(self, user_id: str, persona_id: str, message: str) -> dict:
        """好友申请事件物化：建 friends(status=pending_request) + friendship(pending)。"""
        persona = self.persona_by_id(persona_id)
        if persona is None:
            raise KeyError(persona_id)
        existing = self.get_friendship(user_id, persona_id)
        if existing:
            return {"friendship": dict(existing), "friend_id": existing.get("friend_id") or ""}
        friend_payload = self._friend_row_for_persona(persona, status="pending_request")
        friend_payload["request_message"] = _clean(message, 200)
        friend = self.db.create_friend({**friend_payload, "owner_user_id": user_id})
        now = _iso()
        fid = _uuid()
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO friendships (id, user_id, persona_id, status, friend_id,"
                " request_message, created_at, updated_at)"
                " VALUES (?,?,?,'pending',?,?,?,?)",
                (fid, user_id, persona_id, friend["id"], _clean(message, 200), now, now),
            )
            row = self.conn.execute("SELECT * FROM friendships WHERE id=?", (fid,)).fetchone()
        return {"friendship": dict(row), "friend_id": friend["id"]}

    # ------------------------------------------------------------------ #
    # 社交事件
    # ------------------------------------------------------------------ #
    def _event_public(self, row: Any) -> dict:
        pmap = self._persona_map()
        persona = pmap.get(str(row["persona_id"])) or {}
        payload = _loads(row["payload"], {})
        return {
            "id": row["id"],
            "type": row["event_type"],
            "status": row["status"],
            "is_read": bool(row["is_read"]),
            "created_at": row["created_at"],
            "scheduled_at": row["scheduled_at"],
            "post_id": row["post_id"],
            "payload": payload,
            "persona": {
                "id": persona.get("id", row["persona_id"]),
                "nickname": persona.get("nickname", "AI 人物"),
                "avatar": persona.get("avatar", ""),
                "bio": persona.get("bio", ""),
                "is_ai": True,
                "ai_label": "AI 人物",
            },
        }

    def _existing_events(self, user_id: str, post_id: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM social_events WHERE user_id=? AND post_id=? ORDER BY created_at",
                (user_id, post_id),
            ).fetchall()
        return [self._event_public(r) for r in rows]

    def _already_evaluated(self, user_id: str, post_id: str) -> bool:
        with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM social_event_attempts WHERE user_id=? AND post_id=?"
                " AND event_type='evaluated' LIMIT 1",
                (user_id, post_id),
            ).fetchone()
        return row is not None

    def _recent_persona_event_at(self, user_id: str) -> dict[str, float]:
        """每个 persona 最近一次事件时间（做冷却判定）。"""
        with self.lock:
            rows = self.conn.execute(
                "SELECT persona_id, MAX(created_at) AS at FROM social_events WHERE user_id=?"
                " GROUP BY persona_id",
                (user_id,),
            ).fetchall()
        out: dict[str, float] = {}
        for row in rows:
            try:
                out[str(row["persona_id"])] = datetime.fromisoformat(
                    str(row["at"]).replace("Z", "+00:00")
                ).timestamp()
            except (TypeError, ValueError):
                continue
        return out

    def _events_last_day(self, user_id: str) -> int:
        since = _iso(_now() - timedelta(hours=24))
        with self.lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM social_events WHERE user_id=? AND created_at>=?",
                (user_id, since),
            ).fetchone()
        return int(row["n"]) if row else 0

    def _record_attempt(self, user_id: str, post_id: str, attempt: dict) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO social_event_attempts (id, user_id, post_id, persona_id, event_type,"
                " reason, roll, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    _uuid(), user_id, post_id, str(attempt.get("persona_id") or ""),
                    str(attempt.get("event_type") or "no_action"),
                    str(attempt.get("reason") or ""), float(attempt.get("roll") or 0.0), _iso(),
                ),
            )

    async def trigger_social_events(self, user_id: str, post: dict) -> dict:
        """用户发布公开动态后触发（幂等，后台任务调用）。

        - 私有/好友可见动态**不触发**公共社交事件。
        - 已评估过的动态直接返回既有事件，不重复生成。
        """
        post_id = str(post.get("id") or "")
        visibility = str(post.get("visibility") or "public")
        if visibility != engine.PUBLIC_VISIBILITY:
            return {"events": [], "skipped": "not_public"}
        if not self.config.enabled:
            return {"events": [], "skipped": "disabled"}
        if self._already_evaluated(user_id, post_id):
            return {"events": self._existing_events(user_id, post_id), "skipped": "evaluated"}

        # 标记「已评估」先落库：并发重复触发也只会有一个生效（UNIQUE 由 attempt 行保证）
        marker = {"persona_id": "", "event_type": "evaluated", "reason": "evaluated", "roll": 0.0}
        self._record_attempt(user_id, post_id, marker)

        total_today = self._events_last_day(user_id)
        if total_today >= self.config.max_events_per_user_day:
            self._record_attempt(user_id, post_id, {
                "event_type": "no_action", "reason": "user_daily_limit", "roll": 0.0,
            })
            return {"events": [], "skipped": "daily_limit"}

        tags = engine.extract_tags(
            str(post.get("content") or ""),
            post.get("tags") or [],
            self._profile_interests(user_id),
        )
        pmap = self._persona_map()
        friends_accepted = self._accepted_persona_ids(user_id)
        recent = self._recent_persona_event_at(user_id)
        cooldown = self.config.persona_cooldown_s
        now_ts = _now().timestamp()
        cooled = {
            pid for pid, at in recent.items() if cooldown > 0 and (now_ts - at) < cooldown
        }
        candidates = [
            c for c in engine.rank_candidates(
                list(pmap.values()),
                tags,
                is_friend=lambda p: str(p.get("id")) in friends_accepted,
                limit=self.config.max_candidates,
                min_score=self.config.min_match_score,
            )
            if str((c.get("persona") or {}).get("id")) not in cooled
        ]

        remaining_slots = max(
            0, self.config.max_events_per_post - len(self._existing_events(user_id, post_id))
        )
        remaining_user = max(0, self.config.max_events_per_user_day - total_today)
        planned, attempts = engine.plan_events(
            candidates,
            rng=self.rng,
            config=self.config,
            remaining_slots=min(remaining_slots, remaining_user),
        )
        for attempt in attempts:
            self._record_attempt(user_id, post_id, attempt)

        created: list[dict] = []
        for item in planned[: max(0, min(remaining_slots, remaining_user))]:
            persona = pmap.get(item.persona_id) or {}
            try:
                payload, status = await self._build_event_payload(user_id, item, persona, post)
            except Exception as exc:  # noqa: BLE001 - 事件生成失败不能影响发布
                log.warning("社交事件内容生成失败（%s），跳过", exc)
                continue
            event_id = _uuid()
            dedupe = f"{post_id}:{item.persona_id}:{item.event_type}"
            scheduled = _iso(_now() + timedelta(seconds=max(0, item.delay_s)))
            with self.lock, self.conn:
                # 频率限制在**写入同一事务内**复核，避免并发触发（多条动态同时发布）突破每日上限
                if self.config.max_events_per_user_day > 0:
                    since = _iso(_now() - timedelta(hours=24))
                    count_row = self.conn.execute(
                        "SELECT COUNT(*) AS n FROM social_events WHERE user_id=? AND created_at>=?",
                        (user_id, since),
                    ).fetchone()
                    if int(count_row["n"] or 0) >= self.config.max_events_per_user_day:
                        self._record_attempt(user_id, post_id, {
                            "event_type": "no_action", "reason": "user_daily_limit", "roll": 0.0,
                        })
                        break
                self.conn.execute(
                    "INSERT OR IGNORE INTO social_events (id, user_id, persona_id, post_id,"
                    " event_type, payload, status, is_read, dedupe_key, scheduled_at, created_at)"
                    " VALUES (?,?,?,?,?,?,?,0,?,?,?)",
                    (event_id, user_id, item.persona_id, post_id, item.event_type,
                     _dumps(payload), status, dedupe, scheduled, _iso()),
                )
                row = self.conn.execute(
                    "SELECT * FROM social_events WHERE dedupe_key=?", (dedupe,)
                ).fetchone()
            if row is not None:
                created.append(self._event_public(row))

        return {"events": created, "skipped": "" if created else "no_event", "tags": tags}

    def _profile_interests(self, user_id: str) -> list[str]:
        if user_id != LOCAL_USER_ID:
            return []
        try:
            fact = (self.db.get_profile() or {}).get("fact_sheet") or {}
            return [str(x) for x in (fact.get("interests") or [])]
        except Exception:  # noqa: BLE001
            return []

    def _accepted_persona_ids(self, user_id: str) -> set[str]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT persona_id FROM friendships WHERE user_id=? AND status='accepted'",
                (user_id,),
            ).fetchall()
        return {str(r["persona_id"]) for r in rows}

    async def _build_event_payload(
        self, user_id: str, item: engine.PlannedEvent, persona: dict, post: dict
    ) -> tuple[dict, str]:
        """生成事件内容（LLM 优先，模板兜底）。"""
        content = str(post.get("content") or "")
        excerpt = _clean(content, 80)
        if item.event_type == "like":
            return {
                "reaction": "like",
                "persona_name": persona.get("nickname", ""),
                "post_excerpt": excerpt,
            }, "delivered"
        if item.event_type == "comment":
            text = await self._llm_comment(persona, content)
            return {
                "comment": text,
                "persona_name": persona.get("nickname", ""),
                "post_excerpt": excerpt,
            }, "delivered"
        # friend_request
        message = await self._llm_request_message(persona, content)
        return {
            "message": message,
            "persona_name": persona.get("nickname", ""),
            "post_excerpt": excerpt,
        }, "pending"

    async def _llm_comment(self, persona: dict, content: str) -> str:
        rng = self.rng
        fallback = _fallback_comment(persona, {}, rng)
        llm = getattr(self.engines, "llm", None)
        if llm is None or getattr(llm, "uses_mock", False):
            return fallback
        try:
            from app.v2.llm_json import complete_json_safe  # noqa: PLC0415
            from app.v3.seed_data import LANGUAGE_NAMES  # noqa: PLC0415

            name = persona.get("nickname") or "someone"
            lang_code = str(persona.get("language") or "en").strip().lower()
            lang_name = LANGUAGE_NAMES.get(lang_code, "English")
            personality = _clean(persona.get("personality"), 500)
            system = (
                f"You are {name}, an AI persona on a language-learning social app. "
                "You just read a stranger's public post. Write ONE short, natural comment "
                f"**in {lang_name}** (max 20 words), like a real social app comment. "
                "Stay in character. Do not explain, do not mention being an AI. "
                'Output JSON only: {"comment": "..."}'
            )
            if personality:
                system += f"\nYour personality: {personality}"
            data = await complete_json_safe(
                llm,
                [{"role": "system", "content": system},
                 {"role": "user", "content": f"Post: {_clean(content, 400)}"}],
                schema_hint='{"comment": "short comment"}',
                fallback=lambda: {"comment": fallback},
                temperature=0.9,
                max_tokens=200,
            )
            text = _clean((data or {}).get("comment"), 160)
            return text or fallback
        except Exception as exc:  # noqa: BLE001
            log.debug("评论生成失败: %s", exc)
            return fallback

    async def _llm_request_message(self, persona: dict, content: str) -> str:
        rng = self.rng
        fallback = _fallback_request(persona, rng)
        llm = getattr(self.engines, "llm", None)
        if llm is None or getattr(llm, "uses_mock", False):
            return fallback
        try:
            from app.v2.llm_json import complete_json_safe  # noqa: PLC0415
            from app.v3.seed_data import LANGUAGE_NAMES  # noqa: PLC0415

            name = persona.get("nickname") or "someone"
            lang_code = str(persona.get("language") or "en").strip().lower()
            lang_name = LANGUAGE_NAMES.get(lang_code, "English")
            personality = _clean(persona.get("personality"), 400)
            system = (
                f"You are {name}, an AI persona on a language-learning social app. "
                "You saw a stranger's public post that matched your interests and want to add them. "
                f"Write ONE short friend request message **in {lang_name}** (max 25 words). "
                "Stay in character. Output JSON only: "
                '{"message": "..."}'
            )
            if personality:
                system += f"\nYour personality: {personality}"
            data = await complete_json_safe(
                llm,
                [{"role": "system", "content": system},
                 {"role": "user", "content": f"Post: {_clean(content, 400)}"}],
                schema_hint='{"message": "short request"}',
                fallback=lambda: {"message": fallback},
                temperature=0.85,
                max_tokens=200,
            )
            text = _clean((data or {}).get("message"), 200)
            return text or fallback
        except Exception as exc:  # noqa: BLE001
            log.debug("好友申请语生成失败: %s", exc)
            return fallback

    def materialize_due_events(self, user_id: str) -> int:
        """把到期的 pending 事件真正落地（点赞/评论/好友申请），幂等。"""
        now = _iso()
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM social_events WHERE user_id=? AND scheduled_at<=?"
                " AND processed_at IS NULL ORDER BY scheduled_at",
                (user_id, now),
            ).fetchall()
        done = 0
        for row in rows:
            try:
                self._materialize_one(user_id, row)
                done += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("事件物化失败 %s: %s", row["id"], exc)
        return done

    def _materialize_one(self, user_id: str, row: Any) -> None:
        event_id = str(row["id"])
        etype = str(row["event_type"])
        post_id = str(row["post_id"])
        persona_id = str(row["persona_id"])
        payload = _loads(row["payload"], {})
        now = _iso()
        with self.lock, self.conn:
            if etype == "like":
                self.conn.execute(
                    "INSERT OR IGNORE INTO user_post_likes (id, post_id, persona_id, created_at)"
                    " VALUES (?,?,?,?)",
                    (_uuid(), post_id, persona_id, now),
                )
                self.conn.execute(
                    "UPDATE user_posts SET like_count=(SELECT COUNT(*) FROM user_post_likes"
                    " WHERE post_id=?) WHERE id=?",
                    (post_id, post_id),
                )
            elif etype == "comment":
                text = str(payload.get("comment") or "").strip() or "Nice one!"
                exists = self.conn.execute(
                    "SELECT 1 FROM user_post_comments WHERE post_id=? AND persona_id=?"
                    " AND content=? LIMIT 1",
                    (post_id, persona_id, text),
                ).fetchone()
                if exists is None:
                    self.conn.execute(
                        "INSERT INTO user_post_comments (id, post_id, persona_id, content, created_at)"
                        " VALUES (?,?,?,?,?)",
                        (_uuid(), post_id, persona_id, text, now),
                    )
                self.conn.execute(
                    "UPDATE user_posts SET comment_count=(SELECT COUNT(*) FROM user_post_comments"
                    " WHERE post_id=?) WHERE id=?",
                    (post_id, post_id),
                )
            elif etype == "friend_request":
                message = str(payload.get("message") or "")
                self._ensure_request_friend(user_id, persona_id, message)
            self.conn.execute(
                "UPDATE social_events SET processed_at=? WHERE id=?", (now, event_id)
            )

    def list_events(self, user_id: str, *, unread_only: bool = False, limit: int = 50) -> dict:
        self.materialize_due_events(user_id)
        limit = max(1, min(int(limit), 100))
        sql = "SELECT * FROM social_events WHERE user_id=? AND status<>'declined' AND scheduled_at<=?"
        params: list[Any] = [user_id, _iso()]
        if unread_only:
            sql += " AND is_read=0"
        sql += " ORDER BY scheduled_at DESC LIMIT ?"
        params.append(limit)
        with self.lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return {
            "items": [self._event_public(r) for r in rows],
            "unread": self.unread_event_count(user_id),
        }

    def unread_event_count(self, user_id: str) -> int:
        with self.lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM social_events WHERE user_id=? AND is_read=0"
                " AND status<>'declined' AND scheduled_at<=?",
                (user_id, _iso()),
            ).fetchone()
        return int(row["n"]) if row else 0

    def mark_event_read(self, user_id: str, event_id: str, *, read: bool = True) -> bool:
        with self.lock, self.conn:
            cur = self.conn.execute(
                "UPDATE social_events SET is_read=? WHERE id=? AND user_id=?",
                (1 if read else 0, event_id, user_id),
            )
            return cur.rowcount > 0

    def mark_all_events_read(self, user_id: str) -> int:
        with self.lock, self.conn:
            cur = self.conn.execute(
                "UPDATE social_events SET is_read=1 WHERE user_id=? AND is_read=0", (user_id,)
            )
            return cur.rowcount

    def respond_friend_request(
        self, user_id: str, event_id: str, *, accept: bool
    ) -> dict | None:
        """处理好友申请事件：只有接受才建立正式好友关系。"""
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM social_events WHERE id=? AND user_id=?", (event_id, user_id)
            ).fetchone()
        if row is None:
            return None
        if str(row["event_type"]) != "friend_request":
            return None
        persona_id = str(row["persona_id"])
        friendship = self.get_friendship(user_id, persona_id)
        if friendship is None:
            # 事件还没物化（或历史数据）：先补建申请关系
            payload = _loads(row["payload"], {})
            self._ensure_request_friend(user_id, persona_id, str(payload.get("message") or ""))
            friendship = self.get_friendship(user_id, persona_id)

        if accept:
            if friendship is None:
                result = self.add_friend(user_id, persona_id)
                friend_id = result["friend_id"]
            else:
                friend_id = str(friendship.get("friend_id") or "")
                now = _iso()
                with self.lock, self.conn:
                    self.conn.execute(
                        "UPDATE friendships SET status='accepted', updated_at=? WHERE id=?",
                        (now, friendship["id"]),
                    )
                    if friend_id:
                        self.conn.execute(
                            "UPDATE friends SET status='active' WHERE id=?", (friend_id,)
                        )
            status = "accepted"
        else:
            now = _iso()
            friend_id = str((friendship or {}).get("friend_id") or "")
            with self.lock, self.conn:
                if friendship is not None:
                    self.conn.execute("DELETE FROM friendships WHERE id=?", (friendship["id"],))
                self.conn.execute(
                    "UPDATE social_events SET status='declined', is_read=1, processed_at=? WHERE id=?",
                    (now, event_id),
                )
            if friend_id:
                try:
                    self.db.delete_friend(friend_id)
                except Exception as exc:  # noqa: BLE001
                    log.warning("拒绝申请时删除好友失败 %s: %s", friend_id, exc)
            return {"status": "declined", "friend_id": ""}

        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE social_events SET status=?, is_read=1 WHERE id=?", (status, event_id)
            )
        return {"status": status, "friend_id": friend_id}

    def stats(self, user_id: str) -> dict:
        with self.lock:
            attempts = self.conn.execute(
                "SELECT event_type, reason, COUNT(*) AS n FROM social_event_attempts"
                " WHERE user_id=? GROUP BY event_type, reason",
                (user_id,),
            ).fetchall()
            events = self.conn.execute(
                "SELECT event_type, status, COUNT(*) AS n FROM social_events"
                " WHERE user_id=? GROUP BY event_type, status",
                (user_id,),
            ).fetchall()
        return {
            "config": self.config.public(),
            "attempts": [dict(r) for r in attempts],
            "events": [dict(r) for r in events],
        }


__all__ = ["FEED_PAGE", "MAX_FEED_PAGE", "V3Service", "VISIBILITIES"]
