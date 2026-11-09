# SpeakEasy · Persona 真实感升级：实施计划与模块契约

> 配套：`docs/PERSONA_AUDIT.md`（审计）。本文是**施工图纸**：文件归属、函数签名、数据形状、
> 集成点、测试要求。所有实现必须严格遵守本契约，否则并行开发会互相踩。
>
> 铁律：
> 1. **Lead 独占**共享文件（见 §1），其他人只写自己的新文件 + 自己的测试文件。
> 2. 一次聊天仍然只允许**一次主 LLM 调用**；行为选择/示例检索/事件检索全部是规则 + DB。
> 3. 新表/新列全部幂等迁移（`PRAGMA table_info` + `ALTER TABLE ... DEFAULT`），旧数据零丢失。
> 4. 计划中(planned)的事件**不得**被当成已发生事实注入聊天。
> 5. 纯逻辑模块（behavior / speech_dna 的派生 / life_events 的规则层）不碰网络、可注入 rng。

---

## 1. 文件归属（write scope）

| 所有者 | 只允许写这些文件 |
|---|---|
| **Lead** | `backend/app/v3/schema.py`、`backend/app/v3/persona_context.py`、`backend/app/v2/chat_service.py`、`backend/app/v2/memory.py`、`backend/app/v3/service.py`、`backend/app/main.py`、`docs/*`、`backend/tests/test_persona_integration.py` |
| **speech-dna** | `backend/app/v3/speech_dna.py`、`backend/app/v3/seed_data.py`（只追加 DNA/示例语料）、`backend/tests/test_v3_speech_dna.py` |
| **behavior** | `backend/app/v3/behavior.py`、`backend/tests/test_v3_behavior.py` |
| **life-events** | `backend/app/v3/life_events.py`、`backend/app/v3/emotion.py`、`backend/tests/test_v3_life_events.py`、`backend/tests/test_v3_emotion.py` |
| **event-content** | `backend/app/v3/event_posts.py`、`backend/tests/test_v3_event_posts.py` |

> 任何跨文件需求：**不要动别人的文件**，在最终汇报里写明需要 Lead 做什么。
> 所有新模块从 `app.v3.*` 导入；禁止修改 `app/v2/db.py`。

---

## 2. 数据库变更（Lead 实施，其他人只需依赖）

### 2.1 新增列（全部有默认值，幂等 ALTER）

```sql
-- personas
speech_dna     TEXT NOT NULL DEFAULT '{}'   -- Speech DNA JSON
emotion_state  TEXT NOT NULL DEFAULT '{}'   -- 持续情绪状态 JSON

-- persona_posts
source_event_id TEXT NOT NULL DEFAULT ''    -- 来源 persona_events.id（可空 = 非事件驱动）
post_type       TEXT NOT NULL DEFAULT ''    -- 'life_event' | 'daily_life' | 'musing' | 'seed'
published_at    TEXT NOT NULL DEFAULT ''    -- 对外可见时间；空 = 用 created_at
metadata        TEXT NOT NULL DEFAULT '{}'  -- 事件快照等

-- persona_post_comments
author_persona_id TEXT NOT NULL DEFAULT ''  -- 非空 = 这条评论是 Persona 发的
author_type       TEXT NOT NULL DEFAULT 'user'  -- 'user' | 'persona'

-- friends（V2 表，聊天主体）
recent_behaviors TEXT NOT NULL DEFAULT '[]' -- 最近行为类型（最新在后，最多 8 条）
persona_id       TEXT NOT NULL DEFAULT ''   -- 反向指向 personas.id，激活 Persona 层
```

### 2.2 新表

