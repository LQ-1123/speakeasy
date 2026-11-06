# SpeakEasy V3 · API 与数据模型

> 本轮新增 `/api/v3/*`：**Persona / Post / Friendship / SocialEvent**。
> `/api/v2/*` 全部保留（聊天、语音、通话、记忆、表达本、设置），两者共用同一个
> SQLite 文件（`data/speakeasy_v2.db`）。V3 加好友后写入 `friends` 表，
> 因此后续私聊/TTS/通话完全复用 V2 链路。

## 1. 身份与鉴权

```
POST /api/v3/auth/register   {"device_key": "<8-128位>", "display_name": "..."}
  → {"user_id": "u_...", "token": "<一次性返回>", "created": true}
```

- 之后所有 `/api/**` 请求带 `X-User-Id` + `X-User-Token`（前端 `installApiIdentity()`
  自动给 `window.fetch` 补头，因此 V2 请求也会自动带上身份）。
- **不带** `X-User-Id` → 服务端按本地用户 `local` 处理（兼容老客户端/测试）。
- 带 id 但 token 缺失/错误 → `401`；非法 id → `400`。
- token 只存 `sha256`；同一 `device_key` 重复注册会轮换 token（本地丢失可恢复）。

## 2. 世界生成（一次捏人 = 人物 + 信息流）

V3 的「捏人」只发生一次，产物就是整个信息流世界：

| 模式 | 触发方式 | 说明 |
|---|---|---|
| `seed`（默认） | 首次启动 | 写入 `seed_data` 的 12 人物 / 36 动态，零模型调用、可复现 |
| `llm` | `SE_WORLD_GEN=llm` 且空库 | 启动时用 LLM 生成 N 人物 × M 动态（`SE_WORLD_PERSONAS`，默认 8），失败自动回落 `seed` |

- 生成结果记在 `v3_meta`（`world_source` / `world_generated_at`），`GET /api/v3/health` 的 `world` 字段可查。
- 世界一旦存在就**不会重复生成/灌入**（`seed_if_needed` 对非空库直接跳过）。
- 偶遇、信息流、主页都读同一个世界：人物头像、资料、声音完全一致。

