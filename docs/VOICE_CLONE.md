# 声音克隆（用你自己的音色说话）

上传一段 **10–30 秒**的英文录音，系统就会学会你的音色，之后对话里的角色可以用**你的声音 + 真实母语口音**说英语。

---

## 一、核心：口音是怎么保住的

这件事经历过一次**架构重写**，因为最初方案（XTTS 直接克隆）有个致命问题：

### 失败的方案：XTTS 一步到位

```
文本 → XTTS → 音色 ✓  口音 ✗
```

XTTS-v2 的 speaker encoder 提取的是 **timbre embedding**（声带、声道长度），
而**口音**（语调曲线、元音位置、重音节奏）不在那个空间里。

实测证据（归一化 F0 语调曲线相关性，1.0 = 完全一致）：

| 口音 | XTTS 直克隆 |
|---|---|
| 印度 | **-0.269** |
| 爱尔兰 | 0.179 |
| 南非 | 0.050 |

**5 个角色听起来一样**，因为口音全丢了。用户一开始就说"我听着差不多"——这个判断是对的。

### 现在的方案：级联（保口音 + 换音色）

```
LLM 文本
  ↓
edge-tts（该角色母语声线）        ← 口音、韵律 100% 真实（真人录音）
  ↓  带口音的语音
OpenVoice V2（换成用户上传的音色）  ← 变声器：只替换 timbre 那一支
  ↓
口音真实 + 音色是你
```

原理：OpenVoice 是**语音→语音**的变声器，它把音频拆成
`[内容 | 韵律/语调 | 音色]` 三支，**只替换音色**，韵律和语调原封不动。

实测对比（同样三个口音）：

| 口音 | XTTS 直克隆 | 级联 |
|---|---|---|
| 印度 | -0.269 | **0.142** |
| 爱尔兰 | 0.179 | **0.980** |
| 南非 | 0.050 | **0.844** |

### 线上验收数据（`/api/voice/status` 实测）

同一上传音色 × 4 个角色，两两语调差异：

```
oliver vs raj   : 0.732  ✓
oliver vs marco : 0.461  ✓
oliver vs priya : 0.503  ✓
raj    vs marco : 0.568  ✓
raj    vs priya : 0.544  ✓
marco  vs priya : 0.510  ✓
平均: 0.553      （>0.3 人耳明显可辨）
```

角色们的 f0 都落在 107–123Hz（共用同一音色 ✓），但语调差异显著（口音不同 ✓）。

---

## 二、怎么用

1. 打开 <http://127.0.0.1:8000>
2. 点右上角 **🔊 克隆音色**
3. 两种方式任选：
   - **上传音频文件**：wav / mp3 / m4a / flac / ogg / webm / aac / opus，≤25MB
   - **直接用麦克风录**：点一下开始，念 10–30 秒，再点停止
4. 看到「音色已就绪」后：
   - **试听** → 用该角色口音 + 你的音色读一句
   - **应用到 {角色}** → 之后这个角色就用你的音色说话
5. 返回对话页，正常说话即可

**恢复默认**：弹窗底部有「恢复 {角色} 的默认音色」。

---

## 三、角色 → 口音映射（口音策略）

这里的核心原则：**官方英语变体优先，外语声线只作兜底**。

### 优先级

| 优先级 | 来源 | 说明 | 口音真实度 |
|---|---|---|---|
| **1** | `official` — 官方英语变体 | `en-GB` / `en-IN` / `en-IE` / `en-SG` / `en-ZA` / `en-AU` … | **100%**（真实地区变体） |
| 2 | `foreign_fallback` — 外语声线读英文 | `it-IT` / `fr-FR` / `de-DE` / `ja-JP` … | 高（元音空间与节奏真实，个别词发音可能不准） |
| 绝不使用 | 在英语声线上"假装"外语口音 | — | 既不像也不准 |

### 当前角色

