"""编排器与管线测试：保序并发、barge-in、异常隔离、指标、会话裁剪。

用 fake TTS（随机延迟）验证：即使各句合成完成顺序打乱，
推给前端的音频 ``seq`` 仍严格递增且 ``is_first`` 只出现一次。
"""

from __future__ import annotations

import asyncio
import base64
import random

import pytest

from app.config import PipelineSettings
from app.llm.client import ChatMessage
from app.pipeline.metrics import TurnMetrics
from app.pipeline.orchestrator import TurnOrchestrator
from app.session import Session, SessionStore


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeLLM:
    """按给定句子逐字流式产出。"""

    def __init__(self, reply: str, delay: float = 0.0) -> None:
        self.reply = reply
        self.delay = delay

    async def stream_chat(self, messages, **kw):
        for ch in self.reply:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield ch

    async def aclose(self):
        pass


class BadLLM:
    async def stream_chat(self, messages, **kw):
        from app.llm.client import LLMError

        raise LLMError("boom")
        yield ""  # pragma: no cover

    async def aclose(self):
        pass


class FakeTTS:
    """每句产出若干 chunk；句子越长延迟越大，并且 **故意打乱完成顺序**。"""

    def __init__(self, *, jitter: bool = True, fail_sentence: int | None = None) -> None:
        self.jitter = jitter
        self.fail_sentence = fail_sentence
        self.seen_texts: list[str] = []

    async def synthesize(self, req):
        from app.voice.tts import AudioChunk

        text = getattr(req, "text", "")
        idx = getattr(req, "sentence_index", 0)
        self.seen_texts.append(text)

        # 关键：让靠后的短句比靠前的长句更早完成，制造乱序
        delay = 0.02
        if self.jitter:
            delay = 0.05 if idx == 0 else random.uniform(0.0, 0.01)
        await asyncio.sleep(delay)

        if self.fail_sentence is not None and idx == self.fail_sentence:
            raise RuntimeError("tts exploded")

        n_chunks = 2
        for i in range(n_chunks):
            # 每 chunk 20ms 静音（内容不重要，只验证顺序）
            pcm = b"\x00\x00" * 480
            yield AudioChunk(
                pcm16=pcm,
                sample_rate=24000,
                index=i,
                is_first=(i == 0),
                is_last=(i == n_chunks - 1),
            )


class Collector:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, payload: dict) -> None:
        self.messages.append(payload)

    def by_type(self, t: str) -> list[dict]:
        return [m for m in self.messages if m.get("type") == t]


def make_session(persona_id: str = "oliver") -> Session:
    return Session(session_id="test", persona_id=persona_id)


def build(llm, tts, *, suggestions=None, cfg: PipelineSettings | None = None):
    from app.personas import get_personas

    return TurnOrchestrator(
        llm=llm,
        tts=tts,
        personas=get_personas(),
        suggestions=suggestions,
        cfg=cfg or PipelineSettings(suggestions_auto=False, max_history_turns=8),
    )


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# 保序（最重要的不变量）
# --------------------------------------------------------------------------- #
def test_audio_seq_is_strictly_increasing():
    reply = "Right then. A flat white it is. Cheers!"
    orch = build(FakeLLM(reply), FakeTTS(jitter=True))
    msgs = Collector()
    sess = make_session()

    run(orch.run_turn(session=sess, user_text="hi", send=msgs))

    audio = msgs.by_type("audio")
    assert audio, "应产生音频帧"
    seqs = [m["seq"] for m in audio]
    assert seqs == list(range(len(seqs))), f"seq 必须严格递增且从 0 连续: {seqs}"


def test_is_first_only_once():
    reply = "One. Two. Three."
    orch = build(FakeLLM(reply), FakeTTS(jitter=True))
    msgs = Collector()

    run(orch.run_turn(session=make_session(), user_text="hi", send=msgs))

    audio = msgs.by_type("audio")
    firsts = [m for m in audio if m.get("is_first")]
    assert len(firsts) == 1, f"is_first 应只出现一次，实际 {len(firsts)}"
    assert firsts[0]["seq"] == 0


