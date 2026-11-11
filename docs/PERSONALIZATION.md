# 个性化世界：标签体系、推荐快照与供给恢复（第二期）

> 对应《Sonder 多用户、个性化世界与商业化升级方案》第二期「共享世界的个人化体验」。
> 本期落地：统一标签 ID + 多语言显示名 + 别名迁移、用户专属推荐快照游标（兴趣/好友/近期互动/
> 新鲜度/已看 + 20% 探索）、生成批次中断恢复、最终过期清理、按活跃关系与内容缺口决定供给，
> 并把「用户学习水平」从公共世界生成里剥离（只影响私聊/学习辅助）。

## 1. 统一标签体系（`backend/app/v3/taxonomy.py`）

- **稳定 ID**：小写英文 slug（`coffee`、`cycling`、`photography`…共 28 类），写入数据库的永远是 ID。
- **多语言显示名**：`TAGS[id] = {zh, en}`；`labels_map(ids)` 一次性下发给前端 `{id: {zh, en, known}}`。
- **别名归一**：中英文常见写法映射同一 ID（`咖啡`/`coffee`/`espresso`/`cafe` → `coffee`；
  `骑行`/`bicycle`/`bike` → `cycling`…），并支持 `/`、`、`、`#`、大小写、多余空格等清洗。
- **未知自定义标签**：保留为 `custom:<slug>`（中文等无法 slug 时 `custom:x-<hash>`），
  不静默丢弃；`label()` 给出可读占位。
- **写入口径**：资料兴趣（`update_profile_fields`）、发帖标签（`create_user_post`）、
  游客人物/动态（`daily_world._insert_persona`）、LLM 世界（`world._insert_persona`）统一走
  `taxonomy.canonical_tags`。
- **历史迁移**：`migrate_tags(db)` 幂等归一 `user_profiles.fact_sheet.interests`、
  `personas.interests`、`persona_posts.tags`、`user_posts.tags`；启动时（`main.py`）与世界
  同步时（`world.ensure_world`）各跑一次，老库不需要手工脚本。
- **读口径**：推荐/偶遇/匹配都按 ID 比较（`_user_interests`、`discover_next` 的 `match_tags`）；
  `profile_state()` 新增 additive 字段 `interest_labels`。

## 2. 推荐流与快照游标（`backend/app/v3/recommend.py`）

### 2.1 数据

| 表 | 作用 |
|---|---|
| `v3_feed_snapshots` | 每个用户每次刷新一份有序推荐快照（TTL 30 分钟，payload 为 `[{kind,id}]`） |
| `v3_feed_seen` | 用户已看记录（`user_id + item_key`，48 小时内降权） |

### 2.2 排序

| 信号 | 权重 |
|---|---|
| 动态标签 ∩ 用户兴趣 | +3 / 命中 |
| 人物兴趣 ∩ 用户兴趣 | +2 / 命中 |
| 好友关系 | accepted +4 / pending +1 |
| 近 7 天与该人物互动 | +2 |
| 新鲜度 | 36 小时半衰期，满分 2 |
| 48 小时内已看 | −3 |

- **本人动态**三种可见性全部包含并置于流首（保证 `is_mine` 与「我的动态」语义）。
- **20% 探索位**：容量留 20% 给低分/未看候选，`random.Random(snapshot_id)` 打乱后每 5 条插 1 条；
  同一快照内顺序稳定，翻页不重复、不遗漏。
- **回退**：候选不足/无兴趣命中时按新鲜度与语言过滤回退到共享适龄内容；语言过滤
  （`target_languages`）在快照构建时执行，与旧行为一致。

### 2.3 游标与刷新

- 无 cursor → 建快照取首页，返回 `next_cursor="snap:<snapshot_id>:<offset>"`；
- 有 cursor → 读取同一快照的下一页；
- cursor 非法/过期/属于他人 → 自动重建新快照（200，不报错）；旧的 `created_at|id` 游标也安全回落；
- 每页返回时顺带 `mark_seen`，供后续快照降权。

## 3. 世界生成恢复、清理与供给（`daily_world.py` / `lifecycle.py`）

### 3.1 中断恢复（不再重复造人）

