# 商业合作（广告）闭环：管理台、聊天/动态投放与频控（第三期）

> 对应《Sonder 多用户、个性化世界与商业化升级方案》第三期「成年用户的广告灰度」。
> 本期落地：品牌/活动/素材/角色分配/预览/投放记录/汇总的管理台闭环，聊天与动态两渠道投放，
> 原子额度预留与频控、曝光/点击/关闭/举报事件、触发摘录留痕与保留策略。
> **未成年人、未知年龄、未主动开启合作推荐的用户一律不投放。**

## 1. 合规边界（硬约束）

- 只有**已核验成年**（`age_group=adult` 且 `verified`）且**主动开启**「合作推荐」的用户才可能看到；
  默认关闭（`client_preferences.ads=false`），客户端可在设置里随时关闭。
- 未成年/未知年龄即使开启偏好也不投（`ads.ad_eligible` 返回 `not_adult_verified`）。
- 聊天广告：**先正常回答，再给商业段落**；文案只由**审核通过的素材模板 + 活动事实**拼装，
  不接 LLM，不虚构功效/价格/使用体验；带「广告 · 品牌合作」标识。
- 心理困扰/情感依赖类表述（焦虑、孤独、想哭、insomnia…）**不营销**。
- 用户关闭/举报广告 → 该活动屏蔽 30 天；活动暂停/结束 → 立即停止新投放。
- 广告不进入人物长期记忆、不写入人物公共情绪/经历，也不出现在人物主页、偶遇、评论链路。

## 2. 数据模型（`backend/app/v3/ads.py`）

| 表 | 作用 |
|---|---|
| `ads_brands` | 品牌/资质/落地页/启停 |
| `ads_campaigns` | 活动：目的、所需标签、约束、审核事实、渠道、有效期、状态、版本 |
| `ads_campaign_versions` | 活动版本快照（修改已发布活动产生新版本，历史可还原） |
| `ads_creatives` | 素材：按渠道独立审核（draft/approved/rejected） |
| `ads_assignments` | 人物分配：系统按兴趣/职业/语言给建议，运营确认才算可投 |
| `ads_deliveries` | 投放记录：渠道、活动版本、素材、状态、位置、预留/确认时间、上下文 |
| `ads_events` | 曝光/点击/关闭/举报事件（另存素材审核备注） |
| `ads_chat_audit` | 聊天投放留痕：匹配理由、触发消息 + 前文摘录（≤1000 字符）、30 天删除 |
| `ads_feed_state` | 每个用户的「距上次动态广告的普通动态条数」 |
| `ads_blocks` | 用户关闭/举报后的活动屏蔽（30 天） |

建表在 `main.py` lifespan（`ensure_ads_schema`），另在读取侧惰性自愈。

## 3. 活动生命周期与管理台

状态机：`draft → reviewed → enabled → paused → enabled`，任意状态可 `ended`。
已发布（reviewed/enabled/paused）的修改会**生成新版本并回到 draft**，必须重新审核；
`restore/{version}` 还原历史版本（也生成新版本，不删历史）。

