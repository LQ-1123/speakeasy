# Sonder 4.2 · AI Persona Prompt System & First-time Onboarding

> 本文件记录 2026-11 这次迭代的**实现方式**、**关键设计决策**与**本机验证证据**。
> 验收顺序：本机 Web → 服务器部署 → 远端 Web → Android 打包。

## 1. 这次改了什么

### 1.1 统一 Prompt Composer（`backend/app/v3/prompt_composer.py`，新增）

原先 `chat_service._system_prompt()` 在一个函数里散着拼 Prompt，并且写死了一句
`You are talking to a Chinese person who reads English fine but is SCARED to speak it.`。
现在所有提示词来源集中到 **Prompt Composer**，按 A–H 八层模块化组装：

| 层 | 模块 key | 内容 | 来源 |
|---|---|---|---|
| A | `global` | 平台基础规则：虚构人物、禁止 OOC/暴露 Prompt、不虚构现实事实、反模板化原则 | 代码常量 |
| A | `language` | 目标语言规则 + 学习者该语言水平 + 纠错偏好 | `user_profiles` |
| B | `character_card` | 结构化角色卡：姓名/年龄/性别/职业/国家城市/Description/Personality/Temperament/Interests/Likes/Communication style/Speech quirks/Boundaries | `personas` + Speech DNA |
| C | `scenario` | 此刻时间/在做什么/情绪 + 既有生活事实 + 身边关系 | Life Events / Emotion / Event Posts |
| D | `opening` | 首次开场风格（仅无历史时注入） | `first_message.py` |
| E | `examples` | 情境化示例对话（只学声线，不抄台词） | `persona_example_dialogs` |
| F | `user_persona` | 真实用户资料：昵称/母语/目标语言+分语言水平/兴趣/城市/职业/纠错偏好；缺失则明确「不知道、不要编」 | `user_profiles` |
| G | `relationship_memory` | 熟稔度阶段 + UserFactSheet/SharedEpisodes/OpenLoops 记忆（去重后） | `memory.build_context()` |
| H | `behavior` + `author_note` | 本轮行为倾向 + **最后一块**动态指令（长度/是否提问/是否分享/是否允许分歧/是否可结束） | Behavior Engine |

- **版本号**：`PROMPT_VERSION = "sonder-prompt-3.2.0"`，`PROMPT_VERSIONS` 保留历史登记；
- **预算**：文字 5600 字符、通话 1100 字符；裁剪顺序 examples → opening → 生活事实 → memory →
  压缩受保护模块，`behavior`/`author_note` 永不整块删除；
- **降级**：每个模块独立 try/except，失败标 `enabled=False` 继续；对外永不抛异常；
- **调试**：管理端（`/api/admin/prompt/*`，仅管理员/本机）可看最终 Prompt、按模块查看、
  用 `module_overrides` 改某模块文本后重新预览（不落库），并可对比 text / image / call 三种模式。

### 1.2 去掉的「过度约束」

以下硬规则已删除或改为「倾向」，避免把人物变成统一的客服口吻：

- `ALWAYS use contractions`、`Start with a small reaction or filler`（开场必须 filler）
- `Always use casual expressions`、每轮 emoji 提示、每轮提问
- `WHO YOU ARE TALKING TO: a Chinese person ... SCARED to speak it`（硬编码用户画像 → 动态资料）
- 通话 Prompt 的 `always use contractions / open with a small filler`

保留的反模板化原则：语言风格是倾向不是动作；不要求每轮提问/情绪回应/口头禅/emoji；
允许很短回复、停顿、转移话题、结束对话、主动分享、表达不同意见；不要每轮总结用户；
不要所有人物都热情友善。

### 1.3 First Message（`backend/app/v3/first_message.py`，新增）

- 按人物气质分 7 桶（outgoing / reserved / witty_sarcastic / warm / dry / playful / poetic）；
- 每桶 en/zh 多套模板，确定性选择（同一人物稳定）；低水平用短句池；
- 三处复用：空聊天页开场（`/api/v3/personas/{id}` 暴露 `first_message`）、Prompt 的 `opening` 模块、
  管理端对比预览。

### 1.4 首次建档 Onboarding 状态（后端）

- `user_profiles` / `user_profile` 幂等新增 `profile_setup_completed_at`；
- **一次性迁移回填**：功能上线前已存在的用户（历史资料行 + 旧 `v3_users` 账号）标记为已完成，
  **不会被强制重新填写**；迁移后新注册用户为空（需要引导）；
- `PUT /api/v3/profile` 新增 `communication_preference`（natural/occasional/improve）与
  `complete_setup`；完成标记与资料写入在**同一次** DB 更新里，缺必填返回 400 且不标完成；
- 新增 `GET /api/v3/profile`（profile + setup），bootstrap 也自然带出完成状态（不依赖 localStorage）。

