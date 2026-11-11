"""First Message：人物第一次开口的「开场风格」（确定性、零 LLM）。

为什么单独一个模块
------------------
聊天 prompt 可以教模型「像这个人说话」，但第一次开口是**没有上下文**的：
模型最容易退化成客服欢迎语（"Hi! How are you?"）或自我介绍。这里把
「第一个人物会怎么主动开口」提前写好，供三处复用：

1. :func:`first_message` —— 空聊天页面上展示的第一句（前端 payload）；
2. :func:`opening_style_line` —— prompt 里的一行「你第一次开口通常……」；
3. :func:`first_message_variants` —— 管理端/调试时对比同一人物的多种开场。

设计约束
--------
- **确定性**：同一人物 + 同一语言，永远返回同一句（按 persona id 哈希轮转，
  不使用 ``random`` / Python ``hash()``，跨进程稳定）。
- **按人物分桶**：先用 ``ai_config.temperament`` / ``speech_dna`` / 性格关键词
  归到 outgoing / reserved / witty_sarcastic / warm / dry / playful / poetic，
  不同桶的模板完全不同，避免「12 个人一个开场」。
- **语言干净**：en / zh 两套模板（其它语言回退 en）。中文模板里不放英文词，
  英文模板里不放中日韩文字，防止把人物带跑到错误语言。
- **水平低更短**：``level`` 命中「入门/初级/A1-A2/beginner」时用短句池。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: 开场桶。顺序固定，便于调试与测试。
BUCKETS: tuple[str, ...] = (
    "outgoing",
    "reserved",
    "witty_sarcastic",
    "warm",
    "dry",
    "playful",
    "poetic",
)

#: 支持的语言模板（其余语言回退 en，见模块 docstring）。
SUPPORTED_LANGUAGES: tuple[str, ...] = ("en", "zh")

#: 低水平关键词（英文小写 / 中文原样）。命中即用短句池。
_LOW_LEVEL_TOKENS: tuple[str, ...] = (
    "a1", "a2", "beginner", "beginner level", "basic", "elementary", "low",
    "starting", "just started", "novice", "zero",
    "初级", "入门", "零基础", "刚开始", "不熟", "只会一点", "很难", "很差", "一点点",
)

#: 正常长度模板池。``{city}`` / ``{interest}`` 只在英文模板里出现。
_TEMPLATES: dict[str, dict[str, tuple[str, ...]]] = {
    "en": {
        "outgoing": (
            "Just got back from walking around {city}. The light was doing that late-afternoon thing. What's your day been like?",
            "I'm at a café with a coffee going cold and a hundred things to do. What are you up to?",
            "Quick, no-pressure question: {interest} — waste of time or actually good? I have opinions.",
            "You caught me mid-something. I'm somewhere in {city}, slightly lost, and in the mood to talk. What's going on with you?",
            "Say something interesting. I'm bored and {city} is suspiciously quiet today.",
        ),
        "reserved": (
            "Not sure how to start these. I'm around {city} today, taking it slow. What about you?",
            "I had a quiet morning. Still deciding whether that's a good thing. You?",
            "I'm not much of a texter, so bear with me. What's your day like so far?",
            "There's a cat outside my window and it's judging me. That's my news. Yours?",
            "I'm here. Not doing anything exciting. You?",
        ),
        "witty_sarcastic": (
            "I've been productively staring at a wall for ten minutes. How's your day going?",
            "My plan for today was to be productive. Bold of me, honestly.",
            "I'm around {city}, pretending I know where I'm going. You?",
            "Small-talk time: what's the least boring thing that happened to you today?",
            "I was told I should text first. So: here I am, being a person. You?",
        ),
        "warm": (
            "I was just thinking about you — hope today's been kind. What's on your mind?",
            "Just made tea and sat down. Perfect timing to hear how your day went.",
            "You crossed my mind earlier. How are you holding up?",
            "Spent the afternoon around {city} and saw something you'd like. Tell me about your day?",
            "No agenda — just wanted to check in. How's your day going?",
        ),
        "dry": (
            "Coffee. Then more coffee. That's the update. Yours?",
            "Nothing to report. Which is usually when something happens. You?",
            "I'm around {city}, avoiding responsibilities. What's your day doing?",
            "Today has been a long sentence with no full stop. What about you?",
            "I have one biscuit left and no plan. What's going on with you?",
        ),
        "playful": (
            "Important: I found the best {interest} place near {city}. I'll tell you where if you ask nicely.",
            "I'm on a quest for the best coffee in {city}. Current score: not great.",
            "Quick question with no stakes: {interest} — yes or no? I have thoughts.",
            "I did something slightly reckless today. Not telling you until you tell me about your day.",
            "Reporting live from my sofa. Nothing is happening. It's great. You?",
        ),
        "poetic": (
            "The light over {city} turned gold for about four minutes and everyone stopped looking at their phones. What did you see today?",
            "I keep noticing small things today — steam off a cup, a dog waiting by a door. What stuck with you?",
            "Rain all afternoon here. I don't mind it. What's your weather like, inside and out?",
            "I walked home the long way and the city felt like it was holding its breath. What's new with you?",
            "I was thinking how strange it is that we just talk in these little rectangles. Anyway. How's your day?",
        ),
    },
    "zh": {
        "outgoing": (
            "刚在外面转了一圈回来，风有点凉。你今天怎么样？",
            "我正在喝一杯凉掉的咖啡，突然想找人说说话。你在忙什么？",
            "突然想起你，最近怎么样？",
            "今天过得有点乱，想听听你的。",
            "刚忙完一阵，脑子还在转。你今天顺利吗？",
        ),
        "reserved": (
            "我不太会主动开口。今天有点安静，你呢？",
            "刚从午睡里醒过来，还没想好要说什么。你今天过得怎么样？",
            "窗外有只猫一直在看我。这就是今天的新鲜事，你的呢？",
            "没什么特别的事，就是想找个人聊聊。你今天呢？",
            "我在，今天挺安静的。你那边怎么样？",
        ),
        "witty_sarcastic": (
            "我最近十分钟的成就，是认真盯着一面墙。你今天怎么样？",
            "原本打算今天很有效率。想想而已。",
            "先聊起来吧：你今天遇到最不无聊的事是什么？",
            "有人说第一次开口要自然一点。那我来了。你怎么样？",
            "今天的主要计划是发呆，目前执行得很顺利。你呢？",
        ),
        "warm": (
            "刚泡了茶坐下来，正好想听听你今天怎么样。",
            "今天想到你了，希望你还好。有没有什么想说的？",
            "没什么事，就是确认一下你还好吗。",
            "下午路过一条街，想如果你在应该会喜欢。你今天怎么样？",
            "不用有压力，就是来问问你今天过得如何。",
        ),
        "dry": (
            "咖啡，然后更多咖啡。这就是今天的情况。你呢？",
            "今天没什么可报告的。通常这种时候会出事。你今天怎么样？",
            "今天像一句没有句号的长句子。你那边呢？",
            "我只有一块饼干和一堆没做完的事。你在忙什么？",
            "没什么新闻。天气一般，人还在。你呢？",
        ),
        "playful": (
            "紧急消息：我找到一家不错的咖啡。想听就好好问。",
            "我来问一个没有压力的问题：咖啡还是茶？我有自己的答案。",
            "今天做了一件有点冲动的事。先说说你的今天。",
            "从我家的沙发发来现场报道：什么都没发生，很好。你呢？",
            "刚发现一个新地方，正在犹豫要不要告诉你。你今天怎么样？",
        ),
        "poetic": (
            "今天傍晚的光很好，整条街都安静了一会儿。你今天看到什么了？",
            "今天老在注意小东西：杯口的热气、等在门口的狗。什么让你停了一下？",
            "这边下了一下午的雨。我不讨厌。你那边呢？",
            "我绕远路走回家，城市像屏住呼吸。你最近怎么样？",
            "今天的云很低，走得也慢。你那边是什么样子？",
        ),
    },
}

#: 低水平短句池：更短、更简单，不问复杂问题。
_SHORT_TEMPLATES: dict[str, dict[str, tuple[str, ...]]] = {
    "en": {
        "outgoing": (
            "What's your day like?",
            "I just got back from a walk around {city}. You?",
            "Say something. I'm bored.",
        ),
        "reserved": (
            "I'm around. You?",
            "Quiet day here. Your day?",
            "Not much here. You?",
        ),
        "witty_sarcastic": (
            "Productive wall-staring over here. You?",
            "Nothing to report. Suspicious. You?",
            "Coffee count: two. You?",
        ),
        "warm": (
            "Just wanted to check in. You okay?",
            "Made tea. How's your day?",
            "Thought of you. What's up?",
        ),
        "dry": (
            "Coffee. Work. Repeat. You?",
            "Nothing much. You?",
            "Long day here. Yours?",
        ),
        "playful": (
            "I found good coffee. Ask me. You?",
            "Busy or bored today?",
            "Two coffees, zero plan. You?",
        ),
        "poetic": (
            "Rain here. You?",
            "This light is nice. Your day?",
            "The city is quiet. You?",
        ),
    },
    "zh": {
        "outgoing": (
            "刚回来。你今天怎么样？",
            "在忙什么？",
            "突然想找你聊两句。",
        ),
        "reserved": (
            "今天挺安静的。你呢？",
            "我还在。你怎么样？",
            "没什么事。你那边呢？",
        ),
        "witty_sarcastic": (
            "今天的主要成就是发呆。你呢？",
            "没什么可报告的。你呢？",
            "咖啡第二杯了。你怎么样？",
        ),
        "warm": (
            "刚泡了茶，想问问你怎么样。",
            "今天过得还好吗？",
            "想到你，来说说话。",
        ),
        "dry": (
            "咖啡、工作、重复。你呢？",
            "今天一般。你那边呢？",
            "没什么事。你怎么样？",
        ),
        "playful": (
            "我找到一家好咖啡，想知道就问我。",
            "今天忙还是无聊？",
            "两杯咖啡，零计划。你呢？",
        ),
        "poetic": (
            "这边在下雨。你那边呢？",
            "今天的光很好看。你怎么样？",
            "城市很安静。你最近怎么样？",
        ),
    },
}

#: 用固定顺序的替换词表计算哈希（不用 Python hash()，跨进程稳定）。
_SALT = "sonder-first-message-v1"


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


def _digest(*parts: Any) -> int:
    text = "|".join(str(part) for part in parts)
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:12], 16)


def _base_language(language: str) -> str:
    """语言代码 → 模板语言（zh-* → zh，其余只保留 en，其它回退 en）。"""
    code = str(language or "").strip().lower().replace("_", "-")
    if not code:
        return "en"
    head = code.split("-")[0]
    if head == "zh" or head == "cn":
        return "zh"
    return "en"


def _is_low_level(level: str) -> bool:
    text = str(level or "").strip().lower()
    if not text:
        return False
    return any(token in text for token in _LOW_LEVEL_TOKENS)


def _persona_text(persona: dict) -> str:
    """把可能影响气质判定的文本拼起来（小写）。"""
    data = persona if isinstance(persona, dict) else {}
    interests = _loads(data.get("interests"), [])
    if not isinstance(interests, list):
        interests = []
    parts = [
        str(data.get("personality") or ""),
        str(data.get("bio") or ""),
        str(data.get("occupation") or ""),
        str(data.get("archetype_id") or ""),
        " ".join(str(item) for item in interests),
    ]
    return " ".join(parts).lower()


def persona_bucket(persona: dict) -> str:
    """persona → 开场风格桶（确定性，永不抛异常）。

    判定顺序（先看显式配置，再看 DNA，最后看性格文本）：
    temperament → speech_dna → 性格关键词 → persona id 哈希兜底。
    """
    data = persona if isinstance(persona, dict) else {}
    ai = _loads(data.get("ai_config"), {})
    temperament = str((ai or {}).get("temperament") or "").strip().lower()
    energy = (ai or {}).get("energy")
    dna = _loads(data.get("speech_dna"), {})
    dna = dna if isinstance(dna, dict) else {}
    text = _persona_text(data)

    humor = str(dna.get("humor_style") or "").strip().lower()
    initiative = str(dna.get("conversation_initiative") or "").strip().lower()
    expression = str(dna.get("emotional_expression") or "").strip().lower()
    verbosity = str(dna.get("verbosity") or "").strip().lower()

    if temperament in ("sarcastic", "witty") or humor == "sarcastic":
        return "witty_sarcastic"
    if "sarcastic" in text or "ironic" in text or "deadpan" in text or "dry humour" in text:
        return "witty_sarcastic"
    if temperament in ("quiet", "shy", "sleepy", "reserved") or (
        initiative == "low" and verbosity == "one_line"
    ):
        return "reserved"
    if temperament in ("calm",) and humor in ("none", ""):
        return "reserved"
    if temperament in ("warm", "caring", "gentle") or expression in ("expressive", "warm"):
        return "warm"
    if temperament == "poetic" or "poetic" in text:
        return "poetic"
    if temperament in ("playful", "passionate", "upbeat", "energetic", "cheerful"):
        return "outgoing" if temperament in ("upbeat", "energetic", "cheerful") else "playful"
    if temperament in ("dry", "blunt", "practical") or humor == "dry":
        return "dry"
    if humor == "playful" or humor == "absurd":
        return "playful"
    if initiative == "high":
        return "outgoing"
    if expression in ("reserved", "guarded"):
        return "reserved"
    try:
        energy_value = float(energy)
    except (TypeError, ValueError):
        energy_value = 0.5
    if energy_value >= 0.8:
        return "playful"
    # 兜底：按 persona id 哈希分桶，保证同一人物稳定
    pid = str(data.get("id") or data.get("persona_id") or data.get("nickname") or "persona")
    return BUCKETS[_digest(pid, _SALT) % len(BUCKETS)]


def _fill(template: str, persona: dict, language: str) -> str:
    """填模板占位符；只对支持的模板语言做替换并做语言净化。"""
    data = persona if isinstance(persona, dict) else {}
    city = str(data.get("city") or "").strip() or "this city"
    interests = _loads(data.get("interests"), [])
    interest = ""
    if isinstance(interests, list):
        for raw in interests:
            text = str(raw or "").strip()
            if text:
                interest = text
                break
    interest = interest or "small talk"
    if language == "en":
        body = template.replace("{city}", city).replace("{interest}", interest)
    else:
        # 中文模板不允许出现英文占位符（语言污染）
        body = template.replace("{city}", "").replace("{interest}", "")
    return _purify(body, language)


def _purify(text: str, language: str) -> str:
    """防御性语言净化：中文里不留拉丁字母，英文里不留中日韩文字。"""
    body = " ".join(str(text or "").split())
    if not body:
        return ""
    if language == "zh":
        return "".join(ch for ch in body if not ("a" <= ch.lower() <= "z"))
    # en：去掉 CJK / 假名 / 韩文
    out = []
    for ch in body:
        code = ord(ch)
        if 0x3040 <= code <= 0x30FF or 0x3400 <= code <= 0x9FFF or 0xAC00 <= code <= 0xD7AF:
            continue
        out.append(ch)
    return " ".join("".join(out).split())


def _pool(persona: dict, language: str, level: str) -> tuple[str, ...]:
    bucket = persona_bucket(persona)
    source = _SHORT_TEMPLATES if _is_low_level(level) else _TEMPLATES
    return source[language][bucket]


def first_message_variants(
    persona: dict,
    *,
    language: str,
    level: str = "",
    seed: str | None = None,
    k: int = 3,
) -> list[str]:
    """按人物桶返回 k 条不同的开场（确定性顺序，可直接给前端轮播/管理端对比）。

    - ``language``：目标回复语言（en / zh 有模板，其它回退 en）；
    - ``level``：学习者在该语言的水平，命中低水平词时用短句池；
    - ``seed``：额外扰动（同一人物想换一组开场时用），默认只由人物决定。
    """
    data = persona if isinstance(persona, dict) else {}
    try:
        want = max(1, int(k))
    except (TypeError, ValueError):
        want = 3
    lang = _base_language(language)
    pool = _pool(data, lang, level)
    if not pool:
        return []
    pid = str(data.get("id") or data.get("persona_id") or data.get("nickname") or "persona")
    start = _digest(pid, lang, seed or "", "pool") % len(pool)
    picked: list[str] = []
    for index in range(min(want, len(pool))):
        template = pool[(start + index) % len(pool)]
        line = _fill(template, data, lang)
        if line and line not in picked:
            picked.append(line)
    return picked


def first_message(
    persona: dict,
    *,
    language: str,
    level: str = "",
    seed: str | None = None,
) -> str:
    """确定性返回「这个人物第一次开口」的一句话。"""
    variants = first_message_variants(
        persona, language=language, level=level, seed=seed, k=3
    )
    return variants[0] if variants else ""


#: opening_style_line 里「不要这样开场」的示例（英文元指令用）。
_NO_WELCOME = "Never a customer-service welcome (\"Hi! How are you?\") or a mini self-introduction."


def opening_style_line(persona: dict, language: str) -> str:
    """prompt 用的一行：你第一次开口通常是什么样。

    元指令固定英文（与 behavior_directive 一致）。目标语言不是 en/zh 时，
    额外提醒「用你自己的语言开口」，避免把英文示例塞给日语/韩语人物。
    """
    data = persona if isinstance(persona, dict) else {}
    bucket = persona_bucket(data)
    lang = _base_language(language)
    flavor = {
        "outgoing": "you start like someone glad to have a person to talk to",
        "reserved": "you start small and a little careful, never performatively friendly",
        "witty_sarcastic": "you start with a dry, slightly self-deprecating observation",
        "warm": "you start by checking in on them, gently, with no agenda",
        "dry": "you start flat and factual, one short line, no sparkle",
        "playful": "you start with something light and slightly mischievous",
        "poetic": "you start with a small concrete image you noticed today",
    }[bucket]
    parts = [f"You almost never open with a greeting: {flavor}.", _NO_WELCOME]
    if lang not in SUPPORTED_LANGUAGES:
        parts.append("Write that first line in your own language, not English.")
    return " ".join(parts)


__all__ = [
    "BUCKETS",
    "SUPPORTED_LANGUAGES",
    "first_message",
    "first_message_variants",
    "opening_style_line",
    "persona_bucket",
]
