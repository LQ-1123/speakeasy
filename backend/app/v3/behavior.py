"""SpeakEasy V3 · 行为选择引擎（Behavior Engine，纯规则、零 LLM 调用）。

产品动机
--------
现在的所有 Persona 都走同一条路：用户输入 → 拼 prompt → LLM 回复，而 prompt 里
强制「每轮留一个问题」「只写 1-2 句」「保持热情」，结果 **所有人物都变成了客服**。

真实的人在聊天时并不是每轮都想问问题：有时只想回一句 "hm."，有时想吐槽自己的
一天，有时想讲个刚发生的小故事，有时想换个话题，有时是真的想收尾了。本模块补的
就是「这一轮人物想干什么」这一层：

    用户输入 → detect_intent（情境识别）
             → choose_behavior（行为选择，本模块，零 LLM）
             → behavior_directive（给 LLM 的本轮指令）
             → Speech DNA 决定「怎么说」（另一模块）

三条产品原则（也是本模块存在的理由）：

1. **允许不提问**：提问始终可选；正常澄清不受随机行为标签禁止。
2. **允许一句话**：``acknowledge`` 可以只有一两个词，长度倾向由 DNA verbosity 决定。
3. **允许不同意 / 允许结束**：``disagree`` 与 ``end_conversation`` 都是合法选择，
   不会被「热情、配合」的硬规则吃掉。

设计约束
--------
- **纯逻辑**：只依赖 ``random`` / ``dataclasses`` / ``typing`` / ``re``；不碰 db、llm、
  httpx、asyncio，也不 import ``app.v3.speech_dna``（那边会 import 本模块，避免循环依赖）。
  DNA 数值一律以 ``dna_traits: dict[str, float]`` 形式传入。
- **抽样可复现**：``rng=None`` 时内部 ``random.Random()``；测试传 ``random.Random(42)``，
  同一 seed 必得同一序列。
- **权重语义**：所有 ``_base_weights`` 都是相对值，乘完各类系数后归一化；
  ``BehaviorChoice.weights`` 存的是**归一化后**的结果（sum ≈ 1.0），便于测试与线上观测。
- **``social_distance`` 方向约定**：**1.0 = 亲近（familiar/intimate），0.0 = 疏远（distant）**。
  这与真实生产者 ``app.v3.speech_dna.dna_traits()`` 一致——那边把 ``social_distance``
  枚举映射成「越大越亲近」（distant=0.15 … intimate=0.95），并会把它加进 warmth。
  内部按 ``closeness_trait = social_distance`` 使用；若要算「距离」用 ``1 - closeness_trait``。
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

# --------------------------------------------------------------------------- #
# 契约常量（见 docs/PERSONA_UPGRADE_PLAN.md §4.1）
# --------------------------------------------------------------------------- #
#: 全部可被选中的行为（顺序固定，抽样按此顺序累积，保证可复现）
BEHAVIORS: tuple[str, ...] = (
    "respond",
    "share",
    "joke",
    "tease",
    "empathize",
    "comfort",
    "disagree",
    "complain",
    "celebrate",
    "tell_story",
    "change_topic",
    "acknowledge",
    "end_conversation",
    "ask_question",
)

#: 全部可识别的情境
INTENTS: tuple[str, ...] = (
    "greeting",
    "question",
    "good_news",
    "bad_news",
    "opinion_probe",
    "small_talk",
    "closing",
    "silence",
    "unknown",
)

#: BehaviorInput.dna_traits 的固定键（由 speech_dna.dna_traits() 产出，全部 0..1）
TRAIT_KEYS: tuple[str, ...] = (
    "initiative",
    "warmth",
    "humour",
    "directness",
    "verbosity",
    "disclosure",
    "emoji",
    "slang",
    "playfulness",
    "social_distance",
)

#: 缺键 / 脏值时的安全默认（中性人物）
DEFAULT_TRAITS: dict[str, float] = {key: 0.5 for key in TRAIT_KEYS}

#: 历史窗口：只看最近 3 轮
HISTORY_WINDOW = 3
#: 行为重复的衰减系数（最近一轮罚到 0.35，往前按几何速度回流到 1.0）
RECENCY_DECAY = 0.35
#: ask_question 在最近 2 轮出现过的额外惩罚
ASK_QUESTION_EXTRA = 0.15
#: respond 的绝对权重下限（归一化后仍 > 0，任何情境都能「只是回应」）
RESPOND_FLOOR = 0.08
#: end_conversation 的基础权重（很低但非 0）
END_BASE_WEIGHT = 0.025
#: ask_question 基础权重的最小值 / 随 initiative 的跨度（上限受 initiative 约束）
ASK_BASE_MIN = 0.03
ASK_BASE_SPAN = 0.95

_MOOD_KEYS: tuple[str, ...] = ("energy", "stress", "social_battery")

# --------------------------------------------------------------------------- #
# 数据结构（§4.2）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BehaviorInput:
    """一轮聊天里行为选择需要的全部输入（不依赖 DB / LLM）。

    ``dna_traits`` 的键固定为 :data:`TRAIT_KEYS`，缺键自动补 0.5；越界值会被夹到 0..1。
    ``disagreement_style`` 是契约之外**可选的**补充字段：Speech DNA 里有
    ``disagreement_style`` 枚举（avoid/soften/state_plainly/challenge），但 values
    并不在 ``dna_traits`` 的数值键里，所以单独以字符串传入，用来实现
    「avoid → disagree 权重≈0.02」这条规则；不传时按 directness 推导。
    """

    dna_traits: dict[str, float]
    user_text: str = ""
    recent_behaviors: tuple[str, ...] = ()  # 最新在后
    closeness: int = 1  # 1..5（app/v2/social.py 的熟稔度等级）
    mood: dict | None = None  # {'mood','energy','stress','social_battery'}
    turn_index: int = 0
    is_first_turn: bool = False
    has_due_open_loop: bool = False
    current_activity: str = ""  # 例 "at work" / "on the bus"
    time_period: str = ""  # timeutil.local_state()['period']
    disagreement_style: str = ""  # 可选："avoid" | "soften" | "state_plainly" | "challenge"


@dataclass(frozen=True)
class BehaviorChoice:
    """本轮选中的行为。

    ``weights`` 是**归一化后**的 14 个行为权重（sum ≈ 1.0），可直接落日志/做 A/B 观测。
    """

    behavior: str
    reason: str
    confidence: float
    weights: dict[str, float]


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _clamp01(value: Any, default: float = 0.5) -> float:
    """任意脏输入 → 0..1 浮点；非法值返回 ``default``（NaN 也走默认）。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if num != num:  # NaN
        return default
    return min(1.0, max(0.0, num))


