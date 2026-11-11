"""管理台「角色工坊」：全量重捏 / 单角色属性编辑 / 新建测试角色。

设计原则
--------
- **原地重捏（reroll）**：保留 persona id、好友关系、聊天记录，只重掷人设/头像/声音，
  适合"换一批脸和声音但继续用"。
- **彻底重建（recreate）**：先备份数据库，再删除全部 AI 角色及动态/示例/关系/好友/聊天，
  然后按素材池重新生成 N 个新角色（可一键绑定给所有用户）。用户账号不受影响。
- **单角色编辑**：personas 全部字段（含 Speech DNA / ai_config JSON）可改；
  改完同步到 V2 ``friends`` 行（聊天/朗读/通话同源）。
- 全部逻辑**不调用 LLM**（重掷用素材池 + 固定模板），保证管理台操作快且可控。
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.logging_utils import get_logger

log = get_logger(__name__)

EDITABLE_FIELDS = (
    "nickname",
    "avatar_seed",
    "avatar_style",
    "avatar_options",
    "bio",
    "personality",
    "interests",
    "language",
    "country",
    "city",
    "timezone",
    "occupation",
    "age",
    "gender",
    "voice_id",
    "fish_voice_id",
    "archetype_id",
    "ai_config",
    "status",
    "lifecycle",
    "expires_at",
    "last_interaction_at",
    "speech_dna",
    "emotion_state",
)

JSON_FIELDS = ("interests", "avatar_options", "ai_config", "speech_dna", "emotion_state")

LANGUAGES = ["en", "zh", "ja", "ko", "de", "es", "it", "pt", "fr"]


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _db_path(db: Any) -> str:
    return str(getattr(db, "path", "") or "")


def backup_db(db: Any) -> str:
    """把数据库复制到 data/_backup/，返回备份目录。"""
    src = _db_path(db)
    if not src or not os.path.exists(src):
        return ""
    data_dir = os.path.dirname(os.path.abspath(src))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest_dir = os.path.join(data_dir, "_backup", f"personas-{stamp}")
    os.makedirs(dest_dir, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        path = src + suffix
        if os.path.exists(path):
            shutil.copy2(path, os.path.join(dest_dir, os.path.basename(path)))
    return dest_dir


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if str(value or "").strip() else default
    except (TypeError, ValueError):
        return default


def invalidate(service: Any) -> None:
    if service is None:
        return
    try:
        service._persona_cache = {"at": 0.0, "rows": []}  # noqa: SLF001
        service._feed_cache = {"at": 0.0, "key": "", "items": []}  # noqa: SLF001
    except Exception:  # noqa: BLE001
        pass


def _counts(db: Any, ids: list[str] | None = None) -> dict[str, int]:
    where, args = "", []
    if ids is not None:
        where = " WHERE persona_id IN (%s)" % ",".join("?" * len(ids))
        args = list(ids)
    out: dict[str, int] = {}
    with db._lock:  # noqa: SLF001
        for table in (
            "personas",
            "persona_posts",
            "persona_example_dialogs",
            "persona_events",
            "persona_relationships",
            "friendships",
        ):
            try:
                if table == "personas":
                    sql = "SELECT COUNT(*) FROM personas"
                    out["personas"] = int(db.conn.execute(sql).fetchone()[0])
                elif table == "persona_relationships" and ids is not None:
                    marks = "(" + ",".join("?" * len(ids)) + ")"
                    out[table] = int(
                        db.conn.execute(
                            "SELECT COUNT(*) FROM persona_relationships"
                            f" WHERE persona_a IN {marks} OR persona_b IN {marks}",
                            list(ids) + list(ids),
                        ).fetchone()[0]
                    )
                else:
                    out[table] = int(
                        db.conn.execute(
                            f"SELECT COUNT(*) FROM {table}{where}", args
                        ).fetchone()[0]
                    )
            except sqlite3.Error:
                out[table] = -1
        for table in ("user_post_likes", "user_post_comments", "social_events"):
            try:
                out[table] = int(
                    db.conn.execute(f"SELECT COUNT(*) FROM {table}{where}", args).fetchone()[0]
                )
            except sqlite3.Error:
                out[table] = -1
        try:
            out["v3_users"] = int(db.conn.execute("SELECT COUNT(*) FROM v3_users").fetchone()[0])
        except sqlite3.Error:
            out["v3_users"] = 0
        try:
            out["friends"] = int(
                db.conn.execute(
                    "SELECT COUNT(*) FROM friends WHERE COALESCE(persona_id, '') != ''"
                ).fetchone()[0]
            )
        except sqlite3.Error:
            out["friends"] = -1
    return out


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
def list_personas(db: Any, *, query: str = "", limit: int = 300) -> list[dict[str, Any]]:
    sql = (
        "SELECT p.id, p.nickname, p.gender, p.age, p.language, p.country, p.city,"
        " p.occupation, p.fish_voice_id, p.voice_id, p.status, p.lifecycle,"
        " p.created_at, p.avatar_seed, p.avatar_style, p.archetype_id,"
        " (SELECT COUNT(*) FROM friendships f WHERE f.persona_id=p.id) AS friend_count,"
        " (SELECT COUNT(*) FROM persona_posts pp WHERE pp.persona_id=p.id) AS post_count,"
        " (SELECT COUNT(*) FROM persona_example_dialogs e WHERE e.persona_id=p.id) AS example_count"
        " FROM personas p"
    )
    args: list[Any] = []
    if query.strip():
        like = f"%{query.strip()}%"
        sql += " WHERE p.nickname LIKE ? OR p.id LIKE ? OR p.city LIKE ? OR p.country LIKE ?"
        args = [like, like, like, like]
    sql += " ORDER BY p.created_at DESC, p.nickname LIMIT ?"
    args.append(max(1, min(int(limit or 300), 1000)))
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(sql, args).fetchall()
    names = _voice_name_map()
    out = []
    for row in rows:
        item = dict(row)
        item["fish_voice"] = item.get("fish_voice_id") or ""
        item["fish_voice_name"] = names.get(str(item.get("fish_voice_id") or ""), "")
        item.pop("fish_voice_id", None)
        out.append(item)
    return out


def list_users(db: Any, limit: int = 100) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        with db._lock:  # noqa: SLF001
            rows = db.conn.execute(
                "SELECT u.id, u.device_key, u.display_name, u.last_seen_at,"
                " (SELECT COUNT(*) FROM friendships f WHERE f.user_id=u.id AND f.status='accepted') AS friend_count"
                " FROM v3_users u ORDER BY COALESCE(NULLIF(u.last_seen_at,''), u.created_at) DESC LIMIT ?",
                (max(1, min(int(limit or 100), 500)),),
            ).fetchall()
        for row in rows:
            item = dict(row)
            device = str(item.get("device_key") or "")
            item["device_key_masked"] = (device[:8] + "…" + device[-4:]) if len(device) > 14 else device
            item.pop("device_key", None)
            out.append(item)
    except sqlite3.Error:
        return []
    return out


def persona_detail(db: Any, persona_id: str) -> dict[str, Any] | None:
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT * FROM personas WHERE id=?", (persona_id,)
        ).fetchone()
    if not row:
        return None
    persona = dict(row)
    for field in JSON_FIELDS:
        if field in persona:
            persona[field] = _loads(persona[field], [] if field == "interests" else {})
    counts = _counts(db, [persona_id])
    with db._lock:  # noqa: SLF001
        posts = [
            dict(r)
            for r in db.conn.execute(
                "SELECT id, content, created_at, like_count, comment_count"
                " FROM persona_posts WHERE persona_id=? ORDER BY created_at DESC LIMIT 5",
                (persona_id,),
            ).fetchall()
        ]
        examples = int(
            db.conn.execute(
                "SELECT COUNT(*) FROM persona_example_dialogs WHERE persona_id=?",
                (persona_id,),
            ).fetchone()[0]
        )
    persona["fish_voice_name"] = _voice_name_map().get(str(persona.get("fish_voice_id") or ""), "")
    persona["_counts"] = {
        "friends": counts.get("friendships", 0),
        "posts": counts.get("persona_posts", 0),
        "examples": examples,
    }
    persona["_recent_posts"] = posts
    return persona


# --------------------------------------------------------------------------- #
# 写：单角色
# --------------------------------------------------------------------------- #
def _sync_friend_rows(db: Any, persona: dict[str, Any]) -> int:
    """把 persona 的属性同步到 V2 ``friends`` 行（只更新存在的列）。"""
    try:
        with db._lock:  # noqa: SLF001
            columns = {
                str(r[1])
                for r in db.conn.execute("PRAGMA table_info(friends)").fetchall()
            }
    except sqlite3.Error:
        return 0
    if not columns:
        return 0
    mapping = {
        "name": str(persona.get("nickname") or ""),
        "avatar_seed": str(persona.get("avatar_seed") or ""),
        "avatar_style": str(persona.get("avatar_style") or ""),
        "avatar_options": _json(persona.get("avatar_options") or {}),
        "archetype_id": str(persona.get("archetype_id") or ""),
        "voice_id": str(persona.get("voice_id") or ""),
        "fish_voice_id": str(persona.get("fish_voice_id") or ""),
        "nationality": str(persona.get("country") or ""),
        "city": str(persona.get("city") or ""),
        "timezone": str(persona.get("timezone") or "UTC"),
        "native_lang": str(persona.get("language") or "en"),
        "age": int(persona.get("age") or 25),
    }
    fields = [(k, v) for k, v in mapping.items() if k in columns]
    if not fields:
        return 0
    assignments = ", ".join(f"{k}=?" for k, _ in fields)
    params = [v for _, v in fields] + [persona["id"]]
    updated = 0
    try:
        with db._lock, db.conn:  # noqa: SLF001
            updated += db.conn.execute(
                f"UPDATE friends SET {assignments} WHERE persona_id=?", params
            ).rowcount
            updated += db.conn.execute(
                f"UPDATE friends SET {assignments} WHERE id IN"
                " (SELECT friend_id FROM friendships WHERE persona_id=?)",
                params,
            ).rowcount
    except sqlite3.Error as exc:
        log.debug("同步 friends 行失败: %s", exc)
        return 0
    return int(updated or 0)


def _voice_name_map() -> dict[str, str]:
    try:
        from app.admin import voice_pool  # noqa: PLC0415

        return {v["reference_id"]: v["name"] for v in voice_pool.load_pool()}
    except Exception:  # noqa: BLE001
        return {}


def _resolve_voice(value: Any) -> tuple[str, bool]:
    """把名称/id 解析成 reference_id。返回 (id, 是否解析成功)。"""
    raw = str(value or "").strip()
    if not raw:
        return "", True
    try:
        from app.admin import voice_pool  # noqa: PLC0415

        hit = voice_pool.resolve(raw)
    except Exception:  # noqa: BLE001
        return raw, True
    if hit:
        return hit["reference_id"], True
    # 长得像手输 id 就原样保留；否则说明是名称但没匹配上，拒绝保存防手误
    looks_like_id = bool(re.fullmatch(r"[0-9A-Za-z_-]{8,}", raw))
    return raw, looks_like_id


def _avatar_options(persona: dict[str, Any]) -> dict[str, Any]:
    from app.v3.avatar_style import default_options  # noqa: PLC0415

    try:
        return default_options({**persona})
    except Exception:  # noqa: BLE001
        return {}


def update_persona(db: Any, persona_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    current = persona_detail(db, persona_id)
    if not current:
        return {"ok": False, "issues": ["角色不存在"]}
    sets: list[str] = []
    args: list[Any] = []
    if "fish_voice_id" in payload:
        resolved, ok = _resolve_voice(payload.get("fish_voice_id"))
        if not ok:
            return {
                "ok": False,
                "issues": [
                    f"素材池里找不到音色「{payload.get('fish_voice_id')}」（可填名称或 reference_id）"
                ],
            }
        payload = {**payload, "fish_voice_id": resolved}
    if os.getenv("SE_VOICE_STRICT", "0").lower() in ("1", "true", "yes") and any(
        key in payload for key in ("voice_id", "fish_voice_id")
    ):
        from app.voice.catalog import verified_voice

        current = persona_detail(db, persona_id) or {}
        merged = {**current, **payload}
        provider, reference = (("fish", str(merged.get("fish_voice_id") or ""))
                               if merged.get("fish_voice_id") else ("edge", str(merged.get("voice_id") or "")))
        column = "fish_voice_id" if provider == "fish" else "voice_id"
        with db._lock:
            collision = db.conn.execute(
                f"SELECT 1 FROM personas WHERE {column}=? AND id<>? AND status='active'", (reference, persona_id)
            ).fetchone()
        if collision:
            return {"ok": False, "issues": ["voice_already_assigned"]}
        if not verified_voice(db, provider, reference):
            payload = {**payload, "status": "waiting_voice"}
    for field in EDITABLE_FIELDS:
        if field not in payload:
            continue
        value = payload[field]
        if field in JSON_FIELDS:
            if not isinstance(value, (dict, list, str)):
                value = _json(value)
            elif isinstance(value, (dict, list)):
                value = _json(value)
        elif field in ("age", "interaction_count"):
            try:
                value = int(value)
            except (TypeError, ValueError):
                return {"ok": False, "issues": [f"{field} 必须是整数"]}
        elif field in ("nickname", "language", "gender") and not str(value or "").strip():
            return {"ok": False, "issues": [f"{field} 不能为空"]}
        sets.append(f"{field}=?")
        args.append(value)
    if not sets:
        return {"ok": False, "issues": ["没有要修改的字段"]}
    args.append(persona_id)
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(f"UPDATE personas SET {', '.join(sets)} WHERE id=?", args)
    updated = persona_detail(db, persona_id) or {}
    synced = _sync_friend_rows(db, updated)
    return {"ok": True, "persona": updated, "friends_synced": synced}


def delete_persona(db: Any, service: Any, persona_id: str, *, backup: bool = True, conversation_store: Any = None) -> dict[str, Any]:
    detail = persona_detail(db, persona_id)
    if not detail:
        return {"ok": False, "issues": ["角色不存在"]}
    backup_path = backup_db(db) if backup else ""
    counts = _purge_personas(db, [persona_id])
    _clear_conversations(conversation_store, [persona_id])
    invalidate(service)
    return {"ok": True, "backup": backup_path, "deleted": counts}


# --------------------------------------------------------------------------- #
# 重掷（素材池 + 模板，不调 LLM）
# --------------------------------------------------------------------------- #
def _roll_profile(language: str, index: int, day: str, *, gender: str = "") -> dict[str, Any]:
    from app.v3 import daily_world  # noqa: PLC0415

    lang = language if language in daily_world._FALLBACK_LANG else "en"  # noqa: SLF001
    pools = daily_world._world_pools()  # noqa: SLF001
    entries = daily_world._name_entries(pools, lang)  # noqa: SLF001
    base = daily_world._fallback_persona(index, day, lang)  # noqa: SLF001
    target_gender = str(gender or "").strip().lower()
    if target_gender in ("female", "male"):
        same = [e for e in entries if str(e.get("gender") or "").lower() == target_gender]
        if same:
            entries = same
    entry: dict[str, Any] = {}
    if entries:
        entry = entries[(index + int(time.time_ns() % 9973)) % len(entries)]
    nickname = str(entry.get("name") or base.get("nickname") or "Friend")
    rolled_gender = str(entry.get("gender") or gender or base.get("gender") or "female")
    country = str(entry.get("country") or base.get("country") or "")
    occupations = daily_world._pool_items(pools, "occupations", lang)  # noqa: SLF001
    interests = daily_world._pool_items(pools, "interests", lang)  # noqa: SLF001
    city = daily_world._city_for(pools, country, index, day) or base.get("city") or ""  # noqa: SLF001
    return {
        "nickname": nickname[:40],
        "gender": rolled_gender,
        "age": 20 + (abs(hash(f"{day}:{index}:{time.time_ns()}")) % 22),
        "country": country[:60],
        "city": str(city)[:60],
        "language": lang,
        "occupation": (
            daily_world._pick_sequence(occupations, index, f"job:{day}")  # noqa: SLF001
            or base.get("occupation")
            or "student"
        )[:80],
        "interests": (
            daily_world._pick_many(interests, index, 4, f"interest:{day}")  # noqa: SLF001
            or ["coffee", "books", "walking", "travel"]
        ),
        "bio": str(base.get("bio") or "")[:400],
        "personality": str(base.get("personality") or "curious, calm, a little funny")[:600],
    }


def _roll_voice(db: Any, language: str, gender: str, *, exclude: set[str] | None = None) -> str:
    from app.admin import voice_pool  # noqa: PLC0415

    try:
        picked = voice_pool.pick_from_pool(language, gender, exclude or set(), randomize=True)
        if picked:
            return picked
        pool = voice_pool.load_pool()
        if pool:
            candidates = [v["reference_id"] for v in pool if v["reference_id"] not in (exclude or set())]
            if not candidates:
                candidates = [v["reference_id"] for v in pool]
            if candidates:
                return random.choice(candidates)
    except Exception:  # noqa: BLE001
        pass
    return ""


def reroll_persona(
    db: Any,
    persona_id: str,
    *,
    regen_voice: bool = True,
    regen_avatar: bool = True,
    overrides: dict[str, Any] | None = None,
    gender: str = "",
) -> dict[str, Any]:
    persona = persona_detail(db, persona_id)
    if not persona:
        return {"ok": False, "issues": ["角色不存在"]}
    lang = str(persona.get("language") or "en")
    index = int(abs(hash(persona_id)) % 10000)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rolled = _roll_profile(lang, index, day, gender=gender)
    voice = persona.get("fish_voice_id") or ""
    if regen_voice:
        used_others: set[str] = set()
        with db._lock:  # noqa: SLF001
            for row in db.conn.execute(
                "SELECT DISTINCT fish_voice_id FROM personas WHERE id<>? AND COALESCE(fish_voice_id,'')<>''",
                (persona_id,),
            ).fetchall():
                used_others.add(str(row[0]))
        voice = (
            _roll_voice(db, language=rolled["language"], gender=rolled["gender"], exclude=used_others)
            or ""
        )
    updates: dict[str, Any] = {
        "nickname": rolled["nickname"],
        "gender": rolled["gender"],
        "age": rolled["age"],
        "country": rolled["country"],
        "city": rolled["city"],
        "occupation": rolled["occupation"],
        "interests": rolled["interests"],
        "bio": rolled["bio"],
        "personality": rolled["personality"],
        "archetype_id": "",
    }
    if regen_voice and voice:
        updates["fish_voice_id"] = voice
        updates["voice_id"] = ""
    if regen_avatar:
        updates["avatar_seed"] = f"persona-{persona_id}"
        updates["avatar_options"] = _avatar_options({**persona, **updates, "id": persona_id})
    if overrides:
        for key, value in overrides.items():
            if key in EDITABLE_FIELDS:
                updates[key] = value
    result = update_persona(db, persona_id, updates)
    if result.get("ok"):
        result["before"] = {
            "nickname": persona.get("nickname"),
            "gender": persona.get("gender"),
            "city": persona.get("city"),
            "fish_voice_id": persona.get("fish_voice_id"),
        }
    return result


def create_persona(
    db: Any,
    service: Any,
    payload: dict[str, Any],
    *,
    bind_user_id: str = "",
    posts: list[str] | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    pid = "p_custom_" + uuid.uuid4().hex[:10]
    language = str(payload.get("language") or "en").strip().lower()
    gender = str(payload.get("gender") or "female").strip().lower()
    nickname = str(payload.get("nickname") or "").strip()
    if not nickname:
        return {"ok": False, "issues": ["昵称不能为空"]}
    voice, voice_ok = _resolve_voice(payload.get("fish_voice_id"))
    if not voice_ok:
        return {"ok": False, "issues": [f"素材池里找不到音色「{payload.get('fish_voice_id')}」"]}
    if not voice:
        voice = _roll_voice(db, language=language, gender=gender)
    edge_voice = str(payload.get("voice_id") or "").strip()
    if not edge_voice:
        from app.v3.seed_data import voice_for_language  # noqa: PLC0415

        edge_voice = voice_for_language(language, seed=pid, gender=gender)
    base = {
        "id": pid,
        "nickname": nickname[:40],
        "avatar": "",
        "avatar_seed": f"persona-{pid}",
        "avatar_style": str(payload.get("avatar_style") or "avataaars"),
        "avatar_options": _json(payload.get("avatar_options") or {}),
        "bio": str(payload.get("bio") or "")[:400],
        "personality": str(payload.get("personality") or "")[:600],
        "interests": _json(payload.get("interests") or []),
        "language": language,
        "country": str(payload.get("country") or "")[:60],
        "city": str(payload.get("city") or "")[:60],
        "timezone": str(payload.get("timezone") or "UTC"),
        "occupation": str(payload.get("occupation") or "")[:80],
        "age": int(payload.get("age") or 25),
        "gender": gender,
        "voice_id": edge_voice,
        "fish_voice_id": voice,
        "archetype_id": str(payload.get("archetype_id") or ""),
        "ai_config": _json(payload.get("ai_config") or {"source": "admin-test"}),
        "status": ("waiting_voice" if os.getenv("SE_VOICE_STRICT", "0").lower() in ("1", "true", "yes")
                   else str(payload.get("status") or "active")),
        "created_at": now,
        "lifecycle": str(payload.get("lifecycle") or "permanent"),
        "expires_at": str(payload.get("expires_at") or ""),
        "speech_dna": _json(payload.get("speech_dna") or {}),
        "emotion_state": _json(payload.get("emotion_state") or {}),
    }
    if not base["avatar_options"] or base["avatar_options"] == "{}":
        base["avatar_options"] = _json(_avatar_options({**base}))
    columns = ", ".join(base.keys())
    marks = ",".join("?" * len(base))
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            f"INSERT INTO personas ({columns}) VALUES ({marks})", list(base.values())
        )
        for idx, text in enumerate((posts or [])[:5]):
            content = str(text or "").strip()[:400]
            if not content:
                continue
            created = (
                datetime.now(timezone.utc) - timedelta(minutes=35 + idx * 53)
            ).isoformat(timespec="seconds")
            db.conn.execute(
                "INSERT INTO persona_posts (id, persona_id, content, media, tags, language,"
                " visibility, like_count, comment_count, created_at, expires_at)"
                " VALUES (?,?,?, '[]', '[]', ?, 'public', 0, 0, ?, '')",
                (f"pp_{uuid.uuid4().hex[:12]}", pid, content, language, created),
            )

    bound = 0
    if bind_user_id and service is not None:
        invalidate(service)  # 新角色刚插入，先让 service 的 60s 人物缓存失效
        try:
            service.add_friend(bind_user_id, pid, request_message="")
            bound = 1
            invalidate(service)
        except Exception as exc:  # noqa: BLE001
            log.warning("测试角色绑定好友失败: %s", exc)
    return {"ok": True, "persona": persona_detail(db, pid), "bound": bound}


# --------------------------------------------------------------------------- #
# 全量重捏 / 彻底重建
# --------------------------------------------------------------------------- #
def rebuild_preview(db: Any) -> dict[str, Any]:
    with db._lock:  # noqa: SLF001
        ids = [
            str(r[0])
            for r in db.conn.execute("SELECT id FROM personas").fetchall()
        ]
    counts = _counts(db, ids)
    try:
        import os as _os  # noqa: PLC0415

        conv_path = _os.path.join(
            _os.path.dirname(_db_path(db)), "conversations.json"
        )
        counts["conversations_file"] = 1 if _os.path.exists(conv_path) else 0
    except Exception:  # noqa: BLE001
        counts["conversations_file"] = -1
    return {"counts": counts, "persona_count": len(ids)}


# --------------------------------------------------------------------------- #
# 动态素材（本地语言模板 + 种子语料，不调 LLM）
# --------------------------------------------------------------------------- #
_POST_BANK_CACHE: dict[str, list[str]] = {}


def _seed_posts_by_language() -> dict[str, list[str]]:
    try:
        from app.v3.seed_data import PERSONAS, POSTS  # noqa: PLC0415

        lang_by_pid = {str(p.get("id")): str(p.get("language") or "").lower() for p in PERSONAS}
        out: dict[str, list[str]] = {}
        for item in POSTS:
            try:
                pid, content = str(item[0]), str(item[1]).strip()
            except (IndexError, TypeError):
                continue
            lang = lang_by_pid.get(pid, "")
            if content and lang:
                out.setdefault(lang, []).append(content)
        return out
    except Exception:  # noqa: BLE001
        return {}


def _post_bank(language: str) -> list[str]:
    """每种语言的一批候选动态：生活模板 + 兜底人设文案 + 种子语料。"""
    from app.v3 import daily_world  # noqa: PLC0415

    lang = language if language in daily_world._FALLBACK_LANG else "en"  # noqa: SLF001
    if lang in _POST_BANK_CACHE:
        return _POST_BANK_CACHE[lang]
    bank: list[str] = []
    try:
        from app.v3.life_posts import _FALLBACK_LIFE, _fallback as _lp_fallback  # noqa: PLC0415

        if lang in _FALLBACK_LIFE:
            for seed in ("a", "b", "c", "d"):
                text = str(_lp_fallback(_FALLBACK_LIFE, lang, seed) or "").strip()
                if text:
                    bank.append(text)
    except Exception:  # noqa: BLE001
        pass
    try:
        spec = daily_world._FALLBACK_LANG.get(lang, {})  # noqa: SLF001
        for raw in spec.get("posts") or []:
            text = str(raw.get("content") if isinstance(raw, dict) else raw or "").strip()
            if text:
                bank.append(text)
    except Exception:  # noqa: BLE001
        pass
    for text in _seed_posts_by_language().get(lang, []):
        if text:
            bank.append(text)
    seen: set[str] = set()
    unique: list[str] = []
    for text in bank:
        if text in seen:
            continue
        seen.add(text)
        unique.append(text)
    _POST_BANK_CACHE[lang] = unique
    return unique


def _media_data_dir() -> str:
    try:
        from app.config import get_settings  # noqa: PLC0415

        return os.path.join(str(get_settings().data_path), "media")
    except Exception:  # noqa: BLE001
        return "data/media"


def _media_bank(db: Any, *, data_dir: str | None = None) -> list[dict[str, Any]]:
    """返回本地真实存在的图库条目：[{path, alt, credit}]（写进 persona_posts.media）。"""
    root = data_dir or _media_data_dir()
    out: list[dict[str, Any]] = []
    try:
        with db._lock:  # noqa: SLF001
            rows = db.conn.execute(
                "SELECT path, COALESCE(title,'') AS title, COALESCE(tags,'[]') AS tags,"
                " COALESCE(author,'') AS author,"
                " COALESCE(license,'') AS license, COALESCE(source_url,'') AS source_url"
                " FROM media_library ORDER BY fetched_at DESC LIMIT 300"
            ).fetchall()
    except sqlite3.Error:
        return out
    for row in rows:
        item = dict(row)
        path = str(item.get("path") or "")
        rel = path[len("/media/"):] if path.startswith("/media/") else path.lstrip("/")
        if not rel or not os.path.isfile(os.path.join(root, rel)):
            continue
        tags: list[str] = []
        try:
            raw_tags = json.loads(str(item.get("tags") or "[]"))
            if isinstance(raw_tags, list):
                tags = [str(t) for t in raw_tags if str(t).strip()][:12]
        except (TypeError, ValueError):
            tags = []
        out.append(
            {
                "path": path,
                "alt": str(item.get("title") or "")[:200],
                "tags": tags,
                "credit": {
                    "author": str(item.get("author") or ""),
                    "license": str(item.get("license") or ""),
                    "url": str(item.get("source_url") or ""),
                },
            }
        )
    return out



#: 图片/文案的场景词（多语言），用于「文字 ↔ 图片」语义匹配
_SCENE_WORDS: dict[str, tuple[str, ...]] = {
    "coffee": ("coffee", "cafe", "café", "espresso", "latte", "cappuccino", "kaffee",
               "kaffeehaus", "kettle", "tea", "tee", "cup", "tasse", "barista"),
    "food": ("food", "meal", "pasta", "tagliatelle", "brunch", "cake", "bread", "essen",
             "nudeln", "frühstück", "kuchen", "dinner", "lunch", "restaurant", "kitchen",
             "küche", "teller", "plate", "pizza", "ramen", "sushi", "cook", "kochen"),
    "beach": ("beach", "sea", "ocean", "strand", "meer", "wave", "welle", "surf", "coast",
              "küste", "sand"),
    "city": ("city", "street", "stadt", "straße", "strasse", "building", "gebäude",
             "architektur", "architecture", "road", "platz", "town", "downtown", "skyline"),
    "night": ("night", "nacht", "sunset", "sonnenuntergang", "sky", "himmel", "evening",
              "abend", "lights", "lichter", "dusk", "mond", "moon"),
    "pets": ("cat", "katze", "kitten", "dog", "hund", "pet", "haustier"),
    "books": ("book", "buch", "read", "lesen", "library", "bibliothek", "novel", "roman"),
    "music": ("music", "musik", "guitar", "gitarre", "record", "schallplatte", "piano",
              "klavier", "concert", "konzert", "band"),
    "art": ("art", "kunst", "drawing", "sketch", "zeichnung", "painting", "gemälde",
            "museum", "design", "poster"),
    "nature": ("flower", "blume", "garden", "garten", "park", "tree", "baum", "plant",
               "pflanze", "forest", "wald", "blüte"),
    "work": ("desk", "schreibtisch", "laptop", "office", "büro", "computer", "work",
             "arbeit", "meeting", "projekt", "project"),
    "travel": ("train", "zug", "station", "bahnhof", "travel", "reise", "airport",
               "flughafen", "luggage", "koffer", "window", "fenster"),
    "fitness": ("run", "running", "laufen", "jog", "bike", "fahrrad", "gym", "fitness",
                "sport", "yoga", "hike", "wanderung"),
}
_SCENE_INDEX: dict[str, str] = {}
for _scene, _words in _SCENE_WORDS.items():
    for _w in _words:
        _SCENE_INDEX[_w] = _scene


def _scenes_of(text: str) -> set[str]:
    found: set[str] = set()
    for token in re.findall(r"[a-zäöüßéèàçñ']+", str(text or "").lower()):
        scene = _SCENE_INDEX.get(token)
        if scene:
            found.add(scene)
    return found


def _match_image_for_text(
    text: str, images: list[dict[str, Any]], used: set[str]
) -> dict[str, Any] | None:
    """给一段文案挑一张场景相符的图；不相关就返回 None（宁可不配图也不乱配）。"""
    want = _scenes_of(text)
    if not want or not images:
        return None
    best: dict[str, Any] | None = None
    best_score = 0
    for image in images:
        if image["path"] in used:
            continue
        have = _scenes_of(
            str(image.get("title") or image.get("alt") or "")
            + " "
            + " ".join(image.get("tags") or [])
        )
        score = len(want & have)
        if score > best_score:
            best, best_score = image, score
    return best



def _insert_varied_posts(
    db: Any,
    persona_id: str,
    language: str,
    *,
    count: int = 5,
    now: datetime | None = None,
) -> int:
    """给一个角色插入 N 条动态：内容按语言素材库随机、时间错峰；约 1/3 带图。"""
    bank = _post_bank(language)
    if not bank:
        return 0
    images = _media_bank(db)
    rng = random.Random(f"{persona_id}:{time.time_ns()}")
    posts = list(bank)
    rng.shuffle(posts)
    take = posts[: min(max(1, count), len(posts))]
    base = now or datetime.now(timezone.utc)
    inserted = 0
    used_images: set[str] = set()
    with db._lock, db.conn:  # noqa: SLF001
        for idx, content in enumerate(take):
            created = base - timedelta(minutes=rng.randint(35, 4000) + idx * 11)
            media: list[str] = []
            alt_text = ""
            post_kind = "life_text"
            source_ref: dict[str, Any] = {}
            image = _match_image_for_text(content, images, used_images) if idx % 3 == 0 else None
            if image is not None:
                used_images.add(image["path"])
                media = [image["path"]]
                alt_text = image["alt"]
                post_kind = "life_photo"
                source_ref = {"kind": "stock_photo", **image["credit"]}
            db.conn.execute(
                "INSERT INTO persona_posts (id, persona_id, content, media, tags, language,"
                " visibility, like_count, comment_count, created_at, expires_at,"
                " post_kind, alt_text, source_ref)"
                " VALUES (?,?,?, ?, '[]', ?, 'public', 0, 0, ?, '', ?, ?, ?)",
                (
                    f"pp_{uuid.uuid4().hex[:12]}",
                    persona_id,
                    content[:400],
                    _json(media),
                    str(language or "en")[:8],
                    created.isoformat(timespec="seconds"),
                    post_kind,
                    alt_text,
                    _json(source_ref) if source_ref else "",
                ),
            )
            inserted += 1
    return inserted



def _purge_personas(db: Any, ids: list[str]) -> dict[str, int]:
    """删除这些 persona 及全部关联数据（不做备份，调用方负责）。"""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    deleted: dict[str, int] = {}
    with db._lock, db.conn:  # noqa: SLF001
        statements: list[tuple[str, list[Any]]] = [
            (
                f"DELETE FROM persona_post_likes WHERE post_id IN"
                f" (SELECT id FROM persona_posts WHERE persona_id IN ({marks}))",
                ids,
            ),
            (
                f"DELETE FROM persona_post_comments WHERE post_id IN"
                f" (SELECT id FROM persona_posts WHERE persona_id IN ({marks}))"
                f" OR author_persona_id IN ({marks})",
                ids + ids,
            ),
            (f"DELETE FROM persona_posts WHERE persona_id IN ({marks})", ids),
            (f"DELETE FROM persona_example_dialogs WHERE persona_id IN ({marks})", ids),
            (f"DELETE FROM persona_events WHERE persona_id IN ({marks})", ids),
            (
                f"DELETE FROM persona_relationships WHERE persona_a IN ({marks})"
                f" OR persona_b IN ({marks})",
                ids + ids,
            ),
            (f"DELETE FROM persona_schedules WHERE persona_id IN ({marks})", ids),
            (f"DELETE FROM user_post_likes WHERE persona_id IN ({marks})", ids),
            (f"DELETE FROM user_post_comments WHERE persona_id IN ({marks})", ids),
            (f"DELETE FROM social_events WHERE persona_id IN ({marks})", ids),
            (f"DELETE FROM social_event_attempts WHERE persona_id IN ({marks})", ids),
            (f"DELETE FROM v3_discover_views WHERE persona_id IN ({marks})", ids),
            (f"DELETE FROM friendships WHERE persona_id IN ({marks})", ids),
            (f"DELETE FROM personas WHERE id IN ({marks})", ids),
        ]
        for sql, args in statements:
            try:
                cursor = db.conn.execute(sql, args)
                deleted[sql.split(" FROM ")[1].split(" ")[0]] = int(cursor.rowcount or 0)
            except sqlite3.Error as exc:
                log.debug("清理失败（可忽略）: %s -> %s", sql[:60], exc)
        try:
            deleted["friends"] = int(
                db.conn.execute(
                    f"DELETE FROM friends WHERE persona_id IN ({marks})", ids
                ).rowcount
                or 0
            )
        except sqlite3.Error:
            pass
        try:
            db.conn.execute("DELETE FROM daily_world")
        except sqlite3.Error:
            pass
        # 关键：清掉「当天生活事件已生成」的幂等标记。
        # 否则重建删了事件、标记还在 → life_loop 认为今天已经生成过，永远不再补。
        try:
            db.conn.execute(
                "DELETE FROM v3_meta WHERE key LIKE 'life_events_day:%'"
                " OR key LIKE 'event_posts%' OR key LIKE 'life_posts%'"
            )
        except sqlite3.Error:
            pass
    return deleted


def _clear_conversations(store: Any, ids: list[str]) -> int:
    if store is None:
        return 0
    cleared = 0
    for pid in ids:
        try:
            if store.clear(pid):
                cleared += 1
        except Exception:  # noqa: BLE001
            pass
    return cleared


def _create_bulk(
    db: Any, count: int, *, languages: list[str], auto_friend_users: bool = False
) -> list[str]:
    """按素材池批量生成新角色（不调 LLM）：音色均匀分配 + 每人多条错峰动态。"""
    from app.v3 import daily_world  # noqa: PLC0415
    from app.v3.seed_data import voice_for_language  # noqa: PLC0415

    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    langs = [lang for lang in (languages or LANGUAGES) if lang in daily_world._FALLBACK_LANG]  # noqa: SLF001
    if not langs:
        langs = ["en"]
    created_ids: list[str] = []
    used_voices: set[str] = set()
    for index in range(max(1, count)):
        lang = langs[index % len(langs)]
        target_gender = "female" if index % 2 == 0 else "male"
        base = daily_world._fallback_persona(index, day, lang, gender=target_gender)  # noqa: SLF001
        pid = "p_re_" + uuid.uuid4().hex[:10]
        gender = str(base.get("gender") or "female")
        voice = _roll_voice(db, language=lang, gender=gender, exclude=used_voices)
        if voice:
            used_voices.add(voice)
        edge_voice = voice_for_language(lang, seed=pid, gender=gender)
        persona = {
            "id": pid,
            "nickname": str(base.get("nickname") or "Friend")[:40],
            "avatar": "",
            "avatar_seed": "persona-" + pid[2:],
            "avatar_style": "avataaars",
            "avatar_options": _json(
                _avatar_options(
                    {"id": pid, "nickname": base.get("nickname"), "gender": gender, "age": base.get("age")}
                )
            ),
            "bio": str(base.get("bio") or "")[:400],
            "personality": str(base.get("personality") or "")[:600],
            "interests": _json(base.get("interests") or []),
            "language": lang,
            "country": str(base.get("country") or "")[:60],
            "city": str(base.get("city") or "")[:60],
            "timezone": "UTC",
            "occupation": str(base.get("occupation") or "")[:80],
            "age": int(base.get("age") or 27),
            "gender": gender,
            "voice_id": edge_voice,
            "fish_voice_id": voice,
            "archetype_id": "",
            "ai_config": _json({"generated": "admin-rebuild", "day": day}),
            "status": "waiting_voice" if os.getenv("SE_VOICE_STRICT", "0").lower() in ("1", "true", "yes") else "active",
            "created_at": now.isoformat(timespec="seconds"),
            "lifecycle": "permanent",
            "expires_at": "",
            "speech_dna": "{}",
            "emotion_state": "{}",
        }
        columns = ", ".join(persona.keys())
        marks = ",".join("?" * len(persona))
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                f"INSERT INTO personas ({columns}) VALUES ({marks})",
                list(persona.values()),
            )
        _insert_varied_posts(db, pid, lang, count=5, now=now)
        created_ids.append(pid)
    return created_ids


async def init_life_layer(db: Any) -> dict[str, Any]:
    """给全部角色补齐：作息表 + Speech DNA/示例对话 + 人物关系（0 次 LLM）。"""
    from app.v3 import life_events, speech_dna  # noqa: PLC0415

    with db._lock:  # noqa: SLF001
        rows = [dict(r) for r in db.conn.execute("SELECT * FROM personas WHERE status='active'").fetchall()]
    schedules = 0
    for row in rows:
        try:
            life_events.ensure_schedule(db, str(row["id"]))
            schedules += 1
        except Exception:  # noqa: BLE001
            pass
    relationships = 0
    try:
        relationships = int(life_events.ensure_relationships(db, rows) or 0)
    except Exception:  # noqa: BLE001
        pass
    dna: Any = {}
    try:
        dna = await speech_dna.ensure_persona_dna(db, None, rows, use_llm=False)
    except Exception as exc:  # noqa: BLE001
        dna = {"error": f"{type(exc).__name__}: {exc}"}
    return {"personas": len(rows), "schedules": schedules, "relationships": relationships, "dna": dna}


async def rebuild_all(
    db: Any,
    service: Any,
    *,
    mode: str = "reroll",
    count: int = 0,
    auto_friend_users: bool = False,
    confirm: str = "",
    conversation_store: Any = None,
) -> dict[str, Any]:
    preview = rebuild_preview(db)
    if mode == "reroll":
        backup = backup_db(db)
        with db._lock:  # noqa: SLF001
            ids = [str(r[0]) for r in db.conn.execute("SELECT id FROM personas").fetchall()]
        rolled = 0
        samples: list[dict[str, Any]] = []
        for pos, pid in enumerate(ids):
            res = reroll_persona(
                db,
                pid,
                regen_voice=True,
                regen_avatar=True,
                gender="female" if pos % 2 == 0 else "male",
            )
            if res.get("ok"):
                rolled += 1
                if len(samples) < 8:
                    samples.append(
                        {
                            "id": pid,
                            "before": res.get("before"),
                            "after": {
                                "nickname": res["persona"].get("nickname"),
                                "gender": res["persona"].get("gender"),
                                "city": res["persona"].get("city"),
                                "fish_voice_id": res["persona"].get("fish_voice_id"),
                            },
                        }
                    )
        invalidate(service)
        return {
            "ok": True,
            "mode": "reroll",
            "backup": backup,
            "before": preview["counts"],
            "rerolled": rolled,
            "samples": samples,
        }

    # ---- recreate ----
    if str(confirm or "").strip().upper() != "RESET":
        return {"ok": False, "issues": ["彻底重建需要输入 RESET 确认"]}
    backup = backup_db(db)
    with db._lock:  # noqa: SLF001
        ids = [str(r[0]) for r in db.conn.execute("SELECT id FROM personas").fetchall()]
    deleted = _purge_personas(db, ids)
    cleared = _clear_conversations(conversation_store, ids)
    languages: list[str] = []
    try:
        from app.v3 import daily_world  # noqa: PLC0415

        langs, _levels = daily_world._user_lang_prefs(db)  # noqa: SLF001
        languages = [lang for lang in langs if lang]
    except Exception:  # noqa: BLE001
        languages = []
    target = int(count or 0) or max(1, preview["persona_count"])
    created = _create_bulk(db, target, languages=languages or ["en", "ja", "zh"])
    bound = 0
    if auto_friend_users:
        invalidate(service)
        users = [u["id"] for u in list_users(db, limit=500)]
        for uid in users:
            for pid in created:
                try:
                    service.add_friend(uid, pid, request_message="")
                    bound += 1
                except Exception:  # noqa: BLE001
                    pass
    invalidate(service)
    life = await init_life_layer(db)
    invalidate(service)
    return {
        "ok": True,
        "mode": "recreate",
        "backup": backup,
        "before": preview["counts"],
        "deleted": deleted,
        "cleared_conversations": cleared,
        "created": len(created),
        "bound_friends": bound,
        "life_layer": life,
        "personas": list_personas(db, limit=min(len(created), 20)),
    }




async def _llm_caption(llm: Any, persona: dict[str, Any], image: dict[str, Any]) -> str:
    """让 LLM 按图片内容（标题/标签）用人物母语写一句配图文案。"""
    from app.v3 import life_posts as lp  # noqa: PLC0415

    facts = lp._persona_facts(persona)  # noqa: SLF001
    prompt = lp._IMAGE_PROMPT.format(  # noqa: SLF001
        persona=json.dumps(facts, ensure_ascii=False),
        title=str(image.get("title") or "(none)"),
        tags=", ".join(str(t) for t in (image.get("tags") or [])[:8]) or "(none)",
        language=str(persona.get("language") or "en"),
    )
    data = await lp._llm_json(llm, [{"role": "user", "content": prompt}], max_tokens=220)  # noqa: SLF001
    text = str(data.get("content") or "").strip()
    return text[:400]


async def rematch_media(db: Any, llm: Any, *, limit: int = 20) -> dict[str, Any]:
    """修正存量图文帖：用 LLM 按图片内容重写文案（保持人物语言/口吻）。"""
    from app.v3 import life_posts as lp  # noqa: PLC0415

    with db._lock:  # noqa: SLF001
        posts = [
            dict(r)
            for r in db.conn.execute(
                "SELECT id, persona_id, content, media, alt_text, source_ref FROM persona_posts"
                " WHERE media<>'[]' AND media<>''"
                " AND COALESCE(source_ref,'') NOT LIKE ?"
                " ORDER BY created_at DESC LIMIT ?",
                ('%caption%llm%', max(1, int(limit))),
            ).fetchall()
        ]
        personas = {
            str(r["id"]): dict(r)
            for r in db.conn.execute("SELECT * FROM personas").fetchall()
        }
        media_rows = {
            str(r["path"]): dict(r)
            for r in db.conn.execute("SELECT path, title, tags FROM media_library").fetchall()
        }
    fixed = 0
    failed = 0
    removed = 0
    samples: list[dict[str, Any]] = []
    for post in posts:
        try:
            media_paths = json.loads(str(post.get("media") or "[]"))
        except (TypeError, ValueError):
            media_paths = []
        if not media_paths:
            continue
        image = media_rows.get(str(media_paths[0]))
        persona = personas.get(str(post.get("persona_id")))
        if not persona:
            failed += 1
            continue
        if not image:
            # 图片库里没有这张图（老 mock 图/文件已丢）：直接降级为纯文字，避免图文不匹配
            with db._lock, db.conn:  # noqa: SLF001
                db.conn.execute(
                    "UPDATE persona_posts SET media='[]', alt_text='',"
                    " post_kind='life_text', source_ref=? WHERE id=?",
                    ('{"media_fix":"missing"}', post["id"]),
                )
            removed += 1
            continue
        content = ""
        for _try in range(2):
            try:
                content = await _llm_caption(llm, persona, image)
            except Exception:  # noqa: BLE001
                content = ""
            if content:
                break
        if not content:
            # LLM 失败时：宁可去掉图片，也不要保留「图文不匹配」的动态
            with db._lock, db.conn:  # noqa: SLF001
                db.conn.execute(
                    "UPDATE persona_posts SET media='[]', alt_text='',"
                    " post_kind='life_text', source_ref=? WHERE id=?",
                    ('{"media_fix":"removed"}', post["id"]),
                )
            removed += 1
            continue
        try:
            source_ref = json.loads(str(post.get("source_ref") or "{}"))
            if not isinstance(source_ref, dict):
                source_ref = {}
        except (TypeError, ValueError):
            source_ref = {}
        source_ref["caption"] = "llm"
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                "UPDATE persona_posts SET content=?, source_ref=?, alt_text=?"
                " WHERE id=?",
                (
                    content,
                    json.dumps(source_ref, ensure_ascii=False, separators=(",", ":")),
                    str(image.get("title") or "")[:200],
                    post["id"],
                ),
            )
        fixed += 1
        if len(samples) < 8:
            samples.append(
                {
                    "id": post["id"],
                    "image": str(image.get("title") or "")[:60],
                    "before": str(post.get("content") or "")[:60],
                    "after": content[:60],
                }
            )
    with db._lock:  # noqa: SLF001
        remaining = int(
            db.conn.execute(
                "SELECT COUNT(*) FROM persona_posts WHERE media<>'[]' AND media<>''"
                " AND COALESCE(source_ref,'') NOT LIKE ?",
                ('%caption%llm%',),
            ).fetchone()[0]
        )
    return {
        "ok": True,
        "fixed": fixed,
        "failed": failed,
        "removed": removed,
        "remaining": remaining,
        "samples": samples,
    }



def _studio_name_entries(pools: dict, language: str) -> list[dict[str, str]]:
    """An absent editable name pool falls back to the built-in persona catalog."""
    from app.v3 import daily_world
    from app.v2.voices import load_archetypes

    entries = daily_world._name_entries(pools, language)
    if entries:
        return entries
    fallback: list[dict[str, str]] = []
    for archetype in load_archetypes():
        locale = archetype.get("locale_info") or {}
        if str(locale.get("native_lang") or "en") != language:
            continue
        gender = str((archetype.get("vibe_constraints") or {}).get("gender") or "")
        for name in archetype.get("name_pool") or []:
            fallback.append({"name": str(name), "gender": gender, "country": str(locale.get("country") or "")})
    return fallback


def rebalance_genders(db: Any, service: Any, *, female_ratio: float = 0.5) -> dict[str, Any]:
    """把现有角色性别调整到大致 1:1（按语言/国家换同名性别的名字，同步好友行）。"""
    from app.v3 import daily_world as dw  # noqa: PLC0415

    ratio = min(0.9, max(0.1, float(female_ratio or 0.5)))
    with db._lock:  # noqa: SLF001
        rows = [dict(r) for r in db.conn.execute("SELECT * FROM personas WHERE status='active'").fetchall()]
    total = len(rows)
    if total < 2:
        return {"ok": True, "changed": 0, "total": total, "female": 0, "male": total}
    target_female = int(round(total * ratio))
    current_female = sum(1 for r in rows if str(r.get("gender") or "").lower() == "female")
    need = target_female - current_female
    pools = dw._world_pools()  # noqa: SLF001
    used_names = {str(r.get("nickname") or "").strip() for r in rows}
    changed = 0
    samples: list[dict[str, Any]] = []

    if need > 0:
        candidates = [r for r in rows if str(r.get("gender") or "").lower() != "female"]
        new_gender = "female"
    elif need < 0:
        candidates = [r for r in rows if str(r.get("gender") or "").lower() == "female"]
        new_gender = "male"
    else:
        candidates = []
        new_gender = ""

    for row in candidates:
        if changed >= abs(need):
            break
        lang = str(row.get("language") or "en").strip().lower()
        entries = _studio_name_entries(pools, lang)
        same_gender = [e for e in entries if str(e.get("gender") or "").lower() == new_gender]
        # 优先同国家，其次同语言，最后任意
        same_country = [
            e for e in same_gender if str(e.get("country") or "") == str(row.get("country") or "")
        ]
        pool = same_country or same_gender
        if not pool:
            for other in ("en", "de", "zh"):
                entries2 = _studio_name_entries(pools, other)
                pool = [e for e in entries2 if str(e.get("gender") or "").lower() == new_gender]
                if pool:
                    break
        if not pool:
            continue
        unused = [e for e in pool if str(e.get("name") or "") not in used_names]
        entry = (unused or pool)[int(abs(hash(str(row.get("id")))) % len(unused or pool))]
        nickname = str(entry.get("name") or "").strip()
        if not nickname:
            continue
        persona = persona_detail(db, str(row["id"]))
        if not persona:
            continue
        new_country = str(entry.get("country") or row.get("country") or "")
        updates = {
            "nickname": nickname[:40],
            "gender": new_gender,
            "country": new_country[:60],
            "avatar_seed": f"persona-{row['id']}",
            "avatar_options": _avatar_options(
                {
                    **persona,
                    "nickname": nickname,
                    "gender": new_gender,
                    "country": new_country,
                }
            ),
        }
        res = update_persona(db, str(row["id"]), updates)
        if res.get("ok"):
            changed += 1
            used_names.add(nickname)
            if len(samples) < 8:
                samples.append(
                    {
                        "id": row["id"],
                        "before": {"nickname": row.get("nickname"), "gender": row.get("gender")},
                        "after": {"nickname": nickname, "gender": new_gender},
                    }
                )

    deduped, dedupe_samples = _dedupe_nicknames(db)
    voices = redistribute_voices(db, service) if changed else {}
    invalidate(service)
    with db._lock:  # noqa: SLF001
        female = int(
            db.conn.execute(
                "SELECT COUNT(*) FROM personas WHERE status='active' AND gender='female'"
            ).fetchone()[0]
        )
        male = int(
            db.conn.execute(
                "SELECT COUNT(*) FROM personas WHERE status='active' AND gender='male'"
            ).fetchone()[0]
        )
    return {
        "ok": True,
        "total": total,
        "changed": changed,
        "deduped": deduped,
        "dedupe_samples": dedupe_samples,
        "female": female,
        "male": male,
        "samples": samples,
        "voices": {k: voices.get(k) for k in ("updated", "voices")} if voices else {},
    }




def _dedupe_nicknames(db: Any) -> tuple[int, list[dict[str, Any]]]:
    """清理重名角色：保留最早的一个，其余换成同性别/同语言的未占用名字。"""
    from app.v3 import daily_world as dw  # noqa: PLC0415

    with db._lock:  # noqa: SLF001
        rows = [
            dict(r)
            for r in db.conn.execute(
                "SELECT id, nickname, gender, language, country FROM personas"
                " WHERE status='active' ORDER BY created_at, id"
            ).fetchall()
        ]
    pools = dw._world_pools()  # noqa: SLF001
    used = {str(r.get("nickname") or "").strip() for r in rows if str(r.get("nickname") or "").strip()}
    seen: set[str] = set()
    changed = 0
    samples: list[dict[str, Any]] = []
    for row in rows:
        name = str(row.get("nickname") or "").strip()
        if not name:
            continue
        if name not in seen:
            seen.add(name)
            continue
        lang = str(row.get("language") or "en").strip().lower()
        gender = str(row.get("gender") or "female").strip().lower()
        entries = _studio_name_entries(pools, lang)
        pool = [
            e
            for e in entries
            if str(e.get("gender") or "").lower() == gender and str(e.get("name") or "") not in used
        ]
        if not pool:
            for other in ("en", "de", "zh"):
                entries = _studio_name_entries(pools, other)
                pool = [
                    e
                    for e in entries
                    if str(e.get("gender") or "").lower() == gender
                    and str(e.get("name") or "") not in used
                ]
                if pool:
                    break
        if not pool:
            continue
        entry = pool[int(abs(hash(str(row.get("id")))) % len(pool))]
        nickname = str(entry.get("name") or "").strip()
        persona = persona_detail(db, str(row["id"]))
        if not persona:
            continue
        res = update_persona(
            db,
            str(row["id"]),
            {
                "nickname": nickname[:40],
                "country": str(entry.get("country") or row.get("country") or "")[:60],
                "avatar_seed": f"persona-{row['id']}",
                "avatar_options": _avatar_options(
                    {**persona, "nickname": nickname, "country": entry.get("country") or row.get("country")}
                ),
            },
        )
        if res.get("ok"):
            used.add(nickname)
            seen.add(nickname)
            changed += 1
            if len(samples) < 8:
                samples.append({"id": row["id"], "before": name, "after": nickname})
    return changed, samples



def redistribute_voices(db: Any, service: Any) -> dict[str, Any]:
    """把素材池音色均匀重新分配给全部角色（最少使用优先，语言/性别匹配优先）。"""
    from app.admin import voice_pool  # noqa: PLC0415

    raw_pool = voice_pool.load_pool()
    strict = os.getenv("SE_VOICE_STRICT", "0").lower() in ("1", "true", "yes")
    pool = raw_pool
    if strict:
        from app.voice.catalog import verified_voice

        pool = [v for v in raw_pool if verified_voice(db, "fish", str(v.get("reference_id") or ""))]
    if not pool:
        if raw_pool:
            return {
                "ok": False,
                "issues": [
                    f"音色池里有 {len(raw_pool)} 个音色，但通过验证的为 0"
                    "（严格模式 SE_VOICE_STRICT=1，重分配只认已验证音色）。"
                    "先到「音色池」点『全部检测并标记已验证』，或关闭严格模式后再试。"
                ],
            }
        return {"ok": False, "issues": ["音色池为空，先到「音色池」加音色"]}
    with db._lock:  # noqa: SLF001
        rows = [
            dict(r)
            for r in db.conn.execute(
                "SELECT id, language, gender, fish_voice_id FROM personas ORDER BY created_at, id"
            ).fetchall()
        ]
    usage = {str(v["reference_id"]): 0 for v in pool}
    owners = {str(row["fish_voice_id"]): str(row["id"]) for row in rows if row["fish_voice_id"]}
    updated = 0
    for row in rows:
        lang = str(row.get("language") or "").lower()
        gender = str(row.get("gender") or "").lower()
        tiers = [
            [v for v in pool if str(v["language"]).lower() == lang and str(v["gender"]).lower() == gender],
            [v for v in pool if str(v["gender"]).lower() == gender],
            [v for v in pool if str(v["language"]).lower() == lang],
            pool,
        ]
        chosen = None
        for tier in tiers:
            available = [v for v in tier if not usage[str(v["reference_id"])]
                         and owners.get(str(v["reference_id"]), str(row["id"])) == str(row["id"])]
            if not available:
                continue
            chosen = random.choice(available)
            break
        if not chosen:
            continue
        rid = str(chosen["reference_id"])
        usage[rid] += 1
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                "UPDATE personas SET fish_voice_id=? WHERE id=?",
                (rid, row["id"]),
            )
        persona = persona_detail(db, row["id"])
        if persona:
            _sync_friend_rows(db, persona)
        updated += 1
    invalidate(service)
    distribution = sorted(
        (
            {
                "reference_id": rid,
                "name": next((v["name"] for v in pool if str(v["reference_id"]) == rid), rid),
                "count": n,
            }
            for rid, n in usage.items()
        ),
        key=lambda item: -item["count"],
    )
    return {"ok": True, "updated": updated, "unassigned": len(rows) - updated,
            "distribution": distribution[:20], "voices": len(pool)}


def regenerate_posts(db: Any, service: Any, *, per_persona: int = 5) -> dict[str, Any]:
    """重建全部角色的动态：每人 N 条，内容按语言素材库随机、时间错峰。"""
    with db._lock:  # noqa: SLF001
        rows = [
            dict(r)
            for r in db.conn.execute(
                "SELECT id, language FROM personas WHERE status='active'"
            ).fetchall()
        ]
        for row in rows:
            try:
                db.conn.execute(
                    "DELETE FROM persona_post_likes WHERE post_id IN"
                    " (SELECT id FROM persona_posts WHERE persona_id=?)",
                    (row["id"],),
                )
                db.conn.execute(
                    "DELETE FROM persona_post_comments WHERE post_id IN"
                    " (SELECT id FROM persona_posts WHERE persona_id=?)",
                    (row["id"],),
                )
                db.conn.execute("DELETE FROM persona_posts WHERE persona_id=?", (row["id"],))
            except sqlite3.Error:
                pass
    total = 0
    per: list[dict[str, Any]] = []
    for row in rows:
        inserted = _insert_varied_posts(
            db, str(row["id"]), str(row.get("language") or "en"), count=per_persona
        )
        total += inserted
        per.append({"id": row["id"], "posts": inserted})
    with_media = 0
    try:
        with db._lock:  # noqa: SLF001
            with_media = int(
                db.conn.execute(
                    "SELECT COUNT(*) FROM persona_posts WHERE media<>'[]' AND media<>''"
                ).fetchone()[0]
            )
    except sqlite3.Error:
        pass
    invalidate(service)
    return {
        "ok": True,
        "personas": len(rows),
        "posts": total,
        "posts_with_media": with_media,
        "per_persona": per_persona,
    }


__all__ = [
    "backup_db",
    "create_persona",
    "delete_persona",
    "invalidate",
    "list_personas",
    "list_users",
    "persona_detail",
    "rebuild_all",
    "init_life_layer",
    "rebuild_preview",
    "rebalance_genders",
    "rematch_media",
    "redistribute_voices",
    "regenerate_posts",
    "reroll_persona",
    "update_persona",
]
