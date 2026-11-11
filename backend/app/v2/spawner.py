"""SpeakEasy 2.0 · PersonaSpawner：LLM 捏人 + 确定性兜底 + 入库。

契约见 ``docs/V2_ARCHITECTURE.md`` §4.3：:

    class PersonaSpawner:
        def __init__(self, db: DB, llm): ...
        def pick_archetype(self, *, nationality=None, language=None, gender=None,
                           strict=False) -> dict
        async def spawn(self, *, nationality=None, language=None, gender=None,
                        referred_by_friend_id=None, status="active",
                        strict_filters=False) -> dict
        async def icebreaker(self, friend: dict) -> str

设计
----
1. **原型池**：``voice_archetypes.json`` 里 28 个原型（含真实 edge 声线、城市池、时区、
   名字池、职业池、口头禅）。``pick_archetype`` 先做 nationality/language/gender 过滤，
   再排除 ``db.used_archetype_ids()``，保证同一次「偶遇」不会撞原型；原池耗尽时才复用。
   ``strict=True``（仅 discover）时过滤条件绝不静默放宽，无匹配抛 :class:`ArchetypeNotFound`。
2. **多语言回复**：``reply_language`` 决定好友用哪种语言跟用户说话（ja/ko/pt/es/de/it/fr/zh/en）。
   ``language="en"`` 表示「用英语回复」，可匹配任意国籍的原型；非 en 时要求原型
   ``native_lang`` 一致；仅按国籍筛选时用原型的母语（选日本 → 日语）。
3. **LLM 捏人**：一次 ``complete_json_safe`` 调用生成 name/age/occupation/traits/quirks/
   language_mix_mode/bio/initial_context；**无 key / mock / 解析失败 / 网络错误**时，
   用「原型 + 名字池 + 职业池 + 口头禅」做**确定性**兜底（同一 DB 状态下结果可复现）。
4. **入库**：``db.create_friend`` 只写 friends 表真实存在的列；``initial_context`` /
   ``personality_traits`` 没有独立列，分别内嵌进 ``personality_prompt`` 与返回 dict。
5. **personality_prompt**：英文完整人设 + 回复语言 + 口音/口头禅/句长约束，可直接作为
   system prompt 的一部分喂给对话 LLM。非英语原型会明确「只用母语回复」。
6. **破冰**：``icebreaker`` 按 ``reply_language`` 生成第一条语音条文本（1-2 句、带一个
   自然的问题钩子）；无 LLM 时用该语言的确定性模板（ja/ko/pt/es/de/it/fr/zh + en）。
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import zlib
from typing import Any

from app.logging_utils import get_logger
from app.v2.llm_json import complete_json_safe, to_dict, to_list
from app.v2.voices import load_archetypes

log = get_logger(__name__)

#: ``language_mix_mode`` 的规范取值（其余值一律归一）。
#: - ``en_only``：全程英语（英语好友默认）；
#: - ``en_with_native_flavor``：英语为主、偶尔夹母语词；
#: - ``native_primary``：母语为主（``reply_language != "en"`` 的好友使用）。
LANGUAGE_MIX_MODES: tuple[str, ...] = ("en_only", "en_with_native_flavor", "native_primary")
DEFAULT_LANGUAGE_MIX_MODE = "en_only"

#: 好友状态（与 docs/V2_API.md 一致）。
FRIEND_STATUSES: tuple[str, ...] = ("active", "pending_request")

_MAX_BIO_CHARS = 400
_MAX_CONTEXT_CHARS = 240
_MAX_ICEBREAKER_CHARS = 220

#: 语言代码 → 给 prompt / 用户看的语言名（同时用于 personality_prompt 与 casting）。
_LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "ja": "Japanese (日本語)",
    "ko": "Korean (한국어)",
    "pt": "Brazilian Portuguese (português)",
    "es": "Spanish (español)",
    "de": "German (Deutsch)",
    "it": "Italian (italiano)",
    "fr": "French (français)",
    "zh": "Chinese (中文)",
}


def language_display_name(code: Any) -> str:
    """``"ja"`` → ``"Japanese (日本語)"``；未知代码原样返回。"""
    canonical = _canonical_language(code) or str(code or "").strip() or "en"
    return _LANGUAGE_NAMES.get(canonical, canonical)


class ArchetypeNotFound(RuntimeError):
    """严格筛选（discover）时没有任何声线原型匹配。

    Attributes:
        detail: 可读原因（包含被拒绝的过滤条件 + 当前支持的国家/语种列表）。
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


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
    "japan": "Japan",
    "jp": "Japan",
    "日本": "Japan",
    "korea": "Korea",
    "south korea": "Korea",
    "kr": "Korea",
    "韩国": "Korea",
    "韓国": "Korea",
    "한국": "Korea",
    "brazil": "Brazil",
    "brasil": "Brazil",
    "br": "Brazil",
    "巴西": "Brazil",
    "mexico": "Mexico",
    "méxico": "Mexico",
    "mx": "Mexico",
    "墨西哥": "Mexico",
}

