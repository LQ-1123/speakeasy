"""事件驱动的社交内容：生活事件 → Persona 多视角动态 → Persona 评论 → 聊天事实块。

产品动机
--------
过去的「人物动态」是独立随机生成的文案：信息流里 Emily 发了句爵士乐，聊天里 Emily
却完全不知道自己昨天去过酒吧。人物没有「生活」，只有一张嘴。

本模块把动态变成**生活事件的公开表达**：一条 ``persona_events`` 记录（例如
「Emily 和 Sophie 周五晚去了爵士酒吧」，status=completed，participants=[Emily, Sophie]）
可以派生出**挂在同一个 event_id 上的多条动态**——每个参与者用自己的 Speech DNA 声线，
写自己那一侧看到的版本。它们是同一件事的不同视角，而不是两件互相矛盾的事。

设计要点
--------
1. **事实约束**：写动态时 prompt 里只有事件事实（title / description / location /
   participants / 时间）。显式禁止新增地点、人物、日期、数字，禁止提「我是 AI」。
   生成结果还要过一遍 :func:`post_content_violations` 校验，违规就不写库——宁可没有，
   也不要一条编造事实的动态。
2. **不编造**：LLM 不可用/失败 → 返回 ``None``，**不写库**。本模块**没有**模板兜底：
   模板做不到「多视角」，而且会凭空造出事件里没有的细节。
3. **幂等**：全部以数据库查询判定，不用内存标记。
   - 动态：``(source_event_id, persona_id)`` 已存在 → 直接返回已有行；
   - 评论：``(post_id, author_persona_id)`` 已存在 → 直接返回已有行；
   - 批处理：:func:`run_posts_cycle` 只挑「还没派生过动态」的事件（事件级闸门），
     第二次运行新增 0 条；可用 ``since_hours`` 只看最近 N 小时内结束的事件，
     避免首次启动把历史事件全部补发（省钱，也更符合产品直觉）。
4. **允许什么都不发生**：:func:`should_post` 先算概率再掷骰子；daily_routine / meal
   这类琐事默认概率很低；**每个人物有自己的发帖预算**（:func:`persona_post_budget`，2..8），
   话痨和冷淡的人频率本来就不该一样。
5. **纯读取**：:func:`persona_recent_facts` 只读不改库，供聊天注入「已发生」的
   真实事实（planned 事件必须标成 "not happened yet"，cancelled 绝不出现）。
6. **当天感（阶段二）**：批处理挑事件时按 :data:`VALUE_TIERS` **价值优先**再按时间正序
   ——当天信息流先出现 achievement / social 这类值得分享的事，而不是先刷一堆 meal；
   :func:`run_posts_cycle` 有**每日配额**（``SE_EVENT_POSTS_PER_DAY``，缺省 8），
   不会一天冒出几十条事件动态；:func:`comment_backfill` 给「已经发出去但还没人互动」的
   老动态补一条 AI 评论，让互动不只集中在「这一轮刚发的」动态上（可 ``SE_COMMENT_BACKFILL=0`` 关）。
7. **关系张力（阶段三）**：:func:`comment_probability` 读 ``persona_relationships.tension``
   （只读，life-events 负责写）并按 ``max(TENSION_PENALTY_FLOOR, 1 - TENSION_PENALTY_STEP*tension)``
   打折——刚吵过架的两个人不会立刻在对方动态下热情评论，这是关系真实感的一部分。

依赖说明
--------
本模块**不依赖** ``app.v3.life_events`` / ``app.v3.speech_dna``（并行开发的模块）：
- 事件查询、DNA 解析、时间工具都在本文件内自带安全实现，测试只需直接 INSERT
  ``persona_events`` 行即可；
- 若 ``app.v3.speech_dna`` 已可用，会优先用它的 ``dna_prompt_block`` 作为声线描述
  （单一事实来源），不可用时回落本地的紧凑描述。
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from app.logging_utils import get_logger
from app.v3.social_writing import (
    PUBLIC_POST_STYLE, duplicate_post, history_block, persona_profile, post_text, recent_posts,
)

log = get_logger(__name__)

#: prompt 结构版本（便于以后对比/回滚文案策略）
PROMPT_VERSION = "2-natural-public-voice"

#: 事件驱动的动态在 ``persona_posts.post_type`` 上的标记
POST_TYPE_EVENT = "life_event"
#: 信息流按 ``post_kind`` 分流（life_text / life_photo）。事件动态仍是纯文字，
#: 所以 ``post_kind`` 保持 life_text，新语义只写进 post_type——前端无需改动。
POST_KIND_FEED = "life_text"

#: 事件状态（与 life_events.EVENT_STATUSES 对齐，但本模块不 import 它）
STATUS_PLANNED = "planned"
STATUS_ONGOING = "ongoing"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"

#: 正文/评论长度上限（与 life_posts 的动态长度一致，便于信息流排版）
MAX_POST_CHARS = 400
MAX_COMMENT_CHARS = 160

#: 概率上限：再爱分享的人也不是 100%（允许「今天不想发」）
MAX_SHARE_PROBABILITY = 0.95

#: ---- 人物化发文节奏（阶段三） ----
#: 每个 Persona 24h 内最多发多少条：话痨与冷淡的人必须不一样。
#: budget = clamp(round(2 + 3.5*self_disclosure + 2.5*closeness), 2, 8)
POST_BUDGET_BASE = 2.0
POST_BUDGET_DISCLOSURE_WEIGHT = 3.5
POST_BUDGET_CLOSENESS_WEIGHT = 2.5
POST_BUDGET_MIN = 2
POST_BUDGET_MAX = 8
#: 用掉一半预算后开始收敛（体感上「今天已经发太多了」）
POST_BUDGET_SOFT_RATIO = 0.5
POST_BUDGET_SUPPRESS_FACTOR = 0.45

#: ---- 关系张力对互动意愿的惩罚（阶段三） ----
#: 刚吵过架的人不会马上在对方动态下热情评论：prob *= max(floor, 1 - step*tension)
#: tension 0 → 1.00, 1 → 0.82, 3 → 0.46, 5 → 0.15
TENSION_PENALTY_STEP = 0.18
TENSION_PENALTY_FLOOR = 0.15
TENSION_MAX = 5

#: 事件类型 → 分享权重。achievement / conflict 更值得说，daily_routine / meal 基本不说。
EVENT_TYPE_SHARE_WEIGHT: dict[str, float] = {
    "achievement": 1.30,
    "personal_milestone": 1.25,
    "unexpected_event": 1.15,
    "conflict": 1.10,
    "relationship": 1.05,
    "social": 1.00,
    "entertainment": 1.00,
    "travel": 0.90,
    "work": 0.62,
    "study": 0.60,
    "meal": 0.35,
    "daily_routine": 0.22,
}
#: 未知事件类型的兜底权重（不把它做成 0：未知不等于不值得说）
DEFAULT_EVENT_SHARE_WEIGHT = 0.70

#: 事件「价值分层」：当天信息流应该先出现值得分享的事件，而不是一堆 meal。
#: 只影响**批处理取事件的顺序**，不影响单条事件的分享概率（那是 EVENT_TYPE_SHARE_WEIGHT）。
#: 3 = 里程碑/冲突/关系（最有故事）; 2 = 社交/娱乐/旅行; 1 = 工作/学习/意外; 0 = 吃饭/日常
VALUE_TIERS: dict[str, int] = {
    "achievement": 3,
    "conflict": 3,
    "personal_milestone": 3,
    "relationship": 3,
    "social": 2,
    "entertainment": 2,
    "travel": 2,
    "work": 1,
    "study": 1,
    "unexpected_event": 1,
    "meal": 0,
    "daily_routine": 0,
}
#: 未知事件类型的层级：不认识的一律当最低（不抢当天的位置）
DEFAULT_VALUE_TIER = 0
#: SQL 里的等价 CASE 表达式（只由上面的常量拼出，无外部输入）
_VALUE_TIER_SQL = "CASE event_type " + " ".join(
    f"WHEN '{event_type}' THEN {tier}"
    for event_type, tier in sorted(VALUE_TIERS.items())
) + f" ELSE {DEFAULT_VALUE_TIER} END"

#: 每天（本地日）最多产出多少条事件动态；<=0 = 不限制
DEFAULT_EVENT_POSTS_PER_DAY = 8

#: 评论补位默认只看最近 6 小时内的动态（再老就不硬塞互动了）
COMMENT_BACKFILL_WINDOW_HOURS = 6.0

#: 事件可见性 → 分享权重。private 只是「没那么想公开」，不是禁止（人物可以挑着说）。
VISIBILITY_SHARE_FACTOR: dict[str, float] = {
    "public": 1.0,
    "friends": 0.90,
    "private": 0.62,
}

#: 本模块内部依赖的 10 个数值化特质（0..1），与 speech_dna.dna_traits 的契约一致；
#: 调用方既可以直接传 speech_dna.dna_traits() 的结果，也可以传 :func:`dna_traits_for`。
TRAIT_KEYS: tuple[str, ...] = (
    "initiative", "warmth", "humour", "directness", "verbosity",
    "disclosure", "emoji", "slang", "playfulness", "social_distance",
)

#: Speech DNA 枚举 → 0..1。缺字段/非法值一律走安全默认。
#: 数值刻意与 ``speech_dna`` 模块的 ``_TRAIT_*`` 表保持一致，这样「传
#: ``speech_dna.dna_traits()`` 的结果」和「传 :func:`dna_traits_for` 的结果」
#: 得到几乎相同的分享概率，集成时不会因为两套映射而行为跳变。
_TRAIT_FROM_DNA: dict[str, dict[str, float]] = {
    "conversation_initiative": {"low": 0.20, "medium": 0.50, "high": 0.82},
    "humor_style": {"none": 0.05, "dry": 0.50, "sarcastic": 0.68, "playful": 0.82,
                    "absurd": 0.92},
    "emotional_expression": {"reserved": 0.22, "warm": 0.60, "expressive": 0.82,
                             "volatile": 0.88},
    "directness": {"blunt": 0.95, "direct": 0.72, "diplomatic": 0.38, "indirect": 0.18},
    "verbosity": {"one_line": 0.12, "short": 0.32, "medium": 0.62, "long": 0.92},
    "self_disclosure": {"guarded": 0.15, "measured": 0.50, "open": 0.90},
    "emoji_usage": {"never": 0.0, "rare": 0.20, "sometimes": 0.55, "often": 0.90},
    "slang_usage": {"none": 0.0, "rare": 0.22, "moderate": 0.55, "heavy": 0.90},
    "sentence_rhythm": {"clipped": 0.20, "measured": 0.35, "flowing": 0.45,
                        "fragmented": 0.85},
}
#: ``social_distance`` 在本模块内部统一按**亲近度**使用（1.0 = 很亲近）：
#: 枚举值越高表示越亲近——与 ``speech_dna.dna_traits()['social_distance']`` 的
#: 数值方向一致（那边同样是 intimate 最高）。
_SOCIAL_CLOSENESS_FROM_DNA: dict[str, float] = {
    "distant": 0.15, "neutral": 0.45, "familiar": 0.72, "intimate": 0.95,
}

#: 声线描述里带上这些键（顺序即 prompt 里的顺序）
_STYLE_FIELDS: tuple[tuple[str, str], ...] = (
    ("sentence_rhythm", "rhythm"),
    ("directness", "directness"),
    ("humor_style", "humor"),
    ("emotional_expression", "emotion"),
    ("vocabulary_style", "vocabulary"),
    ("slang_usage", "slang"),
    ("emoji_usage", "emoji"),
    ("verbosity", "length"),
    ("self_disclosure", "self-disclosure"),
)

#: 常见城市/国家名。生成正文时出现在「事件事实之外」→ 判定为编造地点。
#: 故意保持精简：这是**防编造的兜底网**，不是完整的 NER。
_PLACE_WORDS: frozenset[str] = frozenset({
    "tokyo", "kyoto", "osaka", "seoul", "beijing", "shanghai", "taipei", "hong kong",
    "singapore", "bangkok", "hanoi", "manila", "jakarta", "mumbai", "delhi", "bangalore",
    "london", "manchester", "edinburgh", "dublin", "paris", "lyon", "berlin", "munich",
    "hamburg", "vienna", "prague", "amsterdam", "brussels", "copenhagen", "stockholm",
    "oslo", "helsinki", "reykjavik", "zurich", "geneva", "madrid", "barcelona", "lisbon",
    "porto", "rome", "milan", "venice", "florence", "athens", "istanbul", "warsaw",
    "budapest", "moscow", "new york", "nyc", "boston", "chicago", "seattle",
    "san francisco", "los angeles", "toronto", "vancouver", "mexico city", "sao paulo",
    "são paulo", "rio de janeiro", "buenos aires", "santiago", "lima", "bogota", "bogotá",
    "sydney", "melbourne", "auckland", "cairo", "cape town", "nairobi", "lagos", "dubai",
    "tel aviv", "japan", "korea", "china", "taiwan", "thailand", "vietnam", "india",
    "england", "scotland", "ireland", "britain", "germany", "france", "italy", "spain",
    "portugal", "netherlands", "belgium", "denmark", "sweden", "norway", "finland",
    "poland", "austria", "greece", "turkey", "russia", "brazil", "mexico", "argentina",
    "chile", "peru", "colombia", "canada", "australia", "new zealand", "egypt", "nigeria",
    "kenya", "south africa", "morocco", "indonesia", "philippines", "malaysia",
    "united states", "usa", "america",
})
_PLACE_RE = re.compile(
    r"\b(" + "|".join(re.escape(p) for p in sorted(_PLACE_WORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
#: 提到自己是 AI / 模型 → 直接判定违规（人物永远不承认自己是程序）
_AI_RE = re.compile(
    r"\bai\b|\bas an ai\b|language model|\bllm\b|chatgpt|人工智能|作为一个ai|作为ai模型",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")
#: 相关性打分用的停用词（只影响排序，不影响事实）
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "you", "your", "our", "his", "her",
    "have", "has", "had", "was", "were", "are", "been", "being", "about", "from",
    "into", "they", "them", "their", "there", "what", "when", "where", "which", "who",
    "did", "does", "doing", "not", "but", "all", "any", "can", "could", "would",
    "should", "very", "just", "some", "than", "then", "too", "out", "off", "over",
    "today", "yesterday", "tonight", "tomorrow", "morning", "evening", "night",
})


# --------------------------------------------------------------------------- #
# 基础工具：JSON / 时间 / 数值
# --------------------------------------------------------------------------- #
def _loads(raw: Any, default: Any) -> Any:
    """宽松解析 JSON 列（脏数据 → default，绝不抛异常）。"""
    if isinstance(raw, (dict, list)):
        return raw
    if not raw:
        return default
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return default
    return value if value is not None else default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _row_dict(row: Any) -> dict:
    """sqlite3.Row / dict → 普通 dict（Row 不能直接 dict() 转换）。"""
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    try:
        return {key: row[key] for key in row.keys()}
    except Exception:  # noqa: BLE001 - 非 Row 的奇怪输入
        return {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _now_iso() -> str:
    return _iso(_utcnow())


def _hours_ago_iso(hours: float) -> str:
    return _iso(_utcnow() - timedelta(hours=hours))


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _finite_float(value: Any, default: float) -> float:
    """更严格的数值解析：None/bool/非数字/NaN/inf 一律回落默认值。

    ``persona_post_budget`` / ``tension`` 这类「会直接影响行为」的输入必须抗脏数据：
    ``round(nan)`` 会抛 ValueError，NaN 参与比较还会得到反直觉结果。
    """
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _participants(event: dict) -> list[str]:
    """事件参与者（去重、保持顺序；主角保证在列表里）。"""
    raw = event.get("participants")
    if isinstance(raw, str):
        raw = _loads(raw, [])
    ids: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        text = str(item).strip()
        if text and text not in ids:
            ids.append(text)
    owner = str(event.get("persona_id") or "").strip()
    if owner and owner not in ids:
        ids.insert(0, owner)
    return ids


def _clean_text(value: Any, limit: int = 0) -> str:
    """LLM 文本清洗：去引号、压空白、超长按词边界截断。"""
    text = " ".join(str(value or "").split()).strip()
    text = text.strip().strip('"').strip("'").strip()
    if limit > 0 and len(text) > limit:
        cut = text[:limit]
        space = cut.rfind(" ")
        text = (cut[:space] if space > limit * 0.6 else cut).rstrip(" ,;:.-")
    return text


def _tokens(text: Any) -> set[str]:
    return {
        token for token in _TOKEN_RE.findall(str(text or "").lower())
        if token not in _STOPWORDS
    }


# --------------------------------------------------------------------------- #
# Speech DNA（本地安全解析；不 import 并行开发中的 speech_dna 模块）
# --------------------------------------------------------------------------- #
def dna_from_persona(persona: Any) -> dict:
    """任意 persona 行/dict → DNA dict（缺字段给空，由 trait 映射兜默认值）。"""
    data = _as_dict(persona)
    raw = data.get("speech_dna")
    if isinstance(raw, str):
        raw = _loads(raw, {})
    return raw if isinstance(raw, dict) else {}


def _trait_from_dna(dna: dict, key: str, default: float = 0.5) -> float:
    raw = dna.get(key)
    if not isinstance(raw, str) or not raw.strip():
        return default
    table = _TRAIT_FROM_DNA.get(key) or {}
    return _clamp(table.get(raw.strip().lower(), default))


def dna_traits_for(persona: Any) -> dict[str, float]:
    """persona → 10 个 0..1 特质（与 ``speech_dna.dna_traits`` 契约一致）。

    优先用 ``app.v3.speech_dna.dna_traits``（单一事实来源）；该模块不可用/未稳定时
    回落到本地的**等价镜像实现** :func:`_dna_traits_local`，两者数值一致，
    因此 ``share_probability`` 的判定不会因为模块在不在而跳变。
    """
    dna = dna_from_persona(persona)
    try:  # pragma: no cover - 取决于 speech_dna 是否可用
        from app.v3.speech_dna import dna_traits as _shared_traits  # noqa: PLC0415

        got = _shared_traits(dna)
        if isinstance(got, dict) and all(key in got for key in TRAIT_KEYS):
            return {key: _clamp(_as_float(got[key], 0.5)) for key in TRAIT_KEYS}
    except Exception:  # noqa: BLE001 - 模块缺失/未稳定都不能拖垮生成
        pass
    return _dna_traits_local(dna)


def _dna_traits_local(dna: dict) -> dict[str, float]:
    """``speech_dna.dna_traits`` 的本地镜像（同样的加权公式与取值表）。"""
    # 各默认值 = speech_dna.normalize_dna 对空 DNA 的默认枚举（measured/direct/dry/…），
    # 保证「有 speech_dna」与「没有 speech_dna」两种情况得到同样的数值。
    humour = _trait_from_dna(dna, "humor_style", 0.50)
    directness = _trait_from_dna(dna, "directness", 0.72)
    emotion = _trait_from_dna(dna, "emotional_expression", 0.60)
    closeness = _social_closeness(dna)
    emoji = _trait_from_dna(dna, "emoji_usage", 0.20)
    slang = _trait_from_dna(dna, "slang_usage", 0.22)
    disclosure = _trait_from_dna(dna, "self_disclosure", 0.50)
    rhythm = _trait_from_dna(dna, "sentence_rhythm", 0.35)
    raw_initiative = _trait_from_dna(dna, "conversation_initiative", 0.50)
    return {
        "initiative": _clamp(0.70 * raw_initiative + 0.15 * disclosure + 0.15 * emotion),
        "warmth": _clamp(
            0.45 * emotion + 0.25 * closeness + 0.15 * humour + 0.15 * (1.0 - directness)
        ),
        "humour": _clamp(humour),
        "directness": _clamp(directness),
        "verbosity": _trait_from_dna(dna, "verbosity", 0.32),
        "disclosure": _clamp(disclosure),
        "emoji": _clamp(emoji),
        "slang": _clamp(slang),
        "playfulness": _clamp(0.5 * humour + 0.3 * rhythm + 0.2 * emoji),
        "social_distance": _clamp(closeness),
    }


def _social_closeness(dna: dict) -> float:
    """DNA 里的 ``social_distance`` 枚举 → 亲近度（0..1，越高越亲近）。"""
    raw = dna.get("social_distance")
    if not isinstance(raw, str) or not raw.strip():
        return 0.45
    return _clamp(_SOCIAL_CLOSENESS_FROM_DNA.get(raw.strip().lower(), 0.45))


def _merge_traits(dna_traits: Any, persona: Any) -> dict[str, float]:
    """显式传入的 traits 优先，缺的键用 persona 推导值补齐。"""
    base = dna_traits_for(persona)
    supplied = _as_dict(dna_traits)
    for key, value in supplied.items():
        if key not in base:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        base[key] = _clamp(float(value))
    return base


def _style_block(persona: dict, dna: dict, *, language: str = "en") -> str:
    """Use public writing habits, without importing chat instructions or clipped prose."""
    parts = [
        f"{label}: {dna[key]}"
        for key, label in _STYLE_FIELDS
        if isinstance(dna.get(key), str) and str(dna.get(key)).strip()
    ]
    phrases = dna.get("signature_phrases")
    if isinstance(phrases, list):
        clean = [str(p).strip() for p in phrases if str(p).strip()][:4]
        if clean:
            parts.append("signature phrases (use sparingly): " + ", ".join(clean))
    if not parts:
        return ""
    return (
        "Writing style (tendencies, NOT rules — do not turn these into a checklist): "
        + "; ".join(parts)
        + ". Keep the voice natural; a single short sentence is fine."
    )


# --------------------------------------------------------------------------- #
# 分享概率 / 是否发布
# --------------------------------------------------------------------------- #
def _mood_factor(persona: dict) -> float:
    """情绪状态对「想不想分享」的轻微影响（无状态时恒等于 1）。

    幅度刻意很小（0.78..1.12）：情绪是滤镜，不是开关。
    """
    state = persona.get("emotion_state")
    if isinstance(state, str):
        state = _loads(state, {})
    if not isinstance(state, dict) or not state:
        return 1.0
    energy = _as_float(state.get("energy"), 0.5)
    stress = _as_float(state.get("stress"), 0.3)
    battery = _as_float(state.get("social_battery"), 0.5)
    factor = (
        0.90
        + 0.18 * _clamp(energy)
        - 0.12 * max(0.0, _clamp(stress) - 0.35)
        + 0.08 * (_clamp(battery) - 0.5)
    )
    return _clamp(factor, 0.78, 1.12)


def share_probability(persona: dict, dna_traits: dict, event: dict) -> float:
    """这个人物愿意把这件事公开说出来的概率（0..1）。

    纯函数、无随机：由说话风格（self-disclosure）、社交亲近度、事件类型、
    事件可见性、当前情绪共同决定。``dna_traits['social_distance']`` 在本模块按
    **亲近度** 解释（1.0 = 很亲近，数值越大越愿意分享），与
    ``speech_dna.dna_traits()`` 的数值方向一致，见 :func:`dna_traits_for`。
    """
    data = _as_dict(persona)
    ev = _as_dict(event)
    traits = _merge_traits(dna_traits, data)

    disclosure = traits["disclosure"]
    closeness = traits["social_distance"]
    warmth = traits["warmth"]

    base = 0.10 + 0.45 * disclosure + 0.20 * closeness + 0.05 * warmth
    event_type = str(ev.get("event_type") or "").strip().lower()
    type_weight = EVENT_TYPE_SHARE_WEIGHT.get(event_type, DEFAULT_EVENT_SHARE_WEIGHT)
    visibility = str(ev.get("visibility") or "").strip().lower()
    visibility_weight = VISIBILITY_SHARE_FACTOR.get(visibility, 1.0)

    prob = base * type_weight * visibility_weight * _mood_factor(data)
    return _clamp(prob, 0.0, MAX_SHARE_PROBABILITY)


def persona_post_budget(dna_traits: dict) -> int:
    """这个人物 24h 内最多发几条动态（2..8）。纯函数，脏/缺键安全默认。

    话痨和冷淡的人发帖频率本来就该不一样：
    ``budget = clamp(round(2 + 3.5*self_disclosure + 2.5*closeness), 2, 8)``。
    这里的 ``social_distance`` 是 ``dna_traits`` 里的**亲近度**（1.0 = 很亲近，
    与 ``speech_dna.dna_traits()`` 的数值方向一致）。

    缺键/脏输入按 0.5/0.5 处理 → budget = 5；全低 → 2，全高 → 8。
    """
    traits = _as_dict(dna_traits)
    disclosure = _clamp(_finite_float(traits.get("disclosure"), 0.5))
    closeness = _clamp(_finite_float(traits.get("social_distance"), 0.5))
    raw = (
        POST_BUDGET_BASE
        + POST_BUDGET_DISCLOSURE_WEIGHT * disclosure
        + POST_BUDGET_CLOSENESS_WEIGHT * closeness
    )
    budget = int(_clamp(float(round(raw)), POST_BUDGET_MIN, POST_BUDGET_MAX))
    return budget


def should_post(
    *,
    persona: dict,
    dna_traits: dict,
    event: dict,
    recent_post_count: int,
    rng: random.Random | None = None,
    relationship_tension: float | None = None,
) -> bool:
    """**允许不发布**：概率 + 人物化发文节奏。同 rng seed 必定同结果（可复现）。

    24h 内的上限由人物自己的预算决定（:func:`persona_post_budget`，2..8）：
    - 已发 ≥ budget → 直接 False（不掷骰子：这个人今天已经说得够多了）；
    - 已发 ≥ budget*0.5 → 概率 × ``POST_BUDGET_SUPPRESS_FACTOR``（开始收敛）；
    - 否则概率不打折。

    ``relationship_tension`` 是**可选占位参数**：按契约它**不参与**发文概率
    （发文是个人行为，不是关系行为）；关系张力只影响评论/互动
    （:func:`comment_probability`）。这里接受它是为了调用方不必分情况传参。

    系统级每日配额（``run_posts_cycle(daily_quota=...)``，默认 8）是全体总闸，
    与人物预算天然取小（budget 上限 8 就是默认日配额；配额调低时总闸先卡住）。
    """
    _ = relationship_tension  # 契约：不参与发文概率（保留参数占位）
    rng = rng or random.Random()
    prob = share_probability(persona, dna_traits, event)
    budget = persona_post_budget(_merge_traits(dna_traits, persona))
    count = max(0, int(_as_float(recent_post_count, 0)))
    if count >= budget:
        return False
    if count >= budget * POST_BUDGET_SOFT_RATIO:
        prob *= POST_BUDGET_SUPPRESS_FACTOR
    return rng.random() < prob


def _relationship_closeness(relationship: dict) -> float:
    """关系亲密度 1..5 → 0..1。"""
    raw = _as_float(relationship.get("closeness"), 2.0)
    return _clamp((raw - 1.0) / 4.0)


def _relationship_tension(relationship: dict) -> float:
    """关系张力 0..5（缺失/脏值 → 0，向后兼容）。

    ``tension`` 由 life-events 写入 ``persona_relationships.tension``，本模块**只读**。
    """
    raw = _finite_float(_as_dict(relationship).get("tension"), 0.0)
    return _clamp(raw, 0.0, float(TENSION_MAX))


def comment_probability(persona: dict, dna_traits: dict, relationship: dict | None) -> float:
    """另一个 Persona 会不会来评论：看亲近度 + 温暖度，再被关系张力打折。没有关系 → 0。

    张力惩罚（阶段三）：
    ``prob *= max(TENSION_PENALTY_FLOOR, 1.0 - TENSION_PENALTY_STEP * tension)``

    含义：刚吵过架的两个人不会立刻在对方动态下热情互动——
    tension 0 → ×1.00（与旧行为完全一致）、1 → ×0.82、3 → ×0.46、5 → ×0.15。
    """
    if not relationship:
        return 0.0
    traits = _merge_traits(dna_traits, _as_dict(persona))
    closeness = _relationship_closeness(relationship)
    base = _clamp(0.30 + 0.45 * traits["warmth"] + 0.25 * closeness, 0.15, 0.92)
    tension = _relationship_tension(relationship)
    penalty = max(TENSION_PENALTY_FLOOR, 1.0 - TENSION_PENALTY_STEP * tension)
    return _clamp(base * penalty, 0.0, 0.92)


# --------------------------------------------------------------------------- #
# 事实校验：生成结果 vs 事件事实
# --------------------------------------------------------------------------- #
def _allowed_fact_text(event: dict, persona: dict | None) -> str:
    """允许出现在正文里的「事实词面」集合（小写拼接，供违规检查用）。"""
    ev = _as_dict(event)
    data = _as_dict(persona)
    chunks = [
        str(ev.get("title") or ""),
        str(ev.get("description") or ""),
        str(ev.get("location") or ""),
        str(data.get("city") or ""),
        str(data.get("country") or ""),
        str(data.get("occupation") or ""),
    ]
    return " ".join(chunks).lower()


def post_content_violations(
    content: Any, event: dict, persona: dict | None = None
) -> list[str]:
    """检查动态正文是否守住了事件事实。返回违规代码列表（空 = 通过）。

    代码：``empty`` / ``ai_mention`` / ``invented_place:<词>`` /
    ``invented_date:<年>``。这是**兜底网**：prompt 已经禁止，这里再挡一次，
    宁可这条动态不出现，也不要信息流里冒出编造的地点/日期。
    """
    text = _clean_text(content)
    if not text:
        return ["empty"]
    problems: list[str] = []
    if _AI_RE.search(text):
        problems.append("ai_mention")

    allowed = _allowed_fact_text(event, persona)
    lowered = text.lower()
    for match in _PLACE_RE.finditer(lowered):
        place = match.group(1).lower()
        if place not in allowed:
            problems.append(f"invented_place:{place}")
            break
    for year in _YEAR_RE.findall(text):
        if year not in allowed:
            problems.append(f"invented_date:{year}")
            break
    return problems


# --------------------------------------------------------------------------- #
# prompt 组装
# --------------------------------------------------------------------------- #
def _language_of(persona: dict) -> str:
    return str(persona.get("language") or "en").strip().lower() or "en"


def _language_name(code: str) -> str:
    try:  # pragma: no cover - 只是查表
        from app.v3.seed_data import LANGUAGE_NAMES  # noqa: PLC0415

        return LANGUAGE_NAMES.get(code, "English")
    except Exception:  # noqa: BLE001
        return "English"


def _persona_block(persona: dict) -> str:
    return persona_profile(persona)


def _event_facts_block(event: dict, participant_names: dict[str, str] | None = None) -> str:
    """把事件压成「只允许引用这些」的事实清单。"""
    names = participant_names or {}
    ev = _as_dict(event)
    people = [names.get(pid, pid) for pid in _participants(ev)]

    start = _parse_iso(ev.get("start_time"))
    end = _parse_iso(ev.get("end_time"))
    if start and end:
        when = f"{start.strftime('%Y-%m-%d %H:%M')} - {end.strftime('%H:%M')} (UTC)"
    elif start:
        when = f"{start.strftime('%Y-%m-%d %H:%M')} (UTC)"
    else:
        when = "(not specified)"

    lines = [
        "- what happened: " + (str(ev.get("title") or "") or "(not specified)"),
        "- details: " + (str(ev.get("description") or "") or "(none given)"),
        "- when: " + when,
        "- where: " + (str(ev.get("location") or "") or "(not specified — do NOT invent a place)"),
        "- people there: " + (", ".join(people) if people else "(just you)"),
    ]
    return "EVENT FACTS (these are the ONLY true details; nothing else is known):\n" + "\n".join(lines)


def _post_messages(
    *,
    persona: dict,
    event: dict,
    participant_names: dict[str, str],
    recent: list[str] | None = None,
) -> list[dict]:
    """写动态的 prompt。事实只来自 event；声线来自 DNA。"""
    ev = _as_dict(event)
    code = _language_of(persona)
    lang_name = _language_name(code)
    dna = dna_from_persona(persona)
    style = _style_block(persona, dna, language=code)
    others = [
        participant_names.get(pid, pid)
        for pid in _participants(ev)
        if pid != str(persona.get("id") or "")
    ]

    system = [
        f"Write as {persona.get('nickname') or 'someone'}, the fictional character below,",
        "posting about their own recorded life. Stay in their everyday voice.",
        _persona_block(persona),
    ]
    if style:
        system.append(style)
    system.append(PUBLIC_POST_STYLE)
    system.append(
        "This is a public POST, not a chat reply: do not write it as an answer to a message."
    )
    if others:
        system.append(
            "You were there with " + ", ".join(others)
            + ". They may write their own post about the same thing, so write only YOUR angle."
        )
    system_text = "\n".join(part for part in system if part)

    rules = (
        "RULES (all mandatory):\n"
        "1. Use ONLY the event facts below. Do NOT invent places, people, dates, times, "
        "or numbers. If a detail is not listed, leave it out.\n"
        "2. Choose what YOU would actually share from this event. A detail, reaction, "
        "or plain observation is enough. Feelings and first-person pronouns are optional. "
        "Do not mechanically retell the event card or disclose private conflict details.\n"
        "3. Do not write someone else's perspective.\n"
        f"4. Write in {lang_name} ({code}), at most {MAX_POST_CHARS} characters. "
        "A few words can be a complete post; do not pad it.\n"
        "5. The event is completed, but its date may not be today. Use the supplied dates; "
        "do not invent 'just now', weather, food, dialogue, outcomes or photo ownership.\n"
        '6. Output JSON only: {"content": "..."}. Return {"content": ""} if there is '
        "nothing this person would share."
    )
    user_text = (
        _event_facts_block(ev, participant_names) + "\n\n"
        + history_block(recent or []) + "\n\n" + rules
    )
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]


def _comment_messages(
    *,
    commenter: dict,
    post: dict,
    event: dict,
    participant_names: dict[str, str],
    relationship: dict,
) -> list[dict]:
    """评论 prompt：只能就「看到的动态 + 共享事件事实」说话，不得新增细节。"""
    ev = _as_dict(event)
    code = _language_of(commenter)
    lang_name = _language_name(code)
    dna = dna_from_persona(commenter)
    style = _style_block(commenter, dna, language=code)
    relation = str(relationship.get("relation_type") or "friend")
    tension = _as_float(relationship.get("tension"), 0)

    system = [
        f"Write as {commenter.get('nickname') or 'someone'}, the fictional character below.",
        "Someone you know posted an update",
        f"({relation}, closeness {int(_as_float(relationship.get('closeness'), 2))}/5).",
        f"Current relationship tension: {tension:g}/5. High tension may mean a restrained "
        "comment or no comment; do not suddenly act affectionate after a conflict.",
        "Write ONE short natural comment, like a real comment under a friend's post.",
        "React in your own voice, without explaining the role or task. A fragment is fine.",
        "Do not automatically praise, give advice, repeat the post, or add a follow-up question.",
        "Match the relationship's distance; do not invent an inside joke or shared memory.",
        _persona_block(commenter),
    ]
    if style:
        system.append(style)
    system_text = "\n".join(part for part in system if part)

    rules = (
        "RULES (all mandatory):\n"
        "1. React to what you can see. Do NOT invent new places, people, dates or numbers; "
        "if it is not in the post or the facts below, leave it out.\n"
        f"2. Write in {lang_name} ({code}), at most {MAX_COMMENT_CHARS} characters. "
        "Use only as much as this reaction needs.\n"
        "3. Emoji and punctuation follow your habits. Do not manufacture enthusiasm.\n"
        '4. Output JSON only: {"comment": "..."}; use an empty comment if you have nothing to add.'
    )
    # Knowing the author does not grant access to their private event ledger.
    shared_facts = (
        _event_facts_block(ev, participant_names)
        if str(commenter.get("id") or "") in _participants(ev)
        else "You were NOT a participant. You know only what the public post says; "
        "do not imply you were there or know unpublished event details."
    )
    user_text = (
        'Their post: "' + str(post.get("content") or "")[:400] + '"\n\n'
        + shared_facts + "\n\n" + rules
    )
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]


# --------------------------------------------------------------------------- #
# LLM 调用（失败一律返回空串 → 上层返回 None，不写库）
# --------------------------------------------------------------------------- #
async def _llm_text(
    llm: Any,
    messages: list[dict],
    *,
    key: str,
    schema_hint: str,
    max_tokens: int = 260,
) -> str:
    if llm is None or getattr(llm, "uses_mock", False):
        # 没有真实模型 = 宁可不发，也不编造
        return ""
    try:
        from app.v2.llm_json import complete_json_safe  # noqa: PLC0415

        data = await complete_json_safe(
            llm,
            messages,
            schema_hint=schema_hint,
            fallback=lambda: None,
            temperature=0.9,
            max_tokens=max_tokens,
        )
    except Exception as exc:  # noqa: BLE001 - LLM 挂掉不能拖垮后台循环
        log.warning("事件动态 LLM 调用失败（%s: %s）", type(exc).__name__, exc)
        return ""
    if not isinstance(data, dict):
        return ""
    value = data.get(key)
    return value.strip() if isinstance(value, str) else ""


# --------------------------------------------------------------------------- #
# 数据库读写
# --------------------------------------------------------------------------- #
def _event_id(db: Any, event: dict) -> str:
    """事件 id：优先 event['id']，其次按 dedupe_key 反查（没有 id 就无法保证幂等）。"""
    direct = str(event.get("id") or event.get("event_id") or "").strip()
    if direct:
        return direct
    dedupe = str(event.get("dedupe_key") or "").strip()
    if not dedupe:
        return ""
    with db._lock:  # noqa: SLF001 - 与 DB 层共用连接与锁
        row = db.conn.execute(
            "SELECT id FROM persona_events WHERE dedupe_key=?", (dedupe,)
        ).fetchone()
    return str(_row_dict(row).get("id") or "")


def _persona_row(db: Any, persona_id: str) -> dict:
    if not persona_id:
        return {}
    with db._lock:  # noqa: SLF001
        row = db.conn.execute("SELECT * FROM personas WHERE id=?", (persona_id,)).fetchone()
    return _row_dict(row)


def _personas_map(db: Any, persona_ids: Iterable[str]) -> dict[str, dict]:
    ids = [str(pid) for pid in persona_ids if str(pid or "").strip()]
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            f"SELECT * FROM personas WHERE id IN ({marks})", tuple(ids)
        ).fetchall()
    return {str(_row_dict(row).get("id") or ""): _row_dict(row) for row in rows}


def _display_names(db: Any, persona_ids: Iterable[str]) -> dict[str, str]:
    """参与者 id → 昵称（查不到 id 就原样显示，绝不编名字）。"""
    names: dict[str, str] = {}
    for pid, row in _personas_map(db, persona_ids).items():
        nickname = str(row.get("nickname") or "").strip()
        if nickname:
            names[pid] = nickname
    return names


def _row_to_post(row: Any) -> dict:
    data = _row_dict(row)
    if not data:
        return {}
    meta = _loads(data.get("metadata"), {})
    return {
        "id": str(data.get("id") or ""),
        "persona_id": str(data.get("persona_id") or ""),
        "content": str(data.get("content") or ""),
        "language": str(data.get("language") or ""),
        "tags": _loads(data.get("tags"), []),
        "media": _loads(data.get("media"), []),
        "post_kind": str(data.get("post_kind") or ""),
        "post_type": str(data.get("post_type") or ""),
        "source_event_id": str(data.get("source_event_id") or ""),
        "published_at": str(data.get("published_at") or ""),
        "created_at": str(data.get("created_at") or ""),
        "metadata": meta if isinstance(meta, dict) else {},
        "source_ref": _loads(data.get("source_ref"), {}),
    }


def _row_to_comment(row: Any) -> dict:
    data = _row_dict(row)
    if not data:
        return {}
    return {
        "id": str(data.get("id") or ""),
        "post_id": str(data.get("post_id") or ""),
        "content": str(data.get("content") or ""),
        "author_persona_id": str(data.get("author_persona_id") or ""),
        "author_type": str(data.get("author_type") or "user"),
        "created_at": str(data.get("created_at") or ""),
    }


def _row_to_event(row: Any) -> dict:
    data = _row_dict(row)
    if not data:
        return {}
    meta = _loads(data.get("metadata"), {})
    return {
        "id": str(data.get("id") or ""),
        "persona_id": str(data.get("persona_id") or ""),
        "event_type": str(data.get("event_type") or ""),
        "title": str(data.get("title") or ""),
        "description": str(data.get("description") or ""),
        "start_time": str(data.get("start_time") or ""),
        "end_time": str(data.get("end_time") or ""),
        "participants": [str(p) for p in _loads(data.get("participants"), [])],
        "location": str(data.get("location") or ""),
        "related_personas": [str(p) for p in _loads(data.get("related_personas"), [])],
        "event_status": str(data.get("event_status") or ""),
        "visibility": str(data.get("visibility") or ""),
        "day": str(data.get("day") or ""),
        "dedupe_key": str(data.get("dedupe_key") or ""),
        "completed_at": str(data.get("completed_at") or ""),
        "metadata": meta if isinstance(meta, dict) else {},
    }


def posts_for_event(db: Any, event_id: str) -> list[dict]:
    """该事件派生出的全部动态（按发布时间升序）。纯读取。"""
    if not str(event_id or "").strip():
        return []
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT * FROM persona_posts WHERE source_event_id=?"
            " ORDER BY COALESCE(NULLIF(published_at, ''), created_at), id",
            (str(event_id),),
        ).fetchall()
    return [_row_to_post(row) for row in rows]


def _find_post(db: Any, event_id: str, persona_id: str) -> dict:
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT * FROM persona_posts WHERE source_event_id=? AND persona_id=?"
            " ORDER BY COALESCE(NULLIF(published_at, ''), created_at), id LIMIT 1",
            (event_id, persona_id),
        ).fetchone()
    return _row_to_post(row)


def _find_comment(db: Any, post_id: str, commenter_id: str) -> dict:
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT * FROM persona_post_comments WHERE post_id=? AND author_persona_id=?"
            " ORDER BY created_at, id LIMIT 1",
            (post_id, commenter_id),
        ).fetchone()
    return _row_to_comment(row)


def _recent_post_count(db: Any, persona_id: str, *, hours: float = 24.0) -> int:
    """该人物 24h 内已发动态数（含种子/其他来源，冷却看的是「整体发帖频率」）。"""
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT COUNT(*) AS n FROM persona_posts WHERE persona_id=? AND created_at>=?",
            (persona_id, _hours_ago_iso(hours)),
        ).fetchone()
    return int(_as_float(_row_dict(row).get("n"), 0))


def _relationship(db: Any, persona_a: str, persona_b: str) -> dict:
    """两人之间的关系行（方向任意；不存在返回 {}）。"""
    if not persona_a or not persona_b or persona_a == persona_b:
        return {}
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT * FROM persona_relationships"
            " WHERE (persona_a=? AND persona_b=?) OR (persona_a=? AND persona_b=?)"
            " ORDER BY closeness DESC LIMIT 1",
            (persona_a, persona_b, persona_b, persona_a),
        ).fetchone()
    return _row_dict(row)


def _insert_post(db: Any, *, persona: dict, event: dict, event_id: str, content: str) -> dict:
    """写一条事件动态（调用方已确认不存在）。幂等由调用方在同一把锁内复核。"""
    post_id = f"pp_{uuid.uuid4().hex[:12]}"
    now = _now_iso()
    metadata = {
        "event_id": event_id,
        "event_title": str(event.get("title") or ""),
        "event_type": str(event.get("event_type") or ""),
        "event_status": str(event.get("event_status") or ""),
        "event_start_time": str(event.get("start_time") or ""),
        "event_location": str(event.get("location") or ""),
        "participants": _participants(event),
        "prompt_version": PROMPT_VERSION,
    }
    values = (
        post_id,
        str(persona.get("id") or ""),
        content,
        "[]",                                    # media：事件动态是纯文字
        _dumps([str(event.get("event_type") or "")] if event.get("event_type") else []),
        _language_of(persona),
        "public",
        0,
        0,
        now,
        "",                                      # expires_at：事件动态长期可见
        POST_KIND_FEED,
        "",
        "",
        event_id,
        POST_TYPE_EVENT,
        now,
        _dumps(metadata),
    )
    with db._lock, db.conn:  # noqa: SLF001
        # 同一把锁内复核：并发/重入时仍然只会有一条 (event_id, persona_id)
        existing = db.conn.execute(
            "SELECT * FROM persona_posts WHERE source_event_id=? AND persona_id=? LIMIT 1",
            (event_id, str(persona.get("id") or "")),
        ).fetchone()
        if existing is not None:
            return _row_to_post(existing)
        db.conn.execute(
            "INSERT INTO persona_posts (id, persona_id, content, media, tags, language,"
            " visibility, like_count, comment_count, created_at, expires_at, post_kind,"
            " alt_text, source_ref, source_event_id, post_type, published_at, metadata)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
        row = db.conn.execute("SELECT * FROM persona_posts WHERE id=?", (post_id,)).fetchone()
    return _row_to_post(row)


def _update_post(db: Any, post_id: str, *, content: str, persona: dict) -> dict:
    """force=True 时原地重写正文（不新增行 → 幂等键始终唯一）。"""
    now = _now_iso()
    with db._lock, db.conn:  # noqa: SLF001
        row = db.conn.execute("SELECT * FROM persona_posts WHERE id=?", (post_id,)).fetchone()
        base = _row_to_post(row)
        old_meta = base.get("metadata") if isinstance(base.get("metadata"), dict) else {}
        merged = dict(old_meta)
        merged.update({"content_updated_at": now, "prompt_version": PROMPT_VERSION})
        db.conn.execute(
            "UPDATE persona_posts SET content=?, language=?, metadata=? WHERE id=?",
            (content, _language_of(persona), _dumps(merged), post_id),
        )
        row = db.conn.execute("SELECT * FROM persona_posts WHERE id=?", (post_id,)).fetchone()
    return _row_to_post(row)


def _insert_comment(
    db: Any, *, post: dict, commenter: dict, content: str
) -> dict:
    comment_id = f"ppc_{uuid.uuid4().hex[:12]}"
    now = _now_iso()
    post_id = str(post.get("id") or "")
    commenter_id = str(commenter.get("id") or "")
    with db._lock, db.conn:  # noqa: SLF001
        existing = db.conn.execute(
            "SELECT * FROM persona_post_comments WHERE post_id=? AND author_persona_id=?"
            " LIMIT 1",
            (post_id, commenter_id),
        ).fetchone()
        if existing is not None:
            return _row_to_comment(existing)
        db.conn.execute(
            "INSERT INTO persona_post_comments (id, post_id, user_id, content, created_at,"
            " author_persona_id, author_type) VALUES (?,?,?,?,?,?,?)",
            (comment_id, post_id, "", content, now, commenter_id, "persona"),
        )
        db.conn.execute(
            "UPDATE persona_posts SET comment_count = comment_count + 1 WHERE id=?",
            (post_id,),
        )
        row = db.conn.execute(
            "SELECT * FROM persona_post_comments WHERE id=?", (comment_id,)
        ).fetchone()
    return _row_to_comment(row)


# --------------------------------------------------------------------------- #
# 公开 API：生成单条动态 / 一个事件的多视角动态
# --------------------------------------------------------------------------- #
async def generate_post_for_participant(
    db: Any,
    llm: Any,
    event: dict,
    persona: dict,
    *,
    rng: random.Random | None = None,
    force: bool = False,
) -> dict | None:
    """为某个参与者生成一条事件动态（幂等）。

    - 事实只来自 ``event``（title/description/location/participants/时间），
      prompt 里显式禁止新增地点、人物、日期、数字；
    - 用该 persona 的 Speech DNA 声线、母语书写；
    - 写入 ``persona_posts``：``source_event_id`` / ``post_type='life_event'`` /
      ``published_at`` / ``metadata={'event_title','event_type',...}``；
    - 同一 ``(event_id, persona_id)`` 已存在 → 直接返回已有行（``force=True`` 时
      原地重写正文，**不新增行**）；
    - LLM 不可用/失败/产出违反事实 → 返回 ``None``（不写库、不编造）；
    - 只对 ``completed`` 事件发动态（planned/cancelled 不得当成已发生）。
    """
    ev = _as_dict(event)
    person = _as_dict(persona)
    persona_id = str(person.get("id") or "").strip()
    if not persona_id:
        log.debug("generate_post_for_participant: persona 缺 id，跳过")
        return None

    event_id = _event_id(db, ev)
    if not event_id:
        # 没有事件 id 就没有幂等键：宁可放弃，也不制造重复动态
        log.warning("generate_post_for_participant: 事件缺少 id/dedupe_key，跳过")
        return None

    status = str(ev.get("event_status") or "").strip().lower()
    existing = _find_post(db, event_id, persona_id)
    if existing and not force:
        # 幂等优先：即使事件状态后来被改（refresh_statuses 的边界情况），
        # 已经发过的动态仍然原样返回，不会消失或重复。
        return existing
    if status != STATUS_COMPLETED:
        log.debug("事件 %s 状态为 %s，不生成「已发生」动态", event_id, status or "(空)")
        return None

    participant_names = _display_names(db, _participants(ev))
    history = recent_posts(db, persona_id)
    messages = _post_messages(persona=person, event=ev,
                              participant_names=participant_names, recent=history)
    content = await _llm_text(
        llm, messages, key="content",
        schema_hint='{"content": "a personal post, or empty to skip"}',
        max_tokens=600,
    )
    content = post_text(content, limit=MAX_POST_CHARS)
    if not content or (not force and duplicate_post(content, history)):
        return None
    problems = post_content_violations(content, ev, person)
    if problems:
        log.info("事件动态被事实校验拦下（%s）：%s", ",".join(problems), content[:80])
        return None

    from app.v3.social_writing import grounded_post

    content = await grounded_post(
        llm, content, source={"event": _event_facts_block(ev, participant_names)},
        language=_language_of(person), limit=MAX_POST_CHARS,
    )
    if not content or post_content_violations(content, ev, person) or (not force and duplicate_post(content, history)):
        return None

    if existing and force:
        return _update_post(db, str(existing.get("id")), content=content, persona=person)
    return _insert_post(db, persona=person, event=ev, event_id=event_id, content=content)


async def generate_event_posts(
    db: Any,
    llm: Any,
    event: dict,
    *,
    rng: random.Random | None = None,
    max_posts: int | None = None,
) -> dict:
    """多参与者不同视角：每个 participant 独立过 :func:`should_post` 再生成。

    返回 ``{'posts': [...], 'skipped': [{'persona_id','reason'}]}``。
    ``posts`` 里既包含本次新建的，也包含此前已存在（幂等返回）的。

    ``max_posts``（可选，向后兼容）只限制**本次新建**的条数——批处理的每日配额用；
    它不裁剪事件事实（prompt 里仍然是完整参与者），只是不再给剩下的人发。
    """
    ev = _as_dict(event)
    rng = rng or random.Random()
    event_id = _event_id(db, ev)
    posts: list[dict] = []
    skipped: list[dict] = []
    created = 0
    if not event_id:
        return {"posts": [], "skipped": [{"persona_id": "", "reason": "event_without_id"}]}

    for persona_id in _participants(ev):
        if max_posts is not None and created >= max_posts:
            skipped.append({"persona_id": persona_id, "reason": "quota_reached"})
            continue
        existing = _find_post(db, event_id, persona_id)
        if existing:
            posts.append(existing)
            skipped.append({"persona_id": persona_id, "reason": "already_posted"})
            continue

        persona = _persona_row(db, persona_id)
        if not persona:
            skipped.append({"persona_id": persona_id, "reason": "persona_missing"})
            continue

        traits = dna_traits_for(persona)
        count = _recent_post_count(db, persona_id)
        if not should_post(persona=persona, dna_traits=traits, event=ev,
                           recent_post_count=count, rng=rng):
            skipped.append({"persona_id": persona_id, "reason": "should_post_false"})
            continue

        post = await generate_post_for_participant(db, llm, ev, persona, rng=rng)
        if post:
            posts.append(post)
            created += 1
        else:
            skipped.append({"persona_id": persona_id, "reason": "generation_failed"})
    return {"posts": posts, "skipped": skipped}


async def maybe_persona_comment(
    db: Any,
    llm: Any,
    post: dict,
    event: dict,
    commenter: dict,
    *,
    rng: random.Random | None = None,
) -> dict | None:
    """另一个 Persona 评论这条动态（写入 ``author_persona_id`` / ``author_type='persona'``）。

    - 关系必须存在（``persona_relationships``，方向任意）：不认识就不评论；
    - 幂等：同一 ``(post_id, commenter_persona_id)`` 只允许一条，第二次直接返回已有；
    - 概率 + 冷却在此刻掷一次；不评论返回 ``None``；
    - LLM 失败/产出违规 → ``None``，不写库。
    """
    post_data = _as_dict(post)
    person = _as_dict(commenter)
    post_id = str(post_data.get("id") or "").strip()
    commenter_id = str(person.get("id") or "").strip()
    if not post_id or not commenter_id:
        return None
    if commenter_id == str(post_data.get("persona_id") or ""):
        # 自己不能评论自己的动态（那是「补充说明」，不是评论）
        return None

    existing = _find_comment(db, post_id, commenter_id)
    if existing:
        return existing

    relationship = _relationship(db, str(post_data.get("persona_id") or ""), commenter_id)
    if not relationship:
        return None

    rng = rng or random.Random()
    prob = comment_probability(person, dna_traits_for(person), relationship)
    if rng.random() >= prob:
        return None

    ev = _as_dict(event)
    participant_names = _display_names(db, _participants(ev)) if ev else {}
    messages = _comment_messages(
        commenter=person, post=post_data, event=ev,
        participant_names=participant_names, relationship=relationship,
    )
    text = await _llm_text(
        llm, messages, key="comment",
        schema_hint='{"comment": "one short comment"}',
        max_tokens=180,
    )
    text = post_text(text, limit=MAX_COMMENT_CHARS)
    if not text:
        return None
    problems = post_content_violations(text, ev, person)
    # 评论更短、更口语，只拦「提到 AI」和「编造地名/年份」；太短不算错
    problems = [p for p in problems if p != "too_short"]
    if problems:
        log.info("评论被事实校验拦下（%s）：%s", ",".join(problems), text[:60])
        return None
    return _insert_comment(db, post=post_data, commenter=person, content=text)


# --------------------------------------------------------------------------- #
# 批处理：事件队列推进
# --------------------------------------------------------------------------- #
#: 事件「评估过但没发成」的补试上限。超过就当终局，不再占用队列——
#: 否则一个候选人全部概率拒绝/一直被事实校验拦下的事件会永远卡在队首。
POSTS_EVAL_RETRY_LIMIT = 3


def _event_evaluated(event: dict) -> bool:
    """该事件是否已经被动态生成流程「评估终结」（metadata 里有 posts_evaluated_at）。

    终结 = 这个事件我们不再尝试发动态：要么已经发过，要么当时没有人愿意发。
    """
    meta = event.get("metadata")
    if isinstance(meta, str):
        meta = _loads(meta, {})
    if not isinstance(meta, dict):
        return False
    return bool(str(meta.get("posts_evaluated_at") or "").strip())


def _record_posts_attempt(
    db: Any, event_id: str, *, generated: int, retryable: bool
) -> bool:
    """把本轮评估结果写回 ``persona_events.metadata``，返回是否已「终结」。

    写入字段：``posts_evaluated_at``（终结时刻，存在即不再重试）、
    ``posts_generated``（本次产出条数）、``posts_attempts``、``posts_last_attempt_at``。

    ``retryable=True`` 只用于「生成环节失败」（LLM 挂了/输出违反事实）这种偶发情况，
    累计 ``POSTS_EVAL_RETRY_LIMIT`` 次仍失败也算终结——这样既不会因为一次网络抖动
    丢掉这个事件，也不会让队列永远卡在同一条事件上。
    """
    now = _now_iso()
    with db._lock, db.conn:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT metadata FROM persona_events WHERE id=?", (event_id,)
        ).fetchone()
        meta = _loads(_row_dict(row).get("metadata"), {})
        if not isinstance(meta, dict):
            meta = {}
        attempts = int(_as_float(meta.get("posts_attempts"), 0)) + 1
        final = generated > 0 or not retryable or attempts >= POSTS_EVAL_RETRY_LIMIT
        meta["posts_attempts"] = attempts
        meta["posts_generated"] = int(generated)
        meta["posts_last_attempt_at"] = now
        if final:
            meta["posts_evaluated_at"] = now
        db.conn.execute(
            "UPDATE persona_events SET metadata=?, updated_at=? WHERE id=?",
            (_dumps(meta), now, event_id),
        )
    return final


def value_tier(event_type: Any) -> int:
    """事件类型的「价值层级」：3 = 里程碑/冲突/关系，2 = 社交/娱乐/旅行，
    1 = 工作/学习/意外，0 = 吃饭/日常。未知类型按 0 处理。

    只用于**批处理挑事件的顺序**（当天先出现值得分享的事），不参与分享概率。
    """
    key = str(event_type or "").strip().lower()
    return VALUE_TIERS.get(key, DEFAULT_VALUE_TIER)


def _completed_event_rows(
    db: Any, *, day: str | None, limit: int, since_hours: float | None = None
) -> list[dict]:
    """取「还没评估过」的 completed 事件，**先高价值、同层再先老后新**。

    排序 = ``value_tier DESC, 事件结束时间 ASC, id ASC``：
    - 价值优先：当天信息流应当先出现 achievement / social 这类值得分享的事件，
      而不是先冒出一堆 meal / daily_routine（阶段二「当天感」的核心）；
    - 同价值层内先老后新：后台每轮只处理 ``max_events`` 条，若按最新优先，队列会
      永远停在最新那几条低概率事件上，生活系统看起来停摆；先老后新让积压被推完。

    ``since_hours`` 用「事件真正结束的时间」做下界：
    ``COALESCE(NULLIF(completed_at,''), NULLIF(end_time,''), start_time)``。
    本项目的 ISO8601 字符串格式统一（``YYYY-MM-DDTHH:MM:SS+00:00``），
    字典序即时间序，所以直接字符串比较即可（与项目其他地方一致）。

    已经评估终结（metadata 有 ``posts_evaluated_at``）的事件不再返回——
    这是「人决定不发就是不发」的产品语义，也是队列能往前走的关键
    （否则每轮都在重试同样几条被概率拒绝的事件）。
    """
    sql = (
        "SELECT * FROM persona_events WHERE event_status=?"
        # 终结标记用 JSON 键名字面匹配；只做粗筛，最终以解析后的 metadata 为准
        " AND (metadata IS NULL OR metadata NOT LIKE '%\"posts_evaluated_at\"%')"
    )
    params: list[Any] = [STATUS_COMPLETED]
    text = str(day or "").strip()
    if text:
        sql += " AND (day=? OR substr(start_time, 1, 10)=?)"
        params.extend([text, text])
    if since_hours is not None and since_hours > 0:
        sql += (
            " AND COALESCE(NULLIF(completed_at, ''), NULLIF(end_time, ''), start_time) >= ?"
        )
        params.append(_hours_ago_iso(float(since_hours)))
    sql += (
        f" ORDER BY {_VALUE_TIER_SQL} DESC,"
        " COALESCE(NULLIF(completed_at, ''), NULLIF(end_time, ''), start_time) ASC,"
        " id ASC LIMIT ?"
    )
    params.append(max(1, int(limit)))
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(sql, tuple(params)).fetchall()
    events = [_row_to_event(row) for row in rows]
    # 兜底复核：LIKE 只做粗筛，真正判定以解析后的 metadata 为准
    return [event for event in events if not _event_evaluated(event)]


def _normalize_since_hours(value: Any) -> float | None:
    """``since_hours`` 归一化：None / bool / 非数字 / <=0 一律返回 None（= 不回看限制）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        hours = float(str(value).strip()) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        return None
    return hours if hours > 0 else None