## 3. 信息流

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v3/feed?cursor=&limit=12` | Persona 公共动态 + 用户动态（公开；自己的任意可见性）合并按时间倒序，游标分页 |
| GET | `/api/v3/feed/summary` | `{persona_posts, my_posts, friends, unread_events}` |
| GET | `/api/v3/discover/next?language=` | **偶遇**：从已生成世界里挑一位（优先没加好友的，按用户+日期+人物稳定哈希轮换；看过的人记录在 `v3_discover_views`，全部看完自动重置） |
| GET | `/api/v3/personas?limit=&offset=` | Persona 列表 |
| GET | `/api/v3/personas/{id}` | 主页：资料 + 性格 + 兴趣 + 动态 + 好友状态 |
| GET | `/api/v3/personas/{id}/posts?cursor=&limit=` | 该人物动态分页 |

### 3.2 语言一致性（重要）

每个人物用自己的**母语**发动态、评论、聊天，声线也必须是该语言的声线：

| 人物 | 语言 | 声线 |
|---|---|---|
| Adrian / Annika / Callum / Finn / Mei | en | en-GB / en-US / en-AU / en-IE |
| Camila | pt | pt-BR-ThalitaMultilingualNeural |
| Giulia | it | it-IT-IsabellaNeural |
| Haruto | ja | ja-JP-KeitaNeural |
| Ines | es | es-ES-XimenaNeural |
| Jiwoo | ko | ko-KR-SunHiNeural |
| Lin | zh | zh-CN-XiaoyiNeural |
| Morten | de | de-DE-FlorianMultilingualNeural |

- 加好友后 riends.reply_language = persona.language，聊天文字/语音条同语言；
  每次启动 sync_persona_friends() 会把已加好友的 Persona 语言/声线同步成最新值。
- 每条动态存 language 列（种子/LLM 生成时写入，老库启动自动回填）：**朗读声线由帖子语言决定**；
  跨语言发帖（例如英文人物发韩语帖）会自动换成韩语声线。
- 语音 URL 带内容哈希 ?v= 作为缓存版本，语料升级后不会播放旧音频。
- 中文用户看信息流/通知时用「翻译」看中文；评论与好友申请也可单独翻译。
- LLM 世界生成（SE_WORLD_GEN=llm）同样要求按人物母语生成并匹配声线。

### 3.4 种子头像（确定性生成）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/v3/avatar/{seed}.svg?g=male&r=east_asian&a=28 | 按种子生成插画头像 SVG：同一 seed 永远同一张脸；g/
 只影响肤色/发型/胡子等；Cache-Control: immutable |

- 当前主头像：**DiceBear Avataaars**（`personas.avatar_style = "avataaars"`）。部件保存在
  `personas.avatar_options`（JSON：top/hairColor/skinColor/clothing/eyes/eyebrows/mouth/
  accessories/facialHair/backgroundColor…），由 **LLM 侧写**人物资料后选出，无 LLM 时用
  确定性映射（地域→肤色、性别→发型池、年龄→灰发/眼镜概率）兜底。
- 客户端渲染：`avatar_style + avatar_options` → 本地 Avataaars SVG（离线、确定性），
  信息流/主页/偶遇/聊天/通知全部同源；`avatar` 字段始终返回 SeedFace URL 作为兜底。
- 好友表 `friends.avatar_options` 会随人物同步，V2 聊天列表也能渲染同一套部件。
- 老版 SeedFace 三维度（人种/性别/年龄）仍可由 `/api/v3/avatar/{seed}.svg` 使用：
- 三个可区分维度：**人种/地域**（肤色 + 发色发质）、**性别**（发型/胡须/耳环/唇色）、**年龄**（灰白发/皱纹/眼镜/发型偏好，三档 <25 / 25-32 / ≥33）；`g` / `r` / `a` 分别对应这三个维度。
- Persona 头像不再使用 public/mock/avatars/*.jpg，改为 personas.avatar_seed（persona-<id>）驱动；
  数据库只存种子，图片由接口确定性生成，零图片资源、零网络依赖。
- 同一个人物在信息流、Persona 主页、偶遇卡片、聊天列表里都引用**同一个 URL/种子**，跨端一致。
- 老库启动时 ackfill_avatar_seeds() 自动回填种子（幂等）。

### 3.10 朗读互斥（同一时刻只播一个）

朗读/试听音频在前端由 `audioBus` 统一管理：拦截 `HTMLMediaElement.play`，播放前暂停上一段；
`local/tts` 的 WebAudio 播放路径同样在开始新音频前 `stopWebAudio()`。
因此连续点两条动态的「朗读」时，上一句会立刻停止，不会重叠。

### 3.9 个人资料、性别与头像

- `PUT /api/v3/profile` 支持 `nickname / target_languages / birthday / city / job / **gender** / interests`；
  `gender ∈ {male, female, other, ""}`，存入 `fact_sheet.profile.gender`。
- 头像不再由用户选风格：客户端按「昵称 + 性别」用 Avataaars 确定性生成（女性长发池、男性短发池），
  原「头像风格」选择器已移除。
- 用户动态（`user_posts`）的作者名/头像统一跟随当前个人资料：改昵称会同步历史动态，
  前端渲染 user_post 时使用「我」的昵称与头像，避免「发帖人是 Traveler、资料里是另一个人」。

### 3.8 个人兴趣与偶遇匹配

- `PUT /api/v3/profile`：更新昵称 / 目标语言 / 生日 / 城市 / 职业 / **兴趣标签**；
  城市与职业写进 `fact_sheet.profile`，兴趣写进 `fact_sheet.interests`（与 V2 记忆同源）。
- 兴趣清理：去 `#`、去空白、去重、单条 ≤16 字、**最多 8 个**；8 个预设（咖啡/美食/旅行/
  音乐/读书/电影/摄影/运动/科技/艺术 中的常用项）+ 自定义输入。
- 偶遇（`GET /api/v3/discover/next`）：先排除已加好友/待处理的人，再按
  **兴趣重合数降序** + 当日稳定 hash 排序；返回新增 `match_tags`（共同兴趣），
  前端把它排在人物标签最前面。
- 「我」页资料卡展示兴趣 chip；编辑资料弹窗可增删（预设 + 自定义）。

**多图布局**：动态 2 张并排、3 张一行、4 张 2×2（小方块，点击放大），单图按原比例。

### 3.7 用户上传图片

