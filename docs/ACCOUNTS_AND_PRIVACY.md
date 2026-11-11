# 账号体系与数据隔离（第一期）

> **当前状态（2026-11 起）：首版改为「免登录」。**
> 邮箱/密码登录、邀请码、邮箱验证的前端入口已隐藏；多用户仍靠**设备身份**隔离，
> 唯一进入门槛是首次的**用户信息采集引导**（`Onboarding`）。后端账号接口、测试与
> 数据隔离逻辑全部保留，恢复登录时把前端 `AuthGate` 挂回 `App.tsx`、设
> `SE_REQUIRE_AUTH=1` 即可重新启用（见文末「下一期」）。
>
> 对应《Sonder 多用户、个性化世界与商业化升级方案》第 3 节「第一期：账号、隔离与分龄能力」。
> 以下设计为账号能力完整版（保留待恢复）；数据隔离：信息流只含本人动态、人物评论按用户隔离、
> 私聊情绪按关系隔离、音频/图片归属校验——这些**在免登录首版仍然生效**。


## 1. 身份模型

### 1.1 三种身份，一个 `user_id`

沿用现有 `v3_users.id`（好友、动态、聊天、事件的归属键），新增凭据层，不新建用户表：

| 身份 | 来源 | 能做什么 |
|---|---|---|
| `local` | 不带任何身份头 | 本地单用户/联调兼容；`SE_REQUIRE_AUTH=1` 时直接 401 |
| 设备身份 | `POST /api/v3/auth/register`（`device_key`） | 旧客户端；**绑定邮箱后立即失效**，不再签发 token |
| 邮箱账号 | 注册/登录 → 会话凭据 | 正式账号：可撤销会话、跨设备登录、找回密码 |

### 1.2 请求鉴权顺序（`app/v3/security.py::bind_current_user`）

1. `X-Session-Token` 头或 `Authorization: Bearer <token>` → 查 `v3_sessions`；
2. `sonder_session` HttpOnly Cookie（Web）→ 同样查会话表；**非安全方法必须带
   `X-CSRF-Token` 且与 `sonder_csrf` 一致**，否则 403；
3. 旧 `X-User-Id` + `X-User-Token`：该身份一旦绑定账号 → 401；
4. 无凭据 → `local`（`SE_REQUIRE_AUTH=1` 时 401）。

状态码约定：401 未登录/凭据失效/设备路径已关闭，403 权限（邀请码、CSRF、停用、年龄门禁），
409 冲突（邮箱已注册），422/400 参数不合法。

### 1.3 密码与令牌

- 密码哈希：**Argon2id 优先**（安装 `argon2-cffi` 时自动启用，m=64MiB/t=3/p=2）；
  离线环境退化为标准库 `hashlib.scrypt`（N=16384,r=8,p=1）。哈希串自带算法前缀，
  两种格式可共存校验，后续装回 argon2-cffi 不需要迁移。
- 会话 token、邮箱令牌：只存 `sha256`，明文只在响应里返回一次；会话默认 30 天，
  可撤销、可过期；改密/找回密码撤销该账号**全部**会话。
- 邀请码：`UPDATE ... WHERE used_count < max_uses` 原子占用，支持有效期与停用。

## 2. 数据模型（`backend/app/v3/accounts.py`、`backend/app/v3/privacy.py`、`backend/app/v3/media_access.py`）

| 表 | 作用 |
|---|---|
| `v3_accounts` | 一个 user_id 一份凭据：邮箱唯一（`COLLATE NOCASE`）、密码哈希、验证时间、状态 |
| `v3_invite_codes` | 邀请码：限量、有效期、可停用、使用计数 |
| `v3_email_tokens` | 邮箱验证 / 找回密码令牌：一次性、30 分钟、只存哈希 |
| `v3_sessions` | 设备会话：可撤销、可过期、记录 device_key 尾号与平台 |
| `v3_age_verifications` | 年龄核验结论（独立于可编辑的资料生日），含监护人与核验方式 |
| `v3_age_audit` | 核验/监护变更审计 |
| `persona_user_emotion` | 「用户 × 人物」私聊情绪（公共情绪仍由世界事件写 `personas.emotion_state`） |
| `media_assets` | 音频/图片归属（owner_user_id + kind + filename） |