# --------------------------------------------------------------------------- #
# 每日配额（避免一天刷出几十条事件动态）
# --------------------------------------------------------------------------- #
def _local_day_start_iso() -> str:
    """本地日（``SE_TZ``，缺省 Asia/Shanghai）零点对应的 UTC ISO 字符串。

    「当天」必须按人物/用户所处的本地日算，不能用 UTC——否则北京时间早上 8 点前
    会被算成前一天，配额就会算错。
    """
    local_now: datetime | None = None
    try:  # pragma: no cover - 依赖 daily_world 的实现细节
        from app.v3.daily_world import now_local  # noqa: PLC0415

        local_now = now_local()
    except Exception:  # noqa: BLE001 - 模块不可用就退回系统本地时区
        local_now = None
    if local_now is None:
        local_now = datetime.now().astimezone()
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return _iso(start)


def _today_event_post_count(db: Any) -> int:
    """本地日已经产出的事件动态条数（配额判定的唯一依据，纯查询）。"""
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT COUNT(*) AS n FROM persona_posts WHERE post_type=? AND created_at>=?",
            (POST_TYPE_EVENT, _local_day_start_iso()),
        ).fetchone()
    return int(_as_float(_row_dict(row).get("n"), 0))


def _normalize_daily_quota(value: Any) -> int | None:
    """``daily_quota`` 归一化。

    - 显式传 ``None`` → 读 ``SE_EVENT_POSTS_PER_DAY``（缺省 8）；
    - 显式传 ``<=0`` → 不限制（返回 None；调参时想关掉配额就用 0）；
    - 非数字/脏值 → 回落缺省 8（**失败安全**：绝不因为配置写错变成「无限发」）。
    """
    raw = value
    if raw is None:
        raw = os.getenv("SE_EVENT_POSTS_PER_DAY", "")
        if str(raw).strip() == "":
            return DEFAULT_EVENT_POSTS_PER_DAY
    if isinstance(raw, bool):
        return DEFAULT_EVENT_POSTS_PER_DAY
    try:
        quota = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return DEFAULT_EVENT_POSTS_PER_DAY
    return quota if quota > 0 else None


