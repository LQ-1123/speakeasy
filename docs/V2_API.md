# SpeakEasy 2.0 · REST API 契约（`/api/v2`）

> 所有响应为 JSON（除音频文件/上传）。时间字符串为 UTC ISO8601。
> 前端以相对路径调用；无鉴权（本机应用）。

## 0. 通用对象

```jsonc
// Friend
{
  "id": "uuid", "name": "Marco", "avatar_seed": "m-8132", "archetype_id": "it_multilingual_male_01",
  "reply_language": "it",             // 好友回复语言（en/ja/ko/pt/es/de/fr/it/zh）；TTS 用对应声线
  "voice_id": "it-IT-GiuseppeMultilingualNeural", "voice_rate": "+0%", "voice_pitch": "+0Hz",
  "nationality": "Italy", "city": "Rome", "timezone": "Europe/Rome", "native_lang": "it",
  "language_mix_mode": "en_only", "age": 27, "occupation": "independent vinyl record store clerk",
  "personality_prompt": "...", "speech_quirks": ["allora", "dai"], "bio": "...",
  "status": "active",                 // active | pending_request
  "referred_by_friend_id": null, "closeness_level": 2, "total_turns": 12,
  "shared_episodes": ["2026-11-03: ..."], "open_loops": [{"event":"...","follow_up_after":"...","status":"pending"}],
  "last_interaction_at": "...", "created_at": "...",
  // 列表派生字段（后端计算，非 DB 列）：
  "local_time": "16:32", "local_period": "afternoon", "local_icon": "☕",
  "local_activity": "在店里忙着晚高峰", "unread": 2,
  "last_message": {"sender":"friend","transcript":"...","msg_type":"voice","duration_ms":5200,"created_at":"..."}
}

// Message
{
  "id": "uuid", "friend_id": "uuid", "sender": "user|friend", "msg_type": "voice|text|call_summary",
  "transcript": "...", "audio_path": "data/audio/xxx.wav", "audio_url": "/api/v2/audio/xxx.wav",
  "duration_ms": 5200, "suggestion": null | {"better_version":"...","explanation":"...","tag":"chunk","is_saved":false},
  "is_read": true, "context": null, "created_at": "..."
}
```

## 1. 好友

| 方法/路由 | 说明 |
|---|---|
| `GET /api/v2/bootstrap` | 一次性拉取：profile、词表数量、LLM 是否 mock、好友列表（同 friends）。用于首屏。首次无好友时**异步触发匹配 2 位好友**，返回 `pending_onboarding: true`，前端轮询 `/friends` 直到出现。 |
| `GET /api/v2/friends?status=` | 好友列表（默认 active）。含 pending_request 列表 `{"friends": [...], "requests": [...], "pending_onboarding": bool}`。首次无好友时后台生成 2 位反差好友（英音 + 意大利），前端轮询直到出现。 |
| `POST /api/v2/friends/discover` | body `{"nationality":"Japan","language":"ja","gender":"female"}`（都可选）→ `{"friend": Friend, "message": Message}`；新好友自动发第一条破冰语音条。**筛选严格生效**：不支持的组合返回 422 + 当前支持的国家/语种列表，不再静默换人。 |
| `GET /api/v2/friends/{id}` | 好友详情 + `memory`（episodes/open_loops）+ `request_message`。 |
| `POST /api/v2/friends/{id}/accept` | 通过好友申请（pending_request → active），返回 `{"friend":..., "message":...}`，并发第一条语音。 |
| `POST /api/v2/friends/{id}/decline` | 忽略申请（内部即删除该 pending 好友）。 |
| `DELETE /api/v2/friends/{id}` | **删除好友**：级联删除聊天记录/朋友圈/音频文件，返回 `{"ok":true,"friend_id","name","deleted_audio"}`；不存在返回 404。 |
| `GET /api/v2/friends/{id}/messages?limit=30&before=<iso>` | 历史消息（升序）。 |
| `POST /api/v2/friends/{id}/read` | 清未读。 |

## 2. 聊天（主力：异步语音条）

| 方法/路由 | 说明 |
|---|---|
| `POST /api/v2/chat/{friend_id}/voice` | multipart：`audio` 文件（webm/wav/ogg/mp3）、`duration_ms` 可选。返回见下。 |
| `POST /api/v2/chat/{friend_id}/text` | body `{"text":"...", "context": null}`。调试/无麦兜底。 |
| `POST /api/v2/chat/{friend_id}/idle` | 用户离开聊天窗时调用，**立即**触发记忆提炼（不等静默计时，异步返回）。 |
| `GET /api/v2/chat/{friend_id}/messages?limit=30&before=` | 轮询新消息/异步润色结果（同 friends messages）。 |

`voice`/`text` 响应：

