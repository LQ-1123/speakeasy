# Sonder 运营控制台（管理端）· 使用与实现说明

> 旧版是「本机词库编辑器」时代的产物（`page.py` 一个 2222 行内联 HTML + 原生 JS，11 个平铺标签）。
> 现在服务已公网部署、有真实用户，管理端改为**按域组织**的 React 控制台：
> **用户 / AI 世界 / 捏人 / 系统**。
>
> 第 4 期已删除 `backend/app/admin/page.py` 与 `/admin/legacy`。
> 跨模块的锁死契约（表结构、CSRF、响应格式、四期交付记录）在
> [`ADMIN_CONSOLE_CONTRACT.md`](./ADMIN_CONSOLE_CONTRACT.md)。

---

## 1. 打开与登录

- 地址：`/admin`（hash 路由，如 `#/users`、`#/world/events`、`#/personas/prompts`）
- 登录：`SE_ADMIN_PASSWORD` 口令 → `POST /admin/login` → HttpOnly 会话 Cookie（30 天）
- 退出：右上角「退出」→ `POST /api/admin/logout`（幂等，会话过期也能调）
- 远程访问：`X-Admin-Token`（值 `SE_ADMIN_TOKEN`）仍可用，供脚本 / CLI；
  **未配口令且来自回环地址时直通**（本机开发不设密码也能进）
- 同 IP 连续输错 5 次锁 5 分钟
- 页面产物：`backend/app/static/app/admin/index.html`，由 `npm run build:backend` 生成

### 安全模型（第 4 期收口）

| 层 | 规则 |
|---|---|
| 鉴权 | Cookie 会话 → `X-Admin-Token` → 未配口令时回环直通 |
| CSRF | **Cookie 身份的写请求一律要求**同源 + `X-Admin-CSRF`（P1~P3 曾是「带就校验」的灰名单，已收口） |
| 豁免 | `X-Admin-Token` 与回环直通不发 CSRF（自定义头不构成 CSRF 面，跨站页面无法伪造） |
| 审计 | 写操作**成功返回后**自动落一条 `admin_audit`；失败的写不记 |

> `/api/admin/session` 与 `/api/admin/logout` 是**故意不鉴权**的：
> 前端靠 `session` 返回的 `authenticated: false` 决定跳登录页，靠 `logout` 在会话过期时仍能退出。
> 它们挂在独立的 `api_public_router` 上——如果挂进带 `require_admin` 的主路由，未登录会被 403。

---

## 2. 四个域

| 域 | 页面 | 干什么 |
|---|---|---|
| 用户 | 用户列表 / 用户详情 | 搜用户、看设备与推送状态、指派角色好友、发测试推送 |
| AI 世界 | 事件 / 动态 / 图片素材 / 世界节奏 | 手动补事件、公共事件池（预演谁会领）、动态待发队列、图库、产出参数 |
| 捏人 | 角色工坊 / 表达 DNA / 捏人素材池 / 音色池 / Prompt 控制台 / 场景与口头禅 | 创建与编辑角色、调行为与表达、按角色改写 prompt |
| 系统 | API 与密钥 / 版本发布 / 后台任务 / 操作日志 | LLM·STT·TTS·Deepgram·JPush 配置、APK 发布、任务状态、变更留痕 |

> 管理台**不提供聊天记录查看**。用户详情只展示账号、设备、好友、计数元数据。
> 这是产品决定：运营需要判断"这个人是谁、处在什么状态"，不需要读用户的私聊。

---

## 3. 词库文件（仍然是唯一真相）

```
data/lexicons/
├── dna.json        # DNA 字段 × 枚举值 × 数值映射（0..1）+ 默认值
├── scenes.json     # 示例对话场景
├── phrases.json    # 全局禁用词（红线）+ 分语言口头禅池
└── world.json      # 捏人素材池：名字 / 职业 / 城市 / 兴趣
```

初始化（把代码里现有词表导出成 JSON，可重复执行）：

```powershell
python scripts\export_lexicons.py            # 只补缺失文件
python scripts\export_lexicons.py --force    # 覆盖（会先备份为 *.bak.json）
```

文件不存在时**代码默认值继续生效**——删掉整个目录也不会坏。

### 三条硬规则（管理台会强制）

