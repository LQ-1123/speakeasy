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

import asyncio
import re
import time
import zlib
from typing import Any

from app.llm.client import ChatMessage
from app.logging_utils import get_logger
from app.v2 import social
from app.v2.suggestion import generate_suggestion

log = get_logger(__name__)

_MAX_REPLY_CHARS = 420
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
    ),
    "games": (
        "{q}indie games are the best — no big studio nonsense. Which one are you playing right now?",
        "{q}I lose whole evenings to games like that. What's the last one that got you hooked?",
    ),
    "interview": (
        "{q}an interview on Friday? That's exciting. How are you prepping for it?",
        "{q}you'll be fine — just be yourself. What's the role about?",
    ),
    "english": (
        "{q}your English is way better than you think. What part feels hardest right now?",
        "{q}the trick is to stop translating in your head. What do you usually talk about in English?",
    ),
    "food": (
        "{q}now I'm hungry. What's the best thing you've eaten this week?",
        "{q}food is the most important part of the day, obviously. Do you cook much?",
    ),
    "travel": (
        "{q}travel changes your whole brain, I swear. Where do you want to go next?",
        "{q}I'd pack a bag tomorrow if I could. What's your dream trip?",
    ),
    "work": (
        "{q}that sounds like a lot. How are you balancing it with everything else?",
        "{q}sounds busy — is it the kind of busy you enjoy, or the exhausting kind?",
    ),
    "music": (
        "{q}music is basically medicine. What have you had on repeat lately?",
        "{q}my taste is all over the place. What are you listening to these days?",
    ),
}

_MOCK_DEFAULT_REPLIES: tuple[str, ...] = (
    "{q}that's interesting. Tell me a bit more about that?",
    "{q}I get what you mean. How did that make you feel?",
    "{q}haha, seriously? What happened next?",
    "{q}fair enough. What are you up to later today?",
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
    ),
    "ko": (
        "{q}그렇구나. 요즘 뭐 하고 지내?",
        "{q}오, 재밌다. 주말에 뭐 해?",
        "{q}그래? 좀 더 얘기해 줄래?",
    ),
    "pt": (
        "{q}entendi. E o que você tem feito ultimamente?",
        "{q}que legal. O que você costuma fazer no fim de semana?",
        "{q}sério? Me conta mais sobre isso.",
    ),
    "es": (
        "{q}ya veo. ¿Y qué has estado haciendo últimamente?",
        "{q}qué bien. ¿Qué te gusta hacer los fines de semana?",
        "{q}¿en serio? Cuéntame un poco más.",
    ),
    "de": (
        "{q}verstehe. Und was machst du so in letzter Zeit?",
        "{q}schön. Was machst du am Wochenende?",
        "{q}echt? Erzähl mir mehr davon.",
    ),
    "fr": (
        "{q}je vois. Et qu'est-ce que tu fais en ce moment ?",
        "{q}super. Qu'est-ce que tu fais le week-end ?",
        "{q}sérieux ? Raconte-moi un peu plus.",
    ),
    "it": (
        "{q}capisco. E cosa fai in questi giorni?",
        "{q}bello. Cosa fai nel fine settimana?",
        "{q}davvero? Raccontami qualcosa in più.",
    ),
    "zh": (
        "{q}原来是这样。你最近在忙什么？",
        "{q}挺好的。周末一般做什么？",
        "{q}真的吗？多说一点吧。",
    ),
}


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