### 1.5 前端 Onboarding（`frontend/src/w4/v3/Onboarding.tsx`，新增）

- 三步：**Get to know you**（昵称+母语）→ **Your language journey**（多目标语言 + 每语言独立水平）
  → **Things you love**（12 预设 + 自定义兴趣 ≤8 + 纠错偏好三选一）；
- 设计：Material 3 token、深浅色自适应、桌面居中卡片 / 手机全宽 safe-area、320px 无横向溢出；
- 交互：进度条 1/3、返回、Continue/开始认识朋友、保存中 loading、失败停在本步 + 重试、
  400 `profile_incomplete` 按缺失字段跳回并高亮、Enter 提交、reduced-motion 降级；
- 闸门：bootstrap 完成后 `profile.setupCompleted === false` 才展示；undefined/true/错误/预览模式均走原界面。

### 1.6 两项功能的连接

建档资料通过 Composer 的 `user_persona` / `language` 模块真实进入每一轮对话；用户主动填写的资料
优先级高于记忆推测（Prompt 里显式声明 + 昵称同步进 `fact_sheet` 修复占位覆盖）。人物会随交流逐渐
了解用户，但 Prompt 明确禁止每轮采访式追问。

## 2. 本机验证（真实 Chrome + 真实 LLM，不是 Mock）

### 2.1 自动化测试

| 项目 | 命令 | 结果 |
|---|---|---|
| 后端全量 | `pytest backend/tests -q` | **984 passed** |
| 前端类型 | `npx tsc --noEmit` | **0 error** |
| 前端单测 | `npx vitest run` | **18 files / 126 tests passed** |
| 前端构建 | `npm run build:backend` | 成功，静态导出 9.32 MB → `backend/app/static/app` |

基线（改动前）：后端 **950 passed**。新增 34 个后端测试（Prompt Composer 14 / Onboarding 12 /
管理端预览 8 等）。

### 2.2 浏览器端到端（8 个场景全过）

`powershell -File scripts/run_local_web_verify.ps1`（PowerShell 启动 Chrome + Python CDP 附着）：

| 场景 | 覆盖 | 结果 |
|---|---|---|
| main | 全新用户三步建档、落库、刷新不重复引导、「我」显示昵称、console/网络无错 | 29/29 |
| reopen | 关闭浏览器再进入（同一 profile）不再出现引导 | 2/2 |
| legacy | 已建档老用户直接进入主界面，资料保留 | 3/3 |
| mobile | 390/320 视口三步无横向溢出、手机端可完成建档 | 11/11 |
| offline | 断网保存失败：停留第 3 步、显示错误、**不标完成**；恢复后重试成功 | 16/16 |
| isolation ×2 | 两个用户资料/prompt 不串 | 14/14 ×2 |
| isolation-check | A/B 资料隔离校验 | 5/5 |
| chat | 真实浏览器发消息 → 真实 LLM 回复 → 语音条（同一人物音色） | 21/21 |

证据：`docs/qa/local-web-*.json`、截图 `docs/screenshots/onboarding/`。

### 2.3 AI 对话质量（3 种性格 × 8 话题，真实 LLM）

`python scripts/eval_persona_chat.py`（Callum 外向吐槽 / Mei 内向安静 / Adrian 毒舌短句）：

| 指标 | Callum | Mei | Adrian | 判定 |
|---|---|---|---|---|
| 平均词数 | 27.9 | 14.5 | 7.5 | 性格差异明显 |
| 每轮提问率 | 0.12 | 0.0 | 0.0 | 非「每轮都问」 |
| 重复开头 | 0 | 0 | 0 | 无模板化 |
| 模板句命中 | 0 | 0 | 0 | 无 "I'm sorry to hear…" |
| 语言一致性 | 1.0 | 1.0 | 1.0 | 目标语言正确 |

跨人物口吻相似度 0.094–0.161（< 0.35 阈值），**verdict 6/6 通过**。
人工复核：三种性格在安慰、分歧、用户短句、结束对话上表现不同；重连时会接上次话题（记忆生效）。

### 2.4 四种交互模式一致性

`python scripts/verify_chat_modes.py`：**20/20 通过**

- 文字/图片/通话/语音条均返回真实回复且语言正确；
- 文字与通话 Prompt 共享同一角色卡核心字段（姓名/职业/城市），通话版更短（精简但同一人设）；
- `GET /api/admin/prompt/preview` 验证模块顺序 `global → language → character_card → scenario …`；
- 语音条返回音频地址（同一人物音色），人物动态朗读 TTS 可用。

## 3. 复现方式（本机）

```powershell
# 1) 后端（用数据副本，避免污染本机 data/）
$env:DATA_DIR = "$PWD\.tmp\e2e-data"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000

# 2) 前端静态产物
cd frontend; npm run build:backend; cd ..

# 3) 一键验证
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_local_web_verify.ps1
.\.venv\Scripts\python.exe scripts\eval_persona_chat.py
.\.venv\Scripts\python.exe scripts\verify_chat_modes.py
```

