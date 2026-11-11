# 管理台重新设计 · 契约与决策记录

> 本文件锁定跨模块契约。**改动任何契约前先改本文件**，否则前后端会漂移。
> 状态：第 0 期（地基前置验证）已完成。

---

## 0. 已验证的架构前提（第 0 期产出）

### 0.1 双根布局（已验证通过）

Next.js 15.5.28 静态导出（`output: "export"` + `trailingSlash: true`）下双根布局可用：

- `frontend/src/app/(app)/` —— 现有客户端页面（`/`、`/classic`、`/proto`、`/v2`、`/w4`、`/design-preview`），URL **全部不变**
- `frontend/src/app/(admin)/` —— 管理台，`/admin` 导出为 `out/admin/index.html`

**实测结论（2026-11-10）：**

| 检查项 | 结果 |
|---|---|
| `next build` 双根布局 | ✅ 通过（3.6s 编译，10 个静态页） |
| `out/admin/index.html` 存在 | ✅ 7341 字节 |
| 管理台页含开屏 `#sonder-splash` | ✅ **不含**（隔离成功） |
| 管理台页含主题 bootstrap 脚本 | ✅ **不含** |
| 管理台页含 `noindex` | ✅ 含 |
| 根页 `/` 含开屏 | ✅ 仍含（未受影响） |

> **注**：`(app)/layout.tsx` 移入路由组后，相对导入需改一层：
> `../globals.css`、`../../theme/tokens.css`、`../../theme/bootstrap`。

### 0.2 环境教训（避免重踩）

1. **Shell 写文件必须指定 UTF-8**。PowerShell `Set-Content` 默认走系统 ANSI（GBK），会把含中文的 `.tsx` 写成非法 UTF-8，导致 `next build` 报 `stream did not contain valid UTF-8`。
   → **改文件一律用 write/edit 工具**；非要用 shell，必须 `-Encoding utf8`。
2. **后台任务输出会被缓冲**。此前 `npm run build | Select-Object -Last N` 看起来"卡死" 10 分钟，实际构建只要 4 秒。判断任务进度看**产物 mtime**（`.next/BUILD_ID`、`out/`），不要只看管道输出。

---

## 1. API 契约总则

### 1.1 前缀与路由
- 管理 API 前缀仍是 `/api/admin`（**旧路径全部保留**，不破坏现有脚本与测试）
- 新代码按域拆到 `backend/app/admin/api/{overview,users,events,posts,media,personas,prompts,lexicons,config,release,jobs,audit}.py`
- `backend/app/admin/router.py` 退化为聚合器（`include_router`），`page_router` 保留页面路由

### 1.2 鉴权
- 守卫从"每个 handler 手写 `_guard(request)`"改为**路由级依赖**：
  ```python
  router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])
  ```
- `require_admin` 在 `backend/app/admin/auth.py`：Cookie 会话 → `X-Admin-Token` 头 → 未配口令时回环直通

#### CSRF：灰名单策略（P1 实际落地，重要）

**背景**：新 React 管理台需要 CSRF 保护；旧管理台（`page.py` 内联 JS）在 Cookie 登录下**不带** `X-Admin-CSRF`（只带 `X-Admin-Token`）。全强制会让旧台写操作全 403，全放行则新台无保护。

**落地方案**（`backend/app/admin/api/deps.py::guard`）：

| 请求形态 | 行为 |
|---|---|
| 写请求 + **带** `X-Admin-CSRF` 头 | **必须校验通过**（值错/过期/来源非法 → 403）。新 React 台走这条，全程受保护 |
| 写请求 + **不带**该头 | 放行，维持旧台行为（旧 `page.py` 与脚本走这条） |
| 写请求 + `Sec-Fetch-Site: cross-site/none` | 一律 403 |
| 带合法 `X-Admin-Token` | 豁免 CSRF |
| GET/HEAD | 不做 CSRF 校验 |

> **收口条件**：第 4 期删除 `page.py` 后，把 `guard()` 整体替换为 `Depends(require_admin)` 即可全强制。
> 回归保护：`backend/tests/test_admin_csrf_greylist.py`（9 条）钉死该策略，防止被"顺手改成全强制/全放行"。

