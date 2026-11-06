"""SpeakEasy V3 · 世界生成（一次捏出「人物 + 信息流」）。

产品模型
--------
V3 的「捏人」只发生一次：**生成世界**——同时生成 N 个 AI 人物和他们的动态。
之后用户浏览信息流、偶遇某个人，都是在这个已生成的世界里挑选，不会重复捏人。

两种来源
--------
- ``seed``（默认）：:mod:`app.v3.seed_data` 的预置语料（12 人物 / 36 动态），
  零模型调用、可复现，保证离线也能跑。
- ``llm``：``SE_WORLD_GEN=llm`` 时，空库首次启动会用 LLM 生成人物档案 + 动态，
  失败自动回落预置语料（永不空信息流）。

生成结果写入 ``v3_meta``（``world_source`` / ``world_generated_at``），
``GET /api/v3/health`` 可以查看当前世界来自哪里。
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

from app.logging_utils import get_logger
from app.v3.schema import (
    SEED_KEY,
    SEED_VERSION,
    backfill_post_languages,
    seed_if_needed,
    sync_persona_friends,
)
from app.v3.seed_data import PERSONAS, POSTS

log = get_logger(__name__)

WORLD_SOURCE_KEY = "world_source"
WORLD_AT_KEY = "world_generated_at"


def world_path() -> str:
    return (os.getenv("SE_WORLD_GEN", "seed") or "seed").strip().lower()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


def world_status(db: Any) -> dict:
    with db._lock:  # noqa: SLF001 - 只读
        rows = {
            str(r["key"]): str(r["value"])
            for r in db.conn.execute("SELECT key, value FROM v3_meta").fetchall()
        }
        personas = db.conn.execute("SELECT COUNT(*) AS n FROM personas").fetchone()["n"]
        posts = db.conn.execute("SELECT COUNT(*) AS n FROM persona_posts").fetchone()["n"]
    return {
        "source": rows.get(WORLD_SOURCE_KEY) or "seed",
        "generated_at": rows.get(WORLD_AT_KEY) or "",
        "personas": int(personas or 0),
        "posts": int(posts or 0),
        "seed_version": SEED_VERSION,
    }


def _mark(db: Any, source: str) -> None:
    with db._lock, db.conn:  # noqa: SLF001
        for key, value in ((WORLD_SOURCE_KEY, source), (WORLD_AT_KEY, _now_iso())):
            db.conn.execute(
                "INSERT INTO v3_meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        db.conn.execute(
            "INSERT INTO v3_meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SEED_KEY, SEED_VERSION),
        )


def _persona_rows() -> list[dict]:
    return list(PERSONAS)


def _post_rows() -> list[tuple[str, str, list[str], list[str]]]:
    return list(POSTS)


# --------------------------------------------------------------------------- #
# LLM 世界生成
# --------------------------------------------------------------------------- #
_WORLD_PROMPT = """You are designing the AI cast of a language-learning social app.
Create ONE fictional AI persona and 2 short social posts written in their voice.

Rules:
- The persona is clearly an AI character living a plausible daily life abroad.
- **All fields and posts must be written in the persona's native language** (`language`).
  Examples: Korean persona -> Korean text; Italian persona -> Italian text.
- Posts must sound like that exact person: hobbies, job, humour, city.
- Each post <= 240 characters, first person, no hashtags.
- interests: 3-4 lowercase english tags (e.g. coffee, running, books) for matching only.

Output JSON only:
{"nickname","avatar","bio","personality","interests":[...],"language":"en|ja|ko|de|es|it|pt|zh",
 "country","city","timezone","occupation","age","gender":"male|female",
 "posts":[{"content","tags":[...]}]}
