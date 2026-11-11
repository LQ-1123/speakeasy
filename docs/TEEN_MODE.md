# 青少年模式与 AI 身份披露（第一期）

> 对应《Sonder 多用户、个性化世界与商业化升级方案》第一期「青少年模式」与「身份提示」。
> 本期落地：年龄核验消费、监护绑定/撤销、分龄门禁（HTTP/通话/实时通道/推送）、
> 时长与宵禁、运营接口；同时移除「绝不承认自己是 AI」的提示词要求，改为**保留人物风格、
> 被问到时诚实说明 AI 身份**。

## 1. 边界与默认策略

- **14–17 岁**：完成监护绑定后开放普通交流与学习；**无广告、无虚拟伴侣/亲密关系**。
- **未知年龄 / 待核验**：门禁开启时只保留注册处理与帮助入口，内容与聊天接口统一 403。
- **未满 14 岁**：本期不开放注册（运营侧拒绝；代码未引入该分支，由核验结论 + 运营流程控制）。
- 内测默认限制（只允许监护人收紧）：
  - 每天最多 **60 分钟**；
  - 连续使用 **30 分钟**提醒（不强制断开，客户端据 `reminder` 弹提示）；
  - **22:00–次日 06:00** 停用（Asia/Shanghai，按账号时区可配置）。
- 年龄核验结论独立于可编辑的资料生日（`v3_age_verifications`），**不收集证件原件**；
  内测由运营核验后写入，监护关系可由运营代绑或确认码绑定。

## 2. 数据模型（`backend/app/v3/teen.py`，幂等 DDL）

| 表 | 作用 |
|---|---|
| `v3_guardian_links` | 监护关系：`pending`（确认码待用）/`active`/`revoked`；一个未成年人只保留一条 |
| `v3_minor_settings` | 限制配置：每日/连续分钟、宵禁起止、时区、屏蔽人物列表 |
| `v3_minor_usage` | 按本地自然日累计：当日秒数、连续会话起点、最近活动时间 |

建表在 `main.py` lifespan 统一接线（`ensure_teen_schema`），读取侧另有惰性自愈。

## 3. 门禁矩阵

| 身份 | 门禁开启（`SE_AGE_GATE=1`） | 门禁关闭（默认） |
|---|---|---|
| `local` / 旧设备身份 | 不限制（联调/老客户端兼容） | 不限制 |
| 成年（adult + verified） | 全部放行；广告候选 | 全部放行 |
| 青少年（teen + verified + 已监护） | 白天放行；宵禁/超时/屏蔽人物拒绝；连续 30 分钟提醒 | 全部放行（只上报状态） |
| 青少年（未监护/监护撤销） | 403 `minor:guardian_required` | 放行 |
| 未知 / 待核验 | 403 `minor:age_unverified` | 放行 |

拒绝统一为 `HTTPException(403, detail="minor:<reason>")`，`reason ∈ age_unverified /
guardian_required / curfew / daily_limit / persona_blocked`；宵禁还返回 `retry_after_s`
（到次日 06:00 的秒数）。

实际接线：
- HTTP：`/api/v2/chat/*`（文字/语音/图片/通话轮次）、`/api/v3/feed`、`/feed/summary`、
  `/discover/next`、`/personas*`、`POST /posts`、`/events`、`/friendships`；
- 实时：`POST /api/v3/calls`、`/{call_id}/ticket`、`POST /api/v3/client/event-ticket`、
  `WS /api/v3/client/events`（close code 4003）；
- 后台：`client_events.dispatch_push` 对宵禁/超时/待核验用户**保持 pending 并延后重试**，
  不丢弃主动消息；
- 通话结束/退出登录调用 `teen.reset_session` 结束连续会话窗口（不清当日累计）。

## 4. 接口

### 4.1 客户端（`/api/v3/teen`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/status` | 监护角色、限制、今日用量、当前 `access` 结论（含 `reminder`/`retry_after_s`） |
| POST | `/heartbeat` | 记一次活动并返回最新状态（客户端据此弹提醒/收起入口） |
| POST | `/session/end` | 主动结束连续会话（退出/挂断） |
| POST | `/guardian/request` | 未成年人发起监护确认，返回一次性确认码 |
| POST | `/guardian/confirm` | 监护人登录后凭确认码完成绑定 |

### 4.2 运营（`/api/admin/accounts/{uid}/teen*`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/teen` | 状态汇总（监护、限制、用量、门禁结论） |
| POST | `/teen/guardian` | 运营代绑监护关系（写入 `age_verifications=teen/verified`） |
| DELETE | `/teen/guardian` | 撤销监护（立刻回到 `guardian_required`） |
| POST | `/teen/settings` | 调整限制；**只能收紧**（≤60 分钟、≤30 分钟提醒、宵禁覆盖 22:00–06:00） |

写操作沿用管理台统一鉴权与审计（`admin_audit`）。

## 5. AI 身份披露

- 移除「绝不承认自己是 AI / 永远是真人」的提示词（`chat_service` 兜底、`memory` 规则、
  `spawner` 人设、`prompt_composer` 文字/通话/精简角色卡边界）。
- 新措辞统一为：「你是 AI 伙伴，不是真人；被用户直接问到时坦诚说明，同时保留人物语气」。
- 公开动态/事件仍按人物视角叙事（不主动讨论工程实现），但不再禁止承认 AI 身份。
- `event_posts` 的「人物动态提到自己是 AI → 判违规」保留：那是**公共内容质量规则**
  （人物帖子不讨论系统），不影响私聊诚实披露。

## 6. 配置项

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `SE_AGE_GATE` | `0` | 分龄门禁总开关；内测邀请阶段置 `1` |
| （限制值） | 60/30/22:00–06:00 | 目前为代码默认，可由监护人/运营收紧后落在 `v3_minor_settings` |

## 7. 测试证据

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe -m pytest backend/tests/test_teen_core.py backend/tests/test_ai_identity.py -q
# 14 passed（核心 10 + 身份 4，另有两个 teammate 的接线测试文件）
```

- `test_teen_core.py`：默认限制与「只能收紧」校验、监护绑定/撤销即时生效、确认码流程、
  成年/未知/local 矩阵、连续 30 分钟提醒、60 分钟上限、宵禁 + 推送抑制、屏蔽人物、
  `reset_session` 保留当日累计、运营绑定/收紧/撤销、客户端 status/heartbeat。
- `test_ai_identity.py`：文字角色卡、通话角色卡、memory 规则、composer 兜底 prompt 四处措辞，
  并断言旧措辞（`never say you are an AI` / `never claim to be a real human`）不再出现。
- 既有两个断言旧措辞的测试（`test_v2_memory`、`test_v2_spawner`）随新语义更新。

## 8. 已知边界（下一轮补）

1. 前端提示：连续 30 分钟弹窗、宵禁/超时页面、监护确认入口尚未做 UI（接口已就绪）。
2. 举报与「随时退出」入口：后端未建举报表，随合规二期补。
3. 监护人查看使用概况、时段管理 UI：当前只有运营接口，监护人自助视图未做。
4. `SE_AGE_GATE` 默认关闭：需要在邀请内测环境打开；关闭时门禁只上报不拦截（便于灰度）。
5. 通话 WS 的建连门禁由 `POST /calls` 与 ticket 续签控制；未单独在 WS 握手再做一次判定。