```sql
CREATE TABLE IF NOT EXISTS persona_example_dialogs (
    id TEXT PRIMARY KEY,
    persona_id TEXT NOT NULL,
    scene TEXT NOT NULL,                 -- SCENES 之一
    language TEXT NOT NULL DEFAULT '',
    user_line TEXT NOT NULL,
    persona_line TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    ordinal INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ped_persona ON persona_example_dialogs(persona_id, scene, ordinal);

CREATE TABLE IF NOT EXISTS persona_events (
    id TEXT PRIMARY KEY,
    persona_id TEXT NOT NULL,                  -- 主角
    event_type TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    start_time TEXT NOT NULL,                  -- ISO8601 UTC
    end_time TEXT NOT NULL DEFAULT '',
    participants TEXT NOT NULL DEFAULT '[]',   -- JSON [persona_id]（含主角）
    location TEXT NOT NULL DEFAULT '',
    related_personas TEXT NOT NULL DEFAULT '[]',
    event_status TEXT NOT NULL DEFAULT 'planned',
    source_type TEXT NOT NULL DEFAULT 'rule',
    source_reference TEXT NOT NULL DEFAULT '',
    visibility TEXT NOT NULL DEFAULT 'private',
    metadata TEXT NOT NULL DEFAULT '{}',
    dedupe_key TEXT NOT NULL UNIQUE,
    day TEXT NOT NULL DEFAULT '',              -- 生成批次（幂等）
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pe_persona ON persona_events(persona_id, start_time DESC);
CREATE INDEX IF NOT EXISTS idx_pe_status ON persona_events(event_status, start_time);
CREATE INDEX IF NOT EXISTS idx_pe_day ON persona_events(day);

CREATE TABLE IF NOT EXISTS persona_relationships (
    id TEXT PRIMARY KEY,
    persona_a TEXT NOT NULL,
    persona_b TEXT NOT NULL,
    relation_type TEXT NOT NULL DEFAULT 'friend',
    closeness INTEGER NOT NULL DEFAULT 2,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(persona_a, persona_b)
);

CREATE TABLE IF NOT EXISTS persona_schedules (
    persona_id TEXT PRIMARY KEY,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    weekday_blocks TEXT NOT NULL DEFAULT '[]',
    weekend_blocks TEXT NOT NULL DEFAULT '[]',
    work_hours TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pp_source_event ON persona_posts(source_event_id);
```

`v3_meta` 新键：`persona_dna_version`（值 = `SPEECH_DNA_VERSION`）、`life_events_day:<day>` 用于批次幂等观测。

---

## 3. 模块 A：Speech DNA（owner: speech-dna）

文件：`backend/app/v3/speech_dna.py`

### 3.1 常量

```python
SPEECH_DNA_VERSION = "1"

SCENES: tuple[str, ...] = (
    "small_talk", "life_share", "happy", "tired", "joke", "disagree",
    "comfort", "private_question", "unsure", "short_reply", "wrap_up", "reconnect",
)

DNA_KEYS = (
    "sentence_rhythm", "directness", "humor_style", "emotional_expression",
    "vocabulary_style", "slang_usage", "emoji_usage", "verbosity",
    "conversation_initiative", "self_disclosure", "social_distance",
    "disagreement_style", "topic_transition",
)
```

### 3.2 取值域（枚举，**不要** 1-10 分）

```
sentence_rhythm          clipped | measured | flowing | fragmented
directness               blunt | direct | diplomatic | indirect
humor_style              none | dry | sarcastic | playful | absurd
emotional_expression     reserved | warm | expressive | volatile
vocabulary_style         plain | precise | literary | street | technical
slang_usage              none | rare | moderate | heavy
emoji_usage              never | rare | sometimes | often
verbosity                one_line | short | medium | long
conversation_initiative  low | medium | high
self_disclosure          guarded | measured | open
social_distance          distant | neutral | familiar | intimate
disagreement_style       avoid | soften | state_plainly | challenge
topic_transition         stay | gentle_shift | abrupt_shift | callback
```

附加可选键（同样纳入 `normalize_dna`，缺省给空）：
`signature_phrases: list[str]`（人物口头禅，≤4）、`banned_phrases: list[str]`（该人物绝不会说的话，≤4）、
`punctuation_habit: str`（自由短语）、`note: str`（一句人设补充）。

