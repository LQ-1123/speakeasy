"""HTTP 请求/响应模型（Pydantic v2）。WebSocket 消息不走这里，见 docs/PROTOCOL.md。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# 通用
# --------------------------------------------------------------------------- #
class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "loading"] = "ok"
    version: str = "0.1.0"
    engines: dict[str, Any] = Field(default_factory=dict)
    llm: dict[str, Any] = Field(default_factory=dict)
    gpu: dict[str, Any] = Field(default_factory=dict)


class PersonaPublic(BaseModel):
    id: str
    name: str
    title: str = ""
    scene: str = ""
    accent: str = ""
    accent_short: str = ""
    flag: str = "🌍"
    city: str = ""
    difficulty: str = "medium"
    tags: list[str] = Field(default_factory=list)
    opening_line: str = ""
    traits: list[str] = Field(default_factory=list)
    voice_notes: str = ""
    has_ref_audio: bool = False


class ModePublic(BaseModel):
    id: str
    name: str
    description: str = ""


class PersonasResponse(BaseModel):
    personas: list[PersonaPublic]
    modes: list[ModePublic] = Field(default_factory=list)
    default_persona_id: str = ""


class AudioConfig(BaseModel):
    format: str = "pcm_s16le"
    input_sample_rate: int = 16000
    sample_rate: int = 24000
    channels: int = 1


class ConfigResponse(BaseModel):
    protocol_version: int = 1
    audio: AudioConfig = Field(default_factory=AudioConfig)
    engines: dict[str, Any] = Field(default_factory=dict)
    llm: dict[str, Any] = Field(default_factory=dict)
    features: dict[str, bool] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 调试 / 工具端点
# --------------------------------------------------------------------------- #
class EchoRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    persona_id: str | None = None
    mode_id: str | None = None


class EchoResponse(BaseModel):
    sentences: list[str]
    reply_preview: str
    system_prompt_chars: int = 0
    latency_ms: float = 0.0


class SuggestionItem(BaseModel):
    original: str
    better: str
    why: str = ""
    register: str = "neutral"


class SuggestRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    persona_id: str | None = None


class SuggestResponse(BaseModel):
    items: list[SuggestionItem] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    detail: str
    code: str = "error"