1. **枚举值只能停用、不能删除**。老角色的 `speech_dna` 可能还在用这个值，删了它就成非法值。
2. **新增枚举值必须带 `trait`（0..1）**。行为引擎只读数值，没有数值的枚举无法参与决策，保存会被拒。
3. **保存前必须过校验，写盘原子 + 保留上一版**。校验不过直接返回问题列表、不落盘；成功会先备份 `*.bak.json`。

其他校验：默认值必须在取值列表且未停用；场景不能重复且至少 8 个；禁用词必须非空；口头禅池单语言 ≤80 条。

---

## 4. 两处「改了就要立刻生效」的设计

### 4.1 Prompt 控制台：改写真的会进入对话

最容易做成假功能的一块：界面能改能存，真实对话用的还是原文。

做法是**把「读取已保存改写」放进 `compose_prompt` 内部**，而不是让每个调用方各传一遍。
`compose_prompt` 的调用点不止一处（真实聊天 / 通话 / 管理端预览），靠调用方记得传，迟早漏一个。

**可改 vs 只读按内容来源判定**（不是拍脑袋）：

| 来源 | 模块 | 可改写 | 理由 |
|---|---|---|---|
| `global` / `persona` / `first_message` | global, language, character_card, examples, opening | ✅ | 内容不随这一轮对话变化 |
| `plan` | scenario, behavior, author_note | ❌ | 按运行时计划算出，改它会让"角色此刻在做什么"失真 |
| `profile` | user_persona | ❌ | 用户资料 |
| `memory` | relationship_memory | ❌ | 关系记忆 |

`editable` 由后端 `prompt_composer.module_meta()` 给出，前端不自己按 key 猜——
否则会出现"界面允许改、实际被忽略"。**即使直接往表里塞动态模块的改写也不会生效**（加载时按 `editable` 过滤）。

保存后**立即生效，不需要重启**。改写可存为模板复用（模板是复制源，不会自动应用）。

### 4.2 世界节奏：参数读词库，不用重启

10 个节奏参数（事件/动态配额、张力惩罚、tick 间隔等）按 **环境变量 > 词库文件 > 默认值** 解析，
维护循环每轮重新读取，改完保存即生效。`stats_persona` 的 `background` 块也走同一份快照，避免两处漂移。

---

## 5. 第 3 期新增的三块能力

| 能力 | 要点 |
|---|---|
| **好友指派** | 两种模式：`request`（推通知，等用户同意）/ `direct`（跳过同意，立刻出现在好友列表）。界面上分别说明区别。**幂等**：已是好友返回 `{ok:false, issues:["已经是好友"]}`，不插重复行 |
| **公共事件池** | 建池 → **预演**（4 维评分：职业/兴趣/城市/表达 DNA，每维给 `contribution`，各维之和 = 总分，只算不落库）→ 领取（写 `event_pool_claims`，`UNIQUE(pool_id,persona_id)` 保幂等，回填 `event_id`，领满置 `filled`） |
| **动态待发队列** | 独立表 `post_queue`，**不给 `persona_posts` 加状态列**——那张表在代码里约 157 处引用，加状态列要逐处补过滤，漏一处就把未发布内容露给真实用户。取消是**软取消**（`status='cancelled'`，保留记录）；已发布不可编辑 |

---

## 6. 操作日志

页面：`#/system/audit`。用途只有一个——出问题时回答「最近谁改了什么」。

- 只记**成功的写操作**（由 `require_admin` 的 yield 依赖保证：handler 抛错就不写）。
  失败的写不进表，否则误操作会把真正生效的改动淹掉。
- **摘要只记字段名、不记值**（`audit.summarize`）。值里可能有密钥或用户内容，进日志等于二次泄露。
- 时间戳带毫秒：同一秒常有多条写（比如"发布动态"= 入队 + 改状态），秒级精度下顺序只能靠随机 uuid 打破，排障时会误导。
- 审计写入失败**永不影响业务结果**（旁路，不是关键路径）。

---

## 7. API 一览

完整清单以 `/openapi.json` 为准（筛选 `/api/admin`）。分组：