### 1.3 返回格式
| 场景 | 约定 |
|---|---|
| 成功 | `200` + JSON 对象 |
| 参数校验失败 | `422` + `{"issues": ["...", ...]}` |
| 其他错误 | 对应 HTTP 状态码 + `{"detail": "..."}`（FastAPI 默认） |
| 分页 | `{"items": [...], "total": N, "offset": N, "limit": N}` |

> **现状**：旧代码大量返回 `{"ok": false, "issues": [...]}` 配 200。**新接口一律用 422**；旧接口保持原样（兼容），在聚合层不强行改造。

### 1.4 操作日志
- 所有**写操作**（POST/PUT/DELETE/PATCH）成功后写 `admin_audit`
- 记录：时间、IP、动作（`action`）、对象（`target`）、摘要（`summary`，**不记录密钥明文**）
- 由 `require_admin` 依赖链或统一中间件落地，避免每个 handler 手写

---

## 2. 数据库契约

新增表写在 `backend/app/v3/schema.py` 的 `DDL`，沿用 `CREATE TABLE IF NOT EXISTS`：

| 表 | 关键列 | 说明 |
|---|---|---|
| `admin_audit` | `id, at, ip, action, target, summary` | 操作日志 |
| `event_pool` | `id, title, description, event_type, start_at, location, slots, conditions, status, created_at` | 公共事件池 |
| `event_pool_claims` | `id, pool_id, persona_id, score, score_detail, event_id, claimed_at` | 领取记录，`UNIQUE(pool_id, persona_id)` 保幂等 |
| `post_queue` | `id, persona_id, content, media, source_type, source_event_id, status, publish_at, created_at` | 待发动态；`status ∈ queued/published/cancelled` |
| `prompt_overrides` | `persona_id, mode, module_key, text, updated_at`，`PRIMARY KEY(persona_id, mode, module_key)` | 角色 × 模式 × 模块 → 改写文本 |
| `prompt_templates` | `id, name, description, modules(JSON), created_at` | 模板 |

**关键决策**：待发队列用独立表，**不给 `persona_posts` 加状态列**——`persona_posts` 在代码里约 157 处引用，加状态列要逐处加过滤，漏一处就把未发布内容露给用户。

---

## 3. 前端契约

### 3.1 目录结构
```
frontend/src/app/(admin)/admin/page.tsx   # 唯一入口，客户端渲染
frontend/src/admin/
  api.ts          # 统一 fetch + CSRF + 错误格式化
  queryKeys.ts
  router.ts       # hash 路由（同 w4/navigation.ts 做法）
  shell/          # 侧栏、顶栏、确认框
  ui/             # 表格、表单控件、抽屉、滑杆、标签云、分段按钮、音频试听、图片选择器
  sections/{overview,users,world,personas,system}/
```

### 3.2 路由
- **单页 + hash 路由**：`/admin/#/users/u_xxx`
- 各分区用 `next/dynamic` 懒加载
- 样式只用 `frontend/src/theme/tokens.css` 的 `--md-*` 变量，管理台**固定一套配色**

### 3.3 Service Worker
`frontend/public/sw.js`：`/admin` 路径**不走缓存**（管理台必须实时）。

---

## 4. 与原计划的偏差（核查后确认）

| 计划原文 | 核查结果 | 处理 |
|---|---|---|
| 「`router.py`（860 行）」 | 实际 **851 行**（含行尾无换行的末行） | 无实质影响 |
| 「`page.py` 2222 行」 | ✅ 确认 **2222 行** | — |
| 「流式 TTS」 | ✅ 确认不存在；流式的是 Deepgram **识别** | UI 标为「流式识别」 |
| 「4 个节奏参数存了没生效」 | ✅ 确认：`maintenance.py` 读的是 `SE_*` 环境变量，`lexicon` 的 `life_tick_s`/`posts_tick_s`/`posts_max_events`/`posts_since_hours` 未被运行时读取 | 本次接线，改为保存即生效 |
| 「重启后生效的标注是错的」 | ✅ 确认 `RESTART_REQUIRED = ("life_tick_s","posts_tick_s")` 与 `page.py` 标注「（重启生效）」一致，但因根本没生效，标注确实误导 | 接线后去掉该限制 |
| 「`_sync_friend_rows` 不同步」 | ⚠️ **部分不准确**：该函数已存在（`persona_studio.py:261`）并同步了 name/avatar/voice/nationality/city/timezone/native_lang/age，**缺** occupation/bio/personality/speech_quirks | 补全缺失字段，而非重写 |
| 「提示词有字符预算 5600/1100」 | ✅ 确认 `MAX_TEXT_CHARS=5600`、`MAX_CALL_CHARS=1100` | 界面显示裁剪结果 |

