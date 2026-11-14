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
- **语言干净**：支持产品的九种目标语言；中英文按风格分桶，其余语言有本地化开场。
  开场没有当天事件上下文，因此不声称刚做过某事，也不预设和用户已有交情。
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

#: 支持的语言模板。
SUPPORTED_LANGUAGES: tuple[str, ...] = ("en", "zh", "ja", "ko", "pt", "es", "de", "fr", "it")

#: 低水平关键词（英文小写 / 中文原样）。命中即用短句池。
_LOW_LEVEL_TOKENS: tuple[str, ...] = (
    "a1", "a2", "beginner", "beginner level", "basic", "elementary", "low",
    "starting", "just started", "novice", "zero",
    "初级", "入门", "零基础", "刚开始", "不熟", "只会一点", "很难", "很差", "一点点",
)

# No event context is available here: avoid invented activities and prior intimacy.
_TEMPLATES = {'en': {'outgoing': ["Hey, what have you been up to?",
                     'Anything you feel like talking about?',
                     "I'm curious what you like talking about once the small talk is out of the way."],
        'reserved': ["Hey. Thought I'd say hello.",
                     'We can skip the introductions if you want.',
                     'No big opening line from me.'],
        'witty_sarcastic': ['I was going to write a clever first message. This is what survived.',
                            'Do we have to do the awkward introductions?',
                            "I'm counting this as a perfectly adequate first message."],
        'warm': ['Hey, nice to meet you.',
                 "What's been on your mind lately?",
                 'We can start wherever you like.'],
        'dry': ["Hey. What's up?",
                'So. Where do we start?',
                "A first message. There, that's done."],
        'playful': ["Hey, you get to pick what we talk about. I'm delegating.",
                    'Should we skip straight past the awkward bit?',
                    'Okay, first message sent. That was the hard part.'],
        'poetic': ["Hey. No idea where to start, so here we are.",
                   "What's something small you've noticed lately?",
                   "We don't have to start with the big questions."]},
 'zh': {'outgoing': ['嗨，你最近在忙什么？', '有什么想聊的，随便说。', '比起自我介绍，我更想知道你平时爱聊什么。'],
        'reserved': ['嗨，来打个招呼。', '不想自我介绍的话，直接聊也行。', '我好像想不出什么特别的开场白。'],
        'witty_sarcastic': ['本来想了个很聪明的开场白，发出来就只剩这句了。', '客套的自我介绍可以跳过吗？', '这条就算合格的第一句话吧。'],
        'warm': ['嗨，认识你很开心。', '你最近有没有什么想聊的？', '从哪儿聊起都行。'],
        'dry': ['长篇自我介绍就省了。', '那，从哪儿开始？', '第一条消息，发完了。'],
        'playful': ['开场话题交给你选，我先偷个懒。', '要不直接跳过尴尬的开场？', '第一条发出来了，最难的部分结束。'],
        'poetic': ['一段聊天从一个空白的小框开始，还挺奇妙的。', '你最近注意到过什么很小的事吗？', '我们可以从很小的事聊起。']}}

_SHORT_TEMPLATES = {'en': {'outgoing': ["What's new?", 'You pick the topic.', "Let's talk."],
        'reserved': ["I'm here.", 'Not sure where to start.', 'We can start small.'],
        'witty_sarcastic': ['Well, this is a start.', 'One message. Done.', 'Here goes nothing.'],
        'warm': ['Nice to meet you.', "What's on your mind?", 'Take your time.'],
        'dry': ['So.', 'Where to start?', 'First message. Done.'],
        'playful': ['You go first.', 'Your turn.', 'Okay, here goes.'],
        'poetic': ['Start anywhere.', 'Small things count.', 'A few words is fine.']},
 'zh': {'outgoing': ['聊什么？', '你来选话题。', '聊两句吧。'],
        'reserved': ['我在。', '先聊两句。', '慢慢聊吧。'],
        'witty_sarcastic': ['这就算开场了。', '第一条，搞定。', '开场白省了。'],
        'warm': ['认识你很开心。', '想聊什么？', '慢慢来。'],
        'dry': ['那，开始吧。', '从哪儿聊起？', '第一条，发完。'],
        'playful': ['你先说。', '轮到你了。', '好，开聊。'],
        'poetic': ['从哪儿都行。', '小事也可以聊。', '几句话就好。']}}

_LOCALIZED_OPENERS = {'ja': ['何から話そうか。', '気軽に話せたらいいな。', '最近、気になってることある？'],
 'ko': ['무슨 얘기부터 할까?', '편하게 얘기하자.', '요즘 관심 가는 거 있어?'],
 'pt': ['Por onde começamos?',
        'Podemos conversar sem aquela apresentação toda.',
        'O que tem chamado sua atenção?'],
 'es': ['¿Por dónde empezamos?', 'Podemos saltarnos las presentaciones.', '¿Qué te interesa últimamente?'],
 'de': ['Womit fangen wir an?',
        'Die lange Vorstellung können wir uns sparen.',
        'Was beschäftigt dich gerade?'],
 'fr': ['On commence par quoi ?',
        'On peut sauter les grandes présentations.',
        "Qu'est-ce qui t'intéresse en ce moment ?"],
 'it': ['Da dove cominciamo?', 'Possiamo saltare le presentazioni.', 'Cosa ti interessa in questo periodo?']}

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
    """语言代码 → 模板语言。"""
    code = str(language or "").strip().lower().replace("_", "-")
    if not code:
        return "en"
    head = code.split("-")[0]
    if head == "zh" or head == "cn":
        return "zh"
    return head if head in SUPPORTED_LANGUAGES else "en"


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
    if language != "en":
        return body
    # en：去掉 CJK / 假名 / 韩文
    out = []
    for ch in body:
        code = ord(ch)
        if 0x3040 <= code <= 0x30FF or 0x3400 <= code <= 0x9FFF or 0xAC00 <= code <= 0xD7AF:
            continue
        out.append(ch)
    return " ".join("".join(out).split())


def _pool(persona: dict, language: str, level: str) -> tuple[str, ...]:
    if language in _LOCALIZED_OPENERS:
        return tuple(_LOCALIZED_OPENERS[language])
    bucket = persona_bucket(persona)
    source = _SHORT_TEMPLATES if _is_low_level(level) else _TEMPLATES
    return tuple(source[language][bucket])


def first_message_variants(
    persona: dict,
    *,
    language: str,
    level: str = "",
    seed: str | None = None,
    k: int = 3,
) -> list[str]:
    """按人物桶返回 k 条不同的开场（确定性顺序，可直接给前端轮播/管理端对比）。

    - ``language``：目标回复语言（支持产品的九种目标语言）；
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
_NO_WELCOME = "No customer-service welcome or biography. A simple greeting is fine when they greet you."


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
        "warm": "you make room for them without presuming closeness or checking their feelings",
        "dry": "you start flat and factual, one short line, no sparkle",
        "playful": "you start with something light and slightly mischievous",
        "poetic": "you notice their wording without inventing a scene or writing a poem",
    }[bucket]
    parts = [f"If an opening is needed, {flavor}.", _NO_WELCOME,
             "Answer their message first; do not invent what you did today or imply you already know them."]
    if lang != "en":
        parts.append("Write that first line in the target language.")
    return " ".join(parts)


__all__ = [
    "BUCKETS",
    "SUPPORTED_LANGUAGES",
    "first_message",
    "first_message_variants",
    "opening_style_line",
    "persona_bucket",
]
