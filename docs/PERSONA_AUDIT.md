# SpeakEasy · Persona 真实感升级：现有代码审计

> 审计时间：2026-11-07 · 基线：`main` @ `1508412` · 全部结论来自实际读代码与本地库探查，不是推测。
> 基线测试：`pytest backend/tests/test_v3_*.py -q` → **74 passed**。

---

## 1. 当前 Persona 数据结构

主表 `personas`（`backend/app/v3/schema.py`，与 V2 共用 `data/speakeasy_v2.db`）：

| 字段 | 用途 |
|---|---|
| `id`, `nickname`, `avatar`, `avatar_seed`, `avatar_style`, `avatar_options` | 身份与头像（Avataaars 部件） |
| `bio`, `personality`, `interests`(JSON), `occupation`, `age`, `gender` | 人设（**自由文本**，非结构化） |
| `language`, `country`, `city`, `timezone` | 母语与地点（决定信息流语言、声线、作息时区） |
| `voice_id`, `fish_voice_id`, `archetype_id` | 声线 |
| `ai_config`(JSON) | 只有 `{"temperament","energy"}` 或 `{"generated":...}`，**没被 prompt 使用** |
| `status`, `lifecycle`(permanent/guest), `expires_at`, `last_interaction_at`, `interaction_count` | 生命周期 |

**结论**：人物性格是「一段英文/母语自由文本」+ 两个未被使用的数值。没有任何结构化表达配置，也没有示例对话。

## 2. 当前聊天生成链路

```
用户消息
→ ChatService._handle_user_text (backend/app/v2/chat_service.py:978)
→ db.add_message / add_turns / closeness_level
→ _llm_reply (1210)
   → _system_prompt(friend, review_exprs) (1130)
       = persona(personality_prompt + bio)
       + base(语言规则 + 「1-2 VERY short lines」+「Always leave a hook」+「easy way in」)
       + memory.build_context(friend)
   + history[-10:] + 当前消息
→ 一次 stream_chat 调用（temperature 0.85, max_tokens 600）
→ 语言校验 → 不合格就**再来一次** rewrite 调用 → 还不合格走 _mock_reply 模板
```

关键事实：
- **一次主生成**，只在语言不匹配时额外一次 rewrite。行为决策阶段**完全不存在**。
- `friend` 是 V2 `friends` 行，不是 V3 `personas` 行。桥接在 `V3Service._friend_row_for_persona()`
  （`backend/app/v3/service.py`）：`personality_prompt = personality + "\n" + bio`，
  **只复制一次**，之后 Persona 侧变化不会同步。
- prompt 里没有任何「当前在做什么 / 最近发生了什么」的信息，只有时区作息标签。

## 3. 当前记忆系统

`backend/app/v2/memory.py`（`MemoryManager`）已相当完整，**必须复用**：

| 组成 | 位置 | 说明 |
|---|---|---|
| UserFactSheet | `user_profile.fact_sheet` | 用户画像（兴趣、语言水平、事实） |
| SharedEpisodes | `friends.shared_episodes` | 与用户共同经历摘要 |
| OpenLoops | `friends.open_loops` | 用户提到但未闭环的事 + 到期追问 |
| review_expressions | `saved_expressions` | 待复习表达，注入 1-2 条 |
| 熟稔度 | `app/v2/social.py` | turns → Lv1..5 语气 |
| 当地时间/作息 | `app/v2/timeutil.py` | period + activity 标签 |
| 后台提炼 | `MemoryManager.after_turn` | LLM 提炼 fact/loop/episode |

**结论**：记忆是 **Persona-User 关系维度**的，完全没有 **Persona 自身生活维度**。这是「人物在用户不聊天时不存在」的根因。

## 4. 当前动态生成方式

三条互不相干的路径，**都不来自事件**：

