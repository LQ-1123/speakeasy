"""LLM 层：OpenAI 兼容客户端、流式断句、润色建议、离线规则后端。

对外导出（契约见 ``docs/CONTRACTS.md`` 第 6–8 节）::

    from app.llm import ChatMessage, LLMClient, LLMConfigError, LLMError
    from app.llm import Suggestion, SuggestionService
    from app.llm import StreamingSentenceSplitter
"""

from __future__ import annotations

from app.llm.client import (
    ChatMessage,
    LLMClient,
    LLMConfigError,
    LLMError,
    extract_json,
    strip_code_fence,
)
from app.llm.sentence_splitter import StreamingSentenceSplitter, split_all
from app.llm.suggestions import Suggestion, SuggestionService, parse_suggestions

__all__ = [
    "ChatMessage",
    "LLMClient",
    "LLMConfigError",
    "LLMError",
    "Suggestion",
    "SuggestionService",
    "StreamingSentenceSplitter",
    "extract_json",
    "parse_suggestions",
    "split_all",
    "strip_code_fence",
]
