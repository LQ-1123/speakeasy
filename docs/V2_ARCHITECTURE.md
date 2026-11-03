# SpeakEasy 2.0 · 架构与协作契约（v2.0.0）

> 本文件是并行开发的**权威接口定义**。所有模块以此为准；改动接口必须先改本文件。
> 产品需求见用户提供的 PRD（v2.0.0）。目标：在 1.0 后端（FastAPI + WS + edge-tts + faster-whisper）之上
> 增加 SQLite 持久层、捏人/记忆/社交/朋友圈机制与移动端 IM 前端，**不破坏** 1.0 的 `/api/*` 与 `/ws`。

## 1. 目录与归属

| 文件/目录 | 负责人 | 内容 |
|---|---|---|
| `backend/app/v2/db.py` | Lead | SQLite 数据层（5 张表 + CRUD） |
| `backend/app/v2/timeutil.py` | Lead | 时区/当地时间/作息状态 |
| `backend/app/v2/llm_json.py` | Lead | JSON 补全容错工具 |
| `backend/app/v2/voices.py` | voice | 声线原型加载 + 每条好友语音合成（edge 主 / tone 兜底）+ wav 落盘 |
| `backend/app/personas/voice_archetypes.json` | voice | 20+ 声线原型底池 |
| `backend/app/v2/spawner.py` | voice | PersonaSpawner：LLM 捏人 + 确定性兜底 + 入库 |
| `backend/app/v2/memory.py` | memory | 双层记忆：上下文组装、后台提炼、open loops |
| `backend/app/v2/social.py` | memory | 熟稔度状态机、好友裂变判定 |
| `backend/app/v2/moments.py` | memory | 朋友圈批量生成 + 时区展示 |
| `backend/app/v2/suggestion.py` | Lead | 气泡内联润色（含确定性兜底） |
| `backend/app/v2/chat_service.py` | Lead | 语音条主链路：STT→记忆→LLM→TTS→落库 |
| `backend/app/v2/audio_store.py` | Lead | 上传音频解码/落盘、TTS wav 存储、静态服务 |
| `backend/app/v2/router.py` | Lead | `/api/v2/*` 全部路由 |
| `backend/app/v2/schemas.py` | Lead | 请求/响应 Pydantic 模型 |
| `frontend/src/**`、`frontend/public/**` | frontend | 移动端三 Tab UI + PWA；构建产物由 `npm run build:backend` 拷到 `backend/app/static/app/` |
| `backend/tests/test_v2_*.py` | 各自 | 离线单测（不得依赖网络/真实 LLM key） |

## 2. 全局约定

1. **不新增 Python 依赖**：SQLite 用标准库 `sqlite3`；时间用 `zoneinfo`；音频解码复用
   `app.voice.audio_utils.read_audio_file / write_wav`。数据库同步方法由 API 层用
   `asyncio.to_thread` 调用，不要自己开线程池。
2. **离线可测**：LLM 未配置 key 时 `engines.llm.uses_mock` 为真。所有 LLM 结构化调用**必须**
   提供确定性兜底（模板/规则），保证无网络、无 key 时功能与测试仍完整。
3. **TTS 可降级**：优先 edge-tts（`EdgeTTSEngine.synthesize_voice` 扩展 rate/pitch），
   失败时用 `app.voice.tone_tts` 生成占位音频，**绝不让接口 500**。单测 monkeypatch voice 层。
4. **时间**：数据库内 `created_at` 一律 UTC ISO8601（`datetime.now(timezone.utc).isoformat()`）。
   展示层用好友 `timezone` 转当地时间。
5. **JSON 字段形状**（存 DB 的 TEXT JSON）：
   - `fact_sheet = {"profile": {"nickname","city","major_or_job","routine"},
     "interests": ["..."], "language_profile": {"target":"en","weaknesses":["..."]},
     "recent_events": ["..."]}`
   - `shared_episodes = ["2026-11-03: 聊了他在做的软件工程项目和家里的牡丹鹦鹉", ...]`（≤8 条，单句）
   - `open_loops = [{"event":"周四下午有场技术面试","follow_up_after":"2026-11-05T18:00:00+08:00","status":"pending"}]`
   - `messages.suggestion = {"better_version":"...","explanation":"...","tag":"grammar|chunk|tone","is_saved":false}` 或 null
   - `speech_quirks = ["lowkey","deadass"]`