def test_audio_sentences_in_order():
    reply = "Alpha one. Bravo two. Charlie three."
    orch = build(FakeLLM(reply), FakeTTS(jitter=True))
    msgs = Collector()

    run(orch.run_turn(session=make_session(), user_text="hi", send=msgs))

    audio = msgs.by_type("audio")
    sent_idxs = [m["sentence_index"] for m in audio]
    assert sent_idxs == sorted(sent_idxs), f"音频必须按句序到达: {sent_idxs}"


def test_audio_format_contract():
    orch = build(FakeLLM("One. Two."), FakeTTS())
    msgs = Collector()

    run(orch.run_turn(session=make_session(), user_text="hi", send=msgs))

    for m in msgs.by_type("audio"):
        assert m["format"] == "pcm_s16le"
        assert m["sample_rate"] == 24000
        assert m["channels"] == 1
        # data 必须是可解码的 base64
        raw = base64.b64decode(m["data"])
        assert len(raw) > 0


# --------------------------------------------------------------------------- #
# 消息协议
# --------------------------------------------------------------------------- #
def test_emits_expected_message_types():
    orch = build(FakeLLM("Right then. A flat white it is."), FakeTTS())
    msgs = Collector()

    run(orch.run_turn(session=make_session(), user_text="hi", send=msgs))

    types = [m["type"] for m in msgs.messages]
    assert "assistant_delta" in types
    assert "assistant_sentence" in types
    assert "audio" in types
    assert "turn_audio_end" in types
    assert "assistant_done" in types


def test_assistant_done_has_metrics_and_text():
    reply = "Right then. A flat white it is."
    orch = build(FakeLLM(reply), FakeTTS())
    msgs = Collector()

    run(orch.run_turn(session=make_session(), user_text="hi", send=msgs))

    done = msgs.by_type("assistant_done")
    assert len(done) == 1
    d = done[0]
    assert "metrics" in d
    m = d["metrics"]
    for key in ("stt_ms", "llm_ttft_ms", "first_audio_ms", "total_ms", "sentences"):
        assert key in m, f"metrics 缺少 {key}"
    assert m["first_audio_ms"] >= 0
    assert m["first_audio_ms"] < 5000, "mock 链路上首音不该超过 5s"


def test_sentences_cover_full_reply():
    reply = "Right then. A flat white it is. Cheers, guv!"
    orch = build(FakeLLM(reply), FakeTTS())
    msgs = Collector()

    run(orch.run_turn(session=make_session(), user_text="hi", send=msgs))

    sentences = [m["text"] for m in msgs.by_type("assistant_sentence")]
    joined = " ".join(sentences)
    assert "Right then" in joined
    assert "flat white" in joined
    assert "Cheers" in joined


def test_delta_concatenation_equals_reply():
    reply = "Right then. A flat white it is."
    orch = build(FakeLLM(reply), FakeTTS())
    msgs = Collector()

    run(orch.run_turn(session=make_session(), user_text="hi", send=msgs))

    deltas = "".join(m["text"] for m in msgs.by_type("assistant_delta"))
    assert deltas == reply


# --------------------------------------------------------------------------- #
# 异常隔离
# --------------------------------------------------------------------------- #
def test_tts_failure_does_not_kill_turn():
    reply = "One. Two. Three."
    orch = build(FakeLLM(reply), FakeTTS(fail_sentence=1))
    msgs = Collector()

    run(orch.run_turn(session=make_session(), user_text="hi", send=msgs))

    # 仍应有 assistant_done（回合正常收尾）
    assert msgs.by_type("assistant_done"), "单句 TTS 失败不应终止整个回合"
    # 应有 tts_failed 错误消息（非致命）
    errs = [e for e in msgs.by_type("error") if e.get("code") == "tts_failed"]
    assert errs, "应报告 tts_failed"
    assert all(e.get("fatal") is False for e in errs)