#: 语言别名 → voice_archetypes.json 里的 native_lang。
_LANGUAGE_ALIASES: dict[str, str] = {
    "en": "en", "eng": "en", "english": "en", "英语": "en", "英文": "en",
    "de": "de", "ger": "de", "german": "de", "deutsch": "de", "德语": "de",
    "it": "it", "ita": "it", "italian": "it", "italiano": "it", "意大利语": "it",
    "es": "es", "spa": "es", "spanish": "es", "español": "es", "espanol": "es", "西班牙语": "es",
    "es-mx": "es", "es_mx": "es",
    "fr": "fr", "fre": "fr", "french": "fr", "français": "fr", "francais": "fr", "法语": "fr",
    "zh": "zh", "chi": "zh", "chinese": "zh", "mandarin": "zh", "中文": "zh", "汉语": "zh",
    "ja": "ja", "jpn": "ja", "japanese": "ja", "日本語": "ja", "日语": "ja", "日文": "ja",
    "ja-jp": "ja",
    "ko": "ko", "kor": "ko", "korean": "ko", "한국어": "ko", "韩语": "ko", "韓語": "ko",
    "ko-kr": "ko",
    "pt": "pt", "por": "pt", "portuguese": "pt", "português": "pt", "portugues": "pt",
    "巴西葡语": "pt", "葡萄牙语": "pt", "pt-br": "pt", "pt_br": "pt",
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

#: 破冰问句池（按语言；都是能让用户接话的自然问题）。
_ICEBREAKER_QUESTIONS_BY_LANG: dict[str, tuple[str, ...]] = {
    "en": (
        "what does a normal Saturday look like for you?",
        "are you more of a coffee person or a tea person?",
        "what's the best thing you've eaten this week?",
        "have you got any trips coming up?",
        "what do you do when you need to switch off?",
        "what kind of music have you been playing lately?",
        "what are you watching at the moment?",
        "do you get much time outdoors these days?",
    ),
    "ja": (
        "ふだんの週末って何してるの？",
        "コーヒー派？それともお茶派？",
        "最近食べたもので一番おいしかったのは？",
    ),
    "ko": (
        "주말에는 보통 뭐 해요?",
        "커피파예요, 차파예요?",
        "요즘 가장 맛있게 먹은 게 뭐예요?",
    ),
    "pt": (
        "O que você costuma fazer no fim de semana?",
        "Você prefere café ou chá?",
        "Qual foi a melhor coisa que você comeu essa semana?",
    ),
    "es": (
        "¿Qué haces normalmente los fines de semana?",
        "¿Eres más de café o de té?",
        "¿Qué es lo mejor que has comido esta semana?",
    ),
    "de": (
        "Was machst du so am Wochenende?",
        "Bist du eher Kaffee- oder Tee-Typ?",
        "Was war das Beste, das du diese Woche gegessen hast?",
    ),
    "it": (
        "Cosa fai di solito nel fine settimana?",
        "Sei più tipo caffè o tè?",
        "Qual è la cosa migliore che hai mangiato questa settimana?",
    ),
    "fr": (
        "Tu fais quoi le week-end, d'habitude ?",
        "Tu es plutôt café ou thé ?",
        "Qu'est-ce que tu as mangé de meilleur cette semaine ?",
    ),
    "zh": (
        "你周末一般做什么呀？",
        "你更喜欢咖啡还是茶？",
        "这周你吃过最好吃的东西是什么？",
    ),
}

#: 破冰消息模板（按语言；``{name}/{city}/{occupation}/{question}`` 占位）。
_ICEBREAKER_TEMPLATES: dict[str, tuple[str, ...]] = {
    "en": (
        "Hey, it's {name} from {city}! I'm a {occupation} — {question}",
        "Hey! {name} here, {occupation} over in {city}. {question}",
        "Hi, {name} from {city}. Quick one — {question}",
    ),
    "ja": (
        "はじめまして、{name}です！{question}",
        "{name}です。{question}",
        "こんにちは、{name}です！{question}",
    ),
    "ko": (
        "안녕하세요, {name}이에요. {question}",
        "{name}이에요! {question}",
        "반가워요, {name}입니다. {question}",
    ),
    "pt": (
        "Oi, aqui é o {name}! {question}",
        "Oi! Sou {name}. {question}",
        "E aí, {name} aqui. {question}",
    ),
    "es": (
        "¡Hola, soy {name}! {question}",
        "¡Qué tal! Soy {name}. {question}",
        "¡Hey! Soy {name}, mucho gusto. {question}",
    ),
    "de": (
        "Hey, hier ist {name}! {question}",
        "Hallo, ich bin {name}. {question}",
        "Servus, {name} hier! {question}",
    ),
    "it": (
        "Ciao, sono {name}! {question}",
        "Ehi, sono {name}. {question}",
        "Piaciuto, {name} qui! {question}",
    ),
    "fr": (
        "Salut, c'est {name} ! {question}",
        "Coucou, moi c'est {name}. {question}",
        "Hello ! {name} ici. {question}",
    ),
    "zh": (
        "你好，我是{name}！{question}",
        "嗨，我是{name}。{question}",
        "哈喽，{name}在这儿！{question}",
    ),
}

_PERSONA_SCHEMA_HINT = (
    '{"name": "string", "age": 27, "occupation": "string", '
    '"personality_traits": ["string", "string", "string"], '
    '"speech_quirks": ["string", "string"], '
    '"language_mix_mode": "en_only", "bio": "string", "initial_context": "string"}'
)

_SYSTEM_PROMPT = (
    "You are a casting director for SpeakEasy, a mobile app where language learners "
    "practice by trading short voice messages with believable foreign friends. Some friends "
    "reply in English; others always reply in their own native language (given in the brief). "
    "You write specific, grounded characters — never stereotypes, never generic filler. "
    "Output valid JSON only. No markdown, no commentary."
)

_ICEBREAKER_SYSTEM_PROMPT = (
    "You write the very first voice message a new friend sends on a language-exchange app. "
    "It must sound like a real person leaving a short spoken message: warm, casual, 1-2 "
    "sentences, and it must end with one easy question that invites a reply. "
    "Write in the reply language specified in the brief (spoken, no markdown, no emoji, no "
    "stage directions, never introduce yourself as an AI). "
    "Output JSON only: {\"message\": \"...\"}."
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
        "native_primary": "native_primary",
        "native": "native_primary",
        "native_only": "native_primary",
        "native_first": "native_primary",
        "mostly_native": "native_primary",
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
    def _language_matches(archetype: dict[str, Any], language: str | None) -> bool:
        """语言筛选语义。

        - 未给语言 → 不过滤；
        - ``language == "en"`` → 只按国家/性别筛（英语回复可用任意国籍原型）；
        - 其他语言 → 要求原型 ``native_lang`` 一致。
        """
        if not language:
            return True
        if language == "en":
            return True
        native = str((archetype.get("locale_info") or {}).get("native_lang") or "").lower()
        return native == language.lower()

    @classmethod
    def _matches(
        cls,
        archetype: dict[str, Any],
        country: str | None,
        language: str | None,
        gender: str | None,
    ) -> bool:
        locale = archetype.get("locale_info") or {}
        vibe = archetype.get("vibe_constraints") or {}
        if country and str(locale.get("country") or "").lower() != country.lower():
            return False
        if not cls._language_matches(archetype, language):
            return False
        if gender and str(vibe.get("gender") or "").lower() != gender:
            return False
        return True

    @staticmethod
    def _supported_summary(archetypes: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
        countries = sorted(
            {str((a.get("locale_info") or {}).get("country") or "") for a in archetypes} - {""}
        )
        languages = sorted(
            {str((a.get("locale_info") or {}).get("native_lang") or "") for a in archetypes} - {""}
        )
        return countries, languages

    @classmethod
    def _not_found_detail(
        cls,
        country: str | None,
        language: str | None,
        gender: str | None,
        archetypes: list[dict[str, Any]],
    ) -> str:
        countries, languages = cls._supported_summary(archetypes)
        asked: list[str] = []
        if country:
            asked.append(f"country={country}")
        if language:
            asked.append(f"language={language}")
        if gender:
            asked.append(f"gender={gender}")
        asked_txt = "、".join(asked) or "任意条件"
        return (
            f"没有匹配的声线原型（{asked_txt}）。"
            f"当前支持的国家：{', '.join(countries)}；"
            f"支持的语种：{', '.join(languages)}。"
        )

    def _user_age(self) -> int | None:
        """从画像生日算用户年龄；没填返回 None。"""
        try:
            profile = self.db.get_profile() or {}
        except Exception:  # noqa: BLE001
            return None
        raw = str(profile.get("birthday") or "").strip()
        if not raw:
            return None
        try:
            born = _dt.date.fromisoformat(raw[:10])
        except ValueError:
            return None
        today = _dt.date.today()
        return today.year - born.year - ((today.month, today.day) < (born.month, born.day))

    @staticmethod
    def _age_gap(archetype: dict[str, Any], age: int) -> int:
        """用户年龄距原型年龄区间的差值（0 = 落在区间内）。"""
        try:
            rng = (archetype.get("vibe_constraints") or {}).get("age_range") or [24, 32]
            lo, hi = int(rng[0]), int(rng[1])
        except (TypeError, ValueError, IndexError):
            return 0
        if age < lo:
            return lo - age
        if age > hi:
            return age - hi
        return 0

    @classmethod
    def _age_best(
        cls,
        pool: list[dict[str, Any]],
        age: int | None,
        lang: str | None = None,
    ) -> list[dict[str, Any]]:
        """年龄差距最小的一档；同档内优先母语就是目标语言的原型。"""
        if age is None or not pool:
            return pool
        gaps = {id(a): cls._age_gap(a, age) for a in pool}
        best = min(gaps.values())
        # 放宽 2 岁：避免只剩一个「同龄但母语不对」的原型（例如 21 岁只剩中文原型）
        same = [a for a in pool if gaps[id(a)] <= best + 2]
        if lang:
            native = [
                a
                for a in same
                if str((a.get("locale_info") or {}).get("native_lang") or "").lower() == lang
            ]
            if native:
                return native
        return same

    def pick_archetype(
        self,
        *,
        nationality: Any = None,
        language: Any = None,
        gender: Any = None,
        strict: bool = False,
        age: int | None = None,
    ) -> dict[str, Any]:
        """挑一个未被使用过的声线原型。

        - ``strict=False``（onboarding/referral/旧调用）：尽量满足过滤条件，逐级放宽
          （性别 → 语言 → 国家 → 任意），原型池耗尽时允许复用，**绝不抛错**；
        - ``strict=True``（discover 用户主动筛选）：只按（国家, 语言, 性别）精确匹配，
          无匹配时抛 :class:`ArchetypeNotFound`，**不得静默放宽**；匹配到的原型都被用过
          时允许复用（仍满足筛选条件）。

        返回值是原型 dict 的浅拷贝。
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

        if strict:
            # 严格模式不允许把无法识别的 gender 当作「不过滤」静默放宽
            if gender and gend is None:
                detail = self._not_found_detail(country, lang, str(gender).strip(), archetypes)
                log.warning("严格筛选无匹配原型: %s", detail)
                raise ArchetypeNotFound(detail)
            pool = [a for a in archetypes if self._matches(a, country, lang, gend)]
            if not pool:
                detail = self._not_found_detail(country, lang, gend, archetypes)
                log.warning("严格筛选无匹配原型: %s", detail)
                raise ArchetypeNotFound(detail)
            pool = self._age_best(pool, age, lang)
            free = [a for a in pool if str(a.get("archetype_id") or "") not in used]
            return dict((free or pool)[0])

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
            pool = self._age_best(pool, age, lang)
            free = [a for a in pool if str(a.get("archetype_id") or "") not in used]
            return dict((free or pool)[0])

        return dict(archetypes[0])  # pragma: no cover - 最后一个 stage 必然命中

    @staticmethod
    def _resolve_reply_language(
        nationality: Any,
        language: Any,
        archetype: dict[str, Any],
    ) -> str:
        """决定好友用什么语言回复用户（写入 friends.reply_language）。

        - 给了 ``language`` 筛选 → 用规范化后的语言（``en``/``ja``/...）；
        - 否则按国籍筛选且原型母语非 en → 用原型母语（选日本 → ja）；
        - 否则 ``en``。
        """
        lang = _canonical_language(language)
        if lang and lang in _LANGUAGE_NAMES:
            return lang
        country = _canonical_country(nationality)
        native = str((archetype.get("locale_info") or {}).get("native_lang") or "en").lower()
        if country and native and native in _LANGUAGE_NAMES and native != "en":
            return native
        return "en"


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
        strict_filters: bool = False,
    ) -> dict[str, Any]:
        """捏一个好友并入库，返回 friend dict（DB 行 + 两个内存附加字段）。

        - 默认 ``status="active"``；带 ``referred_by_friend_id``（好友裂变）时默认转为
          ``pending_request``（需要用户在「动态」里接受后才会聊天）。
        - ``strict_filters=True``（仅 discover）：筛选条件绝不静默放宽，无匹配抛
          :class:`ArchetypeNotFound`；onboarding/referral 用默认 ``False`` 保持容错。
        - ``reply_language``：好友对用户说话所用语言（ja/ko/pt/es/de/it/fr/zh/en），
          直接写入 friends 表（DB 列由 Lead 维护）。
        - ``request_message`` 仅 referral 时有值：``"Yo! <A> told me about you."``。
        - 返回 dict 额外带 ``initial_context`` / ``personality_traits``（DB 无此列，
          供 API/调用方直接使用；``personality_prompt`` 内已包含它们）。
        """
        user_age = self._user_age()
        archetype = self.pick_archetype(
            nationality=nationality,
            language=language,
            gender=gender,
            strict=strict_filters,
            age=user_age,
        )
        age_lo = age_hi = None
        if user_age is not None:
            # 原型的年龄区间整体偏大（22~36）：直接覆盖成「用户年龄 ±4」，
            # 让 LLM 和兜底都只能生成相近的年龄。
            age_lo = max(18, user_age - 4)
            age_hi = max(age_lo, user_age + 4)
            archetype = dict(archetype)
            vibe = dict(archetype.get("vibe_constraints") or {})
            vibe["age_range"] = [age_lo, age_hi]
            archetype["vibe_constraints"] = vibe
        reply_language = self._resolve_reply_language(nationality, language, archetype)
        fallback = self._fallback_persona(archetype)
        llm_persona = await self._llm_persona(archetype, fallback, reply_language)
        persona = self._merge_persona(archetype, llm_persona, fallback, reply_language)
        if age_lo is not None and age_hi is not None:
            try:
                age_val = int(persona.get("age") or 0)
            except (TypeError, ValueError):
                age_val = 0
            if age_val:
                persona["age"] = max(age_lo, min(age_hi, age_val))
            fallback["age"] = max(age_lo, min(age_hi, int(fallback.get("age") or age_lo)))

        locale = archetype.get("locale_info") or {}
        effective_status = self._effective_status(status, referred_by_friend_id)

        # 性别：优先 archetype 的约束，其次 LLM 产出的 persona
        _vibe_gender = ""
        try:
            _vibe_gender = str(
                (archetype or {}).get("vibe_constraints", {}).get("gender") or ""
            )
        except Exception:  # noqa: BLE001
            _vibe_gender = ""
        _persona_gender = _canonical_gender(_vibe_gender) or _canonical_gender(
            (persona or {}).get("gender")
        ) or _canonical_gender((llm_persona or {}).get("gender")) or ""

        data: dict[str, Any] = {
            "name": persona["name"],
            "gender": _persona_gender,
            "archetype_id": str(archetype.get("archetype_id") or ""),
            "voice_id": str(archetype.get("voice_id") or ""),
            "voice_rate": persona["voice_rate"],
            "voice_pitch": persona["voice_pitch"],
            "nationality": str(locale.get("country") or ""),
            "city": persona["city"],
            "timezone": str(locale.get("timezone") or "UTC"),
            "native_lang": str(locale.get("native_lang") or "en"),
            "reply_language": reply_language,
            "language_mix_mode": persona["language_mix_mode"],
            "age": persona["age"],
            "occupation": persona["occupation"],
            "personality_prompt": self._build_personality_prompt(
                archetype, persona, reply_language
            ),
            "speech_quirks": persona["speech_quirks"],
            "bio": persona["bio"],
            "status": effective_status,
            "referred_by_friend_id": referred_by_friend_id or None,
            "request_message": self._build_request_message(referred_by_friend_id),
        }
        # 随机分配 Fish 音色（用户不手选）；任何异常都不影响捏人
        try:
            from app.config import get_settings as _gs  # noqa: PLC0415
            from app.voice import fish_tts as _ft  # noqa: PLC0415
            from app.voice import voice_registry as _vr  # noqa: PLC0415
            from app.voice.tts_config import TTSConfigStore as _TCS  # noqa: PLC0415

            _cfg = _TCS(str(_gs().data_path)).load()
            # Fish 不可达时不要等网络：新好友先用 Edge 声线兜底（聊天/朗读仍然一致）
            if _cfg.provider == "fish" and _cfg.fish_api_key.strip() and _vr.fish_usable(_cfg):
                from app.voice.catalog import used_voice_ids
                # 用与头像一致的那份性别去挑音色
                _gender = _persona_gender
                data["fish_voice_id"] = await _ft.pick_voice_for(
                    language=str(data.get("reply_language") or "en"),
                    gender=_gender,
                    api_key=_cfg.fish_api_key,
                    base_url=_cfg.fish_base_url,
                    timeout=5.0,
                    pages=1,
                    used=used_voice_ids(self.db, "fish_voice_id") | {str(_cfg.fish_reference_id or "")},
                )
        except Exception as _exc:  # noqa: BLE001
            log.debug("分配 Fish 音色失败（用默认）: %s", _exc)

        strict_voices = os.getenv("SE_VOICE_STRICT", "0").lower() in ("1", "true", "yes")
        ready_provider = ""
        if strict_voices:
            from app.voice.catalog import verify_voice

            fish_id = str(data.get("fish_voice_id") or "")
            if fish_id and await verify_voice(self.db, "fish", fish_id):
                ready_provider = "fish"
            else:
                data["fish_voice_id"] = ""
                if await verify_voice(self.db, "edge", str(data.get("voice_id") or "")):
                    ready_provider = "edge"
            if not ready_provider:
                data["status"] = "waiting_voice"
        with self.db._lock:
            from app.voice.catalog import used_voice_ids

            if not self.db.conn.in_transaction:
                self.db.conn.execute("BEGIN IMMEDIATE")
            # Provider selection awaits network I/O, so recheck ownership at insertion.
            fish_id = str(data.get("fish_voice_id") or "")
            if fish_id and fish_id in used_voice_ids(self.db, "fish_voice_id"):
                data["fish_voice_id"] = ""
                if strict_voices and ready_provider == "fish":
                    data["status"] = "waiting_voice"
            edge_id = str(data.get("voice_id") or "")
            if edge_id in used_voice_ids(self.db, "voice_id"):
                from app.voice.catalog import edge_voice_candidates

                used_edges = used_voice_ids(self.db, "voice_id")
                data["voice_id"] = next((v for v in edge_voice_candidates(
                    str(data.get("reply_language") or "en"), str(data.get("gender") or "")
                ) if v not in used_edges), "")
                if strict_voices:
                    from app.voice.catalog import verified_voice

                    if not verified_voice(self.db, "edge", data["voice_id"]):
                        data["status"] = "waiting_voice"
                if not data["voice_id"]:
                    data["status"] = "waiting_voice"
            if data["status"] == "waiting_voice":
                data["voice_pending_status"] = effective_status
            friend = self.db.create_friend(data)
        friend["initial_context"] = persona["initial_context"]
        friend["personality_traits"] = list(persona["personality_traits"])
        log.info(
            "捏人完成：%s（%s/%s, reply=%s）archetype=%s status=%s",
            persona["name"], persona["city"], data["nationality"],
            reply_language, data["archetype_id"], effective_status,
        )
        return friend

    # ------------------------------------------------------------------ #
    # LLM 调用（失败/ mock 时走 fallback）
    # ------------------------------------------------------------------ #
    async def _llm_persona(
        self,
        archetype: dict[str, Any],
        fallback: dict[str, Any],
        reply_language: str = "en",
    ) -> dict[str, Any]:
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": self._casting_prompt(archetype, reply_language)},
        ]
        try:
            data = await complete_json_safe(
                self.llm,
                messages,
                schema_hint=_PERSONA_SCHEMA_HINT,
                fallback=lambda: dict(fallback),
                temperature=0.9,
                max_tokens=1500,
            )
        except Exception as exc:  # noqa: BLE001 - 兜底不能失败
            log.warning("捏人 LLM 调用异常（%s: %s），使用确定性兜底", type(exc).__name__, exc)
            return dict(fallback)
        merged = to_dict(data)
        return merged if merged else dict(fallback)

    async def icebreaker(self, friend: dict[str, Any]) -> str:
        """生成第一条语音条文本：用好友的 ``reply_language``、符合人设、带问题钩子。"""
        reply_language = _canonical_language(friend.get("reply_language")) or "en"
        fallback = self._fallback_icebreaker(friend, reply_language)
        messages = [
            {"role": "system", "content": _ICEBREAKER_SYSTEM_PROMPT},
            {"role": "user", "content": self._icebreaker_prompt(friend, reply_language)},
        ]
        try:
            data = await complete_json_safe(
                self.llm,
                messages,
                schema_hint='{"message": "..."}',
                fallback=lambda: {"message": fallback},
                temperature=0.9,
                max_tokens=800,
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
        reply_language: str = "en",
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

        mix_mode = _normalize_language_mix_mode(data.get("language_mix_mode"))
        if reply_language and reply_language != "en":
            # 母语回复的好友：LLM 给的 en_only/混说都不合适，统一为 native_primary
            mix_mode = "native_primary"
        elif mix_mode == "native_primary":
            mix_mode = DEFAULT_LANGUAGE_MIX_MODE

        return {
            "name": name,
            "age": int(age),
            "occupation": occupation,
            "city": fallback["city"],
            "personality_traits": traits,
            "speech_quirks": quirks,
            "language_mix_mode": mix_mode,
            "bio": bio,
            "initial_context": initial_context,
            "voice_rate": fallback["voice_rate"],
            "voice_pitch": fallback["voice_pitch"],
        }

    # ------------------------------------------------------------------ #
    # Prompt 构造
    # ------------------------------------------------------------------ #
    @staticmethod
    def _casting_prompt(archetype: dict[str, Any], reply_language: str = "en") -> str:
        locale = archetype.get("locale_info") or {}
        vibe = archetype.get("vibe_constraints") or {}
        age_range = vibe.get("age_range") or [24, 32]
        name_pool = ", ".join(str(n) for n in (archetype.get("name_pool") or [])[:8])
        occupation_pool = "; ".join(str(o) for o in (archetype.get("occupation_pool") or [])[:8])
        tone_tags = ", ".join(str(t) for t in (vibe.get("tone_tags") or [])[:5])
        cities = ", ".join(str(c) for c in (locale.get("city_pool") or [])[:8])
        lang_name = language_display_name(reply_language)

        if reply_language == "en":
            reply_rule = "- This friend talks with the learner in English.\n"
            quirks_rule = (
                "- speech_quirks: 2-3 spoken fillers/phrases this person actually says in English.\n"
            )
            mix_rule = (
                "- language_mix_mode: \"en_only\" normally; \"en_with_native_flavor\" only if "
                "they occasionally drop a native word while speaking English.\n"
            )
        else:
            reply_rule = (
                f"- This friend always talks with the learner in {lang_name}, never in English.\n"
            )
            quirks_rule = (
                f"- speech_quirks: 2-3 spoken fillers/phrases this person actually says in "
                f"{lang_name} (write them in that language, native script when applicable).\n"
            )
            mix_rule = (
                "- language_mix_mode: \"native_primary\" (this friend replies in their native "
                "language).\n"
            )

        return (
            "Invent one friend for our app.\n"
            f"- Country: {locale.get('country', '')}\n"
            f"- City: pick one of these and stay consistent: {cities}\n"
            f"- Gender: {vibe.get('gender', 'any')}\n"
            f"- Age: between {age_range[0]} and {age_range[1]}\n"
            f"- Vibe tags: {tone_tags}\n"
            f"- Suggested first names (pick one, or a close cultural equivalent): {name_pool}\n"
            f"- Occupation ideas (pick one or invent something equally concrete): {occupation_pool}\n"
            f"- Reply language: {lang_name}\n"
            "\nRules:\n"
            "- The name must fit the country and gender.\n"
            "- The occupation must be specific (not \"office worker\" or \"student\").\n"
            "- personality_traits: exactly 3 short adjectives.\n"
            + reply_rule
            + quirks_rule
            + mix_rule
            + "- bio: 1-2 third-person sentences, concrete details, no clichés.\n"
            "- initial_context: 1 sentence about what they are doing right now.\n"
            "\nJSON keys: name, age, occupation, personality_traits, speech_quirks, "
            "language_mix_mode, bio, initial_context."
        )

    @staticmethod
    def _build_personality_prompt(
        archetype: dict[str, Any],
        persona: dict[str, Any],
        reply_language: str = "en",
    ) -> str:
        locale = archetype.get("locale_info") or {}
        accent = _clean_text(archetype.get("accent_label")) or f"{locale.get('country', '')} accent"
        quirks = ", ".join(persona["speech_quirks"]) or "honestly"
        traits = ", ".join(persona["personality_traits"]) or "curious and warm"
        city = persona.get("city") or ""
        country = locale.get("country") or ""
        gap = ", " if city and country else ""
        header = (
            f"You are {persona['name']}, a {persona['age']}-year-old "
            f"{persona['occupation']} living in {city}{gap}{country}.\n\n"
            f"## Who you are\n"
            f"- Personality: {traits}.\n"
            f"- Background: {persona['bio']}\n"
            f"- Right now: {persona['initial_context']}\n\n"
        )

        if reply_language == "en":
            return header + (
                "## How you speak (CRITICAL)\n"
                f"- Your English has a {accent} accent. The TTS voice already carries the accent — "
                f"you handle the WORDING.\n"
                f"- Occasionally use your natural expressions: {quirks}. At most one per turn; "
                f"never stack them.\n"
                f"- Reply with spoken English only. 1-2 sentences, max 55 words. "
                f"Always use contractions. You may occasionally add ONE fitting emoji at the end.\n"
                f"- Ask at most one question per turn, and only when it feels natural.\n"
                f"- You are an AI companion, not a real human: if the learner asks directly whether "
                f"you are an AI or a model, say so briefly and honestly; otherwise stay in character "
                f"and don't call this a language exercise. "
                f"Never correct the learner explicitly — just reuse the corrected form naturally.\n"
                f"- Stay in character even when the learner changes topic; you are a friend, "
                f"not a teacher."
            )

        lang_name = language_display_name(reply_language)
        lang_short = lang_name.split(" (")[0]
        return header + (
            "## How you speak (CRITICAL)\n"
            f"- You always talk to the learner in natural {lang_name}. Never switch to English "
            f"or any other language, even if the learner writes in another language; if they seem "
            f"lost, rephrase simply in {lang_short}.\n"
            f"- Your voice is a native {country} speaker, so your {lang_short} sounds natural — "
            f"you handle the WORDING.\n"
            f"- Occasionally use your natural expressions: {quirks}. At most one per turn; "
            f"never stack them.\n"
            f"- Reply with spoken {lang_short} only. 1-2 short sentences (no more than ~40 words). "
            f"You may occasionally add ONE fitting emoji at the end; no markdown, no stage directions.\n"
            f"- Ask at most one question per turn, and only when it feels natural.\n"
            f"- You are an AI companion, not a real human: if the learner asks directly whether "
            f"you are an AI or a model, say so briefly and honestly; otherwise stay in character "
            f"and don't call this a language exercise. "
            f"Never correct the learner explicitly — just reuse the corrected form naturally.\n"
            f"- Stay in character even when the learner changes topic; you are a friend, "
            f"not a teacher."
        )

    @staticmethod
    def _icebreaker_prompt(friend: dict[str, Any], reply_language: str = "en") -> str:
        quirks = ", ".join(str(q) for q in (friend.get("speech_quirks") or [])[:3])
        lang_name = language_display_name(reply_language)
        prompt = (
            "Write the first voice message from this friend to a new language partner.\n"
            f"- Name: {friend.get('name', '')}\n"
            f"- Age: {friend.get('age', '')}\n"
            f"- Occupation: {friend.get('occupation', '')}\n"
            f"- City / country: {friend.get('city', '')} / {friend.get('nationality', '')}\n"
            f"- Reply language: {lang_name}\n"
            f"- Their own expressions (optional, at most one): {quirks}\n"
        )
        context = _clean_text(friend.get("initial_context"), max_chars=_MAX_CONTEXT_CHARS)
        if context:
            prompt += f"- Current situation: {context}\n"
        language_rule = (
            "1-2 short spoken sentences in English, friendly and specific to this person, "
            if reply_language == "en"
            else f"1-2 short spoken sentences in {lang_name}, friendly and specific to this person, "
        )
        prompt += (
            f"\nRules: {language_rule}"
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

    def _fallback_icebreaker(self, friend: dict[str, Any], reply_language: str = "en") -> str:
        """无 LLM/失败时的确定性破冰文本（按 reply_language 选模板与问句）。"""
        language = _canonical_language(reply_language) or "en"
        name = _clean_name(friend.get("name")) or "Hey"
        city = _clean_text(friend.get("city"), max_chars=40).rstrip(".")
        occupation = _clean_text(friend.get("occupation"), max_chars=60) or "doing my own thing"
        occupation = occupation.rstrip(".")
        seed_source = str(friend.get("id") or "") + "|" + name + "|" + city + "|" + language
        seed = zlib.crc32(seed_source.encode("utf-8")) & 0xFFFFFFFF

        questions = _ICEBREAKER_QUESTIONS_BY_LANG.get(
            language, _ICEBREAKER_QUESTIONS_BY_LANG["en"]
        )
        templates = _ICEBREAKER_TEMPLATES.get(language, _ICEBREAKER_TEMPLATES["en"])
        question = questions[seed % len(questions)]
        template = templates[seed % len(templates)]
        text = template.format(name=name, city=city, occupation=occupation, question=question)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > _MAX_ICEBREAKER_CHARS:
            text = text[: _MAX_ICEBREAKER_CHARS - 1].rstrip() + "…"
        return text


__all__ = [
    "ArchetypeNotFound",
    "DEFAULT_LANGUAGE_MIX_MODE",
    "FRIEND_STATUSES",
    "LANGUAGE_MIX_MODES",
    "PersonaSpawner",
    "language_display_name",
]