全部 DDL 都是 `CREATE TABLE IF NOT EXISTS`，可反复执行；`main.py` 启动时统一
`ensure_schema → ensure_client_schema → ensure_accounts_schema → ensure_privacy_schema → ensure_media_schema`。
读取侧另有惰性补建（老库/精简测试装配不会因缺表 500）。

## 3. 接口（`backend/app/v3/accounts_router.py`，前缀 `/api/v3/auth`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/register/email` | 邮箱注册（邀请码 + 密码强度校验）；返回会话凭据、`dev_verification_token`（仅 `SE_MAIL_DEV=1`） |
| POST | `/login` | 邮箱密码登录；5 次失败/15 分钟限频 |
| POST | `/logout` | 撤销当前会话并清 Cookie |
| POST | `/logout-all` | 撤销该账号全部会话 |
| POST | `/verify-email` | 消费邮箱验证令牌 |
| POST | `/resend-verification` | 重发验证令牌（需登录） |
| POST | `/password/forgot` | 找回密码（防枚举：一律 200，`SE_MAIL_DEV=1` 下发令牌） |
| POST | `/password/reset` | 按令牌改密，撤销全部会话 |
| POST | `/password/change` | 原密码校验后改密，撤销全部会话 |
| POST | `/bind-legacy` | 用旧设备身份头证明归属后绑定邮箱，保留好友/聊天/动态；成功后设备 token 清空 |
| GET | `/sessions` | 会话列表（device_key 只回尾号） |
| DELETE | `/sessions/{id}` | 撤销指定会话 |
| GET | `/capabilities` | 账号状态 + 分龄 + 可用能力（`features.ads` 等） |
| GET | `/identity` | 当前请求身份的解析结果（排查 401/降级 local） |

注册/登录响应同时设置 `sonder_session`（HttpOnly）与 `sonder_csrf`（非 HttpOnly）Cookie，
并返回 `csrf_token` 供前端写操作使用。Android 继续用 `X-Session-Token` 头（沿用现有加密身份存储）。

### 3.1 运营侧接口（`backend/app/admin/api/accounts.py`，前缀 `/api/admin/accounts`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/invites` | 邀请码列表（含已用/剩余/是否停用） |
| POST | `/invites` | 创建邀请码（可指定 code/次数/有效期） |
| POST | `/invites/{code}/disable` | 停用邀请码（已注册用户不受影响） |
| GET | `/users` | 按邮箱/用户 id 模糊查询账号（不返回密码哈希与 token） |
| GET | `/users/{uid}/capabilities` | 该用户当前账号状态、分龄与能力 |
| POST | `/users/{uid}/age` | 写入年龄核验结论（运营核验，不收集证件），变更进 `v3_age_audit`；响应直接带更新后的能力 |

管理台统一鉴权与审计沿用现有 `require_admin`（写操作自动落 `admin_audit`），客户端会话与本模块完全解耦。

### 3.2 能力（capabilities）

```json
{
  "account": {"type": "email", "status": "active", "email_verified": true, "has_password": true},
  "age": {"group": "adult|teen|unknown", "verified": true, "guardian_bound": false, "enforced": true},
  "features": {"chat": true, "calls": true, "feed": true, "ads": true,
               "intimate_relationship": true, "help": true, "learning": true}
}
```

规则：设备身份/`local` 永不投广告；`SE_AGE_GATE=1`（分龄阶段开关）时——成年可广告与亲密关系，
青少年无广告/无亲密关系但保留普通交流，未知或待核验只保留注册处理与帮助入口。
`features.ads` 只是「允许投」的必要条件，最终投放还要过用户主动开启与频控（第三期）。

## 4. 数据隔离决策