6. **测试要求**：每个模块提供 `backend/tests/test_v2_<module>.py`，全部离线、`pytest -q` 通过。
   TTS/STT 用 mock 或 monkeypatch；不得下载模型、不得调用 edge-tts 网络。

## 3. 数据层 API（`app/v2/db.py`）

```python
class DB:
    def __init__(self, path: str): ...
    def init(self) -> None
    def close(self) -> None

    # profile
    def get_profile(self) -> dict
    def update_profile(self, **fields) -> dict          # 只允许 nickname/native_language/target_languages/fact_sheet
    def set_fact_sheet(self, fact: dict) -> dict

    # friends
    def list_friends(self, status: str | None = None) -> list[dict]   # last_interaction_at desc NULLS LAST
    def get_friend(self, fid: str) -> dict | None
    def create_friend(self, data: dict) -> dict         # 缺省字段自动补齐
    def update_friend(self, fid: str, **fields) -> dict
    def delete_friend(self, fid: str) -> None
    def used_archetype_ids(self) -> set[str]
    def touch_friend(self, fid: str) -> None            # last_interaction_at=now

    # messages
    def add_message(self, friend_id: str, sender: str, transcript: str, *, msg_type="voice",
                    audio_path=None, duration_ms=0, suggestion=None, is_read=True,
                    context=None) -> dict
    def get_message(self, mid: str) -> dict | None
    def update_message(self, mid: str, **fields) -> dict
    def list_messages(self, friend_id: str, *, limit=30, before=None) -> list[dict]  # 升序返回
    def recent_messages(self, friend_id: str, limit=10) -> list[dict]                 # 升序
    def count_turns(self, friend_id: str) -> int        # sender='user' 的条数
    def last_message(self, friend_id: str) -> dict | None
    def unread_count(self, friend_id: str) -> int
    def mark_read(self, friend_id: str) -> None

    # moments
    def add_moment(self, friend_id, content, mood_tag, local_time_str, feed_date) -> dict
    def list_moments(self, limit=30) -> list[dict]
    def get_moment(self, mid: str) -> dict | None
    def mark_moment_replied(self, mid: str) -> None
    def moments_exist_for_date(self, feed_date: str) -> bool

    # expressions
    def add_expression(self, original_text, better_expression, explanation=None, source_friend_id=None) -> dict
    def list_expressions(self, status=None) -> list[dict]
    def get_expression(self, eid: str) -> dict | None
    def update_expression(self, eid: str, **fields) -> dict
    def delete_expression(self, eid: str) -> None
    def pick_review_expressions(self, n=2) -> list[dict]   # status='learning'，按 mastery_count/created_at
```

所有 friend/message/moment/expression 返回 dict；JSON 列已解码；`closeness_level`、`total_turns` 为 int。

## 4. 模块接口

### 4.1 `timeutil`
```python
def local_state(tz_name: str, now: datetime | None = None) -> dict
# {"local_time":"16:32","local_date":"2026-11-03","weekday":"周一","period":"afternoon",
#  "activity":"在店里忙着晚高峰","icon":"☕","is_awake":True}
def local_date_str(tz_name: str, now=None) -> str   # 用户本地日期（用于朋友圈幂等）
```

### 4.2 `voices`（voice 负责人）
```python
def load_archetypes(path=None) -> list[dict]        # 读 voice_archetypes.json
def archetype_by_id(aid: str) -> dict | None
class FriendVoiceSynth:
    def __init__(self, sample_rate=24000): ...
    async def synth_to_wav(self, text: str, voice_id: str, *, rate="+0%", pitch="+0Hz",
                           out_dir: str) -> tuple[str, int]   # (相对/绝对 wav 路径, duration_ms)
```
要求：edge 成功→wav(24k)，失败→tone 兜底；同一 (text,voice,rate,pitch) 命中进程内缓存可跳过。
单测不得联网（monkeypatch `EdgeTTSEngine.synthesize_voice`）。

### 4.3 `spawner`（voice 负责人）
```python
class PersonaSpawner:
    def __init__(self, db: DB, llm): ...
    async def spawn(self, *, nationality=None, language=None, gender=None,
                    referred_by_friend_id=None, status="active") -> dict
    # 返回创建好的 friend dict；同时若 status="active" 返回后由 chat_service 发破冰消息
    def pick_archetype(self, *, nationality=None, language=None, gender=None) -> dict
    async def icebreaker(self, friend: dict) -> str   # 第一条语音条文本
```
兜底：无 key/JSON 解析失败时，用 archetype + 名字池 + 职业池确定性生成完整人设（仍要有个性）。

