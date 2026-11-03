"""SpeakEasy 2.0 · PersonaSpawner：LLM 捏人 + 确定性兜底 + 入库。

契约见 ``docs/V2_ARCHITECTURE.md`` §4.3：:

    class PersonaSpawner:
        def __init__(self, db: DB, llm): ...
        def pick_archetype(self, *, nationality=None, language=None, gender=None) -> dict
        async def spawn(self, *, nationality=None, language=None, gender=None,
                        referred_by_friend_id=None, status="active") -> dict
        async def icebreaker(self, friend: dict) -> str

设计
----
1. **原型池**：``voice_archetypes.json`` 里 20 个原型（含真实 edge 声线、城市池、时区、
   名字池、职业池、口头禅）。``pick_archetype`` 先做 nationality/language/gender 过滤，
   再排除 ``db.used_archetype_ids()``，保证同一次「偶遇」不会撞原型；原池耗尽时才复用。
2. **LLM 捏人**：一次 ``complete_json_safe`` 调用生成 name/age/occupation/traits/quirks/
   language_mix_mode/bio/initial_context；**无 key / mock / 解析失败 / 网络错误**时，
   用「原型 + 名字池 + 职业池 + 口头禅」做**确定性**兜底（同一 DB 状态下结果可复现）。
3. **入库**：``db.create_friend`` 只写 friends 表真实存在的列；``initial_context`` /
   ``personality_traits`` 没有独立列，分别内嵌进 ``personality_prompt`` 与返回 dict。
4. **personality_prompt**：英文完整人设 + 口音/口头禅/句长约束，可直接作为 system prompt
   的一部分喂给对话 LLM。
5. **破冰**：``icebreaker`` 生成第一条英文语音条文本（1-2 句、带一个自然的问题钩子）。
"""

from __future__ import annotations

import re
import zlib
from typing import Any

from app.logging_utils import get_logger
from app.v2.llm_json import complete_json_safe, to_dict, to_list
from app.v2.voices import load_archetypes

log = get_logger(__name__)

#: ``language_mix_mode`` 的规范取值（其余值一律归一到 ``en_only``）。
LANGUAGE_MIX_MODES: tuple[str, ...] = ("en_only", "en_with_native_flavor")
DEFAULT_LANGUAGE_MIX_MODE = "en_only"

#: 好友状态（与 docs/V2_API.md 一致）。
FRIEND_STATUSES: tuple[str, ...] = ("active", "pending_request")

_MAX_BIO_CHARS = 400
_MAX_CONTEXT_CHARS = 240
_MAX_ICEBREAKER_CHARS = 220

#: 国家别名 → voice_archetypes.json 里的规范 country。
_COUNTRY_ALIASES: dict[str, str] = {
    "united states": "United States",
    "united states of america": "United States",
    "usa": "United States",
    "u.s.": "United States",
    "u.s.a.": "United States",
    "us": "United States",
    "america": "United States",
    "美国": "United States",
    "united kingdom": "United Kingdom",
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
    "great britain": "United Kingdom",
    "britain": "United Kingdom",
    "england": "United Kingdom",
    "scotland": "United Kingdom",
    "wales": "United Kingdom",
    "英国": "United Kingdom",
    "australia": "Australia",
    "aus": "Australia",
    "aussie": "Australia",
    "澳大利亚": "Australia",
    "澳洲": "Australia",
    "india": "India",
    "印度": "India",
    "ireland": "Ireland",
    "eire": "Ireland",
    "爱尔兰": "Ireland",
    "germany": "Germany",
    "deutschland": "Germany",
    "德国": "Germany",
    "italy": "Italy",
    "italia": "Italy",
    "意大利": "Italy",
    "spain": "Spain",
    "españa": "Spain",
    "espana": "Spain",
    "西班牙": "Spain",
    "france": "France",
    "法国": "France",
    "china": "China",
    "prc": "China",
    "mainland china": "China",
    "中国": "China",
}