- 前端「发动态 → 上传照片」，最多 4 张；内置 mock 图仍可作为快捷选择。
- 后端 `POST /api/v3/media/upload`：Pillow 解码校验 → 按 EXIF 方向摆正 → 去掉 EXIF/GPS →
  最长边 1600px → webp(85) → 存 `data/uploads/{user_hash}/{sha256(原图)}.webp`；
  同名内容天然去重；静态服务 `/uploads/*`。
- 隐私：上传后原图的 EXIF/GPS 不再保留；文件仅本人 URL 可访问（不含公开索引）。
- 动态 `media` 只允许 `/mock/photos/*` 与 `/uploads/*`，服务端强制校验。

### 3.6 开放图库与生活动态（零 key）

每日世界里，人物除了发文字，还会发「生活图文」：图片来自开放图库，文案由 LLM
按人设 + 图片事实卡生成。

| 项 | 默认 | 说明 |
|---|---|---|
| 图源 | Wikimedia Commons → Openverse | 零 key；Wikimedia 元数据更准优先，Openverse 数量更多兜底 |
| 下载 | 900px webp 落盘 `data/media/` | 本地服务 `/media/*`，不热链、不依赖外部可用性 |
| 授权 | CC0 / PDM / CC BY / CC BY-SA | 自动跳过 NC/ND；`media_library` 存作者、license、原页链接 |
| 署名 | 动态下方「图：作者 · 来源 · 授权」 | CC BY/BY-SA 必须署名，`source_ref` 随动态下发 |
| 人设硬过滤 | 有人物关键词（woman/man/portrait/selfie…）→ 丢弃；男性人设拒绝裙子/高跟鞋/手袋等；敏感词（酒/枪/血）→ 丢弃 | `app/v3/media.py:is_life_safe` |
| 相关性 | 标题/标签须命中检索词（长度≥4） | 过滤 Openverse 错标图 |
| 检索词 | 由人物 interests + occupation 映射到静物/场景词 | 如咖啡→espresso machine，海洋→ocean waves |
| 文案规则 | 母语、≤180 字、不得说「照片里是我」、不得出现城市/国家/地标名、不得编造事实 | 违规自动降级为语言模板 |
| 图文比例 | 每位人物 1 条图文 + 1 条纯文字 | 另有 3 条纯文字来自每日世界生成，合计约 25 条/天（5 人） |
| 开关 | `SE_MEDIA_ENABLED=0` 关闭图库（测试用） | 关闭时只生成纯文字 |

动态新增字段：`post_kind`（`life_photo` / `life_text`）、`alt_text`、`media_credit`、
`source_ref`（署名与授权）。前端图片带 `alt`，支持点击放大。

### 3.5 每日世界（定时生成 + 生命周期）

信息流不再是一份静态语料：每天本地时间 **02:00** 批量生成「今日世界」，并按下表回收。

| 项 | 默认 | 说明 |
|---|---|---|
| 生成规模 | 5 人 × 6 条 = 30 条/天 | `SE_DAILY_PERSONAS` / `SE_DAILY_POSTS_PER_PERSONA` 可调 |
| 生成方式 | LLM 批量生成 | 每位人物一次调用（人物资料 + 6 条动态）；无 LLM/失败时用内置模板兜底 |
| 生成时间 | 02:00（`SE_DAILY_HOUR`） | 启动时先补今天；内容不足 10 条时惰性补量，避免空窗 |
| 幂等 | `daily_world.day` 主键 | 同一天只生成一次（`force=True` 可强制重生成） |
| 动态可见期 | 24h（`SE_POST_TTL_HOURS`） | 过期后从 feed / 人物主页消失 |
| 互动动态 | 保留 7 天（`SE_POST_KEEP_DAYS`） | 点赞 / 评论 / 加好友 / 看主页 / 朗读 都算互动 |
| 游客人物 | 24h 内无互动 → 连同动态回收 | 有互动则 `lifecycle=adopted` 转正，不再回收 |
| 分页 | 每页 **10 条** | keyset cursor（`created_at|id`），刷新回到最新 10 条 |
| 互动记录 | `personas.interaction_count` / `persona_posts.interacted` | 用于 TTL 判定 |