| 角色 | 口音 | 声线 | 来源 |
|---|---|---|---|
| Oliver | British RP 英式标准音 | `en-GB-RyanNeural` | 官方 |
| Raj | Indian English 印度英语 | `en-IN-PrabhatNeural` | 官方 |
| **Marco** | Italian-accented 意大利腔 | `it-IT-GiuseppeMultilingualNeural` | **兜底** |
| Emily | American 美式英语 | `en-US-AriaNeural` | 官方 |
| Priya | Singapore English 新加坡英语 | `en-SG-LunaNeural` | 官方 |

### 为什么 Marco 必须走兜底

微软语音库里有 **14 个地区的官方英语变体**，但**没有 `en-IT`**（也没有 en-FR / en-DE / en-JP）。

所以意大利腔的唯一可行做法：**用意大利语母语声线读英文文本**。
`it-IT-GiuseppeMultilingualNeural` 是为意大利语训练的，它的声学系统里刻着意大利语的
元音空间、音节节奏、辅音习惯。把英文喂给它，它只能用意大利语的方式去读。

实测元音偏移（相对英式，越大越偏离）：

```
fr-FR  +890 ΔF1   总偏移 1105
it-IT  -270       总偏移  910   ← Marco 用的这个
de-DE  -413       总偏移  741
es-ES  +234       总偏移  304
```

**代价**：它没学过英文单词，靠字母规则硬读，个别词发音可能不准。
用户实测反馈：「口音接近了，这是一个解决方法。可以用于兜底。」

### 完整可选变体清单

**官方英语变体（36 个，14 个地区）**：

| 地区 | 声线 |
|---|---|
| `en-GB` 英国 | Ryan / Thomas / Sonia / Libby / Maisie |
| `en-IE` 爱尔兰 | Connor / Emily |
| `en-AU` 澳大利亚 | Natasha / William |
| `en-NZ` 新西兰 | Mitchell / Molly |
| `en-CA` 加拿大 | Liam / Clara |
| `en-US` 美国 | Andrew / Brian / Guy / Jenny / Emma / Aria |
| `en-IN` 印度 | Prabhat / Neerja / NeerjaExpressive |
| `en-SG` 新加坡 | Luna / Wayne |
| `en-ZA` 南非 | Leah / Luke |
| `en-HK` 香港 | Yan / Sam |
| `en-KE` 肯尼亚 | Chilemba / Asilia |
| `en-NG` 尼日利亚 | Abeo / Ezinne |
| `en-PH` 菲律宾 | James / Rosa |
| `en-TZ` 坦桑尼亚 | Elimu / Imani |

**外语兜底（14 个，6 种语言）**：`it-IT`(4) / `fr-FR`(2) / `de-DE`(2) / `es-ES`(2) / `ja-JP`(2) / `zh-CN`(2)

查询完整清单：

```bash
curl http://127.0.0.1:8000/api/voice/accents
```

### 如果你想要某个特定地区的口音

1. **先查有没有官方变体** → `GET /api/voice/accents` 的 `official`
2. **有** → 直接用，口音 100% 自然（这是首选）
3. **没有** → 走外语兜底，接受个别词发音可能不准
4. **都不满意** → 上传一段该地区真人的录音，OpenVoice 能把语调特征带过来
   （但音素层仍是角色声线的，这是架构上限）

---

## 四、录音建议

| 项目 | 建议 |
|---|---|
| 时长 | **10–30 秒最佳**（最少 3 秒，最多 2 分钟） |
| 内容 | 连贯的自我介绍 / 描述你的一天 / 讲一件最近的事 |
| 环境 | 安静、无回声、无背景音乐 |
| 麦克风 | 距嘴 15–20cm，正常音量 |
| 语速 | 自然，不要刻意放慢或朗读腔 |
| 情绪 | 平稳 |

**不要**：多人同时说话、唱歌、纯静音、强混响、音量忽大忽小。

上传后系统会自动：转 mono / 22.05kHz、**去掉首尾静音**、**峰值归一化**。

---

## 五、技术实现

### 模型

