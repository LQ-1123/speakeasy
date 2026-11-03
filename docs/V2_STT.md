# SpeakEasy 2.0 · STT 云端配置契约（SenseVoiceSmall @ SiliconFlow）

> 目标：默认可用本地 faster-whisper；在「个人设置」里填硅基流动 API Key 后，
> **STT 自动切到 SenseVoiceSmall（云端，中文/英文/多语种）**，无需重启。
> 密钥只写本机 `data/stt_config.json`（0600），任何接口不回传明文。

## 1. SiliconFlow 官方接口（已核对）

```
POST {base_url}/audio/transcriptions        # base_url 默认 https://api.siliconflow.cn/v1
Authorization: Bearer <SILICONFLOW_API_KEY>
Content-Type: multipart/form-data
fields:
  file:  <audio.wav>                        # 我们内部把 16k mono PCM16 包成 WAV
  model: FunAudioLLM/SenseVoiceSmall

200 → {"text": "transcribed text"}
400/401/404/429/503/504 → 错误 JSON
```

## 2. 运行时配置 `data/stt_config.json`

```jsonc
{
  "provider": "auto",                 // auto | local | siliconflow
  "base_url": "https://api.siliconflow.cn/v1",
  "model": "FunAudioLLM/SenseVoiceSmall",
  "api_key": "sk-...",                // 只落盘；接口脱敏
  "language": "en",
  "timeout_s": 30
}
```

语义：
- `provider="local"` → 永远本地 faster-whisper（云端关闭）。
- `provider="siliconflow"` → 优先云端，失败自动回退本地（不报错给用户，只记 health.last_error）。
- `provider="auto"`（默认）→ 有 key 就云端、无 key 用本地。
- 优先级：文件 > `.env`（`STT_PROVIDER` / `STT_SILICONFLOW_API_KEY` / `STT_SILICONFLOW_BASE_URL` / `STT_SILICONFLOW_MODEL`）> 内置默认。

## 3. 后端接口（新增，挂在 `/api/v2/settings`）

### `GET /api/v2/settings`
聚合当前配置（供个人设置首屏）：
```jsonc
{
  "llm": {"config": {...}, "health": {...}, "presets": [...]},   // 复用 /api/llm/config 的返回
  "stt": {
    "config": {
      "provider": "auto", "enabled": false,
      "base_url": "https://api.siliconflow.cn/v1",
      "model": "FunAudioLLM/SenseVoiceSmall",
      "language": "en", "timeout_s": 30,
      "api_key_set": false, "api_key_masked": "",
      "configured": false, "source": "env"
    },
    "health": {
      "enabled": false, "provider": "auto", "configured": false,
      "backend": "faster-whisper",          // 当前实际生效 STT 后端（cloud 生效时为 sensevoice）
      "model": "FunAudioLLM/SenseVoiceSmall",
      "last_error": null, "last_latency_ms": null,
      "requests": 0, "last_success_at": null
    }
  }
}
```

### `GET /api/v2/settings/stt`
只取 STT 部分：`{"config": {...}, "health": {...}, "presets": [...]}`；
`presets` 固定两条：
```json
[{"id":"local","name":"本地 Whisper（离线）","base_url":"","model":"distil-large-v3"},
 {"id":"siliconflow","name":"硅基流动 SenseVoiceSmall","base_url":"https://api.siliconflow.cn/v1","model":"FunAudioLLM/SenseVoiceSmall"}]
```

### `POST /api/v2/settings/stt`
```jsonc
{ "provider": "siliconflow", "base_url": "https://api.siliconflow.cn/v1",
  "api_key": "sk-...", "model": "FunAudioLLM/SenseVoiceSmall",
  "language": "en", "timeout_s": 30, "verify": true }
```
- `api_key` 省略/空串 = 不改；`"__CLEAR__"` = 清空。
- 保存后**立即热生效**（无需重启），返回：
```jsonc
{ "ok": true, "config": {...}, "health": {...},
  "verified": {"ok": true, "message": "连接成功，SenseVoiceSmall 正常响应", "latency_ms": 820.5, "text": ""} }
```
- `verify=true` 时真的发一次 0.5s 静音/提示音转写请求；失败 `ok` 仍为 true（配置已保存），`verified.ok=false`。

### `POST /api/v2/settings/stt/test`
只测不存，字段缺省用当前配置：`{provider?, base_url?, api_key?, model?, language?, timeout_s?}`
→ `{"ok": bool, "message": "...", "latency_ms": number, "text": "..."}`

### `POST /api/v2/settings/stt/reset`
删除 `data/stt_config.json`，回到 `.env`/默认；返回 `{"config", "health"}`。