---

## 5. 分期与验收

| 期 | 内容 | 验收 |
|---|---|---|
| 1 | 地基：路由守卫、外壳、CSRF、操作日志、总览、后台任务；旧页移到 `/admin/legacy` | pytest + build + 手动 E2E |
| 2 | 平移旧功能：词库类、角色工坊、音色池、图片库、API 与密钥、版本发布 | 同上 |
| 3 | 新能力：用户管理、好友指派、图片上传、DNA/换音色、待发队列、事件公共池、Prompt 控制台、JPush | 同上 |
| 4 | 收尾：删 `page.py` 与 `/admin/legacy`，更新 `docs/ADMIN_CONSOLE.md` | 全量回归 |

**已确认的两项默认做法：**
1. 旧管理台**保留在 `/admin/legacy` 直到第 4 期结束**再删除（可随时回退对照）
2. 每期收尾**真实起服务跑端到端**主流程，不只跑自动化测试

---

## 6. 第 1 期交付记录（已完成）

### 6.1 交付内容

| 层 | 内容 |
|---|---|
| 后端·鉴权 | `auth.py`：`require_admin` / `require_admin_identity` / `csrf_token` / `check_csrf` / `same_origin`；`audit.py`：`write_audit` / `list_audit`（摘要只记字段名，不记值）；`schema.py` 加 `admin_audit` 表 |
| 后端·拆分 | `admin/router.py` **851 → 160 行**（只剩 `page_router` + 聚合器）；新增 `admin/api/` **15 个模块**；**旧路径 0 丢失、OpenAPI 签名 0 变化**（拆分前后 dump+diff 验证） |
| 后端·新接口 | `GET /api/admin/overview`、`GET /api/admin/jobs`、`GET /api/admin/jobs/kinds`、`POST /api/admin/jobs/{kind}/run`、`GET /api/admin/session`、`POST /api/admin/logout` |
| 后端·页面 | `/admin` → React 控制台（受登录保护）；`/admin/legacy` → 旧页面（回退通道）；旧路径 `/admin/{path:path}` 通配守住 `/admin/index.html` 绕过；登录页改 Sonder 品牌 + 旧台入口 |
| 前端 | `src/admin/`：`api.ts`（CSRF + 错误格式化）、`router.ts`（hash 路由 + 导航树）、`queryKeys.ts`、`ui.tsx`（16 个通用组件）、`admin.css`（`--md-*` 变量）、`shell.tsx`、`AdminApp.tsx`、`sections/{Overview,Jobs,ComingSoon}` |
| 前端·布局 | `(app)` / `(admin)` 双根布局；`/admin` 导出为独立页（无开屏、无主题脚本、`noindex`）；`sw.js` 排除 `/admin` |

### 6.2 关键验证数据

- **pytest：1069 passed**（基线 984 → P1 结束 1069，新增 85 条；零旧用例失败）
- **端到端：27/27 通过**（`scripts/e2e_admin_p1.py`，对真实 uvicorn）
- **前端：typecheck 通过、build 通过、`out/admin/index.html` 存在且不含开屏**
- **安全验证**：未登录访问 `/admin`、`/admin/`、`/admin/index.html`、`/admin/deep` **四种变体全部落到登录页**，无泄漏；错误 CSRF 403；跨站写 403；旧台与 token 通道仍可用

### 6.3 过程中发现并修正的偏差