def normalize_traits(raw: Mapping[str, Any] | None) -> dict[str, float]:
    """把任意（可能缺键/越界的）DNA 数值字典补全成 :data:`TRAIT_KEYS` 全量 0..1。

    缺键 → 0.5（中性），非数字 → 0.5，越界夹紧。本函数只做数值清洗，不推导 DNA。
    """
    src: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    return {key: _clamp01(src.get(key, 0.5), 0.5) for key in TRAIT_KEYS}


def _closeness(value: Any) -> int:
    """closeness 夹到 1..5（脏输入回落到 1）。"""
    try:
        num = int(value)
    except (TypeError, ValueError):
        return 1
    return min(5, max(1, num))


def _mood_values(mood: Mapping[str, Any] | None) -> tuple[float, float, float, str]:
    """mood dict → ``(energy, stress, social_battery, mood_label)``，全部带安全默认。"""
    src: Mapping[str, Any] = mood if isinstance(mood, Mapping) else {}
    energy = _clamp01(src.get("energy", 0.6), 0.6)
    stress = _clamp01(src.get("stress", 0.35), 0.35)
    battery = _clamp01(src.get("social_battery", 0.6), 0.6)
    label = str(src.get("mood") or "").strip().lower()
    return energy, stress, battery, label


def _safe_recent(recent: Iterable[Any] | None) -> tuple[str, ...]:
    """只保留认识的行为名，截最近 :data:`HISTORY_WINDOW` 条。"""
    if not recent:
        return ()
    names = [str(item) for item in recent if isinstance(item, str) and item in BEHAVIORS]
    return tuple(names[-HISTORY_WINDOW:])


# --------------------------------------------------------------------------- #
# 情境识别 detect_intent
# --------------------------------------------------------------------------- #
#: 含「内容」的字符（拉丁 / 中日韩）——只有标点或 emoji 的输入视为 silence
_CONTENT_RE = re.compile(
    r"[0-9A-Za-z\u00c0-\u024f\u0400-\u04ff\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]"
)

#: 纯语气词 / 附和词（"嗯"、"ok"、"hmm"）——没有信息量，按 silence 处理
_FILLER_RE = re.compile(
    r"^(?:h+m+|u+h+|u+m+|e+h+|o+h+|o+k|k+|o+kay|m+k+|y+e+a+h*|y+e+p+|y+u+p+|n+a+h*|"
    r"n+o+p+e*|s+u+r+e|a+l+r+i+g+h+t|r+i+g+h+t|f+a+i+r|g+o+t+i+t|"
    r"嗯+|哦+|噢+|啊+|额+|唔+|呃+|呵+|哈+|好+|是+|对+|行+|可以|好的|好吧|行吧|"
    r"真的|确实|不错|厉害|牛)"
    r"[\s\.．。,…，、!！?？~～\-—_]*$",
    re.IGNORECASE,
)

#: 坏消息（负面事件 / 情绪低落）——优先级最高，需要 empathy 而不是段子
_BAD_NEWS_RE = re.compile(
    r"\b(?:died|death|passed away|passed on|funeral|broke up|break[- ]?up|broke me|"
    r"dumped|divorc\w*|got fired|was fired|laid off|lost my job|lost his job|lost her job|"
    r"failed|flunked|rejected|rejection|turned down|got sick|fell ill|is ill|is sick|hospital|surgery|"
    r"accident|crashed|car broke|stolen|robbed|scammed|evicted|"
    r"missed my flight|missed the bus|panic attack|depressed|diagnos\w*|"
    r"cancer|miscarriage|lost my|lost her|lost his|bad news|terrible day|awful day|"
    r"hate my|so sad|devastated|heartbroken|exhausted)\b"
    r"|去世|过世|离世|葬礼|分手了|分手|离婚|被甩|挂科|考砸|没考好|被拒|被炒|被裁|"
    r"失业|丢工作|生病|住院|手术|出事|车祸|撞车|被盗|被偷|被抢|被骗|退学|崩溃|"
    r"焦虑|抑郁|难过|伤心|想哭|哭了|失落|绝望|压力好大|熬不住|撑不住|不舒服",
    re.IGNORECASE,
)

