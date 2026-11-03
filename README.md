# SpeakEasy · 英语口语对练

> ## 🆕 SpeakEasy 2.0：移动端拟真跨国语伴（PWA）
> 1.0 的实时语音能力之上，新增 **SQLite 持久层 + 随机捏人 + 双层长期记忆 + 熟稔度社交 + 朋友圈 + 移动端三 Tab IM**。
> 按微信式**异步语音条**练口语：捏一个外国朋友、听他当地时间的语音、顺手把表达改地道。
>
> - 运行/验收：**[docs/V2_GUIDE.md](docs/V2_GUIDE.md)** · 接口：**[docs/V2_API.md](docs/V2_API.md)**
> - 启动：`HF_HOME="$PWD/.cache/hf" .venv/bin/python -m uvicorn app.main:app --app-dir backend --port 8000` → <http://127.0.0.1:8000>
> - 前端：`cd frontend && npm install && npm run build:backend`；PWA 可"添加到主屏幕"
> - 不填 LLM Key 也能完整演示（规则引擎 + edge-tts + 本地 STT）；填 Key 后自动切真实模型
>
> 下面 1.0 的实时语音 / 本地克隆 / GPU 说明仍然有效（`/classic` 页面与 `/ws` 通道保留）。

> 本地语音栈（Silero-VAD + faster-whisper + CosyVoice2 / F5-TTS）+ 云端 LLM 的实时多口音英语对话练习。

不是又一个"AI 聊天框"。SpeakEasy 要解决的是**哑巴英语**的核心困境：
你缺的不是词汇量，是**在真实压力下用另一种口音即时对话的条件反射**。

所以它做了三件大多数产品没做的事：

1. **口音是真的**——不是给标准音加滤镜。英国咖啡师的 RP、班加罗尔工程师的 syllable-timed 节奏、罗马设计师的音乐性语调，都是靠 **zero-shot 音色克隆**从 5–10 秒参考音频里还原的。你说英文，对面用**那个人的口音和语气**回你。
2. **延迟是真的**——所有语音处理在本地 GPU 上跑，LLM 流出来的**第一句话立刻进 TTS**，音频边合成边推送。目标首音 **< 1 秒**，而不是等一整段回复生成完。
3. **反馈不打断你**——对话进行中，侧栏异步给你**地道表达建议**（"I very like this coffee" → "I'm really into this coffee"，并告诉你为什么）。它不评分、不弹窗、不要求你停下来。

---

## 目录

