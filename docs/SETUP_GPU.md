# 本地 GPU 环境配置（Windows + RTX 5080 16GB）

> 本文是**最容易踩坑**的部分写成的操作手册。RTX 5080 是 Blackwell 架构（compute capability **12.0 / sm_120**），
> 对 CUDA / PyTorch 版本有硬性要求，装错就会报 `no kernel image is available for execution on the device`。

## 0. 一分钟版本

```powershell
cd D:\desktop\口语

# 1) Python 3.11 虚拟环境（不要用 3.12/3.13/3.14，faster-whisper / ctranslate2 轮子还不全）
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2) 先装 CUDA 12.8 版 PyTorch（关键！顺序不能反）
pip install --upgrade pip
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128

# 3) 验证 GPU 能用
python -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
# 期望输出类似： 2.x.x+cu128 True NVIDIA GeForce RTX 5080

# 4) 装其余后端依赖
pip install -r backend/requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

# 5) 复制配置
Copy-Item .env.example .env
# 编辑 .env，填 LLM_API_KEY
```

启动：

```powershell
cd D:\desktop\口语
.\.venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
# 浏览器打开 http://127.0.0.1:8000
```

---

## 1. 硬件与驱动

| 项目 | 本机实际值 | 要求 |
|---|---|---|
| GPU | NVIDIA GeForce RTX 5080 | — |
| 显存 | 16303 MiB (≈16 GB) | ≥ 12 GB 可用 |
| 驱动 | 610.88 | **≥ 570**（CUDA 12.8 最低要求） |
| 计算能力 | sm_120 (12.0) | 需 CUDA 12.8+ 编译的 kernel |
| RAM | 32 GB | 建议 ≥ 24 GB（模型加载峰值） |

检查驱动是否够新：

```powershell
nvidia-smi
# 右上角 "CUDA Version: 12.8" 或更高
```

若驱动过旧，去 <https://www.nvidia.com/download/index.aspx> 更新到 570+。**不需要单独安装 CUDA Toolkit**
——PyTorch 的 cu128 轮子自带 CUDA 运行时（`torch/lib/*.dll`）。

---

## 2. Python 版本

**必须 3.10 / 3.11 / 3.12**，推荐 **3.11**。

原因：
- `ctranslate2`（faster-whisper 的推理后端）对 3.13/3.14 尚未提供 Windows 轮子，会 fallback 到源码编译（需要 MSVC + 数小时）。
- `torch` 官方 Windows 轮子目前覆盖到 3.13，但生态库常常更慢。

本机已有：
```
C:\Python314\python.exe                                        ← 系统默认，不要用
C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe
```

用 `py -3.11` 调用后者。

---

## 3. PyTorch：唯一正确的装法

### 3.1 选对索引

| 情况 | 命令 |
|---|---|
| **RTX 5080（本机）** | `pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128` |
| 更新的 CUDA（12.9/13.x 发布后） | 换成 `/whl/cu129` 等 |
| 无 GPU 想先用起来 | `pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu` |

### 3.2 三条铁律

1. **先装 torch，再装 `requirements.txt`。**
   `requirements.txt` 里故意**不写** torch；如果反过来先装 requirements，
   某些库（如 `faster-whisper`、`silero-vad`）会把 CPU 版 torch 作为依赖拉进来，
   然后你再去装 cu128 版会变成"两个 torch 共存"，出现 `torch.cuda.is_available() == False` 的诡异现象。
   若已经搞混：`pip uninstall -y torch torchaudio torchvision` 后重来。

2. **不要用 `pip install torch`（不带 index-url）。**
   PyPI 上的 Windows 版 torch 默认是 **CPU-only** 或 cu12x 但可能不含 sm_120。

3. **torch / torchaudio / torchvision 版本必须同源同版本号。**
   混合会出现 DLL 冲突（`OSError: [WinError 127]` 找不到指定的程序）。

### 3.3 验证脚本