#: 好消息
_GOOD_NEWS_RE = re.compile(
    r"\b(?:got the job|got promoted|promoted|new job|job offer|got an offer|"
    r"accepted|passed the exam|passed my|graduated|won|winner|first place|"
    r"award|engaged|pregnant|expecting|new apartment|moved in|got a raise|raise|"
    r"scholarship|approved|visa|i did it|so happy|so excited|good news|great news|"
    r"best day|birthday|got into)\b"
    r"|升职|加薪|涨薪|拿到offer|拿到了offer|拿到offer了|被录取|录取了|考过了|考上了|"
    r"通过考试|拿到|毕业了|获奖|中奖|赢了|冠军|订婚|结婚了|怀孕|买房|搬家了|入职|"
    r"转正|太好了|好消息|成功了|发表论文|我过了|我考过了|太棒了",
    re.IGNORECASE,
)

#: 征求意见 / 让对方表态
_OPINION_RE = re.compile(
    r"\b(?:what do you think|what'?s your take|your opinion|do you think|"
    r"what would you do|would you rather|should i|is it worth|agree\?|"
    r"sound like a good idea)\b"
    r"|你觉得|你怎么看|你怎么想|你认为|你的看法|你 opinion|我应该|要不要|该不该|"
    r"值不值|靠谱吗|同意吗|行不行|好不好|可以吗|对吗|是不是该",
    re.IGNORECASE,
)

#: 问候（只认明确短语，避免把 "morning routine" 之类误判）
_GREETING_RE = re.compile(
    r"^(?:hi|hello|hiya|hey|heya|yo|sup|howdy|good (?:morning|afternoon|evening)|"
    r"how are you(?: doing)?|how'?re you|how is it going|how'?s it going|"
    r"how are things|nice to (?:see|meet) you|long time no see|"
    r"你好|您好|哈喽|嗨|早上好|早安|上午好|中午好|下午好|晚上好|"
    r"在吗|在不在|最近怎么样|最近好吗|好久不见|好久没聊)"
    r"(?:[\s\.!！~,，。?？]|$)",
    re.IGNORECASE,
)

#: 英文疑问句开头
_EN_QUESTION_START_RE = re.compile(
    r"^(?:what|why|how|when|where|who|whom|whose|which|do|does|did|are|is|was|were|"
    r"am|can|could|will|would|should|have|has|had|may|might)\b",
    re.IGNORECASE,
)

#: 英文疑问句里常见的「助动词 + 主语」中段结构
_EN_QUESTION_MID_RE = re.compile(
    r"\b(?:do|does|did|are|is|was|were|can|could|would|will|should|have|has)"
    r"\s+(?:you|i|we|they|he|she|it)\b",
    re.IGNORECASE,
)

#: 中文疑问词 / 疑问语气
_ZH_QUESTION_RE = re.compile(
    r"吗|嘛|请问|想问|是不是|有没有|能不能|可不可以|要不要|对不对|好不好|行不行|"
    r"为什么|怎么|怎样|什么|啥|哪个|哪里|哪儿|谁|多少|几点|几号|几年"
)

#: 中文句尾疑问语气词（"呢" 只在句尾才算问句）
_ZH_QUESTION_TAIL_RE = re.compile(r"(?:呢|么)[\s\.。!！~～]*$")

#: 收尾 / 告别
_CLOSING_RE = re.compile(
    r"\b(?:bye|bye[- ]?bye|goodbye|good ?night|night[- ]night|see (?:you|ya)|"
    r"catch (?:you|ya)|talk (?:to you )?later|talk tomorrow|ttyl|cya|"
    r"i'?m (?:off|out)|gotta (?:go|run)|got to go|have to go|need to go|"
    r"i should go|going to bed|off to bed|signing off|take care|"
    r"that'?s it for tonight|wrap (?:it )?up)\b"
    r"|再见|拜拜|拜~|晚安|先这样|先这么着|我?先?睡了|睡觉去|我下了|回头聊|回头说|"
    r"晚点聊|明天聊|改天聊|下次聊|不聊了|去忙了|我先忙|886|88|撤了|挂了|"
    r"有空再聊|就这样吧|先不聊了",
    re.IGNORECASE,
)

#: 日常闲聊的信号词（今天/天气/工作/吃喝/通勤……）
_SMALL_TALK_RE = re.compile(
    r"\b(?:lol|lmao|haha+|hehe|today|tonight|yesterday|weekend|weather|busy|tired|"
    r"exhausted|bored|rain\w*|sunny|cold|hot|hungry|lunch|dinner|breakfast|coffee|tea|"
    r"work|worked|working|school|class|classes|meeting|traffic|netflix|movie|game|"
    r"gaming|gym|walk|dog|cat|food|cook\w*|sleep\w*|stressed|ugh|pasta|pizza)\b"
    r"|今天|明天|昨天|晚上|早上|中午|下午|周末|上班|下班|上课|加班|好累|累死|无聊|"
    r"天气|下雨|好冷|好热|吃饭|午饭|晚饭|咖啡|地铁|堵车|追剧|游戏|健身|散步|猫|狗|"
    r"好困|忙|累了|熬夜|做了|吃了|去了|谢谢|多谢|thank(?:s| you)|thx",
    re.IGNORECASE,
)

#: 纯附和（"cool"、"确实"）——归入 small_talk
_ACK_RE = re.compile(
    r"^(?:cool|nice|great|wow|really|true|right|sure|yeah|yep|okay|ok|alright|"
    r"fair enough|fair|agreed|same|indeed|nice one|good one|"
    r"哈哈+|真的|确实|是啊|对啊|好吧|行吧|可以|不错|厉害了?|牛|笑死)"
    r"[\s!~.。！～]*$",
    re.IGNORECASE,
)

