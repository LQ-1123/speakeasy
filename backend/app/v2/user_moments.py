"""用户发布的动态 + AI 好友评论（Phase A）。

流程
----
用户 ``POST /api/v2/moments`` 后，后台 ``generate_comments`` 会挑 2~3 位活跃好友，
按各自 ``reply_language`` 生成 1 句短评论（最多 1 个 emoji），写入 ``moment_comments``。
前端轮询 ``GET /api/v2/moments`` 就能看到评论逐条出现。

设计
----
- 无 LLM Key / 调用失败 / 语言跑偏：用各语言的确定性模板兜底，保证评论一定有；
- 幂等：同一好友对同一动态只会写一条评论，重复触发直接返回已有；
- 好友选择：优先熟稔度高/聊得多的，再按语言/国家稍微打散，避免永远同一个人。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from app.logging_utils import get_logger
from app.v2.llm_json import complete_json_safe

log = get_logger(__name__)

MAX_FRIENDS = 3
MAX_COMMENT_CHARS = 160

_LANG_NAMES: dict[str, str] = {
    "zh": "简体中文",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "pt": "Português",
    "es": "Español",
    "de": "Deutsch",
    "fr": "Français",
    "it": "Italiano",
}

_FALLBACK_COMMENTS: dict[str, tuple[str, ...]] = {
    "zh": (
        "哈哈哈，太真实了 😂",
        "看着就舒服，今天也辛苦啦 ☕",
        "懂你，这种小日子最治愈了 ✨",
    ),
    "en": (
        "Love this! Sounds like a good day 😄",
        "Ha, that is so you.",
        "Honestly, this made me smile.",
    ),
    "ja": (
        "いいね！なんだか楽しそう 😊",
        "わかる〜、そういう日あるよね。",
        "おつかれさま！また話そうね。",
    ),
    "ko": (
        "완전 공감이야 😄",
        "좋아 보인다! 오늘도 수고했어.",
        "그런 하루도 괜찮지 뭐.",
    ),
    "pt": (
        "Adorei! Que dia bom 😄",
        "Haha, super a sua cara.",
        "Isso me fez sorrir, sério.",
    ),
    "es": (
        "¡Me encanta! Qué buen día 😄",
        "Jaja, eres tú totalmente.",
        "Esto me ha sacado una sonrisa.",
    ),
    "de": (
        "Klingt nach einem guten Tag 😄",
        "Haha, das bist wirklich du.",
        "Das hat mich zum Lächeln gebracht.",
    ),
    "fr": (
        "J'adore ! On dirait une belle journée 😄",
        "Haha, c'est tellement toi.",
        "Ça m'a fait sourire, franchement.",
    ),
    "it": (
        "Adoro! Sembra una bella giornata 😄",
        "Ahah, sei proprio tu.",
        "Mi hai fatto sorridere, davvero.",
    ),
}

_QUOTE_RE = re.compile(r'^["“”\'`]+|["“”\'`]+$')


def _clean(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return _QUOTE_RE.sub("", text).strip()


def _lang(friend: dict) -> str:
    lang = str(friend.get("reply_language") or "en").strip().lower()
    return lang if lang in _LANG_NAMES else "en"


def _fallback_comment(friend: dict, moment: dict) -> str:
    lang = _lang(friend)
    options = _FALLBACK_COMMENTS.get(lang) or _FALLBACK_COMMENTS["en"]
    seed = sum(ord(ch) for ch in f"{friend.get('id', '')}{moment.get('id', '')}")
    return options[seed % len(options)]


def _pick_friends(friends: list[dict], moment: dict, max_friends: int) -> list[dict]:
    active = [
        f
        for f in friends
        if str(f.get("status") or "active") == "active" and f.get("id")
    ]
    active.sort(
        key=lambda f: (
            -int(f.get("closeness_level") or 0),
            -int(f.get("total_turns") or 0),
            str(f.get("reply_language") or ""),
        )
    )
    chosen: list[dict] = []
    seen_langs: set[str] = set()
    # 先挑语言不同的，最多 max_friends；不够再用剩余好友补齐
    for friend in active:
        lang = _lang(friend)
        if lang in seen_langs:
            continue
        chosen.append(friend)
        seen_langs.add(lang)
        if len(chosen) >= max_friends:
            return chosen
    for friend in active:
        if friend in chosen:
            continue
        chosen.append(friend)
        if len(chosen) >= max_friends:
            break
    return chosen


async def _comment_for(
    db: Any, llm: Any, moment: dict, friend: dict, taken: set[str]
) -> dict | None:
    friend_id = str(friend.get("id") or "")
    if not friend_id or friend_id in taken:
        return None
    lang = _lang(friend)
    fallback = _fallback_comment(friend, moment)
    name = friend.get("name") or "friend"
    city = friend.get("city") or ""
    country = friend.get("nationality") or ""
    persona = _clean(friend.get("personality_prompt"))[:700]
    system = (
        f"You are {name}, a real friend from {city or country}. "
        f"You just saw a friend's moment. Reply with ONE short, natural comment in "
        f"{_LANG_NAMES.get(lang, 'English')} (max ~25 words, like a chat message). "
        "You may use at most one emoji if it fits naturally. "
        "Do not explain, do not give advice, do not mention being an AI. "
        'Output JSON only: {"comment": "..."}'
    )
    if persona:
        system += f"\nYour personality/background (stay in character): {persona}"
    user = f"Moment: {_clean(moment.get('content'))}"
    mood = _clean(moment.get("mood_tag"))
    if mood:
        user += f"\nMood tag: {mood}"
    try:
        data = await complete_json_safe(
            llm,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            schema_hint='{"comment": "short comment"}',
            fallback=lambda: {"comment": fallback},
            temperature=0.8,
            max_tokens=120,
        )
        comment = _clean((data or {}).get("comment")) or fallback
    except Exception as exc:  # noqa: BLE001 - 评论失败不能影响用户发动态
        log.warning("AI 评论生成失败（%s: %s），用模板兜底", type(exc).__name__, exc)
        comment = fallback

    if len(comment) > MAX_COMMENT_CHARS:
        comment = comment[:MAX_COMMENT_CHARS].rstrip() + "…"
    row = await asyncio.to_thread(db.add_moment_comment, moment["id"], friend_id, comment)
    return row


async def generate_comments(
    db: Any,
    llm: Any,
    moment: dict,
    friends: list[dict],
    *,
    max_friends: int = MAX_FRIENDS,
) -> list[dict]:
    """为一条用户动态生成 AI 好友评论；幂等，返回该动态当前全部评论。"""
    existing = await asyncio.to_thread(db.list_moment_comments, moment["id"])
    if existing:
        return existing

    if getattr(llm, "uses_mock", False) or not friends:
        # 无 LLM：仍生成 2~3 条模板评论，保证 UI 有内容
        selected = _pick_friends(friends, moment, max_friends) if friends else []
        for friend in selected:
            comment = _fallback_comment(friend, moment)
            await asyncio.to_thread(
                db.add_moment_comment, moment["id"], friend["id"], comment
            )
        return await asyncio.to_thread(db.list_moment_comments, moment["id"])

    selected = _pick_friends(friends, moment, max_friends)
    taken: set[str] = set()
    await asyncio.gather(
        *(_comment_for(db, llm, moment, friend, taken) for friend in selected),
        return_exceptions=True,
    )
    return await asyncio.to_thread(db.list_moment_comments, moment["id"])
