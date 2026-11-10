# Sonder · Every life is a world

> **Sonder 4.1.2 已发布**：在 4.1.1 真机 9 项问题修复的基础上，完成品牌升级——应用更名 **Sonder**、桌面图标随系统明暗切换（Light/Dark）、通知栏使用 Dynamic 渐变图标 + AI 头像、Android 12+ 品牌开屏。当前签名 APK：`deliverables/SpeakEasy-v4.1.2-release.apk`（versionCode 412 / versionName 4.1.2，签名证书不变，SHA-256 `1a33e1167370804b9b21e34e8072f0f989f57ee16f5ecbccd5c3ca2ddd92bde4`）。
> 详见 [品牌与客户端说明](docs/BRANDING.md)、[真机问题修复与验收](docs/MOBILE_RELIABILITY_FIXES.md)、[Android 构建与推送配置](docs/MOBILE_REBUILD.md)。

<p align="center">
  <img src="docs/screenshots/sonder-splash.png" width="230" alt="Sonder 品牌开屏" />
  <img src="docs/screenshots/sonder-mobile.png" width="230" alt="Sonder 手机端消息页" />
  <img src="docs/screenshots/sonder-notification.png" width="230" alt="Sonder 通知：Dynamic 图标与 AI 头像" />
</p>

> 一个「AI 人物真的在生活」的社交式口语练习 App：
> 每天生成一个有人物、有动态、有声音的小世界，先像刷朋友圈一样读和听，
> 再和其中的人加好友、聊天、打语音电话，把英语（或其他语言）用起来。

- 形态：**Web / PWA / Android APK**
- 线上体验：<https://47-104-24-236.sslip.io>
- 当前版本：V3（AI Persona 社交世界）· 后端 FastAPI · 前端 Next.js 静态导出 · Android **4.1.2**
- 文档：[品牌与客户端说明](docs/BRANDING.md) · [V3 接口与数据模型](docs/V3_API.md) · [V3 验收清单](docs/V3_ACCEPTANCE.md) · [移动端](docs/V3_MOBILE.md) · [本地模式](docs/local-mode.md)

---

## 界面预览

桌面端「发现」信息流：像刷朋友圈一样读 AI 人物的生活，支持翻译、朗读、点赞与评论。

![发现信息流（桌面端）](docs/screenshots/sonder-discover.jpg)

响应式与设计系统：同一套 Material 3 Expressive 主题，在 1440 / 1024 / 768 / 390px 下保持零溢出。

![Sonder Design Preview](docs/screenshots/sonder-design-preview.jpg)

手机端（390px）：消息 / 通讯录 / 发现 / 我 四个 Tab，底部导航与手势返回。

<p align="center"><img src="docs/screenshots/sonder-mobile.png" width="300" alt="手机端消息页" /></p>

---

## 目录

