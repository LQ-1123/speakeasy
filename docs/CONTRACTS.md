# 内部接口契约（Internal Contracts）

> 本文件是并行开发的**唯一权威接口定义**。改动任何签名，必须先改本文件并通知 Lead。
> 所有模块位于 `backend/app/`，包名 `app`，导入方式 `from app.xxx import ...`。

## 0. 全局约定

- Python 3.11，`from __future__ import annotations` 一律加上。
- 所有"引擎"（VAD/STT/TTS）**构造时不得加载权重**；权重在 `prewarm()` / 首次调用时懒加载。
  - 原因：没有 GPU/没下载模型时，`create_app()` 仍必须能启动，并用 mock 后端跑通链路。
- 音频内存表示：**16-bit signed PCM 小端，mono**，用 `bytes` 传递；浮点用 `numpy.ndarray[float32, -1.0..1.0]`。
- 输入采样率固定 **16000 Hz**（浏览器端已重采样）；TTS 输出固定 **24000 Hz**（CosyVoice2 / F5-TTS / Tone 均为 24k）。
  如某 TTS 实际输出不是 24k，**由 TTS 层负责重采样到 24k**，上层不做假设。
- 所有阻塞推理调用（faster-whisper / torch TTS）必须通过 `asyncio.to_thread` 或专用单线程 executor 执行，**不得阻塞事件循环**。
- 日志：`from app.logging_utils import get_logger` → `log = get_logger(__name__)`（Lead 提供）。

## 1. `app.voice.audio_utils`（owner: backend-voice）

```python
SR_IN: int = 16000
SR_OUT: int = 24000

def pcm16_to_float32(pcm: bytes) -> np.ndarray: ...        # -> float32, /32768, shape (-1,)
def float32_to_pcm16(x: np.ndarray) -> bytes: ...           # clip 到 [-1,1]
def resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray: ...
    # 纯 numpy 实现（窗函数 sinc 或线性插值 + 抗混叠）；不得依赖 scipy
def duration_s(pcm: bytes, sr: int = SR_IN) -> float: ...
def rms(x: np.ndarray) -> float: ...
def read_audio_file(path: str, target_sr: int = SR_IN) -> np.ndarray: ...
    # 支持 wav（soundfile 可选）与 flac/mp3（ffmpeg 子进程可选）；缺失时抛 AudioDecodeError
def write_wav(path: str, x: np.ndarray, sr: int) -> None: ...  # 仅用标准库 wave 模块写 16-bit PCM
def trim_silence(x: np.ndarray, sr: int, threshold: float = 0.01, pad_ms: int = 100) -> np.ndarray: ...
```

异常：`AudioDecodeError(RuntimeError)`。

## 2. `app.voice.vad`（owner: backend-voice）

流式 VAD，逐帧喂 PCM。**必须**在 silero 不可用时降级为能量 VAD（见 `mock` / `auto` 后端）。

```python
class VADEvent(str, Enum):
    SPEECH_START = "speech_start"
    SPEECH_END   = "speech_end"

@dataclass
class VADResult:
    event: VADEvent | None
    utterance: bytes | None   # 仅当 event==SPEECH_END 时为完整一句话 PCM16@16k（含 preroll 与尾音 pad）
    speech_ms: int            # 当前/刚结束语音段时长

class StreamingVAD:
    def __init__(self, cfg: VADSettings | None = None) -> None: ...
    def reset(self) -> None: ...
    def push(self, pcm16_frame: bytes) -> VADResult: ...
        # 帧长任意（内部按 512 samples 切块喂 silero）
        # 内部维护：preroll 环形缓冲（speech_pad_ms）、最小语音时长过滤、最大语音时长强制截断
    @property
    def is_speaking(self) -> bool: ...
    def flush(self) -> VADResult: ...   # 手动结束（用户点"停止"）：若正在说话则强制产出 utterance
```

`VADSettings` 来自 `app.config.settings.vad`（字段见 `config.py`：`threshold`、`min_silence_ms`、`min_speech_ms`、`speech_pad_ms`、`max_utterance_s`）。
实现要点：`min_speech_ms` 内的噪声段不触发 SPEECH_START（或触发后立即回滚）；`min_silence_ms` 静音后触发 SPEECH_END。

## 3. `app.voice.stt`（owner: backend-voice）

```python
@dataclass
class Transcript:
    text: str
    language: str            # "en"
    duration_s: float
    latency_ms: float
    backend: str             # "faster-whisper" | "mock"
    segments: list[dict]     # 可选，原始 segment 摘要

class STTEngine:
    def __init__(self, cfg: STTSettings | None = None) -> None: ...
    async def prewarm(self) -> None: ...
        # 懒加载模型（换到线程池）。失败时若 backend=="auto"，记录 warning 并降级 mock
    async def transcribe(self, pcm16: bytes, sample_rate: int = 16000) -> Transcript: ...
    @property
    def backend_name(self) -> str: ...
    async def aclose(self) -> None: ...
```