### 3.3 函数契约

```python
def normalize_dna(value: dict | str | None) -> dict
    """任意脏输入 → 完整合法 DNA（缺字段用 derive 默认；非法枚举回落到默认）。"""

def derive_dna(persona: dict) -> dict
    """从 persona 行（nickname/personality/bio/interests/occupation/archetype_id/ai_config）
    确定性推导 DNA。同输入必同输出（用 persona id 的 sha256 做细微平局抖动）。
    必须让 3 个不同人物得到**可区分**的结果：至少 humor/verbosity/directness 三项不同。"""

def dna_traits(dna: dict) -> dict[str, float]
    """→ {'initiative','warmth','humour','directness','verbosity','disclosure',
          'emoji','slang','playfulness','social_distance'} 全部 0..1。
     behavior.py 只依赖这些数值。"""

def dna_prompt_block(dna: dict, *, language: str = "en", max_chars: int = 900) -> str
    """紧凑英文指令块（中文语言时仍用英文元语言描述，因为 LLM 指令用英文最稳）。
    必须包含：节奏/长度倾向/幽默/直接度/情绪表达/emoji/主动提问倾向/自我暴露/分歧方式/
    话题转换/signature_phrases；并明确写 a) 这是**倾向不是规则** b) 不强制每轮提问
    c) 允许一句话回复 d) 允许不同意。"""

def scene_for_context(*, user_text: str, behavior: str, dna: dict) -> str
    """用户文本 + 本轮行为 → SCENES 之一（用于示例检索）。"""

def example_bank(persona: dict, dna: dict) -> list[dict]
    """确定性兜底示例：12 个 SCENES 各 1 组 {scene,user_line,persona_line,note}。
    必须使用 persona 的 nickname/occupation/city/interests 和 DNA 做差异化，
    不能所有人物返回值相同（测试会断言）。"""

def pick_examples(rows, *, scene: str, context_text: str = "", k: int = 2,
                  rng: random.Random | None = None) -> list[dict]
    """从 DB 行里挑最相关的 ≤k 组：同 scene 优先 → 词面重合度 → ordinal 稳定排序。"""

def example_block(rows, *, language: str, max_chars: int = 700) -> str
    """把挑出的示例拼成 prompt 片段。必须带一句 "imitate the VOICE, never copy the lines"。"""

def dna_from_persona_row(row) -> dict
    """sqlite Row/dict → DNA（解析 speech_dna JSON，非法则 derive_dna）。"""

def load_examples(db, persona_id: str) -> list[dict]
    """读 persona_example_dialogs。"""

def save_examples(db, persona_id: str, rows: list[dict]) -> int
    """整体替换该 persona 的示例（先 DELETE 再 INSERT，事务内）。"""

async def ensure_persona_dna(db, llm, personas: list[dict] | None = None, *,
                             force: bool = False, use_llm: bool | None = None) -> dict
    """幂等补齐：每个 persona 的 speech_dna + 12 组示例。
    - DB 已有合法 DNA 且示例数 ≥ 8 且非 force → 跳过
    - persona 行带 'speech_dna' 键（seed 语料）→ 用它
    - 否则 derive_dna
    - 示例：use_llm（默认读 env SE_DNA_LLM，缺省 "1"）且 llm 可用 → 一次 LLM 调用生成 12 组，
      校验失败/超时 → 回落 example_bank；否则直接用 example_bank
    - 返回 {'dna': n, 'examples': n, 'llm': bool}
    永不抛异常（LLM 失败内部消化）。"""
```

### 3.4 seed 语料扩展（同 owner，文件 `backend/app/v3/seed_data.py`）

给 `PERSONAS` 的**每个** dict 增加：

```python
"speech_dna": { ...13 个键全填，带 signature_phrases/banned_phrases... },
```

12 位人物必须彼此明显不同（例如 Adrian=blunt/dry/clipped/one_line；Callum=sarcastic/playful/medium；
Lin=precise/diplomatic/measured…）。**只追加键，不要改已有字段的值**（会破坏既有语料测试）。

