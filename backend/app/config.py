"""统一配置层。

优先级：环境变量 > .env 文件 > 代码默认值。
所有引擎的配置都从 ``get_settings()`` 取，禁止在模块里散落硬编码。

用法::

    from app.config import get_settings
    cfg = get_settings()
    cfg.stt.model_size
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ASSETS_DIR = os.path.join(PROJECT_ROOT, "assets")
REF_AUDIO_DIR = os.path.join(ASSETS_DIR, "ref_audio")
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
STATIC_DIR = os.path.join(PROJECT_ROOT, "backend", "app", "static")


def _abs(path: str, base: str = PROJECT_ROOT) -> str:
    if not path:
        return base
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(base, path))


# --------------------------------------------------------------------------- #
# 子系统配置
# --------------------------------------------------------------------------- #
class VADSettings(BaseModel):
    """Silero-VAD 流式参数（毫秒级）。"""

    backend: Literal["auto", "silero", "energy", "mock"] = "auto"
    threshold: float = 0.5
    min_silence_ms: int = 550          # 判定"说完了"的静音时长
    min_speech_ms: int = 200           # 短于此视为噪声，丢弃
    speech_pad_ms: int = 200           # 前后各留的余量，避免吃字
    max_utterance_s: float = 30.0      # 单句最长，超时强制截断送 STT
    window_size_samples: int = 512     # silero v5 要求 512 @16k
    # energy 后端参数（silero 不可用时的降级实现）
    energy_threshold: float = 0.012
    energy_abs_floor: float = 0.0


class STTSettings(BaseModel):
    backend: Literal["auto", "faster-whisper", "mock"] = "auto"
    model_size: str = "distil-large-v3"        # 或 "large-v3" / "Systran/faster-distil-whisper-large-v3"
    device: str = "cuda"
    compute_type: str = "int8_float16"         # RTX 5080 只有 16G，int8_float16 更稳
    language: str = "en"
    beam_size: int = 1
    best_of: int = 1
    vad_filter: bool = False                   # 上游已有 VAD，这里关掉省时间
    condition_on_previous_text: bool = False   # 关闭以降低幻听
    without_timestamps: bool = True
    num_workers: int = 1
    cpu_fallback: bool = True
    download_root: str = ""                    # 空 = 用 HF 缓存
    local_files_only: bool = False
    initial_prompt: str = (
        "Casual English conversation. Common fillers: well, right, yeah, I mean."
    )
    hotwords: str = ""
    mock_text: str = "I'd like a flat white to go, please."
    mock_latency_ms: int = 60
    # ---- 多语言本地转写（distil-large-v3 是英语专用；ja/ko 等用多语言小模型）----
    multilingual_model: str = "small"         # small / medium / large-v3；按需懒加载
    call_model: str = "small"                 # 通话专用快速模型（延迟优先，默认 small）
    # ---- 云端 STT（SiliconFlow SenseVoiceSmall；可在个人设置里热切换）----
    provider: str = "auto"                    # auto | local | siliconflow
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    siliconflow_model: str = "FunAudioLLM/SenseVoiceSmall"
    siliconflow_api_key: str = ""
    siliconflow_timeout_s: float = 30.0


class TTSSettings(BaseModel):
    backend: Literal["auto", "edge", "xtts", "cosyvoice2", "f5tts", "tone", "mock"] = "auto"
    sample_rate: int = 24000
    device: str = "cuda"
    dtype: Literal["float16", "float32", "bfloat16"] = "float16"
    # CosyVoice2
    cosyvoice_model_dir: str = "pretrained_models/CosyVoice2-0.5B"
    cosyvoice_repo_dir: str = "third_party/CosyVoice"
    load_jit: bool = False
    load_trt: bool = False
    load_vllm: bool = False
    fp16: bool = True
    # F5-TTS
    f5_model: str = "F5TTS_Base"
    f5_repo_dir: str = "third_party/F5-TTS"
    f5_ckpt_file: str = ""
    # 通用
    speed: float = 1.0
    nfe_step: int = 24                 # F5-TTS 采样步数：24 是延迟/质量甜点
    max_chars_per_sentence: int = 220
    prewarm: bool = True
    fallback_to_tone: bool = True      # 克隆失败时降级，保证链路不断
    tone_fallback_enabled: bool = True


class LLMSettings(BaseModel):
    """OpenAI 兼容协议（DeepSeek / OpenAI / 任何兼容网关）。"""

    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model: str = "deepseek-chat"
    temperature: float = 0.85
    max_tokens: int = 1600
    top_p: float = 0.95
    timeout_s: float = 30.0
    max_retries: int = 1
    stream: bool = True
    # 建议服务用的小模型（可指向同一端点），留空则复用主模型
    suggestion_model: str = ""
    suggestion_temperature: float = 0.4
    suggestion_max_tokens: int = 800
    suggestion_timeout_s: float = 20.0
    extra_headers: dict[str, str] = Field(default_factory=dict)
    # 调试/离线：不调用真实 API，用本地规则生成回复（便于无 key 演示)
    mock_when_missing_key: bool = True


class PipelineSettings(BaseModel):
    max_history_turns: int = 8
    sentence_min_chars: int = 2
    sentence_max_chars: int = 120
    first_sentence_max_chars: int = 60
    tts_concurrency: int = 1           # 单卡：串行合成，保证延迟稳定
    jitter_queue_max_sentences: int = 6
    audio_chunk_ms: int = 200          # 每个 audio 帧包含的音频时长
    suggestions_enabled: bool = True
    suggestions_auto: bool = True      # 每轮结束自动异步推送
    barge_in_enabled: bool = True


class ServerSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    log_level: str = "INFO"
    static_dir: str = STATIC_DIR
    serve_frontend: bool = True


class MetricsSettings(BaseModel):
    enabled: bool = True
    keep_last: int = 50


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- 顶层环境变量（扁平，便于 .env 书写）----
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    hf_endpoint: str = ""          # 例如 https://hf-mirror.com
    hf_home: str = ""
    torch_home: str = ""
    data_dir: str = ""
    jpush_app_key: str = ""
    jpush_master_secret: str = ""
    ref_audio_dir: str = ""
    # 云端 STT：扁平 env（pydantic-settings 能直接读到；嵌套 STTSettings 字段读不到）
    stt_provider: str = ""
    stt_siliconflow_api_key: str = ""
    stt_siliconflow_base_url: str = ""
    stt_siliconflow_model: str = ""
    stt_siliconflow_timeout_s: float | None = None
    stt_multilingual_model: str = ""
    stt_call_model: str = ""

    server: ServerSettings = Field(default_factory=ServerSettings)
    vad: VADSettings = Field(default_factory=VADSettings)
    stt: STTSettings = Field(default_factory=STTSettings)
    tts: TTSSettings = Field(default_factory=TTSSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    pipeline: PipelineSettings = Field(default_factory=PipelineSettings)
    metrics: MetricsSettings = Field(default_factory=MetricsSettings)

    @field_validator("server", mode="after")
    @classmethod
    def _noop(cls, v):  # pragma: no cover - 占位，保持 pydantic 行为稳定
        return v

    def apply_env_overrides(self) -> "Settings":
        """把扁平环境变量并入子配置（env 优先）。"""
        if self.llm_base_url:
            self.llm.base_url = self.llm_base_url
        if self.llm_api_key:
            self.llm.api_key = self.llm_api_key
        if self.llm_model:
            self.llm.model = self.llm_model
        # 常见别名
        for alias, target in (
            ("OPENAI_BASE_URL", "base_url"),
            ("OPENAI_API_KEY", "api_key"),
            ("OPENAI_MODEL", "model"),
            ("DEEPSEEK_API_KEY", "api_key"),
        ):
            val = os.getenv(alias)
            if val:
                setattr(self.llm, target, val)
        if os.getenv("API_KEY") and not self.llm.api_key:
            self.llm.api_key = os.environ["API_KEY"]
        # 云端 STT（.env 扁平项 → STTSettings）
        if self.stt_provider:
            self.stt.provider = self.stt_provider
        if self.stt_siliconflow_api_key:
            self.stt.siliconflow_api_key = self.stt_siliconflow_api_key
        if self.stt_siliconflow_base_url:
            self.stt.siliconflow_base_url = self.stt_siliconflow_base_url
        if self.stt_siliconflow_model:
            self.stt.siliconflow_model = self.stt_siliconflow_model
        if self.stt_siliconflow_timeout_s is not None:
            self.stt.siliconflow_timeout_s = float(self.stt_siliconflow_timeout_s)
        if self.stt_multilingual_model:
            self.stt.multilingual_model = self.stt_multilingual_model
        if self.stt_call_model:
            self.stt.call_model = self.stt_call_model
        return self

    # ---- 路径 ----
    @property
    def data_path(self) -> str:
        return _abs(self.data_dir or DATA_DIR)

    @property
    def ref_audio_path(self) -> str:
        return _abs(self.ref_audio_dir or REF_AUDIO_DIR)

    @property
    def personas_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "personas", "personas.json")

    def apply_hf_env(self) -> None:
        """HF 下载配置必须在 import transformers/huggingface_hub 之前生效。"""
        if self.hf_endpoint:
            os.environ.setdefault("HF_ENDPOINT", self.hf_endpoint)
        if self.hf_home:
            os.environ.setdefault("HF_HOME", _abs(self.hf_home))
        if self.torch_home:
            os.environ.setdefault("TORCH_HOME", _abs(self.torch_home))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.apply_env_overrides()
    s.apply_hf_env()
    os.makedirs(s.data_path, exist_ok=True)
    return s


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()
