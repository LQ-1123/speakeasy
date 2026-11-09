# SpeakEasy · Persona 升级 · 第三阶段（关系深度 + 叙事 + 人物化节奏）

> 前置：第二阶段的「共同经历累积 closeness」已上线并通过本地验收（`docs/PERSONA_ACCEPTANCE.md` §8）。
> 本阶段目标：让关系**不只是变近**，而是有**张力、有历史、能被用户看见**；并让「多久发一次动态」变成人物特征。

铁律不变：一轮聊天/通话 **1 次主 LLM 调用**；刷新信息流 **0 次**；后台任务幂等。

---

## 1. 文件归属

| 所有者 | 本阶段允许写 |
|---|---|
| **Lead** | `schema.py`、`persona_context.py`、`service.py`、`router.py`、`tests/test_persona_relationships.py`、`frontend/src/components/w4/v3/PersonaProfileView.tsx`、`frontend/src/w4/v3/api.ts`、`docs/*` |
| **life-events** | `life_events.py`、`tests/test_v3_life_events.py` |
| **event-content** | `event_posts.py`、`tests/test_v3_event_posts.py` |

---

## 2. 数据库变更（Lead 实施）

```sql
ALTER TABLE persona_relationships ADD COLUMN tension INTEGER NOT NULL DEFAULT 0;      -- 0..5 摩擦度
ALTER TABLE persona_relationships ADD COLUMN last_conflict_at TEXT NOT NULL DEFAULT '';
```

`tension` 是**内部状态**，不直接以数字展示给用户；前端只用它决定是否显示「最近有点小摩擦」这类文案。

---

## 3. 模块 A：关系张力（owner: life-events）

```python
def raise_tension(db, persona_a: str, persona_b: str, *, now=None, delta: int = 1) -> dict | None
    """摩擦 +delta（默认 1），clamp 0..5；同时更新 last_conflict_at / updated_at。
    关系不存在返回 None（不造关系）。"""

def ease_tension(db, persona_a: str, persona_b: str, *, now=None, delta: int = 1) -> dict | None
    """摩擦 -delta，clamp 0..5（不会为负）。关系不存在返回 None。"""

def relationship_summary(db, persona_id: str, *, limit: int = 5) -> list[dict]
    """在第二阶段字段基础上**追加**：
    - tension: int
    - last_shared_at / last_conflict_at
    - recent_event: {'title','event_type','start_time'} | None
      取该对关系**共同参与过**的最近一条 completed 事件（事件集 = persona_id ∪ participants 同时包含两人）。
    排序：shared_events DESC, closeness DESC, tension ASC。"""
```

接线（`refresh_statuses` 完成迁移那一次，与情绪/关系 bump 同一事务）：
- `event_type == "conflict"` → 对每一对参与集合：`raise_tension(delta=1)` + 仍然 `bump_relationship`（争吵也是一次共同经历）；
- `event_type in ("social","relationship","achievement","entertainment")` → 每对 `ease_tension(delta=1)`（修复）+ bump；
- 其他类型（work/meal/daily_routine/…）→ 只 bump，不动 tension。
- 幂等：只在 rowcount==1 的迁移分支执行；重复 refresh 不再改。
- `generate_narrative_events` 的 prompt 增加约束：把「熟人列表」里 `tension>=3` 的人标成 `(currently a bit tense with you)`，并要求**不要**把他们写成愉快的共同活动参与者（可以写「没回他消息」这类张力事件）。

测试：raise/ease 边界与 clamp；conflict 事件完成后 tension +1 且 shared_events 仍 +1；social 事件完成后 tension -1 不为负；
重复 refresh 不变；关系不存在返回 None；`relationship_summary.recent_event` 取到正确事件且按共享次数排序。

---

## 4. 模块 B：张力影响互动 + 人物化发文节奏（owner: event-content）

### 4.1 张力影响评论
`comment_probability(persona, dna_traits, relationship)` 增加：
```
prob *= max(0.15, 1.0 - 0.18 * tension)      # tension 0→1.0, 3→0.46, 5→0.15
```
（`relationship` 里新增 `tension` 字段；缺失按 0 处理，向后兼容。）

### 4.2 人物化节奏（不再全局 8 条）
`should_post` 里的 24h 冷却/上限由 DNA 决定：
```python
max_24h = clamp(round(2 + 3.5*self_disclosure + 2.5*social_distance), 2, 8)
prob *= 1.0 if recent < max_24h*0.5 else 0.45 if recent < max_24h else 0.0
```
- `recent_post_count` 已存在（该 persona 24h 内动态数）；
- 新增可选 `relationship_tension` 不参与发文概率（发文是个人行为，不是关系行为）；
- `run_posts_cycle` 的 `daily_quota` 保留为**系统级**总闸（默认 8），人物级 max_24h 是**个体**闸；两者取小。

### 4.3 观测
导出一个纯函数便于观测：
```python
def persona_post_budget(dna_traits: dict) -> int   # 2..8
```
测试：低 self_disclosure + 低 social_distance 的人物预算明显低于高值人物（2 vs 8）；tension 提高时 comment_probability 单调下降；预算 0/脏输入安全默认；既有测试全绿。

---

## 5. 模块 C：关系在聊天里可见 + 主页可见（owner: Lead）

### 5.1 聊天事实块（`persona_context`）
`build_persona_layer` 的生活事实区追加至多 2 行 `[Your people]`：
```
- Sophie: close friend, 3 shared moments (last: "Jazz bar with Sophie").
- Callum: friendly, but a bit tense right now (last friction: "Pub quiz argument").
```
- 只取 `closeness >= 3` 或 `tension >= 2` 的关系（避免把 11 对熟人全塞进 prompt）；
- 总长度 ≤ 220 字符，超出丢弃后面的；
- 计划中的事件不能当既往经历（沿用 `event_fact_block` 口径）。

### 5.2 Persona 主页 API
`persona_profile()` 增加：
```json
"relations": [
  {"persona_id":"p_x","nickname":"Sophie","relation_type":"friend","closeness":4,
   "tension":0,"shared_events":3,"recent_event":{"title":"Jazz bar with Sophie","event_type":"social"}}
]
```
最多 3 条，按 shared_events DESC。

### 5.3 前端（Persona 主页一行）
在 Persona 主页「兴趣」下方加一行灰字：
`最近常见到 Sophie、Callum`（closeness>=3）或 `最近和 Sophie 有点小摩擦`（tension>=3），
没有关系数据时不渲染。**不展示任何数字/内部 ID**。

### 5.4 观测端点
`/api/v3/stats/persona` 的 `relationships` 增加 `avg_tension`、`tense_pairs`（tension>=3 的对数）。

---

## 6. 验收门

1. `pytest backend/tests -q`：无新增失败（基线 6 个历史失败不变）。
2. 真实浏览器打开一个 Persona 主页：能看到关系那一行（或正确不渲染）。
3. 聊天 prompt 里能看到 `[Your people]` 行（用测试断言，不靠肉眼）。
4. 关系张力的真实链路：插入 conflict 事件 → 后台 tick 完成 → `tension +1`、`shared_events +1`、评论概率下降。
5. 更新 `docs/PERSONA_ACCEPTANCE.md` 第三阶段章节。