### 3.5 测试（`backend/tests/test_v3_speech_dna.py`）

- `normalize_dna` 对 `{}` / `"{}"` / 非法枚举 / 缺键 → 都返回 13 键齐全的合法 dict。
- `derive_dna` 确定性（同输入两次相等）+ 12 个种子人物两两之间至少 3 个字段不同。
- `dna_traits` 全部 0..1。
- `dna_prompt_block` 含「不强制提问」「允许一句话」等关键约束词。
- `example_bank` 对不同 persona 输出不同（比较 user_line 集合）。
- `pick_examples`：同 scene 优先；k 上限；rng 固定可复现。
- `ensure_persona_dna`：空库 → 写入 12 条/人；再跑一次不重复插入（幂等）；mock LLM 下走兜底。

---

## 4. 模块 B：Behavior Engine（owner: behavior）

文件：`backend/app/v3/behavior.py`（**纯逻辑，禁止 import db/llm/httpx**）

### 4.1 常量

```python
BEHAVIORS: tuple[str, ...] = (
    "respond", "share", "joke", "tease", "empathize", "comfort", "disagree",
    "complain", "celebrate", "tell_story", "change_topic", "acknowledge",
    "end_conversation", "ask_question",
)

INTENTS: tuple[str, ...] = (
    "greeting", "question", "good_news", "bad_news", "opinion_probe",
    "small_talk", "closing", "silence", "unknown",
)
```

### 4.2 数据结构

```python
@dataclass(frozen=True)
class BehaviorInput:
    dna_traits: dict[str, float]          # 来自 speech_dna.dna_traits()
    user_text: str = ""
    recent_behaviors: tuple[str, ...] = ()  # 最新在后
    closeness: int = 1                     # 1..5
    mood: dict | None = None               # {'mood','energy','stress','social_battery'}
    turn_index: int = 0
    is_first_turn: bool = False
    has_due_open_loop: bool = False
    current_activity: str = ""             # 例 "at work" / "on the bus"
    time_period: str = ""                  # 来自 timeutil.local_state()['period']

@dataclass(frozen=True)
class BehaviorChoice:
    behavior: str
    reason: str
    confidence: float
    weights: dict[str, float]              # 归一化后（测试/调试用）
```

### 4.3 函数契约

```python
def detect_intent(user_text: str) -> str
def choose_behavior(inp: BehaviorInput, *, rng: random.Random | None = None) -> BehaviorChoice
def behavior_directive(choice: BehaviorChoice, *, dna_traits: dict[str, float],
                       language: str = "en") -> str
def behavior_history_penalty(recent: tuple[str, ...] | list[str], behavior: str) -> float
```

`choose_behavior` 规则（必须实现，测试会验证）：
- 基础权重由 `dna_traits` 决定：`share`/`tell_story` 随 disclosure↑；`joke`/`tease` 随 humour↑；
  `ask_question` 随 initiative↑ 且**上限受 initiative 约束**；`empathize`/`comfort` 随 warmth↑；
  `disagree` 随 directness↑ 且 `disagreement_style=="avoid"` 时权重≈0.02；`acknowledge` 随 verbosity↓。
- intent 乘数：`bad_news`→empathize/comfort/respond↑，joke/tease/celebrate↓；
  `good_news`→celebrate/respond↑；`opinion_probe`→disagree/respond↑；`closing`→end_conversation/acknowledge↑；
  `greeting`→respond/greet-like(acknowledge)；`silence`→acknowledge/share↑。
- mood：`energy<0.35` → joke/celebrate/ask_question↓，acknowledge/complain↑；
  `stress>0.65` → complain/disagree↑，joke↓；`social_battery<0.3` → end_conversation/acknowledge↑。
- 历史惩罚：`behavior_history_penalty` = 最近 3 轮里出现次数的衰减惩罚
  `0.35 ** recency`；`ask_question` 额外惩罚（最近 2 轮出现过 → ×0.15）。