| 发现 | 处理 |
|---|---|
| 旧 `_guard` 不做 CSRF，新台写操作因此**无保护** | 引入**灰名单策略**（见 §1.2），新台受保护、旧台不坏 |
| `/admin/{path:path}` 若在 `/admin/legacy` 之前注册会吞掉它 | 调整注册顺序，并加测试 `test_legacy_not_swallowed_by_catchall` |
| 我的 `admin_page` 一度丢失"未配口令时回环直通"分支 | 抽出 `_denied()` 统一三态，回归测试覆盖 |
| 两条旧测试断言 `/admin` 返回旧页面 | 按新语义修正为"`/admin` 是控制台、`/admin/legacy` 是旧台"，不放宽断言 |
| 本机有系统代理，`httpx` 打回环会 502 | 所有 E2E 脚本用 `trust_env=False` + `NO_PROXY` |

### 6.4 未完成 / 留给后续期

- `overview.today.posts_pending` 目前固定 0 —— `post_queue` 表第 3 期才建，届时换一行 SQL
- `overview` **不做联网探测**（30 秒刷新会拖垮首屏），连通性仍走 `POST /config/{target}/test`
- 用户 / AI 世界 / 捏人三大域的分区仍是「即将上线」占位，按 P2/P3 填充

---

## 7. 第 2 期交付记录（已完成）

### 7.1 交付内容：把旧管理的 9 个页面平移成 React 分区

| 分区 | 路由 | 主要接口 |
|---|---|---|
| 表达 DNA | `#/personas/dna` | `GET/PUT /lexicons/dna`、`/dna/validate`、`/dna/reset`、`GET /usage` |
| 场景与口头禅 | `#/personas/scenes` | `GET/PUT /lexicons/{scenes,phrases}` |
| 世界节奏 + 事件类型 | `#/world/rhythm` | `GET/PUT /lexicons/{rhythm,events}`、`GET /simulate` |
| 捏人素材池 | `#/personas/pool` | `GET/PUT /lexicons/world`、`GET /preview-personas` |
| 角色工坊 | `#/personas/studio` | `GET/POST/PUT/DELETE /personas[/{id}]`、`/{id}/reroll`、`POST /personas/{action}` |
| 音色池 | `#/personas/voices` | `GET /voice-pool`、`/models`、`/validate`、`/add`、`/remove`、`POST /preview` |
| 图片素材 | `#/world/media` | `GET /media/{stats,library,search}`、`POST /media/{fetch,fill,prune}` |
| API 与密钥 | `#/system/config` | `GET /config`、`PUT /config/{target}`、`POST /config/{target}/test` |
| 版本发布 | `#/system/release` | `GET /app-release`、`POST /app-release/{upload,rollback}` |

**后端唯一的代码修复**（其余都是前端平移）：世界节奏 4 个参数从来没生效过。

### 7.2 关键修复：4 个节奏参数从"改了没用"变成"保存即生效"

**问题**：`life_tick_s` / `posts_tick_s` / `posts_max_events` / `posts_since_hours`
在后台能改、能落盘，但 `maintenance.py` 运行时直接读 `os.getenv`，
**从不看词库**。旧页面标的"重启后生效"也是错的——重启同样不生效。

**修复**：
1. `lexicon.py` 新增 `rhythm_params()`，作为**唯一取值入口**，
   优先级 **环境变量 > 词库文件 > 代码默认**（环境变量优先是为了保住运维的紧急刹车）。
2. `maintenance_loop` 每轮重新读取，所以保存后**下一轮即生效**，不需要重启。
3. `RESTART_REQUIRED` 从 `("life_tick_s","posts_tick_s")` 改为 **`()`**。
4. `service.py` 的调试快照 `_rhythm_snapshot()` 也走同一入口，
   消除"界面显示一套、主循环按另一套跑"的不可见不一致。

> 回归保护：`backend/tests/test_lexicon_rhythm_wiring.py`（**13 条**），
> 其中一条用**静态检查**断言 `maintenance.py` 里不再残留 `SE_LIFE_TICK_S` 之类的
> 直接读取——因为这个 bug 的形态是"绕过词库读环境变量"，纯行为测试很难覆盖。
> 另一条断言 `RHYTHM_DEFAULTS` 与各模块代码常量相等，防止两处漂移。