| 前缀 | 内容 |
|---|---|
| `/api/admin/session`、`/logout` | 会话与退出（**不鉴权**） |
| `/api/admin/overview` | 总览（服务健康、今日产出、待发数） |
| `/api/admin/jobs` | 后台任务状态与立即执行 |
| `/api/admin/lexicons/*` | 词库读/存/校验/回滚/用量/模拟/预览捏人 |
| `/api/admin/personas/*` | 角色 CRUD、批量工具、音色、Prompt 模块与改写/模板 |
| `/api/admin/users/*` | 用户列表/详情/候选角色/好友指派/解除/测试推送 |
| `/api/admin/events`、`/event-pool/*` | 角色事件、公共池 CRUD/预演/领取 |
| `/api/admin/posts/*` | 待发队列、已发、写一条、编辑、软取消、立即发布 |
| `/api/admin/media/*` | 图库、搜图入库、上传、按兴趣补图 |
| `/api/admin/config/*` | LLM / STT / TTS / Deepgram / JPush 保存与连通测试 |
| `/api/admin/release/*` | APK 上传与发布 |
| `/api/admin/audit`、`/audit/actions` | 操作日志与动作清单 |

**响应约定**：业务失败一律 HTTP 200 + `{ok:false, issues:[...]}`；
只有参数本身非法才 400。前端以 `ok` 判断，不以状态码猜。

### JPush 凭据优先级

**环境变量 > 管理台保存（`data/jpush_config.json`） > `.env`**。
管理台状态只回指纹（`app_key_tail`）；若被环境变量覆盖，界面会明示"后台设置不生效"——
不说清楚就会变成"改了没用"的玄学问题。

---

## 8. 验收

```powershell
# 后端（本机需排除一条依赖外网、会挂死的用例）
.\.venv\Scripts\python.exe -m pytest backend/tests -q -p no:cacheprovider `
  --deselect "backend/tests/test_v3_world.py::test_profile_language_levels_and_feed_filter"

# 前端
cd frontend && npx tsc --noEmit && npx vitest run && npm run build:backend

