# 参考音频库（assets/ref_audio/）

Zero-shot 口音克隆的"声带"。每个角色需要一段 **5–10 秒**的参考音频 + 与之**逐字一致**的转写文本。

## 为什么这里默认是空的

声音克隆涉及**声音权 / 肖像权**。本项目不附带任何真人录音，需要你自己准备。
这也是效果最稳定的做法——用自己的声音模仿目标口音，克隆出的音色不会突变，
而且没有版权顾虑。

## 快速开始

```powershell
# 看要录什么
python scripts/record_reference.py --list

# 录一个（按屏幕提示朗读，自动裁剪/归一化/写 manifest）
python scripts/record_reference.py --persona oliver

# 体检
python scripts/record_reference.py --validate
```

没录之前完全不影响使用：`TTSEngine` 会自动降级到内置 `tone` 后端
（numpy 合成的"有节奏类语音"），**协议、音频流、延迟指标都是真实的**，
只是不好听。这让你可以先把前端和链路调好，再补音频。

## 文件清单

| 文件 | 角色 | 口音 | 时长 |
|---|---|---|---|
| `oliver_rp.wav` | Oliver 🇬🇧 | British RP | 5–10 s |
| `raj_indian.wav` | Raj 🇮🇳 | Indian English | 5–10 s |
| `marco_italian.wav` | Marco 🇮🇹 | Italian English | 5–10 s |
| `emily_us.wav` | Emily 🇺🇸 | American English | 5–10 s |
| `priya_sg.wav` | Priya 🇸🇬 | Singapore English | 5–10 s |

## 技术规格（硬要求）

| 项 | 要求 | 说明 |
|---|---|---|
| 格式 | WAV (PCM) | mp3/flac 需 ffmpeg，且引入解码损耗 |
| 采样率 | 16000 或 24000 Hz | 其它值会被重采样，但可能引入轻微失真 |
| 声道 | 单声道 | 立体声会被下混 |
| 位深 | 16-bit | |
| 时长 | **5–10 秒**（下限 4s，上限 15s） | 太短克隆不稳，太长收益递减且拖慢推理 |
| 内容 | 单说话人、无背景音乐、无明显混响 | 背景噪声会被一起克隆进去 |
| 语速 | 自然偏慢一点 | 参考音频的语速会被克隆，太快会让角色一直很急 |

## 怎么选朗读内容

参考文本要在 **5–10 秒内覆盖该口音的特征音素**。`personas.json` 里每个角色的
`voice.ref_text` 已经是精心设计的稿子，直接用即可。

想自己写的话，抓这几个点：

- **British RP**：`/ɑː/` vs `/ɒ/`（bath / lot），清晰的 `t`（not the American flap），非卷舌 `r`
- **Indian English**：`/θ/`→`/t/`（think→tink）、`/v/`→`/ʋ/`、syllable-timed 节奏（每个音节等长）
- **Italian English**：词尾元音化（每个词都"收在元音上"）、明显的双辅音、音乐性语调
- **American**：卷舌 `r`、flap `t`（water→wadder）、`/æ/` 更开
- **Singapore English**：快节奏、相对平的语调、句尾语气词

## manifest.json

由 `record_reference.py` 自动维护，不需要手写。结构：

```json
{
  "version": 1,
  "voices": {
    "oliver": {
      "file": "oliver_rp.wav",
      "ref_text": "Right then, that's a flat white for you, and a pastry on the side. Cheers, guv, that's spot on.",
      "accent": "British RP",
      "duration_s": 8.2,
      "sample_rate": 16000,
      "source": "self-recorded",
      "speed": 1.0
    }
  }
}
```

`ref_text` **必须与音频内容逐字一致**（标点符号可省略）。不一致会显著劣化克隆质量——
模型会试图把音素和错误的文本对齐，产生含糊或跳字。

## 合规

- ✅ 自己录 → 随便用
- ⚠️ 公开播客/视频片段 → **仅限个人学习**，不要分发、不要商用、不要用于冒充他人
- ❌ 未经授权克隆他人声音 → 可能违法（中国《民法典》1023 条声音权参照肖像权保护；欧盟 AI Act 对深度伪造有明确要求）

生产环境请使用**购买授权的**音色库或**自己录制**的音频。