### 7.3 验证数据

- **pytest：1081 passed**（P1 的 1069 + 12 条节奏接线；实际新增 13，1 条替换）
- **API 契约：28/28 通过**（`scripts/e2e_admin_p2_api.py`，真 Cookie 会话打真实 uvicorn）
- **构建产物接线**：9 个分区的独有文案全部能在 `_next/**/*.js` 里找到
  ——证明 dynamic import 没写错、代码真的进了 bundle
- **浏览器真实渲染**：登录后依次访问 9 个分区，**page-error = 0**，
  控制台只有 6 条无害提示（Chromium 关于密码框不在 `<form>` 内的无障碍建议）。
  总览页与版本发布页截图确认：侧栏四域、P2 项可用、P3 项仍灰、数据是真实值。
- **前端**：typecheck EXIT=0、`npm run build:backend` 通过（120 文件）

### 7.4 过程中发现并修正的偏差

| 发现 | 处理 |
|---|---|
| 旧页面把 `events` 的"展示内容"存成 **`show_title`**、中文名存成 **`zh`**（不是 `label`/`show_content`） | UI 兼容两种写法，写入以 `show_title` 为准 |
| `POST /voice-pool/preview` 返回 mp3 但只支持 POST，而 `requestBlob` 只做 GET | 在 `api.ts` 新增 **`requestBlobPost`**（CSRF 头 + 403 重试），**消除**队友在两处各写一份的重复实现 |
| `adminKeys` 缺 usage 的 key，两个分区用字面量 | 加 `adminKeys.usage()`，统一失效口径 |
| 导航曾用 `item.phase <= 1` 判断可用 | 改为 `isImplemented(route)`，以**实际有页面**为准，避免"期数到了但页面没做"把用户带进空壳 |
| 移除了 `.ps1`/Python 浏览器脚本 | 本机 `agent-browser` 的 `open` 会挂死（profile 锁 + daemon 卡住）；改用 REA 的浏览器通道 + 可重复的 API 契约脚本，不把脆弱脚本留在仓库 |

### 7.5 已知的、有意保留的取舍

- **角色列表没有分页**：后端 `/api/admin/personas` 只接受 `query`/`limit`，
  不返回 `total`/`offset`。要分页得先改后端。
- **事件类型的"价值档/来源/分享权重"编辑后立即生效**，不需要点保存——
  这是按旧页面一致的交互，不是遗漏。
- **`ScenesSection` 的语料覆盖表**与 **`RhythmSection` 的事件卡**是旧页面没有的增强；
  保留是因为它们直接支撑"这个池子够不够用"的判断。

### 7.6 未完成 / 留给后续期

- 用户管理、好友指派、事件公共池、动态待发队列、Prompt 控制台、JPush 配置
  → **第 3 期**（导航里显示 `P3` 且置灰）
- 操作日志页面 → 后端 `audit.py` 已就绪，只差前端分区（导航显示 `P1`）
- `page.py` 与 `/admin/legacy` 的删除 → **第 4 期**

---

## 8. 第 3 期交付记录（已完成）

### 8.1 新增 5 张表（`schema.py`）

`event_pool` / `event_pool_claims` / `post_queue` / `prompt_overrides` / `prompt_templates`。

**待发队列坚持用独立表**，不给 `persona_posts` 加状态列：该表在代码里约 157 处引用，
加状态列要逐处补过滤，漏一处就把未发布内容露给真实用户。这个判断在本期被验证是对的
——`post_queue` 只在管理端与发布路径两处出现。

### 8.2 后端能力（18 条新路由）

