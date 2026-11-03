# SpeakEasy 2.0 · 移动端拟真跨国语伴（开发指南）

> 产品需求见 PRD v2.0.0；接口契约见 [`docs/V2_API.md`](V2_API.md)，内部模块契约见 [`docs/V2_ARCHITECTURE.md`](V2_ARCHITECTURE.md)。
> 本文件面向"接手/部署/验收"的人：怎么跑、做了什么、怎么测、还有什么坑。

---

## 1. 一句话

在 1.0 的实时语音能力（`/ws` + StreamingVAD + faster-whisper + 流式断句 + edge-tts）之上，
新增 **SQLite 持久层 + 随机捏人 + 双层长期记忆 + 社交熟稔度 + 朋友圈 + 移动端三 Tab IM**，
做成"跨国微信"式的异步语音条外语社交练习 App（PWA）。

## 2. PRD → 实现对照

| PRD 机制 | 实现 | 代码 |
|---|---|---|
| 声线原型底池（20+ 原型、Multilingual 声线） | 20 个原型（10 国 × 男女），含 8 个 `*MultilingualNeural`、城市池、IANA 时区、名字/职业/口头禅池 | `backend/app/personas/voice_archetypes.json`、`backend/app/v2/voices.py` |
| LLM 随机捏人 + 固定声线听感 | `PersonaSpawner`：LLM JSON 生成 + mock/失败确定性兜底；原型去重；referral 支持 | `backend/app/v2/spawner.py` |
| 好友获取（引导 / 裂变 / 偶遇） | 首启自动 2 位反差好友（英音 + 意大利）；15 轮后好友在回复里介绍新朋友（pending_request + 验证消息）；`POST /friends/discover` 抽卡 | `v2/router.py`、`chat_service.py` |
| 熟稔度 Lv1–5 | 10/30/80/150 轮阈值；等级注入 prompt 语气；UI 显示"初识/常聊/熟人…" | `backend/app/v2/social.py` |
| 双层长期记忆 | 全局 `UserFactSheet`（profile/interests/weaknesses/recent_events，≤~300 tokens）+ 好友 `shared_episodes`（≤8）+ `open_loops`（到期好友主动追问） | `backend/app/v2/memory.py`、`db.py` |
| 时区生活状态注入 | 每次 prompt 注入好友当地时间/作息/`activity_en`；UI 显示时刻 + 🌅☕💻📱 等状态 | `backend/app/v2/timeutil.py` |
| 朋友圈 Moments | 一次批量 LLM 为活跃好友生成 15–35 词动态 + mood_tag + 当地时刻；同一 feed_date 幂等；语音评论转私聊 | `backend/app/v2/moments.py`、`v2/router.py` |
| 异步语音条主链路 | 上传 webm/wav/mp4 → 解码 16k → STT → 记忆上下文 → LLM → edge-tts wav → 落库；旁路异步润色批注、记忆提炼、表达本实战检测 | `backend/app/v2/chat_service.py`、`suggestion.py`、`audio_store.py` |
| 气泡内联批注 | 用户气泡下 `✨ 地道表达` 胶囊：better_version/解释/tag、🔊 试听、⭐ 收藏；无 key 时规则库兜底 | `suggestion.py`、`frontend/src/components/v2/SuggestionPill.tsx` |
| 表达本刻意复现 | learning 表达每轮抽 1–2 条注入 prompt；用户实战说出 → mastery+1，满 3 次 mastered | `chat_service.py`、`v2/router.py` |
| 实时语音通话 | 1.0 的 `/ws` 通道与 `/classic` 页面保留可用（新 UI 暂以"即将上线"占位，接口未破坏） | `backend/app/routers/ws.py`、`frontend/src/app/classic` |
| 多语言好友（ja/ko/pt/es-MX…） | 28 个声线原型（14 国 × 男女）；「偶遇」国家/语种筛选**严格生效**（无匹配返回 422 + 支持列表）；好友按 `reply_language` 用该语言回复，TTS 用对应语言声线；**回复语言有后置校验/纠偏**（跑偏成英文时自动重写，仍失败则用目标语言模板兜底，绝不发错语言）；日语/韩语等语音输入走多语言 STT 模型或云端 SenseVoice | `voice_archetypes.json`、`spawner.py`、`chat_service.py`、`DiscoverTab.tsx` |
| 云端 STT（SenseVoiceSmall） | 个人设置里填硅基流动 Key 即热切换到 `FunAudioLLM/SenseVoiceSmall`；云端失败自动回退本地 Whisper→mock | `backend/app/voice/sensevoice.py`、`stt_config.py`、`v2/router.py`、`frontend/src/components/v2/SettingsPanel.tsx` |
| 聊天内翻译 | 每条消息「译」按钮（好友消息→中文、自己消息→对方语言）+ ⋯「翻译最近消息」批量对照；**朋友圈动态每条可「译」、支持「全部翻译」**；复用 LLM Key，无 Key 时明确提示 | `v2/translate.py`、`ChatView.tsx`、`DiscoverTab.tsx` |
| 删除好友 | 单聊右上角「⋯」→「删除好友」（二次确认），后端级联删除消息/朋友圈/音频并清理推荐关系 | `router.py`、`ChatView.tsx` |
| 移动端三 Tab + PWA | 消息 / 动态·偶遇 / 表达本·画像；微信式浅色；按住说话；PWA manifest+SW+图标 | `frontend/src/components/v2/*`、`frontend/public/*` |
| SQLite 5 表 | `user_profile` / `friends` / `messages` / `moments` / `saved_expressions`（额外列见 db.py） | `backend/app/v2/db.py` |
| REST API | `/api/v2/bootstrap|friends|chat|moments|expressions|memories|audio|tts` | `backend/app/v2/router.py`、`docs/V2_API.md` |

