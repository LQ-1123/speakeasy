# SpeakEasy · Persona 升级 · 第二阶段（语音链路 + 生活节奏 + 关系演进）

> 前置：第一阶段已交付并通过本地验收（见 `docs/PERSONA_ACCEPTANCE.md`）。
> 本阶段目标：**把「有生活的人物」从文字聊天扩展到语音通话，并让生活节奏与人物关系真正随时间变化。**
>
> 铁律不变：一轮聊天/一轮通话仍然只有 **1 次主 LLM 调用**；刷新信息流 **0 次**；后台任务必须幂等。

---

## 1. 文件归属

| 所有者 | 本阶段允许写 |
|---|---|
| **Lead** | `backend/app/v3/schema.py`、`backend/app/v3/persona_context.py`、`backend/app/v2/chat_service.py`、`backend/app/v3/router.py`、`backend/app/v3/service.py`、`backend/app/main.py`、`backend/tests/test_persona_call.py`、`docs/*` |
| **life-events** | `backend/app/v3/life_events.py`、`backend/tests/test_v3_life_events.py` |
| **event-content** | `backend/app/v3/event_posts.py`、`backend/tests/test_v3_event_posts.py` |

---

## 2. 数据库变更（Lead 已实施）

```sql
-- persona_relationships 增加「共同经历」证据
ALTER TABLE persona_relationships ADD COLUMN shared_events INTEGER NOT NULL DEFAULT 0;
ALTER TABLE persona_relationships ADD COLUMN last_shared_at TEXT NOT NULL DEFAULT '';
```

---

## 3. 模块 A：人物关系演进（owner: life-events）

```python
def relationship_between(db, persona_a: str, persona_b: str) -> dict | None
    """双向查询，返回 {'id','persona_a','persona_b','relation_type','closeness',
    'shared_events','last_shared_at'}；不存在返回 None（方向已归一化）。"""

def bump_relationship(db, persona_a: str, persona_b: str, *,
                      now: Any = None) -> dict | None
    """共同经历 +1 并可能加深关系（幂等由调用方保证只在该事件完成时调一次）：
    - shared_events += 1，last_shared_at = now
    - closeness 单调不减：shared_events ∈ {2, 5, 9, 14} 时 +1，上限 5
    - 不存在的关系统一返回 None（不凭空造关系）
    """

def relationship_summary(db, persona_id: str, *, limit: int = 5) -> list[dict]
    """人物主页/观测用：熟人昵称 + 关系类型 + closeness + 共同经历数。"""
```

接线要求：`refresh_statuses()` 里**事件从 planned/ongoing 迁移到 completed 的那一次**，
若 `participants` ≥ 2，对**每一对参与者**调用一次 `bump_relationship`（与情绪写入同一事务，重复 refresh 不重复加）。
`cancelled` 不加。

测试（加到 `test_v3_life_events.py`）：
- 同一事件完成一次 → 关系 `shared_events` +1；再 `refresh_statuses()` 不再加。
- 两个参与者的关系双向可查（A→B 与 B→A 同一条）。
- 单参与者事件不 bump；cancelled 不 bump。
- closeness 在 shared_events=2/5/9 时 +1 且不超过 5，且单调不减。

---

## 4. 模块 B：事件动态「当天感」（owner: event-content）

### 4.1 价值排序（让当天先出现「值得分享」的事件）

`_completed_event_rows` 的排序改为**价值优先，再按时间正序**：

```
value_tier: 3 = achievement / conflict / personal_milestone / relationship
            2 = social / entertainment / travel
            1 = work / study / unexpected_event
            0 = meal / daily_routine
ORDER BY value_tier DESC, COALESCE(NULLIF(completed_at,''), NULLIF(end_time,''), start_time) ASC, id ASC
```

### 4.2 每日配额

```python
async def run_posts_cycle(db, llm, *, day=None, max_events=6, rng=None,
                          since_hours=None, daily_quota: int | None = None) -> dict
```
- `daily_quota`：当天（本地日）**新建事件动态**的硬上限，默认读 `SE_EVENT_POSTS_PER_DAY`（缺省 8）；
  `None`/`<=0` 表示不限制。
- 返回里带 `daily_quota`、`today_posts`（当天已产出的条数）。
- 配额用尽时直接返回，不做候选查询（省掉无意义的 DB 扫描）。

### 4.3 评论补位（让互动不只集中在「这一轮刚发的」）