def _comment_backfill_enabled() -> bool:
    raw = os.getenv("SE_COMMENT_BACKFILL", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


# --------------------------------------------------------------------------- #
# 评论补位（让互动不只集中在「这一轮刚发的」动态上）
# --------------------------------------------------------------------------- #
async def comment_backfill(
    db: Any,
    llm: Any,
    *,
    max_comments: int = 2,
    window_hours: float = COMMENT_BACKFILL_WINDOW_HOURS,
    rng: random.Random | None = None,
) -> dict:
    """给「老的事件动态」补 AI 评论（每条最多 1 条，单次最多 ``max_comments`` 条）。

    规则：
    - 只看最近 ``window_hours`` 内、**还没有任何 AI 评论**的事件动态（更老的不硬塞互动）；
    - 评论者必须是**该事件参与者之外的熟人**，且与动态作者的关系已存在于
      ``persona_relationships``（不认识就不评论，绝不凭空造关系）；
    - 概率与写库完全复用 :func:`maybe_persona_comment`（含 ``(post_id, commenter)``
      幂等），本函数只负责「挑哪条动态、挑谁」；
    - LLM 不可用/失败 → 不写库、不编造，返回 0 条。

    返回 ``{'posts': 尝试过的动态数, 'comments': 新增评论数}``。
    """
    rng = rng or random.Random()
    budget = max(0, int(_as_float(max_comments, 2)))
    stats = {"posts": 0, "comments": 0}
    if budget == 0 or llm is None or getattr(llm, "uses_mock", False):
        return stats
    hours = _normalize_since_hours(window_hours)
    if hours is None:
        hours = COMMENT_BACKFILL_WINDOW_HOURS

    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT p.* FROM persona_posts p"
            " WHERE p.post_type=?"
            "   AND COALESCE(NULLIF(p.published_at, ''), p.created_at) >= ?"
            "   AND NOT EXISTS (SELECT 1 FROM persona_post_comments c"
            "                   WHERE c.post_id=p.id AND c.author_persona_id<>'')"
            " ORDER BY COALESCE(NULLIF(p.published_at, ''), p.created_at) ASC, p.id ASC"
            " LIMIT ?",
            (POST_TYPE_EVENT, _hours_ago_iso(hours), max(20, budget * 10)),
        ).fetchall()
    posts = [_row_to_post(row) for row in rows]

    for post in posts:
        if stats["comments"] >= budget:
            break
        author_id = str(post.get("persona_id") or "")
        event_id = str(post.get("source_event_id") or "")
        if not author_id or not event_id:
            continue
        event = _event_by_id(db, event_id)
        if not event:
            continue  # 事件被删了就没有事实依据，宁可不评论
        participants = set(_participants(event)) | {author_id}

        # 候选人 = 与作者有关系、且不在事件参与者里的熟人；越亲近越优先
        collaborators: list[tuple[float, str]] = []
        with db._lock:  # noqa: SLF001
            rel_rows = db.conn.execute(
                "SELECT persona_a, persona_b, closeness FROM persona_relationships"
                " WHERE persona_a=? OR persona_b=?",
                (author_id, author_id),
            ).fetchall()
        for rel in rel_rows:
            data = _row_dict(rel)
            other = (
                str(data.get("persona_b") or "")
                if str(data.get("persona_a") or "") == author_id
                else str(data.get("persona_a") or "")
            )
            if not other or other in participants:
                continue
            collaborators.append((_as_float(data.get("closeness"), 2.0), other))
        collaborators.sort(key=lambda item: (-item[0], item[1]))
        if not collaborators:
            continue

        stats["posts"] += 1
        for _closeness, commenter_id in collaborators:
            commenter = _persona_row(db, commenter_id)
            if not commenter:
                continue
            comment = await maybe_persona_comment(
                db, llm, post, event, commenter, rng=rng
            )
            if comment:
                stats["comments"] += 1
                break
    return stats


