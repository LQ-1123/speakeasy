"""好友回复主链路（异步语音条）。

``用户语音 → STT → 记忆上下文 → LLM → edge-tts 合成 wav → 落库``
以及旁路：气泡润色批注、双层记忆提炼、熟稔度升级、好友裂变推荐。

设计要点
--------
- 任何 LLM/TTS/STT 失败都**降级**（规则回复 / tone 兜底 / 空转写），接口不 500。
- 数据库是同步的，统一用 ``asyncio.to_thread`` 调用。
- 后台任务（润色 / 记忆 / 推荐）不阻塞用户等待的语音回复。
"""

from __future__ import annotations

import uuid

import asyncio
import json
import os
import re
import time
import wave
import zlib
from typing import Any
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from contextlib import asynccontextmanager
from functools import partial

from app.capacity import ChatAdmission, CapacityError, UsageBudget
from app.llm.client import ChatMessage
from app.logging_utils import get_logger
from app.v2 import social
from app.v2.suggestion import generate_suggestion

log = get_logger(__name__)
_turn_audio_tasks: ContextVar[list | None] = ContextVar('turn_audio_tasks', default=None)


def _write_bytes(path: str, data: bytes) -> None:
    import os as _os

    _os.makedirs(_os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def _fish_duration_ms(data: bytes) -> int:
    if data.startswith(b'RIFF'):
        import io
        with wave.open(io.BytesIO(data), 'rb') as audio:
            return int(audio.getnframes() / audio.getframerate() * 1000)
    from app.voice.fish_tts import estimate_mp3_duration_ms
    return estimate_mp3_duration_ms(data)

_MARKDOWN_RE = re.compile(r"[*_#`>]+")

#: mock（无 LLM key）时按话题生成有人设味的回复，保证演示对话不重复。
_TOPIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("coffee", re.compile(r"coffee|flat white|latte|espresso|barista|cappuccino", re.I)),
    ("games", re.compile(r"game|gaming|indie|steam|console|play", re.I)),
    ("interview", re.compile(r"interview|job|career|recruit|hiring", re.I)),
    ("english", re.compile(r"english|speak|pronounce|grammar|accent|language", re.I)),
    ("food", re.compile(r"food|eat|lunch|dinner|dish|restaurant|cook", re.I)),
    ("travel", re.compile(r"travel|trip|abroad|flight|vacation|holiday", re.I)),
    ("work", re.compile(r"work|project|deadline|meeting|study|school|class|exam", re.I)),
    ("music", re.compile(r"music|band|song|vinyl|record|concert", re.I)),
)

_MOCK_TOPIC_REPLIES: dict[str, tuple[str, ...]] = {
    "coffee": (
        "{q}coffee is serious business around {city}. What's your go-to order?",
        "{q}I'm picky about coffee, honestly. Do you take yours black or with milk?",
        "{q}a good coffee doesn't need to be complicated.",
        "{q}a bad flat white is a disappointing way to spend money.",
    ),
    "games": (
        "{q}indie games are the best — no big studio nonsense. Which one are you playing right now?",
        "{q}I lose whole evenings to games like that. What's the last one that got you hooked?",
        "{q}that's the danger of 'just one more round'.",
        "{q}sometimes a game is a good excuse to switch off for a bit.",
    ),
    "interview": (
        "{q}what's the role about?",
        "{q}is there a particular part you're unsure about?",
        "{q}interviews are just a conversation with worse lighting.",
        "{q}prep helps, but half of it is whether you like them. Weird but true.",
    ),
    "english": (
        "{q}what part are you trying to say?",
        "{q}want to try putting it another way?",
        "{q}honestly, nobody is grading you. That's the whole point.",
        "{q}we don't have to turn this into a lesson.",
    ),
    "food": (
        "{q}now I'm hungry. What's the best thing you've eaten this week?",
        "{q}food is the most important part of the day, obviously. Do you cook much?",
        "{q}good food doesn't always need a complicated recipe.",
        "{q}I could eat the same thing four days straight and be happy.",
    ),
    "travel": (
        "{q}travel changes your whole brain, I swear. Where do you want to go next?",
        "{q}I'd pack a bag tomorrow if I could. What's your dream trip?",
        "{q}I'd rather leave some room to wander than plan every minute.",
        "{q}getting there can be half the hassle.",
    ),
    "work": (
        "{q}that sounds like a lot. How are you balancing it with everything else?",
        "{q}sounds busy — is it the kind of busy you enjoy, or the exhausting kind?",
        "{q}some tasks somehow grow another task while you're doing them.",
        "{q}a quiet stretch without interruptions sounds good.",
    ),
    "music": (
        "{q}music is basically medicine. What have you had on repeat lately?",
        "{q}my taste is all over the place. What are you listening to these days?",
        "{q}sometimes one good track is enough.",
        "{q}I like when a song takes a turn you didn't expect.",
    ),
}

_MOCK_DEFAULT_REPLIES: tuple[str, ...] = (
    "{q}what do you mean by that?",
    "{q}which part?",
    "{q}yeah. That tracks.",
    "{q}hmm. I get that.",
    "{q}okay.",
    "{q}mm.",
)

#: 多语言回复：display name（用于 prompt）与确定性 mock 回复池（无 LLM key 时也能说对语言）
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "ja": "Japanese (日本語)",
    "ko": "Korean (한국어)",
    "pt": "Portuguese (Português)",
    "es": "Spanish (Español)",
    "de": "German (Deutsch)",
    "fr": "French (Français)",
    "it": "Italian (Italiano)",
    "zh": "Chinese (中文)",
}

_MOCK_LANG_REPLIES: dict[str, tuple[str, ...]] = {
    "ja": (
        "{q}なるほどね。最近は何をしてることが多い？",
        "{q}それはいいね。週末は何をする予定？",
        "{q}へえ、面白いね。もう少し詳しく聞かせて？",
        "{q}うん、なるほど。",
        "{q}それ、いいな。",
    ),
    "ko": (
        "{q}그렇구나. 요즘 뭐 하고 지내?",
        "{q}오, 재밌다. 주말에 뭐 해?",
        "{q}그래? 좀 더 얘기해 줄래?",
        "{q}응, 알겠어.",
        "{q}그거 좋다.",
    ),
    "pt": (
        "{q}entendi. E o que você tem feito ultimamente?",
        "{q}que legal. O que você costuma fazer no fim de semana?",
        "{q}sério? Me conta mais sobre isso.",
        "{q}pois é, entendi.",
        "{q}isso é bom.",
    ),
    "es": (
        "{q}ya veo. ¿Y qué has estado haciendo últimamente?",
        "{q}qué bien. ¿Qué te gusta hacer los fines de semana?",
        "{q}¿en serio? Cuéntame un poco más.",
        "{q}ya, entiendo.",
        "{q}eso está bien.",
    ),
    "de": (
        "{q}verstehe. Und was machst du so in letzter Zeit?",
        "{q}schön. Was machst du am Wochenende?",
        "{q}echt? Erzähl mir mehr davon.",
        "{q}ja, verstehe.",
        "{q}das ist gut.",
    ),
    "fr": (
        "{q}je vois. Et qu'est-ce que tu fais en ce moment ?",
        "{q}super. Qu'est-ce que tu fais le week-end ?",
        "{q}sérieux ? Raconte-moi un peu plus.",
        "{q}ouais, je vois.",
        "{q}c'est bien ça.",
    ),
    "it": (
        "{q}capisco. E cosa fai in questi giorni?",
        "{q}bello. Cosa fai nel fine settimana?",
        "{q}davvero? Raccontami qualcosa in più.",
        "{q}sì, capisco.",
        "{q}va bene così.",
    ),
    "zh": (
        "{q}原来是这样。你最近在忙什么？",
        "{q}挺好的。周末一般做什么？",
        "{q}真的吗？多说一点吧。",
        "{q}嗯，知道了。",
        "{q}挺好的。",
    ),
}

# Minimal offline reactions. These are not a replacement for a conversational model,
# but they must respect the user's intent before any topic/voice variation.
_MOCK_CONTEXT_REPLIES = {
    "en": ("Yeah?", "Talk later.", "Oh no. I'm sorry.", "That's good news!", "Mm.", "I'm not sure how to answer that.", "I'm an AI character here in Sonder."),
    "zh": ("在呢。", "回头聊。", "啊，听到这个很难过。", "那太好了！", "嗯。", "这个我还真不知道怎么答。", "我是 Sonder 里的 AI 人物。"),
    "ja": ("うん。", "またね。", "それはつらいね。", "よかったね！", "うん。", "それはちょっとわからない。", "私は Sonder の AI キャラクターだよ。"),
    "ko": ("응.", "다음에 얘기하자.", "마음이 아프네.", "잘됐다!", "응.", "그건 잘 모르겠어.", "나는 Sonder의 AI 캐릭터야."),
    "pt": ("Oi.", "Até mais.", "Poxa, sinto muito.", "Que boa notícia!", "Hum.", "Não sei bem como responder a isso.", "Sou uma personagem de IA no Sonder."),
    "es": ("Hola.", "Hablamos luego.", "Vaya, lo siento mucho.", "¡Qué buena noticia!", "Mmm.", "No sé bien cómo responder a eso.", "Soy un personaje de IA en Sonder."),
    "de": ("Hey.", "Bis später.", "Oh nein. Das tut mir leid.", "Das ist schön!", "Hm.", "Darauf weiß ich gerade keine Antwort.", "Ich bin eine KI-Figur in Sonder."),
    "fr": ("Salut.", "À plus.", "Oh non, je suis désolé.", "Bonne nouvelle !", "Hmm.", "Je ne sais pas trop quoi répondre à ça.", "Je suis un personnage d'IA dans Sonder."),
    "it": ("Ciao.", "A dopo.", "Mi dispiace molto.", "Che bella notizia!", "Mmh.", "Non so bene come rispondere.", "Sono un personaggio di IA in Sonder."),
}
_MOCK_INTENT_INDEX = {"greeting": 0, "closing": 1, "bad_news": 2, "good_news": 3, "silence": 4, "question": 5}
_IDENTITY_QUESTION_RE = re.compile(
    r"\b(?:are|r) you (?:an? )?(?:ai|bot|human|real person)\b|"
    r"\b(?:ai|bot) or (?:a )?human\b|你(?:是|到底是|是不是).{0,8}(?:AI|ai|机器人|真人|人工智能)"
)


def _language_of(friend: dict) -> str:
    lang = str(friend.get("reply_language") or "en").strip().lower()
    return lang if lang in LANGUAGE_NAMES else "en"


