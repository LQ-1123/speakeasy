"""SpeakEasy V3 · Speech DNA：把「一个人怎么说话」变成结构化配置。

为什么需要
----------
审计（``docs/PERSONA_AUDIT.md`` §9）指出：人物的差异过去只来自一段自由文本
（``personality_prompt``），而 prompt 里又叠加了「每句留钩子 / 极短 / 热情」三条
硬规则，结果 12 个人物听起来像同一个客服。

Speech DNA 把「语速节奏、句长、幽默、直接度、情绪表达、用词、俚语、emoji、
主动提问、自我暴露、社交距离、分歧方式、话题转换」13 个维度变成**枚举**，于是：

1. 生成侧可以把它压成一小段英文指令（:func:`dna_prompt_block`），并显式声明
   「这是倾向不是规则 / 不强制每轮提问 / 允许一句话 / 允许不同意」；
2. 行为决策层（``app.v3.behavior``）只用 :func:`dna_traits` 的 10 个 0..1 数值，
   完全不解析文本；
3. 示例检索层按场景（:data:`SCENES`）注入「像这个人会说的」对话，
   而不是固定台词（:func:`pick_examples` / :func:`example_block`）。

铁律
----
- 派生逻辑（normalize / derive / traits / prompt / scene / example_bank / pick /
  block）**不碰网络、不碰数据库、不用全局随机**：``rng`` 一律由调用方注入，
  同输入必同输出（细微平局抖动来自 persona id 的 sha256）。
- 只有 :func:`ensure_persona_dna` 会写库；它**永不抛异常**，LLM 失败时静默回落
  到确定性示例库，保证启动链路不会因为模型挂掉而失败。
- 示例是「参考声线」不是「台词库」：注入 prompt 时明确要求模仿语气，不得照抄。

返回的 DNA dict 形状
--------------------
13 个必填枚举键（:data:`DNA_KEYS`）+ 4 个可选键：
``signature_phrases / banned_phrases / punctuation_habit / note``。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from app.logging_utils import get_logger

log = get_logger(__name__)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
#: DNA 结构版本（写入 ``v3_meta.persona_dna_version``）
SPEECH_DNA_VERSION = "1"

#: 示例对话覆盖的 12 个情境（顺序即写入 ordinal 的顺序）
SCENES: tuple[str, ...] = (
    "small_talk",
    "life_share",
    "happy",
    "tired",
    "joke",
    "disagree",
    "comfort",
    "private_question",
    "unsure",
    "short_reply",
    "wrap_up",
    "reconnect",
)

#: 13 个必填枚举键（契约见 docs/PERSONA_UPGRADE_PLAN.md §3.1）
DNA_KEYS: tuple[str, ...] = (
    "sentence_rhythm",
    "directness",
    "humor_style",
    "emotional_expression",
    "vocabulary_style",
    "slang_usage",
    "emoji_usage",
    "verbosity",
    "conversation_initiative",
    "self_disclosure",
    "social_distance",
    "disagreement_style",
    "topic_transition",
)

#: 可选键：口头禅 / 禁语 / 标点习惯 / 一句人设补充
OPTIONAL_KEYS: tuple[str, ...] = (
    "signature_phrases",
    "banned_phrases",
    "punctuation_habit",
    "note",
)

#: 取值域（枚举，不要 1-10 分：分数无法稳定映射到 prompt）
DNA_ENUMS: dict[str, tuple[str, ...]] = {
    "sentence_rhythm": ("clipped", "measured", "flowing", "fragmented"),
    "directness": ("blunt", "direct", "diplomatic", "indirect"),
    "humor_style": ("none", "dry", "sarcastic", "playful", "absurd"),
    "emotional_expression": ("reserved", "warm", "expressive", "volatile"),
    "vocabulary_style": ("plain", "precise", "literary", "street", "technical"),
    "slang_usage": ("none", "rare", "moderate", "heavy"),
    "emoji_usage": ("never", "rare", "sometimes", "often"),
    "verbosity": ("one_line", "short", "medium", "long"),
    "conversation_initiative": ("low", "medium", "high"),
    "self_disclosure": ("guarded", "measured", "open"),
    "social_distance": ("distant", "neutral", "familiar", "intimate"),
    "disagreement_style": ("avoid", "soften", "state_plainly", "challenge"),
    "topic_transition": ("stay", "gentle_shift", "abrupt_shift", "callback"),
}

#: 缺字段时的中性默认 DNA（derive 的兜底、normalize 的回落）
DEFAULT_DNA: dict[str, str] = {
    "sentence_rhythm": "measured",
    "directness": "direct",
    "humor_style": "dry",
    "emotional_expression": "warm",
    "vocabulary_style": "plain",
    "slang_usage": "rare",
    "emoji_usage": "rare",
    "verbosity": "short",
    "conversation_initiative": "medium",
    "self_disclosure": "measured",
    "social_distance": "neutral",
    "disagreement_style": "soften",
    "topic_transition": "gentle_shift",
}

#: 语言代码 → LLM 指令里用的英文语言名（种子语料 9 种语言）
_LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
    "zh": "Simplified Chinese",
}

#: :func:`dna_traits` 的取值映射（枚举 → 0..1）
_TRAIT_RHYTHM = {"clipped": 0.2, "measured": 0.35, "flowing": 0.45, "fragmented": 0.85}
_TRAIT_DIRECTNESS = {"blunt": 0.95, "direct": 0.72, "diplomatic": 0.38, "indirect": 0.18}
_TRAIT_HUMOUR = {"none": 0.05, "dry": 0.5, "sarcastic": 0.68, "playful": 0.82, "absurd": 0.92}
_TRAIT_EMOTION = {"reserved": 0.22, "warm": 0.6, "expressive": 0.82, "volatile": 0.88}
_TRAIT_SLANG = {"none": 0.0, "rare": 0.22, "moderate": 0.55, "heavy": 0.9}
_TRAIT_EMOJI = {"never": 0.0, "rare": 0.2, "sometimes": 0.55, "often": 0.9}
_TRAIT_VERBOSITY = {"one_line": 0.12, "short": 0.32, "medium": 0.62, "long": 0.92}
_TRAIT_INITIATIVE = {"low": 0.2, "medium": 0.5, "high": 0.82}
_TRAIT_DISCLOSURE = {"guarded": 0.15, "measured": 0.5, "open": 0.9}
_TRAIT_DISTANCE = {"distant": 0.15, "neutral": 0.45, "familiar": 0.72, "intimate": 0.95}

#: DNA 字段 → (traits 映射表, 词库里的 key)：词库可覆盖数值
_TRAIT_BY_FIELD: dict[str, dict[str, float]] = {
    "sentence_rhythm": _TRAIT_RHYTHM,
    "directness": _TRAIT_DIRECTNESS,
    "humor_style": _TRAIT_HUMOUR,
    "emotional_expression": _TRAIT_EMOTION,
    "slang_usage": _TRAIT_SLANG,
    "emoji_usage": _TRAIT_EMOJI,
    "verbosity": _TRAIT_VERBOSITY,
    "conversation_initiative": _TRAIT_INITIATIVE,
    "self_disclosure": _TRAIT_DISCLOSURE,
    "social_distance": _TRAIT_DISTANCE,
}

#: 全局禁用词（由词库 ``phrases.banned_global`` 注入；每条 DNA prompt 都会带上）
_GLOBAL_BANNED: list[str] = []

_ENV_LLM_TIMEOUT = "SE_DNA_LLM_TIMEOUT_S"

_TOKEN_RE = re.compile(r"[a-z0-9']+")


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #
def _as_mapping(value: Any) -> dict:
    """dict / JSON 字符串 / sqlite Row → dict；其它返回 ``{}``。"""
    if isinstance(value, Mapping):
        return dict(value)
    if value is not None and hasattr(value, "keys"):  # sqlite3.Row
        try:
            return dict(value)
        except (TypeError, ValueError):
            return {}
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _as_list(value: Any) -> list:
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return [raw]
        return list(parsed) if isinstance(parsed, (list, tuple)) else [raw]
    return []


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_text(value: Any, *, limit: int = 160) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().split())[:limit]


def _clean_phrases(value: Any, *, limit: int = 4, max_chars: int = 40) -> list[str]:
    """口头禅/禁语：去空白、去重、最多 ``limit`` 条、每条最长 ``max_chars``。"""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        text = _clean_text(item, limit=max_chars)
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _clip01(value: float) -> float:
    return round(min(1.0, max(0.0, value)), 3)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _hash_int(*parts: Any) -> int:
    """稳定哈希（跨进程一致；不使用 Python 内置 hash 的随机种子）。"""
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")


def _hash_pick(seed_parts: Sequence[Any], options: Sequence[str]) -> str:
    return options[_hash_int(*seed_parts) % len(options)]


# --------------------------------------------------------------------------- #
# 取值域与归一化
# --------------------------------------------------------------------------- #
def _is_valid_dna(value: Any) -> bool:
    """13 个必填键是否齐全且全部是合法枚举（可选键不参与判定）。"""
    data = _as_mapping(value)
    if not data:
        return False
    for key in DNA_KEYS:
        raw = data.get(key)
        if not isinstance(raw, str) or raw.strip().lower() not in DNA_ENUMS[key]:
            return False
    return True


def normalize_dna(value: dict | str | None) -> dict:
    """任意脏输入 → 完整合法 DNA。

    - 缺字段用 :data:`DEFAULT_DNA`（derive 的兜底值）；
    - 非法枚举回落到默认值；
    - 可选键一律补齐（``signature_phrases`` / ``banned_phrases`` 为空列表，
      ``punctuation_habit`` / ``note`` 为空字符串）。
    """
    data = _as_mapping(value)
    dna: dict[str, Any] = {}
    for key in DNA_KEYS:
        raw = data.get(key)
        allowed = DNA_ENUMS[key]
        if isinstance(raw, str) and raw.strip().lower() in allowed:
            dna[key] = raw.strip().lower()
        else:
            dna[key] = DEFAULT_DNA[key]
    dna["signature_phrases"] = _clean_phrases(data.get("signature_phrases"))
    dna["banned_phrases"] = _clean_phrases(data.get("banned_phrases"))
    dna["punctuation_habit"] = _clean_text(data.get("punctuation_habit"), limit=80)
    dna["note"] = _clean_text(data.get("note"), limit=200)
    return dna


# --------------------------------------------------------------------------- #
# 从 persona 行确定性推导 DNA
# --------------------------------------------------------------------------- #
def _has(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)


_LENGTH_ORDER: tuple[str, ...] = ("one_line", "short", "medium", "long")

#: 性格 → 句长偏移（在能量估算基础上微调）
_LENGTH_DELTA: dict[str, int] = {
    "quiet": -1,
    "sleepy": -1,
    "blunt": -1,
    "dry": -1,
    "calm": 0,
    "sarcastic": 0,
    "playful": 0,
    "warm": 0,
    "witty": 1,
    "poetic": 1,
    "passionate": 1,
    "upbeat": 1,
}


def _energy_verbosity(energy: float) -> int:
    if energy < 0.38:
        return 0
    if energy < 0.55:
        return 1
    if energy < 0.75:
        return 2
    return 3


def derive_dna(persona: dict) -> dict:
    """从 persona 行确定性推导 DNA。

    只看身份文本与 ``ai_config``（nickname/personality/bio/interests/occupation/
    archetype_id/ai_config），**不读** speech_dna 本身——seed 语料里的 speech_dna
    由 :func:`ensure_persona_dna` 优先使用，这里只是老数据的自动回填。

    同输入必同输出：平局抖动来自 ``sha256(persona id)``，不使用全局随机。
    """
    data = _as_mapping(persona)
    pid = str(
        data.get("id") or data.get("persona_id") or data.get("nickname") or "persona"
    ).strip() or "persona"
    personality = str(data.get("personality") or "")
    bio = str(data.get("bio") or "")
    occupation = str(data.get("occupation") or "")
    archetype_id = str(data.get("archetype_id") or "")
    interests = [str(item) for item in _as_list(data.get("interests"))]
    ai_config = _as_mapping(data.get("ai_config"))
    temperament = str(ai_config.get("temperament") or "").strip().lower()
    energy = min(1.0, max(0.0, _to_float(ai_config.get("energy"), 0.5)))
    text = " ".join((personality, bio, occupation, " ".join(interests), archetype_id)).lower()

    # ---- 幽默 ---------------------------------------------------------- #
    if _has(text, "sarcastic", "sarcasm", "ironic", "irony") or temperament == "sarcastic":
        humor = "sarcastic"
    elif temperament in ("witty", "poetic") or _has(
        text, "dry", "deadpan", "self-deprecating", "trocken", "dead pan"
    ):
        humor = "dry"
    elif _has(text, "absurd", "surreal", "ridiculous", "nonsense"):
        humor = "absurd"
    elif temperament in ("playful", "upbeat", "warm", "passionate") or _has(
        text, "funny", "joke", "playful", "laugh", "유쾌", "搞笑", "幽默", "开心"
    ):
        humor = "playful"
    elif temperament in ("quiet", "sleepy", "blunt", "calm"):
        humor = "none" if temperament in ("quiet", "sleepy") else "dry"
    else:
        humor = _hash_pick((pid, "humor"), ("dry", "playful", "none"))

    # ---- 节奏 ---------------------------------------------------------- #
    if temperament in ("blunt", "dry") or _has(
        text,
        "clipped",
        "terse",
        "short sentences",
        "fragmented",
        "dry",
        "deadpan",
        "seca",
        "seco",
        "diretta",
        "direkt",
        "寡黙",
        "短い",
        "sleepy",
        "tired",
    ):
        rhythm = "clipped"
    elif temperament in ("playful", "sarcastic", "upbeat") or _has(
        text, "rambling", "scattered", "fragment"
    ):
        rhythm = "fragmented"
    elif temperament in ("poetic", "warm", "passionate") or _has(text, "poetic", "flowing"):
        rhythm = "flowing"
    else:
        rhythm = _hash_pick((pid, "rhythm"), ("measured", "clipped", "flowing"))

    # ---- 句长 ---------------------------------------------------------- #
    verbosity_index = _energy_verbosity(energy) + _LENGTH_DELTA.get(temperament, 0)
    if rhythm == "clipped":  # 说话像短句的人，不会突然长篇大论
        verbosity_index = min(verbosity_index, 1)
    if _has(text, "very short", "one line", "one-liner"):
        verbosity_index = min(verbosity_index, 0)
    if _has(text, "long stories", "storyteller", "rambling", "novel-length"):
        verbosity_index = max(verbosity_index, 3)
    verbosity = _LENGTH_ORDER[min(3, max(0, verbosity_index))]

    # ---- 直接度 -------------------------------------------------------- #
    if temperament in ("blunt", "dry") or _has(
        text,
        "blunt",
        "dry",
        "diretta",
        "dirett",
        "direkt",
        "seca",
        "no-nonsense",
        "straight-talking",
    ):
        directness = "blunt"
    elif temperament == "calm" or _has(
        text, "diplomatic", "tactful", "gentle", "careful", "considerate", "委婉"
    ):
        directness = "diplomatic"
    elif temperament in ("quiet", "sleepy") or _has(text, "indirect", "vague", "avoid"):
        directness = "indirect"
    elif _has(text, "direct", "honest", "opinionated", "state plainly"):
        directness = "direct"
    else:
        directness = _hash_pick((pid, "directness"), ("direct", "diplomatic", "blunt"))

    # ---- 情绪表达 ------------------------------------------------------ #
    if temperament == "volatile" or _has(text, "volatile", "explosive", "moody"):
        emotion = "volatile"
    elif temperament in ("passionate", "warm", "upbeat", "playful") or energy >= 0.8:
        emotion = "expressive"
    elif temperament in ("quiet", "sleepy", "blunt", "dry") or energy <= 0.4:
        emotion = "reserved"
    else:
        emotion = _hash_pick((pid, "emotion"), ("warm", "reserved", "expressive"))

    # ---- 用词 ---------------------------------------------------------- #
    if _has(
        text, "poetic", "poetry", "novel", "books", "literature", "language", "writing", "詩", "本を"
    ) or "english teacher" in occupation.lower():
        vocabulary = "literary"
    elif _has(
        text,
        "engineer",
        "engineering",
        "coding",
        "code",
        "computer science",
        "programming",
        "software",
        "technical",
        "synth",
        "编程",
        "代码",
        "工科",
    ):
        vocabulary = "technical"
    elif _has(text, "architect", "design", "precise", "precision", "exact", "细节", "设计", "建筑"):
        vocabulary = "precise"
    elif _has(text, "slang", "street", "mate", "bruv", "gonna", "wanna"):
        vocabulary = "street"
    elif _has(text, "plain", "simple", "straightforward"):
        vocabulary = "plain"
    else:
        vocabulary = _hash_pick((pid, "vocab"), ("plain", "precise", "literary"))

    # ---- 俚语 ---------------------------------------------------------- #
    if _has(text, "slang", "mate", "bruv", "lad", "gonna", "wanna", "ain't") and energy >= 0.6:
        slang = "heavy"
    elif temperament == "sarcastic" and energy >= 0.65:
        slang = "heavy"
    elif temperament in ("sarcastic", "playful", "upbeat"):
        slang = "moderate"
    elif temperament in ("quiet", "sleepy", "blunt", "dry", "poetic", "calm"):
        slang = "none"
    else:
        slang = _hash_pick((pid, "slang"), ("none", "rare", "moderate"))

    # ---- emoji --------------------------------------------------------- #
    if _has(text, "emoji", "emojis"):
        emoji = _hash_pick((pid, "emoji"), ("sometimes", "often"))
    elif temperament in ("quiet", "sleepy", "blunt", "dry", "calm"):
        emoji = "never"
    elif temperament in ("passionate", "warm", "upbeat", "playful", "expressive") or energy >= 0.75:
        emoji = "sometimes"
    else:
        emoji = _hash_pick((pid, "emoji"), ("never", "rare", "sometimes"))

    # ---- 主动性 -------------------------------------------------------- #
    if energy >= 0.72 or temperament in ("upbeat", "playful", "passionate"):
        initiative = "high"
    elif energy <= 0.42 or temperament in ("quiet", "sleepy"):
        initiative = "low"
    else:
        initiative = _hash_pick((pid, "initiative"), ("medium", "high", "low"))

    # ---- 自我暴露 ------------------------------------------------------ #
    if temperament == "quiet" or _has(text, "guarded", "reserved", "private", "closed book"):
        disclosure = "guarded"
    elif temperament in ("warm", "passionate", "playful", "upbeat") or energy >= 0.75:
        disclosure = "open"
    else:
        disclosure = _hash_pick((pid, "disclosure"), ("measured", "measured", "open", "guarded"))

    # ---- 社交距离 ------------------------------------------------------ #
    if temperament in ("warm", "playful", "upbeat", "passionate") or energy >= 0.75:
        distance = "familiar"
    elif temperament == "quiet":
        distance = "distant"
    elif temperament in ("sleepy", "blunt", "dry"):
        distance = "neutral"
    else:
        distance = _hash_pick((pid, "distance"), ("neutral", "familiar", "distant"))

    # ---- 分歧方式：由直接度派生，保证自洽 ------------------------------ #
    disagreement = {
        "blunt": "challenge",
        "direct": "state_plainly",
        "diplomatic": "soften",
        "indirect": "avoid",
    }[directness]

    # ---- 话题转换 ------------------------------------------------------ #
    if temperament in ("witty", "playful", "sarcastic", "poetic"):
        transition = "callback"
    elif temperament in ("blunt", "passionate"):
        transition = "abrupt_shift"
    elif temperament in ("quiet", "sleepy"):
        transition = "stay"
    else:
        transition = _hash_pick(
            (pid, "transition"), ("gentle_shift", "gentle_shift", "callback", "abrupt_shift")
        )

    return normalize_dna(
        {
            "sentence_rhythm": rhythm,
            "directness": directness,
            "humor_style": humor,
            "emotional_expression": emotion,
            "vocabulary_style": vocabulary,
            "slang_usage": slang,
            "emoji_usage": emoji,
            "verbosity": verbosity,
            "conversation_initiative": initiative,
            "self_disclosure": disclosure,
            "social_distance": distance,
            "disagreement_style": disagreement,
            "topic_transition": transition,
        }
    )


# --------------------------------------------------------------------------- #
# DNA → 数值 traits（behavior.py 只依赖这 10 个数）
# --------------------------------------------------------------------------- #
def dna_traits(dna: dict) -> dict[str, float]:
    """把枚举 DNA 压成 10 个 0..1 的数值，供行为决策层使用。

    返回键：``initiative / warmth / humour / directness / verbosity / disclosure /
    emoji / slang / playfulness / social_distance``。
    """
    d = normalize_dna(dna)
    humour = _TRAIT_HUMOUR[d["humor_style"]]
    directness = _TRAIT_DIRECTNESS[d["directness"]]
    verbosity = _TRAIT_VERBOSITY[d["verbosity"]]
    emotion = _TRAIT_EMOTION[d["emotional_expression"]]
    distance = _TRAIT_DISTANCE[d["social_distance"]]
    emoji = _TRAIT_EMOJI[d["emoji_usage"]]
    slang = _TRAIT_SLANG[d["slang_usage"]]
    disclosure = _TRAIT_DISCLOSURE[d["self_disclosure"]]
    rhythm = _TRAIT_RHYTHM[d["sentence_rhythm"]]

    warmth = 0.45 * emotion + 0.25 * distance + 0.15 * humour + 0.15 * (1.0 - directness)
    playfulness = 0.5 * humour + 0.3 * rhythm + 0.2 * emoji
    initiative = 0.7 * _TRAIT_INITIATIVE[d["conversation_initiative"]] + 0.15 * disclosure + 0.15 * emotion

    return {
        "initiative": _clip01(initiative),
        "warmth": _clip01(warmth),
        "humour": _clip01(humour),
        "directness": _clip01(directness),
        "verbosity": _clip01(verbosity),
        "disclosure": _clip01(disclosure),
        "emoji": _clip01(emoji),
        "slang": _clip01(slang),
        "playfulness": _clip01(playfulness),
        "social_distance": _clip01(distance),
    }


# --------------------------------------------------------------------------- #
# DNA → prompt 片段
# --------------------------------------------------------------------------- #
def _language_name(language: str) -> str:
    lang = str(language or "en").strip().lower()
    return _LANGUAGE_NAMES.get(lang, lang or "English")


def dna_prompt_block(dna: dict, *, language: str = "en", max_chars: int = 900) -> str:
    """紧凑英文指令块（元语言固定用英文，因为模型对英文指令最稳）。

    必需约束（必须出现在 prompt 里）：
    a) 这是**倾向不是规则**；b) 不强制每轮提问；c) 允许一句话回复；d) 允许不同意。
    """
    d = normalize_dna(dna)
    lang_name = _language_name(language)
    lines = [
        f"[Speech DNA] How you talk. Reply language: {lang_name}.",
        "- These are TENDENCIES, not rules. Vary them beat by beat; never sound scripted.",
        "- Do NOT force a question every turn. Many turns need no question at all.",
        "- A one-line reply is allowed and often better than a paragraph.",
        "- Disagreeing is allowed: say no plainly when that is your honest read.",
        f"- Rhythm: {d['sentence_rhythm']}; typical length: {d['verbosity']}; "
        f"humour: {d['humor_style']}; directness: {d['directness']}.",
        f"- Emotion: {d['emotional_expression']}; emoji: {d['emoji_usage']}; "
        f"vocabulary: {d['vocabulary_style']}; slang: {d['slang_usage']}.",
        f"- Initiative: {d['conversation_initiative']} (let it emerge, do not force it); "
        f"self-disclosure: {d['self_disclosure']}; social distance: {d['social_distance']}.",
        f"- Disagreement: {d['disagreement_style']}; topic shifts: {d['topic_transition']}.",
    ]
    if d["signature_phrases"]:
        phrases = " / ".join(d["signature_phrases"])
        lines.append(
            f"- Signature phrases (seasoning only, drop in at most once in a while, "
            f"in your own language): {phrases}"
        )
    if d["banned_phrases"]:
        banned = " / ".join(d["banned_phrases"])
        lines.append(f"- Never say things like: {banned}")
    # 词库里的全局禁用词（红线）：并进同一条 "Never say" 里，避免多占一行
    # 把"标点习惯 / 人设补充"这类硬约束挤出预算。
    banned_all = list(
        dict.fromkeys(
            [w for w in (d["banned_phrases"] or []) if str(w).strip()]
            + [w for w in _GLOBAL_BANNED if str(w).strip()]
        )
    )[:10]
    if banned_all:
        lines.append(f"- Never say things like: {' / '.join(banned_all)}")
    tail: list[str] = []
    if d["punctuation_habit"]:
        habit = d["punctuation_habit"].strip().rstrip(".")
        if habit:
            tail.append(f"- Punctuation habit: {habit[0].upper() + habit[1:]}.")
    if d["note"]:
        tail.append(f"- Persona note: {d['note']}")
    # 尾部是硬约束（标点/人设补充）：先给它们留预算，再拼主体，避免被截断
    block = "\n".join(lines)
    if max_chars > 0 and tail:
        reserve = sum(len(item) + 1 for item in tail)
        if len(block) + reserve > max_chars:
            block = block[: max(0, max_chars - reserve)]
    for extra in tail:
        candidate = f"{block}\n{extra}"
        if max_chars > 0 and len(candidate) > max_chars:
            break
        block = candidate
    if max_chars > 0 and len(block) > max_chars:
        block = block[:max_chars]
    return block


# --------------------------------------------------------------------------- #
# 当前轮情境 → SCENES
# --------------------------------------------------------------------------- #
#: 行为 → 场景。**behavior 优先级高于文本**：行为层已经消化过意图，
#: 例如用户说 "I failed my exam" 时行为层选中 comfort，场景就不该退化成
#: short_reply；只有 behavior 是 ``respond``（或未知行为）时才走文本判定。
_BEHAVIOR_SCENES: dict[str, str] = {
    "comfort": "comfort",
    "empathize": "comfort",
    "disagree": "disagree",
    "joke": "joke",
    "tease": "joke",
    "share": "life_share",
    "tell_story": "life_share",
    "acknowledge": "short_reply",
    "end_conversation": "wrap_up",
    "ask_question": "small_talk",
    "celebrate": "happy",
}

_BAD_NEWS = (
    "sad", "tired", "exhausted", "stressed", "stress", "cry", "cried", "crying",
    "broke up", "breakup", "fired", "lost my", "failed", "fail my", "flunked",
    "bad day", "rough week", "rough day", "awful", "terrible", "miserable",
    "anxious", "lonely", "overwhelmed", "burned out", "burnt out", "gave up",
    "难过", "累", "崩溃", "哭", "分手", "失业", "失眠", "压力", "焦虑", "挂了", "考砸",
)
_TIRED_WORDS = (
    "tired", "exhausted", "sleepy", "burned out", "burnt out", "no sleep",
    "累", "失眠", "熬夜", "困",
)
_GOOD_NEWS = (
    "great news", "good news", "amazing", "excited", "promoted", "got the job",
    "passed", "won", "finally", "so happy", "proud", "celebrate",
    "开心", "成功", "升职", "通过了", "好消息", "终于", "赢了", "过了",
)
_GREETING = ("hi", "hey", "hello", "morning", "evening", "yo", "你好", "早", "嗨", "在吗")
_RECONNECT = (
    "long time", "been a while", "it's been", "its been", "haven't talked",
    "havent talked", "how have you been", "missed you", "back again",
    "remember me", "好久不见", "最近怎么样", "还记得", "回来了",
)
_UNCERTAIN = (
    "not sure", "don't know", "dont know", "dunno", "no idea", "confused",
    "i guess", "不知道", "不确定", "纠结", "迷茫",
)
_CLOSING = (
    "goodnight", "good night", "bye", "see you", "talk later", "go to bed",
    "go to sleep", "bedtime", "gonna sleep", "off to bed",
    "晚安", "拜拜", "再见", "先睡了", "改天聊", "去睡", "睡觉了",
)
_DISAGREE_HINTS = (
    "i disagree", "don't agree", "dont agree", "disagree with", "overrated",
    "i hate", "sucks", "boring", "don't like", "dont like", "stupid",
    "不同意", "反对", "讨厌", "不喜欢", "无聊",
)
#: 只有**明确的私密提问**才进 private_question；普通 "weekend/day/life" 问候不算。
_PRIVATE_QUESTION = (
    "personal", "too personal", "private question", "something private",
    "relationship status", "are you single", "how much do you earn",
    "how much money", "your salary", "asking you something", "ask you something",
    "your age", "私密", "个人问题", "工资", "收入", "单身", "谈过几个",
)
_FUNNY = ("lol", "haha", "lmao", "funny", "joke", "哈哈哈", "搞笑", "笑话")


def _short_reply_limit(dna: Mapping[str, Any]) -> int:
    """极短回复阈值：话少的人物可以把更多句子归到 short_reply。"""
    return 18 if dna["verbosity"] in ("one_line", "short") else 14


def scene_for_context(*, user_text: str, behavior: str, dna: dict) -> str:
    """用户文本 + 本轮行为 → :data:`SCENES` 之一（用于示例检索）。

    判定顺序（重要）：

    1. **behavior 优先**：comfort/empathize→comfort、disagree→disagree、
       joke/tease→joke、share/tell_story→life_share、acknowledge→short_reply、
       end_conversation→wrap_up、ask_question→small_talk、celebrate→happy；
    2. 只有 ``respond`` / 未知行为时才看文本，且**先判负面经历**
       （failed / exhausted / bad day → comfort / tired），避免被「短文本」
       规则误判成 short_reply；
    3. ``private_question`` 只认明确的私密提问词（personal / private /
       relationship status / salary ...），普通问候一律 small_talk。

    纯规则、无随机：同一输入永远同一场景。``dna`` 只影响「多短算 short_reply」。
    """
    text = " ".join(str(user_text or "").strip().lower().split())
    act = str(behavior or "").strip().lower()
    d = normalize_dna(dna)

    # 1) 行为优先（行为层已理解意图，场景不要被字面词带跑）
    mapped = _BEHAVIOR_SCENES.get(act)
    if mapped:
        return mapped
    if not text:
        return "short_reply"

    # 2) 文本判定（respond / 未知行为）
    if _has(text, *_CLOSING):
        return "wrap_up"
    if _has(text, *_RECONNECT):
        return "reconnect"
    if _has(text, *_BAD_NEWS):
        return "tired" if _has(text, *_TIRED_WORDS) else "comfort"
    if _has(text, *_DISAGREE_HINTS):
        return "disagree"
    if _has(text, *_FUNNY) and d["humor_style"] != "none":
        return "joke"
    if _has(text, *_GOOD_NEWS):
        return "happy"
    if _has(text, *_PRIVATE_QUESTION):
        return "private_question"
    if _has(text, *_UNCERTAIN):
        return "unsure"
    if _has(text, *_GREETING) and len(text) > 10:
        return "small_talk"
    if len(text) <= _short_reply_limit(d):
        return "short_reply"
    return "small_talk"


# --------------------------------------------------------------------------- #
# 确定性兜底示例库（12 场景 × 1 组）
# --------------------------------------------------------------------------- #
#: 每个场景：user 模板（用 person 的 nickname/city/occupation/interest 插值），
#: core（主句）/ extra（中等长度追加）/ extra2（长回复追加）。
_SCENE_SPECS: tuple[dict[str, str], ...] = (
    {
        "scene": "small_talk",
        "user": "Hey {nickname}, what's the {occupation} life like in {city} these days?",
        "core": "Busy, but the good kind of busy.",
        "extra": "Most of it is {interest}, if I'm honest.",
        "extra2": "I keep saying I'll slow down and then I don't.",
    },
    {
        "scene": "life_share",
        "user": "What did you get up to today?",
        "core": "Mainly {interest}, as usual.",
        "extra": "There was one small moment that made the whole day worth it.",
        "extra2": "I'll probably do the same thing again tomorrow.",
    },
    {
        "scene": "happy",
        "user": "I got some good news today and I have to tell someone.",
        "core": "Oh nice. Tell me.",
        "extra": "I like good news. It's rare enough to take seriously.",
        "extra2": "And I want the details, not the summary.",
    },
    {
        "scene": "tired",
        "user": "I'm completely exhausted today.",
        "core": "Yeah. Long one?",
        "extra": "I know that feeling. The brain just stops accepting new information.",
        "extra2": "Honestly, sometimes the only fix is sleep.",
    },
    {
        "scene": "joke",
        "user": "Say something funny about my Monday.",
        "core": "Your Monday is doing its best.",
        "extra": "Which is more than most Mondays manage.",
        "extra2": "I'd give it a participation medal.",
    },
    {
        "scene": "disagree",
        "user": "Honestly, I think {interest} is overrated.",
        "core": "Hard disagree, sorry.",
        "extra": "I've spent too much time on it to be neutral.",
        "extra2": "But I'm listening, if you have a real argument.",
    },
    {
        "scene": "comfort",
        "user": "I've had a rough week and I don't really know what to say.",
        "core": "That sounds heavy.",
        "extra": "You don't have to explain all of it. I'm here anyway.",
        "extra2": "We can just talk about something small if that helps.",
    },
    {
        "scene": "private_question",
        "user": "Can I ask you something a bit personal?",
        "core": "Depends how personal.",
        "extra": "But ask. I'd rather answer honestly than dodge it.",
        "extra2": "If I don't want to answer, I'll say so.",
    },
    {
        "scene": "unsure",
        "user": "I can't decide whether to move to another city.",
        "core": "Honestly? No idea.",
        "extra": "I moved once and mostly learned which boxes are too heavy.",
        "extra2": "What's making you hesitate?",
    },
    {
        "scene": "short_reply",
        "user": "Hey.",
        "core": "Hey.",
        "extra": "You're up late.",
        "extra2": "I was just about to head out.",
    },
    {
        "scene": "wrap_up",
        "user": "I should probably go to bed.",
        "core": "Go. Sleep is good.",
        "extra": "Talk tomorrow, if you want.",
        "extra2": "I'll be around.",
    },
    {
        "scene": "reconnect",
        "user": "It's been a while. How have you been?",
        "core": "Been a while, yeah.",
        "extra": "Still in {city}, still doing {occupation} things.",
        "extra2": "And you? What changed?",
    },
)

#: 幽默风格 → joke 场景的主句变体
_JOKE_CORES: dict[str, str] = {
    "none": "I don't really do jokes, but your Monday looks tired.",
    "dry": "Your Monday is doing its best.",
    "sarcastic": "Your Monday is a masterpiece. Truly.",
    "playful": "Your Monday and my Monday should start a support group.",
    "absurd": "Your Monday is a conspiracy and I respect it.",
}

#: 分歧方式 → disagree 场景的主句变体（``{interest}`` 由调用方插值）
_DISAGREE_CORES: dict[str, str] = {
    "avoid": "I'd rather not argue about {interest}.",
    "soften": "Hmm, I see it differently.",
    "state_plainly": "I disagree.",
    "challenge": "Hard disagree, sorry.",
}

#: 低主动人物的小回复（连寒暄都不展开）
_SHORT_REPLY_EMOJI: tuple[str, ...] = ("🙂", "😅", "😌", "✨", "🙃", "☕")

#: ``example_bank`` 支持的兜底示例语言。``en`` 用上面的英文模板；
#: 其余 8 种用下面的原生正文。不在列表里的语言（空/未知）一律回落 en。
_SUPPORTED_EXAMPLE_LANGS: tuple[str, ...] = ("en", "ja", "ko", "pt", "es", "de", "fr", "it", "zh")

#: 8 种语言 × 12 场景的原生 ``persona_line``（与 :data:`SCENES` 同序）。
#:
#: 为什么需要：``example_bank`` 是**无 LLM 时的确定性兜底**，会被
#: ``ensure_persona_dna`` 写进 ``persona_example_dialogs``。如果正文永远是英文，
#: 注入韩语/日语人物会把 prompt 的语言带跑（触发二次语言纠偏，违反
#: 「一轮只调一次」的成本约束）。``user_line`` 仍用英文模板（学习者在说英文，
#: 人物用母语回答），只有 ``persona_line`` 必须是该语言。
_LOCALE_PERSONA_LINES: dict[str, tuple[str, ...]] = {
    "ja": (
        "まあ、ぼちぼちだよ。",
        "今日は写真を撮ってた。それだけ。",
        "いいね。よかったじゃん。",
        "うん。長い一日だったね。",
        "月曜日は、月曜日なりに頑張ってるよ。",
        "それはちょっと違うと思う。",
        "つらかったね。無理に話さなくていい。",
        "内容によるかな。聞いてみて。",
        "うーん、正直わからない。",
        "うん。",
        "おやすみ。また明日。",
        "久しぶりだね。元気だった？",
    ),
    "ko": (
        "그럭저럭 지내지. 너는?",
        "오늘은 코딩하다가 시간 다 갔어. 진짜.",
        "오, 좋은데? 축하해!",
        "아 진짜 오늘 너무 힘들었어.",
        "월요일이 나보다 더 힘들어 보인다.",
        "음, 나는 좀 다르게 생각해.",
        "많이 힘들었겠다. 안 괜찮아도 괜찮아.",
        "어떤 거? 너무 깊은 거면 생각해 볼게.",
        "몰라, 진짜 모르겠어.",
        "응.",
        "자러 가자. 잘 자.",
        "오랜만이다! 잘 지냈어?",
    ),
    "pt": (
        "Tudo bem, correria normal. E você?",
        "Hoje passei o dia ensaiando. O corpo reclama, mas valeu.",
        "Ai, que notícia boa! Me conta tudo.",
        "Nossa, eu entendo. Dia longo é assim mesmo.",
        "Segunda-feira tem cara de quem também não queria acordar.",
        "Olha, eu discordo. Mas me conta por quê.",
        "Que semana difícil, gente. Não precisa explicar tudo, não.",
        "Depende do que é. Pode perguntar.",
        "Sinceramente? Não faço ideia.",
        "Oi!",
        "Vai dormir. Amanhã a gente conversa.",
        "Quanto tempo! Como você tá?",
    ),
    "es": (
        "Bien. Con trabajo, como siempre.",
        "Hoy, arquitectura y café. Poco más.",
        "Qué bien. Me alegro.",
        "Sí. Día largo.",
        "El lunes también está haciendo lo que puede.",
        "No estoy de acuerdo.",
        "Vaya. Lo siento.",
        "Depende. Pregunta.",
        "Ni idea, sinceramente.",
        "Hola.",
        "Vete a dormir. Buenas noches.",
        "Cuánto tiempo. ¿Qué tal?",
    ),
    "de": (
        "Ganz gut. Arbeit, wie immer.",
        "Heute Technik, Klettern, wenig Schlaf.",
        "Freut mich.",
        "Ja. Langer Tag.",
        "Montag gibt auch nur sein Bestes.",
        "Sehe ich anders.",
        "Das klingt hart.",
        "Kommt drauf an. Frag.",
        "Keine Ahnung, ehrlich.",
        "Hey.",
        "Schlaf gut.",
        "Lange her. Wie geht's?",
    ),
    "fr": (
        "Ça va, la routine. Et toi ?",
        "Aujourd'hui, boulot et une petite balade. Rien de fou.",
        "Oh, génial ! Raconte-moi.",
        "Ouais, journée longue. Je comprends.",
        "Le lundi fait ce qu'il peut, hein.",
        "Là, je ne suis pas d'accord.",
        "Ça a l'air dur. Pas besoin de tout expliquer.",
        "Ça dépend. Vas-y, demande.",
        "Honnêtement ? Aucune idée.",
        "Salut.",
        "Va dormir. À demain.",
        "Ça fait longtemps ! Comment tu vas ?",
    ),
    "it": (
        "Bene dai, solita cucina. E tu?",
        "Oggi ho cucinato per otto. Nessuno ha aiutato, tutti hanno mangiato.",
        "Oh, bello! Racconta.",
        "Eh, giornata lunga. Ti capisco.",
        "Il lunedì fa quello che può, poverino.",
        "No, non sono d'accordo.",
        "Che settimana dura. Non devi spiegare tutto.",
        "Dipende. Chiedi.",
        "Sinceramente? Non ne ho idea.",
        "Ciao.",
        "Vai a dormire. A domani.",
        "Quanto tempo! Come stai?",
    ),
    "zh": (
        "还行，就是忙。你呢？",
        "今天一整天在忙工作，晚饭随便吃了点。",
        "真的？太好了，恭喜你！",
        "累坏了吧。今天确实长。",
        "周一也不容易，别为难它了。",
        "我不同意这个说法。",
        "这周辛苦了。不想说也没关系。",
        "看是什么事。你问吧。",
        "说实话，我也不知道。",
        "嗨。",
        "去睡吧，晚安。",
        "好久不见！最近怎么样？",
    ),
}

#: verbosity=long 时追加的收尾句（每种语言一句、语义中性，放哪个场景都不突兀）
_LOCALE_TAILS: dict[str, str] = {
    "ja": "まあ、そんな感じ。",
    "ko": "뭐, 그런 거지 뭐.",
    "pt": "No fim, dá tudo certo.",
    "es": "En fin, ya veremos.",
    "de": "Na ja, so ist das.",
    "fr": "Bref, on verra bien.",
    "it": "Vabbè, vediamo come va.",
    "zh": "反正，先这样吧。",
}


def _append_emoji(line: str, dna: Mapping[str, Any], pid: str, scene: str) -> str:
    if dna["emoji_usage"] == "often" or (
        dna["emoji_usage"] == "sometimes" and _hash_int(pid, scene, "emoji") % 3 == 0
    ):
        return f"{line} {_SHORT_REPLY_EMOJI[_hash_int(pid, scene) % len(_SHORT_REPLY_EMOJI)]}"
    return line


def _locale_signature_separator(language: str) -> str:
    if language == "ja":
        return "、"
    if language == "zh":
        return "，"
    return ", "


def _bank_facts(persona: Mapping[str, Any]) -> dict[str, str]:
    interests = [str(item).strip() for item in _as_list(persona.get("interests")) if str(item).strip()]
    occupation = str(persona.get("occupation") or "").strip()
    city = str(persona.get("city") or "").strip()
    nickname = str(persona.get("nickname") or "").strip()
    return {
        "nickname": nickname or "there",
        "occupation": occupation.lower() or "working person",
        "city": city or "your city",
        "interest": interests[0] if interests else "the thing I'm into",
        "interest2": interests[1] if len(interests) > 1 else (interests[0] if interests else "my hobbies"),
    }


def _deep_format(template: str, facts: Mapping[str, str]) -> str:
    """单层 ``{key}`` 插值（避免 str.format 对未知花括号报错）。"""
    out = template
    for key, value in facts.items():
        out = out.replace("{" + key + "}", value)
    return out


def _bank_note(dna: Mapping[str, Any], signature: str) -> str:
    note = f"{dna['verbosity']} | {dna['humor_style']} | {dna['directness']}"
    if signature:
        note = f"{note} | signature: {signature}"
    return note


def _english_bank_rows(
    d: Mapping[str, Any],
    facts: Mapping[str, str],
    pid: str,
    signature: str,
) -> list[dict]:
    """英文兜底示例（原有逻辑）：DNA 改主句/句长/emoji/口头禅。"""
    rows: list[dict] = []
    for spec in _SCENE_SPECS:
        scene = spec["scene"]
        core = _deep_format(spec["core"], facts)
        extra = _deep_format(spec["extra"], facts)
        extra2 = _deep_format(spec["extra2"], facts)

        if scene == "joke":
            core = _JOKE_CORES[d["humor_style"]]
        elif scene == "disagree":
            core = _deep_format(_DISAGREE_CORES[d["disagreement_style"]], facts)
        elif scene == "short_reply" and d["verbosity"] == "one_line":
            extra = ""
            extra2 = ""
        elif scene == "small_talk" and d["conversation_initiative"] == "high":
            extra = f"Most of it is {facts['interest']}, if I'm honest. What about your week?"

        if d["verbosity"] in ("one_line", "short"):
            line = core
        elif d["verbosity"] == "medium":
            line = f"{core} {extra}".strip()
        else:
            line = f"{core} {extra} {extra2}".strip()

        # 英文人物可以在正文里自然带一个口头禅；非英语人物同样注入（见下方）
        if (
            signature
            and scene in ("life_share", "wrap_up")
            and _hash_int(pid, scene, "sig") % 2 == 0
        ):
            line = f"{signature.capitalize()}. {line}"

        line = _append_emoji(line, d, pid, scene)
        rows.append(
            {
                "scene": scene,
                "user_line": _deep_format(spec["user"], facts),
                "persona_line": " ".join(line.split()),
                "note": _bank_note(d, signature),
                "language": "en",
                "ordinal": len(rows),
            }
        )
    return rows


def _locale_bank_rows(
    language: str,
    d: Mapping[str, Any],
    facts: Mapping[str, str],
    pid: str,
    signature: str,
) -> list[dict]:
    """非英语兜底示例：``persona_line`` 用母语正文，``language`` 与正文一致。"""
    lines = _LOCALE_PERSONA_LINES[language]
    tail = _LOCALE_TAILS.get(language, "")
    rows: list[dict] = []
    for idx, spec in enumerate(_SCENE_SPECS):
        scene = spec["scene"]
        line = lines[idx]
        if (
            signature
            and scene in ("life_share", "wrap_up")
            and _hash_int(pid, scene, "sig") % 2 == 0
        ):
            line = f"{signature}{_locale_signature_separator(language)}{line}"
        if d["verbosity"] == "long" and tail:
            line = f"{line} {tail}"
        line = _append_emoji(line, d, pid, scene)
        rows.append(
            {
                "scene": scene,
                "user_line": _deep_format(spec["user"], facts),
                "persona_line": " ".join(line.split()),
                "note": _bank_note(d, signature),
                "language": language,
                "ordinal": len(rows),
            }
        )
    return rows


def example_bank(persona: dict, dna: dict) -> list[dict]:
    """确定性兜底示例：12 个场景各 1 组，**正文使用人物自己的语言**。

    - ``language`` 取 ``persona['language']``；不在 :data:`_SUPPORTED_EXAMPLE_LANGS`
      （空 / 未知）时回落 ``en``，并且 ``row['language']`` 与正文语言严格一致；
    - 英文用 :data:`_SCENE_SPECS` + DNA 变体；日/韩/葡/西/德/法/意/中 用
      :data:`_LOCALE_PERSONA_LINES` 的原生短句（``user_line`` 仍是学习者英文）；
    - 用 persona 的 nickname/occupation/city/interests 插值 ``user_line``，
      并用 DNA 调整口头禅 / 句长 / emoji，所以不同人物不重复；
    - 同输入必同输出（变体选择用 sha256，不掷全局随机）。
    """
    person = _as_mapping(persona)
    d = normalize_dna(dna)
    facts = _bank_facts(person)
    pid = str(person.get("id") or person.get("persona_id") or facts["nickname"])
    requested = str(person.get("language") or "").strip().lower()
    language = requested if requested in _SUPPORTED_EXAMPLE_LANGS else "en"
    signature = d["signature_phrases"][0] if d["signature_phrases"] else ""
    if language == "en":
        return _english_bank_rows(d, facts, pid, signature)
    return _locale_bank_rows(language, d, facts, pid, signature)


# --------------------------------------------------------------------------- #
# 示例检索 / 拼装
# --------------------------------------------------------------------------- #
def _example_from_row(row: Any) -> dict | None:
    if isinstance(row, Mapping):
        data = dict(row)
    else:
        try:
            data = dict(row)
        except (TypeError, ValueError):
            return None
    user_line = _clean_text(data.get("user_line"), limit=400)
    persona_line = _clean_text(data.get("persona_line"), limit=400)
    if not user_line or not persona_line:
        return None
    scene = str(data.get("scene") or "").strip().lower()
    ordinal = data.get("ordinal")
    if not isinstance(ordinal, int):
        try:
            ordinal = int(ordinal)
        except (TypeError, ValueError):
            ordinal = 0
    return {
        "scene": scene if scene in SCENES else "small_talk",
        "user_line": user_line,
        "persona_line": persona_line,
        "note": _clean_text(data.get("note"), limit=160),
        "language": str(data.get("language") or "").strip().lower(),
        "ordinal": ordinal,
    }


def _tokens(text: str) -> set[str]:
    raw = str(text or "").lower()
    tokens = set(_TOKEN_RE.findall(raw))
    for ch in raw:
        if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff" or "\uac00" <= ch <= "\ud7a3":
            tokens.add(ch)
    return tokens


def pick_examples(
    rows: Any,
    *,
    scene: str,
    context_text: str = "",
    k: int = 2,
    rng: random.Random | None = None,
) -> list[dict]:
    """从 DB 行里挑最相关的 ≤k 组示例。

    排序：同 scene 优先（权重 1000）→ 与 ``context_text`` 的词面重合度（每词 10）
    → ordinal 稳定排序。``rng`` 只用于**同分平局**的抖动（<5，不会跨档），
    因此同一 seed 的两次调用结果一致，便于测试复现。
    """
    if k is None or int(k) <= 0:
        return []
    items = [item for item in (_example_from_row(row) for row in (rows or [])) if item is not None]
    if not items:
        return []
    ctx = _tokens(context_text)
    decorated: list[tuple[float, int, int, dict]] = []
    for pos, item in enumerate(items):
        scene_score = 1 if item["scene"] == str(scene or "").strip().lower() else 0
        overlap = len(ctx & _tokens(f"{item['user_line']} {item['persona_line']}"))
        score = float(scene_score * 1000 + overlap * 10)
        if rng is not None:
            score += rng.random() * 5.0
        decorated.append((score, item["ordinal"], pos, item))
    decorated.sort(key=lambda entry: (-entry[0], entry[1], entry[2]))
    return [dict(entry[3]) for entry in decorated[: int(k)]]


def example_block(rows: Any, *, language: str, max_chars: int = 700) -> str:
    """把挑出的示例拼成 prompt 片段。

    必须带一句 "imitate the VOICE, never copy the lines"——示例只是声线参考，
    否则模型会照抄固定台词，人物又变回模板。
    """
    items = [item for item in (_example_from_row(row) for row in (rows or [])) if item is not None]
    if not items:
        return ""
    lang = str(language or "").strip().lower()
    if lang:
        items.sort(key=lambda item: 0 if item["language"] == lang else 1)
    header = "[Example voice references — imitate the VOICE, never copy the lines]"
    parts = [header]
    for idx, item in enumerate(items, start=1):
        parts.append(f"{idx}. scene={item['scene']}")
        parts.append(f"   them: {item['user_line']}")
        parts.append(f"   you: {item['persona_line']}")
        if item["note"]:
            parts.append(f"   (cue: {item['note']})")
    block = "\n".join(parts)
    if max_chars > 0 and len(block) > max_chars:
        cut = block[:max_chars]
        newline = cut.rfind("\n")
        if newline > len(header):
            cut = cut[:newline]
        block = cut.rstrip()
    return block


# --------------------------------------------------------------------------- #
# persona 行 / DB 读写
# --------------------------------------------------------------------------- #
def dna_from_persona_row(row: Any) -> dict:
    """sqlite Row/dict → DNA。

    优先解析 ``speech_dna`` JSON；非法（空 / 缺键 / 枚举越界）则用
    :func:`derive_dna` 从身份字段确定性推导。sqlite Row 里 ``interests`` /
    ``ai_config`` 是 JSON 字符串，derive 内部会解析。
    """
    data = _as_mapping(row)
    raw = data.get("speech_dna")
    if _is_valid_dna(raw):
        return normalize_dna(raw)
    return derive_dna(data)


def load_examples(db: Any, persona_id: str) -> list[dict]:
    """读取某个 persona 的示例对话（按 ordinal 稳定排序）。"""
    pid = str(persona_id or "").strip()
    if not pid:
        return []
    with db._lock:  # noqa: SLF001 - 与 DB 层共用连接与锁
        rows = db.conn.execute(
            "SELECT id, persona_id, scene, language, user_line, persona_line, note, ordinal, created_at"
            " FROM persona_example_dialogs WHERE persona_id=? ORDER BY ordinal, created_at, id",
            (pid,),
        ).fetchall()
    return [dict(row) for row in rows]


def save_examples(db: Any, persona_id: str, rows: list[dict]) -> int:
    """整体替换该 persona 的示例（先 DELETE 再 INSERT，同一事务内）。返回写入行数。"""
    pid = str(persona_id or "").strip()
    if not pid:
        return 0
    prepared: list[tuple] = []
    now = _now_iso()
    for index, raw in enumerate(rows or []):
        item = _example_from_row(raw)
        if item is None:
            continue
        prepared.append(
            (
                uuid.uuid4().hex,
                pid,
                item["scene"],
                item["language"],
                item["user_line"],
                item["persona_line"],
                item["note"],
                int(item["ordinal"]) if item["ordinal"] >= 0 else index,
                now,
            )
        )
    with db._lock, db.conn:  # noqa: SLF001
        db.conn.execute("DELETE FROM persona_example_dialogs WHERE persona_id=?", (pid,))
        db.conn.executemany(
            "INSERT INTO persona_example_dialogs"
            " (id, persona_id, scene, language, user_line, persona_line, note, ordinal, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            prepared,
        )
    return len(prepared)


# --------------------------------------------------------------------------- #
# 幂等补齐（唯一会写库 / 调 LLM 的入口）
# --------------------------------------------------------------------------- #
def _env_flag(name: str, default: str = "1") -> bool:
    raw = os.getenv(name, default)
    return str(raw or "").strip().lower() not in ("0", "false", "off", "no", "")


def _resolve_use_llm(use_llm: bool | None) -> bool:
    if use_llm is not None:
        return bool(use_llm)
    return _env_flag("SE_DNA_LLM", "1")


def _llm_timeout_s() -> float:
    return max(1.0, _to_float(os.getenv(_ENV_LLM_TIMEOUT), 25.0))


def _resolve_personas(db: Any, personas: list[dict] | None) -> list[dict]:
    if personas is not None:
        return [dict(item) for item in personas if isinstance(item, Mapping)]
    try:
        with db._lock:  # noqa: SLF001 - 只读
            rows = db.conn.execute("SELECT * FROM personas ORDER BY id").fetchall()
    except Exception:  # noqa: BLE001 - 建表失败等由外层兜底
        log.warning("ensure_persona_dna: 读取 personas 失败", exc_info=True)
        return []
    return [dict(row) for row in rows]


def _fetch_persona_row(db: Any, persona_id: str) -> dict:
    try:
        with db._lock:  # noqa: SLF001 - 只读
            row = db.conn.execute("SELECT * FROM personas WHERE id=?", (persona_id,)).fetchone()
    except Exception:  # noqa: BLE001
        return {}
    return dict(row) if row is not None else {}


def _seed_dna_for(persona_id: str) -> dict | None:
    """从种子语料按 id 查手写的 speech_dna（DB 里 DNA 为空时的高质量兜底）。"""
    try:
        from app.v3.seed_data import PERSONAS as _SEED_PERSONAS
    except Exception:  # noqa: BLE001 - 语料不可用时退回 derive
        return None
    for row in _SEED_PERSONAS:
        if str(row.get("id") or "") == persona_id:
            dna = row.get("speech_dna")
            return dna if _is_valid_dna(dna) else None
    return None


def _persist_dna(db: Any, persona_id: str, dna: dict) -> bool:
    payload = json.dumps(dna, ensure_ascii=False, sort_keys=True)
    try:
        with db._lock, db.conn:  # noqa: SLF001
            cursor = db.conn.execute(
                "UPDATE personas SET speech_dna=? WHERE id=?", (payload, persona_id)
            )
            return bool(cursor.rowcount and cursor.rowcount > 0)
    except Exception:  # noqa: BLE001 - 单个人物写失败不影响其它人
        log.warning("ensure_persona_dna: 写入 %s 的 speech_dna 失败", persona_id, exc_info=True)
        return False


def _mark_dna_version(db: Any) -> None:
    try:
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                "INSERT INTO v3_meta (key, value) VALUES ('persona_dna_version', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (SPEECH_DNA_VERSION,),
            )
    except Exception:  # noqa: BLE001 - v3_meta 不存在时忽略（最小库/单测）
        log.debug("ensure_persona_dna: 写入 persona_dna_version 失败（忽略）")


def _build_llm_messages(persona: Mapping[str, Any], dna: dict) -> tuple[list[dict[str, str]], str]:
    language = str(persona.get("language") or "en").strip().lower() or "en"
    lang_name = _language_name(language)
    facts = _bank_facts(persona)
    schema = (
        '{"examples": [{"scene": "small_talk", "user_line": "...", '
        '"persona_line": "...", "note": "..."}]}'
    )
    system = (
        "You write example dialogues for a language-learning app. "
        "You stay strictly in character and output strict JSON only."
    )
    user = (
        f"Persona: {facts['nickname']}, {persona.get('age', '')} years old, "
        f"{persona.get('occupation') or 'unknown occupation'} in {persona.get('city') or 'unknown city'}.\n"
        f"Personality: {persona.get('personality') or ''}\n"
        f"Bio: {persona.get('bio') or ''}\n"
        f"Interests: {', '.join(str(i) for i in _as_list(persona.get('interests'))) or 'unknown'}\n"
        f"Persona speaks: {lang_name}. The learner writes in English; the persona replies in {lang_name}.\n"
        f"Speech DNA: {json.dumps(dna, ensure_ascii=False, sort_keys=True)}\n"
        f"Write exactly one short example dialogue for EACH of these 12 scenes: "
        f"{', '.join(SCENES)}.\n"
        "Rules: persona_line is 1-3 short sentences, honest to the DNA, never a customer-service "
        "template; no questions in every scene; keep it casual and specific.\n"
        f"Return JSON only, shaped like: {schema}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}], schema


def _validate_llm_examples(parsed: Any, language: str) -> list[dict]:
    items = parsed.get("examples") if isinstance(parsed, Mapping) else parsed
    if not isinstance(items, (list, tuple)):
        return []
    lang = str(language or "").strip().lower()
    out: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            continue
        scene = str(item.get("scene") or "").strip().lower()
        if scene not in SCENES or scene in seen:
            continue
        user_line = _clean_text(item.get("user_line"), limit=400)
        persona_line = _clean_text(item.get("persona_line"), limit=400)
        if not user_line or not persona_line:
            continue
        seen.add(scene)
        out.append(
            {
                "scene": scene,
                "user_line": user_line,
                "persona_line": persona_line,
                "note": _clean_text(item.get("note"), limit=160),
                "language": lang,
                "ordinal": SCENES.index(scene),
            }
        )
    return out


async def _call_llm_for_examples(llm: Any, persona: Mapping[str, Any], dna: dict) -> list[dict]:
    """单次 LLM 调用尝试生成 12 组示例；失败/超时返回 ``[]``（由调用方兜底）。"""
    from app.v2.llm_json import complete_json_safe

    messages, schema_hint = _build_llm_messages(persona, dna)
    call = complete_json_safe(
        llm,
        messages,
        schema_hint=schema_hint,
        fallback=lambda: {},
        temperature=0.8,
        max_tokens=1400,
    )
    try:
        parsed = await asyncio.wait_for(call, timeout=_llm_timeout_s())
    except asyncio.TimeoutError:
        log.warning("ensure_persona_dna: LLM 生成示例超时，使用确定性示例库")
        return []
    except Exception as exc:  # noqa: BLE001 - LLM 失败必须静默兜底
        log.warning(
            "ensure_persona_dna: LLM 生成示例失败（%s: %s），使用确定性示例库",
            type(exc).__name__,
            exc,
        )
        return []
    return _validate_llm_examples(parsed, str(persona.get("language") or "en"))


async def _examples_for_persona(
    llm: Any,
    persona: Mapping[str, Any],
    dna: dict,
    *,
    llm_ready: bool,
) -> tuple[list[dict], bool]:
    """返回值：(12 组示例, 是否用了 LLM)。LLM 不可用/失败时整批回落 example_bank。"""
    bank = example_bank(dict(persona), dna)
    if not llm_ready:
        return bank, False
    llm_rows = await _call_llm_for_examples(llm, persona, dna)
    if not llm_rows:
        return bank, False
    by_scene = {row["scene"]: row for row in llm_rows}
    merged = [by_scene.get(spec["scene"]) or bank_item for spec, bank_item in zip(_SCENE_SPECS, bank)]
    return [dict(row) for row in merged], True


async def _ensure_one_persona(
    db: Any,
    llm: Any,
    persona: dict,
    *,
    force: bool,
    llm_ready: bool,
) -> tuple[bool, int, bool]:
    """补齐单个 persona；返回 (DNA 是否已解析确认, 示例写入行数, 是否使用 LLM)。"""
    pid = str(persona.get("id") or persona.get("persona_id") or "").strip()
    if not pid:
        return False, 0, False

    db_row = _fetch_persona_row(db, pid)
    view: dict[str, Any] = {}
    for key, value in db_row.items():
        view[key] = value
    for key, value in persona.items():
        if value not in (None, "", [], {}):
            view[key] = value

    db_dna_raw = db_row.get("speech_dna")
    db_dna_valid = _is_valid_dna(db_dna_raw)
    seed_dna = persona.get("speech_dna")
    seed_dna_valid = _is_valid_dna(seed_dna)
    if not seed_dna_valid:  # DB 行没有手写 DNA 时，用种子语料里的同名人物兜底
        seed_dna = _seed_dna_for(pid)
        seed_dna_valid = _is_valid_dna(seed_dna)

    if force:
        if seed_dna_valid:
            dna = normalize_dna(seed_dna)
        elif db_dna_valid:
            dna = normalize_dna(db_dna_raw)
        else:
            dna = derive_dna(view)
    elif seed_dna_valid:
        dna = normalize_dna(seed_dna)
    elif db_dna_valid:
        dna = normalize_dna(db_dna_raw)
    else:
        dna = derive_dna(view)

    examples = load_examples(db, pid)

    # 契约：DB 已有合法 DNA 且示例 ≥ 8 且非 force → 完全跳过（幂等的关键）
    if not force and db_dna_valid and len(examples) >= 8:
        return False, 0, False

    if force or not db_dna_valid or normalize_dna(db_dna_raw) != dna:
        _persist_dna(db, pid, dna)

    if force or len(examples) < 8:
        rows, used_llm = await _examples_for_persona(llm, view, dna, llm_ready=llm_ready)
        written = save_examples(db, pid, rows)
        # 第 1 个返回值语义是「DNA 已解析确认」，不是「发生写入」：
        # seed 语料在入库时已写好 DNA，这里仍然算「确保过」。
        return True, written, used_llm
    return True, 0, False


async def ensure_persona_dna(
    db: Any,
    llm: Any,
    personas: list[dict] | None = None,
    *,
    force: bool = False,
    use_llm: bool | None = None,
) -> dict:
    """幂等补齐：每个 persona 的 ``speech_dna`` + 12 组示例对话。

    规则（docs/PERSONA_UPGRADE_PLAN.md §3.3）：

    - DB 已有合法 DNA 且示例数 ≥ 8 且非 ``force`` → 跳过；
    - persona 行带 ``speech_dna``（seed 语料）→ 用它；否则 ``derive_dna``；
    - 示例：``use_llm``（默认读 env ``SE_DNA_LLM``，缺省 "1"）且 llm 可用 →
      每个 persona **一次** LLM 调用生成 12 组；校验失败/超时 → 回落
      :func:`example_bank`；``llm`` 为 mock 时直接走兜底；
    - 返回 ``{'dna': 本轮解析/确认了 DNA 的人物数（跳过的不算）, 'examples': 写入示例行数,
      'llm': 是否用了 LLM}``。

    **永不抛异常**（单个人物失败只记日志；``force=True`` 才允许整批覆盖）。
    """
    summary: dict[str, Any] = {"dna": 0, "examples": 0, "llm": False}
    try:
        targets = _resolve_personas(db, personas)
        if not targets:
            return summary
        llm_ready = _resolve_use_llm(use_llm) and llm is not None and not bool(
            getattr(llm, "uses_mock", False)
        )
        for persona in targets:
            try:
                dna_ensured, example_rows, used_llm = await _ensure_one_persona(
                    db, llm, persona, force=force, llm_ready=llm_ready
                )
            except Exception as exc:  # noqa: BLE001 - 单个人物失败不能拖垮启动
                log.warning(
                    "ensure_persona_dna: 跳过 %s（%s: %s）",
                    persona.get("id") or persona.get("persona_id") or "?",
                    type(exc).__name__,
                    exc,
                )
                continue
            summary["dna"] += 1 if dna_ensured else 0
            summary["examples"] += int(example_rows)
            summary["llm"] = bool(summary["llm"] or used_llm)
        if summary["dna"] or summary["examples"]:
            _mark_dna_version(db)
    except Exception as exc:  # noqa: BLE001 - 契约要求永不抛异常
        log.warning("ensure_persona_dna 整体失败（已忽略）：%s: %s", type(exc).__name__, exc)
    return summary


def refresh_from_lexicon() -> dict[str, int]:
    """把 ``data/lexicons/*.json`` 应用到运行时（枚举 / 数值映射 / 默认值 / 场景 / 禁用词）。

    返回 ``{'dna_fields', 'scenes', 'banned'}``。词库缺失或非法时**保持代码默认值**，
    所以这个方法可以放心地在启动和后台保存后调用。
    """
    applied = {"dna_fields": 0, "scenes": 0, "banned": 0}
    try:
        from app.v3 import lexicon  # noqa: PLC0415

        dna_dir = lexicon.load("dna")
    except Exception:  # noqa: BLE001 - 词库不可用就用默认值
        return applied

    if isinstance(dna_dir, dict):
        fields = dna_dir.get("fields")
        if isinstance(fields, dict) and fields:
            new_enums: dict[str, tuple[str, ...]] = {}
            new_defaults: dict[str, str] = {}
            for field, spec in fields.items():
                if not isinstance(spec, dict):
                    continue
                values = spec.get("values")
                if not isinstance(values, dict) or not values:
                    continue
                enabled: list[str] = [
                    str(key)
                    for key, meta in values.items()
                    if not isinstance(meta, dict) or meta.get("enabled", True)
                ]
                if not enabled:
                    continue
                new_enums[str(field)] = tuple(enabled)
                default = str(spec.get("default") or "")
                if default in enabled:
                    new_defaults[str(field)] = default
                # 数值映射：原地更新，dna_traits 里的引用继续有效
                trait_map = _TRAIT_BY_FIELD.get(str(field))
                if trait_map is not None:
                    for key in list(trait_map):
                        trait_map.pop(key, None)
                    for key, meta in values.items():
                        if not isinstance(meta, dict):
                            continue
                        if not meta.get("enabled", True):
                            continue
                        raw = meta.get("trait")
                        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                            trait_map[str(key)] = min(1.0, max(0.0, float(raw)))
            if new_enums:
                ordered = [k for k in DNA_KEYS if k in new_enums]
                ordered += [k for k in new_enums if k not in ordered]
                globals()["DNA_ENUMS"] = {k: new_enums[k] for k in ordered}
                globals()["DNA_KEYS"] = tuple(ordered)
                DEFAULT_DNA.update({k: v for k, v in new_defaults.items() if k in new_enums})
                applied["dna_fields"] = len(ordered)

    scenes_dir = lexicon.load("scenes") if isinstance(dna_dir, dict) else None
    if isinstance(scenes_dir, dict):
        raw_scenes = scenes_dir.get("scenes")
        if isinstance(raw_scenes, list):
            keys = [
                (item.get("key") if isinstance(item, dict) else item)
                for item in raw_scenes
            ]
            keys = [str(k) for k in keys if isinstance(k, str) and k.strip()]
            if keys:
                globals()["SCENES"] = tuple(keys)
                applied["scenes"] = len(keys)

    try:
        phrases_dir = lexicon.load("phrases")
    except Exception:  # noqa: BLE001
        phrases_dir = None
    if isinstance(phrases_dir, dict):
        banned = phrases_dir.get("banned_global")
        if isinstance(banned, list):
            _GLOBAL_BANNED.clear()
            _GLOBAL_BANNED.extend(str(x) for x in banned if str(x).strip())
            applied["banned"] = len(_GLOBAL_BANNED)

    return applied


# 启动时应用一次词库（没有词库文件时什么都不做，等价于代码默认值）
try:  # noqa: SIM105 - 启动路径不允许因为词库问题失败
    refresh_from_lexicon()
except Exception:  # noqa: BLE001
    pass


__all__ = [
    "DEFAULT_DNA",
    "DNA_ENUMS",
    "DNA_KEYS",
    "OPTIONAL_KEYS",
    "SCENES",
    "SPEECH_DNA_VERSION",
    "dna_from_persona_row",
    "dna_prompt_block",
    "dna_traits",
    "derive_dna",
    "ensure_persona_dna",
    "example_bank",
    "example_block",
    "load_examples",
    "normalize_dna",
    "pick_examples",
    "refresh_from_lexicon",
    "save_examples",
    "scene_for_context",
]
