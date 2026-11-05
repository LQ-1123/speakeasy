"""LLM 客户端（OpenAI 兼容协议，流式）。

对外契约见 ``docs/CONTRACTS.md`` 第 6 节。

关键行为
--------
- **无 key 自动降级**：``cfg.api_key`` 为空且 ``cfg.mock_when_missing_key`` 为真时，
  ``stream_chat`` / ``complete_json`` 走 :mod:`app.llm.mock_llm` 的本地规则后端，
  整条链路（断句 → TTS → 播报）在无 key 环境下仍然可跑通。warning 只打一次。
- **取消透传**：``asyncio.CancelledError`` 永远原样抛出，barge-in 才能生效。
- **错误分类**：401 → :class:`LLMConfigError`；连接失败 → :class:`LLMConfigError`（带 base_url）；
  超时 → 重试后抛 :class:`LLMError`。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator, Sequence

import openai

from app.config import LLMSettings, get_settings
from app.llm import mock_llm
from app.logging_utils import get_logger

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# 数据类型与异常
# --------------------------------------------------------------------------- #
@dataclass
class ChatMessage:
    """一条对话消息（OpenAI 兼容的最小结构）。

    ``images`` 是 data URL 列表（``data:image/jpeg;base64,...``）。
    有图时 ``content`` 会展开成 OpenAI 的多模态 parts 形式；
    没图时保持纯字符串，兼容所有纯文本网关。
    """

    role: str  # "system" | "user" | "assistant"
    content: str
    images: tuple[str, ...] = ()

    def to_openai(self) -> dict[str, Any]:
        if not self.images:
            return {"role": self.role, "content": self.content}
        parts: list[dict[str, Any]] = []
        if self.content:
            parts.append({"type": "text", "text": self.content})
        for url in self.images:
            parts.append({"type": "image_url", "image_url": {"url": url}})
        return {"role": self.role, "content": parts}

    def to_dict(self) -> dict[str, Any]:
        if not self.images:
            return {"role": self.role, "content": self.content}
        # 对外序列化（日志/建议器）用占位符，不把 base64 抖出去
        return {
            "role": self.role,
            "content": self.content,
            "images": len(self.images),
        }


class LLMError(RuntimeError):
    """LLM 调用失败（可上报为 ``llm_failed``）。"""


class LLMConfigError(LLMError):
    """LLM 未配置 / 配置无效（可上报为 ``llm_not_configured``）。"""


#: 明确表示"没配 key"的粗筛（用于 health 与错误提示）
_PLACEHOLDER_KEYS = {"", "none", "null", "changeme", "your-api-key", "sk-xxx"}


def _is_placeholder_key(key: str | None) -> bool:
    k = (key or "").strip()
    return k.lower() in _PLACEHOLDER_KEYS


# --------------------------------------------------------------------------- #
# JSON 提取工具（complete_json 的兜底解析，测试也直接用它）
# --------------------------------------------------------------------------- #
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def strip_code_fence(text: str) -> str:
    """剥离 ```json ... ``` 围栏；没有围栏时返回原文。"""
    if not text:
        return ""
    m = _FENCE_RE.search(text)
    return m.group(1).strip() if m else text.strip()


def extract_json(text: str) -> Any:
    """从任意文本里抠出 JSON（对象或数组）。

    顺序：整体解析 → 去围栏 → 从第一个 ``{``/``[`` 到最后一个 ``}``/``]`` 切片。
    全部失败抛 :class:`LLMError`。
    """
    raw = (text or "").strip()
    if not raw:
        raise LLMError("LLM 返回为空")

    for candidate in (raw, strip_code_fence(raw)):
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            pass

    cleaned = strip_code_fence(raw)
    starts = [i for i in (cleaned.find("{"), cleaned.find("[")) if i >= 0]
    if starts:
        start = min(starts)
        ends = [i for i in (cleaned.rfind("}"), cleaned.rfind("]")) if i >= start]
        if ends:
            end = max(ends)
            chunk = cleaned[start : end + 1]
            try:
                return json.loads(chunk)
            except json.JSONDecodeError:
                # 再试一次：去掉行尾逗号这类小毛病
                repaired = re.sub(r",(\s*[}\]])", r"\1", chunk)
                try:
                    return json.loads(repaired)
                except json.JSONDecodeError:
                    pass
    raise LLMError(f"无法从 LLM 输出中解析 JSON（前 200 字符: {raw[:200]!r}）")


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
class LLMClient:
    """OpenAI 兼容的异步 LLM 客户端（流式 + JSON 补全）。"""

    #: 重试前的退避基数（秒），指数增长；测试可调小。
    retry_backoff_s: float = 0.35

    def __init__(self, cfg: LLMSettings | None = None) -> None:
        self.cfg: LLMSettings = cfg or get_settings().llm
        self._mock_warned = False
        self._client: openai.AsyncOpenAI | None = None
        if not self.configured:
            if self.cfg.mock_when_missing_key:
                log.warning(
                    "LLM_API_KEY 未配置 → 使用本地规则回复（mock 后端）。"
                    "在 .env 里设置 LLM_API_KEY 即可切换到 %s",
                    self.cfg.model,
                )
                self._mock_warned = True
            else:
                log.warning("LLM_API_KEY 未配置，且 mock_when_missing_key=False；对话将报错")

    # ---------------- 状态 ----------------
    @property
    def configured(self) -> bool:
        """是否配置了可用的 API key。"""
        return not _is_placeholder_key(self.cfg.api_key)

    def health(self) -> dict[str, Any]:
        """健康状态（绝不抛异常，``/api/health`` 与 WS ``ready`` 会用）。"""
        return {
            "configured": self.configured,
            "base_url": self.cfg.base_url,
            "model": self.cfg.model,
            "mock": self.uses_mock,
        }

    @property
    def uses_mock(self) -> bool:
        """本轮是否走本地规则后端。"""
        return (not self.configured) and bool(self.cfg.mock_when_missing_key)

    # ---------------- 客户端复用 ----------------
    def _ensure_client(self) -> openai.AsyncOpenAI:
        if self._client is None:
            self._client = openai.AsyncOpenAI(
                base_url=self.cfg.base_url,
                api_key=self.cfg.api_key,
                timeout=self.cfg.timeout_s,
                max_retries=0,  # 重试策略我们自己控制，见 _with_retries
                default_headers=dict(self.cfg.extra_headers) or None,
            )
        return self._client

    # ---------------- 流式对话 ----------------
    async def stream_chat(
        self,
        messages: Sequence[ChatMessage] | Sequence[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """流式产出增量文本。首个 token 到达即 yield。"""
        payload = _normalize_messages(messages)

        if self.uses_mock:
            async for token in mock_llm.mock_stream_chat(payload):
                yield token
            return

        if not self.configured:
            raise LLMConfigError(
                "未配置 LLM_API_KEY，请在 .env 中设置 LLM_API_KEY（或把 LLM_MOCK_WHEN_MISSING_KEY 设为 true 使用本地规则回复）"
            )

        client = self._ensure_client()
        kwargs: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": payload,
            "stream": True,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
            "top_p": self.cfg.top_p,
        }
        if extra:
            _eb = dict(kwargs.get("extra_body") or {})
            _eb.update(extra)
            kwargs["extra_body"] = _eb
        if self.cfg.extra_headers:
            kwargs["extra_headers"] = dict(self.cfg.extra_headers)

        attempts = max(0, int(self.cfg.max_retries)) + 1
        last_exc: BaseException | None = None
        for attempt in range(1, attempts + 1):
            emitted = False
            try:
                stream = await client.chat.completions.create(**kwargs)
                try:
                    async for chunk in stream:
                        for delta_text in _iter_delta_text(chunk):
                            emitted = True
                            yield delta_text
                finally:
                    # 生成器被提前关闭（barge-in）时尽量释放底层连接
                    closer = getattr(stream, "close", None)
                    if closer is not None:
                        try:
                            await closer()
                        except Exception as exc:  # pragma: no cover - 清理失败不致命
                            log.debug("closing llm stream failed: %s", exc)
                return
            except asyncio.CancelledError:
                raise  # 取消必须透传
            except openai.AuthenticationError as exc:
                raise LLMConfigError(
                    f"API key 无效或未配置（base_url={self.cfg.base_url}）。"
                    "请检查 .env 中的 LLM_API_KEY / LLM_BASE_URL 是否匹配。"
                ) from exc
            except openai.PermissionDeniedError as exc:
                raise LLMConfigError(
                    f"API key 无权访问 model={self.cfg.model}（base_url={self.cfg.base_url}）"
                ) from exc
            except openai.NotFoundError as exc:
                raise LLMConfigError(
                    f"模型或端点不存在：model={self.cfg.model} base_url={self.cfg.base_url}"
                ) from exc
            except openai.APIConnectionError as exc:
                last_exc = exc
                log.warning(
                    "LLM 连接失败（第 %d/%d 次）: base_url=%s err=%s",
                    attempt, attempts, self.cfg.base_url, exc,
                )
                if attempt >= attempts:
                    raise LLMConfigError(
                        f"无法连接 LLM 服务 {self.cfg.base_url}（{type(exc).__name__}: {exc}）。"
                        "检查网络/代理或 base_url 是否正确。"
                    ) from exc
            except openai.APITimeoutError as exc:
                last_exc = exc
                log.warning("LLM 超时（第 %d/%d 次）: %.1fs", attempt, attempts, self.cfg.timeout_s)
                if attempt >= attempts:
                    raise LLMError(
                        f"LLM 超时（{self.cfg.timeout_s:.0f}s，重试 {attempts - 1} 次后放弃）"
                    ) from exc
            except openai.APIStatusError as exc:
                status = getattr(exc, "status_code", None)
                if status in (401, 403):
                    raise LLMConfigError(
                        f"LLM 鉴权失败（HTTP {status}），请检查 LLM_API_KEY"
                    ) from exc
                if emitted:
                    raise LLMError(f"LLM 流中断（HTTP {status}）: {exc}") from exc
                last_exc = exc
                log.warning("LLM HTTP %s（第 %d/%d 次）: %s", status, attempt, attempts, exc)
                if attempt >= attempts:
                    raise LLMError(f"LLM 调用失败（HTTP {status}）: {exc}") from exc
                if status is not None and status < 500:
                    raise LLMError(f"LLM 调用失败（HTTP {status}）: {exc}") from exc

            # 重试前退避（指数），但必须可被 cancel
            if attempt < attempts:
                await asyncio.sleep(self.retry_backoff_s * (2 ** (attempt - 1)))

        raise LLMError(f"LLM 调用失败: {last_exc}")  # pragma: no cover - 循环必返回/抛出

    # ---------------- JSON 补全 ----------------
    async def complete_json(
        self,
        messages: Sequence[ChatMessage] | Sequence[dict[str, str]],
        *,
        schema_hint: str = "",
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout_s: float | None = None,
    ) -> str:
        """非流式 JSON 补全；返回**原始字符串**（由调用方 ``extract_json`` 解析）。"""
        payload = _normalize_messages(messages)

        if self.uses_mock:
            return await mock_llm.mock_complete_json(payload, schema_hint=schema_hint)

        if not self.configured:
            raise LLMConfigError("未配置 LLM_API_KEY，无法调用 complete_json")

        client = self._ensure_client()
        use_model = model or self.cfg.suggestion_model or self.cfg.model
        base_kwargs: dict[str, Any] = {
            "model": use_model,
            "messages": payload,
            "temperature": self.cfg.suggestion_temperature if temperature is None else temperature,
            "max_tokens": self.cfg.suggestion_max_tokens if max_tokens is None else max_tokens,
        }
        if timeout_s is not None:
            base_kwargs["timeout"] = timeout_s

        # 先尝试 JSON mode；网关不支持时退回普通调用
        try:
            resp = await client.chat.completions.create(
                **base_kwargs, response_format={"type": "json_object"}
            )
        except asyncio.CancelledError:
            raise
        except openai.BadRequestError as exc:
            log.debug("response_format=json_object 不被支持，退回普通调用: %s", exc)
            try:
                resp = await client.chat.completions.create(**base_kwargs)
            except asyncio.CancelledError:
                raise
            except openai.AuthenticationError as exc2:
                raise LLMConfigError("API key 无效或未配置") from exc2
            except openai.APIError as exc2:
                raise LLMError(f"LLM JSON 补全失败: {exc2}") from exc2
        except openai.AuthenticationError as exc:
            raise LLMConfigError("API key 无效或未配置") from exc
        except openai.APIConnectionError as exc:
            raise LLMConfigError(f"无法连接 LLM 服务 {self.cfg.base_url}") from exc
        except openai.APITimeoutError as exc:
            raise LLMError("LLM JSON 补全超时") from exc
        except openai.APIStatusError as exc:
            raise LLMError(f"LLM JSON 补全失败（HTTP {getattr(exc, 'status_code', '?')}）") from exc

        try:
            choices = resp.choices
            content = choices[0].message.content if choices else None
        except (AttributeError, IndexError):  # pragma: no cover - 防御不同网关
            content = None
        return content or ""

    # ---------------- 关闭 ----------------
    async def aclose(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            await client.close()
        except Exception as exc:  # pragma: no cover - 关闭失败不致命
            log.debug("closing llm client failed: %s", exc)


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _normalize_messages(
    messages: Sequence[ChatMessage] | Sequence[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """统一成 OpenAI 需要的 ``[{"role","content"}]``。

    纯文本消息的 ``content`` 保持字符串（兼容性最好）；
    带图片的消息 ``content`` 是 parts 列表（多模态）。
    """
    out: list[dict[str, Any]] = []
    for m in messages or ():
        if isinstance(m, ChatMessage):
            out.append(m.to_openai())
        elif isinstance(m, dict):
            content = m.get("content", "")
            images = m.get("images") or ()
            if images and not isinstance(content, (list, tuple)):
                parts: list[dict[str, Any]] = []
                if content:
                    parts.append({"type": "text", "text": str(content)})
                for url in images:
                    parts.append({"type": "image_url", "image_url": {"url": str(url)}})
                out.append({"role": str(m.get("role", "user")), "content": parts})
            else:
                out.append(
                    {
                        "role": str(m.get("role", "user")),
                        "content": content if isinstance(content, (list, tuple)) else str(content),
                    }
                )
        else:  # 鸭子类型（测试里的 fake message）
            out.append(
                {
                    "role": str(getattr(m, "role", "user")),
                    "content": str(getattr(m, "content", "")),
                }
            )
    return out


def _iter_delta_text(chunk: Any) -> list[str]:
    """从一个流式 chunk 里取出所有非空的 delta 文本。"""
    try:
        choices = chunk.choices
    except AttributeError:  # pragma: no cover - 防御异常网关
        return []
    if not choices:
        return []
    out: list[str] = []
    for choice in choices:
        delta = getattr(choice, "delta", None)
        if delta is None:
            continue
        content = getattr(delta, "content", None)
        if content:
            out.append(content)
    return out


__all__ = [
    "ChatMessage",
    "LLMClient",
    "LLMConfigError",
    "LLMError",
    "extract_json",
    "strip_code_fence",
]
