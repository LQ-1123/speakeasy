"""Persona 生活层：把「人物自己」接进聊天链路。

职责边界
--------
- :mod:`app.v2.memory` 负责 **Persona ↔ 这个用户** 的记忆（fact sheet / shared episodes /
  open loops / 熟稔度）。它已经很好，本模块**不重建它**。
- 本模块负责 **Persona 自己**：说话方式（Speech DNA）、这一轮想干什么（Behavior）、
  此刻的生活与情绪（Life Events / Emotion）、最近真实发生过的事（事件与动态）。

拼在一起的效果：人物不再只是「对用户做出反应」，而是有稳定的表达习惯、有自己的日程、
有可以主动分享的经历，并且不会每句话都追问用户。

设计约束
--------
1. 一轮聊天**不增加 LLM 调用**：全部是规则 + SQLite 查询。
2. 计划中(planned)的事件只能以「未来计划」出现，已完成(completed)的才能当过去经历。
3. 任何查询失败都降级为「没有这段信息」，绝不阻断聊天主链路。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.logging_utils import get_logger
from app.v3 import behavior as behavior_mod
from app.v3 import emotion as emotion_mod
from app.v3 import event_posts as event_posts_mod
from app.v3 import life_events as life_events_mod
from app.v3 import privacy as privacy_mod
from app.v3 import speech_dna as speech_mod
from app.v3.identity import current_user_id
from app.v2 import social as social_mod

log = get_logger(__name__)

#: Persona 层总字符预算（超出时按优先级裁剪：示例 → 生活事实 → 其余不动）
MAX_LAYER_CHARS = 2600
#: 每轮注入的示例对话条数（必须少，避免把示例变成固定台词）
EXAMPLE_K = 2
#: friends.recent_behaviors 保留条数
MAX_RECENT_BEHAVIORS = 8


def _loads(raw: Any, default: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if raw is None or raw == "":
        return default
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


#: 非拉丁文字的脚本特征：用来挡「英语兜底示例被注入到中日韩人物」的语言污染
_SCRIPT_RES: dict[str, re.Pattern[str]] = {
    "ja": re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]"),
    "ko": re.compile(r"[\uac00-\ud7af]"),
    "zh": re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]"),
}


def _examples_for_language(examples: list[dict], language: str) -> list[dict]:
    """只保留真正是该语言的示例（语言防污染闸门）。

    V3 的确定性兜底示例库已按 9 种语言分别撰写（speech_dna._LOCALE_PERSONA_LINES），
    正常情况下 12/12 全部保留；这里作为**防御层**存在：如果示例来源发生回归
    （例如老库里的英文示例、或未来新增语言暂无语料），宁可这轮不注入示例，
    也不要把英文示例喂给中日韩人物——那会把人物语言带跑，并触发一次多余的纠偏调用。
    """
    pattern = _SCRIPT_RES.get(str(language or "en").strip().lower())
    if pattern is None:
        return examples
    kept: list[dict] = []
    for item in examples:
        body = f"{item.get('persona_line') or ''} {item.get('user_line') or ''}"
        if pattern.search(body):
            kept.append(item)
    return kept


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Any) -> datetime | None:
    try:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("+00:00"):
            return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S+00:00").replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


# --------------------------------------------------------------------------- #
# 身份解析
# --------------------------------------------------------------------------- #
def persona_id_for_friend(db: Any, friend: dict) -> str:
    """好友行 → Persona id。

    优先用 ``friends.persona_id``（新库直接有）；老库回退到 ``friendships`` 反查。
    """
    friend = friend if isinstance(friend, dict) else {}
    pid = str(friend.get("persona_id") or "").strip()
    if pid:
        return pid
    fid = str(friend.get("id") or "").strip()
    if not fid or db is None:
        return ""
    try:
        with db._lock:  # noqa: SLF001 - 与 DB 层共用连接
            row = db.conn.execute(
                "SELECT persona_id FROM friendships WHERE friend_id=? LIMIT 1", (fid,)
            ).fetchone()
        return str(row["persona_id"]) if row is not None else ""
    except Exception as exc:  # noqa: BLE001 - 老库没有 friendships 表
        log.debug("persona_id_for_friend 查询失败（忽略）: %s", exc)
        return ""


def _persona_row(db: Any, persona_id: str) -> dict | None:
    if db is None or not persona_id:
        return None
    try:
        with db._lock:  # noqa: SLF001
            row = db.conn.execute("SELECT * FROM personas WHERE id=?", (persona_id,)).fetchone()
        return dict(row) if row is not None else None
    except Exception as exc:  # noqa: BLE001
        log.debug("读 persona 行失败（忽略）: %s", exc)
        return None


def _friend_as_persona(friend: dict, persona_id: str) -> dict:
    """没有 personas 行时的降级人设（老好友 / 单测场景）。"""
    return {
        "id": persona_id or str(friend.get("id") or ""),
        "nickname": friend.get("name") or "Friend",
        "personality": friend.get("personality_prompt") or "",
        "bio": friend.get("bio") or "",
        "occupation": friend.get("occupation") or "",
        "city": friend.get("city") or "",
        "country": friend.get("nationality") or "",
        "language": friend.get("reply_language") or friend.get("native_lang") or "en",
        "age": friend.get("age"),
        "interests": [],
        "ai_config": {},
    }


# --------------------------------------------------------------------------- #
# 多用户：当前用户 & 情绪分层（关系情绪 → 公共基线）
# --------------------------------------------------------------------------- #
def resolve_user_id(user_id: str | None = None) -> str:
    """本轮情绪归属的用户。

    - 显式传入的 ``user_id`` 优先（后台任务 / 测试可以钉死用户）；
    - 否则读 :mod:`app.v3.identity` 的 ContextVar。``asyncio.to_thread`` 与
      ``create_task`` 都会复制 contextvars，所以聊天线程里同样拿得到请求用户；
    - 没有身份头的本地单用户模式恒为 ``"local"``（与老库数据 owner 一致）。
    """
    explicit = str(user_id or "").strip()
    if explicit:
        return explicit
    try:
        return current_user_id()
    except Exception:  # noqa: BLE001 - 身份上下文异常退化为本地用户
        return "local"


def _emotion_for_turn(
    db: Any,
    persona: dict,
    row: dict | None,
    persona_id: str,
    user_id: str,
    moment: datetime,
) -> dict:
    """本轮情绪的完整读法（多用户隔离的关键）：

    1. **关系情绪优先**：``persona_user_emotion`` 里「当前用户 × 该人物」的记录，
       这是这个用户与这个人物私聊累积出来的私有状态；
    2. **没有记录 → 公共基线**：``personas.emotion_state``（只由世界事件写入）经时间
       衰减；老库/新人物还没有公共情绪时，用 :func:`app.v3.emotion.default_state`
       按人设生成确定性初始状态（比裸基线更贴人物气质）。

    两条路径都会按 ``updated_at`` 到现在的小时数做衰减，和升级前读公共情绪的节奏一致。
    """
    relation: dict = {}
    if db is not None and persona_id and user_id:
        try:
            relation = privacy_mod.get_user_persona_emotion(db, user_id, persona_id)
        except Exception as exc:  # noqa: BLE001 - 隐私存储失败就回退公共基线
            log.debug("读关系情绪失败（回退公共基线）: %s", exc)
            relation = {}

    if relation:
        state = emotion_mod.normalize_state(relation)
    else:
        raw = _loads(persona.get("emotion_state"), {}) if row is not None else {}
        state = emotion_mod.normalize_state(raw) if raw else emotion_mod.default_state(persona)

    updated = _parse_iso(state.get("updated_at"))
    if updated is not None:
        hours = max(0.0, (moment - updated).total_seconds() / 3600.0)
        if hours >= 0.5:
            state = emotion_mod.decay(state, hours=hours)
    return state


# --------------------------------------------------------------------------- #
# 一轮对话的计划
# --------------------------------------------------------------------------- #
@dataclass
class TurnPlan:
    """本轮聊天要注入的全部 Persona 侧信息（可测试、可打印）。"""

    persona_id: str = ""
    user_id: str = ""
    persona: dict = field(default_factory=dict)
    dna: dict = field(default_factory=dict)
    dna_traits: dict = field(default_factory=dict)
    emotion: dict = field(default_factory=dict)
    behavior: str = "respond"
    behavior_reason: str = ""
    behavior_confidence: float = 0.0
    directive: str = ""
    scene: str = "small_talk"
    examples: list[dict] = field(default_factory=list)
    life_block: str = ""
    activity: dict | None = None
    language: str = "en"
    degraded: bool = False


def _recent_behaviors(friend: dict) -> list[str]:
    raw = _loads(friend.get("recent_behaviors"), [])
    return [str(x) for x in raw if str(x).strip()][-MAX_RECENT_BEHAVIORS:]


def _closeness(friend: dict) -> int:
    for key, default in (("closeness_level", 1),):
        try:
            value = int(friend.get(key) or default)
        except (TypeError, ValueError):
            value = default
        return max(1, min(5, value))
    return 1


def build_turn_plan(
    db: Any,
    friend: dict,
    user_text: str,
    *,
    turn_index: int = 0,
    rng: Any = None,
    now: datetime | None = None,
    include_examples: bool = True,
    user_id: str | None = None,
) -> TurnPlan:
    """计算本轮：人设、DNA、情绪、行为、示例、生活事实。**永不抛异常**。

    ``include_examples=False`` 用于语音通话（示例会拉高 TTFT，通话只要声音像本人）。
    ``user_id`` 显式给出时优先，否则取 :func:`app.v3.identity.current_user_id`
    （聊天链路默认走后者，实现「A 的私聊情绪不影响 B」）。
    """
    friend = friend if isinstance(friend, dict) else {}
    moment = now or _now()
    persona_id = persona_id_for_friend(db, friend)
    row = _persona_row(db, persona_id)
    persona = row if row is not None else _friend_as_persona(friend, persona_id)
    uid = resolve_user_id(user_id)

    language = str(
        friend.get("reply_language") or friend.get("native_lang")
        or persona.get("language") or "en"
    ).strip().lower() or "en"

    plan = TurnPlan(persona_id=persona_id, user_id=uid, persona=persona, language=language)

    # --- Speech DNA ---
    try:
        plan.dna = speech_mod.dna_from_persona_row(persona)
        plan.dna_traits = speech_mod.dna_traits(plan.dna)
    except Exception as exc:  # noqa: BLE001
        log.warning("Speech DNA 解析失败（用默认）: %s: %s", type(exc).__name__, exc)
        plan.dna = {}
        plan.dna_traits = {
            "initiative": 0.5, "warmth": 0.5, "humour": 0.4, "directness": 0.5,
            "verbosity": 0.4, "disclosure": 0.4, "emoji": 0.2, "slang": 0.3,
            "playfulness": 0.4, "social_distance": 0.5,
        }
        plan.degraded = True

    # --- 情绪（关系情绪优先 → 公共基线；读 → 按时间衰减） ---
    try:
        plan.emotion = _emotion_for_turn(db, persona, row, persona_id, uid, moment)
    except Exception as exc:  # noqa: BLE001
        log.debug("情绪状态解析失败（忽略）: %s", exc)
        plan.emotion = {}

    # --- 当前生活 ---
    try:
        plan.activity = life_events_mod.current_activity(db, persona_id, now=moment) if persona_id else None
    except Exception as exc:  # noqa: BLE001
        log.debug("current_activity 失败（忽略）: %s", exc)
        plan.activity = None

    # --- 行为选择（纯规则，零 LLM） ---
    try:
        time_period = ""
        try:
            from app.v2 import timeutil  # noqa: PLC0415

            st = timeutil.local_state(str(persona.get("timezone") or friend.get("timezone") or "UTC"), moment)
            time_period = str(st.get("period") or "")
        except Exception:  # noqa: BLE001
            time_period = ""

        choice = behavior_mod.choose_behavior(
            behavior_mod.BehaviorInput(
                dna_traits=plan.dna_traits,
                user_text=str(user_text or ""),
                recent_behaviors=tuple(_recent_behaviors(friend)),
                closeness=_closeness(friend),
                mood=plan.emotion,
                turn_index=max(0, int(turn_index or 0)),
                is_first_turn=max(0, int(turn_index or 0)) == 0,
                current_activity=str((plan.activity or {}).get("label") or ""),
                time_period=time_period,
                # 该人物「意见不同」的惯用方式（avoid 时几乎不会顶撞用户）
                disagreement_style=str((plan.dna or {}).get("disagreement_style") or ""),
            ),
            rng=rng,
        )
        plan.behavior = choice.behavior
        plan.behavior_reason = choice.reason
        plan.behavior_confidence = float(choice.confidence)
        plan.directive = behavior_mod.behavior_directive(
            choice, dna_traits=plan.dna_traits, language=language
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("行为选择失败（退化为正常回应）: %s: %s", type(exc).__name__, exc)
        plan.behavior = "respond"
        plan.directive = ""
        plan.degraded = True

    # --- 示例对话（按情境挑 1-2 组） ---
    try:
        plan.scene = speech_mod.scene_for_context(
            user_text=str(user_text or ""), behavior=plan.behavior, dna=plan.dna
        )
        if include_examples and persona_id:
            rows = speech_mod.load_examples(db, persona_id)
        else:
            rows = []
        if rows:
            plan.examples = speech_mod.pick_examples(
                rows, scene=plan.scene, context_text=str(user_text or ""), k=EXAMPLE_K, rng=rng
            )
            plan.examples = _examples_for_language(plan.examples, language)
    except Exception as exc:  # noqa: BLE001
        log.debug("示例对话检索失败（忽略）: %s", exc)
        plan.examples = []

    # --- 生活事实（已完成事件 / 自己的动态 / 相关评论 / 未来计划） ---
    if persona_id:
        try:
            plan.life_block = event_posts_mod.persona_recent_facts(
                db, persona_id, str(user_text or ""), limit=3
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("生活事实检索失败（忽略）: %s", exc)
            plan.life_block = ""

    return plan


def build_persona_layer(db: Any, friend: dict, plan: TurnPlan, *, language: str = "") -> str:
    """把 TurnPlan 拼成 system prompt 的一段。

    顺序有讲究：**行为指令放最后**（模型对末尾指令最敏感），示例放在它前面，
    因为「像谁在说话」比「说什么」更靠后也更容易被模仿。
    超长时先丢示例，再丢生活事实，DNA 与行为指令永不裁剪。
    """
    lang = language or plan.language or "en"
    dna_block = ""
    try:
        dna_block = speech_mod.dna_prompt_block(plan.dna, language=lang)
    except Exception as exc:  # noqa: BLE001
        log.debug("dna_prompt_block 失败（忽略）: %s", exc)

    life_lines: list[str] = []
    activity = plan.activity or {}
    now_line = _life_now_line(activity, moment=_now())
    if now_line:
        life_lines.append(now_line)
    # 你身边的人（关系深度 / 张力）：比事件更稳定，放在事件事实前面
    people = _people_lines(db, plan.persona_id)
    if people:
        life_lines.append("[Your people — only these relations are true]")
        life_lines.extend(people)
    if str(plan.life_block or "").strip():
        life_lines.append(plan.life_block.strip())

    example_block = ""
    if plan.examples:
        try:
            example_block = speech_mod.example_block(plan.examples, language=lang)
        except Exception as exc:  # noqa: BLE001
            log.debug("example_block 失败（忽略）: %s", exc)

    # 各模块自带的块标题已经足够（[Speech DNA] / [This turn]），这里只给生活事实加护栏标题
    head_sections: list[str] = []
    if str(dna_block or "").strip():
        head_sections.append(dna_block.strip())
    if life_lines:
        head_sections.append(
            "[Your life — only these facts are true; do not invent others]\n" + "\n".join(life_lines)
        )
    if example_block:
        # example_block 自带 "[Example voice references …]" 标题，不再重复加
        head_sections.append(example_block.strip())
    if plan.directive:
        head_sections.append(plan.directive.strip())

    text = "\n\n".join(section for section in head_sections if section)
    if len(text) <= MAX_LAYER_CHARS:
        return text

    # 超预算：从最不关键的段落开始丢（示例 → 生活事实）
    drop_order = ["example", "life"]
    for target in drop_order:
        if len(text) <= MAX_LAYER_CHARS:
            break
        if target == "example" and example_block:
            head_sections = [s for s in head_sections if "Example voice references" not in s]
            example_block = ""
        elif target == "life" and life_lines:
            head_sections = [s for s in head_sections if "Your life — only these facts" not in s]
            life_lines = []
        text = "\n\n".join(section for section in head_sections if section)

    if len(text) > MAX_LAYER_CHARS:
        # 仍然超（极长 DNA）：硬截断，但保证 [This turn] 完整保留
        directive = plan.directive.strip() if plan.directive else ""
        room = max(0, MAX_LAYER_CHARS - len(directive) - 2)
        body = text.replace(directive, "").strip()
        text = (body[:room].rstrip() + "\n\n" + directive).strip() if directive else body[:room]
    return text


#: 通话层字符预算：语音链路对 TTFT 敏感，比文字层短得多（约 250 token）
MAX_CALL_LAYER_CHARS = 900

#: 从 behavior_directive 里挑出通话真正需要的行（太长会拖慢首字）
_CALL_DIRECTIVE_PREFIXES: tuple[str, ...] = (
    "Action:",
    "Length:",
    "Do not end",
    "This is the one turn",
    "Write the reply in",
)


def build_call_plan(
    db: Any,
    friend: dict,
    transcript: str,
    *,
    turn_index: int = 0,
    rng: Any = None,
    user_id: str | None = None,
) -> TurnPlan:
    """语音通话的本轮计划：与文字聊天同源，但**不注入示例对话**（省 TTFT）。"""
    return build_turn_plan(
        db,
        friend,
        transcript,
        turn_index=turn_index,
        rng=rng,
        include_examples=False,
        user_id=user_id,
    )


def _compact_directive(directive: str) -> str:
    """把行为指令压缩成通话版：只留动作 / 长度 / 提问策略 / 语言。

    解析失败就整段截断——宁可多几行，也不能丢掉「这轮该干什么」。
    """
    text = str(directive or "").strip()
    if not text:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    kept = [line for line in lines if line.startswith(_CALL_DIRECTIVE_PREFIXES)]
    return "\n".join(kept) if kept else text[:420]


def _compact_dna_line(dna: dict) -> str:
    """通话版 DNA：一行，只留对「听感」影响最大的四项。"""
    dna = dna if isinstance(dna, dict) else {}
    parts = [
        f"rhythm: {dna.get('sentence_rhythm') or 'measured'}",
        f"humour: {dna.get('humor_style') or 'none'}",
        f"directness: {dna.get('directness') or 'direct'}",
        f"typical length: {dna.get('verbosity') or 'short'}",
        f"emoji: {dna.get('emoji_usage') or 'never'}",
    ]
    return (
        "[Speech DNA on the phone] tendencies, not rules: "
        + "; ".join(parts)
        + ". One mood can be flat — you do not have to sound upbeat."
    )


def build_call_layer(db: Any, friend: dict, plan: TurnPlan, *, language: str = "") -> str:
    """通话专用的紧凑 Persona 层（上限 :data:`MAX_CALL_LAYER_CHARS`）。

    顺序：DNA（听感）→ 此刻生活 → 本轮行为指令（放最后，模型对末尾最敏感）。
    超预算时先丢生活事实，再丢 DNA，行为指令永不裁剪。
    """
    dna_line = ""
    try:
        dna_line = _compact_dna_line(plan.dna)
    except Exception as exc:  # noqa: BLE001
        log.debug("通话 DNA 行构造失败（忽略）: %s", exc)

    life_lines: list[str] = []
    now_line = _life_now_line(plan.activity or {}, moment=_now())
    if now_line:
        life_lines.append(now_line)
    # 生活事实只取前两条「- 」行，避免通话 prompt 变长
    for raw in str(plan.life_block or "").splitlines():
        line = raw.strip()
        if line.startswith("- "):
            life_lines.append(line)
        if len(life_lines) >= 3:
            break

    directive = _compact_directive(plan.directive)

    sections: list[str] = []
    if dna_line:
        sections.append(dna_line)
    if life_lines:
        sections.append(
            "[Your life right now — only these facts are true]\n" + "\n".join(life_lines)
        )
    if directive:
        sections.append(
            "[This turn — you are SPEAKING on a live call, not writing]\n"
            + directive
            + "\nSound spoken: contractions, fragments fine, no lists, no markdown."
        )

    text = "\n\n".join(section for section in sections if section)
    if len(text) <= MAX_CALL_LAYER_CHARS:
        return text

    # 超预算：先丢生活事实，再丢 DNA
    if life_lines:
        sections = [s for s in sections if "Your life right now" not in s]
        text = "\n\n".join(sections)
    if len(text) > MAX_CALL_LAYER_CHARS and dna_line:
        sections = [s for s in sections if not s.startswith("[Speech DNA on the phone]")]
        text = "\n\n".join(sections)
    if len(text) > MAX_CALL_LAYER_CHARS:
        text = text[:MAX_CALL_LAYER_CHARS].rstrip()
    return text


#: 通聊天事实块里「你身边的人」的行数与字符上限（Prompt 预算）
MAX_PEOPLE_LINES = 2
MAX_PEOPLE_CHARS = 220


def _people_lines(db: Any, persona_id: str) -> list[str]:
    """「你身边的人」1-2 行：只写有关系证据的人，不写内部数字。

    取关系里 closeness>=3（亲近）或 tension>=2（有摩擦）的人；有摩擦的人排在前面
    （对当下的表达影响更大）。任何查询失败都返回空列表，不阻断聊天。
    """
    if db is None or not persona_id:
        return []
    try:
        rows = life_events_mod.relationship_summary(db, persona_id, limit=8)
    except Exception as exc:  # noqa: BLE001
        log.debug("relationship_summary 失败（忽略）: %s", exc)
        return []
    picked: list[tuple[int, str]] = []
    for rel in rows or []:
        if not isinstance(rel, dict):
            continue
        name = str(rel.get("nickname") or "").strip()
        if not name:
            continue
        try:
            closeness = int(rel.get("closeness") or 0)
            tension = int(rel.get("tension") or 0)
        except (TypeError, ValueError):
            continue
        if closeness < 3 and tension < 2:
            continue
        relation = str(rel.get("relation_type") or "friend")
        recent = rel.get("recent_event") or {}
        title = str(recent.get("title") or "").strip() if isinstance(recent, dict) else ""
        if tension >= 2:
            text = f"- {name} ({relation}): a bit tense with you right now"
            if title:
                text += f", since \"{title}\""
            text += "."
            picked.append((0, text))
        else:
            text = f"- {name} ({relation}): close to you"
            if title:
                text += f", last together: \"{title}\""
            text += "."
            picked.append((1, text))
    picked.sort(key=lambda item: item[0])
    lines: list[str] = []
    used = 0
    for _rank, text in picked:
        if len(lines) >= MAX_PEOPLE_LINES or used + len(text) > MAX_PEOPLE_CHARS:
            break
        lines.append(text)
        used += len(text)
    return lines


def _life_now_line(activity: dict, *, moment: datetime | None = None) -> str:
    """「你现在在做什么」一行。只陈述规则/事件给出的事实，不编造。

    已结束的 ongoing 事件（后台还没刷状态）在这里被忽略，避免人物「11:15 还在做项目」到半夜。
    """
    if not isinstance(activity, dict) or not activity:
        return ""
    label = str(activity.get("label") or "").strip()
    if not label:
        return ""
    current = moment or _now()
    ended = _parse_iso(activity.get("end_time"))
    if ended is not None and ended <= current:
        return ""
    started = activity.get("start_time") or ""
    if started:
        return f"- Right now: {label} (started {str(started)[11:16]})."
    return f"- Right now: {label}."


# --------------------------------------------------------------------------- #
# 公开适配层：prompt_composer 需要复用「此刻生活 / 身边的人 / 通话压缩」这些
# 已经打磨过的片段，但不应该 import 私有函数（_people_lines / _life_now_line …）。
# 这里只做导出适配，行为与原函数完全一致。
# --------------------------------------------------------------------------- #
def life_now_line(activity: dict, *, moment: datetime | None = None) -> str:
    """公开版本 :func:`_life_now_line`（供 prompt_composer 使用）。"""
    return _life_now_line(activity, moment=moment)


def people_lines(db: Any, persona_id: str) -> list[str]:
    """公开版本 :func:`_people_lines`（供 prompt_composer 使用）。"""
    return _people_lines(db, persona_id)


def compact_directive(directive: str) -> str:
    """公开版本 :func:`_compact_directive`（通话版行为指令压缩）。"""
    return _compact_directive(directive)


def compact_dna_line(dna: dict) -> str:
    """公开版本 :func:`_compact_dna_line`（通话版 DNA 一行）。"""
    return _compact_dna_line(dna)


# --------------------------------------------------------------------------- #
# 回合结束后写回
# --------------------------------------------------------------------------- #
def record_behavior(db: Any, friend: dict, behavior_value: str) -> list[str]:
    """把本轮行为记进 ``friends.recent_behaviors``（最多 8 条，最新在后）。

    以**数据库里的当前值**为准追加，而不是用调用方传来的 friend dict——
    同一次请求里 friend 可能是几轮之前的快照，直接用它会把历史截断成 1 条。
    """
    friend = friend if isinstance(friend, dict) else {}
    fid = str(friend.get("id") or "").strip()
    current = _recent_behaviors(friend)
    if fid and db is not None:
        try:
            with db._lock:  # noqa: SLF001
                row = db.conn.execute(
                    "SELECT recent_behaviors FROM friends WHERE id=?", (fid,)
                ).fetchone()
            if row is not None:
                current = _recent_behaviors({"recent_behaviors": row["recent_behaviors"]})
        except Exception as exc:  # noqa: BLE001 - 读不到就用调用方传的
            log.debug("读取 recent_behaviors 失败（用传入值）: %s", exc)
    if behavior_value:
        current = (current + [str(behavior_value)])[-MAX_RECENT_BEHAVIORS:]
    if not fid or db is None:
        return current
    try:
        with db._lock, db.conn:  # noqa: SLF001
            db.conn.execute(
                "UPDATE friends SET recent_behaviors=? WHERE id=?",
                (json.dumps(current, ensure_ascii=False), fid),
            )
    except Exception as exc:  # noqa: BLE001 - 记录失败不影响聊天
        log.debug("写入 recent_behaviors 失败（忽略）: %s", exc)
    return current


def update_emotion_after_turn(
    db: Any,
    plan: TurnPlan,
    *,
    intent: str = "",
    behavior_value: str = "",
    user_id: str | None = None,
) -> dict:
    """一轮聊天结束后更新**「当前用户 × 该人物」**的关系情绪。

    多用户一期起，私聊**绝不写** ``personas.emotion_state``：那是公共基线，只由世界
    事件（:mod:`app.v3.life_events`）推进。私聊结果落到
    ``persona_user_emotion(user_id, persona_id)``，A 的情绪不会改变 B 遇到的人物。

    ``user_id`` 解析优先级：显式参数 → ``plan.user_id``（本轮计划读情绪时用的用户，
    保证读写同一个人的记录）→ :func:`app.v3.identity.current_user_id`。
    """
    if db is None or not plan.persona_id:
        return plan.emotion or {}
    try:
        state = emotion_mod.apply_chat(
            plan.emotion or {}, intent=intent or "unknown", behavior=behavior_value or plan.behavior
        )
        state["updated_at"] = _now().strftime("%Y-%m-%dT%H:%M:%S+00:00")
        uid = str(user_id or "").strip() or str(getattr(plan, "user_id", "") or "").strip()
        uid = uid or resolve_user_id()
        privacy_mod.save_user_persona_emotion(db, uid, plan.persona_id, state)
        return state
    except Exception as exc:  # noqa: BLE001
        log.debug("更新情绪状态失败（忽略）: %s", exc)
        return plan.emotion or {}


def behavior_summary(plan: TurnPlan) -> dict:
    """给 API/日志用的本轮行为摘要（不含用户可见文案）。"""
    return {
        "persona_id": plan.persona_id,
        "behavior": plan.behavior,
        "reason": plan.behavior_reason,
        "scene": plan.scene,
        "examples": len(plan.examples),
        "has_life_facts": bool(str(plan.life_block or "").strip()),
        "activity": str((plan.activity or {}).get("label") or ""),
        "degraded": plan.degraded,
    }


def prompt_summary(composed: Any) -> dict:
    """本轮 Prompt Composer 合成摘要（版本 / 模块 key / 每模块 chars）。

    延迟导入 :mod:`app.v3.prompt_composer`，避免 v2/v3 循环导入；任何异常返回空 dict。
    """
    try:
        from app.v3.prompt_composer import prompt_summary as _summary  # noqa: PLC0415

        return _summary(composed)
    except Exception as exc:  # noqa: BLE001 - 摘要只服务排查，不允许影响主链路
        log.debug("prompt_summary 失败（忽略）: %s", exc)
        return {}


__all__ = [
    "EXAMPLE_K",
    "MAX_CALL_LAYER_CHARS",
    "MAX_LAYER_CHARS",
    "MAX_RECENT_BEHAVIORS",
    "TurnPlan",
    "behavior_summary",
    "build_call_layer",
    "build_call_plan",
    "build_persona_layer",
    "build_turn_plan",
    "compact_directive",
    "compact_dna_line",
    "life_now_line",
    "people_lines",
    "persona_id_for_friend",
    "prompt_summary",
    "record_behavior",
    "resolve_user_id",
    "update_emotion_after_turn",
]
