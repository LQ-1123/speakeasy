# 本次环境安装与变更记录（v0.2）

> 针对两个需求做的实际落地：**① LLM 显式配置**、**② 装一个适合本机的语音模型**。

---

## 一、环境检测结果（实测）

| 项目 | 值 |
|---|---|
| GPU | NVIDIA GeForce RTX 5080 |
| 显存 | 16303 MiB (15.9 GB) |
| 计算能力 | **12.0 (sm_120, Blackwell)** |
| 驱动 | 610.88 |
| OS | Windows |
| Python | 3.11.1（`C:\Users\Administrator\AppData\Local\Programs\Python\Python311`） |
| 磁盘 D: | 678 GB 可用 |

**结论**：sm_120 必须用 **CUDA 12.8+** 编译的 PyTorch，否则报
`no kernel image is available for execution on the device`。

---

## 二、实际安装了哪些东西

### 1. PyTorch（GPU 基础）

```powershell
# 用纯 ASCII 路径的 venv（中文路径会让 pip 卡住）
py -3.11 -m venv D:\speakeasy-venv
D:\speakeasy-venv\Scripts\python.exe -m pip install torch torchaudio ^
  --index-url https://download.pytorch.org/whl/cu128
```

结果：
```
torch 2.11.0+cu128
cuda_build 12.8
cuda_available True
device NVIDIA GeForce RTX 5080
capability 12.0
vram_gb 15.9
kernel_ok True          ← 实际跑了 fp16 matmul，不是只看 is_available()
```

### 2. STT：faster-whisper（**GPU 上跑，实测可用**）

```powershell
D:\speakeasy-venv\Scripts\python.exe -m pip install faster-whisper silero-vad
```

模型：`Systran/faster-distil-whisper-large-v3`（走 hf-mirror.com，约 1.5GB）

**实测转写延迟（RTX 5080, int8_float16）**：

| 输入音频 | 转写结果 | 耗时 |
|---|---|---|
| "I would like a flat white coffee to go, please." | 完全正确 | 首次 6273ms（含 CUDA 预热）<br>后续 **145ms** |
| "Actually, can we prepone the release to Friday?" | 完全正确（含印度英语词 prepone） | **145ms** |
| "Hello, how are you doing today?" | 完全正确 | **145ms** |
| 重复 3 次同一段 | 稳定 | 133 / 143 / 128 ms |

显存占用约 1.2 GB。

### 3. TTS：**Edge-TTS 真人神经声线**（关键突破）

**为什么不是先装 CosyVoice2 / F5-TTS**：
- GitHub clone 在本机被墙（`git clone F5-TTS` 失败）
- 两者都需要额外 1.5–2GB 权重 + 为每个角色录 5–10s 参考音频
- 在此之前只能用 `tone` 占位合成器 → **就是那堆"嗯嗯啊啊"的正弦波**

Edge-TTS 的优势：**开箱就是真人语音**，且覆盖我们需要的全部口音：

| 角色 | 声线 | 口音 |
|---|---|---|
| Oliver | `en-GB-RyanNeural` | 英国 RP |
| Raj | `en-IN-PrabhatNeural` | 印度英语 |
| Marco | `en-GB-ThomasNeural`（rate -4%） | 最接近意大利腔英语的可用声线 |
| Emily | `en-US-AriaNeural` | 美音 |
| Priya | `en-SG-LunaNeural` | 新加坡英语 |

可用英语声线共 **47 个**，覆盖 en-GB / en-IN / en-US / en-AU / en-IE / en-SG /
en-NZ / en-ZA / en-CA / en-HK / en-KE / en-NG / en-PH / en-TZ。

**实测合成（5 个角色全部成功）**：

```
oliver   120384 samples  5.02s  peak=0.586  1304ms
raj      124416 samples  5.18s  peak=0.789  1799ms
marco    137664 samples  5.74s  peak=0.710  1643ms
emily     80640 samples  3.36s  peak=0.519  1633ms
priya    135360 samples  5.64s  peak=0.576  1581ms
```

### 4. 尚未安装：F5-TTS / CosyVoice2（本地克隆）

`pip install f5-tts` 依赖链过长（含 x-transformers / vocos / torchdiffeq / bitsandbytes 等），
安装时间超过 20 分钟仍未完成，已终止。

**这不是阻塞项**：Edge-TTS 已经提供真人语音。F5-TTS 的唯一增量价值是
"还原**特定人物**的音色"，而不是"更像人说英语"。

想装的话见 `docs/SETUP_GPU.md §7.1`。

---

## 三、延迟实测与瓶颈分析

### Edge-TTS 的固定开销

```
1词  "Hi."                 首块 913ms
3词  "Right then."         首块 974ms
8词  (31 字符)             首块 1187ms
18词 (76 字符)             首块 1324ms
30词 (130 字符)            首块 1149ms
```

**关键发现**：首块延迟**与文本长度几乎无关**（913–1324ms 波动），
说明瓶颈是微软语音服务的 **WebSocket 建连 + 会话建立**，不是合成算力。

因此"把句子拆短"无效——我实测过，拆出 `"Allora,"` 单独请求反而更慢。