| 内容 | 规则 |
|---|---|
| 用户动态（`user_posts`） | **只出现在作者自己的信息流**；不再向其他真人展示（public/friends/private 只影响未来 AI 可见性语义） |
| 人物动态下的评论 | **按评论者隔离**：真人评论只对本人可见；带 `user_id` 的 AI 回复也只对该用户可见；世界级 AI 评论（`author_type='persona'` 且 `user_id=''`）属于共享世界，人人可见。评论计数与列表同一口径 |
| 私聊情绪 | 写入 `persona_user_emotion(user_id, persona_id)`；`personas.emotion_state` 只由世界事件（`life_events.apply_event`）更新 |
| 音频/图片 | 落盘时登记 `media_assets(owner_user_id, kind, filename)`：私聊语音/图片、通话逐句语音、动态配图、上传图片。`GET /api/v2/audio/{filename}`、`/api/v2/media/{filename}` 对已登记资产校验归属，跨用户 403；播放器可用 `GET /api/v3/media/access?kind=&filename=` 领取 30–3600s 媒体令牌（`?t=`，仅 owner 可领）；未登记历史文件保持可读并带 `X-Legacy-Asset: 1`（迁移期兼容） |
| 后台任务/推送 | 沿用 `run_as_user` 式显式用户上下文；推送设备与会话解绑在退出时由客户端清理（见「下一期」） |

「AI 世界可互动／AI 好友可互动／仅自己」的文案调整随前端改版落地；后端行为已按「不展示给其他真人」执行。

## 5. 配置项

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `SE_INVITE_REQUIRED` | `1` | 注册是否必须邀请码（邀请内测） |
| `SE_MAIL_DEV` | `0` | 1 时把邮箱令牌放进响应，便于联调/自动化测试 |
| `SE_SMTP_HOST` / `SE_SMTP_PORT` / `SE_SMTP_USER` / `SE_SMTP_PASSWORD` / `SE_SMTP_FROM` | 空 | 配置后真实发信；未配置只记日志 |
| `SE_REQUIRE_AUTH` | `0` | 生产置 1：无凭据请求不再降级 `local` |
| `SE_AGE_GATE` | `0` | 分龄门禁开关（青少年阶段开启） |
| `SE_MEDIA_SECRET` | 开发默认 | 媒体令牌 HMAC 密钥，生产必须配置 |
| `SE_SESSION_TTL_DAYS` | `30` | 会话有效期 |

## 6. 迁移与兼容

- **旧数据不自动分配**：历史 `local` 数据仍归 `local`，不会划给任何新账号；
  设备用户通过 `/auth/bind-legacy` 显式升级，`user_id` 不变，好友/聊天/动态全保留。
- **设备身份关闭**：绑定后 `v3_users.token_hash` 清空，`/api/v3/auth/register` 对同一
  `device_key` 返回 401，客户端应回到登录态（不再自动建号）。
- 老客户端（不带身份头）行为不变；管理台鉴权、审计、任务队列未改动。

## 7. 测试证据

```powershell
$env:PYTHONIOENCODING='utf-8'
# 新增/相关用例（45 passed）
.\.venv\Scripts\python.exe -m pytest backend/tests/test_auth_email.py backend/tests/test_admin_accounts.py `
  backend/tests/test_feed_privacy.py backend/tests/test_emotion_privacy.py backend/tests/test_media_ownership.py `
  backend/tests/test_uploads_access.py backend/tests/test_reports.py -q
# 全量（93 个测试文件，1255 用例）
.\.venv\Scripts\python.exe -m pytest backend/tests -q
```

> 测试默认 `SE_MEDIA_ENABLED=0`（`backend/tests/conftest.py`）：离线环境抓开放图库会一直等
> 超时（曾导致 `test_v3_world.py` 整体挂起），纯文字生活动态仍会生成。需要真图的用例
> 请显式 `monkeypatch.setenv("SE_MEDIA_ENABLED", "1")`。

新增/更新覆盖：
- `test_auth_email.py`（11）：邀请码准入、邮箱唯一、验证令牌一次性、登录限频、会话撤销/退出、
  找回密码撤销全部会话、设备身份绑定保数据且设备路径失效、Cookie+CSRF、`SE_REQUIRE_AUTH`、
  停用账号拒用会话、分龄能力矩阵、密码哈希加盐回环。
- `test_admin_accounts.py`（2）：邀请码创建/列表/停用、账号查询脱敏、年龄核验即时生效、写操作进审计。
- `test_feed_privacy.py`（3）：两位用户信息流互不可见、人物评论按评论者隔离（含计数）、
  世界级 AI 评论仍公开。
