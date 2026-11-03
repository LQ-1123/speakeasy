# WebSocket / HTTP 协议（权威）

端点：
- WS：`ws://127.0.0.1:8000/ws`（同源页面用 `ws(s)://<host>/ws`）
- HTTP：`/api/*`（见文末）
- 静态：`/` → `backend/app/static/app/index.html`（若已构建），否则 `backend/app/static/standalone.html`

传输约定：
- **文本帧 = JSON**（控制/文本消息）
- **二进制帧 = 原始 PCM16LE 单声道 16 kHz 音频**（麦克风上行，无包头、无编码）
- 音频下行用 JSON + base64（便于调试与逐句对齐；本地回环开销可忽略）

---

## 1. 客户端 → 服务端

### 1.1 `hello`（连接后第一条，必须）

```json
{
  "type": "hello",
  "persona_id": "oliver",
  "input_sample_rate": 16000,
  "mode": "free",
  "client": "web",
  "protocol_version": 1
}
```

`mode` 可选（`free` | `task` | `drill`），缺省 `free`。
服务端回 `ready`。若 `persona_id` 不存在 → `error` 后关闭。

### 1.2 二进制音频帧
每帧建议 20–40 ms（320–640 samples @16k）。服务端内部缓冲，交给流式 VAD。
可以是任意长度，但**不要**在中间重采样；`input_sample_rate` 必须是 16000（其它值服务端会拒绝并报错）。

### 1.3 `audio_end`
用户手动结束本句（例如松开"说话"按钮）。触发 VAD `flush()`。
```json
{ "type": "audio_end" }
```

### 1.4 `text`
跳过 STT，直接送文本（调试 / 无麦克风环境 / 单元测试）。
```json
{ "type": "text", "text": "Could I get a flat white, please?" }
```

### 1.5 `interrupt`
打断（barge-in）：取消当前 LLM 流与 TTS 队列，丢弃未发送音频。
```json
{ "type": "interrupt" }
```

### 1.6 `set_persona`
```json
{ "type": "set_persona", "persona_id": "raj" }
```
不影响历史对话；仅切换后续回合的 system prompt 与音色。
成功回 `{"type":"persona_changed","persona_id":"raj"}`；未知 id 回 `error{code:"persona_not_found"}`。

### 1.6b `set_mode`
```json
{ "type": "set_mode", "mode": "task" }
```
合法值 `free` | `task` | `drill`。成功回 `{"type":"mode_changed","mode":"task"}`；
非法值回 `{"type":"error","code":"bad_message"}`，连接保持。

### 1.7 `suggestion`
请求对指定（或最近）用户语句做润色建议。**前端也可不显式请求**：服务端在每轮结束后自动异步推送。
```json
{ "type": "suggestion", "text": "I very like this coffee" }
```
`text` 省略时使用最近一条用户语句。

### 1.8 `ping`
```json
{ "type": "ping", "t_ms": 1717000000000 }
```

---

## 2. 服务端 → 客户端

### 2.1 `ready`
```json
{
  "type": "ready",
  "session_id": "3f6c...",
  "protocol_version": 1,
  "persona_id": "oliver",
  "mode": "free",
  "personas": [ { "id": "oliver", "name": "Oliver", "accent": "British RP", "...": "..." } ],
  "modes": [
    { "id": "free", "name": "自由闲聊", "description": "..." },
    { "id": "task", "name": "任务达成", "description": "..." },
    { "id": "drill", "name": "表达特训", "description": "..." }
  ],
  "engines": {
    "stt": "faster-whisper:distil-large-v3",
    "tts": "f5tts",
    "llm": { "configured": true, "model": "deepseek-chat", "base_url": "https://api.deepseek.com/v1" }
  },
  "audio": { "format": "pcm_s16le", "sample_rate": 24000, "channels": 1 }
}
```

### 2.2 `vad`
```json
{ "type": "vad", "state": "speech_start" }
{ "type": "vad", "state": "speech_end", "speech_ms": 2140 }
```

