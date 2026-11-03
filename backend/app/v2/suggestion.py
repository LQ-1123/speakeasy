"""气泡内联润色（v2）。

目标：给用户语音条旁边生成一条轻量批注：
``{"better_version", "explanation", "tag", "is_saved": false}``。

- 有 LLM key：一次 JSON 补全；解析失败退规则。
- 无 key / 离线：用规则库（覆盖中国学习者高频病句），没有可改进点时返回 ``None``。
绝不抛异常、绝不阻塞好友回复（由后台任务调用）。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from app.logging_utils import get_logger
from app.v2.llm_json import complete_json_safe, to_dict

log = get_logger(__name__)

#: (正则, 替换片段, 解释, 标签)；多规则可同时生效，拼成更完整的地道句。
_RULES: list[tuple[re.Pattern[str], str, str, str]] = [
    (
        re.compile(r"\bI\s+very\s+(like|love|enjoy|want)\b", re.I),
        r"I really \1",
        "very 不能直接修饰动词；用 really 更自然。",
        "grammar",
    ),
    (
        re.compile(r"\bI\s+very\s+much\s+(like|love)\b", re.I),
        r"I really \1",
        "very much 不与 like/love 连用；口语里用 really。",
        "grammar",
    ),
    (
        re.compile(r"\b(it|this|that|he|she|they)\s+(?:is|are)\s+too\s+much\s+(good|nice|bad|cool|tired)\b", re.I),
        r"it's actually \2",
        "too much 不修饰形容词；强调程度用 really / way / so。",
        "grammar",
    ),
    (
        re.compile(r"\bhow\s+to\s+say\b", re.I),
        "how do you say",
        "问“怎么说”用 how do you say，而不是 how to say。",
        "grammar",
    ),
    (
        re.compile(r"\bI\s+am\s+agree\b", re.I),
        "I agree",
        "agree 本身是动词，不需要 be 动词。",
        "grammar",
    ),
    (
        re.compile(r"\bopen\s+the\s+light\b", re.I),
        "turn on the light",
        "开灯用 turn on the light，不是 open the light。",
        "chunk",
    ),
    (
        re.compile(r"\bI\s+have\s+a\s+question\s+ask\s+you\b", re.I),
        "can I ask you a question",
        "ask 后面直接接人，不需要 a question ask you。",
        "chunk",
    ),
    (
        re.compile(r"\bgo\s+to\s+home\s+by\s+foot\b", re.I),
        "walk home",
        "walk home 比 go home by foot 更自然。",
        "chunk",
    ),
]


def _tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = re.sub(r"([,.!?;:])(?=[A-Za-z])", r"\1 ", text)
    text = text.strip()
    if text:
        text = text[0].upper() + text[1:]
    return text


def rule_suggestion(text: str) -> dict[str, Any] | None:
    """离线规则批注；没有命中返回 None。

    多条规则可叠加（例如同时修正 "I very like" 与 "it is too much good"），
    最终给出一句完整、可朗读的地道版本。
    """
    t = (text or "").strip()
    if len(t) < 3:
        return None

    fixed = t
    explanations: list[str] = []
    tags: list[str] = []
    for pattern, replacement, explanation, tag in _RULES:
        if not pattern.search(fixed):
            continue
        fixed = pattern.sub(replacement, fixed)
        explanations.append(explanation)
        tags.append(tag)

    if not explanations:
        return None
    fixed = _tidy(fixed)
    return {
        "better_version": fixed,
        "explanation": "；".join(dict.fromkeys(explanations)),
        "tag": tags[0],
        "is_saved": False,
    }


def _clean(value: Any, *, fallback: str = "") -> str:
    return str(value).strip() if value is not None else fallback


async def generate_suggestion(
    llm: Any,
    text: str,
    *,
    friend_name: str = "",
    friend_context: str = "",
) -> dict[str, Any] | None:
    """为一句用户英文生成润色批注。失败/无建议返回 None。"""
    text = (text or "").strip()
    if len(text) < 3:
        return None

    def _fallback() -> dict[str, Any] | None:
        return rule_suggestion(text)

    messages = [
        {
            "role": "system",
            "content": (
                "You are a warm English-speaking friend helping a Chinese learner sound more natural. "
                "Given the learner's sentence, produce ONE concise inline note as strict JSON: "
                '{"better_version": "...", "explanation": "...", "tag": "grammar|chunk|tone"}. '
                "better_version must be a full natural sentence (same meaning). "
                "explanation: one short Chinese sentence explaining why. "
                "If the sentence is already natural, return {\"better_version\": \"\", \"explanation\": \"\", \"tag\": \"\"}."
                + (f"\nLearner is chatting with {friend_name}." if friend_name else "")
                + (f"\nConversation context: {friend_context}" if friend_context else "")
            ),
        },
        {"role": "user", "content": text},
    ]
    try:
        parsed = await complete_json_safe(
            llm,
            messages,
            schema_hint='{"better_version": string, "explanation": string, "tag": string}',
            fallback=_fallback,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 后台任务不能炸
        log.warning("润色生成失败: %s: %s", type(exc).__name__, exc)
        return rule_suggestion(text)

    data = to_dict(parsed)
    better = _clean(data.get("better_version"))
    explanation = _clean(data.get("explanation"))
    tag = _clean(data.get("tag")) or "chunk"
    if not better:
        return None
    return {"better_version": better, "explanation": explanation, "tag": tag, "is_saved": False}
