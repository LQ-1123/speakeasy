"""回合编排：LLM 流式 → 断句 → TTS → 有序音频上线。

对外契约见 ``docs/CONTRACTS.md`` 第 9 节，线协议见 ``docs/PROTOCOL.md`` 2.4–2.9。

流水线
------
::

    LLM stream ──delta──▶ splitter ──sentence──▶ create_task(TTS #0) ─┐
                                                   create_task(TTS #1) ─┤ asyncio.Queue（FIFO）
                                                   create_task(TTS #2) ─┘        │
                                                                     ┌───────────┘
                                                                     ▼
                                            单消费者协程：按 FIFO 顺序 await 每个任务 → send(audio)

要点
----
1. **保序并发**：句子一断出来就 ``create_task`` 进 TTS（GPU 一空就能开下一句），
   但音频发送由**唯一的消费者协程**按 FIFO（== sentence_index 升序）逐个 await 并发送，
   因此 ``seq`` 严格递增、``is_first`` 只出现在本回合第一个 chunk 上。
2. **不阻塞生产者**：LLM 循环只做"喂断句器 + 建任务 + 入队"，绝不 await 音频，
   所以 TTS 慢不会拖慢后续 token 的接收与断句（这才是真正的流水线并行）。
3. **首包延迟**：第一句一断出来立刻开合成，不等 LLM 结束。
4. **barge-in**：:meth:`TurnOrchestrator.cancel` 置位 + 取消 TTS 任务 + 关闭 LLM 生成器，
   消费者检测到取消位后安静收尾。
5. **异常隔离**：某句 TTS 失败 → 发 ``error(tts_failed, fatal=False)`` 并跳过该句，
   后续句子继续；LLM 失败 → ``error(llm_*)`` + ``assistant_done``（空文本）。
"""

from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass, field
from typing import Sequence, TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Protocol

from app.config import PipelineSettings, get_settings
from app.llm.client import ChatMessage, LLMClient, LLMConfigError, LLMError
from app.llm.sentence_splitter import StreamingSentenceSplitter
from app.llm.suggestions import SuggestionService
from app.logging_utils import get_logger
from app.personas import PersonaLibrary
from app.pipeline import metrics as metrics_mod
from app.pipeline.metrics import TurnMetrics
from app.session import Session

if TYPE_CHECKING:  # pragma: no cover - 仅类型提示；运行时不 import voice
    from app.voice.tts import AudioChunk, TTSEngine, TTSSentenceRequest

log = get_logger(__name__)

#: 往 WS 推一条 JSON 消息
SendFn = Callable[[dict[str, Any]], Awaitable[None]]

#: 音频下行固定采样率（契约 §0；TTS 层负责保证输出 24k）
OUTPUT_SAMPLE_RATE = 24000

#: 消费者队列哨兵：表示"不会再有新句子了"
_END = object()


class TTSEngineLike(Protocol):
    """``engines.tts`` 需要满足的最小接口（便于测试注入 fake）。"""

    def synthesize(self, req: Any) -> AsyncIterator[Any]: ...


# --------------------------------------------------------------------------- #
# voice 层 dataclass 的延迟获取（voice 可能尚未就绪）
# --------------------------------------------------------------------------- #
def _make_sentence_request(
    text: str, persona_id: str, sentence_index: int, is_first_sentence: bool
) -> Any:
    """构造 ``TTSSentenceRequest``；voice 层不可用时退回鸭子类型对象。"""
    try:
        from app.voice.tts import TTSSentenceRequest  # noqa: PLC0415

        return TTSSentenceRequest(
            text=text,
            persona_id=persona_id,
            sentence_index=sentence_index,
            is_first_sentence=is_first_sentence,
        )
    except Exception:  # pragma: no cover - 仅 voice 缺失时触发
        from types import SimpleNamespace

        return SimpleNamespace(
            text=text,
            persona_id=persona_id,
            sentence_index=sentence_index,
            is_first_sentence=is_first_sentence,
        )


# --------------------------------------------------------------------------- #
# 内部数据结构
# --------------------------------------------------------------------------- #
@dataclass
class _SentenceJob:
    """一句的合成任务（消费者按 index 顺序 await 它）。"""

    index: int
    text: str
    task: asyncio.Task[list[Any]]
    started_at: float = field(default_factory=time.perf_counter)