| 模块 | 能力 |
|---|---|
| `users.py` | 列表（device_key 只回尾 6 位）/ 详情（**不含聊天记录**）/ 候选角色（match_hint）/ 好友指派（request\|direct，幂等）/ 解除好友 / 测试推送 |
| `events.py` | 角色事件列表 + 手动补事件；事件池 CRUD；**preview**（4 维评分 + 每维 contribution，只算不落库）；**claim**（自动按分挑 / 显式指定，幂等，回填 event_id，领满置 filled） |
| `posts.py` | pending（仅 queued）/ published / personas；排队与立即发布；部分更新；软取消；publish-now（幂等） |
| `media.py` | `POST /media/upload`：Pillow + mime 双校验、大小上限复用 `limits()`、**服务端重命名防路径穿越** |

**复用而非另造**（这是本期最重要的实现纪律）：
- 发布走 `event_posts._insert_post`（唯一 INSERT 入口），没有另写一条
- 好友走 `V3Service.add_friend` / `_ensure_request_friend`，通知复用 `social_events → client_events → JPush`
- 角色事件走 `life_events.add_event`

### 8.3 Prompt 持久化：改写必须真的进 prompt

**这是本期最容易做成假功能的一块。** 原 `compose_prompt` 支持 `module_overrides`
但那是调试用的一次性覆盖，传完就没了。

**做法**：在 `compose_prompt` **内部**读取已保存的改写，而不是让每个调用方各传一遍。
`compose_prompt` 的调用点不止一处（真实聊天 / 通话 / 管理端预览），靠调用方记得传，
迟早漏一个——漏掉的那个就是"改了不生效"。

**可改 vs 只读的边界**（按模块内容来源判定，不是拍脑袋）：

| 来源 | 模块 | 可改写 | 理由 |
|---|---|---|---|
| `global` / `persona` / `first_message` | global, language, character_card, examples, opening | ✅ | 内容不随这一轮对话变化 |
| `plan` | scenario, behavior, author_note | ❌ | 由运行时计划算出，改它会让"角色此刻在做什么"失真 |
| `profile` | user_persona | ❌ | 用户资料 |
| `memory` | relationship_memory | ❌ | 关系记忆 |

`editable` 由**后端**给（`prompt_composer.module_meta`），前端不自己按 key 猜——
否则会出现"界面允许改、实际被 compose_prompt 忽略"。

### 8.4 JPush 可配置化

新增 `app/v3/push_config.py`：优先级 **环境变量 > 管理台保存 > .env**，与节奏参数一致。
`dispatch_push` 改为走 `push_config.effective()`，以前它内联读 env/settings，管理台改不了。

管理台状态**只回指纹**（`app_key_tail`），且界面明示"若被环境变量覆盖，后台设置不生效"
——不说清楚会变成"改了没用"的玄学问题。

### 8.5 验证数据

- **pytest：1127 passed**（P2 的 1099 → +28 P3 后端用例；另含 prompt 持久化 17 条）
- **P3 端到端：56/56 通过**（`scripts/e2e_admin_p3.py`，真 uvicorn + 隔离 DATA_DIR）
  - 关键覆盖：好友重复添加被拒、事件池重复领取被跳过、publish-now 重复不产生第二条、
    已发布不可编辑、软取消幂等、**改写进入真实 full_text**、上传文件名被重写防穿越
- **P1 端到端 27/27 仍绿**（无回归）
- **浏览器真实渲染**：5 个新分区依次访问，**page-error = 0**；
  截图确认侧栏 P3 项全部点亮，只剩「操作日志」仍灰
- **前端**：typecheck EXIT=0、`npm run build:backend` 通过（125 文件）

### 8.6 过程中发现并修正的偏差