def _event_by_id(db: Any, event_id: str) -> dict:
    if not event_id:
        return {}
    with db._lock:  # noqa: SLF001
        row = db.conn.execute(
            "SELECT * FROM persona_events WHERE id=?", (event_id,)
        ).fetchone()
    return _row_to_event(row)


async def run_posts_cycle(
    db: Any,
    llm: Any,
    *,
    day: str | None = None,
    max_events: int = 6,
    rng: random.Random | None = None,
    since_hours: float | None = None,
    daily_quota: int | None = None,
) -> dict:
    """批处理：completed 事件 → 动态 + Persona 评论 + 老动态评论补位。

    幂等：以 ``source_event_id`` 是否已有动态为准——**该事件只要已经派生过动态，
    就整体跳过**（纯 DB 查询，无内存标记）。因此第二次运行必然新增 0 条，
    与随机数无关。``rng`` 是可选注入（生产环境传 None 即可），
    仅为测试可复现而增加，不改变契约调用方式。

    ``since_hours``（可选，向后兼容）：只处理「最近 N 小时内真正结束」的事件。
    后台循环传 ``since_hours=6`` 就不会在首次启动时把一整天甚至几天的历史事件
    全部补发成动态（产品上也不该给 10 小时前结束的活动现在补一条动态）。
    传 None / <=0 / 非数字 → 行为与不传完全一致。

    ``daily_quota``（可选，向后兼容）：当天（本地日）事件动态的硬上限。
    默认读 ``SE_EVENT_POSTS_PER_DAY``（缺省 8）；传 ``<=0`` 或非数字 = 不限制；
    配额用尽时**不做候选查询**直接返回（省掉无意义的 DB 扫描）。
    配额只限制「新建动态」条数，不会吃掉事件的评估进度。

    队列推进：每处理完一个事件（**无论最终有没有发文**）都会把结果写进该事件的
    ``metadata.posts_evaluated_at``。因此「所有人都不想发」的事件只会被评估一次，
    下一轮直接跳过，队列持续往前走——否则后台会一直卡在最新那几条低价值事件上。

    结尾自动调用一次 :func:`comment_backfill`（``SE_COMMENT_BACKFILL=0`` 可关），
    让互动不只集中在「这一轮刚发的」动态上。

    返回 ``{'day','events','posts','comments','skipped','since_hours',
    'daily_quota','today_posts','backfill_posts','backfill_comments'}``。
    """
    rng = rng or random.Random()
    wanted = max(0, int(_as_float(max_events, 6)))
    window = _normalize_since_hours(since_hours)
    quota = _normalize_daily_quota(daily_quota)
    today_posts = _today_event_post_count(db)
    stats: dict = {
        "day": str(day or ""), "events": 0, "posts": 0, "comments": 0, "skipped": 0,
        "since_hours": window, "daily_quota": quota, "today_posts": today_posts,
        "backfill_posts": 0, "backfill_comments": 0,
    }

    async def _finish() -> dict:
        """收尾：评论补位 + 重算当天条数 + 打日志（即使本轮什么都没发）。"""
        if _comment_backfill_enabled():
            backfill = await comment_backfill(db, llm, rng=rng)
            stats["backfill_posts"] = int(backfill.get("posts") or 0)
            stats["backfill_comments"] = int(backfill.get("comments") or 0)
        stats["today_posts"] = _today_event_post_count(db)
        # 每轮都打一行（哪怕 posts=0）：运维/验收靠它判断「在推进」还是「卡住了」
        log.info("事件动态批处理：%s", stats)
        return stats

    if wanted == 0:
        return await _finish()
    if llm is None or getattr(llm, "uses_mock", False):
        # 没有真实模型就不生成：事件动态必须来自 LLM，绝不编造。
        # 提前返回避免后台每轮都白跑一遍（SE_POSTS_TICK_S 默认为 240s × 事件 × 参与者）。
        log.debug("事件动态批处理跳过：LLM 不可用")
        return stats
    if quota is not None and today_posts >= quota:
        # 配额已满：直接返回，不做候选查询（评论补位仍然值得做，交给 _finish）
        log.info("事件动态批处理：今日配额已满（%d/%d）", today_posts, quota)
        return await _finish()

    candidates = _completed_event_rows(
        db, day=day, limit=max(wanted * 4, 12), since_hours=window
    )
    for event in candidates:
        if stats["events"] >= wanted:
            break
        # 每日配额：本轮还能新建几条动态。不看候选查询，逐事件卡住就不会超发。
        remaining: int | None = None
        if quota is not None:
            remaining = quota - today_posts - stats["posts"]
            if remaining <= 0:
                break
        event_id = str(event.get("id") or "")
        if not event_id:
            continue
        already = posts_for_event(db, event_id)
        # 幂等闸门：该事件只要已经派生过动态，就整体跳过。这样「第二次运行新增 0 条」
        # 不依赖随机数：第一次若有人选择不发，也不会在下一次循环里突然补发。
        # 顺手给「老数据」补上终结标记（历史动态是本次改动之前生成的，没有 metadata），
        # 否则它们会每轮占掉候选名额。
        if already:
            _record_posts_attempt(db, event_id, generated=len(already), retryable=False)
            stats["skipped"] += 1
            continue

        result = await generate_event_posts(
            db, llm, event, rng=rng, max_posts=remaining
        )
        new_posts = list(result.get("posts") or [])
        skipped_items = list(result.get("skipped") or [])
        stats["events"] += 1
        stats["posts"] += len(new_posts)
        stats["skipped"] += len(skipped_items)
        # 记评估结果：本轮没发文也不算失败——「没人想发」是终局决定；
        # 只有「生成环节失败」（LLM 异常/输出违规）才允许有限次重试。
        retryable = any(
            str(item.get("reason") or "") == "generation_failed" for item in skipped_items
        )
        _record_posts_attempt(
            db, event_id, generated=len(new_posts), retryable=retryable
        )
        if not new_posts:
            continue

        # 评论预算：每条事件动态 ≤1 条 AI 评论。只评论本轮新建的动态，
        # 这样第二次运行绝不会新增评论（幂等优先于「补评论」）。
        for post in new_posts:
            author_id = str(post.get("persona_id") or "")
            commenter_ids = [
                pid for pid in _participants(event)
                if pid and pid != author_id
            ]
            rng.shuffle(commenter_ids)
            for commenter_id in commenter_ids:
                commenter = _persona_row(db, commenter_id)
                if not commenter:
                    continue
                comment = await maybe_persona_comment(
                    db, llm, post, event, commenter, rng=rng
                )
                if comment:
                    stats["comments"] += 1
                    break
    return await _finish()