`backend == "mock"` 时返回 `settings.stt.mock_text`（默认 `"I'd like a flat white to go, please."`），并 `await asyncio.sleep(0.05)`。
`compute_type` 允许 `float16` / `int8_float16` / `int8`；CUDA 不可用时自动退回 `int8` + `cpu`。

## 4. `app.voice.tts`（owner: backend-voice）

```python
@dataclass
class AudioChunk:
    pcm16: bytes            # mono, sample_rate
    sample_rate: int        # 24000
    index: int              # 句子内递增
    is_first: bool
    is_last: bool           # 该句最后一个 chunk

@dataclass
class TTSSentenceRequest:
    text: str               # 已清洗、适合朗读的单句（无 markdown、无 emoji）
    persona_id: str
    sentence_index: int     # 本回合第几句，从 0 开始
    is_first_sentence: bool

class TTSEngine:
    def __init__(self, cfg: TTSSettings | None = None) -> None: ...
    async def prewarm(self, persona_ids: list[str] | None = None) -> None: ...
        # 加载模型 + 预加载参考音频 + 可选 warmup 合成
    async def synthesize(self, req: TTSSentenceRequest) -> AsyncIterator[AudioChunk]: ...
        # 异步生成器；内部串行化（同一时刻只允许一个推理在 GPU 上）
    @property
    def backend_name(self) -> str: ...
    async def aclose(self) -> None: ...
    def list_persona_voices(self) -> list[str]: ...
```

**TTS 层职责**：
- 从 `app.voice.ref_audio.ReferenceLibrary` 取 `persona_id → (ref_wav_path, ref_text, style)`。
- zero-shot 克隆：CosyVoice2 / F5-TTS；后端 `tone` 为无模型占位（生成可听音节的"含糊语音"），用于无 GPU 时的端到端联调。
- `text` 清洗：去掉 `*动作*`、emoji、markdown、括号旁白；数字/缩写做基础规范化（由 `normalize_for_tts(text)` 完成，**纯函数，可单测**）。
- 失败重试一次；仍失败则**降级为 tone 后端合成该句**并打 warning（绝不整轮失败）。

`normalize_for_tts(text: str) -> str` 与 `split_for_tts(text: str) -> list[str]`（兜底二次切分）也放在本模块，二者必须纯函数。

## 5. `app.voice.ref_audio`（owner: backend-voice）

```python
@dataclass(frozen=True)
class ReferenceVoice:
    persona_id: str
    ref_wav: str            # 绝对路径，16k 或 24k wav，5-10s
    ref_text: str           # 参考音频的逐字转写（zero-shot 必需）
    speed: float = 1.0
    description: str = ""

class ReferenceLibrary:
    def __init__(self, ref_dir: str, manifest_path: str | None = None) -> None: ...
    def get(self, persona_id: str) -> ReferenceVoice | None: ...
    def ensure(self, persona_id: str) -> ReferenceVoice: ...   # 缺失抛 ReferenceAudioMissing
    def all(self) -> list[ReferenceVoice]: ...
    def validate(self) -> list[str]:  # 返回问题列表（文件缺失/时长越界/无 ref_text）
```

数据集：`assets/ref_audio/manifest.json`（Lead 提供 schema 与占位文件），实际语音由用户用 `scripts/record_reference.py` 录制或用 `scripts/fetch_ref_audio.py` 下载。**代码必须在参考音频缺失时仍可运行**（tone/无克隆模式）。

## 6. `app.llm.client`（owner: backend-core）

```python
@dataclass
class ChatMessage:
    role: str   # "system" | "user" | "assistant"
    content: str

class LLMClient:
    def __init__(self, cfg: LLMSettings | None = None) -> None: ...
    async def stream_chat(self, messages: list[ChatMessage], *, temperature: float | None = None,
                          max_tokens: int | None = None) -> AsyncIterator[str]: ...
        # 产出增量文本 delta；首个 token 到达即 yield
    async def complete_json(self, messages: list[ChatMessage], *, schema_hint: str) -> str: ...
    async def aclose(self) -> None: ...
    def health(self) -> dict: ...   # {"configured": bool, "base_url": str, "model": str}
```

要求：OpenAI 兼容（`openai.AsyncOpenAI(base_url=..., api_key=...)`），**流式**，超时/重试/取消（`asyncio.CancelledError` 必须透传），401 时给出可读错误 `LLMConfigError`。

## 7. `app.llm.sentence_splitter`（owner: backend-core）

```python
class StreamingSentenceSplitter:
    def __init__(self, *, min_chars: int = 2, max_chars: int = 120,
                 first_chunk_max_chars: int = 60) -> None: ...
    def feed(self, delta: str) -> list[str]: ...   # 返回新完成的句子（可能 0..n 个）
    def flush(self) -> list[str]: ...              # 流结束时的残留
```