#: 语言脚本特征：用于检查「模型是不是又回英文了」
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_HANGUL_RE = re.compile(r"[\uac00-\ud7af]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def _visible_chars(text: str) -> int:
    return sum(1 for ch in (text or "") if not ch.isspace())


def _matches_language(text: str, lang: str) -> bool:
    """粗判一段回复是否为目标语言（足够抓「中文好友突然整段英文」）。

    只做脚本级判断，不判断英/法/德等拉丁语系之间的差异（那需要模型）。
    """
    body = text or ""
    total = _visible_chars(body)
    if total == 0:
        return True
    cjk = len(_CJK_RE.findall(body))
    kana = len(_KANA_RE.findall(body))
    hangul = len(_HANGUL_RE.findall(body))
    latin = len(_LATIN_RE.findall(body))
    cjk_ratio = (cjk + kana) / total
    latin_ratio = latin / total

    if lang == "zh":
        return cjk >= 2 and cjk_ratio >= 0.25
    if lang == "ja":
        return kana >= 1 or cjk_ratio >= 0.35
    if lang == "ko":
        return hangul >= 1 and hangul / total >= 0.2
    if lang in ("de", "fr", "es", "it", "pt"):
        # 拉丁语系之间无法脚本区分；只拒绝「几乎全中文/日文/韩文」的回复
        return cjk_ratio < 0.4 and latin >= 2
    if lang == "en":
        return cjk_ratio < 0.25
    return True


def _sanitize_persona_language(persona: str) -> str:
    """修复旧版本生成的角色 prompt 里的语言矛盾句（「Never switch ... (or Chinese)」）。"""
    text = persona or ""
    text = text.replace(
        "Never switch to English (or Chinese)",
        "Never switch to English or any other language",
    )
    text = text.replace(
        "even if the learner writes in English or Chinese",
        "even if the learner writes in another language",
    )
    return text


def _native_quirk_prefix(quirk: str, lang: str) -> str:
    q = (quirk or "").strip()
    if not q:
        return ""
    if lang in ("ja", "zh"):
        return f"{q}，"
    return f"{q}, "


_EMOJI_RE = re.compile(
    "["
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F680-\U0001F6FF"  # transport
    "\U0001F900-\U0001F9FF"  # supplemental symbols
    "\U0001FA00-\U0001FAFF"  # newer symbols
    "\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D"
    "]+",
    flags=re.UNICODE,
)


def _strip_emoji(text: str) -> str:
    """TTS 前去掉 emoji（气泡里仍显示原文）；避免语音引擎把 emoji 念出来。"""
    cleaned = _EMOJI_RE.sub("", text or "")
    return " ".join(cleaned.split()).strip()


_SENT_END_RE = re.compile(r"[。！？!?…]+|\.(?=\s|$)")


def _split_ready_sentences(
    buffer: str, *, min_chars: int = 8, max_chars: int = 90
) -> tuple[list[str], str]:
    """从流式 token 缓冲里切出「已完整」的句子（用于边生成边 TTS）。"""
    ready: list[str] = []
    rest = buffer
    while True:
        match = _SENT_END_RE.search(rest)
        if match is None:
            break
        end = match.end()
        sentence = rest[:end].strip()
        if sentence:
            ready.append(sentence)
        rest = rest[end:].lstrip()
    # 缓冲区过长仍没有标点：主动按逗号/空格切一段，避免一直等不到 TTS
    while len(rest) > max_chars:
        cut = rest.rfind(",", 0, max_chars)
        if cut < min_chars:
            cut = rest.rfind(" ", 0, max_chars)
        if cut < min_chars:
            cut = max_chars
        piece = rest[:cut].strip()
        if piece:
            ready.append(piece)
        rest = rest[cut:].lstrip()
    return ready, rest


def _combine_wavs(paths: list[str], out_path: str) -> tuple[str, int]:
    """把多句 wav 拼成一条（给聊天记录回放/持久化用）。"""
    if not paths:
        raise ValueError("没有音频可拼接")
    if len(paths) == 1:
        with wave.open(paths[0], "rb") as src:
            frames = src.readframes(src.getnframes())
            params = src.getparams()
        with wave.open(out_path, "wb") as dst:
            dst.setparams(params)
            dst.writeframes(frames)
        return out_path, int(len(frames) / max(1, params.framerate * params.sampwidth) * 1000)
    params = None
    frames = bytearray()
    for path in paths:
        with wave.open(path, "rb") as src:
            if params is None:
                params = src.getparams()
            frames += src.readframes(src.getnframes())
    assert params is not None
    with wave.open(out_path, "wb") as dst:
        dst.setparams(params)
        dst.writeframes(bytes(frames))
    duration_ms = int(len(frames) / max(1, params.framerate * params.sampwidth) * 1000)
    return out_path, duration_ms


#: LLM 允许使用的 Fish 行内情绪标记（只作提示，不读出、不显示）
ALLOWED_EMOTION_TAGS = {
    # Fish 官方常见情绪
    "happy", "sad", "angry", "scared", "disgusted", "surprised",
    "calm", "excited", "soft", "laughing", "whispering",
    "sigh", "gasp", "crying", "singing",
    # 停顿/换气控制
    "break", "long-break", "breath", "pause",
    # 兼容旧提示词里用过的词
    "curious", "sympathetic",
}

#: 允许字母、下划线、连字符和空格（long-break / no way 之类）
_TAG_RE = re.compile(r"\[\s*([a-zA-Z_\- ]+?)\s*\]")


def strip_emotion_tags(text: str) -> str:
    """只保留「给用户看/给 Edge 读」的文本：去掉白名单情绪标记并收拾空格。

    Fish 需要这些标记来表演；Edge/Tone 会把它们照字面念出来，所以放给
    Edge 之前必须调用这个函数。非白名单方括号（用户写的 `[哈哈]`）原样保留。
    """
    raw = text or ""

    def _sub(m: re.Match[str]) -> str:
        name = m.group(1).strip().lower()
        return "" if name in ALLOWED_EMOTION_TAGS else m.group(0)

    out = _TAG_RE.sub(_sub, raw)
    out = re.sub(r"\s{2,}", " ", out).strip()
    # 标记常紧贴标点：`hi [excited]!` → 去掉标记后别留 "hi !"
    out = re.sub(r"\s+([,.!?;:，。！？；：])", r"\1", out)
    return out


def split_emotion(text: str) -> tuple[str, str]:
    """把 LLM 回复拆成 (显示文本, TTS 文本)。

    * 只有白名单里的 [tag] 会被保留给 TTS；其它方括号内容（如用户写的 [哈哈]）
      原样留在显示文本里，避免误吞
    * 显示文本里剔除标记 → 用户看不到 [excited]
    * TTS 文本保留标记 → Fish 才知道该怎么演
    """
    raw = text or ""
    keep: list[str] = []

    def _sub(m: re.Match[str]) -> str:
        name = m.group(1).strip().lower()
        if name in ALLOWED_EMOTION_TAGS:
            keep.append(name)
            return ""
        return m.group(0)

    display = _TAG_RE.sub(_sub, raw)
    display = re.sub(r"\s{2,}", " ", display).strip()
    display = re.sub(r"\s+([,.!?;:，。！？；：])", r"\1", display)
    if not keep:
        return display, display
    # 标记统一放到开头（多个用空格连接）：Fish 对句首标记最敏感
    prefix = " ".join(f"[{t}]" for t in keep[:3])
    return display, f"{prefix} {display}".strip()


def _clean_reply(text: str) -> str:
    """Clean display formatting; the model token budget already bounds reply length.

    A second character cap used to silently remove answers after 420 characters,
    sometimes leaving a negation or clarification on the discarded side.
    """
    text = _MARKDOWN_RE.sub("", text or "")
    text = re.sub(r"\s+", " ", text).strip().strip('"')
    return text


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9' ]+", " ", (text or "").lower()).strip()


_EMOTION_PROSODY: dict[str, tuple[float, float]] = {
    "excited": (10.0, 8.0),    # 兴奋/惊喜：快 + 高
    "amused": (8.0, 4.0),      # 好笑/轻松
    "curious": (3.0, 6.0),     # 疑惑/追问：音高上扬
    "warm": (-3.0, -2.0),      # 温柔/安慰：慢一点、柔一点
    "down": (-8.0, -6.0),      # 低落/同情
    "neutral": (0.0, 0.0),
}

_EXCITED_RE = re.compile(
    r"(!|wow|no way|oh my|that'?s (?:mad|crazy|insane|amazing|brilliant)|"
    r"seriously\?|really\?|can'?t believe)",
    re.I,
)
_WARM_RE = re.compile(
    r"(sorry|that'?s (?:rough|tough|gut+ing|awful)|poor you|oh no|"
    r"happens to (?:all of us|the best)|don'?t worry|take it easy)",
    re.I,
)
_AMUSED_RE = re.compile(r"\b(ha|ha ha|haha|hehe|lol|funny|hilarious|cracks? me up)\b", re.I)
_DOWN_RE = re.compile(r"(tired|exhausted|knackered|shattered|sad|miss |stressed)", re.I)


def detect_emotion(text: str) -> str:
    """从一句回复里粗略判情绪。宁可判错成 neutral，也不要乱飙。"""
    t = text or ""
    exclam = t.count("!") + t.count("?")
    if _EXCITED_RE.search(t) or exclam >= 2:
        return "excited"
    if _WARM_RE.search(t):
        return "warm"
    if _AMUSED_RE.search(t):
        return "amused"
    if _DOWN_RE.search(t):
        return "down"
    if "?" in t:
        return "curious"
    return "neutral"


_PROSODY_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)\s*(%|Hz)$", re.I)


def emotion_prosody(text: str, base_rate: str, base_pitch: str) -> tuple[str, str, str]:
    """返回 (rate, pitch, emotion)。把情绪 delta 叠到好友基础 prosody 上。"""
    emotion = detect_emotion(text)
    dr, dp = _EMOTION_PROSODY.get(emotion, (0.0, 0.0))

    def add(value: str, delta: float, lo: float, hi: float, unit: str) -> str:
        m = _PROSODY_RE.match(str(value or "").strip())
        base = float(m.group(1)) if m else 0.0
        return f"{max(lo, min(hi, base + delta)):+.0f}{unit}"

    return (
        add(base_rate, dr, -25.0, 40.0, "%"),
        add(base_pitch, dp, -25.0, 20.0, "Hz"),
        emotion,
    )


#: 情绪 -> Fish 行内标记。只有 Fish 会真的「演」出来。
_EMOTION_TAG: dict[str, str] = {
    "excited": "[excited]",
    "amused": "[laughing]",
    "warm": "[sympathetic]",
    "down": "[sad]",
    "curious": "[curious]",
    "neutral": "",
}