_TOKEN_RE = re.compile(r"[A-Za-z0-9']+")
_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def _token_count(text: str) -> int:
    """粗粒度 token 数：英文按词、中文按字。"""
    return len(_TOKEN_RE.findall(text)) + len(_CJK_CHAR_RE.findall(text))


def _looks_question(low: str) -> bool:
    """英文/中文疑问句判定（在 opinion_probe / greeting 之后调用）。"""
    if "?" in low or "？" in low:
        return True
    if _ZH_QUESTION_RE.search(low):
        return True
    if _ZH_QUESTION_TAIL_RE.search(low):
        return True
    if _EN_QUESTION_START_RE.match(low):
        return True
    if _EN_QUESTION_MID_RE.search(low):
        return True
    return False


def detect_intent(user_text: str) -> str:
    """用户文本 → :data:`INTENTS` 之一（纯规则 + 正则，中英文都认）。

    判定顺序（先强特征，后弱特征）：

    1. ``silence``：空串 / 纯标点 / 纯 emoji / 纯语气词（"嗯"、"ok"、"hmm"）。
    2. ``bad_news``：明确的负面事件或情绪（优先级最高——这时候不该讲段子）。
    3. ``good_news``：恭喜类事件。
    4. ``opinion_probe``：明确征求看法（"what do you think"、"你觉得"）。
    5. ``greeting``：明确的问候短语，且整句够短（避免 "hi, my dog died" 被误判）。
    6. ``question``：疑问句（问号 / 疑问词 / 助动词开头）。
    7. ``closing``：告别语。
    8. ``small_talk``：日常闲聊信号词、纯附和，或长度足够的普通陈述。
    9. ``unknown``：兜底（内容太短且没有任何特征）。

    >>> detect_intent("hey!"), detect_intent("我的猫去世了"), detect_intent("")
    ('greeting', 'bad_news', 'silence')
    """
    text = "" if user_text is None else str(user_text)
    stripped = text.strip()
    if not stripped:
        return "silence"
    if not _CONTENT_RE.search(stripped):
        # 只有标点 / emoji / 空白：没有可回应内容
        return "silence"

    low = stripped.lower()
    if _FILLER_RE.match(low):
        return "silence"
    if _BAD_NEWS_RE.search(low):
        return "bad_news"
    if _GOOD_NEWS_RE.search(low):
        return "good_news"
    if _OPINION_RE.search(low):
        return "opinion_probe"
    if _GREETING_RE.match(low) and _token_count(low) <= 8:
        return "greeting"
    if _looks_question(low):
        return "question"
    if _CLOSING_RE.search(low):
        return "closing"
    if _SMALL_TALK_RE.search(low) or _ACK_RE.match(stripped):
        return "small_talk"
    # 兜底：句子足够长就当成普通闲聊，否则 unknown（例如单个无法识别的词）
    if _token_count(stripped) >= 3 or len(_CJK_CHAR_RE.findall(stripped)) >= 4:
        return "small_talk"
    return "unknown"


# --------------------------------------------------------------------------- #
# 历史惩罚 behavior_history_penalty
# --------------------------------------------------------------------------- #
def behavior_history_penalty(
    recent: tuple[str, ...] | list[str],
    behavior: str,
) -> float:
    """最近 :data:`HISTORY_WINDOW` 轮里重复出现 ``behavior`` 的惩罚系数（0..1，越小越压）。

    规则：
    - 只看最近 3 轮；``recent`` 最新在后（与 ``BehaviorInput.recent_behaviors`` 一致）。
    - 最近一轮出现 → ×0.35；再往前每退一轮，惩罚按 0.35 的几何速度衰减回 1.0
      （即 ``1 - 0.65 * 0.35**offset``，offset=0 是最新）。
    - ``ask_question`` 额外惩罚：最近 2 轮出现过 → 再 ×0.15（防止每轮都追问）。
    - 没出现过 → 1.0（不惩罚）。

    >>> round(behavior_history_penalty(("ask_question",), "ask_question"), 4)
    0.0525
    >>> behavior_history_penalty((), "share")
    1.0
    """
    name = str(behavior or "")
    if name not in BEHAVIORS:
        return 1.0
    window = _safe_recent(recent)
    if not window:
        return 1.0

    penalty = 1.0
    # 最新在后：reversed 后 offset=0 即最近一轮
    for offset, seen in enumerate(reversed(window)):
        if seen != name:
            continue
        penalty *= 1.0 - (1.0 - RECENCY_DECAY) * (RECENCY_DECAY**offset)
    if name == "ask_question" and "ask_question" in window[-2:]:
        penalty *= ASK_QUESTION_EXTRA
    return min(1.0, max(0.0, penalty))