| 发现 | 处理 |
|---|---|
| `/prompt/modules` 只回模块元信息、**没有原文**，只读模块在界面上没法显示内容 | 改为复用预览链路取每个模块的**实际文本**（含已保存改写），一并回 `text`/`chars`/`overridden` |
| `/prompt/preview` 未回 `trimmed` 与 `max_chars` | 补上：运营需要知道"我写的这段是不是被长度预算挤掉了" |
| 我在 `router.py` 先加了 include，但三个 stub 还没有 `router` 对象 → **整个 admin 包导入失败** | 给三个 stub 补最小 `APIRouter`；并记下教训：跨模块接线要么等对方就位，要么先放防御 |
| 一条 P1 测试断言"P3 模块不该有 router" | 按新语义改为"未实现的模块不注册路由"（原则不变，清单随期数演进） |
| 我自己用 PowerShell 生成代码时又把转义写坏（`__all__ = [\"router\"]`） | 改用 edit 工具逐处修正。**这已是本会话第 4 次同类错误**，结论固定为：中文/引号内容一律不经过 PowerShell |
| e2e 里我的 JPush 脱敏断言用子串匹配，被 `has_master_secret`（键名）误伤 | 改为检查**是否承载明文值**，而不是键名是否含关键字 |
| e2e 里手写的 base64 PNG 被上传端点判为坏图 | 用 Pillow 生成真图。**上传端点的校验是对的，错的是我的测试数据** |
| e2e 用户用例因库为空而失败 | 增加 `seed_user()` 播种 fixture；并把"环境不具备"与"代码坏了"区分开（SKIP vs FAIL） |

### 8.7 已知的、有意保留的取舍

- **HTTP 语义**：参数非法 → 400；业务失败（含"对象不存在"）→ 200 + `{ok:false,issues}`。
  与 §1.3 写的 422/404 不一致，**以前端与测试都已按 400/200 落地为准**；
  §1.3 的 422 是 P1 对"新增接口"的设想，实际实现选择了"业务错误不抛 HTTP 错误"。
- **管理台手发的动态会占当日事件帖配额**：因为复用了 `_insert_post`，它带
  `post_type='life_event'` 并计入 `_today_event_post_count()`。想让管理台动态不占配额，
  需要给 `_insert_post` 加参数——留到后续。
- **角色列表不分页**（后端只接受 query/limit，无 total/offset）。

### 8.8 未完成 / 留给第 4 期

1. **操作日志分区**：`api/audit.py` 仍是空壳（0 路由）。而且更关键的是——
   **审计目前还没有真正落库**：写端点都设了 `request.state.audit_*`，
   但聚合路由挂的是过渡期的 `guard`，不是会写审计的 `require_admin`。
   P4 换掉 `guard` 后，`admin_audit` 才会开始有数据，操作日志页才有意义。
2. **删除 `page.py` 与 `/admin/legacy`**，并把 `guard` 整体换成 `require_admin`（全强制 CSRF）。
3. 更新 `docs/ADMIN_CONSOLE.md`。

---

## 9. 第 4 期交付记录（收尾，已完成）

### 9.1 鉴权收口：灰名单 → 全强制

P1~P3 期间 `deps.guard(request)` 由每个 handler 显式调用，CSRF 是**灰名单**
（带 `X-Admin-CSRF` 才校验）——那是为了"新台受保护 + 旧台还能用"的临时折中。

P4 删除旧台后收口为路由级依赖：

```python
router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(admin_auth.require_admin)],
)
```

并**删除了 79 处 handler 内的 `deps.guard` 调用**（12 个文件）与 `deps.guard` 本体。
两套判定并存是 P4 之前吃过的亏：灰名单与全强制两套规则会漂移。

### 9.2 一个由测试抓出来的真实回归：`/session` 被误伤

把 `require_admin` 挂到父路由后，`GET /api/admin/session` 也要求登录 → 未登录返回 403。
**但前端正是靠它的 `authenticated: false` 决定跳登录页**，403 会让前端只能猜状态码。

修法：`session.public_router`（`/session` + `/logout`）+ 顶层 `api_public_router`，
由 `app/main.py` **单独 include**。

注意 `include_router` 会把父路由的 `dependencies` 带给子路由，
所以这两个接口**不能**挂进主 `router`，只能走平行路由。
`/logout` 同理：会话过期时"退出"按钮仍要能用（幂等）。

### 9.3 审计真正开始落库（并暴露一个潜伏 bug）

`_audit_after` 里有一行 `log.debug(...)`，而 `auth.py` **从来没有定义过 `log`**。
P1~P3 期间 `_audit_after` 从未被调用（`guard` 不写审计），所以这个 `NameError` 一直潜伏；
P4 让审计真正跑起来，它立刻炸了 11 个测试。