```python
async def comment_backfill(db, llm, *, max_comments: int = 2,
                           window_hours: float = 6.0, rng=None) -> dict
```
- 取「最近 window_hours 内的事件动态、还没有任何 AI 评论」的 post，
  按关系与 `comment_probability` 决定评论者（必须是该事件参与者之外的熟人，且必须已有关系）。
- 每条动态最多 1 条 AI 评论；总共最多 `max_comments` 条/次；幂等（已评论的不会再评）。
- 返回 `{'posts','comments'}`。

`run_posts_cycle` 结尾自动调用一次 `comment_backfill`（可配 `SE_COMMENT_BACKFILL=0` 关闭）。

测试：
- 价值排序：构造 meal（早完成）+ achievement（晚完成），`max_events=1` 时先处理 achievement。
- 配额：`daily_quota=1` 时一轮最多 1 条动态，第二轮 0 条且 `today_posts` 正确。
- `comment_backfill`：老动态能补到评论、第二次 0 条、`max_comments` 生效、无关系的评论者不会被选。

---

## 5. 模块 C：语音通话 Persona 化（owner: Lead）

`persona_context` 新增：

```python
def build_call_plan(db, friend, transcript, *, turn_index: int = 0) -> TurnPlan
    """与 build_turn_plan 同源，但只做通话需要的部分（不加示例对话以控 TTFT）。"""

def build_call_layer(db, friend, plan, *, language: str = "") -> str
    """通话专用紧凑层（上限 ~520 字符）：
    [Speech DNA] 只保留 节奏/长度/幽默/直接度 四行
    [Your life right now] 一行（当前在做什么）
    [This turn on a call] 行为动作 + 「这是语音通话：1-2 句、口语、不要念稿」+ 是否允许提问
    """
```

`chat_service._handle_call_turn_stream`：
- 在拼 prompt 前 `build_call_plan`；call_hint 的「ONE short spoken sentence (max ~18 words)」改为**由 DNA verbosity 决定**（one_line→≤12 词；short→≤20；medium→≤30；long→≤40，且不超过 3 句）。
- prompt = persona + call_hint + call_layer（`call_layer` 放最后，模型对末尾指令最敏感）。
- 回合结束后 `record_behavior` + `update_emotion_after_turn`（与文字聊天一致）。
- 仍然只有 1 次 `stream_chat`。

测试（`backend/tests/test_persona_call.py`）：
- 同一个人物的通话 prompt 含 DNA 与行为指令，且长度受 verbosity 影响。
- 一轮通话恰好 1 次 `stream_chat`。
- `build_call_layer` 长度 ≤ 520；不同人物 layer 不同。
- 通话后 `recent_behaviors` 有新增记录、情绪状态被更新。

---

## 6. 模块 D：只读观测端点（owner: Lead）

`GET /api/v3/stats/persona`（无需身份，只读，不含任何用户数据）：

```json
{
  "personas": {"total": 22, "permanent": 12, "adopted": 5, "guest": 5},
  "events": {"total": 92, "planned": 43, "ongoing": 0, "completed": 43, "cancelled": 6,
             "by_type": {"meal": 20, "...": 0}, "by_source": {"rule": 68, "llm": 24}},
  "posts": {"persona_posts": 81, "event_driven": 8, "by_type": {"life_event": 8},
            "last_24h": 9},
  "comments": {"persona_comments": 1, "user_comments": 0},
  "relationships": {"total": 15, "avg_closeness": 2.6, "deepest": 4},
  "emotion": {"with_state": 16, "avg_energy": 0.58, "avg_stress": 0.24},
  "background": {"life_llm": true, "posts_tick_s": 240, "posts_max_events": 4,
                 "posts_since_hours": 6, "event_posts_per_day": 8}
}
```

测试：端点 200；字段齐全；**不触发任何 LLM**（把 `engines.llm` 换成抛异常的 stub，端点仍应正常返回）。

---

## 7. 验收门

1. `pytest backend/tests -q`：新增失败必须为 0（基线 6 个历史失败不变）。
2. 通话链路：真实浏览器打一次电话（或 `POST /api/v2/chat/{friend_id}/call/turn-text`），
   回复风格与该人物文字聊天一致（短/长、干/暖），且**不每句追问**。
3. 信息流：当天事件动态中，先出现 achievement/social 类；刷新不产生新动态。
4. 观测端点：`curl /api/v3/stats/persona` 数据自洽。
5. 更新 `docs/PERSONA_ACCEPTANCE.md` 的第二阶段章节。