# --------------------------------------------------------------------------- #
# 聊天用事实块（纯读取）
# --------------------------------------------------------------------------- #
def _format_when(value: Any, *, with_time: bool = False, timezone_name: str = "UTC") -> str:
    moment = _parse_iso(value)
    if moment is None:
        return ""
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        moment = moment.astimezone(ZoneInfo(timezone_name))
    except (KeyError, ValueError):
        pass
    return moment.strftime("%Y-%m-%d %H:%M") if with_time else moment.strftime("%Y-%m-%d")


def _event_happened_at(event: dict) -> Any:
    # completed_at is a processing timestamp: delayed workers may backfill old events today.
    return event.get("end_time") or event.get("start_time") or event.get("completed_at")


def _event_line(event: dict, names: dict[str, str], *, viewer_id: str = "") -> str:
    """单条事件 → 一行事实（completed / planned 两种前缀，与 life_events 语义一致）。

    ``viewer_id`` 是「这条事实是给谁看的」：同去的人里排除 TA 自己，
    否则 Sophie 会看到 "with Sophie"。
    """
    title = str(event.get("title") or "").strip() or "(untitled)"
    status = str(event.get("event_status") or "").strip().lower()
    tail = _event_meta_tail(event, names, viewer_id=viewer_id)
    tz_name = str(_as_dict(event.get("metadata")).get("timezone") or "UTC")
    if status == STATUS_PLANNED:
        when = _format_when(event.get("start_time"), with_time=True, timezone_name=tz_name)
        head = f"PLAN (not happened yet) {when}:" if when else "PLAN (not happened yet):"
        return f"- {head} {title}{tail}"
    when = _format_when(_event_happened_at(event), timezone_name=tz_name)
    head = f"DONE {when}:" if when else "DONE:"
    return f"- {head} {title}{tail}"