"""

#: 语言 → 可用声线（保证文字与朗读同语言）
_VOICE_BY_LANG: dict[str, list[str]] = {
    "en": ["en-GB-RyanNeural", "en-US-AvaMultilingualNeural", "en-AU-WilliamMultilingualNeural"],
    "ja": ["ja-JP-KeitaNeural", "ja-JP-NanamiNeural"],
    "ko": ["ko-KR-InJoonNeural", "ko-KR-SunHiNeural"],
    "de": ["de-DE-FlorianMultilingualNeural", "de-DE-SeraphinaMultilingualNeural"],
    "es": ["es-ES-AlvaroNeural", "es-ES-XimenaNeural"],
    "it": ["it-IT-GiuseppeMultilingualNeural", "it-IT-IsabellaNeural"],
    "pt": ["pt-BR-AntonioNeural", "pt-BR-ThalitaMultilingualNeural"],
    "zh": ["zh-CN-YunxiNeural", "zh-CN-XiaoyiNeural"],
    "fr": ["fr-FR-RemyMultilingualNeural", "fr-FR-VivienneMultilingualNeural"],
}

_AVATARS = [
    "/mock/avatars/adrian.jpg", "/mock/avatars/annika.jpg", "/mock/avatars/callum.jpg",
    "/mock/avatars/camila.jpg", "/mock/avatars/finn.jpg", "/mock/avatars/giulia.jpg",
    "/mock/avatars/haruto.jpg", "/mock/avatars/ines.jpg", "/mock/avatars/jiwoo.jpg",
    "/mock/avatars/lin.jpg", "/mock/avatars/mei.jpg", "/mock/avatars/morten.jpg",
]


def _insert_persona(db: Any, persona: dict, posts: list[dict]) -> int:
    """写入一位人物和 TA 的动态，返回写入的帖子数。"""
    import json
    from datetime import datetime, timedelta, timezone

    pid = f"p_{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)
    lang = str(persona.get("language") or "en").strip().lower()
    if lang not in _VOICE_BY_LANG:
        lang = "en"
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO personas (id, nickname, avatar, bio, personality, interests, language,"
            " country, city, timezone, occupation, age, gender, voice_id, archetype_id,"
            " ai_config, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                pid,
                str(persona.get("nickname") or "Friend")[:40],
                str(persona.get("avatar") or "/mock/avatars/lin.jpg"),
                str(persona.get("bio") or "")[:400],
                str(persona.get("personality") or "")[:600],
                json.dumps(persona.get("interests") or [], ensure_ascii=False),
                lang,
                str(persona.get("country") or "")[:60],
                str(persona.get("city") or "")[:60],
                str(persona.get("timezone") or "UTC")[:64],
                str(persona.get("occupation") or "")[:80],
                int(persona.get("age") or 27),
                str(persona.get("gender") or "female"),
                str(persona.get("voice_id") or _VOICE_BY_LANG[lang][0]),
                "",
                json.dumps({"generated": "llm"}, ensure_ascii=False),
                "active",
                _now_iso(),
            ),
        )
        written = 0
        for index, post in enumerate(posts[:3]):
            content = str(post.get("content") or "").strip()[:400]
            if not content:
                continue
            post_id = f"pp_{uuid.uuid4().hex[:12]}"
            created = now - timedelta(hours=1 + index * 6 + (written % 5))
            db.conn.execute(
                "INSERT INTO persona_posts (id, persona_id, content, media, tags, language,"
                " visibility, like_count, comment_count, created_at)"
                " VALUES (?,?,?, '[]', ?, ?, 'public', 0, 0, ?)",
                (
                    post_id, pid, content,
                    json.dumps(post.get("tags") or persona.get("interests") or []),
                    lang,
                    created.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                ),
            )
            written += 1
    return written


async def generate_world_llm(db: Any, llm: Any, *, count: int | None = None) -> dict:
    """用 LLM 生成整个世界（人物 + 动态）。失败自动回落预置语料。"""
    total = int(count or os.getenv("SE_WORLD_PERSONAS", "8") or 8)
    total = max(1, min(total, 16))
    created_personas = 0
    created_posts = 0

    try:
        from app.v2.llm_json import complete_json_safe  # noqa: PLC0415

        if llm is None or getattr(llm, "uses_mock", False):
            raise RuntimeError("LLM 不可用（mock/未配置）")
    except Exception as exc:  # noqa: BLE001
        log.warning("世界生成回落预置语料：%s", exc)
        return {"source": "seed", "personas": 0, "posts": seed_if_needed(db), "fallback": True}

    for index in range(total):
        try:
            data = await complete_json_safe(
                llm,
                [{"role": "system", "content": _WORLD_PROMPT},
                 {"role": "user", "content": f"Create persona #{index + 1} of {total}."}],
                schema_hint='{"nickname","bio","personality","interests":[],"posts":[{"content","tags":[]}]}',
                fallback=lambda: {},
                temperature=1.0,
                max_tokens=900,
            )
            if not isinstance(data, dict) or not data.get("nickname"):
                continue
            gender = "male" if str(data.get("gender", "")).lower() == "male" else "female"
            lang = str(data.get("language") or "en").strip().lower()
            if lang not in _VOICE_BY_LANG:
                lang, gender = "en", "female"
            data["language"] = lang
            voices = _VOICE_BY_LANG[lang]
            data.setdefault("voice_id", voices[index % len(voices)])
            data.setdefault("avatar", _AVATARS[index % len(_AVATARS)])
            posts = data.get("posts") or []
            if not isinstance(posts, list):
                posts = []
            created_posts += _insert_persona(db, data, posts)
            created_personas += 1
        except Exception as exc:  # noqa: BLE001 - 单个失败不影响整体
            log.warning("世界生成第 %d 个人物失败: %s", index + 1, exc)

    if created_personas == 0:
        log.warning("LLM 世界生成全部失败，回落预置语料")
        return {"source": "seed", "personas": 0, "posts": seed_if_needed(db), "fallback": True}

    _mark(db, "llm")
    log.info("LLM 世界生成完成：%d 人物 / %d 动态", created_personas, created_posts)
    return {"source": "llm", "personas": created_personas, "posts": created_posts, "fallback": False}


async def ensure_world(db: Any, llm: Any) -> dict:
    """启动时确保世界存在：LLM 模式先生成，失败/未开启则用预置语料。

    已有世界时：
    - 来源是预置语料 → 调用 ``seed_if_needed`` 做**版本内 upsert**
      （语料升级，例如把英文世界升级为多语言世界，不会重复插入）
    - 来源是 LLM → 保持原样，不覆盖
    """
    status = world_status(db)
    if status["personas"] > 0:
        if status["source"] == "seed":
            seed_if_needed(db)  # 版本一致时内部直接返回 0
        if not status["generated_at"]:
            _mark(db, status["source"] or "seed")
        # 每次启动都同步：人物语言/声线 → 已加好友；动态语言缺失 → 回填
        sync_persona_friends(db)
        backfill_post_languages(db)
        return world_status(db)

    if world_path() == "llm":
        await generate_world_llm(db, llm)
    written = seed_if_needed(db)
    if written:
        _mark(db, "seed")
    sync_persona_friends(db)
    backfill_post_languages(db)
    return world_status(db)


__all__ = [
    "WORLD_AT_KEY",
    "WORLD_SOURCE_KEY",
    "ensure_world",
    "generate_world_llm",
    "world_path",
    "world_status",
]