## 4. STTEngine 集成契约（voice 负责人）

`backend/app/voice/sensevoice.py`：
```python
class SenseVoiceError(RuntimeError): ...
class SiliconFlowSTT:
    def __init__(self, *, base_url: str, api_key: str, model: str,
                 timeout_s: float = 30.0, language: str = "en", transport=None) -> None
    @property
    def configured(self) -> bool
    async def transcribe(self, pcm16: bytes, sample_rate: int = 16000) -> Transcript
        # 16k mono PCM16 → WAV(BytesIO) → POST multipart → {"text":...}
        # 失败抛 SenseVoiceError（含 HTTP 状态/可读原因）
    async def verify(self) -> dict
        # {"ok","message","latency_ms","text"}；内部生成 0.5s 提示音 WAV 发一次
```
`STTEngine`（`backend/app/voice/stt.py`）新增：
```python
def apply_cloud_config(self, cfg) -> None      # cfg 对象或 dict：provider/enabled/base_url/api_key/model/language/timeout_s
def cloud_health(self) -> dict                 # 见上 health 结构 + backend 名称
```
`transcribe()` 顺序：云端已启用且已配置 → 先试云端（成功返回 `Transcript(backend="sensevoice-siliconflow")`）
→ 失败记 `last_error` 并**继续现有本地流程**（faster-whisper → mock），绝不抛。
`backend_name`：云端生效时返回 `"sensevoice"`，否则保持现状（不改 1.0 语义）。

## 5. 前端「个人设置」契约（frontend 负责人）

- 入口：顶部栏新增 ⚙（`Settings` 图标，三 Tab 都有），点击打开全屏「个人设置」页（可返回）。
- 分区 1「对话模型 LLM」：base_url / api_key（password，占位显示脱敏值）/ model；
  预设下拉（DeepSeek / OpenAI / SiliconFlow / 自定义，取自 `/api/llm/config` 的 presets）；
  「测试连接」`POST /api/llm/test`；「保存并启用」`POST /api/llm/config {verify:true}`；「恢复 .env」`POST /api/llm/reset`。
- 分区 2「语音识别 STT」：provider 三选一（自动 / 本地 Whisper / 硅基流动 SenseVoiceSmall）；
  base_url、api_key、model（选中硅基流动时预填默认值）；「测试连接」`POST /api/v2/settings/stt/test`；
  「保存」`POST /api/v2/settings/stt {verify:true}`；「恢复默认」reset。
  保存后展示当前 health：`backend / model / last_error`。
- 密钥输入框永远为空（只有占位 `已设置：sk-a…2345`），保存时留空=不改。
- 保存成功后刷新 bootstrap + toast；失败显示可读错误（401/网络超时）。
- 390px 下可滚动、不横向溢出；`npm run typecheck` 与 `npm run build:backend` 必须通过。

## 6. 实现状态（本机已验证）

- ✅ `SiliconFlowSTT` 客户端（WAV 封装 / multipart / 401/超时/非 JSON 可读错误 / verify）
- ✅ `STTEngine.apply_cloud_config` 热切换 + 云端失败自动回退本地 + `cloud_health`
- ✅ `data/stt_config.json`（0600）+ GET/POST/test/reset 端点 + 掩码展示（`sk-d…5678`）
- ✅ 个人设置页（LLM + STT 分区、脱敏密钥、测试/保存/恢复/清除、health）
- ✅ 真实 SiliconFlow 请求验证：假 Key → `HTTP 401 {"code":30014,"message":"Token is invalid."}`，UI 中文提示；
  语音条随后回退本地 faster-whisper 并正确转写。
- ⏳ 真实 Key 下的转写效果需用户填入自己的 SiliconFlow Key 验证（本机未持有可用 Key）。

## 7. 常见报错对照（SiliconFlow 官方错误码）

| HTTP | 含义 | 处理 |
|---|---|---|
| 401 | API Key 无效/没带 Bearer | 重新粘贴 Key |
| **402 / code 30001** | **账户欠费/余额不足**（即使模型价格页标注"免费"，账户欠费或免费额度受限时仍会被拦） | 控制台完成实名认证 + 充值少量余额后重试；期间可在「个人设置」切「本地 Whisper」 |
| 403 | 权限不足，常见原因：未完成实名认证 | 控制台完成实名认证 |
| 429 | 触发 Rate Limits（免费模型有固定限额） | 稍等重试，或切本地 |
| 503/504 | 服务负载 | 稍后重试 |

**本项目的降级行为**：云端任何 4xx/5xx/超时都不会中断聊天——`STTEngine` 记下 `cloud_health.last_error`
后自动回退本地 faster-whisper；设置页 health 会显示最近错误与请求数。