- `end_conversation` 只在 intent∈{closing, silence} 或 social_battery<0.25 时权重放大，其余情况低但非 0。
- `respond` 永远保留权重下限（不会被压到 0）。
- 权重归一化后抽样；`rng=None` 用 `random.Random()`。
- **不得**出现「每轮 ask_question」：连续两次 ask_question 后第三次概率 < 5%。

`behavior_directive` 输出英文指令，包含：
- 本轮行为标签 + 一句具体动作要求（例如 share → "tell them one concrete thing that happened to you today"）。
- **明确写**：只有 `ask_question` 才要求提问；其他行为写 "Do not end with a question this turn unless they asked you one."
- 长度提示来自 `verbosity`（one_line → "one short line, no more than 8 words"；long → "3-4 sentences is fine"）。
- `end_conversation` → "wrap this topic up naturally; a short sign-off is fine; do not ask a question."
- `acknowledge` → "a very short reaction is enough; one or two words can be perfect."

### 4.4 测试（`backend/tests/test_v3_behavior.py`）

- `detect_intent` 对 8 类示例文本正确。
- 用 `random.Random(42)` 抽样 500 次：高 initiative 人物 ask_question 占比显著高于低 initiative（≥2x）。
- humour 高 → joke+tease 占比高于 humour 低的人（≥1.5x）。
- bad_news 下 empathize+comfort+respond 合计 > 60%。
- closing 下 end_conversation+acknowledge 明显上升。
- 最近连续 ask_question → 下一轮 ask_question 概率 < 5%。
- 低 energy/高 stress 时 complain/acknowledge 上升、celebrate 下降。
- `behavior_directive`：ask_question 的指令含 "question"，其它行为含 "Do not end with a question"。
- 权重归一化：`sum(weights.values()) ≈ 1.0`，`respond > 0`。

---

## 5. 模块 C：Life Events + Emotion（owner: life-events）

### 5.1 `backend/app/v3/life_events.py`

```python
EVENT_TYPES = ("daily_routine","work","study","meal","entertainment","travel",
               "social","relationship","achievement","conflict","personal_milestone",
               "unexpected_event")
EVENT_STATUSES = ("planned","ongoing","completed","cancelled")
OPEN_STATUSES = ("planned","ongoing")
DONE_STATUSES = ("completed",)

def build_schedule(persona: dict) -> dict
    """按职业/年龄/性格/时区确定性生成作息：
    weekday_blocks/weekend_blocks = [{"start":"09:00","end":"12:00","kind":"work","label":"..."}]
    work_hours = {"start":"09:30","end":"18:00","days":[1,2,3,4,5]}
    不同职业差异必须明显：学生有课/社团、设计师有项目/客户、自由职业不规律、
    咖啡店主/服务行业早晚班。"""

def ensure_schedule(db, persona_id: str) -> dict
    """读/建 persona_schedules（幂等）。"""

def rule_events_for_day(persona: dict, day: str, *, now=None) -> list[dict]
    """规则事件（纯函数）：当天作息块 → planned 事件（work/study/meal/daily_routine）。
    返回未落库的 dict 列表，键见 §5.3。"""

def detect_conflicts(events: list[dict], persona_ids: list[str],
                     start: str, end: str) -> list[dict]
    """返回与 [start,end) 冲突的既有事件（同一 persona 时间重叠）。纯函数。"""

async def generate_narrative_events(db, llm, persona: dict, day: str, *,
                                    rng: random.Random | None = None,
                                    max_events: int = 2) -> list[dict]
    """LLM 生成 0..max_events 条「有叙事价值」的事件（achievement/conflict/social/
    unexpected_event/entertainment/travel…）。必须约束：符合职业/城市/兴趣；
    不与其他人物冲突；时间落在 day 内；参与者必须来自 persona_relationships 里的熟人。
    LLM 不可用/失败 → 返回 []（上层用规则事件兜底）。"""

def add_event(db, event: dict) -> tuple[str, bool]
    """幂等写入（dedupe_key 冲突返回 (existing_id, False)）。返回 (id, created)。"""

def refresh_statuses(db, *, now=None) -> int
    """按 start/end 推进状态：planned→ongoing→completed；
    超过 end + 6h 仍 ongoing 的也标 completed；返回变更行数。"""

def events_for_persona(db, persona_id: str, *, statuses=None,
                       since_days: int = 7, limit: int = 6) -> list[dict]
def current_activity(db, persona_id: str, *, now=None) -> dict | None
    """正在发生(ongoing)的事件；没有则从作息推断（不写库）。"""
def relevant_events(db, persona_id: str, query_text: str, *, limit: int = 3) -> list[dict]
    """相关性检索：只返回 **completed** 的事件（已发生事实），
    按 title/description/location/tags 与 query 的词面重合 + 时间衰减排序。"""
def upcoming_events(db, persona_id: str, *, limit: int = 2) -> list[dict]
    """planned 且未开始的最近事件（只能作为「计划」注入，不能当已发生）。"""
def event_fact_block(event: dict, *, include_status: bool = True) -> str
    """单条事件 → prompt 用的一行事实。
    planned → 必须写成 "PLAN (not happened yet): ..."；completed → "DONE: ..."；
    cancelled → 不返回（调用方过滤）。"""
async def generate_day_events(db, llm, *, day: str | None = None,
                              persona_ids: list[str] | None = None,
                              force: bool = False) -> dict
    """批量：给所有 permanent persona 生成当天事件（规则 + LLM）。
    幂等：同 day 已生成过（v3_meta 'life_events_day:<day>'）且非 force → 跳过。
    返回 {'day','events','created','source'}。"""
async def life_loop(db, llm, *, tick_s: float = 300.0) -> None
    """后台循环：启动先 refresh_statuses + ensure 今天事件；
    之后每 tick：refresh_statuses；跨天则生成新一天。"""
```