- [30 秒跑起来](#30-秒跑起来)
- [它长什么样](#它长什么样)
- [架构](#架构)
- [目录结构](#目录结构)
- [五个角色](#五个角色)
- [关键设计决策](#关键设计决策)
- [配置](#配置)
- [脚本](#脚本)
- [故障排查](#故障排查)
- [路线图](#路线图)

---

## 30 秒跑起来

**你机器上已经装好了**（RTX 5080 / Python 3.11 / venv 在 `D:\speakeasy-venv`）：

```powershell
cd D:\desktop\口语
D:\speakeasy-venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --port 8000
# 打开 http://127.0.0.1:8000
```

**填 API Key（两种方式，二选一）**：

1. **界面填**（推荐，即时生效无需重启）：打开页面 → 右上角 **⚙ 模型设置** → 选预设或填 Base URL / API Key / 模型名 → 点「保存并启用」。会立即发一次真实请求验证。
2. **改 `.env`**：`LLM_BASE_URL=...`、`LLM_API_KEY=...`、`LLM_MODEL=...`，重启。

> 界面保存的配置存在 `data/llm_config.json`，优先级高于 `.env`。
> Key 只写在本机，接口永不回传明文（返回的是 `sk-a…2345` 这样的指纹）。

**全新机器从零装**：

```powershell
python scripts/setup_env.py      # 建 venv、装 cu128 torch、装依赖、生成 .env
```

具体见 [docs/SETUP_GPU.md](docs/SETUP_GPU.md)（CUDA 12.8 / sm_120 / 显存分配 / 各种坑）。

---

## 四个入口，都在页面右上角

| 按钮 | 作用 | 文档 |
|---|---|---|
| **🌐 口音市场** | 50 个声线 / 20 个地区，试听后给角色换口音 | [docs/ACCENT_MARKET.md](docs/ACCENT_MARKET.md) |
| **➕ 造角色** | 填表设计你自己的角色和场景（提示词自动生成） | [docs/ACCENT_MARKET.md](docs/ACCENT_MARKET.md) |
| **🔊 克隆音色** | 上传 30s–1min 录音，让角色用**你的声音**说英语 | [docs/VOICE_CLONE.md](docs/VOICE_CLONE.md) |
| **⚙ 模型设置** | 填 Base URL / API Key / 模型名，即时生效 | 本页下方 |

### 口音市场

**36 个官方英语变体**（14 个地区，真实口音 100% 自然）+ **14 个外语兜底**
（意大利/法国/德国/西班牙/日本/中国，口音浓但个别词可能读不准）。

英式、美式、印度、爱尔兰、澳洲、新西兰、加拿大、新加坡、南非、香港、
肯尼亚、尼日利亚、菲律宾、坦桑尼亚 —— 全部可用。

点 ▶ 试听，点 ✓ 应用到角色，**立即生效**。

### 造角色

填名字/城市/职业/口音/性格/开场白 → 提示词自动生成，不用写 prompt。
口音从市场里选，可以先用这些设定试听一句再保存。

### 场景

9 个内置模板（咖啡店/机场/面试/看医生/酒店/租房/客服/社交/自定义），
可以加自己的细节（地点、目标、背景）。

### 声音克隆

点 **🔊 克隆音色** → 上传音频或直接录音 → 试听 → 应用到角色。
基于 **XTTS-v2 + OpenVoice V2 级联**：口音来自角色的真人声线，
音色来自你的录音。

> 配置存 `data/llm_config.json`，优先级高于 `.env`。Key 接口永不回传明文。
> 自定义角色/场景/口音覆盖存 `data/custom.json`，升级不丢。

完整的 GPU 环境说明（CUDA 12.8 / sm_120 / 显存分配 / 各种坑）见 **[docs/SETUP_GPU.md](docs/SETUP_GPU.md)**。

---

## 它长什么样

```
┌──────────────────────────────────────────────────────────────────────────┐
│ ● 就绪   SpeakEasy   首音 682ms   STT 178ms   TTFT 341ms   丢帧 0        │
├──────────┬───────────────────────────────────────────┬───────────────────┤
│ 对话伙伴  │ 对话 · STT:distil-large-v3 | TTS:f5tts     │ 地道润色建议       │
│          │                                           │                   │
│ 🇬🇧 Oliver│  Oliver                          14:32:01 │ ┌───────────────┐ │
│ 伦敦咖啡师│  Morning! What can I get you?             │ │ I very like   │ │
│ RP      ●│                                           │ │ this coffee   │ │
│          │                    你            14:32:08 │ │ ───────────── │ │
│ 🇮🇳 Raj   │                    i want a flat white   │ │ I'm really    │ │
│ 班加罗尔  │                                           │ │ into this     │ │
│ 印度英语 ●│  Oliver                          14:32:11 │ │ coffee        │ │
│          │  Right you are — one flat white coming    │ │               │ │
│ 🇮🇹 Marco │  up. That'll be three eighty.  ▎          │ │ very 不能直接 │ │
│ 罗马设计  │  ▁▃▅▇▅▃▁▂▃▅▇▅▃▁  ← 正在播放                │ │ 修饰动词，用  │ │
│ 意大利语 ●│                                           │ │ really / be   │ │
│          │  ┌────────────────────────────────────┐   │ │ into 更地道   │ │
│ 🇺🇸 Emily │  │ ▁▂▃▅▇▅▃▂▁  [🎤 按住说话] [停止][打断]│   │ │ casual        │ │
│ 纽约招聘  │  └────────────────────────────────────┘   │ └───────────────┘ │
│          │  ┌──────────────────────────┬─────────┐  │                   │
│ 模式      │  │ 没麦克风？直接打字…       │  发送   │  │                   │
│ ●自由闲聊 │  └──────────────────────────┴─────────┘  │                   │
│ ○任务达成 │                                           │                   │
│ ○表达特训 │                                           │                   │
└──────────┴───────────────────────────────────────────┴───────────────────┘
```

顶部四个数字是**实时真实指标**，不是装饰：
`首音` = 从 STT 出字到第一帧音频推送的毫秒数；`丢帧` = 音频 seq 跳号的次数。

---

## 架构

```
                    ┌──────────────────── 浏览器 ─────────────────────┐
                    │  AudioWorklet (16kHz)  →  PCM16 20ms 帧          │
                    │  AnalyserNode 波形     ←  AudioBufferSourceNode  │
                    └───────────┬───────────────────────▲──────────────┘
                       二进制帧 │                        │ JSON+base64 音频
                                ▼                        │
 ┌──────────────────────────── FastAPI (WebSocket) ──────┴──────────────┐
 │                                                                      │
 │  ┌─────────────┐   utterance   ┌──────────────┐                      │
 │  │ StreamingVAD│──────────────▶│  STTEngine   │  faster-whisper      │
 │  │ Silero-VAD  │  preroll 缓冲  │  distil-large│  int8_float16        │
 │  │ +能量降级    │  静音550ms截断 │  -v3         │  ~180ms              │
 │  └─────────────┘               └──────┬───────┘                      │
 │                                       │ "i want a flat white"        │
 │                                       ▼                              │
 │                          ┌────────────────────────┐                  │
 │                          │ TurnOrchestrator       │                  │
 │                          │  · persona system prompt│                 │
 │                          │  · 历史裁剪 (8轮)       │                 │
 │                          └────────┬───────────────┘                  │
 │                                   ▼                                  │
 │                        ┌──────────────────────┐   SSE/stream         │
 │                        │ LLMClient (async)    │─────────▶ 云端 LLM   │
 │                        │ OpenAI 兼容 + mock    │◀───────── token 流  │
 │                        └──────────┬───────────┘                      │
 │                                   ▼ delta                            │
 │                     ┌──────────────────────────┐                     │
 │                     │ StreamingSentenceSplitter│  首句更激进：        │
 │                     │ 缩写/小数/引号 不误切     │  遇 , 就切           │
 │                     └──────────┬───────────────┘                     │
 │                         sentence 0, 1, 2 …                          │
 │                                ▼                                     │
 │              ┌────────────────────────────────────┐                  │
 │              │ 有序 TTS 队列（串行推理,保序发送）   │                  │
 │              │ TTSEngine: CosyVoice2 / F5-TTS     │                  │
 │              │ zero-shot 克隆参考音频 → 24kHz      │                  │
 │              └──────────────┬─────────────────────┘                  │
 │                             ▼ 200ms PCM chunks, seq 严格递增          │
 │              ┌──────────────────────────────┐                        │
 │              │ 建议服务（后台 fire-and-forget）│──▶ 异步, 绝不阻塞音频  │
 │              └──────────────────────────────┘                        │
 └──────────────────────────────────────────────────────────────────────┘
```

**首音延迟预算**（详见 [docs/SETUP_GPU.md §10](docs/SETUP_GPU.md)）：

| 阶段 | 耗时 | 可调项 |
|---|---:|---|
| VAD 判定说完了 | 550 ms | `VAD_MIN_SILENCE_MS` |
| STT (distil-large-v3, int8) | ~180 ms | 模型大小 / compute_type |
| LLM 首 token | ~350 ms | 模型 / `max_tokens` |
| 首句断句 | ~30 ms | `PIPELINE_FIRST_SENTENCE_MAX_CHARS` |
| TTS 首句合成 | ~300 ms | `TTS_NFE_STEP` / 参考音频长度 |
| **首音（STT→音频）** | **~680 ms** | ✅ 目标 < 1000 |

---

## 目录结构

```
口语/
├── README.md                     ← 你在这里
├── .env.example                  环境变量模板（复制成 .env）
├── pyproject.toml                包元数据 + pytest/ruff/mypy 配置
│
├── docs/
│   ├── SETUP_GPU.md              ★ CUDA/torch/ffmpeg/显存/坑 全在这里
│   ├── CONTRACTS.md              内部接口契约（并行开发的权威定义）
│   └── PROTOCOL.md               ★ WebSocket / HTTP 协议（前后端契约）
│
├── scripts/
│   ├── setup_env.py              一键安装 + 体检（真的跑 CUDA kernel 验证）
│   ├── download_models.py        HF 镜像下载模型
│   ├── record_reference.py       录制零样本克隆参考音频 + 校验
│   └── e2e_test.py               端到端测试（无需麦克风/无需 key）
│
├── assets/ref_audio/             5–10s 参考音频 + manifest.json
│
├── backend/
│   ├── requirements.txt          （不含 torch，见 SETUP_GPU §3）
│   ├── requirements-tts.txt      TTS 克隆引擎（二选一）
│   ├── app/
│   │   ├── main.py               FastAPI 入口 + 静态挂载
│   │   ├── __main__.py           python -m app
│   │   ├── config.py             全部配置（pydantic-settings）
│   │   ├── engines.py            引擎装配 + GPU 探测
│   │   ├── schemas.py            HTTP 模型
│   │   ├── logging_utils.py      日志 + Stopwatch 打点
│   │   ├── session.py            会话与历史
│   │   ├── personas/
│   │   │   ├── __init__.py       PersonaLibrary：prompt 构造
│   │   │   └── personas.json     ★ 角色模板库（prompt + 音色 + 口音要点）
│   │   ├── voice/                ← 本地语音栈
│   │   │   ├── audio_utils.py    PCM/重采样/读写 wav（纯 numpy）
│   │   │   ├── vad.py            StreamingVAD（Silero + 能量降级 + preroll）
│   │   │   ├── stt.py            STTEngine（faster-whisper + mock）
│   │   │   ├── tts.py            TTSEngine（CosyVoice2 / F5-TTS / tone）
│   │   │   ├── ref_audio.py      ReferenceLibrary
│   │   │   └── tone_tts.py       无模型占位合成器（全局兜底）
│   │   ├── llm/                  ← 文本大脑
│   │   │   ├── client.py         LLMClient（OpenAI 兼容 + 流式 + mock）
│   │   │   ├── sentence_splitter.py  ★ 流式断句（首包延迟关键）
│   │   │   ├── suggestions.py    润色建议（容错 JSON）
│   │   │   └── mock_llm.py       无 key 时的规则回复
│   │   ├── pipeline/
│   │   │   ├── orchestrator.py   ★ 回合编排（保序并发流水线）
│   │   │   └── metrics.py        延迟指标
│   │   ├── routers/
│   │   │   ├── ws.py             ★ WebSocket 路由
│   │   │   └── http_api.py       REST API
│   │   └── static/
│   │       ├── standalone.html   零依赖备用前端（单文件，无需构建）
│   │       └── app/              Next.js 构建产物（挂到 /）
│   └── tests/                    pytest
│
└── frontend/                     Next.js 15 + TS + Tailwind v4
    └── src/…
```

---

## 五个角色

每个角色都是**口音 + 人格 + 说话习惯**的完整包，不只是换个名字：

| | Oliver 🇬🇧 | Raj 🇮🇳 | Marco 🇮🇹 | Emily 🇺🇸 | Priya 🇸🇬 |
|---|---|---|---|---|---|
| **设定** | Soho 咖啡师 | 班加罗尔工程师 | 罗马设计师 | 纽约招聘官 | 新加坡 PM |
| **口音** | British RP | Indian English | Italian English | American | Singapore |
| **场景** | 点单闲聊 | 技术评审 | 聊设计 | 模拟面试 | 产品决策 |
| **难度** | 简单 | 中 | 难 | 中 | 难 |
| **说话标记** | right then, spot on, cheers, a bit of | actually, itself, prepone, na | allora, dai, no?, ma dai | okay so, walk me through, got it | can/cannot, ah, lah, already |

**Prompt 设计要点**（见 `personas/personas.json`）：

- **强制短句**——1–2 句，最多 55 词。这是口语，不是作文。
- **用词塑造口音，不靠拼写**——印度英语用 `prepone`/`itself`，意大利英语用 `allora`/`dai`，**绝不**写 `"it is-a too much"` 这种假口音拼写。音色由 TTS 克隆负责，文本负责词法与句法习惯。
- **隐性纠错**——不直接说"你错了"，而是把对方的想法用正确形式重述一遍。这是二语习得里最有效的 recast 反馈。
- **不许出戏**——不承认是 AI，不用 markdown/emoji/星号动作描写。
- **每轮必须留钩子**——一个提问或一个邀你继续的动作。

三种对话模式可叠加在任意角色上：
`自由闲聊`（角色主动带话题）/ `任务达成`（角色设置障碍，逼你谈判）/ `表达特训`（角色反复示范一个句式，诱导你模仿）。

---

## 关键设计决策

### 为什么断句要这么激进？

LLM 吐完整段再 TTS，首音要等 3–5 秒——这毁掉对话感。
所以 `StreamingSentenceSplitter` 做了件反直觉的事：**第一句提前切**。

```
常规： 等到句号才切 → 首句可能 90 个字符 → TTS 合成 1.2s
首句： 遇逗号且长度 ≥ 40 就切 → 首句 45 字符 → TTS 合成 0.6s
```

代价是第一句可能偏短（"Right then,"），但这在真实对话里**完全自然**——母语者也这么喘气。
后续句子用正常规则（`.!?` + 缩写白名单 + 小数保护 + `...` 保护），保证语义完整。

### 为什么 TTS 串行，音频却不卡？

单卡上并行的 TTS 推理会互相抢显存/算力，反而抖动更大。
所以设计是：**LLM 流式与断句并行跑，TTS 推理串行排队，但发送严格保序**。

```
LLM:  ──delta──delta──delta─────────delta──▶
切分:      ↓sentence0   ↓sentence1      ↓s2
TTS:   [====s0====][====s1====][====s2====]   串行（GPU 独占）
发送:       ↓s0帧…     ↓s1帧…      ↓s2帧…      严格按 seq 递增
```

`s0` 在合成时，`s1` 已经在队列里等着，LLM 也还在继续。总时长不变，但**第一帧音频的延迟最小化**。

### 为什么每个引擎都要有降级后端？

这是一个会在别人机器上运行的项目。目标机器可能：
没 GPU / 没装 torch / HF 下载失败 / 用户没填 API key。

如果任意一处硬失败就整个跑不起来，用户连"看看界面长什么样"都做不到。
所以每个关键组件都有一个**功能退化但协议完整**的替身：

| 真组件 | 降级 | 效果 |
|---|---|---|
| faster-whisper | `mock` STT | 返回固定英文句子，延迟真实 |
| CosyVoice2/F5-TTS | `tone` TTS | numpy 合成的"有节奏类语音"，不好听但能验证流与延迟 |
| Silero-VAD | 能量 VAD | 自适应噪声底 + 迟滞，接口一致 |
| 云端 LLM | 规则引擎 | 按角色/关键词选模板，逐字流式 |

**协议不变、指标真实**——这样你可以在没有模型时先把前端、播放、波形、jitter buffer 全调好，
再插上模型验证质量。降级状态在 `/api/health` 和 UI 里**显式可见**，不会偷偷糊弄。

### 为什么用 WebSocket 而不是 WebRTC？

WebRTC 的低延迟优势在**网络不可控**时才关键。本地回环上：
WebSocket + 原始 PCM 的额外开销 ≈ 0，而复杂度差了整整一个量级
（信令服务器、ICE、SDP、TURN、编解码协商）。

WebRTC 的优势是可以做**真双工 + 原生 jitter buffer + 丢包补偿**。
如果将来要部署到公网/移动端，`docs/PROTOCOL.md` 的消息语义可以原样搬到 DataChannel。
当前选择是**用复杂度换确定性**：单文件协议、`ws` 一行连上、肉眼可调试。

---

## 配置

全部配置在 `.env`（从 `.env.example` 复制）。最常改的几项：

```ini
# 文本大脑
LLM_API_KEY=sk-...                          # 不填则走规则引擎
LLM_BASE_URL=https://api.deepseek.com/v1    # 任何 OpenAI 兼容端点
LLM_MODEL=deepseek-chat

# 语音
STT_MODEL_SIZE=distil-large-v3              # large-v3 更准但慢一倍
STT_COMPUTE_TYPE=int8_float16               # 16G 显存推荐
TTS_BACKEND=auto                            # auto|cosyvoice2|f5tts|tone

# 延迟 vs 稳定
VAD_MIN_SILENCE_MS=550                      # 调小→更快，但容易截断
PIPELINE_FIRST_SENTENCE_MAX_CHARS=60        # 调小→首音更快
TTS_NFE_STEP=24                             # F5-TTS 采样步数，16 更快

# 镜像（国内必配）
HF_ENDPOINT=https://hf-mirror.com
```

---

## 脚本

| 命令 | 作用 |
|---|---|
| `python scripts/setup_env.py` | 建 venv、装 cu128 torch、装依赖、生成 .env |
| `python scripts/setup_env.py --check` | 体检：真的跑一次 fp16 matmul 验证 sm_120 kernel |
| `python scripts/download_models.py --list` | 列出可用模型与体积 |
| `python scripts/download_models.py --stt distil-large-v3 --tts f5tts` | 下载权重 |
| `python scripts/record_reference.py --list` | 列出角色与录制稿 |
| `python scripts/record_reference.py --persona oliver` | 录参考音频 + 自动裁剪/归一化/更新 manifest |
| `python scripts/record_reference.py --validate` | 体检参考音频（时长/采样率/音量/ref_text） |
| `python scripts/e2e_test.py` | 端到端：连 WS、跑多轮、打印延迟分解表 + SLO 判定 |
| `python -m pytest backend/tests -q` | 单元测试 |

---

## 故障排查

**"torch.cuda.is_available() 是 False"**
装的是 CPU 版。`pip uninstall -y torch torchaudio` 后用 `--index-url https://download.pytorch.org/whl/cu128` 重装。
RTX 5080 是 sm_120，**必须** cu128+。详见 [docs/SETUP_GPU.md §3](docs/SETUP_GPU.md)。

**"no kernel image is available for execution on the device"**
同上——你的 torch 编译时不含 sm_120 kernel。这不是代码问题。

**"页面打不开 / 白屏"**
后端没起。`python -m uvicorn app.main:app --app-dir backend --port 8000`。
后端会自己回落 `standalone.html`，所以即使 Next 前端没构建也能用。

**"能连上但没声音"**
看 UI 的 `TTS:` 字段。如果是 `tone`，说明克隆引擎没装——它合成的音频"不好听但应该有声"。
若连 tone 都没声，检查 `/api/health` 和浏览器控制台（自动播放策略要求先有用户手势，点一下"连接"即可）。

**"首音 > 2 秒"**
按 [docs/SETUP_GPU.md §10](docs/SETUP_GPU.md) 的清单逐项调。最常见原因：
参考音频太长（>10s）、`VAD_MIN_SILENCE_MS` 太大、GPU 被别的程序占用。

**"识别全是空白/乱码"**
前端采样率不是 16k。后端会拒收其它采样率并回 `error{code:"unsupported_sample_rate"}`。
检查浏览器是否支持 `new AudioContext({sampleRate:16000})`；不支持时 worklet 会重采样。

**"ffmpeg not found"**
`winget install --id Gyan.FFmpeg -e`，然后**重开终端**（PATH 才生效）。

更多见 [docs/SETUP_GPU.md §9](docs/SETUP_GPU.md) 的完整故障表。

---

## 路线图

已经能用的：多口音 zero-shot 克隆、流式断句、barge-in 打断、异步润色建议、5 角色 × 3 模式、无依赖降级链路。

还没做但值得做：

- [ ] **发音评分**——用 wav2vec2 + 强制对齐给出音素级偏差（当前只做表达润色，不做发音）
- [ ] **流式 STT**——现在是说完才转写，改 chunked 推理可再省 200ms
- [ ] **WebRTC 传输**——为公网/移动端准备
- [ ] **难度自适应**——根据用户错误率动态调整角色语速与词汇复杂度
- [ ] **会话持久化**——SQLite 存历史，支持"上周和 Marco 聊到哪了"
- [ ] **自定义角色**——上传一段音频 + 描述，自动生成 persona 与 prompt

---

## 许可与合规

代码 MIT。

**参考音频重要提示**：用真人录音做声音克隆涉及**声音权/肖像权**。本项目默认让你**自己录制**
（`scripts/record_reference.py`），这也是效果最稳定的做法。若使用公开素材，
**仅限个人学习自用**，不要分发、不要商用、不要用于冒充他人。生产环境请使用已授权的音色库。

用到的开源项目：Silero-VAD (MIT)、faster-whisper (MIT)、CTranslate2 (MIT)、
CosyVoice (Apache-2.0)、F5-TTS (MIT)、FastAPI (MIT)、Next.js (MIT)。