def _event_meta_tail(event: dict, names: dict[str, str], *, viewer_id: str = "") -> str:
    bits: list[str] = []
    desc = str(event.get("description") or "").strip()
    if desc:
        bits.append(desc[:120])
    hide = viewer_id or str(event.get("persona_id") or "")
    others = [names.get(pid) or "someone" for pid in _participants(event) if pid != hide]
    if others:
        bits.append("with " + ", ".join(others))
    location = str(event.get("location") or "").strip()
    if location:
        bits.append("at " + location)
    # 用纯 ASCII 分隔符：这段文本会进 prompt / 日志，避免任何编码噪音
    return (" - " + ", ".join(bits)) if bits else ""


def _event_overlap(event: dict, query_text: str) -> int:
    if not query_text:
        return 0
    haystack = " ".join((
        str(event.get("title") or ""),
        str(event.get("description") or ""),
        str(event.get("location") or ""),
        str(event.get("event_type") or ""),
    ))
    return len(_tokens(query_text) & _tokens(haystack))


def _event_ts(event: dict) -> float:
    moment = _parse_iso(_event_happened_at(event))
    return moment.timestamp() if moment else 0.0


def _events_for_persona(db: Any, persona_id: str, *, days: int = 7, now: datetime | None = None) -> list[dict]:
    """该人物参与过的 completed 事件（主角或参与者）。cancelled/ongoing 不返回。"""
    current = now or _utcnow()
    params: tuple = (persona_id, STATUS_COMPLETED, current.isoformat())
    if days > 0:
        params += ((current - timedelta(days=days)).isoformat(),)
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT e.* FROM persona_event_participants m"
            " JOIN persona_events e ON e.id=m.event_id"
            " WHERE m.persona_id=? AND m.event_status=?"
            " AND m.happened_at <= julianday(?)"
            + (" AND m.happened_at >= julianday(?)" if days > 0 else "")
            + " ORDER BY m.happened_at DESC LIMIT 60",
            params,
        ).fetchall()
    events: list[dict] = []
    for row in rows:
        event = _row_to_event(row)
        # Keep the public decoder's validation of legacy malformed rows.
        if persona_id not in event["participants"] and event["persona_id"] != persona_id:
            continue
        if event["event_status"] != STATUS_COMPLETED:
            continue
        ts = _event_ts(event)
        if not ts or ts > current.timestamp():
            continue
        if days > 0 and ts < (current - timedelta(days=days)).timestamp():
            continue
        events.append(event)
    return events