- 游客人物 ID 确定性：`p_d{YYYYMMDD}{idx:02d}`；动态 ID：`pp_{persona_id}_{idx:02d}`；
- personas / persona_posts 全部 `INSERT OR IGNORE`；已存在人物不再重新挑声线；
- 单个 persona 失败 `try/except` 跳过且**不写成功行**；批次目标人数先写
  `v3_meta['daily_batch:<day>']`，重试按同一批人数补齐后清标记——「生成中断重跑不重复造人」。

### 3.2 最终清理（`lifecycle.cleanup`）

- 保留旧行为：互动动态过期后延长到「创建 + 7 天」；未互动过期动态删除；过期游客人物回收。
- 新增：30 天前 `daily_world` 批次行（`SE_DAILY_WORLD_KEEP_DAYS` 可配）、孤儿 `persona_posts`、
  过期且无互动/无好友的 guest（**含 waiting_voice**）最终回收；
- 返回新增统计键 `deleted_daily_world / deleted_orphan_posts / recycled_waiting_voice /
  deleted_batch_markers`，旧键不变。

### 3.3 按活跃关系与内容缺口供给

`supply_target(db)`（2–8 人）：近 7 天 accepted friendship 去重人物数为主信号
（`base = 2 + min(4, (n+1)//2)`），可见未过期帖子不足时 +1；显式 `SE_DAILY_PERSONAS`
优先（测试/运营可控）。空库冷启动仍按 `DAILY_PERSONAS` 起世界，稳态才用供给函数。

### 3.4 公共世界与个人水平解耦

`generate_daily` 不再把任何用户的 `level` 传给 `_llm_persona`（语言轮换保留）；
公共动态只由人物自身设定与语言决定，学习水平只影响私聊与学习辅助。

## 4. 接口变化（全部向后兼容）

- `GET /api/v3/feed`：item 字段不变；`next_cursor` 变成 `snap:<id>:<offset>`（旧格式仍能解析）；
  返回顺序由推荐决定。
- `GET /api/v3/profile`：新增 `interest_labels`（`{tag_id: {zh, en, known}}`），兴趣本身返回稳定 ID。
- 发帖/资料写入：`tags/interests` 存稳定 ID（未知保留 `custom:`），响应里同样是 ID。
- `GET /api/v3/discover/next`：标签匹配按 ID，不影响返回文案。

## 5. 配置项

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `SE_DAILY_PERSONAS` | 空 | 显式指定每日游客人物数（覆盖 `supply_target`） |
| `SE_DAILY_WORLD_KEEP_DAYS` | `30` | `daily_world` 批次行保留天数 |
| （快照 TTL） | 30 分钟 | `recommend.SNAPSHOT_TTL_S` |
| （探索比例） | 20% | `recommend.EXPLORE_RATIO` |

## 6. 测试证据

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe -m pytest backend/tests/test_taxonomy.py backend/tests/test_recommend.py `
  backend/tests/test_generation_recovery.py -q
# 22 passed（标签 7 + 推荐 10 + 生成恢复 5）
.\.venv\Scripts\python.exe -m pytest backend/tests -q
# 1218 passed
```

- `test_taxonomy.py`：别名归一、未知 custom 保留、多语言显示名、列表去重/上限、历史迁移幂等。
- `test_recommend.py`：快照翻页稳定、兴趣命中排序、已看降权、20% 探索位、语言过滤、本人动态置顶、
  两个用户兴趣互不影响（`_user_interests` bug 修复）、非法/过期 cursor 自动重建、`mark_seen` 幂等、
  `interest_labels`。
- `test_generation_recovery.py`：force 重跑不翻倍、中途失败重试恢复且无重复、cleanup 三类新增清理
  （互动延长不回退）、`supply_target` 三档、用户 level 不进入公共世界 prompt。
- 随语义更新的旧断言：`test_v3_world.py`（标签/兴趣改稳定 ID）、`test_onboarding_profile.py`（同上）。

## 7. 已知边界（下一轮）

1. **广告闭环**：管理台「商业运营」、聊天/动态投放、原子额度、曝光/点击事件、广告档案（方案第三期）。
2. **前端**：标签多语言展示（接口已给 `interest_labels`）、推荐流的「刷新=新快照」交互、登录/青少年页面。
3. 探索比例、半衰期、已看窗口目前是常量，后续可按留存/负反馈做成可配置实验参数。
4. 供给函数只用了 friendship 活跃度与内容缺口；后续可纳入真实聊天活跃（messages 近 7 天）做更细的供给。