### 有效的优化：**短句缓存 + 启动预热**

实现了 LRU 缓存（48MB 上限）与启动预热（12 条常用短语 × 5 个角色）：

```
第 1 次合成 "Right then."  →  1240ms
第 2 次（缓存命中）        →     0ms
第 3 次（缓存命中）        →     0ms
```

**端到端效果**：

| 轮次 | 首音延迟 | 说明 |
|---|---|---|
| 第 1 轮 | 1283ms | 未命中缓存 |
| 第 2 轮 | **0.4ms** | `"Right then"` 命中缓存 |

对话中角色有大量固定口头禅（Oliver 的 "Right then"、Raj 的 "Actually"、
Marco 的 "Allora"），实际使用中缓存命中率会很高。

### 结论：要稳定 sub-1s，需要本地 TTS

Edge-TTS 的 ~800-1100ms 是**外部服务的物理下限**，无法在本机优化掉。
只有本地 GPU TTS（F5-TTS / CosyVoice2）才能做到稳定 < 500ms，因为：
- 无网络往返
- 无会话建立
- 参考音频的 speaker embedding 可缓存在显存

这也是原技术选型要求 CosyVoice2/F5-TTS 的原因。

---

## 四、需求①：LLM 显式配置

### 做了什么

新增 `backend/app/llm/runtime_config.py` + `backend/app/routers/llm_config_api.py`：

| 端点 | 作用 |
|---|---|
| `GET /api/llm/config` | 当前配置（**key 脱敏**）+ 7 个供应商预设 |
| `POST /api/llm/config` | 保存并**立即生效**（重建客户端，不用重启）+ 可选校验 |
| `POST /api/llm/test` | 只测连通性，不保存 |
| `POST /api/llm/reset` | 删除运行时配置，回到 `.env` |

### 界面

页面右上角 **⚙ 模型设置** → 弹窗：
- **快速预设**下拉：DeepSeek / OpenAI / SiliconFlow / Moonshot / 智谱 / 本地 Ollama / 自定义
- Base URL、API Key、模型名、温度
- 「测试连接」「保存并启用」「还原 .env」

未配置时页面顶部有黄色提示条，可直接点击进入设置。

### 安全设计

- Key 只写本机 `data/llm_config.json`，**接口永不回传明文**（只返回 `sk-a…2345` 指纹）
- 文件权限尽力收紧为 0600
- 保存时**真的发一次极小的 API 请求**验证，避免"存了错的配置还以为好了"

### 实测验证

用假 key 测试保存流程：
```
POST /api/llm/config  {"api_key":"sk-fake-key-for-test",...}
→ ok=false
  message: "配置已保存，但连接测试失败：API Key 无效或无权限（AuthenticationError）：
            Error code: 401 - Authentication Fails, Your api key: ****test is invalid"
  config: {..., "api_key_masked":"sk-f…test", "api_key_set":true, "source":"file"}
```

这个 401 来自 DeepSeek 真实服务器——**证明 base_url、请求链路、错误处理全部真实工作**。

---

## 五、当前运行状态

```
GET /api/health
{
  "status": "ok",
  "engines": {
    "stt": "faster-whisper",     ← GPU
    "tts": "edge",                ← 真人声线
    "llm_config": {"configured": true, "source": "env"}
  },
  "gpu": {
    "available": true,
    "torch": "2.11.0+cu128",
    "name": "NVIDIA GeForce RTX 5080",
    "total_vram_gb": 15.9,
    "capability": "12.0"
  }
}
```

测试：**138 / 138 通过**

---

## 六、你现在要做的

**唯一必须做的一件事**：填 API Key。

1. 打开 <http://127.0.0.1:8000>
2. 点右上角 **⚙ 模型设置**
3. 选 DeepSeek（或你的供应商）→ 粘贴 Key → **保存并启用**
4. 看到绿色「✓ 连接成功」即可

之后刷新页面，直接用麦克风说话，或者用底部输入框打字。

**之前的"嗯嗯啊啊"已经不会出现了**——现在 TTS 是 `edge`（真人声线）。
如果还听到正弦波，说明服务没重启，或 `.env` 里 `TTS_BACKEND` 被改成了 `tone`。

---

## 七、后续可选优化

如果想进一步降低首音延迟（当前 0.8–1.3s，缓存命中时 ~0s）：

1. **装 F5-TTS**（本地 GPU 克隆）
   ```powershell
   D:\speakeasy-venv\Scripts\python.exe -m pip install f5-tts
   D:\speakeasy-venv\Scripts\python.exe scripts/download_models.py --tts f5tts
   python scripts/record_reference.py --persona oliver
   ```
   然后 `.env` 里 `TTS_BACKEND=f5tts`

2. **扩大预热短语表**：在 `backend/app/voice/tts.py` 的 `_EDGE_WARMUP_PHRASES`
   里加上你角色常用的开场白，缓存命中率越高首音越接近 0。

3. **持久化缓存到磁盘**：目前缓存是进程内的，重启后失效。可以改成
   把合成结果按 hash 存到 `data/tts_cache/`，跨重启复用。