### 5.2 `backend/app/v3/emotion.py`

```python
def default_state(persona: dict) -> dict
    """{'mood': str, 'energy': 0..1, 'stress': 0..1, 'social_battery': 0..1,
        'emotional_context': str, 'updated_at': iso}"""

def normalize_state(value) -> dict           # 脏输入 → 合法状态
def decay(state: dict, *, hours: float) -> dict   # 向基线缓慢回归（不是数值游戏）
def apply_event(state: dict, event: dict) -> dict  # 事件改变状态（幅度小、有上限）
def apply_chat(state: dict, *, intent: str, behavior: str) -> dict
def emotion_prompt_block(state: dict, *, language: str = "en") -> str
```

情绪规则要求：变化幅度小（单事件 ≤0.15），有上下限，`emotional_context` 是一句英文短语
（例如 "a bit drained after a long meeting"），**不展示数字给用户**。

### 5.3 事件 dict 形状（跨模块统一）

```python
{
  "persona_id": str, "event_type": str, "title": str, "description": str,
  "start_time": iso, "end_time": iso, "participants": [pid, ...],
  "location": str, "related_personas": [pid, ...],
  "event_status": "planned", "source_type": "rule"|"llm", "source_reference": str,
  "visibility": "private"|"public", "metadata": {}, "dedupe_key": str, "day": "YYYY-MM-DD",
}
```

`dedupe_key` 规范：`f"{persona_id}:{event_type}:{start_time[:13]}:{slug(title)}"`（规则事件）
与 `f"{persona_id}:{event_type}:{day}:{n}"`（LLM 事件）。

### 5.4 测试

`test_v3_life_events.py`：建表、作息差异化（设计师 vs 学生 vs 自由职业至少 2 个时段不同）、
规则事件落库、`add_event` 幂等（同 dedupe_key 第二次 created=False）、
`refresh_statuses` 的 planned→ongoing→completed、取消后不再出现在 `relevant_events`、
冲突检测、`relevant_events` 只返回 completed、`event_fact_block` 区分 PLAN/DONE、
`generate_day_events` 幂等。