```jsonc
{
  "user_message": Message,
  "friend_messages": [Message],        // 正常 1 条；长回复可 1..n 条（当前实现 1 条）
  "suggestion_pending": true,          // 润色批注异步生成，前端轮询 messages 获取
  "closeness_level": 2,
  "total_turns": 13,
  "memory_updated": false
}
```

## 3. 朋友圈

| 方法/路由 | 说明 |
|---|---|
| `GET /api/v2/moments` | `{"moments":[{...moment, "friend": {...}}], "generated_at":"..."}`；若今日为空自动返回空，前端可调 generate。 |
| `POST /api/v2/moments/generate` | 为 3~5 位活跃好友批量生成今日动态（幂等：同一天已有则直接返回）。 |
| `POST /api/v2/moments/{id}/comment` | multipart `audio`（或 JSON `{"text":...}`）→ 把动态作为 `context` 写入私聊用户消息 + 好友回复；返回 `{"friend_id":..., "moment":..., "reply": {...}}`。 |

## 4. 表达本 / 记忆 / 画像

| 方法/路由 | 说明 |
|---|---|
| `GET /api/v2/expressions?status=` | 表达本列表（learning/mastered）。 |
| `POST /api/v2/expressions` | `{"original_text","better_expression","explanation","source_friend_id"}` → expression。 |
| `PATCH /api/v2/expressions/{id}` | `{"status":"mastered"}` 等。 |
| `DELETE /api/v2/expressions/{id}` | 删除。 |
| `GET /api/v2/memories` | `{"profile": {...}, "fact_sheet": {...}, "friends":[{"id","name","shared_episodes","open_loops","closeness_level"}]}`。 |
| `PUT /api/v2/memories` | `{"nickname","native_language","target_languages","fact_sheet"}` 局部更新；fact_sheet 整体替换（用户手动纠错）。 |
| `PUT /api/v2/friends/{id}/memory` | `{"shared_episodes": [...], "open_loops": [...]}` 手动修正好友记忆。 |

## 4.5 个人设置（LLM / STT 热配置）

| 方法/路由 | 说明 |
|---|---|
| `GET /api/v2/settings` | 聚合 LLM + STT + TTS 当前状态（个人设置首屏）。 |
| `GET /api/v2/settings/stt` | STT 配置 + health + 预设（本地 Whisper / 硅基流动 SenseVoiceSmall）。 |
| `POST /api/v2/settings/stt` | 保存并**热生效**：`{provider, base_url, api_key?, model, language?, timeout_s?, verify}`；api_key 留空=不改，`__CLEAR__`=清空。 |
| `POST /api/v2/settings/stt/test` | 只测不存（缺省用当前配置，api_key 可覆盖）。 |
| `POST /api/v2/settings/stt/reset` | 删除 `data/stt_config.json`，回到 `.env`/默认。 |

完整字段与降级语义见 [`docs/V2_STT.md`](V2_STT.md)。LLM 配置沿用 1.0 的 `/api/llm/config|test|reset`。

## 5. 音频

| 方法/路由 | 说明 |
|---|---|
| `GET /api/v2/audio/{filename}` | 返回 `data/audio/<filename>`（wav/mp3），用于语音条播放。 |
| `GET /api/v2/tts/sample?text=...&voice=...` | 调试/试听：用指定 edge 声线合成一句 wav。 |
| `GET /api/v2/archetypes` | 声线原型底池（`archetypes / countries / languages`），供偶遇筛选下拉框。 |
| `GET /api/v2/health` | v2 健康状态（好友数、待处理申请、LLM 状态、引导是否进行中）。 |

## 6. 错误

`{"detail": "..."} `，HTTP 4xx/5xx。未知 friend → 404；LLM/TTS 失败 → 降级，不 500。

## 7. 前端集成要点

- 录音上传：`FormData` 字段名 `audio`；浏览器优先 `audio/webm;codecs=opus`，Safari 用 `audio/mp4`。
- 发送后立刻渲染本地语音气泡（`URL.createObjectURL(blob)` + 转写“识别中…”占位），响应到达后替换为服务端 Message。
- 好友语音条默认只显示时长/波形；点“文”展开 transcript；点倍速切换 0.8x。
- 润色：`message.suggestion` 非空时在用户气泡下方显示胶囊 `✨ 地道表达: ...`，展开含解释、🔊 朗读（`new Audio("/api/v2/tts/sample?...")`）、⭐ 收藏（POST expressions）。
- 聊天页每 2.5s 轮询一次 `GET .../messages?limit=30`（或增量 `before` 用最后一条时间），直到 suggestion 出现。
- 深链：`/?tab=chats|discover|notes`，`/?tab=chats&friend=<id>`；用于截图/调试。
- 好友列表轮询 10s；`GET /friends` 每次会检查到期 open loops（异步补发语音条）。
