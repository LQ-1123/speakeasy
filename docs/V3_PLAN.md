# SpeakEasy V3 实施清单（阶段 0 产出）

> 目标：把「发现」从「偶遇 + 好友朋友圈」升级为 **以 AI Persona 为中心的社交信息流**，
> 并补齐 Persona / Post / Friendship / SocialEvent 四个独立实体，本地 → 云端 → APK 逐阶段验收。

## 1. 当前架构

```
frontend (Next.js 15, output: export, 静态产物 out/)
  src/app/page.tsx  ──dynamic(ssr:false)──▶  src/w4/App.tsx（W4 主界面：消息/通讯录/发现/表达本/我）
  src/w4/apiFacade.ts：本地模式(local/engine.ts + IndexedDB) / 服务器模式(w4/api.ts → /api/v2)
        │  HTTP  (同源；dev 期 Next rewrite 代理)
        ▼
backend (FastAPI, uvicorn :8000)
  app/main.py：lifespan 装配 engines + v2 services；根路径 StaticFiles 挂 backend/app/static/app（Next 产物）
  app/v2/router.py  /api/v2/*：好友、聊天、语音/通话、朋友圈、表达本、记忆、设置
  app/v2/db.py      SQLite（data/speakeasy_v2.db）：user_profile / friends / messages / moments /
                    user_moments / moment_comments / saved_expressions（**单用户**，无 user_id）
  app/v2/spawner.py LLM 捏人；app/v2/moments.py 好友动态；app/v2/user_moments.py 用户动态 + AI 评论
  app/voice/*、app/llm/*：STT(SenseVoice/SiliconFlow)、TTS(XTTS/Fish/edge)、LLM(DeepSeek 兼容 OpenAI)
deploy/：systemd + nginx 示例；云端 47.104.24.236:8000 已在跑 v2.0.0
mobile/：Capacitor（android/ 原生工程，www/ 为静态产物拷贝）
```

关键事实（审查结论）：

- 入口统一为 `http://localhost:8000`：FastAPI 直接托管 Next 静态导出，**已满足**。
- 现有「发现」= `DiscoverPane`：两个入口「动态(好友朋友圈)」+「偶遇(LLM 现捏一个人)」，
  信息流内容全部来自**已加好友**，没有「未添加的 Persona 也可以出现在信息流」。
- 现库 `friends` 无 `user_id`：全库单用户。用户隔离能力缺失（本次必须补）。
- 动态内容为**按需 LLM 生成**（`/moments/generate`），不能用作公共信息流（成本 + 一致性）。
- 已有 LLM/STT/TTS 全部走后端（key 在 `data/*_config.json`），前端无 key。

## 2. 本次改造模块

| 模块 | 变更 |
|---|---|
| `backend/app/v3/`（新增） | 身份、schema、Persona 语料、社交事件引擎、REST 路由 |
| `backend/app/v2/db.py` | `friends.owner_user_id`、`user_moments.user_id`、`saved_expressions.user_id` + 按当前用户过滤的轻量迁移 |
| `backend/app/v2/router.py` | 路由级用户绑定依赖；好友申请 accept/decline 同步 friendship 状态 |
| `backend/app/main.py` | 装配 v3 services + include v3 router |
| `frontend/src/w4/v3/*`（新增） | feed/persona/events 的 API 客户端与组件 |
| `frontend/src/w4/components/DiscoverPane.tsx` | 「动态」替换为 Persona 信息流；「偶遇」保留为次级入口 |
| `frontend/src/w4/App.tsx` + `model.ts` | 新增 `#/persona/:id` 路由与 Persona 主页 |
| `frontend/src/w4/api.ts` | 所有请求带设备身份头 `X-User-Id` / `X-User-Token` |
| `mobile/` | 全局 API base 指向云端、Android 权限/HTTPS、APK 产物 |

## 3. 数据库变更（迁移脚本内置，SQLite 同库）

新增表：