# --------------------------------------------------------------------------- #
# 基础权重
# --------------------------------------------------------------------------- #
def _base_weights(traits: dict[str, float], inp: BehaviorInput) -> dict[str, float]:
    """DNA 决定的「无情境先验」：这一轮本来就比较想干什么。

    每一项都随某个 trait 单调变化（测试会按倍数关系验证）：
    - ``share`` / ``tell_story`` ← disclosure↑、亲密度↑
    - ``joke`` / ``tease`` ← humour↑、playfulness↑（tease 还要求亲近）
    - ``ask_question`` ← initiative↑，并用幂次压低低主动型人物的提问上限
    - ``empathize`` / ``comfort`` ← warmth↑
    - ``disagree`` ← directness↑；``disagreement_style=="avoid"`` 时压到 ≈0.02
    - ``acknowledge`` ← verbosity↓、关系疏远↓
    - ``end_conversation`` 给一个很低但非 0 的底（否则「允许结束」永远抽不到）

    注意：``social_distance`` 在本项目的语义是「越大越亲近」（见模块 docstring）。
    """
    initiative = traits["initiative"]
    warmth = traits["warmth"]
    humour = traits["humour"]
    directness = traits["directness"]
    verbosity = traits["verbosity"]
    disclosure = traits["disclosure"]
    playfulness = traits["playfulness"]
    closeness_trait = traits["social_distance"]  # 1.0 = intimate, 0.0 = distant

    closeness_f = (_closeness(inp.closeness) - 1) / 4.0  # Lv1→0.0, Lv5→1.0
    intimacy = min(1.0, max(0.0, 0.45 * closeness_f + 0.55 * closeness_trait))
    distance = 1.0 - closeness_trait  # 「疏远」程度，用于少数反向项

    avoid_disagreement = str(inp.disagreement_style or "").strip().lower() == "avoid"
    disagree = 0.02 if avoid_disagreement else 0.04 + 0.65 * directness

    return {
        "respond": 1.0,
        "share": 0.12 + 0.85 * disclosure + 0.35 * intimacy,
        "tell_story": 0.06 + 0.75 * disclosure * (0.4 + 0.6 * verbosity) + 0.25 * intimacy,
        "joke": 0.02 + 0.85 * (humour**1.3) * (0.5 + 0.5 * playfulness),
        "tease": 0.01 + 0.50 * humour * playfulness * (0.35 + 0.65 * intimacy),
        "empathize": 0.06 + 0.70 * warmth,
        "comfort": 0.04 + 0.55 * warmth * (0.6 + 0.4 * intimacy),
        "disagree": disagree,
        "complain": 0.08 + 0.30 * directness + 0.20 * (1.0 - warmth),
        "celebrate": 0.06 + 0.30 * warmth + 0.25 * playfulness,
        "change_topic": 0.03 + 0.35 * distance + 0.15 * (1.0 - disclosure),
        "acknowledge": 0.06 + 0.75 * (1.0 - verbosity) + 0.15 * distance,
        "end_conversation": END_BASE_WEIGHT,
        # initiative 用 1.7 次幂做「上限约束」：低主动型上限低，高主动型才明显想问
        "ask_question": ASK_BASE_MIN + ASK_BASE_SPAN * (initiative**1.7),
    }


#: intent → 行为乘数（§4.3 规则；未列出的行为乘 1.0）
_INTENT_MULTIPLIERS: dict[str, dict[str, float]] = {
    "greeting": {
        "respond": 1.70,
        "acknowledge": 1.50,
        "share": 0.80,
        "tell_story": 0.70,
        "ask_question": 0.80,
    },
    "question": {
        "respond": 1.90,
        "ask_question": 0.55,
        "share": 0.90,
        "change_topic": 0.60,
        "end_conversation": 0.60,
    },
    "good_news": {
        "celebrate": 6.00,
        "respond": 1.70,
        "acknowledge": 0.80,
        "complain": 0.25,
        "disagree": 0.30,
        "joke": 0.55,
        "tease": 0.55,
        "change_topic": 0.40,
        "ask_question": 0.70,
        "end_conversation": 0.20,
        "tell_story": 0.60,
        "share": 0.80,
        "empathize": 0.70,
        "comfort": 0.80,
    },
    "bad_news": {
        "empathize": 3.50,
        "comfort": 3.50,
        "respond": 2.00,
        "joke": 0.05,
        "tease": 0.05,
        "celebrate": 0.05,
        "share": 0.25,
        "tell_story": 0.25,
        "change_topic": 0.25,
        "complain": 0.40,
        "disagree": 0.45,
        "acknowledge": 0.60,
        "ask_question": 0.40,
        "end_conversation": 0.30,
    },
    "opinion_probe": {
        "disagree": 2.40,
        "respond": 1.80,
        "share": 1.20,
        "tell_story": 1.10,
        "ask_question": 0.80,
        "acknowledge": 0.50,
        "end_conversation": 0.20,
    },
    "small_talk": {
        "respond": 1.15,
        "share": 1.25,
        "tell_story": 1.20,
        "complain": 1.15,
        "ask_question": 1.00,
    },
    "closing": {
        "end_conversation": 10.00,
        "acknowledge": 1.80,
        "respond": 0.60,
        "ask_question": 0.08,
        "share": 0.35,
        "tell_story": 0.25,
        "joke": 0.30,
        "tease": 0.20,
        "celebrate": 0.20,
        "disagree": 0.15,
        "complain": 0.40,
    },
    "silence": {
        "acknowledge": 2.60,
        "share": 1.60,
        "tell_story": 0.90,
        "respond": 1.00,
        "ask_question": 0.50,
        "end_conversation": 1.50,
    },
    "unknown": {
        "respond": 1.30,
        "acknowledge": 1.20,
        "ask_question": 0.70,
    },
}

#: 生理状态阈值（§4.3 mood 规则）
_LOW_ENERGY = 0.35
_HIGH_STRESS = 0.65
_LOW_BATTERY = 0.30
_HIGH_BATTERY = 0.80
#: ``end_conversation`` 的放大闸门：只有这些情境才「放大」，其余仍是低权重非 0
_END_GATE_FACTOR = 6.0
_END_GATE_INTENTS: tuple[str, ...] = ("closing", "silence")


