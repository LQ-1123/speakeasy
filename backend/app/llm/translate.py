"""翻译：把 AI 的英文回复译成中文，供学习者对照。

为什么不单独接翻译 API
----------------------
用户已经配了一个 OpenAI 兼容的 LLM，直接用它翻译最省事：不用多一个 key、
不用多一份额度，而且**同一个模型知道上下文**（口音、场景、角色称呼），
译出来比通用翻译器更贴合对话。

成本控制
--------
- 逐句/逐条翻译，请求很短（几十 token）
- 进程内 LRU 缓存：同一句重复出现（角色固定开场白）不再花钱
- 翻译失败**不影响主对话**，前端只是不显示译文
"""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from typing import Any

from app.llm.client import ChatMessage, LLMClient, LLMError

log = logging.getLogger(__name__)

#: 译文缓存上限（条）。一条几十字，500 条 ≈ 几十 KB。
_CACHE_MAX = 500
_cache: "OrderedDict[str, str]" = OrderedDict()

SYSTEM_PROMPT = (
    "你是英语学习助手。把用户给的英文翻译成**自然、口语化的简体中文**。"
    "要求：\n"
    "- 只输出译文本身，不要引号、不要解释、不要原文\n"
    "- 保持原句的语气（随便/礼貌/惊讶等）\n"
    "- 保留人名、地名、品牌不译\n"
    "- 如果原文本身是中文，原样返回"
)


def _key(text: str) -> str:
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()


def _cache_get(text: str) -> str | None:
    k = _key(text)
    v = _cache.get(k)
    if v is not None:
        _cache.move_to_end(k)
    return v


def _cache_put(text: str, translated: str) -> None:
    k = _key(text)
    _cache[k] = translated
    _cache.move_to_end(k)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


def clear_cache() -> None:
    _cache.clear()


async def translate(
    text: str,
    *,
    client: LLMClient,
    target: str = "zh",
) -> dict[str, Any]:
    """把 ``text`` 译成中文。

    返回 ``{"ok": bool, "text": str, "translation": str, "cached": bool, "error": str|None}``。
    """
    src = (text or "").strip()
    if not src:
        return {"ok": True, "text": "", "translation": "", "cached": False, "error": None}

    cached = _cache_get(src)
    if cached is not None:
        return {"ok": True, "text": src, "translation": cached, "cached": True, "error": None}

    llm = client
    lang = "简体中文" if target in ("zh", "zh-CN", "cn") else target

    messages = [
        ChatMessage(role="system", content=SYSTEM_PROMPT),
        ChatMessage(role="user", content=f"翻成{lang}：\n{src}"),
    ]

    try:
        chunks: list[str] = []
        async for tok in llm.stream_chat(messages, temperature=0.2, max_tokens=1200):
            chunks.append(tok)
        out = "".join(chunks).strip().strip('"').strip("“”").strip()
        if not out:
            raise LLMError("翻译返回空")
        _cache_put(src, out)
        return {"ok": True, "text": src, "translation": out, "cached": False, "error": None}
    except Exception as exc:  # noqa: BLE001 - 翻译失败不能影响主流程
        log.warning("翻译失败: %s", exc)
        return {
            "ok": False,
            "text": src,
            "translation": "",
            "cached": False,
            "error": str(exc),
        }