| 模型 | 用途 | 显存 | 加载 |
|---|---|---|---|
| XTTS-v2 (Coqui) | 兜底克隆（单模型，无 OpenVoice 时） | 1.76 GB | 10.6s |
| OpenVoice V2 (Coqui) | **主力**：声音转换，保留口音 | ~1 GB | 首次 ~8s |

### 关键代码

| 文件 | 作用 |
|---|---|
| `backend/app/voice/openvoice_engine.py` | **新增**：OpenVoice 封装、音色登记、22.05k↔24k 重采样 |
| `backend/app/voice/tts.py` | `_cascade_chunks()` 级联路径 + `_cascade_ready()` |
| `backend/app/voice/xtts_engine.py` | XTTS 兜底 + 参考音频规范化 |
| `backend/app/voice/xtts_patch.py` | 绕开 torchcodec 的补丁 |
| `backend/app/routers/voice_api.py` | 上传/试听/绑定/删除，`accent_preserving` 能力标志 |
| `frontend/src/components/VoiceCloneDialog.tsx` | 上传界面（显示口音能力） |

### 合成优先级

```
0. cascade（OpenVoice 可用 且 有绑定音色）    ← 最优先：口音保留
1. xtts 直接克隆（OpenVoice 不可用时的兜底）  ← 音色对，口音丢
2. edge-tts（无绑定音色时的默认）             ← 真人声线，口音真实
3. tone（占位）
```

任何一环失败都会自动降级，**不会让整轮对话失败**。

### API

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/voice/status` | 可用性 + 音色列表 + **`accent_preserving`** + 角色口音表 |
| POST | `/api/voice/upload` | multipart：`file` + `voice_id` |
| POST | `/api/voice/preview/{voice_id}?persona_id=raj` | 试听（走级联，用该角色口音） |
| POST | `/api/voice/assign` | `{persona_id, voice_id}` 绑定 |
| DELETE | `/api/voice/{voice_id}` | 删除音色 |

---

## 六、踩过的坑

### 1. torchcodec ABI 不匹配

**症状**：
```
OSError: Could not load this library: ...libtorchcodec_core6.dll
```

**原因**：torch 2.9+ 起 `torchaudio.load()` 改用 torchcodec，而 torchcodec 的预编译
DLL（`0.17.0+cpu`）与 torch 2.11.0+cu128 ABI 不匹配，装系统 FFmpeg 也救不了。

**解决**：`xtts_patch.py` 把 `load_audio` 换成 soundfile 实现。

### 2. 口音全部丢失（最重要）

见本文第一节。最初的指标用 MFCC 判断"口音保留"，但 **MFCC 同时包含音色和口音**，
换音色必然改变 MFCC——指标本身是错的，得出了"保留 50%"的错误结论。
换成**归一化 F0 语调曲线相关性**（与音色近似独立的纯口音指标）后，真相才暴露。

### 3. hf_xet CAS 401

**症状**：`CAS Client Error: HTTP status client error (401 ...)`

**解决**：`HF_HUB_DISABLE_XET=1`

---

## 七、已知限制

| 限制 | 说明 |
|---|---|
| 音色不是精确复刻 | f0 通常到位 60–85%，是"像你"而非"就是你" |
| 延迟增加 | edge 网络请求 + VC 推理，比纯 XTTS 慢 |
| 22.05k 往返 | OpenVoice 原生 22.05k，重采样到 24k 后极高频略损 |
| 差异小的口音难分 | 英式 vs 美式这类相近口音，保留下来的差异有限 |
| 显存 | XTTS + OpenVoice 同时驻留约 2.8GB（16GB 无压力） |

---

## 八、数据与隐私

- 录音只存在**本机** `data/voices/<voice_id>.wav`
- 不会上传到任何服务器（XTTS / OpenVoice 完全本地推理）
- 删除音色会同时删掉文件
- ⚠️ **请只克隆你自己的声音，或已获得明确授权的声音**。克隆他人声音用于冒充属于违法行为。