def _mood_multipliers(energy: float, stress: float, battery: float) -> dict[str, float]:
    """情绪/体力 → 行为乘数（振幅小、只调倾向，不做数值游戏）。"""
    mult: dict[str, float] = {}
    if energy < _LOW_ENERGY:
        mult.update(
            {
                "joke": 0.30,
                "celebrate": 0.12,
                "ask_question": 0.45,
                "acknowledge": 1.70,
                "complain": 1.90,
                "end_conversation": 1.30,
                "share": 1.10,
            }
        )
    if stress > _HIGH_STRESS:
        mult.update(
            {
                "complain": mult.get("complain", 1.0) * 1.60,
                "disagree": mult.get("disagree", 1.0) * 1.40,
                "joke": mult.get("joke", 1.0) * 0.45,
                "celebrate": mult.get("celebrate", 1.0) * 0.50,
                "comfort": 0.75,  # 自己都绷着，很难照顾别人
                "end_conversation": mult.get("end_conversation", 1.0) * 1.25,
            }
        )
    if battery < _LOW_BATTERY:
        mult.update(
            {
                "end_conversation": mult.get("end_conversation", 1.0) * 4.00,
                "acknowledge": mult.get("acknowledge", 1.0) * 1.60,
                "share": mult.get("share", 1.0) * 0.45,
                "tell_story": mult.get("tell_story", 1.0) * 0.40,
                "ask_question": mult.get("ask_question", 1.0) * 0.60,
                "change_topic": mult.get("change_topic", 1.0) * 0.70,
            }
        )
    elif battery > _HIGH_BATTERY:
        mult.update({"share": 1.15, "ask_question": 1.15, "end_conversation": 0.60})
    return mult


def _period_multipliers(period: str) -> dict[str, float]:
    """当地时间/作息 → 极小的乘数（深夜别追问、更容易收尾）。"""
    key = str(period or "").strip().lower()
    if key in ("late_night", "sleeping"):
        return {"ask_question": 0.60, "end_conversation": 1.60, "joke": 0.80, "share": 0.90}
    if key == "early_morning":
        return {"ask_question": 0.80, "respond": 1.05}
    if key == "night":
        return {"share": 1.05, "end_conversation": 1.10}
    return {}


def choose_behavior(
    inp: BehaviorInput,
    *,
    rng: random.Random | None = None,
) -> BehaviorChoice:
    """这一轮选一个行为（零 LLM，纯加权抽样）。

    计算顺序（对应 §4.3，也方便调试时逐层看）：

    1. **基础权重**：DNA traits 决定无情境先验（``_base_weights``）。
    2. **上下文乘数**：首轮 / open loop / 熟稔度 / 作息 / 当前活动。
    3. **intent 乘数**：这一轮用户说了什么（§4.3 的 intent 规则表）。
    4. **mood 乘数**：energy / stress / social_battery。
    5. **end_conversation 闸门**：非收尾情境保持低权重但绝不归零。
    6. **历史惩罚**：``behavior_history_penalty``（连续 ask_question 强力压制）。
    7. **下限保护**：``respond`` 至少 :data:`RESPOND_FLOOR`；ask_question 受 initiative 上限约束。
    8. **归一化 + 抽样**（sum ≈ 1，``rng=None`` 时内部 ``random.Random()``）。

    >>> choice = choose_behavior(BehaviorInput(dna_traits={}), rng=random.Random(42))
    >>> choice.behavior in BEHAVIORS
    True
    """
    rng = rng if isinstance(rng, random.Random) else random.Random()
    traits = normalize_traits(inp.dna_traits)
    intent = detect_intent(inp.user_text)

    weights = _base_weights(traits, inp)
    energy, stress, battery, mood_label = _mood_values(inp.mood)
    closeness_f = (_closeness(inp.closeness) - 1) / 4.0
    recent = _safe_recent(inp.recent_behaviors)

    # ---- 2. 上下文乘数（全部是温和修正） -------------------------------- #
    context_mult: dict[str, float] = {}
    if inp.is_first_turn:
        context_mult.update(
            {"respond": 1.40, "acknowledge": 1.20, "share": 0.75, "ask_question": 0.80,
             "end_conversation": 0.40}
        )
    if inp.has_due_open_loop:
        context_mult.update(
            {"ask_question": 2.20, "respond": 1.10, "acknowledge": 0.80, "share": 0.90}
        )
    # 越熟越少「采访式追问」，也越不容易收尾
    context_mult["ask_question"] = context_mult.get("ask_question", 1.0) * (1.15 - 0.30 * closeness_f)
    context_mult["end_conversation"] = context_mult.get("end_conversation", 1.0) * (1.10 - 0.20 * closeness_f)
    if inp.current_activity:
        context_mult["share"] = context_mult.get("share", 1.0) * 1.15
        context_mult["tell_story"] = context_mult.get("tell_story", 1.0) * 1.10
    _accumulate(context_mult, _period_multipliers(inp.time_period))
    if int(inp.turn_index or 0) >= 14:
        context_mult["end_conversation"] = context_mult.get("end_conversation", 1.0) * 1.25

    _apply(weights, context_mult)

    # ---- 3. intent 乘数 ------------------------------------------------ #
    _apply(weights, _INTENT_MULTIPLIERS.get(intent, {}))

    # ---- 4. mood 乘数 -------------------------------------------------- #
    _apply(weights, _mood_multipliers(energy, stress, battery))

    # ---- 5. end_conversation 闸门 -------------------------------------- #
    if intent in _END_GATE_INTENTS or battery < 0.25:
        weights["end_conversation"] *= _END_GATE_FACTOR
    else:
        weights["end_conversation"] *= 1.0  # 保持低权重但非 0（«允许结束»）

    # ---- 6. 历史惩罚 ---------------------------------------------------- #
    for name in BEHAVIORS:
        weights[name] *= behavior_history_penalty(recent, name)

    # ---- 7. 下限 / 上限保护 --------------------------------------------- #
    weights["respond"] = max(weights["respond"], RESPOND_FLOOR)
    # ask_question 的上限受 initiative 约束：低主动型人物不可能靠情境堆出高频提问
    ask_cap = 0.35 + 1.15 * traits["initiative"]
    weights["ask_question"] = min(weights["ask_question"], ask_cap)

    # Variation must not override the conversational obligation. A weighted lottery
    # must not celebrate bereavement, start a story after goodbye, or dodge a question.
    allowed = {
        "closing": {"end_conversation", "acknowledge", "respond"},
        "question": {"respond", "share", "tell_story", "ask_question"},
        "bad_news": {"empathize", "comfort", "respond", "acknowledge", "ask_question"},
    }.get(intent)
    if allowed is not None:
        weights = {name: weight if name in allowed else 0.0 for name, weight in weights.items()}

    # ---- 8. 归一化 + 抽样 ---------------------------------------------- #
    cleaned = {name: max(0.0, float(weights.get(name, 0.0))) for name in BEHAVIORS}
    total = sum(cleaned.values())
    if total <= 0:  # 理论不可达（respond 有下限），保险起见
        cleaned = {name: 0.0 for name in BEHAVIORS}
        cleaned["respond"] = 1.0
        total = 1.0
    normalized = {name: value / total for name, value in cleaned.items()}
    behavior = _sample(normalized, rng)

    return BehaviorChoice(
        behavior=behavior,
        reason=_reason(intent, mood_label, inp, recent, behavior, normalized[behavior]),
        confidence=round(normalized[behavior], 6),
        weights=normalized,
    )