#: 语言别名 → voice_archetypes.json 里的 native_lang。
_LANGUAGE_ALIASES: dict[str, str] = {
    "en": "en", "eng": "en", "english": "en", "英语": "en", "英文": "en",
    "de": "de", "ger": "de", "german": "de", "deutsch": "de", "德语": "de",
    "it": "it", "ita": "it", "italian": "it", "italiano": "it", "意大利语": "it",
    "es": "es", "spa": "es", "spanish": "es", "español": "es", "espanol": "es", "西班牙语": "es",
    "fr": "fr", "fre": "fr", "french": "fr", "français": "fr", "francais": "fr", "法语": "fr",
    "zh": "zh", "chi": "zh", "chinese": "zh", "mandarin": "zh", "中文": "zh", "汉语": "zh",
}

#: 性别别名 → male / female。
_GENDER_ALIASES: dict[str, str] = {
    "male": "male", "m": "male", "man": "male", "boy": "male", "男": "male", "男性": "male",
    "female": "female", "f": "female", "woman": "female", "girl": "female",
    "女": "female", "女性": "female",
}

#: 确定性兜底用的「正在干什么」话题池（与 archetype 的 tone_tags/职业无关，稳定即可）。
_CONTEXT_TOPICS: tuple[str, ...] = (
    "music", "weekend plans", "food", "travel", "films", "coffee", "books", "sport",
)

#: 破冰问题池（都是能让用户接话的自然问题）。
_ICEBREAKER_QUESTIONS: tuple[str, ...] = (
    "what does a normal Saturday look like for you?",
    "are you more of a coffee person or a tea person?",
    "what's the best thing you've eaten this week?",
    "have you got any trips coming up?",
    "what do you do when you need to switch off?",
    "what kind of music have you been playing lately?",
    "what are you watching at the moment?",
    "do you get much time outdoors these days?",
)

_PERSONA_SCHEMA_HINT = (
    '{"name": "string", "age": 27, "occupation": "string", '
    '"personality_traits": ["string", "string", "string"], '
    '"speech_quirks": ["string", "string"], '
    '"language_mix_mode": "en_only", "bio": "string", "initial_context": "string"}'
)

_SYSTEM_PROMPT = (
    "You are a casting director for SpeakEasy, a mobile app where Chinese learners of "
    "English practice by trading short voice messages with believable foreign friends. "
    "You write specific, grounded characters — never stereotypes, never generic filler. "
    "Output valid JSON only. No markdown, no commentary."
)

_ICEBREAKER_SYSTEM_PROMPT = (
    "You write the very first voice message a new friend sends on a language-exchange app. "
    "It must sound like a real person leaving a short spoken message: warm, casual, 1-2 "
    "sentences, and it must end with one easy question that invites a reply. "
    "Spoken English only. No markdown, no emoji, no stage directions, no self-introduction "
    "as an AI. Output JSON only: {\"message\": \"...\"}."
)


def _canonical_country(value: Any) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    return _COUNTRY_ALIASES.get(raw.lower()) or raw


def _canonical_language(value: Any) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    return _LANGUAGE_ALIASES.get(raw.lower()) or raw.lower()


def _canonical_gender(value: Any) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    return _GENDER_ALIASES.get(raw.lower())


def _clean_text(value: Any, *, max_chars: int = 0) -> str:
    """去掉引号/markdown 痕迹、压缩空白；空值返回 ``""``。"""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    text = text.strip("\"'“”‘’`")
    text = re.sub(r"\s+", " ", text)
    if max_chars and len(text) > max_chars:
        text = text[: max(0, max_chars - 1)].rstrip() + "…"
    return text