接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v3/world/daily` | 今日状态：是否已生成、今日人物/动态数、可见动态数、游客数、下次生成时间、当前配置 |
| GET | `/api/v3/health` | 新增 `daily` 字段，内容同上 |

生命周期清理在后台每 10 分钟跑一次（`app/v3/lifecycle.py`）：延长互动动态 → 删除过期未互动动态 → 回收过期且无互动的游客人物。

### 3.3 朗读性能（毫秒级）

信息流是预生成语料，所以朗读音频**在用户点击前就合成好并落盘**：

| 环节 | 说明 |
|---|---|
| 启动预热 | 服务启动后后台合成第一页（前 8 条）人物动态音频，单并发，不阻塞启动 |
| 浏览预热 | /api/v3/feed 返回的每页动态、/discover/next 的招呼动态都会自动进合成队列 |
| 内容寻址 | 文件名 {post_id}-{content_hash}-{voice}.mp3：内容/声线变化自动换文件，不会播旧音频 |
| 格式 | wav 转 **mp3 64kbps**（约 33–51 KB，wav 约 315 KB），有 ffmpeg 时启用，否则回退 wav |
| 点击 | 命中文件直接返回；未命中才同步合成兜底（此时前端有 loading 态） |
| 前端 | 首屏前 4 条人物动态 + 偶遇卡片的音频用 Audio(preload=auto) 预取到浏览器缓存 |

实测（本机）：

| 场景 | 延迟 |
|---|---|
| 未预热冷启动 | ~8400 ms |
| 预热后接口（mp3，含 33–51 KB 传输） | **3–17 ms** |
| 浏览器已预取后 canplay | **11–16 ms** |
| 状态查询 | GET /api/v3/health 的 udio 字段（ready/queued/generated/failed/format） |

### 3.1 动态翻译与朗读

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v2/translate` | 信息流「翻译」复用 V2 翻译链路（LLM 优先 + 缓存），`{text,target:"zh"}` → `{translation}` |
| GET | `/api/v3/persona-posts/{post_id}/speech` | **用人物自己的声线**朗读该动态，返回 `audio/wav`；服务端合成并缓存（同一内容命中进程内缓存）；公共内容，`<audio>` 可直接播放，失败 503 时前端回退 `/api/v2/tts/sample` |

`/api/v3/discover/next` 返回：

```json
{
  "persona": {"id": "p_jiwoo", "nickname": "Jiwoo", "avatar": "/mock/avatars/jiwoo.jpg", "is_ai": true, "...": "..."},
  "greeting": "Three hours debugging. Missing comma. ...",
  "post_id": "pp_jiwoo_24",
  "friendship_status": "none",
  "remaining": 11,
  "source": "world"
}
```

信息流条目：

```json
{
  "kind": "persona_post | user_post",
  "id": "pp_adrian_00 | up_...",
  "content": "...",
  "media": ["/mock/photos/x.jpg"],
  "tags": ["cooking"],
  "visibility": "public|friends|private",
  "created_at": "2026-11-06T05:00:00+00:00",
  "like_count": 1, "comment_count": 2, "liked_by_me": false, "is_mine": false,
  "friendship_status": "none|pending|accepted",
  "author": {"id": "p_adrian", "nickname": "Adrian", "avatar": "/mock/avatars/adrian.jpg",
             "country": "United Kingdom", "is_ai": true, "ai_label": "AI 人物"},
  "comments": [{"id": "...", "content": "...", "author": {"nickname": "Mei", "is_ai": true}}]
}
```

## 4. 好友关系（用户 ↔ Persona）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v3/personas/{id}/friend` | 加好友（幂等）。已有 pending 申请时等价于「接受」 |
| DELETE | `/api/v3/personas/{id}/friend` | 解除关系（同时删除 friends 行与聊天记录） |
| GET | `/api/v3/friendships` | `{pending: [], accepted: []}` |
| GET | `/api/v3/events` | 社交事件（含到期的 friend_request） |
| POST | `/api/v3/events/{id}/accept` / `decline` | 只有 accept 才建立正式好友关系 |

加好友会写入 `friends`（`owner_user_id` = 当前用户，`status=active`，`avatar_url`=人物头像），
所以 `GET /api/v2/friends`、`/api/v2/chat/{friend_id}/text` 等立刻可用。