- `test_emotion_privacy.py`（9）：私聊不改公共情绪、关系情绪按用户隔离、世界事件仍更新公共情绪。
- `test_media_ownership.py`（10）：跨用户读音频/聊天图片/动态配图 403、本人 200、媒体令牌
  （owner 才能领、匿名可用、他人冒用 403）、会话凭据可申领、历史文件兼容、登记幂等。
- 通话/事件通道的身份入口（`calls.require_user`）已改为只要求「已登录」：会话凭据与旧设备头
  均可，不再强制 `X-User-Id` + `X-User-Token`，邮箱账号可直接发起通话。
- `test_uploads_access.py`（6）：`/uploads/*` 归属校验（owner 200 / 他人 403 / 匿名 403）、
  媒体令牌、未登记历史文件兼容（`X-Legacy-Asset: 1`）、目录穿越拒绝、序列化签名 URL 可读。
- `test_reports.py`（4）：举报创建/我的列表、类别与对象校验、24h 去重、每日上限、
  运营状态机（reviewing→resolved，终态不可变）与汇总。
- `test_v3_api.py::test_two_users_are_isolated`、`test_v3_service.py::test_feed_visibility_rules`、
  `test_persona_call.py::test_call_turn_records_behavior_and_emotion` 随新语义更新断言。

## 9. 举报与媒体收口（本轮补充）

### 9.1 内容举报

| 接口 | 说明 |
|---|---|
| `POST /api/v3/reports` | 举报人物/动态/评论/私聊消息/广告/用户；类别 harassment/sexual/violence/self_harm/misinformation/spam/other；需登录 |
| `GET /api/v3/reports/mine` | 我的举报与处理状态 |
| `GET /api/admin/reports`、`GET /stats`、`POST /{id}/status` | 运营筛选/汇总/处理（`open→reviewing→resolved|rejected`，终态不可变） |

- 防刷：同一用户同一对象 24h 只记一次、每天最多 20 条；举报表**不复制被举报正文**（只存对象引用）；
- 广告的「关闭/举报」仍走 `/api/v3/ads/events`（30 天屏蔽该活动），这里用于通用内容；
- 写操作沿用管理台统一审计。

### 9.2 `/uploads/*` 受保护

- 原来的 `StaticFiles` 直挂换成 `ProtectedUploads`（[uploads_guard.py](../backend/app/v3/uploads_guard.py)）：
  已登记图片只有 owner（会话/设备身份均可）或有效 `?t=<媒体令牌>` 可读，其他用户 403、匿名 403；
- **未登记的历史文件保持可读**并带 `X-Legacy-Asset: 1`，老数据不中断；
- 浏览器 `<img>` 不带身份头：消息/动态序列化时由服务端给本人媒体 URL 自动补短时令牌
  （`media_access.signed_url` / `signed_media_list`，默认 1 小时），**前端无需改造**；
- 目录穿越/隐藏文件被拒；归属查询异常按「历史文件」降级而不是 500。

### 9.3 客户端会话接入（Web / Android 共用）

新增 [session.ts](../frontend/src/w4/v3/session.ts)，让新账号体系在现有前端可直接使用：

- `loginEmail` / `registerEmail` / `logoutSession` / `logoutAllSessions` /
  `authIdentity` / `listSessions` / `revokeSession` / 邮箱验证与找回密码；
- 登录成功后 `localStorage` 保存 `session_token` 与 `csrf_token`；
  `installApiIdentity()` 改为**会话优先**：所有 `/api/**` 请求带 `X-Session-Token`，
  写请求再加 `X-CSRF-Token`；登录/注册请求本身不带任何身份头（避免被设备身份兜底抢先）；
- `request()` 的 401 自动重注册只在「无会话」时触发，避免退出后又自动登录；
- Android 无 Cookie，完全依赖上述请求头；Web 生产环境仍由服务端 HttpOnly Cookie 兜底。

测试（`frontend/src/w4/v3/sessionAuth.test.ts`，3 个用例）覆盖：登录持久化与会话头注入、
写请求 CSRF、退出撤销并清空、登录失败透出服务端文案。

### 9.4 前端页面（登录门 / 设置·账号 / 举报）

> **已停用（2026-11 起）**：`AuthGate` 不再挂载到 `App.tsx`，设置页「账号」分区已移除；
> 新用户进来只走用户信息引导（`Onboarding`），多用户靠设备身份隔离。
> 以下组件与逻辑**保留在仓库**，恢复登录时重新挂载即可。