- `v3_users(id, token_hash, display_name, created_at, last_seen_at)`
- `personas(id, nickname, avatar, bio, personality, interests, language, country, city, timezone, occupation, age, gender, voice_id, archetype_id, ai_config, status, created_at)`
- `persona_posts(id, persona_id, content, media, tags, visibility, like_count, comment_count, created_at)`
- `persona_post_likes(post_id, user_id)` UNIQUE(post_id,user_id)
- `persona_post_comments(id, post_id, user_id, author_type, author_id, content, created_at)`
- `friendships(id, user_id, persona_id, status, friend_id, request_message, created_at, updated_at)` UNIQUE(user_id,persona_id)
- `user_posts(id, user_id, author_name, content, media, tags, visibility, like_count, comment_count, created_at)`
- `user_post_likes(post_id, persona_id)` UNIQUE(post_id,persona_id)
- `user_post_comments(id, post_id, persona_id, content, created_at)`
- `social_events(id, user_id, persona_id, post_id, event_type, payload, status, is_read, dedupe_key UNIQUE, scheduled_at, created_at, processed_at)`
- `social_event_attempts(id, user_id, post_id, persona_id, event_type, reason, roll, created_at)`（含 no_action 统计）

存量表 ALTER（幂等）：

- `friends` + `owner_user_id TEXT DEFAULT 'local'`（老数据归 `local`，单用户行为不变）
- `user_moments` + `user_id TEXT DEFAULT 'local'`
- `saved_expressions` + `user_id TEXT DEFAULT 'local'`

## 4. API 变更（新增 `/api/v3`，v2 全部保留）

- `POST /api/v3/auth/register` → `{user_id, token}`（设备首启，幂等）
- `GET  /api/v3/feed?cursor=&limit=` → Persona 公共动态 + 用户公开动态（游标分页）
- `GET  /api/v3/personas?limit=` / `GET /api/v3/personas/{id}`（含其动态、好友状态）
- `POST /api/v3/personas/{id}/friend` → 建立 Friendship + 复用 friends 表（可直接聊天）
- `GET  /api/v3/friendships`（当前用户）
- `POST /api/v3/posts`（用户发动态：visibility=public|friends|private）
- `GET  /api/v3/posts/mine`、`DELETE /api/v3/posts/{id}`
- `POST /api/v3/posts/{id}/like`、`POST /api/v3/posts/{id}/comments`（Persona 动态互动）
- `GET  /api/v3/events?unread_only=`、`POST /api/v3/events/{id}/read`
- `POST /api/v3/events/{id}/accept|decline`（好友申请事件）
- `GET  /api/v3/health`

鉴权：`X-User-Id` + `X-User-Token`；不带头 = `local` 兼容老客户端。
所有用户维度的数据按 `user_id` 过滤，越权返回 404。

## 5. 已发现的风险

1. **无 SSH 凭据**：`47.104.24.236:22` 可达、8000 已有 v2 在跑，但本机仅有 `id_ed25519_bilimusic`，`root@` 公钥认证被拒（password 认证开放但无密码）。阶段 2 会阻塞，需用户提供。
2. 现库为**单用户共享库**：加 `owner_user_id` 默认 `local` 保证向后兼容；云端多用户需在部署后新建用户身份，历史的 `local` 数据仍归 `local`。
3. LLM 依赖外网（DeepSeek）+ GPU TTS：云服务器需单独验证连通性（阶段 2 待做）。
4. 现有 `w4/store.tsx` 体量大（40KB）：V3 信息流采用独立 hook + 组件，避免改动聊天/记忆主链路。
5. 概率事件必须可测：引擎注入 `random.Random(seed)`，概率/冷却/频率全部可配置（env）。

## 6. 本地启动与测试命令

```powershell
# 后端（已在本机跑通，端口 8000）
cd D:\desktop\口语
$env:PYTHONPATH='D:\desktop\口语\backend'
D:\speakeasy-venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000

# 前端（构建产物拷到后端静态目录，最终由 :8000 提供）
cd frontend
npm run typecheck
npm run build:backend      # next build + copy-out.mjs → backend/app/static/app

# 测试
D:\speakeasy-venv\Scripts\python.exe -m pytest backend/tests -q
D:\speakeasy-venv\Scripts\python.exe -m pytest backend/tests/test_v3_*.py -q
```

## 7. 阶段门禁

- 阶段 1：`http://localhost:8000` 真实浏览器通过场景 A–F，才进入阶段 2。
- 阶段 2：公网 `http://47.104.24.236:8000` 验收通过，才进入阶段 4（APK）。
