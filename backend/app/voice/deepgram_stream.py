"""Deepgram 流式 STT 客户端封装（供通话 WebSocket 转发使用）。

设计：
- 连接由调用方（后端 WS 端点）创建，负责把手机 PCM 原始帧转发给 Deepgram；
- 事件（partial / final / metadata）通过 async generator 交回；
- 未配置 DEEPGRAM_API_KEY 时抛出 StreamUnavailable，调用方回退到整段上传 STT。
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator

import websockets

DEFAULT_WS = "wss://api.deepgram.com/v1/listen"

# Deepgram 支持的语言码基本与 BCP-47 前两位一致，这里做一次兜底映射
_LANG_MAP = {
    "en": "en",
    "zh": "zh",
    "ja": "ja",
    "ko": "ko",
    "de": "de",
    "fr": "fr",
    "es": "es",
    "it": "it",
    "pt": "pt",
    "ru": "ru",
    "ar": "ar",
    "hi": "hi",
}


class StreamUnavailable(RuntimeError):
    """Deepgram 流式不可用（未配置 key / 连接失败）。"""


def deepgram_key() -> str:
    return (os.environ.get("DEEPGRAM_API_KEY") or "").strip()


def language_code(lang: str | None) -> str:
    code = (lang or "en").strip().lower()[:2]
    return _LANG_MAP.get(code, "en")


class DeepgramStreamer:
    def __init__(self, api_key: str, language: str = "en", *, model: str = "nova-3") -> None:
        self.api_key = api_key
        self.language = language_code(language)
        self.model = model
        self._ws: Any = None
        self.final_text = ""

    async def connect(self) -> None:
        url = (
            f"{DEFAULT_WS}?model={self.model}&language={self.language}"
            "&encoding=linear16&sample_rate=16000&channels=1"
            "&interim_results=true&smart_format=true&punctuate=true"
            "&endpointing=150&vad_events=true&utterance_end_ms=1200"
        )
        headers = {"Authorization": f"Token {self.api_key}"}
        try:
            # websockets >=15 会自动读取 HTTPS_PROXY/ALL_PROXY 环境变量
            self._ws = await websockets.connect(url, additional_headers=headers, max_size=None)
        except Exception as exc:  # noqa: BLE001 - 统一转成可回退错误
            raise StreamUnavailable(str(exc)) from exc

    async def send(self, pcm: bytes) -> None:
        if self._ws is None:
            raise StreamUnavailable("not connected")
        await self._ws.send(pcm)

    async def finalize(self) -> None:
        if self._ws is None:
            return
        await self._ws.send(json.dumps({"type": "Finalize"}))

    async def close(self) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps({"type": "CloseStream"}))
        except Exception:  # noqa: BLE001
            pass
        try:
            await self._ws.close()
        except Exception:  # noqa: BLE001
            pass
        self._ws = None

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """把 Deepgram 原始消息归一化成 {type: partial|final|metadata|error}。"""
        if self._ws is None:
            return
        async for raw in self._ws:
            try:
                msg = json.loads(raw)
            except Exception:  # noqa: BLE001 - 非 JSON 忽略
                continue
            kind = msg.get("type")
            if kind == "Results":
                alt = (msg.get("channel") or {}).get("alternatives") or [{}]
                text = (alt[0].get("transcript") or "").strip()
                if not text:
                    # 空结果也通知一次 metadata 进度
                    if msg.get("is_final"):
                        yield {"type": "final", "text": ""}
                    continue
                if msg.get("is_final"):
                    self.final_text = text
                    yield {"type": "final", "text": text}
                else:
                    yield {"type": "partial", "text": text}
            elif kind == "UtteranceEnd":
                yield {"type": "utterance_end"}
            elif kind == "Metadata":
                yield {"type": "metadata"}
            elif kind == "Error":
                yield {"type": "error", "message": str(msg.get("message") or "deepgram error")}