def _clean_reply(text: str) -> str:
    text = _MARKDOWN_RE.sub("", text or "")
    text = re.sub(r"\s+", " ", text).strip().strip('"')
    if len(text) > _MAX_REPLY_CHARS:
        cut = text[:_MAX_REPLY_CHARS]
        # 尽量在句末截断
        for ch in (".", "!", "?"):
            idx = cut.rfind(ch)
            if idx > _MAX_REPLY_CHARS * 0.5:
                return cut[: idx + 1]
        return cut + "…"
    return text


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9' ]+", " ", (text or "").lower()).strip()


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

    # ------------------------------------------------------------------ #
    # 后台任务
    # ------------------------------------------------------------------ #
    def spawn_bg(self, coro) -> asyncio.Task:
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
        friend = await asyncio.to_thread(self.db.get_friend, friend_id)
        if friend is None:
            raise KeyError(friend_id)

        decoded = await asyncio.to_thread(self.audio.decode_upload, audio_bytes, filename)
        reply_lang = _language_of(friend)
        stt_language = None if reply_lang == "en" else reply_lang
        transcript, stt_meta = await self._transcribe(decoded["pcm16"], language=stt_language)
        if not transcript:
            transcript = "（语音没有识别清楚，可以再说一次吗？）"
        dur = int(decoded["duration_ms"] or duration_ms or 0)
        return await self._handle_user_text(
            friend,
            transcript,
            msg_type="voice",
            audio_filename=decoded["audio_filename"],
            duration_ms=dur,
            context=context,
            stt_meta=stt_meta,
        )

    async def handle_text(
        self,
        friend_id: str,
        text: str,
        context: str | None = None,
    ) -> dict:
        friend = await asyncio.to_thread(self.db.get_friend, friend_id)
        if friend is None:
            raise KeyError(friend_id)
        return await self._handle_user_text(
            friend, text, msg_type="text", context=context,
            stt_meta={"backend": "text", "latency_ms": 0.0},
        )

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

        friend_msg = await self.store_friend_voice(friend, reply_text)
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
            "Look at the photo carefully and react like a friend in a chat: mention one "
            "specific thing you see, then ask one natural follow-up question. "
            "Keep it to 1-2 short spoken sentences."
        )
        messages: list[Any] = [
            {"role": "system", "content": self._system_prompt(friend, review_exprs)}
        ]
        for item in history[-10:]:
            role = "user" if item.get("sender") == "user" else "assistant"
            content = (item.get("transcript") or "").strip()
            if content:
                messages.append({"role": role, "content": content})
        messages.append(ChatMessage(role="user", content=instruction, images=(data_url,)))

        chunks: list[str] = []
        try:
            async for delta in self.engines.llm.stream_chat(
                messages, temperature=0.8, max_tokens=160
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
    ) -> dict:
        fid = friend["id"]
        transcript = (transcript or "").strip()
        if not transcript:
            raise ValueError("空文本")

        history = await asyncio.to_thread(self.db.recent_messages, fid, 10)
        user_msg = await asyncio.to_thread(
            self.db.add_message,
            fid,
            "user",
            transcript,
            msg_type=msg_type,
            audio_path=audio_filename,
            duration_ms=duration_ms,
            is_read=True,
            context=context,
        )

        # 熟稔度 / 轮数
        friend = await asyncio.to_thread(self.db.add_turns, fid, 1)
        total_turns = await asyncio.to_thread(self.db.count_turns, fid)
        level = social.closeness_level(total_turns)
        friend = await asyncio.to_thread(
            self.db.update_friend,
            fid,
            closeness_level=level,
            total_turns=total_turns,
            last_interaction_at=_now_iso(),
        )

        # 好友回复（LLM + 裂变推荐）
        review_exprs = await asyncio.to_thread(self.db.pick_review_expressions, 2)
        referral_friend = await self._maybe_refer(friend)
        reply_text = await self._llm_reply(friend, transcript, history, review_exprs)
        if referral_friend is not None:
            reply_text = f"{reply_text} {social.referral_line(friend, referral_friend['name'])}".strip()
            await self._notify_referral(friend, referral_friend)

        friend_msg = await self.store_friend_voice(friend, reply_text)

        # 旁路：润色批注 + 记忆提炼 + 表达本实战检测
        self.spawn_bg(
            self._suggestion_worker(user_msg["id"], friend, transcript, _language_of(friend))
        )
        self._schedule_memory(fid)
        self.spawn_bg(self._expression_mastery_worker(transcript))

        return {
            "user_message": user_msg,
            "friend_messages": [friend_msg],
            "suggestion_pending": True,
            "closeness_level": level,
            "total_turns": total_turns,
            "memory_updated": False,
            "reply_language": self._reply_language(friend),
            "stt": stt_meta or {"backend": "unknown", "latency_ms": 0.0},
        }

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
        self, pcm16: bytes, *, language: str | None = None
    ) -> tuple[str, dict[str, Any]]:
        """返回 (文本, STT 元信息)，元信息用于前端展示/排查（backend、latency）。

        ``language``：好友回复语言非英语时传给 STT，避免用英语模型硬转写日语/韩语等。
        """
        try:
            try:
                result = await self.engines.stt.transcribe(pcm16, 16000, language=language)
            except TypeError:  # 兼容不支持 language 参数的旧 STT 实现
                result = await self.engines.stt.transcribe(pcm16, 16000)
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
                lang = str(meta.get("language") or "").lower()
                if lang and lang not in ("en", "english"):
                    multi = getattr(stt_cfg, "multilingual_model", None)
                    if multi:
                        meta["model"] = f"faster-whisper:{multi}"
                else:
                    local_model = getattr(stt_cfg, "model_size", None)
                    if local_model:
                        meta["model"] = f"faster-whisper:{local_model}"
            return text, meta
        except Exception as exc:  # noqa: BLE001
            log.warning("STT 失败（降级为空）: %s: %s", type(exc).__name__, exc)
            return "", {"backend": "error", "latency_ms": 0.0, "error": f"{type(exc).__name__}"}

    def _reply_language(self, friend: dict) -> str:
        return _language_of(friend)

    def _system_prompt(self, friend: dict, review_exprs: list[dict]) -> str:
        persona = _sanitize_persona_language((friend.get("personality_prompt") or "").strip())
        try:
            memory_ctx = self.memory.build_context(friend, review_expressions=review_exprs)
        except Exception as exc:  # noqa: BLE001
            log.warning("build_context 失败（忽略）: %s: %s", type(exc).__name__, exc)
            memory_ctx = ""
        lang = _language_of(friend)
        lang_name = LANGUAGE_NAMES[lang]
        if lang == "en":
            language_rule = "Reply with 1-2 short sentences of natural spoken English. "
        else:
            language_rule = (
                f"You always reply in natural {lang_name}. Even if the learner writes in English or "
                f"mixes languages, answer ONLY in {lang_name} — never write an English sentence "
                "as the reply, and never translate. Keep it to 1-2 short spoken sentences, "
                "like a real chat message. "
            )
        base = (
            f"You are {friend.get('name', 'a friend')} from {friend.get('city', '')}, "
            f"{friend.get('nationality', '')}. You are chatting with your friend on a messaging app. "
            + language_rule
            + "Never mention being an AI. Always leave a hook (a question or an invitation to continue). "
            "You may occasionally add ONE fitting emoji at the very end (not in the middle of a "
            "sentence); no markdown, no action descriptions."
        )
        return "\n\n".join(p for p in (persona, base, memory_ctx) if p)

    def _mock_reply(self, friend: dict, user_text: str) -> str:
        """无 key 时的 deterministic 人设回复（比通用模板更像真人）。

        非英语好友用该语言的模板，保证「选日语就真的回日语」在演示模式下也成立。
        """
        quirks = friend.get("speech_quirks") or []
        quirk = str(quirks[0]).strip() if quirks else ""
        city = str(friend.get("city") or "here")
        lang = _language_of(friend)
        seed = zlib.crc32(f"{friend.get('id','')}|{user_text}".encode("utf-8")) & 0xFFFFFFFF

        if lang != "en":
            pool = _MOCK_LANG_REPLIES.get(lang) or _MOCK_DEFAULT_REPLIES
            prefix = _native_quirk_prefix(quirk, lang)
            text = pool[seed % len(pool)].format(q=prefix, city=city)
            return _clean_reply(text)

        prefix = f"{quirk[:1].upper()}{quirk[1:]}, " if quirk else ""
        topic = "default"
        for name, pattern in _TOPIC_PATTERNS:
            if pattern.search(user_text or ""):
                topic = name
                break
        pool = _MOCK_TOPIC_REPLIES.get(topic) or _MOCK_DEFAULT_REPLIES
        text = pool[seed % len(pool)].format(q=prefix, city=city)
        return _clean_reply(text)

    async def _llm_reply(
        self,
        friend: dict,
        user_text: str,
        history: list[dict],
        review_exprs: list[dict],
    ) -> str:
        # 无 key（mock）时不走通用模板，直接用带人设/话题的 deterministic 回复
        if getattr(self.engines.llm, "uses_mock", False):
            return self._mock_reply(friend, user_text)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self._system_prompt(friend, review_exprs)}
        ]
        for item in history[-10:]:
            role = "user" if item.get("sender") == "user" else "assistant"
            content = (item.get("transcript") or "").strip()
            if content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_text})

        chunks: list[str] = []
        try:
            async for delta in self.engines.llm.stream_chat(
                messages, temperature=0.85, max_tokens=140
            ):
                if delta:
                    chunks.append(delta)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM 回复失败（使用规则兜底）: %s: %s", type(exc).__name__, exc)

        text = _clean_reply("".join(chunks))
        if text:
            lang = _language_of(friend)
            if not _matches_language(text, lang):
                log.info(
                    "好友回复语言不符（期望 %s，实际 %r），尝试纠偏",
                    lang, text[:80],
                )
                fixed = await self._rewrite_in_language(friend, text, lang)
                if fixed:
                    return fixed
                # 纠偏失败：用目标语言的确定性回复兜底，绝不发错语言
                return self._mock_reply(friend, user_text)
            return text
        return self._mock_reply(friend, user_text)

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
                messages, temperature=0.2, max_tokens=220
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
        quirks = friend.get("speech_quirks") or []
        quirk = str(quirks[0]) if quirks else "well"
        name = friend.get("name", "your friend")
        options = [
            f"{quirk}, that sounds interesting. Tell me more about it?",
            f"{quirk}, I get what you mean. How did that make you feel?",
            f"Haha, {quirk}, seriously? What happened next?",
            f"{quirk}, I'm curious — what are you up to later today?",
        ]
        idx = int(time.time() * 1000 + len(name)) % len(options)
        return options[idx]

    async def store_friend_voice(self, friend: dict, text: str) -> dict:
        fid = friend["id"]
        audio_filename: str | None = None
        duration_ms = 0
        try:
            path, duration_ms = await self.voice.synth_to_wav(
                _strip_emoji(text) or text,
                friend.get("voice_id") or "en-GB-RyanNeural",
                rate=friend.get("voice_rate") or "+0%",
                pitch=friend.get("voice_pitch") or "+0Hz",
                out_dir=self.audio.audio_dir,
            )
            import os  # noqa: PLC0415

            audio_filename = os.path.basename(path)
        except Exception as exc:  # noqa: BLE001 - tone 再兜一层
            log.warning("TTS 合成失败（落 tone）: %s: %s", type(exc).__name__, exc)
            try:
                from app.voice.tone_tts import ToneSynthesizer  # noqa: PLC0415

                pcm = ToneSynthesizer(sample_rate=24000).synthesize(text)
                audio_filename = self.audio.save_tone_wav(pcm)
                duration_ms = int(len(pcm) / 24000 * 1000)
            except Exception as exc2:  # noqa: BLE001
                log.warning("tone 兜底也失败（无音频）: %s", exc2)
                audio_filename = None

        msg = await asyncio.to_thread(
            self.db.add_message,
            fid,
            "friend",
            text,
            msg_type="voice",
            audio_path=audio_filename,
            duration_ms=int(duration_ms or 0),
            is_read=False,
        )
        await asyncio.to_thread(self.db.touch_friend, fid)
        return msg

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

    async def _memory_worker(self, friend_id: str, delay_s: float | None = None) -> None:
        try:
            delay = self.memory_idle_s if delay_s is None else max(0.0, float(delay_s))
            if delay > 0:
                await asyncio.sleep(delay)
            friend = await asyncio.to_thread(self.db.get_friend, friend_id)
            if friend is None:
                return
            result = await self.memory.after_turn(friend)
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