- **登录门**（[AuthGate.tsx](../frontend/src/w4/v3/AuthGate.tsx)）：服务端要求登录
  （`SE_REQUIRE_AUTH=1`）且本地无会话时自动弹层；也可用右下角「登录 / 注册」、
  设置·账号入口（`sonder:open-auth` 事件）或 `?login=1` 打开。
- **登录/注册/找回密码**（[AuthScreen.tsx](../frontend/src/w4/v3/AuthScreen.tsx)）：
  邮箱 + 密码 + 邀请码（内测）、两次密码一致性、找回密码两步（发信 → 重置码 + 新密码）；
  开发模式直接展示 `dev_verify_token` / `dev_reset_token`，方便内测。
- **设置·账号**（[AccountSection.tsx](../frontend/src/w4/components/AccountSection.tsx)）：
  登录状态与邮箱验证、退出登录/退出全部设备、设备会话列表与逐个撤销、
  合作推荐开关（仅成年+已核验）、青少年模式原因提示。
- **举报**（[ReportDialog.tsx](../frontend/src/w4/v3/ReportDialog.tsx)）：动态卡片「举报」
  入口，7 类违禁类别 + 可选说明，提交后提示运营处理（**免登录首版仍可用**，走设备身份）。
- 校验/错误文案/开发令牌提示被抽到 [authFlow.ts](../frontend/src/w4/v3/authFlow.ts)，
  有独立单测（`authFlow.test.ts`）。

### 9.5 广告卡片与可见曝光（前端）

- **动态流**（[SponsoredCard.tsx](../frontend/src/w4/v3/SponsoredCard.tsx)）：
  `kind="sponsored"` 条目渲染「广告」标识、品牌、审核文案、落地页；关闭/举报后本页隐藏
  并上报服务端（关闭/举报会屏蔽该活动 30 天）。
- **聊天**（[ChatPane.tsx](../frontend/src/w4/components/ChatPane.tsx) `ChatAdCard`）：
  `msg_type="ad"` 独立气泡，带「广告 · 品牌合作」标识与落地页；不参与朗读/翻译，
  也不进入记忆（服务端 `recent_messages` 已过滤）。
- **可见曝光规则**（[adEvents.ts](../frontend/src/w4/v3/adEvents.ts)）：进入视口并**连续可见
  ≥1 秒**才上报 `impression`（带 `visible_ms`），同一投放只报一次（sessionStorage 去重）；
  `click` 仅允许 http(s) 落地页。
- **青少年门禁**（[TeenGate.tsx](../frontend/src/w4/v3/TeenGate.tsx)）：登录账号每 60 秒心跳，
  `allowed=false` 时**全屏拦截**（宵禁/超限/未核验/未监护/限定人物），连续 30 分钟提醒每
  30 分钟弹一次（不强制断开）；逻辑抽到 [teenGuard.ts](../frontend/src/w4/v3/teenGuard.ts)。
- **退出清理**（[logoutFlow.ts](../frontend/src/w4/v3/logoutFlow.ts)）：退出时撤销服务端会话后
  停止音频、清理 `speakeasy.*`/`sonder.*`/`w4.*` 本地缓存与事件游标、清空 sessionStorage，
  随后 reload 断开 WebSocket/通话并按新身份重绑推送。

```powershell
cd frontend
node node_modules/typescript/bin/tsc --noEmit -p tsconfig.json   # exit 0
node node_modules/vitest/vitest.mjs run                          # 23 files / 145 tests passed
node node_modules/next/dist/bin/next build                       # exit 0（静态导出 10 页）
```

## 10. 下一期（按方案顺序）

1. **恢复账号登录（产品决定后）**：把 `AuthGate` / 设置·账号挂回前端，设
   `SE_REQUIRE_AUTH=1`；补邮箱验证落地页、邀请码发放流程、账号切换页面状态重置。
   —— 在此之前：免登录、只保留首次用户信息引导；广告/青少年按账号的能力暂时不可用。
2. **前端收尾**：标签多语言展示、运营可配文案。
3. **广告闭环收尾**：点击跳转归因与计费（本期只做留痕与汇总）。
4. **运营侧**：举报队列 UI、活动数据看板导出、监护人自助视图。