1. **种子语料** `seed_data.py` → `POSTS`（36 条硬编码），`seed_if_needed()` 幂等 upsert。
2. **每日世界** `daily_world.py::generate_daily()`：每天 02:00 批量**新建 5 个 guest 人物 + 每人 3 条动态**，全部由 LLM 一次生成（人物 prompt 要求顺带写 posts）。
3. **生活图文** `life_posts.py::generate_life_posts()`：为当天新人物抓图库图片 + 写文案，1 图 1 文。

一条 `persona_posts` 的来源只记录为 `post_kind`(life_text/life_photo) 与 `source_ref`（图片授权信息）。
**没有任何字段指向「因为发生了什么所以发了这条」**。

## 5. 当前 SocialEvent 的实际作用

`social_events` 表 = **给用户的互动通知队列**，不是人物生活事件：

- 字段：`user_id`, `persona_id`, `post_id`, `event_type`(like/comment/friend_request),
  `payload`, `status`, `dedupe_key`, `scheduled_at`, `processed_at`。
- 产生方式：用户发动态 → `trigger_social_events()` 按兴趣匹配候选人 → `social_engine.py::plan_events()`
  概率抽样 → 延迟落库 → `materialize_due_events()` 物化。
- 语义：**"某个 Persona 对用户的这条动态做了反应"**，`user_id NOT NULL`，1 事件 = 1 通知。

**结论**：`social_events` 不能承担「人物自己的生活事件」——它是**用户私有的通知**，不是**人物全局经历**；
多人物共同参与、计划/进行/完成/取消状态、事件→动态的 1:N 关联，它一个都不具备。
→ 需要新表 `persona_events`（理由见 §9 与计划文档）。

`social_engine.py` 本身是**纯逻辑、可注入 rng**，质量很好，其「允许什么都不发生」「候选打分」
「冷却/上限」的模式应被新的生活事件/发文概率模块**直接借鉴**。

## 6. 数据库中已有的事件、动态、人物关系字段

```
personas          17 行（12 种子 + 5 每日 guest）
persona_posts     61 行
friendships        2 行    ← 用户↔Persona，friend_id 指向 V2 friends 行
social_events      3 行
friends            4 行    ← V2 聊天主体（personality_prompt / speech_quirks / shared_episodes / open_loops）
messages           4 行
daily_world        1 行    ← 当天批次幂等标记
```

- 人物↔人物关系：**不存在**。`personas` 之间没有任何表。
- 事件：只有用户通知型 `social_events`。
- 动态→事件：**不存在**。
- 动态评论：`persona_post_comments` **只允许 user 评论**（列是 `user_id`），Persona 之间不能互评。

## 7. 可直接复用的能力

| 能力 | 复用方式 |
|---|---|
| `MemoryManager` / `fact_sheet` / `open_loops` / `shared_episodes` | 全部保留，只在其上**追加** Persona 生活层，不重建 |
| `timeutil.local_state()` | 作为规则日程的时间基座 |
| `social.closeness_level/style` | 行为选择的一个输入 |
| `social_engine.py` 的概率/冷却/允许空手而归模式 | 新模块照此风格（纯函数 + 注入 rng，可测） |
| `daily_world.daily_loop()` 后台批处理框架 | 生活事件生成挂到同一个 loop，不引入新框架 |
| `llm_json.complete_json_safe()` | 所有结构化 LLM 输出的统一出口（自带 fallback / JSON 修复） |
| `_friend_row_for_persona()` 桥接 | 扩展它，把 Speech DNA / 生活状态同步进 `friends` |
| `_persona_post_public()` 返回结构 | 前端 `FeedItem` 类型已支持 `author.is_ai` 的 AI 评论，**前端几乎不用改** |
| 幂等迁移风格（`PRAGMA table_info` + `ALTER TABLE`） | 所有新列/新表沿用，可反复执行 |

## 8. 需要扩展的能力