## 3. 快速启动

```bash
cd repo

# 1) 后端（依赖见 1.0 的 venv；v2 不新增 Python 依赖）
/opt/homebrew/bin/python3.11 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
.venv/bin/pip install edge-tts          # 1.0 默认 TTS 后端，不在 requirements 里
cp -n .env.example .env                 # 可选：填 LLM_API_KEY 获得真实对话

# 2) 启动（首次会从 hf-mirror 下载 STT 模型约 1.4G；沙箱环境把 HF_HOME 指到仓库内）
HF_HOME="$PWD/.cache/hf" .venv/bin/python -m uvicorn app.main:app --app-dir backend --port 8000
```

打开 <http://127.0.0.1:8000>（手机尺寸更好看；手机浏览器可"添加到主屏幕"变全屏 PWA）。
前端源码改动后：`cd frontend && npm install && npm run build:backend`（产物自动拷到 `backend/app/static/app/`）。

**演示模式**：不填 `LLM_API_KEY` 时 LLM 走本地规则（人员生成、记忆提炼、润色、朋友圈都有确定性兜底），
TTS 走 edge-tts（需联网，失败退 tone），STT 走 faster-whisper（CPU 可用）。UI 顶部会显示「演示模式」。
填 Key：页面右上角 **⚙ 个人设置**：
- **对话模型 LLM**：Base URL / API Key / 模型名（DeepSeek、OpenAI、SiliconFlow、自定义），保存即热生效；
- **语音识别 STT**：选「硅基流动 SenseVoiceSmall」→ 填 SiliconFlow API Key（默认 `https://api.siliconflow.cn/v1` +
  `FunAudioLLM/SenseVoiceSmall`），保存即热切换；云端失败自动回退本地 Whisper。密钥只存 `data/stt_config.json`（0600），
  接口只回传脱敏指纹。
也可写 `.env`：`STT_PROVIDER=siliconflow`、`STT_SILICONFLOW_API_KEY=...`（扁平项）。

环境变量补充：`V2_MEMORY_IDLE_S`（默认 120，聊天静默多久后提炼记忆；调 0 便于演示/测试）。
数据库/音频默认在 `repo/data/`（`speakeasy_v2.db`、`audio/`），删掉即可重置。

## 4. 数据与记忆流

1. 用户发语音条 → `AudioStore.decode_upload` 解码 16k PCM → `faster-whisper` 转写；
2. `MemoryManager.build_context` 组装：好友当地时间/作息、熟稔度语气、UserFactSheet、
   SharedEpisodes、OpenLoops、待复习表达 → 作为 system prompt 的一部分；
3. LLM 生成 1–2 句回复 → edge-tts 合成 wav → 消息落库（好友消息未读）；
4. 响应后异步：润色批注（`messages.suggestion`）、记忆提炼（≥3 轮 + 静默 120s / 调用 `/idle`）、
   表达本实战检测、15 轮裂变推荐；