def _planned_events_for_persona(
    db: Any, persona_id: str, *, limit: int = 1, now: datetime | None = None,
) -> list[dict]:
    """未来最近的 planned 事件（只能作为「计划」注入，绝不能当已发生）。"""
    if limit <= 0:
        return []
    now = now or _utcnow()
    with db._lock:  # noqa: SLF001
        rows = db.conn.execute(
            "SELECT e.* FROM persona_event_participants m"
            " JOIN persona_events e ON e.id=m.event_id"
            " WHERE m.persona_id=? AND m.event_status=? AND m.starts_at >= julianday(?)"
            " ORDER BY m.starts_at ASC LIMIT ?",
            (persona_id, STATUS_PLANNED, now.isoformat(), limit),
        ).fetchall()
    events: list[dict] = []
    for row in rows:
        event = _row_to_event(row)
        if persona_id not in event["participants"] and event["persona_id"] != persona_id:
            continue
        moment = _parse_iso(event.get("start_time"))
        if moment is None or moment < now:
            continue
        events.append(event)
        if len(events) >= limit:
            break
    return events


def persona_recent_facts(
    db: Any,
    persona_id: str,
    query_text: str = "",
    *,
    limit: int = 3,
    max_chars: int = 900,
    now: datetime | None = None,
) -> str:
    """给聊天用的**已发生**事实块（纯读取，绝不写库）。

    包含：
    1. 该人物最近 ``limit`` 条 **completed** 事件（可按 ``query_text`` 相关性排序）；
    2. 最近 1 条 **planned** 事件，写成 "PLAN (not happened yet)"（计划不是事实）；
    3. 自己发过的最近 2 条动态（含事件驱动的）；
    4. 别人对自己动态的最近 2 条评论（Persona 评论显示昵称）。
    **cancelled 事件绝不出现**；ongoing 也不出现（它不在「已发生 / 计划」两态里，
    当前在做什么由 persona_context 用 current_activity 单独提供）。
    返回空串表示没有任何相关内容。
    """
    pid = str(persona_id or "").strip()
    if not pid:
        return ""
    query = str(query_text or "").strip()
    want = max(0, int(_as_float(limit, 3)))
    moment = _parse_iso(now) or _utcnow()
    now_iso = _iso(moment)

    events = _events_for_persona(db, pid, days=7, now=moment)
    if query and events:
        events.sort(key=lambda ev: (-_event_overlap(ev, query), -_event_ts(ev)))
    else:
        events.sort(key=_event_ts, reverse=True)
    events = events[:want]
    planned = [] if want <= 0 else _planned_events_for_persona(db, pid, limit=1, now=moment)

    part_ids: list[str] = []
    for event in events + planned:
        part_ids.extend(_participants(event))
    names = _display_names(db, part_ids)

    with db._lock:  # noqa: SLF001
        post_rows = db.conn.execute(
            "SELECT * FROM persona_posts WHERE persona_id=?"
            " AND COALESCE(NULLIF(published_at, ''), created_at)<=?"
            " ORDER BY COALESCE(NULLIF(published_at, ''), created_at) DESC, id DESC LIMIT 2",
            (pid, now_iso),
        ).fetchall()
        comment_rows = db.conn.execute(
            "SELECT c.* FROM persona_post_comments c"
            " JOIN persona_posts p ON p.id = c.post_id"
            " WHERE p.persona_id=? AND c.content<>'' AND c.author_persona_id<>?"
            " AND c.created_at<=? AND COALESCE(NULLIF(p.published_at, ''), p.created_at)<=?"
            " ORDER BY c.created_at DESC, c.id DESC LIMIT 2",
            (pid, pid, now_iso, now_iso),
        ).fetchall()
    posts = [_row_to_post(row) for row in post_rows]
    comments = [_row_to_comment(row) for row in comment_rows]
    author_names = _display_names(
        db, [c["author_persona_id"] for c in comments if c["author_persona_id"]]
    )

    sections: list[str] = []
    if events:
        lines = [_event_line(event, names, viewer_id=pid) for event in events]
        sections.append("[Your life: things that already happened]\n" + "\n".join(lines))
    if planned:
        lines = [_event_line(event, names, viewer_id=pid) for event in planned]
        sections.append("[Your plans: not happened yet]\n" + "\n".join(lines))
    if posts:
        lines = []
        for post in posts:
            text = _clean_text(post.get("content"), 160)
            if not text:
                continue
            title = str((post.get("metadata") or {}).get("event_title") or "")
            tail = f" (about: {title})" if title else ""
            stamp = str(post.get("published_at") or post.get("created_at") or "")[:16]
            source = post.get("source_ref") or {}
            kind = "shared source, not personal experience" if isinstance(source, dict) and source.get("kind") == "stock_photo" else "past post"
            lines.append(f'- {stamp} ({kind}): "{text}"{tail}')
        if lines:
            sections.append("[What you recently posted — historical, not current activity]\n" + "\n".join(lines))
    if comments:
        lines = []
        for comment in comments:
            author_id = comment.get("author_persona_id") or ""
            who = author_names.get(author_id, "a reader") if author_id else "a reader"
            text = _clean_text(comment.get("content"), 140)
            if text:
                lines.append(f'- {who}: "{text}"')
        if lines:
            sections.append("[What people said about your posts]\n" + "\n".join(lines))

    text = "\n".join(sections).strip()
    if not text:
        return ""
    if max_chars > 0 and len(text) > max_chars:
        # 硬截断时尽量断在行末，避免半句事实
        cut = text[:max_chars]
        newline = cut.rfind("\n")
        text = (cut[:newline] if newline > max_chars * 0.5 else cut).rstrip()
    return text



