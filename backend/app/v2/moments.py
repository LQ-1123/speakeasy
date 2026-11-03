"""朋友圈（moments）批量生成 + 兜底模板（v2）。

契约见 ``docs/V2_ARCHITECTURE.md`` §4.6 / ``docs/V2_API.md`` §3：

- ``generate_daily(db, llm, friends, *, feed_date, per_friend_max=1)``：
  一次批量 LLM 调用为活跃好友生成今日动态（15~35 词英文 + ``mood_tag``），
  写入 ``db.add_moment``；**同一天幂等**（已有则直接返回已有动态）。
- ``moment_for_prompt(moment)``：用户评论某条动态时，把动态转成一句英文 prompt 片段。

设计
----
- 无 key / LLM 失败 / 单条不合规（词数不在 15~35、内容为空）时，按
  **nationality（国家风味）+ 当地时段 + personality 种子** 用确定性模板生成，
  保证每位活跃好友至少 1 条、离线可演示。
- 只处理 ``status == "active"`` 的好友（pending_request 不生成）。
- 生成时间用 ``timeutil.local_state`` / ``moment_local_label``，展示为好友当地时间。
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any

from app.logging_utils import get_logger
from app.v2 import timeutil
from app.v2.llm_json import complete_json_safe, to_list

log = get_logger(__name__)

MIN_WORDS = 15
MAX_WORDS = 35

#: 当地时段 → (emoji, 中文标签) —— 作为 mood_tag
_PERIOD_MOOD: dict[str, tuple[str, str]] = {
    "early_morning": ("🌅", "清晨"),
    "morning": ("☕", "晨间咖啡"),
    "noon": ("🍜", "午饭时间"),
    "afternoon": ("💻", "下午搬砖"),
    "evening": ("🏃", "傍晚收工"),
    "night": ("🍝", "晚间放松"),
    "late_night": ("📱", "睡前刷手机"),
    "sleeping": ("😴", "补觉中"),
}
_DEFAULT_MOOD = ("🌤️", "日常碎片")

#: 当地时段 → 动态开头
_PERIOD_OPENERS: dict[str, str] = {
    "early_morning": "Up before the city gets loud",
    "morning": "Morning coffee in hand",
    "noon": "Lunch break at last",
    "afternoon": "Halfway through the afternoon",
    "evening": "Off the clock, finally",
    "night": "Dinner done and the sofa has won",
    "late_night": "Late night, phone in hand",
    "sleeping": "Quiet hours, everyone else asleep",
}

#: nationality 关键词 → 一句国家风味（贴合人设，避免所有好友千篇一律）
_COUNTRY_FLAVORS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("italy", "italian"), "the light on the old buildings is doing that golden thing again"),
    (("india", "indian"), "chai number three and somehow still not enough"),
    (("united kingdom", "uk", "britain", "england", "scotland", "wales"), "grey sky outside but the flat white is decent"),
    (("united states", "usa", "america", "american", "us"), "coffee to go and walking fast for no reason"),
    (("germany", "german"), "the tram arrived exactly on time, obviously"),
    (("france", "french"), "bought a baguette and felt very local about it"),
    (("spain", "spanish"), "siesta energy is very real today"),
    (("japan", "japanese"), "the convenience store dinner is quietly elite"),
    (("brazil", "brazilian"), "the playlist today is doing a lot of heavy lifting"),
    (("china", "chinese"), "the delivery app and I are in a committed relationship"),
    (("canada", "canadian"), "polite queue, decent coffee, cold air, all fine"),
    (("australia", "australian"), "sun's out and everyone is suddenly a morning person"),
    (("netherlands", "dutch"), "bike lane traffic is a whole personality today"),
    (("korea", "korean"), "iced americano in hand, zero regrets"),
    (("mexico", "mexican"), "street tacos made the whole afternoon worth it"),
    (("singapore", "singaporean"), "hawker centre lunch, absolutely no regrets"),
    (("ireland", "irish"), "rain outside, tea inside, balance restored"),
)
_DEFAULT_FLAVORS: tuple[str, ...] = (
    "small things keep adding up, but in a good way",
    "one of those days that feels longer than it looks",
    "nothing dramatic, just steady little wins",
    "a slow start that turned into a decent day",
)

_HOOKS: tuple[str, ...] = (
    "Anyone else feeling the same?",
    "Tell me I'm not the only one.",
    "That's the whole update, honestly.",
    "Small things, honestly.",
    "Hope everyone's day is decent.",
    "Back to it in a minute.",
)

_FILLERS: tuple[str, ...] = (
    "Small things, honestly.",
    "No complaints here.",
    "That's the day then.",
    "Keeping it simple.",
    "Back to it in a minute.",
)


def _as_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ("" if value is None else str(value).strip())


def _clean_text(text: Any) -> str:
    cleaned = " ".join(_as_str(text).split())
    return cleaned.strip().strip('"').strip("“”").strip()


def _word_count(text: str) -> int:
    return len(_clean_text(text).split())


def _pick(options: tuple[str, ...], seed: str) -> str:
    if not options:  # pragma: no cover - 模板表保证非空
        return ""
    return options[sum(ord(ch) for ch in seed) % len(options)]


def _country_flavor(friend: dict, seed: str) -> str:
    haystack = f"{_as_str(friend.get('nationality'))} {_as_str(friend.get('native_lang'))}".lower()
    for keywords, flavor in _COUNTRY_FLAVORS:
        if any(keyword in haystack for keyword in keywords):
            return flavor
    return _pick(_DEFAULT_FLAVORS, seed)


def _fit_content(text: Any) -> str:
    """把任意文本整理成 15~35 词的英文动态（不足补短句，超长按句裁剪）。"""
    cleaned = _clean_text(text)
    words = cleaned.split()
    filler_index = 0
    while len(words) < MIN_WORDS:
        words.extend(_FILLERS[filler_index % len(_FILLERS)].split())
        filler_index += 1
    cleaned = " ".join(words)

    if _word_count(cleaned) > MAX_WORDS:
        sentences = re.split(r"(?<=[.!?])\s+", cleaned)
        kept: list[str] = []
        total = 0
        for sentence in sentences:
            count = _word_count(sentence)
            if kept and total + count > MAX_WORDS:
                break
            if not kept and count > MAX_WORDS:
                kept = [" ".join(sentence.split()[:MAX_WORDS])]
                total = MAX_WORDS
                break
            kept.append(sentence)
            total += count
        cleaned = " ".join(kept)
        if cleaned and not cleaned.endswith((".", "!", "?")):
            cleaned += "."

    words = cleaned.split()
    filler_index = 0
    while len(words) < MIN_WORDS:
        words.extend(_FILLERS[filler_index % len(_FILLERS)].split())
        filler_index += 1
    return " ".join(words)


def _valid_content(text: str) -> bool:
    return MIN_WORDS <= _word_count(text) <= MAX_WORDS


def _sentence(text: str) -> str:
    """首字母大写（模板里的风味短语当独立句子用时）。"""
    cleaned = _clean_text(text)
    return cleaned[:1].upper() + cleaned[1:] if cleaned else cleaned


def _fallback_content(friend: dict, state: dict, variant: int = 0) -> str:
    """确定性模板：nationality 风味 + 当地时段 + 人设种子。"""
    period = _as_str(state.get("period")) or "afternoon"
    opener = _PERIOD_OPENERS.get(period, _PERIOD_OPENERS["afternoon"])
    city = _as_str(friend.get("city"))
    seed = "|".join(
        (
            _as_str(friend.get("id")),
            _as_str(friend.get("name")),
            _as_str(friend.get("archetype_id")),
            _as_str(friend.get("city")),
            _as_str(friend.get("nationality")),
            period,
            str(variant),
        )
    )
    flavor = _country_flavor(friend, seed)
    hook = _pick(_HOOKS, seed + "|hook")
    flavour_variant = variant % 3
    if flavour_variant == 0 and city:
        content = f"{opener}. {city} is being kind to me today. {_sentence(flavor)}. {hook}"
    elif flavour_variant == 1:
        content = f"{opener} — {flavor}. {hook}"
    else:
        content = f"{opener}. {_sentence(flavor)}. {hook}"
    return _fit_content(content)


def _fallback_mood_tag(state: dict) -> str:
    emoji, label = _PERIOD_MOOD.get(_as_str(state.get("period")), _DEFAULT_MOOD)
    return f"{emoji} {label}"


def _fallback_candidate(friend: dict, state: dict, variant: int = 0) -> dict:
    return {
        "friend_id": _as_str(friend.get("id")),
        "content": _fallback_content(friend, state, variant),
        "mood_tag": _fallback_mood_tag(state),
    }


def _existing_for_date(db: Any, feed_date: str) -> list[dict]:
    try:
        moments = db.list_moments(limit=100) or []
    except Exception as exc:  # noqa: BLE001 - 读取失败就当没有
        log.warning("list_moments 失败: %s", exc)
        return []
    return [m for m in moments if isinstance(m, dict) and _as_str(m.get("feed_date")) == feed_date]


def _friends_for_feed(friends: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for friend in friends or ():
        if not isinstance(friend, dict):
            continue
        friend_id = _as_str(friend.get("id"))
        if not friend_id or friend_id in seen:
            continue
        status = _as_str(friend.get("status") or "active").lower() or "active"
        if status != "active":
            continue
        seen.add(friend_id)
        out.append(friend)
    return out


def _build_batch_prompt(friends: list[dict], states: dict[str, dict]) -> list[dict[str, str]]:
    lines: list[str] = []
    for friend in friends:
        friend_id = _as_str(friend.get("id"))
        state = states.get(friend_id) or {}
        episodes = friend.get("shared_episodes") or []
        latest = _as_str(episodes[-1]) if isinstance(episodes, list) and episodes else ""
        quirks = friend.get("speech_quirks") or []
        quirk_text = ", ".join(_as_str(item) for item in quirks[:3]) if isinstance(quirks, list) else ""
        persona = _as_str(friend.get("personality_prompt"))
        lines.append(
            f"- id={friend_id} | {_as_str(friend.get('name'))}, {friend.get('age') or ''}, "
            f"{_as_str(friend.get('occupation')) or 'no job listed'}, in {_as_str(friend.get('city')) or 'unknown city'}"
            f", {_as_str(friend.get('nationality')) or 'unknown country'}; "
            f"personality: {persona[:160] or 'friendly and a bit quirky'}"
            + (f"; speech quirks: {quirk_text}" if quirk_text else "")
            + f"; local time: {state.get('local_time', '?')} ({state.get('period', '?')}), "
            f"{state.get('activity_en', 'going about the day')}"
            + (f"; latest memory: {latest[:100]}" if latest else "; no recent memory")
        )
    return [
        {
            "role": "system",
            "content": (
                "You write the social feed for a language-learning app. For each friend below, write "
                "ONE English social-media moment in first person: casual, warm, 15-35 words, no hashtags, "
                "no links, no emoji in the text itself. Match their persona, country and the local time of "
                "day. Also give a short mood_tag: one emoji + 2-4 Chinese characters, e.g. \"☕ 晨间咖啡\". "
                'Return strict JSON: {"moments": [{"friend_id": "...", "content": "...", '
                '"mood_tag": "..."}]}. Exactly one moment per friend, in the same order.'
            ),
        },
        {"role": "user", "content": "\n".join(lines)},
    ]


def _match_friend(candidate: dict, by_id: dict[str, dict], by_name: dict[str, dict]) -> dict | None:
    friend_id = _as_str(candidate.get("friend_id") or candidate.get("id"))
    if friend_id and friend_id in by_id:
        return by_id[friend_id]
    name = _as_str(candidate.get("name")).lower()
    if name and name in by_name:
        return by_name[name]
    return None


async def _generate(
    db: Any,
    llm: Any,
    friends: list[dict],
    *,
    feed_date: str,
    per_friend_max: int,
    now: datetime | None,
) -> list[dict]:
    active = _friends_for_feed(friends)
    if not active:
        return []

    if now is None:
        now_utc = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now_utc = now.replace(tzinfo=timezone.utc)
    else:
        now_utc = now.astimezone(timezone.utc)

    feed_date = _as_str(feed_date) or timeutil.local_date_str(
        _as_str(active[0].get("timezone")) or "UTC", now_utc
    )

    # 幂等：同一天已经生成过 → 直接返回已有
    existing = _existing_for_date(db, feed_date)
    if existing:
        return existing

    per_friend = max(1, int(per_friend_max or 1))
    states: dict[str, dict] = {}
    for friend in active:
        states[_as_str(friend.get("id"))] = timeutil.local_state(
            _as_str(friend.get("timezone")) or "UTC", now_utc
        )

    def _fallback() -> dict:
        moments: list[dict] = []
        for friend in active:
            friend_id = _as_str(friend.get("id"))
            state = states.get(friend_id) or {}
            for variant in range(per_friend):
                moments.append(_fallback_candidate(friend, state, variant))
        return {"moments": moments}

    parsed = await complete_json_safe(
        llm,
        _build_batch_prompt(active, states),
        schema_hint='{"moments": [{"friend_id": string, "content": string, "mood_tag": string}]}',
        fallback=_fallback,
    )

    if isinstance(parsed, dict):
        raw_candidates = parsed.get("moments")
        candidates = to_list(raw_candidates)
    else:
        candidates = to_list(parsed)

    by_id = {_as_str(friend.get("id")): friend for friend in active}
    by_name = {
        _as_str(friend.get("name")).lower(): friend
        for friend in active
        if _as_str(friend.get("name"))
    }

    # friend_id -> [(content, mood_tag), ...]（每个好友最多 per_friend 条）
    llm_pool: dict[str, list[tuple[str, str]]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        friend = _match_friend(candidate, by_id, by_name)
        if friend is None:
            continue
        content = _clean_text(candidate.get("content"))
        if not _valid_content(content):
            continue  # 不合规 → 该好友走确定性模板
        mood = _clean_text(candidate.get("mood_tag"))
        friend_id = _as_str(friend.get("id"))
        pool = llm_pool.setdefault(friend_id, [])
        if len(pool) < per_friend:
            pool.append((content, mood))

    created: list[dict] = []
    for friend in active:
        friend_id = _as_str(friend.get("id"))
        state = states.get(friend_id) or {}
        pool = llm_pool.get(friend_id, [])
        for slot in range(per_friend):
            if slot < len(pool):
                content, mood = pool[slot]
                mood_tag = mood or _fallback_mood_tag(state)
            else:
                candidate = _fallback_candidate(friend, state, slot)
                content = candidate["content"]
                mood_tag = candidate["mood_tag"]
            local_time_str = timeutil.moment_local_label(
                _as_str(friend.get("timezone")) or "UTC",
                _as_str(friend.get("city")),
                now_utc,
            )
            try:
                moment = db.add_moment(
                    friend_id,
                    content,
                    mood_tag=mood_tag,
                    local_time_str=local_time_str,
                    feed_date=feed_date,
                )
            except Exception as exc:  # noqa: BLE001 - 单条写失败不影响其它好友
                log.warning("add_moment 失败（friend=%s）: %s", friend_id, exc)
                continue
            if isinstance(moment, dict):
                created.append(moment)
    return created


async def generate_daily(
    db: Any,
    llm: Any,
    friends: list[dict],
    *,
    feed_date: str,
    per_friend_max: int = 1,
    now: datetime | None = None,
) -> list[dict]:
    """为活跃好友批量生成 ``feed_date`` 当天的朋友圈动态。

    幂等：``db.moments_exist_for_date(feed_date)`` 为真时直接返回已有动态。
    任何异常都不抛出（返回已生成的部分 / 空列表），保证 API 不 500。
    """
    try:
        return await _generate(
            db,
            llm,
            friends,
            feed_date=feed_date,
            per_friend_max=per_friend_max,
            now=now,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 朋友圈生成失败不能拖垮接口
        log.warning("generate_daily 失败（%s: %s）", type(exc).__name__, exc)
        return []


def moment_for_prompt(moment: dict) -> str:
    """把一条朋友圈转成英文 prompt 片段（用户评论动态时作为 context）。"""
    if not isinstance(moment, dict):
        return ""
    content = _clean_text(moment.get("content"))
    label = _clean_text(moment.get("local_time_str"))
    mood = _clean_text(moment.get("mood_tag"))
    bits: list[str] = []
    if label:
        bits.append(f"at {label}")
    if content:
        bits.append(f'"{content}"')
    if mood:
        bits.append(f"(mood: {mood})")
    if not bits:
        return ""
    return "Friend's moment " + " ".join(bits)


__all__ = [
    "MAX_WORDS",
    "MIN_WORDS",
    "generate_daily",
    "moment_for_prompt",
]