5. `GET /friends` 会检查到期 open loop 并在后台补发好友语音条（主动破冰）。

## 5. 验收结果（本机 macOS / Apple Silicon）

- 后端测试：`pytest backend/tests -q` → **355 passed**（1.0 回归 174 + v2 新增 153，含 SenseVoice 云端 STT 34 个离线用例）。
- 前端：`npm run typecheck` ✅、`npm run build:backend` ✅（36 文件 / 16 目录 → `backend/app/static/app`）。
- 真实链路（无 LLM key）：
  - 首启 onboarding 生成英音 + 意大利好友并各发一条 edge-tts 破冰语音；
  - 语音条上传（3.6s wav）→ CPU STT 逐字正确 → 好友语音回复 + 异步润色胶囊；
  - 表达收藏 → 再次说出表达 → mastery 计数；朋友圈生成 + 语音/文字评论转私聊；
  - 15 轮裂变生成 pending 好友申请；到期 open loop 自动补发追问；
  - `POST /discover` 偶遇新好友（真实 edge 声线）。
  - **云端 STT（SenseVoiceSmall）**：个人设置保存 SiliconFlow Key → 热生效为 `sensevoice`；用假 Key 打真实接口返回 401
    （UI 中文提示），语音条仍自动回退本地 faster-whisper 且转写正确；`cloud_health` 记录 `last_error/requests`。
    真 Key 的实际转写需用户填入自己的 SiliconFlow Key 验证（本机未持有可用 Key）。
- 浏览器（Puppeteer + 系统 Chrome，390×844 / 375×844）：三 Tab、单聊、胶囊展开、朋友圈、
  记忆卡全部正常；`innerWidth == scrollWidth` 无横向溢出；无 JS 异常。
- 截图见 `artifacts/v2-final-*.png`（主界面）与 `artifacts/v2-settings-filled.png` / `v2-settings-stt.png`（个人设置 / STT 401 回退）。

## 6. 已知限制

1. **无 LLM key 时内容是规则生成的**：能完整演示流程与状态，但对话/朋友圈质量不如真实模型；
   配 DeepSeek（或任意 OpenAI 兼容端点）后自动切换。
2. **TTS 依赖 edge-tts 联网**（冷启动 2–3s/句，命中缓存后 0ms）；断网自动退 `tone`（可听但不自然）。
   本地零样本克隆（XTTS/F5/CosyVoice）本机未安装且 CPU 不实用，沿用 1.0 结论。
3. **本机 STT 为 CPU**（distil-large-v3 int8）：3.6s 音频约 2.8s 转写；GPU 机器快得多。
4. 实时语音电话入口在新 UI 里暂为占位；1.0 的 `/ws` 实时通道与 `/classic` 页面仍保留可用。
5. `V2_MEMORY_IDLE_S` 默认 120s，因此刚聊完几轮不会立刻在记忆卡出现新事实（可调 0 或调 `/idle`）。
6. Safari 录音为 `audio/mp4`，后端解码路径已支持但未在 Safari 真机验证；无麦克风时自动切文字输入。
7. PWA 安装需要 `localhost` 或 HTTPS 安全上下文；iOS 使用"添加到主屏幕"。
8. 本机 `edge-tts` 与 `huggingface` 下载受网络影响；HF 下载走 `.env` 的 `HF_ENDPOINT=hf-mirror.com`。

## 7. 目录速查

```
backend/app/v2/
  db.py             SQLite 5 表 + CRUD
  timeutil.py       好友时区/作息状态
  voices.py         声线原型 + 好友语音合成（edge→tone 兜底）
  spawner.py        LLM 捏人 + 确定性兜底
  memory.py         双层记忆组装/提炼/open loops
  social.py         熟稔度/裂变
  moments.py        朋友圈批量生成
  suggestion.py     气泡润色（规则 + LLM）
  audio_store.py    上传解码/落盘/静态服务
  chat_service.py   异步语音条主链路
  router.py         /api/v2/* 全部路由
  services.py       服务装配（挂在 app.state.v2）
frontend/src/components/v2/   10 个移动端组件
frontend/src/lib/v2/          API/类型/录音/格式化
frontend/public/              manifest.webmanifest、sw.js、icons/
backend/tests/test_v2_*.py    108 个 v2 离线测试
```
