"""润色建议服务：把学习者的英文句子改得更地道，并给出中文解释。

对外契约见 ``docs/CONTRACTS.md`` 第 8 节。

设计约束
--------
- **绝不阻塞音频**：只在 ``assistant_done`` 之后由后台 task 调用（见 orchestrator）。
- **绝不抛异常**：任何失败都吞掉并 debug 日志，返回 ``[]``。
- **容错解析**：LLM 可能返回 ```json 围栏、前后废话、字段缺失、类型错乱、
  甚至单个对象而不是数组——逐条校验，坏数据丢掉而不是整批失败。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

from app.config import LLMSettings, PipelineSettings
from app.llm.client import ChatMessage, LLMClient, LLMError, extract_json
from app.llm.mock_llm import mock_suggestions
from app.logging_utils import get_logger
from app.personas import PersonaLibrary

log = get_logger(__name__)

_VALID_REGISTERS = frozenset({"neutral", "casual", "formal"})

_SYSTEM_PROMPT = """You are a native English editor helping a Mandarin-speaking learner of spoken English.

You will be given one sentence the learner said, plus the persona they are practising with.

Your job: propose 1-2 concrete improvements to how they said it.

Rules:
- Only touch phrasing, collocation, register, or structure. Never invent new content or change the meaning.
- The rewrite must be something a native speaker would actually say out loud in that scene.
- Prefer 3-8 words of change, not a full rewrite. Keep it learnable.
- The `why` explanation MUST be written in Simplified Chinese (中文), 15-40 characters, and must name the
  specific mechanism (e.g. "very 不能直接修饰动词", "点单用 I'd like 更礼貌", "feedback 不可数").
- `register` is exactly one of: "neutral", "casual", "formal".

Respond with ONLY a JSON array, no prose, no markdown fence:
[{"original": "<the exact substring you are improving>", "better": "<the full improved sentence>",
  "why": "<中文解释>", "register": "casual"}]