```powershell
python -c @"
import torch
print('torch      :', torch.__version__)
print('cuda build :', torch.version.cuda)
print('available  :', torch.cuda.is_available())
if torch.cuda.is_available():
    p = torch.cuda.get_device_properties(0)
    print('device     :', p.name)
    print('capability :', f'{p.major}.{p.minor}')   # 必须是 12.0
    print('vram       :', round(p.total_memory/1024**3,1), 'GB')
    # 真正跑一次 kernel，才证明 sm_120 有实现
    a = torch.randn(1024, 1024, device='cuda', dtype=torch.float16)
    b = a @ a
    torch.cuda.synchronize()
    print('matmul fp16: OK', float(b.abs().mean()))
"@
```

最后一步 `matmul fp16: OK` 是关键——只有 `is_available()` 为 True 但没跑 kernel，可能仍然是假阳性。

---

## 4. 系统级依赖

### 4.1 ffmpeg（**必须**）

faster-whisper 解码非 wav 音频（webm/opus/mp3）依赖 ffmpeg；前端上行是原始 PCM，但参考音频/调试音频常是 mp3。

```powershell
# 方式一：winget（推荐）
winget install --id Gyan.FFmpeg -e

# 方式二：手动
# 下载 https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip
# 解压到 C:\ffmpeg，把 C:\ffmpeg\bin 加入 PATH
```

验证：

```powershell
ffmpeg -version
# 新开的 PowerShell 窗口才生效
```

> 本机当前**未安装** ffmpeg。装好后重开终端。

### 4.2 MSVC 运行库

PyTorch / ctranslate2 依赖 VC++ 2015-2022 Redistributable：

<https://aka.ms/vs/17/release/vc_redist.x64.exe>

Windows 10/11 一般已有。若报 `VCRUNTIME140_1.dll not found` 就装这个。

### 4.3 长路径支持（建议）

模型缓存路径可能很深，Windows 默认 260 字符限制会炸：

```powershell
# 管理员权限
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" `
  -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force
```

---

## 5. HuggingFace 镜像（国内必配）

`huggingface.co` 本机**不可达**（已实测超时），必须用镜像：

```powershell
# 当前会话
$env:HF_ENDPOINT = "https://hf-mirror.com"
```

或在 `.env` 里：

```ini
HF_ENDPOINT=https://hf-mirror.com
HF_HOME=D:/models/hf
```

后端启动时会自动读取并设置（见 `app/config.py::apply_hf_env`），
但**必须在 import transformers/huggingface_hub 之前**——`app/config.py` 已经是这样做的。

验证镜像：

```powershell
# 应该返回 200，且能列出文件
curl.exe -I https://hf-mirror.com/Systran/faster-distil-whisper-large-v3/resolve/main/config.json
```

---

## 6. STT：faster-whisper 模型选择

| 模型 | 显存(FP16) | 显存(INT8) | 相对速度 | 英文准确度 | 建议 |
|---|---|---|---|---|---|
| `distil-large-v3` | ~2.3 GB | ~1.2 GB | **2× 快** | 接近 large-v3 | ✅ **默认** |
| `large-v3` | ~4.7 GB | ~2.5 GB | 1× | 最好 | 追求极致准确 |
| `medium` | ~2.3 GB | ~1.2 GB | 3× | 够用 | 显存紧张 |
| `small` | ~0.9 GB | ~0.5 GB | 6× | 一般 | 只做演示 |

本机 `.env` 默认 `STT_MODEL_SIZE=distil-large-v3` + `STT_COMPUTE_TYPE=int8_float16`：
- INT8 权重 + FP16 计算，在 Blackwell 上走 Tensor Core，速度快、显存省。
- 若发现识别质量不满意，改 `STT_COMPUTE_TYPE=float16`（多占 ~1GB，质量略升）。

首次运行会从 `HF_ENDPOINT` 下载（`distil-large-v3` 约 **1.5 GB**）。预下载：

```powershell
python scripts/download_models.py --stt distil-large-v3
```

### 显存预算（16GB 怎么分）

```
faster-whisper distil-large-v3 (int8_float16)   ~1.2 GB
CosyVoice2-0.5B (fp16)                          ~3.5 GB
                                              ─────────