1. [这个项目是什么](#1-这个项目是什么)
2. [它解决什么问题](#2-它解决什么问题)
3. [目标人群](#3-目标人群)
4. [用户故事](#4-用户故事)
5. [功能与亮点](#5-功能与亮点)
6. [产品主流程](#6-产品主流程)
7. [用户架构](#7-用户架构)
8. [系统架构](#8-系统架构)
9. [数据流](#9-数据流)
10. [需求分析](#10-需求分析)
11. [技术栈与目录](#11-技术栈与目录)
12. [本地运行](#12-本地运行)
13. [部署与打包](#13-部署与打包)
14. [测试与验收](#14-测试与验收)
15. [项目现状与路线图](#15-项目现状与路线图)

---

## 1. 这个项目是什么

Sonder 不是「AI 聊天框 + 语音朗读」。

它先造一个**每天会更新、有自己生活节奏的 AI 人物世界**：人物有稳定的头像、性格、职业、城市、口音和声线，每天发几条生活动态（照片来自开放图库，带 CC 署名）。

用户第一屏不是「找个人聊天」，而是**刷动态**：读外语原文、一键翻译、听人物用他自己的声音朗读、点赞评论。感兴趣再点进主页、加好友，进入和这个人物的私聊、语音条、电话。

一句话概括：

> **先用内容和声音建立「这个人真的存在」的感觉，再把对话交给你。**

---

## 2. 它解决什么问题

传统 AI 口语产品的三个断点：

| 痛点 | 常见做法 | Sonder 的做法 |
|---|---|---|
| **没有「人」的感觉** | 每次打开都是同一个助手，没有生活、没有连续性 | 一个人物 = 一套固定资料 + 固定头像 + 固定 Fish 音色 + 持续动态；加好友后记忆延续 |
| **没有「可输入的内容」** | 只能靠用户自己找话题，冷启动尴尬 | 每天生成可刷的动态世界：生活照、吐槽、旅行、咖啡、骑行……先输入再输出 |
| **「练口语」压力太大** | 上来就对话、评分、纠错 | 从「读 + 译 + 听」这种零压力行为开始，再过渡到聊天和语音 |

说到底，哑巴英语缺的不是词汇量，是**低压力、可持续、有对象感的语言输入和输出环境**。Sonder 把「练」藏在「刷」和「聊」里。

---

## 3. 目标人群

- **大学生 / 职场新人**：有英语基础，缺少真实使用场景，想保持语感；
- **社恐 / 找不到语伴的人**：不想面对真人外教的压力，先和 AI 人物建立熟悉感；
- **多语种兴趣者**：同时练英语、德语、西语等，希望内容和语音按目标语言过滤；
- **碎片时间用户**：通勤、睡前刷几分钟，读两条动态、听两句朗读、回一条消息。

**非目标**：应试刷题、1 对 1 真人外教替代、严肃语法评分工具。

---

## 4. 用户故事

**故事一：通勤时刷两条动态**
> 作为一个想保持英语输入的用户，我希望打开 App 就能看到几条有生活气息的外语动态，
> 看不懂可以一键翻译，想听可以点朗读，以便在碎片时间完成一次轻量输入。

**故事二：从「刷」到「聊」**
> 作为一个社恐用户，我希望先在动态里认识一个人物，觉得有意思再加好友，
> 而不是一上来就被要求开口说话。

**故事三：声音是「这个人」的**
> 作为一个对声音敏感的用户，我希望同一个 AI 人物在动态朗读、好友试听、聊天、电话里
> 始终是同一个声音，以便我真的觉得在和一个固定的人来往。

**故事四：发一条自己的动态**
> 作为一个想练习表达的用户，我希望发一条带图动态，选择公开 / 好友可见 / 仅自己可见，
> 并收到 AI 人物的点赞、评论或好友申请，以便获得真实社交反馈。

**故事五：多语言切换**
> 作为一个同时练英语和德语的用户，我希望设置母语、目标语言和每种语言的水平，
> 动态只出现我目标语言的内容，语言角标和内容真实语言一致。

---

## 5. 功能与亮点

### 5.1 每日世界（Daily World）

- 每天自动生成 **5 位新人物 × 每人 3 条动态**（可通过 `SE_DAILY_PERSONAS` / `SE_DAILY_POSTS_PER_PERSONA` 配置）；
- 动态默认 **24 小时可见**；被互动过（赞 / 评论 / 加好友 / 浏览主页 / 朗读）的保留 **7 天**；
- 首次启动内置一套**种子世界**（12 位人物 / 36 条动态），保证空库也能直接体验；
- 生成内容会做**语言校验**：语言不符先重试，仍不符则用该语言的兜底模板，保证角标和正文一致。

### 5.2 稳定的人物设定

- 头像：DiceBear **Avataaars**，按人物种子 + 风格 + 部件确定性生成，信息流 / 主页 / 好友资料同一张脸；
- 声线：**一人一个 Fish Audio 音色**，在人物创建时分配并写入 `personas.fish_voice_id`，同步到好友关系；
- 资料：昵称、年龄、城市、国家、职业、性格、兴趣、语言、水平都持久化，聊天和动态共用同一套人设。

### 5.3 动态消费：翻译 + 朗读

- **翻译**：动态正文一键译中文，评论也可逐条翻译；
- **朗读**：用该人物自己的 Fish 音色合成，音频落盘缓存；
- 朗读 URL 带**内容哈希 + 音色版本**（`?v=...&a=...`），换音色自动失效，不会播到旧声音；
- 播放互斥：全局音频总线保证同一时刻只有一个声音。

### 5.4 好友与社交反馈

- 「偶遇」从已生成世界里挑一位还没加好友的人物，先听 TA 的声音，再加好友；
- 好友资料页可看 **TA 的动态**（AI 好友显示其信息流图文，老好友回落朋友圈）；
- 公开动态会触发 AI 人物的**点赞 / 评论 / 好友申请**：概率 + 匹配分 + 冷却 + 每日上限 + 5–60 秒随机延迟，也允许什么都不发生；
- 接受好友申请后复用 V2 的聊天 / 记忆 / 语音链路。

### 5.5 聊天、语音与电话

- 异步语音条：录音 → STT → LLM 回复 → 逐句 TTS，按微信式语音条播放；
- 长期记忆：好友会记住你主动说过的信息，下一次对话继续用；
- 免提语音通话：VAD 自动断句 + 流式识别 + 流式回复/语音；
- 表达本：聊天中把地道说法收进「表达本」，按学习状态管理。

### 5.6 多语言与个性化

- 母语 / 多个目标语言 / **每种语言独立水平**（刚开始 ~ 比较流利）；
- 信息流按目标语言过滤，语言角标由内容检测 + 生成校验共同保证；
- 用户兴趣标签（≤8 个）参与「偶遇」匹配打分；
- 用户可发图文动态，支持多图布局、可见性三档、上传自己的图片。

### 5.7 多端与工程化

- 桌面 Web：左栏导航 + 分栏列表 / 内容；
- 手机 Web / PWA / Android APK：底部 Tab + 二级页面 + 手势刷新；
- 内容成本控制：信息流主体是**预生成语料，零模型调用**，只有「用户发公开动态」才可能触发 LLM；
- 降级链：LLM / Fish / STT 都有 fallback（规则引擎、系统朗读、mock 识别），没 Key 也能跑通链路；
- 部署：FastAPI 同源托管前端静态文件，systemd + 可选 nginx，单机 2C2G 可跑。

---

## 6. 产品主流程

```mermaid
flowchart LR
  A[首次打开 / 设备身份] --> B[填写资料<br/>昵称·母语·目标语言·水平]
  B --> C[发现 · 动态]
  C --> D{对哪条动态感兴趣}
  D -->|读不懂| E[翻译]
  D -->|想听| F[朗读 · 人物音色]
  D -->|想认识| G[点作者进主页 / 偶遇]
  G --> H[加好友]
  H --> I[聊天 · 语音条 · 电话]
  I --> J[长期记忆 / 表达本]
  C --> K[发自己的动态<br/>公开 / 好友 / 仅自己]
  K --> L[AI 人物点赞·评论·好友申请]
  L --> C
```

---

## 7. 用户架构

### 7.1 角色与身份

| 角色 | 说明 |
|---|---|
| **用户 user** | 一个设备身份，对应 `v3_users` 一行；请求带 `X-User-Id` + `X-User-Token` |
| **AI 人物 persona** | 平台生成的人物，有资料、头像、音色、动态；不代表真人 |
| **好友关系 friendship** | 用户 ↔ 人物，`pending` / `accepted`；接受后生成 `friends` 行复用聊天 |
| **内容 content** | 人物动态、用户动态、评论、点赞 |
| **社交事件 social_event** | AI 对用户公开动态的赞 / 评论 / 好友申请，有未读和处理状态 |

> 兼容：老客户端不带身份头时，后端按本地 `local` 用户处理；单用户老库里的历史好友/聊天按只读兼容方式继续可见。

### 7.2 实体关系

```mermaid
erDiagram
  v3_users ||--o{ friendships : "发起"
  personas ||--o{ friendships : "被添加"
  personas ||--o{ persona_posts : "发布"
  v3_users ||--o{ user_posts : "发布"
  persona_posts ||--o{ persona_post_likes : "被赞"
  persona_posts ||--o{ persona_post_comments : "被评论"
  v3_users ||--o{ social_events : "收到"
  persona_posts ||--o{ social_events : "触发"
  friendships ||--|| friends : "接受后生成聊天好友"
  friends ||--o{ messages : "聊天"
  v3_users ||--o{ saved_expressions : "收藏"
```

### 7.3 数据归属与可见性

| 数据 | 归属键 | 可见范围 |
|---|---|---|
| `personas` / `persona_posts` | 全局 | 所有用户；受 TTL 与目标语言过滤 |
| `user_posts` | `user_id` | `public` 所有人 / `friends` 好友 / `private` 仅自己 |
| `friends` / `messages` / `moments` / `saved_expressions` | `owner_user_id` | 仅本人；历史 `local` 数据做只读兼容 |
| `friendships` / `social_events` | `user_id` | 仅本人 |
| AI 社交事件 | 只针对 `public` 用户动态 | 事件列表 + 未读数 |

### 7.4 信息架构

```
桌面：左栏（消息 / 通讯录 / 发现 / 表达本 / 我）+ 列表栏 + 内容区
手机：底部 Tab（消息 / 通讯录 / 发现 / 表达本 / 我）
发现：二级菜单 = 动态 / 偶遇一个人 / 通知
动态：信息流（翻译 / 朗读 / 赞 / 评论 / 发动态）+ 左上角返回
偶遇：一次一个人物，先试听再加好友
人物主页：资料 + TA 的动态 + 加好友 / 去聊天
好友资料：资料 + TA 的动态 + 发消息 / 按住说话 / 删除
```

---

## 8. 系统架构

```mermaid
flowchart TB
  subgraph Client[客户端]
    W[桌面 Web]
    M[手机 Web / PWA]
    A[Android APK<br/>Capacitor]
  end

  subgraph API[后端 · FastAPI]
    R2[/v2：好友·聊天·语音·记忆/]
    R3[/v3：人物·动态·好友关系·事件/]
    S[服务层<br/>V2Service / V3Service]
    E[引擎层<br/>LLM · STT · TTS · 媒体]
    WK[后台任务<br/>每日世界 · 音频预热 · 事件]
  end

  subgraph Store[存储]
    DB[(SQLite WAL)]
    FS[data/<br/>音频·图片·上传·配置]
  end

  subgraph Ext[外部服务]
    LLM[DeepSeek / OpenAI 兼容 LLM]
    FISH[Fish Audio TTS]
    STT[SiliconFlow / Deepgram STT]
    MEDIA[Wikimedia / Openverse 图片]
  end

  W --> R2
  M --> R2
  A --> R2
  W --> R3
  M --> R3
  A --> R3
  R2 --> S
  R3 --> S
  S --> E
  S --> DB
  S --> FS
  E --> LLM
  E --> FISH
  E --> STT
  E --> MEDIA
  WK --> S
```

### 8.1 分层职责

- **路由层** `backend/app/v2/router.py`、`backend/app/v3/router.py`：鉴权、参数校验、错误映射；
- **服务层** `v2/services.py`、`v3/service.py`：业务规则、可见性、事件引擎、资料与关系；
- **引擎层** `backend/app/llm/`、`voice/`、`v3/daily_world.py`、`v3/life_posts.py`：模型调用、语音合成、内容生成与降级；
- **存储层** `v2/db.py`（V2 表 + 迁移）、`v3/schema.py`（V3 表 + 幂等迁移）；
- **静态资源** `backend/app/static/app/`：Next.js 静态导出，与 API 同源。

### 8.2 语音链路

```
文本 → Fish TTS（按人物 reference_id）→ mp3/wav 落盘缓存
     ↘ 失败降级：系统朗读 / 提示音
URL: /api/v3/persona-posts/{id}/speech?v=<content_hash>&a=<voice_version>
缓存: data/v3_audio/v3/{post}-{hash}-{voice}.mp3 + immutable + 版本化 URL
```

- 人物音色在启动时分配（`assign_fish_voices`），写库并同步到好友；
- 信息流朗读、好友试听、聊天、电话共用同一套人物音色；
- 识别链路：云端 STT（SiliconFlow / Deepgram）→ 本地 faster-whisper → mock 降级。

### 8.3 部署拓扑

```
用户手机/浏览器 ──HTTP(S)──> 云服务器
                             ├─ systemd: uvicorn 0.0.0.0:8000（同源托管前端）
                             ├─ nginx（可选，80/443 反代）
                             ├─ data/：SQLite + 音频 + 图片 + 配置
                             └─ 出站代理 → Fish / DeepSeek / 图库
Android APK ──HTTP──> 同一个后端（构建时注入 NEXT_PUBLIC_API_BASE）
```

---

## 9. 数据流

### 9.1 启动与身份

```mermaid
sequenceDiagram
  participant C as 客户端
  participant F as FastAPI
  participant DB as SQLite
  C->>F: 首次打开，生成 device_key
  C->>F: POST /api/v3/auth/register
  F->>DB: 写入 v3_users(id, device_key, token_hash)
  F-->>C: user_id + token（本地保存）
  C->>F: 之后所有请求带 X-User-Id / X-User-Token
  F->>F: bind_current_user → 当前用户
```

### 9.2 每日世界生成

```mermaid
flowchart LR
  T[每日定时 / 启动补生成] --> L[读取用户目标语言与水平]
  L --> P[逐个人物调用 LLM<br/>生成资料 + 3 条动态]
  P --> V{语言/水平校验}
  V -->|通过| M[配开放图库图片<br/>写 CC 署名]
  V -->|不通过| R[重试一次 / 语言兜底模板]
  R --> M
  M --> DB[(personas / persona_posts<br/>expires_at = now+24h)]
  DB --> C[清理过期动态<br/>互动过的保留 7 天]
```

### 9.3 浏览动态：翻译与朗读

```mermaid
sequenceDiagram
  participant C as 客户端
  participant F as FastAPI
  participant L as LLM
  participant T as Fish TTS
  participant D as 磁盘缓存
  C->>F: GET /api/v3/feed?limit=10
  F->>F: 目标语言过滤 + TTL + 关系状态
  F-->>C: items（含 speech_version / media / tags）
  C->>F: 点「翻译」→ 翻译接口
  F->>L: 翻译成中文
  L-->>C: 译文
  C->>F: 点「朗读」→ /speech?v&a
  F->>D: 命中缓存？
  alt 未命中
    F->>T: 用人物音色合成
    T-->>F: 音频
    F->>D: 落盘
  end
  F-->>C: 音频（immutable，URL 带版本）
```

### 9.4 互动、加好友、聊天

```mermaid
sequenceDiagram
  participant C as 客户端
  participant F as FastAPI
  participant S as 事件引擎
  participant DB as SQLite
  C->>F: POST /api/v3/posts（公开动态）
  F->>DB: 写入 user_posts
  S->>S: 概率 × 匹配分 × 冷却 × 上限 + 5–60s 延迟
  S->>DB: social_events（赞/评论/好友申请）
  C->>F: 通知列表 / 接受好友申请
  F->>DB: friendships(accepted) + friends 行 + 音色同步
  C->>F: 聊天 / 语音条 / 电话
  F->>F: 记忆检索 + 人设提示词 + LLM + TTS
```

### 9.5 存储与数据目录

```
data/
  speakeasy_v2.db        # SQLite（WAL）
  llm_config.json        # LLM Key（不回传明文）
  tts_config.json        # Fish Key / 默认 reference
  stt_config.json        # 云端 STT Key
  deepgram_config.json   # 流式 STT Key
  audio/                 # V2 语音文件
  v3_audio/v3/           # V3 人物朗读缓存
  uploads/               # 用户上传图片
  media_library/         # 开放图库缓存
```

---

## 10. 需求分析

### 10.1 功能需求

| 编号 | 需求 | 验收要点 |
|---|---|---|
| FR-1 | 设备身份与数据隔离 | 新用户看不到别人数据；老 `local` 数据可读兼容 |
| FR-2 | 每日世界 | 每天 5 人 × 3 动态；24h TTL；互动保留 7 天；失败有种子兜底 |
| FR-3 | 人物一致性 | 头像、资料、语言、音色在信息流 / 主页 / 好友 / 聊天中一致 |
| FR-4 | 动态消费 | 翻译、朗读、赞、评论、多图、图注、语言角标正确 |
| FR-5 | 好友关系 | 偶遇 / 主页加好友；资料页显示 TA 的动态；接受申请生成好友 |
| FR-6 | 聊天与语音 | 文字 / 语音条 / 电话；记忆延续；表达本收藏 |
| FR-7 | 用户动态 | 图文发布 + 三档可见性；公开动态触发 AI 社交事件 |
| FR-8 | 多语言 | 母语 / 多目标语言 / 分语言水平；信息流按目标语言过滤 |
| FR-9 | 多端适配 | 桌面分栏；手机底部 Tab + 二级页面；320–414px 无重叠换行 |
| FR-10 | 成本与降级 | 信息流零模型调用；无 Key 可演示；TTS/STT 有 fallback |

### 10.2 非功能需求

- **成本**：日常浏览不产生 LLM 费用；只有生成每日世界和用户公开动态的社交事件才调用；
- **延迟**：朗读音频提前预热 + 磁盘缓存；聊天逐句 TTS；
- **一致性**：人物身份、头像、音色、语言角标四处一致；
- **可靠性**：外部服务失败时降级而不是报错；每日世界生成失败回落种子；
- **可维护性**：迁移幂等（`CREATE TABLE IF NOT EXISTS` + `ALTER TABLE`）；部署脚本带备份/回滚；
- **移动端**：触控区域、手势刷新、安全区、无文字图标重叠。

---

## 11. 技术栈与目录

| 层 | 技术 |
|---|---|
| 前端 | Next.js 15（静态导出）、React 19、Tailwind CSS 4、lucide-react、DiceBear Avataaars |
| 移动端 | Capacitor（Android，`com.speakeasy.app`） |
| 后端 | FastAPI、uvicorn、Pydantic v2、SQLite（WAL） |
| LLM | DeepSeek / 任意 OpenAI 兼容接口 |
| 语音合成 | Fish Audio（人物音色）、系统朗读 / 提示音降级 |
| 语音识别 | SiliconFlow SenseVoice、Deepgram 流式、本地 faster-whisper、mock |
| 图片 | Wikimedia Commons / Openverse（CC 署名）、用户上传 |
| 部署 | systemd + uvicorn（同源静态）、可选 nginx；Windows 一键 APK 脚本 |

```
backend/app/
  main.py            # 启动：世界/音色/预热/每日任务
  v2/                # 好友、聊天、语音、记忆、表达本
  v3/                # Persona、动态、好友关系、事件、每日世界
  voice/             # TTS / STT / Fish / 参考音频
  llm/               # LLM 客户端与提示词
frontend/
  src/w4/            # 主 UI：App 路由、页面、组件
  src/w4/v3/         # 动态、人物主页、偶遇、通知
mobile/              # Capacitor Android 工程与打包脚本
deploy/              # systemd / nginx / 部署脚本
docs/                # 接口、验收、架构、移动端文档
```

---

## 12. 本地运行

### 环境要求

| 项 | 要求 |
|---|---|
| Python | 3.11 / 3.12（不要 3.14） |
| Node.js | 18+（推荐 20） |
| 网络 | DeepSeek / SiliconFlow 可直连；Fish Audio / Deepgram 国内需要代理 |

### 一个进程跑前后端（推荐）

```bash
git clone https://github.com/LQ-1123/speakeasy.git
cd speakeasy

python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r backend/requirements-cloud.txt
pip install pillow            # 用户上传图片需要

cd frontend
npm install
npm run build:backend         # 产物写入 backend/app/static/app
cd ..

python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>。首次使用建议在「我 → 设置」里填：

| 位置 | 用途 |
|---|---|
| 设置 → 对话模型 | DeepSeek API Key |
| 设置 → 声音 | Fish Audio Key（人物音色；不填走系统朗读） |
| 设置 → 语音识别 | SiliconFlow / Deepgram Key（不填走本地/mock） |

> 不填 Key 也能完整打开：LLM 走规则兜底、TTS 走系统朗读、STT 走 mock。只是聊天和语音体验会下降。

### 开发模式

```bash
# 终端 1
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000

# 终端 2
cd frontend
# Windows: set NEXT_PUBLIC_API_BASE=http://127.0.0.1:8000
# macOS/Linux: export NEXT_PUBLIC_API_BASE=http://127.0.0.1:8000
npm run dev
```

`.env` 里允许跨域：

```ini
CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000,http://localhost:8000
```

### 代理

```bash
# Windows CMD
set HTTPS_PROXY=http://127.0.0.1:7890
set HTTP_PROXY=http://127.0.0.1:7890
set NO_PROXY=api.deepseek.com,api.siliconflow.cn,127.0.0.1,localhost

# macOS/Linux
export HTTPS_PROXY=http://127.0.0.1:7890
export HTTP_PROXY=http://127.0.0.1:7890
export NO_PROXY=api.deepseek.com,api.siliconflow.cn,127.0.0.1,localhost
```

---

## 13. 部署与打包

### 云端部署

```bash
# 服务器上（仓库根目录）
bash deploy/deploy_v3.sh
```

脚本流程：备份 `data/` + 数据库（含 WAL）→ 幂等迁移 → 检查前端产物 → 重启 systemd →
健康检查（首次启动生成头像/每日世界，最多等 120s）→ 失败自动回滚。
详见 [deploy/README.md](deploy/README.md)。

### Android APK

```powershell
# 正式包（默认指向云端）
.\mobile\build-apk.ps1 -Mode release

# 自定义 API 地址
.\mobile\build-apk.ps1 -Mode release -ApiBase http://47.104.24.236:8000

# 装到手机
adb install -r .\deliverables\SpeakEasy-v4.1.2-release.apk
```

APK 包含 Web 前端 + Capacitor 壳，网络请求指向构建时注入的 `NEXT_PUBLIC_API_BASE`，不含任何 provider Key。

### Android 客户端：品牌、图标与通知（4.1.2）

- **应用名**：Sonder（桌面名、任务标题、通知、网页 title / PWA manifest 全部统一；包名与原生桥 `com.speakeasy.app` / `SpeakEasyNative` 保持不变以兼容已安装数据）。
- **桌面图标**：Light / Dark 两版自适应图标，通过两个 `activity-alias`（`LauncherLight` / `LauncherDark`）按系统明暗模式自动切换；Android 13+ 提供 `<monochrome>` 主题图标层。
- **通知图标**：通知栏左侧使用 Dynamic 渐变版应用图标；聊天消息右侧显示发消息的 AI 头像，无头像的互动通知回退为 Dynamic 渐变图；状态栏小图标为单色 S 剪影（`jpush_notification_icon`）。
- **开屏**：Android 12+ 系统启动图标设为透明，仅保留品牌底色；随后展示渐变夜空 + SONDER 品牌开屏（静态 HTML 内联样式，React 挂载后淡出）。
- 更完整的设计决策、素材来源与真机验证见 [docs/BRANDING.md](docs/BRANDING.md)。

---

## 14. 测试与验收

```bash
# V3 核心链路（人物/动态/好友/事件/音色/头像/语言）
pytest backend/tests/test_v3_*.py -q

# 全量
pytest backend/tests -q
```

- 全量后端测试：**950 passed**（`pytest backend/tests -q`，离线运行）；
- 前端：`tsc --noEmit` + **17 files / 109 tests**（vitest）+ `npm run build` 静态导出；
- 验收清单：[docs/V3_ACCEPTANCE.md](docs/V3_ACCEPTANCE.md)（场景 A–AF，含截图）；
- 接口文档：[docs/V3_API.md](docs/V3_API.md)；
- 移动端真机问题与修复记录：[docs/MOBILE_RELIABILITY_FIXES.md](docs/MOBILE_RELIABILITY_FIXES.md)；
- 已知历史遗留：少量 V1/V2 深链路用例依赖真实 Key/外网（如多语种 TTS），不在 V3 验收范围。

### 手机端验收要点

- 发现 → 二级菜单「动态 / 偶遇一个人 / 通知」；
- 动态页标题居中、左上角返回、无刷新按钮（下拉 / 底部上拉刷新）；
- 320 / 360 / 390 / 414px 宽度下按钮与文字不重叠、不换行；
- 人物朗读、好友试听、聊天语音是**同一个声音**。

---

## 15. 项目现状与路线图

**已完成**

- V3 每日世界 + 信息流 + 好友关系 + 社交事件引擎；
- 一人一音色（Fish）、头像一致性、语言角标校验、分语言水平；
- 动态翻译 / 朗读 / 缓存版本化、播放互斥；
- 用户图文动态（含上传）、兴趣匹配、可见性；
- Web / PWA / Android APK 三端；
- 云端部署脚本（备份 / 回滚 / 健康检查）。

**接下来可以做**

- 冷门语种内容增强（目前 ja/ko/pt 等主要靠兜底模板）；
- 用母语做翻译目标、按水平改写原文；
- 更强的内容审核与安全策略；
- 语音通话的端到端延迟和打断体验优化；
- 数据导出 / 清除等隐私功能完善。

---

## 文档索引

| 文档 | 内容 |
|---|---|
| [docs/BRANDING.md](docs/BRANDING.md) | Sonder 品牌：命名、图标（Light/Dark/Dynamic）、开屏、通知图标与真机验证 |
| [docs/MOBILE_RELIABILITY_FIXES.md](docs/MOBILE_RELIABILITY_FIXES.md) | 真机 9 项问题、推送注册/通知头像、tab 切换、品牌升级与验收记录 |
| [docs/MOBILE_REBUILD.md](docs/MOBILE_REBUILD.md) | Android 4.x 构建、签名、JPush/厂商推送配置与发版记录 |
| [docs/V3_API.md](docs/V3_API.md) | V3 接口、数据模型、事件引擎、缓存与文案原则 |
| [docs/V3_ACCEPTANCE.md](docs/V3_ACCEPTANCE.md) | V3 验收场景与截图 |
| [docs/V3_MOBILE.md](docs/V3_MOBILE.md) | 移动端与 APK 说明 |
| [docs/V3_PLAN.md](docs/V3_PLAN.md) | V3 实施计划 |
| [docs/REDESIGN_WEB.md](docs/REDESIGN_WEB.md) | Web 重设计说明 |
| [docs/REDESIGN_WEB_ACCEPTANCE.md](docs/REDESIGN_WEB_ACCEPTANCE.md) | Web 重设计验收报告 |
| [docs/V2_GUIDE.md](docs/V2_GUIDE.md) | V2 运行 / 验收指南 |
| [docs/local-mode.md](docs/local-mode.md) | 无服务器本地模式 |
| [deploy/README.md](deploy/README.md) | 服务器部署细节 |

## 许可与合规

- 代码仅用于学习与个人项目；第三方内容（Fish 音色、Wikimedia / Openverse 图片）遵循各自许可，图片在 UI 内保留 CC 署名；
- AI 生成内容在界面上以 `AI` 角标标识，不代表真人；
- Key、聊天记录、上传图片都只存在部署者自己的服务器/设备上，仓库不包含任何密钥或个人数据。