`test_v3_emotion.py`：默认值、decay 回落、单事件变化 ≤0.15、边界钳制、prompt block 不含数字。

---

## 6. 模块 D：Event-driven Social Content（owner: event-content）

文件：`backend/app/v3/event_posts.py`

```python
def share_probability(persona: dict, dna_traits: dict, event: dict) -> float
    """0..1。由 self_disclosure、social_distance、事件类型（achievement/conflict 更高，
    daily_routine 更低）、事件是否 public 决定。"""

def should_post(*, persona, dna_traits, event, recent_post_count: int,
                rng: random.Random | None = None) -> bool
    """概率 + 冷却（同一 persona 24h 内 ≥3 条 → 概率大降）。
    **允许不发布**：daily_routine/meal 类默认很低。"""

async def generate_post_for_participant(db, llm, event: dict, persona: dict,
                                        *, rng=None, force=False) -> dict | None
    """为单个参与者生成一条动态：
    - 事实必须来自 event（title/description/location/participants/时间），**不得新增事实**
    - 用该 persona 的 speech_dna 声线写（persona['speech_dna'] 或 dna_prompt_block）
    - 母语书写
    - 写入 persona_posts：source_event_id=event.id, post_type='life_event',
      published_at=now, metadata={'event_title':..., 'event_type':...}
    - 同一 (event_id, persona_id) 已存在 → 直接返回已有（幂等）
    - LLM 失败 → 返回 None（不写库，不编造）"""

async def generate_event_posts(db, llm, event: dict, *, rng=None) -> dict
    """多参与者不同视角：对每个 participant 独立 should_post → 生成。
    返回 {'posts': [...], 'skipped': [...]}。"""

async def maybe_persona_comment(db, llm, post: dict, event: dict,
                                commenter: dict, *, rng=None) -> dict | None
    """另一个 Persona 评论这条动态（写入 persona_post_comments，
    author_persona_id/author_type='persona'）。
    幂等：同一 (post_id, commenter_persona_id) 只允许一条。关系必须存在。"""

def posts_for_event(db, event_id: str) -> list[dict]
async def run_posts_cycle(db, llm, *, day: str | None = None, max_events: int = 6) -> dict
    """批处理：取最近的 completed 且「还没生成过动态」的事件 → 生成动态 + 评论。
    幂等：以 source_event_id 是否已有动态为准。返回统计。"""

def persona_recent_facts(db, persona_id: str, query_text: str = "", *, limit: int = 3) -> str
    """给聊天用的**已发生**事实块：
    - 最近 completed 事件（relevant_events）
    - 自己发过的动态（含 source_event_id 的 + 普通动态，取最近 2 条）
    - 别人对自己动态的评论（最近 1-2 条）
    - 可选的 planned 事件（写成 "plan, not happened yet"）
    返回空串表示没有任何相关内容。**不得**包含 cancelled 事件。"""
```

### 6.2 测试（`backend/tests/test_v3_event_posts.py`）

用确定性 mock LLM（返回固定 JSON）：
- `share_probability` 随 disclosure 单调上升；daily_routine 低于 achievement。
- `should_post` 用固定 rng 可复现；recent_post_count 高 → 拒绝。
- `generate_post_for_participant` 幂等（同 event+persona 第二次不新增行）。
- **核心验收**：1 个事件 + 2 个参与者 → 2 条动态，`source_event_id` 相同，
  metadata 里事件标题一致，两条正文不同（不同视角）。
- 评论幂等：同一评论者第二次不新增。
- `run_posts_cycle` 第二次运行新增 0 条（后台不重复）。
- `persona_recent_facts` 不包含 cancelled 事件；planned 事件带 "not happened yet"。

---

## 7. 集成层（Lead）

### 7.1 `backend/app/v3/persona_context.py`（Lead 新文件）