def _accumulate(target: dict[str, float], extra: Mapping[str, float]) -> None:
    """把 ``extra`` 乘进 ``target``（同键相乘）。"""
    for key, value in extra.items():
        target[key] = target.get(key, 1.0) * float(value)


def _apply(weights: dict[str, float], multipliers: Mapping[str, float]) -> None:
    """把一组乘数作用到权重上（同键相乘；未知键忽略）。"""
    for name, factor in multipliers.items():
        if name not in weights:
            continue
        weights[name] *= max(0.0, float(factor))


def _sample(weights: Mapping[str, float], rng: random.Random) -> str:
    """按固定 BEHAVIORS 顺序累积抽样（给同一 rng 可完全复现）。"""
    total = sum(max(0.0, weights.get(name, 0.0)) for name in BEHAVIORS)
    if total <= 0:
        return "respond"
    roll = rng.random() * total
    acc = 0.0
    last_positive = "respond"
    for name in BEHAVIORS:
        value = max(0.0, weights.get(name, 0.0))
        if value <= 0:
            continue
        last_positive = name
        acc += value
        if roll < acc:
            return name
    return last_positive


def _reason(
    intent: str,
    mood_label: str,
    inp: BehaviorInput,
    recent: tuple[str, ...],
    behavior: str,
    probability: float,
) -> str:
    """给日志/调试的一行可解释理由（确定性，不含随机数）。"""
    parts = [f"intent={intent}", f"mood={mood_label or 'neutral'}"]
    parts.append(f"closeness=Lv{_closeness(inp.closeness)}")
    if inp.has_due_open_loop:
        parts.append("open_loop")
    if int(inp.turn_index or 0) > 0:
        parts.append(f"turn={int(inp.turn_index)}")
    if recent:
        parts.append("recent=" + ",".join(recent[-HISTORY_WINDOW:]))
    parts.append(f"picked={behavior}")
    parts.append(f"p={probability:.3f}")
    return "; ".join(parts)


# --------------------------------------------------------------------------- #
# 给 LLM 的本轮指令 behavior_directive
# --------------------------------------------------------------------------- #
#: 行为 → 一句具体动作要求（英文，直接进 system prompt）
_BEHAVIOR_ACTIONS: dict[str, str] = {
    "respond": "answer or react to the specific thing they said; that alone can be enough.",
    "share": "if relevant, share one established detail or your present opinion; do not invent an event.",
    "joke": "a brief joke is an option if their mood and the topic invite it; no forced punchline.",
    "tease": "gently tease them about what they just said; keep it affectionate, never mean.",
    "empathize": "react to what happened in your own voice; no feeling labels or stock validation needed.",
    "comfort": "stay with their situation; brief company can be enough, advice only if wanted.",
    "disagree": "give your actual view; disagree only if there is a real point of disagreement.",
    "complain": "a small complaint about an established situation is an option if relevant; don't hijack their news.",
    "celebrate": "react to their good news at a scale that fits; a few delighted words can be enough.",
    "tell_story": "use a relevant recorded experience if one exists; otherwise just respond, without inventing a story.",
    "change_topic": "only move on if the current topic has run its course and nothing needs an answer.",
    "acknowledge": "give a short reaction; one or two words can be perfect.",
    "end_conversation": "wrap this topic up naturally; a short sign-off is fine; do not ask a question.",
    "ask_question": "one relevant question is an option if something genuinely needs following up; answer them first.",
}