同时给 `_audit_after` 加了整体 `try`：**审计失败永远不能把成功的写操作变成 500**
（否则运营会以为没生效然后重试，造成重复写）。

**时间戳精度**：`at` 从秒级改为毫秒级。同一秒内常有多条写（"发布动态" = 入队 + 改状态），
秒级精度下顺序只能靠随机 uuid 打破，排障时看到"同一秒三条、顺序随机"会误导判断。

### 9.4 旧管理台删除

- 删除 `backend/app/admin/page.py`（2222 行）
- 删除 `/admin/legacy` 路由；该路径现由 `/admin/{path:path}` 接住并返回新控制台（仍要求登录）
- 登录页移除「打开旧版词库管理台」链接与对应 CSS
- 侧栏底部移除「旧版管理台」入口（改成品牌标识）——留着死链会让人以为还有回退通道
- `ComingSoonSection` 从「即将上线（第 N 期）」改成「这个地址没有对应页面」：
  所有分区都交付了，再显示"即将上线"会让人去等一个永远不会来的东西

### 9.5 操作日志分区

`GET /api/admin/audit`（分页 / 按动作精确 / 按对象模糊）+ `GET /api/admin/audit/actions`
（从已有日志聚合动作清单，**不维护硬编码枚举**——枚举一定会漏，漏掉的就筛不到）。

前端 `AuditSection.tsx`：时间 / 动作 / 对象 / 摘要 / 来源，页面顶部明示
"只记成功的写操作，失败的不进这张表——这里回答的是'哪些改动真的生效了'，不是'谁尝试过'"。

### 9.6 测试按新语义更新（不放宽断言）

| 测试 | 原断言 | 更新为 |
|---|---|---|
| `test_admin_csrf_greylist.py`（删除） | 不带 CSRF → 放行 | 换成 `test_admin_csrf.py`：**不带 CSRF → 403**，并补 `/session` 未登录可访问、`/logout` 幂等、令牌与服务端同源 |
| `test_admin_page_guard.py` legacy ×3 | `/admin/legacy` 返回旧页面 | 改为「仍要求登录」「旧页面内容已消失」「`page.py` 文件已删除」 |
| `test_admin_auth.py`、`test_lexicon.py` | `/admin/legacy` 含「词库管理」 | 改为断言**不含** |
| `test_admin_api_split.py` ×3 | session 在受守卫 router；audit 无路由 | 改为 public_router 承载、受守卫 router 不含它们；audit 已实现 |
| 5 处测试 app fixture | 只挂 `router` | 补挂 `api_public_router`（与 `main.py` 真实装配一致） |

### 9.7 端到端脚本同步更新

- `e2e_admin_p1.py`：CSRF 期望由"放行"改为"403"；legacy 断言改为"不再是旧页面"
- `e2e_admin_p3.py`：**所有写操作开始需要 CSRF 头**（这是 P4 生效的直接证据），
  登录后统一设 `c.headers["X-Admin-CSRF"]`；顺带修掉一个变量遮蔽 bug
  （局部 `skipped` 与模块级计数表重名，导致汇总行 `UnboundLocalError`）
- 新增 `e2e_admin_p4.py`（23 项）：CSRF 全强制四态、token 豁免、审计条数增长、
  摘要不含正文、`page.py` 已删、未登录 `/session` 仍 200

### 9.8 验证数据

- **pytest：1129 passed**（P3 的 1127 → 净 +2：新增 csrf 12 条，替换 greylist 9 条并调整若干）
- **P1 e2e 27/27、P3 e2e 57/57、P4 e2e 23/23**（真 uvicorn + 隔离 DATA_DIR + 全新数据库）
- **浏览器真实渲染**：操作日志页有 14+ 条真实记录；未知 hash 走兜底页；侧栏 17 项全部点亮；**page-error = 0**
- **前端**：typecheck EXIT=0、vitest 126 passed、`npm run build:backend` 通过（126 文件）

### 9.9 收尾后仍保留的取舍

见 `docs/ADMIN_CONSOLE.md` §10：管理台手发动态占当日事件帖配额、角色列表不分页、
HTTP 语义按 400/200（而非契约草稿的 422/404）。