def _clean_name(value: Any) -> str:
    """名字只留字母/空格/撇号/连字符，避免 LLM 塞进整句话。"""
    text = _clean_text(value)
    if not text:
        return ""
    text = re.sub(r"[^A-Za-z\u00C0-\u024F' -]", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ' -")
    if not text:
        return ""
    return " ".join(part.capitalize() for part in text.split(" ")[:2])


def _clean_list(value: Any, *, max_items: int = 4, max_chars: int = 40) -> list[str]:
    out: list[str] = []
    for item in to_list(value):
        text = _clean_text(item, max_chars=max_chars)
        if text and text not in out:
            out.append(text)
        if len(out) >= max_items:
            break
    return out


def _normalize_language_mix_mode(value: Any) -> str:
    raw = _clean_text(value).lower().replace("-", "_").replace(" ", "_")
    if not raw:
        return DEFAULT_LANGUAGE_MIX_MODE
    aliases = {
        "en_only": "en_only",
        "english_only": "en_only",
        "en": "en_only",
        "en_with_native_flavor": "en_with_native_flavor",
        "en_with_native_flavour": "en_with_native_flavor",
        "en_plus_native": "en_with_native_flavor",
        "en_with_native": "en_with_native_flavor",
        "mix": "en_with_native_flavor",
        "bilingual": "en_with_native_flavor",
    }
    return aliases.get(raw, DEFAULT_LANGUAGE_MIX_MODE)


class PersonaSpawner:
    """捏人器：原型过滤 → LLM 生成 → 确定性兜底 → 入库。"""

    def __init__(self, db: Any, llm: Any) -> None:
        self.db = db
        self.llm = llm

    # ------------------------------------------------------------------ #
    # 原型选择
    # ------------------------------------------------------------------ #
    @staticmethod
    def _matches(
        archetype: dict[str, Any],
        country: str | None,
        language: str | None,
        gender: str | None,
    ) -> bool:
        locale = archetype.get("locale_info") or {}
        vibe = archetype.get("vibe_constraints") or {}
        if country and str(locale.get("country") or "").lower() != country.lower():
            return False
        if language and str(locale.get("native_lang") or "").lower() != language.lower():
            return False
        if gender and str(vibe.get("gender") or "").lower() != gender:
            return False
        return True

    def pick_archetype(
        self,
        *,
        nationality: Any = None,
        language: Any = None,
        gender: Any = None,
    ) -> dict[str, Any]:
        """挑一个未被使用过的声线原型（尽量满足过滤条件）。

        过滤优先级：全部条件 → 放宽性别 → 放宽语言 → 放宽国家 → 任意未使用原型。
        原型池耗尽时允许复用（不抛错，接口不会 500）。返回值是原型 dict 的浅拷贝。
        """
        archetypes = load_archetypes()
        if not archetypes:  # pragma: no cover - load_archetypes 已保证非空
            raise RuntimeError("声线原型底池为空")

        country = _canonical_country(nationality)
        lang = _canonical_language(language)
        gend = _canonical_gender(gender)

        try:
            used = set(self.db.used_archetype_ids())
        except Exception as exc:  # noqa: BLE001 - DB 未初始化时仍要能选
            log.warning("读取 used_archetype_ids 失败（%s），按全部原型可用处理", exc)
            used = set()

        stages: list[tuple[str | None, str | None, str | None]] = [
            (country, lang, gend),
            (country, lang, None),
            (country, None, gend),
            (None, lang, gend),
            (None, None, gend),
            (country, None, None),
            (None, lang, None),
            (None, None, None),
        ]
        seen_stages: set[tuple[str | None, str | None, str | None]] = set()
        for stage in stages:
            if stage in seen_stages:
                continue
            seen_stages.add(stage)
            pool = [a for a in archetypes if self._matches(a, *stage)]
            if not pool:
                continue
            free = [a for a in pool if str(a.get("archetype_id") or "") not in used]
            return dict((free or pool)[0])

        return dict(archetypes[0])  # pragma: no cover - 最后一个 stage 必然命中

    # ------------------------------------------------------------------ #
    # 捏人
    # ------------------------------------------------------------------ #
    async def spawn(
        self,
        *,
        nationality: Any = None,
        language: Any = None,
        gender: Any = None,
        referred_by_friend_id: str | None = None,
        status: str = "active",
    ) -> dict[str, Any]:
        """捏一个好友并入库，返回 friend dict（DB 行 + 两个内存附加字段）。

        - 默认 ``status="active"``；带 ``referred_by_friend_id``（好友裂变）时默认转为
          ``pending_request``（需要用户在「动态」里接受后才会聊天）。
        - ``request_message`` 仅 referral 时有值：``"Yo! <A> told me about you."``。
        - 返回 dict 额外带 ``initial_context`` / ``personality_traits``（DB 无此列，
          供 API/调用方直接使用；``personality_prompt`` 内已包含它们）。
        """
        archetype = self.pick_archetype(nationality=nationality, language=language, gender=gender)
        fallback = self._fallback_persona(archetype)
        llm_persona = await self._llm_persona(archetype, fallback)
        persona = self._merge_persona(archetype, llm_persona, fallback)

        locale = archetype.get("locale_info") or {}
        effective_status = self._effective_status(status, referred_by_friend_id)
        data: dict[str, Any] = {
            "name": persona["name"],
            "archetype_id": str(archetype.get("archetype_id") or ""),
            "voice_id": str(archetype.get("voice_id") or ""),
            "voice_rate": persona["voice_rate"],
            "voice_pitch": persona["voice_pitch"],
            "nationality": str(locale.get("country") or ""),
            "city": persona["city"],
            "timezone": str(locale.get("timezone") or "UTC"),
            "native_lang": str(locale.get("native_lang") or "en"),
            "language_mix_mode": persona["language_mix_mode"],
            "age": persona["age"],
            "occupation": persona["occupation"],
            "personality_prompt": self._build_personality_prompt(archetype, persona),
            "speech_quirks": persona["speech_quirks"],
            "bio": persona["bio"],
            "status": effective_status,
            "referred_by_friend_id": referred_by_friend_id or None,
            "request_message": self._build_request_message(referred_by_friend_id),
        }
        friend = self.db.create_friend(data)
        friend["initial_context"] = persona["initial_context"]
        friend["personality_traits"] = list(persona["personality_traits"])
        log.info(
            "捏人完成：%s（%s/%s, %s）archetype=%s status=%s",
            persona["name"], persona["city"], data["nationality"],
            data["native_lang"], data["archetype_id"], effective_status,
        )
        return friend

    # ------------------------------------------------------------------ #
    # LLM 调用（失败/ mock 时走 fallback）
    # ------------------------------------------------------------------ #
    async def _llm_persona(
        self, archetype: dict[str, Any], fallback: dict[str, Any]
    ) -> dict[str, Any]:
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": self._casting_prompt(archetype)},
        ]
        try:
            data = await complete_json_safe(
                self.llm,
                messages,
                schema_hint=_PERSONA_SCHEMA_HINT,
                fallback=lambda: dict(fallback),
                temperature=0.9,
                max_tokens=500,
            )
        except Exception as exc:  # noqa: BLE001 - 兜底不能失败
            log.warning("捏人 LLM 调用异常（%s: %s），使用确定性兜底", type(exc).__name__, exc)
            return dict(fallback)
        merged = to_dict(data)
        return merged if merged else dict(fallback)

    async def icebreaker(self, friend: dict[str, Any]) -> str:
        """生成第一条语音条文本：英文、符合人设、带一个问题钩子。"""
        fallback = self._fallback_icebreaker(friend)
        messages = [
            {"role": "system", "content": _ICEBREAKER_SYSTEM_PROMPT},
            {"role": "user", "content": self._icebreaker_prompt(friend)},
        ]
        try:
            data = await complete_json_safe(
                self.llm,
                messages,
                schema_hint='{"message": "..."}',
                fallback=lambda: {"message": fallback},
                temperature=0.9,
                max_tokens=200,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("破冰 LLM 调用异常（%s: %s），使用确定性兜底", type(exc).__name__, exc)
            return fallback

        if isinstance(data, str):
            text = _clean_text(data, max_chars=_MAX_ICEBREAKER_CHARS)
            return text or fallback
        payload = to_dict(data)
        text = _clean_text(
            payload.get("message") or payload.get("icebreaker") or payload.get("text"),
            max_chars=_MAX_ICEBREAKER_CHARS,
        )
        return text or fallback

    # ------------------------------------------------------------------ #
    # 确定性兜底
    # ------------------------------------------------------------------ #
    def _fallback_persona(self, archetype: dict[str, Any]) -> dict[str, Any]:
        """由 archetype 名字池/职业池/口头禅确定性生成完整人设。"""
        archetype_id = str(archetype.get("archetype_id") or "unknown")
        locale = archetype.get("locale_info") or {}
        vibe = archetype.get("vibe_constraints") or {}

        seed = zlib.crc32(archetype_id.encode("utf-8")) & 0xFFFFFFFF
        try:
            variant = len(self.db.used_archetype_ids())
        except Exception:  # noqa: BLE001 - DB 未初始化
            variant = 0

        name_pool = [str(n) for n in (archetype.get("name_pool") or []) if str(n).strip()] or ["Alex"]
        city_pool = [str(c) for c in (locale.get("city_pool") or []) if str(c).strip()] or ["somewhere new"]
        occupation_pool = [
            str(o) for o in (archetype.get("occupation_pool") or []) if str(o).strip()
        ] or ["barista at a neighbourhood café"]

        name = name_pool[(seed + variant) % len(name_pool)]
        city = city_pool[((seed >> 5) + variant * 3) % len(city_pool)]
        occupation = occupation_pool[((seed >> 7) + variant * 7) % len(occupation_pool)]

        age_range = vibe.get("age_range") or [24, 32]
        try:
            age_lo, age_hi = int(age_range[0]), int(age_range[1])
        except (TypeError, ValueError, IndexError):  # pragma: no cover - 数据文件已校验
            age_lo, age_hi = 24, 32
        if age_hi < age_lo:
            age_lo, age_hi = age_hi, age_lo
        age = age_lo + (((seed >> 3) + variant * 5) % max(1, age_hi - age_lo + 1))

        traits = _clean_list(
            archetype.get("personality_traits") or vibe.get("tone_tags"),
            max_items=3, max_chars=30,
        ) or ["curious", "warm", "easy-going"]
        quirks = _clean_list(archetype.get("speech_quirks"), max_items=3, max_chars=24) or ["honestly"]

        topic = _CONTEXT_TOPICS[((seed >> 11) + variant) % len(_CONTEXT_TOPICS)]
        bio = (
            f"{name} is a {age}-year-old {occupation} based in {city}. "
            f"{traits[0].capitalize()} and easy to talk to once the ice is broken, "
            f"with a soft spot for {topic}."
        )
        initial_context = (
            f"{name} just wrapped up a long day, is back home in {city}, "
            f"and is genuinely up for a relaxed chat about {topic} and weekend plans."
        )

        prosody = archetype.get("prosody") or {}
        return {
            "name": name,
            "age": age,
            "occupation": occupation,
            "city": city,
            "personality_traits": traits,
            "speech_quirks": quirks,
            "language_mix_mode": DEFAULT_LANGUAGE_MIX_MODE,
            "bio": bio,
            "initial_context": initial_context,
            "voice_rate": str(prosody.get("rate") or "+0%"),
            "voice_pitch": str(prosody.get("pitch") or "+0Hz"),
        }

    def _merge_persona(
        self,
        archetype: dict[str, Any],
        llm_persona: dict[str, Any] | Any,
        fallback: dict[str, Any],
    ) -> dict[str, Any]:
        """合并 LLM 输出与确定性兜底：字段级校验 + 缺失补全。"""
        data = to_dict(llm_persona)
        vibe = archetype.get("vibe_constraints") or {}

        name = _clean_name(data.get("name")) or fallback["name"]

        age = fallback["age"]
        raw_age = data.get("age")
        try:
            if raw_age is not None and str(raw_age).strip():
                parsed = int(float(raw_age))
                age_range = vibe.get("age_range") or [fallback["age"], fallback["age"]]
                lo, hi = int(age_range[0]), int(age_range[1])
                age = max(int(lo), min(int(hi), parsed))
        except (TypeError, ValueError):  # pragma: no cover - LLM 脏数据
            age = fallback["age"]

        occupation = _clean_text(data.get("occupation"), max_chars=120) or fallback["occupation"]

        traits = _clean_list(data.get("personality_traits"), max_items=5, max_chars=30)
        for trait in fallback["personality_traits"]:
            if len(traits) >= 3:
                break
            if trait not in traits:
                traits.append(trait)
        traits = traits[:5] or list(fallback["personality_traits"])

        quirks = _clean_list(data.get("speech_quirks"), max_items=4, max_chars=24)
        for quirk in fallback["speech_quirks"]:
            if len(quirks) >= 2:
                break
            if quirk not in quirks:
                quirks.append(quirk)
        quirks = quirks or list(fallback["speech_quirks"])

        bio = _clean_text(data.get("bio"), max_chars=_MAX_BIO_CHARS) or fallback["bio"]
        initial_context = (
            _clean_text(data.get("initial_context"), max_chars=_MAX_CONTEXT_CHARS)
            or fallback["initial_context"]
        )

        return {
            "name": name,
            "age": int(age),
            "occupation": occupation,
            "city": fallback["city"],
            "personality_traits": traits,
            "speech_quirks": quirks,
            "language_mix_mode": _normalize_language_mix_mode(data.get("language_mix_mode")),
            "bio": bio,
            "initial_context": initial_context,
            "voice_rate": fallback["voice_rate"],
            "voice_pitch": fallback["voice_pitch"],
        }

    # ------------------------------------------------------------------ #
    # Prompt 构造
    # ------------------------------------------------------------------ #
    @staticmethod
    def _casting_prompt(archetype: dict[str, Any]) -> str:
        locale = archetype.get("locale_info") or {}
        vibe = archetype.get("vibe_constraints") or {}
        age_range = vibe.get("age_range") or [24, 32]
        name_pool = ", ".join(str(n) for n in (archetype.get("name_pool") or [])[:8])
        occupation_pool = "; ".join(str(o) for o in (archetype.get("occupation_pool") or [])[:8])
        tone_tags = ", ".join(str(t) for t in (vibe.get("tone_tags") or [])[:5])
        cities = ", ".join(str(c) for c in (locale.get("city_pool") or [])[:8])

        return (
            "Invent one friend for our app.\n"
            f"- Country: {locale.get('country', '')}\n"
            f"- City: pick one of these and stay consistent: {cities}\n"
            f"- Gender: {vibe.get('gender', 'any')}\n"
            f"- Age: between {age_range[0]} and {age_range[1]}\n"
            f"- Vibe tags: {tone_tags}\n"
            f"- Suggested first names (pick one, or a close cultural equivalent): {name_pool}\n"
            f"- Occupation ideas (pick one or invent something equally concrete): {occupation_pool}\n"
            "\nRules:\n"
            "- The name must fit the country and gender.\n"
            "- The occupation must be specific (not \"office worker\" or \"student\").\n"
            "- personality_traits: exactly 3 short adjectives.\n"
            "- speech_quirks: 2-3 spoken fillers/phrases this person actually says in English.\n"
            "- language_mix_mode: \"en_only\" normally; \"en_with_native_flavor\" only if they "
            "occasionally drop a native word while speaking English.\n"
            "- bio: 1-2 third-person sentences, concrete details, no clichés.\n"
            "- initial_context: 1 sentence about what they are doing right now.\n"
            "\nJSON keys: name, age, occupation, personality_traits, speech_quirks, "
            "language_mix_mode, bio, initial_context."
        )

    @staticmethod
    def _build_personality_prompt(archetype: dict[str, Any], persona: dict[str, Any]) -> str:
        locale = archetype.get("locale_info") or {}
        accent = _clean_text(archetype.get("accent_label")) or f"{locale.get('country', '')} accent"
        quirks = ", ".join(persona["speech_quirks"]) or "honestly"
        traits = ", ".join(persona["personality_traits"]) or "curious and warm"
        city = persona.get("city") or ""
        country = locale.get("country") or ""
        gap = ", " if city and country else ""

        return (
            f"You are {persona['name']}, a {persona['age']}-year-old "
            f"{persona['occupation']} living in {city}{gap}{country}.\n\n"
            f"## Who you are\n"
            f"- Personality: {traits}.\n"
            f"- Background: {persona['bio']}\n"
            f"- Right now: {persona['initial_context']}\n\n"
            f"## How you speak (CRITICAL)\n"
            f"- Your English has a {accent} accent. The TTS voice already carries the accent — "
            f"you handle the WORDING.\n"
            f"- Occasionally use your natural expressions: {quirks}. At most one per turn; "
            f"never stack them.\n"
            f"- Reply with spoken English only. 1-2 sentences, max 55 words. Always use contractions.\n"
            f"- Ask at most one question per turn, and only when it feels natural.\n"
            f"- Never say you are an AI, a model, or that this is a language exercise. "
            f"Never correct the learner explicitly — just reuse the corrected form naturally.\n"
            f"- Stay in character even when the learner changes topic; you are a friend, not a teacher."
        )

    @staticmethod
    def _icebreaker_prompt(friend: dict[str, Any]) -> str:
        quirks = ", ".join(str(q) for q in (friend.get("speech_quirks") or [])[:3])
        prompt = (
            "Write the first voice message from this friend to a new language partner.\n"
            f"- Name: {friend.get('name', '')}\n"
            f"- Age: {friend.get('age', '')}\n"
            f"- Occupation: {friend.get('occupation', '')}\n"
            f"- City / country: {friend.get('city', '')} / {friend.get('nationality', '')}\n"
            f"- Their own expressions (optional, at most one): {quirks}\n"
        )
        context = _clean_text(friend.get("initial_context"), max_chars=_MAX_CONTEXT_CHARS)
        if context:
            prompt += f"- Current situation: {context}\n"
        prompt += (
            "\nRules: 1-2 short spoken sentences in English, friendly and specific to this person, "
            "ending with exactly one easy question. Do not introduce them as an AI. "
            'Output JSON: {"message": "..."}'
        )
        return prompt

    # ------------------------------------------------------------------ #
    # referral / 状态
    # ------------------------------------------------------------------ #
    @staticmethod
    def _effective_status(status: str, referred_by_friend_id: str | None) -> str:
        raw = str(status or "active").strip().lower()
        # 好友裂变进来的新朋友先挂起，等用户接受后再聊天
        if referred_by_friend_id and raw == "active":
            return "pending_request"
        return raw if raw in FRIEND_STATUSES else "active"

    def _build_request_message(self, referred_by_friend_id: str | None) -> str:
        if not referred_by_friend_id:
            return ""
        referrer_name = ""
        try:
            referrer = self.db.get_friend(referred_by_friend_id)
            if isinstance(referrer, dict):
                referrer_name = str(referrer.get("name") or "")
        except Exception as exc:  # noqa: BLE001 - 名字查不到也要能建好友
            log.warning("读取推荐人 %s 失败（%s）", referred_by_friend_id, exc)
        referrer_name = _clean_name(referrer_name) or "A mate of mine"
        return f"Yo! {referrer_name} told me about you."

    def _fallback_icebreaker(self, friend: dict[str, Any]) -> str:
        name = _clean_name(friend.get("name")) or "Hey"
        city = _clean_text(friend.get("city"), max_chars=40).rstrip(".")
        occupation = _clean_text(friend.get("occupation"), max_chars=60) or "doing my own thing"
        occupation = occupation.rstrip(".")
        seed_source = str(friend.get("id") or "") + "|" + name + "|" + city
        seed = zlib.crc32(seed_source.encode("utf-8")) & 0xFFFFFFFF
        question = _ICEBREAKER_QUESTIONS[seed % len(_ICEBREAKER_QUESTIONS)]

        variants = (
            f"Hey, it's {name} from {city}! I'm a {occupation} — {question}",
            f"Hey! {name} here, {occupation} over in {city}. {question}",
            f"Hi, {name} from {city}. Quick one — {question}",
        )
        text = variants[seed % len(variants)]
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > _MAX_ICEBREAKER_CHARS:
            text = text[: _MAX_ICEBREAKER_CHARS - 1].rstrip() + "…"
        return text


__all__ = [
    "DEFAULT_LANGUAGE_MIX_MODE",
    "FRIEND_STATUSES",
    "LANGUAGE_MIX_MODES",
    "PersonaSpawner",
]