### 2.3 `user_text`
```json
{ "type": "user_text", "turn_id": 3, "text": "i want a flat white",
  "latency": { "stt_ms": 182.4 }, "truncated": false }
```

### 2.4 `assistant_delta`
LLM 增量（原文，未断句）：
```json
{ "type": "assistant_delta", "turn_id": 3, "text": "Righ" }
```

### 2.5 `assistant_sentence`
一句话断句完成，即将进入 TTS：
```json
{ "type": "assistant_sentence", "turn_id": 3, "sentence_index": 0,
  "text": "Right you are — one flat white coming up.",
  "tts_backend": "f5tts" }
```

### 2.6 `audio`
```json
{
  "type": "audio", "turn_id": 3, "sentence_index": 0, "seq": 0,
  "format": "pcm_s16le", "sample_rate": 24000, "channels": 1,
  "data": "<base64>", "is_first": true, "is_last": false
}
```
- `seq` 在**同一 turn 内全局递增**（跨句连续），前端据此排序/检测丢包。
- 前端应维护 `next_seq`，按序解码进 `AudioBuffer`；`sentence_index` 用于字幕高亮。
- `is_last && sentence_index == last` 之后跟 `turn_audio_end` 便于前端收尾。

### 2.7 `turn_audio_end`
```json
{ "type": "turn_audio_end", "turn_id": 3, "total_bytes": 187392, "sentences": 3 }
```

### 2.8 `assistant_done`
```json
{
  "type": "assistant_done", "turn_id": 3,
  "text": "Right you are — one flat white coming up. That'll be three eighty.",
  "metrics": {
    "stt_ms": 182.4,
    "llm_ttft_ms": 412.0,
    "first_audio_ms": 980.5,
    "total_ms": 2410.0,
    "tts_first_sentence_ms": 340.0,
    "sentences": 2,
    "audio_bytes": 187392
  }
}
```
`first_audio_ms` = 从收到 `user_text`（或 STT 完成）到**第一条 audio** 推送的时间，是核心 SLO 指标（目标 < 1000 ms）。

### 2.9 `suggestion`
```json
{
  "type": "suggestion", "turn_id": 3,
  "items": [
    { "original": "I very like this coffee",
      "better": "I'm really into this coffee",
      "why": "very 不能直接修饰动词；用 really/so 或 be into 更地道",
      "register": "casual" }
  ]
}
```
异步、可晚于 `assistant_done` 到达；前端**不得**因其阻塞音频播放。

### 2.10 `error`
```json
{ "type": "error", "code": "llm_not_configured",
  "message": "未配置 LLM_API_KEY，请在 .env 中设置", "fatal": false, "turn_id": 3 }
```
`code` 枚举：`bad_message` `unsupported_sample_rate` `llm_not_configured` `llm_failed`
`stt_failed` `tts_failed` `vad_failed` `persona_not_found` `internal`。
`fatal: true` 时服务端将随后关闭连接（当前实现仅在协议初始握手失败时使用）。

### 2.11 `pong`
```json
{ "type": "pong", "t_ms": 1717000000000, "server_time": 1717000000050 }
```

### 2.12 `mode_changed` / `persona_changed`（回执）
```json
{ "type": "mode_changed", "mode": "task" }
{ "type": "persona_changed", "persona_id": "raj" }
```

### 2.13 `session_info`（可选，调试）
```json
{ "type": "session_info", "session_id": "...", "turns": 4, "persona_id": "oliver" }
```

---

