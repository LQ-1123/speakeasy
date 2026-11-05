"""V2 聊天内翻译：任意语言 ↔ 中文（语言学习对照用）。

设计
----
- 复用用户已配置的 OpenAI 兼容 LLM（不额外接翻译 API）：同一模型懂上下文，
  同一条消息只会翻译一次（进程内 LRU 缓存，按 text+target）。
- **方向自动判断**：文本含中日韩字符 → 译成好友的 ``reply_language``（无好友则英语）；
  否则译成简体中文。这样「好友外语 → 中文」和「自己写的中文 → 好友语言」都顺手。
- **无 LLM Key 时明确失败**：返回 ``ok=false`` + 可读 message，前端提示去个人设置，
  绝不返回伪翻译（避免误导学习者）。
- 翻译失败不影响聊天主流程（独立端点，独立缓存）。
"""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from typing import Any

from app.logging_utils import get_logger

log = get_logger(__name__)

CACHE_MAX = 500
_cache: "OrderedDict[str, str]" = OrderedDict()

#: 目标语言展示名（prompt 与前端标签共用）
LANGUAGE_NAMES: dict[str, str] = {
    "zh": "简体中文",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "pt": "Português",
    "es": "Español",
    "de": "Deutsch",
    "fr": "Français",
    "it": "Italiano",
}

_ALIASES = {
    "zh-cn": "zh", "zh_cn": "zh", "cn": "zh", "chinese": "zh", "中文": "zh",
    "english": "en", "en-us": "en", "en-gb": "en",
    "japanese": "ja", "日本語": "ja", "日语": "ja",
    "korean": "ko", "한국어": "ko", "韩语": "ko",
    "portuguese": "pt", "pt-br": "pt", "葡萄牙语": "pt",
    "spanish": "es", "es-es": "es", "es-mx": "es", "西班牙语": "es",
    "german": "de", "de-de": "de", "德语": "de",
    "french": "fr", "fr-fr": "fr", "法语": "fr",
    "italian": "it", "it-it": "it", "意大利语": "it",
}

#: 中日韩字符（假名/汉字/谚文）——用于判断「这是外语回复还是中文输入」
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")


def normalize_language(value: str | None) -> str:
    """把语言代码/别名归一到内部代码；未知返回原小写值。"""
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    return _ALIASES.get(raw, raw)


def has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def resolve_target(
    text: str,
    *,
    friend: dict | None = None,
    target: str | None = None,
) -> str:
    """决定翻译目标语言（见模块 docstring）。"""
    explicit = normalize_language(target)
    if explicit:
        return explicit
    if has_cjk(text):
        lang = normalize_language(str((friend or {}).get("reply_language") or ""))
        return lang or "en"
    return "zh"


def target_label(target: str) -> str:
    return LANGUAGE_NAMES.get(target, target or "中文")


def _cache_key(text: str, target: str) -> str:
    return hashlib.sha1(f"{target}|{text.strip()}".encode("utf-8")).hexdigest()


def _cache_get(text: str, target: str) -> str | None:
    key = _cache_key(text, target)
    value = _cache.get(key)
    if value is not None:
        _cache.move_to_end(key)
    return value


def _cache_put(text: str, target: str, translated: str) -> None:
    key = _cache_key(text, target)
    _cache[key] = translated
    _cache.move_to_end(key)
    while len(_cache) > CACHE_MAX:
        _cache.popitem(last=False)


def clear_cache() -> None:
    _cache.clear()


def _fail(target: str, error: str, message: str, text: str = "") -> dict[str, Any]:
    return {
        "ok": False,
        "text": text,
        "translation": "",
        "target": target,
        "target_label": target_label(target),
        "cached": False,
        "error": error,
        "message": message,
    }


async def translate_text(
    llm: Any,
    text: str,
    *,
    target: str,
    source: str = "auto",
    context: str = "",
) -> dict[str, Any]:
    """翻译一条文本；永不抛异常（返回 ok=false）。"""
    src = (text or "").strip()
    target = normalize_language(target) or "zh"
    if not src:
        return {
            "ok": True,
            "text": "",
            "translation": "",
            "target": target,
            "target_label": target_label(target),
            "cached": False,
            "error": None,
            "message": "",
        }

    cached = _cache_get(src, target)
    if cached is not None:
        return {
            "ok": True,
            "text": src,
            "translation": cached,
            "target": target,
            "target_label": target_label(target),
            "cached": True,
            "error": None,
            "message": "",
        }

    if getattr(llm, "uses_mock", False):
        return _fail(
            target,
            "llm_not_configured",
            "翻译需要配置 LLM Key（右上角 ⚙ 个人设置），配置后即可使用",
            src,
        )

    lang = target_label(target)
    system = (
        "You are a precise translation engine inside a language-learning chat app. "
        f"Translate the user's message into {lang}. "
        "Output ONLY the translation itself: no quotes, no explanation, no original text. "
        "Preserve the original tone (casual / polite / joking) and keep names, places and brands. "
        "If the message is already in the target language, return it unchanged."
    )
    if context:
        system += f"\nChat context (for tone/reference only): {context[:300]}"

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": src},
    ]
    try:
        chunks: list[str] = []
        async for token in llm.stream_chat(messages, temperature=0.15, max_tokens=1200):
            if token:
                chunks.append(token)
        out = "".join(chunks).strip().strip('"').strip("“”").strip()
        if not out:
            return _fail(target, "empty_translation", "翻译返回为空，请重试", src)
        _cache_put(src, target, out)
        return {
            "ok": True,
            "text": src,
            "translation": out,
            "target": target,
            "target_label": target_label(target),
            "cached": False,
            "error": None,
            "message": "",
        }
    except Exception as exc:  # noqa: BLE001 - 翻译失败不能影响聊天
        log.warning("翻译失败（%s）: %s", type(exc).__name__, exc)
        name = type(exc).__name__
        if "Authentication" in name or "Auth" in name or "401" in str(exc):
            message = "翻译失败：LLM API Key 无效，请到 ⚙ 个人设置检查"
        elif "Timeout" in name or "timeout" in str(exc).lower():
            message = "翻译超时，请稍后重试"
        else:
            message = f"翻译失败：{exc}"
        return _fail(target, "translate_failed", message, src)