#: verbosity → 长度提示
_LENGTH_HINTS: tuple[tuple[float, str], ...] = (
    (0.20, "usually a few words; expand if the actual answer needs it"),
    (0.40, "usually one short sentence; no minimum length"),
    (0.60, "usually one or two short sentences; a fragment is fine"),
    (0.80, "a few sentences when needed, otherwise shorter"),
    (1.01, "room for detail when engaged; never pad a simple reply"),
)

#: 提问是倾向，不能成为固定收尾，也不能禁止必要的澄清。
_NO_QUESTION_RULE = "Questions are optional; ask only for a real conversational reason, never to keep them replying."
_ASK_QUESTION_RULE = (
    "Questions are optional even here; skip one if they are winding down or have already answered it."
)


def _length_hint(behavior: str, verbosity: float) -> str:
    """由 verbosity 决定长度提示；acknowledge / end_conversation 有专门覆盖。"""
    if behavior == "acknowledge":
        return "a very short reaction is enough; one or two words can be perfect"
    if behavior == "end_conversation":
        return "keep it brief; a short sign-off is enough"
    if behavior == "ask_question":
        return "keep it short; a follow-up does not need a lead-in paragraph"
    for threshold, hint in _LENGTH_HINTS:
        if verbosity < threshold:
            return hint
    return _LENGTH_HINTS[-1][1]


def _style_hints(traits: dict[str, float]) -> list[str]:
    """从 DNA 数值里挑至多 3 条风格提示（确定性顺序）。"""
    hints: list[str] = []
    if traits["humour"] >= 0.65:
        hints.append("your humour can show, but don't force a punchline")
    elif traits["humour"] <= 0.25:
        hints.append("no joke this turn")
    if traits["directness"] >= 0.70:
        hints.append("be blunt; don't over-soften it")
    elif traits["directness"] <= 0.25:
        hints.append("keep the wording gentle")
    if traits["social_distance"] >= 0.70:
        # social_distance 越大越亲近（见模块 docstring / speech_dna.dna_traits）
        hints.append("be familiar; skip the formalities")
    elif traits["social_distance"] <= 0.25:
        hints.append("keep a bit of distance; polite, not overly familiar")
    if traits["emoji"] >= 0.70:
        hints.append("at most one emoji")
    elif traits["emoji"] <= 0.15:
        hints.append("no emoji")
    if traits["slang"] >= 0.70:
        hints.append("casual slang is fine")
    elif traits["slang"] <= 0.15:
        hints.append("no slang")
    if traits["disclosure"] >= 0.70:
        hints.append("it's fine to reveal something about yourself")
    return hints[:3]


def behavior_directive(
    choice: BehaviorChoice,
    *,
    dna_traits: dict[str, float],
    language: str = "en",
) -> str:
    """把选中的行为翻译成**给主 LLM 的一小段英文指令**（拼进 system prompt）。

    硬性要求（§4.3）：

    - 写明本轮行为标签 + 一句具体动作要求；
    - 行为仅作为倾向，当前消息的需要优先，提问可选；
    - 长度提示来自 ``verbosity``；
    - ``acknowledge`` 明确写「一两个词也可以」；
    - ``end_conversation`` 明确写「自然收尾、不要提问」。

    >>> d = behavior_directive(
    ...     BehaviorChoice("share", "r", 1.0, {}), dna_traits={}, language="en")
    >>> "Questions are optional" in d
    True
    """
    traits = normalize_traits(dna_traits)
    behavior = str(getattr(choice, "behavior", "") or "").strip()
    if behavior not in BEHAVIORS:
        behavior = "respond"

    action = _BEHAVIOR_ACTIONS[behavior]
    length = _length_hint(behavior, traits["verbosity"])
    styles = _style_hints(traits)

    lines = [f"[This turn] Possible behavior: {behavior}.", f"Action: {action}"]
    lines.append("Their actual message takes priority over this tendency; skip it if it doesn't fit.")
    lines.append(f"Length: {length}.")
    if styles:
        lines.append("Style: " + "; ".join(styles) + ".")

    if behavior == "ask_question":
        lines.append(_ASK_QUESTION_RULE)
    else:
        lines.append(_NO_QUESTION_RULE)

    if behavior == "end_conversation":
        lines.append("Do not ask anything at the end; let the conversation rest here.")
    elif behavior == "acknowledge":
        lines.append("It is completely fine if your whole reply is just a word or two.")

    lines.append(_language_line(language))
    lines.append(
        "Output only the message itself — no stage directions, no analysis, no quotes."
    )
    return "\n".join(lines)


def _language_line(language: str) -> str:
    """语言提示（指令本身用英文，因为英文元指令对 LLM 最稳）。"""
    key = str(language or "").strip().lower()
    if not key or key in ("en", "en-us", "en-gb", "english"):
        return "Write the reply in English."
    if key.startswith("zh") or key in ("chinese", "cn", "中文"):
        return "Write the reply in Chinese (中文)."
    return f"Write the reply in {language}."


__all__ = [
    "ASK_QUESTION_EXTRA",
    "BEHAVIORS",
    "BehaviorChoice",
    "BehaviorInput",
    "DEFAULT_TRAITS",
    "END_BASE_WEIGHT",
    "HISTORY_WINDOW",
    "INTENTS",
    "RECENCY_DECAY",
    "RESPOND_FLOOR",
    "TRAIT_KEYS",
    "behavior_directive",
    "behavior_history_penalty",
    "choose_behavior",
    "detect_intent",
    "normalize_traits",
]