管理台接口（`/api/admin/ads*`，统一鉴权 + 审计）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/brands`、PATCH `/brands/{id}` | 品牌与资质 |
| GET/POST | `/campaigns`、GET/PATCH `/campaigns/{id}` | 活动列表/创建/详情/修改（版本） |
| POST | `/campaigns/{id}/status` | 状态流转（非法跳转 400） |
| GET | `/campaigns/{id}/versions` | 版本历史 |
| POST | `/campaigns/{id}/restore/{version}` | 还原历史版本 |
| GET/POST | `/creatives`、POST `/creatives/{id}/review` | 素材提交/审核（驳回必须写原因） |
| GET/POST | `/campaigns/{id}/assignments`、`/suggest`、`/{persona_id}/confirm` | 角色分配建议/确认 |
| GET | `/campaigns/{id}/preview?channel=` | 预览最终文案（含是否已审核素材） |
| GET | `/deliveries`、`/stats` | 投放记录、按活动汇总 |
| POST | `/audit/cleanup` | 立即清理过期聊天留痕（后台 cleanup_job 也会跑） |

## 4. 投放规则

### 4.1 聊天（`try_chat_ad`）

1. 资格：成年 + 已核验 + 已开启偏好；
2. 需求识别（规则版）：命中兴趣/商品标签才继续；泛聊、否定、不确定、心理困扰直接放弃；
3. 候选：已启用 + 在有效期 + 渠道含 chat + **该人物已由运营确认分配** + 有已审核素材；
4. 频控全部通过后在 `BEGIN IMMEDIATE` 事务里再次核验并插入 `reserved`（原子预留）；
5. 发送成功后 `confirm_delivery` 标记 delivered；预留超过 5 分钟未确认自动 released；
6. 写入 `ads_chat_audit`：触发消息 + 最多两条前文，合计 ≤1000 字符，30 天后删除。

消息层面：广告是独立消息 `msg_type='ad'`，`context` 存投放元数据；`db.recent_messages()`
过滤广告，因此**prompt/记忆提炼永远看不到商业段落**；`list_messages` 仍返回它供 UI 展示。

### 4.2 动态（`try_feed_ad`）

- 只在个人推荐流混排 `kind="sponsored"` 条目（含 `delivery_id`、`label="广告"`、活动/素材/落地页）；
- 第一页前 3 条不插；两条广告之间 ≥8 条普通动态（`ads_feed_state` 记录）；
- 24 小时合计 ≤2 次（与聊天共用额度，聊天 ≤1/24h）；
- 活动暂停/结束、用户关闭后屏蔽期 → 不投；
- 广告不计入推荐快照条目，`next_cursor` 语义不变；不进入 `feed_summary`。

### 4.3 频控常量（内测默认）

| 规则 | 值 |
|---|---|
| 两渠道合计 | 2 次 / 24h |
| 聊天 | 1 次 / 24h，且两次之间 ≥10 个用户回合 |
| 动态 | 前 3 条不插，间隔 ≥8 条普通动态 |
| 同品牌冷却 | 7 天（两渠道共用） |
| 关闭/举报屏蔽 | 30 天（该活动） |
| 预留有效期 | 5 分钟 |

## 5. 用户侧接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v3/ads/status` | 是否可接收（`eligible` / `reason`）、偏好、年龄组 |
| POST | `/api/v3/ads/opt-in` | 开启/关闭合作推荐（非成年/无能力 403） |
| POST | `/api/v3/ads/events` | 上报 `impression` / `click` / `close` / `report`（关闭/举报写 30 天屏蔽） |

动态广告条目由 `/api/v3/feed` 直接返回；聊天广告通过聊天的 `ad_message`（及历史消息里的
`msg_type='ad'`）返回。

## 6. 保留与留痕

- 品牌资质、活动版本、素材审核、投放记录/事件：**≥3 年**（不自动删除；`ARCHIVE_KEEP_DAYS`）。
- 聊天触发摘录：**30 天**自动删除（`ads_chat_audit.delete_after`，后台 `cleanup_job` 与
  管理台 `/audit/cleanup` 都可触发）。
- 不上传完整聊天记录、不记录用户心理状态标签。

## 7. 测试证据

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe -m pytest backend/tests/test_ads_core.py backend/tests/test_ads_admin.py `
  backend/tests/test_ads_feed.py backend/tests/test_ads_chat.py -q
# 27 passed（核心 10 + 管理台 3 + 动态 5 + 聊天 9）
.\.venv\Scripts\python.exe -m pytest backend/tests -q
# 1245 passed
```

- `test_ads_core.py`：活动版本/状态机、素材审核、分配、需求识别（命中/泛聊/否定/困扰）、
  资格矩阵、聊天文案来自事实、24h 上限、回合间隔、同品牌冷却、关闭屏蔽、动态间距与形状、
  事件状态与统计、预览与留痕清理。
- `test_ads_admin.py`：品牌/活动/素材全链路、版本还原、分配建议+确认、预览、投放记录/汇总、留痕清理。
- `test_ads_feed.py`：前 3 条不插、≥8 条间隔、未成年/未知/未开启不投、暂停即停、事件/屏蔽、
  `feed_summary` 不含广告。
- `test_ads_chat.py`：正常回复后追加商业段落、未成年/未开启/未命中去投、广告消息不进
  `recent_messages`、24h 内仅一次、文案来自审核模板+事实。

## 8. 已知边界（后续）

1. **前端**：广告卡片 UI（`label`/`landing_url`/关闭举报按钮）、曝光可见性判定由客户端上报
   `visible_ms`，目前服务端只存 payload，不做可见性校验；语音/通话渠道暂不投放（方案首版只做文字）。
2. **归因与计费**：本期只做事件留痕与汇总，不含点击跳转归因、计费/结算、反作弊。
3. **素材自动化**：审核目前必须人工触发；模板变量仅 `{facts}`/`{fact}`，不做多语言自动翻译。
4. **频控参数**：常量写死，后续可按用户/实验配置化。
5. **举报入口**：广告关闭/举报走 `/api/v3/ads/events`（30 天屏蔽）；通用内容举报
   （人物/动态/评论/私聊消息）见 [ACCOUNTS_AND_PRIVACY.md](ACCOUNTS_AND_PRIVACY.md) 第 9.1 节。