## 4. 服务器部署（47.104.24.236）

- 应用地址：`http://47.104.24.236:8000/`（公网 HTTPS 反代：`https://47-104-24-236.sslip.io/`）
- 部署方式：`scripts/remote_deploy.py`（上传 tar → 服务端代码备份 → `deploy/deploy_v3.sh`）
- 部署前备份：`/root/pre-deploy-backups/data-20261110-191045.tar.gz`（180 MB）
- 服务端代码备份：`/root/deploy-backup/code-20261110-191136.tar.gz`（143 MB）
- 部署脚本数据库备份：`/root/speakeasy/data/_backup/deploy-20261110-191150/speakeasy_v2.db`（6.2 MB）
- 幂等迁移结果：`user_profiles` 3 → **83**（既有 `v3_users` 全部回填为已完成，不会被强制重新引导），
  生产数据零丢失（friends 15 / messages 123 / personas 58 / persona_posts 364 / friendships 15 前后一致）

## 5. 远端 Web 验证（真实浏览器 + 真实 LLM，6 场景全过）

`powershell -File scripts\run_remote_web_verify.ps1`（被测地址 `https://47-104-24-236.sslip.io`）：

| 场景 | 覆盖 | 结果 |
|---|---|---|
| main | 生产环境全新用户三步建档、落库、刷新不重复引导、我页昵称 | 29/29 |
| reopen | 关闭浏览器重进不出现引导 | 2/2 |
| legacy | 已建档用户直接进入、资料保留 | 4/4 |
| mobile | 390/320 视口无横向溢出、移动端可完成建档 | 11/11 |
| chat | 生产 LLM 真实回复 + 语音条 + 实时通话票据 | 22/22 |
| compat | 发现流/人物主页/翻译/朋友圈/TTS/头像 + 页面渲染 | 26/26 |

部署后 15 分钟服务端日志：**0 个 5xx、0 个 traceback、0 ERROR**；公开
`/api/admin/prompt/*` 在未配置管理员密码时返回 403（仅本机可调试）。

## 6. Android 打包与安装验证

- 构建：`mobile/build-apk.ps1 -ApiBase https://47-104-24-236.sslip.io -Mode release -VersionCode 420 -VersionName 4.2.0`
- 产物：`deliverables/SpeakEasy-v4.2.0-release.apk`（**15,086,185 B**，
  SHA-256 `779f36f5ce9bc774999a37a158bdaec3d3c886ef1a5b5c3984473e4b665aad83`）
- 签名：与上一版证书一致（V2，`CN=SpeakEasy`，证书 SHA-256 `fac7e179…369b1`），未更换签名
- 静态检查（`scripts/inspect_apk.py`）：生产域名已编入、无 `127.0.0.1:8000`/LAN 地址、
  无 `sk-`/api_key/密码类字符串 → **PASS**
- 安装环境：Android 16 / API 36 x86_64 模拟器（AVD `sonder42`）
- 安装与启动：`adb install -r` Success；cold start `Status: ok`（3297 ms）
- 首次引导：WebView 内 Step1/2/3 全部正常显示，可完成建档并落库生产 API
- 数据验证：生产 `user_profiles` 83 → 90（含本次 Android 新用户），全部 `setup_completed`
- 重启验证：`am force-stop` 后冷启动直接进入主界面，不再出现引导
- 页面验证：「我」页显示昵称 `AndroidE2E` + `英语 · 能聊几句`；发现页加载 366 条生产动态
- 日志：logcat 无 FATAL/AndroidRuntime 崩溃；服务端同步 0 个 5xx

## 7. 已知限制 / 后续优化

- 人物「首条开场消息」已进入 API 与 Prompt 的 `opening` 模块，但空聊天页 UI 尚未展示
  （当前由第一轮真实 LLM 回复承担开场作用）。
- 管理端 Prompt 调试页需要配置 `SE_ADMIN_PASSWORD` 后从公网访问（当前仅本机可用）。
- `complete_setup=false` 的重置入口当前仅内部可用，未加独立特性开关（按需求暂不对外暴露）。
- 人物开场/示例对话的内容质量依赖种子数据；本次未改种子世界观，只改 Prompt 组装。

## 8. 复现脚本

```powershell
# 本机
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_local_web_verify.ps1
.\.venv\Scripts\python.exe scripts\eval_persona_chat.py
.\.venv\Scripts\python.exe scripts\verify_chat_modes.py
# 远端
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_remote_web_verify.ps1
# 打包
powershell -NoProfile -ExecutionPolicy Bypass -File mobile\build-apk.ps1 -ApiBase https://47-104-24-236.sslip.io -Mode release -VersionCode 420 -VersionName 4.2.0
```