### 4.4 `memory`（memory 负责人）
```python
class MemoryManager:
    def __init__(self, db: DB, llm): ...
    def build_context(self, friend: dict, *, review_expressions: list[dict] | None = None,
                      now: datetime | None = None) -> str
    # 注入：好友当地时间/状态、熟稔度语气、UserFactSheet、SharedEpisodes、OpenLoops、待复习表达
    async def after_turn(self, friend: dict) -> dict
    # 异步提炼：更新 fact_sheet / shared_episodes / open_loops（确定性兜底）
    def due_open_loops(self, friend: dict, now: datetime | None = None) -> list[dict]
    def mark_open_loop_done(self, friend_id: str, index: int) -> None
    async def followup_message(self, friend: dict, loop: dict) -> str
```

### 4.5 `social`（memory 负责人）
```python
def closeness_level(total_turns: int) -> int          # 1..5；10/30/80 阈值
def closeness_style(level: int) -> str                # 注入 prompt 的语气约束
def should_refer(friend: dict) -> bool                # total_turns>=15 且 closeness>=2 且未推荐过
def referral_line(friend: dict, new_friend_name: str) -> str
```

### 4.6 `moments`（memory 负责人）
```python
async def generate_daily(db: DB, llm, friends: list[dict], *, feed_date: str,
                         per_friend_max=1) -> list[dict]   # 一次批量 LLM；兜底模板
def moment_for_prompt(moment: dict) -> str
```

### 4.7 `chat_service`（Lead）

> 语言硬约束：`_llm_reply` 生成后会检查是否为目标语言（zh/ja/ko/en 脚本判定）；
> 不符则调用 `_rewrite_in_language` 重写，仍不符则用 `_mock_reply` 的目标语言模板兜底。
```python
class ChatService:
    def __init__(self, engines, db: DB, memory: MemoryManager, voice: FriendVoiceSynth,
                 moments=None): ...
    async def handle_voice(self, friend_id: str, audio_bytes: bytes, filename: str,
                           duration_ms: int = 0) -> dict
    async def handle_text(self, friend_id: str, text: str, context: str | None = None) -> dict
    # 返回 {"user_message":..., "friend_messages":[message dict ...],
    #       "suggestion_pending": True, "closeness_level":int, "total_turns":int}
    async def suggest_for_message(self, mid: str) -> None   # 后台任务
    async def memory_worker(self, friend_id: str) -> None
```

## 5. 前端约定（frontend 负责人）

- 路由：单页 `frontend/src/app/page.tsx`（移动端三 Tab），深链便于截图验收：
  `/ ?tab=chats|discover|notes`、`/?tab=chats&friend=<id>`。
- 契约见 `docs/V2_API.md`；所有请求走相对路径 `/api/v2/...`。
- 录音：`MediaRecorder` → `FormData(file, filename="voice.webm")`；不支持时给文字输入兜底。
- 音频播放：`<audio src="/api/v2/audio/...">`；倍速 1.0/0.8。
- 移动端 390×844 优先；PWA：`public/manifest.webmanifest` + `public/sw.js` + 图标（可 SVG/PNG 生成）。
- 构建验收：`cd frontend && npm install && npm run build:backend`，产物出现在
  `backend/app/static/app/index.html`，后端根路径可直接打开。
- 视觉：微信式 IM，浅色、绿色气泡、圆角；中文界面+英文内容；不要引入新的 UI 组件库（lucide-react 已有）。
- 自测：`npm run typecheck` 通过；用 headless Chrome 按 390×844 截图三个 Tab 到 `artifacts/v2-*.png`。

## 6. 完成标准（DoD）

- `pytest backend/tests -q` 全绿（含 v2 单测）。
- `python -m uvicorn app.main:app --app-dir backend --port 8000` 启动，`/api/v2/*` 全链路可用（无 LLM key 时 mock 也可完整演示）。
- 移动端 UI 三 Tab 可用，能发语音条/收语音条/看到润色胶囊/收藏表达/偶遇好友/看朋友圈/编辑记忆。
- 无网络也能演示（edge-tts/tone 降级、LLM mock）。
