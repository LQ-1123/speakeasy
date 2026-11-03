"""离线规则版 LLM（无 API key 时的演示后端）。

设计目标
--------
1. **不联网**：纯模板 + 正则，零依赖、零延迟抖动。
2. **按角色风格**：从 system prompt 里识别 Oliver / Raj / Marco / Emily / Priya，
   各自的口头禅与句式不同（见 ``_PERSONA_LINES``）。
3. **按话题回应**：从最后一条 user 消息里抓关键词（点单 / API / 设计 / 面试 / 排期），
   命中对应模板；都不命中时用角色兜底句。
4. **流式**：按"词"逐个 yield（每个 token 间隔 :data:`TOKEN_DELAY_S` 秒），
   让 :class:`app.llm.sentence_splitter.StreamingSentenceSplitter` 与首包延迟逻辑
   在无 key 环境下也能被真实验证。

对外接口（契约外的补充，仅本模块使用）::

    compose_reply(messages) -> str
    async mock_stream_chat(messages) -> AsyncIterator[str]
    async mock_complete_json(messages, *, schema_hint="") -> str
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import AsyncIterator, Iterable, Sequence

from app.logging_utils import get_logger

log = get_logger(__name__)

#: 每个流式 token 之间的间隔（秒）。测试可 monkeypatch 成 0。
TOKEN_DELAY_S: float = 0.01

# --------------------------------------------------------------------------- #
# 角色识别
# --------------------------------------------------------------------------- #
_PERSONA_NAMES = ("oliver", "raj", "marco", "emily", "priya")

#: 每个角色的口头禅（首句开头），用于让前端一听就知道换了人。
PERSONA_CATCHPHRASES: dict[str, str] = {
    "oliver": "Right then…",
    "raj": "Actually…, na",
    "marco": "Allora…, no?",
    "emily": "Okay so…",
    "priya": "Can, can. But…",
}

# 话题关键词 → 话题 id
_TOPIC_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "coffee",
        (
            "coffee", "flat white", "latte", "espresso", "cappuccino", "cup", "drink",
            "order", "takeaway", "pastry", "croissant", "milk", "tea", "barista", "café", "cafe",
        ),
    ),
    (
        "food",
        (
            "lunch", "food", "eat", "hungry", "recommend", "menu", "sandwich", "breakfast",
            "dinner", "dish", "taste", "delicious", "recipe", "restaurant", "snack",
        ),
    ),
    (
        "api",
        (
            "api", "deploy", "endpoint", "server", "database", "cache", "redis", "bug",
            "code", "backend", "release", "migration", "retry", "latency", "queue",
            "rollback", "roll back", "test", "service", "idempotent",
        ),
    ),
    (
        "design",
        (
            "design", "colour", "color", "poster", "font", "typography", "shape",
            "layout", "logo", "brand", "palette", "visual", "look", "ugly", "beautiful",
        ),
    ),
    (
        "interview",
        (
            "interview", "experience", "job", "resume", "cv", "role", "hire", "hiring",
            "salary", "company", "career", "candidate", "team", "background",
        ),
    ),
    (
        "scope",
        (
            "scope", "sprint", "deadline", "priority", "ship", "requirement", "roadmap",
            "feature", "timeline", "launch", "milestone", "plan", "estimate", "resourcing",
        ),
    ),
    (
        "farewell",
        (
            "bye", "goodbye", "go now", "got to go", "have to go", "see you", "thanks a lot",
            "thank you", "that's all", "cheers then", "take care", "later",
        ),
    ),
    (
        "smalltalk",
        (
            "how are you", "how's it going", "weather", "weekend", "music", "film", "movie",
            "football", "travel", "holiday", "tired", "busy", "morning", "evening",
        ),
    ),
)

# --------------------------------------------------------------------------- #
# 回复模板：persona -> topic -> 候选回复
# 约束：1-2 句、<= 35 词、带该角色口头禅、纯口语、无 markdown。
# --------------------------------------------------------------------------- #
_PERSONA_LINES: dict[str, dict[str, tuple[str, ...]]] = {
    "oliver": {
        "coffee": (
            "Right then… a flat white it is. Oat or whole milk, and are you sitting in or taking it away?",
            "Right then… one flat white coming up. We've got a lovely Ethiopian on today if you fancy something a bit fruitier?",
            "Right then… that's a tenner for the coffee and the pastry, cheers. You want it in a takeaway cup?",
        ),
        "api": (
            "Right then… I'm only a barista, but I'd say ship the small thing first. Have you got someone keeping an eye on it?",
            "Right then… sounds a bit above my pay grade. What happens if it goes wrong, do you just switch it off?",
        ),
        "design": (
            "Right then… I'm no designer, but that looks quite nice to me. What's the bit you're not sure about?",
            "Right then… honestly? It's a bit busy. I'd lose one of those little things in the corner.",
        ),
        "interview": (
            "Right then… just be yourself, that's how I got this job, more or less. What did they ask you first?",
            "Right then… don't overthink it. Have you got a story about a time you actually sorted something out?",
        ),
        "scope": (
            "Right then… pick the one thing that matters and sack the rest off. Have you got a bit of slack in the week?",
            "Right then… that sounds like a lot for one go. What's the thing that has to be done by Friday?",
        ),
        "_default": (
            "Right then… that's fair enough. Tell me a bit more and I'll see what I can do.",
            "Right then… I'm with you. Go on, what happened next?",
        ),
        "food": (
            "Right then… for a quick lunch I'd do the ham and cheese toastie, not the soup, it's gone a bit thin. Want me to stick one on?",
            "Right then… the pastries are all right, but honestly the falafel place next door does a proper lunch. Give it ten minutes though.",
        ),
        "farewell": (
            "Right then… no worries at all. Cheers, and mind how you go.",
            "Right then… lovely to see you. Take it easy, yeah?",
        ),
        "smalltalk": (
            "Right then… can't complain, bit of a rush on this morning. How's your day going?",
            "Right then… same as ever, really. Coffee helps. What about you, busy day?",
        ),
    },
    "raj": {
        "coffee": (
            "Actually…, na, coffee first, then standup. One small thing only, is that machine even working today?",
            "Actually…, na, I'll take a filter coffee, same to same as yesterday. No sugar, yaar.",
        ),
        "api": (
            "Actually…, na, the deploy looks fine, but what's the retry policy on that endpoint? If it's not idempotent we'll get duplicates only.",
            "Actually…, na, that API returns too many fields. Trim it, keep it same to same, just smaller, no?",
            "Actually…, na, one small thing only: who owns the migration? Somebody has to own it, yaar.",
        ),
        "design": (
            "Actually…, na, the design is not my area, but the data behind it — that I can review. What's the shape of the payload?",
        ),
        "interview": (
            "Actually…, na, your experience is good, yaar. Give me one number: how many users did that service handle?",
            "Actually…, na, that answer was too generic. Tell me what you personally shipped, not the team.",
        ),
        "scope": (
            "Actually…, na, scope is too big for this sprint itself. Can we prepone the small piece and push the rest, no?",
            "Actually…, na, give me a date and a number and we're done. Otherwise it's just talk only.",
        ),
        "_default": (
            "Actually…, na, that's fine. One small thing only — can you give me a concrete example?",
            "Actually…, na, I'm not fully convinced, but go on. What's the number behind that?",
        ),
        "food": (
            "Actually…, na, for lunch the dal place downstairs is best only. They finish by two, so go now, yaar.",
            "Actually…, na, I'll do a dosa, same to same as everyday. Quick and it works only.",
        ),
        "farewell": (
            "Actually…, na, fine, go. We'll pick it up tomorrow, no problem.",
            "Actually…, na, thanks for the time, yaar. Ping me if anything breaks.",
        ),
        "smalltalk": (
            "Actually…, na, too many meetings only today. Yours is the only one I'm enjoying, so — carry on.",
            "Actually…, na, busy busy, but it's like that only. What about you, things okay?",
        ),
    },
    "marco": {
        "coffee": (
            "Allora…, no? Coffee here is a religion. You sit, you drink, you don't walk with the cup — bad luck, yes?",
            "Allora…, no? An espresso, and you take your time. Otherwise what's the point?",
        ),
        "api": (
            "Allora…, no? I don't know about APIs, but this one feels rigid. Where's the space to breathe?",
        ),
        "design": (
            "Allora…, no? The colour is too much. But the shape, the shape is beautiful. Be honest with me.",
            "Allora…, no? You hate it. I can see it in your face. Dai, tell me why.",
            "Allora…, no? Different font, same idea, piano piano. Nothing else changes.",
        ),
        "interview": (
            "Allora…, no? Interviews are theatre. You tell them the story, they believe it. What's your story?",
            "Allora…, no? Don't list things. Tell them one thing that mattered, and make it feel real.",
        ),
        "scope": (
            "Allora…, no? Scope like that and no deadline? Piano piano — one thing, done beautifully.",
            "Allora…, no? Basta. Pick the thing that makes people feel something, drop the rest.",
        ),
        "_default": (
            "Allora…, no? I hear you. But tell me honestly — how does that actually feel?",
            "Allora…, no? That's interesting, but it's not an answer yet. Dai, take a position.",
        ),
        "food": (
            "Allora…, no? For lunch, cacio e pepe. Three ingredients, and if it's wrong you can taste it. Why do people complicate?",
            "Allora…, no? You don't order a sandwich at one o'clock and walk. You sit. Dai, twenty minutes, that's nothing.",
        ),
        "farewell": (
            "Allora…, no? You're leaving already? Va bene — but next time we drink properly, yes?",
            "Allora…, no? Go, go. It was good, this. Ciao, and be careful on that scooter.",
        ),
        "smalltalk": (
            "Allora…, no? Me? I'm fighting with a client about a logo. Tiny thing, a dot. Three days. How's your week?",
            "Allora…, no? Tired, but good tired. Rome does that — beautiful and exhausting, same to same.",
        ),
    },
    "emily": {
        "coffee": (
            "Okay so…, a coffee run is basically a personality test. Anyway — tell me a little about yourself first.",
            "Okay so…, got it. Flat white, and then walk me through what you've been working on.",
        ),
        "api": (
            "Okay so…, got it. That's a solid technical piece — can you give me one specific example of a hard call you made?",
            "Okay so…, right off the bat, how did you decide what to ship and what to cut?",
        ),
        "design": (
            "Okay so…, got it. To be fair, tell me about a project you owned end to end, design through launch.",
        ),
        "interview": (
            "Okay so…, walk me through that role — what did you actually own day to day?",
            "Okay so…, got it. Can you give me a specific example of a result, with a number?",
            "Okay so…, let's tighten that up. What's the one-sentence version?",
        ),
        "scope": (
            "Okay so…, got it. How did you decide what was in scope and what was out, right off the bat?",
            "Okay so…, to be fair, that's a lot. What was the one thing that actually moved the number?",
        ),
        "_default": (
            "Okay so…, got it. Can you give me a specific example of that?",
            "Okay so…, that makes sense. Let's unpack that — what was your actual role there?",
        ),
        "food": (
            "Okay so…, we can grab something after this. But real talk — where do you see yourself in two years?",
            "Okay so…, got it, lunch. Before we go — what's the one thing you'd want to own here?",
        ),
        "farewell": (
            "Okay so…, thanks for hopping on. We'll circle back early next week — sound good?",
            "Okay so…, appreciate the time. I'll be in touch either way, alright?",
        ),
        "smalltalk": (
            "Okay so…, busy season, but I can't complain. How's your week treating you?",
            "Okay so…, honestly? Back-to-back calls. But you're the interesting part of my day, so — go on.",
        ),
    },
    "priya": {
        "coffee": (
            "Can, can. But… we're reviewing scope at ten, so quick one — what's your update?",
            "Can, can. But… coffee after the standup, not before, okay?",
        ),
        "api": (
            "Can, can. But… who signs off on the rollback plan, ah? I need a name, not a team.",
            "Can, can. But… if we deploy Thursday, when does QA happen? By Friday, ah?",
        ),
        "design": (
            "Can, can. But… the design review is Thursday, so can you confirm by then, okay?",
        ),
        "interview": (
            "Can, can. But… for this role, can you commit to the timeline, or cannot?",
        ),
        "scope": (
            "Can, can. But… this sprint is already full, so what do we cut, okay?",
            "Can, can. But… I need one number and one date. So — can, or cannot?",
        ),
        "_default": (
            "Can, can. But… give me one line: what's the ask, and by when?",
            "Can, can. But… so — can, or cannot? Just tell me straight, ah.",
        ),
        "food": (
            "Can, can. But… lunch at one, sharp, because the review starts at two. Can?",
            "Can, can. But… downstairs only, twenty minutes, then we're back. Okay?",
        ),
        "farewell": (
            "Can, can. But… send me the notes by tonight, ah. Then we're done.",
            "Can, can. But… okay, go. Same time tomorrow — can?",
        ),
        "smalltalk": (
            "Can, can. But… quick one, because I'm double-booked. What's your update?",
            "Can, can. But… tired lah, this quarter is brutal. Yours okay?",
        ),
    },
    "_neutral": {
        "_default": (
            "Got it — that makes sense. Tell me a bit more about that.",
            "Fair enough. What happened next?",
        ),
        "food": (
            "Good question — I'd go for something warm and quick. What are you in the mood for?",
        ),
        "farewell": (
            "No worries at all — good talking to you. Take care!",
        ),
        "smalltalk": (
            "Not bad at all, thanks. How about you — busy day?",
        ),
    },
}


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def _message_text(messages: Sequence[object]) -> list[tuple[str, str]]:
    """把 ChatMessage / dict 统一成 ``[(role, content), ...]``。"""
    out: list[tuple[str, str]] = []
    for m in messages:
        if isinstance(m, dict):
            role = str(m.get("role", "user"))
            content = str(m.get("content", "") or "")
        else:
            role = str(getattr(m, "role", "user"))
            content = str(getattr(m, "content", "") or "")
        out.append((role, content))
    return out


def detect_persona(messages: Sequence[object]) -> str:
    """从 system prompt 里猜角色 id；猜不到返回 ``""``。"""
    system = " ".join(c for r, c in _message_text(messages) if r == "system").lower()
    for name in _PERSONA_NAMES:
        if name in system:
            return name
    return ""


def last_user_text(messages: Sequence[object]) -> str:
    for role, content in reversed(_message_text(messages)):
        if role == "user" and content.strip():
            return content.strip()
    return ""


def detect_topic(text: str) -> str:
    """按关键词投票选出话题 id；无命中返回 ``"_default"``。"""
    low = (text or "").lower()
    best, best_hits = "_default", 0
    for topic, keywords in _TOPIC_HINTS:
        hits = sum(1 for kw in keywords if kw in low)
        if hits > best_hits:
            best, best_hits = topic, hits
    return best


def _pick(options: Iterable[str], seed_text: str, *, avoid: Iterable[str] = ()) -> str:
    """确定性选择：同样的输入 + 同样的历史 → 同样的回复（便于测试与回放）。

    ``avoid`` 里是"最近刚说过的话"。先在候选池里剔除它们；
    若全被剔除（模板太少），退化为完整池，但**保证结果不等于最近一条**。

    这样连续相似的输入不会复读同一句——真实对话里不会出现一模一样的重复。
    """
    pool = list(options)
    if not pool:  # pragma: no cover - 模板表已保证非空
        return ""

    avoid_set = {a for a in avoid if a}
    fresh = [p for p in pool if p not in avoid_set] if avoid_set else pool

    key = sum(ord(c) for c in seed_text) if seed_text else 0
    if fresh:
        return fresh[key % len(fresh)]

    # 全被用过：换个索引，确保与最近一条不同（模板数 >= 2 时一定成立）
    last = next(iter(avoid_set), None)
    if len(pool) >= 2 and last is not None:
        start = pool.index(last) if last in pool else 0
        return pool[(start + 1) % len(pool)]
    return pool[key % len(pool)]


def _recent_assistant_text(messages: Sequence[object], limit: int = 6) -> str:
    """取最近几条 assistant 回复，用于避免复读。"""
    said = [c for r, c in _message_text(messages) if r == "assistant" and c.strip()]
    return " ".join(said[-limit:])


def _recent_assistant_lines(messages: Sequence[object], limit: int = 4) -> list[str]:
    """最近几条 assistant 回复的原文列表（精确匹配用）。"""
    said = [c.strip() for r, c in _message_text(messages) if r == "assistant" and c.strip()]
    return said[-limit:]


def compose_reply(messages: Sequence[object]) -> str:
    """根据 system prompt + 最后一条 user 消息生成一条规则回复。"""
    persona = detect_persona(messages) or "_neutral"
    user = last_user_text(messages)
    # 把"自己刚说过的话"并入 seed，让相同话题在不同轮次给出不同模板
    seed = user + "|" + _recent_assistant_text(messages)
    avoid = _recent_assistant_lines(messages)

    # 学习者用中文提问时，仍用英文接话（与 personas 的全局规则一致）
    if re.search(r"[\u4e00-\u9fff]", user):
        table = _PERSONA_LINES.get(persona, _PERSONA_LINES["_neutral"])
        return _pick(table.get("_default", ()), seed, avoid=avoid)

    topic = detect_topic(user)
    table = _PERSONA_LINES.get(persona, _PERSONA_LINES["_neutral"])
    options = table.get(topic) or table.get("_default") or ("Tell me a bit more about that.",)
    return _pick(options, seed, avoid=avoid)


def tokenize_stream(text: str) -> list[str]:
    """把整句切成"词 + 前置空格"的流式 token 序列（最后一个词不带尾空格）。"""
    words = [w for w in text.split(" ") if w != ""]
    if not words:
        return []
    return [words[0]] + [" " + w for w in words[1:]]


async def mock_stream_chat(messages: Sequence[object]) -> AsyncIterator[str]:
    """规则版流式对话：词级增量 + 小间隔 sleep。"""
    reply = compose_reply(messages)
    if not reply:  # pragma: no cover - 防御性
        return
    for i, token in enumerate(tokenize_stream(reply)):
        if i > 0 and TOKEN_DELAY_S > 0:
            await asyncio.sleep(TOKEN_DELAY_S)
        yield token


# --------------------------------------------------------------------------- #
# 规则版"润色建议"（供 mock_complete_json 使用）
# --------------------------------------------------------------------------- #
# (正则, 替换(可用 \1), 中文解释, register)
_SUGGESTION_RULES: tuple[tuple[re.Pattern[str], str, str, str], ...] = (
    (
        re.compile(r"\bI very (like|love|enjoy)\b", re.IGNORECASE),
        r"I really \1",
        "very 不能直接修饰动词；说 really like / be into 才自然。",
        "casual",
    ),
    (
        re.compile(r"\bI want\b", re.IGNORECASE),
        "I'd like",
        "点单或提请求时 I'd like 比 I want 更礼貌，是母语者的默认说法。",
        "neutral",
    ),
    (
        re.compile(r"\bplease give me (a|some) feedback\b", re.IGNORECASE),
        "could you give me some feedback",
        "feedback 不可数不能说 a feedback；请求用 could you 更客气。",
        "formal",
    ),
    (
        re.compile(r"\bI am agree\b", re.IGNORECASE),
        "I agree",
        "agree 是动词，前面不加 be；直接说 I agree 就对。",
        "neutral",
    ),
    (
        re.compile(r"\bdiscuss about\b", re.IGNORECASE),
        "discuss",
        "discuss 是及物动词，后面不加 about。",
        "formal",
    ),
    (
        re.compile(r"\bhow to (say|do|make|use)\b", re.IGNORECASE),
        r"how do I \1",
        "口语提问用 how do I…，how to… 更像标题而不是问句。",
        "neutral",
    ),
    (
        re.compile(r"\bvery very\b", re.IGNORECASE),
        "really",
        "连用两个 very 显得生硬，母语者更常用 really 或 quite。",
        "casual",
    ),
    (
        re.compile(r"\bcan you help me to (\w+)\b", re.IGNORECASE),
        r"could you help me \1",
        "help 后面直接接动词；could you 比 can you 更委婉。",
        "neutral",
    ),
    (
        re.compile(r"\bI have (\w+) years? experience\b", re.IGNORECASE),
        r"I've got \1 years of experience",
        "experience 前要加 of，口语里 I've got 比 I have 更自然。",
        "formal",
    ),
    (
        re.compile(r"\bin my opinion\b", re.IGNORECASE),
        "I'd say",
        "口语中 I'd say 比 in my opinion 更轻、更像聊天。",
        "casual",
    ),
    (
        re.compile(r"\bI think that (it is|it's)\b", re.IGNORECASE),
        "I'd say it's",
        "I think that… 偏书面，口语常用 I'd say / I reckon。",
        "casual",
    ),
)

_CONTRACTION_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"\bI am\b"), "I'm", "口语中 I am 会缩成 I'm，听起来更自然。"),
    (re.compile(r"\bdo not\b"), "don't", "否定句口语里几乎总是缩写：don't。"),
    (re.compile(r"\bit is\b"), "it's", "it is 在口语里通常缩成 it's。"),
    (re.compile(r"\bI would\b"), "I'd", "I would 在口语里通常缩成 I'd。"),
    (re.compile(r"\bcan not\b"), "can't", "can not 应写作 can't，口语里也更顺。"),
)


def mock_suggestions(user_text: str) -> list[dict[str, str]]:
    """规则版润色：返回符合 PROTOCOL ``suggestion.items`` 结构的列表。"""
    text = (user_text or "").strip()
    if len(text) < 3:
        return []
    items: list[dict[str, str]] = []
    for pattern, replacement, why, register in _SUGGESTION_RULES:
        match = pattern.search(text)
        if not match:
            continue
        better = text[: match.start()] + match.expand(replacement) + text[match.end() :]
        better = " ".join(better.split())
        items.append(
            {
                "original": match.group(0).strip(),
                "better": better,
                "why": why,
                "register": register,
            }
        )
        if len(items) >= 2:
            return items
    if items:
        return items

    # 没有命中搭配问题 → 只检查缩写；改不动就老实返回空数组
    contracted = text
    applied: list[str] = []
    for pattern, replacement, _why in _CONTRACTION_RULES:
        if pattern.search(contracted):
            contracted = pattern.sub(replacement, contracted)
            applied.append(replacement)
    if applied and contracted != text:
        return [
            {
                "original": text,
                "better": " ".join(contracted.split()),
                "why": "口语里更常用缩写形式（I'm / don't / it's），念出来更顺。",
                "register": "casual",
            }
        ]
    return []


async def mock_complete_json(messages: Sequence[object], *, schema_hint: str = "") -> str:
    """规则版 JSON 补全（用于润色建议）。返回**纯 JSON 字符串**。"""
    hint = (schema_hint or "").lower()
    if "original" in hint or "suggest" in hint:
        payload = mock_suggestions(last_user_text(messages))
        return json.dumps(payload, ensure_ascii=False)
    return "{}"


__all__ = [
    "TOKEN_DELAY_S",
    "PERSONA_CATCHPHRASES",
    "compose_reply",
    "detect_persona",
    "detect_topic",
    "last_user_text",
    "mock_complete_json",
    "mock_stream_chat",
    "mock_suggestions",
    "tokenize_stream",
]