If the sentence is already natural, return exactly: []"""


@dataclass
class Suggestion:
    """一条润色建议（字段与 PROTOCOL 2.9 的 items 对齐）。"""

    original: str
    better: str
    why: str
    register: str = "neutral"

    def to_dict(self) -> dict[str, str]:
        return {
            "original": self.original,
            "better": self.better,
            "why": self.why,
            "register": self.register,
        }


# --------------------------------------------------------------------------- #
# 解析（纯函数，便于单测）
# --------------------------------------------------------------------------- #
def parse_suggestions(raw: str, *, max_items: int = 2) -> list[Suggestion]:
    """把 LLM 的原始输出解析成 ``Suggestion`` 列表；任何异常都返回 ``[]``。

    容错点：
    - ```json 围栏 / 前后多余文本
    - 返回单个对象而非数组
    - 字段缺失、非字符串、空串
    - ``register`` 非法 → 归一为 ``neutral``
    - ``better`` 与 ``original`` 相同 → 丢弃（没有改进）
    """
    if not raw or not raw.strip():
        return []
    try:
        data = extract_json(raw)
    except LLMError as exc:
        log.debug("suggestion parse failed: %s", exc)
        return []

    # 允许 {"items": [...]} 或单个对象
    if isinstance(data, dict):
        for key in ("items", "suggestions", "data", "result"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        log.debug("suggestion payload is not a list: %r", type(data).__name__)
        return []

    out: list[Suggestion] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        original = _as_text(item.get("original"))
        better = _as_text(item.get("better"))
        if not better:
            continue
        if not original:
            original = better
        if _norm(original) == _norm(better):
            continue
        why = _as_text(item.get("why")) or _as_text(item.get("reason"))
        register = _as_text(item.get("register")).lower() or "neutral"
        if register not in _VALID_REGISTERS:
            register = "neutral"
        out.append(Suggestion(original=original, better=better, why=why, register=register))
        if len(out) >= max_items:
            break
    return out


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


# --------------------------------------------------------------------------- #
# 服务
# --------------------------------------------------------------------------- #
class SuggestionService:
    """调用 LLM 生成润色建议；失败一律静默降级。"""

    def __init__(
        self,
        llm: LLMClient,
        personas: PersonaLibrary,
        llm_cfg: LLMSettings | None = None,
        pipe_cfg: PipelineSettings | None = None,
    ) -> None:
        self.llm = llm
        self.personas = personas
        self.llm_cfg = llm_cfg or getattr(llm, "cfg", None)
        self.pipe_cfg = pipe_cfg
        self.max_items = 2

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.pipe_cfg, "suggestions_enabled", True))

    async def suggest(
        self,
        user_text: str,
        *,
        persona_id: str,
        history: Sequence[ChatMessage] | None = None,
        timeout_s: float | None = None,
    ) -> list[Suggestion]:
        """生成 1-2 条建议。**永不抛异常**。"""
        text = (user_text or "").strip()
        if not text or not self.enabled:
            return []
        try:
            return await self._suggest_impl(
                text, persona_id=persona_id, history=history, timeout_s=timeout_s
            )
        except Exception as exc:  # noqa: BLE001 - 建议服务绝不上升到调用方
            log.debug("suggestion failed (silently ignored): %s: %s", type(exc).__name__, exc)
            return []

    async def _suggest_impl(
        self,
        text: str,
        *,
        persona_id: str,
        history: Sequence[ChatMessage] | None,
        timeout_s: float | None,
    ) -> list[Suggestion]:
        persona = self.personas.get(persona_id)
        context = (
            f"Scene: {persona.scene}\n"
            f"Persona: {persona.name} ({persona.accent_short})\n"
            f"Editorial focus for this persona: {persona.suggestion_hint or '(general naturalness)'}"
        )

        messages: list[ChatMessage] = [
            ChatMessage(role="system", content=_SYSTEM_PROMPT + "\n\n" + context),
        ]
        # 只带最后两条 user 语境，避免把整段历史塞进去（快且省 token）
        for msg in (history or [])[-2:]:
            if msg.role in ("user", "assistant") and msg.content.strip():
                messages.append(ChatMessage(role=msg.role, content=msg.content))
        messages.append(
            ChatMessage(
                role="user",
                content=(
                    "The learner said:\n"
                    f'"""{text}"""\n\n'
                    "Return the JSON array now."
                ),
            )
        )

        # mock 后端直接走规则，省掉一次 JSON round-trip（也与 mock LLM 行为一致）
        if getattr(self.llm, "uses_mock", False):
            return self._mock(text)

        raw = await self.llm.complete_json(messages, schema_hint=_SCHEMA_HINT)
        items = parse_suggestions(raw, max_items=self.max_items)
        if not items:
            log.debug("suggestion returned no usable items (raw=%r)", (raw or "")[:200])
        return items

    def _mock(self, text: str) -> list[Suggestion]:
        try:
            rows = mock_suggestions(text)
        except Exception as exc:  # pragma: no cover - 规则函数本身不会抛
            log.debug("mock suggestions failed: %s", exc)
            return []
        out = []
        for row in rows[: self.max_items]:
            out.append(
                Suggestion(
                    original=str(row.get("original", "")),
                    better=str(row.get("better", "")),
                    why=str(row.get("why", "")),
                    register=str(row.get("register", "neutral")),
                )
            )
        return out

    def parse(self, raw: str) -> list[Suggestion]:
        """暴露解析器，便于 HTTP 层与测试复用。"""
        return parse_suggestions(raw, max_items=self.max_items)


#: 传给 ``complete_json`` 的结构提示（mock 后端据此判断要生成建议）
_SCHEMA_HINT = (
    'JSON array of suggestion objects: '
    '{"original": string, "better": string, "why": string(中文), "register": "neutral"|"casual"|"formal"}'
)


def dumps_suggestions(items: Sequence[Suggestion]) -> str:
    """序列化为 JSON（测试/调试用）。"""
    return json.dumps([i.to_dict() for i in items], ensure_ascii=False)


__all__ = [
    "Suggestion",
    "SuggestionService",
    "dumps_suggestions",
    "parse_suggestions",
]