必须处理：`.!?…;:` 及换行；英文缩写（`Mr.` `Dr.` `e.g.` `i.e.` `U.S.` `etc.`）不切；小数 `3.5` 不切；`...`、引号 `"` 结尾；首个句子用更激进的阈值（遇到 `,` 且长度 ≥ `first_chunk_max_chars` 也切，用于压低首包延迟）；`max_chars` 兜底强切（优先在空格处）。

## 8. `app.llm.suggestions`（owner: backend-core）

```python
@dataclass
class Suggestion:
    original: str
    better: str
    why: str
    register: str      # "neutral" | "casual" | "formal"

class SuggestionService:
    def __init__(self, llm: LLMClient, personas: PersonaLibrary) -> None: ...
    async def suggest(self, user_text: str, *, persona_id: str,
                      history: list[ChatMessage] | None = None) -> list[Suggestion]: ...
```

要求：**异步后台任务**调用（绝不阻塞 TTS/音频流）；失败静默返回 `[]` 并 debug 日志；LLM 返回 JSON 数组，需容错解析（strip ```json 围栏）。

## 9. `app.pipeline.orchestrator`（owner: backend-core）

```python
SendFn = Callable[[dict], Awaitable[None]]      # 往 WS 推一条 JSON 消息

class TurnOrchestrator:
    def __init__(self, *, llm: LLMClient, tts: TTSEngine, personas: PersonaLibrary,
                 suggestions: SuggestionService | None = None,
                 cfg: PipelineSettings | None = None) -> None: ...
    async def run_turn(self, *, session: "Session", user_text: str, send: SendFn) -> None:
        # 1) 追加 user 消息 → 2) 构造 messages（persona system prompt + 历史裁剪）
        # 3) llm.stream_chat 流式 → splitter → 每句 asyncio.create_task 送 TTS（保序队列）
        # 4) 音频 chunk 逐条 send({"type":"audio", ...})   5) assistant_done + metrics
    def cancel(self) -> None: ...     # barge-in：取消当前 LLM/TTS 任务
```

**保序并发**：句子 i 的 TTS 可在句子 i-1 合成时并行**排队**，但音频必须按 `sentence_index` 顺序上线（用 `asyncio.Queue` + 单消费协程）。
`session` 定义见 `app/session.py`（owner: backend-core）：

```python
@dataclass
class Session:
    session_id: str
    persona_id: str
    history: list[ChatMessage]
    created_at: float
    last_user_text: str | None
    def add_user(self, text: str) -> None: ...
    def add_assistant(self, text: str) -> None: ...
    def trimmed(self, max_turns: int) -> list[ChatMessage]: ...
```

## 10. `app.routers.ws` / `app.routers.http_api`（owner: backend-core）

- `ws.py`：`router = APIRouter()`，`@router.websocket("/ws")`。**消息协议见 `docs/PROTOCOL.md`（权威）**。
  握手时把 `session`、`StreamingVAD`、`TurnOrchestrator` 组合起来；接收循环必须能同时处理二进制音频帧与 JSON 文本帧。
- `http_api.py`：`router = APIRouter()`，提供
  - `GET /api/personas` → `{"personas":[PersonaPublic...]}`
  - `GET /api/config` → 非敏感运行配置（后端名、模型名、是否配置了 LLM key）
  - `GET /api/health` → 各引擎状态
  - `POST /api/echo`（调试）：`{"text":str,"persona_id":str}` → 返回 `{"sentences":[...], "llm_preview": str}`（不合成音频）
  - `GET /api/metrics/last` → 最近一回合的延迟分解
- 依赖注入：通过 `request.app.state.engines`（Lead 在 `main.py` 里装配），**禁止**在 router 内直接 `new` 重量级引擎。

## 11. `app.engines`（owner: Lead）

```python
@dataclass
class Engines:
    stt: STTEngine
    tts: TTSEngine
    llm: LLMClient
    personas: PersonaLibrary
    suggestions: SuggestionService

def build_engines(settings: Settings) -> Engines: ...
```

## 12. 谁拥有哪些文件

| 范围 | Owner |
|---|---|
| `backend/app/voice/**`、`backend/tests/test_voice_*.py` | backend-voice |
| `backend/app/llm/**`、`backend/app/pipeline/**`、`backend/app/session.py`、`backend/app/routers/**`、`backend/tests/test_core_*.py` | backend-core |
| `frontend/**`、`backend/app/static/**`、`scripts/build_frontend.*` | frontend |
| `backend/app/{config,schemas,engines,main,logging_utils,personas}*`、`docs/**`、`scripts/{setup,download,record,smoke}*`、`requirements*.txt`、`pyproject.toml`、`README.md`、`backend/app/static/standalone.html` | Lead |

新增文件请落在自己的目录内；跨目录改动先在团队消息里说一声。