## 5. 用户动态与可见性

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v3/media/upload` | 上传动态配图（multipart `file`）。支持 JPG/PNG/WebP/GIF，≤8MB；
自动按 EXIF 摆正、**去除 EXIF/GPS**、最长边 1600px、转 webp；返回 `{url,width,height}` |
| POST | `/api/v3/posts` | 发动态。media 接受**内置图片**（`/mock/photos/*`）或**用户上传**（`/uploads/*`），
最多 4 张，其它地址 400；tags 去 `#`、去重、单条 ≤16 字、最多 6 个（预设 8 个 + 可自定义） |
 `{content, visibility: public\|friends\|private, media[], tags[]}` |
| GET | `/api/v3/posts/mine` | 自己的全部动态 |
| DELETE | `/api/v3/posts/{id}` | 仅作者可删（越权 404） |
| POST | `/api/v3/persona-posts/{id}/like` | 点赞/取消（幂等切换） |
| POST | `/api/v3/persona-posts/{id}/comments` | 评论 Persona 动态 |

- `public`：进入所有人的发现信息流，**且只有 public 会触发公共社交事件**
- `friends`：只有作者自己可见（好友模型是 user↔persona，其他人不可见）
- `private`：只有作者自己可见
- 发布前 UI 必须显式选择可见范围（弹窗内三档 + 文案说明），不会静默公开

## 6. 社交事件引擎

`POST /api/v3/posts` 发布公开动态后，服务端后台触发（幂等）：

1. 从文本/标签/画像提取兴趣标签（`social_engine.extract_tags`，中英文）
2. 与 Persona 池兴趣求交打分（每共同标签 2 分），取前 N 个候选
3. 每个候选按 `base_probability × 匹配加权` 掷骰 → 可能全部落空（`no_action`）
4. 命中后按权重抽事件类型（like / comment / friend_request），生成内容（LLM 优先，模板兜底）
5. `scheduled_at = now + random(delay_min, delay_max)`，延迟后才出现在通知里
6. 限制：同人物冷却、每帖最多 N 个、每用户 24h 最多 M 个、`dedupe_key` 唯一
7. 到期物化：like/comment 写入 `user_post_likes/comments` 并更新计数；friend_request 建 `friends(status=pending_request)` + `friendships(pending)`

环境变量（全部可配置，默认值见下）：

```
SE_SOCIAL_ENABLED=1
SE_SOCIAL_BASE_PROB=0.55
SE_SOCIAL_WEIGHTS=like:0.55,comment:0.32,friend_request:0.13
SE_SOCIAL_DELAY_MIN_S=5
SE_SOCIAL_DELAY_MAX_S=60
SE_SOCIAL_PERSONA_COOLDOWN_S=21600   # 6h
SE_SOCIAL_MAX_PER_POST=2
SE_SOCIAL_MAX_PER_USER_DAY=6
SE_SOCIAL_MAX_CANDIDATES=3
SE_SOCIAL_MIN_MATCH_SCORE=1
SE_SOCIAL_SYNC=0                     # =1 时同步生成（测试用）
```

`GET /api/v3/social/stats` 返回当前配置 + 判定统计（`evaluated / no_action / probability_miss` 等），
`GET /api/v3/health` 返回引擎配置与人物数量。

## 7. 数据表

新增：`v3_users`、`personas`、`persona_posts`、`persona_post_likes`、`persona_post_comments`、
`friendships`、`user_posts`、`user_post_likes`、`user_post_comments`、`social_events`、
`social_event_attempts`、`v3_meta`。

存量表新增列（幂等 ALTER，老数据默认 `local`）：
`friends.owner_user_id` / `friends.avatar_url`、`user_moments.user_id`、`saved_expressions.user_id`。

隔离规则：`DB` 层通过 `app.v3.identity.current_user_id()` 过滤 `friends` / `user_moments` /
`saved_expressions` / `moments`；V3 表全部按 `user_id` 过滤。越权访问一律 404。

## 8. 内容成本控制

- 世界（人物 + 动态）**一次生成、长期复用**：默认预置语料，读信息流/偶遇**零模型调用**
- 人物与动态是**全平台共享**的公共数据，不因某个用户访问而重新生成
- 偶遇不再现场捏人（V2 的 spawner 只在旧链路里，V3 已不再调用）
- 只有「用户发公开动态 → 可能的社交事件」会调用 LLM（概率触发，且每帖上限 2 次）
- 翻译按文本缓存；朗读按（文本, 声线）缓存，同一动态重复播放不重复合成
- 新用户不再自动捏人（`local` 用户保留旧引导），首屏直接进信息流
