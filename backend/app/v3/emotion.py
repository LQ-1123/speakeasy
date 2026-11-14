"""Persona 持续情绪状态（不是数值游戏）。

设计动机
========
升级前，"情绪"是**回复后的一次性分类**：回复完就丢掉，下一轮重新猜。
结果就是人物没有连续的内在状态——昨天刚被夸、今天照样是同一个客服语气。

本模块给每个人物一份**持续的情绪状态**，存在 ``personas.emotion_state``(JSON)：

- ``mood``：一句词级的情绪标签（calm / cheerful / tired / stressed …）
- ``energy`` / ``stress`` / ``social_battery``：0..1 的连续量，**缓慢**变化
- ``emotional_context``：**一句英文短语**（"a bit drained after a long meeting"）
- ``updated_at``：本次更新时间的 ISO 字符串，用于计算衰减

三条硬规则
----------
1. **幅度小**：单个事件对任一维度的影响 ≤0.15；一轮聊天 ≤0.06。
   情绪是"水位"不是"血条"，一次对话不会让人物性格突变。
2. **有边界**：所有数值钳制在 0..1，脏数据（字符串/越界/缺字段）会被
   :func:`normalize_state` 修好，不会污染下游。
3. **不展示数字**：:func:`emotion_prompt_block` 只输出定性描述（"a bit low on energy"），
   绝不把 0.42 这种数字塞进 prompt 或界面——数字会让人物变成仪表盘。

"计划不等于事实"同样适用
------------------------
``event_status == "planned"`` 的事件**不改变情绪**：还没发生的事不会让人累。
只有 ``ongoing``（减半）与 ``completed``（足量）才会推动情绪。

时间衰减
--------
:func:`decay` 让状态向基线缓慢回归：安静几小时后，亢奋和崩溃都会淡掉。
聊天链路每轮读状态时按 ``updated_at`` 到现在的小时数调用一次，不需要后台定时器。
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any

from app.v3.daily_world import iso

#: 合法情绪词（词级，不要 1-10 分）
MOODS: tuple[str, ...] = (
    "calm", "content", "cheerful", "focused", "tired", "drained",
    "stressed", "irritated", "restless", "warm", "low",
)

#: 数值维度的基线（长期均值）：情绪向这里回落
BASELINE: dict[str, float] = {"energy": 0.58, "stress": 0.30, "social_battery": 0.60}

#: 衰减时间常数（小时）：约 9 小时回归 63%，24 小时基本回到基线
DECAY_HOURS = 9.0

#: 单事件 / 单轮聊天的变化上限（硬约束）
EVENT_DELTA_CAP = 0.15
CHAT_DELTA_CAP = 0.06

_NUMERIC_KEYS: tuple[str, ...] = ("energy", "stress", "social_battery")

#: 气质 → 初始情绪词
_TEMPERAMENT_MOOD: dict[str, str] = {
    "witty": "cheerful",
    "sarcastic": "restless",
    "calm": "calm",
    "warm": "warm",
    "shy": "low",
    "anxious": "tired",
    "energetic": "cheerful",
    "dry": "calm",
    "curious": "content",
    "practical": "content",
}

#: 事件类型 → 数值变化（绝对值都 ≤ EVENT_DELTA_CAP）
_EVENT_DELTAS: dict[str, dict[str, Any]] = {
    "achievement": {"energy": 0.10, "stress": -0.08, "social_battery": 0.06, "mood": "cheerful"},
    "personal_milestone": {"energy": 0.12, "stress": -0.10, "social_battery": 0.04, "mood": "cheerful"},
    "conflict": {"energy": -0.06, "stress": 0.14, "social_battery": -0.09, "mood": "irritated"},
    "unexpected_event": {"energy": -0.05, "stress": 0.10, "social_battery": -0.04, "mood": "restless"},
    "social": {"energy": -0.03, "stress": -0.04, "social_battery": 0.12, "mood": "warm"},
    "relationship": {"energy": -0.02, "stress": -0.05, "social_battery": 0.09, "mood": "warm"},
    "travel": {"energy": -0.08, "stress": 0.04, "social_battery": 0.05, "mood": "content"},
    "entertainment": {"energy": 0.05, "stress": -0.07, "social_battery": 0.05, "mood": "content"},
    "work": {"energy": -0.05, "stress": 0.05, "social_battery": -0.02, "mood": "focused"},
    "study": {"energy": -0.04, "stress": 0.04, "social_battery": -0.02, "mood": "focused"},
    "meal": {"energy": 0.03, "stress": -0.02},
    "daily_routine": {"energy": 0.01, "stress": -0.01},
}

#: 事件类型 → 情绪短语候选（英文，单句，不含数字）
_EVENT_CONTEXTS: dict[str, tuple[str, ...]] = {
    "achievement": ("quietly pleased about finishing something", "still a bit proud of a small win"),
    "personal_milestone": ("quietly proud of a bigger step", "carrying a sense of progress"),
    "conflict": ("a bit rattled after an argument", "tense after a disagreement"),
    "unexpected_event": ("thrown off by something unexpected", "still processing a surprise"),
    "social": ("warm from seeing people", "still smiling from good company"),
    "relationship": ("softened after a real conversation", "a little more open than usual"),
    "travel": ("tired but glad to have been somewhere else", "happy-tired from being out"),
    "entertainment": ("lighter after some proper downtime", "relaxed after a good evening"),
    "work": ("a bit drained after a long stretch of work", "steady, still in work mode"),
    "study": ("a bit worn out from studying", "head full from too much reading"),
    "meal": ("settled after eating", "a little sleepy after a proper meal"),
    "daily_routine": ("settled into the day", "moving on autopilot through the day"),
}

#: 聊天 intent → 数值变化
_INTENT_DELTAS: dict[str, dict[str, Any]] = {
    "greeting": {"social_battery": 0.02, "energy": 0.01},
    "question": {"social_battery": -0.01},
    "good_news": {"energy": 0.05, "stress": -0.04, "mood": "cheerful"},
    "bad_news": {"energy": -0.04, "stress": 0.06},
    "opinion_probe": {"stress": 0.02},
    "small_talk": {"social_battery": 0.03, "energy": -0.01},
    "closing": {"social_battery": -0.02},
    "silence": {"social_battery": -0.03, "stress": 0.02},
}

#: 聊天 behavior → 数值变化（都很小）
_BEHAVIOR_DELTAS: dict[str, dict[str, Any]] = {
    "celebrate": {"energy": 0.04, "social_battery": 0.03, "stress": -0.02, "mood": "cheerful"},
    "complain": {"stress": 0.03, "energy": -0.02},
    "end_conversation": {"social_battery": -0.02},
    "empathize": {"energy": -0.02},
    "comfort": {"energy": -0.01, "mood": "warm"},
    "ask_question": {"social_battery": 0.01},
    "share": {"social_battery": 0.02, "energy": -0.01},
    "tell_story": {"social_battery": 0.02, "energy": -0.01},
    "joke": {"energy": 0.02, "stress": -0.02},
    "tease": {"energy": 0.02, "stress": -0.02},
    "disagree": {"stress": 0.02},
    "change_topic": {"social_battery": -0.01},
}

#: 聊天 intent → 情绪短语（只有"值得记一笔"的会话才改写上下文）
_CHAT_CONTEXTS: dict[str, str] = {
    "bad_news": "a little heavy after hearing something hard",
    "good_news": "in a good mood after good news",
    "closing": "winding down",
    "greeting": "up for a bit of company",
}


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _iso_now() -> str:
    return iso(datetime.now(timezone.utc))


def _clamp01(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return max(0.0, min(1.0, float(default)))
    if math.isnan(number) or math.isinf(number):
        return max(0.0, min(1.0, float(default)))
    return max(0.0, min(1.0, number))


def _loads(raw: Any, default: Any) -> Any:
    if isinstance(raw, type(default)):
        return raw
    if raw is None or raw == "":
        return default
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _pick(options: tuple[str, ...], seed: str) -> str:
    """确定性挑一个短语（不用 random，同一个事件永远同一句话）。"""
    if not options:
        return ""
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return options[digest[0] % len(options)]


def _mood_from(energy: float, stress: float, battery: float) -> str:
    """从三个数值推导一个情绪词（只在没有明确 mood 或有较大变化时使用）。"""
    if stress >= 0.70:
        return "stressed"
    if energy <= 0.28:
        return "drained"
    if battery <= 0.28:
        return "tired"
    if energy >= 0.72 and stress <= 0.35:
        return "cheerful"
    if stress <= 0.40 and battery >= 0.60:
        return "content"
    if energy >= 0.60:
        return "focused"
    return "calm"


def _clip_text(text: Any, limit: int = 140) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- #
# 状态构造 / 规范化
# --------------------------------------------------------------------------- #
def default_state(persona: dict) -> dict:
    """按人设生成初始情绪（确定性，不含随机）。

    气质/能量来自 ``personas.ai_config``（例如 ``{"temperament":"witty","energy":0.55}``），
    没有就用基线。返回合法状态，可直接落 ``personas.emotion_state``。
    """
    persona = persona if isinstance(persona, dict) else {}
    cfg = _loads(persona.get("ai_config"), {})
    temperament = str(cfg.get("temperament") or persona.get("temperament") or "").strip().lower()
    cfg_energy = _clamp01(cfg.get("energy"), 0.55) if cfg else 0.55

    energy = _clamp01(0.32 + 0.52 * cfg_energy, BASELINE["energy"])
    stress = _clamp01(0.34 - 0.14 * (cfg_energy - 0.5), BASELINE["stress"])
    battery = _clamp01(0.50 + 0.30 * (cfg_energy - 0.5), BASELINE["social_battery"])
    mood = _TEMPERAMENT_MOOD.get(temperament) or _mood_from(energy, stress, battery)
    return {
        "mood": mood if mood in MOODS else "calm",
        "energy": round(energy, 3),
        "stress": round(stress, 3),
        "social_battery": round(battery, 3),
        "emotional_context": "",
        "updated_at": _iso_now(),
    }


def normalize_state(value: Any) -> dict:
    """脏输入（None / JSON 字符串 / 越界数字 / 乱写 mood）→ 合法状态。

    缺 ``updated_at`` 时**不**补当前时间：那样会让"很久没更新的旧状态"看起来是刚更新的，
    反而挡住 decay。留空表示"新鲜度未知"。
    """
    data = value
    if isinstance(data, str):
        data = _loads(data, {})
    if not isinstance(data, dict):
        data = {}
    mood = str(data.get("mood") or "").strip().lower()
    energy = _clamp01(data.get("energy"), BASELINE["energy"])
    stress = _clamp01(data.get("stress"), BASELINE["stress"])
    battery = _clamp01(data.get("social_battery"), BASELINE["social_battery"])
    if mood not in MOODS:
        mood = _mood_from(energy, stress, battery)
    return {
        "mood": mood,
        "energy": round(energy, 3),
        "stress": round(stress, 3),
        "social_battery": round(battery, 3),
        "emotional_context": _clip_text(data.get("emotional_context")),
        "updated_at": str(data.get("updated_at") or "").strip(),
    }


# --------------------------------------------------------------------------- #
# 衰减
# --------------------------------------------------------------------------- #
def decay(state: dict, *, hours: float) -> dict:
    """向基线**缓慢**回归（纯函数，不改入参）。

    参数 ``hours`` 是距上次更新经过的小时数；0 或负数直接返回规范化后的原状态。
    ``emotional_context`` 是"此刻"的余韵：过了 6 小时以上就淡掉。
    """
    current = normalize_state(state)
    try:
        elapsed = float(hours or 0.0)
    except (TypeError, ValueError):
        elapsed = 0.0
    if elapsed <= 0:
        return current
    rate = 1.0 - math.exp(-elapsed / DECAY_HOURS)
    out = dict(current)
    for key in _NUMERIC_KEYS:
        base = BASELINE[key]
        out[key] = round(_clamp01(out[key] + (base - out[key]) * rate, base), 3)
    if rate >= 0.25:
        out["mood"] = _mood_from(out["energy"], out["stress"], out["social_battery"])
    if elapsed >= 6.0:
        out["emotional_context"] = ""
    out["updated_at"] = _iso_now()
    return out


# --------------------------------------------------------------------------- #
# 事件 / 聊天 → 状态
# --------------------------------------------------------------------------- #
def _apply_delta(state: dict, delta: dict[str, Any], *, cap: float) -> dict:
    """把数值变化钳到 ±cap 后套到状态上（所有维度都钳制 0..1）。"""
    out = normalize_state(state)
    for key in _NUMERIC_KEYS:
        if key not in delta:
            continue
        try:
            change = float(delta[key])
        except (TypeError, ValueError):
            continue
        change = max(-cap, min(cap, change))
        out[key] = round(_clamp01(out[key] + change, BASELINE[key]), 3)
    mood = delta.get("mood")
    if isinstance(mood, str) and mood in MOODS:
        out["mood"] = mood
    out["updated_at"] = _iso_now()
    return out


def _event_context(event_type: str, state: dict, event: dict) -> str:
    """事件 → 一句英文情绪短语（不含数字，长度受控）。"""
    options = _EVENT_CONTEXTS.get(event_type) or _EVENT_CONTEXTS["daily_routine"]
    phrase = _pick(options, f"{event.get('title') or ''}|{event_type}")
    if state["energy"] <= 0.30:
        phrase = f"{phrase}, and running low on energy"
    elif state["stress"] >= 0.72:
        phrase = f"{phrase}, and a bit wound up"
    return _clip_text(phrase, 120)


def apply_event(state: dict, event: dict) -> dict:
    """事件改变情绪（幅度小、有上限、**计划中的事件不算**）。

    - ``planned`` / ``cancelled`` → 原样返回（还没发生的事不影响人）；
    - ``ongoing`` → 变化量减半；
    - ``completed`` → 足量变化，但任一维度 ≤0.15。
    """
    current = normalize_state(state)
    event = event if isinstance(event, dict) else {}
    status = str(event.get("event_status") or "planned").strip().lower()
    if status in ("planned", "cancelled"):
        return current
    event_type = str(event.get("event_type") or "daily_routine").strip().lower()
    delta = dict(_EVENT_DELTAS.get(event_type) or _EVENT_DELTAS["daily_routine"])
    if status == "ongoing":
        for key in _NUMERIC_KEYS:
            if key in delta:
                delta[key] = float(delta[key]) * 0.5
    out = _apply_delta(current, delta, cap=EVENT_DELTA_CAP)
    out["emotional_context"] = _event_context(event_type, out, event)
    return out


def apply_chat(state: dict, *, intent: str = "unknown", behavior: str = "respond") -> dict:
    """一轮聊天对情绪的**微小**推动（≤0.06/维度）。

    ``intent`` 与 ``behavior`` 来自 :mod:`app.v3.behavior`（同一套枚举）；
    认不出来的值一律忽略——聊天链路不能因为枚举漂移而崩。
    """
    current = normalize_state(state)
    delta: dict[str, Any] = {}
    for source, key in ((_INTENT_DELTAS, str(intent or "").lower()),
                        (_BEHAVIOR_DELTAS, str(behavior or "").lower())):
        changes = source.get(key) or {}
        for name, value in changes.items():
            if name in _NUMERIC_KEYS:
                delta[name] = float(delta.get(name, 0.0)) + float(value)
            elif name == "mood" and "mood" not in delta:
                delta[name] = value
    if not delta:
        return current
    out = _apply_delta(current, delta, cap=CHAT_DELTA_CAP)
    context = _CHAT_CONTEXTS.get(str(intent or "").lower())
    if context:
        out["emotional_context"] = _clip_text(context, 120)
    return out


# --------------------------------------------------------------------------- #
# prompt 片段
# --------------------------------------------------------------------------- #
def _energy_word(value: float) -> str:
    if value < 0.25:
        return "running on empty"
    if value < 0.45:
        return "low"
    if value < 0.65:
        return "steady"
    if value < 0.85:
        return "good"
    return "high"


def _stress_word(value: float) -> str:
    if value < 0.25:
        return "calm"
    if value < 0.45:
        return "manageable"
    if value < 0.65:
        return "noticeably tense"
    return "high"


def _battery_word(value: float) -> str:
    if value < 0.25:
        return "empty, so short replies are kind"
    if value < 0.45:
        return "low"
    if value < 0.65:
        return "okay"
    if value < 0.85:
        return "full"
    return "very full, happy to talk"


def _mood_word(mood: str) -> str:
    """情绪词 → 可读短语（避免 prompt 里出现生硬的枚举值）。"""
    return {
        "calm": "calm",
        "content": "quietly content",
        "cheerful": "in a good mood",
        "focused": "focused",
        "tired": "tired",
        "drained": "a bit drained",
        "stressed": "stressed",
        "irritated": "irritated",
        "restless": "restless",
        "warm": "warm and open",
        "low": "a little low",
    }.get(str(mood or ""), "calm")


def emotion_prompt_block(state: dict, *, language: str = "en") -> str:
    """情绪状态 → prompt 用的一小段**定性**描述（**绝不含数字**）。

    ``language`` 保留给后续本地化：prompt 指令层统一英文（与
    ``behavior_directive`` 一致，模型按 system 的语言执行，回复语言另由 DNA 决定）。
    """
    current = normalize_state(state)
    lines = [
        "How you feel right now (internal guidance, not a script):",
        f"- mood: {_mood_word(current['mood'])}",
        f"- energy: {_energy_word(current['energy'])}",
        f"- stress: {_stress_word(current['stress'])}",
        f"- social battery: {_battery_word(current['social_battery'])}",
    ]
    context = str(current.get("emotional_context") or "").strip()
    if context:
        lines.append(f"- underlying: {context}")
    lines.append(
        "Let this quietly colour how much you say and how warm you sound. "
        "Do not recite these labels or make every reply about your mood. "
        "If asked how you are, a plain answer such as feeling tired is fine; "
        "do not invent a cause."
    )
    return "\n".join(lines)


__all__ = [
    "BASELINE",
    "CHAT_DELTA_CAP",
    "DECAY_HOURS",
    "EVENT_DELTA_CAP",
    "MOODS",
    "apply_chat",
    "apply_event",
    "decay",
    "default_state",
    "emotion_prompt_block",
    "normalize_state",
]