def _tag_emotion(text: str, emotion: str) -> str:
    """给文本加上 Fish 的行内情绪标记；neutral 不加。"""
    tag = _EMOTION_TAG.get(emotion, "")
    body = (text or "").strip()
    if not tag or not body:
        return body
    return f"{tag} {body}"


def _tts_defer_audio_enabled() -> bool:
    """是否把好友语音合成放到后台，先落文字消息再补音频。

    **默认关闭**：产品要求「AI 发来的就是语音，点一下立刻能播」。
    如果先落文字、再补音频，气泡会先显示文字、几秒后变成语音，而且音频
    没准备好时点击只能现场再合成一次（更慢）。现在先合成再返回，等待期间
    由客户端的「正在输入」提示承接；消息出现时音频已经可播。

    仍保留开关，供低延迟/弱网实验显式开启；测试环境同样保持同步语义。
    """
    return os.getenv("SE_TTS_DEFER_AUDIO", "0").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


class ChatService:
    def __init__(
        self,
        engines: Any,
        db: Any,
        memory: Any,
        voice: Any,
        spawner: Any,
        audio_store: Any,
        media_store: Any = None,
        *,
        memory_idle_s: float = 120.0,
    ) -> None:
        self.engines = engines
        self.db = db
        self.memory = memory
        self.voice = voice
        self.spawner = spawner
        self.audio = audio_store
        self.media = media_store
        self.memory_idle_s = max(0.0, float(memory_idle_s))
        self._tasks: set[asyncio.Task] = set()
        self._memory_tasks: dict[str, asyncio.Task] = {}
        #: friend_id -> 开始生成回复的 monotonic 时间（前端「对方正在说话」）
        self._replying: dict[str, float] = {}
        self.admission = ChatAdmission()
        self.usage_budget = UsageBudget(db.conn, db._lock) if db is not None else None
        if getattr(self.engines, 'llm', None) is not None:
            self.engines.llm.usage_budget = self.usage_budget
        self._decode_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="voice-decode")
        self._voice_tasks: dict[str, asyncio.Task] = {}
        self._memory_last: dict[str, str] = {}
        self._memory_slots = asyncio.Semaphore(1)

    # ------------------------------------------------------------------ #
    # 后台任务
    # ------------------------------------------------------------------ #
    @asynccontextmanager
    async def foreground_turn(self, user_id: str):
        async with self.admission.turn(user_id):
            owned = []
            token = _turn_audio_tasks.set(owned)
            try:
                yield
            finally:
                pending = [(mid, friend, task) for mid, friend, task in owned if not task.done()]
                for _, _, task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*(task for _, _, task in pending), return_exceptions=True)
                    from app.jobs import run_db_thread
                    from app.v2.audio_events import publish
                    # A task cancelled before its first instruction has no finally
                    # block. Its parent still owns the persisted queued state.
                    for mid, friend, _ in pending:
                        message = await run_db_thread(self.db.get_message, mid)
                        if message and message.get('audio_status') in ('queued', 'generating'):
                            await run_db_thread(self.db.update_message, mid, audio_status='failed',
                                                audio_error='语音生成已取消，请重试')
                            await run_db_thread(publish, self.db, friend, mid, 'failed')
                _turn_audio_tasks.reset(token)

    def spawn_bg(self, coro) -> asyncio.Task:
        if len(self._tasks) >= 256:
            coro.close()
            async def skipped():
                return None
            return asyncio.create_task(skipped())
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def aclose(self) -> None:
        tasks = list(self._tasks) + list(self._memory_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._memory_tasks.clear()
        if hasattr(self, '_decode_pool'):
            await asyncio.to_thread(self._decode_pool.shutdown, wait=True, cancel_futures=True)

    # ------------------------------------------------------------------ #
    # 「对方正在说话」状态（跨前端组件/离开聊天窗后仍然有效）
    # ------------------------------------------------------------------ #
    def mark_replying(self, friend_id: str) -> None:
        self._replying[str(friend_id)] = time.monotonic()

    def clear_replying(self, friend_id: str) -> None:
        self._replying.pop(str(friend_id), None)

    def is_replying(self, friend_id: str) -> bool:
        started = self._replying.get(str(friend_id))
        if started is None:
            return False
        if time.monotonic() - started > 180:  # 异常残留兜底
            self._replying.pop(str(friend_id), None)
            return False
        return True

    def replying_started_at(self, friend_id: str) -> str | None:
        if not self.is_replying(friend_id):
            return None
        return _now_iso()

    # ------------------------------------------------------------------ #
    # 对外主入口
    # ------------------------------------------------------------------ #
    async def handle_voice(
        self,
        friend_id: str,
        audio_bytes: bytes,
        filename: str,
        duration_ms: int = 0,
        context: str | None = None,
    ) -> dict:
        from app.v3.identity import current_user_id
        async with self.foreground_turn(current_user_id()):
            return await self._handle_voice_admitted(friend_id, audio_bytes, filename, duration_ms, context)

    async def _handle_voice_admitted(self, friend_id, audio_bytes, filename, duration_ms, context):
        self.mark_replying(friend_id)
        try:
            friend = await asyncio.to_thread(self.db.get_friend, friend_id)
            if friend is None:
                raise KeyError(friend_id)

            ctx = copy_context()
            decoded = await asyncio.get_running_loop().run_in_executor(
                self._decode_pool, ctx.run, partial(self.audio.decode_upload, audio_bytes, filename))
            reply_lang = _language_of(friend)
            stt_language = None if reply_lang == "en" else reply_lang
            transcript, stt_meta = await self._transcribe(decoded["pcm16"], language=stt_language)
            if not transcript:
                raise ValueError("语音没有识别清楚，请重新录音")
            dur = int(decoded["duration_ms"] or duration_ms or 0)
            result = await self._handle_user_text(
                friend,
                transcript,
                msg_type="voice",
                audio_filename=decoded["audio_filename"],
                duration_ms=dur,
                context=context,
                stt_meta=stt_meta,
            )
            return await self._settle_audio(result)
        finally:
            self.clear_replying(friend_id)

    async def handle_text(
        self,
        friend_id: str,
        text: str,
        context: str | None = None,
        client_message_id: str | None = None,
    ) -> dict:
        from app.v3.identity import current_user_id
        async with self.foreground_turn(current_user_id()):
            return await self._handle_text_admitted(friend_id, text, context, client_message_id)

    async def _handle_text_admitted(self, friend_id, text, context, client_message_id):
        self.mark_replying(friend_id)
        try:
            friend = await asyncio.to_thread(self.db.get_friend, friend_id)
            if friend is None:
                raise KeyError(friend_id)
            if client_message_id:
                existing = await asyncio.to_thread(
                    self.db.get_message_by_client_id, friend_id, client_message_id
                )
                if existing is not None:
                    # 同一客户端消息重复提交（例如未回复时退出页面又重试）：
                    # 不再产生新消息/新回复，直接返回已有回合。
                    duplicate = await asyncio.to_thread(self._duplicate_turn, friend, existing)
                    if duplicate['friend_messages']:
                        return duplicate
            result = await self._handle_user_text(
                friend, text, msg_type="text", context=context,
                stt_meta={"backend": "text", "latency_ms": 0.0},
                client_message_id=client_message_id,
            )
            return await self._settle_audio(result)
        finally:
            self.clear_replying(friend_id)

    def _duplicate_turn(self, friend: dict, user_message: dict) -> dict:
        """幂等命中时构造与正常回合同形的响应（包含已生成的回复）。"""
        fid = str(friend["id"])
        with self.db._lock:
            linked = self.db.conn.execute(
                "SELECT * FROM messages WHERE friend_id=? AND reply_to_message_id<>'' "
                "AND reply_to_message_id=? ORDER BY rowid", (fid, user_message['id'])).fetchall()
            if not linked:
                linked = self.db.conn.execute(
                    "SELECT * FROM messages WHERE friend_id=? AND sender='friend' "
                    "AND COALESCE(reply_to_message_id,'')='' "
                    "AND rowid>(SELECT rowid FROM messages WHERE id=?) "
                    "AND rowid<COALESCE((SELECT MIN(rowid) FROM messages WHERE friend_id=? "
                    "AND sender='user' AND rowid>(SELECT rowid FROM messages WHERE id=?)),9223372036854775807) "
                    "ORDER BY rowid", (fid, user_message['id'], fid, user_message['id'])).fetchall()
            replies = [self.db._row_to_dict(row, 'messages') for row in linked]
        return {
            "user_message": user_message,
            "friend_messages": replies,
            "suggestion_pending": False,
            "closeness_level": friend.get("closeness_level", 1),
            "total_turns": friend.get("total_turns", 0),
            "memory_updated": False,
            "reply_language": self._reply_language(friend),
            "stt": {"backend": "text", "latency_ms": 0.0},
            "duplicate": True,
        }

    # ------------------------------------------------------------------ #
    # 语音通话（流式：边生成边按句 TTS）
    # ------------------------------------------------------------------ #
    async def _synth_sentence(self, text: str, friend: dict, *, out_dir: str | None = None) -> dict | None:
        """为一句话合成 TTS，返回 {path,url,duration_ms}；失败返回 None。"""
        clean = _strip_emoji(text)
        if not clean or self.audio is None:
            return None
        target_dir = out_dir or self.audio.audio_dir
        # Fish 优先：和聊天气泡同一条链路，延迟 ~1s；edge 兜底（国内约 6s）
        try:
            from app.voice import fish_tts
            with fish_tts.priority('call'):
                fish_audio = await self._fish_voice(clean, "neutral", friend)
            if fish_audio is not None:
                cfg = self.tts_config.load()
                ext = (cfg.fish_format or "mp3").lower()
                fname = f"fish_{uuid.uuid4().hex[:12]}.{ext}"
                path = os.path.join(target_dir, fname)
                await asyncio.to_thread(_write_bytes, path, fish_audio)
                duration_ms = max(500, _fish_duration_ms(fish_audio))
                return {
                    "path": path,
                    "url": f"/api/v2/audio/{fname}",
                    "duration_ms": duration_ms,
                }
        except Exception as exc:  # noqa: BLE001 - Fish 失败回落 edge
            log.warning("通话单句 Fish TTS 失败（%s: %s），回落 edge", type(exc).__name__, exc)
        if self.voice is None:
            return None
        binding = self._voice_binding(friend)
        if binding is not None and getattr(binding, "strict", False) and binding.fish_enabled:
            return None  # Pinned Fish voice failed; never replace this person with Edge.
        edge_voice = (
            binding.edge_voice
            if binding is not None
            else (friend.get("voice_id") or "en-GB-RyanNeural")
        )
        try:
            path, duration_ms = await self.voice.synth_to_wav(
                strip_emotion_tags(clean) or clean,
                edge_voice,
                rate=friend.get("voice_rate") or "+0%",
                pitch=friend.get("voice_pitch") or "+0Hz",
                out_dir=target_dir,
            )
        except Exception as exc:  # noqa: BLE001 - 单句 TTS 失败不阻断通话
            log.warning("通话单句 TTS 失败（%s: %s）", type(exc).__name__, exc)
            return None
        return {
            "path": path,
            "url": f"/api/v2/audio/{os.path.basename(path)}",
            "duration_ms": int(duration_ms or 0),
        }

    async def handle_call_turn_stream(
        self,
        friend_id: str,
        audio_bytes: bytes,
        filename: str,
        duration_ms: int = 0,
        *,
        transcript: str | None = None,
        stt_meta: dict[str, Any] | None = None,
    ):
        self.mark_replying(friend_id)
        try:
            async for event in self._handle_call_turn_stream(
                friend_id,
                audio_bytes,
                filename,
                duration_ms,
                transcript=transcript,
                stt_meta=stt_meta,
            ):
                yield event
        finally:
            self.clear_replying(friend_id)

    async def _handle_call_turn_stream(
        self,
        friend_id: str,
        audio_bytes: bytes,
        filename: str,
        duration_ms: int = 0,
        *,
        transcript: str | None = None,
        stt_meta: dict[str, Any] | None = None,
    ):
        """通话回合流式生成器：STT → LLM 流式 → 按句 TTS → done。

        事件（NDJSON）：
        - {"type":"transcript","text":...,"stt":{...}}
        - {"type":"sentence","index":0,"text":"...","audio_url":"/api/v2/audio/x.wav"}
        - {"type":"done","user_message":...,"friend_message":...,
           "closeness_level":...,"total_turns":...,"reply_language":...}
        - {"type":"error","message":"..."}
        """
        friend = await asyncio.to_thread(self.db.get_friend, friend_id)
        if friend is None:
            yield {"type": "error", "message": "好友不存在"}
            return
        if self.audio is None:
            yield {"type": "error", "message": "音频服务未初始化"}
            return

        lang = _language_of(friend)
        if transcript is None:
            try:
                decoded = await asyncio.to_thread(
                    self.audio.decode_upload, audio_bytes, filename
                )
            except ValueError as exc:
                yield {"type": "error", "message": str(exc)}
                return
            stt_language = None if lang == "en" else lang
            transcript, stt_meta = await self._transcribe(
                decoded["pcm16"], language=stt_language, prefer_local=True, fast=True
            )
        stt_meta = stt_meta or {"backend": "deepgram-stream", "latency_ms": 0.0}
        if not transcript:
            transcript = "（语音没有识别清楚，可以再说一次吗？）"
        yield {"type": "transcript", "text": transcript, "stt": stt_meta}

        history = await asyncio.to_thread(self.db.recent_messages, friend_id, 10)
        # 通话内容不写入聊天记录：只做本轮流式回复，挂断后由 call/summary 记一条通话摘要
        user_msg = None
        friend = await asyncio.to_thread(self.db.add_turns, friend_id, 1)
        total_turns = await asyncio.to_thread(self.db.count_turns, friend_id)
        level = social.closeness_level(total_turns)
        friend = await asyncio.to_thread(
            self.db.update_friend,
            friend_id,
            closeness_level=level,
            total_turns=total_turns,
            last_interaction_at=_now_iso(),
        )
        # 通话专用紧凑 prompt：不拼长记忆/表达本/示例，减少 TTFT 与 token 数
        plan = self._call_turn_plan(friend, transcript)
        system_prompt = self._call_system_prompt(friend, plan)
        messages: list[Any] = [{"role": "system", "content": system_prompt}]
        for item in history[-2:]:
            # 通话历史里不要把「通话摘要」当对话内容喂回模型（否则可能被模仿/复读）
            if item.get("msg_type") == "call_summary":
                continue
            role = "user" if item.get("sender") == "user" else "assistant"
            content = (item.get("transcript") or "").strip()
            if content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": transcript})

        full_text = ""
        buffer = ""
        audio_paths: list[str] = []
        fallback_used = False

        async def synth_and_event(sentence: str) -> dict | None:
            event = await self._synth_sentence(sentence, friend)
            if event is None:
                return None
            audio_paths.append(event["path"])
            return {
                "type": "sentence",
                "index": len(audio_paths) - 1,
                "text": sentence,
                "audio_url": event["url"],
            }

        try:
            async for delta in self.engines.llm.stream_chat(
                messages,
                temperature=0.65,
                max_tokens=220,
                # 通话要快：关掉推理模型的思考阶段（省 1~3s）
                extra={"thinking": {"type": "disabled"}},
            ):
                if not delta:
                    continue
                full_text += delta
                buffer += delta
                ready, buffer = _split_ready_sentences(buffer, min_chars=1, max_chars=90)
                for sentence in ready:
                    if not audio_paths and not _matches_language(sentence, lang):
                        # 第一句语言跑偏：整轮改用目标语言模板，避免用户先听到错语言
                        fallback_used = True
                        full_text = self._mock_reply(friend, transcript)
                        event = await synth_and_event(full_text)
                        if event:
                            yield event
                        break
                    event = await synth_and_event(sentence)
                    if event:
                        yield event
                if fallback_used:
                    break
                # 首段音频还没出来时，遇到逗号/分号就先 TTS 一个短 clause，
                # 让用户更早听到声音（剩余部分继续正常按句合成）。
                if not audio_paths and not fallback_used:
                    clause_match = re.search(r"[,;:，；：]", buffer)
                    if clause_match is not None and clause_match.start() >= 3:
                        clause = buffer[: clause_match.end()].strip()
                        if clause and _matches_language(clause, lang):
                            event = await synth_and_event(clause)
                            if event:
                                yield event
                                buffer = buffer[clause_match.end():].lstrip()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 生成失败降级
            log.warning("通话流式生成失败（%s: %s），用模板兜底", type(exc).__name__, exc)
        finally:
            # 通话回合也要记住「这轮用了什么行为」并轻微更新情绪，下一轮才不会重复同一个模式。
            # 只有真的产出了内容才记：用户中途挂断/取消不应该污染行为历史。
            if str(full_text or "").strip():
                self._after_reply_hooks(friend, plan, transcript)

        if not fallback_used and buffer.strip():
            event = await synth_and_event(buffer.strip())
            if event:
                yield event

        if not audio_paths:
            # 完全没有可用音频：用目标语言模板保证通话能继续
            full_text = self._mock_reply(friend, transcript)
            event = await synth_and_event(full_text)
            if event:
                yield event

        if not full_text.strip():
            full_text = self._mock_reply(friend, transcript)

        # 拼成一条 wav 供聊天记录回放 + 落库
        audio_name = ""
        reply_duration = 0
        if audio_paths:
            combined_name = f"call_{int(time.time() * 1000)}_{friend_id[:6]}.wav"
            combined_path = os.path.join(self.audio.audio_dir, combined_name)
            try:
                await asyncio.to_thread(_combine_wavs, audio_paths, combined_path)
                audio_name = combined_name
                with wave.open(combined_path, "rb") as wf:
                    reply_duration = int(wf.getnframes() / wf.getframerate() * 1000)
            except Exception as exc:  # noqa: BLE001
                log.warning("通话音频拼接失败（%s），回退首句音频", exc)
                audio_name = os.path.basename(audio_paths[0])
                reply_duration = 0

        # 同上：通话中的好友回复不落聊天记录
        friend_msg = None
        self._schedule_memory(friend_id)
        yield {
            "type": "done",
            "user_message": user_msg,
            "friend_message": friend_msg,
            "closeness_level": level,
            "total_turns": total_turns,
            "reply_language": self._reply_language(friend),
            "stt": stt_meta,
        }

    # ------------------------------------------------------------------ #
    # 图片多模态回合
    # ------------------------------------------------------------------ #
    async def handle_image(
        self,
        friend_id: str,
        image_bytes: bytes,
        filename: str,
        content_type: str = "",
        caption: str = "",
    ) -> dict:
        """用户发图 → 多模态 LLM 结合图片回复 → TTS，落一条 image 消息。"""
        from app.v3.identity import current_user_id
        async with self.foreground_turn(current_user_id()):
            self.mark_replying(friend_id)
            try:
                result = await self._handle_image(friend_id, image_bytes, filename, content_type, caption)
                return await self._settle_audio(result)
            finally:
                self.clear_replying(friend_id)

    async def _handle_image(
        self,
        friend_id: str,
        image_bytes: bytes,
        filename: str,
        content_type: str = "",
        caption: str = "",
    ) -> dict:
        friend = await asyncio.to_thread(self.db.get_friend, friend_id)
        if friend is None:
            raise KeyError(friend_id)
        if self.media is None:
            raise ValueError("图片存储未初始化")

        image_name = await asyncio.to_thread(
            self.media.save_image, image_bytes, content_type, filename
        )
        text = (caption or "").strip() or "（发来一张图片）"

        history = await asyncio.to_thread(self.db.recent_messages, friend_id, 10)
        user_msg = await asyncio.to_thread(
            self.db.add_message,
            friend_id,
            "user",
            text,
            msg_type="image",
            image_path=image_name,
            is_read=True,
        )

        friend = await asyncio.to_thread(self.db.add_turns, friend_id, 1)
        total_turns = await asyncio.to_thread(self.db.count_turns, friend_id)
        level = social.closeness_level(total_turns)
        friend = await asyncio.to_thread(
            self.db.update_friend,
            friend_id,
            closeness_level=level,
            total_turns=total_turns,
            last_interaction_at=_now_iso(),
        )

        review_exprs = await asyncio.to_thread(self.db.pick_review_expressions, 2)
        referral_friend = await self._maybe_refer(friend)
        reply_text = await self._llm_reply_with_image(
            friend, text, history, review_exprs, image_name
        )
        if referral_friend is not None:
            reply_text = f"{reply_text} {social.referral_line(friend, referral_friend['name'])}".strip()
            await self._notify_referral(friend, referral_friend)

        _display, _tts = split_emotion(reply_text)
        friend_msg = await self.store_friend_voice(friend, _display, _tts)
        self._schedule_memory(friend_id)
        self.spawn_bg(self._expression_mastery_worker(text))

        return {
            "user_message": user_msg,
            "friend_messages": [friend_msg],
            "suggestion_pending": False,
            "closeness_level": level,
            "total_turns": total_turns,
            "memory_updated": False,
            "reply_language": self._reply_language(friend),
            "stt": {"backend": "image", "latency_ms": 0.0},
        }

    async def _llm_reply_with_image(
        self,
        friend: dict,
        caption: str,
        history: list[dict],
        review_exprs: list[dict],
        image_name: str,
    ) -> str:
        if getattr(self.engines.llm, "uses_mock", False):
            return self._mock_image_reply(friend)
        import base64  # noqa: PLC0415

        try:
            with open(self.media.path_for(image_name), "rb") as fh:
                raw = fh.read()
            mime = self.media.mime_for(image_name)
            data_url = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
        except OSError:
            return self._mock_image_reply(friend)

        instruction = (
            f"The user sent you a photo. Caption from them: {caption!r}. "
            "Look at the photo and react like a friend texting: mention one specific thing "
            "you actually see, keep it short and in character."
        )
        plan = self._turn_plan(friend, caption or "")
        messages: list[Any] = [
            {
                "role": "system",
                "content": self._system_prompt(
                    friend, review_exprs, user_text=caption or "", plan=plan
                ),
            }
        ]
        for item in history[-10:]:
            # 通话摘要只是系统记录，不能当对话内容喂给模型（否则会被复读进回复）
            if item.get("msg_type") == "call_summary":
                continue
            role = "user" if item.get("sender") == "user" else "assistant"
            content = (item.get("transcript") or "").strip()
            if content:
                messages.append({"role": role, "content": content})
        messages.append(ChatMessage(role="user", content=instruction, images=(data_url,)))

        chunks: list[str] = []
        try:
            async for delta in self.engines.llm.stream_chat(
                messages, temperature=0.8, max_tokens=500
            ):
                if delta:
                    chunks.append(delta)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 模型不支持图片/网络失败都降级
            log.warning("图片多模态回复失败（%s: %s），用模板兜底", type(exc).__name__, exc)
            return self._mock_image_reply(friend)

        text = _clean_reply("".join(chunks))
        if not text:
            return self._mock_image_reply(friend)
        lang = _language_of(friend)
        if not _matches_language(text, lang):
            fixed = await self._rewrite_in_language(friend, text, lang)
            if fixed:
                return fixed
            return self._mock_image_reply(friend)
        return text

    def _mock_image_reply(self, friend: dict) -> str:
        lang = _language_of(friend)
        defaults = {
            "zh": "这张图看着真不错！你是在哪里拍的呀？",
            "en": "That photo looks great! Where did you take it?",
            "ja": "いい写真だね！どこで撮ったの？",
            "ko": "사진 좋다! 어디서 찍었어?",
            "pt": "Que foto boa! Onde você tirou?",
            "es": "¡Qué buena foto! ¿Dónde la sacaste?",
            "de": "Schönes Foto! Wo hast du das aufgenommen?",
            "fr": "Jolie photo ! Tu l'as prise où ?",
            "it": "Bella foto! Dove l'hai scattata?",
        }
        return defaults.get(lang, defaults["en"])

    # ------------------------------------------------------------------ #
    # 核心回合
    # ------------------------------------------------------------------ #
    async def _handle_user_text(
        self,
        friend: dict,
        transcript: str,
        *,
        msg_type: str,
        audio_filename: str | None = None,
        duration_ms: int = 0,
        context: str | None = None,
        stt_meta: dict[str, Any] | None = None,
        client_message_id: str | None = None,
    ) -> dict:
        fid = friend["id"]
        transcript = (transcript or "").strip()
        if not transcript:
            raise ValueError("空文本")

        turn = await asyncio.to_thread(
            self.db.start_chat_turn, fid, transcript, msg_type=msg_type,
            audio_path=audio_filename, duration_ms=duration_ms, context=context,
            client_message_id=client_message_id)
        friend, history, user_msg = turn['friend'], turn['history'], turn['user_message']
        total_turns, level = turn['total_turns'], turn['closeness_level']
        review_exprs = turn['review_expressions']
        referral_friend = await self._maybe_refer(friend)
        reply_text = await self._llm_reply(friend, transcript, history, review_exprs)
        if referral_friend is not None:
            reply_text = f"{reply_text} {social.referral_line(friend, referral_friend['name'])}".strip()
            await self._notify_referral(friend, referral_friend)

        friend_msg = await self.store_friend_voice(friend, *split_emotion(reply_text), reply_to=user_msg['id'])

        # 商业合作（广告）：**正常回复之后**再以人物口吻追加一条独立商业段落。
        # 广告是独立消息（msg_type="ad"），不进入 friend_messages / prompt / 长期记忆
        # （db.recent_messages 已过滤）。整段吞异常：任何失败都只记日志，绝不影响
        # 正常回复、绝不 500。
        ad_message: dict | None = None
        try:
            ad_message = await self._maybe_ad_message(friend, transcript)
        except Exception as exc:  # noqa: BLE001 - 广告失败不影响聊天主链路
            log.warning("聊天广告投放失败（忽略）: %s: %s", type(exc).__name__, exc)

        # 旁路：润色批注 + 记忆提炼 + 表达本实战检测
        auto_suggestions = os.getenv('SE_CHAT_AUTO_SUGGESTIONS', '0').lower() in ('1', 'true', 'yes')
        if auto_suggestions:
            self.spawn_bg(self._suggestion_worker(user_msg["id"], friend, transcript, _language_of(friend)))
        self._schedule_memory(fid)
        self.spawn_bg(self._expression_mastery_worker(transcript))

        return {
            "user_message": user_msg,
            "friend_messages": [friend_msg],
            "suggestion_pending": auto_suggestions,
            "closeness_level": level,
            "total_turns": total_turns,
            "memory_updated": False,
            "reply_language": self._reply_language(friend),
            "stt": stt_meta or {"backend": "unknown", "latency_ms": 0.0},
            # 商业合作：独立广告消息（无投放时为 None），客户端单独渲染、不计入对话流
            "ad_message": ad_message,
        }

    # ------------------------------------------------------------------ #
    # 商业合作（广告）投放
    # ------------------------------------------------------------------ #
    async def _maybe_ad_message(self, friend: dict, transcript: str) -> dict | None:
        """在正常回复之后尝试投放一条聊天广告；不满足条件/任何异常都返回 None。

        资格（成年+已核验+已开启）、需求识别、频控与原子预留都在
        :func:`app.v3.ads.try_chat_ad` 内部完成；这里只负责取身份、拼前文、
        落一条 ``msg_type="ad"`` 的独立消息并确认投放。
        """
        from app.v3 import ads as ads_mod  # noqa: PLC0415 - 避免 v2/v3 循环导入
        from app.v3.identity import current_user_id  # noqa: PLC0415
        from app.v3.persona_context import persona_id_for_friend  # noqa: PLC0415

        uid = current_user_id()
        if not uid or uid == "local":
            # 设备身份/单用户联调：广告永远不投
            return None
        fid = str(friend.get("id") or "")
        if not fid:
            return None
        persona_id = await asyncio.to_thread(persona_id_for_friend, self.db, friend)
        pre_context = await asyncio.to_thread(self._ad_pre_context, fid)
        ad = await asyncio.to_thread(
            ads_mod.try_chat_ad,
            self.db,
            user_id=uid,
            friend_id=fid,
            persona_id=persona_id,
            user_text=transcript,
            pre_context=pre_context,
        )
        if not ad:
            return None
        context = json.dumps(
            {
                "delivery_id": ad.get("delivery_id", ""),
                "campaign_id": ad.get("campaign_id", ""),
                "campaign_version": int(ad.get("campaign_version") or 0),
                "brand_name": ad.get("brand_name", ""),
                "label": ad.get("label", ""),
                "landing_url": ad.get("landing_url", ""),
                "facts": list(ad.get("facts") or []),
                "persona_id": ad.get("persona_id", ""),
            },
            ensure_ascii=False,
        )
        ad_msg = await asyncio.to_thread(
            self.db.add_message,
            fid,
            "friend",
            ad.get("copy", ""),
            msg_type="ad",
            context=context,
            is_read=False,
        )
        # 消息落库成功 → 确认投放（状态 reserved → delivered）；失败只记日志，
        # 不推翻已经展示给用户的广告消息（否则历史里有一条、当轮却看不到）。
        try:
            await asyncio.to_thread(
                ads_mod.confirm_delivery,
                self.db,
                ad.get("delivery_id", ""),
                message_id=str(ad_msg.get("id") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("广告确认失败（忽略）: %s: %s", type(exc).__name__, exc)
        return {
            "id": ad_msg.get("id"),
            "copy": ad.get("copy", ""),
            "label": ad.get("label", ""),
            "delivery_id": ad.get("delivery_id", ""),
            "brand_name": ad.get("brand_name", ""),
            "landing_url": ad.get("landing_url", ""),
            "facts": list(ad.get("facts") or []),
            "created_at": ad_msg.get("created_at"),
        }

    def _ad_pre_context(self, friend_id: str) -> str:
        """聊天广告的前文：最近 2 条用户消息 transcript 拼接，截断到 300 字以内。"""
        try:
            rows = self.db.list_messages(friend_id, limit=24)
        except Exception as exc:  # noqa: BLE001 - 前文只用于需求识别，失败即空
            log.debug("广告前文读取失败（忽略）: %s: %s", type(exc).__name__, exc)
            return ""
        lines = [
            str(row.get("transcript") or "").strip()
            for row in rows
            if str(row.get("sender") or "") == "user"
            and str(row.get("msg_type") or "") != "ad"
        ]
        lines = [line for line in lines if line][-2:]
        return " / ".join(lines)[:300]

    def trigger_memory(self, friend_id: str) -> None:
        """立即触发记忆提炼（用户切出聊天窗/静默时调用）。"""
        existing = self._memory_tasks.get(friend_id)
        if existing is not None and not existing.done():
            existing.cancel()
        task = self.spawn_bg(self._memory_worker(friend_id, delay_s=0))
        self._memory_tasks[friend_id] = task

    # ------------------------------------------------------------------ #
    # STT / LLM / TTS
    # ------------------------------------------------------------------ #
    async def _transcribe(
        self,
        pcm16: bytes,
        *,
        language: str | None = None,
        prefer_local: bool = False,
        fast: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        """返回 (文本, STT 元信息)，元信息用于前端展示/排查（backend、latency）。

        ``language``：好友回复语言非英语时传给 STT，避免用英语模型硬转写日语/韩语等。
        """
        # 服务器没有本地 Whisper 时，通话等路径也要优先走云端 STT
        try:
            _cloud = self.engines.stt.cloud_health()  # type: ignore[attr-defined]
            if _cloud.get("configured") and _cloud.get("enabled", True):
                prefer_local = False
                fast = False
        except Exception:  # noqa: BLE001
            pass
        try:
            try:
                result = await self.engines.stt.transcribe(
                    pcm16,
                    16000,
                    language=language,
                    prefer_local=prefer_local,
                    fast=fast,
                )
            except TypeError:  # 兼容参数较少的旧 STT 实现
                try:
                    result = await self.engines.stt.transcribe(
                        pcm16, 16000, language=language, prefer_local=prefer_local
                    )
                except TypeError:
                    result = await self.engines.stt.transcribe(
                        pcm16, 16000, language=language
                    )
            text = (getattr(result, "text", "") or "").strip()
            meta = {
                "backend": getattr(result, "backend", "unknown"),
                "latency_ms": round(float(getattr(result, "latency_ms", 0.0) or 0.0), 1),
                "language": getattr(result, "language", None),
            }
            backend = str(meta.get("backend") or "")
            if "sensevoice" in backend:
                health_fn = getattr(self.engines.stt, "cloud_health", None)
                if callable(health_fn):
                    try:
                        model = health_fn().get("model")
                        if model:
                            meta["model"] = model
                    except Exception:  # noqa: BLE001
                        pass
            else:
                stt_cfg = getattr(self.engines.stt, "cfg", None)
                local_model = getattr(stt_cfg, "model_size", None)
                multi = getattr(stt_cfg, "multilingual_model", None)
                lang = str(meta.get("language") or "").lower()
                if lang and lang not in ("en", "english"):
                    if multi:
                        meta["model"] = f"faster-whisper:{multi}"
                elif local_model:
                    meta["model"] = f"faster-whisper:{local_model}"
            return text, meta
        except CapacityError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("STT 失败（降级为空）: %s: %s", type(exc).__name__, exc)
            return "", {"backend": "error", "latency_ms": 0.0, "error": f"{type(exc).__name__}"}

    def _reply_language(self, friend: dict) -> str:
        return _language_of(friend)

    def _system_prompt(
        self,
        friend: dict,
        review_exprs: list[dict],
        *,
        user_text: str = "",
        plan: Any = None,
    ) -> str:
        """文字/图片回合的 system prompt。

        升级后这里**不再拼任何提示词**：只收集输入（friend 行、memory_context、
        TurnPlan），交给 :mod:`app.v3.prompt_composer` 的 A–H 模块统一组装。
        这样「这轮到底喂了什么」可以在管理端预览、也可以按模块覆盖重测。
        """
        friend = friend if isinstance(friend, dict) else {}
        memory_ctx = ""
        memory = getattr(self, "memory", None)
        if memory is not None:
            try:
                memory_ctx = memory.build_context(friend, review_expressions=review_exprs) or ""
            except Exception as exc:  # noqa: BLE001 - 记忆缺失不能拖垮聊天
                log.warning("build_context 失败（忽略）: %s: %s", type(exc).__name__, exc)
                memory_ctx = ""
        try:
            from app.v3 import prompt_composer  # noqa: PLC0415 - 避免 v2/v3 循环导入

            composed = prompt_composer.compose_prompt(
                getattr(self, "db", None),
                friend,
                user_text=str(user_text or ""),
                plan=plan,
                mode="text",
                memory_context=str(memory_ctx or ""),
            )
            log.debug("system prompt composed: %s", composed.summary())
            return composed.text
        except Exception as exc:  # noqa: BLE001 - composer 内部已兜底，这里再兜一层
            log.warning("prompt composer 失败（用极简兜底）: %s: %s", type(exc).__name__, exc)
            return self._fallback_system_prompt(friend)

    def _fallback_system_prompt(self, friend: dict) -> str:
        """composer 完全不可用时的最小 prompt（保持人设与语言，绝不 500）。"""
        name = str(friend.get("name") or "a friend")
        lang_name = LANGUAGE_NAMES.get(_language_of(friend), "English")
        return (
            f"You are {name}, a fictional person chatting on a messaging app. "
            "You are an AI companion, not a real human: keep your persona's voice, and if "
            "the user asks whether you are an AI, answer honestly. "
            f"Reply only in natural {lang_name}. No markdown, no lists. "
            "Only the life facts you are given are true; never invent new ones."
        )

    def _persona_layer(self, friend: dict, user_text: str, plan: Any) -> str:
        """兼容保留：直接返回 Persona 层的旧接口。

        聊天主链路已统一走 :meth:`_system_prompt` → ``prompt_composer``；
        这个方法只给外部调试/老调用方用，不再参与 system prompt 拼接。
        """
        try:
            from app.v3 import persona_context  # noqa: PLC0415 - 避免 v2/v3 循环导入

            if plan is None:
                plan = persona_context.build_turn_plan(
                    getattr(self, "db", None), friend, str(user_text or "")
                )
            return persona_context.build_persona_layer(
                getattr(self, "db", None), friend, plan, language=_language_of(friend)
            )
        except Exception as exc:  # noqa: BLE001 - 生活层失败不影响聊天
            log.debug("persona layer 构建失败（忽略）: %s: %s", type(exc).__name__, exc)
            return ""

    # ------------------------------------------------------------------ #
    # Persona 生活层：本轮计划 + 回合后写回
    # ------------------------------------------------------------------ #
    def _turn_plan(self, friend: dict, user_text: str) -> Any:
        """构建本轮 Persona 计划（DNA / 行为 / 情绪 / 生活事实）。失败返回 None。"""
        try:
            from app.v3 import persona_context  # noqa: PLC0415 - 避免 v2/v3 循环导入

            return persona_context.build_turn_plan(
                getattr(self, "db", None), friend, str(user_text or "")
            )
        except Exception as exc:  # noqa: BLE001 - 生活层失败不影响聊天
            log.debug("build_turn_plan 失败（忽略）: %s: %s", type(exc).__name__, exc)
            return None

    def _after_reply_hooks(self, friend: dict, plan: Any, user_text: str) -> None:
        """回合结束后：记住本轮行为（避免重复模式）+ 轻微更新情绪。"""
        if plan is None:
            return
        try:
            from app.v3 import behavior as behavior_mod  # noqa: PLC0415
            from app.v3 import persona_context  # noqa: PLC0415

            db = getattr(self, "db", None)
            recent = persona_context.record_behavior(db, friend, plan.behavior)
            if isinstance(friend, dict):
                friend["recent_behaviors"] = recent
            persona_context.update_emotion_after_turn(
                db,
                plan,
                intent=behavior_mod.detect_intent(str(user_text or "")),
                behavior_value=plan.behavior,
            )
            log.debug(
                "persona turn=%s behavior=%s scene=%s examples=%d life=%s",
                behavior_mod.detect_intent(str(user_text or "")),
                plan.behavior,
                plan.scene,
                len(plan.examples),
                bool(str(plan.life_block or "").strip()),
            )
        except Exception as exc:  # noqa: BLE001 - 写回失败不影响回复
            log.debug("after_reply hooks 失败（忽略）: %s: %s", type(exc).__name__, exc)

    @staticmethod
    def _pick_mock_line(pool: tuple[str, ...], *, want_question: bool, seed: int) -> str:
        """按「这轮是否允许提问」挑模板：不允许提问时**不会**出现问句。"""
        questions = tuple(x for x in pool if "?" in x or "？" in x)
        statements = tuple(x for x in pool if "?" not in x and "？" not in x)
        candidates = questions if (want_question and questions) else (statements or questions or pool)
        return candidates[seed % len(candidates)]

    def _call_turn_plan(self, friend: dict, transcript: str) -> Any:
        """通话版本轮计划：与文字聊天同源，但不注入示例对话（省首字延迟）。"""
        try:
            from app.v3 import persona_context  # noqa: PLC0415

            return persona_context.build_call_plan(
                getattr(self, "db", None), friend, str(transcript or "")
            )
        except Exception as exc:  # noqa: BLE001 - 生活层失败不影响通话
            log.debug("build_call_plan 失败（忽略）: %s: %s", type(exc).__name__, exc)
            return None

    def _call_persona_layer(self, friend: dict, plan: Any, *, language: str) -> str:
        """兼容保留：通话 Persona 层旧接口（主链路已统一走 prompt_composer）。"""
        if plan is None:
            return ""
        try:
            from app.v3 import persona_context  # noqa: PLC0415

            return persona_context.build_call_layer(
                getattr(self, "db", None), friend, plan, language=language
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("通话 persona 层构建失败（忽略）: %s: %s", type(exc).__name__, exc)
            return ""

    def _call_system_prompt(self, friend: dict, plan: Any) -> str:
        """通话 system prompt：统一由 ``compose_call_prompt`` 组装的精简版。

        与文字模式**同一份角色卡来源**（personas 行 + Speech DNA），只是模块更少、
        预算更短（约 1100 字符），保证语音链路 TTFT。
        """
        friend = friend if isinstance(friend, dict) else {}
        try:
            from app.v3 import prompt_composer  # noqa: PLC0415 - 避免 v2/v3 循环导入

            composed = prompt_composer.compose_call_prompt(
                getattr(self, "db", None), friend, transcript="", plan=plan
            )
            log.debug("call prompt composed: %s", composed.summary())
            return composed.text
        except Exception as exc:  # noqa: BLE001 - 通话不能因为 prompt 组装失败而静音
            log.warning("call prompt composer 失败（极简兜底）: %s: %s", type(exc).__name__, exc)
            name = str(friend.get("name") or "a friend")
            lang_name = LANGUAGE_NAMES.get(_language_of(friend), "English")
            return (
                f"You are {name}, a fictional AI character on a LIVE voice call. "
                "If asked whether you are AI, answer honestly in your own voice. "
                f"Reply only in natural {lang_name}, one or two short spoken sentences. "
                "No greeting, no markdown."
            )

    def _mock_reply(self, friend: dict, user_text: str, *, plan: Any = None) -> str:
        """无 key 时的 deterministic 人设回复。

        升级后不再是「客服句式」：是否提问由本轮行为决定；长度倾向跟随 verbosity。
        非英语好友用该语言的模板，保证「选日语就真的回日语」在演示模式下也成立。
        """
        quirks = friend.get("speech_quirks") or []
        if isinstance(quirks, str):
            quirks = [quirks]
        quirk = str(quirks[0]).strip() if quirks else ""
        city = str(friend.get("city") or "here")
        lang = _language_of(friend)
        from app.v3.behavior import detect_intent  # noqa: PLC0415

        context_lines = _MOCK_CONTEXT_REPLIES.get(lang, _MOCK_CONTEXT_REPLIES["en"])
        if _IDENTITY_QUESTION_RE.search(str(user_text or "").lower()):
            return context_lines[6]
        intent = detect_intent(user_text)
        if intent in _MOCK_INTENT_INDEX:
            return context_lines[_MOCK_INTENT_INDEX[intent]]
        seed = zlib.crc32(f"{friend.get('id','')}|{user_text}".encode("utf-8")) & 0xFFFFFFFF

        behavior = str(getattr(plan, "behavior", "") or "")
        traits = dict(getattr(plan, "dna_traits", None) or {})
        # 只有 ask_question 这一轮允许以问句结尾；行为未知（通话 mock 等老路径）保持原样
        want_question = behavior == "ask_question"
        if behavior in ("acknowledge",) and traits.get("verbosity", 0.5) > 0.5:
            want_question = False
        # 口头禅是「调味」不是固定前缀：约 40% 的回复才带，且不会被堆叠
        use_quirk = bool(quirk) and ((seed >> 8) % 5) < 2

        if lang != "en":
            pool = _MOCK_LANG_REPLIES.get(lang) or _MOCK_DEFAULT_REPLIES
            prefix = _native_quirk_prefix(quirk, lang) if use_quirk else ""
            return _clean_reply(
                self._pick_mock_line(pool, want_question=want_question, seed=seed).format(
                    q=prefix, city=city
                )
            )

        prefix = f"{quirk[:1].upper()}{quirk[1:]}, " if use_quirk else ""
        topic = "default"
        for name, pattern in _TOPIC_PATTERNS:
            if pattern.search(user_text or ""):
                topic = name
                break
        pool = _MOCK_TOPIC_REPLIES.get(topic) or _MOCK_DEFAULT_REPLIES
        return _clean_reply(
            self._pick_mock_line(pool, want_question=want_question, seed=seed).format(
                q=prefix, city=city
            )
        )

    async def _llm_reply(
        self,
        friend: dict,
        user_text: str,
        history: list[dict],
        review_exprs: list[dict],
    ) -> str:
        """一轮回复：**先做行为选择**（零 LLM），再拼 prompt 生成。

        普通一轮仍然只有 1 次 LLM 调用；只有回复语言不对时才多一次纠偏调用。
        """
        plan = await asyncio.to_thread(self._turn_plan, friend, user_text)
        reply = ""
        try:
            if getattr(self.engines.llm, "uses_mock", False):
                # 无 key（mock）时走 deterministic 人设回复，但同样遵守本轮行为
                reply = self._mock_reply(friend, user_text, plan=plan)
                return reply

            messages: list[dict[str, str]] = [
                {
                    "role": "system",
                    "content": await asyncio.to_thread(self._system_prompt,
                        friend, review_exprs, user_text=user_text, plan=plan
                    ),
                }
            ]
            for item in history[-10:]:
                # 通话摘要只是系统记录，不能当对话内容喂给模型（否则会被复读进回复）
                if item.get("msg_type") == "call_summary":
                    continue
                role = "user" if item.get("sender") == "user" else "assistant"
                content = (item.get("transcript") or "").strip()
                if content:
                    messages.append({"role": role, "content": content})
            messages.append({"role": "user", "content": user_text})

            chunks: list[str] = []
            try:
                async for delta in self.engines.llm.stream_chat(
                    messages, temperature=0.85, max_tokens=600
                ):
                    if delta:
                        chunks.append(delta)
            except asyncio.CancelledError:
                raise
            except CapacityError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("LLM 回复失败（使用规则兜底）: %s: %s", type(exc).__name__, exc)

            text = _clean_reply("".join(chunks))
            if not text:
                reply = self._mock_reply(friend, user_text, plan=plan)
                return reply
            lang = _language_of(friend)
            if _matches_language(text, lang):
                reply = text
                return reply
            log.info(
                "好友回复语言不符（期望 %s，实际 %r），尝试纠偏",
                lang, text[:80],
            )
            fixed = await self._rewrite_in_language(friend, text, lang)
            # 纠偏失败：用目标语言的确定性回复兜底，绝不发错语言
            reply = fixed or self._mock_reply(friend, user_text, plan=plan)
            return reply
        finally:
            await asyncio.to_thread(self._after_reply_hooks, friend, plan, user_text)

    async def _rewrite_in_language(self, friend: dict, text: str, lang: str) -> str | None:
        """让模型把自己刚写错语言的回复改写成目标语言；失败返回 None。"""
        lang_name = LANGUAGE_NAMES.get(lang, lang)
        messages = [
            {
                "role": "system",
                "content": (
                    f"You rewrite a chat reply so it is entirely in natural {lang_name}. "
                    "Keep the meaning, tone and any personal details. "
                    f"Output ONLY the rewritten {lang_name} reply — no English sentences, "
                    "no translation, no explanation."
                ),
            },
            {"role": "user", "content": text},
        ]
        chunks: list[str] = []
        try:
            async for delta in self.engines.llm.stream_chat(
                messages, temperature=0.2, max_tokens=800
            ):
                if delta:
                    chunks.append(delta)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("回复语言纠偏失败（%s: %s）", type(exc).__name__, exc)
            return None
        fixed = _clean_reply("".join(chunks))
        if fixed and _matches_language(fixed, lang):
            return fixed
        return None

    def _fallback_reply(self, friend: dict) -> str:
        """最后兜底（LLM 与 mock 都不可用时）。

        旧实现是四句「that sounds interesting / how did that make you feel」的客服话术，
        正是人物模板化的来源之一。这里改成最短的中性反应——**不提问**。
        """
        return _MOCK_CONTEXT_REPLIES.get(_language_of(friend), _MOCK_CONTEXT_REPLIES["en"])[4]

#: 情绪 -> (rate 增量, pitch 增量)。值是相对该好友基础 prosody 的偏移。


    def _voice_binding(self, friend: dict) -> Any:
        """该好友的声音身份（与动态朗读/通话共用同一个 resolver）。

        有 ``persona_id`` 时**实时**读 personas 行：好友行里的 fish_voice_id 是建好友时的快照，
        重新分配音色后不同步会导致「聊天一个声音、动态另一个声音」。
        """
        try:
            from app.voice import voice_registry  # noqa: PLC0415

            return voice_registry.binding_for_friend(
                getattr(self, "db", None), friend, cfg=self._tts_cfg()
            )
        except Exception as exc:  # noqa: BLE001 - 音色解析失败不阻断聊天
            if os.getenv("SE_VOICE_STRICT", "0").lower() in ("1", "true", "yes"):
                raise
            log.debug("解析好友音色失败（用好友行兜底）: %s", exc)
            return None

    def _tts_cfg(self) -> Any:
        """读取 TTS 配置（chat_service 自带 store 优先）。"""
        try:
            store = getattr(self, "tts_config", None)
            if store is not None:
                return store.load()
        except Exception as exc:  # noqa: BLE001
            log.debug("读取 TTS 配置失败: %s", exc)
        return None

    async def _fish_voice(
        self, text: str, emotion: str, friend: dict | None = None
    ) -> bytes | None:
        """走 Fish 合成；未启用/失败返回 None（调用方回落 edge）。"""
        try:
            from app.voice import voice_registry  # noqa: PLC0415

            # SE_TTS_FISH=0（测试/离线环境）时整条链路都用 Edge，避免测试打真实 API
            if voice_registry.load_cfg() is None:
                return None
        except Exception:  # noqa: BLE001
            return None
        try:
            store = getattr(self, "tts_config", None)
            if store is None:
                # 懒加载：从 settings.data_path 建一个（services 启动时也能覆盖）
                try:
                    from app.config import get_settings  # noqa: PLC0415
                    from app.voice.tts_config import TTSConfigStore  # noqa: PLC0415

                    store = TTSConfigStore(str(get_settings().data_path))
                    self.tts_config = store
                except Exception:  # noqa: BLE001
                    return None
            cfg = store.load()
            if not voice_registry.fish_configured(cfg):
                return None
            from app.voice import fish_tts  # noqa: PLC0415

            binding = self._voice_binding(friend)
            # Fish 已熔断 / 该角色没有 Fish 音色 → 直接走 Edge，不再等超时
            if binding is not None and not binding.fish_enabled:
                return None
            # LLM 已经给了 [tag] 就不再叠加，避免 "[excited] [sympathetic] ..."
            has_tag = any(f"[{tag}]" in (text or "").lower() for tag in ALLOWED_EMOTION_TAGS)
            tagged = text if has_tag else _tag_emotion(text, emotion)
            return await fish_tts.synthesize(
                tagged,
                api_key=cfg.fish_api_key,
                base_url=cfg.fish_base_url,
                model=cfg.fish_model,
                reference_id=(
                    binding.ref if binding is not None and binding.ref
                    else str((friend or {}).get("fish_voice_id") or "") or cfg.fish_reference_id
                ),
                audio_format=cfg.fish_format or "wav",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Fish TTS 失败，回落 edge: %s", exc)
            return None

    async def store_friend_voice(
        self, friend: dict, text: str, tts_text: str | None = None, *, reply_to: str | None = None
    ) -> dict:
        """Persist explicit audio state; foreground admission stays until audio settles.

        The opt-in deferred mode can expose the queued row earlier through events.
        Failure/retry always updates this message rather than generating another reply.
        """
        fid = friend["id"]
        def insert_pending():
            msg = self.db.add_message(
                fid, "friend", text, msg_type="voice", is_read=False,
                audio_path=None, duration_ms=0, audio_status="queued",
                context=json.dumps({'reply_to': reply_to}) if reply_to else None,
                reply_to_message_id=reply_to or '')
            self.db.touch_friend(fid)
            return msg
        inserting = asyncio.create_task(asyncio.to_thread(insert_pending))
        try:
            msg = await asyncio.shield(inserting)
        except asyncio.CancelledError:
            msg = await inserting
            await asyncio.to_thread(self.db.update_message, msg['id'], audio_status='failed',
                                    audio_error='语音生成已取消，请重试')
            from app.v2.audio_events import publish
            await asyncio.to_thread(publish, self.db, friend, msg['id'], 'failed')
            raise
        task = asyncio.create_task(self._deferred_voice_worker(msg["id"], friend, text, tts_text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if not hasattr(self, "_voice_tasks"):
            self._voice_tasks = {}
        self._voice_tasks[msg["id"]] = task
        task.add_done_callback(lambda done: self._voice_tasks.pop(msg["id"], None))
        owned = _turn_audio_tasks.get()
        if owned is not None:
            owned.append((msg['id'], friend, task))
        if _tts_defer_audio_enabled():
            return msg
        await task
        return await asyncio.to_thread(self.db.get_message, msg["id"])

    async def _settle_audio(self, result: dict) -> dict:
        for message in result.get("friend_messages", []):
            task = self._voice_tasks.get(message["id"])
            if task is not None:
                await task
        result["friend_messages"] = [
            (await asyncio.to_thread(self.db.get_message, m["id"])) or m
            for m in result.get("friend_messages", [])]
        return result

    async def _deferred_voice_worker(self, message_id, friend, text, tts_text) -> None:
        from app.jobs import run_db_thread
        from app.v2.audio_events import publish
        from app.v3.media_access import register_asset, KIND_AUDIO
        status, error = "failed", "语音生成失败，请重试"
        try:
            await run_db_thread(self.db.update_message, message_id, audio_status="generating", audio_error="")
            audio_filename, duration_ms = await self._synth_friend_audio(friend, text, tts_text)
            if audio_filename:
                owner = str(friend.get("owner_user_id") or "local")
                await run_db_thread(register_asset, self.db, owner_user_id=owner,
                                        kind=KIND_AUDIO, filename=audio_filename)
                await run_db_thread(self.db.update_message, message_id,
                                        audio_path=audio_filename, duration_ms=int(duration_ms or 0),
                                        audio_status="ready", audio_error="")
                status, error = "ready", ""
        except asyncio.CancelledError:
            error = "语音生成超时或已取消，请重试"
            raise
        except Exception as exc:
            log.warning("Audio synthesis failed message=%s: %s", message_id, type(exc).__name__)
        finally:
            if status != "ready":
                await run_db_thread(self.db.update_message, message_id,
                                        audio_status="failed", audio_error=error)
            await run_db_thread(publish, self.db, friend, message_id, status)

    async def retry_audio(self, message_id: str) -> dict:
        from app.v3.identity import current_user_id
        async with self.admission.turn(current_user_id()):
            message = await asyncio.to_thread(self.db.get_message, message_id)
            if message is None or message.get("sender") != "friend":
                raise KeyError(message_id)
            friend = await asyncio.to_thread(self.db.get_friend, message["friend_id"], owner_user_id=current_user_id())
            if friend is None:
                raise KeyError(message_id)
            if message.get("audio_path"):
                return message
            if message_id in self._voice_tasks:
                raise CapacityError("语音正在生成，请稍后重试")
            await self._deferred_voice_worker(message_id, friend, message.get("transcript") or "", None)
            return await asyncio.to_thread(self.db.get_message, message_id)

    async def _synth_friend_audio(
        self, friend: dict, text: str, tts_text: str | None = None
    ) -> tuple[str | None, int]:
        """合成一条好友语音，返回 (音频文件名, 时长毫秒)；失败时返回 (None, 0)。"""
        audio_filename: str | None = None
        duration_ms = 0
        try:
            speak_text = _strip_emoji(tts_text or text) or (tts_text or text)
            rate, pitch, emotion = emotion_prosody(
                speak_text,
                friend.get("voice_rate") or "+0%",
                friend.get("voice_pitch") or "+0Hz",
            )
            log.debug(
                "tts emotion=%s rate=%s pitch=%s text=%r",
                emotion, rate, pitch, speak_text[:60],
            )

            # Fish 优先：把情绪映射成行内标记（[excited] / [laughing] ...），
            # 这是唯一能真正「演」出来的路径；失败自动回落 edge。
            path = None
            duration_ms = 0
            fish_audio = await self._fish_voice(speak_text, emotion, friend)
            if fish_audio is not None:
                try:
                    import uuid  # noqa: PLC0415

                    # Fish 回 mp3（wav 头帧数不可靠）。时长按 128kbps CBR 估算，
                    # 只用于气泡长度和进度条，不需要精确。
                    ext = (self.tts_config.load().fish_format or "mp3").lower()
                    fname = f"fish_{uuid.uuid4().hex[:12]}.{ext}"
                    fpath = os.path.join(self.audio.audio_dir, fname)
                    await asyncio.to_thread(_write_bytes, fpath, fish_audio)
                    path = fpath
                    duration_ms = max(500, _fish_duration_ms(fish_audio))
                except Exception as exc:  # noqa: BLE001
                    log.warning("Fish 音频落盘失败，回落 edge: %s", exc)
                    path = None

            if path is None:
                binding = self._voice_binding(friend)
                if binding is not None and getattr(binding, "strict", False) and binding.fish_enabled:
                    raise RuntimeError("speech-unavailable")
                edge_voice = (
                    binding.edge_voice
                    if binding is not None
                    else (friend.get("voice_id") or "en-GB-RyanNeural")
                )
                # Edge 不认识 [excited]：必须去掉标记，否则会把 "excited" 念出来
                edge_text = strip_emotion_tags(speak_text) or strip_emotion_tags(text) or speak_text
                path, duration_ms = await self.voice.synth_to_wav(
                    edge_text,
                    edge_voice,
                    rate=rate,
                    pitch=pitch,
                    out_dir=self.audio.audio_dir,
                )
            audio_filename = os.path.basename(path)
        except Exception as exc:  # noqa: BLE001 - tone 再兜一层
            log.warning("TTS 合成失败（落 tone）: %s: %s", type(exc).__name__, exc)
            try:
                if os.getenv("SE_VOICE_STRICT", "0").lower() in ("1", "true", "yes"):
                    raise RuntimeError("speech-unavailable")
                from app.voice.tone_tts import ToneSynthesizer  # noqa: PLC0415

                pcm = ToneSynthesizer(sample_rate=24000).synthesize(text)
                audio_filename = self.audio.save_tone_wav(pcm)
                duration_ms = int(len(pcm) / 24000 * 1000)
            except Exception as exc2:  # noqa: BLE001
                log.warning("tone 兜底也失败（无音频）: %s", exc2)
                audio_filename = None

        return audio_filename, int(duration_ms or 0)

    # ------------------------------------------------------------------ #
    # 裂变推荐
    # ------------------------------------------------------------------ #
    async def _maybe_refer(self, friend: dict) -> dict | None:
        try:
            if not social.should_refer(friend):
                return None
            friends = await asyncio.to_thread(self.db.list_friends)
            if any(f.get("referred_by_friend_id") == friend["id"] for f in friends):
                return None
            new_friend = await self.spawner.spawn(
                referred_by_friend_id=friend["id"], status="pending_request"
            )
            log.info("好友裂变：%s 推荐了新朋友 %s", friend.get("name"), new_friend.get("name"))
            return new_friend
        except Exception as exc:  # noqa: BLE001
            log.warning("好友裂变失败（忽略）: %s: %s", type(exc).__name__, exc)
            return None

    async def _notify_referral(self, friend: dict, new_friend: dict) -> None:
        """把推荐人名字/关系落库（新好友申请里展示）。"""
        try:
            request = (
                f"Yo! {friend.get('name', 'a friend')} told me about you — "
                f"said we'd get along. I'm {new_friend.get('name', '')}."
            )
            await asyncio.to_thread(
                self.db.update_friend, new_friend["id"], request_message=request
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("更新 referral request_message 失败: %s", exc)

    # ------------------------------------------------------------------ #
    # 旁路 worker
    # ------------------------------------------------------------------ #
    async def _suggestion_worker(
        self, message_id: str, friend: dict, text: str, language: str = "en"
    ) -> None:
        try:
            result = await generate_suggestion(
                self.engines.llm,
                text,
                friend_name=friend.get("name", ""),
                friend_context=friend.get("personality_prompt", "")[:200],
                language=language,
            )
            await asyncio.to_thread(
                self.db.update_message, message_id, suggestion=result
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("润色 worker 失败（忽略）: %s: %s", type(exc).__name__, exc)

    def _schedule_memory(self, friend_id: str) -> None:
        existing = self._memory_tasks.get(friend_id)
        if existing is not None and not existing.done():
            return
        task = self.spawn_bg(self._memory_worker(friend_id))
        self._memory_tasks[friend_id] = task
        task.add_done_callback(lambda done: self._memory_tasks.pop(friend_id, None)
                               if self._memory_tasks.get(friend_id) is done else None)

    async def _memory_worker(self, friend_id: str, delay_s: float | None = None) -> None:
        try:
            delay = self.memory_idle_s if delay_s is None else max(0.0, float(delay_s))
            if delay > 0:
                await asyncio.sleep(delay)
            from app.jobs import background_scope
            async with self._memory_slots:
                with background_scope(lambda: self.admission.busy):
                    while self.admission.busy:
                        await asyncio.sleep(.5)
                    friend = await asyncio.to_thread(self.db.get_friend, friend_id)
                    if friend is None:
                        return
                    recent = await asyncio.to_thread(self.db.recent_messages, friend_id, 20)
                    fingerprint = str(recent[-1].get('id') or recent[-1].get('created_at')) if recent else ''
                    if not fingerprint or self._memory_last.get(friend_id) == fingerprint:
                        return
                    result = await self.memory.after_turn(friend, recent_messages=recent)
                    if not result.get('error'):
                        if len(self._memory_last) >= 2000:
                            self._memory_last.pop(next(iter(self._memory_last)))
                        self._memory_last[friend_id] = fingerprint
                    log.info("记忆提炼完成 friend=%s keys=%s", friend_id, list(result or {}))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("记忆 worker 失败（忽略）: %s: %s", type(exc).__name__, exc)
        finally:
            self._memory_tasks.pop(friend_id, None)

    async def _expression_mastery_worker(self, transcript: str) -> None:
        """用户实战用出表达本短语 → 熟练度 +1，满 3 次标记 mastered。"""
        try:
            exprs = await asyncio.to_thread(self.db.list_expressions, "learning")
            norm_text = _norm(transcript)
            if not norm_text:
                return
            for expr in exprs:
                phrase = _norm(expr.get("better_expression") or "")
                if len(phrase) < 3:
                    continue
                # 取第一个可选表达（"/" 或 "…" 分隔）
                candidate = re.split(r"[/…]| or ", phrase)[0].strip()
                if len(candidate) < 3:
                    continue
                if candidate in norm_text:
                    count = int(expr.get("mastery_count") or 0) + 1
                    fields: dict[str, Any] = {"mastery_count": count}
                    if count >= 3:
                        fields["status"] = "mastered"
                    await asyncio.to_thread(self.db.update_expression, expr["id"], **fields)
                    log.info("表达实战命中：%s (count=%d)", candidate, count)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.debug("表达熟练度检测失败（忽略）: %s", exc)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")
