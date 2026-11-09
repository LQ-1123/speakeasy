"""Per-call state: heard context, idempotent turns and bounded text/audio pipeline."""
from __future__ import annotations

import asyncio
import contextlib
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from app.v2.chat_service import _matches_language, _split_ready_sentences, split_emotion


@dataclass
class CallRuntime:
    history: list[dict[str, str]] = field(default_factory=list)
    seen_turns: set[str] = field(default_factory=set)
    current_turn: str = ""
    pending: OrderedDict[str, dict] = field(default_factory=OrderedDict)
    sequence: int = 0
    played_turns: int = 0
    generation: asyncio.Task | None = None
    socket: Any = None
    started_at: float = 0
    summarized: bool = False
    user_id: str = ""
    friend_id: str = ""
    disconnect_version: int = 0
    media: dict[str, dict] = field(default_factory=dict)
    audio_dir: str = ""

    def begin_turn(self, turn_id: str, text: str) -> bool:
        if not turn_id or turn_id in self.seen_turns or len(self.seen_turns) >= 2000:
            return False
        self.interrupt()
        self.current_turn = turn_id
        self.seen_turns.add(turn_id)
        self.history.append({"role": "user", "content": text})
        self.history = self.history[-20:]
        return True

    def offer_audio(self, audio_id: str, turn_id: str, text: str, index: int) -> None:
        if turn_id == self.current_turn:
            self.pending[audio_id] = {"turn_id": turn_id, "text": text, "index": index}

    def acknowledge(self, audio_id: str, turn_id: str) -> bool:
        if not self.pending or turn_id != self.current_turn:
            return False
        first = next(iter(self.pending))
        if first != audio_id:
            return False
        item = self.pending.pop(first)
        if item["index"] == 0:
            self.played_turns += 1
        if self.history and self.history[-1]["role"] == "assistant":
            self.history[-1]["content"] += " " + item["text"]
        else:
            self.history.append({"role": "assistant", "content": item["text"]})
        self.history = self.history[-20:]
        return True

    def interrupt(self) -> None:
        if self.generation and not self.generation.done():
            self.generation.cancel()
        self.current_turn = ""
        self.pending.clear()
        self.media.clear()


async def reply_audio(chat: Any, friend: dict, text: str, history: list[dict], *, audio_dir: str | None = None) -> AsyncIterator[dict]:
    """One model reader and one synthesizer, with at most 4 queued sentences.

    Backpressure bounds memory. Model/TTS failure is an error, never fabricated audio.
    The caller owns interruption and adds only playback-confirmed text to history.
    """
    llm = chat.engines.llm
    if getattr(llm, "uses_mock", False):
        raise RuntimeError("model-unavailable")
    if not text.strip():
        raise RuntimeError("empty-transcript")
    plan = chat._call_turn_plan(friend, text)
    messages = [{"role": "system", "content": chat._call_system_prompt(friend, plan)}, *history[-12:]]
    if not messages or messages[-1].get("role") != "user" or messages[-1].get("content") != text:
        messages.append({"role": "user", "content": text})
    sentences: asyncio.Queue = asyncio.Queue(maxsize=4)
    language = friend.get("reply_language") or "en"

    async def produce() -> None:
        buffer = ""
        try:
            async with asyncio.timeout(25):
                async for delta in llm.stream_chat(
                    messages, temperature=.65, max_tokens=220,
                    extra={"thinking": {"type": "disabled"}},
                ):
                    buffer += delta or ""
                    ready, buffer = _split_ready_sentences(buffer, min_chars=1, max_chars=90)
                    for sentence in ready:
                        await sentences.put(sentence)
                if buffer.strip():
                    await sentences.put(buffer.strip())
            await sentences.put(None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await sentences.put(exc)

    producer = asyncio.create_task(produce(), name="call-model-reader")
    emitted = 0
    try:
        while True:
            sentence = await sentences.get()
            if sentence is None:
                break
            if isinstance(sentence, Exception):
                raise RuntimeError("model-unavailable") from sentence
            display, speech = split_emotion(sentence)
            if not display:
                continue
            if not _matches_language(display, language):
                raise RuntimeError("wrong-language")
            synthesis = chat._synth_sentence(speech, friend, out_dir=audio_dir) if audio_dir else chat._synth_sentence(speech, friend)
            audio = await asyncio.wait_for(synthesis, timeout=12)
            if not audio or not audio.get("url"):
                raise RuntimeError("speech-unavailable")
            yield {**audio, "text": display, "index": emitted}
            emitted += 1
        if emitted == 0:
            raise RuntimeError("empty-reply")
    finally:
        producer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await producer