## 3. HTTP API

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/health` | `{"status":"ok","engines":{...},"llm":{...},"gpu":{...}}`，模型加载中为 `"loading"` |
| GET | `/api/personas` | 角色列表（公开字段，不含 prompt 全文） |
| GET | `/api/config` | 前端需要的非敏感配置（采样率、是否配置 LLM、可用 TTS 后端） |
| GET | `/api/metrics/last` | 最近回合延迟分解（无则为 `{}`） |
| POST | `/api/echo` | `{"text","persona_id"}` → `{"sentences":["..."],"reply_preview":"..."}` 仅文本链路，用于无麦调试 |
| POST | `/api/suggest` | `{"text","persona_id"}` → 同步返回 `{"items":[...]}` |
| GET | `/api/ref-audio/{persona_id}` | 返回参考音频 wav（若存在），用于前端试听 |

### 3.1 LLM 显式配置（v0.2 新增）

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/llm/config` | 当前配置（**key 脱敏**）+ 供应商预设 + 健康状态 |
| POST | `/api/llm/config` | 保存并**立即生效**（无需重启），可选校验 |
| POST | `/api/llm/test` | 只测连通性，不保存 |
| POST | `/api/llm/reset` | 删除运行时配置，回到 `.env` |

**GET /api/llm/config** 响应：
```json
{
  "config": {
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "temperature": 0.85,
    "max_tokens": 220,
    "suggestion_model": "",
    "api_key_set": true,
    "api_key_masked": "sk-a…2345",
    "configured": true,
    "source": "file"
  },
  "health": { "configured": true, "model": "deepseek-chat", "base_url": "..." },
  "presets": [
    { "id": "deepseek", "name": "DeepSeek", "base_url": "...", "model": "...",
      "key_hint": "sk-...", "note": "国内直连" }
  ],
  "note": "密钥只保存在本机 data/llm_config.json，接口永不回传明文。"
}
```

**POST /api/llm/config** 请求：
```json
{
  "base_url": "https://api.deepseek.com/v1",
  "api_key": "sk-...",
  "model": "deepseek-chat",
  "temperature": 0.85,
  "verify": true
}
```

约定：
- 所有字段可选，只改传了的（部分更新）。
- `api_key` 传空字符串 = **不改动**已保存的；传 `"__CLEAR__"` = 清除。
- `verify: true`（默认）会在保存后**真的发一次极小请求**验证。

响应：
```json
{
  "ok": true,
  "message": "已保存并生效",
  "config": { "...": "同 GET 的 config" },
  "verified": { "ok": true, "message": "连接成功，模型 deepseek-chat 正常响应",
                "latency_ms": 412.3, "model_reply": "OK" }
}
```

连不上时 `ok: false` 但**配置仍然保存**（用户可以稍后修 key 再点保存）：
```json
{
  "ok": false,
  "message": "配置已保存，但连接测试失败：API Key 无效或无权限（AuthenticationError）：...",
  "config": { "...": "已更新" },
  "verified": { "ok": false, "message": "...", "latency_ms": 591.2, "model_reply": "" }
}
```

**POST /api/llm/test** 请求（未传字段用当前已保存值）：
```json
{ "base_url": "...", "api_key": "...", "model": "..." }
```
响应：`{"config_preview": {...}, "ok": bool, "message": str, "latency_ms": float, "model_reply": str}`

---

## 4. 前端实现要求（对齐本协议）

1. `AudioContext` 以 16 kHz 采集（`new AudioContext({sampleRate:16000})`，必要时用 `OfflineAudioContext`/`AudioWorklet` 重采样到 16k），
   `AudioWorkletProcessor` 里把 `Float32 → Int16` 并每 20 ms `port.postMessage` 一次 → WS `send(ArrayBuffer)`。
2. 播放：收到 `audio` 帧 → base64 解码 → `Int16 → Float32` → 按 `seq` 排序后连续排入 `AudioBufferSourceNode`，
   保持 200–400 ms 的 jitter buffer；`seq` 跳号时插入静音并计数。
3. 波形：麦克风用 `AnalyserNode`（时域）画上行波形；播放端可画下行音量条。
4. 打断：用户开口（本地能量检测）或点"打断"按钮 → 停播并 `send({"type":"interrupt"})`。
5. 界面必须显示：角色选择、连接状态、波形、对话记录、`first_audio_ms` 等实时指标、润色建议卡片（异步）。
6. 无麦克风时提供"文字输入"框（走 `text` 消息），保证任何环境可演示。