_CODE_VALUE_TIERS = dict(VALUE_TIERS)
_CODE_SHARE_WEIGHT = dict(EVENT_TYPE_SHARE_WEIGHT)
_CODE_RHYTHM = {name: globals()[name] for name in (
    "DEFAULT_EVENT_POSTS_PER_DAY", "TENSION_PENALTY_STEP", "TENSION_PENALTY_FLOOR",
    "COMMENT_BACKFILL_WINDOW_HOURS", "_VALUE_TIER_SQL",
)}


def refresh_from_lexicon() -> dict[str, int]:
    """把 ``events.json``（价值档/分享权重）和 ``rhythm.json``（配额/惩罚/补位窗口）应用回运行时。"""
    applied = {"event_types": 0, "rhythm": 0}
    VALUE_TIERS.clear()
    VALUE_TIERS.update(_CODE_VALUE_TIERS)
    EVENT_TYPE_SHARE_WEIGHT.clear()
    EVENT_TYPE_SHARE_WEIGHT.update(_CODE_SHARE_WEIGHT)
    globals().update(_CODE_RHYTHM)
    try:
        from app.v3 import lexicon  # noqa: PLC0415

        events = lexicon.load("events")
    except Exception:  # noqa: BLE001
        events = None
    if isinstance(events, dict):
        types = events.get("types")
        if isinstance(types, dict) and types:
            enabled = {
                key: spec for key, spec in types.items()
                if isinstance(spec, dict) and spec.get("enabled", True)
            }
            if enabled:
                VALUE_TIERS.clear()
                EVENT_TYPE_SHARE_WEIGHT.clear()
                for key, spec in enabled.items():
                    tier = spec.get("value_tier", DEFAULT_VALUE_TIER)
                    if isinstance(tier, (int, float)) and not isinstance(tier, bool):
                        VALUE_TIERS[str(key)] = max(0, min(3, int(tier)))
                    weight = spec.get("share_weight", DEFAULT_EVENT_SHARE_WEIGHT)
                    if isinstance(weight, (int, float)) and not isinstance(weight, bool):
                        EVENT_TYPE_SHARE_WEIGHT[str(key)] = max(0.0, min(2.0, float(weight)))
                globals()["_VALUE_TIER_SQL"] = (
                    "CASE event_type "
                    + " ".join(
                        f"WHEN '{event_type}' THEN {tier}"
                        for event_type, tier in sorted(VALUE_TIERS.items())
                    )
                    + f" ELSE {DEFAULT_VALUE_TIER} END"
                )
                applied["event_types"] = len(VALUE_TIERS)
    try:
        from app.v3 import lexicon as _lex  # noqa: PLC0415

        rhythm = _lex.load("rhythm")
    except Exception:  # noqa: BLE001
        rhythm = None
    if isinstance(rhythm, dict) and isinstance(rhythm.get("params"), dict):
        params = rhythm["params"]

        def _num(key: str, default: float, low: float, high: float) -> float:
            raw = params.get(key, default)
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return float(default)
            return max(low, min(high, value))

        globals()["DEFAULT_EVENT_POSTS_PER_DAY"] = int(
            _num("event_posts_per_day", DEFAULT_EVENT_POSTS_PER_DAY, 1, 30)
        )
        globals()["TENSION_PENALTY_STEP"] = _num(
            "tension_penalty_step", TENSION_PENALTY_STEP, 0.0, 0.5
        )
        globals()["TENSION_PENALTY_FLOOR"] = _num(
            "tension_penalty_floor", TENSION_PENALTY_FLOOR, 0.0, 0.8
        )
        globals()["COMMENT_BACKFILL_WINDOW_HOURS"] = _num(
            "comment_backfill_hours", COMMENT_BACKFILL_WINDOW_HOURS, 0.0, 24.0
        )
        applied["rhythm"] = 4
    return applied


__all__ = [
    "COMMENT_BACKFILL_WINDOW_HOURS",
    "DEFAULT_EVENT_POSTS_PER_DAY",
    "EVENT_TYPE_SHARE_WEIGHT",
    "MAX_COMMENT_CHARS",
    "MAX_POST_CHARS",
    "POST_BUDGET_MAX",
    "POST_BUDGET_MIN",
    "POST_BUDGET_SUPPRESS_FACTOR",
    "POST_TYPE_EVENT",
    "PROMPT_VERSION",
    "TENSION_MAX",
    "TENSION_PENALTY_FLOOR",
    "TENSION_PENALTY_STEP",
    "TRAIT_KEYS",
    "VALUE_TIERS",
    "refresh_from_lexicon",
    "comment_backfill",
    "comment_probability",
    "dna_from_persona",
    "dna_traits_for",
    "generate_event_posts",
    "generate_post_for_participant",
    "maybe_persona_comment",
    "persona_post_budget",
    "persona_recent_facts",
    "post_content_violations",
    "posts_for_event",
    "run_posts_cycle",
    "share_probability",
    "should_post",
    "value_tier",
]