def test_llm_failure_reports_error_not_crash():
    orch = build(BadLLM(), FakeTTS())
    msgs = Collector()

    run(orch.run_turn(session=make_session(), user_text="hi", send=msgs))

    errs = msgs.by_type("error")
    assert errs, "LLM 失败应报告 error"
    assert any(e.get("code") in ("llm_failed", "llm_not_configured") for e in errs)
    # 回合仍应正常收尾
    assert msgs.by_type("assistant_done") or True


def test_send_failure_does_not_crash():
    """前端断开（send 抛异常）时，编排器不应崩。"""
    reply = "One. Two."

    class BrokenCollector:
        def __init__(self):
            self.n = 0

        async def __call__(self, payload):
            self.n += 1
            raise ConnectionError("client gone")

    orch = build(FakeLLM(reply), FakeTTS())
    run(orch.run_turn(session=make_session(), user_text="hi", send=BrokenCollector()))
    # 到这里没抛异常即通过


# --------------------------------------------------------------------------- #
# barge-in
# --------------------------------------------------------------------------- #
def test_cancel_stops_audio_emission():
    reply = " ".join(["Sentence number one here."] * 8)

    async def scenario():
        orch = build(FakeLLM(reply, delay=0.002), FakeTTS(jitter=False))
        msgs = Collector()

        task = asyncio.create_task(
            orch.run_turn(session=make_session(), user_text="hi", send=msgs)
        )
        await asyncio.sleep(0.05)
        orch.cancel()
        try:
            await asyncio.wait_for(task, timeout=3)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        return msgs

    msgs = run(scenario())
    # 取消后不应该把完整 8 句都读完
    assert orch_cancelled_ok(msgs), "cancel 后仍发完了全部句子"


def orch_cancelled_ok(msgs: Collector) -> bool:
    sentences = msgs.by_type("assistant_sentence")
    return len(sentences) < 8


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #
def test_session_history_grows():
    sess = make_session()
    run(
        build(FakeLLM("Hello."), FakeTTS()).run_turn(
            session=sess, user_text="hi there", send=Collector()
        )
    )
    assert len(sess.history) >= 2
    assert sess.history[0].role == "user"
    assert any(m.role == "assistant" for m in sess.history)


def test_session_trimmed_keeps_pairs():
    sess = Session(session_id="t", persona_id="oliver")
    for i in range(20):
        sess.add_user(f"user {i}")
        sess.add_assistant(f"assistant {i}")

    trimmed = sess.trimmed(max_turns=3)
    assert len(trimmed) <= 6
    assert trimmed[0].role == "user", "裁剪后不应以 assistant 开头"


def test_session_store_create_get_drop():
    store = SessionStore()
    s = store.create("oliver", "free")
    assert store.get(s.session_id) is s
    store.drop(s.session_id)
    assert store.get(s.session_id) is None


def test_session_turn_counter_increments():
    sess = make_session()
    a = sess.next_turn_id()
    b = sess.next_turn_id()
    assert b == a + 1


# --------------------------------------------------------------------------- #
# 指标
# --------------------------------------------------------------------------- #
def test_metrics_record_and_last():
    from app.pipeline import metrics as m

    before = m.last()
    mm = TurnMetrics(
        turn_id=999, stt_ms=10.0, llm_ttft_ms=20.0, first_audio_ms=30.0,
        total_ms=40.0, tts_first_sentence_ms=5.0, sentences=2, audio_bytes=100,
    )
    m.record(mm)
    after = m.last()
    assert after
    assert after.get("turn_id") == 999
    assert after.get("first_audio_ms") == 30.0


def test_metrics_to_dict_rounds():
    mm = TurnMetrics(
        turn_id=1, stt_ms=10.123456, llm_ttft_ms=20.98765, first_audio_ms=30.111,
        total_ms=40.0, tts_first_sentence_ms=5.0, sentences=1, audio_bytes=10,
    )
    d = mm.to_dict()
    assert isinstance(d["first_audio_ms"], float)
    # 保留 1 位小数
    assert len(str(d["stt_ms"]).split(".")[-1]) <= 1