@dataclass
class _TurnStats:
    """一回合的原始测量值（绝对 ``perf_counter`` 时间点）。"""

    started_at: float = field(default_factory=time.perf_counter)
    llm_first_token_at: float | None = None
    first_sentence_at: float | None = None
    first_audio_sent_at: float | None = None
    first_sentence_tts_started_at: float | None = None
    first_sentence_first_chunk_at: float | None = None
    ended_at: float | None = None
    sentences_created: int = 0
    sentences_voiced: int = 0
    audio_chunks: int = 0
    audio_bytes: int = 0

    @staticmethod
    def _delta_ms(start: float | None, end: float | None) -> float:
        if start is None or end is None:
            return 0.0
        return round((end - start) * 1000.0, 2)


class TurnOrchestrator:
    """把一轮"用户说话 → 角色英文回应"的流水线跑完。

    每个 WS 连接持有一个实例（因为 barge-in 需要 per-connection 的取消状态）。
    """

    def __init__(
        self,
        *,
        llm: LLMClient,
        tts: TTSEngineLike,
        personas: PersonaLibrary,
        suggestions: SuggestionService | None = None,
        cfg: PipelineSettings | None = None,
    ) -> None:
        self.llm = llm
        self.tts = tts
        self.personas = personas
        self.suggestions = suggestions
        self.cfg: PipelineSettings = cfg or get_settings().pipeline

        self._cancelled = False
        self._running = False
        self._turn_id: int = 0
        self._session: Session | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._llm_aiter: AsyncIterator[str] | None = None
        self._suggestion_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def turn_id(self) -> int:
        return self._turn_id

    # ------------------------------------------------------------------ #
    # 对外：跑一回合
    # ------------------------------------------------------------------ #
    async def run_turn(
        self,
        *,
        session: Session,
        user_text: str,
        send: SendFn,
        turn_id: int | None = None,
        stt_ms: float = 0.0,
        images: "Sequence[str]" = (),
    ) -> None:
        """跑完一整轮并把所有消息推给 ``send``。

        参数
        ----
        session: 会话（会写入 user/assistant 历史）
        user_text: 用户本轮输入（STT 结果或直接文本）
        send: 往 WS 推消息的协程
        turn_id: 已分配的回合号；省略则从 session 分配（先发 ``user_text`` 的调用方应自己传）
        stt_ms: 本回合 STT 耗时，仅用于指标
        """
        self._cancelled = False
        self._running = True
        self._session = session
        session.add_user(user_text)
        #: 本轮的图片（data URL）。只在构造 messages 时挂到这一条 user 上，
        #: 历史里的旧图不重复发送（省 token）。
        self._turn_images = tuple(images or ())
        tid = turn_id if turn_id is not None else session.next_turn_id()
        self._turn_id = tid

        stats = _TurnStats()
        splitter = StreamingSentenceSplitter.from_settings(self.cfg)
        jobs: asyncio.Queue[Any] = asyncio.Queue()
        reply_parts: list[str] = []
        llm_error: BaseException | None = None
        sent_any_audio = False
        seq = 0

        # ---------------- 消费者协程（唯一发送音频的地方） ---------------- #
        async def _consume() -> None:
            nonlocal sent_any_audio, seq
            while True:
                item = await jobs.get()
                if item is _END:
                    return
                job: _SentenceJob = item
                try:
                    chunks = await job.task
                except asyncio.CancelledError:
                    if self._cancelled:
                        return
                    raise
                except Exception as exc:  # noqa: BLE001 - 单句失败不影响后续
                    log.warning(
                        "TTS failed (turn=%d sentence=%d %r): %s",
                        tid, job.index, _short(job.text), exc,
                    )
                    await _safe_send(
                        send,
                        {
                            "type": "error",
                            "code": "tts_failed",
                            "message": f"第 {job.index + 1} 句语音合成失败，已跳过",
                            "fatal": False,
                            "turn_id": tid,
                            "sentence_index": job.index,
                        },
                    )
                    continue

                if self._cancelled:
                    return
                if not chunks:
                    log.debug("sentence %d produced no audio (turn=%d)", job.index, tid)
                    continue

                stats.sentences_voiced += 1
                if job.index == 0 and stats.first_sentence_first_chunk_at is None:
                    stats.first_sentence_first_chunk_at = time.perf_counter()

                for chunk in chunks:
                    if self._cancelled:
                        return
                    pcm = _chunk_pcm(chunk)
                    if not pcm:
                        continue
                    now = time.perf_counter()
                    if stats.first_audio_sent_at is None:
                        stats.first_audio_sent_at = now
                    await _safe_send(
                        send,
                        {
                            "type": "audio",
                            "turn_id": tid,
                            "sentence_index": job.index,
                            "seq": seq,
                            "format": "pcm_s16le",
                            "sample_rate": _chunk_rate(chunk),
                            "channels": 1,
                            "data": base64.b64encode(pcm).decode("ascii"),
                            "is_first": not sent_any_audio,
                            "is_last": bool(_chunk_attr(chunk, "is_last", False)),
                        },
                    )
                    sent_any_audio = True
                    seq += 1
                    stats.audio_chunks += 1
                    stats.audio_bytes += len(pcm)

        consumer = asyncio.create_task(_consume(), name=f"turn-{tid}-audio")
        self._tasks.add(consumer)
        consumer.add_done_callback(self._tasks.discard)

        # ---------------- 生产者：LLM → 断句 → 排 TTS ------------------ #
        def _enqueue_sentence(sentence: str) -> None:
            """断出一句就立刻开 TTS 任务并入队（不 await，不阻塞生产者）。"""
            idx = stats.sentences_created
            stats.sentences_created += 1
            if stats.first_sentence_at is None:
                stats.first_sentence_at = time.perf_counter()
            job = self._spawn_tts_job(sentence, idx, session.persona_id)
            if idx == 0:
                # 第一句的 TTS 起点：用于 tts_first_sentence_ms
                stats.first_sentence_tts_started_at = job.started_at
            jobs.put_nowait(job)

        async def _emit_sentence_notice(sentence: str, idx: int) -> None:
            await _safe_send(
                send,
                {
                    "type": "assistant_sentence",
                    "turn_id": tid,
                    "sentence_index": idx,
                    "text": sentence,
                    "tts_backend": self._tts_backend_name(),
                },
            )

        try:
            try:
                aiter = self.llm.stream_chat(_to_openai_messages(self._build_messages(session, user_text)))
                self._llm_aiter = aiter
                async for delta in aiter:
                    if self._cancelled:
                        break
                    if not delta:
                        continue
                    if stats.llm_first_token_at is None:
                        stats.llm_first_token_at = time.perf_counter()
                    reply_parts.append(delta)
                    await _safe_send(
                        send,
                        {"type": "assistant_delta", "turn_id": tid, "text": delta},
                    )
                    for sentence in splitter.feed(delta):
                        if self._cancelled:
                            break
                        idx = stats.sentences_created
                        _enqueue_sentence(sentence)
                        await _emit_sentence_notice(sentence, idx)
            except asyncio.CancelledError:
                raise
            except LLMConfigError as exc:
                llm_error = exc
                log.warning("LLM 未配置/配置错误: %s", exc)
                await _safe_send(
                    send,
                    {
                        "type": "error",
                        "code": "llm_not_configured",
                        "message": str(exc),
                        "fatal": False,
                        "turn_id": tid,
                    },
                )
            except LLMError as exc:
                llm_error = exc
                log.warning("LLM 调用失败: %s", exc)
                await _safe_send(
                    send,
                    {
                        "type": "error",
                        "code": "llm_failed",
                        "message": str(exc),
                        "fatal": False,
                        "turn_id": tid,
                    },
                )
            finally:
                self._llm_aiter = None

            # 流结束：flush 断句器残留
            if not self._cancelled:
                for sentence in splitter.flush():
                    if self._cancelled:
                        break
                    idx = stats.sentences_created
                    _enqueue_sentence(sentence)
                    await _emit_sentence_notice(sentence, idx)
        except asyncio.CancelledError:
            self._cancelled = True
            _cancel_all(self._tasks)
            raise
        except Exception as exc:  # noqa: BLE001 - 任何未预期异常都不该断连
            log.exception("run_turn 未预期异常（turn=%d）", tid)
            await _safe_send(
                send,
                {
                    "type": "error",
                    "code": "internal",
                    "message": f"服务端内部错误: {type(exc).__name__}",
                    "fatal": False,
                    "turn_id": tid,
                },
            )
        finally:
            # 关闭队列并等待消费者把已排队的音频发完（除非被取消）
            jobs.put_nowait(_END)
            if not self._cancelled:
                try:
                    await asyncio.shield(consumer)
                except asyncio.CancelledError:
                    self._cancelled = True
                    _cancel_all(self._tasks)
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.debug("audio consumer ended with error: %s", exc)
            else:
                consumer.cancel()
                try:
                    await consumer
                except (asyncio.CancelledError, Exception):  # noqa: B014
                    pass
            self._running = False
            stats.ended_at = time.perf_counter()

        # ---------------- 收尾消息 ------------------------------------ #
        reply_text = "".join(reply_parts).strip()
        if self._cancelled:
            await _safe_send(
                send,
                {
                    "type": "turn_audio_end",
                    "turn_id": tid,
                    "total_bytes": stats.audio_bytes,
                    "sentences": stats.sentences_voiced,
                    "interrupted": True,
                },
            )
            return

        if reply_text:
            session.add_assistant(reply_text)

        if stats.audio_chunks > 0:
            await _safe_send(
                send,
                {
                    "type": "turn_audio_end",
                    "turn_id": tid,
                    "total_bytes": stats.audio_bytes,
                    "sentences": stats.sentences_voiced,
                },
            )

        m = TurnMetrics(
            turn_id=tid,
            stt_ms=float(stt_ms or 0.0),
            llm_ttft_ms=stats._delta_ms(stats.started_at, stats.llm_first_token_at),
            first_audio_ms=stats._delta_ms(stats.started_at, stats.first_audio_sent_at),
            total_ms=stats._delta_ms(stats.started_at, stats.ended_at),
            tts_first_sentence_ms=stats._delta_ms(
                stats.first_sentence_tts_started_at, stats.first_sentence_first_chunk_at
            ),
            sentences=stats.sentences_voiced,
            audio_bytes=stats.audio_bytes,
        )
        m.extra = {
            "llm_error": type(llm_error).__name__ if llm_error else None,
            "sentences_created": stats.sentences_created,
            "audio_chunks": stats.audio_chunks,
            "reply_chars": len(reply_text),
        }
        metrics_mod.record(m)

        await _safe_send(
            send,
            {
                "type": "assistant_done",
                "turn_id": tid,
                "text": reply_text,
                "metrics": m.to_dict(),
            },
        )

        # ---------------- 异步润色建议（绝不阻塞音频） ----------------- #
        if reply_text and self._should_suggest(user_text):
            task = asyncio.create_task(
                self._suggest_async(session, user_text, tid, send),
                name=f"turn-{tid}-suggest",
            )
            self._suggestion_tasks.add(task)
            task.add_done_callback(self._suggestion_tasks.discard)

    # ------------------------------------------------------------------ #
    # 取消（barge-in）
    # ------------------------------------------------------------------ #
    def cancel(self) -> None:
        """打断当前回合：置位 + 取消所有 TTS 任务 + 关闭 LLM 生成器。幂等、同步。"""
        self._cancelled = True

        aiter = self._llm_aiter
        self._llm_aiter = None
        if aiter is not None:
            aclose = getattr(aiter, "aclose", None)
            if aclose is not None:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(aclose())
                except RuntimeError:  # pragma: no cover - 无运行 loop
                    pass

        _cancel_all(self._tasks)
        log.debug("turn cancelled (barge-in)")

    async def aclose(self) -> None:
        """连接关闭：取消所有后台任务。"""
        self.cancel()
        pending = [t for t in list(self._tasks) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()

        sugg = [t for t in list(self._suggestion_tasks) if not t.done()]
        for task in sugg:
            task.cancel()
        if sugg:
            await asyncio.gather(*sugg, return_exceptions=True)
        self._suggestion_tasks.clear()

    # ------------------------------------------------------------------ #
    # 内部：TTS 任务
    # ------------------------------------------------------------------ #
    def _spawn_tts_job(self, sentence: str, index: int, persona_id: str) -> _SentenceJob:
        """创建一句的 TTS 任务；任务内部收集完整 chunk 列表后返回。"""
        req = _make_sentence_request(
            text=sentence,
            persona_id=persona_id,
            sentence_index=index,
            is_first_sentence=(index == 0),
        )

        async def _collect() -> list[Any]:
            chunks: list[Any] = []
            agen = self.tts.synthesize(req)
            try:
                async for chunk in agen:
                    chunks.append(chunk)
            finally:
                aclose = getattr(agen, "aclose", None)
                if aclose is not None:
                    try:
                        await aclose()
                    except Exception as exc:  # pragma: no cover - 清理失败不致命
                        log.debug("closing tts stream failed: %s", exc)
            return chunks

        task = asyncio.create_task(_collect(), name=f"tts-{index}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return _SentenceJob(index=index, text=sentence, task=task)

    # ------------------------------------------------------------------ #
    # 内部：消息构造
    # ------------------------------------------------------------------ #
    def _build_messages(self, session: Session, user_text: str) -> list[ChatMessage]:
        """system prompt（persona + mode + scene + 本轮话题）+ 裁剪后的历史。"""
        persona = self.personas.get(session.persona_id)
        system = self.personas.system_prompt_for(session.persona_id, session.mode_id)

        extra: list[str] = []
        if persona.scene:
            extra.append(f"## Scene (you are here right now)\n{persona.scene.strip()}")
        mode = self.personas.mode(session.mode_id) if session.mode_id else None
        if mode is not None:
            extra.append(f"## Current mode: {mode.name}\n{mode.description.strip()}")
        topic = (user_text or "").strip()
        if topic:
            extra.append(f"## What the learner just brought up (stay on it)\n{topic[:280]}")
        if extra:
            system = system.rstrip() + "\n\n" + "\n\n".join(extra)

        messages: list[ChatMessage] = [ChatMessage(role="system", content=system)]
        max_turns = int(getattr(self.cfg, "max_history_turns", 8))
        messages.extend(session.trimmed(max_turns))  # 已含本轮 user（run_turn 开头写入）

        # 把本轮图片挂到**最后一条 user** 上（多模态 parts 形式）
        imgs = getattr(self, "_turn_images", ())
        if imgs:
            for i in range(len(messages) - 1, -1, -1):
                if getattr(messages[i], "role", "") == "user":
                    messages[i] = ChatMessage(
                        role="user",
                        content=str(getattr(messages[i], "content", "")),
                        images=tuple(imgs),
                    )
                    break
        return messages

    # ------------------------------------------------------------------ #
    # 内部：建议
    # ------------------------------------------------------------------ #
    def _should_suggest(self, user_text: str) -> bool:
        if self.suggestions is None:
            return False
        if not bool(getattr(self.cfg, "suggestions_enabled", True)):
            return False
        if not bool(getattr(self.cfg, "suggestions_auto", True)):
            return False
        return bool((user_text or "").strip())

    async def _suggest_async(
        self, session: Session, user_text: str, turn_id: int, send: SendFn
    ) -> None:
        """后台生成建议并推送；任何失败静默（debug 日志）。"""
        if self.suggestions is None:  # pragma: no cover - 调用前已检查
            return
        try:
            items = await self.suggestions.suggest(
                user_text,
                persona_id=session.persona_id,
                history=session.trimmed(int(getattr(self.cfg, "max_history_turns", 8))),
            )
            if not items:
                return
            await _safe_send(
                send,
                {
                    "type": "suggestion",
                    "turn_id": turn_id,
                    "items": [i.to_dict() for i in items],
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.debug("async suggestion failed: %s: %s", type(exc).__name__, exc)

    def _tts_backend_name(self) -> str:
        name = getattr(self.tts, "backend_name", "tts")
        try:
            return str(name)
        except Exception:  # pragma: no cover
            return "tts"


# --------------------------------------------------------------------------- #
# 模块级工具
# --------------------------------------------------------------------------- #
def _cancel_all(tasks: set[asyncio.Task[Any]]) -> None:
    for task in list(tasks):
        if not task.done():
            task.cancel()


def _to_openai_messages(messages: list[ChatMessage]) -> list[dict[str, str]]:
    return [m.to_openai() for m in messages]


async def _safe_send(send: SendFn, message: dict[str, Any]) -> bool:
    """发送一条消息；连接已断时吞掉异常并返回 ``False``。"""
    try:
        await send(message)
        return True
    except Exception as exc:  # noqa: BLE001 - 协议层不应因发送失败崩溃
        log.debug("send failed (connection likely closed): %s: %s", type(exc).__name__, exc)
        return False


def _chunk_attr(chunk: Any, name: str, default: Any = None) -> Any:
    if isinstance(chunk, dict):
        return chunk.get(name, default)
    return getattr(chunk, name, default)


def _chunk_pcm(chunk: Any) -> bytes:
    pcm = _chunk_attr(chunk, "pcm16", b"")
    return bytes(pcm) if isinstance(pcm, (bytes, bytearray, memoryview)) else b""


def _chunk_rate(chunk: Any) -> int:
    rate = _chunk_attr(chunk, "sample_rate", OUTPUT_SAMPLE_RATE)
    try:
        return int(rate)
    except (TypeError, ValueError):  # pragma: no cover
        return OUTPUT_SAMPLE_RATE


def _short(text: str, limit: int = 48) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


__all__ = [
    "OUTPUT_SAMPLE_RATE",
    "SendFn",
    "TurnOrchestrator",
]
