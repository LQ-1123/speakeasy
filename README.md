# Sonder · Every life is a world

> **在线体验：[打开 Sonder](https://47-104-24-236.sslip.io)** · 邮箱验证码登录，新账号需要内测邀请码。
> **获取内测码：请联系 [yulinsun547@gmail.com](mailto:yulinsun547@gmail.com)**。这是本项目的内测联系邮箱，不代表任何第三方平台或服务商。
>
> **Sonder v4.4.0（versionCode 440）**：[下载 Android APK](https://github.com/LQ-1123/speakeasy/releases/download/v4.4.0/Sonder-v4.4.0-release.apk) · [发布说明与 SHA-256 校验信息](https://github.com/LQ-1123/speakeasy/releases/tag/v4.4.0)。GitHub Release 仅附 APK；源码可从仓库获取。
> 本版包含邮箱会话、多账号上游资源池和有界聊天/语音调度。容量证据及限制见[上游资源池](docs/PROVIDER_POOLS.md)与[容量测试说明](docs/PERFORMANCE_CAPACITY.md)；编译与离线测试不代表完成真机或真实 2C2G 服务器验收。

以下品牌截图为历史版本示意，当前界面与登录流程以 v4.4.0 为准。

<p align="center">
  <img src="docs/screenshots/sonder-splash.png" width="230" alt="Sonder 品牌开屏" />
  <img src="docs/screenshots/sonder-mobile.png" width="230" alt="Sonder 手机端消息页" />
</p>

> 一个「AI 人物真的在生活」的社交式口语练习 App：
> 每天生成一个有人物、有动态、有声音的小世界，先像刷朋友圈一样读和听，
> 再和其中的人加好友、聊天、打语音电话，把英语（或其他语言）用起来。

- 形态：**Web / PWA / Android APK**
- 线上体验：<https://47-104-24-236.sslip.io>
- 当前版本：V3（AI Persona 社交世界）· 后端 FastAPI · 前端 Next.js 静态导出 · Android **4.4.0 / 440**
- 文档：[品牌与客户端说明](docs/BRANDING.md) · [V3 接口与数据模型](docs/V3_API.md) · [V3 验收清单](docs/V3_ACCEPTANCE.md) · [移动端](docs/V3_MOBILE.md) · [本地模式](docs/local-mode.md)

---

## 界面预览

以下截图保留项目演进过程中的界面示例，不作为当前版本逐项验收证据。

桌面端「发现」信息流：像刷朋友圈一样读 AI 人物的生活，支持翻译、朗读、点赞与评论。

![发现信息流（桌面端）](docs/screenshots/sonder-discover.jpg)

响应式与设计系统的历史预览：同一套 Material 3 Expressive 主题适配桌面与手机布局。

![Sonder Design Preview](docs/screenshots/sonder-design-preview.jpg)

历史手机端截图（390px）；当前入口与导航以随 APK 打包的客户端为准。

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

- 每日人物供给根据近期活跃关系与内容缺口调整，也可用 `SE_DAILY_PERSONAS` 显式指定；每人动态数可通过 `SE_DAILY_POSTS_PER_PERSONA` 配置（默认 3 条）；
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
- 内容成本控制：读取已生成的信息流通常不触发新一轮生成；每日世界、聊天、翻译、记忆整理、建议与社交事件等仍可能调用 LLM，朗读和语音识别也会消耗对应上游额度；
- 稳定声线：生产默认严格人物音色；Fish 不可用时显示失败与重试，不用提示音或其他人的声音冒充成功。离线规则/mock 测试与正式语音服务分开配置；
- 部署：FastAPI 同源托管前端静态文件，单个 Uvicorn worker + SQLite WAL + nginx HTTPS；2C2G 是待实测的部署目标，不能由开发机模拟测试推定容量；
- 多账号上游资源池：Fish / Deepgram / OpenAI 兼容 LLM / SiliconFlow STT 支持加密凭据、同模型路由、共享配额、冷却、排空和显式回滚，详见[配置指南](docs/PROVIDER_POOLS.md)。

---

## 6. 产品主流程

```mermaid
flowchart LR
  A[首次打开 / 邮箱验证码登录<br/>新账号填写邀请码] --> B[填写资料<br/>昵称·母语·目标语言·水平]
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
| **用户 user** | 邮箱账号关联 `v3_users`；Web 使用 HttpOnly 会话 Cookie + CSRF，Android 使用原生加密保存的会话与 `X-Session-Token` |
| **AI 人物 persona** | 平台生成的人物，有资料、头像、音色、动态；不代表真人 |
| **好友关系 friendship** | 用户 ↔ 人物，`pending` / `accepted`；接受后生成 `friends` 行复用聊天 |
| **内容 content** | 人物动态、用户动态、评论、点赞 |
| **社交事件 social_event** | AI 对用户公开动态的赞 / 评论 / 好友申请，有未读和处理状态 |

> 正式邮箱模式拒绝旧设备注册和旧身份头，不会匿名回退到 `local`。同邮箱复用同一账号；历史设备身份与数据归属按[账号上线与归档指南](docs/EMAIL_CODE_AUTH_ROLLOUT.md)处理，不自动并入新账号。

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
| `friends` / `messages` / `moments` / `saved_expressions` | `owner_user_id` | 仅已认证的所属账号；历史归档不向新账号自动开放 |
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
     ↘ 严格模式失败：明确错误 / 可重试，不替换人物声线
URL: /api/v3/persona-posts/{id}/speech?v=<content_hash>&a=<voice_version>
缓存: data/v3_audio/v3/{post}-{hash}-{voice}.mp3 + immutable + 版本化 URL
```

- 人物音色在启动时分配（`assign_fish_voices`），写库并同步到好友；
- 信息流朗读、好友试听、聊天、电话共用同一套人物音色；
- 识别链路：按已配置的云端 STT（SiliconFlow / Deepgram）或显式本地配置工作；云精简部署不包含本地 Whisper，mock 仅用于离线开发/测试。

### 8.3 部署拓扑

```
用户手机/浏览器 ──HTTPS──> 云服务器
                             ├─ nginx（443 → 127.0.0.1:8000；公开哈希静态资源）
                             ├─ systemd: 单个 uvicorn worker（仅信任回环代理头）
                             ├─ data/：SQLite + 音频 + 图片 + 配置
                             └─ 出站代理 → Fish / DeepSeek / 图库
Android APK（内置 Web 资源，https://localhost）──HTTPS──> 同一个后端
```

---

## 9. 数据流

### 9.1 启动与身份

```mermaid
sequenceDiagram
  participant C as 客户端
  participant F as FastAPI
  participant DB as SQLite
  C->>F: POST /api/v3/auth/email-code/request（邮箱）
  F-->>C: challenge_id（验证码通过邮件发送）
  C->>F: POST /api/v3/auth/email-code/verify（验证码；首次含邀请码）
  F->>DB: 同邮箱复用账号，创建可撤销会话
  F-->>C: Web HttpOnly Cookie / Android 加密保存会话
  C->>F: 后续请求携带会话；Web 写请求附 CSRF
  F->>F: 会话校验与账号数据隔离
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
  provider-master.key    # provider 凭据加密主密钥，独立受控备份
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
| FR-1 | 邮箱账号与数据隔离 | 新邮箱需邀请码；同邮箱恢复账号；会话可撤销；旧设备身份不匿名回退 |
| FR-2 | 每日世界 | 按活跃关系/内容缺口或显式配置供给；24h TTL；互动保留 7 天；失败有种子兜底 |
| FR-3 | 人物一致性 | 头像、资料、语言、音色在信息流 / 主页 / 好友 / 聊天中一致 |
| FR-4 | 动态消费 | 翻译、朗读、赞、评论、多图、图注、语言角标正确 |
| FR-5 | 好友关系 | 偶遇 / 主页加好友；资料页显示 TA 的动态；接受申请生成好友 |
| FR-6 | 聊天与语音 | 文字 / 语音条 / 电话；记忆延续；表达本收藏 |
| FR-7 | 用户动态 | 图文发布 + 三档可见性；公开动态触发 AI 社交事件 |
| FR-8 | 多语言 | 母语 / 多目标语言 / 分语言水平；信息流按目标语言过滤 |
| FR-9 | 多端适配 | 桌面分栏；手机底部 Tab + 二级页面；320–414px 无重叠换行 |
| FR-10 | 成本与失败处理 | 预生成与缓存减少重复调用；上游次数预算/共享配额；严格音色失败与重试可见 |

### 10.2 非功能需求

- **成本**：读取已生成内容与缓存减少调用；聊天、翻译、每日世界、记忆等按配置消耗额度，次数预算不等于货币上限；
- **延迟**：朗读音频提前预热 + 磁盘缓存；聊天逐句 TTS；
- **一致性**：人物身份、头像、音色、语言角标四处一致；
- **可靠性**：外部服务失败、超时或满载时明确显示错误/重试；严格人物音色不替换；每日世界可按配置回落种子；
- **可维护性**：迁移幂等（`CREATE TABLE IF NOT EXISTS` + `ALTER TABLE`）；发布前制作一致性备份并制定保留新数据的回滚方案；
- **移动端**：触控区域、手势刷新、安全区、无文字图标重叠。

---

## 11. 技术栈与目录

| 层 | 技术 |
|---|---|
| 前端 | Next.js 15（静态导出）、React 19、Tailwind CSS 4、lucide-react、DiceBear Avataaars |
| 移动端 | Capacitor（Android，`com.speakeasy.app`） |
| 后端 | FastAPI、uvicorn、Pydantic v2、SQLite（WAL） |
| LLM | DeepSeek / 任意 OpenAI 兼容接口 |
| 语音合成 | Fish Audio / 角色已绑定的有效声线；生产严格音色与显式失败 |
| 语音识别 | SiliconFlow、Deepgram 流式；本地 faster-whisper / mock 仅用于显式选择的开发或本地配置 |
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
| Node.js | 22+（Capacitor 8 CLI 要求）；按各工程锁文件安装 |
| 网络 | 由部署环境验证已配置上游的可达性；如需代理仅配置服务器出站，不把凭据交给客户端 |

### 一个进程跑前后端（推荐）

```bash
git clone https://github.com/LQ-1123/speakeasy.git
cd speakeasy

python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r backend/requirements-cloud.txt

cd frontend
npm ci
npm run build:backend         # 产物写入 backend/app/static/app
cd ..

python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>。本地配置与正式内测应分开；正式邮箱模式需要 SMTP、会话密钥和邀请码，见[邮箱账号部署指南](docs/EMAIL_CODE_AUTH_ROLLOUT.md)。不要把生产 `.env`、数据库或密钥复制进测试目录或提交到仓库。

| 管理位置 | 用途 |
|---|---|
| Admin → API 与密钥 | 配置并逐项验证 LLM / Fish / Deepgram / SiliconFlow 资源；支持多账号池 |
| 受保护的服务器环境配置 | SMTP、会话与媒体密钥、管理员凭据、推送服务端密钥 |
| 独立离线测试配置 | 合成凭据、规则或 mock 上游；不代表真实模型/语音可用 |

> 服务可以在没有模型 Key 时启动，但正式聊天、语音和验证码登录依赖相应服务配置。严格人物音色失败会明确报错，不承诺无 Key 也能完成全部体验。

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

先在开发机生成静态产物，再按 [deploy/README.md](deploy/README.md) 的维护窗口流程发布：SQLite backup API 一致性备份、单独保护配置与 provider 主密钥、保留 `.env` / `data/` / systemd / TLS，再做健康和登录链路检查。

`deploy/deploy_v3.sh` 保留为历史脚本，其逐文件复制活动数据库及 WAL、直接覆盖恢复的步骤不提供一致性保证；不要把它当作可直接执行的在线升级/回滚方案。

### Android APK

```powershell
# 在已配置原签名的开发机打包；脚本会重新构建并打入前端
.\mobile\build-apk.ps1 -Mode release -ApiBase https://47-104-24-236.sslip.io `
  -VersionCode 440 -VersionName 4.4.0 -BuildRoot D:/speakeasy-android-build
```

用户安装请使用 [v4.4.0 GitHub Release APK](https://github.com/LQ-1123/speakeasy/releases/download/v4.4.0/Sonder-v4.4.0-release.apk)，SHA-256 见[该版发布说明](https://github.com/LQ-1123/speakeasy/releases/tag/v4.4.0)。构建脚本的本地产物保存在被 Git 忽略的 `deliverables/`，不是仓库内的下载链接。

APK 内置 Web 前端，从 `https://localhost` 加载，API 只连接构建时配置的 HTTPS 源。Web 页面变更要重新打 APK 才能更新已安装客户端。包内不含上游服务端秘密；JPush 客户端 AppKey 与仅服务器使用的 Master Secret 必须区分。签名、可选厂商通道、通知与原生通话见 [mobile/README.md](mobile/README.md)。GitHub 发版不自动修改服务端更新目录或宣传页。

### Android 客户端：品牌、图标与通知

- **应用名**：Sonder（桌面名、任务标题、通知、网页 title / PWA manifest 全部统一；包名与原生桥 `com.speakeasy.app` / `SpeakEasyNative` 保持不变以兼容已安装数据）。
- **桌面图标**：Light / Dark 两版自适应图标，通过两个 `activity-alias`（`LauncherLight` / `LauncherDark`）按系统明暗模式自动切换；Android 13+ 提供 `<monochrome>` 主题图标层。
- **通知图标**：通知栏左侧使用 Dynamic 渐变版应用图标；聊天消息右侧显示发消息的 AI 头像，无头像的互动通知回退为 Dynamic 渐变图；状态栏小图标为单色 S 剪影（`jpush_notification_icon`）。
- **开屏**：旧版渐变夜空 Web 品牌开屏已移除；历史截图与品牌决策保留在文档中。
- 历史设计决策、素材与当时的验收记录见 [docs/BRANDING.md](docs/BRANDING.md)，不代表 v4.4.0 已完成全部真机检查。

---

## 14. 测试与验收

```bash
# V3 核心链路（人物/动态/好友/事件/音色/头像/语言）
pytest backend/tests/test_v3_*.py -q

# 全量
pytest backend/tests -q
```

- 最新离线回归与浏览器验证结果见[资源池验证记录](docs/PROVIDER_POOLS.md#本次验证结果2026-11-14)，测试命令及来源指纹以对应报告为准；历史 950 项后端 / 109 项前端结果属于 4.1.x，不作为本版结果；
- 验收清单：[docs/V3_ACCEPTANCE.md](docs/V3_ACCEPTANCE.md)（场景 A–AF，含截图）；
- 接口文档：[docs/V3_API.md](docs/V3_API.md)；
- 移动端真机问题与修复记录：[docs/MOBILE_RELIABILITY_FIXES.md](docs/MOBILE_RELIABILITY_FIXES.md)；
- 容量验证：100 个合成用户按 3.33 轮/秒持续 30 分钟，6,000/6,000 轮模拟音频在期限内可解码；长测属于进程启动时源码快照，测试期间工作树有变动。最终冻结源码另做 60 秒 200/200 复测。均为 Windows 开发机与回环假上游，不能证明真实 2C2G Linux、真实账号配额、音色质量或费用上限，见[完整边界](docs/PROVIDER_POOLS.md)与[测量方法](docs/PERFORMANCE_CAPACITY.md)。
- 真实邮件收取、原签名覆盖升级、锁屏/厂商推送、通话与系统音频行为需单独验收；本次 GitHub 发版不凭编译结果宣称真机通过。

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
- 邮箱验证码与邀请码内测、可撤销会话；
- 多账号上游资源池、有界队列/期限、合成负载测量工具与安全部署指南。

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
| [mobile/README.md](mobile/README.md) | 当前 Android 内置前端、HTTPS、签名与设备验收边界 |
| [docs/APP_UPDATE.md](docs/APP_UPDATE.md) | GitHub APK 发布与服务端更新渠道的区别、签名及校验 |
| [docs/PROVIDER_POOLS.md](docs/PROVIDER_POOLS.md) | 多账号上游配置、共享配额、调度限制与模拟验证结果 |
| [docs/PROVIDER_POOL_IMPLEMENTATION.md](docs/PROVIDER_POOL_IMPLEMENTATION.md) | 资源池实现与验收边界 |
| [docs/PERFORMANCE_CAPACITY.md](docs/PERFORMANCE_CAPACITY.md) | 有界聊天、负载工具、资源测量与生产容量边界 |
| [docs/CAPACITY_IMPLEMENTATION.md](docs/CAPACITY_IMPLEMENTATION.md) | 容量目标、实现决策与验收条件 |
| [docs/EMAIL_CODE_AUTH_ROLLOUT.md](docs/EMAIL_CODE_AUTH_ROLLOUT.md) | 正式邮箱登录、SMTP 配置、历史身份归档与回滚 |
| [docs/V3_API.md](docs/V3_API.md) | V3 接口、数据模型、事件引擎、缓存与文案原则 |
| [docs/V3_ACCEPTANCE.md](docs/V3_ACCEPTANCE.md) | V3 验收场景与截图 |
| [docs/V3_MOBILE.md](docs/V3_MOBILE.md) | 移动端与 APK 说明 |
| [docs/V3_PLAN.md](docs/V3_PLAN.md) | V3 实施计划 |
| [docs/REDESIGN_WEB.md](docs/REDESIGN_WEB.md) | Web 重设计说明 |
| [docs/REDESIGN_WEB_ACCEPTANCE.md](docs/REDESIGN_WEB_ACCEPTANCE.md) | Web 重设计验收报告 |
| [docs/V2_GUIDE.md](docs/V2_GUIDE.md) | V2 运行 / 验收指南 |
| [docs/ACCOUNTS_AND_PRIVACY.md](docs/ACCOUNTS_AND_PRIVACY.md) | 多用户与数据隔离；邮箱验证码登录、邀请码与会话管理；[上线与历史归档](docs/EMAIL_CODE_AUTH_ROLLOUT.md) |
| [docs/TEEN_MODE.md](docs/TEEN_MODE.md) | 青少年模式：监护绑定、60 分钟/30 分钟提醒/宵禁、HTTP 与实时通道门禁、推送抑制、AI 身份披露 |
| [docs/PERSONALIZATION.md](docs/PERSONALIZATION.md) | 个性化世界：统一标签 ID/多语言、推荐快照游标与 20% 探索、生成批次恢复、过期清理与人物供给 |
| [docs/ADS.md](docs/ADS.md) | 商业合作：管理台品牌/活动/素材/分配、聊天与动态投放、频控与原子预留、事件与留痕 |
| [docs/PROMPTS.md](docs/PROMPTS.md) | Prompt 控制台：A–H 模块、可改写边界、与酒馆（SillyTavern）文风/角色卡的对应与迁移步骤 |
| [docs/local-mode.md](docs/local-mode.md) | 无服务器本地模式 |
| [deploy/README.md](deploy/README.md) | 服务器部署细节 |

## 许可与合规

- 代码仅用于学习与个人项目；第三方内容（Fish 音色、Wikimedia / Openverse 图片）遵循各自许可，图片在 UI 内保留 CC 署名；
- AI 生成内容在界面上以 `AI` 角标标识，不代表真人；
- 仓库与 Release 不应包含密钥、私有签名文件、聊天记录或用户上传。实际使用配置的 LLM / STT / TTS / SMTP / 推送等服务时，必要请求数据会发送给相应服务商；部署者需要核对其隐私政策与数据处理条款。