合计                                            ~4.7 GB
剩余给 CUDA context / 激活 / 峰值               ~11 GB ✅ 很宽裕
```

如果用 F5-TTS（F5TTS_Base ~330M 参数，fp16 约 **1.3 GB**），总占用更低。
**16GB 完全够，不需要量化或 offload。**

---

## 7. TTS 选择：Windows 上的现实

| 引擎 | 质量 | Windows 安装难度 | 首音延迟(24k, 单句) | 建议 |
|---|---|---|---|---|
| **F5-TTS** | 优秀，口音克隆强 | ⭐ 简单（纯 pip） | ~250–450 ms | ✅ **Windows 首选** |
| **CosyVoice 2** | 优秀，中英混合更强 | ⭐⭐⭐⭐ 难（pynini/WeTextProcessing 在 Windows 编译困难） | ~200–400 ms (流式) | 有 Linux/WSL 时用 |

### 7.1 F5-TTS 安装（推荐）

```powershell
git clone https://github.com/SWivid/F5-TTS.git third_party/F5-TTS
# 国内加速：git clone https://ghproxy.net/https://github.com/SWivid/F5-TTS.git third_party/F5-TTS
pip install -e third_party/F5-TTS

# 权重（F5TTS_Base 约 1.4GB），首次运行自动下载到 HF_HOME
python scripts/download_models.py --tts f5tts
```

`.env`：
```ini
TTS_BACKEND=f5tts
TTS_NFE_STEP=24
```

### 7.2 CosyVoice 2 安装（进阶）

CosyVoice 的核心依赖 `pynini` / `WeTextProcessing` 在 Windows 上需要 conda 预编译包：

```powershell
# 1) 用 conda 装 pynini（pip 在 Windows 上编不过）
conda install -c conda-forge pynini=2.1.5

# 2) clone 仓库
git clone --recursive https://github.com/FunAudioLLM/CosyVoice.git third_party/CosyVoice

# 3) 装依赖
pip install -r third_party/CosyVoice/requirements.txt

# 4) 下模型
python scripts/download_models.py --tts cosyvoice2
```

若 `pynini` 装不上，可改用 **WSL2**：
```powershell
wsl --install -d Ubuntu-22.04
```
在 WSL 里配 CUDA（<https://learn.microsoft.com/windows/wsl/tutorials/gpu-compute>），然后整个后端跑在 WSL，
Windows 侧只开浏览器访问 `http://localhost:8000`——这是**最省心**的方案。

`.env`：
```ini
TTS_BACKEND=cosyvoice2
COSYVOICE_REPO_DIR=third_party/CosyVoice
COSYVOICE_MODEL_DIR=pretrained_models/CosyVoice2-0.5B
```

### 7.3 零模型降级（先跑通链路）

不装任何 TTS 时，`TTS_BACKEND=auto` 会落到内置 **`tone`** 后端：
它用 numpy 合成"有节奏的类语音"，不好听但**延迟数据完全真实**，
可以先验证 WebSocket 流、播放、波形、首音延迟是否达标，再接真模型。

---

## 8. 参考音频（口音克隆的心脏）

Zero-shot 克隆需要每个角色一段 **5–10 秒**的参考音频 + 逐字转写文本。

### 8.1 目录结构

```
assets/ref_audio/
├── manifest.json          # 由 backend-voice 提供 schema
├── oliver_rp.wav          # 英国 RP
├── raj_indian.wav         # 印度英语
├── marco_italian.wav      # 意大利英语
├── emily_us.wav           # 美音
└── priya_sg.wav           # 新加坡英语
```

### 8.2 三种搞法

**A. 自己录（最快、最安全、最像你想要的）**

```powershell
python scripts/record_reference.py --persona oliver --seconds 8
# 按提示朗读屏幕上给出的参考文本，脚本自动裁静音、转 16k 单声道 wav
```

强烈建议：**用自己的声音模仿该口音**录参考音频，克隆出的音色会稳定且没有版权问题。

**B. 公开数据集 / 播客片段**