```python
def persona_id_for_friend(db, friend: dict) -> str
    """friends.persona_id 优先；否则用 friendships(friend_id) 反查；否则 ''。"""

def build_turn_plan(db, friend: dict, user_text: str, *, turn_index: int = 0) -> "TurnPlan"
    """计算本轮：persona 行、DNA、traits、emotion、behavior choice、scene、示例、
    生活事实块。返回 TurnPlan dataclass（供 prompt 组装与测试断言）。"""

def build_persona_layer(db, friend: dict, plan, *, language: str) -> str
    """拼装注入 memory.build_context 之外的 Persona 层：
    [Speech DNA] + [This turn] + [Your life right now] + [Recent true facts] + [Examples]
    总长度上限 ~2600 字符（硬截断，保序）。"""

def record_behavior(db, friend: dict, behavior: str) -> list[str]
    """把 behavior 记进 friends.recent_behaviors（最多 8 条）。返回新列表。"""
```

### 7.2 集成点（Lead 修改）

| 文件 | 改动 |
|---|---|
| `chat_service._system_prompt` | 增加 `user_text`/`plan` 参数；注入 `build_persona_layer`；**删除** "Always leave a hook"、"Reply with 1-2 VERY short lines"、"easy way in" 等强制规则，改为由 behavior directive 决定 |
| `chat_service._llm_reply` | 生成前 `build_turn_plan`，生成后 `record_behavior`；仅语言纠偏时才二次调用（保持） |
| `chat_service._fallback_reply` / `_mock_reply` | 改为 DNA-aware、无客服句式；去掉 "That sounds interesting. Tell me more about it?" |
| `memory.build_context` | 删除 `[Rules] ... end with a hook` 与 "1-2 short spoken English sentences" 硬约束，改为「跟随 system 里的 This-turn 指令」 |
| `service._friend_row_for_persona` | 写入 `persona_id`、`speech_dna`（无列则写进 personality_prompt 尾部不必要——直接写 friends.persona_id 即可），并同步 emotion 摘要 |
| `service._persona_post_public` | 评论按 `author_type` 输出 AI 作者（`is_ai: True`），并返回 `source_event_id`/`post_type` |
| `main.py` | 启动时 `ensure_persona_dna`（后台 task）+ 启动 `life_loop` + 启动 `run_posts_cycle` 的周期任务（挂在 life_loop 里或单独 task） |
| `schema.py` | 上述 DDL + 迁移 + `seed_if_needed` 写入 `speech_dna` |

### 7.3 LLM 调用预算（硬约束）

| 场景 | 调用数 |
|---|---|
| 普通一轮聊天 | **1**（语言纠偏才 +1） |
| 每个 persona 一次性 DNA/示例生成 | 1（仅首次，seed 语料可直接跳过用内置 DNA） |
| 每天每 persona 叙事事件 | ≤1（`SE_LIFE_LLM=1` 时；默认按人物数量批处理，5-12 人/天） |
| 每条已完成事件 | ≤参与者数（每条动态 1 次） |
| 每条事件动态的 AI 评论 | ≤1 |
| 刷新信息流 | **0** |

后台任务必须幂等（`daily_world.day`、`persona_events.day`、`source_event_id` 唯一性、`dedupe_key`）。

---

## 8. 开发顺序与验收门

1. **Lead**：schema.py 全部 DDL + 迁移 + 跑 `pytest test_v3_*`（必须仍绿）→ 通知全员。
2. **并行**：speech-dna / behavior / life-events / event-content 各自实现 + 自己的测试。
3. **Lead**：persona_context.py + chat_service/memory/service/main 集成。
4. **全员**：各自模块测试绿；Lead 跑全量 `pytest backend/tests -q` 对比基线（原 6 个历史失败）。
5. **Lead + QA**：本地启动 `http://localhost:8000`，真浏览器验收（信息流 AI 评论、聊天不每句追问、
   提到事件、多视角动态）；写 `docs/PERSONA_ACCEPTANCE.md`。
6. 汇报时严格区分：已实现 / 已通过自动测试 / 已在本地 Web 验证 / 未完成。