# 端到端（需要先起真服务，DATA_DIR 指向临时目录——data/ 是只读的）
python scripts\e2e_admin_p1.py   # 路由/静态产物/登录/CSRF/审计落库
python scripts\e2e_admin_p3.py   # 用户/好友/事件池/动态队列/Prompt/JPush/上传
python scripts\e2e_admin_p4.py   # 鉴权收口 + 审计真的在落库 + 旧台已删
```

起服务的最小配方：

```powershell
cd backend
$env:DATA_DIR="D:\path\to\.tmp\e2e"; $env:SE_ADMIN_PASSWORD="test-pw-123456"
$env:SE_ADMIN_TOKEN="test-token-abc"; $env:NO_PROXY="127.0.0.1,localhost"
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8899
```

> `data/`（真实数据目录）对测试是只读的，端到端必须用临时 `DATA_DIR`，否则写库报
> `attempt to write a readonly database`。
> `scripts/check_admin.py`、`scripts/check_admin_events.py` 是更早的取证脚本，
> 断言的是旧管理台页面内容，**已被上面的 e2e 取代**。

---

## 9. 客户端解耦（保持有效）

| 项 | 状态 |
|---|---|
| 管理端「API 与密钥」 | ✅ LLM / STT / TTS / Deepgram / **JPush** 集中配置，密钥只显示指纹 |
| 保存策略 | ✅ 先验证再落盘（LLM 真发一次极小请求；TTS 探测 Fish 音色列表；STT 走官方 model 列表；JPush 打一次只读鉴权接口——**不发真推送**，避免给真实用户弹通知），失败默认不保存，可强制保存 |
| 客户端配置入口 | ✅ 已移除；`/api/v2/settings/*` 写操作返回 403 并指向 `/admin`（开发调试设 `ALLOW_KEY_EDIT=1`） |
| 音色池 | `/admin#/personas/voices`：Fish 音色入库、试听、已分配人数；新捏人物优先从池子挑（一人一色） |
| 角色工坊 | `/admin#/personas/studio`：全量重捏（保留 ID/好友/聊天）、平衡男女比例、重配音色、重建（先备份 DB）、单角色全属性编辑、新建测试角色、删除前自动备份 |
| APK 发布 | `/admin#/system/release`：上传 → 填 versionCode/versionName → 发布；旧版本进历史可回滚；App 启动检查 `/api/app/latest` |
| 图片库 | `/admin#/world/media`：搜图（Wikimedia/Openverse）、上传、按角色兴趣补图；生命周期：单文件 ≤12MB、总量 200MB/800 张、未引用 45 天过期、被引用永不删除 |
| 公网部署 | `https://47-104-24-236.sslip.io`；`ALLOW_KEY_EDIT=0`；Fish 经 mihomo 出网；edge-tts 兜底 |

---

## 10. 已知取舍（有意保留）

1. **管理台手发的动态会占当日事件帖配额**——因为发布复用了 `event_posts._insert_post`
   （唯一 INSERT 入口，这是有意的），它带 `post_type='life_event'` 并计入 `_today_event_post_count()`。
   要让管理台动态不占配额，需要给该函数加参数，留待后续。
2. **角色列表不分页**（后端只接受 `query` / `limit`，无 `total` / `offset`）。
3. **HTTP 语义**：参数非法 400；业务失败（含"对象不存在"）→ 200 + `ok:false`。
   与契约早期草稿写的 422/404 不一致，**以实现与测试为准**。

---

## 11. 用户身份模型：「注册行数」不等于「人数」

这一条值得单独写，因为线上真的踩过：管理台一度显示「用户总数 91」，而实际上
只有 1 个人在用。原因不是多用户功能，而是**身份模型本身的特性**。

### 机制

客户端首次启动会生成随机 `device_key` 换一个 `user_id`（`frontend/src/w4/v3/api.ts`），
并存在 localStorage 的 `speakeasy.device_key.v1` 里——同一 key 永远是同一个 user。
但反过来，**只要 localStorage 是空的就会造一个新 user**：

- 无痕 / 隐私窗口
- 新的浏览器 profile、换 WebView 容器
- 重装 App、清应用数据
- 自动化脚本每次用全新 profile 跑一遍（这一条贡献了线上绝大部分垃圾数据）

所以「用户只有我一个」和「库里有 91 行」并不矛盾：缺的是**账号绑定**，
不是多用户开关。要真正做到"一个人一个账号"，需要登录/绑定体系；在那之前，
行数天然大于人数。

### 管理台怎么处理

口径定义在 `backend/app/admin/user_scope.py`（**唯一真相**，总览与用户列表共用）：

| 口径 | 含义 |
|---|---|
| `total` | `v3_users` 原始行数 |
| `real` | 有使用痕迹：注册设备 / 好友 / 动态 / 消息 / moment，任一即是 |
| `empty` | `total - real`，注册后毫无痕迹的残留 |
| `registered` | 注册过推送设备的数量——最接近"真的装了 App 并授权通知的人" |

- 总览首屏主数字是 **`real`**，原始行数降级为副标题（"共 N 条注册记录，M 条无使用痕迹"）
- 用户列表默认 `scope=real`，可切「仅注册未使用 / 全部」，每行带「有/仅注册」标记
- **`real` 必须与 `v3_users` 求交集**：痕迹表里残留着指向已不存在用户的 id
  （最典型是 `messages` → `friends.owner_user_id = 'local'`，V2 单用户时代的哨兵值）。
  不取交集会出现「有使用痕迹 18 个、合计 17 条」这种自相矛盾的数字。

### 清理脚本

`scripts/cleanup_empty_users.py`。三档，**默认全部只演练，必须 `--apply` 才删**：

```bash
# 一档：只删完全没有痕迹的空壳（最安全）
python scripts/cleanup_empty_users.py --data-dir data

# 二档：连自动化测试身份一起删（按 device_key 前缀识别）
python scripts/cleanup_empty_users.py --data-dir data --test-identities

# 三档：只保留注册过设备的账号 + --keep 指定的，其余连同聊天记录/好友一起删
python scripts/cleanup_empty_users.py --data-dir data --all --keep u_xxx --apply
```

安全设计（每一条都对应一次真实踩坑）：

1. **先备份再删**，备份失败就中止；备份目录里有 manifest 记录删了哪些 id 和每张表行数
2. **`--keep` 的 id 必须真实存在**——否则手误传入截断 id 会让保护名单形同虚设，
   而三档会把人全删光（这是演练时发现的，不是假想）
3. **子表按 `CASCADE` 级联删除**（消息→好友→好友关系→动态→推送事件→画像→用户），
   避免留下指向已删用户的孤儿行
4. **`persona_example_dialogs` 显式排除**：它的 `user_line` 是"示例对话里用户那句话"
   的内容，不是用户外键。按列名模糊匹配会清空全部示例语料
5. 单事务：任一张表失败整体回滚
6. 不 `VACUUM`（线上服务在跑，且清理几千行回收不了多少空间）

### 已知的历史残留

数据库里还有两类**清理前就已存在**的孤儿，与本次设计无关，可择机单独处理：

- `friends.owner_user_id = 'local'`（V2 单用户时代的哨兵值）
- `v3_discover_views` 中指向已不存在用户的浏览记录