- `scripts/fetch_ref_audio.py` 提供下载自公开来源的脚本骨架（含出处与许可提示）。
- ⚠️ **合规提醒**：用真人录音做声音克隆涉及肖像权/声音权。仅限**个人学习自用**，
  不要分发、不要商用、不要用于冒充他人。生产环境请使用**购买授权的**或**自己录制**的音色。

**C. 用 TTS 生成"伪参考"**

用 `tone` 之外的可控 TTS（如 edge-tts 的 en-GB-RyanNeural）生成一段，再喂给克隆引擎。
效果一般但零版权风险，适合快速验证。

### 8.3 参考音频质量清单

- 单说话人、无背景音乐、无明显混响
- 16k 或 24k、单声道、16-bit PCM wav
- 时长 5–10 s（低于 4s 克隆不稳定，高于 15s 收益递减）
- 语音内容**覆盖该口音的特征音素**（RP 的 /ɑː/ /ɒ/；印度的 r/θ 替代；意大利的元音结尾）
- `ref_text` 必须与音频**逐字一致**（含标点可省略），不一致会显著劣化克隆

---

## 9. 常见故障排查

| 现象 | 原因 | 解决 |
|---|---|---|
| `torch.cuda.is_available() == False` | 装的是 CPU 版 torch | `pip uninstall -y torch torchaudio` 后按 §3.1 重装 |
| `no kernel image is available` | torch 的 CUDA 版本不含 sm_120 | 必须 cu128+ |
| `CUDA out of memory` | 两个模型同时加载超 16G / batch 太大 | 降 `STT_COMPUTE_TYPE=int8`；`TTS_DTYPE=float16`；关掉其它占显存程序 |
| `WinError 127 找不到指定的程序` | torch 与 torchaudio 版本不匹配 | 同版本重装 |
| `ctranslate2` 编译失败 | Python 3.13/3.14 | 用 3.11 |
| HF 下载超时 | 没配镜像 | 设 `HF_ENDPOINT=https://hf-mirror.com` |
| `pynini` 装不上 | Windows 无预编译轮子 | conda-forge 安装，或改用 F5-TTS，或用 WSL |
| 首音 > 2 秒 | 参考音频太长 / 首句阈值太大 | 参考音频裁到 5-8s；`PIPELINE_FIRST_SENTENCE_MAX_CHARS=40` |
| 识别全是空白 | 前端采样率不是 16k | 检查 `input_sample_rate`；后端会拒绝非 16k |
| 声音断续 | 网络/GPU 抢占 / jitter buffer 太小 | 前端 jitter 提到 200ms；确认没有别的进程占 GPU |
| `ffmpeg not found` | 没装或不在 PATH | §4.1，装完**重开终端** |

---

## 10. 延迟预算（目标：首字 < 1s）

```
用户说完 → VAD 判定结束        550 ms   (VAD_MIN_SILENCE_MS，可调)
        → STT (distil-large-v3)  ~180 ms  (1-3s 语音，int8_float16)
        → LLM 首 token           ~350 ms  (DeepSeek 流式)
        → 首句断句               ~30 ms
        → TTS 首句合成           ~300 ms  (F5-TTS nfe=24)
─────────────────────────────────────────
合计（从 LLM 收到文本算首音）    ~680 ms ✅ 
合计（从用户说完算）             ~1.23 s
```

**关键**：`first_audio_ms` 指标是从「STT 完成」量到「第一帧音频推送」，
这才是用户感知的等待。VAD 的 550ms 是另一段可调延迟。

调优手段（按性价比）：
1. `VAD_MIN_SILENCE_MS` 550 → 400（省 150ms，但更容易打断）
2. `PIPELINE_FIRST_SENTENCE_MAX_CHARS` 60 → 40（首句更早进 TTS）
3. `TTS_NFE_STEP` 24 → 16（F5-TTS 省 ~120ms，质量略降）
4. `STT_MODEL_SIZE` → `distil-large-v3`（已是默认）
5. 参考音频裁到 5–6s（克隆的 prompt 编码更快）
