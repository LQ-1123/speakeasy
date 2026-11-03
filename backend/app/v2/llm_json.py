"""LLM 结构化输出工具：JSON 提取 / 容错补全 / 确定性兜底。

约定：本模块**不**假设 LLM 一定可用。``complete_json_safe`` 在
- LLM 未配置（mock 模式）、
- 网络/鉴权错误、
- 返回非 JSON
时，一律调用调用方提供的 ``fallback``，保证业务链路永不因为 LLM 挂掉。
"""

from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable, Sequence

from app.logging_utils import get_logger

log = get_logger(__name__)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(raw: str) -> Any | None:
    """从模型输出里尽量抠出 JSON 对象/数组。失败返回 None。"""
    if not raw:
        return None
    text = raw.strip()
    # 1) 直接解析
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        pass
    # 2) ```json ... ``` 代码块
    m = _FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except (TypeError, ValueError):
            pass
    # 3) 找第一个 { 或 [ 并配平括号
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        if start < 0:
            continue
        depth = 0
        in_str = False
        escape = False
        for idx in range(start, len(text)):
            ch = text[idx]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : idx + 1])
                    except (TypeError, ValueError):
                        break
    return None


def to_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def to_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


async def complete_json_safe(
    llm: Any,
    messages: Sequence[dict[str, str]],
    *,
    schema_hint: str = "",
    fallback: Callable[[], Any] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> Any:
    """调用 ``llm.complete_json`` 并解析；任何失败返回 ``fallback()``。

    ``fallback`` 只在 mock/失败时调用（不是每次都调用），因此可用于构造确定性结果。
    """
    if getattr(llm, "uses_mock", False):
        return fallback() if fallback is not None else None

    try:
        raw = await llm.complete_json(
            messages,
            schema_hint=schema_hint,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except Exception as exc:  # noqa: BLE001 - LLM 失败不能拖垮业务
        log.warning("complete_json 失败（%s: %s），使用兜底", type(exc).__name__, exc)
        return fallback() if fallback is not None else None

    parsed = extract_json(raw)
    if parsed is None:
        log.warning("LLM 返回无法解析为 JSON（len=%d），使用兜底", len(raw or ""))
        return fallback() if fallback is not None else None
    return parsed


async def maybe_await(value: Any) -> Any:
    if isinstance(value, Awaitable):
        return await value
    return value