1. **Speech DNA**：新增结构化表达配置（`personas.speech_dna` JSON）+ 示例对话表 `persona_example_dialogs`。
2. **Example Dialogue 检索**：按当前情境挑 1-2 组注入，不能全塞。
3. **Behavior Engine**：新增 `app/v3/behavior.py`（纯规则、零 LLM），在 prompt 组装前决定「这轮想干什么」。
4. **Persona Life Events**：新增 `persona_events` + `persona_schedules` + `persona_relationships`。
5. **Emotion State**：新增持续性状态 `personas.emotion_state`（mood/energy/stress/social_battery + 衰减），不是回复后分类。
6. **事件→动态**：`persona_posts` 增加 `source_event_id/post_type/published_at/metadata`；`persona_post_comments` 增加 AI 作者列。
7. **聊天检索生活事实**：新增 `persona_context.py` 统一组装（DNA/示例/行为/生活状态/事件/自己发的动态/相关评论）。
8. **移除模板化强制规则**（见 §9）。

## 9. 当前实现中导致人物表达模板化的**具体原因**

按危害排序，全部有代码位置：

1. **强制每轮留钩子**
   `chat_service._system_prompt` 的 base 结尾：`"Always leave a hook (a question or an invitation to continue)."`（约 1192 行）+
   `memory.build_context` 的 `[Rules] ... then end with a hook (a question or a playful tease)`（约 900 行）。
   → 每个 Persona 每句话都以问题结尾，这正是「客服腔」的第一来源。

2. **强制极短**
   `"Reply with 1-2 VERY short lines"`（1141）+ `"1-2 short spoken English sentences (under 40 words total)"`（memory）。
   → 人物无法讲故事、无法吐槽、无法展开自己的生活；长度分布方差被压成 0。

3. **强制热情/配合**
   base 里 `"Always leave an easy way in"`、`"be warm and make it easy"`、`"Keep YOUR turns short - they should be doing most of the talking"`。
   → 不允许冷淡、不允许拒绝话题、不允许只说一句 "hm."

4. **Persona 差异只来自一段自由文本**
   `personality_prompt` 是唯一的人格来源，且 `_friend_row_for_persona` 复制一次后不再同步；
   `ai_config.temperament/energy` 存在但**没有任何代码读它**。
   → 三个不同性格的人物分享同一套句式约束（短、留钩子、热情），差异只剩词汇。

5. **模型兜底模板本身就是客服话术**
   `_fallback_reply()`：`"{quirk}, that sounds interesting. Tell me more about it?"`
   `_mock_reply()` 的 topic 模板池同理。
   → 任务书里点名的那句 "Yeah, that's interesting! What about you?" 在代码里真实存在。

6. **没有行为决策层**
   同一句用户输入，无论人物心情、关系、最近发生过什么，都走同一条「react + end with hook」路径。

7. **没有任何生活事实可讲**
   prompt 里没有「你昨天做了什么」「你刚发了什么」，人物只能对用户做出反应，无法**主动提供内容**。
   `self_disclosure`（分享自己）在结构上不可能发生。

8. **最近行为无记忆**
   `friends.recent_behaviors` 不存在，模型不知道上一条自己是不是已经问过问题。

---

## 10. 最小必要改造方案（结论）

- **不新建**聊天/记忆系统：`MemoryManager` 原样保留，只做「追加一层 Persona 生活上下文」。
- **不新建**第二套社交事件：`social_events` 保留原职责（用户通知），新增 `persona_events` 承担人物生活。
- **不新建**调度框架：生活事件生成挂进现有 `daily_loop`，幂等键沿用 `daily_world.day` 风格。
- **不改造** feed 主查询：`source_event_id` 等字段是**附加列**，信息流仍只读数据库，不触发生成。
- **一次聊天仍然只有一次主 LLM 调用**：行为选择、示例检索、事件检索全部是规则/DB 查询。
- 旧 Persona 兼容：新列全部有默认值；DNA/示例由 `derive_dna()` 确定性回填，无需重跑 LLM 也能用。

详细模块契约与开发顺序见 `docs/PERSONA_UPGRADE_PLAN.md`。
